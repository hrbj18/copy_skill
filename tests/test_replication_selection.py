from __future__ import annotations

import json
import types
from pathlib import Path

from douyin_intelligence.config import load_config
from douyin_intelligence.face_metrics import FACE_FREE, FACE_HEAVY, FACE_UNAVAILABLE
from douyin_intelligence.replication_candidates import Candidate
from douyin_intelligence.replication_selection import (
    VisualMetrics,
    evaluate_script_transcript,
    material_candidate_pool,
    script_candidate_pool,
    select_material_replicas,
    select_script_replica,
    sort_candidates,
    validate_probe,
)
from douyin_intelligence.sources.base import DownloadTarget, MediaResolutionError


def _candidate(video_id: str, *, digg: int, author: str, duration: float, heat: float | None = None) -> Candidate:
    candidate = Candidate(video_id=video_id, author=author, digg_count=digg, duration_seconds=duration)
    if heat is not None:
        candidate.heat_score = heat
    return candidate


def _config(tmp_path: Path) -> dict:
    """A selection config whose external effects are all faked.

    The download-validation layer runs a real ffprobe+ffmpeg, which the fake
    downloader's non-media bytes cannot satisfy, so it is isolated here (it has
    its own dedicated test module).
    """
    config = load_config()
    config["_project_root"] = str(tmp_path)
    config["jobs"]["material_replication"]["validation"] = {"enabled": False}
    return config


def test_sort_candidates_is_deterministic() -> None:
    rows = [
        _candidate("b", digg=1, author="a", duration=10, heat=0.5),
        _candidate("a", digg=1, author="a", duration=10, heat=0.5),
        _candidate("c", digg=1, author="a", duration=20, heat=0.5),
    ]
    assert [candidate.video_id for candidate in sort_candidates(rows)] == ["c", "a", "b"]


def test_script_candidate_pool_applies_heat_and_duration() -> None:
    config = load_config()
    candidates = [
        _candidate(f"v{i}", digg=100 - i, author="a", duration=60, heat=(100 - i) / 100) for i in range(10)
    ]
    candidates.append(_candidate("short", digg=1, author="a", duration=5, heat=0.01))
    pool = script_candidate_pool(candidates, config)
    assert pool
    assert all(30 <= candidate.duration_seconds <= 300 for candidate in pool)


def test_evaluate_script_transcript_gates() -> None:
    config = load_config()
    assert evaluate_script_transcript({"status": "success", "text": "a" * 200}, 60, config)[0] is True
    assert evaluate_script_transcript({"status": "no_speech", "text": ""}, 60, config)[0] is False
    assert evaluate_script_transcript({"status": "success", "text": "短"}, 60, config)[0] is False
    # 150 chars over 300s => 0.5 cps, below 1.2.
    assert evaluate_script_transcript({"status": "success", "text": "字" * 150}, 300, config)[0] is False


def test_material_candidate_pool_keeps_all_candidates_by_default() -> None:
    """Heat must order candidates, not discard them: whether a clip is usable as footage
    is independent of a post's popularity, and a median cut removes exactly the mid-tier
    creators that publish hands-on footage."""
    config = load_config()
    candidates = [_candidate(str(i), digg=i, author="a", duration=30, heat=i / 10) for i in range(1, 6)]
    pool, median = material_candidate_pool(candidates, config)
    assert median == 0.3
    assert len(pool) == len(candidates)
    scores = [candidate.heat_score for candidate in pool]
    assert scores == sorted(scores, reverse=True)


def test_material_candidate_pool_honours_heat_gate_percentile() -> None:
    """An explicit heat_gate_percentile re-introduces a heat floor when wanted."""
    config = load_config()
    config["jobs"]["material_replication"]["material_replica"]["heat_gate_percentile"] = 0.5
    candidates = [_candidate(str(i), digg=i, author="a", duration=30, heat=i / 10) for i in range(1, 6)]
    pool, median = material_candidate_pool(candidates, config)
    assert median == 0.3
    assert pool, "设置分位门槛后应保留前半部分候选"
    assert all(candidate.heat_score >= median for candidate in pool)


class _FakeRunner:
    def __init__(self, payload: dict, backend: str = FACE_FREE):
        self.payload = payload
        self.backend = backend

    def run(self, *args, **kwargs):
        return self.payload


