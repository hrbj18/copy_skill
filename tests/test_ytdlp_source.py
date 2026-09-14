"""Offline tests for :class:`YtDlpSource`.

No network is touched: a fake ``yt_dlp`` object is injected through the
``ydl_factory`` seam.  The tests cover metadata mapping, the ``yt-`` id prefix,
lazy per-candidate stream resolution, the empty-string-vs-exception contract,
the sensitive-field whitelist on the report, and graceful degradation when
yt-dlp is not importable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from douyin_intelligence import sources
from douyin_intelligence.replication_candidates import Candidate
from douyin_intelligence.sources import MediaResolutionError
from douyin_intelligence.sources import ytdlp as ytdlp_module
from douyin_intelligence.sources.ytdlp import SENSITIVE_REPORT_KEYS, YtDlpSource


class _FakeYDL:
    def __init__(self, opts, search_map, resolve_map, calls) -> None:
        self._opts = opts
        self._search_map = search_map
        self._resolve_map = resolve_map
        self._calls = calls

    def __enter__(self) -> "_FakeYDL":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def extract_info(self, url, download=False):
        self._calls.append(url)
        if url.startswith("ytsearch"):
            return {"entries": self._search_map.get(url, [])}
        value = self._resolve_map.get(url)
        if isinstance(value, Exception):
            raise value
        return value


def _factory(search_map=None, resolve_map=None):
    calls: list[str] = []
    search_map = search_map or {}
    resolve_map = resolve_map or {}

    def factory(opts):
        return _FakeYDL(opts, search_map, resolve_map, calls)

    return factory, calls


_SEARCH_ENTRIES = [
    {
        "id": "abc123",
        "title": "标题一",
        "uploader": "频道甲",
        "channel_id": "UC-1",
        "url": "https://www.youtube.com/watch?v=abc123",
        "duration": 42,
        "view_count": 1000,
        "timestamp": 1700000000,
    },
    {"id": "def456", "title": "标题二"},
    {"title": "没有 id 应被跳过"},
]


def test_search_maps_entries_to_candidates() -> None:
    factory, _ = _factory(search_map={"ytsearch10:猫咪": _SEARCH_ENTRIES})
    source = YtDlpSource(ydl_factory=factory)

    result = source.search(["猫咪"], 5, config={})

    assert result.status == "success"
    assert result.source == "ytdlp"
    assert result.keywords_requested == ["猫咪"]
    assert result.keywords_used == ["猫咪"]
    assert len(result.candidates) == 2  # the id-less entry is dropped

    first = result.candidates[0]
    assert isinstance(first, Candidate)
    assert first.video_id == "yt-abc123"
    assert ":" not in first.video_id  # never poisons a Windows video_path
    assert first.duration_seconds == 42.0
    assert first.duration_source == "ytdlp.duration"
    assert first.play_count == 1000
    assert first.author == "频道甲"
    assert first.author_hash == "UC-1"
    assert first.source_keyword == "猫咪"
    # Watch page (public), never a signed media URL.
    assert first.source_url == "https://www.youtube.com/watch?v=abc123"
    # The id-only second entry still yields a candidate with a constructed URL.
    assert result.candidates[1].video_id == "yt-def456"


def test_resolve_is_lazy_and_per_candidate() -> None:
    resolve_map = {
        "https://www.youtube.com/watch?v=abc123": {
            "formats": [
                {"url": "https://cdn.example/video.mp4", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a"},
            ]
        }
    }
    factory, calls = _factory(search_map={"ytsearch10:猫咪": _SEARCH_ENTRIES}, resolve_map=resolve_map)
    source = YtDlpSource(ydl_factory=factory)
    result = source.search(["猫咪"], 5, config={})

    # Discovery already ran once per keyword; nothing else yet.
    assert calls == ["ytsearch10:猫咪"]

    resolved = source.resolve_media_url(result.candidates[0])
    assert resolved == "https://cdn.example/video.mp4"
    # Exactly one extra extraction, for the resolved candidate only.
    assert calls == ["ytsearch10:猫咪", "https://www.youtube.com/watch?v=abc123"]

    # The second candidate was never requested -> still not extracted.
    assert source.resolve_media_url(result.candidates[0]) == "https://cdn.example/video.mp4"
    assert calls.count("https://www.youtube.com/watch?v=abc123") == 1  # cached


def test_resolve_returns_empty_string_when_no_media() -> None:
    factory, _ = _factory(search_map={"ytsearch10:猫咪": _SEARCH_ENTRIES}, resolve_map={})
    source = YtDlpSource(ydl_factory=factory)
    result = source.search(["猫咪"], 5, config={})
    # Extraction returns nothing usable -> "" (not an exception).
    assert source.resolve_media_url(result.candidates[0]) == ""
    # A candidate the source never saw also resolves to "".
    assert source.resolve_media_url(Candidate(video_id="unknown")) == ""


def test_resolve_raises_media_resolution_error_on_backend_failure() -> None:
    resolve_map = {
        "https://www.youtube.com/watch?v=abc123": RuntimeError("boom"),
    }
    factory, _ = _factory(search_map={"ytsearch10:猫咪": _SEARCH_ENTRIES}, resolve_map=resolve_map)
    source = YtDlpSource(ydl_factory=factory)
    result = source.search(["猫咪"], 5, config={})
    with pytest.raises(MediaResolutionError):
        source.resolve_media_url(result.candidates[0])


def _walk_keys(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield str(key)
            yield from _walk_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


def _walk_strings(obj):
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _walk_strings(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_strings(item)
    elif isinstance(obj, str):
        yield obj


def test_report_never_contains_sensitive_fields() -> None:
    factory, _ = _factory(search_map={"ytsearch10:猫咪": _SEARCH_ENTRIES})
    source = YtDlpSource(ydl_factory=factory)
    result = source.search(["猫咪"], 5, config={})

    keys = set(_walk_keys(result.report))
    assert keys.isdisjoint(SENSITIVE_REPORT_KEYS)
    # No direct/signed link of any kind leaks into the disk-bound report.
    assert all("http" not in value for value in _walk_strings(result.report))
    # Candidates keep only the *public watch page* -- never a signed URL.
    assert all("signed" not in (candidate.source_url or "") for candidate in result.candidates)


def test_import_failure_degrades_to_failed_status(monkeypatch) -> None:
    def boom():
        raise ImportError("no yt_dlp here")

    monkeypatch.setattr(ytdlp_module, "_import_ytdlp", boom)
    source = YtDlpSource()  # no injected factory -> tries the real import
    result = source.search(["猫咪"], 10, config={})
    assert result.status == "failed"
    assert "yt_dlp" in result.error
    assert result.candidates == []


def test_empty_keywords_is_empty_status() -> None:
    factory, calls = _factory()
    source = YtDlpSource(ydl_factory=factory)
    result = source.search([], 10, config={})
    assert result.status == "empty"
    assert calls == []


def test_source_timeout_is_configurable() -> None:
    source = YtDlpSource(timeout_seconds=7)
    assert source._timeout({}) == 7.0
    # Config key is honoured when the instance was not given an explicit value.
    config = {"jobs": {"material_replication": {"ytdlp_timeout_seconds": 33}}}
    assert YtDlpSource()._timeout(config) == 33.0


def test_registry_exposes_ytdlp_adapter() -> None:
    assert "ytdlp" in sources.known_sources()
    adapter = sources.get_source("ytdlp")
    assert isinstance(adapter, YtDlpSource)
