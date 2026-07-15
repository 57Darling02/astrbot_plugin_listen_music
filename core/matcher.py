"""Small, order-preserving guards for playable Bilibili candidates.

Bilibili owns recall and ordering. Candidate title labels such as ``Live``,
``AI``, or ``MV`` are evidence for the conversation layer, not reliable local
grounds for rejecting a requested recording. This module therefore keeps only
facts that the delivery layer can verify.
"""

from __future__ import annotations

from collections.abc import Iterable
import re
import unicodedata

from .models import BilibiliCandidate


MAX_DELIVERABLE_DURATION_MS = 15 * 60 * 1000
"""The largest page duration the delivery layer is willing to materialize."""


def filter_bilibili_candidates(
    candidates: Iterable[BilibiliCandidate],
    *,
    limit: int = 5,
) -> tuple[BilibiliCandidate, ...]:
    """Return unique, deliverable pages in their original platform order.

    The filter deliberately does not interpret titles or categories.  Version
    preference is conversational context that the LLM can evaluate with the
    complete candidate list; local code only rejects pages that cannot be
    delivered under the fixed duration boundary.
    """

    if limit < 1:
        return ()

    accepted: list[BilibiliCandidate] = []
    seen_ids: set[str] = set()
    for candidate in candidates:
        if candidate.candidate_id in seen_ids:
            continue
        seen_ids.add(candidate.candidate_id)
        if not _has_deliverable_duration(candidate.duration_ms):
            continue
        accepted.append(candidate)
        if len(accepted) >= limit:
            break
    return tuple(accepted)


def prepare_bilibili_search_query(query: str) -> str:
    """Remove request syntax and source-distorting noise from a search term.

    Original-recording markers describe a local delivery preference, not a
    Bilibili query supplement.  In particular, this function never adds
    ``原版`` (or an equivalent) to a search keyword.
    """

    return _clean_bilibili_search_terms(query)


def _has_deliverable_duration(duration_ms: int) -> bool:
    return 0 < duration_ms <= MAX_DELIVERABLE_DURATION_MS


def _strip_query_noise(query: str) -> str:
    value = query
    while True:
        stripped = _QUERY_PREFIX.sub("", value, count=1)
        if stripped == value:
            return value
        value = stripped


def _clean_bilibili_search_terms(query: str) -> str:
    """Remove request syntax without changing the requested work identity."""

    cleaned = _strip_query_noise(_normalise_spaces(query))
    cleaned = _remove_patterns(cleaned, _ORIGINAL_MARKERS)
    cleaned = _QUERY_QUALITY_MARKERS.sub(" ", cleaned)
    return _normalise_spaces(cleaned)


def _remove_patterns(value: str, patterns: tuple[re.Pattern[str], ...]) -> str:
    for pattern in patterns:
        value = pattern.sub(" ", value)
    return value


def _normalise_spaces(value: str) -> str:
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value).casefold()).strip()


def _markers(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


_WHITESPACE = re.compile(r"\s+")
_QUERY_PREFIX = re.compile(
    r"^\s*(?:(?:请(?:帮我)?|帮我)?(?:播放(?:一下)?(?:歌曲?|音乐)?|点歌|点(?:一)?首(?:歌曲?)?|"
    r"听(?:一下)?(?:歌曲?|音乐)?|下载(?:一)?首?(?:歌曲?)?)|"
    r"(?:我要|我想)(?:听(?:一下)?(?:歌曲?|音乐)?|播放(?:一下)?(?:歌曲?|音乐)?|"
    r"下载(?:一)?首?(?:歌曲?)?)?|来(?:一)?首(?:歌曲?)?)\s*",
    re.IGNORECASE,
)
_QUERY_QUALITY_MARKERS = re.compile(
    r"(?<![a-z0-9])(?:\d{3,4}p|\d{2,3}k(?:bps)?|flac|mp3|hi[ -]?res|hq|sq)(?![a-z0-9])|"
    r"无损|高音质|完整版",
    re.IGNORECASE,
)
_ORIGINAL_MARKERS = _markers(
    r"原版",
    r"原唱",
    r"原曲",
    r"(?<![a-z])original(?:\s+version)?(?![a-z])",
)
