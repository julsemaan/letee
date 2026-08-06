from pathlib import Path
import os
import socket as unix_socket
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import letee.sessions as sessions
from letee.names import PaneTarget, Target
from letee.sessions import attach_command, create, kill, pane_attach_command, ssh_command


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

    def test_ssh_command_interactive_forces_controlpersist_no(self):
        shared = (
            "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=3",
            "-o", "AddKeysToAgent=yes",
        )
        self.assertEqual(
            ssh_command("-t", "dev", "remote command", persistent_ssh=True, interactive=True),
            (
                "ssh", *shared,
                "-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/letee-%C",
                "-o", "ControlPersist=no", "-t", "dev", "remote command",
            ),
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
        self.assertEqual(
            attach_command(Target("local", "work")),
            "env -u TMUX tmux -T clipboard new-session -A -s work",
        )
        with patch("letee.sessions.load_persistent_ssh", return_value=True):
            self.assertEqual(
                attach_command(Target("ssh", "work", "dev")),
                "ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -o AddKeysToAgent=yes -o ControlMaster=auto -o 'ControlPath=~/.ssh/letee-%C' -o ControlPersist=no -t dev 'tmux -T clipboard new-session -A -s work'",
            )

    def test_pane_attach_commands_select_exact_local_and_remote_pane(self):
        local = PaneTarget(Target("local", "work"), "@3", "%7", "/tmp/tmux socket")
        self.assertEqual(
            pane_attach_command(local),
            "env -u TMUX tmux -S '/tmp/tmux socket' select-window -t work:@3 \\; select-pane -t %7 \\; attach-session -t work",
        )
        remote = PaneTarget(Target("ssh", "work", "dev"), "@3", "%7", "/tmp/tmux socket")
        with patch("letee.sessions.load_persistent_ssh", return_value=False):
            self.assertEqual(
                pane_attach_command(remote),
                "ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -o AddKeysToAgent=yes -t dev 'tmux -S '\"'\"'/tmp/tmux socket'\"'\"' select-window -t work:@3 \\; select-pane -t %7 \\; attach-session -t work'",
            )

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

    def test_command_failures_include_operation_and_target(self):
        error = subprocess.CalledProcessError(1, ["command"], stderr="permission denied\n")
        for operation, action in (
            ("create", lambda: create(Target("local", "work"))),
            ("kill", lambda: kill(Target("ssh", "work", "dev"))),
        ):
            with self.subTest(operation=operation), patch("letee.sessions.subprocess.run", side_effect=error):
                with self.assertRaisesRegex(SystemExit, rf"^{operation} .* failed: permission denied$"):
                    action()

    def test_command_timeout_includes_operation_and_target(self):
        with patch(
            "letee.sessions.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["ssh"], 10),
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
        self.assertEqual(run.call_args.kwargs, {"check": False})

    def test_preparation_returns_failure_for_ssh_failure(self):
        with patch("letee.sessions.subprocess.run", return_value=Mock(returncode=255)):
            self.assertFalse(sessions.prepare_host("prod"))

    def test_bootstrap_hosts_attempts_all_hosts_in_order(self):
        with patch.object(sessions, "prepare_host", side_effect=[True, False]) as prepare:
            self.assertEqual(sessions.bootstrap_hosts(["dev", "prod"]), [True, False])

        self.assertEqual(prepare.call_args_list, [unittest.mock.call("dev"), unittest.mock.call("prod")])


if __name__ == "__main__":
    unittest.main()
