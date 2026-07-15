from __future__ import annotations

from pathlib import Path
import sys
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.matcher import filter_bilibili_candidates, prepare_bilibili_search_query
from core.models import BilibiliCandidate


def candidate(
    *,
    bvid: str = "BV1test",
    cid: int = 1,
    title: str = "周杰伦 - 晴天",
    page_title: str = "",
    uploader: str = "音乐频道",
    duration_ms: int = 269_000,
    search_title: str = "",
) -> BilibiliCandidate:
    return BilibiliCandidate(
        bvid=bvid,
        cid=cid,
        title=title,
        page_title=page_title,
        uploader=uploader,
        duration_ms=duration_ms,
        search_title=search_title,
    )


class BilibiliCandidateTests(unittest.TestCase):
    def test_candidate_has_a_stable_playable_identity_and_display_title(self) -> None:
        result = candidate(
            bvid="BV1abc",
            cid=42,
            title="周杰伦作品合集",
            page_title="P03 晴天",
        )

        self.assertEqual(result.candidate_id, "BV1abc:42")
        self.assertEqual(result.display_title, "周杰伦作品合集 - P03 晴天")

    def test_search_title_is_retained_without_affecting_deliverability(self) -> None:
        result = candidate(
            title="详情页标题",
            page_title="歌曲页",
            search_title="晴天 AI 翻唱",
        )

        self.assertEqual(result.search_title, "晴天 AI 翻唱")
        self.assertEqual(filter_bilibili_candidates([result]), (result,))


class BilibiliCandidateFilterTests(unittest.TestCase):
    def test_prepare_search_query_removes_noise_without_inventing_original(
        self,
    ) -> None:
        for original_marker in ("原版", "原唱", "原曲"):
            with self.subTest(original_marker=original_marker):
                self.assertEqual(
                    prepare_bilibili_search_query(
                        f"请帮我听歌 {original_marker} 周杰伦 晴天 320kbps 无损"
                    ),
                    "周杰伦 晴天",
                )

        self.assertEqual(prepare_bilibili_search_query("日不落"), "日不落")
        self.assertEqual(
            prepare_bilibili_search_query("我要听 晴天 Live Remix"),
            "晴天 live remix",
        )

    def test_keeps_platform_order_for_all_version_labels(self) -> None:
        pages = [
            candidate(bvid="ai", title="晴天 AI 翻唱"),
            candidate(bvid="live", title="晴天 现场 Live"),
            candidate(bvid="cover", title="晴天 翻唱"),
            candidate(bvid="remix", title="晴天 DJ Remix"),
            candidate(bvid="tutorial", title="晴天 吉他教学教程"),
            candidate(bvid="mv", title="晴天 Official MV"),
        ]

        results = filter_bilibili_candidates(pages, limit=len(pages))

        self.assertEqual(
            [item.bvid for item in results],
            ["ai", "live", "cover", "remix", "tutorial", "mv"],
        )

    def test_only_unplayable_durations_are_removed(self) -> None:
        pages = [
            candidate(bvid="unknown", duration_ms=0),
            candidate(bvid="too-long", duration_ms=15 * 60 * 1000 + 1),
            candidate(bvid="short-song", duration_ms=21_000),
            candidate(bvid="normal", duration_ms=269_000),
        ]

        results = filter_bilibili_candidates(pages)

        self.assertEqual([item.bvid for item in results], ["short-song", "normal"])

    def test_deduplicates_and_applies_the_limit_in_platform_order(self) -> None:
        first = candidate(bvid="first", title="晴天")
        duplicate = candidate(bvid="first", title="晴天 重复数据")
        second_page = candidate(bvid="first", cid=2, title="晴天 P2")
        second = candidate(bvid="second", title="晴天")

        results = filter_bilibili_candidates(
            [first, duplicate, second_page, second],
            limit=3,
        )

        self.assertEqual(
            [item.candidate_id for item in results],
            ["first:1", "first:2", "second:1"],
        )

    def test_non_positive_limit_returns_no_candidates(self) -> None:
        self.assertEqual(filter_bilibili_candidates([candidate()], limit=0), ())


if __name__ == "__main__":
    unittest.main()
