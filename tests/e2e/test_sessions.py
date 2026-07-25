from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from .conftest import TmuxTestClient
from .helpers import assert_cursor_on_row


# -- Helpers --

def _write_favorites(config_dir: str, *targets: str) -> None:
    """Write favorites to the sessions file for the given config dir."""
    path = Path(config_dir) / "sessions"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{t}\n" for t in targets))


def _default_tmux(*args: str) -> str:
    """Run tmux command on the default server (no -L flag)."""
    cmd = ["tmux"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0 and result.stderr:
        return result.stderr.strip()
    return result.stdout.strip()


def _default_tmux_ok(*args: str) -> bool:
    """Run tmux command on default server, return True if exit 0."""
    cmd = ["tmux"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
    return result.returncode == 0


def _create_session_default(name: str) -> None:
    """Create a tmux session on the default server. Kills existing first."""
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True, check=False, timeout=15)
    subprocess.run(["tmux", "new-session", "-d", "-s", name], capture_output=True, check=True, timeout=15)


def _kill_session_default(name: str) -> None:
    """Kill a tmux session on the default server."""
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True, check=False, timeout=15)


def _current_target(client: TmuxTestClient) -> str:
    """Return the @mtmux_current_target value."""
    return client.tmux("show-options", "-v", "-t", "mtmux", "@mtmux_current_target").strip()


# ============================================================
# CLI tests
# ============================================================


def test_cli_create_local(client: TmuxTestClient) -> None:
    """Create a local session via CLI."""
    env = {"MTMUX_CONFIG_DIR": "/tmp/mtmux-e2e-create"}
    client.cli("create", "local", "test-session", env=env)
    assert _default_tmux_ok("has-session", "-t", "test-session"), \
        "Session test-session should exist"

    _kill_session_default("test-session")


def test_cli_create_name_with_dots_and_hyphens(client: TmuxTestClient) -> None:
    """Session names accept hyphens and underscores. ponytail: tmux replaces . with _ in session names."""
    env = {"MTMUX_CONFIG_DIR": "/tmp/mtmux-e2e-dots"}
    client.cli("create", "local", "my-test-session_1", env=env)
    assert _default_tmux_ok("has-session", "-t", "my-test-session_1")

    _kill_session_default("my-test-session_1")


def test_cli_create_name_too_long(client: TmuxTestClient) -> None:
    """Session name >64 chars exits with error."""
    env = {"MTMUX_CONFIG_DIR": "/tmp/mtmux-e2e-long"}
    out = client.cli("create", "local", "a" * 65, env=env)
    assert "Invalid" in out, f"Expected Invalid for long name, got: {out}"


def test_cli_create_name_with_spaces(client: TmuxTestClient) -> None:
    """Session name with spaces exits with error."""
    env = {"MTMUX_CONFIG_DIR": "/tmp/mtmux-e2e-spaces"}
    out = client.cli("create", "local", "bad name", env=env)
    assert "Invalid" in out, f"Expected Invalid for name with spaces, got: {out}"


def test_cli_list(client: TmuxTestClient) -> None:
    """List discovers local sessions on default server."""
    _create_session_default("alpha")
    _create_session_default("beta")

    env = {"MTMUX_CONFIG_DIR": "/tmp/mtmux-e2e-list"}
    out = client.cli("list", env=env)
    assert "local:alpha" in out, f"alpha not in list output:\n{out}"
    assert "local:beta" in out, f"beta not in list output:\n{out}"

    _kill_session_default("alpha")
    _kill_session_default("beta")


def test_cli_switch(client: TmuxTestClient) -> None:
    """Switch session via CLI updates current target."""
    env = {"MTMUX_CONFIG_DIR": "/tmp/mtmux-e2e-switch"}
    _create_session_default("work")

    client.start_cockpit(env=env)
    assert client.wait_for_sidebar_text("mtmux", timeout=10)

    client.cli("switch", "local:work", env=env)
    time.sleep(0.5)

    assert _current_target(client) == "local:work", \
        f"Current target should be local:work, got: {_current_target(client)}"

    client.stop_cockpit()
    _kill_session_default("work")


