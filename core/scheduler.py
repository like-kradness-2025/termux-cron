"""Scheduler module for termux-cron.

Provides ``TaskScheduler``, an in-memory scheduler that tracks
per-task next-run timestamps and determines when a task is due.
"""

import time
from typing import TYPE_CHECKING

from core.config import parse_interval

if TYPE_CHECKING:
    from _typeshed import SupportsRichComparison  # noqa: F401  # pragma: no cover


# ── TaskScheduler ────────────────────────────────────────────────────────────


class TaskScheduler:
    """In-memory schedule tracker for a set of cron-like tasks.

    Each task's *next_run* is stored as a UNIX timestamp (float).  A task
    whose *next_run* is in the past (or equal to *now*) is considered due.
    After a task is marked as run, its *next_run* advances by ``every``
    interval seconds.

    Parameters
    ----------
    tasks : list[dict]
        Task dictionaries as returned by :func:`core.config.load`.  Only
        enabled tasks are tracked.
    now : float | None
        The "current time" used for initialisation (seconds since epoch).
        Defaults to the real wall-clock time (``time.time()``).
    """

    def __init__(self, tasks: list[dict], now: float | None = None) -> None:
        if now is None:
            now = time.time()

        # Map task name -> task dict for fast lookup
        self._tasks: dict[str, dict] = {}
        # Map task name -> next-run UNIX timestamp
        self._next_run: dict[str, float] = {}

        for task in tasks:
            name: str = task["name"]
            enabled: bool = task.get("enabled", True)
            if not enabled:
                continue
            self._tasks[name] = task
            # New/never-run tasks are due immediately on first tick
            self._next_run[name] = now

    # ── Public API ─────────────────────────────────────────────────────────

    def is_due(self, task_name: str, now: float | None = None) -> bool:
        """Check whether *task_name* is due to run.

        Parameters
        ----------
        task_name : str
            Name of the task to check.
        now : float | None
            Current time (seconds since epoch).  Defaults to
            ``time.time()``.

        Returns
        -------
        bool
            ``True`` if the task is enabled and its *next_run* ≤ *now*.
        """
        if now is None:
            now = time.time()

        if task_name not in self._tasks:
            return False

        # Only enabled tasks are stored in _tasks, but double-check anyway
        task = self._tasks[task_name]
        if not task.get("enabled", True):
            return False

        return now >= self._next_run.get(task_name, 0.0)

    def mark_run(self, task_name: str, now: float | None = None) -> None:
        """Record that *task_name* has just been executed.

        Advances the task's *next_run* by its configured interval.

        Parameters
        ----------
        task_name : str
            Name of the task that ran.
        now : float | None
            Timestamp of this run's start or finish.  Defaults to
            ``time.time()``.

        Raises
        ------
        KeyError
            If *task_name* is not tracked by this scheduler.
        """
        if now is None:
            now = time.time()

        task = self._tasks[task_name]  # raises KeyError if unknown
        interval_seconds = parse_interval(task["every"])
        self._next_run[task_name] = now + interval_seconds

    def get_next_run(self, task_name: str) -> float | None:
        """Return the scheduled *next_run* timestamp for *task_name*.

        Returns ``None`` if the task is not tracked.
        """
        return self._next_run.get(task_name)

    def get_task(self, task_name: str) -> dict | None:
        """Return the task dict for *task_name*, or ``None`` if not tracked."""
        return self._tasks.get(task_name)

    @property
    def tasks(self) -> list[dict]:
        """Return the list of tracked task dicts."""
        return list(self._tasks.values())

    @property
    def task_names(self) -> list[str]:
        """Return the names of all tracked tasks."""
        return list(self._tasks.keys())

    def __len__(self) -> int:
        return len(self._tasks)

    def __repr__(self) -> str:
        return f"<TaskScheduler tasks={len(self._tasks)}>"
