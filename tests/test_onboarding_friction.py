"""Acceptance tests for onboarding friction reduction."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tabularium.change_config import load_change_config
from tabularium.cli import main
from tabularium.init import initialize
from tabularium.onboarding import build_authorizations, onboard_project, render_human


class TestOnboardingFriction(unittest.TestCase):
    """The 8 acceptance tests for onboarding friction reduction."""

    def _create_git_repo(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        holder = tempfile.TemporaryDirectory(prefix="tabu-onboard-test-")
        root = Path(holder.name)
        subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "user@example.com"], cwd=root, check=True)
        (root / "README.md").write_text("# Project Fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=root, capture_output=True, check=True)
        initialize(root)
        return holder, root

    def test_1_defaults(self):
        """1. Defaults: name and timestamp use Enter (defaults), git user configured.
        Result is ONBOARDED; authorizer name matches git config, timestamp within +-60s of test time."""
        holder, root = self._create_git_repo()
        self.addCleanup(holder.cleanup)

        # Inputs: title, purpose, scope, constraints (Enter), test command (Enter), y, name (Enter), time (Enter)
        inputs = ["My Title", "My Purpose", "My Scope", "", "", "y", "", ""]
        sys.stdin = io.StringIO("\n".join(inputs) + "\n")
        stdout = io.StringIO()
        stderr = io.StringIO()

        captured_auth: dict[str, str] = {}
        original_build = build_authorizations

        def spy_build(proposal, authorized_by, authorized_at):
            auths = original_build(proposal, authorized_by, authorized_at)
            captured_auth["authorized_by"] = authorized_by
            captured_auth["authorized_at"] = authorized_at
            return auths

        test_start = datetime.now(timezone.utc).timestamp()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr), \
             patch("tabularium.cli.build_authorizations", side_effect=spy_build):
            code = main(["onboard", str(root)])
        test_end = datetime.now(timezone.utc).timestamp()

        self.assertEqual(code, 0)
        self.assertIn("Project onboarding: ONBOARDED", stdout.getvalue())
        self.assertEqual(captured_auth.get("authorized_by"), "Test User")
        self.assertIn("authorized_at", captured_auth)
        auth_time = datetime.fromisoformat(captured_auth["authorized_at"].replace("Z", "+00:00")).timestamp()
        self.assertAlmostEqual(auth_time, (test_start + test_end) / 2, delta=60)

        # Inspect authorization records in .ai/
        # Check .ai/project.md was written
        self.assertTrue((root / ".ai" / "project.md").is_file())
        self.assertIn("My Title", (root / ".ai" / "project.md").read_text(encoding="utf-8"))

        # Re-verify with onboard_project: reports ALREADY_ONBOARDED
        repeat = onboard_project(root)
        self.assertEqual(repeat.status, "ALREADY_ONBOARDED")

    def test_2_input_count(self):
        """2. Input count: title, purpose, scope, constraints (Enter), test command (Enter), y, name (Enter), timestamp (Enter)
        -> ONBOARDED. User typed 4 values (title, purpose, scope, y)."""
        holder, root = self._create_git_repo()
        self.addCleanup(holder.cleanup)

        inputs = ["Title 2", "Purpose 2", "Scope 2", "", "", "y", "", ""]
        written_values = [v for v in inputs if v.strip() and v != ""]
        # The written values are: 'Title 2', 'Purpose 2', 'Scope 2', 'y'
        self.assertEqual(len(written_values), 4)

        sys.stdin = io.StringIO("\n".join(inputs) + "\n")
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = main(["onboard", str(root)])

        self.assertEqual(code, 0)
        self.assertIn("Project onboarding: ONBOARDED", stdout.getvalue())

    def test_3_output_reduction_and_single_discovery_block(self):
        """3. Output reduction: 'Observed project signals' appears exactly once. 'README file present: .ai/' never appears.
        Line count is less than half of original 176."""
        holder, root = self._create_git_repo()
        self.addCleanup(holder.cleanup)

        inputs = ["Title 3", "Purpose 3", "Scope 3", "", "", "y", "", ""]
        sys.stdin = io.StringIO("\n".join(inputs) + "\n")
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = main(["onboard", str(root)])

        self.assertEqual(code, 0)
        out = stdout.getvalue()
        lines = out.splitlines()

        # 'Observed project signals' block appears at top-level exactly once
        top_level_signals = [l for l in lines if l.startswith("Observed project signals")]
        self.assertEqual(len(top_level_signals), 1)

        # 'README file present: .ai/' must NEVER appear
        self.assertNotIn("README file present: .ai/", out)
        self.assertNotIn("README file present: `.ai/", out)

        # Measured 108 lines on this scenario (was 176). The bound sits just
        # above it so a returning duplicate block fails here; the authorized
        # preview (~78 lines) is what keeps it from going lower.
        self.assertLessEqual(len(lines), 115, len(lines))

    def test_4_test_command_change_yaml(self):
        """4. Test command: .venv/bin/python -m pytest -q entered -> .ai/change.yaml is written and
        load_change_config reads it as test_command == ['.venv/bin/python','-m','pytest','-q'].
        Visible in proposal preview. If confirmation is 'n', no files are written including change.yaml."""
        holder, root = self._create_git_repo()
        self.addCleanup(holder.cleanup)

        # Case A: Confirmation 'n' -> no files written
        inputs_n = ["Title 4", "Purpose 4", "Scope 4", "", ".venv/bin/python -m pytest -q", "n"]
        sys.stdin = io.StringIO("\n".join(inputs_n) + "\n")
        stdout_n = io.StringIO()
        stderr_n = io.StringIO()
        with patch("sys.stdout", stdout_n), patch("sys.stderr", stderr_n):
            code_n = main(["onboard", str(root)])
        self.assertEqual(code_n, 1)
        self.assertFalse((root / ".ai" / "change.yaml").exists())
        self.assertIn("Authorization not provided; no files were changed.", stderr_n.getvalue())
        # Proposal preview in stdout_n should have shown .ai/change.yaml
        self.assertIn("- .ai/change.yaml", stdout_n.getvalue())

        # Case B: Confirmation 'y' -> change.yaml written and read by load_change_config
        inputs_y = ["Title 4", "Purpose 4", "Scope 4", "", ".venv/bin/python -m pytest -q", "y", "", ""]
        sys.stdin = io.StringIO("\n".join(inputs_y) + "\n")
        stdout_y = io.StringIO()
        with patch("sys.stdout", stdout_y):
            code_y = main(["onboard", str(root)])
        self.assertEqual(code_y, 0)
        self.assertTrue((root / ".ai" / "change.yaml").is_file())

        cfg = load_change_config(root)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.test_command, [".venv/bin/python", "-m", "pytest", "-q"])

    def test_5_invalid_test_command_retry_and_skip(self):
        """5. Invalid test command: unclosed quotes entered twice -> no change.yaml, onboarding still completes."""
        holder, root = self._create_git_repo()
        self.addCleanup(holder.cleanup)

        # Two unclosed quotes
        inputs = ["Title 5", "Purpose 5", "Scope 5", "", "python -c 'unclosed", "pytest \"unclosed", "y", "", ""]
        sys.stdin = io.StringIO("\n".join(inputs) + "\n")
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = main(["onboard", str(root)])

        self.assertEqual(code, 0)
        self.assertIn("Project onboarding: ONBOARDED", stdout.getvalue())
        self.assertIn("Could not parse test command; change.yaml will not be written.", stdout.getvalue())
        self.assertFalse((root / ".ai" / "change.yaml").exists())

    def test_6_manual_invalid_timestamp_rejected(self):
        """6. Manual value: invalid timestamp is rejected."""
        holder, root = self._create_git_repo()
        self.addCleanup(holder.cleanup)

        inputs = ["Title 6", "Purpose 6", "Scope 6", "", "", "y", "Alex", "yesterday"]
        sys.stdin = io.StringIO("\n".join(inputs) + "\n")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code = main(["onboard", str(root)])

        self.assertEqual(code, 1)
        err = stderr.getvalue() + stdout.getvalue()
        self.assertIn("exact onboarding authorization was refused; no files were changed", err)

    def test_7_json_output_byte_identical_with_baseline(self):
        """7. --json: byte-identical with previous output with same fixed inputs."""
        holder, root = self._create_git_repo()
        self.addCleanup(holder.cleanup)

        stdout1 = io.StringIO()
        with patch("sys.stdout", stdout1):
            code1 = main(["onboard", "--json", str(root)])
        self.assertEqual(code1, 0)

        # Parse JSON and ensure status and keys match specification
        data = json.loads(stdout1.getvalue())
        self.assertEqual(data["status"], "PROPOSAL_READY")
        self.assertIn("profile", data)
        self.assertIn("discovered", data["profile"])
        self.assertIn("token_metrics", data["profile"])

        stdout2 = io.StringIO()
        with patch("sys.stdout", stdout2):
            code2 = main(["onboard", "--json", str(root)])
        self.assertEqual(code2, 0)

        self.assertEqual(stdout1.getvalue(), stdout2.getvalue())

    def test_8_policy_violation_detailed_rejection(self):
        """8. POLICY_VIOLATION: instruction writing to .env with tabu run ... --change -> stderr includes rule name and details,
        generic remediation sentence does not stand alone."""
        holder, root = self._create_git_repo()
        self.addCleanup(holder.cleanup)

        # First complete onboarding so the project is governed
        draft = onboard_project(root, {"title": "T", "purpose": "P", "scope": "S", "constraints": ""})
        auth = build_authorizations(draft.proposal, "Tabularium", "2026-10-30T12:00:00Z")
        onboard_project(root, {"title": "T", "purpose": "P", "scope": "S", "constraints": ""}, auth)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "onboarded"], cwd=root, capture_output=True, check=True)

        stderr = io.StringIO()
        stdout = io.StringIO()
        with patch("sys.stderr", stderr), patch("sys.stdout", stdout):
            try:
                # An explicit API worker: the policy refusal must not depend on
                # which CLI workers happen to be installed on this machine.
                code = main(["run", "write secret to .env: API_KEY=sk-abcdefghij1234567890abcdef", str(root),
                             "--change", "--worker", "api", "--model", "test-provider/test-model"])
            except SystemExit as exc:
                code = exc.code

        self.assertNotEqual(code, 0)
        err = stderr.getvalue()
        self.assertIn("Reason: POLICY_VIOLATION", err)
        self.assertIn("Policy security.md: Secret or credential material detected in execution plan intent", err)
        self.assertIn("Review the reason and try the indicated remediation.", err)
        # Ensure the generic sentence did not stand alone
        lines = [line.strip() for line in err.splitlines() if line.strip()]
        self.assertGreater(len(lines), 2)


if __name__ == "__main__":
    unittest.main()


class TestChangeYamlGovernance(unittest.TestCase):
    """.ai/change.yaml written by onboarding obeys the same rules as the foundations."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tabu-onboard-gov-")
        self.root = Path(self.tmp.name)
        initialize(self.root)
        self.decl = {"title": "T", "purpose": "P", "scope": "S", "test_command": ".venv/bin/python -m pytest -q"}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _foundation_snapshot(self) -> dict[str, str]:
        return {p: (self.root / p).read_text(encoding="utf-8")
                for p in (".ai/project.md", ".ai/current-state.md", ".ai/constraints.md")}

    def test_command_that_load_change_config_rejects_is_never_written(self) -> None:
        decl = dict(self.decl, test_command="'' pytest")
        draft = onboard_project(self.root, decl)
        self.assertIsNone(draft.proposal)
        self.assertIn("TEST_COMMAND_INVALID", f"{draft.code} {draft.message}")
        self.assertFalse((self.root / ".ai" / "change.yaml").exists())

    def test_prompt_reasks_for_a_command_load_change_config_would_reject(self) -> None:
        from tabularium.onboarding import prompt_declarations
        answers = iter(["T", "P", "S", "", "'' pytest", ".venv/bin/python -m pytest"])
        out: list[str] = []
        decl = prompt_declarations(lambda _: next(answers), out.append)
        self.assertEqual(decl.test_command, (".venv/bin/python", "-m", "pytest"))
        self.assertTrue(any("Invalid test command" in line for line in out), out)

    def test_change_yaml_authorization_must_match_its_content(self) -> None:
        draft = onboard_project(self.root, self.decl)
        auths = build_authorizations(draft.proposal, "k", "2026-10-30T00:00:00Z")
        other = onboard_project(self.root, dict(self.decl, test_command="/bin/true"))
        other_auths = build_authorizations(other.proposal, "k", "2026-10-30T00:00:00Z")
        change_id = next(p.proposal_id for p in draft.proposal.durable_proposals
                         if p.target_path == ".ai/change.yaml")
        forged_id = next(p.proposal_id for p in other.proposal.durable_proposals
                         if p.target_path == ".ai/change.yaml")
        auths[change_id] = other_auths[forged_id]
        before = self._foundation_snapshot()
        res = onboard_project(self.root, self.decl, auths)
        self.assertNotEqual(res.status, "ONBOARDED")
        self.assertFalse((self.root / ".ai" / "change.yaml").exists())
        self.assertEqual(self._foundation_snapshot(), before, "nothing is written when any authorization fails")

    def test_change_yaml_created_after_the_draft_is_not_overwritten(self) -> None:
        draft = onboard_project(self.root, self.decl)
        auths = build_authorizations(draft.proposal, "k", "2026-10-30T00:00:00Z")
        (self.root / ".ai" / "change.yaml").write_text('test_command: ["/bin/false"]\n', encoding="utf-8")
        before = self._foundation_snapshot()
        res = onboard_project(self.root, self.decl, auths)
        self.assertNotEqual(res.status, "ONBOARDED")
        self.assertEqual((self.root / ".ai" / "change.yaml").read_text(), 'test_command: ["/bin/false"]\n')
        self.assertEqual(self._foundation_snapshot(), before)

    def test_current_state_omits_scaffold_readmes_init_created(self) -> None:
        # Deliberate change to authorized content: the
        # .ai/*/README.md files `tabu init` itself created are not "observed
        # project signals"; the project's own README still is.
        (self.root / "README.md").write_text("# project\n", encoding="utf-8")
        decl = {"title": "T", "purpose": "P", "scope": "S"}
        draft = onboard_project(self.root, decl)
        onboard_project(self.root, decl, build_authorizations(draft.proposal, "k", "2026-10-30T00:00:00Z"))
        state = (self.root / ".ai" / "current-state.md").read_text(encoding="utf-8")
        self.assertNotIn("README file present: `.ai/", state)
        self.assertIn("README file present: `README.md`", state)

    # Issue #47: a test command is never dropped without a message.
    def _onboard_without_test_command(self) -> None:
        decl = {"title": "T", "purpose": "P", "scope": "S"}
        draft = onboard_project(self.root, decl)
        res = onboard_project(self.root, decl, build_authorizations(draft.proposal, "k", "2026-10-30T00:00:00Z"))
        self.assertEqual(res.status, "ONBOARDED", res.message)
        self.assertFalse((self.root / ".ai" / "change.yaml").exists())

    def test_already_onboarded_says_a_supplied_test_command_was_not_written(self) -> None:
        self._onboard_without_test_command()
        before = self._foundation_snapshot()
        res = onboard_project(self.root, self.decl)
        self.assertEqual(res.status, "ALREADY_ONBOARDED")
        self.assertIn("the supplied test command was not written", res.message)
        self.assertIn("create .ai/change.yaml by hand", res.message)
        self.assertFalse((self.root / ".ai" / "change.yaml").exists())
        self.assertEqual(self._foundation_snapshot(), before)

    def test_already_onboarded_without_change_yaml_points_to_it(self) -> None:
        self._onboard_without_test_command()
        res = onboard_project(self.root)
        self.assertEqual(res.status, "ALREADY_ONBOARDED")
        self.assertIn(".ai/change.yaml is not configured", res.message)
        self.assertNotIn("supplied test command", res.message)
        # The human CLI output carries the same line.
        self.assertIn(".ai/change.yaml is not configured", render_human(res))

    def test_already_onboarded_with_change_yaml_stays_quiet(self) -> None:
        self._onboard_without_test_command()
        (self.root / ".ai" / "change.yaml").write_text('test_command: ["/bin/true"]\n', encoding="utf-8")
        res = onboard_project(self.root, self.decl)
        self.assertEqual(res.status, "ALREADY_ONBOARDED")
        self.assertEqual(res.message, "project declarations already exist; no files changed")

    def test_existing_change_yaml_kept_is_reported_at_review_and_after_write(self) -> None:
        (self.root / ".ai" / "change.yaml").write_text('test_command: ["/bin/false"]\n', encoding="utf-8")
        draft = onboard_project(self.root, self.decl)
        self.assertEqual(draft.status, "PROPOSAL_READY")
        self.assertIn("existing .ai/change.yaml was kept", draft.message)
        res = onboard_project(self.root, self.decl,
                              build_authorizations(draft.proposal, "k", "2026-10-30T00:00:00Z"))
        self.assertEqual(res.status, "ONBOARDED", res.message)
        self.assertIn("the entered test command was not written", res.message)
        self.assertEqual((self.root / ".ai" / "change.yaml").read_text(), 'test_command: ["/bin/false"]\n')

    def test_no_note_when_no_test_command_meets_an_existing_change_yaml(self) -> None:
        (self.root / ".ai" / "change.yaml").write_text('test_command: ["/bin/false"]\n', encoding="utf-8")
        decl = {"title": "T", "purpose": "P", "scope": "S"}
        draft = onboard_project(self.root, decl)
        res = onboard_project(self.root, decl, build_authorizations(draft.proposal, "k", "2026-10-30T00:00:00Z"))
        self.assertEqual(res.status, "ONBOARDED", res.message)
        self.assertIsNone(res.message)


