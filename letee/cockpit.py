from __future__ import annotations

import locale
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from .config import (
    COMPACT_SIDEBAR_WIDTH,
    DEFAULT_KEYBINDINGS,
    DEFAULT_SIDEBAR_KEYBINDINGS,
    ensure_config,
    load_hosts,
    load_keybindings,
    load_prefix,
    load_sidebar_collapse_mode,
    load_sidebar_keybindings,
    load_sidebar_width,
)
from .names import INNER_SERVER_SOCKET, DEFAULT_SERVER, PaneTarget, Target, WindowTarget, parse_target
from . import attachment, diagnostics, sessions, tmux


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
        except SystemExit as error:
            diagnostics.error(error, operation="help_keybindings")
            keybindings = DEFAULT_KEYBINDINGS
    if sidebar_keybindings is None:
        sidebar_keybindings = load_sidebar_keybindings()
    kb = DEFAULT_KEYBINDINGS | keybindings
    skb = sidebar_keybindings
    text = f"""letee

Navigation
  {_display_key(prefix, kb["focus_agents"])}  focus/open Agents
  {_display_key(prefix, kb["focus_sessions"])}  focus/open Sessions
  {_display_key(prefix, kb["find_work"])}  find work
  {_display_key(prefix, kb["add_session"])}  add session
  {_display_key(prefix, kb["remove_active"])}  remove active session
  {_display_key(prefix, kb["kill_active"])}  kill and remove active session (confirm)
  {_display_key(prefix, kb["jump_alert"])}  jump to first agent needing attention
  {_display_key(prefix, kb["previous_agent"])}  switch to previous agent
  {_display_key(prefix, kb["next_agent"])}  switch to next agent
  {_display_key(prefix, kb["previous_session"])}  switch to previous session
  {_display_key(prefix, kb["next_session"])}  switch to next session
  {_display_key(prefix, kb["focus_right"])}  focus right pane
  {_display_key(prefix, kb["toggle_sidebar"])}  collapse/expand sidebar
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

Compact sidebar
  Click ‹ in the expanded header to collapse, or < in ASCII mode
  Five columns: session number, agent status + two-column name, ! alert
  Click rows to switch; wheel scrolls each section
  Top button expands and focuses right; focus/actions restore full controls
  sidebar_collapse_mode = "hidden" keeps hide/show behavior

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
SIDEBAR_ACTION_KEYS = {"add": "F11", "remove": "F8", "kill": "F9", "alert": "F10", "previous_agent": "F12", "next_agent": "F5", "previous_session": "F3", "next_session": "F4"}
TARGET = f"{tmux.SESSION}:{tmux.WINDOW}"
COCKPIT_OPTION = "@letee_cockpit"
SIDEBAR_PANE_OPTION = "@letee_sidebar_pane"
SIDEBAR_WIDTH_OPTION = "@letee_sidebar_width"
SIDEBAR_COMPACT_OPTION = "@letee_sidebar_compact"
RIGHT_PANE_OPTION = "@letee_right_pane"
CURRENT_TARGET_OPTION = "@letee_current_target"
EXPECTED_RIGHT_PANE_DEATH_OPTION = "@letee_expected_right_pane_death"
EXPECTED_RIGHT_PANE_DEATH_TARGET_OPTION = "@letee_expected_right_pane_death_target"
EXPECTED_RIGHT_PANE_DEATH_GENERATION_OPTION = "@letee_expected_right_pane_death_generation"
_EXPECTED_RIGHT_PANE_DEATH_PENDING = "1"
_EXPECTED_RIGHT_PANE_DEATH_CONSUMED = "consumed"
_EXPECTED_RIGHT_PANE_DEATH_SUCCEEDED = "succeeded"
CURRENT_AGENT_OPTION = "@letee_current_agent"
BELL_TARGET_OPTION = "@letee_bell_target"
ROOT_KEYS_OPTION = "@letee_root_keys"
ATTACHMENT_LAUNCH_GENERATION_OPTION = "@letee_attachment_launch_generation"
ATTACHMENT_FAILURE_TARGET_OPTION = "@letee_attachment_failure_target"
ATTACHMENT_FAILURE_GENERATION_OPTION = "@letee_attachment_failure_generation"
ATTACHMENT_ID_OPTION = "@letee_attachment_id"
ATTACHMENT_GENERATION_OPTION = "@letee_attachment_generation"
ATTACHMENT_TARGET_OPTION = "@letee_attachment_target"
ATTACHMENT_AGENT_OPTION = "@letee_attachment_agent"
ATTACHMENT_SWITCH_OPTION = "@letee_attachment_switch"
ATTACHMENT_RUN_OPTION = "@letee_attachment_run"
NO_COCKPIT = "No valid letee. Run: letee"


@dataclass(frozen=True)
class StatusSnapshot:
    current_target: Target | None
    bell_target: Target | None
    current_agent: str | None
    pane_active: bool
    attachment_failure: tuple[Target, str] | None = None


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


def _ensure_client_size() -> bool | tuple[str, os.terminal_size]:
    try:
        raw = tmux.out(
            "list-clients",
            "-t", tmux.SESSION,
            "-F", "#{client_pid}:#{client_tty}:#{client_width}:#{client_height}:#{client_session}",
            check=False,
        )
    except Exception as error:
        diagnostics.error(error, operation="layout_client_query")
        diagnostics.log("layout_client_query_error", error_type=type(error).__name__)
        return True
    if not raw:
        diagnostics.log("layout_client_query", status="empty")
        return True
    for line in raw.splitlines():
        if not line:
            continue
        parts = line.split(":")
        if len(parts) != 5:
            continue
        pid_s, tty, w_s, h_s, session = parts
        # Only consider clients attached to the letee session if tmux reports it.
        # When filtering by -t SESSION tmux already limits to that session,
        # but guard against unexpected output: if session is present and not ours, skip.
        # Detached clients have no session association; they appear only when -t is omitted,
        # so with -t they are already excluded. Keep this guard cheap.
        if session and session != tmux.SESSION:
            continue
        # Treat empty/detached tty as detached.
        if not tty or tty == "":
            continue
        try:
            cached_w = int(w_s)
            cached_h = int(h_s)
            pid = int(pid_s)
        except ValueError as error:
            diagnostics.error(error, handling="ignored", operation="layout_client_parse")
            continue
        try:
            fd = os.open(tty, os.O_RDONLY | os.O_NOCTTY)
        except OSError as error:
            diagnostics.error(error, handling="ignored", operation="layout_tty_open")
            continue
        try:
            size = os.get_terminal_size(fd)
        except OSError as error:
            diagnostics.error(error, handling="ignored", operation="layout_tty_size")
            continue
        finally:
            try:
                os.close(fd)
            except OSError as error:
                diagnostics.error(error, handling="ignored", operation="layout_tty_close")
        stale = size.columns != cached_w or size.lines != cached_h
        diagnostics.log(
            "layout_client_probe",
            client_pid=pid,
            client_tty=tty,
            cached_width=cached_w,
            cached_height=cached_h,
            tty_width=size.columns,
            tty_height=size.lines,
            status="stale" if stale else "healthy",
        )
        if stale:
            try:
                os.kill(pid, signal.SIGWINCH)
            except (ProcessLookupError, PermissionError, OSError) as error:
                diagnostics.error(error, operation="layout_client_signal")
                diagnostics.log(
                    "layout_client_signal",
                    client_pid=pid,
                    client_tty=tty,
                    signal="SIGWINCH",
                    status="error",
                    error_type=type(error).__name__,
                )
                return False
            diagnostics.log(
                "layout_client_signal",
                client_pid=pid,
                client_tty=tty,
                signal="SIGWINCH",
                status="sent",
            )
            return tty, size
    return True


@diagnostics.operation("cockpit.repair_layout")
def repair_layout(left: str) -> None:
    client = _ensure_client_size()
    if client is False:
        return
    if isinstance(client, tuple):
        tty, size = client
        command = (
            f"resize-window -x {size.columns} -y {size.lines} -t {TARGET} ; "
            f"resize-pane -t {left} -x '#{{{SIDEBAR_WIDTH_OPTION}}}' ; "
            f"refresh-client -t {tty}"
        )
        result = tmux.tmux("run-shell", "-C", "-t", left, command, check=False)
        diagnostics.log(
            "layout_repair",
            pane=left,
            mode="stale_client",
            client_tty=tty,
            width=size.columns,
            height=size.lines,
            returncode=getattr(result, "returncode", None),
        )
        return
    state = tmux.out(
        "display-message", "-p", "-t", left,
        f"#{{pane_width}}:#{{{SIDEBAR_WIDTH_OPTION}}}:#{{window_width}}:#{{client_width}}:#{{window_height}}:#{{client_height}}:#{{window_offset_x}}",
        check=False,
    ).split(":")
    healthy = (
        len(state) == 7
        and state[0] == state[1]
        and state[2] == state[3]
        and state[4] == state[5]
        and state[6] in ("", "0")
    )
    diagnostics.log("layout_state", pane=left, state=state, healthy=healthy)
    if healthy:
        return
    command = f"resize-window -a -t {TARGET} ; resize-pane -t {left} -x '#{{{SIDEBAR_WIDTH_OPTION}}}'"
    result = tmux.tmux("run-shell", "-C", "-t", left, command, check=False)
    diagnostics.log(
        "layout_repair",
        pane=left,
        mode="automatic",
        state=state,
        returncode=getattr(result, "returncode", None),
    )


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
    keybindings = DEFAULT_KEYBINDINGS | keybindings
    help_cmd = help_command(prefix, keybindings)
    try:
        old_raw = _option(ROOT_KEYS_OPTION)
    except Exception as error:
        diagnostics.error(error, operation="root_keybindings")
        old_raw = ""
    old_keys = shlex.split(old_raw) if old_raw else []
    for key in old_keys:
        tmux.tmux("unbind-key", "-q", "-T", "root", key)
    tmux.tmux("unbind-key", "-a", "-T", "prefix")
    tmux.tmux(
        "bind-key", "-n", "MouseDown1Pane",
        f"if-shell -F -t = '#{{==:#{{pane_id}},{sidebar_pane}}}' "
        "{ send-keys -t = F2 ; send-keys -M -t = ; select-pane -t = } "
        "{ select-pane -t = ; send-keys -M }",
    )
    tmux.tmux("bind-key", prefix, "send-prefix")
    _bind_key(keybindings["detach"], "detach-client")
    _bind_key(keybindings["toggle_sidebar"], "run-shell", _letee_command("toggle-sidebar"))
    _bind_key(keybindings["find_work"], "run-shell", "-b", _letee_command("find-work"))
    _bind_key(keybindings["quit"], "kill-session", "-t", tmux.SESSION)
    _bind_key(keybindings["focus_agents"], "run-shell", _focus_sidebar_command("agents"))
    _bind_key(keybindings["focus_sessions"], "run-shell", _focus_sidebar_command("sessions"))
    _bind_key(keybindings["add_session"], "run-shell", _focus_sidebar_command("add"))
    _bind_key(keybindings["remove_active"], "run-shell", _focus_sidebar_command("remove"))
    _bind_key(keybindings["kill_active"], "run-shell", _focus_sidebar_command("kill"))
    _bind_key(keybindings["jump_alert"], "run-shell", _focus_sidebar_command("alert"))
    _bind_key(keybindings["previous_agent"], "run-shell", _focus_sidebar_command("previous_agent"))
    _bind_key(keybindings["next_agent"], "run-shell", _focus_sidebar_command("next_agent"))
    _bind_key(keybindings["previous_session"], "run-shell", _focus_sidebar_command("previous_session"))
    _bind_key(keybindings["next_session"], "run-shell", _focus_sidebar_command("next_session"))
    _bind_key(keybindings["focus_right"], "select-pane", "-t", right_pane)
    _bind_key(keybindings["help"], "respawn-pane", "-k", "-t", right_pane, help_cmd)
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


def _animated_command(target: Target, title: str, progress: str) -> str:
    text, animation_states = attachment.animation_data(target, title)
    indent = "    "
    states = " ".join(shlex.quote(f"{color}:{frame}:{dots}") for color, frame, dots in animation_states)
    save_cursor = shlex.quote("\0337")
    spinner = shlex.quote(f"\0338\033[2K{indent}\033[38;5;%sm%s\033[0m {progress}%s")
    animate = 'color=${state%%:*}; rest=${state#*:}; frame=${rest%%:*}; dots=${rest#*:}'
    return f"printf %s {shlex.quote(text)}; printf %s {save_cursor}; while :; do for state in {states}; do {animate}; printf {spinner} \"$color\" \"$frame\" \"$dots\"; sleep 0.1; done; done"


def _clear_expected_right_pane_death() -> str:
    return (
        f"set-option -u -t {tmux.SESSION} {EXPECTED_RIGHT_PANE_DEATH_OPTION} ; "
        f"set-option -u -t {tmux.SESSION} {EXPECTED_RIGHT_PANE_DEATH_TARGET_OPTION} ; "
        f"set-option -u -t {tmux.SESSION} {EXPECTED_RIGHT_PANE_DEATH_GENERATION_OPTION}"
    )


def _option_equals(name: str, value: str) -> str:
    return f"#{{==:#{{{name}}},{value}}}"


def _guard_expected_right_pane_death(target: Target, action: str) -> str:
    expected_target = _option_equals(EXPECTED_RIGHT_PANE_DEATH_TARGET_OPTION, target.format())
    current_target = _option_equals(CURRENT_TARGET_OPTION, target.format())
    stale_cleanup = f"if-shell -F '{expected_target}' {{ {_clear_expected_right_pane_death()} }} {{ }}"
    return (
        f"if-shell -F '{expected_target}' "
        f"{{ if-shell -F '{current_target}' {{ {action} }} {{ {stale_cleanup} }} }} {{ }}"
    )


def _right_pane_death_action(
    left: str,
    right: str,
    prefix: str,
    *,
    succeeded: bool,
    target: Target | None = None,
) -> str:
    cleanup = "set-option -u -t letee @letee_current_agent ; set-option -u -t letee @letee_bell_target"
    marker_cleanup = _clear_expected_right_pane_death()
    if succeeded:
        return (
            f"{cleanup} ; {marker_cleanup} ; "
            f"set-option -u -t {tmux.SESSION} {CURRENT_TARGET_OPTION} ; "
            f"respawn-pane -k -t {right} {shlex.quote(help_command(prefix))} ; select-pane -t {left}"
        )
    restore_target = (
        f"set-option -t {tmux.SESSION} {CURRENT_TARGET_OPTION} {shlex.quote(target.format())} ; "
        if target is not None
        else ""
    )
    return (
        f"{cleanup} ; {restore_target}{marker_cleanup} ; "
        f"respawn-pane -k -t {right} {shlex.quote(_unavailable_command(target))} ; select-pane -t {left}"
    )


def _missing_command(target: Target) -> str:
    return _animated_command(target, "Session missing", "Waiting for session")


def _reconnecting_command(target: Target) -> str:
    return _animated_command(target, "Connection interrupted", "Reconnecting")


def _switching_command(target: Target) -> str:
    return _animated_command(target, "Switching session", "Preparing")


def _install_right_pane_reset(left: str, right: str, prefix: str) -> None:
    tmux.tmux("set-option", "-p", "-t", right, "remain-on-exit", "on")
    target_match = f"#{{==:#{{{EXPECTED_RIGHT_PANE_DEATH_TARGET_OPTION}}},#{{{CURRENT_TARGET_OPTION}}}}}"
    generation_match = _option_equals(EXPECTED_RIGHT_PANE_DEATH_GENERATION_OPTION, "#{pane_pid}")
    attachment_match = (
        f"#{{&&:#{{pane_dead}},{_option_equals(ATTACHMENT_LAUNCH_GENERATION_OPTION, '#{pane_pid}')}}}"
    )
    current_attachment = _option_equals(ATTACHMENT_TARGET_OPTION, f"#{{{CURRENT_TARGET_OPTION}}}")
    unexpected = f"#{{!=:#{{{EXPECTED_RIGHT_PANE_DEATH_GENERATION_OPTION}}},#{{pane_pid}}}}"
    unsuccessful = "#{||:#{pane_dead_status},#{pane_dead_signal}}"
    record_failure = (
        f"if-shell -F '#{{&&:{unsuccessful},#{{&&:{attachment_match},#{{&&:{current_attachment},{unexpected}}}}}}}' {{ "
        f"set-option -F -t {right} {ATTACHMENT_FAILURE_TARGET_OPTION} '#{{{ATTACHMENT_TARGET_OPTION}}}' ; "
        f"set-option -F -t {right} {ATTACHMENT_FAILURE_GENERATION_OPTION} '#{{pane_pid}}' "
        f"}} ; "
    )
    fallback = record_failure + _right_pane_death_action(left, right, prefix="", succeeded=False)
    state_action = (
        f"if-shell -F '#{{==:#{{{EXPECTED_RIGHT_PANE_DEATH_OPTION}}},{_EXPECTED_RIGHT_PANE_DEATH_PENDING}}}' "
        f"{{ set-option -t {tmux.SESSION} {EXPECTED_RIGHT_PANE_DEATH_OPTION} {_EXPECTED_RIGHT_PANE_DEATH_CONSUMED} }} "
        f"{{ if-shell -F '#{{==:#{{{EXPECTED_RIGHT_PANE_DEATH_OPTION}}},{_EXPECTED_RIGHT_PANE_DEATH_SUCCEEDED}}}' "
        f"{{ {_right_pane_death_action(left, right, prefix, succeeded=True)} }} "
        f"{{ {fallback} }} }}"
    )
    expected_generation = (
        f"if-shell -F '{generation_match}' {{ {state_action} }} {{ {fallback} }}"
    )
    stale_target = (
        f"if-shell -F '{generation_match}' {{ {_clear_expected_right_pane_death()} }} {{ {fallback} }}"
    )
    expected_death = (
        f"if-shell -F '#{{?{EXPECTED_RIGHT_PANE_DEATH_TARGET_OPTION},1,0}}' "
        f"{{ if-shell -F '{target_match}' {{ {expected_generation} }} {{ {stale_target} }} }} "
        f"{{ {fallback} }}"
    )
    callback_action = ""
    debug = diagnostics.get_diagnostics(server=tmux.SERVER)
    if debug.enabled:
        # tmux expands these before recovery. q escapes for the callback shell,
        # with '' preserving empty arguments. Quoting formats would escape twice.
        fields = (
            "hook_pane", ATTACHMENT_ID_OPTION, "pane_pid", ATTACHMENT_TARGET_OPTION,
            ATTACHMENT_AGENT_OPTION, ATTACHMENT_SWITCH_OPTION, "pane_dead_status",
            "pane_dead_signal", EXPECTED_RIGHT_PANE_DEATH_OPTION,
        )
        callback = (
            (f"env LETEE_DEBUG_LOG={shlex.quote(str(debug.path.resolve()))} "
             + _letee_command("attachment-exited")).replace("#", "##")
            + f" --parent-run-id #{{q:{ATTACHMENT_RUN_OPTION}}}'' -- "
            + " ".join(f"#{{q:{field}}}''" for field in fields)
        )
        # Only matching generations may carry identity. Placeholder processes and
        # exits before identity installation deliberately report unknown metadata.
        unknown_fields = {ATTACHMENT_ID_OPTION, ATTACHMENT_TARGET_OPTION,
                          ATTACHMENT_AGENT_OPTION, ATTACHMENT_SWITCH_OPTION,
                          ATTACHMENT_RUN_OPTION, EXPECTED_RIGHT_PANE_DEATH_OPTION}
        unknown_callback = callback
        for field in unknown_fields:
            unknown_callback = unknown_callback.replace(f"#{{q:{field}}}", "")
        marker_target_match = _option_equals(
            EXPECTED_RIGHT_PANE_DEATH_TARGET_OPTION, f"#{{{ATTACHMENT_TARGET_OPTION}}}"
        )
        marker_generation_match = _option_equals(EXPECTED_RIGHT_PANE_DEATH_GENERATION_OPTION, "#{pane_pid}")
        marker_match = f"#{{&&:{marker_target_match},{marker_generation_match}}}"
        marker = f"#{{q:{EXPECTED_RIGHT_PANE_DEATH_OPTION}}}"
        callback = callback.replace(marker, f"#{{?{marker_match},{marker},}}")
        generation_match = _option_equals(ATTACHMENT_GENERATION_OPTION, "#{pane_pid}")
        match = f"#{{&&:#{{pane_dead}},{generation_match}}}"
        # A delayed hook may already see a live successor. Its PID is not an
        # exited generation, so do not report it even as an unknown attachment.
        unknown_callback = unknown_callback.replace("#{q:pane_pid}", "#{?pane_dead,#{q:pane_pid},}")
        callback_action = (
            f"if-shell -F '{match}' {{ run-shell -b {shlex.quote(callback)} }} "
            f"{{ run-shell -b {shlex.quote(unknown_callback)} }} ; "
        )
    command = (
        f"if-shell -F '#{{&&:#{{==:#{{hook_pane}},{right}}},#{{pane_dead}}}}' "
        f"{{ {callback_action}{expected_death} }}"
    )
    tmux.tmux("set-hook", "-t", tmux.SESSION, "pane-died", command)


@diagnostics.operation("cockpit.configure")
def _configure_cockpit(left: str, right: str, prefix: str, sidebar_width: int) -> None:
    _set_markers(left, right)
    _fix_layout(left, sidebar_width)
    _install_layout_hooks(left, sidebar_width)
    _install_bell_hook()
    _install_right_pane_reset(left, right, prefix)
    tmux.tmux("set-option", "-t", tmux.SESSION, "prefix", prefix)
    tmux.tmux("set-option", "-t", tmux.SESSION, "status", "off")
    tmux.tmux("set-option", "-s", "escape-time", "0")
    _enable_mouse()
    _enable_clipboard()
    _enable_truecolor()
    _install_bindings(prefix, left, right)


@diagnostics.operation("cockpit.build")
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
    tmux.tmux("set-option", "-u", "-t", tmux.SESSION, SIDEBAR_COMPACT_OPTION)
    _configure_cockpit(left, right, prefix, sidebar_width)


@diagnostics.operation("cockpit.ensure")
def ensure_cockpit(*, restart_sidebar: bool = False) -> None:
    prefix = load_prefix()
    sidebar_width = load_sidebar_width()
    if _valid():
        left = _option(SIDEBAR_PANE_OPTION)
        right = _option(RIGHT_PANE_OPTION)
        effective_width = COMPACT_SIDEBAR_WIDTH if sidebar_compact() else sidebar_width
        _configure_cockpit(left, right, prefix, effective_width)
        if restart_sidebar:
            tmux.tmux("respawn-pane", "-k", "-t", left, _sidebar_command())
        return
    if _option(COCKPIT_OPTION) == "1":
        right = _option(RIGHT_PANE_OPTION)
        if right and tmux.has_pane(right):
            left = tmux.out("split-window", "-h", "-b", "-l", str(sidebar_width), "-P", "-F", "#{pane_id}", "-t", right, _sidebar_command())
            tmux.tmux("set-option", "-u", "-t", tmux.SESSION, SIDEBAR_COMPACT_OPTION)
            _configure_cockpit(left, right, prefix, sidebar_width)
            return
    _build(prefix, sidebar_width)


@diagnostics.operation("cockpit.attach")
def _attach() -> int:
    executable = tmux.tmux_executable()
    attach_cmd = shlex.join((executable, "-L", tmux.SOCKET, "attach", "-d", "-t", TARGET))
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        diagnostics.log("cockpit_attach", outcome="non_tty")
        print(f"Cockpit ready. Attach from a real terminal: {attach_cmd}")
        return 0
    try:
        tty = os.ttyname(0)
    except OSError as error:
        diagnostics.error(error, operation="attach_tty")
        tty = ""
    if tty == "/dev/tty":
        diagnostics.log("cockpit_attach", outcome="dev_tty")
        print(f"Cockpit ready. Current fd is /dev/tty; tmux refuses it. Run: {attach_cmd}")
        return 0
    cmd = [executable, "-L", tmux.SOCKET, "attach-session", "-d", "-t", TARGET]
    diagnostics.log("cockpit_attach", outcome="exec", right_pane=TARGET)
    diagnostics.flush()
    os.execvp(executable, cmd)
    return 0


@diagnostics.operation("cockpit.prepare_remote_hosts", origin="startup", active=False)
def _prepare_remote_hosts(hosts: list[str]) -> None:
    if not hosts:
        diagnostics.log("ssh_preparation", outcome="skipped", reason="no_hosts")
        return
    interactive = sys.stdin.isatty()
    diagnostics.log("ssh_preparation", outcome="started", interactive=interactive, host_count=len(hosts))
    if interactive:
        print("Preparing SSH access before opening letee.", flush=True)
        print("Enter key passphrase if OpenSSH asks. If an SSH agent is available, successfully used keys will be saved in it.", flush=True)
        print("Press Ctrl-C to cancel.", flush=True)
    agent_ready = sessions.ensure_ssh_agent()
    if not interactive:
        diagnostics.log("ssh_preparation", outcome="skipped", reason="non_tty", host_count=len(hosts))
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
                diagnostics.log("ssh_preparation_host", host=host, outcome="skipped", reason="already_checked")
                continue
            with diagnostics.get_diagnostics().context(host=host, origin="startup_interactive"):
                host_ready = sessions.prepare_host(host)
            results[host] = host_ready
            if host_ready:
                diagnostics.log("ssh_preparation_group", host=host, outcome="ready", host_count=len(group))
                break

    probe_hosts = [host for host in hosts if host not in results]
    probe_host_set = set(probe_hosts)
    if probe_hosts:
        with diagnostics.get_diagnostics().context(origin="startup_batch"):
            probe_results = sessions.probe_hosts(probe_hosts)
        results.update(zip(probe_hosts, probe_results))

    ready = 0
    failed: list[str] = []
    for index, host in enumerate(hosts, 1):
        host_ready = results.get(host, False)
        if host in probe_host_set and not host_ready:
            print(f"[{index}/{len(hosts)}] {host} — retrying interactively...", flush=True)
            diagnostics.log("ssh_preparation_host", host=host, outcome="retrying", reason="batch_failed")
            with diagnostics.get_diagnostics().context(host=host, origin="startup_retry"):
                host_ready = sessions.prepare_host(host)
            results[host] = host_ready
        diagnostics.log("ssh_preparation_host", host=host, outcome="ready" if host_ready else "failed")
        if host_ready:
            ready += 1
            print(f"[{index}/{len(hosts)}] {host} — ready", flush=True)
        else:
            failed.append(host)
            print(f"[{index}/{len(hosts)}] {host} — failed", flush=True)
    diagnostics.log("ssh_preparation", outcome="completed", ready_count=ready, failed_count=len(failed))
    print(f"SSH check complete: {ready} ready, {len(failed)} failed.", flush=True)
    if failed:
        print(f"Failed hosts remain unavailable. Run `ssh {failed[0]}` to diagnose.", flush=True)
    print("Starting letee...", flush=True)


def cockpit() -> int:
    with diagnostics.operation("cockpit.launch") as operation:
        ensure_config()
        width = shutil.get_terminal_size((80, 24)).columns
        if width < 90:
            operation.outcome = "rejected"
            print("Terminal too narrow for letee: need at least 90 columns.", file=sys.stderr)
            return 2
        if _valid():
            ensure_cockpit()
            return _attach()
        hosts = load_hosts()
        try:
            _prepare_remote_hosts(hosts)
        except KeyboardInterrupt as error:
            operation.outcome = "cancelled"
            diagnostics.error(error, handling="cancelled", operation="ssh_preparation")
            print("\nSSH preparation canceled.", file=sys.stderr, flush=True)
            return 130
        ensure_cockpit(restart_sidebar=True)
        return _attach()


def sidebar_compact() -> bool:
    return _option(SIDEBAR_COMPACT_OPTION) == "1"


def _sidebar_hidden() -> bool:
    return tmux.out("display-message", "-p", "-t", TARGET, "#{window_zoomed_flag}", check=False) == "1"


@diagnostics.operation("cockpit.expand_sidebar")
def expand_sidebar(*, focus_right: bool = False) -> None:
    left = _option(SIDEBAR_PANE_OPTION)
    right = _option(RIGHT_PANE_OPTION)
    if not left or not right:
        return
    if _sidebar_hidden():
        tmux.tmux("resize-pane", "-Z", "-t", right)
    if sidebar_compact():
        width = load_sidebar_width()
        tmux.tmux("set-option", "-u", "-t", tmux.SESSION, SIDEBAR_COMPACT_OPTION)
        _fix_layout(left, width)
        _install_layout_hooks(left, width)
    if focus_right:
        tmux.tmux("select-pane", "-t", right)


def find_work() -> int:
    """Open Find without changing the sidebar layout or underlying pane focus."""
    if not _valid():
        raise SystemExit(NO_COCKPIT)
    # Expand before display-popup for tmux 3.3; avoid strftime percent markers.
    width, height = tmux.out(
        "display-message", "-p",
        "#{?#{e|>:#{client_width},125},100,#{e|/:#{e|*:#{client_width},80},100}} "
        "#{?#{e|>:#{client_height},37},30,#{e|/:#{e|*:#{client_height},80},100}}",
    ).split()
    return tmux.tmux(
        "display-popup", "-E", "-w", width, "-h", height, "-x", "C", "-y", "C",
        _letee_command("finder"), timeout=None,
    ).returncode


@diagnostics.operation("cockpit.toggle_sidebar")
def toggle_sidebar() -> int:
    if not _valid():
        raise SystemExit(NO_COCKPIT)
    if sidebar_compact() or _sidebar_hidden():
        expand_sidebar(focus_right=True)
        return 0
    mode = load_sidebar_collapse_mode()
    left = _option(SIDEBAR_PANE_OPTION)
    right = _option(RIGHT_PANE_OPTION)
    if mode == "hidden":
        tmux.tmux("resize-pane", "-Z", "-t", right)
    else:
        tmux.tmux("set-option", "-t", tmux.SESSION, SIDEBAR_COMPACT_OPTION, "1")
        _fix_layout(left, COMPACT_SIDEBAR_WIDTH)
        _install_layout_hooks(left, COMPACT_SIDEBAR_WIDTH)
    tmux.tmux("select-pane", "-t", right)
    return 0


@diagnostics.operation("cockpit.focus_sidebar")
def focus_sidebar(region: str = "sessions") -> int:
    ensure_config()
    background_action = region in ("alert", "previous_agent", "next_agent", "previous_session", "next_session")
    if not background_action or not _valid():
        ensure_cockpit()
    pane = _option(SIDEBAR_PANE_OPTION)
    if not background_action:
        expand_sidebar()
        tmux.tmux("select-pane", "-t", pane)
    keys = {
        "sessions": ("F6",),
        "agents": ("F7",),
        "add": (SIDEBAR_ACTION_KEYS["add"],),
        "remove": ("F6", SIDEBAR_ACTION_KEYS["remove"]),
        "kill": ("F6", SIDEBAR_ACTION_KEYS["kill"]),
        "alert": ("F7", SIDEBAR_ACTION_KEYS["alert"]),
        "previous_agent": ("F7", SIDEBAR_ACTION_KEYS["previous_agent"]),
        "next_agent": ("F7", SIDEBAR_ACTION_KEYS["next_agent"]),
        "previous_session": ("F6", SIDEBAR_ACTION_KEYS["previous_session"]),
        "next_session": ("F6", SIDEBAR_ACTION_KEYS["next_session"]),
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


def require_cockpit() -> None:
    """Fail with NO_COCKPIT before callers do any remote work (e.g. SSH)."""
    _require_right_pane()


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


def _record_attachment_replacement(pane: str, *, reason: str, switch_id: str | None = None) -> dict[str, object] | None:
    if not diagnostics.get_diagnostics().enabled:
        return
    fields = (ATTACHMENT_ID_OPTION, "pane_pid", ATTACHMENT_TARGET_OPTION,
              ATTACHMENT_AGENT_OPTION, ATTACHMENT_SWITCH_OPTION, ATTACHMENT_RUN_OPTION,
              ATTACHMENT_GENERATION_OPTION)
    try:
        raw = tmux.out("display-message", "-p", "-t", pane,
                       "\t".join(f"#{{{field}}}" for field in fields), check=False)
    except (Exception, SystemExit) as error:
        diagnostics.error(error, handling="ignored", operation="attachment_replacement_probe")
        return
    values = raw.split("\t")
    if len(values) != 7 or not values[0] or not values[1].isdecimal() or values[1] != values[6]:
        return
    attachment_id, generation, target, agent_id, old_switch_id, parent_run_id, _ = values
    fields = dict(
        right_pane=pane, attachment_id=attachment_id, generation=int(generation),
        target=target, agent_id=agent_id or None, switch_id=old_switch_id or None,
        parent_run_id=parent_run_id or None, replacement_switch_id=switch_id,
        intentional=True, reason=reason,
    )
    diagnostics.log("attachment_replacement_requested", **fields)
    return fields


def _install_attachment_identity(pane: str, generation: str, fields: dict[str, str]) -> None:
    # One native queue checks the process and writes metadata. A delayed Python
    # writer cannot label a newer process. Commit generation last, so partial
    # option failures leave metadata unusable by the exit hook.
    if not generation.isdecimal():
        return
    commands = [f"set-option -p -t {pane} {ATTACHMENT_GENERATION_OPTION} ''"]
    commands.extend(f"set-option -p -t {pane} {option} {shlex.quote(value)}"
                    for option, value in fields.items())
    commands.append(f"set-option -p -t {pane} {ATTACHMENT_GENERATION_OPTION} {generation}")
    try:
        tmux.tmux("if-shell", "-F", "-t", pane, f"#{{==:#{{pane_pid}},{generation}}}",
                  " ; ".join(commands))
    except (Exception, SystemExit) as error:
        diagnostics.error(error, handling="ignored", operation="attachment_identity")


_RESPAWN_SUCCEEDED = "letee-respawn-succeeded"


def _respawn_attachment(pane: str, command: str, target: Target | None = None) -> str:
    # These commands share one native queue. Record success before the diagnostic
    # PID query, so even a query failure cannot turn a successful respawn into an
    # application failure. A respawn error aborts the queue before the marker.
    metadata = () if target is None else (
        "set-option", "-p", "-t", pane, ATTACHMENT_TARGET_OPTION, target.format(), ";",
        "set-option", "-p", "-F", "-t", pane, ATTACHMENT_LAUNCH_GENERATION_OPTION, "#{pane_pid}", ";",
    )
    try:
        result = tmux.tmux(
            "respawn-pane", "-k", "-t", pane, command, ";",
            "display-message", "-p", _RESPAWN_SUCCEEDED, ";", *metadata,
            "display-message", "-p", "-t", pane, "#{pane_pid}", capture=True,
        )
        output = result.stdout if isinstance(getattr(result, "stdout", None), str) else ""
    except (subprocess.CalledProcessError, SystemExit) as error:
        # tmux wraps timeouts in SystemExit but retains the timeout as context.
        failure = error.__context__ if isinstance(error, SystemExit) else error
        if not isinstance(failure, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
            raise
        output = failure.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        if _RESPAWN_SUCCEEDED not in output.splitlines():
            raise
        diagnostics.error(error, handling="ignored", operation="attachment_generation")
    lines = output.splitlines()
    return lines[1] if len(lines) == 2 and lines[0] == _RESPAWN_SUCCEEDED and lines[1].isdecimal() else ""


_Result = TypeVar("_Result")


@diagnostics.operation("cockpit.switch")
def switch(
    target: Target,
    attach_command: str | Callable[[], str],
    agent_id: str | None = None,
    *,
    action_id: str | None = None,
    input_id: str | None = None,
) -> None:
    debug = diagnostics.get_diagnostics(server=tmux.SERVER)
    switch_id = None
    if debug.enabled:
        context = debug.context_values()
        action_id = action_id or context.get("action_id")
        input_id = input_id or context.get("input_id")
        switch_id = action_id or debug.new_id("switch")
    try:
        pane = right_pane()
    except BaseException as error:
        diagnostics.error(error, handling="propagated", operation="switch_right_pane_lookup")
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
        diagnostics.error(error, handling="propagated", operation="switch_right_pane_lookup")
        debug.emit(
            "switch_error",
            **_switch_fields(target, pane, switch_id, action_id, input_id),
            stage="right_pane_lookup",
            error_type=type(error).__name__,
        )
        raise error

    def stage(kind: str, name: str, operation: Callable[[], _Result]) -> _Result:
        fields = _switch_fields(target, pane, switch_id, action_id, input_id)
        debug.emit(f"switch_{kind}", **fields, stage=name, status="started")
        try:
            with diagnostics.operation(f"cockpit.{name}", **fields):
                result = operation()
        except BaseException as error:
            diagnostics.error(error, handling="propagated", operation=name, **fields)
            debug.emit(f"switch_{kind}", **fields, stage=name, status="error", error_type=type(error).__name__)
            debug.emit(
                "switch_error",
                **fields,
                stage=name,
                error_type=type(error).__name__,
            )
            raise
        debug.emit(f"switch_{kind}", **fields, stage=name, status="completed")
        return result

    def update_markers() -> None:
        marker_update = (
            f"set-option -p -t {pane} {ATTACHMENT_LAUNCH_GENERATION_OPTION} '' ; "
            f"set-option -u -t {tmux.SESSION} {ATTACHMENT_FAILURE_TARGET_OPTION} ; "
            f"set-option -u -t {tmux.SESSION} {ATTACHMENT_FAILURE_GENERATION_OPTION} ; "
            f"set-option -t {tmux.SESSION} {CURRENT_TARGET_OPTION} {shlex.quote(target.format())}"
        )
        tmux.tmux("if-shell", "-F", "1", marker_update)
        if agent_id:
            tmux.tmux("set-option", "-t", tmux.SESSION, CURRENT_AGENT_OPTION, agent_id)
        else:
            tmux.tmux("set-option", "-u", "-t", tmux.SESSION, CURRENT_AGENT_OPTION)
        tmux.tmux("set-option", "-u", "-t", tmux.SESSION, BELL_TARGET_OPTION)

    replacement = _record_attachment_replacement(pane, reason="switch", switch_id=switch_id) if debug.enabled else None

    def replaced() -> None:
        nonlocal replacement
        if replacement:
            diagnostics.log("attachment_replaced", **replacement)
            replacement = None

    stage("marker_update", "current_target_marker", update_markers)
    if callable(attach_command):
        stage(
            "respawn",
            "switch_progress",
            lambda: tmux.tmux("respawn-pane", "-k", "-t", pane, _switching_command(target)),
        )
        replaced()
        try:
            attach_command = stage("prepare", "attach_command_preparation", attach_command)
        except BaseException as error:
            try:
                timed_out = isinstance(error, subprocess.TimeoutExpired) or isinstance(error.__cause__, subprocess.TimeoutExpired)
                command = _reconnecting_command(target) if target.kind == "ssh" and timed_out else _unavailable_command(target)
                tmux.tmux("respawn-pane", "-k", "-t", pane, command)
            except BaseException as recovery_error:
                diagnostics.error(recovery_error, handling="ignored", operation="switch_error_recovery")
            raise
    parts = shlex.split(attach_command)
    if target.kind == "ssh" and parts and parts[0] == "ssh":
        attach_command = attachment.command(target, attach_command)
    attachment_id = None
    if debug.enabled and attach_command != _reconnecting_command(target):
        attachment_id = uuid.uuid4().hex
    if attachment_id:
        diagnostics.log(
            "attachment_requested", attachment_id=attachment_id,
            **_switch_fields(target, pane, switch_id, action_id, input_id), agent_id=agent_id,
        )
    generation = stage(
        "respawn",
        "right_pane_respawn",
        lambda: _respawn_attachment(pane, attach_command, target)
        if attach_command != _reconnecting_command(target)
        else tmux.tmux("respawn-pane", "-k", "-t", pane, attach_command),
    )
    replaced()
    if attachment_id:
        _install_attachment_identity(pane, generation, {
            ATTACHMENT_ID_OPTION: attachment_id,
            ATTACHMENT_TARGET_OPTION: target.format(),
            ATTACHMENT_AGENT_OPTION: agent_id or "",
            ATTACHMENT_SWITCH_OPTION: switch_id or "",
            ATTACHMENT_RUN_OPTION: debug.run_id,
        })
        diagnostics.log(
            "attachment_started", attachment_id=attachment_id,
            generation=int(generation) if generation.isdecimal() else None,
            **_switch_fields(target, pane, switch_id, action_id, input_id), agent_id=agent_id,
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


def open_session(target: Target, attach_command: str | Callable[[], str] | None = None) -> None:
    """Open a session with an eagerly built attachment command."""
    switch(target, attach_command if attach_command is not None else sessions.attach_command(target))


def open_window(window: WindowTarget, attach_command: str | Callable[[], str] | None = None) -> None:
    """Open an existing window without selecting an agent or a different pane."""
    switch(window.target, attach_command if attach_command is not None else lambda: sessions.window_attach_command(window))


def open_agent(
    pane_target: PaneTarget, agent_id: str, attach_command: str | Callable[[], str] | None = None,
) -> None:
    """Open an agent's exact pane using the existing switch behavior."""
    switch(pane_target.target, attach_command if attach_command is not None else sessions.pane_attach_command(pane_target), agent_id)


