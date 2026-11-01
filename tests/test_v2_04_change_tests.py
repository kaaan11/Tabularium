"""Acceptance tests for isolated tests on the --change path."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock, patch

from tabularium.change_config import (
    CHANGE_CONFIG_INVALID,
    ChangeConfig,
    ChangeConfigError,
    load_change_config,
)
from tabularium.change_path import run_change_path, sanitize_terminal
from tabularium.execution import ProductResult, run_instruction
from tabularium.init import initialize
from tabularium.onboarding import build_authorizations, onboard_project
from tabularium.engine.code_gate.isolated_run import (
    SANDBOX_TREE_UNSUPPORTED,
    SANDBOX_UNAVAILABLE,
    probe_isolation,
    run_tests_on_copy,
)
from tabularium.engine.code_gate.model import (
    ISOLATED_TESTS_FAILED,
    PROTECTED_TARGET_DENIED,
    TARGET_CHANGED,
)
from tests.test_v2_09_change_path import FakeWorkerAdapter, current_year

AVAILABLE, WHY = probe_isolation()
if os.environ.get("CI") and not AVAILABLE:
    raise RuntimeError(f"bwrap isolation is required in CI: {WHY}")
needs_bwrap = unittest.skipUnless(AVAILABLE, f"bwrap isolation unavailable: {WHY}")

PY = sys.executable


class ChangeTestsFixture(unittest.TestCase):
    """Sets up an initialized, onboarded, clean git repository with pkg/a.py and pkg/b.py."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="tabu-v204b2-test-")
        self.root = Path(self.temp_dir.name).resolve()

        # 1. Initialize Tabularium
        initialize(self.root)

        # 2. Onboard project
        declarations = {
            "title": "Change Tests Fixture",
            "purpose": "Verification of isolated tests under --change",
            "scope": "Testing worktree and test execution",
        }
        draft = onboard_project(self.root, declarations)
        onboard_res = onboard_project(
            self.root,
            declarations,
            build_authorizations(draft.proposal, "human:user", "2026-01-01T00:00:00+00:00"),
        )
        self.assertEqual(onboard_res.status, "ONBOARDED")

        # 3. Create initial code files
        pkg_dir = self.root / "pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "a.py").write_text("def get_value():\n    return 'old'\n")
        (pkg_dir / "b.py").write_text(
            "from pkg.a import get_value\n\ndef check_val():\n    return get_value()\n"
        )

        # 4. Git init & initial commit
        subprocess.run(["git", "-C", str(self.root), "init", "-b", "main"], check=True, capture_output=True)
        for args in (
            ["config", "user.name", "Tabularium Worker"],
            ["config", "user.email", "test@example.invalid"],
            ["add", "-A"],
            ["commit", "-m", "Initial pkg commit"],
        ):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_change_yaml(self, content: str) -> Path:
        cfg = self.root / ".ai" / "change.yaml"
        cfg.write_text(content, encoding="utf-8")
        return cfg

    def get_last_commit_message(self) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.root), "log", "-1", "--pretty=%B"],
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout


