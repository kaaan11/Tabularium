# Scripts

**STATUS: ACTIVE — clean baseline tree**

Operational packages supporting the Tabularium. Per-file docstrings are
authoritative for each package's boundary; this table is a map, not a spec.

## Components

| Package | Role |
| --- | --- |
| `context_loader/` | Deterministic read-only Selective Context Loader (M2 lineage); entry validation, manifest/ruleset checks, consumed-record validation |
| `shadow_contracts/` | Non-normative executable mirror of the frozen record contracts; development-time drift detection only |
| `workers/` | Worker abstraction and the `WorkerAdapter` boundary |
| `workers/api/` | API worker over LiteLLM (optional extra); the only path with a provider dependency |
| `workers/cli/` | Native session-authenticated CLI worker (subprocess transport) |
| `projection/` | Deterministic token-budgeted Context Package projection |
| `skills/` | Read-only skill package loader and governance evaluation (narrowing-only sidecar) |
| `runtime/` | LangGraph governed runtime: approval boundary, checkpoints, resume |
| `execution/` | Governed pipeline wiring loader → skills → projection → worker → runtime |
| `persistence/` | The nine-condition write gate and content-bound authorization/writer |
| `continuation/` | Durable Handoff/Report proposal flow through the persistence gate |
| `tools/` | Governed tool boundary (request / authorization / result kept distinct) |
| `observability/` | Descriptive execution trace model + optional OTel/Langfuse exporters |
| `interop/`, `memory/`, `remote/` | M6-lineage capability packages (source-checkout scope) |
| `tests/` | Conformance, adversarial/security, and product test suites |

## Dependency declarations

Deliberately separate so an optional transport can never become a requirement
of the loader:

- `requirements.txt` — loader runtime (PyYAML only)
- `requirements-dev.txt` — Pydantic for shadow contracts
- `requirements-api.txt` — LiteLLM for the API worker only (lazy import)
- `requirements-runtime.txt` — LangGraph runtime extras
- `requirements-observability.txt` — optional OTel/Langfuse exporters
