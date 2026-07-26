import unittest
from unittest.mock import patch

from mtmux import cockpit


class CockpitLayoutTest(unittest.TestCase):
    def test_fix_layout_pins_sidebar_to_configured_width(self):
        calls = []

        with patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)):
            cockpit._fix_layout("%1", 52)

        self.assertEqual(
            calls,
            [
                ("set-window-option", "-t", cockpit.TARGET, "main-pane-width", "52"),
                ("set-window-option", "-u", "-t", cockpit.TARGET, "window-style"),
                ("set-window-option", "-u", "-t", cockpit.TARGET, "window-active-style"),
                ("set-window-option", "-t", cockpit.TARGET, "pane-border-style", "fg=terminal"),
                ("set-window-option", "-t", cockpit.TARGET, "pane-active-border-style", "fg=terminal"),
                ("set-window-option", "-t", cockpit.TARGET, "pane-border-lines", "single"),
                ("resize-pane", "-t", "%1", "-x", "52"),
            ],
        )

    def test_configure_cockpit_applies_complete_runtime_configuration(self):
        with (
            patch.object(cockpit, "_set_markers") as set_markers,
            patch.object(cockpit, "_fix_layout") as fix_layout,
            patch.object(cockpit, "_install_layout_hooks") as install_layout_hooks,
            patch.object(cockpit, "_install_bell_hook") as install_bell_hook,
            patch.object(cockpit, "_install_right_pane_reset") as install_right_pane_reset,
            patch.object(cockpit, "_enable_mouse") as enable_mouse,
            patch.object(cockpit, "_enable_clipboard") as enable_clipboard,
            patch.object(cockpit, "_enable_truecolor") as enable_truecolor,
            patch.object(cockpit, "_install_bindings") as install_bindings,
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit._configure_cockpit("%1", "%2", "C-x", 52)

        set_markers.assert_called_once_with("%1", "%2")
        fix_layout.assert_called_once_with("%1", 52)
        install_layout_hooks.assert_called_once_with("%1", 52)
        install_bell_hook.assert_called_once_with()
        install_right_pane_reset.assert_called_once_with("%1", "%2")
        enable_mouse.assert_called_once_with()
        enable_clipboard.assert_called_once_with()
        enable_truecolor.assert_called_once_with()
        install_bindings.assert_called_once_with("C-x", "%1", "%2")
        self.assertEqual(
            tmux_call.call_args_list,
            [
                unittest.mock.call("set-option", "-t", "mtmux", "prefix", "C-x"),
                unittest.mock.call("set-option", "-t", "mtmux", "status", "off"),
                unittest.mock.call("set-option", "-s", "escape-time", "0"),
            ],
        )

    def test_existing_cockpit_gets_layout_reapplied(self):
        with (
            patch.object(cockpit, "_valid", return_value=True),
            patch.object(cockpit, "_option", return_value="%1"),
            patch.object(cockpit, "_set_markers") as set_markers,
            patch.object(cockpit, "_fix_layout") as fix_layout,
            patch.object(cockpit, "_install_layout_hooks") as install_layout_hooks,
            patch.object(cockpit, "_install_bindings") as install_bindings,
            patch.object(cockpit, "_enable_mouse") as enable_mouse,
            patch.object(cockpit, "_enable_clipboard") as enable_clipboard,
            patch.object(cockpit, "_enable_truecolor") as enable_truecolor,
            patch.object(cockpit, "_install_bell_hook") as install_bell_hook,
            patch.object(cockpit, "_install_right_pane_reset") as install_right_pane_reset,
            patch.object(cockpit, "load_prefix", return_value="C-x"),
            patch.object(cockpit, "load_sidebar_width", return_value=52),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.ensure_cockpit()

        set_markers.assert_called_once_with("%1", "%1")
        fix_layout.assert_called_once_with("%1", 52)
        install_layout_hooks.assert_called_once_with("%1", 52)
        install_bell_hook.assert_called_once_with()
        install_right_pane_reset.assert_called_once_with("%1", "%1")
        install_bindings.assert_called_once_with("C-x", "%1", "%1")
        enable_mouse.assert_called_once_with()
        enable_clipboard.assert_called_once_with()
        enable_truecolor.assert_called_once_with()
        self.assertEqual(
            tmux_call.call_args_list,
            [
                unittest.mock.call("set-option", "-t", "mtmux", "prefix", "C-x"),
                unittest.mock.call("set-option", "-t", "mtmux", "status", "off"),
                unittest.mock.call("set-option", "-s", "escape-time", "0"),
            ],
        )

    def test_layout_hooks_repin_sidebar_after_attach_or_resize(self):
        calls = []

        with patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)):
            cockpit._install_layout_hooks("%1", 52)

        command = "run-shell -b 'for delay in 0.1 0.2 0.4; do sleep \"$delay\"; tmux -L mtmux resize-pane -t %1 -x 52 2>/dev/null && break; done; true'"
        self.assertEqual(
            calls,
            [
                ("set-hook", "-t", "mtmux", "client-attached", command),
                ("set-hook", "-t", "mtmux", "client-resized", command),
            ],
        )

    def test_enable_mouse_sets_runtime_option_without_live_border_dragging(self):
        with patch.object(cockpit.tmux, "tmux") as tmux_call:
            cockpit._enable_mouse()

        self.assertEqual(
            tmux_call.call_args_list,
            [
                unittest.mock.call("set-option", "-t", "mtmux", "mouse", "on"),
                unittest.mock.call("unbind-key", "-q", "-T", "root", "MouseDrag1Border"),
            ],
        )

    def test_enable_clipboard_sets_runtime_server_option(self):
        with patch.object(cockpit.tmux, "tmux") as tmux_call:
            cockpit._enable_clipboard()

        tmux_call.assert_called_once_with("set-option", "-s", "set-clipboard", "on")

    def test_enable_truecolor_appends_rgb_when_colorterm_is_truecolor(self):
        with patch.dict(cockpit.os.environ, {"COLORTERM": "truecolor"}), patch.object(cockpit.tmux, "tmux") as tmux_call:
            cockpit._enable_truecolor()

        tmux_call.assert_called_once_with("set-option", "-as", "terminal-features", ",xterm-256color:RGB")

    def test_enable_truecolor_appends_rgb_when_colorterm_is_24bit(self):
        with patch.dict(cockpit.os.environ, {"COLORTERM": "24bit"}), patch.object(cockpit.tmux, "tmux") as tmux_call:
            cockpit._enable_truecolor()

        tmux_call.assert_called_once_with("set-option", "-as", "terminal-features", ",xterm-256color:RGB")

    def test_enable_truecolor_skips_when_colorterm_is_absent(self):
        with patch.dict(cockpit.os.environ, {}, clear=True), patch.object(cockpit.tmux, "tmux") as tmux_call:
            cockpit._enable_truecolor()

        tmux_call.assert_not_called()

    def test_bindings_replace_outer_prefix_table_with_mtmux_shortcuts(self):
        calls = []

        with patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)):
            cockpit._install_bindings("C-x", "%1", "%2")

        self.assertEqual(
            calls,
            [
                ("unbind-key", "-a", "-T", "prefix"),
                ("bind-key", "C-x", "send-prefix"),
                ("bind-key", "d", "detach-client"),
                ("bind-key", "h", "kill-pane", "-t", "%1"),
                ("bind-key", "q", "kill-session", "-t", "mtmux"),
                ("bind-key", "a", "run-shell", f"{cockpit.FOCUS_SIDEBAR} agents"),
                ("bind-key", "s", "run-shell", f"{cockpit.FOCUS_SIDEBAR} sessions"),
                ("bind-key", "n", "run-shell", f"{cockpit.FOCUS_SIDEBAR} new"),
                ("bind-key", "?", "respawn-pane", "-k", "-t", "%2", cockpit.help_command("C-x")),
                *[
                    ("bind-key", str(slot), "run-shell", f"{cockpit.shlex.quote(cockpit.sys.executable)} -m mtmux switch-session {slot}")
                    for slot in range(1, 10)
                ],
            ],
        )

    def test_focus_sidebar_recreates_selects_and_injects_region_key(self):
        with (
            patch.object(cockpit, "ensure_config"),
            patch.object(cockpit, "ensure_cockpit"),
            patch.object(cockpit, "_option", return_value="%7"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.focus_sidebar("agents")

        self.assertEqual(
            tmux_call.call_args_list,
            [
                unittest.mock.call("select-pane", "-t", "%7"),
                unittest.mock.call("send-keys", "-t", "%7", "F7"),
            ],
        )

    def test_focus_sidebar_new_opens_session_creator(self):
        with (
            patch.object(cockpit, "ensure_config"),
            patch.object(cockpit, "ensure_cockpit"),
            patch.object(cockpit, "_option", return_value="%7"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.focus_sidebar("new")

        self.assertEqual(
            tmux_call.call_args_list,
            [
                unittest.mock.call("select-pane", "-t", "%7"),
                unittest.mock.call("send-keys", "-t", "%7", "F6", "n"),
            ],
        )

    def test_right_pane_reset_shows_unavailable_message_and_preserves_target(self):
        calls = []

        with patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)):
            cockpit._install_right_pane_reset("%1", "%2")

        command = calls[1][4]
        self.assertEqual(calls[0], ("set-option", "-p", "-t", "%2", "remain-on-exit", "on"))
        self.assertEqual(calls[1][:4], ("set-hook", "-t", "mtmux", "pane-died"))
        self.assertIn("Active session is unavailable.", command)
        self.assertNotIn("set-option -u -t mtmux @mtmux_current_target", command)
        self.assertIn("select-pane -t %1", command)

    def test_help_uses_configured_prefix(self):
        command = cockpit.help_command("C-x")

        self.assertIn("C-x a  focus/open Agents", command)
        self.assertIn("C-x s  focus/open Sessions", command)
        self.assertIn("C-x n  new session", command)
        self.assertIn("C-x h  hide sidebar", command)
        self.assertIn("C-x q  quit cockpit", command)
        self.assertIn("C-x 1-9  switch session", command)
        self.assertIn("C-x ?  open help", command)
        self.assertIn("K/J    move session up/down", command)
        self.assertIn("Agent actions", command)
        self.assertIn("h/l    cycle agent ordering", command)
        self.assertIn("Enter  switch session / open Add / create on host", command)
        self.assertIn("n      open grouped local/SSH Add picker", command)
        self.assertNotIn("a      open grouped local/SSH Add picker", command)
        self.assertIn("r      remove selected session", command)
        self.assertNotIn("f      star/unstar", command)
        self.assertNotIn("r      refresh", command)
        self.assertIn("C-x d  detach cockpit", command)
        self.assertTrue(command.endswith("; exec tail -f /dev/null"))
        self.assertNotIn("exec sh", command)

    def test_new_cockpit_sets_configured_prefix_and_startup_help(self):
        calls = []

        def tmux_call(*args, **kwargs):
            calls.append((args, kwargs))
            return type("Result", (), {"returncode": 1})()

        with (
            patch.object(cockpit, "ensure_config", return_value=(None, "wrapper")),
            patch.object(cockpit.tmux, "tmux", side_effect=tmux_call),
            patch.object(cockpit.tmux, "out", side_effect=["%2", "%1"]) as tmux_out,
            patch.object(cockpit, "_fix_layout"),
            patch.object(cockpit, "_set_markers"),
            patch.object(cockpit, "_install_layout_hooks"),
            patch.object(cockpit, "_install_bell_hook"),
            patch.object(cockpit, "_install_right_pane_reset"),
            patch.object(cockpit, "_install_bindings"),
            patch.object(cockpit, "_enable_clipboard") as enable_clipboard,
            patch.object(cockpit, "_enable_truecolor"),
        ):
            cockpit._build("C-x", 52)

        enable_clipboard.assert_called_once_with()
        self.assertIn((("set-option", "-t", "mtmux", "prefix", "C-x"), {}), calls)
        self.assertIn((("set-option", "-t", "mtmux", "mouse", "on"), {}), calls)
        self.assertIn((("set-option", "-s", "escape-time", "0"), {}), calls)
        new_session = next(args for args, _ in calls if args[0] == "new-session")
        self.assertIn("C-x s  focus/open Sessions", new_session[-1])
        split = tmux_out.call_args_list[1].args
        self.assertEqual(split[split.index("-l") + 1], "52")

    def test_missing_sidebar_recreation_reapplies_mouse(self):
        with (
            patch.object(cockpit, "load_prefix", return_value="C-x"),
            patch.object(cockpit, "load_sidebar_width", return_value=52),
            patch.object(cockpit, "_valid", return_value=False),
            patch.object(cockpit, "_option", side_effect=lambda name: "1" if name == "@mtmux_cockpit" else "%2"),
            patch.object(cockpit.tmux, "has_pane", return_value=True),
            patch.object(cockpit.tmux, "out", return_value="%1"),
            patch.object(cockpit.tmux, "tmux"),
            patch.object(cockpit, "_fix_layout"),
            patch.object(cockpit, "_set_markers"),
            patch.object(cockpit, "_install_layout_hooks"),
            patch.object(cockpit, "_install_bell_hook"),
            patch.object(cockpit, "_install_right_pane_reset"),
            patch.object(cockpit, "_install_bindings"),
            patch.object(cockpit, "_enable_mouse") as enable_mouse,
            patch.object(cockpit, "_enable_clipboard") as enable_clipboard,
            patch.object(cockpit, "_enable_truecolor"),
        ):
            cockpit.ensure_cockpit()

        enable_mouse.assert_called_once_with()
        enable_clipboard.assert_called_once_with()

    def test_switch_uses_valid_right_pane_and_supplied_attach_command(self):
        calls = []
        target = cockpit.Target("local", "work")
        with (
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)),
        ):
            cockpit.switch(target, "attach work")

        self.assertEqual(
            calls,
            [
                ("set-option", "-t", "mtmux", "@mtmux_current_target", "local:work"),
                ("set-option", "-u", "-t", "mtmux", "@mtmux_current_agent"),
                ("set-option", "-u", "-t", "mtmux", "@mtmux_bell_target"),
                ("respawn-pane", "-k", "-t", "%2", "attach work"),
                ("select-pane", "-t", "%2"),
            ],
        )

    def test_agent_switch_persists_exact_agent_and_getter_recovers_it(self):
        calls = []
        target = cockpit.Target("local", "work")
        with (
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)),
        ):
            cockpit.switch(target, "attach work", "agent-1")

        self.assertIn(("set-option", "-t", "mtmux", "@mtmux_current_agent", "agent-1"), calls)
        with patch.object(cockpit, "_option", side_effect=["agent-1", ""]):
            self.assertEqual(cockpit.current_agent(), "agent-1")
            self.assertIsNone(cockpit.current_agent())

    def test_switch_rejects_missing_cockpit(self):
        with patch.object(cockpit, "right_pane", return_value=None):
            with self.assertRaisesRegex(SystemExit, "No valid mtmux cockpit"):
                cockpit.switch(cockpit.Target("local", "work"), "attach work")

    def test_show_reconnecting_replaces_frozen_session_with_feedback(self):
        with (
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.show_reconnecting(cockpit.Target("ssh", "work", "dev"))

        command = tmux_call.call_args.args
        self.assertEqual(command[:4], ("respawn-pane", "-k", "-t", "%2"))
        self.assertIn("Reconnecting to ssh:dev:work", command[4])
        self.assertTrue(command[4].endswith("; exec tail -f /dev/null"))
        self.assertNotIn("exec sh", command[4])

    def test_show_unavailable_replaces_frozen_session_with_message(self):
        with (
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.show_unavailable(cockpit.Target("ssh", "work", "dev"))

        command = tmux_call.call_args.args
        self.assertEqual(command[:4], ("respawn-pane", "-k", "-t", "%2"))
        self.assertIn("Session ssh:dev:work is unavailable.", command[4])
        self.assertIn("Select another session", command[4])

    def test_show_help_respawns_right_pane(self):
        with (
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit, "load_prefix", return_value="C-x"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.show_help()

        command = tmux_call.call_args.args
        self.assertEqual(command[:4], ("respawn-pane", "-k", "-t", "%2"))
        self.assertIn("C-x s  focus/open Sessions", command[4])

    def test_current_target_recovers_from_right_pane_command(self):
        with (
            patch.object(cockpit, "_option", return_value=""),
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "out", return_value="ssh -t dev 'tmux -T clipboard new-session -A -s work'"),
        ):
            self.assertEqual(cockpit.current_target(), cockpit.Target("ssh", "work", "dev"))

    def test_current_target_recovers_from_option_rich_ssh_command(self):
        command = "ssh -o ControlMaster=auto -o ControlPersist=10m -o 'ControlPath=~/.ssh/mtmux-%C' -t dev 'tmux -T clipboard new-session -A -s work'"
        with (
            patch.object(cockpit, "_option", return_value=""),
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "out", return_value=command),
        ):
            self.assertEqual(cockpit.current_target(), cockpit.Target("ssh", "work", "dev"))

    def test_bell_target_returns_valid_target_only(self):
        with patch.object(cockpit, "_option", side_effect=["local:work", "bad"]):
            self.assertEqual(cockpit.bell_target(), cockpit.Target("local", "work"))
            self.assertIsNone(cockpit.bell_target())

    def test_sidebar_active_reads_managed_sidebar_pane(self):
        with (
            patch.object(cockpit, "_option", return_value="%1"),
            patch.object(cockpit.tmux, "out", return_value="1") as out,
        ):
            self.assertTrue(cockpit.sidebar_active())

        out.assert_called_once_with("display-message", "-p", "-t", "%1", "#{pane_active}", check=False)

    def test_install_bell_hook_enables_outer_tmux_bells(self):
        calls = []

        with patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)):
            cockpit._install_bell_hook()

        self.assertEqual(
            calls,
            [
                ("set-window-option", "-t", "mtmux:cockpit", "monitor-bell", "on"),
                ("set-option", "-t", "mtmux", "bell-action", "any"),
                (
                    "set-hook",
                    "-t",
                    "mtmux",
                    "alert-bell",
                    "set-option -F -t mtmux @mtmux_bell_target '#{@mtmux_current_target}'",
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
