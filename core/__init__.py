"""core package — termux-cron modules.

Public submodules
-----------------
- config    — YAML task config read/write, validation, interval parsing
- daemon    — main event loop (orchestrator)
- logwriter — buffered log writer (Android-optimised)
- runner    — subprocess execution with timeout
- scheduler — in-memory next-run tracking
- storage   — SQLite execution history
- webhook   — HTTP POST notifications
"""
