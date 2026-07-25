from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from .conftest import TmuxTestClient


def _setup(client: TmuxTestClient, config: str, status_dir: str, *sessions: str) -> None:
    for name in sessions:
        client.default_tmux("kill-session", "-t", name, check=False)
        client.default_tmux("new-session", "-d", "-s", name)
    client.write_file(f"{config}/sessions", "".join(f"local:{name}\n" for name in sessions))
    client.start_cockpit(env={
        "MTMUX_CONFIG_DIR": config,
        "MTMUX_ASCII": "1",
        "AGENT_STATUS_DIR": status_dir,
    })
    assert client.wait_for_sidebar_text("mtmux", timeout=10)


def _write_agent(
    client: TmuxTestClient,
    status_dir: str,
    session: str,
    agent_id: str,
    state: str,
    *,
    name: str = "pi",
    age: int = 0,
) -> None:
    socket_path = client.default_tmux("display-message", "-p", "-t", session, "#{socket_path}")
    pane_id = client.default_tmux("display-message", "-p", "-t", session, "#{pane_id}")
    updated = datetime.now(timezone.utc) - timedelta(seconds=age)
    payload = {
        "schema_version": "agent-status/v1alpha1",
        "agent_id": agent_id,
        "agent_name": name,
        "runtime": {"lifecycle": "running", "updated_at": updated.isoformat()},
        "task": {"state": state, "status_timestamp": updated.isoformat()},
        "x_meta": {"tmux_socket": socket_path, "tmux_pane": pane_id},
    }
    client.write_file(f"{status_dir}/{agent_id}.json", json.dumps(payload))


def _cleanup(client: TmuxTestClient, *sessions: str) -> None:
    client.stop_cockpit()
    for name in sessions:
        client.default_tmux("kill-session", "-t", name, check=False)


def _wait_for_line(client: TmuxTestClient, *parts: str, timeout: float = 4) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in client.sidebar_text().splitlines():
            if all(part in line for part in parts):
                return line
        time.sleep(0.1)
    raise AssertionError(f"Sidebar line missing {parts!r}\n{client.sidebar_text()}")


def test_agents_appear_for_tracked_sessions(client: TmuxTestClient) -> None:
    config, statuses = "/tmp/mtmux-e2e-agent", "/tmp/mtmux-e2e-agent-status"
    _setup(client, config, statuses, "dev")
    try:
        _write_agent(client, statuses, "dev", "pi-visible", "working")
        _wait_for_line(client, "pi", "working")
        assert "AGENTS" in client.sidebar_text()
    finally:
        _cleanup(client, "dev")


def test_agents_only_for_tracked_sessions(client: TmuxTestClient) -> None:
    config, statuses = "/tmp/mtmux-e2e-agent-hidden", "/tmp/mtmux-e2e-agent-hidden-status"
    _setup(client, config, statuses, "tracked")
    client.default_tmux("new-session", "-d", "-s", "hidden")
    try:
        _write_agent(client, statuses, "hidden", "hidden-agent", "working", name="secret-agent")
        time.sleep(1)
        assert "secret-agent" not in client.sidebar_text()
    finally:
        _cleanup(client, "tracked", "hidden")


def test_agent_status_icons_update(client: TmuxTestClient) -> None:
    config, statuses = "/tmp/mtmux-e2e-agent-update", "/tmp/mtmux-e2e-agent-update-status"
    _setup(client, config, statuses, "dev")
    try:
        _write_agent(client, statuses, "dev", "pi-update", "working")
        _wait_for_line(client, "pi", "working")
        _write_agent(client, statuses, "dev", "pi-update", "completed")
        line = _wait_for_line(client, "pi", "completed")
        assert "+" in line
    finally:
        _cleanup(client, "dev")


def test_agent_alert_on_completion(client: TmuxTestClient) -> None:
    config, statuses = "/tmp/mtmux-e2e-agent-alert", "/tmp/mtmux-e2e-agent-alert-status"
    _setup(client, config, statuses, "dev")
    try:
        _write_agent(client, statuses, "dev", "pi-alert", "working")
        _wait_for_line(client, "pi", "working")
        _write_agent(client, statuses, "dev", "pi-alert", "completed")
        _wait_for_line(client, "pi", "completed", "BELL")
    finally:
        _cleanup(client, "dev")


def test_agent_alert_clears_on_select(client: TmuxTestClient) -> None:
    config, statuses = "/tmp/mtmux-e2e-agent-clear", "/tmp/mtmux-e2e-agent-clear-status"
    _setup(client, config, statuses, "dev")
    try:
        _write_agent(client, statuses, "dev", "pi-clear", "working")
        _wait_for_line(client, "pi", "working")
        _write_agent(client, statuses, "dev", "pi-clear", "completed")
        _wait_for_line(client, "pi", "completed", "BELL")
        client.tmux("run-shell", "mtmux focus-sidebar agents")
        client.tmux("send-keys", "-t", "mtmux:cockpit.0", "j", "Enter")
        _wait_for_line(client, "pi", "completed")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if "BELL" not in _wait_for_line(client, "pi", "completed"):
                break
            time.sleep(0.1)
        else:
            raise AssertionError(f"Agent alert did not clear\n{client.sidebar_text()}")
    finally:
        _cleanup(client, "dev")


def test_agent_ordering_priority_vs_session(client: TmuxTestClient) -> None:
    config, statuses = "/tmp/mtmux-e2e-agent-order", "/tmp/mtmux-e2e-agent-order-status"
    _setup(client, config, statuses, "alpha", "beta")
    try:
        _write_agent(client, statuses, "alpha", "alpha-agent", "working", name="alpha-agent")
        _write_agent(client, statuses, "beta", "beta-agent", "failed", name="beta-agent")
        _wait_for_line(client, "alpha-agent", "working")
        _wait_for_line(client, "beta-agent", "failed")
        sidebar = client.sidebar_text()
        assert sidebar.index("beta-agent") < sidebar.index("alpha-agent")

        client.tmux("run-shell", "mtmux focus-sidebar agents")
        client.tmux("send-keys", "-t", "mtmux:cockpit.0", "l")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            sidebar = client.sidebar_text()
            if "SESSION" in sidebar and sidebar.index("alpha-agent") < sidebar.index("beta-agent"):
                break
            time.sleep(0.1)
        else:
            raise AssertionError(f"Session ordering not applied\n{sidebar}")
    finally:
        _cleanup(client, "alpha", "beta")


def test_stale_agents_ignored(client: TmuxTestClient) -> None:
    config, statuses = "/tmp/mtmux-e2e-agent-stale", "/tmp/mtmux-e2e-agent-stale-status"
    _setup(client, config, statuses, "dev")
    try:
        _write_agent(client, statuses, "dev", "pi-stale", "working", name="stale-agent", age=61)
        time.sleep(1)
        assert "stale-agent" not in client.sidebar_text()
    finally:
        _cleanup(client, "dev")
