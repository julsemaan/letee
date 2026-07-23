from __future__ import annotations

import curses
import locale
import os
import socket
import textwrap
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from . import cockpit, sessions
from .discovery import AgentEntry, DiscoveryPoller, SessionSnapshot
from .config import load_hosts, load_sessions, load_status_timeout, save_sessions
from .names import PaneTarget, Target, validate_name


UI_POLL_INTERVAL_MS = 50
COCKPIT_BELL_POLL_INTERVAL = 0.5


@dataclass(frozen=True)
class Effect:
    kind: Literal["switch", "switch_pane", "add_switch", "create", "kill", "help", "save_favorites", "status", "quit"]
    target: Target | PaneTarget | None = None
    favorites: tuple[Target, ...] | None = None
    message: str = ""


@dataclass
class SidebarState:
    filter_text: str = ""
    filtering: bool = False
    adding: bool = False
    creation_host: str | None = None
    creation_text: str = ""
    selected_target: Target | None = None
    selected_index: int = 0
    selected_tracked: bool = False
    pending_selection: Target | None = None
    favorites: list[Target] = field(default_factory=list)
    status: str = ""
    status_deadline: float | None = None
    rang_bells: set[Target] = field(default_factory=set)
    scroll_offset: int | None = None
    focused_region: Literal["sessions", "agents"] = "sessions"
    agent_selected_index: int = 0
    selected_agent_key: tuple[PaneTarget, str] | None = None
    agent_states: dict[tuple[PaneTarget, str], str] = field(default_factory=dict)
    agent_alerts: set[tuple[PaneTarget, str]] = field(default_factory=set)
    agent_rows: int | None = None
    agent_ordering: Literal["priority", "session"] = "priority"


@dataclass(frozen=True)
class Entry:
    label: str
    kind: str  # section | header | host | session | unavailable
    target: Target | None = None
    host: str | None = None
    unavailable_favorite: bool = False
    tracked: bool = False
    shortcut_slot: int | None = None
    pane_target: PaneTarget | None = None
    agent_id: str | None = None
    status: str | None = None
    runtime_updated_at: datetime | None = None
    task_status_timestamp: datetime | None = None


_COLOR: dict[str, int] = {}


def _ascii() -> bool:
    enc = locale.getpreferredencoding(False).lower()
    return os.environ.get("MTMUX_ASCII") == "1" or "utf" not in enc


def _icons() -> dict[str, str]:
    if _ascii():
        return {"local": "*", "remote": "*", "local_header": "LOCAL", "remote_header": "SSH", "create": "+", "unavailable": "!", "selected": ">", "enter": "<-"}
    return {"local": "●", "remote": "◆", "local_header": "💻", "remote_header": "🌐", "create": "＋", "unavailable": "⚠", "selected": "›", "enter": "↵"}


def _init_colors() -> None:
    global _COLOR
    _COLOR = {}
    try:
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
        if getattr(curses, "COLORS", 0) >= 256:
            charcoal, teal, green, mint, orange, red = 233, 30, 36, 79, 214, 167
        else:
            charcoal, teal, green, mint, orange, red = (
                curses.COLOR_BLACK, curses.COLOR_CYAN, curses.COLOR_GREEN, curses.COLOR_CYAN,
                curses.COLOR_YELLOW, curses.COLOR_RED
            )
        pairs = {
            "title": (1, mint, charcoal, curses.A_BOLD),
            "active": (2, orange, -1, curses.A_BOLD),
            "local": (3, green, -1, curses.A_BOLD),
            "remote": (4, teal, -1, curses.A_BOLD),
            "create": (5, mint, -1, 0),
            "unavailable": (6, curses.COLOR_YELLOW, -1, curses.A_DIM),
            "danger": (7, curses.COLOR_RED, -1, 0),
            "hints": (8, teal, -1, curses.A_DIM),
            "add_entry": (9, charcoal, mint, curses.A_BOLD),
            "slot": (10, mint, -1, curses.A_BOLD | curses.A_REVERSE),
            "slot_active": (11, orange, -1, curses.A_BOLD | curses.A_REVERSE),
            "agent_working": (12, green, -1, 0),
            "agent_submitted": (13, teal, -1, 0),
            "agent_input_required": (14, red, -1, curses.A_BOLD),
            "agent_auth_required": (15, curses.COLOR_MAGENTA, -1, curses.A_BOLD),
            "agent_failed": (16, curses.COLOR_RED, -1, curses.A_BOLD),
            "agent_rejected": (17, curses.COLOR_RED, -1, curses.A_BOLD),
            "agent_completed": (18, mint, -1, 0),
            "agent_unknown": (19, curses.COLOR_YELLOW, -1, 0),
        }
        for name, (pair, fg, bg, attr) in pairs.items():
            curses.init_pair(pair, fg, bg)
            _COLOR[name] = curses.color_pair(pair) | attr
        _COLOR["section"] = _COLOR["create"] | curses.A_BOLD
    except curses.error:
        _COLOR = {}


def _color(name: str) -> int:
    return _COLOR.get(name, 0)


def _fade(attr: int) -> int:
    return attr | curses.A_DIM


def _pane_active() -> bool:
    return cockpit.sidebar_active()


def _target_status(target: Target, snapshot: SessionSnapshot) -> str | None:
    if target in snapshot.sessions:
        return None
    if target.kind == "ssh":
        if target.host not in snapshot.remotes:
            return "unavailable"
        source = snapshot.remotes[target.host]
        if source is None:
            return "connecting…"
        if not source.available:
            return "reconnecting…"
    return "unavailable"


def _entries(
    filter_text: str,
    snapshot: SessionSnapshot,
    favorites: list[Target] | None = None,
    adding: bool = False,
) -> list[Entry]:
    needle = filter_text.lower()
    adding = adding or favorites is None
    favorites = favorites or []
    hostname = socket.gethostname()
    if not adding:
        slots = {target: slot for slot, target in enumerate(favorites[:9], 1)}
        out = [Entry("Add session", "add"), Entry("", "spacer")]
        for target in favorites:
            status = _target_status(target, snapshot)
            out.append(Entry(
                target.session,
                "session",
                target,
                target.host or hostname,
                unavailable_favorite=status is not None,
                tracked=True,
                shortcut_slot=slots.get(target),
                status=status,
            ))
        if len(out) == 2:
            out.append(Entry("Press enter to add a session", "hint"))
        return out

    icons = _icons()
    out: list[Entry] = []
    local_kind = "host" if snapshot.local.available and not filter_text else "header"
    local_label = hostname if local_kind == "host" else f"{icons['local_header']} {hostname}"
    out.append(Entry(local_label, local_kind, host=""))
    if not snapshot.local.available:
        label = f"unavailable: {snapshot.local.error}" if snapshot.local.error else "unavailable"
        out.append(Entry(label, "unavailable", host=""))
    else:
        for target in snapshot.local.sessions:
            if target not in favorites and needle in target.session.lower():
                out.append(Entry(target.session, "session", target))

    for host, source in snapshot.remotes.items():
        available = source is not None and source.available
        host_kind = "host" if available and not filter_text else "header"
        host_label = host if host_kind == "host" else f"{icons['remote_header']} {host}"
        out.append(Entry(host_label, host_kind, host=host))
        if source is None:
            out.append(Entry("connecting…", "unavailable", host=host))
            continue
        if not source.available:
            label = f"reconnecting…: {source.error}" if source.error else "reconnecting…"
            out.append(Entry(label, "unavailable", host=host))
            continue
        for target in source.sessions:
            if target not in favorites and needle in target.session.lower():
                out.append(Entry(target.session, "session", target, host))
    return out


