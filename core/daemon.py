"""
Daemon module for termux-cron.

Main event loop that orchestrates scheduler, runner, storage, webhook,
and the buffered log writer.  Handles graceful shutdown on SIGTERM/SIGINT.

Optimised for Android/Termux:
    - Buffered log writes (avoid open→write→close per task run on F2FS)
    - Optional ``termux-wake-lock`` acquisition on startup
    - Optional memory-pressure check before executing tasks
    - Config file change detection (auto-reload tasks)
"""

import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.config import load, parse_interval
from core.logwriter import BufferedLogWriter
from core.runner import run_command
from core.scheduler import TaskScheduler
from core.storage import Storage
from core.webhook import post_webhook

logger = logging.getLogger(__name__)

# ── Tunables ─────────────────────────────────────────────────────────────────

LOG_RETENTION_DAYS: int = 7
"""Default log retention period in days."""

LOGS_DIR: Path = Path(os.environ.get("TERMUX_CRON_LOGS", Path.cwd() / "logs"))
"""Base directory for log files (override via TERMUX_CRON_LOGS env var)."""

CONFIG_RELOAD_INTERVAL: float = 5.0
"""Seconds between config-file mtime checks for auto-reload."""

LOG_CLEANUP_INTERVAL: float = 3600.0
"""Seconds between periodic old-log cleanup passes (1 hour)."""

CLEANUP_OUTPUTS_INTERVAL_TICKS: int = 3600
"""Tick-interval between DB output cleanup passes (~1 hour at 1s tick)."""

MEMORY_WARN_MB: int = 1024
"""Warn if MemAvailable drops below this many MB (0 = disabled)."""


# ── Signal handling ──────────────────────────────────────────────────────────


class GracefulShutdown:
    """Context manager that installs SIGTERM/SIGINT handlers.

    Sets ``shutdown_requested = True`` when a termination signal is
    received so the main loop can exit cleanly.
    """

    def __init__(self) -> None:
        self.shutdown_requested: bool = False
        self._original_sigterm = None
        self._original_sigint = None

    def _handler(self, signum: int, frame: Any) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Received %s, initiating graceful shutdown...", sig_name)
        self.shutdown_requested = True

    def __enter__(self) -> "GracefulShutdown":
        self._original_sigterm = signal.signal(signal.SIGTERM, self._handler)
        self._original_sigint = signal.signal(signal.SIGINT, self._handler)
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._original_sigterm is not None:
            signal.signal(signal.SIGTERM, self._original_sigterm)
        if self._original_sigint is not None:
            signal.signal(signal.SIGINT, self._original_sigint)


# ── Termux integration ──────────────────────────────────────────────────────


def _termux_wake_lock() -> str | None:
    """Acquire a Termux wake-lock, returning the lock name on success.

    Returns ``None`` if ``termux-wake-lock`` is not available or fails.
    This prevents the Android device from entering deep sleep (Doze)
    while the daemon is running.
    """
    if not shutil.which("termux-wake-lock"):
        logger.info("termux-wake-lock not found — skipping wake-lock acquisition")
        return None

    lock_name = "termux-cron"
    try:
        subprocess.run(
            ["termux-wake-lock", lock_name],
            check=True,
            capture_output=True,
            timeout=5,
        )
        logger.info("Acquired wake-lock: %s", lock_name)
        return lock_name
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        logger.warning("Failed to acquire wake-lock: %s", exc)
        return None


def _termux_wake_unlock(lock_name: str | None) -> None:
    """Release a previously acquired Termux wake-lock."""
    if lock_name is None:
        return
    try:
        subprocess.run(
            ["termux-wake-unlock", lock_name],
            check=True,
            capture_output=True,
            timeout=5,
        )
        logger.info("Released wake-lock: %s", lock_name)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        logger.warning("Failed to release wake-lock: %s", exc)


# ── Memory pressure check (Android-safe) ─────────────────────────────────────


def _check_memory() -> str | None:
    """Return a warning string if MemAvailable is below threshold, else None.

    Reads ``/proc/meminfo`` — accessible from Termux on most Android
    versions.  If the file cannot be read (permission denied), returns
    ``None`` so execution is not blocked.
    """
    if MEMORY_WARN_MB <= 0:
        return None
    try:
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        avail_kb = int(parts[1])
                        avail_mb = avail_kb // 1024
                        if avail_mb < MEMORY_WARN_MB:
                            return (
                                f"Low memory: MemAvailable={avail_mb} MB "
                                f"(threshold={MEMORY_WARN_MB} MB)"
                            )
                        return None
    except (PermissionError, FileNotFoundError, OSError, ValueError):
        # /proc/meminfo blocked or unreadable — skip check
        pass
    return None


# ── Log management (retention) ───────────────────────────────────────────────


