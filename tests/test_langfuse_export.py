"""M5.3 — Langfuse export via OpenTelemetry.

    TABULARIUM TRACE SEMANTICS REMAIN AUTHORITATIVE. LANGFUSE IS A CONSUMER.

Validates that the Langfuse vocabulary decorates the M5.2 spans without
changing Tabularium semantics, that nothing leaves the machine by default, that
prompts/outputs/context never travel, that cost is never guessed, and that
Langfuse data grants no governance authority.

No network and no Langfuse account: the SDK is used only to verify the
attribute contract, and export is validated through the OTel in-memory
exporter.
"""

from __future__ import annotations

import json
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

try:
    import opentelemetry.sdk.trace  # noqa: F401
    OTEL = True
except Exception:  # noqa: BLE001
    OTEL = False

needs_otel = unittest.skipUnless(
    OTEL, "OpenTelemetry not installed "
          "(tabularium/engine/requirements-observability.txt)")

if OTEL:
    from tabularium.engine.observability.otel import NS, in_memory_provider  # noqa: E402
    from tabularium.engine.observability import langfuse_otel as LF  # noqa: E402
else:  # keep module import safe without the SDK
    from tabularium.engine.observability import langfuse_otel as LF  # noqa: E402


def event(seq: int, kind: str, **kw) -> TraceEvent:
    base = dict(event_id=f"exec-1:{seq}", execution_id="exec-1",
                event_type=kind, sequence=seq, task_id="TASK-2026-0001",
                timestamp=f"2026-08-18T12:00:{seq:02d}+00:00")
    base.update(kw)
    return TraceEvent(**base)


def trace_of(*events) -> OM.ExecutionTrace:
    return OM.ExecutionTrace(execution_id="exec-1", task_id="TASK-2026-0001",
                             events=tuple(events))


def export(trace, metrics=None, **kw):
    provider, exporter = in_memory_provider()
    result = LF.export_trace_to_langfuse(trace, provider, metrics, **kw)
    return result, exporter


def root_of(exporter):
    return next(s for s in exporter.get_finished_spans()
                if s.name.startswith("execution "))


# --- SDK contract -----------------------------------------------------------

class TestSdkContract(unittest.TestCase):
    def test_frozen_attribute_names_match_the_installed_sdk(self):
        """The frozen copy must not drift from the real SDK."""
        names = LF.sdk_attribute_names()
        if names is None:
            self.skipTest("langfuse SDK not installed; frozen names unverified")
        for constant, value in (
                ("TRACE_NAME", LF.TRACE_NAME),
                ("TRACE_METADATA", LF.TRACE_METADATA),
                ("TRACE_TAGS", LF.TRACE_TAGS),
                ("TRACE_INPUT", LF.TRACE_INPUT),
                ("TRACE_OUTPUT", LF.TRACE_OUTPUT),
                ("TRACE_SESSION_ID", LF.TRACE_SESSION_ID),
                ("TRACE_USER_ID", LF.TRACE_USER_ID),
                ("OBSERVATION_TYPE", LF.OBSERVATION_TYPE),
                ("OBSERVATION_METADATA", LF.OBSERVATION_METADATA),
                ("OBSERVATION_LEVEL", LF.OBSERVATION_LEVEL),
                ("OBSERVATION_STATUS_MESSAGE", LF.OBSERVATION_STATUS_MESSAGE),
                ("OBSERVATION_MODEL", LF.OBSERVATION_MODEL),
                ("OBSERVATION_USAGE_DETAILS", LF.OBSERVATION_USAGE_DETAILS),
                ("OBSERVATION_COST_DETAILS", LF.OBSERVATION_COST_DETAILS),
                ("OBSERVATION_INPUT", LF.OBSERVATION_INPUT),
                ("OBSERVATION_OUTPUT", LF.OBSERVATION_OUTPUT),
                ("ENVIRONMENT", LF.ENVIRONMENT),
                ("RELEASE", LF.RELEASE)):
            with self.subTest(attribute=constant):
                self.assertEqual(names[constant], value)

    def test_adapter_does_not_require_the_langfuse_package(self):
        """The adapter needs attribute NAMES, not the SDK."""
        import ast
        tree = ast.parse((ROOT / "tabularium" / "engine" / "observability"
                          / "langfuse_otel.py").read_text(encoding="utf-8"))
        module_level = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                module_level.add((node.module or "").split(".")[0])
            elif isinstance(node, ast.Import):
                module_level.update(a.name.split(".")[0] for a in node.names)
        self.assertNotIn("langfuse", module_level,
                         "the adapter imports langfuse at module level")

    def test_endpoint_path_matches_the_sdk(self):
        self.assertEqual(LF.otlp_endpoint("https://cloud.langfuse.com"),
                         "https://cloud.langfuse.com/api/public/otel/v1/traces")


