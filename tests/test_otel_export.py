"""M5.2 — OpenTelemetry export adapter.

    TABULARIUM OWNS OBSERVABILITY SEMANTICS. OTEL IS TRANSPORT.

Validates that export is faithful (same taxonomy, same order, same ids), safe
(sanitized, best-effort, no project mutation), and optional (nothing in the
production chain imports OpenTelemetry).

Uses the SDK's own in-memory span exporter: no network, no collector, no
backend.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tabularium.engine.continuation import ContinuationPlan, apply_continuation  # noqa: E402
from tabularium.engine.execution import model as P  # noqa: E402
from tabularium.engine.observability import (InMemoryCollector, JsonlCollector,  # noqa: E402
                                   TraceEvent, TraceMetrics)
from tabularium.engine.observability import collector as C  # noqa: E402
from tabularium.engine.observability import events as EV  # noqa: E402
from tabularium.engine.observability import model as OM  # noqa: E402
from tests.e2e_fixture import (ACTOR, WHEN, E2ETestCase,  # noqa: E402
                                       RecordingWorker, needs_runtime)
from tests.test_e2e_validation import E2EMixin, tool_constraints  # noqa: E402
from tabularium.engine.tools import adapter as TA  # noqa: E402
from tabularium.engine.tools.local_fixture import FixtureWriteTool  # noqa: E402

try:
    import opentelemetry.sdk.trace  # noqa: F401
    OTEL = True
except Exception:  # noqa: BLE001
    OTEL = False

needs_otel = unittest.skipUnless(
    OTEL, "OpenTelemetry not installed "
          "(tabularium/engine/requirements-observability.txt)")

if OTEL:
    from tabularium.engine.observability.otel import (NS, export_trace,  # noqa: E402
                                            in_memory_provider, is_available)


def trace_of(*events) -> OM.ExecutionTrace:
    return OM.ExecutionTrace(execution_id="exec-1", task_id="TASK-2026-0001",
                             events=tuple(events))


def event(seq: int, kind: str, **kw) -> TraceEvent:
    base = dict(event_id=f"exec-1:{seq}", execution_id="exec-1",
                event_type=kind, sequence=seq, task_id="TASK-2026-0001",
                timestamp=f"2026-08-18T12:00:{seq:02d}+00:00")
    base.update(kw)
    return TraceEvent(**base)


def spans_by_name(exporter) -> dict:
    return {s.name: s for s in exporter.get_finished_spans()}


# --- mapping ----------------------------------------------------------------

@needs_otel
class TestMapping(unittest.TestCase):
    def test_mapping_uses_the_tabu_taxonomy_verbatim(self):
        """No second lifecycle taxonomy: span/event names ARE Tabularium events."""
        trace = trace_of(
            event(1, EV.EXECUTION_STARTED),
            event(2, EV.CONTEXT_LOADED, duration_ms=12),
            event(3, EV.WORKER_STARTED),
            event(4, EV.EXECUTION_COMPLETED))
        provider, exporter = in_memory_provider()
        export_trace(trace, provider)
        names = set(spans_by_name(exporter))
        root = next(s for s in exporter.get_finished_spans()
                    if s.name.startswith("execution "))
        emitted = names | {e.name for e in root.events}
        for name in emitted:
            if name.startswith("execution "):
                continue
            self.assertIn(name, EV.EVENT_TYPES,
                          f"{name} is not a Tabularium event type")

    def test_mapping_stage_with_duration_becomes_a_child_span(self):
        trace = trace_of(event(1, EV.EXECUTION_STARTED),
                         event(2, EV.CONTEXT_LOADED, duration_ms=25),
                         event(3, EV.EXECUTION_COMPLETED))
        provider, exporter = in_memory_provider()
        export_trace(trace, provider)
        self.assertIn(EV.CONTEXT_LOADED, spans_by_name(exporter))

    def test_mapping_event_without_duration_becomes_a_span_event(self):
        trace = trace_of(event(1, EV.EXECUTION_STARTED),
                         event(2, EV.SKILL_APPLIED),
                         event(3, EV.EXECUTION_COMPLETED))
        provider, exporter = in_memory_provider()
        export_trace(trace, provider)
        self.assertNotIn(EV.SKILL_APPLIED, spans_by_name(exporter))
        root = next(s for s in exporter.get_finished_spans()
                    if s.name.startswith("execution "))
        self.assertIn(EV.SKILL_APPLIED, [e.name for e in root.events])

    def test_mapping_one_tabu_trace_is_one_otel_trace(self):
        trace = trace_of(event(1, EV.EXECUTION_STARTED),
                         event(2, EV.CONTEXT_LOADED, duration_ms=5),
                         event(3, EV.WORKER_COMPLETED, duration_ms=9),
                         event(4, EV.EXECUTION_COMPLETED))
        provider, exporter = in_memory_provider()
        export_trace(trace, provider)
        ids = {s.context.trace_id for s in exporter.get_finished_spans()}
        self.assertEqual(len(ids), 1)

    def test_mapping_empty_trace_exports_nothing(self):
        provider, exporter = in_memory_provider()
        result = export_trace(OM.ExecutionTrace(execution_id="exec-1"),
                              provider)
        self.assertEqual(exporter.get_finished_spans(), ())
        self.assertTrue(result.healthy)


# --- identity ---------------------------------------------------------------

@needs_otel
class TestIdentity(unittest.TestCase):
    def test_identity_tabu_ids_preserved_on_the_root(self):
        provider, exporter = in_memory_provider()
        export_trace(trace_of(event(1, EV.EXECUTION_STARTED),
                              event(2, EV.EXECUTION_COMPLETED)), provider)
        root = exporter.get_finished_spans()[-1]
        self.assertEqual(root.attributes[f"{NS}execution_id"], "exec-1")
        self.assertEqual(root.attributes[f"{NS}task_id"], "TASK-2026-0001")

    def test_identity_tabu_ids_preserved_on_children_and_events(self):
        provider, exporter = in_memory_provider()
        export_trace(trace_of(
            event(1, EV.EXECUTION_STARTED),
            event(2, EV.CONTEXT_LOADED, duration_ms=3),
            event(3, EV.EXECUTION_COMPLETED)), provider)
        child = spans_by_name(exporter)[EV.CONTEXT_LOADED]
        self.assertEqual(child.attributes[f"{NS}event_id"], "exec-1:2")
        self.assertEqual(child.attributes[f"{NS}sequence"], 2)

    def test_identity_otel_ids_are_not_tabu_identity(self):
        """OTel ids are transport. Tabularium ids are never replaced by them."""
        provider, exporter = in_memory_provider()
        result = export_trace(trace_of(event(1, EV.EXECUTION_STARTED),
                                       event(2, EV.EXECUTION_COMPLETED)),
                              provider)
        root = exporter.get_finished_spans()[-1]
        otel_id = format(root.context.trace_id, "032x")
        self.assertNotEqual(otel_id, "exec-1")
        self.assertEqual(root.attributes[f"{NS}execution_id"], "exec-1")
        # The Tabularium trace model itself gained no OTel field.
        self.assertFalse(any("otel" in f.lower() or "span" in f.lower()
                             for f in TraceEvent.__dataclass_fields__))
        self.assertEqual(result.trace_id, otel_id)

    def test_identity_correlation_ids_exported(self):
        provider, exporter = in_memory_provider()
        export_trace(trace_of(
            event(1, EV.EXECUTION_STARTED),
            event(2, EV.APPROVAL_REQUESTED,
                  approval_request_id="exec-1:approval-1"),
            event(3, EV.PERSISTENCE_WRITTEN, duration_ms=2,
                  proposal_id="p-1", record_id="HND-2026-0001"),
            event(4, EV.EXECUTION_COMPLETED)), provider)
        written = spans_by_name(exporter)[EV.PERSISTENCE_WRITTEN]
        self.assertEqual(written.attributes[f"{NS}record_id"],
                         "HND-2026-0001")
        root = next(s for s in exporter.get_finished_spans()
                    if s.name.startswith("execution "))
        approval = next(e for e in root.events
                        if e.name == EV.APPROVAL_REQUESTED)
        self.assertEqual(approval.attributes[f"{NS}approval_request_id"],
                         "exec-1:approval-1")


# --- ordering ---------------------------------------------------------------

@needs_otel
class TestOrdering(unittest.TestCase):
    def test_ordering_follows_tabu_sequence_not_arrival(self):
        # Feed events out of order with inverted timestamps.
        late = event(1, EV.EXECUTION_STARTED)
        early = event(2, EV.SKILL_APPLIED,
                      timestamp="2026-08-18T11:00:00+00:00")
        end = event(3, EV.EXECUTION_COMPLETED)
        provider, exporter = in_memory_provider()
        export_trace(trace_of(end, early, late), provider)
        root = next(s for s in exporter.get_finished_spans()
                    if s.name.startswith("execution "))
        sequences = [e.attributes[f"{NS}sequence"] for e in root.events]
        self.assertEqual(sequences, sorted(sequences))

    def test_ordering_uses_recorded_timestamps(self):
        provider, exporter = in_memory_provider()
        export_trace(trace_of(
            event(1, EV.EXECUTION_STARTED),
            event(2, EV.CONTEXT_LOADED, duration_ms=100),
            event(3, EV.EXECUTION_COMPLETED)), provider)
        child = spans_by_name(exporter)[EV.CONTEXT_LOADED]
        # 100ms duration replayed from the recorded timestamp, not from now().
        self.assertEqual((child.end_time - child.start_time) // 1_000_000, 100)


# --- metrics ----------------------------------------------------------------

@needs_otel
class TestMetrics(unittest.TestCase):
    def full_metrics(self) -> TraceMetrics:
        return TraceMetrics(
            available_context={"chars": 24870, "estimated_tokens": 6218},
            governed_context={"chars": 23369, "estimated_tokens": 5843},
            effective_context=("required", "constraints"),
            projected_context={"chars": 4359, "estimated_tokens": 1090},
            worker_input={"chars": 4359, "estimated_tokens": 1090},
            continuation_growth=197, initial_reduction_pct=81.3,
            next_session_reduction_pct=81.5,
            context_load_ms=13, projection_ms=1, worker_ms=1146,
            tool_ms=4, persistence_ms=7, active_ms=1171, total_ms=1171)

    def exported_root(self, metrics):
        provider, exporter = in_memory_provider()
        export_trace(trace_of(event(1, EV.EXECUTION_STARTED),
                              event(2, EV.EXECUTION_COMPLETED)),
                     provider, metrics)
        return exporter.get_finished_spans()[-1]

    def test_metrics_token_economy_exported(self):
        attrs = self.exported_root(self.full_metrics()).attributes
        self.assertEqual(attrs[f"{NS}available_context.chars"], 24870)
        self.assertEqual(attrs[f"{NS}governed_context.chars"], 23369)
        self.assertEqual(attrs[f"{NS}projected_context.estimated_tokens"], 1090)
        self.assertEqual(attrs[f"{NS}worker_input.chars"], 4359)
        self.assertEqual(attrs[f"{NS}continuation_growth"], 197)
        self.assertEqual(attrs[f"{NS}initial_reduction_pct"], 81.3)
        self.assertEqual(attrs[f"{NS}next_session_reduction_pct"], 81.5)
        self.assertEqual(list(attrs[f"{NS}effective_context"]),
                         ["required", "constraints"])

    def test_metrics_latency_exported(self):
        attrs = self.exported_root(self.full_metrics()).attributes
        for name, expected in (("context_load_ms", 13), ("projection_ms", 1),
                               ("worker_ms", 1146), ("tool_ms", 4),
                               ("persistence_ms", 7), ("total_ms", 1171)):
            self.assertEqual(attrs[f"{NS}{name}"], expected)

    def test_metrics_unmeasured_values_are_absent_not_zero(self):
        """An unmeasured metric must never be exported as a measurement."""
        attrs = self.exported_root(TraceMetrics(worker_ms=5)).attributes
        self.assertEqual(attrs[f"{NS}worker_ms"], 5)
        self.assertNotIn(f"{NS}approval_wait_ms", attrs)
        self.assertNotIn(f"{NS}total_ms", attrs)

    def test_metrics_human_wait_not_merged_into_active(self):
        metrics = TraceMetrics(active_ms=100, approval_wait_ms=90_000)
        attrs = self.exported_root(metrics).attributes
        self.assertEqual(attrs[f"{NS}active_ms"], 100)
        self.assertEqual(attrs[f"{NS}approval_wait_ms"], 90_000)

    def test_metrics_summary_counts_exported(self):
        provider, exporter = in_memory_provider()
        export_trace(trace_of(
            event(1, EV.EXECUTION_STARTED),
            event(2, EV.WORKER_STARTED),
            event(3, EV.TOOL_REQUESTED),
            event(4, EV.APPROVAL_REQUESTED),
            event(5, EV.PERSISTENCE_WRITTEN, duration_ms=1),
            event(6, EV.EXECUTION_COMPLETED)), provider)
        root = next(s for s in exporter.get_finished_spans()
                    if s.name.startswith("execution "))
        self.assertEqual(root.attributes[f"{NS}worker_count"], 1)
        self.assertEqual(root.attributes[f"{NS}tool_count"], 1)
        self.assertEqual(root.attributes[f"{NS}approval_count"], 1)
        self.assertEqual(root.attributes[f"{NS}persistence_count"], 1)


# --- status -----------------------------------------------------------------

@needs_otel
class TestStatus(unittest.TestCase):
    def status_of(self, terminal_event: str):
        from opentelemetry.trace import StatusCode
        provider, exporter = in_memory_provider()
        export_trace(trace_of(event(1, EV.EXECUTION_STARTED),
                              event(2, terminal_event, status=EV.ERROR
                                    if terminal_event in
                                    (EV.EXECUTION_FAILED,) else EV.OK)),
                     provider)
        root = next(s for s in exporter.get_finished_spans()
                    if s.name.startswith("execution "))
        return root.status.status_code, root.attributes, StatusCode

    def test_status_completed_is_ok(self):
        code, _, StatusCode = self.status_of(EV.EXECUTION_COMPLETED)
        self.assertEqual(code, StatusCode.OK)

    def test_status_failure_is_error(self):
        code, _, StatusCode = self.status_of(EV.EXECUTION_FAILED)
        self.assertEqual(code, StatusCode.ERROR)

    def test_status_preparation_rejection_is_error(self):
        code, _, StatusCode = self.status_of(EV.PREPARATION_REJECTED)
        self.assertEqual(code, StatusCode.ERROR)

    def test_status_human_rejection_is_not_ok_and_not_error(self):
        """A governed refusal is neither a success nor a system fault.

        OK would report a refusal as success; ERROR would page whoever watches
        error rates for the system working correctly. UNSET asserts nothing,
        and the exact outcome stays on the span.
        """
        code, attrs, StatusCode = self.status_of(EV.EXECUTION_REJECTED)
        self.assertEqual(code, StatusCode.UNSET)
        self.assertEqual(attrs[f"{NS}terminal_status"], EV.EXECUTION_REJECTED)

    def test_status_cancelled_is_unset_with_the_exact_outcome(self):
        code, attrs, StatusCode = self.status_of(EV.EXECUTION_CANCELLED)
        self.assertEqual(code, StatusCode.UNSET)
        self.assertEqual(attrs[f"{NS}terminal_status"], EV.EXECUTION_CANCELLED)

    def test_status_export_success_is_not_execution_success(self):
        from opentelemetry.trace import StatusCode
        provider, exporter = in_memory_provider()
        result = export_trace(trace_of(event(1, EV.EXECUTION_STARTED),
                                       event(2, EV.EXECUTION_FAILED,
                                             status=EV.ERROR,
                                             reason_codes=("BOOM",))),
                              provider)
        self.assertTrue(result.healthy, "the export itself worked")
        root = next(s for s in exporter.get_finished_spans()
                    if s.name.startswith("execution "))
        self.assertEqual(root.status.status_code, StatusCode.ERROR,
                         "a healthy export reported a failed execution as OK")

    def test_status_failure_reason_codes_exported(self):
        provider, exporter = in_memory_provider()
        export_trace(trace_of(
            event(1, EV.EXECUTION_STARTED),
            event(2, EV.WORKER_FAILED, status=EV.ERROR, duration_ms=3,
                  reason_codes=("WORKER_UNAVAILABLE",)),
            event(3, EV.EXECUTION_FAILED, status=EV.ERROR)), provider)
        root = next(s for s in exporter.get_finished_spans()
                    if s.name.startswith("execution "))
        self.assertIn("WORKER_UNAVAILABLE",
                      list(root.attributes[f"{NS}failure_reason_codes"]))


# --- sanitization -----------------------------------------------------------

@needs_otel
class TestExportSanitization(unittest.TestCase):
    SECRET = "sk-otelexport00000000secret"

    def test_sanitization_credentials_never_exported(self):
        provider, exporter = in_memory_provider()
        export_trace(trace_of(
            event(1, EV.EXECUTION_STARTED,
                  attributes={"note": f"token={self.SECRET}"}),
            event(2, EV.EXECUTION_COMPLETED)), provider)
        blob = json.dumps([str(s.attributes) + str(
            [str(e.attributes) for e in s.events])
            for s in exporter.get_finished_spans()])
        self.assertNotIn(self.SECRET, blob)

    def test_sanitization_authority_and_bulk_keys_never_exported(self):
        provider, exporter = in_memory_provider()
        export_trace(trace_of(
            event(1, EV.EXECUTION_STARTED, attributes={
                "approved_by": ACTOR, "authorized_by": ACTOR,
                "decision": "APPROVE", "payload": {"required": "..."},
                "output": "the whole answer", "content": "---\nrecord\n"}),
            event(2, EV.EXECUTION_COMPLETED)), provider)
        blob = str([s.attributes for s in exporter.get_finished_spans()])
        root = next(s for s in exporter.get_finished_spans()
                    if s.name.startswith("execution "))
        blob += str([e.attributes for e in root.events])
        for marker in ("approved_by", "authorized_by", "decision",
                       "the whole answer", "schema_version"):
            self.assertNotIn(marker, blob)

    def test_sanitization_long_values_summarized(self):
        provider, exporter = in_memory_provider()
        export_trace(trace_of(
            event(1, EV.EXECUTION_STARTED,
                  attributes={"blob": "x" * 5000}),
            event(2, EV.EXECUTION_COMPLETED)), provider)
        root = next(s for s in exporter.get_finished_spans()
                    if s.name.startswith("execution "))
        started = next(e for e in root.events
                       if e.name == EV.EXECUTION_STARTED)
        self.assertIn("chars omitted", started.attributes[f"{NS}attr.blob"])


# --- best-effort ------------------------------------------------------------

@needs_otel
class TestBestEffort(unittest.TestCase):
    def test_exporter_failure_never_raises(self):
        class ExplodingProvider:
            def get_tracer(self, *a, **kw):
                raise RuntimeError("backend down")

        result = export_trace(trace_of(event(1, EV.EXECUTION_STARTED),
                                       event(2, EV.EXECUTION_COMPLETED)),
                              ExplodingProvider())
        self.assertFalse(result.healthy)
        self.assertTrue(result.failures)
        self.assertEqual(result.exported_spans, 0)

    def test_partial_span_failure_is_recorded_not_raised(self):
        from opentelemetry.sdk.trace import TracerProvider

        class HalfBroken(TracerProvider):
            def get_tracer(self, *a, **kw):
                tracer = super().get_tracer(*a, **kw)

                class Wrapper:
                    def start_span(inner, name, **kw):
                        if name != "execution exec-1":
                            raise RuntimeError("span rejected")
                        return tracer.start_span(name, **kw)
                return Wrapper()

        result = export_trace(trace_of(
            event(1, EV.EXECUTION_STARTED),
            event(2, EV.CONTEXT_LOADED, duration_ms=5),
            event(3, EV.EXECUTION_COMPLETED)), HalfBroken())
        self.assertFalse(result.healthy)
        self.assertTrue(result.failures)

    def test_malformed_timestamp_degrades_not_aborts(self):
        provider, exporter = in_memory_provider()
        result = export_trace(trace_of(
            event(1, EV.EXECUTION_STARTED, timestamp="not-a-timestamp"),
            event(2, EV.EXECUTION_COMPLETED)), provider)
        self.assertTrue(result.exported_spans >= 1)
        self.assertTrue(exporter.get_finished_spans())


# --- disabled / isolation ---------------------------------------------------

class TestDisabledAndIsolation(E2ETestCase, E2EMixin):
    def test_disabled_observability_init_does_not_import_otel(self):
        script = (
            f"import sys; sys.path.insert(0, {str(ROOT)!r});"
            " import tabularium.engine.observability;"
            " print('opentelemetry' in sys.modules)")
        proc = subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("False", proc.stdout)

    def test_dependency_isolation_production_chain_never_imports_otel(self):
        modules = ["tabularium.engine.context_loader", "tabularium.engine.projection",
                   "tabularium.engine.skills", "tabularium.engine.workers", "tabularium.engine.runtime",
                   "tabularium.engine.execution", "tabularium.engine.persistence",
                   "tabularium.engine.continuation", "tabularium.engine.tools"]
        for module in modules:
            with self.subTest(module=module):
                script = (
                    f"import sys; sys.path.insert(0, {str(ROOT)!r});"
                    f" import {module};"
                    " print('opentelemetry' in sys.modules)")
                proc = subprocess.run([sys.executable, "-c", script],
                                      capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("False", proc.stdout,
                              f"{module} imported OpenTelemetry")

    def test_no_production_module_imports_the_otel_adapter(self):
        import ast
        for package in ("runtime", "execution", "persistence", "continuation",
                        "tools", "skills", "projection", "workers",
                        "context_loader"):
            for module in (ROOT / "tabularium" / "engine" / package).glob("*.py"):
                text = module.read_text(encoding="utf-8")
                self.assertNotIn("observability.otel", text,
                                 f"{package}/{module.name} imports the "
                                 "OTel adapter")

    @needs_otel
    def test_disabled_mode_execution_is_identical(self):
        """Exporting afterwards cannot have changed the execution."""
        traced = self.start(skill_path=self.skill(),
                            collector=InMemoryCollector())
        untraced = self.start(execution_id="exec-2", skill_path=self.skill())
        a, b = traced.to_dict(), untraced.to_dict()
        for key in ("status", "reason_codes", "output", "metrics"):
            self.assertEqual(a[key], b[key])

    def test_is_available_never_raises(self):
        if OTEL:
            self.assertTrue(is_available())
        # With or without the SDK the call must be safe.


# --- end to end -------------------------------------------------------------

@needs_otel
@needs_runtime
class TestEndToEndExport(E2ETestCase, E2EMixin):
    def test_e2e_full_lifecycle_exported(self):
        collector = InMemoryCollector()
        result = self.start(adapter=RecordingWorker(),
                            skill_path=self.skill(), collector=collector)
        self.assertEqual(result.status, P.COMPLETED)

        request = self.write_request()
        TA.run_tool_cycle(request, FixtureWriteTool(self.tool_root),
                          tool_constraints("WRITE"), collector=collector)
        TA.run_tool_cycle(request, FixtureWriteTool(self.tool_root),
                          tool_constraints("WRITE"), self.tool_auth(request),
                          collector=collector)
        prepared = self.continuation(
            self.outcome_from(result),
            ContinuationPlan(handoff=self.handoff_spec()))
        apply_continuation(prepared, self.authorize_all(prepared),
                           str(self.fx.project), task_id="TASK-2026-0001",
                           execution_id="exec-1", collector=collector)

        metrics = TraceMetrics(
            available_context=result.metrics.available_context,
            governed_context=result.metrics.governed_context,
            projected_context=result.metrics.payload_size,
            effective_context=result.metrics.effective_allowed_classes,
            initial_reduction_pct=result.metrics.reduction_pct)

        provider, exporter = in_memory_provider()
        export = export_trace(collector.trace("exec-1"), provider, metrics)
        self.assertTrue(export.healthy, export.failures)

        spans = exporter.get_finished_spans()
        root = next(s for s in spans if s.name.startswith("execution "))
        visible = set(spans_by_name(exporter)) | {e.name for e in root.events}

        for expected in (EV.EXECUTION_STARTED, EV.CONTEXT_LOADED,
                         EV.CONTEXT_GOVERNED, EV.SKILL_APPLIED,
                         EV.CONTEXT_PROJECTED, EV.WORKER_PREPARED,
                         EV.WORKER_STARTED, EV.WORKER_COMPLETED,
                         EV.EXECUTION_COMPLETED, EV.TOOL_REQUESTED,
                         EV.TOOL_AUTHORIZATION_REQUIRED, EV.TOOL_EXECUTED,
                         EV.CONTINUATION_PLANNED, EV.PERSISTENCE_PROPOSED,
                         EV.PERSISTENCE_WRITTEN):
            self.assertIn(expected, visible, f"{expected} not exported")

        self.assertEqual(len({s.context.trace_id for s in spans}), 1)
        self.assertEqual(root.attributes[f"{NS}execution_id"], "exec-1")
        self.assertEqual(root.attributes[f"{NS}available_context.chars"],
                         result.metrics.available_context["chars"])

    def test_e2e_export_writes_nothing_into_the_project(self):
        before = self.ai_snapshot()
        collector = InMemoryCollector()
        self.start(skill_path=self.skill(), collector=collector)
        after_execution = self.ai_snapshot()
        provider, _ = in_memory_provider()
        export_trace(collector.trace("exec-1"), provider)
        self.assertEqual(self.ai_snapshot(), after_execution)
        self.assertEqual(list((self.fx.project / ".ai").rglob("*.jsonl")), [])
        self.assertTrue(set(after_execution) >= set(before))

    def test_e2e_worker_output_and_context_not_exported(self):
        marker_ctx = "UNIQUE-CTX-MARKER-OTEL-31"
        self.fx.doc("constraints.md", f"# Constraints\n\n{marker_ctx}\n")
        collector = InMemoryCollector()
        self.start(adapter=RecordingWorker(output="SECRET-OUTPUT-OTEL-77"),
                   skill_path=self.skill(), collector=collector)
        provider, exporter = in_memory_provider()
        export_trace(collector.trace("exec-1"), provider)
        blob = ""
        for span in exporter.get_finished_spans():
            blob += str(dict(span.attributes))
            blob += str([dict(e.attributes) for e in span.events])
        self.assertNotIn(marker_ctx, blob)
        self.assertNotIn("SECRET-OUTPUT-OTEL-77", blob)


# --- process death ----------------------------------------------------------

@needs_otel
@needs_runtime
class TestProcessDeathExport(E2ETestCase, E2EMixin):
    def test_process_death_exports_as_one_otel_trace(self):
        """Exporting a finished trace gives ONE OTel trace across processes.

        The events were written by two different processes; export happens once,
        afterwards, so a single root span covers both — no context propagation
        needed.
        """
        path = Path(self.workdir) / "trace.jsonl"
        collector = JsonlCollector(path, project_root=self.fx.project)
        self.start(skill_path=self.skill(approval_required=True),
                   collector=collector)

        script = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
from tabularium.engine.execution import inspect_execution, resume_execution
from tabularium.engine.observability import JsonlCollector
from tabularium.engine.runtime.approval import HumanApprovalResolution
from tests.e2e_fixture import RecordingWorker
c = JsonlCollector({str(path)!r})
pending = inspect_execution("exec-1", {self.db!r}).approval_request
resume_execution("exec-1", {self.db!r},
    HumanApprovalResolution(approval_request_id=pending["request_id"],
                            execution_id="exec-1", decision="APPROVE",
                            decided_by={ACTOR!r}, decided_at={WHEN!r}),
    adapter=RecordingWorker(), task_id="TASK-2026-0001", collector=c)
"""
        proc = subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        combined = JsonlCollector(path).read("exec-1")
        provider, exporter = in_memory_provider()
        export = export_trace(combined, provider)
        self.assertTrue(export.healthy)
        spans = exporter.get_finished_spans()
        self.assertEqual(len({s.context.trace_id for s in spans}), 1,
                         "a process death split the OTel trace")
        root = next(s for s in spans if s.name.startswith("execution "))
        self.assertEqual(root.attributes[f"{NS}execution_id"], "exec-1")

    def test_process_death_tabu_correlation_is_mandatory(self):
        path = Path(self.workdir) / "trace.jsonl"
        collector = JsonlCollector(path, project_root=self.fx.project)
        self.start(skill_path=self.skill(), collector=collector)
        combined = JsonlCollector(path).read("exec-1")
        self.assertTrue(combined.events)
        self.assertEqual({e.execution_id for e in combined.events},
                         {"exec-1"})


