from __future__ import annotations

import curses
import locale
import os
import socket
import textwrap
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

from . import cockpit, sessions
from .discovery import DiscoveryPoller, SessionSnapshot
from .config import load_hosts, load_sessions, load_status_timeout, save_sessions
from .names import Target, validate_name


UI_POLL_INTERVAL_MS = 50
COCKPIT_BELL_POLL_INTERVAL = 0.5


@dataclass(frozen=True)
class Effect:
    kind: Literal["switch", "add_switch", "create", "kill", "help", "save_favorites", "status", "quit"]
    target: Target | None = None
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


@dataclass(frozen=True)
class Entry:
    label: str
    kind: str  # section | header | host | session | unavailable
    target: Target | None = None
    host: str | None = None
    unavailable_favorite: bool = False
    tracked: bool = False
    shortcut_slot: int | None = None


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
            charcoal, teal, green, mint, orange = 233, 30, 36, 79, 214
        else:
            charcoal, teal, green, mint, orange = (
                curses.COLOR_BLACK, curses.COLOR_CYAN, curses.COLOR_GREEN, curses.COLOR_CYAN, curses.COLOR_YELLOW
            )
        pairs = {
            "title": (1, mint, charcoal, curses.A_BOLD),
            "active": (2, orange, -1, curses.A_BOLD),
            "local": (3, green, -1, 0),
            "remote": (4, teal, -1, 0),
            "create": (5, mint, -1, 0),
            "unavailable": (6, curses.COLOR_YELLOW, -1, curses.A_DIM),
            "danger": (7, curses.COLOR_RED, -1, 0),
            "hints": (8, teal, -1, curses.A_DIM),
            "add_entry": (9, charcoal, mint, curses.A_BOLD),
            "slot": (10, mint, -1, curses.A_BOLD | curses.A_REVERSE),
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


def _entries(
    filter_text: str,
    snapshot: SessionSnapshot,
    favorites: list[Target] | None = None,
    adding: bool = False,
) -> list[Entry]:
    needle = filter_text.lower()
    adding = adding or favorites is None
    favorites = favorites or []
    available = set(snapshot.sessions)
    hostname = socket.gethostname()
    if not adding:
        slots = {target: slot for slot, target in enumerate(favorites[:9], 1)}
        out = [Entry("Add session", "add"), Entry("", "spacer")]
        out.extend(
            Entry(target.session, "session", target, target.host or hostname, target not in available, True, slots.get(target))
            for target in favorites
        )
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
            label = f"unavailable: {source.error}" if source.error else "unavailable"
            out.append(Entry(label, "unavailable", host=host))
            continue
        for target in source.sessions:
            if target not in favorites and needle in target.session.lower():
                out.append(Entry(target.session, "session", target, host))
    return out


def _selectable(entries: list[Entry]) -> list[int]:
    return [i for i, entry in enumerate(entries) if entry.kind in ("session", "host", "add")]


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
        if effect.kind in ("switch", "add_switch") and effect.target:
            if effect.kind == "add_switch" and effect.target not in state.favorites:
                state.favorites.append(effect.target)
                save_sessions(state.favorites)
            cockpit.switch(effect.target, sessions.attach_command(effect.target))
            state.filter_text = ""
            state.filtering = False
            state.selected_target = effect.target
            _set_status(state, f"switched {effect.target.format()}", status_timeout)
        elif effect.kind == "create" and effect.target:
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
    return 2 if entry.tracked else 1


def _viewport(entries: list[Entry], selected: int, height: int) -> tuple[int, int]:
    body = max(0, height - 2)
    if not entries or body <= 0:
        return selected, selected
    if body == 1:
        return selected, min(len(entries), selected + 1)

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
    entries: list[Entry], selected: int, row: int, height: int, footer_height: int, top: int = 1
) -> int | None:
    content_height = height - footer_height - top + 2
    start, end = _viewport(entries, selected, content_height)
    entry_row = row - top - int(start > 0)
    if entry_row < 0 or row >= height - footer_height:
        return None
    for index in range(start, end):
        if entry_row < _entry_height(entries[index]):
            return index if entries[index].kind in ("session", "host", "add") else None
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