def test_select_script_replica_picks_first_qualified(tmp_path: Path) -> None:
    config = _config(tmp_path)
    candidates = [_candidate("v1", digg=100, author="a", duration=60, heat=1.0)]
    deps = types.SimpleNamespace(
        downloader=lambda url, dest, cfg: (Path(dest).parent.mkdir(parents=True, exist_ok=True), Path(dest).write_bytes(b"x")),
        prober=lambda path, cfg: {"duration_seconds": 60.0, "width": 1080, "height": 1920},
        transcriber=_FakeRunner({"status": "success", "text": "字" * 200, "segments": []}),
    )
    result = select_script_replica(config, candidates, deps=deps)
    assert result["status"] == "found"
    assert result["candidate"].video_id == "v1"


def test_select_script_replica_reports_structured_unmet_when_asr_fails(tmp_path: Path) -> None:
    config = _config(tmp_path)
    candidates = [_candidate("v1", digg=100, author="a", duration=60, heat=1.0)]
    deps = types.SimpleNamespace(
        downloader=lambda url, dest, cfg: (Path(dest).parent.mkdir(parents=True, exist_ok=True), Path(dest).write_bytes(b"x")),
        prober=lambda path, cfg: {"duration_seconds": 60.0, "width": 1080, "height": 1920},
        transcriber=_FakeRunner({"status": "error", "text": "", "segments": [], "error": "ASR 引擎不可用"}),
    )
    result = select_script_replica(config, candidates, deps=deps)
    assert result["status"] == "not_found"
    assert result["unmet"], "not_found must carry attributable reasons"
    entry = result["unmet"][0]
    assert entry["video_id"] == "v1"
    assert entry["stage"] == "speech"
    assert "ASR 状态 error" in entry["reason"]
    assert result["stage"]["candidate_pool"] == 1
    assert result["stage"]["asr_attempted"] == 1
    assert result["stage"]["conclusion"] == "not_found"


def test_select_script_replica_reports_duration_rejection(tmp_path: Path) -> None:
    config = _config(tmp_path)
    candidates = [_candidate("v1", digg=100, author="a", duration=60, heat=1.0)]
    deps = types.SimpleNamespace(
        downloader=lambda url, dest, cfg: (Path(dest).parent.mkdir(parents=True, exist_ok=True), Path(dest).write_bytes(b"x")),
        prober=lambda path, cfg: {"duration_seconds": 22.0, "width": 1080, "height": 1920},
        transcriber=_FakeRunner({"status": "success", "text": "字" * 200, "segments": []}),
    )
    result = select_script_replica(config, candidates, deps=deps)
    assert result["status"] == "not_found"
    assert result["unmet"][0]["stage"] == "duration"
    assert "22s" in result["unmet"][0]["reason"]
    assert result["stage"]["asr_attempted"] == 0


def test_select_material_replicas_ignores_face_class_but_keeps_it(tmp_path: Path, monkeypatch) -> None:
    """Face class is descriptive metadata, not an admission gate (2026-09-18).

    A themed run may legitimately need footage with people in it (a host, an
    interview, an on-site recording), so a ``face_heavy`` candidate is selected
    like any other while its class still travels with the row.
    """
    config = _config(tmp_path)
    candidates = [
        _candidate("v1", digg=100, author="A", duration=60, heat=0.5),
        _candidate("v2", digg=90, author="A", duration=60, heat=0.5),
        _candidate("v3", digg=80, author="B", duration=60, heat=0.5),
        _candidate("v4", digg=70, author="C", duration=60, heat=0.5),
    ]
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: VisualMetrics(sampled_frames=10, motion_frame_ratio=0.8, ocr_text_frame_ratio=0.1, visual_ok=True),
    )
    face_by_id = {"v1": FACE_FREE, "v2": FACE_FREE, "v3": FACE_HEAVY, "v4": FACE_FREE}

    class _Face:
        backend = "opencv_yunet"

        def run(self, video, duration, cache_dir, temp_dir):
            return {"face_class": face_by_id[Path(video).stem], "face_frame_ratio": 0.0, "max_face_area_ratio": 0.0}

    deps = types.SimpleNamespace(
        downloader=lambda url, dest, cfg: (Path(dest).parent.mkdir(parents=True, exist_ok=True), Path(dest).write_bytes(b"x")),
        prober=lambda path, cfg: {"duration_seconds": 60.0, "width": 1080, "height": 1920},
        ocr=_FakeRunner({"items": [], "sampled_frames": 10}),
        face_detector=_Face(),
        transcriber=_FakeRunner({"status": "no_speech", "text": "", "segments": []}),
    )
    result = select_material_replicas(config, candidates, deps=deps)
    selected_ids = [item["candidate"].video_id for item in result["selected"]]
    # v2 is skipped by author dedup; v3 is face_heavy yet still selected.
    assert selected_ids == ["v1", "v3", "v4"]
    assert result["counters"]["clips_rejected_face_heavy"] == 0
    assert result["counters"]["face_errors"] == 0
    # The class is still recorded on the delivered row (a reader can see it).
    classes = {item["candidate"].video_id: item["face"].get("face_class") for item in result["selected"]}
    assert classes["v3"] == FACE_HEAVY
    assert not any(entry["stage"] == "face" for entry in result["unmet"])


