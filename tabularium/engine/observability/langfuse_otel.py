"""Langfuse export via OpenTelemetry (M5.3).

    TABULARIUM TRACE SEMANTICS REMAIN AUTHORITATIVE. LANGFUSE IS A CONSUMER.

    LANGFUSE != EVIDENCE != APPROVAL != AUTHORIZATION != PROJECT TRUTH

STRATEGY: **LANGFUSE VIA OTEL.**

Verified against the installed SDK (langfuse 4.14.4), not from memory:

* Langfuse ingestion IS OTLP/HTTP — `/api/public/otel/v1/traces`. The SDK's own
  legacy ingestion client is deprecated in favour of it, in its docstrings.
* The Langfuse domain model is carried entirely in OTel SPAN ATTRIBUTES, named
  by the SDK's public `LangfuseOtelSpanAttributes`
  (`langfuse.observation.type`, `langfuse.observation.model.name`,
  `langfuse.observation.usage_details`, `langfuse.trace.name`, …).
* `Langfuse(tracer_provider=…, span_exporter=…)` are public constructor
  parameters, so bringing your own provider is a supported path.

So Langfuse consumes exactly what M5.2 already produces. This module therefore
adds no second exporter and no second lifecycle taxonomy: it is an ENRICHER
that decorates the M5.2 spans with Langfuse's vocabulary. One Tabularium event
remains one span, named for the Tabularium event type.

Consequences worth stating plainly:

* **Tabularium attributes win every collision.** The enricher's output is merged
  UNDER the `tabu.*` attributes, so a backend vocabulary can never redefine
  what Tabularium asserted.
* **This module does not require the `langfuse` package at runtime.** It needs
  attribute NAMES, which it reads from the SDK when installed and otherwise
  takes from a frozen copy below. A test asserts the two agree whenever the SDK
  is present, so drift is caught rather than assumed away.
* **Nothing is shipped anywhere by default.** Producing spans is not sending
  them; see `otlp_headers` for the explicit, credential-bearing step.

"""

from __future__ import annotations

import json
from typing import Any

from . import events as E
from . import model as M
from . import otel as OTEL

# --- attribute names --------------------------------------------------------
#
# Frozen copy of `langfuse.LangfuseOtelSpanAttributes`, verified byte-for-byte
# against the SDK by test when it is installed. Kept here so the adapter works
# with no Langfuse dependency at all.

TRACE_NAME = "langfuse.trace.name"
TRACE_METADATA = "langfuse.trace.metadata"
TRACE_TAGS = "langfuse.trace.tags"
TRACE_INPUT = "langfuse.trace.input"
TRACE_OUTPUT = "langfuse.trace.output"
TRACE_SESSION_ID = "session.id"
TRACE_USER_ID = "user.id"
OBSERVATION_TYPE = "langfuse.observation.type"
OBSERVATION_METADATA = "langfuse.observation.metadata"
OBSERVATION_LEVEL = "langfuse.observation.level"
OBSERVATION_STATUS_MESSAGE = "langfuse.observation.status_message"
OBSERVATION_MODEL = "langfuse.observation.model.name"
OBSERVATION_USAGE_DETAILS = "langfuse.observation.usage_details"
OBSERVATION_COST_DETAILS = "langfuse.observation.cost_details"
OBSERVATION_INPUT = "langfuse.observation.input"
OBSERVATION_OUTPUT = "langfuse.observation.output"
ENVIRONMENT = "langfuse.environment"
RELEASE = "langfuse.release"

# Attributes that would carry prompts, completions, or record content. They are
# NEVER written by this adapter — listed explicitly so the exclusion is
# reviewable, and asserted by test.
FORBIDDEN_LANGFUSE_ATTRIBUTES = frozenset({
    TRACE_INPUT, TRACE_OUTPUT, OBSERVATION_INPUT, OBSERVATION_OUTPUT,
})

# --- observation types (SDK `ObservationTypeLiteral`) -----------------------

SPAN = "span"
AGENT = "agent"
TOOL = "tool"
GENERATION = "generation"
EVENT = "event"

# Tabularium event -> Langfuse observation type. Deliberately conservative: a worker
# stage is a plain span unless a real model call is evidenced, because calling
# a local fixture worker a "generation" would fabricate model semantics in the
# backend's UI.
OBSERVATION_TYPES = {
    E.TOOL_REQUESTED: TOOL,
    E.TOOL_EXECUTED: TOOL,
    E.TOOL_FAILED: TOOL,
    E.TOOL_DENIED: TOOL,
    E.TOOL_AUTHORIZATION_REQUIRED: TOOL,
    E.WORKER_STARTED: SPAN,
    E.WORKER_COMPLETED: SPAN,
    E.WORKER_FAILED: SPAN,
}

# Langfuse observation levels. A governed refusal is a WARNING, not an ERROR:
# the system worked correctly and a human said no.
LEVEL_DEFAULT = "DEFAULT"
LEVEL_WARNING = "WARNING"
LEVEL_ERROR = "ERROR"

