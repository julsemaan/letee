import hashlib
import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import fetch_tmux


class FetchTmuxArchiveTest(unittest.TestCase):
    def _write_archive(self, content):
        fd, name = tempfile.mkstemp(suffix=".tar.gz")
        os.close(fd)
        path = Path(name)
        self.addCleanup(path.unlink, missing_ok=True)
        path.write_bytes(content)
        return path

    def _archive(self, members):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for name, kind, data in members:
                info = tarfile.TarInfo(name)
                if kind == "file":
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
                elif kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = data.decode()
                    archive.addfile(info)
                elif kind == "directory":
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                else:
                    raise AssertionError(kind)
        return output.getvalue()

    def test_extract_archive_rejects_symlinks(self):
        path = self._write_archive(self._archive((("tmux", "symlink", b"/etc/passwd"),)))

        with self.assertRaisesRegex(ValueError, "non-regular"):
            fetch_tmux.extract_archive(path, {"tmux"})

    def test_extract_archive_rejects_traversal_and_extra_members(self):
        for member in ("../tmux", "/tmux", "extra"):
            with self.subTest(member=member):
                path = self._write_archive(self._archive(((member, "file", b"tmux"),)))

                with self.assertRaises(ValueError):
                    fetch_tmux.extract_archive(path, {"tmux"})

    def test_extract_archive_allows_license_root_directory_only(self):
        path = self._write_archive(
            self._archive(
                (
                    (".", "directory", b""),
                    ("./COPYING.tmux", "file", b"license"),
                )
            )
        )

        self.assertEqual(
            fetch_tmux.extract_archive(path, {"COPYING.tmux"}, allow_root=True),
            {"COPYING.tmux": b"license"},
        )


class FetchTmuxDownloadTest(unittest.TestCase):
    def _tar(self, files, *, root=False):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            if root:
                info = tarfile.TarInfo(".")
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            for name, content in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return output.getvalue()

    def test_checksum_is_verified_before_existing_archive_is_read(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "archive.tar.gz"
            path.write_bytes(b"not the pinned archive")

            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                fetch_tmux.verify_sha256(path, "0" * 64)

    def test_fetch_stages_vendor_and_rebuilds_damaged_outputs(self):
        binaries = {
            "linux-x86_64": b"linux-x86_64 tmux",
            "linux-arm64": b"linux-arm64 tmux",
            "macos-x86_64": b"macos-x86_64 tmux",
            "macos-arm64": b"macos-arm64 tmux",
        }
        artifacts = []
        payloads = {}
        for platform_name, binary in binaries.items():
            filename = f"{platform_name}.tar.gz"
            payload = self._tar({"tmux": binary})
            artifact = fetch_tmux.Artifact(
                filename,
                f"https://example.invalid/{filename}",
                hashlib.sha256(payload).hexdigest(),
                f"{platform_name}/tmux",
            )
            artifacts.append(artifact)
            payloads[artifact.url] = payload
        license_payload = self._tar(
            {name: name.encode() for name in fetch_tmux.EXPECTED_LICENSES}, root=True
        )
        license_artifact = fetch_tmux.Artifact(
            "licenses.tar.gz",
            "https://example.invalid/licenses.tar.gz",
            hashlib.sha256(license_payload).hexdigest(),
        )
        artifacts.append(license_artifact)
        payloads[license_artifact.url] = license_payload

        def open_url(url, timeout):
            self.assertEqual(timeout, 60)
            return io.BytesIO(payloads[url])

        with tempfile.TemporaryDirectory() as tempdir:
            with (
                patch.object(fetch_tmux, "urlopen", side_effect=open_url),
                patch.object(fetch_tmux, "ARTIFACTS", tuple(artifacts)),
            ):
                fetch_tmux.fetch(Path(tempdir))

            vendor = Path(tempdir) / "letee" / "_vendor" / "tmux"
            self.assertEqual((vendor / "linux-x86_64/tmux").read_bytes(), binaries["linux-x86_64"])
            self.assertEqual((vendor / "linux-arm64/tmux").read_bytes(), binaries["linux-arm64"])
            self.assertEqual((vendor / "macos-x86_64/tmux").read_bytes(), binaries["macos-x86_64"])
            self.assertEqual((vendor / "macos-arm64/tmux").read_bytes(), binaries["macos-arm64"])
            self.assertEqual((vendor / "licenses/COPYING.tmux").read_bytes(), b"COPYING.tmux")
            self.assertEqual((vendor / "linux-x86_64/tmux").stat().st_mode & 0o777, 0o755)

            with (
                patch.object(fetch_tmux, "urlopen", side_effect=AssertionError("complete set must not download")),
                patch.object(fetch_tmux, "ARTIFACTS", tuple(artifacts)),
            ):
                fetch_tmux.fetch(Path(tempdir))

            def rebuild_without_download():
                with (
                    patch.object(fetch_tmux, "urlopen", side_effect=AssertionError("damaged vendor must not download")),
                    patch.object(fetch_tmux, "ARTIFACTS", tuple(artifacts)),
                ):
                    fetch_tmux.fetch(Path(tempdir))

            binary = vendor / "linux-x86_64/tmux"
            binary.unlink()
            rebuild_without_download()
            self.assertEqual(binary.read_bytes(), binaries["linux-x86_64"])

            license_notice = vendor / "licenses/COPYING.tmux"
            license_notice.unlink()
            license_notice.mkdir()
            rebuild_without_download()
            self.assertEqual(license_notice.read_bytes(), b"COPYING.tmux")

            binary.write_bytes(b"damaged")
            with patch.object(Path, "read_bytes", side_effect=OSError("unreadable")):
                rebuild_without_download()
            self.assertEqual(binary.read_bytes(), binaries["linux-x86_64"])


if __name__ == "__main__":
    unittest.main()
