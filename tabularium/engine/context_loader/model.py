"""Ephemeral Context Package data model and deterministic serialization.

The package is a logical, ephemeral, derived, non-authoritative session view.
It has no canonical id, no lifecycle, no storage location, and no durable
schema. Nothing here is written to a project.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# --- taxonomy ---------------------------------------------------------------

DRIFT = "DRIFT"
CONFLICT = "CONFLICT"
AMBIGUITY = "AMBIGUITY"
WARNING = "WARNING"

CATEGORY_ORDER = {DRIFT: 0, CONFLICT: 1, AMBIGUITY: 2, WARNING: 3}

# blocking_dimension — never collapsed into one boolean, never scored
PACKAGE = "PACKAGE"
TASK_ACTIONABILITY = "TASK_ACTIONABILITY"
NEITHER = "NEITHER"

# load modes
METADATA_ONLY = "METADATA_ONLY"
FRAGMENT = "FRAGMENT"
FULL_BODY = "FULL_BODY"

LOAD_MODE_STRENGTH = {METADATA_ONLY: 0, FRAGMENT: 1, FULL_BODY: 2}

# package_status
READY = "READY"
BLOCKED = "BLOCKED"
DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"

# task_actionability
ACTIONABLE = "ACTIONABLE"
NOT_ACTIONABLE = "NOT_ACTIONABLE"
NOT_APPLICABLE = "NOT_APPLICABLE"

# entry modes
GOVERNED = "GOVERNED"
DIAGNOSTIC = "DIAGNOSTIC"

# families, in the normative reporting order
FAMILY_ORDER = ["Task", "Question", "Decision", "Investigation",
                "Evidence", "Handoff", "Report"]


@dataclass
class Diagnostic:
    """One diagnostic. No severity score, no trust score, no risk score."""

    category: str
    code: str
    sources: list[str]
    reason: str
    blocking_dimension: str
    semantic_owner: str | None = None
    field_or_fragment: str | None = None
    target: str | None = None
    wave: int | None = None

    def sort_key(self) -> tuple:
        return (CATEGORY_ORDER.get(self.category, 99), self.code,
                ",".join(self.sources), self.target or "")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "category": self.category,
            "code": self.code,
            "sources": list(self.sources),
            "semantic_owner": self.semantic_owner,
            "field_or_fragment": self.field_or_fragment,
            "target": self.target,
            "reason": self.reason,
            "wave": self.wave,
            "blocking_dimension": self.blocking_dimension,
        }
        return out


@dataclass
class SelectedItem:
    """A selected record. Selection never upgrades authority."""

    record_id: str
    family: str
    path: str | None
    load_mode: str
    selection_reasons: list[str] = field(default_factory=list)
    wave: int | None = None
    native_status: str | None = None
    schema_version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    body: str | None = None
    fragment: str | None = None
    annotations: list[str] = field(default_factory=list)

    def add_reason(self, reason: str) -> None:
        if reason not in self.selection_reasons:
            self.selection_reasons.append(reason)

    def raise_load_mode(self, mode: str) -> None:
        """An independently stronger load mode remains in force."""
        if LOAD_MODE_STRENGTH[mode] > LOAD_MODE_STRENGTH[self.load_mode]:
            self.load_mode = mode

    def annotate(self, note: str) -> None:
        if note not in self.annotations:
            self.annotations.append(note)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.record_id,
            "family": self.family,
            "path": self.path,
            "load_mode": self.load_mode,
            "selection_reasons": sorted(self.selection_reasons),
            "wave": self.wave,
            "native_status": self.native_status,
            "schema_version": self.schema_version,
            "annotations": sorted(self.annotations),
            "fragment": self.fragment,
            "metadata": self.metadata,
            "body": self.body,
        }


@dataclass
class ContextPackage:
    entry_mode: str
    package_status: str
    task_actionability: str
    project_root: str
    compatibility: dict[str, Any] = field(default_factory=dict)
    foundations: dict[str, Any] = field(default_factory=dict)
    root_task: dict[str, Any] | None = None
    selected: list[SelectedItem] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    selection_trace: list[dict[str, Any]] = field(default_factory=list)
    omissions: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[dict[str, Any]] = field(default_factory=list)
    navigation: dict[str, list[str]] = field(default_factory=dict)

    def add_diagnostic(self, diag: Diagnostic) -> None:
        self.diagnostics.append(diag)

    def add_omission(self, family: str, reason: str, count: int,
                     on_demand: bool, boundary: str | None) -> None:
        """Aggregate equivalent omissions into one entry with a summed count.

        The aggregation key is (family, reason, on_demand, traversal_boundary);
        equivalent omissions never appear as repeated count=1 rows.
        """
        key = (family, reason, on_demand, boundary)
        for existing in self.omissions:
            if existing["_key"] == key:
                existing["count"] += count
                return
        self.omissions.append({"_key": key, "family": family, "reason": reason,
                               "count": count, "on_demand": on_demand,
                               "traversal_boundary": boundary})

    def to_dict(self) -> dict[str, Any]:
        fam_rank = {f: i for i, f in enumerate(FAMILY_ORDER)}
        items = sorted(
            self.selected,
            key=lambda s: (fam_rank.get(s.family, 99), s.record_id,
                           s.fragment or ""),
        )
        diags = sorted(self.diagnostics, key=lambda d: d.sort_key())
        return {
            "entry": {
                "mode": self.entry_mode,
                "project_root": self.project_root,
                "compatibility": self.compatibility,
            },
            "package_status": self.package_status,
            "task_actionability": self.task_actionability,
            "foundations": self.foundations,
            "root_task": self.root_task,
            "selected": [i.to_dict() for i in items],
            # candidate navigation order is preserved separately, because the
            # canonical `selected` ordering deliberately carries no recency
            "navigation": {fam: list(self.navigation[fam])
                           for fam in sorted(self.navigation)},
            "diagnostics": [d.to_dict() for d in diags],
            "selection_trace": self.selection_trace,
            "omissions": [
                {k: v for k, v in o.items() if k != "_key"}
                for o in sorted(self.omissions,
                                key=lambda o: (str(o.get("family")),
                                               str(o.get("reason"))))
            ],
            "assumptions": self.assumptions,
        }

    def to_json(self, indent: int | None = 2) -> str:
        # sort_keys keeps map serialization stable for regression testing
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True,
                          ensure_ascii=False)
