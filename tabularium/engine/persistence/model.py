"""Persistence proposal, authorization, and result (M4.4).

THREE DISTINCT CONCEPTS, NEVER INTERCHANGEABLE:

    WorkerResult / RuntimeResult   what happened during execution
    PersistenceProposal            what durable change is being PROPOSED
    PersistenceAuthorization       explicit human authority for an EXACT write

There is no conversion from the first to the third. An execution result is not
project truth, and a human approving an EXECUTION has not thereby approved
writing its output into the project.

Constructing or inspecting any object here writes nothing. Only
`writer.apply_authorized_proposal` may touch a project, and only when a
proposal, a passing gate evaluation, and a matching authorization are all
present.

"""

from __future__ import annotations

import hashlib
import json
import re

from tabularium.engine.secret_tokens import PROVIDER_TOKEN_PATTERNS
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

# --- durable families -------------------------------------------------------
#
# Mirrors the frozen loader mapping. Tabularium never decides that a WorkerResult
# "is" one of these: the family is always stated explicitly by the caller.

FAMILY_DIRECTORY: dict[str, str] = {
    "Task": "tasks",
    "Question": "questions",
    "Decision": "decisions",
    "Investigation": "investigations",
    "Evidence": "evidence",
    "Handoff": "handoffs",
    "Report": "reports",
}

FAMILY_ID_PATTERN: dict[str, re.Pattern] = {
    "Task": re.compile(r"^TASK-[0-9]{4}-[0-9]{4}$"),
    "Question": re.compile(r"^QST-[0-9]{4}-[0-9]{4}$"),
    "Decision": re.compile(r"^DEC-[0-9]{4}-[0-9]{4}$"),
    "Investigation": re.compile(r"^INV-[0-9]{4}-[0-9]{4}$"),
    "Evidence": re.compile(r"^EVD-[0-9]{4}-[0-9]{4}$"),
    "Handoff": re.compile(r"^HND-[0-9]{4}-[0-9]{4}$"),
    "Report": re.compile(r"^RPT-[0-9]{4}-[0-9]{4}$"),
}

FAMILIES = tuple(FAMILY_DIRECTORY)

# --- operations -------------------------------------------------------------
#
# Deliberately two. DELETE, MOVE, RENAME, and bulk or glob writes are absent:
# the frozen write-authority boundary describes proposing and creating records
# and updating a Task through its contract, not removing or relocating durable
# truth.

CREATE = "CREATE"
UPDATE = "UPDATE"
OPERATIONS = (CREATE, UPDATE)

# --- persistence eligibility (M3.4 vocabulary, unchanged) -------------------

NONE = "NONE"
PROPOSE = "PROPOSE"

# --- result statuses --------------------------------------------------------

PROPOSAL_READY = "PROPOSAL_READY"
DENIED = "DENIED"
WRITTEN = "WRITTEN"
FAILED = "FAILED"

RESULT_STATUSES = (PROPOSAL_READY, DENIED, WRITTEN, FAILED)

# --- reason codes -----------------------------------------------------------

PERSISTENCE_DISABLED = "PERSISTENCE_DISABLED"
PROPOSAL_INVALID = "PROPOSAL_INVALID"
TARGET_OUTSIDE_PROJECT = "TARGET_OUTSIDE_PROJECT"
TARGET_FAMILY_MISMATCH = "TARGET_FAMILY_MISMATCH"
TARGET_ALREADY_EXISTS = "TARGET_ALREADY_EXISTS"
TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
STRUCTURAL_VALIDATION_FAILED = "STRUCTURAL_VALIDATION_FAILED"
PERSISTENCE_AUTHORIZATION_REQUIRED = "PERSISTENCE_AUTHORIZATION_REQUIRED"
PERSISTENCE_AUTHORIZATION_MISMATCH = "PERSISTENCE_AUTHORIZATION_MISMATCH"
PERSISTENCE_ACTOR_REQUIRED = "PERSISTENCE_ACTOR_REQUIRED"
PERSISTENCE_TIMESTAMP_INVALID = "PERSISTENCE_TIMESTAMP_INVALID"
PROPOSAL_CONTENT_CHANGED = "PROPOSAL_CONTENT_CHANGED"
STALE_TARGET = "STALE_TARGET"
PERSISTENCE_GATE_FAILED = "PERSISTENCE_GATE_FAILED"
WRITE_FAILED = "WRITE_FAILED"
AUTHORIZATION_ALREADY_USED = "AUTHORIZATION_ALREADY_USED"
SECRET_MATERIAL_PRESENT = "SECRET_MATERIAL_PRESENT"
DUPLICATE_CANONICAL_OWNER = "DUPLICATE_CANONICAL_OWNER"

REASON_CODES = (
    PERSISTENCE_DISABLED, PROPOSAL_INVALID, TARGET_OUTSIDE_PROJECT,
    TARGET_FAMILY_MISMATCH, TARGET_ALREADY_EXISTS, TARGET_NOT_FOUND,
    STRUCTURAL_VALIDATION_FAILED, PERSISTENCE_AUTHORIZATION_REQUIRED,
    PERSISTENCE_AUTHORIZATION_MISMATCH, PERSISTENCE_ACTOR_REQUIRED,
    PERSISTENCE_TIMESTAMP_INVALID, PROPOSAL_CONTENT_CHANGED, STALE_TARGET,
    PERSISTENCE_GATE_FAILED, WRITE_FAILED, AUTHORIZATION_ALREADY_USED,
    SECRET_MATERIAL_PRESENT, DUPLICATE_CANONICAL_OWNER)

# ISO 8601 with an EXPLICIT offset, matching M4.3 and the frozen Task
# convention. A naive timestamp is refused rather than reinterpreted.
_OFFSET = re.compile(r"(Z|[+-][0-9]{2}:?[0-9]{2})$")

