"""Optional, non-authoritative memory pilot (M6.2).

STATUS: UNWIRED / OPT-IN EXTENSION.
This module is an opt-in extension and is not wired to default runtime paths.
Memory is a candidate retrieval aid. It is not project truth, evidence,
decision, approval, authorization, task state, or current execution state.
The package is dependency-free and performs no I/O or network activity when
imported. Vendor adapters are intentionally not present in the base layer.
"""

PACKAGE_STATUS: str = "UNWIRED / OPT-IN EXTENSION"

from .model import (
    CANDIDATE_ORPHAN,
    CANDIDATE_RESOLVED,
    CANDIDATE_STALE,
    CANDIDATE_UNRESOLVED,
    MEMORY_DISABLED,
    MEMORY_OK,
    MEMORY_UNAVAILABLE,
    MemoryCandidate,
    MemoryError,
    MemoryQuery,
    MemoryRecord,
    MemoryResult,
    MemoryWriteResult,
    SourceSnapshot,
)
from .precedence import (
    CONFLICT_DETECTED,
    MemoryPrecedenceVerdict,
    evaluate_memory_precedence,
)
from .retrieval import authoritative_sources, resolve_candidate, resolved_candidates
from .store import InMemoryMemoryStore, JsonMemoryStore, MemoryStore

__all__ = [
    "MemoryRecord", "MemoryQuery", "MemoryCandidate", "MemoryResult",
    "MemoryWriteResult", "MemoryError", "SourceSnapshot", "MemoryStore",
    "InMemoryMemoryStore", "JsonMemoryStore", "resolve_candidate",
    "resolved_candidates", "authoritative_sources", "MEMORY_OK",
    "MEMORY_DISABLED", "MEMORY_UNAVAILABLE", "CANDIDATE_RESOLVED",
    "CANDIDATE_STALE", "CANDIDATE_ORPHAN", "CANDIDATE_UNRESOLVED",
    "evaluate_memory_precedence", "MemoryPrecedenceVerdict",
    "CONFLICT_DETECTED",
]
