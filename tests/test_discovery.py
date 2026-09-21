import os
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
    _read_agents,
    _source_result,
    _stop_process,
    _ssh_command,
    discover,
    local_snapshot,
    remote_snapshot,
)
from datetime import datetime, timezone

from letee.names import INNER_SERVER_SOCKET, PaneTarget, Target


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
        keepalive = (
            "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=3",
            "-o", "AddKeysToAgent=yes",
        )
        discovery = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "dev", REMOTE_COMMAND)
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

    def test_remote_snapshot_rejects_oversized_output_and_distinguishes_no_server(self):
        def oversized(command, **kwargs):
            kwargs["stdout"].write(b"x" * (1024 * 1024 + 1))
            kwargs["stderr"].write(b"diagnostic")
            return FakeProcess(0)

        with patch("letee.discovery.subprocess.Popen", side_effect=oversized):
            self.assertEqual(remote_snapshot("dev").error, "output exceeded 1 MiB")

        no_server = FakeProcess(1)
        no_server.stderr = "no server running on /tmp/tmux-1000/letee.inner\n"
        with patch("letee.discovery.subprocess.Popen", return_value=no_server):
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

            with patch("letee.discovery.subprocess.Popen", side_effect=popen):
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
            patch("letee.discovery.subprocess.Popen", return_value=process) as popen,
            patch("letee.discovery.os.killpg") as killpg,
        ):
            snapshot = remote_snapshot("dev")

        self.assertEqual(snapshot.error, "ssh failed")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(killpg.call_args_list, [call(106, signal.SIGTERM), call(106, signal.SIGKILL)])
        self.assertTrue(process.communicated)

    def test_remote_snapshot_timeout_cleans_process_group(self):
        process = GroupTimeoutProcess(pid=107)
        with (
            patch("letee.discovery.subprocess.Popen", return_value=process),
            patch("letee.discovery.os.killpg") as killpg,
        ):
            snapshot = remote_snapshot("dev")

        self.assertEqual(snapshot.error, "timed out")
        self.assertEqual(killpg.call_args_list, [call(107, signal.SIGTERM), call(107, signal.SIGKILL)])
        self.assertEqual(process.wait_timeouts, [10, 1, None])
        self.assertTrue(process.communicated)

    def test_remote_snapshot_interruption_cleans_process_group_before_reraising(self):
        process = Mock(pid=108)
        process.poll.return_value = None
        process.wait.side_effect = [KeyboardInterrupt(), subprocess.TimeoutExpired("ssh", 1), -9]
        process.communicate.return_value = (None, None)
        with (
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
    def test_removed_process_group_is_already_cleaned_up(self):
        process = FakeProcess(0, stdout="output", pid=109)
        with patch("letee.discovery.os.killpg", side_effect=ProcessLookupError):
            stdout, stderr = _stop_process(process)

        self.assertEqual((stdout, stderr), ("output", ""))
        self.assertTrue(process.communicated)

    def make_poller(self, hosts, **kwargs):
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
            poller = self.make_poller(["dev"], popen=popen, clock=Mock(return_value=0))
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

    def test_failed_remote_retries_continuously_when_inactive(self):
        now = [0.0]
        popen = Mock(side_effect=[FakeProcess(255), FakeProcess(255), FakeProcess(255)])
        poller = self.make_poller(["dev"], popen=popen, clock=lambda: now[0])

        poller.tick()
        now[0] = 2
        poller.tick()
        now[0] = 4
        poller.tick()

        self.assertEqual(popen.call_count, 3)
        self.assertEqual(poller._next["dev"], 6)

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

    def test_spawn_failure_becomes_unavailable_snapshot(self):
        poller = self.make_poller(["dev"], popen=Mock(side_effect=OSError("no ssh")), clock=Mock(return_value=0))

        self.assertTrue(poller.tick())
        self.assertEqual(poller.snapshot.remotes["dev"].error, "no ssh")

    def test_refresh_updates_local_and_retries_remote_immediately(self):
        clock = Mock(side_effect=[0, 0, 1, 1])
        popen = Mock(side_effect=[FakeProcess(255), FakeProcess(0)])
        local = SourceSnapshot(True, (Target("local", "new"),), frozenset())
        poller = self.make_poller(
            ["dev"], popen=popen, clock=clock,
            local=Mock(side_effect=[EMPTY_LOCAL, local]),
        )

        poller.tick()
        self.assertTrue(poller.refresh())
        poller.tick()

        self.assertEqual(poller.snapshot.local, local)
        self.assertEqual(popen.call_count, 2)

    def test_timeout_isolated_from_completed_host(self):
        clock = Mock(side_effect=[0, 0, 11])
        slow = FakeProcess()
        healthy = FakeProcess(0, "work:@1:%1:0:-:/tmp/tmux\n")
        poller = self.make_poller(["slow", "dev"], popen=Mock(side_effect=[slow, healthy, FakeProcess()]), clock=clock)

        poller.tick()
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
        clock = Mock(side_effect=[0, 0, 2])
        popen = Mock(side_effect=[FakeProcess(255), FakeProcess(0)])
        poller = self.make_poller(["dev"], popen=popen, clock=clock)

        poller.tick()
        poller.tick()

        self.assertEqual(poller._next["dev"], 12)

    def test_timeout_kills_and_reaps_process_that_ignores_terminate(self):
        process = TerminateIgnoringProcess()
        poller = self.make_poller(["dev"], popen=Mock(return_value=process), clock=Mock(side_effect=[0, 0, 11]))
        poller.tick()

        self.assertTrue(poller.tick())
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(process.wait_timeouts, [1, None])

    def test_timeout_kills_remaining_process_group_and_reaps_leader(self):
        process = GroupTimeoutProcess(pid=103)
        poller = self.make_poller(["dev"], popen=Mock(return_value=process), clock=Mock(side_effect=[0, 0, 11]))

        with patch("letee.discovery.os.killpg") as killpg:
            poller.tick()
            self.assertTrue(poller.tick())

        self.assertEqual(killpg.call_args_list, [call(103, signal.SIGTERM), call(103, signal.SIGKILL)])
        self.assertEqual(process.wait_timeouts, [1, None])
        self.assertTrue(process.communicated)

    def test_discard_removes_target_and_cancels_stale_request(self):
        completed = FakeProcess(0, "work:@1:%1:0:-:/tmp/tmux\n")
        stale = FakeProcess(pid=104)
        poller = self.make_poller(
            ["dev"], popen=Mock(side_effect=[completed, stale]),
            clock=Mock(side_effect=[0, 0, 1, 1]),
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


if __name__ == "__main__":
    unittest.main()
