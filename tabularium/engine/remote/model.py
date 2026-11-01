"""Framework-neutral remote worker lifecycle contracts (M6.3)."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

from tabularium.engine.interop.model import InteropRequest, RemoteIdentity

REQUESTED = "REQUESTED"
IN_FLIGHT = "IN_FLIGHT"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
TIMEOUT = "TIMEOUT"
CANCELLED = "CANCELLED"
UNKNOWN = "UNKNOWN"

LIFECYCLE_STATUSES = (REQUESTED, IN_FLIGHT, COMPLETED, FAILED, TIMEOUT,
                      CANCELLED, UNKNOWN)

IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
PAYLOAD_MISMATCH = "PAYLOAD_MISMATCH"
DUPLICATE_RESULT = "DUPLICATE_RESULT"
LATE_RESULT = "LATE_RESULT"
REMOTE_UNAVAILABLE = "REMOTE_UNAVAILABLE"
REMOTE_TIMEOUT = "REMOTE_TIMEOUT"
REMOTE_FAILED = "REMOTE_FAILED"
REMOTE_MALFORMED = "REMOTE_MALFORMED"
REMOTE_CANCEL_REQUESTED = "REMOTE_CANCEL_REQUESTED"
REMOTE_CANCEL_CONFIRMED = "REMOTE_CANCEL_CONFIRMED"
REMOTE_CANCEL_UNKNOWN = "REMOTE_CANCEL_UNKNOWN"
REQUEST_TERMINAL = "REQUEST_TERMINAL"
CAPABILITY_NOT_TRUSTED = "CAPABILITY_NOT_TRUSTED"
LEDGER_LOCK_FAILED = "LEDGER_LOCK_FAILED"
LEDGER_WRITE_FAILED = "LEDGER_WRITE_FAILED"
LEDGER_CORRUPT = "LEDGER_CORRUPT"
LEDGER_IDENTITY_MISMATCH = "LEDGER_IDENTITY_MISMATCH"

_REDACTED_FIELDS = frozenset({
    "credentials", "credential", "authorization", "api_key", "token",
    "password", "secret", "headers",
})


def payload_digest(request: InteropRequest) -> str:
    """Digest the exact canonical projected payload, not the full context."""
    return hashlib.sha256(request.payload_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RemoteWorkerConfig:
    """Explicit remote target configuration without credentials."""

    worker_id: str
    identity: RemoteIdentity
    timeout_s: float
    required_remote_capability: str | None = None
    remote_capabilities: tuple[str, ...] = ()
    trusted_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.worker_id.strip():
            raise ValueError("worker_id must be non-empty")
        if not isinstance(self.identity, RemoteIdentity):
            raise TypeError("identity must be a RemoteIdentity")
        if not self.identity.endpoint.startswith(("http://", "https://")):
            raise ValueError("remote endpoint must be explicit HTTP or HTTPS")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive and explicit")

    @property
    def usable_capabilities(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.remote_capabilities)
                            & set(self.trusted_capabilities)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "identity": self.identity.to_dict(),
            "timeout_s": self.timeout_s,
            "required_remote_capability": self.required_remote_capability,
            "remote_capabilities": list(self.remote_capabilities),
            "trusted_capabilities": list(self.trusted_capabilities),
        }


@dataclass(frozen=True)
class RemoteEnvelope:
    """The transport envelope around the existing InteropRequest."""

    worker_id: str
    interop_request: InteropRequest
    payload_digest: str
    idempotency_key: str

    def __post_init__(self) -> None:
        if not self.worker_id.strip():
            raise ValueError("remote envelope worker_id must be non-empty")
        if not self.idempotency_key.strip():
            raise ValueError("remote envelope idempotency_key must be non-empty")
        expected = payload_digest(self.interop_request)
        if self.payload_digest != expected:
            raise ValueError("remote envelope payload digest mismatch")
        if self.idempotency_key != self.interop_request.interop_request_id:
            raise ValueError("idempotency key must bind interop request identity")

    @property
    def execution_id(self) -> str:
        return self.interop_request.execution_id

    @property
    def task_id(self) -> str:
        return self.interop_request.task_id

    @property
    def interop_request_id(self) -> str:
        return self.interop_request.interop_request_id

    @property
    def payload_chars(self) -> int:
        return self.interop_request.payload_chars

    @property
    def payload_tokens(self) -> int:
        return self.interop_request.payload_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": "tabu-remote-worker/1",
            "worker_id": self.worker_id,
            "interop_request": self.interop_request.to_dict(),
            "payload_digest": self.payload_digest,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class RemoteCancellation:
    """Describes cancellation transport state, never Tabularium authority."""

    local_requested: bool = False
    remote_request_sent: bool = False
    remote_confirmed: bool = False
    remote_unknown: bool = False
    detail: str = ""

    @property
    def accepted(self) -> bool:
        return self.local_requested and self.remote_request_sent

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RemoteLifecycleResult:
    """Tabularium lifecycle observation; it does not mutate Task semantics."""

    request_id: str
    execution_id: str
    task_id: str
    worker_id: str
    interop_request_id: str
    status: str
    worker_result: Any = None
    reason_codes: tuple[str, ...] = ()
    diagnostics: tuple[dict[str, str], ...] = ()
    payload_digest: str = ""
    remote_status: str = ""
    cancellation: RemoteCancellation | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in LIFECYCLE_STATUSES:
            raise ValueError(f"unknown remote lifecycle status: {self.status}")
        for key in _REDACTED_FIELDS:
            if key in self.metrics:
                raise ValueError("remote lifecycle metrics cannot contain credentials")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "interop_request_id": self.interop_request_id,
            "status": self.status,
            "worker_result": asdict(self.worker_result)
            if hasattr(self.worker_result, "__dataclass_fields__")
            else self.worker_result,
            "reason_codes": list(self.reason_codes),
            "diagnostics": [dict(item) for item in self.diagnostics],
            "payload_digest": self.payload_digest,
            "remote_status": self.remote_status,
            "cancellation": self.cancellation.to_dict()
            if self.cancellation else None,
            "metrics": dict(self.metrics),
        }
