# Report Template

**STATUS: EXAMPLE ONLY — NOT AUTOMATION**

## Purpose

A **Report** is an **immutable, Task-bound, outcome-oriented durable summary**
of work. It records what occurred, what resulted, what was checked or observed,
and what limitations remain without relying on chat history.

It does not establish Task completion, approval, authority, a Decision,
Evidence, validation truth, or execution authorization.

| Document | Holds |
| --- | --- |
| `tabularium/schemas/report.schema.yaml` (in the Tabularium package) | The contract (v1.0) — fields and invariants |
| `report.md` | Canonical record example (illustrative and fictional) |

## Storage and record format

```text
.ai/reports/RPT-YYYY-NNNN.md
```

The filename must equal the Report id. Records use Markdown with YAML
frontmatter: frontmatter is machine-authoritative metadata and the body is the
durable outcome narrative. Project membership is positional; Report has no
`project` field and no cross-project syntax.

## Exactly one Task

Every Report references exactly one Task through required singular `task`.
There is no unbound or multi-Task Report in v1.0. The reference does not mark
the Task complete, change its status, or create reciprocity.

## Keep records small

Six fields are required: `schema_version`, `id`, `title`, `task`, `created_at`,
and `created_by`.

```markdown
---
schema_version: "1.0"
id: RPT-2026-0002
title: Documentation update outcome
task: TASK-2026-0002
created_at: "2026-08-20T18:00:00Z"
created_by: model-A
---

# Report

## Outcome
...

## Validation
...

## Limitations
...
```

Optional fields are `previous_report`, `investigations`, `decisions`,
`evidence`, `questions`, and `handoffs`.

There is no `status`, `updated_at`, `report_type`, `validation_status`, outcome
enum, `success`, `files_changed`, approval metadata, or model/provider/role
metadata.

## Required body sections

- **`## Outcome`** — work performed, results, conclusions, failures, skipped
  work, and material omissions.
- **`## Validation`** — narrative account of what was checked, observed, not
  checked, and what uncertainty remains.
- **`## Limitations`** — residual uncertainty, constraints, omissions, and
  relevant unanswered Questions.

`## Next Steps` is optional. It does not assign or transfer work, create a
Task, expand scope, or authorize execution.

File paths and changes may be mentioned in the body when useful. There is no
structured `files_changed` field; Task `actual_changes` and repository state
retain their own semantics.

## No lifecycle and fully immutable

Report has no lifecycle. Creating the stored record is publication of the
durable artifact; an unfinished draft is not yet a Report.

All frontmatter and body content is immutable after creation. There is no
`updated_at`. A correction or materially changed outcome requires a new
Report.

## Lineage, not supersession

Optional `previous_report` records lineage only. It does not establish
supersession, invalidation, replacement, precedence, reciprocity,
current-effective state, or authority.

Nothing guarantees that the referenced Report exists, belongs to the same
Task, is literally the immediately previous Report, or forms an acyclic chain.
No resolver or Report supersession mechanism exists.

## Assertions are not Evidence

A Report saying “all tests passed” does not establish that claim as truth or
Evidence. Material claims may reference Evidence records. Referencing Evidence
does not copy it, change reliability or `source_type`, or mutate it, and Report
creation creates no Evidence automatically.

Report conclusions are not Decisions, and publication is not approval.

## Current state

- `report.md` is illustrative and fictional.
- No ID allocation, validation, resolver, lifecycle enforcement, publication
  workflow, Task automation, supersession, identity model, or orchestration is
  implemented.
- Nothing checks that a filename matches its `id` or that required body
  sections exist.
