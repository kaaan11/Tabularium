"""Hardening of the --change path against hostile worker manipulations.

The worker is untrusted: it may write any file inside its worktree. None of
that may run code, reach a protected path, or apply something other than the
diff the user was shown.
"""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from tabularium.change_path import create_worktree, cleanup_worktree
from tabularium.execution import run_instruction
from tabularium.engine.code_gate import PatchProposal, evaluate_patch
from tests.test_v2_09_change_path import ChangePathFixture, FakeWorkerAdapter


# Records Tabularium itself writes during a run (candidate Task, report). Everything
# else, including the rest of .ai/, must stay byte-identical.
_MACHINE_RECORDS = ((".ai", "tasks"), (".ai", "reports"))


def _hashes(root: Path) -> dict[str, bytes]:
    out = {}
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if not p.is_file() or ".git" in rel.parts or rel.parts[:2] in _MACHINE_RECORDS:
            continue
        out[str(rel)] = p.read_bytes()
    return out


class TestHostileWorktree(ChangePathFixture):

    def _run(self, mutate, confirm=lambda _: True):
        return run_instruction("x", self.root, adapter=FakeWorkerAdapter(mutate_fn=mutate),
                               change=True, confirm=confirm)

    def test_redirected_gitdir_cannot_run_code(self):
        """B-02: a worker that only writes files must not get git to run its fsmonitor."""
        pwned = Path(self.temp_dir.name) / "pwned"
        asked = []

        def mutate(wt: Path):
            evil = wt / "evil"
            for rel, txt in {
                "HEAD": "ref: refs/heads/master\n",
                "config": f"[core]\n\tbare = false\n\tfsmonitor = \"touch {pwned}; false\"\n",
                "objects/info/keep": "", "refs/heads/keep": "",
            }.items():
                f = evil / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(txt)
            (wt / ".git").write_text("gitdir: evil\n")
            (wt / "src" / "sample.py").write_text("changed\n")

        before = _hashes(self.root)
        result = self._run(mutate, confirm=lambda q: asked.append(q) or True)
        self.assertFalse(pwned.exists(), "worker-controlled git config ran a command")
        self.assertEqual(result.status, "REFUSED")
        self.assertEqual(result.code, "WORKTREE_TAMPERED")
        self.assertEqual(asked, [])
        self.assertEqual(_hashes(self.root), before)

    def test_repo_config_written_in_worktree_root_is_not_used(self):
        """Without touching .git, a worker-written config/hook must still never execute."""
        pwned = Path(self.temp_dir.name) / "pwned2"

        def mutate(wt: Path):
            (wt / ".gitattributes").write_text("*.py diff=evil\n")
            (wt / "src" / "sample.py").write_text("changed\n")

        # Diff driver defined in the main repository's config (the worktree reads it):
        # --no-ext-diff / --no-textconv and diff.external= must keep it from running.
        import subprocess
        subprocess.run(["git", "config", "diff.evil.command", f"touch {pwned}"],
                       cwd=str(self.root), check=True)
        subprocess.run(["git", "config", "diff.evil.textconv", f"sh -c 'touch {pwned}; cat'"],
                       cwd=str(self.root), check=True)
        result = self._run(mutate)
        self.assertFalse(pwned.exists())
        # Two files changed (.gitattributes + sample.py): refused, nothing applied.
        self.assertEqual(result.status, "REFUSED")

    def test_binary_change_next_to_text_change_is_refused(self):
        """B-05: a binary change the parser cannot see must not ride along a text patch."""
        def mutate(wt: Path):
            (wt / "src" / "sample.py").write_text("changed\n")
            (wt / "blob.bin").write_bytes(b"\x00\x01\x02binary")

        before = _hashes(self.root)
        result = self._run(mutate)
        self.assertEqual(result.code, "DIFF_UNSUPPORTED")
        self.assertEqual(_hashes(self.root), before)

    def test_single_binary_file_is_refused(self):
        def mutate(wt: Path):
            (wt / "blob.bin").write_bytes(b"\x00\x01\x02binary")

        result = self._run(mutate)
        self.assertEqual(result.code, "DIFF_UNSUPPORTED")

    def test_symlink_creation_is_refused(self):
        def mutate(wt: Path):
            os.symlink("/etc/passwd", wt / "link")

        result = self._run(mutate)
        self.assertEqual(result.code, "DIFF_UNSUPPORTED")
        self.assertFalse((self.root / "link").exists())

    def test_mode_change_is_refused(self):
        def mutate(wt: Path):
            f = wt / "src" / "sample.py"
            f.chmod(f.stat().st_mode | stat.S_IXUSR)

        result = self._run(mutate)
        self.assertEqual(result.code, "DIFF_UNSUPPORTED")

    def test_empty_file_next_to_text_change_is_refused(self):
        """An empty new file has no ---/+++ lines; the parser would silently drop it."""
        def mutate(wt: Path):
            (wt / "src" / "sample.py").write_text("changed\n")
            (wt / "empty.txt").write_text("")

        before = _hashes(self.root)
        result = self._run(mutate)
        self.assertEqual(result.code, "DIFF_UNSUPPORTED")
        self.assertEqual(_hashes(self.root), before)

    def test_every_worktree_git_call_pins_gitdir_and_disables_hooks(self):
        """Defence in depth: even if .git changed after the tamper check, git
        must not discover a worker-chosen repository or run its fsmonitor/hooks."""
        import subprocess
        from unittest.mock import patch

        calls = []
        real_run = subprocess.run

        def spy(argv, *a, **kw):
            calls.append(list(argv))
            return real_run(argv, *a, **kw)

        def mutate(wt: Path):
            (wt / "src" / "sample.py").write_text("changed\n")

        with patch("tabularium.change_path.subprocess.run", side_effect=spy):
            self._run(mutate, confirm=lambda _: False)
        # Main-repository commands (worktree add/prune, status, config) run in
        # the project; everything else touches the worktree.
        main_repo = {"worktree", "rev-parse", "status", "config"}
        wt_calls = [c for c in calls if c[:1] == ["git"] and c[1] not in main_repo]
        self.assertTrue(wt_calls)
        for argv in wt_calls:
            self.assertTrue(any(a.startswith("--git-dir=") for a in argv), argv)
            self.assertTrue(any(a.startswith("--work-tree=") for a in argv), argv)
            self.assertIn("core.fsmonitor=false", argv)
            self.assertIn("core.hooksPath=/dev/null", argv)

    def test_worktree_path_is_private_and_removed(self):
        """B-06: the worktree lives under a fresh mkdtemp directory, not a guessable path."""
        wt = create_worktree(self.root, "fixed-id")
        try:
            self.assertNotEqual(wt.parent, Path(tempfile.gettempdir()) / "tabu-wt-fixed-id")
            self.assertEqual(stat.S_IMODE(wt.parent.stat().st_mode), 0o700)
            self.assertTrue((wt.path / "src" / "sample.py").is_file())
        finally:
            cleanup_worktree(self.root, wt)
        self.assertFalse(wt.parent.exists())


