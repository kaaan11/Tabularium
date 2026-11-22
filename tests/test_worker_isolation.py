"""Offline worker containment contracts; provider CLIs are never invoked."""
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tabularium import change_path
from tabularium.cli import main
from tabularium.engine.code_gate import isolated_run
from tabularium.engine.code_gate.isolated_run import SandboxError, bwrap_args, run_isolated, probe_isolation
from tabularium.engine.workers.cli.worker import NativeCLIWorker
from tabularium.engine.workers.cli.opencode_worker import OpenCodeCLIWorker
from tabularium.execution import build_execution_report_proposal, run_instruction
from tests.test_v2_09_change_path import ChangePathFixture, FakeWorkerAdapter

AVAILABLE, WHY = probe_isolation(worker=True)
if os.environ.get("CI") and not AVAILABLE:
    raise RuntimeError(f"worker bwrap isolation is required in CI: {WHY}")
needs_bwrap = unittest.skipUnless(AVAILABLE, f"worker bwrap isolation unavailable: {WHY}")


class TestWorkerProfile(unittest.TestCase):
    def test_network_is_shared_but_filesystem_is_allowlisted(self):
        with tempfile.TemporaryDirectory() as scratch:
            args = bwrap_args("bwrap", Path(scratch), ["/usr/bin/true"], hidden=(),
                              status_fd=3, worker_env={})
        self.assertIn("--share-net", args)
        self.assertIn("--unshare-all", args)
        self.assertIn("--disable-userns", args)
        self.assertIn("--clearenv", args)
        self.assertNotIn("/opt", args)
        self.assertNotIn("/etc", args)
        self.assertNotIn(str(Path.home()), args)
        self.assertIn("/etc/resolv.conf", args)
        self.assertNotIn("/etc/ssl/private", args)

    def test_executable_is_bound_as_a_file_not_its_parent(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            exe = root / "stub"
            exe.write_text("#!/bin/sh\nexit 0\n")
            exe.chmod(0o755)
            tree = root / "tree"
            tree.mkdir()
            args = bwrap_args("bwrap", tree, [str(exe)], hidden=(), status_fd=3, worker_env={})
            self.assertIn(str(exe), args)
            self.assertNotIn(str(root), args)
            self.assertEqual(args[-1], "/worker/cli")

    def test_runtime_overlapping_original_checkout_is_refused(self):
        with self.assertRaises(SandboxError):
            bwrap_args("bwrap", Path("/tmp/wt"), ["/usr/bin/true"],
                       hidden=(Path("/usr/local/project"),), status_fd=3, worker_env={})

    def test_missing_bwrap_never_executes_worker(self):
        with tempfile.TemporaryDirectory() as scratch:
            marker = Path(scratch) / "escaped"
            with patch.object(isolated_run, "find_bwrap", return_value=None):
                with self.assertRaises(SandboxError) as caught:
                    run_isolated(scratch, ["/bin/sh", "-c", f"touch {marker}"], worker_env={})
            self.assertFalse(marker.exists())
            self.assertIn("bwrap", caught.exception.detail)

    def test_failed_bwrap_initialization_never_executes_worker(self):
        with tempfile.TemporaryDirectory() as scratch:
            marker = Path(scratch) / "escaped"
            with patch.object(isolated_run, "find_bwrap", return_value=shutil.which("false")):
                with self.assertRaises(SandboxError) as caught:
                    run_isolated(scratch, ["/bin/sh", "-c", f"touch {marker}"], worker_env={})
            self.assertFalse(marker.exists())
            self.assertIn("sandbox", caught.exception.detail)


@needs_bwrap
class TestWorkerFilesystem(unittest.TestCase):
    def test_stub_and_descendant_cannot_read_or_write_host_paths(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            tree = root / "worktree"
            tree.mkdir()
            original = root / "checkout"
            original.mkdir()
            (original / "secret").write_text("not for workers")
            host_state = root / "state"
            host_state.mkdir()
            (host_state / "auth.json").write_text("not for workers")
            (tree / "escape").symlink_to(original, target_is_directory=True)
            script = root / "stub"
            script.write_text("#!/usr/bin/python3\n" + f'''
import os, pathlib, subprocess
for target in ({str(original)!r}, {str(host_state)!r}, {str(Path.home())!r}, '/etc/shadow', '/etc/ssl/private'):
    assert not os.path.exists(target), target
assert not os.path.exists('/work/escape/secret')
for target in ({str(original / 'planted')!r}, {str(host_state / 'planted')!r}, '/usr/planted', '/planted'):
    try:
        open(target, 'w')
    except OSError:
        pass
    else:
        raise AssertionError(target)
assert 'TABU_TEST_SECRET' not in os.environ
assert os.environ['EXPLICIT_TEST_KEY'] == 'dummy-value'
assert os.environ['HOME'] == '/tmp/home'
pathlib.Path(os.environ['CODEX_HOME']).mkdir(parents=True, exist_ok=True)
pathlib.Path(os.environ['HOME'], 'state').write_text('private')
subprocess.run(['/bin/sh', '-c', 'test ! -e {original}/secret && echo child > /work/child'], check=True)
pathlib.Path('/work/edited').write_text('ok')
try:
    pathlib.Path('/work/.ai/policy').write_text('tampered')
except OSError:
    pass
else:
    raise AssertionError('governance writable')
print('confined')
''')
            script.chmod(0o755)
            (tree / ".ai").mkdir()
            (tree / ".ai" / "policy").write_text("governed")
            with patch.dict(os.environ, {"TABU_TEST_SECRET": "private"}):
                result = run_isolated(tree, [str(script)], hidden=(original,),
                                      worker_env={"EXPLICIT_TEST_KEY": "dummy-value"})
            self.assertTrue(result.passed, result.output())
            self.assertEqual((tree / "edited").read_text(), "ok")
            self.assertEqual((tree / "child").read_text().strip(), "child")
            self.assertFalse((original / "planted").exists())
            self.assertFalse((host_state / "planted").exists())

    def test_stdin_and_private_home_do_not_persist_between_runs(self):
        with tempfile.TemporaryDirectory() as scratch:
            command = ["/bin/sh", "-c", "test ! -e ~/state && cat > /work/prompt && touch ~/state"]
            for _ in range(2):
                result = run_isolated(scratch, command, worker_env={}, stdin_text="prompt")
                self.assertTrue(result.passed, result.output())
            self.assertEqual((Path(scratch) / "prompt").read_text(), "prompt")

    def test_timeout_removes_worker_descendants(self):
        with tempfile.TemporaryDirectory() as scratch:
            result = run_isolated(scratch, ["/bin/sh", "-c", "(sleep 1; touch /work/late) & sleep 60"],
                                  timeout=0.1, worker_env={})
            self.assertTrue(result.timed_out)
            import time
            time.sleep(1.2)
            self.assertFalse((Path(scratch) / "late").exists())


class TestChangeWorkerPolicy(ChangePathFixture):
    def _run(self, **kwargs):
        return run_instruction("Edit sample", self.root, change=True, allow_untested=True,
                               task_id=self.task_id, confirm=lambda _: True, **kwargs)

    def _assert_report(self, result, text):
        proposal = build_execution_report_proposal(self.root, result)
        self.assertIsNotNone(proposal)
        self.assertTrue(proposal.ok)
        self.assertIn(text, proposal.proposal.content)
        reports = list((self.root / ".ai" / "reports").glob("*.md"))
        self.assertTrue(any(text in p.read_text() for p in reports), "report must actually be persisted")

    def test_unknown_adapter_is_refused_before_it_runs(self):
        adapter = FakeWorkerAdapter()
        result = self._run(adapter=adapter)
        self.assertEqual(result.code, "WORKER_SANDBOX_UNAVAILABLE")
        self.assertEqual(result.status, "REFUSED")
        self.assertEqual(adapter.call_count, 0)
        self.assertIn("adapter", result.message)
        self.assertEqual(result.change_result["worker_isolation"]["status"], "UNAVAILABLE")
        self._assert_report(result, "WORKER_SANDBOX_UNAVAILABLE")

    def test_native_and_opencode_fail_closed_with_missing_prerequisite_in_report(self):
        for worker_type in (NativeCLIWorker, OpenCodeCLIWorker):
            for bwrap in (None, shutil.which("false")):
                with self.subTest(worker=worker_type.__name__, bwrap=bwrap), tempfile.TemporaryDirectory() as scratch:
                    marker = Path(scratch) / "ran"
                    exe = Path(scratch) / "stub"
                    exe.write_text(f"#!/bin/sh\ntouch {marker}\n")
                    exe.chmod(0o755)
                    worker = worker_type(executable=str(exe))
                    reports_before = set((self.root / ".ai" / "reports").glob("*.md"))
                    with patch.object(isolated_run, "find_bwrap", return_value=bwrap):
                        result = self._run(adapter=worker)
                    self.assertEqual(result.code, "WORKER_SANDBOX_UNAVAILABLE")
                    self.assertEqual(result.status, "REFUSED")
                    self.assertIn("bwrap", result.message)
                    self.assertFalse(marker.exists())
                    self._assert_report(result, "WORKER_SANDBOX_UNAVAILABLE")
                    self.assertEqual(len(set((self.root / ".ai" / "reports").glob("*.md")) - reports_before), 1)
                    self.assertEqual(self.list_worktrees(), [str(self.root)])

    @needs_bwrap
    def test_builtin_workers_really_run_in_boundary_and_record_enforcement(self):
        for worker_type in (NativeCLIWorker, OpenCodeCLIWorker):
            with self.subTest(worker=worker_type.__name__), tempfile.TemporaryDirectory() as scratch:
                root = Path(scratch)
                state = root / "state"
                state.mkdir()
                (state / "auth.json").write_text("never exposed")
                exe = root / "stub"
                native = worker_type is NativeCLIWorker
                exe.write_text("#!/usr/bin/python3\n" + f'''
import json, os, pathlib, sys
assert not pathlib.Path({str(self.root)!r}).exists()
assert not pathlib.Path({str(state)!r}).exists()
assert not pathlib.Path('/etc/shadow').exists()
assert os.environ['HOME'] == '/tmp/home'
assert os.environ['CODEX_HOME'] == '/tmp/home/.codex'
assert os.environ['EXPLICIT_TEST_KEY'] == 'dummy-provider-key'
assert 'UNPASSED_KEY' not in os.environ
assert pathlib.Path.cwd() == pathlib.Path('/work')
if {native!r}:
    assert sys.stdin.read(), 'native prompt not delivered'
    assert '--sandbox' in sys.argv and 'workspace-write' in sys.argv
else:
    assert sys.argv[sys.argv.index('--dir') + 1] == '/work'
    assert os.environ['OPENCODE_DISABLE_PROJECT_CONFIG'] == '1'
    assert json.loads(os.environ['OPENCODE_PERMISSION'])['bash'] == 'deny'
pathlib.Path('src/sample.py').write_text('def calculate_total(a, b):\\n    return a + b + {1 if native else 2}\\n')
print(json.dumps({{'type': 'item.completed', 'item': {{'type': 'agent_message', 'text': 'done'}}}}) if {native!r} else
      json.dumps({{'type': 'text', 'part': {{'text': 'done'}}}}))
''')
                exe.chmod(0o755)
                worker = worker_type(executable=str(exe), **({"change_mode": True} if not native else
                                                           {"sandbox": "workspace-write"}))
                with patch.dict(os.environ, {"TABU_WORKER_ENV": "EXPLICIT_TEST_KEY",
                                             "EXPLICIT_TEST_KEY": "dummy-provider-key",
                                             "UNPASSED_KEY": "not-passed",
                                             "CODEX_HOME": str(state)}):
                    result = self._run(adapter=worker)
                self.assertEqual(result.status, "COMPLETED", result.message)
                self.assertEqual(result.change_result["worker_isolation"]["status"], "ENFORCED")
                self._assert_report(result, "Worker isolation: ENFORCED")
                message = subprocess.run(["git", "log", "-1", "--pretty=%B"], cwd=self.root,
                                         capture_output=True, text=True, check=True).stdout
                self.assertIn("Worker-isolation: bubblewrap (network allowed)", message)
                self.assertNotIn("--allow-unconfined-worker", message)

    def test_opt_out_is_recorded_in_warning_report_and_commit(self):
        def mutate(wt):
            (wt / "src" / "sample.py").write_text("def calculate_total(a, b):\n    return b + a\n")
        with patch.object(isolated_run, "find_bwrap", return_value=None):
            result = self._run(adapter=FakeWorkerAdapter(mutate_fn=mutate), allow_unconfined_worker=True)
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.change_result["worker_isolation"]["status"], "DISABLED")
        self.assertTrue(any("--allow-unconfined-worker" in w for w in result.warnings))
        self._assert_report(result, "--allow-unconfined-worker")
        message = subprocess.run(["git", "log", "-1", "--pretty=%B"], cwd=self.root,
                                 capture_output=True, text=True, check=True).stdout
        self.assertIn("Worker-isolation: disabled (--allow-unconfined-worker)", message)

    def test_opt_out_really_uses_unconfined_cli_transports(self):
        for worker_type in (NativeCLIWorker, OpenCodeCLIWorker):
            with self.subTest(worker=worker_type.__name__), tempfile.TemporaryDirectory() as scratch:
                marker = Path(scratch) / "host-write"
                exe = Path(scratch) / "stub"
                exe.write_text("#!/usr/bin/python3\n" + f'''
import json, pathlib
pathlib.Path({str(marker)!r}).write_text('unconfined')
pathlib.Path('src/sample.py').write_text('def calculate_total(a, b):\\n    return a + b + {1 if worker_type is NativeCLIWorker else 2}\\n')
print(json.dumps({{'type': 'agent_message', 'text': 'done'}}))
print(json.dumps({{'type': 'text', 'part': {{'text': 'done'}}}}))
''')
                exe.chmod(0o755)
                with patch.object(isolated_run, "find_bwrap", return_value=None):
                    result = self._run(adapter=worker_type(executable=str(exe)),
                                       allow_unconfined_worker=True)
                self.assertEqual(result.status, "COMPLETED", result.message)
                self.assertEqual(marker.read_text(), "unconfined")
                self.assertEqual(result.change_result["worker_isolation"]["status"], "DISABLED")

    def test_unconfined_no_change_persists_override_after_external_write(self):
        with tempfile.TemporaryDirectory() as scratch:
            marker = Path(scratch) / "host-write"
            exe = Path(scratch) / "stub"
            exe.write_text("#!/usr/bin/python3\n" + f'''
import json, pathlib
pathlib.Path({str(marker)!r}).write_text('unconfined')
print(json.dumps({{'type': 'item.completed', 'item': {{'type': 'agent_message', 'text': 'done'}}}}))
''')
            exe.chmod(0o755)

            result = self._run(adapter=NativeCLIWorker(executable=str(exe)),
                               allow_unconfined_worker=True)
            self.assertEqual(marker.read_text(), "unconfined")

        self.assertEqual(result.code, "NO_CHANGES_PRODUCED")
        self.assertEqual(result.change_result["worker_isolation"]["status"], "DISABLED")
        reports = list((self.root / ".ai" / "reports").glob("RPT-*.md"))
        self.assertTrue(reports, "no-change result must persist an execution report")
        report_text = reports[0].read_text(encoding="utf-8")
        self.assertIn("Worker isolation: DISABLED (--allow-unconfined-worker)", report_text)

    def test_unconfined_worker_failure_persists_override_after_external_write(self):
        with tempfile.TemporaryDirectory() as scratch:
            marker = Path(scratch) / "host-write"
            exe = Path(scratch) / "stub"
            exe.write_text("#!/usr/bin/python3\n" + f'''
import pathlib, sys
pathlib.Path({str(marker)!r}).write_text('unconfined')
print('worker failed', file=sys.stderr)
raise SystemExit(1)
''')
            exe.chmod(0o755)

            result = self._run(adapter=NativeCLIWorker(executable=str(exe)),
                               allow_unconfined_worker=True)
            self.assertEqual(marker.read_text(), "unconfined")

        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.change_result["worker_isolation"]["status"], "DISABLED")
        reports = list((self.root / ".ai" / "reports").glob("RPT-*.md"))
        self.assertTrue(reports, "worker failure must persist an execution report")
        report_text = reports[0].read_text(encoding="utf-8")
        self.assertIn("Worker isolation: DISABLED (--allow-unconfined-worker)", report_text)

    def test_post_worker_result_exception_persists_override_and_continuation(self):
        """A post-worker exception still records the unconfined override."""
        with tempfile.TemporaryDirectory() as scratch:
            marker = Path(scratch) / "host-write"

            def mutate(wt):
                marker.write_text("unconfined", encoding="utf-8")
                (wt / "src" / "sample.py").write_text(
                    "def calculate_total(a, b):\n    return a + b + 1\n",
                    encoding="utf-8",
                )

            worker = FakeWorkerAdapter(mutate_fn=mutate)
            real_start_execution = change_path.start_execution

            def run_worker_then_raise(plan, collector=None):
                real_start_execution(plan, collector)
                raise RuntimeError("post-worker result processing boom")

            persisted = []
            real_persist = change_path._persist_execution_report

            def record_persistence(*args, **kwargs):
                persisted.append(args[1])
                return real_persist(*args, **kwargs)

            with patch.object(change_path, "start_execution", side_effect=run_worker_then_raise), \
                    patch.object(change_path, "_persist_execution_report",
                                 side_effect=record_persistence):
                result = self._run(adapter=worker, allow_unconfined_worker=True)

            self.assertEqual(marker.read_text(encoding="utf-8"), "unconfined")

        self.assertEqual(result.status, "REFUSED")
        self.assertEqual(result.code, "WORKER_UNAVAILABLE")
        self.assertEqual(len(persisted), 1)
        self.assertIn("post-worker result processing boom", result.message)
        self.assertEqual(result.change_result["worker_isolation"]["status"], "DISABLED")
        self.assertTrue(result.change_result["worker_isolation"]["override"])

        proposal = build_execution_report_proposal(self.root, result)
        self.assertIsNotNone(proposal)
        self.assertTrue(proposal.ok)
        self.assertEqual(proposal.proposal.task_id, self.task_id)
        self.assertIn("Worker isolation: DISABLED (--allow-unconfined-worker)",
                      proposal.proposal.content)
        reports = list((self.root / ".ai" / "reports").glob("RPT-*.md"))
        self.assertTrue(reports, "post-worker exception must persist an execution report")
        self.assertIn("Worker isolation: DISABLED (--allow-unconfined-worker)",
                      reports[0].read_text(encoding="utf-8"))

    def test_cli_forwards_opt_out(self):
        with patch("tabularium.cli.run_instruction", side_effect=SystemExit(0)) as run:
            with self.assertRaises(SystemExit):
                main(["run", "Edit sample", str(self.root), "--change", "--allow-unconfined-worker"])
        self.assertTrue(run.call_args.kwargs["allow_unconfined_worker"])
