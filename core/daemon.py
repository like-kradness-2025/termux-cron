"""Daemon module for termux-cron.

Main event loop that orchestrates scheduler, runner, storage, and webhook
modules. Handles graceful shutdown on SIGTERM/SIGINT and log rotation cleanup.

Tasks are executed **sequentially** (one at a time) per the SPEC.
"""

import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.config import load, parse_interval
from core.runner import run_command
from core.scheduler import TaskScheduler
from core.storage import Storage
from core.webhook import post_webhook

logger = logging.getLogger(__name__)

#: Default log retention period in days.
LOG_RETENTION_DAYS: int = 7

#: Base directory for log files (can be overridden via TERMUX_CRON_LOGS env var).
LOGS_DIR: Path = Path(os.environ.get("TERMUX_CRON_LOGS", Path.cwd() / "logs"))

#: How often (seconds) to check for config file changes and reload tasks.
CONFIG_RELOAD_INTERVAL: float = 5.0

#: How often (seconds) to run periodic log cleanup.
LOG_CLEANUP_INTERVAL: float = 3600.0  # 1 hour

#: How often (ticks) to call cleanup_old_outputs on the storage DB.
CLEANUP_OUTPUTS_INTERVAL_TICKS: int = 3600  # ~1 hour at 1s tick


# ── Signal handling ──────────────────────────────────────────────────────────


class GracefulShutdown:
    """Context manager for graceful shutdown on SIGTERM/SIGINT.

    Sets a flag when a termination signal is received, allowing the main
    loop to exit cleanly after completing the current tick.
    """

    def __init__(self) -> None:
        self.shutdown_requested: bool = False
        self._original_sigterm = None
        self._original_sigint = None

    def _handler(self, signum: int, frame: Any) -> None:
        """Signal handler that sets the shutdown flag."""
        sig_name = signal.Signals(signum).name
        logger.info("Received %s, initiating graceful shutdown...", sig_name)
        self.shutdown_requested = True

    def __enter__(self) -> "GracefulShutdown":
        """Install signal handlers."""
        self._original_sigterm = signal.signal(signal.SIGTERM, self._handler)
        self._original_sigint = signal.signal(signal.SIGINT, self._handler)
        return self

    def __exit__(self, *exc_info: Any) -> None:
        """Restore original signal handlers."""
        if self._original_sigterm is not None:
            signal.signal(signal.SIGTERM, self._original_sigterm)
        if self._original_sigint is not None:
            signal.signal(signal.SIGINT, self._original_sigint)


# ── Log management ───────────────────────────────────────────────────────────


def cleanup_old_logs(logs_dir: Path, retention_days: int = LOG_RETENTION_DAYS) -> int:
    """Remove log files older than *retention_days*.

    Scans the logs directory recursively for .log files and deletes those
    whose modification time is older than the retention threshold.

    Parameters
    ----------
    logs_dir : Path
        Base directory containing task log subdirectories.
    retention_days : int
        Number of days to retain log files (default 7).

    Returns
    -------
    int
        Number of files deleted.
    """
    if not logs_dir.exists():
        return 0

    cutoff = datetime.now() - timedelta(days=retention_days)
    cutoff_ts = cutoff.timestamp()
    deleted = 0

    for log_file in logs_dir.rglob("*.log"):
        try:
            mtime = log_file.stat().st_mtime
            if mtime < cutoff_ts:
                log_file.unlink()
                deleted += 1
                logger.debug("Deleted old log: %s", log_file)
        except OSError as exc:
            logger.warning("Failed to delete %s: %s", log_file, exc)

    if deleted > 0:
        logger.info("Cleaned up %d old log file(s) (older than %d days)", deleted, retention_days)

    return deleted


def get_log_path(task_name: str, logs_dir: Path = LOGS_DIR) -> Path:
    """Return the log file path for a task on the current date.

    Parameters
    ----------
    task_name : str
        Name of the task.
    logs_dir : Path
        Base logs directory (default: ./logs).

    Returns
    -------
    Path
        Path to the log file (e.g., logs/my-task/2026-06-04.log).
    """
    date_str = datetime.now().strftime("%Y-%m-%d")
    task_dir = logs_dir / task_name
    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir / f"{date_str}.log"


