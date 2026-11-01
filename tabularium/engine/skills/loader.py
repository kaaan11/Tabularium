"""Read-only skill package loader (M3.4).

Reads one local skill package directory: the Agent Skills `SKILL.md` and the
Tabularium `tabu.yaml` sidecar. It validates shape and returns a structured result.

It never modifies a skill, never installs one, never downloads one, never
executes one, and never invokes a model. There is no skill registry and no
skill runtime.

PATH CONFINEMENT

Every file read is confined to the canonically resolved skill root. A package
cannot reach outside itself through `..`, an absolute path, or a symlink or
junction, because the resolved path of anything read must still be inside the
resolved root. This is the simple containment rule, not a re-run of the M2
confinement work.

SECRETS

A sidecar key that looks like a credential makes the package malformed. It is
not ignored and not stripped: a package trying to carry a secret is a package
that should not load (immutable principle 6).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..strict_yaml import safe_load_strict

from .model import (GOVERNANCE_FIELDS, INSTRUCTIONS_FILENAME,
                    INSTRUCTIONS_UNPARSEABLE, INVALID_SKILL_NAME,
                    INVALID_SKILL_VERSION, INVALID_VALUE, MISSING_DESCRIPTION,
                    MISSING_FIELD, MISSING_INSTRUCTIONS, MISSING_SIDECAR,
                    NOT_A_SKILL_PACKAGE, OUTPUT_CONTRACTS, PATH_ESCAPE,
                    PERSISTENCE_LEVELS, SECRET_FIELD, SECRET_KEY_PATTERN,
                    SIDECAR_FIELDS, SIDECAR_FILENAME, SIDECAR_NOT_MAPPING,
                    SIDECAR_UNPARSEABLE, SIDECAR_VERSION,
                    SIDECAR_VERSION_UNSUPPORTED, SKILL_NAME_PATTERN,
                    SKILL_VERSION_PATTERN, TOOL_CLASSES, CONTEXT_CLASSES,
                    WORKER_CATEGORY_VALUES, SkillIdentity, SkillLoadResult,
                    SkillPackage, SkillSidecar, code)

LIST_FIELDS = {
    "allowed_context_classes": CONTEXT_CLASSES,
    "allowed_worker_categories": WORKER_CATEGORY_VALUES,
    "allowed_tool_classes": TOOL_CLASSES,
}


def _contained(root: Path, name: str) -> Path | None:
    """Resolve `name` under `root`, or None if it escapes.

    Canonical containment: both sides are fully resolved first, so `..`,
    absolute paths, and symlink or junction targets are all judged by where
    they actually land rather than by how they are spelled.
    """
    try:
        candidate = (root / name).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    if candidate == root or root in candidate.parents:
        return candidate
    return None


def _read(root: Path, name: str) -> tuple[str | None, str | None]:
    """(text, error_code). Never raises for ordinary filesystem trouble."""
    path = _contained(root, name)
    if path is None:
        return None, PATH_ESCAPE
    if not path.is_file():
        return None, None
    try:
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError):
        return None, None


def _frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    """Split Agent Skills YAML frontmatter from the instructional body.

    Minimal by intent: the packaging convention is a `---` delimited YAML
    block at the top of SKILL.md. No heavier dependency is taken to read a
    handful of keys.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            block = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1:])
            try:
                data = safe_load_strict(block)
            except yaml.YAMLError:
                return None, body
            return (data if isinstance(data, dict) else None), body
    return None, text


