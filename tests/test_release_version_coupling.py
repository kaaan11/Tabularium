"""Release version-coupling regression pin (AIOS-V02-RC-R01 guard).

The canonical project template and the init/onboard ruleset gate are
version-coupled to the running Tabularium identity. A release bump that changes
VERSION without changing the coupled sources must fail here instead of
shipping fresh projects that are born VERSION_MISMATCH / INVALID_PROJECT.
"""

import tempfile
import unittest
from pathlib import Path

from tabularium.init import RULESET_VERSION, initialize
from tabularium.onboarding import onboard_project
from tabularium.version import get_version
from tabularium.engine.context_loader.loader import load_context
from tabularium.engine.context_loader.manifest import validate


class ReleaseVersionCouplingTests(unittest.TestCase):
    def test_fresh_project_ruleset_is_coupled_to_running_version(self):
        self.assertEqual(RULESET_VERSION, get_version())
        with tempfile.TemporaryDirectory(
                prefix="tabu-jr1-coupling-") as raw:
            target = Path(raw)
            self.assertEqual(initialize(target).status, "INITIALIZED")
            manifest_text = (target / ".ai" / "manifest.yaml").read_text(
                encoding="utf-8")
            manifest, _ = validate(manifest_text)
            self.assertTrue(manifest)
            self.assertEqual(
                manifest.get("tabularium_ruleset"), get_version())
            package = load_context(target)
            self.assertEqual(package.entry_mode, "GOVERNED")
            self.assertEqual(package.package_status, "READY")

    def test_onboarding_accepts_a_freshly_initialized_project(self):
        with tempfile.TemporaryDirectory(
                prefix="tabu-jr1-onboard-") as raw:
            target = Path(raw)
            initialize(target)
            result = onboard_project(target, {
                "title": "Release coupling fixture",
                "purpose":
                    "Pin fresh-project onboarding against version drift",
                "scope": "Bounded offline regression fixture",
            })
            self.assertNotEqual(result.code, "VERSION_MISMATCH")

    def test_manifest_and_template_share_major_minor_with_version(self):
        from tabularium.engine.context_loader.manifest import ruleset_key
        root = Path(__file__).resolve().parents[1]
        version_text = (root / "VERSION").read_text(encoding="utf-8").strip()
        version_key = ruleset_key(version_text)
        self.assertIsNotNone(version_key)

        # The shipped demo project must not be born VERSION_MISMATCH either.
        demo_manifest_path = root / "examples" / "demo-project" / ".ai" / "manifest.yaml"
        demo_manifest_text = demo_manifest_path.read_text(encoding="utf-8")
        demo_manifest, _ = validate(demo_manifest_text)
        self.assertTrue(demo_manifest)
        self.assertEqual(ruleset_key(demo_manifest["tabularium_ruleset"]), version_key)

        template_manifest_text = (root / "tabularium" / "templates" / "project" / "manifest.yaml").read_text(encoding="utf-8")
        template_manifest, _ = validate(template_manifest_text)
        self.assertTrue(template_manifest)
        self.assertEqual(ruleset_key(template_manifest["tabularium_ruleset"]), version_key)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
