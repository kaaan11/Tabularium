# Coding worker boundary

For `tabu run --change`, Tabularium treats source files, repository configuration,
model output, and the coding worker's subprocesses as untrusted. Prompt injection
can cause arbitrary worker actions. A separate Git worktree provides checkout
separation; the bubblewrap process boundary provides filesystem confinement.
The operator, Tabularium controller, installed runtime files, kernel and
bubblewrap binary are trusted. This is not a VM or a defense against a hostile
host process or a kernel vulnerability.

Containment is the default for both built-in coding workers. Tabularium uses
`engine/code_gate/isolated_run.py`, the same runner used for isolated tests,
with a separate worker mount/network profile. It never substitutes a plain
subprocess when sandbox setup fails.

## Filesystem and environment

| Resource | Worker access |
| --- | --- |
| Change worktree | Read/write at `/work`; `.ai` and `constitution` directories are mounted read-only when present as real directories |
| Original checkout and Git common directory | Not mounted; the worktree's `.git` pointer is present, but its host destination is inaccessible |
| Operator's home, login state, SSH agent, credential files, other checkouts, host `/tmp`, `/run`, `/opt` | Not mounted |
| Runtime | Read-only `/usr`, standard `/bin`, `/sbin`, `/lib*` directories or links; no automatic host Python prefix/virtualenv mounts |
| DNS/TLS runtime configuration | Only `/etc/resolv.conf`, `/etc/hosts`, `/etc/nsswitch.conf`, `/etc/ld.so.cache`, `/etc/ssl/certs`, `/etc/ssl/openssl.cnf`, `/etc/localtime` when present, read-only; no whole `/etc` or `/etc/ssl/private` mount |
| Explicitly selected CLI outside `/usr` | Its resolved executable file alone, read-only at `/worker/cli`; not its parent directory |
| Scratch and state | Private 512 MiB `/tmp` tmpfs, home `/tmp/home`, minimal private `/dev`, namespaced `/proc`; discarded at exit |
| Environment | Cleared. Fixed runtime defaults and explicitly named, permitted `TABU_WORKER_ENV` variables only; no implicit credential inheritance |

The writable host-backed path is the worktree. Scratch, CLI state/config/cache,
and the private device filesystem are ephemeral. The namespace root and runtime
mounts are read-only. Symlinks in the worktree cannot make unmounted host paths
visible. Runtime mounts overlapping the original checkout or home are refused.
Do not store credentials under the public runtime allowlist: files under `/usr`
and the listed configuration paths are visible. The worktree contains tracked
project content, including tracked secrets; the boundary does not redact it.

Tabularium fixes `HOME`, `CODEX_HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`,
`XDG_CACHE_HOME`, `XDG_STATE_HOME`, `PATH`, `TMPDIR` and Python runtime variables to
sandbox locations/defaults. Environment passthrough cannot override these.
OpenCode also receives Tabularium-generated `OPENCODE_DISABLE_PROJECT_CONFIG=1`
and `OPENCODE_PERMISSION`; inherited OpenCode control variables are excluded.
Loader/interpreter-control variables are refused by `TABU_WORKER_ENV` policy.

No host configuration or authentication directory is mounted for either CLI.
Codex can write its own state in `/tmp/home/.codex`; OpenCode can write under the
private home/XDG directories. A CLI that requires an existing subscription login
or host config cannot use it in this profile. Configure credentials using the
CLI's supported environment authentication and explicitly list their variable
names in `TABU_WORKER_ENV`. Tabularium does not log in, copy credentials, or
synthesize provider configuration. Passing a credential grants it to the entire
untrusted worker and its children. Provider/version-specific environment
authentication support must be checked separately.

Only standalone CLI executables or installations wholly available under the
runtime allowlist are supported. A home-installed npm launcher that needs
adjacent packages, an interpreter installed under home, plugins beside a binary,
or a host virtualenv may fail to start. Such failures never trigger wider mounts
or unconfined retries. Dependencies already in the worktree remain available.

## Worker matrix