# --------------------------------------------------------------------------- #
# Relevance gate: relevance stops being an ordering key and becomes an admission
# --------------------------------------------------------------------------- #
_THEME = "Microduck 机械鸭机器人"


def _titled(video_id: str, title: str, *, author: str = "A", heat: float = 0.5) -> Candidate:
    candidate = Candidate(video_id=video_id, title=title, author=author, digg_count=10, duration_seconds=60.0)
    candidate.heat_score = heat
    return candidate


def _gate_deps(downloads: list[str]) -> types.SimpleNamespace:
    def downloader(url, dest, cfg):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"x")
        downloads.append(Path(dest).stem)

    return types.SimpleNamespace(
        downloader=downloader,
        prober=lambda path, cfg: {"duration_seconds": 60.0, "width": 1080, "height": 1920},
        ocr=_FakeRunner({"items": [], "sampled_frames": 10}),
        face_detector=_FreeFace(),
        transcriber=_FakeRunner({"status": "no_speech", "text": "", "segments": []}),
    )


def _gate_candidates() -> list[Candidate]:
    """One on-topic clip plus one unrelated clip that is *hotter* than it.

    Heat desc used to be exactly why an unrelated clip got downloaded: the 9.14
    Microduck pool's top 6 were all generic category terms.
    """
    return [
        _titled("unrelated", "Unitree G1 人形机器人演示", author="A", heat=1.0),
        _titled("related", "microduck 机器鸭开箱", author="B", heat=0.5),
    ]


def test_relevance_gate_is_off_without_the_switch_or_the_theme(tmp_path: Path, monkeypatch) -> None:
    """Absent config key / absent ``theme`` ⇒ the pre-gate chain, byte for byte."""
    config = _config(tmp_path)
    # ``_config`` starts from the *shipped* config, which now carries the key:
    # removing it is what makes "absent key" true here.
    config["jobs"]["material_replication"].pop("relevance_gate", None)
    candidates = _gate_candidates()
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: VisualMetrics(sampled_frames=10, motion_frame_ratio=0.8, ocr_text_frame_ratio=0.1, visual_ok=True),
    )

    no_theme = select_material_replicas(config, candidates, deps=_gate_deps([]))
    no_switch = select_material_replicas(config, candidates, deps=_gate_deps([]), theme=_THEME)

    assert no_theme["counters"]["rejected_relevance"] == 0
    assert no_switch["counters"] == no_theme["counters"]
    assert [item["candidate"].video_id for item in no_switch["selected"]] == [
        item["candidate"].video_id for item in no_theme["selected"]
    ]
    assert not any(entry["stage"] == "relevance" for entry in no_switch["unmet"])

    # An explicit enabled=false is the same no-op.
    config["jobs"]["material_replication"]["relevance_gate"] = {"enabled": False}
    disabled = select_material_replicas(config, candidates, deps=_gate_deps([]), theme=_THEME)
    assert disabled["counters"] == no_theme["counters"]
    assert [item["candidate"].video_id for item in disabled["selected"]] == [
        item["candidate"].video_id for item in no_theme["selected"]
    ]


def test_relevance_gate_drops_an_unrelated_candidate_before_the_download(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    config["jobs"]["material_replication"]["relevance_gate"] = {"enabled": True}
    candidates = _gate_candidates()
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: VisualMetrics(sampled_frames=10, motion_frame_ratio=0.8, ocr_text_frame_ratio=0.1, visual_ok=True),
    )
    downloads: list[str] = []

    result = select_material_replicas(config, candidates, deps=_gate_deps(downloads), theme=_THEME)

    assert [item["candidate"].video_id for item in result["selected"]] == ["related"]
    assert result["counters"]["rejected_relevance"] == 1
    assert result["counters"]["downloaded"] == 1
    # The hotter but unrelated clip is refused before any traffic is spent.
    assert downloads == ["related"]
    entry = next(item for item in result["unmet"] if item["stage"] == "relevance")
    assert entry["video_id"] == "unrelated"
    assert entry["subject_hits"] == 0
    assert "Microduck" in entry["reason"] and "未消耗流量" in entry["reason"]