_STATUS_RANK: dict[str, int] = {
    "input-required": 0, "auth-required": 0,
    "failed": 1, "rejected": 1,
    "completed": 2, "canceled": 2,
    "working": 3, "submitted": 3,
    "idle": 4,
}


def _agent_sort_key(
    entry: Entry,
    favorites: list[Target],
    agent_ordering: str,
    agent_alerts: set[tuple[PaneTarget, str]] | None = None,
) -> tuple:
    target = entry.target
    session_index = favorites.index(target) if target and target in favorites else len(favorites)
    if agent_ordering == "session":
        return (0, session_index, 0, 0, entry.agent_id or "")
    status_rank = _STATUS_RANK.get(entry.status, 5)
    bell_rank = 0 if (entry.pane_target, entry.agent_id) in (agent_alerts or set()) else 1
    window = int(entry.pane_target.window_id[1:]) if entry.pane_target and entry.pane_target.window_id else 0
    pane = int(entry.pane_target.pane_id[1:]) if entry.pane_target and entry.pane_target.pane_id else 0
    return (bell_rank, status_rank, session_index, window, pane, entry.agent_id or "")


def _agent_entries(
    snapshot: SessionSnapshot,
    favorites: list[Target],
    agent_ordering: str = "priority",
    agent_alerts: set[tuple[PaneTarget, str]] | None = None,
) -> list[Entry]:
    tracked = set(favorites)
    entries = [
        Entry(
            agent.agent_name,
            "agent",
            agent.pane_target.target,
            agent.pane_target.target.host or socket.gethostname(),
            pane_target=agent.pane_target,
            agent_id=agent.agent_id,
            status=agent.task_state or "idle",
            runtime_updated_at=agent.runtime_updated_at,
            task_status_timestamp=agent.task_status_timestamp,
        )
        for agent in snapshot.agents
        if agent.pane_target.target in tracked
    ]
    entries.sort(key=lambda e: _agent_sort_key(e, favorites, agent_ordering, agent_alerts))
    return entries


def _focused_agent_id(snapshot: SessionSnapshot, current_target: Target | None, fallback: str | None) -> str | None:
    focused = {pane for pane in snapshot.focused_panes if pane.target == current_target}
    if not focused:
        return fallback
    return next((agent.agent_id for agent in snapshot.agents if agent.pane_target in focused), None)


def _update_agent_alerts(
    state: SidebarState, snapshot: SessionSnapshot, current_target: Target | None
) -> bool:
    attention_states = {"idle", "completed", "input-required", "auth-required", "failed", "rejected", "canceled"}
    tracked = set(state.favorites)
    agents = {
        (agent.pane_target, agent.agent_id): agent.task_state or "idle"
        for agent in snapshot.agents
        if agent.pane_target.target in tracked
    }
    active = {
        key
        for key in agents
        if key[0] in snapshot.focused_panes and key[0].target == current_target
    }
    new_alerts = {
        key for key, status in agents.items()
        if state.agent_states.get(key) == "working" and status in attention_states and key not in active
    }
    state.agent_alerts.intersection_update(agents)
    state.agent_alerts.difference_update(active)
    state.agent_alerts.update(new_alerts)
    state.agent_states = agents
    return bool(new_alerts)


def _selectable(entries: list[Entry]) -> list[int]:
    return [i for i, entry in enumerate(entries) if entry.kind in ("session", "host", "add")]


def _should_auto_create(entries: list[Entry]) -> bool:
    """True when exactly one host and no sessions — skip host selection step."""
    hosts = [e for e in entries if e.kind == "host"]
    sessions = [e for e in entries if e.kind == "session"]
    return len(hosts) == 1 and len(sessions) == 0


def _selected_index(entries: list[Entry], target: Target | None) -> int:
    if target:
        for i, entry in enumerate(entries):
            if entry.target == target:
                return i
    for kind in ("session", "host", "add"):
        for i, entry in enumerate(entries):
            if entry.kind == kind:
                return i
    return 0


def _target_index(entries: list[Entry], target: Target, tracked: bool = False) -> int | None:
    matches = [i for i, entry in enumerate(entries) if entry.target == target]
    if not matches:
        return None
    return next((i for i in matches if entries[i].tracked == tracked), matches[0])


def _sync_selection(state: SidebarState, entries: list[Entry]) -> None:
    if state.pending_selection is not None:
        index = _target_index(entries, state.pending_selection)
        if index is not None:
            state.selected_index = index
            state.selected_target = state.pending_selection
            state.selected_tracked = entries[index].tracked
            state.pending_selection = None
        return
    if state.selected_target is not None:
        index = _target_index(entries, state.selected_target, state.selected_tracked)
        if index is not None:
            state.selected_index = index
            state.selected_tracked = entries[index].tracked
            return
    choices = _selectable(entries)
    state.selected_index = min(choices, key=lambda index: abs(index - state.selected_index)) if choices else 0
    state.selected_target = entries[state.selected_index].target if choices else None
    state.selected_tracked = entries[state.selected_index].tracked if choices else False


def _sync_agent_selection(state: SidebarState, entries: list[Entry]) -> None:
    if state.selected_agent_key:
        for index, entry in enumerate(entries):
            if (entry.pane_target, entry.agent_id) == state.selected_agent_key:
                state.agent_selected_index = index
                return
    if entries:
        state.agent_selected_index = min(state.agent_selected_index, len(entries) - 1)
        entry = entries[state.agent_selected_index]
        state.selected_agent_key = (entry.pane_target, entry.agent_id) if entry.pane_target and entry.agent_id else None
    else:
        state.agent_selected_index = 0
        state.selected_agent_key = None


