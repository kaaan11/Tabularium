"""Offline subprocess fixtures: no installed OpenCode or model requests."""

import json
from dataclasses import asdict
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tabularium.engine.projection.model import ProjectedPayload
from tabularium.engine.workers import WorkerRequest
from tabularium.engine.workers.cli.bounded_process import BoundedProcess
from tabularium.engine.workers.cli.opencode_worker import OpenCodeCLIWorker


@unittest.skipIf(sys.platform == "win32", "POSIX executable/process-group fixtures")
class TestOpenCodeBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.exe = self.root / "opencode"
        self.request = WorkerRequest(request_id="offline", intent="inspect",
                                     payload=ProjectedPayload(sections=(("required", {"intent": "test"}),)))

    def worker(self, code, **kwargs):
        self.exe.write_text(f"#!{sys.executable}\n" + code, encoding="utf-8")
        self.exe.chmod(0o700)
        return OpenCodeCLIWorker(executable=str(self.exe), cwd=self.root, **kwargs)

    def test_environment_allowlist(self):
        worker = self.worker('import os, json\nprint(json.dumps({"type":"text","part":{"text":json.dumps(dict(os.environ))}}))\n')
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-parent-key", "UNRELATED_PRIVATE": "hidden", "PYTHONPATH": "hidden"}):
            result = worker.execute(self.request)
        self.assertEqual(result.status, "COMPLETED")
        env = json.loads(result.output)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("UNRELATED_PRIVATE", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertIn("HOME", env)

    def test_output_and_stream_error_redacted_before_result(self):
        marker = "arbitrary-parent-credential-93871"
        events = [dict(type="text", part=dict(text="sk-syntheticcredential123456 ")),
                  dict(type="text", part=dict(text=marker)),
                  dict(type="error", message="token=synthetic-token-56789"),
                  dict(type="step_finish", part=dict(tokens=dict(input=marker, output=3, total=4)))]
        worker = self.worker("import json\n" + "\n".join(f"print({json.dumps(json.dumps(e))})" for e in events))
        with patch.dict(os.environ, {"TEST_SECRET": marker}):
            result = worker.execute(self.request)
        serialized = json.dumps(asdict(result))
        for secret in (marker, "sk-syntheticcredential123456", "synthetic-token-56789"):
            self.assertNotIn(secret, serialized)
        self.assertIn("[REDACTED]", result.output)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.usage["input_tokens"], 0)

    def test_stderr_and_spawn_exception_do_not_leak(self):
        worker = self.worker('import sys\nprint("password=synthetic-stderr-56789", file=sys.stderr)\nsys.exit(2)\n')
        result = worker.execute(self.request)
        self.assertNotIn("synthetic-stderr", json.dumps(asdict(result)))
        self.assertEqual(result.status, "FAILED")
        with patch("tabularium.engine.workers.cli.opencode_worker.BoundedProcess.run", side_effect=RuntimeError("password=synthetic-error")):
            result = worker.execute(self.request)
        self.assertNotIn("synthetic-error", json.dumps(asdict(result)))

    def test_stdout_and_stderr_caps_without_newlines(self):
        for fd in (1, 2):
            with self.subTest(fd=fd):
                worker = self.worker(f'import os\nwhile True: os.write({fd}, b"x" * 8192)\n', max_output_bytes=4096, timeout_s=3)
                started = time.monotonic()
                result = worker.execute(self.request)
                self.assertLess(time.monotonic() - started, 3)
                self.assertEqual(result.status, "FAILED")
                self.assertEqual(result.output, "")
                self.assertEqual(result.diagnostics[0]["code"], "OUTPUT_LIMIT")

    def test_timeout_reaps_parent_and_descendant_even_after_parent_exit(self):
        for parent_exits in (False, True):
            with self.subTest(parent_exits=parent_exits):
                pid_file = self.root / "child.pid"
                worker = self.worker(
                    'import subprocess, sys, time\n'
                    'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
                    f'open({str(pid_file)!r}, "w").write(str(p.pid))\n'
                    + ('sys.exit(0)\n' if parent_exits else 'time.sleep(30)\n'), timeout_s=0.25)
                result = worker.execute(self.request)
                self.assertEqual(result.diagnostics[0]["code"], "TIMEOUT")
                child = int(pid_file.read_text())
                # A terminated orphan may briefly remain a zombie until PID 1
                # reaps it; it must no longer be executing.
                stat = Path(f"/proc/{child}/stat")
                deadline = time.monotonic() + 2
                while stat.exists() and stat.read_text().split()[2] != "Z" and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(not stat.exists() or stat.read_text().split()[2] == "Z")

    def test_cancel_terminates_and_reaps_process(self):
        process = BoundedProcess([sys.executable, "-c", "import time; time.sleep(30)"], str(self.root), 5, 4096)
        results = []
        thread = threading.Thread(target=lambda: results.append(process.run()))
        thread.start()
        deadline = time.monotonic() + 2
        while process._process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        child = process._process
        process.cancel()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].outcome, "CANCELLED")
        self.assertIsNotNone(child.poll())

    def test_escaped_descendant_cannot_hold_capture_open(self):
        pid_file = self.root / "escaped.pid"
        worker = self.worker(
            'import subprocess, sys\n'
            'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)\n'
            f'open({str(pid_file)!r}, "w").write(str(p.pid))\n', timeout_s=0.25)
        started = time.monotonic()
        try:
            result = worker.execute(self.request)
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(result.diagnostics[0]["code"], "TIMEOUT")
            self.assertIn("capture incomplete", result.diagnostics[0]["detail"])
            self.assertEqual(result.output, "")
        finally:
            # Deliberate escaped fixture remains our responsibility. The
            # adapter makes no OS containment or escaped-family guarantee.
            if pid_file.exists():
                os.kill(int(pid_file.read_text()), 9)

    def test_capture_read_error_is_failed_and_reaped(self):
        process = BoundedProcess([sys.executable, "-c", "print('fixture')"], str(self.root), 2, 4096)
        real_read = os.read
        def failing_read(fd, size):
            # Popen itself uses os.read for its exec-error pipe. Fail only
            # when the capture process is already established.
            if process._process is not None:
                raise OSError("injected capture read error")
            return real_read(fd, size)
        with patch("tabularium.engine.workers.cli.bounded_process.os.read", side_effect=failing_read):
            captured = process.run()
        self.assertEqual(captured.outcome, "CAPTURE_FAILED")
        self.assertFalse(captured.capture_complete)
        self.assertIsNotNone(captured.returncode)

    def test_windows_is_explicitly_refused_before_spawn(self):
        with patch("tabularium.engine.workers.cli.bounded_process.sys.platform", "win32"), patch("subprocess.Popen") as spawn:
            result = BoundedProcess(["unused"], str(self.root), 1, 4096).run()
        self.assertEqual(result.outcome, "UNSUPPORTED_PLATFORM")
        spawn.assert_not_called()

    def test_invalid_bounds_refused(self):
        for kwargs in ({"timeout_s": 0}, {"timeout_s": float("nan")}, {"max_output_bytes": 0}):
            with self.assertRaises(ValueError):
                OpenCodeCLIWorker(**kwargs)
