from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass

from .config import ensure_config, load_prefix, load_sidebar_width
from .names import DEFAULT_SERVER, Target, parse_target
from . import tmux


def _truecolor_enabled() -> bool:
    """Detect whether the host terminal supports truecolor (24-bit color)."""
    colorterm = os.environ.get("COLORTERM", "").lower()
    return colorterm in ("truecolor", "24bit")

def help_command(prefix: str) -> str:
    text = f"""letee

Navigation
  {prefix} a  focus/open Agents
  {prefix} s  focus/open Sessions
  {prefix} +  add session
  {prefix} r  remove active session
  {prefix} x  kill and remove active session (confirm)
  {prefix} !  jump to first alerted agent
  {prefix} w  focus right pane
  {prefix} h  hide/show sidebar
  {prefix} q  quit cockpit
  {prefix} 1-9  switch session
  {prefix} ?  open help
  ?      open help from sidebar
  q      quit sidebar only

Session actions
  Enter  activate selected row
  a      open Add session menu
  r      remove selected session (session keeps running)
  K/J    move session up/down
  x      kill and remove selected session
  Right-click  open session Remove/Kill menu
  /      search untracked existing sessions

Agent actions
  j/k    navigate agents
  Enter  switch to agent pane
  h/l    cycle ordering on selected ordering row (Priority / Session)
  [ / ]  resize agent panel

Recovery
  {prefix} d  detach cockpit
  {prefix} s  restart/focus Sessions
  {prefix} a  restart/focus Agents
  {prefix} +  open Add session menu
  Esc    cancel prompts/filter

Examples
  /work  find available sessions matching work
  Enter  recreate missing session or activate selected Add row
"""
    return f"printf %s {shlex.quote(text)}; exec tail -f /dev/null"


SIDEBAR = f"{shlex.quote(sys.executable)} -m letee sidebar"
FOCUS_SIDEBAR = f"{shlex.quote(sys.executable)} -m letee focus-sidebar"
SIDEBAR_ACTION_KEYS = {"remove": "F8", "kill": "F9", "alert": "F10"}
TARGET = f"{tmux.SESSION}:{tmux.WINDOW}"
COCKPIT_OPTION = "@letee_cockpit"
SIDEBAR_PANE_OPTION = "@letee_sidebar_pane"
SIDEBAR_WIDTH_OPTION = "@letee_sidebar_width"
RIGHT_PANE_OPTION = "@letee_right_pane"
CURRENT_TARGET_OPTION = "@letee_current_target"
CURRENT_AGENT_OPTION = "@letee_current_agent"
BELL_TARGET_OPTION = "@letee_bell_target"
NO_COCKPIT = "No valid letee. Run: letee"


@dataclass(frozen=True)
class StatusSnapshot:
    current_target: Target | None
    bell_target: Target | None
    current_agent: str | None
    pane_active: bool


def _letee_command(*args: str) -> str:
    command = [sys.executable, "-m", "letee"]
    if tmux.SERVER != DEFAULT_SERVER:
        command.extend(("-L", tmux.SERVER))
    return shlex.join((*command, *args))


def _sidebar_command() -> str:
    return _letee_command("sidebar")


def _focus_sidebar_command(region: str) -> str:
    return _letee_command("focus-sidebar", region)


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
    tmux.tmux("set-window-option", "-t", TARGET, SIDEBAR_WIDTH_OPTION, str(sidebar_width))
    tmux.tmux("set-window-option", "-t", TARGET, "main-pane-width", str(sidebar_width))
    tmux.tmux("set-window-option", "-u", "-t", TARGET, "window-style")
    tmux.tmux("set-window-option", "-u", "-t", TARGET, "window-active-style")
    tmux.tmux("set-window-option", "-t", TARGET, "pane-border-style", "fg=terminal")
    tmux.tmux("set-window-option", "-t", TARGET, "pane-active-border-style", "fg=terminal")
    tmux.tmux("set-window-option", "-t", TARGET, "pane-border-lines", "single")
    tmux.tmux("resize-pane", "-t", left, "-x", str(sidebar_width), check=False)


