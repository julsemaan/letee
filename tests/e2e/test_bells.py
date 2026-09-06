from __future__ import annotations

import time

from .conftest import TmuxTestClient
from .helpers import assert_no_bell_icon


def _setup(client: TmuxTestClient, config_dir: str, *sessions: str) -> None:
    for name in sessions:
        client.inner_tmux("kill-session", "-t", name, check=False)
        client.inner_tmux("new-session", "-d", "-s", name)
        client.inner_tmux("set-window-option", "-t", name, "monitor-bell", "on")
    client.write_file(f"{config_dir}/sessions", "".join(f"local:{name}\n" for name in sessions))
    client.start_cockpit(env={"LETEE_CONFIG_DIR": config_dir, "LETEE_ASCII": "1"})
    assert client.wait_for_sidebar_text("letee", timeout=10)


def _ring(client: TmuxTestClient, session: str) -> None:
    client.inner_tmux("send-keys", "-t", session, "printf '\\a'", "Enter")


def _switch(client: TmuxTestClient, config_dir: str, session: str) -> None:
    client.cli("switch", f"local:{session}", env={"LETEE_CONFIG_DIR": config_dir, "LETEE_ASCII": "1"})
    time.sleep(0.6)


def _cleanup(client: TmuxTestClient, *sessions: str) -> None:
    client.stop_cockpit()
    for name in sessions:
        client.inner_tmux("kill-session", "-t", name, check=False)


def test_bell_icon_on_ringing_session(client: TmuxTestClient) -> None:
    config = "/tmp/letee-e2e-bell"
    _setup(client, config, "beeper", "other")
    try:
        _switch(client, config, "other")
        _ring(client, "beeper")
        # capture-pane can place curses text on wrong visual row; flag + rendered marker
        # proves discovery and rendering without unstable row reconstruction.
        assert client.inner_tmux(
            "display-message", "-p", "-t", "beeper", "#{window_bell_flag}"
        ) == "1"
        assert client.wait_for_sidebar_text("BELL", timeout=3)
    finally:
        _cleanup(client, "beeper", "other")


def test_bell_clears_on_switch(client: TmuxTestClient) -> None:
    config = "/tmp/letee-e2e-bell-clear"
    _setup(client, config, "beeper", "other")
    try:
        _switch(client, config, "other")
        _ring(client, "beeper")
        assert client.wait_for_sidebar_text("BELL", timeout=3)
        _switch(client, config, "beeper")
        assert_no_bell_icon(client, "beeper")
    finally:
        _cleanup(client, "beeper", "other")


def test_multiple_bells(client: TmuxTestClient) -> None:
    config = "/tmp/letee-e2e-bells-multiple"
    _setup(client, config, "bell-a", "bell-b", "quiet-c")
    try:
        _switch(client, config, "quiet-c")
        _ring(client, "bell-a")
        _ring(client, "bell-b")
        assert client.wait_for_sidebar_text("BELL", timeout=3)
        assert client.inner_tmux(
            "display-message", "-p", "-t", "bell-a", "#{window_bell_flag}"
        ) == "1"
        assert client.inner_tmux(
            "display-message", "-p", "-t", "bell-b", "#{window_bell_flag}"
        ) == "1"
        assert client.inner_tmux(
            "display-message", "-p", "-t", "quiet-c", "#{window_bell_flag}"
        ) == "0"
        _switch(client, config, "bell-a")
        assert client.inner_tmux(
            "display-message", "-p", "-t", "bell-a", "#{window_bell_flag}"
        ) == "0"
        assert client.inner_tmux(
            "display-message", "-p", "-t", "bell-b", "#{window_bell_flag}"
        ) == "1"
        assert client.wait_for_sidebar_text("BELL", timeout=3)
    finally:
        _cleanup(client, "bell-a", "bell-b", "quiet-c")


def test_no_bell_icon_on_current_session(client: TmuxTestClient) -> None:
    config = "/tmp/letee-e2e-bell-current"
    _setup(client, config, "beeper", "other")
    try:
        _switch(client, config, "beeper")
        _ring(client, "beeper")
        time.sleep(0.8)
        assert_no_bell_icon(client, "beeper")
        # tmux consumes bell flag while window is attached; switching later must not
        # resurrect an already-consumed alert.
        _switch(client, config, "other")
        assert_no_bell_icon(client, "beeper")
    finally:
        _cleanup(client, "beeper", "other")


def test_no_bell_on_unstarred_session(client: TmuxTestClient) -> None:
    config = "/tmp/letee-e2e-bell-unstarred"
    _setup(client, config, "starred")
    client.inner_tmux("new-session", "-d", "-s", "noisy")
    client.inner_tmux("set-window-option", "-t", "noisy", "monitor-bell", "on")
    try:
        _ring(client, "noisy")
        time.sleep(0.8)
        assert "noisy" not in client.sidebar_text()
    finally:
        _cleanup(client, "starred", "noisy")
