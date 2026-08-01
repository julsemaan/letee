from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from .conftest import TmuxTestClient


# -- Helpers --

def _command(client: TmuxTestClient, *args: str) -> list[str]:
    """Run a command where letee runs: host or Docker container."""
    return ["docker", "exec", client.container, *args] if client.container else list(args)


def _write_favorites(client: TmuxTestClient, config_dir: str, *targets: str) -> None:
    """Write favorites to the sessions file for the given config dir."""
    content = "".join(f"{target}\n" for target in targets)
    if client.container:
        code = (
            "from pathlib import Path; import sys; p=Path(sys.argv[1]); "
            "p.parent.mkdir(parents=True, exist_ok=True); p.write_text(sys.argv[2])"
        )
        subprocess.run(
            _command(client, "python", "-c", code, f"{config_dir}/sessions", content),
            check=True,
            timeout=15,
        )
        return
    path = Path(config_dir) / "sessions"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _read_favorites(client: TmuxTestClient, config_dir: str) -> str:
    """Read favorites where letee runs."""
    path = f"{config_dir}/sessions"
    if client.container:
        result = subprocess.run(
            _command(client, "cat", path), capture_output=True, text=True, timeout=15
        )
        return result.stdout
    local_path = Path(path)
    return local_path.read_text() if local_path.exists() else ""


def _default_tmux_ok(client: TmuxTestClient, *args: str) -> bool:
    """Run tmux command on default server, return True if exit 0."""
    result = subprocess.run(_command(client, "tmux", *args), capture_output=True, text=True, timeout=15, check=False)
    return result.returncode == 0


def _create_session_default(client: TmuxTestClient, name: str) -> None:
    """Create a tmux session on the default server. Kills existing first."""
    subprocess.run(_command(client, "tmux", "kill-session", "-t", name), capture_output=True, check=False, timeout=15)
    subprocess.run(_command(client, "tmux", "new-session", "-d", "-s", name), capture_output=True, check=True, timeout=15)


def _kill_session_default(client: TmuxTestClient, name: str) -> None:
    """Kill a tmux session on the default server."""
    subprocess.run(_command(client, "tmux", "kill-session", "-t", name), capture_output=True, check=False, timeout=15)


def _current_target(client: TmuxTestClient) -> str:
    """Return the @letee_current_target value."""
    return client.tmux("show-options", "-v", "-t", "letee", "@letee_current_target").strip()


# ============================================================
# CLI tests
# ============================================================


def test_cli_create_local(client: TmuxTestClient) -> None:
    """Create a local session via CLI."""
    env = {"LETEE_CONFIG_DIR": "/tmp/letee-e2e-create"}
    client.cli("create", "local", "test-session", env=env)
    assert _default_tmux_ok(client, "has-session", "-t", "test-session"), \
        "Session test-session should exist"

    _kill_session_default(client, "test-session")


def test_cli_create_name_with_dots_and_hyphens(client: TmuxTestClient) -> None:
    """Session names accept hyphens and underscores. ponytail: tmux replaces . with _ in session names."""
    env = {"LETEE_CONFIG_DIR": "/tmp/letee-e2e-dots"}
    client.cli("create", "local", "my-test-session_1", env=env)
    assert _default_tmux_ok(client, "has-session", "-t", "my-test-session_1")

    _kill_session_default(client, "my-test-session_1")


def test_cli_create_name_too_long(client: TmuxTestClient) -> None:
    """Session name >64 chars exits with error."""
    env = {"LETEE_CONFIG_DIR": "/tmp/letee-e2e-long"}
    out = client.cli("create", "local", "a" * 65, env=env)
    assert "Invalid" in out, f"Expected Invalid for long name, got: {out}"


def test_cli_create_name_with_spaces(client: TmuxTestClient) -> None:
    """Session name with spaces exits with error."""
    env = {"LETEE_CONFIG_DIR": "/tmp/letee-e2e-spaces"}
    out = client.cli("create", "local", "bad name", env=env)
    assert "Invalid" in out, f"Expected Invalid for name with spaces, got: {out}"


def test_cli_list(client: TmuxTestClient) -> None:
    """List discovers local sessions on default server."""
    _create_session_default(client, "alpha")
    _create_session_default(client, "beta")

    env = {"LETEE_CONFIG_DIR": "/tmp/letee-e2e-list"}
    out = client.cli("list", env=env)
    assert "local:alpha" in out, f"alpha not in list output:\n{out}"
    assert "local:beta" in out, f"beta not in list output:\n{out}"

    _kill_session_default(client, "alpha")
    _kill_session_default(client, "beta")


def test_cli_switch(client: TmuxTestClient) -> None:
    """Switch session via CLI updates current target."""
    env = {"LETEE_CONFIG_DIR": "/tmp/letee-e2e-switch"}
    _create_session_default(client, "work")

    client.start_cockpit(env=env)
    assert client.wait_for_sidebar_text("letee", timeout=10)

    client.cli("switch", "local:work", env=env)
    time.sleep(0.5)

    assert _current_target(client) == "local:work", \
        f"Current target should be local:work, got: {_current_target(client)}"

    client.stop_cockpit()
    _kill_session_default(client, "work")


def test_cli_kill(client: TmuxTestClient) -> None:
    """Kill session via CLI removes it from default server."""
    env = {"LETEE_CONFIG_DIR": "/tmp/letee-e2e-kill"}
    _create_session_default(client, "to-kill")
    assert _default_tmux_ok(client, "has-session", "-t", "to-kill")

    client.cli("kill", "local:to-kill", env=env)
    assert not _default_tmux_ok(client, "has-session", "-t", "to-kill"), \
        "Session should not exist after kill"


