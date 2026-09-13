from __future__ import annotations

import json
import re
from html import unescape
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from .normalize import parse_datetime


@dataclass(slots=True)
class NewsArticle:
    title: str
    url: str
    published_at: str | None
    summary: str
    source_name: str
    source_kind: str
    source_domain: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class NewsEvent:
    title: str
    articles: list[NewsArticle] = field(default_factory=list)
    confirmed: bool = False
    confirmation_reason: str = ""
    douyin_matches: list[dict[str, Any]] = field(default_factory=list)
    score: float = 0.0


def _text(node: ET.Element | None, names: tuple[str, ...]) -> str:
    if node is None:
        return ""
    for child in list(node):
        if child.tag.rsplit("}", 1)[-1].casefold() in names and (child.text or "").strip():
            return (child.text or "").strip()
    return ""


def _date(value: str, timezone_name: str) -> str | None:
    if not value:
        return None
    direct = parse_datetime(value, timezone_name)
    if direct:
        return direct
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.astimezone(ZoneInfo(timezone_name)).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        return None


def parse_feed(data: bytes, source: dict[str, Any], timezone_name: str) -> list[NewsArticle]:
    source_name = str(source.get("name") or "unknown")
    source_kind = str(source.get("kind") or "media")
    source_url = str(source.get("url") or "")
    domain = urlparse(source_url).netloc.casefold()
    stripped = data.lstrip()
    if stripped.startswith((b"{", b"[")):
        payload = json.loads(data.decode("utf-8-sig"))
        rows = payload if isinstance(payload, list) else payload.get("items", [])
        return [NewsArticle(str(row.get("title") or "").strip(), str(row.get("url") or row.get("link") or "").strip(), _date(str(row.get("published_at") or row.get("date") or ""), timezone_name), str(row.get("summary") or row.get("description") or "").strip(), source_name, source_kind, domain) for row in rows if isinstance(row, dict) and str(row.get("title") or "").strip()]
    root = ET.fromstring(data)
    articles: list[NewsArticle] = []
    for node in root.iter():
        local = node.tag.rsplit("}", 1)[-1].casefold()
        if local not in {"item", "entry"}:
            continue
        title = _text(node, ("title",))
        link = _text(node, ("link",))
        if not link:
            link_node = next((child for child in list(node) if child.tag.rsplit("}", 1)[-1].casefold() == "link"), None)
            link = str((link_node.attrib if link_node is not None else {}).get("href") or "")
        published = _text(node, ("pubdate", "published", "updated", "date"))
        summary = _text(node, ("description", "summary", "content"))
        if title:
            articles.append(NewsArticle(title, link, _date(published, timezone_name), unescape(re.sub(r"<[^>]+>", " ", summary)).strip(), source_name, source_kind, domain))
    return articles


def fetch_sources(config: dict[str, Any], inputs: list[str] | None = None) -> tuple[list[NewsArticle], list[dict[str, str]]]:
    settings = config["jobs"]["daily_news"]
    sources = [item for item in settings.get("sources", []) if item.get("enabled", True)]
    if inputs:
        sources = [{"name": Path(value).stem, "url": value, "kind": "media", "enabled": True} for value in inputs]
    rows: list[NewsArticle] = []
    errors: list[dict[str, str]] = []
    timeout = float(settings.get("source_timeout_seconds") or 20)
    for source in sources:
        url = str(source.get("url") or "").strip()
        try:
            path = Path(url)
            if path.is_file():
                data = path.read_bytes()
            else:
                if not url.startswith("https://"):
                    raise ValueError("新闻源必须使用 HTTPS")
                with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False, headers={"User-Agent": "copy-skill-news/1.0"}) as client:
                    response = client.get(url)
                    response.raise_for_status()
                    data = response.content
            rows.extend(parse_feed(data, source, str(config["timezone"])))
        except Exception as exc:
            errors.append({"source": str(source.get("name") or url), "error": str(exc)[:300]})
    return rows, errors


def _fingerprint(title: str) -> set[str]:
    clean = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", title.casefold())
    return {clean[index:index + 2] for index in range(max(0, len(clean) - 1))}


def cluster_articles(articles: list[NewsArticle]) -> list[NewsEvent]:
    events: list[NewsEvent] = []
    for article in articles:
        tokens = _fingerprint(article.title)
        match = None
        best = 0.0
        for event in events:
            other = _fingerprint(event.title)
            similarity = len(tokens & other) / max(1, min(len(tokens), len(other)))
            if similarity > best:
                best, match = similarity, event
        if match is not None and best >= 0.55:
            match.articles.append(article)
        else:
            events.append(NewsEvent(article.title, [article]))
    for event in events:
        domains = {article.source_domain or article.source_name for article in event.articles}
        primary = any(article.source_kind in {"official", "primary"} for article in event.articles)
        event.confirmed = primary or len(domains) >= 2
        event.confirmation_reason = "含官方/一手来源" if primary else f"{len(domains)} 个独立来源" if len(domains) >= 2 else "仅一个非官方来源"
    return events
