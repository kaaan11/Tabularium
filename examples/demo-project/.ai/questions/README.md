# Question Template

**STATUS: EXAMPLE ONLY — NOT AUTOMATION**

## Purpose

A **Question** records an explicit unresolved information request that matters
to project work and must be recorded rather than silently answered by
assumption.

It is information-governance support. **A Question's answer is information.**

| Document | Holds |
| --- | --- |
| `tabularium/schemas/question.schema.yaml` (in the Tabularium package) | The contract (v1.0) — fields and allowed values |
| `question.md` | Canonical record example (illustrative, fictional) |

## What a Question is not

Not a Decision, not an approval, not Evidence, not an execution instruction,
not a chat transcript, not chain-of-thought, not a brainstorm log, not a
discussion thread, not a generic note, not a TODO list, and not a voting
mechanism.

## Answering establishes no authority

`ANSWERED` means exactly one thing: **an explicit answer has been recorded.**

It does **not** mean human approval occurred, that the answer is a Decision,
that the answer is authoritative truth, that execution is authorized, that any
Evidence changed reliability, or that the answer became Evidence.

**Answering mutates nothing else.** It does not change a Task's status, unblock
work, alter an Investigation, create a Decision, or create Evidence. If a
blocked Task should resume, that is a separate explicit act on the Task.

## Storage and record format

```
.ai/questions/QST-YYYY-NNNN.md
```

**The filename MUST equal the Question id.** Records are Markdown with YAML
frontmatter: the frontmatter is machine-authoritative, the body carries
context. Project membership is **positional** — there is no `project` field.

## Keep records cheap

Eight fields are required: `schema_version`, `id`, `title`, `question`,
`status`, `impact`, `created_at`, `created_by`.

```markdown
---
schema_version: "1.0"
id: QST-2026-0002
title: Owner of the shared build runner
question: Who owns the shared build runner, so that access can be requested?
status: OPEN
impact: NON_BLOCKING
created_at: "2026-08-18T14:00:00Z"
created_by: model-A
---

# Question
```

`title` and `question` are both required and are not the same thing: the title
is a scannable handle, the question is what was actually asked.

## Status

| Status | Meaning |
| --- | --- |
| `OPEN` | Raised; no answer recorded |
| `ANSWERED` | An explicit answer is recorded. **Terminal.** |
| `WITHDRAWN` | Closed **without** an answer — no longer needed, or overtaken by events. **Terminal.** |

Only `OPEN → ANSWERED` and `OPEN → WITHDRAWN` are valid. **There is no reopen
in v1.0** — raise a new Question and link it through `related_questions`.

A withdrawn Question is never represented, counted, or reported as answered.

## Impact

| Impact | Meaning |
| --- | --- |
| `BLOCKING` | The related work cannot correctly proceed without an answer |
| `NON_BLOCKING` | The work may proceed, but the answer would still materially affect it |

`impact` is **not** Task priority, risk, severity, or urgency. It says whether
work can proceed, not how much the work matters. `NON_BLOCKING` does not mean
unimportant — a Question is raised at all only because the answer would
materially change the work.

## Answer provenance is conditionally required

`answer`, `answered_by`, and `answered_at` are **one unit** of three:

| `status` | The three answer fields |
| --- | --- |
| `OPEN` | MUST be absent |
| `ANSWERED` | **MUST be present — all three** |
| `WITHDRAWN` | MUST be absent |

Any partial combination is invalid. `answered_by` is an **opaque actor
reference** — recording who answered is not establishing that they were
authorized, and no identity system exists.

Withdrawal provenance is deliberately **not required** in v1.0: there is no
`withdrawn_by`, `withdrawn_at`, or `withdrawal_reason`. The status alone
distinguishes a withdrawn Question from an answered one.

## Immutability is phase-dependent

| Phase | May change |
| --- | --- |
| From creation | `id`, `question`, `created_at`, `created_by` are **always frozen** |
| While `OPEN` | `title`, `impact`, the reference fields, `related_questions`, `updated_at` |
| Once terminal | **only** `related_questions` and `updated_at` |

Once answered or withdrawn, the historical meaning is frozen — what was asked,
how it was labelled, whether it blocked work, what it bore on, and (if
answered) the answer, who gave it, and when.

`related_questions` stays mutable because discovering a relationship later does
not rewrite what was originally asked or answered.

## Relationships

`task`, `investigation`, `decision`, `evidence`, `related_questions` — all
optional, all canonical ids, all project-local, none implying reciprocity or
authority.

The `decision` reference is one-way: Decision has no `questions` field, and
none was added.

## Current state

- `question.md` is illustrative and fictional.
- There is **no ID allocation, no validation, no lifecycle enforcement, no
  reopen, no supersession, no answer or state history, and no automation**.
- Nothing checks that a filename matches its `id`.
