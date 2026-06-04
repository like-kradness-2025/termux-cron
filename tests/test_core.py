"""Tests for termux-cron core modules.

Self-contained — no external dependencies beyond Python stdlib + PyYAML.
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure project root is on sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.config import parse_interval, validate, _TASK_NAME_RE
from core.runner import run_command, MAX_OUTPUT_BYTES
from core.scheduler import TaskScheduler
from core.storage import Storage
from core.webhook import post_webhook


# ── parse_interval ──────────────────────────────────────────────────────────

class TestParseInterval(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(parse_interval("30s"), 30)

    def test_minutes(self):
        self.assertEqual(parse_interval("5m"), 300)

    def test_hours(self):
        self.assertEqual(parse_interval("1h"), 3600)

    def test_days(self):
        self.assertEqual(parse_interval("1d"), 86400)

    def test_invalid_no_unit(self):
        with self.assertRaises(ValueError):
            parse_interval("30")

    def test_invalid_unit(self):
        with self.assertRaises(ValueError):
            parse_interval("5x")

    def test_invalid_empty(self):
        with self.assertRaises(ValueError):
            parse_interval("")

    def test_invalid_type(self):
        with self.assertRaises(ValueError):
            parse_interval(42)  # type: ignore[arg-type]

    def test_zero_value(self):
        with self.assertRaises(ValueError):
            parse_interval("0s")

    def test_max_exceeded(self):
        with self.assertRaises(ValueError):
            parse_interval("366d")


# ── validate task names ─────────────────────────────────────────────────────

class TestValidateTaskName(unittest.TestCase):
    def test_valid_name(self):
        self.assertTrue(_TASK_NAME_RE.match("my_task-1.0"))

    def test_reject_path_traversal(self):
        self.assertIsNone(_TASK_NAME_RE.match("../etc/passwd"))

    def test_reject_slash(self):
        self.assertIsNone(_TASK_NAME_RE.match("foo/bar"))

    def test_reject_spaces(self):
        self.assertIsNone(_TASK_NAME_RE.match("my task"))

    def test_reject_empty(self):
        self.assertIsNone(_TASK_NAME_RE.match(""))

    def test_full_validate_rejects_traversal(self):
        task = {"name": "../evil", "cmd": "echo hi", "every": "1m"}
        with self.assertRaises(ValueError):
            validate(task)

    def test_full_validate_accepts_valid(self):
        task = {"name": "backup-db", "cmd": "echo ok", "every": "5m"}
        validate(task)  # should not raise


# ── config save/load round-trip ─────────────────────────────────────────────

class TestConfigRoundTrip(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._tasks_path = Path(self._tmpdir) / "tasks.yaml"
        self._lock_path = Path(self._tmpdir) / "tasks.lock"
        # Patch module-level paths
        import core.config as cfg
        self._orig_config_dir = cfg.CONFIG_DIR
        self._orig_tasks_path = cfg.TASKS_PATH
        self._orig_lock_file = cfg._LOCK_FILE
        cfg.CONFIG_DIR = Path(self._tmpdir)
        cfg.TASKS_PATH = self._tasks_path
        cfg._LOCK_FILE = self._lock_path

    def tearDown(self):
        import core.config as cfg
        cfg.CONFIG_DIR = self._orig_config_dir
        cfg.TASKS_PATH = self._orig_tasks_path
        cfg._LOCK_FILE = self._orig_lock_file
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_save_load_roundtrip(self):
        import core.config as cfg
        tasks = [
            {"name": "t1", "cmd": "echo 1", "every": "10s", "enabled": True},
            {"name": "t2", "cmd": "echo 2", "every": "1m"},
        ]
        cfg.save(tasks)
        loaded = cfg.load()
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["name"], "t1")
        self.assertEqual(loaded[1]["name"], "t2")

    def test_load_empty(self):
        import core.config as cfg
        result = cfg.load()
        self.assertEqual(result, [])


# ── runner output cap ───────────────────────────────────────────────────────

class TestRunnerOutputCap(unittest.TestCase):
    def test_output_truncated(self):
        # Generate more than 64KB of output
        result = run_command("yes | head -n 20000", timeout=10)
        self.assertEqual(result["exit_code"], 0)
        # Output should be capped at MAX_OUTPUT_BYTES + truncation message
        self.assertLessEqual(len(result["output"].encode("utf-8")),
                             MAX_OUTPUT_BYTES + 100)

    def test_small_output_not_truncated(self):
        result = run_command("echo hello", timeout=5)
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("hello", result["output"])


# ── runner timeout ──────────────────────────────────────────────────────────

class TestRunnerTimeout(unittest.TestCase):
    def test_timeout_raises(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_command("sleep 60", timeout=1)


# ── webhook post failure ────────────────────────────────────────────────────

class TestWebhookFailure(unittest.TestCase):
    def test_invalid_url(self):
        result = post_webhook("", {"task": "t"})
        self.assertFalse(result)

    def test_unreachable_url(self):
        # Use a URL that will fail to connect
        result = post_webhook("http://127.0.0.1:1/nope", {"task": "t"})
        self.assertFalse(result)

    def test_none_url(self):
        result = post_webhook(None, {"task": "t"})  # type: ignore[arg-type]
        self.assertFalse(result)


# ── Storage ─────────────────────────────────────────────────────────────────

class TestStorage(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self._tmpdir) / "test.db"
        self.storage = Storage(db_path=self.db_path)

    def tearDown(self):
        self.storage.close()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_record_run_and_get_history(self):
        row_id = self.storage.record_run(
            task_name="t1",
            started_at="2026-01-01T00:00:00",
            finished_at="2026-01-01T00:00:01",
            exit_code=0,
            duration_ms=1000,
            output="ok",
            webhook_ok=1,
        )
        self.assertIsNotNone(row_id)
        self.assertIsInstance(row_id, int)

        history = self.storage.get_history("t1")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["task_name"], "t1")
        self.assertEqual(history[0]["exit_code"], 0)

    def test_get_history_empty(self):
        history = self.storage.get_history("nonexistent")
        self.assertEqual(history, [])

    def test_cleanup_old_outputs(self):
        # Insert 5 runs for same task
        for i in range(5):
            self.storage.record_run(
                task_name="t1",
                started_at=f"2026-01-0{i+1}T00:00:00",
                output=f"output-{i}",
            )
        # Keep only 2 most recent
        modified = self.storage.cleanup_old_outputs(keep_recent=2)
        self.assertGreater(modified, 0)

        history = self.storage.get_history("t1", limit=10)
        # Most recent 2 should have output, older should be None
        outputs = [h["output"] for h in history]
        self.assertIsNotNone(outputs[0])
        self.assertIsNotNone(outputs[1])
        self.assertIsNone(outputs[2])

    def test_record_run_returns_none_on_db_error(self):
        # Close connection to force an error
        self.storage._conn.close()
        result = self.storage.record_run(
            task_name="t1",
            started_at="2026-01-01T00:00:00",
        )
        self.assertIsNone(result)


# ── Scheduler ───────────────────────────────────────────────────────────────

class TestScheduler(unittest.TestCase):
    def _make_tasks(self):
        return [
            {"name": "fast", "cmd": "echo 1", "every": "10s", "enabled": True},
            {"name": "slow", "cmd": "echo 2", "every": "1h", "enabled": True},
            {"name": "disabled", "cmd": "echo 3", "every": "1m", "enabled": False},
        ]

    def test_is_due_initially(self):
        now = 1000.0
        sched = TaskScheduler(self._make_tasks(), now=now)
        self.assertTrue(sched.is_due("fast", now=now))
        self.assertTrue(sched.is_due("slow", now=now))
        # Disabled task is never due
        self.assertFalse(sched.is_due("disabled", now=now))

    def test_is_due_after_mark(self):
        now = 1000.0
        sched = TaskScheduler(self._make_tasks(), now=now)
        sched.mark_run("fast", now=now)
        # fast interval is 10s, so at now+5 it should NOT be due
        self.assertFalse(sched.is_due("fast", now=now + 5))
        # at now+10 it should be due again
        self.assertTrue(sched.is_due("fast", now=now + 10))

    def test_mark_run_unknown_task(self):
        sched = TaskScheduler(self._make_tasks(), now=1000.0)
        with self.assertRaises(KeyError):
            sched.mark_run("nonexistent")

    def test_unknown_task_not_due(self):
        sched = TaskScheduler(self._make_tasks(), now=1000.0)
        self.assertFalse(sched.is_due("nonexistent"))

    def test_len_excludes_disabled(self):
        sched = TaskScheduler(self._make_tasks(), now=1000.0)
        self.assertEqual(len(sched), 2)


if __name__ == "__main__":
    unittest.main()
