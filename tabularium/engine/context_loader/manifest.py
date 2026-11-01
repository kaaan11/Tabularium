"""Strict Manifest v1.0 validation (F02).

`yaml.safe_load` discards scalar style, so it cannot tell `"0.1.0"` from
`0.1.0`. Manifest v1.0 requires explicitly quoted, non-empty string values, so
validation runs against the composed node graph instead: node kind, resolved
tag, and scalar style are all inspected without ever constructing Python
objects.

Composition uses SafeLoader only. Unsafe tags never instantiate anything; they
are rejected as data.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

ALLOWED_KEYS = ("manifest_version", "tabularium_ruleset", "title")
REQUIRED_KEYS = ("manifest_version", "tabularium_ruleset")
SUPPORTED_MANIFEST_VERSION = "1.0"

STR_TAG = "tag:yaml.org,2002:str"
QUOTED_STYLES = ('"', "'")


def _kind(node) -> str:
    if isinstance(node, yaml.MappingNode):
        return "mapping"
    if isinstance(node, yaml.SequenceNode):
        return "sequence"
    return "scalar"


def validate(text: str) -> tuple[dict[str, str] | None, str | None]:
    """Return (manifest, None) or (None, reason).

    Never raises for malformed input, and never constructs Python objects from
    the document.
    """
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except RecursionError:
        return None, "manifest exceeds supported YAML nesting complexity"
    except yaml.YAMLError as exc:
        return None, f"manifest is not parseable YAML ({exc.__class__.__name__})"

    if root is None:
        return None, "manifest is empty"
    if not isinstance(root, yaml.MappingNode):
        return None, f"manifest root must be a YAML mapping, found {_kind(root)}"

    seen: dict[str, Any] = {}
    for key_node, value_node in root.value:
        if not isinstance(key_node, yaml.ScalarNode) or key_node.tag != STR_TAG:
            return None, "manifest keys must be plain string scalars"
        key = key_node.value
        if key in seen:
            return None, f"manifest declares duplicate key {key!r}"
        seen[key] = value_node

    unknown = sorted(k for k in seen if k not in ALLOWED_KEYS)
    if unknown:
        return None, f"manifest has unknown key(s): {', '.join(unknown)}"
    missing = [k for k in REQUIRED_KEYS if k not in seen]
    if missing:
        return None, f"manifest missing required key(s): {', '.join(missing)}"

    out: dict[str, str] = {}
    for key in ALLOWED_KEYS:
        node = seen.get(key)
        if node is None:
            continue
        if not isinstance(node, yaml.ScalarNode):
            return None, (f"manifest {key} must be a scalar string, found "
                          f"{_kind(node)}")
        if node.tag != STR_TAG:
            short = node.tag.rsplit(":", 1)[-1]
            return None, (f"manifest {key} must be a string, found YAML type "
                          f"{short!r}; unquoted numeric, boolean, null, and "
                          f"tagged values are invalid")
        if node.style not in QUOTED_STYLES:
            return None, (f"manifest {key} must be an explicitly quoted string; "
                          f"Manifest v1.0 does not accept unquoted values")
        if not node.value.strip():
            return None, f"manifest {key} must be a non-empty string"
        out[key] = node.value

    if out["manifest_version"] != SUPPORTED_MANIFEST_VERSION:
        return None, (f"manifest_version {out['manifest_version']!r} is not "
                      f"{SUPPORTED_MANIFEST_VERSION!r}")
    return out, None


# Canonical PEP 440 only, the form Tabularium itself writes (0.3.0, 0.3.0rc1,
# 0.3.0.post1, 0.3.0.dev0, optional +local). One parser on every install:
# a lenient fallback beside packaging.version would accept or refuse the same
# manifest depending on what else happens to be installed.
_RULESET_RE = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:(?:a|b|rc)(?:0|[1-9][0-9]*))?"
    r"(?:\.post(?:0|[1-9][0-9]*))?"
    r"(?:\.dev(?:0|[1-9][0-9]*))?"
    r"(?:\+[a-z0-9]+(?:\.[a-z0-9]+)*)?"
)


def ruleset_key(value: str) -> tuple[int, int] | None:
    """(major, minor) of a canonical PEP 440 ruleset version, else None.

    None for anything non-canonical: a missing patch segment ('0.3'), extra
    release segments, 'v' prefixes, whitespace, upper case, leading zeros,
    epochs or spellings such as '0.3.0-1' and '0.3.0.RC1'.
    """
    if not isinstance(value, str):
        return None
    match = _RULESET_RE.fullmatch(value)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def ruleset_compatible(required: str, available: str) -> bool:
    """Return True if both ruleset keys are non-None and equal."""
    k_req = ruleset_key(required)
    k_avail = ruleset_key(available)
    return k_req is not None and k_avail is not None and k_req == k_avail

