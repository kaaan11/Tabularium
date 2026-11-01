"""M5.4 — adversarial tests against STATE INTEGRITY boundaries.

Attacks persistence, tools, the runtime and its checkpoint, terminal states,
process-death flows, cross-layer error truth, and security event ordering.

The recurring question: can an attacker cause an unintended DURABLE side
effect, or manufacture authority from state they can influence?

A note on the checkpoint threat model, which these tests make explicit rather
than assume: an attacker able to rewrite `checkpoints.db` already holds
filesystem write access to the operator's machine, and could equally edit
`.ai/` records or the source. The checkpoint is operational state, NOT a
security boundary against a local filesystem attacker. What must hold — and is
tested here — is that such tampering cannot ESCALATE into durable project
truth, because persistence carries its own content-bound authorization that
lives nowhere in the checkpoint.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tabularium.engine.continuation import (ContinuationPlan, ExecutionOutcome,  # noqa: E402
                                  apply_continuation)
from tabularium.engine.continuation import model as C  # noqa: E402
from tabularium.engine.execution import model as P  # noqa: E402
from tabularium.engine.observability import InMemoryCollector  # noqa: E402
from tabularium.engine.observability import events as EV  # noqa: E402
from tabularium.engine.persistence import model as PM  # noqa: E402
from tabularium.engine.persistence import writer as PW  # noqa: E402
from tabularium.engine.runtime import model as RM  # noqa: E402
from tabularium.engine.runtime.approval import HumanApprovalResolution  # noqa: E402
from tests.e2e_fixture import (ACTOR, LATER, WHEN,  # noqa: E402
                                       E2ETestCase, FailingWorker,
                                       RecordingWorker, needs_runtime)
from tests.test_e2e_validation import E2EMixin, tool_constraints  # noqa: E402
from tabularium.engine.tools import adapter as TA  # noqa: E402
from tabularium.engine.tools import model as TM  # noqa: E402
from tabularium.engine.tools.local_fixture import FixtureFileTool, FixtureWriteTool  # noqa: E402


# --- persistence attacks ----------------------------------------------------

class TestPersistenceAttacks(E2ETestCase, E2EMixin):
    def prepared_handoff(self):
        return self.continuation(
            ExecutionOutcome(execution_id="exec-1", task_id="TASK-2026-0001",
                             runtime_status="COMPLETED"),
            ContinuationPlan(handoff=self.handoff_spec()))

    def test_every_tamper_axis_fails_closed(self):
        from dataclasses import replace
        prepared = self.prepared_handoff()
        proposal = prepared[0].proposal
        auth = self.authorize_all(prepared)[proposal.proposal_id]
        target = self.fx.project / ".ai" / "handoffs" / "HND-2026-0002.md"

        tampers = {
            "content": replace(proposal, content=proposal.content + "\nX\n"),
            "target_path": replace(
                proposal, target_path=".ai/reports/HND-2026-0002.md"),
            "operation": replace(proposal, operation=PM.UPDATE),
            "record_id": replace(proposal, record_id="HND-2026-0009"),
            "proposal_id": replace(proposal, proposal_id="other"),
            "execution_id": replace(proposal, execution_id="exec-OTHER"),
            "family": replace(proposal, target_family="Report"),
        }
        for axis, tampered in tampers.items():
            with self.subTest(axis=axis):
                before = self.ai_snapshot()
                result = PW.apply_authorized_proposal(
                    tampered, str(self.fx.project), auth)
                self.assertNotEqual(result.status, PM.WRITTEN,
                                    f"tampered {axis} was written")
                self.assertEqual(self.ai_snapshot(), before,
                                 f"tampered {axis} mutated the project")
        self.assertFalse(target.exists())

    def test_reused_authorization_is_refused(self):
        prepared = self.prepared_handoff()
        proposal = prepared[0].proposal
        auth = self.authorize_all(prepared)[proposal.proposal_id]
        self.assertEqual(PW.apply_authorized_proposal(
            proposal, str(self.fx.project), auth).status, PM.WRITTEN)
        replay = PW.apply_authorized_proposal(
            proposal, str(self.fx.project), auth,
            used_bindings=(proposal.binding(),))
        self.assertNotEqual(replay.status, PM.WRITTEN)
        self.assertIn(PM.AUTHORIZATION_ALREADY_USED, replay.reason_codes)

    def test_create_collision_is_refused(self):
        prepared = self.prepared_handoff()
        proposal = prepared[0].proposal
        auth = self.authorize_all(prepared)[proposal.proposal_id]
        self.assertEqual(PW.apply_authorized_proposal(
            proposal, str(self.fx.project), auth).status, PM.WRITTEN)
        original = (self.fx.project / ".ai" / "handoffs"
                    / "HND-2026-0002.md").read_text(encoding="utf-8")
        again = PW.apply_authorized_proposal(proposal, str(self.fx.project),
                                             auth)
        self.assertIn(PM.TARGET_ALREADY_EXISTS, again.reason_codes)
        self.assertEqual((self.fx.project / ".ai" / "handoffs"
                          / "HND-2026-0002.md").read_text(encoding="utf-8"),
                         original)

    def test_target_escape_is_refused(self):
        for escape in ("../../escape/HND-2026-0009.md",
                       "..\\..\\escape\\HND-2026-0009.md",
                       "/etc/HND-2026-0009.md",
                       ".ai/../../HND-2026-0009.md"):
            with self.subTest(path=escape):
                proposal = PM.PersistenceProposal(
                    proposal_id="p-esc", execution_id="exec-1",
                    target_family="Handoff", operation=PM.CREATE,
                    target_path=escape, content="---\n",
                    record_id="HND-2026-0009", reason="escape",
                    effective_persistence=PM.PROPOSE)
                auth = PM.PersistenceAuthorization(
                    proposal_id="p-esc", execution_id="exec-1",
                    target_path=escape, operation=PM.CREATE,
                    content_digest=proposal.content_digest,
                    authorized_by=ACTOR, authorized_at=WHEN,
                    binding=proposal.binding())
                before = self.ai_snapshot()
                result = PW.apply_authorized_proposal(
                    proposal, str(self.fx.project), auth)
                self.assertNotEqual(result.status, PM.WRITTEN)
                self.assertEqual(self.ai_snapshot(), before)

    def test_structurally_invalid_record_is_refused(self):
        proposal = PM.PersistenceProposal(
            proposal_id="p-bad", execution_id="exec-1",
            target_family="Handoff", operation=PM.CREATE,
            target_path=".ai/handoffs/HND-2026-0007.md",
            content="not a record at all", record_id="HND-2026-0007",
            reason="invalid", effective_persistence=PM.PROPOSE)
        auth = PM.PersistenceAuthorization(
            proposal_id="p-bad", execution_id="exec-1",
            target_path=proposal.target_path, operation=PM.CREATE,
            content_digest=proposal.content_digest, authorized_by=ACTOR,
            authorized_at=WHEN, binding=proposal.binding())
        result = PW.apply_authorized_proposal(proposal, str(self.fx.project),
                                              auth)
        self.assertNotEqual(result.status, PM.WRITTEN)
        self.assertFalse((self.fx.project / ".ai" / "handoffs"
                          / "HND-2026-0007.md").exists())

    def test_denied_write_never_traces_as_written(self):
        collector = InMemoryCollector()
        prepared = self.prepared_handoff()
        apply_continuation(prepared, {}, str(self.fx.project),
                           task_id="TASK-2026-0001", execution_id="exec-1",
                           collector=collector)
        kinds = [e.event_type for e in collector.events]
        self.assertIn(EV.PERSISTENCE_DENIED, kinds)
        self.assertNotIn(EV.PERSISTENCE_WRITTEN, kinds,
                         "a denied write was traced as written")

    def test_writer_failure_leaves_no_partial_state(self):
        prepared = self.prepared_handoff()
        proposal = prepared[0].proposal
        auth = self.authorize_all(prepared)[proposal.proposal_id]
        handoffs = self.fx.project / ".ai" / "handoffs"
        for child in list(handoffs.iterdir()):
            child.unlink()
        handoffs.rmdir()
        result = PW.apply_authorized_proposal(proposal, str(self.fx.project),
                                              auth)
        self.assertNotEqual(result.status, PM.WRITTEN)
        self.assertFalse(handoffs.exists(),
                         "the writer created a record directory implicitly")
        leftovers = list((self.fx.project / ".ai").rglob(".tabu-*"))
        self.assertEqual(leftovers, [])

    def test_partial_continuation_never_claims_success(self):
        prepared = self.continuation(
            ExecutionOutcome(execution_id="exec-1", task_id="TASK-2026-0001",
                             runtime_status="COMPLETED"),
            ContinuationPlan(handoff=self.handoff_spec(),
                             report=self.report_spec()))
        auths = self.authorize_all(prepared)
        del auths[next(p.proposal.proposal_id for p in prepared
                       if p.family == C.REPORT)]
        applied = apply_continuation(prepared, auths, str(self.fx.project),
                                     task_id="TASK-2026-0001")
        self.assertEqual(applied.status, C.PARTIAL)
        self.assertNotIn("RPT-2026-0001", applied.written_record_ids)
        self.assertFalse((self.fx.project / ".ai" / "reports"
                          / "RPT-2026-0001.md").exists())


# --- tool attacks -----------------------------------------------------------

class TestToolAttacks(E2ETestCase, E2EMixin):
    def test_every_tamper_axis_fails_closed(self):
        original = self.write_request()
        auth = self.tool_auth(original)
        tampers = {
            "content": self.write_request(
                arguments={"path": "out.txt", "content": "EVIL"}),
            "path": self.write_request(
                arguments={"path": "other.txt", "content": "tool output"}),
            "operation": self.write_request(operation="read_text"),
            "execution_id": self.write_request(execution_id="exec-OTHER"),
            "request_id": self.write_request(tool_request_id="tw-OTHER"),
            "scope": self.write_request(
                requested_scope={"root": str(self.workdir)}),
            "tool_name": self.write_request(tool_name="fixture_file"),
            "tool_class": self.write_request(tool_class=TM.READ),
        }
        for axis, tampered in tampers.items():
            with self.subTest(axis=axis):
                result = TA.run_tool_cycle(
                    tampered, FixtureWriteTool(self.tool_root),
                    tool_constraints("WRITE", "READ"), auth)
                self.assertNotEqual(result.status, TM.EXECUTED,
                                    f"tampered {axis} executed")
        for name in ("out.txt", "other.txt"):
            self.assertFalse((self.tool_root / name).exists())

    def test_path_traversal_and_escapes_fail_closed(self):
        import os
        escapes = ["../escape.txt", "../../etc/passwd",
                   str(Path(self.workdir) / "outside.txt"),
                   "sub/../../escape.txt"]
        for path in escapes:
            with self.subTest(path=path):
                request = self.tool_request(arguments={"path": path})
                result = TA.run_tool_cycle(
                    request, FixtureFileTool(self.tool_root),
                    tool_constraints("READ"))
                self.assertEqual(result.status, TM.DENIED)
                self.assertIn(TM.TOOL_SCOPE_VIOLATION, result.reason_codes)

        # Windows-style separators are platform-specific: on Windows they are
        # path separators and MUST be scope-refused like any other escape; on
        # POSIX a backslash is an ordinary filename character, so the path
        # stays inside the granted root and fails closed in the adapter
        # (verified: FAILED / TOOL_ADAPTER_FAILED, nothing read, nothing
        # created outside the root). Either way the tool never executes.
        windows_escape = "..\\..\\windows\\system32\\config"
        with self.subTest(path=windows_escape, platform=os.name):
            request = self.tool_request(arguments={"path": windows_escape})
            result = TA.run_tool_cycle(
                request, FixtureFileTool(self.tool_root),
                tool_constraints("READ"))
            if os.name == "nt":
                self.assertEqual(result.status, TM.DENIED)
                self.assertIn(TM.TOOL_SCOPE_VIOLATION,
                              result.reason_codes)
            else:
                self.assertNotEqual(result.status, TM.EXECUTED)
                self.assertEqual(result.status, TM.FAILED)

    def test_wildcard_scope_is_refused(self):
        for root in (str(self.tool_root) + "/*", str(self.tool_root) + "/?",
                     str(self.tool_root) + "/[a-z]"):
            with self.subTest(root=root):
                request = self.tool_request(requested_scope={"root": root})
                result = TA.run_tool_cycle(
                    request, FixtureFileTool(self.tool_root),
                    tool_constraints("READ"))
                self.assertEqual(result.status, TM.DENIED)

    def test_tool_cannot_target_the_project_ai_directory(self):
        request = self.write_request(
            requested_scope={"root": str(self.fx.project / ".ai")},
            arguments={"path": "handoffs/HND-2026-0099.md",
                       "content": "---\n"})
        before = self.ai_snapshot()
        result = TA.run_tool_cycle(
            request, FixtureWriteTool(self.tool_root),
            tool_constraints("WRITE"), self.tool_auth(request))
        # The adapter is confined to its own root regardless of the request.
        self.assertNotEqual(result.status, TM.EXECUTED)
        self.assertEqual(self.ai_snapshot(), before)

    def test_replay_within_a_process_is_refused(self):
        request = self.write_request()
        auth = self.tool_auth(request)
        tool = FixtureWriteTool(self.tool_root)
        self.assertEqual(TA.run_tool_cycle(
            request, tool, tool_constraints("WRITE"), auth).status,
            TM.EXECUTED)
        replay = TA.run_tool_cycle(request, tool, tool_constraints("WRITE"),
                                   auth, used_bindings=(request.binding(),))
        self.assertEqual(replay.status, TM.DENIED)
        self.assertIn(TM.TOOL_AUTHORIZATION_ALREADY_USED, replay.reason_codes)

    def test_unknown_tool_and_class_are_refused(self):
        for label, request in (
                ("unknown_tool", self.tool_request(tool_name="nope")),
                ("unknown_class", self.tool_request(tool_class="SUPERUSER"))):
            with self.subTest(case=label):
                result = TA.run_tool_cycle(
                    request, FixtureFileTool(self.tool_root),
                    tool_constraints("READ", "SUPERUSER"))
                self.assertEqual(result.status, TM.DENIED)

    def test_malformed_authorizations_fail_closed(self):
        request = self.write_request()
        for bogus in (None, True, "APPROVE", 1, [], {}, object(),
                      {"authorized_by": ACTOR}, {"binding": "x"}):
            with self.subTest(value=repr(bogus)[:24]):
                result = TA.run_tool_cycle(
                    request, FixtureWriteTool(self.tool_root),
                    tool_constraints("WRITE"), bogus)
                self.assertNotEqual(result.status, TM.EXECUTED)
        self.assertFalse((self.tool_root / "out.txt").exists())


# --- durable tool replay boundary (re-evaluated for M5) ---------------------

class TestToolReplayBoundaryUnderM5(E2ETestCase, E2EMixin):
    """Does M5 instrumentation make tool state cross a process?

    M4.7 concluded the deferred durable replay store stays deferred BECAUSE no
    tool object is ever checkpointed. M5 added tracing and export, so that
    conclusion must be re-tested rather than assumed.
    """

    def test_no_m5_module_makes_tool_state_crossable(self):
        import ast
        for package in ("runtime", "execution", "continuation", "persistence",
                        "observability"):
            for module in (ROOT / "tabularium" / "engine" / package).glob("*.py"):
                tree = ast.parse(module.read_text(encoding="utf-8"))
                names = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        names.add(node.module or "")
                    elif isinstance(node, ast.Import):
                        names.update(a.name for a in node.names)
                self.assertFalse(
                    any("tools" in n.split(".") for n in names),
                    f"{package}/{module.name} reached the tool boundary")

    def test_trace_does_not_carry_a_replayable_authorization(self):
        """A trace records that a tool ran; it must not carry the binding."""
        collector = InMemoryCollector()
        request = self.write_request()
        TA.run_tool_cycle(request, FixtureWriteTool(self.tool_root),
                          tool_constraints("WRITE"), self.tool_auth(request),
                          collector=collector)
        blob = json.dumps([e.to_dict() for e in collector.events])
        self.assertNotIn(request.binding(), blob,
                         "the trace leaked a reusable tool binding")
        self.assertNotIn(request.arguments_digest, blob)
        self.assertNotIn("authorized_by", blob)


# --- runtime and checkpoint -------------------------------------------------

@needs_runtime
class TestRuntimeAttacks(E2ETestCase, E2EMixin):
    def suspend(self):
        result = self.start(skill_path=self.skill(approval_required=True))
        self.assertEqual(result.status, P.AWAITING_APPROVAL)
        from tabularium.engine.execution import inspect_execution
        return inspect_execution("exec-1", self.db).approval_request

    def test_wrong_identity_approvals_are_refused(self):
        pending = self.suspend()
        from tabularium.engine.execution import inspect_execution, resume_execution
        cases = {
            "wrong_request": {"approval_request_id": "exec-1:approval-99"},
            "wrong_execution": {"execution_id": "exec-OTHER"},
            "empty_actor": {"decided_by": ""},
            "naive_timestamp": {"decided_at": "2026-08-18 12:00"},
            "bad_decision": {"decision": "MAYBE"},
        }
        for label, override in cases.items():
            with self.subTest(case=label):
                base = dict(approval_request_id=pending["request_id"],
                            execution_id="exec-1", decision="APPROVE",
                            decided_by=ACTOR, decided_at=WHEN)
                base.update(override)
                result = resume_execution("exec-1", self.db,
                                          HumanApprovalResolution(**base),
                                          adapter=RecordingWorker(),
                                          task_id="TASK-2026-0001")
                self.assertNotEqual(result.status, P.COMPLETED)
        self.assertNotEqual(
            inspect_execution("exec-1", self.db).runtime_status, RM.COMPLETED)

    def test_malformed_resume_values_fail_closed(self):
        self.suspend()
        from tabularium.engine.execution import inspect_execution, resume_execution
        for bogus in (None, True, False, 0, "APPROVE", [], {}, object(),
                      {"decision": "APPROVE"}):
            with self.subTest(value=repr(bogus)[:24]):
                worker = RecordingWorker()
                result = resume_execution("exec-1", self.db, bogus,
                                          adapter=worker,
                                          task_id="TASK-2026-0001")
                self.assertNotEqual(result.status, P.COMPLETED)
                self.assertEqual(worker.call_count, 0)

    def test_checkpoint_inside_the_project_is_refused(self):
        inside = str(self.fx.project / ".ai" / "checkpoints.db")
        result = self.start(checkpoint_path=inside, skill_path=self.skill())
        self.assertEqual(result.status, P.PREPARATION_REJECTED)
        self.assertFalse(os.path.exists(inside))

    def test_unwritable_checkpoint_does_not_fall_back(self):
        blocked = os.path.join(self.workdir, "blocked")
        os.makedirs(blocked, exist_ok=True)
        before = self.ai_snapshot()
        result = self.start(checkpoint_path=blocked,
                            skill_path=self.skill())
        self.assertNotEqual(result.status, P.COMPLETED)
        self.assertEqual(self.ai_snapshot(), before)
        self.assertEqual(list((self.fx.project / ".ai").rglob("*.db")), [])

    def test_missing_checkpoint_cannot_resume(self):
        self.suspend()
        os.remove(self.db)
        from tabularium.engine.execution import resume_execution
        result = resume_execution("exec-1", self.db, HumanApprovalResolution(
            approval_request_id="exec-1:approval-1", execution_id="exec-1",
            decision="APPROVE", decided_by=ACTOR, decided_at=WHEN),
            adapter=RecordingWorker(), task_id="TASK-2026-0001")
        self.assertNotEqual(result.status, P.COMPLETED)


@needs_runtime
class TestCheckpointTampering(E2ETestCase, E2EMixin):
    """Bounded tampering. The property that must hold is NON-ESCALATION."""

    def tamper(self, find: bytes, replace: bytes) -> int:
        con = sqlite3.connect(self.db)
        changed = 0
        for rowid, blob in con.execute(
                "SELECT rowid, checkpoint FROM checkpoints").fetchall():
            if blob and find in blob:
                new = blob.replace(find, replace)
                if new != blob:
                    con.execute(
                        "UPDATE checkpoints SET checkpoint=? WHERE rowid=?",
                        (new, rowid))
                    changed += 1
        con.commit()
        con.close()
        return changed

    def test_flipping_approval_required_does_not_bypass_approval(self):
        self.start(skill_path=self.skill(approval_required=True))
        self.tamper(b"\xb1approval_required\xc3", b"\xb1approval_required\xc2")
        from tabularium.engine.execution import resume_execution
        worker = RecordingWorker()
        result = resume_execution("exec-1", self.db, None, adapter=worker,
                                  task_id="TASK-2026-0001")
        self.assertNotEqual(result.status, P.COMPLETED,
                            "tampering bypassed the approval boundary")
        self.assertEqual(worker.call_count, 0)

    def test_tampering_cannot_produce_a_durable_write(self):
        """The escalation that would matter: checkpoint -> project truth."""
        self.start(skill_path=self.skill(approval_required=True))
        before = self.ai_snapshot()
        self.tamper(b"AWAITING_APPROVAL", b"COMPLETED\x00\x00\x00\x00\x00\x00\x00\x00")
        self.tamper(b"\xb1approval_required\xc3", b"\xb1approval_required\xc2")
        from tabularium.engine.execution import inspect_execution
        inspect_execution("exec-1", self.db)
        self.assertEqual(self.ai_snapshot(), before,
                         "checkpoint tampering mutated project truth")
        self.assertIn("status: IN_PROGRESS", self.task_text())

    def test_tampering_cannot_authorize_persistence(self):
        """Persistence authority lives nowhere in the checkpoint."""
        self.start(skill_path=self.skill(approval_required=True))
        self.tamper(b"AWAITING_APPROVAL",
                    b"COMPLETED\x00\x00\x00\x00\x00\x00\x00\x00")
        prepared = self.continuation(
            ExecutionOutcome(execution_id="exec-1", task_id="TASK-2026-0001",
                             runtime_status="COMPLETED"),
            ContinuationPlan(handoff=self.handoff_spec()))
        applied = apply_continuation(prepared, {}, str(self.fx.project),
                                     task_id="TASK-2026-0001")
        self.assertNotEqual(applied.status, C.COMPLETE)
        self.assertFalse((self.fx.project / ".ai" / "handoffs"
                          / "HND-2026-0002.md").exists())

    def test_unreadable_checkpoint_fails_controlled(self):
        self.start(skill_path=self.skill(approval_required=True))
        with open(self.db, "wb") as handle:
            handle.write(b"this is not a sqlite database")
        from tabularium.engine.execution import inspect_execution, resume_execution
        self.assertIsNone(inspect_execution("exec-1", self.db))
        result = resume_execution("exec-1", self.db, HumanApprovalResolution(
            approval_request_id="exec-1:approval-1", execution_id="exec-1",
            decision="APPROVE", decided_by=ACTOR, decided_at=WHEN),
            adapter=RecordingWorker(), task_id="TASK-2026-0001")
        self.assertNotEqual(result.status, P.COMPLETED)


# --- terminal states --------------------------------------------------------

@needs_runtime
class TestTerminalStateAttacks(E2ETestCase, E2EMixin):
    def test_completed_cannot_be_resurrected(self):
        self.start(skill_path=self.skill())
        from tabularium.engine.execution import inspect_execution, resume_execution
        worker = RecordingWorker()
        resume_execution("exec-1", self.db, HumanApprovalResolution(
            approval_request_id="exec-1:approval-1", execution_id="exec-1",
            decision="APPROVE", decided_by=ACTOR, decided_at=LATER),
            adapter=worker, task_id="TASK-2026-0001")
        self.assertEqual(worker.call_count, 0)
        self.assertEqual(inspect_execution("exec-1", self.db).runtime_status,
                         RM.COMPLETED)

    def test_rejected_cannot_be_approved_afterwards(self):
        self.start(skill_path=self.skill(approval_required=True))
        from tabularium.engine.execution import inspect_execution, resume_execution
        pending = inspect_execution("exec-1", self.db).approval_request
        resume_execution("exec-1", self.db, HumanApprovalResolution(
            approval_request_id=pending["request_id"], execution_id="exec-1",
            decision="REJECT", decided_by=ACTOR, decided_at=WHEN),
            adapter=RecordingWorker(), task_id="TASK-2026-0001")
        worker = RecordingWorker()
        resume_execution("exec-1", self.db, HumanApprovalResolution(
            approval_request_id=pending["request_id"], execution_id="exec-1",
            decision="APPROVE", decided_by=ACTOR, decided_at=LATER),
            adapter=worker, task_id="TASK-2026-0001")
        self.assertEqual(worker.call_count, 0)
        self.assertEqual(inspect_execution("exec-1", self.db).runtime_status,
                         RM.REJECTED)

    def test_failed_execution_surfaces_no_output_and_writes_nothing(self):
        before = self.ai_snapshot()
        result = self.start(adapter=FailingWorker(), skill_path=self.skill())
        self.assertNotEqual(result.status, P.COMPLETED)
        self.assertEqual(result.output, "")
        self.assertEqual(self.ai_snapshot(), before)

    def test_persistence_with_a_mismatched_execution_identity_is_refused(self):
        prepared = self.continuation(
            ExecutionOutcome(execution_id="exec-1", task_id="TASK-2026-0001",
                             runtime_status="COMPLETED"),
            ContinuationPlan(handoff=self.handoff_spec()))
        proposal = prepared[0].proposal
        mismatched = PM.PersistenceAuthorization(
            proposal_id=proposal.proposal_id, execution_id="exec-UNRELATED",
            target_path=proposal.target_path, operation=proposal.operation,
            content_digest=proposal.content_digest, authorized_by=ACTOR,
            authorized_at=WHEN, binding=proposal.binding())
        result = PW.apply_authorized_proposal(proposal, str(self.fx.project),
                                              mismatched)
        self.assertNotEqual(result.status, PM.WRITTEN)


# --- process-death adversarial flows ---------------------------------------

@needs_runtime
class TestProcessDeathAttacks(E2ETestCase, E2EMixin):
    def resume_in_subprocess(self, resolution_kwargs: dict) -> str:
        script = f"""