REFUSAL_EVENTS = (E.EXECUTION_REJECTED, E.EXECUTION_CANCELLED,
                  E.TOOL_DENIED, E.PERSISTENCE_DENIED)

# Token-usage keys Tabularium can report honestly, sourced from the worker's own
# usage report (for an API worker, what the PROVIDER returned — M3.6).
USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def sdk_available() -> bool:
    """Whether the langfuse package is importable. Never raises."""
    try:
        import langfuse  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def sdk_attribute_names() -> dict[str, str] | None:
    """The SDK's own attribute names, or None when it is not installed.

    Used by tests to prove the frozen copy above has not drifted from the
    installed SDK.
    """
    try:
        from langfuse import LangfuseOtelSpanAttributes as A
    except Exception:  # noqa: BLE001
        return None
    return {name: getattr(A, name) for name in dir(A)
            if not name.startswith("_")}


class LangfuseEnricher:
    """Adds Langfuse vocabulary to M5.2 spans. Adds no Tabularium semantics.

    Callable with the M5.2 enricher signature
    `(kind, entry, trace, metrics) -> dict`.
    """

    def __init__(self, environment: str = "", release: str = "",
                 session_id: str = "", user_id: str = "") -> None:
        self.environment = environment
        self.release = release
        self.session_id = session_id
        self.user_id = user_id

    def __call__(self, kind: str, entry: M.TraceEvent | None,
                 trace: M.ExecutionTrace, metrics: M.TraceMetrics | None
                 ) -> dict[str, Any]:
        if kind == "root":
            return self._root(trace, metrics)
        if entry is None:
            return {}
        return self._observation(entry)

    # -- root ---------------------------------------------------------------

    def _root(self, trace: M.ExecutionTrace,
              metrics: M.TraceMetrics | None) -> dict[str, Any]:
        summary = M.summarize(trace, metrics)
        attributes: dict[str, Any] = {
            TRACE_NAME: f"tabularium execution {trace.execution_id}",
            OBSERVATION_TYPE: AGENT,
            OBSERVATION_LEVEL: self._root_level(summary.terminal_status),
            TRACE_TAGS: self._tags(trace, summary),
            TRACE_METADATA: _json({
                "tabu_execution_id": trace.execution_id,
                "tabu_task_id": trace.task_id,
                "terminal_status": summary.terminal_status,
                "event_count": summary.event_count,
                "worker_count": summary.worker_count,
                "tool_count": summary.tool_count,
                "approval_count": summary.approval_count,
                "persistence_count": summary.persistence_count,
                "failure_reason_codes": list(summary.failure_reason_codes),
                "context_economy": _metric_metadata(metrics),
            }),
        }
        if summary.terminal_status:
            attributes[OBSERVATION_STATUS_MESSAGE] = summary.terminal_status
        if self.environment:
            attributes[ENVIRONMENT] = self.environment
        if self.release:
            attributes[RELEASE] = self.release
        if self.session_id:
            attributes[TRACE_SESSION_ID] = self.session_id
        if self.user_id:
            attributes[TRACE_USER_ID] = self.user_id
        return attributes

    @staticmethod
    def _root_level(terminal: str) -> str:
        if terminal in (E.EXECUTION_FAILED, E.PREPARATION_REJECTED):
            return LEVEL_ERROR
        if terminal in (E.EXECUTION_REJECTED, E.EXECUTION_CANCELLED):
            return LEVEL_WARNING
        return LEVEL_DEFAULT

    @staticmethod
    def _tags(trace: M.ExecutionTrace, summary) -> list[str]:
        tags = ["tabularium", f"execution:{trace.execution_id}"]
        if trace.task_id:
            tags.append(f"task:{trace.task_id}")
        if summary.terminal_status:
            tags.append(summary.terminal_status)
        return tags

    # -- per observation ----------------------------------------------------

    def _observation(self, entry: M.TraceEvent) -> dict[str, Any]:
        attributes: dict[str, Any] = {
            OBSERVATION_TYPE: self._observation_type(entry),
            OBSERVATION_LEVEL: self._level(entry),
            OBSERVATION_METADATA: _json(self._metadata(entry)),
        }
        if entry.reason_codes:
            attributes[OBSERVATION_STATUS_MESSAGE] = ", ".join(
                entry.reason_codes)

        model = _model_name(entry)
        if model:
            # Only a real model call becomes a generation. Without an
            # evidenced model there is nothing to generate FROM, and claiming
            # otherwise would put a fictional LLM call in the backend.
            attributes[OBSERVATION_TYPE] = GENERATION
            attributes[OBSERVATION_MODEL] = model

        usage = _usage_details(entry)
        if usage:
            attributes[OBSERVATION_USAGE_DETAILS] = _json(usage)

        cost = _cost_details(entry)
        if cost:
            attributes[OBSERVATION_COST_DETAILS] = _json(cost)
        return attributes

    @staticmethod
    def _observation_type(entry: M.TraceEvent) -> str:
        if entry.event_type in OBSERVATION_TYPES:
            return OBSERVATION_TYPES[entry.event_type]
        if entry.duration_ms is None:
            return EVENT
        return SPAN

    @staticmethod
    def _level(entry: M.TraceEvent) -> str:
        if entry.event_type in REFUSAL_EVENTS:
            return LEVEL_WARNING
        if entry.status == E.ERROR:
            return LEVEL_ERROR
        return LEVEL_DEFAULT

    @staticmethod
    def _metadata(entry: M.TraceEvent) -> dict[str, Any]:
        """Tabularium identity and already-sanitized attributes. Never content.

        `entry.attributes` passed M5.1 sanitization at construction, so
        credentials, authority-bearing keys, and bulk payloads cannot be here.
        """
        data: dict[str, Any] = {
            "tabu_event_id": entry.event_id,
            "tabu_execution_id": entry.execution_id,
            "tabu_event_type": entry.event_type,
            "tabu_sequence": entry.sequence,
            "tabu_component": entry.component,
            "tabu_status": entry.status,
        }
        for name in ("task_id", "approval_request_id", "tool_request_id",
                     "proposal_id", "record_id"):
            value = getattr(entry, name)
            if value:
                data[f"tabu_{name}"] = value
        if entry.duration_ms is not None:
            data["tabu_duration_ms"] = entry.duration_ms
        if entry.reason_codes:
            data["tabu_reason_codes"] = list(entry.reason_codes)
        if entry.attributes:
            data["tabu_attributes"] = dict(entry.attributes)
        return data


