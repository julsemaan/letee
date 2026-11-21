"""Inner sessions run on letee's bundled tmux, locally and on remote hosts."""

import io
import json
import os
import shlex
import signal
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

from letee import cockpit, config, sessions
from letee.__main__ import main
from letee.names import PaneTarget, Target

LOCAL_TMUX = "/opt/letee/bundle/tmux"
REMOTE_TMUX = "/home/me/.local/share/letee/tmux"


def ssh_result(stdout="", returncode=0, stderr=""):
    """Fake the remote_tmux ssh Popen: write output to its files and exit with returncode."""
    process = Mock(pid=None)
    process.wait.return_value = returncode

    def popen(args, **kwargs):
        kwargs["stdout"].write(stdout)
        kwargs["stderr"].write(stderr)
        process.args = args
        return process
    popen.process = process
    return popen


class TmuxPathCommandTest(unittest.TestCase):
    def test_prints_marker_with_bundled_or_bare_tmux(self):
        for executable in (LOCAL_TMUX, "tmux"):
            with self.subTest(executable=executable), \
                    patch("letee.tmux.tmux_executable", return_value=executable), \
                    redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(main(["tmux-path"]), 0)
            self.assertEqual(stdout.getvalue(), f"LETEE_TMUX={executable}\n")


class TmuxCommandTest(unittest.TestCase):
    def test_execs_letee_tmux_with_verbatim_arguments_and_bundle_path(self):
        cases = (
            (["tmux", "-L", "letee.inner", "ls"], ["-L", "letee.inner", "ls"]),
            (["-L", "work", "tmux", "-V"], ["-V"]),
            (["-Lwork", "tmux", "-V"], ["-V"]),
            (["tmux"], []),
        )
        for argv, expected in cases:
            with self.subTest(argv=argv), \
                    patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True), \
                    patch("letee.tmux.tmux_executable", return_value=LOCAL_TMUX), \
                    patch("letee.__main__.os.execvpe", side_effect=SystemExit(0)) as execvpe, \
                    self.assertRaises(SystemExit):
                main(argv)
            execvpe.assert_called_once_with(LOCAL_TMUX, [LOCAL_TMUX, *expected], {"PATH": "/opt/letee/bundle:/usr/bin"})

    def test_other_commands_do_not_exec_tmux(self):
        with patch("letee.__main__.os.execvpe") as execvpe, redirect_stdout(io.StringIO()):
            main(["version"])
        execvpe.assert_not_called()

    def test_help_and_unknown_options_before_tmux_go_to_argparse(self):
        for argv in (["--help", "tmux"], ["--bogus", "tmux"]):
            with self.subTest(argv=argv), \
                    patch("letee.__main__.os.execvpe") as execvpe, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                main(argv)
            execvpe.assert_not_called()


