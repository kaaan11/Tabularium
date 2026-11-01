"""All-or-nothing multi-file apply (tabularium/engine/code_gate/changeset.py, V2-04 part 1)."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tabularium.engine.code_gate import PatchAuthorization, PatchProposal
from tabularium.engine.code_gate import changeset
from tabularium.engine.code_gate.changeset import (
    CHANGESET_TOO_LARGE,
    INTERRUPTED_APPLY,
    MAX_FILES,
    ROLLBACK_FAILED,
    apply_changeset_transactional,
)
from tabularium.engine.code_gate.model import (
    APPLIED,
    DENIED,
    PATCH_APPLY_FAILED,
    PATCH_AUTHORIZATION_REQUIRED,
    PATCH_SCOPE_UNSUPPORTED,
    TARGET_CHANGED,
    TARGET_OUTSIDE_PROJECT,
    TESTS_FAILED,
)

BASE = {"src/a.py": "a = 1\n", "src/b.py": "b = 1\n", "README.md": "# r\n"}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                          check=True).stdout


class ChangesetFixture(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "repo"
        self.root.mkdir()
        for rel, text in BASE.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text)
        _git(self.root, "init", "-q")
        _git(self.root, "add", "-A")
        _git(self.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
        self.journal = Path(self.tmp.name) / "journal.json"

    def tearDown(self):
        self.tmp.cleanup()

    def snapshot(self) -> dict[str, bytes]:
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file() and ".git" not in p.parts}

    def proposal_for(self, edits: dict) -> tuple[PatchProposal, dict[str, str]]:
        """Build the diff git would show for `edits`, then put the tree back."""
        for rel, text in edits.items():
            path = self.root / rel
            if text is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
        _git(self.root, "add", "-A")
        diff = _git(self.root, "diff", "--cached", "HEAD")
        _git(self.root, "reset", "-q", "--hard", "HEAD")
        _git(self.root, "clean", "-qfd")
        targets = tuple(sorted(edits))
        return PatchProposal("prop-1", "exec-1", diff, targets), {
            rel: text for rel, text in edits.items() if text is not None}

    def auth(self, proposal: PatchProposal) -> PatchAuthorization:
        return PatchAuthorization(proposal.proposal_id, proposal.execution_id,
                                  proposal.diff_digest, "alex", "2026-10-29T12:00:00+00:00",
                                  proposal.binding())

    def apply(self, proposal, expected, **kw):
        def verifier(staged):
            return staged == expected, "staged differs from expected"
        kw.setdefault("verifier", verifier)
        kw.setdefault("journal_path", self.journal)
        return apply_changeset_transactional(self.root, proposal, self.auth(proposal), **kw)


class TestApplies(ChangesetFixture):

    def test_update_and_creation_in_new_directories_apply_together(self):
        edits = {"src/a.py": "a = 2\n", "src/b.py": "b = 2\n",
                 "pkg/sub/new.py": "n = 1\n", "tests/test_new.py": "def test(): pass\n"}
        proposal, expected = self.proposal_for(edits)
        os.chmod(self.root / "src/a.py", 0o750)
        result = self.apply(proposal, expected)
        self.assertEqual(result.status, APPLIED, result.diagnostics)
        for rel, text in edits.items():
            self.assertEqual((self.root / rel).read_text(), text)
        self.assertEqual(stat.S_IMODE((self.root / "src/a.py").stat().st_mode), 0o750)
        self.assertEqual(set(result.modified_files), set(edits))
        self.assertFalse(self.journal.exists())
        self.assertEqual(list(self.root.rglob(".tabu-*")), [])

    def test_journal_lists_every_file_before_the_first_replacement(self):
        proposal, expected = self.proposal_for({"src/a.py": "a = 2\n", "new.py": "n\n"})
        seen = []
        real_replace = os.replace

        def spy(src, dst, **kwargs):
            if not kwargs:  # the change set's own replaces; record writes use dir_fd
                seen.append(json.loads(self.journal.read_text()))
            return real_replace(src, dst, **kwargs)

        with patch.object(changeset.os, "replace", side_effect=spy):
            result = self.apply(proposal, expected)
        self.assertEqual(result.status, APPLIED)
        files = {f["path"]: f for f in seen[0]["files"]}
        self.assertEqual(set(files), {"src/a.py", "new.py"})
        self.assertTrue(files["new.py"]["created"])
        self.assertIsNotNone(files["src/a.py"]["original_sha256"])


class TestAllOrNothing(ChangesetFixture):

    def test_failure_on_second_replacement_restores_everything(self):
        edits = {"src/a.py": "a = 2\n", "src/b.py": "b = 2\n", "pkg/sub/new.py": "n\n"}
        proposal, expected = self.proposal_for(edits)
        before = self.snapshot()
        real_replace = os.replace
        calls = []

        def fail_second(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("disk full")
            return real_replace(src, dst)

        with patch.object(changeset.os, "replace", side_effect=fail_second):
            result = self.apply(proposal, expected)
        self.assertEqual(result.status, DENIED)
        self.assertEqual(result.reason_codes, (PATCH_APPLY_FAILED,))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "pkg").exists())
        self.assertFalse(self.journal.exists())
        self.assertEqual(list(self.root.rglob(".tabu-*")), [])

    def test_file_changed_after_planning_aborts_and_restores(self):
        edits = {"src/a.py": "a = 2\n", "src/b.py": "b = 2\n"}
        proposal, expected = self.proposal_for(edits)

        def verifier(staged):
            # Someone edits b.py after it was planned (verification window).
            (self.root / "src/b.py").write_text("someone else\n")
            return True, "ok"

        result = self.apply(proposal, expected, verifier=verifier)
        self.assertEqual(result.reason_codes, (TARGET_CHANGED,))
        self.assertEqual((self.root / "src/a.py").read_text(), "a = 1\n")
        self.assertEqual((self.root / "src/b.py").read_text(), "someone else\n")

    def test_interrupt_mid_commit_restores_and_reraises(self):
        proposal, expected = self.proposal_for({"src/a.py": "a = 2\n", "src/b.py": "b = 2\n"})
        before = self.snapshot()
        real_replace = os.replace
        calls = []

        def interrupt_second(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real_replace(src, dst)

        with patch.object(changeset.os, "replace", side_effect=interrupt_second), \
                self.assertRaises(KeyboardInterrupt):
            self.apply(proposal, expected)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.journal.exists())

    def test_failed_rollback_keeps_the_journal(self):
        proposal, expected = self.proposal_for({"src/a.py": "a = 2\n", "src/b.py": "b = 2\n"})
        real_replace = os.replace
        calls = []

        def fail_second(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("disk full")
            return real_replace(src, dst)

        with patch.object(changeset.os, "replace", side_effect=fail_second), \
                patch.object(changeset, "_restore", side_effect=OSError("still full")):
            result = self.apply(proposal, expected)
        self.assertEqual(result.reason_codes, (ROLLBACK_FAILED,))
        self.assertTrue(self.journal.exists())

    def test_existing_journal_refuses_and_is_left_untouched(self):
        self.journal.write_text('{"from": "an interrupted run"}')
        proposal, expected = self.proposal_for({"src/a.py": "a = 2\n"})
        before = self.snapshot()
        result = self.apply(proposal, expected)
        self.assertEqual(result.reason_codes, (INTERRUPTED_APPLY,))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.journal.read_text(), '{"from": "an interrupted run"}')

    def test_verifier_failure_writes_nothing(self):
        proposal, expected = self.proposal_for({"src/a.py": "a = 2\n", "new.py": "n\n"})
        before = self.snapshot()
        result = self.apply(proposal, expected, verifier=lambda staged: (False, "no"))
        self.assertEqual(result.status, TESTS_FAILED)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.journal.exists())

    def test_truthy_non_true_verifier_is_a_failure(self):
        proposal, expected = self.proposal_for({"src/a.py": "a = 2\n"})
        result = self.apply(proposal, expected, verifier=lambda staged: ("yes", ""))
        self.assertEqual(result.status, TESTS_FAILED)


class TestRefusedBeforeWriting(ChangesetFixture):

    def assertRefused(self, edits, code, prepare=None):
        proposal, expected = self.proposal_for(edits)
        if prepare:
            prepare()
        before = self.snapshot()
        result = self.apply(proposal, expected)
        self.assertEqual(result.status, DENIED)
        self.assertEqual(result.reason_codes, (code,), result.diagnostics)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.journal.exists())

    def test_deletion(self):
        self.assertRefused({"src/a.py": "a = 2\n", "src/b.py": None}, PATCH_SCOPE_UNSUPPORTED)

    def test_too_many_files(self):
        edits = {f"gen/f{i}.py": f"x = {i}\n" for i in range(MAX_FILES + 1)}
        self.assertRefused(edits, CHANGESET_TOO_LARGE)

    def test_creation_target_already_exists(self):
        self.assertRefused({"src/a.py": "a = 2\n", "notes.txt": "worker\n"},
                           PATCH_SCOPE_UNSUPPORTED,
                           prepare=lambda: (self.root / "notes.txt").write_text("mine\n"))

    def test_symlinked_ancestor(self):
        outside = Path(self.tmp.name) / "outside"

        def prepare():
            outside.mkdir()
            (self.root / "pkg").symlink_to(outside)

        # The gate refuses symlinked components before the change set plans.
        self.assertRefused({"src/a.py": "a = 2\n", "pkg/new.py": "n\n"}, TARGET_OUTSIDE_PROJECT,
                           prepare=prepare)
        self.assertEqual(list(outside.iterdir()), [])

    def test_missing_authorization(self):
        proposal, _ = self.proposal_for({"src/a.py": "a = 2\n"})
        result = apply_changeset_transactional(self.root, proposal, None,
                                               verifier=lambda s: (True, ""))
        self.assertEqual(result.reason_codes, (PATCH_AUTHORIZATION_REQUIRED,))


if __name__ == "__main__":
    unittest.main()


class TestChangesetPermissionAndCaseHandling(ChangesetFixture):
    """Changeset rollback mode preservation, umask defaults, and case-folding checks."""

    def test_rollback_keeps_the_original_file_mode(self):
        proposal, expected = self.proposal_for({"src/a.py": "a = 2\n", "src/b.py": "b = 2\n"})
        os.chmod(self.root / "src/a.py", 0o750)
        real_replace = os.replace
        calls = []

        def fail_second(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("disk full")
            return real_replace(src, dst)

        with patch.object(changeset.os, "replace", side_effect=fail_second):
            result = self.apply(proposal, expected)
        self.assertEqual(result.reason_codes, (PATCH_APPLY_FAILED,))
        self.assertEqual((self.root / "src/a.py").read_text(), "a = 1\n")
        self.assertEqual(stat.S_IMODE((self.root / "src/a.py").stat().st_mode), 0o750)

    def test_created_files_get_the_umask_default_mode(self):
        proposal, expected = self.proposal_for({"pkg/new.py": "n\n"})
        old = os.umask(0o022)
        try:
            result = self.apply(proposal, expected)
        finally:
            os.umask(old)
        self.assertEqual(result.status, APPLIED)
        self.assertEqual(stat.S_IMODE((self.root / "pkg/new.py").stat().st_mode), 0o644)

    def test_paths_differing_only_by_case_are_one_path(self):
        proposal, expected = self.proposal_for({"src/Note.md": "x\n", "src/note.md": "y\n"})
        before = self.snapshot()
        result = self.apply(proposal, expected)
        self.assertEqual(result.reason_codes, (PATCH_SCOPE_UNSUPPORTED,))
        self.assertEqual(self.snapshot(), before)


class TestChangesetInterruptAndSymlinkDefense(ChangesetFixture):
    """Interrupted apply rollback, symlink race refusal, and journal write failures."""

    def test_interrupt_right_after_a_replace_still_restores_that_file(self):
        proposal, expected = self.proposal_for({"src/a.py": "a = 2\n", "src/b.py": "b = 2\n"})
        before = self.snapshot()
        real_replace = os.replace

        calls = []

        def replace_then_interrupt(src, dst):
            real_replace(src, dst)          # the file is already replaced ...
            calls.append(dst)
            if len(calls) == 1:
                raise KeyboardInterrupt     # ... before the run can note it

        with patch.object(changeset.os, "replace", side_effect=replace_then_interrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.apply(proposal, expected)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.journal.exists())

    def test_directory_swapped_for_symlink_after_planning_is_refused(self):
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "keep.py").write_text("k\n")
        _git(self.root, "add", "-A")
        _git(self.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "pkg")
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        proposal, expected = self.proposal_for({"pkg/new.py": "n\n"})

        real_gate = changeset.evaluate_patch
        calls = []

        def gate_then_swap(*args, **kwargs):
            result = real_gate(*args, **kwargs)
            calls.append(1)
            if len(calls) == 2:
                # After the final gate and before any write, pkg/ becomes a
                # symlink out of the project (the window the gate cannot see).
                (self.root / "pkg").rename(self.root / "pkg_real")
                (self.root / "pkg").symlink_to(outside)
            return result

        temp_dirs = []
        real_mkstemp = changeset.tempfile.mkstemp

        def spy_mkstemp(*args, **kwargs):
            temp_dirs.append(Path(kwargs["dir"]).resolve())
            return real_mkstemp(*args, **kwargs)

        with patch.object(changeset, "evaluate_patch", side_effect=gate_then_swap), \
                patch.object(changeset.tempfile, "mkstemp", side_effect=spy_mkstemp):
            result = self.apply(proposal, expected)
        self.assertEqual(result.reason_codes, (TARGET_CHANGED,))
        # Not even a temporary file may be created outside the project.
        for directory in temp_dirs:
            self.assertTrue(directory.is_relative_to(self.root.resolve()), directory)
        self.assertEqual(list(outside.iterdir()), [])

    def test_failed_journal_write_leaves_no_journal(self):
        proposal, expected = self.proposal_for({"src/a.py": "a = 2\n"})
        with patch.object(changeset.json, "dump", side_effect=OSError("no space")):
            result = self.apply(proposal, expected)
        self.assertEqual(result.reason_codes, (PATCH_APPLY_FAILED,))
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.apply(proposal, expected).status, APPLIED)
