from __future__ import annotations

import time

from .conftest import TmuxTestClient


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
