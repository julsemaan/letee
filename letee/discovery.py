from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import time

from . import diagnostics
from .config import load_hosts, load_persistent_ssh, load_timing
from .names import INNER_SERVER_SOCKET, SESSION_ID_RE, PaneTarget, Target, validate_host
from .sessions import ssh_command


# After the legacy colon header: TAB session_id:window_index:pane_index TAB
# window_name. The final name is unescaped and may contain tabs and colons.
PANES_FORMAT = "#{session_name}:#{window_id}:#{pane_id}:#{window_bell_flag}:#{window_flags}:#{pane_active}:#{window_active}:#{socket_path}\t#{session_id}:#{window_index}:#{pane_index}\t#{window_name}"
PANES_COMMAND = f'tmux -L {shlex.quote(INNER_SERVER_SOCKET)} list-panes -a -F "{PANES_FORMAT}"'
REMOTE_SEPARATOR = "__LETEE_AGENT_STATUS__"
# Remote record read/parse errors are aggregated in allowlisted counters.
# Exception text, filenames, and record contents never enter the summary.
_REMOTE_READER = """import glob,json,os,pathlib
root=os.environ.get('AGENT_STATUS_DIR') or str(pathlib.Path(os.environ.get('XDG_STATE_HOME', '~/.local/state')).expanduser() / 'agent-status')
files=loaded=failed=0
for path in sorted(glob.glob(os.path.join(root,'*.json'))):
 files+=1
 try:
  with open(path) as stream:
   record=json.load(stream)
  print(json.dumps(record))
  loaded+=1
 except Exception:
  failed+=1
print(json.dumps({'_letee_discovery':{'file_count':min(files,1048576),'loaded_count':min(loaded,1048576),'failed_count':min(failed,1048576)}}))
"""
REMOTE_COMMAND = (
    f"{PANES_COMMAND}; rc=$?; printf '\\n{REMOTE_SEPARATOR}\\n'; "
    f"python3 -c {shlex.quote(_REMOTE_READER)}; letee_reader_rc=$?; "
    "printf '{\"_letee_reader_exit\": %s}\\n' \"$letee_reader_rc\"; exit $rc"
)
# Diagnostic trailers have a separate fixed budget, preserving the payload limit.
_DIAGNOSTIC_TRAILER_LIMIT = 512
_DIAGNOSTIC_TRAILERS = re.compile(
    rb'(?:\{"_letee_discovery": \{"file_count": [0-9]{1,7}, "loaded_count": [0-9]{1,7}, "failed_count": [0-9]{1,7}\}\}\n'
    rb'|\{"_letee_reader_exit": [0-9]{1,3}\}\n){1,2}$'
)
AGENT_STALE_SECONDS = 60
SUPPORTED_TASK_STATES = {
    "working", "submitted", "input-required", "auth-required", "failed",
    "rejected", "completed", "canceled",
}
MAX_REMOTE_OUTPUT = 1024 * 1024
LOCAL_POLL_INTERVAL = 0.5
SUCCESS_POLL_INTERVAL = 10
DIAGNOSTIC_HEARTBEAT_INTERVAL = 30


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
    sampled_at: float = field(default_factory=time.monotonic, compare=False)
    request_id: str | None = field(default=None, compare=False)
    _pane_metadata: tuple = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Pane identity remains stable for alert keys; discovery still notices UI changes.
        object.__setattr__(self, "_pane_metadata", tuple(
            (pane.window_name, pane.session_id, pane.window_index, pane.pane_index,
             pane.window_active, pane.pane_active)
            for pane in self.panes
        ))


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
    def panes(self) -> tuple[PaneTarget, ...]:
        return tuple(
            pane
            for source in (self.local, *self.remotes.values())
            if source and source.available
            for pane in source.panes
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


def _parse_source_snapshot(text: str, *, kind: str, host: str | None = None, request_id: str | None = None) -> SourceSnapshot:
    sessions: list[Target] = []
    bells: set[Target] = set()
    panes: list[PaneTarget] = []
    focused_panes: set[PaneTarget] = set()
    rejected = 0
    for line in text.splitlines():
        parts = line.split(":", 7)
        if len(parts) == 8 and parts[5] in ("0", "1") and parts[6] in ("0", "1"):
            name, window_id, pane_id, bell_flag, window_flags, pane_active, window_active, socket_path = parts
        else:
            parts = line.split(":", 5)
            if len(parts) != 6:
                rejected += 1
                continue
            name, window_id, pane_id, bell_flag, window_flags, socket_path = parts
            pane_active = window_active = "0"
        socket_path, _, window_name = socket_path.partition("\t")
        session_id = ""
        window_index = pane_index = 0
        try:
            metadata, separator, final_name = window_name.partition("\t")
            if separator and metadata.startswith("$"):
                session_id, window_index_text, pane_index_text = metadata.split(":")
                if not SESSION_ID_RE.fullmatch(session_id):
                    continue
                window_index, pane_index = int(window_index_text), int(pane_index_text)
                window_name = final_name
            target = Target("local", name) if kind == "local" else Target("ssh", name, host)
            pane = PaneTarget(target, window_id, pane_id, socket_path, window_name,
                              session_id, window_index, pane_index,
                              window_active == "1", pane_active == "1")
        except (SystemExit, ValueError):
            # Invalid pane identifiers are expected input; aggregate rejections.
            rejected += 1
            continue
        if target not in sessions:
            sessions.append(target)
        panes.append(pane)
        if pane_active == "1" and window_active == "1":
            focused_panes.add(pane)
        if bell_flag in ("1", "!") or "!" in window_flags:
            bells.add(target)
    diagnostics.log(
        "discovery.panes_parsed", kind=kind, host=host, request_id=request_id, pane_count=len(panes),
        session_count=len(sessions), rejected_count=rejected, bell_count=len(bells),
        focused_pane_count=len(focused_panes),
    )
    return SourceSnapshot(True, tuple(sessions), frozenset(bells), panes=tuple(panes), focused_panes=frozenset(focused_panes), request_id=request_id)


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


def _parse_agent(
    payload: object, panes: tuple[PaneTarget, ...], now: datetime, counts: dict[str, int] | None = None,
) -> AgentEntry | None:
    # Expected validation failures are aggregated by reason, never by payload.
    counts = counts if counts is not None else {}

    def reject(reason: str) -> None:
        counts[reason] = counts.get(reason, 0) + 1

    if not isinstance(payload, dict):
        return reject("invalid_payload")
    runtime, meta = payload.get("runtime"), payload.get("x_meta")
    if payload.get("schema_version") != "agent-status/v1alpha1":
        return reject("unsupported_schema")
    if not isinstance(runtime, dict) or runtime.get("lifecycle") != "running" or not isinstance(meta, dict):
        return reject("invalid_runtime")
    try:
        updated = _parse_timestamp(runtime["updated_at"])
        if (now - updated).total_seconds() > AGENT_STALE_SECONDS:
            return reject("stale")
    except (KeyError, TypeError, ValueError):
        return reject("invalid_runtime_timestamp")
    socket_path, pane_id = meta.get("tmux_socket"), meta.get("tmux_pane")
    pane = next((item for item in panes if (item.socket_path, item.pane_id) == (socket_path, pane_id)), None)
    agent_id, agent_name = payload.get("agent_id"), payload.get("agent_name")
    if pane is None:
        return reject("unmatched_pane")
    if not isinstance(agent_id, str) or not agent_id or not isinstance(agent_name, str) or not agent_name:
        return reject("invalid_identity")
    task = payload.get("task")
    state = task.get("state") if isinstance(task, dict) else None
    if state is not None and (not isinstance(state, str) or state not in SUPPORTED_TASK_STATES):
        state = "unknown"
        counts["unknown_task_state"] = counts.get("unknown_task_state", 0) + 1
    try:
        task_timestamp = _parse_timestamp(task["status_timestamp"]) if isinstance(task, dict) and "status_timestamp" in task else None
    except (TypeError, ValueError):
        counts["invalid_task_timestamp"] = counts.get("invalid_task_timestamp", 0) + 1
        task_timestamp = None
    return AgentEntry(pane, agent_id, agent_name, state, updated, task_timestamp)


def _read_agents(panes: tuple[PaneTarget, ...], records: Iterable[object], now: datetime | None = None, *, request_id: str | None = None) -> tuple[AgentEntry, ...]:
    clock = now or datetime.now(timezone.utc)
    agents = []
    record_count = 0
    counts: dict[str, int] = {}
    for payload in records:
        record_count += 1
        if entry := _parse_agent(payload, panes, clock, counts):
            agents.append(entry)
    diagnostics.log("discovery.agents_parsed", request_id=request_id, record_count=record_count, accepted_count=len(agents), rejected_count=record_count - len(agents), reasons=counts)
    return tuple(sorted(agents, key=_agent_sort_key))


def _read_local_agents(panes: tuple[PaneTarget, ...], *, request_id: str | None = None) -> tuple[AgentEntry, ...]:
    records: list[object] = []
    try:
        paths = sorted(_status_dir().glob("*.json"))
    except OSError as error:
        diagnostics.error(handling="fallback", operation="discovery.local_agents", request_id=request_id, error_type=type(error).__name__, reason="directory_read")
        return ()
    failed = 0
    for path in paths:
        try:
            records.append(json.loads(path.read_text()))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            diagnostics.error(error, handling="skip", operation="discovery.local_agents", request_id=request_id, reason="unreadable_record")
            failed += 1
            continue
    diagnostics.log("discovery.local_agents_read", request_id=request_id, file_count=len(paths), loaded_count=len(records), failed_count=failed)
    return _read_agents(panes, records, request_id=request_id)


def _source_result(
    returncode: int,
    stdout: str | None,
    stderr: str | None,
    *,
    kind: str,
    host: str | None = None,
    request_id: str | None = None,
) -> SourceSnapshot:
    if stdout is None or stderr is None:
        diagnostics.log("discovery.source_result", kind=kind, host=host, request_id=request_id, outcome="output_limit", returncode=returncode)
        diagnostics.error(handling="fallback", operation="discovery.source_result", reason="output_limit", request_id=request_id)
        return SourceSnapshot(False, (), frozenset(), "output exceeded 1 MiB", request_id=request_id)
    pane_text, separator, agent_text = stdout.partition(f"\n{REMOTE_SEPARATOR}\n")
    first_error = stderr.splitlines()[0] if stderr.splitlines() else ""
    missing_server = returncode == 1 and (
        first_error.startswith("no server running on ")
        or (first_error.startswith("error connecting to ") and first_error.endswith("(No such file or directory)"))
    )
    if returncode == 0 or missing_server:
        snapshot = _parse_source_snapshot(pane_text if returncode == 0 else "", kind=kind, host=host, request_id=request_id)
        records = []
        malformed = 0
        remote_counts = {}
        reader_metadata_present = False
        reader_returncode = None
        for line in agent_text.splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                # Expected malformed remote records are counted, not logged raw.
                malformed += 1
                continue
            if isinstance(payload, dict) and set(payload) == {"_letee_reader_exit"} and type(payload["_letee_reader_exit"]) is int and 0 <= payload["_letee_reader_exit"] <= 255:
                reader_returncode = payload["_letee_reader_exit"]
            elif isinstance(payload, dict) and set(payload) == {"_letee_discovery"} and isinstance(payload["_letee_discovery"], dict):
                reader_metadata_present = True
                remote_counts = {
                    key: min(value, MAX_REMOTE_OUTPUT)
                    for key in ("file_count", "loaded_count", "failed_count")
                    if type(value := payload["_letee_discovery"].get(key)) is int and value >= 0
                }
            else:
                records.append(payload)
        agents = _read_agents(snapshot.panes, records, request_id=request_id) if separator else ()
        diagnostics.log(
            "discovery.source_result", kind=kind, host=host, request_id=request_id,
            outcome="no_server" if missing_server else "completed", returncode=returncode,
            pane_count=len(snapshot.panes), session_count=len(snapshot.sessions),
            agent_count=len(agents), malformed_agent_count=malformed,
            agent_separator=bool(separator), remote_reader_counts=remote_counts,
            stderr_present=bool(stderr), remote_reader_returncode=reader_returncode,
        )
        if reader_returncode:
            diagnostics.error(handling="fallback", operation="discovery.source_result",
                              reason="remote_python_missing" if reader_returncode == 127 else "remote_reader_failed",
                              request_id=request_id, returncode=reader_returncode)
        elif kind == "ssh" and separator and not reader_metadata_present and stderr:
            diagnostics.error(handling="fallback", operation="discovery.source_result", reason="remote_reader_missing", request_id=request_id)
        if malformed or remote_counts.get("failed_count"):
            diagnostics.error(handling="skip", operation="discovery.source_result", reason="invalid_agent_records", request_id=request_id, malformed_count=malformed)
        return replace(snapshot, agents=agents, request_id=request_id)
    diagnostics.log("discovery.source_result", kind=kind, host=host, request_id=request_id, outcome="unavailable", returncode=returncode, stderr_present=bool(stderr))
    diagnostics.error(handling="fallback", operation="discovery.source_result", reason="command_failed", request_id=request_id, returncode=returncode)
    return SourceSnapshot(False, (), frozenset(), stderr.strip() or f"remote command exited {returncode}", request_id=request_id)


def local_snapshot() -> SourceSnapshot:
    logger = diagnostics.get_diagnostics()
    request_id = logger.new_id("discovery")
    with logger.context(request_id=request_id), diagnostics.operation("discovery.local_attempt", request_id=request_id):
        return _local_snapshot(request_id)


def _local_snapshot(request_id: str | None) -> SourceSnapshot:
    sampled_at = time.monotonic()
    timeout = load_timing().local_command_timeout
    with diagnostics.operation("discovery.local_snapshot", request_id=request_id, timeout=timeout) as operation:
        try:
            proc = subprocess.run(
                ["tmux", "-L", INNER_SERVER_SOCKET, "list-panes", "-a", "-F", PANES_FORMAT],
                text=True,
                capture_output=True,
                timeout=timeout,
                env=_clean_env(),
            )
        except subprocess.TimeoutExpired:
            operation.outcome = "timed_out"
            diagnostics.error(handling="fallback", operation="discovery.local_snapshot", request_id=request_id, reason="timeout")
            return SourceSnapshot(False, (), frozenset(), "timed out", request_id=request_id)
        except OSError as error:
            operation.outcome = "spawn_failed"
            diagnostics.error(handling="fallback", operation="discovery.local_snapshot", request_id=request_id, error_type=type(error).__name__)
            return SourceSnapshot(False, (), frozenset(), error.strerror or str(error), request_id=request_id)
        snapshot = _source_result(proc.returncode, proc.stdout, proc.stderr, kind="local", request_id=request_id)
        operation.fields.update(returncode=proc.returncode, available=snapshot.available, pane_count=len(snapshot.panes))
        if not snapshot.available:
            operation.outcome = "unavailable"
            return snapshot
        return replace(snapshot, agents=_read_local_agents(snapshot.panes, request_id=request_id), sampled_at=sampled_at)


def _ssh_command(host: str, persistent_ssh: bool) -> tuple[str, ...]:
    return ssh_command(
        "-n", "-o", "BatchMode=yes",
        validate_host(host), REMOTE_COMMAND,
        persistent_ssh=persistent_ssh,
    )


def _read_output(
    output: object, fallback: str | bytes | None = None, *, request_id: str | None = None, stream: str | None = None,
) -> str | None:
    output.seek(0)
    allowance = _DIAGNOSTIC_TRAILER_LIMIT if stream == "stdout" else 0
    data = output.read(MAX_REMOTE_OUTPUT + allowance + 1)
    used_fallback = not data and fallback is not None
    if used_fallback:
        data = fallback.encode() if isinstance(fallback, str) else fallback
    trailer = _DIAGNOSTIC_TRAILERS.search(data[-allowance:]) if allowance else None
    trailer_size = len(trailer[0]) if trailer else 0
    payload_size = len(data) - trailer_size
    diagnostics.log("discovery.output_read", request_id=request_id, stream=stream, byte_count=min(payload_size, MAX_REMOTE_OUTPUT + 1), diagnostic_bytes=trailer_size, limit=MAX_REMOTE_OUTPUT, exceeded=payload_size > MAX_REMOTE_OUTPUT, used_fallback=used_fallback)
    if payload_size > MAX_REMOTE_OUTPUT:
        return None
    return data.decode(errors="replace")


def remote_snapshot(host: str) -> SourceSnapshot:
    logger = diagnostics.get_diagnostics()
    request_id = logger.new_id("discovery")
    # Validation/configuration and temporary-file errors belong to the attempt too.
    with logger.context(request_id=request_id), diagnostics.operation("discovery.remote_attempt", request_id=request_id):
        return _remote_snapshot(host, request_id)


def _remote_snapshot(host: str, request_id: str | None) -> SourceSnapshot:
    host = validate_host(host)
    persistent_ssh = load_persistent_ssh()
    timing = load_timing()
    with diagnostics.operation("discovery.remote_snapshot", host=host, request_id=request_id, timeout=timing.remote_command_timeout, persistent_ssh=persistent_ssh) as operation:
        master_preexisting = _persistent_master_alive(host, persistent_ssh)
        operation.fields["master_preexisting"] = master_preexisting
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            try:
                proc = subprocess.Popen(
                    _ssh_command(host, persistent_ssh),
                    stdout=output,
                    stderr=errors,
                    start_new_session=True,
                )
            except OSError as error:
                operation.outcome = "spawn_failed"
                diagnostics.error(handling="fallback", operation="discovery.remote_snapshot", host=host, request_id=request_id, error_type=type(error).__name__)
                return SourceSnapshot(False, (), frozenset(), error.strerror or str(error), request_id=request_id)
            operation.fields["process_pid"] = getattr(proc, "pid", None)
            timed_out = False
            wait_error: OSError | None = None
            returncode = None
            try:
                try:
                    returncode = proc.wait(timeout=timing.remote_command_timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    operation.outcome = "timed_out"
                    diagnostics.error(handling="fallback", operation="discovery.remote_snapshot", request_id=request_id, reason="timeout")
                except OSError as error:
                    wait_error = error
                    operation.outcome = "wait_failed"
                    diagnostics.error(handling="fallback", operation="discovery.remote_snapshot", request_id=request_id, error_type=type(error).__name__)
            finally:
                stdout, stderr = _stop_process(
                    proc, request_id=request_id,
                    reason="completed" if returncode is not None else "wait_incomplete",
                    keep_group=returncode is not None
                    and not master_preexisting
                    and _persistent_master_alive(host, persistent_ssh) is not False,
                )
            if timed_out:
                return SourceSnapshot(False, (), frozenset(), "timed out", request_id=request_id)
            if wait_error is not None:
                return SourceSnapshot(False, (), frozenset(), wait_error.strerror or str(wait_error), request_id=request_id)
            text = _read_output(output, stdout, request_id=request_id, stream="stdout")
            error = _read_output(errors, stderr, request_id=request_id, stream="stderr")
        snapshot = _source_result(returncode, text, error, kind="ssh", host=host, request_id=request_id)
        operation.fields.update(returncode=returncode, available=snapshot.available, pane_count=len(snapshot.panes))
        operation.outcome = "completed" if snapshot.available else "unavailable"
        return snapshot


def discover() -> SessionSnapshot:
    return SessionSnapshot(local_snapshot(), {host: remote_snapshot(host) for host in load_hosts()})


@dataclass
class _Request:
    process: object
    started: float
    output: object
    errors: object
    master_preexisting: bool | None
    request_id: str | None = field(default=None, compare=False)
    diagnostic_started: float = field(default_factory=time.monotonic, compare=False)
    context: dict[str, object] = field(default_factory=dict, compare=False)


def _availability_fields(before: SourceSnapshot | None, after: SourceSnapshot | None) -> dict[str, object]:
    fields: dict[str, object] = {}
    for suffix, snapshot in (("before", before), ("after", after)):
        fields[f"available_{suffix}"] = snapshot.available if snapshot else None
        for name in ("sessions", "panes", "agents"):
            fields[f"{name[:-1]}_count_{suffix}"] = len(getattr(snapshot, name)) if snapshot else 0
    return fields


@contextmanager
def _request_context(host: str, request_id: str | None, started: float, context: dict[str, object], source: SourceSnapshot | None = None):
    """Restore the originating worker context, including on later poll ticks."""
    with diagnostics.get_diagnostics().context(**(context | {"request_id": request_id})):
        try:
            with diagnostics.operation("discovery.request", host=host, request_id=request_id):
                yield
        except BaseException as error:
            diagnostics.log(
                "discovery.request_finished", host=host, request_id=request_id,
                outcome="cancelled" if isinstance(error, (KeyboardInterrupt, GeneratorExit)) else "failed",
                elapsed_seconds=time.monotonic() - started,
                **_availability_fields(source, source),
            )
            raise


def _persistent_master_alive(host: str, persistent_ssh: bool) -> bool | None:
    """Whether a ControlPersist master owns the host's control socket.

    A master keeps its ProxyCommand inside the process group of the request
    that created it, so that group must survive the request. An inconclusive
    check returns None, which must not trigger destructive group cleanup.
    """
    if not persistent_ssh:
        diagnostics.log("discovery.master_check", host=host, outcome="disabled")
        return False
    timeout = load_timing().ssh_master_check_timeout
    with diagnostics.operation("discovery.master_check", host=host, timeout=timeout) as operation:
        try:
            result = subprocess.run(
                ssh_command("-O", "check", host, persistent_ssh=persistent_ssh),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as error:
            operation.outcome = "inconclusive"
            diagnostics.error(handling="fallback", operation="discovery.master_check", host=host, error_type=type(error).__name__)
            return None
        operation.fields.update(returncode=result.returncode, alive=result.returncode == 0)
        return result.returncode == 0


def _stop_process(
    process: object, *, keep_group: bool = False, request_id: str | None = None, reason: str = "completed",
) -> tuple[object, object]:
    with diagnostics.operation("discovery.process_cleanup", request_id=request_id, reason=reason, keep_group=keep_group) as operation:
        running = process.poll() is None
        pid = getattr(process, "pid", None)
        operation.fields.update(process_pid=pid, running=running, group_preserved=keep_group and not running, term_sent=False, kill_sent=False)
        if pid is None or (keep_group and not running):
            if running:
                process.terminate()
                operation.fields["term_sent"] = True
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired as error:
                    diagnostics.error(error, handling="retry", operation="discovery.process_cleanup", request_id=request_id, reason="term_wait_timeout")
                    operation.fields["term_wait_timed_out"] = True
                    process.kill()
                    operation.fields["kill_sent"] = True
                    process.wait()
            return process.communicate()

        try:
            os.killpg(pid, signal.SIGTERM)
            operation.fields["term_sent"] = True
        except ProcessLookupError:
            operation.fields["term_group_missing"] = True
        if running:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired as error:
                diagnostics.error(error, handling="retry", operation="discovery.process_cleanup", request_id=request_id, reason="term_wait_timeout")
                operation.fields["term_wait_timed_out"] = True
        try:
            os.killpg(pid, signal.SIGKILL)
            operation.fields["kill_sent"] = True
        except ProcessLookupError:
            operation.fields["kill_group_missing"] = True
        process.wait()
        return process.communicate()


class DiscoveryPoller:
    def __init__(
        self,
        hosts: Iterable[str],
        *,
        popen: Callable[..., object] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        local: Callable[[], SourceSnapshot] | None = None,
        persistent_ssh: bool | None = None,
    ) -> None:
        self.hosts = tuple(validate_host(host) for host in hosts)
        self._timing = load_timing()
        self._local_snapshot = local or local_snapshot
        self._persistent_ssh = load_persistent_ssh() if persistent_ssh is None else persistent_ssh
        self.local = self._local_snapshot()
        self.remotes: dict[str, SourceSnapshot | None] = dict.fromkeys(self.hosts)
        self._popen = popen
        self._clock = clock
        self._next_local_poll = self._clock() + LOCAL_POLL_INTERVAL
        self._active: dict[str, _Request] = {}
        self._active_remote_host: str | None = None
        self._next = dict.fromkeys(self.hosts, 0.0)
        self._retry_delay = dict.fromkeys(self.hosts, self._timing.retry_initial_delay)
        self._poll_states: dict[str, tuple[str, float]] = {}
        diagnostics.log(
            "discovery.poller_initialized", host_count=len(self.hosts), persistent_ssh=self._persistent_ssh,
            local_available=self.local.available, local_poll_interval=LOCAL_POLL_INTERVAL,
            success_poll_interval=SUCCESS_POLL_INTERVAL, remote_command_timeout=self._timing.remote_command_timeout,
            local_command_timeout=self._timing.local_command_timeout,
            ssh_master_check_timeout=self._timing.ssh_master_check_timeout,
            retry_initial_delay=self._timing.retry_initial_delay, retry_max_delay=self._timing.retry_max_delay,
        )

    @property
    def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(self.local, self.remotes.copy())

    def _schedule(self, host: str, available: bool) -> None:
        if available:
            self._retry_delay[host] = self._timing.retry_initial_delay
            interval = LOCAL_POLL_INTERVAL if host == self._active_remote_host else SUCCESS_POLL_INTERVAL
        else:
            interval = self._retry_delay[host]
            self._retry_delay[host] = min(interval * 2, self._timing.retry_max_delay)
        self._next[host] = self._clock() + interval
        diagnostics.log(
            "discovery.scheduled", host=host, request_id=self.remotes[host].request_id if self.remotes[host] else None,
            available=available, interval=interval, next_deadline=self._next[host],
            next_retry_delay=self._retry_delay[host], active=host == self._active_remote_host,
        )

    def _finish_request(self, host: str, request: _Request, returncode: int) -> bool:
        age = self._clock() - request.started
        stdout, stderr = _stop_process(
            request.process, request_id=request.request_id,
            keep_group=not request.master_preexisting
            and _persistent_master_alive(host, self._persistent_ssh) is not False,
        )
        if age > self._timing.remote_command_timeout:
            request.output.close()
            request.errors.close()
            del self._active[host]
            self._next[host] = self._clock()
            diagnostics.log(
                "discovery.request_finished", host=host, request_id=request.request_id,
                outcome="expired", age_seconds=age, returncode=returncode,
                **_availability_fields(self.remotes[host], self.remotes[host]),
            )
            return False
        text = _read_output(request.output, stdout, request_id=request.request_id, stream="stdout")
        error = _read_output(request.errors, stderr, request_id=request.request_id, stream="stderr")
        request.output.close()
        request.errors.close()
        snapshot = replace(
            _source_result(returncode, text, error, kind="ssh", host=host, request_id=request.request_id),
            sampled_at=request.started,
        )
        previous = self.remotes[host]
        changed = snapshot != previous
        diagnostics.log(
            "discovery.request_finished", host=host, request_id=request.request_id,
            outcome="completed" if snapshot.available else "unavailable", returncode=returncode, changed=changed,
            elapsed_seconds=time.monotonic() - request.diagnostic_started,
            **_availability_fields(previous, snapshot),
        )
        self.remotes[host] = snapshot
        del self._active[host]
        self._schedule(host, snapshot.available)
        return changed

    def _start_request(self, host: str) -> bool:
        logger = diagnostics.get_diagnostics()
        request_id = logger.new_id("discovery")
        context = logger.context_values()
        started = time.monotonic()
        with _request_context(host, request_id, started, context, self.remotes[host]):
            diagnostics.log("discovery.request_starting", host=host, request_id=request_id, timeout=self._timing.remote_command_timeout, persistent_ssh=self._persistent_ssh)
            master_preexisting = _persistent_master_alive(host, self._persistent_ssh)
            output = tempfile.TemporaryFile()
            try:
                errors = tempfile.TemporaryFile()
            except BaseException:
                output.close()
                raise
            try:
                process = self._popen(
                    _ssh_command(host, self._persistent_ssh),
                    stdout=output,
                    stderr=errors,
                    start_new_session=True,
                )
            except OSError as error:
                output.close()
                errors.close()
                diagnostics.error(error, handling="retry", operation="discovery.request_spawn", host=host, request_id=request_id)
                snapshot = SourceSnapshot(False, (), frozenset(), error.strerror or str(error), request_id=request_id)
                previous = self.remotes[host]
                changed = snapshot != previous
                diagnostics.log("discovery.request_finished", host=host, request_id=request_id, outcome="spawn_failed", elapsed_seconds=time.monotonic() - started, **_availability_fields(previous, snapshot))
                self.remotes[host] = snapshot
                self._schedule(host, False)
                return changed
            except BaseException:
                output.close()
                errors.close()
                raise
            self._active[host] = _Request(process, self._clock(), output, errors, master_preexisting, request_id, started, context)
            diagnostics.log("discovery.request_started", host=host, request_id=request_id, process_pid=getattr(process, "pid", None), master_preexisting=master_preexisting)
            return False

    def tick(self, active_remote_host: str | None = None) -> bool:
        now = self._clock()
        active_remote_host = active_remote_host if active_remote_host in self.remotes else None
        if active_remote_host != self._active_remote_host:
            previous = self._active_remote_host
            self._active_remote_host = active_remote_host
            diagnostics.log("discovery.active_host_changed", previous_host=previous, host=active_remote_host)
            if previous and self.remotes[previous] and self.remotes[previous].available:
                self._next[previous] = now + SUCCESS_POLL_INTERVAL
                diagnostics.log("discovery.rescheduled", host=previous, reason="inactive_host", next_deadline=self._next[previous])
            if active_remote_host and active_remote_host not in self._active:
                self._next[active_remote_host] = now
                diagnostics.log("discovery.rescheduled", host=active_remote_host, reason="active_host", next_deadline=now)
        changed = False
        if now >= self._next_local_poll:
            snapshot = self._local_snapshot()
            changed = snapshot != self.local
            self.local = snapshot
            self._next_local_poll = now + LOCAL_POLL_INTERVAL
        for host in self.hosts:
            if host not in self._active and self._clock() >= self._next[host]:
                changed |= self._start_request(host)

        for host, request in list(self._active.items()):
            # Polling itself stays silent; only completion/failure needs a span.
            try:
                returncode = request.process.poll()
            except BaseException:
                with _request_context(host, request.request_id, request.diagnostic_started, request.context, self.remotes[host]):
                    raise
            if returncode is None and self._clock() - request.started < self._timing.remote_command_timeout:
                continue
            with _request_context(host, request.request_id, request.diagnostic_started, request.context, self.remotes[host]):
                if returncode is None:
                    diagnostics.error(handling="retry", operation="discovery.request", host=host, request_id=request.request_id, reason="timeout", timeout=self._timing.remote_command_timeout)
                    _stop_process(request.process, request_id=request.request_id, reason="timeout")
                    request.output.close()
                    request.errors.close()
                    previous = self.remotes[host]
                    snapshot = SourceSnapshot(False, (), frozenset(), "timed out", request_id=request.request_id)
                    diagnostics.log("discovery.request_finished", host=host, request_id=request.request_id, outcome="timed_out", elapsed_seconds=time.monotonic() - request.diagnostic_started, **_availability_fields(previous, snapshot))
                    changed |= snapshot != previous
                    self.remotes[host] = snapshot
                    del self._active[host]
                    self._schedule(host, False)
                else:
                    changed |= self._finish_request(host, request, returncode)
        for host in self.hosts:
            state = "inflight" if host in self._active else "not_due"
            previous_state, logged_at = self._poll_states.get(host, (None, 0.0))
            if state != previous_state or now - logged_at >= DIAGNOSTIC_HEARTBEAT_INTERVAL:
                diagnostics.log("discovery.poll_state", host=host, state=state, next_deadline=self._next[host], request_id=self._active[host].request_id if host in self._active else None)
                self._poll_states[host] = (state, now)
        return changed

    def refresh(self) -> bool:
        diagnostics.log("discovery.refresh", host_count=len(self.hosts), pending_count=len(self._active))
        snapshot = self._local_snapshot()
        changed = snapshot != self.local
        self.local = snapshot
        now = self._clock()
        self._next_local_poll = now + LOCAL_POLL_INTERVAL
        for host in self.hosts:
            self._retry_delay[host] = self._timing.retry_initial_delay
            if host not in self._active:
                self._next[host] = now
                diagnostics.log("discovery.rescheduled", host=host, reason="refresh", next_deadline=now)
        return changed

    def discard(self, target: Target) -> None:
        diagnostics.log("discovery.discard", kind=target.kind, host=target.host)
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
                sampled_at=source.sampled_at,
                request_id=source.request_id,
            )
            return
        if target.host not in self.remotes:
            return
        if request := self._active.pop(target.host, None):
            with _request_context(target.host, request.request_id, request.diagnostic_started, request.context, self.remotes[target.host]):
                _stop_process(request.process, request_id=request.request_id, reason="discard")
                request.output.close()
                request.errors.close()
                diagnostics.log("discovery.request_finished", host=target.host, request_id=request.request_id, outcome="discarded", elapsed_seconds=time.monotonic() - request.diagnostic_started, **_availability_fields(self.remotes[target.host], self.remotes[target.host]))
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
                sampled_at=source.sampled_at,
                request_id=source.request_id,
            )

    def close(self) -> None:
        diagnostics.log("discovery.close", pending_count=len(self._active))
        for host, request in self._active.items():
            with _request_context(host, request.request_id, request.diagnostic_started, request.context, self.remotes[host]):
                _stop_process(request.process, request_id=request.request_id, reason="close")
                request.output.close()
                request.errors.close()
                diagnostics.log("discovery.request_finished", host=host, request_id=request.request_id, outcome="closed", elapsed_seconds=time.monotonic() - request.diagnostic_started, **_availability_fields(self.remotes[host], self.remotes[host]))
        self._active.clear()
