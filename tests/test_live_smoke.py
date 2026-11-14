"""Opt-in live smoke checks against real providers and installed CLI workers.

Everything else in the suite runs on fixtures and never reaches a provider or a
model. This module is the exception, and it is OFF by default: without
TABULARIUM_LIVE_SMOKE every test here is skipped and reported as NOT VERIFIED,
so the default run stays deterministic and needs no credentials.

Opt in with a comma-separated list of targets (or `all`):

    TABULARIUM_LIVE_SMOKE=api,codex,opencode \
        python -m pytest tests/test_live_smoke.py -v -s -rA

    api       one bounded API transport request through LiteLLM; needs one of
              the provider keys in API_PROVIDER_KEYS
    codex     approved and declined `run --change` with the Codex CLI
              (`codex` on PATH or TABU_NATIVE_EXECUTABLE)
    opencode  approved and declined `run --change` with OpenCode
              (`opencode` on PATH or TABU_OPENCODE_EXECUTABLE; TABU_MODEL
              selects the model, otherwise OpenCode's own default)

A target that is requested but whose prerequisite is missing FAILS with the
missing prerequisite named; it is never silently skipped. Targets that are not
requested are skipped as NOT VERIFIED. These checks make billable calls with
the operator's own credentials; provider output is not printed, only status
codes and the CLI/provider version.
"""
from __future__ import annotations

import os
import re
import subprocess
import unittest

from tests.test_v2_09_change_path import ChangePathFixture

LIVE_ENV = "TABULARIUM_LIVE_SMOKE"
TARGETS = ("api", "codex", "opencode")
API_PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY")
API_MODELS = {
    "OPENAI_API_KEY": "openai/gpt-4o-mini",
    "ANTHROPIC_API_KEY": "anthropic/claude-3-5-haiku-20241022",
    "GEMINI_API_KEY": "gemini/gemini-2.0-flash",
    "GROQ_API_KEY": "groq/llama-3.1-8b-instant",
}
MARKER = "# tabularium-live-smoke"
_VERSION_TEXT = re.compile(r"[^A-Za-z0-9 ._:/()+-]")


def requested_targets() -> set[str]:
    raw = os.environ.get(LIVE_ENV, "").strip().casefold()
    if not raw:
        return set()
    names = {part.strip() for part in raw.split(",") if part.strip()}
    if "all" in names:
        return set(TARGETS)
    unknown = names - set(TARGETS)
    if unknown:
        raise ValueError(f"{LIVE_ENV} names unknown target(s) {sorted(unknown)}; "
                         f"choose from {', '.join(TARGETS)} or 'all'")
    return names


def require_target(test: unittest.TestCase, target: str) -> None:
    """Skip as NOT VERIFIED unless the operator asked for this target."""
    try:
        targets = requested_targets()
    except ValueError as exc:
        test.fail(str(exc))
    if target not in targets:
        raise unittest.SkipTest(
            f"live {target} smoke not requested ({LIVE_ENV} unset or without "
            f"'{target}'): NOT VERIFIED")


def tool_version(executable: str) -> str:
    """First line of `<exe> --version`, restricted to plain characters."""
    try:
        out = subprocess.run([executable, "--version"], capture_output=True, text=True,
                             timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unknown ({type(exc).__name__})"
    lines = (out.stdout or out.stderr).strip().splitlines()
    return _VERSION_TEXT.sub("", lines[0])[:80] if lines else "unknown"


def report(target: str, scenario: str, version: str, outcome: str) -> None:
    print(f"LIVE-SMOKE target={target} scenario={scenario} version={version} "
          f"outcome={outcome}")


class TestLiveAPIProvider(unittest.TestCase):
    """One bounded API transport request through the real provider."""

    def test_bounded_api_request(self):
        require_target(self, "api")
        try:
            import litellm  # noqa: F401
        except ImportError:
            self.fail("live api smoke requested but LiteLLM is not installed; "
                      "install the extra: pip install -e \".[api]\"")
        from tabularium.engine.workers import COMPLETED, WorkerRequest
        from tabularium.engine.workers.api import APIWorker, ModelTarget

        available = [k for k in API_PROVIDER_KEYS if os.environ.get(k)]
        if not available:
            self.fail("live api smoke requested but no provider credential is set; "
                      "set one of: " + ", ".join(API_PROVIDER_KEYS))
        model = API_MODELS[available[0]]
        result = APIWorker(model_target=ModelTarget(
            model, max_output_tokens=16, temperature=0.0)).execute(WorkerRequest(
                request_id="live-smoke-api",
                intent="Return exactly the text API_WORKER_OK and nothing else. "
                       "Make no changes.",
                payload={"required": {"task": {"id": "TASK-2026-0001"}}},
                task_id="TASK-2026-0001"))
        version = f"{model} litellm-{_dist_version('litellm')}"
        report("api", "single-request", version, result.status)
        self.assertEqual(result.status, COMPLETED, result.diagnostics)
        self.assertIn("API_WORKER_OK", result.output or "")


def _dist_version(name: str) -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


class _LiveChangeLifecycle:
    """Approved and declined `run --change` against one real writable CLI."""

    target = ""
    worker = ""

    def find_executable(self) -> str | None:  # pragma: no cover - overridden
        raise NotImplementedError

    def setUp(self):
        require_target(self, self.target)
        self.executable = self.find_executable()
        if not self.executable:
            self.fail(f"live {self.target} smoke requested but the {self.target} "
                      "executable was not found (PATH or the configured override)")
        super().setUp()
        self.version = tool_version(self.executable)

    def _run(self, approve: bool):
        from tabularium.execution import run_instruction
        return run_instruction(
            f"Append exactly one line `{MARKER}` to the end of src/sample.py. "
            "Change no other file and nothing else in that file.",
            self.root, worker=self.worker, change=True, confirm=lambda _: approve)

    def test_change_approved_is_applied(self):
        result = self._run(approve=True)
        report(self.target, "change-approved", self.version, result.code)
        self.assertEqual(result.code, "COMPLETED", result.message)
        self.assertIn(MARKER, self.src_file.read_text(encoding="utf-8"))

    def test_change_declined_leaves_project_untouched(self):
        result = self._run(approve=False)
        report(self.target, "change-declined", self.version, result.code)
        self.assertEqual(result.code, "APPROVAL_REJECTED", result.message)
        self.assertEqual(self.src_file.read_text(encoding="utf-8"), self.initial_content)


class TestLiveCodexChange(_LiveChangeLifecycle, ChangePathFixture):
    target = "codex"
    worker = "native"

    def find_executable(self):
        from tabularium.engine.workers.cli.worker import find_native_executable
        return find_native_executable()


class TestLiveOpenCodeChange(_LiveChangeLifecycle, ChangePathFixture):
    target = "opencode"
    worker = "opencode"

    def find_executable(self):
        from tabularium.engine.workers.cli.opencode_worker import find_opencode_executable
        return find_opencode_executable()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
