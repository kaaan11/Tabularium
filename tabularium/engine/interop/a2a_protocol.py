"""Deterministic Agent-to-Agent (A2A 1.0) protocol mapper (DRL-019).

In accordance with Tabularium principles:
1. No external A2A SDK dependency is imported (AST isolation preserved).
2. Pure Python standard library serialization and normalization.
3. Translates Tabularium InteropRequest into A2A Task/Message structures and
   maps incoming A2A responses into Tabularium InteropResult without granting
   remote authority or bypassing local verification.
"""

from __future__ import annotations

import json
from typing import Any

from .model import (
    InteropArtifact,
    InteropError,
    InteropRequest,
    InteropResult,
    MALFORMED_RESPONSE,
    PROTOCOL_ERROR,
    REMOTE_COMPLETED,
    REMOTE_FAILED,
    REMOTE_FAILED_STATUS,
    REMOTE_INPUT_REQUIRED,
    REMOTE_WAITING,
    REMOTE_BLOCKED,
    WRONG_TASK_ID,
)


A2A_PROTOCOL_VERSION = "1.0.0"


def serialize_a2a_task_request(request: InteropRequest) -> dict[str, Any]:
    """Serialize a Tabularium InteropRequest into an A2A Task request format.

    A2A 1.0 represents work as a Task containing Messages with Parts.
    Payload sections are projected into typed text and data parts.
    """
    message_parts: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": f"Intent: {request.metadata.get('intent', 'execute')}",
        }
    ]

    for section_name, content in request.projected_payload.sections:
        message_parts.append({
            "type": "data",
            "mime_type": "application/json",
            "data": {
                "section": section_name,
                "content": content,
            }
        })

    task_body: dict[str, Any] = {
        "protocol_version": A2A_PROTOCOL_VERSION,
        "task_id": request.task_id,
        "execution_id": request.execution_id,
        "interop_request_id": request.interop_request_id,
        "target": {
            "agent_id": request.worker_target.agent_id,
            "provider": request.worker_target.provider,
            "model": request.worker_target.model,
        },
        "message": {
            "message_id": f"msg-{request.interop_request_id}",
            "role": "user",
            "parts": message_parts,
        },
        "metadata": {
            "timeout_s": request.timeout_s,
            "deadline": request.deadline,
            "requested_capability": request.requested_capability,
        },
    }
    return task_body


def _malformed(data: dict[str, Any], expected_request: InteropRequest,
               response_task_id: str, detail: str) -> InteropResult:
    """A structurally broken response. Identity always comes from the request."""
    return InteropResult(
        interop_request_id=expected_request.interop_request_id,
        execution_id=expected_request.execution_id,
        task_id=expected_request.task_id,
        result_id=str(data.get("result_id")
                      or f"res-{expected_request.interop_request_id}"),
        status=REMOTE_FAILED_STATUS,
        error=InteropError(MALFORMED_RESPONSE, detail),
        remote_task_id=response_task_id,
    )


