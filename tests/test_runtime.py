import importlib
import json
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from letee import runtime, tmux
from letee.discovery import SessionSnapshot, SourceSnapshot
from letee.names import Target
from letee.workspace import Session, Workspace, workspace_to_json


def workspace(name="work"):
    return Workspace((Session(Target("local", name), "$1", "/tmp/socket"),))


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("background operation did not finish")
        time.sleep(0.002)


class StatusPollerTests(unittest.TestCase):
    def test_missing_status_snapshot_keeps_previous_state(self):
        snapshot = SessionSnapshot(SourceSnapshot(True, (), frozenset()), {})
        discovery = Mock(snapshot=snapshot)
        target = Target("local", "work")
        poller = runtime.StatusPoller(discovery, target)
        self.addCleanup(poller.close)
        poller.current_agent = "agent"
        with patch.object(runtime.cockpit, "status_snapshot", return_value=None):
            result = poller._sample((), 0)
        self.assertIs(result.snapshot, snapshot)
        self.assertEqual(result.current_target, target)
        self.assertEqual(result.current_agent, "agent")
        self.assertFalse(result.sampled)
        discovery.tick.assert_not_called()

    def test_observe_uses_explicit_updates(self):
        discovery = Mock(snapshot=SessionSnapshot(SourceSnapshot(True, (), frozenset()), {}))
        target = Target("local", "work")
        renamed = Target("local", "renamed")
        poller = runtime.StatusPoller(discovery, target)
        self.addCleanup(poller.close)
        poller.observe(runtime.RuntimeUpdate("rename", target, renamed=renamed, stale=True))
        self.assertEqual(poller.current_target, target)
        poller.observe(runtime.RuntimeUpdate("rename", target, renamed=renamed))
        self.assertEqual(poller.current_target, renamed)
        self.assertTrue(poller.refresh_pending)
        poller.observe(runtime.RuntimeUpdate("save_favorites", renamed, removed=True))
        self.assertIsNone(poller.current_target)
        self.assertEqual(poller._generation, 2)


