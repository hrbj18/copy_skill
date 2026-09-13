from __future__ import annotations

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


def test_select_material_replicas_applies_face_gate_and_author_dedup(tmp_path: Path, monkeypatch) -> None:
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
    # v1 selected; v2 skipped by author dedup; v3 rejected as face_heavy; v4 selected.
    assert selected_ids == ["v1", "v4"]
    assert result["counters"]["clips_rejected_face_heavy"] == 1
    assert result["counters"]["face_errors"] == 0


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
    assert result["selected"] == []
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
