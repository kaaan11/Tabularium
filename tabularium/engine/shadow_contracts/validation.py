"""The shadow conformance API: `validate_shadow(family, data)`.

Validation runs in two independent passes over the same input:

1. SHAPE  — the Pydantic model in `models.py`. Structured Pydantic errors
            (type and location only, never prose) are translated into Tabularium
            reason codes.
2. RULES  — the conditional and lifecycle layer in `rules.py`, run
            unconditionally so that its findings do not depend on whether the
            shape pass succeeded.

Both passes read the input; neither writes to it. The function performs no
filesystem access, no network access, no model call, and never touches
project state. It is a development and test instrument for M3, not a
production governance component.

Normative source: schemas/*.schema.yaml
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from pydantic import ValidationError

from .common import (BOOLEAN_TYPE, ENUM_INVALID, ID_INVALID,
                     INPUT_NOT_MAPPING, ID_PATTERN, OPTIONAL_STRING_TYPE,
                     REF_LIST_ENTRY_INVALID, REF_LIST_TYPE,
                     REF_SINGLE_INVALID, REQUIRED_STRINGS,
                     REQUIRED_STRING_INVALID, RISK, SCHEMA_VERSION_TYPE,
                     SCHEMA_VERSION_UNSUPPORTED, SCOPE_OBJECT_TYPE,
                     SCOPE_PLAN_TYPE, SCOPE_RISK_INVALID,
                     SCOPE_STRING_LIST_INVALID, SCOPE_UNRECOGNIZED_FIELD,
                     STRING_LIST_INVALID, UNRECOGNIZED_FIELD, ShadowResult,
                     code, is_present, nonempty_str, result)
from .models import (BOOL, ENUM, FIELD_CLASS, OPT_STR, REF1, REFN, REQ_STR,
                     SHADOW_MODEL, STR_LIST)
from .rules import RULES
from .versions import SHADOW_VERSIONS

FAMILIES = tuple(sorted(SHADOW_MODEL))

# Pydantic reports the class of a shape failure; the field's own class decides
# how that failure is named in Tabularium terms. Pydantic's message text is never
# read.
_CLASS_CODE = {
    REQ_STR: REQUIRED_STRING_INVALID,
    OPT_STR: OPTIONAL_STRING_TYPE,
    ENUM: ENUM_INVALID,
    BOOL: BOOLEAN_TYPE,
    STR_LIST: STRING_LIST_INVALID,
    REF1: REF_SINGLE_INVALID,
}

_COMPILED_ID = {family: re.compile(p) for family, p in ID_PATTERN.items()}

_SCOPE_LIST_FIELDS = ("approved_files", "approved_operations")
_SCOPE_FIELDS = _SCOPE_LIST_FIELDS + ("approved_risk", "approved_plan")


def _non_string_key_codes(data: Mapping) -> list[str]:
    """Keys that are not strings are unrecognized fields.

    YAML admits non-string mapping keys (`1: x` parses to an int key). No
    frozen contract defines such a field, and an unrecognized field is a
    contract violation rather than an extension point, so it is reported the
    same way any other unrecognized field is.

    Checked here rather than left to the shape model because Pydantic's
    extra-field handling only inspects string keys.
    """
    return [code(UNRECOGNIZED_FIELD, repr(key)) for key in data
            if not isinstance(key, str)]


def _shape_codes(family: str, data: Mapping) -> list[str]:
    """Reason codes derived from the Pydantic shape model."""
    codes: list[str] = []
    classes = FIELD_CLASS[family]
    try:
        SHADOW_MODEL[family].model_validate(dict(data))
    except ValidationError as exc:
        for err in exc.errors():
            loc = err["loc"]
            field = str(loc[0]) if loc else ""
            if err["type"] == "extra_forbidden":
                codes.append(code(UNRECOGNIZED_FIELD, field))
                continue
            field_class = classes.get(field)
            if field_class == REFN:
                # A malformed container and a malformed entry are different
                # facts: one says "this is not a reference list", the other
                # says "this list contains something that is not a canonical
                # id". Entry failures collapse to a single code regardless of
                # how many entries are malformed.
                codes.append(code(REF_LIST_TYPE if len(loc) == 1
                                  else REF_LIST_ENTRY_INVALID, field))
            elif field_class in _CLASS_CODE:
                codes.append(code(_CLASS_CODE[field_class], field))
    return codes


def _identity_codes(family: str, data: Mapping) -> list[str]:
    """schema_version and id, screened explicitly.

    Version binding is explicit and never inferred: a record declaring any
    version other than the one this shadow is bound to acquires no shadow
    semantics.
    """
    codes: list[str] = []
    version = data.get("schema_version")
    if not isinstance(version, str):
        # At the mapping level this is a type check. The frozen requirement
        # that the value be an explicitly QUOTED scalar is a YAML-node
        # property that no post-parse mapping can observe.
        codes.append(SCHEMA_VERSION_TYPE)
    elif version != SHADOW_VERSIONS[family]:
        codes.append(SCHEMA_VERSION_UNSUPPORTED)

    rid = data.get("id")
    if not isinstance(rid, str) or not _COMPILED_ID[family].match(rid):
        codes.append(ID_INVALID)
    return codes


def _required_string_codes(family: str, data: Mapping) -> list[str]:
    """Consumed requiredness: present AND a non-empty string.

    This is narrower than Pydantic requiredness, and it is deliberately not
    expressed as a Pydantic required field: an absent field, an explicit null,
    a wrong primitive, and a whitespace-only string are one fact here — the
    value needed to interpret the record is not usable.
    """
    return [code(REQUIRED_STRING_INVALID, field)
            for field in REQUIRED_STRINGS[family]
            if not nonempty_str(data.get(field))]


def _approved_scope_codes(data: Mapping) -> list[str]:
    """Task v1.10 approved_scope shape.

    Validated explicitly rather than as a nested strict model field, because
    strict mode would reject a plain mapping outright and so could not
    distinguish "not an object" from "an object with a malformed subfield".
    """
    if not is_present(data, "approved_scope"):
        return []
    scope = data["approved_scope"]
    if not isinstance(scope, dict):
        return [SCOPE_OBJECT_TYPE]

    codes = [code(SCOPE_UNRECOGNIZED_FIELD, str(key))
             for key in sorted(set(scope) - set(_SCOPE_FIELDS), key=str)]
    for field in _SCOPE_LIST_FIELDS:
        if field in scope and (
                not isinstance(scope[field], list)
                or any(not isinstance(entry, str) for entry in scope[field])):
            codes.append(code(SCOPE_STRING_LIST_INVALID, field))
    if "approved_risk" in scope and (
            not isinstance(scope["approved_risk"], str)
            or scope["approved_risk"] not in RISK):
        codes.append(SCOPE_RISK_INVALID)
    if "approved_plan" in scope and not isinstance(scope["approved_plan"],
                                                   str):
        codes.append(SCOPE_PLAN_TYPE)
    return codes


def validate_shadow(family: str, data: Any) -> ShadowResult:
    """Validate parsed frontmatter against the named shadow family.

    Returns a deterministic `ShadowResult`. The input is never mutated, and
    nothing is normalized, repaired, guessed, or migrated: a record whose
    consumed shape is not interpretable simply acquires no shadow semantics.

    Body semantics are out of scope. A Handoff or Report body is not read
    here, so a record valid under this function may still be rejected by the
    loader for a missing required body section.
    """
    if family not in SHADOW_MODEL:
        raise KeyError(f"no shadow contract is bound for family {family!r}")
    if not isinstance(data, Mapping):
        return result(family, [INPUT_NOT_MAPPING])

    codes: list[str] = []
    codes += _non_string_key_codes(data)
    codes += _identity_codes(family, data)
    codes += _required_string_codes(family, data)
    codes += _shape_codes(family, data)
    if family == "Task":
        codes += _approved_scope_codes(data)
    RULES[family](codes, data)
    return result(family, codes)
