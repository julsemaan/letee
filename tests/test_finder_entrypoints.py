import shlex
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

from letee import cockpit, config, tmux
from letee.__main__ import main
from letee.names import server_socket


class FinderEntrypointsTest(unittest.TestCase):
    def test_popup_preserves_layout_and_focus_in_named_server(self):
        with (
            patch.object(tmux, "SERVER", "work"),
            patch.object(tmux, "SOCKET", server_socket("work")),
            patch.object(tmux, "tmux_executable", return_value="tmux"),
            patch.object(cockpit, "_valid", return_value=True),
            patch.object(cockpit, "ensure_cockpit") as ensure,
            patch.object(cockpit, "expand_sidebar") as expand,
            patch.object(cockpit, "ensure_config") as ensure_config,
            patch("letee.tmux.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run,
        ):
            self.assertEqual(cockpit.find_work(), 0)

        run.assert_called_once_with(
            ["tmux", "-L", server_socket("work"), "display-popup", "-E",
             "-w", "80%", "-h", "80%", "-x", "C", "-y", "C",
             shlex.join([sys.executable, "-m", "letee", "-L", "work", "finder"])],
            text=True, capture_output=False, check=True, timeout=None,
        )
        ensure.assert_not_called()
        expand.assert_not_called()
        ensure_config.assert_not_called()

    def test_popup_requires_existing_valid_cockpit(self):
        with (
            patch.object(cockpit, "_valid", return_value=False),
            patch.object(cockpit, "ensure_cockpit") as ensure,
            patch.object(tmux, "tmux") as command,
        ):
            with self.assertRaisesRegex(SystemExit, cockpit.NO_COCKPIT):
                cockpit.find_work()
        ensure.assert_not_called()
        command.assert_not_called()

    def test_find_work_dispatches_in_named_context(self):
        with (
            patch("letee.__main__._configure_server") as configure,
            patch.object(cockpit, "find_work", return_value=0) as launch,
        ):
            self.assertEqual(main(["-L", "work", "find-work"]), 0)
        configure.assert_called_once_with("work")
        launch.assert_called_once_with()

    def test_finder_worker_dispatches_lazily(self):
        worker = types.ModuleType("letee.finder_ui")
        with (
            patch.dict(sys.modules, {"letee.finder_ui": worker}),
            patch.object(worker, "main", return_value=0, create=True) as run,
            patch("letee.__main__._configure_server") as configure,
        ):
            self.assertEqual(main(["-L", "work", "finder"]), 0)
        configure.assert_called_once_with("work")
        run.assert_called_once_with()

    def test_find_binding_is_configurable_background_command(self):
        self.assertEqual(config.DEFAULT_KEYBINDINGS["find_work"], "prefix+f")
        for value, binding in (("prefix+M-f", ("bind-key", "M-f")), ("M-f", ("bind-key", "-n", "M-f"))):
            with (
                self.subTest(value=value),
                patch.object(tmux, "SERVER", "work"),
                patch.object(cockpit, "_option", return_value=""),
                patch.object(cockpit, "load_sidebar_keybindings", return_value=config.DEFAULT_SIDEBAR_KEYBINDINGS),
                patch.object(tmux, "tmux") as command,
            ):
                cockpit._install_bindings("C-s", "%left", "%right", {**config.DEFAULT_KEYBINDINGS, "find_work": value})
                command.assert_any_call(*binding, "run-shell", "-b", cockpit._letee_command("find-work"))

    def test_help_merges_incomplete_bindings_with_defaults(self):
        command = cockpit.help_command("C-s", {"focus_agents": "prefix+M-a"}, config.DEFAULT_SIDEBAR_KEYBINDINGS)
        self.assertIn("C-s f  find work", command)
        self.assertIn("C-s M-a  focus/open Agents", command)
        command = cockpit.help_command("C-s", {"find_work": "M-f"}, config.DEFAULT_SIDEBAR_KEYBINDINGS)
        self.assertIn("M-f  find work", command)
