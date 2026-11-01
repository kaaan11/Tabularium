"""Read-only record discovery, frontmatter parsing, and fragment extraction.

Nothing in this module writes, repairs, normalizes, or migrates anything.
Malformed records are reported, never fixed.
"""

from __future__ import annotations

import os
import errno
import stat
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .markdown import iter_headings, section_text
from .validate import validate_consumed

# Family -> (.ai subdirectory, canonical id regex)
FAMILIES: dict[str, tuple[str, str]] = {
    "Task": ("tasks", r"^TASK-[0-9]{4}-[0-9]{4}$"),
    "Question": ("questions", r"^QST-[0-9]{4}-[0-9]{4}$"),
    "Decision": ("decisions", r"^DEC-[0-9]{4}-[0-9]{4}$"),
    "Investigation": ("investigations", r"^INV-[0-9]{4}-[0-9]{4}$"),
    "Evidence": ("evidence", r"^EVD-[0-9]{4}-[0-9]{4}$"),
    "Handoff": ("handoffs", r"^HND-[0-9]{4}-[0-9]{4}$"),
    "Report": ("reports", r"^RPT-[0-9]{4}-[0-9]{4}$"),
}

PREFIX_TO_FAMILY = {"TASK": "Task", "QST": "Question", "DEC": "Decision",
                    "INV": "Investigation", "EVD": "Evidence",
                    "HND": "Handoff", "RPT": "Report"}

FRAGMENT_RE = re.compile(r"^(INV-[0-9]{4}-[0-9]{4})#(H[0-9]+)$")
ANY_ID_RE = re.compile(r"^(TASK|QST|DEC|INV|EVD|HND|RPT)-[0-9]{4}-[0-9]{4}$")

TERMINAL_TASK_STATUSES = {"COMPLETED", "CANCELLED", "ARCHIVED"}
TASK_STATUSES = ["INBOX", "ANALYZING", "PLANNED", "WAITING_APPROVAL",
                 "IN_PROGRESS", "BLOCKED", "REVIEW", "COMPLETED",
                 "CANCELLED", "ARCHIVED"]

MAX_RECORD_BYTES: int = 1 * 1024 * 1024  # 1 MiB
MAX_TOTAL_BYTES: int = 64 * 1024 * 1024  # 64 MiB
MAX_RECORDS: int = 10_000


class DuplicateKeyError(ValueError):
    """A YAML mapping declared the same key twice. Never silently resolved."""


