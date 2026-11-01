"""Security gate and confinement validator for proposed code patches."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tabularium.engine.persistence.model import contains_secret, valid_timestamp
from .model import (
    DIFF_EMPTY,
    DIFF_SYNTAX_INVALID,
    GATE_FAILED,
    PATCH_ACTOR_REQUIRED,
    PATCH_AUTHORIZATION_MISMATCH,
    PATCH_AUTHORIZATION_REQUIRED,
    PATCH_TIMESTAMP_INVALID,
    PROTECTED_TARGET_DENIED,
    SECRET_MATERIAL_PRESENT,
    TARGET_OUTSIDE_PROJECT,
    GateConditionResult,
    PatchAuthorization,
    PatchGateEvaluation,
    PatchProposal,
)
from .parser import parse_unified_diff

# Protected path patterns that model patches must NEVER touch
PROTECTED_PREFIXES = (
    ".git/",
    "constitution/",
    # Governance records are written by Tabularium itself, never by a model patch.
    ".ai/",
)
# Repository files git reads as configuration (filters, submodules).
PROTECTED_NAMES = (
    ".git",
    ".gitattributes",
    ".gitmodules",
    # Agent configuration: a patch here widens a later worker's permissions.
    "opencode.json",
    "opencode.jsonc",
    ".opencode",
    ".claude",
    ".codex",
)

PROTECTED_EXTENSIONS = (
    ".key",
    ".pem",
    ".crt",
    ".env",
)


def evaluate_patch(
    proposal: PatchProposal,
    project_root: str | Path,
    authorization: PatchAuthorization | dict[str, Any] | None = None,
) -> PatchGateEvaluation:
    """Evaluate all security and confinement conditions for a patch proposal."""
    conditions: list[GateConditionResult] = []
    reason_codes: list[str] = []

    def _fail(name: str, code: str, detail: str) -> None:
        conditions.append(GateConditionResult(name=name, passed=False, reason_code=code, detail=detail))
        if code not in reason_codes:
            reason_codes.append(code)

    def _ok(name: str) -> None:
        conditions.append(GateConditionResult(name=name, passed=True))

    root = Path(project_root).resolve()

    # 1. Non-empty diff check
    if not proposal.raw_diff or not proposal.raw_diff.strip():
        _fail("diff_not_empty", DIFF_EMPTY, "diff content is empty")
        return PatchGateEvaluation(allowed=False, conditions=tuple(conditions), reason_codes=tuple(reason_codes))
    _ok("diff_not_empty")

    # 2. Syntax parse check
    try:
        file_patches = parse_unified_diff(proposal.raw_diff)
    except Exception as exc:
        _fail("diff_syntax", DIFF_SYNTAX_INVALID, f"diff syntax parse error: {type(exc).__name__}")
        return PatchGateEvaluation(allowed=False, conditions=tuple(conditions), reason_codes=tuple(reason_codes))

    if not file_patches:
        _fail("diff_syntax", DIFF_SYNTAX_INVALID, "no valid file patches parsed from diff")
        return PatchGateEvaluation(allowed=False, conditions=tuple(conditions), reason_codes=tuple(reason_codes))
    _ok("diff_syntax")

    # 3. Path confinement and protected targets check
    for fp in file_patches:
        norm = fp.normalized_path
        # No absolute paths or escaping relative paths
        if norm.startswith("/") or ".." in norm.split("/"):
            _fail("confinement", TARGET_OUTSIDE_PROJECT, f"path attempts traversal: {norm}")
            break
        # Only the canonical spelling is accepted: "./x", "a//b" and "x/"
        # resolve to the same file as "x" and would slip past prefix checks.
        parts = norm.split("/")
        if not norm or any(part in ("", ".") for part in parts):
            _fail("confinement", TARGET_OUTSIDE_PROJECT, f"path is not canonical: {norm}")
            break

        # Protected checks are case-insensitive: case-insensitive filesystems
        # map ".ENV" and "CONSTITUTION/" onto the protected files.
        folded = norm.casefold()
        folded_parts = folded.split("/")
        if (any(folded.startswith(p) for p in PROTECTED_PREFIXES)
                or folded + "/" in PROTECTED_PREFIXES
                or any(part in PROTECTED_NAMES for part in folded_parts)):
            _fail("protected_paths", PROTECTED_TARGET_DENIED, f"target is in protected repository path: {norm}")
            break
        if any(folded.endswith(ext) for ext in PROTECTED_EXTENSIONS) or ".env" in folded:
            _fail("protected_paths", PROTECTED_TARGET_DENIED, f"target has protected sensitive extension: {norm}")
            break

        # Canonical resolution check
        target_path = (root / norm).resolve()
        if not target_path.is_relative_to(root):
            _fail("confinement", TARGET_OUTSIDE_PROJECT, f"target resolves outside project root: {norm}")
            break
        # An internal symlink can alias a protected directory too. Reject
        # aliases instead of treating lexical confinement as sufficient.
        candidate = root / norm
        if any(p.is_symlink() for p in (candidate, *candidate.parents) if p != root and p.is_relative_to(root)):
            _fail("confinement", TARGET_OUTSIDE_PROJECT, f"symlink target path is unsupported: {norm}")
            break
    else:
        _ok("confinement")
        _ok("protected_paths")

    # 4. Secret scanning check
    for fp in file_patches:
        for added_line in fp.added_lines:
            if contains_secret(added_line):
                _fail("secret_scanner", SECRET_MATERIAL_PRESENT, f"credential pattern detected in diff addition for {fp.normalized_path}")
                break
        if SECRET_MATERIAL_PRESENT in reason_codes:
            break
    else:
        _ok("secret_scanner")

    # 5. Authorization check (if authorization is evaluated)
    if authorization is not None:
        parsed_auth = (
            authorization
            if isinstance(authorization, PatchAuthorization)
            else PatchAuthorization.from_dict(authorization)
        )
        if parsed_auth is None:
            _fail("authorized", PATCH_AUTHORIZATION_REQUIRED, "invalid authorization object")
        else:
            if not parsed_auth.authorized_by.strip():
                _fail("authorized", PATCH_ACTOR_REQUIRED, "authorized_by is required")
            elif not valid_timestamp(parsed_auth.authorized_at):
                _fail("authorized", PATCH_TIMESTAMP_INVALID, "authorized_at must be ISO 8601 with explicit offset")
            elif (parsed_auth.proposal_id != proposal.proposal_id
                  or parsed_auth.execution_id != proposal.execution_id):
                _fail("authorized", PATCH_AUTHORIZATION_MISMATCH, "authorization identity does not match proposal")
            elif parsed_auth.diff_digest != proposal.diff_digest:
                _fail("authorized", PATCH_AUTHORIZATION_MISMATCH, "authorization diff_digest does not match proposal")
            elif parsed_auth.binding and parsed_auth.binding != proposal.binding():
                _fail("authorized", PATCH_AUTHORIZATION_MISMATCH, "authorization binding does not match proposal")
            else:
                _ok("authorized")

    allowed = len(reason_codes) == 0
    return PatchGateEvaluation(
        allowed=allowed,
        conditions=tuple(conditions),
        reason_codes=tuple(reason_codes),
    )
