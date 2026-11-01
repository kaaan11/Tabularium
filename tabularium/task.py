"""Task product surfaces: read-only inspection (Phase I3) and the canonical
Task ID allocator (Phase I4).

The read surfaces are projections over the PRODUCTION Task loader. There is
no second parser, no second scanner, and no repair: discovery, frontmatter
parsing, consumed-invariant validation, and identity resolution all come from
`tabularium.engine.context_loader` exactly as `run` consumes them. A record that cannot
take part in a governed run shows up here as unusable, with the loader's own
evidence.

The allocator generalizes the established `_next_id` pattern (tabularium/engine/
continuation/planner.py) to Task ids under the human-approved policy:
scan canonical Task filenames, allocate max+1 within the year sequence,
refuse unreadable storage and 9999 exhaustion, and surface collisions for
the caller to fail closed on. It never overwrites anything.

Machine-readable surfaces carry the same stable fact set as the human ones.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from tabularium.engine.context_loader.confinement import ProjectScope
from tabularium.engine.context_loader.discovery import (
    FAMILIES,
    TERMINAL_TASK_STATUSES,
    RecordFile,
    discover,
    read_frontmatter,
)
from tabularium.engine.context_loader.validate import SUPPORTED_VERSION

from .execution import _sanitize_fragment
from .init import InitError, _canonical_target

# Reason codes reused from the frozen loader taxonomy wherever it can express
# the outcome; two product-level codes name what only the CLI surface sees.
RECORD_DIRECTORY_UNREADABLE = "RECORD_DIRECTORY_UNREADABLE"
PATH_CONFINEMENT_FAILED = "PATH_CONFINEMENT_FAILED"
RESOLUTION_AMBIGUOUS = "RESOLUTION_AMBIGUOUS"
INVALID_RECORD_QUARANTINED = "INVALID_RECORD_QUARANTINED"
MALFORMED_RECORD_QUARANTINED = "MALFORMED_RECORD_QUARANTINED"
FILENAME_ID_MISMATCH = "FILENAME_ID_MISMATCH"
TASK_NOT_FOUND = "TASK_NOT_FOUND"
TASK_ID_INVALID = "TASK_ID_INVALID"
# Allocator refusals the frozen taxonomy cannot express.
TASK_ID_SEQUENCE_EXHAUSTED = "TASK_ID_SEQUENCE_EXHAUSTED"
TASK_ID_COLLISION = "TASK_ID_COLLISION"
# Raised only when a freshly published record fails its verification re-read.
TASK_CREATE_VERIFICATION_FAILED = "TASK_CREATE_VERIFICATION_FAILED"
# Task lifecycle transition codes.
TASK_TRANSITION_INVALID = "TASK_TRANSITION_INVALID"
TASK_STATUS_INVALID = "TASK_STATUS_INVALID"
TASK_TRANSITION_VERIFICATION_FAILED = "TASK_TRANSITION_VERIFICATION_FAILED"

_TASK_DIRECTORY = "tasks"
_TASK_ID_SCAN = re.compile(rf"^TASK-([0-9]{{4}})-([0-9]{{4}})\.md$")

# Listing is a bounded summary: every surfaced row is capped, and beyond this
# many rows both renderers show an explicit remainder count instead of rows.
_LIST_LIMIT = 100
# Mirrors the established Task-diagnostic detail bound in tabularium.execution.
_UNUSABLE_DETAIL_LIMIT = 5
# One Task's own content, when shown, is capped identically for humans and
# machines, with an explicit truncation fact next to it.
_SHOW_BODY_LIMIT = 20000

ACTIVE = "ACTIVE"
TERMINAL = "TERMINAL"
UNUSABLE = "UNUSABLE"


_TASK_ID_RE = re.compile(FAMILIES["Task"][1])


def _is_canonical_task_id(value: str) -> bool:
    return bool(_TASK_ID_RE.match(value))


def _exit_code_for(code: str | None) -> int:
    if code is None:
        return 0
    if code in {"TARGET_NOT_FOUND", "TARGET_NOT_DIRECTORY",
                "PERMISSION_DENIED", TASK_ID_INVALID, TASK_STATUS_INVALID}:
        return 2
    return 1


def _load_repository(target: Path):
    """One production discovery pass over the project's records."""
    return discover(target, ProjectScope(target))


def _storage_refusal(target: Path, repo) -> dict | None:
    """Fail closed on Task storage that cannot be listed or confined."""
    for family, label in repo.unreadable_dirs:
        if family == "Task":
            return {
                "code": RECORD_DIRECTORY_UNREADABLE,
                "message": "Task storage is unreadable: .ai/tasks/ exists "
                           "but its contents cannot be listed, so Task "
                           "records cannot be inspected. Restore read "
                           "access to .ai/tasks/, then retry.",
            }
    for family, label, _reason in getattr(repo, "escapes", ()):
        if family == "Task":
            return {
                "code": PATH_CONFINEMENT_FAILED,
                "message": "A Task record path resolves outside the project "
                           ".ai root; inspect the .ai/tasks directory.",
            }
    # A count or total-size limit stops discovery part way: a partial Task
    # list must not be presented as the whole one.
    for code, label, reason, _family in getattr(repo, "diagnostics", ()):
        if code in ("RECORD_LIMIT_EXCEEDED", "TOTAL_BYTES_EXCEEDED"):
            return {
                "code": code,
                "message": f"Record discovery stopped at {label}: {reason}. "
                           "Task records cannot all be inspected; reduce the "
                           ".ai records, then retry.",
            }
    return None


def _record_state(repo, rec: RecordFile) -> str:
    if not repo.record_usable(rec):
        return UNUSABLE
    return TERMINAL if rec.status in TERMINAL_TASK_STATUSES else ACTIVE


def _safe_title(rec: RecordFile) -> str | None:
    value = rec.frontmatter.get("title")
    return value if isinstance(value, str) else None


def _defect_code(repo, rec: RecordFile) -> str:
    if rec.malformed:
        return MALFORMED_RECORD_QUARANTINED
    if rec.id_mismatch():
        return FILENAME_ID_MISMATCH
    if rec.declared_id and repo.is_duplicate(rec.declared_id):
        return RESOLUTION_AMBIGUOUS
    return INVALID_RECORD_QUARANTINED


