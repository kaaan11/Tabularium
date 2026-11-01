---
schema_version: "1.10"
id: TASK-2026-0001
type: DEBUGGING
title: Investigate authentication failure
objective: >
  Determine why users are intermittently signed out shortly after a successful
  login, and establish the cause with evidence. Producing a fix is not part of
  this objective.
project: example-service
priority: HIGH
risk: MEDIUM
uncertainty_level: HIGH
status: IN_PROGRESS
plan: >
  Reproduce the sign-out against a non-production environment, collect session
  and token-expiry evidence from application logs, and compare behavior before
  and after the most recent deployment.
approval_required: true
approval_status: APPROVED
approved_by: human:user
approved_at: "2026-08-16T11:30:00Z"
approved_scope:
  approved_operations:
    - read application source
    - read non-production logs
  approved_risk: MEDIUM
  approved_plan: >
    Read-only investigation in the non-production environment. No code changes,
    no configuration changes, and no production access.
actual_changes: []
questions:
  - QST-2026-0001
created_at: "2026-08-16T09:15:00Z"
updated_at: "2026-08-18T09:35:00Z"
created_by: USER
---

# Task

Example of the canonical stored Task record.

Stored, this file would be `.ai/tasks/TASK-2026-0001.md` — the filename equals
the `id`. It sits here under `tabularium/templates/task/` because it is a template, not a
live record.

**The frontmatter above is authoritative.** It holds the machine-readable
metadata and is what a future validator checks against
`tabularium/schemas/task.schema.yaml`. The sections below are narrative for humans and
models: they explain and add context, and they never restate an id, an
enumerated value, a timestamp, or a reference. Nothing below competes with the
frontmatter.

Fields that are not known are omitted from the frontmatter rather than filled
with placeholder text.

## Background

Reports began arriving after the most recent deployment. Users complete a login
successfully, use the application for a short period, and are then returned to
the login screen without any action on their part. It does not reproduce
reliably, which is why the cause is not yet understood.

## Why this is investigation only

Several plausible explanations exist — session lifetime, token refresh, and a
change in how the deployment handles clock skew among them. Choosing between
them without evidence would be guesswork, and a fix built on a guess would have
to be undone.

The work is therefore bounded to establishing the cause. If a fix is wanted
once the cause is known, it becomes a separate Task.

## Notes on scope

The approval is deliberately narrow: the environment being examined is
non-production, and the investigation reads rather than changes. Approving the
objective did not approve any particular way of pursuing it.

If the cause turns out to sit somewhere the approval does not reach, the right
move is to go back for approval rather than widen the work quietly.

## Working notes

Reproduction attempts are in progress against the non-production environment.
Log collection has not yet produced a clear signal, and the comparison across
the deployment boundary has not been made.

## Result

Not recorded — the work is not finished. On completion, `result` holds a short
statement of the outcome and `report` references the Report that accounts for
what happened. The Report structure is not duplicated here.