# Credential shapes that must never enter durable project truth (frozen gate
# condition 7). Deliberately narrow: this is a guard on what this layer emits,
# not a general-purpose secret scanner.
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9]{8,}"),
    re.compile(r"\b(api[_-]?key|secret[_-]?key|access[_-]?key)\b\s*[:=]",
               re.IGNORECASE),
    re.compile(r"\bpassword\b\s*[:=]", re.IGNORECASE),
    re.compile(r"\bauthorization\b\s*[:=]\s*\S", re.IGNORECASE),
    re.compile(r"\bbearer\s+[A-Za-z0-9._-]{8,}", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    *PROVIDER_TOKEN_PATTERNS,
)


def digest(text: str) -> str:
    """SHA-256 over the exact bytes that would be written.

    An identity and integrity binding, not authentication: there is no secret
    and no MAC. Its only job is to make "this authorization is for THIS
    content" checkable, so a one-character change invalidates it.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def valid_timestamp(value: Any) -> bool:
    """ISO 8601 with an explicit UTC offset. Naive values are refused."""
    if not isinstance(value, str) or not _OFFSET.search(value.strip()):
        return False
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


@dataclass(frozen=True)
class PersistenceProposal:
    """A PROPOSED durable change. Creating one changes nothing.

    The caller states the family, the operation, the exact target, and the
    exact content. Tabularium does not infer any of them: deciding that a worker's
    output "is" Evidence is a judgement this layer refuses to make, and M4.5
    owns any mapping from execution outcome to record family.

    `expected_current_digest` binds an UPDATE to the state the proposal was
    built against, so a target that moved underneath is refused rather than
    overwritten.
    """

    proposal_id: str
    execution_id: str
    target_family: str
    operation: str
    target_path: str                      # project-relative, e.g. .ai/reports/RPT-….md
    content: str
    record_id: str = ""
    task_id: str = ""
    reason: str = ""
    effective_persistence: str = NONE
    created_from: dict[str, Any] = field(default_factory=dict)
    expected_current_digest: str = ""

    @property
    def content_digest(self) -> str:
        return digest(self.content)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["content_digest"] = self.content_digest
        return data

    def binding(self) -> str:
        """The canonical identity an authorization must bind to.

        Deterministic and content-sensitive: family, operation, target,
        record, and content digest. Changing any of them produces a different
        binding, which is what makes a stale authorization fail closed.
        """
        return digest(json.dumps({
            "proposal_id": self.proposal_id,
            "execution_id": self.execution_id,
            "target_family": self.target_family,
            "operation": self.operation,
            "target_path": self.target_path,
            "record_id": self.record_id,
            "content_digest": self.content_digest,
        }, sort_keys=True))


@dataclass(frozen=True)
class PersistenceAuthorization:
    """Explicit human authority for ONE exact write.

    Distinct from an M4.3 execution approval, and deliberately not derivable
    from one: a human may authorize a worker to RUN without authorizing its
    output to become project truth. Those are different questions, so they
    need different answers.

    Binds to the proposal's CONTENT, not merely its id or path.
    """

    proposal_id: str
    execution_id: str
    target_path: str
    operation: str
    content_digest: str
    authorized_by: str
    authorized_at: str
    binding: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "PersistenceAuthorization | None":
        """Rebuild from plain data, or None if it is not an authorization.

        Strict by design: anything lacking the full provenance field set
        (ids, path, operation, content digest, actor, timestamp) is not an
        authorization, which is why no bare value can ever authorize a
        write. The composite `binding` digest is optional; when supplied it
        must match the proposal's canonical binding.
        """
        if not isinstance(data, dict):
            return None
        required = ("proposal_id", "execution_id", "target_path", "operation",
                    "content_digest", "authorized_by", "authorized_at")
        if not all(key in data for key in required):
            return None
        return cls(**{key: str(data.get(key) or "")
                      for key in required + ("binding", "note")})


@dataclass(frozen=True)
class ConditionResult:
    """One frozen persistence-gate condition, explicitly evaluated."""

    number: int
    identifier: str
    passed: bool
    reason_code: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GateEvaluation:
    """The result of evaluating all nine conditions. Read-only."""

    eligible: bool
    conditions: tuple[ConditionResult, ...] = ()
    reason_codes: tuple[str, ...] = ()

    @property
    def failed(self) -> tuple[ConditionResult, ...]:
        return tuple(c for c in self.conditions if not c.passed)

    def to_dict(self) -> dict[str, Any]:
        return {"eligible": self.eligible,
                "reason_codes": list(self.reason_codes),
                "conditions": [c.to_dict() for c in self.conditions]}


@dataclass(frozen=True)
class PersistenceResult:
    """Operational audit of one persistence attempt.

    NOT an M1 record. It is not Evidence, it proves nothing about the world,
    and it is never written into a project.
    """

    status: str
    proposal_id: str = ""
    target_path: str = ""
    reason_codes: tuple[str, ...] = ()
    gate: GateEvaluation | None = None
    authorized_by: str = ""
    authorized_at: str = ""
    resulting_digest: str = ""
    diagnostics: tuple[dict[str, str], ...] = ()

    @property
    def written(self) -> bool:
        return self.status == WRITTEN

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "proposal_id": self.proposal_id,
                "target_path": self.target_path,
                "reason_codes": list(self.reason_codes),
                "gate": self.gate.to_dict() if self.gate else None,
                "authorized_by": self.authorized_by,
                "authorized_at": self.authorized_at,
                "resulting_digest": self.resulting_digest,
                "diagnostics": [dict(d) for d in self.diagnostics]}
