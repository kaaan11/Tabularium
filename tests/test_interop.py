"""Focused M6.1 interoperability boundary conformance tests."""

from __future__ import annotations

import ast
import json
import socket
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tabularium.engine.interop import (  # noqa: E402
    DUPLICATE_RESULT,
    MALFORMED_RESPONSE,
    PROTOCOL_ERROR,
    REMOTE_COMPLETED,
    TIMEOUT,
    UNAVAILABLE,
    FixtureMode,
    InteropAdapter,
    InteropResult,
    LocalRemoteTransport,
    RemoteIdentity,
    deserialize_request,
    serialize_request,
)
from tabularium.engine.interop.model import WRONG_EXECUTION_ID, WRONG_TASK_ID  # noqa: E402
from tabularium.engine.interop.mapping import request_from_worker  # noqa: E402
from tabularium.engine.observability import InMemoryCollector  # noqa: E402
from tabularium.engine.projection import project  # noqa: E402
from tabularium.engine.projection.budget import HOT, measure  # noqa: E402
from tabularium.engine.projection.model import ProjectedPayload  # noqa: E402
from tests.test_projection import (INTENT, large_project,  # noqa: E402
                                           pinned_size)
from tabularium.engine.workers.model import COMPLETED, FAILED, REJECTED, WorkerRequest  # noqa: E402


def request(*, request_id: str = "req-1", execution_id: str = "exec-1",
            task_id: str = "TASK-2026-0001") -> WorkerRequest:
    payload = ProjectedPayload((
        ("required", {"intent": "continue", "task": {"id": task_id}}),
        ("constraints", {"text": "bounded"}),
    ))
    return WorkerRequest(
        request_id=request_id,
        intent="continue",
        payload=payload,
        task_id=task_id,
        governance={"approved": True, "authorized_by": "remote", "secret": "sk-request-secret"},
        model_requirements={"credential": "should-not-cross"},
        correlation={"execution_id": execution_id, "task_id": task_id},
    )


def adapter(transport=None, *, collector=None, **kwargs) -> InteropAdapter:
    return InteropAdapter(
        transport or LocalRemoteTransport(),
        RemoteIdentity(provider="fixture-provider", model="fixture-model",
                       worker="fixture-worker", runtime="fixture-runtime",
                       endpoint="fixture://local", agent_id="fixture-agent"),
        collector=collector, **kwargs)


class TestInteropTransportFixture(unittest.TestCase):
    def test_success_normalizes_to_worker_result_and_only_sends_projection(self):
        transport = LocalRemoteTransport()
        result = adapter(transport).execute(request())
        self.assertEqual(result.status, COMPLETED)
        self.assertEqual(result.output, "fixture remote result")
        wire = serialize_request(transport.last_request)
        self.assertNotIn("governance", wire)
        self.assertNotIn("model_requirements", wire)
        self.assertNotIn("credential", json.dumps(wire))
        self.assertNotIn("authorized_by", json.dumps(wire))
        self.assertNotIn("project_root", json.dumps(wire))
        self.assertEqual(set(wire), {
            "interop_request_id", "execution_id", "task_id", "worker_target",
            "projected_payload", "requested_capability", "metadata", "timeout_s",
            "deadline"})

    def test_failure_modes_are_explicit_and_do_not_fallback(self):
        for mode, code in ((FixtureMode.REMOTE_FAILURE, "REMOTE_FAILED"),
                           (FixtureMode.TIMEOUT, TIMEOUT),
                           (FixtureMode.MALFORMED, MALFORMED_RESPONSE)):
            with self.subTest(mode=mode):
                result = adapter(LocalRemoteTransport(mode)).execute(request())
                self.assertEqual(result.status, FAILED)
                self.assertEqual(result.diagnostics[0]["code"], code)

    def test_authority_looking_remote_result_is_inert(self):
        result = adapter(LocalRemoteTransport(FixtureMode.AUTHORITY_LOOKING)).execute(request())
        self.assertEqual(result.status, COMPLETED)
        self.assertEqual(result.output, "APPROVED")
        self.assertEqual(result.artifacts[0]["metadata"]["approved"], True)
        self.assertEqual(result.artifacts[0]["parts"][0]["data"]["persist"], True)
        self.assertTrue(result.artifacts[0]["parts"][0]["data"]["tool_request"]["authorized"])
        self.assertNotIn("authorized_by", result.__dataclass_fields__)
        self.assertNotIn("approved", result.__dataclass_fields__)
        self.assertNotIn("persist", result.__dataclass_fields__)

    def test_remote_endpoint_cannot_carry_credentials(self):
        with self.assertRaises(ValueError):
            RemoteIdentity(endpoint="https://user:secret@example.invalid/a")
        with self.assertRaises(ValueError):
            RemoteIdentity(endpoint="https://example.invalid/a?token=secret")

    def test_remote_task_status_does_not_complete_tabu_task(self):
        class Waiting(LocalRemoteTransport):
            def send(self, envelope):
                result = super().send(envelope)
                return InteropResult(**{**result.__dict__, "status": "WAITING",
                                        "remote_task_status": "WAITING"})

        result = adapter(Waiting()).execute(request())
        self.assertEqual(result.status, FAILED)
        self.assertEqual(result.diagnostics[0]["code"], "REMOTE_STATUS_NOT_COMPLETED")

    def test_capability_claim_cannot_self_widen(self):
        blocked = adapter(LocalRemoteTransport(), remote_capabilities=("WRITE",),
                          trusted_capabilities=(), required_remote_capability="WRITE")
        result = blocked.execute(request())
        self.assertEqual(result.status, REJECTED)
        self.assertIn("CAPABILITY_NOT_TRUSTED:WRITE", result.diagnostics[0]["code"])

        allowed = adapter(LocalRemoteTransport(), remote_capabilities=("WRITE",),
                          trusted_capabilities=("WRITE",), required_remote_capability="WRITE")
        self.assertEqual(allowed.execute(request()).status, COMPLETED)

    def test_wrong_correlation_ids_fail_closed(self):
        for field, code in (("execution_id", WRONG_EXECUTION_ID),
                            ("task_id", WRONG_TASK_ID)):
            class Wrong(LocalRemoteTransport):
                def send(self, envelope, field=field):
                    result = super().send(envelope)
                    values = dict(result.__dict__)
                    values[field] = "wrong-id"
                    return InteropResult(**values)

            result = adapter(Wrong()).execute(request())
            self.assertEqual(result.status, FAILED)
            self.assertEqual(result.diagnostics[0]["code"], code)

    def test_duplicate_result_is_not_handled_twice(self):
        transport = LocalRemoteTransport()
        target = adapter(transport)
        self.assertEqual(target.execute(request()).status, COMPLETED)
        duplicate = target.execute(request())
        self.assertEqual(duplicate.status, FAILED)
        self.assertEqual(duplicate.diagnostics[0]["code"], DUPLICATE_RESULT)


