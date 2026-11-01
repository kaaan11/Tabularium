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
pip install -e ".[runtime]"

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
3. If `.ai/change.yaml` names a test command, your tests run against a
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
tests are skipped with a warning. `tabu onboard` can create this file during
onboarding; for an already onboarded project, create it by hand.

`--change` needs a clean working tree and a worker: the Codex CLI (`codex` on
`PATH`, the default when present), OpenCode (`--worker opencode`), or an API
model through LiteLLM (`--worker api`, `pip install -e ".[api]"`).

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

**Not guaranteed:**

- **The worker is not sandboxed by Tabularium.** It runs with your user's
  permissions, network access and its own credentials. Worktree isolation
  protects your checkout, not your machine. Only the test run is sandboxed.
- **Correctness is not verified.** Passing your tests is the only
  correctness signal. A reviewed diff is still your responsibility.
- **No concurrency control.** Do not run two Tabularium operations on one project at
  once.
- **Live providers are not tested.** Worker integrations are covered by
  offline fixtures. A given provider or CLI version may still fail.
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

```bash
pip install -e ".[api,runtime,observability,dev]"
python -m pytest tests -q
```

`examples/skills/architecture-review/` is the reference skill package used by
the skill tests.

## License

MIT. See [LICENSE](LICENSE).
