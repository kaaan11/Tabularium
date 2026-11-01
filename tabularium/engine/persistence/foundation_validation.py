"""Trusted structural validation for the three M7.2 foundation documents.

This module intentionally accepts only primitive target and content values.
Foundation proposals are data carriers; they do not select or execute the
validation policy.
"""

from __future__ import annotations

from collections.abc import Callable

FOUNDATION_INVALID_STRUCTURE = "FOUNDATION_INVALID_STRUCTURE"
FOUNDATION_TARGET_UNSUPPORTED = "FOUNDATION_TARGET_UNSUPPORTED"


def _validate_project_foundation(content: str) -> str | None:
    required = (
        "# ",
        "**CLASSIFICATION: PROJECT CHARTER**",
        "## Purpose",
        "## Broad scope",
    )
    if any(marker not in content for marker in required):
        return FOUNDATION_INVALID_STRUCTURE
    if content.splitlines()[0].strip() == "#":
        return FOUNDATION_INVALID_STRUCTURE
    return None


def _validate_constraints_foundation(content: str) -> str | None:
    required = (
        "# Constraints",
        "**CLASSIFICATION: CURRENT PROJECT CONSTRAINT REGISTER**",
        "## Recorded project constraints",
    )
    return FOUNDATION_INVALID_STRUCTURE if any(marker not in content for marker in required) else None


def _validate_current_state_foundation(content: str) -> str | None:
    required = (
        "# Current State",
        "**CLASSIFICATION: DERIVED HUMAN-READABLE INDEX**",
        "## Bootstrap sources",
        "## Initial record state",
    )
    return FOUNDATION_INVALID_STRUCTURE if any(marker not in content for marker in required) else None


_TARGET_VALIDATORS: dict[str, Callable[[str], str | None]] = {
    ".ai/project.md": _validate_project_foundation,
    ".ai/constraints.md": _validate_constraints_foundation,
    ".ai/current-state.md": _validate_current_state_foundation,
}


def validate_foundation_document(target_path: str, content: str) -> str | None:
    """Validate exact foundation target/content using closed trusted dispatch."""
    normalized_target = str(target_path).replace("\\", "/")
    validator = _TARGET_VALIDATORS.get(normalized_target)
    if validator is None:
        return FOUNDATION_TARGET_UNSUPPORTED
    if not content.strip() or "{{" in content or "}}" in content:
        return FOUNDATION_INVALID_STRUCTURE
    return validator(content)
