from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
import tomllib
import re
import shlex
import signal
import sys
import socket as unix_socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import letee.sessions as sessions
from letee.config import Timing
from letee.names import INNER_SERVER_SOCKET, PaneTarget, Target, WindowTarget
from letee.sessions import attach_command, create, kill, kill_agent, pane_attach_command, rename, ssh_command


def setUpModule():
    timing_patch = patch("letee.sessions.load_timing", return_value=Timing())
    timing_patch.start()
    unittest.addModuleCleanup(timing_patch.stop)


class SessionOperationsTest(unittest.TestCase):
    def test_configured_operation_and_authentication_deadlines(self):
        timing = Timing(
            local_command_timeout=0.25, remote_command_timeout=0.75, ssh_auth_timeout=40,
            ssh_connect_timeout=4, ssh_keepalive_interval=7, ssh_keepalive_count_max=2,
        )
        with (
            patch("letee.sessions.load_timing", return_value=timing),
            patch("letee.sessions.load_tmux_config_overlay", return_value=False),
            patch("letee.sessions.subprocess.run", return_value=Mock(returncode=0)) as run,
        ):
            kill(Target("local", "work"))
            self.assertEqual(run.call_args.kwargs["timeout"], 0.25)
            kill(Target("ssh", "work", "dev"))
            self.assertEqual(run.call_args.kwargs["timeout"], 0.75)
            self.assertIn("ConnectTimeout=4", run.call_args.args[0])
            self.assertIn("ServerAliveInterval=7", run.call_args.args[0])
            self.assertIn("ServerAliveCountMax=2", run.call_args.args[0])
            sessions.prepare_host("dev")
            self.assertEqual(run.call_args.kwargs["timeout"], 40)
            sessions.probe_hosts(["dev"])
            self.assertEqual(run.call_args.kwargs["timeout"], 0.75)


    def test_local_utilities_and_overlay_use_configured_deadline(self):
        with (
            patch("letee.sessions.load_timing", return_value=Timing(local_command_timeout=0.25)),
            patch("letee.sessions.subprocess.run", return_value=Mock(returncode=0, stdout="")) as run,
        ):
            sessions._agent_reachable("/tmp/agent")
            self.assertEqual(run.call_args.kwargs["timeout"], 0.25)
            sessions._ssh_config("dev")
            self.assertEqual(run.call_args.kwargs["timeout"], 0.25)
            create(Target("local", "work"), overlay=True)
            self.assertEqual([call.kwargs["timeout"] for call in run.call_args_list[-2:]], [0.25, 0.25])

    def test_remote_kill_agent_budget_covers_helper_queries_and_ssh_overhead(self):
        pane = PaneTarget(Target("ssh", "work", "dev"), "@3", "%7", "/tmp/tmux")
        for timing in (Timing(), Timing(local_command_timeout=8, remote_command_timeout=1)):
            with (
                self.subTest(timing=timing),
                patch("letee.sessions.load_timing", return_value=timing),
                patch("letee.sessions.subprocess.run") as run,
            ):
                kill_agent(pane)
                self.assertEqual(
                    run.call_args.kwargs["timeout"],
                    timing.remote_command_timeout + 2 * timing.local_command_timeout,
                )
                self.assertEqual(shlex.split(run.call_args.args[0][-1])[-1], str(timing.local_command_timeout))
                kill(pane.target)
                self.assertEqual(run.call_args.kwargs["timeout"], timing.remote_command_timeout)

    def test_remote_helper_uses_supplied_local_deadline_for_both_subprocesses(self):
        with (
            patch.object(sys, "argv", ["-c", "--", "/tmp/tmux", "%7", "0.25"]),
            patch("subprocess.run", side_effect=[Mock(stdout="123\t/dev/null"), Mock(stdout="456")]) as run,
            patch("os.getpgid", return_value=123),
            patch("os.killpg") as killpg,
            redirect_stdout(io.StringIO()) as metadata,
        ):
            exec(sessions._KILL_AGENT_HELPER, {})
        self.assertEqual(metadata.getvalue(), "LETEE_DIAG kill_agent done success 2 1\n")
        self.assertEqual([call.kwargs["timeout"] for call in run.call_args_list], [0.25, 0.25])
        killpg.assert_called_once_with(456, signal.SIGTERM)

    def test_ssh_command_always_adds_keepalive_and_agent_option_and_optionally_adds_persistence(self):
        shared = (
            "-o", "ConnectTimeout=2", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
            "-o", "AddKeysToAgent=yes",
        )
        self.assertEqual(
            ssh_command("-t", "dev", "remote command", persistent_ssh=True),
            (
                "ssh", *shared,
                "-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/letee-%C",
                "-o", "ControlPersist=10m", "-t", "dev", "remote command",
            ),
        )
        self.assertEqual(
            ssh_command("-t", "dev", "remote command", persistent_ssh=False),
            ("ssh", *shared, "-t", "dev", "remote command"),
        )

    def test_ssh_command_interactive_reuses_persistent_control_socket(self):
        shared = (
            "-o", "ConnectTimeout=2", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
            "-o", "AddKeysToAgent=yes",
        )
        command = ssh_command("-t", "dev", "remote command", persistent_ssh=True, interactive=True)
        self.assertEqual(
            command,
            (
                "ssh", *shared,
                "-o", "ControlMaster=no", "-o", "ControlPath=~/.ssh/letee-%C",
                "-t", "dev", "remote command",
            ),
        )
        self.assertNotIn("ControlPersist=10m", command)
        self.assertNotIn("ControlPath=none", command)

    def test_ssh_command_interactive_without_persistence_leaves_multiplexing_to_ssh_config(self):
        shared = (
            "-o", "ConnectTimeout=2", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
            "-o", "AddKeysToAgent=yes",
        )
        self.assertEqual(
            ssh_command("-t", "dev", "remote command", persistent_ssh=False, interactive=True),
            ("ssh", *shared, "-t", "dev", "remote command"),
        )

    def test_ssh_command_non_interactive_keeps_persist_options(self):
        shared = (
            "-o", "ConnectTimeout=2", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
            "-o", "AddKeysToAgent=yes",
        )
        self.assertEqual(
            ssh_command("dev", "remote command", persistent_ssh=True),
            (
                "ssh", *shared,
                "-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/letee-%C",
                "-o", "ControlPersist=10m", "dev", "remote command",
            ),
        )

    def test_attach_commands_quote_local_and_remote_targets(self):
        with patch("letee.sessions.load_tmux_config_overlay", return_value=False):
            self.assertEqual(
                attach_command(Target("local", "work")),
                "env -u TMUX tmux -L letee.inner -T clipboard new-session -A -s work",
            )
            with patch("letee.sessions.load_persistent_ssh", return_value=True):
                self.assertEqual(
                    attach_command(Target("ssh", "work", "dev")),
                    "ssh -o ConnectTimeout=2 -o ServerAliveInterval=5 -o ServerAliveCountMax=3 -o AddKeysToAgent=yes -o ControlMaster=no -o 'ControlPath=~/.ssh/letee-%C' -t dev 'tmux -L letee.inner -T clipboard new-session -A -s work'",
                )

    def test_pane_attach_commands_select_exact_local_and_remote_pane(self):
        local = PaneTarget(Target("local", "work"), "@3", "%7", "/tmp/tmux socket")
        remote = PaneTarget(Target("ssh", "work", "dev"), "@3", "%7", "/tmp/tmux socket")
        with patch("letee.sessions.load_tmux_config_overlay", return_value=False):
            self.assertEqual(
                pane_attach_command(local),
                "env -u TMUX tmux -S '/tmp/tmux socket' select-window -t work:@3 \\; select-pane -t %7 \\; attach-session -t work",
            )
            with patch("letee.sessions.load_persistent_ssh", return_value=True):
                self.assertEqual(
                    pane_attach_command(remote),
                    "ssh -o ConnectTimeout=2 -o ServerAliveInterval=5 -o ServerAliveCountMax=3 -o AddKeysToAgent=yes -o ControlMaster=no -o 'ControlPath=~/.ssh/letee-%C' -t dev 'tmux -S '\"'\"'/tmp/tmux socket'\"'\"' select-window -t work:@3 \\; select-pane -t %7 \\; attach-session -t work'",
                )

    def test_kill_agent_local_signals_foreground_group_not_pane_shell(self):
        pane = PaneTarget(Target("local", "work"), "@3", "%7", "/tmp/tmux socket")
        with (
            patch.dict("letee.sessions.os.environ", {"TMUX": "/tmp/outer", "PATH": "x"}, clear=True),
            patch(
                "letee.sessions.subprocess.run",
                side_effect=[Mock(stdout="123\t/dev/pts/7\n"), Mock(stdout="456\n")],
            ) as run,
            patch("letee.sessions.os.open") as open_tty,
            patch("letee.sessions.os.tcgetpgrp") as tcgetpgrp,
            patch("letee.sessions.os.getpgid", return_value=123) as getpgid,
            patch("letee.sessions.os.killpg") as killpg,
        ):
            kill_agent(pane)

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ("tmux", "-S", "/tmp/tmux socket", "display-message", "-p", "-t", "%7", "#{pane_pid}\t#{pane_tty}"),
                ("ps", "-o", "tpgid=", "-p", "123"),
            ],
        )
        open_tty.assert_not_called()
        tcgetpgrp.assert_not_called()
        getpgid.assert_called_once_with(123)
        killpg.assert_called_once_with(456, signal.SIGTERM)

    def test_kill_agent_refuses_idle_pane_shell(self):
        pane = PaneTarget(Target("local", "work"), "@3", "%7", "/tmp/tmux")
        with (
            patch(
                "letee.sessions.subprocess.run",
                side_effect=[Mock(stdout="123\t/dev/pts/7\n"), Mock(stdout="123\n")],
            ),
            patch("letee.sessions.os.getpgid", return_value=123),
            patch("letee.sessions.os.killpg") as killpg,
        ):
            with self.assertRaisesRegex(SystemExit, "no foreground process"):
                kill_agent(pane)

        killpg.assert_not_called()

    def test_remote_kill_helper_handles_python_dash_c_separator_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            for name, body in {
                "tmux": "#!/bin/sh\nprintf '%s\\t/dev/null\\n' \"$PPID\"\n",
                "ps": "#!/bin/sh\nprintf '0\\n'\n",
            }.items():
                command = directory_path / name
                command.write_text(body)
                command.chmod(0o700)
            environment = os.environ.copy()
            environment["PATH"] = f"{directory}:{environment.get('PATH', '')}"

            result = subprocess.run(
                (
                    sys.executable,
                    "-c",
                    sessions._KILL_AGENT_HELPER,
                    "--",
                    "/tmp/tmux-test",
                    "%7",
                    "2",
                ),
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "no foreground process\n")
        self.assertNotIn("Traceback", result.stderr)

    def test_kill_agent_remote_uses_persistent_ssh_and_quotes_helper_arguments(self):
        pane = PaneTarget(Target("ssh", "work", "dev"), "@3", "%7", "/tmp/tmux socket; echo unsafe")
        with (
            patch("letee.sessions.load_persistent_ssh", return_value=True),
            patch("letee.sessions.subprocess.run") as run,
        ):
            kill_agent(pane)

        command = run.call_args.args[0]
        self.assertEqual(command[:1], ("ssh",))
        self.assertIn(("-o", "ControlMaster=auto"), list(zip(command, command[1:])))
        self.assertIn(("-o", "ControlPersist=10m"), list(zip(command, command[1:])))
        self.assertEqual(command[-2], "dev")
        self.assertIn("python3 -c", command[-1])
        self.assertEqual(command[-1].count(" -- "), 1)
        self.assertTrue(command[-1].endswith(" -- '/tmp/tmux socket; echo unsafe' %7 2"))
        self.assertIn("/tmp/tmux socket; echo unsafe", command[-1])
        self.assertNotIn("kill-pane", command[-1])
        self.assertEqual(run.call_args.kwargs["timeout"], 7)

    def test_kill_agent_tmux_lookup_failure_includes_operation_and_target(self):
        pane = PaneTarget(Target("local", "work"), "@3", "%7", "/tmp/tmux")
        error = subprocess.CalledProcessError(1, ["tmux"], stderr="pane disappeared\n")

        with patch("letee.sessions.subprocess.run", side_effect=error):
            with self.assertRaisesRegex(SystemExit, r"^kill agent local:work failed: pane disappeared$"):
                kill_agent(pane)

    def test_kill_agent_rejects_invalid_lookup_output(self):
        pane = PaneTarget(Target("local", "work"), "@3", "%7", "/tmp/tmux")

        with patch("letee.sessions.subprocess.run", return_value=Mock(stdout="not pane data")):
            with self.assertRaisesRegex(SystemExit, r"^kill agent local:work failed: invalid pane information$"):
                kill_agent(pane)

    def test_kill_agent_signal_failure_includes_operation_and_target(self):
        pane = PaneTarget(Target("local", "work"), "@3", "%7", "/tmp/tmux")
        with (
            patch(
                "letee.sessions.subprocess.run",
                side_effect=[Mock(stdout="123\t/dev/pts/7\n"), Mock(stdout="456\n")],
            ),
            patch("letee.sessions.os.getpgid", return_value=123),
            patch("letee.sessions.os.killpg", side_effect=PermissionError(1, "not permitted")),
        ):
            with self.assertRaisesRegex(SystemExit, r"^kill agent local:work failed: not permitted$"):
                kill_agent(pane)

    def test_kill_local_session_uses_inner_server(self):
        with (
            patch.dict("letee.sessions.os.environ", {"TMUX": "/tmp/letee,1,0", "PATH": "x"}, clear=True),
            patch("letee.sessions.subprocess.run") as run,
        ):
            kill(Target("local", "work"))

        run.assert_called_once_with(
            ("tmux", "-L", INNER_SERVER_SOCKET, "kill-session", "-t", "work"),
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
            env={"PATH": "x"},
        )

    def test_create_mutates_without_cockpit_dependency(self):
        target = Target("local", "work")
        with (
            patch.dict("letee.sessions.os.environ", {"TMUX": "/tmp/letee,1,0", "PATH": "x"}, clear=True),
            patch("letee.sessions.load_tmux_config_overlay", return_value=False),
            patch("letee.sessions.subprocess.run") as run,
        ):
            create(target)

        run.assert_called_once_with(
            ("tmux", "-L", INNER_SERVER_SOCKET, "new-session", "-d", "-s", "work"),
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
            env={"PATH": "x"},
        )

    def test_remote_create_and_kill_use_configured_persistence(self):
        target = Target("ssh", "work.one", "dev")
        with (
            patch("letee.sessions.load_persistent_ssh", side_effect=[True, False]),
            patch("letee.sessions.load_tmux_config_overlay", return_value=False),
            patch("letee.sessions.subprocess.run") as run,
        ):
            create(target)
            kill(target)

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                (
                    "ssh", "-o", "ConnectTimeout=2", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
                    "-o", "AddKeysToAgent=yes", "-o", "ControlMaster=auto",
                    "-o", "ControlPath=~/.ssh/letee-%C", "-o", "ControlPersist=10m",
                    "dev", "tmux -L letee.inner new-session -d -s work.one",
                ),
                (
                    "ssh", "-o", "ConnectTimeout=2", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
                    "-o", "AddKeysToAgent=yes", "dev", "tmux -L letee.inner kill-session -t work.one",
                ),
            ],
        )

    def test_rename_local_session_accepts_option_like_new_name(self):
        old = Target("local", "work")
        with (
            patch.dict("letee.sessions.os.environ", {"TMUX": "/tmp/letee,1,0", "PATH": "x"}, clear=True),
            patch("letee.sessions.subprocess.run") as run,
        ):
            self.assertEqual(rename(old, "-V"), Target("local", "-V"))

        run.assert_called_once_with(
            ("tmux", "-L", INNER_SERVER_SOCKET, "rename-session", "-t", "work", "--", "-V"),
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
            env={"PATH": "x"},
        )

    def test_rename_remote_session_uses_configured_persistence(self):
        old = Target("ssh", "work", "dev")
        with (
            patch("letee.sessions.load_persistent_ssh", return_value=True),
            patch("letee.sessions.subprocess.run") as run,
        ):
            self.assertEqual(rename(old, "renamed"), Target("ssh", "renamed", "dev"))

        self.assertEqual(
            run.call_args.args[0],
            (
                "ssh", "-o", "ConnectTimeout=2", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
                "-o", "AddKeysToAgent=yes", "-o", "ControlMaster=auto",
                "-o", "ControlPath=~/.ssh/letee-%C", "-o", "ControlPersist=10m",
                "dev", "tmux -L letee.inner rename-session -t work -- renamed",
            ),
        )

    def test_rename_failures_include_old_target(self):
        error = subprocess.CalledProcessError(1, ["tmux"], stderr="permission denied\n")
        with patch("letee.sessions.subprocess.run", side_effect=error):
            with self.assertRaisesRegex(SystemExit, r"^rename local:work failed: permission denied$"):
                rename(Target("local", "work"), "renamed")

    def test_rename_timeout_includes_old_target(self):
        with patch(
            "letee.sessions.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["tmux"], 10),
        ):
            with self.assertRaisesRegex(SystemExit, r"^rename ssh:dev:work timed out$"):
                rename(Target("ssh", "work", "dev"), "renamed")

    def test_rename_validates_new_name_before_running_command(self):
        with patch("letee.sessions.subprocess.run") as run:
            with self.assertRaisesRegex(SystemExit, "Invalid session"):
                rename(Target("local", "work"), "bad name")

        run.assert_not_called()

    def test_command_failures_include_operation_and_target(self):
        error = subprocess.CalledProcessError(1, ["command"], stderr="permission denied\n")
        for operation, action in (
            ("create", lambda: create(Target("local", "work"))),
            ("kill", lambda: kill(Target("ssh", "work", "dev"))),
        ):
            with (
                self.subTest(operation=operation),
                patch("letee.sessions.load_tmux_config_overlay", return_value=False),
                patch("letee.sessions.subprocess.run", side_effect=error),
            ):
                with self.assertRaisesRegex(SystemExit, rf"^{operation} .* failed: permission denied$"):
                    action()

    def test_command_timeout_includes_operation_and_target(self):
        with (
            patch("letee.sessions.load_tmux_config_overlay", return_value=False),
            patch(
                "letee.sessions.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["ssh"], 10),
            ),
        ):
            with self.assertRaisesRegex(SystemExit, r"^create ssh:dev:work timed out$"):
                create(Target("ssh", "work", "dev"))


