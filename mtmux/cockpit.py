from __future__ import annotations

import os
import re
import shlex
import shutil
import sys

from .config import ensure_config, load_prefix, load_sidebar_width
from .names import Target, parse_target
from . import tmux


def _truecolor_enabled() -> bool:
    """Detect whether the host terminal supports truecolor (24-bit color)."""
    colorterm = os.environ.get("COLORTERM", "").lower()
    return colorterm in ("truecolor", "24bit")

def help_command(prefix: str) -> str:
    text = f"""mtmux cockpit

Navigation
  {prefix} s  focus/open sidebar
  {prefix} 1-9  switch session
  ?      open help
  q      quit sidebar only

Session actions
  Enter  switch session / open Add / create on host
  a      open grouped local/SSH Add picker
  r      remove selected session (session keeps running)
  K/J    move session up/down
  x      kill selected session (session keeps running)
  /      open Add picker and filter available sessions

Recovery
  {prefix} d  detach cockpit
  {prefix} s  restart/focus sidebar
  Esc    cancel prompts/filter

Examples
  /work  find available sessions matching work
  Enter  recreate a missing session or create on selected host line
"""
    return f"printf %s {shlex.quote(text)}; exec sh"


SIDEBAR = f"{shlex.quote(sys.executable)} -m mtmux sidebar"
FOCUS_SIDEBAR = f"{shlex.quote(sys.executable)} -m mtmux focus-sidebar"
TARGET = f"{tmux.SESSION}:{tmux.WINDOW}"
COCKPIT_OPTION = "@mtmux_cockpit"
SIDEBAR_PANE_OPTION = "@mtmux_sidebar_pane"
RIGHT_PANE_OPTION = "@mtmux_right_pane"
CURRENT_TARGET_OPTION = "@mtmux_current_target"
BELL_TARGET_OPTION = "@mtmux_bell_target"
NO_COCKPIT = "No valid mtmux cockpit. Run: mtmux cockpit"


def _option(name: str) -> str:
    return tmux.out("show-options", "-v", "-t", tmux.SESSION, name, check=False)


def _window_exists() -> bool:
    return tmux.tmux("has-session", "-t", TARGET, check=False).returncode == 0


def _valid() -> bool:
    if _option(COCKPIT_OPTION) != "1":
        return False
    left = _option(SIDEBAR_PANE_OPTION)
    right = _option(RIGHT_PANE_OPTION)
    return bool(left and right and tmux.has_pane(left) and tmux.has_pane(right))


def _set_markers(left: str, right: str) -> None:
    tmux.tmux("set-option", "-t", tmux.SESSION, COCKPIT_OPTION, "1")
    tmux.tmux("set-option", "-t", tmux.SESSION, SIDEBAR_PANE_OPTION, left)
    tmux.tmux("set-option", "-t", tmux.SESSION, RIGHT_PANE_OPTION, right)


def _fix_layout(left: str, sidebar_width: int) -> None:
    tmux.tmux("set-window-option", "-t", TARGET, "main-pane-width", str(sidebar_width))
    tmux.tmux("set-window-option", "-u", "-t", TARGET, "window-style")
    tmux.tmux("set-window-option", "-u", "-t", TARGET, "window-active-style")
    tmux.tmux("set-window-option", "-t", TARGET, "pane-border-style", "fg=terminal")
    tmux.tmux("set-window-option", "-t", TARGET, "pane-active-border-style", "fg=terminal")
    tmux.tmux("set-window-option", "-t", TARGET, "pane-border-lines", "single")
    tmux.tmux("select-pane", "-t", left)
    tmux.tmux("select-layout", "-t", TARGET, "main-vertical")


def _install_layout_hooks(left: str, sidebar_width: int) -> None:
    command = f"set-window-option -t {TARGET} main-pane-width {sidebar_width} ; select-pane -t {left} ; select-layout -t {TARGET} main-vertical"
    tmux.tmux("set-hook", "-t", tmux.SESSION, "client-attached", command)
    tmux.tmux("set-hook", "-t", tmux.SESSION, "client-resized", command)


