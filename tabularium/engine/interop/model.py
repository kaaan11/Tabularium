"""Tabularium-owned, transport-neutral interop data model (M6.1).

The model is intentionally independent of A2A SDK types.  It describes the
minimum information needed to cross a remote execution boundary and nothing
that could grant Tabularium authority.  In particular, a request has no approval,
tool, persistence, context-package, credential, or project-root field.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from tabularium.engine.projection.model import ProjectedPayload
from tabularium.engine.projection.projector import PAYLOAD_SECTIONS

# Transport failures are Tabularium-owned diagnostics, not remote authority.
UNAVAILABLE = "UNAVAILABLE"
TIMEOUT = "TIMEOUT"
PROTOCOL_ERROR = "PROTOCOL_ERROR"
REMOTE_FAILED = "REMOTE_FAILED"
MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
WRONG_EXECUTION_ID = "WRONG_EXECUTION_ID"
WRONG_TASK_ID = "WRONG_TASK_ID"
DUPLICATE_RESULT = "DUPLICATE_RESULT"
CAPABILITY_NOT_TRUSTED = "CAPABILITY_NOT_TRUSTED"
REMOTE_STATUS_NOT_COMPLETED = "REMOTE_STATUS_NOT_COMPLETED"

REMOTE_COMPLETED = "COMPLETED"
REMOTE_FAILED_STATUS = "FAILED"
REMOTE_WAITING = "WAITING"
REMOTE_INPUT_REQUIRED = "INPUT_REQUIRED"
REMOTE_BLOCKED = "BLOCKED"
INTEROP_RESULT_STATUSES = (
    REMOTE_COMPLETED, REMOTE_FAILED_STATUS, REMOTE_WAITING,
    REMOTE_INPUT_REQUIRED, REMOTE_BLOCKED,
)

MAX_METADATA_CHARS = 8192
MAX_ARTIFACTS = 32


def _compact_size(value: Any) -> int:
    try:
        return len(json.dumps(value, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False))
    except (TypeError, ValueError):
        return MAX_METADATA_CHARS + 1


def _require_id(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


@dataclass(frozen=True)
class RemoteIdentity:
    """Descriptive remote identity; none of these fields is authority."""

    provider: str = ""
    model: str = ""
    worker: str = ""
    runtime: str = ""
    endpoint: str = ""
    agent_id: str = ""

    def __post_init__(self) -> None:
        # Endpoint selection is descriptive configuration, not a credential
        # store. Reject URL userinfo and common credential query hints before
        # the endpoint can enter an InteropRequest or trace attribute.
        if self.endpoint:
            parsed = urlsplit(self.endpoint)
            if parsed.username or parsed.password:
                raise ValueError("remote endpoint must not contain URL credentials")
            lowered = self.endpoint.lower()
            if any(hint in lowered for hint in
                   ("token=", "api_key=", "apikey=", "secret=", "password=")):
                raise ValueError("remote endpoint must not contain credentials")

    def to_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "model": self.model,
                "worker": self.worker, "runtime": self.runtime,
                "endpoint": self.endpoint, "agent_id": self.agent_id}


@dataclass(frozen=True)
class RemoteCapabilityView:
    """Claimed capability versus Tabularium-configured usable capability."""

    claimed: tuple[str, ...] = ()
    trusted: tuple[str, ...] = ()

    @property
    def usable(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.claimed) & set(self.trusted)))

    def to_dict(self) -> dict[str, list[str]]:
        return {"claimed": list(self.claimed), "trusted": list(self.trusted),
                "usable": list(self.usable)}


@dataclass(frozen=True)
class InteropRequest:
    """Only execution-relevant data sent across the interop boundary."""

    interop_request_id: str
    execution_id: str
    task_id: str
    worker_target: RemoteIdentity
    projected_payload: ProjectedPayload
    requested_capability: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    timeout_s: float | None = None
    deadline: str | None = None

    def __post_init__(self) -> None:
        for name, value in (("interop_request_id", self.interop_request_id),
                            ("execution_id", self.execution_id),
                            ("task_id", self.task_id)):
            _require_id(name, value)
        if not isinstance(self.worker_target, RemoteIdentity):
            raise TypeError("worker_target must be a RemoteIdentity")
        if not isinstance(self.projected_payload, ProjectedPayload):
            raise TypeError(
                "interop requires a ProjectedPayload, not a raw mapping")
        if not isinstance(self.metadata, dict) or _compact_size(self.metadata) > MAX_METADATA_CHARS:
            raise ValueError("interop request metadata is invalid or oversized")
        if self.timeout_s is not None and self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive when supplied")

    @property
    def payload_json(self) -> str:
        return self.projected_payload.to_json()

    @property
    def payload_chars(self) -> int:
        return len(self.payload_json)

    @property
    def payload_tokens(self) -> int:
        # Keep the same simple estimator family as the projection layer.
        return (self.payload_chars + 3) // 4

    def to_dict(self) -> dict[str, Any]:
        # This is the transport envelope. No WorkerRequest governance or
        # model_requirements are copied here.
        return {
            "interop_request_id": self.interop_request_id,
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "worker_target": self.worker_target.to_dict(),
            "projected_payload": self.projected_payload.to_dict(),
            "requested_capability": self.requested_capability,
            "metadata": dict(self.metadata),
            "timeout_s": self.timeout_s,
            "deadline": self.deadline,
        }


@dataclass(frozen=True)
class InteropArtifact:
    """Untrusted remote output; artifact presence grants no Tabularium meaning."""

    artifact_id: str
    parts: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_id("artifact_id", self.artifact_id)
        if len(self.parts) > MAX_ARTIFACTS:
            raise ValueError("too many artifact parts")
        if _compact_size(self.parts) + _compact_size(self.metadata) > MAX_METADATA_CHARS:
            raise ValueError("interop artifact is oversized")

    def to_dict(self) -> dict[str, Any]:
        return {"artifact_id": self.artifact_id, "parts": [dict(p) for p in self.parts],
                "metadata": dict(self.metadata)}


@dataclass(frozen=True)
class InteropError:
    code: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail[:300]}


@dataclass(frozen=True)
class InteropResult:
    """Remote response before Tabularium normalizes it into WorkerResult."""

    interop_request_id: str
    execution_id: str
    task_id: str
    result_id: str
    status: str
    output: str = ""
    artifacts: tuple[InteropArtifact, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    remote_task_id: str = ""
    remote_task_status: str = REMOTE_COMPLETED
    error: InteropError | None = None

    def __post_init__(self) -> None:
        for name, value in (("interop_request_id", self.interop_request_id),
                            ("execution_id", self.execution_id),
                            ("task_id", self.task_id),
                            ("result_id", self.result_id)):
            _require_id(name, value)
        if self.status not in INTEROP_RESULT_STATUSES:
            raise ValueError("unknown remote interop status")
        if len(self.artifacts) > MAX_ARTIFACTS:
            raise ValueError("too many artifacts")
        if not isinstance(self.metadata, dict) or _compact_size(self.metadata) > MAX_METADATA_CHARS:
            raise ValueError("interop result metadata is invalid or oversized")

    def to_dict(self) -> dict[str, Any]:
        return {
            "interop_request_id": self.interop_request_id,
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "result_id": self.result_id,
            "status": self.status,
            "output": self.output,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "metadata": dict(self.metadata),
            "remote_task_id": self.remote_task_id,
            "remote_task_status": self.remote_task_status,
            "error": self.error.to_dict() if self.error else None,
        }


def restore_projected_payload(value: Any) -> ProjectedPayload:
    """Restore the declared payload contract at the serialization boundary."""
    if not isinstance(value, dict):
        raise ValueError("projected_payload must be a mapping")
    unknown = sorted(set(value) - set(PAYLOAD_SECTIONS))
    if unknown:
        raise ValueError("payload contains non-projected sections: "
                         + ", ".join(unknown))
    return ProjectedPayload(tuple((str(name), section)
                                  for name, section in value.items()))
