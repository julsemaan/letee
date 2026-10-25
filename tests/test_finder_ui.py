import curses
import json
from dataclasses import replace
from threading import Event
import time
import unittest
from unittest.mock import patch

from finder_fixtures import sample_workspace
from letee import finder_ui, tmux, tui
from letee.workspace import Workspace


class FakeWindow:
    def __init__(self, keys, size=(14, 80)):
        self.keys = iter(keys)
        self.size = size
        self.frames = []
        self.writes = []
        self.cursor = None
        self.timeouts = []

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.writes = []

    def addstr(self, y, x, text, attr=0):
        h, w = self.size
        assert 0 <= y < h and 0 <= x < w
        assert x + tui.cell_width(text) < w
        self.writes.append((y, x, text, attr))

    def move(self, y, x):
        h, w = self.size
        assert 0 <= y < h and 0 <= x < w
        self.cursor = (y, x)

    def refresh(self):
        self.frames.append((list(self.writes), self.cursor))

    def get_wch(self):
        key = next(self.keys)
        if callable(key):
            key = key()
        if key == -1:
            time.sleep(0.001)
            raise curses.error()
        return key

    def timeout(self, value):
        self.timeouts.append(value)

    def keypad(self, enabled):
        pass


def frame_text(frame):
    return "".join(write[2] for write in frame[0])


