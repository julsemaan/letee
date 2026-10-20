from contextlib import contextmanager, nullcontext
import json
import os
import tempfile
import select
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, call, patch

from letee.discovery import (
    DiscoveryPoller,
    REMOTE_COMMAND,
    SessionSnapshot,
    SourceSnapshot,
    _clean_env,
    _parse_source_snapshot,
    _persistent_master_alive,
    _read_agents,
    _source_result,
    _stop_process,
    _ssh_command,
    discover,
    local_snapshot,
    remote_snapshot,
)
from dataclasses import replace
from datetime import datetime, timezone

from letee.names import INNER_SERVER_SOCKET, PaneTarget, Target
from letee.config import Timing, load_timing


REAL_POPEN = subprocess.Popen
EMPTY_LOCAL = SourceSnapshot(True, (), frozenset())


_LEAKING_PROCESS = """
import os
import signal
import sys
import time

write_fd = int(sys.argv[1])
child_pid = os.fork()
if child_pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.write(write_fd, f"{os.getpid()}:{os.getpgrp()}\\n".encode())
    while True:
        time.sleep(1)
time.sleep(0.05)
os._exit(0)
"""


def _spawn_leaking_process(write_fd, kwargs):
    process = REAL_POPEN(
        [sys.executable, "-c", _LEAKING_PROCESS, str(write_fd)],
        stdout=kwargs["stdout"],
        stderr=kwargs["stderr"],
        start_new_session=kwargs.get("start_new_session", False),
        pass_fds=(write_fd,),
    )
    os.close(write_fd)
    return process


def _read_child_info(read_fd):
    pipe = os.fdopen(read_fd, "rb", buffering=0)
    line = pipe.readline().decode().strip()
    child_pid, process_group = (int(value) for value in line.split(":", 1))
    return pipe, child_pid, process_group


