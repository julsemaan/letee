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
        self.assertEqual(len(records), 100)
        self.assertEqual({record["value"] for record in records}, set(range(100)))
        self.assertEqual(len({record["event_id"] for record in records}), 100)

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