def test_relevance_gate_min_subject_hits_requires_more_than_one_term(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    config["jobs"]["material_replication"]["relevance_gate"] = {"enabled": True, "min_subject_hits": 2}
    candidates = [
        _titled("weak", "Microduck 新玩具上架", author="A", heat=1.0),  # hits 1 subject term
        _titled("strong", "Microduck 机械鸭机器人开箱", author="B", heat=0.5),  # hits 2
    ]
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: VisualMetrics(sampled_frames=10, motion_frame_ratio=0.8, ocr_text_frame_ratio=0.1, visual_ok=True),
    )

    result = select_material_replicas(config, candidates, deps=_gate_deps([]), theme=_THEME)

    assert [item["candidate"].video_id for item in result["selected"]] == ["strong"]
    rejected = next(item for item in result["unmet"] if item["stage"] == "relevance")
    assert rejected["video_id"] == "weak" and rejected["subject_hits"] == 1
    # Default threshold 1 would have admitted it (heat order: weak, strong).
    config["jobs"]["material_replication"]["relevance_gate"] = {"enabled": True}
    lenient = select_material_replicas(config, candidates, deps=_gate_deps([]), theme=_THEME)
    assert [item["candidate"].video_id for item in lenient["selected"]] == ["weak", "strong"]
    assert lenient["counters"]["rejected_relevance"] == 0


def test_relevance_gate_stays_off_when_the_theme_yields_no_subject_term(tmp_path: Path, monkeypatch) -> None:
    """A blank theme has nothing to match, so the gate must not empty the pool.

    The no-op must also be *observable*: a gate the operator switched on and that
    silently judged nothing is the false-negative mode this round is about.
    """
    config = _config(tmp_path)
    config["jobs"]["material_replication"]["relevance_gate"] = {"enabled": True}
    candidates = _gate_candidates()
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: VisualMetrics(sampled_frames=10, motion_frame_ratio=0.8, ocr_text_frame_ratio=0.1, visual_ok=True),
    )

    result = select_material_replicas(config, candidates, deps=_gate_deps([]), theme="   ")

    assert result["counters"]["rejected_relevance"] == 0
    assert len(result["selected"]) == 2
    assert any("未解析出主体词，相关性闸门未生效" in warning for warning in result["warnings"])
    assert result["stage"]["relevance_gate"] == "inactive:no_subject_terms"


def test_relevance_gate_records_that_it_bit_in_the_stage_audit(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    config["jobs"]["material_replication"]["relevance_gate"] = {"enabled": True}
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: VisualMetrics(sampled_frames=10, motion_frame_ratio=0.8, ocr_text_frame_ratio=0.1, visual_ok=True),
    )

    result = select_material_replicas(config, _gate_candidates(), deps=_gate_deps([]), theme=_THEME)

    assert result["stage"]["relevance_gate"] == "active"
    assert result["counters"]["rejected_relevance"] == 1
    # A config without the switch keeps the historic stage payload untouched.
    plain = _config(tmp_path)
    plain["jobs"]["material_replication"].pop("relevance_gate", None)
    without_switch = select_material_replicas(plain, _gate_candidates(), deps=_gate_deps([]), theme=_THEME)
    assert "relevance_gate" not in without_switch["stage"]


