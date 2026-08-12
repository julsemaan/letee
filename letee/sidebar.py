from __future__ import annotations

import curses
import locale
import os
import socket
import subprocess
import textwrap
import threading
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from . import cockpit, sessions
from .discovery import DiscoveryPoller, SessionSnapshot
from .config import load_hosts, load_sessions, load_status_timeout, save_sessions
from .names import PaneTarget, Target, validate_name


UI_POLL_INTERVAL_MS = 50
MOVE_SCROLL_INTERVAL = 0.2
LAYOUT_REPAIR_INTERVAL = 0.5
STATUS_POLL_INTERVAL = 0.1
UNICODE_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ASCII_SPINNER = "|/-\\"
UNICODE_STATUS_ICONS = {
    "submitted": "◷", "idle": "○", "completed": "✓", "input-required": "?",
    "auth-required": "⚿", "failed": "✕", "rejected": "⊘", "canceled": "−", "unknown": "?",
}
ASCII_STATUS_ICONS = {
    "submitted": ".", "idle": "o", "completed": "+", "input-required": "?",
    "auth-required": "@", "failed": "x", "rejected": "!", "canceled": "-", "unknown": "?",
}
_PREFIX_ACTION_KEYS = {
    "add": curses.KEY_F11,
    "remove": curses.KEY_F8,
    "kill": curses.KEY_F9,
    "alert": curses.KEY_F10,
}
_PREFIX_ACTIONS = {key: action for action, key in _PREFIX_ACTION_KEYS.items()}


@dataclass(frozen=True)
class Effect:
    kind: Literal[
        "switch", "switch_pane", "add_switch", "create", "kill", "rename",
        "save_favorites", "status", "show_reconnecting", "show_missing",
        "show_unavailable",
    ]
    target: Target | PaneTarget | None = None
    favorites: tuple[Target, ...] | None = None
    message: str = ""
    automatic: bool = False


@dataclass
class SidebarState:
    filter_text: str = ""
    filtering: bool = False
    add_view: Literal["choice", "existing", "location", "name"] | None = None
    creation_host: str | None = None
    creation_text: str = ""
    rename_target: Target | None = None
    selected_target: Target | None = None
    selected_index: int = 0
    selected_tracked: bool = False
    pending_selection: Target | None = None
    favorites: list[Target] = field(default_factory=list)
    status: str = ""
    status_region: Literal["sessions", "agents"] = "sessions"
    status_deadline: float | None = None
    rang_bells: set[Target] = field(default_factory=set)
    scroll_offset: int | None = None
    focused_region: Literal["sessions", "agents"] = "sessions"
    agent_selected_index: int = 0
    selected_agent_key: tuple[PaneTarget, str] | None = None
    agent_states: dict[tuple[PaneTarget, str], str] = field(default_factory=dict)
    agent_alerts: set[tuple[PaneTarget, str]] = field(default_factory=set)
    agent_rows: int | None = None
    add_button_selected: bool = False
    move_source: Target | None = None
    move_target: Target | None = None


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


@dataclass(frozen=True)
class EffectResult:
    effect: Effect
    favorites: tuple[Target, ...]
    error: str = ""
    stale_navigation: bool = False


@dataclass(frozen=True)
class StatusResult:
    snapshot: SessionSnapshot
    current_target: Target | None
    bell_target: Target | None
    current_agent: str | None
    pane_active: bool
    generation: int
    refreshed: bool = False


_COLOR: dict[str, int] = {}


def _ascii() -> bool:
    enc = locale.getpreferredencoding(False).lower()
    return os.environ.get("LETEE_ASCII") == "1" or "utf" not in enc


def _icons() -> dict[str, str]:
    if _ascii():
        return {"local": "*", "remote": "*", "local_header": "LOCAL", "remote_header": "SSH", "create": "+", "unavailable": "!", "selected": ">", "enter": "<-"}
    return {"local": "●", "remote": "◆", "local_header": "💻", "remote_header": "🌐", "create": "＋", "unavailable": "⚠", "selected": "›", "enter": "↵"}


def _spinner_frame(now: float) -> str:
    frames = ASCII_SPINNER if _ascii() else UNICODE_SPINNER
    return frames[int(now * 10) % len(frames)]


def _status_icon(status: str, now: float) -> str:
    if status == "working":
        return _spinner_frame(now)
    icons = ASCII_STATUS_ICONS if _ascii() else UNICODE_STATUS_ICONS
    return icons.get(status, icons["unknown"])


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
            "move": (20, teal, -1, curses.A_BOLD),
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
    if target.kind == "local":
        return "missing" if snapshot.local.available else "unavailable"
    if target.host not in snapshot.remotes:
        return "unavailable"
    source = snapshot.remotes[target.host]
    if source is None:
        return "connecting…"
    if not source.available:
        return "reconnecting…"
    return "missing"


def _should_show_unavailable(target: Target, snapshot: SessionSnapshot) -> bool:
    return _target_status(target, snapshot) not in (None, "connecting…")


def _sync_active_session(
    target: Target | None,
    snapshot: SessionSnapshot,
    interrupted: Target | None,
    submit: Callable[[Effect], bool] | None = None,
) -> Target | None:
    if target is None:
        return None

    def run(effect: Effect) -> bool:
        if submit is not None:
            return bool(submit(effect))
        result = _perform_effect(effect, ())
        if result.error:
            raise SystemExit(result.error)
        return not result.stale_navigation

    def marker_after_submit(next_marker: Target | None) -> Target | None:
        return interrupted if submit is not None else next_marker

    status = _target_status(target, snapshot)
    if status in ("connecting…", "reconnecting…"):
        if interrupted != target and not run(Effect("show_reconnecting", target, automatic=True)):
            return interrupted
        return marker_after_submit(target)
    if status is None and interrupted == target:
        return interrupted if not run(Effect("switch", target, automatic=True)) else marker_after_submit(None)
    if status == "missing" and interrupted != target:
        return marker_after_submit(target) if run(Effect("show_missing", target, automatic=True)) else interrupted
    if status == "unavailable" and interrupted != target:
        return marker_after_submit(target) if run(Effect("show_unavailable", target, automatic=True)) else interrupted
    return interrupted if interrupted == target else None


def _reconcile_active_session_effect(
    unavailable_target_shown: Target | None,
    result: EffectResult,
) -> Target | None:
    if result.error or result.stale_navigation or not result.effect.automatic:
        return unavailable_target_shown
    target = result.effect.target
    if not isinstance(target, Target):
        return unavailable_target_shown
    if result.effect.kind in ("show_reconnecting", "show_missing", "show_unavailable"):
        return target
    if result.effect.kind == "switch":
        return None
    return unavailable_target_shown


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
        out: list[Entry] = []
        for target in favorites:
            status = _target_status(target, snapshot)
            out.append(Entry(
                target.session,
                "session",
                target,
                target.host or "localhost",
                unavailable_favorite=status is not None,
                tracked=True,
                shortcut_slot=slots.get(target),
                status=status,
            ))
        return out or [
            Entry("No sessions yet", "empty"),
            Entry("Press Enter to add one.", "hint"),
        ]

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


def _available_locations(snapshot: SessionSnapshot) -> list[tuple[str, str]]:
    locations = [("localhost", "")] if snapshot.local.available else []
    locations.extend(
        (host, host) for host, source in snapshot.remotes.items()
        if source is not None and source.available
    )
    return locations


def _add_entries(
    view: Literal["choice", "existing", "location"],
    filter_text: str,
    snapshot: SessionSnapshot,
    favorites: list[Target],
) -> list[Entry]:
    if view == "choice":
        return [Entry("New session", "choice_new"), Entry("Existing session", "choice_existing")]
    if view == "existing":
        grouped: list[Entry] = []
        entries = _entries(filter_text, snapshot, favorites, adding=True)
        for index, entry in enumerate(entries):
            if entry.kind in ("host", "header"):
                label = "localhost" if entry.host == "" else entry.label
                grouped.append(Entry(label, "header", host=entry.host))
                if index + 1 == len(entries) or entries[index + 1].kind in ("host", "header"):
                    grouped.append(Entry("No sessions", "empty", host=entry.host))
            else:
                grouped.append(entry)
        return grouped
    entries = [Entry("Select where to create", "section")]
    entries.extend(Entry(label, "location", host=host) for label, host in _available_locations(snapshot))
    if not snapshot.local.available:
        entries.append(Entry("localhost: unavailable", "unavailable", host=""))
    for host, source in snapshot.remotes.items():
        if source is None or not source.available:
            entries.append(Entry(f"{host}: unavailable", "unavailable", host=host))
    if not entries or not any(entry.kind == "location" for entry in entries):
        entries.append(Entry("No available locations", "hint"))
    return entries


def _open_add(state: SidebarState, view: Literal["choice", "existing"] = "choice") -> None:
    state.add_view = view
    state.filtering = view == "existing"
    state.filter_text = ""
    state.creation_host = None
    state.creation_text = ""
    state.rename_target = None
    state.selected_index = 0
    state.selected_target = None
    state.add_button_selected = False


def _start_new(state: SidebarState, snapshot: SessionSnapshot) -> None:
    locations = _available_locations(snapshot)
    if len(locations) == 1:
        _select_location(state, locations[0][1])
    else:
        state.add_view = "location"
        state.filtering = False
        state.selected_index = 0


def _select_location(state: SidebarState, host: str) -> None:
    state.creation_host = host
    state.creation_text = ""
    state.rename_target = None
    state.add_view = "name"
    state.filtering = False


