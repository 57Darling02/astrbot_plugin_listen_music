from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.media import MediaError
from core.models import LocalMedia, ResolvedAudio
from core.services import (
    SEARCH_LIMIT,
    DeliveryError,
    DeliveryService,
    MusicCandidateSummary,
    MusicSearchError,
    SearchService,
    format_search_results,
    summarize_search_candidates,
)


@dataclass(frozen=True)
class Hit:
    bvid: str
    category: str = ""
    title: str = ""


@dataclass(frozen=True)
class Page:
    cid: int
    title: str
    duration_ms: int


@dataclass(frozen=True)
class Video:
    bvid: str
    title: str
    uploader: str
    pages: tuple[Page, ...]
    category: str = ""


class FakeBilibili:
    def __init__(
        self,
        videos: tuple[Video, ...],
        *,
        audio: ResolvedAudio | None = None,
        search_error: Exception | None = None,
        resolve_error: Exception | None = None,
    ) -> None:
        self._videos = {video.bvid: video for video in videos}
        self._hits = tuple(
            Hit(video.bvid, video.category, f"搜索结果：{video.title}")
            for video in videos
        )
        self._audio = audio or ResolvedAudio(
            url="https://cdn.example.test/audio.m4s",
            mime_type="audio/mp4",
            duration_ms=269_000,
            needs_remux=True,
        )
        self._search_error = search_error
        self._resolve_error = resolve_error
        self.search_queries: list[str] = []
        self.resolve_calls: list[tuple[str, int]] = []

    async def search_videos(self, query: str, *, limit: int = 12):
        self.search_queries.append(query)
        if self._search_error is not None:
            raise self._search_error
        return self._hits[:limit]

    async def get_video(self, bvid: str):
        return self._videos[bvid]

    async def resolve_audio(self, bvid: str, cid: int) -> ResolvedAudio:
        self.resolve_calls.append((bvid, cid))
        if self._resolve_error is not None:
            raise self._resolve_error
        return self._audio


class FakeMedia:
    ffmpeg_available = True

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.prepared: list[ResolvedAudio] = []

    async def prepare(self, audio: ResolvedAudio, *, filename_stem: str) -> LocalMedia:
        self.prepared.append(audio)
        if self._error is not None:
            raise self._error
        return LocalMedia(
            path="/tmp/bilibili-fixture.m4a",
            filename=f"{filename_stem}.m4a",
            mime_type="audio/mp4",
            size_bytes=12,
        )


def video(
    bvid: str,
    *,
    title: str = "周杰伦 - 晴天",
    uploader: str = "周杰伦音乐",
    pages: tuple[Page, ...] = (Page(1, "晴天", 269_000),),
    category: str = "",
) -> Video:
    return Video(
        bvid=bvid,
        title=title,
        uploader=uploader,
        pages=pages,
        category=category,
    )


class BilibiliWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def _workflow(
        self,
        videos: tuple[Video, ...],
        *,
        audio: ResolvedAudio | None = None,
        search_error: Exception | None = None,
        resolve_error: Exception | None = None,
        media_error: Exception | None = None,
    ) -> tuple[SearchService, DeliveryService, FakeBilibili, FakeMedia]:
        bilibili = FakeBilibili(
            videos,
            audio=audio,
            search_error=search_error,
            resolve_error=resolve_error,
        )
        search = SearchService(bilibili)
        media = FakeMedia(error=media_error)
        delivery = DeliveryService(bilibili=bilibili, media=media)
        return search, delivery, bilibili, media

    async def test_search_expands_pages_and_returns_only_ten_platform_order_candidates(
        self,
    ) -> None:
        videos = (
            video("BV1"),
            video("BV2"),
            video("BV3"),
            video("BV4"),
            video("BV5"),
            video("BV6"),
            video("BV7"),
            video("BV8"),
            video("BV9"),
            video("BV10"),
            video("BV11"),
        )
        search, _, bilibili, _ = await self._workflow(videos)

        snapshot = await search.search(session_id="chat-a", query="晴天")

        self.assertEqual(SEARCH_LIMIT, 10)
        self.assertEqual(
            [candidate.candidate_id for candidate in snapshot.candidates],
            [
                "BV1:1",
                "BV2:1",
                "BV3:1",
                "BV4:1",
                "BV5:1",
                "BV6:1",
                "BV7:1",
                "BV8:1",
                "BV9:1",
                "BV10:1",
            ],
        )
        self.assertEqual(bilibili.search_queries, ["晴天"])
        self.assertEqual(bilibili.resolve_calls, [])

    async def test_search_strips_original_source_terms_but_keeps_display_query(
        self,
    ) -> None:
        search, _, bilibili, _ = await self._workflow((video("BV1"),))

        snapshot = await search.search(session_id="chat-a", query="请播放 原版 晴天")

        self.assertEqual(snapshot.query, "请播放 原版 晴天")
        self.assertEqual(bilibili.search_queries, ["晴天"])
        self.assertEqual(snapshot.candidates[0].candidate_id, "BV1:1")

    async def test_search_retains_mv_but_discards_an_unrelated_category(self) -> None:
        videos = (
            video(
                "BVmv",
                title="爱人 Official MV",
                pages=(Page(1, "爱人 Official MV", 269_000),),
                category="MV",
            ),
            video(
                "BVstory",
                title="爱人的情感故事",
                pages=(Page(1, "爱人的情感故事", 269_000),),
                category="情感",
            ),
        )
        search, _, _, _ = await self._workflow(videos)

        snapshot = await search.search(session_id="chat-a", query="爱人")

        self.assertEqual(
            [
                (candidate.candidate_id, candidate.category)
                for candidate in snapshot.candidates
            ],
            [("BVmv:1", "MV")],
        )

    async def test_candidate_summary_keeps_search_title_without_source_metadata(
        self,
    ) -> None:
        source = video(
            "BVsource",
            title="详情页标题",
            uploader="不应暴露的上传者",
            pages=(Page(7, "P2 歌曲页", 269_000),),
            category="MV",
        )
        search, _, _, _ = await self._workflow((source,))

        snapshot = await search.search(session_id="chat-a", query="晴天")

        self.assertEqual(snapshot.candidates[0].search_title, "搜索结果：详情页标题")
        summary = summarize_search_candidates(snapshot)
        self.assertEqual(
            summary[0],
            MusicCandidateSummary(
                position=1,
                title="详情页标题 - P2 歌曲页",
                duration="4:29",
                search_title="搜索结果：详情页标题",
                page_title="P2 歌曲页",
            ),
        )
        rendered_for_user = format_search_results(snapshot)
        self.assertIn("详情页标题 - P2 歌曲页 (4:29)", rendered_for_user)
        self.assertNotIn("UP主", rendered_for_user)
        self.assertEqual(
            set(summary[0].__dataclass_fields__),
            {"position", "title", "duration", "search_title", "page_title"},
        )

    async def test_search_can_exclude_alternative_versions_without_rewriting_keyword(
        self,
    ) -> None:
        original = video("BVoriginal")
        live = video(
            "BVlive",
            title="周杰伦 - 晴天 Live",
            pages=(Page(1, "晴天 Live", 269_000),),
        )
        search, _, bilibili, _ = await self._workflow((live, original))

        snapshot = await search.search(
            session_id="chat-a",
            query="晴天",
            exclude_alternative_versions=True,
        )

        self.assertEqual(bilibili.search_queries, ["晴天"])
        self.assertEqual(snapshot.query, "晴天")
        self.assertEqual(
            [item.candidate_id for item in snapshot.candidates], ["BVoriginal:1"]
        )

    async def test_alternative_filter_does_not_override_an_explicit_version(
        self,
    ) -> None:
        original = video("BVoriginal")
        live = video(
            "BVlive",
            title="周杰伦 - 晴天 Live",
            pages=(Page(1, "晴天 Live", 269_000),),
        )
        search, _, bilibili, _ = await self._workflow((original, live))

        snapshot = await search.search(
            session_id="chat-a",
            query="晴天 Live",
            exclude_alternative_versions=True,
        )

        self.assertEqual(bilibili.search_queries, ["晴天 live"])
        self.assertEqual(
            [item.candidate_id for item in snapshot.candidates], ["BVlive:1"]
        )

    async def test_search_returns_a_session_bound_candidate_snapshot(self) -> None:
        search, _, _, _ = await self._workflow((video("BV1"),))
        snapshot = await search.search(session_id="chat-a", query="晴天")

        owned = search.snapshot(search_id=snapshot.search_id, session_id="chat-a")
        self.assertIsNotNone(owned)
        assert owned is not None
        self.assertEqual(owned.candidates, snapshot.candidates)
        self.assertEqual(owned.query, "晴天")
        self.assertIsNone(
            search.snapshot(search_id=snapshot.search_id, session_id="chat-b")
        )

    async def test_multi_page_identity_uses_the_song_title_not_artist_terms(
        self,
    ) -> None:
        pages = (
            Page(10, "周杰伦访谈", 31_000),
            Page(11, "晴天", 269_000),
        )
        search, _, _, _ = await self._workflow(
            (video("BVmulti", title="周杰伦歌曲合集", pages=pages),)
        )

        snapshot = await search.search(
            session_id="chat-a",
            query="晴天 周杰伦",
            song_title="晴天",
        )

        self.assertEqual(
            [candidate.candidate_id for candidate in snapshot.candidates],
            ["BVmulti:11"],
        )

    async def test_multi_page_without_a_structured_title_is_skipped(self) -> None:
        pages = (
            Page(10, "周杰伦访谈", 31_000),
            Page(11, "晴天", 269_000),
        )
        search, _, _, _ = await self._workflow(
            (video("BVmulti", title="周杰伦歌曲合集", pages=pages),)
        )

        with self.assertRaisesRegex(MusicSearchError, "没有找到"):
            await search.search(session_id="chat-a", query="晴天 周杰伦")

    async def test_search_keeps_bilibili_order_for_simplified_traditional_names(
        self,
    ) -> None:
        official = video(
            "BVofficial",
            title=("莉莉周她說 Lily Chou-Chou Lied【愛人】Official Music Video"),
            pages=(
                Page(
                    1,
                    "莉莉周她說 Lily Chou-Chou Lied【愛人】Official Music Video",
                    323_000,
                ),
            ),
        )
        ai_cover = video(
            "BVai",
            title="【AI东雪莲】爱人-莉莉周她说 Lily Chou-Chou Lied",
            pages=(Page(1, "爱人", 304_000),),
        )
        search, _, bilibili, _ = await self._workflow((official, ai_cover))

        snapshot = await search.search(
            session_id="chat-a",
            query="爱人 莉莉周她说 Lily Chou-Chou Lied",
            exclude_alternative_versions=True,
        )

        self.assertEqual(snapshot.candidates[0].candidate_id, "BVofficial:1")
        self.assertEqual(
            bilibili.search_queries,
            ["爱人 莉莉周她说 lily chou-chou lied"],
        )

    async def test_delivery_resolves_only_the_selected_bilibili_page(self) -> None:
        search, delivery, bilibili, media = await self._workflow(
            (video("BV1"), video("BV2")),
        )
        snapshot = await search.search(session_id="chat-a", query="晴天")
        selected = snapshot.candidates[1]

        result = await delivery.deliver(selected)

        self.assertEqual(result.candidate, selected)
        self.assertEqual(bilibili.resolve_calls, [("BV2", 1)])
        self.assertEqual(len(media.prepared), 1)

    async def test_snapshot_rejects_cross_session_or_hallucinated_candidate_ids(
        self,
    ) -> None:
        search, _, bilibili, _ = await self._workflow((video("BV1"),))
        snapshot = await search.search(session_id="chat-a", query="晴天")

        self.assertIsNone(
            search.snapshot(search_id=snapshot.search_id, session_id="chat-b")
        )
        self.assertIsNone(snapshot.candidate("BVinvented:99"))
        self.assertEqual(bilibili.resolve_calls, [])

    async def test_unplayable_selected_page_does_not_silently_switch_candidates(
        self,
    ) -> None:
        search, delivery, bilibili, _ = await self._workflow(
            (video("BV1"), video("BV2")),
            resolve_error=RuntimeError("unavailable"),
        )
        snapshot = await search.search(session_id="chat-a", query="晴天")

        with self.assertRaisesRegex(DeliveryError, "未返回可播放"):
            await delivery.deliver(snapshot.candidates[0])
        self.assertEqual(bilibili.resolve_calls, [("BV1", 1)])

    async def test_search_failure_and_no_deliverable_results_are_reported(self) -> None:
        failing, _, _, _ = await self._workflow(
            (),
            search_error=RuntimeError("network"),
        )
        with self.assertRaisesRegex(MusicSearchError, "暂时不可用"):
            await failing.search(session_id="chat-a", query="晴天")

        unmatched, _, _, _ = await self._workflow(
            (
                video(
                    "BVwrong",
                    title="晴天 吉他教学教程",
                    pages=(Page(1, "晴天", 223_000),),
                ),
            )
        )
        with self.assertRaisesRegex(MusicSearchError, "没有找到"):
            await unmatched.search(session_id="chat-a", query="晴天")

    async def test_media_failure_is_exposed_without_cross_video_retry(self) -> None:
        search, delivery, bilibili, _ = await self._workflow(
            (video("BV1"), video("BV2")),
            media_error=MediaError("download failed"),
        )
        snapshot = await search.search(session_id="chat-a", query="晴天")

        with self.assertRaisesRegex(DeliveryError, "下载失败"):
            await delivery.deliver(snapshot.candidates[0])
        self.assertEqual(bilibili.resolve_calls, [("BV1", 1)])


if __name__ == "__main__":
    unittest.main()