@diagnostics.operation("cockpit.set_expected_right_pane_death")
def set_expected_right_pane_death(target: Target | None) -> None:
    if target is None:
        tmux.tmux("set-option", "-u", "-t", tmux.SESSION, EXPECTED_RIGHT_PANE_DEATH_OPTION)
        tmux.tmux("set-option", "-u", "-t", tmux.SESSION, EXPECTED_RIGHT_PANE_DEATH_TARGET_OPTION)
        tmux.tmux("set-option", "-u", "-t", tmux.SESSION, EXPECTED_RIGHT_PANE_DEATH_GENERATION_OPTION)
    else:
        right = _option(RIGHT_PANE_OPTION)
        if not right:
            return
        marker = (
            f"set-option -t {tmux.SESSION} {EXPECTED_RIGHT_PANE_DEATH_TARGET_OPTION} {shlex.quote(target.format())} ; "
            f"set-option -F -t {right} {EXPECTED_RIGHT_PANE_DEATH_GENERATION_OPTION} '#{{pane_pid}}' ; "
            f"set-option -t {tmux.SESSION} {EXPECTED_RIGHT_PANE_DEATH_OPTION} {_EXPECTED_RIGHT_PANE_DEATH_PENDING}"
        )
        tmux.tmux("if-shell", "-F", _option_equals(CURRENT_TARGET_OPTION, target.format()), marker)


