import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

from letee import config, cockpit, operations
from letee.names import Target


class OperationsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, LETEE_CONFIG_DIR=self.directory.name)
        self.env.start()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.env.stop)
        self.work = Target("local", "work")
        config.save_sessions([self.work])

    def test_remove_invalidates_queued_restore_but_explicit_navigation_is_allowed(self):
        restore = operations.capture(operations.Effect("switch", self.work, automatic=True))
        with patch.object(cockpit, "current_target", return_value=self.work), patch.object(cockpit, "reset_to_help"), patch.object(cockpit, "switch") as switch:
            removed = operations.perform(operations.Effect("save_favorites", self.work), ())
            self.assertFalse(removed.error)
            self.assertTrue(operations.perform(restore, ()).stale_navigation)
            with patch.object(operations.sessions, "attach_command", return_value="attach"):
                self.assertFalse(operations.perform(operations.Effect("switch", self.work), ()).error)
            switch.assert_called_once()

    def test_remove_probe_failure_keeps_tracking_and_operation_state(self):
        state = config.load_operations()
        with patch.object(cockpit, "current_target", side_effect=OSError("probe failed")), \
             patch.object(cockpit, "reset_to_help") as reset:
            result = operations.perform(operations.Effect("save_favorites", self.work))

        self.assertEqual(result.error, "probe failed")
        self.assertEqual(result.favorites, (self.work,))
        self.assertEqual(config.load_sessions(), [self.work])
        self.assertEqual(config.load_operations(), state)
        self.assertFalse(result.partial_success)
        reset.assert_not_called()

    def test_same_target_relaunch_invalidates_old_overlay(self):
        with patch.object(cockpit, "current_target", return_value=self.work), patch.object(cockpit, "switch"), patch.object(cockpit, "show_unavailable") as overlay:
            first = operations.perform(operations.Effect("switch", self.work), ())
            delayed = operations.capture(operations.Effect("show_unavailable", self.work, automatic=True))
            second = operations.perform(operations.Effect("switch", self.work), ())
            self.assertNotEqual(first.launch_id, second.launch_id)
            self.assertTrue(operations.perform(delayed, ()).stale_navigation)
            overlay.assert_not_called()

    def test_automatic_probe_failure_keeps_identity_after_same_target_relaunch(self):
        for probe in ("current_target", "startup_deadline"):
            with self.subTest(probe=probe):
                old_launch = operations.launch_identity()
                sessions_lock = config.sessions_lock
                relaunched = []

                @contextmanager
                def relaunch_after_unlock():
                    try:
                        with sessions_lock() as acquired:
                            yield acquired
                    finally:
                        with patch.object(config, "sessions_lock", sessions_lock), \
                             patch.object(cockpit, "current_target", return_value=self.work), \
                             patch.object(cockpit, "startup_deadline", return_value=None), \
                             patch.object(cockpit, "switch"):
                            relaunched.append(operations.perform(operations.Effect("switch", self.work)))

                with patch.object(config, "sessions_lock", relaunch_after_unlock), \
                     patch.object(cockpit, "current_target", return_value=self.work), \
                     patch.object(cockpit, "startup_deadline", return_value=None), \
                     patch.object(cockpit, probe, side_effect=OSError("probe failed")):
                    result = operations.perform(operations.Effect("show_unavailable", self.work, automatic=True))

                self.assertEqual(result.error, "probe failed")
                self.assertFalse(relaunched[0].error)
                self.assertNotEqual(old_launch, relaunched[0].launch_id)
                self.assertEqual(result.launch_id, old_launch)
                self.assertFalse(operations.is_current(result))

    def test_other_process_mutations_reject_queued_restoration(self):
        script = """
import sys
from unittest.mock import patch
from letee import cockpit, operations
from letee.names import Target
work = Target('local', 'work')
with patch.object(cockpit, 'current_target', return_value=None), \\
     patch.object(cockpit, 'reset_to_help'), \\
     patch.object(cockpit, 'rename_target'), \\
     patch.object(operations.sessions, 'kill'), \\
     patch.object(operations.sessions, 'rename'):
    result = operations.perform(operations.Effect(sys.argv[1], work, message='renamed'))
    assert not result.error, result.error
"""
        for kind in ("kill", "save_favorites", "rename"):
            with self.subTest(kind=kind):
                config.save_sessions([self.work])
                with config.sessions_lock():
                    config.save_operations({"launch": kind, "invalidated": []})
                queued = operations.capture(operations.Effect("ensure_created", self.work, automatic=True))
                subprocess.run([sys.executable, "-c", script, kind], check=True, timeout=5)
                with patch.object(operations.sessions, "ensure_created") as create:
                    self.assertTrue(operations.perform(queued).stale_navigation)
                create.assert_not_called()

    def test_concurrent_add_intents_reload_tracking(self):
        other = Target("local", "other")
        barrier = threading.Barrier(3)
        results = []
        def add(target):
            barrier.wait()
            results.append(operations.perform(operations.Effect("add_switch", target), ()))
        with patch.object(cockpit, "switch"):
            workers = [threading.Thread(target=add, args=(target,)) for target in (self.work, other)]
            for worker in workers:
                worker.start()
            barrier.wait()
            for worker in workers:
                worker.join(2)
                self.assertFalse(worker.is_alive())
        self.assertTrue(all(not result.error for result in results))
        self.assertCountEqual(config.load_sessions(), [self.work, other])

    def test_obsolete_completion_releases_only_its_own_pending_work(self):
        from letee.sidebar import SessionReconciler
        from letee.discovery import SessionSnapshot, SourceSnapshot
        from unittest.mock import Mock

        reconciler = SessionReconciler()
        submit = Mock(return_value=True)
        snapshot = SessionSnapshot(SourceSnapshot(True, (), frozenset()), {})
        reconciler.reconcile((self.work,), None, snapshot, submit, 0)
        old = submit.call_args.args[0]
        with patch.object(operations, "is_current", return_value=False):
            reconciler.observe(operations.EffectResult(old, (self.work,), launch_id="old"))
        self.assertNotIn(self.work, reconciler._pending)
        self.assertFalse(reconciler._awaiting_discovery)

        reconciler.reconcile((self.work,), None, snapshot, submit, 1)
        newer = submit.call_args.args[0]
        self.assertNotEqual(old.request_id, newer.request_id)
        with patch.object(operations, "is_current", return_value=False):
            reconciler.observe(operations.EffectResult(old, (self.work,), launch_id="old"))
        self.assertEqual(reconciler._pending[self.work][0], newer.request_id)
        reconciler.observe(operations.EffectResult(newer, (self.work,)))
        self.assertNotIn(self.work, reconciler._pending)
        self.assertIs(reconciler._completed[self.work], snapshot.local)

    def test_slow_attachment_preparation_keeps_transaction_off_ui_thread(self):
        from letee.sidebar import EffectRunner

        entered, release, removed = threading.Event(), threading.Event(), threading.Event()
        def prepare(target):
            entered.set()
            self.assertTrue(release.wait(2))
            return "attach"

        def switch(target, command, **kwargs):
            self.assertEqual(command(), "attach")

        runner = EffectRunner()
        def remove():
            operations.perform(operations.Effect("save_favorites", self.work))
            removed.set()

        remover = threading.Thread(target=remove)
        try:
            with patch.object(operations.sessions, "attach_command", side_effect=prepare), \
                 patch.object(cockpit, "switch", side_effect=switch), \
                 patch.object(cockpit, "current_target", return_value=None):
                self.assertTrue(runner.submit(operations.Effect("switch", self.work), (self.work,)))
                self.assertTrue(entered.wait(1))
                self.assertIsNone(runner.poll())
                with config.sessions_lock(blocking=False) as acquired:
                    self.assertFalse(acquired)
                remover.start()
                self.assertFalse(removed.wait(0.05))
                release.set()
                remover.join(2)
                self.assertTrue(removed.is_set())
                runner._future.result(timeout=2)
            self.assertEqual(config.load_sessions(), [])
        finally:
            release.set()
            if remover.ident is not None:
                remover.join(2)
            runner.close()

    @patch("letee.sidebar.load_timing", return_value=config.Timing(ssh_auth_timeout=10))
    @patch.object(operations, "clear_startup")
    def test_observed_launch_keeps_completion_based_discovery_wait(self, _clear, _timing):
        from letee.sidebar import SessionReconciler
        from letee.discovery import SessionSnapshot, SourceSnapshot
        from unittest.mock import Mock

        remote = Target("ssh", "work", "dev")
        reconciler = SessionReconciler()
        effect = operations.Effect("switch", remote)
        with patch.object(cockpit, "switch"):
            result = operations.perform(effect)
        reconciler.observe(replace(result, completed_at=10))
        submit = Mock(return_value=True)
        snapshot = SessionSnapshot(SourceSnapshot(True, (), frozenset()), {
            "dev": SourceSnapshot(True, (), frozenset()),
        })
        # Shared deadline starts before slow preparation; local wait starts at completion.
        reconciler.reconcile((remote,), remote, snapshot, submit, 16, 15, result.launch_id)
        for worker in reconciler._startup_clears.values():
            worker.join(1)
        submit.assert_not_called()

    def test_rename_back_allows_fresh_restoration_not_old_queued_work(self):
        queued = operations.capture(operations.Effect("ensure_created", self.work, automatic=True))
        with patch.object(cockpit, "current_target", return_value=None), \
             patch.object(cockpit, "rename_target"), \
             patch.object(operations.sessions, "rename"), \
             patch.object(operations.sessions, "ensure_created") as ensure:
            operations.rename(self.work, "other")
            operations.rename(Target("local", "other"), "work")
            self.assertTrue(operations.perform(queued).stale_navigation)
            current = operations.perform(operations.Effect("ensure_created", self.work, automatic=True))
        self.assertFalse(current.stale_navigation)
        self.assertFalse(current.error)
        ensure.assert_called_once_with(self.work)

    def test_cli_create_does_not_track(self):
        with patch.object(operations.sessions, "create"), patch.object(cockpit, "require_cockpit"), patch.object(cockpit, "switch"):
            operations.create_and_switch(Target("local", "new"))
        self.assertEqual(config.load_sessions(), [self.work])
