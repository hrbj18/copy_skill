"""Independent QA verification of the pre-download metadata prefilter.

Fresh-eyes falsification suite (Task #2).  It does NOT re-run the engineer's
``tests/test_replication_prefilter.py``; it attacks the seven claims with
different instruments:

* V1 instrument the pipeline to prove the gate runs *once* and both selection
  chains receive the *filtered* list (not the raw pool);
* V3 pin the "collected vs entered-download" counting invariant;
* V4 distinguish empty-pool / prefiltered-empty / single-survivor;
* V5 prove the prefilter heat gate is decoupled from the material-pool gate;
* V6 prove ``candidate_pool.json`` / ``scoring.json`` stay full;
* V7 readme section appears only when the gate is active;
* E* edge cases the engineer did not cover (boundaries, negative duration,
  percentile limits, missing block, malformed min>max, empty input, JSON
  integrity, forbidden-backend probe on the download-only path).

All external effects are injected and ``_project_root`` is redirected to
``tmp_path``; the real ``data/`` / ``output/`` trees are never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from douyin_intelligence.config import ConfigurationError, load_config
from douyin_intelligence.replication_candidates import Candidate
from douyin_intelligence.replication_pipeline import ReplicationDeps, run_material_replication
from douyin_intelligence.replication_selection import prefilter_candidates, prefilter_settings

_PROCESS = "05-过程数据"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _row(video_id, author, *, duration=60.0, digg=100, url="", aweme_type=None):
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


def _config(tmp_path, *, prefilter: "dict | None | str" = "keep"):
    """``keep`` -> leave the shipped default; ``None`` -> delete the block."""
    config = load_config()
    config["_project_root"] = str(tmp_path)
    if prefilter == "keep":
        pass
    elif prefilter is None:
        config["jobs"]["material_replication"].pop("prefilter", None)
    else:
        config["jobs"]["material_replication"]["prefilter"] = prefilter
    # Isolate the prefilter verification from the download-validation layer
    # (real ffprobe+ffmpeg) -- it has its own dedicated test module.
    config["jobs"]["material_replication"]["validation"] = {"enabled": False}
    return config


def _collector(rows):
    def collect(config, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None):
        source = Path(str(config.get("_project_root"))) / "raw" / "search_contents_1.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        if before_sanitize is not None:
            before_sanitize([source])
        return {"status": "success", "keywords": keywords, "budget": budget}
    return collect


def _deps(rows, *, full=False, forbidden=False, downloaded=None):
    log = downloaded if downloaded is not None else []

    def downloader(url, destination, config):
        log.append(Path(destination).stem)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"video-bytes")

    def prober(path, config):
        return {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}

    deps = ReplicationDeps(collector=_collector(rows), downloader=downloader, prober=prober)
    deps._downloaded_ids = log  # convenience handle
    if full:
        deps.face_detector = _OkFace() if not forbidden else _Forbidden("face_detector")
        deps.ocr = _Forbidden("ocr") if forbidden else _NullOcr()
        deps.transcriber = _Forbidden("transcriber") if forbidden else _NullTranscriber()
    if forbidden:
        deps.slicer = _Forbidden("slicer")
    return deps


class _OkFace:
    backend = "stub"

    def status(self):
        return {"backend": "stub", "status": "ok", "model_present": False}


class _NullOcr:
    def run(self, video, duration, cache_dir, temp_dir):
        return {"status": "no_text", "items": [], "sampled_frames": 0}


class _NullTranscriber:
    def run(self, video, cache_dir, temp_dir, **kwargs):
        return {"status": "no_speech", "text": "", "segments": []}


class _Forbidden:
    """Any attribute access or call proves an unused backend was touched."""

    def __init__(self, label):
        self.__dict__["_label"] = label

    def __getattr__(self, name):
        raise AssertionError(f"download-only 不应访问 {self.__dict__['_label']}.{name}")

    def __call__(self, *args, **kwargs):
        raise AssertionError(f"download-only 不应调用 {self.__dict__['_label']}")


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _cand(video_id, *, duration, heat):
    c = Candidate(video_id=video_id, duration_seconds=duration)
    c.heat_score = heat
    return c


_ENABLED = {
    "enabled": True, "min_seconds": 10, "max_seconds": 300,
    "heat_gate_percentile": 0.0, "allow_unknown_duration": True,
}


# --------------------------------------------------------------------------- #
# V1 (Claim 2): the gate runs once; both chains see the filtered list
# --------------------------------------------------------------------------- #
def test_v1_gate_runs_once_and_both_chains_get_filtered_list(tmp_path, monkeypatch):
    import douyin_intelligence.replication_pipeline as pipeline

    calls: list[list[str]] = []
    real_prefilter = pipeline.prefilter_candidates

    def counting_prefilter(candidates, config):
        calls.append([c.video_id for c in candidates])
        return real_prefilter(candidates, config)

    script_seen: list[list[str]] = []
    material_seen: list[list[str]] = []

    def fake_script(config, candidates, **kwargs):
        script_seen.append([c.video_id for c in candidates])
        return {
            "status": "not_found", "unmet": [{"video_id": "", "stage": "pool", "reason": "stub"}],
            "errors": [], "downloaded": 0,
            "stage": {"candidate_pool": len(candidates), "asr_attempted": 0, "rejected": 0,
                      "errors": 0, "conclusion": "not_found"},
        }

    def fake_material(config, candidates, **kwargs):
        material_seen.append([c.video_id for c in candidates])
        return {
            "status": "not_found", "selected": [], "insufficient": True, "warnings": [], "unmet": [], "errors": [],
            "counters": {"downloaded": 0, "face_checked": 0, "face_errors": 0, "invalid_media": 0,
                         "clips_rejected_face_heavy": 0, "clips_rejected_duration": 0, "rejected_pool": 0,
                         "rejected_author_duplicate": 0, "rejected_visual": 0, "rejected_speech": 0,
                         "rejected_not_video": 0},
            "stage": {"conclusion": "stub", "candidate_pool": len(candidates), "heat_median": 0.0, "face_checked": 0},
        }

    monkeypatch.setattr(pipeline, "prefilter_candidates", counting_prefilter)
    monkeypatch.setattr(pipeline, "select_script_replica", fake_script)
    monkeypatch.setattr(pipeline, "select_material_replicas", fake_material)
    monkeypatch.setattr(pipeline, "media_tool_available", lambda config, name: False)

    config = _config(tmp_path, prefilter=_ENABLED)
    rows = [
        _row("kept", "作者A", url="https://signed.example/1", duration=60),
        _row("short", "作者B", url="https://signed.example/2", duration=4),
        _row("long", "作者C", url="https://signed.example/3", duration=900),
    ]
    run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", deps=_deps(rows, full=True))

    # Gate ran exactly once, on the RAW pool.
    assert len(calls) == 1, calls
    assert set(calls[0]) == {"kept", "short", "long"}
    # Both chains received the SAME filtered list, never the raw one.
    assert script_seen == [["kept"]], script_seen
    assert material_seen == [["kept"]], material_seen


# --------------------------------------------------------------------------- #
# V3 (Claim 3): collected vs entered-download counting is unambiguous
# --------------------------------------------------------------------------- #
def test_v3_collected_count_stays_raw_while_prefilter_count_is_separate(tmp_path):
    config = _config(tmp_path, prefilter=_ENABLED)
    rows = [
        _row("kept", "作者A", url="https://signed.example/1", duration=60),
        _row("short", "作者B", url="https://signed.example/2", duration=4),
        _row("long", "作者C", url="https://signed.example/3", duration=900),
    ]
    deps = _deps(rows)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    manifest = _read_json(Path(result["output_dir"]) / "清单.json")

    collected = 3
    assert manifest["candidate_pool_size"] == collected
    assert manifest["counters"]["candidates"] == collected
    assert manifest["search_attribution"]["pool_size"] == collected
    assert manifest["prefilter"]["pool_size"] == collected

    # The post-gate numbers are different, and specific.
    assert manifest["prefilter"]["passed"] == 1
    assert manifest["prefilter"]["rejected"] == 2
    assert manifest["search_attribution"]["prefiltered_pool_size"] == 1
    # "entered download" == downloads actually attempted here.
    assert len(manifest["downloads"]) == manifest["prefilter"]["passed"] == 1
    assert set(deps._downloaded_ids) == {"kept"}

    # Sanity: the three "collected" fields are always equal to each other, so no
    # reader can see three different notions of "pool size".
    assert manifest["candidate_pool_size"] == manifest["search_attribution"]["pool_size"] == manifest["counters"]["candidates"]


# --------------------------------------------------------------------------- #
# V4 (Claim 4): empty / prefiltered_empty / single-survivor are distinct
# --------------------------------------------------------------------------- #
def test_v4a_genuinely_empty_pool_reports_empty(tmp_path):
    config = _config(tmp_path, prefilter=_ENABLED)
    deps = _deps([])
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    out = Path(result["output_dir"])
    manifest = _read_json(out / "清单.json")
    joined = "\n".join(manifest["warnings"])
    assert manifest["material_replica"]["conclusion"] == "empty"
    assert "候选池为空" in joined
    assert "全部被下载前预筛剔除" not in joined
    # Nothing to prefilter -> no prefilter artifact, no prefilter key.
    assert not (out / _PROCESS / "prefilter.json").exists()
    assert "prefilter" not in manifest


def test_v4b_all_rejected_reports_prefiltered_empty(tmp_path):
    config = _config(tmp_path, prefilter=_ENABLED)
    rows = [
        _row("short1", "作者A", url="https://signed.example/1", duration=3),
        _row("short2", "作者B", url="https://signed.example/2", duration=5),
    ]
    deps = _deps(rows)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    out = Path(result["output_dir"])
    manifest = _read_json(out / "清单.json")
    joined = "\n".join(manifest["warnings"])
    assert deps._downloaded_ids == []
    assert manifest["prefilter"]["conclusion"] == "prefiltered_empty"
    assert manifest["material_replica"]["conclusion"] == "prefiltered_empty"
    assert "候选池采集到 2 条" in joined and "全部被下载前预筛剔除" in joined
    assert "候选池为空" not in joined
    # prefilter.json IS written here (something was collected and then cut).
    assert (out / _PROCESS / "prefilter.json").is_file()


def test_v4c_single_survivor_is_applied_not_prefiltered_empty(tmp_path):
    """N collected, gate leaves exactly 1 -- must NOT say 'prefiltered_empty'."""
    config = _config(tmp_path, prefilter=_ENABLED)
    rows = [
        _row("kept", "作者A", url="https://signed.example/1", duration=60),
        _row("short1", "作者B", url="https://signed.example/2", duration=4),
        _row("short2", "作者C", url="https://signed.example/3", duration=5),
    ]
    deps = _deps(rows)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    out = Path(result["output_dir"])
    manifest = _read_json(out / "清单.json")
    joined = "\n".join(manifest["warnings"])
    assert deps._downloaded_ids == ["kept"]
    assert result["status"] == "success"
    assert manifest["prefilter"]["conclusion"] == "applied"
    assert manifest["prefilter"]["passed"] == 1
    assert manifest["material_replica"]["conclusion"] == "download_only"
    assert "全部被下载前预筛剔除" not in joined
    assert "候选池为空" not in joined


# --------------------------------------------------------------------------- #
# V5 (Claim 5): prefilter heat gate is decoupled from material-replica gate
# --------------------------------------------------------------------------- #
def test_v5a_material_gate_1_0_does_not_leak_into_download_only(tmp_path):
    config = _config(tmp_path, prefilter=_ENABLED)
    config["jobs"]["material_replication"].setdefault("material_replica", {})["heat_gate_percentile"] = 1.0
    rows = [
        _row("hot", "作者A", url="https://signed.example/1", digg=1000, duration=60),
        _row("cold", "作者B", url="https://signed.example/2", digg=1, duration=60),
    ]
    deps = _deps(rows)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    assert set(deps._downloaded_ids) == {"hot", "cold"}
    assert result["status"] == "success"


def test_v5b_prefilter_heat_percentile_1_0_keeps_top_heat_set(tmp_path):
    config = _config(tmp_path, prefilter={
        "enabled": True, "min_seconds": 0, "max_seconds": 0, "heat_gate_percentile": 1.0,
    })
    rows = [
        _row("h1", "作者A", url="https://signed.example/1", digg=1000, duration=60),
        _row("h2", "作者B", url="https://signed.example/2", digg=500, duration=60),
        _row("h3", "作者C", url="https://signed.example/3", digg=100, duration=60),
    ]
    deps = _deps(rows)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    manifest = _read_json(Path(result["output_dir"]) / "清单.json")
    # percentile=1.0 keeps only the top-heat candidate(s); never empties the pool.
    assert deps._downloaded_ids == ["h1"], deps._downloaded_ids
    assert manifest["prefilter"]["passed"] == 1
    assert manifest["prefilter"]["conclusion"] == "applied"


def test_v5c_hot_candidate_can_be_cut_by_the_DURATION_gate(tmp_path):
    """Answering 'is it a bug if 1.0 empties the pool?': only the *duration*
    gate can cut the top-heat survivor; the heat gate alone cannot."""
    config = _config(tmp_path, prefilter={
        "enabled": True, "min_seconds": 10, "max_seconds": 300, "heat_gate_percentile": 1.0,
    })
    rows = [
        _row("hotlong", "作者A", url="https://signed.example/1", digg=1000, duration=900),  # hottest but too long
        _row("cool", "作者B", url="https://signed.example/2", digg=100, duration=60),
    ]
    deps = _deps(rows)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    manifest = _read_json(Path(result["output_dir"]) / "清单.json")
    # hotlong dropped by duration, cool dropped by heat -> legitimately empty.
    assert deps._downloaded_ids == []
    assert manifest["prefilter"]["conclusion"] == "prefiltered_empty"
    stages = {e["video_id"]: e["stage"] for e in manifest["prefilter"]["rejections"]}
    assert stages == {"hotlong": "pre_duration", "cool": "pre_heat"}


# --------------------------------------------------------------------------- #
# V6 (Claim 6): raw evidence (candidate_pool.json / scoring.json) stays full
# --------------------------------------------------------------------------- #
def test_v6_candidate_pool_and_scoring_keep_dropped_candidates(tmp_path):
    config = _config(tmp_path, prefilter=_ENABLED)
    rows = [
        _row("kept", "作者A", url="https://signed.example/1", duration=60),
        _row("short", "作者B", url="https://signed.example/2", duration=4),
        _row("long", "作者C", url="https://signed.example/3", duration=900),
    ]
    deps = _deps(rows)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    out = Path(result["output_dir"])

    pool = _read_json(out / _PROCESS / "candidate_pool.json")
    pool_ids = {c["video_id"] for c in pool["candidates"]}
    assert pool["pool_size"] == 3
    assert pool_ids == {"kept", "short", "long"}, pool_ids  # dropped rows remain

    scoring = _read_json(out / _PROCESS / "scoring.json")
    scoring_ids = {c["video_id"] for c in scoring["candidates"]}
    assert scoring["pool_size"] == 3
    assert scoring_ids == {"kept", "short", "long"}, scoring_ids


# --------------------------------------------------------------------------- #
# V7 (Claim 7): readme section present iff the gate is active
# --------------------------------------------------------------------------- #
def test_v7a_disabled_readme_has_no_prefilter_section(tmp_path):
    config = _config(tmp_path, prefilter={"enabled": False, "min_seconds": 10, "max_seconds": 300})
    rows = [
        _row("kept", "作者A", url="https://signed.example/1", duration=60),
        _row("short", "作者B", url="https://signed.example/2", duration=3),
        _row("nourl", "作者C", duration=60),  # -> no_media_url failure
    ]
    deps = _deps(rows)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    out = Path(result["output_dir"])
    readme = (out / "00-交付说明.md").read_text(encoding="utf-8")
    assert "下载前预筛" not in readme
    assert set(deps._downloaded_ids) == {"kept", "short"}
    # Locked sections still render (same three as tests/test_replication_pipeline.py).
    assert "## 下载清单" in readme
    assert "## 下载失败" in readme
    assert "模式：仅采集与下载" in readme


def test_v7b_enabled_readme_has_full_section(tmp_path):
    config = _config(tmp_path, prefilter=_ENABLED)
    rows = [
        _row("kept", "作者A", url="https://signed.example/1", duration=60),
        _row("short", "作者B", url="https://signed.example/2", duration=3),
        _row("nourl", "作者C", duration=60),  # -> no_media_url failure
    ]
    deps = _deps(rows)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    readme = (Path(result["output_dir"]) / "00-交付说明.md").read_text(encoding="utf-8")
    assert "## 下载前预筛" in readme
    assert "采集候选 3 条，通过 2 条，剔除 1 条" in readme
    assert "short" in readme and "pre_duration" in readme
    assert "## 下载清单" in readme and "## 下载失败" in readme and "模式：仅采集与下载" in readme


# --------------------------------------------------------------------------- #
# E1 (boundaries): the duration window is CLOSED on both ends
# --------------------------------------------------------------------------- #
def test_e1_duration_window_is_inclusive(tmp_path):
    config = _config(tmp_path, prefilter=_ENABLED)  # 10..300
    at_min = _cand("at_min", duration=10, heat=1.0)
    at_max = _cand("at_max", duration=300, heat=1.0)
    passed, rejected = prefilter_candidates([at_min, at_max], config)
    assert {c.video_id for c in passed} == {"at_min", "at_max"}
    assert rejected == []


def test_e1b_just_outside_window_is_rejected(tmp_path):
    config = _config(tmp_path, prefilter=_ENABLED)
    below = _cand("below", duration=9.999, heat=1.0)
    above = _cand("above", duration=300.001, heat=1.0)
    passed, rejected = prefilter_candidates([below, above], config)
    assert passed == []
    assert {e["video_id"] for e in rejected} == {"below", "above"}
    assert {e["stage"] for e in rejected} == {"pre_duration"}


# --------------------------------------------------------------------------- #
# E2: negative duration
# --------------------------------------------------------------------------- #
def test_e2_negative_duration_treated_as_unknown(tmp_path):
    config = _config(tmp_path, prefilter=_ENABLED)  # allow_unknown_duration=True
    passed, rejected = prefilter_candidates([_cand("neg", duration=-1, heat=1.0)], config)
    # Documented behaviour: <=0 counts as "unknown", so it is kept (not crashed/hidden).
    assert [c.video_id for c in passed] == ["neg"]
    assert rejected == []


def test_e2b_negative_duration_rejected_when_unknowns_disallowed(tmp_path):
    config = _config(tmp_path, prefilter={
        "enabled": True, "min_seconds": 10, "max_seconds": 300, "allow_unknown_duration": False,
    })
    passed, rejected = prefilter_candidates([_cand("neg", duration=-1, heat=1.0)], config)
    assert passed == []
    assert rejected[0]["stage"] == "pre_duration"


# --------------------------------------------------------------------------- #
# E3: percentile limits 0.0 / 1.0
# --------------------------------------------------------------------------- #
def test_e3_heat_percentile_0_0_is_off(tmp_path):
    config = _config(tmp_path, prefilter={"enabled": True, "min_seconds": 0, "max_seconds": 0,
                                          "heat_gate_percentile": 0.0})
    rows = [_cand(str(i), duration=60, heat=i / 10) for i in range(1, 6)]
    passed, rejected = prefilter_candidates(rows, config)
    assert len(passed) == 5 and rejected == []


def test_e3b_heat_percentile_1_0_keeps_only_max(tmp_path):
    config = _config(tmp_path, prefilter={"enabled": True, "min_seconds": 0, "max_seconds": 0,
                                          "heat_gate_percentile": 1.0})
    rows = [_cand(str(i), duration=60, heat=float(i)) for i in range(1, 6)]
    passed, rejected = prefilter_candidates(rows, config)
    assert [c.video_id for c in passed] == ["5"]
    assert {e["stage"] for e in rejected} == {"pre_heat"}


# --------------------------------------------------------------------------- #
# E4: prefilter block completely absent (not just enabled=false)
# --------------------------------------------------------------------------- #
def test_e4_missing_prefilter_block_is_a_noop(tmp_path):
    config = _config(tmp_path, prefilter=None)
    assert prefilter_settings(config) == {}
    rows = [_cand("a", duration=1, heat=1.0), _cand("b", duration=9999, heat=1.0)]
    passed, rejected = prefilter_candidates(rows, config)
    assert [c.video_id for c in passed] == ["a", "b"] and rejected == []


def test_e4b_missing_block_pipeline_emits_no_prefilter_artifact(tmp_path):
    config = _config(tmp_path, prefilter=None)
    rows = [_row("kept", "作者A", url="https://signed.example/1", duration=3)]  # would be gated if present
    deps = _deps(rows)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    out = Path(result["output_dir"])
    assert set(deps._downloaded_ids) == {"kept"}
    assert not (out / _PROCESS / "prefilter.json").exists()
    assert "prefilter" not in _read_json(out / "清单.json")


# --------------------------------------------------------------------------- #
# E5: malformed min>max injected directly (bypassing config validation)
# --------------------------------------------------------------------------- #
def test_e5_min_greater_than_max_does_not_crash(tmp_path):
    config = _config(tmp_path, prefilter={
        "enabled": True, "min_seconds": 300, "max_seconds": 10, "allow_unknown_duration": True,
    })
    rows = [_cand("a", duration=60, heat=1.0), _cand("unknown", duration=0, heat=1.0)]
    # Must not raise; documents the deterministic (pathological) outcome.
    passed, rejected = prefilter_candidates(rows, config)
    assert {c.video_id for c in passed} == {"unknown"}  # positive durations all cut
    assert {e["video_id"] for e in rejected} == {"a"}


def _shipped_config_source() -> dict:
    from douyin_intelligence import config as config_module
    path = config_module.project_root() / "config" / "content_intelligence.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_e5b_config_load_rejects_min_greater_than_max(tmp_path):
    source = _shipped_config_source()
    source["jobs"]["material_replication"]["prefilter"] = {
        "enabled": True, "min_seconds": 300, "max_seconds": 10,
    }
    bad = tmp_path / "cfg.json"
    bad.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_config(bad)


def test_e5c_config_load_rejects_bad_percentile(tmp_path):
    source = _shipped_config_source()
    source["jobs"]["material_replication"]["prefilter"] = {"enabled": True, "heat_gate_percentile": 1.5}
    bad = tmp_path / "cfg.json"
    bad.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_config(bad)


# --------------------------------------------------------------------------- #
# E6: empty candidate list
# --------------------------------------------------------------------------- #
def test_e6_empty_candidate_list_is_safe(tmp_path):
    config = _config(tmp_path, prefilter=_ENABLED)
    assert prefilter_candidates([], config) == ([], [])


# --------------------------------------------------------------------------- #
# E7: prefilter.json structure + Chinese not garbled
# --------------------------------------------------------------------------- #
def test_e7_prefilter_json_is_valid_and_readable_chinese(tmp_path):
    config = _config(tmp_path, prefilter=_ENABLED)
    rows = [_row("dropped", "作者乙", url="https://signed.example/2", duration=3)]
    deps = _deps(rows)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    out = Path(result["output_dir"])
    raw = (out / _PROCESS / "prefilter.json").read_text(encoding="utf-8")
    payload = json.loads(raw)  # valid JSON
    assert payload["schema_version"] == 1
    assert payload["enabled"] is True
    assert payload["conclusion"] == "prefiltered_empty"
    assert payload["rejections"][0]["reason"].startswith("时长")  # readable Chinese, no mojibake
    assert "\\u" not in raw  # stored ensure_ascii=False


# --------------------------------------------------------------------------- #
# E8: the download-only path still touches no face/ocr/asr/slicer
# --------------------------------------------------------------------------- #
def test_e8_forbidden_backends_untouched_with_gate_enabled(tmp_path):
    config = _config(tmp_path, prefilter=_ENABLED)
    rows = [
        _row("kept", "作者A", url="https://signed.example/1", duration=60),
        _row("short", "作者B", url="https://signed.example/2", duration=3),
    ]
    # Must not raise even though those deps are boobytrapped.
    run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True,
        deps=_deps(rows, forbidden=True),
    )


def test_e8b_forbidden_backends_untouched_on_prefiltered_empty(tmp_path):
    config = _config(tmp_path, prefilter=_ENABLED)
    rows = [_row("short", "作者B", url="https://signed.example/2", duration=3)]
    run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True,
        deps=_deps(rows, forbidden=True),
    )
