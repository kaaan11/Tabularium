"""M4.5 — durable continuation flow.

Covers Handoff and Report construction, explicit Task update, persistence-gate
reuse, authority boundaries, partial multi-proposal results, next-session
continuity in a FRESH interpreter, token/context economy, and canonical-project
safety. Every mutation test runs against a throwaway fixture; the canonical
`.ai/` tree is snapshotted before and after each test.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tabularium.engine.context_loader import load_context  # noqa: E402
from tabularium.engine.context_loader.validate import validate_consumed  # noqa: E402
from tabularium.engine.continuation import (ContinuationPlan, ExecutionOutcome,  # noqa: E402
                                  HandoffSpec, ReportSpec, TaskUpdateSpec,
                                  apply_continuation, build_proposals, model as C)
from tabularium.engine.persistence import model as P  # noqa: E402
from tests.test_context_loader import Fixture  # noqa: E402

WHEN = "2026-08-18T12:00:00Z"
AUTH_WHEN = "2026-08-18T12:05:00Z"
ACTOR = "human:user"

import tabularium.engine.continuation.model as CM  # noqa: E402


TASK_COMPLETED = ('---\n'
                  'schema_version: "1.10"\n'
                  'id: TASK-2026-0001\n'
                  'title: Root\n'
                  'objective: Do it\n'
                  'status: COMPLETED\n'
                  'created_at: "2026-01-01T00:00:00Z"\n'
                  'updated_at: "2026-08-18T12:00:00Z"\n'
                  'created_by: USER\n'
                  '---\n'
                  '# Root\n\nBody.\n')


class ContinuationTestCase(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)
        self.fx.task("TASK-2026-0001")
        for name in P.FAMILY_DIRECTORY.values():
            (self.fx.project / ".ai" / name).mkdir(parents=True, exist_ok=True)
        self.canonical_before = self.canonical_snapshot()
        self.addCleanup(self.assert_canonical_untouched)

    # -- helpers ------------------------------------------------------------

    def outcome(self, **kw) -> ExecutionOutcome:
        base = dict(execution_id="exec-1", task_id="TASK-2026-0001",
                    worker_id="w-cli", worker_category="CLI_CODING",
                    runtime_status="COMPLETED", output="did the work")
        base.update(kw)
        return ExecutionOutcome(**base)

    def handoff_spec(self, **kw) -> HandoffSpec:
        base = dict(title="Handover before migration", created_by=ACTOR,
                    created_at=WHEN, next_step="Run the migration",
                    what_attempted="wired the gate",
                    what_completed="tests green", what_remains="docs")
        base.update(kw)
        return HandoffSpec(**base)

    def report_spec(self, **kw) -> ReportSpec:
        base = dict(title="Execution outcome", created_by="ai:claude",
                    created_at=WHEN, outcome="The gate works",
                    validation="tests pass", limitations="single-record only")
        base.update(kw)
        return ReportSpec(**base)

    def authorize(self, prepared, **overrides) -> dict:
        auths = {}
        for item in prepared:
            if not item.ok:
                continue
            p = item.proposal
            base = dict(proposal_id=p.proposal_id, execution_id=p.execution_id,
                        target_path=p.target_path, operation=p.operation,
                        content_digest=p.content_digest, authorized_by=ACTOR,
                        authorized_at=AUTH_WHEN, binding=p.binding())
            base.update(overrides)
            auths[p.proposal_id] = P.PersistenceAuthorization(**base)
        return auths

    def run_plan(self, plan, **auth_overrides):
        prepared = build_proposals(self.outcome(), plan, str(self.fx.project))
        auths = self.authorize(prepared, **auth_overrides)
        return prepared, apply_continuation(prepared, auths,
                                            str(self.fx.project),
                                            task_id="TASK-2026-0001",
                                            execution_id="exec-1")

    def canonical_snapshot(self) -> dict:
        d = {}
        for path in sorted((ROOT / ".ai").rglob("*")):
            if path.is_file():
                d[str(path.relative_to(ROOT))] = hashlib.sha256(
                    path.read_bytes()).hexdigest()
        return d

    def assert_canonical_untouched(self):
        self.assertEqual(self.canonical_snapshot(), self.canonical_before,
                         "the canonical Tabularium project was modified")

    def project_files(self) -> dict:
        d = {}
        for path in sorted((self.fx.project / ".ai").rglob("*")):
            if path.is_file():
                d[path.relative_to(self.fx.project).as_posix()] = \
                    hashlib.sha256(path.read_bytes()).hexdigest()
        return d


# --- Handoff ----------------------------------------------------------------

class TestHandoff(ContinuationTestCase):
    def test_valid_handoff_proposal_written(self):
        _, result = self.run_plan(ContinuationPlan(handoff=self.handoff_spec()))
        self.assertEqual(result.status, C.COMPLETE)
        self.assertIn("HND-2026-0001", result.written_record_ids)

    def test_handoff_validates_under_frozen_validator(self):
        prepared = build_proposals(self.outcome(),
                                   ContinuationPlan(handoff=self.handoff_spec()),
                                   str(self.fx.project))
        item = prepared[0]
        import yaml
        _, front, body = item.proposal.content.split("---", 2)
        self.assertEqual(
            validate_consumed("Handoff", yaml.safe_load(front), body), [])

    def test_handoff_backlinks_the_exact_task(self):
        prepared = build_proposals(self.outcome(),
                                   ContinuationPlan(handoff=self.handoff_spec()),
                                   str(self.fx.project))
        self.assertIn("task: \"TASK-2026-0001\"", prepared[0].proposal.content)

    def test_handoff_has_required_body_sections(self):
        prepared = build_proposals(self.outcome(),
                                   ContinuationPlan(handoff=self.handoff_spec()),
                                   str(self.fx.project))
        content = prepared[0].proposal.content
        self.assertIn("## Current State", content)
        self.assertIn("## Continuation", content)

    def test_handoff_next_step_required(self):
        prepared = build_proposals(
            self.outcome(),
            ContinuationPlan(handoff=self.handoff_spec(next_step="")),
            str(self.fx.project))
        self.assertFalse(prepared[0].ok)
        self.assertEqual(prepared[0].reason_code, C.HANDOFF_CONTENT_REQUIRED)

    def test_handoff_content_bounded(self):
        huge = "x" * (CM.MAX_HANDOFF_BODY_CHARS + 10)
        prepared = build_proposals(
            self.outcome(),
            ContinuationPlan(handoff=self.handoff_spec(what_completed=huge)),
            str(self.fx.project))
        self.assertFalse(prepared[0].ok)
        self.assertEqual(prepared[0].reason_code, C.HANDOFF_CONTENT_REQUIRED)

    def test_handoff_does_not_dump_worker_output(self):
        secret_marker = "RAW-WORKER-DUMP-MARKER"
        prepared = build_proposals(
            self.outcome(output=secret_marker),
            ContinuationPlan(handoff=self.handoff_spec()),
            str(self.fx.project))
        self.assertNotIn(secret_marker, prepared[0].proposal.content)

    def test_next_step_survives_new_session(self):
        _, result = self.run_plan(ContinuationPlan(handoff=self.handoff_spec()))
        self.assertEqual(result.status, C.COMPLETE)
        written = (self.fx.project / ".ai" / "handoffs"
                   / "HND-2026-0001.md").read_text(encoding="utf-8")
        self.assertIn("Next step: Run the migration", written)


# --- Report -----------------------------------------------------------------

class TestReport(ContinuationTestCase):
    def test_valid_report_proposal_written(self):
        _, result = self.run_plan(ContinuationPlan(report=self.report_spec()))
        self.assertEqual(result.status, C.COMPLETE)
        self.assertIn("RPT-2026-0001", result.written_record_ids)

    def test_report_validates_under_frozen_validator(self):
        prepared = build_proposals(self.outcome(),
                                   ContinuationPlan(report=self.report_spec()),
                                   str(self.fx.project))
        import yaml
        _, front, body = prepared[0].proposal.content.split("---", 2)
        self.assertEqual(
            validate_consumed("Report", yaml.safe_load(front), body), [])

    def test_report_distinct_from_handoff(self):
        prepared = build_proposals(
            self.outcome(),
            ContinuationPlan(handoff=self.handoff_spec(),
                             report=self.report_spec()),
            str(self.fx.project))
        handoff = next(p for p in prepared if p.family == C.HANDOFF).proposal.content
        report = next(p for p in prepared if p.family == C.REPORT).proposal.content
        # Report uses result-oriented sections; Handoff uses continuation ones.
        self.assertIn("## Outcome", report)
        self.assertNotIn("## Continuation", report)
        self.assertIn("## Continuation", handoff)
        self.assertNotIn("## Outcome", handoff)

    def test_report_outcome_required(self):
        prepared = build_proposals(
            self.outcome(),
            ContinuationPlan(report=self.report_spec(outcome="")),
            str(self.fx.project))
        self.assertFalse(prepared[0].ok)
        self.assertEqual(prepared[0].reason_code, C.REPORT_CONTENT_REQUIRED)


# --- Task state safety ------------------------------------------------------

class TestTaskState(ContinuationTestCase):
    def test_worker_completion_alone_does_not_mutate_task(self):
        # A COMPLETED runtime status with NO task_update in the plan must never
        # change the Task. Structural: there is no field to infer it from.
        before = self.project_files()
        self.run_plan(ContinuationPlan(handoff=self.handoff_spec()))
        after = self.project_files()
        task_key = ".ai/tasks/TASK-2026-0001.md"
        self.assertEqual(before[task_key], after[task_key],
                         "the Task file changed despite no task_update")
        after_task = (self.fx.project / ".ai" / "tasks"
                      / "TASK-2026-0001.md").read_text(encoding="utf-8")
        self.assertIn("status: IN_PROGRESS", after_task)

    def test_no_plan_field_can_request_task_completion_from_output(self):
        # The plan has no way to say "complete the task because output says so".
        self.assertFalse(hasattr(ContinuationPlan(), "complete_if_completed"))
        # task_update is the only path and it demands full caller content.
        spec = TaskUpdateSpec(new_content="", expected_current_digest="d")
        prepared = build_proposals(self.outcome(),
                                   ContinuationPlan(task_update=spec),
                                   str(self.fx.project))
        self.assertFalse(prepared[0].ok)
        self.assertEqual(prepared[0].reason_code, C.TASK_UPDATE_INVALID)

    def test_explicit_authorized_task_update_applies(self):
        current = (self.fx.project / ".ai" / "tasks"
                   / "TASK-2026-0001.md").read_text(encoding="utf-8")
        spec = TaskUpdateSpec(new_content=TASK_COMPLETED,
                              expected_current_digest=P.digest(current),
                              created_by=ACTOR)
        _, result = self.run_plan(ContinuationPlan(task_update=spec))
        self.assertEqual(result.status, C.COMPLETE)
        updated = (self.fx.project / ".ai" / "tasks"
                   / "TASK-2026-0001.md").read_text(encoding="utf-8")
        self.assertIn("status: COMPLETED", updated)

    def test_task_update_stale_target_refused(self):
        spec = TaskUpdateSpec(new_content=TASK_COMPLETED,
                              expected_current_digest=P.digest("stale"),
                              created_by=ACTOR)
        _, result = self.run_plan(ContinuationPlan(task_update=spec))
        self.assertNotEqual(result.status, C.COMPLETE)
        task_now = (self.fx.project / ".ai" / "tasks"
                    / "TASK-2026-0001.md").read_text(encoding="utf-8")
        self.assertIn("status: IN_PROGRESS", task_now)


# --- persistence gate reuse & authority -------------------------------------

class TestPersistenceReuse(ContinuationTestCase):
    def test_every_write_goes_through_the_m44_gate(self):
        # No independent write path in the continuation package. Parse the AST
        # so a mention of "os.replace" in a docstring is never a false match.
        import ast
        # os.replace / os.rename are blocked via the os-import ban below; the
        # call set targets pathlib mutators and temp-file primitives, and omits
        # bare "replace" so it never collides with str.replace().
        banned_imports = {"os", "tempfile", "shutil"}
        banned_calls = {"fdopen", "mkstemp", "write_text", "write_bytes",
                        "rename", "unlink", "mkdir", "touch"}
        for name in ("flow.py", "planner.py", "model.py", "__init__.py"):
            tree = ast.parse((ROOT / "tabularium" / "engine" / "continuation" / name)
                             .read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name.split(".")[0],
                                         banned_imports,
                                         f"{name} imports {alias.name}")
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Attribute):
                        self.assertNotIn(func.attr, banned_calls,
                                         f"{name} calls .{func.attr}()")
                    if isinstance(func, ast.Name):
                        self.assertNotEqual(func.id, "open",
                                            f"{name} calls open()")

    def test_flow_only_imports_the_gate_writer(self):
        import ast
        tree = ast.parse((ROOT / "tabularium" / "engine" / "continuation" / "flow.py")
                         .read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        self.assertNotIn("os", imported)
        self.assertNotIn("tempfile", imported)
        self.assertTrue(any("persistence" in m for m in imported),
                        "flow.py must reach the durable write only via M4.4")

    def test_execution_approval_cannot_authorize_a_write(self):
        # An M4.3-style execution approval dict is not a persistence auth.
        prepared = build_proposals(self.outcome(),
                                   ContinuationPlan(handoff=self.handoff_spec()),
                                   str(self.fx.project))
        exec_approval = {prepared[0].proposal.proposal_id:
                         {"approval_request_id": "exec-1:approval-1",
                          "decision": "APPROVE", "decided_by": ACTOR,
                          "decided_at": AUTH_WHEN}}
        result = apply_continuation(prepared, exec_approval,
                                    str(self.fx.project),
                                    task_id="TASK-2026-0001")
        self.assertNotEqual(result.status, C.COMPLETE)
        self.assertFalse((self.fx.project / ".ai" / "handoffs"
                          / "HND-2026-0001.md").exists())

    def test_missing_authorization_writes_nothing(self):
        prepared = build_proposals(self.outcome(),
                                   ContinuationPlan(handoff=self.handoff_spec()),
                                   str(self.fx.project))
        result = apply_continuation(prepared, {}, str(self.fx.project),
                                    task_id="TASK-2026-0001")
        self.assertEqual(result.proposals[0].status, C.NOT_AUTHORIZED)
        self.assertFalse((self.fx.project / ".ai" / "handoffs"
                          / "HND-2026-0001.md").exists())


# --- multiple proposals & partial -------------------------------------------

class TestMultipleProposals(ContinuationTestCase):
    def test_per_record_outcomes(self):
        prepared, result = self.run_plan(
            ContinuationPlan(handoff=self.handoff_spec(),
                             report=self.report_spec()))
        self.assertEqual(len(result.proposals), 2)
        self.assertEqual(result.status, C.COMPLETE)

    def test_partial_when_one_authorization_missing(self):
        prepared = build_proposals(
            self.outcome(),
            ContinuationPlan(handoff=self.handoff_spec(),
                             report=self.report_spec()),
            str(self.fx.project))
        auths = self.authorize(prepared)
        # drop the report authorization
        report_pid = next(p.proposal.proposal_id for p in prepared
                          if p.family == C.REPORT)
        del auths[report_pid]
        result = apply_continuation(prepared, auths, str(self.fx.project),
                                    task_id="TASK-2026-0001")
        self.assertEqual(result.status, C.PARTIAL)
        self.assertIn("HND-2026-0001", result.written_record_ids)
        self.assertNotIn("RPT-2026-0001", result.written_record_ids)
        self.assertIn(C.CONTINUATION_PARTIAL, result.reason_codes)

    def test_no_durable_value_when_plan_empty(self):
        result = apply_continuation([], {}, str(self.fx.project),
                                    task_id="TASK-2026-0001",
                                    execution_id="exec-1")
        self.assertEqual(result.status, C.NO_DURABLE_VALUE)


# --- authority: no inflation ------------------------------------------------

class TestAuthorityBoundaries(ContinuationTestCase):
    def test_worker_claim_does_not_create_decision_or_evidence(self):
        # Output claiming decisions/evidence is inert: an empty plan persists
        # nothing, and the plan has no Decision/Evidence field at all.
        out = self.outcome(output="We decided X. I found evidence Y.")
        prepared = build_proposals(out, ContinuationPlan(), str(self.fx.project))
        self.assertEqual(prepared, [])
        for banned in ("decision", "evidence", "investigation", "question"):
            self.assertFalse(hasattr(ContinuationPlan(), banned))

    def test_supported_families_are_only_three(self):
        self.assertEqual(set(C.SUPPORTED_FAMILIES),
                         {"Handoff", "Report", "Task"})

    def test_no_family_inference_helper_exists(self):
        for name in ("planner.py", "flow.py", "model.py"):
            src = (ROOT / "tabularium" / "engine" / "continuation" / name).read_text(
                encoding="utf-8")
            for banned in ("propose_decision", "propose_evidence",
                           "classify_family", "infer_family",
                           "detect_decision"):
                self.assertNotIn(banned, src)


# --- FAILED / REJECTED executions -------------------------------------------

class TestNonCompletedExecutions(ContinuationTestCase):
    def test_rejected_execution_handoff_records_rejection(self):
        out = self.outcome(runtime_status="REJECTED", output="")
        spec = self.handoff_spec(what_remains="human rejected the change",
                                 next_step="revise per rejection")
        _, result = self.run_plan_with(out, ContinuationPlan(handoff=spec))
        self.assertEqual(result.status, C.COMPLETE)
        written = (self.fx.project / ".ai" / "handoffs"
                   / "HND-2026-0001.md").read_text(encoding="utf-8")
        self.assertIn("Runtime outcome: REJECTED", written)

    def run_plan_with(self, outcome, plan):
        prepared = build_proposals(outcome, plan, str(self.fx.project))
        auths = self.authorize(prepared)
        return prepared, apply_continuation(prepared, auths,
                                            str(self.fx.project),
                                            task_id="TASK-2026-0001",
                                            execution_id=outcome.execution_id)


# --- canonical safety -------------------------------------------------------

class TestCanonicalSafety(ContinuationTestCase):
    def test_only_intended_fixture_files_change(self):
        before = self.project_files()
        self.run_plan(ContinuationPlan(handoff=self.handoff_spec(),
                                       report=self.report_spec()))
        after = self.project_files()
        added = set(after) - set(before)
        self.assertEqual(added, {".ai/handoffs/HND-2026-0001.md",
                                 ".ai/reports/RPT-2026-0001.md"})
        # nothing else changed
        for key in before:
            self.assertEqual(before[key], after[key],
                             f"unexpected change to {key}")


# --- token economy ----------------------------------------------------------

class TestTokenEconomy(ContinuationTestCase):
    def _estimate(self, project):
        pkg = load_context(project, task="TASK-2026-0001",
                           ai_os_root=self.fx.ai_os_root)
        return pkg

    def test_continuation_context_is_measured_and_bounded(self):
        pkg_before = self._estimate(self.fx.project)
        before_ids = sorted(i.record_id for i in pkg_before.selected)

        self.run_plan(ContinuationPlan(handoff=self.handoff_spec(),
                                       report=self.report_spec()))

        pkg_after = self._estimate(self.fx.project)
        after_ids = sorted(i.record_id for i in pkg_after.selected)

        self.assertNotIn("HND-2026-0001", before_ids)
        self.assertIn("HND-2026-0001", after_ids)
        self.assertIn("RPT-2026-0001", after_ids)

        # The two continuation records add bounded content, not an explosion.
        added_chars = sum(
            len((self.fx.project / ".ai" / fam / rid).read_text(encoding="utf-8"))
            for fam, rid in (("handoffs", "HND-2026-0001.md"),
                             ("reports", "RPT-2026-0001.md")))
        self.assertLess(added_chars, 2 * CM.MAX_HANDOFF_BODY_CHARS)


# --- next-session continuity in a FRESH interpreter -------------------------

class TestFreshSessionContinuity(ContinuationTestCase):
    def test_handoff_visible_in_a_brand_new_process(self):
        # Session A: persist a Handoff.
        _, result = self.run_plan(ContinuationPlan(handoff=self.handoff_spec()))
        self.assertEqual(result.status, C.COMPLETE)

        # Session B: a fresh interpreter with NO in-memory state from A. It sees
        # the continuation purely from durable project files.
        script = (
            "import sys, json\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from tabularium.engine.context_loader import load_context\n"
            f"pkg = load_context({str(self.fx.project)!r}, task='TASK-2026-0001',"
            f" ai_os_root={str(self.fx.ai_os_root)!r})\n"
            "ids = sorted(i.record_id for i in pkg.selected)\n"
            "hnd = self_path = None\n"
            f"text = open({str(self.fx.project / '.ai' / 'handoffs' / 'HND-2026-0001.md')!r},"
            " encoding='utf-8').read()\n"
            "print(json.dumps({'status': pkg.package_status, 'ids': ids,"
            " 'has_next': 'Next step: Run the migration' in text}))\n")
        proc = subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = __import__("json").loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["status"], "READY")
        self.assertIn("HND-2026-0001", payload["ids"])
        self.assertTrue(payload["has_next"])


@contextmanager
def denied_listing(directory: Path):
    """Deterministically make one record directory unlistable.

    pathlib's glob silently treats an unreadable directory as empty, so the
    regression denies Path.iterdir for exactly this directory instead of
    relying on POSIX permission bits.
    """
    denied = str(directory.resolve())
    real_iterdir = Path.iterdir

    def selective(self, *args, **kwargs):
        if str(self.resolve()) == denied:
            raise PermissionError(13, "Permission denied")
        return real_iterdir(self, *args, **kwargs)

    with patch("tabularium.engine.continuation.planner.Path.iterdir", selective):
        yield


class RecordIdAllocationTests(ContinuationTestCase):
    """D2: id allocation never treats unreadable storage as empty and never
    mints an id outside the frozen canonical format."""

    def write_handoff(self, record_id: str) -> Path:
        path = self.fx.project / ".ai" / "handoffs" / f"{record_id}.md"
        path.write_text(
            "---\n"
            f'schema_version: "1.0"\nid: {record_id}\n'
            'title: "Existing"\ntask: "TASK-2026-0001"\n'
            f'created_at: "{WHEN}"\ncreated_by: "{ACTOR}"\n'
            "---\n# Handoff\n\nbody\n", encoding="utf-8")
        return path

    def test_unlistable_handoffs_directory_refuses_allocation(self):
        existing = self.write_handoff("HND-2026-0001")
        with denied_listing(self.fx.project / ".ai" / "handoffs"):
            prepared = build_proposals(self.outcome(),
                                       ContinuationPlan(handoff=self.handoff_spec()),
                                       str(self.fx.project))
        item = prepared[0]
        self.assertFalse(item.ok)
        self.assertEqual(item.reason_code, C.RECORD_ID_COLLISION)
        self.assertIn(".ai/handoffs", item.detail)
        # the pre-existing record is untouched and no write was prepared
        before = existing.read_bytes()
        result = apply_continuation(
            prepared, {}, str(self.fx.project),
            task_id="TASK-2026-0001", execution_id="exec-1")
        self.assertEqual(result.written_record_ids, ())
        self.assertNotEqual(result.status, C.COMPLETE)
        self.assertEqual(existing.read_bytes(), before)

    def test_unlistable_reports_directory_refuses_allocation(self):
        (self.fx.project / ".ai" / "reports" / "RPT-2026-0001.md").write_text(
            "---\n"
            'schema_version: "1.0"\nid: RPT-2026-0001\n'
            'title: "Existing"\ntask: "TASK-2026-0001"\n'
            f'created_at: "{WHEN}"\ncreated_by: "{ACTOR}"\n'
            "---\n# Report\n\nbody\n", encoding="utf-8")
        with denied_listing(self.fx.project / ".ai" / "reports"):
            prepared = build_proposals(self.outcome(),
                                       ContinuationPlan(report=self.report_spec()),
                                       str(self.fx.project))
        item = prepared[0]
        self.assertFalse(item.ok)
        self.assertEqual(item.reason_code, C.RECORD_ID_COLLISION)
        self.assertIn(".ai/reports", item.detail)

    def test_sequence_overflow_never_mints_a_non_canonical_id(self):
        self.write_handoff("HND-2026-9999")
        prepared = build_proposals(self.outcome(),
                                   ContinuationPlan(handoff=self.handoff_spec()),
                                   str(self.fx.project))
        item = prepared[0]
        self.assertFalse(item.ok)
        self.assertEqual(item.reason_code, C.RECORD_ID_COLLISION)
        self.assertNotIn("10000", item.detail)

    def test_non_canonical_sibling_filenames_neither_crash_nor_collide(self):
        self.write_handoff("HND-2026-0001")
        (self.fx.project / ".ai" / "handoffs" /
         "HND-2026-0003 (copy).md").write_text("stray sync artefact",
                                               encoding="utf-8")
        prepared = build_proposals(self.outcome(),
                                   ContinuationPlan(handoff=self.handoff_spec()),
                                   str(self.fx.project))
        item = prepared[0]
        self.assertTrue(item.ok)
        self.assertEqual(item.proposal.record_id, "HND-2026-0002")

    def test_allocation_increments_past_preexisting_ids(self):
        self.write_handoff("HND-2026-0005")
        prepared = build_proposals(self.outcome(),
                                   ContinuationPlan(handoff=self.handoff_spec()),
                                   str(self.fx.project))
        item = prepared[0]
        self.assertTrue(item.ok)
        self.assertEqual(item.proposal.record_id, "HND-2026-0006")

    def test_missing_family_directory_prepends_from_0001(self):
        prepared = build_proposals(self.outcome(),
                                   ContinuationPlan(handoff=self.handoff_spec()),
                                   str(self.fx.project))
        item = prepared[0]
        self.assertTrue(item.ok)
        self.assertEqual(item.proposal.record_id, "HND-2026-0001")

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0,
        "requires POSIX permissions and a non-root effective user")
    def test_exec_only_directory_cannot_allocate_behind_existing_records(self):
        self.write_handoff("HND-2026-0001")
        handoffs = self.fx.project / ".ai" / "handoffs"
        os.chmod(handoffs, 0o111)
        try:
            prepared = build_proposals(self.outcome(),
                                       ContinuationPlan(handoff=self.handoff_spec()),
                                       str(self.fx.project))
        finally:
            os.chmod(handoffs, 0o755)
        item = prepared[0]
        self.assertFalse(item.ok)
        self.assertEqual(item.reason_code, C.RECORD_ID_COLLISION)


if __name__ == "__main__":
    unittest.main()
