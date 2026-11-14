"""Policy for `--change` when no test command is configured.

Without `.ai/change.yaml` a change is refused before any worker runs, unless
the person explicitly allows an untested change for this run
(`--allow-untested`). The override is recorded in the result, the execution
report and the commit; it never reads as passed tests.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from tabularium.cli import main
from tabularium import explain
from tabularium.execution import run_instruction
from tabularium.engine.code_gate.isolated_run import IsolatedRun, SandboxError

from tests.test_v2_04_change_tests import ChangeTestsFixture, PY
from tests.test_v2_09_change_path import FakeWorkerAdapter
from tests.test_phase1_i8_explain import CountingWorker, ExplainFixture


def _mutate(wt: Path) -> None:
    (wt / "pkg" / "a.py").write_text("def get_value():\n    return 'new'\n")


class TestUntestedChangePolicy(ChangeTestsFixture):

    def _tasks(self) -> set[str]:
        return {p.name for p in (self.root / ".ai" / "tasks").glob("*.md")}

    def _head(self) -> str:
        return subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()

    def _change_report(self) -> str:
        texts = [p.read_text(encoding="utf-8")
                 for p in (self.root / ".ai" / "reports").glob("*.md")]
        change = [t for t in texts if "### Change Path Record" in t]
        self.assertEqual(len(change), 1)
        return change[0]

    def _assert_refused_before_worker(self, res, adapter, tasks, head, confirm_calls):
        self.assertEqual(res.status, "REFUSED")
        self.assertEqual(res.code, "TESTS_NOT_CONFIGURED")
        self.assertIn(".ai/change.yaml", res.message)
        self.assertIn("--allow-untested", res.message)
        self.assertEqual(adapter.call_count, 0, "the worker must not run")
        self.assertEqual(confirm_calls, [], "no approval is asked")
        self.assertEqual(self._tasks(), tasks, "no candidate Task is created")
        self.assertEqual(self._head(), head)
        self.assertEqual((self.root / "pkg" / "a.py").read_text(),
                         "def get_value():\n    return 'old'\n")

    def test_missing_config_is_refused_interactive(self):
        adapter = FakeWorkerAdapter(mutate_fn=_mutate)
        calls: list[str] = []
        tasks, head = self._tasks(), self._head()
        res = run_instruction("Update a.py", self.root, adapter=adapter, change=True,
                              confirm=lambda p: calls.append(p) or True)
        self._assert_refused_before_worker(res, adapter, tasks, head, calls)

    def test_missing_config_is_refused_non_interactive(self):
        adapter = FakeWorkerAdapter(mutate_fn=_mutate)
        tasks, head = self._tasks(), self._head()
        with patch("sys.stdin", io.StringIO("")):
            res = run_instruction("Update a.py", self.root, adapter=adapter, change=True)
        self._assert_refused_before_worker(res, adapter, tasks, head, [])

    def test_explicit_override_applies_and_records_untested(self):
        adapter = FakeWorkerAdapter(mutate_fn=_mutate)
        calls: list[str] = []
        out = io.StringIO()
        with patch("sys.stdout", out):
            res = run_instruction("Update a.py", self.root, adapter=adapter, change=True,
                                  allow_untested=True,
                                  confirm=lambda p: calls.append(p) or True)
        self.assertEqual(res.status, "COMPLETED")
        self.assertEqual(len(calls), 1)

        tests = res.change_result["tests"]
        self.assertEqual(tests["status"], "NOT_CONFIGURED")
        self.assertNotEqual(tests["status"], "PASSED")
        self.assertTrue(tests["untested_override"])

        warning = "Tests: NOT RUN (no .ai/change.yaml; untested change allowed by --allow-untested)"
        self.assertIn(warning, res.warnings)
        self.assertIn(warning, out.getvalue())

        msg = subprocess.run(["git", "-C", str(self.root), "log", "-1", "--pretty=%B"],
                             capture_output=True, text=True, check=True).stdout
        self.assertIn("Tests: not run (no .ai/change.yaml; --allow-untested)", msg)
        self.assertNotIn("Tests: passed", msg)

        self.assertIn("- Tests: NOT_CONFIGURED (untested change allowed by --allow-untested)",
                      self._change_report())

    def test_explicit_override_still_needs_interactive_approval(self):
        adapter = FakeWorkerAdapter(mutate_fn=_mutate)
        with patch("sys.stdin", io.StringIO("")):
            res = run_instruction("Update a.py", self.root, adapter=adapter, change=True,
                                  allow_untested=True)
        self.assertEqual(res.status, "AWAITING_APPROVAL")
        self.assertEqual((self.root / "pkg" / "a.py").read_text(),
                         "def get_value():\n    return 'old'\n")

    def test_declined_approval_with_override_applies_nothing(self):
        adapter = FakeWorkerAdapter(mutate_fn=_mutate)
        res = run_instruction("Update a.py", self.root, adapter=adapter, change=True,
                              allow_untested=True, confirm=lambda _: False)
        self.assertEqual(res.code, "APPROVAL_REJECTED")
        self.assertTrue(res.change_result["tests"]["untested_override"])
        self.assertEqual((self.root / "pkg" / "a.py").read_text(),
                         "def get_value():\n    return 'old'\n")

    def test_configured_tests_record_passed_without_override(self):
        self.write_change_yaml(f'test_command: ["{PY}", "-c", "pass"]\ntimeout_seconds: 30\n')
        adapter = FakeWorkerAdapter(mutate_fn=_mutate)
        passed = IsolatedRun(0, "", "", False, False)
        with patch("sys.stdout", io.StringIO()), \
                patch("tabularium.change_path.run_tests_on_copy", return_value=passed) as runner:
            res = run_instruction("Update a.py", self.root, adapter=adapter, change=True,
                                  confirm=lambda _: True)
        self.assertEqual(res.status, "COMPLETED")
        runner.assert_called_once_with(
            self.root, {"pkg/a.py": "def get_value():\n    return 'new'\n"},
            [PY, "-c", "pass"], timeout=30.0)
        self.assertEqual(res.change_result["tests"]["status"], "PASSED")
        self.assertFalse(res.change_result["tests"]["untested_override"])
        self.assertIn("- Tests: PASSED", self._change_report())
        self.assertIn(f"Tests: passed ({PY} -c pass)", self.get_last_commit_message())
        self.assertNotIn("--allow-untested", self.get_last_commit_message())

    def test_configured_failing_tests_refuse_even_with_override(self):
        self.write_change_yaml(f'test_command: ["{PY}", "-c", "raise SystemExit(1)"]\n')
        adapter = FakeWorkerAdapter(mutate_fn=_mutate)
        calls = []
        head = self._head()
        failed = IsolatedRun(1, "", "assertion failed", False, False)
        with patch("sys.stdout", io.StringIO()), \
                patch("tabularium.change_path.run_tests_on_copy", return_value=failed) as runner:
            res = run_instruction("Update a.py", self.root, adapter=adapter, change=True,
                                  allow_untested=True,
                                  confirm=lambda p: calls.append(p) or True)
        self.assertEqual(res.code, "ISOLATED_TESTS_FAILED")
        runner.assert_called_once()
        self.assertEqual(calls, [])
        self.assertEqual(res.change_result["tests"]["status"], "FAILED")
        self.assertFalse(res.change_result["tests"]["untested_override"])
        self.assertEqual(self._head(), head)
        self.assertEqual((self.root / "pkg" / "a.py").read_text(),
                         "def get_value():\n    return 'old'\n")

    def test_configured_success_with_flag_is_still_tested(self):
        self.write_change_yaml('test_command: ["test-runner"]\n')
        passed = IsolatedRun(0, "", "", False, False)
        with patch("sys.stdout", io.StringIO()), \
                patch("tabularium.change_path.run_tests_on_copy", return_value=passed) as runner:
            res = run_instruction("Update a.py", self.root,
                                  adapter=FakeWorkerAdapter(mutate_fn=_mutate),
                                  change=True, allow_untested=True, confirm=lambda _: True)
        self.assertEqual(res.status, "COMPLETED")
        runner.assert_called_once()
        self.assertEqual(res.change_result["tests"]["status"], "PASSED")
        self.assertFalse(res.change_result["tests"]["untested_override"])
        self.assertNotIn("--allow-untested", self._change_report())

    def test_override_never_bypasses_invalid_configuration(self):
        for content in ('timeout_seconds: 30\n', 'test_command: []\n', 'broken: ['):
            with self.subTest(content=content):
                self.write_change_yaml(content)
                adapter = FakeWorkerAdapter(mutate_fn=_mutate)
                res = run_instruction("Update a.py", self.root, adapter=adapter,
                                      change=True, allow_untested=True, confirm=lambda _: True)
                self.assertEqual(res.code, "CHANGE_CONFIG_INVALID")
                self.assertEqual(adapter.call_count, 0)

    def test_override_never_bypasses_timeout_or_sandbox_failure(self):
        self.write_change_yaml('test_command: ["test-runner"]\n')
        head = self._head()
        for result, error, code in (
            (IsolatedRun(None, "", "", True, False), None, "ISOLATED_TESTS_FAILED"),
            (None, SandboxError("SANDBOX_UNAVAILABLE", "no sandbox"), "SANDBOX_UNAVAILABLE"),
        ):
            with self.subTest(code=code), patch("sys.stdout", io.StringIO()), \
                    patch("tabularium.change_path.run_tests_on_copy",
                          return_value=result, side_effect=error):
                calls = []
                res = run_instruction("Update a.py", self.root,
                                      adapter=FakeWorkerAdapter(mutate_fn=_mutate),
                                      change=True, allow_untested=True,
                                      confirm=lambda p: calls.append(p) or True)
                self.assertEqual(res.code, code)
                self.assertEqual(calls, [])
                self.assertEqual(res.change_result["tests"]["status"], "FAILED")
                self.assertFalse(res.change_result["tests"]["untested_override"])
                self.assertEqual(self._head(), head)


class TestUntestedPolicyCli(ChangeTestsFixture):

    def test_cli_refusal_names_the_options(self):
        err = io.StringIO()
        with patch("sys.stderr", err), patch("sys.stdout", io.StringIO()), \
                patch("tabularium.change_path.find_native_executable",
                      return_value="/nonexistent/codex"):
            code = main(["run", "Update a.py", str(self.root), "--change", "--worker", "native"])
        self.assertNotEqual(code, 0)
        self.assertIn("Reason: TESTS_NOT_CONFIGURED", err.getvalue())
        self.assertIn("--allow-untested", err.getvalue())

    def test_cli_flag_reaches_run_instruction(self):
        seen = {}

        def fake_run(*args, **kwargs):
            seen.update(kwargs)
            raise SystemExit(0)

        with patch("tabularium.cli.run_instruction", side_effect=fake_run), \
                patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["run", "Update a.py", str(self.root), "--change", "--allow-untested"])
        self.assertIs(seen.get("allow_untested"), True)
        self.assertIs(seen.get("change"), True)


class TestTestReadiness(ExplainFixture):

    def test_missing_configuration_is_a_read_only_diagnostic(self):
        before = self.ai_snapshot(self.root)
        worker = CountingWorker()
        result = explain.preflight("Update code", self.root, adapter=worker)
        self.assertTrue(result.would_run, "read-only execution does not require tests")
        diagnostic = next(g for g in result.gates if g.gate == "test_readiness")
        self.assertEqual(diagnostic.outcome, "SKIPPED")
        self.assertIn("--change is refused", diagnostic.detail)
        self.assertIn("--allow-untested", explain.render_human(result))
        self.assertEqual(worker.calls, 0)
        self.assertEqual(self.ai_snapshot(self.root), before)

    def test_change_preflights_refuse_without_writing_or_executing(self):
        before = self.ai_snapshot(self.root)
        from tabularium.execution import _checkpoint_path
        marker = _checkpoint_path(self.root).with_suffix(".execution")
        self.assertFalse(marker.exists())
        with patch("tabularium.cli.run_instruction") as run, \
                patch("tabularium.change_path.run_tests_on_copy") as tests, \
                patch("tabularium.change_path.create_worktree") as worktree:
            for argv in (
                ["explain", "Update code", str(self.root), "--change"],
                ["run", "Update code", str(self.root), "--change", "--dry-run"],
                ["run", "Update code", str(self.root), "--change", "--dry-run", "--json"],
            ):
                with self.subTest(argv=argv):
                    code, out, err = self.run_main(argv)
                    self.assertEqual(code, 1)
                    self.assertIn("TESTS_NOT_CONFIGURED", out + err)
                    self.assertIn("--allow-untested", out + err)
            run.assert_not_called()
            tests.assert_not_called()
            worktree.assert_not_called()
        self.assertFalse(marker.exists())
        self.assertEqual(self.ai_snapshot(self.root), before)

    def test_cli_override_preflights_record_choice_without_running_tests(self):
        before = self.ai_snapshot(self.root)
        for argv in (
            ["explain", "Update code", str(self.root), "--change", "--allow-untested", "--json"],
            ["run", "Update code", str(self.root), "--change", "--allow-untested", "--dry-run", "--json"],
        ):
            with self.subTest(argv=argv):
                code, out, _ = self.run_main(argv)
                self.assertEqual(code, 0)
                result = json.loads(out)
                self.assertTrue(result["approval_required"])
                diagnostic = next(g for g in result["gates"] if g["gate"] == "test_readiness")
                self.assertEqual(diagnostic["outcome"], "SKIPPED")
                self.assertIn("NOT_CONFIGURED", diagnostic["detail"])
                self.assertIn("--allow-untested", diagnostic["detail"])
                self.assertIn("approval still required", diagnostic["detail"])
        self.assertEqual(self.ai_snapshot(self.root), before)

    def test_configured_preflight_reports_readiness_without_claiming_passed_tests(self):
        (self.root / ".ai" / "change.yaml").write_text('test_command: ["test-runner"]\n')
        before = self.ai_snapshot(self.root)
        worker = CountingWorker()
        with patch("tabularium.change_path.run_tests_on_copy") as runner:
            for command in ("explain", "dry-run"):
                result = explain.preflight("Update code", self.root, adapter=worker,
                                           command=command, change=True)
                self.assertTrue(result.would_run)
                diagnostic = next(g for g in result.gates if g.gate == "test_readiness")
                self.assertEqual(diagnostic.outcome, "SKIPPED")
                self.assertIn("CONFIGURED", diagnostic.detail)
                self.assertIn("not run", diagnostic.detail)
            runner.assert_not_called()
        self.assertEqual(worker.calls, 0)
        self.assertEqual(self.ai_snapshot(self.root), before)

    def test_invalid_configuration_refuses_even_with_preflight_override(self):
        (self.root / ".ai" / "change.yaml").write_text('test_command: []\n')
        before = self.ai_snapshot(self.root)
        result = explain.preflight("Update code", self.root, change=True, allow_untested=True)
        self.assertEqual(result.code, "CHANGE_CONFIG_INVALID")
        self.assertFalse(result.would_run)
        self.assertEqual(self.ai_snapshot(self.root), before)
