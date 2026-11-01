"""M7.3 read-only status and doctor product-surface tests."""

from __future__ import annotations

import contextlib
import io
import json
import re
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tabularium.cli import main
from tabularium.diagnostics import (VERSION_MISMATCH, diagnose, render_human,
                              render_json)
from tabularium.init import initialize
from tabularium.onboarding import build_authorizations, onboard_project
from tests.environment_support import pin_coherent_installation


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        pin_coherent_installation(self)

    def make_project(self, onboard: bool = False) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        holder = tempfile.TemporaryDirectory(prefix="tabu-m73-")
        root = Path(holder.name)
        initialize(root)
        if onboard:
            declarations = {
                "title": "Diagnostic fixture",
                "purpose": "Exercise status and doctor",
                "scope": "The diagnostic fixture",
            }
            draft = onboard_project(root, declarations)
            self.assertIsNotNone(draft.proposal)
            onboard_project(
                root,
                declarations,
                build_authorizations(draft.proposal, "alex", "2026-08-19T12:00:00+00:00"),
            )
        return holder, root

    @staticmethod
    def tree_snapshot(root: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def invoke(self, args: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_status_and_doctor_valid_project_are_read_only(self):
        holder, root = self.make_project(onboard=True)
        self.addCleanup(holder.cleanup)
        before = self.tree_snapshot(root)
        for command in ("status", "doctor"):
            for suffix in ([], ["--json"]):
                with self.subTest(command=command, suffix=suffix):
                    code, stdout, stderr = self.invoke([command, str(root), *suffix])
                    self.assertEqual(code, 0)
                    self.assertIn("READY", stdout)
                    self.assertEqual(stderr, "")
                    self.assertEqual(before, self.tree_snapshot(root))

    def test_json_modes_share_health_result_and_are_bounded(self):
        holder, root = self.make_project(onboard=True)
        self.addCleanup(holder.cleanup)
        status = diagnose(root, "status")
        doctor = diagnose(root, "doctor")
        status_json = json.loads(render_json(status))
        doctor_json = json.loads(render_json(doctor))
        self.assertEqual(status_json["overall_state"], "READY")
        self.assertEqual(doctor_json["overall_state"], "READY")
        self.assertEqual(status_json["target"], doctor_json["target"])
        self.assertNotIn("project.md", json.dumps(status_json["foundations"]))
        self.assertNotIn("content", json.dumps(status_json).lower())

    def test_json_exposes_version_origin_not_local_source_path(self):
        holder, root = self.make_project(onboard=True)
        self.addCleanup(holder.cleanup)
        source_root = str(Path(__file__).resolve().parents[1])
        for command in ("status", "doctor"):
            result = diagnose(root, command)
            payload = json.loads(render_json(result))
            serialized = json.dumps(payload)
            self.assertNotIn("source_version_path", serialized)
            self.assertNotIn(source_root, serialized)
            self.assertIn(payload["compatibility"]["source_version_origin"], {
                "repository", "package_metadata"
            })
            self.assertNotIn(source_root, render_human(result))

    def test_not_initialized_and_partial_states_are_controlled(self):
        holder = tempfile.TemporaryDirectory(prefix="tabu-m73-empty-")
        empty = Path(holder.name)
        self.addCleanup(holder.cleanup)
        empty_result = diagnose(empty, "status")
        self.assertEqual(empty_result.overall_state, "NOT_INITIALIZED")
        self.assertEqual(empty_result.exit_code, 1)
        initialize(empty)
        (empty / ".ai" / "constraints.md").unlink()
        partial = diagnose(empty, "doctor")
        self.assertEqual(partial.overall_state, "PARTIAL_INITIALIZATION")
        self.assertEqual(partial.exit_code, 1)
        self.assertTrue(all(check.status != "FAIL" or check.reason_code in {
            "PARTIAL_INITIALIZATION", "PROJECT_NOT_INITIALIZED"
        } for check in partial.checks))

    def test_malformed_foundation_is_diagnosed_without_repair(self):
        holder, root = self.make_project(onboard=True)
        self.addCleanup(holder.cleanup)
        secret = "synthetic-api-key-should-not-appear"
        (root / ".ai" / "project.md").write_text(secret, encoding="utf-8")
        before = self.tree_snapshot(root)
        code, stdout, stderr = self.invoke(["doctor", str(root), "--json"])
        self.assertEqual(code, 1)
        payload = json.loads(stdout or stderr)
        self.assertEqual(payload["overall_state"], "INVALID_PROJECT")
        self.assertIn("FOUNDATION_INVALID_STRUCTURE", json.dumps(payload))
        self.assertNotIn(secret, stdout + stderr)
        self.assertEqual(before, self.tree_snapshot(root))

    def test_version_mismatch_is_explicit(self):
        holder, root = self.make_project()
        self.addCleanup(holder.cleanup)
        (root / ".ai" / "manifest.yaml").write_text(
            'manifest_version: "1.0"\ntabularium_ruleset: "9.9.9"\n', encoding="utf-8"
        )
        result = diagnose(root, "status")
        self.assertEqual(result.overall_state, "VERSION_MISMATCH")
        self.assertTrue(any(check.reason_code == "VERSION_MISMATCH" for check in result.checks))

    # Human output exposes the already-trusted evidence ---------------------------------

    def test_partial_diagnosis_names_the_missing_artifact(self):
        holder = tempfile.TemporaryDirectory(prefix="tabu-m73-partial-")
        root = Path(holder.name)
        self.addCleanup(holder.cleanup)
        initialize(root)
        (root / ".ai" / "constraints.md").unlink()
        result = diagnose(root, "doctor")
        self.assertEqual(result.overall_state, "PARTIAL_INITIALIZATION")
        self.assertEqual(result.exit_code, 1)
        human = render_human(result)
        self.assertIn(".ai/constraints.md", human)
        payload = json.loads(render_json(result))
        self.assertIn("PARTIAL_INITIALIZATION", json.dumps(payload["checks"]))

    def test_version_mismatch_human_output_shows_ruleset_values(self):
        holder, root = self.make_project()
        self.addCleanup(holder.cleanup)
        (root / ".ai" / "manifest.yaml").write_text(
            'manifest_version: "1.0"\ntabularium_ruleset: "9.9.9"\n', encoding="utf-8"
        )
        result = diagnose(root, "status")
        self.assertEqual(result.overall_state, "VERSION_MISMATCH")
        required = result.compatibility.get("required_ruleset")
        available = result.compatibility.get("available_ruleset")
        self.assertEqual(required, "9.9.9")
        self.assertTrue(available)
        human = render_human(result)
        self.assertIn("9.9.9", human)
        self.assertIn(str(available), human)
        # the machine-readable shape is untouched by the human-only line
        payload = json.loads(render_json(result))
        self.assertEqual(
            set(payload),
            {"command", "tabularium_version", "target", "overall_state",
             "initialization", "onboarding", "loader", "foundations",
             "project", "compatibility", "capabilities", "checks",
             "next_steps", "exit_code", "error_code"})

    def test_display_text_is_bounded_and_inert(self):
        from tabularium.diagnostics import _display_text
        hostile = "\x1b]8;;http://evil\x07A" * 40
        out = _display_text(hostile)
        self.assertNotIn("\x1b", out)
        self.assertLessEqual(len(out), 64)
        self.assertTrue(out.endswith("..."))
        self.assertEqual(_display_text(None), "")
        self.assertEqual(_display_text(9), "")

    def test_secret_bearing_files_are_not_disclosed(self):
        holder, root = self.make_project(onboard=True)
        self.addCleanup(holder.cleanup)
        secret = "synthetic-secret-value-123456"
        (root / ".env").write_text(f"API_KEY={secret}\n", encoding="utf-8")
        (root / "README.md").write_text(f"hostile metadata {secret}\n", encoding="utf-8")
        for command in ("status", "doctor"):
            for suffix in ([], ["--json"]):
                code, stdout, stderr = self.invoke([command, str(root), *suffix])
                self.assertEqual(code, 0)
                self.assertNotIn(secret, stdout + stderr)

    def test_network_is_never_attempted(self):
        holder, root = self.make_project(onboard=True)
        self.addCleanup(holder.cleanup)
        with patch.object(socket, "socket", side_effect=AssertionError("network")), \
                patch.object(socket, "create_connection", side_effect=AssertionError("network")):
            self.assertEqual(diagnose(root, "status").overall_state, "READY")
            self.assertEqual(diagnose(root, "doctor").overall_state, "READY")

    def test_diagnostics_have_no_authority_or_repair_path(self):
        source = (Path(__file__).resolve().parents[1] / "tabularium" / "diagnostics.py").read_text(encoding="utf-8")
        for forbidden in ("apply_authorized_proposal", "build_authorizations", "write_text(", "os.replace("):
            self.assertNotIn(forbidden, source)

    def test_repeated_json_diagnostics_are_deterministic(self):
        holder, root = self.make_project(onboard=True)
        self.addCleanup(holder.cleanup)
        self.assertEqual(render_json(diagnose(root, "doctor")), render_json(diagnose(root, "doctor")))

    def test_optional_capabilities_are_non_authoritative(self):
        holder, root = self.make_project()
        self.addCleanup(holder.cleanup)
        result = diagnose(root, "doctor")
        self.assertTrue(result.capabilities)
        self.assertTrue(all(not capability.required for capability in result.capabilities))
        self.assertTrue(all(
            "authorization" in capability.message.lower() or not capability.available
            for capability in result.capabilities
        ))

    def test_invalid_target_uses_hard_diagnostic_exit(self):
        missing = Path(tempfile.gettempdir()) / "tabu-m73-no-such-target-unique"
        result = diagnose(missing, "doctor")
        self.assertEqual(result.overall_state, "ERROR")
        self.assertEqual(result.exit_code, 2)
        self.assertIn(result.error_code, {"TARGET_NOT_FOUND", "TARGET_INVALID"})


class TaskSelectionHintTests(unittest.TestCase):
    """B5-P1: READY teaches project health; a separate line reports ONLY
    Task-selection evidence (candidate availability), never execution
    readiness. Candidate semantics must mirror the run gate exactly."""

    REMEDIATION = "Create or select a usable Task before running work."

    def setUp(self):
        pin_coherent_installation(self)

    def make_onboarded(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        holder = tempfile.TemporaryDirectory(prefix="tabu-b5-")
        root = Path(holder.name)
        initialize(root)
        declarations = {
            "title": "B5 fixture",
            "purpose": "Exercise Task-selection clarity",
            "scope": "A bounded status fixture",
        }
        draft = onboard_project(root, declarations)
        self.assertIsNotNone(draft.proposal)
        onboard_project(
            root, declarations,
            build_authorizations(draft.proposal, "alex",
                                 "2026-08-19T12:00:00+00:00"),
        )
        self.addCleanup(holder.cleanup)
        return holder, root

    @staticmethod
    def write_task(root: Path, task_id: str, **overrides) -> None:
        import yaml
        content = {
            "schema_version": "1.10", "id": task_id,
            "title": f"Task {task_id}",
            "objective": "Exercise status task-selection evidence.",
            "status": "INBOX", "created_at": "2026-01-01T00:00:00Z",
            "created_by": "USER",
        }
        content.update(overrides)
        tasks = root / ".ai" / "tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        (tasks / f"{task_id}.md").write_text(
            "---\n" + yaml.safe_dump(content, sort_keys=False)
            + "---\n# Task\n\nBody.\n", encoding="utf-8")

    @staticmethod
    def tree_snapshot(root: Path) -> dict[str, bytes]:
        return {str(p.relative_to(root)): p.read_bytes()
                for p in root.rglob("*") if p.is_file()}

    # 1 -- healthy project, zero candidates ----------------------------------

    def test_zero_candidates_reports_selection_without_readiness_claim(self):
        _, root = self.make_onboarded()
        before = self.tree_snapshot(root)
        result = diagnose(root, "status")
        rendered = render_human(result)
        self.assertEqual(result.overall_state, "READY")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Task selection: no usable Task candidate", rendered)
        for forbidden in ("execution ready", "ready to run", "actionable"):
            self.assertNotIn(forbidden, rendered.lower())
        self.assertEqual(result.next_steps, (self.REMEDIATION,))
        self.assertEqual(before, self.tree_snapshot(root))

    # 2 -- one valid non-terminal candidate ------------------------------------

    def test_single_candidate_is_named_without_overclaiming(self):
        _, root = self.make_onboarded()
        # Selectable by the run gate's candidate predicate, but its pending
        # approval would still be rejected later: presentation must not
        # claim actionability or readiness.
        self.write_task(root, "TASK-2026-0001", approval_required=True,
                        approval_status="PENDING")
        result = diagnose(root, "status")
        rendered = render_human(result)
        self.assertEqual(result.overall_state, "READY")
        self.assertIn("Task selection: 1 candidate (TASK-2026-0001)",
                      rendered)
        for forbidden in ("actionable", "ready to run", "execution ready"):
            self.assertNotIn(forbidden, rendered.lower())
        self.assertNotIn(self.REMEDIATION, rendered)
        payload = json.loads(render_json(result))
        self.assertEqual(payload["overall_state"], "READY")
        self.assertEqual(payload["compatibility"]["task_actionability"],
                         "NOT_APPLICABLE")

    # 3 -- multiple candidates ---------------------------------------------------

    def test_multiple_candidates_show_count_and_selection_guidance(self):
        _, root = self.make_onboarded()
        self.write_task(root, "TASK-2026-0001")
        self.write_task(root, "TASK-2026-0002")
        before = self.tree_snapshot(root)
        result = diagnose(root, "status")
        rendered = render_human(result)
        self.assertIn("Task selection: 2 candidates — choose one with --task",
                      rendered)
        self.assertEqual(result.next_steps, ())
        self.assertEqual(before, self.tree_snapshot(root))

    # 4 -- invalid/quarantined-only Task ------------------------------------------

    def test_quarantined_only_tasks_point_to_diagnostics_without_detail(self):
        _, root = self.make_onboarded()
        self.write_task(root, "TASK-2026-0001", created_by="Tabularium")
        result = diagnose(root, "status")
        rendered = render_human(result)
        payload = json.loads(render_json(result))
        self.assertEqual(payload["overall_state"], "READY")
        self.assertIn("INVALID_RECORD_QUARANTINED",
                      payload["compatibility"]["diagnostic_codes"])
        self.assertEqual(result.task_selection,
                         "no usable Task candidate — see diagnostics")
        self.assertNotIn("created_by", rendered)

    # 5 -- terminal-only Tasks -------------------------------------------------------

    def test_terminal_tasks_are_not_candidates(self):
        _, root = self.make_onboarded()
        self.write_task(root, "TASK-2026-0001", status="ARCHIVED",
                        archived_outcome="COMPLETED")
        result = diagnose(root, "status")
        self.assertEqual(result.task_selection, "no usable Task candidate")

    # 6 -- equivalence with the run gate's candidate set -----------------------------

    def test_hint_candidates_match_run_gate_classification(self):
        from tabularium.execution import _classify_task_records
        from tabularium.engine.context_loader.confinement import ProjectScope
        from tabularium.engine.context_loader.discovery import discover

        scenarios = {
            "valid": lambda r: self.write_task(r, "TASK-2026-0001"),
            "invalid": lambda r: self.write_task(
                r, "TASK-2026-0002", created_by="Tabularium"),
            "malformed": lambda r: (
                r / ".ai" / "tasks").mkdir(parents=True, exist_ok=True) or (
                r / ".ai" / "tasks" / "TASK-2026-0003.md").write_text(
                "# no frontmatter\n", encoding="utf-8"),
            "mismatch": lambda r: self.write_task(
                r, "TASK-2026-0004", id="TASK-2026-9999"),
            "duplicate": lambda r: (
                self.write_task(r, "TASK-2026-0005"),
                self.write_task(r, "TASK-2026-0006",
                                id="TASK-2026-0005")),
            "terminal": lambda r: self.write_task(
                r, "TASK-2026-0007", status="COMPLETED"),
            "mixed": lambda r: (
                self.write_task(r, "TASK-2026-0008"),
                self.write_task(r, "TASK-2026-0009", created_by="Tabularium")),
            "multiple_valid": lambda r: (
                self.write_task(r, "TASK-2026-0010"),
                self.write_task(r, "TASK-2026-0011")),
        }
        for name, populate in scenarios.items():
            with self.subTest(scenario=name):
                _, root = self.make_onboarded()
                populate(root)
                expected_ids = sorted(
                    rec.declared_id
                    for rec in _classify_task_records(
                        discover(root, ProjectScope(root)))[0]
                    if rec.declared_id)
                hint = diagnose(root, "status").task_selection
                if not expected_ids:
                    self.assertTrue(hint.startswith("no usable Task candidate"))
                elif len(expected_ids) == 1:
                    self.assertEqual(hint, f"1 candidate ({expected_ids[0]})")
                else:
                    self.assertEqual(
                        hint,
                        f"{len(expected_ids)} candidates — choose one "
                        f"with --task")

    # 7 -- discovery failure omits the line rather than fabricating certainty --

    def test_loader_failure_omits_task_selection_line(self):
        _, root = self.make_onboarded()
        self.write_task(root, "TASK-2026-0001")
        with patch("tabularium.diagnostics.load_context",
                   side_effect=RuntimeError("simulated loader failure")):
            result = diagnose(root, "doctor")
        rendered = render_human(result)
        self.assertNotIn("Task selection:", rendered)
        self.assertIsNone(result.task_selection)
        self.assertIn("CONTEXT_LOAD_FAILED", json.dumps(render_json(result)))

    # 8 -- JSON surface unchanged except next_steps content ----------------------

    def test_json_surface_stays_within_the_established_shape(self):
        _, root = self.make_onboarded()
        payload = json.loads(render_json(diagnose(root, "status")))
        self.assertEqual(
            set(payload), {
                "command", "tabularium_version", "target", "overall_state",
                "initialization", "onboarding", "loader", "foundations",
                "project", "compatibility", "capabilities", "checks",
                "next_steps", "exit_code", "error_code"})
        self.assertEqual(payload["next_steps"], [self.REMEDIATION])
        again = json.loads(render_json(diagnose(root, "status")))
        self.assertEqual(payload["next_steps"], again["next_steps"])

    # 9 -- observational authority under hostile conditions ------------------------

    def test_hint_computation_is_observation_only(self):
        _, root = self.make_onboarded()
        self.write_task(root, "TASK-2026-0001")
        before = self.tree_snapshot(root)

        def deny_network(*args, **kwargs):
            raise AssertionError("network attempted")

        import socket
        with patch.object(socket, "socket", side_effect=deny_network), \
                patch.object(socket, "create_connection",
                             side_effect=deny_network):
            result = diagnose(root, "doctor")
        self.assertEqual(result.overall_state, "READY")
        self.assertIn("Task selection: 1 candidate (TASK-2026-0001)",
                      render_human(result))
        self.assertEqual(before, self.tree_snapshot(root))


    # Defensive polish ------------------------------------------------------------------

    def test_non_project_entry_reason_is_not_a_candidate(self):
        """Only the documented project-entry selection reason may
        count. A future loader expansion selecting Tasks for other reasons
        must silently NOT become a run-gate candidate claim."""
        from tabularium.diagnostics import _task_selection_hint
        from tabularium.engine.context_loader.model import (ContextPackage,
                                                  SelectedItem)

        def package_with(*items):
            return ContextPackage(
                entry_mode="GOVERNED", package_status="READY",
                task_actionability="NOT_APPLICABLE", project_root="/b5",
                compatibility={"compatible": True}, selected=list(items))

        def item(task_id, reasons):
            return SelectedItem(record_id=task_id, family="Task", path=None,
                                load_mode="METADATA_ONLY",
                                selection_reasons=list(reasons))

        entry = ["PROJECT_NONTERMINAL_TASK"]
        with self.subTest(case="foreign reason is ignored"):
            self.assertEqual(
                _task_selection_hint(package_with(
                    item("TASK-2026-0001", ["SOME_FUTURE_REASON"]))),
                "no usable Task candidate")
        with self.subTest(case="documented reason counts"):
            self.assertEqual(
                _task_selection_hint(package_with(
                    item("TASK-2026-0001", entry))),
                "1 candidate (TASK-2026-0001)")
        with self.subTest(case="mixed keeps only the documented reason"):
            self.assertEqual(
                _task_selection_hint(package_with(
                    item("TASK-2026-0001", entry),
                    item("TASK-2026-0002", ["SOME_FUTURE_REASON"]))),
                "1 candidate (TASK-2026-0001)")

    def _health(self, overall_state, task_selection=None):
        from tabularium.diagnostics import HealthResult
        return HealthResult(
            "status", "0.1.0", "/b5", overall_state, "INITIALIZED",
            "COMPLETE", "READY", "VALID", task_selection=task_selection)

    def test_render_gate_matches_high_level_health_states(self):
        """The Task-selection line shares the remediation gate."""
        from tabularium.diagnostics import render_human
        for state, expected in (("READY", True), ("DEGRADED", True),
                                ("NOT_ONBOARDED", False),
                                ("INVALID_PROJECT", False),
                                ("VERSION_MISMATCH", False)):
            with self.subTest(state=state):
                rendered = render_human(
                    self._health(state, task_selection="1 candidate (X)"))
                self.assertEqual("Task selection: 1 candidate (X)" in rendered,
                                 expected)
        with self.subTest(state="READY without evidence"):
            self.assertNotIn("Task selection:",
                             render_human(self._health("READY")))

    def test_not_onboarded_project_omits_task_selection_line(self):
        """Real path: evidence may exist, but a non-onboarded project must not
        surface Task-selection presentation next to its own remediation."""
        holder = tempfile.TemporaryDirectory(prefix="tabu-b5-noob-")
        root = Path(holder.name)
        self.addCleanup(holder.cleanup)
        initialize(root)
        self.write_task(root, "TASK-2026-0001")
        result = diagnose(root, "status")
        rendered = render_human(result)
        self.assertEqual(result.overall_state, "NOT_ONBOARDED")
        self.assertIsNotNone(result.task_selection)
        self.assertNotIn("Task selection:", rendered)
        self.assertNotIn("Create or select a usable Task before running work.",
                         result.next_steps + tuple(rendered.splitlines()))

    def test_unrelated_family_quarantine_does_not_add_pointer(self):
        """Another family's quarantine must not make the
        zero-candidate line point at diagnostics."""
        _, root = self.make_onboarded()
        investigations = root / ".ai" / "investigations"
        investigations.mkdir(parents=True, exist_ok=True)
        (investigations / "INV-2026-0001.md").write_text(
            "# no frontmatter\n", encoding="utf-8")
        result = diagnose(root, "status")
        payload = json.loads(render_json(result))
        self.assertEqual(payload["overall_state"], "READY")
        self.assertIn("MALFORMED_RECORD_QUARANTINED",
                      payload["compatibility"]["diagnostic_codes"])
        self.assertEqual(result.task_selection, "no usable Task candidate")
        self.assertNotIn("see diagnostics", render_human(result))


    def test_degraded_health_still_surfaces_task_selection(self):
        """DEGRADED keeps the line when trustworthy evidence
        exists, and the zero-candidate remediation gate already matches."""
        _, root = self.make_onboarded()
        with patch("tabularium.diagnostics._package_versions",
                   return_value=("0.1.0", "0.0.9", "0.1.0", "repository")):
            result = diagnose(root, "status")
        self.assertEqual(result.overall_state, "DEGRADED")
        # D13: DEGRADED is a non-zero diagnostic verdict (only READY exits 0).
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.task_selection, "no usable Task candidate")
        rendered = render_human(result)
        self.assertIn("Task selection: no usable Task candidate", rendered)
        self.assertIn("Create or select a usable Task before running work.",
                      result.next_steps)


    def test_version_mismatch_next_step_is_reachable(self):
        """D6: the dedicated ruleset guidance must not be shadowed by the
        generic first-FAIL-check remediation loop."""
        _, root = self.make_onboarded()
        manifest = root / ".ai" / "manifest.yaml"
        text = manifest.read_text(encoding="utf-8")
        updated = re.sub(
            r'(?m)^tabularium_ruleset: ".*"$', 'tabularium_ruleset: "9.9.9"', text)
        self.assertNotEqual(updated, text)
        manifest.write_text(updated, encoding="utf-8")
        result = diagnose(root, "doctor")
        self.assertEqual(result.overall_state, VERSION_MISMATCH)
        # The guidance must name the file and the field the operator edits,
        # not just the abstract requirement.
        step = result.next_steps[0]
        self.assertIn("tabularium_ruleset", step)
        self.assertIn(".ai/manifest.yaml", step)


if __name__ == "__main__":
    unittest.main()
