"""Multi-source *download* wiring (``jobs.material_replication.sources``).

The candidate-pool side of the multi-source switch is covered by
``test_multisource_pool.py``.  This module covers the step that used to be
missing: turning a pooled candidate into a real download through **its own**
source adapter, and doing so **lazily** at download time.

Three contracts are pinned here:

* ``resolve_download_target`` -- the deliberate split between "no usable address"
  (``None``: a normal miss, one failed download, no alert) and a source-level
  failure (:class:`MediaResolutionError`: recorded on its own ``resolve`` stage);
* ``invoke_downloader`` -- the ``referer`` kwarg is forwarded *only* when the
  callable declares it, so the 3-arg offline fakes (and the legacy Douyin path)
  keep their exact historical invocation;
* end to end, a Bilibili candidate downloads with ``https://www.bilibili.com/``,
  while the legacy Douyin path still downloads with no referer override (the
  downloader keeps its historic ``https://www.douyin.com/`` header, byte for
  byte).

No test here touches the network: the Bilibili adapter is a stub driven through
the same ``sources.get_source`` seam ``test_multisource_pool.py`` patches.
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from douyin_intelligence.config import load_config
from douyin_intelligence.replication_candidates import Candidate
from douyin_intelligence.replication_pipeline import ReplicationDeps, run_material_replication
from douyin_intelligence.replication_selection import invoke_downloader, resolve_download_target
from douyin_intelligence.sources.base import (
    CompositeMediaResolver,
    DownloadTarget,
    MediaResolutionError,
    SourceResult,
)

BV_ID = "BV1uUbG6FEfb"
DOUYIN_ID = "7300000000000000001"
BILIBILI_REFERER = "https://www.bilibili.com/"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _sourced(
    video_id: str,
    source: str,
    *,
    duration: float = 60.0,
    title: str = "",
    digg: int = 100,
) -> Candidate:
    candidate = Candidate(
        video_id=video_id,
        title=title or f"标题-{video_id}",
        author="作者",
        digg_count=digg,
        duration_seconds=duration,
    )
    candidate.source = source
    return candidate


def _clone(candidate: Candidate) -> Candidate:
    clone = _sourced(
        candidate.video_id,
        candidate.source,
        duration=float(candidate.duration_seconds or 0.0),
        title=candidate.title,
        digg=int(candidate.digg_count or 0),
    )
    return clone


class _ScriptedResolver:
    """A :class:`MediaResolver` that returns/raises exactly what the test says."""

    def __init__(self, target: DownloadTarget | None = None, *, raises: Exception | None = None) -> None:
        self._target = target
        self._raises = raises
        self.calls: list[str] = []

    def resolve_target(self, candidate: Candidate) -> DownloadTarget | None:
        self.calls.append(candidate.video_id)
        if self._raises is not None:
            raise self._raises
        return self._target


# --------------------------------------------------------------------------- #
# ``resolve_download_target``: the None vs MediaResolutionError split
# --------------------------------------------------------------------------- #
def test_resolve_download_target_without_a_resolver_is_the_legacy_lookup() -> None:
    candidate = _sourced("v1", "douyin")
    # Hit -> the url verbatim, no referer (the downloader keeps its historic
    # ``https://www.douyin.com/`` header).
    assert resolve_download_target(None, candidate, {"v1": "https://signed.example/x"}) == (
        "https://signed.example/x",
        None,
    )
    # Miss -> the legacy empty string (NOT ``None``): the downloader then fails
    # exactly as it did before, and the caller counts one download failure.
    assert resolve_download_target(None, candidate, {}) == ("", None)


def test_resolve_download_target_uses_the_resolver_url_and_referer() -> None:
    candidate = _sourced(BV_ID, "bilibili")
    resolver = _ScriptedResolver(DownloadTarget(url="https://cdn.example/bv", referer=BILIBILI_REFERER))

    assert resolve_download_target(resolver, candidate, {}) == ("https://cdn.example/bv", BILIBILI_REFERER)
    assert resolver.calls == [BV_ID]


def test_resolve_download_target_none_target_is_a_normal_miss() -> None:
    candidate = _sourced(BV_ID, "bilibili")
    resolver = _ScriptedResolver(None)
    # ``None`` = no usable address: signalled up as ``None`` so the caller records
    # one "no_media_url" miss, never an error.
    assert resolve_download_target(resolver, candidate, {}) is None


def test_resolve_download_target_propagates_media_resolution_error() -> None:
    candidate = _sourced(BV_ID, "bilibili")
    resolver = _ScriptedResolver(raises=MediaResolutionError("b站接口 503"))
    # A source-level failure is *not* swallowed into a "no address" miss.
    with pytest.raises(MediaResolutionError):
        resolve_download_target(resolver, candidate, {})


# --------------------------------------------------------------------------- #
# ``invoke_downloader``: referer forwarded only when the callable declares it
# --------------------------------------------------------------------------- #
def test_invoke_downloader_forwards_referer_when_declared(tmp_path: Path) -> None:
    seen: list[dict] = []

    def four_arg(url, path, config, *, max_bytes=None, referer=None):
        seen.append({"url": url, "referer": referer, "max_bytes": max_bytes})

    invoke_downloader(four_arg, "u", tmp_path / "a.mp4", {}, None, BILIBILI_REFERER)
    assert seen == [{"url": "u", "referer": BILIBILI_REFERER, "max_bytes": None}]


def test_invoke_downloader_never_hands_a_kwarg_to_a_three_arg_callable(tmp_path: Path) -> None:
    called: list[str] = []

    def three_arg(url, path, config):
        called.append(url)

    # ``referer`` is requested but the callable does not declare it -> silently
    # dropped (the whole offline suite, and every older downloader, is 3-arg).
    invoke_downloader(three_arg, "u", tmp_path / "b.mp4", {}, None, BILIBILI_REFERER)
    assert called == ["u"]


def test_invoke_downloader_legacy_fast_path_skips_the_signature_probe(tmp_path: Path) -> None:
    """cap and referer both ``None`` -> the historic positional call, verbatim."""
    calls: list[tuple] = []

    class _Callable:
        def __call__(self, url, path, config):
            calls.append((url, path, config))

    payload = {"k": 1}
    invoke_downloader(_Callable(), "u", tmp_path / "c.mp4", payload, None, None)
    assert calls == [("u", tmp_path / "c.mp4", payload)]


def test_invoke_downloader_tolerates_an_uninspectable_callable(tmp_path: Path, monkeypatch) -> None:
    import douyin_intelligence.replication_selection as selection

    def _boom(*args, **kwargs):
        raise ValueError("no signature for you")

    monkeypatch.setattr(selection, "inspect", types.SimpleNamespace(signature=_boom))
    called: list[str] = []

    def three_arg(url, path, config):
        called.append(url)

    # An optional kwarg is requested but the signature cannot be read -> no
    # kwargs at all, and no crash.
    invoke_downloader(three_arg, "u", tmp_path / "d.mp4", {}, 1000, BILIBILI_REFERER)
    assert called == ["u"]


# --------------------------------------------------------------------------- #
# End to end through the pipeline (download-only path)
# --------------------------------------------------------------------------- #
class _StubSource:
    """A lazy source adapter with a canned URL map and an optional failure set."""

    def __init__(
        self,
        name: str,
        *,
        referer: str | None,
        urls: dict[str, str],
        candidates: list[Candidate],
        raises: dict[str, Exception] | None = None,
    ) -> None:
        self.name = name
        self.download_referer = referer
        self._urls = dict(urls)
        self._candidates = list(candidates)
        self._raises = dict(raises or {})
        self.resolved: list[str] = []

    def search(self, keywords, budget, *, config, run_id=None):
        return SourceResult(
            source=self.name,
            status="success",
            candidates=[_clone(candidate) for candidate in self._candidates],
            keywords_requested=list(keywords),
            keywords_used=list(keywords),
            report={},
        )

    def resolve_media_url(self, candidate: Candidate) -> str:
        self.resolved.append(candidate.video_id)
        if candidate.video_id in self._raises:
            raise self._raises[candidate.video_id]
        return self._urls.get(candidate.video_id, "")


class _BoomFace:
    """Any call proves the download-only path wrongly touched face detection."""

    backend = "opencv_yunet"

    def status(self):
        raise AssertionError("download-only 不应触碰人脸后端")

    def run(self, *args, **kwargs):
        raise AssertionError("download-only 不应运行人脸检测")


def _config(tmp_path: Path) -> dict:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    # Fake downloader bytes are not real media; isolate this module from the
    # real ffprobe+ffmpeg validation layer (it has its own dedicated module).
    config["jobs"]["material_replication"]["validation"] = {"enabled": False}
    return config


def _prober(path, config):
    return {"duration_seconds": 600.0, "width": 1080, "height": 1920, "codec": "h264"}


def _capture_downloader(record: list[dict]):
    def downloader(url, destination, config, *, max_bytes=None, referer=None):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"video-bytes")
        record.append({"url": url, "referer": referer, "max_bytes": max_bytes})

    return downloader


def test_bilibili_candidate_downloads_with_its_own_referer(tmp_path: Path) -> None:
    config = _config(tmp_path)
    material = config["jobs"]["material_replication"]
    material["sources"] = ["bilibili"]
    # Bilibili's long-form index: the per-source window is what admits this clip.
    material["source_duration_windows"] = {"bilibili": {"min_seconds": 10, "max_seconds": 1200}}

    stub = _StubSource(
        "bilibili",
        referer=BILIBILI_REFERER,
        urls={BV_ID: "https://cdn.example/bv"},
        candidates=[_sourced(BV_ID, "bilibili", duration=600.0, title="手搓机器鸭")],
    )

    downloads: list[dict] = []
    deps = ReplicationDeps(
        downloader=_capture_downloader(downloads), prober=_prober, face_detector=_BoomFace(),
    )

    with patch("douyin_intelligence.sources.get_source", return_value=stub):
        result = run_material_replication(
            config, "机器鸭", business_date="2026-09-17", download_only=True, deps=deps,
        )

    assert result["status"] in {"success", "partial"}
    assert [row["video_id"] for row in result["downloads"]] == [BV_ID]
    # The candidate was resolved lazily through its *own* adapter, and the
    # downloader received the Bilibili Referer.
    assert stub.resolved == [BV_ID]
    assert downloads and downloads[0]["url"] == "https://cdn.example/bv"
    assert downloads[0]["referer"] == BILIBILI_REFERER


def test_bilibili_no_address_is_a_normal_download_failure(tmp_path: Path) -> None:
    """``resolve_media_url`` -> "" (lazy, no address): one plain failure, no error."""
    config = _config(tmp_path)
    material = config["jobs"]["material_replication"]
    material["sources"] = ["bilibili"]
    material["source_duration_windows"] = {"bilibili": {"min_seconds": 10, "max_seconds": 1200}}

    stub = _StubSource(
        "bilibili",
        referer=BILIBILI_REFERER,
        urls={},  # no address for this candidate
        candidates=[_sourced(BV_ID, "bilibili", duration=600.0)],
    )
    downloads: list[dict] = []
    deps = ReplicationDeps(
        downloader=_capture_downloader(downloads), prober=_prober, face_detector=_BoomFace(),
    )

    with patch("douyin_intelligence.sources.get_source", return_value=stub):
        result = run_material_replication(
            config, "机器鸭", business_date="2026-09-17", download_only=True, deps=deps,
        )

    assert result["downloads"] == []
    assert downloads == []
    stages = [entry["stage"] for entry in result["failures"]]
    assert stages == ["no_media_url"]


def test_bilibili_source_failure_is_recorded_on_its_own_stage(tmp_path: Path) -> None:
    """``resolve_media_url`` raising -> a distinct ``resolve`` failure, not a miss."""
    config = _config(tmp_path)
    material = config["jobs"]["material_replication"]
    material["sources"] = ["bilibili"]
    material["source_duration_windows"] = {"bilibili": {"min_seconds": 10, "max_seconds": 1200}}

    stub = _StubSource(
        "bilibili",
        referer=BILIBILI_REFERER,
        urls={},
        candidates=[_sourced(BV_ID, "bilibili", duration=600.0)],
        raises={BV_ID: MediaResolutionError("b站接口 503")},
    )
    downloads: list[dict] = []
    deps = ReplicationDeps(
        downloader=_capture_downloader(downloads), prober=_prober, face_detector=_BoomFace(),
    )

    with patch("douyin_intelligence.sources.get_source", return_value=stub):
        result = run_material_replication(
            config, "机器鸭", business_date="2026-09-17", download_only=True, deps=deps,
        )

    assert result["downloads"] == []
    entry = next(entry for entry in result["failures"] if entry["stage"] == "resolve")
    assert "503" in entry["reason"]


def test_absent_sources_keeps_the_legacy_douyin_download_header(tmp_path: Path) -> None:
    """No ``sources`` key -> the Douyin path downloads with no referer override."""
    config = _config(tmp_path)
    # The conftest seam strips the shipped ``sources`` key, so this is the
    # legacy default run.
    assert "sources" not in config["jobs"]["material_replication"]

    row = {
        "aweme_id": DOUYIN_ID,
        "desc": "标题",
        "author": {"uid": "uid-a", "nickname": "作者A"},
        "create_time": "2026-09-11T08:00:00+08:00",
        "statistics": {"digg_count": 100, "comment_count": 1, "share_count": 1, "collect_count": 1},
        "duration": 60,
        "share_url": f"https://www.douyin.com/video/{DOUYIN_ID}",
        "video_download_url": "https://signed.example/1",
    }

    def collector(cfg, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None, **kwargs):
        source = Path(str(cfg.get("_project_root"))) / "raw" / "search_contents_1.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps([row], ensure_ascii=False), encoding="utf-8")
        before_sanitize([source])
        return {"status": "success", "keywords": keywords, "budget": budget}

    downloads: list[dict] = []
    deps = ReplicationDeps(
        collector=collector, downloader=_capture_downloader(downloads),
        prober=_prober, face_detector=_BoomFace(),
    )

    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )

    assert [entry["video_id"] for entry in result["downloads"]] == [DOUYIN_ID]
    assert downloads and downloads[0]["url"] == "https://signed.example/1"
    # No referer override: the real downloader keeps its historic
    # ``https://www.douyin.com/`` header -- byte for byte the pre-feature path.
    assert downloads[0]["referer"] is None


def test_composite_resolver_referer_is_what_the_downloader_receives(tmp_path: Path) -> None:
    """The real :class:`CompositeMediaResolver` (not a scripted double) end to end."""
    adapter = _StubSource(
        "bilibili",
        referer=BILIBILI_REFERER,
        urls={BV_ID: "https://cdn.example/bv"},
        candidates=[_sourced(BV_ID, "bilibili", duration=600.0)],
    )
    resolver = CompositeMediaResolver({"bilibili": adapter})
    target = resolver.resolve_target(_sourced(BV_ID, "bilibili"))
    assert target is not None and target.url == "https://cdn.example/bv"
    assert target.referer == BILIBILI_REFERER
    assert adapter.resolved == [BV_ID]
