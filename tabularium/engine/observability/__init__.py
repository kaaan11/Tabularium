"""Execution trace model and observability foundation (M5.1).

    TRACE != EVIDENCE != DECISION != APPROVAL != AUTHORIZATION != PROJECT TRUTH

Observability is DESCRIPTIVE ONLY. A trace records what an authoritative
subsystem already did; it decides nothing and authorizes nothing. A trace event
saying `PERSISTENCE_WRITTEN` does not create a record — the M4.4 writer did,
and the event reports it afterwards.

Three guarantees the design rests on:

* **No authority.** A `TraceEvent` has no field an authority parser needs, and
  `from_dict` refuses any payload carrying one. It cannot be reshaped into a
  `HumanApprovalResolution`, `ToolAuthorization`, or `PersistenceAuthorization`.
* **No influence.** Collection is BEST_EFFORT: `emit` never raises into a
  production path, so enabling tracing cannot change what an execution does.
  Failures are recorded on the collector instead of being swallowed.
* **No project truth.** Traces are operational. `JsonlCollector` refuses a path
  inside `.ai/`.

Vendor-neutral by construction: no OpenTelemetry, no Langfuse, no dependency.
M5.2 maps these events onto OTel spans without redefining them — Tabularium owns
semantics, an exporter owns transport.
"""

from .collector import (NO_OP, InMemoryCollector, JsonlCollector,
                        NoOpCollector, Tracer, TraceCollector, emit, resolve)
from .events import (COMPONENTS, CONFIRMED_OUTCOME_EVENTS, EVENT_STATUSES,
                     EVENT_TYPES, TERMINAL_EVENTS, TERMINAL_STATUS_EVENTS)
from .model import (MAX_ATTRIBUTE_CHARS, REDACTED, ExecutionTrace, TraceEvent,
                    TraceMetrics, TraceSummary, contains_secret, sanitize,
                    summarize, utc_now)

__all__ = [
    "TraceEvent", "ExecutionTrace", "TraceMetrics", "TraceSummary",
    "summarize", "sanitize", "contains_secret", "utc_now",
    "TraceCollector", "NoOpCollector", "InMemoryCollector", "JsonlCollector",
    "NO_OP", "emit", "resolve", "Tracer",
    "EVENT_TYPES", "TERMINAL_EVENTS", "CONFIRMED_OUTCOME_EVENTS",
    "TERMINAL_STATUS_EVENTS", "COMPONENTS", "EVENT_STATUSES",
    "REDACTED", "MAX_ATTRIBUTE_CHARS",
]
