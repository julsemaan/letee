import curses
import unittest
from unittest.mock import call, patch

import mtmux.sidebar as sidebar
from mtmux.discovery import AgentEntry, SessionSnapshot, SourceSnapshot
from mtmux.names import PaneTarget, Target


def source(kind, sessions=(), bells=(), host=None, available=True, error=None):
    targets = tuple(Target(kind, session, host) for session in sessions)
    bell_targets = frozenset(Target(kind, session, host) for session in bells)
    return SourceSnapshot(available, targets, bell_targets, error)


def snapshot(local=(), remotes=None, local_bells=(), local_available=True, local_error=None):
    return SessionSnapshot(
        source("local", local, local_bells, available=local_available, error=local_error),
        remotes or {},
    )


from mtmux.sidebar import (
    Effect,
    Entry,
    SidebarState,
    _agent_entries,
    _bell_targets,
    _creation_key,
    _draw,
    _fade,
    _entries,
    _entry_at_row,
    _entry_attr,
    _entry_lines,
    _filter_key,
    _init_colors,
    _mouse_mask,
    _read_key,
    _selected_index,
    _should_auto_create,
    _sync_selection,
    _transition,
    _execute,
    _viewport,
    main,
    run,
)


class FakeScreen:
    def __init__(self, keys=None, size=(5, 20)):
        self.calls = []
        self.keys = list(keys or [])
        self.key = ord("y")
        self.size = size

    def erase(self):
        self.calls.append(("erase",))

    def clear(self):
        self.calls.append(("clear",))

    def getmaxyx(self):
        return self.size

    def addnstr(self, *args):
        self.calls.append(("addnstr", *args))

    def addstr(self, *args):
        self.calls.append(("addstr", *args))

    def chgat(self, *args):
        self.calls.append(("chgat", *args))

    def attron(self, *args):
        self.calls.append(("attron", *args))

    def attroff(self, *args):
        self.calls.append(("attroff", *args))

    def getch(self):
        self.calls.append(("getch",))
        if self.keys:
            return self.keys.pop(0)
        return self.key

    def redrawln(self, *args):
        self.calls.append(("redrawln", *args))

    def refresh(self):
        self.calls.append(("refresh",))

    def move(self, *args):
        self.calls.append(("move", *args))

    def timeout(self, *args):
        self.calls.append(("timeout", *args))


class SidebarViewModeTest(unittest.TestCase):
    def test_normal_view_contains_only_ordered_sessions_and_add_action(self):
        sessions = [Target("ssh", "notes", "dev"), Target("local", "work")]

        entries = _entries("", snapshot(local=("work", "other"), remotes={"dev": source("ssh", ("notes", "chat"), host="dev")}), sessions)

        self.assertEqual([entry.target for entry in entries if entry.kind == "session"], sessions)
        self.assertEqual([entry.kind for entry in entries[:2]], ["add", "spacer"])
        self.assertEqual(sidebar._selectable(entries)[0], 0)

    def test_empty_normal_view_prompts_and_offers_add(self):
        entries = _entries("", snapshot(local=("work",)), [])

        self.assertEqual(
            [(entry.label, entry.kind) for entry in entries],
            [("Add session", "add"), ("", "spacer"), ("Press enter to add a session", "hint")],
        )

    def test_add_picker_groups_hosts_and_excludes_tracked(self):
        tracked_target = Target("local", "work")

        entries = _entries("", snapshot(local=("work", "notes"), remotes={"dev": source("ssh", ("chat",), host="dev")}), [tracked_target], adding=True)

        self.assertNotIn(tracked_target, [entry.target for entry in entries])
        self.assertEqual([entry.kind for entry in entries], ["host", "session", "host", "session"])

    def test_add_picker_filter_preserves_headers_and_hides_create_hosts(self):
        entries = _entries("chat", snapshot(local=("work",), remotes={"dev": source("ssh", ("chat",), host="dev")}), [], adding=True)

        self.assertFalse(any(entry.kind == "host" for entry in entries))
        self.assertEqual([entry.kind for entry in entries], ["header", "header", "session"])

    def test_bells_are_limited_to_tracked(self):
        tracked = Target("local", "work")
        untracked = Target("local", "notes")
        discovered = snapshot(local=("work", "notes"), local_bells=("work", "notes"))

        self.assertEqual(_bell_targets(discovered, untracked, [tracked]), {tracked})


class AgentSidebarTest(unittest.TestCase):
    def test_agent_entries_are_compact_and_keep_exact_pane(self):
        pane = PaneTarget(Target("ssh", "work", "dev"), "@1", "%2", "/tmp/tmux")
        data = SessionSnapshot(SourceSnapshot(True, (), frozenset()), {"dev": SourceSnapshot(True, (pane.target,), frozenset(), panes=(pane,), agents=(AgentEntry(pane, "id", "pi", None),))})

        entry = _agent_entries(data)[0]

        self.assertEqual(entry.pane_target, pane)
        with patch("mtmux.sidebar._ascii", return_value=False):
            self.assertEqual(_entry_lines(entry, True, set(), None, 40), ["› pi · idle", "  └─ dev · work"])

    def test_draw_shows_agent_divider_and_empty_state(self):
        screen = FakeScreen(size=(10, 40))

        _draw(screen, [], 0, "", "", agent_entries=[])

        text = [item[3] for item in screen.calls if item[0] == "addnstr"]
        self.assertTrue(any(line.startswith("AGENTS ") for line in text))
        self.assertIn("  No active agents", text)

    def test_agent_entry_can_be_selected_with_mouse(self):
        pane = PaneTarget(Target("local", "work"), "@1", "%2", "/tmp/tmux")
        entries = [Entry("pi", "agent", pane.target, pane_target=pane, agent_id="id")]

        self.assertEqual(_entry_at_row(entries, 0, 4, 8, 0, top=4), 0)

    def test_switch_pane_uses_exact_attach_command(self):
        pane = PaneTarget(Target("local", "work"), "@1", "%2", "/tmp/tmux")
        with patch("mtmux.sidebar.cockpit.switch") as switch:
            _execute(Effect("switch_pane", pane, message="id"), SidebarState(), unittest.mock.Mock(), 5)

        switch.assert_called_once_with(pane.target, "env -u TMUX tmux -S /tmp/tmux select-window -t work:@1 \\; select-pane -t %2 \\; attach-session -t work")