def repair_layout(left: str) -> None:
    state = tmux.out(
        "display-message", "-p", "-t", left,
        f"#{{pane_width}}:#{{{SIDEBAR_WIDTH_OPTION}}}:#{{window_width}}:#{{client_width}}:#{{window_offset_x}}",
        check=False,
    ).split(":")
    if len(state) == 5 and state[0] == state[1] and state[2] == state[3] and state[4] in ("", "0"):
        return
    command = f"resize-window -a -t {TARGET} ; resize-pane -t {left} -x '#{{{SIDEBAR_WIDTH_OPTION}}}'"
    tmux.tmux("run-shell", "-C", "-t", left, command, check=False)


def _install_layout_hooks(left: str, sidebar_width: int) -> None:
    tmux.tmux("set-hook", "-u", "-t", tmux.SESSION, "client-attached")
    tmux.tmux("set-hook", "-u", "-t", tmux.SESSION, "client-resized")
    tmux.tmux("set-hook", "-w", "-t", TARGET, "window-resized", f"resize-pane -t {left} -x {sidebar_width}")


def _install_bindings(prefix: str, _sidebar_pane: str, right_pane: str) -> None:
    tmux.tmux("unbind-key", "-a", "-T", "prefix")
    tmux.tmux("bind-key", prefix, "send-prefix")
    tmux.tmux("bind-key", "d", "detach-client")
    tmux.tmux("bind-key", "h", "resize-pane", "-Z", "-t", right_pane)
    tmux.tmux("bind-key", "q", "kill-session", "-t", tmux.SESSION)
    tmux.tmux("bind-key", "a", "run-shell", _focus_sidebar_command("agents"))
    tmux.tmux("bind-key", "s", "run-shell", _focus_sidebar_command("sessions"))
    tmux.tmux("bind-key", "+", "run-shell", _focus_sidebar_command("add"))
    tmux.tmux("bind-key", "r", "run-shell", _focus_sidebar_command("remove"))
    tmux.tmux("bind-key", "x", "run-shell", _focus_sidebar_command("kill"))
    tmux.tmux("bind-key", "!", "run-shell", _focus_sidebar_command("alert"))
    tmux.tmux("bind-key", "w", "select-pane", "-t", right_pane)
    tmux.tmux("bind-key", "?", "respawn-pane", "-k", "-t", right_pane, help_command(prefix))
    for slot in range(1, 10):
        tmux.tmux("bind-key", str(slot), "run-shell", _letee_command("switch-session", str(slot)))


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
    tmux.tmux("set-hook", "-t", tmux.SESSION, "alert-bell", "set-option -F -t letee @letee_bell_target '#{@letee_current_target}'")


def _unavailable_command(target: Target | None = None) -> str:
    name = f"Session {target.format()}" if target else "Active session"
    text = f"{name} is unavailable.\n\nSelect another session from the sidebar.\n"
    return f"printf %s {shlex.quote(text)}; exec sh"


def _missing_command(target: Target) -> str:
    text = f"Session {target.format()} is missing.\n\nSelect it in the sidebar. Press Enter to recreate it.\n"
    return f"printf %s {shlex.quote(text)}; exec sh"


