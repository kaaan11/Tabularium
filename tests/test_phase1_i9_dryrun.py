"""Phase I (I9) `run --dry-run` surface tests.

The dry-run entry point shares ONE engine with `tabu explain`; only the
originating command label differs. Decision C is enforced at the parser
boundary: machine-readable output exists ONLY for the dry-run mode — a real
run gains no JSON contract, and `continue` gains none at all.
"""

from __future__ import annotations

import contextlib
import io
import unittest

from tabularium import explain as X


from tests.test_phase1_i8_explain import ExplainFixture
from tabularium.cli import main  # noqa: E402


class SharedEngineParityTests(ExplainFixture):
    def test_explain_and_dry_run_share_one_engine(self):
        plain = X.preflight("work", self.root)
        dry = X.preflight("work", self.root, command="dry-run")
        self.assertEqual(plain.status, X.WOULD_RUN)
        self.assertEqual(plain.to_dict() | {"command": "dry-run"},
                         dry.to_dict())



class DryRunCliTests(ExplainFixture):
    def test_cli_dry_run_labels_origin_but_matches_facts(self):
        code, out, _ = self.run_main(["run", "work", str(self.root),
                                      "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("Tabularium preflight (run --dry-run)", out)
        _, explain_out, _ = self.run_main(["explain", "work", str(self.root)])
        dry_body = [line for line in out.splitlines()
                    if not line.startswith("Tabularium preflight")]
        explain_body = [line for line in explain_out.splitlines()
                        if not line.startswith("Tabularium preflight")]
        self.assertEqual(dry_body, explain_body)

    def test_decision_c_plain_run_json_is_a_usage_error(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                main(["run", "work", str(self.root), "--json"])
        self.assertIn("--dry-run", err.getvalue())
        # ...and continue has no JSON flag at all.
        out2, err2 = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out2), contextlib.redirect_stderr(err2):
            with self.assertRaises(SystemExit):
                main(["continue", str(self.root), "--json"])
        self.assertIn("unrecognized arguments", err2.getvalue())



if __name__ == "__main__":  # pragma: no cover
    unittest.main()
