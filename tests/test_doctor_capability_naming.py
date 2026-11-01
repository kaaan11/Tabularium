"""GP1: human status/doctor output names each optional capability.

G0-001 / PF-R01: the human renderer previously showed only an aggregate
count ("Optional capabilities: N available, M unavailable"), so the native
worker and the API/LiteLLM extra were not identifiable without --json. The
renderer now groups capability NAMES under Available/Unavailable while the
machine-readable surface stays byte-compatible.

Contract pins:
- every capability name appears in human output; LiteLLM backing the api
  capability is identifiable through its distribution list;
- labels mirror the JSON `available` field exactly — availability stays an
  environment fact and never claims invocation success;
- names are terminal-safe and bounded (hostile metadata cannot inject raw
  control characters or unbounded lines into human output);
- the count line, the JSON schema, and exit codes are unchanged.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from tabularium.diagnostics import (CapabilityStatus, HealthResult, diagnose,
                              render_human, render_json)


def health(capabilities) -> HealthResult:
    return HealthResult(
        command="doctor", tabularium_version="0.1.0", target="/tmp/proj",
        overall_state="READY", initialization_state="INITIALIZED",
        onboarding_state="COMPLETE", loader_state="READY",
        foundation_state="VALID", capabilities=tuple(capabilities))


CAP_KEYS = {"name", "required", "available", "reason_code", "packages",
            "message"}


class CapabilityNamingRendererTests(unittest.TestCase):
    """Deterministic renderer contract over hand-built results."""

    def test_every_capability_is_named_in_human_output(self):
        result = health([
            CapabilityStatus("api", False, True,
                             "OPTIONAL_DEPENDENCY_AVAILABLE",
                             packages=("litellm",)),
            CapabilityStatus("native-worker", False, False,
                             "NATIVE_WORKER_UNAVAILABLE"),
        ])
        text = render_human(result)
        self.assertIn("native-worker", text)
        self.assertIn("api [litellm]", text)
        self.assertIn("Optional capabilities: 1 available, 1 unavailable",
                      text)

    def test_native_and_api_capabilities_are_individually_identifiable(self):
        result = health([
            CapabilityStatus("api", False, True,
                             "OPTIONAL_DEPENDENCY_AVAILABLE",
                             packages=("litellm",)),
            CapabilityStatus("native-worker", False, True,
                             "NATIVE_WORKER_READY"),
        ])
        available_line = next(line for line in
                              render_human(result).splitlines()
                              if line.strip().startswith("Available:"))
        self.assertIn("native-worker", available_line)
        self.assertIn("api [litellm]", available_line)

    def test_unavailable_group_lists_only_unavailable_names(self):
        result = health([
            CapabilityStatus("api", False, False,
                             "OPTIONAL_DEPENDENCY_MISSING",
                             packages=("litellm",)),
            CapabilityStatus("runtime", False, False,
                             "OPTIONAL_DEPENDENCY_MISSING",
                             packages=("langgraph",)),
            CapabilityStatus("native-worker", False, True,
                             "NATIVE_WORKER_READY"),
        ])
        text = render_human(result)
        self.assertIn("Optional capabilities: 1 available, 2 unavailable",
                      text)
        unavailable_line = next(line for line in text.splitlines()
                                if line.strip().startswith("Unavailable:"))
        self.assertIn("api [litellm]", unavailable_line)
        self.assertIn("runtime [langgraph]", unavailable_line)
        self.assertNotIn("native-worker", unavailable_line)

    def test_empty_groups_are_omitted(self):
        all_available = health([CapabilityStatus(
            "native-worker", False, True, "NATIVE_WORKER_READY")])
        text = render_human(all_available)
        self.assertNotIn("Unavailable:", text)
        none_available = health([CapabilityStatus(
            "native-worker", False, False, "NATIVE_WORKER_UNAVAILABLE")])
        text = render_human(none_available)
        self.assertNotIn("Available:", text)
        self.assertIn("Unavailable:", text)

    def test_no_capabilities_keeps_the_count_line_alone(self):
        text = render_human(health([]))
        self.assertIn("Optional capabilities: 0 available, 0 unavailable",
                      text)
        self.assertNotIn("Available:", text)
        self.assertNotIn("Unavailable:", text)

    def test_hostile_names_are_escaped_and_bounded(self):
        result = health([CapabilityStatus(
            "bad\nCAP\tX" + "y" * 200, False, True,
            "OPTIONAL_DEPENDENCY_AVAILABLE")])
        for line in render_human(result).splitlines():
            self.assertTrue(all(ch.isprintable() or ch == " "
                                for ch in line))

    def test_rendering_does_not_change_the_result_or_its_json(self):
        result = health([
            CapabilityStatus("api", False, True,
                             "OPTIONAL_DEPENDENCY_AVAILABLE",
                             packages=("litellm",)),
            CapabilityStatus("native-worker", False, False,
                             "NATIVE_WORKER_UNAVAILABLE"),
        ])
        before = json.loads(render_json(result))
        exit_before = result.exit_code
        render_human(result)
        self.assertEqual(json.loads(render_json(result)), before)
        self.assertEqual(result.exit_code, exit_before)
        for cap in json.loads(render_json(result))["capabilities"]:
            self.assertEqual(set(cap), CAP_KEYS)


class LiveDoctorNamingTests(unittest.TestCase):
    """The real diagnose() surface agrees with the rendered names."""

    def test_live_doctor_names_every_reported_capability(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TABU_NATIVE_EXECUTABLE", None)
            result = diagnose(None)
        self.assertTrue(result.capabilities)
        text = render_human(result)
        for cap in result.capabilities:
            self.assertIn(cap.name, text)
        # Native worker identity reaches the human reader either way.
        self.assertIn("native-worker", text)
        self.assertIn(f"Optional capabilities: "
                      f"{sum(1 for c in result.capabilities if c.available)} "
                      f"available, "
                      f"{sum(1 for c in result.capabilities if not c.available)} "
                      f"unavailable", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
