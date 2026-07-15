"""Small, direct Bilibili client for music search and audio resolution.

The client deliberately knows nothing about AstrBot, local media files, or
search-selection policy. It exposes the protocol-shaped pieces needed by those
layers: video search, video pages, DASH audio URLs, and QR login polling.
Account persistence is intentionally delegated to the caller through
``credentials_getter``.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import inspect
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias
from urllib.parse import quote, urlencode, urlsplit

import aiohttp

from .models import ResolvedAudio


BILIBILI_WEB_ORIGIN = "https://www.bilibili.com"
BILIBILI_API_ORIGIN = "https://api.bilibili.com"
BILIBILI_PASSPORT_ORIGIN = "https://passport.bilibili.com"

_NAV_URL = f"{BILIBILI_API_ORIGIN}/x/web-interface/nav"
_SEARCH_URL = f"{BILIBILI_API_ORIGIN}/x/web-interface/wbi/search/type"
_VIEW_URL = f"{BILIBILI_API_ORIGIN}/x/web-interface/wbi/view"
_PLAY_URL = f"{BILIBILI_API_ORIGIN}/x/player/wbi/playurl"
_QR_GENERATE_URL = f"{BILIBILI_PASSPORT_ORIGIN}/x/passport-login/web/qrcode/generate"
_QR_POLL_URL = f"{BILIBILI_PASSPORT_ORIGIN}/x/passport-login/web/qrcode/poll"

_WBI_KEY_TTL_SECONDS = 10 * 60
_QR_SESSION_TTL_SECONDS = 180
_WBI_RETRY_CODES = frozenset({-403, -352})
_TARGET_AUDIO_BANDWIDTH = 192_000

# Bilibili's documented mixin permutation.  Keeping it as data makes the
# signing algorithm below easy to audit and test independently.
_MIXIN_INDICES = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    62,
    6,
    63,
    57,
    20,
    34,
    52,
    59,
    11,
    36,
    44,
)

_HTML_TAG_RE = re.compile(r"<[^>]*>")
_WHITESPACE_RE = re.compile(r"\s+")
_WBI_FILTER_RE = re.compile(r"[!'()*]")
_COOKIE_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_BVID_VALUE_RE = re.compile(r"bv[0-9A-Za-z]{10}", re.IGNORECASE)
_VIDEO_REF_TOKEN_RE = re.compile(
    r"(?<![0-9A-Za-z])(?:"
    r"(?P<bvid>bv[0-9A-Za-z]{10})|(?P<aid>av[1-9][0-9]{0,18})"
    r")(?![0-9A-Za-z])",
    re.IGNORECASE,
)

CookieMap: TypeAlias = Mapping[str, str]
CookieGetter: TypeAlias = Callable[[], CookieMap | Awaitable[CookieMap | None] | None]
QrLoginState: TypeAlias = Literal["waiting", "scanned", "confirmed", "expired"]


class BilibiliError(RuntimeError):
    """Raised when a Bilibili response cannot be used safely."""


class BilibiliApiError(BilibiliError):
    """A successful HTTP request whose Bilibili API result is an error."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message or "Bilibili API request failed"
        super().__init__(f"Bilibili API error {code}: {self.message}")


@dataclass(frozen=True, slots=True)
class BilibiliVideoRef:
    """One literal Bilibili video identifier supplied by a user.

    A reference carries either an ``aid`` or a ``bvid``.  It intentionally
    stops short of resolving AV IDs locally: callers must ask Bilibili for the
    canonical video detail before a page can be delivered.
    """

    aid: int | None = None
    bvid: str | None = None

    def __post_init__(self) -> None:
        if (self.aid is None) == (self.bvid is None):
            raise ValueError("exactly one of aid or bvid must be provided")
        if self.aid is not None:
            if (
                isinstance(self.aid, bool)
                or not isinstance(self.aid, int)
                or self.aid <= 0
            ):
                raise ValueError("aid must be a positive integer")
            return

        if not isinstance(self.bvid, str) or not _BVID_VALUE_RE.fullmatch(self.bvid):
            raise ValueError("bvid must be a Bilibili BV identifier")
        object.__setattr__(self, "bvid", f"BV{self.bvid[2:]}")