def _transition(
    state: SidebarState,
    action: str,
    target: Target | None = None,
    *,
    unavailable: bool = False,
) -> Effect | None:
    target = target or state.selected_target
    if action in ("switch", "add_switch", "kill"):
        return Effect(action, target=target) if target else None
    if action == "create":
        return Effect("create", target=target) if target else None
    if action == "toggle_session" and target:
        if target in state.favorites:
            state.favorites.remove(target)
            message = f"removed {target.format()}"
        else:
            state.favorites.append(target)
            message = f"added {target.format()}"
        state.selected_target = None if unavailable else target
        return Effect("save_favorites", favorites=tuple(state.favorites), message=message)
    if action in ("move_session_up", "move_session_down"):
        if not target or not state.selected_tracked or target not in state.favorites:
            return None
        index = state.favorites.index(target)
        offset = -1 if action == "move_session_up" else 1
        new_index = index + offset
        if not 0 <= new_index < len(state.favorites):
            edge = "first" if offset < 0 else "last"
            return Effect("status", message=f"already {edge} session")
        state.favorites[index], state.favorites[new_index] = state.favorites[new_index], state.favorites[index]
        direction = "up" if offset < 0 else "down"
        return Effect("save_favorites", favorites=tuple(state.favorites), message=f"moved {target.format()} {direction}")
    if action in ("help", "quit"):
        return Effect(action)
    return None


def _set_status(state: SidebarState, message: str, timeout: float) -> None:
    state.status = message
    state.status_deadline = time.monotonic() + timeout


def _execute(effect: Effect, state: SidebarState, poller: DiscoveryPoller, status_timeout: float) -> bool:
    try:
        if effect.kind in ("switch", "add_switch") and isinstance(effect.target, Target):
            if effect.kind == "add_switch" and effect.target not in state.favorites:
                state.favorites.append(effect.target)
                save_sessions(state.favorites)
            cockpit.switch(effect.target, sessions.attach_command(effect.target))
            state.filter_text = ""
            state.filtering = False
            state.selected_target = effect.target
            _set_status(state, f"switched {effect.target.format()}", status_timeout)
        elif effect.kind == "switch_pane" and isinstance(effect.target, PaneTarget):
            cockpit.switch(effect.target.target, sessions.pane_attach_command(effect.target), effect.message)
            state.selected_agent_key = (effect.target, effect.message)
            state.agent_alerts.discard(state.selected_agent_key)
            _set_status(state, f"switched {effect.target.target.format()}", status_timeout)
        elif effect.kind == "create" and isinstance(effect.target, Target):
            sessions.create(effect.target)
            if effect.target not in state.favorites:
                state.favorites.append(effect.target)
                save_sessions(state.favorites)
            cockpit.switch(effect.target, sessions.attach_command(effect.target))
            state.adding = False
            state.pending_selection = effect.target
            poller.refresh()
            _set_status(state, f"created {effect.target.session}", status_timeout)
        elif effect.kind == "kill" and effect.target:
            sessions.kill(effect.target)
            poller.discard(effect.target)
            poller.refresh()
            state.selected_target = effect.target
            _set_status(state, f"killed {effect.target.format()}", status_timeout)
        elif effect.kind == "help":
            cockpit.show_help()
            _set_status(state, "help opened", status_timeout)
        elif effect.kind == "save_favorites":
            save_sessions(effect.favorites or ())
            _set_status(state, effect.message, status_timeout)
        elif effect.kind == "status":
            _set_status(state, effect.message, status_timeout)
        elif effect.kind == "quit":
            return True
    except SystemExit as error:
        _set_status(state, str(error), status_timeout)
    return False


def _current_target() -> Target | None:
    return cockpit.current_target()


def _creation_key(state: SidebarState, key: int) -> Effect | None:
    if key in (27, 3):
        state.creation_host = None
        state.creation_text = ""
    elif key in (curses.KEY_BACKSPACE, 8, 127):
        state.creation_text = state.creation_text[:-1]
    elif key in (10, 13, curses.KEY_ENTER):
        name = validate_name(state.creation_text, "session")
        host = state.creation_host
        target = Target("local", name) if host == "" else Target("ssh", name, host)
        state.creation_host = None
        state.creation_text = ""
        return Effect("create", target)
    elif 32 <= key <= 126 and len(state.creation_text) < 64:
        state.creation_text += chr(key)
    return None


def _read_key(stdscr: curses.window, prompt: str) -> int:
    h, w = stdscr.getmaxyx()
    stdscr.timeout(-1)
    try:
        stdscr.addnstr(h - 1, 0, " " * (w - 1), w - 1)
        stdscr.addnstr(h - 1, 0, prompt, w - 1)
        stdscr.refresh()
        return stdscr.getch()
    finally:
        stdscr.addnstr(h - 1, 0, " " * (w - 1), w - 1)
        stdscr.refresh()
        stdscr.timeout(UI_POLL_INTERVAL_MS)


def _filter_key(filter_text: str, key: int) -> str | None:
    if key in (curses.KEY_BACKSPACE, 8, 127):
        return filter_text[:-1]
    if 32 <= key <= 126:
        return filter_text + chr(key)
    return None


def _bell_targets(
    snapshot: SessionSnapshot,
    cockpit_target: Target | None = None,
    favorites: list[Target] | tuple[Target, ...] | None = None,
) -> set[Target]:
    targets = set(snapshot.bells)
    if cockpit_target:
        targets.add(cockpit_target)
    return targets if favorites is None else targets & set(favorites)


def _entry_height(entry: Entry) -> int:
    if entry.kind == "order":
        return 1
    return 2 if entry.tracked or entry.kind == "agent" else 1


def _viewport(entries: list[Entry], selected: int, height: int, scroll_offset: int | None = None) -> tuple[int, int]:
    body = max(0, height - 2)
    if not entries or body <= 0:
        return selected, selected
    if body == 1:
        if scroll_offset is not None:
            start = max(0, min(scroll_offset, len(entries) - 1))
            return start, min(len(entries), start + 1)
        return selected, min(len(entries), selected + 1)

    if scroll_offset is not None:
        start = max(0, min(scroll_offset, len(entries) - 1))
        row_offsets = [0]
        for entry in entries:
            row_offsets.append(row_offsets[-1] + _entry_height(entry))
        end = start + 1
        while end < len(entries):
            rows = row_offsets[end + 1] - row_offsets[start]
            used = rows + int(start > 0) + int(end + 1 < len(entries))
            if used > body:
                break
            end += 1
        return start, end

    best = (selected, min(len(entries), selected + 1))
    best_score = (-1, -1, -1)
    row_offsets = [0]
    for entry in entries:
        row_offsets.append(row_offsets[-1] + _entry_height(entry))
    for start in range(selected + 1):
        for end in range(selected + 1, len(entries) + 1):
            rows = row_offsets[end] - row_offsets[start]
            used = rows + int(start > 0) + int(end < len(entries))
            if used <= body:
                score = (rows, end - start, -abs((start + end - 1) - 2 * selected))
                if score > best_score:
                    best, best_score = (start, end), score
    return best


