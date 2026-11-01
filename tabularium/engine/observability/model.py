"""Tabularium-owned execution trace model (M5.1). DESCRIPTIVE ONLY.

    TRACE != EVIDENCE != DECISION != APPROVAL != AUTHORIZATION != PROJECT TRUTH

A trace records what an authoritative subsystem ALREADY DID. It decides
nothing, authorizes nothing, and proves nothing. A `TraceEvent` saying
`PERSISTENCE_WRITTEN` does not create a record; it reports that the M4.4 writer
already created one.

The model is vendor-neutral and plain: no OpenTelemetry, no Langfuse, no
external dependency. M5.2 maps these events onto OTel spans without changing
their meaning — Tabularium owns semantics, an exporter owns transport.

STRUCTURAL AUTHORITY GUARD: a `TraceEvent` has no `approved_by`,
`authorized_by`, `decided_by`, `decision`, `binding`, or `content` field, and
`from_dict` refuses any payload carrying one. So a trace cannot be reshaped
into an approval, a tool authorization, or a persistence authorization — the
fields those parsers require simply cannot exist here.

"""

from __future__ import annotations

import re

from tabularium.engine.secret_tokens import PROVIDER_TOKEN_PATTERNS
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import events as E

# Field names that carry authority in M4.3 / M4.4 / M4.6. None may ever appear
# on a trace event, in its attributes, or in a serialized trace.
FORBIDDEN_ATTRIBUTE_KEYS = frozenset({
    "approved_by", "approved_at", "authorized_by", "authorized_at",
    "decided_by", "decided_at", "decision", "binding", "content",
    "approval_status", "approved_scope", "arguments", "payload", "output",
    "worker_output", "prompt", "observation", "env", "environ",
})

# Credential shapes, reusing the same rules as M4.4/M4.6 rather than inventing
# a second scanner.
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9]{8,}"),
    re.compile(r"\b(api[_-]?key|secret[_-]?key|access[_-]?key)\b\s*[:=]",
               re.IGNORECASE),
    re.compile(r"\bpassword\b\s*[:=]", re.IGNORECASE),
    re.compile(r"\bauthorization\b\s*[:=]\s*\S", re.IGNORECASE),
    re.compile(r"\bbearer\s+[A-Za-z0-9._-]{8,}", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    *PROVIDER_TOKEN_PATTERNS,
)

REDACTED = "[REDACTED]"

# Attribute values longer than this are replaced by a length summary: a trace
# must not become a second copy of the context it is describing.
MAX_ATTRIBUTE_CHARS = 200


def utc_now() -> str:
    """Timezone-aware UTC, offset-explicit. Never a naive local timestamp."""
    return datetime.now(timezone.utc).isoformat()


def contains_secret(text: str) -> bool:
    return any(p.search(text) for p in SECRET_PATTERNS)


def sanitize(attributes: Any) -> dict[str, Any]:
    """Reduce arbitrary attributes to safe, bounded metadata.

    Drops authority-bearing and bulk keys outright, redacts credential shapes,
    and summarizes anything long by its length rather than its content. This is
    the single choke point: everything entering a trace passes through here.
    """
    if not isinstance(attributes, dict):
        return {}
    clean: dict[str, Any] = {}
    for key, value in attributes.items():
        name = str(key)
        if name.lower() in FORBIDDEN_ATTRIBUTE_KEYS:
            continue
        clean[name] = _sanitize_value(value)
    return clean


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, bool) or isinstance(value, int) or value is None:
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        if contains_secret(value):
            return REDACTED
        if len(value) > MAX_ATTRIBUTE_CHARS:
            return f"[{len(value)} chars omitted]"
        return value
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(v) for v in value][:50]
    if isinstance(value, dict):
        return {str(k): _sanitize_value(v) for k, v in value.items()
                if str(k).lower() not in FORBIDDEN_ATTRIBUTE_KEYS}
    # Anything else is described, never embedded.
    return f"<{type(value).__name__}>"


@dataclass(frozen=True)
class TraceMetrics:
    """Token/context and latency measurements for one execution.

    Mirrors the M3/M4 context-economy vocabulary exactly rather than inventing
    a parallel one. Every field is optional: a metric that was not measured is
    `None`, and is never guessed or back-filled.
    """

    # context economy (chars / estimated tokens, as M4.2 reports them)
    available_context: dict[str, int] | None = None
    governed_context: dict[str, int] | None = None
    effective_context: tuple[str, ...] = ()
    projected_context: dict[str, int] | None = None
    worker_input: dict[str, int] | None = None
    continuation_growth: int | None = None
    initial_reduction_pct: float | None = None
    next_session_reduction_pct: float | None = None

    # latency, milliseconds. Active processing is separated from human waiting
    # because a human deliberating is not the system being slow.
    context_load_ms: int | None = None
    projection_ms: int | None = None
    worker_ms: int | None = None
    tool_ms: int | None = None
    persistence_ms: int | None = None
    active_ms: int | None = None
    approval_wait_ms: int | None = None   # None when it spans a process death
    total_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["effective_context"] = list(self.effective_context)
        return {k: v for k, v in data.items() if v is not None and v != []}


