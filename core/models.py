"""Small, source-specific domain values used by the music workflow.

The models intentionally describe only the data needed to search, resolve and
deliver audio.  Song metadata such as lyrics, covers and comments belongs to
future, separate features rather than to these transport objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class BilibiliCandidate:
    """One user-selectable, playable Bilibili video page.

    Bilibili search returns a video, while audio is resolved for a concrete
    page (``cid``).  Keeping both identifiers in the candidate makes a
    selected search result directly playable and avoids a second, ambiguous
    page-selection step.  ``search_title`` retains the title returned by the
    search endpoint, which can contain useful wording not present in the
    video-detail title. ``uploader`` intentionally remains named as such:
    Bilibili does not reliably expose the recording artist in its video API.
    """

    bvid: str
    cid: int
    title: str
    uploader: str
    duration_ms: int
    page_title: str = ""
    search_title: str = ""

    def __post_init__(self) -> None:
        bvid = self.bvid.strip()
        title = self.title.strip()
        uploader = self.uploader.strip()
        page_title = self.page_title.strip()
        search_title = self.search_title.strip() or title

        if not bvid:
            raise ValueError("bvid must not be blank")
        if self.cid <= 0:
            raise ValueError("cid must be positive")
        if not title:
            raise ValueError("title must not be blank")
        if self.duration_ms < 0:
            raise ValueError("duration_ms must not be negative")

        object.__setattr__(self, "bvid", bvid)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "uploader", uploader)
        object.__setattr__(self, "page_title", page_title)
        object.__setattr__(self, "search_title", search_title)

    @property
    def candidate_id(self) -> str:
        """Opaque, stable identifier used inside short-lived search snapshots."""

        return f"{self.bvid}:{self.cid}"

    @property
    def display_title(self) -> str:
        """A compact title which makes a meaningful multi-page label visible."""

        if not self.page_title or self.page_title == self.title:
            return self.title
        return f"{self.title} - {self.page_title}"


@dataclass(frozen=True, slots=True)
class ResolvedAudio:
    """A short-lived remote stream and the request details needed to fetch it."""

    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    mime_type: str | None = None
    backup_urls: tuple[str, ...] | Sequence[str] = ()
    duration_ms: int | None = None
    needs_remux: bool = False

    def __post_init__(self) -> None:
        url = self.url.strip()
        if not url:
            raise ValueError("url must not be blank")
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ValueError("duration_ms must not be negative")

        headers = MappingProxyType(
            {
                str(name): str(value)
                for name, value in self.headers.items()
                if str(name).strip()
            }
        )
        backup_urls = tuple(
            dict.fromkeys(
                candidate
                for candidate in (str(item).strip() for item in self.backup_urls)
                if candidate and candidate != url
            )
        )

        object.__setattr__(self, "url", url)
        object.__setattr__(self, "headers", headers)
        object.__setattr__(self, "backup_urls", backup_urls)
        object.__setattr__(
            self, "mime_type", self.mime_type.strip() if self.mime_type else None
        )
        object.__setattr__(self, "needs_remux", bool(self.needs_remux))


@dataclass(frozen=True, slots=True)
class LocalMedia:
    """A downloaded temporary file ready for a chat adapter to send."""

    path: Path | str
    filename: str
    mime_type: str
    size_bytes: int

    def __post_init__(self) -> None:
        path = Path(self.path)
        filename = self.filename.strip()
        mime_type = self.mime_type.strip()

        if not filename:
            raise ValueError("filename must not be blank")
        if not mime_type:
            raise ValueError("mime_type must not be blank")
        if self.size_bytes < 0:
            raise ValueError("size_bytes must not be negative")

        object.__setattr__(self, "path", path)
        object.__setattr__(self, "filename", filename)
        object.__setattr__(self, "mime_type", mime_type)


@dataclass(frozen=True, slots=True)
class SearchSnapshot:
    """A session-scoped, expiring immutable candidate set.

    The snapshot preserves only the search result identity and ownership
    boundary. Chat intent, user-selection rules, and delivery mode belong to
    the AstrBot layer rather than to this source-neutral value.
    """

    search_id: str
    session_id: str
    query: str
    candidates: tuple[BilibiliCandidate, ...]
    created_at: float
    expires_at: float

    def __post_init__(self) -> None:
        search_id = self.search_id.strip()
        session_id = self.session_id.strip()
        query = self.query.strip()
        candidates = tuple(self.candidates)

        if not search_id:
            raise ValueError("search_id must not be blank")
        if not session_id:
            raise ValueError("session_id must not be blank")
        if not query:
            raise ValueError("query must not be blank")
        if not candidates:
            raise ValueError("candidates must not be empty")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")

        candidate_ids = [candidate.candidate_id for candidate in candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidates must have unique candidate IDs")

        object.__setattr__(self, "search_id", search_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "query", query)
        object.__setattr__(self, "candidates", candidates)

    def candidate(self, candidate_id: str) -> BilibiliCandidate | None:
        """Return a candidate only when its opaque ID is in this result."""

        expected_id = candidate_id.strip()
        return next(
            (
                candidate
                for candidate in self.candidates
                if candidate.candidate_id == expected_id
            ),
            None,
        )

    def candidate_at(self, position: int) -> BilibiliCandidate | None:
        """Resolve a one-based position from the textual result list."""

        if position < 1:
            return None
        return (
            self.candidates[position - 1] if position <= len(self.candidates) else None
        )
