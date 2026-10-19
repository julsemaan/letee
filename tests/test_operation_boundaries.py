import os
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import Future
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import Mock, patch

from letee import config, cockpit, operations, sidebar
from letee.discovery import SessionSnapshot, SourceSnapshot
from letee.names import PaneTarget, Target


class OperationBoundariesTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(os.environ, LETEE_CONFIG_DIR=directory.name)
        environment.start()
        self.addCleanup(environment.stop)
        self.work = Target("local", "work")
        config.save_sessions([self.work])

    def test_cli_preserves_subprocess_error_reporting(self):
        from letee.__main__ import run_cli
        output = StringIO()
        failure = subprocess.CalledProcessError(1, ["tmux"], stderr="cannot launch")
        with patch.object(cockpit, "require_cockpit"), patch.object(cockpit, "switch", side_effect=failure), redirect_stderr(output):
            self.assertEqual(run_cli(["switch", self.work.format()]), 1)
        self.assertIn("cannot launch", output.getvalue())

    def test_restore_then_destructive_intent_serializes_both_domain_transactions(self):
        for kind in ("kill", "rename", "save_favorites"):
            with self.subTest(kind=kind):
                config.save_sessions([self.work])
                with config.sessions_lock():
                    config.save_operations({"launch": "initial", "invalidated": []})
                entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
                events, results = [], []

                def ensure(target):
                    entered.set()
                    self.assertTrue(release.wait(2))
                    events.append("restore")

                def mutate():
                    attempted.set()
                    results.append(operations.perform(operations.Effect(kind, self.work, message="renamed")))
                    events.append("mutation")

                with (
                    patch.object(cockpit, "current_target", return_value=None),
                    patch.object(cockpit, "reset_to_help"),
                    patch.object(cockpit, "rename_target"),
                    patch.object(operations.sessions, "kill"),
                    patch.object(operations.sessions, "rename"),
                    patch.object(operations.sessions, "ensure_created", side_effect=ensure),
                ):
                    restore = threading.Thread(target=lambda: results.append(operations.perform(operations.Effect("ensure_created", self.work, automatic=True))))
                    mutation = threading.Thread(target=mutate)
                    restore.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        mutation.start()
                        self.assertTrue(attempted.wait(1))
                        with config.sessions_lock(blocking=False) as acquired:
                            self.assertFalse(acquired)
                        self.assertEqual(events, [])
                    finally:
                        release.set()
                        restore.join(2)
                        if mutation.ident is not None:
                            mutation.join(2)
                    self.assertFalse(restore.is_alive())
                    self.assertFalse(mutation.is_alive())
                    self.assertEqual(events, ["restore", "mutation"])
                    self.assertTrue(all(not result.error for result in results))
                    self.assertNotIn(self.work, config.load_sessions())
                    # Even an active marker left behind by failed cleanup cannot restore it.
                    with patch.object(cockpit, "current_target", return_value=self.work):
                        stale = operations.perform(operations.Effect("switch", self.work, automatic=True))
                        self.assertTrue(stale.stale_navigation)

    def test_concurrent_remove_add_reorder_apply_intent_to_latest_tracking(self):
        second, third, added = (Target("local", name) for name in ("second", "third", "added"))
        config.save_sessions([self.work, second, third])
        barrier = threading.Barrier(4)
        results = []
        effects = [operations.Effect("save_favorites", self.work),
                   operations.Effect("add_switch", added),
                   operations.Effect("save_favorites", favorite_move=(second, third))]

        def perform(effect):
            barrier.wait()
            results.append(operations.perform(effect, (self.work, second, third)))

        with patch.object(cockpit, "current_target", return_value=None), patch.object(cockpit, "switch"):
            workers = [threading.Thread(target=perform, args=(effect,)) for effect in effects]
            for worker in workers:
                worker.start()
            barrier.wait()
            for worker in workers:
                worker.join(2)
                self.assertFalse(worker.is_alive())
        self.assertTrue(all(not result.error for result in results))
        self.assertEqual(config.load_sessions(), [third, second, added])

    def test_all_navigation_paths_hold_lock_through_preparation_and_respawn(self):
        from letee.__main__ import main
        pane = PaneTarget(self.work, "@1", "%2", "/tmp/letee.inner")
        paths = [lambda: main(["switch", self.work.format()]),
                 lambda: main(["switch-session", "1"]),
                 lambda: main(["create", "local", self.work.session]),
                 lambda: operations.perform(operations.Effect("switch", self.work)),
                 lambda: operations.perform(operations.Effect("switch_pane", pane)),
                 lambda: operations.perform(operations.Effect("create", self.work))]
        for index, launch in enumerate(paths):
            with self.subTest(path=index):
                entered, release, sampling, sampled = (threading.Event() for _ in range(4))
                marker, results, errors = {}, [], []

                def switch(target, prepare, *args, **kwargs):
                    marker["target"] = target
                    prepare()
                    with config.sessions_lock(blocking=False) as acquired:
                        self.assertFalse(acquired)
                    entered.set()
                    self.assertTrue(release.wait(2))
                    marker["respawned"] = True

                def status():
                    sampled.set()
                    self.assertTrue(marker.get("respawned"))
                    return cockpit.StatusSnapshot(marker["target"], None, None, True)

                def run_launch():
                    try:
                        launch()
                    except BaseException as error:
                        errors.append(error)

                def sample():
                    sampling.set()
                    results.append(operations.sample_target(synchronize=True))

                with (
                    patch.object(cockpit, "require_cockpit"),
                    patch.object(cockpit, "switch", side_effect=switch),
                    patch.object(cockpit, "current_target", side_effect=lambda **kwargs: marker.get("target")),
                    patch.object(cockpit, "status_snapshot", side_effect=status),
                    patch.object(cockpit, "startup_deadline", return_value=None),
                    patch.object(operations.sessions, "create"),
                    patch.object(operations.sessions, "attach_command", return_value="attach"),
                    patch.object(operations.sessions, "pane_attach_command", return_value="pane attach"),
                ):
                    navigation = threading.Thread(target=run_launch)
                    reader = threading.Thread(target=sample)
                    navigation.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        reader.start()
                        self.assertTrue(sampling.wait(1))
                        self.assertFalse(sampled.is_set())
                        self.assertIsNone(operations.tracking_snapshot(None))
                    finally:
                        release.set()
                        navigation.join(2)
                        if reader.ident is not None:
                            reader.join(2)
                    self.assertFalse(navigation.is_alive())
                    self.assertFalse(reader.is_alive())
                    self.assertEqual(errors, [])
                    self.assertEqual(results[0][0], operations.launch_identity())
                    self.assertEqual(results[0][2], self.work)

    def test_first_worker_sample_repairs_rejected_marker_without_tracking_change(self):
        marker = {cockpit.CURRENT_TARGET_OPTION: self.work.format()}
        data = SessionSnapshot(SourceSnapshot(True, (), frozenset()), {})

        def clear_marker(*args):
            self.assertEqual(args, ("set-option", "-u", "-t", cockpit.tmux.SESSION, cockpit.CURRENT_TARGET_OPTION))
            marker.clear()

        def read_status():
            target = self.work if marker else None
            return cockpit.StatusSnapshot(target, None, None, True)

        with (
            patch.object(cockpit, "_option", side_effect=lambda name: marker.get(name, "")),
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "out", return_value="tmux new-session -A -s work"),
            patch.object(cockpit.tmux, "tmux", side_effect=clear_marker),
            patch.object(cockpit, "startup_deadline", return_value=None),
            patch.object(cockpit, "status_snapshot", side_effect=read_status),
            patch.object(operations, "sample_target", wraps=operations.sample_target) as sample,
        ):
            initial_target = cockpit.current_target()
            self.assertIsNone(initial_target)
            self.assertTrue(marker)
            revision = config.sessions_revision()
            status = sidebar.AsyncStatusPoller(Mock(snapshot=data), initial_target)
            try:
                status.tick(0)
                first = status._future.result(timeout=2)
                status.tick(0)
                sample.assert_called_once_with(synchronize=True)
                self.assertTrue(first.synchronized)
                self.assertFalse(marker)
                self.assertIsNone(status.current_target)
                self.assertFalse(status._synchronize_pending)
                status.tick(sidebar.STATUS_POLL_INTERVAL)
                subsequent = status._future.result(timeout=2)
                status.tick(sidebar.STATUS_POLL_INTERVAL)
                self.assertFalse(subsequent.synchronized)
                self.assertIsNone(status.current_target)
                self.assertEqual(config.sessions_revision(), revision)
            finally:
                status.close()

    def test_same_target_old_status_failure_and_cleanup_cannot_change_relaunch(self):
        data = SessionSnapshot(SourceSnapshot(True, (), frozenset()), {})
        with patch.object(cockpit, "current_target", return_value=self.work), patch.object(cockpit, "startup_deadline", return_value=None), patch.object(cockpit, "switch") as switch:
            switch.side_effect = SystemExit("old launch failed")
            failed = operations.perform(operations.Effect("switch", self.work))
            status = sidebar.AsyncStatusPoller(Mock(snapshot=data), self.work)
            try:
                with patch.object(cockpit, "status_snapshot", return_value=cockpit.StatusSnapshot(self.work, None, "old agent", True)):
                    old_sample = status._sample((), 0)
                switch.side_effect = None
                newer = operations.perform(operations.Effect("switch", self.work))
                self.assertNotEqual(failed.launch_id, newer.launch_id)
                status.current_agent = "new agent"
                status.startup_deadline = 99
                status.observe_effect(failed)
                state = sidebar.SidebarState()
                self.assertFalse(sidebar._apply_effect(failed, state, Mock(), 5))
                self.assertEqual(state.status, "")
                future = Future()
                future.set_result(old_sample)
                status._future = future
                status._next_poll = 100
                status._sample_revision = config.sessions_revision()
                status.tick(0)
                self.assertEqual(status.current_agent, "new agent")
                self.assertEqual(status.startup_deadline, 99)
                with patch.object(cockpit, "clear_startup") as clear:
                    operations.clear_startup(self.work, 99, failed.launch_id)
                    clear.assert_not_called()
                    operations.clear_startup(self.work, 99, newer.launch_id)
                    clear.assert_called_once_with(self.work, 99, newer.launch_id)
            finally:
                status.close()

    def test_successful_destruction_invalidates_restore_even_if_cleanup_and_save_fail(self):
        for kind in ("kill", "rename"):
            with self.subTest(kind=kind):
                config.save_sessions([self.work])
                with config.sessions_lock():
                    config.save_operations({"launch": "initial", "invalidated": []})
                with (
                    patch.object(cockpit, "current_target", return_value=self.work),
                    patch.object(cockpit, "set_expected_right_pane_death"),
                    patch.object(cockpit, "resolve_expected_right_pane_death", side_effect=OSError("cleanup failed")) as resolve,
                    patch.object(cockpit, "rename_target", side_effect=OSError("cleanup failed")),
                    patch.object(operations.sessions, "kill"),
                    patch.object(operations.sessions, "rename"),
                    patch.object(config, "save_sessions", side_effect=OSError("save failed")),
                ):
                    result = operations.perform(operations.Effect(kind, self.work, message="renamed"))
                    self.assertTrue(result.error)
                    self.assertTrue(result.partial_success)
                    if kind == "kill":
                        resolve.assert_called_once_with(self.work, True)
                    # Tracking and the active marker are deliberately still stale.
                    self.assertEqual(config.load_sessions(), [self.work])
                    retry = operations.perform(operations.Effect("switch", self.work, automatic=True))
                    self.assertTrue(retry.stale_navigation)

    def test_automatic_preparation_timeout_preserves_overlay_and_identity(self):
        identity = operations.launch_identity()
        with patch.object(cockpit, "current_target", return_value=self.work), patch.object(cockpit, "startup_deadline", return_value=None), patch.object(cockpit, "switch") as switch, patch.object(operations.sessions, "attach_command", side_effect=subprocess.TimeoutExpired("prepare", 2)):
            result = operations.perform(operations.Effect("switch", self.work, automatic=True))
        self.assertIn("timed out", result.error)
        self.assertEqual(result.launch_id, identity)
        switch.assert_not_called()

    def test_ui_only_status_does_not_wait_for_transaction_lock(self):
        results = []
        with config.sessions_lock():
            worker = threading.Thread(target=lambda: results.append(operations.perform(operations.Effect("status", message="ready"))))
            worker.start()
            worker.join(1)
            self.assertFalse(worker.is_alive())
        self.assertEqual(results[0].error, "")
