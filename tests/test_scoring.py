from __future__ import annotations

from douyin_intelligence.config import load_config
from douyin_intelligence.models import VideoRecord
from douyin_intelligence.scoring import deduplicate, score_records


def _record(video_id: str, title: str, url: str, published: str | None, score: float = 0) -> VideoRecord:
    return VideoRecord(
        video_id=video_id,
        title=title,
        account_id="a",
        account_name="A",
        share_url=url,
        published_at=published,
        digg_count=100,
        category="ai_models",
        score=score,
    )


def test_scoring_excludes_missing_time_and_outside_target_day() -> None:
    records = [
        _record("1", "有效", "https://www.douyin.com/video/1", "2026-08-25T10:00:00+08:00"),
        _record("2", "无时间", "https://www.douyin.com/video/2", None),
        _record("3", "窗口外", "https://www.douyin.com/video/3", "2026-08-24T23:59:59+08:00"),
    ]
    scored, report = score_records(records, "2026-08-25", load_config())
    assert [item.video_id for item in scored] == ["1"]
    assert report["excluded_count"] == 2
    assert any("播放量缺失" in reason for reason in scored[0].score_reasons)


def test_scoring_is_deterministic_and_uses_account_median() -> None:
    records = [
        _record("1", "一", "https://www.douyin.com/video/1", "2026-08-25T10:00:00+08:00"),
        _record("2", "二", "https://www.douyin.com/video/2", "2026-08-25T10:00:00+08:00"),
        _record("3", "三", "https://www.douyin.com/video/3", "2026-08-25T10:00:00+08:00"),
    ]
    records[2].digg_count = 10_000
    first, _ = score_records(records, "2026-08-25", load_config())
    scores = {item.video_id: item.score for item in first}
    second, _ = score_records(records, "2026-08-25", load_config())
    assert scores == {item.video_id: item.score for item in second}
    assert scores["3"] > scores["1"]
    assert any("账号内异常热度" in reason for reason in next(item for item in first if item.video_id == "3").score_reasons)


def test_deduplicate_by_id_url_and_title_prefers_higher_score() -> None:
    records = [
        _record("1", "同一个标题", "https://www.douyin.com/video/1?from=a", "2026-08-25T10:00:00+08:00", 90),
        _record("1", "不同标题", "https://www.douyin.com/video/1", "2026-08-25T10:00:00+08:00", 80),
        _record("2", "同一个标题", "https://www.douyin.com/video/2", "2026-08-25T10:00:00+08:00", 70),
    ]
    unique, duplicates = deduplicate(records)
    assert [item.video_id for item in unique] == ["1"]
    assert len(duplicates) == 2

