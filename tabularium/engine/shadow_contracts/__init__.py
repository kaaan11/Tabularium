"""Executable SHADOW contracts for the frozen M1/M2 record families (M3.2).

NON-NORMATIVE. NON-AUTHORITATIVE. NOT USED BY THE M2 LOADER.

Normative source of truth remains, and remains only:

    schemas/*.schema.yaml          the seven frozen M1 record contracts

Production validation remains `tabularium/engine/context_loader/validate.py` and
`tabularium/engine/context_loader/manifest.py`. This package exists so that the
consumed-field behavior of those modules can be expressed declaratively and
compared automatically, making implementation drift detectable. It grants no
authority, decides nothing, and is never imported by the loader.

This package must never import from `tabularium.engine.context_loader`: the two
implementations are deliberately independent, and a shared import would make
the golden conformance comparison vacuous.

Entry point:

    from tabularium.engine.shadow_contracts import validate_shadow
    result = validate_shadow("Task", frontmatter_mapping)
    result.valid          # bool
    result.reason_codes   # tuple[str, ...], sorted, Tabularium-owned
"""

from .common import ShadowResult
from .manifest import validate_manifest_shadow
from .validation import FAMILIES, validate_shadow
from .versions import (SHADOW_MANIFEST_VERSION, SHADOW_VERSIONS,
                       shadow_version)

__all__ = ["ShadowResult", "validate_shadow", "validate_manifest_shadow",
           "FAMILIES", "SHADOW_VERSIONS", "SHADOW_MANIFEST_VERSION",
           "shadow_version"]
