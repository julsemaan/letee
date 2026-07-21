from __future__ import annotations

import os
import shlex
import subprocess

from .config import load_persistent_ssh
from .names import PaneTarget, Target


PERSISTENT_SSH_OPTIONS = (
    "-o", "ControlMaster=auto",
    "-o", "ControlPersist=10m",
    "-o", "ControlPath=~/.ssh/mtmux-%C",
)


def ssh_command(*args: str, persistent_ssh: bool | None = None) -> tuple[str, ...]:
    if persistent_ssh is None:
        persistent_ssh = load_persistent_ssh()
    return ("ssh", *(PERSISTENT_SSH_OPTIONS if persistent_ssh else ()), *args)


def _default_server_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("TMUX", None)
    return env


def attach_command(target: Target) -> str:
    session = shlex.quote(target.session)
    if target.kind == "local":
        return f"env -u TMUX tmux -T clipboard new-session -A -s {session}"
    return shlex.join(ssh_command("-t", target.host or "", f"tmux -T clipboard new-session -A -s {session}"))


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
    return shlex.join(ssh_command("-t", target.host or "", command))


def _run(operation: str, target: Target, command: tuple[str, ...], **kwargs: object) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, **kwargs)
    except subprocess.CalledProcessError as error:
        reason = (error.stderr or "").strip() or f"exit status {error.returncode}"
        raise SystemExit(f"{operation} {target.format()} failed: {reason}") from None


def create(target: Target) -> None:
    if target.kind == "local":
        _run("create", target, ("tmux", "new-session", "-Ad", "-s", target.session), env=_default_server_env())
    else:
        _run("create", target, ssh_command(target.host or "", f"tmux new-session -Ad -s {shlex.quote(target.session)}"))


def kill(target: Target) -> None:
    if target.kind == "local":
        _run("kill", target, ("tmux", "kill-session", "-t", target.session), env=_default_server_env())
    else:
        _run("kill", target, ssh_command(target.host or "", f"tmux kill-session -t {shlex.quote(target.session)}"))