# --- otel is not tabularium storage ----------------------------------------------

@needs_otel
@needs_runtime
class TestOtelIsNotTabulariumStorage(E2ETestCase, E2EMixin):
    def test_discarding_otel_data_leaves_tabu_trace_and_checkpoint(self):
        path = Path(self.workdir) / "trace.jsonl"
        collector = JsonlCollector(path, project_root=self.fx.project)
        self.start(skill_path=self.skill(approval_required=True),
                   collector=collector)
        provider, exporter = in_memory_provider()
        export_trace(JsonlCollector(path).read("exec-1"), provider)

        # Throw away everything OTel captured.
        exporter.clear()
        self.assertEqual(exporter.get_finished_spans(), ())

        # The Tabularium trace survives...
        self.assertTrue(JsonlCollector(path).read("exec-1").events)
        # ...and so does runtime authority.
        from tabularium.engine.execution import inspect_execution, resume_execution
        from tabularium.engine.runtime.approval import HumanApprovalResolution
        pending = inspect_execution("exec-1", self.db).approval_request
        result = resume_execution("exec-1", self.db, HumanApprovalResolution(
            approval_request_id=pending["request_id"], execution_id="exec-1",
            decision="APPROVE", decided_by=ACTOR, decided_at=WHEN),
            adapter=RecordingWorker(), task_id="TASK-2026-0001")
        self.assertEqual(result.status, P.COMPLETED)


if __name__ == "__main__":
    unittest.main()
