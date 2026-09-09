from __future__ import annotations

from pathlib import Path

from douyin_intelligence.config import load_config
from douyin_intelligence.material_pipeline import build_materials
from douyin_intelligence.models import VideoRecord


def test_default_pipeline_deletes_media_and_writes_rich_summary(tmp_path: Path, monkeypatch) -> None:
    config = load_config()
    config["materials"]["output_root"] = str(tmp_path / "output")
    config["materials"]["media_root"] = str(tmp_path / "media")
    config["materials"]["cache_root"] = str(tmp_path / "cache")
    config["materials"]["retention"]["temp_root"] = str(tmp_path / "temp")
    record = VideoRecord(
        video_id="123456789", title="高价值视频", account_id="account", account_name="账号",
        share_url="https://www.douyin.com/video/123456789", published_at="2026-08-26T08:00:00+08:00",
        category="hardware_products", score=90, score_reasons=["高互动"],
    )
    monkeypatch.setattr("douyin_intelligence.material_pipeline.select_candidates", lambda source, cfg: ([{"record": record, "raw": {"video_download_url": "signed"}}], []))
    monkeypatch.setattr("douyin_intelligence.material_pipeline.download_video", lambda url, path, cfg: (path.parent.mkdir(parents=True, exist_ok=True), path.write_bytes(b"video-data")))
    monkeypatch.setattr("douyin_intelligence.material_pipeline.probe_video", lambda path: {"duration_seconds": 20, "codec": "h264", "width": 1080, "height": 1920, "size_bytes": path.stat().st_size})
    def fake_transcribe(self, video, cache_dir, work_dir, **kwargs):
        payload = {"status": "success", "segments": [{"start": 1, "end": 2, "text": "重要内容"}], "text": "重要内容"}
        from douyin_intelligence.exporter import atomic_write_json
        atomic_write_json(cache_dir / "transcript.json", payload)
        return payload

    monkeypatch.setattr("douyin_intelligence.material_pipeline.CheckpointTranscriber.run", fake_transcribe)
    monkeypatch.setattr("douyin_intelligence.material_pipeline.OpenAICompatibleAnalyzer.analyze", lambda self, **kwargs: {"status": "success", "model": "test", "result": {"value_summary": "真正有价值的摘要", "core_points": ["核心信息"], "best_moments": [{"timestamp": "00:01", "content": "重要内容", "reason": "关键"}], "content_angles": ["延伸方向"], "claims_to_verify": ["核验数字"]}})
    monkeypatch.setattr("douyin_intelligence.material_pipeline.OpenAICompatibleAnalyzer.status", lambda self: {"enabled": True, "api_key_configured": True, "model": "test"})

    source = tmp_path / "raw-run"
    source.mkdir()
    result = build_materials(source, config)
    assert result["status"] == "success"
    assert result["retention"]["temp_remaining_bytes"] == 0
    assert not list((tmp_path / "media").rglob("*.mp4"))
    summary = (tmp_path / "output" / source.name / "summary.md").read_text(encoding="utf-8")
    assert "真正有价值的摘要" in summary
    assert "00:01" in summary
    assert "核验数字" in summary

    # A second build must be text-only and must not download the media again.
    monkeypatch.setattr("douyin_intelligence.material_pipeline.download_video", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected download")))
    second = build_materials(source, config)
    assert second["status"] == "success"
