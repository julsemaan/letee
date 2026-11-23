import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from letee import config, resurrect, sessions
from letee.config import Timing
from letee.discovery import SessionSnapshot, SourceSnapshot, UNAVAILABLE
from letee.names import PaneTarget, Target
from tests import use_bare_inner_tmux


def setUpModule():
    use_bare_inner_tmux()
    for target in ("letee.resurrect.load_timing", "letee.sessions.load_timing"):
        patcher = patch(target, return_value=Timing())
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)


class FakeTmux:
    """letee.inner as seen by resurrect: running, ready, and the calls it got."""

    def __init__(self, *, running=False, ready=False, script_rc=0, never_overwrite=""):
        self.running, self.ready, self.script_rc = running, ready, script_rc
        self.never_overwrite = never_overwrite
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv, **kwargs):
        self.assert_inner(argv)
        args = tuple(argv[3:])
        self.calls.append(args)
        rc, stdout = 0, ""
        if args[0] == "show":
            rc = 0 if self.running else 1
            if args[2] == resurrect._NEVER_OVERWRITE_OPTION:
                stdout = f"{self.never_overwrite}\n" if self.never_overwrite else ""
            else:
                stdout = "1\n" if self.ready else ""
        elif args[0] == "new-session":
            self.running = True
        elif args[:3] == ("set", "-g", resurrect.READY_OPTION):
            self.ready = True
        elif "run-shell" in args:
            rc = self.script_rc
        return subprocess.CompletedProcess(argv, rc, stdout, "")

    @staticmethod
    def assert_inner(argv):
        assert tuple(argv[:3]) == ("tmux", "-L", "letee.inner"), argv

    def commands(self):
        return [call[0] if "run-shell" not in call else "script" for call in self.calls]


