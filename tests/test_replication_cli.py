from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from douyin_intelligence import cli
from douyin_intelligence.config import ConfigurationError, load_config
from douyin_intelligence.replication_pipeline import ReplicationDeps, replication_doctor, run_material_replication
from douyin_intelligence.replication_theme import expand_keywords

_EXISTING_COMMANDS = (
    "doctor", "normalize", "run", "export-openmontage", "crawl-plan", "crawl", "browser-start",
    "browser-close", "collect-creators", "build-materials", "collect-materials", "llm-doctor",
    "cleanup-temp", "daily-news", "douyin-tech-ranking", "trusted-account-news", "trusted-ai-brief",
    "account-pool", "material-probe", "visual-anchor", "daily-material-pack", "daily-material-exchange",
    "inspiration", "scheduler", "workbench",
)


def _subcommands() -> set[str]:
    parser = cli.build_parser()
    action = next(item for item in parser._actions if isinstance(item, argparse._SubParsersAction))
    return set(action.choices)


def _row(video_id: str, author: str, *, duration: float = 60.0, digg: int = 100, url: str = "") -> dict:
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
    return row


def _config(tmp_path: Path) -> dict:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    # These CLI tests inject a fake downloader that writes non-media bytes, so
    # isolate them from the download-validation layer (real ffprobe+ffmpeg).
    config["jobs"]["material_replication"]["validation"] = {"enabled": False}
    return config


def _collector(rows: list[dict]):
    def collect(config, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None):
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
        # Stage is identified by ``cache_dir`` (script vs material sub-dir): since
        # P1a both stages share one video cache root, so the source ``.mp4`` no
        # longer distinguishes them.
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


def _deps(tmp_path: Path, rows: list[dict]) -> ReplicationDeps:
    def downloader(url, destination, config):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"video")

    def prober(path, config):
        return {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}

    return ReplicationDeps(
        collector=_collector(rows), downloader=downloader, prober=prober,
        transcriber=_Transcriber(), ocr=_Ocr(), face_detector=_Face(),
    )


def test_config_exposes_valid_material_replication(tmp_path: Path) -> None:
    config = load_config()
    settings = config["jobs"]["material_replication"]
    assert settings["default_pool_size"] == 80
    assert settings["face"]["yunet"]["expected_bytes"] == 232589


def test_config_rejects_inverted_pool_bounds(tmp_path: Path) -> None:
    config = load_config()
    config["jobs"]["material_replication"]["default_pool_size"] = 10
    config["jobs"]["material_replication"]["min_pool_size"] = 90
    bad = tmp_path / "bad_config.json"
    bad.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_config(bad)


def test_cli_registers_material_replication_without_changing_existing() -> None:
    names = _subcommands()
    assert "material-replication" in names
    for command in _EXISTING_COMMANDS:
        assert command in names