def _entry_errors(repo, rec: RecordFile) -> list[str]:
    errors: list[str] = []
    if rec.malformed_reason:
        errors.append(rec.malformed_reason)
    if rec.id_mismatch():
        errors.append(
            f"filename id {rec.filename_id!r} and frontmatter id "
            f"{rec.declared_id!r} disagree")
    if rec.declared_id and repo.is_duplicate(rec.declared_id):
        errors.append(f"canonical id {rec.declared_id} is claimed by "
                      f"multiple files; no winner is chosen")
    errors.extend(rec.invalid_reasons)
    return errors


@dataclass(frozen=True)
class TaskSummary:
    """One bounded listing row; the machine and human views agree."""

    id: str | None
    file: str
    state: str
    status: str | None = None
    title: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"id": self.id, "file": self.file,
                                  "state": self.state}
        if self.status is not None:
            out["status"] = self.status
        if self.title is not None:
            out["title"] = self.title
        if self.reason is not None:
            out["reason"] = self.reason
        return out


@dataclass(frozen=True)
class TaskListResult:
    command: str = "task_list"
    target: str = ""
    tasks: tuple[TaskSummary, ...] = ()
    total_count: int = 0
    omitted_count: int = 0
    code: str | None = None
    message: str | None = None

    @property
    def exit_code(self) -> int:
        return _exit_code_for(self.code)

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "command": self.command, "target": self.target,
            "count": len(self.tasks), "total_count": self.total_count,
            "omitted_count": self.omitted_count,
            "tasks": [t.to_dict() for t in self.tasks],
            "exit_code": self.exit_code,
        }
        if self.code is not None:
            out["code"] = self.code
        if self.message is not None:
            out["message"] = self.message
        return out


def task_list(raw_target: str | Path | None = None) -> TaskListResult:
    try:
        target = _canonical_target(raw_target)
    except InitError as exc:
        return TaskListResult(target=str(raw_target or "."), code=exc.code,
                              message=exc.message)
    repo = _load_repository(target)
    refusal = _storage_refusal(target, repo)
    if refusal is not None:
        return TaskListResult(target=str(target),
                              code=refusal["code"],
                              message=refusal["message"])
    summaries: list[TaskSummary] = []
    for rec in repo.family_records("Task"):
        state = _record_state(repo, rec)
        reason = None
        if state == UNUSABLE:
            reason = "; ".join(_entry_errors(repo, rec)) or \
                "record is unusable"
        summaries.append(TaskSummary(
            id=rec.declared_id,
            file=rec.rel_path,
            state=state,
            status=rec.status if state != UNUSABLE else None,
            title=_safe_title(rec),
            reason=reason))
    total = len(summaries)
    omitted = max(0, total - _LIST_LIMIT)
    return TaskListResult(target=str(target),
                          tasks=tuple(summaries[:_LIST_LIMIT]),
                          total_count=total, omitted_count=omitted)


@dataclass(frozen=True)
class TaskShowResult:
    command: str = "task_show"
    target: str = ""
    requested_id: str = ""
    code: str | None = None
    message: str | None = None
    found: bool = False
    valid: bool = False
    terminal: bool = False
    id: str | None = None
    file: str | None = None
    status: str | None = None
    title: str | None = None
    frontmatter: dict | None = None
    body: str | None = None
    body_truncated: bool = False
    error_codes: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        if self.code is not None:
            return _exit_code_for(self.code)
        return 0 if self.found and self.valid else 1

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "command": self.command, "target": self.target,
            "requested_id": self.requested_id,
            "found": self.found, "valid": self.valid,
            "terminal": self.terminal,
            "error_codes": list(self.error_codes),
            "errors": list(self.errors),
            "exit_code": self.exit_code,
        }
        for key in ("id", "file", "status", "title", "frontmatter", "body"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.body is not None:
            out["body_truncated"] = self.body_truncated
        if self.code is not None:
            out["code"] = self.code
        if self.message is not None:
            out["message"] = self.message
        return out


def task_show(raw_target: str | Path | None, requested_id: str) \
        -> TaskShowResult:
    target_label = str(raw_target or ".")
    if not isinstance(requested_id, str) or \
            not _is_canonical_task_id(requested_id.strip()):
        return TaskShowResult(
            target=target_label, requested_id=str(requested_id),
            code=TASK_ID_INVALID,
            message="A Task id must have the canonical form "
                    "TASK-YYYY-NNNN, e.g. TASK-2026-0001.")
    requested_id = requested_id.strip()
    try:
        target = _canonical_target(raw_target)
    except InitError as exc:
        return TaskShowResult(target=target_label, requested_id=requested_id,
                              code=exc.code, message=exc.message)
    repo = _load_repository(target)
    refusal = _storage_refusal(target, repo)
    if refusal is not None:
        return TaskShowResult(target=str(target), requested_id=requested_id,
                              code=refusal["code"],
                              message=refusal["message"])
    # One production discovery pass serves every identity here: the by_id
    # index holds parsed declared ids, while family_records also exposes
    # malformed files by their canonical filename so show stays able to
    # explain them. No second scan is performed.
    hits = [rec for rec in repo.family_records("Task")
            if requested_id in {rec.declared_id, rec.filename_id}]
    hits.sort(key=lambda rec: rec.declared_id or rec.filename_id)
    if not hits:
        return TaskShowResult(
            target=str(target), requested_id=requested_id,
            code=TASK_NOT_FOUND,
            message=f"No Task record with id {requested_id} exists under "
                    ".ai/tasks/. Run `tabu task list` to see stored Tasks.")
    if len(hits) > 1:
        files = sorted({rec.rel_path for rec in hits})
        return TaskShowResult(
            target=str(target), requested_id=requested_id, found=True,
            code=RESOLUTION_AMBIGUOUS,
            message="The requested Task id is claimed by multiple files; "
                    "no winner is chosen. Tabularium never repairs Task records: "
                    "correct them so exactly one file owns the id.",
            error_codes=(RESOLUTION_AMBIGUOUS,),
            errors=tuple(files))
    rec = hits[0]
    valid = repo.record_usable(rec)
    codes: list[str] = []
    errors: list[str] = []
    if not valid:
        codes.append(_defect_code(repo, rec))
        errors = _entry_errors(repo, rec)
    body = rec.body if not rec.malformed else None
    truncated = False
    if body is not None and len(body) > _SHOW_BODY_LIMIT:
        body = body[:_SHOW_BODY_LIMIT]
        truncated = True
    return TaskShowResult(
        target=str(target), requested_id=requested_id, found=True,
        valid=valid, terminal=rec.status in TERMINAL_TASK_STATUSES,
        id=rec.declared_id, file=rec.rel_path,
        status=rec.status, title=_safe_title(rec),
        frontmatter=dict(rec.frontmatter) if rec.frontmatter else None,
        body=body, body_truncated=truncated,
        error_codes=tuple(codes), errors=tuple(errors))


@dataclass(frozen=True)
class TaskValidationEntry:
    id: str | None
    file: str
    valid: bool
    state: str
    error_codes: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"id": self.id, "file": self.file, "valid": self.valid,
                "state": self.state,
                "error_codes": list(self.error_codes),
                "errors": list(self.errors)}


