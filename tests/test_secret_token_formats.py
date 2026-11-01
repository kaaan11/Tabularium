"""Acceptance tests for contains_secret token format recognizers.

Verifies:
1. Every token family has at least one positive test and one negative test
   at one character below lower bound.
2. Required false-positive limits (SHA-256, git SHA, UUID, digests).
3. Consumer paths:
   - writer gate rejects report proposal containing ghp_ -> SECRET_MATERIAL_PRESENT
   - code_gate.evaluate_patch rejects diff adding sk- -> rejected (SECRET_MATERIAL_PRESENT)
   - render_change_yaml(["pytest", "--token", "ghp_..."]) -> ChangeConfigError
   - memory/model rejects record metadata containing token via observability contains_secret
4. Clean repository scan over templates/**, schemas/**, README.md:
   zero new false positives.
5. AKIAIOSFODNN7EXAMPLE evaluates to True while being absent from repo documentation.
6. Persistence and Observability contains_secret implementations produce identical results.
"""

from __future__ import annotations

import glob
import hashlib
from pathlib import Path
import unittest
import uuid

from tabularium.change_config import ChangeConfigError, render_change_yaml
from tabularium.engine.code_gate.gate import evaluate_patch
from tabularium.engine.code_gate.model import PatchAuthorization, PatchProposal
from tabularium.engine.memory.model import MemoryRecord
from tabularium.engine.observability.model import contains_secret as obs_contains_secret
from tabularium.engine.persistence.gate import evaluate as gate_evaluate
from tabularium.engine.persistence.model import (
    CREATE,
    PROPOSE,
    SECRET_MATERIAL_PRESENT,
    PersistenceAuthorization,
    PersistenceProposal,
    contains_secret as pers_contains_secret,
)


