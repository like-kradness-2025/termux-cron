"""
Storage module for termux-cron.

SQLite-backed persistent storage for task execution history.
"""

import sqlite3
import os
from pathlib import Path

# Default config directory (same logic as config module but independent)
DB_DIR = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'termux-cron'
DB_NAME = 'history.db'


class Storage:
    """Manages the SQLite database for task execution history."""

    def __init__(self, db_path: str | Path | None = None):
        """Initialise the storage backend.

        Args:
            db_path: Path to the SQLite database file.
                     Defaults to ~/.config/termux-cron/history.db.
        """
        if db_path is None:
            db_path = DB_DIR / DB_NAME

        self.db_path = Path(db_path)

        # Ensure the directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn: sqlite3.Connection = sqlite3.connect(
            str(self.db_path), timeout=5.0, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self) -> None:
        """Create the schema if it does not already exist."""
        conn = self._conn
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_name   TEXT NOT NULL,
                started_at  TEXT NOT NULL,
                finished_at TEXT,
                exit_code   INTEGER,
                duration_ms INTEGER,
                output      TEXT,
                webhook_ok  INTEGER
            );
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_runs_task
            ON runs(task_name, started_at);
        """)
        conn.commit()

    def record_run(
        self,
        task_name: str,
        started_at: str,
        finished_at: str | None = None,
        exit_code: int | None = None,
        duration_ms: int | None = None,
        output: str | None = None,
        webhook_ok: int | None = None,
    ) -> int | None:
        """Insert a new run record and return its row id.

        Args:
            task_name:   Name of the task that ran.
            started_at:  ISO-8601 timestamp when execution started.
            finished_at: ISO-8601 timestamp when execution finished (optional).
            exit_code:   Process exit code (optional).
            duration_ms: Execution duration in milliseconds (optional).
            output:      Captured stdout + stderr (optional).
            webhook_ok:  1 = webhook succeeded, 0 = failed, None = not sent.

        Returns:
            The primary key (row id) of the newly inserted record.
        """
        conn = self._conn
        try:
            cursor = conn.execute(
                """
                INSERT INTO runs (task_name, started_at, finished_at,
                                  exit_code, duration_ms, output, webhook_ok)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (task_name, started_at, finished_at,
                 exit_code, duration_ms, output, webhook_ok),
            )
            conn.commit()
            row_id = cursor.lastrowid
            # lastrowid is always set after a successful INSERT on an
            # AUTOINCREMENT table, but Pyright can't narrow it, so we assert.
            assert row_id is not None
            return row_id
        except sqlite3.Error as exc:
            import logging
            logging.getLogger(__name__).error(
                "Failed to record run for %s: %s", task_name, exc
            )
            return None

    def get_history(self, task_name: str, limit: int = 20) -> list[dict]:
        """Return the most recent runs for a given task.

        Args:
            task_name: Name of the task to fetch history for.
            limit:     Maximum number of records to return (default 20).

        Returns:
            List of dictionaries, each representing one run, ordered by
            started_at descending (most recent first).
        """
        conn = self._conn
        cursor = conn.execute(
            """
            SELECT id, task_name, started_at, finished_at,
                   exit_code, duration_ms, output, webhook_ok
            FROM runs
            WHERE task_name = ?
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (task_name, limit),
        )
        return [dict(row) for row in cursor.fetchall()]

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None  # type: ignore[assignment]

    def cleanup_old_outputs(self, keep_recent: int = 100) -> int:
        """NULL out output for runs older than the most recent N per task.

        This prevents unbounded DB growth from stored output text.

        Args:
            keep_recent: Number of most recent runs per task to preserve output for.

        Returns:
            Number of rows modified.
        """
        cursor = self._conn.execute("""
            UPDATE runs SET output = NULL
            WHERE rowid NOT IN (
                SELECT rowid FROM runs
                WHERE id IN (
                    SELECT id FROM runs AS r2
                    WHERE r2.task_name = runs.task_name
                    ORDER BY r2.started_at DESC
                    LIMIT ?
                )
            )
        """, (keep_recent,))
        self._conn.commit()
        return cursor.rowcount

    def __enter__(self) -> "Storage":
        """Context-manager entry."""
        return self

    def __exit__(self, *exc_info) -> None:
        """Context-manager exit – ensures the connection is closed."""
        self.close()
