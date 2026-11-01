# Project Bootstrap Templates

**STATUS: M2.2 TEMPLATES DEFINED — AUTOMATION NOT IMPLEMENTED**

## Purpose

Documents and supplies the smallest approved Project Context scaffold for new
project bootstrap and existing-repository onboarding. These files are
non-executable templates; no CLI, generator, id allocator, or lifecycle
automation exists.

## Positional project structure

The containing `<project-root>/.ai/` tree establishes project membership.
Neither the manifest nor any record creates a global project id, UUID,
registry, resolver, or cross-project address.

```text
.ai/
├── manifest.yaml
├── project.md
├── constraints.md
├── current-state.md
├── architecture.md        # only when cited information exists
├── tasks/
├── evidence/
├── investigations/
├── decisions/
├── questions/
├── handoffs/
└── reports/
```

## Initial file matrix

| Path | Rule | Template or guidance |
| --- | --- | --- |
| `manifest.yaml` | Create after authorization | `manifest.yaml` — manifest v1.0; required fields only, with optional commented title |
| `project.md` | Create only after title, purpose, and broad scope are human-confirmed | `project.md` — all markers must be replaced; never persist guesses or `TBD` |
| `constraints.md` | Create after authorization | `constraints.md` — distinguishes “none recorded” from “none exist” |
| `current-state.md` | Create as a source-linked zero-state derived index | `current-state.md` — replace markers with native sources before persistence |
| `architecture.md` | Create only when cited information exists | No empty template is provided. Separate approved intended architecture from observed implementation and cite each basis |

## Record directories

Create all seven directories as empty/scaffold directories. Directory
existence never creates a record:

| Directory | Contract | Canonical filename |
| --- | --- | --- |
| `tasks/` | `tabularium/schemas/task.schema.yaml` (in the Tabularium package) (v1.10) | `TASK-YYYY-NNNN.md` |
| `evidence/` | `tabularium/schemas/evidence.schema.yaml` (in the Tabularium package) (v1.4) | `EVD-YYYY-NNNN.md` |
| `investigations/` | `tabularium/schemas/investigation.schema.yaml` (in the Tabularium package) (v1.4) | `INV-YYYY-NNNN.md` |
| `decisions/` | `tabularium/schemas/decision.schema.yaml` (in the Tabularium package) (v1.2) | `DEC-YYYY-NNNN.md` |
| `questions/` | `tabularium/schemas/question.schema.yaml` (in the Tabularium package) (v1.0) | `QST-YYYY-NNNN.md` |
| `handoffs/` | `tabularium/schemas/handoff.schema.yaml` (in the Tabularium package) (v1.0) | `HND-YYYY-NNNN.md` |
| `reports/` | `tabularium/schemas/report.schema.yaml` (in the Tabularium package) (v1.0) | `RPT-YYYY-NNNN.md` |

Preliminary read-only discovery requires no Task. Once durable bootstrap or
onboarding is authorized, create exactly one Task for that work under Task
v1.10. Do not create additional records merely because the directories exist.

## Context boundaries

There are exactly two durable context layers: Global Context and positional
Project Context. A Task-centric context view and Context Package are ephemeral
derivatives with no independent authority. There is no durable Task Context,
generic Context Entry, CTX schema, or per-Task memory file.

The project documents retain the M2.1 roles:

| File | Role |
| --- | --- |
| `manifest.yaml` | Machine configuration / compatibility; never project scope |
| `project.md` | Project charter |
| `constraints.md` | Current project constraint register |
| `architecture.md` | Derived architecture synthesis |
| `current-state.md` | Derived source-linked index that establishes no facts |

## Unknown and security rules

Unknown charter title, purpose, broad scope, or ruleset stops durable
bootstrap. Unknown constraints are recorded only as “No project-specific
constraints are currently recorded.” Unknown architecture causes
`architecture.md` to be omitted.

Never persist secrets, API keys, credentials, private keys, tokens, or
sensitive runtime values. If encountered, report only the safe location,
category, and risk; never copy the value.

## Not implemented

No bootstrap or onboarding automation, executable context loader, persisted or
serialized Context Package, validation executable, or migration machinery is
implemented.