def deserialize_a2a_task_response(
    data: Any,
    expected_request: InteropRequest,
) -> InteropResult:
    """Normalize an A2A Task response into a Tabularium InteropResult.

    Validates correlation (task_id, interop_request_id) and extracts
    text output and artifacts without granting remote authority.
    """
    if not isinstance(data, dict):
        return InteropResult(
            interop_request_id=expected_request.interop_request_id,
            execution_id=expected_request.execution_id,
            task_id=expected_request.task_id,
            result_id=f"res-{expected_request.interop_request_id}",
            status=REMOTE_FAILED_STATUS,
            error=InteropError(MALFORMED_RESPONSE, "A2A response root is not a mapping"),
        )

    response_task_id = str(data.get("task_id") or "")
    response_request_id = str(data.get("interop_request_id") or "")

    # 6.2 - Validate correlation: task_id and interop_request_id must match if present
    if response_task_id and response_task_id != expected_request.task_id:
        return InteropResult(
            interop_request_id=expected_request.interop_request_id,
            execution_id=expected_request.execution_id,
            task_id=expected_request.task_id,
            result_id=str(data.get("result_id") or f"res-{expected_request.interop_request_id}"),
            status=REMOTE_FAILED_STATUS,
            error=InteropError(WRONG_TASK_ID, f"remote result task id '{response_task_id}' does not match expected '{expected_request.task_id}'"),
            remote_task_id=response_task_id,
        )

    if response_request_id and response_request_id != expected_request.interop_request_id:
        return InteropResult(
            interop_request_id=expected_request.interop_request_id,
            execution_id=expected_request.execution_id,
            task_id=expected_request.task_id,
            result_id=str(data.get("result_id") or f"res-{expected_request.interop_request_id}"),
            status=REMOTE_FAILED_STATUS,
            error=InteropError(PROTOCOL_ERROR, f"remote interop request id '{response_request_id}' does not match expected '{expected_request.interop_request_id}'"),
            remote_task_id=response_task_id,
        )

    # 6.1 - Status mapping: missing status or unrecognized status fails closed
    status_val = data.get("status")
    if not status_val or not isinstance(status_val, str):
        return InteropResult(
            interop_request_id=expected_request.interop_request_id,
            execution_id=expected_request.execution_id,
            task_id=expected_request.task_id,
            result_id=str(data.get("result_id") or f"res-{expected_request.interop_request_id}"),
            status=REMOTE_FAILED_STATUS,
            error=InteropError(MALFORMED_RESPONSE, "response carries no recognizable A2A status"),
            remote_task_id=response_task_id,
        )

    raw_status = status_val.upper()
    if raw_status in ("COMPLETED", "SUCCESS", "DONE"):
        status = REMOTE_COMPLETED
    elif raw_status in ("INPUT_REQUIRED", "NEEDS_INPUT"):
        status = REMOTE_INPUT_REQUIRED
    elif raw_status in ("WAITING", "QUEUED", "IN_PROGRESS"):
        status = REMOTE_WAITING
    elif raw_status in ("BLOCKED", "PAUSED"):
        status = REMOTE_BLOCKED
    else:
        status = REMOTE_FAILED_STATUS

    output_texts: list[str] = []
    artifacts: list[InteropArtifact] = []

    # Parse message parts or artifact outputs
    message = data.get("message")
    if message is not None and not isinstance(message, dict):
        return _malformed(data, expected_request, response_task_id,
                          "A2A 'message' is present but is not a mapping")
    if isinstance(message, dict):
        parts = message.get("parts")
        if parts is not None and not isinstance(parts, (list, tuple)):
            return _malformed(data, expected_request, response_task_id,
                              "A2A 'message.parts' is present but is not a list")
        if isinstance(parts, (list, tuple)):
            for part in parts:
                if isinstance(part, dict):
                    if part.get("type") == "text" and "text" in part:
                        output_texts.append(str(part["text"]))
                    elif part.get("type") == "data":
                        artifacts.append(InteropArtifact(
                            artifact_id=f"art-{len(artifacts) + 1}",
                            parts=(part,),
                        ))

    # Also inspect explicit A2A artifacts list. A wrong-typed optional field is
    # a malformed response, not an absent one: silently dropping it would let a
    # broken remote report COMPLETED while its payload never arrived.
    raw_artifacts = data.get("artifacts")
    if raw_artifacts is not None and not isinstance(raw_artifacts, (list, tuple)):
        return _malformed(data, expected_request, response_task_id,
                          "A2A 'artifacts' is present but is not a list")
    if isinstance(raw_artifacts, (list, tuple)):
        for idx, art in enumerate(raw_artifacts):
            if isinstance(art, dict):
                art_id = str(art.get("artifact_id") or f"artifact-{idx + 1}")
                parts = art.get("parts")
                if isinstance(parts, (list, tuple)) and all(isinstance(p, dict) for p in parts):
                    artifacts.append(InteropArtifact(
                        artifact_id=art_id,
                        parts=tuple(dict(p) for p in parts),
                        metadata=dict(art.get("metadata") or {}),
                    ))

    output = "\n".join(output_texts) if output_texts else str(data.get("output") or "")

    error = None
    if "error" in data and isinstance(data["error"], dict):
        err_obj = data["error"]
        error = InteropError(
            code=str(err_obj.get("code") or REMOTE_FAILED),
            detail=str(err_obj.get("message") or err_obj.get("detail") or ""),
        )
        # 6.3 - An error object forces status to REMOTE_FAILED_STATUS
        status = REMOTE_FAILED_STATUS

    return InteropResult(
        interop_request_id=expected_request.interop_request_id,
        execution_id=expected_request.execution_id,
        task_id=expected_request.task_id,
        result_id=str(data.get("result_id") or f"res-{expected_request.interop_request_id}"),
        status=status,
        output=output,
        artifacts=tuple(artifacts),
        metadata=dict(data.get("metadata") or {}),
        remote_task_id=response_task_id,
        remote_task_status=raw_status,
        error=error,
    )
