from __future__ import annotations

from pathlib import Path
import sys
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
for path in (PLUGIN_ROOT, TEST_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from support import ensure_aiohttp

ensure_aiohttp()

from core.bilibili import (
    BilibiliClient,
    BilibiliVideoRef,
    derive_wbi_mixin_key,
    parse_bilibili_video_ref,
    sign_wbi_params,
)


class BilibiliVideoRefTests(unittest.TestCase):
    def test_parse_accepts_literal_ids_and_standard_video_urls(self) -> None:
        bvid = "BV1Q541167Qg"
        cases = (
            (bvid, BilibiliVideoRef(bvid=bvid)),
            ("请下载 bv1Q541167Qg", BilibiliVideoRef(bvid=bvid)),
            (
                f"https://www.bilibili.com/video/{bvid}/?p=2",
                BilibiliVideoRef(bvid=bvid),
            ),
            ("https://www.bilibili.com/video/AV170001", BilibiliVideoRef(aid=170001)),
        )

        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(parse_bilibili_video_ref(value), expected)

    def test_parse_uses_the_first_legal_token(self) -> None:
        self.assertEqual(
            parse_bilibili_video_ref("先 AV170001，后 BV1Q541167Qg"),
            BilibiliVideoRef(aid=170001),
        )
        self.assertEqual(
            parse_bilibili_video_ref("BV1Q541167Qg 和 av170001"),
            BilibiliVideoRef(bvid="BV1Q541167Qg"),
        )

    def test_parse_rejects_invalid_ids_and_unresolved_short_links(self) -> None:
        for value in (
            "BV1fixture",
            "BV1Q541167Qg0",
            "av0",
            "av170001suffix",
            "av99999999999999999999",
            "https://b23.tv/abcDef",
            None,
        ):
            with self.subTest(value=value):
                self.assertIsNone(parse_bilibili_video_ref(value))

    def test_reference_requires_exactly_one_valid_identifier(self) -> None:
        invalid_refs = (
            {},
            {"aid": 0},
            {"aid": 170001, "bvid": "BV1Q541167Qg"},
            {"bvid": "BV1fixture"},
        )
        for kwargs in invalid_refs:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    BilibiliVideoRef(**kwargs)


class FixtureBilibiliClient(BilibiliClient):
    async def _wbi_data(self, endpoint, params):
        if "search" in endpoint:
            return {
                "result": [
                    {
                        "type": "video",
                        "aid": 1,
                        "bvid": "BV1fixture",
                        "title": '<em class="keyword">晴天</em>',
                        "author": "周杰伦官方",
                        "duration": "4:29",
                        "typename": "MV",
                    },
                    {
                        "type": "video",
                        "aid": 2,
                        "bvid": "BV1unrelated",
                        "title": "晴天里的情感故事",
                        "author": "故事频道",
                        "duration": "4:29",
                        "typename": "情感",
                    },
                ]
            }
        if "view" in endpoint:
            return {
                "aid": 1,
                "bvid": "BV1fixture",
                "title": "周杰伦 - 晴天",
                "owner": {"name": "周杰伦官方"},
                "pages": [
                    {"cid": 2, "page": 1, "part": "晴天", "duration": 269},
                    {"cid": 3, "page": 2, "part": "晴天（伴奏）", "duration": 269},
                ],
            }
        return {
            "timelength": 269000,
            "dash": {
                "audio": [
                    {
                        "id": 30280,
                        "baseUrl": "https://cdn.example.test/low.m4s",
                        "backupUrl": ["https://backup.example.test/low.m4s"],
                        "bandwidth": 128000,
                        "mimeType": "audio/mp4",
                    },
                    {
                        "id": 30216,
                        "baseUrl": "https://cdn.example.test/high.m4s",
                        "backupUrl": ["https://backup.example.test/high.m4s"],
                        "bandwidth": 192000,
                        "mimeType": "audio/mp4",
                    },
                    {
                        "id": 30232,
                        "baseUrl": "https://cdn.example.test/too-high.m4s",
                        "bandwidth": 256000,
                        "mimeType": "audio/mp4",
                    },
                ]
            },
        }

    async def stream_headers(self, bvid: str):
        return {
            "Referer": f"https://www.bilibili.com/video/{bvid}/",
            "User-Agent": "fixture-agent",
            "Cookie": "SESSDATA=secret",
        }


class AidFixtureBilibiliClient(BilibiliClient):
    def __init__(self) -> None:
        super().__init__(session=object())
        self.view_requests: list[dict[str, int]] = []

    async def _wbi_data(self, endpoint, params):
        self.view_requests.append(dict(params))
        return {
            "aid": 170001,
            "bvid": "BV1Q541167Qg",
            "title": "规范视频",
            "owner": {"name": "测试账号"},
            "pages": [{"cid": 99, "page": 1, "part": "音频", "duration": 269}],
        }


class BilibiliProtocolTests(unittest.IsolatedAsyncioTestCase):
    def test_wbi_signing_fixture_filters_reserved_characters(self) -> None:
        key = derive_wbi_mixin_key(
            "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
            "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
        )
        self.assertEqual(key, "ea1db124af3c7062474693fa704f4ff8")
        self.assertEqual(
            sign_wbi_params({"foo": "a!b", "bar": "x y"}, key, timestamp=1700000000),
            {
                "bar": "x y",
                "foo": "ab",
                "wts": "1700000000",
                "w_rid": "abf34e0c2c2b5a3596151bdd92efafce",
            },
        )

    async def test_search_pages_dash_selection_and_headers(self) -> None:
        client = FixtureBilibiliClient(session=object())
        videos = await client.search_videos("晴天")
        self.assertEqual(videos[0].title, "晴天")
        self.assertEqual(
            [(video.bvid, video.title) for video in videos],
            [("BV1fixture", "晴天"), ("BV1unrelated", "晴天里的情感故事")],
        )

        video = await client.get_video(videos[0].bvid)
        self.assertEqual(
            [(page.cid, page.index, page.title) for page in video.pages],
            [(2, 1, "晴天"), (3, 2, "晴天（伴奏）")],
        )

        audio = await client.resolve_audio(video.bvid, video.pages[0].cid)
        self.assertEqual(audio.url, "https://cdn.example.test/high.m4s")
        self.assertEqual(audio.backup_urls, ("https://backup.example.test/high.m4s",))
        self.assertTrue(audio.needs_remux)
        self.assertFalse(hasattr(audio, "source"))
        self.assertEqual(
            audio.headers["Referer"], "https://www.bilibili.com/video/BV1fixture/"
        )

    async def test_get_video_by_aid_uses_server_canonical_bvid(self) -> None:
        client = AidFixtureBilibiliClient()

        video = await client.get_video_by_aid(170001)

        self.assertEqual(client.view_requests, [{"aid": 170001}])
        self.assertEqual(video.bvid, "BV1Q541167Qg")
        self.assertEqual([(page.cid, page.index) for page in video.pages], [(99, 1)])


if __name__ == "__main__":
    unittest.main()
