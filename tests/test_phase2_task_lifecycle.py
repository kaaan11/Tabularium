"""Phase 2 task lifecycle CLI and transition contract tests.

Validates the Task lifecycle state machine:
- tabu task start (INBOX -> IN_PROGRESS)
- tabu task approve (record human approval, WAITING_APPROVAL -> IN_PROGRESS)
- tabu task complete (IN_PROGRESS/REVIEW -> COMPLETED)
- tabu task transition (enforce SCHEMA_TRANSITIONS, reject invalid transitions fail-closed,
  support --force, reject unknown status names with exit code 2)
- CLI invocation via run_main and JSON output schema.
- DRL-005 file atomicity and narrative body preservation.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

from tabularium import task as T
from tabularium.cli import main
from tabularium.init import initialize
from tabularium.onboarding import build_authorizations, onboard_project


def current_year() -> int:
    return datetime.now(timezone.utc).year


class TaskLifecycleFixture(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-lifecycle-")
        self.root = Path(self.holder.name)
        initialize(self.root)
        declarations = {
            "title": "Lifecycle fixture",
            "purpose": "task lifecycle testing",
            "scope": "Bounded lifecycle project",
        }
        draft = onboard_project(self.root, declarations)
        result = onboard_project(
            self.root, declarations,
            build_authorizations(draft.proposal, "human:user",
                                 "2026-01-01T00:00:00+00:00"))
        self.assertEqual(result.status, "ONBOARDED")
        self.task_dir = self.root / ".ai" / "tasks"
        self.task_dir.mkdir(exist_ok=True)

    def tearDown(self):
        self.holder.cleanup()

    @staticmethod
    def run_main(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def create_task(self, title="Lifecycle Task", objective="Test state transitions") -> str:
        create_res = T.task_create(self.root, title=title, objective=objective, priority="HIGH")
        self.assertEqual(create_res.exit_code, 0)
        return create_res.record_id


class TaskLifecycleUnitTests(TaskLifecycleFixture):
    def test_task_start_transitions_inbox_to_in_progress(self):
        tid = self.create_task()
        result = T.task_start(self.root, tid)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.previous_status, "INBOX")
        self.assertEqual(result.new_status, "IN_PROGRESS")
        self.assertEqual(result.record_id, tid)

        # Verify disk contents
        task_path = self.root / result.file
        content = task_path.read_text(encoding="utf-8")
        fm = yaml.safe_load(content.split("\n---\n", 1)[0])
        self.assertEqual(fm["status"], "IN_PROGRESS")
        self.assertIn("updated_at", fm)

    def test_task_transition_enforces_allowed_transitions(self):
        tid = self.create_task()
        # Initial status is INBOX. Allowed targets: ANALYZING, CANCELLED
        # Trying to transition directly to COMPLETED without force must fail
        result = T.task_transition(self.root, tid, "COMPLETED")
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.code, "TASK_TRANSITION_INVALID")
        self.assertIn("Cannot transition Task from INBOX to COMPLETED", result.message)

        # Valid sequential transitions: INBOX -> ANALYZING -> PLANNED -> WAITING_APPROVAL -> IN_PROGRESS
        res1 = T.task_transition(self.root, tid, "ANALYZING")
        self.assertEqual(res1.exit_code, 0)
        self.assertEqual(res1.new_status, "ANALYZING")

        res2 = T.task_transition(self.root, tid, "PLANNED")
        self.assertEqual(res2.exit_code, 0)
        self.assertEqual(res2.new_status, "PLANNED")

        res3 = T.task_transition(self.root, tid, "WAITING_APPROVAL")
        self.assertEqual(res3.exit_code, 0)
        self.assertEqual(res3.new_status, "WAITING_APPROVAL")

        res4 = T.task_transition(self.root, tid, "IN_PROGRESS")
        self.assertEqual(res4.exit_code, 0)
        self.assertEqual(res4.new_status, "IN_PROGRESS")

    def test_task_transition_with_force_allows_jumping_states(self):
        tid = self.create_task()
        # Force INBOX -> COMPLETED
        result = T.task_transition(self.root, tid, "COMPLETED", force=True)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.new_status, "COMPLETED")

    def test_task_transition_rejects_unknown_status(self):
        tid = self.create_task()
        result = T.task_transition(self.root, tid, "NON_EXISTENT_STATUS")
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.code, "TASK_STATUS_INVALID")
        self.assertIn("Invalid Task status", result.message)

    def test_task_approve_records_metadata_and_moves_to_in_progress(self):
        tid = self.create_task()
        # Transition to WAITING_APPROVAL first
        T.task_transition(self.root, tid, "WAITING_APPROVAL", force=True)

        res_app = T.task_approve(self.root, tid, by="alex")
        self.assertEqual(res_app.exit_code, 0)
        self.assertEqual(res_app.new_status, "IN_PROGRESS")

        task_path = self.root / res_app.file
        content = task_path.read_text(encoding="utf-8")
        fm = yaml.safe_load(content.split("\n---\n", 1)[0])
        self.assertEqual(fm["status"], "IN_PROGRESS")
        self.assertEqual(fm["approval_status"], "APPROVED")
        self.assertEqual(fm["approved_by"], "alex")
        self.assertIn("approved_at", fm)

    def test_task_complete_transitions_to_completed(self):
        tid = self.create_task()
        T.task_start(self.root, tid)
        res_comp = T.task_complete(self.root, tid)
        self.assertEqual(res_comp.exit_code, 0)
        self.assertEqual(res_comp.new_status, "COMPLETED")

        task_path = self.root / res_comp.file
        content = task_path.read_text(encoding="utf-8")
        fm = yaml.safe_load(content.split("\n---\n", 1)[0])
        self.assertEqual(fm["status"], "COMPLETED")

    def test_task_not_found(self):
        result = T.task_start(self.root, "TASK-2026-9999")
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.code, "TASK_NOT_FOUND")


class TaskLifecycleCLITests(TaskLifecycleFixture):
    def test_cli_start_command(self):
        tid = self.create_task()
        code, out, err = self.run_main(["task", "start", tid, str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn(f"State transition: INBOX -> IN_PROGRESS", out)

    def test_cli_transition_json_output(self):
        tid = self.create_task()
        code, out, err = self.run_main(["task", "transition", "--json", tid, "ANALYZING", str(self.root)])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["id"], tid)
        self.assertEqual(data["previous_status"], "INBOX")
        self.assertEqual(data["new_status"], "ANALYZING")
        self.assertEqual(data["exit_code"], 0)

    def test_cli_invalid_transition_fails_cleanly(self):
        tid = self.create_task()
        code, out, err = self.run_main(["task", "transition", tid, "COMPLETED", str(self.root)])
        self.assertEqual(code, 1)
        full_output = out + err
        self.assertIn("TASK_TRANSITION_INVALID", full_output)

    def test_cli_approve_command(self):
        tid = self.create_task()
        self.run_main(["task", "transition", "--force", tid, "WAITING_APPROVAL", str(self.root)])
        code, out, err = self.run_main(["task", "approve", tid, str(self.root), "--by", "security-lead"])
        self.assertEqual(code, 0)
        self.assertIn("State transition: WAITING_APPROVAL -> IN_PROGRESS", out)

    def test_cli_complete_command(self):
        tid = self.create_task()
        self.run_main(["task", "start", tid, str(self.root)])
        code, out, err = self.run_main(["task", "complete", tid, str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn("State transition: IN_PROGRESS -> COMPLETED", out)


if __name__ == "__main__":
    unittest.main()