class SidebarStateTest(unittest.TestCase):
    def test_creation_key_edits_submits_and_cancels(self):
        state = SidebarState(creation_host="dev")

        self.assertIsNone(_creation_key(state, ord("w")))
        self.assertIsNone(_creation_key(state, ord("o")))
        self.assertIsNone(_creation_key(state, 127))
        self.assertEqual(state.creation_text, "w")
        self.assertEqual(_creation_key(state, 10), Effect("create", Target("ssh", "w", "dev")))
        self.assertIsNone(state.creation_host)
        self.assertEqual(state.creation_text, "")

        state.creation_host = ""
        state.creation_text = "draft"
        self.assertIsNone(_creation_key(state, 27))
        self.assertIsNone(state.creation_host)
        self.assertEqual(state.creation_text, "")

    def test_creation_key_rejects_invalid_name_without_closing_editor(self):
        state = SidebarState(creation_host="", creation_text="bad name")

        with self.assertRaisesRegex(SystemExit, "Invalid session"):
            _creation_key(state, 10)

        self.assertEqual(state.creation_host, "")
        self.assertEqual(state.creation_text, "bad name")

    def test_pending_selection_waits_for_discovery_then_selects_target(self):
        target = Target("ssh", "new", "dev")
        state = SidebarState(selected_index=2, pending_selection=target)
        pending_entries = _entries("", snapshot(remotes={"dev": source("ssh", ("work",), host="dev")}))

        _sync_selection(state, pending_entries)
        self.assertEqual(state.pending_selection, target)
        self.assertIsNone(state.selected_target)

        ready_entries = _entries("", snapshot(remotes={"dev": source("ssh", ("work", "new"), host="dev")}))
        _sync_selection(state, ready_entries)
        self.assertEqual(state.selected_target, target)
        self.assertIsNone(state.pending_selection)

    def test_unrelated_snapshot_preserves_user_selection(self):
        selected = Target("local", "notes")
        state = SidebarState(selected_target=selected, selected_index=2)
        entries = _entries("", snapshot(local=("work", "notes"), remotes={"dev": source("ssh", ("chat",), host="dev")}))

        _sync_selection(state, entries)

        self.assertEqual(entries[state.selected_index].target, selected)

    def test_transition_returns_effect_without_running_operations(self):
        target = Target("local", "work")
        state = SidebarState(selected_target=target)

        effect = _transition(state, "switch")

        self.assertEqual(effect, Effect("switch", target=target))

    def test_toggle_favorite_updates_state_and_returns_save_effect(self):
        target = Target("local", "work")
        state = SidebarState(selected_target=target)

        effect = _transition(state, "toggle_session")

        self.assertEqual(state.favorites, [target])
        self.assertEqual(effect, Effect("save_favorites", favorites=(target,), message="added local:work"))

    def test_reorder_favorite_swaps_and_keeps_selection(self):
        first = Target("local", "first")
        second = Target("local", "second")
        state = SidebarState(
            selected_target=second,
            selected_tracked=True,
            favorites=[first, second],
        )

        effect = _transition(state, "move_session_up")

        self.assertEqual(state.favorites, [second, first])
        self.assertEqual(state.selected_target, second)
        self.assertEqual(effect, Effect("save_favorites", favorites=(second, first), message="moved local:second up"))

    def test_reorder_favorite_boundaries_skip_save(self):
        target = Target("local", "only")
        for action, message in (
            ("move_session_up", "already first session"),
            ("move_session_down", "already last session"),
        ):
            with self.subTest(action=action):
                state = SidebarState(selected_target=target, selected_tracked=True, favorites=[target])

                effect = _transition(state, action)

                self.assertEqual(state.favorites, [target])
                self.assertEqual(effect, Effect("status", message=message))

    def test_regular_section_duplicate_cannot_reorder(self):
        first = Target("local", "first")
        second = Target("local", "second")
        state = SidebarState(selected_target=second, selected_tracked=False, favorites=[first, second])

        self.assertIsNone(_transition(state, "move_session_up"))
        self.assertEqual(state.favorites, [first, second])

    def test_successful_create_switches_and_sets_pending_selection(self):
        target = Target("ssh", "new", "dev")
        state = SidebarState()
        poller = unittest.mock.Mock()
        with (
            patch("mtmux.sidebar.sessions.create") as create,
            patch("mtmux.sidebar.save_sessions"),
            patch("mtmux.sidebar.sessions.attach_command", return_value="attach"),
            patch("mtmux.sidebar.cockpit.switch") as switch,
        ):
            self.assertFalse(_execute(Effect("create", target=target), state, poller, 5))

        create.assert_called_once_with(target)
        switch.assert_called_once_with(target, "attach")
        self.assertEqual(state.pending_selection, target)
        poller.refresh.assert_called_once_with()

    def test_successful_kill_discards_target_before_refresh(self):
        target = Target("ssh", "work", "dev")
        state = SidebarState(selected_target=target)
        poller = unittest.mock.Mock()

        with patch("mtmux.sidebar.sessions.kill"):
            _execute(Effect("kill", target=target), state, poller, 5)

        poller.assert_has_calls([unittest.mock.call.discard(target), unittest.mock.call.refresh()])
        self.assertEqual(state.selected_target, target)

    def test_add_switch_tracks_then_switches(self):
        target = Target("local", "work")
        state = SidebarState(adding=True)
        poller = unittest.mock.Mock()
        with (
            patch("mtmux.sidebar.save_sessions") as save,
            patch("mtmux.sidebar.sessions.attach_command", return_value="attach"),
            patch("mtmux.sidebar.cockpit.switch") as switch,
        ):
            _execute(Effect("add_switch", target=target), state, poller, 5)

        self.assertEqual(state.favorites, [target])
        save.assert_called_once_with([target])
        switch.assert_called_once_with(target, "attach")

    def test_successful_create_tracks_after_creation(self):
        target = Target("local", "new")
        state = SidebarState(adding=True)
        poller = unittest.mock.Mock()
        with (
            patch("mtmux.sidebar.sessions.create") as create,
            patch("mtmux.sidebar.save_sessions") as save,
            patch("mtmux.sidebar.sessions.attach_command", return_value="attach"),
            patch("mtmux.sidebar.cockpit.switch"),
        ):
            _execute(Effect("create", target=target), state, poller, 5)

        create.assert_called_once_with(target)
        save.assert_called_once_with([target])
        self.assertFalse(state.adding)

    def test_failed_create_neither_switches_nor_sets_pending_selection(self):
        target = Target("ssh", "new", "dev")
        state = SidebarState()
        poller = unittest.mock.Mock()
        with (
            patch("mtmux.sidebar.sessions.create", side_effect=SystemExit("create failed")),
            patch("mtmux.sidebar.cockpit.switch") as switch,
        ):
            self.assertFalse(_execute(Effect("create", target=target), state, poller, 5))

        switch.assert_not_called()
        self.assertIsNone(state.pending_selection)
        self.assertEqual(state.status, "create failed")