class TestChangeTestsSuite(ChangeTestsFixture):
    """12 acceptance criteria test cases."""

    # 1. Patch is tested.
    @needs_bwrap
    def test_01_patch_is_tested_and_passes(self) -> None:
        # Worker mutates a.py to return 'new'
        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text("def get_value():\n    return 'new'\n")

        adapter = FakeWorkerAdapter(mutate_fn=mutate_a)

        # Test validates both b.py and new behavior of a.py
        test_script = (
            "import sys; from pkg.b import check_val; from pkg.a import get_value; "
            "assert check_val() == 'new', f'unexpected {check_val()}'; "
            "assert get_value() == 'new'"
        )
        self.write_change_yaml(
            f'test_command: ["{PY}", "-c", "{test_script}"]\ntimeout_seconds: 30\n'
        )

        confirm_called = []

        def confirm(prompt: str) -> bool:
            confirm_called.append(prompt)
            return True

        res = run_instruction(
            path=self.root,
            instruction="Update a.py to new",
            change=True,
            adapter=adapter,
            confirm=confirm,
        )

        self.assertEqual(res.status, "COMPLETED", res.message)
        self.assertEqual(len(confirm_called), 1)
        self.assertEqual((self.root / "pkg" / "a.py").read_text(), "def get_value():\n    return 'new'\n")
        msg = self.get_last_commit_message()
        self.assertIn("Tests: passed", msg)
        self.assertEqual(res.change_result["tests"]["status"], "PASSED")

        # Reverse is also tested: if test verifies OLD behavior -> ISOLATED_TESTS_FAILED
        def mutate_a2(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text("def get_value():\n    return 'third_value'\n")

        adapter2 = FakeWorkerAdapter(mutate_fn=mutate_a2)
        test_script_old = (
            # The committed base now returns 'new': only a copy that carries the
            # worker's 'third_value' patch can fail this.
            "from pkg.b import check_val; assert check_val() == 'new'"
        )
        self.write_change_yaml(
            f'test_command: ["{PY}", "-c", "{test_script_old}"]\ntimeout_seconds: 30\n'
        )

        confirm_called.clear()
        res_fail = run_instruction(
            path=self.root,
            instruction="Update a.py again but test expects old",
            change=True,
            adapter=adapter2,
            confirm=confirm,
        )
        self.assertEqual(res_fail.status, "REFUSED")
        self.assertEqual(res_fail.code, ISOLATED_TESTS_FAILED)
        self.assertEqual(len(confirm_called), 0)

    # 2. Broken test.
    @needs_bwrap
    def test_02_broken_test_refuses_without_confirm(self) -> None:
        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text("def get_value():\n    return 'broken'\n")

        adapter = FakeWorkerAdapter(mutate_fn=mutate_a)

        self.write_change_yaml(
            f'test_command: ["{PY}", "-c", "import sys; sys.stderr.write(\'TEST FAILURE DETAIL\\\\n\'); sys.exit(1)"]\n'
        )

        confirm_mock = MagicMock(return_value=True)
        original_a_content = (self.root / "pkg" / "a.py").read_text()

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            res = run_instruction(
                path=self.root,
                instruction="Mutate with broken test",
                change=True,
                adapter=adapter,
                confirm=confirm_mock,
            )

        self.assertEqual(res.status, "REFUSED")
        self.assertEqual(res.code, ISOLATED_TESTS_FAILED)
        confirm_mock.assert_not_called()
        self.assertEqual((self.root / "pkg" / "a.py").read_text(), original_a_content)

        # Output was printed to stdout
        printed = stdout_buf.getvalue()
        self.assertIn("TEST FAILURE DETAIL", printed)

        # Candidate task was cancelled with ISOLATED_TESTS_FAILED
        from tabularium.task import read_frontmatter
        task_files = list((self.root / ".ai" / "tasks").glob("*.md"))
        cancelled_candidates = []
        for tf in task_files:
            rec = read_frontmatter(tf, self.root, "Task")
            if rec.frontmatter.get("result") == ISOLATED_TESTS_FAILED:
                cancelled_candidates.append(rec)
        self.assertTrue(len(cancelled_candidates) >= 1)
        self.assertEqual(cancelled_candidates[0].frontmatter.get("status"), "CANCELLED")

    # 3. Timeout.
    @needs_bwrap
    def test_03_timeout_kills_process_group_and_refuses(self) -> None:
        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text("def get_value():\n    return 'timeout'\n")

        adapter = FakeWorkerAdapter(mutate_fn=mutate_a)

        self.write_change_yaml(
            f'test_command: ["{PY}", "-c", "import time; time.sleep(10)"]\ntimeout_seconds: 2\n'
        )

        t0 = time.monotonic()
        res = run_instruction(
            path=self.root,
            instruction="Sleep in test",
            change=True,
            adapter=adapter,
            confirm=lambda _: True,
        )
        elapsed = time.monotonic() - t0

        self.assertEqual(res.status, "REFUSED")
        self.assertEqual(res.code, ISOLATED_TESTS_FAILED)
        self.assertLess(elapsed, 6.0, f"Elapsed time {elapsed} was too long for timeout 2s")
        self.assertGreater(elapsed, 1.5, f"Elapsed time {elapsed} was too short")
        self.assertIn("timed out", res.message.lower())
        self.assertTrue(res.change_result["tests"]["timed_out"])

    # 4. No configuration.
    def test_04_no_config_warns_and_proceeds(self) -> None:
        cfg = self.root / ".ai" / "change.yaml"
        if cfg.exists():
            cfg.unlink()

        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text("def get_value():\n    return 'no_cfg'\n")

        adapter = FakeWorkerAdapter(mutate_fn=mutate_a)
        confirm_called = []

        def confirm(prompt: str) -> bool:
            confirm_called.append(prompt)
            return True

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            res = run_instruction(
                path=self.root,
                instruction="Apply without change.yaml",
                change=True,
                adapter=adapter,
                confirm=confirm,
            )

        self.assertEqual(res.status, "COMPLETED")
        self.assertEqual(len(confirm_called), 1)
        expected_warning = "Tests: NOT RUN (no .ai/change.yaml)"
        self.assertIn(expected_warning, res.warnings)
        self.assertIn(expected_warning, stdout_buf.getvalue())

        msg = self.get_last_commit_message()
        self.assertIn("Tests: not configured", msg)
        self.assertEqual(res.change_result["tests"]["status"], "NOT_CONFIGURED")

    # 5. Invalid configuration (at least one parameterized case per item)
    def test_05_invalid_config_cases(self) -> None:
        cases = [
            ("malformed yaml", "test_command: [invalid yaml"),
            ("root is not a mapping", "['a', 'list']"),
            ("unknown key", "test_command: ['python']\nextra_unknown: 1"),
            ("test_command missing", "timeout_seconds: 300"),
            ("test_command empty list", "test_command: []"),
            ("test_command non-string item", "test_command: [123]"),
            ("test_command empty string", "test_command: ['']"),
            ("test_command contains NUL", 'test_command:\n  - "python\\0"'),
            ("test_command given as a string", "test_command: 'python -m pytest'"),
            ("timeout_seconds bool", "test_command: ['python']\ntimeout_seconds: true"),
            ("timeout_seconds str", "test_command: ['python']\ntimeout_seconds: '300'"),
            ("timeout_seconds < 1", "test_command: ['python']\ntimeout_seconds: 0"),
            ("timeout_seconds > 1800", "test_command: ['python']\ntimeout_seconds: 1801"),
        ]

        for desc, content in cases:
            with self.subTest(case=desc):
                self.write_change_yaml(content)
                adapter = FakeWorkerAdapter()
                task_count_before = len(list((self.root / ".ai" / "tasks").glob("*.md")))

                res = run_instruction(
                    path=self.root,
                    instruction="Run with invalid config",
                    change=True,
                    adapter=adapter,
                    confirm=lambda _: True,
                )

                self.assertEqual(res.status, "REFUSED")
                self.assertEqual(res.code, CHANGE_CONFIG_INVALID)
                self.assertEqual(adapter.call_count, 0, f"Adapter should not be called for {desc}")
                task_count_after = len(list((self.root / ".ai" / "tasks").glob("*.md")))
                self.assertEqual(task_count_before, task_count_after, f"Task created for {desc}")

        # Case: file is a symlink
        with self.subTest(case="file is a symlink"):
            target_real = self.root / ".ai" / "real_change.yaml"
            target_real.write_text("test_command: ['python']\n")
            symlink_cfg = self.root / ".ai" / "change.yaml"
            if symlink_cfg.exists():
                symlink_cfg.unlink()
            symlink_cfg.symlink_to(target_real)

            adapter = FakeWorkerAdapter()
            res = run_instruction(
                path=self.root,
                instruction="Run with symlink config",
                change=True,
                adapter=adapter,
                confirm=lambda _: True,
            )
            self.assertEqual(res.status, "REFUSED")
            self.assertEqual(res.code, CHANGE_CONFIG_INVALID)
            self.assertEqual(adapter.call_count, 0)
            symlink_cfg.unlink()
            target_real.unlink()

        # Case: file is not a regular file (directory)
        with self.subTest(case="file is not a regular file"):
            dir_cfg = self.root / ".ai" / "change.yaml"
            if dir_cfg.exists():
                dir_cfg.unlink()
            dir_cfg.mkdir()

            adapter = FakeWorkerAdapter()
            res = run_instruction(
                path=self.root,
                instruction="Run with dir config",
                change=True,
                adapter=adapter,
                confirm=lambda _: True,
            )
            self.assertEqual(res.status, "REFUSED")
            self.assertEqual(res.code, CHANGE_CONFIG_INVALID)
            self.assertEqual(adapter.call_count, 0)
            dir_cfg.rmdir()

        # Case: file is not a direct child of .ai/ directory (e.g. .ai symlink)
        with self.subTest(case=".ai is a symlink outside the project"):
            outside = Path(tempfile.mkdtemp(prefix="ai-outside-"))
            ai_dir = self.root / ".ai"
            ai_backup = self.root / ".ai_backup"
            try:
                (outside / "change.yaml").write_text("test_command: ['python']\n")
                ai_dir.rename(ai_backup)
                ai_dir.symlink_to(outside)

                adapter = FakeWorkerAdapter()
                res = run_change_path(
                    target=self.root,
                    instruction="Run with symlinked .ai dir",
                    execution_id="ex-direct-child",
                    checkpoint_path=str(self.root / ".execution"),
                    adapter=adapter,
                    confirm=lambda _: True,
                )
                self.assertEqual(res.status, "REFUSED")
                self.assertEqual(res.code, CHANGE_CONFIG_INVALID)
                self.assertEqual(adapter.call_count, 0)
            finally:
                if ai_dir.is_symlink():
                    ai_dir.unlink()
                if ai_backup.exists():
                    ai_backup.rename(ai_dir)
                shutil.rmtree(outside, ignore_errors=True)

        # Case: content is not UTF-8
        with self.subTest(case="content is not UTF-8"):
            cfg = self.root / ".ai" / "change.yaml"
            cfg.write_bytes(b"\x80\xff\xfe invalid utf8")

            adapter = FakeWorkerAdapter()
            res = run_instruction(
                path=self.root,
                instruction="Run with non-utf8 config",
                change=True,
                adapter=adapter,
                confirm=lambda _: True,
            )
            self.assertEqual(res.status, "REFUSED")
            self.assertEqual(res.code, CHANGE_CONFIG_INVALID)
            self.assertEqual(adapter.call_count, 0)

    # 6. No sandbox.
    def test_06_sandbox_unavailable_does_not_run_on_host(self) -> None:
        marker_file = self.root / "marker_should_not_exist.txt"
        if marker_file.exists():
            marker_file.unlink()

        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text("def get_value():\n    return 'bwrap_missing'\n")

        adapter = FakeWorkerAdapter(mutate_fn=mutate_a)

        # Command attempts to write a marker file to host project
        test_script = f"open('{marker_file}', 'w').write('pwned')"
        self.write_change_yaml(f'test_command: ["{PY}", "-c", "{test_script}"]\n')

        confirm_mock = MagicMock(return_value=True)

        with patch("tabularium.engine.code_gate.isolated_run.find_bwrap", return_value=None):
            res = run_instruction(
                path=self.root,
                instruction="Run when bwrap is missing",
                change=True,
                adapter=adapter,
                confirm=confirm_mock,
            )

        self.assertEqual(res.status, "REFUSED")
        self.assertEqual(res.code, SANDBOX_UNAVAILABLE)
        confirm_mock.assert_not_called()
        self.assertFalse(marker_file.exists(), "Command must NEVER run on host when sandbox is unavailable!")

    # 7. SANDBOX_TREE_UNSUPPORTED.
    @needs_bwrap
    def test_07_sandbox_tree_unsupported(self) -> None:
        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text("def get_value():\n    return 'tree_unsupported'\n")

        adapter = FakeWorkerAdapter(mutate_fn=mutate_a)
        self.write_change_yaml(f'test_command: ["{PY}", "-c", "print(1)"]\n')

        with patch("tabularium.engine.code_gate.isolated_run.MAX_TREE_BYTES", 10):
            res = run_instruction(
                path=self.root,
                instruction="Run with unsupported tree",
                change=True,
                adapter=adapter,
                confirm=lambda _: True,
            )

        self.assertEqual(res.status, "REFUSED")
        self.assertEqual(res.code, SANDBOX_TREE_UNSUPPORTED)

    # 8. Configuration is protected from worker.
    @needs_bwrap
    def test_08_config_protected_from_worker_tampering(self) -> None:
        marker_file = self.root / "tamper_marker.txt"
        if marker_file.exists():
            marker_file.unlink()

        # Target change.yaml has benign command
        self.write_change_yaml(f'test_command: ["{PY}", "-c", "print(\'benign\')"]\n')
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-m", "commit change.yaml"], check=True)

        # Worker attempts to tamper with .ai/change.yaml in worktree
        def mutate_tamper(wt: Path) -> None:
            (wt / ".ai" / "change.yaml").write_text(
                f'test_command: ["{PY}", "-c", "open(\'{marker_file}\', \'w\').write(\'tampered\')"]\n'
            )

        adapter = FakeWorkerAdapter(mutate_fn=mutate_tamper)

        res = run_instruction(
            path=self.root,
            instruction="Tamper with change.yaml",
            change=True,
            adapter=adapter,
            confirm=lambda _: True,
        )

        self.assertEqual(res.status, "REFUSED")
        self.assertEqual(res.code, PROTECTED_TARGET_DENIED)
        self.assertFalse(marker_file.exists(), "Tests must not run when worker touches protected .ai paths")

    # 9. Base changed.
    @needs_bwrap
    def test_09_base_changed_between_confirm_and_apply(self) -> None:
        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text("def get_value():\n    return 'new_concurrent'\n")

        adapter = FakeWorkerAdapter(mutate_fn=mutate_a)
        self.write_change_yaml(f'test_command: ["{PY}", "-c", "print(1)"]\n')

        def concurrent_mutating_confirm(prompt: str) -> bool:
            # Another process modifies a tracked file before user approves
            (self.root / "pkg" / "b.py").write_text("def check_val():\n    return 'concurrent_edit'\n")
            return True

        res = run_instruction(
            path=self.root,
            instruction="Concurrent edit during confirm",
            change=True,
            adapter=adapter,
            confirm=concurrent_mutating_confirm,
        )

        self.assertEqual(res.status, "REFUSED")
        self.assertEqual(res.code, TARGET_CHANGED)
        self.assertEqual((self.root / "pkg" / "a.py").read_text(), "def get_value():\n    return 'old'\n")

    # 10. Non-interactive session.
    def test_10_non_interactive_does_not_run_tests(self) -> None:
        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text("def get_value():\n    return 'non_interactive'\n")

        adapter = FakeWorkerAdapter(mutate_fn=mutate_a)
        self.write_change_yaml(f'test_command: ["{PY}", "-c", "print(1)"]\n')

        with patch("sys.stdin.isatty", return_value=False), \
             patch("tabularium.change_path.run_tests_on_copy") as mock_test_run:
            res = run_instruction(
                path=self.root,
                instruction="Non interactive run",
                change=True,
                adapter=adapter,
                confirm=None,
            )

        self.assertEqual(res.status, "AWAITING_APPROVAL")
        self.assertEqual(res.code, "APPROVAL_REQUIRED")
        mock_test_run.assert_not_called()

    # 11. Terminal security.
    def test_11_terminal_output_sanitization(self) -> None:
        raw_text = "clean\x1b[2Jansi\x1b]0;title\x07osc\r\bdel\x7fc0\x03\ttab\nnewline"
        sanitized = sanitize_terminal(raw_text)

        # Raw control codes must NOT be in sanitized string
        self.assertNotIn("\x1b", sanitized)
        self.assertNotIn("\x07", sanitized)
        self.assertNotIn("\r", sanitized)
        self.assertNotIn("\b", sanitized)
        self.assertNotIn("\x7f", sanitized)
        self.assertNotIn("\x03", sanitized)

        # \n and \t must be preserved
        self.assertIn("\t", sanitized)
        self.assertIn("\n", sanitized)

        # Escapes must be visibly represented
        self.assertIn(r"\x1b[2J", sanitized)
        self.assertIn(r"\x07", sanitized)
        self.assertIn(r"\r", sanitized)
        self.assertIn(r"\b", sanitized)

    # 12. tabu run without --change.
    def test_12_run_without_change_does_not_run_tests(self) -> None:
        self.write_change_yaml(f'test_command: ["{PY}", "-c", "raise SystemExit(1)"]\n')

        # Single active task is required for standard run
        from tabularium.task import task_create
        task_res = task_create(self.root, title="Standard Run Task", objective="Test regular run")
        self.assertTrue(task_res.created)

        adapter = FakeWorkerAdapter()
        with patch("tabularium.change_path.run_tests_on_copy") as mock_test_run:
            res = run_instruction(
                path=self.root,
                instruction="Regular run without change",
                change=False,
                adapter=adapter,
                task_id=task_res.record_id,
            )

        mock_test_run.assert_not_called()
        self.assertEqual(res.status, "COMPLETED")


class TestTerminalSanitizationWiring(ChangeTestsFixture):
    """Both worker-controlled prints go through sanitize_terminal on the real path."""

    @needs_bwrap
    def test_failed_test_output_is_sanitized_when_printed(self) -> None:
        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text("def get_value():\n    return 'x'\n")

        self.write_change_yaml(
            f'test_command: ["{PY}", "-c", '
            '"import sys; sys.stdout.write(\'\\\\x1b[2J\\\\x1b]0;pwned\\\\x07FAKE\\\\r\\\\n\'); sys.exit(1)"]\n'
        )
        out = io.StringIO()
        with patch("sys.stdout", out):
            res = run_instruction(path=self.root, instruction="Escape in test output", change=True,
                                  adapter=FakeWorkerAdapter(mutate_fn=mutate_a),
                                  confirm=lambda _: True)
        self.assertEqual(res.code, ISOLATED_TESTS_FAILED)
        printed = out.getvalue()
        self.assertIn("FAKE", printed)
        for raw in ("\x1b", "\x07", "\r"):
            self.assertNotIn(raw, printed)
        self.assertIn(r"\x1b[2J", printed)

    def test_diff_preview_is_sanitized_when_printed(self) -> None:
        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text(
                "def get_value():\n    return 'old'  # \x1b[2J\x1b]0;pwned\x07\bTests: PASSED\n")

        out = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value="n"):
            res = run_instruction(path=self.root, instruction="Escape in diff", change=True,
                                  adapter=FakeWorkerAdapter(mutate_fn=mutate_a), confirm=None)
        self.assertEqual(res.code, "APPROVAL_REJECTED", res.message)
        printed = out.getvalue()
        self.assertIn("--- Diff preview ---", printed)
        for raw in ("\x1b", "\x07", "\b"):
            self.assertNotIn(raw, printed)
        self.assertIn(r"\x1b]0;pwned\x07\bTests: PASSED", printed)


