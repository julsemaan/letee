import curses
import inspect
import json
import os
import re
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import ANY, call, patch

import letee.sidebar as sidebar
from letee import config, operations
from letee.discovery import AgentEntry, SessionSnapshot, SourceSnapshot
from letee.names import PaneTarget, Target

_overlay_patch = None
_publisher_patch = None


class OperationTestCase(unittest.TestCase):
    def run(self, result=None):
        # Operation identities and tracking are server-scoped persistent state.
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, LETEE_CONFIG_DIR=directory):
            return super().run(result)


def setUpModule():
    # Exact-command assertions assume the overlay is off; overlay tests enable it locally.
    global _overlay_patch, _publisher_patch
    _overlay_patch = patch.object(sidebar.sessions, "load_tmux_config_overlay", return_value=False)
    _overlay_patch.start()
    # Sidebar loop tests must never publish to a host tmux server.
    _publisher_patch = patch.object(sidebar.runtime, "WorkspacePublisher")
    _publisher_patch.start()


def tearDownModule():
    _publisher_patch.stop()
    _overlay_patch.stop()


def source(kind, sessions=(), bells=(), host=None, available=True, error=None):
    targets = tuple(Target(kind, session, host) for session in sessions)
    bell_targets = frozenset(Target(kind, session, host) for session in bells)
    return SourceSnapshot(available, targets, bell_targets, error)


def snapshot(local=(), remotes=None, local_bells=(), local_available=True, local_error=None):
    return SessionSnapshot(
        source("local", local, local_bells, available=local_available, error=local_error),
        remotes or {},
    )


def _assert_deferred_switch(testcase, switch, target, *extra):
    switch.assert_called_once()
    testcase.assertEqual(switch.call_args.args[0], target)
    testcase.assertTrue(callable(switch.call_args.args[1]))
    testcase.assertTrue(switch.call_args.kwargs["launch_id"])
    testcase.assertEqual(switch.call_args.args[2:], extra)


class RevisionTargetSynchronizationTest(OperationTestCase):
    def test_invalidated_cached_target_cannot_starve_reachable_favorite(self):
        old = Target("ssh", "old", "offline")
        favorite = Target("local", "work")
        config.save_sessions([favorite])
        with config.sessions_lock():
            config.save_operations({"launch": "removed", "invalidated": [old.format()]})
        data = snapshot(remotes={"offline": source("ssh", host="offline", available=False)})
        discovery = unittest.mock.Mock(snapshot=data)
        poller = sidebar.AsyncStatusPoller(discovery, old)
        self.addCleanup(poller.close)
        poller._synchronize_pending = False
        reconciler = sidebar.SessionReconciler()
        submit = unittest.mock.Mock(return_value=True)
        for marker in (old, None):
            for synchronize in (True, False, False):
                with self.subTest(marker=marker, synchronize=synchronize), \
                     patch.object(sidebar.cockpit, "current_target", return_value=old), \
                     patch.object(sidebar.cockpit, "status_snapshot", return_value=sidebar.cockpit.StatusSnapshot(marker, None, None, True)), \
                     patch.object(sidebar.cockpit, "startup_deadline", return_value=None), \
                     patch.object(sidebar.cockpit, "attachment_failed", return_value=False):
                    result = poller._sample((), 0, synchronize=synchronize)
                self.assertIsNone(result.current_target)
                discovery.tick.assert_called_with(None)
                reconciler.reconcile((favorite,), result.current_target, result.snapshot, submit, 0)
        self.assertEqual(submit.call_args.args[0].target, favorite)
        self.assertEqual(submit.call_args.args[0].kind, "ensure_created")

    def test_invalid_snapshot_does_not_restore_rejected_cached_target(self):
        target = Target("local", "removed")
        with config.sessions_lock():
            config.save_operations({"launch": "removed", "invalidated": [target.format()]})
        poller = sidebar.AsyncStatusPoller(unittest.mock.Mock(snapshot=snapshot()), target)
        self.addCleanup(poller.close)
        with patch.object(sidebar.cockpit, "current_target", return_value=target), \
             patch.object(sidebar.cockpit, "status_snapshot", return_value=None):
            result = poller._sample((), 0, synchronize=True)
        self.assertIsNone(result.current_target)
        self.assertFalse(result.sampled)

    def test_ordinary_sample_keeps_valid_cached_target_when_marker_is_missing(self):
        target = Target("local", "work")
        poller = sidebar.AsyncStatusPoller(unittest.mock.Mock(snapshot=snapshot()), target)
        self.addCleanup(poller.close)
        poller._synchronize_pending = False
        with patch.object(sidebar.cockpit, "status_snapshot", return_value=sidebar.cockpit.StatusSnapshot(None, None, None, True)), \
             patch.object(sidebar.cockpit, "startup_deadline", return_value=None), \
             patch.object(sidebar.cockpit, "attachment_failed", return_value=False):
            self.assertEqual(poller._sample((), 0).current_target, target)

    def test_blocked_query_leaves_tick_available_while_holding_sessions_lock(self):
        old = Target("local", "old")
        entered, release = threading.Event(), threading.Event()
        data = snapshot(local=("old",))

        def query(**_):
            entered.set()
            release.wait(2)
            return None

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"LETEE_CONFIG_DIR": directory}):
            config.save_sessions([old])
            status = sidebar.AsyncStatusPoller(unittest.mock.Mock(snapshot=data), old)
            try:
                with (
                    patch.object(sidebar.cockpit, "current_target", side_effect=query),
                    patch.object(sidebar.cockpit, "status_snapshot", return_value=sidebar.cockpit.StatusSnapshot(old, None, None, True)),
                ):
                    with config.sessions_lock():
                        status.request_target_sync()
                    status.tick(0)
                    self.assertTrue(entered.wait(1))
                    self.assertFalse(status.tick(0))
                    # The synchronized sample holds sessions_lock while it validates.
                    with config.sessions_lock(blocking=False) as acquired:
                        self.assertFalse(acquired)
                    self.assertEqual(status.current_target, old)
                    release.set()
                    status._future.result(timeout=1)
                    status.tick(0)
                    self.assertIsNone(status.current_target)
                    self.assertFalse(status.refresh_pending)
            finally:
                release.set()
                status.close()

    def test_synchronized_sample_waits_out_switch_marker_window(self):
        work, stale = Target("local", "work"), Target("local", "stale")
        new_command = "tmux -L letee.inner attach-session -t work"
        old_command = "tmux -L letee.inner attach-session -t stale"
        in_window, release, respawned = threading.Event(), threading.Event(), threading.Event()
        pane_queried = threading.Event()
        options, events, errors, results = {}, [], [], []

        def tmux_call(*args, **kwargs):
            if args[0] == "respawn-pane":
                events.append("respawn")
                in_window.set()
                release.wait(2)
                respawned.set()
            elif args[0] == "if-shell":
                options[sidebar.cockpit.CURRENT_TARGET_OPTION] = work.format()
                events.append("publish_marker")
            elif args[0] == "set-option" and args[1] == "-u":
                if args[4] == sidebar.cockpit.CURRENT_TARGET_OPTION:
                    events.append("clear_marker")
                options.pop(args[4], None)
            elif args[0] == "set-option":
                options[args[3]] = args[4]
            return None

        def out(*args, **kwargs):
            if args[0] == "show-options":
                return options.get(args[-1], "")
            if args[3] == "%2":
                pane_queried.set()
                kind = "pane_query_new" if respawned.is_set() else "pane_query_old"
                events.append(kind)
                return new_command if respawned.is_set() else old_command
            return f"%3\t\t{options.get(sidebar.cockpit.CURRENT_TARGET_OPTION, '')}\t\t\t\t\t."

        def run_switch():
            try:
                with config.sessions_lock():
                    sidebar.cockpit.switch(work, new_command)
            except BaseException as error:
                errors.append(error)

        def run_sample():
            try:
                results.append(status._sample((), 0, synchronize=True))
            except BaseException as error:
                errors.append(error)

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"LETEE_CONFIG_DIR": directory}):
            status = sidebar.AsyncStatusPoller(unittest.mock.Mock(snapshot=snapshot(local=("work",))), stale)
            switcher = threading.Thread(target=run_switch)
            sampler = threading.Thread(target=run_sample)
            try:
                with (
                    patch.object(sidebar.cockpit, "right_pane", return_value="%2"),
                    patch.object(sidebar.cockpit, "startup_timeout", return_value=10),
                    patch.object(sidebar.cockpit.tmux, "tmux", side_effect=tmux_call),
                    patch.object(sidebar.cockpit.tmux, "out", side_effect=out),
                ):
                    switcher.start()
                    self.assertTrue(in_window.wait(1))
                    sampler.start()
                    # Broken code validates (and clears) during the window; fixed code blocks on the lock.
                    pane_queried.wait(1)
                    release.set()
                    switcher.join(5)
                    sampler.join(5)
                    self.assertEqual(errors, [])
                    self.assertNotIn("clear_marker", events)
                    self.assertNotIn("pane_query_old", events)
                    self.assertLess(events.index("respawn"), events.index("pane_query_new"))
                    self.assertEqual(options.get(sidebar.cockpit.CURRENT_TARGET_OPTION), work.format())
                    self.assertEqual(sidebar.cockpit.current_target(), work)
                    self.assertEqual(len(results), 1)
                    self.assertEqual(results[0].current_target, work)
                    self.assertTrue(results[0].synchronized)
            finally:
                release.set()
                status.close()

    def test_sidebar_accepts_keyboard_input_during_revision_query(self):
        first, second = Target("local", "first"), Target("local", "second")
        entered, release = threading.Event(), threading.Event()
        screen = FakeScreen(size=(16, 60))
        reads = 0
        navigated = False

        def query(**_):
            if reads:
                entered.set()
                release.wait(2)
            return first

        def read_key():
            nonlocal reads, navigated
            reads += 1
            self.assertLess(reads, 20)
            if reads == 1:
                config.save_sessions([second, first])
            elif navigated:
                self.assertFalse(release.is_set())
                release.set()
                return STOP
            elif status._synchronize_pending and status._future is not None:
                self.assertTrue(entered.wait(1))
                # The synchronized sample holds sessions_lock while it validates.
                with config.sessions_lock(blocking=False) as acquired:
                    self.assertFalse(acquired)
                self.assertEqual(draw.call_args.args[2], 1)
                navigated = True
                return curses.KEY_DOWN
            return -1

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"LETEE_CONFIG_DIR": directory}):
            config.save_sessions([first, second])
            discovery = unittest.mock.Mock(snapshot=snapshot(local=("first", "second")))
            status = sidebar.AsyncStatusPoller(discovery, first)
            status._next_poll = float("inf")
            actions = unittest.mock.Mock(busy=False, blocks_favorite_changes=False)
            actions.poll.return_value = None
            try:
                with (
                    patch.object(sidebar, "AsyncStatusPoller", return_value=status),
                    patch.object(sidebar, "EffectRunner", return_value=actions),
                    patch.object(sidebar, "load_hosts", return_value=[]),
                    patch.object(sidebar.cockpit, "current_target", side_effect=query),
                    patch.object(sidebar.cockpit, "status_snapshot", return_value=sidebar.cockpit.StatusSnapshot(first, None, None, True)),
                    patch.object(sidebar.cockpit, "startup_deadline", return_value=None),
                    patch.object(sidebar, "_init_colors"),
                    patch.object(sidebar, "_mouse_mask"),
                    patch.object(sidebar.curses, "curs_set"),
                    patch.object(sidebar, "_draw", return_value=(2, None)) as draw,
                    patch.object(screen, "getch", side_effect=read_key),
                ):
                    sidebar.run(screen)
                self.assertTrue(navigated)
                self.assertEqual(draw.call_args.args[2], 0)
            finally:
                release.set()
                status.close()

    def test_query_failures_preserve_state_and_retry(self):
        old = Target("local", "old")
        for error in (SystemExit("timeout"), OSError("unavailable"), subprocess.TimeoutExpired("tmux", 1)):
            with self.subTest(error=error):
                status = sidebar.AsyncStatusPoller(unittest.mock.Mock(snapshot=snapshot()), old)
                try:
                    with patch.object(sidebar.cockpit, "current_target", side_effect=error):
                        status.request_target_sync()
                        status.tick(0)
                        status._future.result(timeout=1)
                        status.tick(0)
                    self.assertEqual(status.current_target, old)
                    self.assertTrue(status.refresh_pending)
                finally:
                    status.close()

    def test_results_superseded_by_revision_or_navigation_are_discarded(self):
        old, stale, newer = (Target("local", name) for name in ("old", "stale", "newer"))
        for change in ("revision", "navigation"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"LETEE_CONFIG_DIR": directory}):
                config.save_sessions([old])
                status = sidebar.AsyncStatusPoller(unittest.mock.Mock(snapshot=snapshot()), old)
                try:
                    with (
                        patch.object(sidebar.cockpit, "current_target", return_value=stale),
                        patch.object(sidebar.cockpit, "status_snapshot", return_value=sidebar.cockpit.StatusSnapshot(stale, None, None, True)),
                        patch.object(sidebar.cockpit, "startup_deadline", return_value=None),
                    ):
                        status.request_target_sync()
                        status.tick(0)
                        status._future.result(timeout=1)
                    if change == "revision":
                        config.save_sessions([newer])
                    else:
                        status.observe_effect(sidebar.EffectResult(sidebar.Effect("switch", newer), (newer,)))
                    status.tick(0)
                    self.assertEqual(status.current_target, old if change == "revision" else newer)
                    self.assertTrue(status.refresh_pending)
                finally:
                    status.close()


class SessionReconcilerTest(OperationTestCase):
    @patch.object(sidebar, "load_timing", return_value=config.Timing())
    def setUp(self, _load_timing):
        self.reconciler = sidebar.SessionReconciler()
        self.submit = unittest.mock.Mock(return_value=True)

    def test_failed_attachment_retries_existing_session_with_backoff(self):
        target = Target("ssh", "work", "host")
        data = snapshot(remotes={"host": source("ssh", ("work",), host="host")})
        effect = sidebar.Effect("switch", target, automatic=True)
        self.reconciler.observe(sidebar.EffectResult(effect, (), launch_id="launch", completed_at=0))
        self.reconciler.reconcile((target,), target, data, self.submit, 1,
                                  shared_launch_id="launch", attachment_failed=True)
        self.submit.assert_not_called()
        self.reconciler.reconcile((target,), target, data, self.submit, 100,
                                  shared_launch_id="launch", attachment_failed=True)
        self.assertEqual(self.submit.call_args.args[0].kind, "switch")
        self.assertEqual(self.submit.call_args.args[0].target, target)

    @patch.object(sidebar.cockpit, "startup_deadline", return_value=None)
    def test_reconciliation_does_not_read_startup_marker(self, read_marker):
        target = Target("local", "work")
        self.reconciler.reconcile((target,), target, snapshot(local=("work",)), self.submit, 0)
        read_marker.assert_not_called()

    @patch.object(sidebar.cockpit, "startup_timeout", return_value=10)
    def test_startup_cleanup_is_nonblocking_and_deduplicated(self, _timeout):
        target = Target("local", "work")
        for present in (True, False):
            with self.subTest(present=present):
                reconciler = sidebar.SessionReconciler()
                data = snapshot(local=("work",) if present else ())
                entered, release, returned = (threading.Event() for _ in range(3))

                def clear(*args):
                    entered.set()
                    release.wait(2)
                    return True

                def reconcile():
                    reconciler.reconcile((target,), target, data, self.submit, 15, 10)
                    returned.set()

                with patch.object(sidebar.operations, "clear_startup", side_effect=clear) as cleanup:
                    ui = threading.Thread(target=reconcile)
                    ui.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        self.assertTrue(returned.wait(1), "cleanup blocked reconciliation")
                        reconcile()
                        cleanup.assert_called_once_with(target, 10, None)
                    finally:
                        release.set()
                        ui.join(2)
                        for worker, _ in reconciler._startup_clears.values():
                            worker.join(2)
                    # A completed clear must not be repeated for a cached deadline either.
                    reconcile()
                    cleanup.assert_called_once_with(target, 10, None)

    @patch.object(sidebar.cockpit, "startup_timeout", return_value=10)
    def test_failed_startup_cleanup_retries_same_marker(self, _timeout):
        target = Target("local", "work")
        data = snapshot(local=("work",))
        with patch.object(sidebar.cockpit, "clear_startup", side_effect=[OSError("tmux failed"), None]) as cleanup:
            for expected_calls in (1, 2, 2):
                self.reconciler.reconcile((target,), target, data, self.submit, 15, 10)
                for worker, _ in self.reconciler._startup_clears.values():
                    worker.join(2)
                self.assertEqual(cleanup.call_count, expected_calls)
            cleanup.assert_called_with(target, 10, None)
        self.submit.assert_not_called()

    @patch.object(sidebar.cockpit, "startup_timeout", return_value=10)
    def test_delayed_startup_cleanup_keeps_exact_marker_guard(self, _timeout):
        target = Target("local", "work")
        entered, release = threading.Event(), threading.Event()
        guards = []

        def tmux_call(*args):
            entered.set()
            release.wait(2)
            guards.append(args)

        with patch.object(sidebar.cockpit.tmux, "tmux", side_effect=tmux_call):
            try:
                self.reconciler.reconcile((target,), target, snapshot(), self.submit, 15, 10)
                self.assertTrue(entered.wait(1))
                # A newer startup may be sampled before the old cleanup finishes.
                self.reconciler.reconcile((target,), target, snapshot(), self.submit, 15, 25)
            finally:
                release.set()
                for worker, _ in self.reconciler._startup_clears.values():
                    worker.join(2)
        self.assertEqual(guards, [(
            "if-shell", "-F",
            sidebar.cockpit._option_equals(sidebar.cockpit.STARTUP_OPTION, "local:work\t10"),
            f"set-option -u -t {sidebar.cockpit.tmux.SESSION} {sidebar.cockpit.STARTUP_OPTION}",
        )])

    def test_absent_active_target_switches_even_when_untracked(self):
        target = Target("local", "work")
        self.reconciler.reconcile((), target, snapshot(), self.submit, 0)
        self.submit.assert_called_once_with(sidebar.Effect("switch", target, automatic=True))

    def test_absent_inactive_favorite_is_created_without_navigation(self):
        target = Target("ssh", "work", "dev")
        data = snapshot(remotes={"dev": source("ssh", host="dev")})
        self.reconciler.reconcile((target,), None, data, self.submit, 0)
        self.submit.assert_called_once_with(sidebar.Effect("ensure_created", target, automatic=True))

    def test_existing_unknown_and_unreachable_sources_are_not_created(self):
        targets = (
            Target("local", "present"), Target("ssh", "unknown", "unknown"),
            Target("ssh", "connecting", "connecting"), Target("ssh", "down", "down"),
        )
        data = snapshot(local=("present",), remotes={
            "connecting": None, "down": source("ssh", host="down", available=False),
        })
        self.reconciler.reconcile(targets, None, data, self.submit, 0)
        self.reconciler.reconcile((Target("local", "absent"),), None,
                                  snapshot(local_available=False), self.submit, 0)
        self.submit.assert_not_called()

    def test_in_flight_target_is_not_submitted_twice(self):
        target = Target("local", "work")
        self.reconciler.reconcile((target,), None, snapshot(), self.submit, 0)
        self.reconciler.reconcile((target,), None, snapshot(), self.submit, 100)
        self.submit.assert_called_once()

    def test_rejected_submission_is_not_marked_in_flight(self):
        target = Target("local", "work")
        self.submit.return_value = False
        self.reconciler.reconcile((target,), None, snapshot(), self.submit, 0)
        self.submit.return_value = True
        self.reconciler.reconcile((target,), None, snapshot(), self.submit, 0)
        self.assertEqual(self.submit.call_count, 2)

    @patch.object(sidebar, "load_timing", return_value=config.Timing(
        retry_initial_delay=2, retry_max_delay=5,
    ))
    def test_failure_backoff_uses_completion_time_and_caps_delay(self, _load_timing):
        target = Target("local", "work")
        self.reconciler.reconcile((target,), None, snapshot(), self.submit, 0)
        for completed, delay in ((10, 2), (20, 4), (30, 5), (40, 5)):
            effect = self.submit.call_args.args[0]
            self.assertEqual(effect, sidebar.Effect("ensure_created", target, automatic=True))
            self.reconciler.observe(sidebar.EffectResult(
                effect, (target,), error="timeout", completed_at=completed,
            ))
            self.submit.reset_mock()
            self.reconciler.reconcile((target,), None, snapshot(), self.submit, completed + delay - 0.01)
            self.submit.assert_not_called()
            self.reconciler.reconcile((target,), None, snapshot(), self.submit, completed + delay)
            self.submit.assert_called_once()

    @patch.object(sidebar, "load_timing", return_value=config.Timing(
        ssh_auth_timeout=10,
    ))
    def test_switch_waits_for_discovery_confirmation_or_startup_timeout(self, _load_timing):
        target = Target("ssh", "work", "dev")
        def empty_sample():
            return snapshot(remotes={"dev": source("ssh", host="dev")})

        self.reconciler.reconcile((target,), target, empty_sample(), self.submit, 0)
        self.reconciler.observe(sidebar.EffectResult(
            self.submit.call_args.args[0], (target,), completed_at=5,
        ))
        self.submit.reset_mock()
        for now in (6, 8, 14.99):
            self.reconciler.reconcile((target,), target, empty_sample(), self.submit, now)
        self.submit.assert_not_called()

        confirmed = snapshot(remotes={"dev": source("ssh", ("work",), host="dev")})
        self.reconciler.reconcile((target,), target, confirmed, self.submit, 14.99)
        self.reconciler.reconcile((target,), target, empty_sample(), self.submit, 14.99)
        self.submit.assert_called_once_with(sidebar.Effect("switch", target, automatic=True))
        self.reconciler.observe(sidebar.EffectResult(
            self.submit.call_args.args[0], (target,), completed_at=20,
        ))
        self.submit.reset_mock()
        data = empty_sample()
        self.reconciler.reconcile((target,), target, data, self.submit, 29.99)
        self.submit.assert_not_called()
        self.reconciler.reconcile((target,), target, data, self.submit, 30)
        self.submit.assert_called_once_with(sidebar.Effect("switch", target, automatic=True))

    @patch.object(sidebar, "load_timing", return_value=config.Timing(ssh_auth_timeout=60))
    def test_switching_away_clears_startup_wait_for_tracked_remote(self, _load_timing):
        first = Target("ssh", "first", "dev")
        second = Target("ssh", "second", "dev")
        favorites = (first, second)
        data = snapshot(remotes={"dev": source("ssh", ("second",), host="dev")})
        self.reconciler.reconcile(favorites, first, data, self.submit, 0)
        self.reconciler.observe(sidebar.EffectResult(
            self.submit.call_args.args[0], favorites, completed_at=5,
        ))
        self.submit.reset_mock()
        self.reconciler.reconcile(favorites, first, data, self.submit, 6)
        self.submit.assert_not_called()

        self.reconciler.observe(sidebar.EffectResult(
            sidebar.Effect("switch", second), favorites, completed_at=7,
        ))
        self.reconciler.reconcile(favorites, second, data, self.submit, 8)
        self.submit.assert_called_once_with(sidebar.Effect("ensure_created", first, automatic=True))
        self.assertNotIn(first, self.reconciler._awaiting_discovery)

    @patch.object(sidebar, "load_timing", return_value=config.Timing(ssh_auth_timeout=10))
    @patch.object(sidebar.operations, "clear_startup")
    def test_explicit_navigation_waits_for_startup_timeout_with_empty_discovery(
        self, _clear_startup, _load_timing,
    ):
        target = Target("ssh", "work", "dev")
        for kind in ("switch", "add_switch", "create"):
            with self.subTest(kind=kind):
                reconciler = sidebar.SessionReconciler()
                self.submit.reset_mock()
                reconciler.observe(sidebar.EffectResult(
                    sidebar.Effect(kind, target), (target,), completed_at=5,
                ))
                # Attachment preparation consumes part of the shared startup wait.
                for now in (5, 8, 10, 14.99):
                    data = snapshot(remotes={"dev": source("ssh", host="dev")})
                    reconciler.reconcile((target,), target, data, self.submit, now, 10)
                self.submit.assert_not_called()
                reconciler.reconcile((target,), target, data, self.submit, 15, 10)
                self.submit.assert_called_once_with(sidebar.Effect("switch", target, automatic=True))

    @patch.object(sidebar, "load_timing", return_value=config.Timing(ssh_auth_timeout=10))
    def test_explicit_navigation_does_not_confirm_cached_presence(self, _timing):
        target = Target("ssh", "work", "dev")
        for kind in ("switch", "add_switch", "create"):
            with self.subTest(kind=kind):
                reconciler = sidebar.SessionReconciler()
                self.submit.reset_mock()
                cached = snapshot(remotes={
                    "dev": replace(source("ssh", ("work",), host="dev"), sampled_at=4),
                })
                reconciler.observe(sidebar.EffectResult(
                    sidebar.Effect(kind, target), (target,), completed_at=5,
                ))
                reconciler.reconcile((target,), target, cached, self.submit, 6)
                absent = snapshot(remotes={"dev": source("ssh", host="dev")})
                reconciler.reconcile((target,), target, absent, self.submit, 8)
                self.submit.assert_not_called()
                reconciler.reconcile((target,), target, absent, self.submit, 15)
                self.submit.assert_called_once_with(sidebar.Effect("switch", target, automatic=True))

    @patch.object(sidebar.cockpit, "load_timing", return_value=config.Timing(ssh_auth_timeout=10))
    @patch.object(sidebar.cockpit, "right_pane", return_value="%2")
    @patch.object(time, "monotonic", return_value=5)
    def test_numbered_shortcut_shares_startup_wait_without_observe(self, _clock, _pane, _timing):
        from letee.__main__ import main

        target = Target("ssh", "work", "dev")
        options = {}

        def tmux_command(*args, **kwargs):
            if args[0] == "set-option" and "-u" not in args:
                options[args[3]] = args[4]
            elif args[0] == "if-shell":
                # Apply marker updates and guarded clears to the fake tmux options.
                marker_guard = sidebar.cockpit._option_equals(
                    sidebar.cockpit.STARTUP_OPTION, options.get(sidebar.cockpit.STARTUP_OPTION, ""),
                )
                launch_guard = sidebar.cockpit._option_equals(
                    sidebar.cockpit.STARTUP_LAUNCH_OPTION, options.get(sidebar.cockpit.STARTUP_LAUNCH_OPTION, ""),
                )
                if args[2] in ("1", marker_guard, f"#{{&&:{marker_guard},{launch_guard}}}"):
                    for queued in args[-1].split(" ; "):
                        command = shlex.split(queued)
                        if "-u" in command:
                            options.pop(command[-1], None)
                        else:
                            options[command[-2]] = command[-1]

        def empty_sample():
            return snapshot(remotes={"dev": source("ssh", host="dev")})

        with (
            patch.object(sidebar.cockpit.tmux, "tmux", side_effect=tmux_command),
            patch.object(sidebar.cockpit, "_option", side_effect=lambda name: options.get(name, "")),
            patch("letee.config.load_sessions", return_value=[target]),
            patch.object(sidebar.sessions, "attach_command", return_value="slow ssh attachment"),
        ):
            cached = snapshot(remotes={
                "dev": replace(source("ssh", ("work",), host="dev"), sampled_at=4),
            })
            # Numbered shortcuts invoke switch in another process, without observe().
            main(["switch-session", "1"])
            self.assertEqual(options[sidebar.cockpit.CURRENT_TARGET_OPTION], target.format())
            status = sidebar.AsyncStatusPoller(unittest.mock.Mock(snapshot=cached), target)
            try:
                with patch.object(
                    sidebar.cockpit, "status_snapshot",
                    return_value=sidebar.cockpit.StatusSnapshot(target, None, None, True),
                ):
                    deadline = status._sample((), 0).startup_deadline
            finally:
                status.close()
            self.assertEqual(deadline, 15)
            self.reconciler.reconcile((target,), target, cached, self.submit, 5, deadline)
            self.assertIn(sidebar.cockpit.STARTUP_OPTION, options)
            for now in (5, 8, 14.99):
                self.reconciler.reconcile((target,), target, empty_sample(), self.submit, now, deadline)
            self.submit.assert_not_called()

            confirmed = snapshot(remotes={
                "dev": replace(source("ssh", ("work",), host="dev"), sampled_at=8),
            })
            self.reconciler.reconcile((target,), target, confirmed, self.submit, 8, deadline)
            for worker, _ in self.reconciler._startup_clears.values():
                worker.join(2)
            self.assertNotIn(sidebar.cockpit.STARTUP_OPTION, options)
            self.reconciler.reconcile((target,), target, empty_sample(), self.submit, 8)
            self.submit.assert_called_once_with(sidebar.Effect("switch", target, automatic=True))

            self.reconciler = sidebar.SessionReconciler()
            self.submit.reset_mock()
            _clock.return_value = 5
            main(["switch-session", "1"])
            self.reconciler.reconcile((target,), target, empty_sample(), self.submit, 14.99, deadline)
            self.submit.assert_not_called()
            self.reconciler.reconcile((target,), target, empty_sample(), self.submit, 15, deadline)
            self.submit.assert_called_once_with(sidebar.Effect("switch", target, automatic=True))

    @patch.object(sidebar.cockpit, "load_timing", return_value=config.Timing(ssh_auth_timeout=10))
    @patch.object(sidebar.cockpit, "right_pane", return_value="%2")
    @patch.object(time, "monotonic", return_value=5)
    def test_delayed_overlay_observation_preserves_same_target_shortcut_startup(self, clock, _pane, _timing):
        from letee.__main__ import main

        target = Target("ssh", "work", "dev")
        for kind in ("show_reconnecting", "show_unavailable"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                options = {sidebar.cockpit.CURRENT_TARGET_OPTION: target.format()}
                cleared_under_lock = []

                def tmux_command(*args, **kwargs):
                    if args[0] == "set-option" and "-u" not in args:
                        options[args[3]] = args[4]
                    elif args[0] == "if-shell":
                        guard = sidebar.cockpit._option_equals(
                            sidebar.cockpit.STARTUP_OPTION,
                            options.get(sidebar.cockpit.STARTUP_OPTION, ""),
                        )
                        if args[2] in ("1", guard):
                            for queued in args[-1].split(" ; "):
                                command = shlex.split(queued)
                                if "-u" in command:
                                    if command[-1] == sidebar.cockpit.STARTUP_OPTION:
                                        with config.sessions_lock(blocking=False) as acquired:
                                            cleared_under_lock.append(not acquired)
                                    options.pop(command[-1], None)
                                else:
                                    options[command[-2]] = command[-1]

                with (
                    patch.object(config, "config_dir", return_value=Path(directory)),
                    patch.object(sidebar.cockpit, "current_target", return_value=target),
                    patch.object(sidebar.cockpit.tmux, "tmux", side_effect=tmux_command) as tmux_command_mock,
                    patch.object(sidebar.cockpit, "_option", side_effect=lambda name: options.get(name, "")),
                    patch("letee.config.load_sessions", return_value=[target]),
                    patch.object(sidebar.sessions, "attach_command", return_value="slow ssh attachment"),
                ):
                    clock.return_value = 5
                    main(["switch-session", "1"])
                    clock.return_value = 15
                    reconciler = sidebar.SessionReconciler()
                    reconciler.observe(sidebar.EffectResult(
                        sidebar.Effect("switch", target), (target,), completed_at=5,
                    ))
                    overlay = sidebar._perform_effect(sidebar.Effect(kind, target, automatic=True), (target,))
                    self.assertFalse(overlay.error)
                    self.assertFalse(overlay.stale_navigation)
                    marker_after_overlay = sidebar.cockpit.startup_deadline(target)

                    # The shortcut completes before the UI observes the older overlay result.
                    clock.return_value = 16
                    main(["switch-session", "1"])
                    reconciler.observe(overlay)
                    deadline = sidebar.cockpit.startup_deadline(target)
                    self.assertEqual(deadline, 26)
                    self.assertIsNone(marker_after_overlay)
                    self.assertEqual(cleared_under_lock, [True])
                    # An old result must not change the newer launch's observation state.
                    self.assertIn(target, reconciler._awaiting_discovery)
                    self.submit.reset_mock()
                    tmux_command_mock.reset_mock()
                    for now in (16, 20, 25.99):
                        clock.return_value = now
                        absent = snapshot(remotes={"dev": source("ssh", host="dev")})
                        reconciler.reconcile((target,), target, absent, self.submit, now, deadline)
                        queued = sidebar._perform_effect(sidebar.Effect("switch", target, automatic=True), (target,))
                        self.assertTrue(queued.stale_navigation)
                    self.submit.assert_not_called()
                    tmux_command_mock.assert_not_called()

    def test_failed_or_stale_explicit_navigation_does_not_delay_reconciliation(self):
        target = Target("ssh", "work", "dev")
        for kind in ("switch", "add_switch", "create"):
            for error, stale in (("timeout", False), ("", True)):
                with self.subTest(kind=kind, error=error, stale=stale):
                    reconciler = sidebar.SessionReconciler()
                    self.submit.reset_mock()
                    reconciler.observe(sidebar.EffectResult(
                        sidebar.Effect(kind, target), (target,), error=error,
                        stale_navigation=stale, completed_at=5,
                    ))
                    data = snapshot(remotes={"dev": source("ssh", host="dev")})
                    reconciler.reconcile((target,), target, data, self.submit, 5)
                    self.submit.assert_called_once_with(sidebar.Effect("switch", target, automatic=True))

    @patch.object(sidebar, "load_timing", return_value=config.Timing(ssh_auth_timeout=60))
    def test_overlay_releases_startup_wait_only_after_success(self, _load_timing):
        target = Target("ssh", "work", "dev")
        for kind in ("show_reconnecting", "show_unavailable"):
            for error, stale in (("", False), ("timeout", False), ("", True)):
                with self.subTest(kind=kind, error=error, stale=stale):
                    reconciler = sidebar.SessionReconciler()
                    self.submit.reset_mock()
                    data = snapshot(remotes={"dev": source("ssh", host="dev")})
                    reconciler.reconcile((target,), target, data, self.submit, 0)
                    reconciler.observe(sidebar.EffectResult(
                        self.submit.call_args.args[0], (target,), completed_at=5,
                    ))
                    self.submit.reset_mock()
                    outage = snapshot(remotes={"dev": source("ssh", host="dev", available=False)})
                    reconciler.reconcile((target,), target, outage, self.submit, 6)
                    self.submit.assert_not_called()
                    reconciler.observe(sidebar.EffectResult(
                        sidebar.Effect(kind, target, automatic=True), (target,),
                        error=error, stale_navigation=stale, completed_at=6,
                    ))
                    recovered = snapshot(remotes={"dev": source("ssh", host="dev")})
                    reconciler.reconcile((target,), target, recovered, self.submit, 7)
                    if error or stale:
                        self.submit.assert_not_called()
                        reconciler.reconcile((target,), target, recovered, self.submit, 65)
                    self.submit.assert_called_once_with(sidebar.Effect("switch", target, automatic=True))

    def test_success_suppresses_same_source_until_new_source_identity(self):
        target = Target("local", "work")
        data = snapshot()
        self.reconciler.reconcile((target,), None, data, self.submit, 0)
        self.reconciler.observe(sidebar.EffectResult(
            self.submit.call_args.args[0], (target,), completed_at=1,
        ))
        self.submit.reset_mock()
        self.reconciler.reconcile((target,), None, SessionSnapshot(data.local, {}), self.submit, 100)
        self.submit.assert_not_called()
        self.reconciler.reconcile((target,), None, snapshot(), self.submit, 100)
        self.submit.assert_called_once()

    def test_success_suppression_is_per_source_not_whole_snapshot(self):
        target = Target("ssh", "work", "dev")
        remote = source("ssh", host="dev")
        data = snapshot(remotes={"dev": remote})
        self.reconciler.reconcile((target,), None, data, self.submit, 0)
        self.reconciler.observe(sidebar.EffectResult(
            self.submit.call_args.args[0], (target,), completed_at=1,
        ))
        self.submit.reset_mock()
        self.reconciler.reconcile((target,), None, snapshot(remotes={"dev": remote}), self.submit, 100)
        self.submit.assert_not_called()
        self.reconciler.reconcile((target,), None,
                                  snapshot(remotes={"dev": source("ssh", host="dev")}), self.submit, 100)
        self.submit.assert_called_once()

    def test_failed_first_favorite_does_not_starve_other_targets(self):
        first, second = Target("local", "first"), Target("local", "second")
        data = snapshot()
        self.reconciler.reconcile((first, second), None, data, self.submit, 0)
        effect = self.submit.call_args.args[0]
        self.reconciler.observe(sidebar.EffectResult(
            effect, (first, second), error="timeout", completed_at=1,
        ))
        self.submit.reset_mock()
        self.reconciler.reconcile((first, second), None, data, self.submit, 100)
        self.submit.assert_called_once()
        self.assertNotEqual(self.submit.call_args.args[0].target, effect.target)

    @patch.object(sidebar, "load_timing", return_value=config.Timing())
    def test_failure_backoff_is_independent_per_target(self, _load_timing):
        first, second = Target("local", "first"), Target("local", "second")
        data = snapshot()
        self.reconciler.reconcile((first,), None, data, self.submit, 0)
        self.reconciler.observe(sidebar.EffectResult(
            self.submit.call_args.args[0], (first,), error="timeout", completed_at=10,
        ))
        self.submit.reset_mock()
        self.reconciler.reconcile((first, second), None, data, self.submit, 10)
        self.submit.assert_called_once_with(sidebar.Effect("ensure_created", second, automatic=True))
        self.reconciler.observe(sidebar.EffectResult(
            self.submit.call_args.args[0], (first, second), error="timeout", completed_at=20,
        ))
        self.submit.reset_mock()
        self.reconciler.reconcile((first, second), None, data, self.submit, 20)
        self.submit.assert_called_once_with(sidebar.Effect("ensure_created", first, automatic=True))

    def test_one_submission_per_reconcile_and_in_flight_target_does_not_starve_others(self):
        targets = (Target("local", "first"), Target("local", "second"))
        data = snapshot()
        self.reconciler.reconcile(targets, None, data, self.submit, 0)
        self.submit.assert_called_once()
        first_effect = self.submit.call_args.args[0]
        self.submit.reset_mock()
        self.reconciler.reconcile(targets, None, data, self.submit, 0)
        self.submit.assert_called_once()
        self.assertNotEqual(self.submit.call_args.args[0].target, first_effect.target)

    def test_pruning_untracked_target_discards_in_flight_state(self):
        target = Target("local", "work")
        data = snapshot()
        self.reconciler.reconcile((target,), None, data, self.submit, 0)
        self.reconciler.reconcile((), None, data, self.submit, 1)
        self.submit.reset_mock()
        self.reconciler.reconcile((target,), None, data, self.submit, 1)
        self.submit.assert_called_once()

    def test_successful_kill_and_rename_do_not_recreate_obsolete_target(self):
        old, renamed = Target("local", "old"), Target("local", "renamed")
        for kind in ("kill", "rename"):
            with self.subTest(kind=kind):
                state = SidebarState(favorites=[old], selected_target=old)
                effect = sidebar.Effect(kind, old, message="renamed" if kind == "rename" else "")
                planned = operations.planned_favorites(effect, (old,))
                sidebar._apply_effect(sidebar.EffectResult(effect, planned), state, unittest.mock.Mock(), 5)
                submitted = []
                self.reconciler.reconcile(tuple(state.favorites), None, snapshot(),
                                          lambda item: submitted.append(item) or True, 0)
                self.assertNotIn(old, [item.target for item in submitted])
                self.assertEqual(submitted, [sidebar.Effect("ensure_created", renamed, automatic=True)]
                                 if kind == "rename" else [])

    def test_pruning_untracked_target_discards_backoff_and_success_suppression(self):
        target = Target("local", "work")
        for error in ("timeout", ""):
            with self.subTest(error=error):
                reconciler = sidebar.SessionReconciler()
                data = snapshot()
                self.submit.reset_mock()
                reconciler.reconcile((target,), None, data, self.submit, 0)
                reconciler.observe(sidebar.EffectResult(
                    self.submit.call_args.args[0], (target,), error=error, completed_at=10,
                ))
                reconciler.reconcile((), None, data, self.submit, 10)
                self.submit.reset_mock()
                reconciler.reconcile((target,), None, data, self.submit, 10)
                self.submit.assert_called_once()


class ReconciliationLoopTest(OperationTestCase):
    def _run_reconciliation(self, targets, active, initial, updates=(), keys=None):
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=initial, current_target=active, bell_target=None, startup_deadline=None,
            current_agent=None, pane_active=True, refresh_pending=False,
        )
        samples = iter(updates)

        def tick(_now):
            poller.snapshot = next(samples, poller.snapshot)
            return True

        poller.tick.side_effect = tick
        poller.observe_effect.side_effect = lambda result: setattr(poller, "launch_id", result.launch_id)
        results = []
        actions = unittest.mock.Mock(busy=False, blocks_favorite_changes=False)
        actions.submit.side_effect = lambda effect, favorites: results.append(
            sidebar._perform_effect(effect, favorites)
        ) or True
        actions.poll.side_effect = lambda: results.pop(0) if results else None
        screen = FakeScreen(keys or [-1] * 10 + [STOP], size=(16, 60))
        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "EffectRunner", return_value=actions),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=targets),
            patch.object(sidebar.cockpit, "current_target", return_value=active),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", return_value=(2, None)) as draw,
            patch.object(sidebar, "_bell_targets", return_value=set()),
            patch.object(sidebar.sessions, "ensure_created") as ensure,
            patch.object(sidebar.cockpit, "switch") as switch,
            patch.object(sidebar.cockpit, "show_reconnecting"),
            patch.object(config, "save_sessions"),
        ):
            sidebar.run(screen)
        return ensure, switch, draw, actions

    def test_startup_recreates_all_absent_local_and_remote_favorites(self):
        local, remote = Target("local", "local"), Target("ssh", "remote", "dev")
        ensure, switch, _, actions = self._run_reconciliation(
            [local, remote], None, snapshot(remotes={"dev": source("ssh", host="dev")}),
        )
        self.assertEqual(ensure.call_args_list, [call(local), call(remote)])
        switch.assert_not_called()
        self.assertEqual(actions.submit.call_count, 2)

    def test_startup_reattaches_absent_active_target_and_recreates_inactive(self):
        active, inactive = Target("local", "active"), Target("local", "inactive")
        ensure, switch, _, _ = self._run_reconciliation([active, inactive], active, snapshot())
        switch.assert_called_once_with(active, sidebar.sessions.attach_command(active, attach_only=True), launch_id=ANY)
        self.assertEqual(ensure.call_args_list, [call(active), call(inactive)])

    def test_inactive_remote_recreated_only_after_host_recovers(self):
        target = Target("ssh", "work", "dev")
        down = snapshot(remotes={"dev": source("ssh", host="dev", available=False)})
        recovered = snapshot(remotes={"dev": source("ssh", host="dev")})
        ensure, switch, _, _ = self._run_reconciliation(
            [target], None, down, [down, down, recovered],
        )
        ensure.assert_called_once_with(target)
        switch.assert_not_called()

    def test_active_absent_remote_reattaches_after_host_recovers(self):
        target = Target("ssh", "work", "dev")
        down = snapshot(remotes={"dev": source("ssh", host="dev", available=False)})
        recovered = snapshot(remotes={"dev": source("ssh", host="dev")})
        ensure, switch, _, _ = self._run_reconciliation(
            [target], target, down, [down, down, recovered],
        )
        ensure.assert_called_once_with(target)
        switch.assert_called_once_with(target, sidebar.sessions.attach_command(target, attach_only=True), launch_id=ANY)

    def test_same_target_shortcut_after_overlay_allows_fresh_discovery_and_other_favorite(self):
        active = Target("ssh", "work", "dev")
        missing = Target("local", "other")
        down = snapshot(remotes={"dev": source("ssh", host="dev", available=False)})
        present = snapshot(remotes={"dev": replace(
            source("ssh", ("work",), host="dev"), sampled_at=12,
        )})
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=down, current_target=active, bell_target=None, startup_deadline=None,
            current_agent=None, pane_active=True, refresh_pending=False,
        )
        actions = unittest.mock.Mock(busy=False, blocks_favorite_changes=False)
        results = []
        ticks = 0

        def tick(_now):
            nonlocal ticks
            ticks += 1
            if ticks == 2:
                # The numbered shortcut runs outside the sidebar and keeps the same target.
                poller.startup_deadline = 71
                poller.snapshot = replace(present, remotes={"dev": replace(
                    present.remotes["dev"], sampled_at=10,
                )})
            elif ticks == 3:
                poller.snapshot = present
            elif ticks == 4:
                # The shared startup cleanup has completed; the overlay must stay cleared.
                poller.startup_deadline = None
            return True

        def submit(effect, favorites):
            actions.busy = True
            # A switch queued during startup would be rejected by the worker.
            results.append(sidebar.EffectResult(
                effect, favorites, stale_navigation=effect.kind == "switch",
            ))
            return True

        def poll():
            actions.busy = False
            return results.pop(0) if results else None

        poller.tick.side_effect = tick
        actions.submit.side_effect = submit
        actions.poll.side_effect = poll
        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "EffectRunner", return_value=actions),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[active, missing]),
            patch.object(sidebar.cockpit, "current_target", return_value=active),
            patch.object(sidebar.cockpit, "current_target", return_value=active),
            patch.object(sidebar.cockpit, "startup_timeout", return_value=60),
            patch.object(sidebar.SessionReconciler, "_clear_startup") as clear_startup,
            patch.object(sidebar.time, "monotonic", return_value=12),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", return_value=(2, None)),
            patch.object(sidebar, "_bell_targets", return_value=set()),
        ):
            sidebar.run(FakeScreen([-1] * 8 + [STOP], size=(16, 60)))
        effects = [item.args[0] for item in actions.submit.call_args_list]
        self.assertEqual(effects, [
            sidebar.Effect("show_reconnecting", active, automatic=True),
            sidebar.Effect("ensure_created", missing, automatic=True),
        ])
        clear_startup.assert_called_once_with(active, 71, None)

    def test_background_creation_preserves_keyboard_navigation_and_active_marker(self):
        first, second = Target("local", "first"), Target("local", "second")
        ensure, switch, draw, _ = self._run_reconciliation(
            [first, second], None, snapshot(), keys=[-1, curses.KEY_DOWN, -1, -1, -1, STOP],
        )
        self.assertEqual(ensure.call_args_list, [call(first), call(second)])
        switch.assert_not_called()
        self.assertEqual(draw.call_args.args[2], 1)
        self.assertIsNone(draw.call_args.kwargs.get("current_target"))

    def test_cli_mutations_while_sidebar_is_running(self):
        from letee.__main__ import main

        old, renamed = Target("local", "old"), Target("local", "renamed")
        other = Target("local", "other")
        for command, tracked in (("kill", True), ("rename", True), ("kill", False)):
            for active in (old, other):
                with self.subTest(command=command, active=active, tracked=tracked), tempfile.TemporaryDirectory() as directory:
                    with patch.dict(os.environ, {"LETEE_CONFIG_DIR": directory}):
                        config.save_sessions([old, other] if tracked else [other])
                        marker = [active]
                        data = snapshot(local=("old", "other"))
                        poller = unittest.mock.Mock(launch_id=None,
                            snapshot=data, current_target=active, bell_target=None, startup_deadline=None,
                            current_agent=None, pane_active=True, refresh_pending=False,
                        )
                        ticks = [0]

                        def tick(_now):
                            ticks[0] += 1
                            if ticks[0] == 2:
                                main([command, old.format(), *([renamed.session] if command == "rename" else [])])
                                poller.snapshot = snapshot(
                                    local=("other", "renamed") if command == "rename" else ("other",),
                                )
                            # Deliberately leave the sampled active target stale.
                            return True

                        def mutate(*_args):
                            with config.sessions_lock(blocking=False) as acquired:
                                self.assertFalse(acquired)
                            return renamed

                        poller.tick.side_effect = tick
                        poller.request_target_sync.side_effect = lambda: setattr(poller, "current_target", marker[0])
                        results = []
                        actions = unittest.mock.Mock(busy=False, blocks_favorite_changes=False)
                        actions.submit.side_effect = lambda effect, favorites: results.append(
                            sidebar._perform_effect(effect, favorites)
                        ) or True
                        actions.poll.side_effect = lambda: results.pop(0) if results else None
                        with (
                            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
                            patch.object(sidebar, "EffectRunner", return_value=actions),
                            patch.object(sidebar, "load_hosts", return_value=[]),
                            patch.object(sidebar.cockpit, "current_target", side_effect=lambda **_: marker[0]),
                            patch.multiple(sidebar.cockpit,
                                set_expected_right_pane_death=unittest.mock.Mock(),
                                resolve_expected_right_pane_death=unittest.mock.Mock(side_effect=lambda target, succeeded: marker.__setitem__(0, None) if succeeded and marker[0] == target else None)),

                            patch.object(sidebar.cockpit, "reset_to_help", side_effect=lambda target: marker.__setitem__(0, None) if marker[0] == target else False),
                            patch.object(sidebar.cockpit, "rename_target", side_effect=lambda target, new: marker.__setitem__(0, new) if marker[0] == target else None),
                            patch.object(sidebar.sessions, "kill", side_effect=mutate),
                            patch.object(sidebar.sessions, "rename", side_effect=mutate),
                            patch.multiple(sidebar, _init_colors=unittest.mock.DEFAULT, _mouse_mask=unittest.mock.DEFAULT),
                            patch.object(sidebar.curses, "curs_set"),
                            patch.object(sidebar, "_draw", return_value=(2, None)) as draw,
                            patch.object(sidebar, "_bell_targets", return_value=set()),
                            patch.object(sidebar.sessions, "ensure_created") as ensure,
                            patch.object(sidebar.cockpit, "switch") as switch,
                        ):
                            sidebar.run(FakeScreen([-1] * 12 + [STOP], size=(16, 60)))
                        ensure.assert_not_called()
                        if command == "rename" and active == old:
                            _assert_deferred_switch(self, switch, renamed)
                            self.assertEqual(switch.call_args.kwargs["launch_id"], config.load_operations()["launch"])
                            self.assertEqual(switch.call_args.args[1](), sidebar.sessions.attach_command(renamed, attach_only=True))
                        else:
                            switch.assert_not_called()
                        self.assertNotIn(old, config.load_sessions())
                        expected = (renamed if command == "rename" else None) if active == old else other
                        self.assertEqual(marker[0], expected)
                        self.assertEqual(poller.current_target, expected)
                        self.assertNotIn(old, [entry.target for entry in draw.call_args.args[1]])

    def test_remote_removal_then_local_navigation_does_not_block_reconciliation(self):
        old = Target("ssh", "old", "dev")
        active, missing = Target("local", "work"), Target("local", "missing")
        data = snapshot(local=("work",), remotes={"dev": source("ssh", host="dev")})
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"LETEE_CONFIG_DIR": directory}):
            config.save_sessions([old, active, missing])
            poller = sidebar.AsyncStatusPoller(unittest.mock.Mock(snapshot=data), old)
            marker = [old]
            ticks = [0]
            original_tick = poller.tick

            def tick(now):
                ticks[0] += 1
                if ticks[0] == 2:
                    config.save_sessions([active, missing])
                    marker[0] = active
                if ticks[0] > 2:
                    commands, poller._commands = poller._commands, []
                    poller._sample_revision = config.sessions_revision()
                    poller._future = unittest.mock.Mock()
                    poller._future.done.return_value = True
                    poller._future.result.return_value = sidebar.StatusResult(
                        data, active, None, None, True, poller._generation,
                        any(command == "refresh" for command, _ in commands),
                        sampled=True, synchronized=poller._synchronize_pending,
                    )
                poller._next_poll = float("inf")
                return original_tick(now)

            actions = unittest.mock.Mock(busy=False, blocks_favorite_changes=False)
            actions.poll.return_value = None
            try:
                with (
                    patch.object(poller, "tick", side_effect=tick),
                    patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
                    patch.object(sidebar, "EffectRunner", return_value=actions),
                    patch.object(sidebar, "load_hosts", return_value=[]),
                    patch.object(sidebar.cockpit, "current_target", side_effect=lambda **_: marker[0]),
                    patch.object(sidebar, "_init_colors"),
                    patch.object(sidebar, "_mouse_mask"),
                    patch.object(sidebar.curses, "curs_set"),
                    patch.object(sidebar, "_draw", return_value=(2, None)),
                    patch.object(sidebar, "_bell_targets", return_value=set()),
                ):
                    sidebar.run(FakeScreen([-1] * 6 + [STOP], size=(16, 60)))
                self.assertEqual(poller.current_target, active)
                self.assertIsNone(poller._refresh_target)
                self.assertFalse(poller.refresh_pending)
                self.assertIn(
                    Effect("ensure_created", missing, automatic=True),
                    [call.args[0] for call in actions.submit.call_args_list],
                )
            finally:
                poller.close()

    def test_removal_before_reconciliation_does_not_recreate_target(self):
        target = Target("local", "work")
        ensure, switch, _, _ = self._run_reconciliation(
            [target], None, snapshot(), keys=[ord("r"), -1, -1, STOP],
        )
        ensure.assert_not_called()
        switch.assert_not_called()


class SidebarTrackingMutationTest(OperationTestCase):
    def test_explicit_remote_switch_holds_sessions_lock_during_preparation_and_launch(self):
        target = Target("ssh", "work", "dev")
        command = "prepared remote attach"

        def assert_locked():
            with config.sessions_lock(blocking=False) as acquired:
                self.assertFalse(acquired)

        def prepare(_target):
            assert_locked()
            return command

        def launch(*args, **kwargs):
            if args[:5] == ("respawn-pane", "-k", "-t", "%2", command):
                assert_locked()

        pane = PaneTarget(target, "@1", "%3", "/tmp/tmux")
        for kind, destination, prepare_method in (
            ("switch", target, "attach_command"),
            ("switch_pane", pane, "pane_attach_command"),
        ):
            with (
                self.subTest(kind=kind),
                tempfile.TemporaryDirectory() as directory,
                patch.object(config, "config_dir", return_value=Path(directory)),
                patch.object(sidebar.sessions, prepare_method, side_effect=prepare) as attach,
                patch.object(sidebar.cockpit, "right_pane", return_value="%2"),
                patch.object(sidebar.cockpit, "startup_timeout", return_value=10),
                patch.object(sidebar.cockpit.tmux, "tmux", side_effect=launch) as tmux_command,
            ):
                result = sidebar._perform_effect(sidebar.Effect(kind, destination), (target,))
                with config.sessions_lock(blocking=False) as acquired:
                    self.assertTrue(acquired)

                self.assertFalse(result.error)
                attach.assert_called_once_with(destination)
                tmux_command.assert_any_call(
                    "respawn-pane", "-k", "-t", "%2", command, ";",
                    "display-message", "-p", "letee-respawn-succeeded", ";",
                    "set-option", "-p", "-t", "%2", sidebar.cockpit.ATTACHMENT_TARGET_OPTION, target.format(), ";",
                    "set-option", "-p", "-F", "-t", "%2", sidebar.cockpit.ATTACHMENT_LAUNCH_GENERATION_OPTION, "#{pane_pid}", ";",
                    "display-message", "-p", "-t", "%2", "#{pane_pid}", capture=True,
                )

    def test_cli_kill_before_sidebar_rename_acquires_lock(self):
        from contextlib import contextmanager
        from letee.__main__ import main

        killed, old, renamed = (Target("local", name) for name in ("A", "B", "new"))
        lock = config.sessions_lock

        @contextmanager
        def kill_before_lock():
            with patch.object(config, "sessions_lock", lock):
                main(["kill", killed.format()])
            with lock() as acquired:
                yield acquired

        def rename(*_args):
            with lock(blocking=False) as acquired:
                self.assertFalse(acquired)
            return renamed

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"LETEE_CONFIG_DIR": directory}):
            config.save_sessions([killed, old])
            with (
                patch.object(sidebar.cockpit, "current_target", return_value=None),
                patch.object(sidebar.cockpit, "current_target", return_value=None),
                patch.object(sidebar.cockpit, "reset_to_help"),
                patch.object(sidebar.cockpit, "rename_target"),
                patch.object(sidebar.sessions, "kill"),
                patch.object(sidebar.sessions, "rename", side_effect=rename),
                patch.object(config, "sessions_lock", side_effect=kill_before_lock),
            ):
                result = sidebar._perform_effect(sidebar.Effect("rename", old, message="new"), (killed, old))
            self.assertFalse(result.error)
            self.assertEqual(config.load_sessions(), [renamed])
            submit = unittest.mock.Mock(return_value=True)
            sidebar.SessionReconciler().reconcile(result.favorites, None, snapshot(), submit, 0)
            self.assertNotIn(killed, [call.args[0].target for call in submit.call_args_list])
            with (
                patch.object(sidebar.cockpit, "current_target", return_value=None),
                patch.object(sidebar.sessions, "ensure_created") as ensure,
            ):
                sidebar._perform_effect(sidebar.Effect("ensure_created", killed, automatic=True), (killed, old))
            ensure.assert_not_called()

    def test_stale_removal_and_reorder_preserve_concurrent_tracking_changes(self):
        killed, first, second, added = (Target("local", name) for name in ("A", "B", "C", "D"))
        for action, expected in (("remove_session", [second, added]), ("move_session_down", [second, first, added])):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                with patch.dict(os.environ, {"LETEE_CONFIG_DIR": directory}):
                    state = SidebarState(favorites=[killed, first, second], selected_target=first, selected_tracked=True)
                    effect = sidebar._transition(state, action)
                    config.save_sessions([first, second, added])
                    with patch.object(sidebar.cockpit, "current_target", return_value=None):
                        result = sidebar._perform_effect(effect, (killed, first, second))
                    self.assertFalse(result.error)
                    self.assertEqual(config.load_sessions(), expected)
                    self.assertEqual(result.favorites, tuple(expected))
                    submit = unittest.mock.Mock(return_value=True)
                    sidebar.SessionReconciler().reconcile(result.favorites, None, snapshot(), submit, 0)
                    self.assertNotIn(killed, [call.args[0].target for call in submit.call_args_list])


class AutomaticSessionEffectTest(OperationTestCase):
    def test_reachable_absent_active_session_submits_switch_without_missing_screen(self):
        target = Target("local", "work")
        submit = unittest.mock.Mock(return_value=True)
        marker = sidebar._sync_active_session(target, snapshot(), None, submit)
        self.assertIsNone(marker)
        submit.assert_called_once_with(sidebar.Effect("switch", target, automatic=True))

    def test_automatic_switch_preserves_selection_and_filter(self):
        active, selected = Target("local", "active"), Target("local", "selected")
        state = SidebarState(selected_target=selected, filter_text="selected", selected_index=2)
        sidebar._apply_effect(sidebar.EffectResult(
            sidebar.Effect("switch", active, automatic=True), (active, selected),
        ), state, unittest.mock.Mock(), 5)
        self.assertEqual((state.selected_target, state.filter_text, state.selected_index),
                         (selected, "selected", 2))

    def test_queued_creation_rechecks_tracking_before_recreating(self):
        target = Target("local", "removed")
        effect = sidebar.Effect("ensure_created", target, automatic=True)
        with (
            patch.object(config, "load_sessions", return_value=[]),
            patch.object(sidebar.cockpit, "current_target", return_value=None),
            patch.object(sidebar.sessions, "ensure_created") as ensure,
        ):
            result = sidebar._perform_effect(effect, (target,))
        self.assertTrue(result.stale_navigation)
        ensure.assert_not_called()

    def test_ensure_created_has_no_navigation_or_ui_side_effects(self):
        target, selected = Target("local", "work"), Target("local", "selected")
        favorites = (selected, target)
        effect = sidebar.Effect("ensure_created", target, automatic=True)
        state = SidebarState(favorites=list(favorites), selected_target=selected,
                             filter_text="selected", status="unchanged", add_view="search")
        before = vars(state).copy()
        poller = unittest.mock.Mock(launch_id=None, )

        def ensure_locked(_target):
            with config.sessions_lock(blocking=False) as acquired:
                self.assertFalse(acquired)

        with (
            patch.object(config, "load_sessions", return_value=list(favorites)),
            patch.object(sidebar.sessions, "ensure_created", side_effect=ensure_locked) as ensure,
            patch.object(sidebar.cockpit, "switch") as switch,
            patch.object(config, "save_sessions") as save,
        ):
            result = sidebar._perform_effect(effect, favorites)
            sidebar._apply_effect(result, state, poller, 5)
        ensure.assert_called_once_with(target)
        switch.assert_not_called()
        save.assert_not_called()
        self.assertFalse(result.error)
        self.assertEqual(result.favorites, favorites)
        self.assertEqual(tuple(state.favorites), favorites)
        self.assertEqual(vars(state), before)

    def test_active_reconciliation_blocks_obsolete_target_mutations(self):
        from concurrent.futures import Future

        target = Target("local", "work")
        runner = sidebar.EffectRunner()
        try:
            with patch.object(runner, "_start", side_effect=lambda *_: setattr(runner, "_future", Future())):
                runner.submit(sidebar.Effect("switch", target, automatic=True), (target,))
                self.assertTrue(runner.blocks_favorite_changes)
                self.assertFalse(runner.submit(sidebar.Effect("rename", target, message="new"), (target,)))
                self.assertFalse(runner.submit(sidebar.Effect("kill", target), (target,)))
        finally:
            runner.close()

    def test_ensure_created_blocks_favorite_changes_but_queues_navigation(self):
        from concurrent.futures import Future

        target, next_target = Target("local", "work"), Target("local", "next")
        ensure = sidebar.Effect("ensure_created", target, automatic=True)
        navigation = sidebar.Effect("switch", next_target)
        runner = sidebar.EffectRunner()
        future = Future()
        try:
            with patch.object(runner, "_start", side_effect=lambda *_: setattr(runner, "_future", future)):
                self.assertTrue(runner.submit(ensure, (target, next_target)))
                self.assertTrue(runner.blocks_favorite_changes)
                self.assertTrue(runner.submit(navigation, (target, next_target)))
                self.assertFalse(runner.submit(sidebar.Effect("kill", target), (target, next_target)))
                future.set_result(sidebar.EffectResult(ensure, (target, next_target)))
                result = runner.poll()
                self.assertEqual(result.effect, ensure)
                self.assertTrue(runner.busy)
                self.assertFalse(runner.blocks_favorite_changes)
                self.assertEqual(runner._effect, navigation)
        finally:
            runner.close()


class ActiveSessionAvailabilityTest(OperationTestCase):
    def setUp(self):
        super().setUp()
        ensure_created = patch.object(sidebar.sessions, "ensure_created")
        ensure_created.start()
        self.addCleanup(ensure_created.stop)

    def test_pending_startup_overlays_do_not_starve_local_favorite(self):
        active = Target("ssh", "work", "dev")
        missing = Target("local", "other")
        for remotes, overlay in (
            ({"dev": None}, "show_reconnecting"),
            ({"dev": source("ssh", host="dev", available=False)}, "show_reconnecting"),
            ({}, "show_unavailable"),
        ):
            for marker in (None, active):
                with self.subTest(remotes=remotes, marker=marker):
                    data = snapshot(remotes=remotes)
                    submitted = []

                    def submit(effect):
                        submitted.append(effect)
                        return True

                    interrupted = sidebar._sync_active_session(
                        active, data, marker, submit, now=10, shared_deadline=42,
                    )
                    self.assertEqual(interrupted, marker)
                    self.assertEqual(submitted, [])
                    reconciler = sidebar.SessionReconciler()
                    reconciler.reconcile(
                        (active, missing), active, data, submit, 10, shared_deadline=42,
                    )
                    self.assertEqual(submitted, [Effect("ensure_created", missing, automatic=True)])
                    with (
                        patch.object(sidebar.sessions, "ensure_created") as ensure,
                        patch.object(config, "load_sessions", return_value=[active, missing]),
                        patch.object(sidebar.cockpit, "startup_deadline", return_value=None),
                    ):
                        effect = operations.capture(submitted.pop())
                        result = sidebar._perform_effect(effect, (active, missing))
                    self.assertFalse(result.error)
                    self.assertFalse(result.stale_navigation)
                    ensure.assert_called_once_with(missing)

                    sidebar._sync_active_session(
                        active, data, marker, submit, now=42, shared_deadline=42,
                    )
                    self.assertEqual(
                        submitted,
                        [] if marker == active else [Effect(overlay, active, automatic=True)],
                    )

    @patch.object(sidebar, "load_timing", return_value=config.Timing())
    def test_restoration_backoff_uses_completion_time_and_healthy_discovery_does_not_reset(self, _load_timing):
        target = Target("local", "work")
        healthy = snapshot(local=("work",))
        retry = sidebar.RestorationRetry()
        submitted = []
        marker = target
        for completed, delay in ((10, 1), (20, 2), (30, 4), (40, 5), (50, 5)):
            failed = sidebar.EffectResult(
                sidebar.Effect("switch", target, automatic=True), (), error="timeout"
            )
            marker = sidebar._reconcile_active_session_effect(
                marker, failed, retry=retry, now=completed
            )
            sidebar._sync_active_session(
                target, healthy, marker, lambda effect: submitted.append(effect) or True,
                retry=retry, now=completed + delay - 0.01,
            )
            self.assertEqual(len(submitted), 0)
            sidebar._sync_active_session(
                target, healthy, marker, lambda effect: submitted.append(effect) or True,
                retry=retry, now=completed + delay,
            )
            self.assertEqual(submitted, [sidebar.Effect("switch", target, automatic=True)])
            submitted.clear()

    @patch.object(sidebar, "load_timing", return_value=config.Timing())
    def test_combined_schedulers_preserve_attachment_backoff_after_discovery(self, _timing):
        target, favorite = Target("local", "work"), Target("local", "other")
        retry = sidebar.RestorationRetry()
        reconciler = sidebar.SessionReconciler()
        marker = None
        launch_id = "initial"

        def schedule(now, failed, favorites=(target,)):
            nonlocal marker
            data = SessionSnapshot(replace(source("local", ("work",)), sampled_at=now), {})
            submitted = []

            def submit(effect):
                submitted.append(effect)
                return True

            # Match production order and the action runner's busy guard.
            marker = sidebar._sync_active_session(
                target, data, marker, submit, retry=retry, now=now,
            )
            if not submitted:
                reconciler.reconcile(
                    favorites, target, data, submit, now,
                    shared_launch_id=launch_id, attachment_failed=failed,
                    restoration_retry=retry,
                )
            return submitted

        for generation, (failed_at, delay) in enumerate(((0, 1), (2, 2), (5, 4), (10, 5), (16, 5))):
            with self.subTest(failed_at=failed_at, delay=delay):
                marker = sidebar._reconcile_attachment_failure(
                    marker, target, (target, str(generation)), retry, failed_at,
                )
                self.assertEqual(retry.deadline, failed_at + delay)
                self.assertEqual(schedule(failed_at, True), [])
                if delay > 1:
                    # In particular, t=3 must not switch before the t=4 deadline.
                    effects = schedule(failed_at + 1, True, (target, favorite))
                    self.assertEqual(effects, [Effect("ensure_created", favorite, automatic=True)])
                    reconciler.observe(sidebar.EffectResult(
                        effects[0], (), completed_at=failed_at + 1,
                    ))
                self.assertEqual(schedule(failed_at + delay - 0.01, True), [])
                launched_at = failed_at + delay
                effects = schedule(launched_at, True)
                self.assertEqual(effects, [Effect("switch", target, automatic=True)])
                launch_id = str(generation)
                config.save_operations({"launch": launch_id})
                result = sidebar.EffectResult(
                    effects[0], (), launch_id=launch_id, completed_at=launched_at,
                )
                reconciler.observe(result)
                marker = sidebar._reconcile_active_session_effect(marker, result, retry=retry)
                self.assertEqual(schedule(launched_at + 0.2, False), [])
                self.assertEqual(retry.delay, delay)

    @patch.object(sidebar, "load_timing", return_value=config.Timing())
    def test_overlay_failure_after_restoration_launch_retries_with_healthy_discovery(self, _timing):
        target = Target("ssh", "work", "dev")
        healthy = snapshot(remotes={"dev": source("ssh", ("work",), host="dev")})
        retry = sidebar.RestorationRetry()
        marker = target
        with (
            patch.object(sidebar.cockpit, "current_target", return_value=target),
            patch.object(sidebar.cockpit, "startup_deadline", return_value=None),
            patch.object(sidebar.sessions, "ensure_created"),
            patch.object(sidebar.sessions, "attach_command", return_value="ssh dev attach"),
            patch.object(sidebar.cockpit, "switch") as launch,
        ):
            for generation, delay in enumerate((1, 2, 4, 5, 5), 1):
                submitted = []
                sidebar._sync_active_session(
                    target, healthy, marker, lambda effect: submitted.append(effect) or True,
                    retry=retry, now=retry.deadline,
                )
                self.assertEqual(len(submitted), 1)
                result = sidebar._perform_effect(submitted[0], ())
                marker = sidebar._reconcile_active_session_effect(marker, result, retry=retry)
                self.assertIsNone(marker)
                failure = (target, str(generation))
                now = retry.deadline + 1
                marker = sidebar._reconcile_attachment_failure(marker, target, failure, retry, now)
                self.assertEqual(marker, target)
                self.assertEqual(retry.delay, delay)
                deadline = retry.deadline
                marker = sidebar._reconcile_attachment_failure(marker, target, failure, retry, now + 0.1)
                self.assertEqual(retry.deadline, deadline)
                self.assertEqual(sidebar._sync_active_session(
                    target, healthy, marker, lambda _: self.fail("retried before backoff"),
                    retry=retry, now=deadline - 0.01,
                ), target)
            # Installation now works. Launch clears the marker, not the retry history.
            submitted = []
            sidebar._sync_active_session(
                target, healthy, marker, lambda effect: submitted.append(effect) or True,
                retry=retry, now=retry.deadline,
            )
            result = sidebar._perform_effect(submitted[0], ())
            marker = sidebar._reconcile_active_session_effect(marker, result, retry=retry)
            self.assertIsNone(marker)
            sidebar._sync_active_session(
                target, healthy, marker, lambda _: self.fail("duplicate attachment"),
                retry=retry, now=retry.deadline + 100,
            )
        self.assertEqual(launch.call_count, 6)

    @patch.object(sidebar, "load_timing", return_value=config.Timing())
    def test_clean_detach_does_not_retry_but_failure_markers_do(self, _timing):
        for target, healthy in (
            (Target("local", "work"), snapshot(local=("work",))),
            (Target("ssh", "work", "dev"), snapshot(
                remotes={"dev": source("ssh", ("work",), host="dev")},
            )),
        ):
            for failed in (False, True):
                with self.subTest(target=target, failed=failed):
                    value = target.format()
                    failure_fields = f"{value}\t123" if failed else "\t"
                    with patch.object(
                        sidebar.cockpit.tmux, "out",
                        return_value=f"%1\t%1\t{value}\t\t\t{failure_fields}\t.",
                    ):
                        status = sidebar.cockpit.status_snapshot()
                    retry = sidebar.RestorationRetry()
                    marker = sidebar._reconcile_attachment_failure(
                        None, status.current_target, status.attachment_failure, retry, 10,
                    )
                    self.assertEqual(marker, target if failed else None)
                    submitted = []
                    sidebar._sync_active_session(
                        target, healthy, marker,
                        lambda effect: submitted.append(effect) or True,
                        retry=retry, now=100,
                    )
                    self.assertEqual(submitted, [
                        sidebar.Effect("switch", target, automatic=True),
                    ] if failed else [])

    @patch.object(sidebar, "load_timing", return_value=config.Timing())
    def test_pane_death_failure_channels_drive_both_schedulers(self, _timing):
        cockpit = sidebar.cockpit
        target = Target("local", "work")
        for exit_status, signal, generation, attachment_target, expected, preparation, failed in (
            ("0", "", "123", target.format(), "", False, False),
            ("1", "", "123", target.format(), "", False, True),
            ("0", "TERM", "123", target.format(), "", False, True),
            ("1", "", "122", target.format(), "", False, False),
            ("1", "", "123", "local:other", "", False, False),
            ("1", "", "123", target.format(), "123", False, False),
            ("0", "", "123", target.format(), "", True, True),
        ):
            with self.subTest(exit_status=exit_status, signal=signal, generation=generation,
                              attachment_target=attachment_target, expected=expected,
                              preparation=preparation):
                options = {
                    "hook_pane": "%2", "pane_dead": "1", "pane_pid": "123",
                    "pane_dead_status": exit_status, "pane_dead_signal": signal,
                    cockpit.CURRENT_TARGET_OPTION: target.format(),
                    cockpit.STARTUP_LAUNCH_OPTION: "launch",
                    cockpit.ATTACHMENT_LAUNCH_GENERATION_OPTION: generation,
                    cockpit.ATTACHMENT_TARGET_OPTION: attachment_target,
                    cockpit.EXPECTED_RIGHT_PANE_DEATH_GENERATION_OPTION: expected,
                }

                def expand(value):
                    def replace_format(match):
                        field = match[1]
                        if ":" not in field:
                            return options.get(field, "")
                        operator, operands = field.split(":", 1)
                        left, right = operands.split(",", 1)
                        def truth(v):
                            return bool(v and v != "0")

                        return str(int({
                            "==": left == right, "!=": left != right,
                            "&&": truth(left) and truth(right),
                            "||": truth(left) or truth(right),
                        }[operator]))
                    while "#{" in value:
                        value = re.sub(r"#\{([^{}]*)\}", replace_format, value)
                    return value

                def execute(tokens):
                    # Interpret only the hook's native conditionals and option writes.
                    while tokens:
                        if tokens[0] == ";":
                            tokens = tokens[1:]
                            continue
                        if tokens[0] == "if-shell":
                            condition = expand(tokens[2])
                            tokens = tokens[3:]
                            branches = []
                            while tokens and tokens[0] == "{":
                                depth, end = 1, 1
                                while depth:
                                    depth += (tokens[end] == "{") - (tokens[end] == "}")
                                    end += 1
                                branches.append(tokens[1:end - 1])
                                tokens = tokens[end:]
                            branch = 0 if condition not in ("", "0") else 1
                            if branch < len(branches):
                                execute(branches[branch])
                            continue
                        end = tokens.index(";") if ";" in tokens else len(tokens)
                        command, tokens = tokens[:end], tokens[end:]
                        if command[0] == "set-option":
                            if "-u" in command:
                                options.pop(command[-1], None)
                            else:
                                options[command[-2]] = expand(command[-1])

                with (
                    patch.dict(os.environ, {"LETEE_DEBUG_LOG": ""}),
                    patch.object(cockpit.tmux, "tmux") as call,
                ):
                    cockpit._install_right_pane_reset("%1", "%2", "C-x")
                command = call.call_args.args[-1]
                if preparation:
                    command = cockpit._right_pane_death_action(
                        "%1", "%2", "", succeeded=False, target=target,
                    )
                execute(shlex.split(command))
                config.save_operations({"launch": "launch", "invalidated": []})
                fields = [options.get(name, "") for name in (
                    cockpit.ATTACHMENT_FAILURE_TARGET_OPTION,
                    cockpit.ATTACHMENT_FAILURE_GENERATION_OPTION,
                )]
                with (
                    patch.object(cockpit, "_option", side_effect=lambda name: options.get(name, "")),
                    patch.object(cockpit.tmux, "out", return_value=(
                        f"%1\t%1\t{target.format()}\t\t\t" + "\t".join(fields) + "\t."
                    )),
                ):
                    launch, status, active, deadline = operations.sample_target()
                self.assertEqual(status.attachment_failed, failed)
                self.assertEqual(status.attachment_failure is not None, failed and not preparation)
                retry = sidebar.RestorationRetry()
                marker = sidebar._reconcile_attachment_failure(
                    None, active, status.attachment_failure, retry, 0,
                )
                reconciler = sidebar.SessionReconciler()
                submitted = []
                for now in (0, 1):
                    data = SessionSnapshot(replace(source("local", ("work",)), sampled_at=now), {})
                    def submit(effect):
                        return submitted.append(effect) or True

                    marker = sidebar._sync_active_session(
                        active, data, marker, submit, retry=retry, now=now,
                    )
                    if not submitted:
                        reconciler.reconcile(
                            (target,), active, data, submit, now,
                            shared_launch_id=launch, shared_deadline=deadline,
                            attachment_failed=status.attachment_failed, restoration_retry=retry,
                        )
                self.assertEqual(submitted, [Effect("switch", target, automatic=True)] if failed else [])

    @patch.object(sidebar, "load_timing", return_value=config.Timing())
    def test_renamed_attachment_failure_retries_only_for_current_generation(self, _timing):
        cockpit = sidebar.cockpit
        old, new = Target("ssh", "old", "dev"), Target("ssh", "new", "dev")
        healthy = snapshot(remotes={"dev": source("ssh", ("new",), host="dev")})
        other = Target("local", "other")
        for generation, attachment_target, pending_failure, renamed in (
            ("123", old, None, True), ("122", old, None, False), ("123", other, None, False),
            ("123", old, (old, "123"), True),
            ("123", old, (other, "123"), False),
            ("124", old, (old, "123"), False),
            ("123", other, (old, "123"), False),
        ):
            with self.subTest(generation=generation, attachment_target=attachment_target,
                              pending_failure=pending_failure):
                options = {
                    cockpit.CURRENT_TARGET_OPTION: old.format(),
                    cockpit.ATTACHMENT_TARGET_OPTION: attachment_target.format(),
                    cockpit.ATTACHMENT_LAUNCH_GENERATION_OPTION: generation,
                    "pane_pid": "123", "pane_dead": "0", "pane_dead_status": "1",
                }
                retry = sidebar.RestorationRetry()
                marker = None
                if pending_failure:
                    failed_target, failed_generation = pending_failure
                    options[cockpit.ATTACHMENT_FAILURE_TARGET_OPTION] = failed_target.format()
                    options[cockpit.ATTACHMENT_FAILURE_GENERATION_OPTION] = failed_generation
                    marker = sidebar._reconcile_attachment_failure(None, old, pending_failure, retry, 10)
                    # The unavailable-screen respawn replaces the failed attachment's PID.
                    options["pane_pid"] = "456"

                def expand(value):
                    def replace(match):
                        field = match[1]
                        if ":" not in field:
                            return options.get(field, "")
                        operator, operands = field.split(":", 1)
                        left, right = operands.split(",", 1)
                        return str(int({
                            "==": left == right, "!=": left != right,
                            "&&": bool(left and left != "0" and right and right != "0"),
                            "||": bool((left and left != "0") or (right and right != "0")),
                        }[operator]))
                    while "#{" in value:
                        value = re.sub(r"#\{([^{}]*)\}", replace, value)
                    return value

                def execute(*args):
                    if args[0] == "if-shell":
                        end = args.index("}")
                        if expand(args[2]) == "1":
                            execute(*args[4:end])
                        execute(*args[end + 2:])
                    elif ";" in args:
                        index = args.index(";")
                        execute(*args[:index])
                        execute(*args[index + 1:])
                    elif args[0] == "set-option":
                        options[args[-2]] = expand(args[-1])

                # Execute only the rename queue's if-shell/set-option subset, never host tmux.
                with (
                    patch.object(cockpit, "current_target", return_value=old),
                    patch.object(cockpit, "bell_target", return_value=None),
                    patch.object(cockpit, "_option", return_value="%2"),
                    patch.object(cockpit.tmux, "tmux", side_effect=lambda *args: (
                        execute(*shlex.split(args[-1])) if expand(args[-2]) == "1" else None
                    )),
                ):
                    cockpit.rename_target(old, new)
                self.assertEqual(options[cockpit.CURRENT_TARGET_OPTION], new.format())
                self.assertEqual(options[cockpit.ATTACHMENT_TARGET_OPTION],
                                 new.format() if renamed else attachment_target.format())
                if pending_failure:
                    self.assertEqual(options[cockpit.ATTACHMENT_FAILURE_TARGET_OPTION],
                                     new.format() if renamed else failed_target.format())
                    self.assertEqual(options[cockpit.ATTACHMENT_FAILURE_GENERATION_OPTION], failed_generation)
                    with patch.object(cockpit.tmux, "out", return_value=(
                        f"%1\t%1\t{new.format()}\t\t\t"
                        f"{options[cockpit.ATTACHMENT_FAILURE_TARGET_OPTION]}\t{failed_generation}\t."
                    )):
                        status = cockpit.status_snapshot()
                    marker = sidebar._reconcile_attachment_failure(
                        marker, status.current_target, status.attachment_failure, retry, 10.1,
                    )
                    submitted = []
                    sidebar._sync_active_session(
                        new, healthy, marker, lambda _: self.fail("retried before backoff"),
                        retry=retry, now=10.2,
                    )
                    sidebar._sync_active_session(
                        new, healthy, marker, lambda effect: submitted.append(effect) or True,
                        retry=retry, now=100,
                    )
                    self.assertEqual(submitted, [sidebar.Effect("switch", new, automatic=True)] if renamed else [])
                    continue
                with patch.object(cockpit.tmux, "tmux") as command:
                    cockpit._install_right_pane_reset("%1", "%2", "C-x")
                hook = command.call_args.args[-1]
                condition = re.search(
                    r"if-shell -F '([^']+)' \{ set-option -F -t %2 @letee_attachment_failure_target", hook,
                )[1]
                options["pane_dead"] = "1"
                failure = (new, "123") if expand(condition) == "1" else None
                retry = sidebar.RestorationRetry()
                marker = sidebar._reconcile_attachment_failure(None, new, failure, retry, 10)
                submitted = []
                sidebar._sync_active_session(
                    new, healthy, marker, lambda effect: submitted.append(effect) or True,
                    retry=retry, now=100,
                )
                self.assertEqual(submitted, [sidebar.Effect("switch", new, automatic=True)] if renamed else [])

    def test_failure_is_not_consumed_again_after_explicit_navigation_or_for_other_target(self):
        target = Target("ssh", "work", "dev")
        other = Target("local", "other")
        failure = (target, "123")
        retry = sidebar.RestorationRetry()
        with patch.object(sidebar, "load_timing", return_value=config.Timing()):
            self.assertIsNone(sidebar._reconcile_attachment_failure(None, other, failure, retry, 0))
            self.assertEqual(retry.delay, 0)
            self.assertEqual(sidebar._reconcile_attachment_failure(None, target, failure, retry, 1), target)
            sidebar._reconcile_active_session_effect(
                target, sidebar.EffectResult(sidebar.Effect("switch", target), ()), retry=retry,
            )
            self.assertEqual(retry.delay, 0)
            self.assertIsNone(sidebar._reconcile_attachment_failure(None, target, failure, retry, 2))
            self.assertEqual(retry.delay, 0)

    def test_stale_automatic_failure_does_not_show_error(self):
        target = Target("local", "old")
        state = SidebarState(status="unchanged")
        result = sidebar.EffectResult(
            sidebar.Effect("switch", target, automatic=True), (),
            error="timeout", stale_navigation=True,
        )
        sidebar._apply_effect(result, state, unittest.mock.Mock(), 5)
        self.assertEqual(state.status, "unchanged")

    def test_restoration_deadline_uses_worker_completion_and_configured_delays(self):
        target = Target("local", "work")
        retry = sidebar.RestorationRetry()
        failed = sidebar.EffectResult(
            sidebar.Effect("switch", target, automatic=True), (), error="timeout", completed_at=10
        )
        with patch.object(sidebar, "load_timing", return_value=config.Timing(
            retry_initial_delay=2, retry_max_delay=3
        )):
            sidebar._reconcile_active_session_effect(target, failed, retry=retry)
            self.assertEqual(retry.deadline, 12)
            sidebar._reconcile_active_session_effect(target, failed, retry=retry)
            self.assertEqual(retry.deadline, 13)

    @patch.object(sidebar, "load_timing", return_value=config.Timing())
    def test_automatic_launch_preserves_backoff_and_stale_results_do_not_change_it(self, _load_timing):
        target = Target("local", "work")
        retry = sidebar.RestorationRetry()
        effect = sidebar.Effect("switch", target, automatic=True)
        failed = sidebar.EffectResult(effect, (), error="timeout")
        sidebar._reconcile_active_session_effect(target, failed, retry=retry, now=10)
        before = vars(retry).copy()
        sidebar._reconcile_active_session_effect(
            target, sidebar.EffectResult(effect, (), stale_navigation=True), retry=retry, now=20
        )
        self.assertEqual(vars(retry), before)
        self.assertIsNone(sidebar._reconcile_active_session_effect(
            target, sidebar.EffectResult(effect, ()), retry=retry, now=20
        ))
        sidebar._reconcile_active_session_effect(target, failed, retry=retry, now=30)
        submitted = []
        sidebar._sync_active_session(
            target, snapshot(local=("work",)), target,
            lambda effect: submitted.append(effect) or True, retry=retry, now=32,
        )
        self.assertEqual(len(submitted), 1)

    def test_target_change_discards_restoration_backoff_and_mutations_do_not_retry(self):
        target = Target("local", "work")
        other = Target("local", "other")
        retry = sidebar.RestorationRetry()
        sidebar._reconcile_active_session_effect(
            target, sidebar.EffectResult(sidebar.Effect("switch", target, automatic=True), (), error="timeout"),
            retry=retry, now=10,
        )
        self.assertIsNone(sidebar._sync_active_session(
            other, snapshot(local=("other",)), target, lambda _: True, retry=retry, now=10
        ))
        clean = vars(retry).copy()
        for kind in ("create", "kill", "rename", "kill_agent"):
            sidebar._reconcile_active_session_effect(
                None, sidebar.EffectResult(sidebar.Effect(kind, other), (), error="timeout"),
                retry=retry, now=20,
            )
            self.assertEqual(vars(retry), clean)

    def test_explicit_navigation_bypasses_restoration_backoff_and_success_clears_marker(self):
        target = Target("local", "work")
        retry = sidebar.RestorationRetry()
        sidebar._reconcile_active_session_effect(
            target, sidebar.EffectResult(sidebar.Effect("switch", target, automatic=True), (), error="timeout"),
            retry=retry, now=10,
        )
        with (
            patch.object(sidebar.cockpit, "switch") as switch,
            patch.object(sidebar.sessions, "attach_command", return_value=["tmux"]),
        ):
            result = sidebar._perform_effect(sidebar.Effect("switch", target), ())
        switch.assert_called_once()
        self.assertIsNone(sidebar._reconcile_active_session_effect(target, result, retry=retry, now=10))

    def test_only_unreachable_active_sources_require_unavailable_screen(self):
        target = Target("ssh", "work", "dev")

        self.assertEqual(sidebar._target_status(target, snapshot(remotes={"dev": None})), "connecting…")
        self.assertIsNone(sidebar._target_status(target, snapshot(remotes={"dev": source("ssh", host="dev")})))
        self.assertEqual(sidebar._target_status(target, snapshot(remotes={"dev": source("ssh", host="dev", available=False)})), "reconnecting…")

    def test_stale_absent_active_restore_does_not_replace_newer_live_session(self):
        cached_target = Target("local", "a")
        live_target = Target("local", "b")

        with (
            patch.object(sidebar.cockpit, "current_target", return_value=live_target),
            patch.object(sidebar.cockpit, "switch") as switch,
        ):
            interrupted = sidebar._sync_active_session(
                cached_target, snapshot(local=("b",)), None
            )

        self.assertIsNone(interrupted)
        switch.assert_not_called()

    def test_queued_restore_rechecks_same_target_startup_under_lock(self):
        target = Target("local", "work")
        submitted = []
        reconciler = sidebar.SessionReconciler()
        reconciler.reconcile(
            (target,), target, snapshot(),
            lambda effect: submitted.append(effect) or True, 10,
            shared_deadline=None,
        )
        self.assertEqual(submitted, [sidebar.Effect("switch", target, automatic=True)])

        def fresh_deadline(_target):
            with config.sessions_lock(blocking=False) as acquired:
                self.assertFalse(acquired)
            return 20

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(config, "config_dir", return_value=Path(directory)),
            patch.object(sidebar.cockpit, "current_target", return_value=target),
            patch.object(sidebar.cockpit, "startup_deadline", side_effect=fresh_deadline),
            patch.object(sidebar.time, "monotonic", return_value=11),
            patch.object(sidebar.sessions, "attach_command") as prepare,
            patch.object(sidebar.cockpit, "switch") as switch,
        ):
            result = sidebar._perform_effect(submitted[0], (target,))

        self.assertTrue(result.stale_navigation)
        prepare.assert_not_called()
        switch.assert_not_called()

    def test_matching_live_absent_target_is_restored_immediately(self):
        target = Target("local", "a")

        with (
            patch.object(sidebar.cockpit, "current_target", return_value=target),
            patch.object(sidebar.sessions, "attach_command", return_value="attach"),
            patch.object(sidebar.cockpit, "switch") as switch,
        ):
            interrupted = sidebar._sync_active_session(target, snapshot(local=("b",)), None)

        self.assertIsNone(interrupted)
        switch.assert_called_once_with(target, "attach", launch_id=ANY)

    def test_stale_reconnecting_effect_is_dropped(self):
        cached_target = Target("ssh", "a", "dev")
        live_target = Target("local", "b")

        with (
            patch.object(sidebar.cockpit, "current_target", return_value=live_target),
            patch.object(sidebar.cockpit, "show_reconnecting") as show_reconnecting,
        ):
            interrupted = sidebar._sync_active_session(
                cached_target,
                snapshot(remotes={"dev": source("ssh", host="dev", available=False)}),
                None,
            )

        self.assertIsNone(interrupted)
        show_reconnecting.assert_not_called()

    def test_stale_unavailable_effect_is_dropped(self):
        cached_target = Target("local", "a")
        live_target = Target("local", "b")

        with (
            patch.object(sidebar.cockpit, "current_target", return_value=live_target),
            patch.object(sidebar.cockpit, "show_unavailable") as show_unavailable,
        ):
            interrupted = sidebar._sync_active_session(
                cached_target, snapshot(local_available=False), None
            )

        self.assertIsNone(interrupted)
        show_unavailable.assert_not_called()

    def test_stale_restore_does_not_switch_back_to_cached_session(self):
        cached_target = Target("local", "a")
        live_target = Target("local", "b")

        with (
            patch.object(sidebar.cockpit, "current_target", return_value=live_target),
            patch.object(sidebar.cockpit, "switch") as switch,
        ):
            interrupted = sidebar._sync_active_session(
                cached_target, snapshot(local=("a",)), cached_target
            )

        self.assertEqual(interrupted, cached_target)
        switch.assert_not_called()

    def test_stale_restore_result_does_not_update_sidebar_or_poller_target(self):
        cached_target = Target("local", "a")
        live_target = Target("local", "b")
        state = SidebarState(selected_target=live_target)
        poller = unittest.mock.Mock(launch_id=None, current_target=live_target)

        with (
            patch.object(sidebar.cockpit, "current_target", return_value=live_target),
            patch.object(sidebar.cockpit, "switch") as switch,
        ):
            result = sidebar._perform_effect(
                Effect("switch", cached_target, automatic=True), ()
            )

        self.assertTrue(result.stale_navigation)
        switch.assert_not_called()
        sidebar._apply_effect(result, state, poller, 5)
        poller.observe_effect(result)
        self.assertEqual(state.selected_target, live_target)
        self.assertEqual(poller.current_target, live_target)

    def test_navigation_during_restore_preparation_does_not_respawn_stale_target(self):
        target = Target("local", "cached")
        live_target = Target("local", "live")

        for automatic in (True, False):
            with self.subTest(automatic=automatic):
                active = target

                def prepare(_target, **kwargs):
                    nonlocal active
                    active = live_target
                    return "prepared attach"

                with (
                    patch.object(sidebar.cockpit, "current_target", side_effect=lambda: active),
                    patch.object(sidebar.cockpit, "startup_deadline", return_value=None),
                    patch.object(sidebar.cockpit, "right_pane", return_value="%2"),
                    patch.object(sidebar.cockpit.tmux, "tmux") as tmux_command,
                    patch.object(sidebar.sessions, "attach_command", side_effect=prepare),
                ):
                    result = sidebar._perform_effect(
                        Effect("switch", target, automatic=automatic), ()
                    )

                self.assertEqual(result.stale_navigation, automatic)
                self.assertFalse(result.error)
                if automatic:
                    tmux_command.assert_not_called()
                else:
                    respawns = [c.args[:5] for c in tmux_command.call_args_list if c.args[0] == "respawn-pane"]
                    self.assertEqual(len(respawns), 2)
                    self.assertIn("Switching session", respawns[0][4])
                    self.assertEqual(respawns[1], ("respawn-pane", "-k", "-t", "%2", "prepared attach"))

    def test_queued_stale_absent_restore_does_not_mark_target(self):
        target = Target("local", "cached")
        live_target = Target("local", "live")
        submitted = []

        marker = sidebar._sync_active_session(
            target,
            snapshot(local=("live",)),
            None,
            lambda effect: submitted.append(effect) or True,
        )
        self.assertIsNone(marker)
        effect = submitted[0]

        with patch.object(sidebar.cockpit, "current_target", return_value=live_target):
            result = sidebar._perform_effect(effect, ())

        self.assertTrue(result.stale_navigation)
        self.assertIsNone(sidebar._reconcile_active_session_effect(marker, result))
        self.assertIsNone(
            sidebar._reconcile_active_session_effect(marker, sidebar.EffectResult(effect, ()))
        )

    def test_queued_stale_restore_does_not_clear_target_marker(self):
        target = Target("local", "cached")
        live_target = Target("local", "live")
        submitted = []

        marker = sidebar._sync_active_session(
            target,
            snapshot(local=("cached",)),
            target,
            lambda effect: submitted.append(effect) or True,
        )
        self.assertEqual(marker, target)
        effect = submitted[0]

        with patch.object(sidebar.cockpit, "current_target", return_value=live_target):
            result = sidebar._perform_effect(effect, ())

        self.assertTrue(result.stale_navigation)
        self.assertEqual(
            sidebar._reconcile_active_session_effect(marker, result),
            target,
        )
        self.assertIsNone(
            sidebar._reconcile_active_session_effect(
                marker, sidebar.EffectResult(effect, ())
            )
        )

    def test_failed_navigation_marks_target_for_retry(self):
        target = Target("ssh", "work", "dev")

        for kind in ("switch", "add_switch"):
            with self.subTest(kind=kind):
                result = sidebar.EffectResult(
                    sidebar.Effect(kind, target), (), error="ssh failed"
                )
                self.assertEqual(
                    sidebar._reconcile_active_session_effect(None, result), target
                )

    def test_failed_switch_pane_marks_host_session_for_retry(self):
        target = Target("ssh", "work", "dev")
        pane = PaneTarget(target, "@1", "%1", "/tmp/tmux-1000/letee", "editor")
        result = sidebar.EffectResult(
            sidebar.Effect("switch_pane", pane), (), error="ssh failed"
        )

        self.assertEqual(sidebar._reconcile_active_session_effect(None, result), target)

    def test_failed_kill_leaves_marker_untouched(self):
        shown = Target("ssh", "work", "dev")
        result = sidebar.EffectResult(
            sidebar.Effect("kill", Target("ssh", "other", "dev")), (),
            error="ssh failed",
        )

        self.assertEqual(
            sidebar._reconcile_active_session_effect(shown, result), shown
        )

    def test_active_absent_session_restores_once_before_discovery_catches_up(self):
        target = Target("local", "work")

        with (
            patch.object(sidebar.cockpit, "current_target", return_value=target),
            patch.object(sidebar.sessions, "attach_command", return_value="attach"),
            patch.object(sidebar.cockpit, "switch") as switch,
        ):
            pending = sidebar._sync_active_session(target, snapshot(local=("other",)), None)
            restored = sidebar._sync_active_session(target, snapshot(local=("work",)), pending)

        switch.assert_called_once_with(target, "attach", launch_id=ANY)
        self.assertIsNone(pending)
        self.assertIsNone(restored)

    def test_active_session_shows_reconnecting_then_restores_session(self):
        target = Target("ssh", "work", "dev")

        with (
            patch.object(sidebar.cockpit, "current_target", return_value=target),
            patch.object(sidebar.cockpit, "show_reconnecting") as show_reconnecting,
            patch.object(sidebar.cockpit, "switch") as switch,
        ):
            pending = sidebar._sync_active_session(
                target, snapshot(remotes={"dev": source("ssh", host="dev", available=False)}), None
            )
            restored = sidebar._sync_active_session(
                target, snapshot(remotes={"dev": source("ssh", sessions=("work",), host="dev")}), pending
            )

        show_reconnecting.assert_called_once_with(target)
        switch.assert_called_once_with(target, sidebar.sessions.attach_command(target, attach_only=True), launch_id=ANY)
        self.assertEqual(pending, target)
        self.assertIsNone(restored)

    def test_normal_remote_switch_does_not_wait_for_overlay_ssh(self):
        target = Target("ssh", "work", "dev")
        with (
            patch.object(sidebar.cockpit, "right_pane", return_value="%2"),
            patch.object(sidebar.cockpit.tmux, "tmux") as tmux_call,
            patch.object(sidebar.cockpit, "startup_deadline", return_value=None),
            patch.object(sidebar.sessions, "load_tmux_config_overlay", return_value=True),
            patch.object(sidebar.sessions, "load_persistent_ssh", return_value=True),
            patch.object(sidebar.sessions.subprocess, "run", side_effect=AssertionError("blocking SSH")) as run,
        ):
            result = sidebar._perform_effect(sidebar.Effect("switch", target), ())
        self.assertFalse(result.error)
        run.assert_not_called()
        respawns = [item.args for item in tmux_call.call_args_list if item.args[0] == "respawn-pane"]
        self.assertEqual(len(respawns), 2)
        self.assertIn("Switching session", respawns[0][4])
        self.assertEqual(shlex.split(respawns[1][4])[:6], [sys.executable, "-m", "letee.attachment", target.format(), "--", "ssh"])
        self.assertEqual(tmux_call.call_args.args, ("select-pane", "-t", "%2"))

    def test_absent_remote_preparation_timeout_keeps_existing_pane_until_success(self):
        target = Target("ssh", "work", "dev")
        data = snapshot(remotes={"dev": source("ssh", host="dev")})
        retry = sidebar.RestorationRetry()
        submitted = []
        timeout = subprocess.TimeoutExpired(["ssh"], 3)
        with (
            patch.object(sidebar.cockpit, "current_target", return_value=target),
            patch.object(sidebar.cockpit, "show_reconnecting") as show_reconnecting,
            patch.object(sidebar.cockpit, "show_unavailable") as show_unavailable,
            patch.object(sidebar.cockpit, "switch") as switch,
            patch.object(sidebar.sessions, "attach_command", side_effect=[timeout, "attach"]),
        ):
            sidebar._sync_active_session(
                target, data, None, lambda effect: submitted.append(effect) or True, retry=retry, now=0,
            )
            result = sidebar._perform_effect(submitted.pop(), ())
            self.assertTrue(result.error)
            marker = sidebar._reconcile_active_session_effect(None, result, retry=retry, now=10)
            switch.assert_not_called()
            sidebar._sync_active_session(
                target, data, marker, lambda effect: submitted.append(effect) or True,
                retry=retry, now=retry.deadline,
            )
            result = sidebar._perform_effect(submitted.pop(), ())
            self.assertFalse(result.error)
            self.assertIsNone(sidebar._reconcile_active_session_effect(marker, result, retry=retry))
            show_reconnecting.assert_not_called()
            show_unavailable.assert_not_called()
            switch.assert_called_once_with(target, "attach", launch_id=ANY)

    def test_failed_switch_retries_once_snapshot_is_healthy(self):
        target = Target("ssh", "work", "dev")
        failed = sidebar.EffectResult(
            sidebar.Effect("switch", target, automatic=True), (), error="ssh failed"
        )
        pending = sidebar._reconcile_active_session_effect(None, failed)

        with (
            patch.object(sidebar.cockpit, "current_target", return_value=target),
            patch.object(sidebar.cockpit, "switch") as switch,
        ):
            restored = sidebar._sync_active_session(
                target,
                snapshot(remotes={"dev": source("ssh", sessions=("work",), host="dev")}),
                pending,
            )

        switch.assert_called_once_with(target, sidebar.sessions.attach_command(target, attach_only=True), launch_id=ANY)
        self.assertEqual(pending, target)
        self.assertIsNone(restored)

    def test_reconnect_restore_failure_keeps_sidebar_running(self):
        target = Target("ssh", "work", "dev")
        connected = snapshot(remotes={"dev": source("ssh", sessions=("work",), host="dev")})
        disconnected = snapshot(
            remotes={"dev": source("ssh", sessions=("work",), host="dev", available=False)}
        )
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=connected,
            current_target=target,
            bell_target=None,
            current_agent=None,
            pane_active=True,
            startup_deadline=None,
        )
        snapshots = iter((disconnected, connected, connected))

        def tick(_now):
            poller.snapshot = next(snapshots, poller.snapshot)
            return True

        poller.tick.side_effect = tick
        results = []
        actions = unittest.mock.Mock(busy=False)
        actions.submit.side_effect = lambda effect, favorites: results.append(
            sidebar._perform_effect(effect, favorites)
        ) or True
        actions.poll.side_effect = lambda: results.pop(0) if results else None
        screen = FakeScreen([-1, -1, -1, -1, STOP], size=(12, 40))

        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "EffectRunner", return_value=actions),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[target]),
            patch.object(sidebar.cockpit, "current_target", return_value=target),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", return_value=(2, None)) as draw,
            patch.object(sidebar, "_bell_targets", return_value=set()),
            patch.object(sidebar.cockpit, "show_reconnecting"),
            patch.object(sidebar.cockpit, "switch", side_effect=OSError("tmux unavailable")) as switch,
        ):
            sidebar.run(screen)

        switch.assert_called_once_with(target, sidebar.sessions.attach_command(target, attach_only=True), launch_id=ANY)
        self.assertEqual(draw.call_args_list[-1].args[3], "tmux unavailable")

    def test_production_loop_restores_reconnected_active_ssh_session_without_input(self):
        target = Target("ssh", "work", "dev")
        connected = snapshot(remotes={"dev": source("ssh", sessions=("work",), host="dev")})
        disconnected = snapshot(
            remotes={"dev": source("ssh", sessions=("work",), host="dev", available=False)}
        )
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=connected,
            current_target=target,
            bell_target=None,
            current_agent=None,
            pane_active=True,
        )
        snapshots = iter((disconnected, connected, connected))

        def tick(_now):
            poller.snapshot = next(snapshots, poller.snapshot)
            return True

        poller.tick.side_effect = tick
        results = []
        actions = unittest.mock.Mock(busy=False)
        actions.submit.side_effect = lambda effect, favorites: results.append(
            sidebar._perform_effect(effect, favorites)
        ) or True
        actions.poll.side_effect = lambda: results.pop(0) if results else None
        screen = FakeScreen([-1, -1, -1, -1, -1, STOP], size=(12, 40))
        events = []
        poller.startup_deadline = None

        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "EffectRunner", return_value=actions),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[target]),
            patch.object(sidebar.cockpit, "current_target", return_value=target),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", return_value=(2, None)),
            patch.object(sidebar, "_bell_targets", return_value=set()),
            patch.object(sidebar.cockpit, "show_reconnecting", side_effect=lambda _: events.append("reconnecting")),
            patch.object(sidebar.cockpit, "switch", side_effect=lambda *_, **kwargs: events.append("switch")) as switch,
        ):
            sidebar.run(screen)

        self.assertEqual(events, ["reconnecting", "switch"])
        switch.assert_called_once_with(target, sidebar.sessions.attach_command(target, attach_only=True), launch_id=ANY)
        observed = [call.args[0].effect for call in poller.observe_effect.call_args_list]
        self.assertEqual(
            observed,
            [
                Effect("show_reconnecting", target, automatic=True),
                Effect("switch", target, automatic=True),
            ],
        )

    def test_production_loop_retries_asynchronous_failures_without_discovery_loss(self):
        target = Target("ssh", "work", "dev")
        healthy = snapshot(remotes={"dev": source("ssh", ("work",), host="dev")})
        disconnected = snapshot(remotes={"dev": source("ssh", host="dev", available=False)})
        poller = unittest.mock.Mock(
            snapshot=disconnected, current_target=target, bell_target=None, current_agent=None,
            pane_active=True, status_sampled=True, refresh_pending=False, attachment_failure=None,
            startup_deadline=None, launch_id=operations.launch_identity(),
        )
        clock = [0.0]
        launches = []
        ticks = []

        def tick(now):
            ticks.append(now)
            poller.snapshot = disconnected if len(ticks) == 1 else healthy
            poller.attachment_failure = (target, str(len(launches))) if 0 < len(launches) < 3 else None
            poller.attachment_failed = poller.attachment_failure is not None
            poller.launch_id = operations.launch_identity()
            return True

        def launch(*_args, **_kwargs):
            launches.append(clock[0])

        def monotonic():
            clock[0] += 0.1
            return clock[0]

        poller.tick.side_effect = tick
        results = []
        actions = unittest.mock.Mock(busy=False)
        actions.submit.side_effect = lambda effect, favorites: results.append(
            sidebar._perform_effect(effect, favorites)
        ) or True
        actions.poll.side_effect = lambda: results.pop(0) if results else None
        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "EffectRunner", return_value=actions),
            patch.object(sidebar.time, "monotonic", side_effect=monotonic),
            patch.object(sidebar, "load_timing", return_value=config.Timing()),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[target]),
            patch.object(sidebar.cockpit, "current_target", return_value=target),
            patch.object(sidebar.cockpit, "startup_deadline", return_value=None),
            patch.object(sidebar.sessions, "ensure_created"),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", return_value=(2, None)),
            patch.object(sidebar, "_bell_targets", return_value=set()),
            patch.object(sidebar.sessions, "attach_command", return_value="ssh dev attach"),
            patch.object(sidebar.cockpit, "show_reconnecting"),
            patch.object(sidebar.cockpit, "switch", side_effect=launch),
        ):
            sidebar.run(FakeScreen([*([-1] * 100), STOP], size=(12, 40)))
        self.assertEqual(len(launches), 3, (launches, ticks))
        self.assertGreaterEqual(launches[1] - launches[0], 1)
        self.assertGreaterEqual(launches[2] - launches[1], 2)
        self.assertIs(poller.snapshot, healthy)

    def test_failed_manual_switch_from_other_target_waits_for_status_sample(self):
        old = Target("local", "old")
        target = Target("local", "work")
        data = snapshot(local=("old", "work"))
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=data,
            current_target=old,
            bell_target=None,
            current_agent=None,
            pane_active=True,
            startup_deadline=None,
            status_sampled=False,
        )
        samples = iter((False, False, False, True, False))

        def tick(_now):
            poller.status_sampled = next(samples, False)
            if poller.status_sampled:
                poller.current_target = target
            return poller.status_sampled

        poller.tick.side_effect = tick
        results = []
        submitted = []
        actions = unittest.mock.Mock(busy=False)

        def submit(effect, favorites):
            submitted.append(effect)
            results.append(sidebar.EffectResult(
                effect,
                tuple(favorites),
                error="ssh failed" if len(submitted) == 1 else None,
            ))
            return True

        actions.submit.side_effect = submit
        actions.poll.side_effect = lambda: results.pop(0) if results else None
        screen = FakeScreen([curses.KEY_DOWN, curses.KEY_ENTER, -1, -1, -1, STOP])

        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "EffectRunner", return_value=actions),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[old, target]),
            patch.object(sidebar.cockpit, "current_target", return_value=old),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", return_value=(2, None)),
            patch.object(sidebar, "_bell_targets", return_value=set()),
        ):
            sidebar.run(screen)

        self.assertEqual(
            submitted,
            [
                Effect("switch", target),
                Effect("switch", target, automatic=True),
            ],
        )

    def test_busy_action_delays_automatic_restoration_until_runner_is_idle(self):
        target = Target("ssh", "work", "dev")
        connected = snapshot(remotes={"dev": source("ssh", sessions=("work",), host="dev")})
        disconnected = snapshot(
            remotes={"dev": source("ssh", sessions=("work",), host="dev", available=False)}
        )
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=connected,
            current_target=target,
            bell_target=None,
            current_agent=None,
            pane_active=True,
        )
        snapshots = iter((disconnected, connected, connected))

        def tick(_now):
            poller.snapshot = next(snapshots, poller.snapshot)
            return True

        poller.tick.side_effect = tick

        class BusyRunner:
            def __init__(self):
                self.busy = False
                self.poll_count = 0
                self.results = []

            def submit(self, effect, favorites):
                self.results.append(sidebar._perform_effect(effect, favorites))
                return True

            def poll(self):
                self.poll_count += 1
                self.busy = self.poll_count == 2
                return self.results.pop(0) if self.results else None

            def close(self):
                pass

        actions = BusyRunner()
        poller.startup_deadline = None
        screen = FakeScreen([-1, -1, -1, -1, -1, STOP], size=(12, 40))
        events = []

        def switch(*_args, **kwargs):
            events.append(("switch", actions.busy))

        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "EffectRunner", return_value=actions),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[target]),
            patch.object(sidebar.cockpit, "current_target", return_value=target),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", return_value=(2, None)),
            patch.object(sidebar, "_bell_targets", return_value=set()),
            patch.object(sidebar.cockpit, "show_reconnecting", side_effect=lambda _: events.append(("reconnecting", actions.busy))),
            patch.object(sidebar.cockpit, "switch", side_effect=switch),
        ):
            sidebar.run(screen)

        self.assertEqual(events, [("reconnecting", False), ("switch", False)])


from letee.sidebar import (
    Effect,
    Entry,
    SidebarState,
    _agent_entries,
    _agent_sort_key,
    _bell_targets,
    _draw,
    _entry_height,
    _fade,
    _entries,
    _entry_at_row,
    _entry_attr,
    _entry_lines,
    _init_colors,
    _mouse_activates,
    _mouse_mask,
    _read_key,
    _reset_selection,
    _rename_key,
    _search_key,
    _selected_index,
    _start_rename,
    _sync_agent_selection,
    _sync_selection,
    _transition,
    _execute,
    _update_agent_alerts,
    _viewport,
    main,
    run as sidebar_run,
)


STOP = object()


class FakeScreen:
    def __init__(self, keys=None, size=(5, 20)):
        self.calls = []
        self.keys = list(keys or [])
        self.key = ord("y")
        self.timeout_value = None
        self._letee_test_stop = STOP
        self.size = size

    def erase(self):
        self.calls.append(("erase",))

    def clear(self):
        self.calls.append(("clear",))

    def getmaxyx(self):
        return self.size

    def addnstr(self, *args):
        self.calls.append(("addnstr", *args))

    def addstr(self, *args):
        self.calls.append(("addstr", *args))

    def chgat(self, *args):
        self.calls.append(("chgat", *args))

    def attron(self, *args):
        self.calls.append(("attron", *args))

    def attroff(self, *args):
        self.calls.append(("attroff", *args))

    def getch(self):
        self.calls.append(("getch",))
        if self.keys:
            return self.keys.pop(0)
        return -1 if self.timeout_value == 0 else self.key

    def redrawln(self, *args):
        self.calls.append(("redrawln", *args))

    def refresh(self):
        self.calls.append(("refresh",))

    def move(self, *args):
        self.calls.append(("move", *args))

    def timeout(self, *args):
        self.timeout_value = args[0]
        self.calls.append(("timeout", *args))


def run(stdscr):
    sidebar_run(stdscr)


class CompactSidebarTest(OperationTestCase):
    def _run_compact(self, keys, mouse_events=(), count=2, pane_active=True, alert=False):
        targets = [Target("local", f"session-{index}") for index in range(count)]
        agents = tuple(
            AgentEntry(PaneTarget(target, "@1", f"%{index}", "/tmp/tmux-test"), f"agent-{index}", "pi", "working" if alert and index == 0 else "idle")
            for index, target in enumerate(targets)
        )
        data = SessionSnapshot(SourceSnapshot(True, tuple(targets), frozenset(), agents=agents), {})
        screen = FakeScreen([*keys, STOP], size=(10, 5))
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=data, current_target=None, bell_target=None,
            current_agent=None, pending_agent=None, pane_active=pane_active,
        )
        poller.tick.return_value = False
        if alert:
            alerted = AgentEntry(agents[0].pane_target, "agent-0", "pi", "failed")

            def tick(_now):
                if poller.snapshot is not data:
                    return False
                poller.snapshot = SessionSnapshot(
                    SourceSnapshot(True, tuple(targets), frozenset(), agents=(alerted, *agents[1:])), {},
                )
                return True

            poller.tick.side_effect = tick
        actions = unittest.mock.Mock(busy=False, blocks_favorite_changes=False)
        actions.submit.return_value = True
        actions.poll.return_value = None
        compact = True
        draws = []
        draw_signature = inspect.signature(sidebar._draw)

        def draw_spy(*args, **kwargs):
            bound = draw_signature.bind(*args, **kwargs)
            bound.apply_defaults()
            draws.append(dict(bound.arguments))
            return 2, None

        def expand(**_kwargs):
            nonlocal compact
            compact = False
            screen.size = (10, 40)

        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "DiscoveryPoller"),
            patch.object(sidebar, "EffectRunner", return_value=actions),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=targets),
            patch.object(sidebar, "load_sidebar_keybindings", return_value=config.DEFAULT_SIDEBAR_KEYBINDINGS),
            patch.object(sidebar.cockpit, "current_target", return_value=None),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar, "_mouse_cleanup"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar.curses, "beep"),
            patch.object(sidebar.curses, "getmouse", side_effect=mouse_events),
            patch.object(sidebar, "_draw", side_effect=draw_spy),
            patch.object(sidebar, "_bell_targets", return_value=set()),
            patch.object(sidebar.cockpit, "sidebar_compact", side_effect=lambda: compact) as mode_lookup,
            patch.object(sidebar.cockpit, "expand_sidebar", side_effect=expand) as expansion,
            patch.object(sidebar.cockpit, "show_session_menu") as session_menu,
            patch.object(sidebar.cockpit, "show_agent_menu") as agent_menu,
            patch.object(sidebar.cockpit.tmux, "out", return_value=""),
        ):
            run(screen)

        self.assertEqual(mode_lookup.call_count, 1 + keys.count(curses.KEY_RESIZE))
        effects = [item.args[0] for item in actions.submit.call_args_list]
        return targets, agents, draws, effects, expansion, session_menu, agent_menu

    def test_session_and_agent_clicks_stay_compact_including_handle_column(self):
        for column in (0, 2):
            with self.subTest(column=column):
                targets, agents, draws, effects, expansion, _, _ = self._run_compact(
                    [curses.KEY_MOUSE, curses.KEY_MOUSE],
                    [(0, column, 2, 0, curses.BUTTON1_PRESSED),
                     (0, column, 8, 0, curses.BUTTON1_PRESSED)],
                )
                self.assertEqual(effects, [
                    Effect("switch", targets[1]),
                    Effect("switch_pane", agents[1].pane_target, message="agent-1"),
                ])
                expansion.assert_not_called()
                self.assertTrue(all(draw["compact"] for draw in draws))
                self.assertTrue(all(draw["move_source_entry"] is None for draw in draws))
                self.assertEqual([entry.target for entry in draws[-1]["entries"] if entry.kind == "session"], targets)

    def test_wheel_scroll_in_each_section_maps_clicks_to_displayed_targets(self):
        for kind, row in (("session", 1), ("agent", 7)):
            with self.subTest(kind=kind):
                targets, agents, draws, effects, expansion, _, _ = self._run_compact(
                    [curses.KEY_MOUSE] * 4,
                    [(0, 0, row, 0, curses.BUTTON5_PRESSED),
                     (0, 0, row, 0, curses.BUTTON5_PRESSED),
                     (0, 0, row, 0, curses.BUTTON4_PRESSED),
                     (0, 0, row, 0, curses.BUTTON1_PRESSED)],
                    count=8,
                )
                expected = Effect("switch", targets[1]) if kind == "session" else Effect("switch_pane", agents[1].pane_target, message="agent-1")
                self.assertEqual(effects, [expected])
                offset = "scroll_offset" if kind == "session" else "agent_scroll_offset"
                self.assertIn(1, [draw[offset] for draw in draws])
                expansion.assert_not_called()
                self.assertTrue(all(draw["compact"] for draw in draws))

    def test_top_button_expands_and_focuses_right_without_switching(self):
        _, _, draws, effects, expansion, _, _ = self._run_compact(
            [curses.KEY_MOUSE], [(0, 2, 0, 0, curses.BUTTON1_PRESSED)],
        )
        expansion.assert_called_once_with(focus_right=True)
        self.assertEqual(effects, [])
        self.assertFalse(draws[-1]["compact"])

    def test_right_click_expands_menu_for_exact_scrolled_target(self):
        for kind, row in (("session", 1), ("agent", 7)):
            with self.subTest(kind=kind):
                targets, agents, draws, effects, expansion, session_menu, agent_menu = self._run_compact(
                    [curses.KEY_MOUSE, curses.KEY_MOUSE],
                    [(0, 0, row, 0, curses.BUTTON5_PRESSED),
                     (0, 2, row, 0, curses.BUTTON3_PRESSED)],
                    count=8,
                )
                expansion.assert_called_once_with()
                self.assertEqual(effects, [])
                self.assertFalse(draws[-1]["compact"])
                if kind == "session":
                    session_menu.assert_called_once_with(targets[1], 2, row, config.DEFAULT_SIDEBAR_KEYBINDINGS)
                    agent_menu.assert_not_called()
                    self.assertEqual(draws[-1]["entries"][draws[-1]["selected"]].target, targets[1])
                else:
                    agent_menu.assert_called_once_with("pi", agents[1].pane_target, 2, row, config.DEFAULT_SIDEBAR_KEYBINDINGS)
                    session_menu.assert_not_called()
                    self.assertEqual(draws[-1]["agent_entries"][draws[-1]["agent_selected"]].pane_target, agents[1].pane_target)

    def test_rename_and_kill_expand_without_losing_selected_session(self):
        for key, following, kind in (
            (ord("e"), [ord("z"), curses.KEY_ENTER], "rename"),
            (ord("x"), [ord("y")], "kill"),
        ):
            with self.subTest(kind=kind):
                targets, _, draws, effects, expansion, _, _ = self._run_compact(
                    [curses.KEY_DOWN, key, *following],
                )
                expansion.assert_called_once_with()
                expected = Effect("rename", targets[1], message="session-1z") if kind == "rename" else Effect("kill", targets[1])
                self.assertEqual(effects, [expected])
                self.assertFalse(draws[-1]["compact"])

    def test_agent_kill_expands_without_losing_selected_pane(self):
        _, agents, draws, effects, expansion, _, _ = self._run_compact(
            [curses.KEY_F7, curses.KEY_DOWN, curses.KEY_DOWN, ord("x"), ord("y")],
        )
        expansion.assert_called_once_with()
        self.assertEqual(effects, [Effect("kill_agent", agents[1].pane_target, message="pi", agent_id="agent-1")])
        self.assertFalse(draws[-1]["compact"])

    def test_background_prefix_agent_focus_then_alert_or_next_stays_compact(self):
        for key in (curses.KEY_F10, curses.KEY_F5):
            with self.subTest(key=key):
                _, agents, draws, effects, expansion, _, _ = self._run_compact(
                    [curses.KEY_F7, key], pane_active=False, alert=key == curses.KEY_F10,
                )
                expansion.assert_not_called()
                self.assertTrue(all(draw["compact"] for draw in draws))
                self.assertEqual(draws[-1]["focused_region"], "agents")
                self.assertEqual(effects, [Effect("switch_pane", agents[0].pane_target, message="agent-0")])

    def test_no_attention_status_notifies_once_without_expanding_or_focusing(self):
        with patch.object(sidebar.cockpit.tmux, "tmux") as tmux_command:
            _, _, draws, effects, expansion, _, _ = self._run_compact(
                [curses.KEY_F10, curses.KEY_RESIZE, curses.KEY_RESIZE],
                pane_active=False,
            )
        tmux_command.assert_called_once_with(
            "display-message", "-t", sidebar.cockpit.TARGET,
            "no agents need attention", check=False,
        )
        expansion.assert_not_called()
        self.assertEqual(effects, [])
        self.assertTrue(all(draw["compact"] for draw in draws))
        self.assertTrue(all(not draw["pane_active"] for draw in draws))
        self.assertGreaterEqual(
            sum(draw["status"] == "no agents need attention" for draw in draws), 2,
        )

    def test_status_first_labels_and_alerts(self):
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%1", "/tmp/tmux-test")
        for ascii_mode in (False, True):
            with patch.object(sidebar, "_ascii", return_value=ascii_mode):
                agent = sidebar.Entry("Pi agent", "agent", target, pane_target=pane, agent_id="a", status="working")
                self.assertEqual(sidebar._compact_line(agent, 1, set(), target, {(pane, "a")}, "/"), "/ Pi!")
                wide = sidebar.Entry("界面", "agent", target, pane_target=pane, status="completed")
                self.assertEqual(sidebar._compact_line(wide, 1, set(), target, set(), None), ("+" if ascii_mode else "✓") + " 界 ")
                self.assertEqual(sidebar._compact_line(sidebar.Entry("e\u0301cole", "agent", status="idle"), 1, set(), target, set(), None), "o e\u0301c " if ascii_mode else "○ e\u0301c ")

    def test_session_numbers_continue_and_active_bells_are_acknowledged(self):
        target = Target("local", "work")
        entry = sidebar.Entry("work", "session", target, tracked=True)
        self.assertEqual(sidebar._compact_line(entry, 12, {target}, None, set(), None), "12  !")
        self.assertEqual(sidebar._compact_line(entry, 12, {target}, target, set(), None), "12   ")

    def test_geometry_filters_controls_and_maps_one_row_entries(self):
        entries = [sidebar.Entry("empty", "empty"), sidebar.Entry("one", "session"), sidebar.Entry("two", "session")]
        rows = sidebar._compact_rows(entries, "session", 2, 0, 3, 5)
        self.assertEqual(rows, [(1, 3), (2, 4)])
        self.assertEqual(sidebar._compact_rows(entries, "session", 2, 1, 3, 4), [(2, 3)])
        self.assertEqual(sidebar._compact_rows([], "agent", 0, None, 1, 3), [])
        self.assertEqual(sidebar._compact_layout(10, 40), (1, 6, 10))

    def test_draw_preserves_alert_after_combining_mark(self):
        screen = FakeScreen(size=(10, 5))
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%1", "/tmp/tmux-test")
        agent = sidebar.Entry("e\u0301cole", "agent", target, pane_target=pane, agent_id="a", status="idle")
        with patch.object(sidebar, "_color", return_value=0), patch.object(sidebar, "_ascii", return_value=False):
            sidebar._draw(screen, [], 0, "", "", agent_entries=[agent], agent_alerts={(pane, "a")}, compact=True)
        row = next(call for call in screen.calls if call[0] == "addnstr" and call[3].startswith("○"))
        rendered = row[3][:row[4]]
        self.assertEqual(rendered, "○ e\u0301c!")
        self.assertEqual(sidebar._cell_width(rendered), 5)

    def test_tiny_panes_tolerate_ncurses_bottom_right_error(self):
        for height in (1, 2, 10):
            screen = FakeScreen(size=(height, 5))
            addnstr = screen.addnstr

            def write(row, column, text, width, attr):
                addnstr(row, column, text, width, attr)
                if row == height - 1:
                    raise curses.error("bottom right")

            screen.addnstr = write
            with patch.object(sidebar, "_color", return_value=0):
                sidebar._draw(screen, [], 0, "", "", agent_entries=[sidebar.Entry("Pi", "agent")], compact=True)

    def test_section_allocation_keeps_both_sections_at_percentage_limits(self):
        for percentage in (0, 100):
            top, separator, bottom = sidebar._compact_layout(10, percentage)
            self.assertGreater(separator, top)
            self.assertGreater(bottom, separator + 1)

    def test_draw_only_highlights_exact_active_pane_with_shared_agent_id(self):
        target = Target("local", "work")
        panes = [PaneTarget(target, "@1", f"%{i}", "/tmp/tmux") for i in range(2)]
        agents = [
            sidebar.Entry("Pi", "agent", target, pane_target=pane, agent_id="shared", status="idle")
            for pane in panes
        ]
        for active_pane in panes:
            with self.subTest(active_pane=active_pane):
                screen = FakeScreen(size=(10, 5))
                with patch.object(sidebar, "_entry_attr", side_effect=lambda entry, active: int(active)):
                    sidebar._draw(
                        screen, [], 0, "", "", agent_entries=agents,
                        active_agent_id="shared", active_agent_pane=active_pane, compact=True,
                    )
                rows = [call for call in screen.calls if call[0] == "addnstr" and "Pi" in call[3]]
                self.assertEqual([call[5] for call in rows], [int(pane == active_pane) for pane in panes])

    def test_draw_omits_controls_and_keeps_selected_spinner(self):
        screen = FakeScreen(size=(10, 5))
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%1", "/tmp/tmux-test")
        sessions = [sidebar.Entry("work", "session", target, tracked=True)]
        agents = [sidebar.Entry("", "order"), sidebar.Entry("Pi agent", "agent", target, pane_target=pane, agent_id="a", status="working")]
        with patch.object(sidebar, "_color", return_value=0), patch.object(sidebar, "_ascii", return_value=False):
            sidebar._draw(screen, sessions, 0, "", "", agent_entries=agents, agent_selected=1, active_agent_id="a", spinner_frame="⠋", compact=True)
        lines = [call[3] for call in screen.calls if call[0] == "addnstr"]
        self.assertIn("  »  ", lines)
        self.assertIn("⠋ Pi ", lines)
        self.assertIn("─────", lines)
        self.assertFalse(any("Order" in line or "AGENTS" in line or "work" in line for line in lines))


class SidebarViewModeTest(OperationTestCase):
    def test_normal_view_contains_only_ordered_sessions_and_add_action(self):
        sessions = [Target("ssh", "notes", "dev"), Target("local", "work")]

        entries = _entries(snapshot(local=("work", "other"), remotes={"dev": source("ssh", ("notes", "chat"), host="dev")}), sessions)

        self.assertEqual([entry.target for entry in entries if entry.kind == "session"], sessions)
        self.assertEqual([entry.kind for entry in entries[:2]], ["session", "session"])
        self.assertEqual(sidebar._selectable(entries)[0], 0)

    def test_empty_normal_view_shows_add_instructions(self):
        entries = _entries(snapshot(local=("work",)), [])

        self.assertEqual(
            [(entry.label, entry.kind) for entry in entries],
            [("No sessions yet", "empty"), ("Press Enter to add one.", "hint")],
        )

    def test_tracked_remote_favorite_shows_connecting_during_initial_discovery(self):
        target = Target("ssh", "work", "dev")

        entry = next(entry for entry in _entries(snapshot(remotes={"dev": None}), [target]) if entry.target == target)

        self.assertTrue(entry.unavailable_favorite)
        self.assertEqual(entry.status, "connecting…")
        self.assertIn("connecting…", _entry_lines(entry, False, set(), None, 40)[1])

    def test_tracked_remote_favorite_shows_reconnecting_after_failure(self):
        target = Target("ssh", "work", "dev")
        failed = source("ssh", host="dev", available=False, error="connection refused")

        entry = next(entry for entry in _entries(snapshot(remotes={"dev": failed}), [target]) if entry.target == target)

        self.assertTrue(entry.unavailable_favorite)
        self.assertEqual(entry.status, "reconnecting…")
        self.assertIn("reconnecting…", _entry_lines(entry, False, set(), None, 40)[1])

    def test_tracked_reachable_sessions_have_no_missing_label_when_absent(self):
        targets = (Target("local", "work"), Target("ssh", "work", "dev"))
        data = snapshot(local=("other",), remotes={"dev": source("ssh", ("other",), host="dev")})

        entries = [next(entry for entry in _entries(data, [target]) if entry.target == target) for target in targets]

        self.assertFalse(any(entry.unavailable_favorite for entry in entries))
        self.assertEqual([entry.status for entry in entries], [None, None])
        for ascii_mode in (False, True):
            with patch("letee.sidebar._ascii", return_value=ascii_mode):
                self.assertTrue(all("missing" not in line for entry in entries
                                    for line in _entry_lines(entry, False, set(), None, 40)))

    def test_tracked_unknown_remote_host_stays_unavailable(self):
        target = Target("ssh", "work", "unknown")

        entry = next(entry for entry in _entries(snapshot(), [target]) if entry.target == target)

        self.assertEqual(entry.status, "unavailable")


class ReconnectingSelectionTest(OperationTestCase):
    @patch.object(sidebar.cockpit, "startup_timeout", return_value=10)
    @patch.object(sidebar.cockpit, "right_pane", return_value="%2")
    @patch.object(time, "monotonic", return_value=5)
    def test_explicit_reconnecting_clears_new_marker_and_recovery_attaches(self, _clock, _pane, _timeout):
        target = Target("ssh", "work", "dev")
        for previous_deadline in (None, 12.0):
            with self.subTest(previous_deadline=previous_deadline):
                options = {}
                if previous_deadline is not None:
                    options[sidebar.cockpit.STARTUP_OPTION] = f"{target.format()}\t{previous_deadline}"

                def tmux_command(*args, **kwargs):
                    if args[0] == "set-option" and "-u" not in args:
                        options[args[3]] = args[4]
                    elif args[0] == "if-shell":
                        guard = sidebar.cockpit._option_equals(
                            sidebar.cockpit.STARTUP_OPTION,
                            options.get(sidebar.cockpit.STARTUP_OPTION, ""),
                        )
                        if args[2] == guard:
                            options.pop(shlex.split(args[-1])[-1], None)

                with (
                    patch.object(sidebar.cockpit, "current_target", return_value=target),
                    patch.object(sidebar.cockpit.tmux, "tmux", side_effect=tmux_command),
                    patch.object(sidebar.cockpit, "_option", side_effect=lambda name: options.get(name, "")),
                ):
                    result = sidebar._perform_effect(Effect("show_reconnecting", target), (target,))
                    self.assertFalse(result.error)
                    interrupted = sidebar._reconcile_active_session_effect(None, result)
                    self.assertEqual(interrupted, target)
                    submit = unittest.mock.Mock(return_value=True)
                    recovered = snapshot(remotes={
                        "dev": replace(source("ssh", ("work",), host="dev"), sampled_at=6),
                    })
                    sidebar._sync_active_session(
                        target, recovered, interrupted, submit, now=6,
                        shared_deadline=sidebar.cockpit.startup_deadline(target),
                    )
                    submit.assert_called_once_with(Effect("switch", target, automatic=True))
                    self.assertIsNone(sidebar.cockpit.startup_deadline(target))

    def test_enter_on_reconnecting_session_shows_progress_without_attaching(self):
        target = Target("ssh", "work", "dev")
        data = snapshot(
            remotes={"dev": source("ssh", host="dev", available=False, error="connection refused")}
        )
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=data,
            current_target=None,
            bell_target=None,
            current_agent=None,
            pane_active=True,
            startup_deadline=None,
        )
        poller.tick.return_value = False
        poller.observe_effect.side_effect = lambda result: setattr(poller, "current_target", target)
        results = []
        actions = unittest.mock.Mock(busy=False)

        def submit(effect, favorites, **_kwargs):
            results.append(sidebar._perform_effect(effect, favorites))
            return True

        actions.submit.side_effect = submit
        actions.poll.side_effect = lambda: results.pop(0) if results else None
        screen = FakeScreen([curses.KEY_ENTER, -1, STOP], size=(12, 40))

        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "EffectRunner", return_value=actions),
            patch.object(sidebar, "DiscoveryPoller"),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[target]),
            patch.object(sidebar.cockpit, "current_target", return_value=None),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", return_value=(2, None)),
            patch.object(sidebar, "_bell_targets", return_value=set()),
            patch.object(sidebar.cockpit, "switch") as switch,
            patch.object(sidebar.cockpit, "show_reconnecting") as show_reconnecting,
            patch.object(sidebar.sessions, "attach_command") as attach_command,
        ):
            run(screen)

        self.assertEqual(
            [call.args[0] for call in actions.submit.call_args_list],
            [Effect("show_reconnecting", target)],
        )
        switch.assert_called_once_with(target, sidebar.cockpit._reconnecting_command(target), launch_id=ANY)
        show_reconnecting.assert_not_called()
        attach_command.assert_not_called()

    def test_user_reconnecting_effect_updates_status_poller_target(self):
        target = Target("ssh", "work", "dev")
        discovery = unittest.mock.Mock(snapshot=snapshot())
        poller = sidebar.AsyncStatusPoller(discovery, None)
        try:
            poller.observe_effect(sidebar.EffectResult(Effect("show_reconnecting", target), ()))
            self.assertEqual(poller.current_target, target)
            self.assertIsNone(poller.current_agent)
        finally:
            poller.close()


    def test_bells_are_limited_to_tracked(self):
        tracked = Target("local", "work")
        untracked = Target("local", "notes")
        discovered = snapshot(local=("work", "notes"), local_bells=("work", "notes"))

        self.assertEqual(_bell_targets(discovered, untracked, [tracked]), {tracked})


class AgentSidebarTest(OperationTestCase):
    def test_status_icons_cover_every_state_in_unicode_and_ascii(self):
        expected = {
            False: {"working": "⠋", "submitted": "◷", "idle": "○", "completed": "✓", "input-required": "?", "auth-required": "⚿", "failed": "✕", "rejected": "⊘", "canceled": "−", "unknown": "?"},
            True: {"working": "|", "submitted": ".", "idle": "o", "completed": "+", "input-required": "?", "auth-required": "@", "failed": "x", "rejected": "!", "canceled": "-", "unknown": "?"},
        }
        for ascii_mode, icons in expected.items():
            with self.subTest(ascii=ascii_mode), patch("letee.sidebar._ascii", return_value=ascii_mode):
                self.assertEqual({status: sidebar._status_icon(status, 0) for status in icons}, icons)

    def test_agent_cursor_replaces_icon_only_while_focused(self):
        entry = Entry("pi", "agent", Target("local", "work"), status="completed")
        with patch("letee.sidebar._ascii", return_value=False):
            self.assertEqual(_entry_lines(entry, True, set(), None, 40)[0], "› pi · completed")
            self.assertEqual(_entry_lines(entry, False, set(), None, 40)[0], "✓ pi · completed")

    def test_spinner_frames_progress_and_wrap(self):
        with patch("letee.sidebar._ascii", return_value=False):
            self.assertEqual([sidebar._spinner_frame(t) for t in (0, 0.1, 0.9, 1.0)], ["⠋", "⠙", "⠏", "⠋"])
        with patch("letee.sidebar._ascii", return_value=True):
            self.assertEqual([sidebar._spinner_frame(t) for t in (0, 0.1, 0.2, 0.3, 0.4)], ["|", "/", "-", "\\", "|"])

    def test_agent_icon_and_status_share_semantic_attr_but_cursor_stays_active(self):
        entry = Entry("pi", "agent", Target("local", "work"), status="completed")
        with patch.dict("letee.sidebar._COLOR", {"active": 123, "agent_completed": 456}, clear=True):
            screen = FakeScreen(size=(6, 40))
            sidebar._draw_entries(screen, [entry], 0, 5, 40, set(), None, dimmed=True)
            semantic = [call for call in screen.calls if call[0] == "addnstr" and call[3] in ("✓", "completed")]
            self.assertEqual([call[5] for call in semantic], [_fade(456), _fade(456)])

            screen = FakeScreen(size=(6, 40))
            sidebar._draw_entries(screen, [entry], 0, 5, 40, set(), None)
            cursor = next(call for call in screen.calls if call[0] == "addnstr" and call[3] == "›")
            status = next(call for call in screen.calls if call[0] == "addnstr" and call[3] == "completed")
            self.assertEqual((cursor[5], status[5]), (456, 456))

    def test_agent_entries_only_include_exact_tracked_targets(self):
        local = Target("local", "work")
        tracked_remote = Target("ssh", "work", "dev")
        other_remote = Target("ssh", "work", "prod")
        agents = tuple(
            AgentEntry(PaneTarget(target, "@1", pane_id, "/tmp/tmux"), pane_id, "pi", None)
            for target, pane_id in ((local, "%1"), (tracked_remote, "%2"), (other_remote, "%3"))
        )
        data = SessionSnapshot(SourceSnapshot(True, (), frozenset(), agents=agents), {})

        self.assertEqual([entry.agent_id for entry in _agent_entries(data, [local, tracked_remote])], ["%1", "%2"])
        self.assertEqual(_agent_entries(data, []), [])

    def test_agent_entries_are_compact_and_keep_exact_pane(self):
        pane = PaneTarget(Target("ssh", "work", "dev"), "@1", "%2", "/tmp/tmux", "editor")
        data = SessionSnapshot(SourceSnapshot(True, (), frozenset()), {"dev": SourceSnapshot(True, (pane.target,), frozenset(), panes=(pane,), agents=(AgentEntry(pane, "id", "pi", None),))})

        entry = _agent_entries(data, [pane.target])[0]

        self.assertEqual(entry.pane_target, pane)
        self.assertEqual(entry.host, "dev")
        with patch("letee.sidebar._ascii", return_value=False):
            self.assertEqual(_entry_lines(entry, True, set(), None, 40), ["› pi · idle", "  └─ work · editor"])

    def test_agent_location_omits_host_and_truncates_safely(self):
        pane = PaneTarget(Target("ssh", "work", "dev"), "@1", "%2", "/tmp/tmux", "editor")
        entry = Entry("pi", "agent", pane.target, host="dev", pane_target=pane, agent_id="id")

        with patch("letee.sidebar._ascii", return_value=False):
            self.assertEqual(_entry_lines(entry, False, set(), None, 40)[1], "  └─ work · editor")
            lines = _entry_lines(entry, False, set(), None, 4)

        self.assertNotIn("dev", lines[1])
        self.assertTrue(all(sidebar._cell_width(line) <= 4 for line in lines))

    def test_local_agent_location_uses_window_name(self):
        pane = PaneTarget(Target("local", "work"), "@1", "%2", "/tmp/tmux", "shell")
        data = SessionSnapshot(
            SourceSnapshot(True, (pane.target,), frozenset(), agents=(AgentEntry(pane, "id", "pi", None),)),
            {},
        )

        entry = _agent_entries(data, [pane.target])[0]

        with patch("letee.sidebar._ascii", return_value=False):
            self.assertEqual(_entry_lines(entry, False, set(), None, 40)[1], "  └─ work · shell")

    def test_draw_shows_agent_divider_and_empty_state(self):
        screen = FakeScreen(size=(10, 40))

        _draw(screen, [], 0, "", "", agent_entries=[])

        text = [item[3] for item in screen.calls if item[0] == "addnstr"]
        self.assertTrue(any(line.startswith("AGENTS ") for line in text))
        self.assertIn("  No active agents", text)

    def test_empty_agent_message_stays_inside_narrow_pane(self):
        screen = FakeScreen(size=(10, 12))

        _draw(screen, [], 0, "", "", agent_entries=[])

        message = next(
            call for call in screen.calls
            if call[0] == "addnstr" and "No active" in call[3]
        )
        self.assertLessEqual(sidebar._cell_width(message[3]), 12)

    def test_agent_status_message_is_single_bounded_line(self):
        screen = FakeScreen(size=(10, 12))

        _draw(
            screen,
            [],
            0,
            "failed\n" + "界" * 20,
            "",
            agent_entries=[Entry("", "order")],
            status_region="agents",
        )

        message = next(
            call for call in screen.calls
            if call[0] == "addnstr" and "failed" in call[3]
        )
        self.assertNotIn("\n", message[3])
        self.assertLessEqual(sidebar._cell_width(message[3]), 12)

    def test_short_height_keeps_both_lines_of_first_agent_entry_visible(self):
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%1", "/tmp/tmux", "shell")
        agent = Entry("pi", "agent", target, pane_target=pane, agent_id="id", status="idle")
        screen = FakeScreen(size=(9, 40))

        _draw(screen, [], 0, "", "", agent_entries=[agent])

        text = [item[3] for item in screen.calls if item[0] == "addnstr"]
        self.assertIn("○ pi · idle", text)
        self.assertIn("  └─ work · shell", text)

    def test_agent_entry_can_be_selected_with_mouse(self):
        pane = PaneTarget(Target("local", "work"), "@1", "%2", "/tmp/tmux")
        entries = [Entry("pi", "agent", pane.target, pane_target=pane, agent_id="id")]

        self.assertEqual(_entry_at_row(entries, 0, 4, 8, 0, top=4), 0)

    def test_add_menu_entries_can_be_selected_with_mouse(self):
        for kind in ("create", "location"):
            with self.subTest(kind=kind):
                self.assertEqual(_entry_at_row([Entry("Add", kind)], 0, 2, 4, 0, top=2), 0)

    def test_switch_pane_uses_exact_attach_command_and_agent_id(self):
        pane = PaneTarget(Target("local", "work"), "@1", "%2", "/tmp/tmux")
        state = SidebarState()
        with patch("letee.sidebar.cockpit.switch") as switch:
            _execute(Effect("switch_pane", pane, message="id"), state, unittest.mock.Mock(), 5)

        _assert_deferred_switch(self, switch, pane.target, "id")
        self.assertEqual(
            switch.call_args.args[1](),
            "env -u TMUX tmux -S /tmp/tmux select-window -t work:@1 \\; select-pane -t %2 \\; attach-session -t work",
        )
        self.assertEqual(state.status, "")
        self.assertIsNone(state.status_deadline)

    def test_switch_effects_source_overlay_when_enabled(self):
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        source = shlex.quote(str(sidebar.sessions.OVERLAY_FILE))
        with (
            patch.object(sidebar.sessions, "load_tmux_config_overlay", return_value=True),
            patch("letee.sidebar.cockpit.switch") as switch,
        ):
            _execute(Effect("switch", target), SidebarState(), unittest.mock.Mock(), 5)
            _execute(Effect("switch_pane", pane, message="id"), SidebarState(), unittest.mock.Mock(), 5)
            attach, pane_attach = (call.args[1]() for call in switch.call_args_list)

        self.assertIn(f"tmux -L letee.inner -T clipboard new-session -A -s work \\; source-file {source}", attach)
        self.assertIn(f"tmux -S /tmp/tmux source-file {source} \\; select-window -t work:@1", pane_attach)

    def test_kill_agent_refreshes_discovery_without_changing_favorites(self):
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        state = SidebarState(
            favorites=[target],
            selected_target=target,
            selected_agent_key=(pane, "id"),
        )
        poller = unittest.mock.Mock(launch_id=None, )

        with patch.object(sidebar.sessions, "kill_agent") as kill_agent:
            _execute(Effect("kill_agent", pane, message="pi"), state, poller, 5)

        kill_agent.assert_called_once_with(pane)
        poller.refresh.assert_called_once_with()
        self.assertEqual(state.favorites, [target])
        self.assertEqual(state.selected_target, target)
        self.assertEqual(state.selected_agent_key, (pane, "id"))
        self.assertEqual(state.status, "killed agent pi in local:work")
        self.assertEqual(state.status_region, "agents")

    def test_successful_agent_kill_hides_stale_status_record(self):
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        data = SessionSnapshot(
            SourceSnapshot(
                True,
                (target,),
                frozenset(),
                agents=(AgentEntry(pane, "id", "pi", "working"),),
            ),
            {},
        )
        state = SidebarState(favorites=[target], selected_agent_key=(pane, "id"))
        poller = unittest.mock.Mock(launch_id=None, )

        with patch.object(sidebar.sessions, "kill_agent"):
            _execute(Effect("kill_agent", pane, message="pi", agent_id="id"), state, poller, 5)

        self.assertEqual(_agent_entries(data, [target], hidden_agents=state.hidden_agents), [])

    def test_failed_agent_kill_preserves_selection_and_session(self):
        target = Target("ssh", "work", "dev")
        pane = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        state = SidebarState(
            favorites=[target],
            selected_target=target,
            selected_agent_key=(pane, "id"),
        )
        poller = unittest.mock.Mock(launch_id=None, )

        with patch.object(
            sidebar.sessions,
            "kill_agent",
            side_effect=SystemExit("kill agent ssh:dev:work failed: no foreground process"),
        ):
            _execute(Effect("kill_agent", pane, message="pi"), state, poller, 5)

        poller.refresh.assert_not_called()
        self.assertEqual(state.favorites, [target])
        self.assertEqual(state.selected_target, target)
        self.assertEqual(state.selected_agent_key, (pane, "id"))
        self.assertEqual(state.status, "no foreground process")
        self.assertEqual(state.status_region, "agents")

    def test_agent_duration_uses_task_timestamp_then_runtime_fallback(self):
        now = datetime(2026, 6, 20, 16, 45, 30, tzinfo=timezone.utc)
        pane = PaneTarget(Target("local", "work"), "@1", "%2", "/tmp/tmux")
        agents = (
            AgentEntry(pane, "working", "pi", "working", now - timedelta(minutes=9), now - timedelta(seconds=12)),
            AgentEntry(pane, "idle", "pi", None, now - timedelta(minutes=3)),
            AgentEntry(pane, "future", "pi", "input-required", now + timedelta(seconds=5)),
        )
        entries = _agent_entries(SessionSnapshot(SourceSnapshot(True, (), frozenset(), agents=agents), {}), [pane.target])

        with patch("letee.sidebar._ascii", return_value=False):
            lines = {entry.agent_id: _entry_lines(entry, False, set(), None, 40, now=now)[0] for entry in entries}

        self.assertEqual(lines["working"][2:], "pi · working · for 12s")
        self.assertEqual(lines["idle"], "○ pi · idle")
        self.assertEqual(lines["future"], "? pi · input-required")

    def test_duration_formatter_covers_seconds_minutes_hours_and_days(self):
        self.assertEqual([sidebar._format_duration(seconds) for seconds in (12, 180, 7200, 345600)], ["12s", "3m", "2h", "4d"])

    def test_missing_timestamp_omits_duration_and_narrow_ascii_truncates_safely(self):
        entry = Entry("pi", "agent", Target("local", "work"), host="laptop", status="working")
        with patch("letee.sidebar._ascii", return_value=True):
            lines = _entry_lines(entry, False, set(), None, 14)
        self.assertIn(lines[0][0], "|/-\\")
        self.assertEqual(lines[0][1:], " pi · working")
        self.assertTrue(all(sidebar._cell_width(line) <= 14 for line in lines))


class AgentAlertTest(OperationTestCase):
    def setUp(self):
        self.target = Target("local", "work")
        self.pane = PaneTarget(self.target, "@1", "%2", "/tmp/tmux")
        self.state = SidebarState(favorites=[self.target])

    def update(self, *agents, focused=(), current_target=None):
        data = SessionSnapshot(
            SourceSnapshot(True, (self.target,), frozenset(), agents=tuple(agents), focused_panes=frozenset(focused)),
            {},
        )
        return _update_agent_alerts(self.state, data, current_target)

    def agent(self, agent_id="id", status="working", pane=None):
        return AgentEntry(pane or self.pane, agent_id, "pi", status)

    def test_working_to_attention_transition_alerts_once(self):
        self.update(self.agent())

        self.assertTrue(self.update(self.agent(status="completed")))
        self.assertFalse(self.update(self.agent(status="completed")))
        self.assertEqual(self.state.agent_alerts, {(self.pane, "id")})

    def test_alert_persists_when_agent_resumes_working(self):
        self.update(self.agent())
        self.update(self.agent(status="input-required"))

        self.assertFalse(self.update(self.agent()))
        self.assertEqual(self.state.agent_alerts, {(self.pane, "id")})

    def test_focused_exact_pane_does_not_alert_and_clears_existing_alert(self):
        self.update(self.agent())
        self.assertFalse(self.update(self.agent(status="failed"), focused=(self.pane,), current_target=self.target))
        self.assertEqual(self.state.agent_alerts, set())

        self.update(self.agent())
        self.update(self.agent(status="failed"))
        self.assertFalse(self.update(self.agent(status="failed"), focused=(self.pane,), current_target=self.target))
        self.assertEqual(self.state.agent_alerts, set())

    def test_focused_pane_in_background_session_alerts(self):
        self.update(self.agent(), focused=(self.pane,), current_target=self.target)

        self.assertTrue(
            self.update(
                self.agent(status="idle"),
                focused=(self.pane,),
                current_target=Target("local", "other"),
            )
        )
        self.assertEqual(self.state.agent_alerts, {(self.pane, "id")})

    def test_same_agent_id_in_different_panes_stays_distinct(self):
        other = PaneTarget(self.target, "@1", "%3", "/tmp/tmux")
        self.update(self.agent(), self.agent(pane=other))

        self.assertTrue(self.update(self.agent(status="completed"), self.agent(pane=other)))
        self.assertEqual(self.state.agent_alerts, {(self.pane, "id")})

    def test_missing_agent_drops_alert_and_prior_state(self):
        self.update(self.agent())
        self.update(self.agent(status="canceled"))

        self.assertFalse(self.update())
        self.assertEqual(self.state.agent_alerts, set())
        self.assertEqual(self.state.agent_states, {})

    def test_successful_exact_pane_switch_clears_alert_after_focus_update(self):
        key = (self.pane, "id")
        self.state.agent_alerts.add(key)
        with patch("letee.sidebar.cockpit.switch"):
            _execute(Effect("switch_pane", self.pane, message="id"), self.state, unittest.mock.Mock(), 5)

        self.assertIn(key, self.state.agent_alerts)
        data = SessionSnapshot(
            SourceSnapshot(
                True,
                (self.target,),
                frozenset(),
                focused_panes=frozenset({self.pane}),
            ),
            {},
        )
        _update_agent_alerts(self.state, data, self.target)
        self.assertNotIn(key, self.state.agent_alerts)

    def test_switching_alerted_agent_defers_priority_reorder_until_focus_update(self):
        first_pane = PaneTarget(self.target, "@1", "%1", "/tmp/tmux")
        second_pane = PaneTarget(self.target, "@1", "%2", "/tmp/tmux")
        agents = (
            AgentEntry(first_pane, "first", "pi", "completed"),
            AgentEntry(second_pane, "second", "pi", "input-required"),
        )
        data = SessionSnapshot(
            SourceSnapshot(True, (self.target,), frozenset(), agents=agents),
            {},
        )
        key = (first_pane, "first")
        state = SidebarState(favorites=[self.target], agent_alerts={key})

        self.assertEqual(
            [entry.agent_id for entry in _agent_entries(data, [self.target], agent_alerts=state.agent_alerts)],
            ["first", "second"],
        )
        with patch("letee.sidebar.cockpit.switch"):
            _execute(Effect("switch_pane", first_pane, message="first"), state, unittest.mock.Mock(), 5)

        self.assertEqual(
            [entry.agent_id for entry in _agent_entries(data, [self.target], agent_alerts=state.agent_alerts)],
            ["first", "second"],
        )

    def test_agent_alert_marker_has_unicode_and_ascii_forms(self):
        entry = Entry("pi", "agent", self.target, pane_target=self.pane, agent_id="id", status="completed")
        for ascii_mode, marker in ((False, "🔔"), (True, "BELL")):
            with self.subTest(ascii=ascii_mode), patch("letee.sidebar._ascii", return_value=ascii_mode):
                line = _entry_lines(entry, False, set(), None, 40, agent_alerts={(self.pane, "id")})[0]
            self.assertIn(marker, line)


class AddFlowTest(OperationTestCase):
    def test_add_title_renders_back_and_returns_hit_column(self):
        screen = FakeScreen(size=(6, 40))

        _, back_col = _draw(
            screen,
            sidebar._add_entries("location", "", snapshot(), []),
            0,
            "",
            "",
            adding=True,
        )

        title = "".join(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertIn("‹ back", title)
        self.assertEqual(back_col, 40 - sidebar._cell_width("‹ back"))

    def test_add_title_shows_the_chosen_host(self):
        screen = FakeScreen(size=(6, 40))

        _draw(
            screen,
            sidebar._search_entries("dev", "", snapshot(remotes={"dev": source("ssh", host="dev")}), []),
            0,
            "",
            "",
            name_input=True,
            adding=True,
            creation_host="dev",
        )

        title = "".join(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertIn("Add session · dev", title)

        local = FakeScreen(size=(6, 40))
        _draw(
            local,
            sidebar._search_entries("", "", snapshot(), []),
            0,
            "",
            "",
            name_input=True,
            adding=True,
            creation_host="",
        )
        local_title = "".join(call[3] for call in local.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertIn("Add session · localhost", local_title)

    def test_search_footer_talks_about_create_or_switch(self):
        screen = FakeScreen(size=(6, 40))

        _draw(screen, [], 0, "", "wor", name_input=True, adding=True)

        footer = " ".join(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] >= 4)
        self.assertIn("type a name", footer)
        self.assertIn("create or switch", footer)

    def test_name_title_renders_back_and_returns_hit_column(self):
        screen = FakeScreen(size=(6, 40))

        back_col = sidebar._draw_name(screen, SidebarState(add_view="name", creation_host=""))

        title = "".join(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertIn("‹ back", title)
        self.assertEqual(back_col, 40 - sidebar._cell_width("‹ back"))

    def test_selected_add_title_uses_selection_pointer_for_back(self):
        screen = FakeScreen(size=(6, 40))

        _draw(
            screen,
            sidebar._add_entries("location", "", snapshot(), []),
            0,
            "",
            "",
            adding=True,
            add_button_selected=True,
        )

        title = "".join(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertIn("› back", title)
        self.assertNotIn("‹ back", title)

    def test_back_title_uses_ascii_label(self):
        screen = FakeScreen(size=(6, 40))

        with patch("letee.sidebar._ascii", return_value=True):
            _, back_col = _draw(
                screen,
                sidebar._add_entries("location", "", snapshot(), []),
                0,
                "",
                "",
                adding=True,
            )

        title = "".join(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertIn("< back", title)
        self.assertNotIn("‹ back", title)
        self.assertEqual(back_col, 40 - len("< back"))

    def test_narrow_add_title_has_no_invisible_back_target(self):
        screen = FakeScreen(size=(6, 5))

        _, back_col = _draw(
            screen,
            sidebar._add_entries("location", "", snapshot(), []),
            0,
            "",
            "",
            adding=True,
        )

        self.assertIsNone(back_col)
        self.assertFalse(any("back" in call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 0))

        name_screen = FakeScreen(size=(6, 5))
        self.assertIsNone(sidebar._draw_name(name_screen, SidebarState(add_view="name")))
        self.assertFalse(any("back" in call[3] for call in name_screen.calls if call[0] == "addnstr" and call[1] == 0))

    def test_create_row_uses_the_create_icon_and_color(self):
        create = Entry("fresh", "create", host="")

        with patch("letee.sidebar._ascii", return_value=False), patch.dict(
            "letee.sidebar._COLOR", {"create": 11}, clear=True
        ):
            self.assertEqual(_entry_lines(create, True, set(), None, 40), ["› ＋ fresh"])
            self.assertEqual(_entry_lines(create, False, set(), None, 40), ["  ＋ fresh"])
            self.assertEqual(_entry_attr(create, False), 11 | curses.A_BOLD)

        with patch("letee.sidebar._ascii", return_value=True):
            self.assertEqual(_entry_lines(create, False, set(), None, 40), ["  + fresh"])

        self.assertEqual(_entry_height(create), 1)

    def test_locations_count_availability_not_sessions(self):
        data = snapshot(local=("work",), remotes={"dev": source("ssh", ("chat",), host="dev")})
        entries = sidebar._add_entries("location", "", data, [])
        self.assertEqual(
            [(entry.label, entry.host) for entry in entries],
            [("Select a location", None), ("localhost", ""), ("dev", "dev")],
        )

    def test_location_picker_uses_icons_and_location_colors(self):
        local = Entry("localhost", "location", host="")
        remote = Entry("dev", "location", host="dev")

        with patch("letee.sidebar._ascii", return_value=False), patch.dict(
            "letee.sidebar._COLOR", {"local": 11, "remote": 22, "add_entry": 33}, clear=True
        ):
            self.assertEqual(_entry_lines(local, False, set(), None, 40), ["  ● localhost"])
            self.assertEqual(_entry_lines(remote, True, set(), None, 40), ["› ◆ dev"])
            self.assertEqual(_entry_attr(local, False), 11)
            self.assertEqual(_entry_attr(remote, False), 22)

    def test_unavailable_locations_are_disabled_and_empty_state_is_useful(self):
        data = snapshot(local_available=False, remotes={"dev": source("ssh", host="dev", available=False)})
        entries = sidebar._add_entries("location", "", data, [])
        self.assertFalse(sidebar._selectable(entries))
        self.assertTrue(any("No available locations" in entry.label for entry in entries))

    def test_search_lists_all_discovered_sessions_on_chosen_host(self):
        tracked = Target("local", "work")
        tracked_remote = Target("ssh", "chat", "dev")
        data = snapshot(
            local=("work", "notes"),
            remotes={"dev": source("ssh", ("chat", "build"), host="dev")},
        )

        local = sidebar._search_entries("", "", data, [tracked, tracked_remote])
        self.assertEqual(
            [(entry.target, entry.kind) for entry in local],
            [(Target("local", "work"), "session"), (Target("local", "notes"), "session")],
        )

        remote = sidebar._search_entries("dev", "", data, [tracked, tracked_remote])
        self.assertEqual(
            [(entry.target, entry.kind) for entry in remote],
            [
                (Target("ssh", "chat", "dev"), "session"),
                (Target("ssh", "build", "dev"), "session"),
            ],
        )

    def test_search_shows_create_row_only_for_a_free_valid_name(self):
        data = snapshot(local=("work",))

        self.assertEqual(
            [(entry.label, entry.kind) for entry in sidebar._search_entries("", "new", data, [])],
            [("new", "create")],
        )
        self.assertEqual(
            [(entry.label, entry.kind) for entry in sidebar._search_entries("", "work", data, [])],
            [("work", "session")],
        )

        def entries_for(name, favorites=()):
            return [
                (entry.label, entry.kind)
                for entry in sidebar._search_entries("", name, data, list(favorites))
            ]

        empty_state = [("No sessions", "empty"), ("Type a session name to create", "hint")]
        # A tracked name remains available as a switch target.
        tracked_entries = sidebar._search_entries("", "work", data, [Target("local", "work")])
        self.assertEqual([(entry.label, entry.kind) for entry in tracked_entries], [("work", "session")])
        self.assertEqual(sidebar._selectable(tracked_entries), [0])
        # Invalid names never create.
        self.assertEqual(entries_for("bad name"), empty_state)

    def test_search_sorts_exact_match_first_and_hides_create_row(self):
        data = snapshot(local=("workshop", "work"))

        entries = sidebar._search_entries("", "work", data, [])

        self.assertEqual([entry.label for entry in entries], ["work", "workshop"])
        self.assertFalse(any(entry.kind == "create" for entry in entries))

    def test_search_keeps_connection_rows_for_unavailable_hosts(self):
        data = snapshot(local_error="boom", local_available=False, remotes={"dev": None})

        self.assertEqual(
            [(entry.label, entry.kind) for entry in sidebar._search_entries("", "", data, [])],
            [("reconnecting…: boom", "unavailable")],
        )
        self.assertEqual(
            [(entry.label, entry.kind) for entry in sidebar._search_entries("dev", "", data, [])],
            [("connecting…", "unavailable")],
        )

    def test_search_empty_state_suggests_creating(self):
        entries = sidebar._search_entries("", "", snapshot(), [])

        self.assertEqual(
            [(entry.label, entry.kind) for entry in entries],
            [("No sessions", "empty"), ("Type a session name to create", "hint")],
        )
        self.assertFalse(sidebar._selectable(entries))
        empty = entries[0]
        self.assertTrue(_entry_attr(empty, False) & curses.A_DIM)
        self.assertEqual(_entry_lines(empty, False, set(), None, 40), ["    No sessions"])

    def test_open_add_uses_search_for_one_location_and_picker_for_many(self):
        single = SidebarState(focused_region="agents")

        sidebar._open_add(single, snapshot(local=("work",)))

        self.assertEqual(
            (single.add_view, single.creation_host, single.focused_region),
            ("search", "", "sessions"),
        )

        data = snapshot(local=("work",), remotes={"dev": source("ssh", host="dev")})
        multiple = SidebarState()

        sidebar._open_add(multiple, data)

        self.assertEqual(
            (multiple.add_view, multiple.creation_host),
            ("location", None),
        )

        sidebar._select_location(multiple, "dev")
        self.assertEqual(
            (multiple.add_view, multiple.creation_host),
            ("search", "dev"),
        )

        sidebar._add_back(multiple, data)
        self.assertEqual(multiple.add_view, "location")

        sidebar._add_back(single, snapshot(local=("work",)))
        self.assertIsNone(single.add_view)

    def test_search_typing_allows_existing_names_and_reports_invalid_names(self):
        data = snapshot(remotes={"dev": source("ssh", ("work",), host="dev")})
        state = SidebarState(add_view="search", creation_host="dev")

        self.assertFalse(_search_key(state, curses.KEY_DOWN, data))
        self.assertFalse(_search_key(state, 10, data))
        for letter in "work":
            self.assertTrue(_search_key(state, ord(letter), data))

        self.assertEqual(state.filter_text, "work")
        self.assertEqual(state.status, "")

        self.assertTrue(_search_key(state, ord(" "), data))
        self.assertEqual(state.status, "Invalid session name")

    def test_search_key_caps_name_length_and_clears_stale_status(self):
        data = snapshot()
        state = SidebarState(
            add_view="search",
            creation_host="",
            filter_text="x" * 64,
            status="agent failed",
            status_region="agents",
            status_deadline=123.0,
        )

        self.assertTrue(_search_key(state, ord("y"), data))
        self.assertEqual(state.filter_text, "x" * 64)

        self.assertTrue(_search_key(state, curses.KEY_BACKSPACE, data))

        self.assertEqual(len(state.filter_text), 63)
        self.assertEqual((state.status, state.status_region, state.status_deadline), ("", "sessions", None))

    def test_back_navigation_clears_warning_and_deadline(self):
        data = snapshot(local=("work",), remotes={"dev": source("ssh", host="dev")})
        state = SidebarState(
            add_view="search",
            creation_host="dev",
            filter_text="work",
            status="stale",
            status_region="agents",
            status_deadline=123.0,
        )

        sidebar._add_back(state, data)

        self.assertEqual((state.add_view, state.filter_text), ("location", ""))
        self.assertEqual((state.status, state.status_region, state.status_deadline), ("", "sessions", None))

    def test_opening_add_clears_prior_session_or_agent_status(self):
        for region in ("sessions", "agents"):
            with self.subTest(region=region):
                state = SidebarState(status="stale", status_region=region, status_deadline=123.0)

                sidebar._open_add(state, snapshot(local=("work",)))

                self.assertEqual((state.status, state.status_region, state.status_deadline), ("", "sessions", None))

    def test_name_screen_uses_dedicated_row_and_cursor_at_narrow_width(self):
        screen = FakeScreen(size=(6, 16))
        state = SidebarState(add_view="name", creation_host="", creation_text="x" * 64)
        sidebar._draw_name(screen, state)
        lines = [call[3] for call in screen.calls if call[0] == "addnstr"]
        self.assertTrue(any("● localhost" in line for line in lines))
        self.assertTrue(any(line.startswith(" ❯ ") for line in lines))
        self.assertTrue(any("Letters" in line for line in lines))
        cursor = next(call for call in screen.calls if call[0] == "move")
        self.assertLess(cursor[2], 16)
        footer = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 5)
        self.assertEqual(footer[4], 15)

    def test_name_screen_shows_validation_feedback_below_name(self):
        screen = FakeScreen(size=(7, 30))
        state = SidebarState(add_view="name", creation_host="", creation_text="bad name", status="Invalid session name")

        sidebar._draw_name(screen, state)

        feedback = next(call for call in screen.calls if call[0] == "addnstr" and "✕ Invalid session name" in call[3])
        self.assertEqual(feedback[1], 4)

    def test_name_screen_has_ascii_fallback(self):
        screen = FakeScreen(size=(6, 30))
        state = SidebarState(
            add_view="name",
            rename_target=Target("ssh", "work", "dev"),
            creation_host="dev",
            creation_text="work",
        )

        with patch("letee.sidebar._ascii", return_value=True):
            sidebar._draw_name(screen, state)

        lines = [call[3] for call in screen.calls if call[0] == "addnstr"]
        self.assertTrue(any("* dev" in line for line in lines))
        self.assertTrue(any(line.startswith(" > ") for line in lines))
        self.assertTrue(any("Rename session" in line for line in lines))
        self.assertTrue(any("Esc cancel" in line for line in lines))
        self.assertTrue(any("Enter rename" in line for line in lines))


class AddMouseTest(OperationTestCase):
    def _run_mouse(self, keys, data, events):
        screen = FakeScreen(keys, size=(14, 40))
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=data,
            current_target=None,
            bell_target=None,
            current_agent=None,
            pane_active=True,
        )
        poller.tick.return_value = False

        with (
            patch("letee.sidebar.AsyncStatusPoller", return_value=poller),
            patch("letee.sidebar.DiscoveryPoller"),
            patch("letee.sidebar.load_hosts", return_value=[]),
            patch("letee.config.load_sessions", return_value=[]),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar.curses.curs_set") as curs_set,
            patch("letee.sidebar.curses.mousemask"),
            patch("letee.sidebar.curses.getmouse", side_effect=events),
            patch("letee.sidebar._init_colors"),
            patch.object(sidebar, "_add_back", wraps=sidebar._add_back) as add_back,
        ):
            run(screen)

        return add_back, curs_set

    @staticmethod
    def _click_events():
        return [
            (0, 39, 0, 0, curses.BUTTON1_PRESSED),
            (0, 39, 0, 0, curses.BUTTON1_RELEASED),
        ]

    def test_click_back_from_single_location_search_returns_to_sessions(self):
        add_back, curs_set = self._run_mouse(
            [curses.KEY_F11, curses.KEY_MOUSE, curses.KEY_MOUSE, STOP],
            snapshot(),
            self._click_events(),
        )

        add_back.assert_called_once()
        self.assertIsNone(add_back.call_args.args[0].add_view)
        self.assertEqual(curs_set.call_args, call(0))

    def test_click_back_from_search_returns_to_location_with_multiple_locations(self):
        data = snapshot(remotes={"dev": source("ssh", host="dev")})
        add_back, curs_set = self._run_mouse(
            [curses.KEY_F11, curses.KEY_ENTER, curses.KEY_MOUSE, curses.KEY_MOUSE, STOP],
            data,
            self._click_events(),
        )

        add_back.assert_called_once()
        self.assertEqual(add_back.call_args.args[0].add_view, "location")
        self.assertEqual(curs_set.call_args, call(0))

    def test_click_back_from_location_returns_to_sessions(self):
        data = snapshot(remotes={"dev": source("ssh", host="dev")})
        add_back, _ = self._run_mouse(
            [curses.KEY_F11, curses.KEY_MOUSE, curses.KEY_MOUSE, STOP],
            data,
            self._click_events(),
        )

        add_back.assert_called_once()
        self.assertIsNone(add_back.call_args.args[0].add_view)

    def test_mouse_press_goes_back_immediately(self):
        add_back, _ = self._run_mouse(
            [curses.KEY_F11, curses.KEY_MOUSE, STOP],
            snapshot(),
            [(0, 39, 0, 0, curses.BUTTON1_PRESSED)],
        )

        add_back.assert_called_once()

    def test_up_selects_back_when_search_has_no_sessions(self):
        add_back, _ = self._run_mouse(
            [curses.KEY_F11, curses.KEY_UP, curses.KEY_ENTER, STOP],
            snapshot(),
            [],
        )

        add_back.assert_called_once()
        self.assertIsNone(add_back.call_args.args[0].add_view)


class AddRunLoopTest(OperationTestCase):
    def _run_add_flow(self, keys, data, getmouse=None, favorites=()):
        screen = FakeScreen(keys, size=(14, 40))
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=data,
            current_target=None,
            bell_target=None,
            current_agent=None,
            pane_active=True,
        )
        poller.tick.return_value = False
        patches = [
            patch("letee.sidebar.AsyncStatusPoller", return_value=poller),
            patch("letee.sidebar.DiscoveryPoller"),
            patch("letee.sidebar.load_hosts", return_value=[]),
            patch("letee.config.load_sessions", return_value=list(favorites)),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch("letee.sidebar._init_colors"),
        ]
        if getmouse is not None:
            patches.append(patch("letee.sidebar.curses.getmouse", side_effect=getmouse))
        for item in patches:
            item.start()
        try:
            run(screen)
        finally:
            for item in patches:
                item.stop()
        return screen

    def test_typed_name_then_enter_creates_and_tracks(self):
        with (
            patch("letee.sidebar.sessions.create") as create,
            patch("letee.config.save_sessions") as save,
            patch("letee.sidebar.sessions.attach_command", return_value="attach"),
            patch("letee.sidebar.cockpit.switch"),
        ):
            self._run_add_flow(
                [curses.KEY_F11, *map(ord, "new"), curses.KEY_ENTER, STOP],
                snapshot(local=("existing",)),
            )

        create.assert_called_once_with(Target("local", "new"))
        save.assert_called_once_with([Target("local", "new")])

    def test_enter_on_untracked_match_tracks_and_switches_without_creating(self):
        with (
            patch("letee.sidebar.sessions.create") as create,
            patch("letee.config.save_sessions") as save,
            patch("letee.sidebar.sessions.attach_command", return_value="attach") as attach_command,
            patch("letee.sidebar.cockpit.switch") as switch,
        ):
            self._run_add_flow(
                [curses.KEY_F11, *map(ord, "existing"), curses.KEY_ENTER, STOP],
                snapshot(local=("existing",)),
            )
            attach_command.assert_not_called()
            _assert_deferred_switch(self, switch, Target("local", "existing"))
            self.assertEqual(switch.call_args.args[1](), "attach")

        create.assert_not_called()
        save.assert_called_once_with([Target("local", "existing")])
        attach_command.assert_called_once_with(Target("local", "existing"))

    def test_enter_on_tracked_match_switches_without_creating_or_persisting(self):
        target = Target("local", "existing")
        with (
            patch("letee.sidebar.sessions.create") as create,
            patch("letee.config.save_sessions") as save,
            patch("letee.sidebar.sessions.attach_command", return_value="attach") as attach_command,
            patch("letee.sidebar.cockpit.switch") as switch,
        ):
            self._run_add_flow(
                [curses.KEY_F11, *map(ord, "existing"), curses.KEY_ENTER, STOP],
                snapshot(local=("existing",)),
                favorites=[target],
            )
            attach_command.assert_not_called()
            _assert_deferred_switch(self, switch, target)
            self.assertEqual(switch.call_args.args[1](), "attach")

        create.assert_not_called()
        save.assert_not_called()
        attach_command.assert_called_once_with(target)

    def test_enter_on_location_immediately_lists_that_hosts_sessions(self):
        data = snapshot(local=("work",), remotes={"dev": source("ssh", ("notes",), host="dev")})
        with (
            patch("letee.sidebar.sessions.create") as create,
            patch("letee.config.save_sessions"),
            patch("letee.sidebar.sessions.attach_command", return_value="attach") as attach_command,
            patch("letee.sidebar.cockpit.switch") as switch,
        ):
            self._run_add_flow(
                [
                    curses.KEY_F11,
                    curses.KEY_DOWN,
                    curses.KEY_ENTER,
                    curses.KEY_DOWN,
                    curses.KEY_ENTER,
                    STOP,
                ],
                data,
            )
            attach_command.assert_not_called()
            _assert_deferred_switch(self, switch, Target("ssh", "notes", "dev"))
            self.assertEqual(switch.call_args.args[1](), "attach")

        create.assert_not_called()
        # The second Enter must act on the dev search list rebuilt right after
        # selecting the location, before any new poll or keypress.
        attach_command.assert_called_once_with(Target("ssh", "notes", "dev"))

    def test_region_keys_do_not_steal_enter_from_the_add_search(self):
        with (
            patch("letee.sidebar.sessions.create") as create,
            patch("letee.config.save_sessions"),
            patch("letee.sidebar.sessions.attach_command", return_value="attach"),
            patch("letee.sidebar.cockpit.switch"),
        ):
            self._run_add_flow(
                [
                    curses.KEY_F11,
                    curses.KEY_F7,
                    *map(ord, "new"),
                    curses.KEY_ENTER,
                    STOP,
                ],
                snapshot(local=("existing",)),
            )

        create.assert_called_once_with(Target("local", "new"))

    def test_full_search_consumes_navigation_key_at_max_length(self):
        name = "x" * 64
        draws = []

        def draw(*args, **kwargs):
            draws.append(kwargs)
            return 1, None

        with (
            patch.object(sidebar, "load_sidebar_keybindings", return_value=config.DEFAULT_SIDEBAR_KEYBINDINGS),
            patch.object(sidebar, "_draw", side_effect=draw),
        ):
            self._run_add_flow(
                [curses.KEY_F11, *map(ord, name), ord("k"), STOP],
                snapshot(local=(name,)),
            )

        self.assertFalse(draws[-1]["add_button_selected"])

    def test_clicking_the_create_row_creates(self):
        with (
            patch("letee.sidebar.sessions.create") as create,
            patch("letee.config.save_sessions"),
            patch("letee.sidebar.sessions.attach_command", return_value="attach"),
            patch("letee.sidebar.cockpit.switch"),
        ):
            self._run_add_flow(
                [curses.KEY_F11, *map(ord, "new"), curses.KEY_MOUSE, STOP],
                snapshot(local=("existing",)),
                getmouse=[(0, 4, 3, 0, curses.BUTTON1_CLICKED)],
            )

        create.assert_called_once_with(Target("local", "new"))

    def test_async_results_hide_cursor_after_add_editor_closes(self):
        cases = (
            (
                "create",
                [curses.KEY_F11, *map(ord, "new"), curses.KEY_ENTER, STOP],
                snapshot(local=("existing",)),
                (),
                sidebar.EffectResult(Effect("create", Target("local", "new")), ()),
                None,
            ),
            (
                "add-switch",
                [curses.KEY_F11, curses.KEY_ENTER, STOP],
                snapshot(local=("existing",)),
                (),
                sidebar.EffectResult(
                    Effect("add_switch", Target("local", "existing")),
                    (Target("local", "existing"),),
                ),
                None,
            ),
            (
                "rename",
                [ord("e"), *([curses.KEY_BACKSPACE] * 3), *map(ord, "new"), curses.KEY_ENTER, STOP],
                snapshot(local=("old",)),
                (Target("local", "old"),),
                sidebar.EffectResult(
                    Effect("rename", Target("local", "old"), message="new"),
                    (Target("local", "new"),),
                ),
                Target("local", "old"),
            ),
        )

        for name, keys, data, favorites, result, current_target in cases:
            with self.subTest(name=name):
                screen = FakeScreen(keys, size=(14, 40))
                poller = unittest.mock.Mock(launch_id=None,
                    snapshot=data,
                    current_target=current_target,
                    bell_target=None,
                    current_agent=None,
                    pane_active=True,
                )
                poller.tick.return_value = False
                runner = unittest.mock.Mock(blocks_favorite_changes=False, busy=False)
                pending_results = [result]
                submitted = False

                def submit(*_args, **_kwargs):
                    nonlocal submitted
                    submitted = True
                    return True

                def poll():
                    return pending_results.pop(0) if submitted and pending_results else None

                runner.submit.side_effect = submit
                runner.poll.side_effect = poll
                with (
                    patch("letee.sidebar.AsyncStatusPoller", return_value=poller),
                    patch("letee.sidebar.DiscoveryPoller"),
                    patch("letee.sidebar.load_hosts", return_value=[]),
                    patch("letee.config.load_sessions", return_value=list(favorites)),
                    patch("letee.cockpit.current_target", return_value=current_target),
                    patch("letee.sidebar.EffectRunner", return_value=runner),
                    patch("letee.sidebar.curses.curs_set") as curs_set,
                    patch("letee.sidebar.curses.mousemask"),
                    patch("letee.sidebar._init_colors"),
                ):
                    run(screen)

                self.assertEqual(curs_set.call_args_list[-1], call(0))


class SidebarStateTest(OperationTestCase):
    def test_search_key_edits_the_name_and_ignores_navigation(self):
        data = snapshot(remotes={"dev": source("ssh", host="dev")})
        state = SidebarState(add_view="search", creation_host="dev")

        self.assertTrue(_search_key(state, ord("w"), data))
        self.assertTrue(_search_key(state, ord("o"), data))
        self.assertTrue(_search_key(state, 127, data))
        self.assertEqual(state.filter_text, "w")

        self.assertFalse(_search_key(state, curses.KEY_DOWN, data))
        self.assertFalse(_search_key(state, 10, data))
        self.assertEqual(state.filter_text, "w")

    def test_search_key_allows_existing_names_and_reports_invalid_names(self):
        data = snapshot(remotes={"dev": source("ssh", ("work",), host="dev")})
        state = SidebarState(add_view="search", creation_host="dev")

        for letter in "work":
            _search_key(state, ord(letter), data)

        self.assertEqual(state.status, "")

        state.filter_text = "bad name"
        self.assertTrue(_search_key(state, ord("!"), data))
        self.assertEqual(state.status, "Invalid session name")

    def test_search_key_allows_same_name_on_different_host(self):
        data = snapshot(local=("work",), remotes={"dev": source("ssh", host="dev")})
        state = SidebarState(add_view="search", creation_host="dev")

        for letter in "work":
            self.assertTrue(_search_key(state, ord(letter), data))

        self.assertEqual(state.status, "")
        self.assertEqual(
            [(entry.label, entry.kind) for entry in sidebar._search_entries("dev", state.filter_text, data, [])],
            [("work", "create")],
        )

    def test_search_edit_resets_an_existing_match_selection(self):
        data = snapshot(local=("existing",))
        state = SidebarState(
            add_view="search",
            creation_host="",
            filter_text="existing",
            selected_target=Target("local", "existing"),
        )

        self.assertTrue(_search_key(state, curses.KEY_BACKSPACE, data))
        self.assertEqual(
            (state.selected_target, state.selected_index, state.selected_tracked, state.add_button_selected),
            (None, 0, False, False),
        )

        entries = sidebar._search_entries("", state.filter_text, data, [])
        _sync_selection(state, entries)
        self.assertEqual(entries[state.selected_index].kind, "create")

    def test_search_edit_resets_a_selected_back_button(self):
        data = snapshot()
        state = SidebarState(add_view="search", creation_host="", add_button_selected=True)

        self.assertTrue(_search_key(state, ord("n"), data))
        self.assertEqual(
            (state.selected_target, state.selected_index, state.selected_tracked, state.add_button_selected),
            (None, 0, False, False),
        )

        entries = sidebar._search_entries("", state.filter_text, data, [])
        _sync_selection(state, entries)
        self.assertEqual(entries[state.selected_index].kind, "create")

    def test_search_key_caps_name_length_and_clears_stale_status(self):
        state = SidebarState(
            add_view="search",
            creation_host="dev",
            filter_text="x" * 64,
            status="agent failed",
            status_region="agents",
            status_deadline=123.0,
        )

        self.assertTrue(_search_key(state, ord("y"), snapshot()))
        self.assertEqual(state.filter_text, "x" * 64)

        self.assertTrue(_search_key(state, curses.KEY_BACKSPACE, snapshot()))

        self.assertEqual((state.status, state.status_region, state.status_deadline), ("", "sessions", None))

    def test_rename_editor_prefills_selected_session_name(self):
        target = Target("local", "old")
        state = SidebarState(selected_target=target, selected_tracked=True)

        _start_rename(state, target)

        self.assertEqual(
            (state.add_view, state.rename_target, state.creation_host, state.creation_text),
            ("name", target, "", "old"),
        )

    def test_rename_key_rejects_same_host_conflict_without_closing_editor(self):
        old = Target("local", "old")
        other = Target("local", "other")
        state = SidebarState(
            add_view="name", rename_target=old, creation_host="", creation_text="other"
        )

        with self.assertRaisesRegex(SystemExit, "already exists"):
            _rename_key(state, 10, (old, other))

        self.assertEqual((state.add_view, state.rename_target, state.creation_text), ("name", old, "other"))

    def test_rename_key_allows_same_name_on_different_host(self):
        old = Target("ssh", "old", "dev")
        state = SidebarState(
            add_view="name", rename_target=old, creation_host="dev", creation_text="work"
        )

        self.assertEqual(
            _rename_key(state, 10, (old, Target("local", "work"))),
            Effect("rename", target=old, message="work"),
        )

    def test_rename_key_unchanged_name_is_noop_and_closes_editor(self):
        old = Target("local", "old")
        state = SidebarState(
            add_view="name", rename_target=old, creation_host="", creation_text="old"
        )

        self.assertIsNone(_rename_key(state, 10, (old,)))
        self.assertIsNone(state.add_view)
        self.assertIsNone(state.rename_target)

    def test_successful_rename_replaces_favorite_selection_and_discovery_cache(self):
        old = Target("local", "old")
        renamed = Target("local", "new")
        other = Target("local", "other")
        state = SidebarState(
            add_view="name", rename_target=old, creation_host="", creation_text="new",
            selected_target=old, selected_tracked=True, favorites=[old, other],
        )
        poller = unittest.mock.Mock(launch_id=None, )
        with (
            patch.object(sidebar.sessions, "rename", return_value=renamed) as rename,
            patch.object(config, "load_sessions", return_value=[old, other]),
            patch.object(config, "save_sessions") as save,
            patch.object(sidebar.cockpit, "rename_target") as rename_marker,
        ):
            _execute(Effect("rename", target=old, message="new"), state, poller, 5)

        rename.assert_called_once_with(old, "new")
        rename_marker.assert_called_once_with(old, renamed)
        save.assert_called_once_with([renamed, other])
        self.assertEqual(state.favorites, [renamed, other])
        self.assertEqual(state.selected_target, renamed)
        self.assertIsNone(state.add_view)
        poller.assert_has_calls([call.discard(old), call.refresh()])

    def test_failed_rename_keeps_editor_open_with_error(self):
        old = Target("local", "old")
        state = SidebarState(
            add_view="name", rename_target=old, creation_host="", creation_text="new",
            favorites=[old], selected_target=old, selected_tracked=True,
        )
        poller = unittest.mock.Mock(launch_id=None, )
        with (
            patch.object(sidebar.sessions, "rename", side_effect=SystemExit("rename failed")),
            patch.object(config, "load_sessions", return_value=[old]),
            patch.object(config, "save_sessions") as save,
        ):
            _execute(Effect("rename", target=old, message="new"), state, poller, 5)

        save.assert_not_called()
        self.assertEqual((state.add_view, state.rename_target, state.creation_text), ("name", old, "new"))
        self.assertEqual(state.status, "rename failed")

    def test_reset_selection_uses_first_session_and_agent(self):
        first = Target("local", "first")
        state = SidebarState(
            selected_target=Target("local", "last"),
            selected_index=3,
            focused_region="agents",
            agent_selected_index=2,
            selected_agent_key=(PaneTarget(first, "@1", "%1", "/tmp/tmux"), "id"),
            add_button_selected=True,
        )

        _reset_selection(state, [Entry("first", "session", first)])

        self.assertEqual((state.selected_index, state.selected_target), (0, first))
        self.assertEqual((state.agent_selected_index, state.selected_agent_key), (0, None))
        self.assertFalse(state.add_button_selected)

    def test_reset_selection_selects_add_button_when_sessions_are_empty(self):
        state = SidebarState()

        _reset_selection(state, _entries(snapshot(), []))

        self.assertTrue(state.add_button_selected)

    def test_sync_selection_preserves_add_button_selection_when_sessions_exist(self):
        target = Target("local", "work")
        state = SidebarState(add_button_selected=True)
        entries = _entries(snapshot(local=("work",)), [target])

        _sync_selection(state, entries)

        self.assertTrue(state.add_button_selected)

    def test_pending_selection_waits_for_discovery_then_selects_target(self):
        target = Target("ssh", "new", "dev")
        tracked = Target("local", "work")
        state = SidebarState(selected_index=2, pending_selection=target)
        pending_entries = _entries(snapshot(remotes={"dev": source("ssh", ("work",), host="dev")}), [tracked])

        _sync_selection(state, pending_entries)
        self.assertEqual(state.pending_selection, target)
        self.assertIsNone(state.selected_target)

        ready_entries = _entries(snapshot(remotes={"dev": source("ssh", ("work", "new"), host="dev")}), [tracked, target])
        _sync_selection(state, ready_entries)
        self.assertEqual(state.selected_target, target)
        self.assertIsNone(state.pending_selection)

    def test_unrelated_snapshot_preserves_user_selection(self):
        selected = Target("local", "notes")
        state = SidebarState(selected_target=selected, selected_index=2)
        entries = _entries(snapshot(local=("work", "notes"), remotes={"dev": source("ssh", ("chat",), host="dev")}), [selected])

        _sync_selection(state, entries)

        self.assertEqual(entries[state.selected_index].target, selected)

    def test_transition_returns_effect_without_running_operations(self):
        target = Target("local", "work")
        state = SidebarState(selected_target=target)

        effect = _transition(state, "switch")

        self.assertEqual(effect, Effect("switch", target=target))

    def test_remove_session_only_removes_tracked_target(self):
        target = Target("local", "work")
        state = SidebarState(selected_target=target, favorites=[target])

        effect = _transition(state, "remove_session")

        self.assertEqual(state.favorites, [])
        self.assertEqual(
            effect,
            Effect("save_favorites", target=target, favorites=(), message="removed local:work", previous_favorites=(target,)),
        )

        self.assertIsNone(_transition(SidebarState(selected_target=target), "remove_session"))

    def test_remove_active_session_persists_before_resetting_pane(self):
        target = Target("local", "work")
        other = Target("local", "other")
        state = SidebarState(selected_target=target, favorites=[target, other])
        poller = unittest.mock.Mock(launch_id=None, )
        events = []

        effect = _transition(state, "remove_session")
        self.assertIsNotNone(effect)
        with (
            patch.object(sidebar.cockpit, "current_target", return_value=target),
            patch.object(config, "load_sessions", return_value=[target, other]),
            patch.object(config, "save_sessions", side_effect=lambda favorites: events.append(("save", favorites))) as save,
            patch.object(sidebar.cockpit, "reset_to_help", side_effect=lambda _target: events.append(("reset",))) as reset,
            patch.object(sidebar.sessions, "kill") as kill,
        ):
            _execute(effect, state, poller, 5)

        save.assert_called_once_with((other,))
        reset.assert_called_once_with(target)
        kill.assert_not_called()
        poller.observe_effect.assert_called_once_with(unittest.mock.ANY)
        self.assertEqual(events, [("save", (other,)), ("reset",)])

    def test_remove_inactive_session_does_not_reset_pane(self):
        removed = Target("local", "removed")
        active = Target("local", "active")
        state = SidebarState(selected_target=removed, favorites=[removed, active])
        poller = unittest.mock.Mock(launch_id=None, )
        effect = _transition(state, "remove_session")
        self.assertIsNotNone(effect)

        with (
            patch.object(sidebar.cockpit, "current_target", return_value=active),
            patch.object(config, "load_sessions", return_value=[removed, active]),
            patch.object(config, "save_sessions") as save,
            patch.object(sidebar.cockpit, "reset_to_help") as reset,
            patch.object(sidebar.sessions, "kill") as kill,
        ):
            _execute(effect, state, poller, 5)

        save.assert_called_once_with((active,))
        reset.assert_not_called()
        kill.assert_not_called()

    def test_remove_persistence_failure_does_not_reset_active_pane(self):
        target = Target("local", "work")
        other = Target("local", "other")
        state = SidebarState(selected_target=target, favorites=[target, other])
        poller = unittest.mock.Mock(launch_id=None, )
        effect = _transition(state, "remove_session")
        self.assertIsNotNone(effect)

        with (
            patch.object(sidebar.cockpit, "current_target", return_value=target),
            patch.object(config, "load_sessions", return_value=[target, other]),
            patch.object(config, "save_sessions", side_effect=SystemExit("save failed")) as save,
            patch.object(sidebar.cockpit, "reset_to_help") as reset,
            patch.object(sidebar.sessions, "kill") as kill,
        ):
            _execute(effect, state, poller, 5)

        save.assert_called_once_with((other,))
        reset.assert_not_called()
        kill.assert_not_called()
        self.assertEqual(state.favorites, [target, other])
        self.assertEqual(state.status, "save failed")

    def test_failed_tracking_writes_restore_latest_persisted_favorites(self):
        first, second, added = (Target("local", name) for name in ("one", "two", "added"))
        for action in ("remove_session", "move_session_down"):
            with self.subTest(action=action):
                state = SidebarState(favorites=[first, second], selected_target=first, selected_tracked=True)
                effect = _transition(state, action)
                with (
                    patch.object(config, "load_sessions", return_value=[first, second, added]),
                    patch.object(config, "save_sessions", side_effect=OSError("write failed")),
                ):
                    result = sidebar._perform_effect(effect, tuple(state.favorites))
                sidebar._apply_effect(result, state, unittest.mock.Mock(), 5)
                self.assertEqual(state.favorites, [first, second, added])
                self.assertEqual(state.selected_target, first)
                self.assertEqual(state.status, "write failed")

    def test_effect_setup_failures_return_action_errors(self):
        target = Target("local", "work")
        for source, error in (
            ("lock", OSError("lock failed")),
            ("load", OSError("read failed")),
            ("load", SystemExit("invalid session")),
        ):
            with self.subTest(source=source, error=error):
                with (
                    patch.object(sidebar.config, "sessions_lock") as lock,
                    patch.object(config, "load_sessions") as load,
                    patch.object(sidebar.sessions, "create") as create,
                ):
                    if source == "lock":
                        lock.return_value.__enter__.side_effect = error
                    else:
                        load.side_effect = error
                    result = sidebar._perform_effect(Effect("create", target=target), (target,))
                self.assertEqual(result.error, str(error))
                self.assertEqual(result.favorites, (target,))
                self.assertFalse(result.partial_success)
                create.assert_not_called()

    def test_effect_setup_failures_restore_optimistic_tracking_changes(self):
        first, second = (Target("local", name) for name in ("one", "two"))
        for action in ("remove_session", "move_session_down"):
            for source, error in (
                ("lock", OSError("lock failed")),
                ("load", OSError("read failed")),
                ("load", SystemExit("invalid session")),
            ):
                with self.subTest(action=action, source=source, error=error):
                    state = SidebarState(favorites=[first, second], selected_target=first, selected_tracked=True)
                    effect = _transition(state, action)
                    self.assertNotEqual(state.favorites, [first, second])
                    with (
                        patch.object(sidebar.config, "sessions_lock") as lock,
                        patch.object(config, "load_sessions") as load,
                        patch.object(config, "save_sessions") as save,
                    ):
                        if source == "lock":
                            lock.return_value.__enter__.side_effect = error
                        else:
                            load.side_effect = error
                        result = sidebar._perform_effect(effect, tuple(state.favorites))
                    sidebar._apply_effect(result, state, unittest.mock.Mock(), 5)
                    self.assertEqual(state.favorites, [first, second])
                    self.assertEqual(state.selected_target, first)
                    self.assertEqual(state.status, str(error))
                    save.assert_not_called()

    def test_remove_reset_failure_keeps_successfully_persisted_removal(self):
        target = Target("local", "one")
        state = SidebarState(favorites=[target], selected_target=target)
        effect = _transition(state, "remove_session")
        with (
            patch.object(config, "load_sessions", return_value=[target]),
            patch.object(config, "save_sessions"),
            patch.object(sidebar.cockpit, "current_target", return_value=target),
            patch.object(sidebar.cockpit, "reset_to_help", side_effect=OSError("reset failed")),
        ):
            result = sidebar._perform_effect(effect, ())
        sidebar._apply_effect(result, state, unittest.mock.Mock(), 5)
        self.assertEqual(state.favorites, [])
        self.assertEqual(state.status, "reset failed")

    def test_reorder_favorite_swaps_and_keeps_selection(self):
        first = Target("local", "first")
        second = Target("local", "second")
        state = SidebarState(
            selected_target=second,
            selected_tracked=True,
            favorites=[first, second],
        )

        effect = _transition(state, "move_session_up")

        self.assertEqual(state.favorites, [second, first])
        self.assertEqual(state.selected_target, second)
        self.assertEqual(effect, Effect("save_favorites", favorites=(second, first), message="moved local:second up", favorite_move=(second, first), previous_favorites=(first, second)))

    def test_reorder_favorite_boundaries_skip_save(self):
        target = Target("local", "only")
        for action, message in (
            ("move_session_up", "already first session"),
            ("move_session_down", "already last session"),
        ):
            with self.subTest(action=action):
                state = SidebarState(selected_target=target, selected_tracked=True, favorites=[target])

                effect = _transition(state, action)

                self.assertEqual(state.favorites, [target])
                self.assertEqual(effect, Effect("status", message=message))

    def test_regular_section_duplicate_cannot_reorder(self):
        first = Target("local", "first")
        second = Target("local", "second")
        state = SidebarState(selected_target=second, selected_tracked=False, favorites=[first, second])

        self.assertIsNone(_transition(state, "move_session_up"))
        self.assertEqual(state.favorites, [first, second])

    def test_successful_create_switches_and_sets_pending_selection(self):
        target = Target("ssh", "new", "dev")
        state = SidebarState()
        poller = unittest.mock.Mock(launch_id=None, )
        with (
            patch("letee.sidebar.sessions.create") as create,
            patch("letee.config.save_sessions"),
            patch("letee.sidebar.sessions.attach_command", return_value="attach") as attach_command,
            patch("letee.sidebar.cockpit.switch") as switch,
        ):
            self.assertFalse(_execute(Effect("create", target=target), state, poller, 5))
            attach_command.assert_not_called()
            _assert_deferred_switch(self, switch, target)
            self.assertEqual(switch.call_args.args[1](), "attach")

        create.assert_called_once_with(target)
        attach_command.assert_called_once_with(target)
        self.assertEqual(state.pending_selection, target)
        poller.refresh.assert_called_once_with()

    def test_successful_kill_removes_target_before_refresh(self):
        target = Target("ssh", "work", "dev")
        other = Target("local", "other")
        state = SidebarState(selected_target=target, favorites=[target, other])
        poller = unittest.mock.Mock(launch_id=None, )

        with (
            patch("letee.sidebar.sessions.kill"),
            patch.object(config, "load_sessions", return_value=[target, other]),
            patch("letee.config.save_sessions") as save,
        ):
            _execute(Effect("kill", target=target), state, poller, 5)

        self.assertEqual(state.favorites, [other])
        save.assert_called_once_with([other])
        poller.assert_has_calls([unittest.mock.call.discard(target), unittest.mock.call.refresh()])
        self.assertEqual(state.selected_target, target)
        self.assertEqual(state.status, "killed ssh:dev:work")

    def test_inactive_kill_resets_only_if_navigation_selects_target(self):
        target = Target("local", "work")
        other = Target("local", "other")

        for navigation_target, reset_expected in ((target, True), (other, False)):
            with self.subTest(navigation_target=navigation_target):
                current_target = [other]
                events = []

                def kill(_target):
                    events.append("kill")
                    current_target[0] = navigation_target

                def reset_to_help(reset_target):
                    matched = current_target[0] == reset_target
                    if matched:
                        events.append("reset")
                    return matched

                with (
                    patch.object(sidebar.cockpit, "current_target", side_effect=lambda: current_target[0]),
                    patch.object(sidebar.cockpit, "reset_to_help", side_effect=reset_to_help) as reset,
                    patch.object(sidebar.sessions, "kill", side_effect=kill),
                ):
                    result = sidebar._perform_effect(Effect("kill", target=target), ())

                self.assertFalse(result.error)
                self.assertEqual(result.reset_matched, reset_expected)
                reset.assert_called_once_with(target)
                self.assertEqual(events, ["kill", "reset"] if reset_expected else ["kill"])

    def test_numbered_navigation_during_inactive_kill_clears_cached_target(self):
        killed = Target("local", "killed")
        previous = Target("local", "previous")
        current_target = [previous]
        poller = unittest.mock.Mock(launch_id=None, snapshot=snapshot(local=("killed", "previous")))
        status_poller = sidebar.AsyncStatusPoller(poller, previous)

        def kill(_target):
            # A numbered shortcut makes the inactive target current while kill blocks.
            current_target[0] = killed

        def reset_to_help(reset_target):
            matched = current_target[0] == reset_target
            if matched:
                current_target[0] = None
            return matched

        try:
            with (
                patch.object(sidebar.cockpit, "current_target", side_effect=lambda: current_target[0]),
                patch.object(sidebar.cockpit, "reset_to_help", side_effect=reset_to_help),
                patch.object(sidebar.sessions, "kill", side_effect=kill),
            ):
                result = sidebar._perform_effect(Effect("kill", target=killed), ())

            self.assertTrue(result.reset_matched)
            status_poller.observe_effect(result)
            with patch.object(
                sidebar.cockpit,
                "status_snapshot",
                return_value=sidebar.cockpit.StatusSnapshot(None, None, None, True),
            ):
                sampled = status_poller._sample((), status_poller._generation)

            self.assertIsNone(status_poller.current_target)
            self.assertIsNone(sampled.current_target)
        finally:
            status_poller.close()

    def test_kill_persistence_failure_keeps_destructive_state(self):
        target = Target("local", "work")
        other = Target("local", "other")
        state = SidebarState(selected_target=target, favorites=[target, other])
        poller = unittest.mock.Mock(launch_id=None, snapshot=snapshot(local=("work", "other")))

        with (
            patch.object(sidebar.cockpit, "current_target", return_value=target),
            patch.object(sidebar.cockpit, "set_expected_right_pane_death") as expected_death,
            patch.object(sidebar.cockpit, "resolve_expected_right_pane_death") as resolve,
            patch.object(sidebar.sessions, "kill"),
            patch.object(config, "load_sessions", return_value=[target, other]),
            patch.object(config, "save_sessions", side_effect=SystemExit("save failed")) as save,
        ):
            result = sidebar._perform_effect(Effect("kill", target=target), tuple(state.favorites))

        expected_death.assert_called_once_with(target)
        resolve.assert_called_once_with(target, True)
        self.assertEqual(result.error, "save failed")
        self.assertTrue(result.partial_success)
        status = sidebar.AsyncStatusPoller(poller, target)
        try:
            sidebar._apply_effect(result, state, poller, 5)
            status.observe_effect(result)

            save.assert_called_once_with([other])
            self.assertEqual(state.favorites, [other])
            self.assertEqual(state.status, "save failed")
            poller.assert_has_calls([call.discard(target), call.refresh()])
            self.assertIsNone(status.current_target)
        finally:
            status.close()

    def test_kill_cleanup_failure_still_persists_favorites_without_failure_handshake(self):
        target = Target("local", "work")
        other = Target("local", "other")
        events = []

        def resolve(_target, succeeded):
            events.append(f"resolve {succeeded}")
            if succeeded:
                raise SystemExit("cleanup failed")

        with (
            patch.object(sidebar.cockpit, "current_target", return_value=target),
            patch.object(sidebar.cockpit, "set_expected_right_pane_death") as expected_death,
            patch.object(sidebar.cockpit, "resolve_expected_right_pane_death", side_effect=resolve) as handshake,
            patch.object(sidebar.sessions, "kill"),
            patch.object(config, "load_sessions", return_value=[target, other]),
            patch.object(config, "save_sessions", side_effect=lambda favorites: events.append(("save", favorites))) as save,
        ):
            result = sidebar._perform_effect(Effect("kill", target=target), (target, other))

        self.assertEqual(result.error, "cleanup failed")
        self.assertTrue(result.partial_success)
        expected_death.assert_called_once_with(target)
        handshake.assert_called_once_with(target, True)
        save.assert_called_once_with([other])
        self.assertEqual(events, ["resolve True", ("save", [other])])

    def test_failed_active_kill_uses_failure_handshake_without_racy_target_check(self):
        target = Target("local", "work")
        events = []

        def fail_kill(_target):
            events.append("kill")
            raise SystemExit("kill local:work failed: denied")

        def resolve(_target, succeeded):
            events.append(f"resolve {succeeded}")

        with (
            patch.object(sidebar.cockpit, "current_target", return_value=target) as current_target,
            patch.object(sidebar.cockpit, "set_expected_right_pane_death") as expected_death,
            patch.object(sidebar.cockpit, "resolve_expected_right_pane_death", side_effect=resolve) as handshake,
            patch.object(sidebar.sessions, "kill", side_effect=fail_kill),
        ):
            result = sidebar._perform_effect(Effect("kill", target=target), (target,))

        self.assertEqual(result.error, "kill local:work failed: denied")
        current_target.assert_called_once_with()
        expected_death.assert_called_once_with(target)
        handshake.assert_called_once_with(target, False)
        self.assertEqual(events, ["kill", "resolve False"])

    def test_add_switch_tracks_then_switches(self):
        target = Target("local", "work")
        state = SidebarState(add_view="search", creation_host="")
        poller = unittest.mock.Mock(launch_id=None, )
        with (
            patch.object(config, "load_sessions", return_value=[]),
            patch("letee.config.save_sessions") as save,
            patch("letee.sidebar.sessions.attach_command", return_value="attach") as attach_command,
            patch("letee.sidebar.cockpit.switch") as switch,
        ):
            _execute(Effect("add_switch", target=target), state, poller, 5)
            attach_command.assert_not_called()
            _assert_deferred_switch(self, switch, target)
            self.assertEqual(switch.call_args.args[1](), "attach")

        self.assertEqual(state.favorites, [target])
        save.assert_called_once_with([target])
        attach_command.assert_called_once_with(target)
        self.assertEqual(state.status, "")
        self.assertIsNone(state.status_deadline)

    def test_successful_create_tracks_after_creation(self):
        target = Target("local", "new")
        state = SidebarState(add_view="search", creation_host="", filter_text="new")
        poller = unittest.mock.Mock(launch_id=None, )
        with (
            patch.object(config, "load_sessions", return_value=[]),
            patch("letee.sidebar.sessions.create") as create,
            patch("letee.config.save_sessions") as save,
            patch("letee.sidebar.sessions.attach_command", return_value="attach"),
            patch("letee.sidebar.cockpit.switch"),
        ):
            _execute(Effect("create", target=target), state, poller, 5)

        create.assert_called_once_with(target)
        save.assert_called_once_with([target])
        self.assertIsNone(state.add_view)

    def test_failed_create_neither_switches_nor_sets_pending_selection(self):
        target = Target("ssh", "new", "dev")
        state = SidebarState(add_view="search", creation_host="dev", filter_text="new")
        poller = unittest.mock.Mock(launch_id=None, )
        with (
            patch("letee.sidebar.sessions.create", side_effect=SystemExit("create failed")),
            patch("letee.sidebar.cockpit.switch") as switch,
        ):
            self.assertFalse(_execute(Effect("create", target=target), state, poller, 5))

        switch.assert_not_called()
        self.assertIsNone(state.pending_selection)
        self.assertEqual(state.status, "create failed")
        self.assertEqual((state.add_view, state.filter_text), ("search", "new"))

    def test_create_error_does_not_leak_when_backing_out_of_add_flow(self):
        target = Target("ssh", "new", "dev")
        state = SidebarState(add_view="search", creation_host="dev", filter_text="new")
        poller = unittest.mock.Mock(launch_id=None, )
        with patch("letee.sidebar.sessions.create", side_effect=SystemExit("create failed")):
            _execute(Effect("create", target=target), state, poller, 5)

        self.assertEqual(state.status, "create failed")
        sidebar._add_back(state, snapshot(local_available=False, remotes={"dev": source("ssh", host="dev")}))

        self.assertIsNone(state.add_view)
        self.assertEqual((state.status, state.status_region, state.status_deadline), ("", "sessions", None))

    def test_add_switch_error_does_not_leak_when_backing_out_of_add_flow(self):
        target = Target("local", "work")
        state = SidebarState(add_view="search", creation_host="")
        poller = unittest.mock.Mock(launch_id=None, )
        with (
            patch("letee.config.save_sessions"),
            patch("letee.sidebar.sessions.attach_command", return_value="attach"),
            patch("letee.sidebar.cockpit.switch", side_effect=SystemExit("add failed")),
        ):
            _execute(Effect("add_switch", target=target), state, poller, 5)

        self.assertEqual(state.status, "add failed")
        sidebar._add_back(state, snapshot())

        self.assertIsNone(state.add_view)
        self.assertEqual((state.status, state.status_region, state.status_deadline), ("", "sessions", None))

    def test_pane_timeout_status_includes_action_and_session_target(self):
        pane = PaneTarget(Target("local", "work"), "@1", "%2", "/tmp/tmux")
        state = SidebarState()
        with patch("letee.sidebar.cockpit.switch", side_effect=subprocess.TimeoutExpired("tmux", 10)):
            _execute(Effect("switch_pane", pane), state, unittest.mock.Mock(), 5)

        self.assertEqual(state.status, "switch_pane local:work timed out")

    def test_exact_active_agent_uses_active_session_color(self):
        target = Target("local", "work")
        entries = [
            Entry("pi", "agent", target, host="laptop", agent_id="one", status="working"),
            Entry("pi", "agent", target, host="laptop", agent_id="two", status="working"),
        ]
        screen = FakeScreen(size=(8, 40))
        with patch.dict("letee.sidebar._COLOR", {"active": 123, "agent_working": 456}, clear=True):
            sidebar._draw_entries(screen, entries, 0, 7, 40, set(), None, active_agent_id="one")

        first_location = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 2 and "work" in call[3])
        second_location = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 4 and "work" in call[3])
        self.assertEqual(first_location[5], 123)
        self.assertNotEqual(second_location[5], 123)

    def test_focused_pane_selects_active_agent_without_sidebar_click(self):
        pane = PaneTarget(Target("local", "work"), "@1", "%2", "/tmp/tmux")
        snapshot = SessionSnapshot(
            SourceSnapshot(True, (pane.target,), frozenset(), agents=(AgentEntry(pane, "one", "pi", "working"),), focused_panes=frozenset({pane})),
            {},
        )

        self.assertEqual(sidebar._focused_agent_id(snapshot, pane.target, "stale"), "one")

    def test_active_agent_uses_active_session_color(self):
        entry = Entry("pi", "agent", Target("local", "work"), host="laptop", agent_id="one", status="working")
        screen = FakeScreen(size=(6, 40))
        with patch.dict("letee.sidebar._COLOR", {"active": 123, "agent_working": 456}, clear=True):
            sidebar._draw_entries(screen, [entry], 0, 5, 40, set(), None, active_agent_id="one")

        location = next(call for call in screen.calls if call[0] == "addnstr" and "work" in call[3])
        self.assertEqual(location[5], 123)


class AsyncSidebarWorkTest(OperationTestCase):
    def test_automatic_overlays_reject_pending_startup_samples_and_reattach(self):
        target = Target("ssh", "work", "dev")
        recovered = snapshot(remotes={
            "dev": replace(source("ssh", ("work",), host="dev"), sampled_at=10),
        })
        for kind in ("show_reconnecting", "show_unavailable"):
            with self.subTest(kind=kind):
                sampled = threading.Event()
                release = threading.Event()

                def block_discovery(_host):
                    sampled.set()
                    self.assertTrue(release.wait(2))

                poller = unittest.mock.Mock(snapshot=recovered, tick=block_discovery)
                status = sidebar.AsyncStatusPoller(poller, target)
                status.startup_deadline = 42
                status.current_agent = "agent"
                pending_agent = (PaneTarget(target, "@1", "%2", "/tmp/tmux"), "agent")
                status._pending_agent = pending_agent
                launch_id = status.launch_id
                try:
                    with (
                        patch.object(sidebar.operations, "sample_target", return_value=(
                            launch_id,
                            sidebar.cockpit.StatusSnapshot(target, None, None, True),
                            target, 42,
                        )),
                        patch.object(sidebar.cockpit, "startup_timeout", return_value=60),
                    ):
                        status.tick(0)
                        self.assertTrue(sampled.wait(1))
                        status.observe_effect(sidebar.EffectResult(
                            Effect(kind, target, automatic=True), (target,), launch_id=launch_id,
                        ))
                        self.assertIsNone(status.startup_deadline)
                        self.assertEqual(status.current_target, target)
                        self.assertEqual(status.current_agent, "agent")
                        self.assertEqual(status.pending_agent, pending_agent)
                        self.assertEqual(status.launch_id, launch_id)

                        release.set()
                        status._future.result(timeout=1)
                        status._next_poll = float("inf")
                        status.tick(1)
                        self.assertFalse(status.status_sampled)
                        self.assertIsNone(status.startup_deadline)
                        submit = unittest.mock.Mock(return_value=True)
                        interrupted = sidebar._sync_active_session(
                            target, status.snapshot, target, submit,
                            shared_deadline=status.startup_deadline, now=10,
                        )
                        submit.assert_called_once_with(Effect("switch", target, automatic=True))
                        self.assertEqual(interrupted, target)
                finally:
                    release.set()
                    status.close()

    def test_startup_deadline_is_sampled_off_ui_thread_and_rejects_old_generation(self):
        target = Target("local", "work")
        poller = unittest.mock.Mock(launch_id=None, snapshot=snapshot(local=("work",)))
        status = sidebar.AsyncStatusPoller(poller, target)
        ui_thread = threading.get_ident()

        def read_marker(sampled_target):
            self.assertNotEqual(threading.get_ident(), ui_thread)
            self.assertEqual(sampled_target, target)
            return 42

        try:
            with (
                patch.object(
                    sidebar.cockpit, "status_snapshot",
                    return_value=sidebar.cockpit.StatusSnapshot(target, None, None, True),
                ),
                patch.object(sidebar.cockpit, "startup_deadline", side_effect=read_marker),
                patch.object(sidebar.cockpit, "current_target", return_value=target),
            ):
                status.tick(0)
                status._future.result(timeout=1)
                status._next_poll = float("inf")
                status.tick(0)
                self.assertEqual(status.startup_deadline, 42)

                status._next_poll = 0
                status.tick(1)
                status._future.result(timeout=1)
                other = Target("local", "other")
                status.observe_effect(sidebar.EffectResult(Effect("switch", other), ()))
                self.assertIsNone(status.startup_deadline)
                status._next_poll = float("inf")
                status.tick(1)
                self.assertEqual(status.current_target, other)
                self.assertIsNone(status.startup_deadline)
        finally:
            status.close()

    def test_status_poller_uses_batched_cockpit_snapshot(self):
        target = Target("local", "work")
        poller = unittest.mock.Mock(launch_id=None, snapshot=snapshot(local=("work",)))
        status = sidebar.AsyncStatusPoller(poller, target)
        try:
            with (
                patch.object(
                    sidebar.cockpit,
                    "status_snapshot",
                    return_value=sidebar.cockpit.StatusSnapshot(
                        target, target, "agent", False, attachment_failure=(target, "123"),
                    ),
                ) as read,
                patch.object(sidebar.cockpit, "startup_deadline", return_value=42) as marker,
                patch.object(sidebar.cockpit, "current_target") as legacy_target,
                patch.object(sidebar, "_pane_active") as legacy_activity,
            ):
                result = status._sample((), 0)
        finally:
            status.close()

        read.assert_called_once_with()
        marker.assert_called_once_with(target)
        self.assertEqual(result.startup_deadline, 42)
        self.assertEqual(result.attachment_failure, (target, "123"))
        legacy_target.assert_not_called()
        legacy_activity.assert_not_called()
        self.assertEqual(
            (result.current_target, result.bell_target, result.current_agent, result.pane_active),
            (target, target, "agent", False),
        )

    def test_status_poller_preserves_last_known_target_when_snapshot_omits_target(self):
        target = Target("ssh", "work", "dev")
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=snapshot(remotes={"dev": source("ssh", ("work",), host="dev")})
        )
        status = sidebar.AsyncStatusPoller(poller, target)
        try:
            with patch.object(
                sidebar.cockpit,
                "status_snapshot",
                return_value=sidebar.cockpit.StatusSnapshot(None, None, None, True),
            ):
                result = status._sample((), 0)
        finally:
            status.close()

        self.assertEqual(result.current_target, target)
        poller.tick.assert_called_once_with("dev")

    def test_status_query_failure_retains_last_known_state(self):
        target = Target("local", "work")
        bell = Target("local", "bell")
        poller = unittest.mock.Mock(launch_id=None, snapshot=snapshot(local=("work",)))
        status = sidebar.AsyncStatusPoller(poller, target)
        status.bell_target = bell
        status.current_agent = "agent"
        status.pane_active = False
        try:
            with patch.object(sidebar.cockpit, "status_snapshot", side_effect=OSError("tmux unavailable")):
                result = status._sample((), 0)
        finally:
            status.close()

        self.assertEqual(
            (result.current_target, result.bell_target, result.current_agent, result.pane_active),
            (target, bell, "agent", False),
        )
        poller.tick.assert_not_called()

    def test_completed_switch_does_not_overwrite_newer_favorite_changes(self):
        old = Target("local", "old")
        newer = Target("local", "newer")
        state = SidebarState(favorites=[newer])

        sidebar._apply_effect(
            sidebar.EffectResult(Effect("switch", old), (old,)),
            state,
            unittest.mock.Mock(),
            5,
        )

        self.assertEqual(state.favorites, [newer])

    def test_effect_runner_submit_does_not_wait_for_action(self):
        started = threading.Event()
        release = threading.Event()
        effect = Effect("switch", Target("local", "work"))

        def perform(effect, favorites):
            started.set()
            release.wait(1)
            return sidebar.EffectResult(effect, favorites)

        runner = sidebar.EffectRunner()
        try:
            with patch("letee.sidebar._perform_effect", side_effect=perform):
                self.assertTrue(runner.submit(effect, ()))
                self.assertTrue(started.wait(1))
                self.assertIsNone(runner.poll())
                release.set()
                deadline = time.monotonic() + 1
                while (result := runner.poll()) is None and time.monotonic() < deadline:
                    time.sleep(0.001)
            self.assertEqual(result, sidebar.EffectResult(effect, ()))
        finally:
            release.set()
            runner.close()

    def test_effect_runner_coalesces_navigation_to_latest_target(self):
        release = threading.Event()
        first = Effect("switch", Target("local", "one"))
        middle = Effect("switch", Target("local", "two"))
        latest = Effect("switch", Target("local", "three"))
        performed = []

        def perform(effect, favorites):
            performed.append(effect)
            if effect == first:
                release.wait(1)
            return sidebar.EffectResult(effect, favorites)

        runner = sidebar.EffectRunner()
        try:
            with patch("letee.sidebar._perform_effect", side_effect=perform):
                self.assertTrue(runner.submit(first, ()))
                self.assertTrue(runner.submit(middle, ()))
                self.assertTrue(runner.submit(latest, ()))
                release.set()
                results = []
                deadline = time.monotonic() + 1
                while len(results) < 2 and time.monotonic() < deadline:
                    if result := runner.poll():
                        results.append(result)
                    time.sleep(0.001)

            self.assertEqual(performed, [first, latest])
            self.assertTrue(results[0].stale_navigation)
            self.assertFalse(results[1].stale_navigation)
        finally:
            release.set()
            runner.close()

    def test_effect_runner_coalesces_reconnecting_navigation_to_latest_target(self):
        release = threading.Event()
        reconnecting = Effect("show_reconnecting", Target("ssh", "one", "dev"))
        latest = Effect("switch", Target("local", "two"))
        performed = []

        def perform(effect, favorites):
            performed.append(effect)
            if effect == reconnecting:
                release.wait(1)
            return sidebar.EffectResult(effect, favorites)

        runner = sidebar.EffectRunner()
        try:
            with patch("letee.sidebar._perform_effect", side_effect=perform):
                self.assertTrue(runner.submit(reconnecting, ()))
                self.assertTrue(runner.submit(latest, ()))
                release.set()
                results = []
                deadline = time.monotonic() + 1
                while len(results) < 2 and time.monotonic() < deadline:
                    if result := runner.poll():
                        results.append(result)
                    time.sleep(0.001)

            self.assertEqual(performed, [reconnecting, latest])
            self.assertTrue(results[0].stale_navigation)
            self.assertFalse(results[1].stale_navigation)
        finally:
            release.set()
            runner.close()

    def test_effect_runner_rejects_non_navigation_while_busy(self):
        release = threading.Event()
        switch = Effect("switch", Target("local", "one"))
        create = Effect("create", Target("local", "new"))

        def perform(effect, favorites):
            release.wait(1)
            return sidebar.EffectResult(effect, favorites)

        runner = sidebar.EffectRunner()
        try:
            with patch("letee.sidebar._perform_effect", side_effect=perform):
                self.assertTrue(runner.submit(switch, ()))
                self.assertFalse(runner.submit(create, ()))
        finally:
            release.set()
            runner.close()

    def test_effect_runner_rejects_navigation_behind_non_navigation(self):
        release = threading.Event()
        create = Effect("create", Target("local", "new"))
        switch = Effect("switch", Target("local", "one"))

        def perform(effect, favorites):
            release.wait(1)
            return sidebar.EffectResult(effect, favorites)

        runner = sidebar.EffectRunner()
        try:
            with patch("letee.sidebar._perform_effect", side_effect=perform):
                self.assertTrue(runner.submit(create, ()))
                self.assertFalse(runner.submit(switch, ()))
        finally:
            release.set()
            runner.close()

    def test_stale_session_switch_preserves_newer_selection(self):
        old_target = Target("local", "old")
        newer_target = Target("local", "newer")
        state = SidebarState(selected_target=newer_target)

        sidebar._apply_effect(
            sidebar.EffectResult(
                Effect("switch", old_target), (), stale_navigation=True
            ),
            state,
            unittest.mock.Mock(),
            5,
        )

        self.assertEqual(state.selected_target, newer_target)

    def test_stale_agent_switch_preserves_newer_selections(self):
        old_target = Target("local", "old")
        newer_target = Target("local", "newer")
        old_pane = PaneTarget(old_target, "@1", "%1", "/tmp/tmux")
        newer_pane = PaneTarget(newer_target, "@2", "%2", "/tmp/tmux")
        old_key = (old_pane, "old-agent")
        state = SidebarState(
            selected_target=newer_target,
            selected_agent_key=(newer_pane, "new-agent"),
            agent_alerts={old_key},
        )

        sidebar._apply_effect(
            sidebar.EffectResult(
                Effect("switch_pane", old_pane, message="old-agent"),
                (),
                stale_navigation=True,
            ),
            state,
            unittest.mock.Mock(),
            5,
        )

        self.assertEqual(state.selected_target, newer_target)
        self.assertEqual(state.selected_agent_key, (newer_pane, "new-agent"))
        self.assertNotIn(old_key, state.agent_alerts)

    def test_status_poller_resolves_focused_agent_from_same_snapshot(self):
        target = Target("local", "work")
        focused_pane = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        stored_pane = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        poller = unittest.mock.Mock(launch_id=None, )
        poller.snapshot = SessionSnapshot(
            SourceSnapshot(
                True,
                (target,),
                frozenset(),
                agents=(
                    AgentEntry(focused_pane, "focused", "pi-one", "working"),
                    AgentEntry(stored_pane, "stored", "pi-two", "working"),
                ),
                focused_panes=frozenset({focused_pane}),
            ),
            {},
        )

        with patch.object(
            sidebar.cockpit,
            "status_snapshot",
            return_value=sidebar.cockpit.StatusSnapshot(target, None, "stored", True),
        ):
            status = sidebar.AsyncStatusPoller(poller, target)
            try:
                result = status._sample((), 0)
            finally:
                status.close()

        self.assertEqual(result.current_agent, "focused")

    def test_status_poller_updates_current_and_bell_targets_after_rename(self):
        old = Target("local", "old")
        renamed = Target("local", "new")
        poller = unittest.mock.Mock(launch_id=None, snapshot=snapshot(local=("old",)))
        status = sidebar.AsyncStatusPoller(poller, old)
        status.bell_target = old
        try:
            status.observe_effect(
                sidebar.EffectResult(Effect("rename", target=old, message="new"), (renamed,))
            )
        finally:
            status.close()

        self.assertEqual(status.current_target, renamed)
        self.assertEqual(status.bell_target, renamed)

    def test_status_poller_drops_current_target_after_kill_effect(self):
        killed = Target("local", "killed")
        poller = unittest.mock.Mock(launch_id=None, snapshot=snapshot(local=("killed", "other")))
        status = sidebar.AsyncStatusPoller(poller, killed)
        generation = status._generation
        try:
            status.observe_effect(sidebar.EffectResult(Effect("kill", target=killed), ()))
        finally:
            status.close()

        self.assertIsNone(status.current_target)
        self.assertEqual(status._generation, generation + 1)

    def test_failed_navigation_drops_in_flight_status_sample(self):
        old = Target("local", "old")
        target = Target("local", "work")
        poller = unittest.mock.Mock(launch_id=None, snapshot=snapshot(local=("old", "work")))
        status = sidebar.AsyncStatusPoller(poller, old)
        status._next_poll = float("inf")
        generation = status._generation
        status._future = unittest.mock.Mock()
        status._future.done.return_value = True
        status._future.result.return_value = sidebar.StatusResult(
            poller.snapshot, target, None, None, True, generation, sampled=True
        )

        try:
            status.observe_effect(
                sidebar.EffectResult(Effect("switch", target), (), error="ssh failed")
            )
            status.tick(0)
        finally:
            status.close()

        self.assertEqual(status.current_target, old)
        self.assertFalse(status.status_sampled)
        self.assertEqual(status._generation, generation + 1)

    def test_navigation_preserves_refresh_acknowledgment_and_queued_refresh(self):
        old = Target("local", "old")
        target = Target("local", "work")
        for queued_refresh in (False, True):
            with self.subTest(queued_refresh=queued_refresh):
                poller = unittest.mock.Mock(launch_id=None, snapshot=snapshot(local=("old", "work")))
                status = sidebar.AsyncStatusPoller(poller, old)
                status._synchronize_pending = False  # Startup synchronization has completed.
                try:
                    status.refresh()
                    status._commands.clear()  # The in-flight sample consumed the refresh.
                    status._future = unittest.mock.Mock()
                    status._future.done.return_value = True
                    status._future.result.return_value = sidebar.StatusResult(
                        snapshot(local=("old",)), old, old, "stale-agent", False,
                        status._generation, refreshed=True, sampled=True,
                    )
                    status.observe_effect(
                        sidebar.EffectResult(Effect("switch", target), (target,))
                    )
                    if queued_refresh:
                        status.refresh()
                    status._next_poll = float("inf")

                    self.assertFalse(status.tick(0))
                    self.assertEqual(status.refresh_pending, queued_refresh)
                    self.assertEqual(status.current_target, target)
                    self.assertEqual(status.snapshot, poller.snapshot)
                    self.assertIsNone(status.bell_target)
                    self.assertIsNone(status.current_agent)
                    self.assertTrue(status.pane_active)
                    self.assertFalse(status.status_sampled)
                finally:
                    status.close()

    def test_status_poller_ignores_stale_target_after_kill_until_navigation(self):
        killed = Target("ssh", "work", "dev")
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=snapshot(remotes={"dev": source("ssh", ("work",), host="dev")})
        )
        status = sidebar.AsyncStatusPoller(poller, killed)
        try:
            status.observe_effect(sidebar.EffectResult(Effect("kill", target=killed), ()))
            with patch.object(
                sidebar.cockpit,
                "status_snapshot",
                side_effect=(
                    sidebar.cockpit.StatusSnapshot(killed, None, None, True),
                    sidebar.cockpit.StatusSnapshot(None, None, None, True),
                    sidebar.cockpit.StatusSnapshot(killed, None, None, True),
                ),
            ):
                self.assertIsNone(status._sample((), status._generation).current_target)
                self.assertIsNone(status._sample((), status._generation).current_target)

                status.observe_effect(
                    sidebar.EffectResult(sidebar.Effect("switch", target=killed), (killed,))
                )
                self.assertEqual(status._sample((), status._generation).current_target, killed)
        finally:
            status.close()

    def test_status_poller_expires_kill_suppression_after_external_navigation(self):
        killed = Target("local", "killed")
        other = Target("local", "other")
        poller = unittest.mock.Mock(launch_id=None, snapshot=snapshot(local=("killed", "other")))
        status = sidebar.AsyncStatusPoller(poller, killed)
        try:
            status.observe_effect(sidebar.EffectResult(Effect("kill", target=killed), ()))
            with patch.object(
                sidebar.cockpit,
                "status_snapshot",
                side_effect=(
                    sidebar.cockpit.StatusSnapshot(killed, None, None, True),
                    sidebar.cockpit.StatusSnapshot(other, None, None, True),
                    sidebar.cockpit.StatusSnapshot(killed, None, None, True),
                ),
            ):
                self.assertIsNone(status._sample((), status._generation).current_target)
                expired = status._sample((), status._generation)
                self.assertEqual(expired.current_target, other)
                status._next_poll = float("inf")
                status._future = unittest.mock.Mock()
                status._future.done.return_value = True
                status._future.result.return_value = expired
                status.tick(0)
                self.assertIsNone(status._suppressed_target)
                self.assertEqual(status._sample((), status._generation).current_target, killed)
        finally:
            status.close()

    def test_status_poller_resynchronizes_suppressed_target_on_new_launch(self):
        target = Target("local", "work")
        for launch_id in ("old", "new"):
            with (
                self.subTest(launch_id=launch_id),
                patch.object(sidebar.operations, "launch_identity", return_value="old") as identity,
                patch.object(sidebar.operations, "_state", side_effect=lambda: {"launch": identity.return_value, "invalidated": []}),
                patch.object(sidebar.cockpit, "current_target", return_value=None) as current,
                patch.object(sidebar.cockpit, "status_snapshot", return_value=(
                    sidebar.cockpit.StatusSnapshot(target, None, None, True)
                )),
                patch.object(sidebar.cockpit, "startup_deadline", return_value=None),
            ):
                status = sidebar.AsyncStatusPoller(
                    unittest.mock.Mock(snapshot=snapshot(local=("work",))), target,
                )
                status._next_poll = float("inf")

                def accept_sample(*, synchronize=False):
                    result = status._sample((), status._generation, synchronize=synchronize)
                    status._future = unittest.mock.Mock()
                    status._future.done.return_value = True
                    status._future.result.return_value = result
                    status.tick(-1)

                try:
                    # Synchronization after a kill clears the active target but retains suppression.
                    accept_sample(synchronize=True)
                    self.assertIsNone(status.current_target)
                    self.assertEqual(status._suppressed_target, target)
                    self.assertFalse(status._synchronize_pending)

                    current.return_value = target
                    identity.return_value = launch_id
                    accept_sample()
                    self.assertIsNone(status.current_target)
                    self.assertEqual(status._suppressed_target, target)
                    self.assertEqual(status.launch_id, launch_id)
                    self.assertEqual(status._synchronize_pending, launch_id == "new")

                    if status._synchronize_pending:
                        accept_sample(synchronize=True)
                        self.assertEqual(status.current_target, target)
                        self.assertIsNone(status._suppressed_target)
                        self.assertFalse(status._synchronize_pending)
                finally:
                    status.close()

    def test_status_poller_keeps_current_target_after_kill_of_other_session(self):
        killed = Target("local", "killed")
        active = Target("local", "active")
        poller = unittest.mock.Mock(launch_id=None, snapshot=snapshot(local=("killed", "active")))
        status = sidebar.AsyncStatusPoller(poller, active)
        generation = status._generation
        try:
            status.observe_effect(sidebar.EffectResult(Effect("kill", target=killed), ()))
        finally:
            status.close()

        self.assertEqual(status.current_target, active)
        self.assertEqual(status._generation, generation)

    def test_status_poller_drops_removed_active_target_and_rejects_stale_result(self):
        removed = Target("local", "removed")
        poller = unittest.mock.Mock(launch_id=None, snapshot=snapshot(local=("removed",)))
        status = sidebar.AsyncStatusPoller(poller, removed)
        generation = status._generation
        status._next_poll = float("inf")
        stale = sidebar.StatusResult(
            poller.snapshot,
            removed,
            removed,
            "agent",
            False,
            generation,
        )
        try:
            status.observe_effect(
                sidebar.EffectResult(
                    Effect("save_favorites", target=removed, favorites=()),
                    (),
                )
            )

            self.assertIsNone(status.current_target)
            self.assertEqual(status._generation, generation + 1)

            status._future = unittest.mock.Mock()
            status._future.done.return_value = True
            status._future.result.return_value = stale
            status.tick(0)
        finally:
            status.close()

        self.assertIsNone(status.current_target)
        self.assertIsNone(status.bell_target)
        self.assertIsNone(status.current_agent)
        self.assertTrue(status.pane_active)

    def test_status_poller_keeps_refresh_pending_until_renamed_remote_session_is_seen(self):
        old = Target("ssh", "old", "dev")
        renamed = Target("ssh", "renamed", "dev")
        stale = snapshot(remotes={"dev": source("ssh", ("old",), host="dev")})
        fresh = snapshot(remotes={"dev": source("ssh", ("renamed",), host="dev")})
        poller = unittest.mock.Mock(launch_id=None, snapshot=stale)
        status = sidebar.AsyncStatusPoller(poller, old)
        status._synchronize_pending = False  # Startup synchronization has completed.
        status._next_poll = float("inf")
        try:
            status.refresh()
            status.observe_effect(
                sidebar.EffectResult(Effect("rename", target=old, message="renamed"), (renamed,))
            )

            status._future = unittest.mock.Mock()
            status._future.done.return_value = True
            status._future.result.return_value = sidebar.StatusResult(
                stale, renamed, None, None, True, status._generation
            )
            status.tick(0)
            self.assertTrue(status.refresh_pending)

            status._future = unittest.mock.Mock()
            status._future.done.return_value = True
            status._future.result.return_value = sidebar.StatusResult(
                stale, renamed, None, None, True, status._generation, True
            )
            status.tick(0)
            self.assertTrue(status.refresh_pending)

            status._future = unittest.mock.Mock()
            status._future.done.return_value = True
            status._future.result.return_value = sidebar.StatusResult(
                fresh, renamed, None, None, True, status._generation
            )
            status.tick(0)
            self.assertFalse(status.refresh_pending)
        finally:
            status.close()

    def test_remote_rename_fresh_absence_releases_wait_and_resumes_reconciliation(self):
        old = Target("ssh", "old", "dev")
        renamed = Target("ssh", "renamed", "dev")
        absent = source("ssh", host="dev")
        poller = unittest.mock.Mock(launch_id=None, snapshot=snapshot())
        status = sidebar.AsyncStatusPoller(poller, old)
        status._synchronize_pending = False  # Startup synchronization has completed.
        reconciler = sidebar.SessionReconciler()
        submit = unittest.mock.Mock(return_value=True)
        status._next_poll = float("inf")
        try:
            with patch.object(sidebar.time, "monotonic", return_value=10):
                status.observe_effect(sidebar.EffectResult(
                    Effect("rename", target=old, message="renamed"), (renamed,)
                ))
            for sampled_at, pending in ((9, True), (10, True), (11, False)):
                with self.subTest(sampled_at=sampled_at):
                    data = snapshot(remotes={"dev": replace(absent, sampled_at=sampled_at)})
                    status._future = unittest.mock.Mock()
                    status._future.done.return_value = True
                    status._future.result.return_value = sidebar.StatusResult(
                        data, renamed, None, None, True, status._generation
                    )
                    status.tick(0)
                    self.assertEqual(status.refresh_pending, pending)
                    if not status.refresh_pending:
                        reconciler.reconcile((renamed,), renamed, status.snapshot, submit, 12)
                    if pending:
                        submit.assert_not_called()
            submit.assert_called_once_with(sidebar.Effect("switch", renamed, automatic=True))
        finally:
            status.close()

    def test_status_poller_keeps_switched_agent_until_focused_pane_is_confirmed(self):
        target = Target("local", "work")
        first_pane = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        second_pane = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        agents = (
            AgentEntry(first_pane, "first", "pi-one", "working"),
            AgentEntry(second_pane, "second", "pi-two", "working"),
        )

        def agent_snapshot(focused_pane):
            return SessionSnapshot(
                SourceSnapshot(
                    True,
                    (target,),
                    frozenset(),
                    agents=agents,
                    focused_panes=frozenset({focused_pane}),
                ),
                {},
            )

        stale = agent_snapshot(first_pane)
        poller = unittest.mock.Mock(launch_id=None, snapshot=stale)
        status = sidebar.AsyncStatusPoller(poller, target)
        status._next_poll = float("inf")

        def complete(snapshot, current_agent):
            status._future = unittest.mock.Mock()
            status._future.done.return_value = True
            status._future.result.return_value = sidebar.StatusResult(
                snapshot, target, None, current_agent, True, status._generation
            )
            status.tick(0)

        try:
            status.observe_effect(
                sidebar.EffectResult(
                    Effect("switch_pane", second_pane, message="second"), ()
                )
            )
            complete(stale, "first")

            self.assertEqual(status.current_agent, "second")
            self.assertEqual(status.pending_agent, (second_pane, "second"))

            complete(agent_snapshot(second_pane), "second")
            self.assertIsNone(status.pending_agent)
            complete(stale, "first")

            self.assertEqual(status.current_agent, "first")
        finally:
            status.close()

    def test_automatic_switch_drops_stale_in_flight_status_result(self):
        started = threading.Event()
        release = threading.Event()
        target = Target("local", "work")
        selected_target = Target("local", "selected")

        class BlockingPoller:
            snapshot = snapshot()

            def tick(self, active_host):
                started.set()
                release.wait(1)
                self.snapshot = snapshot(local=("work",))
                return True

            def refresh(self):
                return False

            def discard(self, target):
                pass

            def close(self):
                pass

        with patch.object(
            sidebar.cockpit,
            "status_snapshot",
            return_value=sidebar.cockpit.StatusSnapshot(target, target, "stale-agent", False),
        ):
            poller = sidebar.AsyncStatusPoller(BlockingPoller(), None)
            try:
                self.assertFalse(poller.tick(0))
                self.assertTrue(started.wait(1))
                self.assertEqual(poller.snapshot.sessions, ())
                poller._next_poll = float("inf")
                poller.observe_effect(
                    sidebar.EffectResult(
                        Effect("switch", selected_target, automatic=True), ()
                    )
                )
                release.set()
                deadline = time.monotonic() + 1
                while poller._future is not None and time.monotonic() < deadline:
                    poller.tick(1)
                    time.sleep(0.001)
                self.assertIsNone(poller._future)
                self.assertEqual(poller.snapshot.sessions, ())
                self.assertEqual(poller.current_target, selected_target)
                self.assertEqual(poller._generation, 1)
                self.assertIsNone(poller.bell_target)
                self.assertIsNone(poller.current_agent)
                self.assertTrue(poller.pane_active)
            finally:
                release.set()
                poller.close()

    def test_stale_in_flight_status_result_does_not_clear_newer_kill_suppression(self):
        started = threading.Event()
        release = threading.Event()
        stale_target = Target("local", "stale")
        killed_target = Target("local", "killed")
        poller = unittest.mock.Mock(launch_id=None, snapshot=snapshot(local=("stale", "killed")))

        status_reads = 0

        def read_status():
            nonlocal status_reads
            if status_reads == 0:
                started.set()
                self.assertTrue(release.wait(1))
                result = sidebar.cockpit.StatusSnapshot(stale_target, None, None, True)
            else:
                result = sidebar.cockpit.StatusSnapshot(killed_target, None, None, True)
            status_reads += 1
            return result

        status = sidebar.AsyncStatusPoller(poller, stale_target)
        try:
            status.observe_effect(
                sidebar.EffectResult(sidebar.Effect("kill", target=stale_target), ())
            )
            with patch.object(sidebar.cockpit, "status_snapshot", side_effect=read_status):
                self.assertFalse(status.tick(0))
                self.assertTrue(started.wait(1))
                status._next_poll = float("inf")
                status.observe_effect(
                    sidebar.EffectResult(sidebar.Effect("switch", target=killed_target), ())
                )
                status.observe_effect(
                    sidebar.EffectResult(sidebar.Effect("kill", target=killed_target), ())
                )
                release.set()

                deadline = time.monotonic() + 1
                while status._future is not None and time.monotonic() < deadline:
                    status.tick(1)
                    time.sleep(0.001)

                self.assertIsNone(status._future)
                self.assertEqual(status._suppressed_target, killed_target)
                self.assertIsNone(status._sample((), status._generation).current_target)
        finally:
            release.set()
            status.close()


    def test_blocked_sample_does_not_publish_after_target_change(self):
        started = threading.Event()
        release = threading.Event()
        stale_target = Target("ssh", "work", "old")
        new_target = Target("ssh", "work", "new")
        stale_snapshot = snapshot(remotes={"old": source("ssh", ("work",), host="old")})
        fresh_snapshot = snapshot(remotes={"new": source("ssh", ("work",), host="new")})

        class BlockingPoller:
            snapshot = stale_snapshot

            def tick(self, active_host):
                self.active_host = active_host
                self.snapshot = fresh_snapshot
                return True

            def refresh(self):
                return False

            def discard(self, target):
                pass

            def close(self):
                pass

        poller = BlockingPoller()
        status = sidebar.AsyncStatusPoller(poller, stale_target)

        def read_status():
            started.set()
            self.assertTrue(release.wait(1))
            return sidebar.cockpit.StatusSnapshot(None, None, None, True)

        try:
            with (
                patch.object(sidebar.cockpit, "status_snapshot", side_effect=read_status),
                patch.object(sidebar.cockpit, "current_target", return_value=stale_target),
            ):
                self.assertFalse(status.tick(0))
                self.assertTrue(started.wait(1))
                status._next_poll = float("inf")
                status.observe_effect(
                    sidebar.EffectResult(sidebar.Effect("switch", target=new_target), ())
                )
                release.set()

                deadline = time.monotonic() + 1
                while status._future is not None and time.monotonic() < deadline:
                    status.tick(1)
                    time.sleep(0.001)

            self.assertEqual(poller.active_host, "old")
            self.assertEqual(status.snapshot, stale_snapshot)
            self.assertEqual(status.current_target, new_target)
        finally:
            release.set()
            status.close()


class SidebarColorTest(OperationTestCase):
    def setUp(self):
        original = sidebar._COLOR
        self.addCleanup(setattr, sidebar, "_COLOR", original)
        sidebar._COLOR = {}

    def test_256_color_terminal_uses_logo_palette(self):
        with (
            patch("letee.sidebar.curses.has_colors", return_value=True),
            patch("letee.sidebar.curses.start_color"),
            patch("letee.sidebar.curses.use_default_colors"),
            patch.object(curses, "COLORS", 256, create=True),
            patch("letee.sidebar.curses.init_pair") as init_pair,
            patch("letee.sidebar.curses.color_pair", side_effect=lambda pair: pair << 8),
        ):
            _init_colors()

        self.assertEqual(
            init_pair.call_args_list,
            [
                call(1, 79, 233),
                call(2, 214, -1),
                call(3, 36, -1),
                call(4, 30, -1),
                call(5, 79, -1),
                call(6, curses.COLOR_YELLOW, -1),
                call(7, curses.COLOR_RED, -1),
                call(8, 30, -1),
                call(9, 233, 79),
                call(10, 79, -1),
                call(11, 214, -1),
                call(12, 36, -1), call(13, 30, -1), call(14, 167, -1),
                call(15, curses.COLOR_MAGENTA, -1), call(16, curses.COLOR_RED, -1),
                call(17, curses.COLOR_RED, -1), call(18, 79, -1),
                call(19, curses.COLOR_YELLOW, -1),
                call(20, 30, -1),
            ],
        )
        self.assertEqual(sidebar._COLOR["title"], (1 << 8) | curses.A_BOLD)
        self.assertEqual(sidebar._COLOR["active"], (2 << 8) | curses.A_BOLD)
        self.assertEqual(sidebar._COLOR["section"], (5 << 8) | curses.A_BOLD)
        self.assertEqual(sidebar._COLOR["hints"], (8 << 8) | curses.A_DIM)
        self.assertEqual(sidebar._COLOR["slot"], (10 << 8) | curses.A_BOLD | curses.A_REVERSE)
        self.assertEqual(sidebar._COLOR["slot_active"], (11 << 8) | curses.A_BOLD | curses.A_REVERSE)

    def test_8_color_terminal_uses_safe_palette(self):
        with (
            patch("letee.sidebar.curses.has_colors", return_value=True),
            patch("letee.sidebar.curses.start_color"),
            patch("letee.sidebar.curses.use_default_colors"),
            patch.object(curses, "COLORS", 8, create=True),
            patch("letee.sidebar.curses.init_pair") as init_pair,
            patch("letee.sidebar.curses.color_pair", side_effect=lambda pair: pair << 8),
        ):
            _init_colors()

        self.assertEqual(
            init_pair.call_args_list,
            [
                call(1, curses.COLOR_CYAN, curses.COLOR_BLACK),
                call(2, curses.COLOR_YELLOW, -1),
                call(3, curses.COLOR_GREEN, -1),
                call(4, curses.COLOR_CYAN, -1),
                call(5, curses.COLOR_CYAN, -1),
                call(6, curses.COLOR_YELLOW, -1),
                call(7, curses.COLOR_RED, -1),
                call(8, curses.COLOR_CYAN, -1),
                call(9, curses.COLOR_BLACK, curses.COLOR_CYAN),
                call(10, curses.COLOR_CYAN, -1),
                call(11, curses.COLOR_YELLOW, -1),
                call(12, curses.COLOR_GREEN, -1), call(13, curses.COLOR_CYAN, -1),
                call(14, curses.COLOR_RED, -1), call(15, curses.COLOR_MAGENTA, -1),
                call(16, curses.COLOR_RED, -1), call(17, curses.COLOR_RED, -1),
                call(18, curses.COLOR_CYAN, -1), call(19, curses.COLOR_YELLOW, -1),
                call(20, curses.COLOR_CYAN, -1),
            ],
        )
        self.assertEqual(sidebar._COLOR["active"], (2 << 8) | curses.A_BOLD)
        self.assertEqual(sidebar._COLOR["section"], (5 << 8) | curses.A_BOLD)
        self.assertEqual(sidebar._COLOR["slot"], (10 << 8) | curses.A_BOLD | curses.A_REVERSE)
        self.assertEqual(sidebar._COLOR["slot_active"], (11 << 8) | curses.A_BOLD | curses.A_REVERSE)

    def test_no_color_terminal_leaves_palette_empty(self):
        sidebar._COLOR = {"title": 123}
        with patch("letee.sidebar.curses.has_colors", return_value=False):
            _init_colors()
        self.assertEqual(sidebar._COLOR, {})

    def test_curses_error_leaves_palette_empty(self):
        sidebar._COLOR = {"title": 123}
        with (
            patch("letee.sidebar.curses.has_colors", return_value=True),
            patch("letee.sidebar.curses.start_color", side_effect=curses.error),
        ):
            _init_colors()
        self.assertEqual(sidebar._COLOR, {})


class SidebarDrawTest(OperationTestCase):
    def test_compact_header_matches_expanded_title_style_across_row(self):
        for color in (123, 0):
            for ascii_mode in (False, True):
                with self.subTest(color=color, ascii=ascii_mode), patch.dict(sidebar._COLOR, {"title": color, "section": 456}, clear=True), patch.object(sidebar, "_ascii", return_value=ascii_mode):
                    expanded = FakeScreen(size=(10, 40))
                    compact = FakeScreen(size=(10, 7))
                    _draw(expanded, [], 0, "", "")
                    _draw(compact, [], 0, "", "", compact=True)
                    title = next(item for item in expanded.calls if item[0] == "addnstr" and item[1] == 0)
                    header = next(item for item in compact.calls if item[0] == "addnstr" and item[1] == 0)
                    self.assertEqual(header[5], title[5])
                    self.assertEqual(header[5], color or (curses.A_BOLD | curses.A_REVERSE))
                    self.assertEqual(header[3], ("  >  " if ascii_mode else "  »  ").ljust(7))
                    self.assertEqual(header[4], 7)

    def test_expanded_header_has_collapse_control_but_add_header_does_not(self):
        for ascii_mode in (False, True):
            with self.subTest(ascii=ascii_mode), patch.object(sidebar, "_ascii", return_value=ascii_mode):
                for width, adding in ((8, False), (40, False), (40, True)):
                    screen = FakeScreen(size=(10, width))
                    sidebar._draw_title(screen, width, [], "", adding=adding)
                    header = next(item[3] for item in screen.calls if item[0] == "addnstr" and item[1] == 0)
                    control = "<" if ascii_mode else "‹"
                    self.assertEqual(header[1] == control, not adding)
                    self.assertTrue(any(("back" if adding else "add") in item[3] for item in screen.calls if item[0] == "addnstr"))

    def test_inactive_sidebar_keeps_colors_and_hides_pointer(self):
        target = Target("local", "work")
        screen = FakeScreen(size=(7, 30))

        with patch.dict("letee.sidebar._COLOR", {"local": 45}, clear=True):
            _draw(screen, [Entry("work", "session", target)], 0, "", "", pane_active=False)

        row = next(call for call in screen.calls if call[0] == "addnstr" and "work" in call[3])
        self.assertNotIn("›", row[3])
        self.assertEqual(row[5], 45)

    def test_default_agent_panel_uses_40_percent_of_available_height(self):
        entries = [Entry("", "order")]
        layout = sidebar._agent_layout(24, 1, entries, 40, False)
        footer_top, session_top, _, separator = layout
        available = footer_top - session_top - 1

        self.assertEqual(SidebarState().agent_percentage, 40)
        self.assertEqual(footer_top - separator - 1, round(available * 0.4))

    def test_tall_to_small_resize_keeps_percentage_instead_of_rows(self):
        entries = [Entry("", "order")]
        tall = sidebar._agent_layout(30, 1, entries, 60, False)
        small = sidebar._agent_layout(12, 1, entries, 60, False)

        self.assertEqual(tall[0] - tall[3] - 1, 16)
        self.assertEqual(small[0] - small[3] - 1, 5)

    def test_small_to_tall_resize_expands_proportionally(self):
        entries = [Entry("", "order")]
        small = sidebar._agent_layout(12, 1, entries, 40, False)
        tall = sidebar._agent_layout(30, 1, entries, 40, False)

        self.assertEqual(small[0] - small[3] - 1, 3)
        self.assertEqual(tall[0] - tall[3] - 1, 10)

    def test_small_screen_does_not_use_stale_tall_panel_rows(self):
        entries = [Entry("", "order")]
        tall = sidebar._agent_layout(40, 1, entries, 80, False)
        small = sidebar._agent_layout(12, 1, entries, 80, False)

        self.assertGreater(tall[0] - tall[3] - 1, 20)
        self.assertEqual(small[0] - small[3] - 1, 6)

    def test_minimum_complete_agent_rows_remain_visible(self):
        entries = [Entry("", "order"), Entry("pi", "agent")]
        footer_top, _, minimum_agent_rows, separator = sidebar._agent_layout(20, 1, entries, 0, False)

        self.assertEqual(minimum_agent_rows, 4)
        self.assertGreaterEqual(footer_top - separator - 1, 4)

    def test_maximum_panel_rows_leave_session_section_space(self):
        entries = [Entry("", "order"), Entry("pi", "agent")]
        footer_top, session_top, _, separator = sidebar._agent_layout(20, 1, entries, 100, False)

        self.assertGreaterEqual(separator - session_top, 1)
        self.assertLessEqual(footer_top - separator - 1, footer_top - session_top - 2)

    def _run_agent_resize_keys(self, keys, resize_step=5):
        screen = FakeScreen([*(ord(key) for key in keys), STOP], size=(24, 40))
        percentages = []

        def draw_spy(*args, **kwargs):
            bound = inspect.signature(_draw).bind(*args, **kwargs)
            percentages.append(bound.arguments.get("agent_percentage"))
            return _draw(*args, **kwargs)

        poller = unittest.mock.Mock(launch_id=None,
            snapshot=snapshot(),
            current_target=None,
            bell_target=None,
            current_agent=None,
            pane_active=True,
        )
        poller.tick.return_value = False
        with (
            patch("letee.sidebar.AsyncStatusPoller", return_value=poller),
            patch("letee.sidebar.load_agent_panel_resize_step", return_value=resize_step),
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._mouse_mask"),
            patch("letee.sidebar._init_colors"),
            patch("letee.config.load_sessions", return_value=[]),
            patch("letee.sidebar._entries", return_value=[]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar._draw", side_effect=draw_spy),
        ):
            run(screen)

        return percentages

    def test_agent_resize_keys_change_percentage_by_default_five_points(self):
        self.assertEqual(self._run_agent_resize_keys("["), [40, 45])
        self.assertEqual(self._run_agent_resize_keys("]"), [40, 35])

    def test_agent_resize_keys_use_custom_configured_step(self):
        self.assertEqual(self._run_agent_resize_keys("[", resize_step=12), [40, 52])

    def test_repeated_resize_keys_stop_at_percentage_bounds(self):
        increase = self._run_agent_resize_keys("[" * 30)
        decrease = self._run_agent_resize_keys("]" * 30)

        self.assertEqual(increase[-1], 100)
        self.assertEqual(decrease[-1], 0)
        self.assertTrue(all(0 <= percentage <= 100 for percentage in increase + decrease))

    def test_layout_maintainer_repairs_until_stopped(self):
        waits = []

        class StopAfterOneRepair:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, timeout):
                waits.append(timeout)
                self.stopped = True

        with patch.object(sidebar.cockpit, "repair_layout") as repair_layout:
            sidebar._maintain_sidebar(StopAfterOneRepair(), "%1")

        repair_layout.assert_called_once_with("%1")
        self.assertEqual(waits, [sidebar.LAYOUT_REPAIR_INTERVAL])

    def test_layout_maintainer_retries_after_transient_repair_errors(self):
        waits = []

        class StopAfterRetries:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, timeout):
                waits.append(timeout)
                if len(waits) == 4:
                    self.stopped = True

        with patch.object(
            sidebar.cockpit,
            "repair_layout",
            side_effect=[
                OSError("tmux unavailable"),
                subprocess.SubprocessError("tmux failed"),
                SystemExit("tmux timed out"),
                None,
            ],
        ) as repair_layout:
            sidebar._maintain_sidebar(StopAfterRetries(), "%1")

        self.assertEqual(repair_layout.call_count, 4)
        self.assertEqual(waits, [sidebar.LAYOUT_REPAIR_INTERVAL] * 4)

    def test_layout_maintainer_propagates_unexpected_repair_errors(self):
        stop = unittest.mock.Mock()
        stop.is_set.return_value = False

        with patch.object(sidebar.cockpit, "repair_layout", side_effect=RuntimeError("programming error")):
            with self.assertRaisesRegex(RuntimeError, "programming error"):
                sidebar._maintain_sidebar(stop, "%1")

        stop.wait.assert_not_called()

    def test_main_restarts_after_keyboard_interrupt(self):
        with (
            patch.dict(sidebar.os.environ, {}, clear=True),
            patch("letee.sidebar.curses.wrapper", side_effect=[KeyboardInterrupt, None]) as wrapper,
        ):
            self.assertEqual(main(), 0)

        self.assertEqual(wrapper.call_count, 2)

    def test_mouse_press_and_synthesized_click_activate(self):
        self.assertTrue(_mouse_activates(curses.BUTTON1_PRESSED))
        self.assertTrue(_mouse_activates(curses.BUTTON1_CLICKED))
        self.assertFalse(_mouse_activates(curses.BUTTON1_RELEASED))

    def test_mouse_mask_includes_position_reports_and_disables_batching(self):
        expected = (
            curses.BUTTON1_CLICKED
            | curses.BUTTON1_PRESSED
            | curses.BUTTON1_RELEASED
            | curses.BUTTON3_PRESSED
            | curses.BUTTON4_PRESSED
            | getattr(curses, "BUTTON5_PRESSED", 0)
            | curses.REPORT_MOUSE_POSITION
        )
        with (
            patch("letee.sidebar.curses.mousemask") as mousemask,
            patch("letee.sidebar.curses.mouseinterval") as mouseinterval,
            patch("letee.sidebar.os.write"),
        ):
            _mouse_mask()
            _mouse_mask(True)

        self.assertEqual(mousemask.call_args_list, [call(expected), call(expected)])
        self.assertEqual(mouseinterval.call_args_list, [call(0), call(0)])

    def test_mouse_mask_restores_base_tracking_after_disabling_motion(self):
        events = []
        with (
            patch("letee.sidebar.curses.mousemask", side_effect=lambda _: events.append("mask")),
            patch("letee.sidebar.os.write", side_effect=lambda *_: events.append("disable")),
        ):
            _mouse_mask()

        self.assertEqual(events, ["disable", "mask"])

    def test_mouse_mask_tolerates_missing_button5(self):
        with patch.object(curses, "BUTTON5_PRESSED", None), patch("letee.sidebar.curses.mousemask") as mousemask:
            _mouse_mask()

        self.assertFalse(mousemask.call_args.args[0] & 2097152)

    def test_entry_at_row_maps_visible_and_scrolled_rows(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(10)]

        self.assertEqual(_entry_at_row(entries, 0, 1, 8, 1), 0)
        start, _ = _viewport(entries, 9, 7)
        self.assertEqual(_entry_at_row(entries, 9, 2, 8, 1), start)

    def test_down_more_uses_last_available_row(self):
        entries = [
            Entry(str(i), "session", Target("local", str(i)), tracked=True)
            for i in range(4)
        ]
        screen = FakeScreen(size=(6, 30))

        sidebar._draw_entries(screen, entries, 0, 6, 30, set(), None)

        marker = next(call for call in screen.calls if call[0] == "addnstr" and call[3] == "↓ more")
        self.assertEqual(marker[1], 4)

    def test_reorder_handle_uses_reverse_video_in_color_and_monochrome(self):
        entry = Entry("work", "session", Target("local", "work"), tracked=True)

        for ascii_mode, handle in ((False, "↕"), (True, ":")):
            for palette in ({"move": 123}, {}):
                with self.subTest(ascii=ascii_mode, color=bool(palette)), patch(
                    "letee.sidebar._ascii", return_value=ascii_mode
                ), patch.dict("letee.sidebar._COLOR", palette, clear=True):
                    screen = FakeScreen(size=(6, 30))
                    sidebar._draw_entries(screen, [entry], 0, 5, 30, set(), None)

                rendered = next(
                    call for call in screen.calls
                    if call[0] == "addnstr" and handle in call[3]
                )
                self.assertEqual(rendered[2], 27)
                self.assertEqual(rendered[3], f" {handle} ")
                self.assertEqual(rendered[4], 3)
                self.assertEqual(
                    rendered[5], palette.get("move", 0) | curses.A_BOLD | curses.A_REVERSE
                )

    def test_move_mode_hides_reorder_handles(self):
        entries = [
            Entry("one", "session", Target("local", "one"), tracked=True),
            Entry("two", "session", Target("local", "two"), tracked=True),
        ]

        for ascii_mode in (False, True):
            with self.subTest(ascii=ascii_mode), patch(
                "letee.sidebar._ascii", return_value=ascii_mode
            ):
                screen = FakeScreen(size=(8, 30))
                sidebar._draw_entries(
                    screen, entries, 0, 7, 30, set(), None,
                    move_source_entry=0, move_target_entry=1,
                )

            handle_column = sidebar._move_handle_bounds(30)[0]
            self.assertFalse(
                any(call[0] == "addnstr" and call[2] == handle_column for call in screen.calls)
            )

    def test_downward_move_target_line_follows_destination_session(self):
        entries = [
            Entry(name, "session", Target("local", name), tracked=True, shortcut_slot=index)
            for index, name in enumerate(("one", "two", "three"), 1)
        ]
        screen = FakeScreen(size=(10, 30))

        with (
            patch.dict("letee.sidebar._COLOR", {"move": 123}, clear=True),
            patch("letee.sidebar._ascii", return_value=False),
        ):
            sidebar._draw_entries(
                screen, entries, 0, 9, 30, set(), None,
                move_source_entry=0, move_target_entry=1,
            )

        rows = {
            name: next(call[1] for call in screen.calls if call[0] == "addnstr" and name in call[3])
            for name in ("one", "two", "three")
        }
        target_attr = next(
            call[5] for call in screen.calls if call[0] == "addnstr" and "two" in call[3]
        )
        indicators = [
            (call[1], call[3], call[5])
            for call in screen.calls
            if call[0] == "addnstr" and call[3].startswith("Click to place")
        ]
        self.assertEqual(rows, {"one": 1, "two": 3, "three": 5})
        self.assertEqual(target_attr, 123)
        self.assertEqual(indicators, [(4, "Click to place " + "─" * 15, 123)])

    def test_move_target_prompt_has_ascii_fallback(self):
        entry = Entry("work", "session", Target("local", "work"), tracked=True)
        screen = FakeScreen(size=(6, 30))

        with patch("letee.sidebar._ascii", return_value=True):
            sidebar._draw_entries(
                screen, [entry], 0, 5, 30, set(), None, move_target_entry=0
            )

        prompt = next(
            call[3]
            for call in screen.calls
            if call[0] == "addnstr" and call[3].startswith("Click to place")
        )
        self.assertEqual(prompt, "Click to place " + "-" * 15)

    def test_entry_at_row_ignores_non_selectable_and_non_entry_areas(self):
        entries = [
            Entry("LOCAL", "header"),
            Entry("offline", "unavailable"),
            Entry("", "spacer"),
            Entry("work", "session", Target("local", "work")),
            *[Entry(str(i), "session", Target("local", str(i))) for i in range(6)],
        ]

        self.assertEqual(sidebar._selectable(entries), [3, 4, 5, 6, 7, 8, 9])
        self.assertIsNone(_entry_at_row(entries, 3, 0, 8, 1))  # title
        self.assertIsNone(_entry_at_row(entries, 0, 1, 10, 1))  # header
        self.assertIsNone(_entry_at_row(entries, 0, 2, 10, 1))  # unavailable
        self.assertIsNone(_entry_at_row(entries, 0, 3, 10, 1))  # spacer
        self.assertIsNone(_entry_at_row(entries, 3, 7, 8, 1))  # footer/down marker


    def test_move_mode_keeps_normal_footer(self):
        entry = Entry("work", "session", Target("local", "work"), tracked=True)
        for ascii_mode, expected in ((False, "↵ activate"), (True, "Enter activate")):
            screen = FakeScreen(size=(10, 60))
            with self.subTest(ascii=ascii_mode), patch(
                "letee.sidebar._ascii", return_value=ascii_mode
            ):
                _draw(screen, [entry], 0, "", "", agent_entries=[], move_source_entry=0)

            footer = [
                call[3].rstrip()
                for call in screen.calls
                if call[0] == "addnstr" and call[1] == 9
            ]
            self.assertEqual(footer, [expected])

    def test_footer_fills_terminal_width(self):
        screen = FakeScreen(size=(7, 60))

        _draw(screen, [], 0, "", "")

        footer_last_columns = [call for call in screen.calls if call[0] == "chgat" and call[1] >= 5]
        self.assertEqual(footer_last_columns, [("chgat", 6, 59, 1, curses.A_BOLD | curses.A_REVERSE)])



    def test_status_renders_below_title_without_replacing_footer(self):
        screen = FakeScreen(size=(7, 30))

        _draw(screen, [], 0, "killed local:work", "", agent_entries=[])

        status = next(call for call in screen.calls if call[0] == "addnstr" and "killed local:work" in call[3])
        footer = [call[3].rstrip() for call in screen.calls if call[0] == "addnstr" and call[1] == 6]
        self.assertEqual(status[1], 1)
        self.assertEqual(footer, ["↵ activate"])

    def test_name_input_status_renders_below_name_input(self):
        screen = FakeScreen(size=(7, 30))

        _draw(screen, [], 0, "filter cleared", "work", name_input=True, adding=True)

        status = next(call for call in screen.calls if call[0] == "addnstr" and "filter cleared" in call[3])
        self.assertEqual(status[1], 2)

    def test_long_status_truncates_without_exceeding_terminal_width(self):
        screen = FakeScreen(size=(7, 12))

        _draw(screen, [], 0, "failed: " + "界" * 20, "")

        status = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 1)
        self.assertLessEqual(sidebar._cell_width(status[3]), 12)

    def test_sessions_and_agents_share_minimal_footer_with_ascii_fallback(self):
        removed_hints = ("Tab", "resize", "add", "remove", "kill", "reorder", "K/J", "[ / ]")
        for ascii_mode, expected in (
            (False, ["↵ activate"]),
            (True, ["Enter activate"]),
        ):
            footers = []
            for focused_region in ("sessions", "agents"):
                screen = FakeScreen(size=(10, 60))
                with self.subTest(ascii=ascii_mode, focused_region=focused_region), patch(
                    "letee.sidebar._ascii", return_value=ascii_mode
                ):
                    _draw(
                        screen, [], 0, "hidden status", "", agent_entries=[], focused_region=focused_region
                    )
                footer = [
                    call[3].rstrip()
                    for call in screen.calls
                    if call[0] == "addnstr" and call[1] >= 9
                ]
                self.assertEqual(footer, expected)
                self.assertFalse(any(hint in " ".join(footer) for hint in removed_hints))
                footers.append(footer)
            self.assertEqual(footers[0], footers[1])

    def test_name_input_uses_two_instruction_rows_with_ascii_fallback(self):
        for ascii_mode, expected in (
            (False, ["type a name  backspace edit", "esc back  ↵ create or switch"]),
            (True, ["type a name  backspace edit", "esc back  Enter create or switch"]),
        ):
            screen = FakeScreen(size=(7, 60))
            with self.subTest(ascii=ascii_mode), patch("letee.sidebar._ascii", return_value=ascii_mode):
                _draw(screen, [], 0, "ignored", "", name_input=True, adding=True)
            footer = [call[3].rstrip() for call in screen.calls if call[0] == "addnstr" and call[1] >= 5]
            self.assertEqual(footer, expected)

    def test_footer_reuses_title_style_and_dims_inactive_pane(self):
        screen = FakeScreen(size=(7, 60))
        with patch.dict("letee.sidebar._COLOR", {"title": 123}, clear=True):
            _draw(screen, [], 0, "", "", dimmed=True)
        footer = [call for call in screen.calls if call[0] == "addnstr" and call[1] >= 5]
        self.assertTrue(all(call[5] == _fade(123) for call in footer))

    def test_footer_monochrome_fallback_is_bold_reverse_video(self):
        screen = FakeScreen(size=(7, 60))
        with patch.dict("letee.sidebar._COLOR", {}, clear=True):
            _draw(screen, [], 0, "", "")
        footer = [call for call in screen.calls if call[0] == "addnstr" and call[1] >= 5]
        self.assertTrue(all(call[5] & curses.A_BOLD and call[5] & curses.A_REVERSE for call in footer))


    def test_read_key_shows_confirmation_below_title_and_preserves_footer(self):
        screen = FakeScreen(size=(5, 20))
        footer = "footer shortcuts"
        screen.addnstr(4, 0, footer, 19)
        screen.calls.clear()

        self.assertEqual(_read_key(screen, "kill work? y/N"), ord("y"))

        prompt = next(call for call in screen.calls if call[0] == "addnstr" and "kill work" in call[3])
        self.assertEqual(prompt[1], 1)
        self.assertFalse(any(call[0] == "addnstr" and call[1] == 4 for call in screen.calls))
        self.assertEqual(screen.calls[0], ("timeout", -1))
        self.assertEqual(screen.calls[-1], ("timeout", 50))

    def test_read_key_places_confirmation_below_filter_and_truncates_safely(self):
        screen = FakeScreen(size=(6, 12))

        with patch("letee.sidebar._ascii", return_value=False):
            _read_key(screen, "kill session-with-a-long-name? y/N", name_input=True)

        prompt = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 2 and call[3].strip())
        self.assertLessEqual(sidebar._cell_width(prompt[3]), 12)
        self.assertTrue(prompt[5] & curses.A_BOLD)

    def test_agent_confirmation_row_matches_agents_divider(self):
        entries = [Entry("pi", "agent", status="working")]
        for name_input, agent_percentage in ((False, 40), (False, 80), (True, 40), (True, 50)):
            with self.subTest(name_input=name_input, agent_percentage=agent_percentage):
                screen = FakeScreen(size=(20, 40))
                footer_height, _ = _draw(
                    screen,
                    [],
                    0,
                    "",
                    "",
                    name_input=name_input,
                    agent_entries=entries,
                    agent_percentage=agent_percentage,
                )
                divider = next(
                    call for call in screen.calls
                    if call[0] == "addnstr" and call[3].startswith("AGENTS ")
                )
                self.assertEqual(
                    sidebar._agent_prompt_row(screen, footer_height, entries, agent_percentage, name_input),
                    divider[1] + 2,
                )

    def test_add_button_cursor_does_not_replace_selected_session_slot(self):
        screen = FakeScreen(size=(7, 40))
        target = Target("local", "work")
        entries = [Entry("work", "session", target, host="localhost", tracked=True, shortcut_slot=1)]

        with patch("letee.sidebar._ascii", return_value=True):
            _draw(screen, entries, 0, "", "", add_button_selected=True)

        title = "".join(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        session_badge = next(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 2 and call[2] == 0)
        self.assertIn("> add", title)
        self.assertEqual(session_badge, "[1]")

    def test_inactive_sidebar_hides_add_button_pointer(self):
        screen = FakeScreen(size=(7, 40))

        with patch("letee.sidebar._ascii", return_value=True):
            _draw(screen, [], 0, "", "", add_button_selected=True, pane_active=False)

        title = "".join(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertIn("add", title)
        self.assertNotIn("> add", title)

    def test_title_adds_terminal_icon_with_ascii_fallback(self):
        screen = FakeScreen(size=(5, 40))

        with patch("letee.sidebar._ascii", return_value=False):
            _draw(screen, [], 0, "ok", "")
        title = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertTrue(title[3].startswith(" ‹  letee"))

        screen = FakeScreen(size=(5, 40))
        with patch("letee.sidebar._ascii", return_value=True):
            _draw(screen, [], 0, "ok", "")
        title = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertTrue(title[3].startswith(" < letee"))

    def test_draw_erases_without_forcing_full_repaint(self):
        screen = FakeScreen(size=(5, 40))

        _draw(screen, [], 0, "ok", "")

        self.assertEqual(screen.calls[0], ("erase",))
        self.assertNotIn(("clear",), screen.calls)

    def test_title_forces_terminal_line_redraw_after_count_changes(self):
        screen = FakeScreen(size=(5, 40))

        _draw(screen, [Entry("work", "session", Target("local", "work"))], 0, "", "", dimmed=True)

        title = next(i for i, call in enumerate(screen.calls) if call[0] == "addnstr" and call[1] == 0)
        redraw = screen.calls.index(("redrawln", 0, 1))
        self.assertLess(title, redraw)

    def test_normal_title_shows_brand_without_session_count(self):
        screen = FakeScreen(size=(5, 40))
        entries = [
            Entry("LOCAL", "header"),
            Entry("work", "session", Target("local", "work")),
            Entry("notes", "session", Target("local", "notes")),
            Entry("new local", "create", host=""),
        ]

        _draw(screen, entries, 1, "ok", "")

        title_text = "".join(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertIn("letee", title_text)
        self.assertIn("＋ add", title_text)
        self.assertNotIn("2 sessions", title_text)

    def test_titles_never_show_session_or_match_counts(self):
        screen = FakeScreen(size=(5, 40))
        entries = [Entry("work", "session", Target("local", "work"))]

        _draw(screen, entries, 0, "ok", "")
        normal_text = "".join(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertIn("letee", normal_text)
        self.assertIn("＋ add", normal_text)
        self.assertNotIn("1 session", normal_text)

        screen = FakeScreen(size=(6, 40))
        _draw(screen, entries, 0, "filtering", "work", name_input=True, adding=True)
        add_text = "".join(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        filter_row = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 1)
        self.assertIn("letee", add_text)
        self.assertNotIn("1 match", add_text)
        self.assertTrue(filter_row[3].startswith(" Name: work"))

    def test_name_input_uses_dedicated_full_width_row(self):
        screen = FakeScreen(size=(6, 40))
        entries = [Entry("work", "session", Target("local", "work"))]

        _draw(screen, entries, 0, "filtering", "work", name_input=True, adding=True)

        title = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        filter_row = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 1)
        self.assertNotIn("work", title[3])
        self.assertEqual(filter_row[3], " Name: work" + " " * 29)
        self.assertEqual(filter_row[4], 40)
        self.assertIn(("move", 1, len(" Name: work")), screen.calls)

    def test_session_rows_use_last_available_column(self):
        screen = FakeScreen(size=(7, 20))
        entry = Entry("x" * 40, "session", Target("local", "work"))

        _draw(screen, [entry], 0, "", "")

        row = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 2)
        self.assertEqual(row[4], 20)
        self.assertEqual(sidebar._cell_width(row[3]), 20)

    def test_empty_name_input_has_visible_input_position(self):
        screen = FakeScreen(size=(6, 20))

        _draw(screen, [], 0, "filtering", "", name_input=True, adding=True)

        filter_row = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 1)
        self.assertTrue(filter_row[3].startswith(" Name: "))
        self.assertIn(("move", 1, len(" Name: ")), screen.calls)

    def test_narrow_name_input_drops_count_before_clipping_query(self):
        screen = FakeScreen(size=(5, 16))

        _draw(screen, [Entry("work", "session", Target("local", "work"))], 0, "filtering", "abcdefghij", name_input=True, adding=True)

        title = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        filter_row = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 1)
        self.assertNotIn("abcdefghij", title[3])
        self.assertEqual(filter_row[3], " Name: abcdefgh…")
        cursor = next(call for call in screen.calls if call[0] == "move")
        self.assertEqual(cursor, ("move", 1, 15))

    def test_title_colors_final_terminal_column(self):
        screen = FakeScreen(size=(5, 20))

        _draw(screen, [], 0, "ok", "")

        title_calls = [call for call in screen.calls if call[0] == "addnstr" and call[1] == 0]
        self.assertEqual(len(title_calls), 2)
        self.assertEqual(title_calls[0][4], 14)
        self.assertEqual(title_calls[1][4], 6)

    def test_title_uses_configured_style_and_dims_inactive_pane(self):
        screen = FakeScreen(size=(5, 20))

        with patch.dict("letee.sidebar._COLOR", {"title": 123}, clear=True):
            _draw(screen, [], 0, "ok", "", dimmed=True)

        title_calls = [call for call in screen.calls if call[0] == "addnstr" and call[1] == 0]
        main_title = title_calls[0]
        self.assertEqual(main_title[4], 14)
        self.assertEqual(main_title[5], _fade(123))

    def test_title_monochrome_fallback_is_bold_reverse_video(self):
        screen = FakeScreen(size=(5, 20))

        with patch.dict("letee.sidebar._COLOR", {}, clear=True):
            _draw(screen, [], 0, "ok", "")

        title = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertTrue(title[5] & curses.A_BOLD)
        self.assertTrue(title[5] & curses.A_REVERSE)

    def test_bell_targets_combines_cockpit_local_and_remote_bells(self):
        discovered = snapshot(
            local=("notes",),
            local_bells=("notes",),
            remotes={"dev": source("ssh", ("chat",), ("chat",), "dev")},
        )
        self.assertEqual(
            _bell_targets(discovered, Target("local", "work")),
            {Target("local", "work"), Target("local", "notes"), Target("ssh", "chat", "dev")},
        )

    def test_draw_marks_matching_bell_target(self):
        screen = FakeScreen(size=(8, 40))
        target = Target("ssh", "work", "dev")

        _draw(screen, [Entry("work", "session", target)], 0, "", "", bell_targets={target})

        self.assertTrue(any(call[0] == "addnstr" and "work 🔔" in call[3] for call in screen.calls))

    def test_draw_does_not_mark_current_target_bell(self):
        screen = FakeScreen()
        target = Target("local", "work")

        _draw(screen, [Entry("work", "session", target)], 0, "", "", bell_targets={target}, current_target=target)

        self.assertFalse(any(call[0] == "addnstr" and "🔔" in call[3] for call in screen.calls))




    def test_injected_region_keys_select_exact_region_and_tab_does_nothing(self):
        for keys, expected in (
            ([curses.KEY_F7, STOP], "agents"),
            ([curses.KEY_F7, curses.KEY_F6, STOP], "sessions"),
            ([9, STOP], "sessions"),
        ):
            screen = FakeScreen(keys, size=(10, 40))
            with (
                self.subTest(keys=keys),
                patch("letee.sidebar.curses.curs_set"),
                patch("letee.sidebar._init_colors"),
                patch("letee.sidebar._entries", return_value=[]),
                patch("letee.sidebar._bell_targets", return_value=set()),
                patch("letee.cockpit.current_target", return_value=None),
                patch("letee.sidebar._draw", return_value=(2, None)) as draw,
            ):
                run(screen)

            self.assertEqual(draw.call_args_list[-1].args[14], expected)

    def test_removed_focused_keys_do_nothing(self):
        for key in map(ord, "q?/a"):
            with self.subTest(key=chr(key)):
                screen = FakeScreen([key, STOP], size=(10, 40))
                with (
                    patch("letee.sidebar.curses.curs_set"),
                    patch("letee.sidebar._init_colors"),
                    patch("letee.sidebar._entries", return_value=[]),
                    patch("letee.sidebar._bell_targets", return_value=set()),
                    patch("letee.cockpit.current_target", return_value=None),
                    patch("letee.sidebar._draw", return_value=(2, None)) as draw,
                    patch.object(sidebar, "_transition", wraps=sidebar._transition) as transition,
                ):
                    run(screen)

                self.assertEqual(screen.calls.count(("getch",)), 2)
                self.assertFalse(any(call.args[10] for call in draw.call_args_list))
                self.assertFalse(any(call.args[1] in ("help", "quit") for call in transition.call_args_list))

    def test_h_and_l_leave_agent_ordering_unchanged(self):
        for key in map(ord, "hl"):
            with self.subTest(key=key):
                screen = FakeScreen([curses.KEY_F7, key, STOP], size=(10, 40))
                with (
                    patch("letee.sidebar.curses.curs_set"),
                    patch("letee.sidebar._init_colors"),
                    patch("letee.sidebar._entries", return_value=[]),
                    patch("letee.sidebar._bell_targets", return_value=set()),
                    patch("letee.cockpit.current_target", return_value=None),
                    patch("letee.sidebar._draw", return_value=(2, None)) as draw,
                ):
                    run(screen)

                self.assertEqual(draw.call_args_list[-1].kwargs["agent_ordering"], "priority")

    def test_left_and_right_cycle_agent_ordering(self):
        for key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            with self.subTest(key=key):
                screen = FakeScreen([curses.KEY_F7, key, STOP], size=(10, 40))
                with (
                    patch("letee.sidebar.curses.curs_set"),
                    patch("letee.sidebar._init_colors"),
                    patch("letee.sidebar._entries", return_value=[]),
                    patch("letee.sidebar._bell_targets", return_value=set()),
                    patch("letee.cockpit.current_target", return_value=None),
                    patch("letee.sidebar._draw", return_value=(2, None)) as draw,
                ):
                    run(screen)

                self.assertEqual(draw.call_args_list[-1].kwargs["agent_ordering"], "session")

    def test_f11_opens_add_menu(self):
        screen = FakeScreen([curses.KEY_F11, STOP], size=(10, 40))

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._entries", return_value=[]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar._draw", return_value=(2, None)) as draw,
        ):
            run(screen)

        self.assertTrue(any(call.args[10] for call in draw.call_args_list))

    def test_up_at_top_of_add_menu_selects_back(self):
        screen = FakeScreen([curses.KEY_F11, curses.KEY_UP, STOP])

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._entries", return_value=[Entry("work", "session", Target("local", "work"))]),
            patch("letee.sidebar._available_locations", return_value=[("localhost", "")]),
            patch(
                "letee.sidebar._search_entries",
                return_value=[Entry("work", "session", Target("local", "work"))],
            ),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar._draw", return_value=(2, None)) as draw,
        ):
            run(screen)

        add_draws = [call for call in draw.call_args_list if call.args[10]]
        self.assertTrue(add_draws)
        self.assertTrue(any(call.kwargs["add_button_selected"] for call in add_draws))

    def test_down_from_selected_back_returns_to_first_add_entry(self):
        screen = FakeScreen([curses.KEY_F11, curses.KEY_UP, curses.KEY_DOWN, STOP])

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._entries", return_value=[Entry("work", "session", Target("local", "work"))]),
            patch("letee.sidebar._available_locations", return_value=[("localhost", "")]),
            patch(
                "letee.sidebar._search_entries",
                return_value=[Entry("work", "session", Target("local", "work"))],
            ),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar._draw", return_value=(2, None)) as draw,
        ):
            run(screen)

        add_draws = [call for call in draw.call_args_list if call.args[10]]
        self.assertTrue(add_draws)
        self.assertFalse(add_draws[-1].kwargs["add_button_selected"])
        self.assertEqual(add_draws[-1].args[2], 0)

    def test_enter_on_selected_back_returns_to_sessions(self):
        screen = FakeScreen([curses.KEY_F11, curses.KEY_UP, curses.KEY_ENTER, STOP])

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._entries", return_value=[Entry("work", "session", Target("local", "work"))]),
            patch("letee.sidebar._available_locations", return_value=[("localhost", "")]),
            patch(
                "letee.sidebar._search_entries",
                return_value=[Entry("work", "session", Target("local", "work"))],
            ),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar._draw", return_value=(2, None)),
            patch.object(sidebar, "_add_back", wraps=sidebar._add_back) as add_back,
        ):
            run(screen)

        add_back.assert_called_once()
        self.assertIsNone(add_back.call_args.args[0].add_view)

    def test_session_name_typing_handles_queued_keys_without_background_polling(self):
        screen = FakeScreen([curses.KEY_F11, curses.KEY_ENTER, *map(ord, "fast"), 27, STOP])
        poller = unittest.mock.Mock(launch_id=None, )
        poller.snapshot = snapshot()
        poller.tick.return_value = False

        with (
            patch("letee.sidebar.DiscoveryPoller", return_value=poller),
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._init_colors"),
            patch("letee.cockpit.current_target", return_value=None) as current_target,
            patch("letee.sidebar._pane_active", return_value=True) as pane_active,
        ):
            run(screen)

        self.assertTrue(any(call[0] == "addnstr" and "fast" in call[3] for call in screen.calls))
        self.assertLessEqual(current_target.call_count, 2)
        self.assertLessEqual(pane_active.call_count, 1)

    def test_e_opens_prefilled_editor_and_renames_selected_session(self):
        old = Target("local", "old")
        renamed = Target("local", "new")
        screen = FakeScreen([ord("e"), curses.KEY_BACKSPACE, curses.KEY_BACKSPACE, curses.KEY_BACKSPACE, *map(ord, "new"), 10, STOP], size=(10, 40))
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=snapshot(local=("old",)), current_target=old, bell_target=None,
            current_agent=None, pane_active=True,
        )
        poller.tick.return_value = False
        with (
            patch("letee.sidebar.DiscoveryPoller", return_value=poller),
            patch("letee.sidebar.load_hosts", return_value=[]),
            patch("letee.config.load_sessions", return_value=[old]),
            patch("letee.cockpit.current_target", return_value=old),
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._mouse_mask"),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar.sessions.rename", return_value=renamed) as rename,
            patch("letee.sidebar.cockpit.rename_target"),
            patch("letee.config.save_sessions"),
        ):
            run(screen)

        rename.assert_called_once_with(old, "new")
        self.assertTrue(any(call[0] == "addnstr" and "Rename session" in call[3] for call in screen.calls))
        self.assertTrue(any(call[0] == "addnstr" and "new" in call[3] for call in screen.calls))

    def test_e_rejects_absent_local_and_remote_sessions_with_matching_prefix(self):
        for target, data in (
            (Target("local", "work"), snapshot(local=("work-other",))),
            (Target("ssh", "work", "dev"), snapshot(remotes={
                "dev": source("ssh", ("work-other",), host="dev"),
            })),
        ):
            with self.subTest(target=target):
                screen = FakeScreen([ord("e"), *map(ord, "new"), 10, STOP], size=(10, 80))
                poller = unittest.mock.Mock(launch_id=None,
                    snapshot=data, current_target=None, bell_target=None,
                    current_agent=None, pane_active=True,
                )
                poller.tick.return_value = False
                with (
                    patch("letee.sidebar.DiscoveryPoller", return_value=poller),
                    patch("letee.sidebar.load_hosts", return_value=["dev"]),
                    patch("letee.config.load_sessions", return_value=[target]),
                    patch("letee.cockpit.current_target", return_value=None),
                    patch("letee.sidebar.curses.curs_set"),
                    patch("letee.sidebar._mouse_mask"),
                    patch("letee.sidebar._init_colors"),
                    patch("letee.sidebar.sessions.rename") as rename,
                    patch("letee.sidebar._start_rename", wraps=sidebar._start_rename) as start_rename,
                    patch("letee.sidebar.cockpit.switch"),
                ):
                    run(screen)

                start_rename.assert_not_called()
                rename.assert_not_called()
                self.assertTrue(any(
                    call[0] == "addnstr" and "Session unavailable; rename a running session" in call[3]
                    for call in screen.calls
                ))

    def test_queued_input_is_handled_before_automatic_session_actions(self):
        screen = FakeScreen([STOP])
        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._init_colors"),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar._sync_active_session") as sync_active_session,
        ):
            run(screen)

        sync_active_session.assert_not_called()

    def test_rename_refresh_does_not_recover_active_session_from_stale_snapshot(self):
        for old, renamed, stale in (
            (
                Target("local", "old"), Target("local", "renamed"),
                snapshot(local=("old",)),
            ),
            (
                Target("ssh", "old", "dev"), Target("ssh", "renamed", "dev"),
                snapshot(remotes={"dev": source("ssh", ("old",), host="dev")}),
            ),
        ):
            with self.subTest(kind=old.kind):
                poller = unittest.mock.Mock(launch_id=None,
                    snapshot=stale, current_target=renamed,
                    bell_target=None, current_agent=None, pane_active=True,
                    refresh_pending=True,
                )
                poller.tick.return_value = False
                screen = FakeScreen([-1, STOP], size=(10, 40))
                with (
                    patch("letee.sidebar.AsyncStatusPoller", return_value=poller),
                    patch("letee.sidebar.DiscoveryPoller"),
                    patch("letee.sidebar.load_hosts", return_value=[]),
                    patch("letee.config.load_sessions", return_value=[renamed]),
                    patch("letee.cockpit.current_target", return_value=renamed),
                    patch("letee.sidebar.curses.curs_set"),
                    patch("letee.sidebar._init_colors"),
                    patch("letee.sidebar._mouse_mask"),
                    patch("letee.sidebar._draw", return_value=(2, None)),
                    patch("letee.sidebar._sync_active_session") as sync_active_session,
                ):
                    run(screen)

                sync_active_session.assert_not_called()

    def test_run_sets_timeout_and_refreshes_on_timeout(self):
        screen = FakeScreen([-1, STOP])
        calls = []

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._entries", side_effect=lambda *args: calls.append(args) or [Entry("work", "session", Target("local", "work"))]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
        ):
            run(screen)

        self.assertIn(("timeout", 50), screen.calls)
        self.assertEqual(len(calls), 1)

    def test_agent_duration_redraws_each_second_and_restart_reads_active_agent(self):
        pane = PaneTarget(Target("local", "work"), "@1", "%2", "/tmp/tmux")
        poller = unittest.mock.Mock(launch_id=None, )
        poller.snapshot = SessionSnapshot(
            SourceSnapshot(True, (pane.target,), frozenset(), agents=(AgentEntry(pane, "id", "pi", "working"),)),
            {},
        )
        poller.tick.return_value = False
        screen = FakeScreen([-1, -1, STOP], size=(10, 40))
        with (
            patch("letee.sidebar.DiscoveryPoller", return_value=poller),
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._init_colors"),
            patch("letee.config.load_sessions", return_value=[pane.target]),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.sidebar.cockpit.current_agent", return_value="id"),
            patch("letee.sidebar.time.monotonic", side_effect=[100, 100.1]),
            patch("letee.sidebar._draw", return_value=(2, None)) as draw,
        ):
            run(screen)

        self.assertEqual(draw.call_count, 2)
        self.assertTrue(all(call.args[-1] in (None, "id") for call in draw.call_args_list))

    def test_working_agent_redraws_at_spinner_rate(self):
        pane = PaneTarget(Target("local", "work"), "@1", "%2", "/tmp/tmux")
        poller = unittest.mock.Mock(launch_id=None, )
        poller.snapshot = SessionSnapshot(SourceSnapshot(True, (pane.target,), frozenset(), agents=(AgentEntry(pane, "id", "pi", "working"),)), {})
        poller.tick.return_value = False
        screen = FakeScreen([-1, -1, -1, -1, STOP], size=(10, 40))
        with (
            patch("letee.sidebar.DiscoveryPoller", return_value=poller),
            patch("letee.sidebar.curses.curs_set"), patch("letee.sidebar._init_colors"),
            patch("letee.config.load_sessions", return_value=[pane.target]),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar.time.monotonic", side_effect=[0, 0.05, 0.1]),
            patch("letee.sidebar._draw", return_value=(2, None)) as draw,
        ):
            run(screen)
        self.assertEqual(draw.call_count, 2)

    def test_idle_ui_ticks_do_not_redraw_sidebar(self):
        screen = FakeScreen([-1, -1, STOP])

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._entries", return_value=[Entry("work", "session", Target("local", "work"))]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar._draw", return_value=(2, None)) as draw,
        ):
            run(screen)

        draw.assert_called_once()

    def test_rapid_ui_ticks_do_not_accelerate_cockpit_bell_polling(self):
        screen = FakeScreen([-1, -1, -1, STOP])
        poller = unittest.mock.Mock(launch_id=None, )
        poller.snapshot = snapshot()
        poller.tick.return_value = False

        with (
            patch("letee.sidebar.DiscoveryPoller", return_value=poller),
            patch("letee.sidebar.time.monotonic", side_effect=[0, 0.1, 0.49, 0.5]),
            patch("letee.sidebar.cockpit.bell_target", return_value=None) as bell_target,
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._init_colors"),
            patch("letee.cockpit.current_target", return_value=None),
        ):
            run(screen)

        self.assertLessEqual(bell_target.call_count, 2)
        self.assertLessEqual(poller.tick.call_count, 4)

    def test_run_beeps_once_for_new_background_bell(self):
        screen = FakeScreen([-1, -1, STOP])
        target = Target("local", "work")

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.beep") as beep,
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._entries", return_value=[Entry("work", "session", target)]),
            patch("letee.sidebar._bell_targets", return_value={target}),
            patch("letee.cockpit.current_target", return_value=Target("local", "shell")),
            patch("letee.sidebar.cockpit.show_unavailable"),
        ):
            run(screen)

        beep.assert_called_once_with()

    def test_switching_away_from_ringing_current_target_does_not_beep(self):
        screen = FakeScreen([-1, STOP])
        ringing = Target("local", "a")
        current = Target("local", "b")

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.beep") as beep,
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._entries", return_value=[Entry("a", "session", ringing)]),
            patch("letee.sidebar._bell_targets", return_value={ringing}),
            patch("letee.cockpit.current_target", side_effect=[ringing, ringing, current]),
            patch("letee.sidebar.cockpit.show_unavailable"),
        ):
            run(screen)

        beep.assert_not_called()


    def test_agent_press_switches_and_release_does_not_duplicate(self):
        target = Target("ssh", "work", "dev")
        first = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        second = PaneTarget(target, "@2", "%2", "/tmp/tmux")
        agents = (
            AgentEntry(first, "first", "pi-one", "working"),
            AgentEntry(second, "second", "pi-two", "idle"),
        )
        poller = unittest.mock.Mock(launch_id=None, )
        poller.snapshot = SessionSnapshot(
            SourceSnapshot(True, (target,), frozenset(), agents=agents), {}
        )
        poller.tick.return_value = False
        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, STOP], size=(20, 30))

        with (
            patch("letee.sidebar.DiscoveryPoller", return_value=poller),
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch(
                "letee.sidebar.curses.getmouse",
                side_effect=[
                    (0, 0, 17, 0, curses.BUTTON1_PRESSED),
                    (0, 0, 17, 0, curses.BUTTON1_RELEASED),
                ],
            ),
            patch("letee.sidebar._init_colors"),
            patch("letee.config.load_sessions", return_value=[target]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar.cockpit.switch") as switch,
        ):
            run(screen)

        _assert_deferred_switch(self, switch, target, "second")
        self.assertEqual(switch.call_args.args[1](), sidebar.sessions.pane_attach_command(second))

    def test_body_press_activates_and_incidental_motion_does_not_start_move(self):
        target = Target("local", "one")
        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, STOP], size=(12, 30))
        move_targets = []

        def draw_spy(*args, **kwargs):
            move_targets.append(kwargs.get("move_target_entry"))
            return (2, None)

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch(
                "letee.sidebar.curses.getmouse",
                side_effect=[
                    (0, 0, 2, 0, curses.BUTTON1_PRESSED),
                    (0, 1, 2, 0, curses.REPORT_MOUSE_POSITION),
                ],
            ),
            patch("letee.sidebar._init_colors"),
            patch("letee.config.load_sessions", return_value=[target]),
            patch("letee.sidebar._entries", return_value=[Entry("one", "session", target, tracked=True)]),
            patch("letee.sidebar._agent_entries", return_value=[]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar._draw", side_effect=draw_spy),
            patch("letee.sidebar.cockpit.switch") as switch,
        ):
            run(screen)

        switch.assert_called_once()
        self.assertEqual(move_targets, [None, None])

    def test_unfocused_body_press_activates_without_waiting_for_focus_poll(self):
        target = Target("local", "one")
        entries = [Entry("one", "session", target, tracked=True)]
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=snapshot(local=("one",)), current_target=None, bell_target=None,
            current_agent=None, pane_active=False,
        )
        poller.tick.return_value = False
        screen = FakeScreen([curses.KEY_MOUSE, STOP], size=(12, 30))

        with (
            patch("letee.sidebar.AsyncStatusPoller", return_value=poller),
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.getmouse", return_value=(0, 0, 2, 0, curses.BUTTON1_PRESSED)),
            patch("letee.sidebar._init_colors"),
            patch("letee.config.load_sessions", return_value=[target]),
            patch("letee.sidebar._entries", return_value=entries),
            patch("letee.sidebar._agent_entries", return_value=[]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar.cockpit.switch") as switch,
        ):
            run(screen)

        _assert_deferred_switch(self, switch, target)

    def test_slow_session_switch_marks_target_active_before_action_completes(self):
        first = Target("local", "one")
        second = Target("ssh", "two", "dev")
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=snapshot(local=("one",), remotes={"dev": source("ssh", ("two",), host="dev")}),
            current_target=first,
            bell_target=None,
            current_agent=None,
            pane_active=True,
        )
        poller.tick.return_value = False
        release = threading.Event()
        active_targets = []
        screen = FakeScreen([curses.KEY_DOWN, curses.KEY_ENTER, -1, STOP], size=(12, 30))

        def draw_spy(*args, **kwargs):
            active_targets.append(args[7])
            if args[7] == second:
                release.set()
            return (2, None)

        with (
            patch("letee.sidebar.AsyncStatusPoller", return_value=poller),
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._init_colors"),
            patch("letee.config.load_sessions", return_value=[first, second]),
            patch("letee.sidebar._draw", side_effect=draw_spy),
            patch("letee.sidebar.cockpit.switch", side_effect=lambda *_: release.wait(0.2)),
        ):
            run(screen)

        self.assertIn(second, active_targets)

    def test_slow_agent_switch_marks_agent_active_before_action_completes(self):
        target = Target("ssh", "work", "dev")
        pane = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=SessionSnapshot(
                SourceSnapshot(True, (), frozenset()),
                {"dev": SourceSnapshot(True, (target,), frozenset(), agents=(AgentEntry(pane, "id", "pi", "working"),))},
            ),
            current_target=None,
            bell_target=None,
            current_agent=None,
            pending_agent=None,
            pane_active=True,
        )
        poller.tick.return_value = False
        release = threading.Event()
        active_agents = []
        screen = FakeScreen([curses.KEY_F7, curses.KEY_DOWN, curses.KEY_ENTER, -1, STOP], size=(12, 30))

        def draw_spy(*args, **kwargs):
            active_agents.append(args[16])
            if args[16] == "id":
                release.set()
            return (2, None)

        with (
            patch("letee.sidebar.AsyncStatusPoller", return_value=poller),
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._init_colors"),
            patch("letee.config.load_sessions", return_value=[target]),
            patch("letee.sidebar._draw", side_effect=draw_spy),
            patch("letee.sidebar.cockpit.switch", side_effect=lambda *_: release.wait(0.2)),
        ):
            run(screen)

        self.assertIn("id", active_agents)

    def test_completed_agent_switch_does_not_restore_stale_focused_agent(self):
        target = Target("local", "work")
        first_pane = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        second_pane = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        agents = (
            AgentEntry(first_pane, "first", "pi-one", "working"),
            AgentEntry(second_pane, "second", "pi-two", "working"),
        )
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=SessionSnapshot(
                SourceSnapshot(
                    True,
                    (target,),
                    frozenset(),
                    agents=agents,
                    focused_panes=frozenset({first_pane}),
                ),
                {},
            ),
            current_target=target,
            bell_target=None,
            current_agent="first",
            startup_deadline=None,
            pending_agent=None,
            pane_active=True,
        )
        poller.tick.return_value = False
        poller.observe_effect.side_effect = lambda result: setattr(
            poller, "current_agent", result.effect.message
        )
        active_agents = []
        screen = FakeScreen(
            [curses.KEY_F7, curses.KEY_DOWN, curses.KEY_ENTER, -1, -1, -1, STOP],
            size=(14, 30),
        )

        result = sidebar.EffectResult(
            Effect("switch_pane", second_pane, message="second"), (target,)
        )
        runner = unittest.mock.Mock(blocks_favorite_changes=False, busy=False)
        runner.submit.return_value = True
        runner.poll.side_effect = [None] * 5 + [result]

        with (
            patch("letee.sidebar.AsyncStatusPoller", return_value=poller),
            patch("letee.sidebar.EffectRunner", return_value=runner),
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._init_colors"),
            patch("letee.config.load_sessions", return_value=[target]),
            patch(
                "letee.sidebar._draw",
                side_effect=lambda *args, **kwargs: active_agents.append(args[16]) or (2, None),
            ),
        ):
            run(screen)

        second_selected = active_agents.index("second")
        self.assertNotIn("first", active_agents[second_selected:])

    def test_tracking_dispatch_returns_while_another_process_holds_lock(self):
        first, second = Target("local", "one"), Target("local", "two")
        for key, expected in ((ord("r"), [second]), (ord("J"), [second, first])):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                with patch.dict(os.environ, {"LETEE_CONFIG_DIR": directory}):
                    config.save_sessions([first, second])
                    holder = subprocess.Popen(
                        [os.sys.executable, "-c",
                         "from letee import config; import sys\n"
                         "with config.sessions_lock():\n"
                         " print('locked', flush=True)\n"
                         " sys.stdin.readline()\n"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                    )
                    self.assertEqual(holder.stdout.readline().strip(), "locked")
                    timed_out = threading.Event()

                    def release_lock():
                        holder.stdin.write("release\n")
                        holder.stdin.flush()

                    def timeout():
                        timed_out.set()
                        release_lock()

                    timer = threading.Timer(2, timeout)
                    returned = []
                    state = SidebarState(favorites=[first, second], selected_target=first)

                    class Screen(FakeScreen):
                        def getch(inner):
                            if not inner.keys:
                                returned.append(not timed_out.is_set())
                                self.assertEqual(state.status, "another action is still changing sessions")
                                timer.cancel()
                                release_lock()
                                return STOP
                            return super().getch()

                    poller = unittest.mock.Mock(launch_id=None,
                        snapshot=snapshot(local=("one", "two")), current_target=None,
                        bell_target=None, current_agent=None, pane_active=True,
                    )
                    poller.tick.return_value = False
                    timer.start()
                    try:
                        with (
                            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
                            patch.object(sidebar, "SidebarState", return_value=state),
                            patch.object(sidebar.curses, "curs_set"),
                            patch.object(sidebar, "_init_colors"),
                            patch.object(sidebar.cockpit, "current_target", return_value=None),
                            patch.object(sidebar, "_draw", return_value=(2, None)),
                        ):
                            run(Screen([key, ord("r"), ord("J")], size=(12, 30)))
                    finally:
                        timer.cancel()
                        if holder.poll() is None:
                            holder.terminate()
                        holder.wait(timeout=5)
                        holder.stdin.close()
                        holder.stdout.close()
                    self.assertEqual(returned, [True])
                    self.assertEqual(config.load_sessions(), expected)

    def test_rejected_tracking_dispatch_rolls_back_optimistic_changes(self):
        first, second = Target("local", "one"), Target("local", "two")
        for key in (ord("r"), ord("J")):
            with self.subTest(key=key):
                state = SidebarState(favorites=[first, second], selected_target=first)
                poller = unittest.mock.Mock(launch_id=None,
                    snapshot=snapshot(local=("one", "two")), current_target=None,
                    bell_target=None, current_agent=None, pane_active=True,
                )
                poller.tick.return_value = False
                runner = unittest.mock.Mock(busy=True, blocks_favorite_changes=False)
                runner.submit.return_value = False
                runner.poll.return_value = None
                with (
                    patch.object(sidebar, "SidebarState", return_value=state),
                    patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
                    patch.object(sidebar, "EffectRunner", return_value=runner),
                    patch.object(sidebar.curses, "curs_set"),
                    patch.object(sidebar, "_init_colors"),
                    patch.object(config, "load_sessions", return_value=[first, second]),
                    patch.object(sidebar, "_draw", return_value=(2, None)),
                    patch.object(config, "save_sessions") as save,
                ):
                    run(FakeScreen([key, STOP], size=(12, 30)))
                runner.submit.assert_called_once()
                save.assert_not_called()
                self.assertEqual(state.favorites, [first, second])
                self.assertEqual(state.selected_target, first)
                self.assertEqual(state.status, "another action is still running")

    def test_adjacent_move_handle_columns_start_move(self):
        target = Target("local", "one")
        entry = Entry("one", "session", target, tracked=True)

        for column in (27, 29):
            with self.subTest(column=column):
                screen = FakeScreen([curses.KEY_MOUSE, STOP], size=(12, 30))
                with (
                    patch("letee.sidebar.curses.curs_set"),
                    patch("letee.sidebar.curses.getmouse", return_value=(0, column, 2, 0, curses.BUTTON1_PRESSED)),
                    patch("letee.sidebar._mouse_mask") as mouse_mask,
                    patch("letee.sidebar._init_colors"),
                    patch("letee.config.load_sessions", return_value=[target]),
                    patch("letee.sidebar._entries", return_value=[entry]),
                    patch("letee.sidebar._agent_entries", return_value=[]),
                    patch("letee.sidebar._bell_targets", return_value=set()),
                    patch("letee.cockpit.current_target", return_value=None),
                ):
                    run(screen)

                self.assertIn(call(True), mouse_mask.call_args_list)

    def test_move_handle_then_destination_press_reorders_tracked_sessions(self):
        first = Target("local", "one")
        second = Target("local", "two")
        entries = [
            Entry("one", "session", first, tracked=True),
            Entry("two", "session", second, tracked=True),
        ]
        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, curses.KEY_MOUSE, STOP], size=(12, 30))

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch(
                "letee.sidebar.curses.getmouse",
                side_effect=[
                    (0, 28, 2, 0, curses.BUTTON1_PRESSED),
                    (0, 0, 4, 0, curses.REPORT_MOUSE_POSITION),
                    (0, 0, 4, 0, curses.BUTTON1_PRESSED),
                ],
            ),
            patch("letee.sidebar._init_colors"),
            patch("letee.config.load_sessions", return_value=[first, second]),
            patch("letee.sidebar._entries", return_value=entries),
            patch("letee.sidebar._agent_entries", return_value=[]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.config.save_sessions") as save_sessions,
            patch("letee.sidebar.cockpit.switch") as switch,
        ):
            run(screen)

        save_sessions.assert_called_once_with((second, first))
        switch.assert_not_called()

    def test_delayed_move_scroll_resets_deadline_from_current_time(self):
        entries = [
            Entry(str(index), "session", Target("local", str(index)), tracked=True)
            for index in range(8)
        ]
        scroll_offsets = []
        screen = FakeScreen(
            [curses.KEY_MOUSE, curses.KEY_MOUSE, -1, -1, STOP],
            size=(14, 30),
        )
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=snapshot(local=tuple(str(index) for index in range(8))),
            current_target=None,
            bell_target=None,
            current_agent=None,
            pane_active=True,
            refresh_pending=False,
        )
        poller.tick.return_value = False

        def draw_spy(*args, **kwargs):
            bound = inspect.signature(_draw).bind(*args, **kwargs)
            scroll_offsets.append(bound.arguments["scroll_offset"])
            return (2, None)

        with (
            patch("letee.sidebar.AsyncStatusPoller", return_value=poller),
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch(
                "letee.sidebar.curses.getmouse",
                side_effect=[
                    (0, 28, 2, 0, curses.BUTTON1_PRESSED),
                    (0, 0, 6, 0, curses.REPORT_MOUSE_POSITION),
                ],
            ),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar.load_hosts", return_value=[]),
            patch("letee.config.load_sessions", return_value=[entry.target for entry in entries]),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar._entries", return_value=entries),
            patch("letee.sidebar._agent_entries", return_value=[]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.sidebar._draw", side_effect=draw_spy),
            # Include the initial local discovery sample's timestamp.
            patch("letee.sidebar.time.monotonic", side_effect=[0, 0, 0.1, 1.0, 1.1]),
        ):
            run(screen)

        self.assertEqual(scroll_offsets, [None, None, 1])

    def test_wheel_scroll_clears_move_target_until_next_motion(self):
        entries = [
            Entry(str(index), "session", Target("local", str(index)), tracked=True)
            for index in range(4)
        ]
        move_targets = []
        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, STOP], size=(14, 30))

        def draw_spy(*args, **kwargs):
            move_targets.append(kwargs.get("move_target_entry"))
            return (2, None)

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch(
                "letee.sidebar.curses.getmouse",
                side_effect=[
                    (0, 28, 2, 0, curses.BUTTON1_PRESSED),
                    (0, 0, 5, 0, curses.BUTTON5_PRESSED),
                ],
            ),
            patch("letee.sidebar._init_colors"),
            patch("letee.config.load_sessions", return_value=[entry.target for entry in entries]),
            patch("letee.sidebar._entries", return_value=entries),
            patch("letee.sidebar._agent_entries", return_value=[]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar._draw", side_effect=draw_spy),
        ):
            run(screen)

        self.assertIsNone(move_targets[-1])

    def test_destination_press_moves_last_session_upward(self):
        first = Target("local", "one")
        second = Target("local", "two")
        entries = [
            Entry("one", "session", first, tracked=True),
            Entry("two", "session", second, tracked=True),
        ]
        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, STOP], size=(12, 30))

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.getmouse", side_effect=[
                (0, 28, 4, 0, curses.BUTTON1_PRESSED),
                (0, 0, 2, 0, curses.BUTTON1_PRESSED),
            ]),
            patch("letee.sidebar._init_colors"),
            patch("letee.config.load_sessions", return_value=[first, second]),
            patch("letee.sidebar._entries", return_value=entries),
            patch("letee.sidebar._agent_entries", return_value=[]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.config.save_sessions") as save_sessions,
        ):
            run(screen)

        save_sessions.assert_called_once_with((second, first))

    def test_background_press_cancels_move_without_saving(self):
        target = Target("local", "one")
        entry = Entry("one", "session", target, tracked=True)
        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, STOP], size=(12, 30))

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.getmouse", side_effect=[
                (0, 28, 2, 0, curses.BUTTON1_PRESSED),
                (0, 0, 1, 0, curses.BUTTON1_PRESSED),
            ]),
            patch("letee.sidebar._init_colors"),
            patch("letee.config.load_sessions", return_value=[target]),
            patch("letee.sidebar._entries", return_value=[entry]),
            patch("letee.sidebar._agent_entries", return_value=[]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.config.save_sessions") as save_sessions,
        ):
            run(screen)

        save_sessions.assert_not_called()

    def test_repeated_move_handle_press_cancels_without_switching_or_saving(self):
        target = Target("local", "one")
        entry = Entry("one", "session", target, tracked=True)
        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, STOP], size=(12, 30))

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.getmouse", side_effect=[
                (0, 28, 2, 0, curses.BUTTON1_PRESSED),
                (0, 28, 2, 0, curses.BUTTON1_PRESSED),
            ]),
            patch("letee.sidebar._init_colors"),
            patch("letee.config.load_sessions", return_value=[target]),
            patch("letee.sidebar._entries", return_value=[entry]),
            patch("letee.sidebar._agent_entries", return_value=[]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.config.save_sessions") as save_sessions,
            patch("letee.sidebar.cockpit.switch") as switch,
        ):
            run(screen)

        save_sessions.assert_not_called()
        switch.assert_not_called()

    def test_move_mode_enables_then_disables_motion_tracking(self):
        target = Target("local", "one")
        entry = Entry("one", "session", target, tracked=True)
        screen = FakeScreen([curses.KEY_MOUSE, 27, STOP], size=(12, 30))

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.getmouse", return_value=(0, 28, 2, 0, curses.BUTTON1_PRESSED)),
            patch("letee.sidebar._mouse_mask") as mouse_mask,
            patch("letee.sidebar._mouse_cleanup"),
            patch("letee.sidebar._init_colors"),
            patch("letee.config.load_sessions", return_value=[target]),
            patch("letee.sidebar._entries", return_value=[entry]),
            patch("letee.sidebar._agent_entries", return_value=[]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
        ):
            run(screen)

        self.assertEqual(mouse_mask.call_args_list, [call(), call(True), call(False)])

    def test_single_click_switches_tracked_session_after_press_release_events(self):
        target = Target("local", "one")
        entries = [Entry("one", "session", target, tracked=True)]
        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, STOP], size=(12, 30))

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch(
                "letee.sidebar.curses.getmouse",
                side_effect=[
                    (0, 0, 2, 0, curses.BUTTON1_PRESSED),
                    (0, 0, 2, 0, curses.BUTTON1_RELEASED),
                ],
            ),
            patch("letee.sidebar._init_colors"),
            patch("letee.config.load_sessions", return_value=[target]),
            patch("letee.sidebar._entries", return_value=entries),
            patch("letee.sidebar._agent_entries", return_value=[]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar.cockpit.switch") as switch,
        ):
            run(screen)

        _assert_deferred_switch(self, switch, target)
        self.assertEqual(
            switch.call_args.args[1](),
            "env -u TMUX tmux -L letee.inner -T clipboard new-session -A -s one",
        )

    def test_press_release_selects_and_switches_untracked_session(self):
        target = Target("local", "one")
        entries = [Entry("one", "session", target)]
        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, STOP], size=(12, 30))

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch(
                "letee.sidebar.curses.getmouse",
                side_effect=[
                    (0, 0, 2, 0, curses.BUTTON1_PRESSED),
                    (0, 0, 2, 0, curses.BUTTON1_RELEASED),
                ],
            ),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._entries", return_value=entries),
            patch("letee.sidebar._agent_entries", return_value=[]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar.cockpit.switch") as switch,
        ):
            run(screen)

        _assert_deferred_switch(self, switch, target)
        self.assertEqual(
            switch.call_args.args[1](),
            "env -u TMUX tmux -L letee.inner -T clipboard new-session -A -s one",
        )

    def test_press_release_opens_add_button(self):
        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, STOP], size=(12, 30))
        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch(
                "letee.sidebar.curses.getmouse",
                side_effect=[
                    (0, 29, 0, 0, curses.BUTTON1_PRESSED),
                    (0, 29, 0, 0, curses.BUTTON1_RELEASED),
                ],
            ),
            patch("letee.sidebar._init_colors"),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar._available_locations", return_value=[("localhost", "")]),
            patch("letee.sidebar.curses.curs_set") as curs_set,
        ):
            run(screen)

        text = [call[3] for call in screen.calls if call[0] == "addnstr"]
        self.assertTrue(any("Add session" in line for line in text))
        self.assertEqual(curs_set.call_args_list, [call(0), call(1)])

    def test_synthesized_short_click_switches_session(self):
        entries = [
            Entry("LOCAL", "header"),
            Entry("one", "session", Target("local", "one")),
            Entry("two", "session", Target("local", "two")),
        ]
        screen = FakeScreen([curses.KEY_MOUSE, STOP], size=(12, 30))

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch("letee.sidebar.curses.getmouse", return_value=(0, 0, 4, 0, curses.BUTTON1_CLICKED)),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._entries", return_value=entries),
            patch("letee.sidebar._agent_entries", return_value=[]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar.cockpit.switch") as switch,
        ):
            run(screen)

        target = Target("local", "two")
        _assert_deferred_switch(self, switch, target)
        self.assertEqual(
            switch.call_args.args[1](),
            "env -u TMUX tmux -L letee.inner -T clipboard new-session -A -s two",
        )

    def test_right_click_press_opens_menu_without_switching(self):
        first = Target("local", "one")
        second = Target("local", "two")
        entries = [
            Entry("one", "session", first, tracked=True),
            Entry("two", "session", second, tracked=True),
        ]
        screen = FakeScreen([curses.KEY_MOUSE, STOP], size=(12, 30))

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask") as mousemask,
            patch("letee.sidebar._mouse_cleanup") as mouse_cleanup,
            patch(
                "letee.sidebar.curses.getmouse",
                return_value=(0, 7, 4, 0, curses.REPORT_MOUSE_POSITION | curses.BUTTON3_PRESSED),
            ),
            patch("letee.sidebar._init_colors"),
            patch("letee.config.load_sessions", return_value=[first, second]),
            patch("letee.sidebar._entries", return_value=entries),
            patch("letee.sidebar._agent_entries", return_value=[]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=first),
            patch("letee.sidebar.cockpit.show_session_menu") as show_menu,
            patch("letee.sidebar.cockpit.switch") as switch,
        ):
            run(screen)

        show_menu.assert_called_once_with(second, 7, 4, unittest.mock.ANY)
        self.assertEqual(mouse_cleanup.call_count, 2)
        self.assertEqual(mousemask.call_count, 2)
        switch.assert_not_called()

    def test_right_click_agent_selects_exact_agent_without_switching(self):
        target = Target("ssh", "work", "dev")
        first = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        second = PaneTarget(target, "@2", "%2", "/tmp/tmux")
        agents = (
            AgentEntry(first, "first", "pi-one", "working"),
            AgentEntry(second, "second", "pi-two", "working"),
        )
        data = SessionSnapshot(SourceSnapshot(True, (target,), frozenset(), agents=agents), {})
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=data,
            current_target=None,
            bell_target=None,
            current_agent=None,
            pane_active=True,
        )
        poller.tick.return_value = False
        screen = FakeScreen([curses.KEY_MOUSE, STOP], size=(20, 30))
        drawn = []
        real_draw = sidebar._draw

        def draw_spy(*args, **kwargs):
            bound = inspect.signature(real_draw).bind(*args, **kwargs)
            drawn.append(bound.arguments)
            return real_draw(*args, **kwargs)

        with (
            patch("letee.sidebar.AsyncStatusPoller", return_value=poller),
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask") as mousemask,
            patch("letee.sidebar._mouse_cleanup") as mouse_cleanup,
            patch(
                "letee.sidebar.curses.getmouse",
                return_value=(0, 7, 17, 0, curses.REPORT_MOUSE_POSITION | curses.BUTTON3_PRESSED),
            ),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar.load_hosts", return_value=[]),
            patch("letee.config.load_sessions", return_value=[target]),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar.cockpit.show_agent_menu") as show_menu,
            patch("letee.sidebar.cockpit.switch") as switch,
            patch("letee.sidebar._draw", side_effect=draw_spy),
        ):
            run(screen)

        show_menu.assert_called_once_with("pi-two", second, 7, 17, unittest.mock.ANY)
        self.assertEqual(drawn[-1]["agent_selected"], 2)
        self.assertEqual(mouse_cleanup.call_count, 2)
        self.assertEqual(mousemask.call_count, 2)
        switch.assert_not_called()

    def test_right_click_agent_ignores_divider_and_empty_space(self):
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        data = SessionSnapshot(
            SourceSnapshot(
                True,
                (target,),
                frozenset(),
                agents=(AgentEntry(pane, "id", "pi", "working"),),
            ),
            {},
        )
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=data,
            current_target=None,
            bell_target=None,
            current_agent=None,
            pane_active=True,
        )
        poller.tick.return_value = False
        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, STOP], size=(24, 30))

        with (
            patch("letee.sidebar.AsyncStatusPoller", return_value=poller),
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch("letee.sidebar._mouse_cleanup"),
            patch(
                "letee.sidebar.curses.getmouse",
                side_effect=[
                    (0, 7, 14, 0, curses.BUTTON3_PRESSED),
                    (0, 7, 22, 0, curses.BUTTON3_PRESSED),
                ],
            ),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar.load_hosts", return_value=[]),
            patch("letee.config.load_sessions", return_value=[target]),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar.cockpit.show_agent_menu") as show_menu,
            patch("letee.sidebar.cockpit.switch") as switch,
        ):
            run(screen)

        show_menu.assert_not_called()
        switch.assert_not_called()

    def test_right_click_agent_ignores_invalid_coordinates(self):
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        data = SessionSnapshot(
            SourceSnapshot(
                True,
                (target,),
                frozenset(),
                agents=(AgentEntry(pane, "id", "pi", "working"),),
            ),
            {},
        )
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=data,
            current_target=None,
            bell_target=None,
            current_agent=None,
            pane_active=True,
        )
        poller.tick.return_value = False
        screen = FakeScreen([curses.KEY_MOUSE, STOP], size=(20, 30))

        with (
            patch("letee.sidebar.AsyncStatusPoller", return_value=poller),
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch("letee.sidebar._mouse_cleanup"),
            patch("letee.sidebar.curses.getmouse", return_value=(0, -1, 17, 0, curses.BUTTON3_PRESSED)),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar.load_hosts", return_value=[]),
            patch("letee.config.load_sessions", return_value=[target]),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar.cockpit.show_agent_menu") as show_menu,
            patch("letee.sidebar.cockpit.switch") as switch,
        ):
            run(screen)

        show_menu.assert_not_called()
        switch.assert_not_called()

    def test_location_click_target_enters_search_view(self):
        state = SidebarState(add_view="location")
        sidebar._select_location(state, "")

        self.assertEqual(
            (state.add_view, state.creation_host),
            ("search", ""),
        )

    def test_search_back_returns_to_location_for_multiple_locations(self):
        state = SidebarState(add_view="search", creation_host="dev", filter_text="draft")
        data = snapshot(remotes={"dev": source("ssh", host="dev")})

        sidebar._add_back(state, data)

        self.assertEqual(state.add_view, "location")
        self.assertIsNone(state.creation_host)

    def test_invalid_name_keeps_rename_editor_state(self):
        old = Target("local", "old")
        state = SidebarState(
            add_view="name", rename_target=old, creation_host="dev", creation_text="bad name"
        )

        with self.assertRaisesRegex(SystemExit, "Invalid session"):
            _rename_key(state, 10, (old,))

        self.assertEqual((state.add_view, state.creation_host, state.creation_text), ("name", "dev", "bad name"))

    def test_wheel_scrolls_viewport_without_changing_selection(self):
        entries = [
            Entry("LOCAL", "header"),
            Entry("one", "session", Target("local", "one")),
            Entry("two", "session", Target("local", "two")),
        ]
        captured = []
        # j to move selection to entry 2, then wheel that no longer moves selection
        screen = FakeScreen([ord("j"), curses.KEY_MOUSE, 10, STOP], size=(8, 30))

        def draw_spy(*args, **kwargs):
            selected = args[2]
            scroll_offset = args[11] if len(args) > 11 else None
            captured.append((selected, scroll_offset))
            return (2, None)

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch("letee.sidebar.curses.getmouse", return_value=(0, 0, 0, 0, curses.BUTTON4_PRESSED)),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._entries", return_value=entries),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar._draw", side_effect=draw_spy),
            patch("letee.sidebar.cockpit.switch") as switch,
            patch("letee.sidebar.sessions.attach_command", return_value="attach") as attach_command,
        ):
            run(screen)
            # Selection stays at index 2 ("two") after wheel, Enter switches to "two".
            target_two = Target("local", "two")
            attach_command.assert_not_called()
            _assert_deferred_switch(self, switch, target_two)
            self.assertEqual(switch.call_args.args[1](), "attach")
            attach_command.assert_called_once_with(target_two)

    def test_malformed_mouse_event_is_ignored(self):
        screen = FakeScreen([curses.KEY_MOUSE, STOP])
        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch("letee.sidebar.curses.getmouse", side_effect=[(0, 0, None, 0, None), curses.error()]),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._entries", return_value=[Entry("work", "session", Target("local", "work"))]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
        ):
            run(screen)

    def test_startup_selects_first_session_instead_of_active_session(self):
        active = Target("local", "old")
        first = Target("local", "work")
        entries = [Entry("work", "session", first), Entry("old", "session", active)]
        selected = []
        screen = FakeScreen([STOP])

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._entries", return_value=entries),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=active),
            patch("letee.sidebar._draw", side_effect=lambda _, __, index, *args, **kwargs: selected.append(index) or (2, None)),
        ):
            run(screen)

        self.assertEqual(selected, [0])







    def test_only_first_nine_tracked_entries_get_slots(self):
        favorites = [Target("local", f"session-{slot}") for slot in range(10)]

        tracked_entries = [entry for entry in _entries(snapshot(), favorites) if entry.tracked]

        self.assertEqual([entry.shortcut_slot for entry in tracked_entries], [1, 2, 3, 4, 5, 6, 7, 8, 9, None])



    def test_section_divider_fills_width_and_has_ascii_fallback(self):
        with patch("letee.sidebar._ascii", return_value=False):
            unicode_line = _entry_lines(Entry("✱ STARRED", "section"), False, set(), None, 20)[0]
        with patch("letee.sidebar._ascii", return_value=True):
            ascii_line = _entry_lines(Entry("* STARRED", "section"), False, set(), None, 20)[0]

        self.assertEqual(unicode_line, "✱ STARRED " + "─" * 10)
        self.assertEqual(ascii_line, "* STARRED " + "-" * 10)
        self.assertTrue(ascii_line.isascii())

    def test_section_divider_truncates_safely_at_narrow_width(self):
        with patch("letee.sidebar._ascii", return_value=False):
            self.assertEqual(_entry_lines(Entry("ALL SESSIONS", "section"), False, set(), None, 5), ["ALL …"])

    def test_sections_are_non_selectable_and_mouse_ignores_them(self):
        entries = [
            Entry("✱ STARRED", "section"),
            Entry("work", "session", Target("local", "work"), tracked=True),
            Entry("ALL SESSIONS", "section"),
        ]

        self.assertEqual(sidebar._selectable(entries), [1])
        self.assertIsNone(_entry_at_row(entries, 1, 1, 8, 1))
        self.assertIsNone(_entry_at_row(entries, 1, 4, 8, 1))

    def test_section_attr_uses_mint_bold_and_inactive_dim(self):
        entry = Entry("STARRED", "section")
        with patch.dict("letee.sidebar._COLOR", {"section": 123 | curses.A_BOLD}, clear=True):
            active = _entry_attr(entry, False)
            inactive = _entry_attr(entry, False, True)

        self.assertEqual(active, 123 | curses.A_BOLD)
        self.assertEqual(inactive, active | curses.A_DIM)

    def test_section_attr_monochrome_fallback_is_bold(self):
        with patch.dict("letee.sidebar._COLOR", {}, clear=True):
            self.assertEqual(_entry_attr(Entry("STARRED", "section"), False), curses.A_BOLD)







    def test_add_titles_name_mode_and_filter_query(self):
        for query, name_input, adding, creation_host, expected in (
            ("", False, False, None, "＋ add"),
            ("", False, True, None, "letee / Add session"),
            ("work", True, True, None, "letee / Add session"),
            ("work", True, True, "dev", "letee / Add session · dev"),
        ):
            screen = FakeScreen(size=(5, 50))
            _draw(
                screen, [], 0, "", query, name_input=name_input, adding=adding,
                creation_host=creation_host,
            )
            all_title_text = "".join(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
            self.assertIn(expected, all_title_text)


    def test_title_excludes_tracked_duplicates_and_stale_entries(self):
        screen = FakeScreen(size=(5, 40))
        target = Target("local", "work")
        entries = [Entry("work", "session", target, tracked=True), Entry("work", "session", target), Entry("gone", "session", Target("local", "gone"), unavailable_favorite=True, tracked=True)]

        _draw(screen, entries, 0, "ok", "")

        title_text = "".join(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertIn("letee", title_text)

    def test_numbered_tracked_has_no_slot_in_entry_lines_in_unicode_and_ascii(self):
        entry = Entry(
            "work", "session", Target("local", "work"), host="laptop",
            tracked=True, shortcut_slot=3,
        )

        for ascii_mode, expected in ((False, "work"), (True, "work")):
            with self.subTest(ascii=ascii_mode), patch("letee.sidebar._ascii", return_value=ascii_mode):
                line = _entry_lines(entry, False, set(), None, 30)[0]
            self.assertEqual(line, expected)
            self.assertNotIn("✱", line)
            self.assertNotIn("3", line)

    def test_tracked_entry_draws_slot_badge_with_correct_attribute(self):
        tracked = Entry("work", "session", Target("local", "work"), host="laptop",
                        tracked=True, shortcut_slot=3)
        other = Entry("other", "session", Target("local", "other"))
        screen = FakeScreen(size=(7, 30))

        with patch.dict("letee.sidebar._COLOR", {"slot": 456, "local": 123}, clear=True):
            sidebar._draw_entries(
                screen, [other, tracked], 0, 5, 30, set(), None, dimmed=False, top=1,
            )

        slot_call = next(call for call in screen.calls
                         if call[0] == "addnstr" and call[3].startswith("["))
        self.assertEqual(slot_call[3], "[3]")
        self.assertEqual(slot_call[5], 456)
        line_call = next(call for call in screen.calls
                         if call[0] == "addnstr" and call[1] == slot_call[1] and not call[3].startswith("["))
        self.assertEqual(line_call[5], 123)

    def test_tracked_entries_render_session_then_source_without_raw_targets(self):
        local = next(entry for entry in _entries(snapshot(local=("dashboard",)), [Target("local", "dashboard")]) if entry.kind == "session")
        remote = Entry("auth", "session", Target("ssh", "auth", "dev"), host="dev", tracked=True)

        self.assertEqual(local.host, "localhost")
        with patch("letee.sidebar._ascii", return_value=False):
            self.assertEqual(_entry_lines(local, True, set(), None, 30), ["dashboard", "  └─ @localhost"])
            self.assertEqual(_entry_lines(remote, False, set(), None, 30), ["  auth", "  └─ @dev"])
        with patch("letee.sidebar._ascii", return_value=True):
            self.assertEqual(_entry_lines(local, True, set(), None, 30), ["dashboard", "  `- @localhost"])

        self.assertNotIn("local:", "".join(_entry_lines(local, True, set(), None, 30)))
        self.assertNotIn("ssh:", "".join(_entry_lines(remote, False, set(), None, 30)))

    def test_tracked_lines_truncate_session_and_metadata_and_keep_bell(self):
        entry = Entry("s" * 64, "session", Target("ssh", "s" * 64, "host"), host="h" * 64, tracked=True)

        with patch("letee.sidebar._ascii", return_value=False):
            lines = _entry_lines(entry, False, {entry.target}, None, 20)

        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("… 🔔"))
        self.assertTrue(lines[1].endswith("…"))
        self.assertTrue(all(len(line) <= 20 for line in lines))

    def test_ascii_tracked_metadata_and_ellipsis_are_ascii_only(self):
        entry = Entry(
            "session-name", "session", Target("ssh", "session-name", "long-host"),
            host="long-host", tracked=True, unavailable_favorite=True, status="reconnecting…",
        )

        with patch("letee.sidebar._ascii", return_value=True):
            lines = _entry_lines(entry, True, set(), None, 24)

        self.assertTrue(lines[0].isascii())
        self.assertTrue(lines[1].isascii())
        self.assertIn("@", lines[1])
        self.assertIn("reconnecting...", lines[1])
        self.assertIn("...", "".join(lines))

    def test_active_duplicate_is_highlighted_in_tracked_and_all_sections(self):
        target = Target("local", "work")
        entries = [
            Entry("work", "session", target, tracked=True),
            Entry("work", "session", target),
        ]
        screen = FakeScreen(size=(7, 30))

        with patch.dict("letee.sidebar._COLOR", {"active": 123}, clear=True):
            _draw(screen, entries, 0, "ok", "", current_target=target)

        rows = [call for call in screen.calls if call[0] == "addnstr" and call[3].strip().endswith("work")]
        self.assertEqual([call[5] for call in rows], [123, 123])

    def test_active_tracked_styles_both_rows_and_both_rows_map_to_entry(self):
        target = Target("local", "work")
        entry = Entry("work", "session", target, host="laptop", tracked=True)
        screen = FakeScreen(size=(6, 30))

        with patch.dict("letee.sidebar._COLOR", {"active": 123}, clear=True):
            _draw(screen, [entry], 0, "ok", "", current_target=target)

        rows = [call for call in screen.calls if call[0] == "addnstr" and call[1] in (2, 3)]
        handle_column = sidebar._move_handle_bounds(30)[0]
        self.assertEqual([call[5] for call in rows if call[2] != handle_column], [123, 123])
        self.assertEqual(_entry_at_row([entry], 0, 2, 6, 1, top=2), 0)
        self.assertEqual(_entry_at_row([entry], 0, 3, 6, 1, top=2), 0)

    def test_viewport_budgets_two_rows_for_selected_tracked_entry(self):
        entries = [Entry("STARRED", "header"), Entry("work", "session", Target("local", "work"), tracked=True), Entry("LOCAL", "header")]

        start, end = _viewport(entries, 1, 6)

        self.assertLessEqual(start, 1)
        self.assertGreater(end, 1)
        self.assertLessEqual(sum(2 if entry.tracked else 1 for entry in entries[start:end]) + int(start > 0) + int(end < len(entries)), 4)

    def test_tracked_rows_have_no_tracked_glyph(self):
        target = Target("local", "work")
        entry = Entry("work", "session", target, host="laptop", tracked=True)
        for ascii_mode, pointer in ((False, "› work"), (True, "> work")):
            screen = FakeScreen(size=(7, 30))
            with self.subTest(ascii=ascii_mode), patch("letee.sidebar._ascii", return_value=ascii_mode):
                _draw(screen, [entry], 0, "ok", "")
            rendered = [call[3].rstrip() for call in screen.calls if call[0] == "addnstr"]
            self.assertIn(pointer, rendered)
            self.assertNotIn("✱", "".join(rendered))


    def test_uppercase_j_moves_selected_tracked_target_down_and_persists(self):
        first = Target("local", "first")
        second = Target("local", "second")
        screen = FakeScreen([ord("J"), STOP], size=(10, 40))
        selections = []

        with (
            patch("letee.config.load_sessions", return_value=[first, second]),
            patch("letee.config.save_sessions") as save,
            patch("letee.discovery.local_snapshot", return_value=source("local", ("first", "second"))),
            patch("letee.sidebar.load_hosts", return_value=[]),
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar._draw", side_effect=lambda _, entries, index, *args, **kwargs: selections.append(entries[index]) or (2, None)),
        ):
            run(screen)

        save.assert_called_once_with((second, first))
        self.assertEqual(selections[-1].target, first)
        self.assertTrue(selections[-1].tracked)

    def test_empty_session_list_opens_add_menu_and_ignores_remove_and_kill(self):
        screen = FakeScreen([curses.KEY_ENTER, ord("r"), ord("x"), STOP], size=(8, 60))

        with (
            patch("letee.discovery.local_snapshot", return_value=source("local")),
            patch("letee.config.load_sessions", return_value=[]),
            patch("letee.sidebar.load_hosts", return_value=[]),
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar.sessions.kill") as kill,
        ):
            run(screen)

        kill.assert_not_called()
        self.assertTrue(any(
            call[0] == "addnstr" and "Add session" in call[3]
            for call in screen.calls
        ))

    def test_kill_outside_session_rows_is_ignored(self):
        screen = FakeScreen([curses.KEY_F11, ord("x"), STOP], size=(8, 60))

        with (
            patch("letee.discovery.local_snapshot", return_value=source("local")),
            patch("letee.config.load_sessions", return_value=[]),
            patch("letee.sidebar.load_hosts", return_value=[]),
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar.sessions.kill") as kill,
        ):
            run(screen)

        kill.assert_not_called()
        self.assertFalse(any(call[0] == "addnstr" and "select session to kill" in call[3] for call in screen.calls))

    def test_absent_session_kill_guides_custom_removal(self):
        target = Target("local", "work")
        custom = {**config.DEFAULT_SIDEBAR_KEYBINDINGS, "remove": "D"}
        screen = FakeScreen([ord("x"), STOP], size=(8, 60))

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._init_colors"),
            patch("letee.config.load_sessions", return_value=[target]),
            patch("letee.sidebar.load_sidebar_keybindings", return_value=custom),
            patch("letee.sidebar._entries", return_value=[Entry("work", "session", target, unavailable_favorite=True, tracked=True)]),
            patch("letee.sidebar._agent_entries", return_value=[]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
        ):
            run(screen)

        self.assertTrue(any(
            call[0] == "addnstr" and "Session unavailable; press D to remove" in call[3]
            for call in screen.calls
        ))

    def test_cancelled_kill_has_no_redundant_status(self):
        screen = FakeScreen([ord("x"), ord("n"), STOP], size=(8, 60))
        target = Target("local", "work")

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._init_colors"),
            patch("letee.config.load_sessions", return_value=[target]),
            patch("letee.sidebar._entries", return_value=[Entry("work", "session", target, tracked=True)]),
            patch("letee.sidebar._agent_entries", return_value=[]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=target),
            patch("letee.sidebar.sessions.kill") as kill,
        ):
            run(screen)

        kill.assert_not_called()
        self.assertFalse(any(call[0] == "addnstr" and "cancelled" in call[3] for call in screen.calls))

    def test_failed_kill_keeps_sidebar_open_and_shows_error_above_footer(self):
        screen = FakeScreen([ord("x"), ord("y"), -1, STOP], size=(8, 60))
        target = Target("local", "work")
        runner = sidebar.EffectRunner()
        getch = screen.getch

        def wait_for_kill():
            key = getch()
            if key == -1 and runner._future is not None:
                runner._future.result(timeout=1)
            return key

        with (
            patch.object(screen, "getch", side_effect=wait_for_kill),
            patch.object(sidebar, "EffectRunner", return_value=runner),
            patch("letee.discovery.local_snapshot", return_value=source("local", ("work",))),
            patch("letee.config.load_sessions", return_value=[target]),
            patch("letee.sidebar.load_hosts", return_value=[]),
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=target),
            patch("letee.sidebar.cockpit.set_expected_right_pane_death") as expected_death,
            patch("letee.sidebar.cockpit.resolve_expected_right_pane_death"),
            patch("letee.sidebar.sessions.kill", side_effect=SystemExit("kill local:work failed: denied")),
        ):
            run(screen)

        expected_death.assert_called_once_with(target)
        error = next(call for call in screen.calls if call[0] == "addnstr" and "kill local:work failed: denied" in call[3])
        self.assertEqual(error[1], 1)
        footer = [call[3].rstrip() for call in screen.calls if call[0] == "addnstr" and call[1] == 7]
        self.assertTrue(footer)
        self.assertTrue(all(line == "↵ activate" for line in footer))



    def test_rendered_rows_include_icons(self):
        screen = FakeScreen(size=(9, 30))

        with patch("letee.sidebar._ascii", return_value=False):
            _draw(screen, [Entry("work", "session", Target("local", "work")), Entry("chat", "session", Target("ssh", "chat", "dev"), host="dev")], 0, "ok", "")

        text = "\n".join(str(call) for call in screen.calls)
        self.assertIn("● work", text)
        self.assertIn("◆ chat", text)

    def test_selection_pointer_and_active_color_are_independent(self):
        active = Target("local", "active")
        selected = Target("local", "selected")
        entries = [Entry("active", "session", active), Entry("selected", "session", selected)]
        screen = FakeScreen(size=(7, 30))

        with patch.dict("letee.sidebar._COLOR", {"active": 123, "local": 45}, clear=True):
            _draw(screen, entries, 1, "ok", "", current_target=active)

        rows = {call[3].strip(): call for call in screen.calls if call[0] == "addnstr" and "session" not in call[3]}
        self.assertEqual(rows["● active"][5], 123)
        self.assertEqual(rows["› ● selected"][5], 45)

    def test_unfocused_sidebar_hides_pointer_keeps_colors_and_active_reverse(self):
        active = Target("local", "active")
        selected = Target("local", "selected")
        screen = FakeScreen(size=(7, 30))

        with patch.dict("letee.sidebar._COLOR", {"active": curses.A_REVERSE, "local": 45}, clear=True):
            _draw(
                screen,
                [Entry("active", "session", active), Entry("selected", "session", selected)],
                1,
                "ok",
                "",
                current_target=active,
                pane_active=False,
            )

        rows = [call for call in screen.calls if call[0] == "addnstr" and ("active" in call[3] or "selected" in call[3])]
        self.assertFalse(any("›" in call[3] for call in rows))
        active_row = next(call for call in rows if "active" in call[3])
        selected_row = next(call for call in rows if "selected" in call[3])
        self.assertTrue(active_row[5] & curses.A_REVERSE)
        self.assertEqual(selected_row[5], 45)

    def test_unfocused_viewport_preserves_offscreen_selection(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(10)]
        screen = FakeScreen(size=(5, 30))

        _draw(screen, entries, 9, "ok", "", current_target=entries[0].target, pane_active=False)

        rendered = [call[3] for call in screen.calls if call[0] == "addnstr"]
        self.assertFalse(any(line.strip().endswith("0") for line in rendered))
        self.assertTrue(any(line.strip().endswith("9") for line in rendered))

    def test_scrolling_keeps_selected_visible(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(10)]

        start, end = _viewport(entries, 9, 5)

        self.assertLessEqual(start, 9)
        self.assertLess(9, end)

    def test_selected_index_prefers_current_target(self):
        entries = [
            Entry("LOCAL", "header"),
            Entry("notes", "session", Target("local", "notes")),
            Entry("work", "session", Target("local", "work")),
        ]

        self.assertEqual(_selected_index(entries, Target("local", "work")), 2)

    def test_selected_index_prefers_session_over_earlier_host(self):
        entries = [
            Entry("laptop", "host", host=""),
            Entry("dev", "host", host="dev"),
            Entry("work", "session", Target("ssh", "work", "dev"), "dev"),
        ]

        self.assertEqual(_selected_index(entries, Target("local", "missing")), 2)




    def test_poller_closes_after_keyboard_interrupt(self):
        screen = FakeScreen()
        screen.getch = unittest.mock.Mock(side_effect=KeyboardInterrupt)
        poller = unittest.mock.Mock(launch_id=None, )
        poller.snapshot = snapshot()
        poller.tick.return_value = False

        with (
            patch("letee.sidebar.DiscoveryPoller", return_value=poller),
            patch("letee.sidebar.load_hosts", return_value=[]),
            patch("letee.sidebar.cockpit.bell_target", return_value=None),
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar._init_colors"),
            patch("letee.cockpit.current_target", return_value=None),
        ):
            with self.assertRaises(KeyboardInterrupt):
                run(screen)

        poller.close.assert_called_once_with()



class SidebarRenderRecoveryTest(OperationTestCase):
    def _run(self, state, keys, screen):
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=snapshot(local=("work-one", "work-two")),
            current_target=None,
            bell_target=None,
            current_agent=None,
            pane_active=True,
        )
        poller.tick.return_value = False
        actions = unittest.mock.Mock(busy=False)
        actions.poll.return_value = None
        with (
            patch.object(sidebar, "SidebarState", return_value=state),
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "DiscoveryPoller"),
            patch.object(sidebar, "EffectRunner", return_value=actions),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=state.favorites),
            patch.object(sidebar.cockpit, "current_target", return_value=None),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar, "_mouse_cleanup"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_bell_targets", return_value=set()),
        ):
            screen.keys = list(keys)
            sidebar.run(screen)
        poller.close.assert_called_once_with()
        actions.close.assert_called_once_with()
        return poller, actions

    def test_transient_render_failure_retries_and_preserves_state(self):
        targets = [Target("local", name) for name in ("work-one", "work-two")]
        for view in (None, "search", "name"):
            for operation in ("erase", "refresh"):
                with self.subTest(view=view, operation=operation):
                    state = SidebarState(
                        favorites=targets.copy(),
                        add_view=view, filter_text="work" if view == "search" else "",
                        creation_host="" if view else None,
                        creation_text="renamed" if view == "name" else "",
                        rename_target=targets[1] if view == "name" else None,
                    )
                    screen = FakeScreen(size=(12, 40))
                    attempts = []
                    failure_attempt = 3 if view is None else 2
                    original = getattr(screen, operation)

                    def draw_operation():
                        attempts.append((
                            state.selected_target, state.selected_index, state.add_view,
                            state.filter_text, state.creation_text, state.rename_target,
                        ))
                        if len(attempts) == failure_attempt:
                            raise curses.error("terminal resized during rendering")
                        original()

                    keys = [-1, curses.KEY_RESIZE, -1, -1, ord("z"), STOP]
                    if view is None:
                        keys = [curses.KEY_DOWN, -1] + keys
                    with patch.object(screen, operation, side_effect=draw_operation):
                        self._run(state, keys, screen)

                    self.assertGreaterEqual(len(attempts), failure_attempt + 1)
                    before, failed, retried = attempts[failure_attempt - 2:failure_attempt + 1]
                    self.assertEqual(before, failed)
                    self.assertEqual(failed, retried)
                    self.assertEqual(before[0], targets[1] if view is None else targets[0])
                    self.assertEqual(state.add_view, view)
                    self.assertEqual(state.filter_text, "workz" if view == "search" else "")
                    self.assertEqual(state.creation_text, "renamedz" if view == "name" else "")
                    self.assertEqual(state.rename_target, targets[1] if view == "name" else None)

    def test_resize_redraws_when_dimensions_return_to_previous_values(self):
        # A rapid shrink/grow can restore the old size before KEY_RESIZE is read.
        screen = FakeScreen(size=(12, 40))
        with patch.object(screen, "refresh", wraps=screen.refresh) as refresh:
            self._run(
                SidebarState(),
                [-1, curses.KEY_RESIZE, -1, curses.KEY_RESIZE, -1, STOP],
                screen,
            )
        self.assertEqual(refresh.call_count, 3)

    def test_unrelated_render_exceptions_propagate(self):
        for view in (None, "name"):
            with self.subTest(view=view):
                screen = FakeScreen(size=(12, 40))
                with (
                    patch.object(screen, "refresh", side_effect=RuntimeError("unexpected failure")),
                    self.assertRaisesRegex(RuntimeError, "unexpected failure"),
                ):
                    self._run(SidebarState(add_view=view), [-1, STOP], screen)


class SidebarBurstInputTest(OperationTestCase):
    def test_idle_polls_do_not_repeat_compact_lookup(self):
        with patch.object(sidebar.cockpit, "sidebar_compact", return_value=False) as lookup:
            _, poller, draws, _ = self._run([-1] * 10 + [STOP], snapshot())
        self.assertGreater(poller.tick.call_count, 1)
        lookup.assert_called_once_with()
        self.assertEqual(len(draws), 1)

    def test_resize_refreshes_compact_mode_in_both_directions(self):
        for initial in (False, True):
            with self.subTest(initial=initial), patch.object(
                sidebar.cockpit, "sidebar_compact", side_effect=[initial, not initial],
            ) as lookup:
                _, _, draws, _ = self._run([curses.KEY_RESIZE, STOP], snapshot())
                self.assertEqual(lookup.call_count, 2)
                self.assertEqual([draw["compact"] for draw in draws], [initial, not initial])

    def test_header_collapse_activates_only_visible_control(self):
        for width in (8, 40):
            for buttons in (curses.BUTTON1_PRESSED, curses.BUTTON1_CLICKED):
                with self.subTest(width=width, buttons=buttons), patch.object(sidebar.cockpit, "toggle_sidebar") as toggle:
                    self._run(
                        [curses.KEY_MOUSE, curses.KEY_MOUSE, STOP], snapshot(),
                        mouse_events=[(0, 1, 0, 0, buttons), (0, 1, 0, 0, curses.BUTTON1_RELEASED)],
                        size=(12, width),
                    )
                    toggle.assert_called_once_with()

    def test_header_collapse_ignores_other_events_and_add_views(self):
        cases = [
            ((), (0, 1, 0, 0, curses.BUTTON1_RELEASED), 40),
            ((), (0, 1, 0, 0, curses.BUTTON4_PRESSED), 40),
            ((), (0, 1, 0, 0, curses.BUTTON5_PRESSED), 40),
            ((), (0, 1, 0, 0, curses.BUTTON3_PRESSED), 40),
            ((), (0, 0, 0, 0, curses.BUTTON1_PRESSED), 40),
            ((), (0, 2, 0, 0, curses.BUTTON1_PRESSED), 40),
            ((), (0, 1, 1, 0, curses.BUTTON1_PRESSED), 40),
            ((), (0, 1, 0, 0, curses.BUTTON1_PRESSED), 6),
            ((curses.KEY_F11,), (0, 1, 0, 0, curses.BUTTON1_PRESSED), 40),
        ]
        for keys, event, width in cases:
            with self.subTest(keys=keys, event=event, width=width), patch.object(sidebar.cockpit, "toggle_sidebar") as toggle:
                self._run([*keys, curses.KEY_MOUSE, STOP], snapshot(), mouse_events=[event], size=(12, width))
                toggle.assert_not_called()

    def test_header_collapse_redraws_in_compact_mode(self):
        compact = False

        def collapse():
            nonlocal compact
            compact = True

        with (
            patch.object(sidebar.cockpit, "sidebar_compact", side_effect=lambda: compact) as lookup,
            patch.object(sidebar.cockpit, "toggle_sidebar", side_effect=collapse) as toggle,
        ):
            _, _, draws, switch = self._run(
                [curses.KEY_MOUSE, STOP], snapshot(),
                mouse_events=[(0, 1, 0, 0, curses.BUTTON1_PRESSED)],
            )
        toggle.assert_called_once_with()
        self.assertEqual(lookup.call_count, 2)
        self.assertFalse(draws[0]["compact"])
        self.assertTrue(draws[-1]["compact"])
        switch.assert_not_called()

    def _run(self, keys, data, favorites=(), mouse_events=(), size=(12, 40), screen_class=FakeScreen):
        screen = screen_class(keys, size=size)
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=data,
            current_target=None,
            bell_target=None,
            current_agent=None,
            pending_agent=None,
            pane_active=True,
        )
        poller.tick.return_value = False
        draws = []
        draw_signature = inspect.signature(sidebar._draw)

        def draw_spy(*args, **kwargs):
            bound = draw_signature.bind(*args, **kwargs)
            bound.apply_defaults()
            draws.append({
                "selected": bound.arguments["selected"],
                "target": (
                    bound.arguments["entries"][bound.arguments["selected"]].target
                    if bound.arguments["entries"]
                    and 0 <= bound.arguments["selected"] < len(bound.arguments["entries"])
                    else None
                ),
                "scroll_offset": bound.arguments["scroll_offset"],
                "adding": bound.arguments["adding"],
                "filtering": bound.arguments["name_input"],
                "compact": bound.arguments["compact"],
            })
            _, add_col = sidebar._draw_title(
                args[0], args[0].getmaxyx()[1], bound.arguments["entries"], "",
                adding=bound.arguments["adding"],
            )
            return 2, add_col

        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "DiscoveryPoller"),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=list(favorites)),
            patch.object(sidebar.cockpit, "current_target", return_value=None),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", side_effect=draw_spy),
            patch.object(sidebar, "_bell_targets", return_value=set()),
            patch.object(sidebar.curses, "getmouse", side_effect=mouse_events),
            patch.object(sidebar.cockpit.tmux, "out", return_value=""),
            patch.object(sidebar.cockpit, "switch") as switch,
        ):
            sidebar.run(screen)

        return screen, poller, draws, switch

    def test_rapid_session_navigation_applies_every_key_in_order(self):
        favorites = [Target("local", name) for name in ("one", "two", "three", "four")]
        _, _, draws, _ = self._run(
            [ord("j"), ord("j"), ord("j"), curses.KEY_F7, STOP],
            snapshot(local=tuple(target.session for target in favorites)),
            favorites,
        )

        self.assertEqual(draws[-1]["target"], favorites[-1])

    def test_fragmented_arrow_sequences_are_read_during_burst_polling(self):
        class FragmentedArrowScreen(FakeScreen):
            def __init__(self, keys, size=(12, 40)):
                super().__init__(keys, size=size)
                self._arrow_count = 0

            def getch(self):
                if self.keys and self.keys[0] in (curses.KEY_UP, curses.KEY_DOWN):
                    key = self.keys.pop(0)
                    self._arrow_count += 1
                    if self.timeout_value == 0 and self._arrow_count > 1:
                        return 27  # The escape prefix arrives before the sequence is complete.
                    self.calls.append(("getch",))
                    return key
                return super().getch()

        favorites = [Target("local", name) for name in ("one", "two", "three", "four")]
        _, _, draws, _ = self._run(
            [curses.KEY_DOWN, curses.KEY_DOWN, curses.KEY_UP, curses.KEY_UP, STOP],
            snapshot(local=tuple(target.session for target in favorites)),
            favorites,
            screen_class=FragmentedArrowScreen,
        )

        self.assertEqual(draws[-1]["target"], favorites[0])

    def test_rapid_add_existing_navigation_applies_every_key_in_order(self):
        _, _, draws, _ = self._run(
            [curses.KEY_F11, curses.KEY_DOWN, curses.KEY_DOWN, curses.KEY_F7, STOP],
            snapshot(local=("one", "two", "three")),
        )

        self.assertTrue(draws[-1]["adding"])
        self.assertEqual(draws[-1]["target"], Target("local", "three"))

    def test_rapid_search_entry_preserves_every_character(self):
        screen = FakeScreen(
            [curses.KEY_F11, *map(ord, "session"), getattr(curses, "KEY_RESIZE", 410), STOP],
            size=(12, 40),
        )
        entered = []
        draw_signature = inspect.signature(sidebar._draw)
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=snapshot(local=("running",)),
            current_target=None,
            bell_target=None,
            current_agent=None,
            pane_active=True,
        )
        poller.tick.return_value = False

        def draw_spy(*args, **kwargs):
            bound = draw_signature.bind(*args, **kwargs)
            bound.apply_defaults()
            entered.append(bound.arguments["filter_text"])
            return 2, None

        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "DiscoveryPoller"),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[]),
            patch.object(sidebar.cockpit, "current_target", return_value=None),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", side_effect=draw_spy),
            patch.object(sidebar, "_bell_targets", return_value=set()),
        ):
            sidebar.run(screen)

        self.assertIn("session", entered)
        self.assertEqual(entered[-1], "session")

    def test_mixed_wheel_and_keyboard_input_keeps_order(self):
        first = Target("local", "one")
        second = Target("local", "two")
        _, _, _, switch = self._run(
            [curses.KEY_MOUSE, ord("j"), curses.KEY_MOUSE, ord("k"), curses.KEY_ENTER, STOP],
            snapshot(local=("one", "two")),
            [first, second],
            [
                (0, 0, 3, 0, curses.BUTTON5_PRESSED),
                (0, 0, 3, 0, curses.BUTTON4_PRESSED),
            ],
        )

        _assert_deferred_switch(self, switch, first)

    def test_mouse_press_and_release_execute_once(self):
        target = Target("local", "one")
        _, _, _, switch = self._run(
            [curses.KEY_MOUSE, curses.KEY_MOUSE, STOP],
            snapshot(local=("one",)),
            [target],
            [
                (0, 0, 2, 0, curses.BUTTON1_PRESSED),
                (0, 0, 2, 0, curses.BUTTON1_RELEASED),
            ],
        )

        _assert_deferred_switch(self, switch, target)

    def test_wheel_then_click_renders_new_layout_before_hit_testing(self):
        targets = [Target("local", str(index)) for index in range(8)]
        order = []
        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, STOP], size=(12, 40))
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=snapshot(local=tuple(target.session for target in targets)),
            current_target=None,
            bell_target=None,
            current_agent=None,
            pane_active=True,
        )
        poller.tick.return_value = False
        draw_signature = inspect.signature(sidebar._draw)

        def draw_spy(*args, **kwargs):
            bound = draw_signature.bind(*args, **kwargs)
            bound.apply_defaults()
            order.append(("draw", bound.arguments["scroll_offset"]))
            return 2, None

        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "DiscoveryPoller"),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=targets),
            patch.object(sidebar.cockpit, "current_target", return_value=None),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", side_effect=draw_spy),
            patch.object(sidebar, "_bell_targets", return_value=set()),
            patch.object(sidebar.curses, "getmouse", side_effect=[
                (0, 0, 3, 0, curses.BUTTON5_PRESSED),
                (0, 0, 3, 0, curses.BUTTON1_PRESSED),
            ]),
            patch.object(sidebar.cockpit, "switch", side_effect=lambda *_, **kwargs: order.append("switch")),
        ):
            sidebar.run(screen)

        self.assertIn(("draw", 1), order)
        self.assertLess(order.index(("draw", 1)), order.index("switch"))

    def test_continuous_input_still_polls_and_renders(self):
        favorites = [Target("local", name) for name in ("one", "two")]
        _, poller, draws, _ = self._run(
            [ord("j")] * 40 + [STOP],
            snapshot(local=("one", "two")),
            favorites,
        )

        self.assertGreaterEqual(poller.tick.call_count, 2)
        self.assertGreaterEqual(len(draws), 2)
        self.assertLess(poller.tick.call_count, 40)
        self.assertLess(len(draws), 40)

    def test_resize_and_malformed_mouse_keep_following_event(self):
        target = Target("local", "one")
        _, _, _, switch = self._run(
            [getattr(curses, "KEY_RESIZE", 410), curses.KEY_MOUSE, curses.KEY_MOUSE, STOP],
            snapshot(local=("one",)),
            [target],
            [curses.error(), (0, 0, 2, 0, curses.BUTTON1_PRESSED)],
        )

        _assert_deferred_switch(self, switch, target)

class SidebarScrollOffsetTest(OperationTestCase):
    def test_viewport_respects_scroll_offset(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(10)]

        start, end = _viewport(entries, 0, 8, scroll_offset=5)

        self.assertEqual(start, 5)
        self.assertLess(start, end)
        self.assertLessEqual(end, len(entries))

    def test_viewport_scroll_offset_none_uses_selection(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(10)]

        start, end = _viewport(entries, 9, 8)

        self.assertLessEqual(start, 9)
        self.assertLess(9, end)

    def test_scroll_offset_is_none_initially(self):
        state = SidebarState()
        self.assertIsNone(state.scroll_offset)

    def test_wheel_outside_list_regions_does_not_scroll_sessions(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(10)]

        for row in (0, 7):
            with self.subTest(row=row):
                captured = []

                def draw_spy(*args, **kwargs):
                    captured.append(args[11] if len(args) > 11 else None)
                    return (2, None)

                screen = FakeScreen([curses.KEY_MOUSE, STOP], size=(8, 30))
                with (
                    patch("letee.sidebar.curses.curs_set"),
                    patch("letee.sidebar.curses.mousemask"),
                    patch("letee.sidebar._init_colors"),
                    patch("letee.sidebar._entries", return_value=entries),
                    patch("letee.sidebar._agent_entries", return_value=[]),
                    patch("letee.sidebar._bell_targets", return_value=set()),
                    patch("letee.cockpit.current_target", return_value=None),
                    patch("letee.sidebar._draw", side_effect=draw_spy),
                    patch("letee.sidebar.curses.getmouse", return_value=(0, 0, row, 0, curses.BUTTON5_PRESSED)),
                ):
                    run(screen)

                self.assertEqual(captured, [None])

    def test_wheel_up_decrements_scroll_offset_not_selection(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(10)]
        captured = []

        def draw_spy(*args, **kwargs):
            selected = args[2]
            scroll_offset = args[11] if len(args) > 11 else None
            captured.append((selected, scroll_offset))
            return (2, None)

        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, STOP], size=(8, 30))
        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._entries", return_value=entries),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar._draw", side_effect=draw_spy),
            patch("letee.sidebar.curses.getmouse", return_value=(0, 0, 2, 0, curses.BUTTON4_PRESSED)),
        ):
            run(screen)

        self.assertGreater(len(captured), 1)
        selected_values = {sel for sel, _ in captured}
        self.assertEqual(selected_values, {0})  # selection never changed
        offsets = [off for _, off in captured if off is not None]
        self.assertTrue(len(offsets) >= 1)  # scroll_offset was set

    def test_wheel_down_increments_scroll_offset_not_selection(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(10)]
        captured = []

        def draw_spy(*args, **kwargs):
            selected = args[2]
            scroll_offset = args[11] if len(args) > 11 else None
            captured.append((selected, scroll_offset))
            return (2, None)

        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, STOP], size=(8, 30))
        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._entries", return_value=entries),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar._draw", side_effect=draw_spy),
            patch("letee.sidebar.curses.getmouse", return_value=(0, 0, 2, 0, curses.BUTTON5_PRESSED)),
        ):
            run(screen)

        self.assertGreater(len(captured), 1)
        selected_values = {sel for sel, _ in captured}
        self.assertEqual(selected_values, {0})  # selection never changed
        offsets = [off for _, off in captured if off is not None]
        self.assertTrue(len(offsets) >= 1)  # scroll_offset was set

    def test_wheel_down_reaches_bottom_of_session_region(self):
        entries = [Entry(str(i), "session", Target("local", str(i)), tracked=True) for i in range(8)]
        captured = []
        mouse_events = [(0, 0, 2, 0, curses.BUTTON5_PRESSED)] * 10
        screen = FakeScreen([curses.KEY_MOUSE] * len(mouse_events) + [STOP], size=(12, 30))

        def draw_spy(*args, **kwargs):
            captured.append(args[11] if len(args) > 11 else None)
            return (1, None)

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._entries", return_value=entries),
            patch("letee.sidebar._agent_entries", return_value=[]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar._draw", side_effect=draw_spy),
            patch("letee.sidebar.curses.getmouse", side_effect=mouse_events),
        ):
            run(screen)

        _, end = _viewport(entries, 0, 7, captured[-1])
        self.assertEqual(end, len(entries))

    def test_discovery_update_preserves_wheel_position(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(8)]
        captured = []
        poller = unittest.mock.Mock(launch_id=None, snapshot=snapshot())
        poller.tick.side_effect = [False, True]
        screen = FakeScreen([curses.KEY_MOUSE, -1, STOP], size=(12, 30))

        def draw_spy(*args, **kwargs):
            captured.append(args[11] if len(args) > 11 else None)
            return (1, None)

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar.DiscoveryPoller", return_value=poller),
            patch("letee.sidebar._entries", return_value=entries),
            patch("letee.sidebar._agent_entries", return_value=[]),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar._draw", side_effect=draw_spy),
            patch("letee.sidebar.curses.getmouse", return_value=(0, 0, 2, 0, curses.BUTTON5_PRESSED)),
        ):
            run(screen)

        self.assertEqual(captured, [None, 1])

    def test_queued_wheel_reversal_is_processed_before_next_poll(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(10)]
        captured = []
        mouse_events = [
            (0, 0, 2, 0, curses.BUTTON4_PRESSED),
            (0, 0, 2, 0, curses.BUTTON4_PRESSED),
            (0, 0, 2, 0, curses.BUTTON5_PRESSED),
        ]
        screen = FakeScreen([curses.KEY_MOUSE] * len(mouse_events) + [STOP], size=(8, 30))

        def draw_spy(*args, **kwargs):
            captured.append(args[11] if len(args) > 11 else None)
            return (2, None)

        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._entries", return_value=entries),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None) as current_target,
            patch("letee.sidebar._draw", side_effect=draw_spy),
            patch("letee.sidebar.curses.getmouse", side_effect=mouse_events),
        ):
            run(screen)

        self.assertEqual(captured, [None, 1])
        self.assertLess(current_target.call_count, len(mouse_events) + 1)

    def test_j_key_resets_scroll_offset(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(5)]
        captured = []

        def draw_spy(*args, **kwargs):
            selected = args[2]
            scroll_offset = args[11] if len(args) > 11 else None
            captured.append((selected, scroll_offset))
            return (2, None)

        screen = FakeScreen([
            curses.KEY_MOUSE,  # wheel to set scroll_offset
            ord("j"),          # j to reset scroll_offset
            STOP,
        ], size=(8, 30))
        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._entries", return_value=entries),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar._draw", side_effect=draw_spy),
            patch("letee.sidebar.curses.getmouse", return_value=(0, 0, 2, 0, curses.BUTTON4_PRESSED)),
        ):
            run(screen)

        # Final render should have scroll_offset=None (reset by j)
        self.assertIsNone(captured[-1][1])

    def test_k_key_resets_scroll_offset(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(5)]
        captured = []

        def draw_spy(*args, **kwargs):
            selected = args[2]
            scroll_offset = args[11] if len(args) > 11 else None
            captured.append((selected, scroll_offset))
            return (2, None)

        screen = FakeScreen([
            curses.KEY_MOUSE,  # wheel to set scroll_offset
            ord("k"),          # k to reset scroll_offset
            STOP,
        ], size=(8, 30))
        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._entries", return_value=entries),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar._draw", side_effect=draw_spy),
            patch("letee.sidebar.curses.getmouse", return_value=(0, 0, 2, 0, curses.BUTTON4_PRESSED)),
        ):
            run(screen)

        self.assertIsNone(captured[-1][1])

    def test_enter_resets_scroll_offset(self):
        entries = [Entry("work", "session", Target("local", "work"))]
        captured = []

        def draw_spy(*args, **kwargs):
            selected = args[2]
            scroll_offset = args[11] if len(args) > 11 else None
            captured.append((selected, scroll_offset))
            return (2, None)

        screen = FakeScreen([
            curses.KEY_MOUSE,  # wheel to set scroll_offset
            10,                # Enter to reset
            STOP,
        ], size=(8, 30))
        with (
            patch("letee.sidebar.curses.curs_set"),
            patch("letee.sidebar.curses.mousemask"),
            patch("letee.sidebar._init_colors"),
            patch("letee.sidebar._entries", return_value=entries),
            patch("letee.sidebar._bell_targets", return_value=set()),
            patch("letee.cockpit.current_target", return_value=None),
            patch("letee.sidebar._draw", side_effect=draw_spy),
            patch("letee.sidebar.curses.getmouse", return_value=(0, 0, 2, 0, curses.BUTTON4_PRESSED)),
            patch("letee.sidebar.cockpit.switch"),
            patch("letee.sidebar.sessions.attach_command", return_value="attach"),
        ):
            run(screen)

        # After Enter, scroll_offset should be None (reset)
        final_offsets = [off for _, off in captured[-2:]]
        self.assertIn(None, final_offsets)

    @staticmethod
    def _agent_data(count=5):
        target = Target("local", "work")
        agents = tuple(
            AgentEntry(
                PaneTarget(target, "@1", f"%{index}", "/tmp/tmux"),
                f"agent-{index}",
                "pi",
                "idle",
            )
            for index in range(1, count + 1)
        )
        return target, SessionSnapshot(
            SourceSnapshot(True, (target,), frozenset(), agents=agents), {}
        )

    def test_agent_wheel_scrolls_only_agent_viewport_without_focus(self):
        target, data = self._agent_data()
        captured = []
        draw_signature = inspect.signature(sidebar._draw)

        def draw_spy(*args, **kwargs):
            bound = draw_signature.bind(*args, **kwargs)
            captured.append({
                name: bound.arguments[name]
                for name in ("selected", "scroll_offset", "agent_selected", "agent_scroll_offset", "focused_region")
            })
            return 2, None

        poller = unittest.mock.Mock(launch_id=None,
            snapshot=data,
            current_target=None,
            bell_target=None,
            current_agent=None,
            pane_active=False,
        )
        poller.tick.return_value = False
        screen = FakeScreen([curses.KEY_MOUSE, STOP], size=(20, 40))

        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[target]),
            patch.object(sidebar.cockpit, "current_target", return_value=None),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar.curses, "getmouse", return_value=(0, 0, 14, 0, curses.BUTTON5_PRESSED)),
            patch.object(sidebar, "_draw", side_effect=draw_spy),
        ):
            run(screen)

        self.assertGreaterEqual(len(captured), 2)
        self.assertEqual(captured[-1]["focused_region"], "sessions")
        self.assertEqual(captured[-1]["selected"], 0)
        self.assertEqual(captured[-1]["scroll_offset"], None)
        self.assertEqual(captured[-1]["agent_selected"], 0)
        self.assertEqual(captured[-1]["agent_scroll_offset"], 1)

    def test_left_click_maps_scrolled_agent_row_to_visible_agent(self):
        target, data = self._agent_data(4)
        second = data.agents[1].pane_target
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=data,
            current_target=None,
            bell_target=None,
            current_agent=None,
            pane_active=True,
        )
        poller.tick.return_value = False
        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, STOP], size=(20, 40))

        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[target]),
            patch.object(sidebar.cockpit, "current_target", return_value=None),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar.curses, "getmouse", side_effect=(
                (0, 0, 16, 0, curses.BUTTON5_PRESSED),
                (0, 0, 16, 0, curses.BUTTON1_PRESSED),
            )),
            patch.object(sidebar.cockpit, "switch") as switch,
        ):
            run(screen)

        _assert_deferred_switch(self, switch, target, "agent-2")
        self.assertEqual(switch.call_args.args[1](), sidebar.sessions.pane_attach_command(second))

    def test_right_click_maps_scrolled_agent_row_to_visible_agent(self):
        target, data = self._agent_data(4)
        second = data.agents[1].pane_target
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=data,
            current_target=None,
            bell_target=None,
            current_agent=None,
            pane_active=True,
        )
        poller.tick.return_value = False
        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, STOP], size=(20, 40))

        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[target]),
            patch.object(sidebar.cockpit, "current_target", return_value=None),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar, "_mouse_cleanup"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar.curses, "getmouse", side_effect=(
                (0, 0, 16, 0, curses.BUTTON5_PRESSED),
                (0, 7, 16, 0, curses.BUTTON3_PRESSED),
            )),
            patch.object(sidebar.cockpit, "show_agent_menu") as show_menu,
            patch.object(sidebar.cockpit, "switch") as switch,
        ):
            run(screen)

        show_menu.assert_called_once_with("pi", second, 7, 16, unittest.mock.ANY)
        switch.assert_not_called()

    def test_agent_keyboard_navigation_resets_agent_scroll_offset(self):
        for key in (ord("j"), ord("k"), curses.KEY_DOWN, curses.KEY_UP):
            with self.subTest(key=key):
                target, data = self._agent_data()
                captured = []
                draw_signature = inspect.signature(sidebar._draw)

                def draw_spy(*args, **kwargs):
                    bound = draw_signature.bind(*args, **kwargs)
                    captured.append(bound.arguments["agent_scroll_offset"])
                    return 2, None

                poller = unittest.mock.Mock(launch_id=None,
                    snapshot=data,
                    current_target=None,
                    bell_target=None,
                    current_agent=None,
                    pending_agent=None,
                    pane_active=True,
                )
                poller.tick.return_value = False
                screen = FakeScreen(
                    [curses.KEY_F7, curses.KEY_MOUSE, key, STOP], size=(20, 40)
                )

                with (
                    patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
                    patch.object(sidebar, "load_hosts", return_value=[]),
                    patch.object(config, "load_sessions", return_value=[target]),
                    patch.object(sidebar.cockpit, "current_target", return_value=None),
                    patch.object(sidebar, "_init_colors"),
                    patch.object(sidebar, "_mouse_mask"),
                    patch.object(sidebar.curses, "curs_set"),
                    patch.object(sidebar.curses, "getmouse", return_value=(0, 0, 14, 0, curses.BUTTON5_PRESSED)),
                    patch.object(sidebar, "_draw", side_effect=draw_spy),
                ):
                    run(screen)

                self.assertNotIn(1, captured)
                self.assertIsNone(captured[-1])

    def test_discovery_refresh_preserves_and_clamps_agent_scroll_offset(self):
        target, data = self._agent_data(6)
        reduced_data = SessionSnapshot(
            SourceSnapshot(
                True,
                (target,),
                frozenset(),
                agents=(data.agents[0],),
            ),
            {},
        )
        captured = []
        draw_signature = inspect.signature(sidebar._draw)
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=data,
            current_target=None,
            bell_target=None,
            current_agent=None,
            pane_active=True,
        )
        ticks = 0

        def tick(_now):
            nonlocal ticks
            ticks += 1
            if ticks == 3:
                poller.snapshot = reduced_data
                return True
            return False

        poller.tick.side_effect = tick
        screen = FakeScreen(
            [curses.KEY_MOUSE, curses.KEY_MOUSE, curses.KEY_MOUSE, curses.KEY_MOUSE, -1, -1, -1, STOP],
            size=(20, 40),
        )

        def draw_spy(*args, **kwargs):
            bound = draw_signature.bind(*args, **kwargs)
            captured.append(bound.arguments["agent_scroll_offset"])
            return 2, None

        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[target]),
            patch.object(sidebar.cockpit, "current_target", return_value=None),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(
                sidebar.curses,
                "getmouse",
                side_effect=[(0, 0, 14, 0, curses.BUTTON5_PRESSED)] * 4,
            ),
            patch.object(sidebar, "_draw", side_effect=draw_spy),
        ):
            run(screen)

        self.assertEqual(captured, [None, 4, 0])

    def test_agent_panel_resize_clamps_agent_scroll_offset(self):
        target, data = self._agent_data(6)
        captured = []
        draw_signature = inspect.signature(sidebar._draw)
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=data,
            current_target=None,
            bell_target=None,
            current_agent=None,
            pane_active=True,
        )
        poller.tick.return_value = False
        screen = FakeScreen(
            [curses.KEY_MOUSE] * 4 + [ord("["), STOP],
            size=(20, 40),
        )

        def draw_spy(*args, **kwargs):
            bound = draw_signature.bind(*args, **kwargs)
            captured.append((
                bound.arguments["agent_percentage"],
                bound.arguments["agent_scroll_offset"],
            ))
            return 2, None

        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[target]),
            patch.object(sidebar.cockpit, "current_target", return_value=None),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(
                sidebar.curses,
                "getmouse",
                side_effect=[(0, 0, 14, 0, curses.BUTTON5_PRESSED)] * 4,
            ),
            patch.object(sidebar, "load_agent_panel_resize_step", return_value=60),
            patch.object(sidebar, "_draw", side_effect=draw_spy),
        ):
            run(screen)

        self.assertEqual(captured, [(40, None), (40, 4), (100, 0)])


class AgentOrderingTest(OperationTestCase):
    def _make_agent(self, target, window_id, pane_id, agent_id, status):
        pane = PaneTarget(target, window_id, pane_id, "/tmp/tmux")
        return Entry(
            "pi", "agent", target, target.host or "laptop",
            pane_target=pane, agent_id=agent_id, status=status,
        )

    def test_agents_follow_session_window_pane_and_agent_id(self):
        first = Target("local", "first")
        second = Target("local", "second")
        entries = [
            self._make_agent(second, "@1", "%1", "second", "failed"),
            self._make_agent(first, "@2", "%1", "window-two", "input-required"),
            self._make_agent(first, "@1", "%10", "pane-ten", "completed"),
            self._make_agent(first, "@1", "%2", "z", "idle"),
            self._make_agent(first, "@1", "%2", "a", "working"),
        ]

        entries.sort(key=lambda entry: _agent_sort_key(entry, [first, second]))

        self.assertEqual(
            [entry.agent_id for entry in entries],
            ["a", "pane-ten", "window-two", "second", "z"],
        )

    def test_status_and_alert_changes_never_reorder_agents(self):
        target = Target("local", "work")
        first = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        second = PaneTarget(target, "@1", "%2", "/tmp/tmux")

        def ids(first_status, second_status):
            data = SessionSnapshot(
                SourceSnapshot(
                    True, (), frozenset(),
                    agents=(
                        AgentEntry(first, "first", "pi", first_status),
                        AgentEntry(second, "second", "pi", second_status),
                    ),
                ),
                {},
            )
            return [entry.agent_id for entry in _agent_entries(data, [target])]

        self.assertEqual(ids("idle", "input-required"), ["second", "first"])
        self.assertEqual(ids("failed", "working"), ["first", "second"])

    def test_priority_mode_alerts_override_status(self):
        target = Target("local", "work")
        bell = self._make_agent(target, "@1", "%1", "bell", "idle")
        urgent = self._make_agent(target, "@1", "%2", "urgent", "input-required")
        entries = [bell, urgent]
        entries.sort(key=lambda entry: _agent_sort_key(
            entry, [target], "priority", {(bell.pane_target, "bell")},
        ))
        self.assertEqual([entry.agent_id for entry in entries], ["urgent", "bell"])

    def test_session_mode_ignores_status_alerts_and_idle_recency(self):
        first = Target("local", "first")
        second = Target("local", "second")
        entries = [
            self._make_agent(second, "@1", "%1", "urgent", "input-required"),
            self._make_agent(first, "@1", "%1", "idle", "idle"),
        ]
        entries.sort(key=lambda entry: _agent_sort_key(entry, [first, second], "session"))
        self.assertEqual(
            [(entry.target.session, entry.agent_id) for entry in entries],
            [("first", "idle"), ("second", "urgent")],
        )

    def test_session_mode_uses_numeric_window_and_pane_ids(self):
        target = Target("local", "work")
        entries = [
            self._make_agent(target, "@10", "%1", "window-ten", "idle"),
            self._make_agent(target, "@2", "%10", "pane-ten", "idle"),
            self._make_agent(target, "@2", "%2", "pane-two", "idle"),
        ]
        entries.sort(key=lambda entry: _agent_sort_key(entry, [target], "session"))
        self.assertEqual([entry.agent_id for entry in entries], ["pane-two", "pane-ten", "window-ten"])

        self.assertEqual(sidebar._numeric_tmux_id("window"), (1, "window"))
        self.assertEqual(sidebar._numeric_tmux_id(None), (1, ""))

    def test_idle_since_tracks_transitions_not_producer_timestamps(self):
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        state = SidebarState(favorites=[target])

        def data(status):
            return SessionSnapshot(
                SourceSnapshot(True, (), frozenset(), agents=(AgentEntry(pane, "id", "pi", status),)),
                {},
            )

        sidebar._update_agent_alerts(state, data("idle"), None, now=10)
        self.assertEqual(state.agent_transition_at, {})
        sidebar._update_agent_alerts(state, data("idle"), None, now=20)
        self.assertEqual(state.agent_transition_at, {})
        sidebar._update_agent_alerts(state, data("working"), None, now=30)
        self.assertEqual(state.agent_transition_at, {(pane, "id"): 30})
        sidebar._update_agent_alerts(state, data("idle"), None, now=40)
        self.assertEqual(state.agent_transition_at, {(pane, "id"): 40})

    def test_agent_entries_sort_by_selected_mode(self):
        target = Target("local", "work")
        first = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        second = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        data = SessionSnapshot(
            SourceSnapshot(True, (), frozenset(), agents=(
                AgentEntry(first, "first", "pi", "idle"),
                AgentEntry(second, "second", "pi", "input-required"),
            )),
            {},
        )
        self.assertEqual([entry.agent_id for entry in _agent_entries(data, [target], "priority")], ["second", "first"])
        self.assertEqual([entry.agent_id for entry in _agent_entries(data, [target], "session")], ["first", "second"])

    def test_ordering_defaults_to_priority_and_preserves_agent_selection(self):
        self.assertEqual(SidebarState().agent_ordering, "priority")
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        other_pane = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        state = SidebarState(favorites=[target], agent_selected_index=1, selected_agent_key=(pane, "id"))
        priority = [
            Entry("", "order"),
            Entry("other", "agent", target, pane_target=other_pane, agent_id="other", status="input-required"),
            Entry("pi", "agent", target, pane_target=pane, agent_id="id", status="idle"),
        ]
        _sync_agent_selection(state, priority)
        self.assertEqual((state.agent_selected_index, state.selected_agent_key), (2, (pane, "id")))
        _sync_agent_selection(state, [priority[0], priority[2], priority[1]])
        self.assertEqual((state.agent_selected_index, state.selected_agent_key), (1, (pane, "id")))

    def test_ordering_row_is_two_rows_and_mouse_selectable(self):
        order = Entry("", "order")
        self.assertEqual(_entry_height(order), 2)
        entries = [order, Entry("pi", "agent", Target("local", "work"), agent_id="id")]
        self.assertEqual(_entry_at_row(entries, 0, 4, 9, 0, top=4), 0)
        self.assertEqual(_entry_at_row(entries, 0, 6, 9, 0, top=4), 1)

    def test_ordering_row_renders_in_unicode_and_ascii(self):
        screen = FakeScreen(size=(10, 40))
        _draw(screen, [], 0, "", "", agent_entries=[Entry("", "order")], agent_ordering="priority")
        text = [item[3] for item in screen.calls if item[0] == "addnstr"]
        self.assertTrue(any("⇅" in line and "Priority" in line and "Session" in line for line in text))
        with patch("letee.sidebar._ascii", return_value=True):
            screen = FakeScreen(size=(10, 40))
            _draw(screen, [], 0, "", "", agent_entries=[Entry("", "order")], agent_ordering="session")
        text = [item[3] for item in screen.calls if item[0] == "addnstr"]
        order_line = next(line for line in text if "Order:" in line)
        self.assertIn("PRIORITY", order_line)
        self.assertIn("SESSION", order_line)
        self.assertTrue(order_line.isascii())

    def test_empty_agent_view_keeps_ordering_row(self):
        screen = FakeScreen(size=(10, 40))
        _draw(screen, [], 0, "", "", agent_entries=[Entry("", "order")])
        text = [item[3] for item in screen.calls if item[0] == "addnstr"]
        self.assertTrue(any("⇅" in line for line in text))
        self.assertIn("  No active agents", text)

    def test_numeric_tmux_ids_sort_numerically(self):
        target = Target("local", "work")
        entries = [
            self._make_agent(target, "@10", "%1", "window-ten", "idle"),
            self._make_agent(target, "@2", "%10", "pane-ten", "idle"),
            self._make_agent(target, "@2", "%2", "pane-two", "idle"),
        ]

        entries.sort(key=lambda entry: _agent_sort_key(entry, [target]))

        self.assertEqual(
            [entry.agent_id for entry in entries],
            ["pane-two", "pane-ten", "window-ten"],
        )

    def test_selecting_idle_alerted_agent_clears_alert_after_focus_update_without_reordering(self):
        target = Target("local", "work")
        pane_a = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        pane_b = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        state = SidebarState(
            favorites=[target],
            agent_states={(pane_a, "a"): "idle", (pane_b, "b"): "idle"},
            agent_transition_at={(pane_a, "a"): 10, (pane_b, "b"): 20},
            agent_alerts={(pane_b, "b")},
        )
        data = SessionSnapshot(
            SourceSnapshot(
                True, (), frozenset(),
                agents=(AgentEntry(pane_a, "a", "pi", "idle"), AgentEntry(pane_b, "b", "pi", "idle")),
            ),
            {},
        )
        before = [e.agent_id for e in _agent_entries(data, [target], "priority", state.agent_alerts, state.agent_transition_at)]
        self.assertEqual(before, ["b", "a"])
        with patch("letee.sidebar.cockpit.switch"):
            _execute(Effect("switch_pane", pane_b, message="b"), state, unittest.mock.Mock(), 5)
        self.assertIn((pane_b, "b"), state.agent_alerts)
        self.assertEqual(state.agent_transition_at, {(pane_a, "a"): 10, (pane_b, "b"): 20})
        during_switch = [e.agent_id for e in _agent_entries(data, [target], "priority", state.agent_alerts, state.agent_transition_at)]
        self.assertEqual(during_switch, ["b", "a"])
        focused_data = SessionSnapshot(
            SourceSnapshot(
                True, (), frozenset(),
                agents=data.local.agents,
                focused_panes=frozenset({pane_b}),
            ),
            {},
        )
        _update_agent_alerts(state, focused_data, target)
        self.assertNotIn((pane_b, "b"), state.agent_alerts)
        after = [e.agent_id for e in _agent_entries(data, [target], "priority", state.agent_alerts, state.agent_transition_at)]
        self.assertEqual(after, ["b", "a"])

    def test_newer_idle_transition_moves_above_older_idle(self):
        target = Target("local", "work")
        pane_a = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        pane_b = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        state = SidebarState(favorites=[target])
        def data(a_status, b_status):
            return SessionSnapshot(
                SourceSnapshot(True, (), frozenset(), agents=(
                    AgentEntry(pane_a, "a", "pi", a_status),
                    AgentEntry(pane_b, "b", "pi", b_status),
                )),
                {},
            )
        _update_agent_alerts(state, data("idle", "idle"), None, now=10)
        _update_agent_alerts(state, data("working", "idle"), None, now=20)
        _update_agent_alerts(state, data("idle", "idle"), None, now=30)
        # a now has newer transition (30) than b (no transition yet, so b has no timestamp)
        entries = _agent_entries(data("idle", "idle"), [target], "priority", set(), state.agent_transition_at)
        self.assertEqual([e.agent_id for e in entries], ["a", "b"])
        # b then transitions and becomes newest
        _update_agent_alerts(state, data("idle", "working"), None, now=40)
        _update_agent_alerts(state, data("idle", "idle"), None, now=50)
        entries = _agent_entries(data("idle", "idle"), [target], "priority", set(), state.agent_transition_at)
        self.assertEqual([e.agent_id for e in entries], ["b", "a"])

    def test_non_idle_remains_above_newer_idle(self):
        target = Target("local", "work")
        idle = self._make_agent(target, "@1", "%2", "idle-agent", "idle")
        working = self._make_agent(target, "@1", "%1", "working-agent", "working")
        # idle has newer transition, but working group should still be first
        transition_at = {(working.pane_target, "working-agent"): 10, (idle.pane_target, "idle-agent"): 100}
        entries = [idle, working]
        entries.sort(key=lambda e: _agent_sort_key(e, [target], "priority", set(), transition_at))
        self.assertEqual([e.agent_id for e in entries], ["working-agent", "idle-agent"])

    def test_recently_transitioned_before_without_transition(self):
        target = Target("local", "work")
        pane_b = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        # both idle, only b has transition
        a = self._make_agent(target, "@1", "%1", "a", "idle")
        b = self._make_agent(target, "@1", "%2", "b", "idle")
        transition_at = {(pane_b, "b"): 10}
        entries = [a, b]
        entries.sort(key=lambda e: _agent_sort_key(e, [target], "priority", set(), transition_at))
        self.assertEqual([e.agent_id for e in entries], ["b", "a"])
        # same for non-idle group
        c = self._make_agent(target, "@1", "%3", "c", "working")
        d = self._make_agent(target, "@1", "%4", "d", "working")
        transition_at = {(PaneTarget(target, "@1", "%4", "/tmp/tmux"), "d"): 10}
        entries = [c, d]
        entries.sort(key=lambda e: _agent_sort_key(e, [target], "priority", set(), transition_at))
        self.assertEqual([e.agent_id for e in entries], ["d", "c"])

    def test_session_mode_ignores_transition_recency(self):
        target = Target("local", "work")
        a = self._make_agent(target, "@1", "%1", "a", "working")
        b = self._make_agent(target, "@1", "%2", "b", "idle")
        transition_at = {(a.pane_target, "a"): 10, (b.pane_target, "b"): 100}
        entries = [b, a]
        entries.sort(key=lambda e: _agent_sort_key(e, [target], "session", set(), transition_at))
        self.assertEqual([e.agent_id for e in entries], ["a", "b"])
        # also test that session order beats recency across sessions
        first = Target("local", "first")
        second = Target("local", "second")
        idle_second = self._make_agent(second, "@1", "%1", "idle", "idle")
        working_first = self._make_agent(first, "@1", "%1", "working", "working")
        transition_at = {(idle_second.pane_target, "idle"): 100, (working_first.pane_target, "working"): 10}
        entries = [idle_second, working_first]
        entries.sort(key=lambda e: _agent_sort_key(e, [first, second], "session", set(), transition_at))
        self.assertEqual([e.agent_id for e in entries], ["working", "idle"])

    def test_missing_agents_lose_transition_data(self):
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        state = SidebarState(
            favorites=[target],
            agent_states={(pane, "id"): "idle"},
            agent_transition_at={(pane, "id"): 10},
        )
        empty = SessionSnapshot(SourceSnapshot(True, (), frozenset(), agents=()), {})
        _update_agent_alerts(state, empty, None, now=20)
        self.assertEqual(state.agent_transition_at, {})
        self.assertEqual(state.agent_states, {})
        # also test kill path
        state = SidebarState(
            favorites=[target],
            agent_states={(pane, "id"): "working"},
            agent_transition_at={(pane, "id"): 10},
            selected_agent_key=(pane, "id"),
        )
        pane_target = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        with patch.object(sidebar.sessions, "kill_agent"):
            _execute(Effect("kill_agent", pane_target, message="pi", agent_id="id"), state, unittest.mock.Mock(), 5)
        self.assertNotIn((pane, "id"), state.agent_transition_at)
        self.assertNotIn((pane, "id"), state.agent_states)

    def test_ordering_row_ignores_enter_and_kill(self):
        screen = FakeScreen([curses.KEY_F7, curses.KEY_ENTER, ord("x"), STOP], size=(10, 40))
        with (
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[]),
            patch.object(sidebar.cockpit, "current_target", return_value=None),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", return_value=(2, None)),
            patch.object(sidebar, "_read_key") as read_key,
            patch.object(sidebar.sessions, "kill_agent") as kill_agent,
        ):
            run(screen)
        read_key.assert_not_called()
        kill_agent.assert_not_called()

    def test_left_and_right_toggle_ordering_on_selected_row(self):
        for key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            screen = FakeScreen([curses.KEY_F7, key, STOP], size=(10, 40))
            with (
                self.subTest(key=key),
                patch.object(sidebar, "AsyncStatusPoller") as poller_type,
                patch.object(sidebar, "load_hosts", return_value=[]),
                patch.object(config, "load_sessions", return_value=[]),
                patch.object(sidebar.cockpit, "current_target", return_value=None),
                patch.object(sidebar, "_init_colors"),
                patch.object(sidebar, "_mouse_mask"),
                patch.object(sidebar.curses, "curs_set"),
                patch.object(sidebar, "_draw", return_value=(2, None)) as draw,
            ):
                poller = unittest.mock.Mock(launch_id=None,
                    snapshot=snapshot(), current_target=None, bell_target=None,
                    current_agent=None, pending_agent=None, pane_active=True,
                )
                poller.tick.return_value = False
                poller_type.return_value = poller
                run(screen)
            self.assertEqual(draw.call_args_list[-1].kwargs["agent_ordering"], "session")

    def test_right_click_on_ordering_row_is_ignored(self):
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        data = SessionSnapshot(
            SourceSnapshot(True, (), frozenset(), agents=(AgentEntry(pane, "id", "pi", "working"),)),
            {},
        )
        screen = FakeScreen([curses.KEY_F7, curses.KEY_MOUSE, STOP], size=(12, 40))
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=data, current_target=None, bell_target=None,
            current_agent=None, pending_agent=None, pane_active=True,
        )
        poller.tick.return_value = False
        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[target]),
            patch.object(sidebar.cockpit, "current_target", return_value=None),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", return_value=(2, None)),
            patch.object(sidebar.curses, "getmouse", return_value=(0, 7, 6, 0, curses.BUTTON3_PRESSED)),
            patch.object(sidebar.cockpit, "show_agent_menu") as show_menu,
        ):
            run(screen)
        show_menu.assert_not_called()

    def test_mouse_ordering_resets_agent_scroll_offset(self):
        target = Target("local", "work")
        panes = [PaneTarget(target, "@1", f"%{index}", "/tmp/tmux") for index in range(1, 4)]
        data = SessionSnapshot(
            SourceSnapshot(
                True, (target,), frozenset(),
                agents=tuple(
                    AgentEntry(pane, f"id-{index}", "pi", "working")
                    for index, pane in enumerate(panes, 1)
                ),
            ),
            {},
        )
        captured = []
        draw_signature = inspect.signature(sidebar._draw)

        def draw_spy(*args, **kwargs):
            bound = draw_signature.bind(*args, **kwargs)
            captured.append((
                bound.arguments["agent_scroll_offset"], bound.arguments["agent_ordering"]
            ))
            return 2, None

        screen = FakeScreen(
            [curses.KEY_F7, curses.KEY_MOUSE, -1, curses.KEY_MOUSE, -1, curses.KEY_MOUSE, STOP],
            size=(12, 40),
        )
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=data, current_target=None, bell_target=None,
            current_agent=None, pending_agent=None, pane_active=True,
        )
        poller.tick.return_value = False
        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[target]),
            patch.object(sidebar.cockpit, "current_target", return_value=None),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", side_effect=draw_spy),
            patch.object(sidebar.sessions, "ensure_created") as ensure_created,
            patch.object(sidebar.curses, "getmouse", side_effect=(
                (0, 0, 8, 0, curses.BUTTON5_PRESSED),
                (0, 0, 8, 0, curses.BUTTON4_PRESSED),
                (0, 14, 6, 0, curses.BUTTON1_PRESSED),
            )),
        ):
            run(screen)
        ensure_created.assert_not_called()
        self.assertEqual(captured[-2:], [(0, "priority"), (None, "session")])


class PrefixActionTest(OperationTestCase):
    def _run(self, keys, favorites, current_target, data, *, seed_alerts=None, current_agent=None):
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=data,
            current_target=current_target,
            bell_target=None,
            startup_deadline=None,
            current_agent=current_agent,
            pending_agent=None,
            pane_active=True,
        )
        poller.tick.return_value = False
        screen = FakeScreen(keys, size=(12, 40))
        patches = [
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(config, "load_sessions", return_value=favorites),
            patch.object(sidebar.cockpit, "current_target", return_value=current_target),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar.curses, "curs_set"),
        ]
        alert_patch = (
            patch.object(sidebar, "_update_agent_alerts", side_effect=seed_alerts)
            if seed_alerts is not None else nullcontext()
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], alert_patch:
            run(screen)
        return screen

    def test_cycle_sessions_in_manual_order_with_pending_navigation(self):
        first, second, third = [Target("local", name) for name in ("z", "a", "m")]
        favorites = [first, second, third]
        data = snapshot(local=("a", "m", "z"))
        cases = (
            (first, [curses.KEY_F4] * 3, [second, third, first]),
            (first, [curses.KEY_F3] * 3, [third, second, first]),
            (None, [curses.KEY_F4], [first]),
            (None, [curses.KEY_F3], [third]),
            (Target("local", "untracked"), [curses.KEY_F4], [first]),
            (Target("local", "untracked"), [curses.KEY_F3], [third]),
            (second, [curses.KEY_UP, curses.KEY_F4], [third]),
            (second, [curses.KEY_DOWN, curses.KEY_F3], [first]),
        )
        for active, keys, expected in cases:
            with self.subTest(active=active, keys=keys):
                actions = unittest.mock.Mock(busy=True, blocks_favorite_changes=False)
                actions.poll.return_value = None
                actions.submit.return_value = True
                with patch.object(sidebar, "EffectRunner", return_value=actions):
                    self._run(keys + [STOP], favorites, active, data)
                self.assertEqual(
                    [call.args[0] for call in actions.submit.call_args_list],
                    [Effect("switch", target) for target in expected],
                )

    def test_cycle_sessions_includes_missing_and_unavailable_targets(self):
        active = Target("local", "active")
        missing = Target("local", "missing")
        remote = Target("ssh", "remote", "host")
        cases = (
            ({}, "switch"),
            ({"host": None}, "show_reconnecting"),
            ({"host": source("ssh", host="host", available=False)}, "show_reconnecting"),
        )
        for remotes, kind in cases:
            with self.subTest(remotes=remotes):
                data = snapshot(local=("active",), remotes=remotes)
                actions = unittest.mock.Mock(busy=True, blocks_favorite_changes=False)
                actions.poll.return_value = None
                actions.submit.return_value = True
                with patch.object(sidebar, "EffectRunner", return_value=actions):
                    self._run([curses.KEY_F4, curses.KEY_F4, STOP], [active, missing, remote], active, data)
                self.assertEqual(
                    [call.args[0] for call in actions.submit.call_args_list],
                    [Effect("switch", missing), Effect(kind, remote)],
                )

    def test_cycle_sessions_empty_and_single_lists(self):
        target = Target("local", "work")
        for favorites, active, expected in (([], None, []), ([target], target, []), ([target], None, [Effect("switch", target)])):
            with self.subTest(favorites=favorites, active=active):
                actions = unittest.mock.Mock(busy=False, blocks_favorite_changes=False)
                actions.poll.return_value = None
                actions.submit.return_value = True
                with patch.object(sidebar, "EffectRunner", return_value=actions):
                    self._run([curses.KEY_F3, curses.KEY_F4, STOP], favorites, active, snapshot(local=("work",)))
                self.assertEqual([call.args[0] for call in actions.submit.call_args_list], expected)

    def test_cycle_sessions_selects_and_reveals_destination(self):
        targets = [Target("local", f"session-{i}") for i in range(12)]
        captured = []
        signature = inspect.signature(sidebar._draw)

        def draw(*args, **kwargs):
            captured.append(signature.bind(*args, **kwargs).arguments)
            return 2, None

        actions = unittest.mock.Mock(busy=True, blocks_favorite_changes=False)
        actions.poll.return_value = None
        actions.submit.return_value = True
        with patch.object(sidebar, "EffectRunner", return_value=actions), patch.object(sidebar, "_draw", side_effect=draw):
            self._run([curses.KEY_F3, STOP], targets, targets[0], snapshot(local=tuple(t.session for t in targets)))
        last = captured[-1]
        self.assertEqual(last["entries"][last["selected"]].target, targets[-1])
        self.assertIsNone(last["scroll_offset"])
        self.assertEqual(last["focused_region"], "sessions")

    def test_cycle_agents_in_display_order_with_pending_navigation(self):
        first, second = Target("local", "first"), Target("local", "second")
        panes = [PaneTarget(t, "@1", f"%{i}", "/tmp/tmux") for i, t in enumerate((first, second, first))]
        agents = tuple(AgentEntry(pane, str(i), "pi", "working") for i, pane in enumerate(panes))
        data = SessionSnapshot(
            SourceSnapshot(True, (first, second), frozenset(), agents=agents, focused_panes=frozenset({panes[0]})),
            {},
        )
        # Equal-priority agents follow tracked session order, not discovery order.
        cases = (
            ("0", [curses.KEY_F5] * 3, [2, 1, 0]),
            ("0", [curses.KEY_F12] * 3, [1, 2, 0]),
            (None, [curses.KEY_F5], [2]),
            ("missing", [curses.KEY_F12], [1]),
        )
        for active, keys, expected in cases:
            with self.subTest(active=active, keys=keys):
                actions = unittest.mock.Mock(busy=True, blocks_favorite_changes=False)
                actions.poll.return_value = None
                actions.submit.return_value = True
                with patch.object(sidebar, "EffectRunner", return_value=actions):
                    self._run(keys + [STOP], [first, second], first, data, current_agent=active)
                self.assertEqual(
                    [(call.args[0].target, call.args[0].message) for call in actions.submit.call_args_list],
                    [(panes[i], str(i)) for i in expected],
                )

    def test_same_id_agents_use_exact_focused_and_pending_panes(self):
        target = Target("local", "work")
        panes = [PaneTarget(target, "@1", f"%{i}", "/tmp/tmux") for i in range(3)]
        agents = tuple(AgentEntry(pane, "shared", "pi", "working") for pane in panes)
        data = SessionSnapshot(
            SourceSnapshot(True, (target,), frozenset(), agents=agents, focused_panes=frozenset({panes[1]})),
            {},
        )
        cases = (
            ([curses.KEY_F7], [], panes[1]),
            ([curses.KEY_F5], [panes[2]], panes[2]),
            ([curses.KEY_F12], [panes[0]], panes[0]),
            ([curses.KEY_F5, curses.KEY_F5, curses.KEY_F7], [panes[2], panes[0]], panes[0]),
            ([curses.KEY_F12, curses.KEY_F12, curses.KEY_F7], [panes[0], panes[2]], panes[2]),
        )
        for keys, expected_switches, expected_selection in cases:
            with self.subTest(keys=keys):
                actions = unittest.mock.Mock(busy=True, blocks_favorite_changes=False)
                actions.poll.return_value = None
                actions.submit.return_value = True
                with (
                    patch.object(sidebar, "EffectRunner", return_value=actions),
                    patch.object(sidebar, "_draw", return_value=(2, None)) as draw,
                ):
                    with patch.object(sidebar.cockpit, "sidebar_compact", return_value=True):
                        self._run(keys + [STOP], [target], target, data, current_agent="shared")
                self.assertEqual(
                    [call.args[0].target for call in actions.submit.call_args_list], expected_switches,
                )
                rendered = inspect.signature(sidebar._draw).bind(*draw.call_args.args, **draw.call_args.kwargs).arguments
                self.assertEqual(
                    rendered["agent_entries"][rendered["agent_selected"]].pane_target, expected_selection,
                )
                self.assertEqual(rendered["active_agent_pane"], expected_selection)

    def test_cycle_after_effect_completion_before_discovery_confirmation(self):
        target = Target("local", "work")
        panes = [PaneTarget(target, "@1", f"%{i}", "/tmp/tmux") for i in range(4)]
        for ids in (("a", "b", "c", "d"), ("shared",) * 4):
            for key, expected in ((curses.KEY_F5, [2, 3]), (curses.KEY_F12, [0, 3])):
                with self.subTest(ids=ids, key=key):
                    data = SessionSnapshot(
                        SourceSnapshot(
                            True, (target,), frozenset(),
                            agents=tuple(AgentEntry(pane, agent_id, "pi", "working") for pane, agent_id in zip(panes, ids)),
                            focused_panes=frozenset({panes[1]}),
                        ), {},
                    )
                    status = sidebar.AsyncStatusPoller(unittest.mock.Mock(snapshot=data), target)
                    status.current_agent = ids[1]
                    status.tick = unittest.mock.Mock(return_value=False)
                    actions = unittest.mock.Mock(busy=False, blocks_favorite_changes=False)
                    completed = []
                    actions.submit.side_effect = lambda effect, *args, **kwargs: completed.append(sidebar.EffectResult(effect, ())) or True
                    actions.poll.side_effect = lambda: completed.pop(0) if completed else None
                    try:
                        with (
                            patch.object(sidebar, "AsyncStatusPoller", return_value=status),
                            patch.object(sidebar, "EffectRunner", return_value=actions),
                            patch.object(config, "load_sessions", return_value=[target]),
                            patch.object(sidebar.cockpit, "current_target", return_value=target),
                            patch.object(sidebar, "_init_colors"),
                            patch.object(sidebar.curses, "curs_set"),
                        ):
                            run(FakeScreen([key, key, STOP], size=(12, 40)))
                        self.assertEqual(
                            [call.args[0].target for call in actions.submit.call_args_list],
                            [panes[i] for i in expected],
                        )
                    finally:
                        status.close()

    def test_cycle_before_first_status_sample_uses_focused_agent(self):
        target = Target("local", "work")
        panes = [PaneTarget(target, "@1", f"%{i}", "/tmp/tmux") for i in range(3)]
        data = SessionSnapshot(
            SourceSnapshot(
                True, (target,), frozenset(),
                agents=tuple(AgentEntry(pane, "shared", "pi", "working") for pane in panes),
                focused_panes=frozenset({panes[1]}),
            ), {},
        )
        for key, expected in ((curses.KEY_F5, panes[2]), (curses.KEY_F12, panes[0])):
            with self.subTest(key=key):
                actions = unittest.mock.Mock(busy=True, blocks_favorite_changes=False)
                actions.poll.return_value = None
                actions.submit.return_value = True
                with patch.object(sidebar, "EffectRunner", return_value=actions):
                    self._run([key, STOP], [target], target, data)
                self.assertEqual(actions.submit.call_args.args[0].target, expected)

    def test_cycle_empty_and_single_agent_lists(self):
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        for agents in ((), (AgentEntry(pane, "id", "pi", "working"),)):
            with self.subTest(agents=agents):
                actions = unittest.mock.Mock(busy=False, blocks_favorite_changes=False)
                actions.poll.return_value = None
                actions.submit.return_value = True
                data = SessionSnapshot(SourceSnapshot(True, (target,), frozenset(), agents=agents), {})
                with patch.object(sidebar, "EffectRunner", return_value=actions):
                    self._run([curses.KEY_F12, curses.KEY_F5, STOP], [target], target, data, current_agent="id")
                self.assertEqual(actions.submit.call_count, 2 if agents else 0)
                for call in actions.submit.call_args_list:
                    self.assertEqual(call.args[0], Effect("switch_pane", pane, message="id"))

    def test_focus_selects_active_entries_without_switching(self):
        targets = [Target("local", f"session-{i}") for i in range(8)]
        active = targets[-1]
        panes = [PaneTarget(t, "@1", f"%{i}", "/tmp/tmux") for i, t in enumerate(targets)]
        agents = tuple(AgentEntry(pane, str(i), "pi", "working") for i, pane in enumerate(panes))
        data = SessionSnapshot(
            SourceSnapshot(True, tuple(targets), frozenset(), agents=agents, focused_panes=frozenset({panes[-1]})),
            {},
        )
        captured = []
        signature = inspect.signature(sidebar._draw)

        def draw(*args, **kwargs):
            captured.append(signature.bind(*args, **kwargs).arguments)
            return 2, None

        with (
            patch.object(sidebar, "_draw", side_effect=draw),
            patch.object(sidebar.cockpit, "switch") as switch,
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "getmouse", side_effect=(
                (0, 0, 3, 0, curses.BUTTON4_PRESSED),
                (0, 0, 8, 0, curses.BUTTON4_PRESSED),
            )),
        ):
            self._run(
                [curses.KEY_F6, curses.KEY_UP, curses.KEY_MOUSE, -1,
                 curses.KEY_F7, curses.KEY_UP, curses.KEY_MOUSE, -1,
                 curses.KEY_F6, curses.KEY_F7, STOP],
                targets, active, data, current_agent="7",
            )
        last = captured[-1]
        self.assertEqual(last["entries"][last["selected"]].target, active)
        self.assertEqual(last["agent_entries"][last["agent_selected"]].agent_id, "7")
        self.assertTrue(any(row["scroll_offset"] is not None for row in captured))
        self.assertTrue(any(row["agent_scroll_offset"] is not None for row in captured))
        self.assertIsNone(last["scroll_offset"])
        self.assertIsNone(last["agent_scroll_offset"])
        switch.assert_not_called()

    def test_remove_prefix_targets_active_session_not_stale_selection(self):
        stale = Target("local", "stale")
        active = Target("local", "active")
        data = snapshot(local=("stale", "active"))

        with patch.object(config, "save_sessions") as save, patch.object(sidebar.sessions, "kill") as kill:
            self._run([curses.KEY_F6, curses.KEY_F8, STOP], [stale, active], active, data)

        save.assert_called_once_with((stale,))
        kill.assert_not_called()

    def test_kill_prefix_targets_active_session_and_confirms(self):
        stale = Target("local", "stale")
        active = Target("local", "active")
        data = snapshot(local=("stale", "active"))

        # The marker must be set before the kill so the pane-died hook knows why it died.
        order = []

        def record(name):
            return lambda *args, **kwargs: order.append(name)

        with (
            patch.object(sidebar.sessions, "kill", side_effect=record("kill")) as kill,
            patch.object(config, "save_sessions") as save,
            patch.object(sidebar.cockpit, "set_expected_right_pane_death", side_effect=record("mark")) as expected_death,
            patch.object(sidebar.cockpit, "resolve_expected_right_pane_death", side_effect=lambda *_args: order.append("resolve")),
        ):
            self._run([curses.KEY_F6, curses.KEY_F9, ord("y"), STOP], [stale, active], active, data)

        kill.assert_called_once_with(active)
        expected_death.assert_called_once_with(active)
        self.assertEqual(order, ["mark", "kill", "resolve"])
        save.assert_called_once_with([stale])

    def test_kill_of_inactive_session_keeps_current_target(self):
        stale = Target("local", "stale")
        active = Target("local", "active")
        data = snapshot(local=("stale", "active"))

        with (
            patch.object(sidebar.sessions, "kill") as kill,
            patch.object(config, "save_sessions") as save,
            patch.object(sidebar.cockpit, "set_expected_right_pane_death") as expected_death,
        ):
            self._run([ord("x"), ord("y"), STOP], [stale, active], active, data)

        kill.assert_called_once_with(stale)
        expected_death.assert_not_called()
        save.assert_called_once_with([active])

    def test_absent_session_kill_prefix_guides_custom_removal(self):
        target = Target("local", "active")
        custom = {**config.DEFAULT_SIDEBAR_KEYBINDINGS, "remove": "D"}

        with patch.object(sidebar, "load_sidebar_keybindings", return_value=custom):
            screen = self._run([curses.KEY_F6, curses.KEY_F9, STOP], [target], target, snapshot())

        self.assertTrue(any(
            call[0] == "addnstr" and "Session unavailable; press D" in call[3]
            for call in screen.calls
        ))

    def test_rejected_kill_prefix_does_not_mutate_sessions(self):
        target = Target("local", "active")
        data = snapshot(local=("active",))

        with patch.object(sidebar.sessions, "kill") as kill, patch.object(config, "save_sessions") as save:
            self._run([curses.KEY_F6, curses.KEY_F9, ord("n"), STOP], [target], target, data)

        kill.assert_not_called()
        save.assert_not_called()

    def test_agents_x_confirms_and_dispatches_selected_pane(self):
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        data = SessionSnapshot(
            SourceSnapshot(
                True,
                (target,),
                frozenset(),
                agents=(AgentEntry(pane, "id", "pi", "working"),),
            ),
            {},
        )

        with patch.object(sidebar.sessions, "kill_agent") as kill_agent, patch.object(config, "save_sessions") as save:
            screen = self._run([curses.KEY_F7, curses.KEY_DOWN, ord("x"), ord("y"), STOP], [target], target, data)

        kill_agent.assert_called_once_with(pane)
        save.assert_not_called()
        prompt = next(
            call for call in screen.calls
            if call[0] == "addnstr" and "kill pi in local:work? y/N" in call[3]
        )
        self.assertGreater(prompt[1], 1)

    def test_rejected_agents_x_does_not_signal(self):
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        data = SessionSnapshot(
            SourceSnapshot(True, (target,), frozenset(), agents=(AgentEntry(pane, "id", "pi", "working"),)),
            {},
        )

        with patch.object(sidebar.sessions, "kill_agent") as kill_agent:
            self._run([curses.KEY_F7, curses.KEY_DOWN, ord("x"), ord("n"), STOP], [target], target, data)

        kill_agent.assert_not_called()

    def test_rejected_agents_x_repaints_agent_status(self):
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        data = SessionSnapshot(
            SourceSnapshot(True, (target,), frozenset(), agents=(AgentEntry(pane, "id", "pi", "working"),)),
            {},
        )
        statuses = []

        def draw_spy(*args, **kwargs):
            statuses.append((args[3], kwargs["status_region"]))
            return 2, None

        with patch.object(sidebar, "_draw", side_effect=draw_spy):
            self._run(
                [curses.KEY_F7, curses.KEY_DOWN, curses.KEY_F10, ord("x"), ord("n"), STOP],
                [target],
                target,
                data,
            )

        self.assertEqual(statuses.count(("no agents need attention", "agents")), 2)

    def test_busy_agents_x_skips_confirmation(self):
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        data = SessionSnapshot(
            SourceSnapshot(True, (target,), frozenset(), agents=(AgentEntry(pane, "id", "pi", "working"),)),
            {},
        )
        actions = unittest.mock.Mock(busy=True, blocks_favorite_changes=False)
        actions.poll.return_value = None

        with (
            patch.object(sidebar, "EffectRunner", return_value=actions),
            patch.object(sidebar, "_read_key") as read_key,
            patch.object(sidebar.sessions, "kill_agent") as kill_agent,
        ):
            self._run([curses.KEY_F7, curses.KEY_DOWN, ord("x"), ord("y"), STOP], [target], target, data)

        read_key.assert_not_called()
        kill_agent.assert_not_called()

    def test_busy_runner_skips_kill_confirmation(self):
        target = Target("local", "active")
        data = snapshot(local=("active",))
        actions = unittest.mock.Mock(busy=True, blocks_favorite_changes=True)
        actions.poll.return_value = None

        with (
            patch.object(sidebar, "EffectRunner", return_value=actions),
            patch.object(sidebar, "_read_key") as read_key,
            patch.object(sidebar.sessions, "kill") as kill,
        ):
            screen = self._run([curses.KEY_F6, curses.KEY_F9, ord("y"), STOP], [target], target, data)

        read_key.assert_not_called()
        kill.assert_not_called()
        self.assertTrue(any(
            "another action is still running" in call[3]
            for call in screen.calls
            if call[0] == "addnstr"
        ))

    def test_prefix_actions_never_fall_back_when_active_target_is_missing_or_untracked(self):
        tracked = Target("local", "tracked")
        active = Target("local", "active")
        data = snapshot(local=("tracked", "active"))

        for current_target, expected_status in ((active, "not tracked"), (None, "no active session")):
            with self.subTest(current_target=current_target), patch.object(sidebar.sessions, "kill") as kill, patch.object(config, "save_sessions") as save:
                screen = self._run(
                    [curses.KEY_F6, curses.KEY_F8, curses.KEY_F9, STOP],
                    [tracked], current_target, data,
                )

            kill.assert_not_called()
            save.assert_not_called()
            self.assertTrue(any(expected_status in call[3] for call in screen.calls if call[0] == "addnstr"))

    def test_alert_prefix_uses_first_alert_in_current_agent_order(self):
        target = Target("local", "work")
        first_pane = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        second_pane = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        agents = (
            AgentEntry(first_pane, "first", "pi", "completed"),
            AgentEntry(second_pane, "second", "pi", "input-required"),
        )
        data = SessionSnapshot(
            SourceSnapshot(True, (target,), frozenset(), agents=agents), {},
        )

        def seed_alerts(state, *_args, **_kwargs):
            state.agent_alerts.update({(first_pane, "first"), (second_pane, "second")})
            return False

        with patch.object(sidebar.cockpit, "switch") as switch:
            self._run([curses.KEY_F7, curses.KEY_F10, STOP], [target], None, data, seed_alerts=seed_alerts)

        _assert_deferred_switch(self, switch, target, "first")
        self.assertEqual(switch.call_args.args[1](), sidebar.sessions.pane_attach_command(first_pane))

    def test_alert_prefix_jumps_to_attention_states_without_bells(self):
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        for status in ("input-required", "auth-required", "failed", "rejected"):
            with self.subTest(status=status), patch.object(sidebar.cockpit, "switch") as switch:
                data = SessionSnapshot(
                    SourceSnapshot(True, (target,), frozenset(), agents=(
                        AgentEntry(pane, "agent", "pi", status),
                    )), {},
                )
                self._run([curses.KEY_F7, curses.KEY_F10, STOP], [target], None, data)

                _assert_deferred_switch(self, switch, target, "agent")
                self.assertEqual(switch.call_args.args[1](), sidebar.sessions.pane_attach_command(pane))

    def test_alert_prefix_mixed_bell_and_state_candidates_follow_current_order(self):
        target = Target("local", "work")
        first_pane = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        second_pane = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        for bell_first in (True, False):
            with self.subTest(bell_first=bell_first), patch.object(sidebar.cockpit, "switch") as switch:
                bell_pane = first_pane if bell_first else second_pane
                data = SessionSnapshot(
                    SourceSnapshot(True, (target,), frozenset(), agents=(
                        AgentEntry(first_pane, "first", "pi", "working" if bell_first else "input-required"),
                        AgentEntry(second_pane, "second", "pi", "input-required" if bell_first else "working"),
                    )), {},
                )

                def seed_alerts(state, *_args, **_kwargs):
                    state.agent_alerts.add((bell_pane, "first" if bell_first else "second"))
                    return False

                self._run([curses.KEY_F7, curses.KEY_F10, STOP], [target], None, data, seed_alerts=seed_alerts)

                _assert_deferred_switch(self, switch, target, "first")
                self.assertEqual(switch.call_args.args[1](), sidebar.sessions.pane_attach_command(first_pane))

    def test_alert_prefix_other_states_without_bells_do_not_navigate(self):
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        for status in ("working", "submitted", "idle", "completed", "canceled", "unknown"):
            with self.subTest(status=status), patch.object(sidebar.cockpit, "switch") as switch:
                data = SessionSnapshot(
                    SourceSnapshot(True, (target,), frozenset(), agents=(
                        AgentEntry(pane, "agent", "pi", status),
                    )), {},
                )
                self._run([curses.KEY_F7, curses.KEY_F10, STOP], [target], None, data)
                switch.assert_not_called()

    def test_alert_prefix_without_alerts_shows_feedback_and_does_not_navigate(self):
        target = Target("local", "work")
        data = snapshot(local=("work",))

        with patch.object(sidebar.cockpit, "switch") as switch:
            screen = self._run([curses.KEY_F7, curses.KEY_F10, STOP], [target], None, data)

        switch.assert_not_called()
        message = next(
            call for call in screen.calls
            if call[0] == "addnstr" and "no agents need attention" in call[3]
        )
        self.assertTrue(message[3].startswith("⚠ no agents need attention"))
        self.assertGreater(message[1], 2)

    def test_no_alert_message_uses_danger_color_and_warning_icon(self):
        screen = FakeScreen(size=(12, 40))
        with patch.object(sidebar, "_ascii", return_value=False), patch.dict(
            sidebar._COLOR, {"danger": 123}, clear=True
        ):
            _draw(
                screen,
                [],
                0,
                "no agents need attention",
                "",
                agent_entries=[],
                focused_region="agents",
                status_region="agents",
            )

        message = next(
            call for call in screen.calls
            if call[0] == "addnstr" and "no agents need attention" in call[3]
        )
        self.assertEqual(message[3], "⚠ no agents need attention")
        self.assertEqual(message[5], 123 | curses.A_BOLD)

    def test_failed_alert_switch_preserves_alert(self):
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        state = SidebarState(agent_alerts={(pane, "agent")})

        with patch.object(sidebar.cockpit, "switch", side_effect=SystemExit("switch failed")):
            _execute(Effect("switch_pane", pane, message="agent"), state, unittest.mock.Mock(), 5)

        self.assertEqual(state.agent_alerts, {(pane, "agent")})


class SidebarDiagnosticsTest(OperationTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tempdir.name) / "sidebar.jsonl"
        self.env = patch.dict(os.environ, {"LETEE_DEBUG_LOG": str(self.log_path)}, clear=True)
        self.env.start()

    def tearDown(self):
        sidebar.diagnostics.close()
        self.env.stop()
        self.tempdir.cleanup()

    def records(self):
        sidebar.diagnostics.flush()
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def test_status_worker_and_fallback_inherit_request_context(self):
        status = sidebar.AsyncStatusPoller(unittest.mock.Mock(snapshot=snapshot()), None)
        debug = sidebar.diagnostics.get_diagnostics()
        try:
            with (
                debug.context(action_id="action-parent", input_id="input-parent"),
                patch.object(sidebar.cockpit, "current_target", return_value=None),
                patch.object(sidebar.cockpit, "status_snapshot", side_effect=OSError("PRIVATE")),
            ):
                status.tick(0)
                status._future.result(timeout=2)
                status.tick(0)
        finally:
            status.close()
        records = self.records()
        request = next(r for r in records if r["event"] == "status_requested")
        error = next(r for r in records if r["event"] == "error")
        for key in ("request_id", "sample_id", "snapshot_id", "action_id", "input_id"):
            self.assertEqual(error[key], request[key])
        self.assertEqual(error["operation"], "status_sample")
        self.assertNotIn("PRIVATE", json.dumps(records))

    def test_status_unexpected_future_failure_is_correlated_and_propagated(self):
        status = sidebar.AsyncStatusPoller(unittest.mock.Mock(snapshot=snapshot()), None)
        try:
            with patch.object(status, "_sample", side_effect=ValueError("PRIVATE")):
                status.tick(0)
                with self.assertRaises(ValueError):
                    status._future.result(timeout=2)
                with self.assertRaises(ValueError):
                    status.tick(0)
        finally:
            status.close()
        records = self.records()
        request = next(r for r in records if r["event"] == "status_requested")
        errors = [r for r in records if r["event"] == "error"]
        self.assertEqual({r["name"] for r in errors}, {"status_sample", "status_collect"})
        self.assertTrue(all(r["sample_id"] == request["sample_id"] for r in errors))
        result = next(r for r in records if r["event"] == "status_result")
        self.assertEqual((result["accepted"], result["reason"]), (False, "worker_failed"))
        self.assertNotIn("PRIVATE", json.dumps(records))

    def test_effect_worker_failure_preserves_context_through_collection(self):
        runner = sidebar.EffectRunner()
        try:
            with (
                sidebar.diagnostics.get_diagnostics().context(request_id="parent-request"),
                patch.object(sidebar, "_perform_effect", side_effect=ValueError("PRIVATE")),
            ):
                runner.submit(sidebar.Effect("switch", Target("local", "one")), (), input_id="input-test")
                with self.assertRaises(ValueError):
                    runner._future.result(timeout=2)
                with self.assertRaises(ValueError):
                    runner.poll()
        finally:
            runner.close()
        records = self.records()
        errors = [r for r in records if r["event"] == "error"]
        self.assertEqual({r["operation"] for r in errors}, {"effect_worker", "effect_collect"})
        self.assertEqual(len({r["error_id"] for r in errors}), 1)
        self.assertTrue(all(r["request_id"] == "parent-request" and r["input_id"] == "input-test" for r in errors))
        self.assertNotIn("PRIVATE", json.dumps(records))

    def test_handled_effect_failures_and_partial_kill_do_not_log_status_text(self):
        target = Target("local", "one")
        for error in (OSError("PRIVATE"), SystemExit("PRIVATE"), subprocess.TimeoutExpired("PRIVATE", 1)):
            with self.subTest(error=type(error).__name__):
                with patch.object(sidebar.cockpit, "switch", side_effect=error):
                    result = sidebar._perform_effect(sidebar.Effect("switch", target), (target,))
                self.assertTrue(result.error)
        with (
            patch.object(sidebar.cockpit, "current_target", return_value=None),
            patch.object(sidebar.sessions, "kill"),
            patch.object(sidebar.cockpit, "reset_to_help", side_effect=OSError("PRIVATE")),
            patch.object(config, "save_sessions"),
        ):
            partial = sidebar._perform_effect(sidebar.Effect("kill", target), (target,))
        self.assertTrue(partial.partial_success)
        sidebar._log_effect_applied(partial, SidebarState(status="PRIVATE"))
        records = self.records()
        errors = [r for r in records if r["event"] == "error"]
        self.assertEqual(len(errors), 4)
        self.assertTrue(all(r["handling"] == "status" for r in errors))
        applied = next(r for r in records if r["event"] == "effect_applied")
        self.assertTrue(applied["status_present"])
        self.assertNotIn("PRIVATE", json.dumps(records))

    def test_restoration_wrapped_timeout_preserves_overlay_and_reports_failure(self):
        target = Target("ssh", "one", "host")
        timeout = subprocess.TimeoutExpired("PRIVATE command", 1, output="PRIVATE output")
        error = SystemExit("PRIVATE message")
        error.__cause__ = timeout
        with (
            patch.object(sidebar.cockpit, "current_target", return_value=target),
            patch.object(sidebar.cockpit, "startup_deadline", return_value=None),
            patch.object(sidebar.sessions, "ensure_created", side_effect=error),
            patch.object(sidebar.cockpit, "show_reconnecting") as reconnecting,
        ):
            result = sidebar._perform_effect(sidebar.Effect(
                "switch", target, automatic=True,
            ), ())
        self.assertTrue(result.error)
        self.assertFalse(result.stale_navigation)
        reconnecting.assert_not_called()
        sidebar._log_effect_applied(result, SidebarState(status=result.error))
        records = self.records()
        applied = next(r for r in records if r["event"] == "effect_applied")
        self.assertTrue(applied["error"])
        self.assertTrue(applied["status_present"])
        self.assertNotIn("PRIVATE", json.dumps(records))

    def test_compact_render_summarizes_statuses_without_labels(self):
        with (
            patch.object(sidebar.cockpit, "sidebar_compact", return_value=True),
            patch.object(sidebar, "_entries", return_value=[sidebar.Entry(
                "PRIVATE session label", "session", target=Target("local", "one"), status="unavailable",
            )]),
            patch.object(sidebar, "_agent_entries", return_value=[sidebar.Entry(
                "PRIVATE agent label", "agent", agent_id="agent-test", status="working",
            )]),
        ):
            records = self._run_mouse_trace([], keys=[-1, -1, STOP])
        render = next(r for r in records if r["event"] == "sidebar_render")
        self.assertEqual(render["mode"], "compact")
        self.assertEqual(render["session_statuses"], {"unavailable": 1})
        self.assertEqual(render["agent_statuses"], {"working": 1})
        self.assertEqual(render["viewport"]["width"], 30)
        visible = [r for r in records if r["event"] == "sidebar_entry_draw"]
        self.assertEqual({r["status"] for r in visible}, {"unavailable", "working"})
        self.assertTrue(all(r["compact"] and r["row_count"] == 1 for r in visible))
        renders = {r["render_id"]: r for r in records if r["event"] == "sidebar_render"}
        self.assertTrue(all(r["entry_id"] == renders[r["render_id"]]["entry_id"] for r in visible))
        self.assertNotIn("PRIVATE", json.dumps(records))

    def test_status_discovery_failure_keeps_snapshot_with_diagnostics(self):
        data = snapshot(local=("one",))
        discovery = unittest.mock.Mock(snapshot=data)
        discovery.tick.side_effect = subprocess.TimeoutExpired("PRIVATE", 1)
        status = sidebar.AsyncStatusPoller(discovery, None)
        try:
            with (
                patch.object(sidebar.cockpit, "current_target", return_value=None),
                patch.object(sidebar.cockpit, "status_snapshot", return_value=sidebar.cockpit.StatusSnapshot(None, None, None, True)),
            ):
                status.tick(0)
                status._future.result(timeout=2)
                status.tick(0)
                self.assertIs(status.snapshot, data)
        finally:
            status.close()
        error = next(r for r in self.records() if r["event"] == "error")
        self.assertEqual((error["operation"], error["error_type"]), ("status_sample", "TimeoutExpired"))
        self.assertIn("sample_id", error)
        self.assertNotIn("PRIVATE", json.dumps(self.records()))

    def test_layout_repair_handled_and_unexpected_failures_are_diagnosed(self):
        for error in (OSError("PRIVATE"), RuntimeError("PRIVATE")):
            with self.subTest(error=type(error).__name__):
                stop = threading.Event()
                def repair(_pane):
                    stop.set()
                    raise error
                with patch.object(sidebar.cockpit, "repair_layout", side_effect=repair):
                    if isinstance(error, OSError):
                        sidebar._maintain_sidebar(stop, "%1")
                    else:
                        with self.assertRaises(RuntimeError):
                            sidebar._maintain_sidebar(stop, "%1")
        records = self.records()
        errors = [r for r in records if r["event"] == "error"]
        self.assertEqual([r["handling"] for r in errors], ["ignore", "reraise"])
        self.assertTrue(all(r["operation"] == "layout_repair" for r in errors))
        self.assertNotIn("PRIVATE", json.dumps(records))

    def test_fallback_handlers_capture_types_without_exception_text(self):
        cases = (
            ("key_name", "letee.sidebar.curses.keyname", curses.error, lambda: sidebar._key_name(7)),
            ("tmux_query", "letee.sidebar.cockpit.tmux.out", OSError, lambda: sidebar._tmux_query("show-options")),
            ("mouse_capability", "letee.sidebar.curses.has_mouse", curses.error, sidebar._mouse_diagnostics),
            ("terminal_capability", "letee.sidebar.curses.tigetstr", ValueError, sidebar._mouse_diagnostics),
            ("init_colors", "letee.sidebar.curses.has_colors", curses.error, sidebar._init_colors),
            ("validate_session_name", "letee.sidebar.validate_name", SystemExit, lambda: sidebar._valid_session_name("one")),
            ("mouse_motion_mode", "letee.sidebar.os.write", OSError, sidebar._mouse_mask),
            ("mouse_mask", "letee.sidebar.curses.mousemask", curses.error, sidebar._mouse_mask),
            ("mouse_cleanup", "letee.sidebar.os.write", OSError, sidebar._mouse_cleanup),
        )
        for operation, target, error_type, invoke in cases:
            with self.subTest(operation=operation):
                start = len(self.records()) if self.log_path.exists() else 0
                with (
                    patch.object(sidebar.cockpit.tmux, "out", return_value=""),
                    patch.object(sidebar.os, "write", return_value=0),
                    patch.object(sidebar.curses, "mousemask", return_value=(0, 0)),
                    patch(target, side_effect=error_type("PRIVATE"), create=True),
                    sidebar.diagnostics.get_diagnostics().context(request_id="handler-request"),
                ):
                    invoke()
                errors = [r for r in self.records()[start:] if r["event"] == "error" and r.get("operation") == operation]
                self.assertTrue(errors)
                self.assertTrue(all(r["error_type"] == error_type.__name__ and r["request_id"] == "handler-request" for r in errors))
                self.assertTrue(all(r["handling"] == ("validation" if operation == "validate_session_name" else "ignore" if operation.startswith("mouse_m") or operation == "mouse_cleanup" else "fallback") for r in errors))
                if operation == "terminal_capability":
                    self.assertEqual({r["capability"] for r in errors}, {"kmous", "XM"})
        sidebar._numeric_tmux_id("invalid")
        self.assertTrue(any(r.get("operation") == "numeric_tmux_id" for r in self.records()))
        self.assertNotIn("PRIVATE", json.dumps(self.records()))

    def test_mouse_decode_handlers_keep_input_identity_in_normal_and_name_views(self):
        for prefix, operation in (([], "sidebar_mouse"), ([ord("e")], "name_mouse")):
            with self.subTest(operation=operation):
                start = len(self.records()) if self.log_path.exists() else 0
                records = self._run_mouse_trace(
                    [curses.error("PRIVATE")], keys=[*prefix, curses.KEY_MOUSE, -1, STOP],
                )[start:]
                errors = [r for r in records if r["event"] == "error" and r.get("operation") in ("read_mouse", operation)]
                self.assertEqual({r["operation"] for r in errors}, {"read_mouse", operation})
                received = next(r for r in records if r["event"] == "input_received" and r["key_name"] == "KEY_MOUSE")
                self.assertTrue(all(r["input_id"] == received["input_id"] and r["handling"] == "ignore" for r in errors))
                self.assertNotIn("PRIVATE", json.dumps(records))

    def test_status_handlers_report_sync_and_rename_failures(self):
        for name, keys in (("_sync_active_session", [-1, STOP]), ("_rename_key", [ord("e"), ord("x"), STOP])):
            with self.subTest(handler=name):
                start = len(self.records()) if self.log_path.exists() else 0
                with patch.object(sidebar, name, side_effect=SystemExit("PRIVATE")):
                    records = self._run_mouse_trace([], keys=keys)[start:]
                errors = [r for r in records if r["event"] == "error" and r.get("operation") == name[1:]]
                self.assertTrue(errors)
                self.assertTrue(all(r["handling"] == "status" and r["error_type"] == "SystemExit" for r in errors))
                if name == "_rename_key":
                    self.assertTrue(all(r["input_id"] for r in errors))
                # The status is intentionally visible, but its text must not enter diagnostics.
                self.assertNotIn("PRIVATE", json.dumps(records))

    def test_input_read_spans_record_timeout_and_duration_without_input(self):
        self._run_mouse_trace([], keys=[-1, STOP])
        records = self.records()
        starts = [r for r in records if r["event"] == "operation_start" and r.get("name") == "sidebar_input_read"]
        ends = [r for r in records if r["event"] == "operation_end" and r.get("name") == "sidebar_input_read"]
        self.assertTrue(starts)
        self.assertEqual(len(starts), len(ends))
        self.assertEqual(starts[0]["timeout_ms"], 0)
        for start, end in zip(starts, ends):
            self.assertEqual(start["operation_id"], end["operation_id"])
            self.assertEqual(start["timeout_ms"], end["timeout_ms"])
            self.assertGreaterEqual(end["duration_ms"], 0)
            self.assertNotIn("key_code", end)
            self.assertNotIn("key_name", end)

    def test_read_input_interrupt_is_reraised_or_cancels_name_editor(self):
        for prefix, handling in (([], "reraise"), ([ord("e")], "cancel")):
            with self.subTest(handling=handling):
                start = len(self.records()) if self.log_path.exists() else 0
                with patch.object(FakeScreen, "getch", side_effect=[*prefix, KeyboardInterrupt("PRIVATE"), STOP]):
                    if handling == "reraise":
                        with self.assertRaises(KeyboardInterrupt):
                            self._run_mouse_trace([])
                    else:
                        self._run_mouse_trace([])
                error = next(r for r in self.records()[start:] if r["event"] == "error" and r.get("operation") == "read_input")
                self.assertEqual((error["handling"], error["error_type"]), (handling, "KeyboardInterrupt"))
                self.assertNotIn("PRIVATE", json.dumps(self.records()[start:]))

    def test_compact_write_failure_ignores_only_bottom_right_row(self):
        for row, handling in ((0, "reraise"), (3, "ignore")):
            with self.subTest(row=row):
                start = len(self.records()) if self.log_path.exists() else 0
                screen = FakeScreen([], size=(4, 5))
                original = screen.addnstr

                def write(y, *args):
                    if y == row:
                        raise curses.error("PRIVATE")
                    return original(y, *args)

                with patch.object(screen, "addnstr", side_effect=write):
                    manager = self.assertRaises(curses.error) if handling == "reraise" else nullcontext()
                    with manager:
                        sidebar._draw_compact(
                            screen, [], [sidebar.Entry("PRIVATE label", "agent")],
                            0, 0, None, None, 50, set(), None, None, None, set(), None,
                        )
                error = next(r for r in self.records()[start:] if r["event"] == "error" and r.get("operation") == "compact_write_row")
                self.assertEqual((error["row"], error["handling"]), (row, handling))
                self.assertNotIn("PRIVATE", json.dumps(self.records()[start:]))

    def test_accepted_status_links_discovery_requests_to_visible_entries(self):
        data = sidebar.replace(
            snapshot(local=("one",), remotes={
                "dev": sidebar.replace(source("ssh", ("remote",), host="dev"), request_id="remote-request"),
                "pending": None,
            }),
            local=sidebar.replace(source("local", ("one",)), request_id="local-request"),
        )
        poller = sidebar.AsyncStatusPoller(unittest.mock.Mock(snapshot=data), None)
        try:
            with patch.object(sidebar.cockpit, "status_snapshot", return_value=sidebar.cockpit.StatusSnapshot(None, None, None, True)):
                poller.tick(0)
                poller._future.result(timeout=2)
                poller.tick(0)
            accepted_trace = dict(poller.snapshot_trace)
            with patch.object(poller, "tick", return_value=False):
                records = self._run_mouse_trace([], keys=[-1, STOP], poller=poller)
        finally:
            poller.close()
        requested = next(r for r in records if r["event"] == "status_requested")
        result = next(r for r in records if r["event"] == "status_result")
        completed = next(r for r in records if r["event"] == "status_worker_completed")
        self.assertTrue(result["accepted"])
        for record in (result, completed):
            self.assertEqual(record["discovery_requests"], {"local": "local-request", "dev": "remote-request", "pending": None})
        entries = next(r for r in records if r["event"] == "entries_built")
        visible = [r for r in records if r["event"] == "sidebar_entry_draw"]
        self.assertTrue(visible)
        for record in (result, completed, entries, *visible):
            for key, value in accepted_trace.items():
                self.assertEqual(record[key], value)
                self.assertEqual(record[key], requested[key])
        self.assertTrue(all(r["entry_id"] == entries["entry_id"] for r in visible))

    def test_background_remote_recovery_reaches_screen_without_input_or_switching_active_local(self):
        from letee.discovery import _source_result

        active = Target("local", "one")
        offline = _source_result(255, "", "PRIVATE connection failure", kind="ssh", host="dev", request_id="remote-offline")
        discovery = unittest.mock.Mock(snapshot=snapshot(local=("one",), remotes={"dev": offline}))
        poller = sidebar.AsyncStatusPoller(discovery, active)
        try:
            with (
                patch.object(sidebar.cockpit, "current_target", return_value=active),
                patch.object(sidebar.cockpit, "startup_deadline", return_value=None),
                patch.object(sidebar.cockpit, "status_snapshot", return_value=sidebar.cockpit.StatusSnapshot(active, None, None, True)),
            ):
                poller.tick(0)
                poller._future.result(timeout=2)
                poller.tick(0)
                recovered = _source_result(0, "remote:@1:%2:0::/tmp/remote\twork\n", "", kind="ssh", host="dev", request_id="remote-recovered")
                discovery.snapshot = snapshot(local=("one",), remotes={"dev": recovered})
                poller.tick(1)
                poller._future.result(timeout=2)
                poller.tick(1)
            with patch.object(poller, "tick", return_value=False), patch.object(poller, "close"):
                switch = self._run_mouse_trace([], keys=[-1, STOP], poller=poller, current_target=active, collect_records=False, size=(30, 30), favorites=[active, Target("ssh", "remote", "dev")])
            switch.assert_not_called()
            self.assertEqual(poller.current_target, active)
        finally:
            poller.close()
        records = self.records()
        accepted = [r for r in records if r["event"] == "status_result" and r["accepted"]]
        self.assertEqual([r["discovery_requests"]["dev"] for r in accepted], ["remote-offline", "remote-recovered"])
        drawn = next(r for r in records if r["event"] == "sidebar_entry_draw" and r.get("target") == "ssh:dev:remote")
        self.assertEqual(drawn["snapshot_id"], accepted[-1]["snapshot_id"])
        self.assertIsNone(drawn["status"])
        self.assertFalse(any(r["event"] == "input_received" for r in records))
        self.assertNotIn("PRIVATE", json.dumps(records))

    def test_failed_status_submission_preserves_snapshot_and_request_trace(self):
        data = snapshot(local=("one",))
        poller = sidebar.AsyncStatusPoller(unittest.mock.Mock(snapshot=data), None)
        original_trace = dict(poller.snapshot_trace)
        try:
            with (
                patch.object(poller._executor, "submit", side_effect=RuntimeError("PRIVATE")),
                sidebar.diagnostics.get_diagnostics().context(action_id="parent-action", input_id="parent-input"),
                self.assertRaises(RuntimeError),
            ):
                poller.tick(0)
            self.assertIs(poller.snapshot, data)
            self.assertEqual(poller.snapshot_trace, original_trace)
            self.assertIsNone(poller._future)
        finally:
            poller.close()
        records = self.records()
        requested = next(r for r in records if r["event"] == "status_requested")
        failed = next(r for r in records if r["event"] == "error" and r.get("name") == "status_submit")
        for key in ("request_id", "sample_id", "snapshot_id", "action_id", "input_id"):
            self.assertEqual(failed[key], requested[key])
        self.assertEqual((failed["handling"], failed["error_type"]), ("propagate", "RuntimeError"))
        self.assertFalse(any(r["event"] in ("status_worker_started", "status_result") for r in records))
        self.assertNotIn("PRIVATE", json.dumps(records))

    def test_tmux_version_failures_keep_startup_running(self):
        for error in (OSError("PRIVATE"), SystemExit("PRIVATE"), subprocess.TimeoutExpired("PRIVATE", 1)):
            with self.subTest(error=type(error).__name__):
                start = len(self.records()) if self.log_path.exists() else 0
                def query(*args, **kwargs):
                    if args == ("-V",):
                        raise error
                    return ""

                records = self._run_mouse_trace([], keys=[-1, STOP], tmux_side_effect=query)[start:]
                failed = next(r for r in records if r["event"] == "error" and r.get("operation") == "tmux_version")
                startup = next(r for r in records if r["event"] == "sidebar_startup")
                self.assertEqual((failed["error_type"], failed["handling"]), (type(error).__name__, "fallback"))
                self.assertIsNone(startup["tmux_version"])
                self.assertNotIn("PRIVATE", json.dumps(records))

    def test_kill_failure_logs_same_error_at_handler_and_effect_boundary(self):
        target = Target("local", "one")
        for error in (OSError("PRIVATE"), SystemExit("PRIVATE"), subprocess.TimeoutExpired("PRIVATE", 1)):
            with self.subTest(error=type(error).__name__):
                start = len(self.records()) if self.log_path.exists() else 0
                with (
                    patch.object(sidebar.cockpit, "current_target", return_value=None),
                    patch.object(sidebar.sessions, "kill", side_effect=error),
                ):
                    result = sidebar._perform_effect(sidebar.Effect("kill", target), (target,))
                self.assertTrue(result.error)
                errors = [r for r in self.records()[start:] if r["event"] == "error"]
                self.assertEqual({r["operation"] for r in errors}, {"kill_session", "perform_effect"})
                self.assertEqual({r["handling"] for r in errors}, {"reraise", "status"})
                self.assertEqual(len({r["error_id"] for r in errors}), 1)
                self.assertNotIn("PRIVATE", json.dumps(self.records()[start:]))

    def test_wrapper_interrupt_is_diagnosed_before_retry_and_close(self):
        with patch.object(sidebar.curses, "wrapper", side_effect=[KeyboardInterrupt("PRIVATE"), None]) as wrapper:
            self.assertEqual(sidebar.main(), 0)
        self.assertEqual(wrapper.call_count, 2)
        errors = [r for r in self.records() if r["event"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual((errors[0]["operation"], errors[0]["handling"], errors[0]["error_type"]),
                         ("sidebar_wrapper", "retry", "KeyboardInterrupt"))
        self.assertNotIn("PRIVATE", json.dumps(errors))

    def test_executor_submission_failure_is_diagnosed(self):
        for runner in (
            sidebar.AsyncStatusPoller(unittest.mock.Mock(snapshot=snapshot()), None),
            sidebar.EffectRunner(),
        ):
            try:
                with patch.object(runner._executor, "submit", side_effect=RuntimeError("PRIVATE")):
                    with self.assertRaises(RuntimeError):
                        if isinstance(runner, sidebar.AsyncStatusPoller):
                            runner.tick(0)
                        else:
                            runner.submit(sidebar.Effect("switch", Target("local", "one")), ())
            finally:
                runner.close()
        errors = [r for r in self.records() if r["event"] == "error"]
        self.assertEqual({r["name"] for r in errors}, {"status_submit", "effect_submit"})
        self.assertTrue(all(r["handling"] == "propagate" for r in errors))
        self.assertNotIn("PRIVATE", json.dumps(errors))

    def test_restoration_runner_rejection_is_linked_to_automatic_request(self):
        target = Target("local", "one")
        marker = sidebar._sync_active_session(
            target, snapshot(local=("one",)), target, submit=lambda effect: False,
        )
        self.assertEqual(marker, target)
        records = self.records()
        request = next(r for r in records if r["event"] == "restoration_requested")
        deferred = next(r for r in records if r["event"] == "restoration_deferred")
        self.assertEqual(deferred["reason"], "runner_rejected")
        for key in ("target", "effect", "automatic", "source"):
            self.assertEqual(deferred[key], request[key])
        self.assertEqual((deferred["automatic"], deferred["source"]), (True, "automatic"))

    def test_background_recovery_without_input_links_failure_backoff_and_retry(self):
        target = Target("ssh", "one", "dev")
        connected = snapshot(remotes={"dev": source("ssh", ("one",), host="dev")})
        disconnected = snapshot(remotes={"dev": source("ssh", ("one",), host="dev", available=False)})
        poller = unittest.mock.Mock(
            snapshot=disconnected, current_target=target, bell_target=None, current_agent=None,
            pane_active=True, refresh_pending=True, status_sampled=True,
            startup_deadline=None, launch_id=None, attachment_failed=False,
            snapshot_trace={"snapshot_id": "recovery-snapshot"},
        )
        ticks = []

        def tick(_now):
            ticks.append(_now)
            poller.refresh_pending = False
            if len(ticks) > 2:
                poller.snapshot = replace(connected, remotes={
                    "dev": replace(connected.remotes["dev"], sampled_at=_now),
                })
            return True

        poller.tick.side_effect = tick
        runner = sidebar.EffectRunner()
        submit = runner._executor.submit

        def finish_worker(*args, **kwargs):
            future = submit(*args, **kwargs)
            future.result(timeout=2)
            return future

        clock = [0]

        def monotonic():
            clock[0] += 0.1
            return clock[0]

        with (
            patch.object(sidebar, "EffectRunner", return_value=runner),
            patch.object(runner._executor, "submit", side_effect=finish_worker),
            patch.object(sidebar.time, "monotonic", side_effect=monotonic),
            patch.object(sidebar, "load_timing", return_value=config.Timing()),
            patch.object(sidebar.sessions, "ensure_created"),
            patch.object(sidebar.sessions, "attach_command", return_value=["tmux", "attach"]),
            patch.object(sidebar.cockpit, "startup_deadline", return_value=None),
            patch.object(sidebar.cockpit, "show_reconnecting") as overlay,
        ):
            records = self._run_mouse_trace(
                [], keys=[*([-1] * 24), STOP], poller=poller, current_target=target,
                switch_side_effect=[OSError("PRIVATE"), None],
            )
        overlay.assert_called_once_with(target)
        heartbeat = next(r for r in records if r["event"] == "sidebar_heartbeat")
        self.assertEqual(heartbeat["restoration_defer_reason"], "refresh_pending")
        requests = [r for r in records if r["event"] == "restoration_requested"]
        self.assertEqual([r["effect"] for r in requests], ["show_reconnecting", "switch", "switch"])
        navigation = {"switch", "show_reconnecting"}
        submitted = [r for r in records if r["event"] == "effect_submitted" and r["accepted"]
                     and r["effect"] in navigation]
        self.assertEqual(len(submitted), 3)
        self.assertTrue(all(r["automatic"] and r.get("input_id") is None for r in submitted))
        for event in ("effect_worker_completed", "effect_applied"):
            self.assertEqual([r["action_id"] for r in submitted], [r["action_id"] for r in records
                             if r["event"] == event and r["effect"] in navigation])
        failed = next(r for r in records if r["event"] == "error" and r.get("operation") == "perform_effect")
        backoff = next(r for r in records if r["event"] == "restoration_backoff")
        self.assertEqual(failed["action_id"], submitted[1]["action_id"])
        self.assertEqual(backoff["action_id"], failed["action_id"])
        self.assertNotIn("PRIVATE", json.dumps(records))

    def test_restoration_backoff_defer_and_stale_result_are_diagnosed(self):
        target = Target("local", "one")
        retry = sidebar.RestorationRetry()
        failed = sidebar.EffectResult(
            sidebar.Effect("switch", target, automatic=True), (), error="PRIVATE",
            action_id="restore-action", input_id="restore-input", completed_at=10,
        )
        with patch.object(sidebar, "load_timing", return_value=config.Timing()):
            sidebar._reconcile_active_session_effect(target, failed, retry=retry)
            sidebar._sync_active_session(target, snapshot(local=("one",)), target, retry=retry, now=10.5)
            sidebar._reconcile_active_session_effect(
                target, sidebar.replace(failed, stale_navigation=True), retry=retry,
            )
        records = self.records()
        backoff = next(r for r in records if r["event"] == "restoration_backoff")
        self.assertEqual((backoff["delay"], backoff["deadline"]), (1, 11))
        self.assertEqual(backoff["action_id"], "restore-action")
        deferred = next(r for r in records if r["event"] == "restoration_deferred")
        self.assertEqual(deferred["reason"], "backoff")
        outcomes = [r["outcome"] for r in records if r["event"] == "restoration_result"]
        self.assertEqual(outcomes, ["failed", "stale_navigation"])
        self.assertNotIn("PRIVATE", json.dumps(records))

    def test_restoration_deferral_heartbeat_and_launch_reset_do_not_change_retry_equality(self):
        target = Target("local", "one")
        retry = sidebar.RestorationRetry(target, 10, 20)
        expected = sidebar.RestorationRetry(target, 10, 20)
        for now in (10, 10.1, 11, 15, 15.1):
            sidebar._sync_active_session(target, snapshot(local=("one",)), target, retry=retry, now=now)
        self.assertEqual(retry, expected)
        result = sidebar.EffectResult(sidebar.Effect("switch", target, automatic=True), (), action_id="restore-action")
        self.assertIsNone(sidebar._reconcile_active_session_effect(target, result, retry=retry))
        records = self.records()
        self.assertEqual(len([r for r in records if r["event"] == "restoration_deferred"]), 2)
        completed = next(r for r in records if r["event"] == "restoration_result")
        self.assertEqual(completed["outcome"], "attachment_launched")
        self.assertEqual(completed["action_id"], "restore-action")
        self.assertEqual(len([r for r in records if r["event"] == "restoration_reset"]), 0)
        self.assertEqual(retry, expected)

    def test_draw_and_refresh_failures_retry_with_inherited_render_correlation(self):
        trace = {"request_id": "request-test", "sample_id": "sample-test", "snapshot_id": "snapshot-test"}
        for method, operation in (("addnstr", "sidebar_draw"), ("refresh", "sidebar_refresh")):
            with self.subTest(method=method):
                start = len(self.records()) if self.log_path.exists() else 0
                poller = unittest.mock.Mock(
                    snapshot=snapshot(local=("one",)), current_target=None, bell_target=None,
                    current_agent=None, pane_active=True, snapshot_trace=trace,
                )
                original = getattr(FakeScreen, method)
                calls = []

                def fail_once(screen, *args, **kwargs):
                    calls.append(args)
                    if len(calls) == 1:
                        raise curses.error("PRIVATE")
                    return original(screen, *args, **kwargs)

                with patch.object(FakeScreen, method, autospec=True, side_effect=fail_once):
                    records = self._run_mouse_trace([], keys=[-1, -1, STOP], poller=poller)[start:]
                failed = next(r for r in records if r["event"] == "sidebar_render" and r["outcome"] == "draw_failed")
                drawn = next(r for r in records if r["event"] == "sidebar_render" and r["outcome"] == "drawn")
                errors = [r for r in records if r["event"] == "error" and r.get("render_id") == failed["render_id"]]
                self.assertEqual(len(errors), 2 if method == "addnstr" else 3)
                self.assertEqual(len({r["error_id"] for r in errors}), 1)
                self.assertTrue(any(r.get("name") == operation and r["handling"] == "propagate" for r in errors))
                self.assertTrue(any(r.get("operation") == "sidebar_draw" and r["handling"] == "retry" for r in errors))
                if method == "refresh":
                    refresh_error = next(r for r in errors if r.get("name") == "sidebar_refresh")
                    draw_error = next(r for r in errors if r.get("name") == "sidebar_draw")
                    self.assertEqual(refresh_error["parent_operation_id"], draw_error["operation_id"])
                for record in [*errors, failed, drawn]:
                    for key, value in trace.items():
                        self.assertEqual(record[key], value)
                    self.assertEqual(record["entry_id"], failed["entry_id"])
                visible = [r for r in records if r["event"] == "sidebar_entry_draw" and r["render_id"] == drawn["render_id"]]
                self.assertTrue(visible)
                self.assertTrue(all(r["entry_id"] == drawn["entry_id"] and r["snapshot_id"] == trace["snapshot_id"] for r in visible))
                self.assertNotIn("PRIVATE", json.dumps(records))

    def test_unchanged_render_is_deduplicated_but_heartbeat_repeats(self):
        clock = [0.0]
        original_getch = FakeScreen.getch

        def getch(screen):
            clock[0] += 0.1
            return original_getch(screen)

        with (
            patch.object(sidebar.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(FakeScreen, "getch", getch),
        ):
            records = self._run_mouse_trace([], keys=[*([-1] * 200), STOP])
        renders = [r for r in records if r["event"] == "sidebar_render"]
        hits = [r for r in renders if r["outcome"] == "cache_hit"]
        self.assertGreaterEqual(len(hits), 2)
        self.assertLess(len(hits), 10)
        heartbeats = [r for r in records if r["event"] == "sidebar_heartbeat" and r["reason"] == "idle"]
        self.assertGreaterEqual(len(heartbeats), 1)
        for record in [*renders, *heartbeats]:
            self.assertEqual(record["mode"], "normal")
            self.assertEqual(record["session_count"], 1)
            self.assertIn("session_statuses", record)
            self.assertIn("session_scroll_offset", record)
            self.assertNotIn("status_text", record)

    def test_status_sample_trace_accepts_and_discards_without_changing_snapshot(self):
        data = snapshot(local=("one",))
        status = sidebar.AsyncStatusPoller(unittest.mock.Mock(snapshot=data), None)
        try:
            with patch.object(sidebar.cockpit, "status_snapshot", return_value=None):
                status.tick(0)
                status._future.result(timeout=2)
                status.tick(0)
                self.assertIs(status.snapshot, data)
                status.tick(1)
                status._future.result(timeout=2)
                status._generation += 1
                status.tick(1)
        finally:
            status.close()
        records = self.records()
        requested = [r for r in records if r["event"] == "status_requested"]
        results = [r for r in records if r["event"] == "status_result"]
        self.assertEqual([r["accepted"] for r in results[:2]], [True, False])
        self.assertEqual(results[1]["reason"], "generation_changed")
        self.assertEqual(requested[0]["request_id"], results[0]["request_id"])
        self.assertTrue(results[0]["sample_id"])
        self.assertTrue(results[0]["snapshot_id"])
        self.assertEqual(status.snapshot_trace, {key: results[0][key] for key in ("request_id", "sample_id", "snapshot_id")})
        self.assertTrue(any(r["event"] == "status_worker_started" for r in records))
        self.assertTrue(any(r.get("handling") == "fallback" for r in records))

    def test_render_trace_links_entries_draw_and_cache_without_terminal_text(self):
        records = self._run_mouse_trace([], keys=[-1, -1, STOP])
        entries = next(r for r in records if r["event"] == "entries_built")
        renders = [r for r in records if r["event"] == "sidebar_render"]
        self.assertTrue(any(r["outcome"] == "drawn" for r in renders))
        self.assertTrue(any(r["outcome"] == "cache_hit" for r in renders))
        self.assertTrue(all(r["entry_id"] == entries["entry_id"] for r in renders))
        self.assertFalse(any("terminal_text" in r or "lines" in r for r in records))

    def test_mouse_press_trace_correlates_receive_map_submit_and_completion(self):
        target = Target("local", "one")
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=snapshot(local=("one",)),
            current_target=None,
            bell_target=None,
            current_agent=None,
            pane_active=True,
        )
        poller.tick.return_value = False
        screen = FakeScreen([curses.KEY_MOUSE, -1, STOP], size=(12, 30))
        runner = sidebar.EffectRunner()
        getch = screen.getch

        def wait_for_switch():
            key = getch()
            if key == -1 and runner._future is not None:
                runner._future.result(timeout=1)
            return key

        with (
            patch.object(screen, "getch", side_effect=wait_for_switch),
            patch.object(sidebar, "EffectRunner", return_value=runner),
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "DiscoveryPoller"),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[target]),
            patch.object(sidebar.cockpit, "current_target", return_value=None),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask", return_value=(0, 0)),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar.curses, "getmouse", return_value=(0, 0, 2, 0, curses.BUTTON1_PRESSED)),
            patch.dict(os.environ, {"TERM_PROGRAM": "kitty", "TERM_PROGRAM_VERSION": "0.40.1"}),
            patch.object(sidebar.cockpit.tmux, "out", return_value="tmux 3.4"),
            patch.object(sidebar.cockpit, "switch"),
        ):
            sidebar.run(screen)

        records = self.records()
        startup = next(record for record in records if record["event"] == "sidebar_startup")
        self.assertEqual(
            (startup["letee_version"], startup["screen_height"], startup["screen_width"]),
            (sidebar.__version__, 12, 30),
        )
        self.assertEqual(
            (startup["requested_mouse_mask"], startup["supported_mouse_mask"], startup["tmux_version"]),
            (0, 0, "tmux 3.4"),
        )
        self.assertEqual(startup["term_program"], "kitty")
        self.assertEqual(startup["term_program_version"], "0.40.1")
        input_record = next(record for record in records if record["event"] == "input_received" and record["key_name"] == "KEY_MOUSE")
        related = [record for record in records if record.get("input_id") == input_record["input_id"]]
        mapped = next(record for record in related if record["event"] == "mouse_mapped")
        submitted = next(record for record in related if record["event"] == "effect_submitted")
        completed = next(record for record in records if record["event"] == "effect_worker_completed")
        applied = next(record for record in records if record["event"] == "effect_applied")

        self.assertEqual(mapped["target"], target.format())
        self.assertEqual(mapped["row"], 2)
        self.assertTrue(submitted["accepted"])
        self.assertEqual(submitted["action_id"], completed["action_id"])
        self.assertEqual(completed["action_id"], applied["action_id"])
        self.assertEqual(applied["selected_target"], target.format())
        self.assertEqual(completed["target"], target.format())
        processed = [record for record in records if record["event"] == "input_processed"]
        self.assertEqual(
            {record["input_id"] for record in processed},
            {record["input_id"] for record in records if record["event"] == "input_received"},
        )
        self.assertTrue(all("applied" in record and "ignored" in record for record in processed))

    def _run_mouse_trace(
        self, mouse_events, keys=None, poller=None, collect_records=True,
        current_target=None, switch_side_effect=None, tmux_side_effect=None, size=(12, 30), favorites=None,
    ):
        target = Target("local", "one")
        poller = poller or unittest.mock.Mock(
            snapshot=snapshot(local=("one",)),
            current_target=None,
            bell_target=None,
            current_agent=None,
            pending_agent=None,
            pane_active=True,
        )
        poller.tick.return_value = False
        screen = FakeScreen(keys or [*(curses.KEY_MOUSE for _ in mouse_events), -1, STOP], size=size)
        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "DiscoveryPoller"),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[target] if favorites is None else favorites),
            patch.object(sidebar.cockpit, "current_target", return_value=current_target),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask", return_value=(0, 0)),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar.curses, "getmouse", side_effect=mouse_events),
            patch.object(sidebar.cockpit.tmux, "out", return_value="tmux 3.4", side_effect=tmux_side_effect),
            patch.object(sidebar.cockpit, "switch", side_effect=switch_side_effect) as switch,
        ):
            sidebar.run(screen)
        return self.records() if collect_records else switch

    def test_ignored_mouse_events_record_their_reason(self):
        records = self._run_mouse_trace([
            (0, 0, 2, 0, curses.BUTTON1_RELEASED),
            (0, 0, 2, 0, "bad"),
            (0, 0, 1, 0, curses.BUTTON1_PRESSED),
        ])

        decisions = [
            (record["decision"], record.get("reason"))
            for record in records
            if record["event"] == "mouse_decision"
        ]
        self.assertIn(("ignored_release", "release_without_press"), decisions)
        self.assertIn(("malformed_event", "invalid_row_or_state"), decisions)
        self.assertIn(("empty_row", None), decisions)
        received = [record for record in records if record["event"] == "input_received"]
        processed = [record for record in records if record["event"] == "input_processed"]
        self.assertEqual(
            [record["input_id"] for record in processed],
            [record["input_id"] for record in received],
        )
        self.assertTrue(all(record["ignored"] == (not record["applied"]) for record in processed))

    def test_wheel_outside_list_regions_records_ignored_decision(self):
        records = self._run_mouse_trace([
            (0, 4, 0, 0, curses.BUTTON5_PRESSED),
        ])

        decision = next(
            record for record in records
            if record["event"] == "mouse_decision"
        )
        self.assertEqual(
            (decision["decision"], decision["row"], decision["column"], decision["direction"]),
            ("ignored_wheel", 0, 4, "down"),
        )

    def test_failed_mouse_followed_by_release_activates_mapped_target(self):
        records = self._run_mouse_trace(
            [curses.error(), (0, 7, 2, 0, curses.BUTTON1_RELEASED)],
            keys=[curses.KEY_MOUSE, curses.KEY_MOUSE, -1, STOP],
        )

        candidates = [
            record for record in records if record["event"] == "mouse_recovery_candidate"
        ]
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        received = [record for record in records if record["event"] == "input_received"]
        self.assertEqual(candidate["failed_input_id"], received[0]["input_id"])
        self.assertEqual(candidate["release_input_id"], received[1]["input_id"])
        self.assertEqual((candidate["release_row"], candidate["release_column"]), (2, 7))
        self.assertEqual(candidate["region_hint"], "sessions")
        self.assertLess(candidate["failure_age_ms"], 1_000)
        effects = [record for record in records if record["event"] == "effect_requested"]
        self.assertEqual(len(effects), 1)
        self.assertEqual((effects[0]["effect"], effects[0]["target"]), ("switch", "local:one"))

    def test_failed_mouse_release_recovers_without_debug_logging(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(sidebar, "_mouse_diagnostics") as mouse_diagnostics,
        ):
            switch = self._run_mouse_trace(
                [curses.error(), (0, 7, 2, 0, curses.BUTTON1_RELEASED)],
                keys=[curses.KEY_MOUSE, curses.KEY_MOUSE, -1, STOP],
                collect_records=False,
            )

        switch.assert_called_once()
        mouse_diagnostics.assert_not_called()

    def test_failed_mouse_release_on_move_handle_is_not_recovered(self):
        records = self._run_mouse_trace(
            [curses.error(), (0, 29, 2, 0, curses.BUTTON1_RELEASED)],
            keys=[curses.KEY_MOUSE, curses.KEY_MOUSE, -1, STOP],
        )

        self.assertTrue(any(record["event"] == "mouse_recovery_candidate" for record in records))
        self.assertFalse(any(record["event"] == "effect_requested" for record in records))

    def test_failed_mouse_release_after_intervening_key_is_not_recovered(self):
        records = self._run_mouse_trace(
            [curses.error(), (0, 7, 2, 0, curses.BUTTON1_RELEASED)],
            keys=[curses.KEY_MOUSE, ord("j"), curses.KEY_MOUSE, -1, STOP],
        )

        self.assertFalse(any(record["event"] == "mouse_recovery_candidate" for record in records))
        self.assertFalse(any(record["event"] == "effect_requested" for record in records))

    def test_failed_mouse_release_after_name_confirmation_is_not_recovered(self):
        records = self._run_mouse_trace(
            [curses.error(), (0, 7, 2, 0, curses.BUTTON1_RELEASED)],
            keys=[ord("e"), curses.KEY_MOUSE, 10, curses.KEY_MOUSE, STOP],
        )

        self.assertFalse(any(record["event"] == "mouse_recovery_candidate" for record in records))
        self.assertFalse(any(record["event"] == "effect_requested" for record in records))

    def test_failed_mouse_release_after_recovery_window_is_not_recovered(self):
        with patch.object(sidebar, "MOUSE_RECOVERY_WINDOW", 0):
            records = self._run_mouse_trace(
                [curses.error(), (0, 7, 2, 0, curses.BUTTON1_RELEASED)],
                keys=[curses.KEY_MOUSE, curses.KEY_MOUSE, -1, STOP],
            )

        self.assertFalse(any(record["event"] == "mouse_recovery_candidate" for record in records))
        self.assertFalse(any(record["event"] == "effect_requested" for record in records))

    def test_first_click_after_idle_recovers_only_with_tmux_click_signal(self):
        target = Target("local", "one")
        pane = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        for region, row in (("sessions", 2), ("agents", 14)):
            with self.subTest(region=region):
                poller = unittest.mock.Mock(
                    snapshot=SessionSnapshot(SourceSnapshot(
                        True, (target,), frozenset(),
                        agents=(AgentEntry(pane, "id", "pi", "idle"),),
                    ), {}),
                    current_target=None, bell_target=None, current_agent=None,
                    pending_agent=None, pane_active=False,
                )
                def enter_sidebar(_now):
                    poller.pane_active = True
                    return False
                poller.tick.side_effect = enter_sidebar
                # No preceding press. Focus entry alone must not turn hover into a click.
                events = [
                    (0, 7, row, 0, curses.REPORT_MOUSE_POSITION),
                    (0, 7, row, 0, curses.REPORT_MOUSE_POSITION),
                    (0, 7, row, 0, curses.BUTTON1_RELEASED),
                    (0, 7, row, 0, curses.BUTTON1_PRESSED),
                    (0, 7, row, 0, curses.BUTTON1_RELEASED),
                ]
                record_start = len(self.records()) if self.log_path.exists() else 0
                with patch.object(sidebar, "_draw", return_value=(2, None)):
                    records = self._run_mouse_trace(
                        events, poller=poller, size=(20, 30),
                        keys=[-1, curses.KEY_MOUSE, curses.KEY_F2, curses.KEY_MOUSE,
                              curses.KEY_MOUSE, -1, curses.KEY_F2, curses.KEY_MOUSE,
                              curses.KEY_MOUSE, -1, STOP],
                    )
                effects = [r for r in records[record_start:] if r["event"] == "effect_requested"]
                self.assertEqual(len(effects), 2)
                self.assertTrue(all(r["target"] == target.format() for r in effects))
                self.assertTrue(all(
                    r["effect"] == ("switch_pane" if region == "agents" else "switch")
                    for r in effects
                ))

    def test_tmux_click_signal_recovers_pure_release(self):
        target = Target("local", "one")
        pane = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        for region, row in (("sessions", 2), ("agents", 14)):
            for debug_enabled in (True, False):
                with self.subTest(region=region, debug=debug_enabled):
                    poller = unittest.mock.Mock(
                        snapshot=SessionSnapshot(SourceSnapshot(
                            True, (target,), frozenset(),
                            agents=(AgentEntry(pane, "id", "pi", "idle"),),
                        ), {}),
                        current_target=None, bell_target=None, current_agent=None,
                        pending_agent=None, pane_active=True,
                    )
                    with (
                        patch.dict(os.environ, dict(os.environ) if debug_enabled else {}, clear=True),
                        patch.object(sidebar, "_draw", return_value=(2, None)),
                    ):
                        switch = self._run_mouse_trace(
                            [(0, 7, row, 0, curses.BUTTON1_RELEASED)],
                            keys=[curses.KEY_F2, curses.KEY_MOUSE, -1, STOP],
                            poller=poller, size=(20, 30), collect_records=False,
                        )
                    _assert_deferred_switch(self, switch, target, *(["id"] if region == "agents" else []))

    def test_tmux_pure_release_requires_unconsumed_signal_and_safe_row(self):
        release = (0, 7, 2, 0, curses.BUTTON1_RELEASED)
        for keys, events, window in (
            ([curses.KEY_MOUSE], [release], 1),
            ([curses.KEY_F2, curses.KEY_MOUSE], [release], 0),
            ([curses.KEY_F2, ord("j"), curses.KEY_MOUSE], [release], 1),
            ([curses.KEY_F2, curses.KEY_MOUSE, curses.KEY_MOUSE],
             [(0, 7, 1, 0, curses.REPORT_MOUSE_POSITION), release], 1),
            ([curses.KEY_F2, curses.KEY_MOUSE, curses.KEY_MOUSE],
             [(0, 29, 2, 0, curses.BUTTON1_RELEASED), release], 1),
            ([curses.KEY_F2, curses.KEY_MOUSE, curses.KEY_MOUSE],
             [(0, 7, 1, 0, curses.BUTTON1_RELEASED), release], 1),
        ):
            with self.subTest(keys=keys, events=events, window=window), patch.object(
                sidebar, "MOUSE_RECOVERY_WINDOW", window
            ):
                switch = self._run_mouse_trace(
                    events, keys=[*keys, -1, STOP], collect_records=False,
                )
                switch.assert_not_called()

    def test_recent_press_does_not_authorize_motion_navigation(self):
        records = self._run_mouse_trace([
            (0, 0, 1, 0, curses.BUTTON1_PRESSED),
            (0, 0, 2, 0, curses.REPORT_MOUSE_POSITION),
        ])
        self.assertFalse(any(record["event"] == "effect_requested" for record in records))

    def test_tmux_click_signal_recovers_without_debug_logging(self):
        with patch.dict(os.environ, {}, clear=True):
            switch = self._run_mouse_trace([
                (0, 7, 2, 0, curses.REPORT_MOUSE_POSITION),
                (0, 7, 2, 0, curses.BUTTON1_RELEASED),
            ], keys=[curses.KEY_F2, curses.KEY_MOUSE, curses.KEY_MOUSE, -1, STOP],
                collect_records=False)
        switch.assert_called_once()

    def test_tmux_click_signal_is_consumed_by_normal_press(self):
        switch = self._run_mouse_trace([
            (0, 7, 2, 0, curses.BUTTON1_PRESSED),
            (0, 7, 2, 0, curses.BUTTON1_RELEASED),
            (0, 7, 2, 0, curses.REPORT_MOUSE_POSITION),
        ], keys=[curses.KEY_F2, curses.KEY_MOUSE, curses.KEY_MOUSE, curses.KEY_MOUSE, -1, STOP],
            collect_records=False)
        switch.assert_called_once()

    def test_tmux_click_signal_does_not_recover_handles_or_empty_rows(self):
        for column, row in ((29, 2), (7, 1), (7, 4)):
            with self.subTest(column=column, row=row):
                switch = self._run_mouse_trace([
                    (0, column, row, 0, curses.REPORT_MOUSE_POSITION),
                    (0, 7, 2, 0, curses.REPORT_MOUSE_POSITION),
                ], keys=[curses.KEY_F2, curses.KEY_MOUSE, curses.KEY_MOUSE, -1, STOP],
                    collect_records=False)
                switch.assert_not_called()

    def test_tmux_click_signal_expires_or_is_cleared_by_keyboard_input(self):
        for intervening_keys, window in (([ord("j")], 1), ([], 0)):
            with self.subTest(keys=intervening_keys, window=window), patch.object(
                sidebar, "MOUSE_RECOVERY_WINDOW", window
            ):
                switch = self._run_mouse_trace([
                    (0, 7, 2, 0, curses.REPORT_MOUSE_POSITION),
                ], keys=[curses.KEY_F2, *intervening_keys, curses.KEY_MOUSE, -1, STOP],
                    collect_records=False)
                switch.assert_not_called()

    def test_unpaired_release_without_recent_click_is_ignored(self):
        records = self._run_mouse_trace([
            (0, 0, 2, 0, curses.REPORT_MOUSE_POSITION),
        ])

        decisions = [
            (record["decision"], record.get("reason"))
            for record in records
            if record["event"] == "mouse_decision"
        ]
        self.assertIn(("ignored_motion", "not_moving"), decisions)
        self.assertFalse(any(record["event"] == "effect_requested" for record in records))

    def test_focus_transition_is_recorded_without_mouse_input(self):
        target = Target("local", "one")
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=snapshot(local=("one",)),
            current_target=None,
            bell_target=None,
            current_agent=None,
            pane_active=True,
        )

        def tick(_now):
            poller.current_target = target
            poller.pane_active = False
            return False

        poller.tick.side_effect = tick
        records = self._run_mouse_trace([], keys=[-1, STOP], poller=poller)

        current = [record for record in records if record["event"] == "current_target_transition"]
        focus = [record for record in records if record["event"] == "pane_focus_transition"]
        self.assertEqual(len(current), 1)
        self.assertEqual((current[0]["previous_target"], current[0]["current_target"]), (None, target.format()))
        self.assertEqual(len(focus), 1)
        self.assertEqual((focus[0]["previous_active"], focus[0]["current_active"]), (True, False))

    def test_queued_navigation_actions_keep_distinct_ids(self):
        release = threading.Event()
        first = Effect("switch", Target("local", "one"))
        middle = Effect("switch", Target("local", "two"))
        latest = Effect("switch", Target("local", "three"))
        performed = []

        def perform(effect, favorites):
            performed.append(effect)
            if effect == first:
                release.wait(1)
            return sidebar.EffectResult(effect, favorites)

        runner = sidebar.EffectRunner()
        try:
            with patch.object(sidebar, "_perform_effect", side_effect=perform):
                self.assertTrue(runner.submit(first, (), input_id="input-one"))
                self.assertTrue(runner.submit(middle, (), input_id="input-two"))
                self.assertTrue(runner.submit(latest, (), input_id="input-three"))
                release.set()
                results = []
                deadline = time.monotonic() + 1
                while len(results) < 2 and time.monotonic() < deadline:
                    if result := runner.poll():
                        results.append(result)
                    time.sleep(0.001)
        finally:
            release.set()
            runner.close()

        submitted = [
            record for record in self.records()
            if record["event"] == "effect_submitted"
        ]
        action_ids = [record["action_id"] for record in submitted]
        self.assertEqual(len(action_ids), len(set(action_ids)))
        superseded = [
            record for record in self.records()
            if record["event"] == "effect_result" and record["status"] == "superseded"
        ]
        self.assertEqual(len(superseded), 2)
        self.assertTrue(all(record["action_id"] != record["superseded_by_action_id"] for record in superseded))
        self.assertEqual({record["input_id"] for record in superseded}, {"input-one", "input-two"})
        self.assertEqual([effect.target.session for effect in performed], ["one", "three"])

    def test_action_context_reaches_cockpit_switch(self):
        target = Target("local", "work")
        runner = sidebar.EffectRunner()
        try:
            with (
                patch.object(sidebar.cockpit, "right_pane", return_value="%2"),
                patch.object(sidebar.cockpit.tmux, "tmux"),
            ):
                self.assertTrue(runner.submit(Effect("switch", target), (), input_id="input-click"))
                deadline = time.monotonic() + 1
                result = None
                while result is None and time.monotonic() < deadline:
                    result = runner.poll()
                    time.sleep(0.001)
        finally:
            runner.close()

        submitted = next(record for record in self.records() if record["event"] == "effect_submitted")
        requested = next(record for record in self.records() if record["event"] == "switch_requested")
        self.assertEqual(requested["action_id"], submitted["action_id"])
        self.assertEqual(requested["input_id"], "input-click")
        self.assertEqual(requested["target"], target.format())

    def test_mouse_diagnostics_capture_curses_and_tmux_state(self):
        def query(*args, **kwargs):
            if args[:2] == ("show-options", "-qv"):
                return {
                    "mouse": "on",
                    "focus-follows-mouse": "off",
                    "focus-events": "on",
                }[args[4]]
            if args[:3] == ("list-keys", "-T", "root"):
                return {
                    "MouseDown1Pane": "bind-key MouseDown1Pane send-keys -t = F2 ; select-pane -t = ; send-keys -M",
                    "MouseDrag1Pane": "bind-key MouseDrag1Pane send-keys -M",
                    "MouseDragEnd1Pane": "bind-key MouseDragEnd1Pane send -M",
                    "MouseUp1Pane": "",
                }[args[3]]
            return "xterm-kitty"

        with (
            patch.object(sidebar.curses, "has_mouse", return_value=True, create=True),
            patch.object(sidebar.curses, "tigetstr", return_value=bytes((0x1B, 91, 77)), create=True),
            patch.object(sidebar.cockpit.tmux, "out", side_effect=query),
        ):
            state = sidebar._mouse_diagnostics((7, 3))

        self.assertTrue(state["curses_has_mouse"])
        self.assertEqual(state["terminfo_kmous"], "1b5b4d")
        self.assertEqual(state["requested_mouse_mask"], 7)
        self.assertEqual(state["supported_mouse_mask"], 3)
        self.assertEqual(state["tmux_mouse"], "on")
        self.assertEqual(state["tmux_focus_follows_mouse"], "off")
        self.assertTrue(state["tmux_mouse_down1_pane_binding"]["selects_pane"])
        self.assertTrue(state["tmux_mouse_down1_pane_binding"]["forwards_mouse"])
        self.assertTrue(state["tmux_mouse_down1_pane_binding"]["sends_f2_marker"])
        self.assertTrue(state["tmux_mouse_drag1_pane_binding"]["forwards_mouse"])
        self.assertTrue(state["tmux_mouse_drag_end1_pane_binding"]["forwards_mouse"])
        self.assertFalse(state["tmux_mouse_up1_pane_binding"]["present"])
        self.assertFalse(state["tmux_mouse_up1_pane_binding"]["sends_f2_marker"])
        self.assertEqual(state["tmux_client_termname"], "xterm-kitty")

    def test_mouse_diagnostics_do_not_mistake_f20_for_click_marker(self):
        with patch.object(sidebar, "_tmux_query", return_value="send-keys F20"):
            state = sidebar._mouse_diagnostics()
        self.assertFalse(state["tmux_mouse_down1_pane_binding"]["sends_f2_marker"])


class SidebarKeybindingTest(OperationTestCase):
    def _run_with_keys(self, keys, bindings=None):
        from letee.sidebar import run as sidebar_run
        target_a = Target("local", "a")
        target_b = Target("local", "b")
        data = snapshot(local=("a", "b"))
        poller = unittest.mock.Mock(launch_id=None,
            snapshot=data,
            current_target=target_a,
            bell_target=None,
            current_agent=None,
            pane_active=True,
        )
        poller.tick.return_value = False
        screen = FakeScreen(keys, size=(12, 40))
        bindings = bindings or config.DEFAULT_SIDEBAR_KEYBINDINGS
        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "DiscoveryPoller"),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[target_a, target_b]),
            patch.object(sidebar, "load_sidebar_keybindings", return_value=bindings),
            patch.object(sidebar.cockpit, "current_target", return_value=target_a),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", return_value=(1, None)) as draw,
            patch.object(sidebar, "_bell_targets", return_value=set()),
        ):
            sidebar_run(screen)
        return screen, poller, draw

    def test_custom_navigation_keys_are_used_and_arrows_still_work(self):
        custom = {**config.DEFAULT_SIDEBAR_KEYBINDINGS, "navigate_down": "n", "navigate_up": "p"}
        _, _, draw = self._run_with_keys([ord("n"), STOP], bindings=custom)
        selected = draw.call_args_list[-1]
        self.assertEqual(
            (selected.args[1][selected.args[2]].target, selected.args[2]),
            (Target("local", "b"), 1),
        )

        _, _, draw = self._run_with_keys([curses.KEY_DOWN, STOP], bindings=custom)
        selected = draw.call_args_list[-1]
        self.assertEqual(
            (selected.args[1][selected.args[2]].target, selected.args[2]),
            (Target("local", "b"), 1),
        )

    def test_sidebar_loads_bindings_once_at_start(self):
        custom = config.DEFAULT_SIDEBAR_KEYBINDINGS
        mock_load = unittest.mock.Mock(return_value=custom)
        target = Target("local", "work")
        data = snapshot(local=("work",))
        poller = unittest.mock.Mock(launch_id=None, snapshot=data, current_target=target, bell_target=None, current_agent=None, pane_active=True)
        poller.tick.return_value = False
        screen = FakeScreen([STOP], size=(8, 40))
        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "DiscoveryPoller"),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[target]),
            patch.object(sidebar, "load_sidebar_keybindings", mock_load),
            patch.object(sidebar.cockpit, "current_target", return_value=target),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", return_value=(1, None)),
            patch.object(sidebar, "_bell_targets", return_value=set()),
        ):
            sidebar.run(screen)
        mock_load.assert_called_once()

    def test_custom_rename_remove_kill_reorder_resize_keys(self):
        custom = {
            "navigate_down": "j",
            "navigate_up": "k",
            "rename": "R",
            "remove": "D",
            "kill": "X",
            "move_up": "U",
            "move_down": "N",
            "resize_inc": "{",
            "resize_dec": "}",
        }
        # rename with R should open rename editor (curs_set called with 1)
        target = Target("local", "work")
        data = snapshot(local=("work",))
        poller = unittest.mock.Mock(launch_id=None, snapshot=data, current_target=target, bell_target=None, current_agent=None, pane_active=True)
        poller.tick.return_value = False
        screen = FakeScreen([ord("R"), 27, STOP], size=(10, 40))
        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller),
            patch.object(sidebar, "DiscoveryPoller"),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[target]),
            patch.object(sidebar, "load_sidebar_keybindings", return_value=custom),
            patch.object(sidebar.cockpit, "current_target", return_value=target),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set") as curs_set,
            patch.object(sidebar, "_draw", return_value=(1, None)),
            patch.object(sidebar, "_draw_name", return_value=None),
            patch.object(sidebar, "_bell_targets", return_value=set()),
        ):
            sidebar.run(screen)
        self.assertIn(unittest.mock.call(1), curs_set.call_args_list)

        # D should remove without kill via custom binding
        target_a = Target("local", "a")
        target_b = Target("local", "b")
        data_two = snapshot(local=("a", "b"))
        poller_two = unittest.mock.Mock(snapshot=data_two, current_target=target_a, bell_target=None, current_agent=None, pane_active=True)
        poller_two.tick.return_value = False
        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller_two),
            patch.object(sidebar, "DiscoveryPoller"),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[target_a, target_b]),
            patch.object(sidebar, "load_sidebar_keybindings", return_value=custom),
            patch.object(sidebar.cockpit, "current_target", return_value=target_a),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", return_value=(1, None)),
            patch.object(sidebar, "_bell_targets", return_value=set()),
            patch.object(config, "save_sessions") as save,
        ):
            screen2 = FakeScreen([ord("D"), STOP], size=(10, 40))
            sidebar.run(screen2)
        save.assert_called_once_with((target_b,))

        # X should kill with confirmation via custom binding
        poller_three = unittest.mock.Mock(snapshot=data_two, current_target=target_a, bell_target=None, current_agent=None, pane_active=True)
        poller_three.tick.return_value = False
        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller_three),
            patch.object(sidebar, "DiscoveryPoller"),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[target_a, target_b]),
            patch.object(sidebar, "load_sidebar_keybindings", return_value=custom),
            patch.object(sidebar.cockpit, "current_target", return_value=target_a),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", return_value=(1, None)),
            patch.object(sidebar, "_bell_targets", return_value=set()),
            patch.object(sidebar.sessions, "kill") as kill,
            patch.object(sidebar.cockpit, "set_expected_right_pane_death") as expected_death,
            patch.object(sidebar.cockpit, "resolve_expected_right_pane_death") as resolve,
            patch.object(config, "save_sessions") as save_kill,
        ):
            screen3 = FakeScreen([ord("X"), ord("y"), STOP], size=(10, 40))
            sidebar.run(screen3)
        kill.assert_called_once_with(target_a)
        expected_death.assert_called_once_with(target_a)
        resolve.assert_called_once_with(target_a, True)
        save_kill.assert_called_once_with([target_b])

        # N should move selected session down via custom binding
        poller_four = unittest.mock.Mock(snapshot=data_two, current_target=target_a, bell_target=None, current_agent=None, pane_active=True)
        poller_four.tick.return_value = False
        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller_four),
            patch.object(sidebar, "DiscoveryPoller"),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[target_a, target_b]),
            patch.object(sidebar, "load_sidebar_keybindings", return_value=custom),
            patch.object(sidebar.cockpit, "current_target", return_value=target_a),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", return_value=(1, None)),
            patch.object(sidebar, "_bell_targets", return_value=set()),
            patch.object(config, "save_sessions") as save_move_down,
        ):
            screen4 = FakeScreen([ord("N"), STOP], size=(10, 40))
            sidebar.run(screen4)
        save_move_down.assert_called_once_with((target_b, target_a))

        # U should move selected session up via custom binding (navigate to second then move up)
        poller_five = unittest.mock.Mock(snapshot=data_two, current_target=target_a, bell_target=None, current_agent=None, pane_active=True)
        poller_five.tick.return_value = False
        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller_five),
            patch.object(sidebar, "DiscoveryPoller"),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[target_a, target_b]),
            patch.object(sidebar, "load_sidebar_keybindings", return_value=custom),
            patch.object(sidebar.cockpit, "current_target", return_value=target_a),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", return_value=(1, None)),
            patch.object(sidebar, "_bell_targets", return_value=set()),
            patch.object(config, "save_sessions") as save_move_up,
        ):
            screen5 = FakeScreen([ord("j"), ord("U"), STOP], size=(10, 40))
            sidebar.run(screen5)
        save_move_up.assert_called_once_with((target_b, target_a))

        # { / } should resize agent panel via custom bindings
        resize_step = 7
        percentages: list[int | None] = []

        def draw_spy(*args, **kwargs):
            bound = inspect.signature(_draw).bind(*args, **kwargs)
            bound.apply_defaults()
            percentages.append(bound.arguments.get("agent_percentage"))
            return (1, None)

        poller_six = unittest.mock.Mock(snapshot=snapshot(), current_target=None, bell_target=None, current_agent=None, pane_active=True)
        poller_six.tick.return_value = False
        percentages.clear()
        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller_six),
            patch.object(sidebar, "DiscoveryPoller"),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[]),
            patch.object(sidebar, "load_sidebar_keybindings", return_value=custom),
            patch.object(sidebar, "load_agent_panel_resize_step", return_value=resize_step),
            patch.object(sidebar.cockpit, "current_target", return_value=None),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", side_effect=draw_spy),
            patch.object(sidebar, "_bell_targets", return_value=set()),
            patch.object(sidebar, "_entries", return_value=[]),
        ):
            screen6 = FakeScreen([ord("{"), STOP], size=(24, 40))
            sidebar.run(screen6)
        self.assertGreater(len(percentages), 1)
        self.assertEqual(percentages[0], 40)
        self.assertEqual(percentages[1], 40 + resize_step)

        percentages.clear()
        poller_seven = unittest.mock.Mock(snapshot=snapshot(), current_target=None, bell_target=None, current_agent=None, pane_active=True)
        poller_seven.tick.return_value = False
        with (
            patch.object(sidebar, "AsyncStatusPoller", return_value=poller_seven),
            patch.object(sidebar, "DiscoveryPoller"),
            patch.object(sidebar, "load_hosts", return_value=[]),
            patch.object(config, "load_sessions", return_value=[]),
            patch.object(sidebar, "load_sidebar_keybindings", return_value=custom),
            patch.object(sidebar, "load_agent_panel_resize_step", return_value=resize_step),
            patch.object(sidebar.cockpit, "current_target", return_value=None),
            patch.object(sidebar, "_init_colors"),
            patch.object(sidebar, "_mouse_mask"),
            patch.object(sidebar.curses, "curs_set"),
            patch.object(sidebar, "_draw", side_effect=draw_spy),
            patch.object(sidebar, "_bell_targets", return_value=set()),
            patch.object(sidebar, "_entries", return_value=[]),
        ):
            screen7 = FakeScreen([ord("}"), STOP], size=(24, 40))
            sidebar.run(screen7)
        self.assertGreater(len(percentages), 1)
        self.assertEqual(percentages[0], 40)
        self.assertEqual(percentages[1], 40 - resize_step)


if __name__ == "__main__":
    unittest.main()
