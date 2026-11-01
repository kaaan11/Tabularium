"""Deterministic acceptance tests for Tabularium V2-09 spike A: governed change path.

Tests cover all 10 acceptance criteria using a fake worker adapter behind
the `run_instruction` seam:
1. Single file change + 'y' -> target modified, worktree removed, report recorded.
2. Single file change + 'n' -> target byte-identical, worktree removed, decision 'ret'.
3. Protected path (.env/.git) -> gate refusal without asking confirmation, target byte-identical.
4. Multi-file change -> explicit refusal without asking confirmation, target byte-identical.
5. Dirty target project -> worker never called (call count == 0).
6. Target changed externally before apply -> apply refusal (TARGET_CHANGED), external modification preserved.
7. Worker raises exception -> worktree removed unconditionally.
8. Non-TTY stdin -> no apply, status AWAITING_APPROVAL.
9. Argv verification:
   - Native: workspace-write + cwd=worktree vs read-only + scratch.
   - OpenCode: --dir worktree + OPENCODE_PERMISSION bash denied.
10. OpenCode stderr permission line -> WORKER_PERMISSION_DENIED.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock, patch

from tabularium.change_path import (
    check_git_clean,
    cleanup_worktree,
    compute_worktree_diff,
    create_worktree,
    get_git_user_name,
    run_change_path,
)
from tabularium.execution import (
    ProductResult,
    build_execution_report_proposal,
    run_instruction,
)
from tabularium.init import initialize
from tabularium.onboarding import build_authorizations, onboard_project
from tabularium.engine.code_gate.model import (
    PROTECTED_TARGET_DENIED,
    TARGET_CHANGED,
)
from tabularium.engine.workers import (
    CLI_CODING,
    COMPLETED,
    FAILED,
    WorkerDescriptor,
    WorkerRequest,
    WorkerResult,
)
from tabularium.engine.workers.cli.bounded_process import Capture
from tabularium.engine.workers.cli.opencode_worker import OpenCodeCLIWorker
from tabularium.engine.workers.cli.worker import NativeCLIWorker
from tabularium.engine.workers.model import CANCELLABLE, STREAMS_OUTPUT, STRUCTURED_OUTPUT


def current_year() -> int:
    return datetime.now(timezone.utc).year


def hash_directory_bytes(root: Path) -> dict[str, str]:
    """sha256 of project files, excluding .git and the records Tabularium writes itself."""
    hashes = {}
    for path in sorted(root.rglob("*")):
        rel_parts = path.relative_to(root).parts
        if path.is_file() and ".git" not in rel_parts \
                and rel_parts[:2] not in ((".ai", "tasks"), (".ai", "reports")):
            rel = str(path.relative_to(root))
            hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


class FakeWorkerAdapter:
    """Deterministic fake worker adapter that writes to its assigned cwd."""

    def __init__(
        self,
        mutate_fn: Callable[[Path], None] | None = None,
        status: str = COMPLETED,
        output: str = "Fake worker finished successfully.",
        raise_exc: Exception | None = None,
    ) -> None:
        self.descriptor = WorkerDescriptor(
            worker_id="fake-cli-worker",
            category=CLI_CODING,
            capabilities=(STREAMS_OUTPUT, STRUCTURED_OUTPUT, CANCELLABLE),
        )
        self.cwd: str | None = None
        self.call_count = 0
        self.mutate_fn = mutate_fn
        self.status = status
        self.output = output
        self.raise_exc = raise_exc

    def accepts(self, request: WorkerRequest) -> tuple[bool, tuple[str, ...]]:
        return True, ()

    def execute(self, request: WorkerRequest) -> WorkerResult:
        self.call_count += 1
        if self.raise_exc is not None:
            raise self.raise_exc

        if self.mutate_fn is not None and self.cwd is not None:
            self.mutate_fn(Path(self.cwd))

        return WorkerResult(
            request_id=request.request_id,
            status=self.status,
            output=self.output,
            worker_id=self.descriptor.worker_id,
        )


class ChangePathFixture(unittest.TestCase):
    """Sets up an initialized, onboarded, clean git repository with a single active Task."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="tabu-change-test-")
        self.root = Path(self.temp_dir.name).resolve()

        # 1. Initialize Tabularium
        initialize(self.root)

        # 2. Onboard project
        declarations = {
            "title": "Change Path Fixture",
            "purpose": "Governed change path verification",
            "scope": "Testing worktree and gate pipeline",
        }
        draft = onboard_project(self.root, declarations)
        onboard_res = onboard_project(
            self.root,
            declarations,
            build_authorizations(draft.proposal, "human:user", "2026-01-01T00:00:00+00:00"),
        )
        self.assertEqual(onboard_res.status, "ONBOARDED")

        # 3. Create active task
        self.task_id = f"TASK-{current_year():04d}-0001"
        tasks_dir = self.root / ".ai" / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        task_file = tasks_dir / f"{self.task_id}.md"
        task_file.write_text(
            f"""---
schema_version: "1.10"
id: {self.task_id}
title: "Test change path"
objective: "Validate change path"
status: "IN_PROGRESS"
created_at: "2026-10-29T00:00:00Z"
created_by: "USER"
---
# Test Change Path Task
Initial description.
""",
            encoding="utf-8",
        )

        (self.root / ".ai" / "reports").mkdir(parents=True, exist_ok=True)

        # 4. Create sample project source file
        self.src_file = self.root / "src" / "sample.py"
        self.src_file.parent.mkdir(parents=True, exist_ok=True)
        self.initial_content = "def calculate_total(a, b):\n    return a + b\n"
        self.src_file.write_text(self.initial_content, encoding="utf-8")

        # 5. Initialize git repository and commit initial state
        subprocess.run(["git", "init"], cwd=str(self.root), capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.name", "Tabularium Tester"],
            cwd=str(self.root),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "user@example.com"],
            cwd=str(self.root),
            capture_output=True,
            check=True,
        )
        subprocess.run(["git", "add", "-A"], cwd=str(self.root), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=str(self.root),
            capture_output=True,
            check=True,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def list_worktrees(self) -> list[str]:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            check=True,
        )
        lines = [line.split(" ")[1] for line in proc.stdout.splitlines() if line.startswith("worktree ")]
        return lines


