#!/usr/bin/env python3
"""termux-cron CLI entrypoint.

Provides commands for managing scheduled tasks and running the daemon.
"""

import argparse
import fcntl
import os
import signal
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Ensure the project root is on sys.path so `core` is importable
# regardless of how the script is invoked.
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.config import CONFIG_DIR, TASKS_PATH, load, parse_interval, save, validate
from core.daemon import LOGS_DIR, run_daemon
from core.storage import Storage

# ── PID file management ──────────────────────────────────────────────────────

_PID_DIR = CONFIG_DIR
_PID_FILE = _PID_DIR / "daemon.pid"


def _write_pid() -> int:
    """Write the current process PID to the PID file with an exclusive lock.

    Returns the opened file descriptor (held until daemon exit) so the lock
    is released on process exit even without explicit cleanup.
    """
    _PID_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(_PID_FILE), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        os.close(fd)
        raise RuntimeError("daemon is already running (PID file locked)")
    # Truncate and write our PID
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    os.fsync(fd)
    return fd


_LOCK_FD: int | None = None


def _release_lock() -> None:
    """Release the PID file lock and remove the file."""
    global _LOCK_FD
    if _LOCK_FD is not None:
        try:
            os.close(_LOCK_FD)
        except OSError:
            pass
        _PID_FILE.unlink(missing_ok=True)
        _LOCK_FD = None


