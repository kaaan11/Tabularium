"""Deterministic token-budgeted Context Package projection (M3.3).

Governed context is what M2 selected. A projected payload is what fits a
budget. This package turns the first into the second by omitting and
extracting only — never by summarizing, paraphrasing, or inferring.

It consumes a Context Package and nothing else: no `.ai/` access, no
filesystem, no network, no model, no writes.

This package must not import `tabularium.engine.context_loader`; the loader must not
import this package.
"""

from .budget import (CODE, DEFAULT_LIMITS, HOT, WARM, BudgetProfile,
                     CharRatioEstimator, measure, resolve_profile)
from .model import (BUDGET_OVERFLOW, MISSING_REQUIRED_EXECUTION_CONTEXT,
                    PACKAGE_NOT_READY, PROJECTED, REFUSAL_CODES, REFUSED,
                    UNSUPPORTED_PROJECTION_PROFILE, ContextEconomy, Omission,
                    ProjectedPayload, ProjectionResult, Truncation)
from .projector import PAYLOAD_SECTIONS, project
from .skeleton import ASTBodyPruner, generate_skeleton

__all__ = ["project", "PAYLOAD_SECTIONS", "ProjectionResult",
           "ProjectedPayload",
           "ContextEconomy", "Truncation", "Omission", "BudgetProfile",
           "CharRatioEstimator", "resolve_profile", "measure",
           "HOT", "WARM", "CODE", "DEFAULT_LIMITS", "PROJECTED", "REFUSED",
           "REFUSAL_CODES", "PACKAGE_NOT_READY", "BUDGET_OVERFLOW",
           "MISSING_REQUIRED_EXECUTION_CONTEXT",
           "UNSUPPORTED_PROJECTION_PROFILE",
           "generate_skeleton", "ASTBodyPruner"]