class FinderUITest(unittest.TestCase):
    def setUp(self):
        self.workspace = sample_workspace()
        self.stored_scope = ""
        self.stored_filters = ""

        def read_option(*args, **kwargs):
            return self.stored_scope if args[-1] == finder_ui.FINDER_SCOPE_OPTION else self.stored_filters

        def save_option(*args, **kwargs):
            if args[-2] == finder_ui.FINDER_SCOPE_OPTION:
                self.stored_scope = args[-1]
            else:
                self.stored_filters = args[-1]

        for name, effect in (("out", read_option), ("tmux", save_option)):
            patcher = patch.object(tmux, name, side_effect=effect)
            setattr(self, "tmux_" + name, patcher.start())
            self.addCleanup(patcher.stop)
        for name, value in (("curs_set", 1), ("mousemask", (0, 0)),
                            ("get_escdelay", 1000), ("set_escdelay", None)):
            patcher = patch.object(curses, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(tui, "init_colors")
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_ui(self, keys, **kwargs):
        window = FakeWindow(keys)
        result = finder_ui.run(window, self.workspace, prepare_open=lambda row: lambda: None, **kwargs)
        return result, window

    def test_missing_or_invalid_scope_defaults_to_all(self):
        for stored in ("", "invalid", "windows"):
            with self.subTest(stored=stored):
                self.stored_scope = stored
                _, window = self.run_ui(["\x03"])
                self.assertTrue(any(text == "[All] " and attr & curses.A_REVERSE
                                    for _, _, text, attr in window.frames[0][0]))
                self.assertEqual(self.stored_scope, "All")

    def test_reopening_remembers_keyboard_and_mouse_scope_on_cancel(self):
        for key, expected in (("\x14", "Sessions"), (curses.KEY_MOUSE, "Windows")):
            with self.subTest(key=key):
                self.stored_scope = ""
                with patch.object(curses, "getmouse", return_value=(0, 20, 1, 0, curses.BUTTON1_CLICKED)):
                    self.run_ui([key, "e", "\t", curses.KEY_DOWN, "\x03"])
                self.assertEqual(self.stored_scope, expected)
                _, window = self.run_ui(["\x03"])
                self.assertTrue(any(text == f"[{expected}] " and attr & curses.A_REVERSE
                                    for _, _, text, attr in window.frames[0][0]))
                self.assertEqual(window.frames[0][1], (0, 2))

    def test_scope_io_is_outside_input_loop(self):
        def check_io():
            self.assertEqual(self.tmux_out.call_count, 2)
            for option in ("@letee_finder_scope", "@letee_finder_filters"):
                self.tmux_out.assert_any_call("show-options", "-qv", "-t", tmux.SESSION, option)
            self.tmux_tmux.assert_not_called()
            return "\x03"

        self.run_ui(["\x14", check_io])
        self.assertEqual(self.tmux_tmux.call_count, 2)
        self.tmux_tmux.assert_any_call(
            "set-option", "-t", tmux.SESSION, "@letee_finder_scope", "Sessions", capture=True)

    def test_successful_navigation_saves_scope_for_reopening(self):
        window = FakeWindow(["\x14", "\x14", "\n"] + [-1] * 1000)
        self.assertTrue(finder_ui.run(window, self.workspace, prepare_open=lambda row: lambda: None))
        self.assertEqual(self.stored_scope, "Windows")
        _, reopened = self.run_ui(["\x03"])
        self.assertTrue(any(text == "[Windows] " and attr & curses.A_REVERSE
                            for _, _, text, attr in reopened.frames[0][0]))

    def test_filter_summary_and_keyboard_preserve_query_and_scope(self):
        popup = finder_ui._Popup(FakeWindow([]), self.workspace)
        popup.render()
        self.assertEqual(popup.capacity, 8)
        self.assertTrue(any(y == 3 and "Filters: none" in text for y, _, text, _ in popup.screen.writes))
        for key in ("e", "\x14", "\x06", " ", curses.KEY_DOWN, " ", "\n"):
            self.assertTrue(popup.key(key, lambda row: self.fail()))
        self.assertEqual(popup.finder.query, "e")
        self.assertEqual(popup.cursor, 1)
        self.assertEqual(popup.finder.scope, "Sessions")
        self.assertEqual(popup.finder.categories, frozenset(finder_ui.FILTER_CATEGORIES[:2]))
        popup.render()
        self.assertTrue(any(y == 3 and "Needs attention, Running" in text
                            for y, _, text, _ in popup.screen.writes))
        popup.key("\x06", lambda row: self.fail())
        self.assertTrue(popup.key("\n", lambda row: self.fail()))
        self.assertFalse(popup.key("\x03", lambda row: self.fail()))

    def test_escape_is_ignored_and_ctrl_c_cancels_in_both_views(self):
        for filters_open in (False, True):
            with self.subTest(filters_open=filters_open):
                popup = finder_ui._Popup(FakeWindow([]), self.workspace)
                popup.key("e", lambda row: self.fail())
                popup.filters_open = filters_open
                selected = popup.finder.selected
                self.assertTrue(popup.key("\x1b", lambda row: self.fail()))
                self.assertEqual(popup.filters_open, filters_open)
                self.assertEqual(popup.finder.query, "e")
                self.assertEqual(popup.finder.selected, selected)
                self.assertFalse(popup.key("\x03", lambda row: self.fail()))

    def test_only_enter_returns_from_filters_without_changing_state(self):
        for enter in ("\n", "\r", curses.KEY_ENTER):
            with self.subTest(enter=enter):
                popup = finder_ui._Popup(FakeWindow([]), self.workspace)
                for key in ("e", "\x14", "\x06", " "):
                    popup.key(key, lambda row: self.fail())
                selected = popup.finder.selected
                for key in ("\x1b", "\x06"):
                    self.assertTrue(popup.key(key, lambda row: self.fail()))
                    self.assertTrue(popup.filters_open)
                self.assertTrue(popup.key(enter, lambda row: self.fail()))
                self.assertFalse(popup.filters_open)
                self.assertEqual(popup.finder.query, "e")
                self.assertEqual(popup.cursor, 1)
                self.assertEqual(popup.finder.scope, "Sessions")
                self.assertEqual(popup.finder.selected, selected)
                self.assertEqual(popup.finder.categories, frozenset({finder_ui.FILTER_CATEGORIES[0]}))

    def test_enter_on_clear_filters_clears_and_returns_to_results(self):
        for enter in ("\n", "\r", curses.KEY_ENTER):
            with self.subTest(enter=enter):
                popup = finder_ui._Popup(FakeWindow([]), self.workspace)
                for key in ("e", "\x14", "\x06", " "):
                    popup.key(key, lambda row: self.fail())
                self.assertTrue(popup.finder.categories)
                popup.filter_index = len(finder_ui.FILTER_CATEGORIES)
                self.assertTrue(popup.key(enter, lambda row: self.fail()))
                self.assertFalse(popup.filters_open)
                self.assertFalse(popup.finder.categories)
                self.assertEqual(popup.finder.query, "e")
                self.assertEqual(popup.finder.scope, "Sessions")
                self.assertEqual(popup.cursor, 1)

    def test_clear_filters_has_distinct_style_and_selection_highlight(self):
        for color in (0, 256):
            with self.subTest(color=color), patch.object(tui, "color", return_value=color):
                popup = finder_ui._Popup(FakeWindow([]), self.workspace)
                popup.filters_open = True
                for selected in (False, True):
                    popup.filter_index = len(finder_ui.FILTER_CATEGORIES) if selected else 0
                    popup.render()
                    writes = {text: attr for _, _, text, attr in popup.screen.writes}
                    expected = color | curses.A_BOLD | (curses.A_REVERSE if selected else 0)
                    self.assertEqual(writes["Clear filters"], expected)
                    self.assertTrue(all(not attr & curses.A_BOLD
                                        for text, attr in writes.items() if text.startswith("[ ] ")))

    def test_hint_wording_styles_and_narrow_clipping(self):
        views = (
            (False, "Ctrl-T scope  Ctrl-F filters  Tab expand  Enter open  Ctrl-C cancel"),
            (True, "Up/Down select  Space toggle  Enter back  Ctrl-C cancel"),
        )
        for colored in (False, True):
            palette = {"create": 256, "hints": 512} if colored else {}
            with patch.object(tui, "color", side_effect=lambda name: palette.get(name, 0)):
                for filters_open, expected in views:
                    with self.subTest(colored=colored, filters_open=filters_open):
                        screen = FakeWindow([], (14, 100))
                        popup = finder_ui._Popup(screen, self.workspace)
                        popup.filters_open = filters_open
                        popup.render()
                        writes = [(x, text, attr) for y, x, text, attr in screen.writes if y == 2]
                        self.assertEqual("".join(text for _, text, _ in writes).rstrip(), expected)
                        for _, text, attr in writes:
                            if text.strip() in ("Ctrl-T", "Ctrl-F", "Tab", "Enter", "Ctrl-C", "Up/Down", "Space"):
                                self.assertEqual(attr, palette.get("create", 0) | curses.A_BOLD)
                            else:
                                self.assertEqual(attr, palette.get("hints", 0) | curses.A_DIM)
                        for width in range(1, len(expected) + 2):
                            screen.size = (14, width)
                            popup.render()  # FakeWindow asserts every write fits.

    def test_category_checklist_counts_refresh_with_query_and_workspace(self):
        popup = finder_ui._Popup(FakeWindow([]), self.workspace)
        popup.key("\x06", lambda row: self.fail())

        def assert_labels():
            popup.render()
            labels = [text for y, _, text, _ in popup.screen.writes if y >= 4]
            for category in finder_ui.FILTER_CATEGORIES:
                checked = "x" if category in popup.finder.categories else " "
                self.assertIn(f"[{checked}] {category} · {popup.finder.category_counts[category]}", labels)

        assert_labels()
        counts = dict(popup.finder.category_counts)
        popup.key(" ", lambda row: self.fail())
        self.assertEqual(popup.finder.category_counts, counts)
        assert_labels()
        popup.key("\n", lambda row: self.fail())
        popup.key("\x14", lambda row: self.fail())
        self.assertEqual(popup.finder.category_counts, counts)
        for key in "zzzz":
            popup.key(key, lambda row: self.fail())
        popup.key("\x06", lambda row: self.fail())
        self.assertTrue(all(count == 0 for count in popup.finder.category_counts.values()))
        assert_labels()
        popup.finder.set_query("")
        self.assertEqual(popup.finder.category_counts, counts)
        popup.finder.update(Workspace())
        assert_labels()
        self.assertTrue(all(count == 0 for count in popup.finder.category_counts.values()))

    def test_filter_persistence_and_malformed_values(self):
        valid = {"categories": ["Running", "Needs attention"]}
        for stored in ("", "{", "null", "[]", "true", "{}",
                       '{"states":["working"],"has_bell":true}',
                       '{"categories":["bogus"]}',
                       '{"categories":"Running"}',
                       '{"categories":[[]]}',
                       '{"categories":[1]}',
                       '{"categories":["Running","bogus"]}',
                       json.dumps(valid)):
            with self.subTest(stored=stored):
                self.stored_filters = stored
                _, window = self.run_ui(["\x03"])
                expected = sorted(valid["categories"]) if stored == json.dumps(valid) else []
                self.assertEqual(json.loads(self.stored_filters), {"categories": expected})
                self.assertIn("Filters:", frame_text(window.frames[0]))

    def test_filter_keyboard_changes_saved_and_reopened_on_cancel(self):
        self.run_ui(["\x06", " ", curses.KEY_DOWN, " ", "\x03"])
        saved = json.loads(self.stored_filters)
        self.assertEqual(saved, {"categories": sorted(finder_ui.FILTER_CATEGORIES[:2])})
        _, window = self.run_ui(["\x03"])
        self.assertIn("Needs attention, Running", frame_text(window.frames[0]))

    def test_filter_checklist_mouse_scroll_clear_and_resize(self):
        screen = FakeWindow([], (9, 60))
        popup = finder_ui._Popup(screen, self.workspace)
        popup.render()
        def click(x, y, button=curses.BUTTON1_CLICKED):
            with patch.object(curses, "getmouse", return_value=(0, x, y, 0, button)):
                popup.key(curses.KEY_MOUSE, lambda row: self.fail("checklist must not open destinations"))
            popup.render()

        click(2, 3)
        self.assertTrue(popup.filters_open)
        click(2, 4)
        self.assertEqual(popup.finder.categories, frozenset({finder_ui.FILTER_CATEGORIES[0]}))
        click(2, 5)
        self.assertEqual(popup.finder.categories, frozenset(finder_ui.FILTER_CATEGORIES[:2]))
        selected = popup.finder.selected
        click(2, 5, curses.BUTTON5_PRESSED)
        self.assertGreater(popup.filter_scroll, 0)
        self.assertEqual(popup.finder.selected, selected)
        click(2, 5, curses.BUTTON4_PRESSED)
        self.assertEqual(popup.filter_scroll, 0)
        for _ in range(len(finder_ui.FILTER_CATEGORIES) + 1):
            popup.key(curses.KEY_DOWN, lambda row: self.fail())
        screen.size = (8, 12)
        popup.key(curses.KEY_RESIZE, lambda row: self.fail())
        popup.render()
        self.assertEqual(popup.filter_scroll, popup.filter_index - popup.capacity + 1)
        click(1, 5)
        self.assertFalse(popup.finder.categories)
        for size in ((1, 1), (4, 8), (6, 12), (14, 80)):
            screen.size = size
            popup.key(curses.KEY_RESIZE, lambda row: self.fail())
            popup.render()
        self.assertIn("Clear filters", frame_text(screen.frames[-1]))
        popup.key("\n", lambda row: self.fail())
        self.assertFalse(popup.filters_open)

    def test_filters_apply_in_every_scope_and_ignore_query_edits_in_checklist(self):
        popup = finder_ui._Popup(FakeWindow([]), self.workspace)
        popup.finder.set_query("Claude")
        popup.cursor = 3
        for scope in finder_ui.SCOPES:
            popup.finder.set_scope(scope)
            popup.key("\x06", lambda row: self.fail())
            for key in ("z", "\x15", curses.KEY_BACKSPACE, "\x14", "\t"):
                popup.key(key, lambda row: self.fail())
            popup.filter_index = finder_ui.FILTER_CATEGORIES.index("Running")
            popup.key(" ", lambda row: self.fail())
            self.assertEqual(popup.finder.query, "Claude")
            self.assertEqual(popup.finder.scope, scope)
            self.assertEqual(popup.cursor, 3)
            self.assertEqual(popup.finder.categories, frozenset({"Running"}))
            popup.filter_index = len(finder_ui.FILTER_CATEGORIES)
            popup.key(" ", lambda row: self.fail())
            self.assertFalse(popup.finder.categories)
            popup.key(curses.KEY_ENTER, lambda row: self.fail())

    def test_successful_navigation_saves_filters(self):
        self.stored_filters = json.dumps({"categories": ["Needs attention"]})
        window = FakeWindow(["\x14", "\x14", "\x14", "\n"] + [-1] * 1000)
        self.assertTrue(finder_ui.run(window, self.workspace, prepare_open=lambda row: lambda: None))
        self.assertEqual(json.loads(self.stored_filters), {"categories": ["Needs attention"]})

    def test_edit_cursor_clear_and_scope(self):
        result, window = self.run_ui([
            "a", "c", curses.KEY_LEFT, "b", curses.KEY_DC,
            curses.KEY_BACKSPACE, "\x15", "界", "\x14", "\x03",
        ])
        self.assertFalse(result)
        text = [frame_text(frame) for frame in window.frames]
        self.assertTrue(any("abc" in item for item in text))
        self.assertTrue(any("界" in item for item in text))
        self.assertLessEqual(window.timeouts[0], 50)

    def test_open_is_async_and_success_returns_true(self):
        opened = []
        window = FakeWindow(["\n"] + [-1] * 1000)
        self.assertTrue(finder_ui.run(window, self.workspace, prepare_open=lambda row: lambda: opened.append(row)))
        self.assertEqual(opened[0].key, self.workspace.sessions[0].key)

    def test_ready_result_does_not_commit_before_queued_cancellation(self):
        opened = []
        window = FakeWindow(["\x03"])
        popup = finder_ui._Popup(window, self.workspace)
        popup.opening = self.workspace.sessions[0].key
        popup.results.put((lambda: opened.append(True), None))
        with patch.object(finder_ui, "_Popup", return_value=popup):
            self.assertFalse(finder_ui.run(window, self.workspace, prepare_open=lambda row: self.fail()))
        self.assertFalse(opened)
        self.assertIn(0, window.timeouts)

    def test_instant_preparation_drains_edits_and_updates_before_cancellation(self):
        for keys in (["\n", "\x03"], ["\n", "x", "\x03"]):
            with self.subTest(keys=keys):
                opened, updates = [], []
                updated = replace(self.workspace, loading=True)
                window = FakeWindow(keys)

                def read_workspace():
                    updates.append(True)
                    return updated

                # Complete preparation on Enter, before the next queued key.
                with patch.object(finder_ui.Thread, "start", lambda thread: thread.run()):
                    self.assertFalse(finder_ui.run(
                        window, self.workspace, read_workspace=read_workspace,
                        prepare_open=lambda row: lambda: opened.append(row.key)))
                self.assertFalse(opened)
                self.assertEqual(len(updates), len(keys))
                if "x" in keys:
                    self.assertIn("x", frame_text(window.frames[-1]))

    def test_resize_follows_selection(self):
        popup = finder_ui._Popup(FakeWindow([]), self.workspace)
        popup.follow_selection = False
        self.assertTrue(popup.key(curses.KEY_RESIZE, lambda row: self.fail()))
        self.assertTrue(popup.follow_selection)

    def test_cancel_during_open_keeps_accepting_query_input(self):
        started, release, finished = Event(), Event(), Event()

        def slow_open(row):
            started.set()
            release.wait(2)
            finished.set()
            return lambda: None

        def type_after_start():
            self.assertTrue(started.wait(1))
            return "x"

        window = FakeWindow(["\n", type_after_start, "\x03"])
        try:
            self.assertFalse(finder_ui.run(window, self.workspace, prepare_open=slow_open))
            self.assertIn("Opening", frame_text(window.frames[-1]))
            self.assertIn("x", frame_text(window.frames[-1]))
            self.assertFalse(finished.is_set())
        finally:
            release.set()
            self.assertTrue(finished.wait(1))

    def test_cancelled_background_completion_never_navigates(self):
        started, release, finished = Event(), Event(), Event()
        opened = []

        def slow_open(row):
            started.set()
            release.wait(2)
            finished.set()
            return lambda: opened.append(row)

        def cancel_after_start():
            self.assertTrue(started.wait(1))
            return "\x03"

        try:
            window = FakeWindow(["\n", cancel_after_start])
            self.assertFalse(finder_ui.run(window, self.workspace, prepare_open=slow_open))
        finally:
            release.set()
            self.assertTrue(finished.wait(1))
        self.assertFalse(opened)

    def test_open_error_preserves_query_and_allows_retry(self):
        attempts = []
        failed = Event()

        def open_row(row):
            attempts.append(row)
            if len(attempts) == 1:
                failed.set()
                raise RuntimeError("cannot open")
            return lambda: None

        def wait_for_error():
            self.assertTrue(failed.wait(1))
            return -1

        window = FakeWindow(list("letee") + ["\n", wait_for_error] + [-1] * 20 + ["\n"] + [-1] * 1000)
        self.assertTrue(finder_ui.run(window, self.workspace, prepare_open=open_row))
        self.assertTrue(any("cannot open" in frame_text(frame) and "letee" in frame_text(frame)
                            for frame in window.frames))
        self.assertEqual(len(attempts), 2)

    def test_preparation_and_commit_errors_allow_retry(self):
        for phase in ("prepare", "commit"):
            for error_type in (SystemExit, RuntimeError):
                with self.subTest(phase=phase, error=error_type):
                    attempts = []
                    failed = Event()

                    def fail():
                        failed.set()
                        raise error_type("navigation failed")

                    def prepare(row):
                        attempts.append(row.key)
                        if len(attempts) == 1:
                            if phase == "prepare":
                                fail()
                            return fail
                        return lambda: None

                    def wait_for_failure():
                        self.assertTrue(failed.wait(1))
                        return -1

                    # Commit failures happen on the UI thread, before the next key.
                    keys = list("letee") + ["\n"] + [-1] * 20 + [wait_for_failure, "\n"] + [-1] * 1000
                    window = FakeWindow(keys)
                    self.assertTrue(finder_ui.run(window, self.workspace, prepare_open=prepare))
                    self.assertEqual(len(attempts), 2)
                    self.assertTrue(any("navigation failed" in frame_text(frame) and "letee" in frame_text(frame)
                                        for frame in window.frames))

    def test_slow_preparation_consumes_updates_and_retains_edited_query_on_error(self):
        started, release = Event(), Event()
        updates = []
        updated = replace(self.workspace, loading=True)

        def prepare(row):
            started.set()
            release.wait(2)
            raise SystemExit("preparation failed")

        def type_while_preparing():
            self.assertTrue(started.wait(1))
            updates.append(updated)
            return "x"

        def finish():
            release.set()
            return -1

        window = FakeWindow(["\n", type_while_preparing, finish] + [-1] * 100 + ["\x03"])
        seen = []

        def read_workspace():
            if updates:
                seen.append(updates.pop())
                return seen[-1]
            return None

        try:
            self.assertFalse(finder_ui.run(window, self.workspace, prepare_open=prepare,
                                          read_workspace=read_workspace))
        finally:
            release.set()
        self.assertEqual(seen, [updated])
        self.assertIn("x", frame_text(window.frames[-1]))
        self.assertIn("preparation failed", frame_text(window.frames[-1]))

    def test_original_destination_is_revalidated_after_preparation(self):
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable):
                started, release = Event(), Event()
                opened = []
                updates = []

                def prepare(row):
                    started.set()
                    release.wait(2)
                    return lambda: opened.append(row.key)

                def remove_original():
                    self.assertTrue(started.wait(1))
                    sessions = self.workspace.sessions
                    if unavailable:
                        sessions = (replace(sessions[0], available=False),) + sessions[1:]
                    else:
                        sessions = sessions[1:]
                    updates.append(replace(self.workspace, sessions=sessions))
                    return curses.KEY_DOWN

                def finish():
                    release.set()
                    return -1

                window = FakeWindow(["\n", remove_original, finish] + [-1] * 100 + ["\x03"])
                try:
                    self.assertFalse(finder_ui.run(window, self.workspace, prepare_open=prepare,
                                                  read_workspace=lambda: updates.pop() if updates else None))
                finally:
                    release.set()
                self.assertFalse(opened)
                self.assertIn("Unavailable", frame_text(window.frames[-1]))

    def test_preparation_commits_original_after_query_hides_it(self):
        started, release = Event(), Event()
        opened = []

        def prepare(row):
            started.set()
            release.wait(2)
            return lambda: opened.append(row.key)

        def change_query():
            self.assertTrue(started.wait(1))
            return "z"

        def finish():
            release.set()
            return -1

        window = FakeWindow(["\n", change_query, finish] + [-1] * 1000)
        try:
            self.assertTrue(finder_ui.run(window, self.workspace, prepare_open=prepare))
        finally:
            release.set()
        self.assertEqual(opened, [self.workspace.sessions[0].key])

    def test_enter_without_selection_reports_unavailable(self):
        result, window = self.run_ui(list("zzzz") + ["\n", "\x03"])
        self.assertFalse(result)
        self.assertIn("Unavailable", frame_text(window.frames[-1]))

    def test_update_vanished_selection_keeps_path_and_blocks_open(self):
        updates = iter([None, Workspace()])
        opened = []
        window = FakeWindow([-1, "\n", "\x03"])
        self.assertFalse(finder_ui.run(window, self.workspace,
                                     read_workspace=lambda: next(updates, None),
                                     prepare_open=lambda row: lambda: opened.append(row)))
        self.assertFalse(opened)
        self.assertIn("Unavailable", frame_text(window.frames[-1]))
        self.assertIn("letee", frame_text(window.frames[-1]))

    def test_loading_empty_and_unavailable(self):
        for workspace, message in ((Workspace(loading=True), "Loading"),
                                   (Workspace(), "No destinations"),
                                   (replace(self.workspace, sessions=tuple(
                                       replace(s, available=False) for s in self.workspace.sessions)), "Unavailable")):
            with self.subTest(message=message):
                window = FakeWindow(["\x03"])
                self.assertFalse(finder_ui.run(window, workspace, prepare_open=lambda row: self.fail()))
                self.assertIn(message, frame_text(window.frames[-1]))

    def test_query_viewport_tracks_cursor_and_resize(self):
        window = FakeWindow(list("abcdefghijk界") + [curses.KEY_LEFT, "\x03"], (8, 12))
        finder_ui.run(window, self.workspace, prepare_open=lambda row: lambda: None)
        writes, cursor = window.frames[-1]
        self.assertEqual(cursor, (0, 8))
        self.assertTrue(any("界" in text for _, _, text, _ in writes))

    def test_keyboard_interrupt_cancels(self):
        def interrupt():
            raise KeyboardInterrupt

        result, _ = self.run_ui([interrupt])
        self.assertFalse(result)

    def test_mouse_scope_changes_without_opening(self):
        with patch.object(curses, "getmouse", return_value=(0, 20, 1, 0, curses.BUTTON1_CLICKED)):
            result, window = self.run_ui([curses.KEY_MOUSE, "\x03"])
        self.assertFalse(result)
        self.assertTrue(any(text == "[Windows] " and attr & curses.A_REVERSE
                            for _, _, text, attr in window.frames[-1][0]))

    def test_small_resized_unicode_and_ascii(self):
        for ascii_mode in (True, False):
            for size in ((1, 1), (2, 3), (5, 12), (14, 80)):
                with self.subTest(size=size, ascii=ascii_mode), patch.object(tui, "ascii_mode", return_value=ascii_mode):
                    def resize():
                        window.size = (3, 5)
                        return curses.KEY_RESIZE

                    window = FakeWindow(list("界e\u0301" * 20) + [curses.KEY_LEFT, resize, "\x03"], size)
                    self.assertFalse(finder_ui.run(window, self.workspace, prepare_open=lambda row: lambda: None))

    def test_tab_expands_tree_and_down_selects_window(self):
        opened = []
        window = FakeWindow(["\t", curses.KEY_DOWN, "\n"] + [-1] * 1000)
        self.assertTrue(finder_ui.run(window, self.workspace, prepare_open=lambda row: lambda: opened.append(row)))
        self.assertEqual(opened[0].kind, "window")
        self.assertTrue(any("editor" in frame_text(frame) for frame in window.frames))

    def test_mouse_scope_disclosure_and_destination(self):
        clicks = iter([(0, 0, 1, 0, curses.BUTTON1_CLICKED),
                       (0, 0, 4, 0, curses.BUTTON1_CLICKED),
                       (0, 10, 5, 0, curses.BUTTON1_CLICKED)])
        opened = []
        with patch.object(curses, "getmouse", side_effect=lambda: next(clicks)):
            window = FakeWindow([curses.KEY_MOUSE] * 3 + [-1] * 1000)
            self.assertTrue(finder_ui.run(window, self.workspace, prepare_open=lambda row: lambda: opened.append(row)))
        self.assertEqual(opened[0].kind, "window")

    def test_wheel_does_not_change_selection(self):
        opened = []
        with patch.object(curses, "getmouse", return_value=(0, 10, 4, 0, curses.BUTTON5_PRESSED)):
            window = FakeWindow([curses.KEY_MOUSE, "\n"] + [-1] * 1000, (7, 80))
            self.assertTrue(finder_ui.run(window, self.workspace, prepare_open=lambda row: lambda: opened.append(row)))
        self.assertEqual(opened[0].key, self.workspace.sessions[0].key)

    def test_combining_mark_stays_with_highlighted_base(self):
        session = self.workspace.sessions[0]
        session = replace(session, windows=(replace(session.windows[0], name="q\u0301"),))
        workspace = replace(self.workspace, sessions=(session,))
        window = FakeWindow(["\x14", "\x14", "q", "\x03"])
        finder_ui.run(window, workspace, prepare_open=lambda row: lambda: None)
        writes = window.frames[-1][0]
        self.assertTrue(any("q\u0301" in text and attr & curses.A_UNDERLINE for _, _, text, attr in writes))
        self.assertFalse(any(text.startswith("\u0301") for _, _, text, _ in writes))

    def test_shared_bell_and_agent_alert_indicators(self):
        session = self.workspace.sessions[0]
        window = session.windows[1]
        agent = replace(window.agents[0], alert=True)
        session = replace(session, bell=True, windows=(replace(window, agents=(agent,)),))
        workspace = replace(self.workspace, sessions=(session,))
        for ascii_mode, indicator in ((True, " BELL"), (False, " 🔔")):
            with self.subTest(ascii=ascii_mode), patch.object(tui, "ascii_mode", return_value=ascii_mode), \
                    patch.object(tui, "color", side_effect=lambda name: curses.A_BLINK if name == "danger" else 0):
                screen = FakeWindow(["\t", curses.KEY_DOWN, "\t", "\x03"])
                finder_ui.run(screen, workspace, prepare_open=lambda row: lambda: None)
                indicators = [(y, attr) for y, _, text, attr in screen.frames[-1][0] if text == indicator]
                self.assertEqual([y for y, _ in indicators], [4, 6])
                self.assertTrue(all(attr & curses.A_BLINK for _, attr in indicators))

    def test_render_highlights_and_current_are_distinct(self):
        window = FakeWindow(["\t", "\t", "\x14", "\x14", "\x14", "C", "\x03"])
        finder_ui.run(window, self.workspace, prepare_open=lambda row: lambda: None)
        writes = [write for frame, _ in window.frames for write in frame]
        self.assertTrue(any(attr & curses.A_UNDERLINE for _, _, _, attr in writes))
        self.assertTrue(any(attr & curses.A_REVERSE for _, _, _, attr in writes))
        self.assertTrue(any("*" in text for _, _, text, _ in writes))
