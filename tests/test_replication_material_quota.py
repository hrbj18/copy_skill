"""Delivered-bytes quota: the material chain's source-volume floor / ceiling.

The material chain used to stop the instant ``target_count`` sources were
selected, so a period whose best clips happened to be short could ship a tiny
delivery (the "24.86 MiB / 4 sources" complaint).  The *delivered-bytes quota*
adds:

* a **floor** (``min_delivered_bytes``) on the summed size of the selected source
  files -- the loop keeps scanning *past* ``target`` until the sum reaches it;
* a **ceiling** (``max_delivered_bytes``) -- a candidate that would overshoot is
  skipped, never appended, so the delivered set can never grow past it.

It counts only the selected source files and never touches ``DownloadBudget``.

Covered here:
* keys absent -> pure ``target`` behaviour (byte-identical to pre-change);
* floor -> scans past ``target`` until the byte floor is reached;
* ceiling -> an over-ceiling candidate is skipped, a smaller later one still fits;
* the pool exhausted below the floor -> ``insufficient`` / ``insufficient_bytes``;
* the quota never mutates ``budget.stopped_by``;
* two identical runs select identically (idempotent);
* config bounds (non-negative, floor <= ceiling) validated;
* a ceiling-only run never overshoots the ceiling.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from douyin_intelligence.config import ConfigurationError, load_config
from douyin_intelligence.materials import MediaTooLargeError
from douyin_intelligence.replication_candidates import Candidate
from douyin_intelligence.replication_pipeline import ReplicationDeps
from douyin_intelligence.replication_selection import (
    DownloadBudget,
    VisualMetrics,
    select_material_replicas,
)

_MIB = 1024 * 1024
_QUOTA_KEYS = ("min_delivered_bytes", "max_delivered_bytes", "max_selected_count")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _candidate(video_id: str, *, heat: float, author: str | None = None, duration: float = 60.0) -> Candidate:
    return Candidate(
        video_id=video_id,
        title=f"标题-{video_id}",
        author=author or f"作者-{video_id}",
        duration_seconds=duration,
        heat_score=heat,
    )


def _config(tmp_path: Path, *, material: dict | None = None, **quota: object) -> dict:
    """A material-chain config with the shipped quota keys reset to ``quota``.

    Every quota key is *removed* first, so a caller passes exactly the keys it
    wants in play; a call with no quota kwargs and a ``material`` override is
    therefore the *key-absent* config (shipped target_count=8 / min_count=2 are
    overridable through ``material``).
    """
    config = load_config()
    config["_project_root"] = str(tmp_path)
    # Isolate from the download-validation layer: the fakes below write non-media
    # bytes, so a real ffprobe+ffmpeg check does not apply.
    config["jobs"]["material_replication"]["validation"] = {"enabled": False}
    block = config["jobs"]["material_replication"]["material_replica"]
    for key in _QUOTA_KEYS:
        block.pop(key, None)
    if material:
        block.update(material)
    if quota:
        block.update(quota)
    return config


class _Ocr:
    def run(self, video, duration, cache_dir, temp_dir):
        return {"status": "no_text", "items": [], "sampled_frames": 10}


class _Transcriber:
    def run(self, video, cache_dir, temp_dir, **kwargs):
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


def _visual_ok(*args, **kwargs):
    return VisualMetrics(sampled_frames=10, motion_frame_ratio=0.9, ocr_text_frame_ratio=0.0, visual_ok=True)


def _deps(sizes: dict[str, int]) -> ReplicationDeps:
    """Deps whose downloader writes exactly ``sizes[video_id]`` bytes to disk."""

    def downloader(url, destination, config, *, max_bytes=None):
        video_id = Path(destination).stem
        size = sizes[video_id]
        if max_bytes is not None and size > max_bytes:
            raise MediaTooLargeError("视频声明体积超限", declared_bytes=size, limit=max_bytes)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"x" * size)

    def prober(path, config):
        return {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}

    deps = ReplicationDeps(downloader=downloader, prober=prober)
    deps.transcriber = _Transcriber()
    deps.ocr = _Ocr()
    deps.face_detector = _Face()
    return deps


def _selected_ids(result: dict) -> list[str]:
    return [item["candidate"].video_id for item in result["selected"]]


def _unmet_stages(result: dict) -> list[str]:
    return [entry["stage"] for entry in result["unmet"]]


# --------------------------------------------------------------------------- #
# 1. Keys absent -> pre-change behaviour (stop exactly at target)
# --------------------------------------------------------------------------- #
def test_keys_absent_behaves_like_pre_change(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics", _visual_ok)
    config = _config(tmp_path, material={"target_count": 3, "min_count": 2})
    sizes = {f"v{i}": 1000 for i in range(6)}
    candidates = [_candidate(f"v{i}", heat=100.0 - i) for i in range(6)]

    result = select_material_replicas(config, candidates, deps=_deps(sizes), validation_store=[])

    # The loop still stops the moment ``target`` is reached: no scan for volume.
    assert _selected_ids(result) == ["v0", "v1", "v2"]
    assert result["delivered_bytes"] == 3000
    assert result["insufficient"] is False
    assert result["stage"]["min_delivered_bytes"] == 0
    assert result["stage"]["max_delivered_bytes"] == 0
    quota = [entry for entry in result["unmet"] if entry["stage"] == "quota"]
    assert quota and all(entry["reason"] == "已达目标 3 条，未评估" for entry in quota)


# --------------------------------------------------------------------------- #
# 2. Floor -> keep scanning past target until the byte floor is met
# --------------------------------------------------------------------------- #
def test_floor_keeps_scanning_past_target(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics", _visual_ok)
    config = _config(tmp_path, material={"target_count": 2, "min_count": 1}, min_delivered_bytes=5000)
    sizes = {f"v{i}": 1000 for i in range(6)}
    candidates = [_candidate(f"v{i}", heat=100.0 - i) for i in range(6)]

    result = select_material_replicas(config, candidates, deps=_deps(sizes), validation_store=[])

    ids = _selected_ids(result)
    assert len(ids) == 5  # 2 was not enough; it kept going to reach 5000 B
    assert result["delivered_bytes"] == 5000
    assert result["delivered_bytes"] >= 5000
    assert result["insufficient"] is False
    # The single leftover still past target is reported under ``quota``.
    assert _unmet_stages(result).count("quota") == 1


# --------------------------------------------------------------------------- #
# 3. Ceiling -> an over-ceiling candidate is skipped, a smaller later one fits
# --------------------------------------------------------------------------- #
def test_ceiling_skips_oversize_candidate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics", _visual_ok)
    config = _config(tmp_path, material={"target_count": 2, "min_count": 1}, max_delivered_bytes=2500)
    sizes = {"v0": 2000, "v1": 2000, "v2": 500}
    candidates = [_candidate("v0", heat=100.0), _candidate("v1", heat=90.0), _candidate("v2", heat=80.0)]

    result = select_material_replicas(config, candidates, deps=_deps(sizes), validation_store=[])

    # v0 (2000) fits; v1 (2000) would make 4000 > 2500 -> skipped; v2 (500) -> 2500.
    assert _selected_ids(result) == ["v0", "v2"]
    assert result["delivered_bytes"] == 2500
    assert result["delivered_bytes"] <= 2500
    oversize = [entry for entry in result["unmet"] if entry["stage"] == "quota_bytes"]
    assert [entry["video_id"] for entry in oversize] == ["v1"]


# --------------------------------------------------------------------------- #
# 4. Pool exhausted below floor -> insufficient_bytes
# --------------------------------------------------------------------------- #
def test_pool_exhausted_below_floor_flags_insufficient_bytes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics", _visual_ok)
    config = _config(tmp_path, material={"target_count": 2, "min_count": 1}, min_delivered_bytes=10 ** 9)
    sizes = {f"v{i}": 1000 for i in range(3)}
    candidates = [_candidate(f"v{i}", heat=100.0 - i) for i in range(3)]

    result = select_material_replicas(config, candidates, deps=_deps(sizes), validation_store=[])

    # Count (3 >= min_count 1) is satisfied, yet the floor is missed -> the new
    # conclusion is spelled out instead of a misleading "success".
    assert result["insufficient"] is True
    assert result["status"] == "insufficient"
    assert result["stage"]["conclusion"] == "insufficient_bytes"
    assert len(_selected_ids(result)) == 3
    assert result["delivered_bytes"] == 3000
    assert any("低于下限" in warning for warning in result["warnings"])


# --------------------------------------------------------------------------- #
# 5. The quota never touches DownloadBudget's terminal stop
# --------------------------------------------------------------------------- #
def test_quota_never_touches_budget_stopped_by(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics", _visual_ok)
    config = _config(
        tmp_path, material={"target_count": 2, "min_count": 1},
        min_delivered_bytes=5000, max_delivered_bytes=10 ** 9,
    )
    budget = DownloadBudget(max_count=0, max_bytes=10 ** 9, max_item_bytes=10 ** 9)
    sizes = {f"v{i}": 1000 for i in range(6)}
    candidates = [_candidate(f"v{i}", heat=100.0 - i) for i in range(6)]

    result = select_material_replicas(
        config, candidates, deps=_deps(sizes), budget=budget, relevance={}, validation_store=[],
    )

    assert budget.stopped_by is None
    assert result["delivered_bytes"] >= 5000


# --------------------------------------------------------------------------- #
# 6. Two identical runs select identically (idempotent)
# --------------------------------------------------------------------------- #
def test_quota_selection_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics", _visual_ok)
    config = _config(tmp_path, material={"target_count": 2, "min_count": 1}, min_delivered_bytes=5000)
    sizes = {f"v{i}": 1000 for i in range(6)}
    candidates = [_candidate(f"v{i}", heat=100.0 - i) for i in range(6)]

    first = select_material_replicas(config, candidates, deps=_deps(sizes), validation_store=[])
    second = select_material_replicas(config, candidates, deps=_deps(sizes), validation_store=[])

    assert _selected_ids(first) == _selected_ids(second)
    assert first["delivered_bytes"] == second["delivered_bytes"]


# --------------------------------------------------------------------------- #
# 7. Config bounds are validated additively
# --------------------------------------------------------------------------- #
def _load_with_quota(tmp_path: Path, patch: dict):
    config = copy.deepcopy(load_config())
    config["jobs"]["material_replication"]["material_replica"].update(patch)
    path = tmp_path / f"cfg-{len(patch)}-{abs(hash(tuple(sorted(patch))))}.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return load_config(path)


def test_config_validates_quota_bounds(tmp_path: Path) -> None:
    # Valid: the 0 sentinel (unbounded) and a floor at/below the ceiling.
    _load_with_quota(tmp_path, {"min_delivered_bytes": 0, "max_delivered_bytes": 0, "max_selected_count": 0})
    _load_with_quota(tmp_path, {"min_delivered_bytes": 100, "max_delivered_bytes": 200})
    # A floor with the ceiling left unlimited (0) is legal.
    _load_with_quota(tmp_path, {"min_delivered_bytes": 300, "max_delivered_bytes": 0})
    # Invalid: negatives.
    for key in _QUOTA_KEYS:
        with pytest.raises(ConfigurationError):
            _load_with_quota(tmp_path, {key: -1})
    # Invalid: a floor sitting above a set ceiling.
    with pytest.raises(ConfigurationError):
        _load_with_quota(tmp_path, {"min_delivered_bytes": 300, "max_delivered_bytes": 200})


# --------------------------------------------------------------------------- #
# 8. A ceiling-only run never overshoots the ceiling
# --------------------------------------------------------------------------- #
def test_ceiling_only_run_never_overshoots(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics", _visual_ok)
    config = _config(tmp_path, material={"target_count": 8, "min_count": 1}, max_delivered_bytes=3500)
    sizes = {f"v{i}": 1000 for i in range(5)}
    candidates = [_candidate(f"v{i}", heat=100.0 - i) for i in range(5)]

    result = select_material_replicas(config, candidates, deps=_deps(sizes), validation_store=[])

    # 3 x 1000 fit; the 4th/5th (4000 > 3500) are skipped, so the sum never grows
    # past the ceiling.
    assert result["delivered_bytes"] == 3000
    assert result["delivered_bytes"] <= 3500
    assert len(_selected_ids(result)) == 3
    assert _unmet_stages(result).count("quota_bytes") == 2