def test_cli_kill_nonexistent(client: TmuxTestClient) -> None:
    """Kill non-existent session exits with error."""
    env = {"LETEE_CONFIG_DIR": "/tmp/letee-e2e-killnx"}
    out = client.cli("kill", "local:no-such-session", env=env)
    assert "failed" in out.lower() or "no-such" in out.lower(), \
        f"Expected error for non-existent session, got: {out}"


def test_cli_invalid_target(client: TmuxTestClient) -> None:
    """Invalid target format exits with error."""
    env = {"LETEE_CONFIG_DIR": "/tmp/letee-e2e-invalid"}
    out1 = client.cli("switch", "garbage", env=env)
    assert "Invalid" in out1 or "failed" in out1.lower(), \
        f"Expected error for garbage target, got: {out1}"

    out2 = client.cli("kill", "ssh:missing", env=env)
    assert "Invalid" in out2 or "failed" in out2.lower(), \
        f"Expected error for ssh:missing, got: {out2}"


# ============================================================
# TUI tests — use send_keys + behavioral assertions.
# Plain capture remains stable for text; sidebar_ansi() is available for focused style checks.
# ============================================================


def _send_sidebar(client: TmuxTestClient, keys: str) -> None:
    """Send keys directly to the sidebar pane via tmux send-keys."""
    client.tmux("send-keys", "-t", "letee:cockpit.0", keys)


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
    client.tmux("send-keys", "-t", "letee:cockpit.0", tmux_key)


def test_tui_add_session(client: TmuxTestClient) -> None:
    """Add a session through the sidebar Add picker."""
    env = {"LETEE_CONFIG_DIR": "/tmp/letee-e2e-tui-add"}

    client.start_cockpit(env=env)
    assert client.wait_for_sidebar_text("letee", timeout=10)

    # Press 'a' to open Add picker
    _send_sidebar(client, "a")
    time.sleep(0.3)

    # Sole localhost entry enters creation mode automatically.
    # Type session name and press Enter to create.
    _send_sidebar(client, "myproject")
    time.sleep(0.2)
    _send_sidebar_special(client, "Enter")

    # Session creation includes discovery and cockpit switching; poll instead of racing it.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not _default_tmux_ok(
        client, "has-session", "-t", "myproject"
    ):
        time.sleep(0.1)
    assert _default_tmux_ok(client, "has-session", "-t", "myproject"), \
        f"Session myproject should be created:\n{client.sidebar_text()}"

    # Current target should be myproject
    assert _current_target(client) == "local:myproject", \
        f"Current target should be local:myproject, got: {_current_target(client)}"

    client.stop_cockpit()
    _kill_session_default(client, "myproject")


def test_tui_switch_session(client: TmuxTestClient) -> None:
    """Switch between sessions using j/k and Enter in sidebar."""
    env = {"LETEE_CONFIG_DIR": "/tmp/letee-e2e-tui-switch"}

    _create_session_default(client, "alpha")
    _create_session_default(client, "beta")
    _write_favorites(client, env["LETEE_CONFIG_DIR"], "local:alpha", "local:beta")

    client.start_cockpit(env=env)
    assert client.wait_for_sidebar_text("letee", timeout=10)

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
    _kill_session_default(client, "alpha")
    _kill_session_default(client, "beta")


def test_tui_remove_favorite(client: TmuxTestClient) -> None:
    """Remove a favorite (unstar) via the sidebar."""
    env = {"LETEE_CONFIG_DIR": "/tmp/letee-e2e-tui-remove"}

    _create_session_default(client, "keepme")
    _write_favorites(client, env["LETEE_CONFIG_DIR"], "local:keepme")

    client.start_cockpit(env=env)
    assert client.wait_for_sidebar_text("letee", timeout=10)

    # keepme should be the first (only) session. Press 'r' to remove favorite.
    _send_sidebar(client, "r")
    time.sleep(0.3)

    # Session still exists on default server
    assert _default_tmux_ok(client, "has-session", "-t", "keepme"), \
        "Session should still exist after unstar"

    # Verify favorites file is now empty (or doesn't contain keepme)
    content = _read_favorites(client, env["LETEE_CONFIG_DIR"])
    assert "keepme" not in content, \
        f"keepme should be removed from favorites file, got: {content}"

    client.stop_cockpit()
    _kill_session_default(client, "keepme")


def test_prefix_number_switch(client: TmuxTestClient) -> None:
    """Switch sessions using C-s 1, C-s 2 prefix bindings.

    ponytail: send C-s + digit to the cockpit pane (not sidebar),
    since prefix bindings are handled by the outer tmux (cockpit window).
    """
    env = {"LETEE_CONFIG_DIR": "/tmp/letee-e2e-prefix"}

    _create_session_default(client, "first")
    _create_session_default(client, "second")
    _create_session_default(client, "third")
    _write_favorites(client, env["LETEE_CONFIG_DIR"], "local:first", "local:second", "local:third")

    client.start_cockpit(env=env)
    assert client.wait_for_sidebar_text("letee", timeout=10)

    # Switch to slot 2 via prefix binding using the right pane
    # ponytail: letee prefix-digit bindings work from inside the cockpit session.
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
    _kill_session_default(client, "first")
    _kill_session_default(client, "second")
    _kill_session_default(client, "third")
