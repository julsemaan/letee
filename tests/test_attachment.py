"""Exercise the relay with real PTYs, never host tmux or SSH."""
import errno
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
import termios
import time
import unittest

from letee.names import Target


class AttachmentTests(unittest.TestCase):
    def start(self, script, *, ascii_mode=False, runner_script=None, initial_input=b""):
        master, slave = pty.openpty()
        termios.tcsetwinsize(slave, (31, 97))
        original = termios.tcgetattr(slave)
        if initial_input:
            os.write(master, initial_input)
            self.assertTrue(select.select([slave], [], [], 5)[0], "input was not queued")
        process = subprocess.Popen(
            [sys.executable, "-c", runner_script] if runner_script else
            [sys.executable, "-m", "letee.attachment", "ssh:dev:work", "--",
             sys.executable, "-c", script],
            stdin=slave, stdout=slave, stderr=slave,
            env={**os.environ, "LETEE_ASCII": "1" if ascii_mode else "0"},
        )
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        self.addCleanup(cleanup)
        return process, master, slave, original

    def read_until(self, master, expected, timeout=5):
        data = b""
        deadline = time.monotonic() + timeout
        while expected not in data and time.monotonic() < deadline:
            if select.select([master], [], [], max(0, deadline - time.monotonic()))[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                data += chunk
        self.assertIn(expected, data)
        return data

    def test_fast_output_has_no_waiting_flash_and_preserves_exit(self):
        process, master, slave, original = self.start("print('connected', flush=True); raise SystemExit(7)")
        data = self.read_until(master, b"connected")
        self.assertNotIn(b"Preparing", data)
        self.assertEqual(process.wait(timeout=5), 7)
        self.assertEqual(termios.tcgetattr(slave), original)

    def test_waiting_animation_clears_before_auth_prompt_and_never_resumes(self):
        process, master, slave, original = self.start(
            "import time; time.sleep(.4); print('Password: ', end='', flush=True); "
            "answer=input(); time.sleep(.2); print('answer='+answer, flush=True)",
            ascii_mode=True,
        )
        data = self.read_until(master, b"Password: ")
        self.assertIn(b"+-- Switching session --+", data)
        self.assertIn(b"work", data)
        self.assertIn(b"dev", data)
        self.assertGreaterEqual(data.count(b"Preparing"), 2)
        self.assertTrue(data.endswith(b"\x1b[H\x1b[2JPassword: "))
        os.write(master, b"secret\n")
        tail = self.read_until(master, b"answer=secret")
        self.assertNotIn(b"Preparing", tail)
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertEqual(termios.tcgetattr(slave), original)

    def test_input_queued_before_startup_reaches_child(self):
        process, master, slave, original = self.start(
            "print('answer='+input(), flush=True)",
            initial_input=b"typed-ahead\n",
        )
        self.read_until(master, b"answer=typed-ahead")
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertEqual(termios.tcgetattr(slave), original)

    def test_initial_size_and_resize_reach_child(self):
        script = (
            "import signal, termios, time; "
            "report=lambda *_: print('size='+str(termios.tcgetwinsize(0)), flush=True); "
            "signal.signal(signal.SIGWINCH, report); report(); time.sleep(10)"
        )
        process, master, slave, original = self.start(script)
        self.read_until(master, b"size=(31, 97)")
        termios.tcsetwinsize(slave, (42, 109))
        process.send_signal(signal.SIGWINCH)
        self.read_until(master, b"size=(42, 109)")
        process.send_signal(signal.SIGHUP)
        self.assertEqual(process.wait(timeout=5), 128 + signal.SIGHUP)
        self.assertEqual(termios.tcgetattr(slave), original)

    def test_cancellation_reaps_child_even_when_it_ignores_hangup(self):
        process, master, slave, original = self.start(
            "import os, signal, time; signal.signal(signal.SIGHUP, signal.SIG_IGN); "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('pid='+str(os.getpid())+'!', flush=True); time.sleep(60)"
        )
        data = self.read_until(master, b"!")
        pid = int(data.split(b"pid=")[1].split(b"!")[0])
        process.terminate()
        self.assertEqual(process.wait(timeout=5), 128 + signal.SIGTERM)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual(termios.tcgetattr(slave), original)

    def test_switching_away_while_waiting_clears_spinner_and_reaps_child(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "pid"
            process, master, slave, original = self.start(
                "import os, time; "
                f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
            )
            self.read_until(master, b"Preparing")
            child = int(pid_file.read_text())
            process.send_signal(signal.SIGHUP)
            self.read_until(master, b"\x1b[H\x1b[2J")
            self.assertEqual(process.wait(timeout=5), 128 + signal.SIGHUP)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)
            self.assertEqual(termios.tcgetattr(slave), original)

    def test_cancellation_during_fork_waits_until_child_pid_is_registered(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "unregistered-child"
            runner = f"""
import os, pty, signal, time
from pathlib import Path
from letee import attachment
from letee.names import Target
original = pty.fork
def fork():
    pid, master = original()
    if pid:
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except SystemExit:
            # Reproduce the race without leaving the orphan behind on failure.
            Path({str(marker)!r}).touch()
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            raise
        return pid, master
    time.sleep(60)
    os._exit(0)
pty.fork = fork
raise SystemExit(attachment.run(Target('ssh', 'work', 'dev'), ['unused']))
"""
            process, _, slave, original = self.start("", runner_script=runner)
            self.assertEqual(process.wait(timeout=5), 128 + signal.SIGTERM)
            self.assertFalse(marker.exists(), "cancellation ran before PID assignment")
            self.assertEqual(termios.tcgetattr(slave), original)

    def test_connection_failure_is_visible_and_nonzero(self):
        from letee import attachment
        self.assertIn("letee.attachment", attachment.command(
            Target('ssh', 'work', 'dev'),
            "ssh -t dev true",
        ))
        process, master, _, _ = self.start("raise SystemExit('connection refused')")
        self.read_until(master, b"connection refused")
        self.assertEqual(process.wait(timeout=5), 1)

    def test_unicode_animation_and_silent_exit_clear_screen(self):
        process, master, _, _ = self.start("import time; time.sleep(.3)")
        data = self.read_until(master, "╭─ Switching session ─╮".encode())
        if data.count(b"\x1b[H\x1b[2J") < 2:
            data += self.read_until(master, b"\x1b[H\x1b[2J")
        self.assertTrue(data.endswith(b"\x1b[H\x1b[2J"))
        self.assertEqual(process.wait(timeout=5), 0)

    def test_large_paste_does_not_block_child_output(self):
        count = 200000
        process, master, _, _ = self.start(
            "import os, tty; tty.setraw(0); os.write(1, b'READY'); "
            f"os.write(1, b'x'*{count}); remaining={count}\n"
            "while remaining: remaining -= len(os.read(0, remaining))\n"
            "os.write(1, b'DONE')"
        )
        self.read_until(master, b"READY")
        os.set_blocking(master, False)
        pending = b"a" * count
        received = b""
        deadline = time.monotonic() + 5
        while b"DONE" not in received and time.monotonic() < deadline:
            readable, writable, _ = select.select([master], [master] if pending else [], [], .1)
            if readable:
                received += os.read(master, 65536)
            if writable:
                pending = pending[os.write(master, pending[:65536]):]
        self.assertIn(b"DONE", received)
        self.assertEqual(process.wait(timeout=5), 0)

    def test_child_signal_exit_status(self):
        process, _, slave, original = self.start(
            "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"
        )
        self.assertEqual(process.wait(timeout=5), 128 + signal.SIGTERM)
        self.assertEqual(termios.tcgetattr(slave), original)
