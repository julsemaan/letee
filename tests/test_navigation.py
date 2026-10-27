import unittest
from unittest.mock import Mock, patch

from letee import cockpit
from letee.names import PaneTarget, Target


class NavigationTest(unittest.TestCase):
    def test_open_session_passes_eager_attachment_to_switch(self):
        for target in (Target("local", "work"), Target("ssh", "work", "dev")):
            with (
                self.subTest(target=target),
                patch.object(cockpit, "switch") as switch,
                patch.object(cockpit.sessions, "attach_command", return_value="attach") as attach,
            ):
                cockpit.open_session(target)

                attach.assert_called_once_with(target)
                switch.assert_called_once_with(target, "attach")

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

    def test_open_agent_builds_exact_pane_attachment_and_preserves_agent_id(self):
        pane = PaneTarget(Target("ssh", "work", "dev"), "@2", "%8", "/tmp/tmux")
        with (
            patch.object(cockpit, "switch") as switch,
            patch.object(cockpit.sessions, "pane_attach_command", return_value="pane attach") as attach,
        ):
            cockpit.open_agent(pane, "agent-1")

            attach.assert_called_once_with(pane)
            switch.assert_called_once_with(pane.target, "pane attach", "agent-1")