import sys, json
sys.path.insert(0, {str(ROOT)!r})
from tabularium.engine.execution import resume_execution
from tabularium.engine.runtime.approval import HumanApprovalResolution
from tests.e2e_fixture import RecordingWorker
worker = RecordingWorker()
result = resume_execution("exec-1", {self.db!r},
    HumanApprovalResolution(**{resolution_kwargs!r}),
    adapter=worker, task_id="TASK-2026-0001")
print(json.dumps({{"status": result.status, "calls": worker.call_count}}))
"""
        proc = subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_stale_approval_after_process_death_is_refused(self):
        self.start(skill_path=self.skill(approval_required=True))
        payload = self.resume_in_subprocess({
            "approval_request_id": "exec-1:approval-99",
            "execution_id": "exec-1", "decision": "APPROVE",
            "decided_by": ACTOR, "decided_at": WHEN})
        self.assertNotEqual(payload["status"], P.COMPLETED)
        self.assertEqual(payload["calls"], 0)

    def test_replayed_resolution_after_process_death_runs_nothing_twice(self):
        self.start(skill_path=self.skill(approval_required=True))
        from tabularium.engine.execution import inspect_execution
        pending = inspect_execution("exec-1", self.db).approval_request
        good = {"approval_request_id": pending["request_id"],
                "execution_id": "exec-1", "decision": "APPROVE",
                "decided_by": ACTOR, "decided_at": WHEN}
        first = self.resume_in_subprocess(good)
        self.assertEqual(first["status"], P.COMPLETED)
        second = self.resume_in_subprocess(good)
        self.assertEqual(second["calls"], 0,
                         "a replayed resolution re-ran the worker")

    def test_second_resume_after_completion_runs_nothing(self):
        self.start(skill_path=self.skill())
        payload = self.resume_in_subprocess({
            "approval_request_id": "exec-1:approval-1",
            "execution_id": "exec-1", "decision": "APPROVE",
            "decided_by": ACTOR, "decided_at": LATER})
        self.assertEqual(payload["calls"], 0)

    def test_persistence_replay_after_process_death_is_refused(self):
        prepared = self.continuation(
            ExecutionOutcome(execution_id="exec-1", task_id="TASK-2026-0001",
                             runtime_status="COMPLETED"),
            ContinuationPlan(handoff=self.handoff_spec()))
        proposal = prepared[0].proposal
        auth = self.authorize_all(prepared)[proposal.proposal_id]
        self.assertEqual(PW.apply_authorized_proposal(
            proposal, str(self.fx.project), auth).status, PM.WRITTEN)
        original = (self.fx.project / ".ai" / "handoffs"
                    / "HND-2026-0002.md").read_text(encoding="utf-8")

        # A fresh process replaying the same authorization: the durable state
        # itself refuses, not merely in-process memory.
        script = f"""
