from __future__ import annotations

from douyin_intelligence.models import VideoRecord
from douyin_intelligence.state import prepare_state


def test_state_reports_cross_day_repeat_without_duplicate_target_dates() -> None:
    record = VideoRecord(
        video_id="1",
        title="标题",
        account_id="a",
        account_name="A",
        share_url="https://www.douyin.com/video/1",
        published_at="2026-08-25T10:00:00+08:00",
    )
    first, first_report = prepare_state({"version": "1.0", "videos": {}}, [record], "2026-08-25")
    assert first_report["new_video_count"] == 1
    same, same_report = prepare_state(first, [record], "2026-08-25")
    assert same_report["previously_seen_other_day_count"] == 0
    assert same["videos"]["1"]["target_dates"] == ["2026-08-25"]
    later, later_report = prepare_state(same, [record], "2026-08-26")
    assert later_report["previously_seen_other_day_ids"] == ["1"]
    assert later["videos"]["1"]["target_dates"] == ["2026-08-25", "2026-08-26"]
