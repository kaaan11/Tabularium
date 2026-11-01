"""M7.1 product entrypoint and project scaffold tests."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tabularium.cli import _emit_execution, main
from tabularium.execution import ProductResult
from tabularium.init import RULESET_VERSION, initialize
from tabularium.onboarding import build_authorizations, onboard_project
from tabularium.version import get_version
from tabularium.engine.context_loader import load_context
from tabularium.engine.execution.model import ContextMetrics, PipelineResult


ROOT = Path(__file__).resolve().parents[1]

try:
    import langgraph  # noqa: F401
    HAS_RUNTIME = True
except ImportError:  # pragma: no cover - environment dependent
    HAS_RUNTIME = False

needs_runtime = unittest.skipUnless(
    HAS_RUNTIME, "LangGraph not installed (tabularium/engine/requirements-runtime.txt)")


def tree_fingerprint(root: Path) -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        out[path.relative_to(root).as_posix()] = (digest, path.stat().st_mtime_ns)
    return out


class ProductEntrypointTests(unittest.TestCase):
    def run_cli(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        return subprocess.run(
            [sys.executable, "-m", "tabularium", *args],
            cwd=cwd or ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_version_is_bound_to_VERSION(self):
        self.assertEqual(get_version(), (ROOT / "VERSION").read_text().strip())

    def test_help_and_version_are_product_oriented(self):
        version = self.run_cli("--version")
        help_result = self.run_cli("--help")
        self.assertEqual(version.returncode, 0)
        self.assertIn(f"tabu {get_version()}", version.stdout)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("init", help_result.stdout)
        self.assertNotIn("ContextPackage", help_result.stdout)
        self.assertNotIn("LangGraph", help_result.stdout)

    def test_init_is_idempotent_and_loader_compatible(self):
        with tempfile.TemporaryDirectory(prefix="tabu-m71-") as raw:
            target = Path(raw)
            first = initialize(target)
            self.assertEqual(first.status, "INITIALIZED")
            before = tree_fingerprint(target / ".ai")
            time.sleep(0.02)
            second = initialize(target)
            self.assertEqual(second.status, "ALREADY_INITIALIZED")
            self.assertEqual(before, tree_fingerprint(target / ".ai"))

            package = load_context(target)
            self.assertEqual(package.entry_mode, "GOVERNED")
            self.assertEqual(package.package_status, "READY")
            self.assertEqual(package.task_actionability, "NOT_APPLICABLE")
            self.assertEqual(package.foundations["manifest"]["tabularium_ruleset"], RULESET_VERSION)
            self.assertIsNotNone(package.foundations["project.md"])

    def test_partial_and_malformed_projects_are_not_repaired(self):
        with tempfile.TemporaryDirectory(prefix="tabu-m71-") as raw:
            target = Path(raw)
            (target / ".ai").mkdir()
            sentinel = target / ".ai" / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            partial = initialize(target)
            self.assertEqual(partial.status, "PARTIAL_INITIALIZATION")
            # B6-B: the refusal names which required artifacts are absent.
            self.assertIsNotNone(partial.message)
            self.assertIn(".ai/manifest.yaml", partial.message)
            self.assertIn(".ai/tasks", partial.message)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

        with tempfile.TemporaryDirectory(prefix="tabu-m71-") as raw:
            target = Path(raw)
            (target / ".ai").mkdir()
            (target / ".ai" / "manifest.yaml").write_text(
                "manifest_version: nope\n", encoding="utf-8"
            )
            conflict = initialize(target)
            self.assertEqual(conflict.status, "EXISTING_AI_DIRECTORY_CONFLICT")

    def test_ruleset_mismatch_is_reported_consistently_and_never_as_malformed(self):
        """D6/D7: a structurally valid manifest for another Tabularium version is
        not a structural conflict. init/onboard/status must agree on the
        VERSION_MISMATCH vocabulary and never claim the .ai tree is
        malformed."""
        with tempfile.TemporaryDirectory(prefix="tabu-d6-") as raw:
            target = Path(raw)
            initialize(target)
            manifest = target / ".ai" / "manifest.yaml"
            manifest.write_text(
                'manifest_version: "1.0"\n'
                'tabularium_ruleset: "9.9.9"\n'
                'title: "Version skew fixture"\n', encoding="utf-8")
            before = tree_fingerprint(target / ".ai")

            result = initialize(target)
            self.assertEqual(result.status, "VERSION_MISMATCH")
            self.assertEqual(result.code, "VERSION_MISMATCH")
            self.assertIn("different Tabularium ruleset", result.message)
            self.assertNotIn("malformed", result.message)
            self.assertNotIn("redirected", result.message)

            onboarding = onboard_project(target)
            self.assertEqual(onboarding.code, "VERSION_MISMATCH")
            self.assertNotIn("malformed", onboarding.message)

            from tabularium.diagnostics import doctor_project
            health = doctor_project(target)
            self.assertEqual(health.overall_state, "VERSION_MISMATCH")

            self.assertEqual(tree_fingerprint(target / ".ai"), before)

    def test_json_result_is_machine_readable(self):
        with tempfile.TemporaryDirectory(prefix="tabu-m71-") as raw:
            result = self.run_cli("init", "--json", cwd=Path(raw))
            self.assertEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "INITIALIZED")
            self.assertEqual(payload["created"], sorted(payload["created"]))

    def test_json_failure_stays_machine_readable_on_stderr(self):
        """D13: a failed init under --json still emits exactly one parseable
        result object — on the failure stream, with a non-zero exit and its
        stable product code intact."""
        with tempfile.TemporaryDirectory(prefix="tabu-d13-") as raw:
            missing = Path(raw) / "missing"
            result = self.run_cli("init", "--json", str(missing))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            payload = json.loads(result.stderr)
            self.assertEqual(payload["status"], "FAILED")
            self.assertEqual(payload["code"], "TARGET_NOT_FOUND")
            self.assertFalse(missing.exists())

    def test_invalid_target_has_product_error_and_no_write(self):
        with tempfile.TemporaryDirectory(prefix="tabu-m71-") as raw:
            missing = Path(raw) / "missing"
            result = self.run_cli("init", str(missing))
            self.assertEqual(result.returncode, 2)
            self.assertIn("target does not exist", result.stderr)
            self.assertFalse(missing.exists())

    def test_source_init_from_outside_repository_cwd(self):
        with tempfile.TemporaryDirectory(prefix="tabu-m71-") as raw:
            target = Path(raw) / "project with ünicode"
            target.mkdir()
            result = self.run_cli("init", cwd=target)
            self.assertEqual(result.returncode, 0)
            self.assertTrue((target / ".ai" / "manifest.yaml").is_file())

    def test_version_help_and_fresh_init_do_not_open_network(self):
        with tempfile.TemporaryDirectory(prefix="tabu-m71-") as raw:
            target = Path(raw)
            with patch("socket.socket", side_effect=AssertionError("network")), \
                    patch("socket.create_connection", side_effect=AssertionError("network")):
                with self.assertRaises(SystemExit):
                    main(["--version"])
                with self.assertRaises(SystemExit):
                    main(["--help"])
                self.assertEqual(initialize(target).status, "INITIALIZED")

    def test_import_does_not_load_optional_runtime_modules(self):
        script = (
            "import sys; import tabularium; "
            "print(any(name.startswith(('langgraph', 'litellm', 'opentelemetry', 'langfuse')) "
            "for name in sys.modules))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "False")


class ExecutionStreamOrderTests(unittest.TestCase):
    """B6-A1: merged stdout/stderr capture must preserve the program's
    semantic emission order. Stream OWNERSHIP is intentional and pinned too:
    header/metrics/status/success-output belong on stdout; Reason/refusal/
    failure messages belong on stderr."""

    @classmethod
    def setUpClass(cls):
        cls.holder = tempfile.TemporaryDirectory(prefix="tabu-stream-")
        root = Path(cls.holder.name)
        initialize(root)
        declarations = {
            "title": "Stream order fixture",
            "purpose": "Exercise CLI stream discipline",
            "scope": "A bounded product-flow test project",
        }
        draft = onboard_project(root, declarations)
        onboard_project(
            root, declarations,
            build_authorizations(draft.proposal, "human:user",
                                 "2026-08-19T12:00:00+00:00"),
        )
        tasks = root / ".ai" / "tasks"
        tasks.mkdir(exist_ok=True)
        (tasks / "TASK-2026-0001.md").write_text(
            "---\n"
            'schema_version: "1.10"\n'
            "id: TASK-2026-0001\n"
            "title: Stream order fixture task\n"
            "objective: Exercise the governed run entrypoint offline.\n"
            "status: INBOX\n"
            'created_at: "2026-01-01T00:00:00Z"\n'
            "created_by: USER\n"
            "---\n# Task\n\nFixture body.\n",
            encoding="utf-8")
        # Offline deterministic native success through the documented
        # TABU_NATIVE_EXECUTABLE override: a valid codex-shaped JSON event.
        # The transport spawns the path directly, so the fixture needs a
        # shebang and the executable bit.
        fixture = root / "echo_fixture.py"
        fixture.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'type': 'item.completed', 'item': "
            "{'type': 'agent_message', 'text': 'STREAM_OK'}}))\n",
            encoding="utf-8")
        fixture.chmod(0o755)
        cls.root = root

    @classmethod
    def tearDownClass(cls):
        cls.holder.cleanup()

    def run_cli(self, *args, merge=False, env_extra=None):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, "-m", "tabularium", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge else subprocess.PIPE,
            text=True,
            env=env,
            check=False,
        )

    # A1 -- merged redirection preserves semantic order -------------------------

    def assert_semantic_order(self, out):
        self.assertLess(out.index("Tabularium"), out.index("Status:"))
        self.assertLess(out.index("Status:"), out.index("Reason:"))

    def assert_no_duplicates(self, out):
        lines = out.splitlines()
        self.assertEqual(1, sum(1 for ln in lines if ln == "Tabularium"))
        self.assertEqual(1, sum(1 for ln in lines if ln.startswith("Reason:")))
        self.assertEqual(1, sum(1 for ln in lines
                                if ln.startswith("Status: refused")))

    def test_merged_run_refusal_keeps_header_before_reason(self):
        with tempfile.TemporaryDirectory(prefix="tabu-stream-empty-") as raw:
            empty = Path(raw) / "empty"
            empty.mkdir()
            for _ in range(3):  # A5 determinism
                result = self.run_cli("run", "hi", str(empty), merge=True)
                self.assertEqual(result.returncode, 1)
                self.assert_semantic_order(result.stdout)
                self.assert_no_duplicates(result.stdout)

    # A4 -- continue shares the same emitter --------------------------------------

    def test_merged_continue_refusal_keeps_header_before_reason(self):
        for _ in range(3):
            result = self.run_cli("continue", str(self.root), merge=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("NO_CONTINUATION", result.stdout)
            self.assert_semantic_order(result.stdout)
            self.assert_no_duplicates(result.stdout)

    # A1 -- shell-style and pipe-style merges share the same discipline ----------

    def _sh_capture(self, command):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        return subprocess.run(["/bin/sh", "-c", command],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True,
                              env=env, check=False)

    def test_shell_style_merge_preserves_order(self):
        with tempfile.TemporaryDirectory(prefix="tabu-stream-empty-") as raw:
            empty = Path(raw) / "empty"
            empty.mkdir()
            argv = (f"{shlex.quote(sys.executable)} -m tabularium run hi "
                    f"{shlex.quote(str(empty))} 2>&1")
            for _ in range(3):  # A5 determinism
                result = self._sh_capture(argv)
                self.assertEqual(result.returncode, 1)
                self.assertEqual("", result.stderr)
                self.assert_semantic_order(result.stdout)
                self.assert_no_duplicates(result.stdout)

    def test_pipe_style_merge_preserves_order(self):
        argv = (f"{shlex.quote(sys.executable)} -m tabularium continue "
                f"{shlex.quote(str(self.root))} 2>&1 | cat")
        for _ in range(3):
            # A POSIX pipeline reports the LAST command's status (cat), so the
            # child's own refusal exit code is not observable here; the
            # content assertions below cannot pass unless the CLI ran.
            result = self._sh_capture(argv)
            self.assertIn("NO_CONTINUATION", result.stdout)
            self.assert_semantic_order(result.stdout)
            self.assert_no_duplicates(result.stdout)

    # A2 -- separate capture pins stream ownership ------------------------------

    def test_separate_capture_preserves_stream_ownership(self):
        with tempfile.TemporaryDirectory(prefix="tabu-stream-empty-") as raw:
            empty = Path(raw) / "empty"
            empty.mkdir()
            result = self.run_cli("run", "hi", str(empty))
            self.assertEqual(result.returncode, 1)
            self.assertIn("Tabularium", result.stdout)
            self.assertIn("Status: refused", result.stdout)
            self.assertNotIn("Reason:", result.stdout)
            self.assertTrue(result.stderr.startswith("Reason:"))
            self.assertIn("Project is not initialized.", result.stderr)

    # A3 -- success path keeps stderr silent and output on stdout ---------------

    @staticmethod
    def completed_result():
        return ProductResult(
            "COMPLETED", "COMPLETED", "Work completed.", "/fixture",
            pipeline=PipelineResult(
                status="COMPLETED", worker_result={"worker_id": "codex-cli"},
                metrics=ContextMetrics(
                    governed_context={"estimated_tokens": 12},
                    payload_size={"estimated_tokens": 30},
                    reduction_pct=60.0),
                output="STREAM_OK"))

    def test_emitter_completed_writes_nothing_to_stderr(self):
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out_buf), \
                contextlib.redirect_stderr(err_buf):
            _emit_execution(self.completed_result())
        self.assertEqual("", err_buf.getvalue())
        out = out_buf.getvalue()
        self.assertIn("Worker: codex-cli", out)
        self.assertIn("STREAM_OK", out)
        self.assertEqual(1, sum(1 for ln in out.splitlines() if ln == "Tabularium"))

    def test_emitter_refusal_keeps_reason_on_stderr_only(self):
        refusal = ProductResult("REFUSED", "NOT_INITIALIZED",
                                "Project is not initialized.", "/fixture")
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out_buf), \
                contextlib.redirect_stderr(err_buf):
            _emit_execution(refusal)
        self.assertEqual(["Reason: NOT_INITIALIZED",
                          "Project is not initialized."],
                         err_buf.getvalue().splitlines())
        out = out_buf.getvalue()
        self.assertLess(out.index("Tabularium"), out.index("Status: refused"))
        self.assertNotIn("Reason:", out)

    def test_flush_failure_is_not_swallowed(self):
        class Broken(io.StringIO):
            def flush(self):
                raise OSError("stdout unavailable")

        refusal = ProductResult("REFUSED", "NOT_INITIALIZED",
                                "Project is not initialized.", "/fixture")
        with patch("sys.stdout", Broken()):
            with self.assertRaises(OSError):
                _emit_execution(refusal)

    @needs_runtime
    def test_successful_run_writes_nothing_to_stderr(self):
        result = self.run_cli(
            "run", "say hi", str(self.root), "--worker", "codex-cli",
            env_extra={"TABU_NATIVE_EXECUTABLE":
                       str(self.root / "echo_fixture.py")})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("STREAM_OK", result.stdout)
        self.assertIn("Status: completed", result.stdout)


if __name__ == "__main__":
    unittest.main()
