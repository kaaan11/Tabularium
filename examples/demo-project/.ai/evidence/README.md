# Evidence Template

**STATUS: EXAMPLE ONLY — NOT AUTOMATION**

## Purpose

**Evidence** records what was observed, sourced, tested, or asserted. It is the
second link in the reasoning chain:

```
SOURCE → EVIDENCE → INVESTIGATION → CONCLUSION → DECISION
```

Evidence stops at the second position. It does not analyse, conclude, or
decide. Investigation has its own contract
(`../../schemas/investigation.schema.yaml`), as does Decision
(`../../schemas/decision.schema.yaml`). Conclusion has no schema.

| Document | Holds |
| --- | --- |
| `tabularium/schemas/evidence.schema.yaml` (in the Tabularium package) | The contract (v1.4) — fields and allowed values |
| `evidence.md` | Canonical record example |

## Evidence is not truth

A record establishes that something was observed, obtained, or asserted — not
that the thing asserted is correct. An AI-generated statement must not silently
become verified evidence.

Recording a model's claim that "authentication probably fails because of
middleware X" is evidence that **the model made this assertion**. It is not
evidence that middleware X is the cause. Establishing that requires testing it
and recording the result as its own record.

There is deliberately **no conclusion field**. Interpretation belongs to
Investigation, not to the record of what was observed.

## Storage and record format

Evidence records belong in **`.ai/evidence/`** within a project's AI context
(`../project/README.md`), at the canonical path:

```
.ai/evidence/EVD-YYYY-NNNN.md
```

**The filename MUST equal the Evidence id** — `.ai/evidence/EVD-2026-0001.md`.
A record whose filename and `id` disagree is invalid.

The canonical stored form is a **Markdown document with YAML frontmatter**:

- **Frontmatter** — machine-authoritative metadata. Every contract field lives
  here.
- **Body** — human- and model-readable narrative. Defines no fields, and holds
  no second authoritative copy of any of them.

## Keep records cheap

Eight fields are required: `schema_version`, `id`, `title`, `source_type`,
`status`, `observation`, `collected_at`, `collected_by`.

```markdown
---
schema_version: "1.4"
id: EVD-2026-0002
title: User reports early sign-out
source_type: USER_ASSERTION
status: ACTIVE
observation: The user reports being signed out roughly a minute after login.
collected_at: "2026-08-16T09:20:00Z"
collected_by: human:user
---

# Evidence
```

`observation` is required because a record carrying only source metadata is not
Evidence — it records that a source was consulted, not what came of it.

`title` is a short human-scannable label so a directory of `EVD-YYYY-NNNN.md`
files can be read without opening each one. It is not a conclusion, does not
replace `observation`, and should not duplicate it.

Everything else is optional. An omitted optional field means NOT RECORDED. Do
not fill a field with invented information to make a record look complete.

## Two things kept apart

- **`source_type`** says what kind of evidence this is. It is **not** a
  reliability score.
- **`reliability`** is the current assessment of this record itself, and is
  **never** derived from `source_type`. A `PRIMARY_SOURCE` is not automatically
  `HIGH`; a `MODEL_ASSERTION` is not automatically `LOW`.

## Immutability

A record is immutable with respect to its observation and source content. If it
later turns out to be wrong or outdated:

1. mark the original `SUPERSEDED` or `RETRACTED`,
2. create a new record,
3. reference the old one.

Records are never deleted merely because they became invalid. Evidence history
is preserved.

If EVD-A is superseded by EVD-B, EVD-A may carry `superseded_by: [EVD-B]` and
EVD-B may carry `supersedes: [EVD-A]` — two directional views of the same
relationship. If only one side exists the record is still structurally valid;
consistency between the two is a future mechanism. Nothing updates the other
side automatically.

## Current state

- `evidence.md` is an illustrative example, not a form and not a generator
  input.
- There is **no evidence database**, no index, no search, no ID allocation, no
  lifecycle enforcement, no validation, and no CLI.
- Nothing checks that a filename matches its `id`, and immutability is a
  contract rule rather than a technical guarantee.
