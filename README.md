# Tabularium

Tabularium puts an AI coding agent inside a governed workflow. Changes are tied to a
Task, isolated in a git worktree, checked by gates and your tests, and applied
only after you confirm them.

It keeps project context in plain files under `.ai/`: the charter, constraints,
Tasks, Decisions and Evidence. Every run loads that context the same way and
gives the agent a bounded slice of it. Nothing becomes project record without an
explicit human authorization bound to the exact content.

**Status: alpha (`0.3.0.dev0`).** Interfaces and file formats may still change
between releases. Linux is the supported platform.

## Five-minute tour

Requirements: Python 3.12+, git with `user.name` and `user.email` set.
`bubblewrap` (`bwrap`) is needed to run your tests in isolation during
`--change`.

```bash
git clone https://github.com/kaaan11/Tabularium.git && cd Tabularium
python -m venv .venv && . .venv/bin/activate
python -m pip install -e ".[runtime,dev]"

cp -r examples/demo-project /tmp/demo && cd /tmp/demo
git init -q && git add -A && git commit -qm "demo"

tabu status                         # READY: project, context and foundations are valid
tabu task list                      # one completed Task, one active Task
tabu explain "add kelvin conversions"
```

`explain` runs every pre-execution gate of a real run: project health, Task
selection, worker selection, context projection. It starts no worker, contacts
no provider and writes nothing. On the demo it ends with:

```text
Task: TASK-2026-0002
Worker: codex-cli [CLI_CODING]
Status: would-run
All preflight gates passed: this instruction WOULD start governed execution now.
```

The worker line depends on what is installed; `tabu doctor` lists the available
workers and capabilities. With no worker installed, `explain` stops at
`WORKER_SELECTION_REQUIRED` and names the options: `--worker native`,
`--worker opencode`, or `TABU_MODEL` for `--worker api`.

### Your own project

```bash
cd my-project                 # a git repository
tabu init                     # scaffold .ai/; creates no records or approvals
tabu onboard                  # you state title, purpose, scope; you authorize the write
tabu task create --title "Add a health check" --objective "Expose /health" \
    --type IMPLEMENTATION --priority NORMAL
tabu explain "add a health check endpoint"
```

## Making a change: `tabu run --change`

```bash
tabu run "add kelvin conversions" --change
```

1. The worker edits a separate git worktree, not your checkout.
2. The diff is checked by the gates. Writes to `.git/`, `.ai/`, git
   configuration files, agent configuration (`.claude`, `.codex`,
   `opencode.json`) and key or `.env` files are refused. A change can touch at
   most 20 files.
3. `.ai/change.yaml` must name a test command. Your tests run against a
   disposable copy of the project with the change applied, inside a bubblewrap
   sandbox with no network. A failing or timed-out test refuses the change.
4. You see the diff and the test result, and answer once. `y` applies the change
   and commits it with the Task id. Anything else leaves your checkout untouched.

```yaml
# .ai/change.yaml
test_command: [".venv/bin/python", "-m", "pytest", "-q"]
timeout_seconds: 300   # optional, 1-1800
```

`test_command` is an argument list, not a shell line. Without `.ai/change.yaml`,
`--change` refuses with `TESTS_NOT_CONFIGURED` before starting a worker or
creating a Task. `tabu onboard` can create this file during onboarding; for an
already onboarded project, create it by hand.

To explicitly permit an untested change for one run, use
`tabu run "add kelvin conversions" --change --allow-untested`. This is separate
from approving the diff: you still review and confirm it. The warning, execution
report, and commit record that tests were **not run** and that the override was
used. Non-interactive runs cannot apply changes, even with the override. Invalid
configuration, sandbox errors, failing tests, and timeouts still refuse the change.

`tabu explain "add kelvin conversions" --change` and
`tabu run "add kelvin conversions" --change --dry-run` diagnose test readiness
without starting a worker, running tests, or writing files. A configured command
has not yet passed; even passing tests do not prove semantic correctness.

`--change` needs a clean working tree and a worker that edits files: the Codex
CLI (`codex` on `PATH`, the default when present) or OpenCode
(`--worker opencode`). The API worker (`--worker api`) only returns text, so
`--change --worker api` is refused with `CHANGE_WORKER_UNSUPPORTED` before any
provider call; use it without `--change`.

## What it guarantees, and what it does not

**Guarantees, each covered by the test suite:**

- **Records need authorization.** A durable record (charter, Task,
  Decision, ...) is written only with an explicit authorization bound by SHA-256
  to its exact content. Approving an execution never authorizes a write.
- **Fail-closed checks.** Invalid records, a version mismatch, ambiguous Task
  selection and gate failures refuse the operation with a reason code. Tabularium
  never repairs or rewrites your records.
- **Read-only diagnostics.** `status`, `doctor`, `explain` and `--dry-run`
  write nothing.
- **Gated changes.** `--change` applies nothing you did not see and confirm,
  and nothing that failed the gates or your tests.