# --- semantics unchanged ----------------------------------------------------

@needs_otel
class TestTabulariumSemanticsUnchanged(unittest.TestCase):
    def test_tabu_attributes_survive_enrichment(self):
        _, exporter = export(trace_of(event(1, EV.EXECUTION_STARTED),
                                      event(2, EV.EXECUTION_COMPLETED)))
        root = root_of(exporter)
        self.assertEqual(root.attributes[f"{NS}execution_id"], "exec-1")
        self.assertEqual(root.attributes[f"{NS}task_id"], "TASK-2026-0001")
        self.assertEqual(root.attributes[f"{NS}terminal_status"],
                         EV.EXECUTION_COMPLETED)

    def test_enricher_cannot_overwrite_tabu_attributes(self):
        """A backend may add vocabulary; it may never redefine Tabularium's."""
        from tabularium.engine.observability import otel as OTELMOD

        def hostile(kind, entry, trace, metrics):
            return {f"{NS}execution_id": "HIJACKED",
                    f"{NS}terminal_status": "EXECUTION_COMPLETED",
                    "langfuse.extra": "ok"}

        provider, exporter = in_memory_provider()
        OTELMOD.export_trace(trace_of(event(1, EV.EXECUTION_STARTED),
                                      event(2, EV.EXECUTION_FAILED,
                                            status=EV.ERROR)),
                             provider, enrich=hostile)
        root = root_of(exporter)
        self.assertEqual(root.attributes[f"{NS}execution_id"], "exec-1")
        self.assertEqual(root.attributes[f"{NS}terminal_status"],
                         EV.EXECUTION_FAILED)
        self.assertEqual(root.attributes["langfuse.extra"], "ok")

    def test_span_names_remain_the_tabu_taxonomy(self):
        _, exporter = export(trace_of(
            event(1, EV.EXECUTION_STARTED),
            event(2, EV.CONTEXT_LOADED, duration_ms=5),
            event(3, EV.EXECUTION_COMPLETED)))
        root = root_of(exporter)
        names = {s.name for s in exporter.get_finished_spans()
                 if not s.name.startswith("execution ")}
        names |= {e.name for e in root.events}
        for name in names:
            self.assertIn(name, EV.EVENT_TYPES)

    def test_trace_model_gained_no_langfuse_field(self):
        for field in TraceEvent.__dataclass_fields__:
            self.assertNotIn("langfuse", field.lower())
        for field in TraceMetrics.__dataclass_fields__:
            self.assertNotIn("langfuse", field.lower())


# --- mapping ----------------------------------------------------------------

