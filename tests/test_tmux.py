import subprocess
import unittest
from unittest.mock import patch

from mtmux.tmux import tmux


class TmuxTests(unittest.TestCase):
    def test_timeout_exits_with_command_without_chained_exception(self):
        with patch("mtmux.tmux.subprocess.run", side_effect=subprocess.TimeoutExpired(["tmux"], 5)):
            with self.assertRaises(SystemExit) as raised:
                tmux("list-sessions")

        self.assertEqual(str(raised.exception), "tmux -L mtmux list-sessions timed out after 5 seconds")
        self.assertIsNone(raised.exception.__cause__)