def _read_pid() -> int | None:
    """Read the PID from the PID file, or None if absent/unreadable."""
    if not _PID_FILE.exists():
        return None
    try:
        return int(_PID_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _remove_pid() -> None:
    """Remove the PID file if it exists."""
    _PID_FILE.unlink(missing_ok=True)


def _is_pid_alive(pid: int) -> bool:
    """Check whether a process with the given PID is running."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it
        return True
    return True


# ── Command handlers ─────────────────────────────────────────────────────────


def cmd_daemon(args: argparse.Namespace) -> int:
    """Start the daemon in the foreground."""
    global _LOCK_FD
    try:
        _LOCK_FD = _write_pid()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        logs_dir = Path(args.logs_dir) if args.logs_dir else LOGS_DIR
        run_daemon(logs_dir=logs_dir)
    finally:
        _release_lock()
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    """Add a new task to the config."""
    tasks = load()

    # Check for duplicate name
    for task in tasks:
        if task["name"] == args.name:
            print(f"Error: task {args.name!r} already exists", file=sys.stderr)
            return 1

    # Build the task dict
    new_task: dict = {
        "name": args.name,
        "cmd": args.cmd,
        "every": args.every,
    }
    if args.webhook is not None:
        new_task["webhook"] = args.webhook
    if args.timeout is not None:
        new_task["timeout"] = args.timeout
    if args.cwd is not None:
        new_task["cwd"] = args.cwd

    # Validate before saving
    try:
        validate(new_task)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    tasks.append(new_task)
    try:
        save(tasks)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Added task {args.name!r}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    """Remove a task from the config."""
    tasks = load()
    original_count = len(tasks)
    tasks = [t for t in tasks if t["name"] != args.name]

    if len(tasks) == original_count:
        print(f"Error: task {args.name!r} not found", file=sys.stderr)
        return 1

    try:
        save(tasks)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Removed task {args.name!r}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """List all configured tasks."""
    tasks = load()

    if not tasks:
        print("No tasks configured.")
        return 0

    # Table header
    name_w = max(len(t["name"]) for t in tasks)
    name_w = max(name_w, 4)  # minimum width for "NAME"
    header = f"{'NAME':<{name_w}}  {'EVERY':<8}  {'ENABLED':<8}  {'WEBHOOK':<5}  {'TIMEOUT':<8}  CMD"
    print(header)
    print("-" * len(header))

    for task in tasks:
        name = task["name"]
        every = task["every"]
        enabled = "yes" if task.get("enabled", True) else "no"
        webhook = "yes" if task.get("webhook") else "no"
        timeout = task.get("timeout", "-")
        cmd = task["cmd"]
        print(f"{name:<{name_w}}  {every:<8}  {enabled:<8}  {webhook:<5}  {timeout:<8}  {cmd}")

    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    """Enable a task."""
    tasks = load()
    found = False
    for task in tasks:
        if task["name"] == args.name:
            task["enabled"] = True
            found = True
            break

    if not found:
        print(f"Error: task {args.name!r} not found", file=sys.stderr)
        return 1

    try:
        save(tasks)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Enabled task {args.name!r}")
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    """Disable a task."""
    tasks = load()
    found = False
    for task in tasks:
        if task["name"] == args.name:
            task["enabled"] = False
            found = True
            break

    if not found:
        print(f"Error: task {args.name!r} not found", file=sys.stderr)
        return 1

    try:
        save(tasks)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Disabled task {args.name!r}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    """Show logs for a task."""
    task_name = args.name
    tail = args.tail
    since = args.since

    # Find the task's log directory
    task_log_dir = LOGS_DIR / task_name
    if not task_log_dir.exists():
        print(f"No logs found for task {task_name!r}")
        return 0

    # Collect all log files, sorted by name (date)
    log_files = sorted(task_log_dir.glob("*.log"))
    if not log_files:
        print(f"No logs found for task {task_name!r}")
        return 0

    # Filter by --since if provided
    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            print(f"Error: invalid --since format: {since!r} (expected ISO 8601)", file=sys.stderr)
            return 1

        filtered = []
        for lf in log_files:
            # Extract date from filename: YYYY-MM-DD.log
            date_str = lf.stem
            try:
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                if file_date >= since_dt.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ):
                    filtered.append(lf)
            except ValueError:
                # Skip files that don't match expected naming
                continue
        log_files = filtered

    if not log_files:
        print(f"No logs matching criteria for task {task_name!r}")
        return 0

    # Apply --since filter at line level
    output_lines: list[str] = []
    for lf in log_files:
        try:
            content = lf.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
        except OSError:
            continue

        if since and since_dt:
            # Strip timezone from since_dt for naive-log comparison
            since_naive = since_dt.replace(tzinfo=None) if since_dt.tzinfo else since_dt
            for line in lines:
                # Log lines start with [ISO8601], e.g. [2026-06-04T12:00:00]
                if line.startswith("["):
                    close_bracket = line.find("]", 1)
                    if close_bracket > 1:
                        try:
                            ts_str = line[1:close_bracket]
                            line_dt = datetime.fromisoformat(
                                ts_str.rstrip("Z")
                            )
                            # line_dt may be aware; normalize to naive for comparison
                            if line_dt.tzinfo:
                                line_dt = line_dt.replace(tzinfo=None)
                            if line_dt >= since_naive:
                                output_lines.append(line)
                            continue
                        except (ValueError, IndexError):
                            pass
                output_lines.append(line)
        else:
            output_lines.extend(lines)

    if not output_lines:
        print(f"No log content for task {task_name!r}")
        return 0

    # Apply --tail
    if tail and tail > 0:
        output_lines = output_lines[-tail:]

    print("\n".join(output_lines))
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """Show execution history for a task."""
    task_name = args.name
    limit = args.limit

    try:
        storage = Storage()
    except Exception as exc:
        print(f"Error opening database: {exc}", file=sys.stderr)
        return 1

    try:
        runs = storage.get_history(task_name, limit=limit)
    finally:
        storage.close()

    if not runs:
        print(f"No history found for task {task_name!r}")
        return 0

    # Table header
    print(f"{'ID':<6}  {'STARTED':<20}  {'EXIT':<5}  {'DUR(ms)':<8}  {'WEBHOOK':<8}")
    print("-" * 60)

    for run in runs:
        run_id = run["id"]
        started = run["started_at"][:19]  # truncate to seconds
        exit_code = run["exit_code"]
        exit_str = str(exit_code) if exit_code is not None else "-"
        duration = run["duration_ms"]
        dur_str = str(duration) if duration is not None else "-"
        webhook_ok = run["webhook_ok"]
        if webhook_ok is None:
            wh_str = "-"
        elif webhook_ok == 1:
            wh_str = "ok"
        else:
            wh_str = "fail"
        print(f"{run_id:<6}  {started:<20}  {exit_str:<5}  {dur_str:<8}  {wh_str:<8}")

    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show daemon status."""
    pid = _read_pid()

    if pid is None:
        print("daemon: not running (no PID file)")
        return 0

    if _is_pid_alive(pid):
        print(f"daemon: running (PID {pid})")
        # Show some stats
        tasks = load()
        enabled = sum(1 for t in tasks if t.get("enabled", True))
        total = len(tasks)
        print(f"tasks: {enabled}/{total} enabled")
        if TASKS_PATH.exists():
            print(f"config: {TASKS_PATH}")
        print(f"logs:   {LOGS_DIR}")
        return 0
    else:
        print(f"daemon: not running (stale PID file for PID {pid})")
        # Clean up stale PID file
        _remove_pid()
        return 0


# ── Argument parser ──────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        prog="termux-cron",
        description="Lightweight scheduler for Termux (no cron required)",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # daemon
    p_daemon = subparsers.add_parser("daemon", help="Start the daemon (foreground)")
    p_daemon.add_argument(
        "--logs-dir",
        default=None,
        help="Override logs directory (default: ./logs)",
    )
    p_daemon.set_defaults(func=cmd_daemon)

    # add
    p_add = subparsers.add_parser("add", help="Add a new task")
    p_add.add_argument("name", help="Task name (must be unique)")
    p_add.add_argument("--every", required=True, help="Interval (e.g. 30s, 5m, 1h, 1d)")
    p_add.add_argument("--cmd", required=True, help="Shell command to execute")
    p_add.add_argument("--webhook", default=None, help="Webhook URL for result notification")
    p_add.add_argument("--timeout", default=None, help="Timeout (e.g. 10m)")
    p_add.add_argument("--cwd", default=None, help="Working directory for the command")
    p_add.set_defaults(func=cmd_add)

    # remove
    p_remove = subparsers.add_parser("remove", help="Remove a task")
    p_remove.add_argument("name", help="Task name to remove")
    p_remove.set_defaults(func=cmd_remove)

    # list
    p_list = subparsers.add_parser("list", help="List all tasks")
    p_list.set_defaults(func=cmd_list)

    # enable
    p_enable = subparsers.add_parser("enable", help="Enable a task")
    p_enable.add_argument("name", help="Task name to enable")
    p_enable.set_defaults(func=cmd_enable)

    # disable
    p_disable = subparsers.add_parser("disable", help="Disable a task")
    p_disable.add_argument("name", help="Task name to disable")
    p_disable.set_defaults(func=cmd_disable)

    # logs
    p_logs = subparsers.add_parser("logs", help="Show task logs")
    p_logs.add_argument("name", help="Task name")
    p_logs.add_argument(
        "--tail", type=int, default=50, help="Show last N lines (default: 50)"
    )
    p_logs.add_argument(
        "--since", default=None, help="Show logs since ISO 8601 datetime (e.g. 2026-06-04)"
    )
    p_logs.set_defaults(func=cmd_logs)

    # history
    p_history = subparsers.add_parser("history", help="Show task execution history")
    p_history.add_argument("name", help="Task name")
    p_history.add_argument(
        "--limit", type=int, default=20, help="Max records to show (default: 20)"
    )
    p_history.set_defaults(func=cmd_history)

    # status
    p_status = subparsers.add_parser("status", help="Show daemon status")
    p_status.set_defaults(func=cmd_status)

    return parser


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 1

    try:
        return args.func(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"error: database: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