def _start_rename(state: SidebarState, target: Target) -> None:
    state.add_view = "name"
    state.filtering = False
    state.status = ""
    state.status_deadline = None
    state.creation_host = "" if target.kind == "local" else target.host
    state.creation_text = target.session
    state.rename_target = target


def _add_back(state: SidebarState, snapshot: SessionSnapshot) -> None:
    state.add_button_selected = False
    if state.add_view == "name":
        if state.rename_target is not None:
            _reset_add(state)
            return
        state.add_view = "location" if len(_available_locations(snapshot)) > 1 else "choice"
        state.creation_host = None
        state.creation_text = ""
    elif state.add_view in ("location", "existing"):
        state.add_view = "choice"
        state.filtering = False
        state.filter_text = ""
    elif state.add_view == "choice":
        state.add_view = None


def _reset_add(state: SidebarState) -> None:
    state.add_view = None
    state.filtering = False
    state.add_button_selected = False
    state.filter_text = ""
    state.creation_host = None
    state.creation_text = ""
    state.rename_target = None


def _numeric_tmux_id(value: str) -> tuple[int, int | str]:
    try:
        return (0, int(value[1:]))
    except (AttributeError, TypeError, ValueError):
        return (1, value or "")


def _agent_sort_key(entry: Entry, favorites: list[Target]) -> tuple:
    target = entry.target
    session_index = favorites.index(target) if target in favorites else len(favorites)
    pane = entry.pane_target
    return (
        session_index,
        _numeric_tmux_id(pane.window_id if pane else ""),
        _numeric_tmux_id(pane.pane_id if pane else ""),
        entry.agent_id or "",
    )


def _agent_entries(snapshot: SessionSnapshot, favorites: list[Target]) -> list[Entry]:
    tracked = set(favorites)
    entries = [
        Entry(
            agent.agent_name,
            "agent",
            agent.pane_target.target,
            agent.pane_target.target.host or "localhost",
            pane_target=agent.pane_target,
            agent_id=agent.agent_id,
            status=agent.task_state or "idle",
            runtime_updated_at=agent.runtime_updated_at,
            task_status_timestamp=agent.task_status_timestamp,
        )
        for agent in snapshot.agents
        if agent.pane_target.target in tracked
    ]
    entries.sort(key=lambda entry: _agent_sort_key(entry, favorites))
    return entries


def _focused_agent_id(snapshot: SessionSnapshot, current_target: Target | None, fallback: str | None) -> str | None:
    focused = {pane for pane in snapshot.focused_panes if pane.target == current_target}
    if not focused:
        return fallback
    return next((agent.agent_id for agent in snapshot.agents if agent.pane_target in focused), None)


def _update_agent_alerts(
    state: SidebarState,
    snapshot: SessionSnapshot,
    current_target: Target | None,
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
    return [i for i, entry in enumerate(entries) if entry.kind in ("session", "host", "location", "choice_new", "choice_existing")]


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
    for kind in ("session", "host"):
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
    if state.add_view is None and not _selectable(entries):
        state.add_button_selected = True
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


def _reset_selection(
    state: SidebarState,
    entries: list[Entry],
    region: Literal["sessions", "agents"] | None = None,
) -> None:
    if region in (None, "sessions"):
        choices = _selectable(entries)
        state.selected_index = choices[0] if choices else 0
        state.selected_target = entries[state.selected_index].target if choices else None
        state.selected_tracked = entries[state.selected_index].tracked if choices else False
        state.add_button_selected = state.add_view is None and not choices
    if region in (None, "agents"):
        state.agent_selected_index = 0
        state.selected_agent_key = None


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


def _tracked_session_index(entries: list[Entry], target: Target | None) -> int | None:
    if target is None:
        return None
    return next(
        (
            index
            for index, entry in enumerate(entries)
            if entry.kind == "session" and entry.tracked and entry.target == target
        ),
        None,
    )


def _alerted_agent_index(
    entries: list[Entry], alerts: set[tuple[PaneTarget, str]]
) -> int | None:
    return next(
        (
            index
            for index, entry in enumerate(entries)
            if entry.kind == "agent"
            and (entry.pane_target, entry.agent_id) in alerts
        ),
        None,
    )


def _select_session_entry(state: SidebarState, entries: list[Entry], target: Target) -> int | None:
    index = _tracked_session_index(entries, target)
    if index is None:
        return None
    state.focused_region = "sessions"
    state.add_button_selected = False
    state.selected_index = index
    state.selected_target = target
    state.selected_tracked = True
    return index


def _select_alerted_agent(
    state: SidebarState, entries: list[Entry]
) -> Effect | None:
    index = _alerted_agent_index(entries, state.agent_alerts)
    if index is None:
        return None
    entry = entries[index]
    state.focused_region = "agents"
    state.agent_selected_index = index
    state.selected_agent_key = (entry.pane_target, entry.agent_id)
    return Effect("switch_pane", entry.pane_target, message=entry.agent_id or "")


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
    if action == "remove_session" and target in state.favorites:
        state.favorites.remove(target)
        state.selected_target = None if unavailable else target
        return Effect(
            "save_favorites",
            favorites=tuple(state.favorites),
            message=f"removed {target.format()}",
        )
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
    return None


def _set_status(
    state: SidebarState,
    message: str,
    timeout: float,
    region: Literal["sessions", "agents"] = "sessions",
) -> None:
    state.status = message
    state.status_region = region
    state.status_deadline = time.monotonic() + timeout


def _renamed_target(effect: Effect) -> Target | None:
    if effect.kind != "rename" or not isinstance(effect.target, Target):
        return None
    return Target(effect.target.kind, effect.message, effect.target.host)


def _planned_favorites(effect: Effect, favorites: tuple[Target, ...]) -> tuple[Target, ...]:
    if effect.kind in ("add_switch", "create") and isinstance(effect.target, Target):
        return favorites if effect.target in favorites else (*favorites, effect.target)
    if effect.kind == "kill" and isinstance(effect.target, Target):
        return tuple(target for target in favorites if target != effect.target)
    if effect.kind == "rename" and isinstance(effect.target, Target):
        renamed = _renamed_target(effect)
        if renamed is not None:
            planned: list[Target] = []
            seen: set[Target] = set()
            for target in favorites:
                candidate = renamed if target == effect.target else target
                if candidate not in seen:
                    planned.append(candidate)
                    seen.add(candidate)
            return tuple(planned)
    if effect.kind == "save_favorites":
        return effect.favorites or ()
    return favorites


def _effect_error(effect: Effect, error: BaseException) -> str:
    if isinstance(error, subprocess.TimeoutExpired):
        target = effect.target.target if isinstance(effect.target, PaneTarget) else effect.target
        return f"{effect.kind}{f' {target.format()}' if isinstance(target, Target) else ''} timed out"
    if isinstance(error, subprocess.CalledProcessError):
        return (error.stderr or error.stdout or "").strip() or f"exit status {error.returncode}"
    if isinstance(error, OSError):
        return error.strerror or str(error)
    return str(error)


def _perform_effect(effect: Effect, favorites: tuple[Target, ...]) -> EffectResult:
    planned = _planned_favorites(effect, favorites)
    try:
        if (
            effect.automatic
            and effect.kind in ("switch", "show_reconnecting", "show_missing", "show_unavailable")
            and isinstance(effect.target, Target)
            and _current_target() != effect.target
        ):
            return EffectResult(effect, planned, stale_navigation=True)
        if effect.kind in ("switch", "add_switch") and isinstance(effect.target, Target):
            if effect.kind == "add_switch" and planned != favorites:
                save_sessions(list(planned))
            cockpit.switch(effect.target, sessions.attach_command(effect.target))
        elif effect.kind == "switch_pane" and isinstance(effect.target, PaneTarget):
            cockpit.switch(effect.target.target, sessions.pane_attach_command(effect.target), effect.message)
        elif effect.kind == "create" and isinstance(effect.target, Target):
            sessions.create(effect.target)
            if planned != favorites:
                save_sessions(list(planned))
            cockpit.switch(effect.target, sessions.attach_command(effect.target))
        elif effect.kind == "rename" and isinstance(effect.target, Target):
            renamed = _renamed_target(effect)
            if renamed is None:
                raise SystemExit("invalid rename effect")
            sessions.rename(effect.target, renamed.session)
            cockpit.rename_target(effect.target, renamed)
            if planned != favorites:
                save_sessions(list(planned))
        elif effect.kind == "kill" and isinstance(effect.target, Target):
            sessions.kill(effect.target)
            if planned != favorites:
                save_sessions(list(planned))
        elif effect.kind == "show_reconnecting" and isinstance(effect.target, Target):
            cockpit.show_reconnecting(effect.target)
        elif effect.kind == "show_missing" and isinstance(effect.target, Target):
            cockpit.show_missing(effect.target)
        elif effect.kind == "show_unavailable" and isinstance(effect.target, Target):
            cockpit.show_unavailable(effect.target)
        elif effect.kind == "save_favorites":
            save_sessions(planned)
    except (SystemExit, OSError, subprocess.SubprocessError) as error:
        return EffectResult(effect, planned, _effect_error(effect, error))
    return EffectResult(effect, planned)


def _apply_effect(
    result: EffectResult,
    state: SidebarState,
    poller: DiscoveryPoller,
    status_timeout: float,
) -> bool:
    effect = result.effect
    if result.error:
        if effect.kind == "create" and isinstance(effect.target, Target):
            state.creation_host = "" if effect.target.kind == "local" else effect.target.host
            state.creation_text = effect.target.session
        elif effect.kind == "rename" and isinstance(effect.target, Target):
            state.rename_target = effect.target
            state.creation_host = "" if effect.target.kind == "local" else effect.target.host
            state.creation_text = effect.message
        _set_status(state, result.error, status_timeout)
        return False
    if effect.kind in ("switch", "add_switch") and isinstance(effect.target, Target):
        if not result.stale_navigation:
            state.filter_text = ""
            state.filtering = False
            state.selected_target = effect.target
        if effect.kind == "add_switch":
            if effect.target not in state.favorites:
                state.favorites.append(effect.target)
            _reset_add(state)
    elif effect.kind == "switch_pane" and isinstance(effect.target, PaneTarget):
        completed_key = (effect.target, effect.message)
        if not result.stale_navigation:
            state.selected_agent_key = completed_key
        state.agent_alerts.discard(completed_key)
    elif effect.kind == "create" and isinstance(effect.target, Target):
        if effect.target not in state.favorites:
            state.favorites.append(effect.target)
        _reset_add(state)
        state.pending_selection = effect.target
        poller.refresh()
        _set_status(state, f"added {effect.target.session}", status_timeout)
    elif effect.kind == "rename" and isinstance(effect.target, Target):
        renamed = _renamed_target(effect)
        if renamed is None:
            return False
        state.favorites[:] = list(result.favorites)
        if state.selected_target == effect.target:
            state.selected_target = renamed
        poller.discard(effect.target)
        poller.refresh()
        _reset_add(state)
        _set_status(state, f"renamed {effect.target.session} to {renamed.session}", status_timeout)
    elif effect.kind == "kill" and isinstance(effect.target, Target):
        if effect.target in state.favorites:
            state.favorites.remove(effect.target)
        poller.discard(effect.target)
        poller.refresh()
        state.selected_target = effect.target
        _set_status(state, f"killed {effect.target.format()}", status_timeout)
    elif effect.kind == "save_favorites":
        _set_status(state, effect.message, status_timeout)
    elif effect.kind == "status":
        _set_status(state, effect.message, status_timeout)
    return False


def _execute(effect: Effect, state: SidebarState, poller: DiscoveryPoller, status_timeout: float) -> bool:
    return _apply_effect(_perform_effect(effect, tuple(state.favorites)), state, poller, status_timeout)


class EffectRunner:
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="letee-action")
        self._future: Future[EffectResult] | None = None
        self._effect: Effect | None = None
        self._pending_navigation: tuple[Effect, tuple[Target, ...]] | None = None

    @property
    def busy(self) -> bool:
        return self._future is not None

    @property
    def blocks_favorite_changes(self) -> bool:
        return self._effect is not None and self._effect.kind in (
            "add_switch", "create", "kill", "rename"
        )

    def submit(self, effect: Effect, favorites: tuple[Target, ...]) -> bool:
        if self._future is not None:
            if (
                self._effect is not None
                and self._effect.kind in ("switch", "switch_pane")
                and effect.kind in ("switch", "switch_pane")
            ):
                self._pending_navigation = (effect, favorites)
                return True
            return False
        self._effect = effect
        self._future = self._executor.submit(_perform_effect, effect, favorites)
        return True

    def poll(self) -> EffectResult | None:
        if self._future is None or not self._future.done():
            return None
        result = self._future.result()
        if self._pending_navigation is None:
            self._future = None
            self._effect = None
            return result
        effect, favorites = self._pending_navigation
        self._pending_navigation = None
        self._effect = effect
        self._future = self._executor.submit(_perform_effect, effect, favorites)
        return EffectResult(result.effect, result.favorites, result.error, stale_navigation=True)

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)