class DiscoveryInstrumentationTest(unittest.TestCase):
    def setUp(self):
        self.events = []
        ids = iter(f"discovery-{index}" for index in range(100))
        id_patch = patch("letee.discovery.diagnostics.get_diagnostics", return_value=Mock(new_id=lambda kind: next(ids), context_values=lambda: {}, context=lambda **fields: nullcontext()))
        id_patch.start()
        self.addCleanup(id_patch.stop)

        @contextmanager
        def operation(name, **fields):
            state = Mock(fields=fields, outcome="completed")
            try:
                yield state
            finally:
                self.events.append((name, dict(state.fields, outcome=state.outcome)))

        for name, replacement in (
            ("operation", operation),
            ("log", lambda event, **fields: self.events.append((event, fields))),
            ("error", lambda exc=None, **fields: self.events.append(("error", {"error_type": type(exc).__name__, **fields}))),
        ):
            patcher = patch(f"letee.discovery.diagnostics.{name}", replacement, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_local_agent_directory_error_returns_empty_and_logs_safe_fallback(self):
        from letee.discovery import _read_local_agents
        directory = Mock()
        directory.glob.side_effect = OSError("private directory")
        with patch("letee.discovery._status_dir", return_value=directory):
            self.assertEqual(_read_local_agents((), request_id="request-1"), ())
        error = next(fields for event, fields in self.events if event == "error")
        self.assertEqual(error["reason"], "directory_read")
        self.assertEqual(error["handling"], "fallback")
        self.assertEqual(error["request_id"], "request-1")
        self.assertNotIn("private", repr(self.events))

    def test_snapshot_spawn_failures_return_unavailable_and_safe_diagnostics(self):
        for remote in (False, True):
            self.events.clear()
            with (
                self.subTest(remote=remote),
                patch("letee.discovery._persistent_master_alive", return_value=False),
                patch("letee.discovery.subprocess.Popen", side_effect=OSError("private spawn")),
                patch("letee.discovery.subprocess.run", side_effect=OSError("private spawn")),
            ):
                snapshot = remote_snapshot("dev") if remote else local_snapshot()
            self.assertFalse(snapshot.available)
            operation = "discovery.remote_snapshot" if remote else "discovery.local_snapshot"
            fields = next(fields for event, fields in self.events if event == operation)
            self.assertEqual(fields["outcome"], "spawn_failed")
            error = next(fields for event, fields in self.events if event == "error")
            self.assertEqual(error["handling"], "fallback")
            self.assertEqual(error["request_id"], snapshot.request_id)
            self.assertNotIn("private", repr(self.events))

    def test_synchronous_timeouts_return_unavailable_with_correlated_safe_events(self):
        for remote in (False, True):
            self.events.clear()
            process = GroupTimeoutProcess(pid=123)
            failure = subprocess.TimeoutExpired(["PRIVATE command"], 5, output="PRIVATE stdout", stderr="PRIVATE stderr")
            with (
                self.subTest(remote=remote),
                patch("letee.discovery._persistent_master_alive", return_value=False),
                patch("letee.discovery.subprocess.run", side_effect=failure),
                patch("letee.discovery.subprocess.Popen", return_value=process),
                patch("letee.discovery.os.killpg") as killpg,
            ):
                snapshot = remote_snapshot("dev") if remote else local_snapshot()
            self.assertFalse(snapshot.available)
            self.assertEqual(snapshot.error, "timed out")
            self.assertIsNotNone(snapshot.request_id)
            operation = "discovery.remote_snapshot" if remote else "discovery.local_snapshot"
            fields = next(fields for event, fields in self.events if event == operation)
            self.assertEqual(fields["outcome"], "timed_out")
            self.assertEqual(fields["request_id"], snapshot.request_id)
            error = next(fields for event, fields in self.events if event == "error" and fields.get("operation") == operation)
            self.assertEqual(error["handling"], "fallback")
            self.assertEqual(error["reason"], "timeout")
            self.assertEqual(error["request_id"], snapshot.request_id)
            if remote:
                cleanup = next(fields for event, fields in self.events if event == "discovery.process_cleanup")
                self.assertEqual(cleanup["request_id"], snapshot.request_id)
                self.assertEqual(cleanup["reason"], "wait_incomplete")
                self.assertEqual(killpg.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])
                self.assertTrue(process.communicated)
            self.assertNotIn("PRIVATE", repr(self.events))

    def test_master_check_exceptions_log_safe_inconclusive_fallback(self):
        for failure in (
            subprocess.TimeoutExpired(["PRIVATE command"], 0.5, output="PRIVATE stdout", stderr="PRIVATE stderr"),
            OSError("PRIVATE check failure"),
            subprocess.SubprocessError("PRIVATE subprocess failure"),
        ):
            self.events.clear()
            with self.subTest(failure=type(failure).__name__), patch("letee.discovery.subprocess.run", side_effect=failure):
                self.assertIsNone(_persistent_master_alive("dev", True))
            fields = next(fields for event, fields in self.events if event == "discovery.master_check")
            self.assertEqual(fields["outcome"], "inconclusive")
            error = next(fields for event, fields in self.events if event == "error")
            self.assertEqual(error["handling"], "fallback")
            self.assertEqual(error["operation"], "discovery.master_check")
            self.assertEqual(error["error_type"], type(failure).__name__)
            self.assertEqual(error["host"], "dev")
            self.assertNotIn("PRIVATE", repr(self.events))

    def test_term_missing_group_is_ignored_and_cleanup_still_reaps(self):
        process = FakeProcess(0, stdout="PRIVATE output", pid=123)
        with patch("letee.discovery.os.killpg", side_effect=ProcessLookupError("PRIVATE group")) as killpg:
            self.assertEqual(_stop_process(process, request_id="request-1"), ("PRIVATE output", ""))
        self.assertEqual(killpg.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])
        self.assertTrue(process.communicated)
        fields = next(fields for event, fields in self.events if event == "discovery.process_cleanup")
        self.assertTrue(fields["term_group_missing"])
        self.assertFalse(fields["term_sent"])
        self.assertEqual(fields["request_id"], "request-1")
        self.assertEqual(fields["outcome"], "completed")
        error = next(fields for event, fields in self.events if event == "error" and fields.get("reason") == "term_group_missing")
        self.assertEqual(error["handling"], "ignore")
        self.assertEqual(error["operation"], "discovery.process_cleanup")
        self.assertEqual(error["request_id"], "request-1")
        self.assertEqual(error["error_type"], "ProcessLookupError")
        self.assertNotIn("PRIVATE", repr(self.events))

    def test_snapshot_wait_error_cleans_process_and_returns_safe_diagnostics(self):
        process = Mock(pid=123)
        process.wait.side_effect = OSError("private wait")
        with (
            patch("letee.discovery._persistent_master_alive", return_value=False),
            patch("letee.discovery.subprocess.Popen", return_value=process),
            patch("letee.discovery._stop_process", return_value=(None, None)) as stop,
        ):
            snapshot = remote_snapshot("dev")
        self.assertFalse(snapshot.available)
        stop.assert_called_once_with(process, request_id=snapshot.request_id, reason="wait_incomplete", keep_group=False)
        fields = next(fields for event, fields in self.events if event == "discovery.remote_snapshot")
        self.assertEqual(fields["outcome"], "wait_failed")
        self.assertTrue(any(event == "error" and fields.get("handling") == "fallback" for event, fields in self.events))
        self.assertNotIn("private", repr(self.events))

    def test_poll_error_is_reraised_with_correlated_terminal_diagnostic(self):
        for failure, outcome in ((OSError("private poll"), "failed"), (KeyboardInterrupt(), "cancelled")):
            self.events.clear()
            process = FakeProcess()
            poller = DiscoveryPoller(
                ["dev"], local=lambda: EMPTY_LOCAL, popen=lambda *a, **k: process,
                clock=lambda: 0, persistent_ssh=False,
            )
            poller.tick()
            request = poller._active["dev"]
            with patch.object(process, "poll", side_effect=failure):
                with self.assertRaises(type(failure)) as raised:
                    poller.tick()
            self.assertIs(raised.exception, failure)
            terminal = next(fields for event, fields in self.events if event == "discovery.request_finished")
            self.assertEqual(terminal["outcome"], outcome)
            self.assertEqual(terminal["request_id"], request.request_id)
            self.assertNotIn("private", repr(self.events))
            poller.close()
            self.assertTrue(request.output.closed)
            self.assertTrue(request.errors.closed)
            self.assertTrue(process.communicated)

    def test_parse_summary_counts_rejections_without_payloads(self):
        snapshot = _source_result(
            0, "work:@1:%1:0:-:/private/socket\nbad line\n\n__LETEE_AGENT_STATUS__\nnot json\n{}\n",
            "private stderr", kind="ssh", host="dev",
        )
        self.assertTrue(snapshot.available)
        fields = next(fields for event, fields in self.events if event == "discovery.source_result")
        self.assertEqual(fields["pane_count"], 1)
        self.assertEqual(fields["malformed_agent_count"], 1)
        self.assertNotIn("private", repr(self.events))
        self.assertNotIn("not json", repr(self.events))

    def test_remote_reader_emits_bounded_failure_summary(self):
        from letee.discovery import _REMOTE_READER
        with tempfile.TemporaryDirectory() as directory:
            from pathlib import Path
            Path(directory, "valid.json").write_text('{"hello":"private value"}')
            Path(directory, "broken.json").write_text("private invalid json")
            result = subprocess.run(
                [sys.executable, "-c", _REMOTE_READER], capture_output=True, text=True,
                env=dict(os.environ, AGENT_STATUS_DIR=directory), check=True,
            )
        records = [json.loads(line) for line in result.stdout.splitlines()]
        summary = records[-1]["_letee_discovery"]
        self.assertEqual(summary, {"file_count": 2, "loaded_count": 1, "failed_count": 1})
        self.assertNotIn("private", json.dumps(summary))

    def test_snapshot_correlation_does_not_affect_equality(self):
        self.assertEqual(
            replace(EMPTY_LOCAL, request_id="request-a"),
            replace(EMPTY_LOCAL, request_id="request-b"),
        )

    def test_timeout_and_schedule_share_request_id(self):
        now = [0.0]
        poller = DiscoveryPoller(
            ["dev"], local=lambda: EMPTY_LOCAL, popen=lambda *a, **k: FakeProcess(),
            clock=lambda: now[0], persistent_ssh=False,
        )
        poller.tick()
        now[0] = load_timing().remote_command_timeout
        poller.tick()
        terminal = next(fields for event, fields in self.events if event == "discovery.request_finished")
        scheduled = next(fields for event, fields in self.events if event == "discovery.scheduled")
        self.assertEqual(terminal["outcome"], "timed_out")
        self.assertEqual(terminal["request_id"], scheduled["request_id"])
        self.assertIsNotNone(terminal["request_id"])
        self.assertEqual(scheduled["interval"], load_timing().retry_initial_delay)

    def test_cleanup_records_escalation_and_expected_missing_group(self):
        process = GroupTimeoutProcess(pid=123)
        with patch("letee.discovery.os.killpg", side_effect=[None, ProcessLookupError()]):
            _stop_process(process, request_id="request-1", reason="timeout")
        fields = next(fields for event, fields in self.events if event == "discovery.process_cleanup")
        self.assertTrue(fields["term_sent"])
        self.assertTrue(fields["term_wait_timed_out"])
        self.assertTrue(fields["kill_group_missing"])
        self.assertFalse(fields["kill_sent"])
        self.assertEqual(fields["request_id"], "request-1")
        self.assertTrue(process.communicated)

    def test_remote_metadata_is_allowlisted_and_capped(self):
        payload = {"_letee_discovery": {"file_count": 10**20, "loaded_count": -1, "failed_count": "private", "private": "secret"}}
        snapshot = _source_result(0, "\n__LETEE_AGENT_STATUS__\n" + json.dumps(payload), "", kind="ssh", host="dev")
        self.assertTrue(snapshot.available)
        fields = next(fields for event, fields in self.events if event == "discovery.source_result")
        self.assertEqual(fields["remote_reader_counts"], {"file_count": 1024 * 1024})
        self.assertNotIn("private", repr(self.events))
        self.assertNotIn("secret", repr(self.events))

    def test_agent_rejection_reasons_are_aggregated_without_identifiers(self):
        _read_agents((), [None, {}, {"schema_version": "agent-status/v1alpha1"}])
        fields = next(fields for event, fields in self.events if event == "discovery.agents_parsed")
        self.assertEqual(fields["rejected_count"], 3)
        self.assertEqual(fields["reasons"], {"invalid_payload": 1, "unsupported_schema": 1, "invalid_runtime": 1})

    def test_spawn_failure_and_close_have_terminal_events(self):
        for process, outcome in ((OSError("private spawn failure"), "spawn_failed"), (FakeProcess(), "closed")):
            self.events.clear()
            popen = Mock(side_effect=process) if isinstance(process, OSError) else Mock(return_value=process)
            poller = DiscoveryPoller(["dev"], local=lambda: EMPTY_LOCAL, popen=popen, clock=lambda: 0, persistent_ssh=False)
            poller.tick()
            poller.close()
            fields = next(fields for event, fields in self.events if event == "discovery.request_finished")
            self.assertEqual(fields["outcome"], outcome)
            self.assertNotIn("private", repr(self.events))

    def test_repeated_identical_results_do_not_change_with_new_request_ids(self):
        poller = DiscoveryPoller(
            ["dev"], local=lambda: EMPTY_LOCAL, popen=lambda *a, **k: FakeProcess(0),
            clock=lambda: 0, persistent_ssh=False,
        )
        self.assertTrue(poller.tick())
        first_id = poller.remotes["dev"].request_id
        poller.refresh()
        self.assertFalse(poller.tick())
        self.assertNotEqual(first_id, poller.remotes["dev"].request_id)


