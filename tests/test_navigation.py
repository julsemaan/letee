import unittest
from unittest.mock import Mock, patch

from letee import cockpit
from letee.names import PaneTarget, Target


class NavigationTest(unittest.TestCase):
    def test_open_session_defers_attachment_to_switch(self):
        for target in (Target("local", "work"), Target("ssh", "work", "dev")):
            with (
                self.subTest(target=target),
                patch.object(cockpit, "switch") as switch,
                patch.object(cockpit.sessions, "attach_command", return_value="attach") as attach,
            ):
                cockpit.open_session(target)

                attach.assert_not_called()
                self.assertEqual(switch.call_args.args[0], target)
                self.assertEqual(switch.call_args.args[1](), "attach")
                attach.assert_called_once_with(target)

    def test_open_session_passes_supplied_command_through(self):
        target = Target("ssh", "work", "dev")
        for command in ("prepared attach", "", Mock(return_value="attach")):
            with (
                self.subTest(command=command),
                patch.object(cockpit, "switch") as switch,
                patch.object(cockpit.sessions, "attach_command") as attach,
            ):
                cockpit.open_session(target, command)

                switch.assert_called_once_with(target, command)
                attach.assert_not_called()
                if callable(command):
                    command.assert_not_called()

    def test_open_agent_defers_exact_pane_attachment_and_preserves_agent_id(self):
        pane = PaneTarget(Target("ssh", "work", "dev"), "@2", "%8", "/tmp/tmux")
        with (
            patch.object(cockpit, "switch") as switch,
            patch.object(cockpit.sessions, "pane_attach_command", return_value="pane attach") as attach,
        ):
            cockpit.open_agent(pane, "agent-1")

            attach.assert_not_called()
            target, prepare, agent_id = switch.call_args.args
            self.assertEqual(target, pane.target)
            self.assertEqual(agent_id, "agent-1")
            self.assertEqual(prepare(), "pane attach")
            attach.assert_called_once_with(pane)
