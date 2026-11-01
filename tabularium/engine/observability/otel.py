"""OpenTelemetry export adapter (M5.2).

    TABULARIUM OWNS OBSERVABILITY SEMANTICS. OTEL IS TRANSPORT.

    OTEL != EVIDENCE != APPROVAL != AUTHORIZATION != PROJECT TRUTH

This module CONSUMES a finished M5.1 `ExecutionTrace` and emits OpenTelemetry
spans. The Tabularium trace model is unchanged: no `TraceEvent` field was added,
removed, or reinterpreted for OTel's benefit, and no second lifecycle taxonomy
exists — span and event names are the M5.1 event types verbatim.

WHY EXPORT A FINISHED TRACE RATHER THAN EMIT LIVE SPANS

Three reasons, each of which would otherwise weaken a property M5.1 paid for:

1. **Ordering stays authoritative.** M5.1 owns order through an explicit
   `sequence`. Live spans would let exporter arrival order and clock skew
   decide what a backend shows. Exporting a finished trace replays events in
   Tabularium order, with their recorded timestamps.
2. **Execution paths never touch OTel.** No frozen M4 code imports or calls
   this module, so an exporter cannot fail inside a governed execution. A test
   asserts every production package imports without OpenTelemetry present.
3. **Process death gets ONE trace.** A completed trace read back from JSONL is
   exported in a single process, so an execution spanning a process death still
   becomes a single OTel trace — something live in-process spans could not do
   safely without context propagation this milestone deliberately excludes.

The cost is that export is not real-time. That is the right trade here: M5.2 is
about faithful transport, not live monitoring.

"""

from __future__ import annotations

from typing import Any

from . import events as E
from . import model as M

# Attribute namespace. Tabularium identity is carried explicitly so a backend can
# correlate on it; OTel's own trace/span ids remain transport-only and are
# never treated as semantic identity.
NS = "tabu."

# Tabularium terminal events that represent a FAULT. A human rejection is not a
# fault, and neither is a cancellation — see `_status_for` for why they are
# deliberately not mapped to OTel ERROR.
FAULT_EVENTS = (E.EXECUTION_FAILED, E.PREPARATION_REJECTED)

# Events that describe a measurable stage. With a recorded `duration_ms` these
# become CHILD SPANS; without one they become span events on the root, because
# a span with an invented duration would be a lie about timing.
STAGE_EVENTS = (
    E.CONTEXT_LOADED, E.CONTEXT_PROJECTED, E.WORKER_STARTED,
    E.WORKER_COMPLETED, E.WORKER_FAILED, E.TOOL_EXECUTED, E.TOOL_FAILED,
    E.PERSISTENCE_WRITTEN,
)


class OtelUnavailable(RuntimeError):
    """Raised only by `is_available`/`export` callers that opt into strictness."""


def is_available() -> bool:
    """Whether the OpenTelemetry SDK can be imported. Never raises."""
    try:
        import opentelemetry.sdk.trace  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


class OtelExportResult:
    """Outcome of one export. Telemetry success is NOT execution success.

    A caller inspecting this learns whether the EXPORT worked. It says nothing
    about whether the execution succeeded — that fact lives in the Tabularium trace
    and in the authoritative M4 result objects.
    """

    __slots__ = ("exported_spans", "failures", "trace_id")

    def __init__(self) -> None:
        self.exported_spans = 0
        self.failures: list[dict[str, str]] = []
        self.trace_id = ""

    @property
    def healthy(self) -> bool:
        return not self.failures

    def record_failure(self, exc: BaseException) -> None:
        self.failures.append({"error": type(exc).__name__,
                              "detail": str(exc)[:120]})

    def to_dict(self) -> dict[str, Any]:
        return {"exported_spans": self.exported_spans,
                "trace_id": self.trace_id,
                "healthy": self.healthy,
                "failures": [dict(f) for f in self.failures]}