class TestAcceptanceChangePath(ChangePathFixture):

    def test_01_single_file_change_approved(self):
        """1. Single file change + 'y' -> target file has expected content, worktree removed, report recorded."""
        new_content = "def calculate_total(a, b):\n    # verified addition\n    return a + b\n"

        def mutate(wt_dir: Path):
            (wt_dir / "src" / "sample.py").write_text(new_content, encoding="utf-8")

        fake_worker = FakeWorkerAdapter(mutate_fn=mutate)

        result = run_instruction(
            "Add comment to calculate_total",
            self.root,
            adapter=fake_worker,
            change=True,
            confirm=lambda _: True,
        )

        # Result checks
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.code, "COMPLETED")

        # Target file content matches expected
        self.assertEqual(self.src_file.read_text(encoding="utf-8"), new_content)

        # Worktree cleaned up
        wt_list = self.list_worktrees()
        self.assertEqual(len(wt_list), 1)
        self.assertFalse(any("tabu-wt-" in wt for wt in wt_list))

        # Report persistence record exists
        # Only canonical records: the scaffold README.md shares the directory,
        # and glob order is filesystem-dependent.
        reports = sorted((self.root / ".ai" / "reports").glob("RPT-*.md"))
        self.assertEqual(len(reports), 1)
        report_text = reports[0].read_text(encoding="utf-8")
        self.assertIn("Proposal Binding:", report_text)
        self.assertIn("Diff Digest:", report_text)
        self.assertIn("Gate Result: ALLOWED", report_text)
        self.assertIn("Decision: APPROVED", report_text)
        self.assertIn("Apply Result: APPLIED", report_text)

    def test_02_single_file_change_rejected(self):
        """2. Same + 'n' -> target project byte-identical (all file hashes), worktree removed, decision REJECTED."""
        initial_hashes = hash_directory_bytes(self.root)

        def mutate(wt_dir: Path):
            (wt_dir / "src" / "sample.py").write_text("def calculate_total(a, b):\n    return a * b\n")

        fake_worker = FakeWorkerAdapter(mutate_fn=mutate)

        result = run_instruction(
            "Change to multiplication",
            self.root,
            adapter=fake_worker,
            change=True,
            confirm=lambda _: False,
        )

        self.assertEqual(result.status, "REFUSED")
        self.assertEqual(result.code, "APPROVAL_REJECTED")
        self.assertEqual(result.change_result["decision"], "REJECTED")

        # Byte-identical target project
        current_hashes = hash_directory_bytes(self.root)
        self.assertEqual(current_hashes, initial_hashes)
        self.assertEqual(self.src_file.read_text(encoding="utf-8"), self.initial_content)

        # Worktree removed
        wt_list = self.list_worktrees()
        self.assertEqual(len(wt_list), 1)

    def test_03_protected_target_denied_without_confirmation(self):
        """3. Worker writes .env or .git/config -> gate refusal, no question asked, target byte-identical."""
        initial_hashes = hash_directory_bytes(self.root)

        def mutate(wt_dir: Path):
            (wt_dir / ".env").write_text("API_SECRET=supersecret123\n")

        confirm_asked = []

        def spy_confirm(prompt: str) -> bool:
            confirm_asked.append(prompt)
            return True

        fake_worker = FakeWorkerAdapter(mutate_fn=mutate)

        result = run_instruction(
            "Write environment secret",
            self.root,
            adapter=fake_worker,
            change=True,
            confirm=spy_confirm,
        )

        self.assertEqual(result.status, "REFUSED")
        self.assertEqual(result.code, PROTECTED_TARGET_DENIED)
        # Question must strictly not be asked
        self.assertEqual(len(confirm_asked), 0)

        # Target is byte-identical
        current_hashes = hash_directory_bytes(self.root)
        self.assertEqual(current_hashes, initial_hashes)
        self.assertFalse((self.root / ".env").exists())

    def test_04_multi_file_change_applies_together(self):
        """4. (V2-04) Two files in one change set: one approval, both applied."""
        def mutate(wt_dir: Path):
            (wt_dir / "src" / "sample.py").write_text("new sample\n")
            (wt_dir / "src" / "helper.py").write_text("def helper(): pass\n")

        confirm_asked = []

        def spy_confirm(prompt: str) -> bool:
            confirm_asked.append(prompt)
            return True

        result = run_instruction(
            "Change two files",
            self.root,
            adapter=FakeWorkerAdapter(mutate_fn=mutate),
            change=True,
            confirm=spy_confirm,
        )

        self.assertEqual(result.code, "COMPLETED", result.message)
        self.assertEqual(len(confirm_asked), 1)
        self.assertEqual(self.src_file.read_text(encoding="utf-8"), "new sample\n")
        self.assertEqual((self.root / "src" / "helper.py").read_text(encoding="utf-8"),
                         "def helper(): pass\n")

    def test_05_dirty_target_project_refuses_worker(self):
        """5. Dirty target project -> worker is never invoked (call count 0)."""
        # Make tracked file dirty
        self.src_file.write_text("dirty uncommitted content\n")

        fake_worker = FakeWorkerAdapter()

        result = run_instruction(
            "Run on dirty project",
            self.root,
            adapter=fake_worker,
            change=True,
            confirm=lambda _: True,
        )

        self.assertEqual(result.status, "REFUSED")
        self.assertEqual(result.code, "GIT_DIRTY")
        self.assertIn("uncommitted tracked changes", result.message)
        # Worker call count must be 0
        self.assertEqual(fake_worker.call_count, 0)

    def test_06_target_changed_before_apply_fails_cas(self):
        """6. Target file modified externally between approval and apply -> apply refused, external edit preserved."""
        new_content = "def calculate_total(a, b):\n    # worker change\n    return a + b\n"
        external_content = "def calculate_total(a, b):\n    # external modification\n    return a + b\n"

        def mutate(wt_dir: Path):
            (wt_dir / "src" / "sample.py").write_text(new_content, encoding="utf-8")

        def external_edit():
            # External party modifies target file before apply transaction commits
            self.src_file.write_text(external_content, encoding="utf-8")

        fake_worker = FakeWorkerAdapter(mutate_fn=mutate)

        result = run_instruction(
            "Apply change with concurrent edit",
            self.root,
            adapter=fake_worker,
            change=True,
            confirm=lambda _: True,
            pre_apply_hook=external_edit,
        )

        self.assertEqual(result.status, "REFUSED")
        self.assertEqual(result.code, TARGET_CHANGED)

        # External modification must be preserved; not overwritten by worker
        self.assertEqual(self.src_file.read_text(encoding="utf-8"), external_content)

    def test_07_worker_exception_cleans_up_worktree(self):
        """7. Worker raises exception -> worktree is still removed."""
        fake_worker = FakeWorkerAdapter(raise_exc=RuntimeError("Worker internal crash"))

        result = run_instruction(
            "Crash worker",
            self.root,
            adapter=fake_worker,
            change=True,
            confirm=lambda _: True,
        )

        self.assertIn(result.status, ("REFUSED", "FAILED"))
        # Worktrees cleaned up
        wt_list = self.list_worktrees()
        self.assertEqual(len(wt_list), 1)
        self.assertFalse(any("tabu-wt-" in wt for wt in wt_list))

    def test_08_non_tty_stdin_awaits_approval(self):
        """8. Non-TTY stdin -> no apply, decision awaits approval."""
        def mutate(wt_dir: Path):
            (wt_dir / "src" / "sample.py").write_text("def calculate_total(a, b):\n    return a + b + 1\n")

        fake_worker = FakeWorkerAdapter(mutate_fn=mutate)

        with patch("sys.stdin.isatty", return_value=False):
            result = run_instruction(
                "Non-interactive run",
                self.root,
                adapter=fake_worker,
                change=True,
                confirm=None,
            )

        self.assertEqual(result.status, "AWAITING_APPROVAL")
        self.assertEqual(result.code, "APPROVAL_REQUIRED")
        self.assertEqual(result.change_result["decision"], "AWAITING_APPROVAL")

        # No apply performed; original content intact
        self.assertEqual(self.src_file.read_text(encoding="utf-8"), self.initial_content)
        # Worktree removed
        wt_list = self.list_worktrees()
        self.assertEqual(len(wt_list), 1)

    def test_09_worker_argv_and_permissions(self):
        """9. Argv: with --change native workspace-write + cwd=worktree; without --change read-only + scratch.
        OpenCode --dir points to worktree and OPENCODE_PERMISSION denies bash.
        """
        req = WorkerRequest(request_id="req-1", intent="test", payload={})

        # Native with change
        native_change = NativeCLIWorker(cwd="/tmp/fake-wt", sandbox="workspace-write")
        argv_nc = native_change.build_argv(req)
        self.assertIn("--sandbox", argv_nc)
        self.assertEqual(argv_nc[argv_nc.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(native_change.cwd, "/tmp/fake-wt")

        # Native without change
        native_ro = NativeCLIWorker()
        argv_ro = native_ro.build_argv(req)
        self.assertIn("--sandbox", argv_ro)
        self.assertEqual(argv_ro[argv_ro.index("--sandbox") + 1], "read-only")
        self.assertIn("tabu-native-", native_ro.cwd)

        # OpenCode with change
        opencode_change = OpenCodeCLIWorker(cwd="/tmp/fake-wt", change_mode=True)
        argv_oc = opencode_change.build_argv("Test prompt")
        self.assertIn("--dir", argv_oc)
        self.assertEqual(argv_oc[argv_oc.index("--dir") + 1], "/tmp/fake-wt")
        self.assertIsNotNone(opencode_change.permissions)
        self.assertEqual(opencode_change.permissions.get("bash"), "deny")
        self.assertEqual(opencode_change.permissions.get("read"), "allow")
        self.assertEqual(opencode_change.permissions.get("edit"), "allow")

        # OpenCode without change
        opencode_ro = OpenCodeCLIWorker()
        argv_or = opencode_ro.build_argv("Test prompt")
        self.assertIn("--dir", argv_or)
        self.assertIn("tabu-opencode-", opencode_ro.cwd)
        self.assertIsNone(opencode_ro.permissions)

    @patch("tabularium.engine.workers.cli.opencode_worker.BoundedProcess")
    def test_10_opencode_stderr_permission_denied(self, mock_process_cls):
        """10. OpenCode stderr permission line -> WORKER_PERMISSION_DENIED."""
        err_line = "permission requested: bash 'curl evil.com' auto-rejecting"
        mock_proc = MagicMock()
        mock_proc.run.return_value = Capture(
            outcome="EXITED",
            returncode=1,
            stdout="",
            stderr_present=True,
            capture_complete=True,
            stderr=f"[2026-10-29T10:00:00Z] {err_line}\n",
        )
        mock_process_cls.return_value = mock_proc

        with tempfile.TemporaryDirectory() as fake_exec_dir:
            fake_bin = Path(fake_exec_dir) / "opencode"
            fake_bin.write_text("#!/bin/sh\nexit 1\n")
            worker = OpenCodeCLIWorker(executable=str(fake_bin))
            req = WorkerRequest(request_id="req-perm-test", intent="test", payload={})
            result = worker.execute(req)

        self.assertEqual(result.status, FAILED)
        diag_codes = [d["code"] for d in result.diagnostics]
        self.assertIn("WORKER_PERMISSION_DENIED", diag_codes)
        self.assertNotIn("STDERR_PRESENT", diag_codes)

        perm_diag = next(d for d in result.diagnostics if d["code"] == "WORKER_PERMISSION_DENIED")
        self.assertIn("permission requested: bash", perm_diag["detail"])
        self.assertIn("auto-rejecting", perm_diag["detail"])



class TestChangePathVerificationHardening(ChangePathFixture):

    def test_11_staged_patch_must_match_worker_output(self):
        """The default verifier compares the staged file with the worktree file."""
        worker_content = "def calculate_total(a, b):\n    # worker change\n    return a + b\n"

        def mutate(wt_dir: Path):
            (wt_dir / "src" / "sample.py").write_text(worker_content, encoding="utf-8")

        fake_worker = FakeWorkerAdapter(mutate_fn=mutate)

        def tamper_worktree():
            # After the diff was shown, the worktree no longer holds what was approved.
            fake_worker_dir = Path(fake_worker.cwd)
            (fake_worker_dir / "src" / "sample.py").write_text("tampered\n", encoding="utf-8")

        result = run_instruction(
            "Apply change, then tamper worktree",
            self.root,
            adapter=fake_worker,
            change=True,
            confirm=lambda _: True,
            pre_apply_hook=tamper_worktree,
        )

        self.assertEqual(result.status, "REFUSED")
        self.assertEqual(result.code, "ISOLATED_TESTS_FAILED")
        self.assertEqual(self.src_file.read_text(encoding="utf-8"), self.initial_content)

    def test_12_cli_change_awaiting_skips_runtime_approval_sheet(self):
        """A non-TTY --change result must not fall into the old approval sheet or --persist."""
        from tabularium import cli
        from tabularium.execution import ProductResult

        awaiting = ProductResult(
            status="AWAITING_APPROVAL",
            code="APPROVAL_REQUIRED",
            message="needs a terminal",
            target=str(self.root),
            change_result={"decision": "AWAITING_APPROVAL"},
        )
        completed = ProductResult(
            status="COMPLETED",
            code="COMPLETED",
            message="applied",
            target=str(self.root),
            change_result={"decision": "APPROVED"},
        )
        for result, expected_rc in ((awaiting, 1), (completed, 0)):
            with patch.object(cli, "run_instruction", return_value=result), \
                    patch.object(cli, "_approval_prompt") as sheet, \
                    patch.object(cli, "_handle_persistence") as persist, \
                    patch.object(cli, "_emit_execution"):
                rc = cli.main(["run", "x", str(self.root), "--change", "--persist"])
            self.assertEqual(rc, expected_rc)
            sheet.assert_not_called()
            persist.assert_not_called()


    def _adapter_seen_by_runtime(self, worker_name: str):
        """Run the real --change worker selection; capture the adapter the runtime receives."""
        seen = {}

        def fake_start(plan, collector=None):
            seen["adapter"] = plan.adapter
            raise RuntimeError("stop after worker selection")

        with patch("tabularium.change_path.find_native_executable", return_value="/bin/true"), \
                patch("tabularium.change_path.find_opencode_executable", return_value="/bin/true"), \
                patch("tabularium.change_path.start_execution", side_effect=fake_start):
            result = run_instruction("x", self.root, worker=worker_name, change=True,
                                     confirm=lambda _: True)
            self.assertEqual(result.code, "WORKER_UNAVAILABLE")
        return seen["adapter"]

    def test_13_change_path_builds_writable_native_worker_in_worktree(self):
        adapter = self._adapter_seen_by_runtime("native")
        self.assertEqual(adapter.sandbox, "workspace-write")
        self.assertIn("tabu-wt-", adapter.cwd)
        self.assertEqual(adapter.build_argv(None)[adapter.build_argv(None).index("--sandbox") + 1],
                         "workspace-write")

    def test_14_change_path_builds_opencode_worker_with_bash_denied(self):
        adapter = self._adapter_seen_by_runtime("opencode")
        self.assertIn("tabu-wt-", adapter.cwd)
        self.assertEqual(adapter.permissions.get("bash"), "deny")
        self.assertEqual(adapter.permissions.get("edit"), "allow")

    @patch("tabularium.engine.workers.cli.opencode_worker.BoundedProcess")
    def test_15_permission_env_reaches_process_and_denial_fails_run(self, mock_process_cls):
        """OPENCODE_PERMISSION is in the child env; a permission denial fails even with output and rc 0."""
        mock_proc = MagicMock()
        mock_proc.run.return_value = Capture(
            outcome="EXITED",
            returncode=0,
            stdout=json.dumps({"type": "text", "part": {"text": "done anyway"}}) + "\n",
            stderr_present=True,
            capture_complete=True,
            stderr="permission requested: bash (ls); auto-rejecting\n",
        )
        mock_process_cls.return_value = mock_proc
        worker = OpenCodeCLIWorker(executable="/bin/true", cwd=str(self.root), change_mode=True)
        result = worker.execute(WorkerRequest(request_id="req-env", intent="test", payload={}))

        env = mock_process_cls.call_args.kwargs["env"]
        self.assertEqual(json.loads(env["OPENCODE_PERMISSION"])["bash"], "deny")
        self.assertEqual(result.status, FAILED)


if __name__ == "__main__":
    unittest.main()
