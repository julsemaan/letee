import subprocess
import unittest
from io import StringIO
from unittest.mock import patch

from letee.__main__ import run_cli


class CliErrorTest(unittest.TestCase):
    def test_normalizes_os_errors(self):
        stderr = StringIO()
        with patch("letee.__main__.main", side_effect=PermissionError(13, "Permission denied", "/tmp/config")):
            with patch("sys.stderr", stderr):
                self.assertEqual(run_cli([]), 1)

        self.assertEqual(stderr.getvalue(), "letee: /tmp/config: Permission denied\n")

    def test_normalizes_subprocess_errors_with_stderr_reason(self):
        error = subprocess.CalledProcessError(1, ["tmux", "list-sessions"], stderr="no server running\n")
        stderr = StringIO()
        with patch("letee.__main__.main", side_effect=error):
            with patch("sys.stderr", stderr):
                self.assertEqual(run_cli([]), 1)

        self.assertEqual(stderr.getvalue(), "letee: tmux failed: no server running\n")

    def test_normalizes_decode_errors(self):
        stderr = StringIO()
        error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        with patch("letee.__main__.main", side_effect=error):
            with patch("sys.stderr", stderr):
                self.assertEqual(run_cli([]), 1)

        self.assertIn("letee: text decoding failed: ", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
