from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
import fcntl
import os
from pathlib import Path
import shlex
import signal
import socket
import stat
import subprocess
import time

from .config import load_persistent_ssh, load_tmux_config_overlay
from .names import INNER_SERVER_SOCKET, PaneTarget, Target, validate_host


OVERLAY_FILE = Path(__file__).with_name("tmux-overlay.conf")
REMOTE_OVERLAY_PATH = "~/.config/letee/tmux-overlay.conf"
# Atomic remote install: private dir, temp file, rename. Unconditional rewrite
# replaces stale remote copies whenever letee ships an updated overlay.
_SSH_INSTALL_OVERLAY = (
    "umask 077 && mkdir -p ~/.config/letee && chmod 700 ~/.config/letee"
    " && tmp=$(mktemp ~/.config/letee/.tmux-overlay.conf.XXXXXX)"
    " && trap 'rm -f \"$tmp\"' 0 HUP INT TERM"
    " && cat > \"$tmp\" && mv \"$tmp\" ~/.config/letee/tmux-overlay.conf"
    " && trap - 0 HUP INT TERM"
)


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
INTERACTIVE_OPTIONS = (
    "-o", "ControlMaster=no",
    "-o", "ControlPath=~/.ssh/letee-%C",
)
PERSIST_OPTIONS = (
    "-o", "ControlPersist=10m",
)
_KILL_AGENT_HELPER = r'''import os
import signal
import subprocess
import sys

try:
    socket_path, pane_id = sys.argv[2:]
    result = subprocess.run(
        (
            "tmux",
            "-S",
            socket_path,
            "display-message",
            "-p",
            "-t",
            pane_id,
            "#{pane_pid}\t#{pane_tty}",
        ),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
        env={key: value for key, value in os.environ.items() if key != "TMUX"},
    )
    pane_pid, pane_tty = result.stdout.strip().split("\t", 1)
    pane_pid = int(pane_pid)
    if pane_pid <= 0:
        raise ValueError("invalid pane PID")
    process = subprocess.run(
        ("ps", "-o", "tpgid=", "-p", str(pane_pid)),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
        env={key: value for key, value in os.environ.items() if key != "TMUX"},
    )
    foreground_pgid = int(process.stdout.strip())
    pane_pgid = os.getpgid(pane_pid)
    if foreground_pgid <= 0 or foreground_pgid == pane_pgid:
        raise RuntimeError("no foreground process")
    os.killpg(foreground_pgid, signal.SIGTERM)
except subprocess.CalledProcessError as error:
    print((error.stderr or "").strip() or str(error), file=sys.stderr)
    raise SystemExit(1)
except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
    print(error, file=sys.stderr)
    raise SystemExit(1)'''


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


