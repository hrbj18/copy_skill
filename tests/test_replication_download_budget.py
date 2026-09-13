"""Download budget: the second layer of cost control for material replication.

The prefilter (layer 1) drops obviously unsuitable candidates; the budget
(layer 2) then takes the top-N by relevance/heat within a byte ceiling so a
period's download cost tracks value instead of "download everything".

Covered here:

* count cap -> exactly ``max_count`` downloads;
* single-item cap -> rejected on ``Content-Length`` with zero body reads;
* byte cap -> bounds delivery on the delivered ledger (``stopped_by == "bytes"``
  only when the delivered ledger itself is full; otherwise the run ends
  ``queue_exhausted`` -- see P1d: an oversize item is skipped, not fatal);
* real-traffic cap -> ``transferred_bytes`` counts *every* attempt that wrote
  bytes (incl. files later rejected), so a download-only run stops with
  ``stopped_by == "transferred_bytes"`` instead of downloading forever;
* a failed download frees its slot (still reaches ``max_count`` successes);
* ``enabled=false`` -> every candidate is downloaded (pre-change behaviour);
* the report is complete and Chinese-safe;
* the budget is shared across the download-only and full-chain loops.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from douyin_intelligence import materials
from douyin_intelligence.config import ConfigurationError, load_config
from douyin_intelligence.materials import MediaTooLargeError
from douyin_intelligence.replication_pipeline import ReplicationDeps, run_material_replication
from douyin_intelligence.replication_selection import VisualMetrics, DownloadBudget


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _row(video_id: str, author: str, *, duration: float = 60.0, digg: int = 100, url: str = "", title: str | None = None) -> dict:
    return {
        "aweme_id": video_id,
        "desc": title if title is not None else f"标题-{video_id}",
        "author": {"uid": f"uid-{author}", "nickname": author},
        "create_time": "2026-09-11T08:00:00+08:00",
        "statistics": {"digg_count": digg, "comment_count": 10, "share_count": 5, "collect_count": 20},
        "duration": duration,
        "video_download_url": url or f"https://signed.example/{video_id}",
        "share_url": f"https://www.douyin.com/video/{video_id}",
    }


def _config(tmp_path: Path, *, budget: dict | None, prefilter_enabled: bool = False) -> dict:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    # Isolate the budget tests from the prefilter layer.
    config["jobs"]["material_replication"]["prefilter"] = {"enabled": prefilter_enabled}
    # ... and from the download-validation layer: these fakes write non-media
    # bytes, so a real ffprobe+ffmpeg check is not applicable here (the layer
    # has its own dedicated test module).
    config["jobs"]["material_replication"]["validation"] = {"enabled": False}
    # ... from the *delivered-bytes quota*: the shipped floor (70 MiB) would
    # otherwise keep the material loop scanning past ``target`` and could consume
    # more of the injected byte budget (e.g. the P1d remaining-budget guard, which
    # pins ``budget.stopped_by is None``).  Zero here == pre-quota behaviour.
    #
    # ... and pin the *material duration window* to its pre-quota values: this
    # module asserts the window mechanism (the duration_pre gate, the boundary
    # tolerance), so it must not drift when the shipped window is retuned for the
    # volume quota (15~180 s -> 15~300 s).  The material window has its own
    # coverage in test_replication_duration_metadata.
    material = config["jobs"]["material_replication"].setdefault("material_replica", {})
    material["min_delivered_bytes"] = 0
    material.update({"min_seconds": 15, "max_seconds": 180})
    if budget is None:
        config["jobs"]["material_replication"].pop("download_budget", None)
    else:
        config["jobs"]["material_replication"]["download_budget"] = budget
    return config


def _collector(rows: list[dict]):
    def collect(config, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None, **kwargs):
        source = Path(str(config.get("_project_root"))) / "raw" / "search_contents_1.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        before_sanitize([source])
        return {"status": "success", "keywords": keywords, "budget": budget}
    return collect


class _Ocr:
    def run(self, video, duration, cache_dir, temp_dir):
        return {"status": "no_text", "items": [], "sampled_frames": 10}


class _Transcriber:
    def run(self, video, cache_dir, temp_dir, **kwargs):
        # Stage is carried by ``cache_dir`` (script vs material sub-dir); since
        # P1a the source ``.mp4`` is shared across stages, so ``video`` no longer
        # identifies the stage.
        if "script" in str(cache_dir):
            return {"status": "success", "text": "字" * 200, "segments": [{"start": 0, "end": 5, "text": "开场"}]}
        return {"status": "no_speech", "text": "", "segments": []}


class _Face:
    backend = "opencv_yunet"

    def status(self):
        return {"backend": "opencv_yunet", "status": "ok", "model_present": True}

    def run(self, video, duration, cache_dir, temp_dir):
        return {
            "backend": "opencv_yunet", "status": "ok", "face_frame_ratio": 0.0, "max_face_area_ratio": 0.0,
            "face_class": "face_free", "sampled_frames": 10, "face_per_frame": [False] * 10,
            "sample_interval_seconds": 1,
        }


def _deps(tmp_path: Path, rows: list[dict], *, item_size: int = 2048, fail_ids: tuple[str, ...] = (), full: bool = False):
    downloaded: list[str] = []

    def downloader(url, destination, config, *, max_bytes=None):
        video_id = Path(destination).stem
        if video_id in fail_ids:
            raise OSError("simulated network failure")
        payload = b"x" * item_size
        if max_bytes is not None and len(payload) > max_bytes:
            raise MediaTooLargeError(
                f"视频声明体积 {len(payload)} 字节超过上限 {max_bytes}",
                declared_bytes=len(payload), limit=max_bytes,
            )
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(payload)
        downloaded.append(video_id)

    def prober(path, config):
        return {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}

    deps = ReplicationDeps(collector=_collector(rows), downloader=downloader, prober=prober)
    if full:
        deps.transcriber = _Transcriber()
        deps.ocr = _Ocr()
        deps.face_detector = _Face()
    return deps, downloaded


def _many_rows(count: int) -> list[dict]:
    return [_row(f"v{index:02d}", f"作者{index}", digg=100) for index in range(count)]


# --------------------------------------------------------------------------- #
# 1. Count cap
# --------------------------------------------------------------------------- #
def test_budget_caps_download_count(tmp_path: Path) -> None:
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9})
    rows = _many_rows(20)
    deps, downloaded = _deps(tmp_path, rows)
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    assert len(downloaded) == 12
    assert len(result["downloads"]) == 12
    output_dir = Path(result["output_dir"])
    block = json.loads((output_dir / "05-过程数据" / "download_budget.json").read_text(encoding="utf-8"))
    assert block["used"]["count"] == 12
    assert block["stopped_by"] == "count"
    assert block["limits"]["max_count"] == 12


# --------------------------------------------------------------------------- #
# 2. Single-item cap: zero body reads
# --------------------------------------------------------------------------- #
def test_download_video_rejects_oversize_without_reading_body(monkeypatch, tmp_path: Path) -> None:
    reads: list[int] = []
    urlopen_calls = {"count": 0}

    class _Response:
        headers = {"Content-Length": "99999999"}

        def read(self, size):
            reads.append(size)
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(*args, **kwargs):
        urlopen_calls["count"] += 1
        return _Response()

    monkeypatch.setattr(materials.urllib.request, "urlopen", fake_urlopen)
    config = load_config()
    target = tmp_path / "video.mp4"
    with pytest.raises(MediaTooLargeError):
        materials.download_video("https://signed.example/x", target, config, max_bytes=1024)
    # Zero waste: the body was never read, and oversize is not retried.
    assert reads == []
    assert urlopen_calls["count"] == 1
    assert not target.exists()
    assert not (tmp_path / "video.mp4.part").exists()


def test_download_video_streaming_cap_catches_missing_content_length(monkeypatch, tmp_path: Path) -> None:
    class _Response:
        headers: dict = {}

        def read(self, size):
            return b"y" * size

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(materials.urllib.request, "urlopen", lambda *a, **k: _Response())
    config = load_config()
    target = tmp_path / "video.mp4"
    with pytest.raises(MediaTooLargeError):
        materials.download_video("https://signed.example/x", target, config, max_bytes=2 * 1024 * 1024)
    assert not (tmp_path / "video.mp4.part").exists()


# --------------------------------------------------------------------------- #
# 3. Byte cap
# --------------------------------------------------------------------------- #
def test_budget_bounds_delivery_on_total_bytes(tmp_path: Path) -> None:
    config = _config(tmp_path, budget={
        "enabled": True, "max_count": 100, "max_bytes": 100_000, "max_item_bytes": 200_000,
    })
    rows = _many_rows(20)
    deps, downloaded = _deps(tmp_path, rows, item_size=40_000)
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    output_dir = Path(result["output_dir"])
    block = json.loads((output_dir / "05-过程数据" / "download_budget.json").read_text(encoding="utf-8"))
    # P1d: the third candidate (40k) exceeds the 20k *remaining* budget and is now
    # a plain skip, not a stop -- one oversize must not abort the scan.  Every
    # later candidate is likewise oversize (all 40k > the 20k left), so the loop
    # runs to the end and reports ``queue_exhausted`` instead of ``bytes``.  The
    # delivered result is unchanged: 40k + 40k fits, the rest do not.
    assert block["stopped_by"] == "queue_exhausted"
    assert block["used"]["count"] == 2
    assert block["used"]["bytes"] <= 100_000


# --------------------------------------------------------------------------- #
# 4. A failed download frees its slot
# --------------------------------------------------------------------------- #
def test_budget_frees_slot_for_failed_download(tmp_path: Path) -> None:
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9})
    rows = _many_rows(20)
    # "v-fail" sorts first (id order) and its download fails -> must be replaced.
    rows[7]["aweme_id"] = "v-fail"
    rows[7]["video_download_url"] = "https://signed.example/boom"
    deps, downloaded = _deps(tmp_path, rows, fail_ids=("v-fail",))
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    assert len(downloaded) == 12, downloaded
    assert "v-fail" not in downloaded
    output_dir = Path(result["output_dir"])
    manifest = json.loads((output_dir / "清单.json").read_text(encoding="utf-8"))
    assert any(item["video_id"] == "v-fail" for item in manifest["download_failures"])
    block = json.loads((output_dir / "05-过程数据" / "download_budget.json").read_text(encoding="utf-8"))
    assert block["used"]["count"] == 12


# --------------------------------------------------------------------------- #
# 5. enabled=false -> download everything (unchanged behaviour)
# --------------------------------------------------------------------------- #
def test_budget_disabled_downloads_everything(tmp_path: Path) -> None:
    config = _config(tmp_path, budget={"enabled": False, "max_count": 3, "max_bytes": 1, "max_item_bytes": 1})
    rows = _many_rows(20)
    deps, downloaded = _deps(tmp_path, rows)
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    assert len(downloaded) == 20
    output_dir = Path(result["output_dir"])
    assert not (output_dir / "05-过程数据" / "download_budget.json").exists()
    manifest = json.loads((output_dir / "清单.json").read_text(encoding="utf-8"))
    assert "download_budget" not in manifest
    # The disabled path must not even add the budget-only field to downloads.
    assert all("relevance_score" not in item for item in manifest["downloads"])
    run_log = json.loads((output_dir / "05-过程数据" / "run_log.json").read_text(encoding="utf-8"))
    assert "download_budget" not in run_log
    readme = (output_dir / "00-交付说明.md").read_text(encoding="utf-8")
    assert "## 下载预算" not in readme


def test_budget_absent_from_config_is_a_noop() -> None:
    config = load_config()
    config["jobs"]["material_replication"].pop("download_budget", None)
    assert DownloadBudget.from_config(config) is None


# --------------------------------------------------------------------------- #
# 6. Report completeness / Chinese safety / readme section
# --------------------------------------------------------------------------- #
def test_budget_report_is_complete_and_readable(tmp_path: Path) -> None:
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": 157286400, "max_item_bytes": 31457280})
    rows = _many_rows(20)
    deps, _ = _deps(tmp_path, rows)
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    output_dir = Path(result["output_dir"])
    raw = (output_dir / "05-过程数据" / "download_budget.json").read_text(encoding="utf-8")
    block = json.loads(raw)  # valid JSON
    assert set(block) >= {"enabled", "limits", "used", "ranking", "stopped_by", "selected", "skipped"}
    assert set(block["limits"]) == {"max_count", "max_bytes", "max_item_bytes"}
    assert block["ranking"]["order"] == ["relevance", "heat_score", "video_id"]
    # The "why the top-N" explanation must state the visual-quality limitation,
    # and must not be mojibake.
    assert "画面质量" in block["ranking"]["note"]
    assert "\\u" not in raw
    assert len(block["selected"]) == 12

    manifest = json.loads((output_dir / "清单.json").read_text(encoding="utf-8"))
    assert manifest["download_budget"]["used"]["count"] == 12
    readme = (output_dir / "00-交付说明.md").read_text(encoding="utf-8")
    assert "## 下载预算" in readme
    assert "为什么这几条值得下" in readme
    assert "排序限制" in readme


# --------------------------------------------------------------------------- #
# Ranking: relevance -> heat -> video_id
# --------------------------------------------------------------------------- #
def test_download_order_follows_relevance_then_heat(tmp_path: Path) -> None:
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9})
    rows = [
        _row("v-low", "作者A", digg=10),
        _row("v-relevant", "作者B", digg=5, title="苹果折叠屏手机 铰链实拍"),
        _row("v-hot", "作者C", digg=999),
    ]
    deps, downloaded = _deps(tmp_path, rows)
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    # The relevance hit wins even though it has the lowest heat.
    assert downloaded[0] == "v-relevant"
    scores = {item["video_id"]: item["relevance_score"] for item in result["downloads"]}
    assert scores["v-relevant"] > scores["v-hot"]


# --------------------------------------------------------------------------- #
# Shared budget across the full chain
# --------------------------------------------------------------------------- #
def _visual_ok(*args, **kwargs):
    return VisualMetrics(sampled_frames=10, motion_frame_ratio=0.9, ocr_text_frame_ratio=0.0, visual_ok=True)


def test_budget_is_shared_across_script_and_material(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path, budget={"enabled": True, "max_count": 2, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9})
    monkeypatch.setattr("douyin_intelligence.replication_pipeline.media_tool_available", lambda config, name: True)
    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics", _visual_ok)
    rows = _many_rows(5)
    deps, downloaded = _deps(tmp_path, rows, full=True)
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", deps=deps)
    output_dir = Path(result["output_dir"])
    block = json.loads((output_dir / "05-过程数据" / "download_budget.json").read_text(encoding="utf-8"))
    # The delivered ledger is shared across both loops AND idempotent per
    # video_id: the script replica (v00) is re-selected as a material source but
    # is one delivered file, so it consumes exactly one slot.  The run therefore
    # delivers ``v00 + v01`` and stops on ``count`` -- never exceeding
    # ``max_count`` *distinct* deliveries.
    assert block["used"]["count"] == 2
    assert block["stopped_by"] == "count"
    chosen_ids = [item["video_id"] for item in block["selected"]]
    assert chosen_ids == ["v00", "v01"]
    assert len(set(chosen_ids)) == block["used"]["count"]
    # The multi-stage invariant: v00 is *fetched* twice (script stage + material
    # stage keep separate copies) yet appears in ``selected`` once.
    assert downloaded.count("v00") == 2
    assert chosen_ids.count("v00") == 1


# --------------------------------------------------------------------------- #
# 7. Cache hit must obey the cap (regression: the >1KB short-circuit used to
#    ``return`` before the max_bytes checks, so a pre-existing oversize file in
#    the never-reclaimed ``data/media/material-replication/material`` tree was
#    delivered into 04-原片 while blowing both the per-item and the run cap).
# --------------------------------------------------------------------------- #
_VALID_PROBE = {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}


def _seed_material_cache(config: dict, video_id: str, size: int) -> Path:
    """Pre-place a cached scene file where the downloader/link would look."""
    from douyin_intelligence.replication_theme import project_path

    media_root = config["jobs"]["material_replication"]["media_root"]
    path = project_path(config, media_root) / "material" / f"{video_id}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"c" * size)
    return path


def _source_bytes(output_dir: Path) -> int:
    return sum(item.stat().st_size for item in (output_dir / "04-原片").glob("*") if item.is_file())


def test_download_video_cache_hit_over_cap_raises_without_network(monkeypatch, tmp_path: Path) -> None:
    """A cached file larger than the cap must raise, not silently return."""
    config = load_config()
    target = tmp_path / "cached.mp4"
    target.write_bytes(b"c" * 5000)  # >1024 => historically short-circuited

    def _no_network(*args, **kwargs):
        raise AssertionError("缓存命中超限时不得发起网络请求")

    monkeypatch.setattr(materials.urllib.request, "urlopen", _no_network)
    with pytest.raises(MediaTooLargeError):
        materials.download_video("https://signed.example/x", target, config, max_bytes=1000)
    # The oversize cache file is left untouched; it is not "delivered".
    assert target.stat().st_size == 5000


def test_download_video_cache_hit_within_cap_still_skips_download(monkeypatch, tmp_path: Path) -> None:
    """The valuable cache optimisation is preserved when the file fits."""
    config = load_config()
    target = tmp_path / "cached.mp4"
    target.write_bytes(b"c" * 5000)

    def _no_network(*args, **kwargs):
        raise AssertionError("缓存命中未超限时应跳过下载，不得发起网络请求")

    monkeypatch.setattr(materials.urllib.request, "urlopen", _no_network)
    materials.download_video("https://signed.example/x", target, config, max_bytes=10000)
    assert target.stat().st_size == 5000


def test_cache_hit_over_budget_is_not_delivered(tmp_path: Path) -> None:
    """End-to-end: a pre-seeded oversize cache file must not reach 04-原片.

    Drives the *real* ``materials.download_video`` (no stub downloader) so the
    cache short-circuit is actually exercised; the prober is stubbed only so no
    ffprobe runs on the fake bytes.
    """
    max_bytes, max_item = 1000, 400
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": max_bytes, "max_item_bytes": max_item})
    rows = [_row("v0000", "作者A")]
    _seed_material_cache(config, "v0000", 5000)
    deps = ReplicationDeps(collector=_collector(rows), prober=lambda path, config: dict(_VALID_PROBE))

    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    output_dir = Path(result["output_dir"])
    block = json.loads((output_dir / "05-过程数据" / "download_budget.json").read_text(encoding="utf-8"))
    # The acceptance invariant the user cares about: neither the ledger nor the
    # on-disk delivery may exceed the cap.
    assert block["used"]["bytes"] <= max_bytes
    assert block["used"]["bytes"] == 0
    assert result["downloads"] == []
    assert _source_bytes(output_dir) == 0
    assert any(item["stage"] in {"budget_item", "budget_bytes"} for item in block["skipped"])


def test_cache_hit_over_total_budget_is_skipped_not_stopped(tmp_path: Path) -> None:
    """A cache hit that overflows the *remaining* budget is skipped, not fatal (P1d).

    ``max_item_bytes`` is huge, so the 5000-byte cached item is refused only
    because it would overflow the 1000-byte run ceiling.  Under P1d that is a
    plain skip (``remaining_bytes() > 0``), so the scan continues instead of
    aborting; the run ends with nothing deliverable and reports
    ``queue_exhausted`` -- never the terminal ``bytes`` stop it used to emit.
    """
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": 1000, "max_item_bytes": 10 ** 9})
    rows = [_row("v0000", "作者A"), _row("v0001", "作者B")]
    _seed_material_cache(config, "v0000", 5000)
    deps = ReplicationDeps(collector=_collector(rows), prober=lambda path, config: dict(_VALID_PROBE))

    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    output_dir = Path(result["output_dir"])
    block = json.loads((output_dir / "05-过程数据" / "download_budget.json").read_text(encoding="utf-8"))
    assert block["used"]["bytes"] == 0
    assert block["stopped_by"] == "queue_exhausted"
    assert _source_bytes(output_dir) == 0


def test_cache_hit_within_budget_is_counted_not_redownloaded(tmp_path: Path) -> None:
    """A cache hit that fits still occupies a slot and its real bytes."""
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9})
    rows = [_row("v0000", "作者A")]
    _seed_material_cache(config, "v0000", 5000)
    deps = ReplicationDeps(collector=_collector(rows), prober=lambda path, config: dict(_VALID_PROBE))

    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    output_dir = Path(result["output_dir"])
    block = json.loads((output_dir / "05-过程数据" / "download_budget.json").read_text(encoding="utf-8"))
    assert block["used"]["count"] == 1
    assert block["used"]["bytes"] == 5000  # accounted, not free
    assert _source_bytes(output_dir) == 5000


# --------------------------------------------------------------------------- #
# 9. Config validation: a literal 0 is illegal; "off" is enabled=false only.
# --------------------------------------------------------------------------- #
def test_config_rejects_zero_budget_limits(tmp_path: Path) -> None:
    base = load_config()
    base["jobs"]["material_replication"]["download_budget"] = {
        "enabled": True, "max_count": 12, "max_bytes": 157286400, "max_item_bytes": 31457280,
    }
    for key in ("max_count", "max_bytes", "max_item_bytes"):
        base["jobs"]["material_replication"]["download_budget"][key] = 0
        bad = tmp_path / f"cfg-{key}.json"
        bad.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ConfigurationError):
            load_config(bad)
        base["jobs"]["material_replication"]["download_budget"][key] = 12


def _load_with_budget(tmp_path: Path, budget_marker: dict | None):
    """Write the shipped config with a given ``download_budget`` block and load it."""
    base = load_config()
    if budget_marker is None:
        base["jobs"]["material_replication"].pop("download_budget", None)
    else:
        base["jobs"]["material_replication"]["download_budget"] = budget_marker
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    return load_config(path)


def test_config_budget_disabled_skips_cap_validation(tmp_path: Path) -> None:
    """Turning the budget off must never be blocked by the cap checks."""
    # 1. {"enabled": false} (caps absent) -> accepted, budget not armed.
    loaded = _load_with_budget(tmp_path, {"enabled": False})
    assert DownloadBudget.from_config(loaded) is None

    # 2. {"enabled": false} with all-zero caps -> still accepted, budget not armed.
    loaded = _load_with_budget(
        tmp_path, {"enabled": False, "max_count": 0, "max_bytes": 0, "max_item_bytes": 0}
    )
    assert DownloadBudget.from_config(loaded) is None

    # 4. section fully absent -> no-op, accepted, not armed.
    loaded = _load_with_budget(tmp_path, None)
    assert DownloadBudget.from_config(loaded) is None

    # 3. {"enabled": true} with a 0 cap -> rejected.
    with pytest.raises(ConfigurationError):
        _load_with_budget(
            tmp_path,
            {"enabled": True, "max_count": 0, "max_bytes": 157286400, "max_item_bytes": 31457280},
        )


def test_shipped_config_budget_still_valid() -> None:
    """The factory config (enabled: true + all three caps) keeps loading and armed."""
    budget = DownloadBudget.from_config(load_config())
    assert budget is not None
    assert (budget.max_count, budget.max_bytes, budget.max_item_bytes) == (20, 262144000, 83886080)


# --------------------------------------------------------------------------- #
# 10. Real-traffic byte accounting: ``transferred_bytes``
#
# The 873 MB complaint is about *bandwidth*, not about what ends up in
# ``04-原片``.  A download-only run that keeps pulling files it later rejects
# must still stop once the wire budget is spent; the delivered ledger alone
# stays at 0 and would let such a run download forever.
# --------------------------------------------------------------------------- #
def _rejecting_validator(video_path, config, **kwargs):
    """Inject a validation verdict that rejects every file (so it is never delivered)."""
    return {
        "video_id": Path(video_path).stem,
        "conclusion": "undecodable",
        "passed": False,
        "error": "test: simulated bad file",
    }


def _validation_config(tmp_path: Path, *, budget: dict) -> dict:
    """A budget config with the download-validation layer *on*."""
    config = _config(tmp_path, budget=budget)
    config["jobs"]["material_replication"]["validation"] = {
        "enabled": True, "full_decode": True, "decode_time_budget_seconds": 20,
        "duration_tolerance": 0.05, "require_metadata_duration": False, "cache_attestation": True,
    }
    return config


def _tracking_deps(
    tmp_path: Path,
    rows: list[dict],
    *,
    item_size: int = 2048,
    validator=None,
    fail_ids: tuple[str, ...] = (),
    oversize_ids: tuple[str, ...] = (),
):
    """A fake downloader that records the bytes it *actually wrote*."""
    downloaded: list[str] = []
    written: dict[str, int] = {}

    def downloader(url, destination, config, *, max_bytes=None):
        video_id = Path(destination).stem
        if video_id in fail_ids:
            raise OSError("simulated network failure")  # zero bytes written
        if video_id in oversize_ids:
            raise MediaTooLargeError(
                "视频声明体积超限", declared_bytes=10 ** 9, limit=max_bytes or 0,
            )  # rejected before the body is read -> zero bytes written
        payload = b"x" * item_size
        if max_bytes is not None and len(payload) > max_bytes:
            raise MediaTooLargeError(
                f"视频声明体积 {len(payload)} 字节超过上限 {max_bytes}",
                declared_bytes=len(payload), limit=max_bytes,
            )
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(payload)
        written[video_id] = len(payload)
        downloaded.append(video_id)

    def prober(path, config):
        return {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}

    deps = ReplicationDeps(collector=_collector(rows), downloader=downloader, prober=prober, validator=validator)
    return deps, downloaded, written


def _budget_block_of(result: dict) -> dict:
    output_dir = Path(result["output_dir"])
    return json.loads((output_dir / "05-过程数据" / "download_budget.json").read_text(encoding="utf-8"))


def test_rejected_downloads_still_charge_transferred_bytes(tmp_path: Path) -> None:
    item = 2048
    config = _validation_config(tmp_path, budget={
        "enabled": True, "max_count": 100, "max_bytes": 3 * item, "max_item_bytes": 10 ** 9,
    })
    rows = _many_rows(10)
    deps, downloaded, written = _tracking_deps(tmp_path, rows, item_size=item, validator=_rejecting_validator)
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)

    block = _budget_block_of(result)
    used = block["used"]
    # Nothing was delivered (every file was rejected) ...
    assert used["count"] == 0
    assert used["delivered_bytes"] == 0
    # ... yet the *real traffic* is fully charged: exactly the three files whose
    # bytes were written, no more.
    assert used["transferred_bytes"] == 3 * item
    assert used["transferred_bytes"] == sum(written.values())
    assert used["transferred_bytes"] <= 3 * item  # core invariant: never exceeds the ceiling
    assert block["stopped_by"] == "transferred_bytes"
    assert len(downloaded) == 3, "the 4th candidate is refused, never downloaded"


def test_zero_byte_failures_are_not_charged(tmp_path: Path) -> None:
    """HTTP/network failures and pre-body oversize rejections cost zero wires bytes."""
    # max_item_bytes < the run ceiling so the oversize is a per-item *skip*, not
    # a run-budget stop: only the per-item branch is being exercised here.
    config = _config(tmp_path, budget={"enabled": True, "max_count": 100, "max_bytes": 10 ** 9, "max_item_bytes": 2000})
    rows = _many_rows(4)  # v00..v03, sorted by video_id
    deps, downloaded, written = _tracking_deps(
        tmp_path, rows, item_size=1000, fail_ids=("v00",), oversize_ids=("v02",),
    )
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)

    block = _budget_block_of(result)
    used = block["used"]
    # Only v01 + v03 wrote bytes; v00 (network error) and v02 (declared oversize,
    # refused before the body) wrote nothing and must not be charged.
    assert sorted(written) == ["v01", "v03"]
    assert used["transferred_bytes"] == 2000
    assert used["delivered_bytes"] == 2000
    assert used["count"] == 2
    assert block["stopped_by"] == "queue_exhausted"


def test_cache_hit_charges_delivered_not_transferred(tmp_path: Path) -> None:
    """A cache hit occupies a slot and the delivered ledger, but adds no new traffic."""
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9})
    rows = [_row("v0000", "作者A")]
    _seed_material_cache(config, "v0000", 5000)  # real download_video hits this cache
    deps = ReplicationDeps(collector=_collector(rows), prober=lambda path, config: dict(_VALID_PROBE))

    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    block = _budget_block_of(result)
    used = block["used"]
    assert used["count"] == 1
    assert used["delivered_bytes"] == 5000
    assert used["transferred_bytes"] == 0, "a cache hit writes no new bytes over the wire"


def test_transferred_bytes_never_exceeds_the_ceiling(tmp_path: Path) -> None:
    """The wire ceiling is a hard bound even when nothing is delivered."""
    item = 4096
    config = _validation_config(tmp_path, budget={
        "enabled": True, "max_count": 0, "max_bytes": 5 * item, "max_item_bytes": 10 ** 9,
    })
    rows = _many_rows(30)
    deps, downloaded, written = _tracking_deps(tmp_path, rows, item_size=item, validator=_rejecting_validator)
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)

    block = _budget_block_of(result)
    used = block["used"]
    assert sum(written.values()) == used["transferred_bytes"] == 5 * item
    assert used["transferred_bytes"] <= 5 * item  # core assertion
    assert used["delivered_bytes"] == 0
    assert block["stopped_by"] == "transferred_bytes"
    assert len(downloaded) == 5
    # The report surfaces BOTH ledgers, in Chinese.
    readme = (Path(result["output_dir"]) / "00-交付说明.md").read_text(encoding="utf-8")
    assert "真实传输" in readme
    assert "交付" in readme


def test_budget_module_helpers_measure_real_traffic(tmp_path: Path) -> None:
    """``measure_transferred_bytes`` charges growth and ignores a cache hit."""
    from douyin_intelligence.replication_selection import file_size, measure_transferred_bytes

    target = tmp_path / "v.mp4"
    assert file_size(target) == 0
    pre = file_size(target)
    target.write_bytes(b"z" * 1234)
    assert measure_transferred_bytes(target, pre) == 1234  # fresh download
    pre_cached = file_size(target)
    assert measure_transferred_bytes(target, pre_cached) == 0  # cache hit: no growth


# --------------------------------------------------------------------------- #
# 11. Streamed oversize is charged; the run shows when the ceiling starves it.
# --------------------------------------------------------------------------- #
class _StreamingResponse:
    """A response with NO ``Content-Length`` that keeps yielding fixed chunks."""

    def __init__(self, served: list[int], *, chunk: int = 4000, reads: int = 5) -> None:
        self.headers: dict = {}  # no Content-Length -> the streamed guard must catch it
        self._served = served
        self._chunk = chunk
        self._reads = reads

    def read(self, size: int) -> bytes:
        if self._reads <= 0:
            return b""
        self._reads -= 1
        self._served.append(self._chunk)
        return b"y" * self._chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _DeclaredOverCapResponse:
    """A response whose ``Content-Length`` alone is over cap; the body is never read."""

    def __init__(self, served: list[int]) -> None:
        self.headers = {"Content-Length": "99999999"}
        self._served = served

    def read(self, size: int) -> bytes:
        self._served.append(-1)  # must never happen
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_streamed_oversize_reports_source_and_bytes_read(monkeypatch, tmp_path: Path) -> None:
    """The exception must say it was the *stream* (and how many bytes were read)."""
    served: list[int] = []
    monkeypatch.setattr(materials.urllib.request, "urlopen", lambda *a, **k: _StreamingResponse(served))
    config = load_config()
    target = tmp_path / "v.mp4"
    with pytest.raises(MediaTooLargeError) as excinfo:
        materials.download_video("https://signed.example/x", target, config, max_bytes=2000)
    assert excinfo.value.source == "streamed"
    assert excinfo.value.bytes_read == 4000  # one 4000-byte chunk was actually read
    assert not target.exists() and not (tmp_path / "v.mp4.part").exists()


def test_declared_oversize_reports_zero_bytes_read(monkeypatch, tmp_path: Path) -> None:
    """A ``Content-Length`` rejection must say it read nothing (0 body bytes)."""
    served: list[int] = []
    monkeypatch.setattr(materials.urllib.request, "urlopen", lambda *a, **k: _DeclaredOverCapResponse(served))
    config = load_config()
    target = tmp_path / "v.mp4"
    with pytest.raises(MediaTooLargeError) as excinfo:
        materials.download_video("https://signed.example/x", target, config, max_bytes=2000)
    assert excinfo.value.source == "declared"
    assert excinfo.value.bytes_read == 0
    assert served == []  # the body was never read


def test_streamed_oversize_is_charged_to_transferred_bytes(monkeypatch, tmp_path: Path) -> None:
    """End-to-end through the *real* ``download_video``: streamed oversize costs traffic.

    No injected downloader here -- ``urllib`` is stubbed so ``materials`` really
    streams and really aborts mid-body.  Every candidate reads 4000 bytes off the
    wire before its per-item cap (2000) trips; those bytes must be charged, even
    though the candidate is skipped and never delivered.
    """
    served: list[int] = []
    monkeypatch.setattr(materials.urllib.request, "urlopen", lambda *a, **k: _StreamingResponse(served))
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": 2000})
    rows = _many_rows(3)
    deps = ReplicationDeps(collector=_collector(rows), prober=lambda path, config: dict(_VALID_PROBE))

    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    block = _budget_block_of(result)
    used = block["used"]

    assert sum(served) == 3 * 4000  # 3 candidates each read one 4000-byte chunk
    assert used["transferred_bytes"] == sum(served) == 12000
    assert used["transferred_bytes"] > 0
    assert used["delivered_bytes"] == 0 and used["count"] == 0
    # Skip attribution + stop reason are unchanged: a per-item oversize is a skip.
    assert block["stopped_by"] == "queue_exhausted"
    assert [item["stage"] for item in block["skipped"]] == ["budget_item"] * 3


def test_declared_oversize_is_not_charged(monkeypatch, tmp_path: Path) -> None:
    """A declared (pre-body) oversize must stay zero-charge -- the fix must not overcount."""
    served: list[int] = []
    monkeypatch.setattr(materials.urllib.request, "urlopen", lambda *a, **k: _DeclaredOverCapResponse(served))
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": 2000})
    rows = _many_rows(3)
    deps = ReplicationDeps(collector=_collector(rows), prober=lambda path, config: dict(_VALID_PROBE))

    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    block = _budget_block_of(result)
    assert served == []  # never read a body byte
    assert block["used"]["transferred_bytes"] == 0
    assert block["used"]["delivered_bytes"] == 0
    assert [item["stage"] for item in block["skipped"]] == ["budget_item"] * 3


def test_byte_ceiling_starvation_is_surfaced_when_nothing_is_delivered(tmp_path: Path) -> None:
    """The 'silent starvation' case: wire ceiling spent, 0 delivered, candidates left."""
    item = 1000
    config = _validation_config(tmp_path, budget={
        "enabled": True, "max_count": 12, "max_bytes": 5 * item, "max_item_bytes": 10 ** 9,
    })
    rows = _many_rows(20)
    deps, downloaded, written = _tracking_deps(tmp_path, rows, item_size=item, validator=_rejecting_validator)
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)

    block = _budget_block_of(result)
    assert block["stopped_by"] == "transferred_bytes"
    assert block["used"]["count"] == 0 < block["limits"]["max_count"]

    manifest = json.loads((Path(result["output_dir"]) / "清单.json").read_text(encoding="utf-8"))
    note = next((w for w in manifest["warnings"] if "提前停止" in w), None)
    assert note is not None, manifest["warnings"]
    assert "真实传输字节" in note  # names the ledger that bound
    assert "实际交付 0 条 / 目标 12 条" in note
    assert "真实传输" in note and "交付" in note
    assert "download_budget.max_bytes" in note  # actionable, uses the real key name
    # ... and the same warning must be visible in the human-facing readme.
    readme = (Path(result["output_dir"]) / "00-交付说明.md").read_text(encoding="utf-8")
    assert "提前停止" in readme


def test_byte_ceiling_starvation_names_the_delivered_ledger(tmp_path: Path) -> None:
    """The 'bytes' variant must name the *delivered* ledger, not the wire one."""
    item = 1000
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": 2 * item, "max_item_bytes": 10 ** 9})
    rows = _many_rows(10)  # no validation -> deliveries accumulate
    deps, downloaded, written = _tracking_deps(tmp_path, rows, item_size=item)
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)

    block = _budget_block_of(result)
    assert block["stopped_by"] == "bytes"
    manifest = json.loads((Path(result["output_dir"]) / "清单.json").read_text(encoding="utf-8"))
    note = next((w for w in manifest["warnings"] if "提前停止" in w), None)
    assert note is not None and "交付字节" in note
    assert "实际交付 2 条 / 目标 12 条" in note


def test_no_starvation_warning_when_the_count_target_is_met(tmp_path: Path) -> None:
    """A normal run that hits ``max_count`` must NOT emit the starvation warning."""
    config = _config(tmp_path, budget={"enabled": True, "max_count": 3, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9})
    rows = _many_rows(10)
    deps, downloaded, written = _tracking_deps(tmp_path, rows, item_size=1000)
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)

    block = _budget_block_of(result)
    assert block["stopped_by"] == "count"
    assert block["used"]["count"] == 3
    manifest = json.loads((Path(result["output_dir"]) / "清单.json").read_text(encoding="utf-8"))
    assert all("提前停止" not in warning for warning in manifest["warnings"])
    readme = (Path(result["output_dir"]) / "00-交付说明.md").read_text(encoding="utf-8")
    assert "提前停止" not in readme


# --------------------------------------------------------------------------- #
# 12. Delivered ledger is idempotent per video_id (defect P1).
#
# The same video legitimately enters the budget twice -- a script-stage source
# re-selected as a material source -- but it is ONE delivered file.  ``count``
# and ``bytes`` must count it once; ``transferred_bytes`` still charges every
# physical fetch (real traffic is real traffic).
# --------------------------------------------------------------------------- #
def _stub_candidate(video_id: str, *, title: str = "t", author: str = "a", heat: float = 1.0):
    from types import SimpleNamespace

    return SimpleNamespace(video_id=video_id, title=title, author=author, heat_score=heat)


def test_select_counts_each_video_once_across_stages() -> None:
    budget = DownloadBudget(max_count=10, max_bytes=10 ** 9, max_item_bytes=10 ** 9)
    first = _stub_candidate("v1")
    second = _stub_candidate("v2", title="t2", author="b", heat=0.5)

    budget.mark_transferred(1000)
    budget.select(first, 1000, relevance=0.3, stage="script")
    # Same video, material stage, genuinely re-fetched (traffic IS charged).
    budget.mark_transferred(1000)
    budget.select(first, 1000, relevance=0.3, stage="material")
    budget.mark_transferred(2000)
    budget.select(second, 2000, relevance=0.1, stage="material")

    # Delivered ledger: one slot + one size per *distinct* video.
    assert budget.count == 2
    assert budget.bytes == 3000
    # Wire ledger: every physical fetch is charged.
    assert budget.transferred_bytes == 4000
    assert [entry["video_id"] for entry in budget.selected] == ["v1", "v2"]
    assert budget.selected[0]["stages"] == ["script", "material"]
    assert budget.selected[1]["stages"] == ["material"]
    assert budget.snapshot()["used"] == {
        "count": 2, "bytes": 3000, "delivered_bytes": 3000, "transferred_bytes": 4000,
    }


def test_select_is_idempotent_within_a_single_stage_too() -> None:
    budget = DownloadBudget(max_count=10, max_bytes=10 ** 9, max_item_bytes=10 ** 9)
    candidate = _stub_candidate("v1")
    budget.select(candidate, 2048, relevance=0.25, stage="material")
    budget.select(candidate, 2048, relevance=0.25, stage="material")
    assert budget.count == 1 and budget.bytes == 2048
    assert budget.selected[0]["stages"] == ["material"]


def test_select_untagged_entry_shape_is_unchanged() -> None:
    """Download-only path: no stage tag -> identical entry shape as before."""
    budget = DownloadBudget(max_count=5, max_bytes=10 ** 9, max_item_bytes=10 ** 9)
    budget.select(_stub_candidate("v1"), 2048, relevance=0.25)
    assert budget.selected == [{
        "video_id": "v1", "title": "t", "author": "a",
        "heat_score": 1.0, "relevance_score": 0.25, "size_bytes": 2048,
    }]
    assert budget.count == 1 and budget.bytes == 2048


# --------------------------------------------------------------------------- #
# 13. Starvation attribution (defect P8): the "wasted traffic" gloss belongs to
#     the wire branch only; the delivered-bytes branch gets a neutral wording.
# --------------------------------------------------------------------------- #
def test_starvation_warning_bytes_branch_has_no_wasted_traffic_claim(tmp_path: Path) -> None:
    item = 1000
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": 2 * item, "max_item_bytes": 10 ** 9})
    rows = _many_rows(10)  # no validation -> every fetched file is delivered
    deps, downloaded, written = _tracking_deps(tmp_path, rows, item_size=item)
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)

    block = _budget_block_of(result)
    assert block["stopped_by"] == "bytes"
    assert block["used"]["transferred_bytes"] == block["used"]["delivered_bytes"]  # nothing wasted
    manifest = json.loads((Path(result["output_dir"]) / "清单.json").read_text(encoding="utf-8"))
    note = next((w for w in manifest["warnings"] if "提前停止" in w), None)
    assert note is not None, manifest["warnings"]
    assert "白耗流量" not in note  # accurate: no traffic was wasted on this branch
    assert "交付字节账已写满上限" in note


def test_starvation_warning_transferred_branch_names_wasted_traffic(tmp_path: Path) -> None:
    item = 1000
    config = _validation_config(tmp_path, budget={
        "enabled": True, "max_count": 12, "max_bytes": 5 * item, "max_item_bytes": 10 ** 9,
    })
    rows = _many_rows(20)
    deps, downloaded, written = _tracking_deps(tmp_path, rows, item_size=item, validator=_rejecting_validator)
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)

    block = _budget_block_of(result)
    assert block["stopped_by"] == "transferred_bytes"
    manifest = json.loads((Path(result["output_dir"]) / "清单.json").read_text(encoding="utf-8"))
    note = next((w for w in manifest["warnings"] if "提前停止" in w), None)
    assert note is not None and "白耗流量" in note


# --------------------------------------------------------------------------- #
# 14. Defect P1a -- one shared video cache root.
#
# The script chain, the material chain and the download-only loop must all cache
# a downloaded video under ``<media_root>/<REPLICATION_VIDEO_SUBDIR>/<id>.mp4``.
# They used to diverge (``script/`` vs ``material/``), so a video serving both
# stages was fetched twice -- real, measured waste: 33,998,762 B (~26% of a run's
# whole traffic) across two ids.  These tests pin the *convergence*, not just the
# constant.
# --------------------------------------------------------------------------- #
def _full_chain_deps_recording(tmp_path: Path, rows: list[dict], destinations: list[str]):
    """A full-chain deps whose downloader records every destination it is given."""
    deps, _ = _deps(tmp_path, rows, full=True)

    def downloader(url, destination, config, *, max_bytes=None):
        destinations.append(str(destination))
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"x" * 2048)

    deps.downloader = downloader
    return deps


def test_p1a_script_and_material_share_one_video_cache_root(tmp_path: Path, monkeypatch) -> None:
    from douyin_intelligence.replication_selection import REPLICATION_VIDEO_SUBDIR

    assert REPLICATION_VIDEO_SUBDIR == "material"  # the persistent store key
    monkeypatch.setattr("douyin_intelligence.replication_pipeline.media_tool_available", lambda config, name: True)
    monkeypatch.setattr("douyin_intelligence.replication_pipeline.export_video_clips",
                        lambda *args, **kwargs: {"degraded": False, "clips": []})
    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics", _visual_ok)
    config = _config(tmp_path, budget={"enabled": True, "max_count": 10, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9})
    rows = _many_rows(5)

    destinations: list[str] = []
    deps = _full_chain_deps_recording(tmp_path, rows, destinations)
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", deps=deps)

    assert destinations, "no download happened"
    # Every destination lives under the single shared sub-root -- never ``script``.
    assert all(REPLICATION_VIDEO_SUBDIR in Path(d).parts for d in destinations), destinations
    assert all("script" not in Path(d).parts for d in destinations), destinations

    # The video common to both chains (the script replica) resolves to exactly ONE
    # cache path, even though both stages fetched it.  The stub downloader skips
    # the real cache short-circuit, so it is *called* twice -- with the same path
    # after P1a, with two different paths before.
    manifest = json.loads((Path(result["output_dir"]) / "清单.json").read_text(encoding="utf-8"))
    script_id = manifest["script_replica"]["video_id"]
    assert script_id
    common = [d for d in destinations if Path(d).stem == script_id]
    assert len(common) >= 2, f"{script_id} 未进入两个阶段：{common}"
    assert len(set(common)) == 1, f"同一视频被缓存到多个路径（P1a 回归）：{common}"


def test_p1a_download_only_uses_the_shared_video_cache_root(tmp_path: Path) -> None:
    from douyin_intelligence.replication_selection import REPLICATION_VIDEO_SUBDIR

    config = _config(tmp_path, budget=None)  # budget off -> the simplest path
    rows = [_row("v0000", "作者A")]
    destinations: list[Path] = []

    def downloader(url, destination, config, *, max_bytes=None):
        destinations.append(Path(destination))
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"x" * 2048)

    deps = ReplicationDeps(collector=_collector(rows), downloader=downloader,
                           prober=lambda path, config: {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"})
    run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)

    # The download-only loop must point at the *same* shared sub-root so its
    # fetches are reused by (and reuse) the full-chain caches.
    assert [d.parent.name for d in destinations] == [REPLICATION_VIDEO_SUBDIR]


# --------------------------------------------------------------------------- #
# 15. Defect P1c -- ``select`` must sit *after* the measured-duration gate.
#
# ``budget.select`` used to run before the duration window was checked, so a file
# rejected for its length still held a slot and a byte of the run budget.  The
# real 9.13 run shows the cost: a candidate rejected with "时长 27s 不在 30~300s"
# still appeared in ``download_budget.selected``.  ``validate_candidate`` stays
# before ``select`` (corrupt files must never hold a slot); only ``select`` moves.
# --------------------------------------------------------------------------- #
def test_p1c_duration_rejected_candidate_never_holds_a_budget_slot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("douyin_intelligence.replication_pipeline.media_tool_available", lambda config, name: True)
    monkeypatch.setattr("douyin_intelligence.replication_pipeline.export_video_clips",
                        lambda *args, **kwargs: {"degraded": False, "clips": []})
    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics", _visual_ok)
    config = _config(tmp_path, budget={"enabled": True, "max_count": 10, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9})
    rows = [
        _row("v00", "作者A", duration=60),
        _row("v01", "作者B", duration=10),  # measured 10s: below script 30 AND material 15
        _row("v02", "作者C", duration=60),
    ]

    def prober(path, config):
        return {
            "duration_seconds": 10.0 if Path(path).stem == "v01" else 60.0,
            "width": 1080, "height": 1920, "codec": "h264",
        }

    deps, _ = _deps(tmp_path, rows, full=True)
    deps.prober = prober
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", deps=deps)

    block = _budget_block_of(result)
    selected_ids = [item["video_id"] for item in block["selected"]]
    # v01 clears ``validate_candidate`` (valid probe) but fails the duration window;
    # it must never hold a slot/byte -> absent from ``selected`` (P1c).
    assert "v01" not in selected_ids, selected_ids
    # The healthy neighbours are still selected: one length-mismatch drops one item,
    # never the run.
    assert "v00" in selected_ids and "v02" in selected_ids, selected_ids
    # The rejection is attributed to a *duration* gate, never to the budget.  v01's
    # metadata already carries its 10 s, so since the material window moved ahead of
    # the download the attribution is now the pre-download gate ("duration_pre")
    # instead of the post-download one ("duration"); accept either -- the "duration"
    # phase itself is still exercised by the tolerance-boundary test below.
    manifest = json.loads((Path(result["output_dir"]) / "清单.json").read_text(encoding="utf-8"))
    rejected = manifest["material_replica"]["rejected"]
    assert any(
        e.get("video_id") == "v01" and e.get("stage") in {"duration", "duration_pre"}
        for e in rejected
    ), rejected


# --------------------------------------------------------------------------- #
# 16. Defect P1d -- a single oversize is a *skip* while budget remains.
#
# Treating "this item is too large" as "the run is exhausted" ``break``-ed the
# scan: the direct cause of the "only 2 materials" bug (rank 23/47 was an oversize
# and cut off 24 unevaluated candidates).  An oversize is cheap -- a declared /
# Content-Length rejection reads no body (0 wire bytes) -- and ``allow()`` still
# bounds count/bytes, so scanning on is safe.  Only a *fully spent* byte budget
# (``remaining_bytes() == 0``) makes an oversize terminal.
# --------------------------------------------------------------------------- #
def test_p1d_lone_oversize_is_skip_while_budget_remains() -> None:
    budget = DownloadBudget(max_count=10, max_bytes=1000, max_item_bytes=100_000)
    budget.mark_transferred(0)
    assert budget.remaining_bytes() == 1000
    assert budget.note_oversize(budget.item_cap()) == "skip"  # oversize, but budget left
    assert budget.stopped_by is None
    budget.mark_transferred(1000)  # spend the ceiling completely
    assert budget.remaining_bytes() == 0
    assert budget.note_oversize(budget.item_cap()) == "stop"  # nothing can fit now
    assert budget.stopped_by == "bytes"


def test_p1d_lone_oversize_is_skip_when_no_byte_ceiling() -> None:
    budget = DownloadBudget(max_count=10, max_bytes=0, max_item_bytes=100_000)  # 0 == unlimited
    assert budget.remaining_bytes() is None
    assert budget.note_oversize(budget.item_cap()) == "skip"
    assert budget.stopped_by is None


def test_p1d_mid_queue_oversize_does_not_starve_later_items(tmp_path: Path) -> None:
    config = _config(tmp_path, budget={"enabled": True, "max_count": 100, "max_bytes": 100_000, "max_item_bytes": 2000})
    sizes = {"v00": 1000, "v01": 5000, "v02": 1000, "v03": 5000, "v04": 1000}
    rows = [_row(video_id, f"作者{index}") for index, video_id in enumerate(sizes)]

    def downloader(url, destination, config, *, max_bytes=None):
        size = sizes[Path(destination).stem]
        if max_bytes is not None and size > max_bytes:
            raise MediaTooLargeError("视频声明体积超限", declared_bytes=size, limit=max_bytes)  # 0 body bytes
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"x" * size)

    deps = ReplicationDeps(collector=_collector(rows), downloader=downloader,
                           prober=lambda path, config: {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"})
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)

    block = _budget_block_of(result)
    # v01 and v03 are oversize *per-item*; they are skipped and -- crucially -- the
    # scan keeps going, so v02 and v04 are still reached (pre-fix the first
    # oversize ``break``-ed and the rest were never evaluated).
    assert [item["video_id"] for item in block["selected"]] == ["v00", "v02", "v04"]
    assert block["stopped_by"] == "queue_exhausted"
    assert [item["stage"] for item in block["skipped"]].count("budget_item") == 2


# --------------------------------------------------------------------------- #
# 16b. Defect P1d -- behavioural guard for the branch the two tests above miss.
#
# ``test_p1d_lone_oversize_is_skip_when_no_byte_ceiling`` and
# ``test_p1d_mid_queue_oversize_does_not_starve_later_items`` both PASS on the
# old implementation: with no byte ceiling, or with an item oversize *per item*,
# the old ``note_oversize`` already returned ``"skip"``.  The actual defect was
# narrower -- it lived in the ``cap == remaining_budget`` branch.  ``item_cap()``
# is ``min(max_item_bytes, remaining_bytes())``, so whenever the *remaining run
# budget* (not the per-item cap) was the binding constraint the old code fell
# through to ``"stop"`` and the caller ``break``-ed, starving every smaller
# candidate behind the oversize (the "only 2 materials" regression: rank 23/47
# was such an oversize and cut off all remaining candidates).
#
# This test pins exactly that branch: ``max_item_bytes`` (1000) is deliberately
# LARGER than ``max_bytes`` (100), so the per-item cap can never bind and the
# mid-queue item can only be refused by the leftover run budget.
# --------------------------------------------------------------------------- #
def test_p1d_remaining_budget_oversize_does_not_starve_later_smaller_item(tmp_path: Path, monkeypatch) -> None:
    """An oversize caused by the *remaining budget* must skip, never ``break`` (P1d).

    Drives the *real* material candidate loop (``select_material_replicas``), so
    the genuine ``allow`` -> ``item_cap`` -> download -> ``note_oversize`` ->
    ``select`` sequence is exercised rather than an isolated ``note_oversize``
    call.  The budget is injected directly because ``max_count=0`` (unlimited)
    is not expressible through config validation.
    """
    from douyin_intelligence.replication_candidates import Candidate
    from douyin_intelligence.replication_selection import select_material_replicas

    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics", _visual_ok)

    # ``max_item_bytes`` (1000) >> ``max_bytes`` (100): a single item can fit the
    # per-item cap yet still overflow the run ceiling, so any oversize here is
    # necessarily a *remaining-budget* oversize -- the old code's blind spot.
    budget = DownloadBudget(max_count=0, max_bytes=100, max_item_bytes=1000)
    sizes = {"v00": 60, "v01": 50, "v02": 30}
    # Three real videos with identical heat/relevance and a duration inside the
    # material window, so *only* the byte budget can refuse any of them.  The
    # deterministic order is relevance -> heat -> video_id == v00, v01, v02.
    candidates = [
        Candidate(
            video_id=vid, title=f"标题-{vid}", author=f"作者{vid}",
            duration_seconds=60.0, heat_score=100.0,
        )
        for vid in sizes
    ]

    def downloader(url, destination, config, *, max_bytes=None):
        size = sizes[Path(destination).stem]
        if max_bytes is not None and size > max_bytes:
            # Declared / Content-Length oversize: refused before any body read.
            raise MediaTooLargeError("视频声明体积超限", declared_bytes=size, limit=max_bytes)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"x" * size)

    deps, _ = _deps(tmp_path, [], full=True)
    deps.downloader = downloader
    deps.prober = lambda path, config: {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}

    config = _config(tmp_path, budget=None)  # budget injected directly, not via config
    select_material_replicas(config, candidates, deps=deps, budget=budget, relevance={}, validation_store=[])

    # A (60 B) is delivered -> remaining 60 -> 40.  B (50 B) then exceeds the
    # *remaining* 40 (= ``item_cap()`` after A, NOT ``max_item_bytes``) -> it must
    # be a plain skip and the scan must keep going; C (30 B <= 40) is delivered.
    # Pre-fix the B oversize ``break``-ed here, so C was never evaluated.
    assert [entry["video_id"] for entry in budget.selected] == ["v00", "v02"]
    assert "v01" in [entry["video_id"] for entry in budget.skipped]
    assert any(
        entry["video_id"] == "v01" and entry["stage"] == "budget_item"
        for entry in budget.skipped
    )
    # The scan ran to the end: it was never terminated by the byte ceiling.
    assert budget.stopped_by is None
    assert budget.bytes == 90  # 60 + 30 delivered; the skipped B contributed nothing


# --------------------------------------------------------------------------- #
# 17. Material-chain duration window moved *before* the download.
#
# ``measured_duration_window_reject`` (replication_selection.py) is the
# post-download half of the *prefilter* window and is an explicit no-op once the
# metadata carried a duration; the *material* window (15~180 s) used to be judged
# only *after* the download.  A candidate the prefilter let through (10~300 s) was
# therefore downloaded in full and only then refused by the material window -- the
# 9.13 cold rerun burned one such 202 s file's entire size (23,446,900 B).
#
# The new ``duration_pre`` gate judges the material window from the metadata,
# before any download, with a +/-``validation.duration_tolerance`` (default 5 %)
# margin so a boundary candidate that could still pass on its *measured* length
# (e.g. 182 s vs a 180 s ceiling) is not killed up front.  It fires only when the
# metadata genuinely carries a duration (``duration_seconds > 0`` and a non-empty
# ``duration_source``); otherwise the candidate keeps the old download-then-judge
# path unchanged.
# --------------------------------------------------------------------------- #
def _candidate_with_duration(video_id: str, seconds: float, source: str):
    from douyin_intelligence.replication_candidates import Candidate

    return Candidate(
        video_id=video_id, title=f"标题-{video_id}", author=f"作者-{video_id}",
        duration_seconds=seconds, duration_source=source, heat_score=100.0,
    )


def _deps_recording_calls(tmp_path: Path, durations: dict[str, float], calls: list[str]):
    """Deps whose downloader records every call and whose prober returns the *measured* length."""
    def downloader(url, destination, config, *, max_bytes=None):
        calls.append(Path(destination).stem)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"x" * 60)

    def prober(path, config):
        return {"duration_seconds": durations[Path(path).stem], "width": 1080, "height": 1920, "codec": "h264"}

    deps, _ = _deps(tmp_path, [], full=True)
    deps.downloader = downloader
    deps.prober = prober
    return deps


def test_material_duration_pre_gate_drops_metadata_oversize_without_downloading(
    tmp_path: Path, monkeypatch
) -> None:
    """(A) A 202 s candidate (> 180 s material ceiling) is refused *before* any download."""
    from douyin_intelligence.replication_selection import select_material_replicas

    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics", _visual_ok)
    calls: list[str] = []
    deps = _deps_recording_calls(tmp_path, {"v202": 202.0}, calls)
    budget = DownloadBudget(max_count=0, max_bytes=1_000_000, max_item_bytes=1_000_000)
    config = _config(tmp_path, budget=None)

    result = select_material_replicas(
        config,
        [_candidate_with_duration("v202", 202.0, "duration_ms")],
        deps=deps, budget=budget, relevance={}, validation_store=[],
    )

    duration_pre = [entry for entry in result["unmet"] if entry.get("stage") == "duration_pre"]
    assert calls == []                                      # never downloaded -> zero traffic
    assert [entry["video_id"] for entry in duration_pre] == ["v202"]
    assert duration_pre[0]["duration_source"] == "duration_ms"
    assert "下载前判定" in duration_pre[0]["reason"]
    assert budget.selected == []                            # holds no slot ...
    assert budget.count == 0 and budget.bytes == 0          # ... and not a single byte
    assert budget.transferred_bytes == 0                    # ... nor any real traffic


def test_material_duration_pre_gate_falls_back_when_metadata_lacks_duration(
    tmp_path: Path, monkeypatch
) -> None:
    """(B) Metadata without a duration is untouched: still downloaded, judged after (as before)."""
    from douyin_intelligence.replication_selection import select_material_replicas

    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics", _visual_ok)
    calls: list[str] = []
    # The prober reports the *measured* length (60 s, inside the material window);
    # the candidate's metadata itself carries no duration.
    deps = _deps_recording_calls(tmp_path, {"v000": 60.0}, calls)
    budget = DownloadBudget(max_count=0, max_bytes=1_000_000, max_item_bytes=1_000_000)
    config = _config(tmp_path, budget=None)

    result = select_material_replicas(
        config,
        [_candidate_with_duration("v000", 0.0, "")],
        deps=deps, budget=budget, relevance={}, validation_store=[],
    )

    assert calls == ["v000"]                                # behaviour unchanged: still downloaded
    assert "duration_pre" not in [entry["stage"] for entry in result["unmet"]]
    assert [entry["video_id"] for entry in budget.selected] == ["v000"]


def test_material_duration_pre_gate_keeps_tolerance_boundary_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    """(C) 182 s is inside 180 * (1 + 5 %) = 189 s, so it is *not* pre-rejected."""
    from douyin_intelligence.replication_selection import select_material_replicas

    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics", _visual_ok)
    calls: list[str] = []
    deps = _deps_recording_calls(tmp_path, {"v182": 182.0}, calls)
    budget = DownloadBudget(max_count=0, max_bytes=1_000_000, max_item_bytes=1_000_000)
    config = _config(tmp_path, budget=None)

    result = select_material_replicas(
        config,
        [_candidate_with_duration("v182", 182.0, "duration_ms")],
        deps=deps, budget=budget, relevance={}, validation_store=[],
    )

    assert calls == ["v182"]                                # tolerated -> still downloaded
    assert "duration_pre" not in [entry["stage"] for entry in result["unmet"]]
    # The strict material window still refuses it later, on the *measured* length
    # (> 180 s) -- that is the pre-existing post-download behaviour, untouched.
    assert [entry["stage"] for entry in result["unmet"] if entry.get("video_id") == "v182"] == ["duration"]