class SessionDiagnosticsTest(unittest.TestCase):
    def test_agent_directory_failure_returns_without_starting_agent(self):
        failure = PermissionError("SECRET_DIRECTORY")
        with (
            patch.dict(sessions.os.environ, {}, clear=True),
            patch.object(Path, "mkdir", side_effect=failure),
            patch.object(sessions.subprocess, "run") as run,
            patch.object(sessions.diagnostics, "error") as error,
        ):
            self.assertIsNone(sessions.ensure_ssh_agent())
        run.assert_not_called()
        error.assert_called_once_with(failure, handling="fallback", stage="agent_directory")
        self.assertNotIn("SECRET", repr(error.call_args.kwargs))

    def test_unknown_inherited_agent_and_failed_fallback_leave_environment_unchanged(self):
        for fallback_status, reason in ((3, "probe_failed"), (2, "wait_failed")):
            with tempfile.TemporaryDirectory() as directory:
                with (
                    self.subTest(reason=reason),
                    patch.dict(sessions.os.environ, {"SSH_AUTH_SOCK": "/SECRET_INHERITED"}, clear=True),
                    patch.object(sessions, "FALLBACK_AGENT_SOCKET", Path(directory) / "agent.sock"),
                    patch.object(sessions.subprocess, "run", side_effect=[
                        Mock(returncode=3), Mock(returncode=fallback_status), Mock(returncode=0),
                    ]) as run,
                    patch.object(sessions, "_wait_for_agent", return_value=False) as wait,
                    patch.object(sessions.diagnostics, "log") as log,
                ):
                    self.assertIsNone(sessions.ensure_ssh_agent())
                    self.assertEqual(os.environ["SSH_AUTH_SOCK"], "/SECRET_INHERITED")
                self.assertEqual(run.call_count, 2 if fallback_status == 3 else 3)
                self.assertEqual(wait.call_count, 0 if fallback_status == 3 else 1)
                self.assertEqual(log.call_args.args, ("sessions.agent_setup.result",))
                self.assertEqual(log.call_args.kwargs, {"outcome": "unavailable", "reason": reason})
                self.assertNotIn("SECRET", repr(log.call_args_list))

    def test_overlay_read_failure_closes_diagnostic_operations_without_running_ssh(self):
        for failure in (OSError("SECRET_OVERLAY"), UnicodeError("SECRET_OVERLAY")):
            with tempfile.TemporaryDirectory() as directory:
                logger = sessions.diagnostics.Diagnostics(Path(directory) / "debug.jsonl")
                try:
                    with (
                        self.subTest(error=type(failure).__name__),
                        patch("letee.diagnostics.get_diagnostics", return_value=logger),
                        patch.object(sessions, "ssh_command", return_value=("ssh",)),
                        patch.object(Path, "read_text", side_effect=failure),
                        patch.object(sessions.subprocess, "run") as run,
                        self.assertRaises(type(failure)) as raised,
                    ):
                        attach_command(Target("ssh", "work", "dev"), overlay=True)
                    self.assertIs(raised.exception, failure)
                    run.assert_not_called()
                    logger.flush()
                    contents = logger.path.read_text()
                finally:
                    logger.close()
            self.assertNotIn("SECRET", contents)
            records = [json.loads(line) for line in contents.splitlines()]
            for name in ("sessions.attach_command", "sessions.overlay_install"):
                self.assertTrue(any(record.get("name") == name and record.get("outcome") == "failed"
                                    for record in records), name)

    def test_diagnostic_records_preserve_privacy_and_command_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = sessions.diagnostics.Diagnostics(Path(directory) / "debug.jsonl")
            try:
                with (
                    patch("letee.diagnostics.get_diagnostics", return_value=logger),
                    patch("letee.sessions.subprocess.run", side_effect=subprocess.CalledProcessError(
                        7, ["SECRET_COMMAND"], output="SECRET_OUTPUT", stderr="SECRET_STDERR",
                    )),
                    self.assertRaises(SystemExit),
                ):
                    kill(Target("local", "work"))
                logger.flush()
                contents = logger.path.read_text()
            finally:
                logger.close()
        records = [json.loads(line) for line in contents.splitlines()]
        self.assertGreaterEqual(contents.count("sessions.command"), 2)
        self.assertTrue(any(record.get("handling") == "raise" for record in records))
        for secret in ("SECRET_COMMAND", "SECRET_OUTPUT", "SECRET_STDERR"):
            self.assertNotIn(secret, contents)

    def test_parallel_authentication_retains_action_context(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = sessions.diagnostics.Diagnostics(Path(directory) / "debug.jsonl")
            try:
                with (
                    patch("letee.diagnostics.get_diagnostics", return_value=logger),
                    patch("letee.sessions.subprocess.run", return_value=Mock(returncode=0)),
                    logger.context(action_id="test-action"),
                ):
                    self.assertEqual(sessions.probe_hosts(["dev", "prod"]), [True, True])
                logger.flush()
                records = [json.loads(line) for line in logger.path.read_text().splitlines()]
            finally:
                logger.close()
        worker_records = [record for record in records if record["thread"].startswith("ThreadPoolExecutor")]
        self.assertTrue(worker_records)
        self.assertTrue(all(record.get("action_id") == "test-action" for record in worker_records))
        authentication = [record for record in worker_records if record.get("name") == "sessions.authentication"]
        self.assertEqual({record.get("host") for record in authentication}, {"dev", "prod"})
        settings = [record for record in worker_records if record["event"] == "sessions.ssh_settings"]
        self.assertEqual(len(settings), 2)
        self.assertEqual({record.get("host") for record in settings}, {"dev", "prod"})
        self.assertTrue(all(record["control_path"] == "disabled" for record in settings))

    def test_target_context_and_keyword_api_exclude_socket_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = sessions.diagnostics.Diagnostics(Path(directory) / "debug.jsonl")
            try:
                with (
                    patch("letee.diagnostics.get_diagnostics", return_value=logger),
                    patch("letee.sessions.subprocess.run", return_value=Mock(returncode=0, stdout="")),
                ):
                    target = Target("ssh", "work", "dev")
                    pane = PaneTarget(target, "@3", "%7", "/SECRET_SOCKET")
                    attach_command(target=target, overlay=False)
                    pane_attach_command(pane_target=pane, overlay=False)
                    create(target=target, overlay=False)
                    kill(target=target)
                    rename(target=target, new_name="renamed")
                    kill_agent(pane_target=pane)
                    sessions._ssh_config(host="dev")
                logger.flush()
                contents = logger.path.read_text()
            finally:
                logger.close()
        records = [json.loads(line) for line in contents.splitlines()]
        starts = [record for record in records if record["event"] == "operation_start"]
        for record in starts:
            self.assertEqual(record.get("host"), "dev")
            if record["name"] != "sessions.ssh_config":
                self.assertEqual(record.get("target"), "ssh:dev:work")
        pane_starts = [record for record in starts if record["name"] in (
            "sessions.pane_attach_command", "sessions.kill_agent")]
        self.assertTrue(all(record.get("pane_id") == "%7" for record in pane_starts))
        self.assertNotIn("SECRET_SOCKET", contents)

    def test_remote_metadata_is_allowlisted_bounded_and_read_from_both_streams(self):
        target = Target("ssh", "work", "dev")
        valid = "LETEE_DIAG kill_agent signal OSError 2 1"
        with patch("letee.sessions.diagnostics.log") as log:
            sessions._remote_metadata("kill agent", target, Mock(
                stdout="SECRET\n" + valid, stderr=valid + "\nSECRET", returncode=1))
        log.assert_called_once_with("sessions.kill_agent.remote", stage="signal", category="OSError",
                                    query_count=2, signal_count=1)
        for invalid in (
            "LETEE_DIAG kill_agent SECRET OSError 2 1",
            "LETEE_DIAG kill_agent signal SECRET 2 1",
            "LETEE_DIAG kill_agent signal OSError 999999 1",
            valid + "x" * 4096,
            valid + "\nSECRET" * 17,
        ):
            with self.subTest(invalid=invalid[:60]), patch("letee.sessions.diagnostics.log") as log:
                sessions._remote_metadata("kill agent", target, Mock(stdout=invalid, stderr="", returncode=1))
                log.assert_called_once_with("sessions.kill_agent.remote", category="metadata_unavailable")

    def test_remote_helper_failures_preserve_messages_and_emit_safe_stage_counts(self):
        cases = (
            ([subprocess.CalledProcessError(1, ["SECRET"], stderr="SECRET")], None, None,
             "pane_lookup", "CalledProcessError", 1, 0),
            ([Mock(stdout="SECRET")], None, None, "pane_lookup", "ValueError", 1, 0),
            ([Mock(stdout="123\t/dev/null"), subprocess.TimeoutExpired(["SECRET"], 2)], None, None,
             "foreground_lookup", "TimeoutExpired", 2, 0),
            ([Mock(stdout="123\t/dev/null"), Mock(stdout="456")], OSError("SECRET"), None,
             "pane_group", "OSError", 2, 0),
            ([Mock(stdout="123\t/dev/null"), Mock(stdout="123")], None, None,
             "foreground_validate", "RuntimeError", 2, 0),
            ([Mock(stdout="123\t/dev/null"), Mock(stdout="456")], None, OSError("SECRET"),
             "signal", "OSError", 2, 1),
        )
        for results, group_error, signal_error, stage, category, queries, signals in cases:
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                self.subTest(stage=stage, category=category),
                patch.object(sys, "argv", ["-c", "--", "/SECRET_SOCKET", "%7", "2"]),
                patch("subprocess.run", side_effect=results),
                patch("os.getpgid", return_value=123, side_effect=group_error),
                patch("os.killpg", side_effect=signal_error),
                redirect_stdout(stdout), redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                exec(sessions._KILL_AGENT_HELPER, {})
            self.assertEqual(raised.exception.code, 1)
            self.assertEqual(stdout.getvalue(), f"LETEE_DIAG kill_agent {stage} {category} {queries} {signals}\n")
            self.assertNotIn("Traceback", stderr.getvalue())
            if category in ("CalledProcessError", "OSError"):
                self.assertEqual(stderr.getvalue(), "SECRET\n")

    def test_remote_failures_log_metadata_without_sensitive_error_text(self):
        pane = PaneTarget(Target("ssh", "work", "dev"), "@3", "%7", "/SECRET_SOCKET")
        with tempfile.TemporaryDirectory() as directory:
            logger = sessions.diagnostics.Diagnostics(Path(directory) / "debug.jsonl")
            try:
                for code, output in ((127, ""), (1, "LETEE_DIAG kill_agent pane_lookup CalledProcessError 1 0\n")):
                    failure = subprocess.CalledProcessError(code, ["SECRET_COMMAND"], output=output, stderr="SECRET_STDERR")
                    with (
                        patch("letee.diagnostics.get_diagnostics", return_value=logger),
                        patch("letee.sessions.subprocess.run", side_effect=failure),
                        self.assertRaisesRegex(SystemExit, "SECRET_STDERR"),
                    ):
                        kill_agent(pane)
                logger.flush()
                contents = logger.path.read_text()
            finally:
                logger.close()
        self.assertNotIn("SECRET", contents)
        metadata = [json.loads(line) for line in contents.splitlines()
                    if json.loads(line)["event"] == "sessions.kill_agent.remote"]
        self.assertEqual([record["category"] for record in metadata], ["python_unavailable", "CalledProcessError"])
        self.assertTrue(all(record["host"] == "dev" for record in metadata))

    def test_remote_overlay_reports_ignored_failure_and_preserves_create_status(self):
        target = Target("ssh", "work", "dev")
        with (
            patch.object(sessions, "_install_overlay"),
            patch.object(sessions, "_run") as run,
        ):
            create(target, overlay=True)
        command = run.call_args.args[2][-1]
        with tempfile.TemporaryDirectory() as directory:
            tmux = Path(directory) / "tmux"
            tmux.write_text('#!/bin/sh\ncase "$3" in\nnew-session) exit "$CREATE_STATUS";;\nsource-file) echo SECRET >&2; exit 9;;\nesac\n')
            tmux.chmod(0o700)
            for create_status in (0, 6):
                with self.subTest(create_status=create_status):
                    result = subprocess.run(
                        ("/bin/sh", "-c", command), check=False, capture_output=True, text=True,
                        env={**os.environ, "PATH": directory, "CREATE_STATUS": str(create_status)},
                    )
                    self.assertEqual(result.returncode, create_status)
                    self.assertEqual(result.stdout, "LETEE_DIAG overlay 9\n" if create_status == 0 else "")
                    with patch("letee.sessions.diagnostics.log") as log:
                        sessions._remote_metadata("create", target, result)
                    if create_status == 0:
                        log.assert_called_once_with("sessions.overlay_source.remote", returncode=9, outcome="ignored")
                    else:
                        log.assert_not_called()

    def test_socket_probe_decisions_are_observable(self):
        for failure, decision, stale in (
            (ConnectionRefusedError("SECRET"), "stale", True),
            (FileNotFoundError("SECRET"), "stale", True),
            (PermissionError("SECRET"), "preserve", False),
            (OSError("SECRET unexpected socket error"), "preserve", False),
        ):
            with (
                self.subTest(decision=decision, error=type(failure).__name__),
                patch.object(sessions, "_is_unix_socket", return_value=True),
                patch("letee.sessions.socket.socket") as socket,
                patch("letee.sessions.diagnostics.error") as capture,
            ):
                socket.return_value.connect.side_effect = failure
                self.assertEqual(sessions._is_stale_unix_socket(Path("/SECRET")), stale)
                socket.return_value.close.assert_called_once()
            capture.assert_called_once_with(failure, handling="fallback", stage="agent_socket_probe", decision=decision)

    def test_agent_socket_remove_failure_is_observable(self):
        failure = PermissionError("SECRET")
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.dict(sessions.os.environ, {}, clear=True),
                patch.object(sessions, "FALLBACK_AGENT_SOCKET", Path(directory) / "agent.sock"),
                patch.object(sessions, "_agent_reachable", return_value=False),
                patch.object(sessions, "_is_stale_unix_socket", return_value=True),
                patch("letee.sessions.os.path.lexists", return_value=True),
                patch.object(Path, "unlink", side_effect=failure),
                patch("letee.sessions.diagnostics.error") as capture,
            ):
                self.assertIsNone(sessions.ensure_ssh_agent())
        capture.assert_called_once_with(failure, handling="fallback", stage="agent_socket_remove")

    def test_agent_wait_stopping_decisions_are_observable(self):
        for reachable, exists, is_socket, reason in (
            (None, False, False, "probe_failed"),
            (False, True, False, "non_socket"),
            (False, True, True, "attempts_exhausted"),
        ):
            with (
                self.subTest(reason=reason),
                patch.object(sessions, "_AGENT_START_ATTEMPTS", 1),
                patch.object(sessions, "_agent_reachable", return_value=reachable),
                patch("letee.sessions.os.path.lexists", return_value=exists),
                patch.object(sessions, "_is_unix_socket", return_value=is_socket),
                patch("letee.sessions.diagnostics.log") as log,
            ):
                self.assertFalse(sessions._wait_for_agent(Path("/SECRET")))
            stopped = [call for call in log.call_args_list if call.args[0] == "sessions.agent_wait.stopped"]
            self.assertEqual(len(stopped), 1)
            self.assertEqual(stopped[0].kwargs["reason"], reason)

    def test_unlock_failure_preserves_error_and_closes_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = sessions.diagnostics.Diagnostics(Path(directory) / "debug.jsonl")
            lock = Mock()
            failure = OSError("SECRET")
            try:
                with (
                    patch("letee.diagnostics.get_diagnostics", return_value=logger),
                    patch.object(Path, "open", return_value=lock),
                    patch("letee.sessions.fcntl.flock", side_effect=[None, failure]),
                    self.assertRaises(OSError) as raised,
                ):
                    with sessions._fallback_agent_lock(Path("/SECRET")):
                        pass
                self.assertIs(raised.exception, failure)
                lock.close.assert_called_once()
                logger.flush()
                contents = logger.path.read_text()
            finally:
                logger.close()
        self.assertNotIn("SECRET", contents)
        records = [json.loads(line) for line in contents.splitlines()]
        self.assertTrue(any(record.get("name") == "sessions.agent_unlock" and record.get("outcome") == "failed"
                            for record in records))

    def test_remaining_local_handlers_capture_only_safe_error_metadata(self):
        failure = OSError("SECRET")
        pane = PaneTarget(Target("local", "work"), "@3", "%7", "/SECRET_SOCKET")
        cases = (
            ("agent_socket_stat", lambda: sessions._is_unix_socket(Path("/SECRET")),
             patch("letee.sessions.os.lstat", side_effect=failure)),
            ("pane_process_group", lambda: sessions._kill_agent_local(pane),
             patch("letee.sessions.os.getpgid", side_effect=failure)),
            ("agent_signal", lambda: sessions._kill_agent_local(pane),
             patch("letee.sessions.os.killpg", side_effect=failure)),
        )
        for stage, action, failure_patch in cases:
            with (
                self.subTest(stage=stage), failure_patch,
                patch.object(sessions, "_pane_process_info", return_value=(123, "/SECRET")),
                patch.object(sessions, "_foreground_pgid", return_value=456),
                patch("letee.sessions.os.getpgid", return_value=123) if stage != "pane_process_group" else patch(
                    "letee.sessions.os.killpg"),
                patch("letee.sessions.diagnostics.error") as capture,
            ):
                if stage == "agent_socket_stat":
                    self.assertFalse(action())
                else:
                    with self.assertRaises(SystemExit):
                        action()
            capture.assert_any_call(failure, handling="fallback" if stage == "agent_socket_stat" else "raise", stage=stage)

    def test_pane_parse_failures_are_observable(self):
        pane = PaneTarget(Target("local", "work"), "@3", "%7", "/SECRET_SOCKET")
        for output, stage, action in (
            ("SECRET\t/dev/null", "pane_pid_parse", lambda: sessions._pane_process_info(pane)),
            ("SECRET", "foreground_pgid_parse", lambda: sessions._foreground_pgid(pane.target, 123)),
        ):
            with (
                self.subTest(stage=stage),
                patch("letee.sessions.subprocess.run", return_value=Mock(stdout=output)),
                patch("letee.sessions.diagnostics.error") as capture,
                self.assertRaises(SystemExit),
            ):
                action()
            self.assertEqual(capture.call_args.kwargs, {"handling": "raise", "stage": stage})
            self.assertIsInstance(capture.call_args.args[0], ValueError)

    def test_agent_directory_and_lock_failures_capture_fallback(self):
        failure = PermissionError("private-path")
        with (
            patch.dict(sessions.os.environ, {}, clear=True),
            patch.object(Path, "mkdir", side_effect=failure),
            patch("letee.sessions.diagnostics.error") as capture,
        ):
            self.assertIsNone(sessions.ensure_ssh_agent())
        capture.assert_called_once_with(failure, handling="fallback", stage="agent_directory")
        with (
            patch.object(Path, "open", side_effect=failure),
            patch("letee.sessions.diagnostics.error") as capture,
        ):
            with sessions._fallback_agent_lock(Path("/private/socket")):
                pass
        capture.assert_called_once_with(
            failure, handling="fallback", stage="agent_lock", decision="continue_unlocked",
        )

    def test_lock_acquisition_failure_closes_open_file_and_keeps_fallback_observable(self):
        failure = PermissionError("SECRET lock")
        lock = Mock()
        with (
            patch.object(Path, "open", return_value=lock),
            patch.object(sessions.fcntl, "flock", side_effect=failure),
            patch.object(sessions.diagnostics, "error") as capture,
        ):
            with sessions._fallback_agent_lock(Path("/SECRET/socket")):
                pass
        lock.close.assert_called_once_with()
        capture.assert_called_once_with(failure, handling="fallback", stage="agent_lock", decision="continue_unlocked")
        self.assertNotIn("SECRET", repr(capture.call_args.kwargs))

    def test_agent_start_failure_still_probes_for_concurrent_agent(self):
        failure = OSError("private-path")
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.dict(sessions.os.environ, {}, clear=True),
                patch.object(sessions, "FALLBACK_AGENT_SOCKET", Path(directory) / "agent.sock"),
                patch.object(sessions, "_agent_reachable", return_value=False),
                patch("letee.sessions.subprocess.run", side_effect=failure),
                patch.object(sessions, "_wait_for_agent", return_value=True) as wait,
                patch("letee.sessions.diagnostics.error") as capture,
            ):
                self.assertIsNotNone(sessions.ensure_ssh_agent())
        wait.assert_called_once()
        capture.assert_called_once_with(
            failure, handling="retry", stage="agent_start", decision="probe_after_start",
        )

    def test_command_failure_captures_safe_error_and_preserves_exit(self):
        for failure in (
            subprocess.CalledProcessError(7, ["private-command"], stderr="private-output"),
            subprocess.TimeoutExpired(["private-command"], 2, output="private-output"),
            OSError("private-output"),
        ):
            with (
                self.subTest(error=type(failure).__name__),
                patch("letee.sessions.subprocess.run", side_effect=failure),
                patch("letee.sessions.diagnostics.error") as capture,
                self.assertRaises(SystemExit),
            ):
                kill(Target("local", "work"))
            capture.assert_any_call(
                failure, handling="raise", stage="command", operation="kill", target_kind="local",
            )

    def test_overlay_failure_is_captured_without_failing_create(self):
        for failure in (subprocess.TimeoutExpired(["private-command"], 2, output="private-output"),
                        OSError("private-output")):
            with (
                self.subTest(error=type(failure).__name__),
                patch("letee.sessions.subprocess.run", side_effect=[Mock(stdout=""), failure]),
                patch("letee.sessions.diagnostics.error") as capture,
            ):
                create(Target("local", "work"), overlay=True)
            capture.assert_any_call(failure, handling="ignore", stage="overlay_source", target_kind="local")

    def test_authentication_and_config_failures_capture_fallback(self):
        failure = OSError("private-path")
        for action, stage in (
            (lambda: sessions.prepare_host("dev"), "authentication"),
            (lambda: sessions._ssh_config("dev"), "ssh_config"),
            (lambda: sessions._agent_reachable("/private/socket"), "agent_probe"),
        ):
            with (
                self.subTest(stage=stage),
                patch("letee.sessions.subprocess.run", side_effect=failure),
                patch("letee.sessions.diagnostics.error") as capture,
            ):
                self.assertFalse(action())
                capture.assert_any_call(failure, handling="fallback", stage=stage)


