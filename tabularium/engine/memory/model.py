"""Tabularium-owned memory contracts (M6.2).

The model deliberately contains no authority-bearing objects and no vendor
types. A record is an explicitly indexed pointer to an authoritative source;
a retrieval result is only a candidate until the source is resolved again.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from tabularium.engine.observability.model import FORBIDDEN_ATTRIBUTE_KEYS, contains_secret

MEMORY_OK = "MEMORY_OK"
MEMORY_DISABLED = "MEMORY_DISABLED"
MEMORY_UNAVAILABLE = "MEMORY_UNAVAILABLE"
MEMORY_INVALID = "MEMORY_INVALID"
MEMORY_SCOPE_MISMATCH = "MEMORY_SCOPE_MISMATCH"
MEMORY_DUPLICATE = "MEMORY_DUPLICATE"

CANDIDATE_RESOLVED = "RESOLVED"
CANDIDATE_STALE = "STALE"
CANDIDATE_ORPHAN = "ORPHAN"
CANDIDATE_UNRESOLVED = "UNRESOLVED"

MAX_MEMORY_TEXT_CHARS = 4000
MAX_MEMORY_METADATA_CHARS = 4096
MAX_MEMORY_TAGS = 24

_AUTHORITY_WORDS = (
    "approved", "approval", "authorized", "authorization", "persist",
    "persistence", "tool_request", "tool_authorization", "decision",
    "evidence", "project_truth", "task_status", "current_execution",
    "credential", "token", "api_key", "password", "secret",
)


def _compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _require_id(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _validate_metadata(value: Any, *, limit: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("memory metadata must be a mapping")
    if len(_compact(value)) > limit:
        raise ValueError("memory metadata is oversized")

    def walk(item: Any, path: str = "metadata") -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                lowered = str(key).lower()
                if lowered in FORBIDDEN_ATTRIBUTE_KEYS or any(
                        word in lowered for word in _AUTHORITY_WORDS):
                    raise ValueError(f"memory metadata contains forbidden key: {path}.{key}")
                walk(child, f"{path}.{key}")
        elif isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                walk(child, f"{path}[{index}]")
        elif isinstance(item, str) and contains_secret(item):
            raise ValueError(f"memory metadata contains secret-shaped value: {path}")

    walk(value)
    return dict(value)


@dataclass(frozen=True)
class SourceSnapshot:
    """The small authoritative fact needed to resolve a memory pointer."""

    source_id: str
    source_kind: str
    digest: str = ""
    exists: bool = True
    updated_at: str = ""

    def __post_init__(self) -> None:
        _require_id("source_id", self.source_id)
        _require_id("source_kind", self.source_kind)


@dataclass(frozen=True)
class MemoryRecord:
    """An explicitly indexed, source-bound memory entry.

    `text` is descriptive memory content only. `source_digest` makes stale
    content detectable; it does not turn the memory into a source of truth.
    """

    memory_id: str
    project_id: str
    source_kind: str
    source_id: str
    source_digest: str
    text: str
    created_at: str = ""
    updated_at: str = ""
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (("memory_id", self.memory_id),
                            ("project_id", self.project_id),
                            ("source_kind", self.source_kind),
                            ("source_id", self.source_id),
                            ("source_digest", self.source_digest)):
            _require_id(name, value)
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("memory text must be non-empty")
        if len(self.text) > MAX_MEMORY_TEXT_CHARS:
            raise ValueError("memory text is oversized")
        if contains_secret(self.text):
            raise ValueError("memory text contains secret-shaped value")
        if len(self.tags) > MAX_MEMORY_TAGS:
            raise ValueError("memory has too many tags")
        if any(not isinstance(tag, str) or not tag.strip() for tag in self.tags):
            raise ValueError("memory tags must be non-empty strings")
        if any(contains_secret(tag) for tag in self.tags):
            raise ValueError("memory tags contain secret-shaped value")
        object.__setattr__(self, "metadata", _validate_metadata(
            self.metadata, limit=MAX_MEMORY_METADATA_CHARS))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["tags"] = list(self.tags)
        return data

    @classmethod
    def from_dict(cls, value: Any) -> "MemoryRecord":
        if not isinstance(value, dict):
            raise ValueError("memory record must be a mapping")
        return cls(**{**value, "tags": tuple(value.get("tags") or ())})


@dataclass(frozen=True)
class MemoryQuery:
    """A scoped, bounded retrieval request; it carries no authority."""

    query_id: str
    project_id: str
    text: str
    limit: int = 8
    tags: tuple[str, ...] = ()
    source_kind: str | None = None

    def __post_init__(self) -> None:
        _require_id("query_id", self.query_id)
        _require_id("project_id", self.project_id)
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("memory query text must be non-empty")
        if contains_secret(self.text):
            raise ValueError("memory query contains secret-shaped value")
        if not 1 <= self.limit <= 50:
            raise ValueError("memory query limit must be between 1 and 50")
        if len(self.tags) > MAX_MEMORY_TAGS:
            raise ValueError("memory query has too many tags")

    def to_dict(self) -> dict[str, Any]:
        return {"query_id": self.query_id, "project_id": self.project_id,
                "text": self.text, "limit": self.limit,
                "tags": list(self.tags), "source_kind": self.source_kind}


@dataclass(frozen=True)
class MemoryCandidate:
    """A retrieval candidate, never an authoritative context record."""

    memory_id: str
    project_id: str
    source_kind: str
    source_id: str
    source_digest: str
    text: str
    score: int
    rank: int
    resolution: str
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (("memory_id", self.memory_id),
                            ("project_id", self.project_id),
                            ("source_kind", self.source_kind),
                            ("source_id", self.source_id),
                            ("source_digest", self.source_digest)):
            _require_id(name, value)
        if not isinstance(self.text, str) or len(self.text) > MAX_MEMORY_TEXT_CHARS:
            raise ValueError("memory candidate text is invalid or oversized")
        if contains_secret(self.text):
            raise ValueError("memory candidate contains secret-shaped value")
        object.__setattr__(self, "metadata", _validate_metadata(
            self.metadata, limit=MAX_MEMORY_METADATA_CHARS))

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        data = asdict(self)
        data["tags"] = list(self.tags)
        if not include_text:
            data.pop("text", None)
        return data


@dataclass(frozen=True)
class MemoryResult:
    query_id: str
    project_id: str
    status: str
    candidates: tuple[MemoryCandidate, ...] = ()
    reason_codes: tuple[str, ...] = ()
    records_scanned: int = 0
    latency_ms: float = 0.0

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        return {
            "query_id": self.query_id, "project_id": self.project_id,
            "status": self.status,
            "candidates": [c.to_dict(include_text=include_text)
                           for c in self.candidates],
            "reason_codes": list(self.reason_codes),
            "records_scanned": self.records_scanned,
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True)
class MemoryWriteResult:
    memory_id: str
    project_id: str
    status: str
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryError:
    code: str
    detail: str = ""
