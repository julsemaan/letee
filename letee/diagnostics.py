from __future__ import annotations

import atexit
from concurrent.futures import CancelledError
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import stat
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterator
from typing import Any, TextIO


_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("letee_diagnostic_context", default={})
_DEFAULT_LOCK = threading.Lock()
_DEFAULT: Diagnostics | None = None
_DRAIN_TIMEOUT = 1.0
_QUEUE_LIMIT = 4096


def _server_name() -> str:
    # Configuration validation can itself log. Do not import or validate it here.
    config = sys.modules.get("letee.config")
    return getattr(config, "_CURRENT_SERVER", "default")


class Diagnostics:
    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        server: str | None = None,
    ) -> None:
        configured = os.environ.get("LETEE_DEBUG_LOG") if path is None else path
        self.configured = configured
        self.path: Path | None = None
        self.server = server or _server_name()
        self.run_id = uuid.uuid4().hex
        self._events = 0
        self._dropped = 0
        self._ids = 0
        self._errors: deque[tuple[BaseException, str]] = deque(maxlen=64)
        self._lock = threading.Lock()
        self._closed = False
        self._warned = False
        self._file: TextIO | None = None
        self._queue: queue.Queue[dict[str, Any] | threading.Event | None] | None = None
        self._writer: threading.Thread | None = None
        if configured:
            try:
                self.path = Path(configured).expanduser()
                self._open()
            except Exception:
                self._warn("initialization")

    @property
    def enabled(self) -> bool:
        return self._queue is not None and not self._closed

    def _warn(self, stage: str) -> None:
        # Never send a sink failure back through the sink, or expose its message.
        if self._warned:
            return
        self._warned = True
        try:
            sys.stderr.write(f"letee: diagnostic log {stage} failed; records may be lost\n")
        except Exception:
            pass

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
            self._warn("initialization")
            return
        self._queue = queue.Queue(maxsize=_QUEUE_LIMIT)
        try:
            self._writer = threading.Thread(
                target=self._write_loop,
                name="letee-diagnostics",
                daemon=True,
            )
            self._writer.start()
        except Exception:
            self._queue = None
            self._writer = None
            self._warn("initialization")
            try:
                self._file.close()
            except Exception:
                self._warn("shutdown")

    def _write_loop(self) -> None:
        assert self._queue is not None
        while True:
            try:
                record = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._closed:
                    break
                continue
            try:
                if record is None:
                    break
                if isinstance(record, threading.Event):
                    record.set()
                    continue
                if self._file is not None:
                    try:
                        line = json.dumps(
                            record,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ) + "\n"
                    except Exception:
                        self._warn("serialization")
                        continue
                    try:
                        self._file.write(line)
                    except Exception:
                        self._warn("write")
                        continue
                    try:
                        self._file.flush()
                    except Exception:
                        self._warn("flush")
            finally:
                self._queue.task_done()
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                self._warn("shutdown")

    def new_id(self, kind: str) -> str | None:
        if not self.enabled:
            return None
        with self._lock:
            self._ids += 1
            return f"{kind}-{self._ids}"

    def error_id(self, exc: BaseException) -> str:
        with self._lock:
            for previous, error_id in self._errors:
                if previous is exc:
                    return error_id
            self._ids += 1
            error_id = f"{self.run_id}:error-{self._ids}"
            self._errors.append((exc, error_id))
            return error_id

    def new_input_id(self) -> str | None:
        return self.new_id("input")

    def new_action_id(self) -> str | None:
        return self.new_id("action")

    def context_values(self) -> dict[str, Any]:
        return dict(_CONTEXT.get())

    @contextmanager
    def context(
        self,
        *,
        action_id: str | None = None,
        input_id: str | None = None,
        **fields: Any,
    ) -> Iterator[None]:
        values = dict(_CONTEXT.get())
        values.update({key: value for key, value in fields.items() if value is not None})
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
            if isinstance(record.get("returncode"), int) and record["returncode"] < 0:
                record["signal"] = -record["returncode"]
            if self._dropped:
                record["dropped_records"] = self._dropped
            try:
                self._queue.put_nowait(record)
            except queue.Full:
                self._dropped += 1
                self._warn("queue capacity")
                return None
            return event_id

    def flush(self) -> None:
        if not self.enabled:
            return
        drained = threading.Event()
        try:
            self._queue.put_nowait(drained)
        except queue.Full:
            self._warn("flush queue capacity")
            return
        if not drained.wait(_DRAIN_TIMEOUT):
            self._warn("flush timeout")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._errors.clear()
            if self._queue is None:
                return
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                # The writer exits after draining a closed queue.
                self._warn("shutdown queue capacity")
        if self._writer is not None:
            self._writer.join(timeout=_DRAIN_TIMEOUT)
            if self._writer.is_alive():
                self._warn("shutdown timeout")


