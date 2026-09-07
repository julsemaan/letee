from __future__ import annotations

import argparse
from pathlib import Path
import os
import stat
import subprocess
import sys

from . import config, cockpit, sessions, tmux
from .config import ensure_config, load_sessions, save_sessions
from .discovery import discover
from .names import (
    DEFAULT_SERVER,
    Target,
    legacy_server_socket,
    normalize_server,
    parse_target,
    server_socket,
    validate_server,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="letee")
    parser.add_argument("-L", dest="server", metavar="SERVER", help="select named letee server")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("cockpit", help="launch or attach cockpit")
    sub.add_parser("sidebar", help=argparse.SUPPRESS)
    focus_sidebar = sub.add_parser("focus-sidebar", help="focus/open cockpit sidebar")
    focus_sidebar.add_argument(
        "region",
        nargs="?",
        choices=("sessions", "agents", "add", "remove", "kill", "alert"),
        default="sessions",
    )
    sub.add_parser("init", help="create missing config files")
    sub.add_parser("list", help="list discovered targets")
    sub.add_parser("list-servers", help="list running letee servers")
    sub.add_parser("kill-server", help="kill selected letee server")

    switch = sub.add_parser("switch", help="switch cockpit target")
    switch.add_argument("target")

    switch_star = sub.add_parser("switch-session", help="switch to numbered tracked target")
    switch_star.add_argument("slot", type=int, choices=range(1, 10))

    kill_parser = sub.add_parser("kill", help="kill target tmux session")
    kill_parser.add_argument("target")

    rename_parser = sub.add_parser("rename", help="rename target tmux session")
    rename_parser.add_argument("target")
    rename_parser.add_argument("new_name")

    create = sub.add_parser("create", help="create target then switch")
    create_sub = create.add_subparsers(dest="create_kind", required=True)
    local = create_sub.add_parser("local", help="create local tmux session")
    local.add_argument("session")
    ssh = create_sub.add_parser("ssh", help="create remote tmux session")
    ssh.add_argument("host")
    ssh.add_argument("session")
    return parser


def _tmux_socket_dir() -> Path:
    root = Path(os.environ.get("TMUX_TMPDIR") or "/tmp").expanduser()
    return root / f"tmux-{os.getuid()}"


def _run_server_tmux(server: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [tmux.tmux_executable(), "-L", server_socket(server), *args],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )


def _run_legacy_tmux(socket_name: str, *args: str) -> subprocess.CompletedProcess[str]:
    # Legacy servers were created by the tmux found on PATH.
    return subprocess.run(
        ["tmux", "-L", socket_name, *args],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )


def _legacy_socket_present(server: str) -> bool:
    path = _tmux_socket_dir() / legacy_server_socket(server)
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return False
    return stat.S_ISSOCK(mode)


def _cleanup_legacy_server(server: str) -> None:
    if not _legacy_socket_present(server):
        return
    socket_name = legacy_server_socket(server)
    try:
        marker = _run_legacy_tmux(socket_name, "show-options", "-v", "-t", tmux.SESSION, "@letee_cockpit")
        if marker.returncode != 0 or (marker.stdout or "").strip() != "1":
            return
        _run_legacy_tmux(socket_name, "kill-server")
    except (OSError, subprocess.SubprocessError):
        return


def _server_attached(server: str) -> bool | None:
    try:
        marker = _run_server_tmux(server, "show-options", "-v", "-t", tmux.SESSION, "@letee_cockpit")
        if marker.returncode != 0 or (marker.stdout or "").strip() != "1":
            return None
        clients = _run_server_tmux(server, "list-clients", "-t", tmux.SESSION, "-F", "#{client_name}")
    except (OSError, subprocess.SubprocessError):
        return None
    if clients.returncode != 0:
        return None
    return bool(clients.stdout.strip())


