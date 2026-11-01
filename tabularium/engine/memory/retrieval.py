"""Resolution helpers for the non-authoritative memory boundary."""

from __future__ import annotations

from typing import Callable, Iterable, Protocol

from .model import (CANDIDATE_ORPHAN, CANDIDATE_RESOLVED, CANDIDATE_STALE,
                    MemoryCandidate, SourceSnapshot)


class SourceResolver(Protocol):
    def resolve(self, source_kind: str, source_id: str) -> SourceSnapshot | None:
        """Return the current authoritative source snapshot, if it exists."""
        ...


ResolverLike = SourceResolver | Callable[[str, str], SourceSnapshot | None]


def _resolve(resolver: ResolverLike, candidate: MemoryCandidate) -> SourceSnapshot | None:
    if hasattr(resolver, "resolve"):
        return resolver.resolve(candidate.source_kind, candidate.source_id)  # type: ignore[union-attr]
    return resolver(candidate.source_kind, candidate.source_id)  # type: ignore[operator]


def resolve_candidate(candidate: MemoryCandidate, resolver: ResolverLike) -> MemoryCandidate:
    """Re-check source presence and digest before memory can influence context."""
    snapshot = _resolve(resolver, candidate)
    if snapshot is None or not snapshot.exists:
        resolution = CANDIDATE_ORPHAN
    elif snapshot.digest != candidate.source_digest:
        resolution = CANDIDATE_STALE
    else:
        resolution = CANDIDATE_RESOLVED
    return MemoryCandidate(**{**candidate.__dict__, "resolution": resolution})


def resolved_candidates(candidates: Iterable[MemoryCandidate],
                        resolver: ResolverLike) -> tuple[MemoryCandidate, ...]:
    """Return only candidates whose authoritative source still resolves.

    The returned objects remain candidates. This helper returns pointers for an
    authoritative caller to load; it does not inject text into a worker or
    write any project record.
    """
    return tuple(resolved for candidate in candidates
                 if (resolved := resolve_candidate(candidate, resolver)).resolution
                 == CANDIDATE_RESOLVED)


def authoritative_sources(candidates: Iterable[MemoryCandidate],
                          resolver: ResolverLike) -> tuple[tuple[str, str], ...]:
    """Return resolved `(source_kind, source_id)` pointers in stable order."""
    return tuple((candidate.source_kind, candidate.source_id)
                 for candidate in resolved_candidates(candidates, resolver))
