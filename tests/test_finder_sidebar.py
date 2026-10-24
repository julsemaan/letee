import curses
import subprocess
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from letee import sidebar, workspace
from letee.discovery import AgentEntry, SessionSnapshot, SourceSnapshot
from letee.names import PaneTarget, Target
from test_sidebar import FakeScreen, STOP


class FinderSidebarTest(unittest.TestCase):
    def data(self):
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%1", "/tmp/finder-test", session_id="$1",
                          window_active=True, pane_active=True)
        agent = AgentEntry(pane, "agent", "pi", "working")
        data = SessionSnapshot(SourceSnapshot(
            True, (target,), frozenset(), panes=(pane,), agents=(agent,), focused_panes=frozenset({pane}),
        ), {})
        return target, pane, data

    def run_sidebar(self, keys, mouse=(), *, state=None, tick=None, launch_error=None, popup_exit=None):
        target, _, data = self.data()
        screen = FakeScreen([*keys, STOP], size=(14, 40))
        poller = Mock(snapshot=data, current_target=target, current_agent=None,
                      pending_agent=None, bell_target=None, pane_active=True)
        poller.tick.side_effect = (lambda now: tick(now, poller)) if tick else (lambda now: False)
        publisher = Mock()
        with ExitStack() as stack:
            for name, value in {
                "AsyncStatusPoller": poller, "DiscoveryPoller": Mock(),
                "load_hosts": [], "load_sessions": [target], "_current_target": target,
                "_init_colors": None, "_mouse_mask": None,
            }.items():
                stack.enter_context(patch.object(sidebar, name, return_value=value))
            stack.enter_context(patch.object(sidebar.runtime, "WorkspacePublisher", return_value=publisher))
            stack.enter_context(patch.object(sidebar.curses, "curs_set"))
            stack.enter_context(patch.object(sidebar.curses, "beep"))
            stack.enter_context(patch.object(sidebar.curses, "getmouse", side_effect=mouse))
            stack.enter_context(patch.object(sidebar.cockpit, "sidebar_compact", return_value=False))
            layout = stack.enter_context(patch.object(sidebar.cockpit, "toggle_sidebar"))
            stack.enter_context(patch.object(sidebar.cockpit.tmux, "out", return_value=""))
            stack.enter_context(patch.object(sidebar.cockpit.tmux, "tmux"))
            stack.enter_context(patch.object(sidebar.cockpit, "_letee_command", return_value="letee find-work"))
            popen = stack.enter_context(patch.object(sidebar.subprocess, "Popen"))
            popen.return_value.poll.return_value = popup_exit
            popen.side_effect = launch_error
            if state is not None:
                stack.enter_context(patch.object(sidebar, "SidebarState", return_value=state))
            sidebar.run(screen)
        return screen, publisher, popen, layout

    def test_find_bounds_preserve_add_and_collapse_and_omit_narrow_headers(self):
        for ascii_mode in (True, False):
            with self.subTest(ascii=ascii_mode), patch.object(sidebar, "_ascii", return_value=ascii_mode):
                screen = FakeScreen(size=(14, 40))
                _, add_col = sidebar._draw_title(screen, 40, [], "")
                start, end = sidebar._find_bounds(40, add_col)
                self.assertGreater(start, 1)
                self.assertLess(end, add_col)
                self.assertTrue(any(call[0] == "addnstr" and call[2] == start and call[3] == "Find"
                                    for call in screen.calls))
                self.assertIsNone(sidebar._find_bounds(12, 7))
                screen.calls.clear()
                sidebar._draw_title(screen, 40, [], "", adding=True)
                self.assertFalse(any("Find" in str(call) for call in screen.calls))

    def test_find_click_launches_once_without_layout_changes_and_add_stays_separate(self):
        screen, publisher, popen, layout = self.run_sidebar(
            [curses.KEY_MOUSE] * 3,
            [(0, 29, 0, 0, curses.BUTTON1_PRESSED),
             (0, 29, 0, 0, curses.BUTTON1_PRESSED),
             (0, 38, 0, 0, curses.BUTTON1_PRESSED)],
        )
        popen.assert_called_once_with(
            ["letee", "find-work"], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        popen.return_value.terminate.assert_not_called()
        popen.return_value.wait.assert_not_called()
        layout.assert_not_called()
        self.assertTrue(any(call[0] == "addnstr" and "Add session" in str(call) for call in screen.calls))
        publisher.close.assert_called_once_with()

    def test_publishes_canonical_initial_workspace_and_does_not_rebuild_on_idle_redraw(self):
        target, _, data = self.data()
        with patch.object(sidebar.workspace, "build_workspace", wraps=workspace.build_workspace) as build:
            _, publisher, _, _ = self.run_sidebar([-1, -1])
        self.assertEqual(publisher.publish.call_args_list[0].args[0], workspace.build_workspace(data, [target], target))
        self.assertEqual(build.call_count, 1)

    def test_publishes_hidden_agents_alerts_tracking_and_current_changes(self):
        target, pane, _ = self.data()
        state = sidebar.SidebarState(favorites=[target], hidden_agents={(pane, "agent")})
        count = 0

        def tick(now, poller):
            nonlocal count
            count += 1
            if count == 2:
                state.hidden_agents.clear()
                state.agent_alerts.add((pane, "agent"))
                poller.bell_target = target
            elif count == 3:
                state.favorites.clear()
            return False

        _, publisher, _, _ = self.run_sidebar([-1] * 10, state=state, tick=tick)
        values = [call.args[0] for call in publisher.publish.call_args_list]
        self.assertFalse(values[0].sessions[0].windows[0].agents)
        self.assertTrue(any(value.sessions and value.sessions[0].windows[0].agents[0].alert
                            for value in values if value.sessions and value.sessions[0].windows[0].agents))
        self.assertTrue(any(value.sessions and value.sessions[0].bell for value in values))
        self.assertEqual(values[-1].sessions, ())
        self.assertIsNone(values[-1].current)

    def test_current_marker_and_disconnected_session_keep_canonical_identity(self):
        target, _, _ = self.data()
        count = 0

        def tick(now, poller):
            nonlocal count
            count += 1
            if count == 2:
                poller.current_agent = "agent"
            elif count == 3:
                poller.current_target = None
            elif count == 4:
                poller.snapshot = SessionSnapshot(SourceSnapshot(False, (), frozenset()), {})
            return False

        _, publisher, _, _ = self.run_sidebar([-1] * 10, tick=tick)
        values = [call.args[0] for call in publisher.publish.call_args_list]
        self.assertEqual(values[0].current, values[0].sessions[0].key + ("@1",))
        self.assertTrue(any(value.current == value.sessions[0].key + ("@1", "%1", "agent")
                            for value in values))
        self.assertIsNone(values[-1].current)
        self.assertFalse(values[-1].sessions[0].available)
        self.assertEqual(values[-1].sessions[0].target, target)
        self.assertEqual(values[-1].sessions[0].key, values[0].sessions[0].key)

    def test_finished_popup_is_reaped_and_find_can_launch_again(self):
        _, _, popen, _ = self.run_sidebar(
            [curses.KEY_MOUSE] * 2,
            [(0, 29, 0, 0, curses.BUTTON1_PRESSED)] * 2, popup_exit=0,
        )
        self.assertEqual(popen.call_count, 2)
        popen.return_value.wait.assert_not_called()

    def test_find_launch_error_is_visible_status_and_cleanup_still_runs(self):
        screen, publisher, _, layout = self.run_sidebar(
            [curses.KEY_MOUSE], [(0, 29, 0, 0, curses.BUTTON1_PRESSED)],
            launch_error=OSError("cannot launch"),
        )
        self.assertTrue(any("Could not open Find: cannot" in str(call) for call in screen.calls))
        publisher.close.assert_called_once_with()
        layout.assert_not_called()

    def test_compact_render_does_not_draw_find(self):
        screen = FakeScreen(size=(14, 5))
        with patch.object(sidebar.cockpit.tmux, "out", return_value=""):
            sidebar._draw(screen, [], 0, "", "", compact=True)
        self.assertFalse(any("Find" in str(call) for call in screen.calls))
