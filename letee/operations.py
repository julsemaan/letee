"""Server-scoped transactions. Callers submit intent, never hold sessions_lock."""
from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Literal

from . import cockpit, config, sessions
from .names import PaneTarget, Target


@dataclass(frozen=True)
class Effect:
    kind: Literal[
        "switch", "switch_pane", "add_switch", "create", "kill", "kill_agent", "rename",
        "save_favorites", "status", "show_reconnecting", "ensure_created", "show_unavailable",
    ]
    target: Target | PaneTarget | None = None
    favorites: tuple[Target, ...] | None = None
    message: str = ""
    automatic: bool = False
    agent_id: str = ""
    favorite_move: tuple[Target, Target] | None = None
    previous_favorites: tuple[Target, ...] | None = None
    launch_id: str | None = field(default=None, compare=False)
    target_revision: str | None = field(default=None, compare=False)
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex, compare=False)


@dataclass(frozen=True)
class EffectResult:
    effect: Effect
    favorites: tuple[Target, ...]
    error: str = ""
    stale_navigation: bool = False
    action_id: str | None = None
    input_id: str | None = None
    partial_success: bool = False
    reset_matched: bool = False
    completed_at: float | None = field(default=None, compare=False)
    launch_id: str | None = None


def _state() -> dict:
    return config.load_operations()


def launch_identity() -> str:
    return _state()["launch"]


def capture(effect: Effect) -> Effect:
    """Capture before queueing automatic work, not after waiting for the lock."""
    if effect.automatic and (effect.launch_id is None or effect.target_revision is None):
        state = _state()
        target = effect.target.target if isinstance(effect.target, PaneTarget) else effect.target
        revision = state.get("revisions", {}).get(target.format(), "") if target else ""
        return replace(effect, launch_id=state["launch"] if effect.launch_id is None else effect.launch_id,
                       target_revision=revision if effect.target_revision is None else effect.target_revision)
    return effect


def _advance(state: dict, invalidated: Target | None = None, explicit: Target | None = None, *, new_launch: bool = True) -> None:
    original = state
    state = {**state, "revisions": dict(state.get("revisions", {})), "invalidated": list(state["invalidated"])}
    if new_launch:
        state["launch"] = uuid.uuid4().hex
    if invalidated is not None:
        state.setdefault("revisions", {})[invalidated.format()] = uuid.uuid4().hex
        if invalidated.format() not in state["invalidated"]:
            state["invalidated"].append(invalidated.format())
    if explicit is not None:
        state.setdefault("revisions", {})[explicit.format()] = uuid.uuid4().hex
        if explicit.format() in state["invalidated"]:
            state["invalidated"].remove(explicit.format())
    config.save_operations(state)
    original.update(state)


def is_current(result: EffectResult) -> bool:
    return result.launch_id is None or result.launch_id == launch_identity()


def renamed_target(effect: Effect) -> Target | None:
    if effect.kind != "rename" or not isinstance(effect.target, Target):
        return None
    return Target(effect.target.kind, effect.message, effect.target.host)


def planned_favorites(effect: Effect, favorites: tuple[Target, ...]) -> tuple[Target, ...]:
    if effect.kind in ("add_switch", "create") and isinstance(effect.target, Target):
        return favorites if effect.target in favorites else (*favorites, effect.target)
    if effect.kind == "kill" and isinstance(effect.target, Target):
        return tuple(target for target in favorites if target != effect.target)
    if effect.kind == "rename":
        renamed = renamed_target(effect)
        if renamed is not None:
            return tuple(dict.fromkeys(renamed if target == effect.target else target for target in favorites))
    if effect.kind == "save_favorites":
        if isinstance(effect.target, Target):
            return tuple(target for target in favorites if target != effect.target)
        if effect.favorite_move is not None:
            source, destination = effect.favorite_move
            if source in favorites and destination in favorites:
                planned = list(favorites)
                index = planned.index(destination)
                planned.remove(source)
                planned.insert(index, source)
                return tuple(planned)
    return favorites