def _current_target() -> Target | None:
    return cockpit.current_target()


class AsyncStatusPoller:
    def __init__(self, poller: DiscoveryPoller, current_target: Target | None) -> None:
        self._poller = poller
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="letee-status")
        self._future: Future[StatusResult] | None = None
        self._commands: list[tuple[str, Target | None]] = []
        self._next_poll = 0.0
        self.snapshot = poller.snapshot
        self.current_target = current_target
        self.bell_target: Target | None = None
        self.current_agent: str | None = None
        self.pane_active = True
        self._generation = 0
        self._refresh_pending = False
        self._refresh_target: Target | None = None
        self._pending_agent: tuple[PaneTarget, str] | None = None

    def _sample(
        self,
        commands: tuple[tuple[str, Target | None], ...],
        generation: int,
    ) -> StatusResult:
        try:
            for command, target in commands:
                if command == "discard" and target is not None:
                    self._poller.discard(target)
                elif command == "refresh":
                    self._poller.refresh()
            status = cockpit.status_snapshot()
            if status is None:
                raise SystemExit("invalid cockpit status snapshot")
            current_target = status.current_target if status.current_target is not None else self.current_target
            active_host = current_target.host if current_target and current_target.kind == "ssh" else None
            self._poller.tick(active_host)
            bell_target = status.bell_target
            stored_agent = status.current_agent
            pane_active = status.pane_active
        except (OSError, SystemExit, subprocess.SubprocessError):
            current_target = self.current_target
            bell_target = self.bell_target
            stored_agent = self.current_agent
            pane_active = self.pane_active
        current_agent = _focused_agent_id(
            self._poller.snapshot, current_target, stored_agent
        )
        return StatusResult(
            self._poller.snapshot,
            current_target,
            bell_target,
            current_agent,
            pane_active,
            generation,
            any(command == "refresh" for command, _ in commands),
        )

    def tick(self, now: float) -> bool:
        changed = False
        if self._future is not None and self._future.done():
            result = self._future.result()
            self._future = None
            changed = result.snapshot != self.snapshot
            self.snapshot = result.snapshot
            if result.refreshed is True:
                self._refresh_pending = any(
                    command == "refresh" for command, _ in self._commands
                )
            if self._refresh_target is not None:
                target = self._refresh_target
                source = self.snapshot.remotes.get(target.host) if target.kind == "ssh" else None
                if (
                    target in self.snapshot.sessions
                    or (result.refreshed is True and target.kind == "local")
                    or (source is not None and not source.available)
                ):
                    self._refresh_target = None
            if result.generation == self._generation:
                self.current_target = result.current_target
                self.bell_target = result.bell_target
                if self._pending_agent is None:
                    self.current_agent = result.current_agent
                elif self._pending_agent[0] in result.snapshot.focused_panes:
                    self._pending_agent = None
                    self.current_agent = result.current_agent
                else:
                    self.current_agent = self._pending_agent[1]
                self.pane_active = result.pane_active
        if self._future is None and now >= self._next_poll:
            commands, self._commands = tuple(self._commands), []
            self._future = self._executor.submit(
                self._sample, commands, self._generation
            )
            self._next_poll = now + STATUS_POLL_INTERVAL
        return changed

    def observe_effect(self, result: EffectResult) -> None:
        if result.error or result.stale_navigation:
            return
        target = result.effect.target
        if result.effect.kind in ("switch", "add_switch", "create") and isinstance(target, Target):
            self.current_target = target
            self.current_agent = None
            self._pending_agent = None
            self._generation += 1
        elif result.effect.kind == "switch_pane" and isinstance(target, PaneTarget):
            self.current_target = target.target
            self.current_agent = result.effect.message or None
            self._pending_agent = (
                (target, result.effect.message) if result.effect.message else None
            )
            self._generation += 1
        elif result.effect.kind == "rename" and isinstance(target, Target):
            renamed = _renamed_target(result.effect)
            if renamed is None:
                return
            if self.current_target == target:
                self.current_target = renamed
                self._refresh_target = renamed
            if self.bell_target == target:
                self.bell_target = renamed
            self._generation += 1

    @property
    def refresh_pending(self) -> bool:
        return self._refresh_pending or self._refresh_target is not None

    def refresh(self) -> bool:
        self._commands.append(("refresh", None))
        self._refresh_pending = True
        self._next_poll = 0.0
        return False

    def discard(self, target: Target) -> None:
        self._commands.append(("discard", target))
        self._next_poll = 0.0

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._poller.close()


def _creation_conflicts(state: SidebarState, existing_sessions: tuple[Target, ...]) -> bool:
    kind = "local" if state.creation_host == "" else "ssh"
    return any(
        target != state.rename_target
        and target.kind == kind
        and target.host == (None if kind == "local" else state.creation_host)
        and target.session == state.creation_text
        for target in existing_sessions
    )


def _creation_key(
    state: SidebarState,
    key: int,
    existing_sessions: tuple[Target, ...] = (),
) -> Effect | None:
    if key in (27, 3):
        state.creation_host = None
        state.creation_text = ""
    elif key in (curses.KEY_BACKSPACE, 8, 127):
        state.creation_text = state.creation_text[:-1]
    elif key in (10, 13, curses.KEY_ENTER):
        name = validate_name(state.creation_text, "session")
        if _creation_conflicts(state, existing_sessions):
            raise SystemExit("Session already exists on this host")
        if state.rename_target is not None:
            if name == state.rename_target.session:
                _reset_add(state)
                return None
            return Effect("rename", target=state.rename_target, message=name)
        host = state.creation_host
        target = Target("local", name) if host == "" else Target("ssh", name, host)
        state.creation_host = None
        state.creation_text = ""
        return Effect("create", target)
    elif 32 <= key <= 126 and len(state.creation_text) < 64:
        state.creation_text += chr(key)

    state.status = "Session already exists on this host" if _creation_conflicts(state, existing_sessions) else ""
    return None


