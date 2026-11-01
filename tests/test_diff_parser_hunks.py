"""Hunk bodies are content: a line's prefix inside a hunk never makes it a header."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tabularium.engine.code_gate import PatchProposal
from tabularium.engine.code_gate.gate import evaluate_patch
from tabularium.engine.code_gate.model import SECRET_MATERIAL_PRESENT
from tabularium.engine.code_gate.parser import parse_unified_diff


class HunkBodyTests(unittest.TestCase):
    def test_added_line_starting_with_plus_plus_is_scanned(self):
        diff = "--- /dev/null\n+++ b/leak.txt\n@@ -0,0 +1 @@\n+++sk-0123456789abcdef\n"
        (patch,) = parse_unified_diff(diff)
        self.assertEqual(patch.added_lines, ("++sk-0123456789abcdef",))
        with tempfile.TemporaryDirectory() as root:
            result = evaluate_patch(PatchProposal("p", "e", diff, ("leak.txt",)), Path(root))
        self.assertFalse(result.allowed)
        self.assertIn(SECRET_MATERIAL_PRESENT, result.reason_codes)

    def test_removed_dash_dash_line_does_not_start_a_new_file(self):
        # "-- note" removed and "++ x" added look like "--- note" / "+++ x" headers.
        diff = ("--- a/q.sql\n+++ b/q.sql\n@@ -1,2 +1,2 @@\n"
                "--- note\n+++ x\n select 1;\n")
        patches = parse_unified_diff(diff)
        self.assertEqual([p.normalized_path for p in patches], ["q.sql"])
        self.assertEqual(patches[0].removed_lines, ("-- note",))
        self.assertEqual(patches[0].added_lines, ("++ x",))

    def test_two_files_still_split_after_a_complete_hunk(self):
        diff = ("--- a/one.txt\n+++ b/one.txt\n@@ -1 +1 @@\n-a\n+b\n"
                "--- a/two.txt\n+++ b/two.txt\n@@ -1 +1 @@\n-c\n+d\n")
        self.assertEqual([p.normalized_path for p in parse_unified_diff(diff)],
                         ["one.txt", "two.txt"])


if __name__ == "__main__":
    unittest.main()