def parse_bilibili_video_ref(value: str | None) -> BilibiliVideoRef | None:
    """Extract the first literal AV or BV identifier from user-supplied text.

    This is deliberately a pure parser.  Standard ``bilibili.com/video`` URLs
    work because their literal AV/BV token is extracted; ``b23.tv`` links are
    not followed or otherwise resolved.
    """

    if not isinstance(value, str):
        return None
    match = _VIDEO_REF_TOKEN_RE.search(value)
    if match is None:
        return None
    bvid = match.group("bvid")
    if bvid is not None:
        return BilibiliVideoRef(bvid=bvid)
    aid = match.group("aid")
    return BilibiliVideoRef(aid=int(aid[2:])) if aid is not None else None


@dataclass(frozen=True, slots=True)
class BilibiliSearchVideo:
    """A video-level hit returned by Bilibili search.

    ``title`` is the exact search-hit title, before a later video-detail
    request potentially supplies a different title for the same BV.
    """

    bvid: str
    title: str


@dataclass(frozen=True, slots=True)
class BilibiliPage:
    """One playable page (P) of a Bilibili video."""

    cid: int
    index: int
    title: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class BilibiliVideo:
    """The subset of video detail needed by the music search workflow."""

    bvid: str
    title: str
    uploader: str
    pages: tuple[BilibiliPage, ...]


@dataclass(frozen=True, slots=True)
class BilibiliQrSession:
    """Opaque QR login session data safe to hand to the account WebUI."""

    poll_token: str
    qr_url: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class BilibiliQrLoginPoll:
    """A QR polling result; cookies only exist after confirmed login."""

    state: QrLoginState
    cookies: Mapping[str, str] | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class BilibiliProfile:
    """Non-sensitive account information shown in the plugin management page."""

    display_name: str


@dataclass(slots=True)
class _PendingQrLogin:
    qrcode_key: str
    cookies: dict[str, str]
    expires_at: float


@dataclass(frozen=True, slots=True)
class _DashAudioTrack:
    stream_id: int
    url: str
    backup_urls: tuple[str, ...]
    bandwidth: int
    mime_type: str


def derive_wbi_mixin_key(img_url: str, sub_url: str) -> str:
    """Derive the short WBI key published by Bilibili's navigation endpoint."""
    img_key = img_url.rsplit("/", 1)[-1].split(".", 1)[0]
    sub_key = sub_url.rsplit("/", 1)[-1].split(".", 1)[0]
    raw_key = img_key + sub_key
    mixed = "".join(raw_key[index] for index in _MIXIN_INDICES if index < len(raw_key))
    if len(mixed) < 32:
        raise BilibiliError("Bilibili returned an invalid WBI signing key")
    return mixed[:32]


def sign_wbi_params(
    params: Mapping[str, Any], mixin_key: str, *, timestamp: int | None = None
) -> dict[str, str]:
    """Return deterministically ordered WBI parameters including ``w_rid``.

    The helper is intentionally public and side-effect-free so protocol changes
    can be covered with a tiny fixture test without constructing a client.
    """
    if not mixin_key:
        raise ValueError("mixin_key must not be empty")

    canonical = {
        str(key): _WBI_FILTER_RE.sub("", str(value))
        for key, value in params.items()
        if value is not None
    }
    canonical["wts"] = str(int(time.time()) if timestamp is None else timestamp)
    ordered = dict(sorted(canonical.items()))
    query = urlencode(tuple(ordered.items()), quote_via=quote, safe="")
    ordered["w_rid"] = hashlib.md5(f"{query}{mixin_key}".encode("utf-8")).hexdigest()
    return ordered


def _clean_cookie_map(cookies: Mapping[str, str] | None) -> dict[str, str]:
    if not cookies:
        return {}
    cleaned: dict[str, str] = {}
    for raw_name, raw_value in cookies.items():
        name = str(raw_name).strip()
        value = str(raw_value).strip()
        if (
            not name
            or not value
            or not _COOKIE_NAME_RE.fullmatch(name)
            or "\r" in value
            or "\n" in value
        ):
            continue
        cleaned[name] = value
    return cleaned