@diagnostics.operation("cockpit.resolve_expected_right_pane_death")
def resolve_expected_right_pane_death(target: Target, succeeded: bool) -> None:
    left = _option(SIDEBAR_PANE_OPTION)
    right = _option(RIGHT_PANE_OPTION)
    if not left or not right:
        tmux.tmux("if-shell", "-F", "1", _guard_expected_right_pane_death(target, _clear_expected_right_pane_death()))
        return
    action = _right_pane_death_action(
        left,
        right,
        load_prefix() if succeeded else "",
        succeeded=succeeded,
        target=target,
    )
    if succeeded and tmux.out("display-message", "-p", "-t", right, "#{pane_current_command}", check=False) == "sh":
        tmux.tmux("if-shell", "-F", "1", _guard_expected_right_pane_death(target, action))
        return
    if succeeded:
        unresolved = f"set-option -t {tmux.SESSION} {EXPECTED_RIGHT_PANE_DEATH_OPTION} {_EXPECTED_RIGHT_PANE_DEATH_SUCCEEDED}"
    else:
        unresolved = _clear_expected_right_pane_death()
    resolution = (
        f"if-shell -F '#{{==:#{{{EXPECTED_RIGHT_PANE_DEATH_OPTION}}},{_EXPECTED_RIGHT_PANE_DEATH_CONSUMED}}}' "
        f"{{ {action} }} {{ {unresolved} }}"
    )
    tmux.tmux("if-shell", "-F", "1", _guard_expected_right_pane_death(target, resolution))


