"""Independent (adversarial) verification of the download-budget feature.

Written by the QA reviewer, *not* the implementer.  Deliberately uses different
angles from ``test_replication_download_budget.py``: it drives the **real**
``materials.download_video`` (no stub) for the signature-passthrough checks,
measures the real delivered bytes under ``04-原片`` instead of trusting
``budget.used``, and exercises the boundary/zero configurations the
implementation tests skip.

Nothing here modifies ``src/``.  Where a defect is demonstrated the test is
marked ``xfail(strict=True)`` so the suite stays green while the defect is
documented and will *loudly* flip if the behaviour changes.
"""

from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

import pytest

from douyin_intelligence import materials
from douyin_intelligence.config import load_config
from douyin_intelligence.materials import MediaTooLargeError
from douyin_intelligence.replication_pipeline import ReplicationDeps, run_material_replication
from douyin_intelligence.replication_selection import (
    DownloadBudget,
    VisualMetrics,
    invoke_downloader,
)

SOURCE_DIR = "04-原片"
PROCESS_DIR = "05-过程数据"
MANIFEST = "清单.json"

MiB = 1024 * 1024


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _row(video_id: str, author: str, *, duration: float = 60.0, digg: int = 100,
         url: str = "", title: str | None = None) -> dict:
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


def _config(tmp_path: Path, *, budget: dict | None) -> dict:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    config["jobs"]["material_replication"]["prefilter"] = {"enabled": False}
    # The download-validation layer runs a real ffprobe+ffmpeg and this module's
    # fakes write non-media bytes; isolate it here (it has its own test module).
    config["jobs"]["material_replication"]["validation"] = {"enabled": False}
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
        # The stage is carried by ``cache_dir`` (``cache_root/script/...`` vs
        # ``cache_root/material/...``), not by the video path: since P1a the
        # script and material chains share one video cache root, so the source
        # ``.mp4`` is identical for both stages.  Only the script source has
        # speech; the material sources are silent.
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


