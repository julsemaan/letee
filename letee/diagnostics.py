from __future__ import annotations

import atexit
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import stat
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any, TextIO


_CONTEXT: ContextVar[dict[str, str]] = ContextVar("letee_diagnostic_context", default={})
_DEFAULT_LOCK = threading.Lock()
_DEFAULT: Diagnostics | None = None


def _server_name() -> str:
    try:
        from . import config

        return config.current_server()
    except (ImportError, AttributeError):
        return "default"


class Diagnostics:
    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        server: str | None = None,
    ) -> None:
        configured = os.environ.get("LETEE_DEBUG_LOG") if path is None else path
        self.path = Path(configured).expanduser() if configured else None
        self.server = server or _server_name()
        self.run_id = uuid.uuid4().hex
        self._events = 0
        self._ids = 0
        self._lock = threading.Lock()
        self._closed = False
        self._file: TextIO | None = None
        self._queue: queue.Queue[dict[str, Any] | None] | None = None
        self._writer: threading.Thread | None = None
        if self.path is not None:
            self._open()

    @property
    def enabled(self) -> bool:
        return self._queue is not None and not self._closed

    def _open(self) -> None:
        assert self.path is not None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(self.path, flags, stat.S_IRUSR | stat.S_IWUSR)
            try:
                os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
                self._file = os.fdopen(fd, "a", encoding="utf-8", buffering=1)
            except Exception:
                os.close(fd)
                raise
        except (OSError, ValueError):
            return
        self._queue = queue.Queue()
        self._writer = threading.Thread(
            target=self._write_loop,
            name="letee-diagnostics",
            daemon=True,
        )
        self._writer.start()

    def _write_loop(self) -> None:
        assert self._queue is not None
        while True:
            record = self._queue.get()
            try:
                if record is None:
                    return
                if self._file is not None:
                    try:
                        line = json.dumps(
                            record,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            default=str,
                        ) + "\n"
                        self._file.write(line)
                        self._file.flush()
                    except Exception:
                        pass
            finally:
                self._queue.task_done()

    def new_id(self, kind: str) -> str | None:
        if not self.enabled:
            return None
        with self._lock:
            self._ids += 1
            return f"{kind}-{self._ids}"

    def new_input_id(self) -> str | None:
        return self.new_id("input")

    def new_action_id(self) -> str | None:
        return self.new_id("action")

    def context_values(self) -> dict[str, str]:
        return dict(_CONTEXT.get())

    @contextmanager
    def context(
        self,
        *,
        action_id: str | None = None,
        input_id: str | None = None,
    ) -> Iterator[None]:
        values = dict(_CONTEXT.get())
        if action_id is not None:
            values["action_id"] = action_id
        if input_id is not None:
            values["input_id"] = input_id
        token = _CONTEXT.set(values)
        try:
            yield
        finally:
            _CONTEXT.reset(token)

    def emit(self, event: str, **fields: Any) -> str | None:
        if not self.enabled:
            return None
        with self._lock:
            if self._closed or self._queue is None:
                return None
            self._events += 1
            event_id = f"{self.run_id}:{self._events}"
            record: dict[str, Any] = {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "monotonic_ns": time.monotonic_ns(),
                "run_id": self.run_id,
                "event_id": event_id,
                "pid": os.getpid(),
                "thread": threading.current_thread().name,
                "server": self.server,
                "event": event,
            }
            record.update(_CONTEXT.get())
            record.update(fields)
            self._queue.put(record)
            return event_id

    def flush(self) -> None:
        if self._queue is not None:
            self._queue.join()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._queue is None:
                return
            self._queue.put(None)
        if self._writer is not None:
            self._writer.join()
        if self._file is not None:
            self._file.close()


def _configured_path() -> str:
    return os.environ.get("LETEE_DEBUG_LOG", "")


def get_diagnostics(server: str | None = None) -> Diagnostics:
    global _DEFAULT
    configured = _configured_path()
    selected_server = server or _server_name()
    with _DEFAULT_LOCK:
        if (
            _DEFAULT is None
            or _DEFAULT.path != (Path(configured).expanduser() if configured else None)
            or _DEFAULT.server != selected_server
            or not _DEFAULT.enabled and configured
        ):
            if _DEFAULT is not None:
                _DEFAULT.close()
            _DEFAULT = Diagnostics(configured or None, server=selected_server)
        return _DEFAULT


def log(event: str, **fields: Any) -> str | None:
    return get_diagnostics().emit(event, **fields)


def new_input_id() -> str | None:
    return get_diagnostics().new_input_id()


def new_action_id() -> str | None:
    return get_diagnostics().new_action_id()


def flush() -> None:
    if _DEFAULT is not None:
        _DEFAULT.flush()


def close() -> None:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is not None:
            _DEFAULT.close()
            _DEFAULT = None


atexit.register(close)
