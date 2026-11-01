"""Tests for the M4.2 governed execution pipeline.

Everything upstream of the worker is PRODUCTION code: the real M2 loader, the
real projector, real skill governance, a real WorkerRequest, and the real M4.1
runtime. Only the worker itself is deterministic and fake, so no model, CLI, or
credential is needed.

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

import yaml  # noqa: E402

from tabularium.engine.execution import model as P  # noqa: E402
from tabularium.engine.projection.model import ProjectedPayload  # noqa: E402
from tabularium.engine.skills import ControlPlaneGrant  # noqa: E402
from tests.test_context_loader import Fixture  # noqa: E402
from tests.test_runtime import (CategoryWorker,  # noqa: E402
                                        human_approval)
from tabularium.engine.workers import MockWorker  # noqa: E402

try:
    import langgraph  # noqa: F401
    LANGGRAPH = True
except ImportError:  # pragma: no cover - environment dependent
    LANGGRAPH = False

needs_runtime = unittest.skipUnless(
    LANGGRAPH, "LangGraph not installed (tabularium/engine/requirements-runtime.txt)")

PROSE = ("Narrative paragraph that exists to occupy budget without changing "
         "meaning. ")


def long_text(chars: int) -> str:
    return (PROSE * (chars // len(PROSE) + 1))[:chars]


class PipelineTestCase(unittest.TestCase):
    """A throwaway project plus a checkpoint directory outside it."""

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)
        self.workdir = tempfile.mkdtemp(prefix="m42-")
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)
        self.db = os.path.join(self.workdir, "checkpoints.db")

    # -- fixtures -----------------------------------------------------------

    def actionable_task(self, task_id="TASK-2026-0001", **fields):
        return self.fx.task(task_id, plan="Do the thing carefully.", **fields)

    def large_project(self):
        self.fx.doc("constraints.md", "# Constraints\n\n" + long_text(3000))
        self.fx.doc("project.md", "# Project\n\n" + long_text(2000))
        self.fx.doc("current-state.md", "# Current State\n\n" + long_text(2000))
        self.fx.rec("TASK-2026-0001",
                    {"schema_version": "1.10", "title": "Root",
                     "objective": "Do it", "status": "IN_PROGRESS",
                     "created_at": "2026-01-01T00:00:00Z",
                     "created_by": "USER", "plan": long_text(1500),
                     "decisions": ["DEC-2026-0001"]},
                    "# Root Task\n\n" + long_text(3000))
        self.fx.decision("DEC-2026-0001", task="TASK-2026-0001",
                         body="# Decision\n\n" + long_text(2000))
        self.fx.handoff("HND-2026-0001",
                        body="# Handoff\n\n## Current State\n\n"
                             + long_text(1500)
                             + "\n\n## Continuation\n\nContinue safely.\n")

    def skill(self, name="narrow-skill", context_classes=None, **sidecar):
        """Write a skill package into the scratch directory, not the project."""
        root = Path(self.workdir) / name
        root.mkdir(parents=True, exist_ok=True)
        (root / "SKILL.md").write_text(
            f'---\nname: {name}\nversion: "1.0"\n'
            f'description: A skill used only by tests.\n---\n\n# Skill\n\nDo it.\n',
            encoding="utf-8")
        data = {
            "sidecar_version": "1.0",
            "allowed_context_classes": context_classes
            if context_classes is not None
            else ["required", "constraints", "task_narrative"],
            "allowed_worker_categories": ["CLI_CODING", "API_MODEL", "LOCAL"],
            "allowed_tool_classes": ["NONE"],
            "approval_required": False,
            "output_contract": "TEXT",
            "persistence": "NONE",
        }
        data.update(sidecar)
        (root / "tabu.yaml").write_text(yaml.safe_dump(data, sort_keys=False),
                                        encoding="utf-8")
        return str(root)

    def plan(self, **overrides) -> P.ExecutionPlan:
        base = {"project_root": str(self.fx.project),
                "task_id": "TASK-2026-0001",
                "intent": "continue the project",
                "execution_id": "exec-1",
                "checkpoint_path": self.db,
                "adapter": MockWorker(),
                "ai_os_root": str(self.fx.ai_os_root)}
        base.update(overrides)
        return P.ExecutionPlan(**base)

    def start(self, **overrides):
        from tabularium.engine.execution import start_execution
        return start_execution(self.plan(**overrides))

    def ai_snapshot(self) -> dict:
        digest = {}
        for path in sorted((self.fx.project / ".ai").rglob("*")):
            if path.is_file():
                digest[str(path.relative_to(self.fx.project))] = \
                    hashlib.sha256(path.read_bytes()).hexdigest()
        return digest


# --- preparation gates ------------------------------------------------------

@needs_runtime
class TestContextGates(PipelineTestCase):
    """Nothing reaches a worker unless M2 says the context is usable."""

    def test_ready_and_actionable_executes(self):
        self.actionable_task()
        result = self.start()
        self.assertEqual(result.status, P.COMPLETED)
        self.assertTrue(result.executed)

    def test_blocked_package_never_reaches_a_worker(self):
        self.actionable_task()
        self.fx.remove("project.md")          # required foundation -> BLOCKED
        result = self.start()
        self.assertEqual(result.status, P.PREPARATION_REJECTED)
        self.assertIn(P.CONTEXT_PACKAGE_NOT_READY, result.reason_codes)
        self.assertIsNone(result.worker_result)
        self.assertFalse(result.executed)

    def test_diagnostic_only_package_never_reaches_a_worker(self):
        self.actionable_task()
        self.fx.manifest({"manifest_version": "1.0",
                          "tabularium_ruleset": "9.9.9"})
        result = self.start()
        self.assertEqual(result.status, P.PREPARATION_REJECTED)
        self.assertIn(P.CONTEXT_PACKAGE_NOT_READY, result.reason_codes)

    def test_not_actionable_task_never_reaches_a_worker(self):
        # A blocking open Question makes the Task NOT_ACTIONABLE under M2.
        self.actionable_task(questions=["QST-2026-0001"])
        self.fx.question("QST-2026-0001", task="TASK-2026-0001",
                         impact="BLOCKING")
        result = self.start()
        self.assertEqual(result.status, P.PREPARATION_REJECTED)
        self.assertIn(P.TASK_NOT_ACTIONABLE, result.reason_codes)
        self.assertIsNone(result.worker_result)

    def test_refusal_keeps_diagnostic_detail(self):
        self.actionable_task(questions=["QST-2026-0001"])
        self.fx.question("QST-2026-0001", task="TASK-2026-0001",
                         impact="BLOCKING")
        result = self.start()
        self.assertTrue(result.diagnostics)
        self.assertTrue(any("OPEN_BLOCKING_QUESTION" in d.get("code", "")
                            or "actionability" in d.get("detail", "")
                            for d in result.diagnostics))

    def test_missing_task_target_is_refused(self):
        self.actionable_task()
        result = self.start(task_id="")
        self.assertIn(P.TASK_TARGET_REQUIRED, result.reason_codes)

    def test_worker_is_required(self):
        self.actionable_task()
        result = self.start(adapter=None)
        self.assertIn(P.WORKER_REQUIRED, result.reason_codes)

    def test_checkpoint_path_is_required_and_outside_the_project(self):
        self.actionable_task()
        self.assertIn(P.CHECKPOINT_PATH_REQUIRED,
                      self.start(checkpoint_path="").reason_codes)
        inside = str(self.fx.project / ".ai" / "runtime.db")
        self.assertIn(P.CHECKPOINT_INSIDE_PROJECT,
                      self.start(checkpoint_path=inside).reason_codes)


# --- skill governance -------------------------------------------------------

@needs_runtime
class TestSkillGovernance(PipelineTestCase):

    def test_no_skill_uses_the_control_plane_grant(self):
        self.actionable_task()
        result = self.start()
        self.assertEqual(result.status, P.COMPLETED)
        self.assertIn("required", result.metrics.effective_allowed_classes)

    def test_valid_skill_is_applied(self):
        self.actionable_task()
        result = self.start(skill_path=self.skill())
        self.assertEqual(result.status, P.COMPLETED)
        self.assertEqual(set(result.metrics.effective_allowed_classes),
                         {"required", "constraints", "task_narrative"})

    def test_missing_skill_is_refused(self):
        self.actionable_task()
        result = self.start(skill_path=str(Path(self.workdir) / "nope"))
        self.assertEqual(result.status, P.PREPARATION_REJECTED)
        self.assertIn(P.SKILL_NOT_FOUND, result.reason_codes)

    def test_malformed_skill_is_refused(self):
        self.actionable_task()
        path = self.skill(name="broken")
        (Path(path) / "tabu.yaml").write_text("allowed: [oops\n",
                                              encoding="utf-8")
        result = self.start(skill_path=path)
        self.assertEqual(result.status, P.PREPARATION_REJECTED)
        self.assertIn(P.SKILL_MALFORMED, result.reason_codes)

    def test_skill_narrows_projected_context(self):
        # THE HB-M3-04 FIX. A forbidden class must never be projected.
        self.large_project()
        wide = self.start(execution_id="wide")
        narrow = self.start(
            execution_id="narrow",
            skill_path=self.skill(context_classes=["required", "constraints"]))
        self.assertEqual(narrow.status, P.COMPLETED)
        self.assertEqual(set(narrow.metrics.projected_sections),
                         {"required", "constraints"})
        self.assertNotIn("task_narrative", narrow.metrics.projected_sections)
        # The wide run proves the section was available and genuinely excluded.
        self.assertIn("task_narrative", wide.metrics.projected_sections)

    def test_skill_cannot_widen_beyond_the_control_plane_grant(self):
        self.large_project()
        grant = ControlPlaneGrant(context_classes=("required", "constraints"))
        result = self.start(
            grant=grant,
            skill_path=self.skill(context_classes=[
                "required", "constraints", "task_narrative", "decisions"]))
        self.assertEqual(result.status, P.COMPLETED)
        self.assertEqual(set(result.metrics.effective_allowed_classes),
                         {"required", "constraints"})
        self.assertNotIn("decisions", result.metrics.projected_sections)

    def test_effective_classes_without_required_are_refused(self):
        # `required` carries package status, actionability, and approval state.
        # A payload without it would let a worker act without knowing whether
        # it is permitted to, so the execution is refused rather than fixed up.
        self.actionable_task()
        result = self.start(
            skill_path=self.skill(context_classes=["constraints"]))
        self.assertEqual(result.status, P.PREPARATION_REJECTED)
        self.assertIn(P.SKILL_CONSTRAINT_VIOLATION, result.reason_codes)

    def test_skill_approval_true_raises_the_requirement(self):
        self.actionable_task()
        result = self.start(skill_path=self.skill(approval_required=True))
        self.assertEqual(result.status, P.AWAITING_APPROVAL)

    def test_skill_approval_false_cannot_lower_a_requirement(self):
        self.actionable_task()
        result = self.start(approval_required=True,
                            skill_path=self.skill(approval_required=False))
        self.assertEqual(result.status, P.AWAITING_APPROVAL)
        self.assertTrue(result.governance["approval_required"])

    def test_skill_persistence_cannot_widen(self):
        self.actionable_task()
        result = self.start(
            grant=ControlPlaneGrant(persistence="NONE"),
            skill_path=self.skill(persistence="PROPOSE"))
        self.assertEqual(result.governance["persistence"], "NONE")

    def test_skill_worker_category_restriction_is_enforced(self):
        self.actionable_task()
        result = self.start(
            adapter=CategoryWorker("w-local", "LOCAL", "OUT"),
            skill_path=self.skill(
                allowed_worker_categories=["CLI_CODING", "API_MODEL"]))
        self.assertEqual(result.status, P.PREPARATION_REJECTED)
        self.assertIn(P.WORKER_INCOMPATIBLE, result.reason_codes)
        self.assertIsNone(result.worker_result)


# --- projection -------------------------------------------------------------

@needs_runtime
class TestProjectionIntegration(PipelineTestCase):

    def test_hot_and_warm_profiles(self):
        self.large_project()
        hot = self.start(execution_id="hot", profile="HOT")
        warm = self.start(execution_id="warm", profile="WARM")
        self.assertEqual(hot.status, P.COMPLETED)
        self.assertEqual(warm.status, P.COMPLETED)
        self.assertLessEqual(hot.metrics.payload_size["estimated_tokens"], 1200)
        self.assertLessEqual(warm.metrics.payload_size["estimated_tokens"], 4000)
        self.assertGreaterEqual(warm.metrics.payload_size["estimated_tokens"],
                                hot.metrics.payload_size["estimated_tokens"])

    def test_unsupported_profile_is_refused(self):
        self.actionable_task()
        result = self.start(profile="COLD")
        self.assertEqual(result.status, P.PREPARATION_REJECTED)
        self.assertIn(P.PROJECTION_FAILED, result.reason_codes)
        self.assertIn("UNSUPPORTED_PROJECTION_PROFILE", result.reason_codes)

    def test_worker_receives_the_projected_payload_not_the_package(self):
        seen = {}

        class Recorder(CategoryWorker):
            def execute(self, request):
                seen["payload"] = request.payload
                seen["governance"] = dict(request.governance)
                return super().execute(request)

        self.large_project()
        result = self.start(adapter=Recorder("rec", "LOCAL", "OUT"))
        self.assertEqual(result.status, P.COMPLETED)
        # The payload crosses a durable checkpoint as plain JSON — that is
        # required for it to be checkpointable at all — but the worker is
        # handed back the canonical ProjectedPayload the M3.3 contract
        # promises (M4.7 integration bug #1). Those are separate concerns:
        # the CHECKPOINT is data, the WORKER INPUT is the contract type.
        # What matters here is that the CONTENT is the projected sections and
        # nothing from the Context Package.
        payload = seen["payload"]
        self.assertIsInstance(payload, ProjectedPayload)
        self.assertEqual(set(payload.section_names()),
                         set(result.metrics.projected_sections))
        text = json.dumps(payload.to_dict())
        # No absolute project path, and none of the Context Package's own
        # structure. `package_status` DOES appear inside the `required`
        # governance section — that is what M3.3 designed it to carry, and a
        # worker needs it to know whether it is permitted to act.
        self.assertNotIn(str(self.fx.project), text)
        for package_key in ("selection_trace", "\"selected\"",
                            "\"foundations\"", "\"omissions\"",
                            "project_root", "ai_os_root"):
            with self.subTest(key=package_key):
                self.assertNotIn(package_key, text)
        self.assertIn("package_status", text)  # inside `required.governance`

    def test_dropped_sections_never_reappear_in_the_payload(self):
        self.large_project()
        result = self.start(
            skill_path=self.skill(context_classes=["required", "constraints"]))
        from tabularium.engine.execution import inspect_execution
        stored = inspect_execution(result.execution_id, self.db)
        text = json.dumps(stored.payload.to_dict()
                          if hasattr(stored.payload, "to_dict")
                          else stored.payload)
        for forbidden in ("task_narrative", "decisions", "continuation"):
            with self.subTest(section=forbidden):
                self.assertNotIn(f'"{forbidden}"', text)

    def test_metrics_are_reported(self):
        self.large_project()
        metrics = self.start().metrics
        for field in ("available_context", "governed_context", "payload_size",
                      "context_avoided"):
            with self.subTest(field=field):
                self.assertIn("estimated_tokens", getattr(metrics, field))
        self.assertIsNotNone(metrics.reduction_pct)
        self.assertGreater(metrics.reduction_pct, 0)

    def test_projection_savings_survive_integration(self):
        self.large_project()
        metrics = self.start().metrics
        governed = metrics.governed_context["estimated_tokens"]
        payload = metrics.payload_size["estimated_tokens"]
        self.assertLess(payload, governed / 2,
                        f"payload={payload} governed={governed}")


# --- worker and model -------------------------------------------------------

@needs_runtime
class TestWorkerAndModel(PipelineTestCase):

    def test_all_worker_categories_run_through_one_pipeline(self):
        self.actionable_task()
        from tabularium.engine.workers.api import ModelTarget
        cases = [("LOCAL", None), ("CLI_CODING", None),
                 ("API_MODEL", ModelTarget("openai/gpt-4o-mini"))]
        for category, target in cases:
            with self.subTest(category=category):
                result = self.start(
                    execution_id=f"exec-{category}",
                    adapter=CategoryWorker(f"w-{category}", category,
                                           f"{category}_OUT"),
                    model_target=target)
                self.assertEqual(result.status, P.COMPLETED)
                self.assertEqual(result.output, f"{category}_OUT")

    def test_api_worker_without_a_model_target_is_refused(self):
        # A model is never guessed: choosing one is a cost and capability
        # decision that belongs to routing, which does not exist yet.
        self.actionable_task()
        result = self.start(
            adapter=CategoryWorker("w-api", "API_MODEL", "OUT"))
        self.assertEqual(result.status, P.PREPARATION_REJECTED)
        self.assertIn(P.MODEL_TARGET_REQUIRED, result.reason_codes)
        self.assertIsNone(result.worker_result)

    def test_incompatible_required_category_is_refused_before_execution(self):
        # Checked by the PIPELINE, not delegated to the adapter: a permissive
        # adapter must not be able to let a mismatched execution start.
        self.actionable_task()
        result = self.start(required_category="API_MODEL",
                            adapter=CategoryWorker("w", "LOCAL", "OUT"))
        self.assertEqual(result.status, P.PREPARATION_REJECTED)
        self.assertIn(P.WORKER_INCOMPATIBLE, result.reason_codes)
        self.assertIsNone(result.worker_result)

    def test_missing_required_capability_is_refused_before_execution(self):
        self.actionable_task()
        result = self.start(required_capabilities=("WRITES_FILES",),
                            adapter=CategoryWorker("w", "LOCAL", "OUT"))
        self.assertEqual(result.status, P.PREPARATION_REJECTED)
        self.assertIn(P.WORKER_INCOMPATIBLE, result.reason_codes)

    def test_pipeline_does_not_execute_the_adapter_directly(self):
        source = (ROOT / "tabularium" / "engine" / "execution"
                  / "pipeline.py").read_text(encoding="utf-8")
        self.assertNotIn("adapter.execute(", source)
        self.assertIn("runtime.start(", source)


# --- approval ---------------------------------------------------------------

@needs_runtime
class TestApprovalFlow(PipelineTestCase):

    def test_awaiting_approval_surfaces_no_output(self):
        self.actionable_task()
        result = self.start(approval_required=True)
        self.assertEqual(result.status, P.AWAITING_APPROVAL)
        self.assertEqual(result.output, "")
        self.assertIsNotNone(result.approval_request)
        self.assertTrue(result.approval_request.request_id)

    def test_approve_completes_through_the_pipeline(self):
        from tabularium.engine.execution import resume_execution
        self.actionable_task()
        started = self.start(approval_required=True)
        result = resume_execution(
            "exec-1", self.db,
            human_approval(started.approval_request, "exec-1"), MockWorker())
        self.assertEqual(result.status, P.COMPLETED)
        self.assertIn("mock executed", result.output)

    def test_reject_produces_no_output(self):
        from tabularium.engine.execution import resume_execution
        self.actionable_task()
        started = self.start(approval_required=True)
        result = resume_execution(
            "exec-1", self.db,
            human_approval(started.approval_request, "exec-1", "REJECT"),
            MockWorker())
        self.assertEqual(result.status, P.REJECTED)
        self.assertEqual(result.output, "")

    def test_invalid_resume_propagates_safely(self):
        from tabularium.engine.execution import resume_execution
        for bad in (None, True, "APPROVE", {"decision": "APPROVE"},
                    {"decision": "yes"}):
            with self.subTest(bad=bad):
                self.fx.cleanup()
                self.setUp()
                self.actionable_task()
                self.start(approval_required=True)
                result = resume_execution("exec-1", self.db, bad, MockWorker())
                self.assertNotEqual(result.status, P.COMPLETED)
                self.assertEqual(result.output, "")


# --- safety -----------------------------------------------------------------

@needs_runtime
class TestProjectSafety(PipelineTestCase):

    def test_project_context_is_never_written(self):
        from tabularium.engine.execution import resume_execution
        self.actionable_task()
        before = self.ai_snapshot()
        started = self.start(approval_required=True)
        resume_execution("exec-1", self.db,
                         human_approval(started.approval_request, "exec-1"),
                         MockWorker())
        self.assertEqual(self.ai_snapshot(), before)

    def test_checkpoint_stays_outside_the_project(self):
        self.actionable_task()
        self.start()
        for path in (self.fx.project / ".ai").rglob("*"):
            self.assertNotIn(".db", path.name)

    def test_no_m1_record_is_created(self):
        self.actionable_task()
        result = self.start()
        blob = json.dumps(result.to_dict())
        for marker in ("EVD-", "DEC-2026", "HND-", "RPT-", "approved_by"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, blob)

    def test_no_credential_reaches_state_or_result(self):
        import re
        self.actionable_task()
        result = self.start()
        blob = json.dumps(result.to_dict()) + Path(self.db).read_bytes().decode(
            "utf-8", errors="ignore")
        for pattern in (r"\bsk-[A-Za-z0-9]{8,}", r"\bapi[_-]?key\b",
                        r"\bpassword\b", r"\bbearer\b"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, blob, re.IGNORECASE))

    def test_no_silent_fallback_in_the_pipeline_source(self):
        source = (ROOT / "tabularium" / "engine" / "execution"
                  / "pipeline.py").read_text(encoding="utf-8")
        # No profile switching, no default worker, no model guessing.
        self.assertNotIn('profile="WARM"', source.replace(" ", ""))
        self.assertNotIn("except Exception:\n        pass", source)


class TestImportIsolation(unittest.TestCase):
    """Only the runtime path may pull LangGraph; only API transport LiteLLM."""

    def check(self, module: str, package: str) -> str:
        code = (f"import sys, {module}; "
                f"print(any(m == '{package}' or m.startswith('{package}.') "
                "for m in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                             capture_output=True, text=True, shell=False)
        return out.stdout.strip() or out.stderr[-300:]

    def test_upstream_layers_pull_no_runtime_or_provider(self):
        for module in ("tabularium.engine.context_loader", "tabularium.engine.projection",
                       "tabularium.engine.skills", "tabularium.engine.workers"):
            for package in ("langgraph", "litellm"):
                with self.subTest(module=module, package=package):
                    self.assertEqual(self.check(module, package), "False")

    def test_importing_the_pipeline_pulls_neither(self):
        for package in ("langgraph", "litellm"):
            with self.subTest(package=package):
                self.assertEqual(self.check("tabularium.engine.execution", package),
                                 "False")

    def test_loader_does_not_import_the_upper_layers(self):
        for package in ("tabularium.engine.projection", "tabularium.engine.skills",
                        "tabularium.engine.workers", "tabularium.engine.runtime",
                        "tabularium.engine.execution"):
            with self.subTest(package=package):
                self.assertEqual(self.check("tabularium.engine.context_loader", package),
                                 "False")


# --- process-death end to end -----------------------------------------------

PHASE_SCRIPT = r'''
import json, sys
sys.path.insert(0, r"{root}")
sys.path.insert(0, r"{tests}")
from tabularium.engine.execution import ExecutionPlan, start_execution, resume_execution
from test_runtime import CategoryWorker

phase = sys.argv[1]
worker = CategoryWorker("worker-a", "LOCAL", "PIPELINE_E2E_OUT")

if phase == "start":
    project_root, ai_os_root, db, skill = sys.argv[2:6]
    plan = ExecutionPlan(project_root=project_root, task_id="TASK-2026-0001",
                         intent="continue the project",
                         execution_id="e2e-1", checkpoint_path=db,
                         adapter=worker, ai_os_root=ai_os_root,
                         skill_path=(skill or None), approval_required=True)
    out = start_execution(plan)
else:
    db, raw = sys.argv[2], json.loads(sys.argv[3])
    if isinstance(raw, dict) and raw.get("decision") in ("APPROVE", "REJECT"):
        from tabularium.engine.execution import inspect_execution
        from tabularium.engine.runtime import HumanApprovalResolution
        stored = inspect_execution("e2e-1", db)
        pending = (stored.approval_request or {{}}).get("request_id", "")
        raw = HumanApprovalResolution(
            approval_request_id=pending, execution_id="e2e-1",
            task_id="TASK-2026-0001", decision=raw["decision"],
            decided_by="human:user", decided_at="2026-08-18T12:00:00Z")
    out = resume_execution("e2e-1", db, raw, worker)
print(json.dumps(out.to_dict(), default=str))
'''


@needs_runtime
class TestProcessDeathEndToEnd(PipelineTestCase):
    """THE M4.2 TEST: real loader, projector, skill governance, runtime."""

    def setUp(self):
        super().setUp()
        self.large_project()
        self.skill_path = self.skill(
            context_classes=["required", "constraints", "task_narrative"])
        self.script = Path(self.workdir) / "phase.py"
        self.script.write_text(
            PHASE_SCRIPT.format(root=str(ROOT),
                                tests=str(ROOT / "tests")),
            encoding="utf-8")

    def phase(self, *args) -> dict:
        out = subprocess.run([sys.executable, str(self.script), *args],
                             cwd=str(ROOT), capture_output=True, text=True,
                             shell=False, timeout=300)
        self.assertEqual(out.returncode, 0,
                         f"phase {args[0]} failed:\n{out.stderr[-2000:]}")
        for line in reversed(out.stdout.strip().splitlines()):
            if line.startswith("{"):
                return json.loads(line)
        raise AssertionError(f"no JSON: {out.stdout[-800:]}")

    def test_full_pipeline_across_process_death_with_approval(self):
        before = self.ai_snapshot()

        started = self.phase("start", str(self.fx.project),
                             str(self.fx.ai_os_root), self.db, self.skill_path)
        self.assertEqual(started["status"], P.AWAITING_APPROVAL)
        self.assertEqual(started["output"], "")
        self.assertTrue(started["approval_request"]["request_id"])
        # Skill narrowing survived the whole pipeline.
        self.assertEqual(set(started["metrics"]["projected_sections"]),
                         {"required", "constraints", "task_narrative"})

        # PROCESS 2 — a genuinely new interpreter resumes from disk.
        resumed = self.phase("resume", self.db,
                             json.dumps({"decision": "APPROVE"}))
        self.assertEqual(resumed["status"], P.COMPLETED)
        self.assertEqual(resumed["output"], "PIPELINE_E2E_OUT")

        self.assertEqual(self.ai_snapshot(), before)

    def test_full_pipeline_across_process_death_with_rejection(self):
        before = self.ai_snapshot()
        self.phase("start", str(self.fx.project), str(self.fx.ai_os_root),
                   self.db, self.skill_path)
        resumed = self.phase("resume", self.db,
                             json.dumps({"decision": "REJECT"}))
        self.assertEqual(resumed["status"], P.REJECTED)
        self.assertEqual(resumed["output"], "")
        self.assertEqual(self.ai_snapshot(), before)

    def test_invalid_resume_across_process_death_fails_closed(self):
        self.phase("start", str(self.fx.project), str(self.fx.ai_os_root),
                   self.db, self.skill_path)
        resumed = self.phase("resume", self.db, json.dumps(True))
        self.assertNotEqual(resumed["status"], P.COMPLETED)
        self.assertEqual(resumed["output"], "")
        good = self.phase("resume", self.db,
                          json.dumps({"decision": "APPROVE"}))
        self.assertEqual(good["status"], P.COMPLETED)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
