from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

from . import cockpit, sessions
from .config import ensure_config, load_sessions
from .discovery import discover
from .names import Target, parse_target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mtmux")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("cockpit", help="launch or attach cockpit")
    sub.add_parser("focus-sidebar", help="focus/open cockpit sidebar")
    sub.add_parser("init", help="create missing config files")
    sub.add_parser("list", help="list discovered targets")

    switch = sub.add_parser("switch", help="switch cockpit target")
    switch.add_argument("target")

    switch_star = sub.add_parser("switch-session", help="switch to numbered tracked target")
    switch_star.add_argument("slot", type=int, choices=range(1, 10))

    kill_parser = sub.add_parser("kill", help="kill target tmux session")
    kill_parser.add_argument("target")

    create = sub.add_parser("create", help="create target then switch")
    create_sub = create.add_subparsers(dest="create_kind", required=True)
    local = create_sub.add_parser("local", help="create local tmux session")
    local.add_argument("session")
    ssh = create_sub.add_parser("ssh", help="create remote tmux session")
    ssh.add_argument("host")
    ssh.add_argument("session")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["sidebar"]:
        from .sidebar import main as sidebar_main
        return sidebar_main()
    args = build_parser().parse_args(argv)
    if args.command == "init":
        cfg, wrapper = ensure_config()
        print(f"Config: {cfg}")
        print(f"Wrapper: {wrapper}")
        return 0
    if args.command == "cockpit":
        return cockpit.cockpit()
    if args.command == "focus-sidebar":
        return cockpit.focus_sidebar()
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
        cockpit.switch(target, sessions.attach_command(target))
        return 0
    if args.command == "switch-session":
        favorites = load_sessions()
        if args.slot > len(favorites):
            raise SystemExit(f"No session in slot {args.slot}")
        target = favorites[args.slot - 1]
        cockpit.switch(target, sessions.attach_command(target))
        return 0
    if args.command == "kill":
        sessions.kill(parse_target(args.target))
        return 0
    if args.command == "create":
        target = Target("local", args.session) if args.create_kind == "local" else Target("ssh", args.session, args.host)
        sessions.create(target)
        cockpit.switch(target, sessions.attach_command(target))
        return 0


def run_cli(argv: list[str] | None = None) -> int:
    try:
        return main(argv)
    except subprocess.CalledProcessError as error:
        command = Path(str(error.cmd[0] if isinstance(error.cmd, (list, tuple)) else error.cmd)).name
        reason = (error.stderr or error.stdout or "").strip() or f"exit status {error.returncode}"
        print(f"mtmux: {command} failed: {reason}", file=sys.stderr)
    except OSError as error:
        reason = error.strerror or str(error)
        detail = f"{error.filename}: {reason}" if error.filename else reason
        print(f"mtmux: {detail}", file=sys.stderr)
    except UnicodeError as error:
        print(f"mtmux: text decoding failed: {error}", file=sys.stderr)
    except subprocess.SubprocessError as error:
        print(f"mtmux: subprocess failed: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(run_cli())