@dataclass(frozen=True)
class TraceEvent:
    """One observed boundary crossing. Carries NO authority.

    `sequence` is the ordering authority, not `timestamp`: clocks can repeat,
    go backwards, and differ between processes, and a resumed execution must be
    able to append without reordering what a dead process already wrote.
    """

    event_id: str
    execution_id: str
    event_type: str
    sequence: int
    timestamp: str = field(default_factory=utc_now)
    component: str = E.PIPELINE
    status: str = E.OK
    task_id: str = ""
    duration_ms: int | None = None
    reason_codes: tuple[str, ...] = ()
    # correlation only — these describe relationships, never permission
    approval_request_id: str = ""
    tool_request_id: str = ""
    proposal_id: str = ""
    record_id: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # Sanitization is enforced at construction so no caller can bypass it.
        object.__setattr__(self, "attributes", sanitize(self.attributes))

    @property
    def is_terminal(self) -> bool:
        return self.event_type in E.TERMINAL_EVENTS

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["reason_codes"] = list(self.reason_codes)
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "TraceEvent | None":
        """Rebuild from plain data, or None if it is not a trace event.

        Refuses any payload carrying an authority-bearing key, so a trace can
        never round-trip into something an authority parser would accept.
        """
        if not isinstance(data, dict):
            return None
        if any(k.lower() in FORBIDDEN_ATTRIBUTE_KEYS for k in data):
            return None
        required = ("event_id", "execution_id", "event_type", "sequence")
        if not all(k in data for k in required):
            return None
        if data["event_type"] not in E.EVENT_TYPES:
            return None
        try:
            sequence = int(data["sequence"])
        except (TypeError, ValueError):
            return None
        return cls(
            event_id=str(data["event_id"]),
            execution_id=str(data["execution_id"]),
            event_type=str(data["event_type"]), sequence=sequence,
            timestamp=str(data.get("timestamp") or utc_now()),
            component=str(data.get("component") or E.PIPELINE),
            status=str(data.get("status") or E.OK),
            task_id=str(data.get("task_id") or ""),
            duration_ms=data.get("duration_ms"),
            reason_codes=tuple(data.get("reason_codes") or ()),
            approval_request_id=str(data.get("approval_request_id") or ""),
            tool_request_id=str(data.get("tool_request_id") or ""),
            proposal_id=str(data.get("proposal_id") or ""),
            record_id=str(data.get("record_id") or ""),
            attributes=data.get("attributes") or {})


@dataclass(frozen=True)
class ExecutionTrace:
    """The ordered events observed for one execution. Not project truth."""

    execution_id: str
    task_id: str = ""
    events: tuple[TraceEvent, ...] = ()

    @property
    def ordered(self) -> tuple[TraceEvent, ...]:
        """Deterministic order: sequence first, then event id as a tiebreak."""
        return tuple(sorted(self.events, key=lambda e: (e.sequence, e.event_id)))

    def of_type(self, *event_types: str) -> tuple[TraceEvent, ...]:
        wanted = set(event_types)
        return tuple(e for e in self.ordered if e.event_type in wanted)

    def to_dict(self) -> dict[str, Any]:
        return {"execution_id": self.execution_id, "task_id": self.task_id,
                "events": [e.to_dict() for e in self.ordered]}


@dataclass(frozen=True)
class TraceSummary:
    """A deterministic reduction of recorded events. Infers nothing.

    Every field is counted or copied from events that were actually emitted.
    If no terminal event was recorded, `terminal_status` is empty rather than
    guessed — an execution whose process died mid-flight has no terminal fact
    to report, and inventing one would make the trace lie.
    """

    execution_id: str
    task_id: str = ""
    terminal_status: str = ""
    event_count: int = 0
    worker_count: int = 0
    tool_count: int = 0
    approval_count: int = 0
    persistence_count: int = 0
    failure_reason_codes: tuple[str, ...] = ()
    metrics: TraceMetrics | None = None
    first_timestamp: str = ""
    last_timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["failure_reason_codes"] = list(self.failure_reason_codes)
        data["metrics"] = self.metrics.to_dict() if self.metrics else None
        return data


def summarize(trace: ExecutionTrace,
              metrics: TraceMetrics | None = None) -> TraceSummary:
    """Reduce a trace to counts and copied facts. No semantic inference."""
    ordered = trace.ordered
    terminal = [e for e in ordered if e.is_terminal]
    failures: list[str] = []
    for event in ordered:
        if event.status == E.ERROR:
            for code in event.reason_codes:
                if code not in failures:
                    failures.append(code)
    return TraceSummary(
        execution_id=trace.execution_id, task_id=trace.task_id,
        terminal_status=(terminal[-1].event_type if terminal else ""),
        event_count=len(ordered),
        worker_count=len(trace.of_type(E.WORKER_STARTED)),
        tool_count=len(trace.of_type(E.TOOL_REQUESTED)),
        approval_count=len(trace.of_type(E.APPROVAL_REQUESTED)),
        persistence_count=len(trace.of_type(E.PERSISTENCE_WRITTEN)),
        failure_reason_codes=tuple(failures), metrics=metrics,
        first_timestamp=(ordered[0].timestamp if ordered else ""),
        last_timestamp=(ordered[-1].timestamp if ordered else ""))
