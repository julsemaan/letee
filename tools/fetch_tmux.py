"""Fetch and stage the pinned tmux binaries used by letee's outer cockpit."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import tempfile
from urllib.request import urlopen

TMUX_VERSION = "3.8"
UPSTREAM_REPOSITORY = "https://github.com/tmux/tmux-builds"
RELEASE_URL = f"{UPSTREAM_REPOSITORY}/releases/tag/v{TMUX_VERSION}"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROVENANCE_FILENAME = "provenance.json"
EXPECTED_LICENSES = frozenset(
    {
        "COPYRIGHT.musl",
        "LICENSE.utf8proc",
        "LICENSE.libevent",
        "COPYING.tmux",
        "COPYING.ncurses",
    }
)


@dataclass(frozen=True)
class Artifact:
    filename: str
    url: str
    sha256: str
    vendor_path: str | None = None


_RELEASE_BASE = f"{UPSTREAM_REPOSITORY}/releases/download/v{TMUX_VERSION}"


def _artifact(filename: str, sha256: str, vendor_path: str | None = None) -> Artifact:
    return Artifact(filename, f"{_RELEASE_BASE}/{filename}", sha256, vendor_path)


def _binary_artifact(platform: str, sha256: str) -> Artifact:
    return _artifact(f"tmux-{TMUX_VERSION}-{platform}.tar.gz", sha256, f"{platform}/tmux")


BINARY_ARTIFACTS = (
    _binary_artifact("linux-x86_64", "147849ff18b62a8aa3d5d8f75dd07c3f9fdfad529f932236afdec838fc1d15e1"),
    _binary_artifact("linux-arm64", "f399311b1159705e69839eaf4f6f68c8b8540dc3a8538b87c30dedb9c3c7fc23"),
    _binary_artifact("macos-x86_64", "e7b92e51d9e9610ce80d1c477da1e6ff6a6398ea6b6b4d4ff7b19cf60f594555"),
    _binary_artifact("macos-arm64", "ec926bbf28ddf55d0dc7c9791d43e03e251ddab392d763f7fbb38d4b8408d051"),
)
LICENSE_ARTIFACT = _artifact(
    "LICENSES.tar.gz",
    "9c2c903970e8245e0c1abd20311534e6eb152bf2bde3e54af77821f09b81effc",
)
ARTIFACTS = BINARY_ARTIFACTS + (LICENSE_ARTIFACT,)


class ChecksumMismatch(ValueError):
    """An existing or downloaded archive does not match its pinned digest."""


def _is_regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _is_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _require_regular(path: Path, label: str) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        raise ValueError(f"Missing {label}: {path}") from None
    except OSError as error:
        raise ValueError(f"Cannot inspect {label}: {path}: {error}") from error
    if stat.S_ISLNK(mode):
        raise ValueError(f"{label} must not be a symlink: {path}")
    if not stat.S_ISREG(mode):
        raise ValueError(f"{label} must be a regular file: {path}")


def _require_directory(path: Path, label: str) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        raise ValueError(f"Missing {label}: {path}") from None
    except OSError as error:
        raise ValueError(f"Cannot inspect {label}: {path}: {error}") from error
    if stat.S_ISLNK(mode):
        raise ValueError(f"{label} must not be a symlink: {path}")
    if not stat.S_ISDIR(mode):
        raise ValueError(f"{label} must be a directory: {path}")


def _ensure_directory(path: Path, label: str) -> None:
    if os.path.lexists(path):
        _require_directory(path, label)
        return
    path.mkdir(parents=True)


def verify_sha256(path: Path, expected: str) -> None:
    """Verify a regular archive before any archive reader opens it."""
    _require_regular(path, "archive")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise ValueError(f"Cannot read archive: {path}: {error}") from error
    actual = digest.hexdigest()
    if actual != expected:
        raise ChecksumMismatch(
            f"SHA-256 mismatch for {path.name}: expected {expected}, got {actual}"
        )


def _member_name(name: str) -> str:
    if not name or "\\" in name:
        raise ValueError(f"unsafe archive member name: {name!r}")
    if name in (".", "./"):
        return "."
    if name.startswith("/") or name.startswith("./"):
        name = name[2:] if name.startswith("./") else name
    parts = PurePosixPath(name).parts
    if not parts or any(part in ("", ".", "..") for part in parts) or name.startswith("/"):
        raise ValueError(f"unsafe archive member name: {name!r}")
    return "/".join(parts)


def extract_archive(
    path: Path,
    expected_names: set[str] | frozenset[str],
    *,
    allow_root: bool = False,
) -> dict[str, bytes]:
    """Read exactly the expected regular files from a verified tar archive."""
    expected = frozenset(expected_names)
    members: dict[str, tarfile.TarInfo] = {}
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                name = _member_name(member.name)
                if name == ".":
                    if not allow_root or not member.isdir() or name in members:
                        raise ValueError(f"unexpected archive member: {member.name!r}")
                    members[name] = member
                    continue
                if not member.isreg():
                    raise ValueError(f"archive member is non-regular: {member.name!r}")
                if name not in expected:
                    raise ValueError(f"unexpected archive member: {member.name!r}")
                if name in members:
                    raise ValueError(f"duplicate archive member: {member.name!r}")
                members[name] = member
            if frozenset(members) - {"."} != expected:
                missing = sorted(expected - frozenset(members))
                raise ValueError(f"missing archive members: {', '.join(missing)}")
            result: dict[str, bytes] = {}
            for name in expected:
                extracted = archive.extractfile(members[name])
                if extracted is None:
                    raise ValueError(f"could not read archive member: {name}")
                result[name] = extracted.read()
            return result
    except (tarfile.TarError, OSError) as error:
        raise ValueError(f"invalid archive {path}: {error}") from error


def _expected_provenance() -> dict[str, object]:
    return {
        "tmux_version": TMUX_VERSION,
        "source": UPSTREAM_REPOSITORY,
        "release": RELEASE_URL,
        "artifacts": {
            artifact.filename: {
                "url": artifact.url,
                "sha256": artifact.sha256,
            }
            for artifact in ARTIFACTS
        },
    }


def _download(artifact: Artifact, destination: Path) -> None:
    if os.path.lexists(destination):
        _require_regular(destination, "archive")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{artifact.filename}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as output:
            with urlopen(artifact.url, timeout=60) as response:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
        verify_sha256(temporary, artifact.sha256)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _ensure_archive(artifact: Artifact, destination: Path) -> None:
    if os.path.lexists(destination):
        _require_regular(destination, "archive")
        try:
            verify_sha256(destination, artifact.sha256)
        except ChecksumMismatch:
            _download(artifact, destination)
            return
        return
    _download(artifact, destination)


def _read_verified_archive(artifact: Artifact, path: Path) -> dict[str, bytes]:
    verify_sha256(path, artifact.sha256)
    expected = {"tmux"} if artifact.vendor_path else EXPECTED_LICENSES
    return extract_archive(path, expected, allow_root=artifact.vendor_path is None)


def _vendor_matches(
    vendor_root: Path,
    archive_paths: dict[str, Path],
    archives: tuple[Artifact, ...],
) -> bool:
    if not _is_directory(vendor_root):
        return False
    metadata_path = vendor_root / PROVENANCE_FILENAME
    if not _is_regular(metadata_path):
        return False
    try:
        if json.loads(metadata_path.read_text()) != _expected_provenance():
            return False
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False

    try:
        for artifact in archives:
            contents = _read_verified_archive(artifact, archive_paths[artifact.filename])
            if artifact.vendor_path:
                destination = vendor_root / artifact.vendor_path
                _require_regular(destination, "bundled tmux binary")
                if not os.access(destination, os.X_OK) or destination.read_bytes() != contents["tmux"]:
                    return False
            else:
                licenses = vendor_root / "licenses"
                if not _is_directory(licenses):
                    return False
                try:
                    children = {child.name for child in licenses.iterdir()}
                except OSError:
                    return False
                if children != EXPECTED_LICENSES:
                    return False
                for name, content in contents.items():
                    destination = licenses / name
                    _require_regular(destination, "license notice")
                    if destination.read_bytes() != content:
                        return False
    except (ValueError, OSError):
        return False
    return True


def _write_file(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.chmod(path, mode)


def _replace_vendor(stage: Path, vendor_root: Path) -> None:
    parent = vendor_root.parent
    if os.path.lexists(vendor_root):
        _require_directory(vendor_root, "vendor directory")
        fd, backup_name = tempfile.mkstemp(prefix=f".{vendor_root.name}.old-", dir=parent)
        os.close(fd)
        backup = Path(backup_name)
        backup.unlink()
        os.replace(vendor_root, backup)
        try:
            os.replace(stage, vendor_root)
        except Exception:
            os.replace(backup, vendor_root)
            raise
        shutil.rmtree(backup)
    else:
        os.replace(stage, vendor_root)


def fetch(root: Path = PROJECT_ROOT) -> None:
    root = Path(root)
    release_dir = root / "release-assets"
    vendor_root = root / "letee" / "_vendor" / "tmux"
    _ensure_directory(release_dir, "release-assets directory")
    _ensure_directory(vendor_root.parent, "vendor parent directory")
    archives = tuple(ARTIFACTS)
    archive_paths = {artifact.filename: release_dir / artifact.filename for artifact in archives}

    if all(_is_regular(path) for path in archive_paths.values()) and _vendor_matches(vendor_root, archive_paths, archives):
        return

    for artifact in archives:
        _ensure_archive(artifact, archive_paths[artifact.filename])

    contents = {
        artifact.filename: _read_verified_archive(artifact, archive_paths[artifact.filename])
        for artifact in archives
    }
    with tempfile.TemporaryDirectory(prefix=".tmux-vendor-", dir=vendor_root.parent) as temporary_dir:
        stage = Path(temporary_dir) / "tmux"
        for artifact in archives:
            if artifact.vendor_path:
                _write_file(stage / artifact.vendor_path, contents[artifact.filename]["tmux"], 0o755)
            else:
                for name, content in contents[artifact.filename].items():
                    _write_file(stage / "licenses" / name, content, 0o644)
        metadata = json.dumps(_expected_provenance(), indent=2, sort_keys=True) + "\n"
        _write_file(stage / PROVENANCE_FILENAME, metadata.encode(), 0o644)
        _replace_vendor(stage, vendor_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=PROJECT_ROOT,
        help="project root (defaults to the repository containing this script)",
    )
    args = parser.parse_args(argv)
    fetch(args.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
