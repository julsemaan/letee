from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import shlex
import socket
import stat
import subprocess
import time

from .config import load_persistent_ssh
from .names import PaneTarget, Target, validate_host


FALLBACK_AGENT_SOCKET = Path.home() / ".ssh" / "letee-agent.sock"
_AGENT_START_ATTEMPTS = 20
_AGENT_START_DELAY = 0.05
SSH_OPTIONS = (
    "-o", "ServerAliveInterval=60",
    "-o", "ServerAliveCountMax=3",
    "-o", "AddKeysToAgent=yes",
)
MULTIPLEX_OPTIONS = (
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=~/.ssh/letee-%C",
)
PERSIST_OPTIONS = (
    "-o", "ControlPersist=10m",
)
PERSISTENT_SSH_OPTIONS = MULTIPLEX_OPTIONS + PERSIST_OPTIONS


def _agent_reachable(socket_path: str) -> bool | None:
    env = os.environ.copy()
    env["SSH_AUTH_SOCK"] = socket_path
    try:
        result = subprocess.run(
            ("ssh-add", "-l"),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode in (0, 1):
        return True
    if result.returncode == 2:
        return False
    return None


def _is_unix_socket(path: Path) -> bool:
    try:
        return stat.S_ISSOCK(os.lstat(path).st_mode)
    except OSError:
        return False


def _is_stale_unix_socket(path: Path) -> bool:
    if not _is_unix_socket(path):
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.1)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        return True
    except OSError:
        return False
    finally:
        probe.close()
    return False


@contextmanager
def _fallback_agent_lock(socket_path: Path):
    lock_path = socket_path.with_name(socket_path.name + ".lock")
    lock = None
    try:
        lock = lock_path.open("a+")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    except OSError:
        if lock is not None:
            lock.close()
        yield
        return
    try:
        yield
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()


def _wait_for_agent(socket_path: Path) -> bool:
    for attempt in range(_AGENT_START_ATTEMPTS):
        reachable = _agent_reachable(str(socket_path))
        if reachable is True:
            return True
        if reachable is None:
            return False
        if os.path.lexists(socket_path) and not _is_unix_socket(socket_path):
            return False
        if attempt + 1 < _AGENT_START_ATTEMPTS:
            time.sleep(_AGENT_START_DELAY)
    return False


def ensure_ssh_agent() -> str | None:
    inherited = os.environ.get("SSH_AUTH_SOCK")
    if inherited and _agent_reachable(inherited) is True:
        return inherited

    socket_path = Path(FALLBACK_AGENT_SOCKET).expanduser()
    try:
        socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        return None

    with _fallback_agent_lock(socket_path):
        reachable = _agent_reachable(str(socket_path))
        if reachable is True:
            os.environ["SSH_AUTH_SOCK"] = str(socket_path)
            return str(socket_path)
        if reachable is None:
            return None
        if os.path.lexists(socket_path):
            if not _is_stale_unix_socket(socket_path):
                return None
            try:
                socket_path.unlink()
            except OSError:
                return None
        try:
            subprocess.run(
                ("ssh-agent", "-s", "-a", str(socket_path)),
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        if _wait_for_agent(socket_path):
            os.environ["SSH_AUTH_SOCK"] = str(socket_path)
            return str(socket_path)
    return None


def prepare_host(host: str) -> bool:
    command = ssh_command(
        "-o", "BatchMode=no",
        "-o", "ConnectTimeout=5",
        "-o", "ControlMaster=no",
        "-o", "ControlPath=none",
        validate_host(host),
        "true",
        persistent_ssh=False,
    )
    try:
        result = subprocess.run(command, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def bootstrap_hosts(hosts: Iterable[str]) -> list[bool]:
    return [prepare_host(host) for host in hosts]


def ssh_command(*args: str, persistent_ssh: bool | None = None, interactive: bool = False) -> tuple[str, ...]:
    if persistent_ssh is None:
        persistent_ssh = load_persistent_ssh()
    # ponytail: same ControlPersist=no on attach for all OpenSSH versions;
    # version detection would add branches without changing the fix
    persist = () if not persistent_ssh else (("-o", "ControlPersist=no") if interactive else PERSIST_OPTIONS)
    multiplex = MULTIPLEX_OPTIONS if persistent_ssh else ()
    return ("ssh", *SSH_OPTIONS, *multiplex, *persist, *args)


def _default_server_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("TMUX", None)
    return env


def attach_command(target: Target) -> str:
    session = shlex.quote(target.session)
    if target.kind == "local":
        return f"env -u TMUX tmux -T clipboard new-session -A -s {session}"
    return shlex.join(ssh_command("-t", target.host or "", f"tmux -T clipboard new-session -A -s {session}", interactive=True))


def pane_attach_command(pane_target: PaneTarget) -> str:
    target = pane_target.target
    tmux = shlex.join(("tmux", "-S", pane_target.socket_path))
    command = (
        f"{tmux} select-window -t {shlex.quote(target.session + ':' + pane_target.window_id)} "
        f"\\; select-pane -t {shlex.quote(pane_target.pane_id)} "
        f"\\; attach-session -t {shlex.quote(target.session)}"
    )
    if target.kind == "local":
        return f"env -u TMUX {command}"
    return shlex.join(ssh_command("-t", target.host or "", command, interactive=True))


def _run(operation: str, target: Target, command: tuple[str, ...], **kwargs: object) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=10, **kwargs)
    except subprocess.TimeoutExpired:
        raise SystemExit(f"{operation} {target.format()} timed out") from None
    except subprocess.CalledProcessError as error:
        reason = (error.stderr or "").strip() or f"exit status {error.returncode}"
        raise SystemExit(f"{operation} {target.format()} failed: {reason}") from None


def create(target: Target) -> None:
    if target.kind == "local":
        _run("create", target, ("tmux", "new-session", "-d", "-s", target.session), env=_default_server_env())
    else:
        _run("create", target, ssh_command(target.host or "", f"tmux new-session -d -s {shlex.quote(target.session)}"))


def kill(target: Target) -> None:
    if target.kind == "local":
        _run("kill", target, ("tmux", "kill-session", "-t", target.session), env=_default_server_env())
    else:
        _run("kill", target, ssh_command(target.host or "", f"tmux kill-session -t {shlex.quote(target.session)}"))