class DiscoveryDiagnosticFaultTest(unittest.TestCase):
    def setUp(self):
        from pathlib import Path
        from letee.diagnostics import Diagnostics
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "events.jsonl"
        self.logger = Diagnostics(self.path)
        self.addCleanup(self.logger.close)
        patcher = patch("letee.diagnostics.get_diagnostics", return_value=self.logger)
        patcher.start()
        self.addCleanup(patcher.stop)

    def records(self):
        self.logger.flush()
        text = self.path.read_text()
        self.assertNotIn("PRIVATE", text)
        return [json.loads(line) for line in text.splitlines()]

    def test_local_read_error_has_safe_frames(self):
        from pathlib import Path
        from letee.discovery import _read_local_agents
        Path(self.directory.name, "status.json").write_text("{}")
        with patch("letee.discovery._status_dir", return_value=Path(self.directory.name)), patch.object(Path, "read_text", side_effect=PermissionError("PRIVATE file")):
            _read_local_agents((), request_id="read-1")
        error = next(record for record in self.records() if record["event"] == "error")
        self.assertEqual(error["handling"], "skip")
        self.assertEqual(error["error_type"], "PermissionError")
        self.assertTrue(error["causes"][0]["frames"])
        self.assertEqual(error["request_id"], "read-1")

    def test_missing_remote_reader_is_distinct_and_private(self):
        snapshot = _source_result(0, "\n__LETEE_AGENT_STATUS__\n", "PRIVATE python missing", kind="ssh", host="dev")
        self.assertTrue(snapshot.available)
        error = next(record for record in self.records() if record["event"] == "error")
        self.assertEqual(error["reason"], "remote_reader_missing")
        self.assertEqual(error["handling"], "fallback")
        _source_result(0, "\n__LETEE_AGENT_STATUS__\n", "", kind="ssh", host="dev")
        _source_result(0, '\n__LETEE_AGENT_STATUS__\n{"_letee_discovery": {}}', "PRIVATE stderr", kind="ssh", host="dev")
        self.assertEqual(len([r for r in self.records() if r["event"] == "error"]), 1)

    def test_idle_state_changes_and_heartbeat_are_not_per_tick(self):
        now = [0.0]
        process = FakeProcess()
        poller = DiscoveryPoller(["dev"], local=lambda: EMPTY_LOCAL, popen=lambda *a, **k: process, clock=lambda: now[0], persistent_ssh=False)
        poller.tick()
        for _ in range(10):
            poller.tick()
        states = [record for record in self.records() if record["event"] == "discovery.poll_state"]
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["state"], "inflight")
        now[0] = 30
        with patch.object(poller, "_timing", replace(load_timing(), remote_command_timeout=60)):
            poller.tick()
        self.assertEqual(len([r for r in self.records() if r["event"] == "discovery.poll_state"]), 2)
        process.returncode = 0
        poller.tick()
        poller.tick()
        states = [r["state"] for r in self.records() if r["event"] == "discovery.poll_state"]
        self.assertEqual(states, ["inflight", "inflight", "not_due"])
        poller.close()

    def test_precheck_failure_and_cancellation_finish_attempt_with_context(self):
        for failure, outcome in ((OSError("PRIVATE precheck"), "failed"), (KeyboardInterrupt("PRIVATE cancel"), "cancelled")):
            with self.subTest(outcome=outcome):
                poller = DiscoveryPoller(["dev"], local=lambda: EMPTY_LOCAL, clock=lambda: 0, persistent_ssh=False)
                with self.logger.context(action_id="action-parent"), patch("letee.discovery._persistent_master_alive", side_effect=failure):
                    with self.assertRaises(type(failure)):
                        poller.tick()
                terminal = [r for r in self.records() if r["event"] == "discovery.request_finished"][-1]
                self.assertEqual(terminal["outcome"], outcome)
                self.assertEqual(terminal["action_id"], "action-parent")
                errors = [r for r in self.records() if r["event"] == "error"]
                self.assertEqual(errors[-1]["request_id"], terminal["request_id"])
                self.assertTrue(errors[-1]["causes"][0]["frames"])

    def test_invalid_timestamps_are_counted_without_values(self):
        panes = _parse_source_snapshot("work:@1:%1:0:-:/PRIVATE/socket", kind="local").panes
        payload = {
            "schema_version": "agent-status/v1alpha1", "agent_id": "PRIVATE-id", "agent_name": "PRIVATE-name",
            "runtime": {"lifecycle": "running", "updated_at": "PRIVATE-timestamp"},
            "x_meta": {"tmux_socket": "/PRIVATE/socket", "tmux_pane": "%1"},
        }
        self.assertEqual(_read_agents(panes, [payload]), ())
        payload["runtime"]["updated_at"] = datetime.now(timezone.utc).isoformat()
        payload["task"] = {"state": "working", "status_timestamp": "PRIVATE-task-timestamp"}
        self.assertIsNone(_read_agents(panes, [payload])[0].task_status_timestamp)
        reasons = [r["reasons"] for r in self.records() if r["event"] == "discovery.agents_parsed"]
        self.assertEqual(reasons, [{"invalid_runtime_timestamp": 1}, {"invalid_task_timestamp": 1}])

    def test_tempfile_failure_finishes_attempt_and_closes_first_file(self):
        poller = DiscoveryPoller(["dev"], local=lambda: EMPTY_LOCAL, clock=lambda: 0, persistent_ssh=False)
        output = tempfile.TemporaryFile()
        with patch("letee.discovery.tempfile.TemporaryFile", side_effect=[output, OSError("PRIVATE tempfile")]):
            with self.assertRaises(OSError):
                poller.tick()
        self.assertTrue(output.closed)
        records = self.records()
        starting = next(r for r in records if r["event"] == "discovery.request_starting")
        terminal = next(r for r in records if r["event"] == "discovery.request_finished")
        self.assertEqual(terminal["request_id"], starting["request_id"])
        self.assertEqual(terminal["outcome"], "failed")

    def test_spawn_cancellation_closes_temporary_files(self):
        files = [tempfile.TemporaryFile(), tempfile.TemporaryFile()]
        poller = DiscoveryPoller(["dev"], local=lambda: EMPTY_LOCAL, popen=Mock(side_effect=KeyboardInterrupt("PRIVATE spawn")), clock=lambda: 0, persistent_ssh=False)
        with patch("letee.discovery.tempfile.TemporaryFile", side_effect=files):
            with self.assertRaises(KeyboardInterrupt):
                poller.tick()
        self.assertTrue(all(file.closed for file in files))
        records = self.records()
        terminal = next(r for r in records if r["event"] == "discovery.request_finished")
        self.assertEqual(terminal["outcome"], "cancelled")
        self.assertEqual(next(r for r in records if r["event"] == "error")["handling"], "cancel")

    def test_synchronous_tempfile_failure_is_correlated_before_spawn(self):
        with patch("letee.discovery._persistent_master_alive", return_value=False), patch("letee.discovery.tempfile.TemporaryFile", side_effect=OSError("PRIVATE tempfile")):
            with self.assertRaises(OSError):
                remote_snapshot("dev")
        records = self.records()
        terminal = next(r for r in records if r["event"] == "operation_end" and r["name"] == "discovery.remote_attempt")
        self.assertEqual(terminal["outcome"], "failed")
        error = next(r for r in records if r["event"] == "error")
        self.assertEqual(error["request_id"], terminal["request_id"])
        self.assertTrue(error["causes"][0]["frames"])

    def test_success_records_availability_counts_and_not_due_state(self):
        poller = DiscoveryPoller(["dev"], local=lambda: EMPTY_LOCAL, popen=lambda *a, **k: FakeProcess(0), clock=lambda: 0, persistent_ssh=False)
        poller.tick()
        records = self.records()
        terminal = next(r for r in records if r["event"] == "discovery.request_finished")
        self.assertIsNone(terminal["available_before"])
        self.assertTrue(terminal["available_after"])
        self.assertEqual(terminal["pane_count_before"], 0)
        self.assertEqual(terminal["pane_count_after"], 0)
        self.assertEqual(next(r for r in records if r["event"] == "discovery.poll_state")["state"], "not_due")
        initialized = next(r for r in records if r["event"] == "discovery.poller_initialized")
        self.assertEqual(initialized["remote_command_timeout"], load_timing().remote_command_timeout)

    def test_cleanup_failure_propagates_under_request_context(self):
        process = FakeProcess(0)
        poller = DiscoveryPoller(["dev"], local=lambda: EMPTY_LOCAL, popen=lambda *a, **k: process, clock=lambda: 0, persistent_ssh=False)
        with self.logger.context(action_id="action-worker"):
            poller._start_request("dev")
        process.communicate = Mock(side_effect=OSError("PRIVATE cleanup"))
        with self.assertRaises(OSError):
            poller.tick()
        records = self.records()
        terminal = next(r for r in records if r["event"] == "discovery.request_finished")
        self.assertEqual(terminal["outcome"], "failed")
        error = next(r for r in records if r["event"] == "error" and r.get("name") == "discovery.process_cleanup")
        self.assertEqual(error["handling"], "propagate")
        self.assertEqual(error["action_id"], "action-worker")
        self.assertEqual(error["request_id"], terminal["request_id"])
        process.communicate = Mock(return_value=("", ""))
        poller.close()


