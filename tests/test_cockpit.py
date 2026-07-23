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
                ("select-pane", "-t", "%1"),
                ("select-layout", "-t", cockpit.TARGET, "main-vertical"),
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
        install_right_pane_reset.assert_called_once_with("%1", "%2", "C-x")
        enable_mouse.assert_called_once_with()
        enable_clipboard.assert_called_once_with()
        enable_truecolor.assert_called_once_with()
        install_bindings.assert_called_once_with("C-x")
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
        install_right_pane_reset.assert_called_once_with("%1", "%1", "C-x")
        install_bindings.assert_called_once_with("C-x")
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

        command = "set-window-option -t mtmux:cockpit main-pane-width 52 ; select-pane -t %1 ; select-layout -t mtmux:cockpit main-vertical"
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

    def test_bindings_include_sidebar_focus_and_numbered_session_shortcuts(self):
        calls = []

        with patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)):
            cockpit._install_bindings("C-x")

        self.assertEqual(
            calls,
            [
                ("bind-key", "C-x", "send-prefix"),
                ("bind-key", "s", "run-shell", cockpit.FOCUS_SIDEBAR),
                *[
                    ("bind-key", str(slot), "run-shell", f"{cockpit.shlex.quote(cockpit.sys.executable)} -m mtmux switch-session {slot}")
                    for slot in range(1, 10)
                ],
            ],
        )

    def test_right_pane_reset_restores_startup_help_and_focuses_sidebar(self):
        calls = []

        with patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)):
            cockpit._install_right_pane_reset("%1", "%2", "C-x")

        command = f"if-shell -F '#{{==:#{{hook_pane}},%2}}' {{ set-option -u -t mtmux @mtmux_current_target ; set-option -u -t mtmux @mtmux_current_agent ; set-option -u -t mtmux @mtmux_bell_target ; respawn-pane -k -t %2 {cockpit.shlex.quote(cockpit.help_command('C-x'))} ; select-pane -t %1 }}"
        self.assertEqual(
            calls,
            [
                ("set-option", "-p", "-t", "%2", "remain-on-exit", "on"),
                ("set-hook", "-t", "mtmux", "pane-died", command),
            ],
        )

    def test_help_uses_configured_prefix(self):
        command = cockpit.help_command("C-x")

        self.assertIn("C-x s  focus/open sidebar", command)
        self.assertIn("C-x 1-9  switch session", command)
        self.assertIn("K/J    move session up/down", command)
        self.assertIn("Agent actions", command)
        self.assertIn("h/l    cycle agent ordering", command)
        self.assertIn("Enter  switch session / open Add / create on host", command)
        self.assertIn("a      open grouped local/SSH Add picker", command)
        self.assertIn("r      remove selected session", command)
        self.assertNotIn("f      star/unstar", command)
        self.assertNotIn("r      refresh", command)
        self.assertIn("C-x d  detach cockpit", command)

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
        self.assertIn("C-x s  focus/open sidebar", new_session[-1])
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

    def test_show_help_respawns_right_pane(self):
        with (
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit, "load_prefix", return_value="C-x"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.show_help()

        command = tmux_call.call_args.args
        self.assertEqual(command[:4], ("respawn-pane", "-k", "-t", "%2"))
        self.assertIn("C-x s  focus/open sidebar", command[4])

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