class ResurrectTestCase(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": tempdir.name})
        env.start()
        self.addCleanup(env.stop)
        self.state = Path(tempdir.name) / "letee" / "resurrect"

    def save_exists(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "last").write_text("pane\n")

    def run_with(self, fake, function):
        with patch("letee.resurrect.subprocess.run", side_effect=fake):
            return function()


class EnsureRestoredTest(ResurrectTestCase):
    def test_ready_server_needs_one_query(self):
        fake = FakeTmux(running=True, ready=True)
        self.assertEqual(self.run_with(fake, resurrect.ensure_restored), "ready")
        self.assertEqual(fake.calls, [("show", "-gqv", resurrect.READY_OPTION)])

    def test_no_save_marks_running_server(self):
        fake = FakeTmux(running=True)
        self.assertEqual(self.run_with(fake, resurrect.ensure_restored), "nothing")
        self.assertEqual(fake.commands(), ["show", "show", "set"])
        self.assertTrue(fake.ready)

    def test_no_save_never_starts_a_server(self):
        fake = FakeTmux()
        self.assertEqual(self.run_with(fake, resurrect.ensure_restored), "nothing")
        self.assertEqual(fake.commands(), ["show", "show"])
        self.assertFalse(fake.running)

    def test_restore_from_scratch_uses_placeholder(self):
        self.save_exists()
        fake = FakeTmux()
        self.assertEqual(self.run_with(fake, resurrect.ensure_restored), "restored")
        self.assertEqual(fake.commands(), ["show", "show", "new-session", "script", "set"])
        self.assertEqual(fake.calls[2], ("new-session", "-d", "-s", "0"))
        self.assertFalse(any(resurrect._NEVER_OVERWRITE_OPTION in call for call in fake.calls))
        script = fake.calls[3]
        self.assertEqual(script[:5], ("set", "-g", "@resurrect-dir", str(self.state), ";"))
        self.assertEqual(script[5], "run-shell")
        self.assertEqual(shlex.split(script[6]), ["bash", str(resurrect.scripts_dir() / "restore.sh")])
        self.assertTrue(fake.ready)

    def test_restore_into_running_server_forces_never_overwrite(self):
        self.save_exists()
        option = resurrect._NEVER_OVERWRITE_OPTION
        for script_rc, outcome in ((0, "restored"), (1, "failed")):
            with self.subTest(script_rc=script_rc):
                fake = FakeTmux(running=True, script_rc=script_rc)
                self.assertEqual(self.run_with(fake, resurrect.ensure_restored), outcome)
                self.assertNotIn("new-session", fake.commands())
                self.assertEqual(fake.calls[2:6], [
                    ("show", "-gqv", option), ("set", "-g", option, "on"),
                    fake.calls[4], ("set", "-gu", option),
                ])
                self.assertEqual(fake.commands()[4], "script")

    def test_never_overwrite_is_unset_when_script_times_out(self):
        self.save_exists()
        fake = FakeTmux(running=True)

        def timing_out(argv, **kwargs):
            result = fake(argv)
            if "run-shell" in argv:
                raise subprocess.TimeoutExpired("tmux", 30)
            return result

        self.assertEqual(self.run_with(timing_out, resurrect.ensure_restored), "failed")
        self.assertEqual(fake.calls[-1], ("set", "-gu", resurrect._NEVER_OVERWRITE_OPTION))

    def test_user_never_overwrite_is_left_alone(self):
        self.save_exists()
        fake = FakeTmux(running=True, never_overwrite="on")
        self.assertEqual(self.run_with(fake, resurrect.ensure_restored), "restored")
        self.assertEqual(fake.commands(), ["show", "show", "show", "script", "set"])
        self.assertFalse(any(resurrect._NEVER_OVERWRITE_OPTION in call[:3] for call in fake.calls if call[0] == "set"))

    def test_restore_waits_with_its_own_timeout(self):
        self.save_exists()
        timeouts = []

        def fake(argv, **kwargs):
            timeouts.append(kwargs["timeout"])
            return FakeTmux(running=True)(argv)

        with patch("letee.resurrect.load_timing", return_value=Timing(session_restore_timeout=42)), \
             patch("letee.resurrect.subprocess.run", side_effect=fake):
            resurrect.ensure_restored()
        self.assertIn(42, timeouts)

    def test_failed_script_leaves_server_unmarked(self):
        self.save_exists()
        fake = FakeTmux(running=True, script_rc=1)
        self.assertEqual(self.run_with(fake, resurrect.ensure_restored), "failed")
        self.assertFalse(fake.ready)

    def test_timeout_is_reported_as_failure(self):
        self.save_exists()
        with patch("letee.resurrect.subprocess.run", side_effect=subprocess.TimeoutExpired("tmux", 30)):
            self.assertEqual(resurrect.ensure_restored(), "failed")

    def test_server_marked_while_waiting_for_lock_is_not_restored(self):
        self.save_exists()
        fake = FakeTmux(running=True)
        responses = iter(("", "1\n"))

        def racing(argv, **kwargs):
            result = fake(argv)
            if argv[3] == "show":
                result.stdout = next(responses)
            return result

        self.assertEqual(self.run_with(racing, resurrect.ensure_restored), "ready")
        self.assertEqual(fake.commands(), ["show", "show"])


class SaveTest(ResurrectTestCase):
    def test_no_server_skips(self):
        fake = FakeTmux()
        self.assertEqual(self.run_with(fake, resurrect.save), "no_server")
        self.assertEqual(fake.commands(), ["show"])

    def test_unchecked_server_runs_restore_check_first(self):
        self.save_exists()
        fake = FakeTmux(running=True)
        self.assertEqual(self.run_with(fake, resurrect.save), "saved")
        self.assertEqual(fake.commands(), ["show", "show", "show", "show", "set", "script", "set", "set", "script"])
        self.assertIn("restore.sh", fake.calls[5][-1])
        self.assertIn("save.sh", fake.calls[-1][-1])

    def test_failed_restore_check_prevents_save(self):
        self.save_exists()
        fake = FakeTmux(running=True, script_rc=1)
        self.assertEqual(self.run_with(fake, resurrect.save), "failed")
        self.assertNotIn("save.sh", " ".join(fake.calls[-1]))

    def test_held_lock_skips(self):
        fake = FakeTmux(running=True, ready=True)
        # flock conflicts across open files, even within one process.
        with config.file_lock(resurrect.state_dir() / ".lock"):
            self.assertEqual(self.run_with(fake, resurrect.save), "busy")
        self.assertEqual(fake.commands(), ["show"])

    def test_save_blocks_under_lock_with_letee_dir(self):
        fake = FakeTmux(running=True, ready=True)
        timeouts = []

        def recording(argv, **kwargs):
            timeouts.append(kwargs["timeout"])
            return fake(argv)

        with patch("letee.resurrect.load_timing", return_value=Timing(session_restore_timeout=42)):
            self.assertEqual(self.run_with(recording, resurrect.save), "saved")
        command = fake.calls[-1]
        self.assertEqual(command[:6], ("set", "-g", "@resurrect-dir", str(self.state), ";", "run-shell"))
        self.assertEqual(shlex.split(command[6]), ["bash", str(resurrect.scripts_dir() / "save.sh"), "quiet"])
        self.assertEqual(timeouts[-1], 42)

    def test_save_makes_state_dir_private(self):
        self.state.mkdir(parents=True)
        self.state.chmod(0o755)
        self.assertEqual(self.run_with(FakeTmux(running=True, ready=True), resurrect.save), "saved")
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)


