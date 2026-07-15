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

from .matcher import filter_bilibili_candidates, prepare_bilibili_search_query
from .media import FfmpegUnavailableError, MAX_MEDIA_DURATION_MS, MediaError, MediaStore
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

    async def resolve_audio(self, bvid: str, cid: int) -> Any: ...


class SearchService:
    """Search Bilibili videos, expand pages, then filter direct or manual results."""

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
        exclude_alternative_versions: bool = False,
    ) -> SearchSnapshot:
        """Search Bilibili and retain a session-bound candidate set.

        The source keyword is the cleaned user request and never receives an
        invented version label. ``exclude_alternative_versions`` only affects
        local filtering: it rejects clearly labelled Live, cover, Remix,
        instrumental, altered, and edit variants unless the query explicitly
        requests one. It does not assert that an unlabelled result is the
        canonical recording.

        ``song_title`` is optional because command searches receive a free-form
        keyword. When supplied by the structured LLM request, it is used only
        to identify pages within a multi-page video. The broader source query
        must never choose a page: it can contain artist and version terms.
        """

        if not session_id.strip():
            raise ValueError("session_id must not be blank")

        requested_query, candidates = await self._filtered_candidates(
            query,
            song_title=song_title,
            exclude_alternative_versions=exclude_alternative_versions,
        )
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
        exclude_alternative_versions: bool,
    ) -> tuple[str, tuple[BilibiliCandidate, ...]]:
        requested_query = _normalize_query(query)
        api_query = prepare_bilibili_search_query(requested_query)
        if not api_query:
            raise MusicSearchError("请提供要搜索的歌曲名称")

        candidates = await self._search_candidates(
            api_query,
            song_title=song_title,
        )
        filtered = filter_bilibili_candidates(
            requested_query,
            candidates,
            exclude_alternative_versions=exclude_alternative_versions,
            limit=SEARCH_LIMIT,
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
            bvid = str(getattr(video, "bvid", "")).strip()
            title = str(getattr(video, "title", "")).strip()
            uploader = str(getattr(video, "uploader", "")).strip()
            if not bvid or not title:
                continue
            pages = _select_pages(getattr(video, "pages", ()), song_title)
            for page in pages:
                cid = _positive_int(getattr(page, "cid", 0))
                duration_ms = _nonnegative_int(getattr(page, "duration_ms", 0))
                if not cid or duration_ms > MAX_MEDIA_DURATION_MS:
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
                            category=str(getattr(search_hit, "category", "")).strip(),
                            search_title=str(getattr(search_hit, "title", "")).strip(),
                        )
                    )
                except ValueError:
                    continue
                if len(candidates) >= BILIBILI_PAGE_LIMIT:
                    return tuple(candidates)
        return tuple(candidates)


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

    async def deliver(self, candidate: BilibiliCandidate) -> DeliveryResult:
        if candidate.duration_ms > MAX_MEDIA_DURATION_MS:
            raise DeliveryError("歌曲时长超过 15 分钟，无法发送")
        if not self._media.ffmpeg_available:
            raise FfmpegUnavailableError("宿主机未安装 ffmpeg，暂时无法听歌或下载歌曲")
        try:
            audio = await self._bilibili.resolve_audio(candidate.bvid, candidate.cid)
            if audio.duration_ms and audio.duration_ms > MAX_MEDIA_DURATION_MS:
                raise DeliveryError("Bilibili 音频时长超过 15 分钟，无法发送")
            media = await self._media.prepare(
                audio,
                filename_stem=_display_stem(candidate),
            )
        except FfmpegUnavailableError:
            raise
        except DeliveryError:
            raise
        except MediaError as exc:
            raise DeliveryError("Bilibili 音频下载失败") from exc
        except Exception as exc:
            raise DeliveryError("Bilibili 未返回可播放音频") from exc
        return DeliveryResult(candidate=candidate, media=media)


def format_search_results(snapshot: SearchSnapshot) -> str:
    """Produce the only user-visible catalogue rendering used by command flow."""

    lines = [f"Bilibili 搜索结果：{snapshot.query}"]
    for position, candidate in enumerate(snapshot.candidates, start=1):
        lines.append(
            f"{position}. {candidate.display_title} ({_duration_text(candidate.duration_ms)})"
        )
    lines.append("回复序号听歌；回复“序号 下载”发送文件。")
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


def _display_stem(candidate: BilibiliCandidate) -> str:
    uploader = candidate.uploader or "Bilibili"
    return f"{candidate.display_title} - {uploader}"


def _duration_text(duration_ms: int) -> str:
    seconds = max(0, duration_ms) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


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
