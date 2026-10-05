import json
import os
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from letee.diagnostics import Diagnostics


class DiagnosticsTest(unittest.TestCase):
    def test_logging_is_inert_without_environment_path(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(os.environ, {}, clear=True):
            diagnostics = Diagnostics()
            self.addCleanup(diagnostics.close)
            diagnostics.emit("ignored", value=1)
            self.assertIsNone(diagnostics.new_ssh_log("ssh:dev:work", "session"))
            diagnostics.flush()

            self.assertFalse(list(Path(tempdir).iterdir()))
            self.assertFalse(diagnostics.enabled)

    def test_enabled_logging_writes_jsonl_with_metadata(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            os.environ, {"LETEE_DEBUG_LOG": str(Path(tempdir) / "trace.jsonl")}, clear=True
        ):
            diagnostics = Diagnostics(server="work")
            diagnostics.emit("test_event", value=1)
            diagnostics.close()

            records = [json.loads(line) for line in Path(tempdir, "trace.jsonl").read_text().splitlines()]

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["event"], "test_event")
        self.assertEqual(records[0]["server"], "work")
        self.assertEqual(records[0]["value"], 1)
        self.assertTrue(records[0]["timestamp_utc"])
        self.assertIsInstance(records[0]["monotonic_ns"], int)
        self.assertTrue(records[0]["run_id"])
        self.assertTrue(records[0]["event_id"])
        self.assertEqual(records[0]["pid"], os.getpid())
        self.assertTrue(records[0]["thread"])

    def test_concurrent_events_and_shutdown_flush_are_complete(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            os.environ, {"LETEE_DEBUG_LOG": str(Path(tempdir) / "trace.jsonl")}, clear=True
        ):
            diagnostics = Diagnostics()
            with ThreadPoolExecutor(max_workers=4) as workers:
                list(workers.map(lambda value: diagnostics.emit("worker", value=value), range(100)))
            diagnostics.close()

            lines = Path(tempdir, "trace.jsonl").read_text().splitlines()

        records = [json.loads(line) for line in lines]
        required = {
            "timestamp_utc", "monotonic_ns", "run_id", "event_id",
            "pid", "thread", "server", "event",
        }
        self.assertEqual(len(records), 100)
        self.assertTrue(all(required <= record.keys() for record in records))
        self.assertEqual({record["value"] for record in records}, set(range(100)))
        self.assertEqual(len({record["event_id"] for record in records}), 100)

    def test_ssh_logs_are_unique_private_and_linked_to_the_action(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            os.environ, {"LETEE_DEBUG_LOG": str(Path(tempdir) / "trace.jsonl")}, clear=True
        ):
            diagnostics = Diagnostics()
            with diagnostics.context(action_id="action-85"):
                first = diagnostics.new_ssh_log("ssh:dev:work", "session")
                second = diagnostics.new_ssh_log("ssh:dev:work", "agent_pane")
            diagnostics.close()

            first_path = Path(first)
            second_exists = Path(second).is_file()
            records = [json.loads(line) for line in Path(tempdir, "trace.jsonl").read_text().splitlines()]
            first_mode = stat.S_IMODE(first_path.stat().st_mode)
            first_dir_mode = stat.S_IMODE(first_path.parent.stat().st_mode)

        self.assertNotEqual(first, second)
        self.assertTrue(second_exists)
        self.assertEqual(first_path.name, "ssh.log")
        self.assertEqual(first_mode, stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual(first_dir_mode, stat.S_IRWXU)
        self.assertEqual([record["ssh_log_path"] for record in records], [first, second])
        self.assertEqual([record["action_id"] for record in records], ["action-85", "action-85"])
        self.assertEqual([record["attach_type"] for record in records], ["session", "agent_pane"])

    def test_log_file_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            os.environ, {"LETEE_DEBUG_LOG": str(Path(tempdir) / "trace.jsonl")}, clear=True
        ):
            diagnostics = Diagnostics()
            diagnostics.emit("test_event")
            diagnostics.close()

            mode = stat.S_IMODE(os.stat(Path(tempdir, "trace.jsonl")).st_mode)

        self.assertEqual(mode, stat.S_IRUSR | stat.S_IWUSR)


if __name__ == "__main__":
    unittest.main()