def _entry_at_row(
    entries: list[Entry], selected: int, row: int, height: int, footer_height: int, top: int = 1,
    scroll_offset: int | None = None,
) -> int | None:
    content_height = height - footer_height - top + 2
    start, end = _viewport(entries, selected, content_height, scroll_offset)
    entry_row = row - top - int(start > 0)
    if entry_row < 0 or row >= height - footer_height:
        return None
    for index in range(start, end):
        if entry_row < _entry_height(entries[index]):
            return index if entries[index].kind in ("session", "host", "add", "agent", "order") else None
        entry_row -= _entry_height(entries[index])
    return None


def _mouse_mask() -> None:
    events = (
        getattr(curses, "BUTTON1_CLICKED", 0),
        getattr(curses, "BUTTON4_PRESSED", 0),
        getattr(curses, "BUTTON5_PRESSED", 0),
    )
    try:
        curses.mousemask(sum(event for event in events if isinstance(event, int)))
    except curses.error:
        pass


def _draw_title(
    stdscr: curses.window,
    w: int,
    entries: list[Entry],
    filter_text: str,
    filtering: bool = False,
    dimmed: bool = False,
    adding: bool = False,
) -> int:
    width = max(1, w)
    count = len({entry.target for entry in entries if entry.kind == "session" and not entry.unavailable_favorite})
    brand = " mtmux" if _ascii() else "  mtmux"
    left = f"{brand} / Add session" if adding else brand
    noun = ("match" if count == 1 else "matches") if filtering else ("session" if count == 1 else "sessions")
    right = f"{count} {noun}"
    title = f"{left}{right.rjust(width - len(left))}" if len(left) + len(right) < width else left
    attr = _color("title") or (curses.A_BOLD | curses.A_REVERSE)
    stdscr.addnstr(0, 0, title[:width].ljust(width), width, _fade(attr) if dimmed else attr)
    stdscr.redrawln(0, 1)
    return min(width - 1, len(left))


def _draw_filter(stdscr: curses.window, w: int, filter_text: str, dimmed: bool) -> tuple[int, int]:
    prefix = " Filter: "
    text = _truncate_cells(filter_text, max(0, w - _cell_width(prefix)))
    line = prefix + text
    attr = _color("hints") or curses.A_DIM
    stdscr.addnstr(1, 0, line.ljust(w), w, _fade(attr) if dimmed else attr)
    return 1, min(w - 1, _cell_width(line))


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    ellipsis = "..." if _ascii() else "…"
    return ellipsis[:width] if width <= len(ellipsis) else text[: width - len(ellipsis)] + ellipsis


def _cell_width(text: str) -> int:
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def _truncate_cells(text: str, width: int) -> str:
    if _cell_width(text) <= width:
        return text
    ellipsis = "..." if _ascii() else "…"
    kept = ""
    for char in text:
        if _cell_width(kept + char + ellipsis) > width:
            break
        kept += char
    return kept + ellipsis if kept else _truncate(ellipsis, width)


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 60 * 60:
        return f"{seconds // 60}m"
    if seconds < 24 * 60 * 60:
        return f"{seconds // (60 * 60)}h"
    return f"{seconds // (24 * 60 * 60)}d"


def _entry_lines(
    entry: Entry,
    selected: bool,
    bell_targets: set[Target],
    current_target: Target | None,
    width: int,
    creation_host: str | None = None,
    creation_text: str = "",
    now: datetime | None = None,
    agent_alerts: set[tuple[PaneTarget, str]] | None = None,
    agent_ordering: str = "priority",
) -> list[str]:
    icon = _icons()
    pointer = icon["selected"] if selected else " "
    if entry.kind == "section":
        rule = "-" if _ascii() else "─"
        if len(entry.label) + 1 >= width:
            return [_truncate(entry.label + " ", width)]
        return [entry.label + " " + rule * (width - len(entry.label) - 1)]
    if entry.kind == "header":
        return [_truncate(entry.label, width)]
    if entry.kind == "add":
        label = f"{pointer} {icon['create']} {entry.label}"
        truncated = _truncate_cells(label, width)
        return [truncated + " " * (width - _cell_width(truncated))]
    if entry.kind == "spacer":
        return [""]
    if entry.kind == "host":
        if creation_host is not None and entry.host == creation_host:
            prefix = f"{icon['create']} {entry.label} / new: "
            room = max(0, width - _cell_width(prefix))
            text = creation_text[-room:] if room else ""
            return [_truncate_cells(prefix, width - _cell_width(text)) + text]
        suffix = f" {icon['create']}"
        if selected:
            label = _truncate_cells(f"{pointer} {entry.label}", max(0, width - _cell_width(suffix) - 1))
        else:
            host_icon = icon["local_header"] if entry.host == "" else icon["remote_header"]
            label = _truncate_cells(f"{host_icon} {entry.label}", max(0, width - _cell_width(suffix) - 1))
        return [_truncate(label + suffix, width)]
    if entry.kind == "order":
        ico = _icons()
        pointer = ico["selected"] if selected else " "
        if _ascii():
            prefix = f"{pointer} Order:  "
            if agent_ordering == "priority":
                line = prefix + "PRIORITY  SESSION"
            else:
                line = prefix + "PRIORITY  SESSION"
        else:
            prefix = f"{pointer} Order:  "
            if agent_ordering == "priority":
                line = prefix + "Priority  Session"
            else:
                line = prefix + "Priority  Session"
        return [_truncate_cells(line, width)]
    if entry.kind == "agent":
        separator = " · "
        alert = " BELL" if _ascii() else " 🔔"
        alert = alert if (entry.pane_target, entry.agent_id) in (agent_alerts or set()) else ""
        prefix = f"{pointer} "
        status = entry.status or "unknown"
        timestamp = entry.task_status_timestamp or entry.runtime_updated_at
        detail = ""
        if status == "working" and timestamp:
            duration = _format_duration(((now or datetime.now(timezone.utc)) - timestamp).total_seconds())
            detail = f" · for {duration}"
        suffix = separator + status + detail + alert
        name = _truncate_cells(entry.label, max(0, width - _cell_width(prefix + suffix)))
        first = _truncate_cells(prefix + name + suffix, width)
        branch = "`-" if _ascii() else "└─"
        location_prefix = f"  {branch} "
        location = f"{entry.host} · {entry.target.session if entry.target else ''}"
        return [_truncate_cells(first, width), location_prefix + _truncate_cells(location, max(0, width - _cell_width(location_prefix)))]
    if entry.kind == "session":
        kind = "unavailable" if entry.unavailable_favorite else ("remote" if entry.target and entry.target.kind == "ssh" else "local")
        bell = " BELL" if _ascii() else " 🔔"
        bell = bell if entry.target in bell_targets and entry.target != current_target else ""
        if entry.tracked:
            prefix = "" if entry.shortcut_slot is not None else f"{pointer} "
            room = max(0, width - _cell_width(prefix) - _cell_width(bell))
            label = _truncate_cells(entry.label, room)
            first = prefix + label + bell
            host_prefix = "@" if entry.target and entry.target.kind == "ssh" else ""
            status = (entry.status or "unavailable").replace("…", "...") if _ascii() else (entry.status or "unavailable")
            suffix = f" {status}" if entry.unavailable_favorite else ""
            branch = "`-" if _ascii() else "└─"
            meta_prefix = f"  {branch} "
            host = _truncate_cells(entry.host or "", max(0, width - _cell_width(meta_prefix) - _cell_width(host_prefix) - _cell_width(suffix)))
            return [first, meta_prefix + host_prefix + host + suffix]
        prefix = f"{pointer} {icon[kind]} "
        first = prefix + _truncate_cells(entry.label, max(0, width - _cell_width(prefix) - _cell_width(bell))) + bell
        return [first]
    if entry.kind == "hint":
        return [_truncate_cells(f"  {icon['enter']} {entry.label}", width)]
    label = entry.label.replace("…", "...") if _ascii() else entry.label
    return [_truncate(f"  {icon['unavailable']} {label}", width)]


