"""Deterministic, read-only Selective Context Loader (M2.4).

It never writes, repairs, normalizes, migrates, scores, or infers authority.
It performs no LLM call, no embedding, no fuzzy match, and uses no database
or cache.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as installed_version
from pathlib import Path
from typing import Any

from . import manifest as manifest_rules
from .confinement import ProjectScope
from .discovery import (FAMILIES, FRAG_AMBIGUOUS, FRAG_MISSING, FRAG_OK,
                        TASK_STATUSES, TERMINAL_TASK_STATUSES, Repository,
                        discover, extract_section, extract_sections,
                        find_hypothesis_fragment, find_section_fragment,
                        id_family, split_fragment)
from .model import (ACTIONABLE, AMBIGUITY, BLOCKED, CONFLICT, DIAGNOSTIC,
                    DIAGNOSTIC_ONLY, DRIFT, FRAGMENT, FULL_BODY, GOVERNED,
                    METADATA_ONLY, NEITHER, NOT_ACTIONABLE, NOT_APPLICABLE,
                    PACKAGE, READY, TASK_ACTIONABILITY, WARNING,
                    ContextPackage, Diagnostic, SelectedItem)

EVIDENCE_KEEP = ["source_type", "status", "reliability", "collected_at",
                 "collected_by", "observation", "source", "source_locator"]

DEC_ID_RE = re.compile(r"\bDEC-[0-9]{4}-[0-9]{4}\b")
CITE_RE = re.compile(r"\b((?:EVD|INV)-[0-9]{4}-[0-9]{4})(#H[0-9]+)?\b")

# selection bases whose load mode is not downgraded by native Decision status
STATUS_OVERRIDING_BASES = {"CALLER_SELECTED", "CONSTRAINTS_CITATION",
                           "ARCHITECTURE_CITATION"}


def default_ai_os_root() -> Path:
    """The Tabularium installation containing this loader."""
    return Path(__file__).resolve().parents[3]


def _looks_like_source_checkout(root: Path) -> bool:
    """Recognize repository layout without treating installed data as source."""
    # site-packages can hold unrelated top-level "tests"/"examples" packages;
    # only a checkout also carries the project's pyproject.toml.
    return (root / "pyproject.toml").is_file() and all(
        (root / name).is_dir() for name in ("tabularium", "tests", "examples"))


class Loader:
    def __init__(self, project_root: Path, ai_os_root: Path | None = None,
                 recent: bool = True) -> None:
        self.project_root = Path(project_root).resolve()
        self.ai_os_root = Path(ai_os_root).resolve() if ai_os_root \
            else default_ai_os_root()
        self.recent = recent
        self.scope = ProjectScope(self.project_root)
        self.pkg: ContextPackage | None = None
        self.repo: Repository | None = None
        self.items: dict[str, SelectedItem] = {}
        self.root_task = None
        self._blocked = False
        self._task_ts_warned = False
        self._expanded: set[str] = set()
        self.explicit: dict[str, set[str]] = {f: set() for f in FAMILIES}

    # -- helpers ------------------------------------------------------------

    def diag(self, category, code, sources, reason, blocking_dimension,
             semantic_owner=None, field_or_fragment=None, target=None,
             wave=None) -> None:
        self.pkg.add_diagnostic(Diagnostic(
            category=category, code=code, sources=list(sources), reason=reason,
            blocking_dimension=blocking_dimension, semantic_owner=semantic_owner,
            field_or_fragment=field_or_fragment, target=target, wave=wave))
        if blocking_dimension == PACKAGE:
            self._blocked = True

    def omit(self, family: str, reason: str, count: int,
             on_demand: bool = False, boundary: str | None = None) -> None:
        """Aggregate equivalent omissions instead of emitting duplicates."""
        if count <= 0 and boundary is None:
            return
        self.pkg.add_omission(family, reason, count, on_demand, boundary)

    def assume(self, text: str, sources, basis: str, effect: str,
               blocking_dimension: str) -> None:
        """Record a complete, ephemeral assumption. Never partial prose."""
        self.pkg.assumptions.append({
            "text": text, "sources": sorted(sources), "basis": basis,
            "effect": effect, "blocking_dimension": blocking_dimension})

    def key(self, record_id: str, fragment: str | None = None) -> str:
        return f"{record_id}#{fragment}" if fragment else record_id

    def select(self, rec, load_mode: str, reason: str, wave: int,
               fragment: str | None = None) -> SelectedItem:
        """Select (or re-select) a record. Multiple paths -> one item."""
        k = self.key(rec.declared_id or rec.filename_id, fragment)
        item = self.items.get(k)
        if item is None:
            item = SelectedItem(
                record_id=rec.declared_id or rec.filename_id,
                family=rec.family, path=rec.rel_path, load_mode=load_mode,
                wave=wave, native_status=rec.status,
                schema_version=rec.schema_version, fragment=fragment)
            self.items[k] = item
            self.pkg.selected.append(item)
        else:
            item.raise_load_mode(load_mode)
            if wave is not None and (item.wave is None or wave < item.wave):
                item.wave = wave
        item.add_reason(reason)
        self._materialize(rec, item)
        return item

    def _materialize(self, rec, item: SelectedItem) -> None:
        """Attach metadata/body per the item's current load mode."""
        fm = rec.frontmatter
        meta = {k: v for k, v in fm.items() if k != "id"}
        if rec.family == "Evidence":
            # observation, native status and provenance stay inseparable
            meta = {k: fm.get(k, "NOT_RECORDED" if k == "reliability" else None)
                    for k in EVIDENCE_KEEP}
            meta.update({k: v for k, v in fm.items()
                         if k not in EVIDENCE_KEEP and k != "id"})
        item.metadata = meta
        if item.load_mode == FULL_BODY:
            item.body = rec.body
            if not rec.body.strip():
                self.diag(WARNING, "SELECTED_BODY_EMPTY", [item.record_id],
                          "a full-body selection resolved to an empty body",
                          NEITHER, semantic_owner=rec.family)
        elif item.load_mode == FRAGMENT and item.fragment:
            text, _ = find_hypothesis_fragment(rec.body, item.fragment)
            item.body = text
        elif item.load_mode == FRAGMENT:
            item.body = extract_section(rec.body, "Current conclusion")

    # -- entry --------------------------------------------------------------

    def _read_manifest(self) -> tuple[dict | None, str | None]:
        if self.scope.escaped("manifest.yaml"):
            return None, ("manifest.yaml resolves outside the project .ai root "
                          "and was not read")
        path = self.scope.entry("manifest.yaml")
        if path is None or not path.is_file():
            return None, "manifest absent at .ai/manifest.yaml"
        try:
            raw = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return None, "manifest is not valid UTF-8 text"
        except OSError:
            return None, "manifest unreadable"
        return manifest_rules.validate(raw)

    def _available_ruleset(self) -> str | None:
        vf = self.ai_os_root / "VERSION"
        try:
            return vf.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            # M7.1 packaging attachment: a source checkout keeps the frozen
            # repository-root lookup above; an installed wheel has no
            # repository-root VERSION file, so use the synchronized package
            # metadata instead. Explicit --ai-os-root paths retain the old
            # unavailable behavior because they are not package metadata.
            if self.ai_os_root != default_ai_os_root():
                return None
            # A source-like tree must keep the repository VERSION contract.
            # Do not let an unrelated installed distribution mask a missing
            # or damaged checkout VERSION file.
            if _looks_like_source_checkout(self.ai_os_root):
                return None
            try:
                return installed_version("tabularium-os")
            except PackageNotFoundError:
                return None

    def _enter(self) -> bool:
        manifest, err = self._read_manifest()
        available = self._available_ruleset()
        self.pkg.compatibility = {
            "ai_os_root": str(self.ai_os_root),
            "available_ruleset": available,
            "required_ruleset": manifest.get("tabularium_ruleset") if manifest else None,
            "manifest_valid": manifest is not None,
            "comparison": "MAJOR_MINOR"}
        if not self.scope.available:
            self.pkg.entry_mode = DIAGNOSTIC
            self.diag(WARNING, "PROJECT_SCOPE_UNAVAILABLE", [".ai"],
                      "the project .ai root is absent or does not resolve to a "
                      "readable directory", NEITHER, semantic_owner="Project")
            return False
        if manifest is None:
            self.pkg.entry_mode = DIAGNOSTIC
            self.diag(WARNING, "MANIFEST_ENTRY_FAILED",
                      [".ai/manifest.yaml"], err, NEITHER,
                      semantic_owner="Project Manifest")
            return False
        self.pkg.foundations["manifest"] = dict(manifest)
        if (
            available is None
            or not manifest_rules.ruleset_compatible(manifest["tabularium_ruleset"], available)
        ):
            self.pkg.entry_mode = DIAGNOSTIC
            self.diag(WARNING, "RULESET_UNAVAILABLE", [".ai/manifest.yaml"],
                      f"required ruleset {manifest['tabularium_ruleset']!r} is not "
                      f"compatible with available ruleset {available!r}; "
                      f"major.minor compatibility only",
                      NEITHER, semantic_owner="Project Manifest")
            return False
        self.pkg.compatibility["compatible"] = True
        self.pkg.entry_mode = GOVERNED
        return True

    # -- foundations --------------------------------------------------------

    def _read_foundation(self, name: str, required: bool) -> str | None:
        """Read one project document, confined to the project .ai root."""
        label = f".ai/{name}"
        if self.scope.escaped(name):
            self.diag(CONFLICT if required else WARNING,
                      "OUT_OF_SCOPE_REFERENCE", [label],
                      f"{name} resolves outside the project .ai root and was "
                      f"not read", PACKAGE if required else NEITHER,
                      semantic_owner="Project", target=label)
            return None
        path = self.scope.entry(name)
        if path is None or not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def _foundations(self, include_architecture: bool) -> None:
        for name in ("project.md", "constraints.md"):
            text = self._read_foundation(name, required=True)
            self.pkg.foundations[name] = text
            if text is None and not self.scope.escaped(name):
                self.diag(CONFLICT, "REQUIRED_FOUNDATION_UNAVAILABLE",
                          [f".ai/{name}"],
                          f"{name} is required for governed entry and is "
                          f"missing or unreadable", PACKAGE,
                          semantic_owner="Project")
        cs = self._read_foundation("current-state.md", required=False)
        self.pkg.foundations["current-state.md"] = cs
        if cs is None and not self.scope.escaped("current-state.md"):
            self.diag(WARNING, "CURRENT_STATE_ABSENT", [".ai/current-state.md"],
                      "optional derived navigation document is absent", NEITHER,
                      semantic_owner="Project")
        if include_architecture:
            arch = self._read_foundation("architecture.md", required=False)
            self.pkg.foundations["architecture.md"] = arch
            if arch is None and not self.scope.escaped("architecture.md"):
                self.diag(WARNING, "ARCHITECTURE_ABSENT", [".ai/architecture.md"],
                          "architecture was explicitly requested but is absent",
                          NEITHER, semantic_owner="Project")
        else:
            self.omit("Project", "architecture is explicit/on-demand only", 1,
                      on_demand=True)
        # manifest title is non-authoritative; charter wins in its domain
        title = self.pkg.foundations.get("manifest", {}).get("title")
        charter = self.pkg.foundations.get("project.md") or ""
        if title and charter:
            first = next((ln.lstrip("# ").strip() for ln in charter.splitlines()
                          if ln.startswith("# ")), None)
            if first and title not in first:
                self.diag(WARNING, "MANIFEST_TITLE_CHARTER_MISMATCH",
                          [".ai/manifest.yaml", ".ai/project.md"],
                          "manifest title differs from the charter title; the "
                          "charter wins within its semantic domain", NEITHER,
                          semantic_owner="Project")

    # -- foundation Decision citations -------------------------------------

    @staticmethod
    def _cited_decisions(text: str | None) -> list[str]:
        """Exact canonical Decision ids. No fuzzy or topic matching."""
        out: list[str] = []
        for m in DEC_ID_RE.finditer(text or ""):
            if m.group(0) not in out:
                out.append(m.group(0))
        return out

    def _foundation_citations(self) -> None:
        """Canonical Decision citations from the project foundations.

        `constraints.md` is always loaded, so at project entry its citations
        stay `METADATA_ONLY`: bounded orientation must not silently expand into
        Decision bodies. Under Task entry the constraints govern the Task work
        and the citation is a deterministic full-body basis.

        `architecture.md` is never part of default orientation, so when it was
        explicitly requested its citations select full bodies in both modes.

        A citation basis is not downgraded by native Decision status: full body
        is not approval, and native status is left exactly as recorded. Neither
        citation creates Decision applicability, authority, or any
        `task_actionability` effect.
        """
        task_mode = self.root_task is not None
        for doc, reason in ((".ai/constraints.md", "CONSTRAINTS_CITATION"),
                            (".ai/architecture.md", "ARCHITECTURE_CITATION")):
            key = doc.split("/")[-1]
            if key == "architecture.md" and \
                    self.pkg.foundations.get("architecture.md") is None:
                continue
            full = task_mode if key == "constraints.md" else True
            for did in self._cited_decisions(self.pkg.foundations.get(key)):
                rec = self._resolve(doc, "canonical citation", did, 1,
                                    owner="Project")
                if rec is None or rec.family != "Decision":
                    continue
                self._select_decision(rec, reason, 1, full=full)
                if reason == "ARCHITECTURE_CITATION" and \
                        rec.frontmatter.get("status") != "APPROVED":
                    self.diag(WARNING, "STALE-SUSPECTED", [doc],
                              f"architecture cites {did}, which is not an "
                              f"effective approved Decision", NEITHER,
                              semantic_owner="Decision", target=did)

    # -- discovery diagnostics ---------------------------------------------

    def _discovery_diagnostics(self) -> None:
        for family, label, reason in self.repo.escapes:
            self.diag(WARNING, "OUT_OF_SCOPE_REFERENCE", [label], reason,
                      NEITHER, semantic_owner=family, target=label)
        if self.repo.escapes:
            self.omit("all", "project-context paths resolving outside the "
                      "project .ai root are never read", len(self.repo.escapes),
                      boundary="project scope confinement")
        for family, label in sorted(self.repo.unreadable_dirs):
            self.diag(WARNING, "RECORD_DIRECTORY_UNREADABLE", [label],
                      "record directory exists but its entries cannot be "
                      "listed; records behind it cannot be discovered and "
                      "are not treated as absent", NEITHER,
                      semantic_owner=family)
        for rec in self.repo.mismatched:
            self.diag(CONFLICT, "FILENAME_ID_MISMATCH", [rec.rel_path],
                      f"filename id {rec.filename_id!r} and frontmatter id "
                      f"{rec.declared_id!r} disagree; neither identity is chosen",
                      NEITHER, semantic_owner=rec.family)
        for rid, hits in sorted(self.repo.by_id.items()):
            if len(hits) > 1:
                self.diag(AMBIGUITY, "RESOLUTION_AMBIGUOUS",
                          sorted(h.rel_path for h in hits),
                          f"canonical id {rid} is declared by multiple files; "
                          f"no winner is chosen", NEITHER,
                          semantic_owner=hits[0].family, target=rid)
        for code, label, reason, family in self.repo.diagnostics:
            self.diag(CONFLICT, code, [label], reason,
                      PACKAGE, semantic_owner=family, target=label)
        bad = [r for r in self.repo.all_records if r.malformed and not r.error_code]
        for rec in bad:
            self.diag(WARNING, "MALFORMED_RECORD_QUARANTINED", [rec.rel_path],
                      rec.malformed_reason or "malformed", NEITHER,
                      semantic_owner=rec.family)
        self.omit("all", "malformed records quarantined", len(bad))
        invalid = [r for r in self.repo.all_records
                   if not r.malformed and r.invalid_reasons]
        for rec in invalid:
            self.diag(WARNING, "INVALID_RECORD_QUARANTINED", [rec.rel_path],
                      f"consumed M1 invariants do not hold: "
                      f"{rec.defect_reason}; the record is not given current "
                      f"native semantics and is not repaired", NEITHER,
                      semantic_owner=rec.family, target=rec.declared_id)
        self.omit("all", "records failing consumed M1 validation quarantined",
                  len(invalid))

    # -- reference resolution ----------------------------------------------

    def _resolve(self, referrer, field_name: str, target: str, wave: int,
                 required: bool = False, owner: str | None = None):
        """Resolve one project-local reference. Returns a RecordFile or None."""
        base, _frag = split_fragment(target)
        owner = owner or id_family(base) or "Project"
        if "/" in target or "\\" in target or target.startswith(".") \
                or ":" in target:
            self.diag(WARNING, "OUT_OF_SCOPE_REFERENCE", [referrer],
                      "reference is a path rather than a project-local "
                      "canonical id and is never followed", NEITHER,
                      semantic_owner=owner, field_or_fragment=field_name,
                      target=target, wave=wave)
            return None
        if id_family(base) is None:
            self.diag(WARNING, "OUT_OF_SCOPE_REFERENCE", [referrer],
                      "reference is not a canonical record id", NEITHER,
                      semantic_owner=owner, field_or_fragment=field_name,
                      target=target, wave=wave)
            return None
        if self.repo.is_duplicate(base):
            self.diag(AMBIGUITY, "RESOLUTION_AMBIGUOUS", [referrer],
                      f"id {base} resolves to multiple files; no winner is "
                      f"chosen", PACKAGE if required else NEITHER,
                      semantic_owner=owner, field_or_fragment=field_name,
                      target=base, wave=wave)
            return None
        rec = self.repo.unique(base)
        if rec is None:
            self.diag(CONFLICT if required else WARNING, "MISSING_REFERENCE",
                      [referrer],
                      f"{field_name} target {target} could not be resolved",
                      PACKAGE if required else NEITHER, semantic_owner=owner,
                      field_or_fragment=field_name, target=target, wave=wave)
            return None
        if not self.repo.record_usable(rec):
            self.diag(CONFLICT if required else WARNING, "MALFORMED_REFERENCE",
                      [referrer],
                      f"{field_name} target {target} is quarantined: "
                      f"{rec.defect_reason}", PACKAGE if required else NEITHER,
                      semantic_owner=owner, field_or_fragment=field_name,
                      target=target, wave=wave)
            return None
        return rec

    # -- actionability ------------------------------------------------------

    def _actionability(self, task_rec) -> tuple[str, list[str]]:
        """The closed v1 rule. Nothing outside these nine conditions applies."""
        fm = task_rec.frontmatter
        reasons: list[str] = []
        status = fm.get("status")
        if status in {"WAITING_APPROVAL", "BLOCKED", "COMPLETED", "CANCELLED",
                      "ARCHIVED"}:
            reasons.append(f"TASK_STATUS_{status}")
        if fm.get("approval_required") is True and \
                fm.get("approval_status") != "APPROVED":
            reasons.append("APPROVAL_REQUIRED_NOT_APPROVED")
        if fm.get("approval_status") == "PENDING":
            reasons.append("APPROVAL_STATUS_PENDING")
        if fm.get("approval_status") == "REJECTED":
            reasons.append("APPROVAL_STATUS_REJECTED")
        root_id = task_rec.declared_id
        for q in self.repo.family_records("Question"):
            # only a VALID Question can satisfy the ninth condition
            if not self.repo.record_usable(q):
                continue
            if (q.frontmatter.get("task") == root_id
                    and q.frontmatter.get("status") == "OPEN"
                    and q.frontmatter.get("impact") == "BLOCKING"):
                reasons.append(f"OPEN_BLOCKING_QUESTION:{q.declared_id}")
        return (NOT_ACTIONABLE if reasons else ACTIONABLE), reasons

    # -- Decision supersession closure -------------------------------------

    @staticmethod
    def _approved_provenance(rec) -> bool:
        fm = rec.frontmatter
        return bool(fm.get("approved_by")) and bool(fm.get("approved_at"))

    def _successor_qualifies(self, rec) -> bool:
        """APPROVED, or SUPERSEDED after previously being approved."""
        st = rec.frontmatter.get("status")
        if st == "APPROVED":
            return True
        return st == "SUPERSEDED" and self._approved_provenance(rec)

    def _edge_valid(self, pred, succ, required: bool) -> bool:
        """One supersession edge pred -> succ, checked symmetrically.

        The verdict must not depend on the direction traversal entered from,
        so both endpoints are validated here regardless of who is being walked.
        """
        pid, sid = pred.declared_id, succ.declared_id
        if not self.repo.record_usable(pred) or \
                not self.repo.record_usable(succ):
            self.diag(CONFLICT, "DECISION_SUPERSESSION_INVALID_ENDPOINT",
                      sorted([pid, sid]),
                      "a supersession endpoint fails consumed M1 validation and "
                      "cannot carry effective supersession semantics",
                      PACKAGE if required else NEITHER,
                      semantic_owner="Decision", target=sid)
            return False
        forward = sid in pred.refs("superseded_by")
        backward = pid in succ.refs("supersedes")
        if not (forward and backward):
            missing = "supersedes" if forward else "superseded_by"
            self.diag(CONFLICT, "DECISION_SUPERSESSION_NON_RECIPROCAL",
                      sorted([pid, sid]),
                      f"the supersession edge {pid} -> {sid} is not reciprocal; "
                      f"the matching {missing} edge is absent and no "
                      f"reciprocity is synthesized", NEITHER,
                      semantic_owner="Decision", field_or_fragment=missing,
                      target=sid)
            return False
        if not self._successor_qualifies(succ):
            self.diag(WARNING, "DECISION_SUCCESSOR_NOT_APPROVED",
                      sorted([pid, sid]),
                      "successor lacks approval-bound status; not an effective "
                      "supersession", NEITHER, semantic_owner="Decision",
                      target=sid)
            return False
        return True

    def _closure(self, start_rec, required: bool) -> None:
        """Traverse supersedes/superseded_by to closure with a visited set."""
        visited: set[str] = set()
        stack = [(start_rec, [start_rec.declared_id])]
        while stack:
            rec, path = stack.pop()
            rid = rec.declared_id
            if rid in visited:
                continue
            visited.add(rid)

            successors = []
            for target in self.repo.successor_candidates(rid):
                nxt = self.repo.unique(target)
                if nxt is None:
                    self.diag(WARNING, "MISSING_REFERENCE", [rid],
                              f"superseded_by target {target} unresolved",
                              NEITHER, semantic_owner="Decision",
                              field_or_fragment="superseded_by", target=target)
                    continue
                if not self._edge_valid(rec, nxt, required):
                    continue
                if nxt.declared_id in path:
                    self.diag(
                        CONFLICT if required else WARNING,
                        "DECISION_SUPERSESSION_CYCLE" if required
                        else "PROJECT_DECISION_SUPERSESSION_CYCLE",
                        sorted([rid, nxt.declared_id]),
                        "supersession edges form a cycle; traversal terminated "
                        "through the visited set and nothing was repaired",
                        PACKAGE if required else NEITHER,
                        semantic_owner="Decision", target=nxt.declared_id)
                    continue
                successors.append(nxt)
            if len(successors) > 1:
                self.diag(AMBIGUITY, "DECISION_MULTIPLE_SUCCESSORS", [rid],
                          "multiple valid approved successor branches; no "
                          "winner, precedence, or singular current Decision is "
                          "derived", NEITHER, semantic_owner="Decision",
                          target=",".join(sorted(s.declared_id
                                                 for s in successors)))
            for target in self.repo.predecessor_candidates(rid):
                prev = self.repo.unique(target)
                if prev is None or prev.declared_id in visited:
                    continue
                # same edge, entered from the successor side
                if not self._edge_valid(prev, rec, required):
                    continue
                self._closure_select(prev)
                # Walking to a predecessor is reverse navigation, not a
                # directed successor step. Start a fresh directed path so the
                # same reciprocal edge is not misreported as a cycle when it
                # is encountered again in its forward direction.
                stack.append((prev, [prev.declared_id]))

            # Push directed successor walks after reverse-navigation walks so
            # LIFO traversal preserves their ancestry when a pair is declared
            # in both directions (a real directed cycle rather than the two
            # halves of one ordinary reciprocal edge).
            for nxt in successors:
                self._closure_select(nxt)
                stack.append((nxt, path + [nxt.declared_id]))

    def _closure_select(self, rec) -> None:
        """A node reached only through closure is METADATA_ONLY regardless of
        native status; an independently stronger load mode stays in force."""
        item = self.select(rec, METADATA_ONLY, "SUPERSESSION_CLOSURE", 2)
        if rec.frontmatter.get("status") == "SUPERSEDED":
            item.annotate("HISTORICAL")

    # -- project entry ------------------------------------------------------

    def _project_entry(self) -> None:
        tasks = [t for t in self.repo.family_records("Task")
                 if self.repo.record_usable(t)]
        for t in tasks:
            if t.status not in TERMINAL_TASK_STATUSES:
                self.select(t, METADATA_ONLY, "PROJECT_NONTERMINAL_TASK", 1)
        self.omit("Task", "terminal Tasks not loaded at project entry",
                  len([t for t in tasks if t.status in TERMINAL_TASK_STATUSES]))
        for q in self.repo.family_records("Question"):
            if not self.repo.record_usable(q):
                continue
            if q.frontmatter.get("status") == "OPEN" and \
                    q.frontmatter.get("impact") == "BLOCKING":
                item = self.select(q, METADATA_ONLY,
                                   "PROJECT_OPEN_BLOCKING_QUESTION", 1)
                item.metadata["question"] = q.frontmatter.get("question")
        for d in self.repo.family_records("Decision"):
            if self.repo.record_usable(d) \
                    and d.frontmatter.get("status") == "APPROVED":
                self.select(d, METADATA_ONLY, "PROJECT_APPROVED_DECISION", 1)
                self._closure(d, required=False)
        for fam in ("Evidence", "Investigation", "Handoff", "Report"):
            self.omit(fam, "not auto-loaded at project entry",
                      len(self.repo.family_records(fam)), on_demand=True)

    # -- task entry ---------------------------------------------------------

    def _resolve_root_task(self, task_id: str) -> None:
        """Wave 0: establish the root Task before any expansion happens."""
        repo = self.repo
        if repo.is_duplicate(task_id):
            self.diag(AMBIGUITY, "RESOLUTION_AMBIGUOUS",
                      sorted(r.rel_path for r in repo.by_id[task_id]),
                      f"supplied root Task id {task_id} resolves to multiple "
                      f"files; no file is selected by path order, timestamp, or "
                      f"content", PACKAGE, semantic_owner="Task", target=task_id)
            return
        rec = repo.unique(task_id)
        if rec is None:
            self.diag(CONFLICT, "ROOT_TASK_UNRESOLVED", [task_id],
                      "supplied root Task id does not resolve to a project-local "
                      "record", PACKAGE, semantic_owner="Task", target=task_id)
            return
        if rec.family != "Task":
            self.diag(CONFLICT, "ROOT_TASK_MALFORMED", [rec.rel_path],
                      "supplied id is not a Task", PACKAGE,
                      semantic_owner="Task", target=task_id)
            return
        if rec.id_mismatch():
            self.diag(CONFLICT, "FILENAME_ID_MISMATCH", [rec.rel_path],
                      "root Task filename and declared id disagree", PACKAGE,
                      semantic_owner="Task", target=task_id)
            return
        if rec.malformed:
            self.diag(CONFLICT, "ROOT_TASK_MALFORMED", [rec.rel_path],
                      rec.malformed_reason or "malformed", PACKAGE,
                      semantic_owner="Task", target=task_id)
            return
        if rec.invalid_reasons:
            self.diag(CONFLICT, "ROOT_TASK_INVALID", [rec.rel_path],
                      f"required root Task fails consumed M1 validation: "
                      f"{rec.defect_reason}", PACKAGE, semantic_owner="Task",
                      target=task_id)
            return
        self.root_task = rec
        item = self.select(rec, FULL_BODY, "ROOT_TASK", 0)
        self.pkg.root_task = item.to_dict()

    def _wave1_task(self) -> None:
        """Wave 1, run only once every Wave-0 root already exists."""
        rec, repo = self.root_task, self.repo
        task_id = rec.declared_id

        for f in ("parent_task", "related_tasks"):
            for target in rec.refs(f):
                t = self._resolve(task_id, f, target, 1, owner="Task")
                if t is not None:
                    self.select(t, METADATA_ONLY, f"TASK_{f.upper()}", 1)
        self.omit("Task", "Task-to-Task references are metadata navigation only",
                  len(rec.refs("parent_task")) + len(rec.refs("related_tasks")),
                  boundary="no recursive Task body loading")

        for target in rec.refs("questions"):
            q = self._resolve(task_id, "questions", target, 1, owner="Task")
            if q is not None:
                self._select_question(q, "TASK_QUESTIONS", 1, task_id)
        for target in rec.refs("decisions"):
            d = self._resolve(task_id, "decisions", target, 1, owner="Task")
            if d is not None:
                self._select_decision(d, "TASK_DECISIONS", 1, full=True)
        for target in rec.refs("investigations"):
            i = self._resolve(task_id, "investigations", target, 1,
                              owner="Task")
            if i is not None:
                self._select_investigation(i, "TASK_INVESTIGATIONS", 1)
        for target in rec.refs("evidence"):
            e = self._resolve(task_id, "evidence", target, 1, owner="Task")
            if e is not None:
                self._select_evidence(e, "TASK_EVIDENCE", 1)
        for f in ("context_items", "evaluations"):
            self.omit("Task", f"{f} is never automatically traversed",
                      len(rec.refs(f)), boundary="Wave stop rule")

        backlinks = {fam: repo.backlinks(fam, task_id)
                     for fam in ("Evidence", "Investigation", "Decision",
                                 "Question", "Handoff", "Report")}
        for q in backlinks["Question"]:
            self._select_question(q, "QUESTION_TASK_BACKLINK", 1, task_id)
        for d in backlinks["Decision"]:
            self._select_decision(d, "DECISION_TASK_BACKLINK", 1, full=True)
        for i in backlinks["Investigation"]:
            self._select_investigation(i, "INVESTIGATION_TASK_BACKLINK", 1)
        for e in backlinks["Evidence"]:
            self._select_evidence(e, "EVIDENCE_TASK_BACKLINK", 1)

        self._handoff_candidates(rec, backlinks["Handoff"])
        self._report_candidates(rec, backlinks["Report"])

        for d in repo.family_records("Decision"):
            if not repo.record_usable(d) \
                    or d.frontmatter.get("status") != "APPROVED":
                continue
            if self.key(d.declared_id) in self.items:
                continue
            self.select(d, METADATA_ONLY,
                        "PROJECT_APPROVED_UNSCOPED_CANDIDATE", 1)
            self._closure(d, required=False)

    # -- family selection ---------------------------------------------------

    def _select_question(self, rec, reason: str, wave: int, root_id: str):
        fm = rec.frontmatter
        assoc = fm.get("task")
        status, impact = fm.get("status"), fm.get("impact")
        if assoc == root_id and status == "OPEN" and impact == "BLOCKING":
            item = self.select(rec, FULL_BODY, reason, wave)
            item.annotate("RELEVANT")
            self.diag(WARNING, "OPEN_BLOCKING_QUESTION", [rec.declared_id],
                      "an open blocking Question is natively associated with "
                      "the root Task", TASK_ACTIONABILITY,
                      semantic_owner="Question", target=root_id)
            return item
        item = self.select(rec, METADATA_ONLY, reason, wave)
        item.metadata["question"] = fm.get("question")
        if status == "OPEN" and impact == "BLOCKING":
            if assoc is None:
                self.diag(WARNING, "QUESTION_TASK_ASSOCIATION_ABSENT",
                          [rec.declared_id], "open blocking Question selected "
                          "only through Task.questions has no native task "
                          "association", NEITHER, semantic_owner="Question")
            elif assoc != root_id:
                self.diag(WARNING, "QUESTION_TASK_ASSOCIATION_MISMATCH",
                          [rec.declared_id], "open blocking Question names a "
                          "different Task", NEITHER, semantic_owner="Question",
                          target=str(assoc))
        if status == "WITHDRAWN":
            self.diag(WARNING, "WITHDRAWN_QUESTION_REFERENCE",
                      [rec.declared_id], "selected Question is WITHDRAWN; no "
                      "Task lifecycle effect", NEITHER,
                      semantic_owner="Question")
        if status == "ANSWERED":
            for f in ("answer", "answered_by", "answered_at"):
                item.metadata[f] = fm.get(f)
            item.annotate("ANSWER_IS_NOT_TRUTH_OR_APPROVAL")
        return item

    def _select_decision(self, rec, reason: str, wave: int, full: bool):
        """Load mode follows the selection basis, not the native status.

        REJECTED/WITHDRAWN Decisions stay metadata under ordinary candidate
        bases, but a stronger independent basis is never downgraded. FULL_BODY
        is not approval, and native status is reported exactly as recorded.
        """
        st = rec.frontmatter.get("status")
        mode = FULL_BODY if full else METADATA_ONLY
        if mode == FULL_BODY and st in ("REJECTED", "WITHDRAWN") \
                and reason not in STATUS_OVERRIDING_BASES:
            mode = METADATA_ONLY
        item = self.select(rec, mode, reason, wave)
        if st == "SUPERSEDED":
            item.annotate("HISTORICAL")
        self._closure(rec, required=(item.load_mode == FULL_BODY))
        return item

    def _select_investigation(self, rec, reason: str, wave: int,
                              fragment: str | None = None,
                              required: bool = False):
        if fragment:
            text, status = find_hypothesis_fragment(rec.body, fragment)
            if status != FRAG_OK:
                return None
            return self.select(rec, FRAGMENT, reason, wave, fragment=fragment)
        st = rec.frontmatter.get("status")
        if st in ("OPEN", "PAUSED"):
            item = self.select(rec, FULL_BODY, reason, wave)
        else:
            existing = self.items.get(self.key(rec.declared_id or
                                               rec.filename_id))
            if existing is not None and existing.load_mode == FULL_BODY:
                return self.select(rec, FULL_BODY, reason, wave)
            _text, status = find_section_fragment(rec.body,
                                                  "Current conclusion")
            if status != FRAG_OK:
                code = "AMBIGUOUS_FRAGMENT" if status == FRAG_AMBIGUOUS \
                    else "INVALID_FRAGMENT"
                self.diag(AMBIGUITY if status == FRAG_AMBIGUOUS else
                          (CONFLICT if required else WARNING), code,
                          [rec.declared_id],
                          "more than one genuine '## Current conclusion' "
                          "heading exists; no first match is chosen"
                          if status == FRAG_AMBIGUOUS else
                          "exact heading '## Current conclusion' does not "
                          "exist outside fenced code; no fragment is "
                          "materialized",
                          PACKAGE if required else NEITHER,
                          semantic_owner="Investigation",
                          field_or_fragment="## Current conclusion",
                          target=rec.declared_id, wave=wave)
                item = self.select(rec, METADATA_ONLY, reason, wave)
                item.annotate("CONCLUSION_FRAGMENT_UNAVAILABLE")
                return item
            item = self.select(rec, FRAGMENT, reason, wave)
            item.annotate("CONCLUSION_FRAGMENT_ONLY")
            self.omit("Investigation",
                      "terminal Investigation full body is on-demand", 1,
                      on_demand=True)
        for target in rec.refs("supporting_evidence"):
            ev = self.repo.unique(target)
            if ev is not None and self.repo.record_usable(ev) \
                    and ev.status == "RETRACTED":
                self.diag(CONFLICT, "RETRACTED_EXPLICIT_SUPPORT",
                          [rec.declared_id, ev.declared_id],
                          "Investigation.supporting_evidence names RETRACTED "
                          "Evidence; it is never presented as active support",
                          NEITHER, semantic_owner="Investigation",
                          field_or_fragment="supporting_evidence",
                          target=ev.declared_id)
        return item

    def _select_evidence(self, rec, reason: str, wave: int):
        item = self.select(rec, METADATA_ONLY, reason, wave)
        st = rec.status
        if st == "RETRACTED":
            item.annotate("RETRACTED_NEVER_ACTIVE_SUPPORT")
            self.diag(WARNING, "RETRACTED_EVIDENCE_REFERENCE",
                      [rec.declared_id], "selected Evidence is RETRACTED; it is "
                      "never presented as active support and retraction is not "
                      "propagated", NEITHER, semantic_owner="Evidence")
        elif st == "SUPERSEDED":
            item.annotate("HISTORICAL")
        item.annotate("ACTIVE_DOES_NOT_MEAN_TRUE")
        return item

    # -- Handoff / Report ---------------------------------------------------

    @staticmethod
    def instant(value) -> datetime | None:
        """Parse a timezone-aware ISO-8601 instant, normalized to UTC.

        Derived M2 navigation only. A timestamp is comparable only when it
        carries a `Z`/`UTC` designator or an explicit numeric UTC offset;
        timezone-naive and unparseable values are not comparable and no local
        timezone is ever assumed.
        """
        if not isinstance(value, str) or not value or value != value.strip():
            return None
        s = value
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        elif s.endswith(("UTC", "utc")):
            s = s[:-3].strip() + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None or dt.utcoffset() is None:
            return None
        return dt.astimezone(timezone.utc)

    def _ts(self, rec) -> datetime | None:
        return self.instant(rec.frontmatter.get("created_at"))

    def _ts_reason(self, rec) -> str:
        raw = rec.frontmatter.get("created_at")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return "created_at is absent or empty"
        if not isinstance(raw, str):
            return "created_at is not a string"
        return ("created_at is timezone-naive or not a parseable ISO-8601 "
                "instant; no local timezone is assumed")

    def _candidate_order(self, recs):
        """Comparable instants descending then id; non-comparable last by id."""
        comparable = sorted((r for r in recs if self._ts(r) is not None),
                            key=lambda r: r.declared_id)
        comparable.sort(key=lambda r: self._ts(r), reverse=True)  # stable
        other = sorted((r for r in recs if self._ts(r) is None),
                       key=lambda r: r.declared_id)
        return comparable + other

    def _candidates(self, rec, task_field_names, backlinks, family,
                    multiple_code):
        """Build the candidate set, including explicit caller selections, then
        decide diagnostics. Explicit selection is known before any warning."""
        cands: dict[str, Any] = {}
        for f in task_field_names:
            for target in rec.refs(f):
                r = self._resolve(rec.declared_id, f, target, 1, owner="Task")
                if r is not None:
                    cands[r.declared_id] = r
        for r in backlinks:
            cands.setdefault(r.declared_id, r)
        for rid in sorted(self.explicit[family]):
            r = self.repo.unique(rid)
            if r is not None and self.repo.record_usable(r):
                cands.setdefault(rid, r)

        ordered = self._candidate_order(list(cands.values()))
        if not ordered:
            return []
        for r in ordered:
            if self._ts(r) is None:
                self.diag(WARNING, "INVALID_OR_MISSING_CANDIDATE_TIMESTAMP",
                          [r.declared_id],
                          f"{self._ts_reason(r)}; no RECENT or HISTORICAL is "
                          f"inferred from it and it is ordered after comparable "
                          f"candidates by canonical id", NEITHER,
                          semantic_owner=family, field_or_fragment="created_at")
        self._warn_task_updated_at()
        explicitly_selected = bool(self.explicit[family] &
                                   {r.declared_id for r in ordered})
        if len(ordered) > 1 and not explicitly_selected:
            self.diag(WARNING, multiple_code,
                      sorted(r.declared_id for r in ordered),
                      f"multiple {family} candidates and none explicitly "
                      f"selected; no current, acceptance, authority, or "
                      f"precedence is inferred", NEITHER, semantic_owner=family)
        self.pkg.navigation[family] = [r.declared_id for r in ordered]
        return ordered

    def _warn_task_updated_at(self) -> None:
        if self.root_task is None or self._task_ts_warned:
            return
        raw = self.root_task.frontmatter.get("updated_at")
        if raw is None or self.instant(raw) is not None:
            return
        self._task_ts_warned = True
        self.diag(WARNING, "INVALID_OR_MISSING_CANDIDATE_TIMESTAMP",
                  [self.root_task.declared_id],
                  "Task.updated_at is timezone-naive or not a parseable "
                  "ISO-8601 instant; no HISTORICAL is inferred from time",
                  NEITHER, semantic_owner="Task",
                  field_or_fragment="updated_at")

    def _navigation_marks(self, item, rec, ordered, selected: bool) -> None:
        """RECENT is timestamp navigation only. Timestamp HISTORICAL needs both
        the candidate instant and Task.updated_at to be comparable, with the
        Task strictly later. An unselected older candidate is also historical
        per section 10; a selected record never is by candidate comparison."""
        ts = self._ts(rec)
        if ts is None:
            return
        if self.recent and ordered and ordered[0] is rec:
            item.annotate("RECENT")
        task_updated = self.instant(
            self.root_task.frontmatter.get("updated_at")) \
            if self.root_task else None
        if task_updated is not None and task_updated > ts:
            item.annotate("HISTORICAL")
        elif not selected and ordered and ordered[0] is not rec:
            item.annotate("HISTORICAL")

    def _handoff_candidates(self, rec, backlinks):
        ordered = self._candidates(rec, ["handoffs"], backlinks, "Handoff",
                                   "MULTIPLE_HANDOFF_CANDIDATES")
        sole = len(ordered) == 1
        for r in ordered:
            chosen = r.declared_id in self.explicit["Handoff"]
            full = sole or chosen
            item = self.select(r, FULL_BODY if full else METADATA_ONLY,
                               "CALLER_SELECTED" if chosen else
                               ("SOLE_CANDIDATE" if sole
                                else "HANDOFF_CANDIDATE"), 0 if chosen else 1)
            self._navigation_marks(item, r, ordered, full)
            self.omit("Handoff", "previous_handoff is lineage/navigation only "
                      "and is never auto-traversed",
                      len(r.refs("previous_handoff")), boundary="Wave stop rule")

    def _report_candidates(self, rec, backlinks):
        singular = rec.refs("report")
        ordered = self._candidates(rec, ["report", "reports"], backlinks,
                                   "Report", "MULTIPLE_REPORT_CANDIDATES")
        sole = len(ordered) == 1
        for r in ordered:
            chosen = r.declared_id in self.explicit["Report"]
            full = sole or chosen or (r.declared_id in singular)
            reason = ("CALLER_SELECTED" if chosen else
                      "TASK_REPORT" if r.declared_id in singular else
                      "SOLE_CANDIDATE" if sole else "REPORT_CANDIDATE")
            item = self.select(r, FULL_BODY if full else METADATA_ONLY, reason,
                               0 if chosen else 1)
            self._navigation_marks(item, r, ordered, full)
            if full and self.root_task is not None and \
                    self.root_task.frontmatter.get("status") \
                    not in TERMINAL_TASK_STATUSES:
                self.diag(WARNING, "REPORT_TASK_STATE_AUTHORITY_BOUNDARY",
                          [r.declared_id], "a Report is selected in full for a "
                          "nonterminal Task; Report has no Task lifecycle "
                          "authority and never becomes Evidence", NEITHER,
                          semantic_owner="Report",
                          target=self.root_task.declared_id)
            self.omit("Report", "previous_report is lineage/navigation only and "
                      "is never auto-traversed", len(r.refs("previous_report")),
                      boundary="Wave stop rule")

    # -- bounded expansion --------------------------------------------------

    WAVE2_ALLOWLIST = {
        "Decision": ["investigation", "evidence"],
        "Investigation": ["evidence", "supporting_evidence",
                          "contradicting_evidence", "questions", "decisions"],
        "Question": ["investigation", "decision", "evidence"],
        "Handoff": ["investigations", "decisions", "evidence", "questions"],
        "Report": ["investigations", "decisions", "evidence", "questions",
                   "handoffs"],
        "Evidence": ["related_evidence", "supersedes", "superseded_by"],
    }

    DECISION_CITATION_SECTIONS = ["Alternatives", "Objections and dissent"]

    def _decision_citations(self, rec) -> list[str]:
        """Canonical citations from the two contract-designated sections."""
        out: list[str] = []
        for heading in self.DECISION_CITATION_SECTIONS:
            for section in extract_sections(rec.body, heading):
                for m in CITE_RE.finditer(section):
                    cite = m.group(1) + (m.group(2) or "")
                    if cite not in out:
                        out.append(cite)
        return out

    def _expand(self, item: SelectedItem, ref_wave: int) -> None:
        """Follow exactly the allowlisted references of one selected record."""
        k = self.key(item.record_id, item.fragment)
        if k in self._expanded:
            return
        # Supersession closure is a separate bounded graph operation. A
        # Decision selected only as metadata never becomes a normal reference
        # expansion source; later independent FULL_BODY promotion still can.
        if item.family == "Decision" and item.load_mode != FULL_BODY:
            return
        self._expanded.add(k)
        rec = self.repo.unique(item.record_id)
        if rec is None or not self.repo.record_usable(rec):
            return

        if item.family == "Decision" and item.load_mode == FULL_BODY:
            for cite in self._decision_citations(rec):
                base, frag = split_fragment(cite)
                t = self._resolve(item.record_id, "body citation", base,
                                  ref_wave, owner="Decision")
                if t is None:
                    continue
                if t.family == "Evidence":
                    self._select_evidence(t, "DECISION_BODY_CITATION", ref_wave)
                elif frag:
                    if self._select_investigation(
                            t, "DECISION_BODY_CITATION", ref_wave,
                            fragment=frag) is None:
                        _text, status = find_hypothesis_fragment(t.body, frag)
                        self.diag(
                            AMBIGUITY if status == FRAG_AMBIGUOUS else WARNING,
                            "AMBIGUOUS_FRAGMENT" if status == FRAG_AMBIGUOUS
                            else "INVALID_FRAGMENT",
                            [item.record_id],
                            "cited hypothesis heading is duplicated"
                            if status == FRAG_AMBIGUOUS else
                            "cited hypothesis heading does not exist; no "
                            "fallback or renumber inference", NEITHER,
                            semantic_owner="Investigation",
                            field_or_fragment=f"### {frag}", target=cite,
                            wave=ref_wave)
                else:
                    self._select_investigation(t, "DECISION_BODY_CITATION",
                                               ref_wave)

        for f in self.WAVE2_ALLOWLIST[item.family]:
            for target in rec.refs(f):
                t = self._resolve(item.record_id, f, target, ref_wave,
                                  owner=item.family)
                if t is None:
                    continue
                reason = f"{item.family.upper()}_{f.upper()}"
                if item.family == "Evidence":
                    # metadata diagnostics only; never support or truth
                    self.select(t, METADATA_ONLY, reason, ref_wave)
                elif t.family == "Evidence":
                    self._select_evidence(t, reason, ref_wave)
                elif t.family == "Investigation":
                    self._select_investigation(t, reason, ref_wave)
                elif t.family == "Decision":
                    # an allowlisted reference from a selected native record is
                    # an approved full-body basis
                    self._select_decision(t, reason, ref_wave, full=True)
                elif t.family == "Question":
                    self._select_question(
                        t, reason, ref_wave,
                        self.root_task.declared_id if self.root_task else "")
                elif t.family == "Handoff":
                    self.select(t, METADATA_ONLY, reason, ref_wave)

    def _wave1_explicit_containers(self) -> None:
        """Direct allowlisted references of an explicitly selected Handoff or
        Report participate in Wave 1."""
        for item in list(self.pkg.selected):
            if item.family in ("Handoff", "Report") and item.wave == 0:
                self._expand(item, 1)

    def _wave2(self) -> None:
        for item in list(self.pkg.selected):
            if item.family in self.WAVE2_ALLOWLIST and item.wave is not None \
                    and item.wave <= 1:
                self._expand(item, 2)
        self.omit("all", "automatic traversal stops after Wave 2", 0,
                  boundary="Wave 2 stop rule; only Decision supersession "
                           "closure continues")

    # -- staleness ----------------------------------------------------------

    def _current_state_drift(self) -> None:
        text = self.pkg.foundations.get("current-state.md")
        if not text or self.root_task is None:
            return
        for line in text.splitlines():
            ids = re.findall(r"TASK-[0-9]{4}-[0-9]{4}", line)
            statuses = [s for s in TASK_STATUSES if re.search(rf"\b{s}\b", line)]
            if len(ids) != 1 or len(statuses) != 1:
                continue
            rec = self.repo.unique(ids[0])
            if rec is None or not self.repo.record_usable(rec):
                continue
            if statuses[0] != rec.status:
                self.diag(DRIFT, "CURRENT_STATE_TASK_STATUS_DRIFT",
                          [".ai/current-state.md", ids[0]],
                          f"current-state states {statuses[0]} while the native "
                          f"Task states {rec.status}; the native Task wins and "
                          f"current-state is not rewritten", NEITHER,
                          semantic_owner="Task", target=ids[0])
                self.diag(WARNING, "STALE-SUSPECTED", [".ai/current-state.md"],
                          f"derived current-state disagrees with native Task "
                          f"{ids[0]}", NEITHER, semantic_owner="Project",
                          target=ids[0])

    # -- explicit selections (Wave 0) ---------------------------------------

    def _explicit(self, selectors: list[str]) -> None:
        for sel in selectors:
            base, frag = split_fragment(sel)
            fam = id_family(base)
            if fam is None:
                self.diag(CONFLICT, "INVALID_EXPLICIT_SELECTION", [sel],
                          "explicit selection is not a canonical project-local "
                          "id or INV fragment", PACKAGE,
                          semantic_owner="Project", target=sel)
                continue
            if self.repo.is_duplicate(base):
                self.diag(AMBIGUITY, "RESOLUTION_AMBIGUOUS",
                          sorted(r.rel_path for r in self.repo.by_id[base]),
                          "explicitly required id resolves to multiple files",
                          PACKAGE, semantic_owner=fam, target=base)
                continue
            rec = self.repo.unique(base)
            if rec is None:
                self.diag(CONFLICT, "MISSING_REFERENCE", [sel],
                          "explicitly required record could not be resolved",
                          PACKAGE, semantic_owner=fam, target=base)
                continue
            if not self.repo.record_usable(rec):
                self.diag(CONFLICT, "MALFORMED_REFERENCE", [sel],
                          f"explicitly required record is quarantined: "
                          f"{rec.defect_reason}", PACKAGE, semantic_owner=fam,
                          target=base)
                continue
            self.explicit[fam].add(base)
            if frag:
                _text, status = find_hypothesis_fragment(rec.body, frag)
                if status != FRAG_OK:
                    self.diag(
                        AMBIGUITY if status == FRAG_AMBIGUOUS else CONFLICT,
                        "AMBIGUOUS_FRAGMENT" if status == FRAG_AMBIGUOUS
                        else "INVALID_FRAGMENT", [base],
                        f"more than one genuine '### {frag}' heading exists; no "
                        f"first match is chosen" if status == FRAG_AMBIGUOUS
                        else f"exact heading '### {frag}' does not exist; no "
                             f"fallback, normalization, renumber inference, or "
                             f"fuzzy match is performed",
                        PACKAGE, semantic_owner="Investigation",
                        field_or_fragment=f"### {frag}", target=sel)
                    self.select(rec, METADATA_ONLY, "CALLER_SELECTED_PARENT", 0)
                    continue
                self.select(rec, FRAGMENT, "CALLER_SELECTED", 0, fragment=frag)
                continue
            if rec.family in ("Handoff", "Report"):
                # candidate machinery selects it in full during Wave 1
                self.select(rec, FULL_BODY, "CALLER_SELECTED", 0)
            elif rec.family == "Decision":
                self._select_decision(rec, "CALLER_SELECTED", 0, full=True)
            else:
                self.select(rec, FULL_BODY, "CALLER_SELECTED", 0)
            assoc = rec.frontmatter.get("task")
            if self.root_task is not None and assoc is not None and \
                    assoc != self.root_task.declared_id:
                self.diag(WARNING, "EXPLICIT_SELECTION_TASK_MISMATCH", [base],
                          "explicitly selected record names a different Task",
                          NEITHER, semantic_owner=rec.family, target=str(assoc))

    # -- entry point --------------------------------------------------------

    def run(self, task_id: str | None = None,
            selections: list[str] | None = None,
            include_architecture: bool = False) -> ContextPackage:
        selections = selections or []
        self.pkg = ContextPackage(entry_mode=DIAGNOSTIC,
                                  package_status=DIAGNOSTIC_ONLY,
                                  task_actionability=NOT_APPLICABLE,
                                  project_root=str(self.project_root))
        # 1. governed entry
        if not self._enter():
            self.pkg.selection_trace.append(
                {"step": "entry", "result": "DIAGNOSTIC",
                 "detail": "manifest, scope, or ruleset boundary failure; no "
                           "governed selection, authority, or Task semantics "
                           "applied"})
            return self.pkg

        self.pkg.package_status = READY
        # 2. project foundations, including explicit architecture
        self._foundations(include_architecture)
        # 3. discovery
        self.repo = discover(self.project_root, self.scope)
        self._discovery_diagnostics()
        self.pkg.selection_trace.append(
            {"step": "discovery", "records": len(self.repo.all_records),
             "escapes": len(self.repo.escapes), "families": sorted(FAMILIES)})

        # 4. Wave 0: root Task, then every explicit caller selection
        if task_id:
            self._resolve_root_task(task_id)
        self._explicit(selections)
        # 5. Wave-0 derived roots: foundation Decision citations
        self._foundation_citations()
        self.pkg.selection_trace.append(
            {"step": "wave_0", "root_task": task_id,
             "explicit": sorted(s for f in self.explicit
                                for s in self.explicit[f]),
             "detail": "all Wave-0 roots established before any expansion"})

        # 6. Wave 1
        if task_id:
            if self.root_task is not None:
                self._wave1_task()
        else:
            self._project_entry()
            self.pkg.selection_trace.append(
                {"step": "project_entry",
                 "detail": "no root Task supplied; orientation only. No Task, "
                           "approval, scope, or execution permission invented"})
        self._wave1_explicit_containers()
        self.pkg.selection_trace.append({"step": "wave_1", "selected":
                                         len(self.pkg.selected)})

        # 7. Wave 2, then the hard stop
        self._wave2()
        self.pkg.selection_trace.append(
            {"step": "wave_2_stop", "selected": len(self.pkg.selected),
             "detail": "no Wave 3; Decision supersession closure is the only "
                       "continuing traversal"})

        if self.root_task is not None:
            self.pkg.root_task = self.items[
                self.key(self.root_task.declared_id)].to_dict()
        self._current_state_drift()

        # 8. derived dimensions, independent of each other
        if task_id:
            if self.root_task is None:
                self.pkg.task_actionability = NOT_ACTIONABLE
                self.assume(
                    "the supplied root Task could not be uniquely and validly "
                    "resolved, so its actionability cannot be assessed",
                    [task_id], "context loader rule: root "
                    "Task resolution", "task_actionability is NOT_ACTIONABLE "
                    "and no Task semantics are applied", TASK_ACTIONABILITY)
            else:
                verdict, reasons = self._actionability(self.root_task)
                self.pkg.task_actionability = verdict
                self.pkg.selection_trace.append(
                    {"step": "task_actionability", "verdict": verdict,
                     "closed_rule_reasons": reasons})
        else:
            self.pkg.task_actionability = NOT_APPLICABLE

        if self._blocked:
            self.pkg.package_status = BLOCKED
        return self.pkg


def load_context(project_root, task=None, select=None,
                 include_architecture=False, ai_os_root=None,
                 recent=True) -> ContextPackage:
    """Build an ephemeral Context Package. Read-only."""
    return Loader(Path(project_root), ai_os_root=ai_os_root,
                  recent=recent).run(task_id=task, selections=select or [],
                                     include_architecture=include_architecture)
