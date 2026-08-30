from __future__ import annotations

import locale
import os
import re
import shlex
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass

from .config import (
    DEFAULT_KEYBINDINGS,
    DEFAULT_SIDEBAR_KEYBINDINGS,
    ensure_config,
    load_hosts,
    load_keybindings,
    load_prefix,
    load_sidebar_keybindings,
    load_sidebar_width,
)
from .names import DEFAULT_SERVER, PaneTarget, Target, parse_target
from . import diagnostics, sessions, tmux


def _truecolor_enabled() -> bool:
    """Detect whether the host terminal supports truecolor (24-bit color)."""
    colorterm = os.environ.get("COLORTERM", "").lower()
    return colorterm in ("truecolor", "24bit")


def _split_prefix_value(value: str) -> tuple[bool, str]:
    if len(value) >= 7 and value[:7].lower() == "prefix+":
        return True, value[7:]
    return False, value


def _display_key(prefix: str, value: str) -> str:
    has_pref, eff = _split_prefix_value(value)
    return f"{prefix} {eff}" if has_pref else eff


def _bind_key(key_value: str, *command: str) -> None:
    has_pref, eff = _split_prefix_value(key_value)
    if has_pref:
        tmux.tmux("bind-key", eff, *command)
    else:
        tmux.tmux("bind-key", "-n", eff, *command)

def help_command(prefix: str, keybindings: dict[str, str] | None = None, sidebar_keybindings: dict[str, str] | None = None) -> str:
    ascii_mode = os.environ.get("LETEE_ASCII") == "1" or "utf" not in locale.getpreferredencoding(False).lower()
    move_handle = ":" if ascii_mode else "↕"
    if keybindings is None:
        try:
            keybindings = load_keybindings()
        except SystemExit:
            keybindings = DEFAULT_KEYBINDINGS
    if sidebar_keybindings is None:
        sidebar_keybindings = load_sidebar_keybindings()
    kb = keybindings
    skb = sidebar_keybindings
    text = f"""letee

Navigation
  {_display_key(prefix, kb["focus_agents"])}  focus/open Agents
  {_display_key(prefix, kb["focus_sessions"])}  focus/open Sessions
  {_display_key(prefix, kb["add_session"])}  add session
  {_display_key(prefix, kb["remove_active"])}  remove active session
  {_display_key(prefix, kb["kill_active"])}  kill and remove active session (confirm)
  {_display_key(prefix, kb["jump_alert"])}  jump to first alerted agent
  {_display_key(prefix, kb["focus_right"])}  focus right pane
  {_display_key(prefix, kb["toggle_sidebar"])}  hide/show sidebar
  {_display_key(prefix, kb["quit"])}  quit cockpit
  {prefix} 1-9  switch session
  {_display_key(prefix, kb["help"])}  open help

Session actions
  Enter  activate selected row
  {skb["rename"]}      rename selected session
  {skb["remove"]}      remove selected session (session keeps running)
  {skb["move_up"]}/{skb["move_down"]}    move session up/down
  {skb["kill"]}      kill and remove selected session
  {move_handle}      start mouse move; hover destination, then click
  Esc    cancel mouse move
  Right-click  open session Rename/Remove/Kill menu

Agent actions
  {skb["navigate_down"]}/{skb["navigate_up"]}    navigate agents
  Enter  switch to agent pane
  {skb["kill"]}      terminate selected agent with SIGTERM (confirm; pane and shell survive)
  Right-click  open agent Kill menu
  Left/Right  cycle ordering on selected ordering row (Priority / Session)
  {skb["resize_inc"]} / {skb["resize_dec"]}  resize agent panel

Recovery
  {_display_key(prefix, kb["detach"])}  detach cockpit
  {_display_key(prefix, kb["focus_sessions"])}  restart/focus Sessions
  {_display_key(prefix, kb["focus_agents"])}  restart/focus Agents
  {_display_key(prefix, kb["add_session"])}  open Add session menu
  Esc/Ctrl-C  cancel prompts/filter

Examples
  Enter  recreate missing session or activate selected Add row
"""
    return f"printf %s {shlex.quote(text)}; exec tail -f /dev/null"