class TestGatePathCanonicalization(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _allowed(self, path: str) -> tuple[bool, tuple[str, ...]]:
        diff = f"--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n+x\n"
        ev = evaluate_patch(PatchProposal("p", "e", diff, (path,)), self.root)
        return ev.allowed, ev.reason_codes

    def test_protected_paths_are_refused_in_every_spelling(self):
        """B-01, B-03, B-04, B-07."""
        for path in ("./.git/config", ".git/config", "src/./x.py", "a//b.py",
                     ".ENV", "prod.ENV", "config.KEY", "cert.PEM",
                     "CONSTITUTION/immutable-principles.md", ".gitattributes",
                     "sub/.gitmodules", "sub/.git/config", ".GIT/config",
                     ".ai/reports/RPT-1.md", ".ai/tasks/TASK-1.md", ".AI/manifest.yaml"):
            with self.subTest(path=path):
                allowed, codes = self._allowed(path)
                self.assertFalse(allowed, f"{path} was allowed")

    def test_ordinary_paths_stay_allowed(self):
        for path in ("README.md", "src/app.py", "docs/guide.md", ".gitignore",
                     "tests/test_app.py"):
            with self.subTest(path=path):
                allowed, codes = self._allowed(path)
                self.assertTrue(allowed, f"{path} refused: {codes}")


if __name__ == "__main__":
    unittest.main()


class TestCandidateTaskNeverLeaks(ChangePathFixture):
    """Every exit path of the auto-task change closes the candidate Task."""

    def _new_task_statuses(self, before: set[str]) -> dict[str, str]:
        from tabularium.task import read_frontmatter
        out = {}
        for f in (self.root / ".ai" / "tasks").glob("TASK-*.md"):
            if f.name not in before:
                rec = read_frontmatter(f, self.root, "Task")
                out[f.name] = str(rec.frontmatter.get("status"))
        return out

    def _run_and_statuses(self, mutate=None, adapter=None, **kw):
        before = {f.name for f in (self.root / ".ai" / "tasks").glob("TASK-*.md")}
        adapter = adapter or FakeWorkerAdapter(mutate_fn=mutate)
        try:
            result = run_instruction("change something", self.root, adapter=adapter,
                                     change=True, confirm=lambda _: True, **kw)
        except BaseException as exc:  # noqa: BLE001 - the path under test
            result = exc
        return result, self._new_task_statuses(before)

    def assertAllTerminal(self, statuses: dict[str, str]):
        self.assertEqual(len(statuses), 1, statuses)
        for name, status in statuses.items():
            self.assertIn(status, ("COMPLETED", "CANCELLED"), f"{name} left {status}")

    def test_tampered_worktree_closes_candidate(self):
        def mutate(wt: Path):
            (wt / ".git").write_text("gitdir: elsewhere\n")
        result, statuses = self._run_and_statuses(mutate)
        self.assertEqual(result.code, "WORKTREE_TAMPERED")
        self.assertAllTerminal(statuses)

    def test_unsupported_diff_closes_candidate(self):
        def mutate(wt: Path):
            (wt / "blob.bin").write_bytes(b"\x00\x01binary")
        result, statuses = self._run_and_statuses(mutate)
        self.assertEqual(result.code, "DIFF_UNSUPPORTED")
        self.assertAllTerminal(statuses)

    def test_worktree_creation_failure_closes_candidate(self):
        from unittest.mock import patch
        from tabularium.change_path import WorktreeError

        with patch("tabularium.change_path.create_worktree",
                   side_effect=WorktreeError("WORKTREE_CREATE_FAILED", "boom")):
            result, statuses = self._run_and_statuses(lambda wt: None)
        self.assertEqual(result.code, "WORKTREE_CREATE_FAILED")
        self.assertAllTerminal(statuses)

    def test_interrupt_during_confirmation_closes_candidate(self):
        """Ctrl-C at the prompt: an exception, not a return."""
        def mutate(wt: Path):
            (wt / "src" / "sample.py").write_text("changed\n")

        def interrupt(_):
            raise KeyboardInterrupt

        before = {f.name for f in (self.root / ".ai" / "tasks").glob("TASK-*.md")}
        with self.assertRaises(KeyboardInterrupt):
            run_instruction("change something", self.root,
                            adapter=FakeWorkerAdapter(mutate_fn=mutate),
                            change=True, confirm=interrupt)
        self.assertAllTerminal(self._new_task_statuses(before))

    def test_candidate_keeps_full_multiline_instruction_and_reason(self):
        """Title is the first line (<=80 chars); objective is the whole instruction;
        a cancelled candidate states its reason in result and body."""
        from tabularium.task import read_frontmatter

        instruction = ("Add an installation section to README.md that covers the "
                       "virtualenv setup and the pinned dependency versions\n"
                       "Second line: keep the existing headings unchanged.")

        def mutate(wt: Path):
            (wt / "src" / "sample.py").write_text("changed\n")

        before = {f.name for f in (self.root / ".ai" / "tasks").glob("TASK-*.md")}
        run_instruction(instruction, self.root, adapter=FakeWorkerAdapter(mutate_fn=mutate),
                        change=True, confirm=lambda _: False)
        new = [f for f in (self.root / ".ai" / "tasks").glob("TASK-*.md") if f.name not in before]
        self.assertEqual(len(new), 1)
        rec = read_frontmatter(new[0], self.root, "Task")
        fm = rec.frontmatter
        self.assertEqual(fm["title"], instruction.splitlines()[0][:80])
        self.assertEqual(" ".join(str(fm["objective"]).split()), " ".join(instruction.split()))
        self.assertEqual(fm["status"], "CANCELLED")
        self.assertEqual(fm.get("result"), "APPROVAL_REJECTED")
        self.assertIn("APPROVAL_REJECTED", rec.body)


class TestChangePathPreflightAndIntegrity(ChangePathFixture):
    """Change path preflight checks, unappliable patch rejection, and isolation."""

    def _run(self, mutate=None, **kw):
        asked = []
        kw.setdefault("adapter", FakeWorkerAdapter(mutate_fn=mutate))
        result = run_instruction("x", self.root, change=True,
                                 confirm=lambda q: asked.append(q) or True, **kw)
        return result, asked

    def test_unappliable_patches_are_refused_before_asking(self):
        """Approval must never be asked for a change apply would then refuse."""
        cases = {
            "deletion": lambda wt: (wt / "src" / "sample.py").unlink(),
            "drops_final_newline": lambda wt: (wt / "src" / "sample.py").write_text(
                "def calculate_total(a, b):\n    return a * b"),
            "crlf": lambda wt: (wt / "src" / "sample.py").write_bytes(
                b"def calculate_total(a, b):\r\n    return a * b\r\n"),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                before = _hashes(self.root)
                result, asked = self._run(mutate)
                self.assertEqual(result.code, "PATCH_NOT_APPLICABLE", result.message)
                self.assertEqual(asked, [])
                self.assertEqual(_hashes(self.root), before)

    def test_untracked_file_with_same_name_is_kept_and_not_asked(self):
        (self.root / "notes.txt").write_text("mine\n")
        result, asked = self._run(lambda wt: (wt / "notes.txt").write_text("worker\n"))
        self.assertEqual(result.code, "PATCH_NOT_APPLICABLE")
        self.assertEqual(asked, [])
        self.assertEqual((self.root / "notes.txt").read_text(), "mine\n")

    def test_non_ascii_file_name_applies(self):
        """git quoted non-ASCII paths ("\\303\\247...") and the gate called them outside the project."""
        result, asked = self._run(lambda wt: (wt / "src" / "çalışma.py").write_text("x = 1\n"))
        self.assertEqual(result.code, "COMPLETED", result.message)
        self.assertEqual((self.root / "src" / "çalışma.py").read_text(), "x = 1\n")

    def test_worktree_gitattributes_cannot_select_a_filter(self):
        """git add reads attributes from the base commit, not from the worker's tree."""
        import subprocess
        ran = Path(self.temp_dir.name) / "filter_ran"
        subprocess.run(["git", "config", "filter.custom.clean", f"sh -c 'touch {ran}; cat'"],
                       cwd=str(self.root), check=True)

        def mutate(wt: Path):
            (wt / ".gitattributes").write_text("*.py filter=custom\n")
            (wt / "src" / "sample.py").write_text("changed\n")

        self._run(mutate)
        self.assertFalse(ran.exists(), "a worker-written .gitattributes selected a filter")

    def test_unknown_worker_is_refused_and_closes_candidate(self):
        result = run_instruction("x", self.root, worker="bogus", change=True,
                                 confirm=lambda _: True)
        self.assertEqual(result.code, "WORKER_SELECTION_INVALID")
        from tabularium.task import read_frontmatter
        for f in (self.root / ".ai" / "tasks").glob("TASK-*.md"):
            st = read_frontmatter(f, self.root, "Task").frontmatter.get("status")
            if f.name != f"{self.task_id}.md":
                self.assertEqual(st, "CANCELLED")

    def test_task_id_must_be_canonical(self):
        for bad in ("../../outside", "TASK-2026-0001/../../x", "task-2026-0001"):
            with self.subTest(task=bad):
                result = run_instruction("x", self.root, task_id=bad, change=True,
                                         adapter=FakeWorkerAdapter(mutate_fn=lambda wt: None),
                                         confirm=lambda _: True)
                self.assertEqual(result.code, "TASK_ID_INVALID")

    def test_target_must_be_repository_root(self):
        sub = self.root / "src"
        result = run_instruction("x", sub, change=True,
                                 adapter=FakeWorkerAdapter(mutate_fn=lambda wt: None),
                                 confirm=lambda _: True)
        self.assertNotEqual(result.code, "COMPLETED")
        from tabularium.change_path import check_git_clean
        ok, code, _ = check_git_clean(sub)
        self.assertFalse(ok)
        self.assertEqual(code, "NOT_REPOSITORY_ROOT")

    def test_opencode_change_mode_denies_external_directories(self):
        """A project opencode.json turned external_directory from ask into allow (reproduced)."""
        from tabularium.engine.workers.cli.opencode_worker import OpenCodeCLIWorker
        worker = OpenCodeCLIWorker(executable="/bin/true", cwd=str(self.root), change_mode=True)
        self.assertEqual(worker.permissions.get("external_directory"), "deny")
        self.assertEqual(worker.permissions.get("bash"), "deny")
        # Allowlist: unnamed tools (task subagents, webfetch, …) are denied.
        self.assertEqual(worker.permissions.get("*"), "deny")
        self.assertEqual({k for k, v in worker.permissions.items() if v == "allow"},
                         {"read", "list", "glob", "grep", "edit"})


class TestGateAgentConfig(TestGatePathCanonicalization):

    def test_agent_configuration_is_protected(self):
        for path in ("opencode.json", "opencode.jsonc", "sub/opencode.json",
                     ".opencode/agent.md", ".claude/settings.json", ".codex/config.toml",
                     ".CLAUDE/settings.json"):
            with self.subTest(path=path):
                allowed, _ = self._allowed(path)
                self.assertFalse(allowed, f"{path} was allowed")


class TestAutoCommit(ChangePathFixture):
    """Tabularium's decision (b): Tabularium commits the approved change with its records."""

    def _git(self, *args):
        import subprocess
        return subprocess.run(["git", *args], cwd=str(self.root), capture_output=True,
                              text=True, check=True).stdout

    def _change(self, content, confirm=lambda _: True, instruction="Add a comment"):
        def mutate(wt: Path):
            (wt / "src" / "sample.py").write_text(content)
        return run_instruction(instruction, self.root, adapter=FakeWorkerAdapter(mutate_fn=mutate),
                               change=True, confirm=confirm)

    def test_applied_change_is_committed_with_its_records(self):
        head = self._git("rev-parse", "HEAD").strip()
        result = self._change("def calculate_total(a, b):\n    # one\n    return a + b\n")
        self.assertEqual(result.code, "COMPLETED", result.message)
        self.assertEqual(self._git("rev-parse", "HEAD~1").strip(), head)
        files = set(self._git("show", "--name-only", "--format=", "HEAD").split())
        task_files = {f for f in files if f.startswith(".ai/tasks/")}
        report_files = {f for f in files if f.startswith(".ai/reports/")}
        self.assertEqual(files - task_files - report_files, {"src/sample.py"})
        self.assertEqual(len(task_files), 1)
        self.assertEqual(len(report_files), 1)
        msg = self._git("log", "-1", "--format=%B")
        self.assertIn("Task: TASK-", msg)
        self.assertIn("Approved-by:", msg)
        self.assertIn("Diff-digest:", msg)
        self.assertEqual(result.change_result["commit"], self._git("rev-parse", "HEAD").strip())

    def test_second_change_is_not_blocked_by_the_first(self):
        first = self._change("def calculate_total(a, b):\n    # one\n    return a + b\n")
        self.assertEqual(first.code, "COMPLETED", first.message)
        second = self._change("def calculate_total(a, b):\n    # two\n    return a + b\n")
        self.assertEqual(second.code, "COMPLETED", second.message)

    def test_users_other_staged_change_is_not_committed(self):
        extra = self.root / ".ai" / "questions" / "mine.md"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text("my own note\n")
        self._git("add", str(extra))
        result = self._change("def calculate_total(a, b):\n    # one\n    return a + b\n")
        self.assertEqual(result.code, "COMPLETED", result.message)
        self.assertNotIn(".ai/questions/mine.md", self._git("show", "--name-only", "--format=", "HEAD"))
        self.assertIn(".ai/questions/mine.md", self._git("diff", "--cached", "--name-only"))

    def test_failing_commit_hook_keeps_change_and_warns(self):
        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho 'blocked by hook' >&2\nexit 1\n")
        hook.chmod(0o755)
        head = self._git("rev-parse", "HEAD").strip()
        content = "def calculate_total(a, b):\n    # one\n    return a + b\n"
        result = self._change(content)
        self.assertEqual(result.status, "COMPLETED")
        self.assertIsNone(result.change_result["commit"])
        self.assertTrue(any("not committed" in w for w in result.warnings), result.warnings)
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), head)
        self.assertEqual(self.src_file.read_text(), content)

    def test_rejected_change_makes_no_commit(self):
        head = self._git("rev-parse", "HEAD").strip()
        self._change("def calculate_total(a, b):\n    # one\n    return a + b\n",
                     confirm=lambda _: False)
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), head)


