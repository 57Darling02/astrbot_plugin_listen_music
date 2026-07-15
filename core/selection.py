"""Short-lived, session-bound search result storage."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import secrets
import time

from .models import BilibiliCandidate, SearchSnapshot


class SearchSnapshotStore:
    """Keep opaque search IDs valid only for their originating chat session.

    All methods are synchronous and do not yield, so a single asyncio event
    loop can use the store without an async lock.  A host that invokes plugin
    code from multiple threads should keep one store per event loop.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 300.0,
        max_entries: int = 1024,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries < 1:
            raise ValueError("max_entries must be positive")

        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._token_factory = token_factory or _new_search_id
        self._snapshots: dict[str, SearchSnapshot] = {}

    def create(
        self,
        *,
        session_id: str,
        query: str,
        candidates: Sequence[BilibiliCandidate],
    ) -> SearchSnapshot:
        """Store candidates and return an opaque ID that cannot cross sessions."""

        now = self._clock()
        self.purge_expired(now=now)
        self._evict_to_capacity()

        search_id = self._allocate_search_id()
        snapshot = SearchSnapshot(
            search_id=search_id,
            session_id=session_id,
            query=query,
            candidates=tuple(candidates),
            created_at=now,
            expires_at=now + self._ttl_seconds,
        )
        self._snapshots[search_id] = snapshot
        return snapshot

    def get(self, *, search_id: str, session_id: str) -> SearchSnapshot | None:
        """Return a non-expired snapshot only to its originating session."""

        snapshot = self._lookup(search_id, now=self._clock())
        if snapshot is None or snapshot.session_id != session_id.strip():
            return None
        return snapshot

    def purge_expired(self, *, now: float | None = None) -> int:
        """Remove expired entries and return how many were reclaimed."""

        current_time = self._clock() if now is None else now
        expired = [
            search_id
            for search_id, snapshot in self._snapshots.items()
            if snapshot.expires_at <= current_time
        ]
        for search_id in expired:
            del self._snapshots[search_id]
        return len(expired)

    def _lookup(self, search_id: str, *, now: float) -> SearchSnapshot | None:
        normalised_id = search_id.strip()
        snapshot = self._snapshots.get(normalised_id)
        if snapshot is None:
            return None
        if snapshot.expires_at <= now:
            del self._snapshots[normalised_id]
            return None
        return snapshot

    def _evict_to_capacity(self) -> None:
        overflow = len(self._snapshots) - self._max_entries + 1
        if overflow <= 0:
            return

        oldest = sorted(
            self._snapshots.values(),
            key=lambda snapshot: (snapshot.expires_at, snapshot.created_at),
        )[:overflow]
        for snapshot in oldest:
            del self._snapshots[snapshot.search_id]

    def _allocate_search_id(self) -> str:
        for _ in range(8):
            search_id = self._token_factory().strip()
            if search_id and search_id not in self._snapshots:
                return search_id
        raise RuntimeError("could not allocate a unique search ID")


def _new_search_id() -> str:
    return secrets.token_urlsafe(12)
