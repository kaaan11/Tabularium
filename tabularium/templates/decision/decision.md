---
schema_version: "1.2"
id: DEC-2026-0001
title: Sequencing of authentication failure remediation
question: >
  Should authentication failures be investigated by changing middleware
  configuration immediately, or should reproduction tests be performed first?
status: APPROVED
approved_by: authority-1
approved_at: "2026-08-17T15:20:00Z"
task: TASK-2026-0001
investigation: INV-2026-0001
evidence:
  - EVD-2026-0001
  - EVD-2026-0002
  - EVD-2026-0004
created_at: "2026-08-17T10:00:00Z"
updated_at: "2026-08-17T15:20:00Z"
created_by: model-A
---

# Decision

**Illustrative example only.** It does not describe a real project, a real
system, or real people. The contributors, the evidence, and the outcome are
fictional, and every identifier is a placeholder. No personal data appears
here.

Stored, this file would be `.ai/decisions/DEC-2026-0001.md` — the filename
equals the `id`. It sits here under `tabularium/templates/decision/` because it is a
template, not a live record.

The frontmatter above is authoritative for the metadata, including whether this
record carries authority. The sections below are the governance record.

## Context

An investigation into authentication test failures reached a point where two
courses of action were available and they could not both be taken first. One
contributor proposed acting; another objected. This record exists because that
disagreement needed resolving by someone with the standing to resolve it.

Note what this record does **not** do: it does not change any configuration, run
any test, or instruct anyone to. Acceptance here is a governance act. Carrying
it out is separate work, and would be its own Task.

## Contributors

| Contributor | Role |
| --- | --- |
| `model-A` | proposer — drafted this record and proposed A1 |
| `model-B` | reviewer — raised the objection below and proposed A2 |
| `authority-1` | authority — held the final decision |

Identifiers are opaque. Nothing here assumes anything about what any
contributor is good at, and drafting this record gave `model-A` no standing
over its outcome.

## Authority

Final authority rests with `authority-1`, the human authority for this project.

This decision touches system configuration and the sequencing of remedial work,
which materially affects the project. Under the human authority principle, the
final decision therefore remains with the user. No delegation mechanism exists
in the Tabularium, so no contributor could have held this authority by delegation.

`model-A` created this record and proposed the initial course of action.
Neither fact conferred authority. `model-B` raised the objection that changed
the outcome; that did not confer authority either.

## Alternatives

### A1 — Change the middleware configuration immediately

- **Statement:** reorder the middleware so the session header is preserved
  through validation, and observe whether the failures stop.
- **Proposed by:** `model-A`
- **Supporting evidence:** `EVD-2026-0002` — a model assertion naming a
  probable cause. No test result supports this alternative.
- **Advantages:** fastest path to a possible fix; requires no further
  investigation before acting.
- **Risks:** the hypothesis it rests on, `INV-2026-0001#H4`, is untested. If
  the change appears to help for unrelated reasons, the real cause is masked
  and the record of why the failures stopped is wrong.

### A2 — Perform reproduction tests first

- **Statement:** reproduce the failure under controlled conditions to confirm
  the mechanism already indicated, and test the middleware hypothesis properly
  before changing anything.
- **Proposed by:** `model-B`
- **Supporting evidence:** `EVD-2026-0001` — the failing test run.
  `EVD-2026-0004` — the reproduction showing a consistent timing offset, which
  supports `INV-2026-0001#H3`.
- **Advantages:** acts on a mechanism that evidence supports rather than one
  that has never been tested; leaves a record of why the failures stopped.
- **Risks:** slower; the failures continue while the tests run.

## Objections and dissent

### O1 — `model-B` objects to A1

- **Objected to:** A1, and specifically to acting on `INV-2026-0001#H4`.
- **Objection:** `INV-2026-0001#H4` is recorded as untested. `EVD-2026-0002`,
  the evidence offered for A1, is a model assertion — it establishes that the
  claim was made, not that it is correct. Meanwhile `INV-2026-0001#H3` is
  supported by `EVD-2026-0004` and points at a different mechanism entirely.
  Changing configuration on the strength of an untested hypothesis is the
  failure mode the investigation protocol exists to prevent.