class TestChangeSetIntegration(ChangePathFixture):
    """V2-04: the --change path applies multi-file change sets all-or-nothing."""

    def _git(self, *args):
        import subprocess
        return subprocess.run(["git", *args], cwd=str(self.root), capture_output=True,
                              text=True, check=True).stdout

    def _two_files(self, wt: Path):
        (wt / "src" / "sample.py").write_text("def calculate_total(a, b):\n    return a + b + 0\n")
        (wt / "tests").mkdir(exist_ok=True)
        (wt / "tests" / "test_sample.py").write_text("def test_total():\n    assert True\n")

    def test_every_file_of_the_change_set_is_committed(self):
        result = run_instruction("function and its test", self.root,
                                 adapter=FakeWorkerAdapter(mutate_fn=self._two_files),
                                 change=True, confirm=lambda _: True)
        self.assertEqual(result.code, "COMPLETED", result.message)
        files = set(self._git("show", "--name-only", "--format=", "HEAD").split())
        self.assertIn("src/sample.py", files)
        self.assertIn("tests/test_sample.py", files)

    def test_apply_is_journaled_in_the_git_dir(self):
        import os as _os
        from unittest.mock import patch
        from tabularium.engine.code_gate import changeset

        journal = self.root / ".git" / "tabu-apply.journal"
        seen = []
        real_replace = _os.replace

        def spy(src, dst, **kwargs):
            # Only the change-set files; Task and report writes happen later.
            if str(dst).endswith(("src/sample.py", "tests/test_sample.py")):
                seen.append(journal.exists())
            return real_replace(src, dst, **kwargs)

        with patch.object(changeset.os, "replace", side_effect=spy):
            result = run_instruction("function and its test", self.root,
                                     adapter=FakeWorkerAdapter(mutate_fn=self._two_files),
                                     change=True, confirm=lambda _: True)
        self.assertEqual(result.code, "COMPLETED", result.message)
        self.assertEqual(seen, [True, True])
        self.assertFalse(journal.exists())

    def test_interrupted_apply_blocks_the_next_run_before_the_worker(self):
        journal = self.root / ".git" / "tabu-apply.journal"
        journal.write_text('{"files": []}')
        worker = FakeWorkerAdapter(mutate_fn=self._two_files)
        result = run_instruction("x", self.root, adapter=worker, change=True,
                                 confirm=lambda _: True)
        self.assertEqual(result.code, "INTERRUPTED_APPLY")
        self.assertEqual(worker.call_count, 0)
        self.assertTrue(journal.exists())