def export_trace(trace: M.ExecutionTrace, tracer_provider: Any,
                 metrics: M.TraceMetrics | None = None,
                 enrich: Any = None) -> OtelExportResult:
    """Export one finished Tabularium trace as one OTel trace. BEST_EFFORT.

    Never raises. An exporter or SDK failure is recorded on the result and
    changes nothing else — it cannot approve, deny, or alter any execution,
    tool action, or persistence, because nothing in those paths calls this.

    `enrich` is an OPTIONAL backend decorator (M5.3): a callable
    `(kind, entry, trace, metrics) -> dict` returning EXTRA span attributes,
    where `kind` is "root" or "event" and `entry` is the `TraceEvent` (None for
    the root). It lets a consumer such as Langfuse add its own vocabulary to
    these spans WITHOUT a second exporter and without changing Tabularium semantics:
    the Tabularium attributes are written first and an enricher cannot overwrite
    them. An enricher that raises is recorded and skipped.
    """
    result = OtelExportResult()
    try:
        from opentelemetry.trace import SpanKind
    except Exception as exc:  # noqa: BLE001 - SDK absent is a normal state
        result.record_failure(exc)
        return result

    ordered = trace.ordered
    if not ordered:
        return result

    try:
        tracer = tracer_provider.get_tracer("tabularium-os")
        root_attributes = _root_attributes(trace, ordered, metrics)
        start_ns = _epoch_ns(ordered[0].timestamp)
        end_ns = _epoch_ns(ordered[-1].timestamp)

        root_attributes = _with_enrichment(
            root_attributes, enrich, "root", None, trace, metrics, result)
        root = tracer.start_span(
            name=f"execution {trace.execution_id}",
            kind=SpanKind.INTERNAL,
            attributes=root_attributes,
            start_time=start_ns)
        result.trace_id = format(root.get_span_context().trace_id, "032x")

        from opentelemetry import trace as otel_trace
        with otel_trace.use_span(root, end_on_exit=False):
            for entry in ordered:
                _export_event(tracer, root, entry, result, enrich, trace,
                              metrics)

        root.set_status(_status_for(ordered))
        root.end(end_time=end_ns)
        result.exported_spans += 1
    except Exception as exc:  # noqa: BLE001 - export never breaks a caller
        result.record_failure(exc)
    return result


def _with_enrichment(attributes: dict, enrich, kind: str, entry, trace,
                     metrics, result: OtelExportResult) -> dict:
    """Merge backend attributes UNDER the Tabularium ones. Never raises.

    Tabularium attributes win every collision: a consumer may add vocabulary but can
    never redefine what Tabularium already asserted about an execution.
    """
    if enrich is None:
        return attributes
    try:
        extra = enrich(kind, entry, trace, metrics) or {}
    except Exception as exc:  # noqa: BLE001 - a decorator cannot break export
        result.record_failure(exc)
        return attributes
    if not isinstance(extra, dict):
        return attributes
    merged = {k: v for k, v in extra.items() if k not in attributes}
    merged.update(attributes)
    return merged


def _export_event(tracer, root, entry: M.TraceEvent,
                  result: OtelExportResult, enrich=None, trace=None,
                  metrics=None) -> None:
    """One Tabularium event -> a child span (if it has a duration) or a span event.

    Names are the M5.1 event types VERBATIM. No second taxonomy is invented,
    and no OTel semantic-convention name is substituted, because renaming would
    make the backend's vocabulary diverge from Tabularium's.
    """
    attributes = _with_enrichment(_event_attributes(entry), enrich, "event",
                                  entry, trace, metrics, result)
    try:
        if entry.event_type in STAGE_EVENTS and entry.duration_ms is not None:
            end_ns = _epoch_ns(entry.timestamp)
            start_ns = end_ns - int(entry.duration_ms) * 1_000_000
            child = tracer.start_span(name=entry.event_type,
                                      attributes=attributes,
                                      start_time=start_ns)
            child.set_status(_event_status(entry))
            child.end(end_time=end_ns)
            result.exported_spans += 1
        else:
            root.add_event(entry.event_type, attributes=attributes,
                           timestamp=_epoch_ns(entry.timestamp))
    except Exception as exc:  # noqa: BLE001
        result.record_failure(exc)


def _root_attributes(trace: M.ExecutionTrace, ordered, metrics) -> dict:
    """Tabularium identity plus summary counts and metrics. Sanitized values only."""
    summary = M.summarize(trace, metrics)
    attributes: dict[str, Any] = {
        f"{NS}execution_id": trace.execution_id,
        f"{NS}event_count": summary.event_count,
        f"{NS}worker_count": summary.worker_count,
        f"{NS}tool_count": summary.tool_count,
        f"{NS}approval_count": summary.approval_count,
        f"{NS}persistence_count": summary.persistence_count,
    }
    if trace.task_id:
        attributes[f"{NS}task_id"] = trace.task_id
    if summary.terminal_status:
        attributes[f"{NS}terminal_status"] = summary.terminal_status
    if summary.failure_reason_codes:
        attributes[f"{NS}failure_reason_codes"] = list(
            summary.failure_reason_codes)
    attributes.update(_metric_attributes(metrics))
    return attributes


