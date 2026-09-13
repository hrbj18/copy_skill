"""Pre-download metadata prefilter for the material-replication workflow.

The gate must let the pipeline drop unusable candidates *before* spending
bandwidth, using only the search metadata that was already collected.  These
tests cover the pure function and both end-to-end modes (``--download-only`` and
the full selection chain), and pin the invariants that keep the change safe:

* ``enabled=false`` behaves exactly like the pre-change pipeline (no extra
  artifact, no manifest key, same downloads);
* a duration window removes out-of-range candidates with **zero** downloads;
* a missing ``duration_seconds`` (``<= 0``) is kept when
  ``allow_unknown_duration`` is true;
* the prefilter heat floor is independent of ``material_replica``'s;
* every rejection is auditable in ``prefilter.json`` and the manifest;
* "prefiltered empty" is reported distinctly from "empty pool".
"""

from __future__ import annotations

import json
from pathlib import Path

from douyin_intelligence.config import load_config
from douyin_intelligence.replication_candidates import Candidate
from douyin_intelligence.replication_pipeline import ReplicationDeps, run_material_replication
from douyin_intelligence.replication_selection import (
    VisualMetrics,
    prefilter_candidates,
    prefilter_settings,
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _row(
    video_id: str,
    author: str,
    *,
    duration: float = 60.0,
    digg: int = 100,
    url: str = "",
    aweme_type: str | None = None,
) -> dict:
    row = {
        "aweme_id": video_id,
        "desc": f"标题-{video_id}",
        "author": {"uid": f"uid-{author}", "nickname": author},
        "create_time": "2026-09-11T08:00:00+08:00",
        "statistics": {"digg_count": digg, "comment_count": 10, "share_count": 5, "collect_count": 20},
        "duration": duration,
        "share_url": f"https://www.douyin.com/video/{video_id}",
    }
    if url:
        row["video_download_url"] = url
    if aweme_type is not None:
        row["aweme_type"] = aweme_type
    return row


def _config(tmp_path: Path, *, prefilter: dict | None = None) -> dict:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    if prefilter is None:
        config["jobs"]["material_replication"].pop("prefilter", None)
    else:
        config["jobs"]["material_replication"]["prefilter"] = prefilter
    # Isolate the prefilter tests from the download-validation layer (real
    # ffprobe+ffmpeg); it has its own dedicated test module.
    config["jobs"]["material_replication"]["validation"] = {"enabled": False}
    return config


def _collector(rows: list[dict]):
    def collect(config, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None, **kwargs):
        source = Path(str(config.get("_project_root"))) / "raw" / "search_contents_1.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        assert before_sanitize is not None
        before_sanitize([source])
        return {"status": "success", "keywords": keywords, "budget": budget}
    return collect


class _Ocr:
    def run(self, video, duration, cache_dir, temp_dir):
        return {"status": "no_text", "items": [], "sampled_frames": 10}


class _Transcriber:
    def run(self, video, cache_dir, temp_dir, **kwargs):
        # Stage via ``cache_dir``: since P1a both stages share one video cache
        # root, so the source ``.mp4`` path no longer distinguishes them.
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


def _deps(tmp_path: Path, rows: list[dict], *, full: bool = False):
    """Injectable deps; returns ``(deps, downloaded_ids)`` where ids is a list."""
    downloaded: list[str] = []

    def downloader(url, destination, config):
        downloaded.append(Path(destination).stem)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"video-bytes")

    def prober(path, config):
        return {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}

    deps = ReplicationDeps(collector=_collector(rows), downloader=downloader, prober=prober)
    if full:
        deps.transcriber = _Transcriber()
        deps.ocr = _Ocr()
        deps.face_detector = _Face()
    return deps, downloaded


def _candidate(video_id: str, *, duration: float, heat: float) -> Candidate:
    candidate = Candidate(video_id=video_id, duration_seconds=duration)
    candidate.heat_score = heat
    return candidate


# --------------------------------------------------------------------------- #
# Pure function: prefilter_candidates
# --------------------------------------------------------------------------- #
def test_prefilter_disabled_returns_input_untouched() -> None:
    config = load_config()
    config["jobs"]["material_replication"]["prefilter"] = {"enabled": False, "min_seconds": 10, "max_seconds": 300}
    rows = [_candidate("a", duration=5, heat=1.0), _candidate("b", duration=999, heat=0.0)]
    passed, rejected = prefilter_candidates(rows, config)
    assert [c.video_id for c in passed] == ["a", "b"]
    assert rejected == []