@dataclass(frozen=True)
class TaskValidateResult:
    command: str = "task_validate"
    target: str = ""
    requested_id: str | None = None
    entries: tuple[TaskValidationEntry, ...] = ()
    valid_count: int = 0
    invalid_count: int = 0
    total_count: int = 0
    omitted_count: int = 0
    code: str | None = None
    message: str | None = None

    @property
    def exit_code(self) -> int:
        if self.code is not None:
            return _exit_code_for(self.code)
        return 1 if self.invalid_count else 0

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "command": self.command, "target": self.target,
            "requested_id": self.requested_id,
            "entries": [e.to_dict() for e in self.entries],
            "valid_count": self.valid_count,
            "invalid_count": self.invalid_count,
            "total_count": self.total_count,
            "omitted_count": self.omitted_count,
            "exit_code": self.exit_code,
        }
        if self.code is not None:
            out["code"] = self.code
        if self.message is not None:
            out["message"] = self.message
        return out


def task_validate(raw_target: str | Path | None = None,
                  requested_id: str | None = None) -> TaskValidateResult:
    target_label = str(raw_target or ".")
    if requested_id is not None and \
            (not isinstance(requested_id, str) or
             not _is_canonical_task_id(requested_id.strip())):
        return TaskValidateResult(
            target=target_label, requested_id=requested_id,
            code=TASK_ID_INVALID,
            message="A Task id must have the canonical form "
                    "TASK-YYYY-NNNN, e.g. TASK-2026-0001.")
    try:
        target = _canonical_target(raw_target)
    except InitError as exc:
        return TaskValidateResult(target=target_label,
                                  requested_id=requested_id,
                                  code=exc.code, message=exc.message)
    repo = _load_repository(target)
    refusal = _storage_refusal(target, repo)
    if refusal is not None:
        return TaskValidateResult(target=str(target),
                                  requested_id=requested_id,
                                  code=refusal["code"],
                                  message=refusal["message"])
    if requested_id is not None:
        records = [rec for rec in repo.family_records("Task")
                   if requested_id.strip() in
                   {rec.declared_id, rec.filename_id}]
    else:
        records = list(repo.family_records("Task"))
    entries: list[TaskValidationEntry] = []
    for rec in records:
        valid = repo.record_usable(rec)
        codes = () if valid else (_defect_code(repo, rec),)
        errors = () if valid else tuple(_entry_errors(repo, rec))
        entries.append(TaskValidationEntry(
            id=rec.declared_id, file=rec.rel_path, valid=valid,
            state=_record_state(repo, rec), error_codes=codes,
            errors=errors))
    total = len(entries)
    omitted = max(0, total - _LIST_LIMIT)
    valid_count = sum(1 for e in entries if e.valid)
    return TaskValidateResult(
        target=str(target), requested_id=requested_id,
        entries=tuple(entries[:_LIST_LIMIT]),
        valid_count=valid_count, invalid_count=total - valid_count,
        total_count=total, omitted_count=omitted)


# --- Task ID allocation (Phase I4) -------------------------------------------


@dataclass(frozen=True)
class TaskIdAllocation:
    """Outcome of one deterministic scan; refusals never invent an id."""

    record_id: str | None = None
    collision: bool = False
    code: str | None = None
    message: str | None = None

    @property
    def refused(self) -> bool:
        return self.code is not None


def allocate_task_id(raw_target: str | Path, *,
                     year: int | None = None) -> TaskIdAllocation:
    """Allocate the next canonical Task id by scanning stored filenames.

    Human-approved policy (Decision B): max+1 over canonical
    ``TASK-YYYY-NNNN.md`` filenames for the target year; unreadable storage
    refuses (an enumeration failure is never treated as empty storage);
    sequence exhaustion at 9999 refuses; an already-present target is a
    collision the caller must refuse on. Read-only and deterministic — no id
    is reserved here. Malformed filenames are not ids and never counted.
    """
    if year is None:
        year = datetime.now(timezone.utc).year
    directory = Path(raw_target) / ".ai" / _TASK_DIRECTORY
    highest = 0
    if directory.is_dir():
        try:
            # iterdir, not glob: a listing failure must refuse here. glob
            # silently treats an unreadable directory as empty, which would
            # reallocate an id that already exists behind it.
            children = list(os.scandir(directory))
        except OSError:
            return TaskIdAllocation(
                code=RECORD_DIRECTORY_UNREADABLE,
                message="Task storage is unreadable: .ai/tasks/ exists but "
                        "its contents cannot be listed, so no Task id can "
                        "be allocated safely. Restore read access to "
                        ".ai/tasks/, then retry.")
        for child in children:
            match = _TASK_ID_SCAN.match(child.name)
            if match and int(match.group(1)) == year:
                highest = max(highest, int(match.group(2)))
    if highest >= 9999:
        return TaskIdAllocation(
            code=TASK_ID_SEQUENCE_EXHAUSTED,
            message=f"The four-digit Task sequence for {year} is exhausted; "
                    "no further Task can be allocated for that year.")
    record_id = f"TASK-{year:04d}-{highest + 1:04d}"
    target_exists = (directory / f"{record_id}.md").exists()
    return TaskIdAllocation(record_id=record_id, collision=target_exists)


