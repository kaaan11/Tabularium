"""Acceptance and unit tests for Tabularium ruleset version-coupling separation."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from tabularium.diagnostics import diagnose, render_json
from tabularium.init import _classify_ai, _manifest_verdict, initialize
from tabularium.onboarding import build_authorizations, onboard_project
from tabularium.engine.context_loader.loader import load_context
from tabularium.engine.context_loader.manifest import (
    ruleset_compatible,
    ruleset_key,
    validate,
)


@contextmanager
def mock_running_version(version_str: str):
    """Context manager mocking the running Tabularium installation version across all surfaces."""
    import tabularium.diagnostics as diag_mod

    orig_read_text = diag_mod._read_text

    def custom_read_text(path: Path) -> str | None:
        if path.name == "VERSION":
            return version_str
        return orig_read_text(path)

    with patch("tabularium.version.get_version", return_value=version_str), \
         patch("tabularium.init.get_version", return_value=version_str), \
         patch("tabularium.init.RULESET_VERSION", version_str), \
         patch("tabularium.diagnostics.get_version", return_value=version_str), \
         patch("tabularium.diagnostics._package_versions", return_value=(version_str, version_str, version_str, "repository")), \
         patch.object(diag_mod, "_read_text", side_effect=custom_read_text), \
         patch("tabularium.engine.context_loader.loader.Loader._available_ruleset", return_value=version_str):
        yield


class RulesetKeyUnitTests(unittest.TestCase):
    """Unit tests for ruleset_key and ruleset_compatible functions."""

    def test_valid_pep440_versions_extract_major_minor(self):
        cases = [
            ("0.3.0.dev0", (0, 3)),
            ("0.3.0", (0, 3)),
            ("0.3.1", (0, 3)),
            ("0.3.0rc1", (0, 3)),
            ("0.3.0b1", (0, 3)),
            ("0.3.0a1", (0, 3)),
            ("0.3.0.post1", (0, 3)),
            ("0.3.0+local", (0, 3)),
            ("1.0.0", (1, 0)),
            ("0.4.0", (0, 4)),
            ("2.1.5.dev4", (2, 1)),
        ]
        for val, expected in cases:
            with self.subTest(val=val):
                self.assertEqual(ruleset_key(val), expected)

    def test_invalid_and_non_canonical_versions_return_none(self):
        """Decision & Rationale for '0.3':

        Decision: '0.3' is REJECTED (returns None).
        Rationale: Tabularium ruleset versioning strictly mandates 3-segment canonical PEP 440
        release identifiers (major.minor.patch). Incomplete two-segment versions like
        '0.3' or '99.0' omit the patch level, introducing ambiguity regarding intended
        release baseline. Following the fail-closed principle, unquoted numbers, two-segment
        releases, leading/trailing whitespace, and 'v'-prefixed tags are strictly rejected.
        """
        invalid_cases = [
            "99.0",       # incomplete 2-segment
            "",           # empty string
            "latest",     # non-version string
            "0.3",        # incomplete 2-segment
            " 0.3.0 ",    # leading/trailing whitespace
            "0.3.0 ",     # trailing whitespace
            " 0.3.0",     # leading whitespace
            "v0.3.0",     # 'v' prefix
            "V0.3.0",     # 'V' prefix
            None,         # non-string
            123,          # integer
            "0.3.0-inv",  # invalid PEP 440 suffix
            "1!0.3.0",    # epoch not accepted
        ]
        for val in invalid_cases:
            with self.subTest(val=val):
                self.assertIsNone(ruleset_key(val))

    def test_one_parser_regardless_of_installed_packages(self):
        """Without packaging the decisions are the same; non-canonical spellings fail closed."""
        cases = ["0.3.0.dev0", "0.3.1", "0.3.0.0", "0.3.0.RC1", "0.3.0-1", "0.03.0", "1!0.3.0", "0.3"]
        with_pkg = [ruleset_key(c) for c in cases]
        with patch.dict("sys.modules", {"packaging": None, "packaging.version": None}):
            without_pkg = [ruleset_key(c) for c in cases]
        self.assertEqual(with_pkg, without_pkg)
        self.assertEqual(with_pkg, [(0, 3), (0, 3), None, None, None, None, None, None])

    def test_ruleset_compatible_logic(self):
        self.assertTrue(ruleset_compatible("0.3.0.dev0", "0.3.1"))
        self.assertTrue(ruleset_compatible("0.3.0", "0.3.0rc1"))
        self.assertFalse(ruleset_compatible("0.3.0", "0.4.0"))
        self.assertFalse(ruleset_compatible("0.3", "0.3.0"))
        self.assertFalse(ruleset_compatible("latest", "0.3.0"))
        self.assertFalse(ruleset_compatible("0.3.0", "invalid"))


class AcceptanceTests(unittest.TestCase):
    """The 6 official acceptance criteria for ruleset version coupling."""

    def _setup_governed_project(self, target: Path, ruleset: str = "0.3.0.dev0") -> None:
        """Create a complete initialized and onboarded project."""
        initialize(target)
        manifest_path = target / ".ai" / "manifest.yaml"
        manifest_path.write_text(
            f'manifest_version: "1.0"\ntabularium_ruleset: "{ruleset}"\ntitle: "Acceptance Fixture"\n',
            encoding="utf-8",
        )
        declarations = {
            "title": "Acceptance Project",
            "purpose": "Verify ruleset version compatibility",
            "scope": "Offline test boundary",
        }
        draft = onboard_project(target, declarations)
        if draft.proposal is not None:
            onboard_project(
                target,
                declarations,
                build_authorizations(draft.proposal, "alex", "2026-08-19T12:00:00+00:00"),
            )

    def test_acceptance_1_patch_and_prerelease_remain_compatible(self):
        """1. A project with tabularium_ruleset: '0.3.0.dev0' is GOVERNED/READY when running version is 0.3.1.

        'tabu status' does not emit VERSION_MISMATCH.
        """
        with tempfile.TemporaryDirectory(prefix="tabu-acc1-") as raw:
            target = Path(raw)
            self._setup_governed_project(target, ruleset="0.3.0.dev0")

            with mock_running_version("0.3.1"):
                package = load_context(target)
                self.assertEqual(package.entry_mode, "GOVERNED")
                self.assertEqual(package.package_status, "READY")

                status_result = diagnose(target, "status")
                self.assertNotEqual(status_result.overall_state, "VERSION_MISMATCH")
                self.assertEqual(status_result.overall_state, "READY")
                self.assertFalse(
                    any(c.reason_code == "VERSION_MISMATCH" for c in status_result.checks),
                    "Status checks must not contain VERSION_MISMATCH",
                )

    def test_acceptance_2_minor_version_difference_fails_closed(self):
        """2. The same project receives RULESET_UNAVAILABLE/VERSION_MISMATCH when running version is 0.4.0."""
        with tempfile.TemporaryDirectory(prefix="tabu-acc2-") as raw:
            target = Path(raw)
            self._setup_governed_project(target, ruleset="0.3.0.dev0")

            with mock_running_version("0.4.0"):
                package = load_context(target)
                self.assertEqual(package.entry_mode, "DIAGNOSTIC")
                self.assertEqual(package.package_status, "DIAGNOSTIC_ONLY")
                self.assertTrue(
                    any(d.code == "RULESET_UNAVAILABLE" for d in package.diagnostics),
                    "Context package diagnostics must include RULESET_UNAVAILABLE",
                )

                status_result = diagnose(target, "status")
                self.assertEqual(status_result.overall_state, "VERSION_MISMATCH")
                self.assertTrue(
                    any(c.reason_code == "VERSION_MISMATCH" for c in status_result.checks),
                    "Status checks must include VERSION_MISMATCH",
                )

    def test_acceptance_3_invalid_and_non_canonical_ruleset_values_rejected(self):
        """3. tabularium_ruleset values '99.0', '', 'latest', '0.3', ' 0.3.0 ', 'v0.3.0' are rejected.

        Parameterized test verifying rejection across parser and runtime paths.
        '0.3' decision & rationale is recorded above in RulesetKeyUnitTests.
        """
        values_to_test = [
            "99.0",
            "",
            "latest",
            "0.3",
            " 0.3.0 ",
            "v0.3.0",
        ]
        with tempfile.TemporaryDirectory(prefix="tabu-acc3-") as raw:
            target = Path(raw)
            self._setup_governed_project(target, ruleset="0.3.0.dev0")

            for val in values_to_test:
                with self.subTest(ruleset=val):
                    self.assertIsNone(ruleset_key(val))

                    # Test actual loader and diagnostic refusal
                    manifest_path = target / ".ai" / "manifest.yaml"
                    manifest_path.write_text(
                        f'manifest_version: "1.0"\ntabularium_ruleset: "{val}"\ntitle: "Acc3 Fixture"\n',
                        encoding="utf-8",
                    )
                    with mock_running_version("0.3.0"):
                        package = load_context(target)
                        self.assertEqual(
                            package.entry_mode,
                            "DIAGNOSTIC",
                            f"Expected DIAGNOSTIC entry mode for ruleset value {val!r}",
                        )
                        status_result = diagnose(target, "status")
                        self.assertIn(
                            status_result.overall_state,
                            {"VERSION_MISMATCH", "INVALID_PROJECT"},
                            f"Expected VERSION_MISMATCH or INVALID_PROJECT for {val!r}",
                        )

    def test_acceptance_4_init_writes_running_version(self):
        """4. tabu init with running version 0.3.1 writes 0.3.1 to manifest.

        Project immediately becomes GOVERNED, and onboard accepts it.
        """
        with tempfile.TemporaryDirectory(prefix="tabu-acc4-") as raw:
            target = Path(raw)
            with mock_running_version("0.3.1"):
                init_result = initialize(target)
                self.assertEqual(init_result.status, "INITIALIZED")

                manifest_text = (target / ".ai" / "manifest.yaml").read_text(encoding="utf-8")
                manifest, err = validate(manifest_text)
                self.assertIsNone(err)
                self.assertIsNotNone(manifest)
                self.assertEqual(manifest.get("tabularium_ruleset"), "0.3.1")

                package = load_context(target)
                self.assertEqual(package.entry_mode, "GOVERNED")
                self.assertEqual(package.package_status, "READY")

                declarations = {
                    "title": "Fresh Project",
                    "purpose": "Verify init writes running version",
                    "scope": "Offline test boundary",
                }
                draft = onboard_project(target, declarations)
                self.assertNotEqual(draft.code, "VERSION_MISMATCH")
                self.assertIsNotNone(draft.proposal)

                final = onboard_project(
                    target,
                    declarations,
                    build_authorizations(draft.proposal, "alex", "2026-08-19T12:00:00+00:00"),
                )
                self.assertEqual(final.status, "ONBOARDED")

    def test_acceptance_5_three_paths_agree_on_same_inputs(self):
        """5. init.py, diagnostics.py, and loader.py make identical decisions for the same inputs.

        Evaluated across an explicit table exercising compatible and incompatible pairs.
        """
        decision_table = [
            # (manifest_ruleset, running_version, expected_compatible)
            ("0.3.0.dev0", "0.3.1", True),
            ("0.3.0", "0.3.1", True),
            ("0.3.0rc1", "0.3.0", True),
            ("0.3.0.post1", "0.3.0", True),
            ("0.3.0.dev0", "0.4.0", False),
            ("0.2.0", "0.3.0", False),
            ("1.0.0", "2.0.0", False),
            ("0.3", "0.3.0", False),
            ("99.0", "0.3.0", False),
            ("latest", "0.3.0", False),
            (" 0.3.0 ", "0.3.0", False),
            ("v0.3.0", "0.3.0", False),
        ]

        with tempfile.TemporaryDirectory(prefix="tabu-acc5-") as raw:
            target = Path(raw)
            self._setup_governed_project(target, ruleset="0.3.0.dev0")
            manifest_path = target / ".ai" / "manifest.yaml"

            for manifest_ruleset, running_version, expected_compatible in decision_table:
                with self.subTest(ruleset=manifest_ruleset, running=running_version):
                    manifest_path.write_text(
                        f'manifest_version: "1.0"\ntabularium_ruleset: "{manifest_ruleset}"\ntitle: "Acc5"\n',
                        encoding="utf-8",
                    )
                    with mock_running_version(running_version):
                        # 1. init.py path
                        verdict = _manifest_verdict(manifest_path.read_text(encoding="utf-8"))
                        init_accepted = (verdict == "VALID")

                        # 2. loader.py path
                        package = load_context(target)
                        loader_accepted = (package.entry_mode == "GOVERNED")

                        # 3. diagnostics.py path
                        diag_result = diagnose(target, "status")
                        diag_accepted = not any(
                            c.reason_code == "VERSION_MISMATCH" for c in diag_result.checks
                        )

                        self.assertEqual(
                            init_accepted,
                            expected_compatible,
                            f"init.py decision mismatch for ({manifest_ruleset!r}, {running_version!r})",
                        )
                        self.assertEqual(
                            loader_accepted,
                            expected_compatible,
                            f"loader.py decision mismatch for ({manifest_ruleset!r}, {running_version!r})",
                        )
                        self.assertEqual(
                            diag_accepted,
                            expected_compatible,
                            f"diagnostics.py decision mismatch for ({manifest_ruleset!r}, {running_version!r})",
                        )
                        # Explicitly verify that all 3 paths agree
                        self.assertEqual(init_accepted, loader_accepted)
                        self.assertEqual(loader_accepted, diag_accepted)

    def test_acceptance_6_json_output_comparison_is_major_minor(self):
        """6. In --json outputs, only the comparison value changes to 'MAJOR_MINOR'."""
        with tempfile.TemporaryDirectory(prefix="tabu-acc6-") as raw:
            target = Path(raw)
            self._setup_governed_project(target, ruleset="0.3.0.dev0")

            with mock_running_version("0.3.1"):
                # Check status JSON
                status_raw = render_json(diagnose(target, "status"))
                status_json = json.loads(status_raw)
                self.assertIn("compatibility", status_json)
                self.assertEqual(status_json["compatibility"]["comparison"], "MAJOR_MINOR")
                self.assertEqual(status_json["compatibility"]["required_ruleset"], "0.3.0.dev0")
                self.assertEqual(status_json["compatibility"]["available_ruleset"], "0.3.1")

                # Check doctor JSON
                doctor_raw = render_json(diagnose(target, "doctor"))
                doctor_json = json.loads(doctor_raw)
                self.assertIn("compatibility", doctor_json)
                self.assertEqual(doctor_json["compatibility"]["comparison"], "MAJOR_MINOR")
                self.assertEqual(doctor_json["compatibility"]["required_ruleset"], "0.3.0.dev0")
                self.assertEqual(doctor_json["compatibility"]["available_ruleset"], "0.3.1")

                # Check loader package compatibility serialization
                package = load_context(target)
                self.assertEqual(package.compatibility["comparison"], "MAJOR_MINOR")
                self.assertEqual(package.compatibility["required_ruleset"], "0.3.0.dev0")
                self.assertEqual(package.compatibility["available_ruleset"], "0.3.1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class StatusStateTests(AcceptanceTests):
    """`tabu status` State, which is decided before the loader runs (diagnostics pre-manifest check)."""

    def _state(self, running: str, ruleset: str = "0.3.0.dev0") -> str:
        with tempfile.TemporaryDirectory(prefix="tabu-state-") as raw:
            target = Path(raw)
            self._setup_governed_project(target, ruleset=ruleset)
            with mock_running_version(running):
                return json.loads(render_json(diagnose(target, "status")))["overall_state"]

    def test_patch_bump_is_not_a_version_mismatch(self):
        self.assertNotEqual(self._state("0.3.1"), "VERSION_MISMATCH")

    def test_minor_bump_is_a_version_mismatch(self):
        self.assertEqual(self._state("0.4.0"), "VERSION_MISMATCH")


class RulesetScaffoldDiagnosticsTests(AcceptanceTests):
    """Scaffold classification precedence and compatibility diagnostics."""

    def test_partial_scaffold_with_compatible_ruleset_is_still_partial(self):
        # The diagnostics pre-manifest check must not hide a damaged scaffold:
        # with a compatible ruleset the scaffold is still classified.
        with tempfile.TemporaryDirectory(prefix="tabu-partial-") as raw:
            target = Path(raw)
            self._setup_governed_project(target, ruleset="0.3.0.dev0")
            (target / ".ai" / "tasks" / "README.md").unlink()
            (target / ".ai" / "tasks").rmdir()
            with mock_running_version("0.3.1"):
                state = json.loads(render_json(diagnose(target, "status")))["overall_state"]
            self.assertEqual(state, "PARTIAL_INITIALIZATION")

    def test_template_without_a_ruleset_line_fails_loudly(self):
        from tabularium import init as init_mod
        from tabularium.init import InitError
        def without_ruleset(name: str) -> str:
            return 'manifest_version: "1.0"\n' if name == "project/manifest.yaml" else ""

        with patch.object(init_mod, "_resource_text", side_effect=without_ruleset):
            with self.assertRaises(InitError):
                init_mod._render_templates()
