import json
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from letee import config, discovery as d


def report(state="working", app="codex", pane="%2"):
    return f"work:@1:{pane}:0::1:1:/scratch/sock\t$1:0:0:osc7501:{app}:{state}\tname:with\ttabs"


class NativeCodexTests(unittest.TestCase):
    def test_config(self):
        for value in (False, True):
            with patch.object(config, "_load_config", return_value=("test", {"experimental_native_codex": value})):
                self.assertIs(config.load_experimental_native_codex(), value)
        with patch.object(config, "_load_config", return_value=("test", {})):
            self.assertFalse(config.load_experimental_native_codex())
        with patch.object(config, "_load_config", return_value=("test", {"experimental_native_codex": "true"})):
            with self.assertRaises(SystemExit):
                config.load_experimental_native_codex()

    @patch.object(d, "load_experimental_native_codex", return_value=True)
    def test_mapping_identity_names_and_merge(self, _):
        ids = set()
        for state, expected in {"working": "working", "blocked": "input-required", "idle": "idle", "done": "completed", "error": "failed", "future": "unknown"}.items():
            snapshot = d._parse_source_snapshot(report(state), kind="local")
            agent, = snapshot.agents
            ids.add(agent.agent_id)
            self.assertEqual(agent.task_state, expected)
            self.assertIsNone(agent.activity_timestamp)
            self.assertEqual(agent.pane_target.window_name, "name:with\ttabs")
            plugin = d.AgentEntry(agent.pane_target, "plugin", "Codex", "working")
            other = d.AgentEntry(agent.pane_target, "other", "Claude", "working")
            self.assertEqual(d._merge_agents(snapshot.agents, (plugin, other)), (agent, other))
        self.assertEqual(len(ids), 1)
        snapshot = d._parse_source_snapshot(report() + "\n" + report(pane="%3"), kind="local")
        self.assertEqual(len({a.agent_id for a in snapshot.agents}), 2)

    @patch.object(d, "load_experimental_native_codex", return_value=True)
    def test_identity_is_unique_and_stable_across_sources(self, _):
        ids = set()
        for kind, host in (("local", None), ("ssh", "host-a"), ("ssh", "host-b")):
            agent, = d._parse_source_snapshot(report(), kind=kind, host=host).agents
            repeated, = d._parse_source_snapshot(report("idle"), kind=kind, host=host).agents
            self.assertEqual(agent.agent_id, repeated.agent_id)
            ids.add(agent.agent_id)
        self.assertEqual(len(ids), 3)

    @patch.object(d, "load_experimental_native_codex", return_value=True)
    def test_clear_legacy_and_remote_without_python(self, _):
        for text in (report("", ""), report("clear"), "work:@1:%2:0::/scratch/sock\tlegacy:with\ttabs", "work:@1:%2:0::1:1:/scratch/sock\t$1:0:0\tlegacy:with\ttabs"):
            snapshot = d._parse_source_snapshot(text, kind="local")
            self.assertFalse(snapshot.agents)
            self.assertEqual(len(snapshot.panes), 1)
            plugin = d.AgentEntry(snapshot.panes[0], "plugin", "Codex", "working")
            self.assertEqual(d._merge_agents(snapshot.agents, (plugin,)), (plugin,))
        remote = d._source_result(0, report() + f'\n{d.REMOTE_SEPARATOR}\n{{"_letee_reader_exit":127}}\n', "python missing", kind="ssh", host="host")
        self.assertEqual(len(remote.agents), 1)
        self.assertEqual(remote.agents[0].pane_target.target.host, "host")
        with patch.object(d.subprocess, "run", return_value=type("Result", (), {"returncode": 0, "stdout": report(), "stderr": ""})()), patch.object(d, "_read_local_agents", return_value=()):
            self.assertEqual(len(d.local_snapshot().agents), 1)

    @patch.object(d, "load_experimental_native_codex", return_value=True)
    def test_local_and_remote_plugin_deduplication(self, _):
        record = {
            "schema_version": "agent-status/v1alpha1", "agent_id": "plugin",
            "agent_name": "Codex", "task": {"state": "failed"},
            "runtime": {"lifecycle": "running", "updated_at": datetime.now(timezone.utc).isoformat()},
            "x_meta": {"tmux_socket": "/scratch/sock", "tmux_pane": "%2"},
        }
        for kind, host in (("local", None), ("ssh", "host")):
            snapshot = d._source_result(0, report() + f"\n{d.REMOTE_SEPARATOR}\n" + json.dumps(record), "", kind=kind, host=host)
            self.assertEqual(len(snapshot.agents), 1)
            self.assertEqual(snapshot.agents[0].task_state, "working")
        pane = d._parse_source_snapshot(report(), kind="local").panes[0]
        plugin = d.AgentEntry(pane, "plugin", "Codex", "failed")
        with patch.object(d.subprocess, "run", return_value=type("Result", (), {"returncode": 0, "stdout": report(), "stderr": ""})()), patch.object(d, "_read_local_agents", return_value=(plugin,)):
            self.assertEqual(d.local_snapshot().agents[0].task_state, "working")

    @patch.object(d, "load_experimental_native_codex", return_value=False)
    def test_opt_out(self, _):
        self.assertFalse(d._parse_source_snapshot(report(), kind="local").agents)
