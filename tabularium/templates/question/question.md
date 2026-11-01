---
schema_version: "1.0"
id: QST-2026-0001
title: Retention window for non-production logs
question: >
  How long should non-production logs be retained before deletion, given that
  the investigation needs historical logs but no retention policy has been
  recorded?
status: ANSWERED
impact: BLOCKING
answer: >
  Non-production logs are retained for 30 days. Longer retention requires a
  separate decision about storage cost.
answered_by: human:user
answered_at: "2026-08-18T11:00:00Z"
task: TASK-2026-0001
investigation: INV-2026-0001
evidence:
  - EVD-2026-0004
created_at: "2026-08-18T09:30:00Z"
updated_at: "2026-08-18T11:00:00Z"
created_by: model-A
---

# Question

**Illustrative example only.** It does not describe a real project, a real
system, or real people. Every identifier is a placeholder.

Stored, this file would be `.ai/questions/QST-2026-0001.md` — the filename
equals the `id`. It sits here under `tabularium/templates/question/` because it is a
template, not a live record.

The frontmatter above is authoritative. The sections below are narrative: they
add context and never restate an id, a status, an enumerated value, a
timestamp, or the answer itself.

## Context

An investigation into intermittent failures needed logs older than what was
available on disk. Nobody could say whether the missing logs had been deleted
by policy or by accident, and no retention policy was recorded anywhere in the
project.

The investigation could not correctly proceed without knowing this — reasoning
about a gap in the logs means something different depending on whether the gap
was expected.

Rather than assume a retention window and build on it, the question was raised
and recorded.

## Answer

The retention window was supplied by the human authority and recorded in the
frontmatter.

Note what recording it did **not** do:

- It did not approve anything. No approval occurred, and nothing here
  authorises a change to how logs are handled.
- It did not become a Decision. If retention should now be set deliberately —
  weighing cost against investigative value — that is a Decision record with
  its own alternatives, objections, and authority.
- It did not become Evidence. This record says the answer was given; it does
  not establish that 30 days is what the system actually does. Confirming that
  would be an Evidence record with its own source type.
- It did not unblock the Task or change the Investigation. Those are separate,
  explicit acts on those records.

Answering closed this Question and nothing else.

## If this turns out to be wrong

This record is not reopened and not rewritten. What was asked, and what was
answered, stays as it is.

If the answer later proves incorrect, or the matter becomes unresolved again, a
**new** Question is raised and linked back through `related_questions` — the
one field that stays mutable here, because discovering a relationship later
does not change what was originally asked or answered.
