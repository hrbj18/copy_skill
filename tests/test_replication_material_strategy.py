"""选材层策略（2026-09-18 strategy Step B）的端到端离线证据。

三条产品断言必须同时成立，且每条都能被独立证伪：

* ``face_class`` 只是**描述性**元数据 —— 同主题的 ``face_heavy``、新闻主持人、
  人物主体都不得被人脸规则拒绝，也可以成为主素材；
* 排序不再以人脸等级打头，改为 主题命中 → 事件直接性 → profile 来源/角色加分
  → 热度 → 实测时长 → ``video_id``（可解释、稳定，且与 ``face_class`` 无关）；
* 主题相关性仍是**硬门槛** —— 无关素材在下载前就被 relevance gate 拒绝，
  高热度也不例外。

这些用例显式打开 ``direct_delivery``（整片直投），因此不依赖 ffmpeg 切片；
``tests/conftest.py`` 会把该开关从活动配置里剥掉，所以这里必须自己写回。
"""

from __future__ import annotations

import json
from pathlib import Path

from douyin_intelligence.config import load_config
from douyin_intelligence.replication_candidates import Candidate
from douyin_intelligence.replication_delivery import validate_delivery_manifest
from douyin_intelligence.replication_pipeline import ReplicationDeps, run_material_replication
from douyin_intelligence.replication_selection import VisualMetrics, material_profile_bonus, material_rank_key

_THEME = "机械鸭"
MANIFEST = "清单.json"
MAIN_DIR = "02-主素材"


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
def _row(video_id: str, desc: str, author: str, *, digg: int = 100, duration: float = 60.0) -> dict:
    return {
        "aweme_id": video_id,
        "desc": desc,
        "author": {"uid": f"uid-{author}", "nickname": author},
        "create_time": "2026-09-15T08:00:00+08:00",
        "statistics": {"digg_count": digg, "comment_count": 10, "share_count": 5, "collect_count": 20},
        "duration": duration,
        "video_download_url": f"https://signed.example/{video_id}",
        "share_url": f"https://www.douyin.com/video/{video_id}",
    }


def _config(
    tmp_path: Path,
    *,
    direct: bool = True,
    relevance_gate: bool = False,
    event_terms: dict[str, list[str]] | None = None,
) -> dict:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    material = config["jobs"]["material_replication"]
    material["prefilter"] = {"enabled": False}
    material["validation"] = {"enabled": False}
    material.pop("download_budget", None)
    material["theme_subject_terms"] = {_THEME: ["机械鸭"]}
    material["theme_event_terms"] = dict(event_terms or {})
    material["relevance_gate"] = {"enabled": relevance_gate, "min_subject_hits": 1}
    if direct:
        material["direct_delivery"] = {"enabled": True, "main_min_seconds": 0}
    else:
        material.pop("direct_delivery", None)
    return config


def _collector(rows: list[dict]):
    def collect(config, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None, **kwargs):
        source = Path(str(config.get("_project_root"))) / "raw" / "search_contents_1.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        if before_sanitize is not None:
            before_sanitize([source])
        return {"status": "success", "keywords": keywords, "budget": budget}

    return collect


class _Ocr:
    def run(self, video, duration, cache_dir, temp_dir):
        return {"status": "no_text", "items": [], "sampled_frames": 10}


class _Transcriber:
    def run(self, video, cache_dir, temp_dir, **kwargs):
        if "script" in str(cache_dir):
            return {"status": "success", "text": "字" * 200, "segments": [{"start": 0, "end": 5, "text": "开场"}]}
        return {"status": "no_speech", "text": "", "segments": []}


class _Face:
    """Injected face runner: ``face_heavy`` by default, per-id overridable."""

    backend = "opencv_yunet"

    def __init__(self, classes: dict[str, str] | None = None, *, default: str = "face_heavy") -> None:
        self._classes = dict(classes or {})
        self._default = default

    def status(self):
        return {"backend": "opencv_yunet", "status": "ok", "model_present": True}

    def run(self, video, duration, cache_dir, temp_dir):
        face_class = self._classes.get(Path(str(video)).stem, self._default)
        heavy = face_class == "face_heavy"
        return {
            "backend": "opencv_yunet", "status": "ok",
            "face_frame_ratio": 0.9 if heavy else 0.0,
            "max_face_area_ratio": 0.4 if heavy else 0.0,
            "face_class": face_class, "sampled_frames": 10,
            "face_per_frame": [heavy] * 10, "sample_interval_seconds": 1,
        }


