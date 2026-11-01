"""The LiteLLM seam and error normalization (M3.6).

This is the ONLY module that knows LiteLLM exists. It is a thin seam so that
every other test in the suite runs without a provider, and so that swapping
LiteLLM later touches one file.

LITELLM IS IMPORTED LAZILY, ON FIRST CALL. Importing `tabularium.engine.workers` — or
the M2 loader, projection, skills, or the CLI spike — must never pull in a
heavy provider stack. An absent LiteLLM disables exactly one worker instead of
breaking the repository.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from .model import (AUTHENTICATION_ERROR, CONTENT_REJECTED,
                    CONTEXT_WINDOW_EXCEEDED, INVALID_REQUEST,
                    PROVIDER_UNAVAILABLE, RATE_LIMITED, TIMEOUT,
                    TRANSPORT_ERROR, UNKNOWN_PROVIDER_ERROR, Usage)


class CompletionFn(Protocol):
    """The seam: anything callable like `litellm.completion(**kwargs)`."""

    def __call__(self, **kwargs) -> Any: ...


def load_litellm_completion() -> Callable:
    """Import LiteLLM on demand and return its completion callable.

    Raises ImportError with a message naming the dependency file, rather than
    letting an obscure traceback surface at worker construction.
    """
    try:
        import litellm
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ImportError(
            "the API worker requires LiteLLM; install it with "
            "`pip install \"tabularium-os[api]\"`. Every other Tabularium path "
            "works without it.") from exc
    # Router and fallbacks are never used: this is the direct SDK call.
    return litellm.completion


# LiteLLM exception class name -> Tabularium error category. Matched by CLASS NAME
# walking the MRO, so no LiteLLM import is needed to classify, a fake seam can
# raise look-alike errors in tests, and a provider's message text never
# becomes a reason code.
_ERROR_BY_NAME = {
    "AuthenticationError": AUTHENTICATION_ERROR,
    "PermissionDeniedError": AUTHENTICATION_ERROR,
    "RateLimitError": RATE_LIMITED,
    "BudgetExceededError": RATE_LIMITED,
    "Timeout": TIMEOUT,
    "APITimeoutError": TIMEOUT,
    "ContextWindowExceededError": CONTEXT_WINDOW_EXCEEDED,
    "ContentPolicyViolationError": CONTENT_REJECTED,
    "BadRequestError": INVALID_REQUEST,
    "InvalidRequestError": INVALID_REQUEST,
    "UnprocessableEntityError": INVALID_REQUEST,
    "UnsupportedParamsError": INVALID_REQUEST,
    "NotFoundError": INVALID_REQUEST,
    "JSONSchemaValidationError": INVALID_REQUEST,
    "ServiceUnavailableError": PROVIDER_UNAVAILABLE,
    "InternalServerError": PROVIDER_UNAVAILABLE,
    "APIConnectionError": TRANSPORT_ERROR,
    "APIConnectionTimeoutError": TIMEOUT,
    "APIError": UNKNOWN_PROVIDER_ERROR,
}

# Checked before the MRO walk so a subclass never matches a broader base
# first. ContextWindowExceededError subclasses BadRequestError in LiteLLM, and
# Timeout subclasses APIError; without this order both would be mis-mapped.
_PRIORITY = ("ContextWindowExceededError", "ContentPolicyViolationError",
             "APITimeoutError", "Timeout", "AuthenticationError",
             "PermissionDeniedError", "RateLimitError")


def classify_error(exc: BaseException) -> str:
    """Map a provider/LiteLLM exception to a stable Tabularium category."""
    names = [cls.__name__ for cls in type(exc).__mro__]
    for name in _PRIORITY:
        if name in names:
            return _ERROR_BY_NAME[name]
    for name in names:
        if name in _ERROR_BY_NAME:
            return _ERROR_BY_NAME[name]
    if isinstance(exc, TimeoutError):
        return TIMEOUT
    if isinstance(exc, (ConnectionError, OSError)):
        return TRANSPORT_ERROR
    return UNKNOWN_PROVIDER_ERROR


# Substrings that must never leave this module inside a diagnostic. A provider
# error occasionally echoes the offending request, and an echoed key must not
# be written into a WorkerResult.
_SECRET_HINTS = ("sk-", "api_key", "api-key", "apikey", "authorization",
                 "bearer ", "token=", "secret", "password")


def sanitize_detail(text: Any, limit: int = 300) -> str:
    """Bounded, secret-scrubbed detail for diagnostics.

    A whole line is dropped if it looks like it carries a credential: losing
    diagnostic detail is strictly preferable to writing a key into a result.
    """
    if not isinstance(text, str):
        text = str(text)
    kept = [line for line in text.splitlines()
            if not any(hint in line.lower() for hint in _SECRET_HINTS)]
    cleaned = " ".join(" ".join(kept).split())
    return cleaned[:limit]


def extract_text(response: Any) -> str:
    """Assistant text from a LiteLLM/OpenAI-shaped response.

    Tolerant of both object and mapping shapes, because the raw provider
    response is never exposed as canonical Tabularium output — only what is read out
    of it is.
    """
    choices = _get(response, "choices") or []
    parts: list[str] = []
    for choice in choices:
        message = _get(choice, "message")
        content = _get(message, "content") if message is not None else None
        if isinstance(content, str) and content:
            parts.append(content)
        elif isinstance(content, list):
            # Structured content blocks: keep only the text parts.
            for block in content:
                text = _get(block, "text")
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(parts)


def extract_usage(response: Any) -> Usage:
    """Normalize usage. Absent usage is tolerated, never invented."""
    raw = _get(response, "usage")
    if raw is None:
        return Usage()
    cost = _get(response, "_hidden_params")
    cost_usd = _get(cost, "response_cost") if cost is not None else None
    return Usage(
        prompt_tokens=_int(_get(raw, "prompt_tokens")),
        completion_tokens=_int(_get(raw, "completion_tokens")),
        total_tokens=_int(_get(raw, "total_tokens")),
        # Cost is captured only when the provider/LiteLLM already reported it.
        # No cost is ever computed, estimated, or looked up over the network.
        cost_usd=float(cost_usd) if isinstance(cost_usd, (int, float)) else None)


def response_model(response: Any) -> str | None:
    value = _get(response, "model")
    return value if isinstance(value, str) else None


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None