SIDEBAR = f"{shlex.quote(sys.executable)} -m letee sidebar"
FOCUS_SIDEBAR = f"{shlex.quote(sys.executable)} -m letee focus-sidebar"
SIDEBAR_ACTION_KEYS = {"add": "F11", "remove": "F8", "kill": "F9", "alert": "F10"}
TARGET = f"{tmux.SESSION}:{tmux.WINDOW}"
COCKPIT_OPTION = "@letee_cockpit"
SIDEBAR_PANE_OPTION = "@letee_sidebar_pane"
SIDEBAR_WIDTH_OPTION = "@letee_sidebar_width"
RIGHT_PANE_OPTION = "@letee_right_pane"
CURRENT_TARGET_OPTION = "@letee_current_target"
CURRENT_AGENT_OPTION = "@letee_current_agent"
BELL_TARGET_OPTION = "@letee_bell_target"
ROOT_KEYS_OPTION = "@letee_root_keys"
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


def _install_bindings(
    prefix: str,
    sidebar_pane: str,
    right_pane: str,
    keybindings: dict[str, str] | None = None,
) -> None:
    if keybindings is None:
        keybindings = load_keybindings()
    try:
        old_raw = _option(ROOT_KEYS_OPTION)
    except Exception:
        old_raw = ""
    old_keys = shlex.split(old_raw) if old_raw else []
    for key in old_keys:
        tmux.tmux("unbind-key", "-q", "-T", "root", key)
    tmux.tmux("unbind-key", "-a", "-T", "prefix")
    tmux.tmux(
        "bind-key", "-n", "MouseDown1Pane",
        f"if-shell -F -t = '#{{==:#{{pane_id}},{sidebar_pane}}}' "
        "{ send-keys -M -t = ; select-pane -t = } "
        "{ select-pane -t = ; send-keys -M }",
    )
    tmux.tmux("bind-key", prefix, "send-prefix")
    _bind_key(keybindings["detach"], "detach-client")
    _bind_key(keybindings["toggle_sidebar"], "resize-pane", "-Z", "-t", right_pane)
    _bind_key(keybindings["quit"], "kill-session", "-t", tmux.SESSION)
    _bind_key(keybindings["focus_agents"], "run-shell", _focus_sidebar_command("agents"))
    _bind_key(keybindings["focus_sessions"], "run-shell", _focus_sidebar_command("sessions"))
    _bind_key(keybindings["add_session"], "run-shell", _focus_sidebar_command("add"))
    _bind_key(keybindings["remove_active"], "run-shell", _focus_sidebar_command("remove"))
    _bind_key(keybindings["kill_active"], "run-shell", _focus_sidebar_command("kill"))
    _bind_key(keybindings["jump_alert"], "run-shell", _focus_sidebar_command("alert"))
    _bind_key(keybindings["focus_right"], "select-pane", "-t", right_pane)
    _bind_key(keybindings["help"], "respawn-pane", "-k", "-t", right_pane, help_command(prefix, keybindings))
    for slot in range(1, 10):
        tmux.tmux("bind-key", str(slot), "run-shell", _letee_command("switch-session", str(slot)))
    new_root_keys = [_split_prefix_value(v)[1] for v in keybindings.values() if not _split_prefix_value(v)[0]]
    if new_root_keys:
        tmux.tmux("set-option", "-t", tmux.SESSION, ROOT_KEYS_OPTION, shlex.join(new_root_keys))
    elif old_keys:
        tmux.tmux("set-option", "-t", tmux.SESSION, ROOT_KEYS_OPTION, "")


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
    help_cmd = help_command(prefix, load_keybindings())
    if _window_exists():
        tmux.tmux("kill-window", "-t", TARGET, check=False)
    if tmux.tmux("has-session", "-t", tmux.SESSION, check=False).returncode != 0:
        tmux.tmux("new-session", "-d", "-s", tmux.SESSION, "-n", tmux.WINDOW, help_cmd, config=wrapper)
    else:
        tmux.tmux("new-window", "-d", "-t", tmux.SESSION, "-n", tmux.WINDOW, help_cmd)
    right = tmux.out("display-message", "-p", "-t", TARGET, "#{pane_id}")
    left = tmux.out("split-window", "-h", "-b", "-l", str(sidebar_width), "-P", "-F", "#{pane_id}", "-t", right, _sidebar_command())
    _configure_cockpit(left, right, prefix, sidebar_width)


def ensure_cockpit(*, restart_sidebar: bool = False) -> None:
    prefix = load_prefix()
    sidebar_width = load_sidebar_width()
    if _valid():
        left = _option(SIDEBAR_PANE_OPTION)
        right = _option(RIGHT_PANE_OPTION)
        _configure_cockpit(left, right, prefix, sidebar_width)
        if restart_sidebar:
            tmux.tmux("respawn-pane", "-k", "-t", left, _sidebar_command())
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