def _metric_attributes(metrics: M.TraceMetrics | None) -> dict:
    """Token/context and latency values as safe numerics.

    A metric that was never measured is ABSENT, not zero. Exporting `0` for an
    unmeasured `approval_wait_ms` would invent a measurement, and human waiting
    time is never folded into active processing time.
    """
    if metrics is None:
        return {}
    out: dict[str, Any] = {}
    for name in ("available_context", "governed_context", "projected_context",
                 "worker_input"):
        value = getattr(metrics, name)
        if isinstance(value, dict):
            for unit, number in value.items():
                if isinstance(number, (int, float)):
                    out[f"{NS}{name}.{unit}"] = number
    if metrics.effective_context:
        out[f"{NS}effective_context"] = list(metrics.effective_context)
    for name in ("continuation_growth", "initial_reduction_pct",
                 "next_session_reduction_pct", "context_load_ms",
                 "projection_ms", "worker_ms", "tool_ms", "persistence_ms",
                 "active_ms", "approval_wait_ms", "total_ms"):
        value = getattr(metrics, name)
        if value is not None:
            out[f"{NS}{name}"] = value
    return out


def _event_attributes(entry: M.TraceEvent) -> dict:
    """Sanitized event metadata.

    `entry.attributes` already passed M5.1 sanitization at construction, so
    credentials, authority-bearing keys, and bulk payloads cannot be here.
    This adds only Tabularium identity and correlation, and re-checks nothing it
    cannot improve — a second scanner would be a second place to drift.
    """
    attributes: dict[str, Any] = {
        f"{NS}event_id": entry.event_id,
        f"{NS}execution_id": entry.execution_id,
        f"{NS}sequence": entry.sequence,
        f"{NS}event_type": entry.event_type,
        f"{NS}component": entry.component,
        f"{NS}status": entry.status,
    }
    if entry.task_id:
        attributes[f"{NS}task_id"] = entry.task_id
    if entry.duration_ms is not None:
        attributes[f"{NS}duration_ms"] = entry.duration_ms
    if entry.reason_codes:
        attributes[f"{NS}reason_codes"] = list(entry.reason_codes)
    for name in ("approval_request_id", "tool_request_id", "proposal_id",
                 "record_id"):
        value = getattr(entry, name)
        if value:
            attributes[f"{NS}{name}"] = value
    for key, value in (entry.attributes or {}).items():
        if isinstance(value, (str, int, float, bool)):
            attributes[f"{NS}attr.{key}"] = value
        elif isinstance(value, (list, tuple)) and all(
                isinstance(v, (str, int, float, bool)) for v in value):
            attributes[f"{NS}attr.{key}"] = list(value)
    return attributes


def _status_for(ordered):
    """OTel status from TABULARIUM execution semantics, not from export success.

    THE DELIBERATE CHOICE: a human REJECTION and a CANCELLATION map to UNSET,
    not ERROR and not OK.

    * OK would report a governed refusal as a success.
    * ERROR would report a human's considered decision as a system fault, and
      would page whoever watches error rates for something working correctly.

    UNSET asserts nothing, which is exactly right — and the precise Tabularium
    outcome is always on the span as `tabu.terminal_status`, so nothing is
    lost. Only genuine faults (FAILED, PREPARATION_REJECTED) become ERROR.
    """
    from opentelemetry.trace import Status, StatusCode

    terminal = [e for e in ordered if e.is_terminal]
    if not terminal:
        return Status(StatusCode.UNSET)
    last = terminal[-1].event_type
    if last in FAULT_EVENTS:
        return Status(StatusCode.ERROR, last)
    if last == E.EXECUTION_COMPLETED:
        return Status(StatusCode.OK)
    return Status(StatusCode.UNSET, last)


def _event_status(entry: M.TraceEvent):
    from opentelemetry.trace import Status, StatusCode

    if entry.status == E.ERROR:
        return Status(StatusCode.ERROR,
                      ", ".join(entry.reason_codes) or entry.event_type)
    return Status(StatusCode.OK)


def _epoch_ns(timestamp: str) -> int | None:
    """ISO 8601 -> epoch nanoseconds, so OTel replays the RECORDED time.

    Returning None on an unparseable value lets the SDK fall back to now()
    rather than raising: a malformed timestamp should degrade the export, not
    abort it.
    """
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return int(parsed.timestamp() * 1_000_000_000)


def in_memory_provider():
    """A TracerProvider wired to the SDK's in-memory exporter, for tests.

    Deterministic and offline: no network, no collector, no backend.
    Returns `(provider, exporter)`.
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import \
        InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter
