import subprocess
import unittest
from unittest.mock import patch

from letee.tmux import set_server, tmux


class TmuxTests(unittest.TestCase):
    def tearDown(self):
        set_server(None)

    def test_named_server_selects_prefixed_socket(self):
        set_server("work")
        with patch("letee.tmux.subprocess.run") as run:
            tmux("list-sessions")

        self.assertEqual(run.call_args.args[0], ["tmux", "-L", "letee-work", "list-sessions"])

    def test_timeout_can_be_disabled_for_interactive_commands(self):
        with patch("letee.tmux.subprocess.run") as run:
            tmux("display-menu", timeout=None)

        run.assert_called_once_with(
            ["tmux", "-L", "letee", "display-menu"],
            text=True, capture_output=False, check=True, timeout=None,
        )

    def test_timeout_exits_with_command_without_chained_exception(self):
        with patch("letee.tmux.subprocess.run", side_effect=subprocess.TimeoutExpired(["tmux"], 5)):
            with self.assertRaises(SystemExit) as raised:
                tmux("list-sessions")

        self.assertEqual(str(raised.exception), "tmux -L letee list-sessions timed out after 5 seconds")
        self.assertIsNone(raised.exception.__cause__)