def test_prefilter_missing_block_is_a_noop() -> None:
    config = load_config()
    config["jobs"]["material_replication"].pop("prefilter", None)
    rows = [_candidate("a", duration=5, heat=1.0)]
    passed, rejected = prefilter_candidates(rows, config)
    assert [c.video_id for c in passed] == ["a"]
    assert rejected == []
    assert prefilter_settings(config) == {}


def test_prefilter_duration_window_rejects_out_of_range_with_audit_fields() -> None:
    config = load_config()
    config["jobs"]["material_replication"]["prefilter"] = {
        "enabled": True, "min_seconds": 10, "max_seconds": 300,
        "heat_gate_percentile": 0.0, "allow_unknown_duration": True,
    }
    short = _candidate("short", duration=4, heat=1.0)
    long = _candidate("long", duration=600, heat=1.0)
    ok = _candidate("ok", duration=60, heat=1.0)
    passed, rejected = prefilter_candidates([short, long, ok], config)
    assert [c.video_id for c in passed] == ["ok"]
    assert {entry["video_id"] for entry in rejected} == {"short", "long"}
    assert {entry["stage"] for entry in rejected} == {"pre_duration"}
    short_entry = next(entry for entry in rejected if entry["video_id"] == "short")
    assert set(short_entry) >= {"video_id", "stage", "reason", "duration_seconds", "heat_score", "title", "author"}


def test_prefilter_keeps_unknown_duration_when_allowed() -> None:
    config = load_config()
    config["jobs"]["material_replication"]["prefilter"] = {
        "enabled": True, "min_seconds": 10, "max_seconds": 300, "allow_unknown_duration": True,
    }
    unknown = _candidate("unknown", duration=0, heat=1.0)
    passed, rejected = prefilter_candidates([unknown], config)
    assert [c.video_id for c in passed] == ["unknown"]
    assert rejected == []


def test_prefilter_rejects_unknown_duration_when_not_allowed() -> None:
    config = load_config()
    config["jobs"]["material_replication"]["prefilter"] = {
        "enabled": True, "min_seconds": 10, "max_seconds": 300, "allow_unknown_duration": False,
    }
    unknown = _candidate("unknown", duration=0, heat=1.0)
    passed, rejected = prefilter_candidates([unknown], config)
    assert passed == []
    assert rejected[0]["stage"] == "pre_duration"


def test_prefilter_heat_floor_is_opt_in_and_uses_pre_stage() -> None:
    config = load_config()
    config["jobs"]["material_replication"]["prefilter"] = {
        "enabled": True, "min_seconds": 0, "max_seconds": 0, "heat_gate_percentile": 0.5,
    }
    rows = [_candidate(str(i), duration=60, heat=i / 10) for i in range(1, 6)]
    passed, rejected = prefilter_candidates(rows, config)
    assert passed and rejected
    assert {entry["stage"] for entry in rejected} == {"pre_heat"}
    assert all(candidate.heat_score >= 0.3 for candidate in passed)


def test_prefilter_heat_floor_defaults_off() -> None:
    config = load_config()
    config["jobs"]["material_replication"]["prefilter"] = {"enabled": True, "min_seconds": 0, "max_seconds": 0}
    rows = [_candidate(str(i), duration=60, heat=i / 10) for i in range(1, 6)]
    passed, rejected = prefilter_candidates(rows, config)
    assert len(passed) == len(rows)
    assert rejected == []


# --------------------------------------------------------------------------- #
# Acceptance #2 / #3: download-only, no download for out-of-range candidates
# --------------------------------------------------------------------------- #
def test_prefilter_blocks_out_of_range_downloads(tmp_path: Path) -> None:
    config = _config(tmp_path, prefilter={
        "enabled": True, "min_seconds": 10, "max_seconds": 300,
        "heat_gate_percentile": 0.0, "allow_unknown_duration": True,
    })
    rows = [
        _row("kept-1", "作者A", url="https://signed.example/1", duration=60),
        _row("short", "作者B", url="https://signed.example/2", duration=4),     # too short
        _row("long", "作者C", url="https://signed.example/3", duration=900),    # too long
    ]
    deps, downloaded = _deps(tmp_path, rows)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    assert result["mode"] == "download_only"
    assert downloaded == ["kept-1"], downloaded
    assert "short" not in downloaded and "long" not in downloaded

    manifest = json.loads((Path(result["output_dir"]) / "清单.json").read_text(encoding="utf-8"))
    assert {item["video_id"] for item in manifest["downloads"]} == {"kept-1"}
    rejections = manifest["prefilter"]["rejections"]
    assert {entry["video_id"] for entry in rejections} == {"short", "long"}
    assert manifest["prefilter"]["pool_size"] == 3
    assert manifest["prefilter"]["passed"] == 1
    assert manifest["prefilter"]["rejected"] == 2