class ForgetTest(ResurrectTestCase):
    def list_sessions(self, rc, stdout=""):
        return patch("letee.resurrect.subprocess.run",
                     return_value=subprocess.CompletedProcess((), rc, stdout, ""))

    def test_empty_or_missing_server_drops_last_save(self):
        for rc, stdout in ((0, ""), (1, "")):
            with self.subTest(rc=rc):
                self.save_exists()
                with self.list_sessions(rc, stdout) as run:
                    self.assertEqual(resurrect.forget(), "forgotten")
                self.assertEqual(tuple(run.call_args.args[0][3:]), ("list-sessions", "-F", "#{session_name}"))
                self.assertFalse((self.state / "last").exists())

    def test_remaining_sessions_keep_last_save(self):
        self.save_exists()
        with self.list_sessions(0, "other\n"):
            self.assertEqual(resurrect.forget(), "kept")
        self.assertTrue((self.state / "last").exists())

    def test_restore_finds_nothing_after_forget(self):
        self.save_exists()
        with self.list_sessions(1):
            resurrect.forget()
        self.assertEqual(self.run_with(FakeTmux(), resurrect.ensure_restored), "nothing")


class RemoteCommandTest(unittest.TestCase):
    def test_remote_command_uses_login_shell(self):
        self.assertEqual(resurrect.remote_command("save"), sessions.remote_letee_command("resurrect", "save"))

    def test_remote_actions_resolve_and_preserve_arguments(self):
        with tempfile.TemporaryDirectory(prefix="remote home ' ") as directory:
            home = Path(directory)
            fallback = home / ".local/bin/letee"
            fallback.parent.mkdir(parents=True)
            path_bin = home / "path bin"
            path_bin.mkdir()
            shell = home / "login shell"
            shell.write_text('#!/bin/sh\n[ "$1" = -lc ] || exit 99\nexec /bin/sh -c "$2"\n')
            shell.chmod(0o755)
            env = {"HOME": str(home), "PATH": str(path_bin), "SHELL": str(shell),
                   "XDG_CONFIG_HOME": str(home / "config")}
            fallback.write_text('#!/bin/sh\nprintf "fallback\\n"; printf "%s\\n" "$@"\n')
            fallback.chmod(0o755)
            for on_path in (False, True):
                executable = path_bin / "letee"
                if on_path:
                    executable.write_text('#!/bin/sh\nprintf "path\\n"; printf "%s\\n" "$@"\n')
                    executable.chmod(0o755)
                for action in ("save", "restore", "forget", "space ' ; $HOME"):
                    with self.subTest(on_path=on_path, action=action):
                        result = subprocess.run(resurrect.remote_command(action), shell=True,
                                                env=env, capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        marker = "path" if on_path else "fallback"
                        self.assertEqual(result.stdout, f"{marker}\nresurrect\n{action}\n")
            executable.unlink()
            fallback.unlink()
            result = subprocess.run(resurrect.remote_command("save"), shell=True,
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 127)

    def test_guard_checks_marker_before_restoring(self):
        self.assertEqual(
            resurrect.remote_guard("dev"),
            '[ "$(tmux -L letee.inner show -gqv @letee-resurrect-ready 2>/dev/null)" = 1 ] || '
            + resurrect.remote_command("restore"),
        )

    def test_vendored_scripts_are_executable(self):
        for name in ("save.sh", "restore.sh"):
            self.assertTrue(os.access(resurrect.scripts_dir() / name, os.X_OK))


    def test_vendored_scripts_run_from_path_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            vendor = Path(tmp) / "with space"
            shutil.copytree(resurrect.scripts_dir().parent, vendor)
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            fake_tmux = bin_dir / "tmux"
            fake_tmux.write_text('#!/bin/sh\n[ "$1" = -V ] && echo "tmux 3.4"\nexit 0\n')
            fake_tmux.chmod(0o755)
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SCRIPTS": str(vendor / "scripts")}
            check = 'CURRENT_DIR="$SCRIPTS"; source "$SCRIPTS/variables.sh"; source "$SCRIPTS/helpers.sh"; supported_tmux_version_ok'
            result = subprocess.run(["bash", "-c", check], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

class PsStrategyTest(unittest.TestCase):
    script = resurrect.scripts_dir().parent / "save_command_strategies" / "ps.sh"

    def save_command(self, pane_pid, env=None):
        result = subprocess.run(["bash", str(self.script), str(pane_pid)], env=env, capture_output=True, text=True, check=True)
        return result.stdout

    def spawn_pane(self, script):
        pane = subprocess.Popen(["bash", "-c", script])
        self.addCleanup(lambda: (subprocess.run(["pkill", "-P", str(pane.pid)]), pane.kill(), pane.wait()))
        return pane

    def wait_for_children(self, pane, count):
        for _ in range(100):
            children = subprocess.run(["pgrep", "-P", str(pane.pid)], capture_output=True, text=True).stdout.split()
            if len(children) == count:
                return
            time.sleep(0.05)
        self.fail(f"pane {pane.pid} never had {count} children")

    @unittest.skipUnless(Path("/proc/self/cmdline").exists(), "needs procfs")
    def test_procfs_preserves_exact_argv(self):
        argv = ["python3", "-c", "import time; time.sleep(30)", "notes -S/tmp/x.vim", "a\nb", "$(id)"]
        pane = self.spawn_pane(f"{shlex.join(argv)} & wait")
        self.wait_for_children(pane, 1)
        saved = self.save_command(pane.pid)
        self.assertEqual(saved.count("\n"), 1)
        reparsed = subprocess.run(["bash", "-c", f"printf '%s\\0' {saved}"], capture_output=True, text=True, check=True)
        self.assertEqual(reparsed.stdout.split("\0")[:-1], argv)

    def test_several_children_save_nothing(self):
        pane = self.spawn_pane("sleep 30 & sleep 30 & wait")
        self.wait_for_children(pane, 2)
        self.assertEqual(self.save_command(pane.pid), "")

    def test_without_procfs_keeps_single_word_commands_only(self):
        cases = {
            "htop": "htop\n",
            "/usr/bin/top": "/usr/bin/top\n",
            "vim notes.txt": "",
            "vim notes -S/tmp/x.vim": "",
            "vim$(id)": "",
            "less;id": "",
        }
        for command, expected in cases.items():
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                fake_ps = Path(tmp) / "ps"
                fake_ps.write_text(
                    "#!/bin/sh\n"
                    'if [ "$1" = -Ao ]; then echo "   42 999999999"; echo "    7 43"; '
                    f"else printf '%s\\n' {shlex.quote(command)}; fi\n"
                )
                fake_ps.chmod(0o755)
                env = {**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"}
                self.assertEqual(self.save_command(42, env), expected)


def pane(host, session="work", pane_id="%1"):
    target = Target("ssh", session, host) if host else Target("local", session)
    return PaneTarget(target, "@1", pane_id, "/tmp/sock")


def source(*panes):
    return SourceSnapshot(True, tuple({p.target for p in panes}), frozenset(), panes=panes)


class SessionSaverTest(unittest.TestCase):
    def setUp(self):
        thread = patch("letee.resurrect.threading.Thread")
        thread.start()
        self.addCleanup(thread.stop)
        with patch("letee.resurrect.load_timing", return_value=Timing(session_save_interval=60)):
            self.saver = resurrect.SessionSaver()

    def queued(self):
        hosts = []
        while not self.saver._requests.empty():
            hosts.append(self.saver._requests.get())
        return hosts

    def finish(self, host, saved=True):
        self.saver._results.put((host, saved))

    def test_first_sample_saves_each_available_host_with_panes(self):
        snapshot = SessionSnapshot(source(pane(None)), {
            "dev": source(pane("dev")), "down": UNAVAILABLE, "empty": source(), "gone": None,
        })
        self.saver.observe(snapshot, 0)
        self.assertEqual(self.queued(), [None, "dev"])

    def test_structure_change_saves_before_interval(self):
        self.saver.observe(SessionSnapshot(source(pane(None)), {}), 0)
        self.finish(None)
        self.queued()
        self.saver.observe(SessionSnapshot(source(pane(None)), {}), 10)
        self.assertEqual(self.queued(), [])
        self.saver.observe(SessionSnapshot(source(pane(None), pane(None, pane_id="%2")), {}), 11)
        self.assertEqual(self.queued(), [None])

    def test_interval_saves_unchanged_structure(self):
        snapshot = SessionSnapshot(source(pane(None)), {})
        self.saver.observe(snapshot, 0)
        self.finish(None)
        self.queued()
        self.saver.observe(snapshot, 59)
        self.assertEqual(self.queued(), [])
        self.saver.observe(snapshot, 60)
        self.assertEqual(self.queued(), [None])

    def test_pending_save_is_not_queued_twice(self):
        self.saver.observe(SessionSnapshot(source(pane(None)), {}), 0)
        self.saver.observe(SessionSnapshot(source(pane(None, pane_id="%2")), {}), 1)
        self.assertEqual(self.queued(), [None])

    def test_failure_backs_off_one_interval(self):
        self.saver.observe(SessionSnapshot(source(pane(None)), {}), 0)
        self.finish(None, saved=False)
        self.queued()
        changed = SessionSnapshot(source(pane(None, pane_id="%2")), {})
        self.saver.observe(changed, 5)
        self.assertEqual(self.queued(), [])
        self.saver.observe(changed, 64)
        self.assertEqual(self.queued(), [])
        self.saver.observe(changed, 65)
        self.assertEqual(self.queued(), [None])

    def test_local_save_runs_in_process(self):
        with patch.object(resurrect, "save", return_value="saved") as save:
            self.assertTrue(resurrect.SessionSaver._save(None))
        save.assert_called_once_with()
        with patch.object(resurrect, "save", return_value="busy"):
            self.assertFalse(resurrect.SessionSaver._save(None))

    def test_remote_save_runs_letee_over_ssh(self):
        with patch("letee.resurrect.subprocess.run", return_value=Mock(returncode=0)) as run, \
             patch("letee.sessions.load_persistent_ssh", return_value=False):
            self.assertTrue(resurrect.SessionSaver._save("dev"))
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ("dev", resurrect.remote_command("save")))
        self.assertIn("BatchMode=yes", command)
        self.assertEqual(run.call_args.kwargs["timeout"], Timing().session_restore_timeout)
        self.assertFalse(run.call_args.kwargs["check"])

    def test_remote_save_failures_report_false(self):
        for outcome in (Mock(returncode=255), subprocess.TimeoutExpired("ssh", 3)):
            with self.subTest(outcome=outcome), \
                 patch("letee.resurrect.subprocess.run", side_effect=[outcome]), \
                 patch("letee.sessions.load_persistent_ssh", return_value=False):
                self.assertFalse(resurrect.SessionSaver._save("dev"))

    def test_worker_reports_each_result(self):
        self.saver._requests.put("dev")
        self.saver._requests.put("stop")
        with patch.object(resurrect.SessionSaver, "_save", side_effect=[True, RuntimeError]):
            with self.assertRaises(RuntimeError):
                self.saver._drain()
        self.assertEqual(self.saver._results.get_nowait(), ("dev", True))


class SessionsGateTest(unittest.TestCase):
    def setUp(self):
        for name, value in (("load_tmux_config_overlay", False), ("load_persistent_ssh", False)):
            patcher = patch.object(sessions, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_local_create_and_attach_run_restore_check(self):
        with patch.object(sessions, "load_session_restore", return_value=True), \
             patch.object(resurrect, "ensure_restored") as ensure, \
             patch("letee.sessions.subprocess.run", return_value=Mock(returncode=0, stdout="")):
            sessions.create(Target("local", "work"))
            sessions.attach_command(Target("local", "work"))
        self.assertEqual(ensure.call_count, 2)

    def test_remote_create_runs_guard_with_restore_timeout(self):
        with patch.object(sessions, "load_session_restore", return_value=True), \
             patch("letee.sessions.subprocess.run", return_value=Mock(returncode=0, stdout="")) as run:
            sessions.create(Target("ssh", "work", "dev"))
        restore, create = run.call_args_list
        self.assertEqual(restore.args[0][-1], resurrect.remote_guard("dev"))
        self.assertEqual(restore.kwargs["timeout"], Timing().session_restore_timeout)
        self.assertEqual(create.args[0][-1], "tmux -L letee.inner new-session -d -s work")

    def test_failing_remote_restore_does_not_block_create(self):
        failure = subprocess.CalledProcessError(127, "ssh", stderr="letee: invalid choice")
        with patch.object(sessions, "load_session_restore", return_value=True), \
             patch("letee.sessions.subprocess.run", side_effect=[failure, Mock(returncode=0, stdout="")]) as run:
            sessions.create(Target("ssh", "work", "dev"))
        self.assertEqual(run.call_args.args[0][-1], "tmux -L letee.inner new-session -d -s work")

    def test_remote_attach_groups_guard_before_tmux(self):
        with patch.object(sessions, "load_session_restore", return_value=True):
            command = shlex.split(sessions.attach_command(Target("ssh", "work", "dev")))[-1]
        self.assertEqual(
            command,
            f"{{ {{ {resurrect.remote_guard('dev')}; }} </dev/null >/dev/null 2>&1; "
            "tmux -L letee.inner -T clipboard new-session -A -s work; }",
        )

    def test_remote_attach_overlay_installer_controls_the_group(self):
        with patch.object(sessions, "load_session_restore", return_value=True):
            command = shlex.split(sessions.attach_command(Target("ssh", "work", "dev"), overlay=True))[-1]
        self.assertIn(f") && {{ {{ {resurrect.remote_guard('dev')}; }}", command)

    def test_local_kill_forgets_save(self):
        with patch.object(sessions, "load_session_restore", return_value=True), \
             patch.object(resurrect, "forget") as forget, \
             patch("letee.sessions.subprocess.run", return_value=Mock(returncode=0, stdout="")):
            sessions.kill(Target("local", "work"))
        forget.assert_called_once_with()

    def test_remote_kill_forgets_save_best_effort(self):
        failure = subprocess.CalledProcessError(2, "ssh", stderr="letee: invalid choice: 'forget'")
        for outcome in (Mock(returncode=0, stdout=""), failure):
            with self.subTest(outcome=outcome), \
                 patch.object(sessions, "load_session_restore", return_value=True), \
                 patch("letee.sessions.subprocess.run",
                       side_effect=[Mock(returncode=0, stdout=""), outcome]) as run:
                sessions.kill(Target("ssh", "work", "dev"))
            command = run.call_args.args[0]
            self.assertEqual(command[-2:], ("dev", resurrect.remote_command("forget")))
            self.assertIn("BatchMode=yes", command)

    def test_kill_skips_forget_when_disabled(self):
        with patch.object(sessions, "load_session_restore", return_value=False), \
             patch.object(resurrect, "forget") as forget, \
             patch("letee.sessions.subprocess.run", return_value=Mock(returncode=0, stdout="")) as run:
            sessions.kill(Target("local", "work"))
            sessions.kill(Target("ssh", "work", "dev"))
        forget.assert_not_called()
        self.assertEqual(run.call_count, 2)

    def test_nothing_added_when_disabled_or_attach_only(self):
        for enabled, attach_only in ((False, False), (True, True)):
            with self.subTest(enabled=enabled, attach_only=attach_only), \
                 patch.object(sessions, "load_session_restore", return_value=enabled), \
                 patch.object(resurrect, "ensure_restored") as ensure, \
                 patch("letee.sessions.subprocess.run", return_value=Mock(returncode=0, stdout="")) as run:
                if not attach_only:
                    sessions.create(Target("local", "work"))
                    sessions.create(Target("ssh", "work", "dev"))
                    self.assertEqual(run.call_count, 2)
                for kind, host in (("local", None), ("ssh", "dev")):
                    command = sessions.attach_command(Target(kind, "work", host), attach_only=attach_only)
                    self.assertNotIn("resurrect", command)
                ensure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
