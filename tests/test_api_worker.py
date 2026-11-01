"""Tests for the M3.6 LiteLLM API worker.

Most tests use a pure fake seam and never import LiteLLM, so they run whether
or not the API dependency is installed. A second group exercises REAL LiteLLM
through its `mock_response` path — real request shaping, a real
`ModelResponse`, real usage and cost accounting — with no network and no
credentials; that group is gated on the dependency being present.

The only environment-dependent skip is the real provider smoke test, which
needs a credential this repository must never contain.

Run:  python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import copy
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tabularium.engine.projection.budget import CharRatioEstimator  # noqa: E402
from tabularium.engine.workers import (API_MODEL, CLI_CODING, COMPLETED,  # noqa: E402
                             FAILED, REJECTED, WorkerAdapter,
                             WorkerDescriptor, WorkerRequest, WorkerResult)
from tabularium.engine.workers.api import (ERROR_CATEGORIES, SYSTEM_INSTRUCTION,  # noqa: E402
                                 APIWorker, ApiCall, ModelTarget, Usage,
                                 classify_error, overhead, render_messages,
                                 sanitize_detail)

try:
    import litellm  # noqa: F401
    LITELLM = True
except ImportError:  # pragma: no cover - environment dependent
    LITELLM = False

ESTIMATOR = CharRatioEstimator()
OPENAI_MODEL = "openai/gpt-4o-mini"
ANTHROPIC_MODEL = "anthropic/claude-3-5-haiku-20241022"


def request(**overrides) -> WorkerRequest:
    base = {"request_id": "req-1", "intent": "Return exactly API_WORKER_OK",
            "payload": {"required": {"task": {"id": "TASK-2026-0001"}}},
            "task_id": "TASK-2026-0001"}
    base.update(overrides)
    return WorkerRequest(**base)


class FakeSeam:
    """Records the kwargs it was called with, and returns a canned response."""

    def __init__(self, response=None, error=None):
        self.calls: list[dict] = []
        self.response = response if response is not None else {
            "choices": [{"message": {"content": "API_WORKER_OK"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3,
                      "total_tokens": 14},
            "model": "gpt-4o-mini"}
        self.error = error

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def worker(seam=None, **kwargs) -> APIWorker:
    kwargs.setdefault("model_target", ModelTarget(OPENAI_MODEL))
    return APIWorker(completion_fn=seam or FakeSeam(), **kwargs)


# --- boundary ---------------------------------------------------------------

class TestWorkerBoundary(unittest.TestCase):

    def test_api_worker_implements_the_worker_adapter_protocol(self):
        self.assertIsInstance(worker(), WorkerAdapter)

    def test_category_is_api_model(self):
        self.assertEqual(worker().descriptor.category, API_MODEL)

    def test_worker_identity_is_not_a_model_or_provider(self):
        # One worker services every provider. Encoding a model into worker
        # identity would collapse Worker into Model.
        descriptor = worker().descriptor
        self.assertEqual(descriptor.worker_id, "api-default")
        for token in ("openai", "anthropic", "gpt", "claude", "gemini"):
            with self.subTest(token=token):
                self.assertNotIn(token, descriptor.worker_id.lower())

    def test_no_provider_specific_adapter_exists(self):
        # The reason LiteLLM is a dependency at all.
        api_dir = ROOT / "tabularium" / "engine" / "workers" / "api"
        names = [p.name.lower() for p in api_dir.glob("*.py")]
        for forbidden in ("openai", "anthropic", "gemini", "bedrock",
                          "azure", "cohere", "mistral"):
            with self.subTest(forbidden=forbidden):
                self.assertFalse([n for n in names if forbidden in n])

    def test_result_is_a_worker_result(self):
        result = worker().execute(request())
        self.assertIsInstance(result, WorkerResult)
        self.assertEqual(result.status, COMPLETED)
        self.assertEqual(result.request_id, "req-1")

    def test_incompatible_category_is_refused_before_any_call(self):
        seam = FakeSeam()
        result = worker(seam).execute(request(required_category=CLI_CODING))
        self.assertEqual(result.status, REJECTED)
        self.assertEqual(seam.calls, [])

    def test_missing_capability_is_refused_before_any_call(self):
        seam = FakeSeam()
        result = worker(seam).execute(
            request(required_capabilities=("WRITES_FILES",)))
        self.assertEqual(result.status, REJECTED)
        self.assertEqual(seam.calls, [])

    def test_worker_output_is_never_a_record(self):
        result = worker().execute(request())
        self.assertTrue(any("not Evidence" in limitation
                            for limitation in result.limitations))

    def test_cancel_is_honest(self):
        # A synchronous provider call cannot be reliably interrupted here, so
        # the worker says so rather than pretending.
        self.assertFalse(worker().cancel("req-1"))
        self.assertNotIn("CANCELLABLE", worker().descriptor.capabilities)


# --- model target -----------------------------------------------------------

class TestModelTarget(unittest.TestCase):

    def test_missing_target_is_refused_not_guessed(self):
        seam = FakeSeam()
        result = APIWorker(completion_fn=seam).execute(request())
        self.assertEqual(result.status, REJECTED)
        self.assertEqual([d["code"] for d in result.diagnostics],
                         ["MISSING_MODEL_TARGET"])
        self.assertEqual(seam.calls, [])

    def test_requirements_are_not_translated_into_a_model(self):
        # Routing is Tabularium governance and is not implemented in M3.6. Stating
        # a requirement must never cause the transport to pick a model.
        seam = FakeSeam()
        result = APIWorker(completion_fn=seam).execute(request(
            model_requirements={"min_context_tokens": 200000,
                                "cost_tier": "low"}))
        self.assertEqual(result.status, REJECTED)
        self.assertEqual(seam.calls, [])

    def test_per_request_resolved_model_wins(self):
        seam = FakeSeam()
        worker(seam).execute(request(
            model_requirements={"resolved_model": ANTHROPIC_MODEL}))
        self.assertEqual(seam.calls[0]["model"], ANTHROPIC_MODEL)

    def test_resolved_model_accepts_a_string_or_a_target(self):
        for value in (ANTHROPIC_MODEL, ModelTarget(ANTHROPIC_MODEL)):
            with self.subTest(value=type(value).__name__):
                seam = FakeSeam()
                worker(seam).execute(request(
                    model_requirements={"resolved_model": value}))
                self.assertEqual(seam.calls[0]["model"], ANTHROPIC_MODEL)

    def test_provider_hint_is_provenance_not_identity(self):
        self.assertEqual(ModelTarget(OPENAI_MODEL).provider_hint, "openai")
        self.assertIsNone(ModelTarget("gpt-4o-mini").provider_hint)

    def test_empty_model_is_refused(self):
        for value in ("", "   ", None, 7):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ModelTarget(value)


# --- prompt -----------------------------------------------------------------

class TestPrompt(unittest.TestCase):

    def test_message_structure_is_deterministic(self):
        first = render_messages(request())
        second = render_messages(request())
        self.assertEqual(first, second)
        self.assertEqual([m["role"] for m in first], ["system", "user"])

    def test_system_message_confers_no_authority(self):
        self.assertIn("no authority", SYSTEM_INSTRUCTION.lower())
        self.assertLess(len(SYSTEM_INSTRUCTION), 400)

    def test_constitution_and_contracts_are_not_shipped(self):
        text = " ".join(m["content"] for m in render_messages(request()))
        for leak in ("immutable principle", "constitution", "schema_version",
                     "protocols/", "policies/"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, text.lower())

    def test_projected_payload_is_the_only_context(self):
        from tabularium.engine.projection import project
        from tests.test_projection import INTENT, small_project
        fx = small_project()
        self.addCleanup(fx.cleanup)
        result = project(fx.load(task="TASK-2026-0001"), intent=INTENT)
        text = " ".join(m["content"] for m in
                        render_messages(request(payload=result.payload)))
        self.assertNotIn(str(fx.project), text)
        self.assertNotIn("ai_os_root", text)
        self.assertNotIn("selection_trace", text)
        for section in result.payload.section_names():
            self.assertIn(section, text)

    def test_projection_omissions_do_not_reappear(self):
        from tabularium.engine.projection import project
        from tests.test_projection import INTENT, large_project
        fx = large_project()
        self.addCleanup(fx.cleanup)
        result = project(fx.load(task="TASK-2026-0001"), intent=INTENT,
                         profile="HOT")
        text = " ".join(m["content"] for m in
                        render_messages(request(payload=result.payload)))
        dropped = [o.section for o in result.omissions if o.reason == "BUDGET"]
        self.assertTrue(dropped)
        for section in dropped:
            with self.subTest(section=section):
                self.assertNotIn(f'"{section}":', text)

    def test_skill_instructions_only_when_supplied(self):
        without = render_messages(request())
        self.assertNotIn("SKILL INSTRUCTIONS", without[1]["content"])
        with_skill = render_messages(request(), "Review the architecture.")
        self.assertIn("SKILL INSTRUCTIONS", with_skill[1]["content"])
        self.assertIn("Review the architecture.", with_skill[1]["content"])

    def test_no_skill_catalog_is_ever_included(self):
        from tabularium.engine.skills import load_skill
        reference = ROOT / "examples" / "skills" / "architecture-review"
        package = load_skill(reference, include_instructions=True).package
        text = " ".join(m["content"] for m in
                        render_messages(request(), package.instructions))
        # The selected skill only, and never its governance sidecar.
        self.assertIn("Architecture Review", text)
        for governance in ("allowed_worker_categories", "persistence:",
                           "allowed_context_classes"):
            with self.subTest(governance=governance):
                self.assertNotIn(governance, text)

    def test_request_is_not_mutated(self):
        req = request()
        before = copy.deepcopy(req.payload)
        worker().execute(req)
        self.assertEqual(req.payload, before)


# --- call shaping -----------------------------------------------------------

class TestCallShaping(unittest.TestCase):

    def test_retries_default_to_zero(self):
        seam = FakeSeam()
        worker(seam).execute(request())
        self.assertEqual(seam.calls[0]["num_retries"], 0)

    def test_retries_are_explicit_when_enabled(self):
        seam = FakeSeam()
        worker(seam, num_retries=2).execute(request())
        self.assertEqual(seam.calls[0]["num_retries"], 2)

    def test_fallbacks_are_disabled(self):
        # Changing provider or model alters cost, capability, and data
        # handling. That is a governance decision, never automatic transport.
        seam = FakeSeam()
        worker(seam).execute(request())
        self.assertIsNone(seam.calls[0]["fallbacks"])

    def test_streaming_is_off(self):
        seam = FakeSeam()
        worker(seam).execute(request())
        self.assertFalse(seam.calls[0]["stream"])

    def test_timeout_is_forwarded(self):
        seam = FakeSeam()
        worker(seam, timeout_s=12.5).execute(request())
        self.assertEqual(seam.calls[0]["timeout"], 12.5)

    def test_no_credential_is_passed_through_tabu_objects(self):
        seam = FakeSeam()
        worker(seam).execute(request())
        for key in seam.calls[0]:
            with self.subTest(key=key):
                self.assertNotIn(key.lower(),
                                 ("api_key", "api_base", "authorization",
                                  "aws_secret_access_key"))

    def test_max_tokens_and_temperature_come_from_the_target(self):
        seam = FakeSeam()
        APIWorker(model_target=ModelTarget(OPENAI_MODEL, max_output_tokens=256,
                                           temperature=0.0),
                  completion_fn=seam).execute(request())
        self.assertEqual(seam.calls[0]["max_tokens"], 256)
        self.assertEqual(seam.calls[0]["temperature"], 0.0)

    def test_call_is_inspectable_without_executing(self):
        call = worker().build_call(request(), ModelTarget(OPENAI_MODEL))
        self.assertIsInstance(call, ApiCall)
        self.assertEqual(call.model, OPENAI_MODEL)
        self.assertEqual(call.num_retries, 0)


# --- provider swap ----------------------------------------------------------

class TestProviderSwap(unittest.TestCase):
    """The primary architectural reason LiteLLM is here."""

    def test_swapping_provider_needs_no_adapter_change(self):
        seam = FakeSeam()
        one = worker(seam)
        for model in (OPENAI_MODEL, ANTHROPIC_MODEL,
                      "gemini/gemini-2.0-flash", "ollama/llama3"):
            with self.subTest(model=model):
                result = one.execute(request(
                    model_requirements={"resolved_model": model}))
                self.assertEqual(result.status, COMPLETED)
                self.assertEqual(result.worker_id, "api-default")
        self.assertEqual([c["model"] for c in seam.calls],
                         [OPENAI_MODEL, ANTHROPIC_MODEL,
                          "gemini/gemini-2.0-flash", "ollama/llama3"])
        self.assertEqual(len({type(one)}), 1)


# --- response and usage -----------------------------------------------------

class TestResponseNormalization(unittest.TestCase):

    def test_text_is_extracted(self):
        self.assertEqual(worker().execute(request()).output, "API_WORKER_OK")

    def test_usage_is_normalized(self):
        usage = worker().execute(request()).usage
        self.assertEqual(usage["prompt_tokens"], 11)
        self.assertEqual(usage["completion_tokens"], 3)
        self.assertEqual(usage["total_tokens"], 14)

    def test_missing_usage_is_tolerated_not_invented(self):
        seam = FakeSeam(response={"choices": [{"message": {"content": "x"}}]})
        result = worker(seam).execute(request())
        self.assertEqual(result.status, COMPLETED)
        self.assertIsNone(result.usage["total_tokens"])
        self.assertIsNone(result.usage["cost_usd"])

    def test_cost_is_never_invented(self):
        seam = FakeSeam(response={"choices": [{"message": {"content": "x"}}],
                                  "usage": {"total_tokens": 5}})
        self.assertIsNone(worker(seam).execute(request()).usage["cost_usd"])

    def test_served_model_is_provenance_not_identity(self):
        result = worker().execute(request())
        self.assertEqual(result.usage["served_model"], "gpt-4o-mini")
        self.assertEqual(result.usage["requested_model"], OPENAI_MODEL)
        self.assertEqual(result.worker_id, "api-default")

    def test_structured_content_blocks_are_read(self):
        seam = FakeSeam(response={"choices": [{"message": {"content": [
            {"type": "text", "text": "part one"},
            {"type": "text", "text": "part two"}]}}]})
        self.assertEqual(worker(seam).execute(request()).output,
                         "part one\npart two")

    def test_raw_provider_response_is_not_exposed(self):
        result = worker().execute(request())
        self.assertIsInstance(result.output, str)
        for diagnostic in result.diagnostics:
            self.assertIsInstance(diagnostic.get("detail", ""), str)


# --- errors -----------------------------------------------------------------

class TestErrorNormalization(unittest.TestCase):

    def make(self, name, base=Exception):
        return type(name, (base,), {})("provider said no")

    def test_categories_are_tabu_owned(self):
        cases = {
            "AuthenticationError": "AUTHENTICATION_ERROR",
            "PermissionDeniedError": "AUTHENTICATION_ERROR",
            "RateLimitError": "RATE_LIMITED",
            "Timeout": "TIMEOUT",
            "APITimeoutError": "TIMEOUT",
            "ContextWindowExceededError": "CONTEXT_WINDOW_EXCEEDED",
            "ContentPolicyViolationError": "CONTENT_REJECTED",
            "BadRequestError": "INVALID_REQUEST",
            "NotFoundError": "INVALID_REQUEST",
            "ServiceUnavailableError": "PROVIDER_UNAVAILABLE",
            "InternalServerError": "PROVIDER_UNAVAILABLE",
            "APIConnectionError": "TRANSPORT_ERROR",
            "APIError": "UNKNOWN_PROVIDER_ERROR",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(classify_error(self.make(name)), expected)

    def test_unknown_exception_falls_back_safely(self):
        self.assertEqual(classify_error(ValueError("boom")),
                         "UNKNOWN_PROVIDER_ERROR")
        self.assertEqual(classify_error(TimeoutError()), "TIMEOUT")
        self.assertEqual(classify_error(ConnectionError()), "TRANSPORT_ERROR")

    def test_every_category_is_declared(self):
        for name in ("AuthenticationError", "RateLimitError", "Timeout",
                     "BadRequestError", "APIError"):
            self.assertIn(classify_error(self.make(name)), ERROR_CATEGORIES)

    def test_failure_normalizes_to_a_result_not_an_exception(self):
        seam = FakeSeam(error=self.make("RateLimitError"))
        result = worker(seam).execute(request())
        self.assertEqual(result.status, FAILED)
        self.assertEqual(result.diagnostics[0]["code"], "RATE_LIMITED")

    def test_raw_exception_prose_is_not_a_reason_code(self):
        seam = FakeSeam(error=self.make("AuthenticationError"))
        codes = [d["code"] for d in worker(seam).execute(request()).diagnostics]
        for code in codes:
            with self.subTest(code=code):
                self.assertNotIn(" ", code)
                self.assertEqual(code, code.upper())


# --- credentials ------------------------------------------------------------

class TestCredentialBoundary(unittest.TestCase):

    SECRETS = ("sk-abc123def456", "Bearer sk-live-xyz",
               "api_key=sk-secret", "AUTHORIZATION: Bearer t",
               "my password is hunter2")

    def test_secret_like_detail_is_scrubbed(self):
        for secret in self.SECRETS:
            with self.subTest(secret=secret):
                self.assertEqual(sanitize_detail(secret), "")

    def test_secret_never_reaches_diagnostics(self):
        seam = FakeSeam(error=type("AuthenticationError", (Exception,), {})(
            "Incorrect API key provided: sk-abc123def456"))
        result = worker(seam).execute(request())
        blob = str(result.diagnostics) + str(result.usage) + result.output
        self.assertNotIn("sk-abc123def456", blob)
        self.assertEqual(result.diagnostics[0]["code"], "AUTHENTICATION_ERROR")

    def test_detail_is_bounded(self):
        self.assertLessEqual(len(sanitize_detail("x" * 5000)), 300)

    def test_no_credential_appears_in_request_or_result(self):
        import re
        result = worker().execute(request())
        blob = (str(request().__dict__) + str(result.usage)
                + str(result.diagnostics))
        # A bare "sk-" substring also occurs inside "task-2026-0001", so the
        # scan matches a credential SHAPE rather than a fragment.
        patterns = (r"api[_-]?key", r"sk-[A-Za-z0-9]{8,}",
                    r"secret", r"password", r"bearer")
        for pattern in patterns:
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, blob, re.IGNORECASE))

    def test_no_credential_file_is_created(self):
        before = {p.name for p in ROOT.iterdir()}
        worker().execute(request())
        self.assertEqual({p.name for p in ROOT.iterdir()}, before)


# --- token economy ----------------------------------------------------------

class TestTokenEconomy(unittest.TestCase):

    def setUp(self):
        from tabularium.engine.projection import project
        from tests.test_projection import INTENT, large_project
        self.fx = large_project()
        self.addCleanup(self.fx.cleanup)
        self.projection = project(self.fx.load(task="TASK-2026-0001"),
                                  intent=INTENT, profile="HOT")

    def test_wrapper_overhead_is_small(self):
        sizes = overhead(request(payload=self.projection.payload))
        pct = 100.0 * sizes["wrapper_chars"] / sizes["payload_chars"]
        # The wrapper is a FIXED cost: a ~200-character system instruction
        # plus four header lines. The percentage therefore falls as the
        # payload grows, and the bound that matters is the constant, not a
        # round percentage. The system instruction is not trimmed further
        # because it carries the execution boundary itself ("no authority to
        # approve, decide, or record"), which is worth its ~50 tokens.
        self.assertLess(pct, 6.0, f"API wrapper inflated context: {sizes}")
        self.assertLess(sizes["wrapper_chars"], 300)

    def test_projection_savings_survive_api_wrapping(self):
        sizes = overhead(request(payload=self.projection.payload))
        governed = self.projection.economy.governed_context["estimated_tokens"]
        final = ESTIMATOR.tokens(sizes["final_chars"])
        self.assertLess(final, governed / 2,
                        f"final API input {final} vs governed {governed}")

    def test_skill_instruction_cost_is_measured(self):
        from tabularium.engine.skills import load_skill
        reference = ROOT / "examples" / "skills" / "architecture-review"
        package = load_skill(reference, include_instructions=True).package
        sizes = overhead(request(payload=self.projection.payload),
                         package.instructions)
        self.assertGreater(sizes["skill_chars"], 0)
        self.assertEqual(
            sizes["final_chars"],
            sizes["payload_chars"] + sizes["skill_chars"]
            + sizes["wrapper_chars"])

    def test_wrapper_is_a_small_fixed_cost(self):
        small = overhead(request(payload={"required": {}}))
        large = overhead(request(payload=self.projection.payload))
        self.assertLess(abs(small["wrapper_chars"] - large["wrapper_chars"]),
                        200)


# --- isolation --------------------------------------------------------------

class TestIsolation(unittest.TestCase):

    def test_importing_the_api_package_does_not_import_litellm(self):
        import subprocess
        code = ("import sys; import tabularium.engine.workers.api; "
                "print('litellm' in sys.modules)")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                             capture_output=True, text=True, shell=False)
        self.assertEqual(out.stdout.strip(), "False", out.stderr)

    def test_m2_loader_does_not_import_litellm(self):
        import subprocess
        code = ("import sys; import tabularium.engine.context_loader; "
                "print('litellm' in sys.modules)")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                             capture_output=True, text=True, shell=False)
        self.assertEqual(out.stdout.strip(), "False", out.stderr)

    def test_cli_path_does_not_import_litellm(self):
        import subprocess
        code = ("import sys; import tabularium.engine.workers.cli; "
                "print('litellm' in sys.modules)")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                             capture_output=True, text=True, shell=False)
        self.assertEqual(out.stdout.strip(), "False", out.stderr)

    def test_only_one_module_knows_litellm_exists(self):
        api_dir = ROOT / "tabularium" / "engine" / "workers" / "api"
        mentioning = [p.name for p in api_dir.glob("*.py")
                      if "import litellm" in p.read_text(encoding="utf-8")]
        self.assertEqual(mentioning, ["transport.py"])

    def test_frozen_loader_requirements_are_untouched(self):
        text = (ROOT / "tabularium" / "engine" / "requirements.txt").read_text(
            encoding="utf-8")
        self.assertNotIn("litellm", text.lower())
        self.assertIn("PyYAML", text)

    def test_api_dependency_is_declared_separately(self):
        text = (ROOT / "tabularium" / "engine" / "requirements-api.txt").read_text(
            encoding="utf-8")
        self.assertIn("litellm", text.lower())


# --- real LiteLLM, no network ----------------------------------------------

@unittest.skipUnless(LITELLM, "LiteLLM not installed "
                              "(tabularium/engine/requirements-api.txt)")
class TestRealLiteLLM(unittest.TestCase):
    """Exercises REAL LiteLLM via `mock_response`: real request shaping, a
    real ModelResponse, real usage and cost accounting — no network, no key."""

    @staticmethod
    def seam(**kwargs):
        kwargs["mock_response"] = "API_WORKER_OK"
        return litellm.completion(**kwargs)

    def test_real_litellm_call_normalizes(self):
        result = worker(self.seam).execute(request())
        self.assertEqual(result.status, COMPLETED)
        self.assertEqual(result.output, "API_WORKER_OK")
        self.assertIsInstance(result.usage["total_tokens"], int)

    def test_real_provider_swap_uses_one_worker(self):
        one = worker(self.seam)
        served = []
        for model in (OPENAI_MODEL, ANTHROPIC_MODEL):
            result = one.execute(request(
                model_requirements={"resolved_model": model}))
            self.assertEqual(result.status, COMPLETED)
            served.append(result.usage["served_model"])
        self.assertEqual(served, ["gpt-4o-mini",
                                  "claude-3-5-haiku-20241022"])

    def test_litellm_accepts_our_kwargs(self):
        # num_retries and fallbacks are kwargs-only in the current SDK; this
        # asserts the call we build is actually accepted.
        call = worker().build_call(request(), ModelTarget(OPENAI_MODEL))
        kwargs = call.to_kwargs()
        kwargs["mock_response"] = "ok"
        response = litellm.completion(**kwargs)
        self.assertEqual(response.choices[0].message.content, "ok")

    def test_real_exception_classes_map_correctly(self):
        cases = [(litellm.AuthenticationError, "AUTHENTICATION_ERROR"),
                 (litellm.RateLimitError, "RATE_LIMITED"),
                 (litellm.Timeout, "TIMEOUT"),
                 (litellm.ContextWindowExceededError,
                  "CONTEXT_WINDOW_EXCEEDED"),
                 (litellm.ContentPolicyViolationError, "CONTENT_REJECTED"),
                 (litellm.BadRequestError, "INVALID_REQUEST"),
                 (litellm.ServiceUnavailableError, "PROVIDER_UNAVAILABLE"),
                 (litellm.APIConnectionError, "TRANSPORT_ERROR")]
        for cls, expected in cases:
            with self.subTest(cls=cls.__name__):
                # Subclass ordering matters: ContextWindowExceededError
                # subclasses BadRequestError and Timeout subclasses APIError,
                # so a naive MRO walk would mis-map both.
                exc = cls.__new__(cls)
                self.assertEqual(classify_error(exc), expected)

    def test_real_usage_and_cost_are_captured_not_invented(self):
        result = worker(self.seam).execute(request())
        self.assertGreater(result.usage["total_tokens"], 0)
        cost = result.usage["cost_usd"]
        self.assertTrue(cost is None or isinstance(cost, float))


# --- real provider smoke ----------------------------------------------------

class TestRealProviderSmoke(unittest.TestCase):
    """The one environment-dependent skip: needs a real credential."""

    PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                     "GEMINI_API_KEY", "AZURE_API_KEY", "GROQ_API_KEY")

    def test_bounded_real_provider_call(self):
        if not LITELLM:
            raise unittest.SkipTest("LiteLLM not installed")
        available = [k for k in self.PROVIDER_KEYS if os.environ.get(k)]
        if not available:
            raise unittest.SkipTest(
                "no provider credential in the environment (checked: "
                + ", ".join(self.PROVIDER_KEYS)
                + "); real provider smoke unavailable. LiteLLM integration is "
                  "still covered by TestRealLiteLLM without network.")
        model = {"OPENAI_API_KEY": "openai/gpt-4o-mini",
                 "ANTHROPIC_API_KEY": ANTHROPIC_MODEL,
                 "GEMINI_API_KEY": "gemini/gemini-2.0-flash",
                 "GROQ_API_KEY": "groq/llama-3.1-8b-instant",
                 }.get(available[0])
        if model is None:  # pragma: no cover
            raise unittest.SkipTest(f"no bounded model mapped for {available[0]}")
        result = APIWorker(
            model_target=ModelTarget(model, max_output_tokens=16,
                                     temperature=0.0)).execute(
            request(intent="Return exactly the text API_WORKER_OK "
                           "and nothing else. Make no changes."))
        self.assertEqual(result.status, COMPLETED, result.diagnostics)
        self.assertIn("API_WORKER_OK", result.output)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
