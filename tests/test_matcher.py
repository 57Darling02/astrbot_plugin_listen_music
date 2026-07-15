from __future__ import annotations

from pathlib import Path
import sys
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.matcher import (
    filter_bilibili_candidates,
    prepare_bilibili_search_query,
)
from core.models import BilibiliCandidate


def candidate(
    *,
    bvid: str = "BV1test",
    cid: int = 1,
    title: str = "周杰伦 - 晴天",
    page_title: str = "",
    uploader: str = "音乐频道",
    duration_ms: int = 269_000,
    category: str = "",
    search_title: str = "",
) -> BilibiliCandidate:
    return BilibiliCandidate(
        bvid=bvid,
        cid=cid,
        title=title,
        page_title=page_title,
        uploader=uploader,
        duration_ms=duration_ms,
        category=category,
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
        self.assertEqual(result.match_text, "周杰伦作品合集 P03 晴天")
        self.assertEqual(result.display_title, "周杰伦作品合集 - P03 晴天")

    def test_search_title_is_retained_and_used_by_hard_filters(self) -> None:
        result = candidate(
            title="详情页标题",
            page_title="歌曲页",
            search_title="晴天 AI 翻唱",
        )

        self.assertEqual(result.search_title, "晴天 AI 翻唱")
        self.assertEqual(result.match_text, "晴天 AI 翻唱 详情页标题 歌曲页")
        self.assertEqual(
            filter_bilibili_candidates(
                "晴天", [result], exclude_alternative_versions=True
            ),
            (),
        )


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

    def test_keeps_bilibili_order_without_query_text_scoring(self) -> None:
        pages = [
            candidate(
                bvid="target",
                title="【私人曲库】愛人 - 莉莉周她說",
            ),
            candidate(bvid="other", title="爱人 - 另一位歌手"),
        ]

        results = filter_bilibili_candidates(
            "爱人-莉莉周她说 Lily Chou-Chou Lied",
            pages,
            exclude_alternative_versions=True,
        )

        self.assertEqual([item.bvid for item in results], ["target", "other"])

    def test_only_unplayable_durations_are_removed(self) -> None:
        pages = [
            candidate(bvid="unknown", duration_ms=0),
            candidate(bvid="too-long", duration_ms=15 * 60 * 1000 + 1),
            candidate(bvid="short-song", duration_ms=21_000),
            candidate(bvid="normal", duration_ms=269_000),
        ]

        results = filter_bilibili_candidates("晴天", pages)

        self.assertEqual([item.bvid for item in results], ["short-song", "normal"])

    def test_manual_search_keeps_explicit_alternative_versions_in_platform_order(
        self,
    ) -> None:
        pages = [
            candidate(bvid="ai", title="晴天 AI翻唱"),
            candidate(bvid="live", title="晴天 现场 Live"),
            candidate(bvid="mv", title="晴天 Official MV"),
        ]

        results = filter_bilibili_candidates("晴天", pages)

        self.assertEqual([item.bvid for item in results], ["ai", "live", "mv"])

    def test_direct_listening_excludes_clear_alternatives_but_keeps_mv(self) -> None:
        pages = [
            candidate(bvid="ai", title="晴天 AI翻唱"),
            candidate(bvid="ai-voice", title="晴天 AI翻声"),
            candidate(bvid="live", title="晴天 现场"),
            candidate(bvid="cover", title="晴天 翻唱"),
            candidate(bvid="remix", title="晴天 DJ Remix"),
            candidate(bvid="inst", title="晴天 伴奏"),
            candidate(bvid="mv", title="晴天 官方MV"),
        ]

        results = filter_bilibili_candidates(
            "晴天",
            pages,
            exclude_alternative_versions=True,
        )

        self.assertEqual([item.bvid for item in results], ["mv"])

    def test_explicit_version_is_an_exact_compatibility_constraint(self) -> None:
        pages = [
            candidate(bvid="original", title="晴天"),
            candidate(bvid="live", title="晴天 Live 现场"),
            candidate(bvid="live-cover", title="晴天 Live 翻唱"),
            candidate(bvid="remix", title="晴天 Remix"),
        ]

        live = filter_bilibili_candidates("晴天 Live", pages)
        original = filter_bilibili_candidates("原唱 晴天", pages)

        self.assertEqual([item.bvid for item in live], ["live"])
        self.assertEqual([item.bvid for item in original], ["original"])

    def test_filters_clearly_non_music_content_but_not_mv(self) -> None:
        pages = [
            candidate(bvid="tutorial", title="晴天 吉他教学教程"),
            candidate(bvid="commentary", title="晴天 歌曲讲解"),
            candidate(bvid="reaction", title="晴天 reaction"),
            candidate(bvid="fan-art", title="晴天 手书"),
            candidate(bvid="lyrics", title="晴天 动态歌词"),
            candidate(bvid="mv", title="晴天 Official MV"),
        ]

        results = filter_bilibili_candidates("晴天", pages)

        self.assertEqual([item.bvid for item in results], ["mv"])

    def test_uses_bilibili_category_as_a_narrow_negative_filter(self) -> None:
        pages = [
            candidate(bvid="mv", title="爱人 Official MV", category="MV"),
            candidate(
                bvid="unrelated",
                title="我的爱人故事",
                category="情感",
            ),
            candidate(
                bvid="unknown",
                title="爱人 录音室版本",
                category="未识别分类",
            ),
        ]

        results = filter_bilibili_candidates("爱人", pages)

        self.assertEqual([item.bvid for item in results], ["mv", "unknown"])

    def test_deduplicates_without_changing_the_first_platform_result(self) -> None:
        first = candidate(bvid="first", title="晴天")
        duplicate = candidate(bvid="first", title="晴天 重复数据")
        second = candidate(bvid="second", title="晴天")

        results = filter_bilibili_candidates("晴天", [first, duplicate, second])

        self.assertEqual([item.bvid for item in results], ["first", "second"])


if __name__ == "__main__":
    unittest.main()