def _rename_key(
    state: SidebarState,
    key: int,
    existing_sessions: tuple[Target, ...] = (),
) -> Effect | None:
    if state.rename_target is None:
        return None
    return _creation_key(state, key, existing_sessions)


def _read_key(stdscr: curses.window, prompt: str, filtering: bool = False) -> int:
    _, w = stdscr.getmaxyx()
    row = 2 if filtering else 1
    width = max(1, w)
    attr = (_color("danger") or 0) | curses.A_BOLD
    stdscr.timeout(-1)
    try:
        stdscr.addnstr(row, 0, " " * width, width)
        stdscr.addnstr(row, 0, _truncate_cells(prompt, width), width, attr)
        stdscr.refresh()
        return stdscr.getch()
    finally:
        stdscr.addnstr(row, 0, " " * width, width)
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
    if entry.kind in ("choice_new", "choice_existing"):
        return 2
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
            if used > body:
                break
            score = (rows, end - start, -abs((start + end - 1) - 2 * selected))
            if score > best_score:
                best, best_score = (start, end), score
    return best


def _entry_at_row(
    entries: list[Entry], selected: int, row: int, height: int, footer_height: int, top: int = 1,
    scroll_offset: int | None = None,
) -> int | None:
    content_height = height - footer_height - top + 1
    start, end = _viewport(entries, selected, content_height, scroll_offset)
    entry_row = row - top - int(start > 0)
    if entry_row < 0 or row >= height - footer_height:
        return None
    for index in range(start, end):
        if entry_row < _entry_height(entries[index]):
            return index if entries[index].kind in (
                "session", "host", "agent", "choice_new", "choice_existing", "location"
            ) else None
        entry_row -= _entry_height(entries[index])
    return None


def _mouse_activates(mouse_state: int) -> bool:
    return bool(mouse_state & (
        (getattr(curses, "BUTTON1_PRESSED", 0) or 0)
        | (getattr(curses, "BUTTON1_CLICKED", 0) or 0)
    ))


def _mouse_mask(motion: bool = False) -> None:
    events = [
        getattr(curses, "BUTTON1_CLICKED", 0),
        getattr(curses, "BUTTON1_PRESSED", 0),
        getattr(curses, "BUTTON1_RELEASED", 0),
        getattr(curses, "BUTTON3_PRESSED", 0),
        getattr(curses, "BUTTON4_PRESSED", 0),
        getattr(curses, "BUTTON5_PRESSED", 0),
    ]
    if motion:
        events.append(getattr(curses, "REPORT_MOUSE_POSITION", 0))

    def set_motion_mode() -> None:
        try:
            import sys

            os.write(sys.stdout.fileno(), b"\033[?1003h" if motion else b"\033[?1003l")
        except Exception:
            pass

    if not motion:
        set_motion_mode()
    try:
        curses.mousemask(sum(event for event in events if isinstance(event, int)))
    except curses.error:
        pass
    if motion:
        set_motion_mode()


def _mouse_cleanup() -> None:
    try:
        import sys

        os.write(sys.stdout.fileno(), b"\033[?1003l")
    except Exception:
        pass


def _back_label(selected: bool = False) -> str:
    if selected:
        return f"{_icons()['selected']} back"
    return "< back" if _ascii() else "‹ back"


def _draw_back_title(
    stdscr: curses.window,
    width: int,
    left: str,
    attr: int,
    dimmed: bool,
    selected: bool = False,
) -> int | None:
    label = _back_label(selected)
    label_width = _cell_width(label)
    action_col = width - label_width
    title_attr = _fade(attr) if dimmed else attr
    button_attr = _color("active") if selected and not dimmed else attr
    button_attr = _fade(button_attr) if dimmed else button_attr
    if action_col < 0:
        title = _truncate_cells(left, width)
        stdscr.addnstr(0, 0, title + " " * max(0, width - _cell_width(title)), width, title_attr)
        return None
    title = _truncate_cells(left, action_col)
    stdscr.addnstr(0, 0, title + " " * max(0, action_col - _cell_width(title)), action_col, title_attr)
    stdscr.addnstr(0, action_col, label, label_width, button_attr)
    return action_col


def _draw_title(
    stdscr: curses.window,
    w: int,
    entries: list[Entry],
    filter_text: str,
    filtering: bool = False,
    dimmed: bool = False,
    adding: bool = False,
    add_button_selected: bool = False,
) -> tuple[int, int | None]:
    width = max(1, w)
    brand = " letee" if _ascii() else "  letee"
    if adding:
        section = "Add existing" if filtering else "New session" if any(entry.kind == "location" for entry in entries) else "Add session"
        left = f"{brand} / {section}"
    else:
        left = brand
    attr = _color("title") or (curses.A_BOLD | curses.A_REVERSE)
    if not adding:
        label = f"{_icons()['selected']} add" if add_button_selected else "＋ add"
        add_col = width - _cell_width(label)
        main_width = max(0, add_col)
        stdscr.addnstr(0, 0, left[:main_width].ljust(main_width), main_width, _fade(attr) if dimmed else attr)
        if add_col > 0:
            button_attr = _color("active") if add_button_selected and not dimmed else attr
            stdscr.addnstr(0, add_col, label, _cell_width(label), _fade(button_attr) if dimmed else button_attr)
    else:
        add_col = _draw_back_title(stdscr, width, left, attr, dimmed, add_button_selected)
    stdscr.redrawln(0, 1)
    return (min(width - 1, len(left)), add_col)


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
    spinner_frame: str | None = None,
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
    if entry.kind in ("choice_new", "choice_existing"):
        symbol = "+" if entry.kind == "choice_new" else "=" if _ascii() else "≡"
        detail = "Create a fresh tmux session" if entry.kind == "choice_new" else "Add a running tmux session"
        return [
            _truncate_cells(f"{pointer} {symbol} {entry.label}", width),
            _truncate_cells(f"    {detail}", width),
        ]
    if entry.kind == "location":
        location_icon = icon["local"] if entry.host == "" else icon["remote"]
        return [_truncate_cells(f"{pointer} {location_icon} {entry.label}", width)]
    if entry.kind == "add":
        label = f"{pointer} {icon['create']} {entry.label}"
        truncated = _truncate_cells(label, width)
        return [truncated + " " * (width - _cell_width(truncated))]
    if entry.kind == "spacer":
        return [""]
    if entry.kind == "host":
        suffix = f" {icon['create']}"
        if selected:
            label = _truncate_cells(f"{pointer} {entry.label}", max(0, width - _cell_width(suffix) - 1))
        else:
            host_icon = icon["local_header"] if entry.host == "" else icon["remote_header"]
            label = _truncate_cells(f"{host_icon} {entry.label}", max(0, width - _cell_width(suffix) - 1))
        return [_truncate(label + suffix, width)]
    if entry.kind == "agent":
        separator = " · "
        alert = " BELL" if _ascii() else " 🔔"
        alert = alert if (entry.pane_target, entry.agent_id) in (agent_alerts or set()) else ""
        status = entry.status or "unknown"
        indicator = pointer if selected else (spinner_frame if status == "working" and spinner_frame else _status_icon(status, time.monotonic()))
        prefix = f"{indicator} "
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
        window_name = entry.pane_target.window_name if entry.pane_target else ""
        location = f"{entry.target.session if entry.target else ''} · {window_name}"
        return [_truncate_cells(first, width), _truncate_cells(location_prefix + location, width)]
    if entry.kind == "session":
        kind = "unavailable" if entry.unavailable_favorite else ("remote" if entry.target and entry.target.kind == "ssh" else "local")
        bell = " BELL" if _ascii() else " 🔔"
        bell = bell if entry.target in bell_targets and entry.target != current_target else ""
        if entry.tracked:
            prefix = "" if entry.shortcut_slot is not None else f"{pointer} "
            room = max(0, width - _cell_width(prefix) - _cell_width(bell))
            label = _truncate_cells(entry.label, room)
            first = prefix + label + bell
            host_prefix = "@"
            status = (entry.status or "unavailable").replace("…", "...") if _ascii() else (entry.status or "unavailable")
            if status == "missing" and not _ascii():
                status = "⚠ missing"
            separator = " | " if _ascii() else " · "
            suffix = f"{separator}{status}" if entry.unavailable_favorite else ""
            branch = "`-" if _ascii() else "└─"
            meta_prefix = f"  {branch} "
            host = _truncate_cells(entry.host or "", max(0, width - _cell_width(meta_prefix) - _cell_width(host_prefix) - _cell_width(suffix)))
            return [first, meta_prefix + host_prefix + host + suffix]
        prefix = f"{pointer} {icon[kind]} "
        first = prefix + _truncate_cells(entry.label, max(0, width - _cell_width(prefix) - _cell_width(bell))) + bell
        return [first]
    if entry.kind == "hint":
        return [_truncate_cells(f"  {icon['enter']} {entry.label}", width)]
    if entry.kind == "empty":
        return [_truncate_cells(f"    {entry.label}", width)]
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


