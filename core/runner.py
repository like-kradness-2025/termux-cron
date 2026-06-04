"""Runner module for termux-cron.

Provides ``run_command``, a thin wrapper around ``subprocess`` that executes a
shell command string, captures combined stdout/stderr, and returns structured
result metadata (exit code, output, duration).
"""

import os
import signal
import subprocess
import time
from typing import Any

#: Maximum bytes of stdout+stderr to capture.  Prevents memory exhaustion
#: from runaway commands that produce unbounded output.
MAX_OUTPUT_BYTES: int = 65536  # 64 KB


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill the entire process group rooted at *proc*.

    Uses ``os.killpg`` to send SIGKILL to the process group so that
    child and grandchild processes spawned by the shell are also
    terminated.  Falls back to ``proc.kill()`` if the group kill fails
    (e.g. process already exited).
    """
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        # Process already gone — nothing to do.
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_command(
    cmd: str,
    timeout: int | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Execute a shell command and return its result.

    The command is run via ``subprocess.Popen`` with ``shell=True`` in a
    **new process session** (``start_new_session=True``) so that the
    entire process tree can be killed on timeout.
    Both *stdout* and *stderr* are captured together (``stderr=STDOUT``).

    Parameters
    ----------
    cmd : str
        Shell command string to execute.
    timeout : int | None
        Maximum wall-clock time in seconds.  If the process does not
        finish within this limit the **entire process group** is killed
        and :class:`subprocess.TimeoutExpired` is raised.
    cwd : str | None
        Working directory for the subprocess.  ``None`` means inherit
        the current process's working directory.

    Returns
    -------
    dict
        Result dictionary with the following keys:

        - **exit_code** (*int*) – Process return code.
        - **output** (*str*) – Combined stdout+stderr text.
        - **duration_ms** (*int*) – Wall-clock duration in milliseconds.

    Raises
    ------
    subprocess.TimeoutExpired
        If the process exceeds *timeout* seconds.
    """
    start = time.monotonic()

    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            start_new_session=True,  # new process group for clean kill
        )
        stdout_bytes, _ = proc.communicate(timeout=timeout)

        # Cap captured output to MAX_OUTPUT_BYTES to prevent memory exhaustion
        if len(stdout_bytes) > MAX_OUTPUT_BYTES:
            stdout_bytes = stdout_bytes[:MAX_OUTPUT_BYTES] + b"\n... (truncated at 64KB)"

        exit_code = proc.returncode
        # communicate already waits for the process, so returncode is set
        assert exit_code is not None  # help type-narrowers

    except subprocess.TimeoutExpired:
        # Kill the entire process group (shell + children + grandchildren)
        if proc is not None:
            _kill_process_group(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        raise

    duration_s = time.monotonic() - start
    duration_ms = round(duration_s * 1000)

    output = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""

    return {
        "exit_code": exit_code,
        "output": output,
        "duration_ms": duration_ms,
    }