def _entry_lines(
    entry: Entry,
    selected: bool,
    bell_targets: set[Target],
    current_target: Target | None,
    width: int,
    creation_host: str | None = None,
    creation_text: str = "",
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
        host_icon = icon["local_header"] if entry.host == "" else icon["remote_header"]
        suffix = icon["create"]
        label = _truncate_cells(f"{host_icon} {entry.label}", max(0, width - _cell_width(suffix) - 1))
        padding = max(1, width - _cell_width(label) - _cell_width(suffix))
        return [_truncate(label + " " * padding + suffix, width)]
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
            suffix = " unavailable" if entry.unavailable_favorite else ""
            branch = "`-" if _ascii() else "└─"
            meta_prefix = f"  {branch} "
            host = _truncate_cells(entry.host or "", max(0, width - _cell_width(meta_prefix) - _cell_width(host_prefix) - _cell_width(suffix)))
            return [first, meta_prefix + host_prefix + host + suffix]
        prefix = f"{pointer} {icon[kind]} "
        first = prefix + _truncate_cells(entry.label, max(0, width - _cell_width(prefix) - _cell_width(bell))) + bell
        return [first]
    if entry.kind == "hint":
        return [_truncate_cells(f"  {icon['enter']} {entry.label}", width)]
    return [_truncate(f"  {icon['unavailable']} {entry.label}", width)]


def _entry_attr(entry: Entry, active: bool, dimmed: bool = False) -> int:
    if active:
        attr = _color("active") or curses.A_REVERSE
    elif entry.kind == "section":
        attr = _color("section") or curses.A_BOLD
    elif entry.kind == "add":
        attr = _color("add_entry") or (curses.A_BOLD | curses.A_REVERSE)
    elif entry.kind in ("header", "host"):
        attr = curses.A_BOLD
    elif entry.unavailable_favorite:
        attr = _color("unavailable") or curses.A_DIM
    elif entry.kind == "session" and entry.target and entry.target.kind == "ssh":
        attr = _color("remote")
    elif entry.kind == "session":
        attr = _color("local")
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
) -> tuple[int, int] | None:
    cursor = None
    start, end = _viewport(entries, _view_index(entries, selected, current_target, dimmed), h - top + 1)
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
        lines = _entry_lines(
            entry, selected_entry and not dimmed, bell_targets, current_target, w,
            creation_host, creation_text,
        )
        host_selected = selected_entry and entry.kind == "host" and not dimmed
        base_attr = _entry_attr(entry, active_entry or host_selected, dimmed)
        slot_badge = ""
        slot_width = 0
        if entry.tracked and entry.shortcut_slot is not None:
            slot_width = 4
            ico = _icons()
            pointer_char = ico["selected"] if selected_entry and not dimmed else " "
        for line_number, line in enumerate(lines):
            if row >= h - 1:
                break
            attr = _fade(base_attr) if line_number and not active_entry else base_attr
            if line_number == 0 and slot_width:
                slot_badge = f" {pointer_char} " if selected_entry else f"[{entry.shortcut_slot}]"
                stdscr.addnstr(row, 0, slot_badge, w, _color("slot") or curses.A_BOLD)
                stdscr.addnstr(row, 3, " " + line, w - 3, attr)
            else:
                stdscr.addnstr(row, 0, line, w, attr)
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
    status: str,
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
        logical_rows = [status or f"{'Enter' if _ascii() else '↵'} add  x kill", "/ filter  Esc back  ? help  q quit"]
    else:
        logical_rows = [status or f"{'Enter' if _ascii() else '↵'} activate  a add  r remove", "x kill  K/J reorder  ? help  q quit"]
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
) -> int:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    cursor = _draw_title(stdscr, w, entries, filter_text, filtering, dimmed, adding)
    if filtering:
        cursor = _draw_filter(stdscr, w, filter_text, dimmed)
    footer_height = _draw_footer(stdscr, h, w, status, filtering, dimmed, creation_host is not None, adding)
    creation_cursor = _draw_entries(
        stdscr,
        entries,
        selected,
        h - footer_height + 1,
        w,
        bell_targets or set(),
        current_target,
        dimmed,
        creation_host,
        creation_text,
        2 if filtering else 1,
    )
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
    state.selected_index = _selected_index(entries, state.selected_target)
    _sync_selection(state, entries)
    cockpit_bell_target: Target | None = None
    next_cockpit_bell_poll = 0.0
    rendered: tuple[object, ...] | None = None
    footer_height = 0
    stdscr.timeout(UI_POLL_INTERVAL_MS)

    def show_status(message: str) -> None:
        _set_status(state, message, status_timeout)

    def rebuild() -> None:
        nonlocal entries
        entries = _entries(state.filter_text, poller.snapshot, state.favorites, state.adding)
        _sync_selection(state, entries)

    try:
        while True:
            now = time.monotonic()
            if state.status_deadline is not None and now >= state.status_deadline:
                state.status = ""
                state.status_deadline = None
            if poller.tick():
                rebuild()
            selectable = _selectable(entries)
            if selectable and state.selected_index not in selectable:
                state.selected_index = selectable[0]
            if now >= next_cockpit_bell_poll:
                cockpit_bell_target = cockpit.bell_target()
                next_cockpit_bell_poll = now + COCKPIT_BELL_POLL_INTERVAL
            bell_targets = _bell_targets(poller.snapshot, cockpit_bell_target, state.favorites)
            current_target = _current_target()
            visible_bells = bell_targets - ({current_target} if current_target else set())
            if visible_bells - state.rang_bells:
                curses.beep()
            state.rang_bells = bell_targets
            dimmed = not _pane_active()
            render_state = (
                tuple(entries), state.selected_index, state.status, state.filter_text,
                state.filtering, state.adding, state.creation_host, state.creation_text,
                frozenset(bell_targets), current_target, dimmed, stdscr.getmaxyx(),
            )
            if render_state != rendered:
                footer_height = _draw(
                    stdscr, entries, state.selected_index, state.status, state.filter_text,
                    state.filtering, bell_targets, current_target, dimmed,
                    state.creation_host, state.creation_text, state.adding,
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
                    _, _, row, _, mouse_state = curses.getmouse()
                except (curses.error, TypeError, ValueError):
                    continue
                if not isinstance(row, int) or not isinstance(mouse_state, int):
                    continue
                if mouse_state & (getattr(curses, "BUTTON4_PRESSED", 0) or 0):
                    key = curses.KEY_UP
                elif mouse_state & (getattr(curses, "BUTTON5_PRESSED", 0) or 0):
                    key = curses.KEY_DOWN
                else:
                    view_index = _view_index(entries, state.selected_index, current_target, dimmed)
                    index = _entry_at_row(
                        entries, view_index, row, stdscr.getmaxyx()[0], footer_height,
                        2 if state.filtering else 1,
                    )
                    if index is None:
                        continue
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
            elif key in (curses.KEY_DOWN, ord("j")) and selectable:
                state.selected_index = selectable[(selectable.index(state.selected_index) + 1) % len(selectable)]
                state.selected_target = entries[state.selected_index].target
                state.selected_tracked = entries[state.selected_index].tracked
            elif key in (curses.KEY_UP, ord("k")) and selectable:
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
                entry = entries[state.selected_index]
                if entry.kind == "add":
                    state.adding = True
                    state.selected_target = None
                    rebuild()
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
