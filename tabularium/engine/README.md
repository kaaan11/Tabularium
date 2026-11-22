# Engine packages

**STATUS: ACTIVE — clean baseline tree**

Operational packages supporting Tabularium. Per-file docstrings are
authoritative for each package's boundary; this table is a map, not a spec.

## Components

| Package | Role |
| --- | --- |
| `context_loader/` | Deterministic read-only Selective Context Loader (M2 lineage); entry validation, manifest/ruleset checks, consumed-record validation |
| `shadow_contracts/` | Non-normative executable mirror of the frozen record contracts; development-time drift detection only |
| `workers/` | Worker abstraction and the `WorkerAdapter` boundary |
| `workers/api/` | API worker over LiteLLM (optional extra); the only path with a provider dependency |
| `workers/cli/` | Session-authenticated CLI workers: Codex (`native`) and OpenCode (subprocess transport) |
| `projection/` | Deterministic token-budgeted Context Package projection |
| `skills/` | Read-only skill package loader and governance evaluation (narrowing-only sidecar) |
| `runtime/` | LangGraph governed runtime: approval boundary, checkpoints, resume |
| `execution/` | Governed pipeline wiring loader → skills → projection → worker → runtime |
| `persistence/` | The nine-condition write gate and content-bound authorization/writer |
| `continuation/` | Durable Handoff/Report proposal flow through the persistence gate |
| `tools/` | Governed tool boundary (request / authorization / result kept distinct) |
| `policies/` | Fail-closed policy checks in the execution pipeline: assigned model, risk approval, secrets in context |
| `code_indexer/` | Deterministic AST code indexing, symbol extraction and selection |
| `code_gate/` | Experimental single-file patch gate, not wired to the product CLI |
| `tui/` | Experimental terminal dashboard with approval sheets and background runs |
| `observability/` | Descriptive execution trace model + optional OTel/Langfuse exporters |
| `interop/`, `memory/`, `remote/` | Experimental interoperability, memory and remote capability packages |

The test suites (conformance, adversarial/security, product) live in the
repository-level `tests/` directory, not in this package.

## Dependency declarations

[pyproject.toml](../../pyproject.toml) at the repository root is the dependency
source of truth. Run installation commands from that root in an activated
virtualenv on Linux with Python 3.12+. The [root README](../../README.md)
covers setup and the bubblewrap (`bwrap`) requirement for sandboxed tests.

| Group | Dependencies | Purpose |
| --- | --- | --- |
| `base` | PyYAML | Context loader; installed by `python -m pip install -e .` |
| `api` | LiteLLM | Optional API worker (lazy import) |
| `runtime` | LangGraph and SQLite checkpointer | Governed runtime |
| `observability` | OpenTelemetry API/SDK and Langfuse | Optional exporters and SDK checks |
| `dev` | Pydantic and pytest | Shadow contracts and tests |

For development with all extras:

```bash
python -m pip install -e ".[api,runtime,observability,dev]"
python -m pytest tests -q
```

The `requirements*.txt` files in this directory are per-component notes kept
for the boundary rationale in their comments. They are split so an optional
transport can never become a requirement of the loader, and each lists a
subset of one `pyproject.toml` declaration. They are compatibility notes,
not the installation source of truth; use the extras above. In particular,
`requirements-dev.txt` lists only Pydantic, not the full development environment.

[tests/test_packaging_drift.py](../../tests/test_packaging_drift.py) fails when a file here names a requirement
that `pyproject.toml` does not declare, when this README names a package
directory or requirements file that does not exist, or when an engine package
is missing from the table above. It also checks documented extras, local guide
links, example paths and the demo's Python requirement. The public
[CI workflow](../../.github/workflows/ci.yml) runs it in the full suite on the
exported checkout. Run it again after each export before publishing.