def test_prefilter_keeps_unknown_duration_candidate_in_download_only(tmp_path: Path) -> None:
    config = _config(tmp_path, prefilter={
        "enabled": True, "min_seconds": 10, "max_seconds": 300, "allow_unknown_duration": True,
    })
    rows = [
        _row("kept", "作者A", url="https://signed.example/1", duration=60),
        _row("unknown", "作者B", url="https://signed.example/2", duration=0),
    ]
    deps, downloaded = _deps(tmp_path, rows)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    assert set(downloaded) == {"kept", "unknown"}
    manifest = json.loads((Path(result["output_dir"]) / "清单.json").read_text(encoding="utf-8"))
    assert manifest["prefilter"]["rejected"] == 0


# --------------------------------------------------------------------------- #
# Acceptance #4: prefilter heat gate independent of material_replica heat gate
# --------------------------------------------------------------------------- #
def test_prefilter_heat_gate_independent_of_material_replica_gate(tmp_path: Path) -> None:
    config = _config(tmp_path, prefilter={
        "enabled": True, "min_seconds": 10, "max_seconds": 300,
        "heat_gate_percentile": 0.0, "allow_unknown_duration": True,
    })
    # A material-pool heat floor of 1.0 must NOT leak into download-only.
    config["jobs"]["material_replication"]["material_replica"]["heat_gate_percentile"] = 1.0
    rows = [
        _row("hot", "作者A", url="https://signed.example/1", digg=1000, duration=60),
        _row("cold", "作者B", url="https://signed.example/2", digg=1, duration=60),
    ]
    deps, downloaded = _deps(tmp_path, rows)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    assert set(downloaded) == {"hot", "cold"}
    assert result["status"] == "success"


# --------------------------------------------------------------------------- #
# Acceptance #5: rejections are auditable in prefilter.json and the manifest
# --------------------------------------------------------------------------- #
def test_rejections_recorded_in_prefilter_json_and_manifest(tmp_path: Path) -> None:
    config = _config(tmp_path, prefilter={
        "enabled": True, "min_seconds": 10, "max_seconds": 300,
        "heat_gate_percentile": 0.0, "allow_unknown_duration": True,
    })
    rows = [
        _row("kept", "作者A", url="https://signed.example/1", duration=60, digg=100),
        _row("dropped", "作者B", url="https://signed.example/2", duration=3, digg=50),
    ]
    deps, _ = _deps(tmp_path, rows)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    output_dir = Path(result["output_dir"])
    payload = json.loads((output_dir / "05-过程数据" / "prefilter.json").read_text(encoding="utf-8"))
    assert payload["enabled"] is True
    assert payload["pool_size"] == 2 and payload["passed"] == 1 and payload["rejected"] == 1
    entry = payload["rejections"][0]
    assert entry["video_id"] == "dropped"
    assert entry["stage"] == "pre_duration"
    assert entry["duration_seconds"] == 3.0
    assert entry["author"] == "作者B" and entry["title"]

    manifest = json.loads((output_dir / "清单.json").read_text(encoding="utf-8"))
    assert manifest["prefilter"]["rejections"][0]["video_id"] == "dropped"
    # Both counts are present and unambiguous.
    assert manifest["prefilter"]["pool_size"] == 2
    assert manifest["prefilter"]["passed"] == 1
    assert manifest["candidate_pool_size"] == 2
    assert manifest["counters"]["candidates"] == 2

    readme = (output_dir / "00-交付说明.md").read_text(encoding="utf-8")
    assert "## 下载前预筛" in readme
    assert "dropped" in readme and "pre_duration" in readme


# --------------------------------------------------------------------------- #
# Acceptance #6: prefiltered-empty is distinct from empty pool
# --------------------------------------------------------------------------- #
def test_prefilter_empty_reports_prefiltered_empty_not_empty_pool(tmp_path: Path) -> None:
    config = _config(tmp_path, prefilter={
        "enabled": True, "min_seconds": 10, "max_seconds": 300,
        "heat_gate_percentile": 0.0, "allow_unknown_duration": True,
    })
    rows = [
        _row("short1", "作者A", url="https://signed.example/1", duration=3),
        _row("short2", "作者B", url="https://signed.example/2", duration=5),
    ]
    deps, downloaded = _deps(tmp_path, rows)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    assert downloaded == []
    assert result["status"] == "failed"
    output_dir = Path(result["output_dir"])
    manifest = json.loads((output_dir / "清单.json").read_text(encoding="utf-8"))
    assert manifest["prefilter"]["conclusion"] == "prefiltered_empty"
    assert manifest["material_replica"]["conclusion"] == "prefiltered_empty"
    # The warning must state that candidates WERE collected, so the operator
    # cannot mistake this for "Douyin had no content".
    joined = "\n".join(manifest["warnings"])
    assert "候选池采集到 2 条" in joined
    assert "全部被下载前预筛剔除" in joined
    assert "候选池为空" not in joined