def effect_error(effect: Effect, error: BaseException) -> str:
    if isinstance(error, subprocess.TimeoutExpired):
        target = effect.target.target if isinstance(effect.target, PaneTarget) else effect.target
        return f"{effect.kind}{f' {target.format()}' if isinstance(target, Target) else ''} timed out"
    if isinstance(error, subprocess.CalledProcessError):
        return (error.stderr or error.stdout or "").strip() or f"exit status {error.returncode}"
    if isinstance(error, OSError):
        return error.strerror or str(error)
    message = str(error)
    if effect.kind == "kill_agent" and message.startswith("kill agent "):
        _, separator, reason = message.partition(" failed: ")
        if separator:
            return reason
        if message.endswith(" timed out"):
            return "timed out"
    return message


def perform(effect: Effect, favorites: tuple[Target, ...] = (), *, raise_errors: bool = False) -> EffectResult:
    if effect.kind == "status":
        return EffectResult(effect, favorites)
    effect = capture(effect)
    fallback = effect.previous_favorites if effect.previous_favorites is not None else favorites
    try:
        with config.sessions_lock():
            favorites = tuple(config.load_sessions())
            fallback = favorites
            return _perform_unlocked(effect, favorites, _state(), raise_errors=raise_errors)
    except (OSError, SystemExit, subprocess.SubprocessError) as error:
        if raise_errors:
            raise
        return EffectResult(effect, fallback, effect_error(effect, error), launch_id=launch_identity())


def _perform_unlocked(effect: Effect, favorites: tuple[Target, ...], state: dict, *, raise_errors: bool = False) -> EffectResult:
    target = effect.target.target if isinstance(effect.target, PaneTarget) else effect.target
    planned = planned_favorites(effect, favorites)
    partial = reset = False
    def result(error: str = "", stale: bool = False) -> EffectResult:
        return EffectResult(effect, planned, error, stale_navigation=stale,
                            partial_success=partial, reset_matched=reset, launch_id=state["launch"],
                            completed_at=time.monotonic())
    try:
        if effect.automatic:
            revision = state.get("revisions", {}).get(target.format(), "") if target else ""
            if (effect.launch_id != state["launch"] or effect.target_revision != revision
                    or (target is not None and target.format() in state["invalidated"])):
                return result(stale=True)
            if effect.kind == "ensure_created":
                if target not in favorites and target != cockpit.current_target():
                    return result(stale=True)
            elif target != cockpit.current_target():
                return result(stale=True)
            if isinstance(target, Target):
                deadline = cockpit.startup_deadline(target)
                if (deadline is not None and time.monotonic() < deadline
                        and not cockpit.attachment_failed(target, state["launch"])):
                    return result(stale=True)
        if effect.kind in ("switch", "add_switch", "switch_pane", "create") and isinstance(target, Target):
            if effect.kind == "create":
                sessions.create(target)
            if planned != favorites:
                config.save_sessions(list(planned))
            # Ensure creation under the lock; delayed pane commands must not recreate renamed targets.
            # Automatic preparation must leave the current overlay in place on failure.
            command = None
            if effect.automatic:
                sessions.ensure_created(target)
                command = sessions.attach_command(target, attach_only=True)
            if effect.automatic and cockpit.current_target() != target:
                return result(stale=True)
            _advance(state, explicit=None if effect.automatic else target)
            if isinstance(effect.target, PaneTarget):
                cockpit.switch(target, lambda: sessions.pane_attach_command(effect.target), effect.message, launch_id=state["launch"])
            else:
                cockpit.switch(target, command if effect.automatic else lambda: sessions.attach_command(target), launch_id=state["launch"])
        elif effect.kind == "ensure_created" and isinstance(target, Target):
            sessions.ensure_created(target)
        elif effect.kind == "kill_agent" and isinstance(effect.target, PaneTarget):
            sessions.kill_agent(effect.target)
        elif effect.kind == "rename" and isinstance(target, Target):
            if target not in favorites:
                raise SystemExit(f"Session not tracked: {target.format()}")
            renamed = renamed_target(effect)
            active = cockpit.current_target() == target
            sessions.rename(target, renamed.session)
            partial = True
            try:
                try:
                    _advance(state, invalidated=target, explicit=renamed, new_launch=active)
                finally:
                    cockpit.rename_target(target, renamed)
                if active:
                    cockpit.switch(renamed, sessions.attach_command(renamed, attach_only=True), launch_id=state["launch"])
            finally:
                config.save_sessions(list(planned))
        elif effect.kind == "kill" and isinstance(target, Target):
            active = cockpit.current_target() == target
            if active:
                cockpit.set_expected_right_pane_death(target)
            try:
                sessions.kill(target)
            except (SystemExit, OSError, subprocess.SubprocessError):
                if active:
                    cockpit.resolve_expected_right_pane_death(target, False)
                raise
            partial = True
            try:
                try:
                    _advance(state, invalidated=target, new_launch=active)
                finally:
                    if active:
                        cockpit.resolve_expected_right_pane_death(target, True)
                    else:
                        reset = cockpit.reset_to_help(target)
            finally:
                config.save_sessions(list(planned))
        elif effect.kind == "save_favorites":
            active = isinstance(target, Target) and cockpit.current_target() == target
            config.save_sessions(planned)
            partial = True
            if isinstance(target, Target):
                try:
                    _advance(state, invalidated=target, new_launch=active)
                finally:
                    if active:
                        reset = cockpit.reset_to_help(target)
        elif effect.kind in ("show_reconnecting", "show_unavailable") and isinstance(target, Target):
            deadline = cockpit.startup_deadline(target)
            if effect.kind == "show_reconnecting":
                if effect.automatic:
                    cockpit.show_reconnecting(target)
                else:
                    _advance(state, explicit=target)
                    cockpit.switch_reconnecting(target, launch_id=state["launch"])
                    deadline = cockpit.startup_deadline(target)
            else:
                cockpit.show_unavailable(target)
            if deadline is not None:
                cockpit.clear_startup(target, deadline)
    except (SystemExit, OSError, subprocess.SubprocessError) as error:
        if raise_errors:
            raise
        if effect.kind == "save_favorites" and not partial:
            planned = favorites
        return result(effect_error(effect, error))
    return result()