def test_test_config_strips_the_shipped_opt_in_switches() -> None:
    """The conftest seam: production turns these switches on; a test fixture must not inherit them.

    Guards the regression where a helper built its config from the live
    ``load_config()`` and silently picked up the newly enabled ``relevance_gate`` /
    ``visual_verify`` / ``material_replica.max_age_days``.  Only the *registered*
    keys may be missing: the rest of the blocks must still be the shipped ones,
    so this also catches a seam that strips too much.

    Keep ``registered`` / ``nested_registered`` / ``nested_blocks`` in sync with
    ``tests/conftest.py::_OPT_IN_MATERIAL_SWITCHES``,
    ``_OPT_IN_MATERIAL_REPLICA_SWITCHES`` and ``_OPT_IN_NESTED_MATERIAL_BLOCKS``
    (``tests`` is not a package, so the tuples cannot be imported).  Note this
    test deliberately does *not* assert what production enables -- it only
    asserts that whatever production ships, the seam removes the registered keys.
    """
    from douyin_intelligence import config as config_module

    registered = (
        "relevance_gate", "visual_verify", "dedup_across_runs", "theme_event_terms", "direct_delivery",
        "sources", "source_duration_windows", "theme_material_profiles", "theme_profile_map",
    )
    nested_registered = ("max_age_days",)
    nested_blocks = ("episode_research_pack",)

    shipped = json.loads(
        (config_module.project_root() / "config" / "content_intelligence.json").read_text(
            encoding="utf-8"
        )
    )
    shipped_mr = shipped["jobs"]["material_replication"]
    # The seam must actually have work to do, or the test is vacuous.
    assert set(registered) & set(shipped_mr)
    assert set(nested_registered) & set(shipped_mr["material_replica"])
    assert all(shipped_mr[name].get("enabled") is True for name in nested_blocks)

    test_mr = load_config()["jobs"]["material_replication"]
    for key in registered:
        assert key not in test_mr
    for key in nested_registered:
        assert key not in test_mr["material_replica"]
    for name in nested_blocks:
        assert test_mr[name]["enabled"] is False

    expected = json.loads(json.dumps(shipped_mr))
    for key in registered:
        expected.pop(key, None)
    for key in nested_registered:
        expected["material_replica"].pop(key, None)
    for name in nested_blocks:
        expected[name]["enabled"] = False
        expected[name].pop("ledger_root", None)
    assert test_mr == expected


def test_select_material_replicas_surfaces_per_video_face_errors(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    candidates = [_candidate("v1", digg=100, author="A", duration=60, heat=0.5)]
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: VisualMetrics(sampled_frames=10, motion_frame_ratio=0.8, ocr_text_frame_ratio=0.1, visual_ok=True),
    )

    class _ErrorFace:
        backend = "opencv_yunet"

        def run(self, video, duration, cache_dir, temp_dir):
            # A failed frame read must not masquerade as "no face".
            return {"status": "error", "face_class": FACE_UNAVAILABLE, "error": "无法读取采样帧", "face_per_frame": []}

    deps = types.SimpleNamespace(
        downloader=lambda url, dest, cfg: (Path(dest).parent.mkdir(parents=True, exist_ok=True), Path(dest).write_bytes(b"x")),
        prober=lambda path, cfg: {"duration_seconds": 60.0, "width": 1080, "height": 1920},
        ocr=_FakeRunner({"items": [], "sampled_frames": 10}),
        face_detector=_ErrorFace(),
        transcriber=_FakeRunner({"status": "no_speech", "text": "", "segments": []}),
    )
    result = select_material_replicas(config, candidates, deps=deps)
    assert result["counters"]["face_errors"] == 1
    assert result["counters"]["face_checked"] == 1
    # A failed sample read is recorded (counter + warning) but no longer refuses
    # the candidate: face class is descriptive metadata (2026-09-18), so an
    # otherwise on-topic clip still ships with ``FACE_UNAVAILABLE`` on its row.
    assert [item["candidate"].video_id for item in result["selected"]] == ["v1"]
    assert result["selected"][0]["face"]["face_class"] == FACE_UNAVAILABLE
    assert any("人脸采样失败" in warning for warning in result["warnings"])


def test_validate_probe_flags_missing_video_stream() -> None:
    assert validate_probe({"duration_seconds": 60.0, "width": 1080, "height": 1920})[0] is True
    ok, reason = validate_probe({"duration_seconds": 60.0, "width": None, "height": None})
    assert ok is False and "无视频流" in reason
    assert validate_probe({})[0] is False
    assert validate_probe({"duration_seconds": 0, "width": 1080, "height": 1920})[0] is False


