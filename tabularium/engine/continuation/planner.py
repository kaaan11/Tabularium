"""Deterministic continuation planning (M4.5). READ-ONLY.

Turns an execution outcome plus an explicit continuation plan into
`PersistenceProposal`s for the supported durable families. Everything here is
deterministic and extractive:

    * content is assembled from caller-supplied strings and transport fields;
    * no second model is ever called to summarize a worker result;
    * record ids are allocated by scanning existing project records, never
      minted at random.

This module reads the project (to allocate the next id and to read a Task's
current content) but WRITES NOTHING. A source scan asserts it holds no
open/write/replace path — durable writes live only in `persistence.writer`.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from ..persistence import model as P
from . import model as M


def build_proposals(outcome: M.ExecutionOutcome, plan: M.ContinuationPlan,
                    project_root: str) -> list[M.PreparedProposal]:
    """Prepare every requested durable artifact. Read-only.

    Returns one `PreparedProposal` per requested family, in a stable order.
    An empty plan returns an empty list; the aggregate then reports
    NO_DURABLE_VALUE.
    """
    prepared: list[M.PreparedProposal] = []
    if plan.handoff is not None:
        prepared.append(_handoff(outcome, plan.handoff, project_root))
    if plan.report is not None:
        prepared.append(_report(outcome, plan.report, project_root))
    if plan.task_update is not None:
        prepared.append(_task_update(outcome, plan.task_update, project_root))
    return prepared


# --- Handoff ----------------------------------------------------------------

def _handoff(outcome: M.ExecutionOutcome, spec: M.HandoffSpec,
             project_root: str) -> M.PreparedProposal:
    if not _nonempty(spec.next_step):
        return _err(M.HANDOFF, M.HANDOFF_CONTENT_REQUIRED,
                    "a Handoff needs an explicit next step")
    if not _nonempty(spec.title) or not _nonempty(spec.created_by):
        return _err(M.HANDOFF, M.HANDOFF_CONTENT_REQUIRED,
                    "title and created_by are required")
    if not _nonempty(outcome.task_id):
        return _err(M.HANDOFF, M.HANDOFF_CONTENT_REQUIRED,
                    "a Handoff is Task-bound; task_id is required")
    if not P.valid_timestamp(spec.created_at):
        return _err(M.HANDOFF, M.HANDOFF_CONTENT_REQUIRED,
                    "created_at must be ISO 8601 with an explicit offset")

    body = _handoff_body(outcome, spec)
    if len(body) > M.MAX_HANDOFF_BODY_CHARS:
        return _err(M.HANDOFF, M.HANDOFF_CONTENT_REQUIRED,
                    f"generated Handoff body exceeds {M.MAX_HANDOFF_BODY_CHARS} "
                    "chars; supply a more concise summary")

    year = _year(spec.created_at)
    allocated = _next_id(project_root, M.HANDOFF, year)
    if allocated is None:
        return _err(M.HANDOFF, M.RECORD_ID_COLLISION,
                    "could not allocate a unique Handoff id from "
                    ".ai/handoffs; restore read access or free the id space")
    record_id, collision = allocated
    if collision:
        return _err(M.HANDOFF, M.RECORD_ID_COLLISION,
                    f"allocated id {record_id} already exists")

    fields = {
        "schema_version": "1.0", "id": record_id, "title": spec.title,
        "task": outcome.task_id, "created_at": spec.created_at,
        "created_by": spec.created_by,
    }
    if _nonempty(spec.intended_recipient):
        fields["intended_recipient"] = spec.intended_recipient
    if _nonempty(spec.previous_handoff):
        fields["previous_handoff"] = spec.previous_handoff
    lists = {"investigations": spec.investigations, "decisions": spec.decisions,
             "evidence": spec.evidence, "questions": spec.questions}
    content = _document(fields, lists, body)

    return _proposal(M.HANDOFF, P.CREATE, outcome, spec, record_id, content,
                     spec.next_step)


def _handoff_body(outcome: M.ExecutionOutcome, spec: M.HandoffSpec) -> str:
    """Assemble the frozen `## Current State` / `## Continuation` body.

    Provenance the Handoff schema has no structured field for — the source
    execution, worker identity, and runtime outcome — is carried NARRATIVELY,
    as the contract intends.
    """
    lines = [f"# {spec.title}", "", "## Current State", ""]
    lines.append(f"- Source execution: {outcome.execution_id or 'UNKNOWN'}")
    lines.append(f"- Task: {outcome.task_id}")
    if _nonempty(outcome.worker_id) or _nonempty(outcome.worker_category):
        worker = outcome.worker_id or "UNKNOWN"
        category = outcome.worker_category or "UNKNOWN"
        lines.append(f"- Worker: {worker} ({category})")
    if _nonempty(outcome.runtime_status):
        lines.append(f"- Runtime outcome: {outcome.runtime_status}")
    lines.append("")
    lines.append(f"Attempted: {_or_unrecorded(spec.what_attempted)}")
    lines.append("")
    lines.append(f"Completed: {_or_unrecorded(spec.what_completed)}")
    lines.append("")
    lines.append(f"Remaining: {_or_unrecorded(spec.what_remains)}")
    lines.append("")
    lines.append(f"Limitations: {_or_unrecorded(spec.limitations)}")
    lines.append("")
    lines.append("## Continuation")
    lines.append("")
    lines.append(f"Next step: {spec.next_step}")
    lines.append("")
    return "\n".join(lines)


# --- Report -----------------------------------------------------------------

def _report(outcome: M.ExecutionOutcome, spec: M.ReportSpec,
            project_root: str) -> M.PreparedProposal:
    if not _nonempty(spec.outcome):
        return _err(M.REPORT, M.REPORT_CONTENT_REQUIRED,
                    "a Report needs an explicit outcome summary")
    if not _nonempty(spec.title) or not _nonempty(spec.created_by):
        return _err(M.REPORT, M.REPORT_CONTENT_REQUIRED,
                    "title and created_by are required")
    if not _nonempty(outcome.task_id):
        return _err(M.REPORT, M.REPORT_CONTENT_REQUIRED,
                    "a Report is Task-bound; task_id is required")
    if not P.valid_timestamp(spec.created_at):
        return _err(M.REPORT, M.REPORT_CONTENT_REQUIRED,
                    "created_at must be ISO 8601 with an explicit offset")

    year = _year(spec.created_at)
    allocated = _next_id(project_root, M.REPORT, year)
    if allocated is None:
        return _err(M.REPORT, M.RECORD_ID_COLLISION,
                    "could not allocate a unique Report id from "
                    ".ai/reports; restore read access or free the id space")
    record_id, collision = allocated
    if collision:
        return _err(M.REPORT, M.RECORD_ID_COLLISION,
                    f"allocated id {record_id} already exists")

    body = _report_body(outcome, spec)
    fields = {
        "schema_version": "1.0", "id": record_id, "title": spec.title,
        "task": outcome.task_id, "created_at": spec.created_at,
        "created_by": spec.created_by,
    }
    if _nonempty(spec.previous_report):
        fields["previous_report"] = spec.previous_report
    lists = {"investigations": spec.investigations, "decisions": spec.decisions,
             "evidence": spec.evidence, "questions": spec.questions,
             "handoffs": spec.handoffs}
    content = _document(fields, lists, body)

    return _proposal(M.REPORT, P.CREATE, outcome, spec, record_id, content, "")


def _report_body(outcome: M.ExecutionOutcome, spec: M.ReportSpec) -> str:
    """Assemble the frozen `## Outcome` / `## Validation` / `## Limitations`."""
    lines = [f"# {spec.title}", "", "## Outcome", "", spec.outcome, ""]
    prov = (f"Source execution: {outcome.execution_id or 'UNKNOWN'}. "
            f"Runtime outcome: {outcome.runtime_status or 'UNKNOWN'}.")
    if _nonempty(outcome.worker_id):
        prov += f" Worker: {outcome.worker_id} ({outcome.worker_category or 'UNKNOWN'})."
    lines += [prov, ""]
    lines += ["## Validation", "", _or_unrecorded(spec.validation), ""]
    lines += ["## Limitations", "", _or_unrecorded(spec.limitations), ""]
    return "\n".join(lines)


# --- Task update ------------------------------------------------------------

def _task_update(outcome: M.ExecutionOutcome, spec: M.TaskUpdateSpec,
                 project_root: str) -> M.PreparedProposal:
    """Prepare an explicit Task state change from CALLER-SUPPLIED content.

    The full replacement record is provided by the caller — the planner never
    fabricates Task fields and never derives a status from the execution. It
    reads the current record only to bind the update to the state it was built
    against; the frozen validator and the M4.4 stale-target check do the rest.
    """
    if not _nonempty(spec.new_content):
        return _err(M.TASK, M.TASK_UPDATE_INVALID,
                    "a Task update needs complete replacement content")
    if not _nonempty(spec.expected_current_digest):
        return _err(M.TASK, M.TASK_UPDATE_INVALID,
                    "a Task update needs the expected current digest")

    record_id = _extract_id(spec.new_content)
    if record_id is None or not P.FAMILY_ID_PATTERN[M.TASK].match(record_id):
        return _err(M.TASK, M.TASK_UPDATE_INVALID,
                    "replacement content has no canonical Task id")
    if _nonempty(outcome.task_id) and record_id != outcome.task_id:
        return _err(M.TASK, M.TASK_UPDATE_INVALID,
                    "replacement Task id does not match the execution's task")

    target = _target_path(M.TASK, record_id)
    current = Path(project_root) / target
    if not current.is_file():
        return _err(M.TASK, M.TASK_UPDATE_INVALID,
                    f"Task {record_id} does not exist to update")

    return M.PreparedProposal(
        family=M.TASK,
        proposal=P.PersistenceProposal(
            proposal_id=f"{outcome.execution_id}:continuation-task-{record_id}",
            execution_id=outcome.execution_id, task_id=record_id,
            target_family=M.TASK, operation=P.UPDATE,
            target_path=target, content=spec.new_content, record_id=record_id,
            reason="explicit human Task state change via continuation flow",
            effective_persistence=P.PROPOSE,
            created_from={"source": "continuation", "family": M.TASK},
            expected_current_digest=spec.expected_current_digest))


# --- shared -----------------------------------------------------------------

def _proposal(family, operation, outcome, spec, record_id, content,
              next_step) -> M.PreparedProposal:
    target = _target_path(family, record_id)
    return M.PreparedProposal(
        family=family,
        proposal=P.PersistenceProposal(
            proposal_id=f"{outcome.execution_id}:continuation-{record_id}",
            execution_id=outcome.execution_id, task_id=outcome.task_id,
            target_family=family, operation=operation,
            target_path=target, content=content, record_id=record_id,
            reason=f"explicit continuation {family} for execution "
                   f"{outcome.execution_id}",
            effective_persistence=P.PROPOSE,
            created_from={"source": "continuation", "family": family,
                          "next_step": next_step}))


def _document(scalars: dict, lists: dict, body: str) -> str:
    """Emit YAML frontmatter + Markdown body the FROZEN validator accepts.

    Scalars are quoted so `schema_version` stays a string and offsets survive;
    lists are emitted only when non-empty. Deterministic key order.
    """
    out = ["---"]
    for key, value in scalars.items():
        out.append(f'{key}: "{_escape(str(value))}"')
    for key, items in lists.items():
        cleaned = [i for i in items if _nonempty(i)]
        if cleaned:
            out.append(f"{key}:")
            out.extend(f'  - "{_escape(str(i))}"' for i in cleaned)
    out.append("---")
    text = "\n".join(out) + "\n" + body
    return text


def _next_id(project_root: str, family: str, year: int
             ) -> tuple[str, bool] | None:
    """Allocate the next id by scanning existing records. Read-only.

    Returns (id, collision). `collision` is True if the allocated target
    somehow already exists — the caller fails closed rather than overwriting.
    Reuses the frozen id format; no random UUID.

    Returns None, and the caller refuses, when NO canonical id can be
    allocated safely: the family directory exists but cannot be listed
    (an enumeration failure is never treated as empty storage), or the
    frozen four-digit sequence for this year is exhausted.
    """
    prefix = {M.HANDOFF: "HND", M.REPORT: "RPT"}.get(family)
    if prefix is None:
        return None
    directory = Path(project_root) / ".ai" / P.FAMILY_DIRECTORY[family]
    highest = 0
    pattern = re.compile(rf"^{prefix}-{year:04d}-([0-9]{{4}})\.md$")
    if directory.is_dir():
        try:
            # iterdir, not glob: a listing failure must surface here. glob
            # silently treats an unreadable directory as empty, which would
            # reallocate an id that already exists behind it.
            children = list(directory.iterdir())
        except OSError:
            return None
        for child in children:
            match = pattern.match(child.name)
            if match:
                highest = max(highest, int(match.group(1)))
    if highest >= 9999:
        return None
    record_id = f"{prefix}-{year:04d}-{highest + 1:04d}"
    target = directory / f"{record_id}.md"
    return record_id, target.exists()


def _target_path(family: str, record_id: str) -> str:
    return f".ai/{P.FAMILY_DIRECTORY[family]}/{record_id}.md"


def _extract_id(content: str) -> str | None:
    """Read the `id:` from the frontmatter of caller-supplied content.

    Scans only the first frontmatter block (between the opening `---` and its
    close). Returns None if absent.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if stripped.startswith("id:"):
            return stripped[3:].strip().strip('"').strip("'") or None
    return None


def _year(timestamp: str) -> int:
    return datetime.fromisoformat(timestamp.strip().replace("Z", "+00:00")).year


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _or_unrecorded(value: str) -> str:
    return value.strip() if _nonempty(value) else "Not recorded."


def _err(family: str, code: str, detail: str) -> M.PreparedProposal:
    return M.PreparedProposal(family=family, reason_code=code, detail=detail)
