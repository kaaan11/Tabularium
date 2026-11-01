"""Post-parse shadow shape for Project Manifest v1.0.

THE BOUNDARY MATTERS HERE, so it is stated first.

Manifest v1.0 has requirements that exist only in the YAML NODE GRAPH and are
already gone by the time any mapping exists:

    * values must be EXPLICITLY QUOTED scalars — `"0.1.0"`, not `0.1.0`
    * duplicate keys must be detected, though a mapping keeps only the last
    * resolved YAML tags must be `str`, not int / float / bool / null / custom
    * the root node must be a mapping, and keys must be plain string scalars

None of that is observable post-parse, so this module does NOT reproduce it
and must never be described as validating a manifest. Those rules belong to
`tabularium/engine/context_loader/manifest.py`, which validates the composed node graph
without ever constructing Python objects.

What this module shadows is only what remains once a mapping exists:

    * the closed key set, and the required keys
    * values being non-empty strings
    * manifest_version being exactly the supported version

Consequently a manifest that this module accepts may still be rejected by the
real parser layer, and that is correct behavior, not a divergence.

"""

from __future__ import annotations

from typing import Any, Mapping

from pydantic import ValidationError

from .common import (MANIFEST_MISSING_KEY, MANIFEST_ROOT_TYPE,
                     MANIFEST_UNKNOWN_KEY, MANIFEST_VALUE_EMPTY,
                     MANIFEST_VALUE_TYPE, MANIFEST_VERSION_UNSUPPORTED,
                     ShadowResult, code, nonempty_str, result)
from .models import ShadowBase
from .versions import SHADOW_MANIFEST_VERSION

ALLOWED_KEYS = ("manifest_version", "tabularium_ruleset", "title")
REQUIRED_KEYS = ("manifest_version", "tabularium_ruleset")

FAMILY = "Manifest"


class ManifestShadow(ShadowBase):
    """Manifest v1.0 post-parse shape. Machine configuration only.

    The manifest cannot override positional project scope, the charter, Task
    state, Decisions, or Evidence. Nothing here grants it that power; this is
    a shape, not an authority.
    """

    manifest_version: str = None
    tabularium_ruleset: str = None
    title: str = None


def validate_manifest_shadow(data: Any) -> ShadowResult:
    """Validate an already-parsed manifest mapping.

    This is the POST-PARSE half of manifest validation only. See the module
    docstring for what deliberately is not checked here.
    """
    if not isinstance(data, Mapping):
        return result(FAMILY, [MANIFEST_ROOT_TYPE])

    codes: list[str] = []
    try:
        ManifestShadow.model_validate(dict(data))
    except ValidationError as exc:
        for err in exc.errors():
            field = str(err["loc"][0]) if err["loc"] else ""
            codes.append(code(MANIFEST_UNKNOWN_KEY, field)
                         if err["type"] == "extra_forbidden"
                         else code(MANIFEST_VALUE_TYPE, field))

    for key in REQUIRED_KEYS:
        if key not in data:
            codes.append(code(MANIFEST_MISSING_KEY, key))

    for key in ALLOWED_KEYS:
        if key in data and isinstance(data[key], str) \
                and not nonempty_str(data[key]):
            codes.append(code(MANIFEST_VALUE_EMPTY, key))

    if isinstance(data.get("manifest_version"), str) \
            and data["manifest_version"] != SHADOW_MANIFEST_VERSION:
        codes.append(MANIFEST_VERSION_UNSUPPORTED)

    return result(FAMILY, codes)
