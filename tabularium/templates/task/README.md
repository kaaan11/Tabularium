# Task Template

**STATUS: EXAMPLE ONLY — NOT AUTOMATION**

## Purpose

A TASK is a single unit of work and the most specific layer of the hierarchy:

```
CONSTITUTION > CORE PROTOCOLS > POLICIES > DOMAIN > PROJECT > TASK
```

The Task object now has a formal contract:

| Document | Holds |
| --- | --- |
| `tabularium/schemas/task.schema.yaml` (in the Tabularium package) | The contract (v1.10) — fields, types, allowed values, state machine |
| `task.md` | **Canonical** stored record: Markdown with YAML frontmatter |
| `task.yaml` | The same example as plain fields, for reading the contract against |

## Storage and record format

Task records belong in **`.ai/tasks/`** within a project's AI context
(`../project/README.md`), at the canonical path:

```
.ai/tasks/TASK-YYYY-NNNN.md
```

**The filename MUST equal the Task id** — `.ai/tasks/TASK-2026-0001.md`. A
record whose filename and `id` disagree is invalid.

The canonical stored form is a **Markdown document with YAML frontmatter**:

- **Frontmatter** — machine-authoritative metadata. Every contract field lives
  here.
- **Body** — human- and model-readable narrative. Defines no fields.

An authoritative field must not have a second authoritative copy in the body.
The body may explain a concept narratively, but must not restate an id, an
enumerated value, a timestamp, or a reference as though it were the record.
Where the two appear to disagree, the frontmatter governs.

## Tasks and `tabu run`

`tabu run` selects one active Task automatically when exactly one qualifies,
and refuses with `--task` guidance when several qualify. A Task qualifies when
the record is usable and its `status` is not terminal (`COMPLETED`,
`CANCELLED`, `ARCHIVED`) — a new `INBOX` Task qualifies. When a run is refused
because no active Task exists, create a record here by hand following the
format above; the filename must equal the id. A malformed or contract-violating
record is quarantined: it is reported with the offending field, never used for
a run, and never repaired — correct the file in place. Task creation tooling
and automatic ID allocation remain **NOT IMPLEMENTED**; records are
hand-authored.

## Governing rules

- Task-level instructions apply only to the task at hand and carry no authority
  beyond it.
- A task instruction may not override a project rule, a policy, or an immutable
  principle.
- The requested scope of a task is the deliverable; it is not silently narrowed
  or widened.
- Work is reported faithfully, including what was not done.

## Keep simple tasks simple

Only seven fields are unconditionally required: `schema_version`, `id`,
`title`, `objective`, `status`, `created_at`, `created_by`. An ARCHIVED Task
also requires `archived_outcome`; a trivial non-archived Task carries only the
seven unconditional fields:

```markdown
---
schema_version: "1.10"
id: TASK-2026-0002
title: Fix typo in README.md
objective: Correct the misspelled word in the installation section.
status: INBOX
created_at: "2026-08-16T10:00:00Z"
created_by: USER
---

# Task
```

`type` is optional and omitted here; when recorded it must be one of
`GENERAL`, `RESEARCH`, `IMPLEMENTATION`, `DEBUGGING`, `REVIEW`, `ANALYSIS`.

## Archived outcome is conditionally required

`archived_outcome` has exactly two values: `COMPLETED` and `CANCELLED`. It is
required when `status` is `ARCHIVED` and forbidden for every other status.

- `COMPLETED` → `ARCHIVED` records `archived_outcome: COMPLETED`.
- `CANCELLED` → `ARCHIVED` records `archived_outcome: CANCELLED`.

The value is immutable after archival. It preserves only which terminal state
preceded `ARCHIVED`; it is not general state history, result detail, approval,
Report outcome, or an archive/cancellation reason. `result`, `report`, and
`reports` remain optional.

## Approval provenance is conditionally required

`approved_by` and `approved_at` are one pair, keyed to `approval_status`:

| `approval_status` | `approved_by` / `approved_at` |
| --- | --- |
| `APPROVED` | **MUST be present** |
| `NOT_REQUIRED` / `PENDING` / `REJECTED` | MUST be absent |

One without the other is invalid in either direction. Human approval must not
be representable without attributable provenance, and provenance must not
appear on a Task that was never approved.

`approved_by` is an **opaque actor reference** — no identity system, authority
registry, permission model, or delegation exists. Recording who approved is not
the same as establishing that they were authorized.

The pair is the **current effective approval** — not an approval history, not
an event log, not a cumulative list of approvers.

While it describes that same approval it is not rewritten to show a different
actor or time. If the Task returns to `PENDING` the current approval no longer
exists and the pair is cleared; a later re-approval writes a **new** pair. The
earlier approval is not retained in the record, and **historical
approval-event retention is NOT IMPLEMENTED** — there is no approval history,
and version control is not the normative record of prior approvals.

The optional fields exist for complex or risk-bearing work. They are not a
checklist. Do not fill a field with invented information to make a Task look
complete — an omitted field means NOT RECORDED, and UNKNOWN is preferable to
fabrication.

`task.md` shows the other end of the range: an approved, in-progress `DEBUGGING`
Task carrying priority, risk, uncertainty, plan, approved scope, approval
provenance, and relationships.

## Current state

- `task.md` and `task.yaml` are illustrative examples, not forms and not
  generator inputs.
- There is no task creation tooling, ID allocation, write-time state
  enforcement, or general schema engine. M2.4's read-only consumer validator
  checks the frozen Task fields it consumes, including filename/`id` equality;
  it is APPROVED. M2 is a freeze candidate and is not frozen until an explicit freeze approval and tag.
- Task schema migration is **NOT IMPLEMENTED**; existing schema versions may
  require future migration procedures.
- `approved_by` is an opaque identifier. No identity model exists yet.
