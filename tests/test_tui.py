import curses
import unittest
from unittest.mock import patch

from letee import tui


class TuiTest(unittest.TestCase):
    def test_ascii_detection_uses_encoding_and_override(self):
        for encoding, override, expected in (("UTF-8", "", False), ("UTF-8", "1", True), ("ASCII", "", True)):
            with self.subTest(encoding=encoding, override=override), patch.object(
                tui.locale, "getpreferredencoding", return_value=encoding
            ), patch.dict(tui.os.environ, {"LETEE_ASCII": override}):
                self.assertEqual(tui.ascii_mode(), expected)

    def test_icons_spinner_and_status_fallback(self):
        for ascii_mode, pointer, frames, completed in (
            (False, "›", "⠋⠙⠏⠋", "✓"), (True, ">", "|//-", "+")
        ):
            with self.subTest(ascii=ascii_mode), patch.object(tui, "ascii_mode", return_value=ascii_mode):
                self.assertEqual(tui.icons()["selected"], pointer)
                self.assertEqual("".join(tui.spinner_frame(t) for t in (0, 0.1, 0.9, 1)), frames)
                self.assertEqual(tui.status_icon("completed", 0), completed)
                self.assertEqual(tui.status_icon("working", 0), frames[0])
                self.assertEqual(tui.status_icon("not-a-status", 0), "?")

    def test_cell_width_and_truncation_preserve_existing_behavior(self):
        self.assertEqual(tui.cell_width("e\u0301界"), 3)
        for ascii_mode, expected in ((False, "界…"), (True, "...")):
            with self.subTest(ascii=ascii_mode), patch.object(tui, "ascii_mode", return_value=ascii_mode):
                self.assertEqual(tui.truncate_cells("界界界", 3), expected)
                self.assertEqual(tui.truncate_cells("e\u0301cole", 6), "e\u0301cole")
                self.assertEqual(tui.truncate_cells("abc", 0), "")
                self.assertEqual(tui.truncate("abcdef", 2), ".." if ascii_mode else "a…")

    def test_duration_boundaries(self):
        self.assertEqual(
            [tui.format_duration(n) for n in (-1, 59.9, 60, 3599, 3600, 86399, 86400)],
            ["0s", "59s", "1m", "59m", "1h", "23h", "1d"],
        )

    def test_palette_and_status_attributes(self):
        with patch.dict(tui._COLOR, {"agent_working": 123, "agent_failed": 456}, clear=True):
            self.assertEqual(tui.color("agent_working"), 123)
            self.assertEqual(tui.color("missing"), 0)
            self.assertEqual(tui.status_attr("working"), 123)
            self.assertEqual(tui.status_attr("failed"), 456 | curses.A_BOLD)
            self.assertEqual(tui.status_attr("idle"), curses.A_DIM)
            self.assertEqual(tui.status_attr("not-a-status"), 0)
            self.assertEqual(tui.fade(123), 123 | curses.A_DIM)
        with patch.object(tui.curses, "has_colors", return_value=False):
            tui.init_colors()
        self.assertEqual(tui.color("agent_working"), 0)
