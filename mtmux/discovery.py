from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from random import random
import shlex
import subprocess
import tempfile
import time

from .config import load_hosts, load_persistent_ssh
from .names import PaneTarget, Target, validate_host
from .sessions import ssh_command


PANES_FORMAT = "#{session_name}:#{window_id}:#{pane_id}:#{window_bell_flag}:#{window_flags}:#{pane_active}:#{window_active}:#{socket_path}"
PANES_COMMAND = f'tmux list-panes -a -F "{PANES_FORMAT}"'
REMOTE_SEPARATOR = "__MTMUX_AGENT_STATUS__"
_REMOTE_READER = """import glob,json,os,pathlib
root=os.environ.get('AGENT_STATUS_DIR') or str(pathlib.Path(os.environ.get('XDG_STATE_HOME', '~/.local/state')).expanduser() / 'agent-status')
for path in sorted(glob.glob(os.path.join(root,'*.json'))):
 try:
  print(json.dumps(json.load(open(path))))
 except Exception:
  pass
"""
REMOTE_COMMAND = f"{PANES_COMMAND}; rc=$?; printf '\\n{REMOTE_SEPARATOR}\\n'; python3 -c {shlex.quote(_REMOTE_READER)}; exit $rc"
AGENT_STALE_SECONDS = 60
SUPPORTED_TASK_STATES = {
    "working", "submitted", "input-required", "auth-required", "failed",
    "rejected", "completed", "canceled",
}
MAX_REMOTE_OUTPUT = 1024 * 1024
LOCAL_POLL_INTERVAL = 0.5
SUCCESS_POLL_INTERVAL = 10
MAX_FAILURE_POLL_INTERVAL = 60


@dataclass(frozen=True)
class AgentEntry:
    pane_target: PaneTarget
    agent_id: str
    agent_name: str
    task_state: str | None
    runtime_updated_at: datetime | None = None
    task_status_timestamp: datetime | None = None

    @property
    def activity_timestamp(self) -> datetime | None:
        return self.task_status_timestamp or self.runtime_updated_at


@dataclass(frozen=True)
class SourceSnapshot:
    available: bool
    sessions: tuple[Target, ...]
    bells: frozenset[Target]
    error: str | None = None
    panes: tuple[PaneTarget, ...] = ()
    agents: tuple[AgentEntry, ...] = ()
    focused_panes: frozenset[PaneTarget] = frozenset()


@dataclass(frozen=True)
class SessionSnapshot:
    local: SourceSnapshot
    remotes: dict[str, SourceSnapshot | None]

    @property
    def sessions(self) -> tuple[Target, ...]:
        return self.local.sessions + tuple(
            target
            for snapshot in self.remotes.values()
            if snapshot and snapshot.available
            for target in snapshot.sessions
        )

    @property
    def bells(self) -> frozenset[Target]:
        return self.local.bells.union(
            *(snapshot.bells for snapshot in self.remotes.values() if snapshot and snapshot.available)
        )

    @property
    def agents(self) -> tuple[AgentEntry, ...]:
        agents = self.local.agents + tuple(
            agent
            for snapshot in self.remotes.values()
            if snapshot and snapshot.available
            for agent in snapshot.agents
        )
        return tuple(sorted(agents, key=_agent_sort_key))

    @property
    def focused_panes(self) -> frozenset[PaneTarget]:
        return self.local.focused_panes.union(
            *(snapshot.focused_panes for snapshot in self.remotes.values() if snapshot and snapshot.available)
        )


UNAVAILABLE = SourceSnapshot(False, (), frozenset())


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("TMUX", None)
    return env


def _agent_sort_key(agent: AgentEntry) -> tuple[str, str, int, int, str]:
    target = agent.pane_target.target
    return (target.host or "", target.session, int(agent.pane_target.window_id[1:]), int(agent.pane_target.pane_id[1:]), agent.agent_id)