class DiscoverySnapshotTest(unittest.TestCase):
    def test_clean_env_removes_current_tmux_socket(self):
        with patch.dict("letee.discovery.os.environ", {"TMUX": "/tmp/tmux,1,0", "PATH": "x"}, clear=True):
            self.assertEqual(_clean_env(), {"PATH": "x"})

    def test_source_parser_deduplicates_targets_and_collects_bells(self):
        snapshot = _parse_source_snapshot(
            "work:@1:%1:0:-:/tmp/tmux:dev\nwork:@1:%2:1:-:/tmp/tmux:dev\nchat:@2:%3:!:-:/tmp/tmux:dev\nbad name:@3:%4:1:!:/tmp/tmux\n",
            kind="ssh",
            host="dev",
        )

        work = Target("ssh", "work", "dev")
        chat = Target("ssh", "chat", "dev")
        self.assertEqual(snapshot.sessions, (work, chat))
        self.assertEqual(snapshot.bells, frozenset({work, chat}))
        self.assertEqual([pane.pane_id for pane in snapshot.panes], ["%1", "%2", "%3"])
        self.assertEqual(snapshot.panes[0].socket_path, "/tmp/tmux:dev")

    def test_source_parser_collects_window_names_with_spaces_and_colons(self):
        snapshot = _parse_source_snapshot(
            "work:@1:%1:0:-:1:1:/tmp/tmux:dev\teditor pane:one\n"
            "work:@1:%2:0:-:0:1:/tmp/tmux\tlogs\n",
            kind="local",
        )

        self.assertEqual([pane.window_name for pane in snapshot.panes], ["editor pane:one", "logs"])
        self.assertEqual([pane.socket_path for pane in snapshot.panes], ["/tmp/tmux:dev", "/tmp/tmux"])

    def test_source_parser_collects_focused_pane(self):
        snapshot = _parse_source_snapshot("work:@1:%1:0:-:1:1:/tmp/tmux\nwork:@1:%2:0:-:0:1:/tmp/tmux\n", kind="local")

        self.assertEqual(snapshot.focused_panes, frozenset({snapshot.panes[0]}))
        self.assertEqual([pane.window_name for pane in snapshot.panes], ["", ""])

    def test_source_parser_keeps_sessions_named_letee(self):
        for kind, host in (("local", None), ("ssh", "dev")):
            with self.subTest(kind=kind):
                target = Target(kind, "letee", host)
                snapshot = _parse_source_snapshot("letee:@1:%1:1:!:/tmp/tmux\n", kind=kind, host=host)

                self.assertEqual(snapshot.sessions, (target,))
                self.assertEqual(snapshot.bells, frozenset({target}))

    def test_local_snapshot_derives_sessions_and_bells_from_one_sample(self):
        proc = Mock(returncode=0, stdout="work:@1:%1:1:!:1:1:/tmp/tmux\nidle:@2:%2:0:-:1:0:/tmp/tmux\n", stderr="")
        with patch("letee.discovery.subprocess.run", return_value=proc) as run:
            snapshot = local_snapshot()

        self.assertEqual(snapshot.sessions, (Target("local", "work"), Target("local", "idle")))
        self.assertEqual(snapshot.bells, frozenset({Target("local", "work")}))
        self.assertEqual([pane.pane_id for pane in snapshot.panes], ["%1", "%2"])
        self.assertEqual(snapshot.focused_panes, frozenset({snapshot.panes[0]}))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][:4], ["tmux", "-L", INNER_SERVER_SOCKET, "list-panes"])

    def test_configured_command_deadlines(self):
        timing = replace(load_timing(), local_command_timeout=0.25, remote_command_timeout=0.75, ssh_master_check_timeout=0.1)
        with (
            patch("letee.discovery.load_timing", return_value=timing),
            patch("letee.discovery.subprocess.run", return_value=Mock(returncode=0, stdout="", stderr="")) as run,
        ):
            local_snapshot()
            self.assertEqual(run.call_args.kwargs["timeout"], 0.25)
            _persistent_master_alive("dev", True)
            self.assertEqual(run.call_args.kwargs["timeout"], 0.1)
        process = FakeProcess(0)
        with (
            patch("letee.discovery.load_timing", return_value=timing),
            patch("letee.discovery.load_persistent_ssh", return_value=False),
            patch("letee.discovery.subprocess.Popen", return_value=process),
        ):
            remote_snapshot("dev")
        self.assertEqual(process.wait_timeouts, [0.75])

    def test_local_snapshot_timeout_is_reported_without_hanging(self):
        with patch(
            "letee.discovery.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["tmux"], 5),
        ):
            self.assertEqual(local_snapshot().error, "timed out")

    def test_local_no_server_is_available_empty_and_other_failure_is_explicit(self):
        no_server = Mock(returncode=1, stdout="", stderr="no server running on /tmp/tmux-1000/letee.inner\n")
        missing_socket = Mock(
            returncode=1,
            stdout="",
            stderr="error connecting to /tmp/tmux-1000/letee.inner (No such file or directory)\n",
        )
        failure = Mock(returncode=1, stdout="", stderr="tmux: permission denied\n")
        with patch("letee.discovery.subprocess.run", side_effect=[no_server, missing_socket, failure]):
            self.assertEqual(local_snapshot(), EMPTY_LOCAL)
            self.assertEqual(local_snapshot(), EMPTY_LOCAL)
            self.assertEqual(local_snapshot().error, "tmux: permission denied")

    def test_session_snapshot_aggregates_target_values(self):
        local = SourceSnapshot(True, (Target("local", "work"),), frozenset({Target("local", "work")}))
        remote = SourceSnapshot(True, (Target("ssh", "chat", "dev"),), frozenset({Target("ssh", "chat", "dev")}))
        snapshot = SessionSnapshot(local, {"dev": remote, "slow": None})

        self.assertEqual(snapshot.sessions, (Target("local", "work"), Target("ssh", "chat", "dev")))
        self.assertEqual(snapshot.bells, frozenset({Target("local", "work"), Target("ssh", "chat", "dev")}))

    def test_agent_reader_correlates_exact_socket_and_pane_and_normalizes_state(self):
        pane = PaneTarget(Target("local", "work"), "@1", "%1", "/tmp/a")
        other = PaneTarget(Target("local", "other"), "@2", "%1", "/tmp/b")
        now = datetime(2026, 6, 20, 16, 45, 30, tzinfo=timezone.utc)

        def record(agent_id, socket_path="/tmp/a", pane_id="%1", state="working", age="2026-06-20T16:45:00Z"):
            payload = {
                "schema_version": "agent-status/v1alpha1",
                "agent_id": agent_id,
                "agent_name": "pi",
                "runtime": {"lifecycle": "running", "updated_at": age},
                "x_meta": {"tmux_socket": socket_path, "tmux_pane": pane_id},
            }
            if state is not None:
                payload["task"] = {"state": state}
            return payload

        agents = _read_agents(
            (pane, other),
            [
                record("b", state="future-state"),
                record("a", state=None),
                record("dict", state={}),
                record("list", state=[]),
                record("wrong", socket_path="/tmp/missing"),
                record("stale", age="2026-06-20T16:44:00Z"),
            ],
            now,
        )

        self.assertEqual([agent.agent_id for agent in agents], ["a", "b", "dict", "list"])
        self.assertEqual([agent.task_state for agent in agents], [None, "unknown", "unknown", "unknown"])
        self.assertEqual(
            [agent.runtime_updated_at for agent in agents],
            [datetime(2026, 6, 20, 16, 45, tzinfo=timezone.utc)] * 4,
        )

    def test_agent_reader_prefers_valid_task_timestamp_and_ignores_malformed_optional_timestamp(self):
        pane = PaneTarget(Target("local", "work"), "@1", "%1", "/tmp/a")
        now = datetime(2026, 6, 20, 16, 45, 30, tzinfo=timezone.utc)

        def record(agent_id, timestamp):
            return {
                "schema_version": "agent-status/v1alpha1",
                "agent_id": agent_id,
                "agent_name": "pi",
                "runtime": {"lifecycle": "running", "updated_at": "2026-06-20T16:45:00Z"},
                "task": {"state": "working", "status_timestamp": timestamp},
                "x_meta": {"tmux_socket": "/tmp/a", "tmux_pane": "%1"},
            }

        agents = {
            agent.agent_id: agent
            for agent in _read_agents(
                (pane,),
                [record("valid", "2026-06-20T16:45:12Z"), record("malformed", "not-a-date")],
                now,
            )
        }

        self.assertEqual(agents["valid"].task_status_timestamp, datetime(2026, 6, 20, 16, 45, 12, tzinfo=timezone.utc))
        self.assertEqual(agents["malformed"].task_status_timestamp, None)
        self.assertEqual(agents["malformed"].activity_timestamp, agents["malformed"].runtime_updated_at)

    def test_ssh_command_uses_shared_keepalive_with_optional_persistence(self):
        timing = load_timing()
        keepalive = (
            "-o", f"ConnectTimeout={timing.ssh_connect_timeout}",
            "-o", f"ServerAliveInterval={timing.ssh_keepalive_interval}",
            "-o", f"ServerAliveCountMax={timing.ssh_keepalive_count_max}",
            "-o", "AddKeysToAgent=yes",
        )
        discovery = ("-o", "BatchMode=yes", "dev", REMOTE_COMMAND)
        persistence = (
            "-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/letee-%C",
            "-o", "ControlPersist=10m",
        )
        self.assertEqual(_ssh_command("dev", True), ("ssh", *keepalive, *persistence, *discovery))
        self.assertEqual(_ssh_command("dev", False), ("ssh", *keepalive, *discovery))
        self.assertIn(f"tmux -L {INNER_SERVER_SOCKET} list-panes", REMOTE_COMMAND)

    def test_remote_result_keeps_panes_when_agent_reader_is_missing(self):
        pane_line = "work:@1:%2:0:-:/tmp/tmux"
        snapshot = _source_result(
            0,
            pane_line + "\n__LETEE_AGENT_STATUS__\n",
            "sh: python3: not found\n",
            kind="ssh",
            host="dev",
        )

        self.assertTrue(snapshot.available)
        self.assertEqual(snapshot.sessions, (Target("ssh", "work", "dev"),))
        self.assertEqual(snapshot.agents, ())

    def test_remote_snapshot_resolves_persistence_for_command(self):
        proc = FakeProcess(0)
        with (
            patch("letee.discovery.load_persistent_ssh", return_value=False),
            patch("letee.discovery.subprocess.Popen", return_value=proc) as popen,
        ):
            remote_snapshot("dev")

        self.assertNotIn("ControlMaster=auto", popen.call_args.args[0])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_master_check_is_skipped_without_persistence(self):
        with patch("letee.discovery.subprocess.run") as run:
            self.assertFalse(_persistent_master_alive("dev", False))

        run.assert_not_called()

    def test_master_check_reports_running_master(self):
        with patch("letee.discovery.subprocess.run", return_value=Mock(returncode=0)) as run:
            self.assertTrue(_persistent_master_alive("dev", True))

        self.assertEqual(run.call_args.args[0][0], "ssh")
        self.assertEqual(run.call_args.args[0][-3:], ("-O", "check", "dev"))
        self.assertEqual(run.call_args.kwargs["timeout"], load_timing().ssh_master_check_timeout)
        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_master_check_failure_is_not_alive(self):
        with patch("letee.discovery.subprocess.run", return_value=Mock(returncode=255)):
            self.assertFalse(_persistent_master_alive("dev", True))

    def test_master_check_errors_are_inconclusive(self):
        for error in (
            subprocess.TimeoutExpired("ssh", load_timing().ssh_master_check_timeout),
            OSError("check unavailable"),
        ):
            with self.subTest(error=error), patch(
                "letee.discovery.subprocess.run", side_effect=error,
            ):
                self.assertIsNone(_persistent_master_alive("dev", True))

    def test_remote_snapshot_rejects_oversized_output_and_distinguishes_no_server(self):
        def oversized(command, **kwargs):
            kwargs["stdout"].write(b"x" * (1024 * 1024 + 1))
            kwargs["stderr"].write(b"diagnostic")
            return FakeProcess(0)

        with (
            patch("letee.discovery._persistent_master_alive", return_value=False),
            patch("letee.discovery.subprocess.Popen", side_effect=oversized),
        ):
            self.assertEqual(remote_snapshot("dev").error, "output exceeded 1 MiB")

        no_server = FakeProcess(1)
        no_server.stderr = "no server running on /tmp/tmux-1000/letee.inner\n"
        with (
            patch("letee.discovery._persistent_master_alive", return_value=False),
            patch("letee.discovery.subprocess.Popen", return_value=no_server),
        ):
            self.assertEqual(remote_snapshot("dev"), SourceSnapshot(True, (), frozenset()))

    def test_remote_snapshot_cleans_exited_parent_and_its_child_process_group(self):
        read_fd, write_fd = os.pipe()
        pipe = None
        child_pid = None
        leader_pid = None
        try:
            def popen(command, **kwargs):
                nonlocal leader_pid
                process = _spawn_leaking_process(write_fd, kwargs)
                leader_pid = process.pid
                return process

            with (
                patch("letee.discovery._persistent_master_alive", return_value=False),
                patch("letee.discovery.subprocess.Popen", side_effect=popen),
            ):
                self.assertTrue(remote_snapshot("dev").available)

            pipe, child_pid, process_group = _read_child_info(read_fd)
            self.assertEqual(process_group, leader_pid)
            readable, _, _ = select.select((pipe,), (), (), 2)
            self.assertEqual(readable, [pipe])
            self.assertEqual(pipe.read(), b"")
        finally:
            if pipe is not None:
                pipe.close()
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                os.close(read_fd)

    def test_remote_snapshot_cleans_completed_failure_process_group(self):
        process = FakeProcess(255, stderr="ssh failed\n", pid=106)
        with (
            patch("letee.discovery._persistent_master_alive", return_value=False),
            patch("letee.discovery.subprocess.Popen", return_value=process) as popen,
            patch("letee.discovery.os.killpg") as killpg,
        ):
            snapshot = remote_snapshot("dev")

        self.assertEqual(snapshot.error, "ssh failed")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(killpg.call_args_list, [call(106, signal.SIGTERM), call(106, signal.SIGKILL)])
        self.assertTrue(process.communicated)

    def test_remote_snapshot_preserves_group_for_master_created_by_request(self):
        process = FakeProcess(0, stdout="work:@1:%1:0:-:/tmp/tmux\n", pid=110)
        with (
            patch("letee.discovery.load_persistent_ssh", return_value=True),
            patch("letee.discovery._persistent_master_alive", side_effect=(False, True)) as alive,
            patch("letee.discovery.subprocess.Popen", return_value=process),
            patch("letee.discovery.os.killpg") as killpg,
        ):
            snapshot = remote_snapshot("dev")

        self.assertTrue(snapshot.available)
        self.assertEqual(alive.call_args_list, [call("dev", True), call("dev", True)])
        killpg.assert_not_called()
        self.assertTrue(process.communicated)

    def test_completed_request_preserves_group_when_final_master_check_is_inconclusive(self):
        for poll in (False, True):
            for initial in (Mock(returncode=255), OSError("check unavailable")):
                for error in (subprocess.TimeoutExpired("ssh", 0.5), OSError("check unavailable")):
                    with self.subTest(poll=poll, initial=initial, error=error):
                        process = FakeProcess(0, stdout="work:@1:%1:0:-:/tmp/tmux\n", pid=114)
                        with (
                            patch("letee.discovery.load_persistent_ssh", return_value=True),
                            patch("letee.discovery.subprocess.run", side_effect=[initial, error]),
                            patch("letee.discovery.subprocess.Popen", return_value=process),
                            patch("letee.discovery.os.killpg") as killpg,
                        ):
                            if poll:
                                poller = DiscoveryPoller(
                                    ["dev"], local=Mock(return_value=EMPTY_LOCAL),
                                    popen=Mock(return_value=process), clock=Mock(return_value=0),
                                    persistent_ssh=True,
                                )
                                poller.tick()
                                snapshot = poller.snapshot.remotes["dev"]
                                poller.close()
                            else:
                                snapshot = remote_snapshot("dev")

                        self.assertEqual(snapshot.sessions, (Target("ssh", "work", "dev"),))
                        killpg.assert_not_called()
                        self.assertTrue(process.communicated)

    def test_remote_snapshot_cleans_group_when_master_predates_request(self):
        process = FakeProcess(0, stdout="work:@1:%1:0:-:/tmp/tmux\n", pid=112)
        with (
            patch("letee.discovery.load_persistent_ssh", return_value=True),
            patch("letee.discovery._persistent_master_alive", return_value=True) as alive,
            patch("letee.discovery.subprocess.Popen", return_value=process),
            patch("letee.discovery.os.killpg") as killpg,
        ):
            snapshot = remote_snapshot("dev")

        self.assertTrue(snapshot.available)
        alive.assert_called_once_with("dev", True)
        self.assertEqual(killpg.call_args_list, [call(112, signal.SIGTERM), call(112, signal.SIGKILL)])
        self.assertTrue(process.communicated)

    def test_remote_snapshot_timeout_cleans_process_group(self):
        process = GroupTimeoutProcess(pid=107)
        with (
            patch("letee.discovery._persistent_master_alive", return_value=False),
            patch("letee.discovery.subprocess.Popen", return_value=process),
            patch("letee.discovery.os.killpg") as killpg,
        ):
            snapshot = remote_snapshot("dev")

        self.assertEqual(snapshot.error, "timed out")
        self.assertEqual(killpg.call_args_list, [call(107, signal.SIGTERM), call(107, signal.SIGKILL)])
        self.assertEqual(process.wait_timeouts, [load_timing().remote_command_timeout, 1, None])
        self.assertTrue(process.communicated)

    def test_remote_snapshot_interruption_cleans_process_group_before_reraising(self):
        process = Mock(pid=108)
        process.poll.return_value = None
        process.wait.side_effect = [KeyboardInterrupt(), subprocess.TimeoutExpired("ssh", 1), -9]
        process.communicate.return_value = (None, None)
        with (
            patch("letee.discovery._persistent_master_alive", return_value=False),
            patch("letee.discovery.subprocess.Popen", return_value=process),
            patch("letee.discovery.os.killpg") as killpg,
        ):
            with self.assertRaises(KeyboardInterrupt):
                remote_snapshot("dev")

        self.assertEqual(killpg.call_args_list, [call(108, signal.SIGTERM), call(108, signal.SIGKILL)])
        process.communicate.assert_called_once_with()

    def test_discover_returns_common_snapshot(self):
        local = SourceSnapshot(True, (Target("local", "work"),), frozenset())
        remote = SourceSnapshot(False, (), frozenset(), "offline")
        with (
            patch("letee.discovery.load_hosts", return_value=["dev"]),
            patch("letee.discovery.local_snapshot", return_value=local),
            patch("letee.discovery.remote_snapshot", return_value=remote),
        ):
            self.assertEqual(discover(), SessionSnapshot(local, {"dev": remote}))


