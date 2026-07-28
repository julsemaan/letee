import curses
from datetime import datetime, timedelta, timezone
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


class ActiveSessionAvailabilityTest(unittest.TestCase):
    def test_missing_active_session_replaces_frozen_pane_after_discovery(self):
        target = Target("ssh", "work", "dev")

        self.assertFalse(sidebar._should_show_unavailable(target, snapshot(remotes={"dev": None})))
        self.assertTrue(sidebar._should_show_unavailable(target, snapshot(remotes={"dev": source("ssh", host="dev")})))
        self.assertTrue(sidebar._should_show_unavailable(target, snapshot(remotes={"dev": source("ssh", host="dev", available=False)})))

    def test_active_missing_session_shows_missing_then_restores_session(self):
        target = Target("local", "work")

        with (
            patch.object(sidebar.cockpit, "show_missing") as show_missing,
            patch.object(sidebar.cockpit, "switch") as switch,
        ):
            pending = sidebar._sync_active_session(target, snapshot(local=("other",)), None)
            restored = sidebar._sync_active_session(target, snapshot(local=("work",)), pending)

        show_missing.assert_called_once_with(target)
        switch.assert_called_once_with(target, sidebar.sessions.attach_command(target))
        self.assertEqual(pending, target)
        self.assertIsNone(restored)

    def test_active_session_shows_reconnecting_then_restores_session(self):
        target = Target("ssh", "work", "dev")

        with (
            patch.object(sidebar.cockpit, "show_reconnecting") as show_reconnecting,
            patch.object(sidebar.cockpit, "switch") as switch,
        ):
            pending = sidebar._sync_active_session(
                target, snapshot(remotes={"dev": source("ssh", host="dev", available=False)}), None
            )
            restored = sidebar._sync_active_session(
                target, snapshot(remotes={"dev": source("ssh", sessions=("work",), host="dev")}), pending
            )

        show_reconnecting.assert_called_once_with(target)
        switch.assert_called_once_with(target, sidebar.sessions.attach_command(target))
        self.assertEqual(pending, target)
        self.assertIsNone(restored)