def append_to_log(task_name: str, output: str, logs_dir: Path = LOGS_DIR) -> None:
    """Append task output to its daily log file.

    Parameters
    ----------
    task_name : str
        Name of the task.
    output : str
        Text to append (stdout + stderr from the command).
    logs_dir : Path
        Base logs directory (default: ./logs).
    """
    if not output:
        return

    log_path = get_log_path(task_name, logs_dir)
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            timestamp = datetime.now().isoformat(timespec="seconds")
            fh.write(f"[{timestamp}]\n{output}\n")
    except OSError as exc:
        logger.error("Failed to write log for %s: %s", task_name, exc)


def _purge_old_logs(logs_dir: Path = LOGS_DIR) -> None:
    """Purge log files older than LOG_RETENTION_DAYS."""
    deleted = cleanup_old_logs(logs_dir, LOG_RETENTION_DAYS)
    if deleted > 0:
        logger.info("Log cleanup: removed %d old log file(s)", deleted)


def _get_config_mtime() -> float:
    """Return the mtime of the tasks config file, or 0 if not found."""
    from core.config import TASKS_PATH
    try:
        return TASKS_PATH.stat().st_mtime
    except OSError:
        return 0.0


def _reload_scheduler(scheduler: TaskScheduler) -> TaskScheduler:
    """Reload tasks from config and return a new scheduler.

    Preserves next_run timestamps for tasks that still exist so we
    don't re-fire them immediately after a reload.
    """
    try:
        tasks = load()
    except ValueError as exc:
        logger.error("Config reload failed: %s", exc)
        return scheduler

    new_scheduler = TaskScheduler(tasks)

    for name in new_scheduler.task_names:
        old_next = scheduler.get_next_run(name)
        if old_next is not None:
            new_scheduler._next_run[name] = old_next

    logger.info(
        "Config reloaded: %d task(s): %s",
        len(new_scheduler),
        ", ".join(new_scheduler.task_names),
    )
    return new_scheduler


def _execute_task(
    task_name: str,
    task: dict,
    logs_dir: Path,
    storage: Storage,
) -> None:
    """Execute a single task synchronously: run command, log, record, webhook."""
    started_at = datetime.now().isoformat(timespec="seconds")
    logger.info("Running task: %s", task_name)

    cmd = task["cmd"]
    timeout_str = task.get("timeout")
    timeout_sec = parse_interval(timeout_str) if timeout_str else None
    cwd = task.get("cwd")

    try:
        result = run_command(cmd, timeout=timeout_sec, cwd=cwd)
        exit_code = result["exit_code"]
        output = result["output"]
        duration_ms = result["duration_ms"]
        finished_at = datetime.now().isoformat(timespec="seconds")

        logger.info(
            "Task %s completed: exit=%d, duration=%dms",
            task_name,
            exit_code,
            duration_ms,
        )

    except Exception as exc:
        finished_at = datetime.now().isoformat(timespec="seconds")
        exit_code = -1
        output = f"Task failed with exception: {exc}"
        try:
            _started = datetime.fromisoformat(started_at)
            duration_ms = max(1, int((datetime.now() - _started).total_seconds() * 1000))
        except Exception:
            duration_ms = 0
        logger.error("Task %s failed: %s", task_name, exc)

    # ── Write to log file ───────────────────────────────────────────────
    append_to_log(task_name, output, logs_dir)

    # ── Webhook ─────────────────────────────────────────────────────────
    webhook_ok = None
    webhook_url = task.get("webhook")
    if webhook_url:
        payload = {
            "task": task_name,
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "output": output,
        }
        try:
            ok = post_webhook(webhook_url, payload)
            webhook_ok = 1 if ok else 0
            if ok:
                logger.info("Webhook sent for %s", task_name)
            else:
                logger.warning("Webhook failed for %s", task_name)
        except Exception:
            logger.exception("webhook POST failed for task %s", task_name)
            webhook_ok = 0

    # ── Record in SQLite ────────────────────────────────────────────────
    storage.record_run(
        task_name=task_name,
        started_at=started_at,
        finished_at=finished_at,
        exit_code=exit_code,
        duration_ms=duration_ms,
        output=output,
        webhook_ok=webhook_ok,
    )


