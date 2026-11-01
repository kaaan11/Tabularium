---
schema_version: "1.4"
id: INV-2026-0001
title: Authentication test failures after deployment
objective: >
  Determine why three authentication tests began failing after the most recent
  deployment, and establish the cause with evidence.
status: CONCLUDED
conclusion_status: SUPPORTED
task: TASK-2026-0001
evidence:
  - EVD-2026-0001
  - EVD-2026-0002
  - EVD-2026-0003
  - EVD-2026-0004
supporting_evidence:
  - EVD-2026-0004
contradicting_evidence:
  - EVD-2026-0003
created_at: "2026-08-16T14:10:00Z"
updated_at: "2026-08-17T09:25:00Z"
created_by: human:user
---

# Investigation

**Illustrative example only.** It does not describe a real project, a real
deployment, or a real system. The service, the evidence, and the findings are
fictional, and the Evidence ids are placeholders.

Stored, this file would be `.ai/investigations/INV-2026-0001.md` — the filename
equals the `id`. It sits here under `tabularium/templates/investigation/` because it is a
template, not a live record.

The frontmatter above is authoritative for the metadata, including how the
answer is classified. The sections below carry the reasoning: the problem, the
evidence in hand, the hypotheses, the actions taken, and where the inquiry
landed.

## Problem

Three tests covering session continuation after login began failing. Tests that
only perform a login, and tests that make an immediate follow-up request, still
pass. The failures started with a specific deployment; nothing in that
deployment obviously touches authentication.

## Known evidence

At the point the inquiry opened, two records existed:

- `EVD-2026-0001` — the test run showing three failures out of 24.
- `EVD-2026-0002` — a model's assertion naming a probable cause.

`EVD-2026-0002` is an assertion, and is treated as one. It establishes that the
claim was made, not that the claim is right. It was used to generate a
hypothesis to test — which is the only thing an untested assertion is good for.

## Hypotheses

Referenced from outside this record as `INV-2026-0001#H1` through `#H4`.

H-numbers here are append-only. `H2` was retired rather than renumbered or
deleted, and the number 2 stays spent — the hypothesis added afterwards is
`H4`, not `H2`.

### H1 — Session token lifetime was shortened by the deployment

**Status: REJECTED**

- **Statement:** the deployment reduced the configured session token lifetime,
  so sessions expire between the login and the follow-up request.
- **Tests:** compared the effective session configuration before and after the
  deployment.
- **Contradicting evidence:** `EVD-2026-0003` — the configuration comparison,
  showing the lifetime value identical on both sides.
- **Evaluation:** the value did not change, so the proposed mechanism cannot
  produce the failure. Ruled out.

This hypothesis came from `EVD-2026-0002`, the model assertion. It sounded
plausible and turned out to be wrong. Testing it cost one action; acting on it
would have cost a code change built on nothing.

### H2 — Token signing configuration changed with the deployment — RETIRED

**Status: UNTESTED** — never tested; withdrawn before any action was taken.

- **Statement:** the deployment altered the token signing configuration, so
  tokens issued before it are no longer accepted afterwards.
- **Retired because:** on a second reading this was `INV-2026-0001#H1` in
  different words — the same proposed mechanism, described at a different
  level. It was never a distinct explanation, so it was withdrawn rather than
  tested twice.
- **Retirement is not rejection.** No evidence ruled this out; it was
  withdrawn. `RETIRED` is a note in this record, not a hypothesis status.

This section stays, and keeps its number. It had already been referenced
externally by the time it was withdrawn, and deleting it would leave that
reference pointing at nothing — or worse, at a later hypothesis that happened
to inherit the number.

### H3 — Clock skew between the two services widens after deployment

**Status: SUPPORTED**

- **Statement:** the two services disagree about the current time by enough that
  a token issued by one is treated as already expired by the other.
- **Tests:** reproduced the failure while capturing the timestamps each service
  used to issue and validate the token.
- **Supporting evidence:** `EVD-2026-0004` — the reproduction, showing a
  consistent offset between issuing and validating time, large enough to place
  the token outside its validity window on arrival.
- **Evaluation:** the observed offset is consistent with the failure pattern,
  including why immediate follow-up requests pass and delayed ones do not.
  Consistent with the evidence — not proven.

### H4 — A middleware ordering change dropped the session header

**Status: UNTESTED**

- **Statement:** a change in middleware ordering causes the session header to be
  stripped before validation.
- **Tests:** none performed.
- **Evaluation:** none. This remains a candidate explanation and nothing more.
  It is recorded so that it is visibly open rather than quietly forgotten.

Added after `INV-2026-0001#H2` was retired. It took the next unused number
rather than the freed-looking 2 — numbers are not freed.

## Actions

| # | Action | Tested | Produced |
| --- | --- | --- | --- |
| 1 | Run the authentication test suite | — | `EVD-2026-0001` |
| 2 | Compare service configuration across the deployment boundary | `INV-2026-0001#H1` | `EVD-2026-0003` |
| 3 | Reproduce the failure while capturing issue and validation timestamps | `INV-2026-0001#H3` | `EVD-2026-0004` |
| 4 | Search project documentation for a stated clock-synchronisation requirement | `INV-2026-0001#H3` | nothing found |

No action tested `INV-2026-0001#H2`; it was withdrawn before any was taken.

Action 4 produced no evidence record. That is recorded rather than omitted: the
absence of a documented requirement is worth knowing, and an action with no
result cannot move a hypothesis on its own.

## Current conclusion

The evidence supports clock skew between the two services as the mechanism
producing the failures. `EVD-2026-0004` shows the offset directly, and the
pattern of which tests fail matches it. `INV-2026-0001#H1` is ruled out by
`EVD-2026-0003`.

What this does **not** say:

- It does not say `INV-2026-0001#H3` is proven. The evidence gathered is
  consistent with it and nothing gathered contradicts it. That is a weaker
  claim, and it is the claim being made.
- It does not rule out `INV-2026-0001#H4`, which was never tested. If the
  offset is corrected and failures persist, that is where to resume — as a new
  investigation, since this one is closed.
- It says nothing about `INV-2026-0001#H2`, which was withdrawn untested. A
  retired hypothesis is neither supported nor ruled out.
- It does not decide anything. Whether to correct the skew, and how, is a
  choice for the human authority, and would be its own Task.

Had the reproduction in action 3 come back ambiguous, this inquiry would still
have finished — but with the answer classified as inconclusive rather than
supported. Finishing and concluding little is a different outcome from stopping
early, and the frontmatter records which one happened.

`EVD-2026-0002` remains a model assertion. Nothing here upgraded it, and the
fact that it pointed at a wrong hypothesis did not change it either. It records
what was asserted; that is all it ever recorded.
