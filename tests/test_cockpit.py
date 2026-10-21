import json
import os
import shlex
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from letee import cockpit, diagnostics
from letee.names import PaneTarget, Target


class SidebarCollapseTest(unittest.TestCase):
    def setUp(self):
        self.options = {cockpit.SIDEBAR_PANE_OPTION: "%1", cockpit.RIGHT_PANE_OPTION: "%2"}
        self.state = patch.object(cockpit, "_option", side_effect=lambda name: self.options.get(name, ""))
        self.state.start()
        self.addCleanup(self.state.stop)

    def test_compact_collapse_and_expand_restore_configured_width(self):
        with (
            patch.object(cockpit, "_valid", return_value=True),
            patch.object(cockpit, "load_sidebar_collapse_mode", return_value="compact"),
            patch.object(cockpit, "load_sidebar_width", return_value=52),
            patch.object(cockpit.tmux, "out", return_value="0"),
            patch.object(cockpit.tmux, "tmux") as command,
        ):
            self.assertEqual(cockpit.toggle_sidebar(), 0)
            command.assert_any_call("resize-pane", "-t", "%1", "-x", "5", check=False)
            command.assert_any_call("select-pane", "-t", "%2")
            self.options[cockpit.SIDEBAR_COMPACT_OPTION] = "1"
            self.assertTrue(cockpit.sidebar_compact())
            command.reset_mock()
            cockpit.expand_sidebar()
            command.assert_any_call("resize-pane", "-t", "%1", "-x", "52", check=False)
            self.assertFalse(any(c.args[0] == "select-pane" for c in command.call_args_list))

    def test_toggle_compact_expands_and_focuses_right(self):
        self.options[cockpit.SIDEBAR_COMPACT_OPTION] = "1"
        with (
            patch.object(cockpit, "_valid", return_value=True),
            patch.object(cockpit.tmux, "out", return_value="0"),
            patch.object(cockpit, "expand_sidebar") as expand,
        ):
            cockpit.toggle_sidebar()
        expand.assert_called_once_with(focus_right=True)

    def test_hidden_collapse_uses_right_pane_zoom(self):
        with (
            patch.object(cockpit, "_valid", return_value=True),
            patch.object(cockpit, "load_sidebar_collapse_mode", return_value="hidden"),
            patch.object(cockpit.tmux, "out", return_value="0"),
            patch.object(cockpit.tmux, "tmux") as command,
        ):
            cockpit.toggle_sidebar()
        command.assert_any_call("resize-pane", "-Z", "-t", "%2")
        self.assertFalse(any("-x" in c.args for c in command.call_args_list))

    def test_expand_hidden_unzooms_and_can_focus_right(self):
        with (
            patch.object(cockpit.tmux, "out", return_value="1"),
            patch.object(cockpit.tmux, "tmux") as command,
        ):
            cockpit.expand_sidebar(focus_right=True)
        self.assertEqual(command.call_args_list, [
            call("resize-pane", "-Z", "-t", "%2"),
            call("select-pane", "-t", "%2"),
        ])

    def test_ensure_preserves_compact_width_and_hooks(self):
        self.options[cockpit.SIDEBAR_COMPACT_OPTION] = "1"
        with (
            patch.object(cockpit, "_valid", return_value=True),
            patch.object(cockpit, "load_prefix", return_value="C-s"),
            patch.object(cockpit, "load_sidebar_width", return_value=52),
            patch.object(cockpit, "_configure_cockpit") as configure,
        ):
            cockpit.ensure_cockpit()
        configure.assert_called_once_with("%1", "%2", "C-s", 5)

    def test_toggle_hidden_expands_and_focuses_right(self):
        with (
            patch.object(cockpit, "_valid", return_value=True),
            patch.object(cockpit.tmux, "out", return_value="1"),
            patch.object(cockpit.tmux, "tmux") as command,
        ):
            cockpit.toggle_sidebar()
        self.assertEqual(command.call_args_list, [
            call("resize-pane", "-Z", "-t", "%2"),
            call("select-pane", "-t", "%2"),
        ])

    def test_toggle_requires_existing_cockpit(self):
        with patch.object(cockpit, "_valid", return_value=False):
            with self.assertRaisesRegex(SystemExit, "No valid letee"):
                cockpit.toggle_sidebar()

    def test_background_actions_do_not_expand_compact_sidebar(self):
        with (
            patch.object(cockpit, "ensure_config"),
            patch.object(cockpit, "_valid", return_value=True),
            patch.object(cockpit, "expand_sidebar") as expand,
            patch.object(cockpit.tmux, "tmux"),
        ):
            cockpit.focus_sidebar("next_agent")
        expand.assert_not_called()