class FakeProcess:
    def __init__(self, returncode=None, stdout="", stderr="", pid=None):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.pid = pid
        self.terminated = False
        self.communicated = False
        self.killed = False
        self.wait_timeouts = []

    def poll(self):
        return self.returncode

    def communicate(self):
        self.communicated = True
        return self.stdout, self.stderr

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class TerminateIgnoringProcess(FakeProcess):
    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired("ssh", timeout)
        return self.returncode


class GroupTimeoutProcess(FakeProcess):
    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if timeout is not None and self.returncode is None:
            raise subprocess.TimeoutExpired("ssh", timeout)
        self.returncode = self.returncode if self.returncode is not None else -9
        return self.returncode


class DiscoveryPollerTest(unittest.TestCase):
    def setUp(self):
        timing_patch = patch("letee.discovery.load_timing", return_value=Timing())
        timing_patch.start()
        self.addCleanup(timing_patch.stop)

    def test_removed_process_group_is_already_cleaned_up(self):
        process = FakeProcess(0, stdout="output", pid=109)
        with patch("letee.discovery.os.killpg", side_effect=ProcessLookupError):
            stdout, stderr = _stop_process(process)

        self.assertEqual((stdout, stderr), ("output", ""))
        self.assertTrue(process.communicated)

    def make_poller(self, hosts, **kwargs):
        kwargs.setdefault("persistent_ssh", False)
        return DiscoveryPoller(hosts, local=kwargs.pop("local", Mock(return_value=EMPTY_LOCAL)), **kwargs)

    def test_local_snapshot_is_sampled_at_startup_and_not_on_rapid_ticks(self):
        local = Mock(return_value=EMPTY_LOCAL)
        poller = self.make_poller([], local=local, clock=Mock(side_effect=[0, 0.1, 0.49]))

        self.assertFalse(poller.tick())
        self.assertFalse(poller.tick())

        local.assert_called_once_with()

    def test_local_bells_refresh_after_poll_interval_without_catch_up(self):
        bell = Target("local", "work")
        ringing = SourceSnapshot(True, (bell,), frozenset({bell}))
        local = Mock(side_effect=[EMPTY_LOCAL, ringing, EMPTY_LOCAL])
        poller = self.make_poller([], local=local, clock=Mock(side_effect=[0, 0.5, 2, 2.49]))

        self.assertTrue(poller.tick())
        self.assertTrue(poller.tick())
        self.assertFalse(poller.tick())

        self.assertEqual(local.call_count, 3)

    def test_refresh_samples_local_immediately_and_resets_deadline(self):
        bell = Target("local", "work")
        ringing = SourceSnapshot(True, (bell,), frozenset({bell}))
        local = Mock(side_effect=[EMPTY_LOCAL, ringing, EMPTY_LOCAL])
        poller = self.make_poller([], local=local, clock=Mock(side_effect=[0, 0.1, 0.59, 0.6]))

        self.assertTrue(poller.refresh())
        self.assertFalse(poller.tick())
        self.assertTrue(poller.tick())

        self.assertEqual(local.call_count, 3)

    def test_poller_resolves_persistence_once_and_uses_it_for_commands(self):
        popen = Mock(return_value=FakeProcess())
        with patch("letee.discovery.load_persistent_ssh", return_value=False) as load:
            poller = self.make_poller(
                ["dev"], popen=popen, clock=Mock(return_value=0), persistent_ssh=None
            )
            poller.tick()

        load.assert_called_once_with()
        self.assertNotIn("ControlMaster=auto", popen.call_args.args[0])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        poller.close()

    def test_pending_process_does_not_duplicate_or_block(self):
        process = FakeProcess()
        popen = Mock(return_value=process)
        poller = self.make_poller(["dev"], popen=popen, clock=Mock(return_value=0))

        self.assertFalse(poller.tick())
        poller.refresh()
        self.assertFalse(poller.tick())
        self.assertEqual(popen.call_count, 1)
        self.assertIsNone(poller.snapshot.remotes["dev"])
        poller.close()

    def test_active_remote_host_polls_every_half_second(self):
        now = [0.0]
        popen = Mock(side_effect=[FakeProcess(0), FakeProcess(0)])
        poller = self.make_poller(["dev"], popen=popen, clock=lambda: now[0])

        poller.tick("dev")
        now[0] = 0.49
        poller.tick("dev")
        now[0] = 0.5
        poller.tick("dev")

        self.assertEqual(popen.call_count, 2)
        self.assertEqual(poller._next["dev"], 1.0)

    def test_failed_remote_retries_with_capped_backoff(self):
        for active_host in (None, "dev"):
            with self.subTest(active_host=active_host):
                now = [0.0]
                popen = Mock(side_effect=lambda *args, **kwargs: FakeProcess(255))
                poller = self.make_poller(["dev"], popen=popen, clock=lambda: now[0])

                for count, delay in enumerate((1, 2, 4, 5, 5), start=1):
                    poller.tick(active_host)
                    self.assertEqual(popen.call_count, count)
                    self.assertEqual(poller._next["dev"], now[0] + delay)
                    now[0] += delay - 0.01
                    poller.tick(active_host)
                    self.assertEqual(popen.call_count, count)
                    now[0] += 0.01

    def test_backoff_is_independent_and_resets_after_success_and_refresh(self):
        now = [0.0]
        processes = {"dev": [255, 255, 0, 255, 255], "off": [255] * 5}
        def popen(command, **kwargs):
            return FakeProcess(processes[command[-2]].pop(0))
        poller = self.make_poller(["dev", "off"], popen=popen, clock=lambda: now[0])
        for timestamp in (0, 1, 3):
            now[0] = timestamp
            poller.tick()
        self.assertEqual(poller._next, {"dev": 13, "off": 7})
        now[0] = 13
        poller.tick()
        self.assertEqual(poller._next, {"dev": 14, "off": 18})
        poller.refresh()
        poller.tick()
        self.assertEqual(poller._next, {"dev": 14, "off": 14})

    def test_active_host_change_bypasses_failed_host_wait_only_once(self):
        now = [0.0]
        popen = Mock(side_effect=lambda *args, **kwargs: FakeProcess(255))
        poller = self.make_poller(["dev"], popen=popen, clock=lambda: now[0])
        poller.tick()
        now[0] = 0.1
        poller.tick("dev")
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(poller._next["dev"], 2.1)
        now[0] = 0.2
        poller.tick("dev")
        self.assertEqual(popen.call_count, 2)

    def test_request_deadline_starts_after_master_check_and_spawn(self):
        now = [0.0]
        process = FakeProcess()
        def check(*args):
            now[0] += 0.5
            return False
        def popen(*args, **kwargs):
            now[0] += 0.5
            return process
        poller = self.make_poller(["dev"], popen=popen, clock=lambda: now[0])
        with patch("letee.discovery._persistent_master_alive", side_effect=check):
            poller.tick()
        self.assertEqual(poller._active["dev"].started, 1)
        now[0] = 3.99
        poller.tick()
        self.assertFalse(process.terminated)
        now[0] = 4
        poller.tick()
        self.assertTrue(process.terminated)
        self.assertEqual(poller._next["dev"], 5)

    def test_poller_uses_configured_timeout_and_retry_delays(self):
        now = [0.0]
        process = FakeProcess()
        timing = replace(Timing(), remote_command_timeout=0.2, retry_initial_delay=0.3, retry_max_delay=0.5)
        with patch("letee.discovery.load_timing", return_value=timing) as load:
            poller = self.make_poller(["dev"], popen=Mock(return_value=process), clock=lambda: now[0])
            poller.tick()
            now[0] = 0.19
            poller.tick()
            self.assertFalse(process.terminated)
            now[0] = 0.2
            poller.tick()
            self.assertTrue(process.terminated)
            self.assertEqual(poller._next["dev"], 0.5)
            now[0] = 0.5
            poller.tick()
            self.assertEqual(poller._next["dev"], 1)
        load.assert_called_once_with()

    def test_refresh_resets_backoff_without_duplicating_pending_request(self):
        now = [0.0]
        pending = FakeProcess()
        popen = Mock(side_effect=[FakeProcess(255), pending])
        poller = self.make_poller(["dev"], popen=popen, clock=lambda: now[0])
        poller.tick()
        now[0] = 1
        poller.tick()
        poller.refresh()
        poller.tick()
        self.assertEqual(popen.call_count, 2)
        pending.returncode = 255
        poller.tick()
        self.assertEqual(poller._next["dev"], 2)

    def test_retry_wait_starts_after_cleanup_and_spawn_failure(self):
        now = [0.0]
        poller = self.make_poller(["dev"], popen=Mock(return_value=FakeProcess(255)), clock=lambda: now[0])
        def stop(*args, **kwargs):
            now[0] += 1
            return "", "offline"
        with patch("letee.discovery._stop_process", side_effect=stop):
            poller.tick()
        self.assertEqual(poller._next["dev"], 2)
        now[0] = 2
        def failed_spawn(*args, **kwargs):
            now[0] += 1
            raise OSError("no ssh")
        poller._popen = failed_spawn
        poller.tick()
        self.assertEqual(poller._next["dev"], 5)

    def test_inactive_remote_host_keeps_ten_second_interval(self):
        now = [0.0]
        popen = Mock(side_effect=[FakeProcess(0), FakeProcess(0)])
        poller = self.make_poller(["dev"], popen=popen, clock=lambda: now[0])

        poller.tick()
        now[0] = 9.99
        poller.tick()
        now[0] = 10
        poller.tick()

        self.assertEqual(popen.call_count, 2)
        self.assertEqual(poller._next["dev"], 20)

    def test_switching_active_host_refreshes_new_host_immediately(self):
        now = [0.0]
        popen = Mock(side_effect=[FakeProcess(0), FakeProcess(0), FakeProcess(0)])
        poller = self.make_poller(["dev", "prod"], popen=popen, clock=lambda: now[0])

        poller.tick("dev")
        now[0] = 0.1
        poller.tick("prod")

        self.assertEqual(popen.call_count, 3)
        self.assertEqual(poller._next["dev"], 10.1)
        self.assertEqual(poller._next["prod"], 0.6)

    def test_active_host_does_not_duplicate_in_flight_request(self):
        now = [0.0]
        process = FakeProcess()
        popen = Mock(return_value=process)
        poller = self.make_poller(["dev"], popen=popen, clock=lambda: now[0])

        poller.tick()
        now[0] = 1
        poller.tick("dev")
        now[0] = 2
        poller.tick("dev")

        self.assertEqual(popen.call_count, 1)
        poller.close()

    def test_completed_and_failed_processes_update_snapshots(self):
        healthy = FakeProcess(0, "work:@1:%1:1:!:/tmp/tmux\n", pid=101)
        failed = FakeProcess(255, pid=102)
        poller = self.make_poller(["dev", "off"], popen=Mock(side_effect=[healthy, failed]), clock=Mock(return_value=0))

        with patch("letee.discovery.os.killpg") as killpg:
            self.assertTrue(poller.tick())

        work = Target("ssh", "work", "dev")
        self.assertEqual(poller.snapshot.remotes["dev"].sessions, (work,))
        self.assertEqual(poller.snapshot.remotes["dev"].bells, frozenset({work}))
        self.assertEqual(poller.snapshot.remotes["off"].error, "remote command exited 255")
        self.assertTrue(healthy.communicated)
        self.assertEqual(
            killpg.call_args_list,
            [
                call(101, signal.SIGTERM),
                call(101, signal.SIGKILL),
                call(102, signal.SIGTERM),
                call(102, signal.SIGKILL),
            ],
        )

    def test_completed_poller_request_cleans_exited_parent_and_its_child_process_group(self):
        read_fd, write_fd = os.pipe()
        pipe = None
        child_pid = None
        leader_pid = None
        poller = None
        try:
            def popen(command, **kwargs):
                nonlocal leader_pid
                process = _spawn_leaking_process(write_fd, kwargs)
                leader_pid = process.pid
                return process

            poller = self.make_poller(["dev"], popen=popen, clock=Mock(return_value=0))
            poller.tick()
            pipe, child_pid, process_group = _read_child_info(read_fd)
            self.assertEqual(process_group, leader_pid)
            for _ in range(100):
                if poller.snapshot.remotes["dev"] is not None:
                    break
                time.sleep(0.01)
                poller.tick()
            self.assertIsNotNone(poller.snapshot.remotes["dev"])
            readable, _, _ = select.select((pipe,), (), (), 2)
            self.assertEqual(readable, [pipe])
            self.assertEqual(pipe.read(), b"")
        finally:
            if poller is not None:
                poller.close()
            if pipe is not None:
                pipe.close()
            else:
                os.close(read_fd)
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_completed_request_keeps_group_of_master_it_created(self):
        process = FakeProcess(0, "work:@1:%1:0:-:/tmp/tmux\n", pid=111)
        poller = self.make_poller(
            ["dev"],
            popen=Mock(return_value=process),
            clock=Mock(return_value=0),
            persistent_ssh=True,
        )

        with (
            patch("letee.discovery._persistent_master_alive", side_effect=(False, True)) as alive,
            patch("letee.discovery.os.killpg") as killpg,
        ):
            self.assertTrue(poller.tick())

        self.assertEqual(alive.call_args_list, [call("dev", True), call("dev", True)])
        killpg.assert_not_called()
        self.assertTrue(process.communicated)
        self.assertEqual(poller.snapshot.remotes["dev"].sessions, (Target("ssh", "work", "dev"),))

    def test_completed_request_cleans_group_when_master_predates_request(self):
        process = FakeProcess(0, "work:@1:%1:0:-:/tmp/tmux\n", pid=113)
        poller = self.make_poller(
            ["dev"],
            popen=Mock(return_value=process),
            clock=Mock(return_value=0),
            persistent_ssh=True,
        )

        with (
            patch("letee.discovery._persistent_master_alive", return_value=True) as alive,
            patch("letee.discovery.os.killpg") as killpg,
        ):
            self.assertTrue(poller.tick())

        alive.assert_called_once_with("dev", True)
        self.assertEqual(killpg.call_args_list, [call(113, signal.SIGTERM), call(113, signal.SIGKILL)])
        self.assertTrue(process.communicated)

    def test_spawn_failure_becomes_unavailable_snapshot(self):
        poller = self.make_poller(["dev"], popen=Mock(side_effect=OSError("no ssh")), clock=Mock(return_value=0))

        self.assertTrue(poller.tick())
        self.assertEqual(poller.snapshot.remotes["dev"].error, "no ssh")

    def test_refresh_updates_local_and_retries_remote_immediately(self):
        now = [0.0]

        def clock():
            return now[0]
        popen = Mock(side_effect=[FakeProcess(255), FakeProcess(0)])
        local = SourceSnapshot(True, (Target("local", "new"),), frozenset())
        poller = self.make_poller(
            ["dev"], popen=popen, clock=clock,
            local=Mock(side_effect=[EMPTY_LOCAL, local]),
        )

        poller.tick()
        now[0] = 0.1
        self.assertTrue(poller.refresh())
        poller.tick()

        self.assertEqual(poller.snapshot.local, local)
        self.assertEqual(popen.call_count, 2)

    def test_timeout_isolated_from_completed_host(self):
        now = [0.0]

        def clock():
            return now[0]
        slow = FakeProcess()
        healthy = FakeProcess(0, "work:@1:%1:0:-:/tmp/tmux\n")
        poller = self.make_poller(["slow", "dev"], popen=Mock(side_effect=[slow, healthy, FakeProcess()]), clock=clock)

        poller.tick()
        now[0] = 3
        self.assertTrue(poller.tick())
        self.assertTrue(slow.terminated)
        self.assertFalse(poller.snapshot.remotes["slow"].available)
        self.assertTrue(poller.snapshot.remotes["dev"].available)
        poller.close()

    def test_oversized_output_is_rejected_and_stderr_is_bounded(self):
        def popen(command, **kwargs):
            kwargs["stdout"].write(b"x" * (1024 * 1024 + 1))
            kwargs["stderr"].write(b"diagnostic")
            return FakeProcess(0)

        poller = self.make_poller(["dev"], popen=popen, clock=Mock(return_value=0))

        self.assertTrue(poller.tick())
        self.assertEqual(poller.snapshot.remotes["dev"].error, "output exceeded 1 MiB")

    def test_no_server_and_tmux_failure_remain_distinct(self):
        def popen(command, **kwargs):
            process = FakeProcess(1)
            kwargs["stderr"].write(
                b"no server running on /tmp/tmux-1000/letee.inner\n"
                if command[-2] == "empty"
                else b"tmux: permission denied\n"
            )
            return process

        poller = self.make_poller(["empty", "broken"], popen=popen, clock=Mock(return_value=0))

        self.assertTrue(poller.tick())
        self.assertEqual(poller.snapshot.remotes["empty"], SourceSnapshot(True, (), frozenset()))
        self.assertEqual(poller.snapshot.remotes["broken"].error, "tmux: permission denied")

    def test_success_after_failure_uses_normal_interval(self):
        now = [0.0]

        def clock():
            return now[0]
        popen = Mock(side_effect=[FakeProcess(255), FakeProcess(0)])
        poller = self.make_poller(["dev"], popen=popen, clock=clock)

        poller.tick()
        now[0] = 1
        poller.tick()

        self.assertEqual(poller._next["dev"], 11)

    def test_timeout_kills_and_reaps_process_that_ignores_terminate(self):
        process = TerminateIgnoringProcess()
        now = [0.0]
        poller = self.make_poller(["dev"], popen=Mock(return_value=process), clock=lambda: now[0])
        poller.tick()
        now[0] = 3

        self.assertTrue(poller.tick())
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(process.wait_timeouts, [1, None])

    def test_timeout_kills_remaining_process_group_and_reaps_leader(self):
        process = GroupTimeoutProcess(pid=103)
        now = [0.0]
        poller = self.make_poller(["dev"], popen=Mock(return_value=process), clock=lambda: now[0])

        with patch("letee.discovery.os.killpg") as killpg:
            poller.tick()
            now[0] = 3
            self.assertTrue(poller.tick())

        self.assertEqual(killpg.call_args_list, [call(103, signal.SIGTERM), call(103, signal.SIGKILL)])
        self.assertEqual(process.wait_timeouts, [1, None])
        self.assertTrue(process.communicated)

    def test_discard_removes_target_and_cancels_stale_request(self):
        completed = FakeProcess(0, "work:@1:%1:0:-:/tmp/tmux\n")
        stale = FakeProcess(pid=104)
        poller = self.make_poller(
            ["dev"], popen=Mock(side_effect=[completed, stale]),
            clock=Mock(return_value=0),
        )
        target = Target("ssh", "work", "dev")
        poller.tick()
        poller.refresh()
        poller.tick()

        with patch("letee.discovery.os.killpg") as killpg:
            poller.discard(target)

        self.assertNotIn(target, poller.snapshot.sessions)
        self.assertEqual(killpg.call_args_list, [call(104, signal.SIGTERM), call(104, signal.SIGKILL)])
        self.assertTrue(stale.communicated)

    def test_close_terminates_and_reaps_active_process(self):
        process = FakeProcess(pid=105)
        poller = self.make_poller(["dev"], popen=Mock(return_value=process), clock=Mock(return_value=0))
        poller.tick()

        with patch("letee.discovery.os.killpg") as killpg:
            poller.close()

        self.assertEqual(killpg.call_args_list, [call(105, signal.SIGTERM), call(105, signal.SIGKILL)])
        self.assertTrue(process.communicated)