class TestInteropBoundary(unittest.TestCase):
    def test_plain_mapping_restores_projected_payload_and_rejects_raw_context(self):
        transport = LocalRemoteTransport()
        target = adapter(transport)
        target.execute(request())
        restored = deserialize_request(serialize_request(transport.last_request))
        self.assertIsInstance(restored.projected_payload, ProjectedPayload)
        self.assertEqual(restored.projected_payload.section_names(),
                         ("required", "constraints"))
        raw = serialize_request(transport.last_request)
        raw["projected_payload"] = {"selected": [{"body": "full project"}]}
        with self.assertRaises(ValueError):
            deserialize_request(raw)

    def test_oversized_and_secret_metadata_never_enters_trace(self):
        class Untrusted(LocalRemoteTransport):
            def send(self, envelope):
                result = super().send(envelope)
                values = dict(result.__dict__)
                values["metadata"] = {
                    "nested": {"api_key": "sk-remote-secret-123456789", "approved": True},
                    "long": "x" * 9000,
                }
                return values

        collector = InMemoryCollector()
        result = adapter(Untrusted(), collector=collector).execute(request())
        self.assertEqual(result.status, FAILED)
        self.assertEqual(result.diagnostics[0]["code"], MALFORMED_RESPONSE)
        trace = json.dumps([event.to_dict() for event in collector.events])
        self.assertNotIn("sk-remote-secret", trace)
        self.assertNotIn("approved", trace)
        self.assertNotIn("x" * 100, trace)

    def test_observability_is_metadata_only(self):
        collector = InMemoryCollector()
        result = adapter(collector=collector).execute(request())
        self.assertEqual(result.status, COMPLETED)
        event_types = [event.event_type for event in collector.events]
        self.assertEqual(event_types, ["INTEROP_REQUESTED", "INTEROP_COMPLETED"])
        for event in collector.events:
            self.assertIn("remote_endpoint", event.attributes)
            self.assertIn("payload_chars", event.attributes)
            self.assertNotIn("payload", event.attributes)
            self.assertNotIn("output", event.attributes)

    def test_wrapper_overhead_does_not_erase_projection_savings(self):
        payload = ProjectedPayload((("required", {"text": "x" * 6000}),))
        req = WorkerRequest(request_id="req-large", intent="continue",
                            payload=payload, task_id="TASK-2026-0001",
                            correlation={"execution_id": "exec-1",
                                         "task_id": "TASK-2026-0001"})
        transport = LocalRemoteTransport()
        adapter(transport).execute(req)
        envelope_chars = len(json.dumps(serialize_request(transport.last_request),
                                        separators=(",", ":")))
        payload_chars = transport.last_request.payload_chars
        self.assertLess(envelope_chars - payload_chars, payload_chars * 0.30)
        self.assertEqual(transport.last_request.payload_tokens,
                         (payload_chars + 3) // 4)

    def test_production_token_economy_metrics_are_reproducible(self):
        fixture = large_project()
        self.addCleanup(fixture.cleanup)
        package = fixture.load(task="TASK-2026-0001")
        projection = project(package, intent=INTENT, profile=HOT)
        self.assertTrue(projection.projected)
        self.assertIsNotNone(projection.payload)

        worker_request = WorkerRequest(
            request_id="economy-1", intent=INTENT, payload=projection.payload,
            task_id="TASK-2026-0001",
            correlation={"execution_id": "exec-economy",
                         "task_id": "TASK-2026-0001"})
        envelope = request_from_worker(
            worker_request,
            RemoteIdentity(endpoint="fixture://local", agent_id="fixture"),
            "economy-1:interop")
        serialized = measure(serialize_request(envelope))
        projected = measure(envelope.projected_payload.to_dict())
        governed_tokens = projection.economy.governed_context["estimated_tokens"]
        projected_tokens = projected["estimated_tokens"]
        remote_input_tokens = serialized["estimated_tokens"]
        projection_reduction_pct = round(
            100 * (governed_tokens - projected_tokens) / governed_tokens, 1)
        effective_remote_reduction_pct = round(
            100 * (governed_tokens - remote_input_tokens) / governed_tokens, 1)
        wrapper_overhead = {
            "chars": serialized["chars"] - projected["chars"],
            "estimated_tokens": remote_input_tokens - projected_tokens,
        }

        # Independently re-pinned. These figures are derived deterministically
        # from the synthetic `large_project()` temporary fixture alone; the
        # projection reads no repository content, so no clean-baseline
        # documentation delta can account for them. The inherited values
        # ({"chars": 36714, ...9179} / {"chars": 34973, ...8744}) were stale
        # on arrival; the exact historical reason is not established by
        # repository evidence and is not claimed here.
        # Whole-package sizes embed the fixture root; pinned_size() holds
        # them to the /tmp pin (36646, 34905) on any TMPDIR. The manifest key
        # rename ai_os_ruleset -> tabularium_ruleset added 5 chars to each.
        self.assertEqual(projection.economy.available_context,
                         pinned_size(fixture, 36646))
        governed = pinned_size(fixture, 34905)
        self.assertEqual(projection.economy.governed_context, governed)
        self.assertEqual(projected,
                         {"chars": 4797, "estimated_tokens": 1200})
        self.assertEqual(serialized,
                         {"chars": 5141, "estimated_tokens": 1286})
        self.assertEqual(wrapper_overhead,
                         {"chars": 344, "estimated_tokens": 86})
        # 86.2 and 85.3 at the /tmp pin.
        pinned_governed = governed["estimated_tokens"]
        self.assertEqual(projection_reduction_pct,
                         round(100 * (pinned_governed - 1200) / pinned_governed, 1))
        self.assertEqual(effective_remote_reduction_pct,
                         round(100 * (pinned_governed - 1286) / pinned_governed, 1))

    def test_default_import_and_fixture_are_network_free(self):
        source = "import socket, tabularium.engine.interop; print('ok')"
        proc = subprocess.run([sys.executable, "-c", source], cwd=ROOT,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        transport = LocalRemoteTransport()
        self.assertIsNone(transport.last_request)

    def test_process_b_can_handle_recorded_result_without_remote_state(self):
        transport = LocalRemoteTransport()
        envelope = transport.last_request
        self.assertIsNone(envelope)
        request_envelope = adapter(transport)
        worker_request = request()
        request_envelope.execute(worker_request)
        recorded = transport.send(transport.last_request).to_dict()
        code = (
            "import json, sys; "
            "from tabularium.engine.interop import deserialize_result; "
            "result = deserialize_result(json.loads(sys.argv[1])); "
            "print(result.execution_id + ':' + result.task_id)"
        )
        proc = subprocess.run([sys.executable, "-c", code,
                               json.dumps(recorded)], cwd=ROOT,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "exec-1:TASK-2026-0001")

    def test_interop_does_not_import_authority_or_side_effect_boundaries(self):
        forbidden = {"persistence", "tools", "continuation", "runtime.approval"}
        for path in (ROOT / "tabularium" / "engine" / "interop").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    self.assertFalse(any(name == item or name.startswith(item + ".")
                                         for item in forbidden),
                                     f"interop imports governed boundary {name}")

    def test_interop_package_has_no_a2a_sdk_dependency(self):
        for path in (ROOT / "tabularium" / "engine" / "interop").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "").split(".")[0]]
                else:
                    continue
                self.assertNotIn("a2a", names)
                self.assertNotIn("a2a_sdk", names)


if __name__ == "__main__":
    unittest.main()
