"""The template-only banner never reaches a project, and doctor flags it."""

from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tabularium.cli import main
from tabularium.diagnostics import WARN, diagnose
from tabularium.init import TEMPLATE_BANNER, InitError, _strip_template_banner, initialize

FOUNDATIONS = ("project.md", "current-state.md")


class TemplateBannerTests(unittest.TestCase):
    def _project(self) -> Path:
        holder = tempfile.TemporaryDirectory(prefix="tabu-banner-test-")
        self.addCleanup(holder.cleanup)
        root = Path(holder.name)
        subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "user@example.com"], cwd=root, check=True)
        (root / "README.md").write_text("# Project Fixture\n", encoding="utf-8")
        initialize(root)
        return root

    def _banner_checks(self, root: Path):
        return [check for check in diagnose(root, "doctor").checks
                if check.reason_code == "TEMPLATE_BANNER_PERSISTED"]

    def test_templates_still_carry_the_banner(self):
        # The banner belongs to the shipped templates; only rendering removes it.
        repo = Path(__file__).resolve().parents[1]
        for name in FOUNDATIONS:
            text = (repo / "tabularium" / "templates" / "project" / name).read_text(encoding="utf-8")
            self.assertEqual(text.count(TEMPLATE_BANNER), 1, name)

    def test_init_does_not_persist_the_banner(self):
        root = self._project()
        for name in FOUNDATIONS:
            text = (root / ".ai" / name).read_text(encoding="utf-8")
            self.assertNotIn("TEMPLATE ONLY", text, name)
            self.assertIn("**CLASSIFICATION:", text, name)
        self.assertEqual(self._banner_checks(root), [])

    def test_onboard_does_not_persist_the_banner(self):
        root = self._project()
        inputs = ["My Title", "My Purpose", "My Scope", "", "", "y", "", ""]
        with patch("sys.stdin", io.StringIO("\n".join(inputs) + "\n")), \
                patch("sys.stdout", io.StringIO()) as stdout, patch("sys.stderr", io.StringIO()):
            code = main(["onboard", str(root)])
        self.assertEqual(code, 0, stdout.getvalue())
        self.assertIn("Project onboarding: ONBOARDED", stdout.getvalue())
        for name in FOUNDATIONS:
            text = (root / ".ai" / name).read_text(encoding="utf-8")
            self.assertNotIn("TEMPLATE ONLY", text, name)
        self.assertIn("My Title", (root / ".ai" / "project.md").read_text(encoding="utf-8"))
        self.assertEqual(self._banner_checks(root), [])

    def test_doctor_warns_when_a_project_still_carries_the_banner(self):
        root = self._project()
        before = diagnose(root, "doctor").overall_state
        path = root / ".ai" / "current-state.md"
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        lines.insert(2, TEMPLATE_BANNER + "\n\n")
        path.write_text("".join(lines), encoding="utf-8")

        result = diagnose(root, "doctor")
        checks = [check for check in result.checks
                  if check.reason_code == "TEMPLATE_BANNER_PERSISTED"]
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].status, WARN)
        self.assertIn("current-state.md", checks[0].message)
        # A cosmetic leftover is reported, not escalated.
        self.assertEqual(result.overall_state, before)

    def test_strip_requires_exactly_one_banner(self):
        body = "# Title\n\n" + TEMPLATE_BANNER + "\n\n**CLASSIFICATION: X**\n"
        self.assertEqual(_strip_template_banner(body, "x.md"), "# Title\n\n**CLASSIFICATION: X**\n")
        crlf = body.replace("\n", "\r\n")
        self.assertEqual(_strip_template_banner(crlf, "x.md"), "# Title\r\n\r\n**CLASSIFICATION: X**\r\n")
        for bad in ("# Title\n", body + TEMPLATE_BANNER + "\n"):
            with self.assertRaises(InitError) as ctx:
                _strip_template_banner(bad, "x.md")
            self.assertEqual(ctx.exception.code, "TEMPLATE_INVALID")


if __name__ == "__main__":
    unittest.main()