def test_select_material_replicas_classifies_invalid_media(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    candidates = [
        _candidate("v1", digg=100, author="A", duration=60, heat=0.5),
        _candidate("v2", digg=90, author="B", duration=60, heat=0.5),
    ]
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: VisualMetrics(sampled_frames=10, motion_frame_ratio=0.8, ocr_text_frame_ratio=0.1, visual_ok=True),
    )
    # v1 downloads to an audio-only/error payload (no video stream); v2 is valid.
    probes = {
        "v1": {"duration_seconds": 60.0, "width": None, "height": None},
        "v2": {"duration_seconds": 60.0, "width": 1080, "height": 1920},
    }

    class _Face:
        backend = "opencv_yunet"

        def run(self, video, duration, cache_dir, temp_dir):
            return {"status": "ok", "face_class": FACE_FREE, "face_frame_ratio": 0.0, "max_face_area_ratio": 0.0}

    deps = types.SimpleNamespace(
        downloader=lambda url, dest, cfg: (Path(dest).parent.mkdir(parents=True, exist_ok=True), Path(dest).write_bytes(b"x")),
        prober=lambda path, cfg: probes[Path(path).stem],
        ocr=_FakeRunner({"items": [], "sampled_frames": 10}),
        face_detector=_Face(),
        transcriber=_FakeRunner({"status": "no_speech", "text": "", "segments": []}),
    )
    result = select_material_replicas(config, candidates, deps=deps)
    assert result["counters"]["invalid_media"] == 1
    assert result["counters"]["face_errors"] == 0
    assert [item["candidate"].video_id for item in result["selected"]] == ["v2"]
    assert any("媒体无效" in warning for warning in result["warnings"])


class _FreeFace:
    backend = "opencv_yunet"

    def run(self, video, duration, cache_dir, temp_dir):
        return {"status": "ok", "face_class": FACE_FREE, "face_frame_ratio": 0.0, "max_face_area_ratio": 0.0}


def _material_deps(*, transcript: dict, probes: dict | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        downloader=lambda url, dest, cfg: (Path(dest).parent.mkdir(parents=True, exist_ok=True), Path(dest).write_bytes(b"x")),
        prober=lambda path, cfg: (probes or {}).get(Path(path).stem, {"duration_seconds": 60.0, "width": 1080, "height": 1920}),
        ocr=_FakeRunner({"items": [], "sampled_frames": 10}),
        face_detector=_FreeFace(),
        transcriber=_FakeRunner(transcript),
    )


def test_select_material_replicas_reports_visual_rejection(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    candidates = [_candidate("v1", digg=100, author="A", duration=60, heat=0.5)]
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: VisualMetrics(sampled_frames=10, motion_frame_ratio=0.05, ocr_text_frame_ratio=0.9, visual_ok=False),
    )
    deps = _material_deps(transcript={"status": "no_speech", "text": "", "segments": []})
    result = select_material_replicas(config, candidates, deps=deps)
    assert result["counters"]["rejected_visual"] == 1
    entry = next(item for item in result["unmet"] if item["stage"] == "visual")
    assert entry["video_id"] == "v1"
    assert "画面代理不达标" in entry["reason"]
    assert entry["motion_frame_ratio"] == 0.05 and entry["ocr_text_frame_ratio"] == 0.9
    assert result["insufficient"] is True
    assert any("未选出素材复刻视频" in warning for warning in result["warnings"])


def test_select_material_replicas_reports_speech_rejection(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    candidates = [_candidate("v1", digg=100, author="A", duration=40, heat=0.5)]
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: VisualMetrics(sampled_frames=10, motion_frame_ratio=0.8, ocr_text_frame_ratio=0.1, visual_ok=True),
    )
    # Verify the *mechanism* (message-density gate rejects + attributes to
    # ``speech``), not a specific factory threshold.  The fake prober reports a
    # 60s clip; 3000 chars over it => 50 chars/sec, rejected for any sane
    # ``max_speech_rate``, so the test stays green when the shipped ceiling is
    # retuned (e.g. 1.2 -> 8.0).
    deps = _material_deps(transcript={"status": "success", "text": "字" * 3000, "segments": []})
    result = select_material_replicas(config, candidates, deps=deps)
    assert result["counters"]["rejected_speech"] == 1
    entry = next(item for item in result["unmet"] if item["stage"] == "speech")
    assert entry["video_id"] == "v1"
    assert entry["chars"] == 3000
    assert entry["speech_rate"] == 50.0
    assert "口播密度" in entry["reason"]