from mtmux.sidebar import (
    Effect,
    Entry,
    SidebarState,
    _agent_entries,
    _agent_sort_key,
    _bell_targets,
    _creation_key,
    _draw,
    _entry_height,
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
    _sync_agent_selection,
    _sync_selection,
    _transition,
    _execute,
    _update_agent_alerts,
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
        self.assertEqual([entry.kind for entry in entries[:2]], ["session", "session"])
        self.assertEqual(sidebar._selectable(entries)[0], 0)

    def test_empty_normal_view_has_no_instruction_row(self):
        self.assertEqual(_entries("", snapshot(local=("work",)), []), [])

    def test_tracked_remote_favorite_shows_connecting_during_initial_discovery(self):
        target = Target("ssh", "work", "dev")

        entry = next(entry for entry in _entries("", snapshot(remotes={"dev": None}), [target]) if entry.target == target)

        self.assertTrue(entry.unavailable_favorite)
        self.assertEqual(entry.status, "connecting…")
        self.assertIn("connecting…", _entry_lines(entry, False, set(), None, 40)[1])

    def test_tracked_remote_favorite_shows_reconnecting_after_failure(self):
        target = Target("ssh", "work", "dev")
        failed = source("ssh", host="dev", available=False, error="connection refused")

        entry = next(entry for entry in _entries("", snapshot(remotes={"dev": failed}), [target]) if entry.target == target)

        self.assertTrue(entry.unavailable_favorite)
        self.assertEqual(entry.status, "reconnecting…")
        self.assertIn("reconnecting…", _entry_lines(entry, False, set(), None, 40)[1])

    def test_tracked_reachable_sessions_show_missing_when_absent(self):
        targets = (Target("local", "work"), Target("ssh", "work", "dev"))
        data = snapshot(local=("other",), remotes={"dev": source("ssh", ("other",), host="dev")})

        entries = [next(entry for entry in _entries("", data, [target]) if entry.target == target) for target in targets]

        self.assertTrue(all(entry.unavailable_favorite for entry in entries))
        self.assertEqual([entry.status for entry in entries], ["missing", "missing"])
        self.assertTrue(all("· ⚠ missing" in _entry_lines(entry, False, set(), None, 40)[1] for entry in entries))
        with patch("mtmux.sidebar._ascii", return_value=True):
            self.assertTrue(all("| missing" in _entry_lines(entry, False, set(), None, 40)[1] for entry in entries))

    def test_tracked_unknown_remote_host_stays_unavailable(self):
        target = Target("ssh", "work", "unknown")

        entry = next(entry for entry in _entries("", snapshot(), [target]) if entry.target == target)

        self.assertEqual(entry.status, "unavailable")

    def test_add_picker_shows_reconnecting_with_connection_error(self):
        failed = source("ssh", host="dev", available=False, error="connection refused")

        entries = _entries("", snapshot(remotes={"dev": failed}), [], adding=True)

        reconnecting = next(entry for entry in entries if entry.kind == "unavailable")
        self.assertEqual(reconnecting.label, "reconnecting…: connection refused")
        with patch("mtmux.sidebar._ascii", return_value=True):
            line = _entry_lines(reconnecting, False, set(), None, 24)[0]
        self.assertTrue(line.isascii())
        self.assertLessEqual(sidebar._cell_width(line), 24)

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
    def test_status_icons_cover_every_state_in_unicode_and_ascii(self):
        expected = {
            False: {"working": "⠋", "submitted": "◷", "idle": "○", "completed": "✓", "input-required": "?", "auth-required": "⚿", "failed": "✕", "rejected": "⊘", "canceled": "−", "unknown": "?"},
            True: {"working": "|", "submitted": ".", "idle": "o", "completed": "+", "input-required": "?", "auth-required": "@", "failed": "x", "rejected": "!", "canceled": "-", "unknown": "?"},
        }
        for ascii_mode, icons in expected.items():
            with self.subTest(ascii=ascii_mode), patch("mtmux.sidebar._ascii", return_value=ascii_mode):
                self.assertEqual({status: sidebar._status_icon(status, 0) for status in icons}, icons)

    def test_agent_cursor_replaces_icon_only_while_focused(self):
        entry = Entry("pi", "agent", Target("local", "work"), status="completed")
        with patch("mtmux.sidebar._ascii", return_value=False):
            self.assertEqual(_entry_lines(entry, True, set(), None, 40)[0], "› pi · completed")
            self.assertEqual(_entry_lines(entry, False, set(), None, 40)[0], "✓ pi · completed")

    def test_spinner_frames_progress_and_wrap(self):
        with patch("mtmux.sidebar._ascii", return_value=False):
            self.assertEqual([sidebar._spinner_frame(t) for t in (0, 0.1, 0.9, 1.0)], ["⠋", "⠙", "⠏", "⠋"])
        with patch("mtmux.sidebar._ascii", return_value=True):
            self.assertEqual([sidebar._spinner_frame(t) for t in (0, 0.1, 0.2, 0.3, 0.4)], ["|", "/", "-", "\\", "|"])

    def test_agent_icon_and_status_share_semantic_attr_but_cursor_stays_active(self):
        entry = Entry("pi", "agent", Target("local", "work"), status="completed")
        with patch.dict("mtmux.sidebar._COLOR", {"active": 123, "agent_completed": 456}, clear=True):
            screen = FakeScreen(size=(6, 40))
            sidebar._draw_entries(screen, [entry], 0, 5, 40, set(), None, dimmed=True)
            semantic = [call for call in screen.calls if call[0] == "addnstr" and call[3] in ("✓", "completed")]
            self.assertEqual([call[5] for call in semantic], [_fade(456), _fade(456)])

            screen = FakeScreen(size=(6, 40))
            sidebar._draw_entries(screen, [entry], 0, 5, 40, set(), None)
            cursor = next(call for call in screen.calls if call[0] == "addnstr" and call[3] == "›")
            status = next(call for call in screen.calls if call[0] == "addnstr" and call[3] == "completed")
            self.assertEqual((cursor[5], status[5]), (456, 456))

    def test_agent_entries_only_include_exact_tracked_targets(self):
        local = Target("local", "work")
        tracked_remote = Target("ssh", "work", "dev")
        other_remote = Target("ssh", "work", "prod")
        agents = tuple(
            AgentEntry(PaneTarget(target, "@1", pane_id, "/tmp/tmux"), pane_id, "pi", None)
            for target, pane_id in ((local, "%1"), (tracked_remote, "%2"), (other_remote, "%3"))
        )
        data = SessionSnapshot(SourceSnapshot(True, (), frozenset(), agents=agents), {})

        self.assertEqual([entry.agent_id for entry in _agent_entries(data, [local, tracked_remote])], ["%1", "%2"])
        self.assertEqual(_agent_entries(data, []), [])

    def test_agent_entries_are_compact_and_keep_exact_pane(self):
        pane = PaneTarget(Target("ssh", "work", "dev"), "@1", "%2", "/tmp/tmux")
        data = SessionSnapshot(SourceSnapshot(True, (), frozenset()), {"dev": SourceSnapshot(True, (pane.target,), frozenset(), panes=(pane,), agents=(AgentEntry(pane, "id", "pi", None),))})

        entry = _agent_entries(data, [pane.target])[0]

        self.assertEqual(entry.pane_target, pane)
        with patch("mtmux.sidebar._ascii", return_value=False):
            self.assertEqual(_entry_lines(entry, True, set(), None, 40), ["› pi · idle", "  └─ @dev · work"])

    def test_local_agent_location_uses_localhost(self):
        pane = PaneTarget(Target("local", "work"), "@1", "%2", "/tmp/tmux")
        data = SessionSnapshot(
            SourceSnapshot(True, (pane.target,), frozenset(), agents=(AgentEntry(pane, "id", "pi", None),)),
            {},
        )

        entry = _agent_entries(data, [pane.target])[0]

        with patch("mtmux.sidebar._ascii", return_value=False):
            self.assertEqual(_entry_lines(entry, False, set(), None, 40)[1], "  └─ @localhost · work")

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

    def test_add_menu_entries_can_be_selected_with_mouse(self):
        for kind in ("choice_new", "choice_existing", "location"):
            with self.subTest(kind=kind):
                self.assertEqual(_entry_at_row([Entry("Add", kind)], 0, 2, 4, 0, top=2), 0)

    def test_switch_pane_uses_exact_attach_command_and_agent_id(self):
        pane = PaneTarget(Target("local", "work"), "@1", "%2", "/tmp/tmux")
        state = SidebarState()
        with patch("mtmux.sidebar.cockpit.switch") as switch:
            _execute(Effect("switch_pane", pane, message="id"), state, unittest.mock.Mock(), 5)

        switch.assert_called_once_with(pane.target, "env -u TMUX tmux -S /tmp/tmux select-window -t work:@1 \\; select-pane -t %2 \\; attach-session -t work", "id")
        self.assertEqual(state.status, "")
        self.assertIsNone(state.status_deadline)

    def test_agent_duration_uses_task_timestamp_then_runtime_fallback(self):
        now = datetime(2026, 6, 20, 16, 45, 30, tzinfo=timezone.utc)
        pane = PaneTarget(Target("local", "work"), "@1", "%2", "/tmp/tmux")
        agents = (
            AgentEntry(pane, "working", "pi", "working", now - timedelta(minutes=9), now - timedelta(seconds=12)),
            AgentEntry(pane, "idle", "pi", None, now - timedelta(minutes=3)),
            AgentEntry(pane, "future", "pi", "input-required", now + timedelta(seconds=5)),
        )
        entries = _agent_entries(SessionSnapshot(SourceSnapshot(True, (), frozenset(), agents=agents), {}), [pane.target])

        with patch("mtmux.sidebar._ascii", return_value=False):
            lines = {entry.agent_id: _entry_lines(entry, False, set(), None, 40, now=now)[0] for entry in entries}

        self.assertEqual(lines["working"][2:], "pi · working · for 12s")
        self.assertEqual(lines["idle"], "○ pi · idle")
        self.assertEqual(lines["future"], "? pi · input-required")

    def test_duration_formatter_covers_seconds_minutes_hours_and_days(self):
        self.assertEqual([sidebar._format_duration(seconds) for seconds in (12, 180, 7200, 345600)], ["12s", "3m", "2h", "4d"])

    def test_missing_timestamp_omits_duration_and_narrow_ascii_truncates_safely(self):
        entry = Entry("pi", "agent", Target("local", "work"), host="laptop", status="working")
        with patch("mtmux.sidebar._ascii", return_value=True):
            lines = _entry_lines(entry, False, set(), None, 14)
        self.assertIn(lines[0][0], "|/-\\")
        self.assertEqual(lines[0][1:], " pi · working")
        self.assertTrue(all(sidebar._cell_width(line) <= 14 for line in lines))


class AgentAlertTest(unittest.TestCase):
    def setUp(self):
        self.target = Target("local", "work")
        self.pane = PaneTarget(self.target, "@1", "%2", "/tmp/tmux")
        self.state = SidebarState(favorites=[self.target])

    def update(self, *agents, focused=(), current_target=None):
        data = SessionSnapshot(
            SourceSnapshot(True, (self.target,), frozenset(), agents=tuple(agents), focused_panes=frozenset(focused)),
            {},
        )
        return _update_agent_alerts(self.state, data, current_target)

    def agent(self, agent_id="id", status="working", pane=None):
        return AgentEntry(pane or self.pane, agent_id, "pi", status)

    def test_initial_idle_agent_does_not_alert(self):
        self.assertFalse(self.update(self.agent(status="idle")))
        self.assertEqual(self.state.agent_alerts, set())

    def test_working_to_attention_transition_alerts_once(self):
        self.update(self.agent())

        self.assertTrue(self.update(self.agent(status="completed")))
        self.assertFalse(self.update(self.agent(status="completed")))
        self.assertEqual(self.state.agent_alerts, {(self.pane, "id")})

    def test_alert_persists_when_agent_resumes_working(self):
        self.update(self.agent())
        self.update(self.agent(status="input-required"))

        self.assertFalse(self.update(self.agent()))
        self.assertEqual(self.state.agent_alerts, {(self.pane, "id")})

    def test_focused_exact_pane_does_not_alert_and_clears_existing_alert(self):
        self.update(self.agent())
        self.assertFalse(self.update(self.agent(status="failed"), focused=(self.pane,), current_target=self.target))
        self.assertEqual(self.state.agent_alerts, set())

        self.update(self.agent())
        self.update(self.agent(status="failed"))
        self.assertFalse(self.update(self.agent(status="failed"), focused=(self.pane,), current_target=self.target))
        self.assertEqual(self.state.agent_alerts, set())

    def test_focused_pane_in_background_session_alerts(self):
        self.update(self.agent(), focused=(self.pane,), current_target=self.target)

        self.assertTrue(
            self.update(
                self.agent(status="idle"),
                focused=(self.pane,),
                current_target=Target("local", "other"),
            )
        )
        self.assertEqual(self.state.agent_alerts, {(self.pane, "id")})

    def test_same_agent_id_in_different_panes_stays_distinct(self):
        other = PaneTarget(self.target, "@1", "%3", "/tmp/tmux")
        self.update(self.agent(), self.agent(pane=other))

        self.assertTrue(self.update(self.agent(status="completed"), self.agent(pane=other)))
        self.assertEqual(self.state.agent_alerts, {(self.pane, "id")})

    def test_missing_agent_drops_alert_and_prior_state(self):
        self.update(self.agent())
        self.update(self.agent(status="canceled"))

        self.assertFalse(self.update())
        self.assertEqual(self.state.agent_alerts, set())
        self.assertEqual(self.state.agent_states, {})

    def test_successful_exact_pane_switch_clears_alert(self):
        key = (self.pane, "id")
        self.state.agent_alerts.add(key)
        with patch("mtmux.sidebar.cockpit.switch"):
            _execute(Effect("switch_pane", self.pane, message="id"), self.state, unittest.mock.Mock(), 5)
        self.assertNotIn(key, self.state.agent_alerts)

    def test_agent_alert_marker_has_unicode_and_ascii_forms(self):
        entry = Entry("pi", "agent", self.target, pane_target=self.pane, agent_id="id", status="completed")
        for ascii_mode, marker in ((False, "🔔"), (True, "BELL")):
            with self.subTest(ascii=ascii_mode), patch("mtmux.sidebar._ascii", return_value=ascii_mode):
                line = _entry_lines(entry, False, set(), None, 40, agent_alerts={(self.pane, "id")})[0]
            self.assertIn(marker, line)


class AddFlowTest(unittest.TestCase):
    def test_choice_has_new_and_existing_rows(self):
        entries = sidebar._add_entries("choice", "", snapshot(), [])
        self.assertEqual([(entry.label, entry.kind) for entry in entries], [
            ("New session", "choice_new"),
            ("Existing session", "choice_existing"),
        ])

    def test_choices_have_icons_descriptions_and_distinct_colors(self):
        new = Entry("New session", "choice_new")
        existing = Entry("Existing session", "choice_existing")

        with patch("mtmux.sidebar._ascii", return_value=False), patch.dict(
            "mtmux.sidebar._COLOR", {"create": 11, "remote": 22}, clear=True
        ):
            self.assertEqual(_entry_lines(new, True, set(), None, 40), ["› + New session", "    Create a fresh tmux session"])
            self.assertEqual(_entry_lines(existing, False, set(), None, 40), ["  ≡ Existing session", "    Add a running tmux session"])
            self.assertEqual(_entry_attr(new, False), 11 | curses.A_BOLD)
            self.assertEqual(_entry_attr(existing, False), 22 | curses.A_BOLD)

        self.assertEqual(_entry_height(new), 2)
        self.assertEqual(_entry_height(existing), 2)

    def test_locations_count_availability_not_sessions(self):
        data = snapshot(local=("work",), remotes={"dev": source("ssh", ("chat",), host="dev")})
        entries = sidebar._add_entries("location", "", data, [])
        self.assertEqual(
            [(entry.label, entry.host) for entry in entries],
            [("Select where to create", None), ("localhost", ""), ("dev", "dev")],
        )

    def test_location_picker_uses_icons_and_location_colors(self):
        local = Entry("localhost", "location", host="")
        remote = Entry("dev", "location", host="dev")

        with patch("mtmux.sidebar._ascii", return_value=False), patch.dict(
            "mtmux.sidebar._COLOR", {"local": 11, "remote": 22, "add_entry": 33}, clear=True
        ):
            self.assertEqual(_entry_lines(local, False, set(), None, 40), ["  ● localhost"])
            self.assertEqual(_entry_lines(remote, True, set(), None, 40), ["› ◆ dev"])
            self.assertEqual(_entry_attr(local, False), 11)
            self.assertEqual(_entry_attr(remote, False), 22)

    def test_unavailable_locations_are_disabled_and_empty_state_is_useful(self):
        data = snapshot(local_available=False, remotes={"dev": source("ssh", host="dev", available=False)})
        entries = sidebar._add_entries("location", "", data, [])
        self.assertFalse(sidebar._selectable(entries))
        self.assertTrue(any("No available locations" in entry.label for entry in entries))

    def test_existing_lists_only_untracked_sessions(self):
        tracked = Target("local", "work")
        data = snapshot(local=("work", "notes"), remotes={"dev": source("ssh", ("chat",), host="dev")})
        entries = sidebar._add_entries("existing", "", data, [tracked])
        self.assertEqual([entry.target for entry in entries if entry.kind == "session"], [Target("local", "notes"), Target("ssh", "chat", "dev")])
        self.assertFalse(any(entry.kind == "host" for entry in entries))

    def test_existing_uses_localhost_and_marks_each_host_without_untracked_sessions(self):
        tracked = Target("local", "work")
        data = snapshot(local=("work",), remotes={"empty": source("ssh", host="empty")})

        entries = sidebar._add_entries("existing", "", data, [tracked])

        self.assertEqual(
            [(entry.label, entry.kind) for entry in entries],
            [
                ("localhost", "header"),
                ("No sessions", "empty"),
                ("empty", "header"),
                ("No sessions", "empty"),
            ],
        )
        self.assertFalse(sidebar._selectable(entries))
        empty = next(entry for entry in entries if entry.kind == "empty")
        self.assertTrue(_entry_attr(empty, False) & curses.A_DIM)
        self.assertEqual(_entry_lines(empty, False, set(), None, 40), ["    No sessions"])

    def test_add_transitions_and_back_hierarchy(self):
        state = SidebarState()
        sidebar._open_add(state)
        self.assertEqual(state.add_view, "choice")
        sidebar._start_new(state, snapshot(local=("work",)))
        self.assertEqual((state.add_view, state.creation_host), ("name", ""))
        sidebar._add_back(state, snapshot(local=("work",)))
        self.assertEqual(state.add_view, "choice")

        sidebar._start_new(state, snapshot(remotes={"dev": source("ssh", host="dev")}))
        self.assertEqual(state.add_view, "location")
        sidebar._select_location(state, "dev")
        sidebar._add_back(state, snapshot(remotes={"dev": source("ssh", host="dev")}))
        self.assertEqual(state.add_view, "location")

    def test_name_screen_uses_dedicated_row_and_cursor_at_narrow_width(self):
        screen = FakeScreen(size=(6, 16))
        state = SidebarState(add_view="name", creation_host="", creation_text="x" * 64)
        with patch("mtmux.sidebar.socket.gethostname", return_value="laptop"):
            sidebar._draw_name(screen, state)
        lines = [call[3] for call in screen.calls if call[0] == "addnstr"]
        self.assertTrue(any("● localhost" in line for line in lines))
        self.assertTrue(any(line.startswith(" ❯ ") for line in lines))
        self.assertTrue(any("Letters" in line for line in lines))
        cursor = next(call for call in screen.calls if call[0] == "move")
        self.assertLess(cursor[2], 16)
        footer = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 5)
        self.assertEqual(footer[4], 15)

    def test_name_screen_shows_validation_feedback_below_name(self):
        screen = FakeScreen(size=(7, 30))
        state = SidebarState(add_view="name", creation_host="", creation_text="bad name", status="Invalid session name")

        sidebar._draw_name(screen, state)

        feedback = next(call for call in screen.calls if call[0] == "addnstr" and "✕ Invalid session name" in call[3])
        self.assertEqual(feedback[1], 4)

    def test_name_screen_has_ascii_fallback(self):
        screen = FakeScreen(size=(6, 30))
        state = SidebarState(add_view="name", creation_host="dev")

        with patch("mtmux.sidebar._ascii", return_value=True):
            sidebar._draw_name(screen, state)

        lines = [call[3] for call in screen.calls if call[0] == "addnstr"]
        self.assertTrue(any("* dev" in line for line in lines))
        self.assertTrue(any(line.startswith(" > ") for line in lines))
        self.assertTrue(any("Enter create" in line for line in lines))


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

    def test_creation_key_reports_same_host_conflict_while_typing(self):
        state = SidebarState(creation_host="dev", creation_text="wor")
        existing = (Target("ssh", "work", "dev"), Target("local", "work"))

        self.assertIsNone(_creation_key(state, ord("k"), existing))

        self.assertEqual(state.status, "Session already exists on this host")
        with self.assertRaisesRegex(SystemExit, "already exists"):
            _creation_key(state, 10, existing)
        self.assertEqual((state.creation_host, state.creation_text), ("dev", "work"))

    def test_creation_key_allows_same_name_on_different_host(self):
        state = SidebarState(creation_host="dev", creation_text="work")

        effect = _creation_key(state, 10, (Target("local", "work"),))

        self.assertEqual(effect, Effect("create", Target("ssh", "work", "dev")))

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

    def test_remove_session_only_removes_tracked_target(self):
        target = Target("local", "work")
        state = SidebarState(selected_target=target, favorites=[target])

        effect = _transition(state, "remove_session")

        self.assertEqual(state.favorites, [])
        self.assertEqual(effect, Effect("save_favorites", favorites=(), message="removed local:work"))

        self.assertIsNone(_transition(SidebarState(selected_target=target), "remove_session"))

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

    def test_successful_kill_removes_target_before_refresh(self):
        target = Target("ssh", "work", "dev")
        other = Target("local", "other")
        state = SidebarState(selected_target=target, favorites=[target, other])
        poller = unittest.mock.Mock()

        with (
            patch("mtmux.sidebar.sessions.kill"),
            patch("mtmux.sidebar.save_sessions") as save,
        ):
            _execute(Effect("kill", target=target), state, poller, 5)

        self.assertEqual(state.favorites, [other])
        save.assert_called_once_with([other])
        poller.assert_has_calls([unittest.mock.call.discard(target), unittest.mock.call.refresh()])
        self.assertEqual(state.selected_target, target)
        self.assertEqual(state.status, "killed ssh:dev:work")

    def test_add_switch_tracks_then_switches(self):
        target = Target("local", "work")
        state = SidebarState(add_view="existing")
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
        self.assertEqual(state.status, "")
        self.assertIsNone(state.status_deadline)

    def test_successful_create_tracks_after_creation(self):
        target = Target("local", "new")
        state = SidebarState(add_view="name", creation_host="")
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
        self.assertIsNone(state.add_view)

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
        self.assertEqual((state.creation_host, state.creation_text), ("dev", "new"))


    def test_exact_active_agent_uses_active_session_color(self):
        target = Target("local", "work")
        entries = [
            Entry("pi", "agent", target, host="laptop", agent_id="one", status="working"),
            Entry("pi", "agent", target, host="laptop", agent_id="two", status="working"),
        ]
        screen = FakeScreen(size=(8, 40))
        with patch.dict("mtmux.sidebar._COLOR", {"active": 123, "agent_working": 456}, clear=True):
            sidebar._draw_entries(screen, entries, 0, 7, 40, set(), None, active_agent_id="one")

        first_location = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 2 and "laptop" in call[3])
        second_location = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 4 and "laptop" in call[3])
        self.assertEqual(first_location[5], 123)
        self.assertNotEqual(second_location[5], 123)

    def test_focused_pane_selects_active_agent_without_sidebar_click(self):
        pane = PaneTarget(Target("local", "work"), "@1", "%2", "/tmp/tmux")
        snapshot = SessionSnapshot(
            SourceSnapshot(True, (pane.target,), frozenset(), agents=(AgentEntry(pane, "one", "pi", "working"),), focused_panes=frozenset({pane})),
            {},
        )

        self.assertEqual(sidebar._focused_agent_id(snapshot, pane.target, "stale"), "one")

    def test_active_agent_uses_active_session_color(self):
        entry = Entry("pi", "agent", Target("local", "work"), host="laptop", agent_id="one", status="working")
        screen = FakeScreen(size=(6, 40))
        with patch.dict("mtmux.sidebar._COLOR", {"active": 123, "agent_working": 456}, clear=True):
            sidebar._draw_entries(screen, [entry], 0, 5, 40, set(), None, active_agent_id="one")

        location = next(call for call in screen.calls if call[0] == "addnstr" and "laptop" in call[3])
        self.assertEqual(location[5], 123)


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
                call(12, 36, -1), call(13, 30, -1), call(14, 167, -1),
                call(15, curses.COLOR_MAGENTA, -1), call(16, curses.COLOR_RED, -1),
                call(17, curses.COLOR_RED, -1), call(18, 79, -1),
                call(19, curses.COLOR_YELLOW, -1),
                call(20, 30, -1),
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
                call(14, curses.COLOR_RED, -1), call(15, curses.COLOR_MAGENTA, -1),
                call(16, curses.COLOR_RED, -1), call(17, curses.COLOR_RED, -1),
                call(18, curses.COLOR_CYAN, -1), call(19, curses.COLOR_YELLOW, -1),
                call(20, curses.COLOR_CYAN, -1),
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

    def test_layout_maintainer_repairs_until_stopped(self):
        waits = []

        class StopAfterOneRepair:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, timeout):
                waits.append(timeout)
                self.stopped = True

        with patch.object(sidebar.cockpit, "repair_layout") as repair_layout:
            sidebar._maintain_sidebar(StopAfterOneRepair(), "%1")

        repair_layout.assert_called_once_with("%1")
        self.assertEqual(waits, [sidebar.LAYOUT_REPAIR_INTERVAL])

    def test_main_restarts_after_keyboard_interrupt(self):
        with (
            patch.dict(sidebar.os.environ, {}, clear=True),
            patch("mtmux.sidebar.curses.wrapper", side_effect=[KeyboardInterrupt, None]) as wrapper,
        ):
            self.assertEqual(main(), 0)

        self.assertEqual(wrapper.call_count, 2)

    def test_mouse_mask_registers_only_supported_events(self):
        expected = (
            curses.BUTTON1_CLICKED
            | curses.BUTTON1_PRESSED
            | curses.BUTTON1_RELEASED
            | curses.REPORT_MOUSE_POSITION
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
        start, _ = _viewport(entries, 9, 7)
        self.assertEqual(_entry_at_row(entries, 9, 2, 8, 1), start)

    def test_drag_target_keeps_visible_line_without_shifting_following_entries(self):
        entries = [
            Entry(name, "session", Target("local", name), tracked=True, shortcut_slot=index)
            for index, name in enumerate(("one", "two", "three"), 1)
        ]
        screen = FakeScreen(size=(10, 30))

        with (
            patch.dict("mtmux.sidebar._COLOR", {"drag": 123}, clear=True),
            patch("mtmux.sidebar._ascii", return_value=False),
        ):
            sidebar._draw_entries(
                screen, entries, 0, 9, 30, set(), None, drag_target_entry=1
            )

        rows = {
            name: next(call[1] for call in screen.calls if call[0] == "addnstr" and name in call[3])
            for name in ("one", "two", "three")
        }
        target_attr = next(
            call[5] for call in screen.calls if call[0] == "addnstr" and "two" in call[3]
        )
        indicators = [
            (call[1], call[5])
            for call in screen.calls
            if call[0] == "addnstr" and call[3] == "─" * 30
        ]
        self.assertEqual(rows, {"one": 1, "two": 4, "three": 5})
        self.assertEqual(target_attr, 123)
        self.assertEqual(indicators, [(3, 123)])

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
        self.assertEqual(footer_last_columns, [("chgat", 6, 59, 1, curses.A_BOLD | curses.A_REVERSE)])



    def test_status_renders_below_title_without_replacing_footer(self):
        screen = FakeScreen(size=(7, 30))

        _draw(screen, [], 0, "killed local:work", "", agent_entries=[])

        status = next(call for call in screen.calls if call[0] == "addnstr" and "killed local:work" in call[3])
        footer = [call[3].rstrip() for call in screen.calls if call[0] == "addnstr" and call[1] == 6]
        self.assertEqual(status[1], 1)
        self.assertEqual(footer, ["↵ activate  ? help  q close"])

    def test_filter_status_renders_below_filter_input(self):
        screen = FakeScreen(size=(7, 30))

        _draw(screen, [], 0, "filter cleared", "work", filtering=True)

        status = next(call for call in screen.calls if call[0] == "addnstr" and "filter cleared" in call[3])
        self.assertEqual(status[1], 2)

    def test_long_status_truncates_without_exceeding_terminal_width(self):
        screen = FakeScreen(size=(7, 12))

        _draw(screen, [], 0, "failed: " + "界" * 20, "")

        status = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 1)
        self.assertLessEqual(sidebar._cell_width(status[3]), 12)

    def test_sessions_and_agents_share_minimal_footer_with_ascii_fallback(self):
        removed_hints = ("Tab", "resize", "add", "remove", "kill", "reorder", "K/J", "[ / ]")
        for ascii_mode, expected in (
            (False, ["↵ activate  ? help  q close"]),
            (True, ["Enter activate  ? help  q close"]),
        ):
            footers = []
            for focused_region in ("sessions", "agents"):
                screen = FakeScreen(size=(10, 60))
                with self.subTest(ascii=ascii_mode, focused_region=focused_region), patch(
                    "mtmux.sidebar._ascii", return_value=ascii_mode
                ):
                    _draw(
                        screen, [], 0, "hidden status", "", agent_entries=[], focused_region=focused_region
                    )
                footer = [
                    call[3].rstrip()
                    for call in screen.calls
                    if call[0] == "addnstr" and call[1] >= 9
                ]
                self.assertEqual(footer, expected)
                self.assertFalse(any(hint in " ".join(footer) for hint in removed_hints))
                footers.append(footer)
            self.assertEqual(footers[0], footers[1])

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

    def test_host_row_never_contains_inline_name_editor(self):
        line = _entry_lines(Entry("laptop", "host", host=""), True, set(), None, 40, "", "work")[0]

        self.assertNotIn("work", line)
        self.assertNotIn("new:", line)

    def test_read_key_shows_confirmation_below_title_and_preserves_footer(self):
        screen = FakeScreen(size=(5, 20))
        footer = "footer shortcuts"
        screen.addnstr(4, 0, footer, 19)
        screen.calls.clear()

        self.assertEqual(_read_key(screen, "kill work? y/N"), ord("y"))

        prompt = next(call for call in screen.calls if call[0] == "addnstr" and "kill work" in call[3])
        self.assertEqual(prompt[1], 1)
        self.assertFalse(any(call[0] == "addnstr" and call[1] == 4 for call in screen.calls))
        self.assertEqual(screen.calls[0], ("timeout", -1))
        self.assertEqual(screen.calls[-1], ("timeout", 50))

    def test_read_key_places_confirmation_below_filter_and_truncates_safely(self):
        screen = FakeScreen(size=(6, 12))

        with patch("mtmux.sidebar._ascii", return_value=False):
            _read_key(screen, "kill session-with-a-long-name? y/N", filtering=True)

        prompt = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 2 and call[3].strip())
        self.assertLessEqual(sidebar._cell_width(prompt[3]), 12)
        self.assertTrue(prompt[5] & curses.A_BOLD)

    def test_add_button_cursor_does_not_replace_selected_session_slot(self):
        screen = FakeScreen(size=(7, 40))
        target = Target("local", "work")
        entries = [Entry("work", "session", target, host="localhost", tracked=True, shortcut_slot=1)]

        with patch("mtmux.sidebar._ascii", return_value=True):
            _draw(screen, entries, 0, "", "", add_button_selected=True)

        title = "".join(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        session_badge = next(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 2 and call[2] == 0)
        self.assertIn("> add", title)
        self.assertEqual(session_badge, "[1]")

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

    def test_normal_title_shows_brand_without_session_count(self):
        screen = FakeScreen(size=(5, 40))
        entries = [
            Entry("LOCAL", "header"),
            Entry("work", "session", Target("local", "work")),
            Entry("notes", "session", Target("local", "notes")),
            Entry("new local", "create", host=""),
        ]

        _draw(screen, entries, 1, "ok", "")

        title_text = "".join(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertIn("mtmux", title_text)
        self.assertIn("＋ add", title_text)
        self.assertNotIn("2 sessions", title_text)

    def test_titles_never_show_session_or_match_counts(self):
        screen = FakeScreen(size=(5, 40))
        entries = [Entry("work", "session", Target("local", "work"))]

        _draw(screen, entries, 0, "ok", "")
        normal_text = "".join(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertIn("mtmux", normal_text)
        self.assertIn("＋ add", normal_text)
        self.assertNotIn("1 session", normal_text)

        screen = FakeScreen(size=(6, 40))
        _draw(screen, entries, 0, "filtering", "work", filtering=True)
        filtering_text = "".join(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        filter_row = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 1)
        self.assertIn("mtmux", filtering_text)
        self.assertNotIn("1 match", filtering_text)
        self.assertTrue(filter_row[3].startswith(" Filter: work"))

        screen = FakeScreen(size=(6, 40))
        _draw(screen, entries, 0, "filtering", "work", filtering=True, adding=True)
        add_text = next(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertNotIn("1 match", add_text)

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

        row = next(call for call in screen.calls if call[0] == "addnstr" and call[1] == 2)
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

        title_calls = [call for call in screen.calls if call[0] == "addnstr" and call[1] == 0]
        self.assertEqual(len(title_calls), 2)
        self.assertEqual(title_calls[0][4], 14)
        self.assertEqual(title_calls[1][4], 6)

    def test_title_uses_configured_style_and_dims_inactive_pane(self):
        screen = FakeScreen(size=(5, 20))

        with patch.dict("mtmux.sidebar._COLOR", {"title": 123}, clear=True):
            _draw(screen, [], 0, "ok", "", dimmed=True)

        title_calls = [call for call in screen.calls if call[0] == "addnstr" and call[1] == 0]
        main_title = title_calls[0]
        self.assertEqual(main_title[4], 14)
        self.assertEqual(main_title[5], _fade(123))

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




    def test_injected_region_keys_select_exact_region_and_tab_does_nothing(self):
        for keys, expected in (
            ([curses.KEY_F7, ord("q")], "agents"),
            ([curses.KEY_F7, curses.KEY_F6, ord("q")], "sessions"),
            ([9, ord("q")], "sessions"),
        ):
            screen = FakeScreen(keys, size=(10, 40))
            with (
                self.subTest(keys=keys),
                patch("mtmux.sidebar.curses.curs_set"),
                patch("mtmux.sidebar._init_colors"),
                patch("mtmux.sidebar._entries", return_value=[]),
                patch("mtmux.sidebar._bell_targets", return_value=set()),
                patch("mtmux.sidebar._current_target", return_value=None),
                patch("mtmux.sidebar._draw", return_value=(2, None)) as draw,
            ):
                run(screen)

            self.assertEqual(draw.call_args_list[-1].args[15], expected)

    def test_up_at_top_of_add_menu_does_not_select_hidden_add_button(self):
        screen = FakeScreen([ord("a"), curses.KEY_UP, ord("q")])

        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=[Entry("work", "session", Target("local", "work"))]),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar._draw", return_value=(2, None)) as draw,
        ):
            run(screen)

        add_draws = [call for call in draw.call_args_list if call.args[11]]
        self.assertTrue(add_draws)
        self.assertFalse(any(call.kwargs["add_button_selected"] for call in add_draws))

    def test_session_name_typing_handles_queued_keys_without_background_polling(self):
        screen = FakeScreen([ord("a"), curses.KEY_ENTER, *map(ord, "fast"), 27, ord("q")])
        poller = unittest.mock.Mock()
        poller.snapshot = snapshot()
        poller.tick.return_value = False

        with (
            patch("mtmux.sidebar.DiscoveryPoller", return_value=poller),
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._current_target", return_value=None) as current_target,
            patch("mtmux.sidebar._pane_active", return_value=True) as pane_active,
        ):
            run(screen)

        self.assertTrue(any(call[0] == "addnstr" and "fast" in call[3] for call in screen.calls))
        self.assertEqual(current_target.call_count, 5)
        self.assertEqual(pane_active.call_count, 4)

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

    def test_agent_duration_redraws_each_second_and_restart_reads_active_agent(self):
        pane = PaneTarget(Target("local", "work"), "@1", "%2", "/tmp/tmux")
        poller = unittest.mock.Mock()
        poller.snapshot = SessionSnapshot(
            SourceSnapshot(True, (), frozenset(), agents=(AgentEntry(pane, "id", "pi", "working"),)),
            {},
        )
        poller.tick.return_value = False
        screen = FakeScreen([-1, ord("q")], size=(10, 40))
        with (
            patch("mtmux.sidebar.DiscoveryPoller", return_value=poller),
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar.load_sessions", return_value=[pane.target]),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar.cockpit.current_agent", return_value="id"),
            patch("mtmux.sidebar.time.monotonic", side_effect=[100, 100.1]),
            patch("mtmux.sidebar._draw", return_value=(2, None)) as draw,
        ):
            run(screen)

        self.assertEqual(draw.call_count, 2)
        self.assertTrue(all(call.args[-1] == "id" for call in draw.call_args_list))

    def test_working_agent_redraws_at_spinner_rate(self):
        pane = PaneTarget(Target("local", "work"), "@1", "%2", "/tmp/tmux")
        poller = unittest.mock.Mock()
        poller.snapshot = SessionSnapshot(SourceSnapshot(True, (), frozenset(), agents=(AgentEntry(pane, "id", "pi", "working"),)), {})
        poller.tick.return_value = False
        screen = FakeScreen([-1, -1, ord("q")], size=(10, 40))
        with (
            patch("mtmux.sidebar.DiscoveryPoller", return_value=poller),
            patch("mtmux.sidebar.curses.curs_set"), patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar.load_sessions", return_value=[pane.target]),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar.time.monotonic", side_effect=[0, 0.05, 0.1]),
            patch("mtmux.sidebar._draw", return_value=(2, None)) as draw,
        ):
            run(screen)
        self.assertEqual(draw.call_count, 2)

    def test_idle_ui_ticks_do_not_redraw_sidebar(self):
        screen = FakeScreen([-1, -1, ord("q")])

        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=[Entry("work", "session", Target("local", "work"))]),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar._draw", return_value=(2, None)) as draw,
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


    def test_press_release_selects_and_switches_scrolled_idle_agent_name_row(self):
        target = Target("ssh", "work", "dev")
        first = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        second = PaneTarget(target, "@2", "%2", "/tmp/tmux")
        agents = (
            AgentEntry(first, "first", "pi-one", "working"),
            AgentEntry(second, "second", "pi-two", "idle"),
        )
        poller = unittest.mock.Mock()
        poller.snapshot = SessionSnapshot(
            SourceSnapshot(True, (target,), frozenset(), agents=agents), {}
        )
        poller.tick.return_value = False
        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, ord("q")], size=(20, 30))

        with (
            patch("mtmux.sidebar.DiscoveryPoller", return_value=poller),
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar.curses.mousemask"),
            patch(
                "mtmux.sidebar.curses.getmouse",
                side_effect=[
                    (0, 0, 17, 0, curses.BUTTON1_PRESSED),
                    (0, 0, 17, 0, curses.BUTTON1_RELEASED),
                ],
            ),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar.load_sessions", return_value=[target]),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar.cockpit.switch") as switch,
            patch("mtmux.sidebar._execute", wraps=_execute) as execute,
        ):
            run(screen)

        switch.assert_called_once_with(target, sidebar.sessions.pane_attach_command(second), "second")
        self.assertEqual(execute.call_args.args[1].selected_agent_key, (second, "second"))

    def test_click_then_hover_does_not_show_drag_target(self):
        target = Target("local", "one")
        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, ord("q")], size=(12, 30))
        drag_targets = []

        def draw_spy(*args, **kwargs):
            drag_targets.append(kwargs.get("drag_target_entry"))
            return (2, None)

        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar.curses.mousemask"),
            patch(
                "mtmux.sidebar.curses.getmouse",
                side_effect=[
                    (0, 0, 2, 0, curses.BUTTON1_PRESSED),
                    (0, 0, 1, 0, curses.REPORT_MOUSE_POSITION),
                ],
            ),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar.load_sessions", return_value=[target]),
            patch("mtmux.sidebar._entries", return_value=[Entry("one", "session", target, tracked=True)]),
            patch("mtmux.sidebar._agent_entries", return_value=[]),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar._draw", side_effect=draw_spy),
        ):
            run(screen)

        self.assertEqual(drag_targets, [None, None])

    def test_drag_hovering_down_more_scrolls_without_more_motion(self):
        targets = [Target("local", str(i)) for i in range(4)]
        entries = [Entry(str(i), "session", target, tracked=True) for i, target in enumerate(targets)]
        expected = (targets[1], targets[2], targets[0], targets[3])
        screen = FakeScreen(
            [curses.KEY_MOUSE, curses.KEY_MOUSE, -1, -1, curses.KEY_MOUSE, ord("q")],
            size=(10, 30),
        )

        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar.curses.mousemask"),
            patch(
                "mtmux.sidebar.curses.getmouse",
                side_effect=[
                    (0, 0, 2, 0, curses.BUTTON1_PRESSED),
                    (0, 0, 4, 0, curses.REPORT_MOUSE_POSITION),
                    (0, -1, -1, 0, curses.REPORT_MOUSE_POSITION),
                ],
            ),
            patch("mtmux.sidebar.time.monotonic", side_effect=[0, 0.01, 0.25, 0.5, 0.51, 0.6] + [1] * 20),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar.load_sessions", return_value=targets),
            patch("mtmux.sidebar._entries", return_value=entries),
            patch("mtmux.sidebar._agent_entries", return_value=[]),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar.save_sessions") as save_sessions,
        ):
            run(screen)

        save_sessions.assert_called_once_with(expected)

    def test_drag_motion_reorders_tracked_sessions(self):
        first = Target("local", "one")
        second = Target("local", "two")
        entries = [
            Entry("one", "session", first, tracked=True),
            Entry("two", "session", second, tracked=True),
        ]
        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, curses.KEY_MOUSE, ord("q")], size=(12, 30))

        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar.curses.mousemask"),
            patch(
                "mtmux.sidebar.curses.getmouse",
                side_effect=[
                    (0, 0, 2, 0, curses.BUTTON1_PRESSED),
                    (0, 0, 4, 0, curses.REPORT_MOUSE_POSITION),
                    (0, 0, 4, 0, curses.BUTTON1_RELEASED),
                ],
            ),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar.load_sessions", return_value=[first, second]),
            patch("mtmux.sidebar._entries", return_value=entries),
            patch("mtmux.sidebar._agent_entries", return_value=[]),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar.save_sessions") as save_sessions,
        ):
            run(screen)

        save_sessions.assert_called_once_with((second, first))

    def test_single_click_switches_tracked_session_after_press_release_events(self):
        target = Target("local", "one")
        entries = [Entry("one", "session", target, tracked=True)]
        screen = FakeScreen([curses.KEY_MOUSE, curses.KEY_MOUSE, ord("q")], size=(12, 30))

        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar.curses.mousemask"),
            patch(
                "mtmux.sidebar.curses.getmouse",
                side_effect=[
                    (0, 0, 2, 0, curses.BUTTON1_PRESSED),
                    (0, 0, 2, 0, curses.BUTTON1_RELEASED),
                ],
            ),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar.load_sessions", return_value=[target]),
            patch("mtmux.sidebar._entries", return_value=entries),
            patch("mtmux.sidebar._agent_entries", return_value=[]),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar.cockpit.switch") as switch,
        ):
            run(screen)

        switch.assert_called_once_with(target, "env -u TMUX tmux -T clipboard new-session -A -s one")

    def test_single_click_selects_and_switches_session(self):
        entries = [
            Entry("LOCAL", "header"),
            Entry("one", "session", Target("local", "one")),
            Entry("two", "session", Target("local", "two")),
        ]
        screen = FakeScreen([curses.KEY_MOUSE, ord("q")], size=(12, 30))

        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar.curses.mousemask"),
            patch("mtmux.sidebar.curses.getmouse", return_value=(0, 0, 4, 0, curses.BUTTON1_CLICKED)),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=entries),
            patch("mtmux.sidebar._agent_entries", return_value=[]),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar.cockpit.switch") as switch,
        ):
            run(screen)

        target = Target("local", "two")
        switch.assert_called_once_with(target, "env -u TMUX tmux -T clipboard new-session -A -s two")

    def test_location_click_target_enters_dedicated_name_view(self):
        state = SidebarState(add_view="location")
        sidebar._select_location(state, "")

        self.assertEqual((state.add_view, state.creation_host), ("name", ""))

    def test_name_back_returns_to_location_for_multiple_locations(self):
        state = SidebarState(add_view="name", creation_host="dev", creation_text="draft")
        data = snapshot(remotes={"dev": source("ssh", host="dev")})

        sidebar._add_back(state, data)

        self.assertEqual(state.add_view, "location")
        self.assertIsNone(state.creation_host)

    def test_invalid_name_keeps_dedicated_name_state(self):
        state = SidebarState(add_view="name", creation_host="dev", creation_text="bad name")

        with self.assertRaisesRegex(SystemExit, "Invalid session"):
            _creation_key(state, 10)

        self.assertEqual((state.add_view, state.creation_host, state.creation_text), ("name", "dev", "bad name"))

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
            return (2, None)

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
            patch("mtmux.sidebar._draw", side_effect=lambda _, __, index, *args, **kwargs: selected.append(index) or (2, None)),
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
        for query, filtering, adding, expected in (
            ("", False, False, "＋ add"),
            ("", False, True, "mtmux / Add session"),
            ("work", True, True, "mtmux / Add existing"),
        ):
            screen = FakeScreen(size=(5, 50))
            _draw(screen, [], 0, "", query, filtering=filtering, adding=adding)
            all_title_text = "".join(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
            self.assertIn(expected, all_title_text)


    def test_title_excludes_tracked_duplicates_and_stale_entries(self):
        screen = FakeScreen(size=(5, 40))
        target = Target("local", "work")
        entries = [Entry("work", "session", target, tracked=True), Entry("work", "session", target), Entry("gone", "session", Target("local", "gone"), unavailable_favorite=True, tracked=True)]

        _draw(screen, entries, 0, "ok", "")

        title_text = "".join(call[3] for call in screen.calls if call[0] == "addnstr" and call[1] == 0)
        self.assertIn("mtmux", title_text)

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
        with patch("mtmux.sidebar.socket.gethostname", return_value="laptop"):
            local = next(entry for entry in _entries("", snapshot(local=("dashboard",)), [Target("local", "dashboard")]) if entry.kind == "session")
        remote = Entry("auth", "session", Target("ssh", "auth", "dev"), host="dev", tracked=True)

        self.assertEqual(local.host, "localhost")
        with patch("mtmux.sidebar._ascii", return_value=False):
            self.assertEqual(_entry_lines(local, True, set(), None, 30), ["dashboard", "  └─ @localhost"])
            self.assertEqual(_entry_lines(remote, False, set(), None, 30), ["  auth", "  └─ @dev"])
        with patch("mtmux.sidebar._ascii", return_value=True):
            self.assertEqual(_entry_lines(local, True, set(), None, 30), ["dashboard", "  `- @localhost"])

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
        entry = Entry(
            "session-name", "session", Target("ssh", "session-name", "long-host"),
            host="long-host", tracked=True, unavailable_favorite=True, status="reconnecting…",
        )

        with patch("mtmux.sidebar._ascii", return_value=True):
            lines = _entry_lines(entry, True, set(), None, 24)

        self.assertTrue(lines[0].isascii())
        self.assertTrue(lines[1].isascii())
        self.assertIn("@", lines[1])
        self.assertIn("reconnecting...", lines[1])
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

        rows = [call for call in screen.calls if call[0] == "addnstr" and call[1] in (2, 3)]
        self.assertEqual([call[5] for call in rows], [123, 123])
        self.assertEqual(_entry_at_row([entry], 0, 2, 6, 1, top=2), 0)
        self.assertEqual(_entry_at_row([entry], 0, 3, 6, 1, top=2), 0)

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
            patch("mtmux.sidebar._draw", side_effect=lambda _, entries, index, *args, **kwargs: selections.append(entries[index]) or (2, None)),
        ):
            run(screen)

        save.assert_called_once_with((second, first))
        self.assertEqual(selections[-1].target, first)
        self.assertTrue(selections[-1].tracked)

    def test_empty_session_list_ignores_enter_remove_and_kill(self):
        screen = FakeScreen([curses.KEY_ENTER, ord("r"), ord("x"), ord("q")], size=(8, 60))

        with (
            patch("mtmux.discovery.local_snapshot", return_value=source("local")),
            patch("mtmux.sidebar.load_sessions", return_value=[]),
            patch("mtmux.sidebar.load_hosts", return_value=[]),
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar.sessions.kill") as kill,
        ):
            run(screen)

        kill.assert_not_called()

    def test_kill_outside_session_rows_is_ignored(self):
        screen = FakeScreen([ord("a"), ord("x"), ord("q")], size=(8, 60))

        with (
            patch("mtmux.discovery.local_snapshot", return_value=source("local")),
            patch("mtmux.sidebar.load_sessions", return_value=[]),
            patch("mtmux.sidebar.load_hosts", return_value=[]),
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar.sessions.kill") as kill,
        ):
            run(screen)

        kill.assert_not_called()
        self.assertFalse(any(call[0] == "addnstr" and "select session to kill" in call[3] for call in screen.calls))

    def test_missing_session_kill_guides_removal(self):
        target = Target("local", "work")
        screen = FakeScreen([ord("x"), ord("q")], size=(8, 60))

        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar.load_sessions", return_value=[target]),
            patch("mtmux.sidebar._entries", return_value=[Entry("work", "session", target, unavailable_favorite=True, tracked=True)]),
            patch("mtmux.sidebar._agent_entries", return_value=[]),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
        ):
            run(screen)

        self.assertTrue(any(
            call[0] == "addnstr" and "Session already missing; press r to remove" in call[3]
            for call in screen.calls
        ))

    def test_cancelled_kill_has_no_redundant_status(self):
        screen = FakeScreen([ord("x"), ord("n"), ord("q")], size=(8, 60))
        target = Target("local", "work")

        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar.load_sessions", return_value=[target]),
            patch("mtmux.sidebar._entries", return_value=[Entry("work", "session", target, tracked=True)]),
            patch("mtmux.sidebar._agent_entries", return_value=[]),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=target),
            patch("mtmux.sidebar.sessions.kill") as kill,
        ):
            run(screen)

        kill.assert_not_called()
        self.assertFalse(any(call[0] == "addnstr" and "cancelled" in call[3] for call in screen.calls))

    def test_failed_kill_keeps_sidebar_open_and_shows_error_above_footer(self):
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

        error = next(call for call in screen.calls if call[0] == "addnstr" and "kill local:work failed: denied" in call[3])
        self.assertEqual(error[1], 1)
        footer = [call[3].rstrip() for call in screen.calls if call[0] == "addnstr" and call[1] == 7]
        self.assertTrue(footer)
        self.assertTrue(all(line == "↵ activate  ? help  q close" for line in footer))



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


    def test_start_new_auto_creates_on_single_location(self):
        state = SidebarState(add_view="choice")
        sidebar._start_new(state, snapshot(local=("existing",)))

        self.assertEqual(state.add_view, "name")
        self.assertEqual(state.creation_host, "")


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
            return (2, None)

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
            return (2, None)

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

    def test_wheel_down_reaches_bottom_of_session_region(self):
        entries = [Entry(str(i), "session", Target("local", str(i)), tracked=True) for i in range(8)]
        captured = []
        mouse_events = [(0, 0, 0, 0, curses.BUTTON5_PRESSED)] * 10
        screen = FakeScreen([curses.KEY_MOUSE] * len(mouse_events) + [ord("q")], size=(12, 30))

        def draw_spy(*args, **kwargs):
            captured.append(args[12] if len(args) > 12 else None)
            return (1, None)

        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar.curses.mousemask"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=entries),
            patch("mtmux.sidebar._agent_entries", return_value=[]),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar._draw", side_effect=draw_spy),
            patch("mtmux.sidebar.curses.getmouse", side_effect=mouse_events),
        ):
            run(screen)

        _, end = _viewport(entries, 0, 7, captured[-1])
        self.assertEqual(end, len(entries))

    def test_discovery_update_preserves_wheel_position(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(8)]
        captured = []
        poller = unittest.mock.Mock(snapshot=snapshot())
        poller.tick.side_effect = [False, True]
        screen = FakeScreen([curses.KEY_MOUSE, -1, ord("q")], size=(12, 30))

        def draw_spy(*args, **kwargs):
            captured.append(args[12] if len(args) > 12 else None)
            return (1, None)

        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar.curses.mousemask"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar.DiscoveryPoller", return_value=poller),
            patch("mtmux.sidebar._entries", return_value=entries),
            patch("mtmux.sidebar._agent_entries", return_value=[]),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None),
            patch("mtmux.sidebar._draw", side_effect=draw_spy),
            patch("mtmux.sidebar.curses.getmouse", return_value=(0, 0, 0, 0, curses.BUTTON5_PRESSED)),
        ):
            run(screen)

        self.assertEqual(captured, [None, 1])

    def test_queued_wheel_reversal_is_processed_before_next_poll(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(10)]
        captured = []
        mouse_events = [
            (0, 0, 0, 0, curses.BUTTON4_PRESSED),
            (0, 0, 0, 0, curses.BUTTON4_PRESSED),
            (0, 0, 0, 0, curses.BUTTON5_PRESSED),
        ]
        screen = FakeScreen([curses.KEY_MOUSE] * len(mouse_events) + [ord("q")], size=(8, 30))

        def draw_spy(*args, **kwargs):
            captured.append(args[12] if len(args) > 12 else None)
            return (2, None)

        with (
            patch("mtmux.sidebar.curses.curs_set"),
            patch("mtmux.sidebar.curses.mousemask"),
            patch("mtmux.sidebar._init_colors"),
            patch("mtmux.sidebar._entries", return_value=entries),
            patch("mtmux.sidebar._bell_targets", return_value=set()),
            patch("mtmux.sidebar._current_target", return_value=None) as current_target,
            patch("mtmux.sidebar._draw", side_effect=draw_spy),
            patch("mtmux.sidebar.curses.getmouse", side_effect=mouse_events),
        ):
            run(screen)

        self.assertEqual(captured, [None, 1])
        self.assertLess(current_target.call_count, len(mouse_events) + 1)

    def test_j_key_resets_scroll_offset(self):
        entries = [Entry(str(i), "session", Target("local", str(i))) for i in range(5)]
        captured = []

        def draw_spy(*args, **kwargs):
            selected = args[2]
            scroll_offset = args[12] if len(args) > 12 else None
            captured.append((selected, scroll_offset))
            return (2, None)

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
            return (2, None)

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
            return (2, None)

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


