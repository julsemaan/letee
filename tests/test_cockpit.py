import unittest
from unittest.mock import call, patch

from letee import cockpit
from letee.names import PaneTarget, Target


class CockpitStartupTest(unittest.TestCase):
    def test_shared_agent_warms_first_host_then_probes_remaining_hosts(self):
        events = []

        def prepare(host):
            events.append(("prepare", host))
            return True

        def probe(hosts):
            events.append(("probe", tuple(hosts)))
            return [True]

        with (
            patch.object(cockpit, "ensure_config"),
            patch.object(cockpit.shutil, "get_terminal_size", return_value=type("Size", (), {"columns": 100})()),
            patch.object(cockpit, "load_hosts", return_value=["dev", "prod"]),
            patch.object(cockpit.sessions, "ensure_ssh_agent", side_effect=lambda: events.append(("agent",)) or "/tmp/agent"),
            patch.object(cockpit.sessions, "group_hosts", side_effect=lambda hosts: events.append(("groups", tuple(hosts))) or [["dev", "prod"]]),
            patch.object(cockpit.sessions, "prepare_host", side_effect=prepare),
            patch.object(cockpit.sessions, "probe_hosts", side_effect=probe),
            patch.object(cockpit, "ensure_cockpit", side_effect=lambda: events.append(("cockpit",))),
            patch.object(cockpit, "_attach", return_value=0),
            patch.object(cockpit.sys.stdin, "isatty", return_value=True),
            patch.object(cockpit.sys.stdout, "isatty", return_value=True),
        ):
            self.assertEqual(cockpit.cockpit(), 0)

        self.assertEqual(
            events,
            [
                ("agent",),
                ("groups", ("dev", "prod")),
                ("prepare", "dev"),
                ("probe", ("prod",)),
                ("cockpit",),
            ],
        )

    def test_warmup_failure_tries_next_host_in_group(self):
        order = []

        def prepare(host):
            order.append(("prepare", host))
            return host == "two"

        def probe(hosts):
            order.append(("probe", tuple(hosts)))
            return [True]

        with (
            patch.object(cockpit, "ensure_config"),
            patch.object(cockpit.shutil, "get_terminal_size", return_value=type("Size", (), {"columns": 100})()),
            patch.object(cockpit, "load_hosts", return_value=["one", "two", "three"]),
            patch.object(cockpit.sessions, "ensure_ssh_agent", return_value="/tmp/agent"),
            patch.object(cockpit.sessions, "group_hosts", return_value=[["one", "two", "three"]]),
            patch.object(cockpit.sessions, "prepare_host", side_effect=prepare),
            patch.object(cockpit.sessions, "probe_hosts", side_effect=probe),
            patch.object(cockpit, "ensure_cockpit"),
            patch.object(cockpit, "_attach", return_value=0),
            patch("builtins.print"),
            patch.object(cockpit.sys.stdin, "isatty", return_value=True),
            patch.object(cockpit.sys.stdout, "isatty", return_value=True),
        ):
            cockpit.cockpit()

        self.assertEqual(order, [("prepare", "one"), ("prepare", "two"), ("probe", ("three",))])

    def test_different_groups_warm_each_group_before_one_parallel_probe(self):
        order = []

        def prepare(host):
            order.append(("prepare", host))
            return True

        def probe(hosts):
            order.append(("probe", tuple(hosts)))
            return [True, True]

        with (
            patch.object(cockpit, "ensure_config"),
            patch.object(cockpit.shutil, "get_terminal_size", return_value=type("Size", (), {"columns": 100})()),
            patch.object(cockpit, "load_hosts", return_value=["one", "two", "three", "four"]),
            patch.object(cockpit.sessions, "ensure_ssh_agent", return_value="/tmp/agent"),
            patch.object(cockpit.sessions, "group_hosts", return_value=[["one", "two"], ["three", "four"]]),
            patch.object(cockpit.sessions, "prepare_host", side_effect=prepare),
            patch.object(cockpit.sessions, "probe_hosts", side_effect=probe),
            patch.object(cockpit, "ensure_cockpit"),
            patch.object(cockpit, "_attach", return_value=0),
            patch("builtins.print"),
            patch.object(cockpit.sys.stdin, "isatty", return_value=True),
            patch.object(cockpit.sys.stdout, "isatty", return_value=True),
        ):
            cockpit.cockpit()

        self.assertEqual(
            order,
            [("prepare", "one"), ("prepare", "three"), ("probe", ("two", "four"))],
        )

    def test_all_connecting_statuses_print_before_parallel_probe(self):
        events = []

        def print_call(*args, **kwargs):
            events.append(("print", args[0], kwargs.get("flush")))

        def prepare(host):
            events.append(("prepare", host))
            return True

        def probe(hosts):
            events.append(("probe", tuple(hosts)))
            return [True]

        with (
            patch.object(cockpit, "ensure_config"),
            patch.object(cockpit.shutil, "get_terminal_size", return_value=type("Size", (), {"columns": 100})()),
            patch.object(cockpit, "load_hosts", return_value=["dev", "prod"]),
            patch.object(cockpit.sessions, "ensure_ssh_agent", return_value="/tmp/agent"),
            patch.object(cockpit.sessions, "group_hosts", return_value=[["dev", "prod"]]),
            patch.object(cockpit.sessions, "prepare_host", side_effect=prepare),
            patch.object(cockpit.sessions, "probe_hosts", side_effect=probe),
            patch.object(cockpit, "ensure_cockpit"),
            patch.object(cockpit, "_attach", return_value=0),
            patch("builtins.print", side_effect=print_call),
            patch.object(cockpit.sys.stdin, "isatty", return_value=True),
            patch.object(cockpit.sys.stdout, "isatty", return_value=True),
        ):
            cockpit.cockpit()

        probe_index = events.index(("probe", ("prod",)))
        connecting = [
            item[1]
            for item in events[:probe_index]
            if item[0] == "print" and "connecting" in item[1]
        ]
        self.assertEqual(connecting, [
            "[1/2] dev — connecting...",
            "[2/2] prod — connecting...",
        ])
        self.assertTrue(all(item[2] for item in events[:probe_index] if item[0] == "print"))

    def test_partial_failure_summary_contains_recovery_command(self):
        output = []
        with (
            patch.object(cockpit, "ensure_config"),
            patch.object(cockpit.shutil, "get_terminal_size", return_value=type("Size", (), {"columns": 100})()),
            patch.object(cockpit, "load_hosts", return_value=["dev", "prod"]),
            patch.object(cockpit.sessions, "ensure_ssh_agent", return_value=None),
            patch.object(cockpit.sessions, "group_hosts", return_value=[["dev", "prod"]]),
            patch.object(cockpit.sessions, "prepare_host", side_effect=[True, False]) as prepare_host,
            patch.object(cockpit.sessions, "probe_hosts", return_value=[False]),
            patch.object(cockpit, "ensure_cockpit"),
            patch.object(cockpit, "_attach", return_value=0),
            patch("builtins.print", side_effect=lambda *args, **kwargs: output.append(str(args[0]))),
            patch.object(cockpit.sys.stdin, "isatty", return_value=True),
            patch.object(cockpit.sys.stdout, "isatty", return_value=True),
        ):
            self.assertEqual(cockpit.cockpit(), 0)

        self.assertEqual(
            prepare_host.call_args_list,
            [call("dev"), call("prod")],
        )
        text = "\n".join(output)
        self.assertIn("SSH agent: unavailable", text)
        self.assertIn("SSH check complete: 1 ready, 1 failed.", text)
        self.assertIn("ssh prod", text)
        self.assertIn("Starting letee...", text)

    def test_only_failed_probes_retry_sequentially_after_agent_setup(self):
        order = []

        def probe(hosts):
            order.append(("probe", tuple(hosts)))
            return [False, False]

        def prepare(host):
            order.append(("prepare", host))
            return host == "three"

        with (
            patch.object(cockpit, "ensure_config"),
            patch.object(cockpit.shutil, "get_terminal_size", return_value=type("Size", (), {"columns": 100})()),
            patch.object(cockpit, "load_hosts", return_value=["one", "two", "three"]),
            patch.object(cockpit.sessions, "ensure_ssh_agent", side_effect=lambda: order.append("agent") or "/tmp/agent"),
            patch.object(cockpit.sessions, "group_hosts", side_effect=lambda hosts: order.append(("groups", tuple(hosts))) or [["one", "two", "three"]]),
            patch.object(cockpit.sessions, "prepare_host", side_effect=lambda host: True if host == "one" else prepare(host)),
            patch.object(cockpit.sessions, "probe_hosts", side_effect=probe),
            patch.object(cockpit, "ensure_cockpit", side_effect=lambda: order.append("cockpit")),
            patch.object(cockpit, "_attach", return_value=0),
            patch("builtins.print"),
            patch.object(cockpit.sys.stdin, "isatty", return_value=True),
            patch.object(cockpit.sys.stdout, "isatty", return_value=True),
        ):
            cockpit.cockpit()

        self.assertEqual(
            order,
            [
                "agent",
                ("groups", ("one", "two", "three")),
                ("probe", ("two", "three")),
                ("prepare", "two"),
                ("prepare", "three"),
                "cockpit",
            ],
        )

    def test_no_hosts_skips_ssh_setup_silently(self):
        with (
            patch.object(cockpit, "ensure_config"),
            patch.object(cockpit.shutil, "get_terminal_size", return_value=type("Size", (), {"columns": 100})()),
            patch.object(cockpit, "load_hosts", return_value=[]),
            patch.object(cockpit.sessions, "ensure_ssh_agent") as ensure_agent,
            patch.object(cockpit.sessions, "prepare_host") as prepare_host,
            patch.object(cockpit, "ensure_cockpit"),
            patch.object(cockpit, "_attach", return_value=0),
            patch("builtins.print") as print_,
            patch.object(cockpit.sys.stdin, "isatty", return_value=True),
            patch.object(cockpit.sys.stdout, "isatty", return_value=True),
        ):
            self.assertEqual(cockpit.cockpit(), 0)

        ensure_agent.assert_not_called()
        prepare_host.assert_not_called()
        print_.assert_not_called()

    def test_non_tty_reports_skip_and_does_not_prompt(self):
        with (
            patch.object(cockpit, "ensure_config"),
            patch.object(cockpit.shutil, "get_terminal_size", return_value=type("Size", (), {"columns": 100})()),
            patch.object(cockpit, "load_hosts", return_value=["dev"]),
            patch.object(cockpit.sessions, "ensure_ssh_agent") as ensure_agent,
            patch.object(cockpit.sessions, "probe_hosts") as probe_hosts,
            patch.object(cockpit.sessions, "prepare_host") as prepare_host,
            patch.object(cockpit, "ensure_cockpit"),
            patch.object(cockpit, "_attach", return_value=0),
            patch("builtins.print") as print_,
            patch.object(cockpit.sys.stdin, "isatty", return_value=False),
            patch.object(cockpit.sys.stdout, "isatty", return_value=True),
        ):
            self.assertEqual(cockpit.cockpit(), 0)

        ensure_agent.assert_called_once_with()
        probe_hosts.assert_not_called()
        prepare_host.assert_not_called()
        self.assertIn("not attached to a TTY", "\n".join(str(call.args[0]) for call in print_.call_args_list))

    def test_stdout_redirect_does_not_skip_interactive_ssh_checks(self):
        with (
            patch.object(cockpit.sessions, "ensure_ssh_agent", return_value="/tmp/agent"),
            patch.object(cockpit.sessions, "group_hosts", return_value=[["dev"]]),
            patch.object(cockpit.sessions, "probe_hosts") as probe_hosts,
            patch.object(cockpit.sessions, "prepare_host", return_value=True) as prepare_host,
            patch("builtins.print"),
            patch.object(cockpit.sys.stdin, "isatty", return_value=True),
            patch.object(cockpit.sys.stdout, "isatty", return_value=False),
        ):
            cockpit._prepare_remote_hosts(["dev"])

        probe_hosts.assert_not_called()
        prepare_host.assert_called_once_with("dev")

    def test_ctrl_c_prevents_cockpit_creation(self):
        with (
            patch.object(cockpit, "ensure_config"),
            patch.object(cockpit.shutil, "get_terminal_size", return_value=type("Size", (), {"columns": 100})()),
            patch.object(cockpit, "load_hosts", return_value=["dev"]),
            patch.object(cockpit.sessions, "ensure_ssh_agent", return_value="/tmp/agent"),
            patch.object(cockpit.sessions, "group_hosts", return_value=[["dev"]]),
            patch.object(cockpit.sessions, "prepare_host", side_effect=KeyboardInterrupt),
            patch.object(cockpit.sessions, "probe_hosts") as probe_hosts,
            patch.object(cockpit, "ensure_cockpit") as ensure_cockpit,
            patch.object(cockpit, "_attach", return_value=0),
            patch("builtins.print"),
            patch.object(cockpit.sys.stdin, "isatty", return_value=True),
            patch.object(cockpit.sys.stdout, "isatty", return_value=True),
        ):
            self.assertEqual(cockpit.cockpit(), 130)

        ensure_cockpit.assert_not_called()
        probe_hosts.assert_not_called()


