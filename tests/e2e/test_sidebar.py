from __future__ import annotations

import time

from .conftest import TmuxTestClient
from .helpers import (
    assert_active_session_highlighted,
    assert_add_button_selected,
    assert_cursor_on_row,
    assert_row_dimmed,
    assert_sidebar_contains,
)


def _start(client: TmuxTestClient, config_dir: str) -> None:
    for name in ("alpha", "beta"):
        client.default_tmux("kill-session", "-t", name, check=False)
        client.default_tmux("new-session", "-d", "-s", name)
    client.write_file(f"{config_dir}/sessions", "local:alpha\nlocal:beta\n")
    client.start_cockpit(env={
        "MTMUX_CONFIG_DIR": config_dir,
        "MTMUX_ASCII": "1",
        "TERM": "xterm-256color",
    })
    assert client.wait_for_sidebar_text("alpha", timeout=10)
    _focus_sidebar(client)


def _send(client: TmuxTestClient, key: str) -> None:
    if key in ("Enter", "Up", "Down"):
        client.send_special(key)
    else:
        client.send_keys(key)
    time.sleep(0.2)


def _wait_for_active_pane(
    client: TmuxTestClient,
    pane: int,
    timeout: float = 5.0,
) -> None:
    deadline = time.monotonic() + timeout
    active = ""
    while time.monotonic() < deadline:
        active = client.tmux(
            "display-message", "-p", "-t", f"mtmux:cockpit.{pane}", "#{pane_active}",
        )
        if active == "1":
            return
        time.sleep(0.1)
    raise AssertionError(
        f"Expected mtmux:cockpit.{pane} to be active, got pane_active={active!r}"
    )


def _focus_sidebar(client: TmuxTestClient) -> None:
    client.send_special("C-s")
    client.send_keys("s")
    _wait_for_active_pane(client, 0)


def _cleanup(client: TmuxTestClient) -> None:
    client.stop_cockpit()
    for name in ("alpha", "beta"):
        client.default_tmux("kill-session", "-t", name, check=False)


def test_cursor_moves_with_jk_and_arrow_keys(client: TmuxTestClient) -> None:
    _start(client, "/tmp/mtmux-e2e-sidebar-cursor")
    try:
        assert_cursor_on_row(client, "alpha")
        _send(client, "j")
        assert_cursor_on_row(client, "beta")
        _send(client, "Up")
        assert_cursor_on_row(client, "alpha")
        _send(client, "Down")
        assert_cursor_on_row(client, "beta")
        _send(client, "k")
        assert_cursor_on_row(client, "alpha")
    finally:
        _cleanup(client)


def test_cursor_and_active_session_move_independently(client: TmuxTestClient) -> None:
    _start(client, "/tmp/mtmux-e2e-sidebar-active")
    try:
        _send(client, "Enter")
        _wait_for_active_pane(client, 1)
        _focus_sidebar(client)
        assert_active_session_highlighted(client, "alpha", "beta")
        _send(client, "j")
        assert_cursor_on_row(client, "beta")
        assert_active_session_highlighted(client, "alpha", "beta")
        _send(client, "Enter")
        assert_active_session_highlighted(client, "beta", "alpha")
    finally:
        _cleanup(client)


def test_unfocused_sidebar_hides_cursor_and_dims_rows(client: TmuxTestClient) -> None:
    _start(client, "/tmp/mtmux-e2e-sidebar-focus")
    try:
        _send(client, "Enter")
        _wait_for_active_pane(client, 1)
        assert ">" not in client.sidebar_text()
        assert_row_dimmed(client, "alpha")
        assert_active_session_highlighted(client, "alpha", "beta")

        _focus_sidebar(client)
        assert_cursor_on_row(client, "alpha")
    finally:
        _cleanup(client)


def test_add_button_selection_opens_add_session_view(client: TmuxTestClient) -> None:
    _start(client, "/tmp/mtmux-e2e-sidebar-add")
    try:
        _send(client, "k")
        assert_add_button_selected(client)
        _send(client, "Enter")
        assert_sidebar_contains(client, "Add session")
    finally:
        _cleanup(client)
