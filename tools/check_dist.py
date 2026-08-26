"""Check that Python distributions contain the pinned tmux bundle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
from zipfile import BadZipFile, ZipFile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TMUX_VERSION = "3.6a"
BINARY_PATHS = frozenset(
    {
        "letee/_vendor/tmux/linux-x86_64/tmux",
        "letee/_vendor/tmux/linux-arm64/tmux",
        "letee/_vendor/tmux/macos-x86_64/tmux",
        "letee/_vendor/tmux/macos-arm64/tmux",
    }
)
LICENSE_PATHS = frozenset(
    f"letee/_vendor/tmux/licenses/{name}"
    for name in (
        "COPYRIGHT.musl",
        "LICENSE.utf8proc",
        "LICENSE.libevent",
        "COPYING.tmux",
        "COPYING.ncurses",
    )
)
PROVENANCE_PATH = "letee/_vendor/tmux/provenance.json"
RAW_ARCHIVES = frozenset(
    {
        "tmux-3.6a-linux-x86_64.tar.gz",
        "tmux-3.6a-linux-arm64.tar.gz",
        "tmux-3.6a-macos-x86_64.tar.gz",
        "tmux-3.6a-macos-arm64.tar.gz",
        "LICENSES.tar.gz",
    }
)


def _member_for_suffix(names: set[str], suffix: str) -> str:
    matches = [name for name in names if name == suffix or name.endswith(f"/{suffix}")]
    if len(matches) != 1:
        raise ValueError(f"distribution is missing or duplicates {suffix}")
    return matches[0]


def _reject_raw_archives(names: set[str]) -> None:
    for name in names:
        if (
            Path(name).name in RAW_ARCHIVES
            or name.startswith("release-assets/")
            or "/release-assets/" in name
        ):
            raise ValueError(f"raw tmux archive in distribution: {name}")


def _check_provenance(content: bytes, label: str) -> None:
    try:
        metadata = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid tmux provenance in {label}: {error}") from error
    if not isinstance(metadata, dict) or metadata.get("tmux_version") != TMUX_VERSION:
        raise ValueError(f"tmux provenance in {label} is not version {TMUX_VERSION}")


def _check_wheel(path: Path) -> None:
    try:
        with ZipFile(path) as archive:
            names = set(archive.namelist())
            _reject_raw_archives(names)
            for suffix in BINARY_PATHS:
                name = _member_for_suffix(names, suffix)
                info = archive.getinfo(name)
                if info.is_dir() or not ((info.external_attr >> 16) & 0o111):
                    raise ValueError(f"bundled binary is not executable in wheel: {name}")
            for suffix in LICENSE_PATHS:
                name = _member_for_suffix(names, suffix)
                if archive.getinfo(name).is_dir():
                    raise ValueError(f"license notice is a directory in wheel: {name}")
            _check_provenance(archive.read(_member_for_suffix(names, PROVENANCE_PATH)), str(path))
    except BadZipFile as error:
        raise ValueError(f"invalid wheel {path}: {error}") from error


def _check_sdist(path: Path) -> None:
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = {member.name: member for member in archive.getmembers()}
            _reject_raw_archives(set(members))
            for suffix in BINARY_PATHS:
                name = _member_for_suffix(set(members), suffix)
                member = members[name]
                if not member.isreg() or not member.mode & 0o111:
                    raise ValueError(f"bundled binary is not executable in source distribution: {name}")
            for suffix in LICENSE_PATHS:
                name = _member_for_suffix(set(members), suffix)
                if not members[name].isreg():
                    raise ValueError(f"license notice is not a regular file in source distribution: {name}")
            name = _member_for_suffix(set(members), PROVENANCE_PATH)
            extracted = archive.extractfile(members[name])
            if extracted is None:
                raise ValueError(f"cannot read provenance in {path}")
            _check_provenance(extracted.read(), str(path))
    except (OSError, tarfile.TarError) as error:
        raise ValueError(f"invalid source distribution {path}: {error}") from error


def _check_host_binary() -> None:
    from letee import tmux

    path = tmux.bundled_tmux_path()
    if path is None:
        system = platform.system().lower()
        if system == "darwin" and platform.mac_ver()[0].split(".", 1)[0].isdigit():
            if int(platform.mac_ver()[0].split(".", 1)[0]) < 15:
                print("Skipping host binary check on macOS before version 15")
                return
        if system not in {"linux", "darwin"}:
            print("Skipping host binary check on unsupported platform")
            return
        raise ValueError("host-compatible bundled tmux binary is missing or not executable")
    try:
        result = subprocess.run([str(path), "-V"], check=False, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"could not run host-compatible tmux: {error}") from error
    if result.returncode != 0 or result.stdout.strip() != f"tmux {TMUX_VERSION}":
        raise ValueError(f"host-compatible tmux did not report tmux {TMUX_VERSION}")


def check_dist(directory: Path) -> None:
    directory = Path(directory)
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("dist must contain exactly one wheel and one source distribution")
    _check_wheel(wheels[0])
    _check_sdist(sdists[0])
    _check_host_binary()
    print(f"Validated tmux {TMUX_VERSION} in {wheels[0].name} and {sdists[0].name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?", default=Path("dist"))
    args = parser.parse_args(argv)
    check_dist(args.directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
