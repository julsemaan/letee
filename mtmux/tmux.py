from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

SOCKET = "mtmux"
SESSION = "mtmux"
WINDOW = "cockpit"


def host_environment(*, without_tmux: bool = False) -> dict[str, str]:
    env = os.environ.copy()
    if getattr(sys, "frozen", False):
        original = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if original is None:
            env.pop("LD_LIBRARY_PATH", None)
        else:
            env["LD_LIBRARY_PATH"] = original
    if without_tmux:
        env.pop("TMUX", None)
    return env


def tmux(*args: str, check: bool = True, capture: bool = False, config: Path | None = None) -> subprocess.CompletedProcess[str]:
    cmd = ["tmux", "-L", SOCKET]
    if config is not None:
        cmd += ["-f", str(config)]
    cmd += list(args)
    return subprocess.run(cmd, text=True, capture_output=capture, check=check, env=host_environment())


def out(*args: str, check: bool = True) -> str:
    proc = tmux(*args, check=check, capture=True)
    return proc.stdout.strip()


def has_pane(pane: str) -> bool:
    return tmux("has-session", "-t", pane, check=False).returncode == 0
