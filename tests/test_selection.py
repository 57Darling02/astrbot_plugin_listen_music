from __future__ import annotations

from pathlib import Path
import sys
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.models import BilibiliCandidate
from core.selection import SearchSnapshotStore


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def candidates() -> tuple[BilibiliCandidate, ...]:
    return (
        BilibiliCandidate(
            "BV1fixture", 101, "第一首", "上传者 A", 180_000, page_title="第一首"
        ),
        BilibiliCandidate(
            "BV1fixture", 102, "第二首", "上传者 B", 200_000, page_title="第二首"
        ),
    )


class SearchSnapshotStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.tokens = iter(("search-1", "search-2", "search-3"))
        self.store = SearchSnapshotStore(
            ttl_seconds=5,
            clock=self.clock,
            token_factory=lambda: next(self.tokens),
        )

    def test_selection_is_bound_to_its_originating_session(self) -> None:
        snapshot = self.store.create(
            session_id="chat-a",
            query="第一首",
            candidates=candidates(),
        )

        owned = self.store.get(search_id=snapshot.search_id, session_id="chat-a")
        self.assertIsNotNone(owned)
        assert owned is not None
        self.assertEqual(owned.candidate("BV1fixture:101"), candidates()[0])
        self.assertIsNone(
            self.store.get(
                search_id=snapshot.search_id,
                session_id="chat-b",
            )
        )

    def test_unknown_candidate_id_is_rejected(self) -> None:
        snapshot = self.store.create(
            session_id="chat-a",
            query="第一首",
            candidates=candidates(),
        )

        self.assertIsNone(snapshot.candidate("hallucinated-id"))

    def test_snapshot_contains_only_search_data(self) -> None:
        snapshot = self.store.create(
            session_id="chat-a",
            query="第一首",
            candidates=candidates(),
        )

        self.assertEqual(snapshot.query, "第一首")
        self.assertEqual(snapshot.candidates, candidates())
        self.assertEqual(
            set(snapshot.__dataclass_fields__),
            {
                "search_id",
                "session_id",
                "query",
                "candidates",
                "created_at",
                "expires_at",
            },
        )

    def test_one_based_text_position_preserves_the_selected_bilibili_page(self) -> None:
        snapshot = self.store.create(
            session_id="chat-a",
            query="第一首",
            candidates=candidates(),
        )

        selected = snapshot.candidate_at(2)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.bvid, "BV1fixture")
        self.assertEqual(selected.cid, 102)
        self.assertEqual(selected.candidate_id, "BV1fixture:102")
        self.assertIsNone(snapshot.candidate_at(3))

    def test_expired_snapshot_is_not_available_and_is_reclaimed(self) -> None:
        snapshot = self.store.create(
            session_id="chat-a",
            query="第一首",
            candidates=candidates(),
        )
        self.clock.now = 105.0

        self.assertIsNone(
            self.store.get(search_id=snapshot.search_id, session_id="chat-a")
        )
        self.assertEqual(self.store.purge_expired(), 0)

    def test_capacity_evicts_the_oldest_entry(self) -> None:
        store = SearchSnapshotStore(
            ttl_seconds=60,
            max_entries=1,
            clock=self.clock,
            token_factory=lambda: next(self.tokens),
        )
        first = store.create(session_id="chat-a", query="one", candidates=candidates())
        self.clock.now += 1
        second = store.create(session_id="chat-a", query="two", candidates=candidates())

        self.assertIsNone(store.get(search_id=first.search_id, session_id="chat-a"))
        self.assertIsNotNone(store.get(search_id=second.search_id, session_id="chat-a"))


if __name__ == "__main__":
    unittest.main()
