"""Mappings at the Tabularium interop boundary (M6.1)."""

from __future__ import annotations

from typing import Any

from tabularium.engine.workers.model import WorkerRequest

from .model import InteropRequest, InteropResult, RemoteIdentity, restore_projected_payload


def serialize_request(request: InteropRequest) -> dict[str, Any]:
    """Produce a plain transport mapping with no authority-bearing fields."""
    return request.to_dict()


def deserialize_request(data: Any) -> InteropRequest:
    """Restore a request and its ProjectedPayload contract from plain data."""
    if not isinstance(data, dict):
        raise ValueError("interop request must be a mapping")
    target = data.get("worker_target")
    if not isinstance(target, dict):
        raise ValueError("worker_target must be a mapping")
    return InteropRequest(
        interop_request_id=str(data.get("interop_request_id") or ""),
        execution_id=str(data.get("execution_id") or ""),
        task_id=str(data.get("task_id") or ""),
        worker_target=RemoteIdentity(**{
            key: str(target.get(key) or "")
            for key in ("provider", "model", "worker", "runtime", "endpoint", "agent_id")
        }),
        projected_payload=restore_projected_payload(data.get("projected_payload")),
        requested_capability=data.get("requested_capability"),
        metadata=dict(data.get("metadata") or {}),
        timeout_s=data.get("timeout_s"),
        deadline=data.get("deadline"),
    )


def deserialize_result(data: Any) -> InteropResult:
    """Normalize a plain remote result without interpreting its authority claims."""
    if not isinstance(data, dict):
        raise ValueError("interop result must be a mapping")
    from .model import InteropArtifact, InteropError
    artifacts = []
    for raw in data.get("artifacts") or ():
        if not isinstance(raw, dict):
            raise ValueError("artifact must be a mapping")
        parts = raw.get("parts") or ()
        if not isinstance(parts, (list, tuple)) or any(not isinstance(p, dict) for p in parts):
            raise ValueError("artifact parts must be mappings")
        artifacts.append(InteropArtifact(
            artifact_id=str(raw.get("artifact_id") or ""),
            parts=tuple(dict(p) for p in parts),
            metadata=dict(raw.get("metadata") or {})))
    error = data.get("error")
    parsed_error = None
    if error is not None:
        if not isinstance(error, dict):
            raise ValueError("error must be a mapping")
        parsed_error = InteropError(code=str(error.get("code") or ""),
                                    detail=str(error.get("detail") or ""))
    return InteropResult(
        interop_request_id=str(data.get("interop_request_id") or ""),
        execution_id=str(data.get("execution_id") or ""),
        task_id=str(data.get("task_id") or ""),
        result_id=str(data.get("result_id") or ""),
        status=str(data.get("status") or ""),
        output=str(data.get("output") or ""),
        artifacts=tuple(artifacts),
        metadata=dict(data.get("metadata") or {}),
        remote_task_id=str(data.get("remote_task_id") or ""),
        remote_task_status=str(data.get("remote_task_status") or ""),
        error=parsed_error,
    )


def request_from_worker(request: WorkerRequest, identity: RemoteIdentity,
                        interop_request_id: str,
                        *, requested_capability: str | None = None,
                        timeout_s: float | None = None,
                        deadline: str | None = None) -> InteropRequest:
    """Project a WorkerRequest into transport data.

    Governance, budget, model requirements, and caller metadata are not copied
    because they are not remote execution input.  The only context crossing is
    the already projected payload.
    """
    execution_id = request.correlation.get("execution_id", "")
    task_id = request.correlation.get("task_id") or request.task_id or ""
    return InteropRequest(
        interop_request_id=interop_request_id,
        execution_id=execution_id,
        task_id=task_id,
        worker_target=identity,
        projected_payload=request.payload,
        requested_capability=requested_capability,
        metadata={"intent": request.intent},
        timeout_s=timeout_s,
        deadline=deadline,
    )
