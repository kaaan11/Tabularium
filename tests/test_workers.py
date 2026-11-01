"""Tests for the Worker abstraction (M3.3).

The interface shape, the terminology separations it exists to protect, the
mock worker, result normalization, and immutability.

Run:  python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import copy
import sys
import tokenize
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tabularium.engine.workers import (API_MODEL, CLI_CODING, COMPLETED,  # noqa: E402
                             LOCAL, REJECTED, WORKER_CATEGORIES, MockWorker,
                             WorkerAdapter, WorkerDescriptor, WorkerRequest,
                             WorkerResult, check_acceptance)
from tabularium.engine.workers.model import STRUCTURED_OUTPUT, WRITES_FILES  # noqa: E402


def request(**overrides) -> WorkerRequest:
    base = {"request_id": "req-1", "intent": "continue the project",
            "payload": {"required": {}}, "task_id": "TASK-2026-0001"}
    base.update(overrides)
    return WorkerRequest(**base)


class TestWorkerTerminology(unittest.TestCase):
    """Worker != Model != Provider != human authority."""

    def test_categories_are_execution_environments(self):
        self.assertEqual(WORKER_CATEGORIES, (CLI_CODING, API_MODEL, LOCAL))

    def test_human_is_not_a_worker_category(self):
        # A human is an authority and approval actor. Adding HUMAN here would
        # place authority inside the execution abstraction.
        self.assertNotIn("HUMAN", WORKER_CATEGORIES)
        for name in ("HUMAN", "USER", "REVIEWER", "APPROVER"):
            with self.subTest(name=name):
                self.assertNotIn(name, WORKER_CATEGORIES)
                with self.assertRaises(ValueError):
                    WorkerDescriptor(worker_id="w", category=name)

    def test_worker_is_not_a_model_and_not_a_provider(self):
        fields = set(WorkerDescriptor.__dataclass_fields__)
        self.assertEqual(fields, {"worker_id", "category", "capabilities"})
        for forbidden in ("model", "provider", "api_key", "base_url",
                          "endpoint", "vendor"):
            self.assertNotIn(forbidden, fields)

    def test_request_expresses_model_requirements_not_a_model_choice(self):
        fields = set(WorkerRequest.__dataclass_fields__)
        self.assertIn("model_requirements", fields)
        for forbidden in ("model", "provider", "credentials", "api_key",
                          "project_root", "context_package"):
            self.assertNotIn(forbidden, fields)

    def test_no_vendor_or_product_names_in_the_core_interface(self):
        # Vendors may be NAMED IN PROSE when explaining what later adapters
        # will carry. What must not exist is a vendor in the code itself: no
        # identifier, constant, or branch may know a product exists.
        vendors = ("codex", "claude", "openai", "anthropic", "gemini",
                   "litellm", "gpt", "ollama")
        for module in ("model.py", "adapter.py"):
            path = Path(__file__).resolve().parents[1] / "tabularium" / "engine" / "workers" / module
            with tokenize.open(str(path)) as handle:
                tokens = list(tokenize.generate_tokens(handle.readline))
            code = " ".join(
                tok.string.lower() for tok in tokens
                if tok.type not in (tokenize.COMMENT, tokenize.STRING))
            for vendor in vendors:
                with self.subTest(module=module, vendor=vendor):
                    self.assertNotIn(vendor, code)


class TestDescriptor(unittest.TestCase):

    def test_unknown_capability_is_refused(self):
        with self.assertRaises(ValueError):
            WorkerDescriptor(worker_id="w", category=LOCAL,
                             capabilities=("IS_SMARTER",))

    def test_supports(self):
        desc = WorkerDescriptor(worker_id="w", category=CLI_CODING,
                                capabilities=(WRITES_FILES, STRUCTURED_OUTPUT))
        self.assertTrue(desc.supports(WRITES_FILES))
        self.assertFalse(desc.supports(WRITES_FILES, "NETWORK_ACCESS"))

    def test_descriptor_is_immutable(self):
        desc = WorkerDescriptor(worker_id="w", category=LOCAL)
        with self.assertRaises(FrozenInstanceError):
            desc.worker_id = "other"


class TestRequest(unittest.TestCase):

    def test_request_id_is_caller_provided(self):
        # Projection and the worker boundary generate no ids, so a request is
        # reproducible and determinism is preserved end to end.
        with self.assertRaises(ValueError):
            request(request_id="")

    def test_unknown_required_category_is_refused(self):
        with self.assertRaises(ValueError):
            request(required_category="HUMAN")

    def test_request_is_immutable(self):
        req = request()
        with self.assertRaises(FrozenInstanceError):
            req.intent = "something else"


class TestAcceptance(unittest.TestCase):

    def setUp(self):
        self.desc = WorkerDescriptor(worker_id="w", category=LOCAL,
                                     capabilities=(STRUCTURED_OUTPUT,))

    def test_accepts_matching_request(self):
        accepted, codes = check_acceptance(self.desc, request())
        self.assertTrue(accepted)
        self.assertEqual(codes, ())

    def test_category_mismatch_is_a_structured_refusal(self):
        accepted, codes = check_acceptance(
            self.desc, request(required_category=CLI_CODING))
        self.assertFalse(accepted)
        self.assertIn("UNSUPPORTED_CATEGORY", codes)

    def test_missing_capability_names_the_capability(self):
        accepted, codes = check_acceptance(
            self.desc, request(required_capabilities=(WRITES_FILES,)))
        self.assertFalse(accepted)
        self.assertIn("MISSING_CAPABILITY:WRITES_FILES", codes)

    def test_empty_payload_is_refused(self):
        accepted, codes = check_acceptance(self.desc, request(payload=None))
        self.assertFalse(accepted)
        self.assertIn("EMPTY_PAYLOAD", codes)

    def test_refusal_codes_are_deterministic(self):
        req = request(required_category=CLI_CODING,
                      required_capabilities=(WRITES_FILES, "RUNS_COMMANDS"))
        first = check_acceptance(self.desc, req)
        second = check_acceptance(self.desc, req)
        self.assertEqual(first, second)


class TestMockWorker(unittest.TestCase):

    def setUp(self):
        self.worker = MockWorker()

    def test_mock_satisfies_the_adapter_protocol(self):
        self.assertIsInstance(self.worker, WorkerAdapter)

    def test_execute_returns_a_normalized_result(self):
        result = self.worker.execute(request())
        self.assertIsInstance(result, WorkerResult)
        self.assertEqual(result.status, COMPLETED)
        self.assertTrue(result.succeeded)
        self.assertEqual(result.request_id, "req-1")
        self.assertEqual(result.worker_id, "mock:echo")
        self.assertTrue(result.limitations)

    def test_execute_is_deterministic(self):
        first = self.worker.execute(request())
        second = MockWorker().execute(request())
        self.assertEqual(first, second)

    def test_refused_request_is_a_result_not_an_exception(self):
        result = self.worker.execute(request(required_category=CLI_CODING))
        self.assertEqual(result.status, REJECTED)
        self.assertFalse(result.succeeded)
        self.assertEqual([d["code"] for d in result.diagnostics],
                         ["UNSUPPORTED_CATEGORY"])

    def test_worker_does_not_mutate_the_request(self):
        req = request(payload={"required": {"task": {"id": "TASK-2026-0001"}}})
        before = copy.deepcopy(req.payload)
        self.worker.execute(req)
        self.assertEqual(req.payload, before)

    def test_cancel_boundary_exists(self):
        self.assertTrue(self.worker.cancel("req-1"))


class TestResult(unittest.TestCase):

    def test_unknown_status_is_refused(self):
        with self.assertRaises(ValueError):
            WorkerResult(request_id="r", worker_id="w", status="MAYBE")

    def test_result_is_immutable(self):
        result = WorkerResult(request_id="r", worker_id="w", status=COMPLETED)
        with self.assertRaises(FrozenInstanceError):
            result.status = "FAILED"

    def test_result_is_not_a_record(self):
        # A WorkerResult is not Evidence, not a Decision, not a Report, and
        # not an approval. Nothing here carries record identity or lifecycle.
        fields = set(WorkerResult.__dataclass_fields__)
        for forbidden in ("schema_version", "approved_by", "approved_at",
                          "status_record", "evidence_id", "decision_id",
                          "reliability", "source_type"):
            self.assertNotIn(forbidden, fields)


class TestIsolation(unittest.TestCase):

    def test_workers_package_does_not_import_the_loader_or_touch_io(self):
        forbidden = ("os", "pathlib", "socket", "urllib", "requests", "httpx",
                     "subprocess", "sqlite3", "shutil")
        root = Path(__file__).resolve().parents[1] / "tabularium" / "engine" / "workers"
        for path in sorted(root.glob("*.py")):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped.startswith(("import ", "from ")):
                    continue
                with self.subTest(module=path.name, line=stripped):
                    self.assertNotIn("context_loader", stripped)
                    self.assertNotIn(stripped.split()[1].split(".")[0],
                                     forbidden)
                    self.assertNotIn("open(", stripped)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
