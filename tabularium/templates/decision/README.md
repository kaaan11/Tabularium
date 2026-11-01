# Decision Template

**STATUS: EXAMPLE ONLY — NOT AUTOMATION**

## Purpose

A **Decision** records what course of action was selected, rejected, or left
unresolved — with who proposed it, what supported it, what was objected to, and
who had authority to approve it.

Decision is the **governance boundary between reasoning and action**. It
records the decision process and the authority behind it. **It does not execute
anything.**

| Object | Answers |
| --- | --- |
| Task | What work is being performed? |
| Investigation | How is a problem being examined? |
| Evidence | What was observed, obtained, or asserted? |
| Decision | What course of action was selected, rejected, or left unresolved? |

| Document | Holds |
| --- | --- |
| `tabularium/schemas/decision.schema.yaml` (in the Tabularium package) | The contract (v1.2) — fields and allowed values |
| `decision.md` | Canonical record example (illustrative, fictional) |

## Four things that are not each other

| | |
| --- | --- |
| **Recommendation** | A proposed course of action. Carries no authority. Producing one changes nothing. |
| **Decision** | A recorded selection among alternatives. May exist with no authority at all — an unapproved record is still a proposal. |
| **Approval** | Acceptance by the applicable authority. This is what gives a decision force. |
| **Execution** | Actually doing it. **Out of scope** — designed later. |

A record must never be readable in a way that blurs them.

## Governance

Models **may** propose, recommend, challenge, disagree, offer alternatives,
provide evidence, identify risks, and question a user's decision.

Models **do not** become authoritative by having produced a proposal. A model's
statement is not a decision; a recommendation is not an approval.

Equally, a user decision is **not automatically technically correct**. A model
may object and cite evidence against it. What it may not do is override it or
quietly act as though something else had been decided.

Every record preserves: who proposed something, why, what evidence supported
it, what alternatives existed, what objections were raised, who had authority
to approve, and what was ultimately decided.

## Authority

The `## Authority` body section answers one question: **who is authorized to
make the final decision?**

Authority is never inferred. The contributor who created the record is not the
authority. The contributor who proposed the selected alternative is not the
authority. Authority is stated, or it is unknown.

**Human authority principle:** when a decision materially affects the user's
project, architecture, security posture, data, cost, destructive operations, or
other consequential outcomes, the final authority remains the user unless an
explicitly approved delegation mechanism exists.

**Delegation is NOT IMPLEMENTED.** No delegated authority can be recorded,
claimed, or exercised.

## Storage and record format

```
.ai/decisions/DEC-YYYY-NNNN.md
```

**The filename MUST equal the Decision id.** Records are Markdown with YAML
frontmatter: the frontmatter is machine-authoritative, the body carries the
governance record.

## Keep records cheap

Seven fields are required: `schema_version`, `id`, `title`, `status`,
`question`, `created_at`, `created_by`.

```markdown
---
schema_version: "1.2"
id: DEC-2026-0002
title: Choice of log retention window
question: How long should non-production logs be retained?
status: PROPOSED
created_at: "2026-08-18T09:00:00Z"
created_by: model-A
---

# Decision
```

Approval fields are **not** unconditionally required — a newly proposed
Decision has not been approved, and demanding approval metadata on it would
make the contract unusable for the case it exists to serve.

## Approval provenance is conditionally required

`approved_by` and `approved_at` depend on the record's status. They move
together: one without the other is invalid in either direction.

| status | `approved_by` / `approved_at` |
| --- | --- |
| `PROPOSED` | MUST be absent |
| `UNDER_REVIEW` | MUST be absent — review is not approval |
| `APPROVED` | **MUST be present** |
| `REJECTED` | MUST be absent |
| `WITHDRAWN` | MUST be absent |
| `SUPERSEDED` | **MUST be present** — it *was* approved before being replaced |

An approved Decision that records no approver is indistinguishable from an
agent approving its own recommendation. Recording *who* approved is not the
same as establishing that they were *authorized*: **authority is a
constitutional constant**, not a schema
field. No authority field exists and none will be added.

## Body sections

`## Context`, `## Authority`, `## Alternatives`, `## Objections and dissent`,
`## Selected decision`, `## Rationale`, `## Risks`.

**Alternatives** are subsections identified locally as `A1`, `A2`, `A3` — each
with a statement, who proposed it, supporting evidence, advantages, and risks.
There is no Alternative schema and no global alternative ids.

**Objections and dissent** records who objected, what to, evidence cited,
alternative proposed, whether it was accepted or rejected, and why. An
overruled objection **stays in the record** — that is what lets a later reader
see a risk was known and accepted rather than missed. There is no Dissent
schema, **no voting, no quorum, no majority rule**.

**Selected decision** is narrative. There is deliberately no
`selected_decision` frontmatter field: `status` says whether the record is
approved, not which option won.

**Rationale** distinguishes evidence, assumptions, constraints, preferences,
and risk tradeoffs — a rationale that mixes them cannot be reviewed.

**Risks** records risk, severity, mitigation, and owner where applicable. There
is no Risk schema and no global risk vocabulary.

