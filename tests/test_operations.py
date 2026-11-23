import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

from tests import disable_session_restore, use_bare_inner_tmux
from letee import config, cockpit, operations
from letee.names import PaneTarget, Target



def setUpModule():
    use_bare_inner_tmux()
    disable_session_restore()

class OperationsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, LETEE_CONFIG_DIR=self.directory.name)
        self.env.start()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.env.stop)
        readiness = patch.object(operations.sessions, "remote_ready", return_value=False, create=True)
        self.ready = readiness.start()
        self.addCleanup(readiness.stop)
        self.work = Target("local", "work")
        config.save_sessions([self.work])

    def test_latest_remote_selection_supersedes_running_and_queued_navigation(self):
        from letee import sidebar

        entered, release = threading.Event(), threading.Event()
        remote = Target("ssh", "remote", "dev")
        queued = Target("local", "queued")
        runner = sidebar.EffectRunner()
        perform = sidebar._perform_effect

        def blocked_perform(effect, favorites):
            if effect.target == self.work:
                entered.set()
                self.assertTrue(release.wait(2))
            return perform(effect, favorites)

        with patch.object(config, "load_session_restore", return_value=True), \
             patch.object(sidebar, "_perform_effect", side_effect=blocked_perform), \
             patch.object(operations.sessions, "restore_remote"), \
             patch.object(operations.sessions, "ensure_created"), \
             patch.object(operations.sessions, "attach_command", return_value="attach"), \
             patch.object(cockpit, "switch_reconnecting"), \
             patch.object(cockpit, "switch") as switch:
            try:
                self.assertTrue(runner.submit(operations.Effect("switch", self.work), ()))
                self.assertTrue(entered.wait(1))
                self.assertTrue(runner.submit(operations.Effect("switch", queued), ()))
                self.assertTrue(runner.submit(operations.Effect("switch", remote), ()))
                runner._recoveries[remote].result(timeout=2)
                self.assertEqual(runner.poll().effect.target, remote)
                release.set()
                runner._future.result(timeout=2)
                self.assertTrue(runner.poll().stale_navigation)
                self.assertFalse(runner.busy)
                self.assertIsNone(runner._pending_navigation)
                self.assertEqual([call.args[0] for call in switch.call_args_list], [remote])
            finally:
                release.set()
                runner.close()

    def test_ready_remote_skips_restore_and_progress_without_caching(self):
        target = Target("ssh", "work", "dev")
        self.ready.side_effect = [True, False]
        with patch.object(config, "load_session_restore", return_value=True), \
             patch.object(operations.sessions, "restore_remote", return_value=None) as restore, \
             patch.object(operations.sessions, "ensure_created"), \
             patch.object(operations.sessions, "attach_command", return_value="attach"), \
             patch.object(cockpit, "switch_reconnecting") as progress, \
             patch.object(cockpit, "switch") as switch:
            result = operations.perform(operations.Effect("switch", target))
            self.assertFalse(result.error)
            progress.assert_not_called()
            restore.assert_not_called()
            switch.assert_called_once()
            operations.perform(operations.Effect("switch", target))
            progress.assert_called_once()
            restore.assert_called_once_with(target)

    def test_ready_automatic_recovery_skips_progress_and_restore(self):
        target = Target("ssh", "work", "dev")
        config.save_sessions([target])
        self.ready.return_value = True
        with patch.object(config, "load_session_restore", return_value=True), \
             patch.object(operations.sessions, "restore_remote") as restore, \
             patch.object(operations.sessions, "ensure_created") as ensure, \
             patch.object(operations.sessions, "attach_command", return_value="attach"), \
             patch.object(cockpit, "current_target", return_value=target), \
             patch.object(cockpit, "startup_deadline", return_value=None), \
             patch.object(cockpit, "show_reconnecting") as progress, \
             patch.object(cockpit, "switch"):
            result = operations.perform(operations.Effect("switch", target, automatic=True))
            self.assertFalse(result.error)
            self.assertFalse(result.stale_navigation)
            progress.assert_not_called()
            restore.assert_not_called()
            ensure.assert_called_once_with(target)

    def test_probe_runs_outside_lock_and_stale_probe_does_not_show_progress(self):
        target = Target("ssh", "work", "dev")
        def probe(_target):
            with config.sessions_lock(blocking=False) as acquired:
                self.assertTrue(acquired)
                operations._advance(config.load_operations(), explicit=self.work)
            return False
        self.ready.side_effect = probe
        with patch.object(config, "load_session_restore", return_value=True), \
             patch.object(operations.sessions, "restore_remote") as restore, \
             patch.object(cockpit, "switch_reconnecting") as progress, \
             patch.object(cockpit, "switch") as switch:
            result = operations.perform(operations.Effect("switch", target))
            self.assertTrue(result.stale_navigation)
            progress.assert_not_called()
            restore.assert_not_called()
            switch.assert_not_called()

    def test_restore_failure_is_reported_after_fallback(self):
        target = Target("ssh", "work", "dev")
        with patch.object(config, "load_session_restore", return_value=True), \
             patch.object(operations.sessions, "_run", side_effect=RuntimeError("restore broke")), \
             patch.object(operations.sessions, "ensure_created") as ensure, \
             patch.object(operations.sessions, "attach_command", return_value="attach"), \
             patch.object(cockpit, "switch_reconnecting"), \
             patch.object(cockpit, "switch") as switch:
            result = operations.perform(operations.Effect("create", target))
        ensure.assert_called_once_with(target)
        switch.assert_called_once()
        self.assertIn("restore broke", result.error)
        self.assertTrue(result.partial_success)

    def test_pane_switch_requires_remote_recovery(self):
        pane = PaneTarget(Target("ssh", "work", "dev"), "@1", "%2", "/tmp/test.sock")
        with patch.object(config, "load_session_restore", return_value=True):
            self.assertTrue(operations.needs_remote_recovery(operations.Effect("switch_pane", pane)))

    def test_remote_restore_runs_outside_lock_and_stale_completion_does_not_switch(self):
        target = Target("ssh", "work", "dev")
        entered, finish = threading.Event(), threading.Event()
        results = []
        def restore(_target):
            entered.set()
            self.assertTrue(finish.wait(2))
        with patch.object(config, "load_session_restore", return_value=True), \
             patch.object(operations.sessions, "restore_remote", side_effect=restore), \
             patch.object(cockpit, "switch_reconnecting"), \
             patch.object(cockpit, "switch") as switch:
            worker = threading.Thread(target=lambda: results.append(operations.perform(operations.Effect("switch", target))))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                with config.sessions_lock(blocking=False) as acquired:
                    self.assertTrue(acquired)
                    operations._advance(config.load_operations(), explicit=self.work)
            finally:
                finish.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertTrue(results[0].stale_navigation)
            switch.assert_not_called()

    def test_automatic_remote_recovery_uses_restore_before_create(self):
        target = Target("ssh", "work", "dev")
        config.save_sessions([target])
        for kind in ("switch", "ensure_created"):
            order = []
            with self.subTest(kind=kind), \
                 patch.object(config, "load_session_restore", return_value=True), \
                 patch.object(operations.sessions, "restore_remote", side_effect=lambda _: order.append("restore")), \
                 patch.object(operations.sessions, "ensure_created", side_effect=lambda _: order.append("create")), \
                 patch.object(cockpit, "current_target", return_value=target), \
                 patch.object(cockpit, "startup_deadline", return_value=None), \
                 patch.object(cockpit, "show_reconnecting") as progress, \
                 patch.object(cockpit, "switch"):
                result = operations.perform(operations.Effect(kind, target, automatic=True))
                self.assertFalse(result.error)
                self.assertFalse(result.stale_navigation)
                self.assertEqual(order, ["restore", "create"])
                progress.assert_called_once_with(target, "restoring…")

    def test_same_host_requests_share_inflight_restore(self):
        sessions = operations.sessions
        target = Target("ssh", "one", "dev")
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
        def restore(_target):
            entered.set()
            release.wait(2)
            raise RuntimeError("shared restore failure")
        with patch.object(sessions, "_restore_remote", side_effect=restore) as restore_mock:
            owner = threading.Thread(target=sessions.restore_remote, args=(target,))
            owner.start()
            self.assertTrue(entered.wait(1))
            with sessions._remote_restores_lock:
                future = sessions._remote_restores["dev"]
            original = future.result
            def wait():
                waiting.set()
                return original()
            with patch.object(future, "result", side_effect=wait):
                follower = threading.Thread(target=sessions.restore_remote, args=(Target("ssh", "two", "dev"),))
                follower.start()
                try:
                    self.assertTrue(waiting.wait(1))
                    self.assertTrue(sessions.remote_restoring(target))
                    self.assertFalse(sessions.remote_restoring(Target("ssh", "other", "prod")))
                finally:
                    release.set()
                    owner.join(2)
                    follower.join(2)
            restore_mock.assert_called_once_with(target)
            self.assertEqual(original().error, "shared restore failure")
            self.assertFalse(owner.is_alive())
            self.assertFalse(follower.is_alive())
            self.assertFalse(sessions.remote_restoring(target))

    def test_remote_restore_precedes_creation_and_attaches_only(self):
        target = Target("ssh", "work", "dev")
        order = []
        with patch.object(config, "load_session_restore", return_value=True), \
             patch.object(operations.sessions, "restore_remote", side_effect=lambda _: order.append("restore")), \
             patch.object(operations.sessions, "ensure_created", side_effect=lambda _: order.append("create")), \
             patch.object(cockpit, "switch_reconnecting"), \
             patch.object(cockpit, "switch") as switch:
            result = operations.perform(operations.Effect("switch", target))
            self.assertFalse(result.error)
            self.assertEqual(order, ["restore", "create"])
            self.assertIn("attach-session", switch.call_args.args[1])
            self.assertNotIn("new-session", switch.call_args.args[1])

    def test_existing_legacy_navigation_uses_attach_only(self):
        for kind in ("switch", "add_switch"):
            for target in (Target("local", "work.one"), Target("ssh", "work.one", "dev")):
                with self.subTest(kind=kind, target=target):
                    config.save_sessions([self.work])
                    with patch.object(operations.sessions, "_run", return_value="") as probe, \
                         patch.object(operations.sessions, "load_tmux_config_overlay", return_value=False), \
                         patch.object(cockpit, "switch") as switch:
                        result = operations.perform(operations.Effect(kind, target))
                        self.assertFalse(result.error)
                        command = switch.call_args.args[1]
                        command = command() if callable(command) else command
                    self.assertIn("attach-session", command)
                    self.assertNotIn("new-session", command)
                    probe.assert_called_once()
                    query = probe.call_args.args[2]
                    if target.kind == "local":
                        self.assertEqual(query[-3:], ("has-session", "-t", "=work.one"))
                    else:
                        self.assertIn("has-session -t =work.one", query[-1])
                    expected = [self.work, target] if kind == "add_switch" else [self.work]
                    self.assertEqual(config.load_sessions(), expected)

    def test_missing_legacy_navigation_keeps_favorites_and_operation_state(self):
        for kind in ("switch", "add_switch"):
            for target in (Target("local", "work.one"), Target("ssh", "work.one", "dev")):
                with self.subTest(kind=kind, target=target):
                    state = config.load_operations()
                    with patch.object(operations.sessions, "_run", side_effect=SystemExit("missing session")), \
                         patch.object(cockpit, "switch") as switch:
                        result = operations.perform(operations.Effect(kind, target))
                    self.assertEqual(result.error, "missing session")
                    self.assertEqual(result.favorites, (self.work,))
                    self.assertEqual(config.load_sessions(), [self.work])
                    self.assertEqual(config.load_operations(), state)
                    switch.assert_not_called()

    def test_create_rejects_legacy_names_before_side_effects(self):
        for target in (Target("local", "work.one"), Target("ssh", "work.one", "dev")):
            with self.subTest(target=target):
                state = config.load_operations()
                with patch.object(operations.sessions, "_run") as command, \
                     patch.object(cockpit, "switch") as switch:
                    result = operations.perform(operations.Effect("create", target))
                self.assertIn("Invalid session", result.error)
                self.assertEqual(config.load_sessions(), [self.work])
                self.assertEqual(config.load_operations(), state)
                command.assert_not_called()
                switch.assert_not_called()

    def test_legacy_navigation_allows_explicit_attach_and_pane_targets(self):
        target = Target("local", "work.one")
        pane = PaneTarget(target, "@1", "%2", "/tmp/inner.sock")
        effects = (
            operations.Effect("switch", target, attach_command="attach-only"),
            operations.Effect("switch_pane", pane),
        )
        for effect in effects:
            with self.subTest(effect=effect):
                with patch.object(cockpit, "switch") as switch:
                    result = operations.perform(effect)
                self.assertFalse(result.error)
                switch.assert_called_once()
                command = switch.call_args.args[1]
                if effect.attach_command is not None:
                    self.assertEqual(command, "attach-only")
                else:
                    self.assertIn("attach-session", command())

    def test_attachment_failure_is_launch_scoped_and_allows_retry_during_startup(self):
        launch = operations.launch_identity()
        marker = f"{self.work.format()}:{launch}"
        with patch.object(cockpit, "_option", return_value=marker), \
             patch.object(cockpit, "current_target", return_value=self.work), \
             patch.object(cockpit, "status_snapshot", return_value=cockpit.StatusSnapshot(self.work, None, None, True)), \
             patch.object(cockpit, "startup_deadline", return_value=float("inf")), \
             patch.object(operations.sessions, "ensure_created") as ensure, \
             patch.object(operations.sessions, "attach_command", return_value="attach"), \
             patch.object(cockpit, "switch") as switch:
            self.assertTrue(operations.sample_target()[1].attachment_failed)
            result = operations.perform(operations.Effect("switch", self.work, automatic=True))
            self.assertFalse(result.error)
            self.assertFalse(result.stale_navigation)
            ensure.assert_called_once_with(self.work)
            switch.assert_called_once()
            self.assertNotEqual(launch, result.launch_id)
            self.assertFalse(operations.sample_target()[1].attachment_failed)
            queued = operations.perform(operations.Effect("switch", self.work, automatic=True))
            self.assertTrue(queued.stale_navigation)
            self.assertEqual(switch.call_count, 1)

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

    def test_reconnecting_overlays_receive_target_status(self):
        target = Target("ssh", "work", "dev")
        config.save_sessions([target])
        with patch.object(cockpit, "current_target", return_value=target), \
             patch.object(cockpit, "startup_deadline", return_value=None), \
             patch.object(cockpit, "show_reconnecting") as show, \
             patch.object(cockpit, "switch_reconnecting") as switch:
            automatic = operations.perform(operations.capture(
                operations.Effect("show_reconnecting", target, automatic=True, message="letee missing")), ())
            explicit = operations.perform(operations.Effect("show_reconnecting", target, message="letee missing"), ())
        self.assertFalse(automatic.error or explicit.error)
        show.assert_called_once_with(target, "letee missing")
        self.assertEqual(switch.call_args.args, (target, "letee missing"))

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
        for worker, _ in reconciler._startup_clears.values():
            worker.join(1)
        submit.assert_not_called()

    def test_delayed_automatic_attachment_does_not_recreate_renamed_remote_session(self):
        target = Target("ssh", "work", "dev")
        renamed = Target("ssh", "other", "dev")
        config.save_sessions([target])
        remote_sessions = {"work"}
        launched = []
        ensured = []

        def run(operation, target, command, **kwargs):
            with config.sessions_lock(blocking=False) as acquired:
                self.assertFalse(acquired)
            tokens = shlex.split(command[-1])
            if operation == "create" and "new-session" in tokens:
                ensured.append(target)
                if target.session in remote_sessions:
                    error = subprocess.CalledProcessError(1, command, stderr="duplicate session: work")
                    raise SystemExit("create ssh:dev:work failed: duplicate session: work") from error
                remote_sessions.add(target.session)
            elif operation == "rename":
                remote_sessions.remove(target.session)
                remote_sessions.add(tokens[-1])
            return ""

        with patch.object(cockpit, "current_target", return_value=target), \
             patch.object(cockpit, "startup_deadline", return_value=None), \
             patch.object(cockpit, "switch", side_effect=lambda target, command, **kwargs: launched.append((target, command() if callable(command) else command, kwargs["launch_id"]))), \
             patch.object(cockpit, "rename_target"), \
             patch.object(operations.sessions, "load_tmux_config_overlay", return_value=False), \
             patch.object(operations.sessions, "_run", side_effect=run):
            result = operations.perform(operations.Effect("switch", target, automatic=True))
            self.assertFalse(result.error)
            operations.rename(target, renamed.session)

        # Execute delayed remote commands against the renamed remote state.
        def attach(command):
            tokens = shlex.split(shlex.split(command)[-1])
            if "new-session" in tokens:
                remote_sessions.add(tokens[tokens.index("-s") + 1])
            self.assertIn("attach-session", tokens)
            session = tokens[tokens.index("-t") + 1].removeprefix("=")
            return session if session in remote_sessions else None

        self.assertIsNone(attach(launched[0][1]))
        self.assertEqual(len(launched), 2)
        self.assertEqual(launched[1][0], renamed)
        self.assertEqual(attach(launched[1][1]), renamed.session)
        self.assertNotEqual(launched[0][2], launched[1][2])
        self.assertEqual(launched[1][2], operations.launch_identity())
        self.assertEqual(remote_sessions, {"other"})
        self.assertEqual(ensured, [target])
        self.assertIn(target.format(), config.load_operations()["invalidated"])

    def test_rename_preparation_failure_retries_even_when_renamed_session_is_present(self):
        from letee.discovery import SessionSnapshot, SourceSnapshot
        from letee.sidebar import SessionReconciler
        from unittest.mock import Mock

        renamed = Target("local", "other")
        calls = []
        failure = OSError("preparation failed")

        def prepare(target, **kwargs):
            self.assertEqual(target, renamed)
            self.assertIn(("set-option", "-t", "letee", cockpit.STARTUP_LAUNCH_OPTION,
                           operations.launch_identity()), calls)
            raise failure

        with patch.object(cockpit, "current_target", return_value=self.work), \
             patch.object(cockpit, "right_pane", return_value="%2"), \
             patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)), \
             patch.object(cockpit, "rename_target"), \
             patch.object(operations.sessions, "rename"), \
             patch.object(operations.sessions, "attach_command", side_effect=prepare):
            result = operations.perform(operations.Effect("rename", self.work, message="other"))

        self.assertEqual(result.error, "preparation failed")
        self.assertTrue(result.partial_success)
        self.assertEqual(config.load_sessions(), [renamed])
        self.assertIn(self.work.format(), config.load_operations()["invalidated"])
        marker = f"{renamed.format()}:{result.launch_id}"
        self.assertTrue(any(cockpit.ATTACHMENT_FAILURE_OPTION in str(call) and marker in str(call)
                            for call in calls))
        with patch.object(cockpit, "_option", return_value=marker), \
             patch.object(cockpit, "status_snapshot", return_value=cockpit.StatusSnapshot(renamed, None, None, True)), \
             patch.object(cockpit, "startup_deadline", return_value=None):
            launch, status, active, deadline = operations.sample_target()
        submit = Mock(return_value=True)
        data = SessionSnapshot(SourceSnapshot(True, (renamed,), frozenset()), {})
        reconciler = SessionReconciler()
        for now in (100, 200):
            reconciler.reconcile((renamed,), active or status.current_target, data, submit,
                                 now, deadline, launch, attachment_failed=status.attachment_failed)
        self.assertEqual(submit.call_args.args[0].kind, "switch")
        with patch.object(cockpit, "current_target", return_value=renamed), \
             patch.object(cockpit, "startup_deadline", return_value=None), \
             patch.object(operations.sessions, "ensure_created"), \
             patch.object(operations.sessions, "attach_command", return_value="attach"), \
             patch.object(cockpit, "switch") as switch:
            retry = operations.perform(submit.call_args.args[0])
        self.assertFalse(retry.error)
        switch.assert_called_once()

    def test_rename_probe_failure_prevents_mutation(self):
        state = config.load_operations()
        with patch.object(cockpit, "current_target", side_effect=OSError("probe failed")), \
             patch.object(operations.sessions, "rename") as rename, \
             patch.object(cockpit, "rename_target") as rename_target:
            result = operations.perform(operations.Effect("rename", self.work, message="other"))

        self.assertEqual(result.error, "probe failed")
        self.assertFalse(result.partial_success)
        self.assertEqual(config.load_sessions(), [self.work])
        self.assertEqual(config.load_operations(), state)
        rename.assert_not_called()
        rename_target.assert_not_called()

    def test_rename_marker_failure_still_invalidates_old_active_target(self):
        renamed = Target("local", "other")
        with patch.object(cockpit, "current_target", side_effect=[self.work, OSError("probe failed")]) as probe, \
             patch.object(operations.sessions, "rename") as rename, \
             patch.object(cockpit, "rename_target", side_effect=OSError("marker cleanup failed")):
            result = operations.perform(operations.Effect("rename", self.work, message="other"))

        self.assertEqual(result.error, "marker cleanup failed")
        self.assertTrue(result.partial_success)
        rename.assert_called_once_with(self.work, "other")
        probe.assert_called_once_with()
        self.assertEqual(config.load_sessions(), [renamed])
        self.assertIn(self.work.format(), config.load_operations()["invalidated"])
        with patch.object(cockpit, "current_target", return_value=self.work), \
             patch.object(operations.sessions, "ensure_created") as ensure:
            restore = operations.perform(operations.Effect("ensure_created", self.work, automatic=True))
        self.assertTrue(restore.stale_navigation)
        ensure.assert_not_called()

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
