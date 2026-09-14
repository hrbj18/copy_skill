"""yt-dlp backed source adapter (YouTube and any site yt-dlp supports).

Design goals
------------
* **Discovery without downloading.**  ``search`` uses yt-dlp's ``ytsearchN:``
  pseudo-URL with ``extract_flat=True`` to get shallow metadata fast; the media
  stream itself is only resolved, lazily and per candidate, by
  ``resolve_media_url``.
* **Disk safety.**  :attr:`SourceResult.report` is *whitelisted by construction*:
  it contains only counts, keywords and status -- never a ``url``/``formats``/
  ``requested_downloads``/``webpage_url`` field or any other signed/direct link.
  The only place a stream URL ever exists is the return value of
  ``resolve_media_url``, which the caller keeps in memory.
* **Non-fatal degradation.**  If ``yt_dlp`` cannot be imported, ``search``
  returns ``status="failed"`` with an ``error`` message instead of raising, so a
  single broken source can never abort an aggregation run.

``yt_dlp`` is imported lazily (never at module import time) so that importing
the adapter package stays cheap and works even when yt-dlp is not installed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Sequence

from .base import MediaResolutionError, SourceResult

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from ..replication_candidates import Candidate


#: Default per-source socket timeout (seconds).  Overridable per instance or via
#: the optional ``jobs.material_replication.ytdlp_timeout_seconds`` config key.
DEFAULT_TIMEOUT_SECONDS = 120

#: Minimum number of entries requested per keyword (mirrors the crawler's floor).
MIN_PER_KEYWORD = 10

#: Fields that must never appear in a disk-bound report (defence in depth -- the
#: report is built by hand, but a regression that leaks a raw yt-dlp dict would
#: be caught here in tests).
SENSITIVE_REPORT_KEYS = frozenset(
    {
        "url",
        "formats",
        "requested_downloads",
        "requested_formats",
        "webpage_url",
        "webpage_url_basename",
        "manifest_url",
        "vcodec",
        "acodec",
    }
)


def _import_ytdlp() -> Any:
    """Import and return the ``yt_dlp`` module (kept as a seam for tests)."""
    import yt_dlp  # noqa: PLC0415  (deliberately lazy)

    return yt_dlp


class YtDlpSource:
    """A :class:`~douyin_intelligence.sources.base.SourceAdapter` over yt-dlp."""

    name = "ytdlp"
    #: The moment a cross-platform source is involved, the historical Douyin
    #: ``Referer`` becomes a real coupling (YouTube's edge 403s on it), so this
    #: source sends none -- see :class:`DownloadTarget`.
    download_referer: str | None = None

    def __init__(self, *, timeout_seconds: float | None = None, ydl_factory: Any = None) -> None:
        """Create the adapter.

        Args:
            timeout_seconds: per-source socket timeout; ``None`` uses
                :data:`DEFAULT_TIMEOUT_SECONDS` (or the config key when set).
            ydl_factory: test seam -- a callable ``factory(opts) -> context
                manager`` returning an object with ``extract_info``.  When
                provided, the real ``yt_dlp`` module is never imported.
        """
        self._timeout_seconds: float | None = (
            float(timeout_seconds) if timeout_seconds is not None else None
        )
        self._ydl_factory = ydl_factory
        #: video_id -> watch/page URL needed for the lazy second extraction.
        #: Kept in memory only; never placed on :class:`SourceResult`.
        self._pending: dict[str, str] = {}
        #: video_id -> resolved stream URL (memory-only cache).
        self._resolved: dict[str, str] = {}
        self._module: Any = None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def search(
        self,
        keywords: Sequence[str],
        budget: int,
        *,
        config: dict[str, Any],
        run_id: str | None = None,
    ) -> SourceResult:
        terms = [str(item).strip() for item in (keywords or []) if str(item).strip()]
        requested = list(terms)
        if not terms:
            return SourceResult(
                source=self.name,
                status="empty",
                candidates=[],
                keywords_requested=requested,
                keywords_used=[],
                report={"site": "youtube", "status": "empty", "keywords": [], "budget": int(budget or 0)},
                warnings=["未提供关键词，未执行搜索"],
            )

        # Fail soft (never raise) when yt-dlp is unavailable.
        if self._ydl_factory is None:
            try:
                self._module = _import_ytdlp()
            except Exception as exc:  # ImportError or a broken install
                return SourceResult(
                    source=self.name,
                    status="failed",
                    candidates=[],
                    keywords_requested=requested,
                    keywords_used=[],
                    report={
                        "site": "youtube",
                        "status": "failed",
                        "keywords": requested,
                        "budget": int(budget or 0),
                    },
                    error=f"yt_dlp 不可导入：{type(exc).__name__}: {exc}",
                )

        per_keyword = max(MIN_PER_KEYWORD, -(-int(budget or 0) // len(terms)))
        candidates: list[Candidate] = []
        warnings: list[str] = []
        used_keywords: list[str] = []
        returned = 0
        for keyword in terms:
            query = f"ytsearch{per_keyword}:{keyword}"
            used_keywords.append(keyword)
            try:
                entries = self._search_entries(query, config)
            except Exception as exc:  # one keyword failing must not kill the source
                warnings.append(
                    f"yt-dlp 搜索关键词「{keyword}」失败：{type(exc).__name__}: {str(exc)[:200]}"
                )
                continue
            for entry in entries:
                returned += 1
                mapped = self._entry_to_candidate(entry, keyword)
                if mapped is not None:
                    candidates.append(mapped)

        status = "success" if candidates else "no_match"
        report: dict[str, Any] = {
            "site": "youtube",
            "site_count": 1,
            "status": status,
            "keywords": used_keywords,
            "keywords_requested": requested,
            "budget": int(budget or 0),
            "per_keyword": per_keyword,
            "returned": returned,
            "mapped": len(candidates),
        }
        return SourceResult(
            source=self.name,
            status=status,
            candidates=candidates,
            keywords_requested=requested,
            keywords_used=used_keywords,
            report=report,
            warnings=warnings,
        )

    def resolve_media_url(self, candidate: "Candidate") -> str:
        video_id = str(getattr(candidate, "video_id", "") or "")
        if not video_id:
            return ""
        if video_id in self._resolved:
            return self._resolved[video_id]
        watch_url = self._pending.get(video_id, "")
        if not watch_url:
            return ""
        try:
            url = self._extract_media_url(watch_url, config={})
        except MediaResolutionError:
            raise
        except Exception as exc:  # backend/network/parse -> single-item failure
            raise MediaResolutionError(
                f"yt-dlp 取流失败：{video_id}：{type(exc).__name__}: {str(exc)[:200]}"
            ) from exc
        self._resolved[video_id] = url
        return url

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _timeout(self, config: dict[str, Any] | None) -> float:
        if self._timeout_seconds is not None:
            return self._timeout_seconds
        settings = ((config or {}).get("jobs") or {}).get("material_replication") or {}
        raw = settings.get("ytdlp_timeout_seconds")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return float(DEFAULT_TIMEOUT_SECONDS)
        return value if value > 0 else float(DEFAULT_TIMEOUT_SECONDS)

    def _open_ydl(self, opts: dict[str, Any]) -> Any:
        if self._ydl_factory is not None:
            return self._ydl_factory(opts)
        module = self._module if self._module is not None else _import_ytdlp()
        return module.YoutubeDL(opts)

    def _search_opts(self, config: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "noplaylist": True,
            "ignoreerrors": True,
            "socket_timeout": self._timeout(config),
        }

    def _resolve_opts(self, config: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": self._timeout(config),
        }

    def _search_entries(self, query: str, config: dict[str, Any] | None) -> list[dict[str, Any]]:
        with self._open_ydl(self._search_opts(config)) as ydl:
            info = ydl.extract_info(query, download=False)
        if not isinstance(info, dict):
            return []
        entries = info.get("entries")
        if entries is None:
            entries = [info]
        return [entry for entry in entries if isinstance(entry, dict)]

    def _extract_media_url(self, watch_url: str, config: dict[str, Any] | None) -> str:
        with self._open_ydl(self._resolve_opts(config)) as ydl:
            info = ydl.extract_info(watch_url, download=False)
        return self._pick_media_url(info)

    @staticmethod
    def _pick_media_url(info: Any) -> str:
        """Choose a progressive MP4 stream URL from a full extraction, else ``""``."""
        if not isinstance(info, dict):
            return ""
        formats = info.get("formats")
        if isinstance(formats, list):
            progressive = ""
            fallback = ""
            for fmt in formats:
                if not isinstance(fmt, dict):
                    continue
                url = fmt.get("url")
                if not isinstance(url, str) or not url:
                    continue
                if not fallback:
                    fallback = url
                if str(fmt.get("ext") or "") != "mp4":
                    continue
                has_video = str(fmt.get("vcodec") or "none") != "none"
                has_audio = str(fmt.get("acodec") or "none") != "none"
                if has_video and has_audio:
                    progressive = url
            if progressive:
                return progressive
            if fallback:
                return fallback
        direct = info.get("url")
        return direct if isinstance(direct, str) else ""

    def _entry_to_candidate(self, entry: dict[str, Any], keyword: str) -> "Candidate | None":
        from ..replication_candidates import Candidate  # local import avoids a cycle

        raw_id = str(entry.get("id") or "").strip()
        if not raw_id:
            return None
        # ``:`` is illegal in a Windows filename and would poison ``video_path``;
        # the ``yt-`` prefix keeps the id from colliding with Douyin ids and
        # survives as ``f"{video_id}.mp4"`` downstream.
        video_id = f"yt-{raw_id}"
        watch_url = str(entry.get("url") or "").strip()
        if not watch_url:
            watch_url = f"https://www.youtube.com/watch?v={raw_id}"
        self._pending[video_id] = watch_url
        return Candidate(
            video_id=video_id,
            title=str(entry.get("title") or ""),
            author=str(entry.get("uploader") or entry.get("channel") or ""),
            author_hash=str(entry.get("channel_id") or ""),
            source_url=f"https://www.youtube.com/watch?v={raw_id}",
            published_at=self._published_at(entry),
            play_count=self._as_int(entry.get("view_count")),
            duration_seconds=self._as_float(entry.get("duration")),
            duration_source="ytdlp.duration" if entry.get("duration") else "",
            source_keyword=str(keyword),
            # Explicit: ``Candidate.source`` defaults to ``"douyin"``, and this
            # field is the cross-source dedup/dispatch key.  The ``yt-`` id prefix
            # above already keeps ids from colliding, but the *label* must be
            # right too or an id-based fallback dispatch would pick Douyin.
            source=self.name,
            media_url_present=True,
        )

    @staticmethod
    def _published_at(entry: dict[str, Any]) -> str:
        timestamp = entry.get("timestamp") or entry.get("release_timestamp")
        try:
            value = int(timestamp)
        except (TypeError, ValueError):
            upload_date = str(entry.get("upload_date") or "")
            if len(upload_date) == 8 and upload_date.isdigit():
                return (
                    f"{upload_date[0:4]}-{upload_date[4:6]}-{upload_date[6:8]}T00:00:00+00:00"
                )
            return ""
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
