"""PTY relay for interactive remote attachments, with a delayed waiting screen."""
from __future__ import annotations

import errno
import locale
import os
import pty
import select
import shlex
import signal
import sys
import termios
import time
import tty

from .names import Target, parse_target

FRAME_INTERVAL = 0.1
CLEAR = "\033[H\033[2J"


def animation_data(target: Target, title: str) -> tuple[str, list[tuple[int, str, str]]]:
    reset, cyan, dim = "\033[0m", "\033[38;5;81m", "\033[2m"
    ascii_mode = os.environ.get("LETEE_ASCII") == "1" or "utf" not in locale.getpreferredencoding(False).lower()
    if ascii_mode:
        banner = f"+-- {title} --+"
        underline = f"+{'-' * (len(banner) - 2)}+"
    else:
        banner = f"╭─ {title} ─╮"
        underline = f"╰{'─' * (len(banner) - 2)}╯"
    indent = "    "
    text = f"\n{indent}{cyan}{banner}\n{indent}{underline}{reset}\n\n{indent}{dim}Session{reset}  {target.session}\n{indent}{dim}Host{reset}     {target.host or 'local'}\n\n"
    frames = ("|", "/", "-", "\\") if ascii_mode else ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    dots = (".  ", ".. ", "...") if ascii_mode else ("·  ", "·· ", "···", " ··", "  ·")
    colors = (45, 51, 87, 123, 159, 123, 87, 51)
    return text, [(color, frame, dots[index % len(dots)])
                  for index, (color, frame) in enumerate(zip(colors, frames))]


def command(target: Target, child_command: str) -> str:
    return shlex.join((sys.executable, "-m", "letee.attachment", target.format(), "--", *shlex.split(child_command)))


def _write(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data):]


def run(target: Target, argv: list[str]) -> int:
    attributes = termios.tcgetattr(0)
    size = termios.tcgetwinsize(0)
    text, states = animation_data(target, "Switching session")
    handlers = {}
    pid = None
    master = None
    animated = False

    def cancelled(signum, _frame):
        raise SystemExit(128 + signum)

    def resize(_signum, _frame):
        if master is not None:
            termios.tcsetwinsize(master, termios.tcgetwinsize(0))

    try:
        for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT, signal.SIGWINCH):
            handlers[signum] = signal.signal(signum, resize if signum == signal.SIGWINCH else cancelled)
        # Defer cancellation until the parent owns the PID, including inside fork.
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGHUP, signal.SIGTERM, signal.SIGINT})
        try:
            pid, master = pty.fork()
        finally:
            if pid != 0:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        if pid == 0:
            try:
                for signum in handlers:
                    signal.signal(signum, signal.SIG_DFL)
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
                termios.tcsetattr(0, termios.TCSANOW, attributes)
                termios.tcsetwinsize(0, size)
                os.execvp(argv[0], argv)
            except OSError as error:
                print(error, file=sys.stderr, flush=True)
            os._exit(127)
        tty.setraw(0, termios.TCSANOW)
        resize(None, None)
        os.set_blocking(master, False)
        pending = b""
        waiting = True
        frame = 0
        deadline = time.monotonic() + FRAME_INTERVAL
        while True:
            ready, writable, _ = select.select(
                [master, 0] if len(pending) < 65536 else [master],
                [master] if pending else [], [],
                max(0, deadline - time.monotonic()) if waiting else None,
            )
            # Always consume output before drawing a frame, including at the deadline.
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    data = b""
                if not data:
                    break
                if waiting:
                    waiting = False
                    if animated:
                        _write(1, CLEAR.encode())
                        animated = False
                _write(1, data)
            if 0 in ready:
                data = os.read(0, 65536)
                if not data:
                    return 0
                pending += data
            if writable:
                try:
                    pending = pending[os.write(master, pending):]
                except BlockingIOError:
                    pass
            if waiting and time.monotonic() >= deadline:
                if not animated:
                    # Raw output needs explicit CRs. Recovery screens run in cooked mode.
                    _write(1, (CLEAR + text.replace("\n", "\r\n") + "\0337").encode())
                    animated = True
                color, glyph, dots = states[frame % len(states)]
                _write(1, f"\0338\033[2K    \033[38;5;{color}m{glyph}\033[0m Preparing{dots}".encode())
                frame += 1
                deadline = time.monotonic() + FRAME_INTERVAL
        _, status = os.waitpid(pid, 0)
        pid = None
        code = os.waitstatus_to_exitcode(status)
        return code if code >= 0 else 128 - code
    finally:
        if pid:
            # SSH has its own controlling terminal. Kill the group and reap it even
            # if it ignores hangup, rather than leaving a connection after a switch.
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                # Cancellation can arrive before fork's child has called setsid.
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            os.waitpid(pid, 0)
        if master is not None:
            os.close(master)
        try:
            termios.tcsetattr(0, termios.TCSANOW, attributes)
            if animated:
                _write(1, CLEAR.encode())
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)


def main() -> int:
    target = parse_target(sys.argv[1])
    return run(target, sys.argv[3:])


if __name__ == "__main__":
    raise SystemExit(main())