import sys, json
sys.path.insert(0, {str(ROOT)!r})
from tabularium.engine.persistence import model as PM, writer as PW
proposal = PM.PersistenceProposal(
    proposal_id={proposal.proposal_id!r}, execution_id="exec-1",
    target_family="Handoff", operation=PM.CREATE,
    target_path={proposal.target_path!r}, content={proposal.content!r},
    record_id={proposal.record_id!r}, reason="replay",
    effective_persistence=PM.PROPOSE)
auth = PM.PersistenceAuthorization(
    proposal_id={proposal.proposal_id!r}, execution_id="exec-1",
    target_path={proposal.target_path!r}, operation=PM.CREATE,
    content_digest=proposal.content_digest, authorized_by={ACTOR!r},
    authorized_at={WHEN!r}, binding=proposal.binding())
r = PW.apply_authorized_proposal(proposal, {str(self.fx.project)!r}, auth)
print(json.dumps({{"status": r.status, "codes": list(r.reason_codes)}}))
"""
        proc = subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertNotEqual(payload["status"], PM.WRITTEN)
        self.assertEqual((self.fx.project / ".ai" / "handoffs"
                          / "HND-2026-0002.md").read_text(encoding="utf-8"),
                         original)


# --- cross-layer truth and event ordering ----------------------------------

@needs_runtime
class TestCrossLayerTruth(E2ETestCase, E2EMixin):
    def test_failed_execution_is_not_reported_as_success_anywhere(self):
        collector = InMemoryCollector()
        result = self.start(adapter=FailingWorker(), skill_path=self.skill(),
                            collector=collector)
        self.assertNotEqual(result.status, P.COMPLETED)
        kinds = [e.event_type for e in collector.trace("exec-1").ordered]
        self.assertNotIn(EV.EXECUTION_COMPLETED, kinds,
                         "a failed execution traced as completed")
        self.assertIn(EV.WORKER_FAILED, kinds)

        try:
            from opentelemetry.trace import StatusCode

            from tabularium.engine.observability.otel import (export_trace,
                                                    in_memory_provider)
        except Exception:  # noqa: BLE001
            self.skipTest("OpenTelemetry not installed")
        provider, exporter = in_memory_provider()
        export = export_trace(collector.trace("exec-1"), provider)
        self.assertTrue(export.healthy, "the export itself worked")
        root = next(s for s in exporter.get_finished_spans()
                    if s.name.startswith("execution "))
        self.assertNotEqual(root.status.status_code, StatusCode.OK,
                            "a healthy export reported a failure as OK")

    def test_denied_persistence_is_consistent_across_layers(self):
        collector = InMemoryCollector()
        prepared = self.continuation(
            ExecutionOutcome(execution_id="exec-1", task_id="TASK-2026-0001",
                             runtime_status="COMPLETED"),
            ContinuationPlan(handoff=self.handoff_spec()))
        applied = apply_continuation(prepared, {}, str(self.fx.project),
                                     task_id="TASK-2026-0001",
                                     execution_id="exec-1",
                                     collector=collector)
        self.assertNotEqual(applied.status, C.COMPLETE)
        kinds = [e.event_type for e in collector.events]
        self.assertNotIn(EV.PERSISTENCE_WRITTEN, kinds)
        self.assertFalse((self.fx.project / ".ai" / "handoffs"
                          / "HND-2026-0002.md").exists())

    def test_security_event_ordering_is_truthful(self):
        collector = InMemoryCollector()
        # Approval boundary.
        self.start(skill_path=self.skill(approval_required=True),
                   collector=collector)
        kinds = [e.event_type for e in collector.trace("exec-1").ordered]
        self.assertIn(EV.APPROVAL_REQUESTED, kinds)
        self.assertNotIn(EV.EXECUTION_COMPLETED, kinds,
                         "completion was traced before approval")

        # Tool authorization boundary.
        request = self.write_request()
        TA.run_tool_cycle(request, FixtureWriteTool(self.tool_root),
                          tool_constraints("WRITE"), collector=collector)
        TA.run_tool_cycle(request, FixtureWriteTool(self.tool_root),
                          tool_constraints("WRITE"), self.tool_auth(request),
                          collector=collector)
        ordered = [e.event_type for e in collector.trace("exec-1").ordered]
        self.assertLess(ordered.index(EV.TOOL_AUTHORIZATION_REQUIRED),
                        ordered.index(EV.TOOL_EXECUTED),
                        "TOOL_EXECUTED preceded its authorization requirement")

    def test_persistence_authorized_precedes_written(self):
        collector = InMemoryCollector()
        prepared = self.continuation(
            ExecutionOutcome(execution_id="exec-1", task_id="TASK-2026-0001",
                             runtime_status="COMPLETED"),
            ContinuationPlan(handoff=self.handoff_spec()))
        apply_continuation(prepared, self.authorize_all(prepared),
                           str(self.fx.project), task_id="TASK-2026-0001",
                           execution_id="exec-1", collector=collector)
        ordered = [e.event_type for e in collector.trace("exec-1").ordered]
        self.assertLess(ordered.index(EV.PERSISTENCE_PROPOSED),
                        ordered.index(EV.PERSISTENCE_AUTHORIZED))
        self.assertLess(ordered.index(EV.PERSISTENCE_AUTHORIZED),
                        ordered.index(EV.PERSISTENCE_WRITTEN))


# --- telemetry failure cannot alter execution ------------------------------

@needs_runtime
class TestTelemetryFailure(E2ETestCase, E2EMixin):
    def hostile_collectors(self):
        class ExplodingEmit(InMemoryCollector):
            def emit(self, event):
                raise RuntimeError("collector exploded")

        class ExplodingSequence(InMemoryCollector):
            def next_sequence(self, execution_id):
                raise RuntimeError("sequence exploded")

        class NotACollector:
            pass

        return {"emit": ExplodingEmit(), "sequence": ExplodingSequence(),
                "bogus": NotACollector(), "none": None, "string": "collector"}

    def test_collector_failure_never_changes_execution(self):
        baseline = self.start(execution_id="base", skill_path=self.skill())
        for label, collector in self.hostile_collectors().items():
            with self.subTest(collector=label):
                result = self.start(execution_id=f"exec-{label}",
                                    skill_path=self.skill(),
                                    collector=collector)
                self.assertEqual(result.status, baseline.status)
                self.assertEqual(result.output, baseline.output)
                self.assertEqual(result.metrics.to_dict(),
                                 baseline.metrics.to_dict())

    def test_collector_failure_never_changes_tool_or_persistence(self):
        for label, collector in self.hostile_collectors().items():
            with self.subTest(collector=label):
                request = self.write_request(
                    tool_request_id=f"tw-{label}",
                    arguments={"path": f"{label}.txt", "content": "x"})
                unauthorized = TA.run_tool_cycle(
                    request, FixtureWriteTool(self.tool_root),
                    tool_constraints("WRITE"), collector=collector)
                self.assertEqual(unauthorized.status,
                                 TM.AUTHORIZATION_REQUIRED)
                self.assertFalse((self.tool_root / f"{label}.txt").exists())

    def test_jsonl_failure_is_recorded_not_raised(self):
        from tabularium.engine.observability import JsonlCollector
        # A directory where the file should be: every append fails.
        bad = Path(self.workdir) / "trace_dir"
        bad.mkdir()
        collector = JsonlCollector(bad, project_root=self.fx.project)
        result = self.start(skill_path=self.skill(), collector=collector)
        self.assertEqual(result.status, P.COMPLETED)
        self.assertFalse(collector.healthy)
        self.assertTrue(collector.failures)

    def test_telemetry_failure_creates_no_authorization(self):
        """A failed collector must not make anything succeed."""
        before = self.ai_snapshot()
        prepared = self.continuation(
            ExecutionOutcome(execution_id="exec-1", task_id="TASK-2026-0001",
                             runtime_status="COMPLETED"),
            ContinuationPlan(handoff=self.handoff_spec()))

        class ExplodingEmit(InMemoryCollector):
            def emit(self, event):
                raise RuntimeError("boom")

        applied = apply_continuation(prepared, {}, str(self.fx.project),
                                     task_id="TASK-2026-0001",
                                     collector=ExplodingEmit())
        self.assertNotEqual(applied.status, C.COMPLETE)
        self.assertEqual(self.ai_snapshot(), before)


if __name__ == "__main__":
    unittest.main()
