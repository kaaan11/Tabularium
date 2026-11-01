# Investigation Template

**STATUS: EXAMPLE ONLY — NOT AUTOMATION**

## Purpose

An **Investigation** is a structured reasoning and inquiry process: how a
problem is being systematically examined using evidence. It is the third link
in the epistemic workflow:

```
SOURCE → EVIDENCE → INVESTIGATION → CONCLUSION → DECISION
```

| Object | Answers |
| --- | --- |
| Task | What work are we doing? |
| Evidence | What was observed, obtained, or asserted? |
| Investigation | How are we systematically examining the problem using evidence? |

Decision has its own contract (`../../schemas/decision.schema.yaml`).
Conclusion is not formalized and has no schema.

| Document | Holds |
| --- | --- |
| `tabularium/schemas/investigation.schema.yaml` (in the Tabularium package) | The contract (v1.4) — fields and allowed values |
| `investigation.md` | Canonical record example (illustrative, fictional) |

## What an Investigation is not

- Not a chat transcript
- **Not a model's chain of thought** — nothing here stores private reasoning
  traces, and no such mechanism may be added
- Not an unrestricted reasoning dump
- Not a collection of unsupported guesses

What is recorded is the externally useful artifact: the problem, the known
evidence, the hypotheses, the actions, the resulting evidence, the evaluation,
and the current conclusion status.

## The anti-assumption rule

```
HYPOTHESIS → TEST/ACTION → EVIDENCE → EVALUATION
```

An unsupported assumption must never silently become a conclusion.

| | |
| --- | --- |
| **Bad** | "Hypothesis X seems correct, so we changed the code." |
| **Good** | "Hypothesis X was tested by reproducing the failure. `EVD-2026-0017` records the result. The hypothesis was then evaluated." |

This exists specifically to prevent assumption-driven debugging.

## Storage and record format

```
.ai/investigations/INV-YYYY-NNNN.md
```

**The filename MUST equal the Investigation id.** Records are Markdown with
YAML frontmatter: the frontmatter is machine-authoritative, and the body
carries the reasoning.

Unlike Task and Evidence, the body is where the substance lives — hypotheses,
actions, evaluations, and the conclusion are body sections, not contract
fields.

## Keep records cheap

Seven fields are required: `schema_version`, `id`, `title`, `status`,
`objective`, `created_at`, `created_by`.

```markdown
---
schema_version: "1.4"
id: INV-2026-0002
title: Intermittent build failures on the shared runner
objective: Determine why the build fails on roughly one run in five.
status: OPEN
created_at: "2026-08-17T08:00:00Z"
created_by: human:user
---

# Investigation
```

Hypotheses, evidence, and the conclusion narrative are deliberately **not**
mandatory frontmatter — an investigation that has just opened has none of them
yet.

One field is **conditionally** required: `conclusion_status`.

| Lifecycle status | `conclusion_status` |
| --- | --- |
| `OPEN` | May be absent |
| `PAUSED` | May be absent |
| `CONCLUDED` | **MUST be present** |
| `ABANDONED` | **MUST be `NO_VALID_CONCLUSION`** |

## Hypotheses and conclusions

Hypotheses are body sections identified locally as `H1`, `H2`, `H3`. There is
**no Hypothesis schema**: they are not records and have no global ids.

A specific hypothesis is cited from outside by composing the Investigation id
with its local identifier:

```
INV-2026-0001#H1
```

This lets a future Decision or Report point at one hypothesis without a global
Hypothesis entity existing. The M2.4 loader resolves such a reference
read-only against the exact `### Hn` heading; **write-time enforcement is NOT
IMPLEMENTED**.

### H-numbers are append-only

**Once assigned, an H-number is never reused.** New hypotheses take the next
unused number; deleted, retired, or rejected ones keep theirs.

```
H1
H2  — RETIRED
H3

next hypothesis → H4        (never H2)
```

Reusing H2 would leave any external citation of `INV-2026-0001#H2` silently
pointing at different content — it would still resolve, and it would be wrong.

A retired hypothesis keeps its number, stays identifiable, and **must not
silently disappear** if it has been referenced from outside. `RETIRED` is a
local narrative marker in the body, not a hypothesis status value.

| Hypothesis status | Meaning |
| --- | --- |
| `UNTESTED` | No action has tested it |
| `SUPPORTED` | Evidence is consistent with it |
| `WEAKENED` | Evidence counts against it, but it is not ruled out |
| `REJECTED` | Evidence rules it out |
| `INCONCLUSIVE` | Tested, and the result settled nothing |

**Status is not truth.** `SUPPORTED` does not mean proven.

A conclusion has two parts: `conclusion_status` in the frontmatter is the
machine-authoritative classification (`SUPPORTED`, `INCONCLUSIVE`,
`NO_VALID_CONCLUSION`), and `## Current conclusion` in the body carries the
narrative. The body explains the classification but must not restate its exact
value as a second authoritative field.

Those three describe the **answer**, not the investigation lifecycle — they are
not status values.

**`CONCLUDED` + `INCONCLUSIVE` is not `ABANDONED` + `NO_VALID_CONCLUSION`.**
The first means the investigation was completed but the evidence did not
support a strong enough conclusion. The second means it was intentionally
terminated before reaching a valid conclusion. A finished inquiry with a weak
answer is not the same as an unfinished one, and the two are never counted or
reported as the same outcome.

## Evidence stays independent

An Investigation references Evidence by id. It never copies evidence content in
and never rewrites it. It may evaluate evidence but does **not** change a
record's `source_type` — a `MODEL_ASSERTION` is not upgraded into verified fact
by being cited here.

## Phase-dependent immutability

While an Investigation is `OPEN` or `PAUSED`, it may evolve under the contract:
hypotheses, actions, evaluations, evidence references, relationships, and the
current conclusion may develop.

Once it becomes `CONCLUDED` or `ABANDONED`, the entire stored artifact freezes:
all YAML/frontmatter and all Markdown body content are immutable, including
`updated_at`, relationships, hypothesis text and status, and conclusion
narrative. There is no post-terminal relationship-maintenance exception.

New Evidence, a correction, a new interpretation, or further inquiry requires
a new Investigation. The terminal record is not reopened or edited; the new
record may reference it using the existing relationship semantics.

## Current state

- `investigation.md` is illustrative and fictional, not a form and not a
  generator input.
- There is **no investigation engine**, no CLI, no ID allocation, no lifecycle
  enforcement, and no validation.
- Nothing checks that a filename matches its `id`, or that an evaluated
  hypothesis cites evidence.
