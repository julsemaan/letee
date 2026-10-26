from dataclasses import replace
import curses
import os
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from finder_fixtures import sample_workspace
from letee import cockpit, config, finder_ui, operations, tmux
from letee.finder import Finder
from letee.names import WindowTarget
from letee.discovery import SessionSnapshot, SourceSnapshot
from letee.sidebar import SessionReconciler
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
            identity = dict(session_id=row.session.session_id, socket_path=row.session.socket_path,
                            window_id=row.window.window_id if row.window else "")
            prepare = {"session": "existing_attach_command", "window": "window_attach_command",
                       "agent": "existing_pane_attach_command"}[kind]
            with self.subTest(kind=kind), patch.object(finder_ui.sessions, prepare, return_value="prepared") as attach, \
                    patch.object(operations, "navigate") as navigate:
                commit = finder_ui._prepare_destination(row, lambda: workspace)
                attach.assert_called_once()
                navigate.assert_not_called()
                commit()
                if kind == "session":
                    attach.assert_called_once_with(row.session.target, row.session.session_id, row.session.socket_path)
                    navigate.assert_called_once_with(row.session.target, attach_command="prepared", blocking=False, cancelled=None, **identity)
                elif kind == "window":
                    window = WindowTarget(row.session.target, row.session.session_id,
                                          row.window.window_id, row.session.socket_path)
                    attach.assert_called_once_with(window)
                    navigate.assert_called_once_with(row.session.target, attach_command="prepared", blocking=False, cancelled=None, **identity)
                else:
                    attach.assert_called_once_with(row.agent.pane_target)
                    navigate.assert_called_once_with(row.agent.pane_target, attach_command="prepared", agent_id=row.agent.agent_id,
                                                    blocking=False, cancelled=None, **identity)

    def test_reopening_removed_session_coordinates_every_scope(self):
        workspace, rows = self.rows()
        for kind in ("session", "window", "agent"):
            row = next(row for row in rows if row.kind == kind)
            target = row.session.target
            if row.agent is not None:
                row = replace(row, agent=replace(row.agent, pane_target=replace(
                    row.agent.pane_target, session_id=row.session.session_id)))
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory, \
                    patch.dict(os.environ, LETEE_CONFIG_DIR=directory), \
                    patch.object(cockpit, "require_cockpit"), \
                    patch.object(cockpit, "current_target", return_value=target), \
                    patch.object(cockpit, "reset_to_help"), \
                    patch.object(cockpit, "startup_deadline", return_value=None), \
                    patch.object(cockpit, "attachment_failed", return_value=False), \
                    patch.object(cockpit, "status_snapshot", return_value=cockpit.StatusSnapshot(target, None, None, True)), \
                    patch.object(cockpit, "switch") as switch, \
                    patch.object(finder_ui.sessions, "_run", side_effect=lambda _op, _target, query, **_kwargs: (
                        f"{row.session.session_id}\t{row.window.window_id if row.window else '@1'}\t"
                        f"{row.agent.pane_target.pane_id if row.agent else '%2'}"
                        + (f"\t{target.session}" if "session_name" in query[-1] else "") + "\n")), \
                    patch.object(finder_ui.sessions, "ensure_created") as ensure:
                config.save_sessions([target])
                queued = operations.capture(operations.Effect("switch", target, automatic=True))
                self.assertFalse(operations.perform(operations.Effect("save_favorites", target)).error)
                removed = config.load_operations()
                self.assertIn(target.format(), removed["invalidated"])
                self.assertIsNone(operations.sample_target()[2])
                queued_after_removal = operations.capture(operations.Effect("switch", target, automatic=True))

                if row.agent is not None:
                    expected_command = finder_ui.sessions.existing_pane_attach_command(row.agent.pane_target)
                elif row.window is not None:
                    expected_command = finder_ui.sessions.window_attach_command(finder_ui._window_target(row))
                else:
                    expected_command = finder_ui.sessions.existing_attach_command(
                        target, row.session.session_id, row.session.socket_path)
                commit = finder_ui._prepare_destination(row, lambda: workspace)
                switch.assert_not_called()
                commit()

                state = config.load_operations()
                self.assertNotIn(target.format(), state["invalidated"])
                self.assertNotEqual(state["launch"], removed["launch"])
                self.assertEqual(operations.sample_target()[2], target)
                self.assertEqual(config.load_sessions(), [])
                switch.assert_called_once()
                self.assertEqual(switch.call_args.args[0], target)
                command = switch.call_args.args[1]
                self.assertEqual(command, expected_command)
                self.assertIn("attach-session -t", command)
                self.assertNotIn("new-session", command)
                self.assertEqual("select-window" in command, kind != "session")
                self.assertEqual("select-pane" in command, kind == "agent")
                if kind == "agent":
                    self.assertEqual(switch.call_args.args[2], row.agent.agent_id)
                self.assertEqual(switch.call_args.kwargs["launch_id"], state["launch"])
                self.assertTrue(operations.perform(queued).stale_navigation)
                self.assertTrue(operations.perform(queued_after_removal).stale_navigation)
                self.assertEqual(switch.call_count, 1)
                ensure.assert_not_called()

    def test_vanished_or_unavailable_target_never_commits(self):
        workspace, rows = self.rows()
        row = rows[0]
        for latest in (Workspace(), replace(workspace, sessions=(replace(row.session, available=False),))):
            with self.subTest(latest=latest), patch.object(finder_ui.sessions, "existing_attach_command", return_value="prepared"), \
                    patch.object(operations, "navigate") as navigate:
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
                patch.object(operations, "navigate") as navigate:
            commit = finder_ui._prepare_destination(row, lambda: latest)
            commit()
        navigate.assert_called_once_with(
            renamed.target, attach_command="stable-id-command", session_id=row.session.session_id,
            socket_path=row.session.socket_path, window_id="", blocking=False, cancelled=None)

    def test_mutation_after_finder_resolution_rejects_stale_destination(self):
        workspace, rows = self.rows()
        row = next(row for row in rows if row.kind == "session")
        target = row.session.target
        renamed = replace(target, session="renamed")
        for mutation in ("rename", "kill"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory, \
                    patch.dict(os.environ, LETEE_CONFIG_DIR=directory), \
                    patch.object(cockpit, "require_cockpit"), \
                    patch.object(cockpit, "current_target", return_value=None), \
                    patch.object(cockpit, "rename_target"), \
                    patch.object(cockpit, "reset_to_help"), \
                    patch.object(cockpit, "startup_deadline", return_value=None), \
                    patch.object(cockpit, "switch") as switch, \
                    patch.object(finder_ui.sessions, "rename"), \
                    patch.object(finder_ui.sessions, "kill"), \
                    patch.object(finder_ui.sessions, "existing_attach_command", return_value="stable-id-command"), \
                    patch.object(finder_ui.sessions, "ensure_created") as ensure, \
                    patch.object(finder_ui.sessions, "_run", return_value=(
                        f"{row.session.session_id}\t@1\t%2\t{renamed.session}\n" if mutation == "rename" else "")), \
                    patch("subprocess.run", side_effect=AssertionError("External processes prohibited")):
                config.save_sessions([target])
                resolved = threading.Event()
                mutated = threading.Event()
                errors = []
                navigate = operations.navigate

                def wait_for_mutation(*args, **kwargs):
                    resolved.set()
                    if not mutated.wait(2):
                        raise AssertionError("Mutation did not finish")
                    return navigate(*args, **kwargs)

                def open_destination():
                    try:
                        commit()
                    except BaseException as error:
                        errors.append(error)

                # The workspace deliberately remains stale even after the mutation.
                commit = finder_ui._prepare_destination(row, lambda: workspace)
                with patch.object(operations, "navigate", side_effect=wait_for_mutation):
                    with config.sessions_lock():
                        worker = threading.Thread(target=open_destination)
                        worker.start()
                        self.assertTrue(resolved.wait(2))
                        result = operations._perform_unlocked(
                            operations.Effect(mutation, target, message=renamed.session),
                            (target,), config.load_operations(), raise_errors=True)
                        self.assertFalse(result.error)
                        state = config.load_operations()
                    mutated.set()
                    worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], SystemExit)
                self.assertEqual(config.load_operations(), state)
                self.assertIn(target.format(), state["invalidated"])
                switch.assert_not_called()
                submitted = []
                snapshot = SessionSnapshot(local=SourceSnapshot(
                    sessions=(renamed,) if mutation == "rename" else (), available=True, bells=frozenset()), remotes={})
                SessionReconciler().reconcile(
                    tuple(config.load_sessions()), cockpit.current_target(), snapshot,
                    lambda effect: submitted.append(effect) or True, 0)
                self.assertFalse(any(effect.target == target for effect in submitted))
                stale_restore = operations.perform(operations.Effect("ensure_created", target, automatic=True))
                self.assertTrue(stale_restore.stale_navigation)
                ensure.assert_not_called()

    def test_live_validation_rejects_changed_identity_or_membership(self):
        workspace, rows = self.rows()
        row = next(row for row in rows if row.kind == "agent")
        expected = [row.session.session_id, row.window.window_id,
                    row.agent.pane_target.pane_id, row.session.target.session]
        for index in range(4):
            changed = expected.copy()
            changed[index] = "different"
            with self.subTest(field=index), patch.object(
                    finder_ui.sessions, "_run", return_value="\t".join(changed) + "\n"):
                with self.assertRaisesRegex(SystemExit, "changed or is unavailable"):
                    finder_ui.sessions.validate_destination(
                        row.session.target, row.session.session_id, row.session.socket_path,
                        window_id=row.window.window_id, pane_id=row.agent.pane_target.pane_id)

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
