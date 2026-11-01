"""Concurrent checkpoint locking and collision prevention (M4.7 / DRL-003 / DEC-2026-0006).

Provides deterministic cross-process and thread-safe locking for SQLite checkpoint
stores, preventing database corruption and execution collisions.
"""

from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator


class CheckpointLockTimeoutError(TimeoutError):
    """Raised when acquiring the checkpoint lock exceeds the allotted timeout."""


class ExecutionCollisionError(RuntimeError):
    """Raised when an execution_id is already active in another concurrent process."""


@contextmanager
def checkpoint_lock(checkpoint_path: str | Path,
                    timeout: float = 10.0,
                    poll_interval: float = 0.05) -> Generator[Path, None, None]:
    """Acquire an exclusive cross-process file lock for a checkpoint database.

    The lock file is placed alongside the SQLite database with a `.lock` extension.
    Uses non-blocking fcntl.flock in a polling loop to support deterministic timeouts.
    """
    path = Path(checkpoint_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")

    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + max(0.1, timeout)
    acquired = False

    try:
        while time.monotonic() < deadline:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (BlockingIOError, OSError):
                time.sleep(poll_interval)

        if not acquired:
            raise CheckpointLockTimeoutError(
                f"Failed to acquire checkpoint lock for '{path}' within {timeout:.1f}s"
            )

        yield lock_path

    finally:
        if acquired:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(lock_fd)
        except OSError:
            pass


@contextmanager
def execution_collision_guard(checkpoint_path: str | Path,
                              execution_id: str) -> Generator[str, None, None]:
    """Ensure that the given execution_id is not concurrently active in another process.

    Creates an execution-specific lock file. If already locked, raises ExecutionCollisionError.
    """
    if not execution_id or not execution_id.strip():
        raise ValueError("execution_id must not be empty for collision guard")

    path = Path(checkpoint_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in execution_id)
    guard_path = path.with_name(f"{path.name}.exec-{safe_id}.lock")

    guard_fd = os.open(guard_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as err:
            raise ExecutionCollisionError(
                f"Execution '{execution_id}' is already concurrently active"
            ) from err

        yield execution_id

    finally:
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(guard_fd)
        except OSError:
            pass
