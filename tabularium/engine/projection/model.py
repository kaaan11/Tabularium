"""Projection result types and reason codes (M3.3).

A CONTEXT PACKAGE AND A PROJECTED PAYLOAD ARE DIFFERENT OBJECTS

A Context Package is what M2 governed as relevant: complete, ephemeral, and
authority-bearing. A ProjectedPayload is what fits a budget: lossy,
non-authoritative, and shaped for a worker. Projection may reduce information;
it may never increase authority.

Neither object is persisted. Both are ephemeral, exactly as the frozen M2
contract requires of the package it derives from.

Reason codes are Tabularium-owned and deterministic. Expected governed states —
a package that is not ready, a budget that cannot hold the required material —
are returned as structured results, never raised. Exceptions are reserved for
programming and type misuse.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .budget import compact

# --- outcome ----------------------------------------------------------------

PROJECTED = "PROJECTED"
REFUSED = "REFUSED"

# --- refusal reason codes ---------------------------------------------------

PACKAGE_NOT_READY = "PACKAGE_NOT_READY"
MISSING_REQUIRED_EXECUTION_CONTEXT = "MISSING_REQUIRED_EXECUTION_CONTEXT"
BUDGET_OVERFLOW = "BUDGET_OVERFLOW"
UNSUPPORTED_PROJECTION_PROFILE = "UNSUPPORTED_PROJECTION_PROFILE"

REFUSAL_CODES = (PACKAGE_NOT_READY, MISSING_REQUIRED_EXECUTION_CONTEXT,
                 BUDGET_OVERFLOW, UNSUPPORTED_PROJECTION_PROFILE)

# --- omission reasons -------------------------------------------------------

OMITTED_BUDGET = "BUDGET"          # eligible, did not fit
OMITTED_COLD = "COLD"              # never injected automatically
OMITTED_NOT_PRESENT = "NOT_PRESENT"  # the package carried nothing of this kind
# Excluded by the caller's effective context classes (M3.4 skill governance,
# wired in M4.2). Distinct from BUDGET: the material was not permitted, not
# merely unaffordable.
OMITTED_NOT_PERMITTED = "NOT_PERMITTED"


@dataclass(frozen=True)
class ProjectedPayload:
    """What a worker may actually see.

    Ordered sections, each already reduced. The payload holds no absolute
    filesystem path, no project root, and no credential: a worker cannot use
    it to reach past what governance selected.
    """

    sections: tuple[tuple[str, Any], ...] = ()

    def section_names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.sections)

    def get(self, name: str) -> Any:
        for section_name, value in self.sections:
            if section_name == name:
                return value
        return None

    def to_dict(self) -> dict[str, Any]:
        # Insertion order is the priority order the projector applied; it is
        # deterministic and meaningful, so it is preserved here rather than
        # re-sorted.
        return {name: value for name, value in self.sections}

    def to_json(self, indent: int | None = None) -> str:
        if indent is None:
            return compact(self.to_dict())
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True,
                          ensure_ascii=False)


@dataclass(frozen=True)
class Truncation:
    """One explicitly marked, bounded truncation.

    Truncation is only ever applied to narrative prose. Ids, enum and status
    values, approval state, diagnostic categories, and provenance identifiers
    are never truncated — shortening any of those would change meaning rather
    than reduce volume.
    """

    section: str
    item_id: str
    field_name: str
    original_chars: int
    kept_chars: int

    def to_dict(self) -> dict[str, Any]:
        return {"section": self.section, "item_id": self.item_id,
                "field": self.field_name,
                "original_chars": self.original_chars,
                "kept_chars": self.kept_chars}


@dataclass(frozen=True)
class Omission:
    """Something the package held that the payload does not carry."""

    section: str
    reason: str
    count: int
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"section": self.section, "reason": self.reason,
                "count": self.count, "detail": self.detail}


@dataclass(frozen=True)
class ContextEconomy:
    """The four metrics, each in characters and estimated tokens.

    Units are stable and all three measured quantities pass through the same
    canonical serializer, so they are directly comparable.

    available_context
        Everything the governed Context Package represents, as M2 emitted it.

    governed_context
        The subset eligible for projection: `available_context` minus material
        classified COLD, which is never injected automatically at any budget.

    payload_size
        What the worker actually receives.

    context_avoided
        `governed_context - payload_size`. What eligible material was kept out
        of the payload. It is not a claim about tokens saved at a provider,
        because the estimator is a heuristic.
    """

    available_context: dict[str, int]
    governed_context: dict[str, int]
    payload_size: dict[str, int]
    context_avoided: dict[str, int]

    @property
    def reduction_pct(self) -> float | None:
        """Percent of ELIGIBLE material kept out of the payload.

        Defined exactly as
            100 * context_avoided.chars / governed_context.chars
        rounded to one decimal place, and None when `governed_context.chars`
        is zero. An undefined denominator yields no percentage rather than a
        manufactured one.
        """
        denominator = self.governed_context["chars"]
        if denominator <= 0:
            return None
        return round(100.0 * self.context_avoided["chars"] / denominator, 1)

    def to_dict(self) -> dict[str, Any]:
        return {"available_context": dict(self.available_context),
                "governed_context": dict(self.governed_context),
                "payload_size": dict(self.payload_size),
                "context_avoided": dict(self.context_avoided),
                "reduction_pct": self.reduction_pct}


@dataclass(frozen=True)
class ProjectionResult:
    """The complete, deterministic outcome of one projection.

    A refusal carries metrics too, so that "this could not be projected" is
    still measurable and diagnosable.
    """

    outcome: str
    profile: str
    budget_limit_tokens: int
    estimated_payload_tokens: int
    within_budget: bool
    economy: ContextEconomy
    payload: ProjectedPayload | None = None
    reason_codes: tuple[str, ...] = ()
    truncations: tuple[Truncation, ...] = ()
    omissions: tuple[Omission, ...] = ()
    cold_context_available: tuple[str, ...] = ()
    estimator: str = ""

    @property
    def projected(self) -> bool:
        return self.outcome == PROJECTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "profile": self.profile,
            "budget_limit_tokens": self.budget_limit_tokens,
            "estimated_payload_tokens": self.estimated_payload_tokens,
            "within_budget": self.within_budget,
            "estimator": self.estimator,
            "reason_codes": list(self.reason_codes),
            "economy": self.economy.to_dict(),
            "truncations": [t.to_dict() for t in self.truncations],
            "omissions": [o.to_dict() for o in self.omissions],
            "cold_context_available": list(self.cold_context_available),
            "payload": self.payload.to_dict() if self.payload else None,
        }

    def to_json(self) -> str:
        return compact(self.to_dict())

    def report(self) -> str:
        """A few lines a developer can read. Not telemetry, not a dashboard."""
        econ = self.economy
        pct = econ.reduction_pct
        lines = [
            f"profile={self.profile} outcome={self.outcome} "
            f"within_budget={self.within_budget}",
            f"  governed context : {econ.governed_context['estimated_tokens']:>6} est. tokens "
            f"({econ.governed_context['chars']} chars)",
            f"  projected payload: {econ.payload_size['estimated_tokens']:>6} est. tokens "
            f"({econ.payload_size['chars']} chars)",
            f"  context avoided  : {econ.context_avoided['estimated_tokens']:>6} est. tokens"
            + (f" ({pct}% of eligible)" if pct is not None else ""),
        ]
        if self.reason_codes:
            lines.append(f"  refused: {', '.join(self.reason_codes)}")
        return "\n".join(lines)
