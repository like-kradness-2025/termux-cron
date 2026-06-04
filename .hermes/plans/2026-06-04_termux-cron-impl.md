# termux-cron Implementation Plan

> **SDD workflow:** profile_delegate(implementation-engineer) → profile_delegate(code-reviewer, score>=95) per task.
> Independent tasks can use parallel delegate_task(tasks=[...]).

**Goal:** Build the termux-cron daemon + CLI tool per SPEC.md.

**Tech Stack:** Python 3.11+ stdlib, PyYAML

---

## Phase 1: Core Infrastructure (parallel independent modules)

### Task 1: Config module

**Files:** `termux-cron/core/config.py`, `termux-cron/core/__init__.py`

Config reader/writer for `~/.config/termux-cron/tasks.yaml`.

- Read YAML, parse interval strings (`30s`, `5m`, `1h`, `1d`)
- Validate task schema (name, cmd, every required)
- Write back YAML (for CLI add/remove/enable/disable)
- Default config path resolution

### Task 2: Storage module

**Files:** `termux-cron/core/storage.py`

SQLite wrapper for `~/.config/termux-cron/history.db`.

- Create table `runs` on init (id, task_name, started_at, finished_at, exit_code, duration_ms, output, webhook_ok)
- `record_run(task_name, started_at, finished_at, exit_code, duration_ms, output, webhook_ok)`
- `get_history(task_name, limit=20)` — recent runs
- Index on (task_name, started_at)

---

## Phase 2: Execution layer (independent)

### Task 3: Scheduler + Runner

**Files:** `termux-cron/core/scheduler.py`, `termux-cron/core/runner.py`

**scheduler.py:**
- `Interval.parse(every_str)` → seconds as int
- `TaskScheduler(tasks: list[Task])` holds next_run per task
- `is_due(task, now)` → bool
- `mark_run(task, now)` → updates next_run

**runner.py:**
- `run_command(cmd, timeout=None, cwd=None)` → (exit_code, stdout+stderr, duration_ms)
- subprocess with timeout, kill on timeout
- capture both stdout and stderr

### Task 4: Webhook module

**Files:** `termux-cron/core/webhook.py`

- `post_webhook(url, payload)` → success bool
- JSON POST with task name, timestamps, exit code, duration, output

---

## Phase 3: Integration (sequential)

### Task 5: Daemon module

**Files:** `termux-cron/core/daemon.py`

Main loop:
- Load config
- Init storage
- while True: iterate tasks, run due ones, log, record, webhook, sleep 1
- SIGTERM/SIGINT → graceful shutdown
- Log rotation: delete files older than 7 days on startup and periodically

### Task 6: CLI module

**Files:** `termux-cron/termux-cron.py` (top-level entry point)

argparse commands:
- `daemon` — start foreground daemon
- `add <name> --every --cmd --webhook --timeout --cwd`
- `remove <name>`
- `list` — show tasks table
- `enable <name>` / `disable <name>`
- `logs <name> [--tail N] [--since ISO]`
- `history <name> [--limit N]`
- `status` — show daemon health if running (pid file)

Installable as `termux-cron` via `pip install -e .` or symlink.

---

## Phase 4: Polish

### Task 7: Log rotation

**Files:** (part of daemon.py + helper)

- `cleanup_logs(logs_dir, max_age_days=7)` — scan and delete old log files
- Called on daemon startup and every hour

### Task 8: Documentation + examples

**Files:** `README.md`, `tasks.yaml.example`

- README: install, usage, commands reference
- tasks.yaml.example: sample config with system-monitor collector/dashboard recipes

---

## Review Gates

| Step | Reviewer | Score |
|------|----------|-------|
| Each Phase 1-2 task | code-reviewer | >= 95 |
| Each Phase 3-4 task | code-reviewer | >= 95 |
| Final adversarial review | sounding-board (GPT-5.5) | >= 95 |

## Execution order

```
Phase 1 (Task 1 & 2 parallel) → Phase 2 (Task 3 & 4 parallel) → Phase 3 (Task 5 → Task 6) → Phase 4 (Task 7 & 8)
```
