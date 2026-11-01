"""Phase 5 Transactional Write Gate and Sandboxing security contract tests.

Validates the two-phase governed code mutation pipeline:
1. Confinement enforcement (rejection of path traversal and external targets).
2. Protection of repository invariants (.git/, protected paths, credentials).
3. Deep secret scanning of proposed diff additions.
4. Cryptographic diff digest and human PatchAuthorization binding.
5. Sandbox execution and transactional fail-closed rollback when tests fail.
6. Atomic application when tests pass.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tabularium.engine.code_gate import (
    APPLIED,
    DENIED,
    ISOLATED_TESTS_FAILED,
    PATCH_AUTHORIZATION_MISMATCH,
    PATCH_AUTHORIZATION_REQUIRED,
    PROTECTED_TARGET_DENIED,
    PatchAuthorization,
    PatchProposal,
    PatchResult,
    SECRET_MATERIAL_PRESENT,
    TARGET_OUTSIDE_PROJECT,
    TESTS_FAILED,
    apply_patch_transactional,
    evaluate_patch,
    parse_unified_diff,
)


class CodeGateFixture(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-codegate-")
        self.root = Path(self.holder.name)
        # Create a sample project structure
        (self.root / "src").mkdir(parents=True)
        self.target_file = self.root / "src" / "sample.py"
        self.target_file.write_text(
            "def calculate_total(a, b):\n    return a + b\n", encoding="utf-8"
        )
        (self.root / "constitution").mkdir(parents=True)
        (self.root / "constitution" / "immutable-principles.md").write_text("# Principles", encoding="utf-8")

    def tearDown(self):
        self.holder.cleanup()

    def make_proposal(self, diff: str, target_files: tuple[str, ...] = ("src/sample.py",)) -> PatchProposal:
        return PatchProposal(
            proposal_id="patch-p5-001",
            execution_id="exec-p5-001",
            raw_diff=diff,
            target_files=target_files,
            task_id="TASK-2026-0005",
            reason="Phase 5 code gate testing",
        )

    def make_authorization(self, proposal: PatchProposal, actor="alex") -> PatchAuthorization:
        return PatchAuthorization(
            proposal_id=proposal.proposal_id,
            execution_id=proposal.execution_id,
            diff_digest=proposal.diff_digest,
            authorized_by=actor,
            authorized_at="2026-09-06T10:00:00Z",
            binding=proposal.binding(),
            note="authorized by human test runner",
        )


class TestCodeGateSecurityEnforcement(CodeGateFixture):
    def test_path_traversal_refused(self):
        malicious_diff = (
            "--- a/src/sample.py\n"
            "+++ b/../../etc/shadow\n"
            "@@ -1,2 +1,2 @@\n"
            "-def calculate_total(a, b):\n"
            "+root:$6$hacked:18000:0:99999:7:::\n"
        )
        prop = self.make_proposal(malicious_diff, target_files=("../../etc/shadow",))
        auth = self.make_authorization(prop)
        eval_res = evaluate_patch(prop, self.root, auth)
        self.assertFalse(eval_res.allowed)
        self.assertIn(TARGET_OUTSIDE_PROJECT, eval_res.reason_codes)

    def test_protected_constitution_path_refused(self):
        diff = (
            "--- a/constitution/immutable-principles.md\n"
            "+++ b/constitution/immutable-principles.md\n"
            "@@ -1 +1 @@\n"
            "-# Principles\n"
            "+# Weakened Principles\n"
        )
        prop = self.make_proposal(diff, target_files=("constitution/immutable-principles.md",))
        auth = self.make_authorization(prop)
        eval_res = evaluate_patch(prop, self.root, auth)
        self.assertFalse(eval_res.allowed)
        self.assertIn(PROTECTED_TARGET_DENIED, eval_res.reason_codes)

    def test_secret_material_in_diff_addition_refused(self):
        diff_with_secret = (
            "--- a/src/sample.py\n"
            "+++ b/src/sample.py\n"
            "@@ -1,2 +1,3 @@\n"
            " def calculate_total(a, b):\n"
            "+    api_key = 'sk-1234567890abcdef987654321'\n"
            "     return a + b\n"
        )
        prop = self.make_proposal(diff_with_secret)
        auth = self.make_authorization(prop)
        eval_res = evaluate_patch(prop, self.root, auth)
        self.assertFalse(eval_res.allowed)
        self.assertIn(SECRET_MATERIAL_PRESENT, eval_res.reason_codes)

    def test_missing_or_mismatched_authorization_refused(self):
        valid_diff = (
            "--- a/src/sample.py\n"
            "+++ b/src/sample.py\n"
            "@@ -1,2 +1,3 @@\n"
            " def calculate_total(a, b):\n"
            "+    # clean comment\n"
            "     return a + b\n"
        )
        prop = self.make_proposal(valid_diff)
        # 1. Missing auth
        res_no_auth = apply_patch_transactional(self.root, prop, authorization=None)
        self.assertEqual(res_no_auth.status, DENIED)
        self.assertIn(PATCH_AUTHORIZATION_REQUIRED, res_no_auth.reason_codes)

        # 2. Tampered auth (wrong digest)
        bad_auth = PatchAuthorization(
            proposal_id=prop.proposal_id,
            execution_id=prop.execution_id,
            diff_digest="deadbeef" * 8,
            authorized_by="alex",
            authorized_at="2026-09-06T10:00:00Z",
        )
        res_bad_auth = apply_patch_transactional(self.root, prop, authorization=bad_auth)
        self.assertEqual(res_bad_auth.status, DENIED)
        self.assertIn(PATCH_AUTHORIZATION_MISMATCH, res_bad_auth.reason_codes)


class TestSandboxedTransactionalExecution(CodeGateFixture):
    def test_broken_tests_trigger_fail_closed_rollback(self):
        # A diff that breaks code
        breaking_diff = (
            "--- a/src/sample.py\n"
            "+++ b/src/sample.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def calculate_total(a, b):\n"
            "-    return a + b\n"
            "+    raise RuntimeError('Break calculations!')\n"
        )
        prop = self.make_proposal(breaking_diff)
        auth = self.make_authorization(prop)

        # Custom verifier simulating running unit tests on the sandbox
        def test_runner(sandbox_root: Path) -> tuple[bool, str]:
            sb_file = sandbox_root / "src" / "sample.py"
            content = sb_file.read_text(encoding="utf-8")
            # If function raises, tests fail!
            if "raise RuntimeError" in content:
                return False, "Test failed: calculate_total raised RuntimeError"
            return True, "All tests passed."

        result = apply_patch_transactional(
            self.root, prop, auth, custom_verifier=test_runner
        )

        self.assertEqual(result.status, TESTS_FAILED)
        self.assertIn(ISOLATED_TESTS_FAILED, result.reason_codes)

        # CRITICAL ASSERTION: Root project must NOT be mutated!
        current_content = self.target_file.read_text(encoding="utf-8")
        self.assertIn("return a + b", current_content)
        self.assertNotIn("raise RuntimeError", current_content)

    def test_passing_tests_trigger_atomic_commit(self):
        valid_diff = (
            "--- a/src/sample.py\n"
            "+++ b/src/sample.py\n"
            "@@ -1,2 +1,3 @@\n"
            " def calculate_total(a, b):\n"
            "+    # verified safe addition\n"
            "     return a + b\n"
        )
        prop = self.make_proposal(valid_diff)
        auth = self.make_authorization(prop)

        def test_runner(sandbox_root: Path) -> tuple[bool, str]:
            return True, "100% test coverage passed."

        result = apply_patch_transactional(
            self.root, prop, auth, custom_verifier=test_runner
        )

        self.assertEqual(result.status, APPLIED)
        self.assertTrue(result.applied)
        self.assertIn("src/sample.py", result.modified_files)

        # Verify root project contains the applied change atomically
        current_content = self.target_file.read_text(encoding="utf-8")
        self.assertIn("# verified safe addition", current_content)


if __name__ == "__main__":
    unittest.main()