class RemoteTrailerBudgetTest(unittest.TestCase):
    def test_diagnostic_trailers_do_not_reduce_original_payload_budget(self):
        import io
        from letee import discovery

        summary = json.dumps({"_letee_discovery": {"file_count": 0, "loaded_count": 0, "failed_count": 0}}).encode() + b"\n"
        exit_record = b'{"_letee_reader_exit": 0}\n'
        for trailer in (summary, exit_record, summary + exit_record):
            for size in (discovery.MAX_REMOTE_OUTPUT - 1, discovery.MAX_REMOTE_OUTPUT, discovery.MAX_REMOTE_OUTPUT + 1):
                with self.subTest(size=size, trailer=trailer):
                    payload = b"x" * (size - 1) + b"\n"
                    result = discovery._read_output(io.BytesIO(payload + trailer), stream="stdout")
                    if size > discovery.MAX_REMOTE_OUTPUT:
                        self.assertIsNone(result)
                    else:
                        self.assertEqual(result, (payload + trailer).decode())
        self.assertIsNone(discovery._read_output(io.BytesIO(b"x" * (discovery.MAX_REMOTE_OUTPUT + 1)), stream="stdout"))

    def test_remote_reader_status_distinguishes_missing_python_and_failure(self):
        for code, reason in ((127, "remote_python_missing"), (1, "remote_reader_failed")):
            with self.subTest(code=code), patch("letee.discovery.diagnostics.error") as error:
                snapshot = _source_result(0, '\n__LETEE_AGENT_STATUS__\n' + json.dumps({"_letee_reader_exit": code}), "SECRET raw output", kind="ssh", host="dev")
                self.assertTrue(snapshot.available)
                self.assertEqual(error.call_args.kwargs["reason"], reason)
                self.assertEqual(error.call_args.kwargs["returncode"], code)
                self.assertNotIn("SECRET", str(error.call_args))


if __name__ == "__main__":
    unittest.main()