def _entry_attr(entry: Entry, active: bool, dimmed: bool = False, *, move_source: bool = False, move_target: bool = False) -> int:
    if move_source:
        return (_color("move") or curses.A_REVERSE) | curses.A_DIM
    if move_target:
        return _color("move") or curses.A_REVERSE
    if active:
        attr = _color("active") or curses.A_REVERSE
    elif entry.kind == "section":
        attr = _color("section") or curses.A_BOLD
    elif entry.kind == "add":
        attr = _color("add_entry") or (curses.A_BOLD | curses.A_REVERSE)
    elif entry.kind == "choice_new":
        attr = (_color("create") or 0) | curses.A_BOLD
    elif entry.kind == "choice_existing":
        attr = (_color("remote") or 0) | curses.A_BOLD
    elif entry.kind in ("header", "host"):
        attr = curses.A_BOLD
    elif entry.unavailable_favorite:
        attr = _color("unavailable") or curses.A_DIM
    elif entry.kind == "session":
        attr = _color("local")
    elif entry.kind == "location":
        attr = _color("local" if entry.host == "" else "remote") or curses.A_BOLD
    elif entry.kind == "agent":
        attr = 0
    elif entry.kind in ("hint", "empty"):
        attr = _color("hints") or curses.A_DIM
    elif entry.kind == "unavailable":
        attr = _color("unavailable") or curses.A_DIM
    else:
        attr = 0
    return _fade(attr) if dimmed else attr


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
    spinner_frame: str | None = None,
    move_source_entry: int | None = None,
    move_target_entry: int | None = None,
    selection_pointer_visible: bool = True,
    pane_active: bool = True,
) -> tuple[int, int] | None:
    cursor = None
    start, end = _viewport(entries, selected, h - top + 1, scroll_offset)
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
        active_entry = entry.target is not None and entry.target == current_target and entry.kind != "agent"
        active_agent = entry.kind == "agent" and entry.agent_id == active_agent_id
        entry_width = max(0, w - 2) if entry.tracked else w
        lines = _entry_lines(
            entry, selected_entry and not dimmed and selection_pointer_visible and pane_active, bell_targets, current_target, entry_width,
            creation_host, creation_text, now, agent_alerts, spinner_frame,
        )
        # ponytail: cursor position indicated by pointer char, not color; only active pane agent gets orange
        is_move_source = move_source_entry is not None and idx == move_source_entry
        is_move_target = move_target_entry is not None and idx == move_target_entry
        if is_move_target:
            rule = "-" if _ascii() else "─"
            prompt = "Click to place "
            lines = [(prompt + rule * max(0, w - len(prompt)))[:w], lines[0]]
        base_attr = _entry_attr(entry, active_entry or active_agent, dimmed, move_source=is_move_source, move_target=is_move_target)
        slot_badge = ""
        slot_width = 0
        if entry.tracked and entry.shortcut_slot is not None and not is_move_target:
            slot_width = 4
            ico = _icons()
        for line_number, line in enumerate(lines):
            if row >= h - 1:
                break
            attr = _fade(base_attr) if line_number and not (active_entry or active_agent or is_move_target) else base_attr
            if line_number == 0 and slot_width:
                if selected_entry and not dimmed and selection_pointer_visible and pane_active:
                    slot_badge = f" {ico['selected']} "
                else:
                    slot_badge = f"[{entry.shortcut_slot}]"
                slot_attr = _color("slot_active") if active_entry else _color("slot")
                stdscr.addnstr(row, 0, slot_badge, w, slot_attr or curses.A_BOLD)
                stdscr.addnstr(row, 3, " " + line, w - 3, attr)
            else:
                stdscr.addnstr(row, 0, line, w, attr)
                if entry.kind == "agent" and line_number == 0 and entry.status:
                    status_attr = _status_attr(entry.status)
                    semantic_attr = _fade(status_attr) if dimmed else status_attr
                    icon_attr = semantic_attr
                    stdscr.addnstr(row, 0, line[0], min(1, w), icon_attr)
                    column = line.rfind(entry.status)
                    if column >= 0:
                        stdscr.addnstr(row, column, entry.status, max(0, w - column), semantic_attr)
            if entry.tracked and line_number == int(is_move_target):
                handle = ":" if _ascii() else "↕"
                handle_attr = _color("move") or curses.A_BOLD
                stdscr.addnstr(row, max(0, w - 2), handle, 1, _fade(handle_attr) if dimmed else handle_attr)
            if entry.kind == "host" and entry.host == creation_host:
                cursor = (row, min(w - 1, _cell_width(line)))
            row += 1
    if end < len(entries) and row < h - 1:
        attr = _color("hints") or curses.A_DIM
        stdscr.addnstr(h - 2, 0, "↓ more", w - 1, _fade(attr) if dimmed else attr)
    return cursor


def _draw_footer(
    stdscr: curses.window,
    h: int,
    w: int,
    filtering: bool = False,
    dimmed: bool = False,
    adding: bool = False,
) -> int:
    if filtering:
        logical_rows = ["type to filter  backspace edit", f"esc clear  {'Enter' if _ascii() else '↵'} switch"]
    elif adding:
        logical_rows = [f"{'Enter' if _ascii() else '↵'} select · Esc back" if not _ascii() else "Enter select  Esc back"]
    else:
        logical_rows = [f"{'Enter' if _ascii() else '↵'} activate"]
    width = max(1, w - 1)
    lines = [line for logical_row in logical_rows for line in (textwrap.wrap(logical_row, width=width) or [""])]
    attr = _color("title") or (curses.A_BOLD | curses.A_REVERSE)
    for row, line in enumerate(lines, h - len(lines)):
        row_attr = _fade(attr) if dimmed else attr
        stdscr.addnstr(row, 0, line.ljust(w - 1), w - 1, row_attr)
        stdscr.chgat(row, w - 1, 1, row_attr)
    return len(lines)


def _draw_name(stdscr: curses.window, state: SidebarState, dimmed: bool = False) -> int | None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    width = max(1, w)
    attr = _color("title") or (curses.A_BOLD | curses.A_REVERSE)
    ascii_mode = _ascii()
    if state.rename_target is None:
        title = " + New session" if ascii_mode else " ＋ New session"
    else:
        title = " e Rename session" if ascii_mode else " ✎ Rename session"
    back_col = _draw_back_title(stdscr, width, title, attr, dimmed)
    host = "localhost" if state.creation_host == "" else (state.creation_host or "")
    host_icon = "*" if ascii_mode else ("●" if state.creation_host == "" else "◆")
    host_attr = _color("local" if state.creation_host == "" else "remote") or curses.A_BOLD
    stdscr.addnstr(2, 0, _truncate_cells(f" {host_icon} {host}", width), width, _fade(host_attr) if dimmed else host_attr)
    prefix = " > " if ascii_mode else " ❯ "
    room = max(0, width - _cell_width(prefix) - 1)
    visible = state.creation_text[-room:] if room else ""
    field = prefix + (visible or "session-name")
    field_attr = _color("add_entry") or curses.A_REVERSE
    stdscr.addnstr(3, 0, _truncate_cells(field, width).ljust(width), width, _fade(field_attr) if dimmed else field_attr)
    if h > 5:
        message = f" ! {state.status}" if ascii_mode and state.status else f" ✕ {state.status}" if state.status else " Letters, numbers, . _ -"
        message_attr = (_color("danger") or curses.A_BOLD) if state.status else (_color("hints") or curses.A_DIM)
        stdscr.addnstr(4, 0, _truncate_cells(message, width), width, _fade(message_attr) if dimmed else message_attr)
    if state.rename_target is None:
        footer = "Esc back  Enter create" if ascii_mode else "Esc back · ↵ create"
    else:
        footer = "Esc cancel  Enter rename" if ascii_mode else "Esc cancel · ↵ rename"
    footer_width = max(0, width - 1)
    stdscr.addnstr(h - 1, 0, footer[:footer_width].ljust(footer_width), footer_width, _fade(attr) if dimmed else attr)
    if width > 1:
        stdscr.chgat(h - 1, width - 1, 1, _fade(attr) if dimmed else attr)
    stdscr.move(3, min(width - 1, _cell_width(prefix + visible)))
    stdscr.refresh()
    return back_col


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
    spinner_frame: str | None = None,
    add_button_selected: bool = False,
    move_source_entry: int | None = None,
    move_target_entry: int | None = None,
    pane_active: bool = True,
    status_region: str = "sessions",
) -> tuple[int, int | None]:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    cursor, add_col = _draw_title(
        stdscr, w, entries, filter_text, filtering, dimmed, adding,
        add_button_selected and pane_active,
    )
    if filtering:
        cursor = _draw_filter(stdscr, w, filter_text, dimmed)
    message_row = 2 if filtering else 1
    message_attr = _color("hints") or curses.A_DIM
    if status_region == "agents" and status:
        message_attr = (_color("danger") or 0) | curses.A_BOLD
        message = _truncate_cells(f"{_icons()['unavailable']} {status}", max(1, w))
    else:
        message = _truncate_cells(status, max(1, w))
    if status_region != "agents":
        stdscr.addnstr(
            message_row, 0, message, max(1, w),
            _fade(message_attr) if dimmed else message_attr,
        )
    footer_height = _draw_footer(stdscr, h, w, filtering, dimmed, adding)
    if agent_entries is None:
        creation_cursor = _draw_entries(
            stdscr, entries, selected, h - footer_height + 1, w, bell_targets or set(), current_target,
            dimmed, creation_host, creation_text, 3 if filtering else 2, scroll_offset,
            move_source_entry=move_source_entry, move_target_entry=move_target_entry,
            selection_pointer_visible=not add_button_selected,
            pane_active=pane_active,
        )
        if creation_cursor:
            stdscr.move(*creation_cursor)
        elif filtering:
            stdscr.move(*cursor)
        stdscr.refresh()
        return footer_height, add_col
    footer_top = h - footer_height
    agents = agent_entries
    has_real_agents = any(e.kind == "agent" for e in agents) if agents else False
    minimum_agent_rows = 4 if has_real_agents else 3
    session_top = 3 if filtering else 2
    if footer_top - session_top < 2 + minimum_agent_rows:
        stdscr.addnstr(session_top, 0, "Terminal too short; resize window", max(0, w - 1), curses.A_BOLD)
        stdscr.refresh()
        return footer_height, add_col
    available = footer_top - session_top - 1
    wanted = agent_rows if agent_rows is not None else max(minimum_agent_rows, round(available * 0.4))
    agent_body = min(max(minimum_agent_rows, wanted), available - 1)
    separator = footer_top - agent_body - 1
    creation_cursor = _draw_entries(
        stdscr, entries, selected, separator + 1, w, bell_targets or set(), current_target,
        dimmed or focused_region != "sessions", creation_host, creation_text, session_top, scroll_offset,
        move_source_entry=move_source_entry, move_target_entry=move_target_entry,
        selection_pointer_visible=not add_button_selected,
        pane_active=pane_active,
    )
    rule = "-" if _ascii() else "─"
    label = "AGENTS "
    stdscr.addnstr(separator, 0, label + rule * max(0, w - len(label)), w, _color("section") or curses.A_BOLD)
    if has_real_agents:
        _draw_entries(
            stdscr, agents, agent_selected, footer_top + 1, w, set(), None,
            dimmed or focused_region != "agents", top=separator + 1,
            active_agent_id=active_agent_id, now=now, agent_alerts=agent_alerts,
            spinner_frame=spinner_frame, pane_active=pane_active,
        )
    else:
        stdscr.addnstr(separator + 1, 0, "  No active agents", max(0, w - 1), curses.A_DIM)
    if status_region == "agents" and status:
        stdscr.addnstr(
            separator + 2, 0, message, max(1, w),
            _fade(message_attr) if dimmed else message_attr,
        )
    if creation_cursor:
        stdscr.move(*creation_cursor)
    elif filtering:
        stdscr.move(*cursor)
    stdscr.refresh()
    return footer_height, add_col


