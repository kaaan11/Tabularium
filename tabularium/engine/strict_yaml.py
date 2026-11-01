"""YAML loading that refuses duplicate mapping keys instead of keeping the last.

`yaml.safe_load` resolves `a: true` followed by `a: false` to `false` without a
word. For governance data that silently drops a value a reader saw, so writers
and loaders outside the context loader use this instead. The context loader
keeps its own strict loader (`context_loader.discovery`).
"""

from __future__ import annotations

from typing import Any

import yaml


class DuplicateKeyError(yaml.YAMLError):
    """A mapping declared the same key twice. A YAMLError, so callers that
    already reject unparseable YAML reject this too."""


class _StrictSafeLoader(yaml.SafeLoader):
    pass


def _mapping_without_duplicates(loader: yaml.SafeLoader, node: yaml.MappingNode,
                                deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise DuplicateKeyError(f"duplicate key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_without_duplicates)


def safe_load_strict(text: str) -> Any:
    """`yaml.safe_load`, except that a duplicate mapping key raises."""
    return yaml.load(text, Loader=_StrictSafeLoader)
