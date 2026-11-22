# Security policy

## Supported versions

Tabularium is alpha software. Only the `main` branch receives security support.

| Version | Supported |
| --- | --- |
| Current `main` | yes |
| Other branches, release tags, and older snapshots | no |

## Reporting a vulnerability

Use GitHub private vulnerability reporting for suspected vulnerabilities.
Open the repository's **Security** tab and choose **Report a vulnerability**.

Include the following details when possible.

- the affected version or commit
- the component (for example the change gates, the test sandbox, record
  authorization or a worker integration)
- steps to reproduce, ideally with a minimal project containing its `.ai/` directory
- the expected impact

TODO(owner): Decide whether to provide a fallback contact for reporters who
cannot use GitHub private vulnerability reporting.

## What to expect

Maintainers review reports as time permits, with no guaranteed response time.

TODO(owner): Define how maintainers and reporters coordinate public disclosure.

## Scope notes

With `tabu run --change`, Tabularium runs built-in CLI workers and their
subprocesses in bubblewrap mount and PID namespaces by default. Tabularium
refuses the run when required isolation is unavailable. The worker shares the
host network and can transmit project content and explicitly passed credentials.
The separate test profile isolates the network.

Tabularium refuses API workers in `--change` before any provider call. Custom
adapters require the explicit `--allow-unconfined-worker` override, which
disables worker containment for that invocation. Workers outside `--change` run
without Tabularium OS containment. See the [worker security boundary](WORKER_SECURITY.md)
and the [README guarantees](README.md#what-it-guarantees-and-what-it-does-not).
