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

from core.bilibili import BilibiliClient, derive_wbi_mixin_key, sign_wbi_params


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
        self.assertEqual(videos[0].duration_ms, 269000)
        self.assertEqual(
            [(video.bvid, video.category) for video in videos],
            [("BV1fixture", "MV"), ("BV1unrelated", "情感")],
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


if __name__ == "__main__":
    unittest.main()
