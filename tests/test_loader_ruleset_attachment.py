"""Validation of source and wheel ruleset attachment in context loader."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tabularium.init import initialize
from tabularium.engine.context_loader import load_context
from tabularium.engine.context_loader.loader import Loader
from tabularium.engine.context_loader import loader as loader_module


ROOT = Path(__file__).resolve().parents[1]


class LoaderRulesetAttachmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tabu-m72-loader-"))
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.assertEqual(initialize(self.project).status, "INITIALIZED")

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_source_checkout_uses_repository_VERSION(self):
        value = Loader(self.project, ai_os_root=ROOT)._available_ruleset()
        self.assertEqual(value, (ROOT / "VERSION").read_text(encoding="utf-8").strip())

    def test_source_like_checkout_missing_VERSION_fails_even_with_metadata(self):
        source_like = self.tmp / "source-like"
        for name in ("tabularium", "tests", "examples"):
            (source_like / name).mkdir(parents=True)
        (source_like / "pyproject.toml").write_text("", encoding="utf-8")
        with patch.object(loader_module, "default_ai_os_root", return_value=source_like), \
                patch.object(loader_module, "installed_version", return_value="0.1.0"):
            self.assertIsNone(Loader(self.project)._available_ruleset())

    def test_site_packages_with_stray_tests_and_examples_is_not_a_checkout(self):
        # Other distributions do install top-level "tests"/"examples" packages.
        site = self.tmp / "site-packages-stray"
        for name in ("tabularium", "tests", "examples"):
            (site / name).mkdir(parents=True)
        with patch.object(loader_module, "default_ai_os_root", return_value=site), \
                patch.object(loader_module, "installed_version", return_value="0.1.0"):
            self.assertEqual(Loader(self.project)._available_ruleset(), "0.1.0")

    def test_installed_wheel_uses_synchronized_package_metadata(self):
        wheel_root = self.tmp / "site-packages"
        wheel_root.mkdir()
        with patch.object(loader_module, "default_ai_os_root", return_value=wheel_root), \
                patch.object(loader_module, "installed_version", return_value="0.1.0"):
            self.assertEqual(Loader(self.project)._available_ruleset(), "0.1.0")

    def test_missing_installed_metadata_fails_explicitly(self):
        wheel_root = self.tmp / "site-packages"
        wheel_root.mkdir()
        with patch.object(loader_module, "default_ai_os_root", return_value=wheel_root), \
                patch.object(
                    loader_module,
                    "installed_version",
                    side_effect=loader_module.PackageNotFoundError,
                ):
            self.assertIsNone(Loader(self.project)._available_ruleset())

    def test_manifest_mismatch_is_still_rejected(self):
        manifest = self.project / ".ai" / "manifest.yaml"
        text = manifest.read_text(encoding="utf-8")
        updated = re.sub(
            r'(?m)^tabularium_ruleset: ".*"$', 'tabularium_ruleset: "9.9.9"', text)
        self.assertNotEqual(updated, text)
        manifest.write_text(updated, encoding="utf-8")
        package = load_context(self.project, ai_os_root=ROOT)
        self.assertEqual(package.entry_mode, "DIAGNOSTIC")
        self.assertEqual(package.package_status, "DIAGNOSTIC_ONLY")
        self.assertTrue(any(d.code == "RULESET_UNAVAILABLE" for d in package.diagnostics))

    def test_source_and_installed_ruleset_paths_are_semantically_equivalent(self):
        source = load_context(self.project, ai_os_root=ROOT)
        released = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        wheel_root = self.tmp / "site-packages"
        wheel_root.mkdir()
        with patch.object(loader_module, "default_ai_os_root", return_value=wheel_root), \
                patch.object(loader_module, "installed_version", return_value=released):
            installed = load_context(self.project)

        self.assertEqual(source.entry_mode, installed.entry_mode)
        self.assertEqual(source.package_status, installed.package_status)
        self.assertEqual(source.task_actionability, installed.task_actionability)
        self.assertEqual(source.foundations.keys(), installed.foundations.keys())
        self.assertEqual(
            [d.code for d in source.diagnostics],
            [d.code for d in installed.diagnostics],
        )


if __name__ == "__main__":
    unittest.main()
