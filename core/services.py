"""Bilibili-only music search, selection, and delivery workflows.

The search service preserves Bilibili's video order, expands each hit into a
concrete playable page, and applies only delivery safety filters. A selected
``bvid`` and ``cid`` is then delivered directly; there is deliberately no
second catalogue and no cross-source substitution.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
import re
from typing import Any, Protocol
import unicodedata

from .bilibili import BilibiliVideoRef
from .matcher import filter_bilibili_candidates, prepare_bilibili_search_query
from .media import (
    FfmpegUnavailableError,
    MediaError,
    MediaLimits,
    MediaStore,
    MediaTooLargeError,
    VOICE_MEDIA_LIMITS,
)
from .models import BilibiliCandidate, LocalMedia, SearchSnapshot
from .selection import SearchSnapshotStore


SEARCH_LIMIT = 10
BILIBILI_VIDEO_LIMIT = 12
BILIBILI_DETAIL_CONCURRENCY = 4
BILIBILI_PAGE_LIMIT = 48


class MusicSearchError(RuntimeError):
    """A user-safe failure to obtain Bilibili music candidates."""


class DeliveryError(RuntimeError):
    """A user-safe failure to materialize a selected Bilibili page."""


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """The exact selected Bilibili page and its local media file."""

    candidate: BilibiliCandidate
    media: LocalMedia


@dataclass(frozen=True, slots=True)
class MusicCandidateSummary:
    """The non-sensitive candidate evidence made available to the LLM.

    Source IDs and account metadata stay inside :class:`BilibiliCandidate`.
    The one-based position is the only selection handle that crosses into the
    conversation layer, where it is resolved against the session snapshot.
    """

    position: int
    title: str
    duration: str
    search_title: str
    page_title: str


class _BilibiliClient(Protocol):
    async def search_videos(
        self, query: str, *, limit: int = BILIBILI_VIDEO_LIMIT
    ) -> Sequence[Any]: ...

    async def get_video(self, bvid: str) -> Any: ...

    async def get_video_by_aid(self, aid: int) -> Any: ...

    async def resolve_audio(self, bvid: str, cid: int) -> Any: ...


class SearchService:
    """Search Bilibili videos, expand pages, then retain deliverable results."""

    def __init__(
        self,
        bilibili: _BilibiliClient,
        snapshots: SearchSnapshotStore | None = None,
    ) -> None:
        self._bilibili = bilibili
        self._snapshots = snapshots or SearchSnapshotStore()

    async def search(
        self,
        *,
        session_id: str,
        query: str,
        song_title: str | None = None,
        video_ref: BilibiliVideoRef | None = None,
        max_duration_ms: int | None = None,
    ) -> SearchSnapshot:
        """Search Bilibili and retain a session-bound candidate set.

        The source keyword is the cleaned user request and never receives an
        invented version label. Candidate labels remain available for the
        LLM's contextual choice; local filtering only enforces delivery facts.

        ``song_title`` is optional because command searches receive a free-form
        keyword. When supplied by the structured LLM request, it identifies
        pages within multi-page videos and can provide one narrower recall
        pass when the complete query returns fewer than ten results.

        ``video_ref`` is an exact video-level reference parsed from user input.
        It bypasses keyword recall but still expands to page-level candidates,
        so a download can never skip the normal snapshot and user selection.
        """

        if not session_id.strip():
            raise ValueError("session_id must not be blank")

        if video_ref is None:
            requested_query, candidates = await self._filtered_candidates(
                query,
                song_title=song_title,
                max_duration_ms=max_duration_ms,
            )
        else:
            requested_query = _video_ref_label(video_ref)
            candidates = filter_bilibili_candidates(
                await self._reference_candidates(
                    video_ref,
                    max_duration_ms=max_duration_ms,
                ),
                limit=SEARCH_LIMIT,
                max_duration_ms=max_duration_ms,
            )
            if not candidates:
                raise MusicSearchError("指定的 Bilibili 视频没有可播放的音频")
        return self._snapshots.create(
            session_id=session_id,
            query=requested_query,
            candidates=candidates,
        )

    async def _filtered_candidates(
        self,
        query: str,
        *,
        song_title: str | None,
        max_duration_ms: int | None,
    ) -> tuple[str, tuple[BilibiliCandidate, ...]]:
        requested_query = _normalize_query(query)
        api_query = prepare_bilibili_search_query(requested_query)
        if not api_query:
            raise MusicSearchError("请提供要搜索的歌曲名称")

        candidates = await self._search_candidates(
            api_query,
            song_title=song_title,
            max_duration_ms=max_duration_ms,
        )
        filtered = filter_bilibili_candidates(
            candidates,
            limit=SEARCH_LIMIT,
            max_duration_ms=max_duration_ms,
        )

        fallback_query = _song_title_fallback_query(song_title, api_query)
        if len(filtered) < SEARCH_LIMIT and fallback_query is not None:
            try:
                fallback_candidates = await self._search_candidates(
                    fallback_query,
                    song_title=song_title,
                    max_duration_ms=max_duration_ms,
                )
            except MusicSearchError:
                # The second query improves recall only; a source failure must
                # never discard usable candidates from the primary query.
                fallback_candidates = ()
            filtered = _merge_candidates(
                filtered,
                fallback_candidates,
                max_duration_ms=max_duration_ms,
            )
        if not filtered:
            raise MusicSearchError(f"没有找到“{requested_query}”的可播放 Bilibili 音乐")
        return requested_query, filtered

    def snapshot(self, *, search_id: str, session_id: str) -> SearchSnapshot | None:
        """Return a valid snapshot only to the session that created it."""

        return self._snapshots.get(search_id=search_id, session_id=session_id)

    async def _search_candidates(
        self,
        api_query: str,
        *,
        song_title: str | None,
        max_duration_ms: int | None,
    ) -> tuple[BilibiliCandidate, ...]:
        try:
            videos = await self._bilibili.search_videos(
                api_query,
                limit=BILIBILI_VIDEO_LIMIT,
            )
        except Exception as exc:
            raise MusicSearchError("Bilibili 搜索暂时不可用，请稍后重试") from exc
        if not videos:
            return ()

        semaphore = asyncio.Semaphore(BILIBILI_DETAIL_CONCURRENCY)

        async def fetch(video: Any) -> Any | None:
            bvid = str(getattr(video, "bvid", "")).strip()
            if not bvid:
                return None
            try:
                async with semaphore:
                    return await self._bilibili.get_video(bvid)
            except Exception:
                return None

        detailed = await asyncio.gather(*(fetch(video) for video in videos))
        candidates: list[BilibiliCandidate] = []
        for search_hit, video in zip(videos, detailed):
            if video is None:
                continue
            candidates.extend(
                _expand_video_candidates(
                    video,
                    search_title=str(getattr(search_hit, "title", "")).strip(),
                    song_title=song_title,
                    max_duration_ms=max_duration_ms,
                )
            )
            if len(candidates) >= BILIBILI_PAGE_LIMIT:
                return tuple(candidates[:BILIBILI_PAGE_LIMIT])
        return tuple(candidates)

    async def _reference_candidates(
        self,
        video_ref: BilibiliVideoRef,
        *,
        max_duration_ms: int | None,
    ) -> tuple[BilibiliCandidate, ...]:
        """Resolve one user-provided video reference without keyword fallback."""

        try:
            if video_ref.bvid is not None:
                video = await self._bilibili.get_video(video_ref.bvid)
            else:
                assert video_ref.aid is not None
                video = await self._bilibili.get_video_by_aid(video_ref.aid)
        except Exception as exc:
            raise MusicSearchError("无法打开指定的 Bilibili 视频") from exc
        return _expand_video_candidates(
            video,
            search_title=str(getattr(video, "title", "")).strip(),
            song_title=None,
            include_all_pages=True,
            max_duration_ms=max_duration_ms,
        )


class DeliveryService:
    """Resolve and materialize one already-validated Bilibili page."""

    def __init__(
        self,
        *,
        bilibili: _BilibiliClient,
        media: MediaStore,
    ) -> None:
        self._bilibili = bilibili
        self._media = media

    async def deliver(
        self,
        candidate: BilibiliCandidate,
        *,
        limits: MediaLimits = VOICE_MEDIA_LIMITS,
    ) -> DeliveryResult:
        """Materialize one selected page within its delivery-mode limits."""

        if _duration_exceeds_limit(candidate.duration_ms, limits):
            raise DeliveryError(_delivery_duration_error(limits))
        if not self._media.ffmpeg_available:
            raise FfmpegUnavailableError("宿主机未安装 ffmpeg，暂时无法听歌或下载歌曲")
        try:
            audio = await self._bilibili.resolve_audio(candidate.bvid, candidate.cid)
            if _duration_exceeds_limit(audio.duration_ms, limits):
                raise DeliveryError(_delivery_duration_error(limits))
            media = await self._media.prepare(
                audio,
                filename_stem=_display_stem(candidate),
                limits=limits,
            )
        except FfmpegUnavailableError:
            raise
        except DeliveryError:
            raise
        except MediaTooLargeError as exc:
            raise DeliveryError(str(exc)) from exc
        except MediaError as exc:
            raise DeliveryError("Bilibili 音频下载失败") from exc
        except Exception as exc:
            raise DeliveryError("Bilibili 未返回可播放音频") from exc
        return DeliveryResult(candidate=candidate, media=media)


def format_search_results(snapshot: SearchSnapshot) -> str:
    """Produce the only user-visible catalogue rendering used by command flow."""

    lines = [f"Bilibili 搜索结果：{snapshot.query}"]
    for position, candidate in enumerate(snapshot.candidates, start=1):
        download_only = (
            "，仅可下载"
            if _duration_exceeds_limit(candidate.duration_ms, VOICE_MEDIA_LIMITS)
            else ""
        )
        lines.append(
            f"{position}. {candidate.display_title} "
            f"({_duration_text(candidate.duration_ms)}{download_only})"
        )
    lines.extend(
        (
            "听歌：回复“序号”",
            "下载：回复“序号 下载”",
        )
    )

    return "\n".join(lines)


def summarize_search_candidates(
    snapshot: SearchSnapshot,
) -> tuple[MusicCandidateSummary, ...]:
    """Return safe, one-based candidate evidence for model-side selection."""

    return tuple(
        MusicCandidateSummary(
            position=position,
            title=candidate.display_title,
            duration=_duration_text(candidate.duration_ms),
            search_title=candidate.search_title,
            page_title=candidate.page_title,
        )
        for position, candidate in enumerate(snapshot.candidates, start=1)
    )


def _normalize_query(query: str) -> str:
    normalized = " ".join(str(query).replace("\x00", "").split())
    if not normalized:
        raise MusicSearchError("请提供要搜索的歌曲名称")
    if len(normalized) > 120:
        raise MusicSearchError("搜索关键词不能超过 120 个字符")
    return normalized


def _video_ref_label(video_ref: BilibiliVideoRef) -> str:
    """Use the literal reference as the short snapshot label before lookup."""

    return video_ref.bvid or f"av{video_ref.aid}"


def _expand_video_candidates(
    video: Any,
    *,
    search_title: str,
    song_title: str | None,
    include_all_pages: bool = False,
    max_duration_ms: int | None = None,
) -> tuple[BilibiliCandidate, ...]:
    """Turn one validated video detail response into bounded page candidates."""

    bvid = str(getattr(video, "bvid", "")).strip()
    title = str(getattr(video, "title", "")).strip()
    uploader = str(getattr(video, "uploader", "")).strip()
    if not bvid or not title:
        return ()

    pages = tuple(getattr(video, "pages", ()))
    selected_pages = pages if include_all_pages else _select_pages(pages, song_title)
    candidates: list[BilibiliCandidate] = []
    for page in selected_pages:
        cid = _positive_int(getattr(page, "cid", 0))
        duration_ms = _nonnegative_int(getattr(page, "duration_ms", 0))
        if not cid or not _is_within_duration_limit(duration_ms, max_duration_ms):
            continue
        try:
            candidates.append(
                BilibiliCandidate(
                    bvid=bvid,
                    cid=cid,
                    title=title,
                    page_title=str(getattr(page, "title", "")).strip(),
                    uploader=uploader,
                    duration_ms=duration_ms,
                    search_title=search_title,
                )
            )
        except ValueError:
            continue
        if len(candidates) >= BILIBILI_PAGE_LIMIT:
            break
    return tuple(candidates)


def _song_title_fallback_query(
    song_title: str | None, primary_query: str
) -> str | None:
    """Return a narrower valid query only when it differs from the primary one."""

    if not isinstance(song_title, str):
        return None
    try:
        title_query = prepare_bilibili_search_query(_normalize_query(song_title))
    except MusicSearchError:
        return None
    return title_query if title_query and title_query != primary_query else None


def _merge_candidates(
    primary: Sequence[BilibiliCandidate],
    fallback: Sequence[BilibiliCandidate],
    *,
    max_duration_ms: int | None,
) -> tuple[BilibiliCandidate, ...]:
    """Preserve primary ordering while deduplicating a bounded recall fallback."""

    return filter_bilibili_candidates(
        (*primary, *fallback),
        limit=SEARCH_LIMIT,
        max_duration_ms=max_duration_ms,
    )


def _is_within_duration_limit(
    duration_ms: int,
    max_duration_ms: int | None,
) -> bool:
    return duration_ms > 0 and (
        max_duration_ms is None or duration_ms <= max_duration_ms
    )


def _display_stem(candidate: BilibiliCandidate) -> str:
    uploader = candidate.uploader or "Bilibili"
    return f"{candidate.display_title} - {uploader}"


def _duration_text(duration_ms: int) -> str:
    seconds = max(0, duration_ms) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def _duration_exceeds_limit(
    duration_ms: int | None,
    limits: MediaLimits,
) -> bool:
    return bool(
        duration_ms
        and limits.max_duration_ms is not None
        and duration_ms > limits.max_duration_ms
    )


def _delivery_duration_error(limits: MediaLimits) -> str:
    """Describe the only duration limit users can encounter: voice delivery."""

    assert limits.max_duration_ms is not None
    minutes = limits.max_duration_ms // 60_000
    if limits.max_duration_ms == VOICE_MEDIA_LIMITS.max_duration_ms:
        return f"歌曲时长超过 {minutes} 分钟，请回复“序号 下载”发送文件"
    return f"歌曲时长超过 {minutes} 分钟，无法发送"


def _positive_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _select_pages(pages: Any, song_title: str | None) -> tuple[Any, ...]:
    """Keep all pages of a single-page video, but never guess a collection P.

    Bilibili ranks videos, not individual pages. A multi-page video can be a
    playlist or an album, where taking its first page would silently change the
    requested song. For those videos this tiny check is a page-identity guard,
    not a second ranking algorithm: pages stay in their source order and are
    retained only when the structured song title is visibly present as a
    complete title phrase. If that evidence is absent, the video is skipped
    rather than delivering an arbitrary page. In particular, artist terms
    from the broader Bilibili query can never choose a page.
    """

    values = tuple(pages)
    if len(values) <= 1:
        return values

    terms = _page_title_terms(song_title)
    if not terms:
        return ()
    return tuple(
        page
        for page in values
        if _contains_page_title(
            _page_title_terms(getattr(page, "title", "")),
            terms,
        )
    )


def _page_title_terms(value: Any) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return tuple(_PAGE_TITLE_TERM_RE.findall(normalized))


def _contains_page_title(
    page_terms: tuple[str, ...],
    requested_terms: tuple[str, ...],
) -> bool:
    window_size = len(requested_terms)
    return any(
        page_terms[index : index + window_size] == requested_terms
        for index in range(len(page_terms) - window_size + 1)
    )


_PAGE_TITLE_TERM_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+", re.IGNORECASE)


__all__ = [
    "BILIBILI_DETAIL_CONCURRENCY",
    "BILIBILI_VIDEO_LIMIT",
    "DeliveryError",
    "DeliveryResult",
    "DeliveryService",
    "MusicCandidateSummary",
    "MusicSearchError",
    "SEARCH_LIMIT",
    "SearchService",
    "format_search_results",
    "summarize_search_candidates",
]
