"""F4: doctor reports native-worker readiness on the capability model.

The default Native worker previously had no doctor visibility while the
LiteLLM extra did. Readiness uses the SAME CapabilityStatus semantics as the
optional extras: it is an environment FACT (executable findable), never a
claim of authorization, configuration, or invocation success. No raw path or
OSError text is ever exposed.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tabularium.diagnostics import (NATIVE_WORKER_READY,
                              NATIVE_WORKER_UNAVAILABLE, diagnose)
from tabularium.engine.workers.cli import find_native_executable

CAP_NAME = "native-worker"


def native_cap(result) -> dict:
    matches = [cap for cap in result.capabilities if cap.name == CAP_NAME]
    assert len(matches) == 1, f"expected exactly one {CAP_NAME} capability"
    return matches[0].to_dict()


class NativeReadinessUnitTests(unittest.TestCase):
    def test_not_configured_and_absent_reports_unavailable(self):
        with patch.dict(os.environ, {}, clear=False), \
                patch("tabularium.diagnostics.find_native_executable",
                      return_value=None):
            os.environ.pop("TABU_NATIVE_EXECUTABLE", None)
            cap = native_cap(diagnose(None))
        self.assertFalse(cap["available"])
        self.assertEqual(cap["reason_code"], NATIVE_WORKER_UNAVAILABLE)
        self.assertIn("TABU_NATIVE_EXECUTABLE", cap["message"])
        self.assertEqual(cap["packages"], [])
        self.assertFalse(cap["required"])

    def test_found_on_path_reports_ready_without_claiming_invocation(self):
        with patch.dict(os.environ, {}, clear=False), \
                patch("tabularium.diagnostics.find_native_executable",
                      return_value="/usr/local/bin/codex"):
            os.environ.pop("TABU_NATIVE_EXECUTABLE", None)
            cap = native_cap(diagnose(None))
        self.assertTrue(cap["available"])
        self.assertEqual(cap["reason_code"], NATIVE_WORKER_READY)
        self.assertIn("PATH", cap["message"])
        self.assertNotIn("/usr/local/bin/codex", cap["message"])
        self.assertIn("authorization", cap["message"])

    def test_valid_override_reports_ready_and_names_the_channel(self):
        with tempfile.TemporaryDirectory(prefix="tabu-f4-") as scratch:
            executable = Path(scratch) / "my-codex"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(executable.stat().st_mode | stat.S_IEXEC)
            env = dict(os.environ, TABU_NATIVE_EXECUTABLE=str(executable))
            with patch.dict(os.environ, env, clear=True), \
                    patch("tabularium.diagnostics.find_native_executable",
                          return_value=str(executable)):
                cap = native_cap(diagnose(None))
        self.assertTrue(cap["available"])
        self.assertEqual(cap["reason_code"], NATIVE_WORKER_READY)
        self.assertIn("TABU_NATIVE_EXECUTABLE", cap["message"])

    def test_invalid_override_is_distinguished_from_path_absence(self):
        env = dict(os.environ,
                   TABU_NATIVE_EXECUTABLE="/nonexistent/tabu-f4/codex")
        with patch.dict(os.environ, env, clear=True), \
                patch("tabularium.diagnostics.find_native_executable",
                      return_value=None):
            cap = native_cap(diagnose(None))
        self.assertFalse(cap["available"])
        self.assertEqual(cap["reason_code"], NATIVE_WORKER_UNAVAILABLE)
        self.assertIn("does not resolve to an executable file",
                      cap["message"])
        self.assertNotIn("/nonexistent/tabu-f4/codex", cap["message"])

    def test_directory_override_is_not_reported_ready(self):
        """M7 pin: only an executable FILE satisfies the override.

        A directory exists but is not an executable file; doctor must keep
        reporting the native worker unavailable, exactly as actual worker
        selection refuses it. Weakening the readiness check from is_file()
        to exists() would report READY here while selection returns None.
        """
        with tempfile.TemporaryDirectory(prefix="tabu-f4-dir-") as scratch:
            directory = Path(scratch) / "codex-dir"
            directory.mkdir()
            env = dict(os.environ, TABU_NATIVE_EXECUTABLE=str(directory))
            with patch.dict(os.environ, env, clear=True):
                cap = native_cap(diagnose(None))
                selected = find_native_executable()
        self.assertFalse(cap["available"])
        self.assertEqual(cap["reason_code"], NATIVE_WORKER_UNAVAILABLE)
        # Readiness mirrors selection: neither side accepts a directory.
        self.assertIsNone(selected)

    def test_capability_shape_matches_the_optional_extras_contract(self):
        with patch.dict(os.environ, {}, clear=False), \
                patch("tabularium.diagnostics.find_native_executable",
                      return_value=None):
            os.environ.pop("TABU_NATIVE_EXECUTABLE", None)
            result = diagnose(None)
        for cap in result.capabilities:
            self.assertEqual(
                set(cap.to_dict()),
                {"name", "required", "available", "reason_code", "packages",
                 "message"})
        names = [cap.name for cap in result.capabilities]
        self.assertEqual(names, sorted(names))


class NativeReadinessPathSmokeTests(unittest.TestCase):
    """Real subprocess smoke: PATH-level discovery, no mocks."""

    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-f4-bin-")
        self.bin_dir = Path(self.holder.name)
        self.repo = Path(__file__).resolve().parents[1]

    def tearDown(self):
        self.holder.cleanup()

    def run_doctor_json(self, path_value: str) -> dict:
        env = dict(os.environ, PATH=path_value,
                   PYTHONPATH=str(self.repo),
                   TABU_NATIVE_EXECUTABLE="")
        with tempfile.TemporaryDirectory(prefix="tabu-f4-target-") as target:
            proc = subprocess.run(
                [sys.executable, "-m", "tabularium.cli", "doctor", "--json", target],
                capture_output=True, text=True, env=env, cwd=self.repo,
                check=False)
        return json.loads(proc.stdout)

    def test_real_codex_on_path_is_reported_ready(self):
        script = self.bin_dir / "codex"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        doc = self.run_doctor_json(str(self.bin_dir))
        caps = {c["name"]: c for c in doc["capabilities"]}
        self.assertEqual(caps[CAP_NAME]["reason_code"], NATIVE_WORKER_READY)
        self.assertTrue(caps[CAP_NAME]["available"])

    def test_empty_path_is_reported_unavailable(self):
        doc = self.run_doctor_json(str(self.bin_dir))
        caps = {c["name"]: c for c in doc["capabilities"]}
        self.assertEqual(caps[CAP_NAME]["reason_code"],
                         NATIVE_WORKER_UNAVAILABLE)
        self.assertFalse(caps[CAP_NAME]["available"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