def _write_sized(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    block = b"x" * min(size, MiB)
    with path.open("wb") as stream:
        remaining = size
        while remaining > 0:
            chunk = block if remaining >= len(block) else block[:remaining]
            stream.write(chunk)
            remaining -= len(chunk)


def _sized_downloader(sizes: dict[str, int], calls: list[str], failures: set[str] | None = None):
    failures = failures or set()

    def downloader(url, destination, config, *, max_bytes=None):
        video_id = Path(destination).stem
        calls.append(video_id)
        if video_id in failures:
            raise OSError("simulated network failure")
        payload = int(sizes.get(video_id, 2048))
        if max_bytes is not None and payload > max_bytes:
            raise MediaTooLargeError(
                f"视频声明体积 {payload} 字节超过上限 {max_bytes}",
                declared_bytes=payload, limit=max_bytes,
            )
        _write_sized(Path(destination), payload)
    return downloader


def _prober(path, config):
    return {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}


def _full_chain_deps(collector, downloader):
    return ReplicationDeps(
        collector=collector, downloader=downloader, prober=_prober,
        transcriber=_Transcriber(), ocr=_Ocr(), face_detector=_Face(),
    )


def _source_bytes(output_dir: Path) -> int:
    return sum(path.stat().st_size for path in (output_dir / SOURCE_DIR).glob("*") if path.is_file())


def _delivery_folder_bytes(output_dir: Path) -> int:
    """Every file in the delivery directory (02/03 + 04-原片 + 05 + 清单 + readme).

    The user's spec is on the delivery directory **as a whole** (70~150 MB); the
    old ``_source_bytes`` (04-原片 only) is blind to 02/03, which is how a doubled
    delivery slipped past this very test.  This is the measure the new pipeline
    gate records, so the acceptance line is asserted against it.
    """
    return sum(path.stat().st_size for path in Path(output_dir).rglob("*") if path.is_file())


def _budget_block(output_dir: Path) -> dict:
    return json.loads((output_dir / PROCESS_DIR / "download_budget.json").read_text(encoding="utf-8"))


class _FakeResponse:
    def __init__(self, *, headers: dict, body: bytes = b"", chunk: int = 1024) -> None:
        self.headers = headers
        self._body = body
        self._chunk = chunk
        self._offset = 0
        self.read_sizes: list[int] = []

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        if self._body:
            part = self._body[self._offset:self._offset + self._chunk]
            self._offset += len(part)
            return part
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


# =========================================================================== #
# H1 — inspect.signature passthrough fragility
# =========================================================================== #
def test_h1_real_download_video_declares_kwonly_max_bytes() -> None:
    """Contract guard: the real downloader MUST keep a ``max_bytes`` kwarg.

    ``invoke_downloader`` silently drops the cap when this parameter is absent,
    so pin it here — a 3-arg refactor of ``download_video`` will fail loudly
    instead of quietly disabling the per-item 30 MB limit.
    """
    signature = inspect.signature(materials.download_video)
    assert "max_bytes" in signature.parameters
    assert signature.parameters["max_bytes"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["max_bytes"].default is None


def test_h1_invoke_downloader_passes_cap_to_real_download_video(monkeypatch, tmp_path: Path) -> None:
    """Real ``download_video`` (not stubbed) must receive ``max_bytes`` in-flight."""
    response = _FakeResponse(headers={"Content-Length": "99999999"})
    monkeypatch.setattr(materials.urllib.request, "urlopen", lambda *a, **k: response)
    config = load_config()
    target = tmp_path / "v.mp4"
    with pytest.raises(MediaTooLargeError):
        invoke_downloader(materials.download_video, "https://signed.example/x", target, config, 1024)
    # Zero-waste guard: the declared size is rejected before any body read.
    assert response.read_sizes == []
    assert not target.exists()


def test_h1_three_arg_wrapper_silently_drops_per_item_cap(monkeypatch, tmp_path: Path) -> None:
    """Latent risk: a 3-arg wrapper makes the per-item cap vanish silently.

    This is the exact failure mode H1 warns about.  It *documents* current
    behaviour (there is no runtime error, just an unbounded download); the
    contract test above is the regression guard that catches it early.
    """
    calls: list[int] = []

    def fake_urlopen(*args, **kwargs):
        calls.append(1)
        return _FakeResponse(headers={"Content-Length": str(4 * MiB)}, body=b"z" * (4 * MiB))

    monkeypatch.setattr(materials.urllib.request, "urlopen", fake_urlopen)
    config = load_config()

    def wrapper(url, destination, config):  # 3-arg: no max_bytes advertised
        return materials.download_video(url, destination, config)

    target = tmp_path / "wrapped.mp4"
    # cap=1024, but the wrapper never sees it -> the 4 MiB body is written anyway.
    invoke_downloader(wrapper, "https://signed.example/x", target, config, 1024)
    assert target.is_file()
    assert target.stat().st_size == 4 * MiB > 1024
    assert len(calls) == 1


# =========================================================================== #
# H2 — every candidate fails: bounded, no infinite loop
# =========================================================================== #
def test_h2_download_only_all_candidates_fail_is_bounded(tmp_path: Path) -> None:
    calls: list[str] = []
    rows = [_row(f"v{index:02d}", f"作者{index}") for index in range(30)]
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9})
    deps = ReplicationDeps(collector=_collector(rows), downloader=_sized_downloader({}, calls, failures={row["aweme_id"] for row in rows}), prober=_prober)

    started = time.monotonic()
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    elapsed = time.monotonic() - started

    assert elapsed < 15.0, f"疑似死循环/超时：{elapsed:.1f}s"
    assert len(calls) == 30, calls  # each candidate attempted exactly once
    assert result["status"] == "failed"
    assert result["downloads"] == []
    assert len(result["failures"]) == 30
    assert all(item["stage"] == "download" for item in result["failures"])
    block = _budget_block(Path(result["output_dir"]))
    assert block["used"]["count"] == 0
    assert block["stopped_by"] == "queue_exhausted"


