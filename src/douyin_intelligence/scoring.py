from __future__ import annotations

import hashlib
import math
import re
import statistics
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from .models import VideoRecord


def _engagement(record: VideoRecord) -> float:
    return float(
        (record.digg_count or 0)
        + (record.comment_count or 0) * 3
        + (record.share_count or 0) * 5
        + (record.collect_count or 0) * 4
        + (record.play_count or 0) * 0.05
    )


def _title_fingerprint(title: str) -> str:
    normalized = re.sub(r"\W+", "", title.casefold())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20] if normalized else ""


def _canonical_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def target_window(target: str | date, timezone_name: str) -> tuple[datetime, datetime]:
    target_date = date.fromisoformat(target) if isinstance(target, str) else target
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(target_date, time.min, tzinfo=zone)
    return start, start + timedelta(days=1)


def score_records(records: Iterable[VideoRecord], target: str | date, config: dict) -> tuple[list[VideoRecord], dict]:
    records = list(records)
    start, end = target_window(target, str(config["timezone"]))
    eligible: list[VideoRecord] = []
    excluded: list[dict[str, str]] = []
    for record in records:
        if not record.title or not record.share_url:
            excluded.append({"video_id": record.video_id, "reason": "缺少标题或作品链接"})
            continue
        if not record.published_at:
            excluded.append({"video_id": record.video_id, "reason": "缺少发布时间，无法验证目标自然日"})
            continue
        published = datetime.fromisoformat(record.published_at)
        if not (start <= published < end):
            excluded.append({"video_id": record.video_id, "reason": "不在目标自然日"})
            continue
        eligible.append(record)

    account_values: dict[str, list[float]] = defaultdict(list)
    for record in eligible:
        account_values[record.account_id].append(_engagement(record))
    account_medians = {
        account_id: statistics.median(values)
        for account_id, values in account_values.items()
        if len(values) >= 3 and statistics.median(values) > 0
    }

    for record in eligible:
        published = datetime.fromisoformat(record.published_at or start.isoformat())
        progress = max(0.0, min(1.0, (published - start).total_seconds() / 86400))
        freshness = 10 + progress * 15
        engagement = _engagement(record)
        interaction = min(40.0, math.log10(1 + engagement) * 8) if engagement > 0 else 0.0
        category_bonus = 15.0 if record.category != "unclassified" else 0.0
        source_bonus = 5.0 if record.source == "douyin_creator" else 3.0 if record.source in {"douyin_search", "douyin_hotboard"} else 0.0
        quality = max(0.0, 5.0 - len(record.missing_fields) * 1.5)
        anomaly = 0.0
        median = account_medians.get(record.account_id)
        if median:
            ratio = engagement / median
            anomaly = min(15.0, max(0.0, math.log2(max(1.0, ratio)) * 6))
        record.score = round(min(100.0, freshness + interaction + category_bonus + source_bonus + quality + anomaly), 3)
        reasons = [
            f"时效 {freshness:.1f}/25",
            f"互动 {interaction:.1f}/40" if engagement else "互动数据缺失或为零，未获得互动分",
            f"题材 {category_bonus:.1f}/15（{record.category}）",
            f"来源 {source_bonus:.1f}/5（{record.source}）",
            f"数据质量 {quality:.1f}/5",
        ]
        if median:
            reasons.append(f"账号内异常热度 {anomaly:.1f}/15（近期样本中位数 {median:.0f}）")
        if record.play_count is None:
            reasons.append("播放量缺失，未将其解释为零价值")
        record.score_reasons = reasons

    return sorted(eligible, key=lambda item: (-item.score, item.video_id)), {
        "input_count": len(records),
        "eligible_count": len(eligible),
        "excluded_count": len(excluded),
        "excluded": excluded,
    }


def deduplicate(records: Iterable[VideoRecord]) -> tuple[list[VideoRecord], list[dict[str, str]]]:
    accepted: list[VideoRecord] = []
    duplicates: list[dict[str, str]] = []
    seen_ids: dict[str, str] = {}
    seen_urls: dict[str, str] = {}
    seen_titles: dict[str, str] = {}
    for record in sorted(records, key=lambda item: (-item.score, item.video_id)):
        canonical_url = _canonical_url(record.share_url)
        title_key = _title_fingerprint(record.title)
        duplicate_of = seen_ids.get(record.video_id) or seen_urls.get(canonical_url) or seen_titles.get(title_key)
        if duplicate_of:
            duplicates.append({"video_id": record.video_id, "duplicate_of": duplicate_of})
            continue
        accepted.append(record)
        seen_ids[record.video_id] = record.video_id
        if canonical_url:
            seen_urls[canonical_url] = record.video_id
        if title_key:
            seen_titles[title_key] = record.video_id
    return accepted, duplicates