def test_select_material_replicas_reports_author_duplicate(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    candidates = [
        _candidate("v1", digg=100, author="A", duration=60, heat=0.5),
        _candidate("v2", digg=90, author="A", duration=60, heat=0.5),
        _candidate("v3", digg=80, author="A", duration=60, heat=0.5),
    ]
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: VisualMetrics(sampled_frames=10, motion_frame_ratio=0.8, ocr_text_frame_ratio=0.1, visual_ok=True),
    )
    deps = _material_deps(transcript={"status": "no_speech", "text": "", "segments": []})
    result = select_material_replicas(config, candidates, deps=deps)
    assert result["counters"]["rejected_author_duplicate"] == 2
    duplicates = [item for item in result["unmet"] if item["stage"] == "author_duplicate"]
    assert {item["video_id"] for item in duplicates} == {"v2", "v3"}
    assert [item["candidate"].video_id for item in result["selected"]] == ["v1"]
    # 1 selected < min 2 => the shortfall must be explained in a readable summary.
    assert any("不足最小 2 条" in warning for warning in result["warnings"])


def test_select_material_replicas_reports_pool_rejection(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    # An explicit heat floor is the only way a candidate is dropped at the pool stage.
    config["jobs"]["material_replication"]["material_replica"]["heat_gate_percentile"] = 0.5
    candidates = [
        _candidate("hot", digg=100, author="A", duration=60, heat=0.9),
        _candidate("mid", digg=90, author="B", duration=60, heat=0.5),
        _candidate("cold", digg=1, author="C", duration=60, heat=0.1),
    ]
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: VisualMetrics(sampled_frames=10, motion_frame_ratio=0.8, ocr_text_frame_ratio=0.1, visual_ok=True),
    )
    deps = _material_deps(transcript={"status": "no_speech", "text": "", "segments": []})
    result = select_material_replicas(config, candidates, deps=deps)
    assert result["counters"]["rejected_pool"] == 1
    entry = next(item for item in result["unmet"] if item["stage"] == "pool")
    assert entry["video_id"] == "cold"
    assert "中位数" in entry["reason"]


def test_is_video_candidate_flags_image_album_and_audio_media() -> None:
    """Douyin image-album posts (aweme_type=68) have no video stream; their download URL
    is the post's background music, so they must be rejected before download."""
    from douyin_intelligence.replication_candidates import Candidate
    from douyin_intelligence.replication_selection import is_video_candidate

    album = Candidate(video_id="a", aweme_type="68", media_is_audio=True)
    usable, reason = is_video_candidate(album)
    assert usable is False
    assert "图文作品" in reason

    audio_only = Candidate(video_id="b", aweme_type="0", media_is_audio=True)
    usable, reason = is_video_candidate(audio_only)
    assert usable is False
    assert "音频" in reason

    normal = Candidate(video_id="c", aweme_type="0", media_is_audio=False)
    assert is_video_candidate(normal) == (True, "")


def test_select_material_replicas_skips_non_video_before_download(tmp_path: Path, monkeypatch) -> None:
    from douyin_intelligence.replication_candidates import Candidate

    config = _config(tmp_path)
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: VisualMetrics(sampled_frames=10, motion_frame_ratio=0.8, ocr_text_frame_ratio=0.1, visual_ok=True),
    )
    candidates = [
        Candidate(video_id="album-1", author="A", aweme_type="68", media_is_audio=True, duration_seconds=60, digg_count=100, heat_score=1.0, heat_rank=1),
        Candidate(video_id="video-1", author="B", aweme_type="0", media_is_audio=False, duration_seconds=60, digg_count=90, heat_score=0.9, heat_rank=2),
    ]
    deps = _material_deps(transcript={"status": "no_speech", "text": "", "segments": []})
    inner_downloader = deps.downloader
    downloaded: list[str] = []

    def _recording_downloader(url, dest, cfg):
        downloaded.append(Path(dest).stem)
        return inner_downloader(url, dest, cfg)

    deps.downloader = _recording_downloader
    result = select_material_replicas(config, candidates, deps=deps)

    not_video = [item for item in result["unmet"] if item["stage"] == "not_video"]
    assert len(not_video) == 1
    assert not_video[0]["video_id"] == "album-1"
    assert "图文作品" in not_video[0]["reason"]
    assert result["counters"]["rejected_not_video"] == 1
    # The image album must never be downloaded.
    assert "album-1" not in downloaded


# --------------------------------------------------------------------------- #
# Download dispatch: the injected multi-source resolver (``resolver=``)
#
# The legacy path (``resolver=None``) is exercised by every test above.  These
# pin the new path: a candidate is resolved through its *own* source adapter and
# the downloader receives that source's Referer; a miss is a plain "no address"
# and a source-level failure is recorded on its own ``resolve`` stage.
# --------------------------------------------------------------------------- #
class _StubMediaResolver:
    """A :class:`~douyin_intelligence.sources.base.MediaResolver` double."""

    def __init__(self, target: DownloadTarget | None = None, *, raises: Exception | None = None) -> None:
        self._target = target
        self._raises = raises
        self.calls: list[str] = []

    def resolve_target(self, candidate: Candidate) -> DownloadTarget | None:
        self.calls.append(candidate.video_id)
        if self._raises is not None:
            raise self._raises
        return self._target