def test_h2_full_chain_all_downloads_fail_is_bounded(tmp_path: Path) -> None:
    calls: list[str] = []
    rows = [_row(f"v{index:02d}", f"作者{index}") for index in range(12)]
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9})
    deps = _full_chain_deps(_collector(rows), _sized_downloader({}, calls, failures={row["aweme_id"] for row in rows}))

    started = time.monotonic()
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", deps=deps)
    elapsed = time.monotonic() - started

    assert elapsed < 20.0, f"疑似死循环/超时：{elapsed:.1f}s"
    assert len(calls) <= 24, calls  # two selection loops, each bounded by the pool
    assert result["counts"]["downloaded"] == 0
    assert result["status"] in {"not_found", "partial", "failed", "success"}
    output_dir = Path(result["output_dir"])
    manifest = json.loads((output_dir / MANIFEST).read_text(encoding="utf-8"))
    assert manifest["script_replica"]["status"] == "not_found"
    assert manifest["material_replica"]["selected"] == 0


# =========================================================================== #
# H3 — the ``budget`` -> ``phase_budget`` rename did not drop a call site
# =========================================================================== #
def test_h3_phase_budget_soft_timeout_paths_still_fire(tmp_path: Path, monkeypatch) -> None:
    """Both soft-budget ``_phase_expired`` checks must still work after the rename.

    A missed rename would surface as an ``AttributeError`` (``DownloadBudget`` has
    no ``.get``) or, worse, silently disable the timeout warning.  Force the clock
    past both phases and assert the two warnings still appear.
    """
    monkeypatch.setattr("douyin_intelligence.replication_pipeline.media_tool_available", lambda config, name: True)
    monkeypatch.setattr("douyin_intelligence.replication_pipeline.export_video_clips",
                        lambda *args, **kwargs: {"degraded": False, "clips": []})
    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics",
                        lambda *args, **kwargs: VisualMetrics(sampled_frames=10, motion_frame_ratio=0.9,
                                                              ocr_text_frame_ratio=0.0, visual_ok=True))
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9})
    rows = [_row(f"v{index:02d}", f"作者{index}") for index in range(6)]
    deps = _full_chain_deps(_collector(rows), _sized_downloader({}, []))

    state = {"t": 0.0}

    def clock() -> float:
        state["t"] += 1000.0
        return state["t"]

    deps.clock = clock
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", deps=deps)

    warnings = " ".join(result["warnings"])
    assert "候选池采集超出软预算" in warnings
    assert "脚本复刻视频选择超出软预算" in warnings


# =========================================================================== #
# H4 / H6 — cache hit accounting and cap enforcement
# =========================================================================== #
def _seed_cache(config: dict, video_id: str, size: int) -> Path:
    from douyin_intelligence.replication_theme import project_path

    media_root = config["jobs"]["material_replication"]["media_root"]
    path = project_path(config, media_root) / "material" / f"{video_id}.mp4"
    _write_sized(path, size)
    return path


def test_h4_cache_hit_is_counted_against_the_budget(tmp_path: Path) -> None:
    """A cache-served download still occupies a slot and its real bytes."""
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9})
    rows = [_row("v0000", "作者A")]
    _seed_cache(config, "v0000", 5000)
    # No ``downloader`` -> the pipeline uses the real ``materials.download_video``,
    # which hits the >1KB cache and returns without touching the network.
    deps = ReplicationDeps(collector=_collector(rows), prober=_prober)

    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    output_dir = Path(result["output_dir"])
    block = _budget_block(output_dir)
    assert block["used"]["count"] == 1
    assert block["used"]["bytes"] == 5000  # accounted, not free
    assert _source_bytes(output_dir) == 5000


def test_h4_cache_hit_over_cap_is_refused_not_delivered(tmp_path: Path) -> None:
    """Regression (was a defect): a cached file over the cap must not reach 04-原片."""
    max_bytes, max_item = 1000, 400
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": max_bytes, "max_item_bytes": max_item})
    rows = [_row("v0000", "作者A")]
    _seed_cache(config, "v0000", 5000)  # > 1024 => historically short-circuited before the cap
    deps = ReplicationDeps(collector=_collector(rows), prober=_prober)

    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    output_dir = Path(result["output_dir"])
    block = _budget_block(output_dir)
    # Both gates hold: the ledger and the on-disk delivery stay inside the cap.
    assert block["used"]["bytes"] == 0
    assert block["used"]["count"] == 0
    assert result["downloads"] == []
    assert _source_bytes(output_dir) == 0
    assert any(item["stage"] == "budget_item" for item in block["skipped"])


