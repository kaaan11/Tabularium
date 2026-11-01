"""APIWorker: one WorkerAdapter for every provider LiteLLM supports (M3.6).

There is no OpenAIAdapter, AnthropicAdapter, or GeminiAdapter here, and there
must never be. Eliminating those is the entire reason LiteLLM is a dependency:
changing provider or model changes a `ModelTarget` value, never this class.

WHAT TABULARIUM KEEPS: the request, the projected payload, the token budget, the
capability requirements, model-selection policy, governance metadata,
provenance, approval and persistence semantics, and normalization of whatever
comes back.

WHAT LITELLM KEEPS: provider API differences, request and response format
normalization, provider authentication mechanics, and the provider exception
hierarchy.

A WorkerResult is never Evidence, a Decision, an approval, or project truth,
whatever a model says inside it.
"""

from __future__ import annotations

from tabularium.engine.workers.adapter import check_acceptance
from tabularium.engine.workers.model import (API_MODEL, COMPLETED, FAILED, REJECTED,
                                   STRUCTURED_OUTPUT, WorkerDescriptor,
                                   WorkerRequest, WorkerResult)

from .model import (LITELLM_UNAVAILABLE, MISSING_MODEL_TARGET, ApiCall,
                    ModelTarget)
from .prompt import render_messages
from .transport import (classify_error, extract_text, extract_usage,
                        load_litellm_completion, response_model,
                        sanitize_detail)


class APIWorker:
    """WorkerAdapter over LiteLLM.

    `worker_id` identifies the WORKER. It is deliberately not a model or a
    provider name: one worker services all of them, and encoding a model into
    worker identity would collapse Worker into Model.
    """

    def __init__(self, worker_id: str = "api-default",
                 model_target: ModelTarget | None = None,
                 timeout_s: float | None = 60.0,
                 num_retries: int = 0,
                 completion_fn=None) -> None:
        self._descriptor = WorkerDescriptor(
            worker_id=worker_id, category=API_MODEL,
            capabilities=(STRUCTURED_OUTPUT,))
        self.model_target = model_target
        self.timeout_s = timeout_s
        # Retries default to ZERO. A retry is another paid provider call, so
        # LiteLLM's own retry behavior never becomes Tabularium policy by default.
        self.num_retries = num_retries
        # The seam. Tests inject a fake; production leaves it None and
        # LiteLLM is imported lazily on first execution.
        self._completion_fn = completion_fn

    @property
    def descriptor(self) -> WorkerDescriptor:
        return self._descriptor

    def accepts(self, request: WorkerRequest):
        return check_acceptance(self._descriptor, request)

    def resolve_target(self, request: WorkerRequest) -> ModelTarget | None:
        """The resolved model for this execution.

        A per-request target wins over the worker's configured default.
        Requirements are NOT translated into a model here: turning
        `model_requirements` into a choice is routing, which is Tabularium
        governance and is not implemented in M3.6. With no explicit choice the
        worker refuses rather than guessing.
        """
        target = request.model_requirements.get("resolved_model")
        if isinstance(target, ModelTarget):
            return target
        if isinstance(target, str) and target.strip():
            return ModelTarget(model=target)
        return self.model_target

    def build_call(self, request: WorkerRequest, target: ModelTarget,
                   skill_instructions: str | None = None) -> ApiCall:
        """The exact call that would be issued. Deterministic and inspectable."""
        return ApiCall(
            model=target.model,
            messages=render_messages(request, skill_instructions),
            timeout=self.timeout_s,
            num_retries=self.num_retries,
            max_tokens=target.max_output_tokens,
            temperature=target.temperature)

    def execute(self, request: WorkerRequest,
                skill_instructions: str | None = None) -> WorkerResult:
        accepted, codes = self.accepts(request)
        if not accepted:
            return self._refuse(request, codes,
                                "request refused before execution")

        target = self.resolve_target(request)
        if target is None:
            # No routing in M3.6, and guessing a model would silently make a
            # cost and capability decision the control plane never made.
            return self._refuse(
                request, (MISSING_MODEL_TARGET,),
                "no resolved model was supplied; model selection is Tabularium "
                "policy and is not performed by the transport")

        try:
            completion = self._completion_fn or load_litellm_completion()
        except ImportError as exc:
            return self._refuse(request, (LITELLM_UNAVAILABLE,),
                                sanitize_detail(exc))

        call = self.build_call(request, target, skill_instructions)
        try:
            response = completion(**call.to_kwargs())
        except BaseException as exc:  # noqa: BLE001 - every failure normalizes
            category = classify_error(exc)
            return WorkerResult(
                request_id=request.request_id,
                worker_id=self._descriptor.worker_id,
                status=FAILED,
                limitations=("the provider call did not complete",),
                usage={"requested_model": target.model},
                diagnostics=({"code": category,
                              "detail": sanitize_detail(exc)},
                             {"code": "EXCEPTION_CLASS",
                              "detail": type(exc).__name__}))

        usage = extract_usage(response)
        served = response_model(response)
        return WorkerResult(
            request_id=request.request_id,
            worker_id=self._descriptor.worker_id,
            status=COMPLETED,
            output=extract_text(response),
            usage={**usage.to_dict(),
                   "requested_model": target.model,
                   # Provenance: which model actually served the call, and a
                   # provider hint. Provenance is not identity — the worker is
                   # still the same worker.
                   "served_model": served,
                   "provider_hint": target.provider_hint},
            limitations=("model output is not Evidence, a Decision, or an "
                         "approval; it is an unverified worker result",),
            diagnostics=({"code": "TRANSPORT", "detail": "litellm.completion"},
                         {"code": "RETRIES", "detail": str(self.num_retries)}))

    def cancel(self, request_id: str) -> bool:
        """Honest answer: NO.

        A synchronous provider call cannot be reliably interrupted through
        this boundary. The worker does not declare CANCELLABLE and does not
        pretend to cancel; a timeout is the only bound M3.6 offers. Durable
        cancellation belongs to the M4 runtime.
        """
        return False

    def _refuse(self, request, codes, limitation) -> WorkerResult:
        return WorkerResult(
            request_id=request.request_id,
            worker_id=self._descriptor.worker_id,
            status=REJECTED,
            limitations=(limitation,),
            diagnostics=tuple({"code": code} for code in codes))
