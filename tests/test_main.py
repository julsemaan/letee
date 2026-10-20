import importlib
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
from letee import __version__
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
        with (
            patch("letee.operations.sessions.kill") as kill,
            patch("letee.config.load_sessions", return_value=[target, other]),
            patch("letee.config.save_sessions") as save,
            patch("letee.__main__.cockpit.reset_to_help") as reset,
        ):
            main(["kill", target.format()])

        kill.assert_called_once_with(target)
        save.assert_called_once_with([other])
        reset.assert_called_once_with(target)

    def test_kill_untracked_target_saves_unchanged_sessions(self):
        target = Target("local", "untracked")
        other = Target("local", "other")
        with (
            patch("letee.operations.sessions.kill") as kill,
            patch("letee.config.load_sessions", return_value=[other]),
            patch("letee.config.save_sessions") as save,
            patch("letee.__main__.cockpit.reset_to_help") as reset,
        ):
            self.assertEqual(main(["kill", target.format()]), 0)

        kill.assert_called_once_with(target)
        save.assert_called_once_with([other])
        reset.assert_called_once_with(target)

    def test_rename_tmux_then_replaces_tracked_target(self):
        target = Target("ssh", "old", "dev")
        renamed = Target("ssh", "new", "dev")
        other = Target("local", "other")
        with (
            patch("letee.config.load_sessions", return_value=[target, other]),
            patch("letee.operations.sessions.rename", return_value=renamed) as rename,
            patch("letee.config.save_sessions") as save,
            patch("letee.__main__.cockpit.rename_target") as rename_target,
        ):
            self.assertEqual(main(["rename", target.format(), "new"]), 0)

        rename.assert_called_once_with(target, "new")
        save.assert_called_once_with([renamed, other])
        rename_target.assert_called_once_with(target, renamed)

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
            patch("letee.config.save_sessions") as replace,
        ):
            with self.assertRaisesRegex(SystemExit, "^rename failed$"):
                main(["rename", target.format(), "new"])

        rename.assert_called_once_with(target, "new")
        replace.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