@diagnostics.operation("cockpit.reset_to_help")
def reset_to_help(target: Target) -> bool:
    left = _option(SIDEBAR_PANE_OPTION)
    right = _option(RIGHT_PANE_OPTION)
    if not left or not right:
        return False
    matched = _option(CURRENT_TARGET_OPTION) == target.format()
    if matched:
        _record_attachment_replacement(right, reason="reset_to_help")
    action = _right_pane_death_action(left, right, load_prefix(), succeeded=True)
    tmux.tmux("if-shell", "-F", _option_equals(CURRENT_TARGET_OPTION, target.format()), action)
    return matched


@diagnostics.operation("cockpit.set_current_target")
def set_current_target(target: Target) -> None:
    tmux.tmux("set-option", "-t", tmux.SESSION, CURRENT_TARGET_OPTION, target.format())


@diagnostics.operation("cockpit.rename_target")
def rename_target(old: Target, new: Target) -> None:
    if current_target() == old:
        right = _option(RIGHT_PANE_OPTION)
        action = f"set-option -t {tmux.SESSION} {CURRENT_TARGET_OPTION} {shlex.quote(new.format())}"
        if right:
            target_match = _option_equals(ATTACHMENT_TARGET_OPTION, old.format())
            generation_match = _option_equals(ATTACHMENT_LAUNCH_GENERATION_OPTION, "#{pane_pid}")
            failure_target_match = _option_equals(ATTACHMENT_FAILURE_TARGET_OPTION, old.format())
            failure_generation_match = _option_equals(
                ATTACHMENT_FAILURE_GENERATION_OPTION, f"#{{{ATTACHMENT_LAUNCH_GENERATION_OPTION}}}",
            )
            pending_failure = (
                f"#{{&&:#{{&&:{target_match},{failure_target_match}}},"
                f"#{{&&:#{{!=:#{{{ATTACHMENT_FAILURE_GENERATION_OPTION}}},}},{failure_generation_match}}}}}"
            )
            action = (
                f"if-shell -F '{pending_failure}' {{ "
                f"set-option -t {tmux.SESSION} {ATTACHMENT_FAILURE_TARGET_OPTION} {shlex.quote(new.format())} ; "
                f"set-option -p -t {right} {ATTACHMENT_TARGET_OPTION} {shlex.quote(new.format())} }} ; "
                f"if-shell -F '#{{&&:{target_match},{generation_match}}}' {{ "
                f"set-option -p -t {right} {ATTACHMENT_TARGET_OPTION} {shlex.quote(new.format())} }} ; "
                f"{action}"
            )
        tmux.tmux("if-shell", "-F", *(("-t", right) if right else ()),
                  _option_equals(CURRENT_TARGET_OPTION, old.format()), action)
    if bell_target() == old:
        tmux.tmux("set-option", "-t", tmux.SESSION, BELL_TARGET_OPTION, new.format())


