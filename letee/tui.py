"""Terminal formatting shared by curses views."""

from __future__ import annotations

import curses
import locale
import os
import unicodedata
from collections.abc import Callable

from . import diagnostics
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
# Logo palette as xterm-256 indexes. tmux-overlay.conf uses the same colors as hex;
# tests keep the two in sync.
PALETTE_256 = {
    "charcoal": 234,  # #1c1c1c bars
    "surface": 236,  # #303030 raised bars, current window
    "border": 238,  # #444444 inactive borders
    "muted": 244,  # #808080 secondary text
    "text": 252,  # #d0d0d0 body text in tmux
    "deep": 23,  # #005f5f search matches
    "teal": 30,  # #008787
    "green": 36,  # #00af87
    "mint": 79,  # #5fd7af brand accent
    "orange": 214,  # #ffaf00 current session/agent
    "amber": 179,  # #d7af5f unavailable
    "red": 167,  # #d75f5f alerts and failures
    "magenta": 176,  # #d787d7 auth required
}
# 8-color fallback: -1 is the terminal default; gray-less terminals dim it instead.
PALETTE_8 = {
    "charcoal": curses.COLOR_BLACK, "surface": curses.COLOR_BLACK, "border": -1, "muted": -1,
    "text": -1, "deep": curses.COLOR_CYAN, "teal": curses.COLOR_CYAN, "green": curses.COLOR_GREEN,
    "mint": curses.COLOR_CYAN, "orange": curses.COLOR_YELLOW, "amber": curses.COLOR_YELLOW,
    "red": curses.COLOR_RED, "magenta": curses.COLOR_MAGENTA,
}


def ascii_mode() -> bool:
    enc = locale.getpreferredencoding(False).lower()
    return os.environ.get("LETEE_ASCII") == "1" or "utf" not in enc


# Optional overrides keep sidebar's existing patch points local to that view.
def icons(*, ascii: bool | None = None) -> dict[str, str]:
    if ascii_mode() if ascii is None else ascii:
        return {"local": "*", "remote": "*", "create": "+", "unavailable": "!", "selected": ">", "enter": "<-", "alert": "!"}
    # The alert mark is deliberately narrow: emoji bells have terminal-dependent widths.
    return {"local": "●", "remote": "◆", "create": "＋", "unavailable": "⚠", "selected": "›", "enter": "↵", "alert": "!"}


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
        p = PALETTE_256 if getattr(curses, "COLORS", 0) >= 256 else PALETTE_8
        quiet = 0 if p["muted"] >= 0 else curses.A_DIM
        pairs = {
            "title": (1, p["mint"], p["charcoal"], curses.A_BOLD),
            "active": (2, p["orange"], -1, curses.A_BOLD),
            "local": (3, p["green"], -1, curses.A_BOLD),
            "remote": (4, p["teal"], -1, curses.A_BOLD),
            "create": (5, p["mint"], -1, 0),
            "unavailable": (6, p["amber"], -1, 0),
            "danger": (7, p["red"], -1, 0),
            "hints": (8, p["muted"], -1, quiet),
            "add_entry": (9, p["charcoal"], p["mint"], curses.A_BOLD),
            "slot": (10, p["muted"], -1, quiet),
            "slot_active": (11, p["orange"], -1, curses.A_BOLD),
            "agent_working": (12, p["green"], -1, 0),
            "agent_submitted": (13, p["teal"], -1, 0),
            "agent_input_required": (14, p["red"], -1, curses.A_BOLD),
            "agent_auth_required": (15, p["magenta"], -1, curses.A_BOLD),
            "agent_failed": (16, p["red"], -1, curses.A_BOLD),
            "agent_rejected": (17, p["red"], -1, curses.A_BOLD),
            "agent_completed": (18, p["mint"], -1, 0),
            "agent_unknown": (19, p["amber"], -1, 0),
            "move": (20, p["teal"], -1, curses.A_BOLD),
            "footer": (21, p["muted"], p["charcoal"], quiet),
        }
        for name, (pair, fg, bg, attr) in pairs.items():
            curses.init_pair(pair, fg, bg)
            _COLOR[name] = curses.color_pair(pair) | attr
        _COLOR["section"] = _COLOR["create"] | curses.A_BOLD
    except curses.error as error:
        diagnostics.error(error, operation="init_colors")
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
