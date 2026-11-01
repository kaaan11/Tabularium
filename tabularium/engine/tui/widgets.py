"""Approval widgets (Faz B, DEC-2026-0008 Alternative A2).

Each widget performs one explicit human act that the CLI performs today,
through the SAME underlying functions — the TUI adds no parallel approval
path and never auto-approves:

* task approval ......... `tabularium.task.task_approve`
* execution approval .... `tabularium.execution.make_resolution` + `continue_execution`
* persistence ........... `tabularium.execution.apply_execution_report_proposal`
* onboarding ............ `tabularium.onboarding.build_authorizations` + `onboard_project`

Discipline (all enforced inside the executors, never trusted to callers):

* Typed confirmation only: the full word ``approve`` (or ``reject`` for an
  execution decision), or a SHA-256 digest prefix for persistence. Bare
  keypresses (``y``, ``a``, ``r``, empty) are refused and record nothing.
* The sheet re-displays the exact content under decision. Secret material
  is never echoed — section/target names and digests only.
* Invalid input is fail-closed: the executor returns without recording,
  writing, or resuming anything.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

CONFIRM_WORD = "approve"
DIGEST_PREFIX_LENGTH = 8
_HEX = frozenset("0123456789abcdef")


def confirm_word(text: str | None, expected: str = CONFIRM_WORD) -> bool:
    """True only for the exact confirmation word (case-insensitive)."""
    return (text or "").strip().casefold() == expected.casefold()


def confirm_decision_word(text: str | None) -> str | None:
    """Map a typed execution decision to APPROVE/REJECT, else None.

    Full words only: the CLI's ``a``/``r`` shorthands are deliberately not
    accepted here (DEC-2026-0008: no bare-keypress acceptance).
    """
    word = (text or "").strip().casefold()
    if word == "approve":
        return "APPROVE"
    if word == "reject":
        return "REJECT"
    return None


def confirm_digest_prefix(text: str | None, digest: str | None,
                          length: int = DIGEST_PREFIX_LENGTH) -> bool:
    """True only for a hex prefix of the content digest."""
    typed = (text or "").strip().lower()
    expected = (digest or "").strip().lower()
    return (
        len(typed) >= length
        and len(expected) >= length
        and expected.startswith(typed)
        and all(char in _HEX for char in typed)
    )


def render_task_approve_sheet(task_id: str, current_status: str | None,
                              actor: str) -> str:
    return (
        "Approve Task\n"
        f"  Task: {task_id}\n"
        f"  Current status: {current_status or 'unknown'}\n"
        f"  Recorded by: {actor}\n"
        f"  Type `{CONFIRM_WORD}` to record approval. Anything else aborts."
    )


def render_persistence_sheet(record_id: str, target_path: str,
                             digest: str) -> str:
    return (
        "Authorize persistence to project truth\n"
        f"  Record: {record_id}\n"
        f"  Target: {target_path}\n"
        f"  Content SHA-256 digest: {digest}\n"
        "  Type the digest prefix (first "
        f"{DIGEST_PREFIX_LENGTH}+ hex chars) to authorize. "
        "Anything else writes nothing."
    )


def summarize_request(request: object) -> str:
    """One bounded human line per available request field."""
    parts: list[str] = []
    if isinstance(request, dict):
        get = request.get
    else:
        get = lambda key: getattr(request, key, None)  # noqa: E731
    for key in ("action", "request_id", "execution_id", "task_id"):
        try:
            value = get(key)
        except (AttributeError, KeyError, TypeError):
            value = None
        if value:
            parts.append(f"{key}={value}")
    detail = get("detail") if isinstance(request, dict) else getattr(request, "detail", None)
    if isinstance(detail, dict) and detail.get("scope"):
        parts.append(f"scope={detail['scope']}")
    return "; ".join(parts) if parts else "(no request detail)"


def render_execution_sheet(summary: str, actor: str, timestamp: str) -> str:
    return (
        "Resolve pending execution approval\n"
        f"  Request: {summary}\n"
        f"  Decided by: {actor}\n"
        f"  Decided at: {timestamp}\n"
        "  Type `approve` or `reject` (full word). Anything else records nothing."
    )


def render_onboarding_sheet(targets: list[str], actor: str,
                            timestamp: str) -> str:
    lines = [
        "Authorize onboarding writes",
        *[f"  Target: {target}" for target in targets],
        f"  Authorized by: {actor}",
        f"  Authorized at: {timestamp}",
        f"  Type `{CONFIRM_WORD}` to authorize. Anything else writes nothing.",
    ]
    return "\n".join(lines)


def approve_task(raw_target, task_id: str, *, actor: str,
                 confirmation: str):
    """Record Task approval after typed confirmation. Fail-closed."""
    from tabularium import task as task_surface

    if not confirm_word(confirmation):
        return task_surface.TaskTransitionResult(
            command="task_approve", target=str(raw_target or "."),
            record_id=task_id, code="CONFIRMATION_REJECTED",
            message="Approval was not recorded: typed confirmation missing.")
    return task_surface.task_approve(raw_target, task_id, by=actor or "USER")


def probe_pending_approval(raw_target):
    """Return the pending AWAITING_APPROVAL result, or its refusal."""
    from tabularium.execution import continue_execution

    return continue_execution(raw_target)


def resolve_pending_approval(raw_target, *, decision_word: str, actor: str,
                             timestamp: str):
    """Resolve a pending approval after typed confirmation. Fail-closed."""
    from tabularium.execution import continue_execution, make_resolution

    decision = confirm_decision_word(decision_word)
    if decision is None or not (actor or "").strip() or not (timestamp or "").strip():
        return continue_execution(raw_target)
    pending = continue_execution(raw_target)
    if getattr(pending, "status", None) != "AWAITING_APPROVAL":
        return pending
    resolution = make_resolution(pending.approval_request, decision,
                                 actor.strip(), timestamp.strip())
    return continue_execution(raw_target, resolution=resolution)


def authorize_persistence(raw_target, proposal, *, confirmation: str,
                          actor: str, timestamp: str | None = None):
    """Authorize a persistence proposal after digest confirmation.

    Returns None when not authorized: nothing is written.
    """
    from tabularium.execution import apply_execution_report_proposal

    digest = getattr(proposal, "content_digest", "") or ""
    if not confirm_digest_prefix(confirmation, digest) or not (actor or "").strip():
        return None
    if not (timestamp or "").strip():
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return apply_execution_report_proposal(
        raw_target, proposal, authorized_by=actor.strip(),
        authorized_at=timestamp.strip())


def authorize_onboarding(raw_target, declarations: dict,
                         *, confirmation: str, actor: str, timestamp: str):
    """Authorize onboarding writes after typed confirmation. Fail-closed."""
    from tabularium.onboarding import build_authorizations, onboard_project

    draft = onboard_project(raw_target, declarations)
    if draft.proposal is None:
        return draft
    if not confirm_word(confirmation) or not (actor or "").strip() \
            or not (timestamp or "").strip():
        return draft
    return onboard_project(
        raw_target, declarations,
        authorizations=build_authorizations(
            draft.proposal, actor.strip(), timestamp.strip()))