class InvalidMappingKeyError(ValueError):
    """A YAML mapping key is not a scalar string field name."""


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys instead of overwriting."""


def _no_duplicates(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        if not isinstance(key_node, yaml.ScalarNode) or \
                key_node.tag != "tag:yaml.org,2002:str":
            raise InvalidMappingKeyError(
                "mapping keys must be scalar strings")
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise InvalidMappingKeyError(
                "mapping keys must be scalar strings")
        if key in mapping:
            raise DuplicateKeyError(f"duplicate key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def parse_yaml_strict(text: str) -> Any:
    """Parse YAML rejecting duplicate keys. Raises on malformed input."""
    return yaml.load(text, Loader=_StrictLoader)


def id_family(record_id: str) -> str | None:
    base = record_id.split("#", 1)[0]
    if not ANY_ID_RE.match(base):
        return None
    return PREFIX_TO_FAMILY.get(base.split("-", 1)[0])


def split_fragment(selector: str) -> tuple[str, str | None]:
    """'INV-2026-0001#H3' -> ('INV-2026-0001', 'H3'); plain id -> (id, None)."""
    m = FRAGMENT_RE.match(selector)
    if m:
        return m.group(1), m.group(2)
    return selector, None


@dataclass
class RecordFile:
    """One discovered file. `malformed` records are never repaired."""

    path: Path
    project_root: Path
    family: str
    filename_id: str
    declared_id: str | None = None
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    malformed: bool = False
    malformed_reason: str | None = None
    invalid_reasons: list[str] = field(default_factory=list)
    escaped: bool = False
    error_code: str | None = None
    bytes_read: int = 0

    @property
    def usable(self) -> bool:
        """May this record supply current native semantics to the loader?"""
        return not self.malformed and not self.invalid_reasons \
            and not self.escaped and not self.id_mismatch()

    @property
    def defect_reason(self) -> str | None:
        reasons: list[str] = []
        if self.malformed_reason:
            reasons.append(self.malformed_reason)
        if self.id_mismatch():
            reasons.append(
                f"filename id {self.filename_id!r} and frontmatter id "
                f"{self.declared_id!r} disagree")
        reasons.extend(self.invalid_reasons)
        return "; ".join(reasons) if reasons else None

    @property
    def rel_path(self) -> str:
        try:
            return self.path.relative_to(self.project_root).as_posix()
        except ValueError:
            return self.path.as_posix()

    @property
    def status(self) -> str | None:
        v = self.frontmatter.get("status")
        return v if isinstance(v, str) else None

    @property
    def schema_version(self) -> str | None:
        v = self.frontmatter.get("schema_version")
        return v if isinstance(v, str) else None

    def id_mismatch(self) -> bool:
        return self.declared_id is not None and self.declared_id != self.filename_id

    def refs(self, field_name: str) -> list[str]:
        """Canonical ids from a relationship field, singular or list."""
        val = self.frontmatter.get(field_name)
        if val is None:
            return []
        vals = val if isinstance(val, list) else [val]
        return [v for v in vals if isinstance(v, str) and v]


def read_frontmatter(path: Path, project_root: Path, family: str) -> RecordFile:
    """Read one record file. Never modifies it; never touches its mtime."""
    rec = RecordFile(path=path, project_root=project_root, family=family,
                     filename_id=path.stem)
    try:
        st = path.stat()
    except OSError as exc:
        rec.malformed = True
        rec.malformed_reason = f"unreadable: {exc.__class__.__name__}"
        return rec

    if not stat.S_ISREG(st.st_mode):
        # A FIFO or device would block or stream without end on open().
        rec.malformed = True
        rec.malformed_reason = "not a regular file"
        return rec

    if st.st_size > MAX_RECORD_BYTES:
        rec.malformed = True
        rec.error_code = "RECORD_TOO_LARGE"
        rec.malformed_reason = (
            f"record exceeds maximum allowed size of {MAX_RECORD_BYTES} bytes "
            f"({st.st_size} bytes)"
        )
        rec.body = ""
        return rec

    try:
        # O_NOFOLLOW/O_NONBLOCK: the entry may have been swapped for a symlink
        # or a FIFO since the stat above; the descriptor is checked again.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise OSError(errno.EINVAL, "not a regular file")
            raw = f.read(MAX_RECORD_BYTES + 1)
    except OSError as exc:
        rec.malformed = True
        rec.malformed_reason = f"unreadable: {exc.__class__.__name__}"
        return rec

    rec.bytes_read = len(raw)

    if len(raw) > MAX_RECORD_BYTES:
        rec.malformed = True
        rec.error_code = "RECORD_TOO_LARGE"
        rec.malformed_reason = (
            f"record exceeds maximum allowed size of {MAX_RECORD_BYTES} bytes "
            f"(read more than {MAX_RECORD_BYTES} bytes)"
        )
        rec.body = ""
        return rec

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        rec.malformed = True
        rec.malformed_reason = "file is not valid UTF-8 text"
        return rec

    if not text.startswith("---\n"):
        rec.malformed = True
        rec.malformed_reason = "missing YAML frontmatter delimiter"
        return rec

    end = text.find("\n---\n", 3)
    if end == -1:
        rec.malformed = True
        rec.malformed_reason = "unterminated YAML frontmatter"
        return rec

    raw_fm = text[4:end + 1]
    rec.body = text[end + 5:]
    try:
        data = parse_yaml_strict(raw_fm)
    except DuplicateKeyError as exc:
        rec.malformed = True
        rec.malformed_reason = f"duplicate frontmatter key ({exc})"
        return rec
    except InvalidMappingKeyError as exc:
        rec.malformed = True
        rec.malformed_reason = f"invalid frontmatter mapping key ({exc})"
        return rec
    except RecursionError:
        rec.malformed = True
        rec.malformed_reason = "frontmatter exceeds supported YAML nesting complexity"
        return rec
    except yaml.YAMLError:
        rec.malformed = True
        rec.malformed_reason = "unparseable YAML frontmatter"
        return rec

    if not isinstance(data, dict):
        rec.malformed = True
        rec.malformed_reason = "frontmatter is not a mapping"
        return rec

    rec.frontmatter = data
    declared = data.get("id")
    if isinstance(declared, str):
        rec.declared_id = declared
    else:
        rec.malformed = True
        rec.malformed_reason = "missing or non-string id"
        return rec
    # consumed M1 invariants: reported, never repaired
    rec.invalid_reasons = validate_consumed(family, data, rec.body)
    return rec


@dataclass
class Repository:
    """All discovered project-local records, indexed by declared id.

    Duplicate declared ids are retained as lists; no winner is ever chosen.
    """

    project_root: Path
    by_id: dict[str, list[RecordFile]] = field(default_factory=dict)
    all_records: list[RecordFile] = field(default_factory=list)
    mismatched: list[RecordFile] = field(default_factory=list)
    escapes: list[tuple[str, str, str]] = field(default_factory=list)
    # (family, ".ai/<subdir>") for directories that exist but cannot be
    # listed. Unreadable storage is never evidence of absence.
    unreadable_dirs: list[tuple[str, str]] = field(default_factory=list)
    decision_successors: dict[str, set[str]] = field(default_factory=dict)
    decision_predecessors: dict[str, set[str]] = field(default_factory=dict)
    diagnostics: list[tuple[str, str, str, str]] = field(default_factory=list)

    def unique(self, record_id: str) -> RecordFile | None:
        hits = self.by_id.get(record_id, [])
        return hits[0] if len(hits) == 1 else None

    def is_duplicate(self, record_id: str) -> bool:
        return len(self.by_id.get(record_id, [])) > 1

    def identity_resolved(self, rec: RecordFile) -> bool:
        """True only when one physical file safely owns its logical id."""
        rid = rec.declared_id
        return bool(rid) and not rec.id_mismatch() and not self.is_duplicate(rid)

    def record_usable(self, rec: RecordFile) -> bool:
        """Repository-level semantic usability, including id ambiguity."""
        return rec.usable and self.identity_resolved(rec)

    def family_records(self, family: str) -> list[RecordFile]:
        return sorted((r for r in self.all_records if r.family == family),
                      key=lambda r: r.declared_id or r.filename_id)

    def backlinks(self, family: str, task_id: str) -> list[RecordFile]:
        """Records of `family` whose native `task` field equals task_id."""
        out = [r for r in self.family_records(family)
               if self.record_usable(r)
               and r.frontmatter.get("task") == task_id]
        return out

    def build_decision_relationship_indexes(self) -> None:
        """Index safely parsed declared half-edges without selecting records.

        For predecessor A and successor B, either A.superseded_by naming B or
        B.supersedes naming A makes the pair visible for reciprocity checking.
        Record usability is still enforced by the loader before an edge can be
        effective.
        """
        decision_re = re.compile(FAMILIES["Decision"][1])
        for rec in self.family_records("Decision"):
            rid = rec.declared_id
            if not rid or not decision_re.match(rid) \
                    or not self.identity_resolved(rec):
                continue
            for successor in rec.refs("superseded_by"):
                if decision_re.match(successor):
                    self.decision_successors.setdefault(rid, set()).add(successor)
                    self.decision_predecessors.setdefault(successor, set()).add(rid)
            for predecessor in rec.refs("supersedes"):
                if decision_re.match(predecessor):
                    self.decision_successors.setdefault(predecessor, set()).add(rid)
                    self.decision_predecessors.setdefault(rid, set()).add(predecessor)

    def successor_candidates(self, predecessor_id: str) -> list[str]:
        return sorted(self.decision_successors.get(predecessor_id, set()))

    def predecessor_candidates(self, successor_id: str) -> list[str]:
        return sorted(self.decision_predecessors.get(successor_id, set()))


def discover(project_root: Path, scope) -> Repository:
    """Scan the seven canonical directories beneath the positional project.

    Every directory and every file must canonically resolve inside the
    project's own `.ai` root. Escapes are recorded and never read. A family
    directory that exists but cannot be listed is recorded in
    `unreadable_dirs` instead of being enumerated as empty.

    Dot-prefixed entries, non-`.md` files, and `README.md` are never records:
    editor lock files, OS metadata, backups, and directory documentation sit
    deliberately outside record identity instead of becoming permanent
    quarantine noise. A plain `.md` file whose stem is not a canonical id IS
    retained, as a malformed discovery, so visible misnamed files remain
    diagnosable.
    """
    repo = Repository(project_root=project_root)
    ai_dir = project_root / ".ai"
    total_bytes_read = 0
    records_seen = 0
    limit_hit = False

    for family, (subdir, id_re) in FAMILIES.items():
        if limit_hit:
            break
        d = ai_dir / subdir
        if scope.escaped(subdir):
            repo.escapes.append((family, f".ai/{subdir}",
                                 "record directory resolves outside the "
                                 "project .ai root"))
            continue
        if not d.is_dir():
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError:
            repo.unreadable_dirs.append((family, f".ai/{subdir}"))
            continue
        for name in names:
            if limit_hit:
                break
            if name.startswith(".") or not name.endswith(".md"):
                continue
            path = d / name
            if name == "README.md":
                continue
            if not scope.contains(path):
                repo.escapes.append(
                    (family, f".ai/{subdir}/{path.name}",
                     "record file resolves outside the project .ai root"))
                continue

            try:
                rel_p = path.relative_to(project_root).as_posix()
            except ValueError:
                rel_p = path.as_posix()

            records_seen += 1
            if records_seen > MAX_RECORDS:
                repo.diagnostics.append(
                    ("RECORD_LIMIT_EXCEEDED", rel_p,
                     f"record limit exceeded (more than {MAX_RECORDS} records)",
                     family)
                )
                limit_hit = True
                break

            if not re.match(id_re, path.stem):
                # Filename is not a canonical id for this family: retain as a
                # malformed discovery rather than guessing an identity.
                rec = RecordFile(path=path, project_root=project_root,
                                 family=family, filename_id=path.stem,
                                 malformed=True,
                                 malformed_reason="filename is not a canonical id")
                repo.all_records.append(rec)
                continue

            try:
                st = path.stat()
                st_size = st.st_size
            except OSError:
                st_size = 0

            # A single oversized record is refused by read_frontmatter without
            # being read (RECORD_TOO_LARGE); it does not count towards the total.
            if st_size <= MAX_RECORD_BYTES and total_bytes_read + st_size > MAX_TOTAL_BYTES:
                repo.diagnostics.append(
                    ("TOTAL_BYTES_EXCEEDED", rel_p,
                     f"total bytes limit exceeded (more than {MAX_TOTAL_BYTES} bytes)",
                     family)
                )
                limit_hit = True
                break

            rec = read_frontmatter(path, project_root, family)
            total_bytes_read += rec.bytes_read

            if rec.error_code == "RECORD_TOO_LARGE":
                repo.diagnostics.append(
                    ("RECORD_TOO_LARGE", rec.rel_path,
                     rec.malformed_reason or f"record exceeds maximum allowed size ({MAX_RECORD_BYTES} bytes)",
                     family)
                )

            if total_bytes_read > MAX_TOTAL_BYTES:
                repo.diagnostics.append(
                    ("TOTAL_BYTES_EXCEEDED", rec.rel_path,
                     f"total bytes limit exceeded (more than {MAX_TOTAL_BYTES} bytes)",
                     family)
                )
                repo.all_records.append(rec)
                limit_hit = True
                break

            repo.all_records.append(rec)
            if rec.id_mismatch():
                repo.mismatched.append(rec)
                # Neither identity is chosen; index under both so the
                # collision stays visible and unresolved.
                repo.by_id.setdefault(rec.filename_id, []).append(rec)
                repo.by_id.setdefault(rec.declared_id, []).append(rec)
            elif rec.declared_id:
                repo.by_id.setdefault(rec.declared_id, []).append(rec)
    repo.build_decision_relationship_indexes()
    return repo


# fragment resolution outcomes
FRAG_OK = "OK"
FRAG_MISSING = "MISSING"
FRAG_AMBIGUOUS = "AMBIGUOUS"


def hypothesis_token(text: str) -> str:
    """The leading identifier of a hypothesis heading, e.g. 'H10' in
    '### H10 — rules out the cache'. Exact token, never a prefix match."""
    return re.split(r"[\s—–-]", text, maxsplit=1)[0].strip()


def find_hypothesis_fragment(body: str, hyp: str) -> tuple[str | None, str]:
    """Resolve `### Hn` exactly. Returns (text, FRAG_OK/MISSING/AMBIGUOUS).

    No fallback, normalization, renumber inference, or fuzzy match. Duplicate
    genuine headings are ambiguous and never silently resolved to the first.
    """
    matches = [(i, lv) for i, lv, text in iter_headings(body)
               if lv == 3 and hypothesis_token(text) == hyp]
    if not matches:
        return None, FRAG_MISSING
    if len(matches) > 1:
        return None, FRAG_AMBIGUOUS
    start, level = matches[0]
    return section_text(body, start, level), FRAG_OK


def extract_hypothesis_fragment(body: str, hyp: str) -> str | None:
    """Convenience wrapper: text only when resolution is unambiguous."""
    text, status = find_hypothesis_fragment(body, hyp)
    return text if status == FRAG_OK else None


def extract_section(body: str, heading_text: str) -> str | None:
    """Return an exact `## <heading_text>` section, else None. Fence-aware."""
    sections = extract_sections(body, heading_text)
    return sections[0] if sections else None


def extract_sections(body: str, heading_text: str) -> list[str]:
    """Return every exact level-two section in source order. Fence-aware."""
    return [section_text(body, i, 2) for i, level, text in iter_headings(body)
            if level == 2 and text == heading_text]


def find_section_fragment(body: str, heading_text: str) -> tuple[str | None, str]:
    """Resolve one exact level-two section without choosing among duplicates."""
    matches = [(i, level) for i, level, text in iter_headings(body)
               if level == 2 and text == heading_text]
    if not matches:
        return None, FRAG_MISSING
    if len(matches) > 1:
        return None, FRAG_AMBIGUOUS
    start, level = matches[0]
    return section_text(body, start, level), FRAG_OK