def _parse_source_snapshot(text: str, *, kind: str, host: str | None = None) -> SourceSnapshot:
    sessions: list[Target] = []
    bells: set[Target] = set()
    panes: list[PaneTarget] = []
    focused_panes: set[PaneTarget] = set()
    for line in text.splitlines():
        parts = line.split(":", 7)
        if len(parts) == 8 and parts[5] in ("0", "1") and parts[6] in ("0", "1"):
            name, window_id, pane_id, bell_flag, window_flags, pane_active, window_active, socket_path = parts
        else:
            parts = line.split(":", 5)
            if len(parts) != 6:
                continue
            name, window_id, pane_id, bell_flag, window_flags, socket_path = parts
            pane_active = window_active = "0"
        try:
            target = Target("local", name) if kind == "local" else Target("ssh", name, host)
            pane = PaneTarget(target, window_id, pane_id, socket_path)
        except SystemExit:
            continue
        if target not in sessions:
            sessions.append(target)
        panes.append(pane)
        if pane_active == "1" and window_active == "1":
            focused_panes.add(pane)
        if bell_flag in ("1", "!") or "!" in window_flags:
            bells.add(target)
    return SourceSnapshot(True, tuple(sessions), frozenset(bells), panes=tuple(panes), focused_panes=frozenset(focused_panes))


def _status_dir() -> Path:
    if value := os.environ.get("AGENT_STATUS_DIR"):
        return Path(value).expanduser()
    if value := os.environ.get("XDG_STATE_HOME"):
        return Path(value).expanduser() / "agent-status"
    return Path.home() / ".local/state/agent-status"


def _parse_timestamp(value: object) -> datetime:
    timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return timestamp.astimezone(timezone.utc)


def _parse_agent(payload: object, panes: tuple[PaneTarget, ...], now: datetime) -> AgentEntry | None:
    if not isinstance(payload, dict):
        return None
    runtime, meta = payload.get("runtime"), payload.get("x_meta")
    if payload.get("schema_version") != "agent-status/v1alpha1":
        return None
    if not isinstance(runtime, dict) or runtime.get("lifecycle") != "running" or not isinstance(meta, dict):
        return None
    try:
        updated = _parse_timestamp(runtime["updated_at"])
        if (now - updated).total_seconds() > AGENT_STALE_SECONDS:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    socket_path, pane_id = meta.get("tmux_socket"), meta.get("tmux_pane")
    pane = next((item for item in panes if (item.socket_path, item.pane_id) == (socket_path, pane_id)), None)
    agent_id, agent_name = payload.get("agent_id"), payload.get("agent_name")
    if pane is None or not isinstance(agent_id, str) or not agent_id or not isinstance(agent_name, str) or not agent_name:
        return None
    task = payload.get("task")
    state = task.get("state") if isinstance(task, dict) else None
    if state is not None and state not in SUPPORTED_TASK_STATES:
        state = "unknown"
    try:
        task_timestamp = _parse_timestamp(task["status_timestamp"]) if isinstance(task, dict) and "status_timestamp" in task else None
    except (TypeError, ValueError):
        task_timestamp = None
    return AgentEntry(pane, agent_id, agent_name, state, updated, task_timestamp)


def _read_agents(panes: tuple[PaneTarget, ...], records: Iterable[object], now: datetime | None = None) -> tuple[AgentEntry, ...]:
    clock = now or datetime.now(timezone.utc)
    agents = [entry for payload in records if (entry := _parse_agent(payload, panes, clock))]
    return tuple(sorted(agents, key=_agent_sort_key))


def _read_local_agents(panes: tuple[PaneTarget, ...]) -> tuple[AgentEntry, ...]:
    records: list[object] = []
    try:
        paths = sorted(_status_dir().glob("*.json"))
    except OSError:
        return ()
    for path in paths:
        try:
            records.append(json.loads(path.read_text()))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
    return _read_agents(panes, records)


