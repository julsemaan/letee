from __future__ import annotations

import re
import time

from .conftest import TmuxTestClient


_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _row_with_text(ansi: str, text: str) -> str:
    rows = [row for row in ansi.splitlines() if text in _ANSI.sub("", row)]
    if len(rows) != 1:
        raise AssertionError(f"Expected one row containing {text!r}, found {len(rows)}\nSidebar:\n{ansi}")
    return rows[0]


def _style_at(ansi: str, text: str) -> frozenset[str]:
    """Return semantic SGR state active at unique visible text."""
    row = _row_with_text(ansi, text)
    visible = ""
    style: set[str] = set()
    position = 0
    for match in _SGR.finditer(row):
        segment = row[position:match.start()]
        if text in visible + segment:
            return frozenset(style)
        visible += segment
        codes = [int(code or 0) for code in match.group(1).split(";")]
        i = 0
        while i < len(codes):
            code = codes[i]
            if code == 0:
                style.clear()
            elif code == 1:
                style.add("bold")
            elif code == 2:
                style.add("dim")
            elif code == 7:
                style.add("reverse")
            elif code == 22:
                style.difference_update(("bold", "dim"))
            elif code == 27:
                style.discard("reverse")
            elif 30 <= code <= 37 or 90 <= code <= 97:
                style = {item for item in style if not item.startswith("fg:")}
                style.add(f"fg:{code}")
            elif code == 39:
                style = {item for item in style if not item.startswith("fg:")}
            elif 40 <= code <= 47 or 100 <= code <= 107:
                style = {item for item in style if not item.startswith("bg:")}
                style.add(f"bg:{code}")
            elif code == 49:
                style = {item for item in style if not item.startswith("bg:")}
            elif code in (38, 48) and i + 2 < len(codes) and codes[i + 1] == 5:
                prefix = "fg:" if code == 38 else "bg:"
                style = {item for item in style if not item.startswith(prefix)}
                style.add(prefix + str(codes[i + 2]))
                i += 2
            i += 1
        position = match.end()
    if text in visible + row[position:]:
        return frozenset(style)
    raise AssertionError(f"Text {text!r} missing from styled row")


def assert_active_session_highlighted(
    client: TmuxTestClient, active_session: str, inactive_session: str
) -> None:
    ansi = client.sidebar_ansi()
    active = _style_at(ansi, active_session)
    inactive = _style_at(ansi, inactive_session)
    if active == inactive or not ({"bold", "reverse"} & active or any(x.startswith(("fg:", "bg:")) for x in active)):
        raise AssertionError(f"Active row {active_session!r} not distinguishable from {inactive_session!r}\nSidebar:\n{ansi}")


def assert_row_not_dimmed(client: TmuxTestClient, row_text: str) -> None:
    ansi = client.sidebar_ansi()
    if "dim" in _style_at(ansi, row_text):
        raise AssertionError(f"Row {row_text!r} is dimmed\nSidebar:\n{ansi}")


def assert_add_button_selected(client: TmuxTestClient) -> None:
    ansi = client.sidebar_ansi()
    plain = _ANSI.sub("", _row_with_text(ansi, "add"))
    if "> add" not in plain or _style_at(ansi, "add") == _style_at(ansi, "mtmux"):
        raise AssertionError(f"Add button is not selected\nSidebar:\n{ansi}")


def assert_sidebar_contains(client: TmuxTestClient, text: str, timeout: float = 5.0) -> None:
    """Assert sidebar pane contains given text within timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pane = client.sidebar_text()
        if pane and text in pane:
            return
        time.sleep(0.1)
    raise AssertionError(f"Sidebar did not contain '{text}' within {timeout}s\nLast seen: {client.sidebar_text()}")


def assert_right_pane_contains(client: TmuxTestClient, text: str, timeout: float = 5.0) -> None:
    """Assert right pane contains given text within timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pane = client.right_pane_text()
        if pane and text in pane:
            return
        time.sleep(0.1)
    raise AssertionError(f"Right pane did not contain '{text}' within {timeout}s\nLast seen: {client.right_pane_text()}")


def assert_cursor_on_row(client: TmuxTestClient, row_text: str) -> None:
    """Assert the `>` pointer appears on the row containing row_text."""
    sidebar = client.sidebar_text()
    for line in sidebar.splitlines():
        if row_text in line and ">" in line:
            return
    raise AssertionError(f"Cursor not found on row containing '{row_text}'\nSidebar:\n{sidebar}")


def assert_bell_icon_on(client: TmuxTestClient, session_name: str) -> None:
    """Assert BELL text appears on session_name row."""
    sidebar = client.sidebar_text()
    for line in sidebar.splitlines():
        if session_name in line and "BELL" in line:
            return
    raise AssertionError(f"Bell icon not found on row containing '{session_name}'\nSidebar:\n{sidebar}")


def assert_no_bell_icon(client: TmuxTestClient, session_name: str) -> None:
    """Assert no bell icon on session_name row."""
    sidebar = client.sidebar_text()
    for line in sidebar.splitlines():
        if session_name in line and "BELL" in line:
            raise AssertionError(f"Unexpected bell icon on row containing '{session_name}'\nSidebar:\n{sidebar}")