def _tmux_supports_menu_mouse() -> bool:
    match = re.search(r"(\d+)\.(\d+)", tmux.out("-V", check=False))
    return bool(match and (int(match.group(1)), int(match.group(2))) >= (3, 5))


@diagnostics.operation("cockpit.show_session_menu")
def show_session_menu(target: Target, x: int, y: int, sidebar_keybindings: dict[str, str] | None = None) -> None:
    pane = _option(SIDEBAR_PANE_OPTION)
    title = f"{target.session}@{target.host or 'localhost'}"
    mouse_flag = ("-M",) if _tmux_supports_menu_mouse() else ()
    if sidebar_keybindings is None:
        try:
            skb = load_sidebar_keybindings()
        except SystemExit as error:
            diagnostics.error(error, operation="session_menu_keybindings")
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


@diagnostics.operation("cockpit.show_agent_menu")
def show_agent_menu(agent_name: str, pane_target: PaneTarget, x: int, y: int, sidebar_keybindings: dict[str, str] | None = None) -> None:
    pane = _option(SIDEBAR_PANE_OPTION)
    title = f"{agent_name}@{pane_target.target.session}@{pane_target.target.host or 'localhost'}"
    mouse_flag = ("-M",) if _tmux_supports_menu_mouse() else ()
    if sidebar_keybindings is None:
        try:
            skb = load_sidebar_keybindings()
        except SystemExit as error:
            diagnostics.error(error, operation="agent_menu_keybindings")
            skb = DEFAULT_SIDEBAR_KEYBINDINGS
    else:
        skb = sidebar_keybindings
    tmux.tmux(
        "display-menu", *mouse_flag, "-O", "-T", title, "-x", str(x), "-y", str(y), "-t", pane,
        "Kill", skb["kill"], f"send-keys -t {pane} {shlex.quote(skb['kill'])} y",
        timeout=None,
    )


