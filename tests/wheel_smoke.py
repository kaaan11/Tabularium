"""Installed-wheel smoke: run with ``python -I tests/wheel_smoke.py``.

Install a noneditable wheel plus pydantic first. Isolated mode deliberately
prevents the checkout or PYTHONPATH from satisfying these imports.
"""

import yaml

from tabularium.schema_resources import SCHEMA_NAMES, schema_text
from tabularium.engine.context_loader.validate import SUPPORTED_VERSION, validate_consumed
from tabularium.engine.shadow_contracts import validate_shadow


def main():
    for name in SCHEMA_NAMES:
        contract = yaml.safe_load(schema_text(name))
        assert contract["schema"] == name
        assert contract["schema_version"] == SUPPORTED_VERSION[name.title()]
    records = {
        "Task": dict(schema_version="1.10", id="TASK-2026-0001", title="Smoke",
                     objective="Check installed contracts", status="INBOX",
                     created_at="2026-10-22T00:00:00Z", created_by="USER"),
        "Evidence": dict(schema_version="1.4", id="EVD-2026-0001", title="Smoke",
                         source_type="OBSERVATION", status="ACTIVE",
                         observation="Offline wheel check",
                         collected_at="2026-10-22T00:00:00Z", collected_by="test"),
    }
    for family, record in records.items():
        for valid, candidate in ((True, record), (False, {**record, "schema_version": "999"})):
            assert (not validate_consumed(family, candidate)) == valid
            assert validate_shadow(family, candidate).valid == valid
    print("wheel smoke: 7 contracts, 4 valid/invalid conformance cases passed")


if __name__ == "__main__":
    main()
