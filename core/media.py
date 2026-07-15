"""Short-lived local media preparation for voice and file delivery.

The plugin deliberately materializes one file for one delivery.  A caller owns
that file from :meth:`MediaStore.prepare` until :meth:`MediaStore.release`;
there is no media cache, shared-download state, or background expiry work.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import mimetypes
import os
from pathlib import Path
import secrets
import shutil
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .models import LocalMedia, ResolvedAudio


_MEBIBYTE = 1024 * 1024


class MediaError(RuntimeError):
    """Base error for a failure to materialize a playable local file."""


class MediaDownloadError(MediaError):
    """No remote URL produced a valid, bounded response."""


class MediaTooLargeError(MediaError):
    """The provider stream exceeded the selected delivery size limit."""


class FfmpegUnavailableError(MediaError):
    """The host cannot make an adapter-safe local audio file."""


class MediaRemuxError(MediaError):
    """ffmpeg could not place a DASH stream into an M4A container."""


@dataclass(frozen=True, slots=True)
class MediaLimits:
    """One delivery mode's bounded network-download and local-media budget."""

    max_bytes: int
    max_duration_ms: int | None
    download_timeout_seconds: float

    def __post_init__(self) -> None:
        if self.max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if self.max_duration_ms is not None and self.max_duration_ms < 1:
            raise ValueError("max_duration_ms must be positive or None")
        if self.download_timeout_seconds <= 0:
            raise ValueError("download_timeout_seconds must be positive")


VOICE_MEDIA_LIMITS = MediaLimits(
    max_bytes=25 * _MEBIBYTE,
    max_duration_ms=15 * 60 * 1000,
    download_timeout_seconds=180.0,
)
DOWNLOAD_MEDIA_LIMITS = MediaLimits(
    max_bytes=100 * _MEBIBYTE,
    max_duration_ms=None,
    download_timeout_seconds=900.0,
)


@dataclass(frozen=True, slots=True)
class MediaHealth:
    """The only host capability the plugin needs to expose to WebUI."""

    ffmpeg_available: bool

    def as_payload(self) -> dict[str, dict[str, str]]:
        if self.ffmpeg_available:
            return {"ffmpeg": {"state": "ready", "message": "ffmpeg 可用"}}
        return {
            "ffmpeg": {
                "state": "missing",
                "message": "未检测到 ffmpeg；听歌和下载不可用",
            }
        }


@dataclass(frozen=True, slots=True)
class _DownloadedFile:
    path: Path
    mime_type: str


@dataclass(slots=True)
class _DeliveryLease:
    """One semaphore permit owned by a preparation or prepared file."""

    released: bool = False


