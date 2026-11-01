"""F3: bounded recovery guidance for API provider-class failures.

The machine-readable reason codes (RATE_LIMITED, CONTEXT_WINDOW_EXCEEDED,
CONTENT_REJECTED) are stable and unchanged. Only the human-facing message
gains specific, static, secret-free remediation direction. No retry, no
fallback, no network behavior, and no provider/exception payload leakage.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tabularium.execution import run_instruction
from tabularium.init import initialize
from tabularium.onboarding import build_authorizations, onboard_project
from tabularium.engine.workers.api import APIWorker, ModelTarget


def _category_exception(name: str) -> type[Exception]:
    return type(name, (Exception,), {})


class GuidanceTests(unittest.TestCase):
    """_from_pipeline guidance, driven through the full product entrypoint."""

    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-f3-")
        self.root = Path(self.holder.name)
        initialize(self.root)
        declarations = {
            "title": "F3 fixture",
            "purpose": "Exercise provider-class failure guidance",
            "scope": "A bounded offline fixture",
        }
        draft = onboard_project(self.root, declarations)
        onboard_project(
            self.root, declarations,
            build_authorizations(draft.proposal, "human:user",
                                 "2026-08-19T12:00:00+00:00"))
        task_dir = self.root / ".ai" / "tasks"
        task_dir.mkdir(exist_ok=True)
        content = {
            "schema_version": "1.10", "id": "TASK-2026-0001",
            "title": "T", "objective": "o", "status": "IN_PROGRESS",
            "created_at": "2026-01-01T00:00:00Z", "created_by": "USER",
            "plan": "p",
        }
        (task_dir / "TASK-2026-0001.md").write_text(
            "---\n" + yaml.safe_dump(content, sort_keys=False) +
            "---\n# Task\n\nbody\n", encoding="utf-8")

    def tearDown(self):
        self.holder.cleanup()

    @staticmethod
    def worker_raising(exc: Exception) -> APIWorker:
        def fail(**kwargs):
            raise exc
        return APIWorker(model_target=ModelTarget("test/model"),
                         completion_fn=fail)

    def assert_guidance(self, exception_name: str, code: str,
                        expected_fragments: list[str]):
        exc = _category_exception(exception_name)(
            "provider prose sk-FakeProbe000000 should never surface")
        result = run_instruction("Review this", self.root,
                                 adapter=self.worker_raising(exc))
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.code, code)
        for fragment in expected_fragments:
            self.assertIn(fragment, result.message)
        self.assertNotIn("Traceback", result.message)
        self.assertNotIn("provider prose", result.message)
        self.assertNotIn("sk-FakeProbe000000", result.message)

    def test_rate_limited_points_to_waiting_not_retrying_now(self):
        self.assert_guidance(
            "RateLimitError", "RATE_LIMITED",
            ["rate limit was reached", "Wait and retry later"])

    def test_context_window_exceeded_points_to_reducing_input(self):
        self.assert_guidance(
            "ContextWindowExceededError", "CONTEXT_WINDOW_EXCEEDED",
            ["context window", "Reduce the supplied context or input"])

    def test_content_rejected_points_to_policy_review(self):
        self.assert_guidance(
            "ContentPolicyViolationError", "CONTENT_REJECTED",
            ["rejected the request content under its policy",
             "Review the instruction and supplied content"])

    def test_messages_are_bounded_and_static(self):
        cases = [
            ("RateLimitError", "RATE_LIMITED"),
            ("ContextWindowExceededError", "CONTEXT_WINDOW_EXCEEDED"),
            ("ContentPolicyViolationError", "CONTENT_REJECTED"),
        ]
        for name, code in cases:
            result = run_instruction(
                "Review this", self.root,
                adapter=self.worker_raising(
                    _category_exception(name)("x" * 5000)))
            self.assertEqual(result.code, code)
            self.assertLess(len(result.message), 600)

    def test_generic_codes_keep_their_established_messages(self):
        from tabularium.execution import _from_pipeline
        from tabularium.engine.execution.model import PipelineResult

        def pipeline_with(code):
            return _from_pipeline(
                self.root, PipelineResult(
                    status="FAILED", execution_id="f3-generic",
                    task_id="TASK-2026-0001", reason_codes=(code,),
                    diagnostics=({"code": code},)))

        for code in ("PROVIDER_UNAVAILABLE", "INVALID_REQUEST", "TIMEOUT",
                     "TRANSPORT_ERROR", "UNKNOWN_PROVIDER_ERROR"):
            result = pipeline_with(code)
            self.assertEqual(result.code, "WORKER_EXECUTION_FAILED")
            self.assertIn("could not complete the request", result.message)
        unlisted = pipeline_with("SOME_FUTURE_CODE")
        self.assertEqual(unlisted.code, "SOME_FUTURE_CODE")
        self.assertIn("Review the reason and try the indicated remediation",
                      unlisted.message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