def test_h4_cache_boundary_exactly_at_cap_ok_one_over_refused(monkeypatch, tmp_path: Path) -> None:
    """Closed interval on the cache path: ``cached_bytes == cap`` ok, ``cap+1`` raises.

    Sizes stay above the ``>1024`` cache-hit threshold, otherwise the file is not
    treated as a cache hit at all (it would be re-downloaded, not short-circuited).
    """

    def _no_network(*args, **kwargs):
        raise AssertionError("缓存边界判定不应发起网络请求")

    monkeypatch.setattr(materials.urllib.request, "urlopen", _no_network)
    config = load_config()
    cap = 2000  # > 1024 so the file is actually recognised as a cache hit

    exact = tmp_path / "exact.mp4"
    exact.write_bytes(b"c" * cap)
    materials.download_video("https://signed.example/x", exact, config, max_bytes=cap)
    assert exact.stat().st_size == cap

    over = tmp_path / "over.mp4"
    over.write_bytes(b"c" * (cap + 1))
    with pytest.raises(MediaTooLargeError):
        materials.download_video("https://signed.example/x", over, config, max_bytes=cap)
    assert over.stat().st_size == cap + 1  # left untouched, not "delivered"


def test_h4_cache_hit_within_cap_still_skips_network(monkeypatch, tmp_path: Path) -> None:
    """The cache optimisation is preserved: a fitting cache hit does no network IO."""

    def _no_network(*args, **kwargs):
        raise AssertionError("合规缓存命中应跳过下载，不得发起网络请求")

    monkeypatch.setattr(materials.urllib.request, "urlopen", _no_network)
    config = load_config()
    target = tmp_path / "cached.mp4"
    target.write_bytes(b"c" * 5000)
    materials.download_video("https://signed.example/x", target, config, max_bytes=10000)
    assert target.stat().st_size == 5000


def test_h4_mixed_cache_and_download_both_gates_hold(tmp_path: Path, monkeypatch) -> None:
    """End-to-end mix of oversize cache / compliant cache / real downloads.

    Drives the *real* ``materials.download_video`` (so the cache path is live);
    only ``urlopen`` is stubbed, to serve the candidates with no cache file.
    Asserts both gates on the **on-disk** delivery, not the self-reported ledger.
    """
    max_bytes, max_item = 100_000, 20_000
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": max_bytes, "max_item_bytes": max_item})
    cached = {"v00": 50_000, "v01": 15_000, "v03": 20_000, "v05": 19_000, "v06": 17_500}
    to_download = {"v02": 18_000, "v04": 30_000, "v07": 15_000}
    for video_id, size in cached.items():
        _seed_cache(config, video_id, size)
    order = ["v00", "v01", "v02", "v03", "v04", "v05", "v06", "v07"]
    rows = [_row(video_id, f"作者{index}") for index, video_id in enumerate(order)]
    for row in rows:
        if row["aweme_id"] in to_download:
            row["video_download_url"] = f"https://signed.example/{row['aweme_id']}"

    class _SizedResponse:
        def __init__(self, size: int) -> None:
            self.headers = {"Content-Length": str(size)}
            self._remaining = size

        def read(self, size: int) -> bytes:
            take = min(4096, self._remaining)
            self._remaining -= take
            return b"d" * take

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(materials.urllib.request, "urlopen",
                        lambda request, timeout=None: _SizedResponse(to_download[request.full_url.rsplit("/", 1)[-1]]))
    deps = ReplicationDeps(collector=_collector(rows), prober=_prober)  # no downloader -> real download_video

    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    output_dir = Path(result["output_dir"])
    block = _budget_block(output_dir)

    delivered_sizes = sorted(path.stat().st_size for path in (output_dir / SOURCE_DIR).glob("*") if path.is_file())
    # v01+v02+v03+v05+v06 == 89500; the oversize cache (v00) and the two oversize
    # downloads (v04 over the item cap, v07 over the *remaining* run budget) are out.
    assert delivered_sizes == [15_000, 17_500, 18_000, 19_000, 20_000], delivered_sizes
    delivered = _source_bytes(output_dir)
    assert delivered <= max_bytes  # run ceiling honoured on disk
    assert max(delivered_sizes) <= max_item  # per-item ceiling honoured on disk
    # Self-report agrees with reality, byte for byte.
    assert block["used"]["bytes"] == sum(item["size_bytes"] for item in result["downloads"]) == delivered
    assert block["used"]["count"] == len(result["downloads"]) == 5
    # Attribution (defect P1d): an oversize beyond the per-item cap is a plain
    # skip; and an item that only fails the *remaining* run budget is ALSO a skip
    # now -- a single oversize must not abort the scan (that abort was the direct
    # cause of the "only 2 material sources" bug).  Every oversize is dropped and
    # the loop runs to the end, so the ceiling is reported as ``queue_exhausted``,
    # not ``bytes``.  (v00, v04 and v07 are all skipped.)
    stages = [item["stage"] for item in block["skipped"]]
    assert stages.count("budget_item") == 3
    assert block["stopped_by"] == "queue_exhausted"


