from __future__ import annotations

import json
from pathlib import Path

from douyin_intelligence.config import load_config
from douyin_intelligence.media_processing import CheckpointTranscriber, faster_whisper_status


def test_transcription_reuses_completed_part_checkpoint(tmp_path: Path, monkeypatch) -> None:
    config = load_config()
    transcriber = CheckpointTranscriber(config)
    cache = tmp_path / "cache"
    parts = cache / "transcript_parts"
    parts.mkdir(parents=True)
    (parts / "part-0000.json").write_text(json.dumps({"status": "success", "language": "zh", "segments": [{"start": 0, "end": 1, "text": "已完成"}]}), encoding="utf-8")

    def fake_run(command, **kwargs):
        audio_dir = tmp_path / "temp" / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        (audio_dir / "part-0000.wav").write_bytes(b"audio")
        (audio_dir / "part-0001.wav").write_bytes(b"audio")
        return type("Completed", (), {"returncode": 0, "stderr": ""})()

    calls = []
    monkeypatch.setattr("douyin_intelligence.media_processing.subprocess.run", fake_run)
    # This case exercises checkpoint reuse, not model provisioning.
    monkeypatch.setattr(transcriber, "_load", lambda: None)
    monkeypatch.setattr(transcriber, "_one", lambda path, offset: calls.append((path.name, offset)) or {"status": "success", "language": "zh", "segments": [{"start": offset, "end": offset + 1, "text": "新片段"}]})
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    result = transcriber.run(video, cache, tmp_path / "temp")
    assert result["status"] == "success"
    assert calls == [("part-0001.wav", 180)]
    assert [row["text"] for row in result["segments"]] == ["已完成", "新片段"]


def test_checkpoint_transcriber_surfaces_model_load_failure(tmp_path: Path, monkeypatch) -> None:
    # A missing/unreachable model (e.g. HuggingFace 502) must be reported as an
    # explicit model-provisioning error, never as a bare HTTP error string.
    config = load_config()
    config["materials"]["transcription"]["model_cache"] = str(tmp_path / "models")
    monkeypatch.setattr(
        "faster_whisper.WhisperModel",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("502 Bad Gateway")),
        raising=False,
    )
    transcriber = CheckpointTranscriber(config)
    result = transcriber.run(tmp_path / "video.mp4", tmp_path / "cache", tmp_path / "temp")
    assert result["status"] == "error"
    assert result["error_kind"] == "model_unavailable"
    assert "ASR 模型不可用/下载失败" in result["error"]
    assert "502 Bad Gateway" in result["error"]
    assert str(tmp_path / "models") in result["error"]


def test_faster_whisper_status_detects_model_presence(tmp_path: Path) -> None:
    config = load_config()
    config["materials"]["transcription"]["model"] = "base"
    missing_cache = tmp_path / "empty-cache"
    missing_cache.mkdir()
    config["materials"]["transcription"]["model_cache"] = str(missing_cache)
    missing = faster_whisper_status(config)
    assert missing["ready"] is False
    assert missing["model_present"] is False
    assert "未下载" in missing["reason"] or "无可用模型" in missing["reason"]

    present_cache = tmp_path / "models"
    (present_cache / "base").mkdir(parents=True)
    (present_cache / "base" / "model.bin").write_bytes(b"weights")
    config["materials"]["transcription"]["model_cache"] = str(present_cache)
    present = faster_whisper_status(config)
    assert present["ready"] is True
    assert present["model_present"] is True
    assert present["reason"] == ""


def _transcription_config(cache: Path) -> dict:
    """Config whose transcription model lives under ``cache``."""
    config = load_config()
    config["materials"]["transcription"]["model"] = "base"
    config["materials"]["transcription"]["model_cache"] = str(cache)
    return config


def _recording_model(captured: dict):
    class _FakeWhisperModel:
        def __init__(self, model, **kwargs):
            captured["model"] = model
            captured.update(kwargs)

    return _FakeWhisperModel


def test_checkpoint_transcriber_uses_local_files_only_when_model_cached(tmp_path: Path, monkeypatch) -> None:
    # A cached model must be loaded strictly offline so an intercepting proxy
    # (huggingface.co -> 502) can never break an otherwise valid cache hit.
    cache = tmp_path / "models"
    (cache / "base").mkdir(parents=True)
    (cache / "base" / "model.bin").write_bytes(b"weights")
    transcriber = CheckpointTranscriber(_transcription_config(cache))
    captured: dict = {}
    monkeypatch.setattr("faster_whisper.WhisperModel", _recording_model(captured), raising=False)
    transcriber._load()
    assert captured["local_files_only"] is True
    assert captured["download_root"] == str(cache)


def test_checkpoint_transcriber_allows_download_when_model_absent(tmp_path: Path, monkeypatch) -> None:
    # No local weights => keep the ability to download (previous behaviour).
    cache = tmp_path / "empty-models"
    cache.mkdir()
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    transcriber = CheckpointTranscriber(_transcription_config(cache))
    captured: dict = {}
    monkeypatch.setattr("faster_whisper.WhisperModel", _recording_model(captured), raising=False)
    transcriber._load()
    assert captured["local_files_only"] is False


def test_checkpoint_transcriber_honours_hf_hub_offline_env(tmp_path: Path, monkeypatch) -> None:
    # HF_HUB_OFFLINE=1 forces the offline path even when nothing is cached yet.
    cache = tmp_path / "empty-models"
    cache.mkdir()
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    transcriber = CheckpointTranscriber(_transcription_config(cache))
    captured: dict = {}
    monkeypatch.setattr("faster_whisper.WhisperModel", _recording_model(captured), raising=False)
    transcriber._load()
    assert captured["local_files_only"] is True


def test_checkpoint_transcriber_offline_matches_hf_snapshot_layout(tmp_path: Path, monkeypatch) -> None:
    # HuggingFace snapshot layout must also be recognised as a local model.
    cache = tmp_path / "hf"
    snapshot = cache / "models--Systran--faster-whisper-base" / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").write_bytes(b"weights")
    transcriber = CheckpointTranscriber(_transcription_config(cache))
    captured: dict = {}
    monkeypatch.setattr("faster_whisper.WhisperModel", _recording_model(captured), raising=False)
    transcriber._load()
    assert captured["local_files_only"] is True

