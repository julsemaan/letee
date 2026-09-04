import io
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

    def _write_wheel(
        self,
        path: Path,
        contents: dict[str, bytes],
        *,
        duplicate_name: str | None = None,
    ) -> None:
        with ZipFile(path, "w") as archive:
            for name in check_dist.BINARY_PATHS:
                info = ZipInfo(name)
                info.external_attr = 0o755 << 16
                archive.writestr(info, contents[name])
            for name in check_dist.LICENSE_PATHS:
                archive.writestr(name, b"license")
            archive.writestr(
                check_dist.PROVENANCE_PATH,
                b'{"tmux_version": "3.6a"}',
            )
            if duplicate_name is not None:
                info = ZipInfo(duplicate_name)
                info.external_attr = 0o755 << 16
                archive.writestr(info, contents[duplicate_name])

    def _write_sdist(
        self,
        path: Path,
        contents: dict[str, bytes],
        *,
        duplicate_name: str | None = None,
    ) -> None:
        with tarfile.open(path, "w:gz") as archive:
            root = "letee-0.0"
            for name in check_dist.BINARY_PATHS:
                content = contents[name]
                info = tarfile.TarInfo(f"{root}/{name}")
                info.mode = 0o755
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            for name in check_dist.LICENSE_PATHS:
                content = b"license"
                info = tarfile.TarInfo(f"{root}/{name}")
                info.mode = 0o644
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            content = b'{"tmux_version": "3.6a"}'
            info = tarfile.TarInfo(f"{root}/{check_dist.PROVENANCE_PATH}")
            info.mode = 0o644
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
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


if __name__ == "__main__":
    unittest.main()
