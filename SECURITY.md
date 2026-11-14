# Security policy

## Supported versions

Tabularium is alpha software. Security fixes are made on the `main` branch.

<!-- TODO(owner): confirm or replace the supported-versions statement below. -->

| Version | Supported |
| --- | --- |
| `main` | yes |
| older snapshots | no |

## Reporting a vulnerability

Please do **not** open a public issue, pull request or discussion for a
suspected vulnerability, and do not publish exploit details before a fix is
available.

Report it privately through GitHub's private vulnerability reporting: open the
repository's **Security** tab and choose **Report a vulnerability**.

<!-- TODO(owner): enable "Private vulnerability reporting" in the repository
settings (Settings > Code security) so the button above exists. Optionally add
a fallback contact here. -->

Useful details to include:

- the affected version or commit;
- the component (for example the change gates, the test sandbox, record
  authorization or a worker integration);
- steps to reproduce, ideally with a minimal project under `.ai/`;
- the impact you expect.

## What to expect

<!-- TODO(owner): state the response commitments, for example the time to
acknowledge a report, the time to an initial assessment and how disclosure is
coordinated. -->

TODO(owner): response times and disclosure process.

## Scope notes

<!-- TODO(owner): confirm this scope statement. -->

The README section "What it guarantees, and what it does not" describes the
intended security boundary. In particular, the worker process is not sandboxed
by Tabularium; only the test run under `--change` is. A report that a worker
can act with the user's own permissions is expected behaviour, not a
vulnerability, unless it bypasses a documented guarantee such as the change
gates or record authorization.