class SidebarColorTest(unittest.TestCase):
    def setUp(self):
        original = sidebar._COLOR
        self.addCleanup(setattr, sidebar, "_COLOR", original)
        sidebar._COLOR = {}

    def test_256_color_terminal_uses_logo_palette(self):
        with (
            patch("mtmux.sidebar.curses.has_colors", return_value=True),
            patch("mtmux.sidebar.curses.start_color"),
            patch("mtmux.sidebar.curses.use_default_colors"),
            patch.object(curses, "COLORS", 256, create=True),
            patch("mtmux.sidebar.curses.init_pair") as init_pair,
            patch("mtmux.sidebar.curses.color_pair", side_effect=lambda pair: pair << 8),
        ):
            _init_colors()

        self.assertEqual(
            init_pair.call_args_list,
            [
                call(1, 79, 233),
                call(2, 214, -1),
                call(3, 36, -1),
                call(4, 30, -1),
                call(5, 79, -1),
                call(6, curses.COLOR_YELLOW, -1),
                call(7, curses.COLOR_RED, -1),
                call(8, 30, -1),
                call(9, 233, 79),
                call(10, 79, -1),
                call(11, 214, -1),
                call(12, 36, -1), call(13, 30, -1), call(14, 214, -1),
                call(15, curses.COLOR_MAGENTA, -1), call(16, curses.COLOR_RED, -1),
                call(17, curses.COLOR_RED, -1), call(18, 79, -1),
                call(19, curses.COLOR_YELLOW, -1),
            ],
        )
        self.assertEqual(sidebar._COLOR["title"], (1 << 8) | curses.A_BOLD)
        self.assertEqual(sidebar._COLOR["active"], (2 << 8) | curses.A_BOLD)
        self.assertEqual(sidebar._COLOR["section"], (5 << 8) | curses.A_BOLD)
        self.assertEqual(sidebar._COLOR["hints"], (8 << 8) | curses.A_DIM)
        self.assertEqual(sidebar._COLOR["slot"], (10 << 8) | curses.A_BOLD | curses.A_REVERSE)
        self.assertEqual(sidebar._COLOR["slot_active"], (11 << 8) | curses.A_BOLD | curses.A_REVERSE)

    def test_8_color_terminal_uses_safe_palette(self):
        with (
            patch("mtmux.sidebar.curses.has_colors", return_value=True),
            patch("mtmux.sidebar.curses.start_color"),
            patch("mtmux.sidebar.curses.use_default_colors"),
            patch.object(curses, "COLORS", 8, create=True),
            patch("mtmux.sidebar.curses.init_pair") as init_pair,
            patch("mtmux.sidebar.curses.color_pair", side_effect=lambda pair: pair << 8),
        ):
            _init_colors()

        self.assertEqual(
            init_pair.call_args_list,
            [
                call(1, curses.COLOR_CYAN, curses.COLOR_BLACK),
                call(2, curses.COLOR_YELLOW, -1),
                call(3, curses.COLOR_GREEN, -1),
                call(4, curses.COLOR_CYAN, -1),
                call(5, curses.COLOR_CYAN, -1),
                call(6, curses.COLOR_YELLOW, -1),
                call(7, curses.COLOR_RED, -1),
                call(8, curses.COLOR_CYAN, -1),
                call(9, curses.COLOR_BLACK, curses.COLOR_CYAN),
                call(10, curses.COLOR_CYAN, -1),
                call(11, curses.COLOR_YELLOW, -1),
                call(12, curses.COLOR_GREEN, -1), call(13, curses.COLOR_CYAN, -1),
                call(14, curses.COLOR_YELLOW, -1), call(15, curses.COLOR_MAGENTA, -1),
                call(16, curses.COLOR_RED, -1), call(17, curses.COLOR_RED, -1),
                call(18, curses.COLOR_CYAN, -1), call(19, curses.COLOR_YELLOW, -1),
            ],
        )
        self.assertEqual(sidebar._COLOR["active"], (2 << 8) | curses.A_BOLD)
        self.assertEqual(sidebar._COLOR["section"], (5 << 8) | curses.A_BOLD)
        self.assertEqual(sidebar._COLOR["slot"], (10 << 8) | curses.A_BOLD | curses.A_REVERSE)
        self.assertEqual(sidebar._COLOR["slot_active"], (11 << 8) | curses.A_BOLD | curses.A_REVERSE)

    def test_no_color_terminal_leaves_palette_empty(self):
        sidebar._COLOR = {"title": 123}
        with patch("mtmux.sidebar.curses.has_colors", return_value=False):
            _init_colors()
        self.assertEqual(sidebar._COLOR, {})

    def test_curses_error_leaves_palette_empty(self):
        sidebar._COLOR = {"title": 123}
        with (
            patch("mtmux.sidebar.curses.has_colors", return_value=True),
            patch("mtmux.sidebar.curses.start_color", side_effect=curses.error),
        ):
            _init_colors()
        self.assertEqual(sidebar._COLOR, {})


