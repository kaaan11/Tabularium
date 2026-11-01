"""B1/B2-P1 regression tests: Task refusal diagnostics and remediation.

These tests pin the fail-closed zero-active-Task refusal while requiring the
refusal to distinguish, from existing discovery evidence only:

    no usable record / malformed / invalid(quarantined) / filename-id mismatch /
    duplicate identity / terminal-only

Selection semantics are unchanged: exactly one usable active Task must still
be selected, more than one must still demand explicit selection, and no
unusable record may ever become actionable or repaired.

The surfaced refusal text is a sanitized, bounded OUTPUT BOUNDARY: user-derived
fragments (record paths, validator defect reasons) must never carry terminal
control sequences, fabricate additional output lines, or exceed the configured
per-fragment bound. Internal validation evidence is not rewritten by that.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import yaml

from tabularium.diagnostics import READY, status_project
from tabularium.execution import (TASK_REQUIRED, TASK_SELECTION_REQUIRED,
                            _UNUSABLE_TASK_DETAIL_LIMIT, run_instruction)
from tabularium.init import initialize
from tabularium.onboarding import build_authorizations, onboard_project
from tabularium.engine.workers import MockWorker
from tests.environment_support import pin_coherent_installation


try:
    import langgraph  # noqa: F401
    HAS_RUNTIME = True
except ImportError:  # pragma: no cover - environment dependent
    HAS_RUNTIME = False


def minimal_task(task_id: str, **overrides) -> dict:
    """One canonical minimal Task frontmatter mapping used by these fixtures."""
    content = {
        "schema_version": "1.10",
        "id": task_id,
        "title": f"Task {task_id}",
        "objective": "Exercise the governed run entrypoint.",
        "status": "INBOX",
        "created_at": "2026-01-01T00:00:00Z",
        "created_by": "USER",
    }
    content.update(overrides)
    return content


def render_task(content: dict) -> str:
    return "---\n" + yaml.safe_dump(content, sort_keys=False) + \
        "---\n# Task\n\nFixture body.\n"


class RecordingWorker(MockWorker):
    """MockWorker that records every execute() call for invocation asserts."""

    def __init__(self):
        super().__init__()
        self.execute_calls = []

    def execute(self, request):
        self.execute_calls.append(request)
        return super().execute(request)


@contextmanager
def denied_tasks_listing(root: Path):
    """Deterministically make `.ai/tasks/` unlistable at the OS boundary.

    Only the exact resolved project tasks directory is denied; every other
    listing proceeds normally. This simulates an access failure without
    depending on chmod/EACCES behavior under root or containers.
    """
    real_listdir = os.listdir
    denied = str((root / ".ai" / "tasks").resolve())

    def selective(path, *args, **kwargs):
        try:
            candidate = str(Path(path).resolve())
        except OSError:
            candidate = str(path)
        if candidate == denied:
            raise PermissionError(13, "Permission denied")
        return real_listdir(path, *args, **kwargs)

    with patch("os.listdir", selective):
        yield


class TaskRefusalDiagnosticTests(unittest.TestCase):
    def setUp(self):
        pin_coherent_installation(self)
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-task-refusal-")
        self.root = Path(self.holder.name)
        initialize(self.root)

    def tearDown(self):
        self.holder.cleanup()

    def onboard(self):
        declarations = {
            "title": "Task refusal fixture",
            "purpose": "Exercise Task refusal diagnostics",
            "scope": "A bounded product-flow test project",
        }
        draft = onboard_project(self.root, declarations)
        self.assertIsNotNone(draft.proposal)
        result = onboard_project(
            self.root, declarations,
            build_authorizations(draft.proposal, "human:user",
                                 "2026-08-19T12:00:00+00:00"),
        )
        self.assertEqual(result.status, "ONBOARDED")

    def write_task(self, name: str, text: str) -> Path:
        tasks = self.root / ".ai" / "tasks"
        tasks.mkdir(exist_ok=True)
        path = tasks / name
        path.write_text(text, encoding="utf-8")
        return path

    # 1 -- no usable Task record (safe absence wording) ------------------------

    def test_no_task_records_refuses_and_points_to_manual_workflow(self):
        self.onboard()
        worker = RecordingWorker()
        result = run_instruction("Review this", self.root, adapter=worker)
        self.assertEqual(result.status, "REFUSED")
        self.assertEqual(result.code, TASK_REQUIRED)
        self.assertIn(".ai/tasks/TASK-YYYY-NNNN.md", result.message)
        self.assertIn(".ai/tasks/README.md", result.message)
        self.assertIn("No usable Task record was found", result.message)
        self.assertNotIn("No Task records exist", result.message)
        self.assertEqual(worker.execute_calls, [])

    # 2 -- one invalid (quarantined) Task --------------------------------------

    def test_invalid_created_by_is_named_with_file_and_reason(self):
        self.onboard()
        path = self.write_task(
            "TASK-2026-0001.md",
            render_task(minimal_task("TASK-2026-0001", created_by="Tabularium")))
        before = path.read_bytes()
        result = run_instruction("Review this", self.root)
        self.assertEqual(result.status, "REFUSED")
        self.assertEqual(result.code, TASK_REQUIRED)
        self.assertIn(".ai/tasks/TASK-2026-0001.md", result.message)
        self.assertIn("created_by", result.message)
        self.assertIn("USER/AGENT/SYSTEM", result.message)
        self.assertNotIn("No usable Task record was found", result.message)
        self.assertEqual(path.read_bytes(), before)
        again = run_instruction("Review this", self.root)
        self.assertEqual(again.message, result.message)

    # 3 -- one malformed Task ---------------------------------------------------

    def test_malformed_record_is_distinguished_from_absent(self):
        self.onboard()
        path = self.write_task(
            "TASK-2026-0001.md", "# just a body, no frontmatter\n")
        before = path.read_bytes()
        result = run_instruction("Review this", self.root)
        self.assertEqual(result.code, TASK_REQUIRED)
        self.assertNotIn("No usable Task record was found", result.message)
        self.assertIn(".ai/tasks/TASK-2026-0001.md", result.message)
        self.assertIn("frontmatter", result.message)
        self.assertEqual(path.read_bytes(), before)

    # 4 -- filename/id mismatch --------------------------------------------------

    def test_filename_id_mismatch_selects_no_identity(self):
        self.onboard()
        self.write_task("TASK-2026-0001.md",
                        render_task(minimal_task("TASK-2026-0002")))
        result = run_instruction("Review this", self.root)
        self.assertEqual(result.code, TASK_REQUIRED)
        self.assertIn(".ai/tasks/TASK-2026-0001.md", result.message)
        self.assertIn("disagree", result.message)

    # 5 -- duplicate/ambiguous identity ------------------------------------------

    def test_duplicate_identity_selects_no_winner(self):
        self.onboard()
        self.write_task("TASK-2026-0001.md",
                        render_task(minimal_task("TASK-2026-0001")))
        self.write_task("TASK-2026-0002.md",
                        render_task(minimal_task("TASK-2026-0001")))
        worker = RecordingWorker()
        result = run_instruction("Review this", self.root, adapter=worker)
        self.assertEqual(result.status, "REFUSED")
        self.assertEqual(result.code, TASK_REQUIRED)
        self.assertIn("multiple files", result.message)
        self.assertIn("no winner", result.message)
        self.assertEqual(worker.execute_calls, [])

    # 6 -- two valid active Tasks -------------------------------------------------

    def test_two_valid_active_tasks_still_require_selection(self):
        self.onboard()
        self.write_task("TASK-2026-0001.md",
                        render_task(minimal_task("TASK-2026-0001")))
        self.write_task("TASK-2026-0002.md",
                        render_task(minimal_task("TASK-2026-0002")))
        result = run_instruction("Review this", self.root)
        self.assertEqual(result.status, "REFUSED")
        self.assertEqual(result.code, TASK_SELECTION_REQUIRED)

    # 7 -- one valid Task plus one invalid sibling ---------------------------------

    def test_valid_task_selected_alongside_invalid_sibling(self):
        self.onboard()
        self.write_task("TASK-2026-0001.md",
                        render_task(minimal_task("TASK-2026-0001")))
        self.write_task("TASK-2026-9999.md",
                        render_task(minimal_task("TASK-2026-9999",
                                                 created_by="Tabularium")))
        result = run_instruction("Review this", self.root, adapter=MockWorker())
        self.assertNotIn(result.code, {TASK_REQUIRED,
                                       TASK_SELECTION_REQUIRED})
        if HAS_RUNTIME:
            self.assertEqual(result.status, "COMPLETED")
            self.assertEqual(result.message, "Work completed.")
            self.assertNotIn("TASK-2026-9999", result.message)
            self.assertNotIn("created_by", result.message)

    # 8 -- only terminal Task records ----------------------------------------------

    def test_terminal_only_records_are_distinguished_from_empty(self):
        self.onboard()
        self.write_task(
            "TASK-2026-0001.md",
            render_task(minimal_task("TASK-2026-0001", status="ARCHIVED",
                                     archived_outcome="COMPLETED")))
        result = run_instruction("Review this", self.root)
        self.assertEqual(result.code, TASK_REQUIRED)
        self.assertNotIn("No usable Task record was found", result.message)
        self.assertIn("terminal", result.message)

    # 9 -- status diagnostic surface unchanged ---------------------------------------

    def test_status_diagnostic_codes_unchanged_for_invalid_task(self):
        self.onboard()
        self.write_task(
            "TASK-2026-0001.md",
            render_task(minimal_task("TASK-2026-0001", created_by="Tabularium")))
        health = status_project(self.root).to_dict()
        self.assertIn("INVALID_RECORD_QUARANTINED",
                      health["compatibility"]["diagnostic_codes"])

    # discovery exception must not claim that no Tasks exist --------------------------

    def test_discovery_failure_does_not_claim_no_tasks_exist(self):
        self.onboard()
        self.write_task("TASK-2026-0001.md",
                        render_task(minimal_task("TASK-2026-0001")))
        with patch("tabularium.execution.discover",
                   side_effect=OSError("simulated discovery failure")):
            result = run_instruction("Review this", self.root)
        self.assertEqual(result.code, TASK_REQUIRED)
        self.assertIn("could not be inspected", result.message)
        self.assertNotIn("No Task records exist", result.message)
        self.assertNotIn("No usable Task record was found", result.message)
        self.assertNotIn("simulated discovery failure", result.message)

    # Refusal precedes any worker invocation -------------------------------------------

    def test_unusable_record_refusal_never_invokes_worker(self):
        self.onboard()
        self.write_task(
            "TASK-2026-0001.md",
            render_task(minimal_task("TASK-2026-0001", created_by="Tabularium")))
        worker = RecordingWorker()
        result = run_instruction("Review this", self.root, adapter=worker)
        self.assertEqual(result.code, TASK_REQUIRED)
        self.assertEqual(worker.execute_calls, [])

    # Detail-count truncation -----------------------------------------------------------

    def test_detail_list_is_bounded_and_ordering_is_stable(self):
        self.onboard()
        for n in range(1, _UNUSABLE_TASK_DETAIL_LIMIT + 3):
            tid = f"TASK-2026-{n:04d}"
            self.write_task(f"{tid}.md",
                            render_task(minimal_task(tid, created_by="Tabularium")))
        first = run_instruction("Review this", self.root)
        second = run_instruction("Review this", self.root)
        bullets = [ln for ln in first.message.splitlines()
                   if ln.startswith("- .ai/tasks/")]
        self.assertEqual(len(bullets), _UNUSABLE_TASK_DETAIL_LIMIT)
        self.assertIn("(and 2 more unusable Task records)", first.message)
        self.assertIn("TASK-2026-0001", bullets[0])
        self.assertIn("TASK-2026-0005", bullets[-1])
        self.assertEqual(first.message, second.message)

    # Terminal escape and newline injection --------------------------------------------

    def test_control_characters_in_record_are_sanitized(self):
        self.onboard()
        hostile_key = "a\x1b[2J\r\nStatus: completed"
        content = minimal_task("TASK-2026-0001")
        content[hostile_key] = "x"
        path = self.write_task("TASK-2026-0001.md", render_task(content))
        before = path.read_bytes()
        result = run_instruction("Review this", self.root)
        message = result.message
        self.assertEqual(result.code, TASK_REQUIRED)
        self.assertNotIn("\x1b", message)
        self.assertNotIn("\r", message)
        for line in message.splitlines():
            self.assertNotEqual(line.strip(), "Status: completed")
        # diagnosis survives as visibly escaped inline text
        self.assertIn("[2J", message)
        self.assertIn("Status: completed", message)
        self.assertIn("\\x1b", message)
        self.assertEqual(path.read_bytes(), before)

    # Oversized diagnostic reason is bounded -------------------------------------------

    def test_oversized_reason_is_bounded_and_deterministic(self):
        self.onboard()
        content = minimal_task("TASK-2026-0001",
                               schema_version="1." + "x" * 20000)
        path = self.write_task("TASK-2026-0001.md", render_task(content))
        before = path.read_bytes()
        result = run_instruction("Review this", self.root)
        again = run_instruction("Review this", self.root)
        self.assertEqual(result.code, TASK_REQUIRED)
        self.assertLess(len(result.message), 1000)
        self.assertIn("...", result.message)
        self.assertIn("schema_version", result.message)
        self.assertEqual(result.message, again.message)
        self.assertEqual(path.read_bytes(), before)

    # Unreadable task storage is never
    # interpreted as empty, missing, or ordinary absence -------------------------
    #
    # Platform note: pathlib's glob silently treats an unreadable directory as
    # empty, so the primary regression denies the directory listing at the
    # os.listdir boundary instead of relying on POSIX permission bits; the
    # chmod probe below stays gated to non-root POSIX environments.

    def test_unreadable_task_storage_is_not_reported_as_absence(self):
        self.onboard()
        with denied_tasks_listing(self.root):
            result = run_instruction("Review this", self.root)
        self.assertEqual(result.status, "REFUSED")
        self.assertEqual(result.code, TASK_REQUIRED)
        self.assertIn("Task storage is unreadable", result.message)
        self.assertNotIn("No usable Task record was found", result.message)
        self.assertNotIn("No Task records exist", result.message)

    def test_unreadable_task_storage_refusal_is_content_independent(self):
        self.onboard()
        self.write_task("TASK-2026-0001.md",
                        render_task(minimal_task("TASK-2026-0001")))
        worker = RecordingWorker()
        with denied_tasks_listing(self.root):
            result = run_instruction("Review this", self.root, adapter=worker)
        self.assertEqual(result.code, TASK_REQUIRED)
        self.assertIn("Task storage is unreadable", result.message)
        self.assertNotIn("No usable Task record was found", result.message)
        self.assertEqual(worker.execute_calls, [])

    def test_unreadable_refusal_is_bounded_and_omits_error_detail(self):
        self.onboard()
        with denied_tasks_listing(self.root):
            result = run_instruction("Review this", self.root)
        self.assertLess(len(result.message), 1000)
        self.assertNotIn("[Errno", result.message)
        self.assertNotIn("PermissionError", result.message)

    def test_status_reports_unreadable_storage_and_stays_inspectable(self):
        self.onboard()
        with denied_tasks_listing(self.root):
            health = status_project(self.root)
        details = health.to_dict()
        self.assertIn("RECORD_DIRECTORY_UNREADABLE",
                      details["compatibility"]["diagnostic_codes"])
        self.assertEqual(health.overall_state, READY)
        self.assertEqual(health.task_selection,
                         "task storage unreadable — see diagnostics")
        self.assertTrue(any(".ai/tasks/" in step and "read access" in step
                            for step in health.next_steps))

    def test_readable_but_empty_storage_keeps_ordinary_absence_wording(self):
        self.onboard()
        result = run_instruction("Review this", self.root)
        self.assertEqual(result.code, TASK_REQUIRED)
        self.assertIn("No usable Task record was found", result.message)
        self.assertNotIn("unreadable", result.message)

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0,
        "requires POSIX permissions and a non-root effective user")
    def test_unreadable_tasks_directory_does_not_claim_absence(self):
        self.onboard()
        self.write_task("TASK-2026-0001.md",
                        render_task(minimal_task("TASK-2026-0001")))
        tasks = self.root / ".ai" / "tasks"
        os.chmod(tasks, 0o000)
        try:
            result = run_instruction("Review this", self.root)
        finally:
            os.chmod(tasks, 0o755)
        self.assertEqual(result.code, TASK_REQUIRED)
        self.assertIn("Task storage is unreadable", result.message)
        self.assertNotIn("No usable Task record was found", result.message)
        self.assertNotIn("No Task records exist", result.message)


    # D1 -- explicit --task against unreadable storage stays distinguishable
    # from an id that resolves to nothing, under the SAME stable product code
    # CONTEXT_PACKAGE_NOT_READY ------------------------------------------------

    def test_explicit_task_unreadable_storage_names_storage_and_fails_closed(self):
        self.onboard()
        self.write_task("TASK-2026-0001.md",
                        render_task(minimal_task("TASK-2026-0001",
                                                 status="IN_PROGRESS")))
        worker = RecordingWorker()
        with denied_tasks_listing(self.root):
            result = run_instruction("Review this", self.root,
                                     task_id="TASK-2026-0001",
                                     adapter=worker)
        self.assertEqual(result.status, "PREPARATION_REJECTED")
        self.assertEqual(result.code, "CONTEXT_PACKAGE_NOT_READY")
        self.assertIn("Task storage", result.message)
        self.assertIn("unreadable", result.message)
        self.assertIn(".ai/tasks", result.message)
        self.assertIn("Restore read access", result.message)
        self.assertNotIn("No usable Task record was found", result.message)
        self.assertNotIn("No Task records exist", result.message)
        self.assertNotIn("[Errno", result.message)
        self.assertNotIn("PermissionError", result.message)
        self.assertLess(len(result.message), 1000)
        self.assertEqual(worker.execute_calls, [])

    def test_explicit_unknown_task_keeps_resolve_guidance_without_absence_claim(self):
        self.onboard()
        self.write_task("TASK-2026-0001.md",
                        render_task(minimal_task("TASK-2026-0001",
                                                 status="IN_PROGRESS")))
        worker = RecordingWorker()
        result = run_instruction("Review this", self.root,
                                 task_id="TASK-2026-9999", adapter=worker)
        again = run_instruction("Review this", self.root,
                                task_id="TASK-2026-9999", adapter=worker)
        self.assertEqual(result.status, "PREPARATION_REJECTED")
        self.assertEqual(result.code, "CONTEXT_PACKAGE_NOT_READY")
        self.assertNotIn("unreadable", result.message)
        self.assertIn("--task", result.message)
        self.assertIn("resolve", result.message)
        self.assertNotIn("No usable Task record was found", result.message)
        self.assertEqual(result.message, again.message)
        self.assertEqual(worker.execute_calls, [])

    def test_explicit_task_unreadable_refusal_is_deterministic(self):
        self.onboard()
        self.write_task("TASK-2026-0001.md",
                        render_task(minimal_task("TASK-2026-0001",
                                                 status="IN_PROGRESS")))
        with denied_tasks_listing(self.root):
            first = run_instruction("Review this", self.root,
                                    task_id="TASK-2026-0001")
            second = run_instruction("Review this", self.root,
                                     task_id="TASK-2026-0001")
        self.assertEqual(first.code, second.code)
        self.assertEqual(first.message, second.message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
