"""Dependency-free memory stores (M6.2).

The JSON store is operational state and must live outside `.ai/`. It is not a
replacement for M4 persistence and has no path that can write project truth.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Protocol

from tabularium.engine.observability import events as E
from tabularium.engine.observability.collector import emit

from .model import (MEMORY_DISABLED, MEMORY_DUPLICATE, MEMORY_INVALID,
                    MEMORY_OK, MEMORY_UNAVAILABLE, MemoryCandidate,
                    MemoryQuery, MemoryRecord, MemoryResult, MemoryWriteResult)
from .retrieval import ResolverLike, resolve_candidate


class MemoryStore(Protocol):
    """Small Tabularium-owned store boundary; no vendor lifecycle is implied."""

    def index(self, record: MemoryRecord) -> MemoryWriteResult: ...
    def query(self, request: MemoryQuery,
              resolver: ResolverLike | None = None) -> MemoryResult: ...
    def delete(self, project_id: str, memory_id: str) -> bool: ...
    def clear(self) -> None: ...


def _terms(text: str) -> set[str]:
    return {term for term in "".join(
        char.lower() if char.isalnum() else " " for char in text).split()
            if len(term) > 1}


def _candidate(record: MemoryRecord, score: int, rank: int) -> MemoryCandidate:
    return MemoryCandidate(
        memory_id=record.memory_id, project_id=record.project_id,
        source_kind=record.source_kind, source_id=record.source_id,
        source_digest=record.source_digest, text=record.text, score=score,
        rank=rank, resolution="UNRESOLVED", tags=record.tags,
        metadata=dict(record.metadata))


class InMemoryMemoryStore:
    """Deterministic fixture store. It performs no I/O and no network calls."""

    def __init__(self, *, enabled: bool = True, collector: Any = None,
                 execution_id: str = "memory", task_id: str = "") -> None:
        self.enabled = enabled
        self.collector = collector
        self.execution_id = execution_id
        self.task_id = task_id
        self._records: dict[tuple[str, str], MemoryRecord] = {}

    @property
    def records(self) -> tuple[MemoryRecord, ...]:
        return tuple(self._records.values())

    def index(self, record: MemoryRecord) -> MemoryWriteResult:
        if not self.enabled:
            return MemoryWriteResult(record.memory_id, record.project_id,
                                     MEMORY_DISABLED)
        key = (record.project_id, record.memory_id)
        if key in self._records:
            return MemoryWriteResult(record.memory_id, record.project_id,
                                     MEMORY_DUPLICATE, (MEMORY_DUPLICATE,))
        self._records[key] = record
        self._trace(E.MEMORY_INDEXED, status=E.OK, record_id=record.memory_id,
                    attributes={"source_kind": record.source_kind,
                                "metadata_chars": len(json.dumps(record.metadata))})
        return MemoryWriteResult(record.memory_id, record.project_id, MEMORY_OK)

    def query(self, request: MemoryQuery,
              resolver: ResolverLike | None = None) -> MemoryResult:
        started = time.perf_counter()
        if not self.enabled:
            return MemoryResult(request.query_id, request.project_id,
                                MEMORY_DISABLED)
        query_terms = _terms(request.text)
        wanted_tags = set(request.tags)
        matches: list[tuple[int, str, MemoryRecord]] = []
        scanned = 0
        for record in self.records:
            scanned += 1
            if record.project_id != request.project_id:
                continue
            if request.source_kind and record.source_kind != request.source_kind:
                continue
            if wanted_tags and not wanted_tags.intersection(record.tags):
                continue
            score = len(query_terms.intersection(_terms(record.text)))
            score += len(wanted_tags.intersection(record.tags))
            if score:
                matches.append((score, record.memory_id, record))
        matches.sort(key=lambda item: (-item[0], item[1]))
        candidates = tuple(_candidate(record, score, index + 1)
                           for index, (score, _, record) in enumerate(matches[:request.limit]))
        if resolver is not None:
            candidates = tuple(resolve_candidate(candidate, resolver)
                               for candidate in candidates)
        result = MemoryResult(
            query_id=request.query_id, project_id=request.project_id,
            status=MEMORY_OK, candidates=candidates, records_scanned=scanned,
            latency_ms=round((time.perf_counter() - started) * 1000, 3))
        self._trace(E.MEMORY_QUERY, status=E.OK,
                    attributes={"query_id": request.query_id,
                                "query_chars": len(request.text),
                                "records_scanned": scanned})
        self._trace(E.MEMORY_RESULT, status=E.OK,
                    attributes={"query_id": request.query_id,
                                "candidate_count": len(candidates),
                                "records_scanned": scanned,
                                "latency_ms": result.latency_ms,
                                "resolution_counts": {
                                    status: sum(c.resolution == status for c in candidates)
                                    for status in ("RESOLVED", "STALE", "ORPHAN", "UNRESOLVED")}})
        return result

    def delete(self, project_id: str, memory_id: str) -> bool:
        return self._records.pop((project_id, memory_id), None) is not None

    def clear(self) -> None:
        self._records.clear()

    def _trace(self, event_type: str, *, status: str, **fields: Any) -> None:
        emit(self.collector, event_type, self.execution_id, task_id=self.task_id,
             component=E.MEMORY, status=status, **fields)


class JsonMemoryStore(InMemoryMemoryStore):
    """Explicit operational JSON store, safe only outside `.ai/`."""

    def __init__(self, path: str | Path, *, project_root: str | Path | None = None,
                 enabled: bool = True, collector: Any = None,
                 execution_id: str = "memory", task_id: str = "") -> None:
        self.path = Path(path)
        if project_root is not None and _inside_ai(self.path, Path(project_root)):
            raise ValueError("memory store must not live inside .ai/")
        super().__init__(enabled=enabled, collector=collector,
                         execution_id=execution_id, task_id=task_id)
        self._loaded = False

    def _ensure_loaded(self) -> bool:
        if self._loaded:
            return True
        self._loaded = True
        if not self.path.is_file():
            return False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("memory store root must be a list")
            for value in raw:
                record = MemoryRecord.from_dict(value)
                self._records[(record.project_id, record.memory_id)] = record
            return True
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._records.clear()
            self._trace(E.MEMORY_FAILED, status=E.ERROR,
                        reason_codes=(MEMORY_INVALID,),
                        attributes={"error_type": type(exc).__name__})
            return False

    def index(self, record: MemoryRecord) -> MemoryWriteResult:
        if not self.enabled:
            return MemoryWriteResult(record.memory_id, record.project_id,
                                     MEMORY_DISABLED)
        self._ensure_loaded()
        result = super().index(record)
        if result.status == MEMORY_OK:
            self._write()
        return result

    def query(self, request: MemoryQuery,
              resolver: ResolverLike | None = None) -> MemoryResult:
        if not self.enabled:
            return MemoryResult(request.query_id, request.project_id,
                                MEMORY_DISABLED)
        available = self._ensure_loaded()
        if not available and not self._records:
            return MemoryResult(request.query_id, request.project_id,
                                MEMORY_UNAVAILABLE,
                                reason_codes=(MEMORY_UNAVAILABLE,))
        return super().query(request, resolver)

    def delete(self, project_id: str, memory_id: str) -> bool:
        self._ensure_loaded()
        deleted = super().delete(project_id, memory_id)
        if deleted:
            self._write()
        return deleted

    def clear(self) -> None:
        self._ensure_loaded()
        super().clear()
        if self.path.exists():
            self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = [record.to_dict() for record in self.records]
        temporary.write_text(json.dumps(payload, sort_keys=True,
                                        ensure_ascii=False, indent=2),
                             encoding="utf-8", newline="\n")
        os.replace(temporary, self.path)


def _inside_ai(path: Path, project_root: Path) -> bool:
    try:
        ai_dir = (project_root / ".ai").resolve()
        candidate = path.resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return candidate == ai_dir or ai_dir in candidate.parents
