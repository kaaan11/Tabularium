"""WorkerRequest -> API messages (M3.6).

THE MODEL SEES THE PROJECTED PAYLOAD, AND NOTHING ELSE.

No Context Package, no project root, no `.ai/` tree, no repository history, no
skill catalog, and no credential is serialized here. Everything already passed
governed selection and a token budget in M3.3, so this function cannot widen
context — it renders what it was handed.

The wrapper is deliberately tiny. Every character it adds is overhead measured
against the payload it wraps, and the Constitution, protocols, and M1/M2
contracts are emphatically NOT shipped on every call.
"""

from __future__ import annotations

from tabularium.engine.projection.budget import compact

# The whole system message. It states the execution boundary and nothing else:
# no authority is conferred, no governance rule is restated, and no
# provider-specific prompt template appears in the generic worker.
SYSTEM_INSTRUCTION = (
    "You are executing one bounded task for a governed system. "
    "Use only the supplied context. You have no authority to approve, "
    "decide, or record anything. State what you could not determine."
)


def render_messages(request, skill_instructions: str | None = None
                    ) -> tuple[dict[str, str], ...]:
    """Deterministic message list: one system message, one user message.

    Structure is fixed, so the same request always produces byte-identical
    messages. Nothing is reordered, sampled, or templated per provider.
    """
    parts = [f"INTENT: {request.intent}"]
    if request.task_id:
        parts.append(f"TASK: {request.task_id}")
    if request.governance:
        parts.append(f"GOVERNANCE: {compact(request.governance)}")
    if skill_instructions:
        # Only the SELECTED skill's instructions, only when a caller passed
        # them. There is no catalog, no automatic selection, and the
        # governance sidecar is not dumped into the prompt — it is control
        # plane metadata, not model input.
        parts.append("SKILL INSTRUCTIONS:")
        parts.append(skill_instructions.strip())
    parts.append("CONTEXT:")
    parts.append(_payload_text(request.payload))

    return ({"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": "\n".join(parts)})


def _payload_text(payload) -> str:
    if payload is None:
        return "{}"
    if hasattr(payload, "to_json"):
        return payload.to_json()
    return compact(payload)


def overhead(request, skill_instructions: str | None = None) -> dict[str, int]:
    """Character split of the final input: payload, skill, wrapper.

    Measured in characters here; callers convert with the M3.3 estimator, so
    no tokenizer dependency is introduced.
    """
    payload_chars = len(_payload_text(request.payload))
    skill_chars = len(skill_instructions.strip()) if skill_instructions else 0
    total = sum(len(m["content"]) for m in
                render_messages(request, skill_instructions))
    return {"payload_chars": payload_chars,
            "skill_chars": skill_chars,
            "wrapper_chars": total - payload_chars - skill_chars,
            "final_chars": total}