def _cookie_header(cookies: Mapping[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def _response_cookies(response: aiohttp.ClientResponse) -> dict[str, str]:
    """Extract only cookie values; attributes stay in the HTTP layer."""
    return {
        name: morsel.value
        for name, morsel in response.cookies.items()
        if morsel.value and morsel["max-age"] != "0"
    }


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _strip_html(value: Any) -> str:
    text = html.unescape(str(value or ""))
    return _WHITESPACE_RE.sub(" ", _HTML_TAG_RE.sub("", text)).strip()


def _normalise_url(value: Any) -> str:
    url = str(value or "").strip()
    if url.startswith("//"):
        url = f"https:{url}"
    parsed = urlsplit(url)
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _normalise_bvid(value: str) -> str:
    bvid = value.strip()
    if not bvid:
        raise ValueError("bvid must not be empty")
    if len(bvid) > 128 or any(
        ord(character) < 32 or ord(character) == 127 for character in bvid
    ):
        raise ValueError("bvid contains invalid characters")
    return bvid


def _unique_urls(primary: str, candidates: Any) -> tuple[str, ...]:
    values = [primary]
    if isinstance(candidates, list):
        values.extend(str(item) for item in candidates)
    seen: set[str] = set()
    urls: list[str] = []
    for value in values:
        url = _normalise_url(value)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return tuple(urls)


class BilibiliClient:
    """Direct async Bilibili client sharing the plugin's ``aiohttp`` session."""

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        session: aiohttp.ClientSession,
        credentials_getter: CookieGetter | None = None,
    ) -> None:
        self._session = session
        self._credentials_getter = credentials_getter
        self._wbi_key: str | None = None
        self._wbi_key_expires_at = 0.0
        self._wbi_lock = asyncio.Lock()
        self._qr_sessions: dict[str, _PendingQrLogin] = {}
        self._qr_lock = asyncio.Lock()

    async def search_videos(
        self, query: str, *, limit: int = 20
    ) -> tuple[BilibiliSearchVideo, ...]:
        """Search videos without fetching their pages or playback URLs."""
        keyword = query.strip()
        if not keyword:
            return ()
        bounded_limit = max(1, min(limit, 50))
        data = await self._wbi_data(
            _SEARCH_URL,
            {
                "search_type": "video",
                "keyword": keyword,
                "order": "totalrank",
                "duration": 0,
                "tids": 0,
                "page": 1,
                "page_size": bounded_limit,
            },
        )
        raw_items = data.get("result")
        if not isinstance(raw_items, list):
            return ()

        videos: list[BilibiliSearchVideo] = []
        for raw in raw_items:
            if not isinstance(raw, Mapping) or raw.get("type") not in {None, "video"}:
                continue
            bvid = str(raw.get("bvid") or "").strip()
            title = _strip_html(raw.get("title"))
            if not bvid or not title:
                continue
            videos.append(
                BilibiliSearchVideo(
                    bvid=bvid,
                    title=title,
                )
            )
            if len(videos) >= bounded_limit:
                break
        return tuple(videos)

    async def get_video(self, bvid: str) -> BilibiliVideo:
        """Fetch a video's current page list for matching and audio resolution."""
        normalized_bvid = _normalise_bvid(bvid)
        data = await self._wbi_data(_VIEW_URL, {"bvid": normalized_bvid})
        return self._parse_video_detail(data, fallback_bvid=normalized_bvid)

    async def get_video_by_aid(self, aid: int) -> BilibiliVideo:
        """Fetch video detail by AV ID and retain Bilibili's canonical BV ID."""
        if isinstance(aid, bool) or not isinstance(aid, int) or aid <= 0:
            raise ValueError("aid must be a positive integer")
        data = await self._wbi_data(_VIEW_URL, {"aid": aid})
        return self._parse_video_detail(data, fallback_bvid=None)

    @staticmethod
    def _parse_video_detail(
        data: Mapping[str, Any], *, fallback_bvid: str | None
    ) -> BilibiliVideo:
        resolved_bvid = str(data.get("bvid") or fallback_bvid or "").strip()
        if not resolved_bvid:
            raise BilibiliError("Bilibili returned a video without a BV identifier")
        raw_pages = data.get("pages")
        pages: list[BilibiliPage] = []
        if isinstance(raw_pages, list):
            for raw_page in raw_pages:
                if not isinstance(raw_page, Mapping):
                    continue
                cid = _to_int(raw_page.get("cid"))
                if cid <= 0:
                    continue
                pages.append(
                    BilibiliPage(
                        cid=cid,
                        index=max(1, _to_int(raw_page.get("page"), len(pages) + 1)),
                        title=_strip_html(raw_page.get("part"))
                        or _strip_html(data.get("title")),
                        duration_ms=max(0, _to_int(raw_page.get("duration"))) * 1000,
                    )
                )
        if not pages:
            cid = _to_int(data.get("cid"))
            if cid > 0:
                pages.append(
                    BilibiliPage(
                        cid=cid,
                        index=1,
                        title=_strip_html(data.get("title")),
                        duration_ms=max(0, _to_int(data.get("duration"))) * 1000,
                    )
                )

        return BilibiliVideo(
            bvid=resolved_bvid,
            title=_strip_html(data.get("title")) or resolved_bvid,
            uploader=_strip_html(
                data.get("owner", {}).get("name")
                if isinstance(data.get("owner"), Mapping)
                else ""
            )
            or "未知上传者",
            pages=tuple(pages),
        )

    async def resolve_audio(self, bvid: str, cid: int) -> ResolvedAudio:
        """Resolve one Bilibili page to its preferred DASH audio stream.

        Normal DASH audio is preferred at or below 192 kbps.  A single
        progressive MP4 segment is retained as a narrow compatibility fallback
        when Bilibili does not expose DASH audio for that page.
        """
        normalized_bvid = _normalise_bvid(bvid)
        if cid <= 0:
            raise ValueError("cid must be positive")

        data = await self._wbi_data(
            _PLAY_URL,
            {
                "bvid": normalized_bvid,
                "cid": cid,
                "qn": 80,
                "fnval": 16,
                "fnver": 0,
                "fourk": 0,
                "platform": "pc",
            },
        )
        duration_ms = _to_int(data.get("timelength")) or None
        headers = await self.stream_headers(normalized_bvid)
        dash = data.get("dash")
        tracks = self._dash_audio_tracks(dash)
        track = self._select_dash_track(tracks)
        if track is not None:
            return ResolvedAudio(
                url=track.url,
                headers=headers,
                mime_type=track.mime_type or "audio/mp4",
                backup_urls=track.backup_urls,
                duration_ms=duration_ms,
                needs_remux=True,
            )

        progressive = self._single_progressive_track(data.get("durl"))
        if progressive is not None:
            url, backup_urls = progressive
            return ResolvedAudio(
                url=url,
                headers=headers,
                mime_type="video/mp4",
                backup_urls=backup_urls,
                duration_ms=duration_ms,
                needs_remux=True,
            )
        raise BilibiliError("Bilibili did not return a playable audio stream")

    async def stream_headers(self, bvid: str) -> dict[str, str]:
        """Build headers required when a downstream downloader fetches a stream."""
        normalized_bvid = _normalise_bvid(bvid)
        cookies = await self._credentials()
        headers = self._headers(
            referer=f"{BILIBILI_WEB_ORIGIN}/video/{normalized_bvid}/"
        )
        headers["Accept"] = "*/*"
        if cookies:
            headers["Cookie"] = _cookie_header(cookies)
        return headers

    async def start_qr_login(self) -> BilibiliQrSession:
        """Create an opaque QR session; callers should only expose its URL/token."""
        payload, response_cookies = await self._request_json(
            _QR_GENERATE_URL,
            cookies={},
            referer=f"{BILIBILI_PASSPORT_ORIGIN}/login",
        )
        data = self._api_data(payload)
        qrcode_key = str(data.get("qrcode_key") or "").strip()
        qr_url = _normalise_url(data.get("url"))
        if not qrcode_key or not qr_url:
            raise BilibiliError("Bilibili returned an invalid QR login session")

        now = time.time()
        poll_token = secrets.token_urlsafe(24)
        expires_at = now + _QR_SESSION_TTL_SECONDS
        async with self._qr_lock:
            self._discard_expired_qr_sessions(now)
            self._qr_sessions[poll_token] = _PendingQrLogin(
                qrcode_key=qrcode_key,
                cookies=dict(response_cookies),
                expires_at=expires_at,
            )
        return BilibiliQrSession(
            poll_token=poll_token,
            qr_url=qr_url,
            expires_at=expires_at,
        )

    async def poll_qr_login(self, poll_token: str) -> BilibiliQrLoginPoll:
        """Poll one opaque QR session without exposing its upstream QR key."""
        now = time.time()
        async with self._qr_lock:
            self._discard_expired_qr_sessions(now)
            pending = self._qr_sessions.get(poll_token)
            if pending is None:
                return BilibiliQrLoginPoll(state="expired", message="二维码已过期")
            request_cookies = dict(pending.cookies)

        payload, response_cookies = await self._request_json(
            _QR_POLL_URL,
            params={"qrcode_key": pending.qrcode_key},
            cookies=request_cookies,
            referer=f"{BILIBILI_PASSPORT_ORIGIN}/login",
        )
        root_code = _to_int(payload.get("code"), -1)
        if root_code != 0:
            raise BilibiliApiError(
                root_code, str(payload.get("message") or "QR login poll failed")
            )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise BilibiliError("Bilibili returned an invalid QR login status")

        code = _to_int(data.get("code"), -1)
        message = (
            str(data.get("message") or payload.get("message") or "").strip() or None
        )
        async with self._qr_lock:
            current = self._qr_sessions.get(poll_token)
            if current is None:
                return BilibiliQrLoginPoll(state="expired", message="二维码已取消")
            if current.expires_at <= time.time():
                self._qr_sessions.pop(poll_token, None)
                return BilibiliQrLoginPoll(state="expired", message="二维码已过期")
            current.cookies.update(response_cookies)
            if code == 0:
                cookies = dict(current.cookies)
                self._qr_sessions.pop(poll_token, None)
                return BilibiliQrLoginPoll(
                    state="confirmed", cookies=cookies, message=message
                )
            if code == 86090:
                return BilibiliQrLoginPoll(state="scanned", message=message)
            if code in {86101, 86102}:
                return BilibiliQrLoginPoll(state="waiting", message=message)
            # 86038 is Bilibili's documented QR expiration code.  Unexpected
            # terminal codes are treated the same so WebUI can offer a fresh QR.
            self._qr_sessions.pop(poll_token, None)
            return BilibiliQrLoginPoll(state="expired", message=message)

    async def cancel_qr_login(self, poll_token: str) -> None:
        """Forget a locally tracked QR session; Bilibili keys then expire naturally."""
        async with self._qr_lock:
            self._qr_sessions.pop(poll_token, None)

    async def profile_from_cookies(
        self, cookies: Mapping[str, str]
    ) -> BilibiliProfile | None:
        """Validate supplied credentials and return only presentation-safe profile data."""
        payload, _ = await self._request_json(_NAV_URL, cookies=cookies)
        data = payload.get("data")
        if not isinstance(data, Mapping) or not bool(data.get("isLogin")):
            return None
        display_name = _strip_html(data.get("uname"))
        if not display_name:
            return None
        return BilibiliProfile(display_name=display_name)

    async def profile(self) -> BilibiliProfile | None:
        """Return the current injected account profile, or ``None`` when anonymous."""
        return await self.profile_from_cookies(await self._credentials())

    async def _wbi_data(
        self, endpoint: str, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        for attempt in range(2):
            mixin_key = await self._get_wbi_key(force=attempt > 0)
            signed_params = sign_wbi_params(params, mixin_key)
            query = urlencode(tuple(signed_params.items()), quote_via=quote, safe="")
            payload, _ = await self._request_json(f"{endpoint}?{query}")
            code = _to_int(payload.get("code"), -1)
            if code == 0:
                return self._api_data(payload)
            if attempt == 0 and code in _WBI_RETRY_CODES:
                await self._invalidate_wbi_key()
                continue
            raise BilibiliApiError(
                code,
                str(payload.get("message") or payload.get("msg") or "request failed"),
            )
        raise BilibiliError("Bilibili WBI request could not be signed")

    async def _get_wbi_key(self, *, force: bool = False) -> str:
        now = time.monotonic()
        if not force and self._wbi_key and now < self._wbi_key_expires_at:
            return self._wbi_key
        async with self._wbi_lock:
            now = time.monotonic()
            if not force and self._wbi_key and now < self._wbi_key_expires_at:
                return self._wbi_key
            payload, _ = await self._request_json(_NAV_URL)
            data = payload.get("data")
            wbi_image = data.get("wbi_img") if isinstance(data, Mapping) else None
            if not isinstance(wbi_image, Mapping):
                raise BilibiliError("Bilibili did not return WBI signing metadata")
            key = derive_wbi_mixin_key(
                str(wbi_image.get("img_url") or ""),
                str(wbi_image.get("sub_url") or ""),
            )
            self._wbi_key = key
            self._wbi_key_expires_at = time.monotonic() + _WBI_KEY_TTL_SECONDS
            return key

    async def _invalidate_wbi_key(self) -> None:
        async with self._wbi_lock:
            self._wbi_key = None
            self._wbi_key_expires_at = 0.0

    async def _credentials(self) -> dict[str, str]:
        if self._credentials_getter is None:
            return {}
        result = self._credentials_getter()
        if inspect.isawaitable(result):
            result = await result
        return _clean_cookie_map(result if isinstance(result, Mapping) else None)

    def _headers(self, *, referer: str = f"{BILIBILI_WEB_ORIGIN}/") -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": referer,
            "User-Agent": self.USER_AGENT,
        }

    async def _request_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        cookies: Mapping[str, str] | None = None,
        referer: str = f"{BILIBILI_WEB_ORIGIN}/",
    ) -> tuple[dict[str, Any], dict[str, str]]:
        request_cookies = (
            await self._credentials() if cookies is None else _clean_cookie_map(cookies)
        )
        headers = self._headers(referer=referer)
        if request_cookies:
            headers["Cookie"] = _cookie_header(request_cookies)
        try:
            async with self._session.get(
                url, params=params, headers=headers
            ) as response:
                text = await response.text()
                if response.status >= 400:
                    raise BilibiliError(
                        f"Bilibili request failed (HTTP {response.status})"
                    )
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise BilibiliError("Bilibili returned invalid JSON") from exc
                if not isinstance(payload, dict):
                    raise BilibiliError("Bilibili returned an unexpected response")
                return payload, _response_cookies(response)
        except aiohttp.ClientError as exc:
            raise BilibiliError("Bilibili network request failed") from exc
        except asyncio.TimeoutError as exc:
            raise BilibiliError("Bilibili request timed out") from exc

    @staticmethod
    def _api_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        code = _to_int(payload.get("code"), -1)
        if code != 0:
            raise BilibiliApiError(
                code,
                str(payload.get("message") or payload.get("msg") or "request failed"),
            )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise BilibiliError("Bilibili returned an unexpected API payload")
        return data

    @staticmethod
    def _dash_audio_tracks(dash: Any) -> tuple[_DashAudioTrack, ...]:
        if not isinstance(dash, Mapping):
            return ()
        raw_tracks = dash.get("audio")
        if not isinstance(raw_tracks, list):
            return ()
        tracks: list[_DashAudioTrack] = []
        for raw_track in raw_tracks:
            if not isinstance(raw_track, Mapping):
                continue
            urls = _unique_urls(
                str(raw_track.get("baseUrl") or raw_track.get("base_url") or ""),
                raw_track.get("backupUrl") or raw_track.get("backup_url"),
            )
            if not urls:
                continue
            tracks.append(
                _DashAudioTrack(
                    stream_id=_to_int(raw_track.get("id"), 0),
                    url=urls[0],
                    backup_urls=urls[1:],
                    bandwidth=max(0, _to_int(raw_track.get("bandwidth"))),
                    mime_type=str(
                        raw_track.get("mimeType")
                        or raw_track.get("mime_type")
                        or "audio/mp4"
                    ),
                )
            )
        return tuple(tracks)

    @staticmethod
    def _select_dash_track(
        tracks: tuple[_DashAudioTrack, ...],
    ) -> _DashAudioTrack | None:
        if not tracks:
            return None
        below_target = [
            track for track in tracks if track.bandwidth <= _TARGET_AUDIO_BANDWIDTH
        ]
        if below_target:
            return max(
                below_target, key=lambda track: (track.bandwidth, track.stream_id)
            )
        return min(
            tracks, key=lambda track: (track.bandwidth or float("inf"), track.stream_id)
        )

    @staticmethod
    def _single_progressive_track(durl: Any) -> tuple[str, tuple[str, ...]] | None:
        if (
            not isinstance(durl, list)
            or len(durl) != 1
            or not isinstance(durl[0], Mapping)
        ):
            return None
        urls = _unique_urls(
            str(durl[0].get("url") or ""),
            durl[0].get("backup_url") or durl[0].get("backupUrl"),
        )
        return (urls[0], urls[1:]) if urls else None

    def _discard_expired_qr_sessions(self, now: float) -> None:
        for token, pending in tuple(self._qr_sessions.items()):
            if pending.expires_at <= now:
                self._qr_sessions.pop(token, None)