class SSHAgentTest(unittest.TestCase):
    def test_reuses_inherited_agent_with_loaded_keys(self):
        with (
            patch.dict("letee.sessions.os.environ", {"SSH_AUTH_SOCK": "/tmp/inherited"}, clear=True),
            patch.object(sessions, "_agent_reachable", return_value=True) as reachable,
            patch.object(sessions, "FALLBACK_AGENT_SOCKET", Path("/tmp/fallback")),
        ):
            self.assertEqual(sessions.ensure_ssh_agent(), "/tmp/inherited")

        reachable.assert_called_once_with("/tmp/inherited")

    def test_reuses_inherited_empty_agent(self):
        with (
            patch.dict("letee.sessions.os.environ", {"SSH_AUTH_SOCK": "/tmp/inherited"}, clear=True),
            patch.object(sessions, "_agent_reachable", return_value=True),
            patch.object(sessions, "FALLBACK_AGENT_SOCKET", Path("/tmp/fallback")),
        ):
            self.assertEqual(sessions.ensure_ssh_agent(), "/tmp/inherited")

    def test_rejects_stale_inherited_socket_without_removing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            inherited = Path(directory) / "inherited.sock"
            stale = unix_socket.socket(unix_socket.AF_UNIX)
            stale.bind(str(inherited))
            stale.close()
            fallback = Path(directory) / "letee-agent.sock"
            with (
                patch.dict("letee.sessions.os.environ", {"SSH_AUTH_SOCK": str(inherited)}, clear=True),
                patch.object(sessions, "FALLBACK_AGENT_SOCKET", fallback),
                patch.object(sessions, "_agent_reachable", side_effect=[False, True]),
            ):
                self.assertEqual(sessions.ensure_ssh_agent(), str(fallback))

            self.assertTrue(inherited.exists())

    def test_reuses_live_fallback_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "letee-agent.sock"
            with (
                patch.dict("letee.sessions.os.environ", {}, clear=True),
                patch.object(sessions, "FALLBACK_AGENT_SOCKET", socket_path),
                patch.object(sessions, "_agent_reachable", return_value=True),
                patch("letee.sessions.subprocess.run") as run,
            ):
                self.assertEqual(sessions.ensure_ssh_agent(), str(socket_path))

            run.assert_not_called()

    def test_removes_confirmed_stale_unix_socket_only(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "letee-agent.sock"
            stale = unix_socket.socket(unix_socket.AF_UNIX)
            stale.bind(str(socket_path))
            stale.close()
            reachable = iter((False, False, True))
            with (
                patch.dict("letee.sessions.os.environ", {}, clear=True),
                patch.object(sessions, "FALLBACK_AGENT_SOCKET", socket_path),
                patch.object(sessions, "_agent_reachable", side_effect=lambda path: next(reachable)),
                patch("letee.sessions.subprocess.run", return_value=Mock(returncode=0)) as run,
                patch("letee.sessions.time.sleep"),
            ):
                self.assertEqual(sessions.ensure_ssh_agent(), str(socket_path))

            self.assertFalse(socket_path.exists())
            self.assertEqual(run.call_args.args[0], ("ssh-agent", "-s", "-a", str(socket_path)))

    def test_preserves_arbitrary_non_socket_fallback_file(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "letee-agent.sock"
            socket_path.write_text("keep me")
            with (
                patch.dict("letee.sessions.os.environ", {}, clear=True),
                patch.object(sessions, "FALLBACK_AGENT_SOCKET", socket_path),
                patch.object(sessions, "_agent_reachable", return_value=False),
                patch("letee.sessions.subprocess.run") as run,
            ):
                self.assertIsNone(sessions.ensure_ssh_agent())

            self.assertEqual(socket_path.read_text(), "keep me")
            run.assert_not_called()

    def test_reprobes_fallback_after_concurrent_startup_race(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "letee-agent.sock"
            reachable = iter((False, False, True))
            with (
                patch.dict("letee.sessions.os.environ", {}, clear=True),
                patch.object(sessions, "FALLBACK_AGENT_SOCKET", socket_path),
                patch.object(sessions, "_agent_reachable", side_effect=lambda path: next(reachable)),
                patch("letee.sessions.subprocess.run", return_value=Mock(returncode=1)) as run,
                patch("letee.sessions.time.sleep"),
            ):
                self.assertEqual(sessions.ensure_ssh_agent(), str(socket_path))
                self.assertEqual(os.environ["SSH_AUTH_SOCK"], str(socket_path))

            self.assertEqual(run.call_args.args[0], ("ssh-agent", "-s", "-a", str(socket_path)))

    def test_agent_probe_handles_loaded_empty_and_unreachable_statuses(self):
        with patch("letee.sessions.subprocess.run", side_effect=[Mock(returncode=0), Mock(returncode=1), Mock(returncode=2)]) as run:
            self.assertTrue(sessions._agent_reachable("/tmp/agent"))
            self.assertTrue(sessions._agent_reachable("/tmp/agent"))
            self.assertFalse(sessions._agent_reachable("/tmp/agent"))

        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args.kwargs["env"]["SSH_AUTH_SOCK"], "/tmp/agent")


class SSHGroupingTest(unittest.TestCase):
    @staticmethod
    def _config(*, agent=None, files=()):
        lines = []
        if agent is not None:
            lines.append(f"identityagent {agent}")
        lines.extend(f"identityfile {identity_file}" for identity_file in files)
        return "\n".join(lines) + "\n"

    def test_groups_hosts_with_same_effective_identity_agent(self):
        configs = {
            "dev": self._config(agent="/tmp/agent-a", files=("~/.ssh/dev",)),
            "prod": self._config(agent="/tmp/agent-a", files=("~/.ssh/prod",)),
            "staging": self._config(agent="/tmp/agent-b", files=("~/.ssh/dev",)),
        }

        def run(command, **kwargs):
            return Mock(returncode=0, stdout=configs[command[-1]], stderr="")

        with patch("letee.sessions.subprocess.run", side_effect=run):
            self.assertEqual(
                sessions.group_hosts(["dev", "prod", "staging"]),
                [["dev", "prod"], ["staging"]],
            )

    def test_groups_hosts_without_agent_by_identity_files(self):
        configs = {
            "dev": self._config(files=("~/.ssh/shared",)),
            "prod": self._config(files=("~/.ssh/shared",)),
            "staging": self._config(files=("~/.ssh/other",)),
        }

        def run(command, **kwargs):
            return Mock(returncode=0, stdout=configs[command[-1]], stderr="")

        with patch.dict(sessions.os.environ, {}, clear=True), patch("letee.sessions.subprocess.run", side_effect=run):
            self.assertEqual(
                sessions.group_hosts(["dev", "prod", "staging"]),
                [["dev", "prod"], ["staging"]],
            )

    def test_default_agent_from_environment_groups_hosts_without_explicit_identity_agent(self):
        configs = {
            "dev": self._config(files=("~/.ssh/dev",)),
            "prod": self._config(files=("~/.ssh/prod",)),
        }

        def run(command, **kwargs):
            return Mock(returncode=0, stdout=configs[command[-1]], stderr="")

        with (
            patch.dict(sessions.os.environ, {"SSH_AUTH_SOCK": "/tmp/default-agent"}, clear=True),
            patch("letee.sessions.subprocess.run", side_effect=run),
        ):
            self.assertEqual(sessions.group_hosts(["dev", "prod"]), [["dev", "prod"]])

    def test_config_lookup_failure_keeps_host_in_own_group(self):
        def run(command, **kwargs):
            return Mock(returncode=255, stdout="", stderr="bad config")

        with patch("letee.sessions.subprocess.run", side_effect=run):
            self.assertEqual(
                sessions.group_hosts(["dev", "prod"]),
                [["dev"], ["prod"]],
            )

    def test_config_lookup_exception_keeps_host_in_own_group(self):
        with patch("letee.sessions.subprocess.run", side_effect=OSError("ssh missing")):
            self.assertEqual(
                sessions.group_hosts(["dev", "prod"]),
                [["dev"], ["prod"]],
            )

    def test_identity_agent_environment_token_resolves_to_default_agent(self):
        configs = {
            host: self._config(agent="$SSH_AUTH_SOCK", files=(f"~/.ssh/{host}",))
            for host in ("dev", "prod")
        }

        def run(command, **kwargs):
            return Mock(returncode=0, stdout=configs[command[-1]], stderr="")

        with (
            patch.dict(sessions.os.environ, {"SSH_AUTH_SOCK": "/tmp/default-agent"}, clear=True),
            patch("letee.sessions.subprocess.run", side_effect=run),
        ):
            self.assertEqual(sessions.group_hosts(["dev", "prod"]), [["dev", "prod"]])


class SSHPreparationTest(unittest.TestCase):
    def test_prepares_host_without_multiplexing_and_keeps_prompt_streams(self):
        with patch("letee.sessions.subprocess.run", return_value=Mock(returncode=0)) as run:
            self.assertTrue(sessions.prepare_host("dev"))

        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ("dev", "true"))
        self.assertIn(("-o", "BatchMode=no"), list(zip(command, command[1:])))
        self.assertIn(("-o", "ConnectTimeout=2"), list(zip(command, command[1:])))
        self.assertEqual(command.count("ConnectTimeout=2"), 1)
        self.assertIn(("-o", "ControlMaster=no"), list(zip(command, command[1:])))
        self.assertIn(("-o", "ControlPath=none"), list(zip(command, command[1:])))
        self.assertNotIn("ControlMaster=auto", command)
        self.assertNotIn("ControlPath=~/.ssh/letee-%C", command)
        self.assertEqual(run.call_args.kwargs, {"check": False, "timeout": 60})

    def test_preparation_returns_failure_for_ssh_failure(self):
        with patch("letee.sessions.subprocess.run", return_value=Mock(returncode=255)):
            self.assertFalse(sessions.prepare_host("prod"))

    def test_preparation_returns_failure_on_timeout(self):
        with patch(
            "letee.sessions.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["ssh"], 10),
        ) as run:
            self.assertFalse(sessions.prepare_host("prod"))

        self.assertEqual(run.call_args.kwargs["timeout"], 60)

    def test_probe_hosts_runs_checks_in_parallel_and_preserves_host_order(self):
        barrier = threading.Barrier(2)

        def run(command, **kwargs):
            barrier.wait(timeout=5)
            return Mock(returncode=0 if command[-2] == "dev" else 255)

        with (
            patch.dict(sessions.os.environ, {"SSH_AUTH_SOCK": "/tmp/agent"}, clear=True),
            patch("letee.sessions.subprocess.run", side_effect=run) as run_mock,
        ):
            self.assertEqual(sessions.probe_hosts(["dev", "prod"]), [True, False])
            self.assertNotIn("SSH_ASKPASS_REQUIRE", sessions.os.environ)

        self.assertEqual({call.args[0][-2] for call in run_mock.call_args_list}, {"dev", "prod"})
        for call in run_mock.call_args_list:
            command = call.args[0]
            self.assertIn(("-o", "BatchMode=yes"), list(zip(command, command[1:])))
            self.assertEqual(call.kwargs["check"], False)
            self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(call.kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(call.kwargs["timeout"], 3)
            self.assertEqual(call.kwargs["env"]["SSH_AUTH_SOCK"], "/tmp/agent")
            self.assertEqual(call.kwargs["env"]["SSH_ASKPASS_REQUIRE"], "never")


class TmuxOverlayTest(unittest.TestCase):
    def test_overlay_enabled_sources_packaged_file_for_local_commands(self):
        source = shlex.quote(str(sessions.OVERLAY_FILE))
        with patch("letee.sessions.load_tmux_config_overlay", return_value=True):
            self.assertEqual(
                attach_command(Target("local", "work")),
                f"env -u TMUX tmux -L letee.inner -T clipboard new-session -A -s work \\; source-file {source}",
            )
            self.assertEqual(
                pane_attach_command(PaneTarget(Target("local", "work"), "@3", "%7", "/tmp/tmux socket")),
                f"env -u TMUX tmux -S '/tmp/tmux socket' source-file {source} \\; select-window -t work:@3 \\; select-pane -t %7 \\; attach-session -t work",
            )
            with patch("letee.sessions.subprocess.run") as run:
                create(Target("local", "work"))

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ("tmux", "-L", INNER_SERVER_SOCKET, "new-session", "-d", "-s", "work"),
                ("tmux", "-L", INNER_SERVER_SOCKET, "source-file", str(sessions.OVERLAY_FILE)),
            ],
        )
        self.assertFalse(run.call_args_list[-1].kwargs["check"])

    def test_overlay_disabled_preserves_commands_exactly(self):
        local = PaneTarget(Target("local", "work"), "@3", "%7", "/tmp/tmux socket")
        remote = PaneTarget(Target("ssh", "work", "dev"), "@3", "%7", "/tmp/tmux socket")
        with (
            patch("letee.sessions.load_persistent_ssh", return_value=True),
            patch("letee.sessions.load_tmux_config_overlay", return_value=False),
            patch("letee.sessions.subprocess.run") as run,
        ):
            self.assertEqual(attach_command(Target("local", "work")), "env -u TMUX tmux -L letee.inner -T clipboard new-session -A -s work")
            self.assertEqual(
                attach_command(Target("ssh", "work", "dev")),
                "ssh -o ConnectTimeout=2 -o ServerAliveInterval=5 -o ServerAliveCountMax=3 -o AddKeysToAgent=yes -o ControlMaster=no -o 'ControlPath=~/.ssh/letee-%C' -t dev 'tmux -L letee.inner -T clipboard new-session -A -s work'",
            )
            self.assertEqual(
                pane_attach_command(local),
                "env -u TMUX tmux -S '/tmp/tmux socket' select-window -t work:@3 \\; select-pane -t %7 \\; attach-session -t work",
            )
            self.assertEqual(
                pane_attach_command(remote),
                "ssh -o ConnectTimeout=2 -o ServerAliveInterval=5 -o ServerAliveCountMax=3 -o AddKeysToAgent=yes -o ControlMaster=no -o 'ControlPath=~/.ssh/letee-%C' -t dev 'tmux -S '\"'\"'/tmp/tmux socket'\"'\"' select-window -t work:@3 \\; select-pane -t %7 \\; attach-session -t work'",
            )
            create(Target("local", "work"))
            create(Target("ssh", "work", "dev"))

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ("tmux", "-L", INNER_SERVER_SOCKET, "new-session", "-d", "-s", "work"),
                (
                    "ssh", "-o", "ConnectTimeout=2", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
                    "-o", "AddKeysToAgent=yes", "-o", "ControlMaster=auto",
                    "-o", "ControlPath=~/.ssh/letee-%C", "-o", "ControlPersist=10m",
                    "dev", "tmux -L letee.inner new-session -d -s work",
                ),
            ],
        )

    def test_overlay_enabled_installs_and_sources_on_remote(self):
        target = Target("ssh", "work", "dev")
        with (
            patch("letee.sessions.load_persistent_ssh", return_value=True),
            patch("letee.sessions.load_tmux_config_overlay", return_value=True),
            patch("letee.sessions.subprocess.run") as run,
        ):
            attach = attach_command(target)
            pane = pane_attach_command(PaneTarget(target, "@3", "%7", "/tmp/tmux socket"))
            create(target)

        install, create_call = run.call_args_list[0], run.call_args_list[-1]
        self.assertEqual(run.call_count, 2)  # Only create uploads synchronously.
        self.assertEqual(
            install.args[0],
            (
                "ssh", "-o", "ConnectTimeout=2", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
                "-o", "AddKeysToAgent=yes", "-o", "ControlMaster=auto",
                "-o", "ControlPath=~/.ssh/letee-%C", "-o", "ControlPersist=10m",
                "dev", sessions._SSH_INSTALL_OVERLAY,
            ),
        )
        self.assertEqual(install.kwargs["input"], sessions.OVERLAY_FILE.read_text())
        self.assertTrue(create_call.args[0][-1].startswith("tmux -L letee.inner new-session -d -s work && { tmux -L letee.inner source-file ~/.config/letee/tmux-overlay.conf; "))
        self.assertTrue(create_call.args[0][-1].endswith("letee_overlay_rc=$?; printf 'LETEE_DIAG overlay %s\\n' \"$letee_overlay_rc\"; true; }"))
        self.assertTrue(shlex.split(attach)[-1].endswith(
            " && tmux -L letee.inner -T clipboard new-session -A -s work \\; source-file ~/.config/letee/tmux-overlay.conf"
        ))
        self.assertIn("source-file ~/.config/letee/tmux-overlay.conf \\; select-window -t work:@3", pane)
        self.assertLess(pane.index("source-file ~/.config/letee/tmux-overlay.conf"), pane.index("select-window -t work:@3"))
        self.assertIn("/tmp/tmux socket", pane)

    def test_remote_attachment_preparation_defers_overlay_install(self):
        target = Target("ssh", "work", "dev")
        pane = PaneTarget(target, "@3", "%7", "/tmp/tmux socket", session_id="$1")
        window = WindowTarget(target, "$1", "@3", "/tmp/tmux socket")
        actions = (
            lambda: attach_command(target, overlay=True),
            lambda: pane_attach_command(pane, overlay=True),
            lambda: sessions.existing_attach_command(target, "$1", pane.socket_path, overlay=True),
            lambda: sessions.window_attach_command(window, overlay=True),
            lambda: sessions.existing_pane_attach_command(pane, overlay=True),
        )
        for index, action in enumerate(actions):
            with (
                self.subTest(attachment=index),
                patch.object(sessions.subprocess, "run", return_value=Mock(stdout="$1\t@3\t%7\n")) as run,
            ):
                command = shlex.split(action())[-1]
            self.assertEqual(run.call_count, 0 if index < 2 else 1)
            if index >= 2:
                self.assertIn("list-panes", run.call_args.args[0][-1])
            self.assertIn(sessions._SSH_INSTALL_OVERLAY, command)
            self.assertIn("printf %s ", command)
            self.assertIn(" && tmux ", command)

    def test_existing_remote_attachment_validates_before_preparing_overlay(self):
        target = Target("ssh", "work", "dev")
        for session_id, socket_path, output, message in (
            ("invalid", "/tmp/tmux", "", "Invalid session ID"),
            ("$1", "", "", "Invalid socket path"),
            ("$1", "/tmp/tmux", "$2\t@3\t%7\n", "target no longer exists"),
        ):
            with (
                self.subTest(session_id=session_id, socket_path=socket_path),
                patch.object(sessions.subprocess, "run", return_value=Mock(stdout=output)) as run,
                patch.object(sessions, "OVERLAY_FILE") as overlay,
                self.assertRaisesRegex(SystemExit, message),
            ):
                sessions.existing_attach_command(target, session_id, socket_path, overlay=True)
            overlay.read_text.assert_not_called()
            self.assertEqual(run.call_count, 1 if output else 0)

    def test_remote_attach_installs_quoted_overlay_without_consuming_attach_stdin(self):
        overlay = "# 'quoted' $HOME $(touch injected) `touch injected` %s \\\nset -g mouse on\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tmux = root / "tmux"
            tmux.write_text('#!/bin/sh\ncat > "$HOME/attach-input"\n')
            tmux.chmod(0o700)
            with (
                patch.object(sessions, "OVERLAY_FILE", Mock(read_text=Mock(return_value=overlay))),
                patch.object(sessions.subprocess, "run", return_value=Mock(stdout="")),
            ):
                command = shlex.split(attach_command(Target("ssh", "work", "dev"), overlay=True))[-1]
            result = subprocess.run(
                ("/bin/sh", "-c", command), input="keyboard input\n", capture_output=True, text=True,
                env={**os.environ, "HOME": directory, "PATH": f"{directory}:{os.environ['PATH']}"},
                cwd=directory,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = root / ".config/letee/tmux-overlay.conf"
            self.assertEqual(installed.read_text(), overlay)
            self.assertEqual(installed.stat().st_mode & 0o777, 0o600)
            self.assertEqual(installed.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(list(installed.parent.glob(".tmux-overlay.conf.*")), [])
            self.assertEqual((root / "attach-input").read_text(), "keyboard input\n")
            self.assertFalse((root / "injected").exists())

    def test_remote_attach_does_not_run_tmux_after_install_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".config").write_text("not a directory")
            tmux = root / "tmux"
            tmux.write_text('#!/bin/sh\ntouch "$HOME/attached"\n')
            tmux.chmod(0o700)
            with patch.object(sessions.subprocess, "run", return_value=Mock(stdout="")):
                command = shlex.split(attach_command(Target("ssh", "work", "dev"), overlay=True))[-1]
            result = subprocess.run(
                ("/bin/sh", "-c", command), capture_output=True, text=True,
                env={**os.environ, "HOME": directory, "PATH": f"{directory}:{os.environ['PATH']}"},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "attached").exists())

    def test_remote_overlay_install_is_atomic_and_private(self):
        command = sessions._SSH_INSTALL_OVERLAY
        self.assertIn("umask 077", command)
        self.assertIn("mkdir -p ~/.config/letee", command)
        self.assertIn("chmod 700 ~/.config/letee", command)
        self.assertIn("mktemp ~/.config/letee/.tmux-overlay.conf.XXXXXX", command)
        self.assertIn('mv "$tmp" ~/.config/letee/tmux-overlay.conf', command)
        cleanup_trap = "trap 'rm -f \"$tmp\"' 0 HUP INT TERM"
        clear_trap = "trap - 0 HUP INT TERM"
        self.assertLess(command.index(cleanup_trap), command.index('cat > "$tmp"'))
        self.assertGreater(
            command.index(clear_trap),
            command.index('mv "$tmp" ~/.config/letee/tmux-overlay.conf'),
        )

    def test_overlay_install_timeout_retains_cause_and_message(self):
        timeout = subprocess.TimeoutExpired(["ssh"], 3)
        with (
            patch("letee.sessions.load_persistent_ssh", return_value=True),
            patch("letee.sessions.subprocess.run", side_effect=timeout),
            self.assertRaisesRegex(SystemExit, r"^overlay ssh:dev:work timed out$") as raised,
        ):
            create(Target("ssh", "work", "dev"), overlay=True)

        self.assertIs(raised.exception.__cause__, timeout)

    def test_overlay_install_failure_exits_clearly(self):
        error = subprocess.CalledProcessError(1, ["ssh"], stderr="connection refused\n")
        with (
            patch("letee.sessions.load_persistent_ssh", return_value=True),
            patch("letee.sessions.load_tmux_config_overlay", return_value=True),
            patch("letee.sessions.subprocess.run", side_effect=error),
        ):
            with self.assertRaisesRegex(SystemExit, r"^overlay ssh:dev:work failed: connection refused$"):
                create(Target("ssh", "work", "dev"))

    def test_no_command_uses_config_file_flag(self):
        local = PaneTarget(Target("local", "work"), "@3", "%7", "/tmp/tmux socket")
        remote = PaneTarget(Target("ssh", "work", "dev"), "@3", "%7", "/tmp/tmux socket")
        with (
            patch("letee.sessions.load_persistent_ssh", return_value=True),
            patch("letee.sessions.subprocess.run") as run,
        ):
            for overlay in (True, False):
                with patch("letee.sessions.load_tmux_config_overlay", return_value=overlay):
                    commands = [
                        attach_command(Target("local", "work")),
                        attach_command(Target("ssh", "work", "dev")),
                        pane_attach_command(local),
                        pane_attach_command(remote),
                    ]
                    create(Target("local", "work"))
                    create(Target("ssh", "work", "dev"))
                    commands.extend(call.args[0] for call in run.call_args_list)
                    run.reset_mock()
                    for command in commands:
                        tokens = shlex.split(command) if isinstance(command, str) else command
                        if tokens[0] == "ssh":
                            tokens = shlex.split(tokens[-1])
                        self.assertNotIn("-f", tokens, command)


