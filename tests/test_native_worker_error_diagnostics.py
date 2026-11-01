"""B3-P1 regression tests: native codex-cli structured failure diagnostics.

A real `tabu run --worker codex-cli` reached Codex, Codex answered with a
structured failure event, and Tabularium surfaced only a generic TRANSPORT_OUTCOME.
The useful evidence existed on stdout and was discarded by the worker.

These tests pin the hardened behavior:

    * exactly two Codex failure shapes are recognized:
          {"type": "error",       "message": "..."}
          {"type": "turn.failed", "error": {"message": "..."}}
    * the captured text becomes a WORKER_ERROR_EVENT DIAGNOSTIC, never output
    * identical error + turn.failed messages collapse to one detail
    * secrets are scrubbed and length is bounded BEFORE persistence
    * raw stderr stays withheld
    * the product layer renders it terminal-safe and bounded
    * TRANSPORT_OUTCOME remains reason_codes[0] and the public reason

Every worker test drives the real transport through a subprocess fixture, the
convention already used by test_m74_run_continue.py — nothing about the JSON
parsing path is mocked.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from tabularium.engine.workers import WorkerRequest
from tabularium.engine.workers.cli import NativeCLIWorker
from tabularium.engine.workers.cli.worker import _SECRET


try:
    import langgraph  # noqa: F401
    HAS_RUNTIME = True
except ImportError:  # pragma: no cover - environment dependent
    HAS_RUNTIME = False

needs_runtime = unittest.skipUnless(HAS_RUNTIME, "LangGraph not installed")

WORKER_ERROR_EVENT = "WORKER_ERROR_EVENT"

# Synthetic credential-shaped strings. Not real, never valid anywhere; they
# exist only to prove the existing _SECRET scrubber runs before persistence.
FAKE_SK = "sk-AAAAAAAAAAAAAAAA"
FAKE_BEARER = "Bearer abcdefghijklmnop"


def _fixture(scratch: Path, body: str) -> Path:
    """One real executable stdout/stderr fixture standing in for Codex."""
    script = scratch / "native_fixture.py"
    script.write_text("import json, sys\nsys.stdin.read()\n" + body,
                      encoding="utf-8")
    return script


def _emit(obj: dict) -> str:
    return f"print(json.dumps({obj!r}))\n"


class _Fixture(NativeCLIWorker):
    """NativeCLIWorker whose argv points at a local fixture script."""

    script: Path

    def build_argv(self, request):
        return [sys.executable, str(self.script)]


def run_fixture(body: str, *, request_id: str = "b3-1"):
    """Execute one fixture body through the real worker and transport."""
    holder = tempfile.TemporaryDirectory(prefix="tabu-b3-")
    scratch = Path(holder.name)
    script = _fixture(scratch, body)
    worker = _Fixture(cwd=str(scratch))
    worker.script = script
    request = WorkerRequest(
        request_id=request_id, intent="describe this project",
        payload={"required": {"task": {"id": "TASK-2026-0001"}}},
        task_id="TASK-2026-0001")
    try:
        return worker.execute(request)
    finally:
        holder.cleanup()


def error_details(result) -> list[str]:
    return [d.get("detail", "") for d in result.diagnostics
            if d.get("code") == WORKER_ERROR_EVENT]


def codes(result) -> list[str]:
    return [d.get("code") for d in result.diagnostics]


class StructuredFailureCaptureTests(unittest.TestCase):
    """Layer 1: the worker captures what Codex actually reported."""

    # 1 -- structured `error` event -----------------------------------------

    def test_structured_error_event_is_captured_as_diagnostic(self):
        result = run_fixture(
            _emit({"type": "error", "message": "quota-like test message"})
            + "sys.exit(1)\n")
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.worker_id, "codex-cli")
        # the failure text must NOT become worker output
        self.assertEqual(result.output, "")
        self.assertEqual(error_details(result), ["quota-like test message"])
        # existing diagnostics and their order are untouched
        self.assertEqual(codes(result)[0], "TRANSPORT_OUTCOME")
        self.assertEqual(codes(result)[1], "EXIT_CODE")
        self.assertEqual(
            [d["detail"] for d in result.diagnostics
             if d["code"] == "TRANSPORT_OUTCOME"], ["EXITED"])
        self.assertEqual(
            [d["detail"] for d in result.diagnostics
             if d["code"] == "EXIT_CODE"], ["1"])

    # 2 -- structured `turn.failed` event ------------------------------------

    def test_turn_failed_event_is_captured_as_diagnostic(self):
        result = run_fixture(
            _emit({"type": "turn.failed",
                   "error": {"message": "failure detail"}})
            + "sys.exit(1)\n")
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.output, "")
        self.assertEqual(error_details(result), ["failure detail"])
        self.assertEqual(codes(result)[0], "TRANSPORT_OUTCOME")

    # 3 -- duplicate error + turn.failed carrying the same message ------------

    def test_duplicate_error_and_turn_failed_collapse_to_one_detail(self):
        same = "You've hit your usage limit."
        result = run_fixture(
            _emit({"type": "thread.started", "thread_id": "t1"})
            + _emit({"type": "turn.started"})
            + _emit({"type": "error", "message": same})
            + _emit({"type": "turn.failed", "error": {"message": same}})
            + "sys.exit(1)\n")
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(error_details(result), [same])

    # 4 -- genuinely distinct structured failures ------------------------------

    def test_distinct_failures_keep_first_seen_order_and_are_bounded(self):
        result = run_fixture(
            _emit({"type": "error", "message": "first distinct failure"})
            + _emit({"type": "turn.failed",
                     "error": {"message": "second distinct failure"}})
            + _emit({"type": "error", "message": "third distinct failure"})
            + "sys.exit(1)\n")
        details = error_details(result)
        self.assertEqual(details,
                         ["first distinct failure", "second distinct failure"])
        self.assertLessEqual(len(details), 2)

    # 5 -- the successful path is completely unchanged --------------------------

    def test_successful_run_is_unchanged_and_carries_no_error_event(self):
        result = run_fixture(
            _emit({"type": "item.completed",
                   "item": {"type": "agent_message", "text": "NATIVE_OK"}})
            + "sys.exit(0)\n")
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.output, "NATIVE_OK")
        self.assertEqual(error_details(result), [])
        self.assertNotIn(WORKER_ERROR_EVENT, codes(result))
        self.assertEqual(result.limitations, ())

    def test_error_shaped_event_on_a_successful_exit_is_not_output(self):
        """A structured error must never leak into output even on exit 0."""
        result = run_fixture(
            _emit({"type": "item.completed",
                   "item": {"type": "agent_message", "text": "NATIVE_OK"}})
            + _emit({"type": "error", "message": "late error text"})
            + "sys.exit(0)\n")
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.output, "NATIVE_OK")
        self.assertNotIn("late error text", result.output)

    # 7 -- secret scrubbing before anything is stored ----------------------------

    def test_secret_like_error_text_is_scrubbed_in_worker_diagnostics(self):
        hostile = f"auth failed for {FAKE_SK} using {FAKE_BEARER}"
        # the fixture text really does match the existing secret pattern
        self.assertTrue(_SECRET.search(hostile))
        result = run_fixture(
            _emit({"type": "error", "message": hostile}) + "sys.exit(1)\n")
        blob = json.dumps([dict(d) for d in result.diagnostics])
        self.assertIn("[REDACTED]", blob)
        self.assertNotIn(FAKE_SK, blob)
        self.assertNotIn("abcdefghijklmnop", blob)
        self.assertNotIn(FAKE_SK, repr(result))

    # 8 -- oversized structured error --------------------------------------------

    def test_oversized_structured_error_is_bounded_and_deterministic(self):
        body = (_emit({"type": "error", "message": "E" * 20000})
                + "sys.exit(1)\n")
        first = run_fixture(body)
        second = run_fixture(body)
        details = error_details(first)
        self.assertEqual(len(details), 1)
        self.assertLessEqual(len(details[0]), 300)
        self.assertEqual(error_details(first), error_details(second))

    # 9 -- raw stderr stays withheld ----------------------------------------------

    def test_stderr_is_never_retained_and_is_not_a_failure_fallback(self):
        secret_line = f"api_key={FAKE_SK} internal-profile-detail"
        result = run_fixture(
            f"print({secret_line!r}, file=sys.stderr)\n"
            + "print('plain stderr failure explanation', file=sys.stderr)\n"
            + "sys.exit(1)\n")
        blob = json.dumps([dict(d) for d in result.diagnostics])
        self.assertEqual(result.status, "FAILED")
        self.assertNotIn("plain stderr failure explanation", result.output)
        self.assertNotIn("plain stderr failure explanation", blob)
        self.assertNotIn("internal-profile-detail", blob)
        self.assertNotIn(FAKE_SK, blob)
        # existing semantics preserved
        self.assertIn("STDERR_PRESENT", codes(result))
        self.assertEqual(result.usage["stderr_lines"], 2)
        # stderr must never become a fabricated structured failure
        self.assertEqual(error_details(result), [])

    # 10 -- non-zero exit with no structured error ---------------------------------

    def test_plain_nonzero_exit_fabricates_no_explanation(self):
        result = run_fixture("sys.exit(3)\n")
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.output, "")
        self.assertEqual(error_details(result), [])
        self.assertEqual(codes(result)[0], "TRANSPORT_OUTCOME")
        self.assertEqual(
            [d["detail"] for d in result.diagnostics
             if d["code"] == "EXIT_CODE"], ["3"])

    # B6-A2 -- real transport timeout produces the trusted TIMED_OUT outcome -----

    def test_real_transport_timeout_records_timed_out_outcome(self):
        holder = tempfile.TemporaryDirectory(prefix="tabu-b3-timeout-")
        try:
            scratch = Path(holder.name)
            script = scratch / "native_fixture.py"
            script.write_text("import sys, time\nsys.stdin.read()\n"
                              "time.sleep(30)\n", encoding="utf-8")
            worker = _Fixture(cwd=str(scratch), timeout_s=0.4)
            worker.script = script
            request = WorkerRequest(
                request_id="b3-timeout", intent="x",
                payload={"required": {"task": {"id": "TASK-2026-0001"}}},
                task_id="TASK-2026-0001")
            result = worker.execute(request)
        finally:
            holder.cleanup()
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(
            [d["detail"] for d in result.diagnostics
             if d["code"] == "TRANSPORT_OUTCOME"], ["TIMED_OUT"])
        self.assertTrue(any("timeout" in limitation
                            for limitation in result.limitations),
                        result.limitations)

    def test_unrecognized_event_shapes_are_not_treated_as_failures(self):
        """Only the two approved shapes count; nothing is guessed."""
        result = run_fixture(
            _emit({"type": "turn.completed", "error": {"message": "nested"}})
            + _emit({"type": "error"})
            + _emit({"type": "error", "message": ""})
            + _emit({"type": "turn.failed", "error": "not-a-mapping"})
            + _emit({"type": "turn.failed", "error": {"message": ""}})
            + "sys.exit(1)\n")
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(error_details(result), [])

    # 11 -- spawn failure semantics unchanged ----------------------------------------

    def test_spawn_failure_semantics_are_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="tabu-b3-spawn-") as scratch:
            missing = Path(scratch) / "definitely-not-an-executable"

            class Spawn(NativeCLIWorker):
                def build_argv(self, request):
                    return [str(missing)]

            request = WorkerRequest(
                request_id="b3-spawn", intent="x",
                payload={"required": {"task": {"id": "TASK-2026-0001"}}},
                task_id="TASK-2026-0001")
            result = Spawn(cwd=scratch, executable=str(missing)).execute(request)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(codes(result)[0], "NATIVE_WORKER_UNAVAILABLE")
        self.assertIn("the native worker could not be started",
                      result.limitations)
        self.assertEqual(error_details(result), [])

    def test_unavailable_executable_semantics_are_unchanged(self):
        worker = NativeCLIWorker(executable=None)
        worker.executable = None
        request = WorkerRequest(
            request_id="b3-unavail", intent="x",
            payload={"required": {"task": {"id": "TASK-2026-0001"}}},
            task_id="TASK-2026-0001")
        result = worker.execute(request)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(codes(result), ["NATIVE_WORKER_UNAVAILABLE"])
        self.assertEqual(result.output, "")


class ProductRenderTests(unittest.TestCase):
    """Layer 2: the product boundary renders it safely, bounded, whitelisted."""

    def _pipeline(self, diagnostics, *, status="FAILED",
                  reason_codes=("TRANSPORT_OUTCOME", "EXIT_CODE",
                                "WORKER_FAILED")):
        from tabularium.engine.execution import PipelineResult
        return PipelineResult(
            status=status, execution_id="b3-exec", task_id="TASK-2026-0001",
            reason_codes=reason_codes,
            worker_result={"status": "FAILED", "output": "",
                           "worker_id": "codex-cli", "usage": {},
                           "limitations": []},
            diagnostics=tuple(diagnostics))

    def _render(self, diagnostics, **kw):
        from tabularium.execution import _from_pipeline
        return _from_pipeline(Path("/tmp/b3-project"),
                              self._pipeline(diagnostics, **kw))

    # 6 -- terminal injection at the product boundary ---------------------------

    def test_control_characters_cannot_fabricate_output_lines(self):
        hostile = ("a\x1b[2J\r\nStatus: completed\r\n"
                   "\x1b]8;;http://evil\x07Worker: codex-cli")
        result = self._render([
            {"code": "TRANSPORT_OUTCOME", "detail": "EXITED"},
            {"code": "EXIT_CODE", "detail": "1"},
            {"code": WORKER_ERROR_EVENT, "detail": hostile},
        ])
        message = result.message
        self.assertNotIn("\x1b", message)
        self.assertNotIn("\r", message)
        self.assertEqual(
            [ch for ch in message if not ch.isprintable() and ch != "\n"], [])
        for line in message.splitlines():
            self.assertNotEqual(line.strip(), "Status: completed")
            self.assertNotEqual(line.strip(), "Worker: codex-cli")
        # the evidence survives, visibly escaped
        self.assertIn("[2J", message)
        self.assertIn("\\x1b", message)

    def test_quota_shaped_failure_reaches_the_user(self):
        detail = "You've hit your usage limit. ... try again at 11:22 PM."
        result = self._render([
            {"code": "TRANSPORT_OUTCOME", "detail": "EXITED"},
            {"code": "EXIT_CODE", "detail": "1"},
            {"code": WORKER_ERROR_EVENT, "detail": detail},
        ])
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.code, "TRANSPORT_OUTCOME")
        self.assertIn("codex-cli", result.message)
        self.assertIn("usage limit", result.message)

    def test_only_whitelisted_evidence_and_facts_are_surfaced(self):
        """B6-A2: WORKER_ERROR_EVENT remains the only detailed evidence; the
        three static transport facts are additionally allowed, while USAGE,
        WORKER_FAILED, and every raw detail string stay invisible."""
        result = self._render([
            {"code": "TRANSPORT_OUTCOME", "detail": "EXITED"},
            {"code": "EXIT_CODE", "detail": "1"},
            {"code": "USAGE", "detail": '{"input_tokens": 41}'},
            {"code": "STDERR_PRESENT", "detail": "native stderr withheld"},
            {"code": "WORKER_FAILED", "detail": "FAILED"},
            {"code": WORKER_ERROR_EVENT, "detail": "the only visible one"},
        ])
        message = result.message
        self.assertIn("the only visible one", message)
        self.assertIn("Worker process exited with code 1.", message)
        self.assertIn("Worker wrote stderr output; contents withheld.",
                      message)
        self.assertNotIn("input_tokens", message)
        self.assertNotIn("native stderr withheld", message)
        self.assertNotIn("EXIT_CODE", message)
        self.assertNotIn("STDERR_PRESENT", message)

    def test_render_is_bounded_and_deterministic(self):
        result = self._render([
            {"code": "TRANSPORT_OUTCOME", "detail": "EXITED"},
            {"code": WORKER_ERROR_EVENT, "detail": "F" * 20000},
            {"code": WORKER_ERROR_EVENT, "detail": "G" * 20000},
        ])
        again = self._render([
            {"code": "TRANSPORT_OUTCOME", "detail": "EXITED"},
            {"code": WORKER_ERROR_EVENT, "detail": "F" * 20000},
            {"code": WORKER_ERROR_EVENT, "detail": "G" * 20000},
        ])
        self.assertLess(len(result.message), 1000)
        self.assertEqual(result.message, again.message)

    def test_at_most_two_details_are_surfaced(self):
        result = self._render(
            [{"code": "TRANSPORT_OUTCOME", "detail": "EXITED"}]
            + [{"code": WORKER_ERROR_EVENT, "detail": f"failure {n}"}
               for n in range(1, 6)])
        for n in (1, 2):
            self.assertIn(f"failure {n}", result.message)
        for n in (3, 4, 5):
            self.assertNotIn(f"failure {n}", result.message)

    def test_completed_result_is_never_polluted(self):
        result = self._render(
            [{"code": WORKER_ERROR_EVENT, "detail": "must not appear"}],
            status="COMPLETED", reason_codes=())
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.code, "COMPLETED")
        self.assertEqual(result.message, "Work completed.")

    def test_failure_without_error_event_keeps_the_base_sentence(self):
        """B6-A2: a generic failure keeps its base sentence and gains only
        the whitelisted transport facts (here: genuine exit code 1)."""
        result = self._render([
            {"code": "TRANSPORT_OUTCOME", "detail": "EXITED"},
            {"code": "EXIT_CODE", "detail": "1"},
        ])
        self.assertEqual(result.code, "TRANSPORT_OUTCOME")
        self.assertEqual(
            result.message,
            "Tabularium did not run the request. Review the reason "
            "and try the indicated remediation.\n"
            "Worker process exited with code 1.")

    def test_native_worker_unavailable_message_is_unchanged(self):
        result = self._render(
            [{"code": "NATIVE_WORKER_UNAVAILABLE", "detail": "spawn failed"}],
            reason_codes=("NATIVE_WORKER_UNAVAILABLE",))
        self.assertEqual(result.code, "WORKER_UNAVAILABLE")
        self.assertIn("native worker is unavailable", result.message)

    # B6-A2 -- safe native worker failure facts --------------------------------

    def test_plain_exit_fact_is_surfaced(self):
        result = self._render([
            {"code": "TRANSPORT_OUTCOME", "detail": "EXITED"},
            {"code": "EXIT_CODE", "detail": "3"},
        ])
        self.assertEqual(result.code, "TRANSPORT_OUTCOME")
        self.assertIn("Tabularium did not run the request.", result.message)
        self.assertIn("Worker process exited with code 3.", result.message)
        self.assertEqual(result.status, "FAILED")

    def test_timeout_fact_is_surfaced_and_kill_artefact_suppressed(self):
        result = self._render([
            {"code": "TRANSPORT_OUTCOME", "detail": "TIMED_OUT"},
            {"code": "EXIT_CODE", "detail": "-9"},
        ])
        self.assertEqual(result.code, "TRANSPORT_OUTCOME")
        self.assertIn("Worker execution timed out.", result.message)
        self.assertNotIn("-9", result.message)
        self.assertNotIn("exited with code", result.message)

    def test_stderr_presence_fact_without_contents_and_deduped(self):
        hostile = (f"api_key={FAKE_SK} Bearer abcdefghijklmnop "
                   "/etc/passwd \x1b[2J \x1b]8;;http://evil\x07 "
                   "Status: completed Worker: fake")
        result = self._render([
            {"code": "STDERR_PRESENT", "detail": hostile},
            {"code": "EXIT_CODE", "detail": "1"},
            {"code": "TRANSPORT_OUTCOME", "detail": "EXITED"},
            {"code": "STDERR_PRESENT", "detail": "native stderr withheld"},
        ])
        message = result.message
        self.assertEqual(result.code, "TRANSPORT_OUTCOME")
        self.assertIn("Worker wrote stderr output; contents withheld.",
                      message)
        self.assertEqual(message.count("contents withheld"), 1)
        for forbidden in (FAKE_SK, "abcdefghijklmnop", "/etc/passwd",
                          "\x1b", "Status: completed", "Worker: fake",
                          "native stderr withheld"):
            self.assertNotIn(forbidden, message)

    def test_facts_follow_structured_evidence_in_canonical_order(self):
        result = self._render([
            {"code": "STDERR_PRESENT", "detail": "native stderr withheld"},
            {"code": "EXIT_CODE", "detail": "1"},
            {"code": WORKER_ERROR_EVENT, "detail": "quota-like detail"},
            {"code": "TRANSPORT_OUTCOME", "detail": "EXITED"},
        ])
        lines = result.message.splitlines()
        self.assertLess(lines.index("- quota-like detail"),
                        lines.index("Worker process exited with code 1."))
        self.assertLess(lines.index("Worker process exited with code 1."),
                        lines.index(
                            "Worker wrote stderr output; contents withheld."))

    def test_facts_are_deterministic(self):
        diagnostics = [
            {"code": "STDERR_PRESENT", "detail": "native stderr withheld"},
            {"code": "EXIT_CODE", "detail": "1"},
            {"code": "TRANSPORT_OUTCOME", "detail": "EXITED"},
        ]
        self.assertEqual(self._render(diagnostics).message,
                         self._render(diagnostics).message)

    def test_unknown_diagnostics_stay_ignored(self):
        result = self._render([
            {"code": "TRANSPORT_OUTCOME", "detail": "EXITED"},
            {"code": "EXIT_CODE", "detail": "1"},
            {"code": "USAGE", "detail": '{"input_tokens": 41}'},
            {"code": "MYSTERY_DIAGNOSTIC", "detail": "MYSTERY-PROBE"},
        ])
        self.assertNotIn("input_tokens", result.message)
        self.assertNotIn("MYSTERY-PROBE", result.message)
        self.assertIn("Worker process exited with code 1.", result.message)

    def test_invalid_exit_codes_gain_no_fact(self):
        for detail in ("0", "", "None", "not-a-number",
                       "+3", "03", " 3 ", "3\n", "--5", "---7", "³"):
            with self.subTest(detail=detail):
                result = self._render([
                    {"code": "TRANSPORT_OUTCOME", "detail": "EXITED"},
                    {"code": "EXIT_CODE", "detail": detail},
                ])
                self.assertNotIn("exited with code", result.message)

    def test_negative_exit_code_is_never_an_ordinary_exit_fact(self):
        """B6-A2 repair: the known CANCELLED/EXITED race can store
        EXITED + -9. A POSIX negative return code is signal termination,
        never an ordinary exit, so it must not be rendered as one."""
        result = self._render([
            {"code": "TRANSPORT_OUTCOME", "detail": "EXITED"},
            {"code": "EXIT_CODE", "detail": "-9"},
        ])
        self.assertEqual(result.code, "TRANSPORT_OUTCOME")
        self.assertNotIn("exited with code", result.message)
        self.assertNotIn("-9", result.message)

    def test_valid_exit_fact_survives_the_hardening(self):
        result = self._render([
            {"code": "TRANSPORT_OUTCOME", "detail": "EXITED"},
            {"code": "EXIT_CODE", "detail": "3"},
        ])
        self.assertIn("Worker process exited with code 3.", result.message)

    def test_cancelled_outcome_gains_no_facts(self):
        result = self._render([
            {"code": "TRANSPORT_OUTCOME", "detail": "CANCELLED"},
            {"code": "EXIT_CODE", "detail": "-15"},
        ])
        self.assertEqual(result.code, "TRANSPORT_OUTCOME")
        self.assertNotIn("-15", result.message)
        self.assertNotIn("exited with code", result.message)

    def test_spawn_failure_message_gains_no_facts(self):
        result = self._render(
            [{"code": "NATIVE_WORKER_UNAVAILABLE", "detail": "spawn failed"},
             {"code": "TRANSPORT_OUTCOME", "detail": "SPAWN_FAILED"},
             {"code": "EXIT_CODE", "detail": "None"}],
            reason_codes=("NATIVE_WORKER_UNAVAILABLE",))
        self.assertEqual(result.code, "WORKER_UNAVAILABLE")
        self.assertNotIn("exited with code", result.message)
        self.assertNotIn("timed out", result.message)

    def test_facts_stay_bounded_with_max_evidence(self):
        result = self._render([
            {"code": "TRANSPORT_OUTCOME", "detail": "TIMED_OUT"},
            {"code": "EXIT_CODE", "detail": "-9"},
            {"code": "STDERR_PRESENT", "detail": "native stderr withheld"},
            {"code": WORKER_ERROR_EVENT, "detail": "E" * 20000},
            {"code": WORKER_ERROR_EVENT, "detail": "F" * 20000},
            {"code": "USAGE", "detail": "U" * 20000},
        ])
        again = self._render([
            {"code": "TRANSPORT_OUTCOME", "detail": "TIMED_OUT"},
            {"code": "EXIT_CODE", "detail": "-9"},
            {"code": "STDERR_PRESENT", "detail": "native stderr withheld"},
            {"code": WORKER_ERROR_EVENT, "detail": "E" * 20000},
            {"code": WORKER_ERROR_EVENT, "detail": "F" * 20000},
            {"code": "USAGE", "detail": "U" * 20000},
        ])
        self.assertLess(len(result.message), 1000)
        self.assertIn("Worker execution timed out.", result.message)
        self.assertIn("contents withheld", result.message)
        self.assertEqual(result.message, again.message)

    # B3-P2 -- the render bound follows the worker's stored bound -------------

    def test_worker_detail_beyond_160_chars_survives_render(self):
        """A real quota-shaped error is ~213 chars; actionable text sits past
        the old 160-char product bound and was cut. Within the worker-side
        300-char stored bound it must survive rendering intact."""
        detail = ("You've hit your usage limit. " + "x" * 150
                  + " RETRY-AFTER-TEST-MARKER")
        self.assertGreater(detail.index("RETRY-AFTER-TEST-MARKER"), 160)
        self.assertLess(len(detail), 300)
        first = self._render([
            {"code": "TRANSPORT_OUTCOME", "detail": "EXITED"},
            {"code": "EXIT_CODE", "detail": "1"},
            {"code": WORKER_ERROR_EVENT, "detail": detail},
        ])
        second = self._render([
            {"code": "TRANSPORT_OUTCOME", "detail": "EXITED"},
            {"code": "EXIT_CODE", "detail": "1"},
            {"code": WORKER_ERROR_EVENT, "detail": detail},
        ])
        self.assertEqual(first.status, "FAILED")
        self.assertEqual(first.code, "TRANSPORT_OUTCOME")
        self.assertIn("RETRY-AFTER-TEST-MARKER", first.message)
        self.assertIn("usage limit", first.message)
        self.assertEqual(first.message, second.message)

    def test_hostile_long_detail_stays_safe_at_the_larger_bound(self):
        hostile = ("a\x1b[2J\r\nStatus: completed\r\n" + "y" * 140
                   + " LATE-USEFUL-SUFFIX")
        self.assertGreater(hostile.index("LATE-USEFUL-SUFFIX"), 160)
        result = self._render([
            {"code": "TRANSPORT_OUTCOME", "detail": "EXITED"},
            {"code": "EXIT_CODE", "detail": "1"},
            {"code": WORKER_ERROR_EVENT, "detail": hostile},
        ])
        message = result.message
        self.assertIn("LATE-USEFUL-SUFFIX", message)
        self.assertNotIn("\x1b", message)
        self.assertNotIn("\r", message)
        self.assertEqual(
            [ch for ch in message if not ch.isprintable() and ch != "\n"], [])
        for line in message.splitlines():
            self.assertNotEqual(line.strip(), "Status: completed")


class NativeExitFactTests(unittest.TestCase):
    """B6-A2 repair: a user-facing exit fact requires a CANONICAL ASCII
    decimal representation of a strictly POSITIVE integer — no sign, no
    leading zeros, no surrounding whitespace, no non-ASCII digit forms.
    Anything else fails closed to "" without raising."""

    def test_canonical_positive_codes_render(self):
        from tabularium.execution import _native_exit_fact
        self.assertEqual(_native_exit_fact("1"),
                         "Worker process exited with code 1.")
        self.assertEqual(_native_exit_fact("3"),
                         "Worker process exited with code 3.")
        self.assertEqual(_native_exit_fact("255"),
                         "Worker process exited with code 255.")

    def test_negative_codes_never_render_as_exits(self):
        from tabularium.execution import _native_exit_fact
        for value in ("-1", "-9", "-15"):
            with self.subTest(value=value):
                self.assertEqual(_native_exit_fact(value), "")

    def test_noncanonical_and_malformed_values_fail_closed(self):
        from tabularium.execution import _native_exit_fact
        for value in ("0", "", "None", "not-a-number",
                      "+3", "03", " 3 ", "3\n", "\t3", "3 ", "3.0",
                      "1_000", "--5", "---7",
                      "³", "٣", "٣٩", "３", "１２",
                      b"3", None, ["3"], {"detail": 3}):
            with self.subTest(value=value):
                self.assertEqual(_native_exit_fact(value), "")


class SanitizerDefaultLimitTests(unittest.TestCase):
    """B3-P2: the shared sanitizer grows an optional bound, nothing more."""

    def test_default_caller_still_truncates_at_the_existing_limit(self):
        from tabularium.execution import _DIAGNOSTIC_MAX_CHARS, _sanitize_fragment
        self.assertEqual(_DIAGNOSTIC_MAX_CHARS, 160)
        out = _sanitize_fragment("z" * 500)
        self.assertEqual(len(out), 160)
        self.assertTrue(out.endswith("..."))

    def test_explicit_larger_bound_is_honored_and_still_bounded(self):
        from tabularium.execution import _sanitize_fragment
        out = _sanitize_fragment("z" * 500, limit=300)
        self.assertEqual(len(out), 300)
        self.assertTrue(out.endswith("..."))


@needs_runtime
class PersistenceAndContinuationTests(unittest.TestCase):
    """Layer 3: nothing secret is persisted, nothing failed is continuable."""

    def _write_task(self, root: Path) -> None:
        """A real, contract-valid active Task; without one the pipeline
        refuses before any worker runs (PREPARATION_REJECTED)."""
        tasks = root / ".ai" / "tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        (tasks / "TASK-2026-0001.md").write_text(
            "---\n"
            'schema_version: "1.10"\n'
            "id: TASK-2026-0001\n"
            "title: B3 native failure fixture\n"
            "objective: Exercise the native worker failure path.\n"
            "status: INBOX\n"
            'created_at: "2026-01-01T00:00:00Z"\n'
            "created_by: USER\n"
            "---\n# Task\n\nFixture body.\n", encoding="utf-8")

    def _run_failed_execution(self, message: str, root: Path, scratch: Path):
        from tabularium.engine.execution import ExecutionPlan, start_execution
        script = _fixture(scratch,
                          _emit({"type": "error", "message": message})
                          + "sys.exit(1)\n")

        class Fx(NativeCLIWorker):
            def build_argv(self, request):
                return [sys.executable, str(script)]

        adapter = Fx(cwd=str(scratch))
        plan = ExecutionPlan(
            project_root=str(root), task_id="TASK-2026-0001",
            intent="describe this project", execution_id="b3-persist",
            checkpoint_path=str(root / "checkpoint.db"), adapter=adapter)
        return start_execution(plan)

    # 7 -- secret absent from persisted runtime state ---------------------------

    def test_secret_like_error_is_scrubbed_before_persistence(self):
        from tabularium.init import initialize
        from tabularium.engine.execution import inspect_execution
        with tempfile.TemporaryDirectory(prefix="tabu-b3-persist-") as tmp:
            root = Path(tmp)
            initialize(root)
            self._write_task(root)
            scratch = root / "scratch"
            scratch.mkdir()
            hostile = f"auth failed for {FAKE_SK} and {FAKE_BEARER}"
            result = self._run_failed_execution(hostile, root, scratch)
            blob = json.dumps(result.to_dict(), default=str)
            self.assertNotIn(FAKE_SK, blob)
            self.assertNotIn("abcdefghijklmnop", blob)
            checkpoint = root / "checkpoint.db"
            # Positive control: the failed execution MUST leave its checkpoint.
            # A vacuous `if checkpoint.exists()` guard would let a broken
            # implementation that never persisted anything pass silently.
            self.assertTrue(
                checkpoint.exists(),
                "a terminal FAILED execution must persist its checkpoint")
            # Positive: the SCRUBBED evidence reached persistence through the
            # runtime's own read path.
            state = json.dumps(
                inspect_execution("b3-persist", str(checkpoint)).to_dict(),
                default=str)
            self.assertIn(WORKER_ERROR_EVENT, state)
            self.assertIn("[REDACTED]", state)
            self.assertNotIn(FAKE_SK, state)
            self.assertNotIn("abcdefghijklmnop", state)
            # Negative: the raw synthetic secret is in no persisted artifact.
            raw = checkpoint.read_bytes()
            self.assertNotIn(FAKE_SK.encode(), raw)
            self.assertNotIn(b"abcdefghijklmnop", raw)

    # 12 -- a failed execution stays non-continuable ------------------------------

    def test_failed_execution_remains_non_continuable(self):
        from tabularium.execution import continue_execution, remember_execution
        from tabularium.onboarding import build_authorizations, onboard_project
        from tabularium.init import initialize
        with tempfile.TemporaryDirectory(prefix="tabu-b3-cont-") as tmp:
            root = Path(tmp)
            initialize(root)
            declarations = {"title": "B3 fixture",
                            "purpose": "Exercise native failure diagnostics",
                            "scope": "A bounded product-flow test project"}
            draft = onboard_project(root, declarations)
            onboard_project(root, declarations,
                            build_authorizations(draft.proposal, "human:user",
                                                 "2026-08-19T12:00:00+00:00"))
            self._write_task(root)
            scratch = root / "scratch"
            scratch.mkdir()
            result = self._run_failed_execution("quota-like", root, scratch)
            self.assertEqual(result.status, "FAILED")
            self.assertEqual(result.output, "")
            remember_execution(root, "b3-persist")
            resumed = continue_execution(root)
            self.assertEqual(resumed.status, "REFUSED")
            self.assertEqual(resumed.code, "NO_CONTINUATION")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