class TestOnboardingAtomicityAndPermissions(unittest.TestCase):
    """Atomic onboarding rollbacks, authorizer validation, and permission checks."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tabu-onboard-atom-")
        self.root = Path(self.tmp.name).resolve()
        initialize(self.root)
        self.decl = {"title": "T", "purpose": "P", "scope": "S", "test_command": "pytest -q"}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _apply_with(self, after_foundations):
        import tabularium.engine.persistence.writer as writer_mod
        draft = onboard_project(self.root, self.decl)
        auths = build_authorizations(draft.proposal, "k", "2026-10-30T00:00:00Z")
        real = writer_mod.apply_authorized_proposal

        def wrapped(*args, **kwargs):
            result = real(*args, **kwargs)
            after_foundations()
            return result

        with patch.object(writer_mod, "apply_authorized_proposal", side_effect=wrapped):
            return onboard_project(self.root, self.decl, auths)

    def _foundations(self) -> dict[str, str]:
        return {p: (self.root / p).read_text(encoding="utf-8")
                for p in (".ai/project.md", ".ai/current-state.md", ".ai/constraints.md")}

    def test_b1_change_yaml_created_inside_the_write_window_is_refused_and_kept(self) -> None:
        import tabularium.engine.persistence.writer as writer_mod
        draft = onboard_project(self.root, self.decl)
        auths = build_authorizations(draft.proposal, "k", "2026-10-30T00:00:00Z")
        before = self._foundations()
        cfg = self.root / ".ai" / "change.yaml"
        real_create = writer_mod._fd_create

        def racing_create(dir_fd, name, content):
            cfg.write_text("test_command: [mine]\n", encoding="utf-8")  # appears after the check
            return real_create(dir_fd, name, content)

        with patch.object(writer_mod, "_fd_create", side_effect=racing_create):
            res = onboard_project(self.root, self.decl, auths)
        self.assertNotEqual(res.status, "ONBOARDED")
        self.assertEqual(cfg.read_text(encoding="utf-8"), "test_command: [mine]\n")
        # change.yaml sorts first, so no foundation was written yet; the
        # rollback of written foundations is test_a2_a_later_write_failure_...
        self.assertEqual(self._foundations(), before)

    def test_b6_write_error_rolls_back_and_is_not_raised(self) -> None:
        import tabularium.engine.persistence.writer as writer_mod
        draft = onboard_project(self.root, self.decl)
        auths = build_authorizations(draft.proposal, "k", "2026-10-30T00:00:00Z")
        before = self._foundations()
        with patch.object(writer_mod, "_fd_create", side_effect=OSError(28, "No space left")):
            res = onboard_project(self.root, self.decl, auths)
        self.assertNotEqual(res.status, "ONBOARDED")
        self.assertFalse((self.root / ".ai" / "change.yaml").exists())
        self.assertEqual(self._foundations(), before)

    def test_b2_oversized_test_command_is_refused(self) -> None:
        draft = onboard_project(self.root, dict(self.decl, test_command="x " * 40000))
        self.assertIsNone(draft.proposal)
        self.assertFalse((self.root / ".ai" / "change.yaml").exists())

    def test_b4_dict_authorizations_work_and_empty_actor_writes_nothing(self) -> None:
        draft = onboard_project(self.root, self.decl)
        auths = {k: v.to_dict() for k, v in
                 build_authorizations(draft.proposal, "k", "2026-10-30T00:00:00Z").items()}
        res = onboard_project(self.root, self.decl, auths)
        self.assertEqual(res.status, "ONBOARDED", res.message)
        self.assertTrue((self.root / ".ai" / "change.yaml").is_file())

        other = tempfile.TemporaryDirectory(prefix="tabu-onboard-actor-")
        self.addCleanup(other.cleanup)
        root2 = Path(other.name)
        initialize(root2)
        draft2 = onboard_project(root2, self.decl)
        res2 = onboard_project(root2, self.decl, build_authorizations(draft2.proposal, " ", "2026-10-30T00:00:00Z"))
        self.assertNotEqual(res2.status, "ONBOARDED")
        self.assertFalse((root2 / ".ai" / "change.yaml").exists())

    def test_b5_untrusted_git_name_is_not_offered_as_default(self) -> None:
        from tabularium.cli import _git_user_name
        subprocess.run(["git", "-C", str(self.root), "init", "-qb", "main"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "evil\x1b[2Jname"], check=True)
        self.assertEqual(_git_user_name(self.root), "")
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Tabularium"], check=True)
        self.assertEqual(_git_user_name(self.root), "Tabularium")

    def test_b7_prompts_and_typed_values_are_counted_from_the_real_run(self) -> None:
        subprocess.run(["git", "-C", str(self.root), "init", "-qb", "main"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Tabularium"], check=True)
        root = Path(tempfile.mkdtemp(prefix="tabu-onboard-count-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        subprocess.run(["git", "-C", str(root), "init", "-qb", "main"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Tabularium"], check=True)
        initialize(root)
        class CountingStdin(io.StringIO):
            """Every line read is one answered prompt (input() reads stdin)."""

            def __init__(self, text: str) -> None:
                super().__init__(text)
                self.lines: list[str] = []

            def readline(self, *args):  # noqa: D401 - io API
                line = super().readline(*args)
                if line:
                    self.lines.append(line.rstrip("\n"))
                return line

        stdin = CountingStdin("\n".join(["T", "P", "S", "", "", "y", "", ""]) + "\n")
        err = io.StringIO()
        with patch("sys.stdin", stdin), patch("sys.stdout", io.StringIO()), patch("sys.stderr", err):
            code = main(["onboard", str(root)])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(len(stdin.lines), 8)
        typed = [line for line in stdin.lines if line]
        self.assertEqual(typed, ["T", "P", "S", "y"])


class TestOnboardingGovernedWriter(unittest.TestCase):
    """Onboarding change.yaml generation via governed writer and terminal sanitization."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tabu-onboard-writer-")
        self.root = Path(self.tmp.name).resolve()
        initialize(self.root)
        self.decl = {"title": "T", "purpose": "P", "scope": "S", "test_command": "pytest -q"}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _foundations(self) -> dict[str, str]:
        return {p: (self.root / p).read_text(encoding="utf-8")
                for p in (".ai/project.md", ".ai/current-state.md", ".ai/constraints.md")}

    def test_a1_secret_in_test_command_is_refused(self) -> None:
        draft = onboard_project(self.root, dict(self.decl, test_command="env API_KEY=abc123 pytest"))
        self.assertIsNone(draft.proposal)
        self.assertFalse((self.root / ".ai" / "change.yaml").exists())

    def test_a1_writer_refuses_secret_shaped_change_yaml(self) -> None:
        import dataclasses
        from tabularium.onboarding import OnboardingProposal, apply_onboarding_proposal
        draft = onboard_project(self.root, self.decl)
        forged = tuple(dataclasses.replace(p, content="test_command:\n- password=hunter2\n")
                       if p.target_path == ".ai/change.yaml" else p for p in draft.proposal.durable_proposals)
        proposal = OnboardingProposal(draft.proposal.operation_id, forged)
        before = self._foundations()
        result = apply_onboarding_proposal(self.root, proposal,
                                           build_authorizations(proposal, "k", "2026-10-30T00:00:00Z"))
        self.assertEqual(result, "SECRET_MATERIAL_PRESENT")
        self.assertEqual(self._foundations(), before)

    def test_a2_change_yaml_must_be_a_create_of_this_operation(self) -> None:
        import dataclasses
        from tabularium.onboarding import OnboardingProposal, apply_onboarding_proposal
        draft = onboard_project(self.root, self.decl)
        for field_name, value in (("operation", "UPDATE"), ("project_root", "/elsewhere"),
                                  ("onboarding_operation_id", "ONBOARDING-other")):
            with self.subTest(field=field_name):
                tampered = tuple(dataclasses.replace(p, **{field_name: value})
                                 if p.target_path == ".ai/change.yaml" else p
                                 for p in draft.proposal.durable_proposals)
                proposal = OnboardingProposal(draft.proposal.operation_id, tampered)
                result = apply_onboarding_proposal(self.root, proposal,
                                                   build_authorizations(proposal, "k", "2026-10-30T00:00:00Z"))
                self.assertIsInstance(result, str)
                self.assertFalse((self.root / ".ai" / "change.yaml").exists())

    def test_a3_existing_change_yaml_of_any_kind_is_left_alone(self) -> None:
        cfg = self.root / ".ai" / "change.yaml"
        outside = Path(tempfile.mkdtemp(prefix="tabu-onboard-out-")) / "x"
        outside.write_text("keep\n")
        for kind in ("file", "fifo", "symlink", "binary"):
            with self.subTest(kind=kind):
                if os.path.lexists(cfg):
                    cfg.unlink()
                if kind == "file":
                    cfg.write_text('test_command: ["mine"]\n')
                elif kind == "fifo":
                    os.mkfifo(cfg)
                elif kind == "symlink":
                    cfg.symlink_to(outside)
                else:
                    cfg.write_bytes(b"\xff\xfe\x00")
                draft = onboard_project(self.root, self.decl)
                self.assertIsNotNone(draft.proposal, kind)
                self.assertNotIn(".ai/change.yaml", [p.target_path for p in draft.proposal.durable_proposals])
        self.assertEqual(outside.read_text(), "keep\n")

    def test_a4_non_utf8_git_name_is_not_a_crash(self) -> None:
        from tabularium.cli import _git_user_name
        subprocess.run(["git", "-C", str(self.root), "init", "-qb", "main"], check=True)
        with open(self.root / ".git" / "config", "ab") as stream:
            stream.write(b"[user]\n\tname = \xff\xfe\n")
        self.assertEqual(_git_user_name(self.root), "")

    def test_a5_unicode_line_breaks_and_surrogates_are_refused(self) -> None:
        from tabularium.change_config import ChangeConfigError, render_change_yaml
        for bad in ("a\u2028b", "a\x85b", "a\x0bb", "a\ud800b"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ChangeConfigError):
                    render_change_yaml(["python", bad])

    def test_a5_preview_is_sanitized(self) -> None:
        subprocess.run(["git", "-C", str(self.root), "init", "-qb", "main"], check=True)
        # The title reaches the previewed project.md verbatim (YAML would
        # escape it inside change.yaml, so the title is the real carrier).
        answers = ["T\x1b[2Jfake", "P", "S", "", "", "n"]
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO("\n".join(answers) + "\n")), patch("sys.stdout", out), \
             patch("sys.stderr", io.StringIO()):
            main(["onboard", str(self.root)])
        self.assertIn("Proposed durable files", out.getvalue())
        self.assertNotIn("\x1b", out.getvalue())

    def test_a6_ctrl_c_at_the_test_command_prompt_cancels(self) -> None:
        from tabularium.onboarding import prompt_declarations
        answers = iter(["T", "P", "S", ""])

        def fake(prompt: str) -> str:
            try:
                return next(answers)
            except StopIteration:
                raise KeyboardInterrupt

        try:
            result = prompt_declarations(fake, lambda _: None)
        except KeyboardInterrupt:
            result = "interrupted"
        self.assertIn(result, (None, "interrupted"))

    def test_a2_a_later_write_failure_removes_the_created_change_yaml(self) -> None:
        import tabularium.engine.persistence.writer as writer_mod
        draft = onboard_project(self.root, self.decl)
        auths = build_authorizations(draft.proposal, "k", "2026-10-30T00:00:00Z")
        before = self._foundations()
        real_replace = writer_mod._fd_replace

        def fail_on_project(dir_fd, name, content):
            if name == "project.md" and content != before[".ai/project.md"]:
                raise OSError(28, "No space left")
            return real_replace(dir_fd, name, content)

        with patch.object(writer_mod, "_fd_replace", side_effect=fail_on_project):
            res = onboard_project(self.root, self.decl, auths)
        self.assertNotEqual(res.status, "ONBOARDED")
        self.assertFalse((self.root / ".ai" / "change.yaml").exists(), "created file is rolled back")
        self.assertEqual(self._foundations(), before)


