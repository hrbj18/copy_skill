"""Bounded, date-aware public-news snippets for sparse Douyin hotspot details."""

from __future__ import annotations

import html
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, time as clock_time, timedelta
from email.utils import parsedate_to_datetime
from hashlib import sha256
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx


_SPACE = re.compile(r"\s+")
_HTML = re.compile(r"<[^>]+>")
_NOISE = re.compile(r"#[^#\s]+|[【】]|(?:点赞|关注|福利|必看|震惊|来了|爆了|热议)")
_GENERIC_QUERY_TERMS = {
    "科技", "新闻", "内容", "消息", "最新", "相关", "功能", "项目", "产品", "模型", "开源", "发布", "更新", "实测", "视频", "这个", "那个",
}
_CHINESE_ANCHOR_SUFFIXES = ("模型", "上下文", "项目", "方案", "系统", "产品", "硬件", "机器人", "出租车", "智能体", "空间", "长剧", "网盘")
_KNOWN_BRAND_ANCHORS = ("苹果", "腾讯", "高德", "特斯拉", "DeepSeek", "MiniMax", "OpenClaw", "Cybercab", "Microduck", "ESP32", "三星", "清华", "GitHub")


def _clean(value: Any, maximum: int = 1_000) -> str:
    text = _HTML.sub(" ", html.unescape(str(value or "")))
    return _SPACE.sub(" ", text).strip()[:maximum]


def detail_query(story: dict[str, Any]) -> str:
    slots = story.get("event_slots") if isinstance(story.get("event_slots"), dict) else {}
    pieces = [_clean(slots.get(key), 80) for key in ("subject", "object", "action")]
    query = " ".join(piece for piece in pieces if piece)
    if len(query) < 6:
        query = _NOISE.sub(" ", _clean(story.get("canonical_title") or story.get("title"), 120))
    return _SPACE.sub(" ", query).strip()[:120]


def _query_anchors(query: str) -> list[str]:
    english = re.findall(r"(?:\d+(?:\.\d+)?[A-Za-z][A-Za-z0-9._-]*|[A-Za-z][A-Za-z0-9._-]{2,})", query)
    chinese = re.findall(r"[\u4e00-\u9fff]{2,}", query)
    rows: list[str] = []
    for item in english + [anchor for anchor in _KNOWN_BRAND_ANCHORS if anchor.casefold() in query.casefold()]:
        cleaned = _clean(item, 40)
        if cleaned and cleaned.casefold() not in _GENERIC_QUERY_TERMS and cleaned not in rows:
            rows.append(cleaned)
    for item in chinese:
        cleaned = _clean(item, 40)
        if len(cleaned) <= 8 and cleaned not in _GENERIC_QUERY_TERMS and cleaned not in rows:
            rows.append(cleaned)
        for suffix in _CHINESE_ANCHOR_SUFFIXES:
            start = cleaned.find(suffix)
            while start >= 0:
                for width in (0, 2, 4, 6):
                    candidate = cleaned[max(0, start - width):start + len(suffix)]
                    if len(candidate) >= 3 and candidate not in _GENERIC_QUERY_TERMS and candidate not in rows:
                        rows.append(candidate)
                start = cleaned.find(suffix, start + len(suffix))
    return sorted(rows, key=len, reverse=True)[:6]


def _relevant(query: str, title: str, summary: str) -> bool:
    corpus = (title + " " + summary).casefold()
    return any(anchor.casefold() in corpus for anchor in _query_anchors(query))


def _within_business_window(value: str, business_date: str, timezone: str, grace_hours: int) -> bool:
    """Accept the business day plus a bounded RSS index-ingestion grace period."""
    try:
        published = parsedate_to_datetime(value)
        if published.tzinfo is None:
            return False
        zone = ZoneInfo(timezone)
        target_day = date.fromisoformat(business_date)
        start = datetime.combine(target_day, clock_time.min, tzinfo=zone)
        end = start + timedelta(days=1, hours=grace_hours)
        return start <= published.astimezone(zone) < end
    except (TypeError, ValueError, OverflowError):
        return False


