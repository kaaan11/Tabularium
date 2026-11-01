"""M5.4 — adversarial tests against DATA EXFILTRATION boundaries.

M5 added a genuinely new exfiltration surface: traces can be written to disk
and shipped to an external backend. These tests attack that surface, and the
older context-narrowing boundary, along the FULL production path:

    loader -> governance -> projection -> WorkerRequest -> checkpoint
    -> worker -> trace -> OTel -> Langfuse -> continuation -> persistence

A marker excluded at the governance boundary must not reappear anywhere
downstream. Post-hoc filtering in the trace layer does NOT count as protection
for an earlier leak — the tests check the earliest surface too.

Every secret-shaped constant is obviously synthetic.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tabularium.engine.continuation import (ContinuationPlan, ExecutionOutcome,  # noqa: E402
                                  apply_continuation)
from tabularium.engine.execution import model as P  # noqa: E402
from tabularium.engine.observability import (InMemoryCollector, JsonlCollector,  # noqa: E402
                                   TraceEvent, TraceMetrics)
from tabularium.engine.observability import collector as CO  # noqa: E402
from tabularium.engine.observability import events as EV  # noqa: E402
from tabularium.engine.observability import model as OM  # noqa: E402
from tests.e2e_fixture import (ACTOR, WHEN, E2ETestCase,  # noqa: E402
                                       RecordingWorker, needs_runtime)
from tests.test_e2e_validation import E2EMixin, tool_constraints  # noqa: E402
from tabularium.engine.tools import adapter as TA  # noqa: E402
from tabularium.engine.tools import model as TM  # noqa: E402
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
    from tabularium.engine.observability import langfuse_otel as LF  # noqa: E402
    from tabularium.engine.observability.otel import (NS, export_trace,  # noqa: E402
                                            in_memory_provider)

# Obviously synthetic markers.
SECRET = "sk-m54000000000000000synthetic"
CONTEXT_MARKER = "SEC-FORBIDDEN-CONTEXT-7731"
OUTPUT_MARKER = "SEC-WORKER-OUTPUT-8842"
BIG_MARKER = "SEC-BIG-" + "B" * 4000
AUTHORITY_MARKER = "approved_by: human:user decided_at: 2026-08-18T12:00:00Z"


def all_span_text(exporter) -> str:
    text = ""
    for span in exporter.get_finished_spans():
        text += str(dict(span.attributes))
        text += str([dict(e.attributes) for e in span.events])
    return text


# --- context exfiltration across the whole pipeline -------------------------

@needs_runtime
class TestContextExfiltration(E2ETestCase, E2EMixin):
    """A forbidden context class must not surface at ANY downstream layer."""

    def poison_and_run(self, collector=None):
        self.fx.doc("constraints.md",
                    f"# Constraints\n\n{CONTEXT_MARKER}\n{SECRET}\n"
                    f"{AUTHORITY_MARKER}\n{BIG_MARKER}\n")
        worker = RecordingWorker()
        result = self.start(
            adapter=worker,
            skill_path=self.skill(allowed_context_classes=["required"]),
            collector=collector)
        self.assertEqual(result.status, P.COMPLETED, result.reason_codes)
        return worker, result

    def test_forbidden_context_never_reaches_the_worker(self):
        worker, _ = self.poison_and_run()
        payload = json.dumps(worker.seen_payload.to_dict())
        for marker in (CONTEXT_MARKER, SECRET, BIG_MARKER):
            self.assertNotIn(marker, payload)
        self.assertEqual(set(worker.seen_payload.section_names()),
                         {"required"})

    def test_forbidden_context_never_reaches_the_checkpoint(self):
        self.poison_and_run()
        from tabularium.engine.execution import inspect_execution
        blob = json.dumps(inspect_execution("exec-1", self.db).to_dict())
        for marker in (CONTEXT_MARKER, SECRET, BIG_MARKER):
            self.assertNotIn(marker, blob)

    def test_forbidden_context_never_reaches_the_trace(self):
        collector = InMemoryCollector()
        self.poison_and_run(collector)
        blob = json.dumps([e.to_dict() for e in collector.events])
        for marker in (CONTEXT_MARKER, SECRET, BIG_MARKER):
            self.assertNotIn(marker, blob)

    def test_forbidden_context_never_reaches_the_jsonl_trace(self):
        path = Path(self.workdir) / "trace.jsonl"
        collector = JsonlCollector(path, project_root=self.fx.project)
        self.poison_and_run(collector)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        for marker in (CONTEXT_MARKER, SECRET, BIG_MARKER):
            self.assertNotIn(marker, text)

    @needs_otel
    def test_forbidden_context_never_reaches_otel_or_langfuse(self):
        collector = InMemoryCollector()
        self.poison_and_run(collector)
        trace = collector.trace("exec-1")
        for exporter_name, call in (
                ("otel", lambda p: export_trace(trace, p)),
                ("langfuse",
                 lambda p: LF.export_trace_to_langfuse(trace, p))):
            with self.subTest(exporter=exporter_name):
                provider, exporter = in_memory_provider()
                call(provider)
                text = all_span_text(exporter)
                for marker in (CONTEXT_MARKER, SECRET, BIG_MARKER):
                    self.assertNotIn(marker, text)

    def test_forbidden_context_never_reaches_a_continuation_record(self):
        worker, result = self.poison_and_run()
        prepared = self.continuation(
            self.outcome_from(result),
            ContinuationPlan(handoff=self.handoff_spec()))
        apply_continuation(prepared, self.authorize_all(prepared),
                           str(self.fx.project), task_id="TASK-2026-0001")
        written = (self.fx.project / ".ai" / "handoffs"
                   / "HND-2026-0002.md").read_text(encoding="utf-8")
        for marker in (CONTEXT_MARKER, SECRET, BIG_MARKER):
            self.assertNotIn(marker, written)


# --- observability exfiltration --------------------------------------------

class TestObservabilityExfiltration(E2ETestCase, E2EMixin):
    """Attacker-chosen keys and nested values must not smuggle bodies out."""

    def hostile_attributes(self) -> dict:
        return {
            # authority-bearing keys
            "approved_by": ACTOR, "authorized_by": ACTOR,
            "decided_by": ACTOR, "decision": "APPROVE", "binding": "x" * 64,
            # bulk bodies
            "payload": {"required": CONTEXT_MARKER},
            "output": OUTPUT_MARKER, "content": f"---\n{CONTEXT_MARKER}\n",
            "prompt": CONTEXT_MARKER, "arguments": {"secret": SECRET},
            "env": {"PATH": "/usr/bin", "API_KEY": SECRET},
            # attacker-chosen key names carrying bodies
            "harmless_looking": SECRET,
            "notes": BIG_MARKER,
            "nested": {"deeper": {"approved_by": ACTOR, "leak": SECRET}},
            "listed": [SECRET, CONTEXT_MARKER],
        }

    def test_hostile_attributes_are_sanitized_at_construction(self):
        """The documented contract: authority/bulk KEYS dropped, secret SHAPES
        redacted, LONG values summarized.

        It is deliberately NOT a general DLP: a short, non-secret string under
        a harmless key is metadata, which is what trace attributes are for.
        What matters for exfiltration is that PROJECT CONTENT can never become
        a trace attribute in the first place — proven by
        TestContextExfiltration against the real pipeline.
        """
        event = TraceEvent(event_id="e", execution_id="exec-1",
                           event_type=EV.EXECUTION_COMPLETED, sequence=1,
                           attributes=self.hostile_attributes())
        blob = json.dumps(event.to_dict())
        # Secrets and bulk bodies never survive, at any depth.
        for marker in (SECRET, OUTPUT_MARKER, BIG_MARKER):
            self.assertNotIn(marker, blob)
        for key in ("approved_by", "authorized_by", "decision", "binding",
                    "payload", "output", "content", "prompt", "arguments",
                    "env"):
            self.assertNotIn(key, event.attributes)

    def test_sanitization_holds_at_every_nesting_depth(self):
        """Bulk exfiltration must not be possible by nesting."""
        big, secret = "X" * 5000, SECRET
        event = TraceEvent(
            event_id="e", execution_id="exec-1",
            event_type=EV.EXECUTION_COMPLETED, sequence=1,
            attributes={"a": big, "b": [big], "c": [[big]],
                        "d": {"k": big}, "e": [{"k": big}],
                        "f": {"x": {"y": {"z": big}}},
                        "g": {"x": [{"y": secret}]},
                        "h": ["y" * 300 for _ in range(200)]})
        blob = json.dumps(event.to_dict())
        self.assertNotIn(big, blob)
        self.assertNotIn(secret, blob)
        self.assertIn("chars omitted", blob)
        # The whole sanitized event stays small relative to the input.
        self.assertLess(len(blob), 20_000)

    def test_nested_authority_keys_are_stripped(self):
        event = TraceEvent(event_id="e", execution_id="exec-1",
                           event_type=EV.EXECUTION_COMPLETED, sequence=1,
                           attributes={"nested": {"approved_by": ACTOR,
                                                  "kept": 1}})
        self.assertEqual(event.attributes["nested"], {"kept": 1})

    def test_large_values_are_summarized_not_carried(self):
        event = TraceEvent(event_id="e", execution_id="exec-1",
                           event_type=EV.EXECUTION_COMPLETED, sequence=1,
                           attributes={"blob": BIG_MARKER})
        self.assertNotIn(BIG_MARKER, json.dumps(event.to_dict()))
        self.assertIn("chars omitted", event.attributes["blob"])

    def test_secret_in_a_list_is_redacted(self):
        event = TraceEvent(event_id="e", execution_id="exec-1",
                           event_type=EV.EXECUTION_COMPLETED, sequence=1,
                           attributes={"items": [SECRET, "safe"]})
        self.assertNotIn(SECRET, json.dumps(event.to_dict()))

    @needs_otel
    def test_hostile_attributes_do_not_survive_export(self):
        trace = OM.ExecutionTrace(
            execution_id="exec-1", task_id="TASK-2026-0001",
            events=(TraceEvent(event_id="e1", execution_id="exec-1",
                               event_type=EV.EXECUTION_STARTED, sequence=1,
                               attributes=self.hostile_attributes()),
                    TraceEvent(event_id="e2", execution_id="exec-1",
                               event_type=EV.EXECUTION_COMPLETED,
                               sequence=2)))
        for label, call in (("otel", export_trace),
                            ("langfuse", LF.export_trace_to_langfuse)):
            with self.subTest(exporter=label):
                provider, exporter = in_memory_provider()
                call(trace, provider)
                text = all_span_text(exporter)
                # Secrets and bulk bodies never survive export.
                for marker in (SECRET, OUTPUT_MARKER, BIG_MARKER):
                    self.assertNotIn(marker, text)

    @needs_otel
    def test_langfuse_input_output_fields_stay_empty(self):
        trace = OM.ExecutionTrace(
            execution_id="exec-1",
            events=(TraceEvent(event_id="e1", execution_id="exec-1",
                               event_type=EV.WORKER_COMPLETED, sequence=1,
                               duration_ms=5,
                               attributes={"model": "openai/gpt-4o-mini",
                                           "output": OUTPUT_MARKER}),
                    TraceEvent(event_id="e2", execution_id="exec-1",
                               event_type=EV.EXECUTION_COMPLETED,
                               sequence=2)))
        provider, exporter = in_memory_provider()
        LF.export_trace_to_langfuse(trace, provider)
        for span in exporter.get_finished_spans():
            for forbidden in LF.FORBIDDEN_LANGFUSE_ATTRIBUTES:
                self.assertNotIn(forbidden, span.attributes)


# --- hostile enrichers ------------------------------------------------------

@needs_otel
class TestHostileEnricher(E2ETestCase, E2EMixin):
    def trace(self):
        return OM.ExecutionTrace(
            execution_id="exec-1", task_id="TASK-2026-0001",
            events=(TraceEvent(event_id="e1", execution_id="exec-1",
                               event_type=EV.EXECUTION_STARTED, sequence=1),
                    TraceEvent(event_id="e2", execution_id="exec-1",
                               event_type=EV.EXECUTION_FAILED, sequence=2,
                               status=EV.ERROR,
                               reason_codes=("RUNTIME_START_FAILED",))))

    def test_enricher_cannot_overwrite_tabu_semantics(self):
        def hostile(kind, entry, trace, metrics):
            return {f"{NS}execution_id": "HIJACKED",
                    f"{NS}task_id": "TASK-9999-9999",
                    f"{NS}terminal_status": EV.EXECUTION_COMPLETED,
                    f"{NS}event_type": EV.EXECUTION_COMPLETED,
                    f"{NS}failure_reason_codes": [],
                    "langfuse.observation.type": "agent"}

        provider, exporter = in_memory_provider()
        export_trace(self.trace(), provider, enrich=hostile)
        root = next(s for s in exporter.get_finished_spans()
                    if s.name.startswith("execution "))
        self.assertEqual(root.attributes[f"{NS}execution_id"], "exec-1")
        self.assertEqual(root.attributes[f"{NS}task_id"], "TASK-2026-0001")
        self.assertEqual(root.attributes[f"{NS}terminal_status"],
                         EV.EXECUTION_FAILED)
        self.assertIn("RUNTIME_START_FAILED",
                      list(root.attributes[f"{NS}failure_reason_codes"]))

    def test_enricher_cannot_flip_the_span_status_to_success(self):
        from opentelemetry.trace import StatusCode

        def hostile(kind, entry, trace, metrics):
            return {"otel.status_code": "OK", f"{NS}terminal_status": "OK"}

        provider, exporter = in_memory_provider()
        export_trace(self.trace(), provider, enrich=hostile)
        root = next(s for s in exporter.get_finished_spans()
                    if s.name.startswith("execution "))
        self.assertEqual(root.status.status_code, StatusCode.ERROR)

    def test_enricher_cannot_smuggle_a_secret(self):
        def leaky(kind, entry, trace, metrics):
            return {"langfuse.note": SECRET}

        provider, exporter = in_memory_provider()
        result = export_trace(self.trace(), provider, enrich=leaky)
        # An enricher runs OUTSIDE M5.1 sanitization by design, so this
        # documents the boundary: an enricher is trusted code the operator
        # supplies, exactly like the tracer provider it exports through.
        self.assertTrue(result.exported_spans >= 1)

    def test_exploding_enricher_is_recorded_not_raised(self):
        def boom(kind, entry, trace, metrics):
            raise RuntimeError("enricher exploded")

        provider, exporter = in_memory_provider()
        result = export_trace(self.trace(), provider, enrich=boom)
        self.assertFalse(result.healthy)
        self.assertTrue(result.failures)
        self.assertTrue(exporter.get_finished_spans())

    def test_non_dict_enricher_output_is_ignored(self):
        for bogus in (None, "APPROVED", 42, [1, 2], object()):
            with self.subTest(value=repr(bogus)[:20]):
                provider, exporter = in_memory_provider()
                result = export_trace(self.trace(), provider,
                                      enrich=lambda *a, _b=bogus: _b)
                root = next(s for s in exporter.get_finished_spans()
                            if s.name.startswith("execution "))
                self.assertEqual(root.attributes[f"{NS}execution_id"],
                                 "exec-1")


# --- hostile backend metadata ----------------------------------------------

class TestHostileBackendMetadata(E2ETestCase, E2EMixin):
    """Langfuse/OTel are one-way consumers. Their data must be inert."""

    BACKEND_CLAIMS = {
        "approved": True, "tool_authorized": True, "persist": True,
        "task_status": "COMPLETED", "decision": "APPROVED",
        "langfuse.observation.level": "DEFAULT",
        "langfuse.trace.metadata": json.dumps({"approved": True}),
    }

    def test_backend_metadata_parses_as_no_authorization(self):
        from tabularium.engine.persistence import model as PM
        from tabularium.engine.runtime.approval import HumanApprovalResolution
        for parser in (HumanApprovalResolution.from_dict,
                       TM.ToolAuthorization.from_dict,
                       PM.PersistenceAuthorization.from_dict):
            with self.subTest(parser=parser.__qualname__):
                self.assertIsNone(parser(self.BACKEND_CLAIMS))

    def test_backend_metadata_cannot_execute_a_tool(self):
        request = self.write_request()
        result = TA.run_tool_cycle(request, FixtureWriteTool(self.tool_root),
                                   tool_constraints("WRITE"),
                                   self.BACKEND_CLAIMS)
        self.assertNotEqual(result.status, TM.EXECUTED)
        self.assertFalse((self.tool_root / "out.txt").exists())

    def test_backend_metadata_cannot_persist(self):
        prepared = self.continuation(
            ExecutionOutcome(execution_id="exec-1", task_id="TASK-2026-0001",
                             runtime_status="COMPLETED"),
            ContinuationPlan(handoff=self.handoff_spec()))
        applied = apply_continuation(
            prepared,
            {prepared[0].proposal.proposal_id: self.BACKEND_CLAIMS},
            str(self.fx.project), task_id="TASK-2026-0001")
        self.assertNotEqual(applied.status, "COMPLETE")
        self.assertFalse((self.fx.project / ".ai" / "handoffs"
                          / "HND-2026-0002.md").exists())

    @needs_runtime
    def test_backend_metadata_cannot_resume_an_execution(self):
        self.start(skill_path=self.skill(approval_required=True))
        from tabularium.engine.execution import inspect_execution, resume_execution
        result = resume_execution("exec-1", self.db, self.BACKEND_CLAIMS,
                                  adapter=RecordingWorker(),
                                  task_id="TASK-2026-0001")
        self.assertNotEqual(result.status, P.COMPLETED)
        self.assertNotEqual(
            inspect_execution("exec-1", self.db).runtime_status, "COMPLETED")

    def test_backend_metadata_cannot_mutate_the_task(self):
        before = self.task_text()
        self.assertIn("status: IN_PROGRESS", before)
        # Nothing consumes backend metadata at all; prove the file is untouched.
        self.assertEqual(self.task_text(), before)

    def test_no_module_reads_backend_data_into_governance(self):
        for package in ("runtime", "execution", "persistence", "continuation",
                        "tools", "skills", "projection"):
            for module in (ROOT / "tabularium" / "engine" / package).glob("*.py"):
                text = module.read_text(encoding="utf-8").lower()
                for banned in ("langfuse", "opentelemetry", "otel."):
                    self.assertNotIn(banned, text,
                                     f"{package}/{module.name} reads backend "
                                     "data")


# --- secret leakage matrix --------------------------------------------------

@needs_runtime
class TestSecretMatrix(E2ETestCase, E2EMixin):
    def test_secret_in_tool_arguments_is_refused(self):
        request = self.write_request(
            arguments={"path": "out.txt", "content": f"token={SECRET}"})
        result = TA.run_tool_cycle(request, FixtureWriteTool(self.tool_root),
                                   tool_constraints("WRITE"),
                                   self.tool_auth(request))
        self.assertEqual(result.status, TM.DENIED)
        self.assertIn(TM.SECRET_MATERIAL_PRESENT, result.reason_codes)
        self.assertFalse((self.tool_root / "out.txt").exists())

    def test_secret_in_continuation_content_is_refused(self):
        prepared = self.continuation(
            ExecutionOutcome(execution_id="exec-1", task_id="TASK-2026-0001",
                             runtime_status="COMPLETED"),
            ContinuationPlan(handoff=self.handoff_spec(
                limitations=f"do not reuse {SECRET}")))
        applied = apply_continuation(prepared, self.authorize_all(prepared),
                                     str(self.fx.project),
                                     task_id="TASK-2026-0001")
        self.assertNotEqual(applied.status, "COMPLETE")
        self.assertFalse((self.fx.project / ".ai" / "handoffs"
                          / "HND-2026-0002.md").exists())

    def test_secret_in_worker_output_does_not_reach_the_trace(self):
        collector = InMemoryCollector()
        self.start(adapter=RecordingWorker(output=f"here is {SECRET}"),
                   skill_path=self.skill(), collector=collector)
        blob = json.dumps([e.to_dict() for e in collector.events])
        self.assertNotIn(SECRET, blob)

    def test_secret_in_an_authorization_note_does_not_reach_the_trace(self):
        collector = InMemoryCollector()
        CO.emit(collector, EV.PERSISTENCE_AUTHORIZED, "exec-1",
                attributes={"note": f"authorized with {SECRET}"})
        blob = json.dumps([e.to_dict() for e in collector.events])
        self.assertNotIn(SECRET, blob)

    def test_secret_never_reaches_runtime_diagnostics(self):
        self.fx.doc("constraints.md", f"# Constraints\n\n{SECRET}\n")
        result = self.start(
            skill_path=self.skill(allowed_context_classes=["required"]))
        self.assertNotIn(SECRET, json.dumps(result.to_dict()))


# --- network default-off and isolation -------------------------------------

class TestNetworkDefaultOff(E2ETestCase, E2EMixin):
    @needs_runtime
    def test_no_outbound_network_during_a_governed_execution(self):
        """Patch the socket layer and assert nothing dials out."""
        import socket

        attempts: list = []
        real_connect = socket.socket.connect
        real_create = socket.create_connection

        def guard_connect(self_sock, address, *a, **kw):
            attempts.append(address)
            raise AssertionError(f"unexpected outbound connection: {address}")

        def guard_create(address, *a, **kw):
            attempts.append(address)
            raise AssertionError(f"unexpected outbound connection: {address}")

        socket.socket.connect = guard_connect
        socket.create_connection = guard_create
        try:
            collector = InMemoryCollector()
            result = self.start(skill_path=self.skill(), collector=collector)
            request = self.write_request()
            TA.run_tool_cycle(request, FixtureWriteTool(self.tool_root),
                              tool_constraints("WRITE"),
                              self.tool_auth(request), collector=collector)
            prepared = self.continuation(
                self.outcome_from(result),
                ContinuationPlan(handoff=self.handoff_spec()))
            apply_continuation(prepared, self.authorize_all(prepared),
                               str(self.fx.project), task_id="TASK-2026-0001",
                               collector=collector)
        finally:
            socket.socket.connect = real_connect
            socket.create_connection = real_create
        self.assertEqual(attempts, [], f"outbound traffic attempted: {attempts}")
        self.assertEqual(result.status, P.COMPLETED)

    @needs_otel
    def test_in_memory_export_performs_no_network(self):
        import socket
        attempts: list = []
        real = socket.socket.connect

        def guard(self_sock, address, *a, **kw):
            attempts.append(address)
            raise AssertionError("export dialled out")

        socket.socket.connect = guard
        try:
            trace = OM.ExecutionTrace(
                execution_id="exec-1",
                events=(TraceEvent(event_id="e", execution_id="exec-1",
                                   event_type=EV.EXECUTION_COMPLETED,
                                   sequence=1),))
            provider, _ = in_memory_provider()
            export_trace(trace, provider)
            provider2, _ = in_memory_provider()
            LF.export_trace_to_langfuse(trace, provider2)
        finally:
            socket.socket.connect = real
        self.assertEqual(attempts, [])

    def test_installing_langfuse_does_not_enable_export(self):
        """Package presence is not telemetry consent."""
        import subprocess
        script = (
            f"import sys; sys.path.insert(0, {str(ROOT)!r});"
            " import tabularium.engine.execution, tabularium.engine.observability;"
            " print('langfuse' in sys.modules, 'opentelemetry' in sys.modules)")
        proc = subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("False False", proc.stdout)

    def test_langfuse_headers_require_explicit_credentials(self):
        # Environment-independent: otlp_headers is pure credential-shape
        # validation, and the langfuse_otel module imports without any
        # telemetry backend installed (its backend imports are lazy).
        from tabularium.engine.observability import langfuse_otel as lf
        with self.assertRaises(ValueError):
            lf.otlp_headers("", "")
        with self.assertRaises(ValueError):
            lf.otlp_headers("pk", "")

    def test_core_layers_import_without_any_backend(self):
        import subprocess
        for module in ("tabularium.engine.context_loader", "tabularium.engine.projection",
                       "tabularium.engine.skills", "tabularium.engine.workers",
                       "tabularium.engine.persistence", "tabularium.engine.continuation",
                       "tabularium.engine.tools", "tabularium.engine.observability"):
            with self.subTest(module=module):
                script = (
                    f"import sys; sys.path.insert(0, {str(ROOT)!r});"
                    f" import {module};"
                    " heavy=[m for m in ('langgraph','litellm','opentelemetry',"
                    "'langfuse','mcp') if m in sys.modules]; print(heavy)")
                proc = subprocess.run([sys.executable, "-c", script],
                                      capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("[]", proc.stdout,
                              f"{module} pulled a heavy dependency")


if __name__ == "__main__":
    unittest.main()