def _status_attr(status: str) -> int:
    if status == "working":
        return _color("agent_working") or 0
    if status == "submitted":
        return _color("agent_submitted") or 0
    if status in ("input-required", "auth-required", "failed", "rejected"):
        return (_color("agent_" + status.replace("-", "_")) or 0) | curses.A_BOLD
    if status == "completed":
        return _color("agent_completed") or 0
    if status in ("idle", "canceled"):
        return curses.A_DIM
    return _color("agent_unknown") or 0


def _entry_attr(entry: Entry, active: bool, dimmed: bool = False) -> int:
    if active:
        attr = _color("active") or curses.A_REVERSE
    elif entry.kind == "order":
        attr = _color("section") or curses.A_BOLD
    elif entry.kind == "section":
        attr = _color("section") or curses.A_BOLD
    elif entry.kind == "add":
        attr = _color("add_entry") or (curses.A_BOLD | curses.A_REVERSE)
    elif entry.kind in ("header", "host"):
        attr = curses.A_BOLD
    elif entry.unavailable_favorite:
        attr = _color("unavailable") or curses.A_DIM
    elif entry.kind == "session":
        attr = _color("local")
    elif entry.kind == "agent":
        attr = 0
    elif entry.kind == "hint":
        attr = _color("hints") or curses.A_DIM
    elif entry.kind == "unavailable":
        attr = _color("unavailable") or curses.A_DIM
    else:
        attr = 0
    return _fade(attr) if dimmed else attr


def _view_index(
    entries: list[Entry], selected: int, current_target: Target | None, dimmed: bool
) -> int:
    if not dimmed or current_target is None:
        return selected
    if 0 <= selected < len(entries) and entries[selected].target == current_target:
        return selected
    matches = [i for i, entry in enumerate(entries) if entry.target == current_target]
    return next((i for i in matches if entries[i].tracked), matches[0] if matches else selected)


def _draw_entries(
    stdscr: curses.window,
    entries: list[Entry],
    selected: int,
    h: int,
    w: int,
    bell_targets: set[Target],
    current_target: Target | None,
    dimmed: bool = False,
    creation_host: str | None = None,
    creation_text: str = "",
    top: int = 1,
    scroll_offset: int | None = None,
    active_agent_id: str | None = None,
    now: datetime | None = None,
    agent_alerts: set[tuple[PaneTarget, str]] | None = None,
    agent_ordering: str = "priority",
) -> tuple[int, int] | None:
    cursor = None
    view_index = _view_index(entries, selected, current_target, dimmed)
    start, end = _viewport(entries, view_index, h - top + 1, scroll_offset)
    row = top
    if start:
        attr = _color("hints") or curses.A_DIM
        stdscr.addnstr(row, 0, "↑ more", w - 1, _fade(attr) if dimmed else attr)
        row += 1
    for idx in range(start, end):
        if row >= h - 1:
            break
        entry = entries[idx]
        selected_entry = idx == selected
        active_entry = entry.target is not None and entry.target == current_target
        active_agent = entry.kind == "agent" and entry.agent_id == active_agent_id
        lines = _entry_lines(
            entry, selected_entry and not dimmed, bell_targets, current_target, w,
            creation_host, creation_text, now, agent_alerts, agent_ordering,
        )
        focused_entry = selected_entry and entry.kind in ("agent", "host") and not dimmed
        base_attr = _entry_attr(entry, active_entry or active_agent or focused_entry, dimmed)
        slot_badge = ""
        slot_width = 0
        if entry.tracked and entry.shortcut_slot is not None:
            slot_width = 4
            ico = _icons()
        for line_number, line in enumerate(lines):
            if row >= h - 1:
                break
            attr = _fade(base_attr) if line_number and not (active_entry or active_agent or focused_entry) else base_attr
            if line_number == 0 and slot_width:
                if selected_entry and not dimmed:
                    slot_badge = f" {ico['selected']} "
                else:
                    slot_badge = f"[{entry.shortcut_slot}]"
                slot_attr = _color("slot_active") if active_entry else _color("slot")
                stdscr.addnstr(row, 0, slot_badge, w, slot_attr or curses.A_BOLD)
                stdscr.addnstr(row, 3, " " + line, w - 3, attr)
            else:
                stdscr.addnstr(row, 0, line, w, attr)
                if entry.kind == "agent" and line_number == 0 and entry.status:
                    column = line.rfind(entry.status)
                    if column >= 0:
                        status_attr = _status_attr(entry.status)
                        stdscr.addnstr(row, column, entry.status, max(0, w - column), _fade(status_attr) if dimmed else status_attr)
            if entry.kind == "order" and line_number == 0:
                active_word = "PRIORITY" if _ascii() else "Priority"
                inactive_word = "SESSION" if _ascii() else "Session"
                if agent_ordering == "session":
                    active_word, inactive_word = inactive_word, active_word
                active_attr = _color("active") or curses.A_REVERSE
                col = line.find(active_word)
                if col >= 0:
                    stdscr.addnstr(row, col, active_word, max(0, w - col), _fade(active_attr) if dimmed else active_attr)
            if entry.kind == "host" and entry.host == creation_host:
                cursor = (row, min(w - 1, _cell_width(line)))
            row += 1
    if end < len(entries) and row < h - 1:
        attr = _color("hints") or curses.A_DIM
        stdscr.addnstr(row, 0, "↓ more", w - 1, _fade(attr) if dimmed else attr)
    return cursor


