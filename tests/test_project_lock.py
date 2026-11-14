"""Project-wide mutation serialization and recovery (issue #4).

Concurrency tests use independent spawned processes synchronized through files.
The first change worker waits until the contender either enters its worker
(the regression) or returns a refusal. Other tests cover thread ownership,
lock I/O errors, and cleanup on refusal or interruption.

Fake-worker changes without a test command explicitly opt in to untested runs;
the configured-test path and missing-config refusal are covered separately.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import signal
import subprocess
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tabularium.change_path import apply_journal_path
from tabularium.execution import (
    ProductResult, apply_execution_report_proposal, build_execution_report_proposal,
    continue_execution, run_instruction,
)
from tabularium.engine.execution import PipelineResult
from tabularium import task
from tabularium.diagnostics import doctor_project, status_project
from tabularium.init import initialize
from tabularium.onboarding import onboard_project

from tests.test_v2_09_change_path import ChangePathFixture, FakeWorkerAdapter

_WAIT_S = 60.0


def _wait_for(predicate, timeout: float = _WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _change_child(root: str, sync_dir: str, name: str, other: str) -> None:
    """One `--change` run in its own process; writes `<name>.json` and `<name>.done`."""
    sync = Path(sync_dir)
    observed: dict[str, bool] = {"entered": False, "saw_other_inside": False}

    def mutate(worktree: Path) -> None:
        observed["entered"] = True
        (sync / f"{name}.in").touch()
        if not _wait_for(lambda: (sync / f"{other}.in").exists() or (sync / f"{other}.done").exists()):
            raise AssertionError("competing process never entered or returned")
        observed["saw_other_inside"] = (sync / f"{other}.in").exists()
        (worktree / "src" / "sample.py").write_text(
            f"def calculate_total(a, b):\n    # {name}\n    return a + b\n", encoding="utf-8")

    try:
        result = run_instruction(f"Change from {name}", root,
                                 adapter=FakeWorkerAdapter(mutate_fn=mutate),
                                 change=True, allow_untested=True, confirm=lambda _prompt: True)
        payload = {"status": result.status, "code": result.code,
                   "message": result.message, **observed}
    except BaseException as exc:  # reported, never swallowed silently
        payload = {"status": "CRASHED", "code": type(exc).__name__,
                   "message": str(exc), **observed}
    (sync / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    (sync / f"{name}.done").touch()


def _hanging_child(root: str, sync_dir: str) -> None:
    """A `--change` whose worker never returns; the parent kills this process."""
    sync = Path(sync_dir)

    def mutate(worktree: Path) -> None:
        (sync / "holder.in").touch()
        time.sleep(_WAIT_S * 10)

    run_instruction("Change that is killed", root,
                    adapter=FakeWorkerAdapter(mutate_fn=mutate),
                    change=True, allow_untested=True, confirm=lambda _prompt: True)


def _task_holder_child(root: str, sync_dir: str) -> None:
    sync = Path(sync_dir)
    allocate = task.allocate_task_id

    def wait_then_allocate(*args, **kwargs):
        (sync / "task.in").touch()
        if not _wait_for(lambda: (sync / "release").exists()):
            raise AssertionError("parent never released Task creation")
        return allocate(*args, **kwargs)

    with patch.object(task, "allocate_task_id", side_effect=wait_then_allocate):
        result = task.task_create(root, title="Holder", objective="Hold allocation")
    (sync / "task.json").write_text(json.dumps(result.to_dict()), encoding="utf-8")


def _task_probe_child(root: str, sync_dir: str) -> None:
    result = task.task_create(root, title="Probe", objective="Check lock scope")
    (Path(sync_dir) / "probe.json").write_text(json.dumps(result.to_dict()), encoding="utf-8")


class ProjectLockFixture(ChangePathFixture):
    def setUp(self) -> None:
        super().setUp()
        self.sync = self.root.parent / f"{self.root.name}-sync"
        self.sync.mkdir()
        self.addCleanup(shutil.rmtree, self.sync, True)
        self.ctx = multiprocessing.get_context("spawn")

    def tearDown(self) -> None:
        if (self.root / ".git").is_dir():
            self.remove_leftover_worktrees()
        super().tearDown()

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=str(self.root), capture_output=True,
                              text=True, check=True).stdout

    def remove_leftover_worktrees(self) -> None:
        for line in self.git("worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                path = Path(line.split(" ", 1)[1])
                if path != self.root and path.name == "wt":
                    shutil.rmtree(path.parent, ignore_errors=True)
        self.git("worktree", "prune")

    def owner_path(self) -> Path:
        journal = apply_journal_path(self.root)
        assert journal is not None
        return journal.with_name("tabu-project.owner")

    def start_child(self, target, args):
        proc = self.ctx.Process(target=target, args=args)
        proc.start()

        def stop():
            if proc.is_alive():
                proc.kill()
            proc.join(_WAIT_S)

        self.addCleanup(stop)
        return proc

    def assert_busy_probe(self):
        proc = self.start_child(_task_probe_child, (str(self.root), str(self.sync)))
        proc.join(_WAIT_S)
        self.assertFalse(proc.is_alive(), "contender hung")
        self.assertEqual(proc.exitcode, 0)
        result = json.loads((self.sync / "probe.json").read_text())
        self.assertEqual(result.get("code"), "PROJECT_BUSY", result)
        self.assertFalse(result["created"], result)


class TestConcurrentChange(ProjectLockFixture):

    def test_two_processes_cannot_both_enter_the_change(self):
        first = self.start_child(_change_child, (str(self.root), str(self.sync), "A", "B"))
        self.assertTrue(_wait_for(lambda: (self.sync / "A.in").exists()))
        second = self.start_child(_change_child, (str(self.root), str(self.sync), "B", "A"))
        procs = [first, second]
        for proc in procs:
            proc.join(_WAIT_S * 2)
            self.assertFalse(proc.is_alive(), "child process hung")
            self.assertEqual(proc.exitcode, 0)

        results = {name: json.loads((self.sync / f"{name}.json").read_text(encoding="utf-8"))
                   for name in ("A", "B")}
        entered = [name for name, res in results.items() if res["entered"]]
        self.assertFalse(any(res["saw_other_inside"] for res in results.values()),
                         f"both runs were inside the change at once: {results}")
        self.assertEqual(len(entered), 1, results)

        winner = entered[0]
        loser = "B" if winner == "A" else "A"
        self.assertEqual((results[winner]["status"], results[winner]["code"]),
                         ("COMPLETED", "COMPLETED"), results)
        self.assertEqual((results[loser]["status"], results[loser]["code"]),
                         ("REFUSED", "PROJECT_BUSY"), results)
        # Actionable diagnostic: who holds the project.
        self.assertIn("another project mutation", results[loser]["message"])
        self.assertIn("pid", results[loser]["message"])

        # Exactly the winner's change landed and was committed once.
        self.assertIn(f"# {winner}", self.src_file.read_text(encoding="utf-8"))
        subjects = self.git("log", "--format=%s").splitlines()
        self.assertEqual([s for s in subjects if s.startswith("tabu: ")],
                         [f"tabu: Change from {winner}"])
        # The refused run allocated no Task and left no lock record behind.
        tasks = sorted(p.name for p in (self.root / ".ai" / "tasks").glob("TASK-*.md"))
        self.assertEqual(len(tasks), 2, tasks)  # fixture task + winner's candidate
        self.assertFalse(self.owner_path().exists())

    def test_killed_holder_leaves_a_stale_lock_that_is_reported_not_broken(self):
        proc = self.start_child(_hanging_child, (str(self.root), str(self.sync)))
        try:
            self.assertTrue(_wait_for(lambda: (self.sync / "holder.in").exists()),
                            "holder never reached its worker")
            # While the holder lives the project is busy.
            busy = run_instruction("Concurrent change", self.root,
                                   adapter=FakeWorkerAdapter(), change=True,
                                   confirm=lambda _prompt: True)
            self.assertEqual((busy.status, busy.code), ("REFUSED", "PROJECT_BUSY"))
            self.assertIn(f"pid {proc.pid}", busy.message)
        finally:
            os.kill(proc.pid, signal.SIGKILL)
            proc.join(_WAIT_S)
        self.assertEqual(proc.exitcode, -signal.SIGKILL)

        owner = self.owner_path()
        self.assertTrue(owner.exists(), "a killed holder cannot remove its record")
        record = owner.read_bytes()

        calls: list[int] = []
        new_content = "def calculate_total(a, b):\n    # after recovery\n    return a + b\n"

        def mutate(worktree: Path) -> None:
            calls.append(1)
            (worktree / "src" / "sample.py").write_text(new_content, encoding="utf-8")

        stale = run_instruction("Change after crash", self.root,
                                adapter=FakeWorkerAdapter(mutate_fn=mutate), change=True,
                                confirm=lambda _prompt: True)
        self.assertEqual((stale.status, stale.code), ("REFUSED", "PROJECT_LOCK_STALE"))
        self.assertIn(f"pid {proc.pid}", stale.message)
        self.assertIn(str(owner), stale.message)
        self.assertEqual(calls, [], "no work may start while the stale record is unresolved")
        self.assertEqual(owner.read_bytes(), record, "the stale record is never removed for the user")
        self.assertEqual(self.src_file.read_text(encoding="utf-8"), self.initial_content)

        # Refusing again is deterministic: nothing was cleared implicitly.
        again = run_instruction("Change after crash", self.root,
                                adapter=FakeWorkerAdapter(mutate_fn=mutate), change=True,
                                confirm=lambda _prompt: True)
        self.assertEqual(again.code, "PROJECT_LOCK_STALE")

        # Recovery is the documented manual step: inspect, then delete the record.
        journal = apply_journal_path(self.root)
        journal.write_text('{"interrupted": true}', encoding="utf-8")
        owner.unlink()
        interrupted = run_instruction("Change after crash", self.root,
                                      adapter=FakeWorkerAdapter(mutate_fn=mutate), change=True,
                                      confirm=lambda _prompt: True)
        self.assertEqual(interrupted.code, "INTERRUPTED_APPLY")
        self.assertIn(str(journal), interrupted.message)
        self.assertEqual(calls, [])
        self.assertTrue(journal.exists(), "journal recovery must remain manual")
        self.remove_leftover_worktrees()
        journal.unlink()
        candidates = [item for item in task.task_list(self.root).tasks if item.id != self.task_id]
        self.assertEqual(len(candidates), 1)
        cancelled = task.task_transition(self.root, candidates[0].id, "CANCELLED", force=True)
        self.assertTrue(cancelled.updated, cancelled)
        recovered = run_instruction("Change after crash", self.root,
                                    adapter=FakeWorkerAdapter(mutate_fn=mutate), change=True,
                                    allow_untested=True, confirm=lambda _prompt: True)
        self.assertEqual((recovered.status, recovered.code), ("COMPLETED", "COMPLETED"),
                         recovered.message)
        self.assertEqual(calls, [1])
        self.assertEqual(self.src_file.read_text(encoding="utf-8"), new_content)
        self.assertFalse(owner.exists())

    def test_task_creation_excludes_all_mutations_but_allows_diagnostics(self):
        proc = self.start_child(_task_holder_child, (str(self.root), str(self.sync)))
        self.assertTrue(_wait_for(lambda: (self.sync / "task.in").exists()))
        before = {p.name: p.read_bytes() for p in (self.root / ".ai" / "tasks").iterdir()}
        operations = [
            lambda: run_instruction("Competing change", self.root, change=True,
                                    adapter=FakeWorkerAdapter(), confirm=lambda _: True),
            lambda: run_instruction("Competing run", self.root, adapter=FakeWorkerAdapter()),
            lambda: continue_execution(self.root),
            lambda: task.task_create(self.root, title="Other", objective="Other"),
            lambda: task.task_start(self.root, self.task_id),
            lambda: task.task_approve(self.root, self.task_id),
            lambda: task.task_complete(self.root, self.task_id),
            lambda: task.task_transition(self.root, self.task_id, "REVIEW"),
            lambda: initialize(self.root),
            lambda: onboard_project(self.root, authorizations={"nonempty": True}),
        ]
        try:
            for operation in operations:
                result = operation()
                self.assertEqual(result.code, "PROJECT_BUSY", result)
                self.assertIn(f"pid {proc.pid}", result.message)
            self.assertEqual(before, {p.name: p.read_bytes()
                                     for p in (self.root / ".ai" / "tasks").iterdir()})
            self.assertEqual(len(task.task_list(self.root).tasks), 1)
            self.assertIsNone(task.task_show(self.root, self.task_id).code)
            self.assertIsNone(task.task_validate(self.root).code)
            self.assertIsNone(task.task_create(self.root, title="Preview", objective="Preview",
                                              print_only=True).code)
            self.assertIsNone(status_project(self.root).error_code)
            self.assertIsNone(doctor_project(self.root).error_code)
        finally:
            (self.sync / "release").touch()
            proc.join(_WAIT_S)
        self.assertEqual(proc.exitcode, 0)
        self.assertTrue(json.loads((self.sync / "task.json").read_text())["created"])

    def test_linked_worktree_uses_the_same_project_lock(self):
        linked = self.sync / "linked"
        self.git("worktree", "add", "--detach", str(linked), "HEAD")
        proc = self.start_child(_task_holder_child, (str(self.root), str(self.sync)))
        try:
            self.assertTrue(_wait_for(lambda: (self.sync / "task.in").exists()))
            result = run_instruction("Linked change", linked, adapter=FakeWorkerAdapter(),
                                     change=True, confirm=lambda _: True)
            self.assertEqual(result.code, "PROJECT_BUSY", result)
        finally:
            (self.sync / "release").touch()
            proc.join(_WAIT_S)

    def test_non_git_project_task_mutations_are_serialized(self):
        shutil.rmtree(self.root / ".git")
        proc = self.start_child(_task_holder_child, (str(self.root), str(self.sync)))
        try:
            self.assertTrue(_wait_for(lambda: (self.sync / "task.in").exists()))
            result = task.task_approve(self.root, self.task_id)
            self.assertEqual(result.code, "PROJECT_BUSY", result)
        finally:
            (self.sync / "release").touch()
            proc.join(_WAIT_S)
        self.assertEqual(proc.exitcode, 0)


class TestLockReleaseInProcess(ProjectLockFixture):

    def test_lock_spans_preflight_tests_approval_apply_persistence_commit_and_cleanup(self):
        from tabularium import change_path
        from tabularium.engine.persistence import writer
        from tabularium.engine.code_gate.isolated_run import IsolatedRun

        phases = []
        (self.root / ".ai" / "change.yaml").write_text(
            'test_command: ["python3", "-c", "pass"]\n', encoding="utf-8")

        def probe_before(name, original):
            def call(*args, **kwargs):
                phases.append(name)
                self.assert_busy_probe()
                return original(*args, **kwargs)
            return call

        def confirm(_prompt):
            phases.append("approval")
            self.assert_busy_probe()
            return True

        def mutate(worktree):
            (worktree / "src" / "sample.py").write_text("x = 1\n", encoding="utf-8")

        with patch.object(change_path, "check_git_clean",
                          side_effect=probe_before("preflight", change_path.check_git_clean)), \
             patch.object(change_path, "run_tests_on_copy", side_effect=probe_before(
                 "tests", lambda *a, **kw: IsolatedRun(0, "", "", False, False))), \
             patch.object(change_path, "apply_changeset_transactional",
                          side_effect=probe_before("apply", change_path.apply_changeset_transactional)), \
             patch.object(writer, "apply_authorized_proposal",
                          side_effect=probe_before("report", writer.apply_authorized_proposal)), \
             patch.object(change_path, "commit_applied_change",
                          side_effect=probe_before("commit", change_path.commit_applied_change)), \
             patch.object(change_path, "cleanup_worktree",
                          side_effect=probe_before("cleanup", change_path.cleanup_worktree)):
            result = run_instruction("Probe all phases", self.root, change=True,
                                     adapter=FakeWorkerAdapter(mutate_fn=mutate), confirm=confirm)
        self.assertEqual(result.code, "COMPLETED", result)
        self.assertEqual(list(dict.fromkeys(phases)),
                         ["preflight", "tests", "approval", "apply", "report", "commit", "cleanup"])
        self.assertFalse(self.owner_path().exists())

    def test_other_thread_cannot_reenter_the_owning_threads_lock(self):
        def mutate(worktree):
            with ThreadPoolExecutor(max_workers=1) as pool:
                contender = pool.submit(task.task_approve, self.root, self.task_id).result(_WAIT_S)
            self.assertEqual(contender.code, "PROJECT_BUSY", contender)
            (worktree / "src" / "sample.py").write_text("x = 1\n", encoding="utf-8")

        result = run_instruction("Thread contention", self.root, change=True,
                                 adapter=FakeWorkerAdapter(mutate_fn=mutate),
                                 allow_untested=True, confirm=lambda _: True)
        self.assertEqual(result.code, "COMPLETED", result)

    def test_standalone_report_persistence_is_refused_while_project_busy(self):
        pipeline = PipelineResult(status="COMPLETED", execution_id="probe-report",
                                  task_id=self.task_id,
                                  worker_result={"worker_id": "fake-cli-worker",
                                                 "worker_category": "CLI_CODING"},
                                  output="Verified offline result.", reason_codes=(), diagnostics=())
        result = ProductResult("COMPLETED", "COMPLETED", "Work completed.", str(self.root),
                               pipeline=pipeline)
        proposal = build_execution_report_proposal(self.root, result, author="tester").proposal
        proc = self.start_child(_task_holder_child, (str(self.root), str(self.sync)))
        try:
            self.assertTrue(_wait_for(lambda: (self.sync / "task.in").exists()))
            refused = apply_execution_report_proposal(
                self.root, proposal, authorized_by="tester", authorized_at="2026-11-14T00:00:00Z")
            self.assertEqual(refused.status, "DENIED")
            self.assertEqual(refused.reason_codes, ("PROJECT_BUSY",))
            self.assertFalse((self.root / proposal.target_path).exists())
        finally:
            (self.sync / "release").touch()
            proc.join(_WAIT_S)
        persisted = apply_execution_report_proposal(
            self.root, proposal, authorized_by="tester", authorized_at="2026-11-14T00:00:00Z")
        self.assertTrue(persisted.written, persisted)

    def test_lock_io_failure_fails_closed_before_checkpoint_or_task_allocation(self):
        from tabularium import project_lock

        before = sorted((self.root / ".ai" / "tasks").iterdir())
        adapter = FakeWorkerAdapter()
        with patch.object(project_lock, "_try_lock", side_effect=OSError("flock unavailable")), \
             patch("tabularium.execution.prepare_checkpoint_storage") as prepare:
            result = run_instruction("No lock", self.root, adapter=adapter, change=True)
        self.assertEqual(result.code, "PROJECT_LOCK_UNAVAILABLE")
        prepare.assert_not_called()
        self.assertEqual(adapter.call_count, 0)
        self.assertEqual(before, sorted((self.root / ".ai" / "tasks").iterdir()))
        self.assertFalse(self.owner_path().exists())

    def test_malformed_stale_owner_is_preserved(self):
        self.owner_path().write_bytes(b"\xffinvalid owner")
        result = run_instruction("Malformed stale record", self.root,
                                 adapter=FakeWorkerAdapter(), change=True)
        self.assertEqual(result.code, "PROJECT_LOCK_STALE")
        self.assertIn("unreadable owner record", result.message)
        self.assertEqual(self.owner_path().read_bytes(), b"\xffinvalid owner")

    def test_owner_write_failure_refuses_without_mutating_the_project(self):
        before = {p.name: p.read_bytes() for p in (self.root / ".ai" / "tasks").iterdir()}
        adapter = FakeWorkerAdapter()
        with patch("os.fsync", side_effect=OSError("device full")):
            result = run_instruction("Owner write fails", self.root, adapter=adapter, change=True)
        self.assertEqual(result.code, "PROJECT_LOCK_UNAVAILABLE")
        self.assertEqual(adapter.call_count, 0)
        self.assertEqual(before, {p.name: p.read_bytes()
                                 for p in (self.root / ".ai" / "tasks").iterdir()})
        # Even an incomplete owner record is evidence requiring inspection.
        again = run_instruction("Retry owner write", self.root, adapter=adapter, change=True)
        self.assertEqual(again.code, "PROJECT_LOCK_STALE")

    def test_interrupt_inside_the_change_releases_the_lock(self):
        def interrupt(worktree: Path) -> None:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            run_instruction("Interrupted change", self.root,
                            adapter=FakeWorkerAdapter(mutate_fn=interrupt), change=True,
                            allow_untested=True, confirm=lambda _prompt: True)
        self.assertFalse(self.owner_path().exists())

        def mutate(worktree: Path) -> None:
            (worktree / "src" / "sample.py").write_text("x = 1\n", encoding="utf-8")

        result = run_instruction("Next change", self.root,
                                 adapter=FakeWorkerAdapter(mutate_fn=mutate), change=True,
                                 allow_untested=True, confirm=lambda _prompt: True)
        self.assertEqual((result.status, result.code), ("COMPLETED", "COMPLETED"), result.message)

    def test_missing_test_config_refusal_holds_then_releases_the_lock(self):
        from tabularium import change_path

        adapter = FakeWorkerAdapter()
        before = {p.name: p.read_bytes() for p in (self.root / ".ai" / "tasks").iterdir()}
        load_config = change_path.load_change_config

        def probe_then_load(target):
            self.assert_busy_probe()
            return load_config(target)

        with patch.object(change_path, "load_change_config", side_effect=probe_then_load), \
             patch("builtins.input") as confirm:
            result = run_instruction("No test config", self.root, adapter=adapter,
                                     change=True, confirm=confirm)
        self.assertEqual((result.status, result.code), ("REFUSED", "TESTS_NOT_CONFIGURED"))
        self.assertEqual(adapter.call_count, 0)
        confirm.assert_not_called()
        self.assertEqual(before, {p.name: p.read_bytes()
                                 for p in (self.root / ".ai" / "tasks").iterdir()})
        self.assertFalse(self.owner_path().exists())

        # An independent process must be able to acquire the released OS lock.
        proc = self.start_child(_task_probe_child, (str(self.root), str(self.sync)))
        proc.join(_WAIT_S)
        self.assertFalse(proc.is_alive(), "probe hung after refusal")
        self.assertEqual(proc.exitcode, 0)
        probe = json.loads((self.sync / "probe.json").read_text())
        self.assertTrue(probe["created"], probe)
        self.assertFalse(self.owner_path().exists())

    def test_refusals_inside_the_change_release_the_lock(self):
        rejected = run_instruction("Rejected change", self.root,
                                   adapter=FakeWorkerAdapter(
                                       mutate_fn=lambda wt: (wt / "src" / "sample.py")
                                       .write_text("y = 2\n", encoding="utf-8")),
                                   change=True, allow_untested=True, confirm=lambda _prompt: False)
        self.assertEqual(rejected.code, "APPROVAL_REJECTED")
        self.assertFalse(self.owner_path().exists())

    def test_non_git_target_is_refused_without_a_lock(self):
        shutil.rmtree(self.root / ".git")
        result = run_instruction("No repository", self.root, adapter=FakeWorkerAdapter(),
                                 change=True, confirm=lambda _prompt: True)
        self.assertEqual((result.status, result.code), ("REFUSED", "NOT_GIT_REPOSITORY"))

    def test_missing_git_preserves_plain_directory_init_but_refuses_repository_mutation(self):
        from tabularium import project_lock

        plain = self.sync / "plain"
        plain.mkdir()
        lexists = os.path.lexists

        def no_inherited_git_marker(path):
            # Model a plain directory independently of ambient ancestor markers
            # (this sandbox supplies a protected /tmp/.git directory).
            if Path(path).name == ".git" and Path(path).parent in plain.parents:
                return False
            return lexists(path)

        with patch.object(project_lock.os.path, "lexists", side_effect=no_inherited_git_marker), \
             patch.object(project_lock.subprocess, "run", side_effect=FileNotFoundError("git")):
            initialized = initialize(plain)
            refused = task.task_approve(self.root, self.task_id)
        self.assertEqual(initialized.status, "INITIALIZED", initialized)
        self.assertEqual(refused.code, "PROJECT_LOCK_UNAVAILABLE", refused)


if __name__ == "__main__":
    unittest.main()