def list_servers() -> list[tuple[str, bool]]:
    try:
        paths = sorted(_tmux_socket_dir().iterdir(), key=lambda path: path.name)
    except OSError:
        return []
    servers: list[tuple[str, bool]] = []
    current_socket = server_socket(DEFAULT_SERVER)
    legacy_socket = legacy_server_socket(DEFAULT_SERVER)
    for path in paths:
        if path.name == current_socket:
            server = DEFAULT_SERVER
        elif path.name.startswith(f"{current_socket}-"):
            try:
                server = validate_server(path.name.removeprefix(f"{current_socket}-"))
            except SystemExit:
                continue
            if server == DEFAULT_SERVER:
                continue
        else:
            if path.name == legacy_socket:
                _cleanup_legacy_server(DEFAULT_SERVER)
            elif path.name.startswith(f"{legacy_socket}-"):
                try:
                    legacy_server = validate_server(path.name.removeprefix(f"{legacy_socket}-"))
                except SystemExit:
                    continue
                if legacy_server != DEFAULT_SERVER:
                    _cleanup_legacy_server(legacy_server)
            continue
        attached = _server_attached(server)
        if attached is not None:
            servers.append((server, attached))
    return sorted(servers, key=lambda item: (item[0] != DEFAULT_SERVER, item[0]))


def _configure_server(server: str | None) -> None:
    selected = config.set_server(server)
    tmux.set_server(selected)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    _configure_server(args.server)
    if args.command == "sidebar":
        from .sidebar import main as sidebar_main
        return sidebar_main()
    if args.command in (None, "cockpit"):
        _cleanup_legacy_server(tmux.SERVER)
        return cockpit.cockpit()
    if args.command == "init":
        cfg, wrapper = ensure_config()
        print(f"Config: {cfg}")
        print(f"Wrapper: {wrapper}")
        return 0
    if args.command == "focus-sidebar":
        return cockpit.focus_sidebar(args.region)
    if args.command == "list-servers":
        for server, attached in list_servers():
            print(f"{server} ({'attached' if attached else 'detached'})")
        return 0
    if args.command == "kill-server":
        server = normalize_server(args.server)
        if _server_attached(server) is None:
            raise SystemExit(f"Not a running letee server: {server}")
        result = _run_server_tmux(server, "kill-server")
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode,
                [tmux.tmux_executable(), "-L", server_socket(server), "kill-server"],
                result.stdout,
                result.stderr,
            )
        return 0
    if args.command == "list":
        snapshot = discover()
        if not snapshot.local.available:
            print(f"local unavailable: {snapshot.local.error or 'unknown error'}")
        else:
            for target in snapshot.local.sessions:
                print(target.format())
        for host, source in snapshot.remotes.items():
            if not source or not source.available:
                print(f"ssh:{host} unavailable")
            else:
                for target in source.sessions:
                    print(target.format())
        return 0
    if args.command == "switch":
        target = parse_target(args.target)
        cockpit.require_cockpit()
        cockpit.switch(target, sessions.attach_command(target))
        return 0
    if args.command == "switch-session":
        favorites = load_sessions()
        if args.slot > len(favorites):
            raise SystemExit(f"No session in slot {args.slot}")
        target = favorites[args.slot - 1]
        cockpit.require_cockpit()
        cockpit.switch(target, sessions.attach_command(target))
        return 0
    if args.command == "kill":
        target = parse_target(args.target)
        sessions.kill(target)
        favorites = load_sessions()
        if target in favorites:
            favorites.remove(target)
            save_sessions(favorites)
        return 0
    if args.command == "rename":
        target = parse_target(args.target)
        renamed = Target(target.kind, args.new_name, target.host)
        if target not in load_sessions():
            raise SystemExit(f"Session not tracked: {target.format()}")
        renamed = sessions.rename(target, renamed.session)
        config.replace_session(target, renamed)
        return 0
    if args.command == "create":
        target = Target("local", args.session) if args.create_kind == "local" else Target("ssh", args.session, args.host)
        sessions.create(target)
        cockpit.require_cockpit()
        cockpit.switch(target, sessions.attach_command(target))
        return 0


def run_cli(argv: list[str] | None = None) -> int:
    try:
        return main(argv)
    except subprocess.CalledProcessError as error:
        command = Path(str(error.cmd[0] if isinstance(error.cmd, (list, tuple)) else error.cmd)).name
        reason = (error.stderr or error.stdout or "").strip() or f"exit status {error.returncode}"
        print(f"letee: {command} failed: {reason}", file=sys.stderr)
    except OSError as error:
        reason = error.strerror or str(error)
        detail = f"{error.filename}: {reason}" if error.filename else reason
        print(f"letee: {detail}", file=sys.stderr)
    except UnicodeError as error:
        print(f"letee: text decoding failed: {error}", file=sys.stderr)
    except subprocess.SubprocessError as error:
        print(f"letee: subprocess failed: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(run_cli())
