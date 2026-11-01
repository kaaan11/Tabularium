# Schemas

**STATUS: SEVEN CONTRACTS DEFINED — ALL OTHER OBJECTS SCAFFOLD ONLY**

## Purpose

Installed wheels include all seven normative YAML files. Downstream consumers
can use `from tabularium.schema_resources import schema_text` and
`schema_text("task")` (or `"evidence"`, etc.) without locating a source checkout.
`SCHEMA_NAMES` lists the supported names. The returned text is the same contract
as this directory, not a generated or migrated schema.

This directory holds the data contracts of the Tabularium: the structure of the
objects the system records.

Contracts here are human-readable YAML documents, not executable schemas, and
there is no schema-library dependency. The M2.4 read-only Context Loader has a
narrow version-specific consumer validator for the frozen M1 fields it uses;
it is APPROVED. M2 was frozen in the source lineage at tag m2-context-foundation-freeze-v0.1.0. A general-purpose
contract validator or migration engine is not implemented.

## Contents

| File | Object | Version | Status |
| --- | --- | --- | --- |
| `task.schema.yaml` | Task | 1.10 | ACTIVE — first formal contract |
| `evidence.schema.yaml` | Evidence | 1.4 | ACTIVE — second formal contract |
| `investigation.schema.yaml` | Investigation | 1.4 | ACTIVE — third formal contract |
| `decision.schema.yaml` | Decision | 1.2 | ACTIVE — fourth formal contract |
| `question.schema.yaml` | Question | 1.0 | ACTIVE — fifth formal contract |
| `handoff.schema.yaml` | Handoff | 1.0 | ACTIVE — sixth formal contract |
| `report.schema.yaml` | Report | 1.0 | ACTIVE — seventh formal contract |

All seven share the same storage shape. Records are **Markdown documents with
YAML frontmatter**, stored in a project's AI context under a filename that
equals the record id:

| Object | Path | Id format |
| --- | --- | --- |
| Task | `.ai/tasks/TASK-YYYY-NNNN.md` | `TASK-YYYY-NNNN` |
| Evidence | `.ai/evidence/EVD-YYYY-NNNN.md` | `EVD-YYYY-NNNN` |
| Investigation | `.ai/investigations/INV-YYYY-NNNN.md` | `INV-YYYY-NNNN` |
| Decision | `.ai/decisions/DEC-YYYY-NNNN.md` | `DEC-YYYY-NNNN` |
| Question | `.ai/questions/QST-YYYY-NNNN.md` | `QST-YYYY-NNNN` |
| Handoff | `.ai/handoffs/HND-YYYY-NNNN.md` | `HND-YYYY-NNNN` |
| Report | `.ai/reports/RPT-YYYY-NNNN.md` | `RPT-YYYY-NNNN` |

The frontmatter is machine-authoritative and the contract validates against it;
the body carries no second authoritative copy of any field. The contracts
themselves stay YAML — the Markdown format is how records are stored, not a
replacement for the contract.

## Reference policy

**This is the authoritative statement of cross-contract reference semantics.**
Individual contracts point here and record only their own exceptions.

1. A reference to a **formalized record type** uses that target contract's
   canonical id — `TASK-YYYY-NNNN`, `EVD-YYYY-NNNN`, `INV-YYYY-NNNN`,
   `DEC-YYYY-NNNN`, `QST-YYYY-NNNN`, `HND-YYYY-NNNN`, `RPT-YYYY-NNNN`.
2. Where a target contract explicitly defines a **canonical fragment syntax**,
   references to a sub-record may use it. The one existing example is
   `INV-YYYY-NNNN#Hn` for a hypothesis, defined by the Investigation contract.
3. References are **opaque identifiers**.
4. Core contracts **do not resolve references**. Nothing looks a reference up,
   dereferences it, or follows it.
5. A reference whose target does not currently resolve **does not, by that fact
   alone, make the containing record structurally invalid**.
6. References are **project-local by default** — see Project scoping below.
7. **Cross-project references are NOT IMPLEMENTED.** No syntax exists for one,
   and no record id carries a project prefix.
8. A reference implies **no reciprocity, no authority, no approval, no
   ownership, and no lifecycle transition**.
