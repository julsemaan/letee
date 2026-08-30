from __future__ import annotations

import os
import re
from pathlib import Path
import tomllib

from .names import DEFAULT_SERVER, Target, normalize_server, parse_target, validate_host

DEFAULT_PREFIX = "C-s"
DEFAULT_SIDEBAR_WIDTH = 40
DEFAULT_STATUS_TIMEOUT = 5
DEFAULT_AGENT_PANEL_RESIZE_STEP = 5
DEFAULT_PERSISTENT_SSH = True
DEFAULT_TMUX_CONFIG_OVERLAY = True
CONFIG_TEXT = f'hosts = []\nprefix = "{DEFAULT_PREFIX}"\nsidebar_width = {DEFAULT_SIDEBAR_WIDTH}\nstatus_timeout = {DEFAULT_STATUS_TIMEOUT}\nagent_panel_resize_step = {DEFAULT_AGENT_PANEL_RESIZE_STEP}\npersistent_ssh = true\ntmux_config_overlay = true\n'

DEFAULT_KEYBINDINGS: dict[str, str] = {
    "focus_agents": "prefix+a",
    "focus_sessions": "prefix+s",
    "add_session": "prefix++",
    "remove_active": "prefix+r",
    "kill_active": "prefix+x",
    "jump_alert": "prefix+!",
    "focus_right": "prefix+w",
    "toggle_sidebar": "prefix+h",
    "quit": "prefix+q",
    "help": "prefix+?",
    "detach": "prefix+d",
}
DEFAULT_SIDEBAR_KEYBINDINGS: dict[str, str] = {
    "navigate_down": "j",
    "navigate_up": "k",
    "rename": "e",
    "remove": "r",
    "kill": "x",
    "move_up": "K",
    "move_down": "J",
    "resize_inc": "[",
    "resize_dec": "]",
}
# ponytail: simple regex covers common tmux tokens; extend only if real configs need Space/Enter etc.
_TMUX_TOKEN_RE = re.compile(r"^(?:(?:C|M|S)-)*(?:[A-Za-z0-9]|[!@#$%^&*()_+\-=\[\]{};':\",./<>?`~|\\]|F[0-9]{1,2})$")
_RESERVED_SLOTS = {str(n) for n in range(1, 10)}
_KEYBINDING_ALIASES = {
    "agents": "focus_agents",
    "sessions": "focus_sessions",
    "add": "add_session",
    "remove": "remove_active",
    "kill": "kill_active",
    "alert": "jump_alert",
    "right": "focus_right",
    "toggle": "toggle_sidebar",
    "hide": "toggle_sidebar",
    "hide_sidebar": "toggle_sidebar",
}
_SIDEBAR_ALIASES = {
    "down": "navigate_down",
    "up": "navigate_up",
    "reorder_up": "move_up",
    "reorder_down": "move_down",
    "increase": "resize_inc",
    "decrease": "resize_dec",
    "resize_increase": "resize_inc",
    "resize_decrease": "resize_dec",
}
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


def _validate_tmux_token(token: str) -> bool:
    return bool(token and token.isprintable() and not any(c.isspace() for c in token) and _TMUX_TOKEN_RE.match(token))


def _split_prefix(value: str) -> tuple[bool, str]:
    if len(value) >= 7 and value[:7].lower() == "prefix+":
        return True, value[7:]
    return False, value


def _effective_token(value: str, *, sidebar: bool) -> str:
    if sidebar:
        return value
    _, eff = _split_prefix(value)
    return eff