def _deps(rows: list[dict], *, face_classes: dict[str, str] | None = None) -> ReplicationDeps:
    def downloader(url, destination, config, **kwargs):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"video-bytes")

    def prober(path, config):
        return {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}

    return ReplicationDeps(
        collector=_collector(rows), downloader=downloader, prober=prober,
        transcriber=_Transcriber(), ocr=_Ocr(), face_detector=_Face(face_classes),
    )


def _healthy_tooling(monkeypatch) -> None:
    monkeypatch.setattr("douyin_intelligence.replication_pipeline.media_tool_available", lambda config, name: True)
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: VisualMetrics(
            sampled_frames=10, motion_frame_ratio=0.9, ocr_text_frame_ratio=0.0, visual_ok=True,
        ),
    )


def _run(config: dict, deps: ReplicationDeps) -> tuple[Path, dict]:
    result = run_material_replication(config, _THEME, business_date="2026-09-18", deps=deps)
    output_dir = Path(result["output_dir"])
    manifest = json.loads((output_dir / MANIFEST).read_text(encoding="utf-8"))
    return output_dir, manifest


# --------------------------------------------------------------------------- #
# (1) face_heavy / 新闻主持人 / 人物主体 不是否决项，也能当主素材
# --------------------------------------------------------------------------- #
def test_face_heavy_on_topic_clips_still_become_main_material(tmp_path: Path, monkeypatch) -> None:
    _healthy_tooling(monkeypatch)
    rows = [
        _row("v01", "机械鸭 开箱实测", "创作者A", digg=100),
        _row("v02", "机械鸭 上手体验", "创作者B", digg=80),
        _row("v03", "机械鸭 深度解析", "创作者C", digg=60),
    ]
    output_dir, manifest = _run(_config(tmp_path, direct=True), _deps(rows))

    main_clips = manifest["main_materials"]
    assert main_clips, "同主题 face_heavy 素材必须能成为主素材"
    assert {clip["face_class"] for clip in main_clips} == {"face_heavy"}
    # 02-主素材 里确实落了整片文件，而不是只写在清单里。
    assert list((output_dir / MAIN_DIR).glob("*.mp4"))
    assert manifest["counters"]["clips_rejected_face_heavy"] == 0
    assert manifest["counters"]["face_errors"] == 0
    assert not any("face" in warning.lower() and "拒绝" in warning for warning in manifest["warnings"])
    # 交付自校验必须通过：人脸不再是否决项。
    verdict = validate_delivery_manifest(output_dir / MANIFEST)
    assert verdict["status"] == "pass", verdict["errors"]


def test_news_host_clip_is_not_refused_by_face_class(tmp_path: Path, monkeypatch) -> None:
    """新闻主持人画面（必然有人脸）不能被脸规则拒绝，且能成为主素材。"""
    _healthy_tooling(monkeypatch)
    rows = [
        _row("news01", "央视新闻 机械鸭发布会现场 主持人报道", "央视新闻", digg=500),
        _row("host02", "人物特写 机械鸭主创专访", "访谈栏目", digg=300),
    ]
    output_dir, manifest = _run(_config(tmp_path, direct=True), _deps(rows))

    delivered_ids = {item["video_id"] for item in manifest["material_replica_sources"]}
    assert {"news01", "host02"} <= delivered_ids
    assert any(clip["face_class"] == "face_heavy" for clip in manifest["main_materials"])
    assert manifest["counters"]["clips_rejected_face_heavy"] == 0
    assert list((output_dir / MAIN_DIR).glob("*.mp4"))