9. A contract may define stronger relationship semantics for its own fields,
   and where it does, they override the generic default in rule 8. The one
   existing example is Decision's effective supersession, which requires
   reciprocity, approval, and a lifecycle transition.

**Unformalized symbolic targets.** `context_items` and `evaluations` remain
unconstrained strings, and **no id format has been invented for them**.
`context_items` does not imply a durable generic Context Entry, CTX record,
fact store, or future contract; M2.1 explicitly rejects that abstraction.
Whether Evaluation is ever formalized remains undecided.

### Supersession semantics are contract-specific

**There is no universal reciprocity invariant for supersession.** Supersession
semantics belong to the contract that owns the relationship, and rule 8 above
continues to govern by default: a reference implies no reciprocity unless the
owning contract explicitly defines it.

The two contracts that currently define supersession define it differently, and
that difference is deliberate:

| | Decision | Evidence |
| --- | --- | --- |
| Reciprocal | **Required** — both directions must be present | Not required — a one-sided reference is structurally valid |
| Approval-bound | **Required** — only an APPROVED superseding record takes effect | No approval semantics |
| Monotonic | **Required** — once recorded, not removed or redirected | No monotonicity rule |

**Why they differ.** Superseding an approved Decision retires a
human-governed choice, so the act is governance-sensitive and is fenced
accordingly. Evidence supersession expresses **evidentiary lineage** — that a
later record has overtaken an earlier one — and does not by itself exercise
human governance authority.

This is a statement about **relationship semantics, not epistemic quality**.
Nothing here makes Evidence less reliable or less important than Decision.

**Identical field names do not imply identical semantics.** `supersedes` and
`superseded_by` appear in both contracts and mean different things about what
must hold.

**Future contracts inherit neither.** Question, Handoff, and Report do **not**
automatically acquire Decision's or Evidence's supersession rules. Question
v1.0, Handoff v1.0, and Report v1.0 each define none, as that rule requires —
Handoff's `previous_handoff` and Report's `previous_report` are lineage only.
A contract that supports supersession must define its own semantics explicitly.

No generic supersession policy, schema, or engine exists, and none is implied
by this section.

Reference **resolution is read-only and deterministic**: the M2.4 loader
resolves project-local canonical ids within the positional project and reports
what it cannot resolve. **Enforcement machinery remains NOT IMPLEMENTED** —
no registry, index, database, or write-time referential integrity.

## Project scoping

**Project membership is POSITIONAL.** A record belongs to the Tabularium project
whose `.ai/` directory contains it:

```
<project-root>/.ai/tasks/TASK-2026-0001.md
```

belongs to that project because of where it sits, not because of anything
written inside it.

- A record-level `project` field is **not** the source of truth for project
  membership. Task and Evidence carry an optional `project` field; it is
  **non-authoritative locator/annotation metadata** and **must not override**
  positional membership. It was not removed, and it was not added to
  Investigation or Decision for symmetry.
- Because references are project-local, a canonical id such as `DEC-2026-0001`
  is interpreted within the containing project's scope.
- **NOT IMPLEMENTED:** project registry, project database, global project ids,
  project resolver, cross-project addressing syntax. The future cross-project
  mechanism is undefined.

## Intentionally not implemented

The following objects are referenced by the existing contracts but have **no
schema and are NOT IMPLEMENTED**:

Project, Conclusion, Evaluation, Context item, Hypothesis,
Action, Alternative, Dissent, Vote, Risk, Identity.

Conclusion will be formalized later. Hypothesis and Action are deliberately
*not* records at all — they are narrative sections inside an Investigation, as
Alternative, Dissent, and Risk are inside a Decision. Vote and Identity are
absent by design: there is no voting mechanism and no identity system.

They are referenced *symbolically only* — a record holds an opaque string, and
no contract exists for the referent to resolve to. There is no referential
integrity, and none is defined.
These schemas are absent because they have not been architecturally specified,
or because the concept is deliberately not a record. None should be invented.