| Worker | What Tabularium enforces during `--change` | Provider flags and limitations |
| --- | --- | --- |
| Native Codex CLI | Entire CLI process and descendants use the worker filesystem/PID boundary; private empty `CODEX_HOME`; cleared/explicit environment; network shared | Still passes `exec --sandbox workspace-write --skip-git-repo-check`. Codex's own tool sandbox may further restrict tools or network; it is not the Tabularium boundary. Compatibility with nested provider sandboxing is version-dependent; Tabularium does not relax flags on failure. No existing session/config is exposed. |
| OpenCode | Same outer process boundary; private empty home/XDG state; cleared/explicit environment; network shared | `run --format json --pure --dir /work`; project config disabled. Change-mode permissions allow read/list/glob/grep/edit and deny bash, external-directory access, task, web tools and unnamed tools. These are OpenCode tool controls, not OS confinement; the outer boundary also covers CLI/plugin code. No existing session/config is exposed. |
| API worker | Refused before any provider call with `CHANGE_WORKER_UNSUPPORTED`; returns text and cannot produce this path's worktree edits | No provider CLI sandbox. Outside `--change`, API calls run in the controller and have no Tabularium OS containment. Returned model text grants no filesystem tools through this adapter. |
| Custom/in-process adapters, including CLI subclasses | Refused with `WORKER_SANDBOX_UNAVAILABLE` unless the explicit unconfined override is used | Changing `cwd` or setting a sandbox attribute cannot contain arbitrary Python adapter code. |

Outside `--change`, Tabularium does not apply this OS boundary to workers.

## Network, subprocesses and resources

Workers need provider egress. The worker profile uses `--unshare-all --share-net`:
**the host network is shared without an egress firewall or endpoint allowlist**.
This includes loopback, LAN/metadata services and abstract Unix sockets. Filesystem
socket paths and SSH-agent environment variables are absent unless explicitly
made available, but the mount boundary cannot protect services reachable over
the shared network. Project content and passed credentials can be exfiltrated.
No network-blocking claim is made for workers. The separate **test** profile
continues to use an unshared network namespace.

Workers and descendants share the same restricted mount, PID, IPC, UTS and user
namespace boundary. Nested user namespaces are disabled, capabilities are
dropped, and a new session is used. PID namespace teardown removes descendants,
including processes in other sessions. The controller enforces timeouts,
cancellation and bounded captured output. Captured worker output is buffered
until completion. There is no CPU/memory cgroup, syscall allowlist, worktree disk
quota or guarantee against resource exhaustion. Scratch has a size limit, while
worktree writes consume host disk.

## Refusal and explicit override

Linux and bubblewrap supporting `--disable-userns` and `--json-status-fd`, with
usable user/mount/PID and related namespaces and the required runtime files,
are prerequisites. Missing bubblewrap, denied namespaces/mounts, and failure to
launch a command inside the sandbox produce `WORKER_SANDBOX_UNAVAILABLE` with
the missing prerequisite or bubblewrap error. A Report under `.ai/reports`
records the refusal and reason; an automatically created candidate Task is
cancelled, and the worktree is cleaned up. Report writes use the existing
governed persistence gate. `explain`/`--dry-run` start no sandbox and do not prove
host sandbox readiness or provider authentication.

`--allow-unconfined-worker` disables only worker containment for that invocation.
The worker then runs with host user access, the CLI's normal host state/config,
and its existing environment policy. Tabularium prints and records a warning,
reports `Worker isolation: DISABLED (--allow-unconfined-worker)`, and adds
`Worker-isolation: disabled (--allow-unconfined-worker)` to the applied change's
commit metadata. Gates, explicit diff approval, and isolated tests remain required
unless separately covered by the existing `--allow-untested` policy.

Offline tests in `tests/test_worker_isolation.py` use stub executables for both
CLIs, exercise actual filesystem denials and child processes when the worker
profile is available, and characterize fail-closed setup and the recorded override.
They do not make provider calls or establish compatibility with real CLI versions,
provider authentication or external network connectivity.
