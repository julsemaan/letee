import importlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from letee.tmux import set_server, tmux


TMUX_MODULE = importlib.import_module("letee.tmux")


class TmuxTests(unittest.TestCase):
    def tearDown(self):
        set_server(None)

    def test_named_server_selects_prefixed_socket(self):
        set_server("work")
        with patch("letee.tmux.subprocess.run") as run:
            tmux("list-sessions")

        self.assertEqual(
            run.call_args.args[0],
            [TMUX_MODULE.tmux_executable(), "-L", "letee@v1-work", "list-sessions"],
        )

    def test_timeout_can_be_disabled_for_interactive_commands(self):
        with patch("letee.tmux.subprocess.run") as run:
            tmux("display-menu", timeout=None)

        run.assert_called_once_with(
            [TMUX_MODULE.tmux_executable(), "-L", "letee@v1", "display-menu"],
            text=True, capture_output=False, check=True, timeout=None,
        )

    def test_timeout_exits_with_command_without_chained_exception(self):
        with patch("letee.tmux.subprocess.run", side_effect=subprocess.TimeoutExpired(["tmux"], 5)):
            with self.assertRaises(SystemExit) as raised:
                tmux("list-sessions")

        self.assertEqual(
            str(raised.exception),
            f"{TMUX_MODULE.tmux_executable()} -L letee@v1 list-sessions timed out after 5 seconds",
        )
        self.assertIsNone(raised.exception.__cause__)


class TmuxResolverTests(unittest.TestCase):
    def test_linux_aliases_select_matching_bundled_binary(self):
        with tempfile.TemporaryDirectory() as tempdir:
            vendor = Path(tempdir)
            for name in ("linux-x86_64", "linux-arm64"):
                path = vendor / name / "tmux"
                path.parent.mkdir(parents=True)
                path.write_bytes(b"tmux")
                path.chmod(0o755)
            with (
                patch.object(TMUX_MODULE, "VENDOR_ROOT", vendor),
                patch.object(TMUX_MODULE.platform, "system", return_value="Linux"),
            ):
                with patch.object(TMUX_MODULE.platform, "machine", return_value="AMD64"):
                    self.assertEqual(
                        TMUX_MODULE.bundled_tmux_path(), vendor / "linux-x86_64" / "tmux"
                    )
                with patch.object(TMUX_MODULE.platform, "machine", return_value="aarch64"):
                    self.assertEqual(
                        TMUX_MODULE.bundled_tmux_path(), vendor / "linux-arm64" / "tmux"
                    )

    def test_macos_15_selects_bundled_binary(self):
        with tempfile.TemporaryDirectory() as tempdir:
            vendor = Path(tempdir)
            path = vendor / "macos-arm64" / "tmux"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"tmux")
            path.chmod(0o755)
            with (
                patch.object(TMUX_MODULE, "VENDOR_ROOT", vendor),
                patch.object(TMUX_MODULE.platform, "system", return_value="Darwin"),
                patch.object(TMUX_MODULE.platform, "machine", return_value="arm64"),
                patch.object(TMUX_MODULE.platform, "mac_ver", return_value=("15.0", (), "")),
            ):
                self.assertEqual(TMUX_MODULE.bundled_tmux_path(), path)

    def test_older_macos_and_unsupported_or_missing_vendor_fall_back_to_path(self):
        with tempfile.TemporaryDirectory() as tempdir:
            vendor = Path(tempdir)
            path = vendor / "macos-x86_64" / "tmux"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"tmux")
            path.chmod(0o755)
            with patch.object(TMUX_MODULE, "VENDOR_ROOT", vendor):
                with (
                    patch.object(TMUX_MODULE.platform, "system", return_value="Darwin"),
                    patch.object(TMUX_MODULE.platform, "machine", return_value="x86_64"),
                    patch.object(TMUX_MODULE.platform, "mac_ver", return_value=("14.6", (), "")),
                ):
                    self.assertIsNone(TMUX_MODULE.bundled_tmux_path())
                with (
                    patch.object(TMUX_MODULE.platform, "system", return_value="FreeBSD"),
                    patch.object(TMUX_MODULE.platform, "machine", return_value="x86_64"),
                ):
                    self.assertIsNone(TMUX_MODULE.bundled_tmux_path())
                path.unlink()
                with (
                    patch.object(TMUX_MODULE.platform, "system", return_value="Linux"),
                    patch.object(TMUX_MODULE.platform, "machine", return_value="x86_64"),
                ):
                    self.assertEqual(TMUX_MODULE.tmux_executable(), "tmux")

    def test_non_executable_vendor_binary_falls_back_to_path(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "linux-x86_64" / "tmux"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"tmux")
            path.chmod(0o644)
            with (
                patch.object(TMUX_MODULE, "VENDOR_ROOT", path.parent.parent),
                patch.object(TMUX_MODULE.platform, "system", return_value="Linux"),
                patch.object(TMUX_MODULE.platform, "machine", return_value="x86_64"),
            ):
                self.assertIsNone(TMUX_MODULE.bundled_tmux_path())

    def test_inaccessible_execute_bit_falls_back_to_path(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "linux-x86_64" / "tmux"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"tmux")
            path.chmod(0o001)
            with (
                patch.object(TMUX_MODULE, "VENDOR_ROOT", path.parent.parent),
                patch.object(TMUX_MODULE.platform, "system", return_value="Linux"),
                patch.object(TMUX_MODULE.platform, "machine", return_value="x86_64"),
            ):
                self.assertIsNone(TMUX_MODULE.bundled_tmux_path())
