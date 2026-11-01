"""Agent Skills packaging plus the Tabularium governance sidecar (M3.4).

A skill says HOW a class of work should be performed. It grants no authority.

    Agent Skills owns   the package convention and SKILL.md content
    Tabularium owns           the governance sidecar, effective authority, worker
                        and context compatibility, the approval lower bound,
                        and the persistence upper bound

The Tabularium sidecar is a Tabularium extension and is not part of the upstream Agent
Skills standard.

This package is read-only: it loads and evaluates local skill packages. It
installs nothing, downloads nothing, executes nothing, and calls no model. It
must not import `tabularium.engine.context_loader`.
"""

from .governance import check_payload, check_tools, check_worker, evaluate
from .loader import load_skill
from .model import (CONTEXT_CLASSES, GOVERNANCE_FIELDS, OUTPUT_CONTRACTS,
                    PERSISTENCE_LEVELS, PERSISTENCE_NONE, PERSISTENCE_PROPOSE,
                    SIDECAR_FILENAME, TOOL_CLASSES, Compatibility,
                    ControlPlaneGrant, EffectiveConstraints, GovernanceResult,
                    SkillIdentity, SkillLoadResult, SkillPackage, SkillSidecar)

__all__ = ["load_skill", "evaluate", "check_worker", "check_payload",
           "check_tools", "SkillPackage", "SkillSidecar", "SkillIdentity",
           "SkillLoadResult", "ControlPlaneGrant", "EffectiveConstraints",
           "GovernanceResult", "Compatibility", "GOVERNANCE_FIELDS",
           "CONTEXT_CLASSES", "TOOL_CLASSES", "OUTPUT_CONTRACTS",
           "PERSISTENCE_LEVELS", "PERSISTENCE_NONE", "PERSISTENCE_PROPOSE",
           "SIDECAR_FILENAME"]
