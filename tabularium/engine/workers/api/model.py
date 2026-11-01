"""API worker vocabulary: model target, errors, and usage (M3.6).

WORKER != MODEL != PROVIDER

`APIWorker` is one worker. The provider and the model are attributes of a
REQUEST, not of the worker's identity: the same worker services every
provider LiteLLM supports, which is the whole reason LiteLLM is here. Swapping
provider or model changes a `ModelTarget`, never a line of adapter code and
never a new adapter class.

"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- error categories -------------------------------------------------------
#
# Tabularium-owned and stable. Provider and LiteLLM exception prose is never
# surfaced as a reason code: the class hierarchy is mapped to these, and any
# sanitized textual detail travels in diagnostics instead.

AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
RATE_LIMITED = "RATE_LIMITED"
TIMEOUT = "TIMEOUT"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
INVALID_REQUEST = "INVALID_REQUEST"
CONTEXT_WINDOW_EXCEEDED = "CONTEXT_WINDOW_EXCEEDED"
CONTENT_REJECTED = "CONTENT_REJECTED"
TRANSPORT_ERROR = "TRANSPORT_ERROR"
UNKNOWN_PROVIDER_ERROR = "UNKNOWN_PROVIDER_ERROR"

ERROR_CATEGORIES = (AUTHENTICATION_ERROR, RATE_LIMITED, TIMEOUT,
                    PROVIDER_UNAVAILABLE, INVALID_REQUEST,
                    CONTEXT_WINDOW_EXCEEDED, CONTENT_REJECTED,
                    TRANSPORT_ERROR, UNKNOWN_PROVIDER_ERROR)

# --- refusal codes ----------------------------------------------------------

MISSING_MODEL_TARGET = "MISSING_MODEL_TARGET"
LITELLM_UNAVAILABLE = "LITELLM_UNAVAILABLE"
PAYLOAD_NOT_EXECUTABLE = "PAYLOAD_NOT_EXECUTABLE"


@dataclass(frozen=True)
class ModelTarget:
    """A RESOLVED model choice, handed to transport by the caller.

    This is the smallest transport-neutral concept that keeps selection policy
    outside the worker. `WorkerRequest.model_requirements` states what the work
    NEEDS (context size, cost tier, capabilities); a `ModelTarget` states what
    was CHOSEN. Routing — turning requirements into a choice — is Tabularium
    governance and is not implemented in M3.6, so the choice arrives
    explicitly rather than being inferred here.

    Without this split the worker would have to guess a model from
    requirements, which is exactly the model-selection policy that must stay
    in the control plane.

    `model` is a LiteLLM model string, including any provider prefix
    (`openai/gpt-4o-mini`, `anthropic/claude-...`). Tabularium does not parse or
    normalize provider naming: that is LiteLLM's job, and re-deriving it here
    would rebuild the per-provider adapters LiteLLM exists to remove.
    """

    model: str
    max_output_tokens: int | None = None
    temperature: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("ModelTarget.model must be a non-empty string")

    @property
    def provider_hint(self) -> str | None:
        """The provider prefix, if the caller supplied one.

        A hint for provenance only. Nothing branches on it, and no
        provider-specific behavior is attached to it.
        """
        return self.model.split("/", 1)[0] if "/" in self.model else None


@dataclass(frozen=True)
class Usage:
    """Normalized token usage. Operational provenance, NOT Evidence.

    Usage says what an execution cost. It supports no claim about the world,
    carries no reliability, and never becomes an M1 record.
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
                "cost_usd": self.cost_usd}


@dataclass(frozen=True)
class ApiCall:
    """Exactly what would be sent to LiteLLM. Deterministic and inspectable.

    Kept as a value so tests can assert request shaping without a provider,
    and so a caller can see the call before it is made. It holds no
    credential: authentication is the operator environment's business and
    LiteLLM reads it directly.
    """

    model: str
    messages: tuple[dict[str, str], ...]
    timeout: float | None = None
    num_retries: int = 0
    max_tokens: int | None = None
    temperature: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(m) for m in self.messages],
            # Retries cost money and tokens. Zero unless a caller explicitly
            # asks otherwise, so no Tabularium execution silently multiplies into
            # several provider calls.
            "num_retries": self.num_retries,
            # No Router, no fallbacks. Changing provider or model alters cost,
            # capability, and data handling, which is a governance decision
            # and never an automatic transport behavior.
            "fallbacks": None,
            "stream": False,
        }
        if self.timeout is not None:
            kwargs["timeout"] = self.timeout
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        kwargs.update(self.extra)
        return kwargs