class TestChangePathDiffAndOpencodeIntegration(ChangePathFixture):
    """Integration tests for diff prefixes, commit hooks, and opencode permissions."""

    def _git(self, *args):
        import subprocess
        return subprocess.run(["git", *args], cwd=str(self.root), capture_output=True,
                              text=True, check=True).stdout

    def test_user_diff_noprefix_does_not_move_files(self):
        """With diff.noprefix the parser would strip a real "b/" directory."""
        self._git("config", "diff.noprefix", "true")

        def mutate(wt: Path):
            (wt / "b").mkdir()
            (wt / "b" / "x.py").write_text("x = 1\n")

        result = run_instruction("x", self.root, adapter=FakeWorkerAdapter(mutate_fn=mutate),
                                 change=True, confirm=lambda _: True)
        self.assertEqual(result.code, "COMPLETED", result.message)
        self.assertEqual((self.root / "b" / "x.py").read_text(), "x = 1\n")
        self.assertFalse((self.root / "x.py").exists())

    def test_failed_commit_leaves_the_index_unstaged(self):
        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)

        def mutate(wt: Path):
            (wt / "src" / "sample.py").write_text("changed\n")

        result = run_instruction("x", self.root, adapter=FakeWorkerAdapter(mutate_fn=mutate),
                                 change=True, confirm=lambda _: True)
        self.assertIsNone(result.change_result["commit"])
        self.assertEqual(self._git("diff", "--cached", "--name-only").strip(), "")
        self.assertEqual(self.src_file.read_text(), "changed\n")

    def test_opencode_ignores_project_config_and_names_denied_tools(self):
        import json
        from unittest.mock import MagicMock, patch
        from tabularium.engine.workers import WorkerRequest
        from tabularium.engine.workers.cli.bounded_process import Capture
        from tabularium.engine.workers.cli.opencode_worker import OpenCodeCLIWorker

        with patch("tabularium.engine.workers.cli.opencode_worker.BoundedProcess") as bp:
            bp.return_value = MagicMock(run=MagicMock(return_value=Capture("EXITED", 0, "")))
            OpenCodeCLIWorker(executable="/bin/true", cwd=str(self.root), change_mode=True).execute(
                WorkerRequest(request_id="r", intent="t", payload={}))
            env = bp.call_args.kwargs["env"]
        self.assertEqual(env.get("OPENCODE_DISABLE_PROJECT_CONFIG"), "1")
        perms = json.loads(env["OPENCODE_PERMISSION"])
        for tool in ("task", "webfetch", "websearch", "skill", "question", "bash",
                     "external_directory"):
            self.assertEqual(perms.get(tool), "deny", tool)

    def test_real_opencode_keeps_tools_closed_with_hostile_project_config(self):
        """Uses `opencode debug agent build` (no model call); skipped without opencode."""
        import json
        import shutil
        import subprocess
        from tabularium.engine.workers.cli.opencode_worker import OpenCodeCLIWorker
        from tabularium.engine.workers.cli.transport import _environment

        exe = shutil.which("opencode")
        if not exe:
            self.skipTest("opencode not installed")
        (self.root / "opencode.json").write_text(json.dumps({
            "permission": {"task": "allow", "webfetch": "allow", "bash": "allow",
                           "external_directory": "allow"},
            "agent": {"build": {"permission": {"task": "allow", "webfetch": "allow"}}}}))
        worker = OpenCodeCLIWorker(executable=exe, cwd=str(self.root), change_mode=True)
        env = _environment()
        env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
        env["OPENCODE_PERMISSION"] = json.dumps(worker.permissions)
        try:
            out = subprocess.run([exe, "debug", "agent", "build", "--pure"], cwd=str(self.root),
                                 env=env, capture_output=True, text=True, timeout=60,
                                 stdin=subprocess.DEVNULL)
            tools = json.loads(out.stdout).get("tools", {})
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
            self.skipTest(f"opencode debug unavailable: {type(exc).__name__}")
        for tool in ("task", "webfetch", "bash", "websearch", "skill"):
            self.assertFalse(tools.get(tool), tool)


