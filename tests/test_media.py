from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.media import (
    FfmpegUnavailableError,
    MediaError,
    MediaStore,
    MediaTooLargeError,
)
from core.models import ResolvedAudio


class FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _: int):
        for chunk in self._chunks:
            yield chunk


class FakeResponse:
    def __init__(
        self,
        chunks: list[bytes] | FakeContent,
        *,
        status: int = 200,
        content_type: str = "audio/mpeg",
        content_length: int | None = None,
    ) -> None:
        self.status = status
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.content = (
            chunks if isinstance(chunks, FakeContent) else FakeContent(chunks)
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


class GateContent(FakeContent):
    def __init__(self) -> None:
        super().__init__([])
        self.started = asyncio.Event()
        self.continue_download = asyncio.Event()

    async def iter_chunked(self, _: int):
        self.started.set()
        await self.continue_download.wait()
        yield b"audio-bytes"


def audio(**overrides: object) -> ResolvedAudio:
    values: dict[str, object] = {
        "url": "https://primary.example.test/audio.m4s",
        "mime_type": "audio/mpeg",
    }
    values.update(overrides)
    return ResolvedAudio(**values)


class MediaStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_backup_url_with_required_headers_and_deletes_after_release(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = FakeSession(
                [FakeResponse([], status=403), FakeResponse([b"audio-bytes"])]
            )
            store = MediaStore(session, directory)
            store._ffmpeg_path = "/bin/true"
            prepared = await store.prepare(
                audio(
                    backup_urls=("https://backup.example.test/audio.m4s",),
                    headers={
                        "Referer": "https://www.bilibili.com/video/BV1/",
                        "Cookie": "SESSDATA=x",
                    },
                ),
                filename_stem="晴天 - 周杰伦",
            )

            self.assertEqual(len(session.calls), 2)
            self.assertEqual(
                session.calls[1]["url"], "https://backup.example.test/audio.m4s"
            )
            headers = session.calls[1]["headers"]
            assert isinstance(headers, dict)
            self.assertEqual(headers["Referer"], "https://www.bilibili.com/video/BV1/")
            self.assertEqual(prepared.filename, "晴天 - 周杰伦.mp3")
            self.assertTrue(prepared.path.is_file())

            await store.release(prepared)
            await store.release(prepared)
            self.assertFalse(prepared.path.exists())
            await store.aclose()

    async def test_each_delivery_downloads_a_fresh_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = FakeSession([FakeResponse([b"first"]), FakeResponse([b"second"])])
            store = MediaStore(session, directory)
            store._ffmpeg_path = "/bin/true"

            first = await store.prepare(audio(), filename_stem="first")
            await store.release(first)
            second = await store.prepare(audio(), filename_stem="second")

            self.assertEqual(len(session.calls), 2)
            self.assertNotEqual(first.path, second.path)
            self.assertTrue(second.path.is_file())
            await store.release(second)
            await store.aclose()

    async def test_reclaims_partial_file_when_stream_exceeds_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MediaStore(
                FakeSession([FakeResponse([b"123456"])]),
                directory,
                max_bytes=5,
            )
            store._ffmpeg_path = "/bin/true"

            with self.assertRaises(MediaTooLargeError):
                await store.prepare(audio(), filename_stem="too-large")

            self.assertEqual(list(Path(directory).iterdir()), [])
            await store.aclose()

    async def test_reclaim_stale_removes_only_startup_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stale_path = Path(directory) / "previous-process.m4a"
            stale_path.write_bytes(b"stale")
            store = MediaStore(FakeSession([FakeResponse([b"audio"])]), directory)
            store._ffmpeg_path = "/bin/true"

            await store.reclaim_stale()
            self.assertFalse(Path(directory).exists())

            prepared = await store.prepare(audio(), filename_stem="fresh")
            with self.assertRaises(MediaError):
                await store.reclaim_stale()

            self.assertTrue(prepared.path.exists())
            await store.release(prepared)
            await store.aclose()

    async def test_reclaim_stale_reports_io_failure_and_can_be_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stale_path = Path(directory) / "previous-process.m4a"
            stale_path.write_bytes(b"stale")
            store = MediaStore(FakeSession([]), directory)

            with patch("core.media.shutil.rmtree", side_effect=PermissionError):
                with self.assertRaisesRegex(
                    MediaError, "无法清理上次运行遗留的媒体文件"
                ):
                    await store.reclaim_stale()

            await store.reclaim_stale()
            self.assertFalse(Path(directory).exists())
            await store.aclose()

    async def test_delivery_concurrency_is_held_until_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_content, second_content, third_content = (
                GateContent(),
                GateContent(),
                GateContent(),
            )
            session = FakeSession(
                [
                    FakeResponse(first_content),
                    FakeResponse(second_content),
                    FakeResponse(third_content),
                ]
            )
            store = MediaStore(session, directory, max_concurrent_deliveries=2)
            store._ffmpeg_path = "/bin/true"

            preparations = [
                asyncio.create_task(
                    store.prepare(audio(), filename_stem=f"song-{index}")
                )
                for index in range(3)
            ]
            await first_content.started.wait()
            await second_content.started.wait()
            await asyncio.sleep(0)
            self.assertEqual(len(session.calls), 2)
            self.assertFalse(third_content.started.is_set())

            first_content.continue_download.set()
            second_content.continue_download.set()
            first, second = await asyncio.gather(*preparations[:2])

            await asyncio.sleep(0)
            self.assertEqual(len(session.calls), 2)
            self.assertFalse(third_content.started.is_set())

            await store.release(first)
            await third_content.started.wait()
            third_content.continue_download.set()
            third = await preparations[2]

            self.assertEqual(len(session.calls), 3)
            self.assertEqual(len({item.path for item in (first, second, third)}), 3)
            await store.release(second)
            await store.release(third)
            await store.aclose()

    async def test_cancellation_reclaims_the_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            content = GateContent()
            store = MediaStore(
                FakeSession([FakeResponse(content), FakeResponse([b"replacement"])]),
                directory,
                max_concurrent_deliveries=1,
            )
            store._ffmpeg_path = "/bin/true"

            preparation = asyncio.create_task(
                store.prepare(audio(), filename_stem="cancelled")
            )
            await content.started.wait()
            preparation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await preparation

            self.assertEqual(list(Path(directory).iterdir()), [])
            replacement = await store.prepare(audio(), filename_stem="replacement")
            await store.release(replacement)
            await store.aclose()

    async def test_cancellation_after_registration_returns_its_delivery_slot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MediaStore(
                FakeSession([FakeResponse([b"first"]), FakeResponse([b"replacement"])]),
                directory,
                max_concurrent_deliveries=1,
            )
            store._ffmpeg_path = "/bin/true"
            original_finish = store._finish_preparation
            entered_finish = asyncio.Event()
            block_finish = True

            async def finish(task: asyncio.Task[object] | None) -> None:
                nonlocal block_finish
                if block_finish:
                    block_finish = False
                    entered_finish.set()
                    await asyncio.Event().wait()
                await original_finish(task)

            store._finish_preparation = finish  # type: ignore[method-assign]
            preparation = asyncio.create_task(
                store.prepare(audio(), filename_stem="cancelled-handoff")
            )
            await entered_finish.wait()
            preparation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await preparation

            self.assertEqual(list(Path(directory).iterdir()), [])
            replacement = await store.prepare(audio(), filename_stem="replacement")
            await store.release(replacement)
            await store.aclose()

    async def test_failed_preparation_returns_its_delivery_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MediaStore(
                FakeSession(
                    [FakeResponse([], status=500), FakeResponse([b"replacement"])]
                ),
                directory,
                max_concurrent_deliveries=1,
            )
            store._ffmpeg_path = "/bin/true"

            with self.assertRaises(MediaError):
                await store.prepare(audio(), filename_stem="failed")

            replacement = await store.prepare(audio(), filename_stem="replacement")
            await store.release(replacement)
            await store.aclose()

    async def test_remuxes_dash_audio_without_reencoding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MediaStore(
                FakeSession([FakeResponse([b"dash-audio"], content_type="audio/mp4")]),
                directory,
            )
            store._ffmpeg_path = "/bin/true"
            calls: list[tuple[Path, str]] = []

            async def remux(input_path: Path, token: str) -> Path:
                calls.append((input_path, token))
                output_path = Path(directory) / f"{token}.m4a"
                output_path.write_bytes(input_path.read_bytes())
                return output_path

            store._remux_to_m4a = remux  # type: ignore[method-assign]
            prepared = await store.prepare(
                audio(mime_type="audio/mp4", needs_remux=True),
                filename_stem="DASH song",
            )

            self.assertEqual(len(calls), 1)
            self.assertEqual(prepared.mime_type, "audio/mp4")
            self.assertEqual(prepared.path.suffix, ".m4a")
            self.assertEqual(prepared.filename, "DASH song.m4a")
            self.assertFalse(calls[0][0].exists())
            await store.release(prepared)
            await store.aclose()

    async def test_aclose_cancels_preparation_cleans_media_and_closes_store(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            content = GateContent()
            store = MediaStore(
                FakeSession([FakeResponse([b"ready"]), FakeResponse(content)]),
                directory,
            )
            store._ffmpeg_path = "/bin/true"
            ready = await store.prepare(audio(), filename_stem="ready")
            preparation = asyncio.create_task(
                store.prepare(audio(), filename_stem="pending")
            )
            await content.started.wait()

            await store.aclose()
            with self.assertRaises(asyncio.CancelledError):
                await preparation

            self.assertFalse(ready.path.exists())
            self.assertFalse(Path(directory).exists())
            self.assertEqual(store._delivery_slots._value, 2)
            with self.assertRaises(MediaError):
                await store.prepare(audio(), filename_stem="after-close")

    async def test_ffmpeg_is_required_before_network_io(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = FakeSession([FakeResponse([b"audio"])])
            store = MediaStore(session, directory, ffmpeg_binary="not-a-real-ffmpeg")

            with self.assertRaises(FfmpegUnavailableError):
                await store.prepare(audio(), filename_stem="song")

            self.assertEqual(session.calls, [])
            await store.aclose()


if __name__ == "__main__":
    unittest.main()
