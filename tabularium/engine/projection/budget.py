"""Token budget profiles and the deterministic size estimator (M3.3).

THE ESTIMATOR IS A HEURISTIC AND SAYS SO

v0.1 estimates tokens as `ceil(characters / 4)`. No tokenizer dependency is
added: a real tokenizer is model-specific, and adopting one here would couple
the governance side of the boundary to a provider's vocabulary — exactly the
coupling this milestone removes. The estimator is replaceable through
`SizeEstimator`, so a worker that can count exactly (M3.6) may supply a
better one without any change to the projector.

Because it is an estimate, it is reported as one. A projection never claims a
payload fits when the estimator says it does not.

BUDGET BANDS ARE PRODUCT DEFAULTS, NOT GOVERNANCE LAW

They are configurable, they carry no authority, and they are deliberately not
Project Manifest keys — Manifest v1.0 allows exactly three keys and
explicitly rejects `token_budget`. Budgets are
ruleset-side configuration or per-invocation overrides.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Protocol

# --- profiles ---------------------------------------------------------------

HOT = "HOT"
WARM = "WARM"
COLD = "COLD"
CODE = "CODE"

# M3.1 provisional bands. HOT is the default for normal task continuation;
# WARM is for complex debugging, review, and multi-record reasoning.
# CODE is for semantic AST code projection with larger context limits.
DEFAULT_LIMITS: dict[str, int] = {
    HOT: 1200,    # M3.1 band: ~500-1500 estimated tokens
    WARM: 4000,   # M3.1 band: ~3000-5000 estimated tokens
    CODE: 8000,   # Phase 6 band: ~6000-10000 estimated tokens for code
}

# COLD is not a budget. It is the class of material that is never injected
# automatically at any budget; a future runtime may request a governed
# re-projection that includes it. Workers never fetch it themselves.
PROFILES = (HOT, WARM, CODE)

CHARS_PER_TOKEN = 4


class SizeEstimator(Protocol):
    """Replaceable size estimation."""

    name: str

    def tokens(self, chars: int) -> int:
        """Estimated model tokens for a given character count."""


@dataclass(frozen=True)
class CharRatioEstimator:
    """`ceil(chars / chars_per_token)`. Deterministic, model-independent."""

    chars_per_token: int = CHARS_PER_TOKEN
    name: str = "char-ratio-v1"

    def tokens(self, chars: int) -> int:
        if chars <= 0:
            return 0
        return math.ceil(chars / self.chars_per_token)


DEFAULT_ESTIMATOR = CharRatioEstimator()


@dataclass(frozen=True)
class BudgetProfile:
    """One resolved budget: a named profile and its token limit."""

    name: str
    limit_tokens: int
    estimator: SizeEstimator = DEFAULT_ESTIMATOR

    @property
    def limit_chars(self) -> int:
        """The limit expressed in characters, under the current estimator."""
        return self.limit_tokens * CHARS_PER_TOKEN

    def fits(self, chars: int) -> bool:
        return self.estimator.tokens(chars) <= self.limit_tokens


def resolve_profile(name: str, limit_tokens: int | None = None,
                    estimator: SizeEstimator | None = None
                    ) -> BudgetProfile | None:
    """Resolve a profile name to a budget, or None if it is not a profile.

    COLD resolves to None on purpose: it is not an executable budget, so
    asking to project "at COLD" is an unsupported profile rather than an
    empty payload.
    """
    if name not in PROFILES:
        return None
    return BudgetProfile(
        name=name,
        limit_tokens=DEFAULT_LIMITS[name] if limit_tokens is None
        else limit_tokens,
        estimator=estimator or DEFAULT_ESTIMATOR)


# --- measurement ------------------------------------------------------------


def compact(value: Any) -> str:
    """The one canonical serialization used for every measurement.

    Deterministic: keys sorted, no incidental whitespace, no ensure_ascii
    escaping. Every size in a projection result is measured through this
    function, so `available`, `governed`, and `payload` are always in the same
    units and remain comparable.
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def measure(value: Any, estimator: SizeEstimator = DEFAULT_ESTIMATOR
            ) -> dict[str, int]:
    """Size of a value in both units. Characters are exact; tokens estimated."""
    chars = len(compact(value))
    return {"chars": chars, "estimated_tokens": estimator.tokens(chars)}
