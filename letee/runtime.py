"""Status sampling and asynchronous workspace snapshots, without UI dependencies."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass

from . import cockpit, diagnostics, tmux
from .discovery import DiscoveryPoller, SessionSnapshot
from .names import PaneTarget, Target
from .workspace import Workspace, workspace_from_json, workspace_to_json


STATUS_POLL_INTERVAL = 0.1
WORKSPACE_OPTION = "@letee_workspace"


@dataclass(frozen=True)
class RuntimeUpdate:
    kind: str
    target: Target | PaneTarget | None = None
    agent_id: str = ""
    renamed: Target | None = None
    removed: bool = False
    reset_matched: bool = False
    failed: bool = False
    partial_success: bool = False
    stale: bool = False
    automatic: bool = False


@dataclass(frozen=True)
class StatusResult:
    snapshot: SessionSnapshot
    current_target: Target | None
    bell_target: Target | None
    current_agent: str | None
    pane_active: bool
    generation: int
    refreshed: bool = False
    suppression_expired: bool = False
    sampled: bool = False


@dataclass(frozen=True)
class StatusPollState:
    snapshot: SessionSnapshot
    current_target: Target | None
    bell_target: Target | None
    current_agent: str | None
    pane_active: bool
    suppressed_target: Target | None


def _focused_agent_id(snapshot: SessionSnapshot, current_target: Target | None, fallback: str | None) -> str | None:
    focused = {pane for pane in snapshot.focused_panes if pane.target == current_target}
    if not focused:
        return fallback
    return next((agent.agent_id for agent in snapshot.agents if agent.pane_target in focused), None)


class StatusPoller:
    def __init__(self, poller: DiscoveryPoller, current_target: Target | None) -> None:
        self._poller = poller
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="letee-status")
        self._future: Future[StatusResult] | None = None
        self._commands: list[tuple[str, Target | None]] = []
        self._next_poll = 0.0
        self.status_sampled = False
        self.snapshot = poller.snapshot
        self.current_target = current_target
        self.bell_target: Target | None = None
        self.current_agent: str | None = None
        self.pane_active = True
        self._generation = 0
        self._refresh_pending = False
        self._refresh_target: Target | None = None
        self._pending_agent: tuple[PaneTarget, str] | None = None
        self._suppressed_target: Target | None = None
        debug = diagnostics.get_diagnostics(server=cockpit.tmux.SERVER)
        # Diagnostic identity must not participate in snapshot equality.
        self.snapshot_trace: dict[str, object] = {"snapshot_id": debug.new_id("snapshot")}
        self._future_trace: dict[str, object] = {}

    @property
    def pending_agent(self) -> tuple[PaneTarget, str] | None:
        return self._pending_agent

    def _poll_state(self) -> StatusPollState:
        return StatusPollState(
            self.snapshot,
            self.current_target,
            self.bell_target,
            self.current_agent,
            self.pane_active,
            self._suppressed_target,
        )

    def _sample(
        self,
        commands: tuple[tuple[str, Target | None], ...],
        generation: int,
        state: StatusPollState | None = None,
    ) -> StatusResult:
        state = state or self._poll_state()
        snapshot = state.snapshot
        current_target = state.current_target
        bell_target = state.bell_target
        stored_agent = state.current_agent
        pane_active = state.pane_active
        suppression_expired = False
        sampled = False
        try:
            for command, target in commands:
                if command == "discard" and target is not None:
                    self._poller.discard(target)
                elif command == "refresh":
                    self._poller.refresh()
            status = cockpit.status_snapshot()
            if status is None:
                raise SystemExit("invalid cockpit status snapshot")
            sampled = True
            suppressed_target = state.suppressed_target
            suppression_expired = (
                suppressed_target is not None
                and status.current_target != suppressed_target
            )
            if suppression_expired:
                suppressed_target = None
            current_target = status.current_target if status.current_target is not None else state.current_target
            if current_target == suppressed_target:
                current_target = None
            active_host = current_target.host if current_target and current_target.kind == "ssh" else None
            self._poller.tick(active_host)
            snapshot = self._poller.snapshot
            bell_target = status.bell_target
            stored_agent = status.current_agent
            pane_active = status.pane_active
        except (OSError, SystemExit, subprocess.SubprocessError) as error:
            diagnostics.error(error, handling="fallback", operation="status_sample", generation=generation)
        current_agent = _focused_agent_id(snapshot, current_target, stored_agent)
        return StatusResult(
            snapshot,
            current_target,
            bell_target,
            current_agent,
            pane_active,
            generation,
            any(command == "refresh" for command, _ in commands),
            suppression_expired,
            sampled,
        )

    def _run_sample(
        self,
        commands: tuple[tuple[str, Target | None], ...],
        generation: int,
        state: StatusPollState,
        trace: dict[str, object],
    ) -> StatusResult:
        with diagnostics.get_diagnostics().context(**trace):
            diagnostics.log("status_worker_started", generation=generation)
            with diagnostics.operation("status_sample", generation=generation):
                result = self._sample(commands, generation, state)
            diagnostics.log(
                "status_worker_completed", generation=generation,
                sampled=result.sampled, refreshed=result.refreshed,
                session_count=len(result.snapshot.sessions), agent_count=len(result.snapshot.agents),
                local_available=result.snapshot.local.available,
                remote_states={host: "pending" if source is None else "available" if source.available else "unavailable"
                               for host, source in result.snapshot.remotes.items()},
                discovery_requests={"local": result.snapshot.local.request_id,
                                    **{host: source.request_id if source else None
                                       for host, source in result.snapshot.remotes.items()}},
            )
            return result

    def tick(self, now: float) -> bool:
        debug = diagnostics.get_diagnostics(server=cockpit.tmux.SERVER)
        changed = False
        self.status_sampled = False
        if self._future is not None and self._future.done():
            with debug.context(**self._future_trace):
                try:
                    with diagnostics.operation("status_collect"):
                        result = self._future.result()
                except BaseException:
                    # status_collect records the exception before this result boundary.
                    debug.emit("status_result", accepted=False, reason="worker_failed", generation=self._generation)
                    raise
            self._future = None
            accepted = result.generation == self._generation
            debug.emit(
                "status_result", **self._future_trace, accepted=accepted,
                reason="current_generation" if accepted else "generation_changed",
                generation=result.generation, current_generation=self._generation,
                sampled=result.sampled, refreshed=result.refreshed,
                discovery_requests={"local": result.snapshot.local.request_id,
                                    **{host: source.request_id if source else None
                                       for host, source in result.snapshot.remotes.items()}},
            )
            if accepted:
                self.snapshot_trace = dict(self._future_trace)
                changed = (
                    result.snapshot != self.snapshot
                    or result.current_target != self.current_target
                )
                self.status_sampled = result.sampled
                self.snapshot = result.snapshot
                if result.refreshed is True:
                    self._refresh_pending = any(
                        command == "refresh" for command, _ in self._commands
                    )
                if self._refresh_target is not None:
                    target = self._refresh_target
                    source = self.snapshot.remotes.get(target.host) if target.kind == "ssh" else None
                    if (
                        target in self.snapshot.sessions
                        or (result.refreshed is True and target.kind == "local")
                        or (source is not None and not source.available)
                    ):
                        self._refresh_target = None
                if result.suppression_expired:
                    self._suppressed_target = None
                self.current_target = result.current_target
                self.bell_target = result.bell_target
                if self._pending_agent is None:
                    self.current_agent = result.current_agent
                elif self._pending_agent[0] in result.snapshot.focused_panes:
                    self._pending_agent = None
                    self.current_agent = result.current_agent
                else:
                    self.current_agent = self._pending_agent[1]
                self.pane_active = result.pane_active
        if self._future is None and now >= self._next_poll:
            commands, self._commands = tuple(self._commands), []
            self._future_trace = {
                "request_id": debug.new_id("request"),
                "sample_id": debug.new_id("sample"),
                "snapshot_id": debug.new_id("snapshot"),
                **debug.context_values(),
            }
            debug.emit(
                "status_requested", **self._future_trace, generation=self._generation,
                commands=[command for command, _ in commands],
            )
            with debug.context(**self._future_trace):
                with diagnostics.operation("status_submit", generation=self._generation):
                    self._future = self._executor.submit(
                        copy_context().run, self._run_sample, commands, self._generation, self._poll_state(),
                        dict(self._future_trace),
                    )
            self._next_poll = now + STATUS_POLL_INTERVAL
        return changed

    def observe(self, update: RuntimeUpdate) -> None:
        if update.stale:
            return
        if update.failed:
            if (
                not update.partial_success
                and update.kind in ("switch", "add_switch", "switch_pane")
            ):
                self._generation += 1
            if not update.partial_success:
                return
        target = update.target
        if update.kind in ("switch", "add_switch", "create") and isinstance(target, Target):
            self._suppressed_target = None
            self.current_target = target
            self.current_agent = None
            self._pending_agent = None
            self._generation += 1
        elif update.kind == "switch_pane" and isinstance(target, PaneTarget):
            self._suppressed_target = None
            self.current_target = target.target
            self.current_agent = update.agent_id or None
            self._pending_agent = (
                (target, update.agent_id) if update.agent_id else None
            )
            self._generation += 1
        elif (
            update.kind == "show_reconnecting"
            and not update.automatic
            and isinstance(target, Target)
        ):
            self._suppressed_target = None
            self.current_target = target
            self.current_agent = None
            self._pending_agent = None
            self._generation += 1
        elif update.kind == "rename" and isinstance(target, Target):
            renamed = update.renamed
            if renamed is None:
                return
            if self.current_target == target:
                self.current_target = renamed
                self._refresh_target = renamed
            if self.bell_target == target:
                self.bell_target = renamed
            self._generation += 1
        elif update.kind == "kill" and isinstance(target, Target):
            if update.reset_matched or self.current_target == target:
                self._suppressed_target = target
                self.current_target = None
                self._generation += 1
        elif update.kind == "save_favorites" and isinstance(target, Target):
            if update.removed and self.current_target == target:
                self.current_target = None
                self._generation += 1

    @property
    def refresh_pending(self) -> bool:
        return self._refresh_pending or self._refresh_target is not None

    def refresh(self) -> bool:
        diagnostics.log("status_refresh_requested", generation=self._generation, **self.snapshot_trace)
        self._commands.append(("refresh", None))
        self._refresh_pending = True
        self._next_poll = 0.0
        return False

    def discard(self, target: Target) -> None:
        diagnostics.log("status_discard_requested", target=target.format(), generation=self._generation)
        self._commands.append(("discard", target))
        self._next_poll = 0.0

    def close(self) -> None:
        with diagnostics.operation("status_shutdown"):
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._poller.close()


def _write_workspace(payload: str) -> None:
    tmux.tmux("set-option", "-t", tmux.SESSION, WORKSPACE_OPTION, payload, capture=True)


def _read_workspace() -> str:
    return tmux.out("show-options", "-qv", "-t", tmux.SESSION, WORKSPACE_OPTION)


class WorkspacePublisher:
    """Keep only the latest pending snapshot, with one independent writer."""

    def __init__(self, writer: Callable[[str], None] | None = None) -> None:
        self._writer = writer or _write_workspace
        self._condition = threading.Condition()
        self._stopped = threading.Event()
        self._pending: Workspace | None = None
        self._published: Workspace | None = None
        self._thread = threading.Thread(target=self._run, name="letee-workspace-publisher", daemon=True)
        self._thread.start()

    def publish(self, workspace: Workspace) -> None:
        with self._condition:
            if self._stopped.is_set():
                return
            self._pending = workspace
            self._condition.notify()

    def _run(self) -> None:
        while not self._stopped.is_set():
            with self._condition:
                self._condition.wait_for(lambda: self._pending is not None or self._stopped.is_set())
                if self._stopped.is_set():
                    return
                workspace, self._pending = self._pending, None
            if workspace == self._published:
                continue
            try:
                self._writer(workspace_to_json(workspace))
            except (OSError, SystemExit, subprocess.SubprocessError):
                with self._condition:
                    if self._pending is None:
                        self._pending = workspace
                # Retry at the normal sampling rate, never in a tight loop.
                self._stopped.wait(STATUS_POLL_INTERVAL)
            else:
                self._published = workspace

    def close(self) -> None:
        self._stopped.set()
        with self._condition:
            self._pending = None
            self._condition.notify()


class WorkspaceReader:
    """Read the outer tmux option in the background, retaining the latest good value."""

    def __init__(self, reader: Callable[[], str] | None = None) -> None:
        self._reader = reader or _read_workspace
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._current = Workspace(loading=True)
        self._delivered = self._current
        self._thread = threading.Thread(target=self._run, name="letee-workspace-reader", daemon=True)
        self._thread.start()

    @property
    def current(self) -> Workspace:
        with self._lock:
            return self._current

    def read(self) -> Workspace | None:
        """Return a new snapshot once, without waiting for tmux."""
        with self._lock:
            if self._current == self._delivered:
                return None
            self._delivered = self._current
            return self._current

    def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                workspace = workspace_from_json(self._reader())
            except (OSError, SystemExit, subprocess.SubprocessError, ValueError):
                pass
            else:
                with self._lock:
                    if not self._stopped.is_set():
                        self._current = workspace
            self._stopped.wait(STATUS_POLL_INTERVAL)

    def close(self) -> None:
        self._stopped.set()

