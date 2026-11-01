"""Read the normative contracts from a checkout or installed wheel."""

from importlib.resources import files

SCHEMA_NAMES = (
    "task", "evidence", "investigation", "decision", "question", "handoff", "report",
)


def schema_text(name: str) -> str:
    """Return a named contract as UTF-8 text (e.g. ``schema_text('task')``).

    These human-readable YAML documents are contracts, not JSON Schema.
    No source checkout or persistent filesystem path is required.
    """
    if name not in SCHEMA_NAMES:
        raise ValueError(f"unknown schema: {name!r}")
    return files("tabularium.schemas").joinpath(f"{name}.schema.yaml").read_text(encoding="utf-8")
