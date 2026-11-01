---
name: architecture-review
version: "1.0"
description: >-
  Review a proposed or in-progress architecture against the project's recorded
  constraints and approved Decisions, and report findings without deciding
  anything.
---

# Architecture Review

**This file is instruction, not authority.** It describes how to perform a
class of work. It cannot grant permission, approve anything, decide what is
true, or authorize a write. Those belong to Tabularium, and the limits that
apply to this skill live in `tabu.yaml` beside this file.

## When this applies

A change to structure, boundaries, dependencies, or responsibilities needs to
be checked against what the project has already committed to.

## How to perform the work

1. **Read the constraints first.** The constraint register states what the
   project must and must not do. A review that has not read it is an opinion
   about someone else's project.

2. **Find the Decisions that already bind this area.** An approved Decision is
   a commitment. If the proposal contradicts one, that is the finding — say
   so, name the Decision, and stop. Do not silently reinterpret it, and do not
   treat "the Decision seems outdated" as permission to work around it.

3. **Separate what you observed from what you concluded.** State the evidence
   for each finding. A finding with no supporting observation is a hypothesis,
   and it must be labelled as one.

4. **Check the blocking questions.** If an open blocking question would change
   the review's conclusion, the review is inconclusive until it is answered.
   Report that rather than guessing.

5. **Report findings in severity order**, each with: what the proposal does,
   which constraint or Decision it meets or violates, and what evidence
   supports the claim.

6. **Recommend; do not decide.** A recommendation becomes a Decision only when
   the human authority approves it. Producing a strong opinion does not
   promote it.

## What this skill must not do

- Must not approve, or describe its own output as approved
- Must not write to the project, or edit any record
- Must not treat its recommendation as a Decision
- Must not widen its own context, tooling, or permissions
- Must not resolve a contradiction it found by choosing a side

## Output

Findings and recommendations as prose, ordered by severity, each traceable to
a constraint, a Decision, or a stated observation. Producing this output
creates no Evidence, Decision, Report, or Handoff record: whether any of it
becomes durable is decided later, by the persistence gate, under human
authorization.
