"""Packaging drift regression tests (BUG-11 / F9)."""

import tomllib
import unittest
from pathlib import Path

from tabularium.schema_resources import SCHEMA_NAMES, schema_text

ROOT = Path(__file__).resolve().parents[1]


class TestPackagingDrift(unittest.TestCase):
    def test_packaged_schemas_match_normative_sources(self):
        for name in SCHEMA_NAMES:
            with self.subTest(name=name):
                self.assertEqual(schema_text(name), (ROOT / "tabularium" / "schemas" / f"{name}.schema.yaml").read_text(encoding="utf-8"))

    def test_unknown_schema_refused(self):
        with self.assertRaises(ValueError):
            schema_text("../VERSION")

    def test_pyproject_packages_match_the_source_tree(self):
        """Every importable package under tabularium/ is declared.

        F9 was exactly this drift.
        """
        with open(ROOT / "pyproject.toml", "rb") as f:
            declared = set(tomllib.load(f)["tool"]["setuptools"]["packages"])
        roots = [ROOT / "tabularium"]
        on_disk = {
            str(p.parent.relative_to(ROOT)).replace("/", ".")
            for r in roots
            for p in r.rglob("__init__.py")
        }
        self.assertEqual(on_disk - declared, set())


if __name__ == "__main__":
    unittest.main()