class SidebarDrawTest(unittest.TestCase):
    def test_inactive_sidebar_dims_existing_colors(self):
        faded = _fade(curses.A_COLOR | curses.A_BOLD)

        self.assertEqual(faded & curses.A_COLOR, curses.A_COLOR)
        self.assertTrue(faded & curses.A_DIM)
        self.assertTrue(faded & curses.A_BOLD)

    def test_main_restarts_after_keyboard_interrupt(self):
        with patch("mtmux.sidebar.curses.wrapper", side_effect=[KeyboardInterrupt, None]) as wrapper:
            self.assertEqual(main(), 0)

        self.assertEqual(wrapper.call_count, 2)

    def test_mouse_mask_registers_only_supported_events(self):
        expected = (
            curses.BUTTON1_CLICKED
            | curses.BUTTON4_PRESSED
            | getattr(curses, "BUTTON5_PRESSED", 0)
        )
        with patch("mtmux.sidebar.curses.mousemask") as mousemask:
            _mouse_mask()

        mousemask.assert_called_once_with(expected)

    def test_mouse_mask_tolerates_missing_button5(self):
        with patch.object(curses, "BUTTON5_PRESSED", None), patch("mtmux.sidebar.curses.mousemask") as mousemask:
            _mouse_mask()

        self.assertFalse(mousemask.call_args.args[0] & 2097152)

    def test_entry_at_row_maps_visible_and_scrolled_rows(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(10)]

        self.assertEqual(_entry_at_row(entries, 0, 1, 8, 1), 0)
        start, _ = _viewport(entries, 9, 8)
        self.assertEqual(_entry_at_row(entries, 9, 2, 8, 1), start)

    def test_entry_at_row_ignores_non_selectable_and_non_entry_areas(self):
        entries = [
            Entry("LOCAL", "header"),
            Entry("offline", "unavailable"),
            Entry("", "spacer"),
            Entry("work", "session", Target("local", "work")),
            *[Entry(str(i), "session", Target("local", str(i))) for i in range(6)],
        ]

        self.assertEqual(sidebar._selectable(entries), [3, 4, 5, 6, 7, 8, 9])
        self.assertIsNone(_entry_at_row(entries, 3, 0, 8, 1))  # title
        self.assertIsNone(_entry_at_row(entries, 0, 1, 10, 1))  # header
        self.assertIsNone(_entry_at_row(entries, 0, 2, 10, 1))  # unavailable
        self.assertIsNone(_entry_at_row(entries, 0, 3, 10, 1))  # spacer
        self.assertIsNone(_entry_at_row(entries, 3, 7, 8, 1))  # footer/down marker


    def test_footer_fills_terminal_width(self):
        screen = FakeScreen(size=(7, 60))

        _draw(screen, [], 0, "", "")

        footer_last_columns = [call for call in screen.calls if call[0] == "chgat" and call[1] >= 5]
        self.assertEqual(footer_last_columns, [("chgat", 5, 59, 1, curses.A_BOLD | curses.A_REVERSE), ("chgat", 6, 59, 1, curses.A_BOLD | curses.A_REVERSE)])



    def test_filtering_uses_two_instruction_rows_with_ascii_fallback(self):
        for ascii_mode, expected in (
            (False, ["type to filter  backspace edit", "esc clear  ↵ switch"]),
            (True, ["type to filter  backspace edit", "esc clear  Enter switch"]),
        ):
            screen = FakeScreen(size=(7, 60))
            with self.subTest(ascii=ascii_mode), patch("mtmux.sidebar._ascii", return_value=ascii_mode):
                _draw(screen, [], 0, "ignored", "", filtering=True)
            footer = [call[3].rstrip() for call in screen.calls if call[0] == "addnstr" and call[1] >= 5]
            self.assertEqual(footer, expected)

    def test_footer_reuses_title_style_and_dims_inactive_pane(self):
        screen = FakeScreen(size=(7, 60))
        with patch.dict("mtmux.sidebar._COLOR", {"title": 123}, clear=True):
            _draw(screen, [], 0, "", "", dimmed=True)
        footer = [call for call in screen.calls if call[0] == "addnstr" and call[1] >= 5]
        self.assertTrue(all(call[5] == _fade(123) for call in footer))

    def test_footer_monochrome_fallback_is_bold_reverse_video(self):
        screen = FakeScreen(size=(7, 60))
        with patch.dict("mtmux.sidebar._COLOR", {}, clear=True):
            _draw(screen, [], 0, "", "")
        footer = [call for call in screen.calls if call[0] == "addnstr" and call[1] >= 5]
        self.assertTrue(all(call[5] & curses.A_BOLD and call[5] & curses.A_REVERSE for call in footer))

    def test_inline_creation_renders_host_text_footer_and_cursor(self):
        for host, label in (("", "laptop"), ("dev", "dev")):
            screen = FakeScreen(size=(7, 40))
            entries = [Entry(label, "host", host=host)]

            with self.subTest(host=host), patch("mtmux.sidebar._ascii", return_value=False):
                _draw(screen, entries, 0, "", "", creation_host=host, creation_text="work")

            row = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 1)
            self.assertIn("＋ " + label + " / new: work", row[3])
            footer = [call[3].rstrip() for call in screen.calls if call[0] == "addnstr" and call[1] >= 5]
            self.assertEqual(footer, ["Esc cancel · Enter create"])
            self.assertTrue(any(call[0] == "move" and call[1] == 1 for call in screen.calls))

    def test_inline_creation_ascii_and_narrow_long_text_keep_cursor_visible(self):
        screen = FakeScreen(size=(5, 16))

        with patch("mtmux.sidebar._ascii", return_value=True):
            _draw(
                screen, [Entry("long-host", "host", host="long-host")], 0, "", "",
                creation_host="long-host", creation_text="abcdefghijklmnop",
            )

        row = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 1)
        self.assertTrue(row[3].startswith("+ "))
        cursor = next(call for call in screen.calls if call[0] == "move")
        self.assertLess(cursor[2], 16)

    def test_read_key_gets_one_char_without_enter(self):
        screen = FakeScreen()

        self.assertEqual(_read_key(screen, "kill work? y/N"), ord("y"))

        self.assertEqual(screen.calls[2], ("addnstr", 4, 0, "kill work? y/N", 19))
        self.assertEqual(screen.calls[4], ("getch",))
        self.assertEqual(screen.calls[0], ("timeout", -1))
        self.assertEqual(screen.calls[-1], ("timeout", 50))

    def test_title_adds_terminal_icon_with_ascii_fallback(self):
        screen = FakeScreen(size=(5, 40))

        with patch("mtmux.sidebar._ascii", return_value=False):
            _draw(screen, [], 0, "ok", "")
        title = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertTrue(title[3].startswith("  mtmux"))

        screen = FakeScreen(size=(5, 40))
        with patch("mtmux.sidebar._ascii", return_value=True):
            _draw(screen, [], 0, "ok", "")
        title = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertTrue(title[3].startswith(" mtmux"))

    def test_draw_erases_without_forcing_full_repaint(self):
        screen = FakeScreen(size=(5, 40))

        _draw(screen, [], 0, "ok", "")

        self.assertEqual(screen.calls[0], ("erase",))
        self.assertNotIn(("clear",), screen.calls)

    def test_title_forces_terminal_line_redraw_after_count_changes(self):
        screen = FakeScreen(size=(5, 40))

        _draw(screen, [Entry("work", "session", Target("local", "work"))], 0, "", "", dimmed=True)

        title = next(i for i, call in enumerate(screen.calls) if call[0] == "addnstr" and call[1] == 0)
        redraw = screen.calls.index(("redrawln", 0, 1))
        self.assertLess(title, redraw)

    def test_normal_title_shows_brand_and_session_count(self):
        screen = FakeScreen(size=(5, 40))
        entries = [
            Entry("LOCAL", "header"),
            Entry("work", "session", Target("local", "work")),
            Entry("notes", "session", Target("local", "notes")),
            Entry("new local", "create", host=""),
        ]

        _draw(screen, entries, 1, "ok", "")

        title = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertEqual(title[3], "  mtmux" + " " * 22 + "2 sessions")

    def test_title_count_uses_singular_labels(self):
        screen = FakeScreen(size=(5, 40))
        entries = [Entry("work", "session", Target("local", "work"))]

        _draw(screen, entries, 0, "ok", "")
        normal = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertTrue(normal[3].endswith("1 session"))

        screen = FakeScreen(size=(6, 40))
        _draw(screen, entries, 0, "filtering", "work", filtering=True)
        filtering = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        filter_row = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 1)
        self.assertEqual(filtering[3].rstrip(), "  mtmux                         1 match")
        self.assertTrue(filter_row[3].startswith(" Filter: work"))

    def test_filter_uses_dedicated_full_width_row(self):
        screen = FakeScreen(size=(6, 40))
        entries = [Entry("work", "session", Target("local", "work"))]

        _draw(screen, entries, 0, "filtering", "work", filtering=True, adding=True)

        title = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        filter_row = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 1)
        self.assertNotIn("work", title[3])
        self.assertEqual(filter_row[3], " Filter: work" + " " * 27)
        self.assertEqual(filter_row[4], 40)
        self.assertIn(("move", 1, len(" Filter: work")), screen.calls)

    def test_session_rows_use_last_available_column(self):
        screen = FakeScreen(size=(7, 20))
        entry = Entry("x" * 40, "session", Target("local", "work"))

        _draw(screen, [entry], 0, "", "")

        row = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 1)
        self.assertEqual(row[4], 20)
        self.assertEqual(sidebar._cell_width(row[3]), 20)

    def test_empty_filter_has_visible_input_position(self):
        screen = FakeScreen(size=(6, 20))

        _draw(screen, [], 0, "filtering", "", filtering=True)

        filter_row = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 1)
        self.assertTrue(filter_row[3].startswith(" Filter: "))
        self.assertIn(("move", 1, len(" Filter: ")), screen.calls)

    def test_narrow_filter_drops_count_before_clipping_query(self):
        screen = FakeScreen(size=(5, 16))

        _draw(screen, [Entry("work", "session", Target("local", "work"))], 0, "filtering", "abcdefghij", filtering=True)

        title = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        filter_row = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 1)
        self.assertNotIn("abcdefghij", title[3])
        self.assertEqual(filter_row[3], " Filter: abcdef…")
        cursor = next(call for call in screen.calls if call[0] == "move")
        self.assertEqual(cursor, ("move", 1, 15))

    def test_title_colors_final_terminal_column(self):
        screen = FakeScreen(size=(5, 20))

        _draw(screen, [], 0, "ok", "")

        title = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertEqual(len(title[3]), 20)
        self.assertEqual(title[4], 20)

    def test_title_uses_configured_style_and_dims_inactive_pane(self):
        screen = FakeScreen(size=(5, 20))

        with patch.dict("mtmux.sidebar._COLOR", {"title": 123}, clear=True):
            _draw(screen, [], 0, "ok", "", dimmed=True)

        title = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertEqual(title[4], 20)
        self.assertEqual(title[5], _fade(123))

    def test_title_monochrome_fallback_is_bold_reverse_video(self):
        screen = FakeScreen(size=(5, 20))

        with patch.dict("mtmux.sidebar._COLOR", {}, clear=True):
            _draw(screen, [], 0, "ok", "")

        title = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertTrue(title[5] & curses.A_BOLD)
        self.assertTrue(title[5] & curses.A_REVERSE)

    def test_filter_key_updates_live_text(self):
        self.assertEqual(_filter_key("a", ord("b")), "ab")
        self.assertEqual(_filter_key("ab", 127), "a")
        self.assertIsNone(_filter_key("ab", 10))

    def test_bell_targets_combines_cockpit_local_and_remote_bells(self):
        discovered = snapshot(
            local=("notes",),
            local_bells=("notes",),
            remotes={"dev": source("ssh", ("chat",), ("chat",), "dev")},
        )
        self.assertEqual(
            _bell_targets(discovered, Target("local", "work")),
            {Target("local", "work"), Target("local", "notes"), Target("ssh", "chat", "dev")},
        )

    def test_draw_marks_matching_bell_target(self):
        screen = FakeScreen(size=(8, 40))
        target = Target("ssh", "work", "dev")

        _draw(screen, [Entry("work", "session", target)], 0, "", "", bell_targets={target})

        self.assertTrue(any(call[0] == "addnstr" and "work 🔔" in call[3] for call in screen.calls))

    def test_draw_does_not_mark_current_target_bell(self):
        screen = FakeScreen()
        target = Target("local", "work")

        _draw(screen, [Entry("work", "session", target)], 0, "", "", bell_targets={target}, current_target=target)

        self.assertFalse(any(call[0] == "addnstr" and "🔔" in call[3] for call in screen.calls))




    def test_run_sets_timeout_and_refreshes_on_timeout(self):
        screen = FakeScreen([-1, ord("q")])
        calls = []

        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", side_effect=lambda filter_text="", *_: calls.append(filter_text) or [Entry("work", "session", Target("local", "work"))]),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
        ):
            run(screen)

        self.assertIn(("timeout", 50), screen.calls)
        self.assertEqual(calls, [""])

    def test_idle_ui_ticks_do_not_redraw_sidebar(self):
        screen = FakeScreen([-1, -1, ord("q")])

        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=[Entry("work", "session", Target("local", "work"))]),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar._draw", return_value=2) as draw,
        ):
            run(screen)

        draw.assert_called_once()

    def test_rapid_ui_ticks_do_not_accelerate_cockpit_bell_polling(self):
        screen = FakeScreen([-1, -1, -1, ord("q")])
        poller = unittest.mock.Mock()
        poller.snapshot = snapshot()
        poller.tick.return_value = False

        with (
            patch("mtmux.sidebar.DiscoveryPoller", return_value=poller),
            patch("mtmux.sidebar.time.monotonic", side_effect=[0, 0.1, 0.49, 0.5]),
            patch("mtmux.sidebar.cockpit.bell_target", return_value=None) as bell_target,
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._current_target", return_value=None),
        ):
            run(screen)

        self.assertEqual(bell_target.call_count, 2)
        self.assertEqual(poller.tick.call_count, 4)

    def test_run_beeps_once_for_new_background_bell(self):
        screen = FakeScreen([-1, -1, ord("q")])
        target = Target("local", "work")

        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar.curses.beep") as beep,
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=[Entry("work", "session", target)]),
            patch("mtmux.sidebar._bell_targets", return_value={target}),
            patch("mtmux.sidebar._current_target", return_value=Target("local", "shell")),
        ):
            run(screen)

        beep.assert_called_once_with()

    def test_switching_away_from_ringing_current_target_does_not_beep(self):
        screen = FakeScreen([-1, ord("q")])
        ringing = Target("local", "a")
        current = Target("local", "b")

        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar.curses.beep") as beep,
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=[Entry("a", "session", ringing)]),
            patch("mtmux.sidebar._bell_targets", return_value={ringing}),
            patch("mtmux.sidebar._current_target", side_effect=[ringing, ringing, current]),
        ):
            run(screen)

        beep.assert_not_called()


    def test_single_click_selects_and_switches_session(self):
        entries = [
            Entry("LOCAL", "header"),
            Entry("one", "session", Target("local", "one")),
            Entry("two", "session", Target("local", "two")),
        ]
        screen = FakeScreen([curses.KEY_MOUSE, ord("q")], size=(8, 30))

        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar.curses.mousemask"),
            patch("mtmux.sidebar.curses.getmouse", return_value=(0, 0, 3, 0, curses.BUTTON1_CLICKED)),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=entries),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar.cockpit.switch") as switch,
        ):
            run(screen)

        target = Target("local", "two")
        switch.assert_called_once_with(target, "env -u TMUX tmux -T clipboard new-session -A -s two")

    def test_single_click_host_starts_inline_editor_and_creates_local_target(self):
        screen = FakeScreen([curses.KEY_MOUSE, ord("n"), ord("e"), ord("w"), 10, ord("q")], size=(8, 30))

        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar.curses.mousemask"),
            patch("mtmux.sidebar.curses.getmouse", return_value=(0, 0, 1, 0, curses.BUTTON1_CLICKED)),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=[Entry("laptop", "host", host="")]),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar.sessions.create") as create,
            patch("mtmux.sidebar.cockpit.switch"),
        ):
            run(screen)

        create.assert_called_once_with(Target("local", "new"))
        self.assertTrue(any(call[0] == "addnstr" and "new: new" in call[3] for call in screen.calls))

    def test_editor_pauses_navigation_and_esc_cancels(self):
        entries = [Entry("laptop", "host", host=""), Entry("dev", "host", host="dev")]
        selected = []
        screen = FakeScreen([10, curses.KEY_DOWN, 27, ord("q")])
        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=entries),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar._draw", side_effect=lambda _, __, index, *args: selected.append(index) or 1),
            patch("mtmux.sidebar.sessions.create") as create,
        ):
            run(screen)

        self.assertEqual(set(selected), {0})
        create.assert_not_called()

    def test_invalid_inline_name_stays_open_until_corrected(self):
        screen = FakeScreen([10, ord("bad name"[0]), ord(" "), 10, 127, 127, ord("x"), 10, ord("q")])
        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=[Entry("dev", "host", host="dev")]),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar.sessions.create") as create,
            patch("mtmux.sidebar.cockpit.switch"),
        ):
            run(screen)

        create.assert_called_once_with(Target("ssh", "x", "dev"))

    def test_wheel_scrolls_viewport_without_changing_selection(self):
        entries = [
            Entry("LOCAL", "header"),
            Entry("one", "session", Target("local", "one")),
            Entry("two", "session", Target("local", "two")),
        ]
        captured = []
        # j to move selection to entry 2, then wheel that no longer moves selection
        screen = FakeScreen([ord("j"), curses.KEY_MOUSE, 10, ord("q")], size=(8, 30))

        def draw_spy(*args, **kwargs):
            selected = args[2]
            scroll_offset = args[12] if len(args) > 12 else None
            captured.append((selected, scroll_offset))
            return 2

        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar.curses.mousemask"),
            patch("mtmux.sidebar.curses.getmouse", return_value=(0, 0, 0, 0, curses.BUTTON4_PRESSED)),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=entries),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar._draw", side_effect=draw_spy),
            patch("mtmux.sidebar.cockpit.switch") as switch,
            patch("mtmux.sidebar.sessions.attach_command", return_value="attach"),
        ):
            run(screen)

        # selection stays at index 2 ("two") after wheel, Enter switches to "two"
        target_two = Target("local", "two")
        switch.assert_called_once_with(target_two, "attach")

    def test_malformed_mouse_event_is_ignored(self):
        screen = FakeScreen([curses.KEY_MOUSE, ord("q")])
        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar.curses.mousemask"),
            patch("mtmux.sidebar.curses.getmouse", side_effect=[(0, 0, None, 0, None), curses.error()]),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=[Entry("work", "session", Target("local", "work"))]),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
        ):
            run(screen)

    def test_switching_tracked_entry_keeps_focus_in_tracked_section(self):
        old = Target("local", "old")
        target = Target("local", "work")
        current = [old]
        entries = [
            Entry("STARRED", "header"),
            Entry("work", "session", target, tracked=True),
            Entry("LOCAL", "header"),
            Entry("work", "session", target),
            Entry("old", "session", old),
        ]
        selected = []
        screen = FakeScreen([curses.KEY_UP, curses.KEY_UP, 10, ord("q")])

        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=entries),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", side_effect=lambda: current[0]),
            patch("mtmux.sidebar._draw", side_effect=lambda _, __, index, *args: selected.append(index) or 2),
            patch("mtmux.sidebar.cockpit.switch", side_effect=lambda *_: current.__setitem__(0, target)),
        ):
            run(screen)

        self.assertEqual(selected, [4, 3, 1, 1])







    def test_only_first_nine_tracked_entries_get_slots(self):
        favorites = [Target("local", f"session-{slot}") for slot in range(10)]

        tracked_entries = [entry for entry in _entries("", snapshot(), favorites) if entry.tracked]

        self.assertEqual([entry.shortcut_slot for entry in tracked_entries], [1, 2, 3, 4, 5, 6, 7, 8, 9, None])



    def test_section_divider_fills_width_and_has_ascii_fallback(self):
        with patch("mtmux.sidebar._ascii", return_value=False):
            unicode_line = _entry_lines(Entry("✱ STARRED", "section"), False, set(), None, 20)[0]
        with patch("mtmux.sidebar._ascii", return_value=True):
            ascii_line = _entry_lines(Entry("* STARRED", "section"), False, set(), None, 20)[0]

        self.assertEqual(unicode_line, "✱ STARRED " + "─" * 10)
        self.assertEqual(ascii_line, "* STARRED " + "-" * 10)
        self.assertTrue(ascii_line.isascii())

    def test_section_divider_truncates_safely_at_narrow_width(self):
        with patch("mtmux.sidebar._ascii", return_value=False):
            self.assertEqual(_entry_lines(Entry("ALL SESSIONS", "section"), False, set(), None, 5), ["ALL …"])

    def test_sections_are_non_selectable_and_mouse_ignores_them(self):
        entries = [
            Entry("✱ STARRED", "section"),
            Entry("work", "session", Target("local", "work"), tracked=True),
            Entry("ALL SESSIONS", "section"),
        ]

        self.assertEqual(sidebar._selectable(entries), [1])
        self.assertIsNone(_entry_at_row(entries, 1, 1, 8, 1))
        self.assertIsNone(_entry_at_row(entries, 1, 4, 8, 1))

    def test_section_attr_uses_mint_bold_and_inactive_dim(self):
        entry = Entry("STARRED", "section")
        with patch.dict("mtmux.sidebar._COLOR", {"section": 123 | curses.A_BOLD}, clear=True):
            active = _entry_attr(entry, False)
            inactive = _entry_attr(entry, False, True)

        self.assertEqual(active, 123 | curses.A_BOLD)
        self.assertEqual(inactive, active | curses.A_DIM)

    def test_section_attr_monochrome_fallback_is_bold(self):
        with patch.dict("mtmux.sidebar._COLOR", {}, clear=True):
            self.assertEqual(_entry_attr(Entry("STARRED", "section"), False), curses.A_BOLD)


    def test_local_discovery_error_is_visible(self):
        entries = _entries("", snapshot(local_available=False, local_error="permission denied"))

        self.assertTrue(any(entry.kind == "unavailable" and entry.label == "unavailable: permission denied" for entry in entries))

    def test_available_hosts_replace_create_rows_and_show_enter_affordance(self):
        with patch("mtmux.sidebar.socket.gethostname", return_value="laptop"), patch(
            "mtmux.sidebar._ascii", return_value=False
        ):
            entries = _entries("", snapshot(remotes={"dev": source("ssh", host="dev")}))

        hosts = [entry for entry in entries if entry.kind == "host"]
        self.assertEqual([(entry.label, entry.host) for entry in hosts], [("laptop", ""), ("dev", "dev")])
        self.assertFalse(any(entry.kind == "create" for entry in entries))
        self.assertEqual(_entry_lines(hosts[0], False, set(), None, 40), ["💻 laptop ＋"])

    def test_filtering_and_unavailable_hosts_are_not_selectable(self):
        filtered = _entries("work", snapshot(local=("work",), remotes={"dev": source("ssh", ("work",), host="dev")}))
        unavailable = _entries("", snapshot(local_available=False, remotes={"dev": None}))

        self.assertFalse(any(entry.kind == "host" for entry in filtered + unavailable))

    def test_ascii_headers_preserve_text_only_labels(self):
        with patch.dict("mtmux.sidebar.os.environ", {"MTMUX_ASCII": "1"}), patch(
            "mtmux.sidebar.socket.gethostname", return_value="laptop"
        ):
            entries = _entries("", snapshot(remotes={"dev": None}))

        self.assertEqual([entry.label for entry in entries if entry.kind == "host"], ["laptop"])
        self.assertEqual([entry.label for entry in entries if entry.kind == "header"], ["SSH dev"])

    def test_filter_hides_new_session_options(self):
        entries = _entries("work", snapshot(local=("work",), remotes={"dev": source("ssh", ("work",), host="dev")}))

        self.assertFalse(any(entry.kind == "create" for entry in entries))


    def test_add_titles_name_mode_and_filter_query(self):
        for query, filtering, expected in (
            ("", False, "mtmux / Add session"),
            ("work", True, "mtmux / Add session"),
        ):
            screen = FakeScreen(size=(5, 50))
            _draw(screen, [], 0, "", query, filtering=filtering, adding=True)
            title = next(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
            self.assertIn(expected, title)

    def test_add_entry_band_and_left_aligned_plus(self):
        entry = Entry("Add session", "add")
        with patch("mtmux.sidebar._ascii", return_value=False), patch.dict(
            "mtmux.sidebar._COLOR", {"add_entry": 123}, clear=True
        ):
            self.assertEqual(_entry_lines(entry, True, set(), None, 30), ["› ＋ Add session" + " " * 14])
            self.assertEqual(_entry_lines(entry, False, set(), None, 30), ["  ＋ Add session" + " " * 14])
            self.assertEqual(_entry_attr(entry, False), 123)
            self.assertEqual(_entry_attr(entry, False, True), _fade(123))

    def test_title_excludes_tracked_duplicates_and_stale_entries(self):
        screen = FakeScreen(size=(5, 40))
        target = Target("local", "work")
        entries = [Entry("work", "session", target, tracked=True), Entry("work", "session", target), Entry("gone", "session", Target("local", "gone"), unavailable_favorite=True, tracked=True)]

        _draw(screen, entries, 0, "ok", "")

        title = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertTrue(title[3].endswith("1 session"))

    def test_numbered_tracked_has_no_slot_in_entry_lines_in_unicode_and_ascii(self):
        entry = Entry(
            "work", "session", Target("local", "work"), host="laptop",
            tracked=True, shortcut_slot=3,
        )

        for ascii_mode, expected in ((False, "work"), (True, "work")):
            with self.subTest(ascii=ascii_mode), patch("mtmux.sidebar._ascii", return_value=ascii_mode):
                line = _entry_lines(entry, False, set(), None, 30)[0]
            self.assertEqual(line, expected)
            self.assertNotIn("✱", line)
            self.assertNotIn("3", line)

    def test_tracked_entry_draws_slot_badge_with_correct_attribute(self):
        tracked = Entry("work", "session", Target("local", "work"), host="laptop",
                        tracked=True, shortcut_slot=3)
        other = Entry("other", "session", Target("local", "other"))
        screen = FakeScreen(size=(7, 30))

        with patch.dict("mtmux.sidebar._COLOR", {"slot": 456, "local": 123}, clear=True):
            sidebar._draw_entries(
                screen, [other, tracked], 0, 5, 30, set(), None, dimmed=False, top=1,
            )

        slot_call = next(call for call in screen.calls
                         if call[0] == "addnstr" and call[3].startswith("["))
        self.assertEqual(slot_call[3], "[3]")
        self.assertEqual(slot_call[5], 456)
        line_call = next(call for call in screen.calls
                         if call[0] == "addnstr" and call[1] == slot_call[1] and not call[3].startswith("["))
        self.assertEqual(line_call[5], 123)

    def test_tracked_entries_render_session_then_source_without_raw_targets(self):
        local = Entry("dashboard", "session", Target("local", "dashboard"), host="laptop", tracked=True)
        remote = Entry("auth", "session", Target("ssh", "auth", "dev"), host="dev", tracked=True)

        with patch("mtmux.sidebar._ascii", return_value=False):
            self.assertEqual(_entry_lines(local, True, set(), None, 30), ["› dashboard", "  └─ laptop"])
            self.assertEqual(_entry_lines(remote, False, set(), None, 30), ["  auth", "  └─ @dev"])
        with patch("mtmux.sidebar._ascii", return_value=True):
            self.assertEqual(_entry_lines(local, True, set(), None, 30), ["> dashboard", "  `- laptop"])

        self.assertNotIn("local:", "".join(_entry_lines(local, True, set(), None, 30)))
        self.assertNotIn("ssh:", "".join(_entry_lines(remote, False, set(), None, 30)))

    def test_tracked_lines_truncate_session_and_metadata_and_keep_bell(self):
        entry = Entry("s" * 64, "session", Target("ssh", "s" * 64, "host"), host="h" * 64, tracked=True)

        with patch("mtmux.sidebar._ascii", return_value=False):
            lines = _entry_lines(entry, False, {entry.target}, None, 20)

        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("… 🔔"))
        self.assertTrue(lines[1].endswith("…"))
        self.assertTrue(all(len(line) <= 20 for line in lines))

    def test_ascii_tracked_metadata_and_ellipsis_are_ascii_only(self):
        entry = Entry("session-name", "session", Target("ssh", "session-name", "long-host"), host="long-host", tracked=True, unavailable_favorite=True)

        with patch("mtmux.sidebar._ascii", return_value=True):
            lines = _entry_lines(entry, True, set(), None, 24)

        self.assertTrue(lines[0].isascii())
        self.assertTrue(lines[1].isascii())
        self.assertIn("@", lines[1])
        self.assertIn("unavailable", lines[1])
        self.assertIn("...", "".join(lines))

    def test_active_duplicate_is_highlighted_in_tracked_and_all_sections(self):
        target = Target("local", "work")
        entries = [
            Entry("work", "session", target, tracked=True),
            Entry("work", "session", target),
        ]
        screen = FakeScreen(size=(7, 30))

        with patch.dict("mtmux.sidebar._COLOR", {"active": 123}, clear=True):
            _draw(screen, entries, 0, "ok", "", current_target=target)

        rows = [call for call in screen.calls if call[0] == "addnstr" and call[3].strip().endswith("work")]
        self.assertEqual([call[5] for call in rows], [123, 123])

    def test_active_tracked_styles_both_rows_and_both_rows_map_to_entry(self):
        target = Target("local", "work")
        entry = Entry("work", "session", target, host="laptop", tracked=True)
        screen = FakeScreen(size=(6, 30))

        with patch.dict("mtmux.sidebar._COLOR", {"active": 123}, clear=True):
            _draw(screen, [entry], 0, "ok", "", current_target=target)

        rows = [call for call in screen.calls if call[0] == "addnstr" and call[1] in (1, 2)]
        self.assertEqual([call[5] for call in rows], [123, 123])
        self.assertEqual(_entry_at_row([entry], 0, 1, 6, 1), 0)
        self.assertEqual(_entry_at_row([entry], 0, 2, 6, 1), 0)

    def test_viewport_budgets_two_rows_for_selected_tracked_entry(self):
        entries = [Entry("STARRED", "header"), Entry("work", "session", Target("local", "work"), tracked=True), Entry("LOCAL", "header")]

        start, end = _viewport(entries, 1, 6)

        self.assertLessEqual(start, 1)
        self.assertGreater(end, 1)
        self.assertLessEqual(sum(2 if entry.tracked else 1 for entry in entries[start:end]) + int(start > 0) + int(end < len(entries)), 4)

    def test_tracked_rows_have_no_tracked_glyph(self):
        target = Target("local", "work")
        entry = Entry("work", "session", target, host="laptop", tracked=True)
        for ascii_mode, pointer in ((False, "› work"), (True, "> work")):
            screen = FakeScreen(size=(7, 30))
            with self.subTest(ascii=ascii_mode), patch("mtmux.sidebar._ascii", return_value=ascii_mode):
                _draw(screen, [entry], 0, "ok", "")
            rendered = [call[3].rstrip() for call in screen.calls if call[0] == "addnstr"]
            self.assertIn(pointer, rendered)
            self.assertNotIn("✱", "".join(rendered))


    def test_uppercase_j_moves_selected_tracked_target_down_and_persists(self):
        first = Target("local", "first")
        second = Target("local", "second")
        screen = FakeScreen([ord("J"), ord("q")], size=(10, 40))
        selections = []

        with (
            patch("mtmux.sidebar.load_sessions", return_value=[first, second]),
            patch("mtmux.sidebar.save_sessions") as save,
            patch("mtmux.discovery.local_snapshot", return_value=source("local", ("first", "second"))),
            patch("mtmux.sidebar.load_hosts", return_value=[]),
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar._draw", side_effect=lambda _, entries, index, *args, **kwargs: selections.append(entries[index]) or 2),
        ):
            run(screen)

        save.assert_called_once_with((second, first))
        self.assertEqual(selections[-1].target, first)
        self.assertTrue(selections[-1].tracked)

    def test_failed_kill_shows_error_and_keeps_sidebar_open(self):
        screen = FakeScreen([ord("x"), ord("y"), ord("q")], size=(8, 60))
        target = Target("local", "work")

        with (
            patch("mtmux.discovery.local_snapshot", return_value=source("local", ("work",))),
            patch("mtmux.sidebar.load_sessions", return_value=[target]),
            patch("mtmux.sidebar.load_hosts", return_value=[]),
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=target),
            patch("mtmux.sidebar.sessions.kill", side_effect=SystemExit("kill local:work failed: denied")),
        ):
            run(screen)

        self.assertTrue(any(call[0] == "addnstr" and "kill local:work failed: denied" in call[3] for call in screen.calls))



    def test_rendered_rows_include_icons(self):
        screen = FakeScreen(size=(9, 30))

        with patch("mtmux.sidebar._ascii", return_value=False):
            _draw(screen, [Entry("laptop", "host", host=""), Entry("work", "session", Target("local", "work"))], 1, "ok", "")

        text = "\n".join(str(call) for call in screen.calls)
        self.assertIn("● work", text)
        self.assertIn("💻 laptop ＋", text)

    def test_selection_pointer_and_active_color_are_independent(self):
        active = Target("local", "active")
        selected = Target("local", "selected")
        entries = [Entry("active", "session", active), Entry("selected", "session", selected)]
        screen = FakeScreen(size=(7, 30))

        with patch.dict("mtmux.sidebar._COLOR", {"active": 123, "local": 45}, clear=True):
            _draw(screen, entries, 1, "ok", "", current_target=active)

        rows = {call[3].strip(): call for call in screen.calls if call[0] == "addnstr" and "session" not in call[3]}
        self.assertEqual(rows["● active"][5], 123)
        self.assertEqual(rows["› ● selected"][5], 45)

    def test_unfocused_sidebar_hides_pointer_and_keeps_active_reverse(self):
        active = Target("local", "active")
        selected = Target("local", "selected")
        screen = FakeScreen(size=(7, 30))

        with patch.dict("mtmux.sidebar._COLOR", {}, clear=True):
            _draw(screen, [Entry("active", "session", active), Entry("selected", "session", selected)], 1, "ok", "", current_target=active, dimmed=True)

        rows = [call for call in screen.calls if call[0] == "addnstr" and ("active" in call[3] or "selected" in call[3])]
        self.assertFalse(any("›" in call[3] for call in rows))
        active_row = next(call for call in rows if "active" in call[3])
        self.assertTrue(active_row[5] & curses.A_REVERSE)

    def test_unfocused_viewport_follows_active_target_not_offscreen_selection(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(10)]
        screen = FakeScreen(size=(5, 30))

        _draw(screen, entries, 9, "ok", "", current_target=entries[0].target, dimmed=True)

        rendered = [call[3] for call in screen.calls if call[0] == "addnstr"]
        self.assertTrue(any("0" in line for line in rendered))
        self.assertFalse(any("9" in line for line in rendered))

    def test_scrolling_keeps_selected_visible(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(10)]

        start, end = _viewport(entries, 9, 5)

        self.assertLessEqual(start, 9)
        self.assertLess(9, end)

    def test_selected_index_prefers_current_target(self):
        entries = [
            Entry("LOCAL", "header"),
            Entry("notes", "session", Target("local", "notes")),
            Entry("work", "session", Target("local", "work")),
        ]

        self.assertEqual(_selected_index(entries, Target("local", "work")), 2)

    def test_selected_index_prefers_session_over_earlier_host(self):
        entries = [
            Entry("laptop", "host", host=""),
            Entry("dev", "host", host="dev"),
            Entry("work", "session", Target("ssh", "work", "dev"), "dev"),
        ]

        self.assertEqual(_selected_index(entries, Target("local", "missing")), 2)




    def test_poller_closes_after_keyboard_interrupt(self):
        screen = FakeScreen()
        screen.getch = unittest.mock.Mock(side_effect=KeyboardInterrupt)
        poller = unittest.mock.Mock()
        poller.snapshot = snapshot()
        poller.tick.return_value = False

        with (
            patch("mtmux.sidebar.DiscoveryPoller", return_value=poller),
            patch("mtmux.sidebar.load_hosts", return_value=[]),
            patch("mtmux.sidebar.cockpit.bell_target", return_value=None),
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._current_target", return_value=None),
        ):
            with self.assertRaises(KeyboardInterrupt):
                run(screen)

        poller.close.assert_called_once_with()

