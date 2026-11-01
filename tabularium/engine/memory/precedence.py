"""Memory conflict resolution and semantic precedence policy (DRL-025, DRL-024).

Constitutional Guarantees:
1. "Context must not be treated as stronger than actual evidence" (Principle 9).
2. "AI opinions do not automatically become decisions" (Principle 7).
3. Memory candidates are non-authoritative pointers. When two memory candidates
   point to the same source and conflict:
   - STALE or ORPHAN candidates are filtered out before context assembly.
   - Unresolved semantic conflicts are flagged as CONFLICT_DETECTED and ALL
     conflicting candidates are rejected without guessing a winner.
   - Authoritative project truth precedence (DRL-024) is NOT YET IMPLEMENTED;
     only digest-based stale/orphan filtering is performed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .model import (
    CANDIDATE_RESOLVED,
    MemoryCandidate,
)


CONFLICT_DETECTED = "CONFLICT_DETECTED"


@dataclass(frozen=True)
class MemoryPrecedenceVerdict:
    """Outcome of evaluating candidate memory records against semantic precedence."""

    accepted_candidates: tuple[MemoryCandidate, ...]
    rejected_candidates: tuple[MemoryCandidate, ...]
    conflicts: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()


def evaluate_memory_precedence(
    candidates: Sequence[MemoryCandidate],
    *,
    authoritative_source_digests: dict[tuple[str, str], str] | None = None,
) -> MemoryPrecedenceVerdict:
    """Filter and sort candidates according to strict semantic precedence rules.

    Rules:
    1. Discard any candidate with resolution != CANDIDATE_RESOLVED.
    2. If authoritative_source_digests is provided (keyed by (source_kind, source_id)),
       discard candidates whose source_digest does not match.
    3. Group candidates by (source_kind, source_id). If multiple records exist
       and their digests or texts differ, ALL candidates in the group are rejected
       with CONFLICT_DETECTED — the module does not guess a winner.
    4. Deterministic sorting: by score descending, then rank ascending, then memory_id ascending.
    """
    auth_digests = authoritative_source_digests or {}
    accepted: list[MemoryCandidate] = []
    rejected: list[MemoryCandidate] = []
    conflicts: list[str] = []
    reasons: list[str] = []

    # Filter out unresolved / stale / orphan
    valid_candidates: list[MemoryCandidate] = []
    for c in candidates:
        if c.resolution != CANDIDATE_RESOLVED:
            rejected.append(c)
            continue
        key = (c.source_kind, c.source_id)
        if key in auth_digests:
            expected_digest = auth_digests[key]
            if c.source_digest != expected_digest:
                rejected.append(c)
                continue
        valid_candidates.append(c)

    # Group by (source_kind, source_id) to detect conflicts
    by_source: dict[tuple[str, str], list[MemoryCandidate]] = {}
    for c in valid_candidates:
        key = (c.source_kind, c.source_id)
        by_source.setdefault(key, []).append(c)

    for (source_kind, source_id), group in by_source.items():
        if len(group) == 1:
            accepted.append(group[0])
            continue

        # Multiple candidates for the same source
        digests = {c.source_digest for c in group}
        texts = {c.text for c in group}

        if len(digests) > 1 or len(texts) > 1:
            # Conflict: reject ALL candidates in the group
            conflicts.append(f"Conflict in memory candidates for {source_kind}:{source_id}")
            if CONFLICT_DETECTED not in reasons:
                reasons.append(CONFLICT_DETECTED)
            for c in group:
                rejected.append(c)
        else:
            # No conflict (duplicates): keep first deterministically, reject rest
            sorted_group = sorted(
                group,
                key=lambda c: (-c.score, c.rank, c.memory_id),
            )
            accepted.append(sorted_group[0])
            for dup in sorted_group[1:]:
                rejected.append(dup)

    # Sort final accepted deterministically
    sorted_accepted = tuple(sorted(
        accepted,
        key=lambda c: (-c.score, c.rank, c.memory_id),
    ))

    return MemoryPrecedenceVerdict(
        accepted_candidates=sorted_accepted,
        rejected_candidates=tuple(rejected),
        conflicts=tuple(conflicts),
        reason_codes=tuple(reasons),
    )