def _source_result(
    returncode: int,
    stdout: str | None,
    stderr: str | None,
    *,
    kind: str,
    host: str | None = None,
) -> SourceSnapshot:
    if stdout is None or stderr is None:
        return SourceSnapshot(False, (), frozenset(), "output exceeded 1 MiB")
    pane_text, separator, agent_text = stdout.partition(f"\n{REMOTE_SEPARATOR}\n")
    first_error = stderr.splitlines()[0] if stderr.splitlines() else ""
    missing_server = returncode == 1 and (
        first_error.startswith("no server running on ")
        or (first_error.startswith("error connecting to ") and first_error.endswith("(No such file or directory)"))
    )
    if returncode == 0 or missing_server:
        snapshot = _parse_source_snapshot(pane_text if returncode == 0 else "", kind=kind, host=host)
        if not separator:
            return snapshot
        records = []
        for line in agent_text.splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return SourceSnapshot(snapshot.available, snapshot.sessions, snapshot.bells, snapshot.error, snapshot.panes, _read_agents(snapshot.panes, records), snapshot.focused_panes)
    return SourceSnapshot(False, (), frozenset(), stderr.strip() or f"remote command exited {returncode}")


def local_snapshot() -> SourceSnapshot:
    try:
        proc = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", PANES_FORMAT],
            text=True,
            capture_output=True,
            env=_clean_env(),
        )
    except OSError as error:
        return SourceSnapshot(False, (), frozenset(), error.strerror or str(error))
    snapshot = _source_result(proc.returncode, proc.stdout, proc.stderr, kind="local")
    if not snapshot.available:
        return snapshot
    return SourceSnapshot(snapshot.available, snapshot.sessions, snapshot.bells, snapshot.error, snapshot.panes, _read_local_agents(snapshot.panes), snapshot.focused_panes)


def _ssh_command(host: str, persistent_ssh: bool) -> tuple[str, ...]:
    return ssh_command(
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        validate_host(host), REMOTE_COMMAND,
        persistent_ssh=persistent_ssh,
    )


def _read_output(output: object, fallback: str | bytes | None = None) -> str | None:
    output.seek(0)
    data = output.read(MAX_REMOTE_OUTPUT + 1)
    if not data and fallback is not None:
        data = fallback.encode() if isinstance(fallback, str) else fallback
    if len(data) > MAX_REMOTE_OUTPUT:
        return None
    return data.decode(errors="replace")


def remote_snapshot(host: str) -> SourceSnapshot:
    host = validate_host(host)
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            proc = subprocess.run(_ssh_command(host, load_persistent_ssh()), stdout=output, stderr=errors, timeout=10)
        except subprocess.TimeoutExpired:
            return SourceSnapshot(False, (), frozenset(), "timed out")
        except OSError as error:
            return SourceSnapshot(False, (), frozenset(), error.strerror or str(error))
        text = _read_output(output, getattr(proc, "stdout", None))
        error = _read_output(errors, getattr(proc, "stderr", None))
    return _source_result(proc.returncode, text, error, kind="ssh", host=host)


def discover() -> SessionSnapshot:
    return SessionSnapshot(local_snapshot(), {host: remote_snapshot(host) for host in load_hosts()})


@dataclass
class _Request:
    process: object
    started: float
    output: object
    errors: object


def _stop_process(process: object) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    process.communicate()