# ── Main daemon loop ─────────────────────────────────────────────────────────


def run_daemon(
    logs_dir: Path = LOGS_DIR,
    tick_interval: float = 1.0,
) -> None:
    """Run the termux-cron daemon main loop.

    Loads tasks from config, initialises the scheduler and storage backend,
    then enters a single-threaded loop that checks for due tasks, executes
    them one at a time (sequentially), records results, and sends webhooks.

    The config file is monitored for changes and tasks are reloaded
    automatically (so CLI add/remove/enable/disable take effect while the
    daemon is running).

    Old logs are cleaned up on startup and periodically (hourly).
    Old DB outputs are also cleaned up periodically via
    ``Storage.cleanup_old_outputs``.

    Exits gracefully on SIGTERM/SIGINT.

    Parameters
    ----------
    logs_dir : Path
        Base directory for task logs (default: ./logs).
    tick_interval : float
        Seconds to sleep between ticks (default: 1.0).
    """
    # ── Setup logging ─────────────────────────────────────────────────────
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger.info("termux-cron daemon starting...")

    # ── Cleanup old logs on startup ───────────────────────────────────────
    _purge_old_logs(logs_dir)

    # ── Load tasks and initialise components ─────────────────────────────
    try:
        tasks = load()
    except ValueError as exc:
        logger.error("Failed to load tasks: %s", exc)
        sys.exit(1)

    if not tasks:
        logger.warning("No tasks configured. Daemon will idle indefinitely.")

    scheduler = TaskScheduler(tasks)
    storage = Storage()

    logger.info("Loaded %d task(s): %s", len(scheduler), ", ".join(scheduler.task_names))

    # Track config file mtime for change detection
    last_config_mtime = _get_config_mtime()
    last_config_check = time.monotonic()

    # Track last log cleanup time for periodic cleanup
    last_log_cleanup = time.monotonic()

    # Tick counter for periodic cleanup_old_outputs
    tick_count = 0

    # ── Graceful shutdown context ─────────────────────────────────────────
    with GracefulShutdown() as shutdown:
        logger.info("Daemon running. Press Ctrl+C to stop.")

        try:
            while not shutdown.shutdown_requested:
                tick_start = time.monotonic()
                tick_count += 1

                # ── Periodic config reload ──────────────────────────────
                if tick_start - last_config_check >= CONFIG_RELOAD_INTERVAL:
                    current_mtime = _get_config_mtime()
                    if current_mtime != last_config_mtime and current_mtime > 0:
                        scheduler = _reload_scheduler(scheduler)
                        last_config_mtime = current_mtime
                    last_config_check = tick_start

                # ── Periodic log cleanup ────────────────────────────────
                if tick_start - last_log_cleanup >= LOG_CLEANUP_INTERVAL:
                    _purge_old_logs(logs_dir)
                    last_log_cleanup = tick_start

                # ── Periodic DB output cleanup ──────────────────────────
                if tick_count >= CLEANUP_OUTPUTS_INTERVAL_TICKS:
                    try:
                        n = storage.cleanup_old_outputs(keep_recent=100)
                        if n > 0:
                            logger.info(
                                "DB cleanup: nulled output for %d old row(s)", n
                            )
                    except Exception:
                        logger.exception("cleanup_old_outputs failed")
                    tick_count = 0

                # ── Check each task (sequential execution) ──────────────
                for task_name in list(scheduler.task_names):
                    if shutdown.shutdown_requested:
                        break

                    task = scheduler.get_task(task_name)
                    if task is None:
                        continue

                    now = time.time()
                    if not scheduler.is_due(task_name, now=now):
                        continue

                    # Execute synchronously — one task at a time
                    _execute_task(task_name, task, logs_dir, storage)

                    # Advance scheduler after successful execution
                    scheduler.mark_run(task_name, now=now)

                # ── Sleep for the tick interval ─────────────────────────
                elapsed = time.monotonic() - tick_start
                sleep_time = max(0.0, tick_interval - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received, shutting down...")

        finally:
            logger.info("Closing storage connection...")
            storage.close()
            logger.info("termux-cron daemon stopped.")


# ── Entry point for testing ──────────────────────────────────────────────────

if __name__ == "__main__":
    run_daemon()