def _install_bindings(prefix: str) -> None:
    tmux.tmux("bind-key", prefix, "send-prefix")
    tmux.tmux("bind-key", "s", "run-shell", FOCUS_SIDEBAR)
    for slot in range(1, 10):
        tmux.tmux("bind-key", str(slot), "run-shell", f"{shlex.quote(sys.executable)} -m mtmux switch-session {slot}")


def _enable_mouse() -> None:
    tmux.tmux("set-option", "-t", tmux.SESSION, "mouse", "on")
    tmux.tmux("unbind-key", "-q", "-T", "root", "MouseDrag1Border")


def _enable_clipboard() -> None:
    tmux.tmux("set-option", "-s", "set-clipboard", "on")


def _enable_truecolor() -> None:
    """Propagate RGB terminal capability when the host reports truecolor."""
    if _truecolor_enabled():
        tmux.tmux("set-option", "-as", "terminal-features", ",xterm-256color:RGB")


def _install_bell_hook() -> None:
    tmux.tmux("set-window-option", "-t", TARGET, "monitor-bell", "on")
    tmux.tmux("set-option", "-t", tmux.SESSION, "bell-action", "any")
    tmux.tmux("set-hook", "-t", tmux.SESSION, "alert-bell", "set-option -F -t mtmux @mtmux_bell_target '#{@mtmux_current_target}'")


def _install_right_pane_reset(left: str, right: str, prefix: str) -> None:
    tmux.tmux("set-option", "-p", "-t", right, "remain-on-exit", "on")
    command = f"if-shell -F '#{{==:#{{hook_pane}},{right}}}' {{ set-option -u -t {tmux.SESSION} @mtmux_current_target ; set-option -u -t {tmux.SESSION} @mtmux_bell_target ; respawn-pane -k -t {right} {shlex.quote(help_command(prefix))} ; select-pane -t {left} }}"
    tmux.tmux("set-hook", "-t", tmux.SESSION, "pane-died", command)


def _configure_cockpit(left: str, right: str, prefix: str, sidebar_width: int) -> None:
    _set_markers(left, right)
    _fix_layout(left, sidebar_width)
    _install_layout_hooks(left, sidebar_width)
    _install_bell_hook()
    _install_right_pane_reset(left, right, prefix)
    tmux.tmux("set-option", "-t", tmux.SESSION, "prefix", prefix)
    tmux.tmux("set-option", "-t", tmux.SESSION, "status", "off")
    _enable_mouse()
    _enable_clipboard()
    _enable_truecolor()
    _install_bindings(prefix)


def _build(prefix: str, sidebar_width: int) -> None:
    _, wrapper = ensure_config()
    help_cmd = help_command(prefix)
    if _window_exists():
        tmux.tmux("kill-window", "-t", TARGET, check=False)
    if tmux.tmux("has-session", "-t", tmux.SESSION, check=False).returncode != 0:
        tmux.tmux("new-session", "-d", "-s", tmux.SESSION, "-n", tmux.WINDOW, help_cmd, config=wrapper)
    else:
        tmux.tmux("new-window", "-d", "-t", tmux.SESSION, "-n", tmux.WINDOW, help_cmd)
    right = tmux.out("display-message", "-p", "-t", TARGET, "#{pane_id}")
    left = tmux.out("split-window", "-h", "-b", "-l", str(sidebar_width), "-P", "-F", "#{pane_id}", "-t", right, SIDEBAR)
    _configure_cockpit(left, right, prefix, sidebar_width)