class TestRelativeTestCommand(ChangeTestsFixture):
    """A committed change.yaml names project paths relatively."""

    def _commit_gitignore(self) -> None:
        (self.root / ".gitignore").write_text(".venv/\n")
        subprocess.run(["git", "-C", str(self.root), "add", ".gitignore"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "ignore venv"], check=True,
                       capture_output=True)

    @needs_bwrap
    def test_relative_project_venv_interpreter_runs(self) -> None:
        self._commit_gitignore()
        subprocess.run([PY, "-m", "venv", "--without-pip", str(self.root / ".venv")], check=True)

        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text("def get_value():\n    return 'venv'\n")

        self.write_change_yaml(
            'test_command: [".venv/bin/python", "-c", '
            f'"import sys, pkg.a; assert sys.prefix == \'{self.root}/.venv\', sys.prefix; '
            'assert pkg.a.get_value() == \'venv\'"]\n')
        res = run_instruction(path=self.root, instruction="Use project venv", change=True,
                              adapter=FakeWorkerAdapter(mutate_fn=mutate_a), confirm=lambda _: True)
        self.assertEqual(res.status, "COMPLETED", res.message)
        self.assertEqual(res.change_result["tests"]["status"], "PASSED")

    @needs_bwrap
    def test_relative_tracked_script_runs_from_patched_copy(self) -> None:
        script = self.root / "run_tests.sh"
        script.write_text("#!/bin/sh\ngrep -q \"return 'patched'\" pkg/a.py\n")
        script.chmod(0o755)
        subprocess.run(["git", "-C", str(self.root), "add", "run_tests.sh"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "test script"], check=True,
                       capture_output=True)

        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text("def get_value():\n    return 'patched'\n")

        self.write_change_yaml('test_command: ["./run_tests.sh"]\n')
        res = run_instruction(path=self.root, instruction="Patch a", change=True,
                              adapter=FakeWorkerAdapter(mutate_fn=mutate_a), confirm=lambda _: True)
        self.assertEqual(res.status, "COMPLETED", res.message)

    def test_resolution_is_limited_to_project_virtualenvs(self) -> None:
        from tabularium.change_path import resolve_test_command
        (self.root / ".venv" / "bin").mkdir(parents=True)
        (self.root / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
        root = str(self.root)
        self.assertEqual(resolve_test_command(self.root, [".venv/bin/python", "-m", "pytest"]),
                         [f"{root}/.venv/bin/python", "-m", "pytest"])
        for unchanged in (["./run_tests.sh"], ["python", "-m", "pytest"],
                          ["../.venv/bin/python"], ["/usr/bin/python3"]):
            self.assertEqual(resolve_test_command(self.root, unchanged), unchanged)
        self.assertEqual(resolve_test_command(self.root, ["scripts/test"]), ["./scripts/test"])


class TestDiffPreviewAndChangeConfigHardening(ChangeTestsFixture):
    """Hardening for diff preview sanitization, output order, and config limits."""

    HOSTILE = "\u202eDESSAP :stseT\u202c \u2066x\u2069 \u200b\ufeff\u2028\x85\x9b2J"

    def test_sanitize_escapes_c1_bidi_and_format_characters(self) -> None:
        shown = sanitize_terminal(self.HOSTILE + " şğü ✓")
        for raw in ("\u202e", "\u202c", "\u2066", "\u2069", "\u200b", "\ufeff", "\u2028", "\x85", "\x9b"):
            self.assertNotIn(raw, shown)
        for visible in (r"\u202e", r"\u2066", r"\u200b", r"\ufeff", r"\u2028", r"\x85", r"\x9b"):
            self.assertIn(visible, shown)
        self.assertIn("şğü ✓", shown)

    def test_diff_preview_hides_no_bidi_override(self) -> None:
        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text(f"def get_value():\n    return 'old'  # \u202eDESSAP :stseT\u202c \u2066x\u2069 \u200b\ufeff\n")

        out = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value="n"):
            res = run_instruction(path=self.root, instruction="Bidi in diff", change=True,
                                  adapter=FakeWorkerAdapter(mutate_fn=mutate_a), confirm=None)
        self.assertEqual(res.code, "APPROVAL_REJECTED", res.message)
        self.assertNotIn("\u202e", out.getvalue())
        self.assertIn(r"\u202e", out.getvalue())

    def test_cli_sanitizes_worker_output_and_message(self) -> None:
        from types import SimpleNamespace
        from tabularium import cli
        from tabularium.execution import ProductResult

        pipe = SimpleNamespace(output="done \x1b[2J\u202eFAKE", worker_result={}, metrics=None)
        for status, message in (("COMPLETED", "m"), ("REFUSED", "worker said \x1b]0;x\x07\u202eboom")):
            out, err = io.StringIO(), io.StringIO()
            with patch("sys.stdout", out), patch("sys.stderr", err):
                cli._emit_execution(ProductResult(status=status, code="X", message=message,
                                                  target=str(self.root), pipeline=pipe))
            printed = out.getvalue() + err.getvalue()
            for raw in ("\x1b", "\x07", "\u202e"):
                self.assertNotIn(raw, printed, status)
        self.assertIn(r"\u202eboom", printed)

    def test_interactive_yes_applies_the_tested_change(self) -> None:
        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text("def get_value():\n    return 'yes'\n")

        self.write_change_yaml(f'test_command: ["{PY}", "-c", "print(1)"]\n')
        out = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value="y"):
            res = run_instruction(path=self.root, instruction="Interactive yes", change=True,
                                  adapter=FakeWorkerAdapter(mutate_fn=mutate_a), confirm=None)
        self.assertEqual(res.status, "COMPLETED", res.message)
        self.assertIn("Tests: PASSED", out.getvalue())
        self.assertLess(out.getvalue().index("Tests: PASSED"), out.getvalue().index("--- Diff preview ---"))
        self.assertIn("return 'yes'", (self.root / "pkg" / "a.py").read_text())

    def test_new_commit_with_clean_tree_is_target_changed(self) -> None:
        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text("def get_value():\n    return 'raced'\n")

        def confirm_after_commit(_: str) -> bool:
            (self.root / "pkg" / "b.py").write_text("def check_val():\n    return 'other'\n")
            subprocess.run(["git", "-C", str(self.root), "commit", "-qam", "concurrent"], check=True,
                           capture_output=True)
            return True

        res = run_instruction(path=self.root, instruction="Race with a commit", change=True,
                              adapter=FakeWorkerAdapter(mutate_fn=mutate_a), confirm=confirm_after_commit)
        self.assertEqual(res.code, TARGET_CHANGED, res.message)
        self.assertIn("return 'old'", (self.root / "pkg" / "a.py").read_text())

    def test_config_changed_after_load_is_target_changed(self) -> None:
        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text("def get_value():\n    return 'cfg'\n")

        self.write_change_yaml(f'test_command: ["{PY}", "-c", "print(1)"]\n')

        def confirm_after_config_edit(_: str) -> bool:
            self.write_change_yaml(f'test_command: ["{PY}", "-c", "print(2)"]\n')
            return True

        res = run_instruction(path=self.root, instruction="Config edited", change=True,
                              adapter=FakeWorkerAdapter(mutate_fn=mutate_a), confirm=confirm_after_config_edit)
        self.assertEqual(res.code, TARGET_CHANGED, res.message)
        self.assertIn("return 'old'", (self.root / "pkg" / "a.py").read_text())

    def test_config_rejects_line_breaks_and_oversize(self) -> None:
        from tabularium.change_config import ChangeConfigError, load_change_config
        for content in ('test_command: ["python", "-c", "print(1)\\nTests: forged"]\n',
                        'test_command: ["python\\r"]\n',
                        'test_command: ["python"]\n#' + "x" * (64 * 1024) + "\n"):
            with self.subTest(content=content[:40]):
                self.write_change_yaml(content)
                with self.assertRaises(ChangeConfigError):
                    load_change_config(self.root)


