"""Unit tests for the policy enforcement engine (Step 5)."""

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

from tabularium.engine.execution.model import ExecutionPlan, POLICY_VIOLATION
from tabularium.engine.execution.pipeline import start_execution
from tabularium.engine.policies import (
    MODEL_ASSIGNMENT_MISMATCH,
    POLICY_REASON_CODES,
    RISK_APPROVAL_REQUIRED,
    SCOPE_RISK_EXCEEDED,
    SECRET_DETECTED_IN_CONTEXT,
    evaluate_policies,
)
from tabularium.engine.policies.model import POLICY_INPUT_INVALID, PolicyVerdict
from tabularium.engine.projection.model import ContextEconomy, ProjectedPayload, ProjectionResult
from tests.e2e_fixture import needs_runtime
from tabularium.engine.workers.adapter import WorkerAdapter
from tabularium.engine.workers.model import WorkerDescriptor, WorkerRequest, WorkerResult, COMPLETED


class MockAdapter:
    def __init__(self, category="CLI_PROCESS", capabilities=("STRUCTURED_OUTPUT",)):
        self._descriptor = WorkerDescriptor(
            worker_id="mock-worker",
            category=category,
            capabilities=capabilities,
        )

    @property
    def descriptor(self):
        return self._descriptor

    def accepts(self, request):
        return True, ()

    def execute(self, request):
        return WorkerResult(
            request_id=request.request_id,
            status=COMPLETED,
            output="ok",
        )