@diagnostics.operation("cockpit.show_unavailable")
def show_unavailable(target: Target) -> None:
    pane = _require_right_pane()
    replacement = _record_attachment_replacement(pane, reason="unavailable")
    tmux.tmux("respawn-pane", "-k", "-t", pane, _unavailable_command(target))
    if replacement:
        diagnostics.log("attachment_replaced", **replacement)


@diagnostics.operation("cockpit.show_missing")
def show_missing(target: Target) -> None:
    pane = _require_right_pane()
    replacement = _record_attachment_replacement(pane, reason="missing")
    tmux.tmux("respawn-pane", "-k", "-t", pane, _missing_command(target))
    if replacement:
        diagnostics.log("attachment_replaced", **replacement)


@diagnostics.operation("cockpit.show_reconnecting")
def show_reconnecting(target: Target) -> None:
    pane = _require_right_pane()
    replacement = _record_attachment_replacement(pane, reason="reconnecting")
    tmux.tmux("respawn-pane", "-k", "-t", pane, _reconnecting_command(target))
    if replacement:
        diagnostics.log("attachment_replaced", **replacement)


def switch_reconnecting(target: Target) -> None:
    switch(target, _reconnecting_command(target))


def _parse_target_option(text: str) -> Target | None:
    if not text:
        return None
    try:
        return parse_target(text)
    except SystemExit as error:
        diagnostics.error(error, handling="ignored", operation="target_option_parse")
        return None