class TestChangePathCommitBoundary(ChangePathFixture):
    """Integrity tests for special characters in commit paths and journal availability."""

    def _git(self, *args):
        import subprocess
        return subprocess.run(["git", *args], cwd=str(self.root), capture_output=True,
                              text=True, check=True).stdout

    def test_glob_like_file_name_commits_only_itself(self):
        """A worker file named "*" made git commit the user's untracked files too."""
        secret = self.root / "notes-untracked.txt"
        secret.write_text("SECRET\n")

        def mutate(wt: Path):
            (wt / "*").write_text("star\n")

        result = run_instruction("x", self.root, adapter=FakeWorkerAdapter(mutate_fn=mutate),
                                 change=True, confirm=lambda _: True)
        self.assertEqual(result.code, "COMPLETED", result.message)
        files = set(self._git("show", "--name-only", "--format=", "HEAD").split("\n")) - {""}
        self.assertNotIn("notes-untracked.txt", files)
        self.assertIn("*", files)
        self.assertIn("notes-untracked.txt", self._git("status", "--porcelain", "--untracked-files=all"))

    def test_missing_journal_location_refuses_before_the_worker(self):
        from unittest.mock import patch
        worker = FakeWorkerAdapter(mutate_fn=lambda wt: None)
        with patch("tabularium.change_path.apply_journal_path", return_value=None):
            result = run_instruction("x", self.root, adapter=worker, change=True,
                                     confirm=lambda _: True)
        self.assertEqual(result.code, "JOURNAL_UNAVAILABLE")
        self.assertEqual(worker.call_count, 0)