def _load_keybindings_block(data: dict, cfg: Path, prefix: str, block_name: str, defaults: dict[str, str], *, sidebar: bool) -> dict[str, str]:
    raw = data.get(block_name, {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SystemExit(f"Invalid config {cfg}: [{block_name}] must be a table")
    aliases = _SIDEBAR_ALIASES if sidebar else _KEYBINDING_ALIASES
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        canon = aliases.get(key, key)
        if canon not in defaults:
            raise SystemExit(f"Invalid config {cfg}: unknown {block_name} action {key!r}")
        if canon in normalized:
            raise SystemExit(f"Invalid config {cfg}: duplicate {block_name} action {canon!r} via alias {key!r}")
        normalized[canon] = value
    raw = normalized
    merged: dict[str, str] = {**defaults}
    for action, value in raw.items():
        if not isinstance(value, str):
            kind = "single-character key" if sidebar else "tmux key token"
            raise SystemExit(f"Invalid config {cfg}: {block_name}.{action} must be a {kind}")
        if sidebar:
            if len(value) != 1 or not value.isprintable() or value.isspace() or not value.isascii():
                raise SystemExit(f"Invalid config {cfg}: {block_name}.{action} must be a single-character ASCII key")
            eff = _effective_token(value, sidebar=True)
            if eff in _RESERVED_SLOTS:
                raise SystemExit(f"Invalid config {cfg}: {block_name}.{action} {value!r} is reserved (session slots 1 through 9)")
        else:
            has_pref, eff = _split_prefix(value)
            # Plain tokens bind globally via `bind-key -n`; only `prefix+...` values use tmux's prefix table.
            # Validation uses the effective token after removing the optional `prefix+`.
            if not eff:
                raise SystemExit(f"Invalid config {cfg}: {block_name}.{action} {value!r} is not a valid tmux key token")
            if not _validate_tmux_token(eff):
                raise SystemExit(f"Invalid config {cfg}: {block_name}.{action} {value!r} is not a valid tmux key token")
            if eff in _RESERVED_SLOTS:
                raise SystemExit(f"Invalid config {cfg}: {block_name}.{action} {value!r} is reserved (session slots 1 through 9)")
            if eff == prefix:
                raise SystemExit(f"Invalid config {cfg}: {block_name}.{action} {value!r} conflicts with prefix {prefix!r}")
        merged[action] = value
    # duplicate bindings: normalized, table-sensitive (prefix+ lowercased; `a` vs `prefix+a` remain distinct)
    seen: dict[tuple[bool, str], str] = {}
    for action, token in merged.items():
        has_pref, eff = _split_prefix(token) if not sidebar else (False, token)
        identity = (has_pref, eff)
        if identity in seen:
            raise SystemExit(f"Invalid config {cfg}: duplicate binding {token!r} for {seen[identity]!r} and {action!r} in [{block_name}]")
        seen[identity] = action
    # prefix conflicts for defaults that equal prefix even if not overridden
    if not sidebar:
        for action, token in merged.items():
            _, eff = _split_prefix(token)
            if eff == prefix and action not in raw:
                raise SystemExit(f"Invalid config {cfg}: {block_name}.{action} {token!r} conflicts with prefix {prefix!r}")
    return merged


def load_keybindings() -> dict[str, str]:
    cfg, data = _load_config()
    prefix = data.get("prefix", DEFAULT_PREFIX)
    if not isinstance(prefix, str) or not prefix or not prefix.isprintable() or any(char.isspace() for char in prefix):
        raise SystemExit(f"Invalid config {cfg}: prefix must be a non-empty, printable, whitespace-free string")
    return _load_keybindings_block(data, cfg, prefix, "keybindings", DEFAULT_KEYBINDINGS, sidebar=False)


def load_sidebar_keybindings() -> dict[str, str]:
    cfg, data = _load_config()
    prefix = data.get("prefix", DEFAULT_PREFIX)
    if not isinstance(prefix, str) or not prefix or not prefix.isprintable() or any(char.isspace() for char in prefix):
        raise SystemExit(f"Invalid config {cfg}: prefix must be a non-empty, printable, whitespace-free string")
    return _load_keybindings_block(data, cfg, prefix, "sidebar_keybindings", DEFAULT_SIDEBAR_KEYBINDINGS, sidebar=True)


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


def load_agent_panel_resize_step() -> int:
    cfg, data = _load_config()
    step = data.get("agent_panel_resize_step", DEFAULT_AGENT_PANEL_RESIZE_STEP)
    if isinstance(step, bool) or not isinstance(step, int) or not 1 <= step <= 100:
        raise SystemExit(
            f"Invalid config {cfg}: agent_panel_resize_step must be an integer from 1 through 100"
        )
    return step


def load_persistent_ssh() -> bool:
    cfg, data = _load_config()
    persistent = data.get("persistent_ssh", DEFAULT_PERSISTENT_SSH)
    if not isinstance(persistent, bool):
        raise SystemExit(f"Invalid config {cfg}: persistent_ssh must be a boolean")
    return persistent


def load_tmux_config_overlay() -> bool:
    cfg, data = _load_config()
    overlay = data.get("tmux_config_overlay", DEFAULT_TMUX_CONFIG_OVERLAY)
    if not isinstance(overlay, bool):
        raise SystemExit(f"Invalid config {cfg}: tmux_config_overlay must be a boolean")
    return overlay


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