def _reconnecting_command(target: Target) -> str:
    reset, cyan, dim = "\033[0m", "\033[38;5;81m", "\033[2m"
    ascii_mode = os.environ.get("LETEE_ASCII") == "1"
    banner = "+-- Connection interrupted --+" if ascii_mode else "╭─ Connection interrupted ─╮"
    underline = "+----------------------------+" if ascii_mode else "╰──────────────────────────╯"
    indent = "    "
    text = f"\n{indent}{cyan}{banner}\n{indent}{underline}{reset}\n\n{indent}{dim}Session{reset}  {target.session}\n{indent}{dim}Host{reset}     {target.host or 'local'}\n\n"
    frames = ("|", "/", "-", "\\") if ascii_mode else ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    dots = (".  ", ".. ", "...") if ascii_mode else ("·  ", "·· ", "···", " ··", "  ·")
    colors = (45, 51, 87, 123, 159, 123, 87, 51)
    states = " ".join(shlex.quote(f"{color}:{frame}:{dots[index % len(dots)]}") for index, (color, frame) in enumerate(zip(colors, frames)))
    save_cursor = shlex.quote("\0337")
    spinner = shlex.quote(f"\0338\033[2K{indent}\033[38;5;%sm%s\033[0m Reconnecting%s")
    animate = 'color=${state%%:*}; rest=${state#*:}; frame=${rest%%:*}; dots=${rest#*:}'
    return f"printf %s {shlex.quote(text)}; printf %s {save_cursor}; while :; do for state in {states}; do {animate}; printf {spinner} \"$color\" \"$frame\" \"$dots\"; sleep 0.1; done; done"


def _install_right_pane_reset(left: str, right: str) -> None:
    tmux.tmux("set-option", "-p", "-t", right, "remain-on-exit", "on")
    command = f"if-shell -F '#{{==:#{{hook_pane}},{right}}}' {{ set-option -u -t {tmux.SESSION} @letee_current_agent ; set-option -u -t {tmux.SESSION} @letee_bell_target ; respawn-pane -k -t {right} {shlex.quote(_unavailable_command())} ; select-pane -t {left} }}"
    tmux.tmux("set-hook", "-t", tmux.SESSION, "pane-died", command)


def _configure_cockpit(left: str, right: str, prefix: str, sidebar_width: int) -> None:
    _set_markers(left, right)
    _fix_layout(left, sidebar_width)
    _install_layout_hooks(left, sidebar_width)
    _install_bell_hook()
    _install_right_pane_reset(left, right)
    tmux.tmux("set-option", "-t", tmux.SESSION, "prefix", prefix)
    tmux.tmux("set-option", "-t", tmux.SESSION, "status", "off")
    tmux.tmux("set-option", "-s", "escape-time", "0")
    _enable_mouse()
    _enable_clipboard()
    _enable_truecolor()
    _install_bindings(prefix, left, right)


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
    left = tmux.out("split-window", "-h", "-b", "-l", str(sidebar_width), "-P", "-F", "#{pane_id}", "-t", right, _sidebar_command())
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
            left = tmux.out("split-window", "-h", "-b", "-l", str(sidebar_width), "-P", "-F", "#{pane_id}", "-t", right, _sidebar_command())
            _configure_cockpit(left, right, prefix, sidebar_width)
            return
    _build(prefix, sidebar_width)


def _attach() -> int:
    attach_cmd = f"tmux -L {shlex.quote(tmux.SOCKET)} attach -d -t {TARGET}"
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
    cmd = ["tmux", "-L", tmux.SOCKET, "attach-session", "-d", "-t", TARGET]
    if shutil.which("script"):
        shell_cmd = " ".join(shlex.quote(part) for part in cmd)
        os.execvp("script", ["script", "-q", "-c", shell_cmd, "/dev/null"])
    os.execvp("tmux", cmd)
    return 0


def cockpit() -> int:
    ensure_config()
    width = shutil.get_terminal_size((80, 24)).columns
    if width < 90:
        print("Terminal too narrow for letee: need at least 90 columns.", file=sys.stderr)
        return 2
    ensure_cockpit()
    return _attach()


def focus_sidebar(region: str = "sessions") -> int:
    ensure_config()
    ensure_cockpit()
    pane = _option(SIDEBAR_PANE_OPTION)
    if region != "alert":
        tmux.tmux("select-pane", "-t", pane)
    keys = {
        "sessions": ("F6",),
        "agents": ("F7",),
        "add": ("F6", "a"),
        "remove": ("F6", SIDEBAR_ACTION_KEYS["remove"]),
        "kill": ("F6", SIDEBAR_ACTION_KEYS["kill"]),
        "alert": ("F7", SIDEBAR_ACTION_KEYS["alert"]),
    }[region]
    tmux.tmux("send-keys", "-t", pane, *keys)
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


