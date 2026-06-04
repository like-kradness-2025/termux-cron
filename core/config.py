"""Config module for termux-cron.

Handles reading/writing the tasks YAML config file,
parsing interval strings, and validating task schemas.
"""

import fcntl
import os
import re
from pathlib import Path

import yaml


# ── Path resolution ──────────────────────────────────────────────────────────

def _get_config_dir() -> Path:
    """Return the base config directory honouring XDG_CONFIG_HOME."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg)
    return Path.home() / ".config"


CONFIG_DIR = _get_config_dir() / "termux-cron"
"""Path to the termux-cron configuration directory."""

TASKS_PATH = CONFIG_DIR / "tasks.yaml"
"""Path to the tasks YAML file."""


# ── Interval parsing ─────────────────────────────────────────────────────────

_INTERVAL_RE = re.compile(r"^(\d+)(s|m|h|d)$")

_MULTIPLIERS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}


def parse_interval(s: str) -> int:
    """Parse an interval string like ``30s``, ``5m``, ``1h``, ``1d``.

    Returns the equivalent number of seconds.

    Raises
    ------
    ValueError
        If the string does not match the expected format.
    """
    if not isinstance(s, str):
        raise ValueError(f"Interval must be a string, got {type(s).__name__}: {s!r}")

    m = _INTERVAL_RE.match(s)
    if not m:
        raise ValueError(
            f"Invalid interval: {s!r}. Expected format like '30s', '5m', '1h', '1d'."
        )

    value = int(m.group(1))
    if value <= 0:
        raise ValueError(
            f"Invalid interval: {s!r}. Value must be positive (got {value})."
        )
    unit = m.group(2)
    seconds = value * _MULTIPLIERS[unit]
    if seconds > 365 * 86400:
        raise ValueError(
            f"Invalid interval: {s!r}. Maximum allowed is 365 days."
        )
    return seconds


# ── Validation ───────────────────────────────────────────────────────────────

_VALID_FIELDS = {"name", "cmd", "every", "enabled", "webhook", "timeout", "cwd"}

#: Regex for valid task names — alphanumeric, underscore, dot, hyphen only.
_TASK_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


def validate(task: dict) -> None:
    """Validate a single task dictionary.

    Parameters
    ----------
    task : dict
        The task to validate.

    Raises
    ------
    ValueError
        If any validation rule is violated.  The message describes the
        first problem encountered.
    """
    if not isinstance(task, dict):
        raise ValueError(f"Task must be a dict, got {type(task).__name__}")

    # ── name ──────────────────────────────────────────────────────────────
    name = task.get("name")
    if name is None or not isinstance(name, str) or not name.strip():
        raise ValueError(
            f"'name' is required and must be a non-empty string, got {name!r}"
        )
    if not _TASK_NAME_RE.match(name):
        raise ValueError(
            f"'name' must match [A-Za-z0-9_.-]{{1,128}}, got {name!r}"
        )

    # ── cmd ───────────────────────────────────────────────────────────────
    cmd = task.get("cmd")
    if cmd is None or not isinstance(cmd, str) or not cmd.strip():
        raise ValueError(
            f"'cmd' is required and must be a non-empty string, got {cmd!r}"
        )

    # ── every ─────────────────────────────────────────────────────────────
    every = task.get("every")
    if every is None or not isinstance(every, str) or not every.strip():
        raise ValueError(
            f"'every' is required and must be a non-empty string, got {every!r}"
        )
    # Try parsing it to ensure it's a valid interval
    try:
        parse_interval(every)
    except ValueError as exc:
        raise ValueError(f"Invalid 'every' for task {name!r}: {exc}") from None

    # ── enabled (optional, default True) ──────────────────────────────────
    enabled = task.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(
            f"'enabled' must be a boolean, got {type(enabled).__name__}: {enabled!r}"
        )

    # ── webhook (optional) ────────────────────────────────────────────────
    webhook = task.get("webhook")
    if webhook is not None and not isinstance(webhook, str):
        raise ValueError(
            f"'webhook' must be a string or None, got {type(webhook).__name__}: {webhook!r}"
        )

    # ── timeout (optional) ────────────────────────────────────────────────
    timeout = task.get("timeout")
    if timeout is not None:
        if not isinstance(timeout, str):
            raise ValueError(
                f"'timeout' must be a string or None, got {type(timeout).__name__}: {timeout!r}"
            )
        try:
            timeout_secs = parse_interval(timeout)
        except ValueError as exc:
            raise ValueError(
                f"Invalid 'timeout' for task {name!r}: {exc}"
            ) from None
        if timeout_secs < 1:
            raise ValueError(
                f"'timeout' for task {name!r} must be >= 1 second, got {timeout!r}"
            )

    # ── cwd (optional) ────────────────────────────────────────────────────
    cwd = task.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ValueError(
            f"'cwd' must be a string or None, got {type(cwd).__name__}: {cwd!r}"
        )

    # ── unknown fields warning (not an error, but we flag them) ───────────
    unknown = set(task) - _VALID_FIELDS
    if unknown:
        # We raise a ValueError for unknown fields to be strict
        raise ValueError(
            f"Unknown field(s) in task {name!r}: {', '.join(sorted(unknown))}"
        )


# ── Load / Save ──────────────────────────────────────────────────────────────

_LOCK_FILE = CONFIG_DIR / "tasks.lock"


def _acquire_lock(shared: bool = True) -> int:
    """Acquire file lock on tasks.lock. Blocks until acquired."""
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(_LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
    return fd


def _release_lock(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def load() -> list[dict]:
    """Load tasks from the default YAML config file.

    Returns
    -------
    list[dict]
        A list of validated task dictionaries.
        Returns an empty list if the file does not exist.
    """
    lock_fd = _acquire_lock(shared=True)
    try:
        if not TASKS_PATH.exists():
            return []

        try:
            with open(TASKS_PATH, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"Failed to load tasks: {exc}") from exc

        if data is None:
            return []

        if not isinstance(data, dict):
            raise ValueError(
                f"Expected a YAML mapping at the top level, got {type(data).__name__}"
            )

        tasks = data.get("tasks", [])
        if not isinstance(tasks, list):
            raise ValueError(
                f"'tasks' must be a list, got {type(tasks).__name__}"
            )

        validated = []
        seen_names: set[str] = set()
        for i, task in enumerate(tasks):
            try:
                validate(task)
            except ValueError as exc:
                raise ValueError(f"Task #{i}: {exc}") from None
            name = task["name"]
            if name in seen_names:
                raise ValueError(
                    f"Task #{i}: duplicate task name {name!r}. Task names must be unique."
                )
            seen_names.add(name)
            # Fill in default for enabled if not present
            if "enabled" not in task:
                task["enabled"] = True
            validated.append(task)

        return validated
    finally:
        _release_lock(lock_fd)


def save(tasks: list[dict]) -> None:
    """Save a list of task dictionaries to the default YAML config file.

    The directory is created automatically if it does not exist.

    Parameters
    ----------
    tasks : list[dict]
        The tasks to persist.  Each dict is validated before writing.
    """
    if not isinstance(tasks, list):
        raise ValueError(f"Expected a list of tasks, got {type(tasks).__name__}")

    # Validate all tasks before writing anything. Includes duplicate check.
    seen_names: set[str] = set()
    for i, task in enumerate(tasks):
        try:
            validate(task)
        except ValueError as exc:
            raise ValueError(f"Task #{i}: {exc}") from None
        name = task["name"]
        if name in seen_names:
            raise ValueError(
                f"Task #{i}: duplicate task name {name!r}. Task names must be unique."
            )
        seen_names.add(name)

    lock_fd = _acquire_lock(shared=False)
    try:
        data = {"tasks": tasks}
        tmp = TASKS_PATH.with_suffix(".yaml.tmp")
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                yaml.dump(data, fh, default_flow_style=False, allow_unicode=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, TASKS_PATH)
            # fsync the directory so the rename is durable
            dir_fd = os.open(str(CONFIG_DIR), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (OSError, yaml.YAMLError) as exc:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise ValueError(f"Failed to save tasks: {exc}") from exc
    finally:
        _release_lock(lock_fd)