class CockpitLayoutTest(unittest.TestCase):
    def test_attach_detaches_existing_cockpit_client(self):
        with (
            patch.object(cockpit.sys.stdin, "isatty", return_value=True),
            patch.object(cockpit.sys.stdout, "isatty", return_value=True),
            patch.object(cockpit.os, "ttyname", return_value="/dev/pts/2"),
            patch.object(cockpit.shutil, "which", return_value=None),
            patch.object(cockpit.os, "execvp", side_effect=RuntimeError) as execvp,
            self.assertRaises(RuntimeError),
        ):
            cockpit._attach()

        execvp.assert_called_once_with(
            "tmux", ["tmux", "-L", "letee", "attach-session", "-d", "-t", "letee:cockpit"]
        )

    def test_named_server_attach_uses_prefixed_socket_and_keeps_target(self):
        cockpit.tmux.set_server("work")
        self.addCleanup(cockpit.tmux.set_server, None)
        with (
            patch.object(cockpit.sys.stdin, "isatty", return_value=True),
            patch.object(cockpit.sys.stdout, "isatty", return_value=True),
            patch.object(cockpit.os, "ttyname", return_value="/dev/pts/2"),
            patch.object(cockpit.shutil, "which", return_value=None),
            patch.object(cockpit.os, "execvp", side_effect=RuntimeError) as execvp,
            self.assertRaises(RuntimeError),
        ):
            cockpit._attach()

        execvp.assert_called_once_with(
            "tmux", ["tmux", "-L", "letee-work", "attach-session", "-d", "-t", "letee:cockpit"]
        )

    def test_fix_layout_pins_sidebar_to_configured_width(self):
        calls = []

        with patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)):
            cockpit._fix_layout("%1", 52)

        self.assertEqual(
            calls,
            [
                ("set-window-option", "-t", cockpit.TARGET, cockpit.SIDEBAR_WIDTH_OPTION, "52"),
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
                unittest.mock.call("set-option", "-t", "letee", "prefix", "C-x"),
                unittest.mock.call("set-option", "-t", "letee", "status", "off"),
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
                unittest.mock.call("set-option", "-t", "letee", "prefix", "C-x"),
                unittest.mock.call("set-option", "-t", "letee", "status", "off"),
                unittest.mock.call("set-option", "-s", "escape-time", "0"),
            ],
        )

    def test_layout_hook_repins_sidebar_after_window_resize(self):
        calls = []

        with patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)):
            cockpit._install_layout_hooks("%1", 52)

        self.assertEqual(
            calls,
            [
                ("set-hook", "-u", "-t", "letee", "client-attached"),
                ("set-hook", "-u", "-t", "letee", "client-resized"),
                ("set-hook", "-w", "-t", cockpit.TARGET, "window-resized", "resize-pane -t %1 -x 52"),
            ],
        )

    def test_repair_layout_skips_healthy_layout(self):
        with (
            patch.object(cockpit.tmux, "out", return_value="52:52:100:100:") as tmux_out,
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.repair_layout("%1")

        tmux_out.assert_called_once_with(
            "display-message", "-p", "-t", "%1",
            "#{pane_width}:#{@letee_sidebar_width}:#{window_width}:#{client_width}:#{window_offset_x}",
            check=False,
        )
        tmux_call.assert_not_called()

    def test_repair_layout_matches_window_to_client_and_repins_sidebar(self):
        with (
            patch.object(cockpit.tmux, "out", return_value="10:52:160:100:60"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.repair_layout("%1")

        tmux_call.assert_called_once_with(
            "run-shell", "-C", "-t", "%1",
            "resize-window -a -t letee:cockpit ; resize-pane -t %1 -x '#{@letee_sidebar_width}'",
            check=False,
        )

    def test_enable_mouse_sets_runtime_option_without_live_border_dragging(self):
        with patch.object(cockpit.tmux, "tmux") as tmux_call:
            cockpit._enable_mouse()

        self.assertEqual(
            tmux_call.call_args_list,
            [
                unittest.mock.call("set-option", "-t", "letee", "mouse", "on"),
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

    def test_bindings_replace_outer_prefix_table_with_letee_shortcuts(self):
        calls = []

        with patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)):
            cockpit._install_bindings("C-x", "%1", "%2")

        self.assertEqual(
            calls,
            [
                ("unbind-key", "-a", "-T", "prefix"),
                ("bind-key", "C-x", "send-prefix"),
                ("bind-key", "d", "detach-client"),
                ("bind-key", "h", "resize-pane", "-Z", "-t", "%2"),
                ("bind-key", "q", "kill-session", "-t", "letee"),
                ("bind-key", "a", "run-shell", f"{cockpit.FOCUS_SIDEBAR} agents"),
                ("bind-key", "s", "run-shell", f"{cockpit.FOCUS_SIDEBAR} sessions"),
                ("bind-key", "+", "run-shell", f"{cockpit.FOCUS_SIDEBAR} add"),
                ("bind-key", "r", "run-shell", f"{cockpit.FOCUS_SIDEBAR} remove"),
                ("bind-key", "x", "run-shell", f"{cockpit.FOCUS_SIDEBAR} kill"),
                ("bind-key", "!", "run-shell", f"{cockpit.FOCUS_SIDEBAR} alert"),
                ("bind-key", "w", "select-pane", "-t", "%2"),
                ("bind-key", "?", "respawn-pane", "-k", "-t", "%2", cockpit.help_command("C-x")),
                *[
                    ("bind-key", str(slot), "run-shell", f"{cockpit.shlex.quote(cockpit.sys.executable)} -m letee switch-session {slot}")
                    for slot in range(1, 10)
                ],
            ],
        )

    def test_named_bindings_propagate_server_to_generated_commands(self):
        cockpit.tmux.set_server("work")
        self.addCleanup(cockpit.tmux.set_server, None)
        calls = []

        with patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)):
            cockpit._install_bindings("C-x", "%1", "%2")

        self.assertEqual(calls[5], ("bind-key", "a", "run-shell", f"{cockpit.shlex.quote(cockpit.sys.executable)} -m letee -L work focus-sidebar agents"))
        self.assertEqual(calls[13], ("bind-key", "1", "run-shell", f"{cockpit.shlex.quote(cockpit.sys.executable)} -m letee -L work switch-session 1"))

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

    def test_focus_sidebar_add_opens_session_menu(self):
        with (
            patch.object(cockpit, "ensure_config"),
            patch.object(cockpit, "ensure_cockpit"),
            patch.object(cockpit, "_option", return_value="%7"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.focus_sidebar("add")

        self.assertEqual(
            tmux_call.call_args_list,
            [
                unittest.mock.call("select-pane", "-t", "%7"),
                unittest.mock.call("send-keys", "-t", "%7", "F11"),
            ],
        )

    def test_focus_sidebar_injects_distinct_action_keys_after_recreation(self):
        for region, injected_key, focus_key, selects_sidebar in (
            ("remove", "F8", "F6", True),
            ("kill", "F9", "F6", True),
            ("alert", "F10", "F7", False),
        ):
            with self.subTest(region=region), patch.object(
                cockpit, "ensure_config"
            ), patch.object(cockpit, "ensure_cockpit") as ensure_cockpit, patch.object(
                cockpit, "_option", return_value="%7"
            ), patch.object(cockpit.tmux, "tmux") as tmux_call:
                cockpit.focus_sidebar(region)

            ensure_cockpit.assert_called_once_with()
            expected = [
                unittest.mock.call("send-keys", "-t", "%7", focus_key, injected_key),
            ]
            if selects_sidebar:
                expected.insert(0, unittest.mock.call("select-pane", "-t", "%7"))
            self.assertEqual(tmux_call.call_args_list, expected)

    def test_right_pane_reset_shows_unavailable_message_and_preserves_target(self):
        calls = []

        with patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)):
            cockpit._install_right_pane_reset("%1", "%2")

        command = calls[1][4]
        self.assertEqual(calls[0], ("set-option", "-p", "-t", "%2", "remain-on-exit", "on"))
        self.assertEqual(calls[1][:4], ("set-hook", "-t", "letee", "pane-died"))
        self.assertIn("Active session is unavailable.", command)
        self.assertNotIn("set-option -u -t letee @letee_current_target", command)
        self.assertIn("select-pane -t %1", command)

    def test_session_menu_targets_sidebar_at_click_coordinates(self):
        with (
            patch.object(cockpit, "_option", return_value="%1"),
            patch.object(cockpit.tmux, "out", return_value="tmux 3.4"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.show_session_menu(Target("ssh", "work", "dev"), 7, 4)

        tmux_call.assert_called_once_with(
            "display-menu", "-O", "-T", "work@dev", "-x", "7", "-y", "4", "-t", "%1",
            "Rename", "e", "send-keys -t %1 e",
            "Remove", "r", "send-keys -t %1 r",
            "Kill", "x", "send-keys -t %1 x y",
            timeout=None,
        )

    def test_session_menu_enables_mouse_on_tmux_35_and_newer(self):
        with (
            patch.object(cockpit, "_option", return_value="%1"),
            patch.object(cockpit.tmux, "out", return_value="tmux 3.6"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.show_session_menu(Target("local", "work"), 7, 4)

        tmux_call.assert_called_once_with(
            "display-menu", "-M", "-O", "-T", "work@localhost", "-x", "7", "-y", "4", "-t", "%1",
            "Rename", "e", "send-keys -t %1 e",
            "Remove", "r", "send-keys -t %1 r",
            "Kill", "x", "send-keys -t %1 x y",
            timeout=None,
        )

    def test_agent_menu_targets_sidebar_at_click_coordinates(self):
        pane_target = PaneTarget(Target("ssh", "work", "dev"), "@2", "%8", "/tmp/tmux")
        with (
            patch.object(cockpit, "_option", return_value="%1"),
            patch.object(cockpit.tmux, "out", return_value="tmux 3.4"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.show_agent_menu("pi", pane_target, 7, 4)

        tmux_call.assert_called_once_with(
            "display-menu", "-O", "-T", "pi@work", "-x", "7", "-y", "4", "-t", "%1",
            "Kill", "x", "send-keys -t %1 x y",
            timeout=None,
        )

    def test_agent_menu_enables_mouse_on_tmux_35_and_newer(self):
        pane_target = PaneTarget(Target("local", "work"), "@2", "%8", "/tmp/tmux")
        with (
            patch.object(cockpit, "_option", return_value="%1"),
            patch.object(cockpit.tmux, "out", return_value="tmux 3.6"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.show_agent_menu("pi", pane_target, 7, 4)

        tmux_call.assert_called_once_with(
            "display-menu", "-M", "-O", "-T", "pi@work", "-x", "7", "-y", "4", "-t", "%1",
            "Kill", "x", "send-keys -t %1 x y",
            timeout=None,
        )

    def test_rename_target_updates_active_and_bell_markers_without_switching(self):
        old = Target("local", "old")
        new = Target("local", "new")
        with (
            patch.object(cockpit, "current_target", return_value=old),
            patch.object(cockpit, "bell_target", return_value=old),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.rename_target(old, new)

        self.assertEqual(
            tmux_call.call_args_list,
            [
                unittest.mock.call("set-option", "-t", "letee", cockpit.CURRENT_TARGET_OPTION, "local:new"),
                unittest.mock.call("set-option", "-t", "letee", cockpit.BELL_TARGET_OPTION, "local:new"),
            ],
        )

    def test_help_uses_configured_prefix(self):
        command = cockpit.help_command("C-x")

        self.assertIn("C-x a  focus/open Agents", command)
        self.assertIn("C-x s  focus/open Sessions", command)
        self.assertIn("C-x +  add session", command)
        self.assertIn("C-x r  remove active session", command)
        self.assertIn("C-x x  kill and remove active session", command)
        self.assertIn("C-x !  jump to first alerted agent", command)
        self.assertIn("C-x w  focus right pane", command)
        self.assertIn("C-x h  hide/show sidebar", command)
        self.assertIn("C-x q  quit cockpit", command)
        self.assertIn("C-x 1-9  switch session", command)
        self.assertIn("C-x ?  open help", command)
        self.assertIn("K/J    move session up/down", command)
        self.assertIn("Agent actions", command)
        self.assertIn("x      terminate selected agent with SIGTERM", command)
        self.assertIn("Left/Right  cycle ordering on selected ordering row", command)
        self.assertIn("Enter  activate selected row", command)
        self.assertIn("e      rename selected session", command)
        self.assertNotIn("a      open Add session menu", command)
        self.assertNotIn("?      open help from sidebar", command)
        self.assertNotIn("q      quit sidebar only", command)
        self.assertNotIn("/      search untracked existing sessions", command)
        self.assertNotIn("/work  find available sessions matching work", command)
        self.assertNotIn("n      open grouped local/SSH Add picker", command)
        self.assertIn("r      remove selected session", command)
        self.assertIn("x      kill and remove selected session", command)
        self.assertIn("Right-click  open session Rename/Remove/Kill menu", command)
        self.assertIn("Right-click  open agent Kill menu", command)
        self.assertNotIn("f      star/unstar", command)
        self.assertNotIn("r      refresh", command)
        self.assertIn("C-x d  detach cockpit", command)
        self.assertIn("C-x a  restart/focus Agents", command)
        self.assertIn("C-x +  open Add session menu", command)
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
        self.assertIn((("set-option", "-t", "letee", "prefix", "C-x"), {}), calls)
        self.assertIn((("set-option", "-t", "letee", "mouse", "on"), {}), calls)
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
            patch.object(cockpit, "_option", side_effect=lambda name: "1" if name == "@letee_cockpit" else "%2"),
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
                ("set-option", "-t", "letee", "@letee_current_target", "local:work"),
                ("set-option", "-u", "-t", "letee", "@letee_current_agent"),
                ("set-option", "-u", "-t", "letee", "@letee_bell_target"),
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

        self.assertIn(("set-option", "-t", "letee", "@letee_current_agent", "agent-1"), calls)
        with patch.object(cockpit, "_option", side_effect=["agent-1", ""]):
            self.assertEqual(cockpit.current_agent(), "agent-1")
            self.assertIsNone(cockpit.current_agent())

    def test_switch_rejects_missing_cockpit(self):
        with patch.object(cockpit, "right_pane", return_value=None):
            with self.assertRaisesRegex(SystemExit, "No valid letee"):
                cockpit.switch(cockpit.Target("local", "work"), "attach work")

    def test_show_reconnecting_displays_remote_session_details_and_unicode_spinner(self):
        with (
            patch.dict(cockpit.os.environ, {}, clear=True),
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.show_reconnecting(cockpit.Target("ssh", "work", "dev"))

        command = tmux_call.call_args.args
        self.assertEqual(command[:4], ("respawn-pane", "-k", "-t", "%2"))
        self.assertIn("\n    \x1b[38;5;81m╭─ Connection interrupted ─╮\n    ╰──────────────────────────╯", command[4])
        self.assertIn("    \x1b[2mSession\x1b[0m  work", command[4])
        self.assertIn("    \x1b[2mHost\x1b[0m     dev", command[4])
        self.assertIn("\x1b7", command[4])
        self.assertIn("\x1b8\x1b[2K    ", command[4])
        self.assertIn("⠋", command[4])
        self.assertEqual(command[4].count("Reconnecting"), 1)
        self.assertIn("Reconnecting%s", command[4])
        self.assertIn("38;5;%sm", command[4])
        self.assertIn("·  ", command[4])
        self.assertNotIn("Trying again automatically", command[4])
        self.assertIn("while :", command[4])
        self.assertNotIn("letee", command[4])
        self.assertNotIn("ssh:dev:work", command[4])
        self.assertNotIn("tail -f", command[4])

    def test_reconnecting_uses_ascii_spinner_when_requested(self):
        with patch.dict(cockpit.os.environ, {"LETEE_ASCII": "1"}):
            command = cockpit._reconnecting_command(cockpit.Target("ssh", "work", "dev"))

        self.assertIn("+-- Connection interrupted --+", command)
        self.assertIn("45:|:.  ", command)
        self.assertNotIn("⠋", command)
        self.assertNotIn("╭", command)

    def test_show_missing_guides_session_recreation(self):
        with (
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.show_missing(cockpit.Target("ssh", "work", "dev"))

        command = tmux_call.call_args.args
        self.assertEqual(command[:4], ("respawn-pane", "-k", "-t", "%2"))
        self.assertIn("Session ssh:dev:work is missing.", command[4])
        self.assertIn("Press Enter", command[4])
        self.assertIn("recreate", command[4])

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

    def test_current_target_recovers_from_right_pane_command(self):
        with (
            patch.object(cockpit, "_option", return_value=""),
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "out", return_value="ssh -t dev 'tmux -T clipboard new-session -A -s work'"),
        ):
            self.assertEqual(cockpit.current_target(), cockpit.Target("ssh", "work", "dev"))

    def test_current_target_recovers_from_option_rich_ssh_command(self):
        command = "ssh -o ControlMaster=auto -o ControlPersist=10m -o 'ControlPath=~/.ssh/letee-%C' -t dev 'tmux -T clipboard new-session -A -s work'"
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

    def test_status_snapshot_reads_all_runtime_state_with_one_tmux_call(self):
        with patch.object(cockpit.tmux, "out", return_value="%1\t%1\tlocal:work\tssh:dev:bell\tagent-1\t.") as out:
            status = cockpit.status_snapshot()

        self.assertEqual(
            status,
            cockpit.StatusSnapshot(
                Target("local", "work"),
                Target("ssh", "bell", "dev"),
                "agent-1",
                True,
            ),
        )
        self.assertEqual(out.call_count, 1)
        self.assertEqual(
            out.call_args.args[:5],
            (
                "display-message",
                "-p",
                "-t",
                cockpit.TARGET,
                "#{pane_id}\t#{@letee_sidebar_pane}\t#{@letee_current_target}\t#{@letee_bell_target}\t#{@letee_current_agent}\t.",
            ),
        )

    def test_status_snapshot_accepts_empty_options(self):
        with patch.object(cockpit.tmux, "out", return_value="%1\t\t\t\t\t."):
            self.assertEqual(
                cockpit.status_snapshot(),
                cockpit.StatusSnapshot(None, None, None, True),
            )

    def test_status_snapshot_marks_sidebar_inactive_when_another_pane_is_active(self):
        with patch.object(cockpit.tmux, "out", return_value="%2\t%1\t\t\t\t."):
            self.assertEqual(
                cockpit.status_snapshot(),
                cockpit.StatusSnapshot(None, None, None, False),
            )

    def test_status_snapshot_ignores_invalid_targets_and_malformed_output(self):
        with patch.object(cockpit.tmux, "out", return_value="%1\t%1\tbad\tssh:nope\tagent-1\t."):
            status = cockpit.status_snapshot()
        self.assertEqual(status, cockpit.StatusSnapshot(None, None, "agent-1", True))

        with patch.object(cockpit.tmux, "out", return_value="malformed"):
            self.assertIsNone(cockpit.status_snapshot())

    def test_install_bell_hook_enables_outer_tmux_bells(self):
        calls = []

        with patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)):
            cockpit._install_bell_hook()

        self.assertEqual(
            calls,
            [
                ("set-window-option", "-t", "letee:cockpit", "monitor-bell", "on"),
                ("set-option", "-t", "letee", "bell-action", "any"),
                (
                    "set-hook",
                    "-t",
                    "letee",
                    "alert-bell",
                    "set-option -F -t letee @letee_bell_target '#{@letee_current_target}'",
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