class TmuxOverlayFileTest(unittest.TestCase):
    def setUp(self):
        self.overlay = sessions.OVERLAY_FILE.read_text()
        self.commands = [
            line for line in self.overlay.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    def test_shipped_overlay_does_not_change_prefix(self):
        for line in self.commands:
            self.assertNotIn("prefix", line, line)

    def test_window_formats_and_legacy_markers_are_not_modified(self):
        self.assertNotRegex(
            self.overlay,
            r"(?m)^\s*set(?:-option)?\s+(-\S+\s+)*window-status(?:-current)?-format(?:\s|$)",
        )
        for marker in ("window_end_flag", "@letee_window_status_format", "@letee_window_status_current_format"):
            self.assertNotIn(marker, self.overlay)

    def test_new_window_button_is_prefixed_to_status_right_and_retains_existing_content(self):
        expected = (
            "set -gF status-right "
            "'#[fg=#1e1e2e,bg=#a6e3a1,bold,range=user|letee-new] ＋ add "
            "#[default,range=right] #{status-right}'"
        )
        self.assertIn(expected, self.overlay)

    def test_new_window_button_has_exact_clickable_range_and_restores_right_range(self):
        line = next(line for line in self.commands if line.strip().startswith("set -gF status-right "))
        self.assertIn("fg=#1e1e2e,bg=#a6e3a1,bold", line)
        self.assertIn("range=user|letee-new] ＋ add #[default,range=right]", line)

    def test_new_window_button_extends_status_right_length_and_preserves_unlimited(self):
        expected = (
            "set -gF status-right-length "
            "'#{?#{==:#{status-right-length},0},0,"
            "#{e|+:#{status-right-length},9}}'"
        )
        self.assertIn(expected, self.overlay)

    def test_repeated_overlay_sourcing_does_not_duplicate_buttons(self):
        self.assertEqual(self.overlay.count("set -gF status-right "), 1)
        self.assertIn("if-shell -F '#{!=:#{m:*#\\[*range=user\\|letee-new]*,#{status-right}},1}' {", self.overlay)

    def test_status_right_length_guard_tracks_its_last_value(self):
        self.assertIn(
            "if-shell -F '#{!=:#{status-right-length},#{@letee_status_right_length}}' {",
            self.overlay,
        )
        self.assertIn(
            "set -gF @letee_status_right_length '#{status-right-length}'",
            self.overlay,
        )

    def test_duplicate_guard_matches_range_marker_after_style_attributes(self):
        guard = next(
            line for line in self.commands
            if line.startswith("if-shell -F ") and "#{m:" in line
        )
        pattern = re.search(r"#\{m:(.*),#\{status-right\}\}", guard).group(1)
        self.assertEqual(pattern, r"*#\[*range=user\|letee-new]*")

    def test_new_window_mouse_range_creates_a_window_in_the_active_directory(self):
        binding = next(line for line in self.commands if line.startswith("bind -n MouseDown1Status "))
        self.assertIn(
            "#{==:#{mouse_status_range},letee-new}",
            binding,
        )
        self.assertIn(
            "{ new-window -c \"#{pane_current_path}\" } { select-window -t = }",
            binding,
        )

    def test_other_window_mouse_clicks_still_select_the_clicked_window(self):
        binding = next(line for line in self.commands if line.startswith("bind -n MouseDown1Status "))
        self.assertTrue(binding.endswith("{ select-window -t = }"))

    def test_new_window_button_and_mouse_range_require_tmux_3_4(self):
        feature_start = self.overlay.index('%if "#{>=:#{version},3.4}"')
        feature_end = self.overlay.index("%endif", feature_start)
        feature = self.overlay[feature_start:feature_end]
        for expected in ("status-right", "MouseDown1Status"):
            self.assertIn(expected, feature)
        for unexpected in ("window-status-format", "window-status-current-format"):
            self.assertNotIn(unexpected, feature)

    def test_shipped_overlay_does_not_assign_status_left(self):
        for line in self.commands:
            self.assertIsNone(
                re.match(r"\s*set(-option)?\s+(-\S+\s+)*status-left(\s|$)", line),
                line,
            )

    def test_shipped_overlay_scopes_all_set_commands(self):
        for line in self.commands:
            if line.strip().startswith("set "):
                self.assertRegex(line.strip(), r"^set -(g|s|ag|gF) \S+ .+$")

    def test_package_ships_the_overlay_file(self):
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        package_data = tomllib.loads(pyproject.read_text())["tool"]["setuptools"]["package-data"]
        self.assertIn("tmux-overlay.conf", package_data["letee"])
        self.assertTrue(sessions.OVERLAY_FILE.exists())
        self.assertEqual(sessions.OVERLAY_FILE.name, "tmux-overlay.conf")


if __name__ == "__main__":
    unittest.main()
