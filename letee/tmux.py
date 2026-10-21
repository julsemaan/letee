from __future__ import annotations

from pathlib import Path
import os
import platform
import shlex
import stat
import subprocess

from . import diagnostics
from .config import load_timing
from .names import DEFAULT_SERVER, normalize_server, server_socket

_DEFAULT_TIMEOUT = object()
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
        except (IndexError, ValueError) as exc:
            diagnostics.error(exc, reason="macos_version_unavailable", handling="fallback")
            return None
        if major < 15:
            diagnostics.log("tmux.bundle_fallback", reason="unsupported_macos_version")
            return None
        family = "macos"
    else:
        diagnostics.log("tmux.bundle_fallback", reason="unsupported_platform")
        return None
    architecture = _ARCHITECTURES.get(platform.machine().lower())
    if architecture is None:
        diagnostics.log("tmux.bundle_fallback", reason="unsupported_architecture")
    return f"{family}-{architecture}" if architecture else None


def bundled_tmux_path() -> Path | None:
    directory = _platform_directory()
    if directory is None:
        return None
    path = VENDOR_ROOT / directory / "tmux"
    try:
        mode = os.lstat(path).st_mode
    except OSError as exc:
        diagnostics.error(exc, handling="fallback", reason="bundle_stat_failed")
        return None
    if stat.S_ISREG(mode) and os.access(path, os.X_OK):
        return path
    diagnostics.error(handling="fallback", reason="bundle_not_executable_file")
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
    timeout: float | None | object = _DEFAULT_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    if timeout is _DEFAULT_TIMEOUT:
        timeout = load_timing().local_command_timeout
    cmd = [tmux_executable(), "-L", SOCKET]
    if config is not None:
        cmd += ["-f", str(config)]
    cmd += list(args)
    with diagnostics.operation("tmux.command", timeout=timeout, checked=check) as operation:
        try:
            proc = subprocess.run(cmd, text=True, capture_output=capture, check=check, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            diagnostics.error(exc, handling="exit", reason="tmux_timeout")
            raise SystemExit(f"{shlex.join(cmd)} timed out after {timeout:g} seconds") from None
        except (OSError, subprocess.CalledProcessError) as exc:
            diagnostics.error(exc, handling="raise", reason="tmux_execution_failed")
            raise
        operation.fields["returncode"] = proc.returncode
        if proc.returncode:
            operation.outcome = "nonzero_exit"
            diagnostics.error(handling="return", reason="tmux_nonzero_exit", returncode=proc.returncode)
        return proc


def out(*args: str, check: bool = True) -> str:
    proc = tmux(*args, check=check, capture=True)
    return proc.stdout.strip()


def has_pane(pane: str) -> bool:
    return tmux("has-session", "-t", pane, check=False).returncode == 0