def _draw_footer(
    stdscr: curses.window,
    h: int,
    w: int,
    filtering: bool = False,
    dimmed: bool = False,
    creating: bool = False,
    adding: bool = False,
) -> int:
    if creating:
        logical_rows = ["Esc cancel · Enter create" if not _ascii() else "Esc cancel  Enter create"]
    elif filtering:
        logical_rows = ["type to filter  backspace edit", f"esc clear  {'Enter' if _ascii() else '↵'} switch"]
    elif adding:
        logical_rows = [f"{'Enter' if _ascii() else '↵'} add  x kill", "/ filter  Esc back  ? help  q quit"]
    else:
        logical_rows = [f"{'Enter' if _ascii() else '↵'} activate  ? help  q quit"]
    width = max(1, w - 1)
    lines = [line for logical_row in logical_rows for line in (textwrap.wrap(logical_row, width=width) or [""])]
    attr = _color("title") or (curses.A_BOLD | curses.A_REVERSE)
    for row, line in enumerate(lines, h - len(lines)):
        row_attr = _fade(attr) if dimmed else attr
        stdscr.addnstr(row, 0, line.ljust(w - 1), w - 1, row_attr)
        stdscr.chgat(row, w - 1, 1, row_attr)
    return len(lines)


def _draw(
    stdscr: curses.window,
    entries: list[Entry],
    selected: int,
    status: str,
    filter_text: str,
    filtering: bool = False,
    bell_targets: set[Target] | None = None,
    current_target: Target | None = None,
    dimmed: bool = False,
    creation_host: str | None = None,
    creation_text: str = "",
    adding: bool = False,
    scroll_offset: int | None = None,
    agent_entries: list[Entry] | None = None,
    agent_selected: int = 0,
    focused_region: str = "sessions",
    agent_rows: int | None = None,
    active_agent_id: str | None = None,
    now: datetime | None = None,
    agent_alerts: set[tuple[PaneTarget, str]] | None = None,
    agent_ordering: str = "priority",
) -> int:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    cursor = _draw_title(stdscr, w, entries, filter_text, filtering, dimmed, adding)
    if filtering:
        cursor = _draw_filter(stdscr, w, filter_text, dimmed)
    footer_height = _draw_footer(stdscr, h, w, filtering, dimmed, creation_host is not None, adding)
    if agent_entries is None:
        creation_cursor = _draw_entries(
            stdscr, entries, selected, h - footer_height + 1, w, bell_targets or set(), current_target,
            dimmed, creation_host, creation_text, 2 if filtering else 1, scroll_offset,
        )
        if creation_cursor:
            stdscr.move(*creation_cursor)
        elif filtering:
            stdscr.move(*cursor)
        stdscr.refresh()
        return footer_height
    footer_top = h - footer_height
    agents = agent_entries
    has_real_agents = any(e.kind == "agent" for e in agents) if agents else False
    minimum_agent_rows = 1 + (2 if has_real_agents else 1)
    session_top = 2 if filtering else 1
    if footer_top - session_top < 2 + minimum_agent_rows:
        stdscr.addnstr(session_top, 0, "terminal too short", max(0, w - 1), curses.A_BOLD)
        stdscr.refresh()
        return footer_height
    available = footer_top - session_top - 1
    wanted = agent_rows if agent_rows is not None else max(minimum_agent_rows, round(available * 0.4))
    agent_body = min(max(minimum_agent_rows, wanted), available - 1)
    separator = footer_top - agent_body - 1
    creation_cursor = _draw_entries(
        stdscr, entries, selected, separator + 1, w, bell_targets or set(), current_target,
        dimmed or focused_region != "sessions", creation_host, creation_text, session_top, scroll_offset,
    )
    rule = "-" if _ascii() else "─"
    label = "AGENTS "
    stdscr.addnstr(separator, 0, label + rule * max(0, w - len(label)), w, _color("section") or curses.A_BOLD)
    if has_real_agents:
        _draw_entries(
            stdscr, agents, agent_selected, footer_top + 1, w, set(), None,
            dimmed or focused_region != "agents", top=separator + 1,
            active_agent_id=active_agent_id, now=now, agent_alerts=agent_alerts,
            agent_ordering=agent_ordering,
        )
    else:
        # Render order row then "No active agents"
        _draw_entries(
            stdscr, agents, 0, footer_top + 1, w, set(), None,
            dimmed or focused_region != "agents", top=separator + 1,
            agent_ordering=agent_ordering,
        )
        stdscr.addnstr(separator + 2, 0, "  No active agents", max(0, w - 1), curses.A_DIM)
    if creation_cursor:
        stdscr.move(*creation_cursor)
    elif filtering:
        stdscr.move(*cursor)
    stdscr.refresh()
    return footer_height


