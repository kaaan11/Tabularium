# Handoff Template

**STATUS: EXAMPLE ONLY — NOT AUTOMATION**

## Purpose

A **Handoff** is an **immutable, Task-bound, point-in-time continuation
snapshot** containing the minimum state and guidance another actor needs to
safely continue work.

It transfers **information**. It does not transfer authority, approval, Task
ownership, scope authorization, truth, proof of completion, or proof of
validation.

| Document | Holds |
| --- | --- |
| `tabularium/schemas/handoff.schema.yaml` (in the Tabularium package) | The contract (v1.0) — fields and allowed values |
| `handoff.md` | Canonical record example (illustrative, fictional) |

## Task-bound

Every Handoff references **exactly one** Task, and `task` is required. There is
no multi-Task handoff and no project-level unbound handoff in v1.0.

Referencing the Task does not assign it, change its status, or alter its
approved scope.

## No lifecycle

Handoff has **no status field and no lifecycle**. Creating the record *is*
issuing the artifact.

There is no `DRAFT`, `ISSUED`, `RECEIVED`, `ACKNOWLEDGED`, `ACCEPTED`,
`REJECTED`, `COMPLETED`, or `ARCHIVED`. Acknowledgment, routing, receipt,
retries, and queues belong to future orchestration — none of it exists.

## Immutable

A Handoff is **immutable after creation** — all frontmatter and all body
content. There is deliberately no `updated_at`, because nothing changes.

**Corrections are never made in place.** If the state changed, or the snapshot
was wrong, write a **new** Handoff.

## Storage and record format

```
.ai/handoffs/HND-YYYY-NNNN.md
```

**The filename MUST equal the Handoff id.** Markdown with YAML frontmatter:
frontmatter is machine-authoritative, the body is the snapshot. Project
membership is **positional** — no `project` field, and no cross-project
transfer syntax.

## Keep records cheap

Six fields are required: `schema_version`, `id`, `title`, `task`, `created_at`,
`created_by`.

```markdown
---
schema_version: "1.0"
id: HND-2026-0002
title: Build-runner investigation, pausing overnight
task: TASK-2026-0002
created_at: "2026-08-19T18:00:00Z"
created_by: model-A
---

# Handoff

## Current State
...

## Continuation
...
```

Optional: `intended_recipient`, `previous_handoff`, `investigations`,
`decisions`, `evidence`, `questions`.

## The body is the substance

Two sections are required:

- **`## Current State`** — only the bounded state needed to continue: work
  done, work remaining, known blockers, relevant task-specific context.
- **`## Continuation`** — the next safe action, plus prerequisites and cautions.

The body **may** mention validation claims, paths, changed files, and
observations where useful. Those stay **narrative assertions**. They are never
promoted into machine-authoritative fields: there is no `files_changed`,
`validation`, `completion_status`, `status`, `scope`, `priority`, `risk`, or
`approval` field, and the Task's own state is not duplicated here.

## Everything stated is a transferred assertion

A Handoff saying **"tests passed"** does not establish that tests passed — only
that the producer said so. A Handoff saying **"implementation complete"** does
not mark the Task complete.

A Handoff **does not become Evidence** and creates none. Where real
evidentiary support exists, reference Evidence records through `evidence`;
their `source_type` and reliability stay theirs.

## Direction

`created_by` is the producing actor — there is no separate `sender` field.

`intended_recipient` is **optional and opaque**. The contract does not decide
whether it names a human, an agent, a model, a role, a worker, or a reviewer,
and no role semantics may be inferred. It does not assign the Task, grant
authority, prove receipt, or prove acceptance. A Handoff without one is fully
valid.

## Lineage, not supersession

`previous_handoff` says only that this Handoff **follows** the referenced one
in the intended continuation lineage. It establishes no supersession,
invalidation, replacement, authority, or reciprocity — the earlier Handoff
stays exactly as valid.

Because references are not resolved, nothing guarantees the referenced Handoff
exists, belongs to the same Task, is literally the immediately previous one, or
that the chain is acyclic. **No supersession mechanism exists in v1.0.**

## Mutates nothing

Creating or reading a Handoff does not change a Task's status or assignment,
alter approved scope, close a Question, change an Investigation, alter a
Decision or Evidence, authorize execution, or approve work. Instructions in the
body **cannot expand a Task's approved scope**.

## Current state

- `handoff.md` is illustrative and fictional.
- No ID allocation, no validation, no orchestration, no routing, no receipt, no
  supersession, no history, no identity model.
- Nothing checks that a filename matches its `id`.
