from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
WINDOW_ID_RE = re.compile(r"^@\d+$")
PANE_ID_RE = re.compile(r"^%\d+$")


def validate_name(value: str, label: str = "name") -> str:
    if not NAME_RE.fullmatch(value):
        raise SystemExit(f"Invalid {label}: {value!r}. Must match [A-Za-z0-9_.-]{{1,64}}")
    return value


def validate_host(value: str) -> str:
    validate_name(value, "host")
    if value.startswith("-"):
        raise SystemExit(f"Invalid host: {value!r}. Must not start with '-'")
    return value


@dataclass(frozen=True)
class Target:
    kind: Literal["local", "ssh"]
    session: str
    host: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("local", "ssh"):
            raise SystemExit(f"Invalid target kind: {self.kind!r}")
        validate_name(self.session, "session")
        if self.kind == "local":
            if self.host is not None:
                raise SystemExit("Local target must not have host")
        elif self.host is None:
            raise SystemExit("Invalid host: None")
        else:
            validate_host(self.host)

    def format(self) -> str:
        if self.kind == "local":
            return f"local:{self.session}"
        return f"ssh:{self.host}:{self.session}"


@dataclass(frozen=True)
class PaneTarget:
    target: Target
    window_id: str
    pane_id: str
    socket_path: str

    def __post_init__(self) -> None:
        if not isinstance(self.target, Target):
            raise SystemExit(f"Invalid target: {self.target!r}")
        if not WINDOW_ID_RE.fullmatch(self.window_id):
            raise SystemExit(f"Invalid window ID: {self.window_id!r}")
        if not PANE_ID_RE.fullmatch(self.pane_id):
            raise SystemExit(f"Invalid pane ID: {self.pane_id!r}")
        if not self.socket_path:
            raise SystemExit("Invalid socket path: must not be empty")


def parse_target(text: str) -> Target:
    parts = text.split(":")
    if len(parts) == 2 and parts[0] == "local":
        return Target("local", validate_name(parts[1], "session"))
    if len(parts) == 3 and parts[0] == "ssh":
        return Target("ssh", validate_name(parts[2], "session"), validate_host(parts[1]))
    raise SystemExit("Invalid target. Use local:<session> or ssh:<host>:<session>")
