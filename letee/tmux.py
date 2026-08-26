from __future__ import annotations

from pathlib import Path
import os
import platform
import shlex
import stat
import subprocess

from .names import DEFAULT_SERVER, normalize_server, server_socket

SERVER = DEFAULT_SERVER
SOCKET = server_socket(SERVER)
SESSION = "letee"
WINDOW = "cockpit"
VENDOR_ROOT = Path(__file__).resolve().parent / "_vendor" / "tmux"
_ARCHITECTURES = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}


def _platform_directory() -> str | None:
    system = platform.system().lower()
    if system == "linux":
        family = "linux"
    elif system == "darwin":
        version = platform.mac_ver()[0]
        try:
            major = int(version.split(".", 1)[0])
        except (IndexError, ValueError):
            return None
        if major < 15:
            return None
        family = "macos"
    else:
        return None
    architecture = _ARCHITECTURES.get(platform.machine().lower())
    return f"{family}-{architecture}" if architecture else None


def bundled_tmux_path() -> Path | None:
    directory = _platform_directory()
    if directory is None:
        return None
    path = VENDOR_ROOT / directory / "tmux"
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return None
    if stat.S_ISREG(mode) and mode & 0o111:
        return path
    return None


def tmux_executable() -> str:
    return str(path) if (path := bundled_tmux_path()) else "tmux"


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
    cmd = [tmux_executable(), "-L", SOCKET]
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