class ShouldAutoCreateTest(unittest.TestCase):
    def test_should_auto_create_true_single_host_no_sessions(self):
        entries = [Entry("laptop", "host", host="")]
        self.assertTrue(_should_auto_create(entries))

    def test_should_auto_create_false_multiple_hosts(self):
        entries = [
            Entry("laptop", "host", host=""),
            Entry("dev", "host", host="dev"),
        ]
        self.assertFalse(_should_auto_create(entries))

    def test_should_auto_create_false_with_sessions(self):
        entries = [
            Entry("laptop", "host", host=""),
            Entry("work", "session", Target("local", "work")),
        ]
        self.assertFalse(_should_auto_create(entries))

    def test_enter_add_auto_creates_on_single_host(self):
        entry = Entry("laptop", "host", host="")
        screen = FakeScreen([10, ord("w"), ord("o"), 10, ord("q")])

        with (
            patch("mtmux.sidebar.curses.curs_set") as curs_set,
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=[Entry("Add session", "add"), entry]),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar.sessions.create") as create,
            patch("mtmux.sidebar.cockpit.switch"),
        ):
            run(screen)

        create.assert_called_once()
        target = create.call_args[0][0]
        self.assertEqual(target.session, "wo")
        self.assertEqual(target.kind, "local")
        curs_set.assert_any_call(1)

    def test_a_key_auto_creates_on_single_host(self):
        entry = Entry("laptop", "host", host="")
        screen = FakeScreen([ord("a"), ord("x"), 10, ord("q")])

        with (
            patch("mtmux.sidebar.curses.curs_set") as curs_set,
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=[entry]),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar.sessions.create") as create,
            patch("mtmux.sidebar.cockpit.switch"),
        ):
            run(screen)

        create.assert_called_once()
        target = create.call_args[0][0]
        self.assertEqual(target.session, "x")
        self.assertEqual(target.kind, "local")
        curs_set.assert_any_call(1)