class DiscoveryPoller:
    def __init__(
        self,
        hosts: Iterable[str],
        *,
        popen: Callable[..., object] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        random: Callable[[], float] = random,
        local: Callable[[], SourceSnapshot] | None = None,
    ) -> None:
        self.hosts = tuple(validate_host(host) for host in hosts)
        self._local_snapshot = local or local_snapshot
        self._persistent_ssh = load_persistent_ssh()
        self.local = self._local_snapshot()
        self.remotes: dict[str, SourceSnapshot | None] = dict.fromkeys(self.hosts)
        self._popen = popen
        self._clock = clock
        self._random = random
        self._next_local_poll = self._clock() + LOCAL_POLL_INTERVAL
        self._active: dict[str, _Request] = {}
        self._active_remote_host: str | None = None
        self._next = dict.fromkeys(self.hosts, 0.0)
        self._failures = dict.fromkeys(self.hosts, 0)

    @property
    def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(self.local, self.remotes.copy())

    def _schedule(self, host: str, now: float, available: bool) -> None:
        if available:
            self._failures[host] = 0
            interval = LOCAL_POLL_INTERVAL if host == self._active_remote_host else SUCCESS_POLL_INTERVAL
            self._next[host] = now + interval
            return
        self._failures[host] += 1
        delay = min(2 ** self._failures[host], MAX_FAILURE_POLL_INTERVAL)
        self._next[host] = now + delay * (0.5 + self._random() / 2)

    def _finish_request(self, host: str, request: _Request, now: float, returncode: int) -> bool:
        stdout, _ = request.process.communicate()
        text = _read_output(request.output, stdout)
        error = _read_output(request.errors)
        request.output.close()
        request.errors.close()
        snapshot = _source_result(returncode, text, error, kind="ssh", host=host)
        changed = snapshot != self.remotes[host]
        self.remotes[host] = snapshot
        del self._active[host]
        self._schedule(host, now, snapshot.available)
        return changed

    def _start_request(self, host: str, now: float) -> bool:
        output = tempfile.TemporaryFile()
        errors = tempfile.TemporaryFile()
        try:
            process = self._popen(_ssh_command(host, self._persistent_ssh), stdout=output, stderr=errors)
        except OSError as error:
            output.close()
            errors.close()
            snapshot = SourceSnapshot(False, (), frozenset(), error.strerror or str(error))
            changed = snapshot != self.remotes[host]
            self.remotes[host] = snapshot
            self._schedule(host, now, False)
            return changed
        self._active[host] = _Request(process, now, output, errors)
        return False

    def tick(self, active_remote_host: str | None = None) -> bool:
        now = self._clock()
        active_remote_host = active_remote_host if active_remote_host in self.remotes else None
        if active_remote_host != self._active_remote_host:
            previous = self._active_remote_host
            self._active_remote_host = active_remote_host
            if previous and self.remotes[previous] and self.remotes[previous].available:
                self._next[previous] = now + SUCCESS_POLL_INTERVAL
            if active_remote_host and active_remote_host not in self._active:
                self._next[active_remote_host] = now
        changed = False
        if now >= self._next_local_poll:
            snapshot = self._local_snapshot()
            changed = snapshot != self.local
            self.local = snapshot
            self._next_local_poll = now + LOCAL_POLL_INTERVAL
        for host in self.hosts:
            if host not in self._active and now >= self._next[host]:
                changed |= self._start_request(host, now)

        for host, request in list(self._active.items()):
            returncode = request.process.poll()
            if returncode is None:
                if now - request.started >= 10:
                    _stop_process(request.process)
                    request.output.close()
                    request.errors.close()
                    snapshot = SourceSnapshot(False, (), frozenset(), "timed out")
                    changed |= snapshot != self.remotes[host]
                    self.remotes[host] = snapshot
                    del self._active[host]
                    self._schedule(host, now, False)
                continue
            changed |= self._finish_request(host, request, now, returncode)
        return changed

    def refresh(self) -> bool:
        snapshot = self._local_snapshot()
        changed = snapshot != self.local
        self.local = snapshot
        now = self._clock()
        self._next_local_poll = now + LOCAL_POLL_INTERVAL
        for host in self.hosts:
            if host not in self._active:
                self._next[host] = now
        return changed

    def discard(self, target: Target) -> None:
        if target.kind == "local":
            source = self.local
            self.local = SourceSnapshot(
                source.available,
                tuple(item for item in source.sessions if item != target),
                frozenset(item for item in source.bells if item != target),
                source.error,
                tuple(item for item in source.panes if item.target != target),
                tuple(item for item in source.agents if item.pane_target.target != target),
                frozenset(item for item in source.focused_panes if item.target != target),
            )
            return
        if target.host not in self.remotes:
            return
        if request := self._active.pop(target.host, None):
            _stop_process(request.process)
            request.output.close()
            request.errors.close()
        source = self.remotes[target.host]
        if source:
            self.remotes[target.host] = SourceSnapshot(
                source.available,
                tuple(item for item in source.sessions if item != target),
                frozenset(item for item in source.bells if item != target),
                source.error,
                tuple(item for item in source.panes if item.target != target),
                tuple(item for item in source.agents if item.pane_target.target != target),
                frozenset(item for item in source.focused_panes if item.target != target),
            )

    def close(self) -> None:
        for request in self._active.values():
            _stop_process(request.process)
            request.output.close()
            request.errors.close()
        self._active.clear()