def switch(target: Target, attach_command: str, agent_id: str | None = None) -> None:
    pane = _require_right_pane()
    tmux.tmux("set-option", "-t", tmux.SESSION, CURRENT_TARGET_OPTION, target.format())
    if agent_id:
        tmux.tmux("set-option", "-t", tmux.SESSION, CURRENT_AGENT_OPTION, agent_id)
    else:
        tmux.tmux("set-option", "-u", "-t", tmux.SESSION, CURRENT_AGENT_OPTION)
    tmux.tmux("set-option", "-u", "-t", tmux.SESSION, BELL_TARGET_OPTION)
    tmux.tmux("respawn-pane", "-k", "-t", pane, attach_command)
    tmux.tmux("select-pane", "-t", pane)


def show_help() -> None:
    tmux.tmux("respawn-pane", "-k", "-t", _require_right_pane(), help_command(load_prefix()))


def _tmux_supports_menu_mouse() -> bool:
    match = re.search(r"(\d+)\.(\d+)", tmux.out("-V", check=False))
    return bool(match and (int(match.group(1)), int(match.group(2))) >= (3, 5))


def show_session_menu(target: Target, x: int, y: int) -> None:
    pane = _option(SIDEBAR_PANE_OPTION)
    title = f"{target.session}@{target.host or 'localhost'}"
    mouse_flag = ("-M",) if _tmux_supports_menu_mouse() else ()
    tmux.tmux(
        "display-menu", *mouse_flag, "-O", "-T", title, "-x", str(x), "-y", str(y), "-t", pane,
        "Remove", "r", f"send-keys -t {pane} r",
        "Kill", "x", f"send-keys -t {pane} x y",
        timeout=None,
    )


def show_unavailable(target: Target) -> None:
    tmux.tmux("respawn-pane", "-k", "-t", _require_right_pane(), _unavailable_command(target))


def show_missing(target: Target) -> None:
    tmux.tmux("respawn-pane", "-k", "-t", _require_right_pane(), _missing_command(target))


def show_reconnecting(target: Target) -> None:
    tmux.tmux("respawn-pane", "-k", "-t", _require_right_pane(), _reconnecting_command(target))


def _parse_target_option(text: str) -> Target | None:
    if not text:
        return None
    try:
        return parse_target(text)
    except SystemExit:
        return None


def _target_option(name: str) -> Target | None:
    return _parse_target_option(_option(name))


def _parse_status_snapshot(text: str) -> StatusSnapshot | None:
    fields = text.split("\t")
    if len(fields) != 5 or fields[0] not in ("0", "1") or fields[4] != ".":
        return None
    return StatusSnapshot(
        _parse_target_option(fields[1]),
        _parse_target_option(fields[2]),
        fields[3] or None,
        fields[0] == "1",
    )


def status_snapshot() -> StatusSnapshot | None:
    text = tmux.out(
        "display-message", "-p", "-t", f"{tmux.SESSION}:#{{{SIDEBAR_PANE_OPTION}}}",
        f"#{{pane_active}}\t#{{{CURRENT_TARGET_OPTION}}}\t#{{{BELL_TARGET_OPTION}}}\t#{{{CURRENT_AGENT_OPTION}}}\t.",
        check=False,
    )
    return _parse_status_snapshot(text)


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


def current_agent() -> str | None:
    return _option(CURRENT_AGENT_OPTION) or None


def bell_target() -> Target | None:
    return _target_option(BELL_TARGET_OPTION)


def sidebar_active() -> bool:
    pane = _option(SIDEBAR_PANE_OPTION)
    return not pane or tmux.out("display-message", "-p", "-t", pane, "#{pane_active}", check=False) == "1"