# --- Task creation (Phase I5) ------------------------------------------------

# The create command records that a human operator entered the request; the
# lifecycle starts where the frozen protocol starts. Approval provenance is
# deliberately NOT acceptable through this command: it is not an approval tool.
_CREATE_SCHEMA_VERSION = SUPPORTED_VERSION["Task"]
_CREATE_STATUS = "INBOX"
_CREATE_CREATED_BY = "USER"
_CREATE_BODY = (
    "# Task\n\n"
    "Created by `tabu task create`. The narrative body remains to be added "
    "by the operator; the frontmatter above is authoritative.\n")


def build_task_frontmatter(*, record_id: str, title: str, objective: str,
                           type_=None, priority=None, risk=None,
                           uncertainty_level=None, plan=None) -> dict:
    """Canonical v1.10 Task frontmatter in deterministic key order.

    Only fields from the current frozen contract are emitted, and only those
    the operator actually supplied — omission means NOT RECORDED, never a
    default.
    """
    fm: dict = {"schema_version": _CREATE_SCHEMA_VERSION, "id": record_id}
    if type_ is not None:
        fm["type"] = type_
    fm["title"] = title
    fm["objective"] = objective
    if priority is not None:
        fm["priority"] = priority
    if risk is not None:
        fm["risk"] = risk
    if uncertainty_level is not None:
        fm["uncertainty_level"] = uncertainty_level
    if plan is not None:
        fm["plan"] = plan
    fm["status"] = _CREATE_STATUS
    fm["created_at"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    fm["created_by"] = _CREATE_CREATED_BY
    return fm


def render_task_document(frontmatter: dict, body: str = _CREATE_BODY) -> str:
    """Canonical stored form: YAML frontmatter + Markdown body."""
    fm_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True,
                             default_flow_style=False)
    return f"---\n{fm_text}---\n{body}"


