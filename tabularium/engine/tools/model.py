"""Governed tool boundary — value objects (M4.6).

THE CORE INVARIANT:

    A WORKER MAY REQUEST A TOOL ACTION.
    A WORKER MAY NOT GRANT ITSELF AUTHORITY TO PERFORM THAT ACTION.

Three objects, never interchangeable:

    ToolRequest         what a worker WANTS to do
    ToolAuthorization   Tabularium/human authority for ONE exact action
    ToolResult          what actually happened

There is no conversion between them, and no bare value — `True`, `"APPROVE"`,
worker prose, an M4.3 execution approval, an M4.4 persistence authorization —
parses as a `ToolAuthorization`.

Tool CLASSES are reused from the M3.4 skill sidecar vocabulary, never
redeclared here: the skill-side upper bound and the tool boundary must be the
same taxonomy or narrowing would be meaningless.

"""

from __future__ import annotations

import hashlib
import json
import re

from tabularium.engine.secret_tokens import PROVIDER_TOKEN_PATTERNS
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from ..skills.model import TOOL_CLASSES

# --- tool classes (reused from M3.4, not redeclared) ------------------------

NONE = "NONE"
READ = "READ"
WRITE = "WRITE"
PROCESS = "PROCESS"
NETWORK = "NETWORK"

# The closed vocabulary is the skill one. `NONE` is a sidecar declaration
# meaning "no tools", never a class a request may claim.
TOOL_CLASS_VALUES = TOOL_CLASSES
REQUESTABLE_CLASSES = tuple(c for c in TOOL_CLASSES if c != NONE)

# --- effect classification --------------------------------------------------
#
# Just enough to decide whether explicit human approval is required. This is
# NOT a risk engine: three deterministic values, assigned per operation by the
# tool descriptor, never inferred from arguments or from worker text.

READ_ONLY = "READ_ONLY"
STATE_CHANGING = "STATE_CHANGING"
EXTERNAL_EFFECT = "EXTERNAL_EFFECT"

EFFECTS = (READ_ONLY, STATE_CHANGING, EXTERNAL_EFFECT)

# Only READ_ONLY may proceed without an explicit tool authorization. Both of
# the others always require one — there is deliberately no "low risk write".
EFFECTS_REQUIRING_AUTHORIZATION = (STATE_CHANGING, EXTERNAL_EFFECT)

# --- result statuses --------------------------------------------------------

EXECUTED = "EXECUTED"
DENIED = "DENIED"
FAILED = "FAILED"
AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"

RESULT_STATUSES = (EXECUTED, DENIED, FAILED, AUTHORIZATION_REQUIRED)

# --- runtime/control surface ------------------------------------------------
#
# The state a runtime surfaces when a tool needs a human. Distinct from the
# M4.1 AWAITING_APPROVAL, which is about EXECUTION authority.

TOOL_APPROVAL_REQUIRED = "TOOL_APPROVAL_REQUIRED"

# --- reason codes -----------------------------------------------------------

TOOL_REQUEST_INVALID = "TOOL_REQUEST_INVALID"
UNKNOWN_TOOL = "UNKNOWN_TOOL"
UNKNOWN_TOOL_CLASS = "UNKNOWN_TOOL_CLASS"
UNKNOWN_OPERATION = "UNKNOWN_OPERATION"
TOOL_CLASS_MISMATCH = "TOOL_CLASS_MISMATCH"
TOOL_CLASS_DENIED_BY_SKILL = "TOOL_CLASS_DENIED_BY_SKILL"
TOOL_SCOPE_REQUIRED = "TOOL_SCOPE_REQUIRED"
TOOL_SCOPE_VIOLATION = "TOOL_SCOPE_VIOLATION"
TOOL_AUTHORIZATION_REQUIRED = "TOOL_AUTHORIZATION_REQUIRED"
TOOL_AUTHORIZATION_MISMATCH = "TOOL_AUTHORIZATION_MISMATCH"
TOOL_AUTHORIZATION_ALREADY_USED = "TOOL_AUTHORIZATION_ALREADY_USED"
TOOL_ARGUMENTS_CHANGED = "TOOL_ARGUMENTS_CHANGED"
TOOL_ACTOR_REQUIRED = "TOOL_ACTOR_REQUIRED"
TOOL_TIMESTAMP_INVALID = "TOOL_TIMESTAMP_INVALID"
TOOL_EXECUTION_MISMATCH = "TOOL_EXECUTION_MISMATCH"
TOOL_ADAPTER_FAILED = "TOOL_ADAPTER_FAILED"
TOOL_GATE_FAILED = "TOOL_GATE_FAILED"
SECRET_MATERIAL_PRESENT = "SECRET_MATERIAL_PRESENT"

REASON_CODES = (
    TOOL_REQUEST_INVALID, UNKNOWN_TOOL, UNKNOWN_TOOL_CLASS, UNKNOWN_OPERATION,
    TOOL_CLASS_MISMATCH, TOOL_CLASS_DENIED_BY_SKILL, TOOL_SCOPE_REQUIRED,
    TOOL_SCOPE_VIOLATION, TOOL_AUTHORIZATION_REQUIRED,
    TOOL_AUTHORIZATION_MISMATCH, TOOL_AUTHORIZATION_ALREADY_USED,
    TOOL_ARGUMENTS_CHANGED, TOOL_ACTOR_REQUIRED, TOOL_TIMESTAMP_INVALID,
    TOOL_EXECUTION_MISMATCH, TOOL_ADAPTER_FAILED, TOOL_GATE_FAILED,
    SECRET_MATERIAL_PRESENT)

