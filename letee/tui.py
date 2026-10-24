"""Terminal formatting shared by curses views."""

from __future__ import annotations

import curses
import locale
import os
import unicodedata
from collections.abc import Callable

from .workspace import ATTENTION_STATES


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
_COLOR: dict[str, int] = {}


def ascii_mode() -> bool:
    enc = locale.getpreferredencoding(False).lower()
    return os.environ.get("LETEE_ASCII") == "1" or "utf" not in enc


# Optional overrides keep sidebar's existing patch points local to that view.
def icons(*, ascii: bool | None = None) -> dict[str, str]:
    if ascii_mode() if ascii is None else ascii:
        return {"local": "*", "remote": "*", "create": "+", "unavailable": "!", "selected": ">", "enter": "<-"}
    return {"local": "●", "remote": "◆", "create": "＋", "unavailable": "⚠", "selected": "›", "enter": "↵"}


def spinner_frame(now: float, *, ascii: bool | None = None) -> str:
    frames = ASCII_SPINNER if (ascii_mode() if ascii is None else ascii) else UNICODE_SPINNER
    return frames[int(now * 10) % len(frames)]


def status_icon(status: str, now: float, *, ascii: bool | None = None) -> str:
    if status == "working":
        return spinner_frame(now, ascii=ascii)
    status_icons = ASCII_STATUS_ICONS if (ascii_mode() if ascii is None else ascii) else UNICODE_STATUS_ICONS
    return status_icons.get(status, status_icons["unknown"])


def init_colors() -> None:
    _COLOR.clear()
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
        _COLOR.clear()


def color(name: str, *, palette: dict[str, int] | None = None) -> int:
    return (_COLOR if palette is None else palette).get(name, 0)


def fade(attr: int) -> int:
    return attr | curses.A_DIM


def truncate(text: str, width: int, *, ascii: bool | None = None) -> str:
    if len(text) <= width:
        return text
    ellipsis = "..." if (ascii_mode() if ascii is None else ascii) else "…"
    return ellipsis[:width] if width <= len(ellipsis) else text[: width - len(ellipsis)] + ellipsis


def cell_width(text: str) -> int:
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def truncate_cells(text: str, width: int, *, ascii: bool | None = None) -> str:
    if cell_width(text) <= width:
        return text
    ellipsis = "..." if (ascii_mode() if ascii is None else ascii) else "…"
    kept = ""
    for char in text:
        if cell_width(kept + char + ellipsis) > width:
            break
        kept += char
    return kept + ellipsis if kept else truncate(ellipsis, width, ascii=ascii)


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 60 * 60:
        return f"{seconds // 60}m"
    if seconds < 24 * 60 * 60:
        return f"{seconds // (60 * 60)}h"
    return f"{seconds // (24 * 60 * 60)}d"


def status_attr(status: str, *, color_lookup: Callable[[str], int] | None = None) -> int:
    lookup = color if color_lookup is None else color_lookup
    if status == "working":
        return lookup("agent_working") or 0
    if status == "submitted":
        return lookup("agent_submitted") or 0
    if status in ATTENTION_STATES:
        return (lookup("agent_" + status.replace("-", "_")) or 0) | curses.A_BOLD
    if status == "completed":
        return lookup("agent_completed") or 0
    if status in ("idle", "canceled"):
        return curses.A_DIM
    return lookup("agent_unknown") or 0