def cleanup_old_logs(logs_dir: Path, retention_days: int = LOG_RETENTION_DAYS) -> int:
    """Remove ``.log`` files older than *retention_days* from *logs_dir*.

    Returns the number of files deleted.
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
        logger.info(
            "Cleaned up %d old log file(s) (older than %d days)",
            deleted, retention_days,
        )
    return deleted


# ── Config reload ────────────────────────────────────────────────────────────


def _get_config_mtime() -> float:
    """Return mtime of the tasks config, or 0 if not found."""
    from core.config import TASKS_PATH
    try:
        return TASKS_PATH.stat().st_mtime
    except OSError:
        return 0.0


def _reload_scheduler(scheduler: TaskScheduler) -> TaskScheduler:
    """Reload tasks from config; preserve existing task next-run timestamps."""
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


# ── Task execution ───────────────────────────────────────────────────────────


def _execute_task(
    task_name: str,
    task: dict,
    logs_dir: Path,
    storage: Storage,
    log_writer: BufferedLogWriter,
) -> None:
    """Execute a single task: run command → log → webhook → DB record."""
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
    except subprocess.TimeoutExpired:
        finished_at = datetime.now().isoformat(timespec="seconds")
        exit_code = -1
        output = f"Task timed out (timeout={timeout_str})"
        try:
            _started = datetime.fromisoformat(started_at)
            duration_ms = max(1, int((datetime.now() - _started).total_seconds() * 1000))
        except Exception:
            duration_ms = 0
        logger.error("Task %s timed out (%s)", task_name, timeout_str)
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

    # ── Buffered log write ──────────────────────────────────────────────
    log_writer.write(task_name, output)

    # ── Webhook ─────────────────────────────────────────────────────────
    webhook_ok = None
    webhook_url = task.get("webhook")
    if webhook_url:
        status_icon = "✅" if exit_code == 0 else "❌"
        duration_str = f"{duration_ms}ms" if duration_ms else "N/A"
        output_preview = (
            (output[:500] + "...") if output and len(output) > 500
            else (output or "N/A")
        )
        content = (
            f"{status_icon} **{task_name}**\n"
            f"```\n{output_preview}\n```\n"
            f"exit={exit_code}  duration={duration_str}"
        )
        payload = {"content": content, "username": "termux-cron", "flags": 4096}
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

    Parameters
    ----------
    logs_dir : Path
        Base directory for task logs (default: ``./logs``).
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

    # ── Termux wake-lock ────────────────────────────────────────────────
    wake_lock_name = _termux_wake_lock()

    # ── Cleanup old logs on startup ───────────────────────────────────────
    cleanup_old_logs(logs_dir)

    # ── Buffered log writer ─────────────────────────────────────────────
    log_writer = BufferedLogWriter(logs_dir)

    # ── Load tasks and initialise components ─────────────────────────────
    try:
        tasks = load()
    except ValueError as exc:
        logger.error("Failed to load tasks: %s", exc)
        _termux_wake_unlock(wake_lock_name)
        sys.exit(1)

    if not tasks:
        logger.warning("No tasks configured. Daemon will idle indefinitely.")

    scheduler = TaskScheduler(tasks)
    try:
        storage = Storage()
    except Exception as exc:
        logger.error("Failed to initialize storage: %s", exc)
        log_writer.close()
        _termux_wake_unlock(wake_lock_name)
        return

    logger.info("Loaded %d task(s): %s", len(scheduler), ", ".join(scheduler.task_names))

    # ── State for periodic operations ─────────────────────────────────────
    last_config_mtime = _get_config_mtime()
    last_config_check = time.monotonic()
    last_log_cleanup = time.monotonic()
    last_memory_check = time.monotonic()
    MEMORY_CHECK_INTERVAL: float = 60.0  # check memory every 60s
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
                    cleanup_old_logs(logs_dir)
                    last_log_cleanup = tick_start

                # ── Periodic memory check ───────────────────────────────
                if tick_start - last_memory_check >= MEMORY_CHECK_INTERVAL:
                    mem_warn = _check_memory()
                    if mem_warn:
                        logger.warning("%s", mem_warn)
                    last_memory_check = tick_start

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

                    _execute_task(task_name, task, logs_dir, storage, log_writer)

                    scheduler.mark_run(task_name, now=now)

                # ── Flush buffered logs ─────────────────────────────────
                log_writer.flush()

                # ── Sleep for the tick interval ─────────────────────────
                elapsed = time.monotonic() - tick_start
                sleep_time = max(0.0, tick_interval - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received, shutting down...")

        finally:
            logger.info("Shutting down: closing log writer...")
            log_writer.close()
            logger.info("Closing storage connection...")
            storage.close()
            _termux_wake_unlock(wake_lock_name)
            logger.info("termux-cron daemon stopped.")


if __name__ == "__main__":
    run_daemon()
