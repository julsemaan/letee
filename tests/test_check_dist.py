import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo

from tools import check_dist


class CheckDistWheelTest(unittest.TestCase):
    def _stage_binaries(self, root: Path, contents: dict[str, bytes]) -> None:
        for name, content in contents.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o755)
        for name in check_dist.LICENSE_PATHS:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"license")
        provenance = root / check_dist.PROVENANCE_PATH
        provenance.parent.mkdir(parents=True, exist_ok=True)
        source = "https://example.invalid/tmux-builds"
        provenance.write_text(
            json.dumps(
                {
                    "tmux_version": check_dist.TMUX_VERSION,
                    "source": source,
                    "release": f"{source}/releases/tag/v{check_dist.TMUX_VERSION}",
                    "artifacts": {
                        name: {
                            "url": f"{source}/releases/download/v{check_dist.TMUX_VERSION}/{name}",
                            "sha256": "f" * 64,
                        }
                        for name in (
                            f"tmux-{check_dist.TMUX_VERSION}-linux-x86_64.tar.gz",
                            f"tmux-{check_dist.TMUX_VERSION}-linux-arm64.tar.gz",
                            f"tmux-{check_dist.TMUX_VERSION}-macos-x86_64.tar.gz",
                            f"tmux-{check_dist.TMUX_VERSION}-macos-arm64.tar.gz",
                            "LICENSES.tar.gz",
                        )
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    def _write_wheel(
        self,
        path: Path,
        contents: dict[str, bytes],
        *,
        duplicate_name: str | None = None,
        provenance: bytes | None = None,
        member_prefix: str = "",
        license_contents: dict[str, bytes] | None = None,
    ) -> None:
        if provenance is None:
            provenance = (path.parent / check_dist.PROVENANCE_PATH).read_bytes()
        if license_contents is None:
            license_contents = {name: b"license" for name in check_dist.LICENSE_PATHS}
        with ZipFile(path, "w") as archive:
            for name in check_dist.BINARY_PATHS:
                info = ZipInfo(f"{member_prefix}{name}")
                info.external_attr = 0o755 << 16
                archive.writestr(info, contents[name])
            for name in check_dist.LICENSE_PATHS:
                archive.writestr(f"{member_prefix}{name}", license_contents[name])
            archive.writestr(f"{member_prefix}{check_dist.PROVENANCE_PATH}", provenance)
            if duplicate_name is not None:
                info = ZipInfo(duplicate_name)
                info.external_attr = 0o755 << 16
                archive.writestr(info, contents[duplicate_name])

    def test_check_wheel_rejects_prefixed_member_paths(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            contents = {name: b"tmux" for name in check_dist.BINARY_PATHS}
            self._stage_binaries(root, contents)
            path = root / "letee.whl"
            self._write_wheel(path, contents, member_prefix="wrong/")

            with patch.object(check_dist, "PROJECT_ROOT", root):
                with self.assertRaisesRegex(ValueError, "missing or duplicates"):
                    check_dist._check_wheel(path)

    def _write_sdist(
        self,
        path: Path,
        contents: dict[str, bytes],
        *,
        duplicate_name: str | None = None,
        provenance: bytes | None = None,
        license_contents: dict[str, bytes] | None = None,
    ) -> None:
        if provenance is None:
            provenance = (path.parent / check_dist.PROVENANCE_PATH).read_bytes()
        if license_contents is None:
            license_contents = {name: b"license" for name in check_dist.LICENSE_PATHS}
        with tarfile.open(path, "w:gz") as archive:
            root = "letee-0.0"
            for name in check_dist.BINARY_PATHS:
                content = contents[name]
                info = tarfile.TarInfo(f"{root}/{name}")
                info.mode = 0o755
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            for name in check_dist.LICENSE_PATHS:
                content = license_contents[name]
                info = tarfile.TarInfo(f"{root}/{name}")
                info.mode = 0o644
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            info = tarfile.TarInfo(f"{root}/{check_dist.PROVENANCE_PATH}")
            info.mode = 0o644
            info.size = len(provenance)
            archive.addfile(info, io.BytesIO(provenance))
            if duplicate_name is not None:
                content = contents[duplicate_name]
                info = tarfile.TarInfo(f"{root}/{duplicate_name}")
                info.mode = 0o755
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))

    def test_check_wheel_rejects_duplicate_member_names(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            contents = {name: b"tmux" for name in check_dist.BINARY_PATHS}
            self._stage_binaries(root, contents)
            duplicate = next(iter(check_dist.BINARY_PATHS))
            path = root / "letee.whl"
            self._write_wheel(path, contents, duplicate_name=duplicate)

            with patch.object(check_dist, "PROJECT_ROOT", root):
                with self.assertRaisesRegex(ValueError, "duplicate"):
                    check_dist._check_wheel(path)

    def test_check_sdist_rejects_duplicate_member_names(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            contents = {name: b"tmux" for name in check_dist.BINARY_PATHS}
            self._stage_binaries(root, contents)
            duplicate = next(iter(check_dist.BINARY_PATHS))
            path = root / "letee.tar.gz"
            self._write_sdist(path, contents, duplicate_name=duplicate)

            with patch.object(check_dist, "PROJECT_ROOT", root):
                with self.assertRaisesRegex(ValueError, "duplicate"):
                    check_dist._check_sdist(path)

    def test_check_dist_rejects_tampered_binary_bytes(self):
        for distribution in ("wheel", "sdist"):
            with self.subTest(distribution=distribution):
                with tempfile.TemporaryDirectory() as tempdir:
                    root = Path(tempdir)
                    contents = {
                        name: f"tmux binary {name}".encode()
                        for name in check_dist.BINARY_PATHS
                    }
                    self._stage_binaries(root, contents)
                    tampered = next(iter(check_dist.BINARY_PATHS))
                    packaged = dict(contents)
                    packaged[tampered] = b"tampered"

                    if distribution == "wheel":
                        path = root / "letee.whl"
                        self._write_wheel(path, packaged)
                        checker = check_dist._check_wheel
                    else:
                        path = root / "letee.tar.gz"
                        self._write_sdist(path, packaged)
                        checker = check_dist._check_sdist

                    with patch.object(check_dist, "PROJECT_ROOT", root):
                        with self.assertRaisesRegex(
                            ValueError, "does not match staged vendor binary"
                        ):
                            checker(path)

    def test_check_dist_rejects_tampered_license_bytes(self):
        for distribution in ("wheel", "sdist"):
            for license_content in (b"tampered", b""):
                with self.subTest(
                    distribution=distribution, license_content=license_content
                ):
                    with tempfile.TemporaryDirectory() as tempdir:
                        root = Path(tempdir)
                        contents = {name: b"tmux" for name in check_dist.BINARY_PATHS}
                        self._stage_binaries(root, contents)
                        tampered = next(iter(check_dist.LICENSE_PATHS))
                        license_contents = {
                            name: b"license" for name in check_dist.LICENSE_PATHS
                        }
                        license_contents[tampered] = license_content

                        if distribution == "wheel":
                            path = root / "letee.whl"
                            self._write_wheel(
                                path, contents, license_contents=license_contents
                            )
                            checker = check_dist._check_wheel
                        else:
                            path = root / "letee.tar.gz"
                            self._write_sdist(
                                path, contents, license_contents=license_contents
                            )
                            checker = check_dist._check_sdist

                        with patch.object(check_dist, "PROJECT_ROOT", root):
                            with self.assertRaisesRegex(
                                ValueError, "does not match staged license notice"
                            ):
                                checker(path)

    def test_check_dist_rejects_incomplete_or_tampered_provenance(self):
        for distribution in ("wheel", "sdist"):
            for mutation in ("missing_source", "altered_url", "altered_sha256"):
                with self.subTest(distribution=distribution, mutation=mutation):
                    with tempfile.TemporaryDirectory() as tempdir:
                        root = Path(tempdir)
                        contents = {name: b"tmux" for name in check_dist.BINARY_PATHS}
                        self._stage_binaries(root, contents)
                        metadata = json.loads(
                            (root / check_dist.PROVENANCE_PATH).read_text()
                        )
                        if mutation == "missing_source":
                            del metadata["source"]
                        else:
                            artifact = next(iter(metadata["artifacts"].values()))
                            artifact["url" if mutation == "altered_url" else "sha256"] = (
                                "https://example.invalid/tmux.tar.gz"
                                if mutation == "altered_url"
                                else "0" * 64
                            )
                        provenance = json.dumps(metadata).encode()

                        if distribution == "wheel":
                            path = root / "letee.whl"
                            self._write_wheel(path, contents, provenance=provenance)
                            checker = check_dist._check_wheel
                        else:
                            path = root / "letee.tar.gz"
                            self._write_sdist(path, contents, provenance=provenance)
                            checker = check_dist._check_sdist

                        with patch.object(check_dist, "PROJECT_ROOT", root):
                            with self.assertRaisesRegex(
                                ValueError, "does not match staged provenance"
                            ):
                                checker(path)


class CheckDistHostBinaryTest(unittest.TestCase):
    def test_skips_unsupported_linux_architecture(self):
        with (
            patch.object(check_dist.platform, "system", return_value="Linux"),
            patch.object(check_dist.platform, "machine", return_value="s390x"),
            patch("letee.tmux.bundled_tmux_path", return_value=None),
        ):
            check_dist._check_host_binary()


if __name__ == "__main__":
    unittest.main()
