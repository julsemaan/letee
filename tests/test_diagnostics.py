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

    def test_empty_environment_path_reuses_disabled_singleton(self):
        from letee import diagnostics as module

        with patch.dict(os.environ, {"LETEE_DEBUG_LOG": ""}), patch.object(module, "_DEFAULT", None):
            logger = module.get_diagnostics()
            self.addCleanup(logger.close)

            self.assertFalse(logger.enabled)
            self.assertIs(module.get_diagnostics(), logger)
            self.assertIs(module.get_diagnostics(), logger)

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

    def test_operation_captures_failure_without_sensitive_messages(self):
        from letee import diagnostics as module

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            logger = Diagnostics(path)
            with patch.object(module, "get_diagnostics", return_value=logger):
                with self.assertRaises(ValueError):
                    with module.operation("outer"):
                        with module.operation("inner"):
                            raise ValueError("SECRET exception and terminal output")
            logger.close()
            content = path.read_text()
            records = [json.loads(line) for line in content.splitlines()]
        self.assertNotIn("SECRET", content)
        errors = [record for record in records if record["event"] == "error"]
        self.assertEqual(len({record["error_id"] for record in errors}), 1)
        starts = [record for record in records if record["event"] == "operation_start"]
        self.assertEqual(starts[1]["parent_operation_id"], starts[0]["operation_id"])
        self.assertEqual(records[-1]["outcome"], "failed")

    def test_expected_exit_and_future_cancellation_are_not_failed_operations(self):
        from concurrent.futures import CancelledError
        from letee import diagnostics as module

        with tempfile.TemporaryDirectory() as directory:
            logger = Diagnostics(Path(directory) / "trace.jsonl")
            with patch.object(module, "get_diagnostics", return_value=logger):
                for exception in (SystemExit(0), CancelledError("SECRET cancellation")):
                    with self.assertRaises(type(exception)) as raised:
                        with module.operation("expected"):
                            raise exception
                    self.assertIs(raised.exception, exception)
            logger.close()
            content = Path(directory, "trace.jsonl").read_text()
            records = [json.loads(line) for line in content.splitlines()]
        self.assertEqual([r["outcome"] for r in records if r["event"] == "operation_end"], ["completed", "cancelled"])
        self.assertEqual([r["handling"] for r in records if r["event"] == "error"], ["cancel"])
        self.assertNotIn("SECRET", content)

    def test_sink_failures_warn_once_and_do_not_raise(self):
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as directory:
            logger = Diagnostics(Path(directory) / "trace.jsonl")
            original = logger._file
            logger._file = Mock()
            logger._file.write.side_effect = OSError("SECRET disk failure")
            with patch("sys.stderr") as stderr:
                logger.emit("first")
                logger.emit("second")
                logger.flush()
                logger.close()
            original.close()
        self.assertEqual(stderr.write.call_count, 1)
        self.assertNotIn("SECRET", str(stderr.write.call_args))

    def test_initialization_failure_warns_without_recursion(self):
        with patch("os.open", side_effect=OSError("SECRET")), patch("sys.stderr") as stderr:
            logger = Diagnostics("/tmp/letee-unavailable-test.jsonl")
            logger.emit("ignored")
            logger.close()
        self.assertFalse(logger.enabled)
        self.assertEqual(stderr.write.call_count, 1)
        self.assertNotIn("SECRET", str(stderr.write.call_args))

    def test_disabled_operation_does_not_capture_or_format_exceptions(self):
        from letee import diagnostics as module

        logger = Diagnostics("")
        with patch.object(module, "get_diagnostics", return_value=logger), patch.object(module, "error") as capture:
            with self.assertRaises(ValueError):
                with module.operation("disabled"):
                    raise ValueError("secret")
        capture.assert_not_called()

    def test_decorator_is_reusable_and_context_is_restored(self):
        from letee import diagnostics as module

        with tempfile.TemporaryDirectory() as directory:
            logger = Diagnostics(Path(directory) / "trace.jsonl")
            with patch.object(module, "get_diagnostics", return_value=logger):
                @module.operation("decorated")
                def execute():
                    module.log("inside")
                execute()
                execute()
            self.assertEqual(logger.context_values(), {})
            logger.close()
            records = [json.loads(line) for line in Path(directory, "trace.jsonl").read_text().splitlines()]
        self.assertEqual(len({item["operation_id"] for item in records if item["event"] == "inside"}), 2)

    def test_serialization_flush_and_close_failures_are_safe(self):
        from unittest.mock import Mock

        for stage in ("serialization", "flush", "shutdown"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                logger = Diagnostics(Path(directory) / "trace.jsonl")
                original = logger._file
                if stage != "serialization":
                    logger._file = Mock()
                    getattr(logger._file, "close" if stage == "shutdown" else "flush").side_effect = OSError("SECRET")
                with patch("sys.stderr") as stderr:
                    logger.emit("test", value=object() if stage == "serialization" else 1)
                    logger.flush()
                    logger.close()
                original.close()
                self.assertEqual(stderr.write.call_count, 1)
                self.assertNotIn("SECRET", str(stderr.write.call_args))
                self.assertIn(stage, str(stderr.write.call_args))

    def test_thread_start_and_unresolvable_path_failures_are_safe(self):
        with tempfile.TemporaryDirectory() as directory, patch("threading.Thread.start", side_effect=RuntimeError("SECRET")), patch("sys.stderr") as stderr:
            logger = Diagnostics(Path(directory) / "trace.jsonl")
            logger.flush()
            logger.close()
            self.assertFalse(logger.enabled)
            self.assertEqual(stderr.write.call_count, 1)
        with patch.object(Path, "expanduser", side_effect=RuntimeError("SECRET")), patch("sys.stderr") as stderr:
            logger = Diagnostics("~missing/trace.jsonl")
            logger.close()
            self.assertFalse(logger.enabled)
            self.assertEqual(stderr.write.call_count, 1)

    def test_error_chain_ids_link_translation_without_messages_or_source(self):
        from letee import diagnostics as module

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            logger = Diagnostics(path)
            with patch.object(module, "get_diagnostics", return_value=logger):
                try:
                    try:
                        raise OSError(13, "SECRET cause", "/SECRET/path")
                    except OSError as exc:
                        module.error(exc, handling="fallback")
                        raise SystemExit("SECRET translation") from None
                except SystemExit as exc:
                    module.error(exc, handling="propagate")
            logger.close()
            content = path.read_text()
            first, second = [json.loads(line) for line in content.splitlines()]
        self.assertNotIn("SECRET", content)
        self.assertEqual(first["errno"], 13)
        self.assertEqual(first["error_id"], second["causes"][1]["error_id"])
        self.assertNotEqual(first["error_id"], second["error_id"])
        self.assertTrue(first["file"])
        self.assertTrue(first["function"])
        self.assertGreater(first["line"], 0)

    def test_slow_sink_bounds_flush_shutdown_and_queue_growth(self):
        import threading
        from letee import diagnostics as module
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(module, "_QUEUE_LIMIT", 2):
                logger = Diagnostics(Path(directory) / "trace.jsonl")
            original = logger._file
            entered, release = threading.Event(), threading.Event()
            sink = Mock()
            def block_write(line):
                entered.set()
                release.wait(2)
            sink.write.side_effect = block_write
            logger._file = sink
            with patch("sys.stderr") as stderr, patch.object(module, "_DRAIN_TIMEOUT", 0.01):
                logger.emit("blocked")
                self.assertTrue(entered.wait(1))
                for _ in range(20):
                    logger.emit("queued")
                self.assertLessEqual(logger._queue.qsize(), 2)
                self.assertGreater(logger._dropped, 0)
                logger.flush()
                logger.close()
                self.assertTrue(logger._writer.is_alive())
                self.assertEqual(stderr.write.call_count, 1)
                release.set()
                logger._writer.join(2)
            original.close()
            self.assertFalse(logger._writer.is_alive())

    def test_unexpected_thread_capture_delegates_to_original_hook(self):
        from types import SimpleNamespace
        from letee import diagnostics as module
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as directory:
            logger = Diagnostics(Path(directory) / "trace.jsonl")
            previous = Mock()
            with patch.object(module, "_DEFAULT", logger), patch.object(module, "get_diagnostics", return_value=logger), patch.object(module, "_THREAD_CAPTURE_INSTALLED", False), patch("threading.excepthook", previous):
                module._install_thread_capture()
                args = SimpleNamespace(exc_value=RuntimeError("SECRET"), thread=None)
                module.threading.excepthook(args)
                previous.assert_called_once_with(args)
            logger.close()
            content = Path(directory, "trace.jsonl").read_text()
        self.assertNotIn("SECRET", content)
        self.assertEqual(json.loads(content)["operation"], "thread")

    def test_setup_failures_close_descriptor_and_warn_without_details(self):
        for stage in ("fchmod", "fdopen"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                with patch(f"os.{stage}", side_effect=OSError("SECRET")), patch("os.close", wraps=os.close) as close_fd, patch("sys.stderr") as stderr:
                    logger = Diagnostics(Path(directory) / "trace.jsonl")
                    logger.close()
                self.assertFalse(logger.enabled)
                self.assertEqual(close_fd.call_count, 1)
                self.assertEqual(stderr.write.call_count, 1)
                self.assertNotIn("SECRET", str(stderr.write.call_args))

    def test_recording_and_error_capture_failures_never_replace_application_error(self):
        from letee import diagnostics as module

        with tempfile.TemporaryDirectory() as directory:
            logger = Diagnostics(Path(directory) / "trace.jsonl")
            with patch.object(module, "_DEFAULT", logger), patch.object(module, "get_diagnostics", return_value=logger), patch.object(logger, "emit", side_effect=OSError("SECRET sink")), patch("sys.stderr") as stderr:
                original = ValueError("SECRET application")
                with self.assertRaises(ValueError) as raised:
                    with module.operation("failure"):
                        raise original
                self.assertIs(raised.exception, original)
                self.assertEqual(stderr.write.call_count, 1)
                self.assertNotIn("SECRET", str(stderr.write.call_args))
            logger.close()
        with tempfile.TemporaryDirectory() as directory:
            logger = Diagnostics(Path(directory) / "trace.jsonl")
            with patch.object(module, "_DEFAULT", logger), patch.object(module, "get_diagnostics", return_value=logger), patch.object(logger, "error_id", side_effect=RuntimeError("SECRET capture")), patch("sys.stderr") as stderr:
                self.assertIsNone(module.error(ValueError("SECRET")))
                self.assertEqual(stderr.write.call_count, 1)
                self.assertNotIn("SECRET", str(stderr.write.call_args))
            logger.close()

    def test_startup_cleanup_failure_warns_once_and_leaves_logging_disabled(self):
        from unittest.mock import Mock

        sink = Mock()
        sink.close.side_effect = OSError("SECRET cleanup")
        with patch("os.open", return_value=123), patch("os.fchmod"), patch("os.fdopen", return_value=sink), patch("threading.Thread.start", side_effect=RuntimeError("SECRET thread")), patch("sys.stderr") as stderr:
            logger = Diagnostics("/tmp/letee-test.jsonl")
            logger.close()
        self.assertFalse(logger.enabled)
        sink.close.assert_called_once_with()
        self.assertEqual(stderr.write.call_count, 1)
        self.assertNotIn("SECRET", str(stderr.write.call_args))

    def test_writer_empty_queue_wait_is_expected_and_does_not_warn(self):
        import queue
        from unittest.mock import Mock

        logger = Diagnostics("")
        logger._queue = Mock()
        logger._queue.get.side_effect = [queue.Empty(), None]
        with patch("sys.stderr") as stderr:
            logger._write_loop()
        logger._queue.task_done.assert_called_once_with()
        stderr.write.assert_not_called()

    def test_exception_reference_eviction_is_bounded_and_close_releases_references(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = Diagnostics(Path(directory) / "trace.jsonl")
            original = ValueError("SECRET")
            first_id = logger.error_id(original)
            for _ in range(64):
                logger.error_id(ValueError("SECRET"))
            self.assertEqual(len(logger._errors), 64)
            self.assertNotEqual(logger.error_id(original), first_id)
            logger.close()
            self.assertFalse(logger._errors)

    def test_warning_failure_does_not_escape(self):
        with patch("os.open", side_effect=OSError("SECRET")), patch("sys.stderr.write", side_effect=OSError("SECRET")):
            logger = Diagnostics("/tmp/unavailable-letee.jsonl")
            logger.close()

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
