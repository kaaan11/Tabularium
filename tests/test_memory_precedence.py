"""Unit tests for memory precedence and conflict resolution (DRL-024, DRL-025)."""

import unittest

from tabularium.engine.memory import (
    CANDIDATE_ORPHAN,
    CANDIDATE_RESOLVED,
    CANDIDATE_STALE,
    CANDIDATE_UNRESOLVED,
    CONFLICT_DETECTED,
    MemoryCandidate,
    evaluate_memory_precedence,
)


def candidate(
    memory_id: str,
    source_id: str = "SRC-1",
    source_kind: str = "NOTE",
    digest: str = "hash1",
    text: str = "sample text",
    score: int = 10,
    rank: int = 1,
    resolution: str = CANDIDATE_RESOLVED,
) -> MemoryCandidate:
    return MemoryCandidate(
        memory_id=memory_id,
        project_id="test-proj",
        source_kind=source_kind,
        source_id=source_id,
        source_digest=digest,
        text=text,
        score=score,
        rank=rank,
        resolution=resolution,
    )


class TestMemoryPrecedence(unittest.TestCase):
    def test_unresolved_stale_orphan_are_rejected(self):
        c1 = candidate("m-1", resolution=CANDIDATE_RESOLVED)
        c2 = candidate("m-2", resolution=CANDIDATE_STALE)
        c3 = candidate("m-3", resolution=CANDIDATE_ORPHAN)
        c4 = candidate("m-4", resolution=CANDIDATE_UNRESOLVED)

        verdict = evaluate_memory_precedence([c1, c2, c3, c4])
        self.assertEqual(len(verdict.accepted_candidates), 1)
        self.assertEqual(verdict.accepted_candidates[0].memory_id, "m-1")
        self.assertEqual(len(verdict.rejected_candidates), 3)

    def test_authoritative_digest_mismatch_rejected(self):
        c1 = candidate("m-1", source_id="SRC-1", source_kind="NOTE", digest="digest-current")
        c2 = candidate("m-2", source_id="SRC-2", source_kind="NOTE", digest="digest-old")

        # Keys are now (source_kind, source_id) tuples
        auth = {
            ("NOTE", "SRC-1"): "digest-current",
            ("NOTE", "SRC-2"): "digest-new",
        }
        verdict = evaluate_memory_precedence([c1, c2], authoritative_source_digests=auth)
        self.assertEqual(len(verdict.accepted_candidates), 1)
        self.assertEqual(verdict.accepted_candidates[0].memory_id, "m-1")
        self.assertEqual(len(verdict.rejected_candidates), 1)
        self.assertEqual(verdict.rejected_candidates[0].memory_id, "m-2")

    def test_conflicting_candidates_are_all_rejected(self):
        """BUG-07: conflicting candidates must ALL be rejected, none accepted."""
        c1 = candidate("m-1", source_id="SRC-1", text="approach A", score=5, rank=2)
        c2 = candidate("m-2", source_id="SRC-1", text="approach B", score=10, rank=1)

        verdict = evaluate_memory_precedence([c1, c2])
        self.assertIn(CONFLICT_DETECTED, verdict.reason_codes)
        self.assertEqual(len(verdict.conflicts), 1)
        # No winner — all conflicting candidates rejected
        self.assertEqual(len(verdict.accepted_candidates), 0)
        self.assertEqual(len(verdict.rejected_candidates), 2)

    def test_same_source_id_different_kind_is_not_cross_judged(self):
        """BUG-07: a DECISION with source_id=X must not be judged against a TASK with source_id=X."""
        c1 = candidate("m-1", source_id="SRC-1", source_kind="DECISION",
                        digest="d1", text="decided X")
        c2 = candidate("m-2", source_id="SRC-1", source_kind="TASK",
                        digest="d2", text="task X")

        auth = {("DECISION", "SRC-1"): "d1"}
        verdict = evaluate_memory_precedence([c1, c2], authoritative_source_digests=auth)
        # Both accepted — they are different source_kinds with the same source_id
        self.assertEqual(len(verdict.accepted_candidates), 2)
        self.assertEqual(len(verdict.conflicts), 0)

    def test_duplicate_candidates_keep_one(self):
        """Non-conflicting duplicates (same digest and text) keep exactly one."""
        c1 = candidate("m-1", source_id="SRC-1", digest="same", text="same text", score=5)
        c2 = candidate("m-2", source_id="SRC-1", digest="same", text="same text", score=10)

        verdict = evaluate_memory_precedence([c1, c2])
        self.assertEqual(len(verdict.accepted_candidates), 1)
        self.assertEqual(verdict.accepted_candidates[0].memory_id, "m-2")
        self.assertEqual(len(verdict.rejected_candidates), 1)
        self.assertNotIn(CONFLICT_DETECTED, verdict.reason_codes)


if __name__ == "__main__":
    unittest.main()
