---
schema_version: "1.4"
id: EVD-2026-0001
title: Authentication tests failed after deployment
source_type: EXPERIMENT
reliability: MEDIUM
status: ACTIVE
observation: Three authentication tests failed.
source: test output
source_locator: "pytest tests/auth/ -v (non-production environment, build 2f9c1ab)"
project: example-service
task: TASK-2026-0001
collected_at: "2026-08-16T13:40:00Z"
collected_by: human:user
---

# Evidence

Example of the canonical stored Evidence record.

Stored, this file would be `.ai/evidence/EVD-2026-0001.md` — the filename
equals the `id`. It sits here under `tabularium/templates/evidence/` because it is a
template, not a live record.

**The frontmatter above is authoritative.** The sections below are narrative
for humans and models: they add detail and context, and they never restate an
id, an enumerated value, a timestamp, or a reference.

## Observation

The suite was run once, end to end, against the non-production environment. Of
24 tests, 21 passed and 3 failed.

All three failures came from the same area: tests that log in, wait, and then
make a second request expecting the session to still be valid. In each case the
second request came back unauthenticated. Tests that only perform a login, and
tests that make an immediate follow-up request, passed.

The failure output showed the second request receiving an authentication
challenge rather than a response, in all three cases.

## Source

The output of a single run of the project's own authentication test suite, in
the non-production environment, at a specific build. The exact invocation and
build are in the frontmatter locator.

Nothing outside that run contributed to this record. No logs, no vendor
documentation, and no model output are included here — those would be separate
records with their own source types.

## Notes

**What this record establishes:** that these three tests failed in this way, in
this run, at this build.

**What it does not establish:** why. This record does not identify a cause, and
deliberately contains no conclusion — interpretation belongs to an
Investigation, not to the record of what was observed.

In particular, it would be wrong to read this as evidence that any specific
component is at fault. A plausible-sounding explanation is a separate claim; if
a model produces one it is recorded as its own assertion, and if it is tested
the test result becomes its own record.

**On the label:** the frontmatter label is a short handle for scanning a
directory of records. It is not a conclusion, and it does not stand in for what
was observed — that is what the observation field is for.

**On the reliability assessment:** the run is reproducible in principle and
came from the project's own suite, which is why it is not treated as weak. It
was a single run on one environment and one build, and flakiness has not been
ruled out by repetition, which is why it is not treated as strong. The
assessment reflects this record itself — not the fact that it happens to be a
test result.

**If this turns out to be wrong or is later replaced:** this record is not
rewritten. It gets marked as superseded or retracted, a new record is created,
and the two point at each other where appropriate.
