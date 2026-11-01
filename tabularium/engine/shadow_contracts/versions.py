"""Explicit shadow-to-contract version binding.

Every shadow model is bound to exactly one frozen contract version. Nothing
here infers compatibility: a contract version that is not listed has no
shadow, and a shadow never speaks for a version it was not written against.

Normative source: schemas/*.schema.yaml
"""

from __future__ import annotations

# family -> the single frozen contract version this shadow reproduces
SHADOW_VERSIONS: dict[str, str] = {
    "Task": "1.10",
    "Evidence": "1.4",
    "Investigation": "1.4",
    "Decision": "1.2",
    "Question": "1.0",
    "Handoff": "1.0",
    "Report": "1.0",
}

# Manifest is not a record family; it is project configuration and is bound
# separately. See manifest.py for the parser/post-parse boundary.
SHADOW_MANIFEST_VERSION = "1.0"


def shadow_version(family: str) -> str:
    """The contract version this shadow family is bound to.

    Raises for an unknown family rather than guessing a version.
    """
    try:
        return SHADOW_VERSIONS[family]
    except KeyError:
        raise KeyError(f"no shadow contract is bound for family {family!r}")