def _parse_document(document: str, *, filename: str, project_root) \
        -> tuple[RecordFile | None, list[str]]:
    """Run the PRODUCTION parser/validator over candidate content.

    Nothing is written under the project: the caller stages the bytes
    wherever it deems safe and hands them here for the exact storage-contract
    round trip (strict YAML parse, id grammar, validate_consumed).
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="tabu-task-validate-") as scratch:
        path = Path(scratch) / filename
        path.write_text(document, encoding="utf-8")
        rec = read_frontmatter(path, Path(project_root), "Task")
    errors = list(rec.invalid_reasons)
    if rec.malformed_reason:
        errors.insert(0, rec.malformed_reason)
    if rec.id_mismatch():
        errors.append(f"filename id {rec.filename_id!r} and frontmatter id "
                      f"{rec.declared_id!r} disagree")
    return rec, errors


@dataclass(frozen=True)
class TaskCreateResult:
    command: str = "task_create"
    target: str = ""
    print_only: bool = False
    created: bool = False
    record_id: str | None = None
    file: str | None = None
    content: str | None = None
    validation_errors: tuple[str, ...] = ()
    code: str | None = None
    message: str | None = None

    @property
    def exit_code(self) -> int:
        if self.code is not None:
            return _exit_code_for(self.code)
        return 0

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "command": self.command, "target": self.target,
            "print_only": self.print_only, "created": self.created,
            "exit_code": self.exit_code,
        }
        if self.record_id is not None:
            out["id"] = self.record_id
        if self.file is not None:
            out["file"] = self.file
        if self.content is not None:
            out["content"] = self.content
        out["validation_errors"] = list(self.validation_errors)
        if self.code is not None:
            out["code"] = self.code
        if self.message is not None:
            out["message"] = self.message
        return out


def _tasks_directory(target: Path):
    """The confined Task storage directory, or a refusal reason."""
    scope = ProjectScope(target)
    entry = scope.entry("tasks")
    tasks_dir = target / ".ai" / "tasks"
    if entry is None or not tasks_dir.exists():
        return tasks_dir, ("Task storage is missing: this project has no "
                           ".ai/tasks/ directory. Run `tabu init` to create "
                           "the project scaffold.")
    if not tasks_dir.is_dir() or not scope.contains(tasks_dir):
        return tasks_dir, ("Task storage is malformed: .ai/tasks/ is not a "
                           "directory confined to the project .ai root.")
    return tasks_dir, None


def task_create(raw_target: str | Path | None = None, *,
                title: str, objective: str, type_: str | None = None,
                priority: str | None = None, risk: str | None = None,
                uncertainty_level: str | None = None, plan: str | None = None,
                print_only: bool = False) -> TaskCreateResult:
    target_label = str(raw_target or ".")
    try:
        target = _canonical_target(raw_target)
    except InitError as exc:
        return TaskCreateResult(target=target_label, print_only=print_only,
                                code=exc.code, message=exc.message)
    from .diagnostics import status_project
    from .execution import INVALID_PROJECT, NOT_INITIALIZED, NOT_ONBOARDED
    from .execution import _health_gate

    try:
        health = status_project(target)
    except (ImportError, OSError, RuntimeError, ValueError):
        return TaskCreateResult(
            target=str(target), print_only=print_only,
            code=INVALID_PROJECT,
            message="The project could not be inspected. Run `tabu doctor` "
                    "and correct the reported project issue.")
    gate = _health_gate(target, health)
    if gate is not None:
        gate_message = gate.message
        gate_code = gate.code
        return TaskCreateResult(target=str(target), print_only=print_only,
                                code=gate_code, message=gate_message)

    tasks_dir, storage_problem = _tasks_directory(target)
    if storage_problem is not None:
        return TaskCreateResult(target=str(target), print_only=print_only,
                                code=TASK_ID_INVALID if False else
                                "TASK_STORAGE_MISSING",
                                message=storage_problem)

    allocation = allocate_task_id(target)
    if allocation.refused:
        return TaskCreateResult(target=str(target), print_only=print_only,
                                code=allocation.code,
                                message=allocation.message)
    if allocation.collision:
        # The scan-to-write race fact from the allocator is a refusal here;
        # nothing is ever overwritten.
        return TaskCreateResult(target=str(target), print_only=print_only,
                                code=TASK_ID_COLLISION,
                                message=f"{allocation.record_id}.md already "
                                        "exists; no Task was written and "
                                        "nothing was overwritten.")
    record_id = allocation.record_id

    frontmatter = build_task_frontmatter(
        record_id=record_id, title=title, objective=objective,
        type_=type_, priority=priority, risk=risk,
        uncertainty_level=uncertainty_level, plan=plan)
    document = render_task_document(frontmatter)
    rec, errors = _parse_document(document, filename=f"{record_id}.md",
                                  project_root=target)
    if rec is None or errors or not rec.usable:
        # Fail closed BEFORE anything exists under the canonical name.
        detail = "; ".join(errors) or "record did not satisfy the contract"
        return TaskCreateResult(
            target=str(target), print_only=print_only,
            validation_errors=tuple(errors),
            code=INVALID_RECORD_QUARANTINED,
            message=f"The requested Task would not satisfy the stored "
                    f"contract, so nothing was written: {detail}")

    if print_only:
        return TaskCreateResult(
            target=str(target), print_only=True, record_id=record_id,
            file=f".ai/tasks/{record_id}.md", content=document,
            message=f"Candidate {record_id}: valid against the stored "
                    "contract. Nothing was written and the id is NOT "
                    "reserved; another create may allocate the same id.")

    final_path = tasks_dir / f"{record_id}.md"
    stage_path = tasks_dir / (f".create-stage-{os.getpid()}-"
                              f"{secrets.token_hex(4)}")
    published = False
    try:
        # Stage fully, fsync, then publish with a link that cannot clobber.
        # The staged name starts with a dot, so discovery never sees it; the
        # canonical name appears only through the atomic no-overwrite link.
        try:
            with open(stage_path, "xb") as handle:
                handle.write(document.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.link(stage_path, final_path)
            published = True
        except FileExistsError:
            return TaskCreateResult(
                target=str(target), print_only=False,
                code=TASK_ID_COLLISION,
                message=f"{record_id}.md already exists; no Task was "
                        "written and nothing was overwritten.")
        except PermissionError:
            return TaskCreateResult(
                target=str(target), print_only=False,
                code="PERMISSION_DENIED",
                message="Permission denied while writing to .ai/tasks/; "
                        "no Task was written.")
        except OSError:
            return TaskCreateResult(
                target=str(target), print_only=False,
                code="WRITE_FAILED",
                message="The Task could not be written to .ai/tasks/; "
                        "no partial record was left behind.")
    finally:
        with suppress(OSError):
            stage_path.unlink()
    verification = read_frontmatter(final_path, target, "Task")
    if not verification.usable or verification.declared_id != record_id:
        return TaskCreateResult(
            target=str(target), print_only=False,
            code=TASK_CREATE_VERIFICATION_FAILED,
            message="A Task was published but its verification re-read did "
                    "not satisfy the stored contract. Inspect "
                    f".ai/tasks/{record_id}.md before running work on it.",
            record_id=record_id, file=f".ai/tasks/{record_id}.md")
    return TaskCreateResult(
        target=str(target), print_only=False, created=True,
        record_id=record_id, file=f".ai/tasks/{record_id}.md",
        message=f"Created {record_id}. The Task starts in state INBOX; it "
                "is not approved, planned, or selected until you make it so.")


# --- Task lifecycle transitions (Phase 2) ------------------------------------

VALID_TASK_STATUSES = (
    "INBOX", "ANALYZING", "PLANNED", "WAITING_APPROVAL",
    "IN_PROGRESS", "BLOCKED", "REVIEW", "COMPLETED",
    "CANCELLED", "ARCHIVED"
)

SCHEMA_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "INBOX": ("ANALYZING", "CANCELLED"),
    "ANALYZING": ("PLANNED", "WAITING_APPROVAL", "BLOCKED", "CANCELLED"),
    "PLANNED": ("IN_PROGRESS", "WAITING_APPROVAL", "BLOCKED", "CANCELLED"),
    "WAITING_APPROVAL": ("IN_PROGRESS", "PLANNED", "ANALYZING", "BLOCKED", "CANCELLED"),
    "IN_PROGRESS": ("REVIEW", "WAITING_APPROVAL", "BLOCKED", "CANCELLED"),
    "BLOCKED": ("ANALYZING", "PLANNED", "WAITING_APPROVAL", "IN_PROGRESS", "REVIEW", "CANCELLED"),
    "REVIEW": ("COMPLETED", "IN_PROGRESS", "WAITING_APPROVAL", "BLOCKED", "CANCELLED"),
    "COMPLETED": ("ARCHIVED",),
    "CANCELLED": ("ARCHIVED",),
    "ARCHIVED": (),
}


@dataclass(frozen=True)
class TaskTransitionResult:
    command: str = "task_transition"
    target: str = ""
    record_id: str | None = None
    file: str | None = None
    previous_status: str | None = None
    new_status: str | None = None
    updated: bool = False
    validation_errors: tuple[str, ...] = ()
    code: str | None = None
    message: str | None = None

    @property
    def exit_code(self) -> int:
        if self.code is not None:
            return _exit_code_for(self.code)
        return 0

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "command": self.command, "target": self.target,
            "updated": self.updated, "exit_code": self.exit_code,
        }
        if self.record_id is not None:
            out["id"] = self.record_id
        if self.file is not None:
            out["file"] = self.file
        if self.previous_status is not None:
            out["previous_status"] = self.previous_status
        if self.new_status is not None:
            out["new_status"] = self.new_status
        out["validation_errors"] = list(self.validation_errors)
        if self.code is not None:
            out["code"] = self.code
        if self.message is not None:
            out["message"] = self.message
        return out


def task_transition(raw_target: str | Path | None = None,
                    task_id: str = "",
                    target_status: str = "",
                    *,
                    approval_status: str | None = None,
                    approved_by: str | None = None,
                    approved_at: str | None = None,
                    archived_outcome: str | None = None,
                    result: str | None = None,
                    body: str | None = None,
                    force: bool = False,
                    command_name: str = "task_transition") -> TaskTransitionResult:
    target_label = str(raw_target or ".")
    try:
        target = _canonical_target(raw_target)
    except InitError as exc:
        return TaskTransitionResult(command=command_name, target=target_label,
                                    code=exc.code, message=exc.message)

    from .diagnostics import status_project
    from .execution import INVALID_PROJECT, _health_gate

    try:
        health = status_project(target)
    except (ImportError, OSError, RuntimeError, ValueError):
        return TaskTransitionResult(
            command=command_name, target=str(target), code=INVALID_PROJECT,
            message="The project could not be inspected. Run `tabu doctor` "
                    "and correct the reported project issue.")
    gate = _health_gate(target, health)
    if gate is not None:
        return TaskTransitionResult(command=command_name, target=str(target),
                                    code=gate.code, message=gate.message)

    task_id = str(task_id).strip()
    if not _is_canonical_task_id(task_id):
        return TaskTransitionResult(
            command=command_name, target=str(target), record_id=task_id,
            code=TASK_ID_INVALID,
            message=f"{task_id!r} is not a canonical Task id (expected TASK-YYYY-NNNN).")

    tasks_dir, storage_problem = _tasks_directory(target)
    if storage_problem is not None:
        return TaskTransitionResult(
            command=command_name, target=str(target), record_id=task_id,
            code="TASK_STORAGE_MISSING", message=storage_problem)

    task_path = tasks_dir / f"{task_id}.md"
    if not task_path.exists() or not task_path.is_file():
        return TaskTransitionResult(
            command=command_name, target=str(target), record_id=task_id,
            code=TASK_NOT_FOUND,
            message=f"Task {task_id} was not found under .ai/tasks/.")

    target_status = target_status.strip().upper()
    if target_status not in VALID_TASK_STATUSES:
        return TaskTransitionResult(
            command=command_name, target=str(target), record_id=task_id,
            code=TASK_STATUS_INVALID,
            message=f"Invalid Task status {target_status!r}. "
                    f"Allowed statuses: {', '.join(VALID_TASK_STATUSES)}")

    rec = read_frontmatter(task_path, target, "Task")
    if rec.malformed:
        return TaskTransitionResult(
            command=command_name, target=str(target), record_id=task_id,
            file=f".ai/tasks/{task_id}.md",
            code=MALFORMED_RECORD_QUARANTINED,
            message=f"Task {task_id} is malformed: {rec.malformed_reason}")
    if rec.id_mismatch():
        return TaskTransitionResult(
            command=command_name, target=str(target), record_id=task_id,
            file=f".ai/tasks/{task_id}.md",
            code=FILENAME_ID_MISMATCH,
            message=f"Filename id {rec.filename_id!r} and frontmatter id {rec.declared_id!r} disagree")

    current_status = str(rec.frontmatter.get("status") or "").strip().upper()
    if current_status == target_status and approval_status is None:
        return TaskTransitionResult(
            command=command_name, target=str(target), record_id=task_id,
            file=f".ai/tasks/{task_id}.md", previous_status=current_status,
            new_status=target_status, updated=False,
            message=f"Task {task_id} is already in status {target_status}.")

    # Validate transition
    allowed = SCHEMA_TRANSITIONS.get(current_status, ())
    if not force and target_status not in allowed:
        return TaskTransitionResult(
            command=command_name, target=str(target), record_id=task_id,
            file=f".ai/tasks/{task_id}.md", previous_status=current_status,
            new_status=target_status,
            code=TASK_TRANSITION_INVALID,
            message=f"Cannot transition Task from {current_status} to {target_status}. "
                    f"Allowed transitions: {', '.join(allowed) if allowed else 'none (terminal state)'}. "
                    "Use --force if intentional.")

    new_fm = dict(rec.frontmatter)
    new_fm["status"] = target_status
    new_fm["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if target_status == "ARCHIVED":
        outcome = archived_outcome or ("COMPLETED" if current_status == "COMPLETED" else "CANCELLED" if current_status == "CANCELLED" else None)
        if outcome not in ("COMPLETED", "CANCELLED"):
            return TaskTransitionResult(
                command=command_name, target=str(target), record_id=task_id,
                file=f".ai/tasks/{task_id}.md", previous_status=current_status,
                new_status=target_status,
                code=TASK_TRANSITION_INVALID,
                message="ARCHIVED requires archived_outcome (COMPLETED or CANCELLED) matching previous state.")
        new_fm["archived_outcome"] = outcome
    else:
        new_fm.pop("archived_outcome", None)

    if result is not None:
        new_fm["result"] = result

    if approval_status is not None:
        new_fm["approval_status"] = approval_status
        if approval_status in ("APPROVED", "REJECTED"):
            new_fm["approved_by"] = approved_by or "USER"
            new_fm["approved_at"] = approved_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    elif target_status == "WAITING_APPROVAL":
        new_fm.pop("approval_status", None)
        new_fm.pop("approved_by", None)
        new_fm.pop("approved_at", None)

    doc_body = body if body is not None else rec.body
    document = render_task_document(new_fm, doc_body)
    chk_rec, errors = _parse_document(document, filename=f"{task_id}.md", project_root=target)
    if chk_rec is None or errors or not chk_rec.usable:
        detail = "; ".join(errors) or "updated Task did not satisfy the contract"
        return TaskTransitionResult(
            command=command_name, target=str(target), record_id=task_id,
            file=f".ai/tasks/{task_id}.md", previous_status=current_status,
            new_status=target_status,
            validation_errors=tuple(errors),
            code=INVALID_RECORD_QUARANTINED,
            message=f"The updated Task would not satisfy the contract; nothing was written: {detail}")

    stage_path = tasks_dir / f".trans-stage-{os.getpid()}-{secrets.token_hex(4)}"
    try:
        with open(stage_path, "w", encoding="utf-8") as f:
            f.write(document)
            f.flush()
            os.fsync(f.fileno())
        os.replace(stage_path, task_path)
        with suppress(OSError):
            dir_fd = os.open(tasks_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except PermissionError:
        return TaskTransitionResult(
            command=command_name, target=str(target), record_id=task_id,
            file=f".ai/tasks/{task_id}.md", previous_status=current_status,
            new_status=target_status, code="PERMISSION_DENIED",
            message="Permission denied while updating Task in .ai/tasks/.")
    except OSError:
        return TaskTransitionResult(
            command=command_name, target=str(target), record_id=task_id,
            file=f".ai/tasks/{task_id}.md", previous_status=current_status,
            new_status=target_status, code="WRITE_FAILED",
            message="The Task update could not be written to .ai/tasks/; no partial record was left behind.")
    finally:
        with suppress(OSError):
            stage_path.unlink()

    verification = read_frontmatter(task_path, target, "Task")
    if not verification.usable or verification.declared_id != task_id:
        return TaskTransitionResult(
            command=command_name, target=str(target), record_id=task_id,
            file=f".ai/tasks/{task_id}.md", previous_status=current_status,
            new_status=target_status,
            code=TASK_TRANSITION_VERIFICATION_FAILED,
            message=f"Task {task_id} was updated but verification re-read failed.")

    return TaskTransitionResult(
        command=command_name, target=str(target), record_id=task_id,
        file=f".ai/tasks/{task_id}.md", previous_status=current_status,
        new_status=target_status, updated=True,
        message=f"Transitioned {task_id} from {current_status} to {target_status}.")


def task_start(raw_target: str | Path | None = None, task_id: str = "") -> TaskTransitionResult:
    """Start work on a Task (advance to IN_PROGRESS)."""
    target_label = str(raw_target or ".")
    try:
        target = _canonical_target(raw_target)
    except InitError as exc:
        return TaskTransitionResult(command="task_start", target=target_label,
                                    code=exc.code, message=exc.message)
    tasks_dir, storage_problem = _tasks_directory(target)
    if storage_problem is not None:
        return TaskTransitionResult(command="task_start", target=str(target),
                                    record_id=task_id, code="TASK_STORAGE_MISSING",
                                    message=storage_problem)
    task_path = tasks_dir / f"{str(task_id).strip()}.md"
    if not task_path.exists() or not task_path.is_file():
        return TaskTransitionResult(command="task_start", target=str(target),
                                    record_id=task_id, code=TASK_NOT_FOUND,
                                    message=f"Task {task_id} was not found under .ai/tasks/.")
    rec = read_frontmatter(task_path, target, "Task")
    current_status = str(rec.frontmatter.get("status") or "").strip().upper()
    if current_status in TERMINAL_TASK_STATUSES:
        return TaskTransitionResult(
            command="task_start", target=str(target), record_id=task_id,
            file=f".ai/tasks/{task_id}.md", previous_status=current_status,
            new_status=current_status, code=TASK_TRANSITION_INVALID,
            message=f"Cannot start Task {task_id}: task is already in terminal status {current_status}.")
    if current_status == "WAITING_APPROVAL" and rec.frontmatter.get("approval_status") != "APPROVED":
        return TaskTransitionResult(
            command="task_start", target=str(target), record_id=task_id,
            file=f".ai/tasks/{task_id}.md", previous_status=current_status,
            new_status=current_status, code=TASK_TRANSITION_INVALID,
            message=f"Task {task_id} is WAITING_APPROVAL and has not been approved. "
                    f"Run `tabu task approve {task_id}` before starting.")
    return task_transition(raw_target, task_id, "IN_PROGRESS", force=True, command_name="task_start")


def task_approve(raw_target: str | Path | None = None, task_id: str = "", *,
                 by: str = "USER") -> TaskTransitionResult:
    """Record human approval for a Task."""
    target_label = str(raw_target or ".")
    try:
        target = _canonical_target(raw_target)
    except InitError as exc:
        return TaskTransitionResult(command="task_approve", target=target_label,
                                    code=exc.code, message=exc.message)
    tasks_dir, storage_problem = _tasks_directory(target)
    if storage_problem is not None:
        return TaskTransitionResult(command="task_approve", target=str(target),
                                    record_id=task_id, code="TASK_STORAGE_MISSING",
                                    message=storage_problem)
    task_path = tasks_dir / f"{str(task_id).strip()}.md"
    if not task_path.exists() or not task_path.is_file():
        return TaskTransitionResult(command="task_approve", target=str(target),
                                    record_id=task_id, code=TASK_NOT_FOUND,
                                    message=f"Task {task_id} was not found under .ai/tasks/.")
    rec = read_frontmatter(task_path, target, "Task")
    current_status = str(rec.frontmatter.get("status") or "").strip().upper()
    if current_status in TERMINAL_TASK_STATUSES:
        return TaskTransitionResult(
            command="task_approve", target=str(target), record_id=task_id,
            file=f".ai/tasks/{task_id}.md", previous_status=current_status,
            new_status=current_status, code=TASK_TRANSITION_INVALID,
            message=f"Cannot approve Task {task_id}: task is in terminal status {current_status}.")

    target_status = "IN_PROGRESS" if current_status == "WAITING_APPROVAL" else current_status
    return task_transition(raw_target, task_id, target_status,
                           approval_status="APPROVED", approved_by=by or "USER",
                           force=True, command_name="task_approve")


def task_complete(raw_target: str | Path | None = None, task_id: str = "") -> TaskTransitionResult:
    """Mark an active Task as COMPLETED."""
    target_label = str(raw_target or ".")
    try:
        target = _canonical_target(raw_target)
    except InitError as exc:
        return TaskTransitionResult(command="task_complete", target=target_label,
                                    code=exc.code, message=exc.message)
    tasks_dir, storage_problem = _tasks_directory(target)
    if storage_problem is not None:
        return TaskTransitionResult(command="task_complete", target=str(target),
                                    record_id=task_id, code="TASK_STORAGE_MISSING",
                                    message=storage_problem)
    task_path = tasks_dir / f"{str(task_id).strip()}.md"
    if not task_path.exists() or not task_path.is_file():
        return TaskTransitionResult(command="task_complete", target=str(target),
                                    record_id=task_id, code=TASK_NOT_FOUND,
                                    message=f"Task {task_id} was not found under .ai/tasks/.")
    rec = read_frontmatter(task_path, target, "Task")
    current_status = str(rec.frontmatter.get("status") or "").strip().upper()
    if current_status == "COMPLETED":
        return TaskTransitionResult(
            command="task_complete", target=str(target), record_id=task_id,
            file=f".ai/tasks/{task_id}.md", previous_status=current_status,
            new_status="COMPLETED", updated=False,
            message=f"Task {task_id} is already COMPLETED.")
    if current_status not in ("IN_PROGRESS", "REVIEW"):
        return TaskTransitionResult(
            command="task_complete", target=str(target), record_id=task_id,
            file=f".ai/tasks/{task_id}.md", previous_status=current_status,
            new_status=current_status, code=TASK_TRANSITION_INVALID,
            message=f"Cannot complete Task {task_id} from status {current_status}. "
                    "Only IN_PROGRESS or REVIEW tasks can be completed.")
    return task_transition(raw_target, task_id, "COMPLETED", force=True, command_name="task_complete")


# --- rendering ---------------------------------------------------------------

_STATE_LABELS = {ACTIVE: "active", TERMINAL: "terminal", UNUSABLE: "unusable"}


def _bound(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text = _sanitize_fragment(value, limit=limit)
    return text or None


def render_human(result: TaskListResult) -> str:
    if result.code is not None:
        return f"{result.code}: {result.message}\nTarget: {result.target}"
    lines = [f"Tasks in {result.target}",
             f"Total: {result.total_count}"]
    if not result.total_count:
        lines.append("No Task records were found under .ai/tasks/.")
    for task in result.tasks:
        label = _bound(task.id, 64) or f"(unnamed file {_bound(task.file, 64)})"
        line = f"- {label} [{_STATE_LABELS[task.state]}]"
        title = _bound(task.title, 64)
        if title:
            line += f" {title}"
        if task.status:
            line += f" ({_bound(task.status, 32)})"
        lines.append(line)
        if task.reason:
            lines.append(f"  reason: {_bound(task.reason, 160)}")
    if result.omitted_count:
        lines.append(f"- (and {result.omitted_count} more Task records)")
    lines.append(f"Result: {result.total_count} Task record(s)")
    return "\n".join(lines)


def render_json(result: TaskListResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)


def render_human_show(result: TaskShowResult) -> str:
    if not result.found:
        return f"{result.code}: {result.message}\nTarget: {result.target}"
    lines = [f"Task {result.requested_id}",
             f"File: {result.file}",
             f"Valid: {'yes' if result.valid else 'no'}"]
    if result.terminal:
        lines.append("State: terminal (cannot be selected for new runs)")
    if result.title:
        lines.append(f"Title: {_bound(result.title, 200)}")
    if result.status:
        lines.append(f"Status: {_bound(result.status, 32)}")
    if result.errors:
        lines.append("Defects:")
        lines.extend(f"- {_bound(error, 160)}" for error in result.errors)
    if result.frontmatter is not None:
        lines.append("")
        lines.append("---")
        lines.append(yaml.safe_dump(result.frontmatter,
                                    sort_keys=False, allow_unicode=True)
                     .rstrip("\n"))
        lines.append("---")
    if result.body is not None:
        lines.append("")
        lines.append(result.body)
        if result.body_truncated:
            lines.append(f"[body truncated at {_SHOW_BODY_LIMIT} characters]")
    elif not result.valid:
        lines.append("")
        lines.append("(content unavailable: the record did not satisfy the "
                     "stored-format contract, so the loader extracted no "
                     "trustworthy body)")
    return "\n".join(lines)


def render_json_show(result: TaskShowResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)


def render_human_validate(result: TaskValidateResult) -> str:
    if result.code is not None:
        return f"{result.code}: {result.message}\nTarget: {result.target}"
    scope = (f"id {result.requested_id}"
             if result.requested_id else "all Task records")
    lines = [f"Task validation ({scope}) in {result.target}",
             f"Valid: {result.valid_count}, invalid: {result.invalid_count}"]
    if not result.total_count:
        lines.append("No Task records were found under .ai/tasks/.")
    for entry in result.entries:
        marker = "OK  " if entry.valid else "FAIL"
        label = _bound(entry.id, 64) or \
            f"(unnamed file {_bound(entry.file, 64)})"
        line = f"[{marker}] {label}"
        if entry.error_codes:
            line += f" ({', '.join(entry.error_codes)})"
        lines.append(line)
        for error in entry.errors:
            lines.append(f"       {_bound(error, 160)}")
    if result.omitted_count:
        lines.append(f"(and {result.omitted_count} more Task records)")
    verdict = "VALID" if result.invalid_count == 0 else "INVALID"
    lines.append(f"Result: {verdict}")
    return "\n".join(lines)


def render_json_validate(result: TaskValidateResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)


def render_human_create(result: TaskCreateResult) -> str:
    if result.code is not None:
        head = f"{result.code}: {result.message}\nTarget: {result.target}"
        if result.validation_errors:
            lines = [head, "Contract evidence:"]
            lines.extend(f"- {error}" for error in
                         result.validation_errors[:_UNUSABLE_DETAIL_LIMIT])
            return "\n".join(lines)
        return head
    if result.print_only:
        lines = [
            f"Candidate Task for {result.target}",
            f"Id: {result.record_id} (candidate only — NOT reserved)",
            f"Would be stored at: {result.file}",
            "Valid against the stored contract: yes",
            "No file was written; no state was changed.",
            "",
            result.content or "",
        ]
        return "\n".join(lines)
    lines = [
        f"Created Task {result.record_id}",
        f"File: {result.file}",
        result.message or "",
    ]
    return "\n".join(line for line in lines if line != "")


def render_json_create(result: TaskCreateResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)


def render_human_transition(result: TaskTransitionResult) -> str:
    if result.code is not None:
        head = f"{result.code}: {result.message}\nTarget: {result.target}"
        if result.validation_errors:
            lines = [head, "Contract evidence:"]
            lines.extend(f"- {error}" for error in
                         result.validation_errors[:_UNUSABLE_DETAIL_LIMIT])
            return "\n".join(lines)
        return head
    if not result.updated:
        return f"Task {result.record_id}: {result.message}\nFile: {result.file}"
    lines = [
        f"Updated Task {result.record_id}",
        f"File: {result.file}",
        f"State transition: {result.previous_status} -> {result.new_status}",
    ]
    if result.message:
        lines.append(result.message)
    return "\n".join(lines)


def render_json_transition(result: TaskTransitionResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)
