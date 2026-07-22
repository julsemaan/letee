import subprocess
import unittest
from unittest.mock import patch

from mtmux.names import PaneTarget, Target
from mtmux.sessions import attach_command, create, kill, pane_attach_command, ssh_command


class SessionOperationsTest(unittest.TestCase):
    def test_ssh_command_always_adds_keepalive_and_optionally_adds_persistence(self):
        keepalive = ("-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=3")
        self.assertEqual(
            ssh_command("-t", "dev", "remote command", persistent_ssh=True),
            (
                "ssh", *keepalive,
                "-o", "ControlMaster=auto", "-o", "ControlPersist=10m",
                "-o", "ControlPath=~/.ssh/mtmux-%C", "-t", "dev", "remote command",
            ),
        )
        self.assertEqual(
            ssh_command("-t", "dev", "remote command", persistent_ssh=False),
            ("ssh", *keepalive, "-t", "dev", "remote command"),
        )

    def test_attach_commands_quote_local_and_remote_targets(self):
        self.assertEqual(
            attach_command(Target("local", "work")),
            "env -u TMUX tmux -T clipboard new-session -A -s work",
        )
        with patch("mtmux.sessions.load_persistent_ssh", return_value=True):
            self.assertEqual(
                attach_command(Target("ssh", "work", "dev")),
                "ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -o ControlMaster=auto -o ControlPersist=10m -o 'ControlPath=~/.ssh/mtmux-%C' -t dev 'tmux -T clipboard new-session -A -s work'",
            )

    def test_pane_attach_commands_select_exact_local_and_remote_pane(self):
        local = PaneTarget(Target("local", "work"), "@3", "%7", "/tmp/tmux socket")
        self.assertEqual(
            pane_attach_command(local),
            "env -u TMUX tmux -S '/tmp/tmux socket' select-window -t work:@3 \\; select-pane -t %7 \\; attach-session -t work",
        )
        remote = PaneTarget(Target("ssh", "work", "dev"), "@3", "%7", "/tmp/tmux socket")
        with patch("mtmux.sessions.load_persistent_ssh", return_value=False):
            self.assertEqual(
                pane_attach_command(remote),
                "ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -t dev 'tmux -S '\"'\"'/tmp/tmux socket'\"'\"' select-window -t work:@3 \\; select-pane -t %7 \\; attach-session -t work'",
            )

    def test_kill_local_session_uses_default_server(self):
        with (
            patch.dict("mtmux.sessions.os.environ", {"TMUX": "/tmp/mtmux,1,0", "PATH": "x"}, clear=True),
            patch("mtmux.sessions.subprocess.run") as run,
        ):
            kill(Target("local", "work"))

        run.assert_called_once_with(
            ("tmux", "kill-session", "-t", "work"),
            check=True,
            capture_output=True,
            text=True,
            env={"PATH": "x"},
        )

    def test_create_mutates_without_cockpit_dependency(self):
        target = Target("local", "work")
        with (
            patch.dict("mtmux.sessions.os.environ", {"TMUX": "/tmp/mtmux,1,0", "PATH": "x"}, clear=True),
            patch("mtmux.sessions.subprocess.run") as run,
        ):
            create(target)

        run.assert_called_once_with(
            ("tmux", "new-session", "-Ad", "-s", "work"),
            check=True,
            capture_output=True,
            text=True,
            env={"PATH": "x"},
        )

    def test_remote_create_and_kill_use_configured_persistence(self):
        target = Target("ssh", "work.one", "dev")
        with (
            patch("mtmux.sessions.load_persistent_ssh", side_effect=[True, False]),
            patch("mtmux.sessions.subprocess.run") as run,
        ):
            create(target)
            kill(target)

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                (
                    "ssh", "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=3",
                    "-o", "ControlMaster=auto", "-o", "ControlPersist=10m",
                    "-o", "ControlPath=~/.ssh/mtmux-%C", "dev", "tmux new-session -Ad -s work.one",
                ),
                (
                    "ssh", "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=3",
                    "dev", "tmux kill-session -t work.one",
                ),
            ],
        )

    def test_command_failures_include_operation_and_target(self):
        error = subprocess.CalledProcessError(1, ["command"], stderr="permission denied\n")
        for operation, action in (
            ("create", lambda: create(Target("local", "work"))),
            ("kill", lambda: kill(Target("ssh", "work", "dev"))),
        ):
            with self.subTest(operation=operation), patch("mtmux.sessions.subprocess.run", side_effect=error):
                with self.assertRaisesRegex(SystemExit, rf"^{operation} .* failed: permission denied$"):
                    action()


if __name__ == "__main__":
    unittest.main()
