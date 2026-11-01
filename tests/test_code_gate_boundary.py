"""Offline regression checks for the experimental code-gate support boundary."""

import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tabularium.engine.code_gate import (
    APPLIED, DENIED, PATCH_APPLY_FAILED, PATCH_AUTHORIZATION_MISMATCH,
    PATCH_SCOPE_UNSUPPORTED, TARGET_CHANGED, TESTS_FAILED, VERIFICATION_REQUIRED,
    apply_patch_transactional, evaluate_patch,
)
from tests.test_phase5_code_gate import CodeGateFixture


DIFF = ('--- a/src/sample.py\n+++ b/src/sample.py\n@@ -1,2 +1,2 @@\n'
        ' def calculate_total(a, b):\n-    return a + b\n+    return a - b\n')


class TestCodeGateBoundary(CodeGateFixture):
    def apply(self, proposal=None, **kwargs):
        proposal = proposal or self.make_proposal(DIFF)
        return apply_patch_transactional(self.root, proposal, self.make_authorization(proposal), **kwargs)

    def test_no_verifier_or_empty_command_is_denied_without_write(self):
        original = self.target_file.read_bytes()
        for kwargs in ({}, {"test_command": []}):
            result = self.apply(**kwargs)
            self.assertEqual(result.status, DENIED)
            self.assertIn(VERIFICATION_REQUIRED, result.reason_codes)
            self.assertEqual(self.target_file.read_bytes(), original)

    def test_multi_file_patch_is_refused_before_any_write_or_verifier(self):
        other = self.root / "src" / "other.py"
        other.write_text("old\n")
        proposal = self.make_proposal(DIFF + '--- a/src/other.py\n+++ b/src/other.py\n@@ -1 +1 @@\n-old\n+new\n',
                                      target_files=("src/sample.py", "src/other.py"))
        original = self.target_file.read_bytes()
        with patch("tabularium.engine.code_gate.sandbox._atomic_write") as write:
            with patch("builtins.print") as verifier:
                result = self.apply(proposal, custom_verifier=verifier)
            verifier.assert_not_called()
            write.assert_not_called()
        self.assertIn(PATCH_SCOPE_UNSUPPORTED, result.reason_codes)
        self.assertEqual(self.target_file.read_bytes(), original)
        self.assertEqual(other.read_text(), "old\n")

    def test_deletion_refused_instead_of_leaving_empty_file(self):
        proposal = self.make_proposal('--- a/src/sample.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-def calculate_total(a, b):\n-    return a + b\n')
        result = self.apply(proposal, custom_verifier=lambda _: (True, ""))
        self.assertIn(PATCH_SCOPE_UNSUPPORTED, result.reason_codes)
        self.assertTrue(self.target_file.is_file())

    def test_verifier_exception_is_failed_and_does_not_write(self):
        original = self.target_file.read_bytes()
        def broken(_):
            raise RuntimeError("verification failed")
        result = self.apply(custom_verifier=broken)
        self.assertEqual(result.status, TESTS_FAILED)
        self.assertEqual(self.target_file.read_bytes(), original)

    def test_verifier_cannot_substitute_staged_bytes(self):
        original = self.target_file.read_bytes()
        def substitute(scratch):
            (scratch / "src/sample.py").write_text("unapproved bytes")
            return True, "passed"
        result = self.apply(custom_verifier=substitute)
        self.assertIn(TARGET_CHANGED, result.reason_codes)
        self.assertEqual(self.target_file.read_bytes(), original)

    def test_changed_project_target_is_not_overwritten(self):
        def stale(_):
            self.target_file.write_text("concurrent edit")
            return True, "passed"
        result = self.apply(custom_verifier=stale)
        self.assertIn(TARGET_CHANGED, result.reason_codes)
        self.assertEqual(self.target_file.read_text(), "concurrent edit")

    def test_atomic_replace_failure_preserves_original_and_cleans_temp(self):
        original = self.target_file.read_bytes()
        with patch("tabularium.engine.code_gate.sandbox.os.replace", side_effect=OSError("injected failure")):
            result = self.apply(custom_verifier=lambda _: (True, "passed"))
        self.assertEqual(result.status, DENIED)
        self.assertIn(PATCH_APPLY_FAILED, result.reason_codes)
        self.assertEqual(self.target_file.read_bytes(), original)
        self.assertEqual(list(self.root.rglob(".tabu-patch-*")), [])

    def test_verifier_sees_full_copy_but_is_not_os_isolated(self):
        original = self.target_file.read_bytes()
        marker = self.root / "outside-staging.txt"
        marker.write_text("trusted verifier can read outside scratch")
        def verifier(scratch):
            self.assertNotEqual(scratch, self.root)
            self.assertEqual(self.target_file.read_bytes(), original)
            self.assertIn("return a - b", (scratch / "src/sample.py").read_text())
            # The copy is the whole project, not just the patched file.
            self.assertEqual((scratch / "outside-staging.txt").read_text(),
                             "trusted verifier can read outside scratch")
            self.assertEqual(marker.read_text(), "trusted verifier can read outside scratch")
            return True, "checked proposed bytes"
        result = self.apply(custom_verifier=verifier)
        self.assertEqual(result.status, APPLIED)
        self.assertIn("return a - b", self.target_file.read_text())

    def test_test_command_passes_and_fails_in_scratch(self):
        original = self.target_file.read_bytes()
        failed = self.apply(test_command=[sys.executable, "-c", "raise SystemExit(1)"])
        self.assertEqual(failed.status, TESTS_FAILED)
        self.assertEqual(self.target_file.read_bytes(), original)
        passed = self.apply(test_command=[sys.executable, "-c", "from pathlib import Path; assert 'return a - b' in Path('src/sample.py').read_text()"])
        self.assertEqual(passed.status, APPLIED)

    def test_authorization_identity_is_bound_without_optional_binding(self):
        proposal = self.make_proposal(DIFF)
        auth = replace(self.make_authorization(proposal), binding="")
        for changed in (replace(proposal, proposal_id="other"), replace(proposal, execution_id="other")):
            self.assertIn(PATCH_AUTHORIZATION_MISMATCH, evaluate_patch(changed, self.root, auth).reason_codes)

    @unittest.skipIf(os.name == "nt", "POSIX modes and symlink fixture")
    def test_internal_symlink_cannot_alias_protected_directory(self):
        (self.root / "alias").symlink_to(self.root / "constitution", target_is_directory=True)
        proposal = self.make_proposal('--- a/alias/immutable-principles.md\n+++ b/alias/immutable-principles.md\n@@ -1 +1 @@\n-# Principles\n+# Changed\n',
                                      target_files=("alias/immutable-principles.md",))
        self.assertFalse(evaluate_patch(proposal, self.root, self.make_authorization(proposal)).allowed)

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_executable_mode_is_preserved(self):
        self.target_file.chmod(0o755)
        self.assertEqual(self.apply(custom_verifier=lambda _: (True, "")).status, APPLIED)
        self.assertEqual(self.target_file.stat().st_mode & 0o777, 0o755)

    def test_single_creation_in_existing_parent_is_supported(self):
        proposal = self.make_proposal('--- /dev/null\n+++ b/src/new.py\n@@ -0,0 +1 @@\n+print("hello")\n',
                                      target_files=("src/new.py",))
        result = self.apply(proposal, custom_verifier=lambda scratch: ((scratch / "src/new.py").read_text() == 'print("hello")\n', "checked"))
        self.assertEqual(result.status, APPLIED)
        self.assertEqual((self.root / "src/new.py").read_text(), 'print("hello")\n')

    def test_creation_collision_missing_update_and_rename_are_refused(self):
        cases = [
            ('--- /dev/null\n+++ b/src/sample.py\n@@ -0,0 +1 @@\n+collision\n', "src/sample.py"),
            ('--- a/src/missing.py\n+++ b/src/missing.py\n@@ -0,0 +1 @@\n+missing\n', "src/missing.py"),
            (DIFF.replace('--- a/src/sample.py', '--- a/src/renamed.py'), "src/sample.py"),
        ]
        original = self.target_file.read_bytes()
        for diff, target in cases:
            with self.subTest(target=target, source=diff.splitlines()[0]):
                proposal = self.make_proposal(diff, target_files=(target,))
                result = self.apply(proposal, custom_verifier=lambda _: (True, ""))
                self.assertIn(PATCH_SCOPE_UNSUPPORTED, result.reason_codes)
        self.assertEqual(self.target_file.read_bytes(), original)

    def test_symlink_swapped_in_during_verification_is_refused_at_commit(self):
        # The target bytes stay identical through the alias, so only the
        # pre-commit gate re-evaluation can notice the new symlink.
        real = self.root / "src_real"
        def swap(_):
            (self.root / "src").rename(real)
            (self.root / "src").symlink_to(real, target_is_directory=True)
            return True, "passed"
        result = self.apply(custom_verifier=swap)
        self.assertEqual(result.status, DENIED)
        self.assertIn("return a + b", (real / "sample.py").read_text())

    def test_truthy_non_true_verifier_result_is_not_a_pass(self):
        original = self.target_file.read_bytes()
        for verdict in ("ok", 1, ["passed"]):
            with self.subTest(verdict=verdict):
                result = self.apply(custom_verifier=lambda _, v=verdict: (v, "looks fine"))
                self.assertEqual(result.status, TESTS_FAILED)
                self.assertEqual(self.target_file.read_bytes(), original)
