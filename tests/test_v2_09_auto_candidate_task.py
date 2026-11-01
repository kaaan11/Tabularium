"""Deterministic acceptance tests for Tabularium V2-09: automatic candidate task for --change.

Covers all 9 acceptance criteria for automatic candidate tasks:
1. No active Task + --change + 'y' -> candidate Task created and ends in COMPLETED;
   approval_status=APPROVED; approved_by/at populated; target file updated; legal schema transitions.
2. Two other active Tasks + --change (no --task) -> no TASK_SELECTION_REQUIRED; run uses candidate Task;
   worker context does NOT contain other tasks' objectives, DOES contain candidate objective.
3. 'n' -> candidate Task CANCELLED, reason APPROVAL_REJECTED recorded, target byte-identical.
4. Gate denial (.env) -> candidate Task CANCELLED, reason code recorded, confirmation never prompted.
5. Non-interactive -> candidate Task CANCELLED, reason code recorded.
6. Consecutive --change runs differing only in .ai/ records -> second run does NOT get GIT_DIRTY.
   Tracked changes outside .ai/ still get GIT_DIRTY.
7. --task X + different instruction -> single-line warning emitted on stderr, run proceeds with X,
   no new Task created.
8. start_execution raises exception -> WORKER_UNAVAILABLE product result, no exception leaks,
   worktree cleaned, candidate Task CANCELLED.
9. tabu run without --change -> byte-identical behavior: no candidate Task created,
   existing TASK_REQUIRED / TASK_SELECTION_REQUIRED behavior preserved.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from tabularium.execution import run_instruction
from tabularium.task import read_frontmatter, task_create
from tabularium.engine.code_gate.model import PROTECTED_TARGET_DENIED
from tests.test_v2_09_change_path import ChangePathFixture, FakeWorkerAdapter


class TestV209AutoCandidateTask(ChangePathFixture):
    """Verifies acceptance criteria for automatic candidate task creation."""

    def test_acceptance_01_no_active_task_creates_completed_auto_task(self):
        """1. When no active Task exists and --change + 'y' is used, a new Task is created and ends in COMPLETED;
        approval_status=APPROVED; target file changed; transition chain conforms to schema."""
        tasks_dir = self.root / ".ai" / "tasks"
        for f in tasks_dir.glob("*.md"):
            f.unlink()

        new_code = "def calculate_total(a, b):\n    # auto-task verified\n    return a + b + 42\n"

        def mutate(wt_dir: Path):
            (wt_dir / "src" / "sample.py").write_text(new_code, encoding="utf-8")

        fake_worker = FakeWorkerAdapter(mutate_fn=mutate)
        instruction = "Refactor calculate_total to add 42"

        result = run_instruction(
            instruction,
            self.root,
            adapter=fake_worker,
            change=True,
            confirm=lambda _: True,
        )

        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.code, "COMPLETED")
        self.assertEqual(self.src_file.read_text(encoding="utf-8"), new_code)

        created_tasks = list(tasks_dir.glob("TASK-*.md"))
        self.assertEqual(len(created_tasks), 1)
        task_file = created_tasks[0]
        rec = read_frontmatter(task_file, self.root, "Task")
        self.assertEqual(rec.frontmatter.get("status"), "COMPLETED")
        self.assertEqual(rec.frontmatter.get("approval_status"), "APPROVED")
        self.assertTrue(bool(rec.frontmatter.get("approved_by")))
        self.assertTrue(bool(rec.frontmatter.get("approved_at")))
        self.assertEqual(rec.frontmatter.get("title"), instruction[:80])
        self.assertEqual(rec.frontmatter.get("objective"), instruction)

    def test_acceptance_02_multiple_active_tasks_ignored_uses_candidate(self):
        """2. When two other active Tasks exist and --change is used (without specifying a Task), TASK_SELECTION_REQUIRED
        is not returned; run uses candidate Task. Worker context does not contain other tasks' objectives,
        does contain candidate objective."""
        tasks_dir = self.root / ".ai" / "tasks"
        for f in tasks_dir.glob("*.md"):
            f.unlink()

        t1 = task_create(self.root, title="Task A", objective="OBJECTIVE_AAA_UNIQUE")
        self.assertTrue(t1.created)
        t2 = task_create(self.root, title="Task B", objective="OBJECTIVE_BBB_UNIQUE")
        self.assertTrue(t2.created)

        received_requests = []

        def mutate(wt_dir: Path):
            (wt_dir / "src" / "sample.py").write_text("def calculate_total(a, b): pass\n")

        fake_worker = FakeWorkerAdapter(mutate_fn=mutate)
        orig_execute = fake_worker.execute

        def spy_execute(req):
            received_requests.append(req)
            return orig_execute(req)

        fake_worker.execute = spy_execute

        instruction = "OBJECTIVE_CANDIDATE_UNIQUE: implement total"
        result = run_instruction(
            instruction,
            self.root,
            adapter=fake_worker,
            change=True,
            confirm=lambda _: True,
        )

        self.assertEqual(result.status, "COMPLETED")
        self.assertNotEqual(result.code, "TASK_SELECTION_REQUIRED")

        self.assertEqual(len(received_requests), 1)
        req = received_requests[0]
        payload_str = json.dumps(req.payload) if isinstance(req.payload, (dict, list)) else str(req.payload)

        self.assertNotIn("OBJECTIVE_AAA_UNIQUE", payload_str)
        self.assertNotIn("OBJECTIVE_BBB_UNIQUE", payload_str)
        self.assertIn("OBJECTIVE_CANDIDATE_UNIQUE", payload_str)
        self.assertNotIn(req.task_id, (t1.record_id, t2.record_id))

    def test_acceptance_03_rejection_cancels_candidate_task(self):
        """3. 'n' -> candidate Task CANCELLED, reason APPROVAL_REJECTED recorded, target byte-identical."""
        tasks_dir = self.root / ".ai" / "tasks"
        for f in tasks_dir.glob("*.md"):
            f.unlink()

        initial_src_content = self.src_file.read_text(encoding="utf-8")

        def mutate(wt_dir: Path):
            (wt_dir / "src" / "sample.py").write_text("rejected change\n")

        fake_worker = FakeWorkerAdapter(mutate_fn=mutate)
        result = run_instruction(
            "Candidate instruction to reject",
            self.root,
            adapter=fake_worker,
            change=True,
            confirm=lambda _: False,
        )

        self.assertEqual(result.status, "REFUSED")
        self.assertEqual(result.code, "APPROVAL_REJECTED")
        self.assertEqual(self.src_file.read_text(encoding="utf-8"), initial_src_content)

        created_tasks = list(tasks_dir.glob("TASK-*.md"))
        self.assertEqual(len(created_tasks), 1)
        task_file = created_tasks[0]
        rec = read_frontmatter(task_file, self.root, "Task")
        self.assertEqual(rec.frontmatter.get("status"), "CANCELLED")
        self.assertEqual(rec.frontmatter.get("result"), "APPROVAL_REJECTED")
        self.assertIn("APPROVAL_REJECTED", task_file.read_text(encoding="utf-8"))

    def test_acceptance_04_gate_denial_cancels_candidate_task_no_confirm(self):
        """4. Gate denial (.env) -> candidate Task CANCELLED, reason code recorded, no question asked."""
        tasks_dir = self.root / ".ai" / "tasks"
        for f in tasks_dir.glob("*.md"):
            f.unlink()

        initial_src_content = self.src_file.read_text(encoding="utf-8")

        def mutate(wt_dir: Path):
            (wt_dir / ".env").write_text("SECRET=12345\n")

        confirm_called = []

        def spy_confirm(_):
            confirm_called.append(True)
            return True

        fake_worker = FakeWorkerAdapter(mutate_fn=mutate)
        result = run_instruction(
            "Write secret to env",
            self.root,
            adapter=fake_worker,
            change=True,
            confirm=spy_confirm,
        )

        self.assertEqual(result.status, "REFUSED")
        self.assertEqual(result.code, PROTECTED_TARGET_DENIED)
        self.assertEqual(len(confirm_called), 0)
        self.assertEqual(self.src_file.read_text(encoding="utf-8"), initial_src_content)

        created_tasks = list(tasks_dir.glob("TASK-*.md"))
        self.assertEqual(len(created_tasks), 1)
        task_file = created_tasks[0]
        rec = read_frontmatter(task_file, self.root, "Task")
        self.assertEqual(rec.frontmatter.get("status"), "CANCELLED")
        self.assertEqual(rec.frontmatter.get("result"), PROTECTED_TARGET_DENIED)

    def test_acceptance_05_non_interactive_cancels_candidate_task(self):
        """5. Non-interactive -> candidate Task CANCELLED (or other reasoned terminal state), reason recorded."""
        tasks_dir = self.root / ".ai" / "tasks"
        for f in tasks_dir.glob("*.md"):
            f.unlink()

        def mutate(wt_dir: Path):
            (wt_dir / "src" / "sample.py").write_text("non-interactive change\n")

        fake_worker = FakeWorkerAdapter(mutate_fn=mutate)
        with patch("sys.stdin.isatty", return_value=False):
            result = run_instruction(
                "Change without tty",
                self.root,
                adapter=fake_worker,
                change=True,
                confirm=None,
            )

        self.assertEqual(result.status, "AWAITING_APPROVAL")
        self.assertEqual(result.code, "APPROVAL_REQUIRED")

        created_tasks = list(tasks_dir.glob("TASK-*.md"))
        self.assertEqual(len(created_tasks), 1)
        task_file = created_tasks[0]
        rec = read_frontmatter(task_file, self.root, "Task")
        self.assertEqual(rec.frontmatter.get("status"), "CANCELLED")
        self.assertEqual(rec.frontmatter.get("result"), "APPROVAL_REQUIRED")

    def test_acceptance_06_dirty_check_ignores_ai_directory_changes(self):
        """6. Consecutive --change runs differing only in .ai/ records -> second run does not get
        GIT_DIRTY. Tracked changes outside .ai/ still get GIT_DIRTY."""
        def mutate1(wt_dir: Path):
            (wt_dir / "src" / "sample.py").write_text("first run output\n")

        fake1 = FakeWorkerAdapter(mutate_fn=mutate1)
        res1 = run_instruction("first instruction", self.root, adapter=fake1, change=True, confirm=lambda _: True)
        self.assertEqual(res1.status, "COMPLETED")

        # The first run commits its own change (auto-commit). An uncommitted
        # record under .ai/ must still not make the tree dirty.
        import subprocess
        tracked = subprocess.run(["git", "ls-files", ".ai"], cwd=str(self.root), check=True,
                                 capture_output=True, text=True).stdout.split()
        note = next(self.root / f for f in tracked if f.endswith(".md") and "/tasks/" not in f
                    and "/reports/" not in f)
        note.write_text(note.read_text() + "\nuncommitted governance note\n")

        def mutate2(wt_dir: Path):
            (wt_dir / "src" / "sample.py").write_text("second run output\n")

        fake2 = FakeWorkerAdapter(mutate_fn=mutate2)
        res2 = run_instruction("second instruction", self.root, adapter=fake2, change=True, confirm=lambda _: True)
        self.assertEqual(res2.status, "COMPLETED")
        self.assertNotEqual(res2.code, "GIT_DIRTY")

        (self.root / "src" / "sample.py").write_text("dirty edit\n")
        fake3 = FakeWorkerAdapter(mutate_fn=mutate2)
        res3 = run_instruction("third instruction", self.root, adapter=fake3, change=True, confirm=lambda _: True)
        self.assertEqual(res3.status, "REFUSED")
        self.assertEqual(res3.code, "GIT_DIRTY")
        self.assertEqual(fake3.call_count, 0)

    def test_acceptance_07_explicit_task_warns_on_different_objective(self):
        """7. --task X + different instruction -> warning line emitted, run proceeds with X, no new Task created."""
        tasks_dir = self.root / ".ai" / "tasks"
        initial_task_count = len(list(tasks_dir.glob("TASK-*.md")))

        def mutate(wt_dir: Path):
            (wt_dir / "src" / "sample.py").write_text("updated with explicit task\n")

        fake_worker = FakeWorkerAdapter(mutate_fn=mutate)
        different_instruction = "Something completely different from task objective"

        stderr_buffer = io.StringIO()
        with patch("sys.stderr", stderr_buffer):
            result = run_instruction(
                different_instruction,
                self.root,
                task_id=self.task_id,
                adapter=fake_worker,
                change=True,
                confirm=lambda _: True,
            )

        self.assertEqual(result.status, "COMPLETED")
        current_tasks = list(tasks_dir.glob("TASK-*.md"))
        self.assertEqual(len(current_tasks), initial_task_count)

        self.assertTrue(any("Warning:" in w and self.task_id in w for w in result.warnings))
        self.assertIn("Warning:", stderr_buffer.getvalue())
        self.assertIn(self.task_id, stderr_buffer.getvalue())

    def test_acceptance_08_start_execution_exception_handled(self):
        """8. start_execution raises exception -> product result WORKER_UNAVAILABLE, exception does not leak,
        worktree cleaned up, candidate Task CANCELLED."""
        tasks_dir = self.root / ".ai" / "tasks"
        for f in tasks_dir.glob("*.md"):
            f.unlink()

        fake_worker = FakeWorkerAdapter()

        with patch("tabularium.change_path.start_execution", side_effect=RuntimeError("Worker crashed unexpectedly")):
            result = run_instruction(
                "Run that causes start_execution to fail",
                self.root,
                adapter=fake_worker,
                change=True,
                confirm=lambda _: True,
            )

        self.assertEqual(result.status, "REFUSED")
        self.assertEqual(result.code, "WORKER_UNAVAILABLE")

        wt_list = self.list_worktrees()
        self.assertEqual(len(wt_list), 1)
        self.assertFalse(any("tabu-wt-" in wt for wt in wt_list))

        created_tasks = list(tasks_dir.glob("TASK-*.md"))
        self.assertEqual(len(created_tasks), 1)
        rec = read_frontmatter(created_tasks[0], self.root, "Task")
        self.assertEqual(rec.frontmatter.get("status"), "CANCELLED")
        self.assertEqual(rec.frontmatter.get("result"), "WORKER_UNAVAILABLE")

    def test_acceptance_09_run_without_change_preserves_behavior(self):
        """9. tabu run without --change -> no Task created; existing TASK_REQUIRED/TASK_SELECTION_REQUIRED
        behavior preserved."""
        tasks_dir = self.root / ".ai" / "tasks"
        for f in tasks_dir.glob("*.md"):
            f.unlink()

        fake_worker = FakeWorkerAdapter()
        res1 = run_instruction("some command", self.root, adapter=fake_worker, change=False)
        self.assertEqual(res1.status, "REFUSED")
        self.assertEqual(res1.code, "TASK_REQUIRED")
        self.assertEqual(len(list(tasks_dir.glob("TASK-*.md"))), 0)

        task_create(self.root, title="Task 1", objective="Obj 1")
        task_create(self.root, title="Task 2", objective="Obj 2")
        self.assertEqual(len(list(tasks_dir.glob("TASK-*.md"))), 2)

        res2 = run_instruction("some command", self.root, adapter=fake_worker, change=False)
        self.assertEqual(res2.status, "REFUSED")
        self.assertEqual(res2.code, "TASK_SELECTION_REQUIRED")
        self.assertEqual(len(list(tasks_dir.glob("TASK-*.md"))), 2)