def run(stdscr: curses.window) -> None:
    _init_colors()
    curses.curs_set(0)
    _mouse_mask()
    status_timeout = load_status_timeout()
    state = SidebarState(favorites=load_sessions(), selected_target=_current_target())
    poller = DiscoveryPoller(load_hosts())
    entries = _entries(state.filter_text, poller.snapshot, state.favorites, state.adding)
    agent_entries = _agent_entries(poller.snapshot, state.favorites)
    agent_entries = [Entry("", "order")] + agent_entries
    _update_agent_alerts(state, poller.snapshot, state.selected_target)
    state.selected_index = _selected_index(entries, state.selected_target)
    _sync_selection(state, entries)
    cockpit_bell_target: Target | None = None
    active_agent_id: str | None = None
    next_cockpit_bell_poll = 0.0
    rendered: tuple[object, ...] | None = None
    footer_height = 0
    stdscr.timeout(UI_POLL_INTERVAL_MS)

    def show_status(message: str) -> None:
        _set_status(state, message, status_timeout)

    def rebuild() -> None:
        nonlocal entries, agent_entries
        entries = _entries(state.filter_text, poller.snapshot, state.favorites, state.adding)
        raw_agents = _agent_entries(poller.snapshot, state.favorites, state.agent_ordering, state.agent_alerts)
        # ponytail: order row is always index 0; rebuild prepends it so draw and navigation see same list
        agent_entries = [Entry("", "order")] + raw_agents
        _sync_selection(state, entries)
        _sync_agent_selection(state, agent_entries)
        state.scroll_offset = None

    try:
        while True:
            now = time.monotonic()
            if state.status_deadline is not None and now >= state.status_deadline:
                state.status = ""
                state.status_deadline = None
            current_target = _current_target()
            active_remote_host = current_target.host if current_target and current_target.kind == "ssh" else None
            agent_alert = False
            if poller.tick(active_remote_host):
                agent_alert = _update_agent_alerts(state, poller.snapshot, current_target)
                rebuild()
            selectable = _selectable(entries)
            if selectable and state.selected_index not in selectable:
                state.selected_index = selectable[0]
            if now >= next_cockpit_bell_poll:
                cockpit_bell_target = cockpit.bell_target()
                active_agent_id = cockpit.current_agent()
                next_cockpit_bell_poll = now + COCKPIT_BELL_POLL_INTERVAL
            bell_targets = _bell_targets(poller.snapshot, cockpit_bell_target, state.favorites)
            active_agent_id = _focused_agent_id(poller.snapshot, current_target, active_agent_id)
            visible_bells = bell_targets - ({current_target} if current_target else set())
            if visible_bells - state.rang_bells or agent_alert:
                curses.beep()
            state.rang_bells = bell_targets
            dimmed = not _pane_active()
            render_state = (
                tuple(entries), state.selected_index, state.status, state.filter_text,
                state.filtering, state.adding, state.creation_host, state.creation_text,
                frozenset(bell_targets), current_target, dimmed, stdscr.getmaxyx(),
                state.scroll_offset, tuple(agent_entries), state.agent_selected_index,
                state.focused_region, state.agent_rows, active_agent_id,
                frozenset(state.agent_alerts), int(time.time()) if agent_entries else None,
                state.agent_ordering,
            )
            if render_state != rendered:
                footer_height = _draw(
                    stdscr, entries, state.selected_index, state.status, state.filter_text,
                    state.filtering, bell_targets, current_target, dimmed,
                    state.creation_host, state.creation_text, state.adding, state.scroll_offset,
                    agent_entries, state.agent_selected_index, state.focused_region, state.agent_rows,
                    active_agent_id, agent_alerts=state.agent_alerts, agent_ordering=state.agent_ordering,
                )
                rendered = render_state
            try:
                key = stdscr.getch()
            except KeyboardInterrupt:
                if not state.filtering and not state.adding and state.creation_host is None:
                    raise
                key = 3
            if key == -1:
                continue
            if state.creation_host is not None:
                try:
                    effect = _creation_key(state, key)
                except SystemExit as error:
                    show_status(str(error))
                    continue
                if state.creation_host is None:
                    curses.curs_set(0)
                    if effect:
                        _execute(effect, state, poller, status_timeout)
                        rebuild()
                    else:
                        show_status("cancelled")
                continue
            if key == curses.KEY_MOUSE:
                try:
                    _, mouse_col, row, _, mouse_state = curses.getmouse()
                except (curses.error, TypeError, ValueError):
                    continue
                if not isinstance(row, int) or not isinstance(mouse_state, int):
                    continue
                if mouse_state & (getattr(curses, "BUTTON4_PRESSED", 0) or 0):
                    if state.scroll_offset is None:
                        view_index = _view_index(entries, state.selected_index, current_target, dimmed)
                        start, _ = _viewport(entries, view_index, stdscr.getmaxyx()[0] - footer_height)
                        state.scroll_offset = start
                    state.scroll_offset = max(0, state.scroll_offset - 1)
                    continue
                elif mouse_state & (getattr(curses, "BUTTON5_PRESSED", 0) or 0):
                    if state.scroll_offset is None:
                        view_index = _view_index(entries, state.selected_index, current_target, dimmed)
                        start, _ = _viewport(entries, view_index, stdscr.getmaxyx()[0] - footer_height)
                        state.scroll_offset = start
                    # ponytail: compute max scroll by finding last start that fits one entry
                    body = max(1, stdscr.getmaxyx()[0] - footer_height - 2)
                    row_offsets = [0]
                    for entry in entries:
                        row_offsets.append(row_offsets[-1] + _entry_height(entry))
                    total = row_offsets[-1]
                    max_offset = 0
                    for i in range(len(entries)):
                        if total - row_offsets[i] <= body:
                            max_offset = i
                            break
                    state.scroll_offset = min(max_offset, state.scroll_offset + 1)
                    continue
                else:
                    h = stdscr.getmaxyx()[0]
                    footer_top = h - footer_height
                    session_top = 2 if state.filtering else 1
                    has_agents = any(e.kind == "agent" for e in agent_entries)
                    minimum_agent_rows = 1 + (2 if has_agents else 1)
                    available = footer_top - session_top - 1
                    wanted = state.agent_rows if state.agent_rows is not None else max(minimum_agent_rows, round(available * 0.4))
                    agent_body = min(max(minimum_agent_rows, wanted), max(minimum_agent_rows, available - 1))
                    separator = footer_top - agent_body - 1
                    if separator < row < footer_top and not state.adding and agent_entries:
                        index = _entry_at_row(agent_entries, state.agent_selected_index, row, footer_top + 1, 0, separator + 1)
                        if index is None:
                            continue
                        state.focused_region = "agents"
                        state.agent_selected_index = index
                        entry = agent_entries[index]
                        state.selected_agent_key = (entry.pane_target, entry.agent_id) if entry.pane_target and entry.agent_id else None
                        if mouse_state & (getattr(curses, "BUTTON1_CLICKED", 0) or 0) and entry.kind == "order":
                            # ponytail: column math on rendered string; fixed offsets per word position
                            prefix = "> Order:  " if _ascii() else "› Order:  "
                            pri_word = "PRIORITY" if _ascii() else "Priority"
                            ses_word = "SESSION" if _ascii() else "Session"
                            pri_start = _cell_width(prefix)
                            ses_start = pri_start + _cell_width(pri_word) + 2
                            if isinstance(mouse_col, int):
                                if pri_start <= mouse_col < pri_start + _cell_width(pri_word):
                                    state.agent_ordering = "priority"
                                    rebuild()
                                elif ses_start <= mouse_col < ses_start + _cell_width(ses_word):
                                    state.agent_ordering = "session"
                                    rebuild()
                            continue
                        if mouse_state & (getattr(curses, "BUTTON1_CLICKED", 0) or 0) and entry.pane_target:
                            _execute(Effect("switch_pane", entry.pane_target, message=entry.agent_id or ""), state, poller, status_timeout)
                        continue
                    view_index = _view_index(entries, state.selected_index, current_target, dimmed)
                    index = _entry_at_row(
                        entries, view_index, row, stdscr.getmaxyx()[0], footer_height,
                        2 if state.filtering else 1,
                        state.scroll_offset,
                    )
                    if index is None:
                        continue
                    state.focused_region = "sessions"
                    state.selected_index = index
                    state.selected_target = entries[index].target
                    state.selected_tracked = entries[index].tracked
                    if mouse_state & (getattr(curses, "BUTTON1_CLICKED", 0) or 0):
                        key = curses.KEY_ENTER
                    else:
                        continue
            if state.filtering:
                if key in (27, 3):
                    state.filter_text = ""
                    state.filtering = False
                    curses.curs_set(0)
                    rebuild()
                    show_status("cancelled" if key == 3 else "filter cleared")
                    continue
                new_filter = _filter_key(state.filter_text, key)
                if new_filter is not None:
                    state.filter_text = new_filter
                    rebuild()
                    if not state.filter_text:
                        show_status("filter cleared")
                    continue
            if key in (27, 3) and state.adding:
                state.adding = False
                state.filter_text = ""
                rebuild()
                show_status("cancelled")
                continue
            selectable = _selectable(entries)
            effect: Effect | None = None
            if key == ord("q"):
                effect = _transition(state, "quit")
            elif key == 9 and not state.adding:
                state.focused_region = "agents" if state.focused_region == "sessions" else "sessions"
            elif key == ord("["):
                state.agent_rows = (state.agent_rows or max(2, round(stdscr.getmaxyx()[0] * 0.4))) + 1
            elif key == ord("]"):
                state.agent_rows = max(1, (state.agent_rows or max(2, round(stdscr.getmaxyx()[0] * 0.4))) - 1)
            elif state.focused_region == "agents" and key in map(ord, "hl") and state.agent_selected_index == 0:
                state.agent_ordering = "session" if state.agent_ordering == "priority" else "priority"
                rebuild()
            elif state.focused_region == "agents" and key in (curses.KEY_LEFT, curses.KEY_RIGHT) and state.agent_selected_index == 0:
                state.agent_ordering = "session" if state.agent_ordering == "priority" else "priority"
                rebuild()
            elif state.focused_region == "agents" and key in (curses.KEY_DOWN, ord("j")) and agent_entries:
                state.agent_selected_index = (state.agent_selected_index + 1) % len(agent_entries)
                entry = agent_entries[state.agent_selected_index]
                state.selected_agent_key = (entry.pane_target, entry.agent_id) if entry.pane_target and entry.agent_id else None
            elif state.focused_region == "agents" and key in (curses.KEY_UP, ord("k")) and agent_entries:
                state.agent_selected_index = (state.agent_selected_index - 1) % len(agent_entries)
                entry = agent_entries[state.agent_selected_index]
                state.selected_agent_key = (entry.pane_target, entry.agent_id) if entry.pane_target and entry.agent_id else None
            elif state.focused_region == "agents" and key in (10, 13, curses.KEY_ENTER):
                if agent_entries:
                    entry = agent_entries[state.agent_selected_index]
                    if entry.pane_target:
                        effect = Effect("switch_pane", entry.pane_target, message=entry.agent_id or "")
            elif state.focused_region == "agents" and key in map(ord, "arxKJ/"):
                effect = Effect("status", message="agent panes are automatic")
            elif key in (curses.KEY_DOWN, ord("j")) and selectable:
                state.scroll_offset = None
                state.selected_index = selectable[(selectable.index(state.selected_index) + 1) % len(selectable)]
                state.selected_target = entries[state.selected_index].target
                state.selected_tracked = entries[state.selected_index].tracked
            elif key in (curses.KEY_UP, ord("k")) and selectable:
                state.scroll_offset = None
                state.selected_index = selectable[(selectable.index(state.selected_index) - 1) % len(selectable)]
                state.selected_target = entries[state.selected_index].target
                state.selected_tracked = entries[state.selected_index].tracked
            elif key == ord("K"):
                effect = _transition(state, "move_session_up")
            elif key == ord("J"):
                effect = _transition(state, "move_session_down")
            elif key == ord("a") and not state.adding:
                state.adding = True
                state.selected_target = None
                rebuild()
                if _should_auto_create(entries):
                    state.creation_host = next(e for e in entries if e.kind == "host").host
                    state.creation_text = ""
                    curses.curs_set(1)
            elif key == ord("r") and not state.adding:
                entry = entries[state.selected_index]
                if entry.target:
                    effect = _transition(state, "toggle_session", entry.target)
            elif key == ord("/"):
                if not state.adding:
                    state.adding = True
                    state.selected_target = None
                    rebuild()
                state.filtering = True
                curses.curs_set(1)
            elif key == ord("?"):
                effect = _transition(state, "help")
            elif key in (10, 13, curses.KEY_ENTER):
                state.scroll_offset = None
                entry = entries[state.selected_index]
                if entry.kind == "add":
                    state.adding = True
                    state.selected_target = None
                    rebuild()
                    if _should_auto_create(entries):
                        state.creation_host = next(e for e in entries if e.kind == "host").host
                        state.creation_text = ""
                        curses.curs_set(1)
                    continue
                if entry.target:
                    effect = _transition(state, "add_switch" if state.adding else "switch", entry.target)
                    if state.adding:
                        state.adding = False
                elif entry.kind == "host":
                    state.creation_host = entry.host
                    state.creation_text = ""
                    curses.curs_set(1)
            elif key == ord("x"):
                entry = entries[state.selected_index]
                if not entry.target:
                    show_status("select session to kill")
                    continue
                if entry.unavailable_favorite:
                    show_status(f"missing {entry.target.format()}")
                    continue
                if _read_key(stdscr, f"kill {entry.target.format()}? y/N") != ord("y"):
                    show_status("cancelled")
                    continue
                effect = _transition(state, "kill", entry.target)
            if effect:
                if _execute(effect, state, poller, status_timeout):
                    return
                if effect.kind in ("switch", "create"):
                    curses.curs_set(0)
                rebuild()
    finally:
        poller.close()


def main() -> int:
    while True:
        try:
            curses.wrapper(run)
            return 0
        except KeyboardInterrupt:
            pass
