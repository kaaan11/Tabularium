"""Phase I (I5) task create contract tests.

Pins the approved behavior chain: validate project/Task storage -> allocate
id -> construct canonical Task -> validate using the existing validator ->
write atomically -> verify -> report. Nothing partially valid is ever
written; collisions never overwrite; --print performs no authoritative
write and reserves no id; JSON carries the same facts as human output.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import yaml

from tabularium import task as T
from tabularium.cli import main
from tabularium.execution import _choose_task
from tabularium.init import initialize
from tabularium.onboarding import build_authorizations, onboard_project
from tabularium.engine.context_loader.confinement import ProjectScope
from tabularium.engine.context_loader.discovery import discover


def current_year() -> int:
    return datetime.now(timezone.utc).year


class TaskCreateFixture(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-i5-")
        self.root = Path(self.holder.name)
        initialize(self.root)
        declarations = {
            "title": "I5 fixture", "purpose": "task create",
            "scope": "Bounded create project",
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
    def ai_snapshot(root: Path) -> dict:
        return {str(p.relative_to(root)): p.read_bytes()
                for p in sorted((root / ".ai").rglob("*")) if p.is_file()}

    @staticmethod
    def run_main(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()


class NormalCreateTests(TaskCreateFixture):
    def test_create_writes_one_valid_canonical_record(self):
        result = T.task_create(self.root, title="Write the report",
                               objective="Produce the quarterly report",
                               priority="HIGH")
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.created)
        self.assertEqual(result.record_id,
                         f"TASK-{current_year():04d}-0001")
        self.assertEqual(result.file,
                         f".ai/tasks/TASK-{current_year():04d}-0001.md")
        path = self.root / result.file
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        fm = yaml.safe_load(text.split("\n---\n", 1)[0])
        self.assertEqual(fm["schema_version"], "1.10")
        self.assertEqual(fm["status"], "INBOX")
        self.assertEqual(fm["created_by"], "USER")
        self.assertEqual(fm["priority"], "HIGH")
        self.assertNotIn("approval_status", fm)
        self.assertNotIn("approved_by", fm)
        # Only supplied optional fields are present; omission is not default.
        self.assertNotIn("risk", fm)
        self.assertNotIn("plan", fm)

    def test_created_record_is_usable_to_the_production_loader(self):
        result = T.task_create(self.root, title="T", objective="o")
        repo = discover(self.root, ProjectScope(self.root))
        records = [r for r in repo.family_records("Task") if r.usable]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].declared_id, result.record_id)
        self.assertTrue(repo.record_usable(records[0]))

    def test_created_task_is_selected_by_run_task_selection(self):
        first = T.task_create(self.root, title="T1", objective="o1")
        chosen, error = _choose_task(self.root, None)
        self.assertIsNone(error)
        self.assertEqual(chosen, first.record_id)

    def test_optional_fields_land_in_frontmatter_when_supplied(self):
        T.task_create(self.root, title="T", objective="o", type_="RESEARCH",
                      risk="MEDIUM", uncertainty_level="HIGH",
                      plan="Investigate first.")
        text = next((self.task_dir).glob("TASK-*.md")).read_text(
            encoding="utf-8")
        fm = yaml.safe_load(text.split("\n---\n", 1)[0])
        self.assertEqual(fm["type"], "RESEARCH")
        self.assertEqual(fm["risk"], "MEDIUM")
        self.assertEqual(fm["uncertainty_level"], "HIGH")
        self.assertEqual(fm["plan"], "Investigate first.")

    def test_sequential_creates_allocate_distinct_ids(self):
        first = T.task_create(self.root, title="A", objective="a")
        second = T.task_create(self.root, title="B", objective="b")
        third = T.task_create(self.root, title="C", objective="c")
        ids = [first.record_id, second.record_id, third.record_id]
        self.assertEqual(len(set(ids)), 3)


class CreateRefusalTests(TaskCreateFixture):
    def test_invalid_enum_refuses_before_any_write_with_loader_evidence(self):
        snapshot = self.ai_snapshot(self.root)
        result = T.task_create(self.root, title="T", objective="o",
                               priority="WHENEVER")
        self.assertEqual(result.code, T.INVALID_RECORD_QUARANTINED)
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.created)
        self.assertTrue(any(e.startswith("priority is not one of ")
                            for e in result.validation_errors))
        self.assertEqual(self.ai_snapshot(self.root), snapshot)
        self.assertEqual([p.name for p in self.task_dir.iterdir()],
                         ["README.md"])

    def test_whitespace_only_required_field_refuses_without_write(self):
        snapshot = self.ai_snapshot(self.root)
        result = T.task_create(self.root, title="   ", objective="o")
        self.assertEqual(result.exit_code, 1)
        self.assertTrue(any("title is required" in e
                            for e in result.validation_errors))
        self.assertEqual(self.ai_snapshot(self.root), snapshot)

    def test_uninitialized_project_refuses(self):
        import shutil
        bare = Path(tempfile.mkdtemp(prefix="tabu-i5-bare-"))
        self.addCleanup(shutil.rmtree, bare, ignore_errors=True)
        result = T.task_create(bare, title="T", objective="o")
        self.assertEqual(result.code, "NOT_INITIALIZED")
        self.assertEqual(result.exit_code, 1)

    def test_missing_target_is_usage_class(self):
        result = T.task_create(self.root / "missing", title="T",
                               objective="o")
        self.assertEqual(result.exit_code, 2)

    def test_unreadable_storage_refuses_allocation(self):
        self.task_dir.chmod(0o000)
        try:
            result = T.task_create(self.root, title="T", objective="o")
        finally:
            self.task_dir.chmod(0o755)
        self.assertEqual(result.code, T.RECORD_DIRECTORY_UNREADABLE)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual([p.name for p in self.task_dir.iterdir()],
                         ["README.md"])


class CollisionTests(TaskCreateFixture):
    def test_allocator_race_fact_refuses_creation(self):
        # A deterministic single-process scan cannot see its own candidate,
        # so simulate another creator winning the race between scan and
        # check: the allocator surfaces the collision fact and creation
        # refuses without writing anything.
        candidate = f"TASK-{current_year():04d}-0001.md"
        real_exists = Path.exists

        def raced(path):
            if path.name == candidate:
                return True
            return real_exists(path)

        with mock.patch.object(Path, "exists", raced):
            result = T.task_create(self.root, title="T", objective="o")
        self.assertEqual(result.code, T.TASK_ID_COLLISION)
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.created)
        self.assertEqual([p.name for p in self.task_dir.iterdir()],
                         ["README.md"])

    def test_publish_never_overwrites_an_existing_canonical_name(self):
        """Mutation M3 killer: even when allocation somehow names an existing
        record, publication must refuse instead of replacing its bytes."""
        existing_name = f"TASK-{current_year():04d}-0001.md"
        existing = self.task_dir / existing_name
        existing.write_text("---\nid: precious\n---\n# keep\n",
                            encoding="utf-8")
        before = existing.read_bytes()

        def forced_allocation(raw_target, **kwargs):
            return T.TaskIdAllocation(record_id=
                                      f"TASK-{current_year():04d}-0001")

        with mock.patch.object(T, "allocate_task_id", forced_allocation):
            result = T.task_create(self.root, title="T", objective="o")
        self.assertEqual(result.code, T.TASK_ID_COLLISION)
        self.assertEqual(existing.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.task_dir.iterdir()),
                         ["README.md", existing_name])


class PrintModeTests(TaskCreateFixture):
    def test_print_is_completely_write_free_and_repeatable(self):
        before = self.ai_snapshot(self.root)
        first = T.task_create(self.root, title="P", objective="p",
                              print_only=True)
        second = T.task_create(self.root, title="P", objective="p",
                               print_only=True)
        self.assertEqual(self.ai_snapshot(self.root), before)
        self.assertTrue(first.print_only and not first.created)
        self.assertEqual(first.record_id, second.record_id)
        self.assertIsNotNone(first.content)
        self.assertIn("NOT reserved", first.message or "")
        # The printed document validates on its own merits.
        rec, errors = T._parse_document(first.content,
                                        filename=f"{first.record_id}.md",
                                        project_root=self.root)
        self.assertTrue(rec.usable)
        self.assertEqual(errors, [])

    def test_print_does_not_reserve_the_candidate_id(self):
        printed = T.task_create(self.root, title="P", objective="p",
                                print_only=True)
        created = T.task_create(self.root, title="C", objective="c")
        self.assertEqual(printed.record_id, created.record_id)

    def test_print_reports_invalid_candidates_without_writing(self):
        before = self.ai_snapshot(self.root)
        result = T.task_create(self.root, title="P", objective="p",
                               priority="WHENEVER", print_only=True)
        self.assertEqual(result.code, T.INVALID_RECORD_QUARANTINED)
        self.assertIsNone(result.content)
        self.assertEqual(self.ai_snapshot(self.root), before)


class JsonParityAndSafetyTests(TaskCreateFixture):
    def test_json_is_the_whole_stdout_payload_with_matching_facts(self):
        code, out, err = self.run_main(
            ["task", "create", "--title", "J", "--objective", "j",
             str(self.root), "--json"])
        self.assertEqual(code, 0)
        parsed = json.loads(out)
        self.assertEqual(parsed["command"], "task_create")
        self.assertTrue(parsed["created"])
        self.assertTrue((self.root / parsed["file"]).exists())
        self.assertEqual(err, "")

    def test_hostile_title_is_stored_verbatim_but_rendered_safely(self):
        hostile = "line1\n\x1b[31mRED\u202etext"
        result = T.task_create(self.root, title=hostile, objective="o")
        self.assertEqual(result.exit_code, 0)
        # The record's semantic content keeps exactly what the operator
        # supplied: a production round trip restores the hostile string.
        stored = next(self.task_dir.glob("TASK-*.md"))
        rec, errors = T._parse_document(
            stored.read_text(encoding="utf-8"),
            filename=stored.name, project_root=self.root)
        self.assertEqual(errors, [])
        self.assertEqual(rec.frontmatter["title"], hostile)
        # The human rendering is bounded and control-sequence free.
        listing = T.render_human(T.task_list(self.root))
        self.assertNotIn("\x1b[31m", listing)
        self.assertNotIn("line1\n\x1b", listing)
        wire = T.render_json(T.task_list(self.root))
        # On the wire the control byte appears only escaped, never raw.
        self.assertNotIn(chr(27), wire)
        self.assertIn("\\u001b", wire)

    def test_approval_provenance_is_not_creatable_through_the_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                main(["task", "create", "--title", "T", "--objective", "o",
                      "--approval-status", "APPROVED", str(self.root)])
        self.assertIn("unrecognized arguments", err.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