def test_cli_material_replication_doctor_and_help_run_offline(capsys) -> None:
    assert "material-replication" in cli.build_parser().format_help()
    assert cli.main(["material-replication", "doctor"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert "face" in output and "ffmpeg" in output


def test_replication_doctor_reports_face_and_ffmpeg(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report = replication_doctor(config)
    assert set(report) >= {"status", "ffmpeg", "ffprobe", "face", "model_present", "asr", "ocr"}


def test_replication_doctor_flags_missing_asr_model(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["materials"]["transcription"]["model_cache"] = str(tmp_path / "empty-models")
    report = replication_doctor(config)
    assert report["asr"] is False
    assert report["asr_model_present"] is False
    assert report["asr_reason"]


def test_replication_doctor_accepts_present_asr_model(tmp_path: Path) -> None:
    config = _config(tmp_path)
    model_dir = tmp_path / "models" / "base"
    model_dir.mkdir(parents=True)
    (model_dir / "model.bin").write_bytes(b"weights")
    config["materials"]["transcription"]["model_cache"] = str(tmp_path / "models")
    report = replication_doctor(config)
    assert report["asr"] is True
    assert report["asr_model_present"] is True
    assert report["asr_reason"] == ""


def test_dry_run_produces_pool_and_scoring_without_download(tmp_path: Path) -> None:
    config = _config(tmp_path)
    rows = [_row("7300000000000000001", "作者A", url="https://signed.example/secret"), _row("7300000000000000002", "作者B")]
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", dry_run=True,
        deps=_deps(tmp_path, rows),
    )
    assert result["status"] == "success"
    assert result["dry_run"] is True
    output_dir = Path(result["output_dir"])
    assert output_dir.name == "9.12苹果折叠屏手机复刻视频"
    process_dir = output_dir / "05-过程数据"
    assert (process_dir / "candidate_pool.json").is_file()
    assert (process_dir / "scoring.json").is_file()
    assert not list(output_dir.rglob("*.mp4"))
    serialized = (process_dir / "candidate_pool.json").read_text(encoding="utf-8")
    assert "signed.example" not in serialized


def test_full_run_offline_degrades_without_ffmpeg(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    rows = [
        _row("7300000000000000001", "作者A", url="https://signed.example/1"),
        _row("7300000000000000002", "作者B", url="https://signed.example/2"),
        _row("7300000000000000003", "作者C", url="https://signed.example/3"),
    ]
    monkeypatch.setattr("douyin_intelligence.replication_pipeline.media_tool_available", lambda config, name: False)
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: __import__("douyin_intelligence.replication_selection", fromlist=["VisualMetrics"]).VisualMetrics(
            sampled_frames=10, motion_frame_ratio=0.9, ocr_text_frame_ratio=0.0, visual_ok=True,
        ),
    )
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", deps=_deps(tmp_path, rows),
    )
    assert result["degraded"] is True
    assert result["status"] == "partial"
    output_dir = Path(result["output_dir"])
    assert (output_dir / "清单.json").is_file()
    assert (output_dir / "01-脚本思路" / "脚本思路.md").is_file()
    assert list((output_dir / "04-原片").glob("*.mp4"))
    validation = json.loads((output_dir / "05-过程数据" / "run_log.json").read_text(encoding="utf-8"))
    assert validation["degraded"] is True
    # manifest self-validation stays green even in the degraded path.
    from douyin_intelligence.replication_delivery import validate_delivery_manifest
    assert validate_delivery_manifest(output_dir / "清单.json")["status"] == "pass"


def test_full_run_marks_degraded_when_face_sampling_fails(tmp_path: Path, monkeypatch) -> None:
    # A healthy static backend status must not hide per-video face failures.
    config = _config(tmp_path)
    rows = [
        _row("7300000000000000001", "作者A", url="https://signed.example/1"),
        _row("7300000000000000002", "作者B", url="https://signed.example/2"),
    ]
    monkeypatch.setattr("douyin_intelligence.replication_pipeline.media_tool_available", lambda config, name: True)
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: __import__("douyin_intelligence.replication_selection", fromlist=["VisualMetrics"]).VisualMetrics(
            sampled_frames=10, motion_frame_ratio=0.9, ocr_text_frame_ratio=0.0, visual_ok=True,
        ),
    )

    class _ErrorFace:
        backend = "opencv_yunet"

        def status(self):
            return {"backend": "opencv_yunet", "status": "ok", "model_present": True}

        def run(self, video, duration, cache_dir, temp_dir):
            return {
                "backend": "opencv_yunet", "status": "error", "face_class": "unavailable",
                "error": "无法读取采样帧", "face_per_frame": [],
            }

    deps = _deps(tmp_path, rows)
    deps.face_detector = _ErrorFace()
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", deps=deps)
    assert result["degraded"] is True
    output_dir = Path(result["output_dir"])
    manifest = json.loads((output_dir / "清单.json").read_text(encoding="utf-8"))
    assert manifest["degraded"] is True
    assert manifest["counters"]["face_errors"] >= 1
    assert any("人脸采样失败" in warning for warning in manifest["warnings"])


def test_script_not_found_is_attributable_in_manifest_and_run_log(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    rows = [
        _row("7300000000000000001", "作者A", url="https://signed.example/1"),
        _row("7300000000000000002", "作者B", url="https://signed.example/2"),
    ]
    monkeypatch.setattr("douyin_intelligence.replication_pipeline.media_tool_available", lambda config, name: False)
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: __import__("douyin_intelligence.replication_selection", fromlist=["VisualMetrics"]).VisualMetrics(
            sampled_frames=10, motion_frame_ratio=0.9, ocr_text_frame_ratio=0.0, visual_ok=True,
        ),
    )

    class _FailingTranscriber:
        def run(self, video, cache_dir, temp_dir, **kwargs):
            return {"status": "error", "text": "", "segments": [], "error": "ASR 引擎不可用"}

    deps = _deps(tmp_path, rows)
    deps.transcriber = _FailingTranscriber()
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", deps=deps)
    output_dir = Path(result["output_dir"])
    manifest = json.loads((output_dir / "清单.json").read_text(encoding="utf-8"))
    script = manifest["script_replica"]
    assert script["status"] == "not_found"
    assert script["unmet_conditions"], "not_found must expose attributable unmet conditions"
    first = script["unmet_conditions"][0]
    assert first["video_id"] and first["stage"] == "speech" and first["reason"]
    assert any("脚本复刻" in warning for warning in manifest["warnings"])
    run_log = json.loads((output_dir / "05-过程数据" / "run_log.json").read_text(encoding="utf-8"))
    assert run_log["script_replica"]["status"] == "not_found"
    assert run_log["script_replica"]["stage"]["asr_attempted"] >= 1
    assert run_log["script_replica"]["unmet_conditions"]


def test_material_not_selected_is_attributable_in_manifest_and_run_log(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    rows = [
        _row("7300000000000000001", "作者A", url="https://signed.example/1"),
        _row("7300000000000000002", "作者B", url="https://signed.example/2"),
    ]
    monkeypatch.setattr("douyin_intelligence.replication_pipeline.media_tool_available", lambda config, name: True)
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: __import__("douyin_intelligence.replication_selection", fromlist=["VisualMetrics"]).VisualMetrics(
            sampled_frames=10, motion_frame_ratio=0.9, ocr_text_frame_ratio=0.0, visual_ok=True,
        ),
    )

    class _VerboseTranscriber:
        def run(self, video, cache_dir, temp_dir, **kwargs):
            # Stage via ``cache_dir`` (see ``_Transcriber``); the source path is
            # shared across stages since P1a.
            if "script" in str(cache_dir):
                return {"status": "success", "text": "字" * 200, "segments": [{"start": 0, "end": 5, "text": "开场"}]}
            # A deliberately extreme 50 chars/sec (3000 chars over the 60s material
            # clip) is rejected for any sane ``max_speech_rate``: this asserts the
            # attribution mechanism, not the shipped ceiling.
            return {"status": "success", "text": "字" * 3000, "segments": []}

    deps = _deps(tmp_path, rows)
    deps.transcriber = _VerboseTranscriber()
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", deps=deps)
    output_dir = Path(result["output_dir"])
    manifest = json.loads((output_dir / "清单.json").read_text(encoding="utf-8"))
    assert manifest["insufficient"] is True
    block = manifest["material_replica"]
    assert block["selected"] == 0
    assert block["conclusion"] == "empty"
    assert block["rejected"], "an empty material set must expose per-video reasons"
    assert {entry["stage"] for entry in block["rejected"]} == {"speech"}
    assert any("未选出素材复刻视频" in warning for warning in manifest["warnings"])
    run_log = json.loads((output_dir / "05-过程数据" / "run_log.json").read_text(encoding="utf-8"))
    assert run_log["material_replica"]["selected"] == 0
    assert run_log["material_replica"]["unmet_conditions"]
    from douyin_intelligence.replication_delivery import validate_delivery_manifest
    assert validate_delivery_manifest(output_dir / "清单.json")["status"] == "pass"


def test_manifest_readme_and_run_log_attribute_keyword_truncation(tmp_path: Path) -> None:
    # The crawler searched only 4 of the 10 expanded keywords.  Every artifact
    # must say so instead of claiming the full coverage.
    config = _config(tmp_path)
    rows = [_row("7300000000000000001", "作者A", url="https://signed.example/1")]
    requested = expand_keywords("苹果折叠屏", config)
    assert len(requested) > 4

    def truncated_collector(config, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None):
        source = Path(str(config.get("_project_root"))) / "raw" / "search_contents_1.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        assert before_sanitize is not None
        before_sanitize([source])
        return {
            "status": "success", "keywords": list(keywords)[:4], "budget": budget,
            "per_keyword_budget": 10, "raw_request_ceiling": 40,
        }

    deps = _deps(tmp_path, rows)
    deps.collector = truncated_collector
    result = run_material_replication(config, "苹果折叠屏", business_date="2026-09-12", dry_run=True, deps=deps)
    output_dir = Path(result["output_dir"])

    manifest = json.loads((output_dir / "清单.json").read_text(encoding="utf-8"))
    assert manifest["keywords_requested"] == requested
    assert manifest["keywords_used"] == requested[:4]
    assert manifest["keywords_truncated"] is True
    for keyword in requested[4:]:
        assert keyword not in manifest["keywords_used"]
    attribution = manifest["search_attribution"]
    assert attribution["search_report_path"] == "05-过程数据/search_report.json"
    assert attribution["keywords_used_count"] == 4
    assert attribution["keywords_requested_count"] == len(requested)
    assert attribution["keywords_truncated"] is True
    assert attribution["min_pool_size"] == config["jobs"]["material_replication"]["min_pool_size"]
    assert any(
        "实际搜索关键词 4 个 / 请求 %d 个" % len(requested) in warning
        and "05-过程数据/search_report.json" in warning
        for warning in manifest["warnings"]
    )

    readme = (output_dir / "00-交付说明.md").read_text(encoding="utf-8")
    assert "关键词覆盖：请求 %d 个 / 实际搜索 4 个（发生关键词截断" % len(requested) in readme
    assert "搜索报告：05-过程数据/search_report.json" in readme

    run_log = json.loads((output_dir / "05-过程数据" / "run_log.json").read_text(encoding="utf-8"))
    assert run_log["keywords_used"] == requested[:4]
    assert run_log["keywords_requested"] == requested
    assert run_log["keywords_truncated"] is True


def test_second_run_requires_overwrite(tmp_path: Path) -> None:
    config = _config(tmp_path)
    rows = [_row("7300000000000000001", "作者A", url="https://signed.example/1")]
    deps = _deps(tmp_path, rows)
    run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", dry_run=True, deps=deps)
    with pytest.raises(FileExistsError):
        run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", dry_run=True, deps=deps)
    # --overwrite replaces atomically instead of half-overwriting.
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", dry_run=True, overwrite=True, deps=deps)
    assert result["status"] == "success"


def test_cli_download_only_flag_defaults_and_parses() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["material-replication", "run", "--theme", "苹果折叠屏手机", "--download-only"])
    assert args.download_only is True
    args_off = parser.parse_args(["material-replication", "run", "--theme", "苹果折叠屏手机"])
    assert args_off.download_only is False


def test_cli_download_only_passes_through_and_rejects_dry_run(monkeypatch, capsys) -> None:
    captured = {}

    def fake_run(config, theme, **kwargs):
        captured["theme"] = theme
        captured.update(kwargs)
        return {"status": "success", "mode": "download_only", "counts": {}, "warnings": []}

    monkeypatch.setattr("douyin_intelligence.replication_pipeline.run_material_replication", fake_run)
    code = cli.main(["material-replication", "run", "--theme", "苹果折叠屏手机", "--download-only"])
    capsys.readouterr()
    assert code == 0
    assert captured["theme"] == "苹果折叠屏手机"
    assert captured["download_only"] is True
    assert captured["dry_run"] is False

    with pytest.raises(SystemExit):
        cli.main(["material-replication", "run", "--theme", "苹果折叠屏手机", "--download-only", "--dry-run"])


def test_cli_download_only_requires_theme() -> None:
    with pytest.raises(SystemExit):
        cli.main(["material-replication", "run", "--download-only"])