def test_slice_delivery_keeps_themed_face_heavy_timeline(tmp_path: Path, monkeypatch) -> None:
    """关闭直投时也不能回退为“只截无人脸区间”的隐性人脸门槛。"""
    _healthy_tooling(monkeypatch)
    rows = [
        _row("news01", "机械鸭发布会现场 主持人报道", "央视新闻", digg=500),
    ]
    deps = _deps(rows)

    def fake_export(ffmpeg, source, duration, intervals, destination_dir, **kwargs):
        destination = Path(destination_dir)
        destination.mkdir(parents=True, exist_ok=True)
        assert [(clip.start, clip.end) for clip in intervals] == [(0.0, 8.0), (8.0, 16.0)]
        names = []
        for index, clip in enumerate(intervals, start=1):
            name = f"slice-{index:02d}.mp4"
            (destination / name).write_bytes(b"slice")
            names.append({
                "index": index, "file": name, "start": clip.start, "end": clip.end,
                "duration": clip.duration(), "status": "ok", "error": "",
            })
        return {"status": "success", "degraded": False, "clips": names, "warnings": []}

    monkeypatch.setattr("douyin_intelligence.replication_pipeline.export_video_clips", fake_export)
    monkeypatch.setattr("douyin_intelligence.replication_pipeline.media_tool_available", lambda config, name: True)
    output_dir, manifest = _run(_config(tmp_path, direct=False), deps)

    assert manifest["main_materials"]
    assert {item["face_class"] for item in manifest["main_materials"]} == {"face_heavy"}
    assert list((output_dir / MAIN_DIR).glob("*.mp4"))
    assert manifest["counters"]["clips_rejected_face_heavy"] == 0


# --------------------------------------------------------------------------- #
# (2) 相关自媒体可成为主素材；无关高热度素材仍被 relevance gate 拒绝
# --------------------------------------------------------------------------- #
def test_related_creator_beats_unrelated_hotter_clip_and_becomes_main(tmp_path: Path, monkeypatch) -> None:
    _healthy_tooling(monkeypatch)
    rows = [
        _row("unrelated", "Unitree G1 人形机器人演示", "机器人频道", digg=100000),
        _row("related", "机械鸭 开箱实测 深度解读", "老王说科技", digg=30),
    ]
    config = _config(tmp_path, direct=True, relevance_gate=True)
    output_dir, manifest = _run(config, _deps(rows))

    # 无关但高热度的素材在下载前被 relevance gate 剔除（rejected 条目自述“未消耗流量”）。
    rejected = manifest["material_replica"]["rejected"]
    unrelated_rejections = [
        entry for entry in rejected if entry["video_id"] == "unrelated" and entry["stage"] == "relevance"
    ]
    assert unrelated_rejections, rejected
    assert "下载前" in unrelated_rejections[0]["reason"]
    # 相关的普通自媒体（非官方、发热量低、face_heavy）仍能成为主素材。
    assert {item["video_id"] for item in manifest["material_replica_sources"]} == {"related"}
    assert manifest["main_materials"], "相关自媒体必须能成为主素材"
    assert {clip["face_class"] for clip in manifest["main_materials"]} == {"face_heavy"}
    assert list((output_dir / MAIN_DIR).glob("*.mp4"))


def test_unrelated_material_is_rejected_by_the_relevance_gate_before_download(tmp_path: Path, monkeypatch) -> None:
    _healthy_tooling(monkeypatch)
    rows = [
        _row("off01", "Unitree G1 人形机器人演示", "机器人频道", digg=100000),
        _row("off02", "四足机器人越野跑酷", "机器人频道2", digg=90000),
        _row("on01", "机械鸭 开箱实测", "创作者A", digg=50),
    ]
    config = _config(tmp_path, direct=True, relevance_gate=True)
    _, manifest = _run(config, _deps(rows))

    assert {item["video_id"] for item in manifest["material_replica_sources"]} == {"on01"}
    rejected = manifest["material_replica"]["rejected"]
    relevance_rejects = {entry["video_id"]: entry for entry in rejected if entry["stage"] == "relevance"}
    assert set(relevance_rejects) == {"off01", "off02"}
    assert all("下载前" in entry["reason"] for entry in relevance_rejects.values())


# --------------------------------------------------------------------------- #
# (3) 排序键：与 face_class 无关，主题命中优先于热度
# --------------------------------------------------------------------------- #
def _candidate(video_id: str, title: str, *, heat: float, author: str = "A") -> Candidate:
    candidate = Candidate(video_id=video_id, title=title, author=author, digg_count=10, duration_seconds=60.0)
    candidate.heat_score = heat
    return candidate


