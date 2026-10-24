from dataclasses import replace
from datetime import datetime, timezone
import unittest

from letee.discovery import AgentEntry, SessionSnapshot, SourceSnapshot
from letee.names import PaneTarget, Target
from letee.workspace import AgentActivity, Session, Window, visible_agents


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.target = Target("local", "letee")
        self.pane = PaneTarget(self.target, "@1", "%2", "/tmp/inner", "editor")
        self.agent = AgentEntry(self.pane, "agent-1", "Fix issue", "working")

    def snapshot(self, agents, focused=()):
        return SessionSnapshot(SourceSnapshot(
            True, (self.target,), frozenset(), panes=(self.pane,),
            agents=tuple(agents), focused_panes=frozenset(focused),
        ), {})

    def test_identity_survives_display_changes_but_separates_hosts_and_servers(self):
        session = Session(self.target, "$1", "/tmp/inner", (Window("@1", 0, "editor"),))
        self.assertEqual(session.key, replace(session, target=Target("local", "renamed")).key)
        self.assertNotEqual(session.key, replace(session, socket_path="/tmp/other").key)
        remote = replace(session, target=Target("ssh", "letee", "remote"))
        self.assertNotEqual(session.key, remote.key)
        self.assertNotEqual(session.key, replace(session, session_id="$2").key)

    def test_visibility_is_tracked_and_not_hidden(self):
        other = replace(self.agent, pane_target=replace(self.pane, target=Target("local", "other")))
        snapshot = self.snapshot((self.agent, other))
        self.assertEqual(visible_agents(snapshot, [self.target]), (self.agent,))
        self.assertEqual(visible_agents(snapshot, [self.target], {(self.pane, "agent-1")}), ())

    def test_activity_transitions_alerts_focus_and_removal(self):
        activity = AgentActivity()
        key = (self.pane, "agent-1")
        self.assertFalse(activity.update(self.snapshot((self.agent,)), [self.target], None, 10))
        completed = replace(self.agent, task_state="completed")
        self.assertTrue(activity.update(self.snapshot((completed,)), [self.target], None, 20))
        self.assertEqual(activity.alerts, {key})
        self.assertEqual(activity.transition_at, {key: 20})
        renamed_window = replace(completed, pane_target=replace(self.pane, window_name="new"))
        self.assertFalse(activity.update(self.snapshot((renamed_window,), (self.pane,)), [self.target], self.target, 30))
        self.assertEqual(activity.alerts, set())
        self.assertEqual(activity.transition_at, {key: 20})
        activity.update(self.snapshot(()), [self.target], None, 40)
        self.assertEqual(activity.states, {})
        self.assertEqual(activity.transition_at, {})

    def test_hidden_agents_do_not_alert_and_missing_state_is_idle(self):
        activity = AgentActivity()
        snapshot = self.snapshot((self.agent,))
        activity.update(snapshot, [self.target], None, 10)
        idle = replace(self.agent, task_state=None, runtime_updated_at=datetime.now(timezone.utc))
        self.assertTrue(activity.update(self.snapshot((idle,)), [self.target], None, 20))
        self.assertEqual(activity.states[(self.pane, "agent-1")], "idle")
        activity.update(snapshot, [self.target], None, 30, {(self.pane, "agent-1")})
        self.assertEqual(activity.alerts, set())
        self.assertEqual(activity.states, {})