def _capturing_deps(captured: list[dict]) -> types.SimpleNamespace:
    """``_material_deps`` variant whose downloader records url + referer."""

    def downloader(url, dest, cfg, *, max_bytes=None, referer=None):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"x")
        captured.append({"url": url, "referer": referer})

    return types.SimpleNamespace(
        downloader=downloader,
        prober=lambda path, cfg: {"duration_seconds": 60.0, "width": 1080, "height": 1920},
        ocr=_FakeRunner({"items": [], "sampled_frames": 10}),
        face_detector=_FreeFace(),
        transcriber=_FakeRunner({"status": "no_speech", "text": "", "segments": []}),
    )


def _bilibili_candidate(video_id: str = "bv-1") -> Candidate:
    candidate = _candidate(video_id, digg=100, author="A", duration=60, heat=1.0)
    candidate.source = "bilibili"
    return candidate


def test_select_material_replicas_downloads_via_the_injected_resolver(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: VisualMetrics(sampled_frames=10, motion_frame_ratio=0.8, ocr_text_frame_ratio=0.1, visual_ok=True),
    )
    candidate = _bilibili_candidate()
    captured: list[dict] = []
    resolver = _StubMediaResolver(
        DownloadTarget(url="https://cdn.example/bv", referer="https://www.bilibili.com/"),
    )

    select_material_replicas(config, [candidate], deps=_capturing_deps(captured), resolver=resolver)

    # The candidate was resolved through the resolver (not the legacy map), and
    # the downloader received the source's own Referer.
    assert resolver.calls == ["bv-1"]
    assert captured and captured[0]["url"] == "https://cdn.example/bv"
    assert captured[0]["referer"] == "https://www.bilibili.com/"


def test_select_material_replicas_resolver_miss_is_a_plain_no_media_url(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: VisualMetrics(sampled_frames=10, motion_frame_ratio=0.8, ocr_text_frame_ratio=0.1, visual_ok=True),
    )
    captured: list[dict] = []
    result = select_material_replicas(
        config, [_bilibili_candidate()], deps=_capturing_deps(captured),
        resolver=_StubMediaResolver(None),  # no usable address
    )

    assert captured == []  # never reached the downloader
    assert any(item["stage"] == "no_media_url" for item in result["unmet"])
    assert not any(item["stage"] == "resolve" for item in result["errors"])


def test_select_material_replicas_source_failure_has_its_own_stage(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: VisualMetrics(sampled_frames=10, motion_frame_ratio=0.8, ocr_text_frame_ratio=0.1, visual_ok=True),
    )
    captured: list[dict] = []
    result = select_material_replicas(
        config, [_bilibili_candidate()], deps=_capturing_deps(captured),
        resolver=_StubMediaResolver(raises=MediaResolutionError("b站接口 503")),
    )

    assert captured == []
    assert any(item["stage"] == "resolve" for item in result["errors"])
    # A source-level failure must NOT be flattened into a "no address" miss.
    assert not any(item["stage"] == "no_media_url" for item in result["unmet"])


def test_select_script_replica_downloads_via_the_injected_resolver(tmp_path: Path) -> None:
    config = _config(tmp_path)
    captured: list[dict] = []

    def downloader(url, dest, cfg, *, max_bytes=None, referer=None):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"x")
        captured.append({"url": url, "referer": referer})

    deps = types.SimpleNamespace(
        downloader=downloader,
        prober=lambda path, cfg: {"duration_seconds": 60.0, "width": 1080, "height": 1920},
        transcriber=_FakeRunner({"status": "success", "text": "字" * 200, "segments": []}),
    )
    resolver = _StubMediaResolver(
        DownloadTarget(url="https://cdn.example/bv", referer="https://www.bilibili.com/"),
    )

    result = select_script_replica(config, [_bilibili_candidate()], deps=deps, resolver=resolver)

    assert result["status"] == "found"
    assert resolver.calls == ["bv-1"]
    assert captured and captured[0]["referer"] == "https://www.bilibili.com/"