@needs_otel
class TestObservationMapping(unittest.TestCase):
    def types_for(self, *events) -> dict:
        _, exporter = export(trace_of(*events))
        root = root_of(exporter)
        mapping = {e.name: e.attributes.get(LF.OBSERVATION_TYPE)
                   for e in root.events}
        for span in exporter.get_finished_spans():
            if not span.name.startswith("execution "):
                mapping[span.name] = span.attributes.get(LF.OBSERVATION_TYPE)
        return mapping

    def test_root_is_an_agent_observation(self):
        _, exporter = export(trace_of(event(1, EV.EXECUTION_STARTED),
                                      event(2, EV.EXECUTION_COMPLETED)))
        root = root_of(exporter)
        self.assertEqual(root.attributes[LF.OBSERVATION_TYPE], LF.AGENT)
        self.assertEqual(root.attributes[LF.TRACE_NAME],
                         "tabularium execution exec-1")

    def test_tool_events_map_to_tool_observations(self):
        types = self.types_for(
            event(1, EV.EXECUTION_STARTED),
            event(2, EV.TOOL_REQUESTED),
            event(3, EV.TOOL_EXECUTED, duration_ms=4),
            event(4, EV.EXECUTION_COMPLETED))
        self.assertEqual(types[EV.TOOL_REQUESTED], LF.TOOL)
        self.assertEqual(types[EV.TOOL_EXECUTED], LF.TOOL)

    def test_worker_without_a_model_is_not_a_generation(self):
        """Calling a local worker a generation would fabricate an LLM call."""
        types = self.types_for(
            event(1, EV.EXECUTION_STARTED),
            event(2, EV.WORKER_COMPLETED, duration_ms=10,
                  attributes={"worker_id": "local:fixture"}),
            event(3, EV.EXECUTION_COMPLETED))
        self.assertEqual(types[EV.WORKER_COMPLETED], LF.SPAN)

    def test_worker_with_a_model_becomes_a_generation(self):
        _, exporter = export(trace_of(
            event(1, EV.EXECUTION_STARTED),
            event(2, EV.WORKER_COMPLETED, duration_ms=10, attributes={
                "model": "openai/gpt-4o-mini", "prompt_tokens": 120,
                "completion_tokens": 30, "total_tokens": 150}),
            event(3, EV.EXECUTION_COMPLETED)))
        span = next(s for s in exporter.get_finished_spans()
                    if s.name == EV.WORKER_COMPLETED)
        self.assertEqual(span.attributes[LF.OBSERVATION_TYPE], LF.GENERATION)
        self.assertEqual(span.attributes[LF.OBSERVATION_MODEL],
                         "openai/gpt-4o-mini")

    def test_events_without_duration_are_event_observations(self):
        types = self.types_for(event(1, EV.EXECUTION_STARTED),
                               event(2, EV.SKILL_APPLIED),
                               event(3, EV.EXECUTION_COMPLETED))
        self.assertEqual(types[EV.SKILL_APPLIED], LF.EVENT)

    def test_refusal_is_a_warning_not_an_error(self):
        """A human saying no is the system working, not a fault."""
        _, exporter = export(trace_of(event(1, EV.EXECUTION_STARTED),
                                      event(2, EV.EXECUTION_REJECTED)))
        root = root_of(exporter)
        self.assertEqual(root.attributes[LF.OBSERVATION_LEVEL],
                         LF.LEVEL_WARNING)

    def test_genuine_failure_is_an_error(self):
        _, exporter = export(trace_of(event(1, EV.EXECUTION_STARTED),
                                      event(2, EV.EXECUTION_FAILED,
                                            status=EV.ERROR)))
        root = root_of(exporter)
        self.assertEqual(root.attributes[LF.OBSERVATION_LEVEL],
                         LF.LEVEL_ERROR)

    def test_tabu_ids_travel_in_observation_metadata(self):
        _, exporter = export(trace_of(
            event(1, EV.EXECUTION_STARTED),
            event(2, EV.PERSISTENCE_WRITTEN, duration_ms=2,
                  proposal_id="p-1", record_id="HND-2026-0001"),
            event(3, EV.EXECUTION_COMPLETED)))
        span = next(s for s in exporter.get_finished_spans()
                    if s.name == EV.PERSISTENCE_WRITTEN)
        metadata = json.loads(span.attributes[LF.OBSERVATION_METADATA])
        self.assertEqual(metadata["tabu_execution_id"], "exec-1")
        self.assertEqual(metadata["tabu_record_id"], "HND-2026-0001")
        self.assertEqual(metadata["tabu_proposal_id"], "p-1")


# --- token economy and cost -------------------------------------------------