class SidebarScrollOffsetTest(unittest.TestCase):
    def test_viewport_respects_scroll_offset(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(10)]

        start, end = _viewport(entries, 0, 8, scroll_offset=5)

        self.assertEqual(start, 5)
        self.assertLess(start, end)
        self.assertLessEqual(end, len(entries))

    def test_viewport_scroll_offset_none_uses_selection(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(10)]

        start, end = _viewport(entries, 9, 8)

        self.assertLessEqual(start, 9)
        self.assertLess(9, end)

    def test_scroll_offset_is_none_initially(self):
        state = SidebarState()
        self.assertIsNone(state.scroll_offset)

    def test_wheel_up_decrements_scroll_offset_not_selection(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(10)]
        captured = []

        def draw_spy(*args, **kwargs):
            selected = args[2]
            scroll_offset = args[12] if len(args) > 12 else None
            captured.append((selected, scroll_offset))
            return 2

        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, ord("q")], size=(8, 30))
        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar.curses.mousemask"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=entries),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar._draw", side_effect=draw_spy),
            patch("mtmux.sidebar.curses.getmouse", return_value=(0, 0, 0, 0, curses.BUTTON4_PRESSED)),
        ):
            run(screen)

        self.assertGreater(len(captured), 1)
        selected_values = {sel for sel, _ in captured}
        self.assertEqual(selected_values, {0})  # selection never changed
        offsets = [off for _, off in captured if off is not None]
        self.assertTrue(len(offsets) >= 1)  # scroll_offset was set

    def test_wheel_down_increments_scroll_offset_not_selection(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(10)]
        captured = []

        def draw_spy(*args, **kwargs):
            selected = args[2]
            scroll_offset = args[12] if len(args) > 12 else None
            captured.append((selected, scroll_offset))
            return 2

        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, ord("q")], size=(8, 30))
        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar.curses.mousemask"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=entries),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar._draw", side_effect=draw_spy),
            patch("mtmux.sidebar.curses.getmouse", return_value=(0, 0, 0, 0, curses.BUTTON5_PRESSED)),
        ):
            run(screen)

        self.assertGreater(len(captured), 1)
        selected_values = {sel for sel, _ in captured}
        self.assertEqual(selected_values, {0})  # selection never changed
        offsets = [off for _, off in captured if off is not None]
        self.assertTrue(len(offsets) >= 1)  # scroll_offset was set

    def test_j_key_resets_scroll_offset(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(5)]
        captured = []

        def draw_spy(*args, **kwargs):
            selected = args[2]
            scroll_offset = args[12] if len(args) > 12 else None
            captured.append((selected, scroll_offset))
            return 2

        screen = FakeScreen([
            curses.KEY_MOUSE,  # wheel to set scroll_offset
            ord("j"),          # j to reset scroll_offset
            ord("q"),
        ], size=(8, 30))
        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar.curses.mousemask"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=entries),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar._draw", side_effect=draw_spy),
            patch("mtmux.sidebar.curses.getmouse", return_value=(0, 0, 0, 0, curses.BUTTON4_PRESSED)),
        ):
            run(screen)

        # Final render should have scroll_offset=None (reset by j)
        self.assertIsNone(captured[-1][1])

    def test_k_key_resets_scroll_offset(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(5)]
        captured = []

        def draw_spy(*args, **kwargs):
            selected = args[2]
            scroll_offset = args[12] if len(args) > 12 else None
            captured.append((selected, scroll_offset))
            return 2

        screen = FakeScreen([
            curses.KEY_MOUSE,  # wheel to set scroll_offset
            ord("k"),          # k to reset scroll_offset
            ord("q"),
        ], size=(8, 30))
        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar.curses.mousemask"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=entries),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar._draw", side_effect=draw_spy),
            patch("mtmux.sidebar.curses.getmouse", return_value=(0, 0, 0, 0, curses.BUTTON4_PRESSED)),
        ):
            run(screen)

        self.assertIsNone(captured[-1][1])

    def test_enter_resets_scroll_offset(self):
        entries = [Entry("work", "session", Target("local", "work"))]
        captured = []

        def draw_spy(*args, **kwargs):
            selected = args[2]
            scroll_offset = args[12] if len(args) > 12 else None
            captured.append((selected, scroll_offset))
            return 2

        screen = FakeScreen([
            curses.KEY_MOUSE,  # wheel to set scroll_offset
            10,                # Enter to reset
            ord("q"),
        ], size=(8, 30))
        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar.curses.mousemask"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=entries),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar._draw", side_effect=draw_spy),
            patch("mtmux.sidebar.curses.getmouse", return_value=(0, 0, 0, 0, curses.BUTTON4_PRESSED)),
            patch("mtmux.sidebar.cockpit.switch"),
            patch("mtmux.sidebar.sessions.attach_command", return_value="attach"),
        ):
            run(screen)

        # After Enter, scroll_offset should be None (reset)
        final_offsets = [off for _, off in captured[-2:]]
        self.assertIn(None, final_offsets)


if __name__ == "__main__":
    unittest.main()