There is no manifest schema, Context Package schema, TaskContext schema,
Context Entry schema, CTX schema, project identity schema, or derived
annotation field. M2.2 defines Project Manifest configuration version 1.0 as a
non-record human-readable specification; it deliberately does not create
`tabularium/schemas/manifest.schema.yaml`. A Task-centric context view and Context Package
remain ephemeral derivatives. M2.3 defines their selective read-only semantics
without defining a durable schema or
changing any M1 valid/invalid record set. The seven contracts listed above
remain the complete record-schema set.

Three amendments so far have been documentation-only, each correcting stale
cross-references after a new contract was defined: Task 1.2, then Task 1.3 and
Evidence 1.2. None changed a field, enum, state, or rule — each is behaviorally
identical to the version before it, and each records that explicitly in its
`amendments` section.

## Versioning

- Task is at **version 1.10**, Evidence at **version 1.4**, Investigation at
  **version 1.4**, Decision at **version 1.2**, Question at **version 1.0**,
  Handoff at **version 1.0**, and Report at **version 1.0**, declared as
  `schema_version` both in the contract file and on every record. Each
  contract's `amendments` section records what its versions changed. Contract
  versions are independent of one another.
- Cross-contract references in Task, Evidence, and Investigation are current:
  all three name Decision as a contract that exists. Those corrections were
  documentation-only and, under the versioning rule below, carried no version
  bump.
- Schema versions are independent of the Tabularium release version (see
  `../VERSION`). Defining a contract does not advance the release version.
- Future schemas must preserve cross-reference compatibility: a later contract
  must not break the way an existing contract refers to it. The contracts
  refer to other objects by opaque string, specifically so that those contracts
  can be defined later without changing them. Evidence, Investigation, and
  Decision were each added without changing any existing field, which is the
  property working as intended — the version bumps that followed corrected
  prose only.
- **Schema migration is NOT IMPLEMENTED.** There is no migration tooling, no
  migration format, and no defined procedure. Existing schema versions may
  require future migration procedures. Seventeen amendments so far would have
  required migration if records existed — Task 1.1 narrowing `type`; Evidence
  1.1 making `observation` required and adding a required `title`;
  Investigation 1.1 making `conclusion_status` mandatory for `CONCLUDED` and
  `ABANDONED` records; Decision 1.1 making approval provenance mandatory for
  `APPROVED` and `SUPERSEDED` records; Decision 1.2 making `superseded_by`
  conditionally required and requiring supersession to be reciprocal; and Task
  1.4 making approval provenance conditionally required by `approval_status`;
  Task 1.5 / Evidence 1.3 / Investigation 1.2 normalizing reference fields to
  canonical target ids; Task 1.6 establishing current-effective approval
  provenance semantics; and Task 1.7 / Evidence 1.4 / Investigation 1.3
  normalizing `questions` to QST ids; Task 1.8 normalizing `handoffs` to HND
  ids; and Task 1.9 normalizing both `reports`
  and `report` to RPT ids; and Investigation 1.4 making CONCLUDED and ABANDONED
  records fully immutable; and Task 1.10 requiring `archived_outcome` for
  ARCHIVED Tasks. No Investigation or Task records exist, so the latter two
  affected no stored records; a later change could affect records that do.
### What a version means

**A contract version represents SEMANTIC COMPATIBILITY, not document revision
count.** It is not a file revision counter.

**A bump IS required** when a change alters contract semantics — the set of
valid and invalid records, field meaning, requiredness, conditional
requirements, enum membership, lifecycle transitions, mutability rules,
governance invariants, or storage/serialization semantics.

**A bump is NOT required** for documentation-only or editorial change that
leaves all of the above identical: typo fixes, stale cross-reference
corrections, clearer wording with identical normative meaning,
documentation/index/status alignment, comments that do not alter semantics, and
examples aligned to already-existing behavior.

Being written as prose does **not** make a change documentation-only. Normative
prose that changes what a record may or may not do is semantic and requires a
bump.

Earlier documentation-only bumps (Task 1.2, Task 1.3, Evidence 1.2) remain
valid historical versions. They are not renumbered or reverted — this rule
governs future changes, not the past.

This rule defines only *when* a version changes. How a version number is
chosen beyond that — and migration between versions — remains
**NOT SPECIFIED**.

## Adding a schema

A new contract requires a recorded decision by the human authority.
"It is standard practice" is not a
justification for defining a contract that has not been specified.
