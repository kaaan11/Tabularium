"""Product strings are English: no Turkish-only letters in shipped code."""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TURKISH = re.compile(r"[çğışöüÇĞİŞÖÜ]")


def production_sources() -> list[Path]:
    return sorted((ROOT / "tabularium").rglob("*.py"))


class ProductLanguageTests(unittest.TestCase):
    def test_string_constants_carry_no_turkish_letters(self):
        hits = []
        for path in production_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and TURKISH.search(node.value)):
                    hits.append(f"{path.relative_to(ROOT)}:{node.lineno}: {node.value[:60]!r}")
        self.assertEqual(hits, [])

    def test_scan_covers_the_product(self):
        names = {p.relative_to(ROOT).as_posix() for p in production_sources()}
        self.assertIn("tabularium/change_path.py", names)
        self.assertIn("tabularium/engine/context_loader/loader.py", names)
        self.assertFalse(any("/tests/" in n for n in names))


if __name__ == "__main__":
    unittest.main()
