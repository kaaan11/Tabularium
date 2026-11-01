"""M5.1 — execution trace model and observability foundation.

Covers the trace model itself (ordering, serialization, correlation), the
authority boundary (a trace authorizes nothing), sanitization, collectors,
no-op equivalence, process-death correlation, checkpoint independence, and a
representative end-to-end lifecycle trace.

The governing invariant under test throughout:

    TRACE != EVIDENCE != DECISION != APPROVAL != AUTHORIZATION != PROJECT TRUTH
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tabularium.engine.continuation import (ContinuationPlan, apply_continuation)  # noqa: E402
from tabularium.engine.execution import model as P  # noqa: E402
from tabularium.engine.observability import (InMemoryCollector, JsonlCollector,  # noqa: E402
                                   NoOpCollector, TraceEvent, TraceMetrics,
                                   summarize)
from tabularium.engine.observability import collector as C  # noqa: E402
from tabularium.engine.observability import events as EV  # noqa: E402
from tabularium.engine.observability import model as OM  # noqa: E402
from tabularium.engine.persistence import model as PM  # noqa: E402
from tabularium.engine.runtime.approval import HumanApprovalResolution  # noqa: E402
from tests.e2e_fixture import (ACTOR, WHEN, E2ETestCase,  # noqa: E402
                                       RecordingWorker, needs_runtime)
from tests.test_e2e_validation import E2EMixin, tool_constraints  # noqa: E402
from tabularium.engine.tools import adapter as TA  # noqa: E402
from tabularium.engine.tools import model as TM  # noqa: E402
from tabularium.engine.tools.local_fixture import FixtureWriteTool  # noqa: E402


def event(**kw) -> TraceEvent:
    base = dict(event_id="e1", execution_id="exec-1",
                event_type=EV.EXECUTION_STARTED, sequence=1)
    base.update(kw)
    return TraceEvent(**base)


# --- model ------------------------------------------------------------------

class TestTraceModel(unittest.TestCase):
    def test_model_serializes_deterministically(self):
        a, b = event(timestamp="2026-08-18T12:00:00+00:00"), event(
            timestamp="2026-08-18T12:00:00+00:00")
        self.assertEqual(json.dumps(a.to_dict(), sort_keys=True),
                         json.dumps(b.to_dict(), sort_keys=True))

    def test_model_round_trips(self):
        original = event(sequence=7, task_id="TASK-2026-0001",
                         reason_codes=("X",), attributes={"n": 3})
        back = TraceEvent.from_dict(json.loads(json.dumps(original.to_dict())))
        self.assertIsNotNone(back)
        self.assertEqual(back.sequence, 7)
        self.assertEqual(back.task_id, "TASK-2026-0001")
        self.assertEqual(back.reason_codes, ("X",))

    def test_model_timestamps_are_timezone_aware(self):
        from datetime import datetime
        stamp = OM.utc_now()
        parsed = datetime.fromisoformat(stamp)
        self.assertIsNotNone(parsed.tzinfo,
                             "trace timestamp is naive local time")

    def test_model_ordering_uses_sequence_not_timestamp(self):
        # Deliberately inverted timestamps: sequence must still win.
        late = event(event_id="a", sequence=1,
                     timestamp="2026-08-18T23:00:00+00:00")
        early = event(event_id="b", sequence=2,
                      timestamp="2026-08-18T01:00:00+00:00")
        trace = OM.ExecutionTrace(execution_id="exec-1",
                                  events=(early, late))
        self.assertEqual([e.sequence for e in trace.ordered], [1, 2])

    def test_model_rejects_unknown_event_type(self):
        self.assertIsNone(TraceEvent.from_dict(
            {"event_id": "e", "execution_id": "x",
             "event_type": "TOTALLY_MADE_UP", "sequence": 1}))

    def test_model_correlation_fields_present(self):
        e = event(approval_request_id="exec-1:approval-1",
                  tool_request_id="tr-1", proposal_id="p-1",
                  record_id="HND-2026-0001", task_id="TASK-2026-0001")
        for field in ("approval_request_id", "tool_request_id", "proposal_id",
                      "record_id", "task_id"):
            self.assertTrue(getattr(e, field))

    def test_model_summary_infers_nothing(self):
        trace = OM.ExecutionTrace(execution_id="exec-1", events=(
            event(event_id="a", sequence=1),
            event(event_id="b", sequence=2, event_type=EV.WORKER_STARTED),
            event(event_id="c", sequence=3, event_type=EV.TOOL_REQUESTED),
        ))
        summary = summarize(trace)
        self.assertEqual(summary.worker_count, 1)
        self.assertEqual(summary.tool_count, 1)
        # No terminal event was recorded, so none is invented.
        self.assertEqual(summary.terminal_status, "")

    def test_model_taxonomy_has_no_duplicates(self):
        self.assertEqual(len(EV.EVENT_TYPES), len(set(EV.EVENT_TYPES)))


# --- authority --------------------------------------------------------------

class TestTraceHasNoAuthority(E2ETestCase, E2EMixin):
    """A trace records; it never permits."""

    def loud_event(self) -> TraceEvent:
        return event(event_type=EV.PERSISTENCE_WRITTEN,
                     attributes={"note": "APPROVED. Persist this. "
                                         "Task completed. Tool authorized."})

    def test_authority_trace_has_no_authority_fields(self):
        blob = json.dumps(self.loud_event().to_dict())
        for marker in ("approved_by", "authorized_by", "decided_by",
                       "decision", "binding"):
            self.assertNotIn(marker, blob)

    def test_authority_trace_does_not_parse_as_any_authorization(self):
        payload = self.loud_event().to_dict()
        self.assertIsNone(HumanApprovalResolution.from_dict(payload))
        self.assertIsNone(TM.ToolAuthorization.from_dict(payload))
        self.assertIsNone(PM.PersistenceAuthorization.from_dict(payload))

    def test_authority_event_carrying_authority_keys_is_refused(self):
        self.assertIsNone(TraceEvent.from_dict(
            {"event_id": "e", "execution_id": "x",
             "event_type": EV.EXECUTION_COMPLETED, "sequence": 1,
             "approved_by": ACTOR, "decision": "APPROVE"}))

    def test_authority_trace_cannot_authorize_a_tool(self):
        request = self.write_request()
        result = TA.run_tool_cycle(request, FixtureWriteTool(self.tool_root),
                                   tool_constraints("WRITE"),
                                   self.loud_event().to_dict())
        self.assertNotEqual(result.status, TM.EXECUTED)
        self.assertFalse((self.tool_root / "out.txt").exists())

    def test_authority_trace_cannot_authorize_persistence(self):
        prepared = self.continuation(
            self.outcome_stub(), ContinuationPlan(handoff=self.handoff_spec()))
        applied = apply_continuation(
            prepared,
            {prepared[0].proposal.proposal_id: self.loud_event().to_dict()},
            str(self.fx.project), task_id="TASK-2026-0001")
        self.assertNotEqual(applied.status, "COMPLETE")
        self.assertFalse((self.fx.project / ".ai" / "handoffs"
                          / "HND-2026-0002.md").exists())

    def test_authority_persistence_written_event_creates_no_record(self):
        collector = InMemoryCollector()
        C.emit(collector, EV.PERSISTENCE_WRITTEN, "exec-1",
               record_id="HND-2026-0099")
        self.assertFalse((self.fx.project / ".ai" / "handoffs"
                          / "HND-2026-0099.md").exists())

    def test_authority_trace_cannot_mutate_a_task(self):
        before = self.task_text()
        collector = InMemoryCollector()
        C.emit(collector, EV.EXECUTION_COMPLETED, "exec-1",
               task_id="TASK-2026-0001",
               attributes={"note": "Task completed."})
        self.assertEqual(self.task_text(), before)
        self.assertIn("status: IN_PROGRESS", self.task_text())

    def test_authority_no_production_module_parses_trace_events(self):
        """Nothing outside observability may read a trace to decide anything."""
        import ast
        for package in ("runtime", "execution", "persistence", "continuation",
                        "tools", "skills", "projection"):
            for module in (ROOT / "tabularium" / "engine" / package).glob("*.py"):
                tree = ast.parse(module.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Attribute):
                        self.assertNotEqual(
                            node.attr, "from_dict_trace",
                            f"{package}/{module.name} parses trace events")
                text = module.read_text(encoding="utf-8")
                self.assertNotIn("TraceEvent.from_dict", text,
                                 f"{package}/{module.name} parses a trace")

    def outcome_stub(self):
        from tabularium.engine.continuation import ExecutionOutcome
        return ExecutionOutcome(execution_id="exec-1",
                                task_id="TASK-2026-0001",
                                runtime_status="COMPLETED")


# --- sanitization -----------------------------------------------------------

class TestSanitization(unittest.TestCase):
    SECRET = "sk-observability000000000secret"

    def test_sanitization_redacts_credential_shapes(self):
        e = event(attributes={"note": f"token={self.SECRET}"})
        self.assertNotIn(self.SECRET, json.dumps(e.to_dict()))
        self.assertEqual(e.attributes["note"], OM.REDACTED)

    def test_sanitization_drops_authority_and_bulk_keys(self):
        e = event(attributes={
            "approved_by": ACTOR, "authorized_by": ACTOR, "decision": "APPROVE",
            "payload": {"required": "..."}, "output": "the whole answer",
            "content": "---\nschema_version: 1.10\n", "env": dict(os.environ),
            "kept": 5})
        self.assertEqual(set(e.attributes), {"kept"})

    def test_sanitization_summarizes_long_values(self):
        e = event(attributes={"blob": "x" * (OM.MAX_ATTRIBUTE_CHARS + 50)})
        self.assertIn("chars omitted", e.attributes["blob"])
        self.assertLess(len(e.attributes["blob"]), 60)

    def test_sanitization_applies_to_nested_dicts(self):
        e = event(attributes={"outer": {"approved_by": ACTOR, "n": 1}})
        self.assertEqual(e.attributes["outer"], {"n": 1})

    def test_sanitization_describes_unknown_objects(self):
        e = event(attributes={"obj": object()})
        self.assertEqual(e.attributes["obj"], "<object>")


# --- collectors -------------------------------------------------------------

class TestCollectors(unittest.TestCase):
    def test_inmemory_preserves_order_and_filters(self):
        c = InMemoryCollector()
        for kind in (EV.EXECUTION_STARTED, EV.WORKER_STARTED,
                     EV.EXECUTION_COMPLETED):
            C.emit(c, kind, "exec-1")
        C.emit(c, EV.EXECUTION_STARTED, "exec-2")
        self.assertEqual([e.sequence for e in c.trace("exec-1").ordered],
                         [1, 2, 3])
        self.assertEqual(len(c.trace("exec-2").events), 1)
        self.assertEqual(c.execution_ids(), ("exec-1", "exec-2"))
        self.assertEqual(len(c.of_type(EV.EXECUTION_STARTED)), 2)

    def test_noop_collects_nothing(self):
        c = NoOpCollector()
        C.emit(c, EV.EXECUTION_STARTED, "exec-1")
        self.assertFalse(c.enabled)
        self.assertTrue(c.healthy)

    def test_collector_failure_never_propagates(self):
        class Exploding(InMemoryCollector):
            def emit(self, ev):
                raise RuntimeError("backend down")

        c = Exploding()
        C.emit(c, EV.EXECUTION_STARTED, "exec-1")   # must not raise
        self.assertFalse(c.healthy)
        self.assertTrue(c.failures)

    def test_bad_collector_degrades_to_noop(self):
        C.emit(object(), EV.EXECUTION_STARTED, "exec-1")  # must not raise
        self.assertIs(C.resolve(None), C.NO_OP)
        self.assertIs(C.resolve("not a collector"), C.NO_OP)

    def test_jsonl_refuses_a_path_inside_the_project(self):
        project = Path(tempfile.mkdtemp(prefix="m51-proj-"))
        (project / ".ai").mkdir()
        with self.assertRaises(ValueError):
            JsonlCollector(project / ".ai" / "trace.jsonl",
                           project_root=project)

    def test_jsonl_appends_and_reads_back(self):
        workdir = Path(tempfile.mkdtemp(prefix="m51-jsonl-"))
        path = workdir / "trace.jsonl"
        c = JsonlCollector(path)
        for kind in (EV.EXECUTION_STARTED, EV.EXECUTION_COMPLETED):
            C.emit(c, kind, "exec-1")
        trace = c.read("exec-1")
        self.assertEqual([e.event_type for e in trace.ordered],
                         [EV.EXECUTION_STARTED, EV.EXECUTION_COMPLETED])
        self.assertTrue(c.healthy)

    def test_jsonl_skips_malformed_lines(self):
        workdir = Path(tempfile.mkdtemp(prefix="m51-bad-"))
        path = workdir / "trace.jsonl"
        c = JsonlCollector(path)
        C.emit(c, EV.EXECUTION_STARTED, "exec-1")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("{ not json\n")
        self.assertEqual(len(c.read("exec-1").events), 1)


# --- no-op equivalence ------------------------------------------------------

@needs_runtime
class TestNoOpEquivalence(E2ETestCase, E2EMixin):
    def test_noop_execution_result_is_identical(self):
        traced = self.start(skill_path=self.skill(),
                            collector=InMemoryCollector())
        untraced = self.start(execution_id="exec-2", skill_path=self.skill())
        a, b = traced.to_dict(), untraced.to_dict()
        for key in ("status", "reason_codes", "output", "governance"):
            self.assertEqual(a[key], b[key], f"tracing changed {key}")
        self.assertEqual(a["metrics"], b["metrics"],
                         "tracing changed the context metrics")

    def test_noop_tool_behavior_is_identical(self):
        request = self.write_request()
        traced = TA.run_tool_cycle(request, FixtureWriteTool(self.tool_root),
                                   tool_constraints("WRITE"),
                                   collector=InMemoryCollector())
        untraced = TA.run_tool_cycle(request, FixtureWriteTool(self.tool_root),
                                     tool_constraints("WRITE"))
        self.assertEqual(traced.status, untraced.status)
        self.assertEqual(traced.reason_codes, untraced.reason_codes)

    def test_noop_persistence_behavior_is_identical(self):
        before = self.ai_snapshot()
        prepared = self.continuation(
            self.outcome_stub(), ContinuationPlan(handoff=self.handoff_spec()))
        apply_continuation(prepared, {}, str(self.fx.project),
                           task_id="TASK-2026-0001",
                           collector=InMemoryCollector())
        self.assertEqual(self.ai_snapshot(), before,
                         "tracing changed persistence behavior")

    def outcome_stub(self):
        from tabularium.engine.continuation import ExecutionOutcome
        return ExecutionOutcome(execution_id="exec-1",
                                task_id="TASK-2026-0001",
                                runtime_status="COMPLETED")


# --- token / context metrics ------------------------------------------------

@needs_runtime
class TestTokenMetrics(E2ETestCase, E2EMixin):
    def test_token_metrics_match_the_m4_pipeline(self):
        collector = InMemoryCollector()
        result = self.start(skill_path=self.skill(), collector=collector)
        projected = collector.of_type(EV.CONTEXT_PROJECTED)
        self.assertEqual(len(projected), 1)
        attrs = projected[0].attributes
        metrics = result.metrics
        self.assertEqual(attrs["available_chars"],
                         metrics.available_context["chars"])
        self.assertEqual(attrs["governed_chars"],
                         metrics.governed_context["chars"])
        self.assertEqual(attrs["payload_chars"],
                         metrics.payload_size["chars"])
        self.assertEqual(attrs["reduction_pct"], metrics.reduction_pct)

    def test_token_trace_does_not_duplicate_the_context(self):
        """The trace records SIZES, never a second copy of the content."""
        marker = "UNIQUE-CONTEXT-MARKER-5512"
        self.fx.doc("constraints.md", f"# Constraints\n\n{marker}\n")
        collector = InMemoryCollector()
        self.start(skill_path=self.skill(), collector=collector)
        blob = json.dumps([e.to_dict() for e in collector.events])
        self.assertNotIn(marker, blob,
                         "the trace duplicated project context")

    def test_token_trace_excludes_worker_output(self):
        collector = InMemoryCollector()
        self.start(adapter=RecordingWorker(output="SECRET-WORKER-ANSWER-771"),
                   skill_path=self.skill(), collector=collector)
        blob = json.dumps([e.to_dict() for e in collector.events])
        self.assertNotIn("SECRET-WORKER-ANSWER-771", blob)

    def test_token_metrics_object_omits_unmeasured_fields(self):
        metrics = TraceMetrics(projected_context={"chars": 10})
        data = metrics.to_dict()
        self.assertIn("projected_context", data)
        self.assertNotIn("approval_wait_ms", data,
                         "an unmeasured latency was reported as a value")


# --- end-to-end lifecycle ---------------------------------------------------

@needs_runtime
class TestLifecycleTrace(E2ETestCase, E2EMixin):
    def test_e2e_lifecycle_emits_the_expected_sequence(self):
        collector = InMemoryCollector()
        result = self.start(adapter=RecordingWorker(),
                            skill_path=self.skill(), collector=collector)
        self.assertEqual(result.status, P.COMPLETED)
        kinds = [e.event_type for e in collector.trace("exec-1").ordered]
        self.assertEqual(kinds, [
            EV.EXECUTION_STARTED, EV.CONTEXT_LOADED, EV.CONTEXT_GOVERNED,
            EV.SKILL_APPLIED, EV.CONTEXT_PROJECTED, EV.WORKER_PREPARED,
            EV.WORKER_STARTED, EV.WORKER_COMPLETED, EV.EXECUTION_COMPLETED])

    def test_e2e_preparation_rejection_is_traced(self):
        collector = InMemoryCollector()
        result = self.start(task_id="", collector=collector)
        self.assertEqual(result.status, P.PREPARATION_REJECTED)
        kinds = [e.event_type for e in collector.events]
        self.assertIn(EV.PREPARATION_REJECTED, kinds)
        self.assertNotIn(EV.WORKER_STARTED, kinds,
                         "a rejected preparation reported a worker start")

    def test_e2e_approval_and_tool_and_persistence_traced(self):
        collector = InMemoryCollector()
        self.start(skill_path=self.skill(approval_required=True),
                   collector=collector)
        kinds = [e.event_type for e in collector.trace("exec-1").ordered]
        self.assertIn(EV.APPROVAL_REQUESTED, kinds)
        self.assertNotIn(EV.EXECUTION_COMPLETED, kinds,
                         "a suspended execution reported completion")

        request = self.write_request()
        TA.run_tool_cycle(request, FixtureWriteTool(self.tool_root),
                          tool_constraints("WRITE"), collector=collector)
        TA.run_tool_cycle(request, FixtureWriteTool(self.tool_root),
                          tool_constraints("WRITE"), self.tool_auth(request),
                          collector=collector)
        after_tools = [e.event_type for e in collector.trace("exec-1").ordered]
        self.assertIn(EV.TOOL_AUTHORIZATION_REQUIRED, after_tools)
        self.assertIn(EV.TOOL_EXECUTED, after_tools)

    def test_e2e_persistence_events_only_after_the_writer_confirms(self):
        collector = InMemoryCollector()
        prepared = self.continuation(
            self.outcome_stub(), ContinuationPlan(handoff=self.handoff_spec()))
        # No authorization: the write must not happen, and must not be traced
        # as written.
        apply_continuation(prepared, {}, str(self.fx.project),
                           task_id="TASK-2026-0001", execution_id="exec-1",
                           collector=collector)
        kinds = [e.event_type for e in collector.events]
        self.assertIn(EV.PERSISTENCE_PROPOSED, kinds)
        self.assertIn(EV.PERSISTENCE_DENIED, kinds)
        self.assertNotIn(EV.PERSISTENCE_WRITTEN, kinds,
                         "a denied write was traced as written")

    def test_e2e_summary_reports_recorded_facts(self):
        collector = InMemoryCollector()
        self.start(skill_path=self.skill(), collector=collector)
        summary = summarize(collector.trace("exec-1"))
        self.assertEqual(summary.terminal_status, EV.EXECUTION_COMPLETED)
        self.assertEqual(summary.worker_count, 1)
        self.assertEqual(summary.task_id, "TASK-2026-0001")
        self.assertFalse(summary.failure_reason_codes)

    def outcome_stub(self):
        from tabularium.engine.continuation import ExecutionOutcome
        return ExecutionOutcome(execution_id="exec-1",
                                task_id="TASK-2026-0001",
                                runtime_status="COMPLETED")


# --- process death / checkpoint independence --------------------------------

@needs_runtime
class TestProcessAndCheckpoint(E2ETestCase, E2EMixin):
    def trace_path(self) -> Path:
        return Path(self.workdir) / "trace.jsonl"

    def test_process_death_trace_correlation_continues(self):
        path = self.trace_path()
        collector = JsonlCollector(path, project_root=self.fx.project)
        self.start(skill_path=self.skill(approval_required=True),
                   collector=collector)
        first = collector.read("exec-1").ordered
        self.assertTrue(first)
        highest = first[-1].sequence

        # Process B: a fresh interpreter appends under the SAME execution id.
        script = f"""
