"""Render only the governed WorkerRequest payload for a native CLI."""

from __future__ import annotations

from tabularium.engine.projection.budget import compact


def render_prompt(request) -> str:
    payload = request.payload
    body = ("{}" if payload is None else
            payload.to_json() if hasattr(payload, "to_json") else
            compact(payload))
    lines = [f"INTENT: {request.intent}"]
    if request.task_id:
        lines.append(f"TASK: {request.task_id}")
    if request.governance:
        lines.append(f"GOVERNANCE: {compact(request.governance)}")
    lines.extend(("CONTEXT:", body))
    return "\n".join(lines) + "\n"
