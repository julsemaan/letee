"""Save and restore every session on letee.inner with the bundled tmux-resurrect.

The host-side functions run in-process for local targets and as
`letee resurrect restore|save|forget` over SSH for remote ones.
"""

from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import queue
import shlex
import subprocess
import threading
from typing import TYPE_CHECKING

from . import diagnostics, sessions
from .config import file_lock, load_timing

if TYPE_CHECKING:
    from .discovery import SessionSnapshot

READY_OPTION = "@letee-resurrect-ready"
_DIR_OPTION = "@resurrect-dir"
# Stops resurrect from replacing live panes when it mistakes a one-pane server for a fresh one.
_NEVER_OVERWRITE_OPTION = "@resurrect-never-overwrite"
# Resurrect restores "from scratch" into a server holding only this session, then kills it.
_PLACEHOLDER_SESSION = "0"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "letee" / "resurrect"


def scripts_dir() -> Path:
    return Path(__file__).with_name("_vendor") / "tmux_resurrect" / "scripts"


def _lock(*, blocking: bool):
    # Snapshots hold each pane's full argv, so keep the directory private to the user.
    directory = state_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    return file_lock(directory / ".lock", blocking=blocking)


def _tmux(*args: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        sessions.inner_argv(None, *args), check=False, capture_output=True, text=True,
        timeout=load_timing().local_command_timeout if timeout is None else timeout,
        env=sessions._inner_server_env(),
    )


def _script(name: str, *args: str) -> tuple[str, ...]:
    """Point resurrect at letee's save dir, then run one of its scripts.

    run-shell blocks until the script finishes, so callers can hold the lock for its whole run.
    """
    command = shlex.join(("bash", str(scripts_dir() / name), *args))
    return ("set", "-g", _DIR_OPTION, str(state_dir()), ";", "run-shell", command)


def _server_state() -> tuple[bool, bool]:
    """Return (running, ready). tmux exits non-zero when no server runs."""
    result = _tmux("show", "-gqv", READY_OPTION)
    return result.returncode == 0, result.returncode == 0 and result.stdout.strip() == "1"


def _mark_ready() -> None:
    _tmux("set", "-g", READY_OPTION, "1")


def ensure_restored() -> str:
    """Restore the last save into a server that has not been checked yet.

    Returns `ready`, `nothing`, `restored` or `failed`.
    """
    with diagnostics.operation("resurrect.restore") as span:
        try:
            span.outcome = _ensure_restored()
        except (OSError, subprocess.SubprocessError) as error:
            diagnostics.error(error, handling="fallback", stage="resurrect_restore")
            span.outcome = "failed"
        return span.outcome


def _ensure_restored() -> str:
    if _server_state()[1]:
        return "ready"
    with ExitStack() as stack:
        with diagnostics.operation("resurrect.restore.lock_wait"):
            stack.enter_context(_lock(blocking=True))
        running, ready = _server_state()
        if ready:
            return "ready"
        if not (state_dir() / "last").exists():
            if running:
                _mark_ready()
            return "nothing"
        protect = running and not _tmux("show", "-gqv", _NEVER_OVERWRITE_OPTION).stdout.strip()
        if protect:
            _tmux("set", "-g", _NEVER_OVERWRITE_OPTION, "on")
        elif not running:
            _tmux("new-session", "-d", "-s", _PLACEHOLDER_SESSION)
        try:
            with diagnostics.operation("resurrect.restore.script") as span:
                result = _tmux(*_script("restore.sh"), timeout=load_timing().session_restore_timeout)
                span.outcome = "restored" if result.returncode == 0 else "failed"
        finally:
            if protect:
                _tmux("set", "-gu", _NEVER_OVERWRITE_OPTION)
        if result.returncode != 0:
            diagnostics.log("resurrect.restore.script", returncode=result.returncode)
            return "failed"
        _mark_ready()
        return "restored"


def save() -> str:
    """Save and wait for it to finish. Never saves a server that skipped the restore check.

    Returns `no_server`, `busy`, `failed` or `saved`.
    """
    with diagnostics.operation("resurrect.save") as span:
        try:
            span.outcome = _save()
        except (OSError, subprocess.SubprocessError) as error:
            diagnostics.error(error, handling="fallback", stage="resurrect_save")
            span.outcome = "failed"
        return span.outcome


