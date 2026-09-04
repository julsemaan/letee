from pathlib import Path
import os
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
from letee.names import PaneTarget, Target
from letee.sessions import attach_command, create, kill, kill_agent, pane_attach_command, rename, ssh_command


class SessionOperationsTest(unittest.TestCase):
    def test_ssh_command_always_adds_keepalive_and_agent_option_and_optionally_adds_persistence(self):
        shared = (
            "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=3",
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
            "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=3",
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
            "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=3",
            "-o", "AddKeysToAgent=yes",
        )
        self.assertEqual(
            ssh_command("-t", "dev", "remote command", persistent_ssh=False, interactive=True),
            ("ssh", *shared, "-t", "dev", "remote command"),
        )

    def test_ssh_command_non_interactive_keeps_persist_options(self):
        shared = (
            "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=3",
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
                "env -u TMUX tmux -T clipboard new-session -A -s work",
            )
            with patch("letee.sessions.load_persistent_ssh", return_value=True):
                self.assertEqual(
                    attach_command(Target("ssh", "work", "dev")),
                    "ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -o AddKeysToAgent=yes -o ControlMaster=no -o 'ControlPath=~/.ssh/letee-%C' -t dev 'tmux -T clipboard new-session -A -s work'",
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
                    "ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -o AddKeysToAgent=yes -o ControlMaster=no -o 'ControlPath=~/.ssh/letee-%C' -t dev 'tmux -S '\"'\"'/tmp/tmux socket'\"'\"' select-window -t work:@3 \\; select-pane -t %7 \\; attach-session -t work'",
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
        self.assertTrue(command[-1].endswith(" -- '/tmp/tmux socket; echo unsafe' %7"))
        self.assertIn("/tmp/tmux socket; echo unsafe", command[-1])
        self.assertNotIn("kill-pane", command[-1])

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

    def test_kill_local_session_uses_default_server(self):
        with (
            patch.dict("letee.sessions.os.environ", {"TMUX": "/tmp/letee,1,0", "PATH": "x"}, clear=True),
            patch("letee.sessions.subprocess.run") as run,
        ):
            kill(Target("local", "work"))

        run.assert_called_once_with(
            ("tmux", "kill-session", "-t", "work"),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
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
            ("tmux", "new-session", "-d", "-s", "work"),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
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
                    "ssh", "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=3",
                    "-o", "AddKeysToAgent=yes", "-o", "ControlMaster=auto",
                    "-o", "ControlPath=~/.ssh/letee-%C", "-o", "ControlPersist=10m",
                    "dev", "tmux new-session -d -s work.one",
                ),
                (
                    "ssh", "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=3",
                    "-o", "AddKeysToAgent=yes", "dev", "tmux kill-session -t work.one",
                ),
            ],
        )

    def test_rename_local_session_returns_new_target(self):
        old = Target("local", "work")
        with (
            patch.dict("letee.sessions.os.environ", {"TMUX": "/tmp/letee,1,0", "PATH": "x"}, clear=True),
            patch("letee.sessions.subprocess.run") as run,
        ):
            self.assertEqual(rename(old, "renamed"), Target("local", "renamed"))

        run.assert_called_once_with(
            ("tmux", "rename-session", "-t", "work", "renamed"),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
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
                "ssh", "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=3",
                "-o", "AddKeysToAgent=yes", "-o", "ControlMaster=auto",
                "-o", "ControlPath=~/.ssh/letee-%C", "-o", "ControlPersist=10m",
                "dev", "tmux rename-session -t work renamed",
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
        self.assertIn(("-o", "ConnectTimeout=5"), list(zip(command, command[1:])))
        self.assertIn(("-o", "ControlMaster=no"), list(zip(command, command[1:])))
        self.assertIn(("-o", "ControlPath=none"), list(zip(command, command[1:])))
        self.assertNotIn("ControlMaster=auto", command)
        self.assertNotIn("ControlPath=~/.ssh/letee-%C", command)
        self.assertEqual(run.call_args.kwargs, {"check": False, "timeout": 10})

    def test_preparation_returns_failure_for_ssh_failure(self):
        with patch("letee.sessions.subprocess.run", return_value=Mock(returncode=255)):
            self.assertFalse(sessions.prepare_host("prod"))

    def test_preparation_returns_failure_on_timeout(self):
        with patch(
            "letee.sessions.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["ssh"], 10),
        ) as run:
            self.assertFalse(sessions.prepare_host("prod"))

        self.assertEqual(run.call_args.kwargs["timeout"], 10)

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
            self.assertEqual(call.kwargs["timeout"], 10)
            self.assertEqual(call.kwargs["env"]["SSH_AUTH_SOCK"], "/tmp/agent")
            self.assertEqual(call.kwargs["env"]["SSH_ASKPASS_REQUIRE"], "never")


class TmuxOverlayTest(unittest.TestCase):
    def test_overlay_enabled_sources_packaged_file_for_local_commands(self):
        source = shlex.quote(str(sessions.OVERLAY_FILE))
        with patch("letee.sessions.load_tmux_config_overlay", return_value=True):
            self.assertEqual(
                attach_command(Target("local", "work")),
                f"env -u TMUX tmux -T clipboard new-session -A -s work \\; source-file {source}",
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
                ("tmux", "new-session", "-d", "-s", "work"),
                ("tmux", "source-file", str(sessions.OVERLAY_FILE)),
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
            self.assertEqual(attach_command(Target("local", "work")), "env -u TMUX tmux -T clipboard new-session -A -s work")
            self.assertEqual(
                attach_command(Target("ssh", "work", "dev")),
                "ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -o AddKeysToAgent=yes -o ControlMaster=no -o 'ControlPath=~/.ssh/letee-%C' -t dev 'tmux -T clipboard new-session -A -s work'",
            )
            self.assertEqual(
                pane_attach_command(local),
                "env -u TMUX tmux -S '/tmp/tmux socket' select-window -t work:@3 \\; select-pane -t %7 \\; attach-session -t work",
            )
            self.assertEqual(
                pane_attach_command(remote),
                "ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -o AddKeysToAgent=yes -o ControlMaster=no -o 'ControlPath=~/.ssh/letee-%C' -t dev 'tmux -S '\"'\"'/tmp/tmux socket'\"'\"' select-window -t work:@3 \\; select-pane -t %7 \\; attach-session -t work'",
            )
            create(Target("local", "work"))
            create(Target("ssh", "work", "dev"))

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ("tmux", "new-session", "-d", "-s", "work"),
                (
                    "ssh", "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=3",
                    "-o", "AddKeysToAgent=yes", "-o", "ControlMaster=auto",
                    "-o", "ControlPath=~/.ssh/letee-%C", "-o", "ControlPersist=10m",
                    "dev", "tmux new-session -d -s work",
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
        self.assertEqual(run.call_count, 4)  # attach, pane attach, and create each install; create then runs
        self.assertEqual(
            install.args[0],
            (
                "ssh", "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=3",
                "-o", "AddKeysToAgent=yes", "-o", "ControlMaster=auto",
                "-o", "ControlPath=~/.ssh/letee-%C", "-o", "ControlPersist=10m",
                "dev", sessions._SSH_INSTALL_OVERLAY,
            ),
        )
        self.assertEqual(install.kwargs["input"], sessions.OVERLAY_FILE.read_text())
        self.assertTrue(create_call.args[0][-1].endswith("tmux new-session -d -s work && { tmux source-file ~/.config/letee/tmux-overlay.conf || true; }"))
        self.assertIn("-t dev 'tmux -T clipboard new-session -A -s work \\; source-file ~/.config/letee/tmux-overlay.conf'", attach)
        self.assertIn("source-file ~/.config/letee/tmux-overlay.conf \\; select-window -t work:@3", pane)
        self.assertLess(pane.index("source-file ~/.config/letee/tmux-overlay.conf"), pane.index("select-window -t work:@3"))
        self.assertIn("/tmp/tmux socket", pane)

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

    def test_overlay_install_failure_exits_clearly(self):
        error = subprocess.CalledProcessError(1, ["ssh"], stderr="connection refused\n")
        with (
            patch("letee.sessions.load_persistent_ssh", return_value=True),
            patch("letee.sessions.load_tmux_config_overlay", return_value=True),
            patch("letee.sessions.subprocess.run", side_effect=error),
        ):
            with self.assertRaisesRegex(SystemExit, r"^overlay ssh:dev:work failed: connection refused$"):
                attach_command(Target("ssh", "work", "dev"))

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
                        tokens = command.split() if isinstance(command, str) else command
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

    def test_new_window_button_is_appended_to_both_window_formats(self):
        button = (
            "#[fg=#1e1e2e,bg=#1e1e2e] "
            "#[fg=#1e1e2e,bg=#89b4fa,bold]#[range=user|letee-new] [+] #[norange]"
        )
        for option in ("window-status-format", "window-status-current-format"):
            expected = f"set -ag {option} '#{{?window_end_flag,{button},}}'"
            self.assertIn(expected, self.overlay)

    def test_new_window_button_is_limited_to_the_final_window_tab(self):
        for option in ("window-status-format", "window-status-current-format"):
            line = next(line for line in self.commands if line.strip().startswith(f"set -ag {option} "))
            self.assertIn("#{?window_end_flag,", line)
            self.assertIn("#[range=user|letee-new] [+] #[norange],}", line)

    def test_repeated_overlay_sourcing_does_not_duplicate_buttons(self):
        for option, marker in (
            ("window-status-format", "letee_window_status_format"),
            ("window-status-current-format", "letee_window_status_current_format"),
        ):
            self.assertEqual(self.overlay.count(f"set -ag {option} "), 1)
            self.assertIn(f"if-shell -F '#{{!=:#{{@{marker}}},1}}' {{", self.overlay)
            self.assertIn(f"set -g @{marker} 1", self.overlay)

    def test_new_window_mouse_range_creates_a_window_in_the_active_directory(self):
        binding = next(line for line in self.commands if line.startswith("bind -n MouseDown1Status "))
        self.assertIn("#{==:#{mouse_status_range},letee-new}", binding)
        self.assertIn('new-window -c "#{pane_current_path}"', binding)

    def test_other_window_mouse_clicks_still_select_the_clicked_window(self):
        binding = next(line for line in self.commands if line.startswith("bind -n MouseDown1Status "))
        self.assertTrue(binding.endswith("'select-window -t ='"))

    def test_new_window_mouse_range_requires_tmux_3_4(self):
        feature_start = self.overlay.index('%if "#{>=:#{version},3.4}"')
        feature_end = self.overlay.index("%endif", feature_start)
        feature = self.overlay[feature_start:feature_end]
        for expected in ("window-status-format", "window-status-current-format", "MouseDown1Status"):
            self.assertIn(expected, feature)

    def test_shipped_overlay_does_not_assign_status_left_or_right(self):
        for line in self.commands:
            self.assertIsNone(
                re.match(r"\s*set(-option)?\s+(-\S+\s+)*status-(left|right)(\s|$)", line),
                line,
            )

    def test_shipped_overlay_scopes_all_set_commands(self):
        for line in self.commands:
            if line.strip().startswith("set "):
                self.assertRegex(line.strip(), r"^set -(g|s|ag) \S+ .+$")

    def test_package_ships_the_overlay_file(self):
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        self.assertIn('[tool.setuptools.package-data]', pyproject.read_text())
        self.assertIn('letee = ["tmux-overlay.conf"]', pyproject.read_text())
        self.assertTrue(sessions.OVERLAY_FILE.exists())
        self.assertEqual(sessions.OVERLAY_FILE.name, "tmux-overlay.conf")


if __name__ == "__main__":
    unittest.main()