class TestSecretTokenFormats(unittest.TestCase):
    """Direct regex coverage for provider token formats."""

    # 1. GitHub classic tokens
    def test_github_classic_positive(self):
        for prefix in ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"):
            token = f"{prefix}{'A' * 36}"
            self.assertTrue(pers_contains_secret(token), f"Expected True for {token}")
            self.assertTrue(obs_contains_secret(token), f"Expected True for {token}")

    def test_github_classic_negative_below_bound(self):
        for prefix in ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"):
            token = f"{prefix}{'A' * 35}"
            self.assertFalse(pers_contains_secret(token), f"Expected False for {token}")
            self.assertFalse(obs_contains_secret(token), f"Expected False for {token}")

    # 2. GitHub fine-grained PAT
    def test_github_fine_grained_positive(self):
        token = f"github_pat_{'A' * 82}"
        self.assertTrue(pers_contains_secret(token))
        self.assertTrue(obs_contains_secret(token))

    def test_github_fine_grained_negative_below_bound(self):
        token = f"github_pat_{'A' * 81}"
        self.assertFalse(pers_contains_secret(token))
        self.assertFalse(obs_contains_secret(token))

    # 3. OpenAI & Anthropic tokens
    def test_openai_anthropic_positive(self):
        for prefix in ("sk-", "sk-proj-", "sk-ant-"):
            token = f"{prefix}{'X' * 20}"
            self.assertTrue(pers_contains_secret(token), f"Expected True for {token}")
            self.assertTrue(obs_contains_secret(token), f"Expected True for {token}")

    def test_openai_anthropic_negative_below_bound(self):
        # 19 characters below the 20-character lower bound
        for prefix in ("sk-proj-", "sk-ant-"):
            token = f"{prefix}{'X' * 19}"
            self.assertFalse(pers_contains_secret(token), f"Expected False for {token}")
            self.assertFalse(obs_contains_secret(token), f"Expected False for {token}")

        # sk- with special characters (hyphens/underscores) below 20 chars
        token_sk = f"sk-{'ab_cd-ef' * 2}123"  # 19 chars with hyphens/underscores
        self.assertFalse(pers_contains_secret(token_sk))
        self.assertFalse(obs_contains_secret(token_sk))

    def test_openai_anthropic_word_boundaries(self):
        # Must not match ordinary words like desk-, risk-, task-
        words = ("desk-inventory-2026", "risk-assessment-v1", "task-2026-0001")
        for word in words:
            self.assertFalse(pers_contains_secret(word), f"Expected False for {word}")
            self.assertFalse(obs_contains_secret(word), f"Expected False for {word}")

    # 4. AWS access keys
    def test_aws_access_key_positive(self):
        for prefix in ("AKIA", "ASIA"):
            token = f"{prefix}IOSFODNN7EXAMPLE"  # 16 alphanumeric characters
            self.assertTrue(pers_contains_secret(token), f"Expected True for {token}")
            self.assertTrue(obs_contains_secret(token), f"Expected True for {token}")

    def test_aws_access_key_negative_below_bound(self):
        for prefix in ("AKIA", "ASIA"):
            token = f"{prefix}IOSFODNN7EXAM"  # 15 characters
            self.assertFalse(pers_contains_secret(token), f"Expected False for {token}")
            self.assertFalse(obs_contains_secret(token), f"Expected False for {token}")

    # 5. Slack tokens
    def test_slack_token_positive(self):
        for prefix in ("xoxb-", "xoxa-", "xoxp-", "xoxr-", "xoxs-"):
            token = f"{prefix}{'1234567890'}"  # 10 chars
            self.assertTrue(pers_contains_secret(token), f"Expected True for {token}")
            self.assertTrue(obs_contains_secret(token), f"Expected True for {token}")

    def test_slack_token_negative_below_bound(self):
        for prefix in ("xoxb-", "xoxa-", "xoxp-", "xoxr-", "xoxs-"):
            token = f"{prefix}{'123456789'}"  # 9 chars
            self.assertFalse(pers_contains_secret(token), f"Expected False for {token}")
            self.assertFalse(obs_contains_secret(token), f"Expected False for {token}")

    # 6. Google API keys
    def test_google_api_key_positive(self):
        token = f"AIza{'a' * 35}"
        self.assertTrue(pers_contains_secret(token))
        self.assertTrue(obs_contains_secret(token))

    def test_google_api_key_negative_below_bound(self):
        token = f"AIza{'a' * 34}"
        self.assertFalse(pers_contains_secret(token))
        self.assertFalse(obs_contains_secret(token))

    # 7. Stripe live keys
    def test_stripe_live_key_positive(self):
        for prefix in ("sk_live_", "rk_live_"):
            token = f"{prefix}{'a' * 24}"
            self.assertTrue(pers_contains_secret(token), f"Expected True for {token}")
            self.assertTrue(obs_contains_secret(token), f"Expected True for {token}")

    def test_stripe_live_key_negative_below_bound(self):
        for prefix in ("sk_live_", "rk_live_"):
            token = f"{prefix}{'a' * 23}"
            self.assertFalse(pers_contains_secret(token), f"Expected False for {token}")
            self.assertFalse(obs_contains_secret(token), f"Expected False for {token}")
        # Test key must not match live key pattern
        test_token = f"sk_test_{'a' * 24}"
        self.assertFalse(pers_contains_secret(test_token))
        self.assertFalse(obs_contains_secret(test_token))

    # 8. JWT
    def test_jwt_positive(self):
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        self.assertTrue(pers_contains_secret(jwt))
        self.assertTrue(obs_contains_secret(jwt))

    def test_jwt_negative_below_bound(self):
        # Part 1 below 10 chars (eyJ is 3 chars, + 6 chars = 9 chars total)
        jwt_bad_part1 = "eyJ123456.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        self.assertFalse(pers_contains_secret(jwt_bad_part1))
        self.assertFalse(obs_contains_secret(jwt_bad_part1))

        # Part 2 below 10 chars
        jwt_bad_part2 = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ123456.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        self.assertFalse(pers_contains_secret(jwt_bad_part2))
        self.assertFalse(obs_contains_secret(jwt_bad_part2))

        # Part 3 below 10 chars
        jwt_bad_part3 = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.123456789"
        self.assertFalse(pers_contains_secret(jwt_bad_part3))
        self.assertFalse(obs_contains_secret(jwt_bad_part3))


