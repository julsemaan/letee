import importlib
import json
import os
import socket
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import letee.sessions as sessions
from letee import __version__, diagnostics
from letee.__main__ import _tmux_socket_dir, main
from letee.discovery import SessionSnapshot, SourceSnapshot
from letee.names import Target


MAIN_MODULE = importlib.import_module("letee.__main__")


class MainTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.environment = patch.dict(os.environ, LETEE_CONFIG_DIR=directory.name)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def tearDown(self):
        from letee import config, tmux

        config.set_server(None)
        tmux.set_server(None)

    def test_attachment_exit_callback_records_captured_state_without_tmux_queries(self):
        with (
            patch.object(MAIN_MODULE.diagnostics, "log") as log,
            patch.object(MAIN_MODULE.tmux, "tmux") as tmux_call,
        ):
            result = main([
                "-L", "work", "attachment-exited", "%2", "attachment-1", "42",
                "ssh:dev:work", "agent-1", "switch-1", "255", "", "1",
            ])
        self.assertEqual(result, 0)
        tmux_call.assert_not_called()
        log.assert_called_once_with(
            "attachment_exited", right_pane="%2", attachment_id="attachment-1",
            generation=42, target="ssh:dev:work", agent_id="agent-1",
            switch_id="switch-1", exit_status=255, exit_signal=None, expected_death="1",
            parent_run_id=None, intentional=True,
        )

    def test_attachment_exit_callback_preserves_dash_prefixed_agent_ids(self):
        for agent_id in ("-agent", "--help"):
            with self.subTest(agent_id=agent_id), patch.object(MAIN_MODULE.diagnostics, "log") as log:
                self.assertEqual(main([
                    "attachment-exited", "--parent-run-id", "old-run", "--",
                    "%2", "old-id", "123", "local:work", agent_id, "old-switch",
                    "0", "", "",
                ]), 0)
            self.assertEqual(log.call_args.args, ("attachment_exited",))
            self.assertEqual(log.call_args.kwargs["agent_id"], agent_id)
            self.assertEqual(log.call_args.kwargs["parent_run_id"], "old-run")

    def test_unknown_attachment_exit_keeps_generation_without_old_identity(self):
        with (
            patch.object(MAIN_MODULE.diagnostics, "log") as log,
            patch.object(MAIN_MODULE.tmux, "tmux") as command,
        ):
            self.assertEqual(main([
                "attachment-exited", "%2", "", "234", "", "", "", "0", "", "",
                "--parent-run-id", "",
            ]), 0)
        command.assert_not_called()
        fields = log.call_args.kwargs
        self.assertEqual(fields["generation"], 234)
        for field in ("attachment_id", "target", "agent_id", "switch_id", "parent_run_id"):
            self.assertIsNone(fields[field])
        self.assertFalse(fields["intentional"])

    def test_attachment_signal_callback_correlates_parent_and_unexpected_death(self):
        with patch.object(MAIN_MODULE.diagnostics, "log") as log:
            self.assertEqual(main([
                "-L", "work", "attachment-exited", "%2", "old-id", "123",
                "local:work", "", "old-switch", "", "TERM", "",
                "--parent-run-id", "old-run",
            ]), 0)
        self.assertEqual(log.call_args.kwargs["exit_signal"], 15)
        self.assertIsNone(log.call_args.kwargs["exit_status"])
        self.assertEqual(log.call_args.kwargs["parent_run_id"], "old-run")
        self.assertFalse(log.call_args.kwargs["intentional"])

    def test_cli_reports_and_audits_os_error_without_changing_message(self):
        error = FileNotFoundError(2, "No such file", "tmux")
        output = StringIO()
        with (
            patch.object(MAIN_MODULE, "main", side_effect=error),
            patch.object(MAIN_MODULE.diagnostics, "error") as audit,
            patch.object(MAIN_MODULE.sys, "stderr", output),
        ):
            self.assertEqual(MAIN_MODULE.run_cli([]), 1)
        self.assertEqual(output.getvalue(), "letee: tmux: No such file\n")
        audit.assert_called_once_with(error, handling="reported", operation="cli")

    def test_successful_help_exit_is_not_reported_as_failure(self):
        error = SystemExit(0)
        with patch.object(MAIN_MODULE, "main", side_effect=error), patch.object(MAIN_MODULE.diagnostics, "log") as log, patch.object(MAIN_MODULE.diagnostics, "error") as audit:
            with self.assertRaises(SystemExit) as raised:
                MAIN_MODULE.run_cli([])
        self.assertIs(raised.exception, error)
        log.assert_called_once_with("cli_exit", outcome="completed", exit_code=0)
        audit.assert_not_called()

    def test_cli_audits_propagated_errors_without_replacing_them(self):
        for error in (SystemExit(2), RuntimeError("private detail"), KeyboardInterrupt()):
            with (
                self.subTest(error=type(error).__name__),
                patch.object(MAIN_MODULE, "main", side_effect=error),
                patch.object(MAIN_MODULE.diagnostics, "error") as audit,
                self.assertRaises(type(error)) as raised,
            ):
                MAIN_MODULE.run_cli([])
            self.assertIs(raised.exception, error)
            audit.assert_called_once_with(error, handling="cancel" if isinstance(error, KeyboardInterrupt) else "propagate", operation="cli")

    def test_nonzero_server_probes_are_audited_without_output(self):
        failure = subprocess.CompletedProcess([], 7, "private stdout", "private stderr")
        with (
            patch.object(MAIN_MODULE, "_run_server_tmux", return_value=failure),
            patch.object(MAIN_MODULE.diagnostics, "error") as audit,
        ):
            self.assertIsNone(MAIN_MODULE._server_attached("work"))
        audit.assert_called_once_with(
            handling="fallback", operation="server_marker_probe", returncode=7, server="work",
        )

    def test_nonzero_client_probe_and_legacy_cleanup_are_audited(self):
        verified = subprocess.CompletedProcess([], 0, "1\n", "")
        failure = subprocess.CompletedProcess([], 9, "private stdout", "private stderr")
        with (
            patch.object(MAIN_MODULE, "_run_server_tmux", side_effect=[verified, failure]),
            patch.object(MAIN_MODULE.diagnostics, "error") as audit,
        ):
            self.assertIsNone(MAIN_MODULE._server_attached("work"))
        audit.assert_called_once_with(
            handling="fallback", operation="server_clients_probe", returncode=9, server="work",
        )
        for responses, operation, handling in (
            ([failure], "legacy_marker_probe", "fallback"),
            ([verified, failure], "legacy_server_kill", "ignored"),
        ):
            with (
                self.subTest(operation=operation),
                patch.object(MAIN_MODULE, "_legacy_socket_present", return_value=True),
                patch.object(MAIN_MODULE, "_run_legacy_tmux", side_effect=responses),
                patch.object(MAIN_MODULE.diagnostics, "error") as audit,
            ):
                MAIN_MODULE._cleanup_legacy_server("work")
            audit.assert_called_once_with(
                handling=handling, operation=operation, returncode=9, server="work",
            )

    def test_version_prints_without_configuring_or_starting_cockpit(self):
        output = StringIO()
        with (
            redirect_stdout(output),
            patch("letee.__main__._configure_server") as configure_server,
            patch("letee.__main__.cockpit.cockpit") as cockpit,
        ):
            self.assertEqual(main(["version"]), 0)

        self.assertEqual(output.getvalue(), f"{__version__}\n")
        configure_server.assert_not_called()
        cockpit.assert_not_called()

    def test_socket_dir_matches_tmux_default_when_tmpdir_is_unset(self):
        with (
            patch.dict("letee.__main__.os.environ", {"TMPDIR": "/wrong"}, clear=True),
            patch("letee.__main__.os.getuid", return_value=1000),
        ):
            self.assertEqual(_tmux_socket_dir(), Path("/tmp/tmux-1000"))

    def test_global_server_option_configures_named_cockpit(self):
        with (
            patch("letee.__main__.config.set_server", return_value="work") as set_config,
            patch("letee.__main__.tmux.set_server") as set_tmux,
            patch("letee.__main__.cockpit.cockpit", return_value=0) as cockpit,
        ):
            self.assertEqual(main(["-L", "work"]), 0)

        set_config.assert_called_once_with("work")
        set_tmux.assert_called_once_with("work")
        cockpit.assert_called_once_with()

    def test_sidebar_is_internal_command_and_accepts_named_server(self):
        with (
            patch("letee.__main__.config.set_server", return_value="work") as set_config,
            patch("letee.__main__.tmux.set_server") as set_tmux,
            patch("letee.sidebar.main", return_value=0) as sidebar,
        ):
            self.assertEqual(main(["-L", "work", "sidebar"]), 0)

        set_config.assert_called_once_with("work")
        set_tmux.assert_called_once_with("work")
        sidebar.assert_called_once_with()

    def test_toggle_sidebar_dispatches_with_named_server(self):
        with patch("letee.__main__.cockpit.toggle_sidebar", return_value=0) as toggle:
            self.assertEqual(main(["-L", "work", "toggle-sidebar"]), 0)
        toggle.assert_called_once_with()
        self.assertEqual(MAIN_MODULE.tmux.SERVER, "work")

    def test_focus_sidebar_defaults_to_sessions_and_accepts_all_actions(self):
        with patch("letee.__main__.cockpit.focus_sidebar") as focus_sidebar:
            main(["focus-sidebar"])
            main(["focus-sidebar", "agents"])
            main(["focus-sidebar", "add"])
            main(["focus-sidebar", "remove"])
            main(["focus-sidebar", "kill"])
            main(["focus-sidebar", "alert"])
            main(["focus-sidebar", "previous_agent"])
            main(["focus-sidebar", "next_agent"])
            main(["focus-sidebar", "previous_session"])
            main(["focus-sidebar", "next_session"])

        self.assertEqual(
            focus_sidebar.call_args_list,
            [
                unittest.mock.call("sessions"),
                unittest.mock.call("agents"),
                unittest.mock.call("add"),
                unittest.mock.call("remove"),
                unittest.mock.call("kill"),
                unittest.mock.call("alert"),
                unittest.mock.call("previous_agent"),
                unittest.mock.call("next_agent"),
                unittest.mock.call("previous_session"),
                unittest.mock.call("next_session"),
            ],
        )

    def test_session_cycle_actions_accept_named_server(self):
        for action in ("previous_session", "next_session"):
            with self.subTest(action=action), patch(
                "letee.__main__.cockpit.focus_sidebar", return_value=0
            ) as focus_sidebar:
                self.assertEqual(main(["-L", "work", "focus-sidebar", action]), 0)
                self.assertEqual(MAIN_MODULE.tmux.SERVER, "work")
                self.assertEqual(MAIN_MODULE.config.current_server(), "work")
                focus_sidebar.assert_called_once_with(action)

    def test_switch_session_uses_persisted_favorite_order(self):
        favorites = [
            Target("ssh", "alpha", "dev"),
            Target("local", "zeta"),
            Target("local", "alpha"),
        ]
        target = Target("local", "zeta")
        with (
            patch("letee.config.load_sessions", return_value=favorites),
            patch("letee.cockpit.right_pane", return_value="pane"),
            patch("letee.operations.sessions.attach_command", return_value="attach") as attach_command,
            patch("letee.__main__.cockpit.switch") as switch,
        ):
            main(["switch-session", "2"])

            attach_command.assert_not_called()
            switch.assert_called_once()
            self.assertTrue(callable(switch.call_args.args[1]))
            self.assertEqual(switch.call_args.args[1](), "attach")

        attach_command.assert_called_once_with(target)

    def test_cli_navigation_holds_restoration_lock_through_launch(self):
        target = Target("local", "work")

        def assert_locked():
            with MAIN_MODULE.config.sessions_lock(blocking=False) as acquired:
                self.assertFalse(acquired)

        def attach(_target):
            assert_locked()
            return "attach"

        def launch(_target, prepare, **kwargs):
            assert_locked()
            self.assertEqual(prepare(), "attach")

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(MAIN_MODULE.config, "config_dir", return_value=Path(directory)),
            patch.object(MAIN_MODULE.config, "load_sessions", return_value=[target]),
            patch.object(MAIN_MODULE.cockpit, "require_cockpit"),
            patch.object(MAIN_MODULE.operations.sessions, "attach_command", side_effect=attach),
            patch.object(MAIN_MODULE.cockpit, "switch", side_effect=launch),
        ):
            for args in (["switch", target.format()], ["switch-session", "1"]):
                with self.subTest(args=args):
                    self.assertEqual(main(args), 0)

    def test_switch_session_looks_up_updated_or_removed_slot_under_lock(self):
        old_target = Target("local", "old")
        new_target = Target("local", "new")
        sessions_lock = MAIN_MODULE.config.sessions_lock
        load_sessions = MAIN_MODULE.config.load_sessions

        for updated in ([new_target], []):
            with (
                self.subTest(updated=updated),
                tempfile.TemporaryDirectory() as directory,
                patch.object(MAIN_MODULE.config, "config_dir", return_value=Path(directory)),
            ):
                MAIN_MODULE.config.save_sessions([old_target])

                @contextmanager
                def lock_after_kill():
                    with sessions_lock():
                        MAIN_MODULE.config.save_sessions(updated)
                        yield True

                def load_locked_sessions():
                    with sessions_lock(blocking=False) as acquired:
                        self.assertFalse(acquired)
                    return load_sessions()

                with (
                    patch.object(MAIN_MODULE.config, "sessions_lock", lock_after_kill),
                    patch.object(MAIN_MODULE.config, "load_sessions", side_effect=load_locked_sessions),
                    patch.object(MAIN_MODULE.cockpit, "require_cockpit"),
                    patch.object(MAIN_MODULE.cockpit, "switch") as switch,
                    patch.object(MAIN_MODULE.operations.sessions, "attach_command") as attach,
                ):
                    if updated:
                        self.assertEqual(main(["switch-session", "1"]), 0)
                        switch.assert_called_once()
                        self.assertEqual(switch.call_args.args[0], new_target)
                        switch.call_args.args[1]()
                        attach.assert_called_once_with(new_target)
                    else:
                        with self.assertRaisesRegex(SystemExit, "^No session in slot 1$"):
                            main(["switch-session", "1"])
                        switch.assert_not_called()
                        attach.assert_not_called()

    def test_switch_session_rejects_empty_slot_without_switching(self):
        with (
            patch("letee.config.load_sessions", return_value=[Target("local", "work")]),
            patch("letee.__main__.cockpit.switch") as switch,
        ):
            with self.assertRaisesRegex(SystemExit, "^No session in slot 2$"):
                main(["switch-session", "2"])

        switch.assert_not_called()

    def test_switch_session_rejects_slots_outside_one_to_nine(self):
        for slot in ("0", "10", "x"):
            with self.subTest(slot=slot), patch("letee.__main__.cockpit.switch") as switch:
                with self.assertRaises(SystemExit):
                    main(["switch-session", slot])
                switch.assert_not_called()

    def test_switch_defers_attach_command_until_cockpit_switches(self):
        target = Target("ssh", "work", "dev")
        with (
            patch("letee.cockpit.right_pane", return_value="pane"),
            patch("letee.operations.sessions.attach_command", return_value="attach") as attach_command,
            patch("letee.__main__.cockpit.switch") as switch,
        ):
            main(["switch", target.format()])

            attach_command.assert_not_called()
            switch.assert_called_once()
            self.assertEqual(switch.call_args.args[0], target)
            self.assertTrue(callable(switch.call_args.args[1]))
            self.assertEqual(switch.call_args.args[1](), "attach")

        attach_command.assert_called_once_with(target)

    def test_switch_without_cockpit_fails_before_building_attach_command(self):
        with (
            patch("letee.cockpit.right_pane", return_value=None),
            patch("letee.operations.sessions.attach_command") as attach_command,
            patch("letee.__main__.cockpit.switch") as switch,
        ):
            with self.assertRaisesRegex(SystemExit, "^No valid letee"):
                main(["switch", "ssh:dev:work"])

        attach_command.assert_not_called()
        switch.assert_not_called()

    def test_kill_removes_target_from_persisted_sessions(self):
        target = Target("local", "work")
        other = Target("ssh", "other", "dev")
        for active in (False, True):
            with (
                self.subTest(active=active),
                patch("letee.operations.sessions.kill") as kill,
                patch("letee.config.load_sessions", return_value=[target, other]),
                patch("letee.config.save_sessions") as save,
                patch("letee.cockpit.current_target", return_value=target if active else other),
                patch("letee.cockpit.set_expected_right_pane_death") as expect_death,
                patch("letee.cockpit.resolve_expected_right_pane_death") as resolve_death,
                patch("letee.cockpit.reset_to_help") as reset,
            ):
                self.assertEqual(main(["kill", target.format()]), 0)

                kill.assert_called_once_with(target)
                save.assert_called_once_with([other])
                if active:
                    expect_death.assert_called_once_with(target)
                    resolve_death.assert_called_once_with(target, True)
                    reset.assert_not_called()
                else:
                    expect_death.assert_not_called()
                    resolve_death.assert_not_called()
                    reset.assert_called_once_with(target)

    def test_kill_untracked_target_saves_unchanged_sessions(self):
        target = Target("local", "untracked")
        other = Target("local", "other")
        with (
            patch("letee.operations.sessions.kill") as kill,
            patch("letee.config.load_sessions", return_value=[other]),
            patch("letee.config.save_sessions") as save,
            patch("letee.cockpit.current_target", return_value=other),
            patch("letee.cockpit.set_expected_right_pane_death") as expect_death,
            patch("letee.cockpit.resolve_expected_right_pane_death") as resolve_death,
            patch("letee.__main__.cockpit.reset_to_help") as reset,
        ):
            self.assertEqual(main(["kill", target.format()]), 0)

        kill.assert_called_once_with(target)
        save.assert_called_once_with([other])
        reset.assert_called_once_with(target)
        expect_death.assert_not_called()
        resolve_death.assert_not_called()

    def test_rename_tmux_then_replaces_tracked_target(self):
        target = Target("ssh", "old", "dev")
        renamed = Target("ssh", "new", "dev")
        other = Target("local", "other")
        for active in (False, True):
            with (
                self.subTest(active=active),
                patch("letee.config.load_sessions", return_value=[target, other]),
                patch("letee.operations.sessions.rename", return_value=renamed) as rename,
                patch("letee.config.save_sessions") as save,
                patch("letee.cockpit.current_target", return_value=target if active else other),
                patch("letee.cockpit.rename_target") as rename_target,
                patch("letee.cockpit.switch") as switch,
            ):
                self.assertEqual(main(["rename", target.format(), "new"]), 0)

                rename.assert_called_once_with(target, "new")
                save.assert_called_once_with([renamed, other])
                rename_target.assert_called_once_with(target, renamed)
                if active:
                    switch.assert_called_once()
                    self.assertEqual(switch.call_args.args[0], renamed)
                    self.assertTrue(callable(switch.call_args.args[1]))
                else:
                    switch.assert_not_called()

    def test_rename_rejects_untracked_target_without_touching_tmux(self):
        target = Target("local", "work")
        with (
            patch("letee.config.load_sessions", return_value=[]),
            patch("letee.operations.sessions.rename") as rename,
            patch("letee.config.save_sessions") as replace,
        ):
            with self.assertRaisesRegex(SystemExit, r"^Session not tracked: local:work$"):
                main(["rename", target.format(), "new"])

        rename.assert_not_called()
        replace.assert_not_called()

    def test_rename_rejects_invalid_new_name_without_touching_tmux(self):
        target = Target("local", "work")
        with (
            patch("letee.config.load_sessions", return_value=[target]),
            patch("letee.operations.sessions.rename") as rename,
        ):
            with self.assertRaisesRegex(SystemExit, "Invalid session"):
                main(["rename", target.format(), "bad name"])

        rename.assert_not_called()

    def test_failed_rename_does_not_replace_tracked_target(self):
        target = Target("local", "old")
        with (
            patch("letee.config.load_sessions", return_value=[target]),
            patch("letee.operations.sessions.rename", side_effect=SystemExit("rename failed")) as rename,
            patch("letee.cockpit.current_target", return_value=target),
            patch("letee.cockpit.rename_target") as rename_target,
            patch("letee.cockpit.switch") as switch,
            patch("letee.config.save_sessions") as replace,
        ):
            with self.assertRaisesRegex(SystemExit, "^rename failed$"):
                main(["rename", target.format(), "new"])

        rename.assert_called_once_with(target, "new")
        replace.assert_not_called()
        rename_target.assert_not_called()
        switch.assert_not_called()

    def test_create_ssh_rejects_option_like_hosts(self):
        for host in ("-V", "-F", "--help"):
            with self.subTest(host=host):
                with patch("letee.operations.sessions.create") as create:
                    with self.assertRaisesRegex(SystemExit, rf"Invalid host: {host!r}"):
                        main(["create", "ssh", "--", host, "work"])
                    create.assert_not_called()

    def test_create_local_keeps_option_like_session_support(self):
        target = Target("local", "-V")
        with (
            patch("letee.operations.sessions.create") as create,
            patch("letee.cockpit.right_pane", return_value="pane"),
            patch("letee.operations.sessions.attach_command", return_value="attach") as attach_command,
            patch("letee.__main__.cockpit.switch") as switch,
        ):
            main(["create", "local", "--", "-V"])

            attach_command.assert_not_called()
            switch.assert_called_once()
            self.assertTrue(callable(switch.call_args.args[1]))
            self.assertEqual(switch.call_args.args[1](), "attach")

        create.assert_called_once_with(target)
        attach_command.assert_called_once_with(target)

    def test_list_uses_session_snapshot_and_displays_local_errors(self):
        snapshot = SessionSnapshot(
            SourceSnapshot(False, (), frozenset(), "permission denied"),
            {
                "dev": SourceSnapshot(True, (Target("ssh", "work", "dev"),), frozenset()),
                "off": SourceSnapshot(False, (), frozenset(), "offline"),
            },
        )
        with patch("letee.__main__.discover", return_value=snapshot), patch("builtins.print") as print_:
            main(["list"])

        self.assertEqual(
            [call.args[0] for call in print_.call_args_list],
            ["local unavailable: permission denied", "ssh:dev:work", "ssh:off unavailable"],
        )

    def test_list_servers_shows_only_verified_servers_and_attachment_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            socket_dir = Path(tempdir) / "tmux-1000"
            socket_dir.mkdir()
            for name in ("letee@v1", "letee@v1-personal", "letee@v1-stale", "other"):
                (socket_dir / name).touch()

            def run(command, **kwargs):
                socket = command[2]
                if socket == "letee@v1-stale":
                    return subprocess.CompletedProcess(command, 1, "", "stale socket")
                if command[3:6] == ["show-options", "-v", "-t"]:
                    return subprocess.CompletedProcess(command, 0, "1\n", "")
                clients = "client\n" if socket == "letee@v1" else ""
                return subprocess.CompletedProcess(command, 0, clients, "")

            with (
                patch("letee.__main__._tmux_socket_dir", return_value=socket_dir),
                patch("letee.__main__.subprocess.run", side_effect=run),
                patch("builtins.print") as print_,
            ):
                main(["list-servers"])

        self.assertEqual(
            [call.args[0] for call in print_.call_args_list],
            ["default (attached)", "personal (detached)"],
        )

    def test_legacy_v1_socket_names_are_migrated_not_listed_as_current_servers(self):
        with tempfile.TemporaryDirectory() as tempdir:
            socket_dir = Path(tempdir) / "tmux-1000"
            socket_dir.mkdir()
            for name in ("letee-v1", "letee-v1-work"):
                (socket_dir / name).touch()

            with (
                patch("letee.__main__._tmux_socket_dir", return_value=socket_dir),
                patch("letee.__main__._legacy_socket_present", return_value=True),
                patch(
                    "letee.__main__._run_legacy_tmux",
                    return_value=subprocess.CompletedProcess([], 0, "1\n", ""),
                ) as run_legacy,
                patch("letee.__main__._server_attached") as server_attached,
            ):
                self.assertEqual(MAIN_MODULE.list_servers(), [])

        self.assertEqual(
            run_legacy.call_args_list,
            [
                unittest.mock.call("letee-v1", "show-options", "-v", "-t", "letee", "@letee_cockpit"),
                unittest.mock.call("letee-v1", "kill-server"),
                unittest.mock.call("letee-v1-work", "show-options", "-v", "-t", "letee", "@letee_cockpit"),
                unittest.mock.call("letee-v1-work", "kill-server"),
            ],
        )
        server_attached.assert_not_called()

    def test_verified_legacy_server_is_killed_with_system_tmux_only(self):
        with tempfile.TemporaryDirectory() as tempdir:
            socket_dir = Path(tempdir) / "tmux-1000"
            socket_dir.mkdir()
            legacy_path = socket_dir / "letee-work"
            legacy = socket.socket(socket.AF_UNIX)
            legacy.bind(str(legacy_path))
            self.addCleanup(legacy.close)
            self.addCleanup(legacy_path.unlink, missing_ok=True)
            calls = []

            def run(server_socket, *args):
                calls.append((server_socket, args))
                return subprocess.CompletedProcess([], 0, "1\n", "")

            with patch("letee.__main__._tmux_socket_dir", return_value=socket_dir), patch(
                "letee.__main__._run_legacy_tmux", side_effect=run
            ):
                MAIN_MODULE._cleanup_legacy_server("work")

        self.assertEqual(
            calls,
            [
                ("letee-work", ("show-options", "-v", "-t", "letee", "@letee_cockpit")),
                ("letee-work", ("kill-server",)),
            ],
        )

    def test_unverified_or_symlink_legacy_server_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tempdir:
            socket_dir = Path(tempdir) / "tmux-1000"
            socket_dir.mkdir()
            legacy_path = socket_dir / "letee-work"
            legacy_path.touch()
            with (
                patch("letee.__main__._tmux_socket_dir", return_value=socket_dir),
                patch("letee.__main__._run_legacy_tmux") as run,
            ):
                MAIN_MODULE._cleanup_legacy_server("work")
            run.assert_not_called()

            legacy_path.unlink()
            target = socket_dir / "other"
            target.touch()
            legacy_path.symlink_to(target)
            with (
                patch("letee.__main__._run_legacy_tmux") as run,
                patch("letee.__main__._tmux_socket_dir", return_value=socket_dir),
            ):
                MAIN_MODULE._cleanup_legacy_server("work")
            run.assert_not_called()

    def test_unverified_legacy_server_is_not_killed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            socket_dir = Path(tempdir) / "tmux-1000"
            socket_dir.mkdir()
            legacy_path = socket_dir / "letee-work"
            legacy = socket.socket(socket.AF_UNIX)
            legacy.bind(str(legacy_path))
            self.addCleanup(legacy.close)
            self.addCleanup(legacy_path.unlink, missing_ok=True)
            with (
                patch("letee.__main__._tmux_socket_dir", return_value=socket_dir),
                patch(
                    "letee.__main__._run_legacy_tmux",
                    return_value=subprocess.CompletedProcess([], 0, "0\n", ""),
                ) as run,
            ):
                MAIN_MODULE._cleanup_legacy_server("work")

        run.assert_called_once_with(
            "letee-work", "show-options", "-v", "-t", "letee", "@letee_cockpit"
        )

    def test_server_commands_use_configured_local_deadline(self):
        with (
            patch("letee.__main__.config.load_timing", return_value=MAIN_MODULE.config.Timing(local_command_timeout=0.25)),
            patch("letee.__main__.subprocess.run") as run,
        ):
            MAIN_MODULE._run_server_tmux("work", "list-clients")
            self.assertEqual(run.call_args.kwargs["timeout"], 0.25)
            MAIN_MODULE._run_legacy_tmux("letee-work", "list-clients")
            self.assertEqual(run.call_args.kwargs["timeout"], 0.25)

    def test_kill_server_requires_verified_letee_server_and_uses_selected_socket(self):
        with (
            patch("letee.__main__.config.load_timing", return_value=MAIN_MODULE.config.Timing()),
            patch("letee.__main__._server_attached", return_value=False) as status,
            patch("letee.__main__.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run,
        ):
            main(["-L", "work", "kill-server"])

        status.assert_called_once_with("work")
        run.assert_called_once_with(
            [MAIN_MODULE.tmux.tmux_executable(), "-L", "letee@v1-work", "kill-server"],
            text=True, capture_output=True, check=False, timeout=2,
        )

    def test_kill_server_refuses_missing_or_non_letee_server(self):
        with patch("letee.__main__._server_attached", return_value=None), patch("letee.__main__.subprocess.run") as run:
            with self.assertRaisesRegex(SystemExit, "Not a running letee server: work"):
                main(["-L", "work", "kill-server"])

        run.assert_not_called()

    def test_failed_create_never_switches(self):
        with (
            patch("letee.operations.sessions.create", side_effect=SystemExit("create failed")),
            patch("letee.cockpit.right_pane", return_value="pane"),
            patch("letee.__main__.cockpit.switch") as switch,
        ):
            with self.assertRaisesRegex(SystemExit, "create failed"):
                main(["create", "local", "work"])

        switch.assert_not_called()

    def test_create_flows_overlay_setting_into_session_commands(self):
        with (
            patch("letee.sessions.load_tmux_config_overlay", return_value=False) as overlay,
            patch("letee.sessions.subprocess.run") as run,
            patch("letee.cockpit.right_pane", return_value="pane"),
            patch("letee.cockpit.switch") as switch,
        ):
            main(["create", "local", "work"])
            attach_command = switch.call_args.args[1]
            self.assertTrue(callable(attach_command))
            command = attach_command()

        overlay.assert_called_with()
        self.assertEqual(run.call_args.args[0], ("tmux", "-L", "letee.inner", "new-session", "-d", "-s", "work"))
        self.assertEqual(command, "env -u TMUX tmux -L letee.inner -T clipboard new-session -A -s work")

    def test_create_sources_packaged_overlay_when_enabled(self):
        with (
            patch("letee.sessions.load_tmux_config_overlay", return_value=True),
            patch("letee.sessions.subprocess.run") as run,
            patch("letee.cockpit.right_pane", return_value="pane"),
            patch("letee.cockpit.switch") as switch,
        ):
            main(["create", "local", "work"])
            command = switch.call_args.args[1]()

        self.assertEqual(run.call_args_list[0].args[0], ("tmux", "-L", "letee.inner", "new-session", "-d", "-s", "work"))
        self.assertEqual(run.call_args_list[1].args[0], ("tmux", "-L", "letee.inner", "source-file", str(sessions.OVERLAY_FILE)))
        self.assertIn("\\; source-file", command)


class MainDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        diagnostics.close()
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.log_path = Path(tempdir.name) / "main.jsonl"
        env = patch.dict(os.environ, {"LETEE_DEBUG_LOG": str(self.log_path)}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(diagnostics.close)
        guard = patch.object(MAIN_MODULE.subprocess, "run", side_effect=AssertionError("unexpected subprocess"))
        guard.start()
        self.addCleanup(guard.stop)

    def records(self):
        diagnostics.flush()
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def assert_handled(self, operation, error_type=None, handling="fallback"):
        matches = [r for r in self.records() if r["event"] == "error" and r.get("operation") == operation]
        self.assertTrue(matches, operation)
        self.assertEqual(matches[-1]["handling"], handling)
        if error_type is not None:
            self.assertEqual(matches[-1]["error_type"], error_type)
        self.assertNotIn("PRIVATE_FAILURE", self.log_path.read_text())

    def test_missing_legacy_socket_and_socket_directory_return_empty_fallbacks(self):
        with patch.object(MAIN_MODULE.os, "lstat", side_effect=OSError("PRIVATE_FAILURE")):
            self.assertFalse(MAIN_MODULE._legacy_socket_present("work"))
        self.assert_handled("legacy_socket_probe", "OSError")
        with patch.object(Path, "iterdir", side_effect=OSError("PRIVATE_FAILURE")):
            self.assertEqual(MAIN_MODULE.list_servers(), [])
        self.assert_handled("list_server_sockets", "OSError")

    def test_server_probe_and_legacy_cleanup_exceptions_are_private_fallbacks(self):
        verified = subprocess.CompletedProcess([], 0, "1\n", "")
        for error in (OSError("PRIVATE_FAILURE"), subprocess.TimeoutExpired(["PRIVATE_FAILURE"], 1, output="PRIVATE_FAILURE", stderr="PRIVATE_FAILURE")):
            for stage in ("marker", "clients_or_kill"):
                responses = [error] if stage == "marker" else [verified, error]
                with (
                    self.subTest(error=type(error).__name__, stage=stage),
                    patch.object(MAIN_MODULE, "_run_server_tmux", side_effect=responses),
                ):
                    self.assertIsNone(MAIN_MODULE._server_attached("work"))
                self.assert_handled("server_attached_probe", type(error).__name__)
                with (
                    patch.object(MAIN_MODULE, "_legacy_socket_present", return_value=True),
                    patch.object(MAIN_MODULE, "_run_legacy_tmux", side_effect=responses),
                ):
                    self.assertIsNone(MAIN_MODULE._cleanup_legacy_server("work"))
                self.assert_handled("legacy_server_cleanup", type(error).__name__)

    def test_unverified_markers_are_logged_without_leaking_output(self):
        unverified = subprocess.CompletedProcess([], 0, "PRIVATE_FAILURE", "PRIVATE_FAILURE")
        with patch.object(MAIN_MODULE, "_run_server_tmux", return_value=unverified) as run:
            self.assertIsNone(MAIN_MODULE._server_attached("work"))
        run.assert_called_once()
        with (
            patch.object(MAIN_MODULE, "_legacy_socket_present", return_value=True),
            patch.object(MAIN_MODULE, "_run_legacy_tmux", return_value=unverified) as run,
        ):
            MAIN_MODULE._cleanup_legacy_server("work")
        run.assert_called_once()
        events = [r["event"] for r in self.records()]
        self.assertIn("server_unverified", events)
        self.assertIn("legacy_server_unverified", events)
        self.assertNotIn("PRIVATE_FAILURE", self.log_path.read_text())

    def test_nonzero_marker_client_and_kill_results_omit_subprocess_output(self):
        verified = subprocess.CompletedProcess([], 0, "1\n", "")
        failure = subprocess.CompletedProcess(["PRIVATE_FAILURE"], 7, "PRIVATE_FAILURE", "PRIVATE_FAILURE")
        for helper, responses, operation, handling in (
            (MAIN_MODULE._server_attached, [failure], "server_marker_probe", "fallback"),
            (MAIN_MODULE._server_attached, [verified, failure], "server_clients_probe", "fallback"),
            (MAIN_MODULE._cleanup_legacy_server, [failure], "legacy_marker_probe", "fallback"),
            (MAIN_MODULE._cleanup_legacy_server, [verified, failure], "legacy_server_kill", "ignored"),
        ):
            runner = "_run_server_tmux" if helper == MAIN_MODULE._server_attached else "_run_legacy_tmux"
            with (
                self.subTest(operation=operation),
                patch.object(MAIN_MODULE, "_legacy_socket_present", return_value=True),
                patch.object(MAIN_MODULE, runner, side_effect=responses),
            ):
                self.assertIsNone(helper("work"))
            self.assert_handled(operation, handling=handling)
            record = next(r for r in self.records() if r.get("operation") == operation)
            self.assertEqual(record["returncode"], 7)

    def test_invalid_socket_server_names_are_skipped_and_audited(self):
        for prefix, operation in (("letee@v1", "server_name_parse"), ("letee", "legacy_server_name_parse")):
            with (
                self.subTest(prefix=prefix),
                patch.object(Path, "iterdir", return_value=iter([Path(prefix + "-PRIVATE_FAILURE!")])),
                patch.object(MAIN_MODULE, "_server_attached") as probe,
                patch.object(MAIN_MODULE, "_cleanup_legacy_server") as cleanup,
            ):
                self.assertEqual(MAIN_MODULE.list_servers(), [])
            probe.assert_not_called()
            cleanup.assert_not_called()
            self.assert_handled(operation, "SystemExit", "ignored")

    def test_cli_reported_errors_flush_diagnostics_without_private_exception_payloads(self):
        errors = (
            subprocess.CalledProcessError(3, ["tmux", "PRIVATE_FAILURE"], output="PRIVATE_FAILURE stdout", stderr="PRIVATE_FAILURE stderr"),
            OSError(5, "PRIVATE_FAILURE", "/PRIVATE_FAILURE"),
            UnicodeDecodeError("utf-8", b"PRIVATE_FAILURE\xff", 15, 16, "PRIVATE_FAILURE"),
            subprocess.TimeoutExpired(["PRIVATE_FAILURE"], 1, output="PRIVATE_FAILURE", stderr="PRIVATE_FAILURE"),
        )
        for error in errors:
            output = StringIO()
            with (
                self.subTest(error=type(error).__name__),
                patch.object(MAIN_MODULE, "main", side_effect=error),
                patch.object(MAIN_MODULE.sys, "stderr", output),
                patch.object(diagnostics, "flush", wraps=diagnostics.flush) as flush,
            ):
                self.assertEqual(MAIN_MODULE.run_cli([]), 1)
            flush.assert_called_once_with()
            self.assertTrue(output.getvalue().startswith("letee: "))
            self.assert_handled("cli", type(error).__name__, "reported")

    def test_cli_propagation_flushes_and_retains_original_exception_privately(self):
        for error in (SystemExit("PRIVATE_FAILURE"), KeyboardInterrupt("PRIVATE_FAILURE"), RuntimeError("PRIVATE_FAILURE")):
            with (
                self.subTest(error=type(error).__name__),
                patch.object(MAIN_MODULE, "main", side_effect=error),
                patch.object(diagnostics, "flush", wraps=diagnostics.flush) as flush,
                self.assertRaises(type(error)) as raised,
            ):
                MAIN_MODULE.run_cli([])
            self.assertIs(raised.exception, error)
            flush.assert_called_once_with()
            self.assert_handled("cli", type(error).__name__, "cancel" if isinstance(error, KeyboardInterrupt) else "propagate")


if __name__ == "__main__":
    unittest.main()