class TestChangeConfigDuplicateKeysAndCopyErrors(ChangeTestsFixture):
    """Validation of duplicate config keys and copy error handling."""

    def test_duplicate_config_key_is_invalid(self) -> None:
        from tabularium.change_config import ChangeConfigError, load_change_config
        self.write_change_yaml('test_command: ["good"]\ntest_command: ["bad"]\n')
        with self.assertRaises(ChangeConfigError):
            load_change_config(self.root)
        self.write_change_yaml('test_command: ["python", "-c", "print({1: 2})"]\n')
        self.assertEqual(load_change_config(self.root).test_command[0], "python")

    def test_oserror_while_copying_is_a_refusal_not_a_crash(self) -> None:
        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text("def get_value():\n    return 'enospc'\n")

        self.write_change_yaml(f'test_command: ["{PY}", "-c", "print(1)"]\n')
        confirm = MagicMock(return_value=True)
        with patch("tabularium.change_path.run_tests_on_copy", side_effect=OSError(28, "No space left on device")):
            res = run_instruction(path=self.root, instruction="Disk full", change=True,
                                  adapter=FakeWorkerAdapter(mutate_fn=mutate_a), confirm=confirm)
        self.assertEqual(res.status, "REFUSED")
        self.assertEqual(res.code, SANDBOX_UNAVAILABLE)
        self.assertIn("No space left", res.message)
        confirm.assert_not_called()
        self.assertIn("return 'old'", (self.root / "pkg" / "a.py").read_text())