class CockpitStartupTest(unittest.TestCase):
    def setUp(self):
        valid = patch.object(cockpit, "_valid", return_value=False)
        valid.start()
        self.addCleanup(valid.stop)

    def test_valid_cockpit_reattaches_without_ssh_preparation_or_sidebar_restart(self):
        events = []
        with (
            patch.object(cockpit, "ensure_config") as ensure_config,
            patch.object(cockpit.shutil, "get_terminal_size", return_value=os.terminal_size((100, 24))),
            patch.object(cockpit, "_valid", return_value=True) as valid,
            patch.object(cockpit, "load_hosts") as load_hosts,
            patch.object(cockpit, "_prepare_remote_hosts") as prepare_hosts,
            patch.object(cockpit, "ensure_cockpit", side_effect=lambda: events.append("configure")) as ensure_cockpit,
            patch.object(cockpit, "_attach", side_effect=lambda: events.append("attach") or 7) as attach,
        ):
            self.assertEqual(cockpit.cockpit(), 7)

        ensure_config.assert_called_once_with()
        valid.assert_called_once_with()
        attach.assert_called_once_with()
        load_hosts.assert_not_called()
        prepare_hosts.assert_not_called()
        ensure_cockpit.assert_called_once_with()
        self.assertEqual(events, ["configure", "attach"])

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
            patch.object(cockpit, "ensure_cockpit", side_effect=lambda **kwargs: events.append(("cockpit",))),
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

    def test_invalid_cockpit_requests_sidebar_restart_on_startup(self):
        with (
            patch.object(cockpit, "ensure_config"),
            patch.object(cockpit.shutil, "get_terminal_size", return_value=type("Size", (), {"columns": 100})()),
            patch.object(cockpit, "load_hosts", return_value=[]),
            patch.object(cockpit, "ensure_cockpit") as ensure_cockpit,
            patch.object(cockpit, "_attach", return_value=0),
        ):
            self.assertEqual(cockpit.cockpit(), 0)

        ensure_cockpit.assert_called_once_with(restart_sidebar=True)

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
            patch.object(cockpit, "ensure_cockpit", side_effect=lambda **kwargs: order.append("cockpit")),
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
            patch.object(cockpit.os, "execvp", side_effect=RuntimeError) as execvp,
            self.assertRaises(RuntimeError),
        ):
            cockpit._attach()

        executable = cockpit.tmux.tmux_executable()
        execvp.assert_called_once_with(
            executable, [executable, "-L", "letee@v1", "attach-session", "-d", "-t", "letee:cockpit"]
        )

    def test_named_server_attach_uses_prefixed_socket_and_keeps_target(self):
        cockpit.tmux.set_server("work")
        self.addCleanup(cockpit.tmux.set_server, None)
        with (
            patch.object(cockpit.sys.stdin, "isatty", return_value=True),
            patch.object(cockpit.sys.stdout, "isatty", return_value=True),
            patch.object(cockpit.os, "ttyname", return_value="/dev/pts/2"),
            patch.object(cockpit.os, "execvp", side_effect=RuntimeError) as execvp,
            self.assertRaises(RuntimeError),
        ):
            cockpit._attach()

        executable = cockpit.tmux.tmux_executable()
        execvp.assert_called_once_with(
            executable, [executable, "-L", "letee@v1-work", "attach-session", "-d", "-t", "letee:cockpit"]
        )

    def test_attach_reports_dev_tty_without_executing_tmux(self):
        with (
            patch.object(cockpit.sys.stdin, "isatty", return_value=True),
            patch.object(cockpit.sys.stdout, "isatty", return_value=True),
            patch.object(cockpit.os, "ttyname", return_value="/dev/tty"),
            patch("builtins.print") as print_,
            patch.object(cockpit.os, "execvp") as execvp,
        ):
            self.assertEqual(cockpit._attach(), 0)

        execvp.assert_not_called()
        self.assertIn("Current fd is /dev/tty; tmux refuses it", print_.call_args.args[0])

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
        install_right_pane_reset.assert_called_once_with("%1", "%2", "C-x")
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
        install_right_pane_reset.assert_called_once_with("%1", "%1", "C-x")
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

    def test_existing_cockpit_restarts_sidebar_when_requested(self):
        with (
            patch.object(cockpit, "_valid", return_value=True),
            patch.object(
                cockpit,
                "_option",
                side_effect=lambda name: {
                    cockpit.SIDEBAR_PANE_OPTION: "%1",
                    cockpit.RIGHT_PANE_OPTION: "%2",
                }.get(name, ""),
            ),
            patch.object(cockpit, "load_prefix", return_value="C-x"),
            patch.object(cockpit, "load_sidebar_width", return_value=52),
            patch.object(cockpit, "_configure_cockpit"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.ensure_cockpit(restart_sidebar=True)

        tmux_call.assert_called_once_with(
            "respawn-pane", "-k", "-t", "%1", cockpit._sidebar_command()
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

    def test_resize_hook_passes_numeric_width_in_both_sidebar_modes(self):
        for width in (52, cockpit.COMPACT_SIDEBAR_WIDTH):
            with self.subTest(width=width), patch.object(cockpit.tmux, "tmux") as command:
                cockpit._install_layout_hooks("%1", width)
                hook = shlex.split(command.call_args.args[-1])
                self.assertEqual(hook[:3], ["resize-pane", "-t", "%1"])
                # resize-pane parses -x as a number, not a tmux format.
                self.assertEqual(int(hook[hook.index("-x") + 1]), width)

    def test_repair_layout_skips_healthy_layout(self):
        with (
            patch.object(cockpit, "_ensure_client_size", return_value=True),
            patch.object(cockpit.tmux, "out", return_value="52:52:100:100:100:100:") as tmux_out,
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.repair_layout("%1")

        tmux_out.assert_called_once_with(
            "display-message", "-p", "-t", "%1",
            "#{pane_width}:#{@letee_sidebar_width}:#{window_width}:#{client_width}:#{window_height}:#{client_height}:#{window_offset_x}",
            check=False,
        )
        tmux_call.assert_not_called()

    def test_repair_layout_repairs_stale_window_height(self):
        with (
            patch.object(cockpit, "_ensure_client_size", return_value=True),
            patch.object(cockpit.tmux, "out", return_value="40:40:377:377:92:60:0") as tmux_out,
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.repair_layout("%1")

        tmux_out.assert_called_once_with(
            "display-message", "-p", "-t", "%1",
            "#{pane_width}:#{@letee_sidebar_width}:#{window_width}:#{client_width}:#{window_height}:#{client_height}:#{window_offset_x}",
            check=False,
        )
        tmux_call.assert_called_once_with(
            "run-shell", "-C", "-t", "%1",
            "resize-window -a -t letee:cockpit ; resize-pane -t %1 -x '#{@letee_sidebar_width}'",
            check=False,
        )

    def test_repair_layout_matches_window_to_client_and_repins_sidebar(self):
        with (
            patch.object(cockpit, "_ensure_client_size", return_value=True),
            patch.object(cockpit.tmux, "out", return_value="10:52:160:100:100:100:60"),
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

        with patch.object(cockpit, "_option", return_value=""), patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)):
            cockpit._install_bindings("C-x", "%1", "%2")

        self.assertEqual(
            calls,
            [
                ("unbind-key", "-a", "-T", "prefix"),
                (
                    "bind-key", "-n", "MouseDown1Pane",
                    "if-shell -F -t = '#{==:#{pane_id},%1}' "
                    "{ send-keys -M -t = ; select-pane -t = } "
                    "{ select-pane -t = ; send-keys -M }",
                ),
                ("bind-key", "C-x", "send-prefix"),
                ("bind-key", "d", "detach-client"),
                ("bind-key", "h", "run-shell", cockpit._letee_command("toggle-sidebar")),
                ("bind-key", "q", "kill-session", "-t", "letee"),
                ("bind-key", "a", "run-shell", f"{cockpit.FOCUS_SIDEBAR} agents"),
                ("bind-key", "s", "run-shell", f"{cockpit.FOCUS_SIDEBAR} sessions"),
                ("bind-key", "+", "run-shell", f"{cockpit.FOCUS_SIDEBAR} add"),
                ("bind-key", "r", "run-shell", f"{cockpit.FOCUS_SIDEBAR} remove"),
                ("bind-key", "x", "run-shell", f"{cockpit.FOCUS_SIDEBAR} kill"),
                ("bind-key", "j", "run-shell", f"{cockpit.FOCUS_SIDEBAR} alert"),
                ("bind-key", "Left", "run-shell", f"{cockpit.FOCUS_SIDEBAR} previous_agent"),
                ("bind-key", "Right", "run-shell", f"{cockpit.FOCUS_SIDEBAR} next_agent"),
                ("bind-key", "Up", "run-shell", f"{cockpit.FOCUS_SIDEBAR} previous_session"),
                ("bind-key", "Down", "run-shell", f"{cockpit.FOCUS_SIDEBAR} next_session"),
                ("bind-key", "w", "select-pane", "-t", "%2"),
                ("bind-key", "?", "respawn-pane", "-k", "-t", "%2", cockpit.help_command("C-x")),
                *[
                    ("bind-key", str(slot), "run-shell", f"{cockpit.shlex.quote(cockpit.sys.executable)} -m letee switch-session {slot}")
                    for slot in range(1, 10)
                ],
            ],
        )

    def test_mouse_binding_uses_sidebar_id_and_preserves_branch_order(self):
        calls = []

        with patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)):
            cockpit._install_bindings("C-x", "%sidebar", "%right")

        binding = next(call for call in calls if call[:3] == ("bind-key", "-n", "MouseDown1Pane"))
        self.assertEqual(
            binding[3],
            "if-shell -F -t = '#{==:#{pane_id},%sidebar}' "
            "{ send-keys -M -t = ; select-pane -t = } "
            "{ select-pane -t = ; send-keys -M }",
        )

    def test_named_bindings_propagate_server_to_generated_commands(self):
        cockpit.tmux.set_server("work")
        self.addCleanup(cockpit.tmux.set_server, None)
        calls = []

        with patch.object(cockpit, "_option", return_value=""), patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)):
            cockpit._install_bindings("C-x", "%1", "%2")

        self.assertEqual(calls[6], ("bind-key", "a", "run-shell", f"{cockpit.shlex.quote(cockpit.sys.executable)} -m letee -L work focus-sidebar agents"))
        self.assertIn(("bind-key", "h", "run-shell", f"{cockpit.shlex.quote(cockpit.sys.executable)} -m letee -L work toggle-sidebar"), calls)
        self.assertEqual(calls[18], ("bind-key", "1", "run-shell", f"{cockpit.shlex.quote(cockpit.sys.executable)} -m letee -L work switch-session 1"))
        for key, action in (("Left", "previous_agent"), ("Right", "next_agent"), ("Up", "previous_session"), ("Down", "next_session")):
            self.assertIn(("bind-key", key, "run-shell", f"{cockpit.shlex.quote(cockpit.sys.executable)} -m letee -L work focus-sidebar {action}"), calls)

    def test_focus_sidebar_recreates_selects_and_injects_region_key(self):
        with (
            patch.object(cockpit, "ensure_config"),
            patch.object(cockpit, "ensure_cockpit"),
            patch.object(cockpit, "_option", return_value="%7"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            with patch.object(cockpit, "expand_sidebar") as expand:
                cockpit.focus_sidebar("agents")
            expand.assert_called_once_with()

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
            with patch.object(cockpit, "expand_sidebar") as expand:
                cockpit.focus_sidebar("add")
            expand.assert_called_once_with()

        self.assertEqual(
            tmux_call.call_args_list,
            [
                unittest.mock.call("select-pane", "-t", "%7"),
                unittest.mock.call("send-keys", "-t", "%7", "F11"),
            ],
        )

    def test_focus_sidebar_injects_distinct_action_keys_after_recreation(self):
        for region, injected_key in (("remove", "F8"), ("kill", "F9")):
            with self.subTest(region=region), patch.object(
                cockpit, "ensure_config"
            ), patch.object(cockpit, "ensure_cockpit") as ensure_cockpit, patch.object(
                cockpit, "_option", return_value="%7"
            ), patch.object(cockpit.tmux, "tmux") as tmux_call:
                with patch.object(cockpit, "expand_sidebar") as expand:
                    cockpit.focus_sidebar(region)
                expand.assert_called_once_with()

            ensure_cockpit.assert_called_once_with()
            self.assertEqual(
                tmux_call.call_args_list,
                [
                    unittest.mock.call("select-pane", "-t", "%7"),
                    unittest.mock.call("send-keys", "-t", "%7", "F6", injected_key),
                ],
            )

    def test_internal_action_keys_fit_screen_terminal_function_key_range(self):
        # screen terminfo defines F1 through F12, but not F13.
        for action, key in cockpit.SIDEBAR_ACTION_KEYS.items():
            with self.subTest(action=action):
                self.assertLessEqual(int(key.removeprefix("F")), 12)

    def test_agent_cycle_keeps_healthy_hidden_sidebar_and_recovers_invalid_cockpit(self):
        for region, key in (("previous_agent", "F12"), ("next_agent", "F5")):
            for healthy in (True, False):
                with (
                    self.subTest(region=region, healthy=healthy),
                    patch.object(cockpit, "ensure_config"),
                    patch.object(cockpit, "_valid", return_value=healthy),
                    patch.object(cockpit, "ensure_cockpit") as ensure_cockpit,
                    patch.object(cockpit, "_option", return_value="%7"),
                    patch.object(cockpit.tmux, "tmux") as tmux_call,
                ):
                    self.assertEqual(cockpit.focus_sidebar(region), 0)
                if healthy:
                    ensure_cockpit.assert_not_called()
                else:
                    ensure_cockpit.assert_called_once_with()
                tmux_call.assert_called_once_with("send-keys", "-t", "%7", "F7", key)

    def test_session_cycle_keeps_healthy_hidden_sidebar_and_recovers_invalid_cockpit(self):
        for region, key in (("previous_session", "F3"), ("next_session", "F4")):
            for healthy in (True, False):
                with (
                    self.subTest(region=region, healthy=healthy),
                    patch.object(cockpit, "ensure_config"),
                    patch.object(cockpit, "_valid", return_value=healthy),
                    patch.object(cockpit, "ensure_cockpit") as ensure_cockpit,
                    patch.object(cockpit, "_option", return_value="%7"),
                    patch.object(cockpit.tmux, "tmux") as tmux_call,
                ):
                    self.assertEqual(cockpit.focus_sidebar(region), 0)
                if healthy:
                    ensure_cockpit.assert_not_called()
                else:
                    ensure_cockpit.assert_called_once_with()
                tmux_call.assert_called_once_with("send-keys", "-t", "%7", "F6", key)

    def test_focus_sidebar_alert_keeps_valid_hidden_cockpit(self):
        with (
            patch.object(cockpit, "ensure_config"),
            patch.object(cockpit, "_valid", return_value=True) as valid,
            patch.object(cockpit, "ensure_cockpit") as ensure_cockpit,
            patch.object(cockpit, "_option", return_value="%7"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.focus_sidebar("alert")

        valid.assert_called_once_with()
        ensure_cockpit.assert_not_called()
        tmux_call.assert_called_once_with("send-keys", "-t", "%7", "F7", "F10")

    def test_focus_sidebar_alert_recreates_invalid_cockpit(self):
        with (
            patch.object(cockpit, "ensure_config"),
            patch.object(cockpit, "_valid", return_value=False) as valid,
            patch.object(cockpit, "ensure_cockpit") as ensure_cockpit,
            patch.object(cockpit, "_option", return_value="%7"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.focus_sidebar("alert")

        valid.assert_called_once_with()
        ensure_cockpit.assert_called_once_with()
        tmux_call.assert_called_once_with("send-keys", "-t", "%7", "F7", "F10")

    def test_pane_death_diagnostics_capture_identity_before_native_recovery(self):
        with (
            patch.object(cockpit.diagnostics, "get_diagnostics") as logger,
            patch.object(cockpit.tmux, "tmux") as call,
        ):
            logger.return_value.enabled = True
            logger.return_value.path = Path("/tmp/log with ' quote.jsonl")
            cockpit._install_right_pane_reset("%1", "%2", "C-x")
        hook = call.call_args.args[-1]
        self.assertIn("run-shell -b", hook)
        self.assertIn("attachment-exited", hook)
        for field in ("pane_pid", "pane_dead_status", "pane_dead_signal", "@letee_attachment_id"):
            self.assertIn(f"#{{q:{field}}}", hook)
        self.assertIn("LETEE_DEBUG_LOG=", hook)
        self.assertIn("--parent-run-id", hook)
        self.assertLess(hook.index("run-shell -b"), hook.index("respawn-pane"))

    def test_pane_death_hook_has_no_callback_when_logging_disabled(self):
        with (
            patch.dict(os.environ, {"LETEE_DEBUG_LOG": ""}),
            patch.object(cockpit.tmux, "tmux") as call,
        ):
            cockpit._install_right_pane_reset("%1", "%2", "C-x")
        hook = call.call_args.args[-1]
        self.assertNotIn("run-shell", hook)
        self.assertIn("respawn-pane", hook)

    def test_attach_flushes_diagnostics_before_exec(self):
        events = []
        with (
            patch.object(cockpit.sys.stdin, "isatty", return_value=True),
            patch.object(cockpit.sys.stdout, "isatty", return_value=True),
            patch.object(cockpit.os, "ttyname", return_value="/dev/pts/1"),
            patch.object(cockpit.tmux, "tmux_executable", return_value="tmux"),
            patch.object(cockpit.diagnostics, "flush", side_effect=lambda: events.append("flush")),
            patch.object(cockpit.os, "execvp", side_effect=lambda *args: events.append("exec")),
        ):
            cockpit._attach()
        self.assertEqual(events, ["flush", "exec"])

    def test_right_pane_reset_uses_expected_death_marker(self):
        calls = []

        with (
            patch.dict(cockpit.os.environ, {"LETEE_ASCII": "0"}),
            patch.object(cockpit.locale, "getpreferredencoding", return_value="UTF-8"),
            patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)),
        ):
            cockpit._install_right_pane_reset("%1", "%2", "C-x")

        command = calls[1][4]
        self.assertEqual(calls[0], ("set-option", "-p", "-t", "%2", "remain-on-exit", "on"))
        self.assertEqual(calls[1][:4], ("set-hook", "-t", "letee", "pane-died"))
        expected_marker = "set-option -u -t letee @letee_expected_right_pane_death"
        current_target = "set-option -u -t letee @letee_current_target"
        help_command = shlex.quote(cockpit.help_command("C-x"))
        consumed = "set-option -t letee @letee_expected_right_pane_death consumed"
        self.assertIn("#{==:#{@letee_expected_right_pane_death},1}", command)
        self.assertIn(consumed, command)
        self.assertIn("#{==:#{@letee_expected_right_pane_death},succeeded}", command)
        # The success branch clears the marker and target only after the kill reports success.
        self.assertLess(command.index(expected_marker), command.index(current_target))
        self.assertLess(command.index(current_target), command.index(help_command))
        self.assertIn(help_command, command)
        # A markerless death uses the unavailable fallback, even without a target marker.
        self.assertIn("Active session is unavailable.", command)
        self.assertIn("select-pane -t %1", command)

    def test_right_pane_death_uses_process_generation_after_switch(self):
        calls = []

        with patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)):
            cockpit._install_right_pane_reset("%1", "%2", "C-x")

        command = calls[1][4]
        target_match = "#{==:#{@letee_expected_right_pane_death_target},#{@letee_current_target}}"
        generation_match = "#{==:#{@letee_expected_right_pane_death_generation},#{pane_pid}}"
        fallback = cockpit._right_pane_death_action("%1", "%2", "", succeeded=False)
        stale_ack = (
            f"if-shell -F '{generation_match}' {{ {cockpit._clear_expected_right_pane_death()} }} "
            f"{{ {fallback} }}"
        )
        self.assertIn(target_match, command)
        self.assertIn(generation_match, command)
        self.assertIn(stale_ack, command)

    def test_set_expected_right_pane_death_sets_or_unsets_session_options(self):
        target = Target("local", "work")

        with (
            patch.object(cockpit, "_option", return_value="%2"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.set_expected_right_pane_death(target)
            cockpit.set_expected_right_pane_death(None)

        self.assertEqual(
            tmux_call.call_args_list,
            [
                call(
                    "if-shell",
                    "-F",
                    "#{==:#{@letee_current_target},local:work}",
                    "set-option -t letee @letee_expected_right_pane_death_target local:work ; "
                    "set-option -F -t %2 @letee_expected_right_pane_death_generation '#{pane_pid}' ; "
                    "set-option -t letee @letee_expected_right_pane_death 1",
                ),
                call("set-option", "-u", "-t", "letee", cockpit.EXPECTED_RIGHT_PANE_DEATH_OPTION),
                call("set-option", "-u", "-t", "letee", cockpit.EXPECTED_RIGHT_PANE_DEATH_TARGET_OPTION),
                call("set-option", "-u", "-t", "letee", cockpit.EXPECTED_RIGHT_PANE_DEATH_GENERATION_OPTION),
            ],
        )

    def test_resolve_failed_expected_death_restores_target_when_hook_consumed_marker(self):
        target = Target("ssh", "work", "dev")

        with (
            patch.object(cockpit, "_option", side_effect=["%1", "%2"]),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.resolve_expected_right_pane_death(target, False)

        condition = "1"
        self.assertEqual(tmux_call.call_args.args[:3], ("if-shell", "-F", condition))
        command = tmux_call.call_args.args[3]
        self.assertIn("#{==:#{@letee_expected_right_pane_death_target},ssh:dev:work}", command)
        self.assertIn("#{==:#{@letee_current_target},ssh:dev:work}", command)
        self.assertIn("set-option -t letee @letee_current_target ssh:dev:work", command)
        self.assertIn("Session ssh:dev:work is unavailable.", command)
        self.assertIn("set-option -u -t letee @letee_expected_right_pane_death", command)
        self.assertIn("set-option -u -t letee @letee_expected_right_pane_death_target", command)
        self.assertIn(
            "if-shell -F '#{==:#{@letee_expected_right_pane_death_target},ssh:dev:work}' { "
            "set-option -u -t letee @letee_expected_right_pane_death ; "
            "set-option -u -t letee @letee_expected_right_pane_death_target ; "
            "set-option -u -t letee @letee_expected_right_pane_death_generation }",
            command,
        )

    def test_resolve_success_restores_help_when_pane_is_already_unavailable(self):
        target = Target("local", "work")

        with (
            patch.object(cockpit, "_option", side_effect=["%1", "%2"]),
            patch.object(cockpit, "load_prefix", return_value="C-x"),
            patch.object(cockpit, "help_command", return_value="help"),
            patch.object(cockpit.tmux, "out", return_value="sh") as tmux_out,
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.resolve_expected_right_pane_death(target, True)

        tmux_out.assert_called_once_with(
            "display-message", "-p", "-t", "%2", "#{pane_current_command}", check=False
        )
        self.assertEqual(tmux_call.call_args.args[:3], ("if-shell", "-F", "1"))
        action = tmux_call.call_args.args[3]
        self.assertIn("#{==:#{@letee_expected_right_pane_death_target},local:work}", action)
        self.assertIn("#{==:#{@letee_current_target},local:work}", action)
        self.assertIn("set-option -u -t letee @letee_expected_right_pane_death", action)
        self.assertIn("set-option -u -t letee @letee_expected_right_pane_death_target", action)
        self.assertIn("set-option -u -t letee @letee_current_target", action)
        self.assertIn("respawn-pane -k -t %2 help", action)

    def test_reset_to_help_reuses_successful_pane_death_cleanup_for_target(self):
        target = Target("local", "work")
        with (
            patch.object(cockpit, "_option", side_effect=["%1", "%2", target.format()]),
            patch.object(cockpit, "load_prefix", return_value="C-x"),
            patch.object(cockpit, "_right_pane_death_action", return_value="reset action") as death_action,
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            self.assertTrue(cockpit.reset_to_help(target))

        death_action.assert_called_once_with("%1", "%2", "C-x", succeeded=True)
        tmux_call.assert_called_once_with(
            "if-shell", "-F", "#{==:#{@letee_current_target},local:work}", "reset action"
        )

    def test_set_current_target_sets_session_option(self):
        target = Target("ssh", "work", "dev")

        with patch.object(cockpit.tmux, "tmux") as tmux_call:
            cockpit.set_current_target(target)

        tmux_call.assert_called_once_with(
            "set-option", "-t", cockpit.tmux.SESSION, cockpit.CURRENT_TARGET_OPTION, "ssh:dev:work"
        )

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
            "display-menu", "-O", "-T", "pi@work@dev", "-x", "7", "-y", "4", "-t", "%1",
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
            "display-menu", "-M", "-O", "-T", "pi@work@localhost", "-x", "7", "-y", "4", "-t", "%1",
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
        with (
            patch.dict(cockpit.os.environ, {"LETEE_ASCII": "0"}),
            patch.object(cockpit.locale, "getpreferredencoding", return_value="UTF-8"),
        ):
            command = cockpit.help_command("C-x")

        self.assertIn("C-x a  focus/open Agents", command)
        self.assertIn("C-x s  focus/open Sessions", command)
        self.assertIn("C-x +  add session", command)
        self.assertIn("C-x r  remove active session", command)
        self.assertIn("C-x x  kill and remove active session", command)
        self.assertIn("C-x j  jump to first agent needing attention", command)
        self.assertIn("C-x Left  switch to previous agent", command)
        self.assertIn("C-x Right  switch to next agent", command)
        self.assertIn("C-x Up  switch to previous session", command)
        self.assertIn("C-x Down  switch to next session", command)
        self.assertIn("C-x w  focus right pane", command)
        self.assertIn("C-x h  collapse/expand sidebar", command)
        self.assertIn("C-x q  quit cockpit", command)
        self.assertIn("C-x 1-9  switch session", command)
        self.assertIn("C-x ?  open help", command)
        self.assertIn("K/J    move session up/down", command)
        self.assertIn("Agent actions", command)
        self.assertIn("x      terminate selected agent with SIGTERM", command)
        self.assertIn("Left/Right  cycle ordering on selected ordering row", command)
        self.assertIn("↕      start mouse move; hover destination, then click", command)
        self.assertIn("Esc    cancel mouse move", command)
        self.assertIn("Priority / Session", command)
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

    def test_help_uses_ascii_move_handle_when_requested(self):
        with patch.dict(cockpit.os.environ, {"LETEE_ASCII": "1"}):
            command = cockpit.help_command("C-x")

        self.assertIn(":      start mouse move; hover destination, then click", command)
        self.assertNotIn("↕      start mouse move; hover destination, then click", command)

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
            patch.object(cockpit, "_install_bindings") as install_bindings,
            patch.object(cockpit, "_enable_mouse") as enable_mouse,
            patch.object(cockpit, "_enable_clipboard") as enable_clipboard,
            patch.object(cockpit, "_enable_truecolor"),
        ):
            cockpit.ensure_cockpit()

        enable_mouse.assert_called_once_with()
        enable_clipboard.assert_called_once_with()
        install_bindings.assert_called_once_with("C-x", "%1", "%2")

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
                (
                    "if-shell",
                    "-F",
                    "1",
                    "set-option -t letee @letee_current_target local:work",
                ),
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

    def test_deferred_switch_shows_spinner_before_preparing_and_attaches(self):
        target = Target("ssh", "work", "dev")
        events = []

        def prepare():
            events.append(("prepare",))
            return "attach work"

        def tmux_call(*args, **kwargs):
            if args[0] == "respawn-pane":
                events.append(("respawn", args[4]))
            elif args[0] == "select-pane":
                events.append(("focus", args[2]))

        with (
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "tmux", side_effect=tmux_call),
        ):
            cockpit.switch(target, prepare)

        self.assertEqual([event[0] for event in events], ["respawn", "prepare", "respawn", "focus"])
        self.assertIn("Switching session", events[0][1])
        self.assertIn("Preparing%s", events[0][1])
        self.assertEqual(events[2], ("respawn", "attach work"))

    def test_remote_overlay_timeout_replaces_preparing_with_reconnecting(self):
        target = Target("ssh", "work", "dev")
        timeout = subprocess.TimeoutExpired(["ssh"], 3)
        with (
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
            patch.object(cockpit.sessions, "load_persistent_ssh", return_value=True),
            patch.object(cockpit.sessions.subprocess, "run", side_effect=timeout) as run,
            self.assertRaisesRegex(SystemExit, r"^overlay ssh:dev:work timed out$") as raised,
        ):
            cockpit.switch(target, lambda: cockpit.sessions.attach_command(target, overlay=True))

        self.assertIs(raised.exception.__cause__, timeout)
        run.assert_called_once()
        respawns = [item.args for item in tmux_call.call_args_list if item.args[0] == "respawn-pane"]
        self.assertEqual(len(respawns), 2)
        self.assertIn("Preparing%s", respawns[0][4])
        self.assertIn("Reconnecting%s", respawns[1][4])
        self.assertNotIn("unavailable", respawns[1][4])
        self.assertFalse(any(item.args[0] == "select-pane" for item in tmux_call.call_args_list))

    def test_direct_preparation_timeout_reconnects_only_remote_targets(self):
        for target in (Target("ssh", "work", "dev"), Target("local", "work")):
            with self.subTest(target=target):
                timeout = subprocess.TimeoutExpired(["prepare"], 3)
                with (
                    patch.object(cockpit, "right_pane", return_value="%2"),
                    patch.object(cockpit.tmux, "tmux") as tmux_call,
                    self.assertRaises(subprocess.TimeoutExpired) as raised,
                ):
                    cockpit.switch(target, unittest.mock.Mock(side_effect=timeout))

                self.assertIs(raised.exception, timeout)
                command = tmux_call.call_args.args[4]
                if target.kind == "ssh":
                    self.assertIn("Reconnecting%s", command)
                else:
                    self.assertIn("Session local:work is unavailable.", command)
                self.assertFalse(any(item.args[0] == "select-pane" for item in tmux_call.call_args_list))

    def test_failed_deferred_switch_replaces_spinner_with_unavailable_message(self):
        target = Target("ssh", "work", "dev")
        events = []

        def prepare():
            events.append(("prepare",))
            raise RuntimeError("ssh setup failed")

        def tmux_call(*args, **kwargs):
            if args[0] == "respawn-pane":
                events.append(("respawn", args[4]))

        with (
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "tmux", side_effect=tmux_call),
            self.assertRaisesRegex(RuntimeError, "ssh setup failed"),
        ):
            cockpit.switch(target, prepare)

        self.assertEqual([event[0] for event in events], ["respawn", "prepare", "respawn"])
        self.assertIn("Switching session", events[0][1])
        self.assertIn("Session ssh:dev:work is unavailable.", events[2][1])

    def test_switch_rejects_missing_cockpit_without_preparing(self):
        prepare = unittest.mock.Mock(return_value="attach work")
        with patch.object(cockpit, "right_pane", return_value=None):
            with self.assertRaisesRegex(SystemExit, "No valid letee"):
                cockpit.switch(cockpit.Target("local", "work"), prepare)

        prepare.assert_not_called()

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

    def test_show_missing_displays_waiting_state_with_session_details(self):
        with (
            patch.dict(cockpit.os.environ, {}, clear=True),
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.show_missing(cockpit.Target("ssh", "work", "dev"))

        command = tmux_call.call_args.args
        self.assertEqual(command[:4], ("respawn-pane", "-k", "-t", "%2"))
        self.assertIn("Session missing", command[4])
        self.assertIn("    \x1b[2mSession\x1b[0m  work", command[4])
        self.assertIn("    \x1b[2mHost\x1b[0m     dev", command[4])
        self.assertIn("Waiting for session%s", command[4])
        self.assertIn("⠋", command[4])
        self.assertIn("while :", command[4])
        self.assertNotIn("Press Enter", command[4])
        self.assertNotIn("recreate", command[4])

    def test_show_missing_uses_ascii_waiting_state_when_requested(self):
        with (
            patch.dict(cockpit.os.environ, {"LETEE_ASCII": "1"}),
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.show_missing(cockpit.Target("ssh", "work", "dev"))

        command = tmux_call.call_args.args[4]
        self.assertIn("+-- Session missing --+", command)
        self.assertIn("45:|:.  ", command)
        self.assertIn("Waiting for session%s", command)
        self.assertNotIn("⠋", command)
        self.assertNotIn("╭", command)
        self.assertNotIn("Press Enter", command)
        self.assertNotIn("recreate", command)

    def test_missing_animation_uses_ascii_for_non_utf_encoding(self):
        with (
            patch.dict(cockpit.os.environ, {}, clear=True),
            patch.object(cockpit.locale, "getpreferredencoding", return_value="ANSI_X3.4-1968"),
        ):
            command = cockpit._missing_command(cockpit.Target("ssh", "work", "dev"))

        self.assertIn("+-- Session missing --+", command)
        self.assertIn("45:|:.  ", command)
        self.assertNotIn("⠋", command)
        self.assertNotIn("╭", command)

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
            patch.object(cockpit.tmux, "out", return_value="ssh -t dev 'tmux -L letee.inner -T clipboard new-session -A -s work'"),
        ):
            self.assertEqual(cockpit.current_target(), cockpit.Target("ssh", "work", "dev"))

    def test_current_target_recovers_from_option_rich_ssh_command(self):
        command = "ssh -o ControlMaster=auto -o ControlPersist=10m -o 'ControlPath=~/.ssh/letee-%C' -t dev 'tmux -L letee.inner -T clipboard new-session -A -s work'"
        with (
            patch.object(cockpit, "_option", return_value=""),
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "out", return_value=command),
        ):
            self.assertEqual(cockpit.current_target(), cockpit.Target("ssh", "work", "dev"))

    def test_current_target_recovers_from_local_inner_attach_command(self):
        command = "env -u TMUX tmux -L letee.inner -T clipboard new-session -A -s work"
        with (
            patch.object(cockpit, "_option", return_value=""),
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "out", return_value=command),
        ):
            self.assertEqual(cockpit.current_target(), cockpit.Target("local", "work"))

    def test_current_target_recovers_from_remote_inner_attach_command(self):
        command = "ssh -t dev 'tmux -L letee.inner -T clipboard new-session -A -s work'"
        with (
            patch.object(cockpit, "_option", return_value=""),
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "out", return_value=command),
        ):
            self.assertEqual(cockpit.current_target(), cockpit.Target("ssh", "work", "dev"))

    def test_current_target_recovers_from_inner_pane_attach_commands(self):
        socket_path = "/tmp/tmux-1000/letee.inner"
        commands = (
            f"env -u TMUX tmux -S {socket_path} select-window -t work:@3 \\; select-pane -t %7 \\; attach-session -t work",
            f"ssh -t dev 'tmux -S {socket_path} select-window -t work:@3 \\; select-pane -t %7 \\; attach-session -t work'",
        )
        for command in commands:
            with self.subTest(command=command), patch.object(cockpit, "_option", return_value=""), patch.object(cockpit, "right_pane", return_value="%2"), patch.object(cockpit.tmux, "out", return_value=command):
                expected = cockpit.Target("ssh", "work", "dev") if command.startswith("ssh ") else cockpit.Target("local", "work")
                self.assertEqual(cockpit.current_target(), expected)

    def test_current_target_clears_stale_marker_for_legacy_pane_attach_commands(self):
        socket_path = "/tmp/tmux-1000/default"
        cases = (
            ("local:work", f"env -u TMUX tmux -S {socket_path} select-window -t work:@3 \\; select-pane -t %7 \\; attach-session -t work"),
            ("ssh:dev:work", f"ssh -t dev 'tmux -S {socket_path} select-window -t work:@3 \\; select-pane -t %7 \\; attach-session -t work'"),
        )
        for marker, command in cases:
            with self.subTest(marker=marker, command=command):
                with (
                    patch.object(cockpit, "_option", return_value=marker),
                    patch.object(cockpit, "right_pane", return_value="%2"),
                    patch.object(cockpit.tmux, "out", return_value=command),
                    patch.object(cockpit.tmux, "tmux") as tmux_call,
                ):
                    self.assertIsNone(cockpit.current_target())

                tmux_call.assert_called_once_with(
                    "set-option", "-u", "-t", cockpit.tmux.SESSION, cockpit.CURRENT_TARGET_OPTION
                )

    def test_current_target_ignores_legacy_server_attach_commands(self):
        commands = (
            "tmux new-session -A -s work",
            "tmux -L other new-session -A -s work",
            "ssh -t dev 'tmux new-session -A -s work'",
            "ssh -t dev 'tmux -L other new-session -A -s work'",
        )
        for command in commands:
            with self.subTest(command=command), patch.object(cockpit, "_option", return_value=""), patch.object(cockpit, "right_pane", return_value="%2"), patch.object(cockpit.tmux, "out", return_value=command):
                self.assertIsNone(cockpit.current_target())

    def test_current_target_clears_stale_marker_for_legacy_session_collision(self):
        cases = (
            ("local:work", "tmux new-session -A -s work"),
            ("ssh:dev:work", "ssh -t dev 'tmux new-session -A -s work'"),
        )
        for marker, command in cases:
            with self.subTest(marker=marker, command=command):
                with (
                    patch.object(cockpit, "_option", return_value=marker),
                    patch.object(cockpit, "right_pane", return_value="%2"),
                    patch.object(cockpit.tmux, "out", return_value=command),
                    patch.object(cockpit.tmux, "tmux") as tmux_call,
                ):
                    self.assertIsNone(cockpit.current_target())

                tmux_call.assert_called_once_with(
                    "set-option", "-u", "-t", cockpit.tmux.SESSION, cockpit.CURRENT_TARGET_OPTION
                )

    def test_current_target_clears_stale_marker_for_mismatched_inner_attach(self):
        cases = (
            ("local:work", "env -u TMUX tmux -L letee.inner -T clipboard new-session -A -s other"),
            ("ssh:dev:work", "ssh -t dev 'tmux -L letee.inner -T clipboard new-session -A -s other'"),
        )
        for marker, command in cases:
            with self.subTest(marker=marker, command=command):
                with (
                    patch.object(cockpit, "_option", return_value=marker),
                    patch.object(cockpit, "right_pane", return_value="%2"),
                    patch.object(cockpit.tmux, "out", return_value=command),
                    patch.object(cockpit.tmux, "tmux") as tmux_call,
                ):
                    self.assertIsNone(cockpit.current_target())

                tmux_call.assert_called_once_with(
                    "set-option", "-u", "-t", cockpit.tmux.SESSION, cockpit.CURRENT_TARGET_OPTION
                )

    def test_current_target_handles_repeated_invalid_options(self):
        command = "tmux " + " ".join(["-!"] * 100)
        with (
            patch.object(cockpit, "_option", return_value=""),
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "out", return_value=command),
        ):
            self.assertIsNone(cockpit.current_target())

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


class CockpitDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tempdir.name) / "cockpit.jsonl"
        self.env = patch.dict(os.environ, {"LETEE_DEBUG_LOG": str(self.log_path)}, clear=True)
        self.env.start()
        # Open the async writer before tests replace os.open/os.close for TTY failures.
        diagnostics.log("test_started")
        diagnostics.flush()
        self.out = patch.object(cockpit.tmux, "out", return_value="")
        self.out.start()
        self.addCleanup(self.out.stop)

    def tearDown(self):
        diagnostics.close()
        self.env.stop()
        self.tempdir.cleanup()

    def records(self):
        diagnostics.flush()
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def assert_handled(self, operation, error_type, handling="fallback"):
        records = self.records()
        matches = [r for r in records if r["event"] == "error" and r.get("operation") == operation]
        self.assertTrue(matches, operation)
        self.assertEqual(matches[-1]["handling"], handling)
        self.assertEqual(matches[-1]["error_type"], error_type)
        self.assertNotIn("PRIVATE_FAILURE", self.log_path.read_text())

    def test_authentication_group_probe_and_retry_have_distinct_origins(self):
        def prepare(host):
            diagnostics.log("auth_test", host=host)
            return True

        def probe(hosts):
            for host in hosts:
                diagnostics.log("auth_test", host=host)
            return [False] * len(hosts)

        with (
            patch.object(cockpit.sys.stdin, "isatty", return_value=True),
            patch.object(cockpit.sessions, "ensure_ssh_agent", return_value=True),
            patch.object(cockpit.sessions, "group_hosts", return_value=[["dev", "peer"]]),
            patch.object(cockpit.sessions, "prepare_host", side_effect=prepare),
            patch.object(cockpit.sessions, "probe_hosts", side_effect=probe),
            patch("builtins.print"),
        ):
            cockpit._prepare_remote_hosts(["dev", "peer"])
        records = self.records()
        attempts = [r for r in records if r["event"] == "auth_test"]
        self.assertEqual([r["host"] for r in attempts], ["dev", "peer", "peer"])
        self.assertEqual([r["origin"] for r in attempts], ["startup_interactive", "startup_batch", "startup_retry"])
        self.assertTrue(all(r["active"] is False for r in attempts))
        self.assertTrue(any(r["event"] == "ssh_preparation_host" and r["outcome"] == "retrying" for r in records))
        result = [r for r in records if r["event"] == "ssh_preparation"][-1]
        self.assertEqual((result["outcome"], result["ready_count"], result["failed_count"]), ("completed", 2, 0))

    def test_help_and_menu_keybinding_failures_use_defaults_privately(self):
        with (
            patch.object(cockpit, "load_keybindings", side_effect=SystemExit("PRIVATE_FAILURE")),
            patch.object(cockpit, "load_sidebar_keybindings", return_value=cockpit.DEFAULT_SIDEBAR_KEYBINDINGS),
        ):
            command = cockpit.help_command("C-b")
        self.assertIn(cockpit._display_key("C-b", cockpit.DEFAULT_KEYBINDINGS["focus_agents"]), command)
        self.assert_handled("help_keybindings", "SystemExit")
        target = Target("local", "work")
        for operation, show, args in (
            ("session_menu_keybindings", cockpit.show_session_menu, (target, 1, 2)),
            ("agent_menu_keybindings", cockpit.show_agent_menu, ("agent", PaneTarget(target, "@1", "%3", "/tmp/letee.inner"), 1, 2)),
        ):
            with (
                self.subTest(operation=operation),
                patch.object(cockpit, "load_sidebar_keybindings", side_effect=SystemExit("PRIVATE_FAILURE")),
                patch.object(cockpit, "_option", return_value="%1"),
                patch.object(cockpit.tmux, "tmux") as command,
            ):
                show(*args)
            self.assertIn(cockpit.DEFAULT_SIDEBAR_KEYBINDINGS["kill"], command.call_args.args)
            self.assert_handled(operation, "SystemExit")

    def test_root_binding_query_failure_still_installs_bindings(self):
        with (
            patch.object(cockpit, "help_command", return_value="help"),
            patch.object(cockpit, "_option", side_effect=OSError("PRIVATE_FAILURE")),
            patch.object(cockpit.tmux, "tmux") as command,
        ):
            cockpit._install_bindings("C-b", "%1", "%2", cockpit.DEFAULT_KEYBINDINGS)
        command.assert_any_call("unbind-key", "-a", "-T", "prefix")
        self.assert_handled("root_keybindings", "OSError")

    def test_layout_query_failure_allows_fallback(self):
        with patch.object(cockpit.tmux, "out", side_effect=OSError("PRIVATE_FAILURE")):
            self.assertIs(cockpit._ensure_client_size(), True)
        self.assert_handled("layout_client_query", "OSError")
        self.assertTrue(any(r["event"] == "layout_client_query_error" for r in self.records()))

    def test_layout_parse_and_tty_failures_are_ignored_privately(self):
        for stage, operation in (
            ("parse", "layout_client_parse"),
            ("open", "layout_tty_open"),
            ("size", "layout_tty_size"),
            ("close", "layout_tty_close"),
        ):
            raw = "PRIVATE_FAILURE:/dev/pts/3:80:24:letee" if stage == "parse" else "1234:/dev/pts/3:80:24:letee"
            with (
                self.subTest(stage=stage),
                patch.object(cockpit.tmux, "out", return_value=raw),
                patch.object(cockpit.os, "open", return_value=123, side_effect=OSError("PRIVATE_FAILURE") if stage == "open" else None),
                patch.object(cockpit.os, "get_terminal_size", return_value=os.terminal_size((80, 24)), side_effect=OSError("PRIVATE_FAILURE") if stage == "size" else None),
                patch.object(cockpit.os, "close", side_effect=OSError("PRIVATE_FAILURE") if stage == "close" else None) as close,
                patch.object(cockpit.os, "kill") as kill,
            ):
                self.assertIs(cockpit._ensure_client_size(), True)
            if stage in ("size", "close"):
                close.assert_called_once_with(123)
            kill.assert_not_called()
            self.assert_handled(operation, "ValueError" if stage == "parse" else "OSError", "ignored")

    def test_layout_signal_failures_abort_repair_privately(self):
        for error in (ProcessLookupError("PRIVATE_FAILURE"), PermissionError("PRIVATE_FAILURE"), OSError("PRIVATE_FAILURE")):
            with (
                self.subTest(error=type(error).__name__),
                patch.object(cockpit.tmux, "out", return_value="1234:/dev/pts/3:80:24:letee"),
                patch.object(cockpit.os, "open", return_value=123),
                patch.object(cockpit.os, "get_terminal_size", return_value=os.terminal_size((100, 24))),
                patch.object(cockpit.os, "close"),
                patch.object(cockpit.os, "kill", side_effect=error),
                patch.object(cockpit.tmux, "tmux") as command,
            ):
                cockpit.repair_layout("%1")
            command.assert_not_called()
            self.assert_handled("layout_client_signal", type(error).__name__)
        signals = [r for r in self.records() if r["event"] == "layout_client_signal"]
        self.assertEqual(len(signals), 3)
        self.assertTrue(all(r["status"] == "error" for r in signals))

    def test_attach_tty_failure_still_executes_attach(self):
        with (
            patch.object(cockpit.sys.stdin, "isatty", return_value=True),
            patch.object(cockpit.sys.stdout, "isatty", return_value=True),
            patch.object(cockpit.os, "ttyname", side_effect=OSError("PRIVATE_FAILURE")),
            patch.object(cockpit.os, "execvp") as execute,
        ):
            self.assertEqual(cockpit._attach(), 0)
        self.assertEqual(execute.call_args.args[1][-4:], ["attach-session", "-d", "-t", cockpit.TARGET])
        self.assert_handled("attach_tty", "OSError")

    def test_cancelled_preparation_does_not_build_or_attach(self):
        with (
            patch.object(cockpit, "ensure_config"),
            patch.object(cockpit.shutil, "get_terminal_size", return_value=os.terminal_size((100, 24))),
            patch.object(cockpit, "_valid", return_value=False),
            patch.object(cockpit, "load_hosts", return_value=["dev"]),
            patch.object(cockpit, "_prepare_remote_hosts", side_effect=KeyboardInterrupt("PRIVATE_FAILURE")),
            patch.object(cockpit, "ensure_cockpit") as ensure,
            patch.object(cockpit, "_attach") as attach,
            patch.object(cockpit.sys, "stderr"),
        ):
            self.assertEqual(cockpit.cockpit(), 130)
        ensure.assert_not_called()
        attach.assert_not_called()
        self.assert_handled("ssh_preparation", "KeyboardInterrupt", "cancelled")
        self.assertTrue(any(r["event"] == "operation_end" and r["name"] == "cockpit.launch" and r["outcome"] == "cancelled" for r in self.records()))

    def test_invalid_target_options_and_recovered_targets_are_audited(self):
        self.assertIsNone(cockpit._parse_target_option("PRIVATE_FAILURE"))
        self.assert_handled("target_option_parse", "SystemExit", "ignored")
        with (
            patch.object(cockpit, "_target_option", return_value=None),
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "out", return_value="ssh invalid! tmux -L letee.inner new-session -A -s work"),
        ):
            self.assertIsNone(cockpit.current_target())
        self.assert_handled("current_target_parse", "SystemExit", "ignored")
        self.assertNotIn("invalid!", self.log_path.read_text())

    def test_right_pane_lookup_failure_preserves_exception_and_stage(self):
        error = SystemExit("PRIVATE_FAILURE")
        with (
            patch.object(cockpit, "right_pane", side_effect=error),
            patch.object(cockpit.tmux, "tmux") as command,
            self.assertRaises(SystemExit) as raised,
        ):
            cockpit.switch(Target("local", "work"), "PRIVATE_FAILURE command")
        self.assertIs(raised.exception, error)
        command.assert_not_called()
        self.assert_handled("switch_right_pane_lookup", "SystemExit", "propagated")
        self.assertTrue(any(r["event"] == "switch_error" and r["stage"] == "right_pane_lookup" for r in self.records()))
        self.assertFalse(any(r["event"] == "switch_completed" for r in self.records()))

    def test_switch_recovery_failure_preserves_original_preparation_error(self):
        original = RuntimeError("PRIVATE_FAILURE preparation")
        recovery = OSError("PRIVATE_FAILURE recovery")
        with (
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "tmux") as command,
            self.assertRaises(RuntimeError) as raised,
        ):
            def execute(*args, **kwargs):
                if args[0] == "respawn-pane" and "unavailable" in args[-1]:
                    raise recovery
            command.side_effect = execute
            cockpit.switch(Target("local", "work"), lambda: (_ for _ in ()).throw(original))
        self.assertIs(raised.exception, original)
        self.assert_handled("switch_error_recovery", "OSError", "ignored")
        self.assert_handled("attach_command_preparation", "RuntimeError", "propagated")
        self.assertFalse(any(r["event"] == "switch_completed" for r in self.records()))

    def test_attachment_diagnostic_failures_are_audited_without_stopping_switch(self):
        def execute(*args, **kwargs):
            if args[0] == "if-shell" and cockpit.ATTACHMENT_ID_OPTION in args[-1]:
                raise SystemExit("PRIVATE_FAILURE identity")
            return subprocess.CompletedProcess(args, 0, stdout="letee-respawn-succeeded\n123\n")
        with (
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "out", side_effect=OSError("PRIVATE_FAILURE probe")),
            patch.object(cockpit.tmux, "tmux", side_effect=execute),
        ):
            cockpit.switch(Target("local", "work"), "PRIVATE_FAILURE shell")
        self.assert_handled("attachment_replacement_probe", "OSError", "ignored")
        self.assert_handled("attachment_identity", "SystemExit", "ignored")
        self.assertTrue(any(r["event"] == "switch_completed" for r in self.records()))

    def test_generation_probe_failure_is_audited_only_after_confirmed_respawn(self):
        for error in (
            subprocess.CalledProcessError(1, ["PRIVATE_FAILURE"], output=b"letee-respawn-succeeded\n", stderr="PRIVATE_FAILURE"),
            SystemExit("PRIVATE_FAILURE"),
            subprocess.CalledProcessError(1, ["PRIVATE_FAILURE"], output="PRIVATE_FAILURE"),
        ):
            with self.subTest(error=type(error).__name__), patch.object(cockpit.tmux, "tmux", side_effect=error):
                if isinstance(error, subprocess.CalledProcessError) and error.stdout == b"letee-respawn-succeeded\n":
                    self.assertEqual(cockpit._respawn_attachment("%2", "PRIVATE_FAILURE shell"), "")
                    self.assert_handled("attachment_generation", "CalledProcessError", "ignored")
                else:
                    before = len(self.records())
                    with self.assertRaises(type(error)) as raised:
                        cockpit._respawn_attachment("%2", "PRIVATE_FAILURE shell")
                    self.assertIs(raised.exception, error)
                    self.assertEqual(len(self.records()), before)
        self.assertNotIn("PRIVATE_FAILURE", self.log_path.read_text())

    def test_callback_shell_quoting_preserves_path_and_empty_arguments(self):
        path = self.log_path.parent / "log with ' #{literal} quote.jsonl"
        with (
            patch.dict(os.environ, {"LETEE_DEBUG_LOG": str(path)}),
            patch.object(cockpit.tmux, "tmux") as call,
        ):
            cockpit._install_right_pane_reset("%1", "%2", "C-x")
        tokens = shlex.split(call.call_args.args[-1])
        callback = tokens[tokens.index("run-shell") + 2].replace("##", "#")
        values = {
            "hook_pane": "%2", cockpit.ATTACHMENT_ID_OPTION: "old-id",
            "pane_pid": "123", cockpit.ATTACHMENT_TARGET_OPTION: "local:old",
            cockpit.ATTACHMENT_AGENT_OPTION: "", cockpit.ATTACHMENT_SWITCH_OPTION: "switch-1",
            "pane_dead_status": "", "pane_dead_signal": "TERM",
            cockpit.EXPECTED_RIGHT_PANE_DEATH_OPTION: "", cockpit.ATTACHMENT_RUN_OPTION: "old-run",
        }
        # Match tmux format_quote_shell without running a host tmux server.
        for field, value in values.items():
            quoted = "".join("\\" + char if char in "|&;<>()$`\\\"'*?[# =%" else char for char in value)
            callback = callback.replace(f"#{{q:{field}}}", quoted)
        args = shlex.split(callback)
        self.assertEqual(args[1], f"LETEE_DEBUG_LOG={path}")
        start = args.index("attachment-exited")
        self.assertEqual(args[start + 1:], [
            "--parent-run-id", "old-run", "--",
            "%2", "old-id", "123", "local:old", "", "switch-1", "", "TERM", "",
        ])

    def test_disabled_attachment_diagnostics_preserve_original_commands(self):
        with (
            patch.dict(os.environ, {"LETEE_DEBUG_LOG": ""}),
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "out") as query,
            patch.object(cockpit.tmux, "tmux") as command,
            patch.object(cockpit, "uuid") as identity,
        ):
            cockpit.switch(Target("local", "work"), "attach work")
        identity.uuid4.assert_not_called()
        query.assert_not_called()
        self.assertEqual(command.call_args_list, [
            call("if-shell", "-F", "1", "set-option -t letee @letee_current_target local:work"),
            call("set-option", "-u", "-t", "letee", cockpit.CURRENT_AGENT_OPTION),
            call("set-option", "-u", "-t", "letee", cockpit.BELL_TARGET_OPTION),
            call("respawn-pane", "-k", "-t", "%2", "attach work"),
            call("select-pane", "-t", "%2"),
        ])

    def test_diagnostic_identity_failure_does_not_prevent_focus(self):
        def execute(*args, **kwargs):
            if args[0] == "if-shell" and cockpit.ATTACHMENT_ID_OPTION in args[-1]:
                raise SystemExit("diagnostic option failed")
            return subprocess.CompletedProcess(args, 0, stdout="letee-respawn-succeeded\n123\n")
        with (
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "out", side_effect=OSError("probe failed")),
            patch.object(cockpit.tmux, "tmux", side_effect=execute) as command,
        ):
            cockpit.switch(Target("local", "work"), "attach work")
        command.assert_any_call("select-pane", "-t", "%2")
        self.assertTrue(any(r["event"] == "switch_completed" for r in self.records()))

    def test_failed_respawn_does_not_complete_replacement(self):
        def execute(*args, **kwargs):
            if args[0] == "respawn-pane":
                raise OSError("respawn failed")
        with (
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "out", return_value="old\t100\tlocal:old\t\t\t\t100"),
            patch.object(cockpit.tmux, "tmux", side_effect=execute),
            self.assertRaises(OSError),
        ):
            cockpit.switch(Target("local", "new"), "attach new")
        events = [r["event"] for r in self.records()]
        self.assertIn("attachment_replacement_requested", events)
        self.assertNotIn("attachment_replaced", events)
        self.assertNotIn("attachment_started", events)

    def test_native_exit_hook_guards_metadata_for_placeholders_and_stale_generations(self):
        with patch.object(cockpit.tmux, "tmux") as command:
            cockpit._install_right_pane_reset("%1", "%2", "C-x")
        hook = command.call_args.args[-1]
        self.assertIn(f"#{{&&:#{{pane_dead}},#{{==:#{{{cockpit.ATTACHMENT_GENERATION_OPTION}}},#{{pane_pid}}}}}}", hook)
        tokens = shlex.split(hook)
        callbacks = [tokens[i + 2] for i, token in enumerate(tokens) if token == "run-shell"]
        self.assertEqual(len(callbacks), 2)
        matching, unknown = callbacks
        for field in (cockpit.ATTACHMENT_ID_OPTION, cockpit.ATTACHMENT_TARGET_OPTION,
                      cockpit.ATTACHMENT_AGENT_OPTION, cockpit.ATTACHMENT_SWITCH_OPTION,
                      cockpit.ATTACHMENT_RUN_OPTION, cockpit.EXPECTED_RIGHT_PANE_DEATH_OPTION):
            self.assertIn(f"#{{q:{field}}}", matching)
            self.assertNotIn(f"#{{q:{field}}}", unknown)
        self.assertIn("#{?pane_dead,#{q:pane_pid},}", unknown)
        self.assertIn("#{q:pane_dead_status}", unknown)

    def test_replacement_probe_rejects_stale_identity(self):
        with patch.object(cockpit.tmux, "out", return_value="old\t200\tlocal:old\t\t\t\t100"):
            self.assertIsNone(cockpit._record_attachment_replacement("%2", reason="missing"))
        self.assertFalse(any(r["event"].startswith("attachment_replacement") for r in self.records()))

    def test_placeholder_switch_does_not_create_attachment(self):
        with (
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "tmux") as command,
            patch.object(cockpit, "uuid") as identity,
        ):
            cockpit.switch_reconnecting(Target("local", "work"))
        identity.uuid4.assert_not_called()
        self.assertFalse(any(r["event"] in ("attachment_started", "attachment_requested") for r in self.records()))
        self.assertFalse(any(cockpit.ATTACHMENT_ID_OPTION in str(c) for c in command.call_args_list))

    def test_placeholder_respawns_do_not_write_identity_and_complete_only_after_success(self):
        for show in (cockpit.show_missing, cockpit.show_reconnecting, cockpit.show_unavailable):
            with self.subTest(show=show.__name__):
                with (
                    patch.object(cockpit, "_require_right_pane", return_value="%2"),
                    patch.object(cockpit.tmux, "out", return_value="old\t100\tlocal:old\t\t\t\t100"),
                    patch.object(cockpit.tmux, "tmux", side_effect=OSError("respawn failed")),
                    self.assertRaises(OSError),
                ):
                    show(Target("local", "work"))
        self.assertFalse(any(r["event"] == "attachment_replaced" for r in self.records()))

    def test_native_respawn_queue_preserves_success_when_diagnostic_query_fails(self):
        failure = subprocess.CalledProcessError(1, "tmux", output="letee-respawn-succeeded\n")
        with (
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "tmux") as command,
        ):
            def execute(*args, **kwargs):
                if args[0] == "respawn-pane":
                    self.assertEqual(args[5:], (
                        ";", "display-message", "-p", cockpit._RESPAWN_SUCCEEDED, ";",
                        "display-message", "-p", "-t", "%2", "#{pane_pid}",
                    ))
                    raise failure
            command.side_effect = execute
            cockpit.switch(Target("local", "work"), "attach work")
        command.assert_any_call("select-pane", "-t", "%2")
        self.assertFalse(any(cockpit.ATTACHMENT_ID_OPTION in str(c) for c in command.call_args_list))

    def test_generation_query_timeout_preserves_confirmed_respawn(self):
        def timeout_after_respawn(*args, **kwargs):
            try:
                raise subprocess.TimeoutExpired("tmux", 1, output=b"letee-respawn-succeeded\n")
            except subprocess.TimeoutExpired:
                raise SystemExit("tmux timed out") from None
        with patch.object(cockpit.tmux, "tmux", side_effect=timeout_after_respawn):
            self.assertEqual(cockpit._respawn_attachment("%2", "attach work"), "")
        self.assert_handled("attachment_generation", "SystemExit", "ignored")

    def test_operations_record_failures_without_changing_exception(self):
        error = OSError("private detail")
        with (
            patch.object(cockpit, "load_prefix", side_effect=error),
            self.assertRaises(OSError) as raised,
        ):
            cockpit.ensure_cockpit()
        self.assertIs(raised.exception, error)
        ends = [r for r in self.records() if r["event"] == "operation_end"]
        self.assertTrue(any(r["name"] == "cockpit.ensure" and r["outcome"] == "failed" for r in ends))
        self.assertNotIn("private detail", self.log_path.read_text())

    def test_switch_records_each_stage_without_attach_command(self):
        target = Target("local", "work")
        with (
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "tmux"),
        ):
            cockpit.switch(target, "attach work")

        records = self.records()
        stages = [
            (record["event"], record.get("stage"), record.get("status"))
            for record in records
            if record["event"].startswith("switch_")
        ]
        self.assertEqual(
            stages,
            [
                ("switch_requested", None, None),
                ("switch_marker_update", "current_target_marker", "started"),
                ("switch_marker_update", "current_target_marker", "completed"),
                ("switch_respawn", "right_pane_respawn", "started"),
                ("switch_respawn", "right_pane_respawn", "completed"),
                ("switch_focus", "right_pane_focus", "started"),
                ("switch_focus", "right_pane_focus", "completed"),
                ("switch_completed", None, None),
            ],
        )
        self.assertTrue(all(record["target"] == target.format() for record in records if record["event"].startswith("switch_")))
        self.assertTrue(all(record["right_pane"] == "%2" for record in records if record["event"].startswith("switch_")))
        self.assertNotIn("attach work", self.log_path.read_text())

    def test_attachment_lifecycle_uses_unique_identity_and_omits_shell_command(self):
        target = Target("ssh", "work", "dev")
        with (
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "tmux", return_value=subprocess.CompletedProcess([], 0, stdout="letee-respawn-succeeded\n123\n")) as tmux_call,
        ):
            cockpit.switch(target, "secret shell command", "agent-1")
            cockpit.switch(target, "secret shell command", "agent-1")
        records = self.records()
        started = [record for record in records if record["event"] == "attachment_started"]
        self.assertEqual(len(started), 2)
        self.assertNotEqual(started[0]["attachment_id"], started[1]["attachment_id"])
        for record in started:
            self.assertEqual(record["target"], "ssh:dev:work")
            self.assertEqual(record["agent_id"], "agent-1")
            self.assertEqual(record["right_pane"], "%2")
            identity_writes = [c for c in tmux_call.call_args_list if c.args[0] == "if-shell" and cockpit.ATTACHMENT_ID_OPTION in c.args[-1]]
            self.assertTrue(any(record["attachment_id"] in c.args[-1] for c in identity_writes))
            for write in identity_writes:
                self.assertEqual(write.args[2:5], ("-t", "%2", "#{==:#{pane_pid},123}"))
                self.assertTrue(write.args[-1].startswith(f"set-option -p -t %2 {cockpit.ATTACHMENT_GENERATION_OPTION} '' ; "))
                self.assertTrue(write.args[-1].endswith(f"{cockpit.ATTACHMENT_GENERATION_OPTION} 123"))
        self.assertNotIn("secret shell command", self.log_path.read_text())

    def test_replacement_captures_old_identity_before_respawn(self):
        calls = []
        old = "old-attachment\t100\tlocal:old\told-agent\told-switch\told-run\t100"
        with (
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "out", return_value=old,
                         side_effect=lambda *a, **k: calls.append(("query", a)) or old),
            patch.object(cockpit.tmux, "tmux", side_effect=lambda *a, **k: calls.append(("command", a))),
        ):
            cockpit.switch(Target("local", "new"), lambda: "attach new")
        replaced = [r for r in self.records() if r["event"] == "attachment_replaced"]
        self.assertEqual(replaced[0]["attachment_id"], "old-attachment")
        self.assertEqual(replaced[0]["parent_run_id"], "old-run")
        self.assertEqual(replaced[0]["generation"], 100)
        self.assertTrue(replaced[0]["intentional"])
        query = next(i for i, call in enumerate(calls) if call[0] == "query")
        respawn = next(i for i, call in enumerate(calls) if call[1][0] == "respawn-pane")
        self.assertLess(query, respawn)

    def test_switch_respawn_and_focus_failures_do_not_report_completion(self):
        for command, stage in (("respawn-pane", "right_pane_respawn"), ("select-pane", "right_pane_focus")):
            error = OSError("private detail")

            def execute(*args, **kwargs):
                if args[0] == command:
                    raise error

            with (
                self.subTest(command=command),
                patch.object(cockpit, "right_pane", return_value="%2"),
                patch.object(cockpit.tmux, "out", return_value=""),
                patch.object(cockpit.tmux, "tmux", side_effect=execute),
                self.assertRaises(OSError) as raised,
            ):
                cockpit.switch(Target("local", "work"), "private command")
            self.assertIs(raised.exception, error)
            self.assert_handled(stage, "OSError", "propagated")
            records = self.records()
            self.assertTrue(any(r["event"] == "switch_error" and r["stage"] == stage for r in records))
            self.assertFalse(any(r["event"] == "switch_completed" for r in records))
        self.assertNotIn("private command", self.log_path.read_text())
        self.assertNotIn("private detail", self.log_path.read_text())

    def test_switch_failure_records_failed_stage(self):
        target = Target("local", "work")
        with (
            patch.object(cockpit, "right_pane", return_value="%2"),
            patch.object(cockpit.tmux, "tmux", side_effect=OSError("denied")),
            self.assertRaises(OSError),
        ):
            cockpit.switch(target, "attach work")

        failed = [
            record for record in self.records()
            if record["event"] == "switch_error"
        ]
        self.assertEqual(failed[0]["stage"], "current_target_marker")
        self.assertEqual(failed[0]["error_type"], "OSError")

