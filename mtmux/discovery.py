from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import os
from random import random
import subprocess
import tempfile
import time

from .config import load_hosts, load_persistent_ssh
from .names import Target, validate_host
from .sessions import ssh_command


WINDOWS_COMMAND = 'tmux list-windows -a -F "#{session_name}:#{window_bell_flag}:#{window_flags}"'
MAX_REMOTE_OUTPUT = 1024 * 1024
LOCAL_POLL_INTERVAL = 0.5
SUCCESS_POLL_INTERVAL = 10
MAX_FAILURE_POLL_INTERVAL = 60


@dataclass(frozen=True)
class SourceSnapshot:
    available: bool
    sessions: tuple[Target, ...]
    bells: frozenset[Target]
    error: str | None = None


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


UNAVAILABLE = SourceSnapshot(False, (), frozenset())


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("TMUX", None)
    return env


def _parse_source_snapshot(text: str, *, kind: str, host: str | None = None) -> SourceSnapshot:
    sessions: list[Target] = []
    bells: set[Target] = set()
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        name, bell_flag, window_flags = parts
        try:
            target = Target("local", name) if kind == "local" else Target("ssh", name, host)
        except SystemExit:
            continue
        if target not in sessions:
            sessions.append(target)
        if bell_flag in ("1", "!") or "!" in window_flags:
            bells.add(target)
    return SourceSnapshot(True, tuple(sessions), frozenset(bells))


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
    missing_server = returncode == 1 and (
        stderr.startswith("no server running on ")
        or (stderr.startswith("error connecting to ") and stderr.strip().endswith("(No such file or directory)"))
    )
    if returncode == 0 or missing_server:
        return _parse_source_snapshot(stdout if returncode == 0 else "", kind=kind, host=host)
    return SourceSnapshot(False, (), frozenset(), stderr.strip() or f"remote command exited {returncode}")


def local_snapshot() -> SourceSnapshot:
    try:
        proc = subprocess.run(
            ["tmux", "list-windows", "-a", "-F", "#{session_name}:#{window_bell_flag}:#{window_flags}"],
            text=True,
            capture_output=True,
            env=_clean_env(),
        )
    except OSError as error:
        return SourceSnapshot(False, (), frozenset(), error.strerror or str(error))
    return _source_result(proc.returncode, proc.stdout, proc.stderr, kind="local")


def _ssh_command(host: str, persistent_ssh: bool) -> tuple[str, ...]:
    return ssh_command(
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=1", "-o", "ServerAliveCountMax=1",
        validate_host(host), WINDOWS_COMMAND,
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
        self._next = dict.fromkeys(self.hosts, 0.0)
        self._failures = dict.fromkeys(self.hosts, 0)

    @property
    def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(self.local, self.remotes.copy())

    def _schedule(self, host: str, now: float, available: bool) -> None:
        if available:
            self._failures[host] = 0
            self._next[host] = now + SUCCESS_POLL_INTERVAL
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

    def tick(self) -> bool:
        now = self._clock()
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
            )

    def close(self) -> None:
        for request in self._active.values():
            _stop_process(request.process)
            request.output.close()
            request.errors.close()
        self._active.clear()
