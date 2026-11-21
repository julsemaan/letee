from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
import fcntl
from functools import wraps
import json
import os
from pathlib import Path
import shlex
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

from . import diagnostics, tmux
from .config import load_persistent_ssh, load_timing, load_tmux_config_overlay, remote_tmux_cache_path
from .names import INNER_SERVER_SOCKET, SESSION_ID_RE, PaneTarget, Target, WindowTarget, validate_host, validate_session_name


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

stage = "arguments"
category = "success"
query_count = 0
signal_count = 0
try:
    tmux_path, socket_path, pane_id, timeout = sys.argv[2:]
    timeout = float(timeout)
    stage = "pane_lookup"
    query_count += 1
    result = subprocess.run(
        (
            tmux_path,
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
        timeout=timeout,
        env={key: value for key, value in os.environ.items() if key != "TMUX"},
    )
    pane_pid, pane_tty = result.stdout.strip().split("\t", 1)
    pane_pid = int(pane_pid)
    if pane_pid <= 0:
        raise ValueError("invalid pane PID")
    stage = "foreground_lookup"
    query_count += 1
    process = subprocess.run(
        ("ps", "-o", "tpgid=", "-p", str(pane_pid)),
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={key: value for key, value in os.environ.items() if key != "TMUX"},
    )
    foreground_pgid = int(process.stdout.strip())
    stage = "pane_group"
    pane_pgid = os.getpgid(pane_pid)
    stage = "foreground_validate"
    if foreground_pgid <= 0 or foreground_pgid == pane_pgid:
        raise RuntimeError("no foreground process")
    stage = "signal"
    signal_count += 1
    os.killpg(foreground_pgid, signal.SIGTERM)
    stage = "done"
except subprocess.CalledProcessError as error:
    category = "CalledProcessError"
    print((error.stderr or "").strip() or str(error), file=sys.stderr)
    raise SystemExit(1)
except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
    category = ("TimeoutExpired" if isinstance(error, subprocess.TimeoutExpired) else
                "OSError" if isinstance(error, OSError) else
                "RuntimeError" if isinstance(error, RuntimeError) else
                "ValueError" if isinstance(error, ValueError) else "SubprocessError")
    print(error, file=sys.stderr)
    raise SystemExit(1)
finally:
    print("LETEE_DIAG kill_agent", stage, category, query_count, signal_count)'''


def _session_operation(name: str):
    """Bind only validated identifiers, never commands, socket paths, or keys."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            subject = args[0] if args else kwargs[function.__code__.co_varnames[0]]
            fields = {}
            if isinstance(subject, PaneTarget):
                fields.update(pane_id=subject.pane_id, window_id=subject.window_id)
                target = subject.target
            else:
                target = subject
            if isinstance(target, Target):
                fields.update(target=target.format(), target_kind=target.kind, host=target.host)
            else:
                fields["host"] = validate_host(target)
            with diagnostics.get_diagnostics().context(**fields):
                with diagnostics.operation(name, **fields):
                    return function(*args, **kwargs)
        return wrapped
    return decorate


@diagnostics.operation("sessions.agent_probe")
def _agent_reachable(socket_path: str) -> bool | None:
    env = os.environ.copy()
    env["SSH_AUTH_SOCK"] = socket_path
    try:
        result = subprocess.run(
            ("ssh-add", "-l"),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=load_timing().local_command_timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        diagnostics.error(error, handling="fallback", stage="agent_probe")
        return None
    diagnostics.log("sessions.agent_probe.result", returncode=result.returncode)
    if result.returncode in (0, 1):
        return True
    if result.returncode == 2:
        return False
    return None


def _is_unix_socket(path: Path) -> bool:
    try:
        return stat.S_ISSOCK(os.lstat(path).st_mode)
    except OSError as error:
        diagnostics.error(error, handling="fallback", stage="agent_socket_stat")
        return False


@diagnostics.operation("sessions.agent_socket_probe")
def _is_stale_unix_socket(path: Path) -> bool:
    if not _is_unix_socket(path):
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.1)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError) as error:
        diagnostics.error(error, handling="fallback", stage="agent_socket_probe", decision="stale")
        return True
    except OSError as error:
        diagnostics.error(error, handling="fallback", stage="agent_socket_probe", decision="preserve")
        return False
    finally:
        probe.close()
    return False


@contextmanager
def _fallback_agent_lock(socket_path: Path):
    lock_path = socket_path.with_name(socket_path.name + ".lock")
    lock = None
    with diagnostics.operation("sessions.agent_lock") as span:
        try:
            lock = lock_path.open("a+")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except OSError as error:
            diagnostics.error(error, handling="fallback", stage="agent_lock", decision="continue_unlocked")
            span.outcome = "continue_unlocked"
            if lock is not None:
                lock.close()
            lock = None
    if lock is None:
        yield
        return
    try:
        yield
    finally:
        with diagnostics.operation("sessions.agent_unlock"):
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            finally:
                lock.close()


@diagnostics.operation("sessions.agent_wait")
def _wait_for_agent(socket_path: Path) -> bool:
    for attempt in range(_AGENT_START_ATTEMPTS):
        reachable = _agent_reachable(str(socket_path))
        diagnostics.log("sessions.agent_wait.attempt", attempt=attempt + 1, reachable=reachable)
        if reachable is True:
            return True
        if reachable is None:
            diagnostics.log("sessions.agent_wait.stopped", reason="probe_failed")
            return False
        if os.path.lexists(socket_path) and not _is_unix_socket(socket_path):
            diagnostics.log("sessions.agent_wait.stopped", reason="non_socket")
            return False
        if attempt + 1 < _AGENT_START_ATTEMPTS:
            time.sleep(_AGENT_START_DELAY)
    diagnostics.log("sessions.agent_wait.stopped", reason="attempts_exhausted", attempts=_AGENT_START_ATTEMPTS)
    return False


@diagnostics.operation("sessions.agent_setup")
def ensure_ssh_agent() -> str | None:
    inherited = os.environ.get("SSH_AUTH_SOCK")
    if inherited and _agent_reachable(inherited) is True:
        diagnostics.log("sessions.agent_setup.result", source="inherited", outcome="reused")
        return inherited

    socket_path = Path(FALLBACK_AGENT_SOCKET).expanduser()
    try:
        socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        diagnostics.error(error, handling="fallback", stage="agent_directory")
        return None

    with _fallback_agent_lock(socket_path):
        reachable = _agent_reachable(str(socket_path))
        if reachable is True:
            os.environ["SSH_AUTH_SOCK"] = str(socket_path)
            diagnostics.log("sessions.agent_setup.result", source="fallback", outcome="reused")
            return str(socket_path)
        if reachable is None:
            diagnostics.log("sessions.agent_setup.result", outcome="unavailable", reason="probe_failed")
            return None
        if os.path.lexists(socket_path):
            if not _is_stale_unix_socket(socket_path):
                diagnostics.log("sessions.agent_setup.result", outcome="unavailable", reason="socket_preserved")
                return None
            try:
                socket_path.unlink()
                diagnostics.log("sessions.agent_socket.removed", reason="stale")
            except OSError as error:
                diagnostics.error(error, handling="fallback", stage="agent_socket_remove")
                return None
        with diagnostics.operation("sessions.agent_start") as span:
            try:
                result = subprocess.run(
                    ("ssh-agent", "-s", "-a", str(socket_path)),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=load_timing().local_command_timeout,
                )
                span.fields["returncode"] = result.returncode
                span.outcome = "started" if result.returncode == 0 else "probe_after_start"
            except (OSError, subprocess.SubprocessError) as error:
                diagnostics.error(error, handling="retry", stage="agent_start", decision="probe_after_start")
                span.outcome = "probe_after_start"
        if _wait_for_agent(socket_path):
            os.environ["SSH_AUTH_SOCK"] = str(socket_path)
            diagnostics.log("sessions.agent_setup.result", source="fallback", outcome="started")
            return str(socket_path)
    diagnostics.log("sessions.agent_setup.result", outcome="unavailable", reason="wait_failed")
    return None


@_session_operation("sessions.host_check")
def _check_host(host: str, *, batch_mode: bool) -> bool:
    command = ssh_command(
        "-o", f"BatchMode={'yes' if batch_mode else 'no'}",
        "-o", "ControlMaster=no",
        "-o", "ControlPath=none",
        validate_host(host),
        "true",
        persistent_ssh=False,
    )
    timing = load_timing()
    kwargs: dict[str, object] = {
        "check": False,
        "timeout": timing.remote_command_timeout if batch_mode else timing.ssh_auth_timeout,
    }
    if batch_mode:
        env = os.environ.copy()
        env["SSH_ASKPASS_REQUIRE"] = "never"
        kwargs.update(
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    with diagnostics.operation("sessions.authentication", batch_mode=batch_mode, timeout=kwargs["timeout"]) as span:
        try:
            result = subprocess.run(command, **kwargs)
        except (OSError, subprocess.SubprocessError) as error:
            diagnostics.error(error, handling="fallback", stage="authentication")
            span.outcome = "unavailable"
            return False
        span.fields["returncode"] = result.returncode
        span.outcome = "authenticated" if result.returncode == 0 else "unavailable"
        return result.returncode == 0


@_session_operation("sessions.ssh_config")
def _ssh_config(host: str) -> tuple[str | None, tuple[str, ...]] | None:
    try:
        result = subprocess.run(
            ("ssh", "-G", validate_host(host)),
            check=False,
            capture_output=True,
            text=True,
            timeout=load_timing().local_command_timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        diagnostics.error(error, handling="fallback", stage="ssh_config")
        return None
    diagnostics.log("sessions.ssh_config.result", returncode=result.returncode)
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


@diagnostics.operation("sessions.group_hosts")
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
    diagnostics.log("sessions.group_hosts.result", group_count=len(grouped), host_count=sum(map(len, grouped.values())))
    return list(grouped.values())


@diagnostics.operation("sessions.reset_ssh_masters")
def reset_ssh_masters(hosts: Iterable[str]) -> None:
    """Close managed transports before a fresh cockpit starts using SSH."""
    if not load_persistent_ssh():
        return
    for host in dict.fromkeys(validate_host(host) for host in hosts):
        with diagnostics.operation("sessions.reset_ssh_master", host=host) as operation:
            try:
                result = subprocess.run(
                    ("ssh", *INTERACTIVE_OPTIONS, "-O", "exit", host),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=load_timing().local_command_timeout,
                    env={**os.environ, "LC_ALL": "C"},
                )
                operation.fields["returncode"] = result.returncode
                if result.returncode == 0:
                    operation.outcome = "closed"
                    continue
                message = result.stderr.strip()
                if message.startswith("Control socket connect(") and message.endswith(": No such file or directory"):
                    operation.outcome = "absent"
                    continue
                diagnostics.error(handling="ignored", stage="ssh_master_reset", host=host,
                                  returncode=result.returncode)
            except (OSError, subprocess.SubprocessError) as error:
                diagnostics.error(error, handling="ignored", stage="ssh_master_reset", host=host)
            operation.outcome = "failed"
            print(f"Warning: could not reset SSH master for {host}; SSH configuration changes may not apply.",
                  file=sys.stderr, flush=True)


def prepare_host(host: str) -> bool:
    return _check_host(host, batch_mode=False)


def _probe_host(host: str) -> bool:
    return _check_host(host, batch_mode=True)


@diagnostics.operation("sessions.probe_hosts")
def probe_hosts(hosts: Iterable[str]) -> list[bool]:
    with ThreadPoolExecutor() as executor:
        futures = [executor.submit(copy_context().run, _probe_host, host) for host in hosts]
        results = [future.result() for future in futures]
    diagnostics.log("sessions.probe_hosts.result", host_count=len(results), reachable_count=sum(results))
    return results


def ssh_command(*args: str, persistent_ssh: bool | None = None, interactive: bool = False, connect_timeout: int | None = None) -> tuple[str, ...]:
    if persistent_ssh is None:
        persistent_ssh = load_persistent_ssh()
    if interactive:
        multiplex = INTERACTIVE_OPTIONS if persistent_ssh else ()
        persist = ()
    else:
        multiplex = MULTIPLEX_OPTIONS if persistent_ssh else ()
        persist = PERSIST_OPTIONS if persistent_ssh else ()
    timing = load_timing()
    if connect_timeout is None:
        connect_timeout = timing.ssh_connect_timeout
    # Only fixed, non-sensitive options are inspected. Later options override defaults.
    effective = {"ControlMaster": "auto" if persistent_ssh else "config",
                 "ControlPath": "managed" if persistent_ssh else "config",
                 "ControlPersist": "10m" if persist else "config", "BatchMode": "config"}
    for index, value in enumerate(args[:-1]):
        if value == "-o":
            option, _, setting = args[index + 1].partition("=")
            allowed = {"ControlMaster": {"auto", "no", "yes"}, "ControlPath": {"none"},
                       "ControlPersist": {"no", "yes", "10m"}, "BatchMode": {"yes", "no"}}
            if option in allowed and setting in allowed[option]:
                effective[option] = "disabled" if option == "ControlPath" else setting
    if interactive and persistent_ssh:
        effective["ControlMaster"] = "no"
    diagnostics.log("sessions.ssh_settings", persistent_ssh=persistent_ssh, interactive=interactive,
                    connect_timeout=connect_timeout,
                    keepalive_interval=timing.ssh_keepalive_interval,
                    keepalive_count_max=timing.ssh_keepalive_count_max,
                    control_master=effective["ControlMaster"], control_path=effective["ControlPath"],
                    control_persist=effective["ControlPersist"], batch_mode=effective["BatchMode"])
    return (
        "ssh",
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", f"ServerAliveInterval={timing.ssh_keepalive_interval}",
        "-o", f"ServerAliveCountMax={timing.ssh_keepalive_count_max}",
        *SSH_OPTIONS, *multiplex, *persist, *args,
    )


INSTALL_URL = "https://raw.githubusercontent.com/julsemaan/letee/main/install.sh"
TMUX_PATH_MARKER = "LETEE_TMUX="
# Non-interactive SSH often lacks ~/.local/bin on PATH; a login shell has it.
_REMOTE_TMUX_PATH = 'exec "${SHELL:-/bin/sh}" -lc "letee tmux-path"'


def _load_remote_tmux() -> dict[str, str]:
    try:
        return json.loads(remote_tmux_cache_path().read_text())
    except FileNotFoundError:
        return {}


def _save_remote_tmux(cache: dict[str, str]) -> None:
    path = remote_tmux_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(cache))
    os.replace(temporary, path)


def forget_remote_tmux() -> None:
    remote_tmux_cache_path().unlink(missing_ok=True)


def cached_remote_tmux(host: str) -> str | None:
    """Return host's tmux executable if already known, without contacting it."""
    return _load_remote_tmux().get(host)


def remote_tmux(host: str, *, connect_timeout: int | None = None, timeout: float | None = None) -> str:
    """Return the tmux executable letee uses on host, asking its letee once."""
    host = validate_host(host)
    # Never log the remote path or output.
    with diagnostics.operation("sessions.remote_tmux", host=host) as span:
        if path := cached_remote_tmux(host):
            span.outcome = "cached"
            return path
        try:
            result = subprocess.run(
                ssh_command("-n", "-o", "BatchMode=yes", host, _REMOTE_TMUX_PATH, connect_timeout=connect_timeout),
                check=False, capture_output=True, text=True,
                timeout=load_timing().remote_command_timeout if timeout is None else timeout,
            )
        except subprocess.TimeoutExpired as error:
            diagnostics.error(error, handling="raise", stage="remote_tmux")
            span.outcome = "unreachable"
            raise SystemExit(f"{host}: timed out") from None
        except OSError as error:
            diagnostics.error(error, handling="raise", stage="remote_tmux")
            span.outcome = "unreachable"
            raise SystemExit(f"{host}: {error.strerror or error}") from None
        span.fields["returncode"] = result.returncode
        if result.returncode == 255:
            span.outcome = "unreachable"
            raise SystemExit(result.stderr.strip() or f"{host}: ssh exited 255")
        path = next((line.removeprefix(TMUX_PATH_MARKER) for line in reversed(result.stdout.splitlines())
                     if line.startswith(TMUX_PATH_MARKER)), "")
        if result.returncode != 0 or not path:
            span.outcome = "missing"
            raise SystemExit(f"letee is not installed on {host} (or not on its login PATH). "
                             f"Install it there with: curl -LsSf {INSTALL_URL} | sh")
        _save_remote_tmux(_load_remote_tmux() | {host: path})
        span.outcome = "resolved"
        return path


def inner_tmux(host: str | None) -> str:
    return tmux.tmux_executable() if host is None else remote_tmux(host)


def server_argv(tmux_path: str, *args: str) -> tuple[str, ...]:
    return (tmux_path, "-L", INNER_SERVER_SOCKET, *args)


def inner_argv(host: str | None, *args: str) -> tuple[str, ...]:
    return server_argv(inner_tmux(host), *args)


def socket_argv(host: str | None, socket_path: str, *args: str) -> tuple[str, ...]:
    return (inner_tmux(host), "-S", socket_path, *args)


def _bundle_dir(executable: str) -> str | None:
    """Inner servers start with their tmux's directory first on PATH, so panes
    and plugins get a matching client. Bare `tmux` has no bundle to expose."""
    return os.path.dirname(executable) if os.path.isabs(executable) else None


def shell_join(argv: tuple[str, ...]) -> str:
    """Join an inner tmux argv for a shell, with the bundle dir first on PATH."""
    directory = _bundle_dir(argv[0])
    prefix = f'PATH={shlex.quote(directory + ":")}"$PATH" ' if directory else ""
    return prefix + shlex.join(argv)


def bundle_env() -> dict[str, str]:
    """The current environment with the local tmux's bundle dir first on PATH."""
    env = os.environ.copy()
    if directory := _bundle_dir(tmux.tmux_executable()):
        env["PATH"] = os.pathsep.join(filter(None, (directory, env.get("PATH"))))
    return env


def _inner_server_env() -> dict[str, str]:
    env = bundle_env()
    env.pop("TMUX", None)
    return env


def _run_inner(operation: str, target: Target, argv: tuple[str, ...], **kwargs: object) -> str:
    if target.kind == "local":
        return _run(operation, target, argv, env=_inner_server_env(), **kwargs)
    return _run(operation, target, ssh_command(target.host or "", shell_join(argv)), **kwargs)


@_session_operation("sessions.overlay_install")
def _install_overlay(target: Target) -> None:
    _run("overlay", target, ssh_command(target.host or "", _SSH_INSTALL_OVERLAY), input=OVERLAY_FILE.read_text())


def _overlay_source(target: Target) -> str:
    return REMOTE_OVERLAY_PATH if target.kind == "ssh" else shlex.quote(str(OVERLAY_FILE))


def _remote_attach_command(target: Target, command: str, *, overlay: bool) -> str:
    if overlay:
        with diagnostics.operation("sessions.overlay_install"):
            # Only the installer reads the pipe; attachment keeps the terminal stdin.
            contents = shlex.quote(OVERLAY_FILE.read_text())
            command = f"printf %s {contents} | ( {_SSH_INSTALL_OVERLAY} ) && {command}"
    return shlex.join(ssh_command("-t", target.host or "", command, interactive=True))


@_session_operation("sessions.attach_command")
def attach_command(target: Target, *, overlay: bool | None = None, attach_only: bool = False) -> str:
    if not attach_only:
        validate_session_name(target.session)
    overlay = load_tmux_config_overlay() if overlay is None else overlay
    action = (f"attach-session -t {shlex.quote('=' + target.session)}" if attach_only
              else f"new-session -A -s {shlex.quote(target.session)}")
    command = f"{shell_join(inner_argv(target.host, '-T', 'clipboard'))} {action}"
    if overlay:
        command += f" \\; source-file {_overlay_source(target)}"
    if target.kind == "local":
        return f"env -u TMUX {command}"
    return _remote_attach_command(target, command, overlay=overlay)


@_session_operation("sessions.pane_attach_command")
def pane_attach_command(pane_target: PaneTarget, *, overlay: bool | None = None) -> str:
    overlay = load_tmux_config_overlay() if overlay is None else overlay
    target = pane_target.target
    parts = []
    if overlay:
        parts.append(f"source-file {_overlay_source(target)}")
    parts += (
        f"select-window -t {shlex.quote(target.session + ':' + pane_target.window_id)}",
        f"select-pane -t {shlex.quote(pane_target.pane_id)}",
        f"attach-session -t {shlex.quote(target.session)}",
    )
    command = f"{shell_join(socket_argv(target.host, pane_target.socket_path))} " + " \\; ".join(parts)
    if target.kind == "local":
        return f"env -u TMUX {command}"
    return _remote_attach_command(target, command, overlay=overlay)


def _existing_attach_command(
    target: Target, session_id: str, socket_path: str, *,
    window_id: str = "", pane_id: str = "", overlay: bool | None = None,
) -> str:
    if not SESSION_ID_RE.fullmatch(session_id):
        raise SystemExit(f"Invalid session ID: {session_id!r}")
    if not socket_path:
        raise SystemExit("Invalid socket path: must not be empty")
    base = socket_argv(target.host, socket_path)
    output = _run_inner("find", target, (*base, "list-panes", "-a", "-F", "#{session_id}\t#{window_id}\t#{pane_id}"))
    expected = (session_id, window_id, pane_id) if pane_id else (session_id, window_id) if window_id else (session_id,)
    if not any(len(fields := line.split("\t")) == 3 and tuple(fields[:len(expected)]) == expected
               for line in output.splitlines()):
        raise SystemExit(f"find {target.format()} failed: target no longer exists")

    overlay = load_tmux_config_overlay() if overlay is None else overlay
    prefix = shell_join(base)
    if target.kind == "local":
        prefix = f"env -u TMUX {prefix}"
    parts = []
    if overlay:
        parts.append(f"source-file {_overlay_source(target)}")
    if window_id:
        parts.append(f"select-window -t {shlex.quote(session_id + ':' + window_id)}")
    if pane_id:
        parts.append(f"select-pane -t {shlex.quote(session_id + ':' + window_id + '.' + pane_id)}")
    parts.append(f"attach-session -t {shlex.quote(session_id)}")
    # Separate tmux invocations stop on failed selection, unlike tmux command queues.
    command = " && ".join(f"{prefix} {part}" for part in parts)
    if target.kind == "local":
        return command
    return _remote_attach_command(target, command, overlay=overlay)


def validate_destination(
    target: Target, session_id: str, socket_path: str, *, window_id: str = "", pane_id: str = "",
) -> None:
    """Check live identity and name before publishing prepared navigation."""
    output = _run_inner("find", target, socket_argv(
        target.host, socket_path, "list-panes", "-a", "-F",
        "#{session_id}\t#{window_id}\t#{pane_id}\t#{session_name}",
    ))
    for line in output.splitlines():
        fields = line.split("\t")
        if (len(fields) == 4 and fields[0] == session_id and fields[3] == target.session
                and (not window_id or fields[1] == window_id)
                and (not pane_id or fields[2] == pane_id)):
            return
    raise SystemExit("Destination changed or is unavailable")


def existing_attach_command(
    target: Target, session_id: str, socket_path: str, *, overlay: bool | None = None,
) -> str:
    """Prepare attachment to an existing stable session ID without creating it."""
    return _existing_attach_command(target, session_id, socket_path, overlay=overlay)


def window_attach_command(window: WindowTarget, *, overlay: bool | None = None) -> str:
    """Prepare an exact window selection without changing its selected pane."""
    return _existing_attach_command(
        window.target, window.session_id, window.socket_path,
        window_id=window.window_id, overlay=overlay,
    )


def existing_pane_attach_command(pane: PaneTarget, *, overlay: bool | None = None) -> str:
    """Prepare attachment only if the pane still belongs to this session and window."""
    return _existing_attach_command(
        pane.target, pane.session_id, pane.socket_path,
        window_id=pane.window_id, pane_id=pane.pane_id, overlay=overlay,
    )


def _remote_metadata(operation: str, target: Target, result) -> None:
    if target.kind != "ssh" or operation not in ("kill agent", "create"):
        return
    found = False
    for stream in (getattr(result, "stdout", None), getattr(result, "stderr", None)):
        if not isinstance(stream, str):
            continue
        # Bound both parsing work and accepted fields. Never log remote text.
        for line in stream[-4096:].splitlines()[-16:]:
            match = re.fullmatch(
                r"LETEE_DIAG kill_agent (arguments|pane_lookup|foreground_lookup|pane_group|foreground_validate|signal|done) "
                r"(success|CalledProcessError|TimeoutExpired|OSError|RuntimeError|ValueError|SubprocessError) ([0-2]) ([01])",
                line,
            )
            if operation == "kill agent" and match and not found:
                stage, category, queries, signals = match.groups()
                diagnostics.log("sessions.kill_agent.remote", stage=stage, category=category,
                                query_count=int(queries), signal_count=int(signals))
                found = True
            match = re.fullmatch(r"LETEE_DIAG overlay ([0-9]{1,3})", line)
            if operation == "create" and match and not found and int(match[1]) <= 255:
                code = int(match[1])
                diagnostics.log("sessions.overlay_source.remote", returncode=code,
                                outcome="completed" if code == 0 else "ignored")
                found = True
    if operation == "kill agent" and not found:
        diagnostics.log("sessions.kill_agent.remote", category=(
            "python_unavailable" if getattr(result, "returncode", None) == 127 else "metadata_unavailable"))


def _run(
    operation: str, target: Target, command: tuple[str, ...], *, timeout: float | None = None, **kwargs: object,
) -> str:
    with diagnostics.operation("sessions.command", operation=operation, target_kind=target.kind) as span:
        try:
            if timeout is None:
                timing = load_timing()
                timeout = timing.remote_command_timeout if target.kind == "ssh" else timing.local_command_timeout
            span.fields["timeout"] = timeout
            result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout, **kwargs)
        except subprocess.TimeoutExpired as error:
            _remote_metadata(operation, target, error)
            diagnostics.error(error, handling="raise", stage="command", operation=operation, target_kind=target.kind)
            raise SystemExit(f"{operation} {target.format()} timed out") from error
        except subprocess.CalledProcessError as error:
            _remote_metadata(operation, target, error)
            diagnostics.error(error, handling="raise", stage="command", operation=operation, target_kind=target.kind)
            reason = (error.stderr or "").strip() or f"exit status {error.returncode}"
            raise SystemExit(f"{operation} {target.format()} failed: {reason}") from error
        except OSError as error:
            diagnostics.error(error, handling="raise", stage="command", operation=operation, target_kind=target.kind)
            reason = error.strerror or str(error)
            raise SystemExit(f"{operation} {target.format()} failed: {reason}") from None
        span.fields["returncode"] = result.returncode
        _remote_metadata(operation, target, result)
        return result.stdout or ""


@_session_operation("sessions.create")
def create(target: Target, *, overlay: bool | None = None) -> None:
    validate_session_name(target.session)
    overlay = load_tmux_config_overlay() if overlay is None else overlay
    command = inner_argv(target.host, "new-session", "-d", "-s", target.session)
    if target.kind == "local":
        _run_inner("create", target, command)
        if overlay:
            # Best effort: tmux pre-3.4 rejects newer overlay options, and
            # timeout/OSError here must not fail the create itself either.
            with diagnostics.operation("sessions.overlay_source", target_kind="local") as span:
                try:
                    result = subprocess.run(
                        inner_argv(None, "source-file", str(OVERLAY_FILE)),
                        check=False,
                        capture_output=True,
                        timeout=load_timing().local_command_timeout,
                        env=_inner_server_env(),
                    )
                    span.fields["returncode"] = result.returncode
                    span.outcome = "completed" if result.returncode == 0 else "ignored"
                except (subprocess.TimeoutExpired, OSError) as error:
                    diagnostics.error(error, handling="ignore", stage="overlay_source", target_kind="local")
                    span.outcome = "ignored"
    else:
        if overlay:
            _install_overlay(target)
        shell = shell_join(command)
        if overlay:
            shell += (f" && {{ {shell_join(inner_argv(target.host, 'source-file'))} {REMOTE_OVERLAY_PATH}; "
                      "letee_overlay_rc=$?; printf 'LETEE_DIAG overlay %s\\n' \"$letee_overlay_rc\"; true; }")
        _run("create", target, ssh_command(target.host or "", shell))


def require_existing(target: Target) -> None:
    """Reject missing legacy destinations before navigation changes state."""
    _run_inner("find", target, inner_argv(target.host, "has-session", "-t", "=" + target.session))


def ensure_created(target: Target, *, overlay: bool | None = None) -> None:
    """Create a detached session, or accept an exact, verified duplicate.

    Reuse create's overlay behavior. Only duplicate-session failures are
    recoverable; connection, timeout, and overlay failures still propagate.
    """
    try:
        create(target, overlay=overlay)
    except SystemExit as error:
        cause = error.__cause__
        if (
            not isinstance(cause, subprocess.CalledProcessError)
            or cause.returncode != 1
            or (cause.stderr or "").strip() != f"duplicate session: {target.session}"
            or str(error) != f"create {target.format()} failed: duplicate session: {target.session}"
        ):
            raise
        # Another creator may have won the race. Never use -A here: it can
        # attach to an existing session even when combined with -d.
        try:
            _run_inner("create", target, inner_argv(target.host, "has-session", "-t", "=" + target.session))
        except SystemExit:
            raise error


@_session_operation("sessions.kill")
def kill(target: Target) -> None:
    _run_inner("kill", target, inner_argv(target.host, "kill-session", "-t", target.session))


@_session_operation("sessions.pane_process_info")
def _pane_process_info(pane_target: PaneTarget) -> tuple[int, str]:
    target = pane_target.target
    output = _run_inner("kill agent", target, socket_argv(
        target.host, pane_target.socket_path, "display-message", "-p",
        "-t", pane_target.pane_id, "#{pane_pid}\t#{pane_tty}",
    ))
    fields = output.strip().split("\t", 1)
    if len(fields) != 2 or not fields[1]:
        diagnostics.log("sessions.kill_agent.rejected", reason="invalid_pane_information")
        raise SystemExit(f"kill agent {target.format()} failed: invalid pane information")
    try:
        pane_pid = int(fields[0])
    except ValueError as error:
        diagnostics.error(error, handling="raise", stage="pane_pid_parse")
        raise SystemExit(f"kill agent {target.format()} failed: invalid pane PID") from None
    if pane_pid <= 0:
        diagnostics.log("sessions.kill_agent.rejected", reason="invalid_pane_pid")
        raise SystemExit(f"kill agent {target.format()} failed: invalid pane PID")
    return pane_pid, fields[1]


@_session_operation("sessions.foreground_pgid")
def _foreground_pgid(target: Target, pane_pid: int) -> int:
    output = _run(
        "kill agent",
        target,
        ("ps", "-o", "tpgid=", "-p", str(pane_pid)),
        env=_inner_server_env(),
    )
    try:
        return int(output.strip())
    except ValueError as error:
        diagnostics.error(error, handling="raise", stage="foreground_pgid_parse")
        raise SystemExit(f"kill agent {target.format()} failed: invalid foreground process group") from None


def _kill_agent_local(pane_target: PaneTarget) -> None:
    target = pane_target.target
    pane_pid, _ = _pane_process_info(pane_target)
    try:
        foreground_pgid = _foreground_pgid(target, pane_pid)
        pane_pgid = os.getpgid(pane_pid)
    except OSError as error:
        diagnostics.error(error, handling="raise", stage="pane_process_group")
        reason = error.strerror or str(error)
        raise SystemExit(f"kill agent {target.format()} failed: {reason}") from None
    if foreground_pgid <= 0 or foreground_pgid == pane_pgid:
        diagnostics.log("sessions.kill_agent.rejected", reason="no_foreground_process")
        raise SystemExit(f"kill agent {target.format()} failed: no foreground process")
    try:
        os.killpg(foreground_pgid, signal.SIGTERM)
    except OSError as error:
        diagnostics.error(error, handling="raise", stage="agent_signal")
        reason = error.strerror or str(error)
        raise SystemExit(f"kill agent {target.format()} failed: {reason}") from None


@_session_operation("sessions.kill_agent")
def kill_agent(pane_target: PaneTarget) -> None:
    target = pane_target.target
    if target.kind == "local":
        _kill_agent_local(pane_target)
        return
    timing = load_timing()
    command = (
        "python3 -c "
        f"{shlex.quote(_KILL_AGENT_HELPER)} -- {shlex.quote(remote_tmux(target.host or ''))} "
        f"{shlex.quote(pane_target.socket_path)} {shlex.quote(pane_target.pane_id)} "
        f"{timing.local_command_timeout}"
    )
    _run(
        "kill agent", target, ssh_command(target.host or "", command),
        timeout=timing.remote_command_timeout + 2 * timing.local_command_timeout,
    )


@_session_operation("sessions.rename")
def rename(target: Target, new_name: str) -> Target:
    validate_session_name(new_name)
    renamed = Target(target.kind, new_name, target.host)
    _run_inner("rename", target, inner_argv(
        target.host, "rename-session", "-t", target.session, "--", renamed.session,
    ))
    return renamed