def _configured_path() -> str:
    return os.environ.get("LETEE_DEBUG_LOG", "")


def get_diagnostics(server: str | None = None) -> Diagnostics:
    global _DEFAULT
    configured = _configured_path()
    selected_server = server or _server_name()
    with _DEFAULT_LOCK:
        if (
            _DEFAULT is None
            or _DEFAULT.configured != (configured or None)
            or _DEFAULT.server != selected_server
        ):
            if _DEFAULT is not None:
                _DEFAULT.close()
            _DEFAULT = Diagnostics(configured or None, server=selected_server)
            if _DEFAULT.enabled:
                _install_thread_capture()
        return _DEFAULT


def log(event: str, **fields: Any) -> str | None:
    try:
        return get_diagnostics().emit(event, **fields)
    except Exception:
        # Diagnostic metadata must never replace an application failure.
        if _DEFAULT is not None:
            _DEFAULT._warn("recording")
        return None


class Operation:
    def __init__(self, fields: dict[str, Any]) -> None:
        self.fields = fields
        self.outcome = "completed"


@contextmanager
def operation(name: str, **fields: Any) -> Iterator[Operation]:
    logger = get_diagnostics()
    span = Operation(dict(fields))
    if not logger.enabled:
        yield span
        return
    operation_id = logger.new_id("operation")
    parent = _CONTEXT.get().get("operation_id")
    started = time.monotonic()
    context = span.fields | {"operation_id": operation_id, "parent_operation_id": parent}
    with logger.context(**context):
        log("operation_start", name=name, **span.fields)
        try:
            yield span
        except BaseException as exc:
            if isinstance(exc, SystemExit) and exc.code in (None, 0):
                span.outcome = "completed"
            else:
                span.outcome = "cancelled" if isinstance(exc, (CancelledError, KeyboardInterrupt, GeneratorExit)) else "failed"
                error(exc, handling="cancel" if span.outcome == "cancelled" else "propagate", name=name)
            raise
        finally:
            completion = span.fields | {"outcome": span.outcome,
                                        "duration_ms": round((time.monotonic() - started) * 1000, 3)}
            log("operation_end", name=name, **completion)


def error(exc: BaseException | None = None, *, handling: str = "fallback", **fields: Any) -> str | None:
    """Capture types and frame coordinates only, never exception text or locals."""
    try:
        logger = get_diagnostics()
        if not logger.enabled:
            return None
        if exc is None:
            exc = sys.exception()
        caller = sys._getframe(1)
        metadata: dict[str, Any] = {
            "handling": handling, "file": Path(caller.f_code.co_filename).name,
            "function": caller.f_code.co_name, "line": caller.f_lineno,
        }
        del caller
        if exc is not None:
            metadata["error_id"] = logger.error_id(exc)
            metadata["error_type"] = type(exc).__name__
            chain = []
            seen: set[int] = set()
            current: BaseException | None = exc
            while current is not None and id(current) not in seen and len(chain) < 8:
                seen.add(id(current))
                frames = []
                tb = current.__traceback__
                while tb is not None:
                    frames.append({"file": Path(tb.tb_frame.f_code.co_filename).name,
                                   "function": tb.tb_frame.f_code.co_name, "line": tb.tb_lineno})
                    tb = tb.tb_next
                chain.append({"type": type(current).__name__, "error_id": logger.error_id(current),
                              "frames": frames[-24:], "context_suppressed": current.__suppress_context__})
                current = current.__cause__ or current.__context__
            metadata["causes"] = chain
            if chain[0]["frames"]:
                metadata.update(chain[0]["frames"][-1])
            for key in ("errno", "returncode"):
                value = getattr(exc, key, None)
                if isinstance(value, int):
                    metadata[key] = value
        metadata.update(fields)
        return log("error", **metadata)
    except Exception:
        if _DEFAULT is not None:
            _DEFAULT._warn("error capture")
        return None


_THREAD_CAPTURE_INSTALLED = False


def _install_thread_capture() -> None:
    global _THREAD_CAPTURE_INSTALLED
    if _THREAD_CAPTURE_INSTALLED:
        return
    previous = threading.excepthook

    def capture(args: threading.ExceptHookArgs) -> None:
        if _DEFAULT is not None and _DEFAULT.enabled:
            error(args.exc_value, handling="propagate", operation="thread",
                  failed_thread=args.thread.name if args.thread else None)
        previous(args)

    threading.excepthook = capture
    _THREAD_CAPTURE_INSTALLED = True


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