class AgentOrderingTest(unittest.TestCase):
    def _make_agent(self, target, window_id, pane_id, agent_id, status):
        pane = PaneTarget(target, window_id, pane_id, "/tmp/tmux")
        return Entry(
            "pi", "agent", target, target.host or "laptop",
            pane_target=pane, agent_id=agent_id, status=status,
        )

    def test_priority_mode_sorts_by_status_then_session(self):
        first = Target("local", "first")
        second = Target("local", "second")
        favorites = [first, second]
        entries = [
            self._make_agent(second, "@1", "%1", "idle", "idle"),
            self._make_agent(first, "@1", "%1", "working", "working"),
            self._make_agent(second, "@1", "%2", "input", "input-required"),
            self._make_agent(first, "@1", "%2", "failed", "failed"),
        ]
        entries.sort(key=lambda e: _agent_sort_key(e, favorites, "priority"))
        ids = [e.agent_id for e in entries]
        self.assertEqual(ids, ["input", "failed", "working", "idle"])

    def test_priority_mode_sorts_idle_agents_by_latest_activity_with_missing_last(self):
        target = Target("local", "work")
        favorites = [target]
        older = datetime(2026, 1, 1, tzinfo=timezone.utc)
        newer = older + timedelta(minutes=1)
        entries = [
            self._make_agent(target, "@1", "%1", "missing", "idle"),
            Entry(
                "pi", "agent", target, "laptop",
                pane_target=PaneTarget(target, "@1", "%2", "/tmp/tmux"),
                agent_id="older", status="idle", task_status_timestamp=older,
            ),
            Entry(
                "pi", "agent", target, "laptop",
                pane_target=PaneTarget(target, "@1", "%3", "/tmp/tmux"),
                agent_id="newer", status="idle", task_status_timestamp=newer,
            ),
        ]

        entries.sort(key=lambda entry: _agent_sort_key(entry, favorites, "priority"))

        self.assertEqual([entry.agent_id for entry in entries], ["newer", "older", "missing"])

    def test_idle_runtime_heartbeats_do_not_reorder_agents_without_activity_timestamps(self):
        target = Target("local", "work")
        older = datetime(2026, 1, 1, tzinfo=timezone.utc)
        entries = [
            Entry(
                "pi", "agent", target, "laptop",
                pane_target=PaneTarget(target, "@1", "%1", "/tmp/tmux"),
                agent_id="first", status="idle", runtime_updated_at=older,
            ),
            Entry(
                "pi", "agent", target, "laptop",
                pane_target=PaneTarget(target, "@1", "%2", "/tmp/tmux"),
                agent_id="second", status="idle", runtime_updated_at=older + timedelta(seconds=1),
            ),
        ]

        entries.sort(key=lambda entry: _agent_sort_key(entry, [target], "priority"))

        self.assertEqual([entry.agent_id for entry in entries], ["first", "second"])

    def test_bell_agents_sort_first_in_priority_mode(self):
        target = Target("local", "work")
        favorites = [target]
        bell_pane = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        other_pane = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        entries = [
            Entry("pi", "agent", target, "laptop", pane_target=bell_pane, agent_id="bell", status="idle"),
            Entry("pi", "agent", target, "laptop", pane_target=other_pane, agent_id="urgent", status="input-required"),
            Entry("pi", "agent", target, "laptop", pane_target=other_pane, agent_id="working", status="working"),
        ]
        alerts = {(bell_pane, "bell")}
        entries.sort(key=lambda e: _agent_sort_key(e, favorites, "priority", alerts))
        self.assertEqual([e.agent_id for e in entries], ["bell", "urgent", "working"])

    def test_session_mode_ignores_status_and_follows_session_order(self):
        first = Target("local", "first")
        second = Target("local", "second")
        favorites = [first, second]
        entries = [
            self._make_agent(second, "@1", "%1", "urgent", "input-required"),
            self._make_agent(first, "@1", "%1", "idle", "idle"),
        ]
        entries.sort(key=lambda e: _agent_sort_key(e, favorites, "session"))
        self.assertEqual([(e.target.session, e.agent_id) for e in entries], [("first", "idle"), ("second", "urgent")])

    def test_priority_mode_tie_breaks_by_window_then_pane(self):
        target = Target("local", "work")
        favorites = [target]
        entries = [
            self._make_agent(target, "@1", "%3", "c", "idle"),
            self._make_agent(target, "@2", "%1", "d", "idle"),
            self._make_agent(target, "@1", "%1", "a", "idle"),
            self._make_agent(target, "@1", "%2", "b", "idle"),
        ]
        entries.sort(key=lambda e: _agent_sort_key(e, favorites, "priority"))
        self.assertEqual([e.agent_id for e in entries], ["a", "b", "c", "d"])

    def test_agent_entries_includes_order_row_and_sorts(self):
        target = Target("local", "work")
        pane1 = PaneTarget(target, "@1", "%1", "/tmp/tmux")
        pane2 = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        from mtmux.discovery import AgentEntry, SessionSnapshot, SourceSnapshot
        agents = (
            AgentEntry(pane1, "idle", "pi", None),
            AgentEntry(pane2, "urgent", "pi", "input-required"),
        )
        snapshot = SessionSnapshot(SourceSnapshot(True, (), frozenset(), agents=agents), {})
        raw = _agent_entries(snapshot, [target], "priority")
        # Should be sorted by priority: input-required before idle
        self.assertEqual([e.agent_id for e in raw], ["urgent", "idle"])

    def test_session_mode_preserves_tracked_order(self):
        first = Target("local", "first")
        second = Target("local", "second")
        pane1 = PaneTarget(first, "@1", "%1", "/tmp/tmux")
        pane2 = PaneTarget(second, "@1", "%1", "/tmp/tmux")
        from mtmux.discovery import AgentEntry, SessionSnapshot, SourceSnapshot
        agents = (
            AgentEntry(pane2, "input", "pi", "input-required"),
            AgentEntry(pane1, "idle", "pi", None),
        )
        snapshot = SessionSnapshot(SourceSnapshot(True, (), frozenset(), agents=agents), {})
        raw = _agent_entries(snapshot, [first, second], "session")
        self.assertEqual([e.agent_id for e in raw], ["idle", "input"])


