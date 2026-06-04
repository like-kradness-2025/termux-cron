# termux-cron

Lightweight cron-like periodic execution manager for Android / Termux.

## Overview

`termux-cron` is a pure-Python scheduler that runs arbitrary shell commands on a timed interval. Think of it as `cron` for environments where you can't run a real cron daemon (Termux, containers, minimal Linux).

**Features:**
- Interval-based scheduling (`30s`, `5m`, `1h`, `1d`, etc.)
- Config file (`~/.config/termux-cron/tasks.yaml`) + CLI commands (`add`/`remove`/`enable`/`disable`)
- Execution history in SQLite (`~/.config/termux-cron/history.db`)
- Per-task log files with 7-day retention
- Webhook notification on task completion
- Graceful shutdown on SIGTERM/SIGINT

## Quick Start

### Install

```bash
git clone https://github.com/like-kradness-2025/termux-cron.git
cd termux-cron
pip install pyyaml  # only external dependency
```

### Usage

```bash
# Start the daemon (foreground)
python termux-cron.py daemon

# In another terminal, add a task
python termux-cron.py add system-monitor --every 1m --cmd "python3 collector.py"

# List tasks
python termux-cron.py list

# View execution history
python termux-cron.py history system-monitor

# Show recent logs
python termux-cron.py logs system-monitor --tail 20

# Disable a task without removing it
python termux-cron.py disable system-monitor

# Check daemon health
python termux-cron.py status
```

## Commands

| Command | Description |
|---------|-------------|
| `daemon` | Start foreground daemon |
| `add <name>` | Add a task (`--every`, `--cmd` required; `--webhook`, `--timeout`, `--cwd` optional) |
| `remove <name>` | Delete a task |
| `list` | Show all tasks |
| `enable <name>` | Enable a disabled task |
| `disable <name>` | Disable a task without removing it |
| `logs <name>` | Show task logs (`--tail N`, `--since ISO`) |
| `history <name>` | Show SQLite execution history (`--limit N`) |
| `status` | Check if daemon is running |

## Config File

Tasks are stored in `~/.config/termux-cron/tasks.yaml`:

```yaml
tasks:
  - name: system-monitor
    cmd: python3 /path/to/collector.py
    every: 1m
    enabled: true

  - name: backup
    cmd: tar czf /sdcard/backup/logs.tar.gz logs/
    every: 6h
    enabled: false

  - name: alert
    cmd: python3 check_alerts.py
    every: 5m
    enabled: true
    webhook: https://discord.com/api/webhooks/...
    timeout: 30s
```

## Project Layout

```
~/.config/termux-cron/
├── tasks.yaml      # Task definitions (YAML)
└── history.db      # Execution history (SQLite)

termux-cron/
├── termux-cron.py  # CLI entrypoint
├── core/
│   ├── config.py   # YAML r/w, validation, interval parsing
│   ├── scheduler.py# Next-run time tracking
│   ├── runner.py   # Subprocess execution
│   ├── storage.py  # SQLite history storage
│   ├── webhook.py  # HTTP POST notifications
│   └── daemon.py   # Main execution loop
├── logs/           # Per-task daily log files
├── tasks.yaml.example
└── README.md
```

## Dependencies

- Python 3.10+
- PyYAML (config parsing)

All other modules use Python stdlib only.

## License

MIT
