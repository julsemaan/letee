from dataclasses import replace
import curses
import unittest
from unittest.mock import Mock, patch

from finder_fixtures import sample_workspace
from letee import finder_ui, tmux
from letee.finder import Finder
from letee.names import WindowTarget
from letee.workspace import Workspace


class FinderWorkerTest(unittest.TestCase):
    def rows(self):
        workspace = sample_workspace()
        finder = Finder(workspace)
        finder.set_query("letee")
        return workspace, finder.rows

    def test_prepare_does_not_navigate_and_commit_uses_shared_opening(self):
        workspace, rows = self.rows()
        for kind in ("session", "window", "agent"):
            row = next(row for row in rows if row.kind == kind)
            prepare = {"session": "existing_attach_command", "window": "window_attach_command",
                       "agent": "existing_pane_attach_command"}[kind]
            with self.subTest(kind=kind), patch.object(finder_ui.sessions, prepare, return_value="prepared") as attach, \
                    patch.object(finder_ui.cockpit, "open_" + kind) as navigate:
                commit = finder_ui._prepare_destination(row, lambda: workspace)
                attach.assert_called_once()
                navigate.assert_not_called()
                commit()
                if kind == "session":
                    attach.assert_called_once_with(row.session.target, row.session.session_id, row.session.socket_path)
                    navigate.assert_called_once_with(row.session.target, "prepared")
                elif kind == "window":
                    window = WindowTarget(row.session.target, row.session.session_id,
                                          row.window.window_id, row.session.socket_path)
                    attach.assert_called_once_with(window)
                    navigate.assert_called_once_with(window, "prepared")
                else:
                    attach.assert_called_once_with(row.agent.pane_target)
                    navigate.assert_called_once_with(row.agent.pane_target, row.agent.agent_id, "prepared")

    def test_vanished_or_unavailable_target_never_commits(self):
        workspace, rows = self.rows()
        row = rows[0]
        for latest in (Workspace(), replace(workspace, sessions=(replace(row.session, available=False),))):
            with self.subTest(latest=latest), patch.object(finder_ui.sessions, "existing_attach_command", return_value="prepared"), \
                    patch.object(finder_ui.cockpit, "open_session") as navigate:
                commit = finder_ui._prepare_destination(row, lambda: latest)
                with self.assertRaisesRegex(SystemExit, "unavailable"):
                    commit()
                navigate.assert_not_called()

    def test_rename_during_preparation_uses_latest_display_target(self):
        workspace, rows = self.rows()
        row = rows[0]
        renamed = replace(row.session, target=replace(row.session.target, session="renamed"))
        latest = replace(workspace, sessions=(renamed,))
        with patch.object(finder_ui.sessions, "existing_attach_command", return_value="stable-id-command"), \
                patch.object(finder_ui.cockpit, "open_session") as navigate:
            commit = finder_ui._prepare_destination(row, lambda: latest)
            commit()
        navigate.assert_called_once_with(renamed.target, "stable-id-command")

    def test_worker_restores_and_saves_scope_when_interrupted(self):
        workspace = sample_workspace()
        reader = Mock(current=workspace, read=Mock(return_value=None))
        screen = Mock()
        screen.get_wch.side_effect = KeyboardInterrupt
        popup = Mock(finder=Finder(workspace))

        def wrapper(run, workspace, **kwargs):
            return run(screen, workspace, **kwargs)

        with patch.object(finder_ui.runtime, "WorkspaceReader", return_value=reader), \
                patch.object(finder_ui.locale, "setlocale"), \
                patch.object(finder_ui.curses, "wrapper", side_effect=wrapper), \
                patch.multiple(curses, get_escdelay=Mock(return_value=1000), set_escdelay=Mock(),
                               curs_set=Mock(return_value=1), mousemask=Mock(return_value=(0, 0))), \
                patch.object(finder_ui.tui, "init_colors"), \
                patch.object(finder_ui, "_Popup", return_value=popup), \
                patch.object(tmux, "out", return_value="Agents") as read, \
                patch.object(tmux, "tmux") as save:
            self.assertEqual(finder_ui.main(), 0)
            self.assertEqual(popup.finder.scope, "Agents")
            read.assert_called_once_with("show-options", "-qv", "-t", tmux.SESSION, "@letee_finder_scope")
            save.assert_called_once_with(
                "set-option", "-t", tmux.SESSION, "@letee_finder_scope", "Agents", capture=True)
            reader.close.assert_called_once()

    def test_worker_uses_only_snapshot_reader_and_always_closes_it(self):
        workspace = sample_workspace()
        reader = Mock(current=workspace)
        with patch.object(finder_ui.runtime, "WorkspaceReader", return_value=reader), \
                patch.object(finder_ui.locale, "setlocale"), \
                patch.object(finder_ui.curses, "wrapper", return_value=False) as wrapper:
            self.assertEqual(finder_ui.main(), 0)
            wrapper.assert_called_once()
            self.assertIs(wrapper.call_args.args[0], finder_ui.run)
            self.assertEqual(wrapper.call_args.args[1], workspace)
            self.assertEqual(wrapper.call_args.kwargs["read_workspace"], reader.read)
            reader.close.assert_called_once()
        with patch.object(finder_ui.runtime, "WorkspaceReader", return_value=reader), \
                patch.object(finder_ui.locale, "setlocale"), \
                patch.object(finder_ui.curses, "wrapper", side_effect=KeyboardInterrupt):
            self.assertEqual(finder_ui.main(), 0)
            self.assertEqual(reader.close.call_count, 2)