def test_truly_empty_pool_still_reports_empty_pool(tmp_path: Path) -> None:
    config = _config(tmp_path, prefilter={
        "enabled": True, "min_seconds": 10, "max_seconds": 300, "allow_unknown_duration": True,
    })
    deps, _ = _deps(tmp_path, [])
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    assert result["status"] == "failed"
    manifest = json.loads((Path(result["output_dir"]) / "清单.json").read_text(encoding="utf-8"))
    assert any("候选池为空" in warning for warning in manifest["warnings"])
    # No prefilter block confusion: nothing was collected to prefilter.
    assert manifest["material_replica"]["conclusion"] == "empty"


# --------------------------------------------------------------------------- #
# Acceptance #7: full chain honours the same gate
# --------------------------------------------------------------------------- #
def _visual_ok(*args, **kwargs):
    return VisualMetrics(sampled_frames=10, motion_frame_ratio=0.9, ocr_text_frame_ratio=0.0, visual_ok=True)


def test_full_chain_prefilter_blocks_out_of_range_downloads(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path, prefilter={
        "enabled": True, "min_seconds": 10, "max_seconds": 300,
        "heat_gate_percentile": 0.0, "allow_unknown_duration": True,
    })
    monkeypatch.setattr("douyin_intelligence.replication_pipeline.media_tool_available", lambda config, name: True)
    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics", _visual_ok)
    rows = [
        _row("kept", "作者A", url="https://signed.example/1", duration=60),
        _row("short", "作者B", url="https://signed.example/2", duration=4),
    ]
    deps, downloaded = _deps(tmp_path, rows, full=True)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", deps=deps,
    )
    assert "short" not in downloaded, downloaded
    output_dir = Path(result["output_dir"])
    manifest = json.loads((output_dir / "清单.json").read_text(encoding="utf-8"))
    assert {entry["video_id"] for entry in manifest["prefilter"]["rejections"]} == {"short"}
    assert manifest["prefilter"]["passed"] == 1
    # search_attribution keeps the *collected* pool size and adds the filtered one.
    assert manifest["search_attribution"]["pool_size"] == 2
    assert manifest["search_attribution"]["prefiltered_pool_size"] == 1


def test_full_chain_prefiltered_empty_is_distinct(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path, prefilter={
        "enabled": True, "min_seconds": 10, "max_seconds": 300,
        "heat_gate_percentile": 0.0, "allow_unknown_duration": True,
    })
    monkeypatch.setattr("douyin_intelligence.replication_pipeline.media_tool_available", lambda config, name: True)
    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics", _visual_ok)
    rows = [_row("short", "作者A", url="https://signed.example/1", duration=3)]
    deps, downloaded = _deps(tmp_path, rows, full=True)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", deps=deps,
    )
    assert downloaded == []
    output_dir = Path(result["output_dir"])
    manifest = json.loads((output_dir / "清单.json").read_text(encoding="utf-8"))
    assert manifest["material_replica"]["conclusion"] == "prefiltered_empty"
    assert any("全部被下载前预筛剔除" in warning for warning in manifest["warnings"])


# --------------------------------------------------------------------------- #
# Acceptance #1: enabled=false is behaviourally equivalent to the pre-change
# behaviour -- the diff is additive only (no removed/changed value) and the
# layer emits none of its own artifacts.
# --------------------------------------------------------------------------- #
def test_disabled_prefilter_emits_no_extra_artifact(tmp_path: Path) -> None:
    config = _config(tmp_path, prefilter={
        "enabled": False, "min_seconds": 10, "max_seconds": 300, "allow_unknown_duration": True,
    })
    rows = [
        _row("kept", "作者A", url="https://signed.example/1", duration=60),
        _row("short", "作者B", url="https://signed.example/2", duration=3),  # would be gated if enabled
    ]
    deps, downloaded = _deps(tmp_path, rows)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    assert set(downloaded) == {"kept", "short"}
    output_dir = Path(result["output_dir"])
    assert not (output_dir / "05-过程数据" / "prefilter.json").exists()
    manifest = json.loads((output_dir / "清单.json").read_text(encoding="utf-8"))
    assert "prefilter" not in manifest
    readme = (output_dir / "00-交付说明.md").read_text(encoding="utf-8")
    assert "下载前预筛" not in readme
