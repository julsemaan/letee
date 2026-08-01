from __future__ import annotations

from pathlib import Path
import shlex
import subprocess

from .names import DEFAULT_SERVER, normalize_server, server_socket

SERVER = DEFAULT_SERVER
SOCKET = server_socket(SERVER)
SESSION = "letee"
WINDOW = "cockpit"


def set_server(server: str | None) -> str:
    global SERVER, SOCKET
    SERVER = normalize_server(server)
    SOCKET = server_socket(SERVER)
    return SERVER


def tmux(
    *args: str,
    check: bool = True,
    capture: bool = False,
    config: Path | None = None,
    timeout: float | None = 5,
) -> subprocess.CompletedProcess[str]:
    cmd = ["tmux", "-L", SOCKET]
    if config is not None:
        cmd += ["-f", str(config)]
    cmd += list(args)
    try:
        return subprocess.run(cmd, text=True, capture_output=capture, check=check, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SystemExit(f"{shlex.join(cmd)} timed out after {timeout:g} seconds") from None


def out(*args: str, check: bool = True) -> str:
    proc = tmux(*args, check=check, capture=True)
    return proc.stdout.strip()


def has_pane(pane: str) -> bool:
    return tmux("has-session", "-t", pane, check=False).returncode == 0