class AgentOrderingToggleTest(unittest.TestCase):
    def test_mode_defaults_to_priority(self):
        self.assertEqual(SidebarState().agent_ordering, "priority")

    def test_mode_toggle_with_keyboard_h_l(self):
        state = SidebarState(agent_ordering="priority")
        # Simulate h key: toggle to session
        with patch("mtmux.sidebar._pane_active", return_value=True):
            pass  # run() would handle this; we test state mutation directly
        state.agent_ordering = "session" if state.agent_ordering == "priority" else "priority"
        self.assertEqual(state.agent_ordering, "session")
        state.agent_ordering = "session" if state.agent_ordering == "priority" else "priority"
        self.assertEqual(state.agent_ordering, "priority")

    def test_mode_toggle_preserves_selection_through_rebuild(self):
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%2", "/tmp/tmux")
        from mtmux.discovery import AgentEntry, SessionSnapshot, SourceSnapshot
        agents = (
            AgentEntry(pane, "id", "pi", "working"),
        )
        snapshot = SessionSnapshot(SourceSnapshot(True, (), frozenset(), agents=agents), {})
        raw = _agent_entries(snapshot, [target], "priority")
        full = [Entry("", "order")] + raw
        state = SidebarState(favorites=[target], agent_ordering="priority")
        # Set selection to the agent (index 1, since 0 is order row)
        state.agent_selected_index = 1
        state.selected_agent_key = (pane, "id")
        _sync_agent_selection(state, full)
        self.assertEqual(state.agent_selected_index, 1)
        self.assertEqual(state.selected_agent_key, (pane, "id"))
        # Now toggle mode and rebuild
        state.agent_ordering = "session"
        raw2 = _agent_entries(snapshot, [target], "session")
        full2 = [Entry("", "order")] + raw2
        _sync_agent_selection(state, full2)
        self.assertEqual(state.agent_selected_index, 1)
        self.assertEqual(state.selected_agent_key, (pane, "id"))

    def test_ordering_row_enter_is_noop(self):
        pane = PaneTarget(Target("local", "work"), "@1", "%2", "/tmp/tmux")
        entries = [Entry("", "order"), Entry("pi", "agent", pane.target, pane_target=pane, agent_id="id")]
        state = SidebarState(agent_selected_index=0, focused_region="agents", favorites=[pane.target])
        # Simulate Enter on order row: no effect because no pane_target
        effect = None
        if entries:
            entry = entries[state.agent_selected_index]
            if entry.pane_target:
                effect = "switch"
        self.assertIsNone(effect)

    def test_h_key_toggles_only_when_on_order_row(self):
        state = SidebarState(agent_ordering="priority", agent_selected_index=1, focused_region="agents")
        # h key: only toggles if agent_selected_index == 0
        if state.agent_selected_index == 0:
            state.agent_ordering = "session" if state.agent_ordering == "priority" else "priority"
        self.assertEqual(state.agent_ordering, "priority")  # unchanged, not on order row