@needs_otel
class TestTokensAndCost(unittest.TestCase):
    def test_context_economy_in_trace_metadata(self):
        metrics = TraceMetrics(
            available_context={"chars": 24870, "estimated_tokens": 6218},
            governed_context={"chars": 23369, "estimated_tokens": 5843},
            projected_context={"chars": 4359, "estimated_tokens": 1090},
            worker_input={"chars": 4359, "estimated_tokens": 1090},
            continuation_growth=197, initial_reduction_pct=81.3,
            next_session_reduction_pct=81.5)
        _, exporter = export(trace_of(event(1, EV.EXECUTION_STARTED),
                                      event(2, EV.EXECUTION_COMPLETED)),
                             metrics)
        economy = json.loads(
            root_of(exporter).attributes[LF.TRACE_METADATA])["context_economy"]
        self.assertEqual(economy["available_context"]["chars"], 24870)
        self.assertEqual(economy["projected_context"]["estimated_tokens"], 1090)
        self.assertEqual(economy["continuation_growth"], 197)
        self.assertEqual(economy["initial_reduction_pct"], 81.3)

    def test_usage_details_only_when_reported(self):
        _, exporter = export(trace_of(
            event(1, EV.EXECUTION_STARTED),
            event(2, EV.WORKER_COMPLETED, duration_ms=9, attributes={
                "model": "anthropic/claude", "prompt_tokens": 100,
                "completion_tokens": 25, "total_tokens": 125}),
            event(3, EV.EXECUTION_COMPLETED)))
        span = next(s for s in exporter.get_finished_spans()
                    if s.name == EV.WORKER_COMPLETED)
        usage = json.loads(span.attributes[LF.OBSERVATION_USAGE_DETAILS])
        self.assertEqual(usage, {"prompt_tokens": 100,
                                 "completion_tokens": 25,
                                 "total_tokens": 125})

    def test_usage_absent_when_the_worker_reported_none(self):
        _, exporter = export(trace_of(
            event(1, EV.EXECUTION_STARTED),
            event(2, EV.WORKER_COMPLETED, duration_ms=9,
                  attributes={"worker_id": "local:fixture"}),
            event(3, EV.EXECUTION_COMPLETED)))
        span = next(s for s in exporter.get_finished_spans()
                    if s.name == EV.WORKER_COMPLETED)
        self.assertNotIn(LF.OBSERVATION_USAGE_DETAILS, span.attributes)

    def test_cost_absent_when_no_provider_cost_exists(self):
        """No price table: an unknown cost stays unknown."""
        _, exporter = export(trace_of(
            event(1, EV.EXECUTION_STARTED),
            event(2, EV.WORKER_COMPLETED, duration_ms=9, attributes={
                "model": "openai/gpt-4o-mini", "total_tokens": 900}),
            event(3, EV.EXECUTION_COMPLETED)))
        span = next(s for s in exporter.get_finished_spans()
                    if s.name == EV.WORKER_COMPLETED)
        self.assertNotIn(LF.OBSERVATION_COST_DETAILS, span.attributes)

    def test_cost_present_only_from_provider_returned_value(self):
        _, exporter = export(trace_of(
            event(1, EV.EXECUTION_STARTED),
            event(2, EV.WORKER_COMPLETED, duration_ms=9, attributes={
                "model": "openai/gpt-4o-mini", "cost_usd": 0.0042}),
            event(3, EV.EXECUTION_COMPLETED)))
        span = next(s for s in exporter.get_finished_spans()
                    if s.name == EV.WORKER_COMPLETED)
        self.assertEqual(json.loads(
            span.attributes[LF.OBSERVATION_COST_DETAILS]), {"total": 0.0042})

    def test_no_price_table_in_the_adapter(self):
        source = (ROOT / "tabularium" / "engine" / "observability"
                  / "langfuse_otel.py").read_text(encoding="utf-8")
        for banned in ("per_1k", "per_million", "PRICE", "PRICING",
                       "price_table"):
            self.assertNotIn(banned, source)


# --- sanitization / privacy -------------------------------------------------