# ISO 8601 with an EXPLICIT offset, matching M4.3/M4.4. Naive is refused.
_OFFSET = re.compile(r"(Z|[+-][0-9]{2}:?[0-9]{2})$")

# Credential shapes that must never enter a tool request (they belong to an
# external operator credential source, never to a serialized request object).
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
    """SHA-256 identity/integrity binding. Not authentication — no secret."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical(value: Any) -> str:
    """Deterministic JSON for digesting. Sorted keys, stable separators."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      default=str)


def valid_timestamp(value: Any) -> bool:
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
class ToolRequest:
    """What a worker WANTS to do. Carries no authority whatsoever.

    A structured object by design: there is no path from worker prose to a
    tool action. "Permission granted" or "run this command" in model output is
    text, and nothing in this package parses text into a request or into
    permission.

    `arguments` and `requested_scope` are explicit. Credentials never appear
    here — a tool needing them obtains them from an external operator source.
    """

    tool_request_id: str
    execution_id: str
    tool_class: str
    tool_name: str
    operation: str
    task_id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    requested_scope: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    @property
    def arguments_digest(self) -> str:
        return digest(canonical(self.arguments))

    def binding(self) -> str:
        """The exact identity an authorization must bind to.

        Deterministic and argument-sensitive: changing the path, the content,
        the operation, or the scope produces a different binding, which is what
        makes a stale authorization fail closed.
        """
        return digest(canonical({
            "tool_request_id": self.tool_request_id,
            "execution_id": self.execution_id,
            "tool_class": self.tool_class,
            "tool_name": self.tool_name,
            "operation": self.operation,
            "arguments_digest": self.arguments_digest,
            "requested_scope": self.requested_scope,
        }))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["arguments_digest"] = self.arguments_digest
        return data


@dataclass(frozen=True)
class ToolAuthorization:
    """Tabularium/human authority for ONE exact tool action.

    Distinct from — and NOT derivable from — an M4.3 execution approval or an
    M4.4 persistence authorization. Those answer different questions:
    "may this execution continue" and "may this record be written". Neither
    says "this worker may touch the outside world".

    Binds to the request's exact ARGUMENTS, not merely its id.
    """

    tool_request_id: str
    execution_id: str
    tool_name: str
    operation: str
    arguments_digest: str
    authorized_by: str
    authorized_at: str
    scope: dict[str, Any] = field(default_factory=dict)
    binding: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "ToolAuthorization | None":
        """Rebuild from plain data, or None if it is not an authorization.

        STRICT BY DESIGN. Every provenance and binding key must be present, so
        no bare value, partial dict, execution approval, or persistence
        authorization can ever be read as tool authority.
        """
        if not isinstance(data, dict):
            return None
        required = ("tool_request_id", "execution_id", "tool_name",
                    "operation", "arguments_digest", "authorized_by",
                    "authorized_at")
        if not all(key in data for key in required):
            return None
        scope = data.get("scope") or {}
        if not isinstance(scope, dict):
            return None
        return cls(scope=scope,
                   **{key: str(data.get(key) or "")
                      for key in required + ("binding", "note")})


@dataclass(frozen=True)
class ToolDescriptor:
    """A registered tool. Tabularium-owned; a worker never registers one.

    `operations` maps each supported operation to its EFFECT. The effect is
    declared here, deterministically, never inferred from the arguments a
    worker supplies.
    """

    name: str
    tool_class: str
    operations: dict[str, str] = field(default_factory=dict)
    description: str = ""

    def effect(self, operation: str) -> str | None:
        return self.operations.get(operation)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "tool_class": self.tool_class,
                "operations": dict(self.operations),
                "description": self.description}


@dataclass(frozen=True)
class CheckResult:
    """One gate check, explicitly evaluated."""

    name: str
    passed: bool
    reason_code: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ToolGateEvaluation:
    """The verdict of the Tabularium tool gate. Read-only; executes nothing."""

    permitted: bool
    effect: str = ""
    authorization_required: bool = False
    checks: tuple[CheckResult, ...] = ()
    reason_codes: tuple[str, ...] = ()

    @property
    def failed(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if not c.passed)

    def to_dict(self) -> dict[str, Any]:
        return {"permitted": self.permitted, "effect": self.effect,
                "authorization_required": self.authorization_required,
                "reason_codes": list(self.reason_codes),
                "checks": [c.to_dict() for c in self.checks]}


@dataclass(frozen=True)
class ToolResult:
    """What actually happened.

    NOT Evidence. NOT project truth. Nothing here is persisted automatically:
    turning a tool outcome into a durable record would require an explicit
    M4.5 continuation proposal and its own M4.4 persistence authorization.
    """

    tool_request_id: str
    status: str
    output: str = ""
    artifacts: tuple[dict[str, str], ...] = ()
    reason_codes: tuple[str, ...] = ()
    gate: ToolGateEvaluation | None = None
    executed_by: str = ""
    diagnostics: tuple[dict[str, str], ...] = ()

    @property
    def executed(self) -> bool:
        return self.status == EXECUTED

    def to_dict(self) -> dict[str, Any]:
        return {"tool_request_id": self.tool_request_id,
                "status": self.status, "output": self.output,
                "artifacts": [dict(a) for a in self.artifacts],
                "reason_codes": list(self.reason_codes),
                "gate": self.gate.to_dict() if self.gate else None,
                "executed_by": self.executed_by,
                "diagnostics": [dict(d) for d in self.diagnostics]}