class WorkspaceTransportTests(unittest.TestCase):
    def test_blocked_publish_coalesces_latest_and_does_not_block_status(self):
        entered = threading.Event()
        release = threading.Event()
        writes = []

        def writer(payload):
            writes.append(payload)
            entered.set()
            release.wait(2)

        publisher = runtime.WorkspacePublisher(writer=writer)
        discovery = Mock(snapshot=SessionSnapshot(SourceSnapshot(True, (), frozenset()), {}))
        poller = runtime.StatusPoller(discovery, None)
        self.addCleanup(poller.close)
        self.addCleanup(release.set)
        self.addCleanup(publisher.close)
        publisher.publish(workspace("first"))
        self.assertTrue(entered.wait(1))
        start = time.monotonic()
        for index in range(100):
            publisher.publish(workspace(str(index)))
        self.assertLess(time.monotonic() - start, 0.1)
        status = runtime.cockpit.StatusSnapshot(None, None, None, True)
        with patch.object(runtime.cockpit, "status_snapshot", return_value=status):
            poller.tick(0)
            wait_for(lambda: poller._future.done())
            poller.tick(0.01)
            poller.tick(0.2)
            wait_for(lambda: poller._future.done())
            poller.tick(0.21)
        self.assertTrue(poller.status_sampled)
        self.assertEqual(discovery.tick.call_count, 2)
        self.assertEqual(len(writes), 1)
        release.set()
        wait_for(lambda: len(writes) == 2)
        self.assertEqual(writes[-1], workspace_to_json(workspace("99")))
        publisher.publish(workspace("99"))
        time.sleep(0.15)
        self.assertEqual(len(writes), 2)

    def test_publish_retries_failure_without_spinning(self):
        attempts = []

        def writer(payload):
            attempts.append(time.monotonic())
            if len(attempts) == 1:
                raise subprocess.CalledProcessError(1, "tmux")

        publisher = runtime.WorkspacePublisher(writer=writer)
        self.addCleanup(publisher.close)
        publisher.publish(workspace())
        wait_for(lambda: len(attempts) == 2)
        self.assertGreaterEqual(attempts[1] - attempts[0], 0.09)
        time.sleep(0.15)
        self.assertEqual(len(attempts), 2)

    def test_close_returns_immediately_with_blocked_io(self):
        for kind in ("reader", "writer"):
            with self.subTest(kind=kind):
                entered = threading.Event()
                release = threading.Event()

                def blocked(*args):
                    entered.set()
                    release.wait(2)
                    return ""

                if kind == "reader":
                    transport = runtime.WorkspaceReader(reader=blocked)
                else:
                    transport = runtime.WorkspacePublisher(writer=blocked)
                    transport.publish(workspace())
                try:
                    self.assertTrue(entered.wait(1))
                    self.assertTrue(transport._thread.daemon)
                    start = time.monotonic()
                    transport.close()
                    self.assertLess(time.monotonic() - start, 0.1)
                finally:
                    release.set()

    def test_read_is_nonblocking_and_keeps_latest_good_snapshot(self):
        entered = threading.Event()
        release = threading.Event()
        value = [workspace_to_json(workspace())]

        def reader():
            entered.set()
            release.wait(2)
            item = value[0]
            if isinstance(item, BaseException):
                raise item
            return item

        transport = runtime.WorkspaceReader(reader=reader)
        self.addCleanup(transport.close)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(1))
        start = time.monotonic()
        self.assertIsNone(transport.read())
        self.assertEqual(transport.current, Workspace(loading=True))
        self.assertLess(time.monotonic() - start, 0.1)
        release.set()
        wait_for(lambda: transport.current == workspace())
        self.assertEqual(transport.read(), workspace())
        self.assertIsNone(transport.read())
        # Whitespace changes do not create a semantic update.
        value[0] = json.dumps(json.loads(value[0]), indent=2)
        time.sleep(0.15)
        self.assertIsNone(transport.read())
        for invalid in ("", "{", '{"version":2}', SystemExit("missing option")):
            value[0] = invalid
            time.sleep(0.15)
            self.assertIsNone(transport.read())
            self.assertEqual(transport.current, workspace())
        value[0] = workspace_to_json(workspace("new"))
        wait_for(lambda: transport.current == workspace("new"))
        value[0] = workspace_to_json(workspace("latest"))
        wait_for(lambda: transport.current == workspace("latest"))
        self.assertEqual(transport.read(), workspace("latest"))
        self.assertIsNone(transport.read())

    def test_default_transport_uses_outer_named_tmux_option_only(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, workspace_to_json(workspace()), "")

        with patch.object(tmux, "SERVER", "named"), patch.object(tmux, "SOCKET", "letee-named"), patch.object(
            tmux, "tmux_executable", return_value="tmux"
        ), patch.object(tmux.subprocess, "run", side_effect=run):
            publisher = runtime.WorkspacePublisher()
            reader = runtime.WorkspaceReader()
            try:
                publisher.publish(workspace())
                wait_for(lambda: any("set-option" in call for call in calls))
                wait_for(lambda: reader.current == workspace())
            finally:
                publisher.close()
                reader.close()
                publisher._thread.join(1)
                reader._thread.join(1)
        self.assertEqual(runtime.WORKSPACE_OPTION, "@letee_workspace")
        for call in calls:
            self.assertEqual(call[:3], ["tmux", "-L", "letee-named"])
            self.assertIn(runtime.WORKSPACE_OPTION, call)
            self.assertIn(tmux.SESSION, call)
            self.assertIn(call[3], ("show-options", "set-option"))

    def test_runtime_does_not_import_sidebar(self):
        result = subprocess.run(
            [sys.executable, "-c", "import sys; import letee.runtime; assert 'letee.sidebar' not in sys.modules"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(importlib.import_module("letee.runtime"), runtime)


if __name__ == "__main__":
    unittest.main()