class CockpitKeybindingTest(unittest.TestCase):
    def test_install_bindings_uses_custom_keybindings(self):
        custom = {
            "previous_session": "prefix+M-Up",
            "next_session": "prefix+M-Down",
            "previous_agent": "prefix+M-Left",
            "next_agent": "prefix+M-Right",
            "focus_agents": "prefix+C-a",
            "focus_sessions": "prefix+C-b",
            "add_session": "prefix+C-c",
            "remove_active": "prefix+C-d",
            "kill_active": "prefix+C-e",
            "jump_alert": "prefix+C-f",
            "focus_right": "prefix+C-g",
            "toggle_sidebar": "prefix+C-h",
            "quit": "prefix+C-i",
            "help": "prefix+C-j",
            "detach": "prefix+C-k",
        }
        calls = []
        with patch.object(cockpit, "_option", return_value=""), patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)):
            cockpit._install_bindings("C-x", "%1", "%2", keybindings=custom)
        # check each custom effective key appears in prefix table binds (without -n)
        for eff in ("C-a", "C-b", "C-c", "C-d", "C-e", "C-f", "C-g", "C-h", "C-i", "C-j", "C-k"):
            self.assertIn(("bind-key", eff), [(c[0], c[1]) for c in calls])
        for key, action in (("M-Left", "previous_agent"), ("M-Right", "next_agent"), ("M-Up", "previous_session"), ("M-Down", "next_session")):
            self.assertIn(("bind-key", key, "run-shell", f"{cockpit.FOCUS_SIDEBAR} {action}"), calls)
        # defaults should not appear
        flat = [c[1] if len(c) > 1 else "" for c in calls]
        self.assertNotIn("a", flat)
        self.assertNotIn("s", flat)
        # slots still fixed
        self.assertIn("1", flat)
        self.assertIn("9", flat)

    def test_install_bindings_supports_global_without_prefix(self):
        custom = {
            "previous_session": "M-Up",
            "next_session": "M-Down",
            "previous_agent": "M-Left",
            "next_agent": "M-Right",
            "focus_agents": "C-a",
            "focus_sessions": "C-b",
            "add_session": "C-c",
            "remove_active": "C-d",
            "kill_active": "C-e",
            "jump_alert": "C-f",
            "focus_right": "C-g",
            "toggle_sidebar": "C-h",
            "quit": "C-i",
            "help": "C-j",
            "detach": "C-k",
        }
        calls = []
        with patch.object(cockpit, "_option", return_value=""), patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)):
            cockpit._install_bindings("C-x", "%1", "%2", keybindings=custom)
        # global binds use -n flag
        for eff in ("C-a", "C-b", "C-c", "C-d", "C-e", "C-f", "C-g", "C-h", "C-i", "C-j", "C-k"):
            self.assertIn(("bind-key", "-n", eff), [(c[0], c[1], c[2]) if len(c) > 2 else c for c in calls])

        for key, action in (("M-Left", "previous_agent"), ("M-Right", "next_agent"), ("M-Up", "previous_session"), ("M-Down", "next_session")):
            self.assertIn(("bind-key", "-n", key, "run-shell", f"{cockpit.FOCUS_SIDEBAR} {action}"), calls)

    def test_install_bindings_unbinds_comma_global_key_on_rerun(self):
        custom = dict(cockpit.DEFAULT_KEYBINDINGS)
        custom["focus_agents"] = ","
        calls = []
        with (
            patch.object(cockpit, "_option", side_effect=["", ","]),
            patch.object(cockpit.tmux, "tmux", side_effect=lambda *args, **kwargs: calls.append(args)),
        ):
            cockpit._install_bindings("C-x", "%1", "%2", keybindings=custom)
            cockpit._install_bindings("C-x", "%1", "%2", keybindings=custom)

        self.assertEqual(calls.count(("unbind-key", "-q", "-T", "root", ",")), 1)

    def test_install_bindings_validates_sidebar_before_mutating_tmux(self):
        with (
            patch.object(cockpit, "_option", return_value="d q"),
            patch.object(cockpit, "load_sidebar_keybindings", side_effect=SystemExit("invalid sidebar")),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
            self.assertRaisesRegex(SystemExit, "invalid sidebar"),
        ):
            cockpit._install_bindings("C-x", "%1", "%2", keybindings=cockpit.DEFAULT_KEYBINDINGS)

        tmux_call.assert_not_called()

    def test_help_generated_from_custom_bindings(self):
        custom = {
            "previous_session": "prefix+M-Up",
            "next_session": "prefix+M-Down",
            "previous_agent": "prefix+M-Left",
            "next_agent": "prefix+M-Right",
            "focus_agents": "prefix+C-a",
            "focus_sessions": "prefix+C-b",
            "add_session": "prefix+C-c",
            "remove_active": "prefix+C-d",
            "kill_active": "prefix+C-e",
            "jump_alert": "prefix+C-f",
            "focus_right": "prefix+C-g",
            "toggle_sidebar": "prefix+C-h",
            "quit": "prefix+C-i",
            "help": "prefix+C-j",
            "detach": "prefix+C-k",
        }
        sidebar = {
            "navigate_down": "n",
            "navigate_up": "p",
            "rename": "R",
            "remove": "D",
            "kill": "X",
            "move_up": "U",
            "move_down": "N",
            "resize_inc": "{",
            "resize_dec": "}",
        }
        with patch.dict(cockpit.os.environ, {}, clear=True), patch.object(cockpit.locale, "getpreferredencoding", return_value="UTF-8"):
            cmd = cockpit.help_command("C-x", custom, sidebar)
        self.assertIn("C-x C-a  focus/open Agents", cmd)
        self.assertIn("C-x C-b  focus/open Sessions", cmd)
        self.assertIn("C-x C-c  add session", cmd)
        self.assertIn("C-x C-d  remove active", cmd)
        self.assertIn("C-x C-e  kill", cmd)
        self.assertIn("C-x C-f  jump", cmd)
        self.assertIn("C-x M-Left  switch to previous agent", cmd)
        self.assertIn("C-x M-Right  switch to next agent", cmd)
        self.assertIn("C-x M-Up  switch to previous session", cmd)
        self.assertIn("C-x M-Down  switch to next session", cmd)
        self.assertIn("C-x C-g  focus right", cmd)
        self.assertIn("C-x C-h  collapse/expand", cmd)
        self.assertIn("C-x C-i  quit", cmd)
        self.assertIn("C-x C-j  open help", cmd)
        self.assertIn("C-x C-k  detach", cmd)
        # global without prefix shows without prefix
        global_custom = {k: v.replace("prefix+", "") for k, v in custom.items()}
        cmd2 = cockpit.help_command("C-x", global_custom, sidebar)
        self.assertIn("C-a  focus/open Agents", cmd2)
        self.assertIn("M-Right  switch to next agent", cmd2)
        self.assertNotIn("C-x M-Right", cmd2)
        self.assertIn("M-Down  switch to next session", cmd2)
        self.assertNotIn("C-x M-Down", cmd2)
        self.assertNotIn("C-x C-a", cmd2)
        self.assertIn("R      rename", cmd)
        self.assertIn("D      remove", cmd)
        self.assertIn("U/N", cmd)
        self.assertIn("X      kill", cmd)
        self.assertIn("n/p    navigate", cmd)
        self.assertIn("{ / }  resize", cmd)

    def test_session_menu_uses_sidebar_accelerators(self):
        custom_sidebar = {
            "navigate_down": "j",
            "navigate_up": "k",
            "rename": "R",
            "remove": "D",
            "kill": "X",
            "move_up": "K",
            "move_down": "J",
            "resize_inc": "[",
            "resize_dec": "]",
        }
        with (
            patch.object(cockpit, "_option", return_value="%1"),
            patch.object(cockpit, "load_sidebar_keybindings", return_value=custom_sidebar),
            patch.object(cockpit.tmux, "out", return_value="tmux 3.6"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.show_session_menu(Target("local", "work"), 2, 3)
        args = tmux_call.call_args.args
        self.assertIn("R", args)
        self.assertIn("D", args)
        self.assertIn("X", args)
        self.assertIn("send-keys -t %1 R", args)
        self.assertIn("send-keys -t %1 D", args)
        self.assertIn("send-keys -t %1 X y", args)

    def test_agent_menu_uses_sidebar_kill_key(self):
        custom_sidebar = {
            "navigate_down": "j",
            "navigate_up": "k",
            "rename": "e",
            "remove": "r",
            "kill": "X",
            "move_up": "K",
            "move_down": "J",
            "resize_inc": "[",
            "resize_dec": "]",
        }
        pane = PaneTarget(Target("local", "work"), "@1", "%2", "/tmp/tmux")
        with (
            patch.object(cockpit, "_option", return_value="%1"),
            patch.object(cockpit, "load_sidebar_keybindings", return_value=custom_sidebar),
            patch.object(cockpit.tmux, "out", return_value="tmux 3.6"),
            patch.object(cockpit.tmux, "tmux") as tmux_call,
        ):
            cockpit.show_agent_menu("pi", pane, 2, 3)
        args = tmux_call.call_args.args
        self.assertIn("X", args)
        self.assertIn("send-keys -t %1 X y", args)


class CockpitSIGWINCHTest(unittest.TestCase):
    def test_stale_tty_repairs_with_real_dimensions_and_refreshes_client(self):
        for width, height in ((100, 24), (160, 40)):
            with self.subTest(width=width, height=height):
                with (
                    patch.object(cockpit.tmux, "out", return_value="1234:/dev/pts/3:80:24:letee") as tmux_out,
                    patch.object(cockpit.os, "open", return_value=3) as mock_open,
                    patch.object(cockpit.os, "get_terminal_size", return_value=os.terminal_size((width, height))) as mock_get,
                    patch.object(cockpit.os, "close") as mock_close,
                    patch.object(cockpit.os, "kill") as mock_kill,
                    patch.object(cockpit.tmux, "tmux") as tmux_run,
                ):
                    cockpit.repair_layout("%1")

                mock_open.assert_called_once_with("/dev/pts/3", os.O_RDONLY | os.O_NOCTTY)
                mock_get.assert_called_once_with(3)
                mock_close.assert_called_once_with(3)
                mock_kill.assert_called_once_with(1234, signal.SIGWINCH)
                tmux_out.assert_called_once_with(
                    "list-clients", "-t", cockpit.tmux.SESSION, "-F", "#{client_pid}:#{client_tty}:#{client_width}:#{client_height}:#{client_session}", check=False
                )
                tmux_run.assert_called_once_with(
                    "run-shell", "-C", "-t", "%1",
                    f"resize-window -x {width} -y {height} -t {cockpit.TARGET} ; resize-pane -t %1 -x '#{{@letee_sidebar_width}}' ; refresh-client -t /dev/pts/3",
                    check=False,
                )

    def test_matching_tty_sends_no_signal_and_repairs(self):
        # sizes agree, so no SIGWINCH and layout repair proceeds
        def out_side_effect(*args, **kw):
            if args[0] == "list-clients":
                return "1234:/dev/pts/3:80:24:letee"
            return "10:52:160:100:100:100:60"
        with (
            patch.object(cockpit.tmux, "out", side_effect=out_side_effect) as tmux_out,
            patch.object(cockpit.os, "open", return_value=3),
            patch.object(cockpit.os, "get_terminal_size", return_value=os.terminal_size((80, 24))),
            patch.object(cockpit.os, "close"),
            patch.object(cockpit.os, "kill") as mock_kill,
            patch.object(cockpit.tmux, "tmux") as tmux_run,
        ):
            cockpit.repair_layout("%1")
            mock_kill.assert_not_called()
            # both queries happened and repair ran
            self.assertEqual(tmux_out.call_count, 2)
            tmux_run.assert_called_once()

    def test_detached_malformed_inaccessible_vanished_ignored_safely(self):
        import contextlib
        cases = [
            ("detached empty tty", "1234::80:24:letee", None),
            ("detached wrong session", "1234:/dev/pts/3:80:24:other", None),
            ("malformed not enough parts", "bad", None),
            ("malformed non-int", "a:/dev/pts/3:80:24:letee", None),
            ("inaccessible tty", "1234:/dev/pts/99:80:24:letee", OSError(5, "no tty")),
            ("vanished pid mismatch", "1234:/dev/pts/3:80:24:letee", "kill_vanish"),
        ]
        for label, raw, _ in cases:
            with self.subTest(label=label):
                def out_side(*args, **kw):
                    if args[0] == "list-clients":
                        return raw
                    return "52:52:100:100:100:100:"
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(cockpit.tmux, "out", side_effect=out_side))
                    kill_p = stack.enter_context(patch.object(cockpit.os, "kill"))
                    run_p = stack.enter_context(patch.object(cockpit.tmux, "tmux"))
                    if label == "inaccessible tty":
                        stack.enter_context(patch.object(cockpit.os, "open", side_effect=OSError(5, "no tty")))
                    elif label == "vanished pid mismatch":
                        stack.enter_context(patch.object(cockpit.os, "open", return_value=3))
                        stack.enter_context(patch.object(cockpit.os, "get_terminal_size", return_value=os.terminal_size((100, 24))))
                        stack.enter_context(patch.object(cockpit.os, "close"))
                        kill_p.side_effect = ProcessLookupError(3, "gone")
                    elif label in ("detached empty tty", "detached wrong session", "malformed not enough parts", "malformed non-int"):
                        pass
                    else:
                        pass
                    cockpit.repair_layout("%1")
                    if label == "vanished pid mismatch":
                        kill_p.assert_called_once_with(1234, signal.SIGWINCH)
                        run_p.assert_not_called()
                    elif label in ("detached empty tty", "detached wrong session", "malformed not enough parts", "malformed non-int"):
                        kill_p.assert_not_called()
                        run_p.assert_not_called()
                    elif label == "inaccessible tty":
                        kill_p.assert_not_called()
                        run_p.assert_not_called()

    def test_attach_executes_bundled_tmux_directly_without_script(self):
        executable = "/tmp/tmux with space"
        with (
            patch.object(cockpit.tmux, "tmux_executable", return_value=executable),
            patch.object(cockpit.sys.stdin, "isatty", return_value=True),
            patch.object(cockpit.sys.stdout, "isatty", return_value=True),
            patch.object(cockpit.os, "ttyname", return_value="/dev/pts/2"),
            patch.object(cockpit.shutil, "which", return_value="/usr/bin/script") as which,
            patch.object(cockpit.os, "execvp", side_effect=RuntimeError) as execvp,
            self.assertRaises(RuntimeError),
        ):
            cockpit._attach()

        which.assert_not_called()
        execvp.assert_called_once_with(
            executable,
            [executable, "-L", "letee@v1", "attach-session", "-d", "-t", "letee:cockpit"],
        )


if __name__ == "__main__":
    unittest.main()
