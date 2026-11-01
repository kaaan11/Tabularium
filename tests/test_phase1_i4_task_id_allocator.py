"""Phase I (I4) Task ID allocator tests.

Human-approved policy (Decision B): scan canonical Task ids, allocate
max+1 within the year sequence, refuse unreadable storage and 9999
exhaustion, surface collisions, never overwrite, stay deterministic.
The allocator generalizes the existing `_next_id` pattern exactly —
including the iterdir-not-glob rule that makes a listing failure a
refusal instead of a silent reallocation.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tabularium.init import initialize
from tabularium.task import (
    RECORD_DIRECTORY_UNREADABLE,
    TASK_ID_SEQUENCE_EXHAUSTED,
    allocate_task_id,
)


def current_year() -> int:
    return datetime.now(timezone.utc).year


class TaskIdAllocatorTests(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-i4-")
        self.root = Path(self.holder.name)
        initialize(self.root)
        self.task_dir = self.root / ".ai" / "tasks"
        self.task_dir.mkdir(exist_ok=True)

    def tearDown(self):
        self.holder.cleanup()

    def touch(self, name):
        (self.task_dir / name).write_text("---\nid: x\n---\n",
                                          encoding="utf-8")

    def alloc(self, year=None):
        return allocate_task_id(self.root, year=year)

    def test_empty_store_starts_at_0001(self):
        allocation = self.alloc(year=2026)
        self.assertFalse(allocation.refused)
        self.assertEqual(allocation.record_id, "TASK-2026-0001")
        self.assertFalse(allocation.collision)

    def test_one_task_allocates_max_plus_one(self):
        self.touch("TASK-2026-0001.md")
        self.assertEqual(self.alloc(2026).record_id, "TASK-2026-0002")

    def test_gaps_are_not_filled(self):
        self.touch("TASK-2026-0003.md")
        self.assertEqual(self.alloc(2026).record_id, "TASK-2026-0004")

    def test_highest_canonical_id_wins_regardless_of_listing_order(self):
        for n in (7, 2, 41, 100):
            self.touch(f"TASK-2026-{n:04d}.md")
        self.assertEqual(self.alloc(2026).record_id, "TASK-2026-0101")

    def test_malformed_neighbors_never_count_as_ids(self):
        # Record policy: only canonical filenames are ids; anything else is
        # quarantine noise and must not reserve or advance the sequence.
        self.touch("TASK-26-9.md")
        self.touch("task-2026-0055.md")
        self.touch("TASK-2026-ABCD.md")
        self.assertEqual(self.alloc(2026).record_id, "TASK-2026-0001")

    def test_year_sequences_are_independent(self):
        # A fully exhausted 2025 says nothing about 2026.
        self.touch("TASK-2025-9999.md")
        self.assertEqual(self.alloc(2025).code, TASK_ID_SEQUENCE_EXHAUSTED)
        allocation = self.alloc(2026)
        self.assertFalse(allocation.refused)
        self.assertEqual(allocation.record_id, "TASK-2026-0001")
        self.touch("TASK-2026-0009.md")
        self.assertEqual(self.alloc(2026).record_id, "TASK-2026-0010")

    def test_unreadable_storage_refuses(self):
        self.touch("TASK-2026-0010.md")
        self.task_dir.chmod(0o000)
        try:
            allocation = self.alloc(2026)
        finally:
            self.task_dir.chmod(0o755)
        self.assertTrue(allocation.refused)
        self.assertIsNone(allocation.record_id)
        self.assertEqual(allocation.code, RECORD_DIRECTORY_UNREADABLE)

    def test_exhaustion_refuses_at_9999(self):
        self.touch("TASK-2026-9999.md")
        allocation = self.alloc(2026)
        self.assertTrue(allocation.refused)
        self.assertEqual(allocation.code, TASK_ID_SEQUENCE_EXHAUSTED)
        self.assertIsNone(allocation.record_id)

    def test_exhaustion_boundary_allows_9998_then_refuses_at_9999(self):
        self.touch("TASK-2026-9998.md")
        self.assertEqual(self.alloc(2026).record_id, "TASK-2026-9999")
        (self.task_dir / "TASK-2026-9999.md").write_text("---\n---\n")
        self.assertTrue(self.alloc(2026).refused)

    def test_collision_fact_is_surfaced_when_the_target_preexists(self):
        # A deterministic single-process scan cannot produce max+1 already
        # present (the presence would raise the max), so the collision fact
        # guards the scan-to-write race window. Simulate exactly that:
        # another creator lands the candidate between scan and check.
        real_exists = Path.exists

        def raced(path):
            if path.name == "TASK-2026-0001.md":
                return True
            return real_exists(path)

        with mock.patch.object(Path, "exists", raced):
            allocation = self.alloc(2026)
        self.assertFalse(allocation.refused)
        self.assertEqual(allocation.record_id, "TASK-2026-0001")
        self.assertTrue(allocation.collision)

    def test_missing_tasks_directory_allocates_from_0001(self):
        shutil.rmtree(self.task_dir)
        allocation = self.alloc(2026)
        self.assertFalse(allocation.refused)
        self.assertEqual(allocation.record_id, "TASK-2026-0001")

    def test_deterministic_across_repeated_scans(self):
        self.touch("TASK-2026-0042.md")
        first = self.alloc(2026)
        for _ in range(5):
            self.assertEqual(self.alloc(2026), first)

    def test_default_year_is_current_utc_year(self):
        self.assertEqual(self.alloc().record_id,
                         f"TASK-{current_year():04d}-0001")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