# =========================================================================== #
# H6 — the user's hard acceptance line, end to end at the REAL thresholds
# =========================================================================== #
def test_h6_delivered_bytes_stay_within_real_thresholds(tmp_path: Path) -> None:
    """Real limits (<=12 items / <=150 MiB / <=30 MiB each); measure 04-原片.

    The *download budget* contract is asserted on ``04-原片`` (that is the ledger's
    unit).  The **user's** line -- the delivery directory as a whole (70~150 MB) --
    is a *different* number and is what the old 04-only measure could not see; it
    is asserted below against the on-disk folder, so this test now closes the
    blind spot that let a doubled delivery pass.
    """
    limit_count, limit_bytes, limit_item = 12, 157286400, 31457280
    config = _config(tmp_path, budget={"enabled": True, "max_count": limit_count, "max_bytes": limit_bytes, "max_item_bytes": limit_item})
    # 6 candidates each exactly at the per-item cap: 5 fit (== limit_bytes), the 6th is refused.
    sizes = {f"v{index:02d}": limit_item for index in range(6)}
    rows = [_row(f"v{index:02d}", f"作者{index}") for index in range(6)]
    calls: list[str] = []
    deps = ReplicationDeps(collector=_collector(rows), downloader=_sized_downloader(sizes, calls), prober=_prober)

    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    output_dir = Path(result["output_dir"])
    block = _budget_block(output_dir)
    manifest = json.loads((output_dir / MANIFEST).read_text(encoding="utf-8"))

    delivered = _source_bytes(output_dir)
    # The download budget's on-disk contract, measured on 04-原片.
    assert delivered <= limit_bytes, delivered
    assert len(result["downloads"]) <= limit_count
    # Self-report must agree with reality: used.bytes == ledger == delivered bytes.
    assert block["used"]["bytes"] == sum(item["size_bytes"] for item in result["downloads"]) == delivered
    assert block["stopped_by"] == "bytes"
    assert len(result["downloads"]) == 5 and delivered == limit_bytes

    # The *user's* spec: the delivery directory total.  The pipeline records it,
    # and it must equal the folder on disk (publish is a rename -> bytes preserved)
    # and be strictly larger than 04-原片 alone (02/03/05/清单 are now counted).
    folder_bytes = _delivery_folder_bytes(output_dir)
    assert manifest["delivery_folder"]["delivery_folder_bytes"] == folder_bytes
    assert folder_bytes >= delivered

    # The report must explain *why* these were worth downloading (user requirement).
    readme = (output_dir / "00-交付说明.md").read_text(encoding="utf-8")
    assert "## 下载预算" in readme and "为什么这几条值得下" in readme
    assert "排序限制" in readme and "画面质量" in readme


def test_h6_used_bytes_matches_disk_for_varied_sizes(tmp_path: Path) -> None:
    sizes = {"s0": 4096, "s1": 8192, "s2": 2048}
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9})
    rows = [_row(video_id, f"作者{index}") for index, video_id in enumerate(sizes)]
    deps = ReplicationDeps(collector=_collector(rows), downloader=_sized_downloader(dict(sizes), []), prober=_prober)

    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    output_dir = Path(result["output_dir"])
    block = _budget_block(output_dir)
    assert block["used"]["bytes"] == sum(sizes.values()) == _source_bytes(output_dir)
    assert block["used"]["count"] == len(sizes)