## Model-agnostic

There are **no model-specific fields** — no `claude_proposal`,
`chatgpt_proposal`, or `codex_proposal`, and none may be added. Contributors
are opaque identifiers with a stated role:

```
Contributor: model-A    Role: proposer
Contributor: model-B    Role: reviewer
```

No capability is assumed for any contributor.

## Evidence stays independent

A Decision references Evidence by id, never copies it in, and **never modifies
it**. A `MODEL_ASSERTION` remains a `MODEL_ASSERTION` however heavily it is
relied upon — approval is a governance act, not an epistemic one.

Investigations are referenced as `INV-YYYY-NNNN`, and a specific hypothesis as
`INV-YYYY-NNNN#Hn` — the canonical format defined by the Investigation
contract.

## Approval freezes the Decision

Mutability after approval is **phase-dependent** — a field that may change
while a Decision is in force is not editable forever.

| Phase | May change | Frozen |
| --- | --- | --- |
| **APPROVED**, not yet superseded | `status` (only → `SUPERSEDED`), `superseded_by` (only as part of that transition), `related_decisions`, `updated_at` | everything else, including `supersedes` and the whole body |
| **SUPERSEDED** | `related_decisions`, `updated_at` | `status` (terminal), `superseded_by`, `supersedes`, approval provenance, and the whole body |

The status change and acquiring `superseded_by` are **one act**: a Decision
that is not `SUPERSEDED` must not carry `superseded_by`, and one that is
`SUPERSEDED` must. There is no valid halfway state.

Everything outside the phase's permitted set is frozen — **and the freeze
covers the body, not just the frontmatter**.

The substance lives in the body: authority, alternatives, objections, selected
decision, rationale, risks. Freezing the metadata while leaving the body
editable would protect the label and lose the decision. An overruled objection
is never removed or softened after the fact.

This is also what binds approval to *what* was approved. Because the substance
cannot change, the approval is tied to it by construction — no scope field, no
duplicated copy of the content, no hashing.

## Immutability and reversal

An approved Decision is **not** rewritten, and **not** reversed by editing its
status. `APPROVED → REJECTED` and `APPROVED → WITHDRAWN` are invalid.

To change course: create a **new** Decision that supersedes the old one. The
old record becomes `SUPERSEDED` and is not deleted — it stays historically
meaningful. `SUPERSEDED` is a **controlled lifecycle change, not permission to
rewrite an approved Decision**: it changes one status value and a reference,
and touches nothing that was approved.

## Effective supersession: reciprocal, approval-bound, monotonic

`supersedes` **may be recorded at any status** — a proposal is allowed to state
that it intends to replace an existing Decision. That records an intention and
supersedes nothing.

**A is effectively superseded by B only when all three hold:**

1. `B.status == APPROVED`
2. `B.supersedes` contains A — the forward view
3. `A.superseded_by` contains B — the reverse view

If any one is missing, A must not be `SUPERSEDED`.

```yaml
# DEC-2026-0007  (the replacement)
status: APPROVED
supersedes: [DEC-2026-0003]

# DEC-2026-0003  (the record being replaced)
status: SUPERSEDED
superseded_by: [DEC-2026-0007]
```

`DEC-2026-0003` was not validly retired until `DEC-2026-0007` was **approved**.
While the replacement sat in `PROPOSED` or `UNDER_REVIEW` it could declare its
intent, and the predecessor stayed in force.

| Superseding Decision | Effect on the target |
| --- | --- |
| `PROPOSED` / `UNDER_REVIEW` | cannot cause supersession |
| `REJECTED` / `WITHDRAWN` / `SUPERSEDED` | cannot cause supersession |
| `APPROVED` **without** `supersedes: [target]` | cannot cause supersession |
| `APPROVED` **with** `supersedes: [target]` | **may** cause the target to become `SUPERSEDED` |

**Being approved is not enough.** If B never names A, then approving B was
never an approval of B *as the replacement for A* — and pointing A at it after
the fact would retire A on an authority the human never gave.

**`supersedes` is frozen once B is approved.** It cannot be added, removed,
redirected, expanded, or reduced. Changing the intended predecessor requires a
new Decision; an approved record is never edited to point somewhere else.

**`superseded_by` is frozen once A is superseded.** It cannot be removed,
redirected, or have its cardinality changed. A supersession that could be
quietly undone would be a silent rewrite of history.

The check is on **status and the recorded relationship**, not identity — so it
needs no identity model.

Whether one Decision may have **multiple** approved successors, and what
precedence would hold between them, is **NOT SPECIFIED**. Both fields remain
lists; no cardinality or precedence rule has been decided.

**Automatic enforcement remains NOT IMPLEMENTED.** Nothing checks the freeze,
the provenance rules, or the superseding record's status. These are contract
rules.

## Current state

- `decision.md` is illustrative and fictional.
- There is **no decision automation**, no voting, no automatic approval, no
  model routing, no ID allocation, no lifecycle enforcement, and no validation.
- `approved_by` and `approved_at` are never populated automatically.
- Nothing here executes a decision.