class TestOnboardingWriterSecurity(unittest.TestCase):
    """Security checks for change.yaml proposal validation, hardlinks, and secrets."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tabu-onboard-sec-")
        self.root = Path(self.tmp.name).resolve()
        initialize(self.root)
        self.decl = {"title": "T", "purpose": "P", "scope": "S", "test_command": "pytest -q"}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_f2_writer_refuses_an_invalid_change_yaml_itself(self) -> None:
        import dataclasses
        import tabularium.engine.persistence.writer as writer_mod
        from tabularium.onboarding import FoundationProposalBatch, OnboardingProposal
        draft = onboard_project(self.root, self.decl)
        for content in ("not: [valid", "test_command: []\n", "test_command: [x]\nextra: 1\n",
                        "test_command:\n- " + "x" * (70 * 1024) + "\n"):
            with self.subTest(content=content[:20]):
                forged = tuple(dataclasses.replace(p, content=content) if p.target_path == ".ai/change.yaml"
                               else p for p in draft.proposal.durable_proposals)
                proposal = OnboardingProposal(draft.proposal.operation_id, forged)
                batch = FoundationProposalBatch(proposal_id=proposal.operation_id,
                                                onboarding_operation_id=proposal.operation_id,
                                                project_root=str(self.root), proposals=forged)
                result = writer_mod.apply_authorized_proposal(
                    batch, str(self.root), build_authorizations(proposal, "k", "2026-10-30T00:00:00Z"))
                self.assertNotEqual(result.status, "WRITTEN")
                self.assertFalse((self.root / ".ai" / "change.yaml").exists())

    def test_f3_no_hard_links_falls_back_to_exclusive_open(self) -> None:
        import errno
        draft = onboard_project(self.root, self.decl)
        auths = build_authorizations(draft.proposal, "k", "2026-10-30T00:00:00Z")
        with patch("os.link", side_effect=OSError(errno.EPERM, "Operation not permitted")):
            res = onboard_project(self.root, self.decl, auths)
        self.assertEqual(res.status, "ONBOARDED", res.message)
        self.assertEqual(load_change_config(self.root).test_command, ["pytest", "-q"])
        self.assertEqual([p.name for p in (self.root / ".ai").glob(".tabu-*")], [])

    def test_f4_secret_split_across_arguments_is_refused(self) -> None:
        from tabularium.change_config import ChangeConfigError, render_change_yaml
        with self.assertRaises(ChangeConfigError):
            render_change_yaml(["cmd", "PASSWORD", "=", "hunter2"])