def run(stdscr: curses.window) -> None:
    _init_colors()
    curses.curs_set(0)
    _mouse_mask()
    status_timeout = load_status_timeout()
    initial_target = _current_target()
    state = SidebarState(favorites=load_sessions(), selected_target=initial_target)
    poller = AsyncStatusPoller(DiscoveryPoller(load_hosts()), initial_target)
    actions = EffectRunner()
    entries = _entries(state.filter_text, poller.snapshot, state.favorites)
    _update_agent_alerts(state, poller.snapshot, state.selected_target)
    agent_entries = _agent_entries(poller.snapshot, state.favorites)
    _reset_selection(state, entries)
    cockpit_bell_target: Target | None = None
    active_agent_id: str | None = None
    unavailable_target_shown: Target | None = None
    pending_navigation: tuple[Target, str | None] | None = None
    rendered: tuple[object, ...] | None = None
    footer_height = 0
    add_col: int | None = None
    move_scroll_direction = 0
    next_move_scroll: float | None = None
    pending_key: int | None = None
    pending_mouse: tuple[int, int, int, int, int] | None = None
    stdscr.timeout(UI_POLL_INTERVAL_MS)

    def show_status(
        message: str, region: Literal["sessions", "agents"] = "sessions"
    ) -> None:
        _set_status(state, message, status_timeout, region)

    def queue_effect(effect: Effect) -> bool:
        return actions.submit(effect, tuple(state.favorites))

    def dispatch(effect: Effect) -> None:
        nonlocal pending_navigation
        if effect.kind == "save_favorites":
            _execute(effect, state, poller, status_timeout)
            return
        if effect.kind == "status":
            _apply_effect(
                EffectResult(effect, tuple(state.favorites)), state, poller, status_timeout
            )
            return
        if not queue_effect(effect):
            show_status("another action is still running")
        elif effect.kind in ("switch", "add_switch") and isinstance(effect.target, Target):
            pending_navigation = (effect.target, None)
        elif effect.kind == "switch_pane" and isinstance(effect.target, PaneTarget):
            pending_navigation = (effect.target.target, effect.message or None)

    def cancel_move() -> None:
        nonlocal move_scroll_direction, next_move_scroll
        if state.move_source is not None:
            _mouse_mask(False)
        state.move_source = None
        state.move_target = None
        move_scroll_direction = 0
        next_move_scroll = None

    def rebuild() -> None:
        nonlocal entries, agent_entries
        entries = (
            _add_entries(state.add_view, state.filter_text, poller.snapshot, state.favorites)
            if state.add_view in ("choice", "existing", "location")
            else _entries(state.filter_text, poller.snapshot, state.favorites)
        )
        agent_entries = _agent_entries(poller.snapshot, state.favorites)
        if state.move_source not in state.favorites or not any(
            entry.tracked and entry.target == state.move_source for entry in entries
        ):
            cancel_move()
        _sync_selection(state, entries)
        _sync_agent_selection(state, agent_entries)
        state.scroll_offset = None

    def prefix_action(action: str, current_target: Target | None) -> Effect | None:
        if action == "add":
            _open_add(state)
            state.focused_region = "sessions"
            curses.curs_set(0)
            rebuild()
            return None
        if state.add_view is not None:
            _reset_add(state)
            curses.curs_set(0)
            rebuild()
        if action in ("remove", "kill"):
            state.focused_region = "sessions"
            state.add_button_selected = False
            if current_target is None:
                show_status("no active session")
                return None
            index = _select_session_entry(state, entries, current_target)
            if index is None:
                show_status(f"active session is not tracked: {current_target.format()}")
                return None
            entry = entries[index]
            if action == "remove":
                if actions.blocks_favorite_changes:
                    show_status("another action is still changing sessions")
                    return None
                return _transition(state, "remove_session", current_target)
            if entry.unavailable_favorite:
                show_status("Session already missing; press r to remove")
                return None
            if actions.busy:
                show_status("another action is still running")
                return None
            if _read_key(stdscr, f"kill {current_target.format()}? y/N", state.filtering) != ord("y"):
                return None
            return _transition(state, "kill", current_target)
        state.focused_region = "agents"
        effect = _select_alerted_agent(state, agent_entries)
        if effect is None:
            show_status("no agent alerts", "agents")
        return effect

    def start_move(target: Target) -> None:
        state.move_source = target
        state.move_target = None
        _mouse_mask(True)

    def commit_move(destination: Target) -> None:
        source = state.move_source
        if source is None:
            return
        if actions.blocks_favorite_changes:
            show_status("another action is still changing sessions")
        elif source not in state.favorites:
            show_status("session disappeared during move")
        elif destination in state.favorites and source != destination:
            destination_index = state.favorites.index(destination)
            state.favorites.remove(source)
            state.favorites.insert(destination_index, source)
            dispatch(Effect(
                "save_favorites",
                favorites=tuple(state.favorites),
                message=f"moved {source.format()}",
            ))
            state.selected_target = source
        cancel_move()
        rebuild()

    try:
        while True:
            now = time.monotonic()
            if pending_key is None:
                stdscr.timeout(0)
                pending_key = stdscr.getch()
                pending_key = pending_key if pending_key != -1 else None
                stdscr.timeout(UI_POLL_INTERVAL_MS)
            result = actions.poll()
            if result is not None:
                if not result.stale_navigation and result.effect.kind in (
                    "switch", "add_switch", "switch_pane"
                ):
                    pending_navigation = None
                if _apply_effect(result, state, poller, status_timeout):
                    return
                unavailable_target_shown = _reconcile_active_session_effect(
                    unavailable_target_shown, result
                )
                poller.observe_effect(result)
                rebuild()
            current_target = poller.current_target
            if (
                state.move_source is not None
                and move_scroll_direction
                and next_move_scroll is not None
                and now >= next_move_scroll
            ):
                h = stdscr.getmaxyx()[0]
                footer_top = h - footer_height
                session_top = 3 if state.filtering else 2
                has_agents = any(entry.kind == "agent" for entry in agent_entries)
                minimum_agent_rows = 4 if has_agents else 3
                available = footer_top - session_top - 1
                wanted = state.agent_rows if state.agent_rows is not None else max(minimum_agent_rows, round(available * 0.4))
                agent_body = min(max(minimum_agent_rows, wanted), max(minimum_agent_rows, available - 1))
                separator = footer_top if state.add_view is not None else footer_top - agent_body - 1
                start, end = _viewport(entries, state.selected_index, separator - session_top + 2, state.scroll_offset)
                can_scroll = start > 0 if move_scroll_direction < 0 else end < len(entries)
                if can_scroll:
                    state.scroll_offset = max(0, start + move_scroll_direction)
                    start, end = _viewport(entries, state.selected_index, separator - session_top + 2, state.scroll_offset)
                    edge = start if move_scroll_direction < 0 else end - 1
                    if entries[edge].tracked:
                        state.move_target = entries[edge].target
                    next_move_scroll += MOVE_SCROLL_INTERVAL
                else:
                    move_scroll_direction = 0
                    next_move_scroll = None
            if state.status_deadline is not None and now >= state.status_deadline:
                state.status = ""
                state.status_deadline = None
            agent_alert = False
            if poller.tick(now):
                current_target = poller.current_target
                agent_alert = _update_agent_alerts(state, poller.snapshot, current_target)
                scroll_offset = state.scroll_offset
                rebuild()
                state.scroll_offset = min(scroll_offset, max(0, len(entries) - 1)) if scroll_offset is not None else None
            if (
                pending_key is None
                and not actions.busy
                and getattr(poller, "refresh_pending", False) is not True
            ):
                try:
                    unavailable_target_shown = _sync_active_session(
                        current_target,
                        poller.snapshot,
                        unavailable_target_shown,
                        submit=queue_effect,
                    )
                except SystemExit as error:
                    show_status(str(error))
            selectable = _selectable(entries)
            if selectable and state.selected_index not in selectable:
                state.selected_index = selectable[0]
            cockpit_bell_target = poller.bell_target
            bell_targets = _bell_targets(poller.snapshot, cockpit_bell_target, state.favorites)
            if pending_navigation is None:
                display_target = current_target
                active_agent_id = poller.current_agent
            else:
                display_target, active_agent_id = pending_navigation
            visible_bells = bell_targets - ({display_target} if display_target else set())
            if visible_bells - state.rang_bells or agent_alert:
                curses.beep()
            state.rang_bells = bell_targets
            dimmed = False
            working_agents = any(entry.status == "working" for entry in agent_entries)
            spinner_frame = _spinner_frame(now) if working_agents else None
            render_state = (
                tuple(entries), state.selected_index, state.status, state.status_region, state.filter_text,
                state.filtering, state.add_view, state.creation_host, state.creation_text,
                frozenset(bell_targets), display_target, poller.pane_active, stdscr.getmaxyx(),
                state.scroll_offset, tuple(agent_entries), state.agent_selected_index,
                state.focused_region, state.agent_rows, active_agent_id,
                frozenset(state.agent_alerts), spinner_frame,
                int(time.time()) if agent_entries else None,
                state.add_button_selected, state.move_source, state.move_target,
            )
            if render_state != rendered:
                if state.add_view == "name":
                    add_col = _draw_name(stdscr, state, dimmed)
                    footer_height = 1
                else:
                    move_src_entry = _tracked_session_index(entries, state.move_source)
                    move_tgt_entry = _tracked_session_index(entries, state.move_target)
                    footer_height, add_col = _draw(
                        stdscr, entries, state.selected_index, state.status, state.filter_text,
                        state.filtering, bell_targets, display_target, dimmed,
                        state.creation_host, state.creation_text, state.add_view is not None, state.scroll_offset,
                        agent_entries if state.add_view is None else None, state.agent_selected_index, state.focused_region, state.agent_rows,
                        active_agent_id, agent_alerts=state.agent_alerts,
                        spinner_frame=spinner_frame, add_button_selected=state.add_button_selected,
                        move_source_entry=move_src_entry, move_target_entry=move_tgt_entry,
                        pane_active=poller.pane_active, status_region=state.status_region,
                    )
                rendered = render_state
            try:
                if pending_key is None:
                    key = stdscr.getch()
                else:
                    key, pending_key = pending_key, None
            except KeyboardInterrupt:
                if state.add_view is None:
                    raise
                key = 3
            if key == -1:
                continue
            # Tests use private sentinel; removed q cannot terminate loop.
            if key is getattr(stdscr, "_letee_test_stop", None):
                return
            if key in (curses.KEY_F6, curses.KEY_F7):
                state.focused_region = "sessions" if key == curses.KEY_F6 else "agents"
                _reset_selection(state, entries, state.focused_region)
                continue
            if key in _PREFIX_ACTIONS:
                effect = prefix_action(_PREFIX_ACTIONS[key], current_target)
                if effect:
                    dispatch(effect)
                    rebuild()
                continue
            if state.add_view == "name":
                while state.add_view == "name":
                    if key in (curses.KEY_F6, curses.KEY_F7):
                        state.focused_region = "sessions" if key == curses.KEY_F6 else "agents"
                        _reset_selection(state, entries, state.focused_region)
                    elif key in (27, 3):
                        _add_back(state, poller.snapshot)
                        curses.curs_set(0)
                        rebuild()
                        break
                    elif key == curses.KEY_MOUSE:
                        try:
                            _, mouse_col, row, _, mouse_state = curses.getmouse()
                        except (curses.error, TypeError, ValueError):
                            pass
                        else:
                            if (
                                isinstance(mouse_col, int)
                                and isinstance(row, int)
                                and isinstance(mouse_state, int)
                                and row == 0
                                and add_col is not None
                                and add_col <= mouse_col < stdscr.getmaxyx()[1]
                                and _mouse_activates(mouse_state)
                            ):
                                _add_back(state, poller.snapshot)
                                curses.curs_set(0)
                                rebuild()
                                break
                    else:
                        try:
                            effect = (
                                _rename_key(state, key, poller.snapshot.sessions)
                                if state.rename_target is not None
                                else _creation_key(state, key, poller.snapshot.sessions)
                            )
                        except SystemExit as error:
                            show_status(str(error))
                        else:
                            if effect:
                                if actions.busy:
                                    show_status("another action is still running")
                                else:
                                    curses.curs_set(0)
                                    dispatch(effect)
                                    rebuild()
                                    break
                            elif state.add_view != "name":
                                curses.curs_set(0)
                                rebuild()
                                break
                    add_col = _draw_name(stdscr, state, dimmed)
                    try:
                        key = stdscr.getch()
                    except KeyboardInterrupt:
                        key = 3
                    if key is getattr(stdscr, "_letee_test_stop", None):
                        return
                    if key == -1:
                        break
                continue
            if key == curses.KEY_MOUSE:
                try:
                    mouse_event = pending_mouse or curses.getmouse()
                    pending_mouse = None
                    _, mouse_col, row, _, mouse_state = mouse_event
                except (curses.error, TypeError, ValueError):
                    continue
                if not isinstance(row, int) or not isinstance(mouse_state, int):
                    continue
                motion = getattr(curses, "REPORT_MOUSE_POSITION", 0) or 0
                button_bits = (
                    (getattr(curses, "BUTTON1_PRESSED", 0) or 0)
                    | (getattr(curses, "BUTTON1_RELEASED", 0) or 0)
                    | (getattr(curses, "BUTTON1_CLICKED", 0) or 0)
                    | (getattr(curses, "BUTTON3_PRESSED", 0) or 0)
                    | (getattr(curses, "BUTTON4_PRESSED", 0) or 0)
                    | (getattr(curses, "BUTTON5_PRESSED", 0) or 0)
                )
                if state.move_source is None and mouse_state & motion and not mouse_state & button_bits:
                    continue
                # Compute layout once for this mouse event
                h = stdscr.getmaxyx()[0]
                footer_top = h - footer_height
                session_top = 3 if state.filtering else 2
                has_agents = any(e.kind == "agent" for e in agent_entries)
                minimum_agent_rows = 4 if has_agents else 3
                available = footer_top - session_top - 1
                wanted = state.agent_rows if state.agent_rows is not None else max(minimum_agent_rows, round(available * 0.4))
                agent_body = min(max(minimum_agent_rows, wanted), max(minimum_agent_rows, available - 1))
                separator = footer_top if state.add_view is not None else footer_top - agent_body - 1
                if state.move_source is not None:
                    start, end = _viewport(
                        entries, state.selected_index, separator - session_top + 2,
                        state.scroll_offset,
                    )
                    top = session_top
                    bottom_marker_row = separator - 1
                    direction = -1 if start > 0 and row == top else 1 if end < len(entries) and row == bottom_marker_row else 0
                    if direction != move_scroll_direction:
                        move_scroll_direction = direction
                        next_move_scroll = now + MOVE_SCROLL_INTERVAL if direction else None
                    index = _entry_at_row(
                        entries, state.selected_index, row, separator + 1, 0,
                        session_top, state.scroll_offset,
                    )
                    state.move_target = (
                        entries[index].target
                        if index is not None and entries[index].tracked
                        else None
                    )
                    if _mouse_activates(mouse_state):
                        if index is None:
                            cancel_move()
                        elif entries[index].target == state.move_source and mouse_col == stdscr.getmaxyx()[1] - 2:
                            cancel_move()
                        elif entries[index].tracked and entries[index].target:
                            commit_move(entries[index].target)
                        continue
                    if mouse_state & motion:
                        continue
                release_or_click = (
                    (getattr(curses, "BUTTON1_RELEASED", 0) or 0)
                    | (getattr(curses, "BUTTON1_CLICKED", 0) or 0)
                )
                if mouse_state & release_or_click and not _mouse_activates(mouse_state):
                    continue
                wheel_up = getattr(curses, "BUTTON4_PRESSED", 0) or 0
                wheel_down = getattr(curses, "BUTTON5_PRESSED", 0) or 0
                if mouse_state & (wheel_up | wheel_down):
                    # ponytail: drain queued wheel events before slow tmux polling so direction changes stay responsive.
                    stdscr.timeout(0)
                    try:
                        while True:
                            viewport_height = separator - session_top + 2
                            if state.scroll_offset is None:
                                start, _ = _viewport(entries, state.selected_index, viewport_height)
                                state.scroll_offset = start
                            if mouse_state & wheel_up:
                                state.scroll_offset = max(0, state.scroll_offset - 1)
                            else:
                                body = max(1, viewport_height - 2)
                                row_offsets = [0]
                                for entry in entries:
                                    row_offsets.append(row_offsets[-1] + _entry_height(entry))
                                total = row_offsets[-1]
                                max_offset = max(0, len(entries) - 1)
                                for i in range(len(entries)):
                                    if total - row_offsets[i] + int(i > 0) <= body:
                                        max_offset = i
                                        break
                                state.scroll_offset = min(max_offset, state.scroll_offset + 1)

                            next_key = stdscr.getch()
                            if next_key != curses.KEY_MOUSE:
                                pending_key = next_key if next_key != -1 else None
                                break
                            try:
                                next_mouse = curses.getmouse()
                                _, _, next_row, _, next_state = next_mouse
                            except (curses.error, TypeError, ValueError):
                                break
                            if not isinstance(next_row, int) or not isinstance(next_state, int):
                                break
                            if not next_state & (wheel_up | wheel_down):
                                pending_key, pending_mouse = curses.KEY_MOUSE, next_mouse
                                break
                            mouse_state = next_state
                    finally:
                        stdscr.timeout(UI_POLL_INTERVAL_MS)
                    continue
                right_click = mouse_state & (getattr(curses, "BUTTON3_PRESSED", 0) or 0)
                if right_click:
                    cancel_move()
                    index = _entry_at_row(
                        entries, state.selected_index, row, separator + 1, 0,
                        3 if state.filtering else 2,
                        state.scroll_offset,
                    )
                    if isinstance(mouse_col, int) and index is not None and entries[index].kind == "session" and entries[index].tracked:
                        state.focused_region = "sessions"
                        state.add_button_selected = False
                        state.selected_index = index
                        state.selected_target = entries[index].target
                        state.selected_tracked = True
                        _mouse_cleanup()
                        try:
                            cockpit.show_session_menu(entries[index].target, mouse_col, row)
                        finally:
                            _mouse_mask(False)
                    continue
                if (
                    row == 0
                    and add_col is not None
                    and isinstance(mouse_col, int)
                    and 0 <= mouse_col < stdscr.getmaxyx()[1]
                    and mouse_col >= add_col
                ):
                    if _mouse_activates(mouse_state):
                        if state.add_view is None:
                            _open_add(state)
                        else:
                            _add_back(state, poller.snapshot)
                            curses.curs_set(0)
                        rebuild()
                    continue
                else:
                    if separator < row < footer_top and state.add_view is None and agent_entries:
                        index = _entry_at_row(agent_entries, state.agent_selected_index, row, footer_top + 1, 0, separator + 1)
                        if index is None:
                            continue
                        state.focused_region = "agents"
                        state.agent_selected_index = index
                        entry = agent_entries[index]
                        state.selected_agent_key = (entry.pane_target, entry.agent_id) if entry.pane_target and entry.agent_id else None
                        if _mouse_activates(mouse_state) and entry.pane_target:
                            dispatch(Effect("switch_pane", entry.pane_target, message=entry.agent_id or ""))
                        continue
                    index = _entry_at_row(
                        entries, state.selected_index, row, separator + 1, 0,
                        3 if state.filtering else 2,
                        state.scroll_offset,
                    )
                    if index is None:
                        continue
                    state.focused_region = "sessions"
                    state.add_button_selected = False
                    state.selected_index = index
                    state.selected_target = entries[index].target
                    state.selected_tracked = entries[index].tracked
                    if (
                        _mouse_activates(mouse_state)
                        and entries[index].tracked
                        and mouse_col == stdscr.getmaxyx()[1] - 2
                        and entries[index].target
                    ):
                        start_move(entries[index].target)
                        continue
                    if _mouse_activates(mouse_state):
                        key = curses.KEY_ENTER
                    else:
                        continue
            if key in (27, 3) and state.move_source is not None:
                cancel_move()
                continue
            if state.filtering:
                if key in (27, 3):
                    if state.add_view == "existing":
                        _add_back(state, poller.snapshot)
                    else:
                        state.filter_text = ""
                        state.filtering = False
                    curses.curs_set(0)
                    rebuild()
                    continue
                new_filter = _filter_key(state.filter_text, key)
                if new_filter is not None:
                    state.filter_text = new_filter
                    rebuild()
                    continue
            if key in (27, 3) and state.add_view is not None:
                _add_back(state, poller.snapshot)
                rebuild()
                continue
            selectable = _selectable(entries)
            effect: Effect | None = None
            if key in (ord("["), ord("]")):
                h = stdscr.getmaxyx()[0]
                footer_top = h - footer_height
                session_top = 3 if state.filtering else 2
                has_real_agents = any(entry.kind == "agent" for entry in agent_entries)
                minimum_agent_rows = 4 if has_real_agents else 3
                available = footer_top - session_top - 1
                baseline = max(minimum_agent_rows, round(available * 0.4))
                current = state.agent_rows if state.agent_rows is not None else baseline
                state.agent_rows = (
                    current + 1
                    if key == ord("[")
                    else max(minimum_agent_rows, current - 1)
                )
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
            elif state.focused_region == "agents" and key in map(ord, "rxKJ"):
                effect = Effect("status", message="agent panes are automatic")
            elif key in (curses.KEY_DOWN, ord("j")) and (selectable or state.add_view is not None):
                state.scroll_offset = None
                if state.add_button_selected:
                    if selectable:
                        state.add_button_selected = False
                        state.selected_index = selectable[0]
                        state.selected_target = entries[state.selected_index].target
                        state.selected_tracked = entries[state.selected_index].tracked
                elif not selectable:
                    state.add_button_selected = True
                else:
                    state.selected_index = selectable[(selectable.index(state.selected_index) + 1) % len(selectable)]
                    state.selected_target = entries[state.selected_index].target
                    state.selected_tracked = entries[state.selected_index].tracked
            elif key in (curses.KEY_UP, ord("k")) and (selectable or state.add_view is not None):
                state.scroll_offset = None
                if state.add_view is not None:
                    if not state.add_button_selected and (not selectable or state.selected_index == selectable[0]):
                        state.add_button_selected = True
                    elif not state.add_button_selected:
                        state.selected_index = selectable[(selectable.index(state.selected_index) - 1) % len(selectable)]
                        state.selected_target = entries[state.selected_index].target
                        state.selected_tracked = entries[state.selected_index].tracked
                elif state.selected_index == selectable[0] and not state.add_button_selected:
                    state.add_button_selected = True
                elif state.add_button_selected:
                    state.add_button_selected = False
                    state.selected_index = selectable[-1]
                    state.selected_target = entries[state.selected_index].target
                    state.selected_tracked = entries[state.selected_index].tracked
                else:
                    state.selected_index = selectable[(selectable.index(state.selected_index) - 1) % len(selectable)]
                    state.selected_target = entries[state.selected_index].target
                    state.selected_tracked = entries[state.selected_index].tracked
            elif key == ord("K"):
                if actions.blocks_favorite_changes:
                    show_status("another action is still changing sessions")
                else:
                    effect = _transition(state, "move_session_up")
            elif key == ord("J"):
                if actions.blocks_favorite_changes:
                    show_status("another action is still changing sessions")
                else:
                    effect = _transition(state, "move_session_down")
            elif key == ord("e") and state.add_view is None and state.focused_region == "sessions" and entries:
                entry = entries[state.selected_index]
                if entry.kind == "session" and entry.target and entry.tracked:
                    if actions.blocks_favorite_changes:
                        show_status("another action is still changing sessions")
                    elif entry.unavailable_favorite:
                        show_status("Session unavailable; rename a running session")
                    else:
                        _start_rename(state, entry.target)
                        curses.curs_set(1)
            elif key == ord("r") and state.add_view is None and entries:
                entry = entries[state.selected_index]
                if entry.kind == "session" and entry.target:
                    if actions.blocks_favorite_changes:
                        show_status("another action is still changing sessions")
                    else:
                        effect = _transition(state, "remove_session", entry.target)
            elif key in (10, 13, curses.KEY_ENTER):
                state.scroll_offset = None
                if state.add_button_selected:
                    state.add_button_selected = False
                    if state.add_view is None:
                        _open_add(state)
                    else:
                        _add_back(state, poller.snapshot)
                        curses.curs_set(0)
                    rebuild()
                    continue
                if not entries:
                    continue
                entry = entries[state.selected_index]
                if entry.kind == "choice_new":
                    _start_new(state, poller.snapshot)
                    rebuild()
                    if state.add_view == "name":
                        curses.curs_set(1)
                    continue
                if entry.kind == "choice_existing":
                    _open_add(state, "existing")
                    rebuild()
                    curses.curs_set(1)
                    continue
                if entry.kind == "location":
                    _select_location(state, entry.host or "")
                    curses.curs_set(1)
                    continue
                if entry.target:
                    effect = _transition(state, "add_switch" if state.add_view == "existing" else "switch", entry.target)
            elif key == ord("x"):
                if not entries:
                    continue
                entry = entries[state.selected_index]
                if entry.kind != "session" or not entry.target:
                    continue
                if entry.unavailable_favorite:
                    show_status("Session already missing; press r to remove")
                    continue
                if _read_key(stdscr, f"kill {entry.target.format()}? y/N", state.filtering) != ord("y"):
                    continue
                effect = _transition(state, "kill", entry.target)
            if effect:
                dispatch(effect)
                if effect.kind in ("switch", "create"):
                    curses.curs_set(0)
                rebuild()
    finally:
        cancel_move()
        actions.close()
        poller.close()
        _mouse_cleanup()


def _maintain_sidebar(stop: threading.Event, pane: str) -> None:
    while not stop.is_set():
        cockpit.repair_layout(pane)
        stop.wait(LAYOUT_REPAIR_INTERVAL)


def main() -> int:
    stop = threading.Event()
    pane = os.environ.get("TMUX_PANE")
    if pane:
        threading.Thread(
            target=_maintain_sidebar,
            args=(stop, pane),
            daemon=True,
        ).start()
    try:
        while True:
            try:
                curses.wrapper(run)
                return 0
            except KeyboardInterrupt:
                pass
    finally:
        stop.set()