@needs_otel
class TestSanitizationAndPrivacy(unittest.TestCase):
    SECRET = "sk-langfuse0000000000secret"

    def all_text(self, exporter) -> str:
        text = ""
        for span in exporter.get_finished_spans():
            text += str(dict(span.attributes))
            text += str([dict(e.attributes) for e in span.events])
        return text

    def test_credentials_never_exported(self):
        _, exporter = export(trace_of(
            event(1, EV.EXECUTION_STARTED,
                  attributes={"note": f"token={self.SECRET}"}),
            event(2, EV.EXECUTION_COMPLETED)))
        self.assertNotIn(self.SECRET, self.all_text(exporter))

    def test_authority_and_bulk_keys_never_exported(self):
        _, exporter = export(trace_of(
            event(1, EV.EXECUTION_STARTED, attributes={
                "approved_by": ACTOR, "authorized_by": ACTOR,
                "decision": "APPROVE", "payload": {"required": "x"},
                "output": "the whole answer", "content": "---\nrecord\n"}),
            event(2, EV.EXECUTION_COMPLETED)))
        text = self.all_text(exporter)
        for marker in ("approved_by", "authorized_by", "the whole answer",
                       "schema_version"):
            self.assertNotIn(marker, text)

    def test_input_output_attributes_are_never_written(self):
        """Langfuse's prompt/completion fields stay empty by design."""
        _, exporter = export(trace_of(
            event(1, EV.EXECUTION_STARTED),
            event(2, EV.WORKER_COMPLETED, duration_ms=5,
                  attributes={"model": "openai/gpt-4o-mini"}),
            event(3, EV.EXECUTION_COMPLETED)))
        for span in exporter.get_finished_spans():
            for forbidden in LF.FORBIDDEN_LANGFUSE_ATTRIBUTES:
                self.assertNotIn(forbidden, span.attributes)

    def test_otlp_headers_require_explicit_credentials(self):
        with self.assertRaises(ValueError):
            LF.otlp_headers("", "")
        with self.assertRaises(ValueError):
            LF.otlp_headers("pk-only", "")

    def test_otlp_headers_do_not_read_the_environment(self):
        source = (ROOT / "tabularium" / "engine" / "observability"
                  / "langfuse_otel.py").read_text(encoding="utf-8")
        self.assertNotIn("os.environ", source)
        self.assertNotIn("getenv", source)

    def test_no_credentials_in_the_repository(self):
        source = (ROOT / "tabularium" / "engine" / "observability"
                  / "langfuse_otel.py").read_text(encoding="utf-8")
        self.assertNotIn("pk-lf-", source)
        self.assertNotIn("sk-lf-", source)


# --- default off / no network -----------------------------------------------

