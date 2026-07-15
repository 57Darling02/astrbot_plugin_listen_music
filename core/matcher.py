"""Order-preserving safety filters for playable Bilibili candidates.

Bilibili already owns query recall and result ordering.  This module does not
try to infer which title is *closest* to a user request: doing so duplicates a
search engine with much less context, and makes aliases or simplified versus
traditional Chinese needlessly fragile.  It only removes pages that cannot be
delivered or are clearly incompatible with the requested recording.

The canonical public function is :func:`filter_bilibili_candidates`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import re
import unicodedata

from .models import BilibiliCandidate


MAX_DELIVERABLE_DURATION_MS = 15 * 60 * 1000
"""The largest page duration the delivery layer is willing to materialize."""


@dataclass(frozen=True, slots=True)
class _FilterIntent:
    """Only the version constraints relevant after Bilibili has searched."""

    requested_versions: frozenset[str]
    exclude_alternative_versions: bool


def filter_bilibili_candidates(
    query: str,
    candidates: Iterable[BilibiliCandidate],
    *,
    exclude_alternative_versions: bool = False,
    limit: int = 5,
) -> tuple[BilibiliCandidate, ...]:
    """Return safely playable candidates in their original platform order.

    ``query`` is consulted only for explicit recording variants such as
    ``Live`` or ``Remix``.  It is never compared to titles, uploaders, or page
    names, so a legitimate result cannot be rejected merely because its title
    uses an alias, different script, or extra contextual wording.

    Manual search normally leaves alternative versions in the list.  Direct
    listening passes ``exclude_alternative_versions=True`` to avoid silently
    playing a clearly labelled cover, AI rendition, live recording, remix,
    instrumental, altered-speed version, or edit.  An explicitly requested
    variant is always an exact compatibility constraint.
    """

    if limit < 1:
        return ()

    intent = _parse_filter_intent(
        query,
        exclude_alternative_versions=exclude_alternative_versions,
    )
    accepted: list[BilibiliCandidate] = []
    seen_ids: set[str] = set()

    for candidate in candidates:
        if candidate.candidate_id in seen_ids:
            continue
        seen_ids.add(candidate.candidate_id)

        if not _is_acceptable_candidate(intent, candidate):
            continue
        accepted.append(candidate)
        if len(accepted) >= limit:
            break

    return tuple(accepted)


def prepare_bilibili_search_query(query: str) -> str:
    """Remove request syntax and source-distorting noise from a search term.

    Original-recording markers describe a local delivery constraint, not a
    Bilibili query supplement.  In particular, this function never adds
    ``原版`` (or an equivalent) to a search keyword.
    """

    return _clean_bilibili_search_terms(query)


def _parse_filter_intent(
    query: str,
    *,
    exclude_alternative_versions: bool,
) -> _FilterIntent:
    normalised = _normalise_spaces(query)
    return _FilterIntent(
        requested_versions=_version_categories(normalised),
        exclude_alternative_versions=(
            exclude_alternative_versions
            or any(pattern.search(normalised) for pattern in _ORIGINAL_MARKERS)
        ),
    )


def _is_acceptable_candidate(
    intent: _FilterIntent, candidate: BilibiliCandidate
) -> bool:
    if not _has_deliverable_duration(candidate.duration_ms):
        return False
    if _is_clearly_non_music(candidate):
        return False

    candidate_versions = _version_categories(candidate.match_text)
    return _versions_are_compatible(intent, candidate_versions)


def _has_deliverable_duration(duration_ms: int) -> bool:
    return 0 < duration_ms <= MAX_DELIVERABLE_DURATION_MS


def _versions_are_compatible(
    intent: _FilterIntent, candidate_versions: frozenset[str]
) -> bool:
    if intent.requested_versions:
        # A request for a live recording must not silently become a live cover,
        # and a remix must not become an instrumental remix.
        return candidate_versions == intent.requested_versions
    if intent.exclude_alternative_versions:
        return not candidate_versions
    return True


def _is_clearly_non_music(candidate: BilibiliCandidate) -> bool:
    text = _normalise_spaces(candidate.match_text)
    return any(marker.search(text) for marker in _NON_MUSIC_MARKERS) or (
        _normalise_category(candidate.category) in _NON_MUSIC_CATEGORIES
    )


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


def _version_categories(value: str) -> frozenset[str]:
    text = _normalise_spaces(value)
    return frozenset(
        category
        for category, markers in _VERSION_MARKERS.items()
        if any(marker.search(text) for marker in markers)
    )


def _normalise_spaces(value: str) -> str:
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value).casefold()).strip()


def _normalise_category(value: str) -> str:
    return _normalise_spaces(value).replace(" ", "")


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
_VERSION_MARKERS: dict[str, tuple[re.Pattern[str], ...]] = {
    "live": _markers(
        r"(?<![a-z])live(?![a-z])",
        r"现场",
        r"演唱会",
        r"concert",
        r"舞台",
    ),
    "cover": _markers(r"(?<![a-z])cover(?![a-z])", r"翻唱", r"翻弹", r"翻声"),
    "remix": _markers(
        r"(?<![a-z])remix(?![a-z])",
        r"(?<![a-z])dj(?:版|mix|remix)?(?![a-z])",
        r"混音",
        r"改编",
        r"mashup",
    ),
    "instrumental": _markers(
        r"(?<![a-z])instrumental(?![a-z])",
        r"(?<![a-z])inst(?![a-z])",
        r"伴奏",
        r"纯音乐",
        r"karaoke",
        r"卡拉\s*ok",
    ),
    "altered": _markers(
        r"nightcore",
        r"sped\s*up",
        r"slowed",
        r"加速",
        r"降速",
        r"慢速",
        r"升调",
        r"降调",
        r"变调",
    ),
    "edit": _markers(
        r"(?<![a-z])demo(?![a-z])",
        r"试听",
        r"片段",
        r"剪辑",
        r"混剪",
        r"short\s*(?:ver|version)",
    ),
    "ai": _markers(
        r"(?<![a-z0-9])ai(?:\s|[-_])*(?:翻唱|翻声|演唱|歌声|cover|voice|"
        r"生成|合成|版本?|版)?(?![a-z0-9])",
        r"人工智能(?:翻唱|翻声|演唱|歌声|生成|合成|版本?|版)?",
    ),
}
_NON_MUSIC_MARKERS = _markers(
    r"教程",
    r"教学",
    r"讲解",
    r"解说",
    r"解析",
    r"反应(?:视频)?",
    r"(?<![a-z])reaction(?![a-z])",
    r"手书",
    r"动态歌词",
    r"歌词排版",
    r"可视化歌词",
)

# This is intentionally a narrow negative list, rather than a list of music
# categories.  Bilibili permits legitimate music uploads under many category
# labels, while these labels have a clear non-music meaning for a song result.
# Unknown categories remain eligible and Bilibili's ordering is untouched.
_NON_MUSIC_CATEGORIES = frozenset(
    {
        "游戏",
        "单机游戏",
        "网络游戏",
        "手机游戏",
        "桌游棋牌",
        "知识",
        "社科·法律·心理",
        "人文历史",
        "科学科普",
        "财经商业",
        "职业职场",
        "科技",
        "数码",
        "软件应用",
        "计算机技术",
        "汽车",
        "汽车生活",
        "摩托车",
        "美食",
        "美食制作",
        "美食侦探",
        "美食测评",
        "动物",
        "喵星人",
        "汪星人",
        "野生动物",
        "运动",
        "篮球",
        "足球",
        "健身",
        "情感",
    }
)