def _target_option(name: str) -> Target | None:
    return _parse_target_option(_option(name))


def _parse_status_snapshot(text: str) -> StatusSnapshot | None:
    fields = text.split("\t")
    if len(fields) != 8 or fields[7] != ".":
        return None
    active_pane, sidebar_pane, current_target, bell_target, current_agent, failed_target, failed_generation, _ = fields
    failure_target = _parse_target_option(failed_target)
    failure = (
        (failure_target, failed_generation)
        if failure_target is not None and failed_target == current_target and failed_generation.isdecimal()
        else None
    )
    return StatusSnapshot(
        _parse_target_option(current_target),
        _parse_target_option(bell_target),
        current_agent or None,
        not sidebar_pane or active_pane == sidebar_pane,
        failure,
    )


def status_snapshot() -> StatusSnapshot | None:
    text = tmux.out(
        "display-message", "-p", "-t", TARGET,
        f"#{{pane_id}}\t#{{{SIDEBAR_PANE_OPTION}}}\t#{{{CURRENT_TARGET_OPTION}}}\t#{{{BELL_TARGET_OPTION}}}\t#{{{CURRENT_AGENT_OPTION}}}\t#{{{ATTACHMENT_FAILURE_TARGET_OPTION}}}\t#{{{ATTACHMENT_FAILURE_GENERATION_OPTION}}}\t.",
        check=False,
    )
    return _parse_status_snapshot(text)