class TestIsolatedTestExecutionHardening(ChangeTestsFixture):
    """Hardening for git clean checks, executable refusal, timeouts, and diagnostic recording."""

    def _git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def test_moving_a_tracked_file_into_ai_is_dirty(self) -> None:
        from tabularium.change_path import check_git_clean
        self.assertTrue(check_git_clean(self.root)[0])
        self._git("mv", "pkg/b.py", ".ai/b.py")
        self.assertEqual(check_git_clean(self.root)[1], "GIT_DIRTY")

    def test_changes_only_under_ai_stay_clean(self) -> None:
        from tabularium.change_path import check_git_clean
        (self.root / ".ai" / "note.md").write_text("x\n")
        self._git("add", ".ai/note.md")
        self._git("commit", "-qm", "note")
        self._git("mv", ".ai/note.md", ".ai/note2.md")
        self.assertTrue(check_git_clean(self.root)[0], check_git_clean(self.root))

    def test_unhashable_yaml_key_is_invalid_config(self) -> None:
        from tabularium.change_config import ChangeConfigError, load_change_config
        for content in ("[a, b]: 1\ntest_command: [x]\n", "{a: 1}: 1\ntest_command: [x]\n"):
            with self.subTest(content=content):
                self.write_change_yaml(content)
                with self.assertRaises(ChangeConfigError):
                    load_change_config(self.root)

    def test_new_executable_file_is_refused_before_prompt(self) -> None:
        def mutate(wt: Path) -> None:
            script = wt / "run.sh"
            script.write_text("#!/bin/sh\necho hi\n")
            script.chmod(0o755)

        confirm = MagicMock(return_value=True)
        res = run_instruction(path=self.root, instruction="Add a script", change=True,
                              adapter=FakeWorkerAdapter(mutate_fn=mutate), confirm=confirm)
        self.assertEqual(res.status, "REFUSED")
        self.assertEqual(res.code, "DIFF_UNSUPPORTED", res.message)
        confirm.assert_not_called()
        self.assertFalse((self.root / "run.sh").exists())

    def test_warnings_and_command_line_are_sanitized(self) -> None:
        from tabularium import cli
        from tabularium.execution import ProductResult
        err = io.StringIO()
        with patch("sys.stderr", err):
            cli._emit_warnings(ProductResult(status="COMPLETED", code="C", message="m", target="/t",
                                             warnings=("hook said \x1b[2J\u202efake",)))
        self.assertNotIn("\x1b", err.getvalue())
        self.assertNotIn("\u202e", err.getvalue())

        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text("def get_value():\n    return 'cmd'\n")

        self.write_change_yaml(f'test_command: ["{PY}", "-c", "print(1)", "\\e[2J"]\n')
        out = io.StringIO()
        with patch("sys.stdout", out):
            run_instruction(path=self.root, instruction="Escape in command", change=True,
                            adapter=FakeWorkerAdapter(mutate_fn=mutate_a), confirm=lambda _: False)
        self.assertIn("Running tests in an isolated copy:", out.getvalue())
        self.assertNotIn("\x1b", out.getvalue())

    def test_config_digest_is_of_the_parsed_bytes(self) -> None:
        from tabularium.change_config import config_digest, load_change_config
        self.write_change_yaml(f'test_command: ["{PY}"]\n')
        self.assertEqual(load_change_config(self.root).digest, config_digest(self.root))

    @needs_bwrap
    def test_timeout_kills_children_holding_the_pipes(self) -> None:
        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text("def get_value():\n    return 'kids'\n")

        self.write_change_yaml(
            f'test_command: ["/bin/sh", "-c", "sleep 30 & sleep 30 & wait"]\ntimeout_seconds: 2\n')
        t0 = time.monotonic()
        res = run_instruction(path=self.root, instruction="Children", change=True,
                              adapter=FakeWorkerAdapter(mutate_fn=mutate_a), confirm=lambda _: True)
        self.assertEqual(res.code, ISOLATED_TESTS_FAILED)
        self.assertLess(time.monotonic() - t0, 10.0)
        self.assertTrue(res.change_result["tests"]["timed_out"])

    def test_refusals_record_their_reason_on_the_candidate_task(self) -> None:
        from tabularium.task import read_frontmatter

        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text("def get_value():\n    return 'why'\n")

        self.write_change_yaml(f'test_command: ["{PY}", "-c", "print(1)"]\n')
        with patch("tabularium.change_path.run_tests_on_copy", side_effect=OSError(28, "No space left")):
            run_instruction(path=self.root, instruction="Record reason", change=True,
                            adapter=FakeWorkerAdapter(mutate_fn=mutate_a), confirm=lambda _: True)
        results = [read_frontmatter(tf, self.root, "Task").frontmatter
                   for tf in (self.root / ".ai" / "tasks").glob("*.md")]
        self.assertIn(("CANCELLED", SANDBOX_UNAVAILABLE),
                      [(fm.get("status"), fm.get("result")) for fm in results])