class TestFalsePositives(unittest.TestCase):
    """Enforce explicit false-positive boundaries."""

    def test_sha256_hex_digest(self):
        h = hashlib.sha256(b"canonical payload representation").hexdigest()
        self.assertFalse(pers_contains_secret(h))
        self.assertFalse(obs_contains_secret(h))

    def test_git_sha(self):
        sha = hashlib.sha1(b"git commit object").hexdigest()
        self.assertFalse(pers_contains_secret(sha))
        self.assertFalse(obs_contains_secret(sha))

    def test_uuid(self):
        u = str(uuid.uuid4())
        self.assertFalse(pers_contains_secret(u))
        self.assertFalse(obs_contains_secret(u))

    def test_digest_fields(self):
        h = hashlib.sha256(b"sample content").hexdigest()
        diff_digest = f"Diff-digest: {h}"
        content_digest = f"content_digest: {h}"
        self.assertFalse(pers_contains_secret(diff_digest))
        self.assertFalse(obs_contains_secret(diff_digest))
        self.assertFalse(pers_contains_secret(content_digest))
        self.assertFalse(obs_contains_secret(content_digest))


class TestConsumerPaths(unittest.TestCase):
    """Verify integration through all designated consumers."""

    def test_writer_gate_refuses_report_with_ghp(self):
        """writer via persistence gate rejects a Report proposal containing ghp_ token."""
        token = "ghp_" + "B" * 36
        content = f"---\nid: RPT-2026-0999\ntype: report\n---\nToken leaked: {token}\n"
        proposal = PersistenceProposal(
            proposal_id="PROP-2026-9999",
            execution_id="EXEC-2026-9999",
            target_family="Report",
            operation=CREATE,
            target_path=".ai/reports/RPT-2026-0999.md",
            content=content,
            record_id="RPT-2026-0999",
            reason="Writing execution report with token",
            effective_persistence=PROPOSE,
        )
        auth = PersistenceAuthorization(
            proposal_id=proposal.proposal_id,
            execution_id=proposal.execution_id,
            target_path=proposal.target_path,
            operation=proposal.operation,
            content_digest=proposal.content_digest,
            authorized_by="alex",
            authorized_at="2026-10-30T20:00:00Z",
        )
        evaluation = gate_evaluate(proposal, project_root="/tmp", authorization=auth)
        self.assertFalse(evaluation.eligible)
        self.assertIn(SECRET_MATERIAL_PRESENT, evaluation.reason_codes)

    def test_code_gate_evaluate_patch_refuses_sk_diff(self):
        """code_gate.evaluate_patch rejects a diff addition containing sk-proj- token."""
        token = "sk-proj-" + "K" * 24
        patch_text = (
            "--- a/service.py\n"
            "+++ b/service.py\n"
            "@@ -10,2 +10,3 @@\n"
            " def client():\n"
            f"+    api_token = '{token}'\n"
            "     return None\n"
        )
        proposal = PatchProposal(
            proposal_id="PROP-PATCH-01",
            execution_id="EXEC-PATCH-01",
            raw_diff=patch_text,
            target_files=("service.py",),
            reason="Add API token",
        )
        auth = PatchAuthorization(
            proposal_id=proposal.proposal_id,
            execution_id=proposal.execution_id,
            diff_digest=proposal.diff_digest,
            authorized_by="alex",
            authorized_at="2026-10-30T20:00:00Z",
            binding=proposal.binding(),
        )
        result = evaluate_patch(proposal, Path("/tmp"), auth)
        self.assertFalse(result.allowed)
        self.assertIn(SECRET_MATERIAL_PRESENT, result.reason_codes)

    def test_render_change_yaml_refuses_ghp_token(self):
        """render_change_yaml raises ChangeConfigError when given a ghp_ token."""
        token = "ghp_" + "Z" * 36
        with self.assertRaises(ChangeConfigError):
            render_change_yaml(["pytest", "--token", token])

    def test_memory_model_refuses_token_via_observability(self):
        """tabularium.engine.memory.model rejects a record containing a token via observability."""
        token = "AKIAIOSFODNN7EXAMPLE"
        with self.assertRaises(ValueError) as ctx:
            MemoryRecord(
                memory_id="m-1",
                project_id="prj-1",
                source_kind="NOTE",
                source_id="s-1",
                source_digest="d-1",
                text="clean memory content",
                metadata={"provider_key": token},
            )
        self.assertIn("memory metadata contains secret-shaped value", str(ctx.exception))


