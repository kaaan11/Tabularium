"""Characterization tests for change-flow approval and apply ordering."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from tabularium import change_path
from tabularium.engine.code_gate.isolated_run import IsolatedRun
from tabularium.execution import run_instruction
from tests.test_v2_09_change_path import ChangePathFixture, FakeWorkerAdapter


class TestChangePathAuthorizationOrdering(ChangePathFixture):
    def test_tests_approval_cas_and_bound_authorization_precede_apply(self):
        """The tested proposal is confirmed once, rechecked, then applied under its exact binding."""
        (self.root / ".ai" / "change.yaml").write_text(
            'test_command: ["not-run-directly"]\ntimeout_seconds: 30\n',
            encoding="utf-8",
        )
        events: list[str] = []
        check_count = 0
        apply_args = []

        def mutate(worktree):
            events.append("worker")
            (worktree / "src" / "sample.py").write_text(
                "def calculate_total(a, b):\n    return a * b\n", encoding="utf-8"
            )

        real_check_git_clean = change_path.check_git_clean

        def check_git_clean(target):
            nonlocal check_count
            check_count += 1
            events.append("preflight" if check_count == 1 else "cas")
            return real_check_git_clean(target)

        def run_tests(*args, **kwargs):
            events.append("tests")
            self.assertEqual(args[1], {
                "src/sample.py": "def calculate_total(a, b):\n    return a * b\n"
            })
            return IsolatedRun(0, "", "", False, False)

        real_apply = change_path.apply_changeset_transactional

        def apply(target, proposal, authorization, **kwargs):
            events.append("apply")
            apply_args.append((proposal, authorization))
            return real_apply(target, proposal, authorization, **kwargs)

        def confirm(_prompt):
            events.append("approval")
            return True

        with patch.object(change_path, "check_git_clean", side_effect=check_git_clean), \
                patch.object(change_path, "run_tests_on_copy", side_effect=run_tests), \
                patch.object(change_path, "apply_changeset_transactional", side_effect=apply):
            result = run_instruction(
                "Multiply the total",
                self.root,
                task_id=self.task_id,
                adapter=FakeWorkerAdapter(mutate_fn=mutate),
                change=True,
                allow_unconfined_worker=True,
                confirm=confirm,
            )

        self.assertEqual(result.code, "COMPLETED", result.message)
        self.assertEqual(events, ["preflight", "worker", "tests", "approval", "cas", "apply"])
        self.assertEqual(len(apply_args), 1)
        proposal, authorization = apply_args[0]
        self.assertEqual(authorization.proposal_id, proposal.proposal_id)
        self.assertEqual(authorization.execution_id, proposal.execution_id)
        self.assertEqual(authorization.diff_digest, proposal.diff_digest)
        self.assertEqual(authorization.binding, proposal.binding())
        self.assertEqual(authorization.authorized_by, "Tabularium Tester")
        self.assertTrue(authorization.authorized_at)


def test_facade_worktree_git_patch_intercepts_calls_without_running_git(tmp_path):
    dotgit = b"gitdir: private-git-dir\n"
    worktree_path = tmp_path / "wt"
    worktree_path.mkdir()
    (worktree_path / ".git").write_bytes(dotgit)
    worktree = change_path.Worktree(
        tmp_path, worktree_path, tmp_path / "private-git-dir", dotgit
    )

    with patch("tabularium.change_path._worktree_git",
               side_effect=["", "captured diff"]) as git_helper, \
            patch("tabularium.change_path.subprocess.run",
                  side_effect=AssertionError("real git call was attempted")) as git_run:
        diff = change_path.compute_worktree_diff(worktree, "base-commit")

    assert diff == "captured diff"
    assert git_helper.call_count == 2
    assert git_helper.call_args_list[0].args[1:] == ("add", "-A")
    assert git_helper.call_args_list[0].kwargs == {"attr_source": "base-commit"}
    assert git_helper.call_args_list[1].args[1:] == (
        "diff", "--cached", "--no-ext-diff", "--no-textconv", "--src-prefix=a/",
        "--dst-prefix=b/", "base-commit",
    )
    assert git_helper.call_args_list[1].kwargs == {"attr_source": "base-commit"}
    git_run.assert_not_called()


def test_facade_unsupported_diff_markers_patch_changes_reason():
    raw_diff = "blocked marker: test input"

    with patch("tabularium.change_path._UNSUPPORTED_DIFF_MARKERS", ("blocked marker:",)):
        reason = change_path.unsupported_diff_reason(raw_diff, parsed_count=0)

    assert reason == "unsupported change: blocked marker: test input"
