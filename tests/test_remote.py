"""Focused M6.3 remote worker lifecycle and OSS-boundary tests."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tabularium.engine.interop.model import RemoteIdentity  # noqa: E402
from tabularium.engine.memory import InMemoryMemoryStore, MemoryRecord  # noqa: E402
from tabularium.engine.observability import InMemoryCollector  # noqa: E402
from tabularium.engine.projection import project  # noqa: E402
from tabularium.engine.projection.budget import HOT, measure  # noqa: E402
from tabularium.engine.projection.model import ProjectedPayload  # noqa: E402
from tabularium.engine.remote import (  # noqa: E402
    CANCELLED,
    COMPLETED,
    FAILED,
    TIMEOUT,
    UNKNOWN,
    InMemoryRemoteLedger,
    JsonRemoteLedger,
    RemoteWorkerAdapter,
    RemoteWorkerConfig,
    UrllibJsonTransport,
)
from tabularium.engine.remote.fixture import (  # noqa: E402
    LoopbackHttpTransport,
    LoopbackMode,
    LoopbackRemoteServer,
)
from tabularium.engine.remote.model import (  # noqa: E402
    DUPLICATE_RESULT,
    IDENTITY_MISMATCH,
    LATE_RESULT,
    PAYLOAD_MISMATCH,
    REMOTE_CANCEL_CONFIRMED,
    REMOTE_MALFORMED,
    REMOTE_TIMEOUT,
    REMOTE_UNAVAILABLE,
)
from tabularium.engine.remote.ledger import LedgerRecord  # noqa: E402
from tabularium.engine.remote.transport import RemoteTransportFailure  # noqa: E402
from tests.test_projection import (INTENT, large_project,  # noqa: E402
                                           pinned_size)
from tabularium.engine.workers.adapter import WorkerAdapter  # noqa: E402
from tabularium.engine.workers.model import (  # noqa: E402
    API_MODEL,
    CANCELLED as WORKER_CANCELLED,
    COMPLETED as WORKER_COMPLETED,
    FAILED as WORKER_FAILED,
    WorkerRequest,
)


def request(request_id="request-1", execution_id="execution-1",
            task_id="TASK-2026-0001", *, text="bounded remote work"):
    return WorkerRequest(
        request_id=request_id, intent="continue",
        payload=ProjectedPayload((("required", {"text": text}),)),
        task_id=task_id,
        governance={"approved": True, "authorized_by": "human",
                    "secret": "sk-never-crosses"},
        model_requirements={"provider": "not-wire-data"},
        correlation={"execution_id": execution_id, "task_id": task_id})


def make_adapter(server, *, mode=None, timeout=1.0, ledger=None,
                 collector=None, worker_id="remote-worker-1"):
    config = RemoteWorkerConfig(
        worker_id=worker_id,
        identity=RemoteIdentity(provider="fixture-provider",
                                model="fixture-model",
                                worker="fixture-worker",
                                runtime="fixture-runtime",
                                endpoint=server.endpoint,
                                agent_id="fixture-agent"),
        timeout_s=timeout,
        remote_capabilities=("STRUCTURED_OUTPUT",),
        trusted_capabilities=("STRUCTURED_OUTPUT",))
    return RemoteWorkerAdapter(
        LoopbackHttpTransport(), config, ledger=ledger, collector=collector)


class TestRemoteWorkerLifecycle(unittest.TestCase):
    def test_adapter_reuses_worker_boundary_and_successfully_maps_result(self):
        with LoopbackRemoteServer().start() as server:
            adapter = make_adapter(server)
            self.assertIsInstance(adapter, WorkerAdapter)
            result = adapter.execute_lifecycle(request())
            self.assertEqual(result.status, COMPLETED)
            self.assertEqual(result.worker_result.status, WORKER_COMPLETED)
            self.assertEqual(result.worker_result.output, "loopback remote result")
            self.assertEqual(result.execution_id, "execution-1")
            self.assertEqual(result.task_id, "TASK-2026-0001")

    def test_request_contains_only_projected_payload_and_exact_binding(self):
        with LoopbackRemoteServer().start() as server:
            adapter = make_adapter(server)
            envelope = adapter.prepare(request())
            body = envelope.to_dict()
            text = json.dumps(body, sort_keys=True)
            self.assertEqual(body["worker_id"], "remote-worker-1")
            self.assertEqual(len(body["payload_digest"]), 64)
            self.assertNotIn("governance", text)
            self.assertNotIn("model_requirements", text)
            self.assertNotIn("sk-never-crosses", text)
            self.assertNotIn("project_root", text)
            self.assertIn("required", text)

    def test_authority_looking_remote_result_is_inert(self):
        with LoopbackRemoteServer(LoopbackMode.AUTHORITY_LOOKING).start() as server:
            result = make_adapter(server).execute_lifecycle(request())
            self.assertEqual(result.status, COMPLETED)
            self.assertEqual(result.worker_result.output, "APPROVED")
            self.assertEqual(result.worker_result.artifacts[0]["metadata"]["approved"], True)
            self.assertNotIn("approved", result.worker_result.__dataclass_fields__)
            self.assertNotIn("authorized", result.worker_result.__dataclass_fields__)

    def test_failure_timeout_malformed_and_unavailable_are_controlled(self):
        for mode, expected_status, expected_code in (
            (LoopbackMode.REMOTE_FAILURE, FAILED, "REMOTE_FAILED"),
            (LoopbackMode.MALFORMED, FAILED, REMOTE_MALFORMED),
            (LoopbackMode.SECRET_METADATA, FAILED, REMOTE_MALFORMED),
        ):
            with self.subTest(mode=mode), LoopbackRemoteServer(mode).start() as server:
                result = make_adapter(server).execute_lifecycle(request())
                self.assertEqual(result.status, expected_status)
                self.assertIn(expected_code, result.reason_codes)
        with LoopbackRemoteServer(LoopbackMode.TIMEOUT, delay_s=0.3).start() as server:
            result = make_adapter(server, timeout=0.03).execute_lifecycle(request())
            self.assertEqual(result.status, TIMEOUT)
            self.assertIn(REMOTE_TIMEOUT, result.reason_codes)
        with LoopbackRemoteServer().start() as server:
            endpoint = server.endpoint
        config = RemoteWorkerConfig(
            "remote-worker-1", RemoteIdentity(endpoint=endpoint), 0.05)
        result = RemoteWorkerAdapter(UrllibJsonTransport(), config).execute_lifecycle(request())
        self.assertEqual(result.status, UNKNOWN)
        self.assertIn(REMOTE_UNAVAILABLE, result.reason_codes)

    def test_identity_and_payload_mismatch_fail_closed(self):
        for mode, code in ((LoopbackMode.IDENTITY_MISMATCH, IDENTITY_MISMATCH),
                           (LoopbackMode.PAYLOAD_MISMATCH, PAYLOAD_MISMATCH)):
            with self.subTest(mode=mode), LoopbackRemoteServer(mode).start() as server:
                result = make_adapter(server).execute_lifecycle(request())
                self.assertEqual(result.status, FAILED)
                self.assertIn(code, result.reason_codes)

    def test_duplicate_result_is_at_most_one_accepted_result(self):
        with LoopbackRemoteServer().start() as server:
            adapter = make_adapter(server)
            first = adapter.execute_lifecycle(request())
            second = adapter.execute_lifecycle(request())
            self.assertEqual(first.status, COMPLETED)
            self.assertEqual(second.status, FAILED)
            self.assertIn(DUPLICATE_RESULT, second.reason_codes)
            self.assertEqual(server.request_count, 1)

    def test_same_request_id_with_different_payload_is_rejected(self):
        with LoopbackRemoteServer().start() as server:
            adapter = make_adapter(server)
            self.assertEqual(adapter.execute_lifecycle(request()).status, COMPLETED)
            result = adapter.execute_lifecycle(
                request(text="different exact projected payload"))
            self.assertEqual(result.status, FAILED)
            self.assertIn(IDENTITY_MISMATCH, result.reason_codes)
            self.assertEqual(server.request_count, 1)

    def test_capability_claim_cannot_self_widen(self):
        with LoopbackRemoteServer().start() as server:
            config = RemoteWorkerConfig(
                "remote-worker-1", RemoteIdentity(endpoint=server.endpoint), 1,
                required_remote_capability="WRITES_FILES",
                remote_capabilities=("WRITES_FILES",), trusted_capabilities=())
            adapter = RemoteWorkerAdapter(LoopbackHttpTransport(), config)
            accepted, reasons = adapter.accepts(request())
            self.assertFalse(accepted)
            self.assertIn("CAPABILITY_NOT_TRUSTED:WRITES_FILES", reasons)
            self.assertEqual(server.request_count, 0)

    def test_replay_ledger_survives_process_death_reconciliation(self):
        with tempfile.TemporaryDirectory() as folder, LoopbackRemoteServer().start() as server:
            ledger_path = str(Path(folder) / "remote-ledger.json")
            first = make_adapter(server, ledger=JsonRemoteLedger(ledger_path))
            envelope = first.prepare(request())
            first.ledger.begin(envelope)
            code = (
                "import sys; "
                "from tabularium.engine.interop.model import RemoteIdentity; "
                "from tabularium.engine.projection.model import ProjectedPayload; "
                "from tabularium.engine.remote import JsonRemoteLedger, RemoteWorkerAdapter, RemoteWorkerConfig; "
                "from tabularium.engine.remote.fixture import LoopbackHttpTransport; "
                "from tabularium.engine.workers.model import WorkerRequest; "
                "p=ProjectedPayload(((\"required\",{\"text\":\"bounded remote work\"}),)); "
                "r=WorkerRequest('request-1','continue',p,task_id='TASK-2026-0001',"
                "correlation={'execution_id':'execution-1','task_id':'TASK-2026-0001'}); "
                "c=RemoteWorkerConfig('remote-worker-1',RemoteIdentity(endpoint=sys.argv[1]),1); "
                "a=RemoteWorkerAdapter(LoopbackHttpTransport(),c,ledger=JsonRemoteLedger(sys.argv[2])); "
                "print(a.reconcile(r).status)"
            )
            child = subprocess.run([sys.executable, "-c", code, server.endpoint,
                                    ledger_path], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(child.stdout.strip(), COMPLETED)
            self.assertEqual(server.request_count, 1)
            self.assertEqual(make_adapter(server, ledger=JsonRemoteLedger(ledger_path))
                             .execute_lifecycle(request()).status, FAILED)

    def test_cancellation_is_best_effort_and_late_result_is_inert(self):
        with LoopbackRemoteServer(LoopbackMode.DELAYED, delay_s=0.3).start() as server:
            adapter = make_adapter(server, timeout=1)
            req = request()
            envelope = adapter.prepare(req)
            holder = []
            thread = threading.Thread(target=lambda: holder.append(
                adapter.execute_lifecycle(req)))
            thread.start()
            deadline = time.time() + 1
            while time.time() < deadline and not adapter._active:
                time.sleep(0.005)
            cancellation = adapter.cancel_lifecycle(req.request_id)
            thread.join(timeout=2)
            self.assertTrue(cancellation.remote_confirmed)
            self.assertIn(REMOTE_CANCEL_CONFIRMED,
                          adapter.ledger.get(envelope.idempotency_key).reason_codes)
            self.assertEqual(holder[0].status, CANCELLED)
            late = adapter.accept_late_response(envelope, {})
            self.assertEqual(late.status, CANCELLED)
            self.assertIn(LATE_RESULT, late.reason_codes)
            self.assertEqual(holder[0].worker_result.status, WORKER_CANCELLED)

    def test_two_executions_are_isolated(self):
        with LoopbackRemoteServer().start() as server:
            adapter = make_adapter(server)
            results = {}

            def run(item):
                results[item] = adapter.execute_lifecycle(
                    request(f"request-{item}", f"execution-{item}",
                            f"TASK-2026-{item:04d}"))

            threads = [threading.Thread(target=run, args=(1,)),
                       threading.Thread(target=run, args=(2,))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
            self.assertEqual({results[1].execution_id, results[2].execution_id},
                             {"execution-1", "execution-2"})
            self.assertEqual({results[1].task_id, results[2].task_id},
                             {"TASK-2026-0001", "TASK-2026-0002"})

    def test_remote_output_does_not_index_memory_or_authorize_anything(self):
        with LoopbackRemoteServer(LoopbackMode.AUTHORITY_LOOKING).start() as server:
            memory = InMemoryMemoryStore()
            result = make_adapter(server).execute_lifecycle(request())
            self.assertEqual(memory.records, ())
            self.assertEqual(result.status, COMPLETED)
            # Only an explicit source-bound Tabularium call can index a memory.
            memory.index(MemoryRecord("m", "p", "NOTE", "s", "v1",
                                      result.worker_result.output))
            self.assertEqual(len(memory.records), 1)

    def test_observability_metadata_only_and_no_credentials(self):
        collector = InMemoryCollector()
        with LoopbackRemoteServer().start() as server:
            adapter = make_adapter(server, collector=collector)
            adapter.transport.credential_provider = lambda: {
                "Authorization": "Bearer sk-remote-secret-123456"
            }
            adapter.execute_lifecycle(request())
        trace = json.dumps([event.to_dict() for event in collector.events])
        self.assertIn("REMOTE_WORKER_REQUESTED", trace)
        self.assertIn("REMOTE_WORKER_COMPLETED", trace)
        self.assertNotIn("sk-remote-secret", trace)
        self.assertNotIn("loopback remote result", trace)
        self.assertNotIn('"output":', trace)

    def test_token_economy_is_reproducible_through_transport(self):
        fixture = large_project()
        self.addCleanup(fixture.cleanup)
        package = fixture.load(task="TASK-2026-0001")
        projection = project(package, intent=INTENT, profile=HOT)
        request_value = WorkerRequest(
            "economy-remote", INTENT, projection.payload, task_id="TASK-2026-0001",
            correlation={"execution_id": "execution-economy",
                         "task_id": "TASK-2026-0001"})
        with LoopbackRemoteServer().start() as server:
            adapter = make_adapter(server)
            envelope = adapter.prepare(request_value)
            first = measure(envelope.interop_request.to_dict())
            transport = measure(envelope.to_dict())
            second = measure(envelope.to_dict())
            lifecycle = adapter.execute_lifecycle(request_value)
            actual_transport = measure(adapter.transport.last_transport_body)
        # Independently re-pinned. Derived deterministically from the
        # synthetic `large_project()` temporary fixture alone; no repository
        # content is read, so no clean-baseline documentation delta can
        # account for it. The inherited value
        # ({"chars": 34973, "estimated_tokens": 8744}) was stale on arrival;
        # the exact historical reason is not established by repository
        # evidence and is not claimed here.
        # 34905 at the /tmp pin (5 chars from the tabularium_ruleset key rename); pinned_size() removes the TMPDIR share.
        self.assertEqual(projection.economy.governed_context,
                         pinned_size(fixture, 34905))
        self.assertEqual(projection.economy.payload_size,
                         {"chars": 4797, "estimated_tokens": 1200})
        self.assertEqual(first, measure(envelope.interop_request.to_dict()))
        self.assertEqual(transport, second)
        self.assertEqual(lifecycle.status, COMPLETED)
        self.assertEqual(transport, actual_transport)
        self.assertIn("request_preparation_ms", lifecycle.metrics)
        self.assertIn("response_parse_ms", lifecycle.metrics)
        self.assertIn("result_mapping_ms", lifecycle.metrics)
        self.assertLess(transport["estimated_tokens"],
                        projection.economy.governed_context["estimated_tokens"])

    def test_network_import_is_silent_and_remote_core_has_no_oss_leak(self):
        source = "import socket; before=socket.socket; import tabularium.engine.remote; print('ok' if socket.socket is before else 'bad')"
        proc = subprocess.run([sys.executable, "-c", source], cwd=ROOT,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "ok")
        for path in (ROOT / "tabularium" / "engine" / "remote").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.append((node.module or "").split(".")[0])
            self.assertNotIn("a2a", imports)
            self.assertNotIn("httpx", imports)
            self.assertNotIn("requests", imports)

    def test_tls_verification_is_default_and_no_insecure_switch_exists(self):
        source = (ROOT / "tabularium" / "engine" / "remote" / "transport.py").read_text(encoding="utf-8")
        self.assertIn("ssl.create_default_context", source)
        self.assertNotIn("verify=False", source)


class TestM63TargetedSecurityCorrections(unittest.TestCase):
    def test_same_request_two_fresh_processes_only_one_sends(self):
        with tempfile.TemporaryDirectory() as folder, \
                LoopbackRemoteServer(LoopbackMode.DELAYED, delay_s=0.15).start() as server:
            ledger_path = str(Path(folder) / "remote-ledger.json")
            processes = [subprocess.Popen(
                [sys.executable, "-c", (
                    "import json,sys; "
                    "from tabularium.engine.interop.model import RemoteIdentity; "
                    "from tabularium.engine.projection.model import ProjectedPayload; "
                    "from tabularium.engine.remote import JsonRemoteLedger,RemoteWorkerAdapter,RemoteWorkerConfig; "
                    "from tabularium.engine.remote.fixture import LoopbackHttpTransport; "
                    "from tabularium.engine.workers.model import WorkerRequest; "
                    "p=ProjectedPayload(((\"required\",{\"text\":\"bounded remote work\"}),)); "
                    "r=WorkerRequest('same-process-race','continue',p,task_id='TASK-RACE',"
                    "correlation={'execution_id':'EXEC-RACE','task_id':'TASK-RACE'}); "
                    "c=RemoteWorkerConfig('remote-worker-1',RemoteIdentity(endpoint=sys.argv[1]),1); "
                    "a=RemoteWorkerAdapter(LoopbackHttpTransport(),c,ledger=JsonRemoteLedger(sys.argv[2])); "
                    "v=a.execute_lifecycle(r); print(json.dumps({'status':v.status,'reasons':list(v.reason_codes)}))"
                ), server.endpoint, ledger_path], cwd=ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(2)]
            outputs = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr)
                outputs.append(json.loads(stdout))
            self.assertEqual(server.request_count, 1)
            self.assertEqual(sum(item["status"] == COMPLETED for item in outputs), 1)
            self.assertEqual(sum(DUPLICATE_RESULT in item["reasons"] for item in outputs), 1)
            ledger = json.loads(Path(ledger_path).read_text(encoding="utf-8"))
            self.assertEqual(len(ledger), 1)
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])
            self.assertEqual(list(Path(folder).glob(".*.tmp")), [])

    def test_cross_process_result_replay_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "remote-ledger.json"
            with LoopbackRemoteServer().start() as server:
                adapter = make_adapter(server, ledger=JsonRemoteLedger(path))
                envelope = adapter.prepare(request("replay-process"))
                self.assertIsNone(adapter.ledger.begin(envelope))
                item = LedgerRecord(
                    key=envelope.idempotency_key, execution_id=envelope.execution_id,
                    task_id=envelope.task_id, worker_id=envelope.worker_id,
                    payload_digest=envelope.payload_digest, status=COMPLETED,
                    result_fingerprint="same-result")
                self.assertTrue(adapter.ledger.record(item))
            code = (
                "import json,sys; from tabularium.engine.remote import JsonRemoteLedger; "
                "from tabularium.engine.remote.ledger import LedgerRecord; "
                "item=LedgerRecord.from_dict(json.loads(sys.argv[2])); "
                "print(JsonRemoteLedger(sys.argv[1]).record(item))"
            )
            child = subprocess.run(
                [sys.executable, "-c", code, str(path), json.dumps(item.to_dict())],
                cwd=ROOT, capture_output=True, text=True, timeout=10)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(child.stdout.strip(), "False")
            self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))), 1)

    def test_different_process_requests_are_independent_and_json_remains_valid(self):
        with tempfile.TemporaryDirectory() as folder, \
                LoopbackRemoteServer().start() as server:
            path = str(Path(folder) / "remote-ledger.json")
            args = [("request-a", "execution-a", "TASK-A"),
                    ("request-b", "execution-b", "TASK-B")]
            processes = [subprocess.Popen(
                [sys.executable, "-c", (
                    "import json,sys; "
                    "from tabularium.engine.interop.model import RemoteIdentity; "
                    "from tabularium.engine.projection.model import ProjectedPayload; "
                    "from tabularium.engine.remote import JsonRemoteLedger,RemoteWorkerAdapter,RemoteWorkerConfig; "
                    "from tabularium.engine.remote.fixture import LoopbackHttpTransport; "
                    "from tabularium.engine.workers.model import WorkerRequest; "
                    "p=ProjectedPayload(((\"required\",{\"text\":sys.argv[3]}),)); "
                    "r=WorkerRequest(sys.argv[4],'continue',p,task_id=sys.argv[5],"
                    "correlation={'execution_id':sys.argv[6],'task_id':sys.argv[5]}); "
                    "c=RemoteWorkerConfig('remote-worker-1',RemoteIdentity(endpoint=sys.argv[1]),1); "
                    "v=RemoteWorkerAdapter(LoopbackHttpTransport(),c,ledger=JsonRemoteLedger(sys.argv[2])).execute_lifecycle(r); "
                    "print(v.status)"
                ), server.endpoint, path, item[0], item[0], item[2], item[1], item[2]],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for item in args]
            for process in processes:
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(stdout.strip(), COMPLETED)
            self.assertEqual(server.request_count, 2)
            self.assertEqual(len(json.loads(Path(path).read_text(encoding="utf-8"))), 2)
            self.assertEqual(list(Path(folder).glob(".*.tmp")), [])

    def test_ledger_lock_failure_is_controlled_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder, LoopbackRemoteServer().start() as server:
            blocked_parent = Path(folder) / "not-a-directory"
            blocked_parent.write_text("fixture", encoding="utf-8")
            ledger = JsonRemoteLedger(blocked_parent / "remote-ledger.json")
            result = make_adapter(server, ledger=ledger).execute_lifecycle(
                request("ledger-lock-failure"))
            self.assertEqual(result.status, UNKNOWN)
            self.assertIn("LEDGER_LOCK_FAILED", result.reason_codes)
            self.assertEqual(server.request_count, 0)


class _RedirectFixtureHandler(BaseHTTPRequestHandler):
    target = ""
    seen_authorization = []
    received = 0
    response_code = 302

    def do_POST(self):  # noqa: N802
        type(self).received += 1
        type(self).seen_authorization.append(self.headers.get("Authorization"))
        self.send_response(type(self).response_code)
        self.send_header("Location", type(self).target)
        self.end_headers()

    def log_message(self, *_args):
        return


class _RedirectTargetHandler(BaseHTTPRequestHandler):
    received = 0

    def do_POST(self):  # noqa: N802
        type(self).received += 1
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_args):
        return


class TestM63RedirectPolicy(unittest.TestCase):
    def test_all_redirects_are_refused_and_credentials_do_not_leak(self):
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                _RedirectTargetHandler.received = 0
                target = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectTargetHandler)
                target_thread = threading.Thread(target=target.serve_forever, daemon=True)
                target_thread.start()
                source_handler = type("SourceHandler", (_RedirectFixtureHandler,), {})
                source_handler.target = f"http://127.0.0.1:{target.server_port}/target"
                source_handler.response_code = code
                source_handler.received = 0
                source_handler.seen_authorization = []
                source = ThreadingHTTPServer(("127.0.0.1", 0), source_handler)
                source_thread = threading.Thread(target=source.serve_forever, daemon=True)
                source_thread.start()
                try:
                    config = RemoteWorkerConfig(
                        "redirect-worker", RemoteIdentity(endpoint=f"http://127.0.0.1:{source.server_port}"), 1)
                    adapter = RemoteWorkerAdapter(
                        UrllibJsonTransport(credential_provider=lambda: {
                            "Authorization": "Bearer fake-test-only"
                        }), config)
                    envelope = adapter.prepare(request("redirect-" + str(code)))
                    with self.assertRaises(RemoteTransportFailure) as caught:
                        adapter.transport.send(envelope)
                    self.assertEqual(caught.exception.code, "REDIRECT_REFUSED")
                    self.assertEqual(_RedirectTargetHandler.received, 0)
                    self.assertEqual(source_handler.seen_authorization,
                                     ["Bearer fake-test-only"])
                finally:
                    source.shutdown(); source.server_close()
                    target.shutdown(); target.server_close()

    def test_redirect_is_refused_without_credentials(self):
        _RedirectTargetHandler.received = 0
        target = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectTargetHandler)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()
        source_handler = type("SourceHandler", (_RedirectFixtureHandler,), {})
        source_handler.target = f"http://127.0.0.1:{target.server_port}/target"
        source_handler.response_code = 302
        source_handler.received = 0
        source_handler.seen_authorization = []
        source = ThreadingHTTPServer(("127.0.0.1", 0), source_handler)
        source_thread = threading.Thread(target=source.serve_forever, daemon=True)
        source_thread.start()
        try:
            config = RemoteWorkerConfig(
                "redirect-worker", RemoteIdentity(endpoint=f"http://127.0.0.1:{source.server_port}"), 1)
            transport = UrllibJsonTransport()
            envelope = RemoteWorkerAdapter(transport, config).prepare(request("redirect-no-auth"))
            with self.assertRaises(RemoteTransportFailure) as caught:
                transport.send(envelope)
            self.assertEqual(caught.exception.code, "REDIRECT_REFUSED")
            self.assertEqual(_RedirectTargetHandler.received, 0)
            self.assertEqual(source_handler.seen_authorization, [None])
        finally:
            source.shutdown(); source.server_close()
            target.shutdown(); target.server_close()


if __name__ == "__main__":
    unittest.main()