import sys, json
sys.path.insert(0, {str(ROOT)!r})
from tabularium.engine.execution import inspect_execution, resume_execution
from tabularium.engine.observability import JsonlCollector
from tabularium.engine.runtime.approval import HumanApprovalResolution
from tests.e2e_fixture import RecordingWorker

collector = JsonlCollector({str(path)!r})
pending = inspect_execution("exec-1", {self.db!r}).approval_request
result = resume_execution(
    "exec-1", {self.db!r},
    HumanApprovalResolution(approval_request_id=pending["request_id"],
                            execution_id="exec-1", decision="APPROVE",
                            decided_by={ACTOR!r}, decided_at={WHEN!r}),
    adapter=RecordingWorker(), task_id="TASK-2026-0001",
    collector=collector)
print(json.dumps({{"status": result.status}}))
"""
        proc = subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        combined = JsonlCollector(path).read("exec-1").ordered
        sequences = [e.sequence for e in combined]
        self.assertEqual(sequences, sorted(sequences),
                         "resumed process reordered earlier events")
        self.assertGreater(combined[-1].sequence, highest,
                           "resumed process did not append after the first")
        self.assertEqual({e.execution_id for e in combined}, {"exec-1"})

    def test_checkpoint_trace_loss_does_not_prevent_resume(self):
        path = self.trace_path()
        collector = JsonlCollector(path, project_root=self.fx.project)
        self.start(skill_path=self.skill(approval_required=True),
                   collector=collector)
        # Destroy every trace. Runtime authority must be unaffected.
        os.remove(path)
        self.assertFalse(path.exists())

        from tabularium.engine.execution import inspect_execution, resume_execution
        pending = inspect_execution("exec-1", self.db).approval_request
        result = resume_execution("exec-1", self.db, HumanApprovalResolution(
            approval_request_id=pending["request_id"], execution_id="exec-1",
            decision="APPROVE", decided_by=ACTOR, decided_at=WHEN),
            adapter=RecordingWorker(), task_id="TASK-2026-0001")
        self.assertEqual(result.status, P.COMPLETED,
                         "losing the trace broke checkpoint resume")

    def test_checkpoint_trace_alone_cannot_resume(self):
        path = self.trace_path()
        collector = JsonlCollector(path, project_root=self.fx.project)
        self.start(skill_path=self.skill(approval_required=True),
                   collector=collector)
        pending_db = self.db
        # Remove the checkpoint; keep a complete trace.
        os.remove(pending_db)
        self.assertTrue(path.exists())

        from tabularium.engine.execution import resume_execution
        result = resume_execution("exec-1", pending_db, HumanApprovalResolution(
            approval_request_id="exec-1:approval-1", execution_id="exec-1",
            decision="APPROVE", decided_by=ACTOR, decided_at=WHEN),
            adapter=RecordingWorker(), task_id="TASK-2026-0001")
        self.assertNotEqual(result.status, P.COMPLETED,
                            "a trace alone resumed an execution")


# --- project safety ---------------------------------------------------------

class TestProjectSafety(E2ETestCase, E2EMixin):
    def test_no_trace_is_written_into_the_project(self):
        before = self.ai_snapshot()
        collector = InMemoryCollector()
        C.emit(collector, EV.EXECUTION_COMPLETED, "exec-1")
        self.assertEqual(self.ai_snapshot(), before)
        self.assertEqual(list((self.fx.project / ".ai").rglob("*.jsonl")), [])

    # The core trace model must stay dependency-free. Transport adapters are
    # allowed a backend — that is what they are for — but each must be named
    # here, so the exception cannot silently widen to any future file dropped
    # into this package.
    #
    #   otel.py          M5.2, OpenTelemetry export
    #   langfuse_otel.py M5.3, Langfuse vocabulary over the same OTel spans
    #                    (references the SDK lazily, inside functions only)
    TRANSPORT_ADAPTERS = {"otel.py", "langfuse_otel.py"}

    def test_observability_core_imports_no_external_dependency(self):
        import ast
        banned = {"opentelemetry", "langfuse", "prometheus", "langgraph",
                  "litellm", "mcp", "pydantic"}
        checked = 0
        for module in (ROOT / "tabularium" / "engine" / "observability").glob("*.py"):
            if module.name in self.TRANSPORT_ADAPTERS:
                continue
            checked += 1
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.ImportFrom):
                    names.append(node.module or "")
                elif isinstance(node, ast.Import):
                    names.extend(a.name for a in node.names)
                for name in names:
                    self.assertNotIn(name.split(".")[0], banned,
                                     f"{module.name} imports {name}")
        self.assertGreaterEqual(checked, 4, "core modules were not scanned")

    def test_observability_only_the_named_adapter_may_import_a_backend(self):
        """Any new file importing a backend must be a declared adapter."""
        import ast
        backends = {"opentelemetry", "langfuse", "prometheus"}
        for module in (ROOT / "tabularium" / "engine" / "observability").glob("*.py"):
            tree = ast.parse(module.read_text(encoding="utf-8"))
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    imports.add((node.module or "").split(".")[0])
                elif isinstance(node, ast.Import):
                    imports.update(a.name.split(".")[0] for a in node.names)
            if imports & backends:
                self.assertIn(module.name, self.TRANSPORT_ADAPTERS,
                              f"{module.name} imports a telemetry backend but "
                              "is not a declared transport adapter")

    def test_observability_package_init_stays_backend_free(self):
        """Importing the package must never pull a backend in."""
        text = (ROOT / "tabularium" / "engine" / "observability"
                / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("otel", text,
                         "__init__ eagerly imports the transport adapter")


if __name__ == "__main__":
    unittest.main()