def _prepare_remote_hosts(hosts: list[str]) -> None:
    if not hosts:
        return
    interactive = sys.stdin.isatty()
    if interactive:
        print("Preparing SSH access before opening letee.", flush=True)
        print("Enter key passphrase if OpenSSH asks. If an SSH agent is available, successfully used keys will be saved in it.", flush=True)
        print("Press Ctrl-C to cancel.", flush=True)
    agent_ready = sessions.ensure_ssh_agent()
    if not interactive:
        print("Skipping interactive SSH checks: non-TTY startup (not attached to a TTY).", flush=True)
        return
    if agent_ready:
        print("SSH agent: ready", flush=True)
    else:
        print("SSH agent: unavailable; passphrases may repeat.", flush=True)

    for index, host in enumerate(hosts, 1):
        print(f"[{index}/{len(hosts)}] {host} — connecting...", flush=True)

    groups = sessions.group_hosts(hosts)
    results: dict[str, bool] = {}
    for group in groups:
        for host in group:
            if host in results:
                continue
            host_ready = sessions.prepare_host(host)
            results[host] = host_ready
            if host_ready:
                break

    probe_hosts = [host for host in hosts if host not in results]
    probe_host_set = set(probe_hosts)
    if probe_hosts:
        probe_results = sessions.probe_hosts(probe_hosts)
        results.update(zip(probe_hosts, probe_results))

    ready = 0
    failed: list[str] = []
    for index, host in enumerate(hosts, 1):
        host_ready = results.get(host, False)
        if host in probe_host_set and not host_ready:
            print(f"[{index}/{len(hosts)}] {host} — retrying interactively...", flush=True)
            host_ready = sessions.prepare_host(host)
            results[host] = host_ready
        if host_ready:
            ready += 1
            print(f"[{index}/{len(hosts)}] {host} — ready", flush=True)
        else:
            failed.append(host)
            print(f"[{index}/{len(hosts)}] {host} — failed", flush=True)
    print(f"SSH check complete: {ready} ready, {len(failed)} failed.", flush=True)
    if failed:
        print(f"Failed hosts remain unavailable. Run `ssh {failed[0]}` to diagnose.", flush=True)
    print("Starting letee...", flush=True)


def cockpit() -> int:
    ensure_config()
    width = shutil.get_terminal_size((80, 24)).columns
    if width < 90:
        print("Terminal too narrow for letee: need at least 90 columns.", file=sys.stderr)
        return 2
    hosts = load_hosts()
    try:
        _prepare_remote_hosts(hosts)
    except KeyboardInterrupt:
        print("\nSSH preparation canceled.", file=sys.stderr, flush=True)
        return 130
    ensure_cockpit(restart_sidebar=True)
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
        "add": (SIDEBAR_ACTION_KEYS["add"],),
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


def _switch_fields(
    target: Target,
    pane: str | None,
    switch_id: str | None,
    action_id: str | None,
    input_id: str | None,
) -> dict[str, object]:
    return {
        "switch_id": switch_id,
        "action_id": action_id,
        "input_id": input_id,
        "target": target.format(),
        "right_pane": pane,
    }


def switch(
    target: Target,
    attach_command: str,
    agent_id: str | None = None,
    *,
    action_id: str | None = None,
    input_id: str | None = None,
) -> None:
    debug = diagnostics.get_diagnostics(server=tmux.SERVER)
    context = debug.context_values()
    action_id = action_id or context.get("action_id")
    input_id = input_id or context.get("input_id")
    switch_id = action_id or debug.new_id("switch")
    try:
        pane = right_pane()
    except BaseException as error:
        debug.emit(
            "switch_requested",
            **_switch_fields(target, None, switch_id, action_id, input_id),
        )
        debug.emit(
            "switch_error",
            **_switch_fields(target, None, switch_id, action_id, input_id),
            stage="right_pane_lookup",
            error_type=type(error).__name__,
        )
        raise
    debug.emit(
        "switch_requested",
        **_switch_fields(target, pane, switch_id, action_id, input_id),
        agent_id=agent_id,
    )
    if not pane:
        error = SystemExit(NO_COCKPIT)
        debug.emit(
            "switch_error",
            **_switch_fields(target, pane, switch_id, action_id, input_id),
            stage="right_pane_lookup",
            error_type=type(error).__name__,
        )
        raise error

    def stage(kind: str, name: str, operation: Callable[[], None]) -> None:
        fields = _switch_fields(target, pane, switch_id, action_id, input_id)
        debug.emit(f"switch_{kind}", **fields, stage=name, status="started")
        try:
            operation()
        except BaseException as error:
            debug.emit(f"switch_{kind}", **fields, stage=name, status="error", error_type=type(error).__name__)
            debug.emit(
                "switch_error",
                **fields,
                stage=name,
                error_type=type(error).__name__,
            )
            raise
        debug.emit(f"switch_{kind}", **fields, stage=name, status="completed")

    def update_markers() -> None:
        tmux.tmux("set-option", "-t", tmux.SESSION, CURRENT_TARGET_OPTION, target.format())
        if agent_id:
            tmux.tmux("set-option", "-t", tmux.SESSION, CURRENT_AGENT_OPTION, agent_id)
        else:
            tmux.tmux("set-option", "-u", "-t", tmux.SESSION, CURRENT_AGENT_OPTION)
        tmux.tmux("set-option", "-u", "-t", tmux.SESSION, BELL_TARGET_OPTION)

    stage("marker_update", "current_target_marker", update_markers)
    stage(
        "respawn",
        "right_pane_respawn",
        lambda: tmux.tmux("respawn-pane", "-k", "-t", pane, attach_command),
    )
    stage(
        "focus",
        "right_pane_focus",
        lambda: tmux.tmux("select-pane", "-t", pane),
    )
    debug.emit(
        "switch_completed",
        **_switch_fields(target, pane, switch_id, action_id, input_id),
        agent_id=agent_id,
    )


