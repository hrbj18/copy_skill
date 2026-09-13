from __future__ import annotations

from pathlib import Path

from douyin_intelligence.material_pipeline import render_material_markdown
from douyin_intelligence.models import VideoRecord


def test_markdown_contains_transcript_and_never_leaks_signed_media_url(tmp_path: Path) -> None:
    record = VideoRecord(
        video_id="123456789", title="这是一条高价值硬件文案", account_id="account", account_name="账号",
        share_url="https://www.douyin.com/video/123456789", published_at="2026-08-26T08:00:00+08:00",
        category="hardware_products", score=88.0, score_reasons=["高互动"],
    )
    raw = {"video_download_url": "https://signed.example/secret-token"}
    text = render_material_markdown(
        {"record": record, "raw": raw},
        {"duration_seconds": 12.3, "width": 1080, "height": 1920, "codec": "h264"},
        {"status": "success", "text": "这个新品值得关注。它采用新的芯片。", "segments": [{"start": 0.0, "end": 2.0, "text": "这个新品值得关注"}]},
        None,
        {"status": "success", "model": "test", "result": {"value_summary": "这是高价值摘要", "core_points": ["这个新品值得关注"], "best_moments": [], "content_angles": [], "claims_to_verify": []}},
        None,
    )
    assert "这个新品值得关注" in text
    assert "事实证据" in text
    assert "signed.example" not in text
