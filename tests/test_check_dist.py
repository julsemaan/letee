import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile, ZipInfo

from tools import check_dist


class CheckDistWheelTest(unittest.TestCase):
    def test_check_wheel_rejects_duplicate_member_names(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "letee.whl"
            with ZipFile(path, "w") as archive:
                for name in check_dist.BINARY_PATHS:
                    info = ZipInfo(name)
                    info.external_attr = 0o755 << 16
                    archive.writestr(info, b"tmux")
                for name in check_dist.LICENSE_PATHS:
                    archive.writestr(name, b"license")
                archive.writestr(
                    check_dist.PROVENANCE_PATH,
                    b'{"tmux_version": "3.6a"}',
                )
                duplicate = next(iter(check_dist.BINARY_PATHS))
                info = ZipInfo(duplicate)
                info.external_attr = 0o755 << 16
                archive.writestr(info, b"tmux")

            with self.assertRaisesRegex(ValueError, "duplicate"):
                check_dist._check_wheel(path)


if __name__ == "__main__":
    unittest.main()
