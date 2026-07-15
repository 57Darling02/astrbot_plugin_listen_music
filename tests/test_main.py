from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PLUGIN_ROOT.parent
for path in (REPOSITORY_ROOT, PLUGIN_ROOT, Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from support import ensure_aiohttp


def _install_astrbot_doubles() -> None:
    """Provide the narrow AstrBot surface needed to import the entry point."""

    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    event_filter = types.ModuleType("astrbot.api.event.filter")
    message_components = types.ModuleType("astrbot.api.message_components")
    star = types.ModuleType("astrbot.api.star")
    web = types.ModuleType("astrbot.api.web")
    core = types.ModuleType("astrbot.core")
    agent = types.ModuleType("astrbot.core.agent")
    tool = types.ModuleType("astrbot.core.agent.tool")
    message = types.ModuleType("astrbot.core.message")
    message_result = types.ModuleType("astrbot.core.message.message_event_result")
    star_package = types.ModuleType("astrbot.core.star")
    filter_package = types.ModuleType("astrbot.core.star.filter")
    command = types.ModuleType("astrbot.core.star.filter.command")
    utils = types.ModuleType("astrbot.core.utils")
    session_waiter = types.ModuleType("astrbot.core.utils.session_waiter")

    class AstrMessageEvent:
        pass

    class Filter:
        class EventMessageType:
            ALL = object()

        @staticmethod
        def command(_: str):
            return lambda handler: handler

        @staticmethod
        def event_message_type(*_args, **_kwargs):
            return lambda handler: handler

        @staticmethod
        def custom_filter(*_args, **_kwargs):
            return lambda handler: handler

    class CustomFilter:
        def __init__(self, *_args, **_kwargs):
            pass

    class File:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class Record:
        @staticmethod
        def fromFileSystem(path):
            return ("record", path)

    class Context:
        pass

    class Star:
        def __init__(self, context, config=None):
            self.context = context
            self.config = config

    class StarTools:
        @staticmethod
        def get_data_dir(_: str):
            return Path("/tmp")

    class FunctionTool:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class MessageChain(list):
        pass

    class GreedyStr(str):
        pass

    class SessionController:
        def __init__(self):
            self.stopped = False
            self._stopped = asyncio.Event()

        def stop(self):
            self.stopped = True
            self._stopped.set()

        async def wait_stopped(self):
            await self._stopped.wait()

    class SessionFilter:
        def filter(self, event):
            raise NotImplementedError

    class SessionWaiter:
        instances: list["SessionWaiter"] = []

        def __init__(self, session_filter, session_id, record_history_chains):
            self.session_filter = session_filter
            self.session_id = session_id
            self.record_history_chains = record_history_chains
            self.session_controller = SessionController()
            self.handler = None
            self.registered = asyncio.Event()
            self.instances.append(self)

        async def register_wait(self, handler, *_args, **_kwargs):
            self.handler = handler
            self.registered.set()
            await self.session_controller.wait_stopped()

    class Logger:
        def info(self, *_args, **_kwargs):
            pass

        def warning(self, *_args, **_kwargs):
            pass

        def exception(self, *_args, **_kwargs):
            pass

    def error_response(message, *, status_code=400, **_kwargs):
        return {"kind": "error", "message": message, "status_code": status_code}

    def json_response(payload, **_kwargs):
        return {"kind": "json", "payload": payload}

    def stream_response(content, **_kwargs):
        return {"kind": "stream", "content": content}

    event.AstrMessageEvent = AstrMessageEvent
    event.filter = Filter
    event_filter.CustomFilter = CustomFilter
    message_components.File = File
    message_components.Record = Record
    star.Context = Context
    star.Star = Star
    star.StarTools = StarTools
    web.error_response = error_response
    web.json_response = json_response
    web.request = types.SimpleNamespace(username="")
    web.stream_response = stream_response
    tool.FunctionTool = FunctionTool
    message_result.MessageChain = MessageChain
    command.GreedyStr = GreedyStr
    session_waiter.SessionController = SessionController
    session_waiter.SessionFilter = SessionFilter
    session_waiter.SessionWaiter = SessionWaiter
    session_waiter.FILTERS = []
    api.logger = Logger()

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.event.filter": event_filter,
            "astrbot.api.message_components": message_components,
            "astrbot.api.star": star,
            "astrbot.api.web": web,
            "astrbot.core": core,
            "astrbot.core.agent": agent,
            "astrbot.core.agent.tool": tool,
            "astrbot.core.message": message,
            "astrbot.core.message.message_event_result": message_result,
            "astrbot.core.star": star_package,
            "astrbot.core.star.filter": filter_package,
            "astrbot.core.star.filter.command": command,
            "astrbot.core.utils": utils,
            "astrbot.core.utils.session_waiter": session_waiter,
        }
    )


ensure_aiohttp()
_install_astrbot_doubles()
listen_main = importlib.import_module("astrbot_plugin_listen_music.main")


class _Event(listen_main.AstrMessageEvent):
    def __init__(
        self,
        session_id: str,
        message: str = "",
        *,
        platform_name: str = "test",
    ) -> None:
        self.unified_msg_origin = session_id
        self.message_str = message
        self._platform_name = platform_name
        self.call_llm = False
        self.stopped = False

    def get_platform_name(self) -> str:
        return self._platform_name

    def should_call_llm(self, value: bool) -> None:
        self.call_llm = value

    def stop_event(self) -> None:
        self.stopped = True


class _SendingEvent(_Event):
    """Event double that records exact user-visible delivery order."""

    def __init__(
        self,
        session_id: str,
        message: str = "",
        *,
        platform_name: str = "test",
    ) -> None:
        super().__init__(session_id, message, platform_name=platform_name)
        self.sent: list[object] = []

    def plain_result(self, text: str) -> tuple[str, str]:
        return ("plain", text)

    async def send(self, message: object) -> None:
        self.sent.append(message)


class _Candidate:
    def __init__(self, candidate_id: str, title: str) -> None:
        self.candidate_id = candidate_id
        self.bvid, raw_cid = candidate_id.split(":", maxsplit=1)
        self.cid = int(raw_cid)
        self.title = title
        self.display_title = title
        self.uploader = "fixture-up"
        self.duration_ms = 180_000
        self.search_title = f"搜索命中 {title}"
        self.page_title = "正片"