class TestIsolatedTestSecurityHardening(ChangeTestsFixture):
    """Security checks for overlay races, warning sanitization, and config tampering."""

    def test_tested_bytes_are_the_approved_diff_not_a_later_worktree(self) -> None:
        import tabularium.change_path as cp

        good = "def get_value():\n    return 'good'\n"
        evil = "def get_value():\n    return 'evil'\n"

        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text(good)

        real_preflight, real_tests = cp.apply_preflight_reason, cp.run_tests_on_copy
        seen: dict[str, str] = {}

        def preflight_then_tamper(target, worktree, fp):
            reason = real_preflight(target, worktree, fp)
            (worktree.path / "pkg" / "a.py").write_text(evil)  # a lingering worker process
            return reason

        def tests_then_restore(project, overlay, command, **kwargs):
            seen.update(overlay)
            return real_tests(project, overlay, command, **kwargs)

        self.write_change_yaml(f'test_command: ["{PY}", "-c", "print(1)"]\n')
        with patch.object(cp, "apply_preflight_reason", side_effect=preflight_then_tamper), \
             patch.object(cp, "run_tests_on_copy", side_effect=tests_then_restore):
            res = run_instruction(path=self.root, instruction="Race the overlay", change=True,
                                  adapter=FakeWorkerAdapter(mutate_fn=mutate_a), confirm=lambda _: True)
        self.assertEqual(seen.get("pkg/a.py"), good)
        # The worktree no longer matches the approved diff, so nothing is applied.
        self.assertNotEqual(res.status, "COMPLETED", res.message)
        self.assertIn("return 'old'", (self.root / "pkg" / "a.py").read_text())

    def test_commit_failure_warning_is_sanitized(self) -> None:
        def mutate_a(wt: Path) -> None:
            (wt / "pkg" / "a.py").write_text("def get_value():\n    return 'hook'\n")

        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nprintf 'rejected \\033[2J\\302\\233fake\\n' >&2\nexit 1\n")
        hook.chmod(0o755)
        err = io.StringIO()
        with patch("sys.stderr", err):
            res = run_instruction(path=self.root, instruction="Hook fails", change=True,
                                  adapter=FakeWorkerAdapter(mutate_fn=mutate_a), confirm=lambda _: True)
        self.assertEqual(res.status, "COMPLETED", res.message)
        self.assertIn("not committed", err.getvalue())
        self.assertNotIn("\x1b", err.getvalue())
        self.assertNotIn("\x9b", err.getvalue())

    def test_deeply_nested_yaml_is_invalid_config(self) -> None:
        from tabularium.change_config import ChangeConfigError, load_change_config
        self.write_change_yaml("test_command: " + "[" * 5000 + "]" * 5000 + "\n")
        with self.assertRaises(ChangeConfigError):
            load_change_config(self.root)

    def test_non_venv_relative_command_is_normalized(self) -> None:
        from tabularium.change_path import resolve_test_command
        self.assertEqual(resolve_test_command(self.root, ["./.venv/../x.sh", "-q"]), ["./x.sh", "-q"])
        self.assertEqual(resolve_test_command(self.root, ["scripts/test"]), ["./scripts/test"])

    def test_tampered_config_never_reaches_the_test_runner(self) -> None:
        def mutate(wt: Path) -> None:
            (wt / ".ai" / "change.yaml").write_text('test_command: ["/bin/true"]\n')

        self.write_change_yaml(f'test_command: ["{PY}", "-c", "print(1)"]\n')
        with patch("tabularium.change_path.run_tests_on_copy") as runner:
            res = run_instruction(path=self.root, instruction="Tamper", change=True,
                                  adapter=FakeWorkerAdapter(mutate_fn=mutate), confirm=lambda _: True)
        self.assertEqual(res.status, "REFUSED")
        runner.assert_not_called()
