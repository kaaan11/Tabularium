"""Tests for the M4.1 LangGraph production runtime.

Process-death resume is exercised against PRODUCTION code (`tabularium/engine/runtime/`),
not the M3.7 spike: each phase runs in a genuinely new OS process, so an
in-memory checkpoint cannot satisfy it.

The framework-neutral tests never import LangGraph. Tests that drive a real
execution are gated on the runtime dependency being installed, because
LangGraph is declared in `tabularium/engine/requirements-runtime.txt` and the rest of
the repository must keep working without it.

Run:  python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tabularium.engine.runtime import model as M  # noqa: E402
from tabularium.engine.runtime import steps as S  # noqa: E402
from tabularium.engine.workers import (COMPLETED as WORKER_COMPLETED,  # noqa: E402
                             FAILED as WORKER_FAILED, MockWorker,
                             WorkerDescriptor, WorkerRequest, WorkerResult)

try:
    import langgraph  # noqa: F401
    LANGGRAPH = True
except ImportError:  # pragma: no cover - environment dependent
    LANGGRAPH = False

needs_runtime = unittest.skipUnless(
    LANGGRAPH, "LangGraph not installed (tabularium/engine/requirements-runtime.txt)")


def human_approval(request, execution_id, decision="APPROVE",
                   task_id="TASK-2026-0001", decided_by="human:user",
                   decided_at="2026-08-18T12:00:00Z", scope=None):
    """A valid M4.3 human resolution bound to `request`.

    Approval now REQUIRES provenance, so tests construct a real resolution
    rather than a bare decision string.
    """
    from tabularium.engine.runtime import ApprovalScope, HumanApprovalResolution
    request_id = getattr(request, "request_id", None) or (
        request.get("request_id") if isinstance(request, dict) else "")
    return HumanApprovalResolution(
        approval_request_id=request_id, execution_id=execution_id,
        task_id=task_id, decision=decision, decided_by=decided_by,
        decided_at=decided_at,
        scope=scope if scope is not None else ApprovalScope())


class CategoryWorker:
    """A deterministic WorkerAdapter with a chosen category and output.

    Proves the runtime does not care whether a worker is LOCAL, CLI_CODING, or
    API_MODEL, and lets a resumed execution continue on a different worker.
    """

    def __init__(self, worker_id="worker-a", category="LOCAL",
                 output="WORKER_A_OUTPUT", status=WORKER_COMPLETED):
        self._descriptor = WorkerDescriptor(worker_id=worker_id,
                                            category=category,
                                            capabilities=("STRUCTURED_OUTPUT",))
        self._output = output
        self._status = status

    @property
    def descriptor(self):
        return self._descriptor

    def accepts(self, request):
        return True, ()

    def execute(self, request):
        return WorkerResult(request_id=request.request_id,
                            worker_id=self._descriptor.worker_id,
                            status=self._status, output=self._output)

    def cancel(self, request_id):
        return False


def make_request(**overrides) -> WorkerRequest:
    base = {"request_id": "req-1", "intent": "continue the project",
            "payload": {"required": {"task": {"id": "TASK-2026-0001"}}},
            "task_id": "TASK-2026-0001"}
    base.update(overrides)
    return WorkerRequest(**base)


def state(**overrides) -> M.ExecutionState:
    base = {"execution_id": "exec-1", "request_id": "req-1",
            "intent": "continue the project",
            "payload": {"required": {}}}
    base.update(overrides)
    return M.ExecutionState(**base)


# --- framework-neutral governance -------------------------------------------

class TestRuntimeState(unittest.TestCase):
    """State is Tabularium-owned, plain, and carries no framework concept."""

    def test_state_has_no_framework_fields(self):
        fields = set(M.ExecutionState.__dataclass_fields__)
        for leaked in ("messages", "chat_history", "memory", "agent",
                       "planner", "thread", "checkpoint", "graph",
                       "project_root", "context_package", "credentials"):
            with self.subTest(leaked=leaked):
                self.assertNotIn(leaked, fields)

    def test_state_round_trips_as_plain_json(self):
        original = state(worker_result={"status": "COMPLETED", "output": "x"})
        text = json.dumps(original.to_dict())
        self.assertEqual(M.ExecutionState.from_dict(json.loads(text)).to_dict(),
                         original.to_dict())

    def test_unknown_keys_are_dropped(self):
        # LangGraph injects `__interrupt__` into the mapping it returns.
        data = state().to_dict()
        data["__interrupt__"] = ["framework bookkeeping"]
        loaded = M.ExecutionState.from_dict(data)
        self.assertFalse(hasattr(loaded, "__interrupt__"))

    def test_state_version_is_explicit(self):
        self.assertEqual(state().state_version, M.STATE_VERSION)

    def test_runtime_status_vocabulary_is_distinct_from_worker_status(self):
        from tabularium.engine.workers import RESULT_STATUSES
        self.assertIn(M.AWAITING_APPROVAL, M.RUNTIME_STATUSES)
        self.assertNotIn(M.AWAITING_APPROVAL, RESULT_STATUSES)
        self.assertNotIn(M.RUNNING, RESULT_STATUSES)

    def test_steps_import_no_framework(self):
        # These modules may DISCUSS LangGraph in prose — they explain why the
        # boundary exists. What they must not do is IMPORT it.
        for name in ("steps.py", "model.py"):
            source = (ROOT / "tabularium" / "engine" / "runtime" / name).read_text(
                encoding="utf-8")
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")):
                    with self.subTest(module=name, line=stripped):
                        self.assertNotIn("langgraph", stripped.lower())

    def test_unknown_decisions_are_refused(self):
        from tabularium.engine.runtime import HumanApprovalResolution
        from tabularium.engine.runtime.approval import (APPROVAL_DECISION_INVALID,
                                              validate)
        pending = S.approval_boundary(state(approval_required=True))
        request = M.ApprovalRequest.from_dict(pending.approval_request)
        for bad in ("YES", "approve", "OK", "", "CANCEL"):
            with self.subTest(bad=bad):
                verdict = validate(HumanApprovalResolution(
                    approval_request_id=request.request_id,
                    execution_id="exec-1", decision=bad,
                    decided_by="human:user",
                    decided_at="2026-08-18T12:00:00Z"), request, "exec-1")
                self.assertFalse(verdict.valid)
                self.assertIn(APPROVAL_DECISION_INVALID, verdict.reason_codes)


class TestGovernanceSteps(unittest.TestCase):
    """The gate logic, tested without any runtime."""

    def test_validate_rejects_missing_input(self):
        for missing in ("execution_id", "request_id", "intent"):
            with self.subTest(missing=missing):
                result = S.validate_input(state(**{missing: ""}))
                self.assertEqual(result.runtime_status, M.FAILED)
                self.assertEqual(result.diagnostics[-1]["code"],
                                 M.INVALID_RUNTIME_INPUT)

    def test_worker_result_is_recorded_not_interpreted_as_authority(self):
        result = S.execute_worker(state(), CategoryWorker())
        self.assertEqual(result.worker_result["status"], WORKER_COMPLETED)
        self.assertNotEqual(result.runtime_status, M.COMPLETED)

    def test_failed_worker_fails_the_execution(self):
        result = S.execute_worker(state(), CategoryWorker(status=WORKER_FAILED))
        self.assertEqual(result.runtime_status, M.FAILED)
        self.assertEqual(result.diagnostics[-1]["code"], M.WORKER_FAILED)

    def test_missing_adapter_is_a_governed_outcome(self):
        result = S.execute_worker(state(), None)
        self.assertEqual(result.runtime_status, M.FAILED)
        self.assertEqual(result.diagnostics[-1]["code"], M.WORKER_UNAVAILABLE)

    def test_approval_boundary_sets_a_first_class_status(self):
        result = S.approval_boundary(S.execute_worker(
            state(approval_required=True), CategoryWorker()))
        self.assertEqual(result.runtime_status, M.AWAITING_APPROVAL)
        self.assertTrue(result.approval_request["request_id"])

    def test_resume_without_resolution_is_not_approval(self):
        from tabularium.engine.runtime.approval import APPROVAL_RESOLUTION_MALFORMED
        pending = S.approval_boundary(state(approval_required=True))
        result = S.resolve_approval(pending, None)
        self.assertIsNone(result.approval_resolution)
        self.assertEqual(result.diagnostics[-1]["code"],
                         APPROVAL_RESOLUTION_MALFORMED)

    def test_truthy_values_are_not_approval(self):
        pending = S.approval_boundary(state(approval_required=True))
        for value in (True, 1, "APPROVE", "yes", ["APPROVE"], {"ok": True}):
            with self.subTest(value=value):
                result = S.resolve_approval(pending, value)
                self.assertIsNone(result.approval_resolution)
                self.assertNotEqual(result.runtime_status, M.COMPLETED)

    def test_resolution_must_match_the_pending_request(self):
        from tabularium.engine.runtime import HumanApprovalResolution
        from tabularium.engine.runtime.approval import APPROVAL_REQUEST_MISMATCH
        pending = S.approval_boundary(state(approval_required=True))
        result = S.resolve_approval(pending, HumanApprovalResolution(
            approval_request_id="someone-else", execution_id="exec-1",
            decision="APPROVE", decided_by="human:user",
            decided_at="2026-08-18T12:00:00Z"))
        self.assertIsNone(result.approval_resolution)
        self.assertIn(APPROVAL_REQUEST_MISMATCH, result.diagnostics[-1]["code"])

    def test_approve_and_reject_require_provenance(self):
        pending = S.approval_boundary(state(approval_required=True))
        request = M.ApprovalRequest.from_dict(pending.approval_request)
        approve = human_approval(request, "exec-1", "APPROVE", task_id="")
        reject = human_approval(request, "exec-1", "REJECT", task_id="")
        self.assertEqual(S.resolve_approval(pending, approve).runtime_status,
                         M.RUNNING)
        self.assertEqual(S.resolve_approval(pending, reject).runtime_status,
                         M.REJECTED)

    def test_completion_is_blocked_without_approval_in_force(self):
        pending = S.approval_boundary(S.execute_worker(
            state(approval_required=True), CategoryWorker()))
        self.assertNotEqual(S.complete(pending).runtime_status, M.COMPLETED)

    def test_typed_resolution_object_is_accepted(self):
        pending = S.approval_boundary(state(approval_required=True))
        request = M.ApprovalRequest.from_dict(pending.approval_request)
        result = S.resolve_approval(
            pending, human_approval(request, "exec-1", task_id=""))
        self.assertEqual(result.approval_resolution["decision"], "APPROVE")
        self.assertEqual(result.approval_resolution["decided_by"],
                         "human:user")

    def test_runtime_approval_is_not_a_canonical_approval_record(self):
        # M4.1 authorizes the RUNTIME to continue. It does not write
        # approved_by/approved_at anywhere; that is a later M4 unit.
        pending = S.approval_boundary(state(approval_required=True))
        request = M.ApprovalRequest.from_dict(pending.approval_request)
        result = S.resolve_approval(
            pending, human_approval(request, "exec-1", task_id=""))
        blob = json.dumps(result.to_dict())
        for field in ("approved_by", "approved_at", "schema_version"):
            with self.subTest(field=field):
                self.assertNotIn(field, blob)


# --- runtime integration ----------------------------------------------------

@needs_runtime
class RuntimeTestCase(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="m41-")
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)
        self.db = os.path.join(self.workdir, "checkpoints.db")

    def runtime(self, **kwargs):
        from tabularium.engine.runtime import LangGraphRuntime
        return LangGraphRuntime(checkpoint_path=self.db, **kwargs)


class TestRuntimeExecution(RuntimeTestCase):

    def test_completes_without_approval(self):
        result = self.runtime().start("exec-1", make_request(),
                                      adapter=MockWorker())
        self.assertEqual(result.runtime_status, M.COMPLETED)
        self.assertTrue(result.completed)
        self.assertIn("mock executed", result.output)

    def test_suspends_when_approval_is_required(self):
        result = self.runtime().start("exec-1", make_request(),
                                      approval_required=True,
                                      adapter=MockWorker())
        self.assertEqual(result.runtime_status, M.AWAITING_APPROVAL)
        self.assertTrue(result.awaiting_approval)
        self.assertIsNotNone(result.approval_request)
        self.assertEqual(result.approval_request.execution_id, "exec-1")

    def test_worker_result_survives_the_suspension(self):
        rt = self.runtime()
        rt.start("exec-1", make_request(), True, MockWorker())
        stored = rt.inspect_execution("exec-1")
        self.assertEqual(stored.worker_result["status"], WORKER_COMPLETED)
        self.assertEqual(stored.runtime_status, M.AWAITING_APPROVAL)

    def test_invalid_input_is_refused(self):
        rt = self.runtime()
        self.assertEqual(rt.start("", make_request()).runtime_status, M.FAILED)
        bad = rt.start("exec-1", object())
        self.assertEqual(bad.reason_codes, (M.INVALID_RUNTIME_INPUT,))

    def test_checkpoint_path_is_required(self):
        from tabularium.engine.runtime import LangGraphRuntime
        for bad in ("", "   ", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    LangGraphRuntime(checkpoint_path=bad)

    def test_worker_rejection_is_a_runtime_failure(self):
        result = self.runtime().start(
            "exec-1", make_request(required_category="API_MODEL"),
            adapter=MockWorker())
        self.assertEqual(result.runtime_status, M.FAILED)
        self.assertIn(M.WORKER_REJECTED, result.reason_codes)


class TestApprovalMechanics(RuntimeTestCase):

    def pending(self, execution_id="exec-1"):
        rt = self.runtime()
        rt.start(execution_id, make_request(), True, MockWorker())
        return rt

    def test_approve_completes(self):
        rt = self.pending()
        request = rt.inspect_execution("exec-1").approval_request
        result = rt.resume("exec-1", human_approval(
            M.ApprovalRequest.from_dict(request), "exec-1"),
            adapter=MockWorker())
        self.assertEqual(result.runtime_status, M.COMPLETED)
        self.assertIn(M.APPROVAL_GRANTED, result.reason_codes)

    def test_reject_halts(self):
        rt = self.pending()
        request = rt.inspect_execution("exec-1").approval_request
        result = rt.resume("exec-1", human_approval(
            M.ApprovalRequest.from_dict(request), "exec-1", "REJECT"),
            adapter=MockWorker())
        self.assertEqual(result.runtime_status, M.REJECTED)
        self.assertEqual(result.output, "")

    def test_cancel_is_a_runtime_action_not_a_human_decision(self):
        # CANCEL is not in the human decision vocabulary: conflating "the
        # human declined" with "the operator stopped it" would blur who
        # decided. Cancellation is `runtime.cancel`.
        from tabularium.engine.runtime import DECISIONS
        self.assertEqual(set(DECISIONS), {"APPROVE", "REJECT"})
        rt = self.pending()
        self.assertEqual(rt.cancel("exec-1").runtime_status, M.CANCELLED)

    def test_invalid_resume_fails_closed(self):
        # THE CENTRAL INVARIANT: resuming is not approving.
        from tabularium.engine.runtime import HumanApprovalResolution
        from tabularium.engine.runtime.approval import (APPROVAL_REQUEST_MISMATCH,
                                              APPROVAL_RESOLUTION_MALFORMED)
        MALFORMED = APPROVAL_RESOLUTION_MALFORMED
        cases = {
            "none": (None, MALFORMED),
            "bare_true": (True, MALFORMED),
            "bare_string": ("APPROVE", MALFORMED),
            "unknown": ({"decision": "yes please"}, MALFORMED),
            "malformed": ({"approved": True}, MALFORMED),
            "wrong_request": (HumanApprovalResolution(
                approval_request_id="other-request", execution_id="exec-1",
                decision="APPROVE", decided_by="human:user",
                decided_at="2026-08-18T12:00:00Z"),
                APPROVAL_REQUEST_MISMATCH),
        }
        for label, (resolution, expected) in cases.items():
            with self.subTest(case=label):
                rt = self.pending(f"exec-{label}")
                if isinstance(resolution, HumanApprovalResolution):
                    resolution = HumanApprovalResolution(
                        **{**resolution.to_dict(),
                           "execution_id": f"exec-{label}",
                           "scope": resolution.scope})
                result = rt.resume(f"exec-{label}", resolution,
                                   adapter=MockWorker())
                self.assertNotEqual(result.runtime_status, M.COMPLETED)
                self.assertIn(expected, result.reason_codes)
                self.assertEqual(result.output, "")

    def test_langgraph_resume_none_defect_is_guarded(self):
        # LangGraph 1.2.11: Command(resume=None) raises an internal
        # UnboundLocalError. Tabularium judges the resolution BEFORE the framework
        # is reached, so a None resume never becomes a framework traceback.
        from tabularium.engine.runtime.approval import APPROVAL_RESOLUTION_MALFORMED
        rt = self.pending()
        result = rt.resume("exec-1", None, adapter=MockWorker())
        self.assertEqual(result.runtime_status, M.AWAITING_APPROVAL)
        self.assertIn(APPROVAL_RESOLUTION_MALFORMED, result.reason_codes)
        for diagnostic in result.diagnostics:
            self.assertNotIn("UnboundLocalError", diagnostic.get("detail", ""))

    def test_refused_resolution_leaves_the_request_retryable(self):
        rt = self.pending()
        rt.resume("exec-1", True, adapter=MockWorker())
        still = rt.inspect_execution("exec-1")
        self.assertEqual(still.runtime_status, M.AWAITING_APPROVAL)
        good = rt.resume("exec-1", human_approval(
            M.ApprovalRequest.from_dict(still.approval_request), "exec-1"),
            adapter=MockWorker())
        self.assertEqual(good.runtime_status, M.COMPLETED)

    def test_resume_of_a_terminal_execution_is_refused(self):
        rt = self.runtime()
        rt.start("exec-1", make_request(), adapter=MockWorker())
        result = rt.resume("exec-1", human_approval(None, "exec-1"),
                           adapter=MockWorker())
        self.assertIn(M.NOTHING_TO_RESUME, result.reason_codes)

    def test_resume_of_an_unknown_execution_is_refused(self):
        result = self.runtime().resume("never-started",
                                       human_approval(None, "never-started"))
        self.assertIn(M.EXECUTION_NOT_FOUND, result.reason_codes)

    def test_cancel_marks_without_completing(self):
        rt = self.pending()
        result = rt.cancel("exec-1")
        self.assertEqual(result.runtime_status, M.CANCELLED)
        self.assertNotEqual(result.runtime_status, M.COMPLETED)


class TestInspection(RuntimeTestCase):

    def test_inspect_reports_pending_state_without_mutating(self):
        rt = self.pending = self.runtime()
        rt.start("exec-1", make_request(), True, MockWorker())
        before = hashlib.sha256(Path(self.db).read_bytes()).hexdigest()
        first = rt.inspect_execution("exec-1")
        second = rt.inspect_execution("exec-1")
        after = hashlib.sha256(Path(self.db).read_bytes()).hexdigest()
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(before, after)
        self.assertEqual(first.runtime_status, M.AWAITING_APPROVAL)
        self.assertEqual(first.approval_request["execution_id"], "exec-1")

    def test_inspect_reports_completed(self):
        rt = self.runtime()
        rt.start("exec-1", make_request(), adapter=MockWorker())
        self.assertEqual(rt.inspect_execution("exec-1").runtime_status,
                         M.COMPLETED)

    def test_inspect_unknown_execution_returns_none(self):
        self.assertIsNone(self.runtime().inspect_execution("nope"))


class TestWorkerNeutrality(RuntimeTestCase):

    def test_every_worker_category_runs_through_one_graph(self):
        for category in ("LOCAL", "CLI_CODING", "API_MODEL"):
            with self.subTest(category=category):
                worker = CategoryWorker(worker_id=f"w-{category}",
                                        category=category,
                                        output=f"{category}_OUTPUT")
                result = self.runtime().start(f"exec-{category}",
                                              make_request(), adapter=worker)
                self.assertEqual(result.runtime_status, M.COMPLETED)
                self.assertEqual(result.output, f"{category}_OUTPUT")

    def test_api_category_does_not_import_litellm(self):
        # An API-category worker that does not use LiteLLM must not drag it in.
        code = ("import sys; sys.path.insert(0, r'%s');"
                "import tabularium.engine.runtime;"
                "print('litellm' in sys.modules)" % ROOT)
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                             capture_output=True, text=True, shell=False)
        self.assertEqual(out.stdout.strip(), "False", out.stderr)

    def test_worker_swap_across_the_interrupt(self):
        # The checkpoint binds no worker identity: a resumed execution builds
        # the graph with a DIFFERENT adapter and still completes. The recorded
        # result stays worker A's, because worker A is what actually ran.
        rt = self.runtime()
        rt.start("exec-1", make_request(), True,
                 CategoryWorker("worker-a", "LOCAL", "A_OUT"))
        request = M.ApprovalRequest.from_dict(
            rt.inspect_execution("exec-1").approval_request)
        result = rt.resume("exec-1", human_approval(request, "exec-1"),
                           adapter=CategoryWorker("worker-b", "CLI_CODING",
                                                  "B_OUT"))
        self.assertEqual(result.runtime_status, M.COMPLETED)
        self.assertEqual(result.worker_result["worker_id"], "worker-a")

    def test_adapter_provider_seam(self):
        rt = self.runtime(
            adapter_provider=lambda: CategoryWorker("provided", "LOCAL",
                                                    "PROVIDED_OUT"))
        result = rt.start("exec-1", make_request())
        self.assertEqual(result.output, "PROVIDED_OUT")


class TestPayloadAndSafety(RuntimeTestCase):

    def test_runtime_does_not_alter_the_projected_payload(self):
        import copy
        request = make_request()
        before = copy.deepcopy(request.payload)
        rt = self.runtime()
        rt.start("exec-1", request, True, MockWorker())
        rt.resume("exec-1", {"decision": M.APPROVE}, adapter=MockWorker())
        self.assertEqual(request.payload, before)
        self.assertEqual(rt.inspect_execution("exec-1").payload, before)

    def test_runtime_injects_no_extra_context(self):
        seen = {}

        class Recorder(CategoryWorker):
            def execute(self, request):
                seen["payload"] = request.payload
                seen["intent"] = request.intent
                return super().execute(request)

        request = make_request()
        self.runtime().start("exec-1", request, adapter=Recorder())
        # The worker is handed the canonical ProjectedPayload (M4.7 bug #1),
        # so compare CONTENT: the point of this test is that the runtime adds
        # nothing, not which wrapper type carries it.
        seen_payload = seen["payload"]
        as_data = (seen_payload.to_dict() if hasattr(seen_payload, "to_dict")
                   else seen_payload)
        expected = (request.payload.to_dict()
                    if hasattr(request.payload, "to_dict") else request.payload)
        self.assertEqual(as_data, expected)
        self.assertEqual(seen["intent"], request.intent)

    def test_checkpoint_carries_no_credentials(self):
        import re
        rt = self.runtime()
        rt.start("exec-1", make_request(), True, MockWorker())
        blob = Path(self.db).read_bytes().decode("utf-8", errors="ignore")
        # Credential SHAPES, not fragments: a bare "sk-" also occurs inside
        # "TASK-2026-0001" and inside binary SQLite padding.
        patterns = (r"sk-[A-Za-z0-9]{8,}", r"api[_-]?key",
                    r"password", r"authorization",
                    r"bearer", r"OPENAI_API_KEY", r"ANTHROPIC_API_KEY")
        for pattern in patterns:
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, blob, re.IGNORECASE))

    def test_checkpoint_carries_no_project_root(self):
        rt = self.runtime()
        rt.start("exec-1", make_request(), True, MockWorker())
        blob = Path(self.db).read_bytes().decode("utf-8", errors="ignore")
        self.assertNotIn(str(ROOT), blob)
        self.assertNotIn(".ai/", blob)

    def test_project_context_is_never_written(self):
        def snapshot():
            digest = {}
            for path in sorted((ROOT / ".ai").rglob("*")):
                if path.is_file():
                    digest[str(path.relative_to(ROOT))] = hashlib.sha256(
                        path.read_bytes()).hexdigest()
            return digest

        before = snapshot()
        rt = self.runtime()
        rt.start("exec-1", make_request(), True, MockWorker())
        rt.inspect_execution("exec-1")
        rt.resume("exec-1", {"decision": M.APPROVE}, adapter=MockWorker())
        self.assertEqual(snapshot(), before)

    def test_no_m1_record_is_created(self):
        rt = self.runtime()
        rt.start("exec-1", make_request(), True, MockWorker())
        result = rt.resume("exec-1", {"decision": M.APPROVE},
                           adapter=MockWorker())
        blob = json.dumps(result.to_dict())
        for marker in ("EVD-", "DEC-", "HND-", "RPT-", "INV-", "QST-",
                       "schema_version", "approved_by", "reliability"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, blob)

    def test_checkpoint_lives_only_where_the_caller_said(self):
        rt = self.runtime()
        rt.start("exec-1", make_request(), adapter=MockWorker())
        written = [p.name for p in Path(self.workdir).iterdir()]
        self.assertTrue(any(name.startswith("checkpoints.db")
                            for name in written), written)

    def test_corrupted_checkpoint_fails_closed_with_checkpoint_error(self):
        # DRL-008: byte-level corruption of a checkpoint database must fail
        # closed as CHECKPOINT_ERROR rather than crashing or being treated as RUNTIME_ERROR.
        Path(self.db).write_bytes(b"CORRUPTED_NON_SQLITE_BYTES")
        rt = self.runtime()
        self.assertIsNone(rt.inspect_execution("exec-1"))
        res = rt.start("exec-1", make_request(), adapter=MockWorker())
        self.assertEqual(res.runtime_status, M.FAILED)
        self.assertIn(M.CHECKPOINT_ERROR, res.reason_codes)

    def test_worker_sqlite_fault_is_not_a_checkpoint_error(self):
        class BadDbWorker(MockWorker):
            def execute(self, request):
                import sqlite3
                raise sqlite3.OperationalError("no such table: worker_cache")

        rt = self.runtime()
        res = rt.start("exec-1", make_request(), adapter=BadDbWorker())
        self.assertEqual(res.runtime_status, M.FAILED)
        self.assertNotIn(M.CHECKPOINT_ERROR, res.reason_codes)
        self.assertIn(M.RUNTIME_ERROR, res.reason_codes)



class TestFrameworkContainment(RuntimeTestCase):

    def test_results_contain_no_framework_objects(self):
        rt = self.runtime()
        result = rt.start("exec-1", make_request(), True, MockWorker())
        for value in (result, result.approval_request):
            with self.subTest(value=type(value).__name__):
                self.assertIn("tabularium.engine.runtime", type(value).__module__)
        json.dumps(result.to_dict())  # must be plain and serializable

    def test_only_one_module_imports_langgraph(self):
        # The THIRD-PARTY package, not our own `langgraph_runtime` module —
        # `from .langgraph_runtime import ...` is an internal import and does
        # not pull the framework in.
        runtime_dir = ROOT / "tabularium" / "engine" / "runtime"
        importing = []
        for module in sorted(runtime_dir.glob("*.py")):
            for line in module.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith(("from langgraph",
                                        "import langgraph")):
                    importing.append(module.name)
                    break
        self.assertEqual(importing, ["langgraph_runtime.py"])

    def test_non_runtime_paths_do_not_import_langgraph(self):
        for module in ("tabularium.engine.context_loader", "tabularium.engine.projection",
                       "tabularium.engine.skills", "tabularium.engine.workers",
                       "tabularium.engine.workers.api", "tabularium.engine.runtime"):
            with self.subTest(module=module):
                code = (f"import sys, {module};"
                        "print('langgraph' in sys.modules)")
                out = subprocess.run([sys.executable, "-c", code],
                                     cwd=str(ROOT), capture_output=True,
                                     text=True, shell=False)
                self.assertEqual(out.stdout.strip(), "False", out.stderr)

    def test_frozen_loader_requirements_untouched(self):
        text = (ROOT / "tabularium" / "engine" / "requirements.txt").read_text(
            encoding="utf-8")
        self.assertNotIn("langgraph", text.lower())
        self.assertIn("PyYAML", text)

    def test_runtime_dependency_declared_separately(self):
        text = (ROOT / "tabularium" / "engine" / "requirements-runtime.txt").read_text(
            encoding="utf-8")
        self.assertIn("langgraph", text.lower())


# --- process-death durability -----------------------------------------------

PHASE_SCRIPT = r'''
import json, sys
sys.path.insert(0, r"{root}")
from tabularium.engine.runtime import LangGraphRuntime
from tabularium.engine.workers import WorkerRequest
sys.path.insert(0, r"{tests}")
from test_runtime import CategoryWorker

phase, db, execution_id = sys.argv[1], sys.argv[2], sys.argv[3]
rt = LangGraphRuntime(checkpoint_path=db)

if phase == "start":
    request = WorkerRequest(request_id="req-1", intent="continue the project",
                            payload={{"required": {{"task": {{"id": "TASK-2026-0001"}}}}}},
                            task_id="TASK-2026-0001")
    out = rt.start(execution_id, request, approval_required=True,
                   adapter=CategoryWorker("worker-a", "LOCAL", "DURABLE_OUT"))
elif phase == "resume":
    raw = json.loads(sys.argv[4])
    if isinstance(raw, dict) and raw.get("decision") in ("APPROVE", "REJECT"):
        from tabularium.engine.runtime import HumanApprovalResolution
        stored = rt.inspect_execution(execution_id)
        pending = (stored.approval_request or {{}}).get("request_id", "")
        raw = HumanApprovalResolution(
            approval_request_id=raw.get("request_id") or pending,
            execution_id=execution_id, task_id="TASK-2026-0001",
            decision=raw["decision"], decided_by="human:user",
            decided_at="2026-08-18T12:00:00Z")
    out = rt.resume(execution_id, raw,
                    adapter=CategoryWorker("worker-b", "CLI_CODING", "OTHER"))
elif phase == "inspect":
    stored = rt.inspect_execution(execution_id)
    print(json.dumps(stored.to_dict() if stored else None))
    raise SystemExit(0)
print(json.dumps(out.to_dict()))
'''


@needs_runtime
class TestProcessDeathResume(unittest.TestCase):
    """Durability against PRODUCTION code, across real process boundaries."""

    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="m41-durable-")
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)
        self.db = os.path.join(self.workdir, "checkpoints.db")
        self.script = Path(self.workdir) / "phase.py"
        self.script.write_text(
            PHASE_SCRIPT.format(root=str(ROOT),
                                tests=str(ROOT / "tests")),
            encoding="utf-8")

    def phase(self, *args) -> dict:
        out = subprocess.run([sys.executable, str(self.script), *args],
                             cwd=str(ROOT), capture_output=True, text=True,
                             shell=False, timeout=240)
        self.assertEqual(out.returncode, 0,
                         f"phase {args} failed:\n{out.stderr[-1500:]}")
        for line in reversed(out.stdout.strip().splitlines()):
            if line.startswith("{") or line == "null":
                return json.loads(line)
        raise AssertionError(f"no JSON from phase {args}: {out.stdout[-800:]}")

    def test_resume_across_process_death(self):
        started = self.phase("start", self.db, "exec-durable")
        self.assertEqual(started["runtime_status"], M.AWAITING_APPROVAL)

        # PROCESS 2 — a genuinely new interpreter reads the durable state.
        seen = self.phase("inspect", self.db, "exec-durable")
        self.assertEqual(seen["runtime_status"], M.AWAITING_APPROVAL)
        self.assertEqual(seen["worker_result"]["output"], "DURABLE_OUT")

        # PROCESS 3 — resumes and completes.
        resumed = self.phase("resume", self.db, "exec-durable",
                             json.dumps({"decision": M.APPROVE}))
        self.assertEqual(resumed["runtime_status"], M.COMPLETED)
        self.assertEqual(resumed["worker_result"]["output"], "DURABLE_OUT")

    def test_rejection_across_process_death(self):
        self.phase("start", self.db, "exec-reject")
        resumed = self.phase("resume", self.db, "exec-reject",
                             json.dumps({"decision": M.REJECT}))
        self.assertEqual(resumed["runtime_status"], M.REJECTED)
        self.assertEqual(resumed["output"], "")

    def test_invalid_resume_across_process_death_fails_closed(self):
        self.phase("start", self.db, "exec-bad")
        from tabularium.engine.runtime.approval import APPROVAL_RESOLUTION_MALFORMED
        resumed = self.phase("resume", self.db, "exec-bad", json.dumps(True))
        self.assertNotEqual(resumed["runtime_status"], M.COMPLETED)
        self.assertIn(APPROVAL_RESOLUTION_MALFORMED, resumed["reason_codes"])
        # Still retryable in yet another process.
        good = self.phase("resume", self.db, "exec-bad",
                          json.dumps({"decision": M.APPROVE}))
        self.assertEqual(good["runtime_status"], M.COMPLETED)

    def test_execution_identity_is_caller_provided_and_stable(self):
        self.phase("start", self.db, "my-own-id")
        seen = self.phase("inspect", self.db, "my-own-id")
        self.assertEqual(seen["execution_id"], "my-own-id")
        self.assertIsNone(self.phase("inspect", self.db, "some-other-id"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
