"""AstrBot entry point for the intentionally small listen-music plugin."""

import asyncio
import base64
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
import io
import json
import re
import time
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.message_components import File, Record
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request, stream_response
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.session_waiter import (
    FILTERS,
    SessionController,
    SessionFilter,
    SessionWaiter,
)

from .core.accounts import (
    AccountError,
    AccountService,
    BilibiliAuthenticator,
    CredentialStore,
    LoginSessionAccessDenied,
    LoginSessionNotFound,
    QrLoginPoll,
    QrLoginStart,
)
from .core.bilibili import BilibiliClient
from .core.media import FfmpegUnavailableError, MediaError, MediaStore
from .core.models import BilibiliCandidate, SearchSnapshot
from .core.selection import SearchSnapshotStore
from .core.services import (
    SEARCH_LIMIT,
    DeliveryError,
    DeliveryResult,
    DeliveryService,
    MusicSearchError,
    SearchService,
    format_search_results,
    summarize_search_candidates,
)


PLUGIN_NAME = "astrbot_plugin_listen_music"
INTERACTION_TIMEOUT_SECONDS = 90
SEARCH_SNAPSHOT_TTL_SECONDS = 300.0
SEARCH_SNAPSHOT_MAX_ENTRIES = 1024
MAX_DELIVERY_NOTE_LENGTH = 120
# AstrBot's weixin_oc adapter accepts File outbound but ignores Record.
_VOICE_AS_FILE_PLATFORMS = frozenset({"weixin_oc"})
_CANONICAL_RECORDING_PREFERENCES = frozenset({"原版", "原唱", "original"})
_CHINESE_SELECTION_POSITIONS = tuple("一二三四五六七八九十")
if SEARCH_LIMIT > len(_CHINESE_SELECTION_POSITIONS):
    raise RuntimeError("selection grammar needs more Chinese position names")
_SELECTION_POSITION_MAP = {
    **{str(position): position for position in range(1, SEARCH_LIMIT + 1)},
    **{
        character: position
        for position, character in enumerate(
            _CHINESE_SELECTION_POSITIONS[:SEARCH_LIMIT], start=1
        )
    },
}
_SELECTION_POSITION_PATTERN = "|".join(
    sorted(
        (re.escape(value) for value in _SELECTION_POSITION_MAP), key=len, reverse=True
    )
)
_SELECTION_RE = re.compile(
    r"^(?:(?:我|我要|我想|帮我|请)\s*)?"
    r"(?:(下载|听(?:歌)?|播放)\s*)?"
    r"(?:(?:选择|选)\s*)?"
    rf"(?:第\s*)?({_SELECTION_POSITION_PATTERN})\s*(?:首(?:歌)?|个|号)?"
    r"(?:\s*(下载|听(?:歌)?|播放))?"
    r"(?:[，,。！？!]\s*)*$"
)
_DELIVERY_NOTE_REJECTED_MARKERS = (
    "正在找",
    "帮你找",
    "我帮你找",
    "找到了",
    "找到后",
    "搜索",
    "查找",
    "正在准备",
    "处理中",
    "正在处理",
    "下载中",
    "检索",
    "匹配",
    "关键词",
    "换用",
    "重试",
    "已送上",
    "播放",
    "发送",
)


class _DeliveryMode(str, Enum):
    """The two AstrBot transport forms for an already-prepared audio file."""

    VOICE = "voice"
    DOWNLOAD = "download"


@dataclass(frozen=True, slots=True)
class _MusicRequest:
    """The structured song identity supplied by one LLM tool call."""

    title: str
    artist: str | None = None
    version: str | None = None

    @classmethod
    def from_fields(
        cls,
        title: object,
        *,
        artist: object | None = None,
        version: object | None = None,
    ) -> "_MusicRequest":
        normalized_title = _normalize_music_request_field(title)
        if not normalized_title:
            raise ValueError("请提供歌曲名称")
        normalized_artist = _normalize_music_request_field(artist)
        normalized_version = _normalize_music_request_field(version)
        return cls(
            title=normalized_title,
            artist=normalized_artist or None,
            version=normalized_version or None,
        )

    @property
    def query(self) -> str:
        """Keep the source query construction in the plugin, not the LLM."""

        parts = [self.title]
        if self.artist:
            parts.append(self.artist)
        if self.version and not self.prefers_canonical_recording:
            parts.append(self.version)
        return " ".join(parts)

    @property
    def prefers_canonical_recording(self) -> bool:
        return bool(self.version and _is_canonical_recording_preference(self.version))


def _normalize_music_request_field(value: object | None) -> str:
    return " ".join(("" if value is None else str(value)).replace("\x00", "").split())


def _is_canonical_recording_preference(version: str) -> bool:
    return "".join(version.casefold().split()) in _CANONICAL_RECORDING_PREFERENCES