class PathPrefixTest(unittest.TestCase):
    def test_local_env_puts_bundle_dir_first_only_for_absolute_tmux(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin", "TMUX": "x"}, clear=True):
            with patch("letee.tmux.tmux_executable", return_value=LOCAL_TMUX):
                self.assertEqual(sessions._inner_server_env(), {"PATH": "/opt/letee/bundle:/usr/bin"})
            with patch("letee.tmux.tmux_executable", return_value="tmux"):
                self.assertEqual(sessions._inner_server_env(), {"PATH": "/usr/bin"})

    def test_local_attach_string_prefixes_path_only_for_absolute_tmux(self):
        target = Target("local", "work")
        with patch("letee.tmux.tmux_executable", return_value=LOCAL_TMUX):
            self.assertEqual(
                sessions.attach_command(target, overlay=False),
                f'env -u TMUX PATH=/opt/letee/bundle:"$PATH" {LOCAL_TMUX} -L letee.inner -T clipboard new-session -A -s work',
            )
        with patch("letee.tmux.tmux_executable", return_value="tmux"):
            self.assertEqual(
                sessions.attach_command(target, overlay=False),
                "env -u TMUX tmux -L letee.inner -T clipboard new-session -A -s work",
            )

    def test_remote_commands_use_remote_tmux_with_path_prefix(self):
        target = Target("ssh", "work", "dev")
        prefix = f'PATH=/home/me/.local/share/letee:"$PATH" {REMOTE_TMUX}'
        with patch("letee.sessions.remote_tmux", return_value=REMOTE_TMUX), \
                patch("letee.sessions.subprocess.run") as run:
            sessions.kill(target)
            self.assertEqual(run.call_args.args[0][-1], f"{prefix} -L letee.inner kill-session -t work")
            sessions.kill_agent(PaneTarget(target, "@3", "%7", "/tmp/sock"))
            self.assertIn(f" -- {REMOTE_TMUX} /tmp/sock %7 ", run.call_args.args[0][-1])
        with patch("letee.sessions.remote_tmux", return_value="tmux"), \
                patch("letee.sessions.subprocess.run") as run:
            sessions.kill(target)
            self.assertEqual(run.call_args.args[0][-1], "tmux -L letee.inner kill-session -t work")


class RemoteTmuxTest(unittest.TestCase):
    def setUp(self):
        sessions.forget_remote_tmux()
        self.addCleanup(sessions.forget_remote_tmux)

    def test_miss_resolves_through_login_shell_and_caches(self):
        noise = "Welcome to dev\nLETEE_TMUX=/stale/tmux\nmotd\n"
        with patch("letee.sessions.subprocess.Popen", side_effect=ssh_result(f"{noise}LETEE_TMUX={REMOTE_TMUX}\n")) as run:
            self.assertEqual(sessions.remote_tmux("dev"), REMOTE_TMUX)
            self.assertEqual(sessions.remote_tmux("dev"), REMOTE_TMUX)
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ("dev", 'exec "${SHELL:-/bin/sh}" -lc "letee tmux-path"'))
        self.assertIn("BatchMode=yes", command)
        self.assertEqual(json.loads(config.remote_tmux_cache_path().read_text()), {"dev": REMOTE_TMUX})

    def test_miss_honors_caller_timeouts(self):
        with patch("letee.sessions.subprocess.Popen", side_effect=ssh_result(f"LETEE_TMUX={REMOTE_TMUX}\n")) as run:
            sessions.remote_tmux("dev", connect_timeout=7, timeout=12.5)
        self.assertIn("ConnectTimeout=7", run.call_args.args[0])
        run.side_effect.process.wait.assert_called_once_with(timeout=12.5)

    def test_missing_letee_raises_install_hint_and_caches_nothing(self):
        for result in (ssh_result("Welcome\n"), ssh_result("", 127, "sh: letee: not found")):
            with self.subTest(result=result), patch("letee.sessions.subprocess.Popen", side_effect=result), \
                    self.assertRaises(SystemExit) as raised:
                sessions.remote_tmux("dev")
            self.assertIn("letee is not installed on dev", str(raised.exception))
            self.assertIn("install.sh | sh", str(raised.exception))
        self.assertFalse(config.remote_tmux_cache_path().exists())

    def test_unreachable_host_reports_ssh_error(self):
        with patch("letee.sessions.subprocess.Popen", side_effect=ssh_result("", 255, "ssh: connect to host dev: Connection refused\n")), \
                self.assertRaises(SystemExit) as raised:
            sessions.remote_tmux("dev")
        self.assertEqual(str(raised.exception), "ssh: connect to host dev: Connection refused")

    def test_hands_ssh_process_to_on_spawn(self):
        fake = ssh_result(f"LETEE_TMUX={REMOTE_TMUX}\n")
        on_spawn = Mock()
        with patch("letee.sessions.subprocess.Popen", side_effect=fake) as popen:
            self.assertEqual(sessions.remote_tmux("dev", on_spawn=on_spawn), REMOTE_TMUX)
        on_spawn.assert_called_once_with(fake.process)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_timeout_kills_and_reaps_ssh(self):
        fake = ssh_result()
        fake.process.pid = 4242
        fake.process.wait.side_effect = [subprocess.TimeoutExpired(("ssh",), 3), -9]
        with patch("letee.sessions.subprocess.Popen", side_effect=fake), \
                patch("letee.sessions.os.killpg") as killpg, self.assertRaises(SystemExit) as raised:
            sessions.remote_tmux("dev", timeout=3)
        self.assertEqual(str(raised.exception), "dev: timed out")
        killpg.assert_called_once_with(4242, signal.SIGKILL)
        self.assertEqual(fake.process.wait.call_count, 2)
        self.assertFalse(config.remote_tmux_cache_path().exists())

    def test_forget_clears_cache(self):
        with patch("letee.sessions.subprocess.Popen", side_effect=ssh_result(f"LETEE_TMUX={REMOTE_TMUX}\n")) as run:
            sessions.remote_tmux("dev")
            sessions.forget_remote_tmux()
            sessions.remote_tmux("dev")
        self.assertEqual(run.call_count, 2)


class CurrentTargetTest(unittest.TestCase):
    def assert_parses(self, command, expected):
        with (
            patch.object(cockpit, "_option", return_value=""),
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "out", return_value=command),
        ):
            self.assertEqual(cockpit.current_target(), expected)

    def test_parses_absolute_tmux_with_path_prefix_and_bare_tmux(self):
        for executable in (LOCAL_TMUX, "tmux"):
            with self.subTest(executable=executable), \
                    patch("letee.tmux.tmux_executable", return_value=executable), \
                    patch("letee.sessions.remote_tmux", return_value=executable), \
                    patch("letee.sessions.load_persistent_ssh", return_value=False):
                local, remote = Target("local", "work"), Target("ssh", "work", "dev")
                self.assert_parses(sessions.attach_command(local, overlay=False), local)
                self.assert_parses(sessions.attach_command(remote, overlay=False), remote)
                pane = PaneTarget(local, "@3", "%7", "/tmp/tmux-1000/letee.inner")
                self.assert_parses(sessions.pane_attach_command(pane, overlay=False), local)

    def test_ssh_attach_string_shape(self):
        with patch("letee.sessions.remote_tmux", return_value=REMOTE_TMUX), \
                patch("letee.sessions.load_persistent_ssh", return_value=False):
            command = sessions.attach_command(Target("ssh", "work", "dev"), overlay=False)
        self.assertTrue(shlex.split(command)[-1].startswith(f'PATH=/home/me/.local/share/letee:"$PATH" {REMOTE_TMUX} -L'))


if __name__ == "__main__":
    unittest.main()
