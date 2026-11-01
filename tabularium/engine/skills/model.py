"""Skill package and Tabularium governance sidecar types (M3.4).

A SKILL TELLS A WORKER HOW TO PERFORM A CLASS OF WORK. IT GRANTS NO AUTHORITY.

A skill does not decide what project state is authoritative, whether execution
is allowed, whether an approval is valid, whether persistence is allowed,
which Task is actionable, or which Context Package is correct. Those are Tabularium
AI OS responsibilities and stay outside the skill package.

The division of ownership is deliberate and is not a claim about the upstream
standard:

    Agent Skills owns   the package convention and SKILL.md instructional
                        content
    Tabularium owns           the governance sidecar, effective authority
                        calculation, worker and context compatibility, the
                        approval lower bound, and the persistence upper bound

The sidecar's Tabularium fields are a Tabularium extension. They are NOT part of the
Agent Skills standard, and a package that loses its sidecar loses privileges
rather than gaining them.

"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from tabularium.engine.projection import PAYLOAD_SECTIONS
from tabularium.engine.workers import WORKER_CATEGORIES

# --- sidecar shape ----------------------------------------------------------

SIDECAR_FILENAME = "tabu.yaml"
INSTRUCTIONS_FILENAME = "SKILL.md"

SIDECAR_VERSION = "1.0"

# The six governance dimensions. Deliberately small: this is not a permission
# DSL, and it is not meant to grow one field per future concern.
GOVERNANCE_FIELDS = ("allowed_context_classes", "allowed_worker_categories",
                     "allowed_tool_classes", "approval_required",
                     "output_contract", "persistence")

# `sidecar_version` carries NO governance meaning. It versions the file shape
# so a later format change is explicit rather than inferred, exactly as
# `manifest_version` does for the Project Manifest.
SIDECAR_FIELDS = ("sidecar_version",) + GOVERNANCE_FIELDS

# --- closed vocabularies ----------------------------------------------------

# Context classes ARE payload section names. There is no second taxonomy:
# every allowed class is objectively checkable against a Projected Payload.
CONTEXT_CLASSES = PAYLOAD_SECTIONS

# Worker categories are reused from M3.3, never redeclared. HUMAN is not a
# worker category there, so it cannot become one here.
WORKER_CATEGORY_VALUES = WORKER_CATEGORIES

# Tool execution is an M4 concern. M3.4 declares only the skill-side upper
# bound, and the reference skill claims none. NONE is spelled explicitly so a
# skill can state "no tools" rather than leaving it to be inferred from an
# empty list.
#
# M4.6 EXTENSION (authorized, additive): the four real classes below are the
# closed vocabulary the governed tool boundary attaches to, exactly where M3.4
# said M4 would attach. This only WIDENS what a sidecar may legally declare —
# it changes nothing for existing sidecars, and NONE keeps its meaning. The
# authority direction is unchanged: `ControlPlaneGrant.tool_classes` still
# defaults to empty, so a class named here is permitted only when the control
# plane grants it AND the skill declares it.
TOOL_CLASSES = ("NONE", "READ", "WRITE", "PROCESS", "NETWORK")

# What kind of output the skill expects to produce. This is an execution
# expectation only: it creates no Evidence, Decision, Report, or Handoff, and
# it maps to no M1 record contract.
OUTPUT_CONTRACTS = ("TEXT", "REVIEW", "CODE_CHANGE", "RESEARCH_SUMMARY")

# Persistence: the MAXIMUM persistence behavior the skill is eligible to
# REQUEST. There is deliberately no value meaning "may write".
#
#   NONE     the skill never proposes durable material
#   PROPOSE  the skill may propose durable material through a native semantic
#            owner; every proposal still passes the nine-condition persistence
#            gate
#
# A third value granting write authority is not offered, and must not be
# added, because gate condition 6 requires explicit human authorization for
# the exact write. No value in a file checked into a repository can satisfy
# that, so the vocabulary cannot express it.
PERSISTENCE_NONE = "NONE"
PERSISTENCE_PROPOSE = "PROPOSE"
PERSISTENCE_LEVELS = (PERSISTENCE_NONE, PERSISTENCE_PROPOSE)
PERSISTENCE_RANK = {PERSISTENCE_NONE: 0, PERSISTENCE_PROPOSE: 1}

# --- forbidden content ------------------------------------------------------
#
# Secrets are never written into the repository, into context, or into a skill
# package (immutable principle 6). A sidecar key that
# looks like a credential is a malformed package, not a field to be ignored.
SECRET_KEY_PATTERN = re.compile(
    r"(api[_-]?key|secret|password|passwd|token|credential|auth|private[_-]?key"
    r"|bearer|access[_-]?key)", re.IGNORECASE)

SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SKILL_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+(\.[0-9]+)?$")

# --- reason codes -----------------------------------------------------------

SKILL_ALLOWED = "SKILL_ALLOWED"
SKILL_MALFORMED = "SKILL_MALFORMED"

MISSING_INSTRUCTIONS = "MISSING_INSTRUCTIONS"
MISSING_SIDECAR = "MISSING_SIDECAR"
SIDECAR_UNPARSEABLE = "SIDECAR_UNPARSEABLE"
SIDECAR_NOT_MAPPING = "SIDECAR_NOT_MAPPING"
SIDECAR_VERSION_UNSUPPORTED = "SIDECAR_VERSION_UNSUPPORTED"
MISSING_FIELD = "MISSING_FIELD"
UNKNOWN_FIELD = "UNKNOWN_FIELD"
INVALID_VALUE = "INVALID_VALUE"
SECRET_FIELD = "SECRET_FIELD"
INVALID_SKILL_NAME = "INVALID_SKILL_NAME"
INVALID_SKILL_VERSION = "INVALID_SKILL_VERSION"
MISSING_DESCRIPTION = "MISSING_DESCRIPTION"
INSTRUCTIONS_UNPARSEABLE = "INSTRUCTIONS_UNPARSEABLE"
PATH_ESCAPE = "PATH_ESCAPE"
NOT_A_SKILL_PACKAGE = "NOT_A_SKILL_PACKAGE"

WORKER_CATEGORY_DENIED = "WORKER_CATEGORY_DENIED"
CONTEXT_CLASS_DENIED = "CONTEXT_CLASS_DENIED"
TOOL_CLASS_DENIED = "TOOL_CLASS_DENIED"
OUTPUT_CONTRACT_MISMATCH = "OUTPUT_CONTRACT_MISMATCH"
PERSISTENCE_DENIED = "PERSISTENCE_DENIED"
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
NO_ELIGIBLE_WORKER_CATEGORY = "NO_ELIGIBLE_WORKER_CATEGORY"


def code(category: str, detail: str | None = None) -> str:
    return category if detail is None else f"{category}:{detail}"


# --- types ------------------------------------------------------------------


@dataclass(frozen=True)
class SkillIdentity:
    """Stable local identity. No UUID, no registry, no global namespace.

    Identity implies no authority: naming a skill `security-audit` grants it
    nothing. Provider, model, credentials, and absolute paths are absent by
    construction.
    """

    name: str
    version: str
    description: str


@dataclass(frozen=True)
class SkillSidecar:
    """Tabularium governance limits declared by a skill package.

    Every field is an UPPER BOUND on what the control plane already permits,
    with one exception: `approval_required` is a LOWER BOUND, because a skill
    may make execution stricter but never laxer.
    """

    sidecar_version: str
    allowed_context_classes: tuple[str, ...]
    allowed_worker_categories: tuple[str, ...]
    allowed_tool_classes: tuple[str, ...]
    approval_required: bool
    output_contract: str
    persistence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sidecar_version": self.sidecar_version,
            "allowed_context_classes": list(self.allowed_context_classes),
            "allowed_worker_categories": list(self.allowed_worker_categories),
            "allowed_tool_classes": list(self.allowed_tool_classes),
            "approval_required": self.approval_required,
            "output_contract": self.output_contract,
            "persistence": self.persistence,
        }


@dataclass(frozen=True)
class SkillPackage:
    """One loaded skill package.

    `instructions` is None unless the caller asked for it. SKILL.md is only
    worth its tokens once the skill is actually selected, so the default load
    carries identity and governance and nothing else.
    """

    identity: SkillIdentity
    sidecar: SkillSidecar
    instructions: str | None = None
    path: str = ""

    @property
    def instructions_loaded(self) -> bool:
        return self.instructions is not None


@dataclass(frozen=True)
class SkillLoadResult:
    """Deterministic outcome of loading one package.

    A malformed package is a structured result, not an exception: "this
    package cannot be trusted" is an ordinary answer.
    """

    valid: bool
    reason_codes: tuple[str, ...] = ()
    package: SkillPackage | None = None


@dataclass(frozen=True)
class ControlPlaneGrant:
    """What Tabularium already permits, before any skill narrows it.

    This is the authority side of the intersection. A skill never appears
    here, and nothing a skill declares can add to it.
    """

    worker_categories: tuple[str, ...] = WORKER_CATEGORIES
    context_classes: tuple[str, ...] = CONTEXT_CLASSES
    tool_classes: tuple[str, ...] = ()
    approval_required: bool = False
    persistence: str = PERSISTENCE_NONE
    output_contracts: tuple[str, ...] = OUTPUT_CONTRACTS


@dataclass(frozen=True)
class EffectiveConstraints:
    """The intersection of control-plane grant and skill sidecar.

    Nothing here can exceed the grant it was derived from.
    """

    worker_categories: tuple[str, ...]
    context_classes: tuple[str, ...]
    tool_classes: tuple[str, ...]
    approval_required: bool
    persistence: str
    output_contract: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_categories": list(self.worker_categories),
            "context_classes": list(self.context_classes),
            "tool_classes": list(self.tool_classes),
            "approval_required": self.approval_required,
            "persistence": self.persistence,
            "output_contract": self.output_contract,
        }


@dataclass(frozen=True)
class GovernanceResult:
    """Outcome of evaluating a skill against a control-plane grant."""

    allowed: bool
    reason_codes: tuple[str, ...]
    constraints: EffectiveConstraints | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed,
                "reason_codes": list(self.reason_codes),
                "constraints": self.constraints.to_dict()
                if self.constraints else None}


@dataclass(frozen=True)
class Compatibility:
    """Deterministic compatibility answer. Compatibility, never routing."""

    compatible: bool
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