def export_trace_to_langfuse(trace: M.ExecutionTrace, tracer_provider: Any,
                             metrics: M.TraceMetrics | None = None,
                             environment: str = "", release: str = "",
                             session_id: str = "", user_id: str = ""
                             ) -> OTEL.OtelExportResult:
    """Export a finished Tabularium trace as Langfuse-shaped OTel spans.

    Reuses the M5.2 exporter wholesale: same spans, same ordering, same Tabularium
    attributes, plus Langfuse vocabulary. BEST_EFFORT — never raises, and
    cannot affect execution because nothing in an execution path calls it.

    Producing spans is NOT sending them. Where they go is decided by the
    `tracer_provider` the caller passes.
    """
    enricher = LangfuseEnricher(environment=environment, release=release,
                                session_id=session_id, user_id=user_id)
    return OTEL.export_trace(trace, tracer_provider, metrics, enrich=enricher)


def otlp_headers(public_key: str, secret_key: str) -> dict[str, str]:
    """Basic-auth headers for the Langfuse OTLP endpoint.

    THE EXPLICIT, CREDENTIAL-BEARING STEP. Nothing calls this automatically:
    telemetry leaves the machine only when a caller deliberately builds an OTLP
    exporter with these headers and attaches it to a provider.

    Credentials are CALLER-SUPPLIED and never read from the environment here,
    never logged, and never written into a trace, a record, or this repository.
    """
    import base64

    if not public_key or not secret_key:
        raise ValueError("Langfuse export requires an explicit public and "
                         "secret key; none is read from the environment")
    token = base64.b64encode(
        f"{public_key}:{secret_key}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}",
            "x-langfuse-ingestion-version": "4"}


def otlp_endpoint(host: str) -> str:
    """The Langfuse OTLP/HTTP trace endpoint for a host.

    Path verified against the installed SDK's own OpenTelemetry client:
    `/api/public/otel/v1/traces`.
    """
    return f"{host.rstrip('/')}/api/public/otel/v1/traces"


# --- helpers ----------------------------------------------------------------

def _model_name(entry: M.TraceEvent) -> str:
    """The model actually used, or empty when no model call is evidenced."""
    value = (entry.attributes or {}).get("model")
    return value if isinstance(value, str) and value.strip() else ""


def _usage_details(entry: M.TraceEvent) -> dict[str, int]:
    """Token counts, only where the worker actually reported them.

    An absent count stays absent. Zero would be a measurement, and this layer
    has no way to measure tokens itself.
    """
    attributes = entry.attributes or {}
    usage = {}
    for key in USAGE_KEYS:
        value = attributes.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            usage[key] = value
    return usage


def _cost_details(entry: M.TraceEvent) -> dict[str, float]:
    """Cost ONLY when a provider returned it.

    There is deliberately no price table here. Tabularium does not know what a token
    costs, and a guessed cost in a dashboard is worse than no cost at all
    because it looks authoritative.
    """
    value = (entry.attributes or {}).get("cost_usd")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"total": float(value)}
    return {}


def _metric_metadata(metrics: M.TraceMetrics | None) -> dict[str, Any]:
    """Numeric context economy. An unmeasured value is omitted, not zeroed."""
    return metrics.to_dict() if metrics else {}


def _json(value: Any) -> str:
    """Langfuse metadata attributes travel as JSON strings."""
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return "{}"