- **Project mutations are serialized.** `run` (including `--change`), `continue`,
  Task creation/transitions/approval, initialization, onboarding writes, and record
  persistence share a nonblocking advisory lock. It spans preflight through
  approval, tests, apply, report/commit writing, and cleanup. Linked Git worktrees
  share their repository's lock. A concurrent mutation is refused with
  `PROJECT_BUSY` and holder details; retry when it finishes. Read-only diagnostics,
  Task inspection, `task create --print`, and onboarding proposals stay available.
  External editors and Git commands must still be coordinated separately.

**Not guaranteed:**

- **The worker is not sandboxed by Tabularium.** It runs with your user's
  permissions, network access and its own credentials. Worktree isolation
  protects your checkout, not your machine. Only the test run is sandboxed.
- **Correctness is not verified.** Passing your tests is the only
  correctness signal. A reviewed diff is still your responsibility.
- **Crash recovery is manual.** A killed process releases its OS lock but leaves
  an owner record; the next mutation refuses with `PROJECT_LOCK_STALE`. Inspect
  `git status` / `git diff`, any `tabu-apply.journal` in the Git common directory,
  `git worktree list` for abandoned worktrees, and `tabu task list` for unfinished
  candidate Tasks. For a journal, inspect every listed file, restore or keep its
  changes, and delete the journal only after resolving that interrupted apply.
  Remove abandoned worktrees and prune their Git metadata. After inspection,
  delete only the owner record named in the diagnostic, then resolve unfinished
  Tasks through their normal lifecycle commands before retrying the run. Never delete
  `tabu-project.lock`: its inode must remain stable. Lock errors fail closed with
  `PROJECT_LOCK_UNAVAILABLE`. Git repositories store both files in their common
  Git directory; non-Git projects use a canonical-path hash under private per-user
  temporary storage (`tabularium-project-locks-<uid>`).
- **Live providers are not tested in CI.** Worker integrations are covered by
  offline fixtures. A given provider or CLI version may still fail. Opt-in
  live smoke checks exist for maintainers (see Development); their result
  holds only for the versions they report.
- **No automation of approvals.** Task transitions (`start`, `approve`,
  `complete`) and record authorizations stay manual by design.

## Commands

| Command | What it does |
| --- | --- |
| `init`, `onboard` | Scaffold `.ai/`; record project identity after your review |
| `status`, `doctor` | Read-only health and capability report |
| `task create/list/show/validate` | Create and inspect Task records through the same loader `run` uses |
| `task start/approve/complete/transition` | Human-invoked lifecycle transitions, validated and fail-closed |
| `explain`, `run --dry-run` | Preflight: what a run would do, without running it |
| `run`, `run --change`, `continue` | Governed execution; `--change` as above; `continue` resumes an approval checkpoint |

`task list|show|validate|create`, `explain` and `run --dry-run` accept `--json`.
Exit codes: `0` success, `1` refused or invalid, `2` usage or target error.

Experimental: `--workflow dag`, the `tui`, and the `interop`, `remote` and
`memory` extensions.

## Development

[pyproject.toml](pyproject.toml) is the dependency source of truth: base installs
PyYAML; optional extras are `api` (LiteLLM), `runtime` (LangGraph and SQLite
checkpointer), `observability` (OpenTelemetry and Langfuse), and `dev` (Pydantic,
pytest and pytest-subtests). The tour includes `dev` so the demo's configured
pytest command is available. See the [engine package map](tabularium/engine/README.md)
for component boundaries.

From the repository root, in an activated Python 3.12+ virtualenv on Linux:

```bash
python -m pip install -e ".[api,runtime,observability,dev]"
python -m pytest tests -q
```

The public [CI workflow](.github/workflows/ci.yml) uses Linux, Python 3.12/3.13
and bubblewrap with usable unprivileged user namespaces. The full suite includes
[documentation drift checks](tests/test_packaging_drift.py) against the public
checkout; run them after exporting the repository and before publishing.

The default run is fixture-tested only: it makes no provider or model calls
and needs no credentials. Live smoke checks against real workers are opt-in:

```bash
TABULARIUM_LIVE_SMOKE=api,codex,opencode python -m pytest tests/test_live_smoke.py -v -s -rA
```

`api` sends one bounded request (needs one of `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY` and the `api` extra);
`codex` and `opencode` run an approved and a declined `run --change` with the
installed CLI (`TABU_NATIVE_EXECUTABLE` / `TABU_OPENCODE_EXECUTABLE` override
the lookup; `TABU_MODEL` picks the OpenCode model). These calls are billable
and use your own credentials. Each check prints a `LIVE-SMOKE` line with the
CLI or provider version and the outcome code, never provider output. A
requested target whose prerequisite is missing fails with the missing piece
named; targets you did not request are skipped as `NOT VERIFIED`.

`examples/skills/architecture-review/` is the reference skill package used by
the skill tests.

## License

MIT. See [LICENSE](LICENSE).