def current_target() -> Target | None:
    target = _target_option(CURRENT_TARGET_OPTION)
    pane = right_pane()
    if not pane:
        return target
    command = tmux.out("display-message", "-p", "-t", pane, "#{pane_start_command}", check=False)
    attach = r"(?:^| )tmux(?: -\S+(?: \S+)?)*+ (?:new-session(?: .*?)? -s [A-Za-z0-9_.-]+|(?:.* )?attach-session(?: .*?)? -t [A-Za-z0-9_.-]+)"
    inner_attach = rf"(?:^| )tmux (?:-L {re.escape(INNER_SERVER_SOCKET)}|-S (?:\S*/)?{re.escape(INNER_SERVER_SOCKET)})(?: -\S+(?: \S+)?)*+ (?:new-session(?: .*?)? -s|(?:.* )?attach-session(?: .*?)? -t) ([A-Za-z0-9_.-]+)"
    try:
        parts = shlex.split(command)
        parsed_command = command
        parsed_target = None
        if len(parts) >= 7 and parts[:3] == [sys.executable, "-m", "letee.attachment"] and parts[4:6] == ["--", "ssh"]:
            parsed_target = parse_target(parts[3])
            parsed_command = ""
        elif parts and parts[0] == "ssh":
            index = 1
            while index < len(parts):
                if parts[index] == "-o":
                    index += 2
                elif parts[index].startswith("-"):
                    index += 1
                else:
                    break
            if index < len(parts):
                parsed_command = " ".join(parts[index + 1:])
                if match := re.search(inner_attach, parsed_command):
                    parsed_target = Target("ssh", match.group(1), parts[index])
        if parsed_target is None and (match := re.search(inner_attach, parsed_command)):
            parsed_target = Target("local", match.group(1))
        if target:
            if parsed_target is not None and target != parsed_target:
                tmux.tmux("set-option", "-u", "-t", tmux.SESSION, CURRENT_TARGET_OPTION)
                return None
            if re.search(attach, parsed_command) and parsed_target is None:
                tmux.tmux("set-option", "-u", "-t", tmux.SESSION, CURRENT_TARGET_OPTION)
                return None
            return target
        return parsed_target
    except SystemExit as error:
        diagnostics.error(error, handling="ignored", operation="current_target_parse")
    return None


def current_agent() -> str | None:
    return _option(CURRENT_AGENT_OPTION) or None


def bell_target() -> Target | None:
    return _target_option(BELL_TARGET_OPTION)


def sidebar_active() -> bool:
    pane = _option(SIDEBAR_PANE_OPTION)
    return not pane or tmux.out("display-message", "-p", "-t", pane, "#{pane_active}", check=False) == "1"