def _validate_sidecar(raw: Any, codes: list[str]) -> SkillSidecar | None:
    if not isinstance(raw, dict):
        codes.append(SIDECAR_NOT_MAPPING)
        return None

    # Secret-like keys are checked before anything else: a package carrying a
    # credential must fail loudly rather than partially validate.
    for key in sorted(str(k) for k in raw):
        if SECRET_KEY_PATTERN.search(key):
            codes.append(code(SECRET_FIELD, key))

    unknown = sorted(str(k) for k in raw if str(k) not in SIDECAR_FIELDS)
    for key in unknown:
        if not SECRET_KEY_PATTERN.search(key):
            codes.append(code("UNKNOWN_FIELD", key))
    for key in SIDECAR_FIELDS:
        if key not in raw:
            codes.append(code(MISSING_FIELD, key))

    version = raw.get("sidecar_version")
    if isinstance(version, str) and version != SIDECAR_VERSION:
        codes.append(SIDECAR_VERSION_UNSUPPORTED)
    elif not isinstance(version, str) and "sidecar_version" in raw:
        codes.append(code(INVALID_VALUE, "sidecar_version"))

    for field_name, allowed in LIST_FIELDS.items():
        if field_name not in raw:
            continue
        value = raw[field_name]
        if not isinstance(value, list) or any(not isinstance(v, str)
                                              for v in value):
            codes.append(code(INVALID_VALUE, field_name))
            continue
        for entry in value:
            if entry not in allowed:
                # An unknown worker category is refused here, which is also
                # what keeps HUMAN impossible: the vocabulary comes from M3.3,
                # where no such category exists.
                codes.append(code(INVALID_VALUE, f"{field_name}.{entry}"))

    if "approval_required" in raw and \
            not isinstance(raw["approval_required"], bool):
        # Strictly a boolean: "false" as a string must not read as false.
        codes.append(code(INVALID_VALUE, "approval_required"))

    if "output_contract" in raw and raw["output_contract"] not in OUTPUT_CONTRACTS:
        codes.append(code(INVALID_VALUE, "output_contract"))

    if "persistence" in raw and raw["persistence"] not in PERSISTENCE_LEVELS:
        codes.append(code(INVALID_VALUE, "persistence"))

    if codes:
        return None
    return SkillSidecar(
        sidecar_version=raw["sidecar_version"],
        allowed_context_classes=tuple(raw["allowed_context_classes"]),
        allowed_worker_categories=tuple(raw["allowed_worker_categories"]),
        allowed_tool_classes=tuple(raw["allowed_tool_classes"]),
        approval_required=raw["approval_required"],
        output_contract=raw["output_contract"],
        persistence=raw["persistence"])


def _validate_identity(meta: dict[str, Any] | None,
                       codes: list[str]) -> SkillIdentity | None:
    if meta is None:
        codes.append(INSTRUCTIONS_UNPARSEABLE)
        return None
    name = meta.get("name")
    version = meta.get("version")
    description = meta.get("description")
    if not isinstance(name, str) or not SKILL_NAME_PATTERN.match(name):
        codes.append(INVALID_SKILL_NAME)
    if not isinstance(version, str) or not SKILL_VERSION_PATTERN.match(version):
        codes.append(INVALID_SKILL_VERSION)
    if not isinstance(description, str) or not description.strip():
        codes.append(MISSING_DESCRIPTION)
    if codes:
        return None
    return SkillIdentity(name=name, version=version,
                         description=description.strip())


def load_skill(package_root, include_instructions: bool = False
               ) -> SkillLoadResult:
    """Load one skill package directory.

    `include_instructions` is False by default. SKILL.md is only worth its
    tokens once the skill has actually been selected, so an ordinary load
    carries identity and governance and leaves the instructional body on disk.
    """
    try:
        root = Path(package_root).resolve()
    except (OSError, ValueError, RuntimeError):
        return SkillLoadResult(valid=False, reason_codes=(NOT_A_SKILL_PACKAGE,))
    if not root.is_dir():
        return SkillLoadResult(valid=False, reason_codes=(NOT_A_SKILL_PACKAGE,))

    codes: list[str] = []

    instructions_text, error = _read(root, INSTRUCTIONS_FILENAME)
    if error:
        codes.append(error)
    elif instructions_text is None:
        codes.append(MISSING_INSTRUCTIONS)

    sidecar_text, error = _read(root, SIDECAR_FILENAME)
    if error and error not in codes:
        codes.append(error)
    elif sidecar_text is None and not error:
        codes.append(MISSING_SIDECAR)

    if codes:
        return SkillLoadResult(valid=False, reason_codes=tuple(sorted(set(codes))))

    try:
        raw_sidecar = safe_load_strict(sidecar_text)
    except yaml.YAMLError:
        return SkillLoadResult(valid=False, reason_codes=(SIDECAR_UNPARSEABLE,))

    meta, body = _frontmatter(instructions_text)
    identity = _validate_identity(meta, codes)
    sidecar = _validate_sidecar(raw_sidecar, codes)

    if codes or identity is None or sidecar is None:
        return SkillLoadResult(valid=False,
                               reason_codes=tuple(sorted(set(codes))))

    return SkillLoadResult(
        valid=True,
        package=SkillPackage(
            identity=identity, sidecar=sidecar,
            instructions=body if include_instructions else None,
            path=root.name))


__all__ = ["load_skill", "GOVERNANCE_FIELDS"]
