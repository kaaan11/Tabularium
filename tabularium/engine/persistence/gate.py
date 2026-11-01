"""The frozen nine-condition persistence gate, as an executable evaluator.

The conditions are not
reinterpreted here, and this module adds none of its own. Every one of the
nine is represented explicitly and evaluated separately: there is no "most
conditions passed" and no best-effort path. If any required condition fails,
the write is denied.

EVALUATION IS READ-ONLY. This module opens files only to check existence and
to read a target's current content for staleness comparison. It never writes,
never creates a directory, and never mutates a project. Applying a proposal is
`writer.apply_authorized_proposal`, and it demands a passing evaluation plus a
matching authorization.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import model as M

# The nine conditions, in the order the frozen policy states them.
CONDITION_NAMES = (
    (1, "SEMANTIC_OWNER_KNOWN"),
    (2, "MATERIAL_ENOUGH_TO_RETAIN"),
    (3, "PROVENANCE_AVAILABLE"),
    (4, "EPISTEMIC_CLASS_PRESERVED"),
    (5, "AUTHORITY_APPROVAL_LIFECYCLE_SATISFIED"),
    (6, "PERSISTENCE_AUTHORIZED"),
    (7, "NO_SECRET_OR_SENSITIVE_VALUE"),
    (8, "PROJECT_LOCAL_CONFORMING_TARGET"),
    (9, "NO_DUPLICATE_CANONICAL_OWNER"),
)


def evaluate(proposal: M.PersistenceProposal, project_root: str,
             authorization: Any = None,
             used_bindings: tuple[str, ...] = ()) -> M.GateEvaluation:
    """Evaluate all nine frozen conditions. Never writes anything."""
    checks = (
        (1, lambda: _condition_1(proposal)),
        (2, lambda: _condition_2(proposal)),
        (3, lambda: _condition_3(proposal)),
        (4, lambda: _condition_4(proposal)),
        (5, lambda: _condition_5(proposal, project_root)),
        (6, lambda: _condition_6(proposal, authorization, used_bindings)),
        (7, lambda: _condition_7(proposal)),
        (8, lambda: _condition_8(proposal, project_root)),
        (9, lambda: _condition_9(proposal, project_root)),
    )
    conditions = []
    for number, check in checks:
        try:
            conditions.append(check())
        except Exception as exc:  # noqa: BLE001 - a gate FAILS CLOSED
            # An evaluator that raises would leave the caller with no verdict
            # at all. An unexpected error is therefore a failed condition, not
            # an exception, and the write is denied.
            conditions.append(_result(
                number, False, M.PERSISTENCE_GATE_FAILED,
                f"condition raised {type(exc).__name__}"))
    codes = tuple(dict.fromkeys(
        c.reason_code for c in conditions if not c.passed and c.reason_code))
    return M.GateEvaluation(eligible=all(c.passed for c in conditions),
                            conditions=tuple(conditions),
                            reason_codes=codes)


# --- the nine conditions ----------------------------------------------------


def _condition_1(proposal) -> M.ConditionResult:
    """1. Semantic owner known — one existing native owner, not generic memory.

    The family must be one of the seven frozen record families. There is no
    generic note, memory entry, or fact store to fall back to.
    """
    ok = proposal.target_family in M.FAMILY_DIRECTORY
    return _result(1, ok, M.TARGET_FAMILY_MISMATCH,
                   f"{proposal.target_family!r} is not a native record family")


def _condition_2(proposal) -> M.ConditionResult:
    """2. Material enough to retain.

    Tabularium does not judge materiality autonomously — that judgement belongs to
    whoever proposes the write, and inventing a heuristic here would be the
    autonomous durable-value classification M4.4 explicitly excludes. What is
    checked is that the caller SUPPLIED substance and a stated reason, so an
    empty or unexplained record cannot pass silently.
    """
    ok = bool(proposal.content.strip()) and bool(proposal.reason.strip())
    return _result(2, ok, M.PROPOSAL_INVALID,
                   "a proposal needs non-empty content and a stated reason")


def _condition_3(proposal) -> M.ConditionResult:
    """3. Provenance available — recorded without fabrication.

    The proposal must say where it came from: an execution, and a record id
    for the durable artifact. Nothing is invented to fill a gap.
    """
    ok = bool(proposal.execution_id.strip()) and bool(proposal.record_id.strip())
    return _result(3, ok, M.PROPOSAL_INVALID,
                   "execution_id and record_id are required provenance")


def _condition_4(proposal) -> M.ConditionResult:
    """4. Epistemic class preserved.

    The record's declared family must match the id it carries: an EVD id in a
    Report, or a Decision written into `evidence/`, would collapse the classes
    the frozen contracts keep distinct. The families themselves define the
    epistemic classes, so consistency between id, family, and directory IS the
    check.
    """
    pattern = M.FAMILY_ID_PATTERN.get(proposal.target_family)
    ok = bool(pattern and proposal.record_id and
              pattern.match(proposal.record_id))
    return _result(4, ok, M.TARGET_FAMILY_MISMATCH,
                   f"record id {proposal.record_id!r} does not belong to "
                   f"{proposal.target_family}")


def _condition_5(proposal, project_root) -> M.ConditionResult:
    """5. Authority, approval, lifecycle, and mutability rules satisfied.

    The target owner must permit the proposed content and transition. Two
    checks apply here: the content must satisfy the FROZEN record contract
    (structural validation, below), and the operation must be permitted for
    the target's current existence — CREATE must not overwrite, UPDATE must
    not invent.
    """
    if proposal.operation not in M.OPERATIONS:
        return _result(5, False, M.PROPOSAL_INVALID,
                       f"{proposal.operation!r} is not a permitted operation")

    target = _resolved_target(proposal, project_root)
    if target is None:
        return _result(5, False, M.TARGET_OUTSIDE_PROJECT,
                       "target does not resolve inside the project")

    exists = target.is_file()
    if proposal.operation == M.CREATE and exists:
        return _result(5, False, M.TARGET_ALREADY_EXISTS,
                       "CREATE never overwrites an existing record")
    if proposal.operation == M.UPDATE and not exists:
        return _result(5, False, M.TARGET_NOT_FOUND,
                       "UPDATE requires an existing record")

    valid, detail = validate_record(proposal)
    if not valid:
        return _result(5, False, M.STRUCTURAL_VALIDATION_FAILED, detail)
    return _result(5, True, "", "")


def _condition_6(proposal, authorization, used_bindings) -> M.ConditionResult:
    """6. Persistence authorized — the user explicitly authorized THIS write.

    THE CONDITION THAT MAKES THE OTHERS MATTER. Eligibility is not permission:
    `PROPOSE` means a proposal may be built and evaluated, never that it may
    be written. An M4.3 execution approval does not satisfy this either — it
    answers "may this worker run", not "may this become project truth".

    The authorization must bind to the exact proposal AND its exact content:
    the proposal id, execution id, target path, operation, and content digest
    fields must all match. An additional composite `binding` digest MAY be
    supplied; when present it must equal the proposal's canonical binding,
    and its absence does not weaken the field matches above.
    """
    if proposal.effective_persistence == M.NONE:
        return _result(6, False, M.PERSISTENCE_DISABLED,
                       "effective persistence is NONE")
    if proposal.effective_persistence != M.PROPOSE:
        return _result(6, False, M.PERSISTENCE_DISABLED,
                       f"unknown persistence level "
                       f"{proposal.effective_persistence!r}")

    parsed = (authorization
              if isinstance(authorization, M.PersistenceAuthorization)
              else M.PersistenceAuthorization.from_dict(authorization))
    if parsed is None:
        return _result(6, False, M.PERSISTENCE_AUTHORIZATION_REQUIRED,
                       "no persistence authorization was supplied")

    if not parsed.authorized_by.strip():
        return _result(6, False, M.PERSISTENCE_ACTOR_REQUIRED,
                       "authorized_by is required")
    if not M.valid_timestamp(parsed.authorized_at):
        return _result(6, False, M.PERSISTENCE_TIMESTAMP_INVALID,
                       "authorized_at must be ISO 8601 with an explicit offset")

    for field_name, expected in (("proposal_id", proposal.proposal_id),
                                 ("execution_id", proposal.execution_id),
                                 ("target_path", proposal.target_path),
                                 ("operation", proposal.operation)):
        if getattr(parsed, field_name) != expected:
            return _result(6, False, M.PERSISTENCE_AUTHORIZATION_MISMATCH,
                           f"authorization {field_name} does not match the "
                           f"proposal")

    if parsed.content_digest != proposal.content_digest:
        return _result(6, False, M.PROPOSAL_CONTENT_CHANGED,
                       "the proposal content changed after authorization")

    binding = proposal.binding()
    if parsed.binding and parsed.binding != binding:
        return _result(6, False, M.PERSISTENCE_AUTHORIZATION_MISMATCH,
                       "authorization binding does not match the proposal")
    if binding in tuple(used_bindings):
        return _result(6, False, M.AUTHORIZATION_ALREADY_USED,
                       "this authorization was already applied")
    return _result(6, True, "", "")


def _condition_7(proposal) -> M.ConditionResult:
    """7. No secret or sensitive runtime value.

    Credentials are never persisted. The proposal's content and its stated
    reason are both checked, because a leaked key in an explanation is as
    durable as one in a record body.
    """
    for text in (proposal.content, proposal.reason):
        if M.contains_secret(text):
            return _result(7, False, M.SECRET_MATERIAL_PRESENT,
                           "credential-shaped material must never be persisted")
    return _result(7, True, "", "")


def _condition_8(proposal, project_root) -> M.ConditionResult:
    """8. Project-local conforming target.

    The target must be inside the positional project's `.ai/` tree, in the
    directory its family owns, named for its record id. Confinement is
    checked by CANONICAL RESOLUTION, so `..`, absolute paths, alternate
    separators, and symlink targets are judged by where they actually land.
    """
    target = _resolved_target(proposal, project_root)
    if target is None:
        return _result(8, False, M.TARGET_OUTSIDE_PROJECT,
                       "target escapes the project .ai/ tree")

    expected_dir = M.FAMILY_DIRECTORY.get(proposal.target_family)
    if expected_dir is None:
        return _result(8, False, M.TARGET_FAMILY_MISMATCH, "unknown family")

    expected = f".ai/{expected_dir}/{proposal.record_id}.md"
    if proposal.target_path.replace("\\", "/") != expected:
        return _result(8, False, M.TARGET_FAMILY_MISMATCH,
                       f"target must be {expected}")
    return _result(8, True, "", "")


def _condition_9(proposal, project_root) -> M.ConditionResult:
    """9. No duplicate canonical owner.

    A CREATE must not produce a second durable source for a record that
    already exists. UPDATE replaces the one canonical file in place, so it
    creates no competitor — but its expected-state binding is checked here,
    because writing over state that moved would silently discard the newer
    owner's content.
    """
    target = _resolved_target(proposal, project_root)
    if target is None:
        return _result(9, False, M.TARGET_OUTSIDE_PROJECT, "unresolved target")

    if proposal.operation == M.CREATE:
        ok = not target.is_file()
        return _result(9, ok, M.TARGET_ALREADY_EXISTS,
                       "a canonical record already exists at this target")

    if proposal.expected_current_digest:
        try:
            current = M.digest(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return _result(9, False, M.TARGET_NOT_FOUND,
                           "the target could not be read for comparison")
        if current != proposal.expected_current_digest:
            return _result(9, False, M.STALE_TARGET,
                           "the target changed after the proposal was built")
    return _result(9, True, "", "")


# --- structural validation --------------------------------------------------


def validate_record(proposal) -> tuple[bool, str]:
    """Validate content against the FROZEN record contract.

    The frozen validator is the authority. The M3.2 Pydantic shadow is
    NON-NORMATIVE and is deliberately not consulted here: making the shadow
    decide a durable write would quietly promote it to a source of truth,
    which is exactly what it was built not to be.
    """
    import yaml

    from tabularium.engine.context_loader.validate import validate_consumed
    from tabularium.engine.strict_yaml import safe_load_strict

    if proposal.target_family not in M.FAMILY_DIRECTORY:
        # The frozen validator keys its tables by family and raises on an
        # unknown one. Condition 1 already rejects this, but every condition
        # is evaluated independently, so this must refuse rather than raise.
        return False, f"{proposal.target_family!r} is not a native family"

    text = proposal.content
    if not text.startswith("---"):
        return False, "record must begin with YAML frontmatter"
    parts = text.split("---", 2)
    if len(parts) < 3:
        return False, "record frontmatter is not terminated"
    try:
        frontmatter = safe_load_strict(parts[1])
    except yaml.YAMLError:
        return False, "record frontmatter is not parseable YAML"
    if not isinstance(frontmatter, dict):
        return False, "record frontmatter must be a mapping"

    errors = validate_consumed(proposal.target_family, dict(frontmatter),
                               parts[2])
    if errors:
        return False, "; ".join(errors[:3])
    if frontmatter.get("id") != proposal.record_id:
        return False, "record id does not match the proposal"
    return True, ""


# --- helpers ----------------------------------------------------------------


def _resolved_target(proposal, project_root: str) -> Path | None:
    """Canonically resolve the target, or None if it escapes the project.

    Both sides are fully resolved first, so `..`, absolute paths, alternate
    separators, and symlink or junction targets are all judged by where they
    actually land rather than how they are spelled.
    """
    try:
        ai_root = Path(project_root).resolve() / ".ai"
        ai_root_resolved = ai_root.resolve()
        candidate = (Path(project_root).resolve()
                     / proposal.target_path).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    if candidate == ai_root_resolved:
        return None
    if ai_root_resolved not in candidate.parents:
        return None
    return candidate


def resolved_target(proposal, project_root: str) -> Path | None:
    """Public form of the confinement check, for the writer."""
    return _resolved_target(proposal, project_root)


def _result(number: int, passed: bool, reason_code: str,
            detail: str) -> M.ConditionResult:
    identifier = dict(CONDITION_NAMES)[number]
    return M.ConditionResult(number=number, identifier=identifier,
                             passed=passed,
                             reason_code="" if passed else reason_code,
                             detail="" if passed else detail)