def navigate(target: Target | PaneTarget | None = None, *, slot: int | None = None) -> None:
    with config.sessions_lock():
        if slot is not None:
            favorites = config.load_sessions()
            if slot > len(favorites):
                raise SystemExit(f"No session in slot {slot}")
            target = favorites[slot - 1]
        cockpit.require_cockpit()
        effect = Effect("switch_pane" if isinstance(target, PaneTarget) else "switch", target)
        _perform_unlocked(effect, tuple(config.load_sessions()), _state(), raise_errors=True)


def create_and_switch(target: Target) -> None:
    with config.sessions_lock():
        sessions.create(target)
        cockpit.require_cockpit()
        _perform_unlocked(Effect("switch", target), tuple(config.load_sessions()), _state(), raise_errors=True)


def kill(target: Target) -> None:
    perform(Effect("kill", target), raise_errors=True)


def rename(target: Target, name: str) -> None:
    perform(Effect("rename", target, message=name), raise_errors=True)


def synchronize_target() -> Target | None:
    with config.sessions_lock():
        return cockpit.current_target(repair=True)


def sample_target(*, synchronize: bool = False):
    """Read markers and their identity together, outside any launch's preparation window."""
    with config.sessions_lock():
        launch_id = launch_identity()
        current = cockpit.current_target(repair=True) if synchronize else None
        status = cockpit.status_snapshot()
        target = current if synchronize else status.current_target if status else None
        deadline = cockpit.startup_deadline(target) if target else None
        if status is not None and target is not None:
            status = replace(status, attachment_failed=cockpit.attachment_failed(target, launch_id))
        return launch_id, status, current, deadline


def clear_startup(target: Target, deadline: float, launch_id: str | None = None) -> bool:
    try:
        with config.sessions_lock():
            if launch_id is None or launch_id == launch_identity():
                cockpit.clear_startup(target, deadline, launch_id)
    except (OSError, SystemExit, subprocess.SubprocessError):
        # The next discovery observation can retry cleanup.
        return False
    return True


def tracking_snapshot(revision):
    with config.sessions_lock(blocking=False) as acquired:
        if acquired:
            current = config.sessions_revision()
            if current != revision:
                return current, tuple(config.load_sessions())
    return None
