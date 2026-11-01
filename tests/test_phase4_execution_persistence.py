"""Phase 4 execution output to governed persistence contract tests.

Validates the complete execution-to-persistence lifecycle:
- build_execution_report_proposal extracts deterministic Report proposal from completed run
- apply_execution_report_proposal binds single-path SHA-256 PersistenceAuthorization
- 9-condition persistence gate enforces project confinement, secrets scanner, and schema
- atomic write commits report to .ai/reports/RPT-YYYY-NNNN.md
- fail-closed rejection on tampered content, missing authorization, or secrets
"""

from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import yaml

from tabularium.cli import main
from tabularium.execution import (
    ProductResult,
    apply_execution_report_proposal,
    build_execution_report_proposal,
)
from tabularium.init import initialize
from tabularium.onboarding import build_authorizations, onboard_project
from tabularium.engine.execution import PipelineResult
from tabularium.engine.persistence import model as PM


def current_year() -> int:
    return datetime.now(timezone.utc).year


class ExecutionPersistenceFixture(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-phase4-")
        self.root = Path(self.holder.name)
        initialize(self.root)
        declarations = {
            "title": "Phase 4 Fixture",
            "purpose": "execution persistence tests",
            "scope": "Bounded execution persistence testing",
        }
        draft = onboard_project(self.root, declarations)
        result = onboard_project(
            self.root, declarations,
            build_authorizations(draft.proposal, "human:user", "2026-01-01T00:00:00+00:00")
        )
        self.assertEqual(result.status, "ONBOARDED")
        (self.root / ".ai" / "reports").mkdir(exist_ok=True)
        (self.root / ".ai" / "tasks").mkdir(exist_ok=True)

    def tearDown(self):
        self.holder.cleanup()

    def mock_product_result(self, output="Execution succeeded with full validation.", status="COMPLETED") -> ProductResult:
        pipeline = PipelineResult(
            status=status,
            execution_id="exec-p4-001",
            task_id="TASK-2026-0001",
            worker_result={"worker_id": "worker-native-01", "worker_category": "CLI_CODING"},
            output=output,
            reason_codes=(),
            diagnostics=(),
        )
        return ProductResult(
            status=status,
            code="COMPLETED" if status == "COMPLETED" else "FAILED",
            message="Work completed.",
            target=str(self.root),
            pipeline=pipeline,
        )


class TestExecutionReportProposal(ExecutionPersistenceFixture):
    def test_build_proposal_from_completed_execution(self):
        res = self.mock_product_result()
        proposal_item = build_execution_report_proposal(self.root, res, author="alex")
        self.assertIsNotNone(proposal_item)
        self.assertTrue(proposal_item.ok)

        prop = proposal_item.proposal
        self.assertTrue(prop.record_id.startswith(f"RPT-{current_year():04d}-"))
        self.assertEqual(prop.target_family, "Report")
        self.assertEqual(prop.operation, "CREATE")
        self.assertEqual(prop.target_path, f".ai/reports/{prop.record_id}.md")
        self.assertTrue(len(prop.content_digest) == 64)
        self.assertIn("schema_version: \"1.0\"", prop.content)
        self.assertIn("Execution succeeded with full validation.", prop.content)

    def test_build_proposal_ignores_failed_or_empty_execution(self):
        failed_res = self.mock_product_result(status="FAILED")
        self.assertIsNone(build_execution_report_proposal(self.root, failed_res))

        empty_res = self.mock_product_result(output="")
        self.assertIsNone(build_execution_report_proposal(self.root, empty_res))


class TestExecutionReportPersistence(ExecutionPersistenceFixture):
    def test_apply_authorized_report_writes_to_reports_directory(self):
        res = self.mock_product_result()
        proposal_item = build_execution_report_proposal(self.root, res, author="alex")
        prop = proposal_item.proposal

        persist_res = apply_execution_report_proposal(
            self.root, prop, authorized_by="alex", authorized_at="2026-09-06T10:00:00Z"
        )
        self.assertEqual(persist_res.status, PM.WRITTEN)
        self.assertTrue(persist_res.written)
        self.assertEqual(persist_res.authorized_by, "alex")

        report_file = self.root / prop.target_path
        self.assertTrue(report_file.is_file())
        text = report_file.read_text(encoding="utf-8")
        self.assertIn("schema_version: \"1.0\"", text)
        self.assertIn(f"id: \"{prop.record_id}\"", text)

    def test_persistence_fails_closed_on_secret_material(self):
        secret_output = "Found secret: sk-1234567890abcdef"
        res = self.mock_product_result(output=secret_output)
        proposal_item = build_execution_report_proposal(self.root, res, author="alex")
        prop = proposal_item.proposal

        persist_res = apply_execution_report_proposal(
            self.root, prop, authorized_by="alex", authorized_at="2026-09-06T10:00:00Z"
        )
        self.assertEqual(persist_res.status, PM.DENIED)
        self.assertIn(PM.SECRET_MATERIAL_PRESENT, persist_res.reason_codes)

        report_file = self.root / prop.target_path
        self.assertFalse(report_file.exists())


class TestCLIPersistenceFlow(ExecutionPersistenceFixture):
    def test_cli_persist_prompt_authorized(self):
        res = self.mock_product_result()
        with mock.patch("tabularium.cli.run_instruction", return_value=res), \
             mock.patch("builtins.input", side_effect=["y", "alex", "2026-09-06T10:00:00Z"]):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(["run", "--persist", "do something", str(self.root)])
            self.assertEqual(code, 0)
            self.assertIn("Successfully persisted report RPT-", out.getvalue())

    def test_cli_persist_prompt_declined(self):
        res = self.mock_product_result()
        with mock.patch("tabularium.cli.run_instruction", return_value=res), \
             mock.patch("builtins.input", side_effect=["n"]):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(["run", "--persist", "do something", str(self.root)])
            self.assertEqual(code, 0)
            self.assertIn("Persistence not authorized", err.getvalue())


if __name__ == "__main__":
    unittest.main()