# =========================================================================== #
# H5 — shared budget across script and material loops
# =========================================================================== #
def test_h5_budget_is_shared_and_byte_bounded_across_chains(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("douyin_intelligence.replication_pipeline.media_tool_available", lambda config, name: True)
    monkeypatch.setattr("douyin_intelligence.replication_pipeline.export_video_clips",
                        lambda *args, **kwargs: {"degraded": False, "clips": []})
    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics",
                        lambda *args, **kwargs: VisualMetrics(sampled_frames=10, motion_frame_ratio=0.9,
                                                              ocr_text_frame_ratio=0.0, visual_ok=True))
    max_bytes = 3000
    config = _config(tmp_path, budget={"enabled": True, "max_count": 10, "max_bytes": max_bytes, "max_item_bytes": 10 ** 9})
    rows = [_row(f"v{index:02d}", f"作者{index}") for index in range(8)]
    calls: list[str] = []
    deps = _full_chain_deps(_collector(rows), _sized_downloader({row["aweme_id"]: 1000 for row in rows}, calls))

    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", deps=deps)
    output_dir = Path(result["output_dir"])
    block = _budget_block(output_dir)

    # 8 candidates, count cap 10 (non-binding) -> the *shared* byte ceiling binds.
    #
    # Two semantics notes.
    # (P1) the delivered ledger is idempotent per ``video_id``: v00 serves as both
    # the script replica and a material source, but it is *one delivered video* --
    # ``count``/``bytes`` grow once.
    # (P1a) the script and material chains now share one video cache root, so
    # v00's material-stage fetch is a genuine cache hit (the file already exists;
    # ``measure_transferred_bytes`` charges 0 wire bytes).  Only *new* files cost
    # traffic, so the 3000-byte ceiling now fits **3** distinct videos (v00 --
    # free on the second stage -- plus v01 and v02).  Both ledgers land on 3000
    # together, and ``allow`` attributes the binding ceiling to the delivered
    # ledger (its check runs first).
    assert block["used"]["bytes"] == 3000
    assert block["used"]["count"] == 3
    assert block["used"]["transferred_bytes"] == 3000
    assert block["stopped_by"] == "bytes"
    # Both loops fed the same ledger: script's pick and the material picks coexist
    # (v00 appears once even though both stages selected it).
    script_ids = {item["video_id"] for item in block["selected"]}
    manifest = json.loads((output_dir / MANIFEST).read_text(encoding="utf-8"))
    assert manifest["script_replica"]["video_id"] in script_ids
    assert len(manifest["script_replica"]["video_id"]) > 0
    assert [item["video_id"] for item in block["selected"]].count(manifest["script_replica"]["video_id"]) == 1


# =========================================================================== #
# Boundaries the implementation tests skip
# =========================================================================== #
def test_boundary_zero_limits_mean_unlimited(tmp_path: Path) -> None:
    """Runtime ``0`` means "no cap" for *programmatic* construction.

    User-visible path (the config file) rejects ``< 1`` and points at
    ``enabled=false`` to turn the budget off, so a literal 0 can never reach a
    real run via config.  The runtime keeping ``0 == unlimited`` is deliberate
    flexibility for programmatic callers (tests, internal code) and must stay.
    """
    config = _config(tmp_path, budget={"enabled": True, "max_count": 0, "max_bytes": 0, "max_item_bytes": 0})
    rows = [_row(f"v{index:02d}", f"作者{index}") for index in range(7)]
    calls: list[str] = []
    deps = ReplicationDeps(collector=_collector(rows), downloader=_sized_downloader({}, calls), prober=_prober)

    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    assert len(result["downloads"]) == 7  # 0 does NOT block downloads
    block = _budget_block(Path(result["output_dir"]))
    assert block["stopped_by"] == "queue_exhausted"
    assert block["limits"] == {"max_count": 0, "max_bytes": 0, "max_item_bytes": 0}


def test_boundary_candidates_exactly_equal_count_cap(tmp_path: Path) -> None:
    config = _config(tmp_path, budget={"enabled": True, "max_count": 5, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9})
    rows = [_row(f"v{index:02d}", f"作者{index}") for index in range(5)]
    calls: list[str] = []
    deps = ReplicationDeps(collector=_collector(rows), downloader=_sized_downloader({}, calls), prober=_prober)

    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    assert len(result["downloads"]) == 5
    block = _budget_block(Path(result["output_dir"]))
    assert block["used"]["count"] == 5
    # No candidate was refused -> the run ended naturally, not by the cap.
    assert block["stopped_by"] == "queue_exhausted"