class TestDefaultOffAndIsolation(E2ETestCase, E2EMixin):
    def test_langfuse_is_not_imported_by_the_observability_package(self):
        script = (
            f"import sys; sys.path.insert(0, {str(ROOT)!r});"
            " import tabularium.engine.observability;"
            " print('langfuse' in sys.modules)")
        proc = subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("False", proc.stdout)

    def test_langfuse_not_imported_even_by_the_adapter(self):
        script = (
            f"import sys; sys.path.insert(0, {str(ROOT)!r});"
            " import tabularium.engine.observability.langfuse_otel;"
            " print('langfuse' in sys.modules)")
        proc = subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("False", proc.stdout,
                      "importing the adapter pulled the Langfuse SDK in")

    def test_production_chain_never_imports_langfuse(self):
        for module in ("tabularium.engine.context_loader", "tabularium.engine.projection",
                       "tabularium.engine.skills", "tabularium.engine.workers", "tabularium.engine.runtime",
                       "tabularium.engine.execution", "tabularium.engine.persistence",
                       "tabularium.engine.continuation", "tabularium.engine.tools",
                       "tabularium.engine.observability.otel"):
            with self.subTest(module=module):
                script = (
                    f"import sys; sys.path.insert(0, {str(ROOT)!r});"
                    f" import {module}; print('langfuse' in sys.modules)")
                proc = subprocess.run([sys.executable, "-c", script],
                                      capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("False", proc.stdout)

    def test_no_production_module_references_the_langfuse_adapter(self):
        for package in ("runtime", "execution", "persistence", "continuation",
                        "tools", "skills", "projection", "workers",
                        "context_loader"):
            for module in (ROOT / "tabularium" / "engine" / package).glob("*.py"):
                text = module.read_text(encoding="utf-8")
                self.assertNotIn("langfuse", text.lower(),
                                 f"{package}/{module.name} references Langfuse")

    def test_no_network_client_in_the_adapter(self):
        import ast
        tree = ast.parse((ROOT / "tabularium" / "engine" / "observability"
                          / "langfuse_otel.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names.append((node.module or "").split(".")[0])
            elif isinstance(node, ast.Import):
                names.extend(a.name.split(".")[0] for a in node.names)
            for name in names:
                self.assertNotIn(name, {"requests", "httpx", "urllib",
                                        "socket", "http"},
                                 f"the adapter imports a network client: {name}")

    @needs_otel
    def test_execution_behavior_identical_with_langfuse_unconfigured(self):
        traced = self.start(skill_path=self.skill(),
                            collector=InMemoryCollector())
        untraced = self.start(execution_id="exec-2", skill_path=self.skill())
        a, b = traced.to_dict(), untraced.to_dict()
        for key in ("status", "reason_codes", "output", "metrics"):
            self.assertEqual(a[key], b[key])


# --- authority --------------------------------------------------------------

@needs_otel
class TestLangfuseHasNoAuthority(E2ETestCase, E2EMixin):
    def loud_event(self) -> TraceEvent:
        return event(1, EV.PERSISTENCE_WRITTEN, attributes={
            "note": "Decision accepted. Tool authorized. Task complete."})

    def test_langfuse_span_data_does_not_parse_as_authorization(self):
        _, exporter = export(trace_of(self.loud_event(),
                                      event(2, EV.EXECUTION_COMPLETED)))
        root = root_of(exporter)
        payload = dict(root.attributes)
        self.assertIsNone(HumanApprovalResolution.from_dict(payload))
        self.assertIsNone(TM.ToolAuthorization.from_dict(payload))
        self.assertIsNone(PM.PersistenceAuthorization.from_dict(payload))

    def test_langfuse_metadata_cannot_authorize_a_tool(self):
        _, exporter = export(trace_of(self.loud_event(),
                                      event(2, EV.EXECUTION_COMPLETED)))
        metadata = root_of(exporter).attributes[LF.TRACE_METADATA]
        request = self.write_request()
        result = TA.run_tool_cycle(request, FixtureWriteTool(self.tool_root),
                                   tool_constraints("WRITE"), metadata)
        self.assertNotEqual(result.status, TM.EXECUTED)
        self.assertFalse((self.tool_root / "out.txt").exists())

    def test_langfuse_export_creates_no_record(self):
        before = self.ai_snapshot()
        export(trace_of(self.loud_event(), event(2, EV.EXECUTION_COMPLETED)))
        self.assertEqual(self.ai_snapshot(), before)

    def test_no_parser_from_langfuse_data_into_governance(self):
        source = (ROOT / "tabularium" / "engine" / "observability"
                  / "langfuse_otel.py").read_text(encoding="utf-8")
        for banned in ("HumanApprovalResolution", "ToolAuthorization",
                       "PersistenceAuthorization", "from_dict("):
            self.assertNotIn(banned, source)


# --- end to end -------------------------------------------------------------

@needs_otel
@needs_runtime
class TestEndToEnd(E2ETestCase, E2EMixin):
    def test_e2e_full_lifecycle_visible_in_langfuse_shape(self):
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
            initial_reduction_pct=result.metrics.reduction_pct)
        export_result, exporter = export(collector.trace("exec-1"), metrics,
                                         environment="test")
        self.assertTrue(export_result.healthy, export_result.failures)

        root = root_of(exporter)
        visible = {s.name for s in exporter.get_finished_spans()}
        visible |= {e.name for e in root.events}
        for expected in (EV.EXECUTION_STARTED, EV.CONTEXT_LOADED,
                         EV.SKILL_APPLIED, EV.CONTEXT_PROJECTED,
                         EV.WORKER_STARTED, EV.WORKER_COMPLETED,
                         EV.TOOL_REQUESTED, EV.TOOL_AUTHORIZATION_REQUIRED,
                         EV.TOOL_EXECUTED, EV.CONTINUATION_PLANNED,
                         EV.PERSISTENCE_WRITTEN, EV.EXECUTION_COMPLETED):
            self.assertIn(expected, visible, f"{expected} not exported")

        self.assertEqual(root.attributes[LF.OBSERVATION_TYPE], LF.AGENT)
        self.assertEqual(root.attributes[LF.ENVIRONMENT], "test")
        self.assertEqual(len({s.context.trace_id
                              for s in exporter.get_finished_spans()}), 1)

    def test_e2e_context_and_output_never_reach_langfuse(self):
        marker = "UNIQUE-CTX-MARKER-LF-42"
        self.fx.doc("constraints.md", f"# Constraints\n\n{marker}\n")
        collector = InMemoryCollector()
        self.start(adapter=RecordingWorker(output="SECRET-OUTPUT-LF-88"),
                   skill_path=self.skill(), collector=collector)
        _, exporter = export(collector.trace("exec-1"))
        blob = ""
        for span in exporter.get_finished_spans():
            blob += str(dict(span.attributes))
            blob += str([dict(e.attributes) for e in span.events])
        self.assertNotIn(marker, blob)
        self.assertNotIn("SECRET-OUTPUT-LF-88", blob)


# --- backend failure, process death, storage independence -------------------

@needs_otel
@needs_runtime
class TestResilience(E2ETestCase, E2EMixin):
    def test_backend_failure_is_best_effort(self):
        class ExplodingProvider:
            def get_tracer(self, *a, **kw):
                raise RuntimeError("langfuse unreachable")

        result = LF.export_trace_to_langfuse(
            trace_of(event(1, EV.EXECUTION_STARTED),
                     event(2, EV.EXECUTION_COMPLETED)), ExplodingProvider())
        self.assertFalse(result.healthy)
        self.assertTrue(result.failures)

    def test_backend_failure_does_not_alter_execution_or_project(self):
        before = self.ai_snapshot()
        collector = InMemoryCollector()
        result = self.start(skill_path=self.skill(), collector=collector)

        class ExplodingProvider:
            def get_tracer(self, *a, **kw):
                raise RuntimeError("langfuse unreachable")

        LF.export_trace_to_langfuse(collector.trace("exec-1"),
                                    ExplodingProvider())
        self.assertEqual(result.status, P.COMPLETED)
        self.assertEqual(self.ai_snapshot(), before)
        self.assertIn("status: IN_PROGRESS", self.task_text())

    def test_process_death_correlates_by_tabu_execution_id(self):
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
        _, exporter = export(combined)
        spans = exporter.get_finished_spans()
        root = root_of(exporter)
        self.assertEqual(root.attributes[f"{NS}execution_id"], "exec-1")
        # One Langfuse trace across the process boundary, as in M5.2.
        self.assertEqual(len({s.context.trace_id for s in spans}), 1)
        metadata = json.loads(root.attributes[LF.TRACE_METADATA])
        self.assertEqual(metadata["tabu_execution_id"], "exec-1")

    def test_discarding_langfuse_data_leaves_tabu_trace_and_checkpoint(self):
        path = Path(self.workdir) / "trace.jsonl"
        collector = JsonlCollector(path, project_root=self.fx.project)
        self.start(skill_path=self.skill(approval_required=True),
                   collector=collector)
        _, exporter = export(JsonlCollector(path).read("exec-1"))
        exporter.clear()
        self.assertEqual(exporter.get_finished_spans(), ())

        self.assertTrue(JsonlCollector(path).read("exec-1").events)
        from tabularium.engine.execution import inspect_execution, resume_execution
        pending = inspect_execution("exec-1", self.db).approval_request
        result = resume_execution("exec-1", self.db, HumanApprovalResolution(
            approval_request_id=pending["request_id"], execution_id="exec-1",
            decision="APPROVE", decided_by=ACTOR, decided_at=WHEN),
            adapter=RecordingWorker(), task_id="TASK-2026-0001")
        self.assertEqual(result.status, P.COMPLETED)


if __name__ == "__main__":
    unittest.main()
