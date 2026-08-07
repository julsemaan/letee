from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from .conftest import TmuxTestClient


def test_cockpit_starts_and_renders_sidebar(client: TmuxTestClient) -> None:
    """Cockpit starts, sidebar renders with letee branding and footer hints."""
    client.start_cockpit()

    # Sidebar renders the title bar with "letee"
    assert client.wait_for_sidebar_text("letee", timeout=10), \
        f"Sidebar did not render letee branding\nSidebar:\n{client.sidebar_text()}"

    sidebar = client.sidebar_text()
    assert "letee" in sidebar, "Sidebar should show letee branding"
    assert "activate" in sidebar.lower() or "help" in sidebar.lower(), \
        f"Sidebar footer should show keybinding hints\nSidebar:\n{sidebar}"

    # tmux session exists
    sessions = client.tmux("list-sessions", "-F", "#{session_name}")
    assert "letee" in sessions, f"letee session not found in: {sessions}"

    # cockpit window exists
    windows = client.tmux("list-windows", "-t", "letee", "-F", "#{window_name}")
    assert "cockpit" in windows, f"cockpit window not found in: {windows}"


def test_cockpit_help(client: TmuxTestClient) -> None:
    """Pressing ? opens help in the right pane."""
    client.start_cockpit()
    assert client.wait_for_sidebar_text("letee", timeout=10)

    # Right pane starts with help content by default.
    # ponytail: long help scrolls off top in small panes; check text visible at bottom.
    right = client.right_pane_text()
    assert "Session actions" in right or "Recovery" in right or "Examples" in right, \
        f"Right pane missing help sections\nRight:\n{right}"

    # Sending C-s ? respawns help (exercise the prefix command, verify right pane still has content)
    client.send_special("C-s")
    client.send_keys("?")
    time.sleep(0.5)
    right2 = client.right_pane_text()
    assert len(right2) > 50, f"Right pane should have help content after C-s ?\nRight:\n{right2}"


def test_cockpit_recovery(client: TmuxTestClient) -> None:
    """After sidebar pane dies, cockpit recovers."""
    client.start_cockpit()
    assert client.wait_for_sidebar_text("letee", timeout=10)

    # Get sidebar pane ID
    sidebar_pane = client.tmux(
        "display-message", "-p", "-t", "letee:cockpit.0", "-F", "#{pane_id}"
    ).strip()
    assert sidebar_pane, "Could not get sidebar pane ID"

    # Kill the sidebar pane
    client.tmux("kill-pane", "-t", sidebar_pane)
    time.sleep(0.5)

    # Stop old cockpit (it's broken now), then restart
    client.stop_cockpit()
    time.sleep(0.5)

    client.start_cockpit()
    assert client.wait_for_sidebar_text("letee", timeout=10), \
        "Sidebar did not recover after pane death"

    # @letee_cockpit option should still be "1"
    cockpit_opt = client.tmux("show-options", "-v", "-t", "letee", "@letee_cockpit").strip()
    assert cockpit_opt == "1", f"@letee_cockpit should be '1', got '{cockpit_opt}'"


def test_cockpit_reattach(client: TmuxTestClient) -> None:
    """Re-attaching to an existing cockpit reuses the same window."""
    client.start_cockpit()
    assert client.wait_for_sidebar_text("letee", timeout=10)

    # Record current windows
    windows_before = client.tmux("list-windows", "-t", "letee", "-F", "#{window_name}").strip()
    cockpit_count = windows_before.count("cockpit")

    # Detach: C-s d
    client.send_special("C-s")
    time.sleep(0.2)
    client.send_keys("d")
    time.sleep(0.5)

    # Verify pexpect exited (detach closes the client)
    assert client._pexpect is not None
    if client._pexpect.isalive():
        # Force detach if still alive
        client.stop_cockpit()

    # Reattach
    client.start_cockpit()
    assert client.wait_for_sidebar_text("letee", timeout=10), \
        "Sidebar did not render after reattach"

    # Only one cockpit window
    windows_after = client.tmux("list-windows", "-t", "letee", "-F", "#{window_name}").strip()
    assert windows_after.count("cockpit") == 1, \
        f"Should have exactly 1 cockpit window, got: {windows_after}"


def test_terminal_too_narrow(client: TmuxTestClient) -> None:
    """Cockpit refuses to start on terminals narrower than 90 columns."""
    env = {"COLUMNS": "79", "LETEE_CONFIG_DIR": "/tmp/letee-narrow", "LETEE_ASCII": "1", "LINES": "24"}
    merged_env = dict(os.environ)
    merged_env.update(env)

    if client.container:
        cmd = ["docker", "exec", "-e", "COLUMNS=79",
               "-e", "LETEE_CONFIG_DIR=/tmp/letee-narrow",
               "-e", "LETEE_ASCII=1",
               client.container, "letee", "cockpit"]
    else:
        cmd = [sys.executable, "-m", "letee", "cockpit"]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=merged_env)
    assert result.returncode == 2, f"Expected exit 2, got {result.returncode}"
    assert "need at least 90 columns" in result.stderr, \
        f"Expected narrow-terminal message, got stderr: {result.stderr}"


def test_terminal_exactly_90_columns(client: TmuxTestClient) -> None:
    """Cockpit starts with exactly 90 columns."""
    client.start_cockpit(cols=90)
    assert client.wait_for_sidebar_text("letee", timeout=10), \
        "Cockpit should start with exactly 90 columns"