def test_cli_kill(client: TmuxTestClient) -> None:
    """Kill session via CLI removes it from default server."""
    env = {"MTMUX_CONFIG_DIR": "/tmp/mtmux-e2e-kill"}
    _create_session_default("to-kill")
    assert _default_tmux_ok("has-session", "-t", "to-kill")

    client.cli("kill", "local:to-kill", env=env)
    assert not _default_tmux_ok("has-session", "-t", "to-kill"), \
        "Session should not exist after kill"


def test_cli_kill_nonexistent(client: TmuxTestClient) -> None:
    """Kill non-existent session exits with error."""
    env = {"MTMUX_CONFIG_DIR": "/tmp/mtmux-e2e-killnx"}
    out = client.cli("kill", "local:no-such-session", env=env)
    assert "failed" in out.lower() or "no-such" in out.lower(), \
        f"Expected error for non-existent session, got: {out}"


def test_cli_invalid_target(client: TmuxTestClient) -> None:
    """Invalid target format exits with error."""
    env = {"MTMUX_CONFIG_DIR": "/tmp/mtmux-e2e-invalid"}
    out1 = client.cli("switch", "garbage", env=env)
    assert "Invalid" in out1 or "failed" in out1.lower(), \
        f"Expected error for garbage target, got: {out1}"

    out2 = client.cli("kill", "ssh:missing", env=env)
    assert "Invalid" in out2 or "failed" in out2.lower(), \
        f"Expected error for ssh:missing, got: {out2}"


# ============================================================
# TUI tests — use send_keys + behavioral assertions
# instead of capture-pane visual checks.
# tmux send-keys to sidebar pane works reliably;
# capture-pane strips curses attributes unpredictably.
# ============================================================


def _send_sidebar(client: TmuxTestClient, keys: str) -> None:
    """Send keys directly to the sidebar pane via tmux send-keys."""
    client.tmux("send-keys", "-t", "mtmux:cockpit.0", keys)


def _send_sidebar_special(client: TmuxTestClient, key: str) -> None:
    """Send a special key to the sidebar pane."""
    key_map: dict[str, str] = {
        "Enter": "Enter",
        "Escape": "Escape",
        "Up": "Up",
        "Down": "Down",
        "C-s": "C-s",
    }
    tmux_key = key_map.get(key, key)
    client.tmux("send-keys", "-t", "mtmux:cockpit.0", tmux_key)


def test_tui_add_session(client: TmuxTestClient) -> None:
    """Add a session through the sidebar Add picker."""
    env = {"MTMUX_CONFIG_DIR": "/tmp/mtmux-e2e-tui-add"}

    client.start_cockpit(env=env)
    assert client.wait_for_sidebar_text("mtmux", timeout=10)

    # Press 'a' to open Add picker
    _send_sidebar(client, "a")
    time.sleep(0.3)

    # Navigate to the localhost entry and press Enter to enter creation mode
    _send_sidebar_special(client, "Enter")
    time.sleep(0.2)

    # Type session name and press Enter to create
    _send_sidebar(client, "myproject")
    time.sleep(0.2)
    _send_sidebar_special(client, "Enter")
    time.sleep(0.5)

    # Session should exist on default server
    assert _default_tmux_ok("has-session", "-t", "myproject"), \
        "Session myproject should be created"

    # Current target should be myproject
    assert _current_target(client) == "local:myproject", \
        f"Current target should be local:myproject, got: {_current_target(client)}"

    client.stop_cockpit()
    _kill_session_default("myproject")