def _check_host(host: str, *, batch_mode: bool) -> bool:
    command = ssh_command(
        "-o", f"BatchMode={'yes' if batch_mode else 'no'}",
        "-o", "ConnectTimeout=5",
        "-o", "ControlMaster=no",
        "-o", "ControlPath=none",
        validate_host(host),
        "true",
        persistent_ssh=False,
    )
    kwargs: dict[str, object] = {"check": False, "timeout": 10}
    if batch_mode:
        env = os.environ.copy()
        env["SSH_ASKPASS_REQUIRE"] = "never"
        kwargs.update(
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    try:
        result = subprocess.run(command, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _ssh_config(host: str) -> tuple[str | None, tuple[str, ...]] | None:
    try:
        result = subprocess.run(
            ("ssh", "-G", validate_host(host)),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    agent = None
    identity_files: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        option, value = parts
        if option == "identityagent":
            agent = value
        elif option == "identityfile" and value != "none":
            identity_files.append(os.path.expanduser(os.path.expandvars(value)))
    return agent, tuple(identity_files)


def _resolve_identity_agent(agent: str | None, default_agent: str | None) -> str | None:
    if agent is None:
        return default_agent
    if not agent or agent == "none":
        return None
    if agent in ("SSH_AUTH_SOCK", "$SSH_AUTH_SOCK", "${SSH_AUTH_SOCK}"):
        return default_agent
    return os.path.expanduser(os.path.expandvars(agent))


def group_hosts(hosts: Iterable[str]) -> list[list[str]]:
    default_agent = os.environ.get("SSH_AUTH_SOCK")
    grouped: dict[tuple[str, object], list[str]] = {}
    for host in hosts:
        config = _ssh_config(host)
        if config is None:
            key = ("host", host)
        else:
            agent, identity_files = config
            resolved_agent = _resolve_identity_agent(agent, default_agent)
            key = (
                ("agent", resolved_agent)
                if resolved_agent
                else ("files", identity_files)
            )
        grouped.setdefault(key, []).append(host)
    return list(grouped.values())


def prepare_host(host: str) -> bool:
    return _check_host(host, batch_mode=False)


def _probe_host(host: str) -> bool:
    return _check_host(host, batch_mode=True)


def probe_hosts(hosts: Iterable[str]) -> list[bool]:
    with ThreadPoolExecutor() as executor:
        return list(executor.map(_probe_host, hosts))


def ssh_command(*args: str, persistent_ssh: bool | None = None, interactive: bool = False) -> tuple[str, ...]:
    if persistent_ssh is None:
        persistent_ssh = load_persistent_ssh()
    if interactive:
        multiplex = INTERACTIVE_OPTIONS if persistent_ssh else ()
        persist = ()
    else:
        multiplex = MULTIPLEX_OPTIONS if persistent_ssh else ()
        persist = PERSIST_OPTIONS if persistent_ssh else ()
    return ("ssh", *SSH_OPTIONS, *multiplex, *persist, *args)


INNER_TMUX = f"tmux -L {shlex.quote(INNER_SERVER_SOCKET)}"


def _inner_server_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("TMUX", None)
    return env


def _install_overlay(target: Target) -> None:
    _run("overlay", target, ssh_command(target.host or "", _SSH_INSTALL_OVERLAY), input=OVERLAY_FILE.read_text())


def _overlay_source(target: Target) -> str:
    return REMOTE_OVERLAY_PATH if target.kind == "ssh" else shlex.quote(str(OVERLAY_FILE))


def attach_command(target: Target, *, overlay: bool | None = None) -> str:
    overlay = load_tmux_config_overlay() if overlay is None else overlay
    if overlay and target.kind == "ssh":
        _install_overlay(target)
    new_session = f"{INNER_TMUX} -T clipboard new-session -A -s {shlex.quote(target.session)}"
    if overlay:
        new_session += f" \\; source-file {_overlay_source(target)}"
    if target.kind == "local":
        return f"env -u TMUX {new_session}"
    return shlex.join(ssh_command("-t", target.host or "", new_session, interactive=True))


def pane_attach_command(pane_target: PaneTarget, *, overlay: bool | None = None) -> str:
    overlay = load_tmux_config_overlay() if overlay is None else overlay
    target = pane_target.target
    if overlay and target.kind == "ssh":
        _install_overlay(target)
    parts = []
    if overlay:
        parts.append(f"source-file {_overlay_source(target)}")
    parts += (
        f"select-window -t {shlex.quote(target.session + ':' + pane_target.window_id)}",
        f"select-pane -t {shlex.quote(pane_target.pane_id)}",
        f"attach-session -t {shlex.quote(target.session)}",
    )
    command = f"tmux -S {shlex.quote(pane_target.socket_path)} " + " \\; ".join(parts)
    if target.kind == "local":
        return f"env -u TMUX {command}"
    return shlex.join(ssh_command("-t", target.host or "", command, interactive=True))


def _run(operation: str, target: Target, command: tuple[str, ...], **kwargs: object) -> str:
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=10, **kwargs)
    except subprocess.TimeoutExpired:
        raise SystemExit(f"{operation} {target.format()} timed out") from None
    except subprocess.CalledProcessError as error:
        reason = (error.stderr or "").strip() or f"exit status {error.returncode}"
        raise SystemExit(f"{operation} {target.format()} failed: {reason}") from None
    except OSError as error:
        reason = error.strerror or str(error)
        raise SystemExit(f"{operation} {target.format()} failed: {reason}") from None
    return result.stdout or ""


def create(target: Target, *, overlay: bool | None = None) -> None:
    overlay = load_tmux_config_overlay() if overlay is None else overlay
    if target.kind == "local":
        command = ("tmux", "-L", INNER_SERVER_SOCKET, "new-session", "-d", "-s", target.session)
        _run("create", target, command, env=_inner_server_env())
        if overlay:
            # Best effort: tmux pre-3.4 rejects newer overlay options, and
            # timeout/OSError here must not fail the create itself either.
            with suppress(subprocess.TimeoutExpired, OSError):
                subprocess.run(
                    ("tmux", "-L", INNER_SERVER_SOCKET, "source-file", str(OVERLAY_FILE)),
                    check=False,
                    capture_output=True,
                    timeout=10,
                    env=_inner_server_env(),
                )
    else:
        if overlay:
            _install_overlay(target)
        command = f"{INNER_TMUX} new-session -d -s {shlex.quote(target.session)}"
        if overlay:
            command += f" && {{ {INNER_TMUX} source-file {REMOTE_OVERLAY_PATH} || true; }}"
        _run("create", target, ssh_command(target.host or "", command))


def kill(target: Target) -> None:
    if target.kind == "local":
        _run("kill", target, ("tmux", "-L", INNER_SERVER_SOCKET, "kill-session", "-t", target.session), env=_inner_server_env())
    else:
        _run("kill", target, ssh_command(target.host or "", f"{INNER_TMUX} kill-session -t {shlex.quote(target.session)}"))


def _pane_process_info(pane_target: PaneTarget) -> tuple[int, str]:
    target = pane_target.target
    output = _run(
        "kill agent",
        target,
        (
            "tmux", "-S", pane_target.socket_path, "display-message", "-p",
            "-t", pane_target.pane_id, "#{pane_pid}\t#{pane_tty}",
        ),
        env=_inner_server_env(),
    )
    fields = output.strip().split("\t", 1)
    if len(fields) != 2 or not fields[1]:
        raise SystemExit(f"kill agent {target.format()} failed: invalid pane information")
    try:
        pane_pid = int(fields[0])
    except ValueError:
        raise SystemExit(f"kill agent {target.format()} failed: invalid pane PID") from None
    if pane_pid <= 0:
        raise SystemExit(f"kill agent {target.format()} failed: invalid pane PID")
    return pane_pid, fields[1]


def _foreground_pgid(target: Target, pane_pid: int) -> int:
    output = _run(
        "kill agent",
        target,
        ("ps", "-o", "tpgid=", "-p", str(pane_pid)),
        env=_inner_server_env(),
    )
    try:
        return int(output.strip())
    except ValueError:
        raise SystemExit(f"kill agent {target.format()} failed: invalid foreground process group") from None


def _kill_agent_local(pane_target: PaneTarget) -> None:
    target = pane_target.target
    pane_pid, _ = _pane_process_info(pane_target)
    try:
        foreground_pgid = _foreground_pgid(target, pane_pid)
        pane_pgid = os.getpgid(pane_pid)
    except OSError as error:
        reason = error.strerror or str(error)
        raise SystemExit(f"kill agent {target.format()} failed: {reason}") from None
    if foreground_pgid <= 0 or foreground_pgid == pane_pgid:
        raise SystemExit(f"kill agent {target.format()} failed: no foreground process")
    try:
        os.killpg(foreground_pgid, signal.SIGTERM)
    except OSError as error:
        reason = error.strerror or str(error)
        raise SystemExit(f"kill agent {target.format()} failed: {reason}") from None


def kill_agent(pane_target: PaneTarget) -> None:
    target = pane_target.target
    if target.kind == "local":
        _kill_agent_local(pane_target)
        return
    command = (
        "python3 -c "
        f"{shlex.quote(_KILL_AGENT_HELPER)} -- "
        f"{shlex.quote(pane_target.socket_path)} {shlex.quote(pane_target.pane_id)}"
    )
    _run("kill agent", target, ssh_command(target.host or "", command))


def rename(target: Target, new_name: str) -> Target:
    renamed = Target(target.kind, new_name, target.host)
    if target.kind == "local":
        _run(
            "rename",
            target,
            ("tmux", "-L", INNER_SERVER_SOCKET, "rename-session", "-t", target.session, renamed.session),
            env=_inner_server_env(),
        )
    else:
        _run(
            "rename",
            target,
            ssh_command(
                target.host or "",
                f"{INNER_TMUX} rename-session -t {shlex.quote(target.session)} {shlex.quote(renamed.session)}",
            ),
        )
    return renamed
