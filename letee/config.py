from __future__ import annotations

import os
from pathlib import Path
import tomllib

from .names import DEFAULT_SERVER, Target, normalize_server, parse_target, validate_host

DEFAULT_PREFIX = "C-s"
DEFAULT_SIDEBAR_WIDTH = 40
DEFAULT_STATUS_TIMEOUT = 5
DEFAULT_PERSISTENT_SSH = True
CONFIG_TEXT = f'hosts = []\nprefix = "{DEFAULT_PREFIX}"\nsidebar_width = {DEFAULT_SIDEBAR_WIDTH}\nstatus_timeout = {DEFAULT_STATUS_TIMEOUT}\npersistent_ssh = true\n'
WRAPPER_TEXT = """unbind C-b
set -g status off
set -g mouse on
"""
_CURRENT_SERVER = DEFAULT_SERVER


def set_server(server: str | None) -> str:
    global _CURRENT_SERVER
    _CURRENT_SERVER = normalize_server(server)
    return _CURRENT_SERVER


def current_server() -> str:
    return _CURRENT_SERVER


def config_dir() -> Path:
    override = os.environ.get("LETEE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "letee"


def paths() -> tuple[Path, Path]:
    base = config_dir()
    return base / "config.toml", base / "wrapper.tmux.conf"


def ensure_config() -> tuple[Path, Path]:
    cfg, wrapper = paths()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    if not cfg.exists():
        cfg.write_text(CONFIG_TEXT)
    if not wrapper.exists():
        wrapper.write_text(WRAPPER_TEXT)
    return cfg, wrapper


def sessions_path(server: str | None = None) -> Path:
    name = normalize_server(_CURRENT_SERVER if server is None else server)
    if name == DEFAULT_SERVER:
        return config_dir() / "sessions"
    return config_dir() / "servers" / name / "sessions"


def _load_config() -> tuple[Path, dict]:
    cfg, _ = ensure_config()
    try:
        return cfg, tomllib.loads(cfg.read_text())
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"Invalid config TOML {cfg}: {e}") from e


def load_prefix() -> str:
    cfg, data = _load_config()
    prefix = data.get("prefix", DEFAULT_PREFIX)
    if not isinstance(prefix, str) or not prefix or not prefix.isprintable() or any(char.isspace() for char in prefix):
        raise SystemExit(f"Invalid config {cfg}: prefix must be a non-empty, printable, whitespace-free string")
    return prefix


def load_sidebar_width() -> int:
    cfg, data = _load_config()
    width = data.get("sidebar_width", DEFAULT_SIDEBAR_WIDTH)
    if isinstance(width, bool) or not isinstance(width, int) or width < 1:
        raise SystemExit(f"Invalid config {cfg}: sidebar_width must be a positive integer")
    return width


def load_status_timeout() -> int:
    cfg, data = _load_config()
    timeout = data.get("status_timeout", DEFAULT_STATUS_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise SystemExit(f"Invalid config {cfg}: status_timeout must be a positive integer")
    return timeout


def load_persistent_ssh() -> bool:
    cfg, data = _load_config()
    persistent = data.get("persistent_ssh", DEFAULT_PERSISTENT_SSH)
    if not isinstance(persistent, bool):
        raise SystemExit(f"Invalid config {cfg}: persistent_ssh must be a boolean")
    return persistent


def load_hosts() -> list[str]:
    cfg, data = _load_config()
    hosts = data.get("hosts", [])
    if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
        raise SystemExit(f"Invalid config {cfg}: hosts must be a list of strings")
    try:
        return [validate_host(host) for host in hosts]
    except SystemExit as error:
        raise SystemExit(f"Invalid config {cfg}: {error}") from error


def load_sessions(server: str | None = None) -> list[Target]:
    path = sessions_path(server)
    if not path.exists():
        return []
    favorites: list[Target] = []
    seen: set[Target] = set()
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        if not (text := line.strip()):
            continue
        try:
            target = parse_target(text)
        except SystemExit as e:
            raise SystemExit(f"Invalid favorite in {path}:{line_number}: {e}") from e
        if target not in seen:
            favorites.append(target)
            seen.add(target)
    return favorites


def save_sessions(favorites: list[Target] | tuple[Target, ...], server: str | None = None) -> None:
    path = sessions_path(server)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{target.format()}\n" for target in favorites))


def replace_session(old: Target, new: Target, server: str | None = None) -> list[Target]:
    replaced: list[Target] = []
    seen: set[Target] = set()
    for target in load_sessions(server):
        candidate = new if target == old else target
        if candidate not in seen:
            replaced.append(candidate)
            seen.add(candidate)
    save_sessions(replaced, server)
    return replaced
