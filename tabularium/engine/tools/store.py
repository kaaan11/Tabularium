"""Durable replay store for ToolAuthorization bindings (DRL-001).

Implements an operational, file-locked replay store outside `.ai/`.
It ensures that once a ToolAuthorization binding is marked as consumed,
it cannot be reused across process boundaries or subsequent executions.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Protocol

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover - POSIX
    import fcntl


class ToolReplayStore(Protocol):
    """Protocol for checking and recording consumed tool authorization bindings."""

    def is_consumed(self, binding: str) -> bool:
        """True if the binding has already been recorded as consumed."""
        ...

    def reserve(self, binding: str, *, execution_id: str = "", tool_request_id: str = "") -> bool:
        """Atomically claim a binding BEFORE the action runs.

        True  — claimed by this caller now.
        False — already consumed; the action must not run.
        A claim is never released: an authorization is one-shot.
        """
        ...

    def consume(self, binding: str, *, execution_id: str = "", tool_request_id: str = "") -> bool:
        """Record a binding as consumed. Returns True on success, False if already consumed."""
        ...


class ToolReplayStoreFailure(OSError):
    """Fail-closed error for durable replay store operations."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail[:300]


class _ReplayFileLock:
    """Bounded, re-entrant-safe file lock matching the JsonRemoteLedger lock pattern."""

    def __init__(self, path: Path, timeout_s: float = 5.0) -> None:
        self.path = path
        self.timeout_s = timeout_s
        self._stream = None

    def __enter__(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = self.path.open("a+", encoding="utf-8")
            if self.path.stat().st_size == 0:
                self._stream.write("lock\n")
                self._stream.flush()
            deadline = time.monotonic() + self.timeout_s
            while True:
                self._stream.seek(0)
                try:
                    if os.name == "nt":
                        msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return None
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise ToolReplayStoreFailure(
                            "REPLAY_LOCK_FAILED", "timed out acquiring replay store lock") from exc
                    time.sleep(0.01)
        except ToolReplayStoreFailure:
            self._close()
            raise
        except (OSError, ValueError) as exc:
            self._close()
            raise ToolReplayStoreFailure(
                "REPLAY_LOCK_FAILED",
                f"cannot acquire replay store lock: {type(exc).__name__}") from exc

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._stream is not None:
            try:
                self._stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                self._close()
                raise ToolReplayStoreFailure("REPLAY_LOCK_FAILED", "cannot release replay store lock")
            self._close()

    def _close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            finally:
                self._stream = None


class JsonToolReplayStore(ToolReplayStore):
    """File-locked durable replay store for tool authorizations.
    
    Lives outside `.ai/` by invariant (operational state is not project truth).
    """

    def __init__(self, path: str | Path, *, project_root: str | Path | None = None) -> None:
        self.path = Path(path).resolve()
        # Protect against living inside .ai/
        if ".ai" in self.path.parts:
            raise ValueError("tool replay store must not live inside .ai/")
        if project_root is not None:
            ai_dir = (Path(project_root) / ".ai").resolve()
            if self.path == ai_dir or ai_dir in self.path.parents:
                raise ValueError("tool replay store must not live inside .ai/")
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def _read_current(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("replay store root must be a dict")
            return raw
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ToolReplayStoreFailure("REPLAY_STORE_CORRUPT",
                                         f"cannot read tool replay store: {type(exc).__name__}") from exc

    def _write_current(self, data: dict[str, dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(f"{self.path.suffix}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            raise ToolReplayStoreFailure("REPLAY_STORE_WRITE_FAILED",
                                         f"cannot write tool replay store: {type(exc).__name__}") from exc
        finally:
            if tmp.is_file():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def is_consumed(self, binding: str) -> bool:
        if not binding:
            return False
        with _ReplayFileLock(self._lock_path):
            current = self._read_current()
            return binding in current

    def reserve(self, binding: str, *, execution_id: str = "", tool_request_id: str = "") -> bool:
        """Atomically claim a binding BEFORE the action runs."""
        return self.consume(binding, execution_id=execution_id, tool_request_id=tool_request_id)

    def consume(self, binding: str, *, execution_id: str = "", tool_request_id: str = "") -> bool:
        if not binding:
            return False
        with _ReplayFileLock(self._lock_path):
            current = self._read_current()
            if binding in current:
                return False
            current[binding] = {
                "binding": binding,
                "execution_id": execution_id,
                "tool_request_id": tool_request_id,
                "consumed_at": time.time(),
            }
            self._write_current(current)
            return True
