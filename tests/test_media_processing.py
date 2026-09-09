from __future__ import annotations

import json
from pathlib import Path

from douyin_intelligence.config import load_config
from douyin_intelligence.media_processing import CheckpointTranscriber


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
    monkeypatch.setattr(transcriber, "_one", lambda path, offset: calls.append((path.name, offset)) or {"status": "success", "language": "zh", "segments": [{"start": offset, "end": offset + 1, "text": "新片段"}]})
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    result = transcriber.run(video, cache, tmp_path / "temp")
    assert result["status"] == "success"
    assert calls == [("part-0001.wav", 180)]
    assert [row["text"] for row in result["segments"]] == ["已完成", "新片段"]