def rename_target(old: Target, new: Target) -> None:
    if current_target() == old:
        tmux.tmux("set-option", "-t", tmux.SESSION, CURRENT_TARGET_OPTION, new.format())
    if bell_target() == old:
        tmux.tmux("set-option", "-t", tmux.SESSION, BELL_TARGET_OPTION, new.format())


def _tmux_supports_menu_mouse() -> bool:
    match = re.search(r"(\d+)\.(\d+)", tmux.out("-V", check=False))
    return bool(match and (int(match.group(1)), int(match.group(2))) >= (3, 5))


def show_session_menu(target: Target, x: int, y: int, sidebar_keybindings: dict[str, str] | None = None) -> None:
    pane = _option(SIDEBAR_PANE_OPTION)
    title = f"{target.session}@{target.host or 'localhost'}"
    mouse_flag = ("-M",) if _tmux_supports_menu_mouse() else ()
    if sidebar_keybindings is None:
        try:
            skb = load_sidebar_keybindings()
        except SystemExit:
            skb = DEFAULT_SIDEBAR_KEYBINDINGS
    else:
        skb = sidebar_keybindings
    tmux.tmux(
        "display-menu", *mouse_flag, "-O", "-T", title, "-x", str(x), "-y", str(y), "-t", pane,
        "Rename", skb["rename"], f"send-keys -t {pane} {shlex.quote(skb['rename'])}",
        "Remove", skb["remove"], f"send-keys -t {pane} {shlex.quote(skb['remove'])}",
        "Kill", skb["kill"], f"send-keys -t {pane} {shlex.quote(skb['kill'])} y",
        timeout=None,
    )


def show_agent_menu(agent_name: str, pane_target: PaneTarget, x: int, y: int, sidebar_keybindings: dict[str, str] | None = None) -> None:
    pane = _option(SIDEBAR_PANE_OPTION)
    title = f"{agent_name}@{pane_target.target.session}@{pane_target.target.host or 'localhost'}"
    mouse_flag = ("-M",) if _tmux_supports_menu_mouse() else ()
    if sidebar_keybindings is None:
        try:
            skb = load_sidebar_keybindings()
        except SystemExit:
            skb = DEFAULT_SIDEBAR_KEYBINDINGS
    else:
        skb = sidebar_keybindings
    tmux.tmux(
        "display-menu", *mouse_flag, "-O", "-T", title, "-x", str(x), "-y", str(y), "-t", pane,
        "Kill", skb["kill"], f"send-keys -t {pane} {shlex.quote(skb['kill'])} y",
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
    if len(fields) != 6 or fields[5] != ".":
        return None
    active_pane, sidebar_pane, current_target, bell_target, current_agent, _ = fields
    return StatusSnapshot(
        _parse_target_option(current_target),
        _parse_target_option(bell_target),
        current_agent or None,
        not sidebar_pane or active_pane == sidebar_pane,
    )


def status_snapshot() -> StatusSnapshot | None:
    text = tmux.out(
        "display-message", "-p", "-t", TARGET,
        f"#{{pane_id}}\t#{{{SIDEBAR_PANE_OPTION}}}\t#{{{CURRENT_TARGET_OPTION}}}\t#{{{BELL_TARGET_OPTION}}}\t#{{{CURRENT_AGENT_OPTION}}}\t.",
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