class MediaStore:
    """Bound the complete prepare-to-release lifecycle of local media.

    A permit is acquired before network I/O and stays with a prepared file until
    its sender calls :meth:`release`. This deliberately applies backpressure to
    slow adapters instead of accumulating files while they upload. Failed and
    cancelled preparations return their permit immediately; :meth:`aclose`
    returns every remaining permit while removing owned files.
    """

    def __init__(
        self,
        session: Any,
        media_dir: str | Path,
        *,
        max_concurrent_deliveries: int = 2,
        ffmpeg_binary: str = "ffmpeg",
    ) -> None:
        if max_concurrent_deliveries < 1:
            raise ValueError("max_concurrent_deliveries must be positive")

        self._session = session
        self._media_dir = Path(media_dir)
        self._delivery_slots = asyncio.Semaphore(max_concurrent_deliveries)
        self._state_lock = asyncio.Lock()
        self._preparations: set[asyncio.Task[Any]] = set()
        self._active_paths: dict[Path, _DeliveryLease] = {}
        self._has_started_preparation = False
        self._reclaiming_stale = False
        self._closed = False
        self._ffmpeg_path = shutil.which(ffmpeg_binary)

    @property
    def health(self) -> MediaHealth:
        return MediaHealth(ffmpeg_available=bool(self._ffmpeg_path))

    @property
    def ffmpeg_available(self) -> bool:
        return bool(self._ffmpeg_path)

    async def reclaim_stale(self) -> None:
        """Remove media left by an earlier process before this store is used.

        Recovery is intentionally explicit and startup-only. Calling it after
        any preparation has started is rejected rather than risking deletion of
        a live delivery file.
        """

        async with self._state_lock:
            self._raise_if_closed_locked()
            if self._has_started_preparation:
                raise MediaError("媒体准备已开始，不能清理启动遗留文件")
            if self._reclaiming_stale:
                raise MediaError("媒体目录正在清理启动遗留文件")
            self._reclaiming_stale = True
        try:
            try:
                await asyncio.to_thread(shutil.rmtree, self._media_dir)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise MediaError("无法清理上次运行遗留的媒体文件") from exc
        finally:
            async with self._state_lock:
                self._reclaiming_stale = False

    async def prepare(
        self,
        audio: ResolvedAudio,
        *,
        filename_stem: str,
        limits: MediaLimits = VOICE_MEDIA_LIMITS,
    ) -> LocalMedia:
        """Materialize ``audio`` as a unique, bounded temporary file."""

        if _duration_exceeds_limit(audio.duration_ms, limits):
            raise MediaError(_duration_limit_message(limits))
        if not self._ffmpeg_path:
            raise FfmpegUnavailableError("宿主机未安装 ffmpeg，暂时无法听歌或下载歌曲")

        task = await self._begin_preparation()
        token = secrets.token_hex(16)
        downloaded: _DownloadedFile | None = None
        output_path: Path | None = None
        lease: _DeliveryLease | None = None
        handed_off = False
        preparation_finished = False
        try:
            await self._delivery_slots.acquire()
            lease = _DeliveryLease()
            await self._ensure_open()
            try:
                # One delivery gets one network budget.  Backup CDN URLs are
                # alternate paths for the same stream, not fresh 15-minute
                # attempts that may extend a queued delivery without bound.
                downloaded = await asyncio.wait_for(
                    self._download_with_backups(audio, token, limits=limits),
                    timeout=limits.download_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise MediaDownloadError("音源下载超时") from exc
            if audio.needs_remux:
                output_path = await self._remux_to_m4a(downloaded.path, token)
                mime_type = "audio/mp4"
            else:
                output_path = downloaded.path
                mime_type = downloaded.mime_type
                downloaded = None

            size_bytes = output_path.stat().st_size
            if size_bytes > limits.max_bytes:
                raise _too_large_error(limits)

            media = LocalMedia(
                path=output_path,
                filename=_filename_for(filename_stem, mime_type),
                mime_type=mime_type,
                size_bytes=size_bytes,
            )
            await self._register_path(output_path, lease)
            # A cancellation here still has no caller able to release the file.
            await self._finish_preparation(task)
            preparation_finished = True
            handed_off = True
            output_path = None
            return media
        except asyncio.CancelledError:
            raise
        except MediaError:
            raise
        except Exception as exc:
            raise MediaError("无法处理音频文件") from exc
        finally:
            if downloaded is not None:
                _unlink_quietly(downloaded.path)
            if output_path is not None:
                _unlink_quietly(output_path)
            if lease is not None and not handed_off:
                await self._release_lease(lease)
            if not preparation_finished:
                await self._finish_preparation(task)

    async def release(self, media: LocalMedia) -> None:
        """Delete a successfully sent local file; repeated releases are harmless."""

        path = Path(media.path)
        async with self._state_lock:
            lease = self._active_paths.pop(path, None)
            if lease is not None:
                self._release_lease_locked(lease)
        if lease is not None:
            _unlink_quietly(path)

    async def aclose(self) -> None:
        """Cancel active preparation and remove all files owned by this store."""

        current = asyncio.current_task()
        async with self._state_lock:
            if self._closed:
                return
            self._closed = True
            pending = tuple(
                task
                for task in self._preparations
                if task is not current and not task.done()
            )
            active_paths = tuple(self._active_paths)
            active_leases = tuple(self._active_paths.values())
            self._active_paths.clear()
            for lease in active_leases:
                self._release_lease_locked(lease)

        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for path in active_paths:
            _unlink_quietly(path)
        await asyncio.to_thread(shutil.rmtree, self._media_dir, ignore_errors=True)

    async def _begin_preparation(self) -> asyncio.Task[Any] | None:
        task = asyncio.current_task()
        async with self._state_lock:
            self._raise_if_closed_locked()
            if self._reclaiming_stale:
                raise MediaError("媒体服务正在清理启动遗留文件")
            self._has_started_preparation = True
            if task is not None:
                self._preparations.add(task)
        return task

    async def _finish_preparation(self, task: asyncio.Task[Any] | None) -> None:
        if task is None:
            return
        async with self._state_lock:
            self._preparations.discard(task)

    async def _ensure_open(self) -> None:
        async with self._state_lock:
            self._raise_if_closed_locked()

    async def _register_path(self, path: Path, lease: _DeliveryLease) -> None:
        async with self._state_lock:
            self._raise_if_closed_locked()
            self._active_paths[path] = lease

    async def _release_lease(self, lease: _DeliveryLease) -> None:
        """Return a permit once, including after a cancelled handoff."""

        async with self._state_lock:
            for path, active_lease in tuple(self._active_paths.items()):
                if active_lease is lease:
                    self._active_paths.pop(path)
                    break
            self._release_lease_locked(lease)

    def _release_lease_locked(self, lease: _DeliveryLease) -> None:
        if lease.released:
            return
        lease.released = True
        self._delivery_slots.release()

    def _raise_if_closed_locked(self) -> None:
        if self._closed:
            raise MediaError("媒体服务已关闭")

    async def _download_with_backups(
        self,
        audio: ResolvedAudio,
        token: str,
        *,
        limits: MediaLimits,
    ) -> _DownloadedFile:
        last_error: Exception | None = None
        for index, url in enumerate((audio.url, *audio.backup_urls)):
            try:
                downloaded = await self._download_one(
                    url,
                    headers=audio.headers,
                    fallback_mime=audio.mime_type,
                    token=f"{token}-{index}",
                    limits=limits,
                )
                if not _is_audio_mime(
                    downloaded.mime_type, allow_video=audio.needs_remux
                ):
                    _unlink_quietly(downloaded.path)
                    raise MediaDownloadError("音源返回了非音频内容")
                return downloaded
            except asyncio.CancelledError:
                raise
            except MediaError as exc:
                last_error = exc
        if isinstance(last_error, MediaTooLargeError):
            raise last_error
        raise MediaDownloadError("没有可下载的音频流") from last_error

    async def _download_one(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        fallback_mime: str | None,
        token: str,
        limits: MediaLimits,
    ) -> _DownloadedFile:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MediaDownloadError("音源返回了无效地址")

        await asyncio.to_thread(self._media_dir.mkdir, parents=True, exist_ok=True)
        part_path = self._media_dir / f".{token}.part"
        _unlink_quietly(part_path)
        response_headers: Any = {}
        try:
            async with self._session.get(
                url,
                headers=dict(headers),
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(
                    connect=15,
                    sock_read=120,
                    total=limits.download_timeout_seconds,
                ),
            ) as response:
                status = int(getattr(response, "status", 0))
                if status < 200 or status >= 300:
                    raise MediaDownloadError(f"音源请求失败（HTTP {status}）")
                response_headers = getattr(response, "headers", {})
                content_length = _content_length(response_headers)
                if content_length is not None and content_length > limits.max_bytes:
                    raise _too_large_error(limits)

                size_bytes = 0
                with part_path.open("wb") as target:
                    async for chunk in response.content.iter_chunked(128 * 1024):
                        if not chunk:
                            continue
                        size_bytes += len(chunk)
                        if size_bytes > limits.max_bytes:
                            raise _too_large_error(limits)
                        target.write(chunk)
        except asyncio.CancelledError:
            _unlink_quietly(part_path)
            raise
        except MediaError:
            _unlink_quietly(part_path)
            raise
        except Exception as exc:
            _unlink_quietly(part_path)
            raise MediaDownloadError("音源下载失败") from exc

        try:
            if not part_path.is_file() or part_path.stat().st_size == 0:
                raise MediaDownloadError("音源返回了空音频文件")

            mime_type = _media_mime(response_headers, fallback_mime)
            output_path = self._media_dir / f"{token}{_suffix_for_mime(mime_type)}"
            _unlink_quietly(output_path)
            os.replace(part_path, output_path)
            return _DownloadedFile(path=output_path, mime_type=mime_type)
        except asyncio.CancelledError:
            _unlink_quietly(part_path)
            raise
        except MediaError:
            _unlink_quietly(part_path)
            raise
        except OSError as exc:
            _unlink_quietly(part_path)
            raise MediaDownloadError("无法保存音频文件") from exc

    async def _remux_to_m4a(self, input_path: Path, token: str) -> Path:
        assert self._ffmpeg_path is not None
        output_path = self._media_dir / f"{token}.m4a"
        _unlink_quietly(output_path)
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                self._ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(input_path),
                "-vn",
                "-c:a",
                "copy",
                str(output_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    await process.wait()
                except ProcessLookupError:
                    pass
            _unlink_quietly(output_path)
            raise
        except OSError as exc:
            _unlink_quietly(output_path)
            raise MediaRemuxError("无法启动 ffmpeg") from exc
        except Exception as exc:
            _unlink_quietly(output_path)
            raise MediaRemuxError("无法使用 ffmpeg 封装 Bilibili 音频") from exc

        if (
            process.returncode != 0
            or not output_path.is_file()
            or output_path.stat().st_size == 0
        ):
            _unlink_quietly(output_path)
            detail = stderr.decode("utf-8", "replace").strip()
            raise MediaRemuxError(
                "无法使用 ffmpeg 封装 Bilibili 音频"
                + (f"：{detail[:240]}" if detail else "")
            )
        return output_path


def _content_length(headers: Any) -> int | None:
    try:
        value = int(headers.get("Content-Length"))
    except (AttributeError, TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _duration_exceeds_limit(duration_ms: int | None, limits: MediaLimits) -> bool:
    return (
        duration_ms is not None
        and limits.max_duration_ms is not None
        and duration_ms > limits.max_duration_ms
    )


def _duration_limit_message(limits: MediaLimits) -> str:
    assert limits.max_duration_ms is not None
    duration_ms = limits.max_duration_ms
    if duration_ms % 60_000 == 0:
        limit_text = f"{duration_ms // 60_000} 分钟"
    elif duration_ms % 1_000 == 0:
        limit_text = f"{duration_ms // 1_000} 秒"
    else:
        limit_text = f"{duration_ms} 毫秒"
    return f"音频时长超过 {limit_text} 限制"


def _too_large_error(limits: MediaLimits) -> MediaTooLargeError:
    return MediaTooLargeError(
        f"音频文件超过 {_format_byte_limit(limits.max_bytes)} 限制"
    )


def _format_byte_limit(max_bytes: int) -> str:
    if max_bytes % _MEBIBYTE == 0:
        return f"{max_bytes // _MEBIBYTE} MiB"
    if max_bytes % 1024 == 0:
        return f"{max_bytes // 1024} KiB"
    return f"{max_bytes} B"


def _media_mime(headers: Any, fallback: str | None) -> str:
    raw = ""
    try:
        raw = str(headers.get("Content-Type") or "")
    except AttributeError:
        pass
    mime_type = raw.partition(";")[0].strip().lower()
    if mime_type and mime_type not in {
        "application/octet-stream",
        "binary/octet-stream",
    }:
        return mime_type
    if fallback:
        return fallback.partition(";")[0].strip().lower() or "audio/mpeg"
    return "audio/mpeg"


def _suffix_for_mime(mime_type: str) -> str:
    known = {
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/aac": ".aac",
        "audio/flac": ".flac",
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
    }
    if mime_type in known:
        return known[mime_type]
    guessed = mimetypes.guess_extension(mime_type, strict=False)
    return guessed if guessed and len(guessed) <= 8 else ".audio"


def _is_audio_mime(mime_type: str, *, allow_video: bool) -> bool:
    if mime_type.startswith("audio/"):
        return True
    return allow_video and mime_type in {"video/mp4", "application/mp4"}


def _filename_for(stem: str, mime_type: str) -> str:
    cleaned = "".join(
        "_" if char in '<>:"/\\|?*\x00' or ord(char) < 32 else char
        for char in stem.strip()
    ).strip(" .")
    compact = " ".join(cleaned.split())[:120].strip(" .")
    return f"{compact or 'music'}{_suffix_for_mime(mime_type)}"


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


__all__ = [
    "DOWNLOAD_MEDIA_LIMITS",
    "FfmpegUnavailableError",
    "MediaDownloadError",
    "MediaError",
    "MediaHealth",
    "MediaLimits",
    "MediaRemuxError",
    "MediaStore",
    "MediaTooLargeError",
    "VOICE_MEDIA_LIMITS",
]