def ensure_cockpit() -> None:
    prefix = load_prefix()
    sidebar_width = load_sidebar_width()
    if _valid():
        left = _option(SIDEBAR_PANE_OPTION)
        right = _option(RIGHT_PANE_OPTION)
        _configure_cockpit(left, right, prefix, sidebar_width)
        return
    if _option(COCKPIT_OPTION) == "1":
        right = _option(RIGHT_PANE_OPTION)
        if right and tmux.has_pane(right):
            left = tmux.out("split-window", "-h", "-b", "-l", str(sidebar_width), "-P", "-F", "#{pane_id}", "-t", right, SIDEBAR)
            _configure_cockpit(left, right, prefix, sidebar_width)
            return
    _build(prefix, sidebar_width)


def _attach() -> int:
    attach_cmd = "tmux -L mtmux attach -t mtmux:cockpit"
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(f"Cockpit ready. Attach from a real terminal: {attach_cmd}")
        return 0
    try:
        tty = os.ttyname(0)
    except OSError:
        tty = ""
    if tty == "/dev/tty":
        print(f"Cockpit ready. Current fd is /dev/tty; tmux refuses it. Run: {attach_cmd}")
        return 0
    cmd = ["tmux", "-L", tmux.SOCKET, "attach-session", "-t", TARGET]
    if shutil.which("script"):
        shell_cmd = " ".join(shlex.quote(part) for part in cmd)
        os.execvp("script", ["script", "-q", "-c", shell_cmd, "/dev/null"])
    os.execvp("tmux", cmd)
    return 0


def cockpit() -> int:
    ensure_config()
    width = shutil.get_terminal_size((80, 24)).columns
    if width < 90:
        print("Terminal too narrow for mtmux cockpit: need at least 90 columns.", file=sys.stderr)
        return 2
    ensure_cockpit()
    return _attach()


def focus_sidebar() -> int:
    ensure_config()
    ensure_cockpit()
    return 0


def right_pane() -> str | None:
    if not _valid():
        return None
    pane = _option(RIGHT_PANE_OPTION)
    return pane if pane and tmux.has_pane(pane) else None


def _require_right_pane() -> str:
    pane = right_pane()
    if not pane:
        raise SystemExit(NO_COCKPIT)
    return pane


def switch(target: Target, attach_command: str) -> None:
    pane = _require_right_pane()
    tmux.tmux("set-option", "-t", tmux.SESSION, CURRENT_TARGET_OPTION, target.format())
    tmux.tmux("set-option", "-u", "-t", tmux.SESSION, BELL_TARGET_OPTION)
    tmux.tmux("respawn-pane", "-k", "-t", pane, attach_command)
    tmux.tmux("select-pane", "-t", pane)


def show_help() -> None:
    tmux.tmux("respawn-pane", "-k", "-t", _require_right_pane(), help_command(load_prefix()))


def _target_option(name: str) -> Target | None:
    text = _option(name)
    if not text:
        return None
    try:
        return parse_target(text)
    except SystemExit:
        return None


def current_target() -> Target | None:
    if target := _target_option(CURRENT_TARGET_OPTION):
        return target
    pane = right_pane()
    command = tmux.out("display-message", "-p", "-t", pane or "", "#{pane_start_command}", check=False)
    try:
        parts = shlex.split(command)
        if parts and parts[0] == "ssh":
            index = 1
            while index < len(parts):
                if parts[index] == "-o":
                    index += 2
                elif parts[index].startswith("-"):
                    index += 1
                else:
                    break
            if index < len(parts) and (match := re.search(r"(?:^| )tmux .* -s ([A-Za-z0-9_.-]+)", " ".join(parts[index + 1:]))):
                return Target("ssh", match.group(1), parts[index])
        if match := re.search(r"(?:^| )tmux new-session .* -s ([A-Za-z0-9_.-]+)", command):
            return Target("local", match.group(1))
    except SystemExit:
        pass
    return None


def bell_target() -> Target | None:
    return _target_option(BELL_TARGET_OPTION)


def sidebar_active() -> bool:
    pane = _option(SIDEBAR_PANE_OPTION)
    return not pane or tmux.out("display-message", "-p", "-t", pane, "#{pane_active}", check=False) == "1"