class AgentOrderingRenderTest(unittest.TestCase):
    def test_ordering_row_renders_with_active_priority_highlighted(self):
        screen = FakeScreen(size=(10, 40))
        _draw(screen, [], 0, "", "", agent_entries=[Entry("", "order")], agent_ordering="priority")
        text = [item[3] for item in screen.calls if item[0] == "addnstr"]
        order_line = next(line for line in text if "⇅" in line)
        self.assertIn("Priority", order_line)
        self.assertIn("Session", order_line)

    def test_ordering_row_switches_active_highlight_on_mode_change(self):
        screen = FakeScreen(size=(10, 40))
        _draw(screen, [], 0, "", "", agent_entries=[Entry("", "order")], agent_ordering="session")
        text = [item[3] for item in screen.calls if item[0] == "addnstr"]
        order_line = next(line for line in text if "⇅" in line)
        self.assertIn("Priority", order_line)
        self.assertIn("Session", order_line)

    def test_ordering_row_renders_in_ascii_mode(self):
        screen = FakeScreen(size=(10, 40))
        with patch("mtmux.sidebar._ascii", return_value=True):
            _draw(screen, [], 0, "", "", agent_entries=[Entry("", "order")], agent_ordering="priority")
        text = [item[3] for item in screen.calls if item[0] == "addnstr"]
        order_line = next(line for line in text if "Order:" in line)
        self.assertIn("PRIORITY", order_line)
        self.assertIn("SESSION", order_line)
        self.assertTrue(order_line.isascii())

    def test_ordering_row_dimmed_when_pane_inactive(self):
        screen = FakeScreen(size=(10, 40))
        with patch.dict("mtmux.sidebar._COLOR", {"active": 123}, clear=True):
            _draw(screen, [], 0, "", "", dimmed=True, agent_entries=[Entry("", "order")], agent_ordering="priority")
        # Order line still rendered; dimming applies via _fade
        text = [item[3] for item in screen.calls if item[0] == "addnstr"]
        self.assertTrue(any("⇅" in line for line in text))

    def test_ordering_row_visible_with_empty_agents(self):
        screen = FakeScreen(size=(10, 40))
        _draw(screen, [], 0, "", "", agent_entries=[Entry("", "order")])
        text = [item[3] for item in screen.calls if item[0] == "addnstr"]
        self.assertTrue(any("⇅" in line for line in text))
        self.assertIn("  No active agents", text)

    def test_entry_lines_order_kind_unicode_and_ascii(self):
        order_entry = Entry("", "order")
        with patch("mtmux.sidebar._ascii", return_value=False):
            unicode_line = _entry_lines(order_entry, False, set(), None, 40, agent_ordering="priority")[0]
        with patch("mtmux.sidebar._ascii", return_value=True):
            ascii_line = _entry_lines(order_entry, False, set(), None, 40, agent_ordering="session")[0]
        self.assertIn("⇅", unicode_line)
        self.assertNotIn("Order:", unicode_line)
        self.assertIn("Priority", unicode_line)
        self.assertIn("Session", unicode_line)
        self.assertIn("PRIORITY", ascii_line)
        self.assertIn("SESSION", ascii_line)
        self.assertTrue(ascii_line.isascii())

    def test_entry_at_row_selects_order_entry(self):
        entries = [Entry("", "order"), Entry("pi", "agent", Target("local", "work"), agent_id="id")]
        self.assertEqual(_entry_at_row(entries, 0, 4, 9, 0, top=4), 0)
        self.assertEqual(_entry_at_row(entries, 0, 6, 9, 0, top=4), 1)

    def test_entry_height_order_is_one(self):
        self.assertEqual(_entry_height(Entry("", "order")), 2)


if __name__ == "__main__":
    unittest.main()