class _Snapshot:
    def __init__(self, candidates: tuple[_Candidate, ...]) -> None:
        self.search_id = "fixture-search"
        self.query = "fixture query"
        self.candidates = candidates
        self.session_id = "chat-a"
        self.expires_at = float("inf")

    def candidate_at(self, position: int) -> _Candidate | None:
        if position < 1 or position > len(self.candidates):
            return None
        return self.candidates[position - 1]


def _configure_selection_waits(plugin: object) -> None:
    plugin._selection_waits = {}
    plugin._llm_searches = {}
    plugin._selection_lock = asyncio.Lock()
    plugin._initialized = True


def _set_llm_search(plugin: object, session_id: str, search_id: str) -> None:
    plugin._llm_searches[session_id] = listen_main._LlmSearch(
        expires_at=float("inf"), search_id=search_id
    )


def _llm_search_ids(plugin: object) -> dict[str, str | None]:
    return {
        session_id: lease.search_id
        for session_id, lease in plugin._llm_searches.items()
    }


class MainContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        listen_main.FILTERS.clear()
        listen_main.SessionWaiter.instances.clear()

    def test_llm_tools_form_a_small_three_step_contract(self) -> None:
        find_tool = listen_main.FindMusicTool(types.SimpleNamespace())
        deliver_tool = listen_main.DeliverMusicTool(types.SimpleNamespace())
        search_tool = listen_main.SearchMusicTool(types.SimpleNamespace())

        self.assertEqual(find_tool.name, "find_music")
        self.assertEqual(
            set(find_tool.parameters["properties"]),
            {"title", "artist", "version"},
        )
        self.assertEqual(find_tool.parameters["required"], ["title"])
        self.assertIn("唱首歌", find_tool.description)

        self.assertEqual(deliver_tool.name, "deliver_music")
        self.assertEqual(
            set(deliver_tool.parameters["properties"]),
            {"search_id", "position", "note"},
        )
        self.assertEqual(deliver_tool.parameters["required"], ["search_id", "position"])
        self.assertEqual(
            deliver_tool.parameters["properties"]["position"]["maximum"],
            listen_main.SEARCH_LIMIT,
        )
        self.assertIn("find_music", deliver_tool.description)

        self.assertEqual(search_tool.name, "search_music")
        self.assertEqual(
            set(search_tool.parameters["properties"]),
            {"title", "artist", "version"},
        )
        self.assertEqual(search_tool.parameters["required"], ["title"])
        self.assertIn("下载", search_tool.description)

    async def test_llm_tools_forward_only_their_own_contract(self) -> None:
        calls: list[tuple[object, ...]] = []

        class FakePlugin:
            async def find_music_for_llm(self, event, title, *, artist, version):
                calls.append(("find", event, title, artist, version))
                return '{"status":"candidates"}'

            async def deliver_music_for_llm(self, event, search_id, position, *, note):
                calls.append(("deliver", event, search_id, position, note))
                return None

            async def present_music_search_for_llm(
                self, event, title, *, artist, version
            ):
                calls.append(("search", event, title, artist, version))
                return None

        event = _Event("chat-a", "播放晴天")
        context = types.SimpleNamespace(context=types.SimpleNamespace(event=event))

        self.assertEqual(
            await listen_main.FindMusicTool(FakePlugin()).call(
                context,
                title="晴天",
                artist="周杰伦",
                version="原唱",
            ),
            '{"status":"candidates"}',
        )
        self.assertIsNone(
            await listen_main.DeliverMusicTool(FakePlugin()).call(
                context,
                search_id="opaque-search",
                position=2,
                note="午后听一听",
            )
        )
        self.assertIsNone(
            await listen_main.SearchMusicTool(FakePlugin()).call(
                context,
                title="晴天",
                artist="周杰伦",
                version="录音室版",
            )
        )
        self.assertEqual(
            calls,
            [
                ("find", event, "晴天", "周杰伦", "原唱"),
                ("deliver", event, "opaque-search", 2, "午后听一听"),
                ("search", event, "晴天", "周杰伦", "录音室版"),
            ],
        )

    def test_music_request_omits_only_default_recording_preferences_from_query(
        self,
    ) -> None:
        for version in ("原版", "原唱", "Original"):
            request = listen_main._MusicRequest.from_fields(
                "日不落",
                artist="蔡依林",
                version=version,
            )
            self.assertEqual(request.query, "日不落 蔡依林")
            self.assertTrue(request.prefers_canonical_recording)

        live = listen_main._MusicRequest.from_fields(
            "日不落",
            artist="蔡依林",
            version="Live",
        )
        self.assertEqual(live.query, "日不落 蔡依林 Live")
        self.assertFalse(live.prefers_canonical_recording)

    async def test_find_music_returns_private_safe_candidates_without_chat_message(
        self,
    ) -> None:
        first = _Candidate("BV1private:42", "温奕心 - 一路生花")
        second = _Candidate("BV1private:43", "一路生花（AI 翻唱）")
        snapshot = _Snapshot((first, second))

        class FakeSearch:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def search(self, **kwargs):
                self.calls.append(kwargs)
                return snapshot

        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._search = search = FakeSearch()
        _configure_selection_waits(plugin)
        event = _SendingEvent("chat-a")

        result = await plugin.find_music_for_llm(
            event,
            "一路生花",
            artist="温奕心",
            version="原唱",
        )

        self.assertEqual(
            search.calls,
            [
                {
                    "session_id": "chat-a",
                    "query": "一路生花 温奕心",
                    "song_title": "一路生花",
                    "max_duration_ms": listen_main.VOICE_MEDIA_LIMITS.max_duration_ms,
                }
            ],
        )
        self.assertEqual(_llm_search_ids(plugin), {"chat-a": "fixture-search"})
        self.assertEqual(event.sent, [])

        payload = json.loads(result)
        self.assertEqual(payload["status"], "candidates")
        self.assertEqual(payload["search_id"], "fixture-search")
        self.assertEqual(len(payload["candidates"]), 2)
        self.assertEqual(
            set(payload["candidates"][0]),
            {"position", "title", "duration", "search_title", "page_title"},
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in (
            "bvid",
            "cid",
            "uploader",
            first.bvid,
            str(first.cid),
            first.uploader,
        ):
            self.assertNotIn(forbidden, serialized)

    async def test_new_message_cannot_reactivate_a_superseded_hidden_search(
        self,
    ) -> None:
        snapshot = _Snapshot((_Candidate("BV1fixture:1", "晴天"),))
        started = asyncio.Event()
        finish = asyncio.Event()

        class DelayedSearch:
            async def search(self, **_kwargs):
                started.set()
                await finish.wait()
                return snapshot

        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._search = DelayedSearch()
        _configure_selection_waits(plugin)
        request_event = _SendingEvent("chat-a")
        replacement_event = _SendingEvent("chat-a", "换一首")

        pending = asyncio.create_task(plugin.find_music_for_llm(request_event, "晴天"))
        await started.wait()
        await plugin.discard_llm_search_on_new_message(replacement_event)
        finish.set()

        result = json.loads(await pending)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["message"], "歌曲请求已被新的消息替换")
        self.assertEqual(plugin._llm_searches, {})

    async def test_cancelled_hidden_search_releases_its_lease(self) -> None:
        started = asyncio.Event()

        class DelayedSearch:
            async def search(self, **_kwargs):
                started.set()
                await asyncio.Event().wait()

        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._search = DelayedSearch()
        _configure_selection_waits(plugin)

        pending = asyncio.create_task(
            plugin.find_music_for_llm(_SendingEvent("chat-a"), "晴天")
        )
        await started.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending

        self.assertEqual(plugin._llm_searches, {})

    def test_hidden_search_leases_are_ttl_and_capacity_bounded(self) -> None:
        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._llm_searches = {
            "expired": listen_main._LlmSearch(expires_at=0.0, search_id="old")
        }

        for index in range(listen_main.SEARCH_SNAPSHOT_MAX_ENTRIES + 1):
            plugin._begin_llm_search(f"chat-{index}")

        self.assertNotIn("expired", plugin._llm_searches)
        self.assertNotIn("chat-0", plugin._llm_searches)
        self.assertEqual(
            len(plugin._llm_searches), listen_main.SEARCH_SNAPSHOT_MAX_ENTRIES
        )

    async def test_deliver_music_consumes_only_the_live_hidden_snapshot(self) -> None:
        candidate = _Candidate("BV1fixture:1", "温奕心 - 一路生花")
        snapshot = _Snapshot((candidate,))
        media = types.SimpleNamespace(
            path=Path("/tmp/fixture.m4a"), filename="fixture.m4a"
        )
        result = types.SimpleNamespace(candidate=candidate, media=media)
        released: list[object] = []

        class FakeSearch:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def snapshot(self, **kwargs):
                self.calls.append(kwargs)
                return snapshot

        class FakeDelivery:
            async def deliver(self, selected, *, limits):
                self.selected = selected
                self.limits = limits
                return result

        class FakeMedia:
            async def release(self, released_media):
                released.append(released_media)

        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._search = search = FakeSearch()
        plugin._delivery = delivery = FakeDelivery()
        plugin._media = FakeMedia()
        plugin._llm_searches = {}
        _set_llm_search(plugin, "chat-a", snapshot.search_id)
        event = _SendingEvent("chat-a")

        tool_result = await plugin.deliver_music_for_llm(
            event,
            snapshot.search_id,
            1,
            note="愿你接下来的路一路生花",
        )

        self.assertIsNone(tool_result)
        self.assertEqual(plugin._llm_searches, {})
        self.assertEqual(
            search.calls,
            [{"search_id": "fixture-search", "session_id": "chat-a"}],
        )
        self.assertIs(delivery.selected, candidate)
        self.assertIs(delivery.limits, listen_main.VOICE_MEDIA_LIMITS)
        self.assertEqual(
            event.sent,
            [
                ("plain", "给你播放《温奕心 - 一路生花》，愿你接下来的路一路生花。"),
                listen_main.MessageChain([("record", Path("/tmp/fixture.m4a"))]),
            ],
        )
        self.assertEqual(released, [media])

    async def test_deliver_music_rejects_a_hallucinated_hidden_search_id(self) -> None:
        class FakeSearch:
            def snapshot(self, **_kwargs):
                raise AssertionError("hallucinated search ID must not reach the store")

        class FailingDelivery:
            async def deliver(self, _candidate, *, limits):
                raise AssertionError("hallucinated search ID must not deliver")

        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._search = FakeSearch()
        plugin._delivery = FailingDelivery()
        plugin._media = types.SimpleNamespace()
        plugin._llm_searches = {}
        _set_llm_search(plugin, "chat-a", "fixture-search")
        event = _SendingEvent("chat-a")

        result = await plugin.deliver_music_for_llm(event, "invented-search", 1)

        self.assertIsNone(result)
        self.assertEqual(_llm_search_ids(plugin), {"chat-a": "fixture-search"})
        self.assertEqual(event.sent, [("plain", "候选已失效，请重新搜索")])

    async def test_deliver_music_rejects_a_hallucinated_position(self) -> None:
        snapshot = _Snapshot((_Candidate("BV1fixture:1", "晴天"),))

        class FakeSearch:
            def snapshot(self, **_kwargs):
                return snapshot

        class FailingDelivery:
            async def deliver(self, _candidate, *, limits):
                raise AssertionError("a position outside the snapshot must not deliver")

        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._search = FakeSearch()
        plugin._delivery = FailingDelivery()
        plugin._media = types.SimpleNamespace()
        plugin._llm_searches = {}
        _set_llm_search(plugin, "chat-a", snapshot.search_id)
        event = _SendingEvent("chat-a")

        result = await plugin.deliver_music_for_llm(event, snapshot.search_id, 2)

        self.assertIsNone(result)
        self.assertEqual(_llm_search_ids(plugin), {"chat-a": "fixture-search"})
        self.assertEqual(event.sent, [("plain", "候选无效或已过期，请重新搜索")])

    async def test_deliver_music_rejects_an_expired_hidden_snapshot(self) -> None:
        class FakeSearch:
            def snapshot(self, **_kwargs):
                return None

        class FailingDelivery:
            async def deliver(self, _candidate, *, limits):
                raise AssertionError("expired search must not deliver")

        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._search = FakeSearch()
        plugin._delivery = FailingDelivery()
        plugin._media = types.SimpleNamespace()
        plugin._llm_searches = {}
        _set_llm_search(plugin, "chat-a", "fixture-search")
        event = _SendingEvent("chat-a")

        result = await plugin.deliver_music_for_llm(event, "fixture-search", 1)

        self.assertIsNone(result)
        self.assertEqual(plugin._llm_searches, {})
        self.assertEqual(event.sent, [("plain", "候选无效或已过期，请重新搜索")])

    async def test_deliver_music_rejects_a_hidden_snapshot_from_another_session(
        self,
    ) -> None:
        class FakeSearch:
            def snapshot(self, **_kwargs):
                raise AssertionError("another session must not access this search")

        class FailingDelivery:
            async def deliver(self, _candidate, *, limits):
                raise AssertionError("another session must not deliver")

        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._search = FakeSearch()
        plugin._delivery = FailingDelivery()
        plugin._media = types.SimpleNamespace()
        plugin._llm_searches = {}
        _set_llm_search(plugin, "chat-a", "fixture-search")
        event = _SendingEvent("chat-b")

        result = await plugin.deliver_music_for_llm(event, "fixture-search", 1)

        self.assertIsNone(result)
        self.assertEqual(_llm_search_ids(plugin), {"chat-a": "fixture-search"})
        self.assertEqual(event.sent, [("plain", "候选已失效，请重新搜索")])

    async def test_presented_search_sends_known_failure_without_returning_it_to_the_model(
        self,
    ) -> None:
        class FailingSearch:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def search(self, **kwargs):
                self.calls.append(kwargs)
                raise listen_main.MusicSearchError("没有找到可播放的歌曲")

        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._search = search = FailingSearch()
        _configure_selection_waits(plugin)
        event = _SendingEvent("chat-a")

        result = await plugin.present_music_search_for_llm(
            event,
            "不存在的歌",
            artist="示例歌手",
            version="原唱",
        )

        self.assertIsNone(result)
        self.assertEqual(
            search.calls,
            [
                {
                    "session_id": "chat-a",
                    "query": "不存在的歌 示例歌手",
                    "song_title": "不存在的歌",
                    "video_ref": None,
                }
            ],
        )
        self.assertEqual(event.sent, [("plain", "没有找到可播放的歌曲")])

    async def test_search_music_shows_a_ten_candidate_user_list_and_registers_selection(
        self,
    ) -> None:
        snapshot = _Snapshot(
            tuple(
                _Candidate(f"BV1fixture:{position}", f"候选 {position}")
                for position in range(1, listen_main.SEARCH_LIMIT + 1)
            )
        )

        class FakeSearch:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def search(self, **kwargs):
                self.calls.append(kwargs)
                return snapshot

        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._search = search = FakeSearch()
        _configure_selection_waits(plugin)
        event = _SendingEvent("chat-a", "下载晴天")

        result = await plugin.present_music_search_for_llm(
            event,
            "晴天",
            artist="周杰伦",
            version="录音室版",
        )

        self.assertIsNone(result)
        self.assertEqual(
            search.calls,
            [
                {
                    "session_id": "chat-a",
                    "query": "晴天 周杰伦 录音室版",
                    "song_title": "晴天",
                    "video_ref": None,
                }
            ],
        )
        self.assertIn("Bilibili 搜索结果", event.sent[0][1])
        self.assertIn("1. 候选 1 (3:00)", event.sent[0][1])
        self.assertIn("10. 候选 10 (3:00)", event.sent[0][1])
        self.assertNotIn("fixture-up", event.sent[0][1])
        self.assertIn("chat-a", plugin._selection_waits)
        self.assertEqual(len(listen_main.SessionWaiter.instances), 1)
        self.assertTrue(listen_main.SessionWaiter.instances[0].registered.is_set())

        await plugin._cancel_selection_wait("chat-a")
        self.assertEqual(plugin._selection_waits, {})

    async def test_search_music_uses_an_av_reference_only_from_user_message(
        self,
    ) -> None:
        snapshot = _Snapshot((_Candidate("BV1fixture:1", "指定视频"),))

        class FakeSearch:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def search(self, **kwargs):
                self.calls.append(kwargs)
                return snapshot

        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._search = search = FakeSearch()
        _configure_selection_waits(plugin)
        event = _SendingEvent("chat-a", "下载 av170001")

        await plugin.present_music_search_for_llm(event, "模型整理出的标题")

        reference = search.calls[0]["video_ref"]
        self.assertIsNotNone(reference)
        assert reference is not None
        self.assertEqual(reference.aid, 170001)
        self.assertIsNone(reference.bvid)
        await plugin._cancel_selection_wait("chat-a")

    async def test_search_music_does_not_trust_a_model_supplied_bv_reference(
        self,
    ) -> None:
        snapshot = _Snapshot((_Candidate("BV1fixture:1", "普通搜索"),))

        class FakeSearch:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def search(self, **kwargs):
                self.calls.append(kwargs)
                return snapshot

        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._search = search = FakeSearch()
        _configure_selection_waits(plugin)

        await plugin.present_music_search_for_llm(
            _SendingEvent("chat-a", "下载晴天"),
            "BV1Q541167Qg",
        )

        self.assertIsNone(search.calls[0]["video_ref"])
        await plugin._cancel_selection_wait("chat-a")

    async def test_search_command_uses_a_bv_reference_from_its_query(self) -> None:
        snapshot = _Snapshot((_Candidate("BV1fixture:1", "指定视频"),))

        class FakeSearch:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def search(self, **kwargs):
                self.calls.append(kwargs)
                return snapshot

        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._search = search = FakeSearch()
        _configure_selection_waits(plugin)
        event = _SendingEvent("chat-a", "搜索歌曲 BV1Q541167Qg")

        results = [
            result
            async for result in plugin.search_song(
                event, listen_main.GreedyStr("BV1Q541167Qg")
            )
        ]

        self.assertEqual(len(results), 1)
        reference = search.calls[0]["video_ref"]
        self.assertIsNotNone(reference)
        assert reference is not None
        self.assertEqual(reference.bvid, "BV1Q541167Qg")
        await plugin._cancel_selection_wait("chat-a")

    async def test_search_song_uses_the_same_selection_session(self) -> None:
        snapshot = _Snapshot((_Candidate("BV1fixture:1", "晴天"),))

        class FakeSearch:
            async def search(self, **_kwargs):
                return snapshot

        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._search = FakeSearch()
        _configure_selection_waits(plugin)
        event = _SendingEvent("chat-a")

        results = [
            item
            async for item in plugin.search_song(event, listen_main.GreedyStr("晴天"))
        ]

        self.assertEqual(len(results), 1)
        self.assertIn("Bilibili 搜索结果", results[0][1])
        self.assertTrue(event.call_llm)
        self.assertTrue(event.stopped)
        self.assertIn("chat-a", plugin._selection_waits)
        await plugin._cancel_selection_wait("chat-a")

    async def test_search_music_waiter_delivers_only_the_next_user_selection(
        self,
    ) -> None:
        candidate = _Candidate("BV1fixture:1", "晴天")
        snapshot = _Snapshot((candidate,))
        media = types.SimpleNamespace(
            path=Path("/tmp/fixture.m4a"), filename="fixture.m4a"
        )
        result = types.SimpleNamespace(candidate=candidate, media=media)

        class FakeSearch:
            async def search(self, **_kwargs):
                return snapshot

            def snapshot(self, **_kwargs):
                return snapshot

        class FakeDelivery:
            async def deliver(self, selected, *, limits):
                self.selected = selected
                self.limits = limits
                return result

        class FakeMedia:
            async def release(self, released_media):
                self.released = released_media

        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._search = FakeSearch()
        plugin._delivery = delivery = FakeDelivery()
        plugin._media = FakeMedia()
        _configure_selection_waits(plugin)

        await plugin.present_music_search_for_llm(
            _SendingEvent("chat-a", "下载晴天"), "晴天"
        )
        waiter = listen_main.SessionWaiter.instances[0]
        reply = _SendingEvent("chat-a", "1 下载")
        assert waiter.handler is not None
        await waiter.handler(waiter.session_controller, reply)
        await asyncio.sleep(0)

        self.assertIs(delivery.selected, candidate)
        self.assertIs(delivery.limits, listen_main.DOWNLOAD_MEDIA_LIMITS)
        self.assertEqual(reply.sent[0][0].kwargs["name"], "fixture.m4a")
        self.assertTrue(waiter.session_controller.stopped)
        self.assertEqual(plugin._selection_waits, {})

    async def test_manual_selection_resolves_the_original_snapshot_once(self) -> None:
        candidate = _Candidate("BV1fixture:2", "候选二")
        snapshot = _Snapshot((_Candidate("BV1fixture:1", "候选一"), candidate))
        media = types.SimpleNamespace(
            path=Path("/tmp/fixture.m4a"), filename="fixture.m4a"
        )
        result = types.SimpleNamespace(candidate=candidate, media=media)
        released: list[object] = []

        class FakeSearch:
            def __init__(self) -> None:
                self.calls: list[dict[str, str]] = []

            def snapshot(self, **kwargs):
                self.calls.append(kwargs)
                return snapshot

        class FakeDelivery:
            async def deliver(self, selected, *, limits):
                self.selected = selected
                self.limits = limits
                return result

        class FakeMedia:
            async def release(self, released_media):
                released.append(released_media)

        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._search = search = FakeSearch()
        plugin._delivery = delivery = FakeDelivery()
        plugin._media = FakeMedia()
        controller = listen_main.SessionController()
        reply = _SendingEvent("chat-a", "第2首 下载")

        await plugin._deliver_selection(controller, reply, snapshot)

        self.assertEqual(
            search.calls,
            [{"search_id": "fixture-search", "session_id": "chat-a"}],
        )
        self.assertIs(delivery.selected, candidate)
        self.assertIs(delivery.limits, listen_main.DOWNLOAD_MEDIA_LIMITS)
        self.assertTrue(controller.stopped)
        component = reply.sent[0][0]
        self.assertEqual(component.kwargs["name"], "fixture.m4a")
        self.assertEqual(component.kwargs["file"], "/tmp/fixture.m4a")
        self.assertEqual(released, [media])

    async def test_long_manual_voice_selection_keeps_wait_for_download(self) -> None:
        candidate = _Candidate("BV1fixture:1", "长音频")
        candidate.duration_ms = 16 * 60 * 1000
        snapshot = _Snapshot((candidate,))

        class FakeSearch:
            def snapshot(self, **_kwargs):
                return snapshot

        class FailingDelivery:
            async def deliver(self, _candidate, *, limits):
                raise AssertionError("long voice must not start a delivery")

        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._search = FakeSearch()
        plugin._delivery = FailingDelivery()
        plugin._media = types.SimpleNamespace()
        controller = listen_main.SessionController()
        reply = _SendingEvent("chat-a", "1")

        await plugin._deliver_selection(controller, reply, snapshot)

        self.assertFalse(controller.stopped)
        self.assertEqual(
            reply.sent,
            [("plain", "第 1 首时长超过 15 分钟，仅可下载；请回复“1 下载”。")],
        )

    async def test_expired_manual_selection_never_delivers_a_stale_candidate(
        self,
    ) -> None:
        snapshot = _Snapshot((_Candidate("BV1fixture:1", "晴天"),))

        class FakeSearch:
            def snapshot(self, **_kwargs):
                return None

        class FailingDelivery:
            async def deliver(self, _candidate, *, limits):
                raise AssertionError("expired selection must not deliver")

        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._search = FakeSearch()
        plugin._delivery = FailingDelivery()
        plugin._media = types.SimpleNamespace()
        controller = listen_main.SessionController()
        reply = _SendingEvent("chat-a", "1 下载")

        await plugin._deliver_selection(controller, reply, snapshot)

        self.assertTrue(controller.stopped)
        self.assertEqual(reply.sent, [("plain", "搜索结果已过期，请重新搜索。")])

    async def test_selection_wait_timeout_notifies_then_cleans_up(self) -> None:
        class TimedOutWaiter:
            async def register_wait(self, *_args, **_kwargs):
                raise TimeoutError

        plugin = object.__new__(listen_main.ListenMusicPlugin)
        _configure_selection_waits(plugin)
        source = _SendingEvent("chat-a")
        snapshot = _Snapshot((_Candidate("BV1fixture:1", "晴天"),))
        selection_filter = listen_main._SelectionSessionFilter("chat-a")
        selection = listen_main._SelectionWait(
            listen_main.SessionController(), asyncio.Event()
        )
        plugin._selection_waits["chat-a"] = selection
        listen_main.FILTERS.append(selection_filter)

        await plugin._run_selection_wait(
            source_event=source,
            snapshot=snapshot,
            selection_filter=selection_filter,
            waiter=TimedOutWaiter(),
            selection=selection,
        )

        self.assertEqual(
            source.sent,
            [("plain", "没有收到选歌回复，本次搜索已结束。")],
        )
        self.assertTrue(selection.finished.is_set())
        self.assertEqual(plugin._selection_waits, {})
        self.assertNotIn(selection_filter, listen_main.FILTERS)

    async def test_cancelled_selection_wait_stays_silent_on_timeout_race(self) -> None:
        class TimedOutWaiter:
            async def register_wait(self, *_args, **_kwargs):
                raise TimeoutError

        plugin = object.__new__(listen_main.ListenMusicPlugin)
        _configure_selection_waits(plugin)
        source = _SendingEvent("chat-a")
        snapshot = _Snapshot((_Candidate("BV1fixture:1", "晴天"),))
        selection_filter = listen_main._SelectionSessionFilter("chat-a")
        selection = listen_main._SelectionWait(
            listen_main.SessionController(), asyncio.Event(), cancelled=True
        )
        plugin._selection_waits["chat-a"] = selection
        listen_main.FILTERS.append(selection_filter)

        await plugin._run_selection_wait(
            source_event=source,
            snapshot=snapshot,
            selection_filter=selection_filter,
            waiter=TimedOutWaiter(),
            selection=selection,
        )

        self.assertEqual(source.sent, [])
        self.assertTrue(selection.finished.is_set())
        self.assertEqual(plugin._selection_waits, {})

    async def test_new_non_selection_message_discards_wait_without_stopping_event(
        self,
    ) -> None:
        plugin = object.__new__(listen_main.ListenMusicPlugin)
        _configure_selection_waits(plugin)
        controller = listen_main.SessionController()
        selection = listen_main._SelectionWait(controller, asyncio.Event())
        plugin._selection_waits["chat-a"] = selection
        _set_llm_search(plugin, "chat-a", "hidden-search")
        event = _SendingEvent("chat-a", "再发一次")

        discarding = asyncio.create_task(
            plugin.discard_selection_wait_on_new_message(event)
        )
        await asyncio.sleep(0)

        self.assertTrue(controller.stopped)
        self.assertTrue(selection.cancelled)
        self.assertFalse(event.stopped)
        self.assertEqual(event.sent, [])
        self.assertFalse(discarding.done())

        plugin._finish_selection_wait("chat-a", selection)
        await discarding
        self.assertEqual(plugin._selection_waits, {})
        self.assertEqual(plugin._llm_searches, {})

    async def test_new_message_discards_hidden_candidates_without_claiming_event(
        self,
    ) -> None:
        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._llm_searches = {}
        _set_llm_search(plugin, "chat-a", "hidden-search")
        event = _SendingEvent("chat-a", "换一首")

        await plugin.discard_llm_search_on_new_message(event)

        self.assertEqual(plugin._llm_searches, {})
        self.assertFalse(event.stopped)
        self.assertEqual(event.sent, [])

    async def test_find_music_cancels_an_old_selection_before_searching(self) -> None:
        plugin = object.__new__(listen_main.ListenMusicPlugin)
        _configure_selection_waits(plugin)
        old_snapshot = _Snapshot((_Candidate("BV1fixture:1", "旧候选"),))
        await plugin._start_selection_wait(_SendingEvent("chat-a"), old_snapshot)
        old_waiter = listen_main.SessionWaiter.instances[-1]

        class FakeSearch:
            async def search(self, **_kwargs):
                self.old_waiter_stopped = old_waiter.session_controller.stopped
                raise listen_main.MusicSearchError("没有找到可播放的歌曲")

        plugin._search = search = FakeSearch()
        event = _SendingEvent("chat-a", "再发一次")

        result = await plugin.find_music_for_llm(event, "晴天")

        self.assertTrue(search.old_waiter_stopped)
        self.assertEqual(plugin._selection_waits, {})
        self.assertEqual(json.loads(result)["status"], "error")

    async def test_search_music_cancels_an_old_selection_before_searching(self) -> None:
        plugin = object.__new__(listen_main.ListenMusicPlugin)
        _configure_selection_waits(plugin)
        old_snapshot = _Snapshot((_Candidate("BV1fixture:1", "旧候选"),))
        await plugin._start_selection_wait(_SendingEvent("chat-a"), old_snapshot)
        old_waiter = listen_main.SessionWaiter.instances[-1]
        new_snapshot = _Snapshot((_Candidate("BV1fixture:2", "新候选"),))

        class FakeSearch:
            async def search(self, **_kwargs):
                self.old_waiter_stopped = old_waiter.session_controller.stopped
                return new_snapshot

        plugin._search = search = FakeSearch()

        await plugin.present_music_search_for_llm(_SendingEvent("chat-a"), "晴天")

        self.assertTrue(search.old_waiter_stopped)
        self.assertEqual(len(listen_main.SessionWaiter.instances), 2)
        await plugin._cancel_selection_wait("chat-a")

    async def test_search_command_cancels_an_old_selection_before_searching(
        self,
    ) -> None:
        plugin = object.__new__(listen_main.ListenMusicPlugin)
        _configure_selection_waits(plugin)
        old_snapshot = _Snapshot((_Candidate("BV1fixture:1", "旧候选"),))
        await plugin._start_selection_wait(_SendingEvent("chat-a"), old_snapshot)
        old_waiter = listen_main.SessionWaiter.instances[-1]
        new_snapshot = _Snapshot((_Candidate("BV1fixture:2", "新候选"),))

        class FakeSearch:
            async def search(self, **_kwargs):
                self.old_waiter_stopped = old_waiter.session_controller.stopped
                return new_snapshot

        plugin._search = search = FakeSearch()
        event = _SendingEvent("chat-a", "搜索歌曲 晴天")

        results = [
            result
            async for result in plugin.search_song(event, listen_main.GreedyStr("晴天"))
        ]

        self.assertTrue(search.old_waiter_stopped)
        self.assertEqual(len(results), 1)
        await plugin._cancel_selection_wait("chat-a")

    async def test_preface_failure_releases_the_prepared_file(self) -> None:
        candidate = _Candidate("BV1fixture:1", "一路生花")
        media = types.SimpleNamespace(
            path=Path("/tmp/fixture.m4a"), filename="fixture.m4a"
        )
        result = types.SimpleNamespace(candidate=candidate, media=media)
        released: list[object] = []

        class FakeDelivery:
            async def deliver(self, _candidate, *, limits):
                return result

        class FakeMedia:
            async def release(self, released_media):
                released.append(released_media)

        class FailingEvent(_SendingEvent):
            async def send(self, _message: object) -> None:
                raise RuntimeError("preface unavailable")

        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._delivery = FakeDelivery()
        plugin._media = FakeMedia()

        with self.assertRaisesRegex(RuntimeError, "preface unavailable"):
            await plugin._deliver_with_preface(
                FailingEvent("chat-a"),
                candidate=candidate,
                action=listen_main._DeliveryMode.VOICE,
                preface="给你播放《一路生花》。",
            )
        self.assertEqual(released, [media])

    async def test_send_delivery_falls_back_to_file_when_voice_component_fails(
        self,
    ) -> None:
        released: list[object] = []

        class FakeMedia:
            async def release(self, media):
                released.append(media)

        media_result = types.SimpleNamespace(
            path=Path("/tmp/fixture.m4a"), filename="fixture.m4a"
        )
        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._media = FakeMedia()
        event = _SendingEvent("chat-a")
        result = types.SimpleNamespace(media=media_result)
        original = listen_main.Record.fromFileSystem

        def fail_component_construction(_path):
            raise RuntimeError("component failed")

        listen_main.Record.fromFileSystem = staticmethod(fail_component_construction)
        try:
            await plugin._send_delivery(event, result, listen_main._DeliveryMode.VOICE)
        finally:
            listen_main.Record.fromFileSystem = staticmethod(original)

        self.assertEqual(released, [media_result])
        self.assertEqual(len(event.sent), 1)
        component = event.sent[0][0]
        self.assertIsInstance(component, listen_main.File)
        self.assertEqual(
            component.kwargs,
            {"name": "fixture.m4a", "file": "/tmp/fixture.m4a"},
        )

    async def test_weixin_oc_voice_delivery_uses_file_without_trying_record(
        self,
    ) -> None:
        released: list[object] = []

        class FakeMedia:
            async def release(self, media):
                released.append(media)

        media_result = types.SimpleNamespace(
            path=Path("/tmp/fixture.m4a"), filename="fixture.m4a"
        )
        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._media = FakeMedia()
        event = _SendingEvent("chat-a", platform_name="weixin_oc")
        result = types.SimpleNamespace(media=media_result)
        original = listen_main.Record.fromFileSystem

        def record_must_not_be_created(_path):
            raise AssertionError("weixin_oc must use a file directly")

        listen_main.Record.fromFileSystem = staticmethod(record_must_not_be_created)
        try:
            await plugin._send_delivery(event, result, listen_main._DeliveryMode.VOICE)
        finally:
            listen_main.Record.fromFileSystem = staticmethod(original)

        self.assertEqual(released, [media_result])
        self.assertEqual(len(event.sent), 1)
        self.assertIsInstance(event.sent[0][0], listen_main.File)

    async def test_send_delivery_retries_file_when_voice_send_is_rejected(self) -> None:
        released: list[object] = []

        class FakeMedia:
            async def release(self, media):
                released.append(media)

        class RecordRejectingEvent(_SendingEvent):
            async def send(self, message: object) -> None:
                if isinstance(message[0], tuple) and message[0][0] == "record":
                    raise RuntimeError("record unsupported")
                await super().send(message)

        media_result = types.SimpleNamespace(
            path=Path("/tmp/fixture.m4a"), filename="fixture.m4a"
        )
        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._media = FakeMedia()
        event = RecordRejectingEvent("chat-a")
        result = types.SimpleNamespace(media=media_result)

        await plugin._send_delivery(event, result, listen_main._DeliveryMode.VOICE)

        self.assertEqual(released, [media_result])
        self.assertEqual(len(event.sent), 1)
        self.assertIsInstance(event.sent[0][0], listen_main.File)

    def test_selection_parser_and_filter_share_one_grammar(self) -> None:
        expected = listen_main._DeliveryMode
        self.assertEqual(listen_main._parse_selection("第2首"), (2, expected.VOICE))
        self.assertEqual(
            listen_main._parse_selection("选第二个 下载"), (2, expected.DOWNLOAD)
        )
        self.assertEqual(
            listen_main._parse_selection("我要下载第3首歌"), (3, expected.DOWNLOAD)
        )
        self.assertEqual(
            listen_main._parse_selection("选第五首 下载"), (5, expected.DOWNLOAD)
        )
        self.assertEqual(listen_main._parse_selection("第10首"), (10, expected.VOICE))
        self.assertEqual(
            listen_main._parse_selection("选第十首 下载"), (10, expected.DOWNLOAD)
        )
        self.assertIsNone(listen_main._parse_selection("第11首"))
        self.assertIsNone(listen_main._parse_selection("选第十一首"))
        self.assertIsNone(listen_main._parse_selection("下载第2首 听"))
        self.assertIsNone(listen_main._parse_selection("我觉得第二首不错"))

        selection_filter = listen_main._SelectionSessionFilter("chat-a")
        self.assertEqual(
            selection_filter.filter(_Event("chat-a", "第2首 下载")), "chat-a"
        )
        self.assertEqual(selection_filter.filter(_Event("chat-a", "取消")), "chat-a")
        self.assertEqual(selection_filter.filter(_Event("chat-a", "下载晴天")), "")
        self.assertEqual(selection_filter.filter(_Event("chat-b", "1")), "")

    def test_discard_wait_filter_is_pure_and_ignores_selection_replies(self) -> None:
        selection_filter = listen_main._SelectionSessionFilter("chat-a")
        listen_main.FILTERS.append(selection_filter)
        discard_filter = listen_main._DiscardSelectionWaitFilter()

        self.assertTrue(discard_filter.filter(_Event("chat-a", "再发一次"), None))
        self.assertEqual(listen_main.FILTERS, [selection_filter])
        self.assertFalse(discard_filter.filter(_Event("chat-a", "第1首"), None))
        self.assertFalse(discard_filter.filter(_Event("chat-a", "取消"), None))
        self.assertFalse(discard_filter.filter(_Event("chat-b", "再发一次"), None))

    def test_delivery_preface_discards_work_trace_notes(self) -> None:
        candidate = _Candidate("BV1fixture:1", "一路生花")

        preface = listen_main._delivery_preface(
            candidate,
            listen_main._DeliveryMode.VOICE,
            "我换用中文关键词再检索一次",
        )

        self.assertEqual(preface, "给你播放《一路生花》。")

    def test_login_views_never_expose_the_raw_qr_url(self) -> None:
        snapshot = {
            "session_id": "opaque-session",
            "state": "waiting",
            "qr_url": "https://secret.example.test/qr",
        }
        public = listen_main._public_login_snapshot(snapshot)
        self.assertNotIn("qr_url", public)

        original = listen_main._qr_png_data_url
        listen_main._qr_png_data_url = lambda value: f"data:image/png;base64,{value}"
        try:
            with_qr = listen_main._public_login_snapshot(snapshot, include_qr=True)
        finally:
            listen_main._qr_png_data_url = original
        self.assertNotIn("qr_url", with_qr)
        self.assertEqual(
            with_qr["qr_data_url"],
            "data:image/png;base64,https://secret.example.test/qr",
        )
        self.assertNotIn("qr_url", listen_main._sse_data(public))

    async def test_dashboard_api_key_cannot_access_account_status(self) -> None:
        web = sys.modules["astrbot.api.web"]
        web.request.username = "api_key:plugin-token"
        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin.context = types.SimpleNamespace(
            get_config=lambda: {"dashboard": {"username": "dashboard-admin"}}
        )

        response = await plugin.account_status()

        self.assertEqual(response["kind"], "error")
        self.assertEqual(response["status_code"], 403)

    def test_account_routes_are_fixed_to_the_single_bilibili_account(self) -> None:
        routes = []
        context = types.SimpleNamespace(registered_web_apis=routes)

        def register(route, handler, methods, description):
            routes.append((route, handler, methods, description))

        context.register_web_api = register
        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin.context = context

        plugin._register_account_routes()

        self.assertEqual(
            [route[0] for route in routes],
            [
                "/astrbot_plugin_listen_music/accounts/status",
                "/astrbot_plugin_listen_music/accounts/login",
                "/astrbot_plugin_listen_music/accounts/login/<session_id>/events",
                "/astrbot_plugin_listen_music/accounts/login/<session_id>/cancel",
                "/astrbot_plugin_listen_music/accounts/logout",
            ],
        )

    async def test_initialize_composes_the_three_music_tools(self) -> None:
        created = types.SimpleNamespace(
            accounts=None, bilibili=None, media=None, http=None
        )

        class FakeSession:
            closed = False

            async def close(self):
                self.closed = True

        class FakeAiohttp:
            class TCPConnector:
                def __init__(self, **_kwargs):
                    pass

            class ClientTimeout:
                def __init__(self, **_kwargs):
                    pass

            @staticmethod
            def ClientSession(**_kwargs):
                created.http = FakeSession()
                return created.http

        class FakeAccounts:
            def __init__(self, _store, authenticator):
                created.accounts = self
                self.authenticator = authenticator
                self.closed = False

            async def restore_credentials(self):
                pass

            def cookies(self):
                return {"SESSDATA": "fixture"}

            async def aclose(self):
                self.closed = True

        class FakeBilibili:
            def __init__(self, _http, *, credentials_getter):
                created.bilibili = self
                self.credentials_getter = credentials_getter

        class FakeMedia:
            def __init__(self, _http, _cache_dir):
                created.media = self
                self.closed = False
                self.reclaimed = False

            async def reclaim_stale(self):
                self.reclaimed = True

            async def aclose(self):
                self.closed = True

        class FakeSearch:
            def __init__(self, bilibili, _snapshots):
                self.bilibili = bilibili

        class FakeDelivery:
            def __init__(self, *, bilibili, media):
                self.bilibili = bilibili
                self.media = media

        class FakeContext:
            def __init__(self):
                self.registered_web_apis = []
                self.tools = []

            def register_web_api(self, route, handler, methods, description):
                self.registered_web_apis.append((route, handler, methods, description))

            def add_llm_tools(self, *tools):
                self.tools.extend(tools)

        originals = {
            "aiohttp": listen_main.aiohttp,
            "AccountService": listen_main.AccountService,
            "BilibiliClient": listen_main.BilibiliClient,
            "MediaStore": listen_main.MediaStore,
            "SearchService": listen_main.SearchService,
            "DeliveryService": listen_main.DeliveryService,
            "StarTools": listen_main.StarTools,
        }
        with tempfile.TemporaryDirectory() as directory:
            try:
                listen_main.aiohttp = FakeAiohttp
                listen_main.AccountService = FakeAccounts
                listen_main.BilibiliClient = FakeBilibili
                listen_main.MediaStore = FakeMedia
                listen_main.SearchService = FakeSearch
                listen_main.DeliveryService = FakeDelivery
                listen_main.StarTools = types.SimpleNamespace(
                    get_data_dir=lambda _name: Path(directory)
                )
                context = FakeContext()
                plugin = listen_main.ListenMusicPlugin(context)

                await plugin.initialize()

                self.assertIsInstance(
                    created.accounts.authenticator, listen_main.BilibiliAuthenticator
                )
                self.assertEqual(
                    created.bilibili.credentials_getter(), {"SESSDATA": "fixture"}
                )
                self.assertIs(plugin._search.bilibili, created.bilibili)
                self.assertIs(plugin._delivery.bilibili, created.bilibili)
                self.assertTrue(created.media.reclaimed)
                self.assertEqual(
                    [tool.name for tool in context.tools],
                    ["find_music", "deliver_music", "search_music"],
                )

                await plugin.terminate()
                self.assertTrue(created.media.closed)
                self.assertTrue(created.accounts.closed)
                self.assertTrue(created.http.closed)
                self.assertEqual(context.registered_web_apis, [])
            finally:
                for name, original in originals.items():
                    setattr(listen_main, name, original)

    async def test_replacement_waits_for_the_old_waiter_cleanup(self) -> None:
        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._selection_lock = asyncio.Lock()
        plugin._initialized = True
        old_controller = listen_main.SessionController()
        old = listen_main._SelectionWait(old_controller, asyncio.Event())
        new = listen_main._SelectionWait(
            listen_main.SessionController(), asyncio.Event()
        )
        plugin._selection_waits = {"chat-a": old}

        replacing = asyncio.create_task(plugin._replace_selection_wait("chat-a", new))
        await asyncio.sleep(0)
        self.assertTrue(old_controller.stopped)
        self.assertFalse(replacing.done())

        plugin._finish_selection_wait("chat-a", old)
        await replacing
        self.assertIs(plugin._selection_waits["chat-a"], new)

    async def test_stopping_plugin_refuses_a_new_selection_wait(self) -> None:
        plugin = object.__new__(listen_main.ListenMusicPlugin)
        plugin._selection_lock = asyncio.Lock()
        plugin._selection_waits = {}
        plugin._initialized = False
        selection = listen_main._SelectionWait(
            listen_main.SessionController(), asyncio.Event()
        )

        registered = await plugin._replace_selection_wait("chat-a", selection)

        self.assertFalse(registered)
        self.assertEqual(plugin._selection_waits, {})


if __name__ == "__main__":
    unittest.main()
