"""Operational replay ledger for remote request identities (M6.3)."""

from __future__ import annotations

import json
import os
import threading
import time
import tempfile
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover - exercised on POSIX CI only
    import fcntl

from .model import (CANCELLED, COMPLETED, FAILED, IN_FLIGHT, REQUESTED,
                    TIMEOUT, UNKNOWN, RemoteEnvelope)


@dataclass(frozen=True)
class LedgerRecord:
    key: str
    execution_id: str
    task_id: str
    worker_id: str
    payload_digest: str
    status: str = REQUESTED
    result_fingerprint: str = ""
    worker_result: dict[str, Any] | None = None
    reason_codes: tuple[str, ...] = ()
    diagnostics: tuple[dict[str, str], ...] = ()
    cancellation: dict[str, Any] | None = None

    @property
    def terminal(self) -> bool:
        return self.status in (COMPLETED, FAILED, TIMEOUT, CANCELLED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "execution_id": self.execution_id,
            "task_id": self.task_id, "worker_id": self.worker_id,
            "payload_digest": self.payload_digest, "status": self.status,
            "result_fingerprint": self.result_fingerprint,
            "worker_result": self.worker_result,
            "reason_codes": list(self.reason_codes),
            "diagnostics": [dict(item) for item in self.diagnostics],
            "cancellation": self.cancellation,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "LedgerRecord":
        if not isinstance(value, dict):
            raise ValueError("ledger record must be a mapping")
        return cls(
            key=str(value.get("key") or ""),
            execution_id=str(value.get("execution_id") or ""),
            task_id=str(value.get("task_id") or ""),
            worker_id=str(value.get("worker_id") or ""),
            payload_digest=str(value.get("payload_digest") or ""),
            status=str(value.get("status") or UNKNOWN),
            result_fingerprint=str(value.get("result_fingerprint") or ""),
            worker_result=value.get("worker_result"),
            reason_codes=tuple(str(item) for item in value.get("reason_codes") or ()),
            diagnostics=tuple(dict(item) for item in value.get("diagnostics") or ()),
            cancellation=dict(value["cancellation"])
            if isinstance(value.get("cancellation"), dict) else None,
        )


class RemoteLedger:
    """Minimal interface needed by the lifecycle adapter."""

    def begin(self, envelope: RemoteEnvelope) -> LedgerRecord | None:
        raise NotImplementedError

    def get(self, key: str) -> LedgerRecord | None:
        raise NotImplementedError

    def record(self, item: LedgerRecord) -> bool:
        raise NotImplementedError


class InMemoryRemoteLedger(RemoteLedger):
    def __init__(self) -> None:
        self._items: dict[str, LedgerRecord] = {}
        self._lock = threading.RLock()

    @property
    def items(self) -> tuple[LedgerRecord, ...]:
        with self._lock:
            return tuple(self._items.values())

    def begin(self, envelope: RemoteEnvelope) -> LedgerRecord | None:
        with self._lock:
            existing = self._items.get(envelope.idempotency_key)
            if existing is not None:
                return existing
            item = LedgerRecord(
                key=envelope.idempotency_key,
                execution_id=envelope.execution_id,
                task_id=envelope.task_id,
                worker_id=envelope.worker_id,
                payload_digest=envelope.payload_digest,
                status=IN_FLIGHT)
            self._items[item.key] = item
            return None

    def get(self, key: str) -> LedgerRecord | None:
        with self._lock:
            return self._items.get(key)

    def record(self, item: LedgerRecord) -> bool:
        with self._lock:
            existing = self._items.get(item.key)
            if existing is not None:
                if not _same_identity(existing, item):
                    return False
                if existing.terminal:
                    return False
            self._items[item.key] = item
            return True


class RemoteLedgerFailure(OSError):
    """Controlled fail-closed outcome for durable ledger operations."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail[:300]


class JsonRemoteLedger(InMemoryRemoteLedger):
    """Durable operational ledger, never project truth and never `.ai/`.

    Every public operation reloads the JSON while holding an OS-level
    exclusive lock.  A begin or terminal record is therefore a single
    lock/reload/compare/update/replace transaction across processes.
    """

    def __init__(self, path: str | Path, *, project_root: str | Path | None = None) -> None:
        self.path = Path(path)
        if project_root is not None and _inside_ai(self.path, Path(project_root)):
            raise ValueError("remote ledger must not live inside .ai/")
        super().__init__()

    @property
    def _lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".lock")

    def _read_current(self) -> dict[str, LedgerRecord]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("remote ledger root must be a list")
            items: dict[str, LedgerRecord] = {}
            for value in raw:
                item = LedgerRecord.from_dict(value)
                if not item.key:
                    raise ValueError("remote ledger record key must be non-empty")
                items[item.key] = item
            return items
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            # A corrupt ledger cannot authorize anything. Fail closed rather
            # than treating corruption as an empty ledger.
            raise RemoteLedgerFailure("LEDGER_CORRUPT",
                                      f"cannot read remote ledger: {type(exc).__name__}") from exc

    def begin(self, envelope: RemoteEnvelope) -> LedgerRecord | None:
        with self._lock:
            with _LedgerFileLock(self._lock_path):
                current = self._read_current()
                existing = current.get(envelope.idempotency_key)
                if existing is not None:
                    self._items = current
                    return existing
                item = LedgerRecord(
                    key=envelope.idempotency_key,
                    execution_id=envelope.execution_id,
                    task_id=envelope.task_id,
                    worker_id=envelope.worker_id,
                    payload_digest=envelope.payload_digest,
                    status=IN_FLIGHT)
                current[item.key] = item
                self._write_items(current)
                self._items = current
                return None

    def get(self, key: str) -> LedgerRecord | None:
        with self._lock:
            with _LedgerFileLock(self._lock_path):
                current = self._read_current()
                self._items = current
                return current.get(key)

    def items(self) -> tuple[LedgerRecord, ...]:
        with self._lock:
            with _LedgerFileLock(self._lock_path):
                current = self._read_current()
                self._items = current
                return tuple(current.values())

    def record(self, item: LedgerRecord) -> bool:
        with self._lock:
            with _LedgerFileLock(self._lock_path):
                current = self._read_current()
                existing = current.get(item.key)
                if existing is not None:
                    if not _same_identity(existing, item) or existing.terminal:
                        self._items = current
                        return False
                current[item.key] = item
                self._write_items(current)
                self._items = current
                return True

    def _write_items(self, items: dict[str, LedgerRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(
                    list(item.to_dict() for item in items.values()),
                    sort_keys=True, ensure_ascii=False, indent=2))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, str(self.path))
            _fsync_directory(self.path.parent)
        except BaseException as exc:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            if isinstance(exc, RemoteLedgerFailure):
                raise
            raise RemoteLedgerFailure("LEDGER_WRITE_FAILED",
                                      f"cannot write remote ledger: {type(exc).__name__}") from exc


def _same_identity(left: LedgerRecord, right: LedgerRecord) -> bool:
    return (left.key == right.key
            and left.execution_id == right.execution_id
            and left.task_id == right.task_id
            and left.worker_id == right.worker_id
            and left.payload_digest == right.payload_digest)


def _fsync_directory(directory: Path) -> None:
    """Persist the replace where the platform permits directory fsync."""
    if os.name == "nt":
        return
    descriptor = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _LedgerFileLock(AbstractContextManager[None]):
    """Small stdlib-only advisory lock released by process death."""

    def __init__(self, path: Path, *, timeout_s: float = 10.0) -> None:
        self.path = path
        self.timeout_s = timeout_s
        self._stream = None

    def __enter__(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = open(self.path, "a+b")
            if os.name == "nt" and self._stream.tell() == 0:
                self._stream.write(b"0")
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
                        raise RemoteLedgerFailure(
                            "LEDGER_LOCK_FAILED", "timed out acquiring remote ledger lock") from exc
                    time.sleep(0.01)
        except RemoteLedgerFailure:
            self._close()
            raise
        except (OSError, ValueError) as exc:
            self._close()
            raise RemoteLedgerFailure(
                "LEDGER_LOCK_FAILED",
                f"cannot acquire remote ledger lock: {type(exc).__name__}") from exc

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._stream is not None:
            try:
                self._stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                # The lock is still closed below; expose unlock failure as a
                # controlled ledger failure instead of silently proceeding.
                self._close()
                raise RemoteLedgerFailure("LEDGER_LOCK_FAILED",
                                          "cannot release remote ledger lock")
            self._close()

    def _close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            finally:
                self._stream = None


def _inside_ai(path: Path, project_root: Path) -> bool:
    try:
        ai_dir = (project_root / ".ai").resolve()
        candidate = path.resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return candidate == ai_dir or ai_dir in candidate.parents