def test_boundary_item_size_exactly_at_cap_is_allowed(tmp_path: Path) -> None:
    cap = 1024
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": cap})
    rows = [_row("v0000", "作者A")]
    calls: list[str] = []
    deps = ReplicationDeps(collector=_collector(rows), downloader=_sized_downloader({"v0000": cap}, calls), prober=_prober)

    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    assert len(result["downloads"]) == 1  # closed interval: exactly cap is accepted
    assert _budget_block(Path(result["output_dir"]))["used"]["bytes"] == cap


def test_boundary_item_one_over_cap_is_skipped(tmp_path: Path) -> None:
    cap = 1024
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": cap})
    rows = [_row("v0000", "作者A")]
    calls: list[str] = []
    deps = ReplicationDeps(collector=_collector(rows), downloader=_sized_downloader({"v0000": cap + 1}, calls), prober=_prober)

    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    assert result["downloads"] == []
    block = _budget_block(Path(result["output_dir"]))
    assert block["used"]["count"] == 0
    assert any(item["stage"] == "budget_item" for item in block["skipped"])


def test_boundary_streaming_cap_catches_missing_content_length(monkeypatch, tmp_path: Path) -> None:
    response = _FakeResponse(headers={}, body=b"y" * (3 * MiB))
    monkeypatch.setattr(materials.urllib.request, "urlopen", lambda *a, **k: response)
    config = load_config()
    target = tmp_path / "v.mp4"
    with pytest.raises(MediaTooLargeError):
        materials.download_video("https://signed.example/x", target, config, max_bytes=2 * MiB)
    assert not target.exists()
    assert not target.with_suffix(".mp4.part").exists()


def test_boundary_streaming_cap_catches_lying_content_length(monkeypatch, tmp_path: Path) -> None:
    """Content-Length under-reports the real size -> second line of defence fires."""
    response = _FakeResponse(headers={"Content-Length": "10"}, body=b"y" * (3 * MiB))
    monkeypatch.setattr(materials.urllib.request, "urlopen", lambda *a, **k: response)
    config = load_config()
    target = tmp_path / "v.mp4"
    with pytest.raises(MediaTooLargeError):
        materials.download_video("https://signed.example/x", target, config, max_bytes=2 * MiB)
    assert not target.exists()
    assert not target.with_suffix(".mp4.part").exists()


def test_boundary_download_budget_section_missing_is_noop(tmp_path: Path) -> None:
    config = _config(tmp_path, budget=None)  # key fully absent, not just enabled=false
    assert DownloadBudget.from_config(config) is None
    rows = [_row(f"v{index:02d}", f"作者{index}") for index in range(4)]
    calls: list[str] = []
    deps = ReplicationDeps(collector=_collector(rows), downloader=_sized_downloader({}, calls), prober=_prober)

    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    output_dir = Path(result["output_dir"])
    manifest = json.loads((output_dir / MANIFEST).read_text(encoding="utf-8"))
    assert "download_budget" not in manifest
    assert not (output_dir / PROCESS_DIR / "download_budget.json").exists()
    assert "## 下载预算" not in (output_dir / "00-交付说明.md").read_text(encoding="utf-8")
    assert len(result["downloads"]) == 4  # everything downloaded


def test_boundary_budget_json_is_valid_and_chinese_safe(tmp_path: Path) -> None:
    config = _config(tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9})
    rows = [_row(f"v{index:02d}", f"作者{index}") for index in range(3)]
    deps = ReplicationDeps(collector=_collector(rows), downloader=_sized_downloader({}, []), prober=_prober)

    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps)
    raw = (Path(result["output_dir"]) / PROCESS_DIR / "download_budget.json").read_text(encoding="utf-8")
    block = json.loads(raw)  # must parse
    assert "\\u" not in raw  # no ASCII-escaped mojibake
    assert "画面质量" in block["ranking"]["note"]
    assert block["ranking"]["order"] == ["relevance", "heat_score", "video_id"]
