from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.media import (
    DOWNLOAD_MEDIA_LIMITS,
    VOICE_MEDIA_LIMITS,
    MediaError,
)
from core.bilibili import BilibiliVideoRef
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


class FakeBilibili:
    def __init__(
        self,
        videos: tuple[Video, ...],
        *,
        audio: ResolvedAudio | None = None,
        search_error: Exception | None = None,
        query_hits: dict[str, tuple[str, ...]] | None = None,
        query_errors: dict[str, Exception] | None = None,
        reference_videos: dict[int, Video] | None = None,
        reference_error: Exception | None = None,
        resolve_error: Exception | None = None,
    ) -> None:
        self._videos = {video.bvid: video for video in videos}
        self._hits = tuple(
            Hit(video.bvid, f"搜索结果：{video.title}") for video in videos
        )
        self._hits_by_bvid = {hit.bvid: hit for hit in self._hits}
        self._audio = audio or ResolvedAudio(
            url="https://cdn.example.test/audio.m4s",
            mime_type="audio/mp4",
            duration_ms=269_000,
            needs_remux=True,
        )
        self._search_error = search_error
        self._query_hits = dict(query_hits or {})
        self._query_errors = dict(query_errors or {})
        self._reference_videos = dict(reference_videos or {})
        self._reference_error = reference_error
        self._resolve_error = resolve_error
        self.search_queries: list[str] = []
        self.video_calls: list[str] = []
        self.aid_calls: list[int] = []
        self.resolve_calls: list[tuple[str, int]] = []

    async def search_videos(self, query: str, *, limit: int = 12):
        self.search_queries.append(query)
        if self._search_error is not None:
            raise self._search_error
        error = self._query_errors.get(query)
        if error is not None:
            raise error
        bvids = self._query_hits.get(query)
        hits = (
            tuple(self._hits_by_bvid[bvid] for bvid in bvids)
            if bvids is not None
            else self._hits
        )
        return hits[:limit]

    async def get_video(self, bvid: str):
        self.video_calls.append(bvid)
        if self._reference_error is not None:
            raise self._reference_error
        return self._videos[bvid]

    async def get_video_by_aid(self, aid: int):
        self.aid_calls.append(aid)
        if self._reference_error is not None:
            raise self._reference_error
        return self._reference_videos[aid]

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
        self.limits: list[object] = []

    async def prepare(
        self, audio: ResolvedAudio, *, filename_stem: str, limits
    ) -> LocalMedia:
        self.prepared.append(audio)
        self.limits.append(limits)
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
) -> Video:
    return Video(
        bvid=bvid,
        title=title,
        uploader=uploader,
        pages=pages,
    )


class BilibiliWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def _workflow(
        self,
        videos: tuple[Video, ...],
        *,
        audio: ResolvedAudio | None = None,
        search_error: Exception | None = None,
        query_hits: dict[str, tuple[str, ...]] | None = None,
        query_errors: dict[str, Exception] | None = None,
        reference_videos: dict[int, Video] | None = None,
        reference_error: Exception | None = None,
        resolve_error: Exception | None = None,
        media_error: Exception | None = None,
    ) -> tuple[SearchService, DeliveryService, FakeBilibili, FakeMedia]:
        bilibili = FakeBilibili(
            videos,
            audio=audio,
            search_error=search_error,
            query_hits=query_hits,
            query_errors=query_errors,
            reference_videos=reference_videos,
            reference_error=reference_error,
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

    async def test_search_keeps_long_audio_without_a_duration_limit(self) -> None:
        search, _, _, _ = await self._workflow(
            (video("BVlong", pages=(Page(1, "长音频", 16 * 60 * 1000),)),)
        )

        snapshot = await search.search(session_id="chat-a", query="长音频")

        self.assertEqual(
            [item.candidate_id for item in snapshot.candidates], ["BVlong:1"]
        )
        self.assertIn("(16:00，仅可下载)", format_search_results(snapshot))

    async def test_search_excludes_long_audio_with_a_duration_limit(self) -> None:
        search, _, _, _ = await self._workflow(
            (video("BVlong", pages=(Page(1, "长音频", 16 * 60 * 1000),)),)
        )

        with self.assertRaisesRegex(MusicSearchError, "没有找到"):
            await search.search(
                session_id="chat-a",
                query="长音频",
                max_duration_ms=15 * 60 * 1000,
            )

    async def test_structured_song_title_fills_short_primary_results_once(self) -> None:
        videos = tuple(video(f"BV{index}") for index in range(1, 12))
        search, _, bilibili, _ = await self._workflow(
            videos,
            query_hits={
                "晴天 周杰伦": ("BV1", "BV2"),
                "晴天": tuple(f"BV{index}" for index in range(2, 12)),
            },
        )

        snapshot = await search.search(
            session_id="chat-a",
            query="晴天 周杰伦",
            song_title="晴天",
        )

        self.assertEqual(bilibili.search_queries, ["晴天 周杰伦", "晴天"])
        self.assertEqual(
            [candidate.candidate_id for candidate in snapshot.candidates],
            [f"BV{index}:1" for index in range(1, SEARCH_LIMIT + 1)],
        )

    async def test_song_title_fallback_uses_the_same_duration_limit(self) -> None:
        search, _, bilibili, _ = await self._workflow(
            (
                video("BVprimary"),
                video("BVlong", pages=(Page(1, "晴天 长内容", 16 * 60 * 1000),)),
                video("BVfallback"),
            ),
            query_hits={
                "晴天 周杰伦": ("BVprimary",),
                "晴天": ("BVlong", "BVfallback"),
            },
        )

        snapshot = await search.search(
            session_id="chat-a",
            query="晴天 周杰伦",
            song_title="晴天",
            max_duration_ms=15 * 60 * 1000,
        )

        self.assertEqual(bilibili.search_queries, ["晴天 周杰伦", "晴天"])
        self.assertEqual(
            [item.candidate_id for item in snapshot.candidates],
            ["BVprimary:1", "BVfallback:1"],
        )

    async def test_song_title_equal_to_primary_query_does_not_search_twice(
        self,
    ) -> None:
        search, _, bilibili, _ = await self._workflow((video("BV1"),))

        await search.search(
            session_id="chat-a",
            query="晴天",
            song_title="晴天",
        )

        self.assertEqual(bilibili.search_queries, ["晴天"])

    async def test_failed_song_title_fallback_keeps_primary_results(self) -> None:
        search, _, bilibili, _ = await self._workflow(
            (video("BV1"),),
            query_hits={"晴天 周杰伦": ("BV1",)},
            query_errors={"晴天": RuntimeError("temporary failure")},
        )

        snapshot = await search.search(
            session_id="chat-a",
            query="晴天 周杰伦",
            song_title="晴天",
        )

        self.assertEqual(bilibili.search_queries, ["晴天 周杰伦", "晴天"])
        self.assertEqual(
            [candidate.candidate_id for candidate in snapshot.candidates],
            ["BV1:1"],
        )

    async def test_exact_av_reference_expands_server_pages_without_searching(
        self,
    ) -> None:
        exact_video = video(
            "BV1canonical",
            title="完整视频",
            pages=(
                Page(11, "P1 正片", 269_000),
                Page(12, "P2 伴奏", 269_000),
            ),
        )
        search, _, bilibili, _ = await self._workflow(
            (),
            reference_videos={170001: exact_video},
        )

        snapshot = await search.search(
            session_id="chat-a",
            query="下载 av170001",
            song_title="不会用于选 P",
            video_ref=BilibiliVideoRef(aid=170001),
        )

        self.assertEqual(snapshot.query, "av170001")
        self.assertEqual(bilibili.search_queries, [])
        self.assertEqual(bilibili.aid_calls, [170001])
        self.assertEqual(
            [candidate.candidate_id for candidate in snapshot.candidates],
            ["BV1canonical:11", "BV1canonical:12"],
        )

    async def test_exact_reference_applies_the_duration_limit(self) -> None:
        exact_video = video(
            "BV1canonical",
            title="完整视频",
            pages=(
                Page(11, "P1 短内容", 269_000),
                Page(12, "P2 长内容", 16 * 60 * 1000),
            ),
        )
        search, _, bilibili, _ = await self._workflow(
            (),
            reference_videos={170001: exact_video},
        )

        snapshot = await search.search(
            session_id="chat-a",
            query="下载 av170001",
            video_ref=BilibiliVideoRef(aid=170001),
            max_duration_ms=15 * 60 * 1000,
        )

        self.assertEqual(bilibili.search_queries, [])
        self.assertEqual(
            [candidate.candidate_id for candidate in snapshot.candidates],
            ["BV1canonical:11"],
        )

    async def test_exact_reference_failure_never_falls_back_to_keyword_search(
        self,
    ) -> None:
        search, _, bilibili, _ = await self._workflow(
            (),
            reference_error=RuntimeError("missing video"),
        )

        with self.assertRaisesRegex(MusicSearchError, "无法打开"):
            await search.search(
                session_id="chat-a",
                query="下载 BV1Q541167Qg",
                video_ref=BilibiliVideoRef(bvid="BV1Q541167Qg"),
            )

        self.assertEqual(bilibili.search_queries, [])
        self.assertEqual(bilibili.video_calls, ["BV1Q541167Qg"])

    async def test_song_title_fallback_scans_past_primary_duplicates(self) -> None:
        search, _, bilibili, _ = await self._workflow(
            (video("BV1"), video("BV2"), video("BV3")),
            query_hits={
                "晴天 周杰伦": ("BV1",),
                "晴天": ("BV1",) * 10 + ("BV2", "BV3"),
            },
        )

        snapshot = await search.search(
            session_id="chat-a",
            query="晴天 周杰伦",
            song_title="晴天",
        )

        self.assertEqual(bilibili.search_queries, ["晴天 周杰伦", "晴天"])
        self.assertEqual(
            [candidate.candidate_id for candidate in snapshot.candidates],
            ["BV1:1", "BV2:1", "BV3:1"],
        )

    async def test_search_retains_all_deliverable_labels_in_source_order(
        self,
    ) -> None:
        videos = (
            video(
                "BVmv",
                title="爱人 Official MV",
                pages=(Page(1, "爱人 Official MV", 269_000),),
            ),
            video(
                "BVstory",
                title="爱人的情感故事",
                pages=(Page(1, "爱人的情感故事", 269_000),),
            ),
        )
        search, _, _, _ = await self._workflow(videos)

        snapshot = await search.search(session_id="chat-a", query="爱人")

        self.assertEqual(
            [candidate.candidate_id for candidate in snapshot.candidates],
            ["BVmv:1", "BVstory:1"],
        )

    async def test_candidate_summary_keeps_search_title_without_source_metadata(
        self,
    ) -> None:
        source = video(
            "BVsource",
            title="详情页标题",
            uploader="不应暴露的上传者",
            pages=(Page(7, "P2 歌曲页", 269_000),),
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
        self.assertIn("听歌：回复“序号”", rendered_for_user)
        self.assertIn("下载：回复“序号 下载”", rendered_for_user)
        self.assertEqual(
            set(summary[0].__dataclass_fields__),
            {"position", "title", "duration", "search_title", "page_title"},
        )

    async def test_search_keeps_version_labels_for_llm_selection(self) -> None:
        original = video("BVoriginal")
        live = video(
            "BVlive",
            title="周杰伦 - 晴天 Live",
            pages=(Page(1, "晴天 Live", 269_000),),
        )
        search, _, bilibili, _ = await self._workflow((live, original))

        snapshot = await search.search(session_id="chat-a", query="晴天")

        self.assertEqual(bilibili.search_queries, ["晴天"])
        self.assertEqual(snapshot.query, "晴天")
        self.assertEqual(
            [item.candidate_id for item in snapshot.candidates],
            ["BVlive:1", "BVoriginal:1"],
        )

    async def test_search_keeps_explicit_version_in_the_source_query(self) -> None:
        original = video("BVoriginal")
        live = video(
            "BVlive",
            title="周杰伦 - 晴天 Live",
            pages=(Page(1, "晴天 Live", 269_000),),
        )
        search, _, bilibili, _ = await self._workflow((original, live))

        snapshot = await search.search(session_id="chat-a", query="晴天 Live")

        self.assertEqual(bilibili.search_queries, ["晴天 live"])
        self.assertEqual(
            [item.candidate_id for item in snapshot.candidates],
            ["BVoriginal:1", "BVlive:1"],
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
        )

        self.assertEqual(
            [candidate.candidate_id for candidate in snapshot.candidates],
            ["BVofficial:1", "BVai:1"],
        )
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
        self.assertEqual(media.limits, [VOICE_MEDIA_LIMITS])

    async def test_long_audio_requires_download_limits_for_delivery(self) -> None:
        search, delivery, bilibili, media = await self._workflow(
            (video("BVlong", pages=(Page(1, "长音频", 16 * 60 * 1000),)),)
        )
        snapshot = await search.search(session_id="chat-a", query="长音频")
        candidate = snapshot.candidates[0]

        with self.assertRaisesRegex(DeliveryError, "序号 下载"):
            await delivery.deliver(candidate)
        self.assertEqual(bilibili.resolve_calls, [])

        result = await delivery.deliver(candidate, limits=DOWNLOAD_MEDIA_LIMITS)
        self.assertEqual(result.candidate, candidate)
        self.assertEqual(bilibili.resolve_calls, [("BVlong", 1)])
        self.assertEqual(media.limits, [DOWNLOAD_MEDIA_LIMITS])

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
                    pages=(Page(1, "晴天", 0),),
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