def _tool_error(message: str) -> str:
    """Return a compact structured failure to the LLM without chat side effects."""

    return json.dumps(
        {"status": "error", "message": message},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _llm_candidate_result(snapshot: SearchSnapshot) -> str:
    """Serialize only the selection evidence the model needs for one choice."""

    return json.dumps(
        {
            "status": "candidates",
            "search_id": snapshot.search_id,
            "candidates": [
                {
                    "position": candidate.position,
                    "title": candidate.title,
                    "duration": candidate.duration,
                    "search_title": candidate.search_title,
                    "page_title": candidate.page_title,
                }
                for candidate in summarize_search_candidates(snapshot)
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _parse_candidate_position(value: object) -> int:
    """Accept only a JSON integer inside the active candidate range."""

    if isinstance(value, int) and not isinstance(value, bool):
        position = value
    elif isinstance(value, str) and value.strip().isdigit():
        position = int(value.strip())
    else:
        raise ValueError("候选序号无效，请重新搜索")
    if not 1 <= position <= SEARCH_LIMIT:
        raise ValueError("候选序号无效，请重新搜索")
    return position


@dataclass(slots=True)
class _SelectionWait:
    """The active host waiter for one chat while a user chooses a result."""

    controller: SessionController
    finished: asyncio.Event
    task: asyncio.Task[None] | None = None
    cancelled: bool = False


@dataclass(slots=True)
class _LlmSearch:
    """A cancellable, short-lived authorization for one hidden candidate set."""

    expires_at: float
    search_id: str | None = None


class _SelectionSessionFilter(SessionFilter):
    """Route only an active result-selection reply into AstrBot's waiter."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        return self._session_id

    def filter(self, event: AstrMessageEvent) -> str:
        if event.unified_msg_origin != self._session_id:
            return ""
        message = " ".join(event.message_str.split())
        if _is_selection_reply(message):
            return self._session_id
        return ""


class _DiscardSelectionWaitFilter(CustomFilter):
    """Activate only for a new message that supersedes an active selection."""

    def filter(self, event: AstrMessageEvent, _config: Any) -> bool:
        message = " ".join(event.message_str.split())
        if _is_selection_reply(message):
            return False
        return any(
            isinstance(session_filter, _SelectionSessionFilter)
            and session_filter.session_id == event.unified_msg_origin
            for session_filter in tuple(FILTERS)
        )


def _music_request_parameters() -> dict[str, Any]:
    """Build the shared, small structured-intent contract for LLM tools."""

    return {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "具体歌曲名；“唱首歌”时必须先选定一首具体歌曲。",
            },
            "artist": {
                "type": "string",
                "description": "歌手、乐队或作品演者；没有明确线索时省略。",
            },
            "version": {
                "type": "string",
                "description": "仅填写用户明确指定的版本偏好；未指定时省略，不要自行添加。",
            },
        },
        "required": ["title"],
        "additionalProperties": False,
    }


def _tool_event(context: Any) -> AstrMessageEvent | None:
    event = getattr(getattr(context, "context", None), "event", None)
    return event if isinstance(event, AstrMessageEvent) else None


class FindMusicTool(FunctionTool):
    """Return a bounded, private candidate set for one direct listening request."""

    def __init__(self, plugin: "ListenMusicPlugin") -> None:
        super().__init__(
            name="find_music",
            description=(
                "仅用于直接听歌前的隐藏检索。传入整理后的具体歌名、歌手和版本偏好；"
                "“唱首歌”时先选定一首再调用。本工具返回当前会话的候选给你判断，"
                "不向用户发送候选，也不发送音乐。成功后必须仅从返回的 search_id 和 position 中选择，"
                "紧接着调用 deliver_music 发送语音。不要用于用户明确搜索、找歌或下载，也不要输出过程文字。"
            ),
            parameters=_music_request_parameters(),
        )
        self._plugin = plugin

    async def call(self, context: Any, **kwargs: Any) -> str:
        event = _tool_event(context)
        if event is None:
            return _tool_error("无法获取当前聊天会话。")
        return await self._plugin.find_music_for_llm(
            event,
            kwargs.get("title", ""),
            artist=kwargs.get("artist"),
            version=kwargs.get("version"),
        )


class DeliverMusicTool(FunctionTool):
    """Terminally send voice for an LLM-selected candidate from a live snapshot."""

    def __init__(self, plugin: "ListenMusicPlugin") -> None:
        super().__init__(
            name="deliver_music",
            description=(
                "仅在 find_music 成功后使用。只能使用其返回的 search_id 和 position，"
                "发送语音并结束本轮音乐流程。不要用于下载，不要编造序号或 search_id，"
                "调用前后不要输出过程、解释、确认文字或其他工具调用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "search_id": {
                        "type": "string",
                        "description": "find_music 返回的当前会话 search_id。",
                    },
                    "position": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": SEARCH_LIMIT,
                        "description": "find_music 返回的候选序号。",
                    },
                    "note": {
                        "type": "string",
                        "maxLength": MAX_DELIVERY_NOTE_LENGTH,
                        "description": "可选的一句自然预告，不重复歌名，不写检索过程或发送后的确认。",
                    },
                },
                "required": ["search_id", "position"],
                "additionalProperties": False,
            },
        )
        self._plugin = plugin

    async def call(self, context: Any, **kwargs: Any) -> None:
        event = _tool_event(context)
        if event is None:
            return None
        return await self._plugin.deliver_music_for_llm(
            event,
            kwargs.get("search_id", ""),
            kwargs.get("position"),
            note=kwargs.get("note"),
        )


class SearchMusicTool(FunctionTool):
    """Terminally show a user-owned candidate list for search or download."""

    def __init__(self, plugin: "ListenMusicPlugin") -> None:
        super().__init__(
            name="search_music",
            description=(
                "仅用于用户明确搜索、找歌或下载歌曲。工具会直接展示候选，用户自行回复序号听歌，"
                "或回复“序号 下载”收文件。下载必须调用本工具，不能自动选择下载版本。"
                "这是终止型工具：调用前后不要输出过程、解释或补充文字。"
            ),
            parameters=_music_request_parameters(),
        )
        self._plugin = plugin

    async def call(self, context: Any, **kwargs: Any) -> None:
        event = _tool_event(context)
        if event is None:
            return None
        return await self._plugin.present_music_search_for_llm(
            event,
            kwargs.get("title", ""),
            artist=kwargs.get("artist"),
            version=kwargs.get("version"),
        )


class ListenMusicPlugin(Star):
    """Bilibili 单源搜索、语音听歌和文件下载。"""

    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context, config)
        self._http: aiohttp.ClientSession | None = None
        self._accounts: AccountService | None = None
        self._bilibili: BilibiliClient | None = None
        self._media: MediaStore | None = None
        self._search: SearchService | None = None
        self._delivery: DeliveryService | None = None
        self._selection_waits: dict[str, _SelectionWait] = {}
        self._llm_searches: dict[str, _LlmSearch] = {}
        self._selection_lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        connector = aiohttp.TCPConnector(limit=16, limit_per_host=8, ttl_dns_cache=300)
        http = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=180, connect=15, sock_read=90),
        )
        try:
            accounts = AccountService(
                CredentialStore(data_dir / "accounts.json"),
                BilibiliAuthenticator(
                    start=self._start_bilibili_login,
                    poll=self._poll_bilibili_login,
                    cancel=self._cancel_bilibili_login,
                    profile=self._bilibili_profile,
                ),
            )
            await accounts.restore_credentials()
            bilibili = BilibiliClient(
                http,
                credentials_getter=accounts.cookies,
            )
            media = MediaStore(http, data_dir / "media")
            await media.reclaim_stale()
            search = SearchService(
                bilibili,
                SearchSnapshotStore(
                    ttl_seconds=SEARCH_SNAPSHOT_TTL_SECONDS,
                    max_entries=SEARCH_SNAPSHOT_MAX_ENTRIES,
                ),
            )

            self._http = http
            self._accounts = accounts
            self._bilibili = bilibili
            self._media = media
            self._search = search
            self._delivery = DeliveryService(
                bilibili=bilibili,
                media=media,
            )
            self._register_account_routes()
            self.context.add_llm_tools(
                FindMusicTool(self),
                DeliverMusicTool(self),
                SearchMusicTool(self),
            )
            self._initialized = True
            logger.info("listen-music plugin initialized")
        except Exception:
            await http.close()
            raise

    async def terminate(self) -> None:
        media, accounts, http = self._media, self._accounts, self._http
        self._initialized = False
        self._unregister_account_routes()
        async with self._selection_lock:
            selection_waits = tuple(self._selection_waits.values())
            self._selection_waits.clear()
            self._llm_searches.clear()
        for selection in selection_waits:
            selection.cancelled = True
            selection.controller.stop()
        selection_tasks = tuple(
            selection.task
            for selection in selection_waits
            if selection.task is not None
            and selection.task is not asyncio.current_task()
        )
        if selection_tasks:
            await asyncio.gather(*selection_tasks, return_exceptions=True)
        self._delivery = None
        self._search = None
        self._media = None
        self._accounts = None
        self._bilibili = None
        self._http = None
        if media is not None:
            await media.aclose()
        if accounts is not None:
            await accounts.aclose()
        if http is not None and not http.closed:
            await http.close()

    @filter.command("搜索歌曲")
    async def search_song(self, event: AstrMessageEvent, query: GreedyStr):
        """搜索歌曲 <关键词>"""
        # This command owns the visible catalogue and the following selection.
        event.should_call_llm(True)
        query = query.strip()
        if not query:
            yield event.plain_result("请使用：搜索歌曲 <关键词>")
            event.stop_event()
            return
        session_id = event.unified_msg_origin
        self._clear_llm_search(session_id)
        await self._cancel_selection_wait(session_id)
        try:
            snapshot = await self._require_search().search(
                session_id=session_id,
                query=query,
            )
        except MusicSearchError as exc:
            yield event.plain_result(str(exc))
            event.stop_event()
            return
        except Exception:
            logger.exception("listen-music command search failed")
            yield event.plain_result("搜索歌曲时发生错误，请稍后重试")
            event.stop_event()
            return

        if not await self._start_selection_wait(event, snapshot):
            yield event.plain_result("插件正在停止，无法继续选歌。")
            event.stop_event()
            return
        yield event.plain_result(format_search_results(snapshot))
        event.stop_event()

    async def present_music_search_for_llm(
        self,
        event: AstrMessageEvent,
        title: object,
        *,
        artist: object | None = None,
        version: object | None = None,
    ) -> None:
        """Show a manual candidate list and hand the next reply to SessionWaiter."""
        session_id = event.unified_msg_origin
        self._clear_llm_search(session_id)
        try:
            await self._cancel_selection_wait(session_id)
            music = _MusicRequest.from_fields(
                title,
                artist=artist,
                version=version,
            )
            snapshot = await self._require_search().search(
                session_id=session_id,
                query=music.query,
                song_title=music.title,
                exclude_alternative_versions=music.prefers_canonical_recording,
            )
            if not await self._start_selection_wait(event, snapshot):
                await self._send_llm_tool_failure(event, "插件正在停止，无法继续选歌。")
                return None
            try:
                await event.send(event.plain_result(format_search_results(snapshot)))
            except Exception:
                await self._cancel_selection_wait(event.unified_msg_origin)
                raise
        except (MusicSearchError, ValueError) as exc:
            await self._send_llm_tool_failure(event, str(exc))
        except Exception:
            logger.exception("listen-music LLM search failed")
            await self._send_llm_tool_failure(event, "搜索歌曲时发生错误，请稍后重试。")
        return None

    async def find_music_for_llm(
        self,
        event: AstrMessageEvent,
        title: object,
        *,
        artist: object | None = None,
        version: object | None = None,
    ) -> str:
        """Create a one-shot candidate snapshot for LLM-side direct selection."""
        session_id = event.unified_msg_origin
        lease = self._begin_llm_search(session_id)
        try:
            await self._cancel_selection_wait(session_id)
            if not self._is_current_llm_search(session_id, lease):
                return _tool_error("歌曲请求已被新的消息替换")
            music = _MusicRequest.from_fields(
                title,
                artist=artist,
                version=version,
            )
            snapshot = await self._require_search().search(
                session_id=session_id,
                query=music.query,
                song_title=music.title,
                exclude_alternative_versions=True,
            )
            if not self._complete_llm_search(session_id, lease, snapshot):
                return _tool_error("歌曲请求已被新的消息替换")
            return _llm_candidate_result(snapshot)
        except asyncio.CancelledError:
            self._discard_llm_search(session_id, lease)
            raise
        except (MusicSearchError, ValueError) as exc:
            self._discard_llm_search(session_id, lease)
            return _tool_error(str(exc))
        except Exception:
            self._discard_llm_search(session_id, lease)
            logger.exception("listen-music LLM candidate search failed")
            return _tool_error("搜索歌曲时发生错误，请稍后重试。")

    async def deliver_music_for_llm(
        self,
        event: AstrMessageEvent,
        search_id: object,
        position: object,
        *,
        note: object | None = None,
    ) -> None:
        """Terminally deliver one candidate from the active LLM search snapshot."""
        session_id = event.unified_msg_origin
        try:
            normalized_search_id = str(search_id).strip()
            selected_position = _parse_candidate_position(position)
            lease = self._active_llm_search(session_id, normalized_search_id)
            if lease is None:
                raise DeliveryError("候选已失效，请重新搜索")

            snapshot = self._require_search().snapshot(
                search_id=normalized_search_id,
                session_id=session_id,
            )
            if snapshot is None:
                self._discard_llm_search(session_id, lease)
                raise DeliveryError("候选无效或已过期，请重新搜索")
            candidate = snapshot.candidate_at(selected_position)
            if candidate is None:
                raise DeliveryError("候选无效或已过期，请重新搜索")
            if not self._consume_llm_search(session_id, lease):
                raise DeliveryError("候选已失效，请重新搜索")
            await self._deliver_with_preface(
                event,
                candidate=candidate,
                action=_DeliveryMode.VOICE,
                preface=_delivery_preface(candidate, _DeliveryMode.VOICE, note),
            )
        except (
            ValueError,
            DeliveryError,
            FfmpegUnavailableError,
            MediaError,
        ) as exc:
            await self._send_llm_tool_failure(event, str(exc))
        except Exception:
            logger.exception("listen-music LLM candidate delivery failed")
            await self._send_llm_tool_failure(event, "歌曲发送失败，请稍后重试。")
        return None

    @filter.event_message_type(filter.EventMessageType.ALL, priority=11)
    async def discard_llm_search_on_new_message(self, event: AstrMessageEvent) -> None:
        """A new chat message must not revive a prior hidden model selection."""

        self._clear_llm_search(event.unified_msg_origin)

    @filter.custom_filter(_DiscardSelectionWaitFilter)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def discard_selection_wait_on_new_message(
        self, event: AstrMessageEvent
    ) -> None:
        """Discard an interrupted manual selection without claiming the message."""

        if _is_selection_reply(" ".join(event.message_str.split())):
            return
        await self._cancel_selection_wait(event.unified_msg_origin)
        self._clear_llm_search(event.unified_msg_origin)

    async def _send_llm_tool_failure(
        self, event: AstrMessageEvent, message: str
    ) -> None:
        """End a terminal tool call after the plugin has shown its own error."""

        try:
            await event.send(event.plain_result(message))
        except Exception:
            logger.exception("listen-music failed to send LLM tool failure")

    async def _deliver_with_preface(
        self,
        event: AstrMessageEvent,
        *,
        candidate: BilibiliCandidate,
        action: _DeliveryMode,
        preface: str,
    ) -> None:
        """Overlap a user-visible preface with preparation of the selected media."""

        preparation = asyncio.create_task(
            self._require_delivery().deliver(candidate),
            name=f"listen-music-delivery-{candidate.candidate_id}",
        )
        handed_to_sender = False
        try:
            # Give the delivery task one event-loop turn to start resolving the
            # stream before the outbound preface begins its own network work.
            await asyncio.sleep(0)
            prepared: DeliveryResult | None = None
            if preparation.done():
                prepared = preparation.result()

            await event.send(event.plain_result(preface))
            if prepared is None:
                prepared = await preparation
            handed_to_sender = True
            await self._send_delivery(event, prepared, action)
        except BaseException:
            if not handed_to_sender:
                await self._discard_delivery_preparation(preparation)
            raise

    async def _discard_delivery_preparation(
        self, preparation: asyncio.Task[DeliveryResult]
    ) -> None:
        """Cancel or release a prepared item when its preface cannot be sent."""

        if not preparation.done():
            preparation.cancel()
        try:
            prepared = await preparation
        except asyncio.CancelledError:
            return
        except Exception:
            return
        await self._require_media().release(prepared.media)

    async def _start_selection_wait(
        self, event: AstrMessageEvent, snapshot: SearchSnapshot
    ) -> bool:
        """Register the only selection path for a search result in this chat."""

        session_id = event.unified_msg_origin
        selection_filter = _SelectionSessionFilter(session_id)
        waiter = SessionWaiter(selection_filter, session_id, False)
        selection = _SelectionWait(waiter.session_controller, asyncio.Event())
        if not await self._replace_selection_wait(session_id, selection):
            return False

        # SessionWaiter registers its session inside register_wait(), while the
        # global filter list is intentionally owned here so replacement is safe.
        FILTERS.append(selection_filter)
        selection.task = asyncio.create_task(
            self._run_selection_wait(
                source_event=event,
                snapshot=snapshot,
                selection_filter=selection_filter,
                waiter=waiter,
                selection=selection,
            ),
            name=f"listen-music-selection-{snapshot.search_id}",
        )
        # Ensure SessionWaiter has registered before the result list can reach
        # a user able to reply immediately.
        await asyncio.sleep(0)
        return True

    async def _run_selection_wait(
        self,
        *,
        source_event: AstrMessageEvent,
        snapshot: SearchSnapshot,
        selection_filter: _SelectionSessionFilter,
        waiter: SessionWaiter,
        selection: _SelectionWait,
    ) -> None:
        async def select_song(
            controller: SessionController, reply: AstrMessageEvent
        ) -> None:
            await self._deliver_selection(controller, reply, snapshot)

        try:
            await waiter.register_wait(select_song, timeout=INTERACTION_TIMEOUT_SECONDS)
        except TimeoutError:
            if not selection.cancelled and self._initialized:
                await source_event.send(
                    source_event.plain_result("没有收到选歌回复，本次搜索已结束。")
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("listen-music selection wait failed")
            if self._initialized:
                await source_event.send(
                    source_event.plain_result("选歌流程已结束，请重新搜索。")
                )
        finally:
            try:
                FILTERS.remove(selection_filter)
            except ValueError:
                pass
            self._finish_selection_wait(snapshot.session_id, selection)

    async def _deliver_selection(
        self,
        controller: SessionController,
        reply: AstrMessageEvent,
        snapshot: SearchSnapshot,
    ) -> None:
        """Resolve the next real user reply against its original snapshot."""

        try:
            if " ".join(reply.message_str.split()) == "取消":
                await reply.send(reply.plain_result("已取消选歌。"))
                return
            parsed = _parse_selection(reply.message_str)
            if parsed is None:
                await reply.send(reply.plain_result("请回复“序号”或“序号 下载”。"))
                return
            position, action = parsed
            current = self._require_search().snapshot(
                search_id=snapshot.search_id,
                session_id=reply.unified_msg_origin,
            )
            candidate = current.candidate_at(position) if current is not None else None
            if candidate is None:
                await reply.send(reply.plain_result("搜索结果已过期，请重新搜索。"))
                return
            result = await self._require_delivery().deliver(candidate)
            await self._send_delivery(reply, result, action)
        except (DeliveryError, FfmpegUnavailableError, MediaError) as exc:
            await reply.send(reply.plain_result(str(exc)))
        except Exception:
            logger.exception("listen-music interactive delivery failed")
            await reply.send(reply.plain_result("歌曲发送失败，请稍后重试。"))
        finally:
            controller.stop()

    async def _cancel_selection_wait(self, session_id: str) -> None:
        """Stop a just-created waiter when its result list could not be sent."""

        async with self._selection_lock:
            selection = self._selection_waits.get(session_id)
            if selection is not None:
                selection.cancelled = True
                selection.controller.stop()
        if selection is not None:
            await selection.finished.wait()

    async def account_status(self):
        owner = self._dashboard_owner()
        if owner is None:
            return error_response("仅 Dashboard 管理员可管理音乐账号", status_code=403)
        accounts, media = self._require_accounts(), self._require_media()
        payload = await accounts.status_payload()
        payload["health"] = media.health.as_payload()
        return json_response(payload)

    async def account_login(self):
        owner = self._dashboard_owner()
        if owner is None:
            return error_response("仅 Dashboard 管理员可管理音乐账号", status_code=403)
        try:
            snapshot = await self._require_accounts().start_login(owner)
            try:
                return json_response(_public_login_snapshot(snapshot, include_qr=True))
            except Exception:
                await self._require_accounts().cancel_login(
                    snapshot["session_id"], owner
                )
                raise
        except AccountError as exc:
            return error_response(str(exc), status_code=400)
        except Exception:
            logger.exception("listen-music account login start failed")
            return error_response("无法创建登录二维码", status_code=502)

    async def account_events(self, session_id: str):
        owner = self._dashboard_owner()
        if owner is None:
            return error_response("仅 Dashboard 管理员可管理音乐账号", status_code=403)
        events = self._require_accounts().login_events(session_id, owner)
        try:
            first = await anext(events)
        except LoginSessionNotFound as exc:
            return error_response(str(exc), status_code=404)
        except LoginSessionAccessDenied as exc:
            return error_response(str(exc), status_code=403)
        except AccountError as exc:
            return error_response(str(exc), status_code=400)

        async def stream() -> AsyncIterator[str]:
            yield _sse_data(_public_login_snapshot(first))
            async for snapshot in events:
                yield _sse_data(_public_login_snapshot(snapshot))

        return stream_response(
            stream(),
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def account_cancel(self, session_id: str):
        owner = self._dashboard_owner()
        if owner is None:
            return error_response("仅 Dashboard 管理员可管理音乐账号", status_code=403)
        try:
            await self._require_accounts().cancel_login(session_id, owner)
            return json_response({"session_id": session_id, "state": "cancelled"})
        except LoginSessionNotFound as exc:
            return error_response(str(exc), status_code=404)
        except LoginSessionAccessDenied as exc:
            return error_response(str(exc), status_code=403)
        except AccountError as exc:
            return error_response(str(exc), status_code=400)

    async def account_logout(self):
        owner = self._dashboard_owner()
        if owner is None:
            return error_response("仅 Dashboard 管理员可管理音乐账号", status_code=403)
        try:
            await self._require_accounts().logout()
            return json_response({"state": "anonymous"})
        except AccountError as exc:
            return error_response(str(exc), status_code=400)

    async def _start_bilibili_login(self) -> QrLoginStart:
        qr_session = await self._require_bilibili().start_qr_login()
        return QrLoginStart(
            qr_url=qr_session.qr_url,
            poll_context=qr_session.poll_token,
            expires_at=qr_session.expires_at,
        )

    async def _poll_bilibili_login(self, poll_context: object) -> QrLoginPoll:
        result = await self._require_bilibili().poll_qr_login(str(poll_context))
        return QrLoginPoll(
            state=result.state,
            message=result.message,
            cookies=result.cookies,
        )

    async def _cancel_bilibili_login(self, poll_context: object) -> None:
        await self._require_bilibili().cancel_qr_login(str(poll_context))

    async def _bilibili_profile(self, cookies: dict[str, str]) -> Any:
        return await self._require_bilibili().profile_from_cookies(cookies)

    async def _send_delivery(
        self,
        event: AstrMessageEvent,
        result: DeliveryResult,
        action: _DeliveryMode,
    ) -> None:
        media = self._require_media()
        try:
            platform_name = event.get_platform_name()
            if (
                action is _DeliveryMode.VOICE
                and platform_name not in _VOICE_AS_FILE_PLATFORMS
            ):
                try:
                    component = Record.fromFileSystem(result.media.path)
                    await event.send(MessageChain([component]))
                    return
                except Exception:
                    logger.warning(
                        "listen-music voice delivery failed on %s; falling back to file",
                        platform_name,
                    )

            component = File(name=result.media.filename, file=str(result.media.path))
            await event.send(MessageChain([component]))
        finally:
            await media.release(result.media)

    def _register_account_routes(self) -> None:
        prefix = f"/{PLUGIN_NAME}/accounts"
        self.context.register_web_api(
            f"{prefix}/status",
            self.account_status,
            ["GET"],
            "Listen Music account status",
        )
        self.context.register_web_api(
            f"{prefix}/login",
            self.account_login,
            ["POST"],
            "Start music account QR login",
        )
        self.context.register_web_api(
            f"{prefix}/login/<session_id>/events",
            self.account_events,
            ["GET"],
            "Stream music account QR login status",
        )
        self.context.register_web_api(
            f"{prefix}/login/<session_id>/cancel",
            self.account_cancel,
            ["POST"],
            "Cancel music account QR login",
        )
        self.context.register_web_api(
            f"{prefix}/logout",
            self.account_logout,
            ["POST"],
            "Remove music account credentials",
        )

    def _unregister_account_routes(self) -> None:
        """Remove handlers bound to this instance when AstrBot disables it."""

        routes = self.context.registered_web_apis
        routes[:] = [
            route
            for route in routes
            if not (
                route[0].startswith(f"/{PLUGIN_NAME}/accounts")
                and getattr(route[1], "__self__", None) is self
            )
        ]

    async def _replace_selection_wait(
        self, session_id: str, selection: _SelectionWait
    ) -> bool:
        """Replace a chat's host waiter only after the old one has cleaned up.

        AstrBot stores waiters globally by unified message origin.  Registering
        a replacement before the old waiter's final cleanup would let that
        cleanup remove the new waiter, so cancellation and replacement need a
        tiny serialized hand-off.
        """

        async with self._selection_lock:
            if not self._initialized:
                return False
            previous = self._selection_waits.get(session_id)
            if previous is not None:
                previous.cancelled = True
                previous.controller.stop()
                await previous.finished.wait()
            self._selection_waits[session_id] = selection
            return True

    def _finish_selection_wait(
        self, session_id: str, selection: _SelectionWait
    ) -> None:
        selection.finished.set()
        if self._selection_waits.get(session_id) is selection:
            self._selection_waits.pop(session_id, None)

    def _begin_llm_search(self, session_id: str) -> _LlmSearch:
        """Replace one chat's hidden search lease without a background cleaner."""

        now = time.monotonic()
        self._purge_expired_llm_searches(now)
        if (
            session_id not in self._llm_searches
            and len(self._llm_searches) >= SEARCH_SNAPSHOT_MAX_ENTRIES
        ):
            oldest_session = min(
                self._llm_searches,
                key=lambda key: self._llm_searches[key].expires_at,
            )
            self._llm_searches.pop(oldest_session, None)
        lease = _LlmSearch(expires_at=now + SEARCH_SNAPSHOT_TTL_SECONDS)
        self._llm_searches[session_id] = lease
        return lease

    def _complete_llm_search(
        self, session_id: str, lease: _LlmSearch, snapshot: SearchSnapshot
    ) -> bool:
        """Publish results only when the request still owns this chat's lease."""

        if self._llm_searches.get(session_id) is not lease:
            return False
        lease.search_id = snapshot.search_id
        lease.expires_at = snapshot.expires_at
        return True

    def _is_current_llm_search(self, session_id: str, lease: _LlmSearch) -> bool:
        return self._llm_searches.get(session_id) is lease

    def _active_llm_search(self, session_id: str, search_id: str) -> _LlmSearch | None:
        """Return a live exact-match lease without consuming a newer request."""

        lease = self._llm_searches.get(session_id)
        if lease is None:
            return None
        if lease.expires_at <= time.monotonic():
            self._discard_llm_search(session_id, lease)
            return None
        if not search_id or lease.search_id != search_id:
            return None
        return lease

    def _consume_llm_search(self, session_id: str, lease: _LlmSearch) -> bool:
        if self._llm_searches.get(session_id) is not lease:
            return False
        self._llm_searches.pop(session_id, None)
        return True

    def _discard_llm_search(self, session_id: str, lease: _LlmSearch) -> None:
        if self._llm_searches.get(session_id) is lease:
            self._llm_searches.pop(session_id, None)

    def _purge_expired_llm_searches(self, now: float) -> None:
        expired = [
            session_id
            for session_id, lease in self._llm_searches.items()
            if lease.expires_at <= now
        ]
        for session_id in expired:
            self._llm_searches.pop(session_id, None)

    def _clear_llm_search(self, session_id: str) -> None:
        """Forget a model-visible candidate set when its conversation moves on."""

        searches = getattr(self, "_llm_searches", None)
        if searches is not None:
            searches.pop(session_id, None)

    def _dashboard_owner(self) -> str | None:
        config = self.context.get_config()
        dashboard = config.get("dashboard", {}) if hasattr(config, "get") else {}
        owner = dashboard.get("username") if isinstance(dashboard, dict) else None
        username = request.username
        if isinstance(owner, str) and owner.strip() and username == owner:
            return owner
        return None

    def _require_accounts(self) -> AccountService:
        if self._accounts is None:
            raise RuntimeError("plugin is not initialized")
        return self._accounts

    def _require_bilibili(self) -> BilibiliClient:
        if self._bilibili is None:
            raise RuntimeError("plugin is not initialized")
        return self._bilibili

    def _require_media(self) -> MediaStore:
        if self._media is None:
            raise RuntimeError("plugin is not initialized")
        return self._media

    def _require_search(self) -> SearchService:
        if self._search is None:
            raise RuntimeError("plugin is not initialized")
        return self._search

    def _require_delivery(self) -> DeliveryService:
        if self._delivery is None:
            raise RuntimeError("plugin is not initialized")
        return self._delivery


def _parse_selection(message: str) -> tuple[int, _DeliveryMode] | None:
    """Parse the one user-facing grammar used by every selection flow."""

    match = _SELECTION_RE.fullmatch(" ".join(message.split()))
    if match is None:
        return None
    prefix_action, raw_position, suffix_action = match.groups()
    actions = {
        _selection_mode_from_word(word)
        for word in (prefix_action, suffix_action)
        if word is not None
    }
    if len(actions) > 1:
        return None
    return _SELECTION_POSITION_MAP[raw_position], next(
        iter(actions), _DeliveryMode.VOICE
    )


def _is_selection_reply(message: str) -> bool:
    return message == "取消" or _parse_selection(message) is not None


def _selection_mode_from_word(word: str) -> _DeliveryMode:
    return _DeliveryMode.DOWNLOAD if word == "下载" else _DeliveryMode.VOICE


def _delivery_preface(
    candidate: BilibiliCandidate,
    action: _DeliveryMode,
    note: object | None,
) -> str:
    """Build the sole user-visible message that precedes a delivered item."""

    if action is _DeliveryMode.VOICE:
        lead = f"给你播放《{candidate.display_title}》"
    else:
        lead = f"给你发送《{candidate.display_title}》的音频文件"

    normalized = " ".join(str(note or "").replace("\x00", "").split())
    if (
        not normalized
        or len(normalized) > MAX_DELIVERY_NOTE_LENGTH
        or any(marker in normalized for marker in _DELIVERY_NOTE_REJECTED_MARKERS)
    ):
        return f"{lead}。"

    normalized = normalized.lstrip("，,。；;：: ")
    if not normalized:
        return f"{lead}。"
    if normalized[-1] not in "。！？!?" and (
        normalized[-1].isalnum() or "\u4e00" <= normalized[-1] <= "\u9fff"
    ):
        normalized += "。"
    return f"{lead}，{normalized}"


def _public_login_snapshot(
    snapshot: dict[str, Any], *, include_qr: bool = False
) -> dict[str, Any]:
    public = dict(snapshot)
    qr_url = str(public.pop("qr_url", ""))
    if include_qr:
        public["qr_data_url"] = _qr_png_data_url(qr_url)
    return public


def _qr_png_data_url(content: str) -> str:
    if not content:
        raise ValueError("平台未返回有效登录二维码")
    try:
        import qrcode
    except ModuleNotFoundError as exc:
        raise RuntimeError("qrcode 依赖不可用") from exc
    image = qrcode.make(content)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _sse_data(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