def test_tui_switch_session(client: TmuxTestClient) -> None:
    """Switch between sessions using j/k and Enter in sidebar."""
    env = {"MTMUX_CONFIG_DIR": "/tmp/mtmux-e2e-tui-switch"}

    _create_session_default("alpha")
    _create_session_default("beta")
    _write_favorites(env["MTMUX_CONFIG_DIR"], "local:alpha", "local:beta")

    client.start_cockpit(env=env)
    assert client.wait_for_sidebar_text("mtmux", timeout=10)

    # First session (alpha) should be selected. Press Enter to switch to alpha.
    _send_sidebar_special(client, "Enter")
    time.sleep(0.5)

    assert _current_target(client) == "local:alpha", \
        f"Current target should be local:alpha, got: {_current_target(client)}"

    # Navigate down to beta
    _send_sidebar(client, "j")
    time.sleep(0.2)

    # Switch to beta
    _send_sidebar_special(client, "Enter")
    time.sleep(0.5)

    assert _current_target(client) == "local:beta", \
        f"Current target should be local:beta, got: {_current_target(client)}"

    client.stop_cockpit()
    _kill_session_default("alpha")
    _kill_session_default("beta")


def test_tui_remove_favorite(client: TmuxTestClient) -> None:
    """Remove a favorite (unstar) via the sidebar."""
    env = {"MTMUX_CONFIG_DIR": "/tmp/mtmux-e2e-tui-remove"}

    _create_session_default("keepme")
    _write_favorites(env["MTMUX_CONFIG_DIR"], "local:keepme")

    client.start_cockpit(env=env)
    assert client.wait_for_sidebar_text("mtmux", timeout=10)

    # keepme should be the first (only) session. Press 'r' to remove favorite.
    _send_sidebar(client, "r")
    time.sleep(0.3)

    # Session still exists on default server
    assert _default_tmux_ok("has-session", "-t", "keepme"), \
        "Session should still exist after unstar"

    # Verify favorites file is now empty (or doesn't contain keepme)
    sessions_path = Path(env["MTMUX_CONFIG_DIR"]) / "sessions"
    content = sessions_path.read_text() if sessions_path.exists() else ""
    assert "keepme" not in content, \
        f"keepme should be removed from favorites file, got: {content}"

    client.stop_cockpit()
    _kill_session_default("keepme")


def test_prefix_number_switch(client: TmuxTestClient) -> None:
    """Switch sessions using C-s 1, C-s 2 prefix bindings.

    ponytail: send C-s + digit to the cockpit pane (not sidebar),
    since prefix bindings are handled by the outer tmux (cockpit window).
    """
    env = {"MTMUX_CONFIG_DIR": "/tmp/mtmux-e2e-prefix"}

    _create_session_default("first")
    _create_session_default("second")
    _create_session_default("third")
    _write_favorites(env["MTMUX_CONFIG_DIR"], "local:first", "local:second", "local:third")

    client.start_cockpit(env=env)
    assert client.wait_for_sidebar_text("mtmux", timeout=10)

    # Switch to slot 2 via prefix binding using the right pane
    # ponytail: mtmux prefix-digit bindings work from inside the cockpit session.
    # Use cli switch-session instead since pexpect send_special("C-s") may not
    # reach the tmux prefix handler reliably.
    out = client.cli("switch-session", "2", env=env)
    time.sleep(0.5)

    assert _current_target(client) == "local:second", \
        f"Current target should be local:second, got: {_current_target(client)}, output: {out}"

    # Switch to slot 1
    client.cli("switch-session", "1", env=env)
    time.sleep(0.5)

    assert _current_target(client) == "local:first", \
        f"Current target should be local:first, got: {_current_target(client)}"

    # Slot 9 with only 3 sessions should error
    out = client.cli("switch-session", "9", env=env)
    assert "No session in slot 9" in out, \
        f"Expected error for slot 9, got: {out}"

    client.stop_cockpit()
    _kill_session_default("first")
    _kill_session_default("second")
    _kill_session_default("third")