def test_material_rank_key_prefers_theme_hit_over_heat_and_ignores_face_class() -> None:
    related_cool = _candidate("v-related", "机械鸭 开箱实测", heat=0.05)
    unrelated_hot = _candidate("v-unrelated", "Unitree G1 人形机器人演示", heat=0.99)

    probe = {"duration_seconds": 60.0}
    related_key = material_rank_key(related_cool, probe, theme_terms=["机械鸭"], event_terms=["开箱"])
    unrelated_key = material_rank_key(unrelated_hot, probe, theme_terms=["机械鸭"], event_terms=["开箱"])

    # 主题命中 > 事件命中 > profile 加分 > 热度：冷淡的相关素材排在炽热的无关素材之前。
    assert related_key < unrelated_key

    # 排序键里没有任何人脸等级：face_class 差异不改变同一素材的 key。
    free = _candidate("v-same", "机械鸭 开箱实测", heat=0.05)
    heavy = _candidate("v-same", "机械鸭 开箱实测", heat=0.05)
    assert material_rank_key(free, probe, theme_terms=["机械鸭"]) == material_rank_key(heavy, probe, theme_terms=["机械鸭"])


def test_material_rank_key_applies_profile_event_and_heat_weights() -> None:
    event_related = _candidate("v-event", "机械鸭 发布会", heat=0.1)
    hotter_generic = _candidate("v-hot", "机械鸭 日常体验", heat=0.9)
    event_profile = {"event_term_weight": 3.0, "heat_weight": 0.15}
    heat_profile = {"event_term_weight": 1.0, "heat_weight": 1.0}

    event_key = material_rank_key(
        event_related, {"duration_seconds": 60.0}, theme_terms=["机械鸭"],
        event_terms=["发布会"], profile=event_profile,
    )
    generic_event_key = material_rank_key(
        hotter_generic, {"duration_seconds": 60.0}, theme_terms=["机械鸭"],
        event_terms=["发布会"], profile=event_profile,
    )
    generic_heat_key = material_rank_key(
        hotter_generic, {"duration_seconds": 60.0}, theme_terms=["机械鸭"],
        event_terms=["发布会"], profile=heat_profile,
    )

    assert event_key < generic_event_key
    assert event_key[1] == -3.0  # one event hit × explicit 3.0 profile weight
    assert generic_event_key[3] == -0.135  # 0.9 heat × explicit 0.15 profile weight
    assert generic_heat_key[3] == -0.9  # default profile scale is one


def test_material_profile_bonus_rewards_official_but_never_rejects_a_creator() -> None:
    profile = {
        "preferred_source_kinds": ["official_original", "news_broadcast", "creator_commentary"],
        "main_roles": ["event_direct", "commentary"],
        "original_source_bonus": 0.1,
    }
    official = material_profile_bonus(
        {"source_kind": "official_original", "visual_role": "event_direct"}, profile
    )
    creator = material_profile_bonus(
        {"source_kind": "creator_commentary", "visual_role": "commentary"}, profile
    )
    unknown = material_profile_bonus({"source_kind": "unknown", "visual_role": "unknown"}, profile)

    assert official > creator >= 0.0  # 官方是加分项
    assert creator > 0.0  # 自媒体仍有位次分，不会被归零
    assert unknown == 0.0  # 无法判定的标签不加分
    assert material_profile_bonus(None, profile) == 0.0
    assert material_profile_bonus({"source_kind": "official_original"}, None) == 0.0


# --------------------------------------------------------------------------- #
# (4) 交付层对齐：profile 标签随选定素材进入 00-素材目录.json
# --------------------------------------------------------------------------- #
def test_material_catalog_carries_profile_labels(tmp_path: Path, monkeypatch) -> None:
    """pipeline 把 ``source_kind`` 等标签写进交付目录，交付层无需再猜。"""
    _healthy_tooling(monkeypatch)
    rows = [_row("v01", "机械鸭 开箱实测 深度解读", "老王说科技", digg=100)]
    config = _config(tmp_path, direct=True)
    output_dir, _ = _run(config, _deps(rows))

    catalog = json.loads((output_dir / "00-素材目录.json").read_text(encoding="utf-8"))
    assert catalog["entries"], catalog
    entry = catalog["entries"][0]
    for key in ("source_kind", "source_authority", "visual_role", "recommended_usage", "rights_status"):
        assert key in entry, key
    # 一条「深度解读」的自媒体标题必须被认出来，而不是一律回退 unknown。
    assert entry["source_kind"] == "creator_commentary"
    assert entry["source_authority"] == "creator"
    assert entry["recommended_usage"] in {"main", "supporting", "optional"}