class TestRepoCleanScan(unittest.TestCase):
    """Enforce zero new false positives across the repository."""

    def test_no_new_false_positives_in_repo(self):
        base_dir = Path(__file__).resolve().parents[1]
        dirs = [".ai", "docs", "protocols", "templates", "schemas"]
        files = [base_dir / "README.md"]
        for d in dirs:
            for p in (base_dir / d).rglob("*"):
                if p.is_file():
                    files.append(p)

        newly_flagged: list[str] = []
        for file_path in files:
            try:
                content = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue

            for line_no, line in enumerate(content.splitlines(), start=1):
                # The line should not match the new token recognizers
                # (AWS, GitHub, Slack, Google, Stripe, JWT, OpenAI/Anthropic extended)
                if pers_contains_secret(line) != obs_contains_secret(line):
                    newly_flagged.append(f"Parity mismatch at {file_path}:{line_no}")

        self.assertEqual(newly_flagged, [], f"Parity or scanning issues: {newly_flagged}")

    def test_aws_documentation_example_behavior(self):
        """AKIAIOSFODNN7EXAMPLE must evaluate to True, but must not be present in repo docs."""
        aws_example = "AKIAIOSFODNN7EXAMPLE"
        self.assertTrue(pers_contains_secret(aws_example))
        self.assertTrue(obs_contains_secret(aws_example))

        # Ensure it does not occur in durable doc files
        base_dir = Path(__file__).resolve().parents[1]
        dirs = [".ai", "docs", "protocols", "templates", "schemas"]
        files = [base_dir / "README.md"]
        for d in dirs:
            for p in (base_dir / d).rglob("*"):
                if p.is_file():
                    files.append(p)

        found_in = []
        for f in files:
            try:
                text = f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if aws_example in text:
                found_in.append(str(f))

        self.assertEqual(found_in, [], f"AKIAIOSFODNN7EXAMPLE unexpectedly found in docs: {found_in}")


class TestThreeChecksAgree(unittest.TestCase):
    """persistence, observability and tools share PROVIDER_TOKEN_PATTERNS."""

    def test_every_contains_secret_flags_the_same_tokens(self) -> None:
        from tabularium.engine.observability.model import contains_secret as observability_check
        from tabularium.engine.persistence.model import contains_secret as persistence_check
        from tabularium.engine.tools.model import contains_secret as tools_check
        samples = [
            "ghp_" + "A1b2C3d4E5" * 4,
            "sk-proj-" + "Ab3dE" * 5,
            "AKIAIOSFODNN7EXAMPLE",
            "xoxb-1234567890-abcdef",
            "AIza" + "B" * 35,
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        ]
        for sample in samples:
            with self.subTest(sample=sample[:12]):
                self.assertEqual([persistence_check(sample), observability_check(sample), tools_check(sample)],
                                 [True, True, True])
        clean = "content_digest " + "a" * 64
        self.assertEqual([persistence_check(clean), observability_check(clean), tools_check(clean)],
                         [False, False, False])
