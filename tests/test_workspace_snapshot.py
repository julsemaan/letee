from dataclasses import replace
from datetime import datetime, timezone
import json
import unittest

from letee.discovery import AgentEntry, SessionSnapshot, SourceSnapshot, UNAVAILABLE
from letee.names import PaneTarget, Target
from letee.workspace import (
    AgentActivity, build_workspace, target_status, visible_agents,
    workspace_from_json, workspace_to_json,
)


class WorkspaceSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.target = Target("ssh", "work", "dev")
        self.pane = PaneTarget(
            self.target, "@42", "%100", "/tmp/tmux/東京", "編集",
            session_id="$17", window_index=3, pane_index=2,
            window_active=True, pane_active=True,
        )
        self.agent = AgentEntry(self.pane, "agent-α", "Fix café", None,
                                datetime(2026, 1, 2, tzinfo=timezone.utc))

    def snapshot(self, panes=None, agents=None, focused=None, bells=frozenset()):
        return SessionSnapshot(SourceSnapshot(True, (), frozenset()), {
            "dev": SourceSnapshot(True, (self.target,), bells,
                                  panes=tuple(panes if panes is not None else (self.pane,)),
                                  agents=tuple(agents if agents is not None else (self.agent,)),
                                  focused_panes=frozenset(focused if focused is not None else (self.pane,))),
            "pending": None,
        })

    def test_assembly_shares_visibility_fallback_and_alerts(self):
        hidden = replace(self.agent, agent_id="hidden")
        untracked = replace(self.agent, pane_target=replace(self.pane, target=Target("ssh", "other", "dev")))
        snapshot = self.snapshot(agents=(self.agent, hidden, untracked), bells=frozenset({self.target}))
        keys = {(self.pane, "hidden")}
        activity = AgentActivity()
        activity.update(snapshot, [self.target], None, hidden=keys)
        workspace = build_workspace(snapshot, [self.target], hidden=keys, alerts={(self.pane, self.agent.agent_id)})
        session = workspace.sessions[0]
        agent = session.windows[0].agents[0]
        self.assertEqual(len(visible_agents(snapshot, [self.target], keys)), 1)
        self.assertEqual(agent.state, activity.states[(self.pane, self.agent.agent_id)])
        self.assertEqual(agent.state, "idle")
        self.assertTrue(agent.alert)
        self.assertTrue(session.bell)
        self.assertTrue(workspace.loading)
        self.assertEqual(session.key, ("ssh", "dev", self.pane.socket_path, "$17"))
        self.assertEqual((session.windows[0].index, agent.pane_index), (3, 2))

    def test_dedup_windows_sort_indexes_and_keep_normal_agent_order(self):
        first = replace(self.pane, window_id="@99", window_index=0, pane_id="%9", pane_index=0)
        earlier = replace(self.pane, pane_id="%11", pane_index=1)
        same_pane = replace(self.agent, agent_id="agent-z")
        snapshot = self.snapshot(panes=(self.pane, earlier, first, self.pane), agents=(self.agent, same_pane, replace(self.agent, pane_target=earlier)))
        workspace = build_workspace(snapshot, [self.target])
        self.assertEqual([w.window_id for w in workspace.sessions[0].windows], ["@99", "@42"])
        self.assertEqual([a.agent_id for a in workspace.sessions[0].windows[1].agents], ["agent-α", "agent-z", "agent-α"])
        self.assertEqual([a.pane_index for a in workspace.sessions[0].windows[1].agents], [1, 2, 2])

    def test_rename_preserves_identity_and_updates_metadata(self):
        original = build_workspace(self.snapshot(), [self.target], self.target, self.agent.agent_id)
        renamed = Target("ssh", "renamed", "dev")
        pane = replace(self.pane, target=renamed, window_name="new")
        snapshot = SessionSnapshot(SourceSnapshot(True, (), frozenset()), {
            "dev": SourceSnapshot(True, (renamed,), frozenset(), panes=(pane,), agents=(replace(self.agent, pane_target=pane),), focused_panes=frozenset({pane})),
        })
        result = build_workspace(snapshot, [renamed], renamed, self.agent.agent_id, previous=original)
        self.assertEqual(result.sessions[0].key, original.sessions[0].key)
        self.assertEqual(result.current, original.current)
        self.assertEqual(result.sessions[0].windows[0].name, "new")
        self.assertEqual(result.sessions[0].windows[0].agents[0].pane_target.target, renamed)

    def test_unavailable_preserves_selection_missing_clears_children_reconnects(self):
        before = build_workspace(self.snapshot(), [self.target], self.target, self.agent.agent_id)
        disconnected = SessionSnapshot(UNAVAILABLE, {"dev": UNAVAILABLE})
        after = build_workspace(disconnected, [self.target], self.target, previous=before)
        self.assertFalse(after.sessions[0].available)
        self.assertEqual(after.sessions[0].windows, before.sessions[0].windows)
        self.assertEqual(after.current, before.current)
        missing = SessionSnapshot(UNAVAILABLE, {"dev": SourceSnapshot(True, (), frozenset())})
        gone = build_workspace(missing, [self.target], self.target, previous=after)
        self.assertEqual(gone.sessions[0].key, before.sessions[0].key)
        self.assertEqual(gone.sessions[0].windows, ())
        self.assertEqual(gone.current, before.sessions[0].key)
        restored = build_workspace(self.snapshot(), [self.target], self.target, self.agent.agent_id, previous=gone)
        self.assertEqual(restored.current, before.current)

    def test_pending_source_is_distinct_from_confirmed_absence(self):
        remote = Target("ssh", "pending", "pending")
        result = build_workspace(self.snapshot(), [remote, Target("local", "missing")])
        self.assertTrue(result.sessions[0].pending)
        self.assertFalse(result.sessions[1].pending)
        self.assertEqual(workspace_from_json(workspace_to_json(result)), result)

    def test_unknown_placeholder_is_not_tmux_id_and_tracked_order_only(self):
        missing = Target("local", "missing")
        workspace = build_workspace(self.snapshot(), [missing, self.target], bells={missing})
        self.assertEqual([s.target for s in workspace.sessions], [missing, self.target])
        self.assertEqual(workspace.sessions[0].session_id, "missing:missing")
        self.assertEqual(workspace.sessions[0].socket_path, "")
        self.assertFalse(workspace.sessions[0].available)
        self.assertTrue(workspace.sessions[0].bell)

    def test_first_discovery_replaces_placeholder_and_drops_vanished_windows(self):
        unknown = build_workspace(self.snapshot(panes=(), agents=()), [self.target], self.target)
        self.assertEqual(unknown.sessions[0].session_id, "missing:work")
        discovered = build_workspace(self.snapshot(), [self.target], self.target, previous=unknown)
        self.assertNotEqual(discovered.current, unknown.current)
        self.assertTrue(discovered.sessions[0].available)
        replacement = replace(self.pane, window_id="@80", pane_id="%200")
        updated = build_workspace(self.snapshot(panes=(replacement,), agents=()), [self.target], self.target, previous=discovered)
        self.assertEqual([w.window_id for w in updated.sessions[0].windows], ["@80"])
        self.assertEqual(updated.sessions[0].windows[0].agents, ())
        self.assertEqual(workspace_from_json(workspace_to_json(unknown)), unknown)

    def test_current_exact_focused_agent_then_window_then_session(self):
        other = replace(self.pane, window_id="@1", window_index=0, pane_id="%1", pane_index=0, window_active=False)
        snapshot = self.snapshot(panes=(other, self.pane), agents=(replace(self.agent, pane_target=other), self.agent))
        selected = build_workspace(snapshot, [self.target], self.target, self.agent.agent_id)
        key = selected.sessions[0].key
        self.assertEqual(selected.current, key + ("@42", "%100", self.agent.agent_id))
        no_agent = build_workspace(snapshot, [self.target], self.target)
        self.assertEqual(no_agent.current, key + ("@42",))
        no_focus = build_workspace(self.snapshot(focused=()), [self.target], self.target)
        self.assertEqual(no_focus.current, key + ("@42",))
        no_active = build_workspace(self.snapshot(panes=(replace(self.pane, window_active=False),), focused=()), [self.target], self.target)
        self.assertEqual(no_active.current, key)

    def test_roundtrip_is_compact_nested_and_preserves_metadata(self):
        agent = replace(self.agent, task_status_timestamp=datetime(2026, 2, 3, 4, 5, 6, 789, timezone.utc))
        workspace = build_workspace(self.snapshot(agents=(agent,)), [self.target], self.target, agent.agent_id, alerts={(self.pane, agent.agent_id)})
        text = workspace_to_json(workspace)
        restored = workspace_from_json(text)
        self.assertEqual(restored, workspace)
        self.assertEqual(workspace_to_json(restored), text)
        self.assertIn("東京", text)
        self.assertEqual(text.count(self.pane.socket_path), 2)  # Session and current identity only.
        decoded = json.loads(text)
        self.assertIsInstance(decoded["current"], list)
        nested = decoded["sessions"][0]["windows"][0]["agents"][0]
        self.assertNotIn("target", nested)
        self.assertNotIn("socket_path", nested)
        pane = restored.sessions[0].windows[0].agents[0].pane_target
        self.assertEqual((pane.session_id, pane.window_index, pane.pane_index), ("$17", 3, 2))
        self.assertEqual((pane.window_id, pane.pane_id, pane.window_name), ("@42", "%100", "編集"))

    def test_rejects_malformed_shapes_types_ids_and_current(self):
        valid = json.loads(workspace_to_json(build_workspace(self.snapshot(), [self.target])))
        mutations = [
            lambda d: d.update(version=2), lambda d: d.update(version=True),
            lambda d: d.update(loading=1), lambda d: d.update(sessions={}),
            lambda d: d.update(current=["ssh", "dev", "/wrong", "$17"]),
            lambda d: d.update(current="wrong"),
            lambda d: d["sessions"].append(d["sessions"][0]),
            lambda d: d["sessions"][0]["windows"].append(d["sessions"][0]["windows"][0]),
            lambda d: d["sessions"][0].pop("host"),
            lambda d: d["sessions"][0].update(windows=[None]),
            lambda d: d["sessions"][0].update(host=3),
            lambda d: d["sessions"][0].update(socket_path=""),
            lambda d: d["sessions"][0].update(session_id="17"),
            lambda d: d["sessions"][0].update(available="yes"),
            lambda d: d["sessions"][0].update(kind="bogus"),
            lambda d: d["sessions"][0].update(session="bad session"),
            lambda d: d["sessions"][0]["windows"][0].update(window_id="42"),
            lambda d: d["sessions"][0]["windows"][0].update(index=True),
            lambda d: d["sessions"][0]["windows"][0]["agents"][0].update(pane_id="100"),
            lambda d: d["sessions"][0]["windows"][0]["agents"][0].update(agent_id=""),
            lambda d: d["sessions"][0]["windows"][0]["agents"][0].update(pane_index=-1),
            lambda d: d["sessions"][0]["windows"][0]["agents"][0].update(alert=1),
            lambda d: d["sessions"][0]["windows"][0]["agents"][0].update(runtime_updated_at="yesterday"),
        ]
        for mutate in mutations:
            payload = json.loads(json.dumps(valid))
            mutate(payload)
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                workspace_from_json(json.dumps(payload))
        for text in ("{", "null", "[]", "{}", '"hi"'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                workspace_from_json(text)

    def test_target_status_matches_sidebar_contract(self):
        self.assertIsNone(target_status(self.target, self.snapshot()))
        self.assertEqual(target_status(Target("ssh", "work", "pending"), self.snapshot()), "connecting…")
        self.assertEqual(target_status(self.target, SessionSnapshot(UNAVAILABLE, {"dev": UNAVAILABLE})), "reconnecting…")
        self.assertEqual(target_status(Target("local", "missing"), self.snapshot()), "missing")
        self.assertEqual(target_status(self.target, SessionSnapshot(UNAVAILABLE, {})), "unavailable")