class TestPolicyEngine(unittest.TestCase):
    def setUp(self):
        self.plan = SimpleNamespace(
            intent="Test policy enforcement",
            model_target="gpt-4o",
            profile="HOT",
            project_root="/tmp",
            ai_os_root="/tmp",
        )
        self.package = SimpleNamespace(
            root_task={
                "metadata": {
                    "assigned_model": "gpt-4o",
                    "risk": "LOW",
                }
            }
        )
        z = {"chars": 2000, "estimated_tokens": 500}
        self.projection = ProjectionResult(
            outcome="PROJECTED", profile="HOT", budget_limit_tokens=1200,
            estimated_payload_tokens=500, within_budget=True,
            economy=ContextEconomy(z, z, z, {"chars": 0, "estimated_tokens": 0}),
            payload=ProjectedPayload((("required", {"text": "Clean text"}),)),
        )
        self.constraints = SimpleNamespace(
            context_classes=("required",),
            approval_required=False,
            persistence=False,
            output_contract="TEXT",
        )

    def test_clean_evaluation_allowed(self):
        verdict = evaluate_policies(self.plan, self.package, self.projection, self.constraints)
        self.assertTrue(verdict.allowed)
        self.assertFalse(verdict.requires_approval)
        self.assertEqual(len(verdict.violations), 0)

    def test_model_assignment_mismatch_refused(self):
        self.package.root_task["metadata"]["assigned_model"] = "claude-3-5-sonnet"
        verdict = evaluate_policies(self.plan, self.package, self.projection, self.constraints)
        self.assertFalse(verdict.allowed)
        self.assertIn(MODEL_ASSIGNMENT_MISMATCH, verdict.reason_codes)

    def test_high_risk_requires_approval(self):
        self.package.root_task["metadata"]["risk"] = "HIGH"
        verdict = evaluate_policies(self.plan, self.package, self.projection, self.constraints)
        self.assertTrue(verdict.allowed)
        self.assertTrue(verdict.requires_approval)

    def test_scope_risk_exceeded_refused(self):
        self.package.root_task["metadata"]["risk"] = "CRITICAL"
        self.package.root_task["metadata"]["approved_scope"] = {"approved_risk": "MEDIUM"}
        verdict = evaluate_policies(self.plan, self.package, self.projection, self.constraints)
        self.assertFalse(verdict.allowed)
        self.assertIn(SCOPE_RISK_EXCEEDED, verdict.reason_codes)

    def test_secret_in_intent_refused(self):
        self.plan.intent = "Using api_key = sk-1234567890abcdef to authenticate"
        verdict = evaluate_policies(self.plan, self.package, self.projection, self.constraints)
        self.assertFalse(verdict.allowed)
        self.assertIn(SECRET_DETECTED_IN_CONTEXT, verdict.reason_codes)

    def test_secret_in_projected_payload_shapes_refused(self):
        SECRET = "sk-abcdefghij1234567890"
        shapes = (
            {"text": SECRET},
            {"task": {"notes": SECRET}},
            {"items": [SECRET]},
            {"body": SECRET},
            SECRET,
        )
        for shape in shapes:
            with self.subTest(shape=shape):
                proj = replace(self.projection, payload=ProjectedPayload((("required", shape),)))
                verdict = evaluate_policies(self.plan, self.package, proj, self.constraints)
                self.assertFalse(verdict.allowed)
                self.assertIn(SECRET_DETECTED_IN_CONTEXT, verdict.reason_codes)

    def test_risk_input_validation_matrix(self):
        matrix = [
            ("HIGH", None, True, True, ()),
            ("high", None, False, False, (POLICY_INPUT_INVALID,)),
            ("EXTREME", None, False, False, (POLICY_INPUT_INVALID,)),
            ("CRITICAL", "low", False, False, (POLICY_INPUT_INVALID,)),
            ("CRITICAL", "NONSENSE", False, False, (POLICY_INPUT_INVALID,)),
            ("CRITICAL", "MEDIUM", False, False, (SCOPE_RISK_EXCEEDED,)),
        ]
        for task_risk, approved_risk, exp_allowed, exp_approval, exp_codes in matrix:
            with self.subTest(task_risk=task_risk, approved_risk=approved_risk):
                self.package.root_task["metadata"]["risk"] = task_risk
                if approved_risk is not None:
                    self.package.root_task["metadata"]["approved_scope"] = {"approved_risk": approved_risk}
                else:
                    self.package.root_task["metadata"].pop("approved_scope", None)
                verdict = evaluate_policies(self.plan, self.package, self.projection, self.constraints)
                self.assertEqual(verdict.allowed, exp_allowed)
                self.assertEqual(verdict.requires_approval, exp_approval)
                for code in exp_codes:
                    self.assertIn(code, verdict.reason_codes)

    def test_matching_model_target_object_is_allowed(self):
        from tabularium.engine.workers.api.model import ModelTarget
        self.plan.model_target = ModelTarget(model="openai/gpt-4o")
        self.package.root_task["metadata"]["assigned_model"] = "openai/gpt-4o"
        verdict = evaluate_policies(self.plan, self.package, self.projection, self.constraints)
        self.assertTrue(verdict.allowed, verdict.violations)

    def test_mismatched_model_target_object_is_refused(self):
        from tabularium.engine.workers.api.model import ModelTarget
        self.plan.model_target = ModelTarget(model="anthropic/claude-opus-4")
        self.package.root_task["metadata"]["assigned_model"] = "openai/gpt-4o"
        verdict = evaluate_policies(self.plan, self.package, self.projection, self.constraints)
        self.assertIn(MODEL_ASSIGNMENT_MISMATCH, verdict.reason_codes)


    def test_malformed_task_metadata_does_not_crash_the_engine(self):
        """The engine sits unguarded on the execution path: it must refuse, not raise."""
        for meta in (None, [], "not-a-mapping"):
            with self.subTest(metadata=meta):
                self.package.root_task = {"metadata": meta}
                verdict = evaluate_policies(self.plan, self.package,
                                            self.projection, self.constraints)
                self.assertTrue(verdict.allowed)

    def test_section_nested_past_the_scan_bound_is_refused(self):
        """Unscannable is not clean: an unwalkable section fails closed."""
        from tabularium.engine.policies.engine import MAX_SCAN_DEPTH
        from tabularium.engine.policies.model import POLICY_INPUT_INVALID

        deep = current = {}
        for _ in range(MAX_SCAN_DEPTH + 5):
            current["n"] = {}
            current = current["n"]
        current["leak"] = "sk-abcdefghij1234567890"

        from dataclasses import replace
        projection = replace(self.projection,
                             payload=ProjectedPayload((("required", deep),)))
        verdict = evaluate_policies(self.plan, self.package, projection,
                                    self.constraints)
        self.assertFalse(verdict.allowed)
        self.assertIn(POLICY_INPUT_INVALID, verdict.reason_codes)

class PolicyEngineE2ETests(unittest.TestCase):
    @needs_runtime
    def test_secret_in_task_narrative_fails_pipeline(self):
        from tests.e2e_fixture import E2ETestCase
        from tabularium.engine.execution.model import POLICY_VIOLATION, SECRET_DETECTED_IN_CONTEXT

        tc = E2ETestCase()
        tc.setUp()
        try:
            # Inject secret into task narrative right after frontmatter
            task_path = tc.fx.project / ".ai" / "tasks" / "TASK-2026-0001.md"
            content = task_path.read_text(encoding="utf-8")
            parts = content.split("---", 2)
            new_content = "---" + parts[1] + "---\n# Root Task\n\nSecret: sk-abcdefghij1234567890\n" + parts[2]
            task_path.write_text(new_content, encoding="utf-8")

            skill_p = tc.skill(
                "secret-e2e-skill",
                allowed_context_classes=["required", "task_narrative"],
            )
            res = tc.start(skill_path=skill_p)
            self.assertEqual(res.status, "PREPARATION_REJECTED")
            self.assertIn(POLICY_VIOLATION, res.reason_codes)
            self.assertIn(SECRET_DETECTED_IN_CONTEXT, res.reason_codes)
        finally:
            tc.tearDown()
if __name__ == "__main__":
    unittest.main()
