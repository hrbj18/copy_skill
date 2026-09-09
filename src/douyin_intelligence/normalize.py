from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .models import VideoRecord


VIDEO_ID_RE = re.compile(r"(?:/video/|modal_id=)(\d{8,})")
COUNT_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*([万亿kKmM]?)$")


def _deep_get(item: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        node: Any = item
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if node not in (None, ""):
            return node
    return None


def parse_count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip().replace(",", "").replace("+", "")
    if not text or text.lower() in {"none", "null", "nan", "-"}:
        return None
    match = COUNT_RE.match(text)
    if not match:
        return None
    number = float(match.group(1))
    multiplier = {"": 1, "万": 10_000, "亿": 100_000_000, "k": 1_000, "K": 1_000, "m": 1_000_000, "M": 1_000_000}[match.group(2)]
    return max(0, int(number * multiplier))


def parse_datetime(value: Any, timezone_name: str) -> str | None:
    if value in (None, ""):
        return None
    zone = ZoneInfo(timezone_name)
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        timestamp = int(float(value))
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(zone).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone).isoformat(timespec="seconds")


def infer_source(path: Path, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    name = path.name.lower()
    if "hot" in name or "board" in name:
        return "douyin_hotboard"
    if "search" in name:
        return "douyin_search"
    return "douyin_creator"


def _iter_json_payload(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item
        return
    if not isinstance(payload, dict):
        return
    for key in ("videos", "items", "aweme_list", "contents", "data", "result"):
        nested = payload.get(key)
        if isinstance(nested, (list, dict)):
            yield from _iter_json_payload(nested)
            return
    yield payload


def load_raw_records(path: str | Path) -> list[dict[str, Any]]:
    source_path = Path(path)
    if source_path.suffix.lower() == ".jsonl":
        rows: list[dict[str, Any]] = []
        for number, line in enumerate(source_path.read_text(encoding="utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{source_path} 第 {number} 行不是有效 JSON") from exc
            rows.extend(_iter_json_payload(payload))
        return rows
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source_path} 不是有效 JSON") from exc
    return list(_iter_json_payload(payload))


def categorize(title: str, source_keyword: str, categories: dict[str, Any]) -> str:
    corpus = f"{title} {source_keyword}".casefold()
    priority = {
        "daily_news": 0,
        "interesting_tech": 1,
        "ai_models": 2,
        "software_tools": 3,
        "hardware_products": 4,
        "open_source": 5,
    }
    best = (0, -1, "unclassified")
    for category, keywords in categories.items():
        if not isinstance(keywords, list):
            continue
        hits = sum(1 for keyword in keywords if str(keyword).casefold() in corpus)
        candidate = (hits, priority.get(str(category), 0), str(category))
        if candidate > best:
            best = candidate
    return best[2]


def normalize_record(
    item: dict[str, Any],
    *,
    source: str,
    timezone_name: str,
    categories: dict[str, Any],
    raw_file: str = "",
) -> VideoRecord:
    source = str(item.get("source") or source).strip()
    title = str(_deep_get(item, "title", "desc", "caption", "content", "aweme_info.desc") or "").strip()
    url = str(_deep_get(item, "aweme_url", "share_url", "url", "detail_url") or "").strip()
    raw_id = _deep_get(item, "aweme_id", "video_id", "item_id", "id", "aweme_info.aweme_id")
    video_id = str(raw_id or "").strip()
    if not video_id and url:
        match = VIDEO_ID_RE.search(url)
        video_id = match.group(1) if match else ""
    account_id = str(_deep_get(item, "account_id", "creator_hash", "sec_uid", "sec_user_id", "author.uid", "author.sec_uid") or "unknown").strip()
    account_name = str(_deep_get(item, "account_name", "nickname", "author.nickname", "author.unique_id") or account_id).strip()
    published_at = parse_datetime(_deep_get(item, "published_at", "create_time", "publish_time", "aweme_info.create_time"), timezone_name)
    source_keyword = str(_deep_get(item, "source_keyword", "keyword", "search_keyword") or "").strip()
    derived = False
    if not video_id:
        material = "|".join((url, title, account_id, published_at or ""))
        video_id = "derived-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
        derived = True
    if not url and video_id.isdigit():
        url = f"https://www.douyin.com/video/{video_id}"
    missing: list[str] = []
    for field_name, value in (("title", title), ("share_url", url), ("published_at", published_at)):
        if not value:
            missing.append(field_name)
    if derived:
        missing.append("stable_video_id")
    metrics = {
        "play_count": parse_count(_deep_get(item, "play_count", "statistics.play_count", "statistics.play_count_text")),
        "digg_count": parse_count(_deep_get(item, "digg_count", "liked_count", "like_count", "statistics.digg_count")),
        "comment_count": parse_count(_deep_get(item, "comment_count", "statistics.comment_count")),
        "share_count": parse_count(_deep_get(item, "share_count", "statistics.share_count")),
        "collect_count": parse_count(_deep_get(item, "collect_count", "collected_count", "statistics.collect_count")),
    }
    category = str(item.get("category") or "").strip() or categorize(title, source_keyword, categories)
    return VideoRecord(
        video_id=video_id,
        title=title,
        account_id=account_id,
        account_name=account_name,
        share_url=url,
        published_at=published_at,
        category=category,
        source=source,
        source_keyword=source_keyword,
        missing_fields=missing,
        id_derived=derived,
        raw_file=raw_file,
        **metrics,
    )


def normalize_files(paths: Iterable[str | Path], config: dict[str, Any], source: str | None = None) -> list[VideoRecord]:
    records: list[VideoRecord] = []
    for value in paths:
        path = Path(value)
        inferred = infer_source(path, source)
        account_hint = next(
            (
                item
                for item in config.get("benchmark_accounts") or []
                if isinstance(item, dict)
                and str(item.get("id") or "").strip()
                and str(item["id"]).casefold() in str(path).casefold()
            ),
            None,
        )
        for item in load_raw_records(path):
            record = normalize_record(
                item,
                source=inferred,
                timezone_name=str(config["timezone"]),
                categories=config["categories"],
                raw_file=str(path.resolve()),
            )
            if inferred == "douyin_creator" and account_hint:
                record.account_id = str(account_hint["id"])
                record.account_name = str(account_hint.get("name") or account_hint["id"])
                if record.category == "unclassified" and account_hint.get("category"):
                    record.category = str(account_hint["category"])
            records.append(record)
    return records