class PublicDetailDiscovery:
    """Fetch bounded Google News RSS snippets without treating them as proof.

    Douyin remains the heat source. Google News is a second, date-aware
    discovery channel; index links remain in the audit and only matching,
    fresh title-plus-summary text reaches semantic extraction.
    """

    def __init__(self, settings: dict[str, Any], *, deadline: float, business_date: str, timezone: str):
        self.settings = settings
        self.deadline = deadline
        self.business_date = business_date
        self.timezone = timezone
        self.queries = 0
        self.downloaded_bytes = 0
        self.errors: list[str] = []
        self.outcomes: list[dict[str, Any]] = []

    def __call__(self, story: dict[str, Any]) -> dict[str, Any]:
        query = detail_query(story)
        story_id = _clean(story.get("story_id") or story.get("event_id"), 100)
        outcome: dict[str, Any] = {
            "story_id": story_id,
            "query": query,
            "provider": "google_news_rss",
            "status": "skipped",
            "results": [],
            "discarded": [],
        }
        if not query:
            outcome["reason"] = "empty_query"
        elif self.queries >= int(self.settings["max_queries"]):
            outcome["reason"] = "query_budget_exhausted"
        elif self.downloaded_bytes >= int(self.settings["max_total_bytes"]):
            outcome["reason"] = "download_budget_exhausted"
        elif time.monotonic() >= self.deadline:
            outcome["reason"] = "deadline_exhausted"
        else:
            self.queries += 1
            freshness = int(self.settings.get("freshness_days") or 3)
            url = (
                "https://news.google.com/rss/search?q="
                + quote(query + f" when:{freshness}d")
                + "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
            )
            try:
                timeout = min(float(self.settings["request_timeout_seconds"]), max(1.0, self.deadline - time.monotonic()))
                with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False) as client:
                    response = client.get(url, headers={"Accept": "application/xml"})
                    response.raise_for_status()
                    maximum = min(int(self.settings["max_response_bytes"]), max(0, int(self.settings["max_total_bytes"]) - self.downloaded_bytes))
                    body = response.content[:maximum]
                self.downloaded_bytes += len(body)
                root = ET.fromstring(body)
                limit = int(self.settings["max_results_per_query"])
                for item in root.findall("./channel/item"):
                    title = _clean(item.findtext("title"), 240)
                    summary = _clean(item.findtext("description"), 900)
                    link = _clean(item.findtext("link"), 1_000)
                    published_at = _clean(item.findtext("pubDate"), 80)
                    source = item.find("source")
                    source_name = _clean(source.text if source is not None else "", 100)
                    source_url = _clean(source.attrib.get("url") if source is not None else "", 300)
                    audit_row = {
                        "title": title,
                        "summary": summary,
                        "index_url": link,
                        "published_at": published_at,
                        "source_name": source_name,
                        "source_url": source_url,
                    }
                    if not _within_business_window(published_at, self.business_date, self.timezone, int(self.settings["freshness_grace_hours"])):
                        outcome["discarded"].append({"reason": "outside_business_window", **audit_row})
                    elif len(_clean(title + " " + summary, 1_200)) < int(self.settings["min_detail_chars"]):
                        outcome["discarded"].append({"reason": "summary_too_short", **audit_row})
                    elif not _relevant(query, title, summary):
                        outcome["discarded"].append({"reason": "query_not_supported", **audit_row})
                    elif len(outcome["results"]) < limit:
                        outcome["results"].append(audit_row)
                outcome["status"] = "success" if outcome["results"] else "empty"
            except Exception as exc:
                outcome["status"] = "failed"
                outcome["reason"] = type(exc).__name__
                self.errors.append(type(exc).__name__)
        self.outcomes.append(outcome)
        evidence = [
            {
                "video_id": "public-" + sha256((story_id + item["title"]).encode("utf-8")).hexdigest()[:16],
                "author": "google-news-index",
                "selection_reason": "fresh_relevant_public_detail_query",
                "method": "public_news_snippet",
                "status": "success",
                "text": item["title"] + "。" + item["summary"],
            }
            for item in outcome["results"]
        ]
        return {"evidence": evidence, "audit": outcome}

    def report(self) -> dict[str, Any]:
        accepted = sum(len(item["results"]) for item in self.outcomes)
        discarded = sum(len(item["discarded"]) for item in self.outcomes)
        return {
            "provider": "google_news_rss",
            "business_date": self.business_date,
            "queries": self.queries,
            "downloaded_bytes": self.downloaded_bytes,
            "accepted_results": accepted,
            "discarded_results": discarded,
            "outcomes": self.outcomes,
            "errors": self.errors,
            "status": "success" if not self.errors else "partial",
        }
