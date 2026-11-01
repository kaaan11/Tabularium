"""Phase I (I3) contract, integration, and parity tests for Task read
surfaces (`tabu task list|show|validate`).

Pins:
- reuse of the production loader only (states mirror repository usability);
- stable machine contracts (--json is the whole stdout payload, reason codes,
  exit classes: 0 valid/success, 1 refused/invalid, 2 usage/path class);
- human/JSON factual parity;
- read-only behavior of every surface.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from tabularium import task as T
from tabularium.cli import build_parser, main
from tabularium.init import initialize
from tabularium.onboarding import build_authorizations, onboard_project

FIXTURE_PATHS: list[str] = []


def minimal_task(record_id, **overrides):
    content = {
        "schema_version": "1.10", "id": record_id,
        "title": f"Title {record_id}", "objective": "o",
        "status": "IN_PROGRESS", "created_at": "2026-01-01T00:00:00Z",
        "created_by": "USER",
    }
    content.update(overrides)
    return content


def write_task(task_dir: Path, filename: str, frontmatter, body="# b\n"):
    text = "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n"
    text += body
    (task_dir / filename).write_text(text, encoding="utf-8")


class TaskSurfaceFixture(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-i3-")
        self.root = Path(self.holder.name)
        initialize(self.root)
        declarations = {
            "title": "I3 fixture", "purpose": "Task surfaces",
            "scope": "Bounded task-surface project",
        }
        draft = onboard_project(self.root, declarations)
        result = onboard_project(
            self.root, declarations,
            build_authorizations(draft.proposal, "human:user",
                                 "2026-01-01T00:00:00+00:00"))
        self.assertEqual(result.status, "ONBOARDED")
        self.task_dir = self.root / ".ai" / "tasks"
        self.task_dir.mkdir(exist_ok=True)
        FIXTURE_PATHS.clear()
        FIXTURE_PATHS.append(str(self.root))

    def tearDown(self):
        self.holder.cleanup()

    @staticmethod
    def project_snapshot(root: Path) -> dict:
        return {str(p.relative_to(root)): p.read_bytes()
                for p in sorted((root / ".ai").rglob("*")) if p.is_file()}


class TaskListTests(TaskSurfaceFixture):
    def test_empty_store_lists_zero_and_exits_zero(self):
        result = T.task_list(self.root)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.total_count, 0)

    def test_states_are_repository_usability_facts_in_deterministic_order(self):
        write_task(self.task_dir, "TASK-2026-0002.md",
                   minimal_task("TASK-2026-0002"))
        write_task(self.task_dir, "TASK-2026-0001.md",
                   minimal_task("TASK-2026-0001"))
        write_task(self.task_dir, "TASK-2026-0003.md",
                   minimal_task("TASK-2026-0003", status="COMPLETED"))
        write_task(self.task_dir, "TASK-26-bad.md",
                   {"schema_version": "1.10"}, "# x\n")
        write_task(self.task_dir, "TASK-2026-0004.md",
                   minimal_task("TASK-2026-0004", priority="WHENEVER"))
        result = T.task_list(self.root)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.total_count, 5)
        by_id = {t.id or f"file:{t.file}": t for t in result.tasks}
        self.assertEqual(by_id["TASK-2026-0001"].state, T.ACTIVE)
        self.assertEqual(by_id["TASK-2026-0002"].state, T.ACTIVE)
        self.assertEqual(by_id["TASK-2026-0003"].state, T.TERMINAL)
        self.assertEqual(by_id["TASK-2026-0004"].state, T.UNUSABLE)
        self.assertIsNotNone(by_id["TASK-2026-0004"].reason)
        unnamed = [t for t in result.tasks if t.id is None]
        self.assertEqual(len(unnamed), 1)
        self.assertEqual(unnamed[0].state, T.UNUSABLE)
        # The loader sorts by declared id, else filename stem.
        keys = [(t.id or Path(t.file).stem) for t in result.tasks]
        self.assertEqual(keys, sorted(keys))

    def test_unreadable_storage_refuses_with_loader_code(self):
        write_task(self.task_dir, "TASK-2026-0001.md",
                   minimal_task("TASK-2026-0001"))
        self.task_dir.chmod(0o000)
        try:
            result = T.task_list(self.root)
        finally:
            self.task_dir.chmod(0o755)
        self.assertEqual(result.code, T.RECORD_DIRECTORY_UNREADABLE)
        self.assertEqual(result.exit_code, 1)

    def test_listing_is_bounded_with_explicit_remainder(self):
        for n in range(1, T._LIST_LIMIT + 6):
            write_task(self.task_dir, f"TASK-2026-{n:04d}.md",
                       minimal_task(f"TASK-2026-{n:04d}"))
        result = T.task_list(self.root)
        self.assertEqual(result.total_count, T._LIST_LIMIT + 5)
        self.assertEqual(len(result.tasks), T._LIST_LIMIT)
        self.assertEqual(result.omitted_count, 5)
        self.assertIn("(and 5 more Task records)", T.render_human(result))

    def test_invalid_target_is_usage_class(self):
        result = T.task_list(self.root / "missing")
        self.assertEqual(result.exit_code, 2)


class TaskShowTests(TaskSurfaceFixture):
    def test_valid_record_shows_loader_extracted_content(self):
        body = "# Task\n\nThe narrative body.\n"
        write_task(self.task_dir, "TASK-2026-0007.md",
                   minimal_task("TASK-2026-0007"), body=body)
        result = T.task_show(self.root, "TASK-2026-0007")
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.found and result.valid)
        self.assertEqual(result.title, "Title TASK-2026-0007")
        self.assertEqual(result.status, "IN_PROGRESS")
        self.assertEqual(result.frontmatter["id"], "TASK-2026-0007")
        self.assertEqual(result.body, body)
        human = T.render_human_show(result)
        self.assertIn("Valid: yes", human)
        self.assertIn("The narrative body.", human)

    def test_not_found_is_a_stable_refusal(self):
        result = T.task_show(self.root, "TASK-2026-9999")
        self.assertEqual(result.code, T.TASK_NOT_FOUND)
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.found)

    def test_non_canonical_id_is_usage_class(self):
        result = T.task_show(self.root, "not-a-task-id")
        self.assertEqual(result.code, T.TASK_ID_INVALID)
        self.assertEqual(result.exit_code, 2)

    def test_duplicate_declared_ids_are_ambiguous_never_chosen(self):
        write_task(self.task_dir, "TASK-2026-0010.md",
                   minimal_task("TASK-2026-0010"))
        write_task(self.task_dir, "TASK-2026-0011.md",
                   minimal_task("TASK-2026-0010"))
        result = T.task_show(self.root, "TASK-2026-0010")
        self.assertEqual(result.code, T.RESOLUTION_AMBIGUOUS)
        self.assertEqual(result.exit_code, 1)

    def test_malformed_record_is_found_but_invalid(self):
        (self.task_dir / "TASK-2026-0012.md").write_text(
            "no frontmatter here\n", encoding="utf-8")
        result = T.task_show(self.root, "TASK-2026-0012")
        self.assertTrue(result.found)
        self.assertFalse(result.valid)
        self.assertIn(T.MALFORMED_RECORD_QUARANTINED, result.error_codes)
        self.assertIsNone(result.body)
        self.assertEqual(result.exit_code, 1)

    def test_id_mismatch_is_reported_from_loader_evidence(self):
        write_task(self.task_dir, "TASK-2026-0013.md",
                   minimal_task("TASK-2026-7777"))
        result = T.task_show(self.root, "TASK-2026-0013")
        self.assertFalse(result.valid)
        self.assertIn(T.FILENAME_ID_MISMATCH, result.error_codes)
        self.assertTrue(any("disagree" in e for e in result.errors))


class TaskValidateTests(TaskSurfaceFixture):
    def test_targeted_valid_record_passes(self):
        write_task(self.task_dir, "TASK-2026-0001.md",
                   minimal_task("TASK-2026-0001"))
        result = T.task_validate(self.root, "TASK-2026-0001")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.valid_count, 1)
        self.assertEqual(result.invalid_count, 0)

    def test_targeted_invalid_record_reports_loader_errors(self):
        write_task(self.task_dir, "TASK-2026-0002.md",
                   minimal_task("TASK-2026-0002", risk="EXTREME"))
        result = T.task_validate(self.root, "TASK-2026-0002")
        self.assertEqual(result.exit_code, 1)
        entry = result.entries[0]
        self.assertFalse(entry.valid)
        self.assertTrue(any(e.startswith("risk is not one of ")
                            for e in entry.errors))
        self.assertEqual(entry.error_codes, (T.INVALID_RECORD_QUARANTINED,))

    def test_all_mode_counts_every_discovered_record(self):
        write_task(self.task_dir, "TASK-2026-0001.md",
                   minimal_task("TASK-2026-0001"))
        write_task(self.task_dir, "TASK-2026-0002.md",
                   minimal_task("TASK-2026-0002"))
        (self.task_dir / "broken.md").write_text("---\nid: 5\n---\n",
                                                 encoding="utf-8")
        result = T.task_validate(self.root)
        self.assertEqual(result.total_count, 3)
        self.assertEqual(result.valid_count, 2)
        self.assertEqual(result.invalid_count, 1)
        self.assertEqual(result.exit_code, 1)

    def test_empty_store_validates_to_zero_records_exit_zero(self):
        result = T.task_validate(self.root)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.total_count, 0)
        self.assertIn("No Task records", T.render_human_validate(result))

    def test_unreadable_storage_refuses_validation(self):
        self.task_dir.chmod(0o000)
        try:
            result = T.task_validate(self.root)
        finally:
            self.task_dir.chmod(0o755)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.code, T.RECORD_DIRECTORY_UNREADABLE)

    def test_bad_id_syntax_is_usage_class(self):
        result = T.task_validate(self.root, "TASK-20-1")
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.code, T.TASK_ID_INVALID)


class JsonContractAndParityTests(TaskSurfaceFixture):
    """Policy 14/15: --json is the entire stdout payload and carries the same
    stable fact set as the human rendering."""

    def setUp(self):
        super().setUp()
        write_task(self.task_dir, "TASK-2026-0001.md",
                   minimal_task("TASK-2026-0001"))
        write_task(self.task_dir, "TASK-2026-0002.md",
                   minimal_task("TASK-2026-0002", status="COMPLETED"))

    @staticmethod
    def run_main(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_json_is_the_whole_stdout_payload_for_every_surface(self):
        for argv in (
            ["task", "list", str(self.root), "--json"],
            ["task", "show", "TASK-2026-0001", str(self.root), "--json"],
            ["task", "validate", str(self.root), "--json"],
        ):
            code, out, err = self.run_main(argv)
            self.assertEqual(code, 0, argv)
            parsed = json.loads(out)  # raises unless stdout is one JSON value
            self.assertIsInstance(parsed, dict)
            self.assertEqual(parsed["exit_code"], 0)
            self.assertNotIn("Tabularium", out.split('"')[0])

    def test_human_json_parity_on_list(self):
        result = T.task_list(self.root)
        human = T.render_human(result)
        machine = json.loads(T.render_json(result))
        for task in result.tasks:
            if task.id:
                self.assertIn(task.id, human)
            self.assertIn(task.to_dict()["file"], machine["files"] if False else
                          [t["file"] for t in machine["tasks"]])
        self.assertEqual(machine["total_count"], result.total_count)
        states_json = sorted(t["state"] for t in machine["tasks"])
        self.assertEqual(states_json,
                         sorted(t.state for t in result.tasks))

    def test_human_json_parity_on_show(self):
        result = T.task_show(self.root, "TASK-2026-0001")
        human = T.render_human_show(result)
        machine = json.loads(T.render_json_show(result))
        self.assertEqual(machine["id"], result.id)
        self.assertEqual(machine["valid"], result.valid)
        self.assertEqual(machine["body"], result.body)
        self.assertIn(str(machine["id"]), human)
        self.assertIn(machine["body"].strip().splitlines()[0], human)

    def test_human_json_parity_on_validate(self):
        write_task(self.task_dir, "TASK-2026-0009.md",
                   minimal_task("TASK-2026-0009", type="WHIM"))
        result = T.task_validate(self.root)
        human = T.render_human_validate(result)
        machine = json.loads(T.render_json_validate(result))
        self.assertEqual(machine["invalid_count"], 1)
        self.assertIn("[FAIL]", human)
        failing = next(e for e in result.entries if not e.valid)
        self.assertIn(failing.id, human)
        self.assertEqual(
            [e["id"] for e in machine["entries"] if not e["valid"]],
            [failing.id])
        self.assertEqual(machine["exit_code"], 1)

    def test_failure_routes_human_output_to_stderr_json_to_stdout(self):
        code, out, err = self.run_main(
            ["task", "show", "TASK-2026-9999", str(self.root)])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("TASK_NOT_FOUND", err)
        code, out, err = self.run_main(
            ["task", "show", "TASK-2026-9999", str(self.root), "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out)["code"], "TASK_NOT_FOUND")

    def test_surfaces_are_read_only(self):
        before = self.project_snapshot(self.root)
        for argv in (
            ["task", "list", str(self.root)],
            ["task", "show", "TASK-2026-0001", str(self.root)],
            ["task", "validate", str(self.root)],
        ):
            code, _, _ = self.run_main(argv + ["--json"])
            self.assertEqual(code, 0, argv)
        self.assertEqual(self.project_snapshot(self.root), before)


class ParserContractTests(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-i3-cli-")
        FIXTURE_PATHS.clear()
        FIXTURE_PATHS.append(self.holder.name)

    def tearDown(self):
        self.holder.cleanup()

    def test_task_requires_subcommand(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                main(["task"])
        self.assertIn("subcommand", err.getvalue())

    def test_validate_id_is_optional_show_requires_it(self):
        parser = build_parser()
        args = parser.parse_args(["task", "validate", "--json"])
        self.assertIsNone(args.task_id)
        args = parser.parse_args(["task", "show", "TASK-2026-0001", "."])
        self.assertEqual(args.task_id, "TASK-2026-0001")

    def test_lone_non_id_positional_is_the_project_directory(self):
        code, out, _ = JsonContractAndParityTests.run_main(
            ["task", "validate", str(FIXTURE_PATHS[0]), "--json"])
        self.assertEqual(code, 0)
        parsed = json.loads(out)
        self.assertIsNone(parsed["requested_id"])
        self.assertIn(str(FIXTURE_PATHS[0]), parsed["target"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
