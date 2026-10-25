from dataclasses import replace
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from letee import cockpit, sessions
from letee import names
from letee.discovery import SessionSnapshot, SourceSnapshot, _parse_source_snapshot
from letee.names import PaneTarget, Target


class FinderMetadataTest(unittest.TestCase):
    def test_metadata_updates_snapshot_without_changing_alert_identity(self):
        pane = PaneTarget(Target("local", "work"), "@1", "%2", "/tmp/socket")
        source = SourceSnapshot(True, (pane.target,), frozenset(), panes=(pane,))
        for field, value in (("window_name", "editor"), ("session_id", "$3"),
                             ("window_index", 2), ("pane_index", 4),
                             ("window_active", True), ("pane_active", True)):
            with self.subTest(field=field):
                updated = replace(pane, **{field: value})
                self.assertEqual(pane, updated)
                self.assertEqual(hash(pane), hash(updated))
                self.assertEqual({(pane, "agent"): "alert"}[(updated, "agent")], "alert")
                self.assertNotEqual(source, replace(source, panes=(updated,)))

    def test_discovery_metadata_precedes_unescaped_window_name(self):
        source = _parse_source_snapshot(
            "work:@1:%2:0:-:1:0:/tmp/socket:dev\t$3:4:5\teditor:one\tlogs\n",
            kind="local",
        )
        pane, = source.panes
        self.assertEqual((pane.session_id, pane.window_index, pane.pane_index), ("$3", 4, 5))
        self.assertEqual((pane.pane_active, pane.window_active), (True, False))
        self.assertEqual(pane.window_name, "editor:one\tlogs")
        unavailable = replace(source, available=False)
        self.assertEqual(SessionSnapshot(source, {"dev": source, "down": unavailable}).panes, (pane, pane))
        self.assertEqual(SessionSnapshot(unavailable, {}).panes, ())

    def test_window_target_validation(self):
        target = Target("local", "work")
        for session_id, window_id, socket in (("work", "@1", "/tmp/s"),
                                               ("$1", "1", "/tmp/s"), ("$1", "@1", "")):
            with self.subTest(session_id=session_id, window_id=window_id, socket=socket):
                with self.assertRaises(SystemExit):
                    names.WindowTarget(target, session_id, window_id, socket)


