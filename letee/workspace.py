"""Tracked work and agent activity, independent of terminal presentation.

Display names are never part of finder identity.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
import json
import re
import time

from .discovery import AgentEntry, SessionSnapshot
from .names import PaneTarget, Target


Identity = tuple[str, ...]
AgentKey = tuple[PaneTarget, str]
ATTENTION_STATES = {"input-required", "auth-required", "failed", "rejected"}
_TRANSITION_ALERT_STATES = ATTENTION_STATES | {"idle", "completed", "canceled"}


@dataclass(frozen=True)
class Agent:
    pane_target: PaneTarget
    agent_id: str
    name: str
    state: str
    pane_index: int
    runtime_updated_at: datetime | None = None
    task_status_timestamp: datetime | None = None
    alert: bool = False

    @property
    def activity_timestamp(self) -> datetime | None:
        return self.task_status_timestamp or self.runtime_updated_at


@dataclass(frozen=True)
class Window:
    window_id: str
    index: int
    name: str
    agents: tuple[Agent, ...] = ()


@dataclass(frozen=True)
class Session:
    target: Target
    session_id: str
    socket_path: str
    windows: tuple[Window, ...] = ()
    available: bool = True
    bell: bool = False
    pending: bool = False

    @property
    def key(self) -> Identity:
        return (self.target.kind, self.target.host or "", self.socket_path, self.session_id)


@dataclass(frozen=True)
class Workspace:
    # Session order belongs to the sidebar. Windows arrive in tmux index order.
    sessions: tuple[Session, ...] = ()
    current: Identity | None = None
    loading: bool = False


def visible_agents(
    snapshot: SessionSnapshot,
    tracked: Iterable[Target],
    hidden: set[AgentKey] | None = None,
) -> tuple[AgentEntry, ...]:
    tracked_set = set(tracked)
    return tuple(
        agent for agent in snapshot.agents
        if agent.pane_target.target in tracked_set
        and (agent.pane_target, agent.agent_id) not in (hidden or ())
    )


def target_status(target: Target, snapshot: SessionSnapshot) -> str | None:
    if target in snapshot.sessions:
        return None
    if target.kind == "local":
        return "missing" if snapshot.local.available else "unavailable"
    if target.host not in snapshot.remotes:
        return "unavailable"
    source = snapshot.remotes[target.host]
    if source is None:
        return "connecting…"
    if not source.available:
        return "reconnecting…"
    return "missing"


def _agent_state(agent: AgentEntry) -> str:
    return agent.task_state or "idle"


def build_workspace(
    snapshot: SessionSnapshot,
    tracked: Iterable[Target],
    current_target: Target | None = None,
    current_agent: str | None = None,
    *,
    hidden: set[AgentKey] | None = None,
    alerts: set[AgentKey] | None = None,
    bells: set[Target] | frozenset[Target] | None = None,
    previous: Workspace | None = None,
) -> Workspace:
    """Assemble tracked destinations from the source data already available."""
    tracked = tuple(tracked)
    agents = visible_agents(snapshot, tracked, hidden)
    previous_sessions = {session.target: session for session in previous.sessions} if previous else {}
    sessions = []
    current = None
    rang_bells = snapshot.bells.union(bells or ())
    for target in tracked:
        source = snapshot.local if target.kind == "local" else snapshot.remotes.get(target.host)
        prior = previous_sessions.get(target)
        panes = tuple(pane for pane in source.panes if pane.target == target) if source and source.available else ()
        discovered = next((pane for pane in panes if pane.session_id), None)
        available = bool(source and source.available and target in source.sessions and discovered)
        if available:
            windows = {}
            for pane in panes:
                windows.setdefault(pane.window_id, pane)
            built_windows = []
            for window_id, pane in sorted(windows.items(), key=lambda item: item[1].window_index):
                window_agents = tuple(
                    Agent(entry.pane_target, entry.agent_id, entry.agent_name, _agent_state(entry),
                          entry.pane_target.pane_index, entry.runtime_updated_at,
                          entry.task_status_timestamp, (entry.pane_target, entry.agent_id) in (alerts or ()))
                    for entry in sorted(
                        (entry for entry in agents if entry.pane_target.target == target and entry.pane_target.window_id == window_id),
                        key=lambda entry: entry.pane_target.pane_index,
                    )
                )
                built_windows.append(Window(window_id, pane.window_index, pane.window_name, window_agents))
            session = Session(target, discovered.session_id, discovered.socket_path, tuple(built_windows),
                              bell=target in rang_bells)
        else:
            # A confirmed absence removes children. A disconnected source retains them.
            windows = prior.windows if prior and (source is None or not source.available) else ()
            session = Session(target, prior.session_id if prior else "missing:" + target.session,
                              prior.socket_path if prior else "", windows, False, target in rang_bells,
                              target.kind == "ssh" and target.host in snapshot.remotes and source is None)
        sessions.append(session)
        if target != current_target:
            continue
        current = session.key
        if not available:
            if session.windows and previous and previous.current and previous.current[:4] == session.key:
                current = previous.current
            continue
        focused = next((pane for pane in panes if pane in snapshot.focused_panes), None)
        active = focused or next((pane for pane in panes if pane.window_active), None)
        if active:
            current += (active.window_id,)
        if focused and current_agent:
            match = next((agent for window in session.windows for agent in window.agents
                          if agent.pane_target == focused and agent.agent_id == current_agent), None)
            if match:
                current += (focused.pane_id, match.agent_id)
    return Workspace(tuple(sessions), current, any(source is None for source in snapshot.remotes.values()))


def workspace_to_json(workspace: Workspace) -> str:
    """Encode the canonical workspace without repeating parent metadata."""
    return json.dumps({
        "version": 1,
        "loading": workspace.loading,
        "current": workspace.current,
        "sessions": [{
            "kind": session.target.kind, "host": session.target.host,
            "session": session.target.session, "session_id": session.session_id,
            "socket_path": session.socket_path, "available": session.available, "bell": session.bell,
            "pending": session.pending,
            "windows": [{
                "window_id": window.window_id, "index": window.index, "name": window.name,
                "agents": [{
                    "agent_id": agent.agent_id, "name": agent.name, "state": agent.state,
                    "pane_id": agent.pane_target.pane_id, "pane_index": agent.pane_index,
                    "runtime_updated_at": agent.runtime_updated_at.isoformat() if agent.runtime_updated_at else None,
                    "task_status_timestamp": agent.task_status_timestamp.isoformat() if agent.task_status_timestamp else None,
                    "alert": agent.alert,
                } for agent in window.agents],
            } for window in session.windows],
        } for session in workspace.sessions],
    }, ensure_ascii=False, separators=(",", ":"))


def _json_field(obj: object, name: str, kind: type):
    if not isinstance(obj, dict) or name not in obj or type(obj[name]) is not kind:
        raise ValueError(f"Invalid workspace field: {name}")
    return obj[name]


def _json_index(obj: object, name: str) -> int:
    value = _json_field(obj, name, int)
    if value < 0:
        raise ValueError(f"Invalid workspace index: {name}")
    return value


def _json_timestamp(obj: dict, name: str) -> datetime | None:
    if name not in obj:
        raise ValueError(f"Missing workspace timestamp: {name}")
    if obj[name] is None:
        return None
    return datetime.fromisoformat(_json_field(obj, name, str))


def workspace_from_json(text: str) -> Workspace:
    """Decode and validate external workspace data, raising ValueError on bad input."""
    try:
        data = json.loads(text)
        if _json_field(data, "version", int) != 1:
            raise ValueError("Unsupported workspace version")
        loading = _json_field(data, "loading", bool)
        sessions = []
        identities = set()
        for item in _json_field(data, "sessions", list):
            kind = _json_field(item, "kind", str)
            host = item["host"]
            if host is not None and type(host) is not str:
                raise ValueError("Invalid workspace host")
            target = Target(kind, _json_field(item, "session", str), host)
            session_id = _json_field(item, "session_id", str)
            socket = _json_field(item, "socket_path", str)
            available = _json_field(item, "available", bool)
            bell = _json_field(item, "bell", bool)
            pending = _json_field(item, "pending", bool)
            if pending and available:
                raise ValueError("Pending session cannot be available")
            window_items = _json_field(item, "windows", list)
            if session_id == "missing:" + target.session:
                if available or socket or window_items:
                    raise ValueError("Invalid missing session")
            elif not re.fullmatch(r"\$[0-9]+", session_id) or not socket:
                raise ValueError("Invalid session identity")
            key = (target.kind, target.host or "", socket, session_id)
            if key in identities:
                raise ValueError("Duplicate session identity")
            identities.add(key)
            windows = []
            for window_item in window_items:
                window_id = _json_field(window_item, "window_id", str)
                if not re.fullmatch(r"@[0-9]+", window_id):
                    raise ValueError("Invalid window identity")
                window_key = key + (window_id,)
                if window_key in identities:
                    raise ValueError("Duplicate window identity")
                identities.add(window_key)
                index = _json_index(window_item, "index")
                name = _json_field(window_item, "name", str)
                agents = []
                for agent_item in _json_field(window_item, "agents", list):
                    pane_id = _json_field(agent_item, "pane_id", str)
                    pane_index = _json_index(agent_item, "pane_index")
                    agent_id = _json_field(agent_item, "agent_id", str)
                    if not agent_id:
                        raise ValueError("Empty agent identity")
                    pane = PaneTarget(target, window_id, pane_id, socket, name,
                                      session_id=session_id, window_index=index, pane_index=pane_index)
                    agent_key = window_key + (pane_id, agent_id)
                    if agent_key in identities:
                        raise ValueError("Duplicate agent identity")
                    identities.add(agent_key)
                    agents.append(Agent(
                        pane, agent_id, _json_field(agent_item, "name", str),
                        _json_field(agent_item, "state", str), pane_index,
                        _json_timestamp(agent_item, "runtime_updated_at"),
                        _json_timestamp(agent_item, "task_status_timestamp"),
                        _json_field(agent_item, "alert", bool),
                    ))
                windows.append(Window(window_id, index, name, tuple(agents)))
            sessions.append(Session(target, session_id, socket, tuple(windows), available, bell, pending))
        current = data["current"]
        if current is not None:
            if type(current) is not list or any(type(part) is not str for part in current):
                raise ValueError("Invalid current identity")
            current = tuple(current)
            if current not in identities:
                raise ValueError("Unknown current identity")
        return Workspace(tuple(sessions), current, loading)
    except (SystemExit, KeyError, TypeError) as error:
        raise ValueError("Invalid workspace snapshot") from error


@dataclass
class AgentActivity:
    states: dict[AgentKey, str] = field(default_factory=dict)
    transition_at: dict[AgentKey, float] = field(default_factory=dict)
    alerts: set[AgentKey] = field(default_factory=set)

    def update(
        self,
        snapshot: SessionSnapshot,
        tracked: Iterable[Target],
        current_target: Target | None,
        now: float | None = None,
        hidden: set[AgentKey] | None = None,
    ) -> bool:
        agents = {
            (agent.pane_target, agent.agent_id): _agent_state(agent)
            for agent in visible_agents(snapshot, tracked, hidden)
        }
        for key, status in agents.items():
            if key in self.states and self.states[key] != status:
                if now is None:
                    now = time.monotonic()
                self.transition_at[key] = now
        self.transition_at = {key: ts for key, ts in self.transition_at.items() if key in agents}
        active = {
            key for key in agents
            if key[0] in snapshot.focused_panes and key[0].target == current_target
        }
        new_alerts = {
            key for key, status in agents.items()
            if self.states.get(key) == "working" and status in _TRANSITION_ALERT_STATES and key not in active
        }
        self.alerts.intersection_update(agents)
        self.alerts.difference_update(active)
        self.alerts.update(new_alerts)
        self.states = agents
        return bool(new_alerts)