def _save() -> str:
    running, ready = _server_state()
    if not running:
        return "no_server"
    if not ready and ensure_restored() == "failed":
        return "failed"
    with _lock(blocking=False) as acquired:
        if not acquired:
            return "busy"
        result = _tmux(*_script("save.sh", "quiet"), timeout=load_timing().session_restore_timeout)
        return "saved" if result.returncode == 0 else "failed"


def forget() -> str:
    """Drop the last save once the server has no sessions left.

    Call this only after a deliberate kill. An empty server cannot be saved,
    so the next start would otherwise restore the killed sessions.
    Returns `forgotten`, `kept` or `failed`.
    """
    with diagnostics.operation("resurrect.forget") as span:
        try:
            with _lock(blocking=True):
                result = _tmux("list-sessions", "-F", "#{session_name}")
                if result.returncode == 0 and result.stdout.strip():
                    span.outcome = "kept"
                else:
                    (state_dir() / "last").unlink(missing_ok=True)
                    span.outcome = "forgotten"
        except (OSError, subprocess.SubprocessError) as error:
            diagnostics.error(error, handling="fallback", stage="resurrect_forget")
            span.outcome = "failed"
        return span.outcome


def remote_command(action: str) -> str:
    """Run `letee resurrect <action>` in a login shell, like the tmux-path lookup."""
    return sessions.remote_letee_command("resurrect", action)


def remote_guard(host: str) -> str:
    """Shell snippet that asks the remote letee to restore unless already checked.

    It fails when the remote letee is too old to have the subcommand.
    """
    probe = sessions.shell_join(sessions.inner_argv(host, "show", "-gqv", READY_OPTION))
    return f'[ "$({probe} 2>/dev/null)" = 1 ] || {remote_command("restore")}'


class SessionSaver:
    """Queue resurrect saves from sidebar snapshots without blocking the UI loop.

    A host saves when its pane structure changes or an interval has passed.
    A failed save makes that host wait one interval before trying again.
    """

    def __init__(self) -> None:
        self._interval = load_timing().session_save_interval
        self._signatures: dict[str | None, tuple] = {}
        self._next_save: dict[str | None, float] = {}
        self._retry_at: dict[str | None, float] = {}
        self._pending: set[str | None] = set()
        self._requests: queue.SimpleQueue[str | None] = queue.SimpleQueue()
        self._results: queue.SimpleQueue[tuple[str | None, bool]] = queue.SimpleQueue()
        threading.Thread(target=self._drain, name="letee-session-saver", daemon=True).start()

    def observe(self, snapshot: SessionSnapshot, now: float) -> None:
        while not self._results.empty():
            host, saved = self._results.get()
            self._pending.discard(host)
            if not saved:
                self._retry_at[host] = now + self._interval
        for host, source in ((None, snapshot.local), *snapshot.remotes.items()):
            if host in self._pending or not source or not source.available or not source.panes:
                continue
            if now < self._retry_at.get(host, now):
                continue
            signature = tuple(sorted((pane.target.session, pane.window_id, pane.pane_id) for pane in source.panes))
            if signature == self._signatures.get(host) and now < self._next_save[host]:
                continue
            self._signatures[host] = signature
            self._next_save[host] = now + self._interval
            self._pending.add(host)
            self._requests.put(host)

    def _drain(self) -> None:
        while True:
            host = self._requests.get()
            self._results.put((host, self._save(host)))

    @staticmethod
    def _save(host: str | None) -> bool:
        if host is None:
            return save() == "saved"
        with diagnostics.operation("resurrect.remote_save", host=host) as span:
            try:
                result = subprocess.run(
                    sessions.ssh_command("-n", "-o", "BatchMode=yes", host, remote_command("save")),
                    check=False, capture_output=True, text=True,
                    timeout=load_timing().session_restore_timeout,
                )
            except (OSError, subprocess.SubprocessError) as error:
                diagnostics.error(error, handling="fallback", stage="resurrect_remote_save")
                span.outcome = "failed"
                return False
            span.fields["returncode"] = result.returncode
            span.outcome = "saved" if result.returncode == 0 else "failed"
            return result.returncode == 0