class FinderAttachmentTest(unittest.TestCase):
    def pane(self, kind="local"):
        target = Target(kind, "work", "dev" if kind == "ssh" else None)
        return PaneTarget(target, "@1", "%2", "/tmp/a socket;'$(bad)", session_id="$3")

    def prepare(self, pane, scope):
        if scope == "session":
            return sessions.existing_attach_command(pane.target, pane.session_id, pane.socket_path, overlay=False)
        if scope == "window":
            return sessions.window_attach_command(
                names.WindowTarget(pane.target, pane.session_id, pane.window_id, pane.socket_path), overlay=False,
            )
        return sessions.existing_pane_attach_command(pane, overlay=False)

    def test_local_and_remote_exact_targets_and_quote_safety(self):
        for kind in ("local", "ssh"):
            for scope in ("session", "window", "pane"):
                with self.subTest(kind=kind, scope=scope):
                    pane = self.pane(kind)
                    with patch("letee.sessions._run", return_value="$3\t@1\t%2\n") as run:
                        command = self.prepare(pane, scope)
                    run.assert_called_once()
                    validation = run.call_args.args[2]
                    if kind == "ssh":
                        validation = shlex.split(validation[-1])
                        command = shlex.split(command)[-1]
                    else:
                        self.assertTrue(command.startswith("env -u TMUX "))
                    self.assertIn(pane.socket_path, validation)
                    self.assertIn("list-panes", validation)
                    self.assertIn("attach-session -t '$3'", command)
                    self.assertNotIn("new-session", command)
                    self.assertNotIn("work", command)
                    self.assertNotIn("\\;", command)
                    for segment in command.removeprefix("env -u TMUX ").split(" && "):
                        self.assertEqual(shlex.split(segment.removeprefix("env -u TMUX "))[2], pane.socket_path)
                    self.assertEqual("select-window" in command, scope != "session")
                    self.assertEqual("select-pane" in command, scope == "pane")
                    if scope != "session":
                        self.assertIn("select-window -t '$3:@1'", command)
                        self.assertIn(" && ", command)
                    if scope == "pane":
                        self.assertIn("select-pane -t '$3:@1.%2'", command)

    def test_stale_and_wrong_membership_never_prepare(self):
        for scope, output in (("session", "$4\t@1\t%2\n"),
                              ("window", "$3\t@9\t%2\n"),
                              ("pane", "$3\t@9\t%2\n"),
                              ("pane", "$3\t@1\t%8\n"), ("pane", "")):
            with self.subTest(scope=scope, output=output):
                with patch("letee.sessions._run", return_value=output):
                    with self.assertRaisesRegex(SystemExit, "no longer exists"):
                        self.prepare(self.pane(), scope)

    def test_failed_selection_stops_shell_before_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            tmux = Path(directory, "tmux")
            tmux.write_text('#!/bin/sh\nprintf "%s\\n" "$3" >> "$FINDER_LOG"\n[ "$3" != "$FINDER_FAIL" ]\n')
            tmux.chmod(0o700)
            log = Path(directory, "calls")
            for failure, expected in (("select-window", ["select-window"]),
                                      ("select-pane", ["select-window", "select-pane"])):
                with self.subTest(failure=failure):
                    log.write_text("")
                    with patch("letee.sessions._run", return_value="$3\t@1\t%2\n"):
                        command = self.prepare(self.pane(), "pane")
                    env = {**os.environ, "PATH": directory + os.pathsep + os.defpath,
                           "FINDER_LOG": str(log), "FINDER_FAIL": failure}
                    result = subprocess.run(command, shell=True, env=env, capture_output=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(log.read_text().splitlines(), expected)

    def test_pane_moved_after_preparation_cannot_attach_neighbor(self):
        # This fake command models tmux's native qualified target lookup. No
        # tmux server is started: global %2 remains valid after a move, while
        # $3:@1.%2 no longer resolves in its original window.
        with tempfile.TemporaryDirectory() as directory:
            tmux = Path(directory, "tmux")
            tmux.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$3" >> "$FINDER_LOG"\n'
                'if [ "$3" = select-pane ]; then [ "$5" = "%2" ]; else exit 0; fi\n'
            )
            tmux.chmod(0o700)
            log = Path(directory, "calls")
            with patch("letee.sessions._run", return_value="$3\t@1\t%2\n"):
                command = self.prepare(self.pane(), "pane")
            result = subprocess.run(command, shell=True, capture_output=True, env={
                **os.environ, "PATH": directory + os.pathsep + os.defpath, "FINDER_LOG": str(log),
            })
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("attach-session", log.read_text().splitlines())

    def test_missing_pane_session_id_fails_without_query(self):
        with patch("letee.sessions._run") as run:
            with self.assertRaisesRegex(SystemExit, "session ID"):
                self.prepare(replace(self.pane(), session_id=""), "pane")
            run.assert_not_called()

    def test_query_failure_reports_system_exit(self):
        with patch("letee.sessions.subprocess.run", side_effect=subprocess.CalledProcessError(1, "tmux", stderr="gone")):
            with self.assertRaisesRegex(SystemExit, "gone"):
                self.prepare(self.pane(), "window")

    def test_remote_prepared_navigation_uses_attachment_runner_once(self):
        pane = self.pane("ssh")
        window = names.WindowTarget(pane.target, "$3", "@1", pane.socket_path)
        child = "ssh -t dev 'tmux -S /tmp/custom attach-session -t $3'"
        for kind in ("session", "window", "agent"):
            with (
                self.subTest(kind=kind),
                patch.object(cockpit, "right_pane", return_value="%2"),
                patch.object(cockpit.tmux, "tmux") as command,
            ):
                if kind == "session":
                    cockpit.open_session(pane.target, child)
                elif kind == "window":
                    cockpit.open_window(window, child)
                else:
                    cockpit.open_agent(pane, "exact-agent", child)
            respawns = [item.args[4] for item in command.call_args_list if item.args[0] == "respawn-pane"]
            self.assertEqual(len(respawns), 1)
            self.assertEqual(shlex.split(respawns[0]), [cockpit.sys.executable, "-m", "letee.attachment", pane.target.format(), "--", *shlex.split(child)])

    def test_navigation_preserves_agent_identity_and_accepts_prepared_commands(self):
        pane = self.pane()
        window = names.WindowTarget(pane.target, "$3", "@1", pane.socket_path)
        with patch("letee.cockpit.switch") as switch:
            cockpit.open_window(window, "prepared-window")
            switch.assert_called_once_with(pane.target, "prepared-window")
        with patch("letee.cockpit.switch") as switch:
            cockpit.open_agent(pane, "exact-agent", "prepared-pane")
            switch.assert_called_once_with(pane.target, "prepared-pane", "exact-agent")
        with patch("letee.cockpit.switch") as switch, patch("letee.sessions.window_attach_command", return_value="window") as prepare:
            cockpit.open_window(window)
            prepare.assert_not_called()
            self.assertEqual(switch.call_args.args[1](), "window")
            prepare.assert_called_once_with(window)


if __name__ == "__main__":
    unittest.main()