- **Evidence cited:** `EVD-2026-0002`, `EVD-2026-0004`.
- **Alternative proposed:** A2.
- **Outcome:** **accepted** by the authority.
- **Reason:** the objection identified a real distinction between an asserted
  cause and a supported one, and the record of the investigation bore it out.

### O2 — `model-A` objects to the cost of A2

- **Objected to:** A2, on the grounds that reproduction takes time during which
  users continue to be affected.
- **Evidence cited:** none. This is a judgement about cost, not a claim about
  the system, and it is recorded as such.
- **Alternative proposed:** none beyond A1.
- **Outcome:** **rejected** by the authority.
- **Reason:** the delay is real and was weighed. It was judged a smaller cost
  than acting on an untested hypothesis and losing the ability to say why the
  failures stopped.

`O2` was overruled and stays in this record. A later reader should be able to
see that the delay was known and accepted, not overlooked.

## Selected decision

**A2 — perform reproduction tests first.**

The middleware configuration is not to be changed until `INV-2026-0001#H4` has
actually been tested.

Note the sequence this record went through, because the three are not the same
thing: `model-A` **recommended** A1, which changed nothing on its own; the
selection of A2 is the **decision**; and that decision carried no force until
`authority-1` accepted it, which is the **approval** recorded in the
frontmatter. Had the authority never acted, this record would still say A2 was
selected, and it would still be a proposal.

Carrying out A2 is **execution**, which this record does not perform and does
not authorise on its own.

## Rationale

**Evidence.** `EVD-2026-0004` supports the timing-offset mechanism
(`INV-2026-0001#H3`). Nothing supports the middleware mechanism
(`INV-2026-0001#H4`) beyond `EVD-2026-0002`, which is a model assertion.

**Assumptions.** That reproduction under controlled conditions will behave as
it did in `EVD-2026-0004`. This is an assumption, not a finding — a single
reproduction is not proof of reliability.

**Constraints.** The non-production environment is the only place changes may
be trialled, which rules out learning from a production change regardless of
which alternative was preferred.

**Preferences.** A preference for a slower path that leaves a clear causal
record. This is a preference, stated as one — another authority could
reasonably weigh it differently.

**Risk tradeoffs.** Continued failures during reproduction were accepted in
exchange for not acting on an untested hypothesis.

Citing `EVD-2026-0002` throughout did not make it more reliable. It was a model
assertion before this decision and remains one after it; approval is a
governance act, not an epistemic one.

## Risks

| Risk | Severity | Mitigation | Owner |
| --- | --- | --- | --- |
| Failures continue while reproduction runs | Medium | Time-box the reproduction; revisit if it overruns | `authority-1` |
| Reproduction does not reproduce the fault | Medium | Fall back to testing `INV-2026-0001#H4` directly rather than changing configuration blind | `model-B` |
| The timing offset is a symptom rather than the cause | Low | Keep `INV-2026-0001#H4` open; a supported hypothesis is not a proven one | `model-B` |

Severities here are local to this record. No global risk vocabulary exists.

## If this changes

If the course of action must change, this record is **not rewritten**. A new
Decision is created, it references this one through `supersedes`, and this one
is retired.

Three rules bound what that retirement may touch:

- **What may change depends on the phase.** While this record is in force,
  `status` may move to superseded, `superseded_by` may be populated as part of
  that same act, and `related_decisions` and `updated_at` may change. Once it
  has been retired, `superseded_by` is frozen too. Everything else is frozen
  from approval onward, and the freeze covers this body as well as the
  frontmatter. The alternatives, the objections, the selected course, the
  rationale, and the risks are what was approved; they stay as they were. The
  overruled objection above in particular is never removed once it is on the
  record.
- **Only an approved Decision that names this one can retire it.** A proposal
  may *state* that it intends to replace this record — that is part of what it
  proposes — but it cannot cause the retirement until it has itself been
  accepted by the authority. And being accepted is not enough on its own: its
  approved content must name this record. Approving some other decision is not
  approving it *as the replacement for this one*.
- **The relationship, once recorded, is not undone.** Both records carry it —
  the replacement names this one, and this one names the replacement — and
  neither side may later be removed or pointed elsewhere. A retirement that
  could be quietly reversed would be a silent rewrite of what happened.

Nothing enforces either rule today. Both are contract rules, and both rest on
the record being kept honestly and on version history.
