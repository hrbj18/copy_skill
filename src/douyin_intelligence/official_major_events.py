from __future__ import annotations

"""Bounded primary-source discovery for the V2 daily material package.

This module intentionally discovers *attributed source events*, not truth.  It
does not fetch article bodies, search-engine pages, social posts, credentials,
or arbitrary URLs.  The caller may subsequently ask Douyin for an independent
propagation signal, which is kept outside the existing heat calculation.
"""

import hashlib
import json
import re
import time
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from .material_probe import MaterialProbeError, RequestBudget, SafeFetcher, _safe_error, validate_https_url
from .news_sources import NewsArticle, parse_feed


_SPACE = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
_PUNCT = re.compile(r"[^0-9a-z\u4e00-\u9fff]+", re.IGNORECASE)
_ISO_DATE = re.compile(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b")
_ZH_DATE = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日")
_EN_DATE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2}),?\s+(20\d{2})\b",
    re.IGNORECASE,
)
_ANCHOR_HREF = re.compile(r"<a\b[^>]*\bhref\s*=\s*(['\"])(?P<href>.*?)\1[^>]*>", re.IGNORECASE | re.DOTALL)
_HEADING = re.compile(r"<h[1-4]\b[^>]*>(?P<title>.*?)</h[1-4]>", re.IGNORECASE | re.DOTALL)
_PARAGRAPH = re.compile(r"<p\b[^>]*>(?P<summary>.*?)</p>", re.IGNORECASE | re.DOTALL)
_FEED_ENTRY = re.compile(r"<(?:[\w.-]+:)?(?:entry|item)\b[^>]*>(?P<body>.*?)</(?:[\w.-]+:)?(?:entry|item)>", re.IGNORECASE | re.DOTALL)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_ZH = re.compile(r"[\u4e00-\u9fff]")
_CONSUMER_META = re.compile(r"(?:据输入(?:摘要)?|根据输入(?:摘要)?|输入摘要(?:显示|称)?|来源(?:显示|称|标注)?|发布(?:方)?(?:表示|称)|公开来源)")
_GOOGLE_NEWS_PUBLISHER = re.compile(r"\s+[-—]\s+(?P<publisher>[^-—]{2,80})$")
_NEWS_INDEX_NOISE = re.compile(r"(?:早报|午报|晚报|股价|概念股|ETF|基金|涨跌|收盘|征稿|课程|招聘|评论|观点|综述|盘点|怎么|为何|如何|哪些|值得买吗|实测对比|步骤|解析|教程|指南|副业|获奖|大赛|调研|推进)", re.IGNORECASE)
_NEWS_INDEX_EVENT = re.compile(r"(?:发布|开源|上线|推出|升级|更新|测试|量产|交付|融资|收购|签约|启动|建成|投产|获批|落地|扩大|下架|上调|进入|接入|开放|发布会|启幕|举行|亮相|首发|支持)", re.IGNORECASE)
_READER_JARGON = ("生态", "基座", "端侧", "具身", "评测基准", "迁移")
_DEFAULT_READER_EDITORIAL = {
    "preview_terms": ("预告", "计划", "即将", "将于", "拟", "有望", "或将", "准备推出"),
    "research_terms": ("评测基准", "benchmark", "测试标准", "数据集", "论文", "协议", "迁移", "企业迁移"),
    "platform_terms": ("开放平台", "开发者平台", "开放能力", "开放基座", "新增功能", "新增能力", "升级"),
    "consumer_impact_terms": ("用户", "消费者", "手机", "汽车", "智能硬件", "眼镜", "耳机", "家用", "设备", "应用"),
    "industry_only_terms": ("开发者", "企业级", "基础设施", "工作流", "连接器", "框架", "协议", "数据驻留"),
    "mainstream_categories": ("ai_security", "model_release", "generative_media", "policy_governance", "consumer_product", "autonomous_mobility", "embodied_ai"),
}


def _feed_tag(body: str, names: tuple[str, ...]) -> str:
    for name in names:
        match = re.search(rf"<(?:[\w.-]+:)?{re.escape(name)}\b[^>]*>(?P<value>.*?)</(?:[\w.-]+:)?{re.escape(name)}>", body, re.IGNORECASE | re.DOTALL)
        if match:
            return _clean(match.group("value"), limit=800)
    return ""


def _recover_malformed_feed(data: bytes, source: dict[str, Any], timezone_name: str) -> list[NewsArticle]:
    """Small fail-closed fallback for an otherwise public RSS/Atom feed.

    Some feeds embed malformed HTML inside a content field, which makes strict
    ElementTree reject the whole response.  This parser only accepts complete
    entry/item blocks with a title, date and link; it never repairs arbitrary
    markup or invents fields.
    """
    document = data.decode("utf-8", errors="replace")
    source_name = str(source.get("name") or "unknown")
    source_kind = str(source.get("kind") or "official")
    source_domain = (urlsplit(str(source.get("url") or "")).hostname or "").casefold()
    rows: list[NewsArticle] = []
    for entry in _FEED_ENTRY.finditer(document):
        body = entry.group("body")
        title = _feed_tag(body, ("title",))
        date_text = _feed_tag(body, ("published", "updated", "pubDate", "date"))
        link_match = re.search(r"<(?:[\w.-]+:)?link\b[^>]*\bhref\s*=\s*(['\"])(?P<href>.*?)\1[^>]*/?>", body, re.IGNORECASE | re.DOTALL)
        link = link_match.group("href") if link_match else _feed_tag(body, ("link",))
        published_at = _listing_date(date_text, timezone_name)
        if not published_at:
            # RFC822-style pubDate is already handled by the strict parser.  A
            # malformed feed without an ISO-style entry date is safely skipped.
            continue
        if title and link:
            rows.append(NewsArticle(title, link, published_at, _feed_tag(body, ("summary", "description", "content")), source_name, source_kind, source_domain))
    return rows


def _clean(value: Any, *, limit: int = 500) -> str:
    text = _SPACE.sub(" ", _TAG.sub(" ", str(value or ""))).strip()
    return text[:limit].rstrip()


def _title_key(value: str) -> str:
    return _PUNCT.sub("", value.casefold())[:240]


def _event_identity_key(title: str) -> str:
    """Use only explicit named/versioned anchors for a cross-source merge."""
    normalized = _title_key(title)
    anchors = (
        ("astra", "entity_astra"), ("atlas", "entity_atlas"),
        ("fable51", "entity_fable51"), ("mythos51", "entity_mythos51"),
    )
    for token, key in anchors:
        if token in normalized:
            return key
    return normalized


def _public_index_same_event(left: NewsArticle, right: NewsArticle) -> bool:
    """Conservatively collapse syndications without chaining broad topics."""
    if left.source_kind not in {"news_index", "media"} or right.source_kind not in {"news_index", "media"}:
        return False
    def fingerprints(value: str) -> set[str]:
        normalized = _title_key(value)
        for noise in ("发布", "开源", "上线", "推出", "升级", "更新", "正式", "最新", "首个"):
            normalized = normalized.replace(noise, "")
        return {normalized[index:index + 2] for index in range(max(0, len(normalized) - 1))}
    first, second = fingerprints(left.title), fingerprints(right.title)
    if min(len(first), len(second)) < 5:
        return False
    overlap = len(first & second)
    ratio = overlap / min(len(first), len(second))
    first_numbers, second_numbers = set(_NUMBER.findall(left.title)), set(_NUMBER.findall(right.title))
    if first_numbers and second_numbers and not first_numbers & second_numbers:
        return False
    return overlap >= 4 and ratio >= 0.5


def _is_major_candidate(article: NewsArticle) -> bool:
    """Avoid filling a limited news list with routine maintenance notes."""
    text = _title_key(article.title)
    routine_terms = ("githubcli", "deprecat", "documentation", "bugfix", "releasecandidate", "modelaccessupdate")
    malformed_listing_title = bool(re.match(r"^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\d{1,2}\d{4}(?:announcement)?", text))
    if malformed_listing_title or any(term in text for term in routine_terms):
        return False
    if article.source_kind in {"news_index", "media"}:
        headline = _clean(article.title, limit=260)
        return bool(_NEWS_INDEX_EVENT.search(headline)) and not bool(_NEWS_INDEX_NOISE.search(headline))
    return True


def _safe_same_origin_article(value: str, source: dict[str, Any]) -> str:
    allowed = source.get("allowed_domains") or []
    return validate_https_url(value, allowed)


def _listing_date(value: str, timezone_name: str) -> str | None:
    text = _clean(value, limit=200)
    match = _ISO_DATE.search(text) or _ZH_DATE.search(text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            return datetime(year, month, day, tzinfo=ZoneInfo(timezone_name)).isoformat(timespec="seconds")
        except ValueError:
            return None
    english = _EN_DATE.search(text)
    if english:
        try:
            month, day, year = english.groups()
            parsed = datetime.strptime(f"{month[:3]} {day} {year}", "%b %d %Y")
            return parsed.replace(tzinfo=ZoneInfo(timezone_name)).isoformat(timespec="seconds")
        except ValueError:
            return None
    return None


def _raw_heading_cards(document: str) -> list[dict[str, Any]]:
    """Recover malformed nested anchor cards emitted by some modern SSR sites.

    HTML formally forbids nested anchors, yet several public index pages render
    them.  HTMLParser closes the outer card early, separating its heading from
    its date.  This bounded fallback only looks ahead from each same-page anchor
    to a nearby heading/date; URL validation still happens in the caller.
    """
    records: list[dict[str, Any]] = []
    for match in _ANCHOR_HREF.finditer(document):
        window = document[match.end(): match.end() + 5000]
        heading = _HEADING.search(window)
        if not heading:
            continue
        title = _clean(heading.group("title"), limit=240)
        prefix = window[: max(heading.end(), 1) + 1800]
        summary_match = _PARAGRAPH.search(window, heading.end())
        summary = _clean(summary_match.group("summary"), limit=420) if summary_match else ""
        if title and _listing_date(prefix, "Asia/Shanghai"):
            records.append({"href": match.group("href"), "title": [title], "time": [prefix], "summary": [summary] if summary else [], "text": [title, summary] if summary else [title, prefix]})
    return records


class _ListingParser(HTMLParser):
    """Extract repeated article-card anchors without depending on page CSS."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[str] = []
        self._active: list[dict[str, Any]] = []
        self.records: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        values = {str(key).casefold(): str(value or "").strip() for key, value in attrs}
        self._stack.append(tag)
        if tag == "a" and values.get("href"):
            self._active.append({"href": values["href"], "title": [], "time": [], "summary": [], "text": []})

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        text = _clean(data, limit=800)
        if not text:
            return
        current = self._stack[-1] if self._stack else ""
        for item in self._active:
            item["text"].append(text)
            if current in {"h1", "h2", "h3", "h4"}:
                item["title"].append(text)
            elif current == "time":
                item["time"].append(text)
            elif current in {"p", "div"}:
                item["summary"].append(text)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "a" and self._active:
            self.records.append(self._active.pop())
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index] == tag:
                del self._stack[index:]
                break


class _ArticleDetailParser(HTMLParser):
    """Read public article descriptions without executing page scripts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.descriptions: list[str] = []
        self.paragraphs: list[str] = []
        self._in_paragraph = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).casefold(): str(value or "").strip() for key, value in attrs}
        if tag.casefold() == "meta":
            key = (values.get("name") or values.get("property") or "").casefold()
            if key in {"description", "og:description", "twitter:description"} and values.get("content"):
                self.descriptions.append(_clean(values["content"], limit=900))
        elif tag.casefold() == "p":
            self._in_paragraph = True

    def handle_data(self, data: str) -> None:
        if self._in_paragraph:
            text = _clean(data, limit=900)
            if text:
                self.paragraphs.append(text)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "p":
            self._in_paragraph = False


def _article_detail_text(document: bytes) -> str:
    parser = _ArticleDetailParser()
    parser.feed(document.decode("utf-8", errors="replace"))
    for value in [*parser.descriptions, *parser.paragraphs]:
        text = _clean(value, limit=900)
        if len(text) >= 40:
            return text
    return ""


def parse_html_listing(data: bytes, source: dict[str, Any], timezone_name: str) -> list[NewsArticle]:
    """Parse only date-bearing same-origin cards from an official index page."""
    document = data.decode("utf-8", errors="replace")
    parser = _ListingParser()
    parser.feed(document)
    source_name = str(source.get("name") or "unknown")
    source_kind = str(source.get("kind") or "official")
    source_url = str(source.get("url") or "")
    domain = (urlsplit(source_url).hostname or "").casefold()
    prefixes = tuple(str(value) for value in source.get("article_path_prefixes") or ["/"])
    output: list[NewsArticle] = []
    seen: set[str] = set()
    for card in [*_raw_heading_cards(document), *parser.records]:
        try:
            link = _safe_same_origin_article(urljoin(source_url, str(card.get("href") or "")), source)
        except MaterialProbeError:
            continue
        path = urlsplit(link).path or "/"
        if not any(path.startswith(prefix) for prefix in prefixes):
            continue
        title_options = [_clean(" ".join(card.get("title") or []), limit=240), _clean(" ".join(card.get("text") or []), limit=240)]
        title = next((item for item in title_options if len(item) >= 4), "")
        # A card may put its date in a paragraph rather than a <time> element.
        all_text = " ".join([*(card.get("time") or []), *(card.get("text") or [])])
        published_at = _listing_date(all_text, timezone_name)
        if not title or not published_at or link in seen:
            continue
        summary = _clean(" ".join(card.get("summary") or []), limit=420)
        if not summary or summary == title:
            summary = _clean(" ".join(card.get("text") or []), limit=420)
        output.append(NewsArticle(title, link, published_at, summary, source_name, source_kind, domain))
        seen.add(link)
    return output


def _parse_source(data: bytes, source: dict[str, Any], timezone_name: str) -> list[NewsArticle]:
    format_name = str(source.get("format") or "feed").casefold()
    if format_name == "google_news_rss":
        output: list[NewsArticle] = []
        for article in parse_feed(data, source, timezone_name):
            match = _GOOGLE_NEWS_PUBLISHER.search(article.title)
            publisher = _clean(match.group("publisher"), limit=80) if match else "Google News"
            headline = _clean(article.title[:match.start()] if match else article.title, limit=240)
            if headline:
                output.append(
                    NewsArticle(
                        headline,
                        article.url,
                        article.published_at,
                        _clean(article.summary, limit=420),
                        publisher,
                        "news_index",
                        _title_key(publisher) or "google_news",
                    )
                )
        return output
    if format_name == "html_listing":
        return parse_html_listing(data, source, timezone_name)
    if format_name == "feed":
        try:
            return parse_feed(data, source, timezone_name)
        except Exception:
            return _recover_malformed_feed(data, source, timezone_name)
    raise ValueError("unsupported_source_format")


def fetch_official_sources(
    config: dict[str, Any],
    *,
    fetcher_factory: Callable[[dict[str, Any], RequestBudget], SafeFetcher] = SafeFetcher,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[NewsArticle], list[dict[str, str]], dict[str, Any]]:
    """Fetch configured public source indexes under one shared bounded budget."""
    settings = config["jobs"]["daily_hot_candidate_pool_v2"]["official_discovery"]
    budget = RequestBudget(
        max_requests=int(settings["max_requests"]),
        max_total_bytes=int(settings["max_total_bytes"]),
        total_timeout_seconds=float(settings["total_timeout_seconds"]),
        started_at=clock(),
    )
    source_settings = {
        "request_timeout_seconds": int(settings["request_timeout_seconds"]),
        "max_redirects": int(settings["max_redirects"]),
        "fake_ip_networks": list(settings.get("fake_ip_networks") or []),
        # Replaced for every source below.  SafeFetcher validates redirects too.
        "allowed_domains": [],
    }
    fetcher = fetcher_factory(source_settings, budget)
    articles: list[NewsArticle] = []
    errors: list[dict[str, str]] = []
    try:
        sources = [item for item in settings.get("sources") or [] if isinstance(item, dict) and item.get("enabled", True)]
        for source in sources[: int(settings["max_sources"])]:
            name = str(source.get("name") or "official_source")
            try:
                source_settings["allowed_domains"] = list(source.get("allowed_domains") or [])
                if not source_settings["allowed_domains"]:
                    raise ValueError("source_missing_allowed_domains")
                _, _, data = fetcher.get(
                    str(source.get("url") or ""),
                    maximum_bytes=int(settings["max_page_bytes"]),
                    accepted_types=("application/xml", "text/xml", "application/rss+xml", "application/atom+xml", "application/json", "text/html", "application/xhtml+xml"),
                )
                parsed = _parse_source(data, source, str(config["timezone"]))
                for article in parsed:
                    try:
                        article.url = _safe_same_origin_article(urljoin(str(source.get("url") or ""), article.url), source)
                    except MaterialProbeError:
                        # A feed entry may contain an external redirect/tracker.  It
                        # is not eligible for this source-first lane.
                        continue
                    articles.append(article)
            except Exception as exc:
                errors.append({"source": name, "error": _safe_error(exc)})
    finally:
        fetcher.close()
    return articles, errors, budget.snapshot()


def _category(title: str, summary: str) -> tuple[str, int, str]:
    text = f"{title} {summary}".casefold()
    rules = (
        ("ai_security", ("cybersecurity", "security", "漏洞", "网络安全"), 28, "AI 安全或网络安全能力变化"),
        ("model_release", ("model", "模型", "open source", "开源", "weights", "权重"), 26, "模型能力或开放策略变化"),
        ("generative_media", ("world model", "video", "3d", "图像", "视频", "空间智能"), 23, "生成式内容或空间智能能力变化"),
        ("policy_governance", ("网信", "监管", "policy", "governance", "清朗", "合规"), 23, "政策与平台治理变化"),
        ("ai_infrastructure", ("server", "data center", "supercomputer", "芯片", "算力", "服务器"), 20, "AI 基础设施或产业投入变化"),
        ("embodied_ai", ("人形机器人", "具身", "机器人", "embodied"), 23, "具身智能或机器人能力变化"),
        ("autonomous_mobility", ("自动驾驶", "智能驾驶", "辅助驾驶", "智驾", "vla"), 23, "智能驾驶或自动化出行能力变化"),
        ("consumer_product", ("手机", "device", "产品", "hardware", "e-sim", "esim", "打印机"), 18, "面向用户的产品或设备变化"),
    )
    for name, terms, bonus, reason in rules:
        if any(term in text for term in terms):
            return name, bonus, reason
    return "technology_update", 12, "科技行业的重要公开动态"


def _source_weight(article: NewsArticle) -> int:
    # A same-day article fetched from a configured publisher feed is more
    # useful for the reader-facing lane than an otherwise similar index-only
    # headline: it is the one for which we can obtain bounded article detail.
    # This does not change the separate Douyin heat calculation.
    return 70 if article.source_kind in {"official", "primary"} else 55 if article.source_kind in {"authority", "government"} else 64 if article.source_kind == "media" else 48 if article.source_kind == "news_index" else 40


def _public_index_bonus(title: str, refs: list[dict[str, Any]]) -> int:
    """Prefer concrete product or company events over generic trend coverage."""
    text = _clean(title, limit=260)
    direct = sum(term in text for term in ("发布", "开源", "上线", "推出", "融资", "收购", "量产", "交付", "首发", "获批"))
    named = bool(re.search(r"(?:[A-Za-z]{2,}|\d+(?:\.\d+)?|[\u4e00-\u9fff]{2,12}(?:模型|芯片|机器人|智能体|平台))", text))
    return min(16, direct * 4 + (4 if named else 0) + min(8, max(0, len(refs) - 1) * 4))


def _event_id(title: str, published_at: str) -> str:
    digest = hashlib.sha256(f"{_title_key(title)}|{published_at[:10]}".encode("utf-8")).hexdigest()[:16]
    return f"official-{digest}"


def _douyin_query(title: str) -> str:
    # Keep the actual named title as the query: no invented entity and no generic
    # "AI news" backfill.  MediaCrawler itself caps results per keyword.
    text = _clean(title, limit=80)
    return text[:48].strip(" -—:：，,。.")


def _company_event_priority(
    title: str,
    summary: str,
    category: str,
    settings: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return an auditable reader-delivery preference without changing truth or heat."""
    empty = {
        "status": "unmatched",
        "tier": None,
        "boost": 0,
        "matched_aliases": [],
        "action_category": category,
        "reason": "no_priority_company_match",
    }
    if not settings or settings.get("enabled") is not True:
        return {**empty, "status": "disabled", "reason": "company_priority_disabled"}
    # A summary may quote another company, compare prices, or cite a market
    # context that does not identify the event owner.  Delivery preference is
    # therefore deliberately title-bound: its audit trail must answer "who
    # did what" from the event headline itself, rather than from incidental
    # prose pulled from a third-party summary.
    title_text = _clean(title, limit=260).casefold()
    routine_terms = [str(item).strip() for item in settings.get("routine_exclusion_terms") or [] if str(item).strip()]
    context_terms = [str(item).strip() for item in settings.get("context_exclusion_terms") or [] if str(item).strip()]
    tiers = [item for item in settings.get("tiers") or [] if isinstance(item, dict)]
    for tier in tiers:
        aliases = [str(item).strip() for item in tier.get("aliases") or [] if str(item).strip()]
        matches = [alias for alias in aliases if alias.casefold() in title_text]
        if not matches:
            continue
        tier_id = str(tier.get("id") or "").strip() or "configured_tier"
        if any(term.casefold() in title_text for term in routine_terms):
            return {
                "status": "excluded_routine_product",
                "tier": tier_id,
                "boost": 0,
                "matched_aliases": matches,
                "action_category": category,
                "reason": "routine_product_or_commercial_anchor",
            }
        if any(term.casefold() in title_text for term in context_terms):
            return {
                "status": "matched_non_company_action",
                "tier": tier_id,
                "boost": 0,
                "matched_aliases": matches,
                "action_category": category,
                "reason": "opinion_or_third_party_mention",
            }
        eligible_categories = {str(item) for item in tier.get("eligible_categories") or []}
        if category not in eligible_categories:
            return {
                "status": "matched_ineligible_action",
                "tier": tier_id,
                "boost": 0,
                "matched_aliases": matches,
                "action_category": category,
                "reason": "event_category_not_eligible",
            }
        required_terms = [str(item).strip() for item in tier.get("required_terms") or [] if str(item).strip()]
        if required_terms and not any(term.casefold() in title_text for term in required_terms):
            return {
                "status": "matched_missing_technology_anchor",
                "tier": tier_id,
                "boost": 0,
                "matched_aliases": matches,
                "action_category": category,
                "reason": "required_technology_anchor_missing",
            }
        action_terms = [str(item).strip() for item in tier.get("action_terms") or [] if str(item).strip()]
        if not any(term.casefold() in title_text for term in action_terms):
            return {
                "status": "matched_missing_major_action",
                "tier": tier_id,
                "boost": 0,
                "matched_aliases": matches,
                "action_category": category,
                "reason": "major_action_anchor_missing",
            }
        return {
            "status": "boosted",
            "tier": tier_id,
            "boost": int(tier.get("boost") or 0),
            "matched_aliases": matches,
            "action_category": category,
            "reason": "company_and_major_action_matched",
        }
    return empty


def _reader_policy_terms(settings: dict[str, Any] | None, key: str) -> tuple[str, ...]:
    configured = settings.get(key) if isinstance(settings, dict) else None
    values = configured if isinstance(configured, list) else _DEFAULT_READER_EDITORIAL[key]
    return tuple(str(item).strip() for item in values if str(item).strip())


def _reader_event_text(event: dict[str, Any]) -> str:
    detail = event.get("detail_evidence") if isinstance(event.get("detail_evidence"), list) else []
    detail_text = " ".join(_clean(item.get("text"), limit=900) for item in detail if isinstance(item, dict))
    return _clean(f"{event.get('title') or ''} {event.get('summary') or ''} {detail_text}", limit=1800)


def _contains_any(text: str, terms: tuple[str, ...]) -> list[str]:
    folded = text.casefold()
    return [term for term in terms if term.casefold() in folded]


def classify_public_reader_event(
    event: dict[str, Any],
    reader_editorial: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify what a dated source proves and which reader lane receives it."""
    text = _reader_event_text(event)
    category = _clean(event.get("event_category"), limit=80)
    preview_hits = _contains_any(text, _reader_policy_terms(reader_editorial, "preview_terms"))
    research_hits = _contains_any(text, _reader_policy_terms(reader_editorial, "research_terms"))
    platform_hits = _contains_any(text, _reader_policy_terms(reader_editorial, "platform_terms"))
    consumer_hits = _contains_any(text, _reader_policy_terms(reader_editorial, "consumer_impact_terms"))
    industry_hits = _contains_any(text, _reader_policy_terms(reader_editorial, "industry_only_terms"))
    mainstream_categories = set(_reader_policy_terms(reader_editorial, "mainstream_categories"))

    if preview_hits:
        freshness = "preview_or_plan"
        freshness_reason = "preview_or_future_action_anchor"
    elif research_hits:
        freshness = "research_or_industry_infrastructure"
        freshness_reason = "research_or_industry_infrastructure_anchor"
    elif platform_hits:
        freshness = "new_platform_or_capability"
        freshness_reason = "platform_or_capability_anchor"
    elif category in {"model_release", "generative_media", "ai_security"}:
        freshness = "model_or_major_technology_release"
        freshness_reason = "category_indicates_major_technology_action"
    elif category in {"consumer_product", "autonomous_mobility", "embodied_ai"}:
        freshness = "new_product_or_service"
        freshness_reason = "category_indicates_product_or_service_action"
    else:
        freshness = "unclear_from_available_evidence"
        freshness_reason = "available_source_text_does_not_establish_action_level"

    if freshness in {"preview_or_plan", "research_or_industry_infrastructure"}:
        lane = "industry_brief"
        routing_reason = "preview_or_research_is_not_a_general_reader_headline"
    elif freshness == "new_platform_or_capability" and industry_hits and not consumer_hits:
        lane = "industry_brief"
        routing_reason = "developer_or_enterprise_platform_without_consumer_impact"
    elif category in mainstream_categories or consumer_hits:
        lane = "mainstream"
        routing_reason = "concrete_public_technology_action_or_consumer_impact"
    else:
        lane = "industry_brief"
        routing_reason = "industry_action_lacks_a_clear_general_reader_impact"

    event["event_freshness"] = {
        "classification": freshness,
        "reason": freshness_reason,
        "source_published_at": _clean(event.get("published_at"), limit=48),
        "event_first_release_status": "not_inferred_from_source_publication_date",
        "evidence_scope": "dated_source_entry_and_allowlisted_detail_when_present",
        "matched_terms": {"preview": preview_hits, "research": research_hits, "platform": platform_hits},
    }
    event["audience_routing"] = {
        "lane": lane,
        "reason": routing_reason,
        "consumer_impact_terms": consumer_hits,
        "industry_terms": industry_hits,
    }
    return event


def apply_public_reader_policy(
    events: list[dict[str, Any]],
    reader_editorial: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Refresh deterministic reader classification after detail enrichment."""
    for event in events:
        classify_public_reader_event(event, reader_editorial)
        language = event.get("reader_language") if isinstance(event.get("reader_language"), dict) else {}
        event["reader_language"] = {
            "writing_status": str(language.get("writing_status") or "not_written"),
            "title": _clean(language.get("title"), limit=160),
            "summary": _clean(language.get("summary"), limit=460),
            "plain_explanation": _clean(language.get("plain_explanation"), limit=220),
            "fallback_reason": _clean(language.get("fallback_reason"), limit=120),
        }
    return events


def build_official_major_events(
    articles: list[NewsArticle],
    *,
    business_date: str,
    maximum: int,
    company_priority: dict[str, Any] | None = None,
    reader_editorial: dict[str, Any] | None = None,
    public_web_discovery_by_url: dict[str, list[dict[str, str]]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Create separately ranked source events; no fuzzy cross-entity merge."""
    excluded: list[dict[str, str]] = []
    grouped: dict[str, list[NewsArticle]] = {}
    for article in articles:
        if not article.published_at or not article.published_at.startswith(business_date):
            excluded.append({"title": _clean(article.title, limit=120), "reason": "not_target_business_date"})
            continue
        if not article.title or not article.url:
            excluded.append({"title": _clean(article.title, limit=120), "reason": "missing_title_or_url"})
            continue
        if not _is_major_candidate(article):
            excluded.append({"title": _clean(article.title, limit=120), "reason": "routine_maintenance_not_major_event"})
            continue
        identity = _event_identity_key(article.title)
        if article.source_kind in {"news_index", "media"}:
            match = next(
                (key for key, rows in grouped.items() if rows and _public_index_same_event(article, rows[0])),
                None,
            )
            if match:
                identity = match
        grouped.setdefault(identity, []).append(article)
    events: list[dict[str, Any]] = []
    for _, rows in sorted(grouped.items()):
        primary = sorted(rows, key=lambda row: (-_source_weight(row), -len(row.title + row.summary), row.published_at or "", row.url))[0]
        category, category_bonus, category_reason = _category(primary.title, primary.summary)
        refs = [
            {
                "name": item.source_name,
                "url": item.url,
                "published_at": item.published_at,
                "kind": item.source_kind,
                "domain": item.source_domain,
                "extractor": "official_index",
            }
            for item in sorted(rows, key=lambda item: (item.source_name, item.url))
        ]
        discovery_rows: list[dict[str, str]] = []
        seen_discovery: set[tuple[str, str]] = set()
        for item in rows:
            for discovered in (public_web_discovery_by_url or {}).get(item.url, []):
                if not isinstance(discovered, dict):
                    continue
                key = (str(discovered.get("query_id") or ""), str(discovered.get("result_url") or ""))
                if not key[0] or not key[1] or key in seen_discovery:
                    continue
                seen_discovery.add(key)
                discovery_rows.append({str(name): _clean(value, limit=240) for name, value in discovered.items()})
        score = _source_weight(primary) + category_bonus + (_public_index_bonus(primary.title, refs) if primary.source_kind in {"news_index", "media"} else 0)
        company_delivery = _company_event_priority(primary.title, primary.summary, category, company_priority)
        reader_delivery_score = score + int(company_delivery["boost"])
        source_status = (
            "official_primary_source_attributed"
            if primary.source_kind in {"official", "primary"}
            else "authority_source_attributed"
            if primary.source_kind in {"authority", "government"}
            else "public_news_index_attributed"
        )
        events.append(
            {
                "official_event_id": _event_id(primary.title, primary.published_at or business_date),
                "title": _clean(primary.title, limit=220),
                "summary": _clean(primary.summary, limit=420) or _clean(primary.title, limit=220),
                "published_at": primary.published_at,
                "business_date": business_date,
                "event_category": category,
                "official_importance_score": score,
                "company_event_priority": company_delivery,
                "reader_delivery_score": reader_delivery_score,
                "importance_reasons": [f"{primary.source_name}：{source_status}", category_reason],
                "source_status": source_status,
                "primary_source_kind": primary.source_kind,
                "source_refs": refs,
                "primary_source_url": primary.url,
                "public_web_discovery": discovery_rows,
                "discovery_channels": ["configured_source", *(["public_web_search"] if discovery_rows else [])],
                "observed_heat_status": "unknown",
                "candidate_status": "lead_only" if discovery_rows else "detail_backed" if primary.source_kind in {"official", "primary", "authority", "government", "media"} else "needs_more_sources",
                "douyin_query": _douyin_query(primary.title),
                "douyin_signal": {"status": "not_attempted", "matched_video_count": 0, "raw_interactions": {"like": 0, "comment": 0, "collect": 0, "share": 0}, "videos": []},
                "truth_status": "not_checked",
                "disclaimer": "来源归因不等于 copy_skill 对新闻事实、数字或未来结果的核验。",
            }
        )
    apply_public_reader_policy(events, reader_editorial)
    events.sort(key=lambda item: (-int(item["reader_delivery_score"]), -int(item["official_importance_score"]), str(item["published_at"]), str(item["official_event_id"])))
    for index, event in enumerate(events[:maximum], 1):
        event["official_rank"] = index
    return events[:maximum], excluded


def enrich_public_event_details(
    events: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    fetcher_factory: Callable[[dict[str, Any], RequestBudget], SafeFetcher] = SafeFetcher,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch an allowlisted article description for public-source events only.

    Direct publisher feeds are attempted first.  Google News remains a bounded
    discovery input and may redirect to an allowlisted publisher article.  A
    non-redirecting index shell is not treated as article detail.  Failure
    leaves the original index snippet intact and is reported per event.
    """
    settings = config["jobs"]["daily_hot_candidate_pool_v2"]["official_discovery"]
    public_events = [event for event in events if event.get("source_status") == "public_news_index_attributed"]
    # Use entries with a first-party media URL before index-only entries so a
    # finite article budget cannot be spent entirely on search shells.
    selected = sorted(
        public_events,
        key=lambda event: (
            0 if str(event.get("primary_source_kind") or "") == "media" else 1,
            -int(event.get("reader_delivery_score") or event.get("official_importance_score") or 0),
            str(event.get("official_event_id") or ""),
        ),
    )[: int(settings["article_detail_max_events"])]
    budget = RequestBudget(
        max_requests=int(settings["article_detail_max_requests"]),
        max_total_bytes=int(settings["article_detail_max_total_bytes"]),
        total_timeout_seconds=float(settings["article_detail_total_timeout_seconds"]),
        started_at=clock(),
    )
    fetcher = fetcher_factory(
        {
            "request_timeout_seconds": int(settings["article_detail_timeout_seconds"]),
            "max_redirects": int(settings["max_redirects"]),
            "fake_ip_networks": list(settings.get("fake_ip_networks") or []),
            "allowed_domains": ["news.google.com", *list(settings.get("article_detail_allowed_domains") or [])],
        },
        budget,
    )
    successes = 0
    errors: list[dict[str, str]] = []
    try:
        for event in selected:
            source_url = str(event.get("primary_source_url") or "").strip()
            if not source_url:
                errors.append({"event_id": str(event.get("official_event_id") or ""), "error": "missing_primary_source_url"})
                continue
            try:
                final_url, _, document = fetcher.get(
                    source_url,
                    maximum_bytes=int(settings["article_detail_max_page_bytes"]),
                    accepted_types=("text/html", "application/xhtml+xml"),
                    allow_truncated=True,
                )
                detail = _article_detail_text(document)
                if not detail:
                    raise MaterialProbeError("article_detail_missing")
                existing = _clean(event.get("summary"), limit=420)
                if _title_key(detail) != _title_key(existing):
                    event["summary"] = _clean(f"{existing} {detail}", limit=900)
                event["detail_evidence"] = [{"method": "allowlisted_article_meta", "text": detail, "url": final_url}]
                successes += 1
            except Exception as exc:
                errors.append({"event_id": str(event.get("official_event_id") or ""), "error": _safe_error(exc)})
    finally:
        fetcher.close()
    return events, {
        "selected_events": len(selected),
        "success_count": successes,
        "failure_count": len(errors),
        "errors": errors,
        "usage": budget.snapshot(),
    }


def _reader_fallback_title(event: dict[str, Any]) -> str:
    """Provide a conservative, readable audit fallback without inventing facts."""
    title = _clean(event.get("title"), limit=160).split("：", 1)[0].split(":", 1)[0]
    freshness = str(((event.get("event_freshness") or {}).get("classification") or ""))
    if "WorkBuddy" in title and "开放平台" in title:
        return "腾讯开放 WorkBuddy 开发者平台"
    if "MobileMem" in title:
        return "OPPO 参与发布手机 AI 记忆测试标准"
    if "OpenBridge" in title:
        return "元点机器人预告推出机器人 AI 开源工具"
    replacements = (
        ("Agent", "AI 助手"),
        ("智能体", "AI 助手"),
        ("端侧", "设备本地"),
        ("具身智能", "机器人 AI"),
        ("Physical AI", "机器人 AI"),
        ("开源生态", "开源工具"),
        ("评测基准", "测试标准"),
    )
    for source, target in replacements:
        title = title.replace(source, target)
    if freshness == "new_platform_or_capability" and "开放平台" in title:
        title = title.replace("上线", "开放")
    return _clean(title, limit=80)


def _reader_fallback_explanation(event: dict[str, Any]) -> str:
    freshness = str(((event.get("event_freshness") or {}).get("classification") or ""))
    if freshness == "new_platform_or_capability":
        return "这次报道的重点是新增开放能力，不等于该产品在当天首次推出。"
    if freshness == "preview_or_plan":
        return "目前属于预告或计划，是否落地仍要看后续正式发布。"
    if freshness == "research_or_industry_infrastructure":
        return "这主要是行业研发或测试工具，不是普通用户马上能直接使用的新功能。"
    if freshness == "model_or_major_technology_release":
        return "这是一项模型或核心技术的新动作，具体可用范围以发布说明为准。"
    return "这是一项公开科技动态，具体影响仍以产品后续信息为准。"


def _deterministic_reader_fallback(event: dict[str, Any]) -> tuple[str, str, str, bool]:
    """Return a publishable fallback only for a narrowly understood event shape."""
    source_title = str(event.get("title") or "")
    freshness = str(((event.get("event_freshness") or {}).get("classification") or ""))
    if freshness == "new_platform_or_capability" and "WorkBuddy" in source_title and "开放平台" in source_title:
        return (
            "腾讯开放 WorkBuddy 开发者平台",
            "这次新增的是开放能力，不能把它理解为 WorkBuddy 产品第一次上线。腾讯把这套 AI 助手能力开放给设备厂商、行业应用和开发者。",
            "主要面向设备厂商、行业应用和开发者，不是一个普通用户当天新下载的产品。",
            True,
        )
    title = _reader_fallback_title(event)
    summary = _clean(event.get("summary"), limit=460)
    return title, summary, _reader_fallback_explanation(event), False


def _fallback_editorial_card(event: dict[str, Any], reason: str) -> dict[str, Any]:
    """Keep a conservative reader fallback when a Chinese rewrite is unavailable."""
    title, summary, explanation, reader_safe = _deterministic_reader_fallback(event)
    return {
        "status": "fallback",
        "locale": "source_original",
        "title": title,
        "summary": summary,
        "why_it_matters": explanation,
        "plain_explanation": explanation,
        "reader_safe": reader_safe,
        "source_bound_fields": ["title", "summary", "importance_reasons", "source_refs"],
        "fallback_reason": reason,
    }


def _valid_editorial_text(value: Any, *, maximum: int, source_numbers: set[str]) -> str | None:
    text = _clean(value, limit=maximum)
    if len(text) < 4 or not _ZH.search(text) or "http://" in text.casefold() or "https://" in text.casefold() or _CONSUMER_META.search(text):
        return None
    if not set(_NUMBER.findall(text)) <= source_numbers:
        return None
    return text


def _valid_editorial_title(value: Any, *, source_numbers: set[str], event: dict[str, Any]) -> str | None:
    """Require a reader-facing event sentence rather than a topical label."""
    text = _valid_editorial_text(value, maximum=80, source_numbers=source_numbers)
    if not text or ":" in text or "：" in text or "—" in text or "－" in text:
        return None
    # The source screening already requires a concrete event verb.  Repeat the
    # guard for generated titles so a model cannot turn it back into a bare
    # product/topic label such as "某模型的新进展".
    if not _NEWS_INDEX_EVENT.search(text):
        return None
    if any(term in text for term in _READER_JARGON):
        return None
    freshness = str(((event.get("event_freshness") or {}).get("classification") or ""))
    compact = re.sub(r"\s+", "", text)
    if freshness == "preview_or_plan" and any(term in compact for term in ("正式发布", "已上线", "正式上线", "已经落地")):
        return None
    if freshness == "new_platform_or_capability" and "WorkBuddy" in str(event.get("title") or "") and re.search(r"(?:上线|发布)WorkBuddy", compact, re.IGNORECASE):
        return None
    return text


def _record_reader_language(event: dict[str, Any], card: dict[str, Any]) -> None:
    event["reader_language"] = {
        "writing_status": "success" if card.get("status") == "success" and card.get("locale") == "zh-CN" else "fallback_safe" if card.get("reader_safe") is True else "fallback",
        "title": _clean(card.get("title"), limit=160),
        "summary": _clean(card.get("summary"), limit=460),
        "plain_explanation": _clean(card.get("plain_explanation") or card.get("why_it_matters"), limit=220),
        "fallback_reason": _clean(card.get("fallback_reason"), limit=120),
    }


def _apply_freshness_language_guard(event: dict[str, Any], title: str, summary: str) -> tuple[str, str]:
    """Make the action level explicit even when a model leaves it implicit."""
    freshness = str(((event.get("event_freshness") or {}).get("classification") or ""))
    source_title = str(event.get("title") or "")
    if freshness == "new_platform_or_capability":
        if "WorkBuddy" in source_title and "开放平台" in source_title:
            title = "腾讯开放 WorkBuddy 开发者平台"
        summary = _clean(f"这次新增的是开放能力，不能把它理解为产品第一次上线。{summary}", limit=280)
    elif freshness == "preview_or_plan" and not any(term in summary for term in ("预告", "计划", "即将")):
        summary = _clean(f"目前仍属于预告或计划，尚未等同于正式落地。{summary}", limit=280)
    return title, summary


def localize_official_event_cards(
    events: list[dict[str, Any]],
    *,
    enabled: bool,
    maximum: int,
    max_output_tokens: int,
    generate: Callable[[str, str, int], dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Create source-bound Chinese handoff cards in one bounded model request.

    This is editorial compression/translation only.  It deliberately retains
    every original event field and falls back per event when the model is
    unavailable or its output contains a field that cannot be checked locally.
    """
    mainstream = [
        event for event in events
        if not isinstance(event.get("audience_routing"), dict) or event["audience_routing"].get("lane") == "mainstream"
    ]
    selected = mainstream[: max(0, int(maximum))]
    for event in events:
        if event not in selected and isinstance(event.get("audience_routing"), dict) and event["audience_routing"].get("lane") != "mainstream":
            event["reader_language"] = {
                "writing_status": "not_applicable_industry_brief",
                "title": "",
                "summary": "",
                "plain_explanation": "",
                "fallback_reason": _clean(event["audience_routing"].get("reason"), limit=120),
            }
    if not selected:
        return events, {"status": "skipped", "requested": 0, "success_count": 0, "fallback_count": 0, "request_count": 0, "industry_brief_count": len(events) - len(mainstream), "errors": []}
    if not enabled:
        for event in selected:
            event["editorial_card"] = _fallback_editorial_card(event, "disabled")
            _record_reader_language(event, event["editorial_card"])
        return events, {"status": "disabled", "requested": len(selected), "success_count": 0, "fallback_count": len(selected), "request_count": 0, "industry_brief_count": len(events) - len(mainstream), "errors": []}
    if generate is None:
        for event in selected:
            event["editorial_card"] = _fallback_editorial_card(event, "model_unavailable")
            _record_reader_language(event, event["editorial_card"])
        return events, {"status": "unavailable", "requested": len(selected), "success_count": 0, "fallback_count": len(selected), "request_count": 0, "industry_brief_count": len(events) - len(mainstream), "errors": ["model_unavailable"]}

    source_rows: list[dict[str, Any]] = []
    for event in selected:
        refs = event.get("source_refs") if isinstance(event.get("source_refs"), list) else []
        source = refs[0] if refs and isinstance(refs[0], dict) else {}
        source_rows.append(
            {
                "official_rank": int(event.get("official_rank") or 0),
                "title": _clean(event.get("title"), limit=160),
                "summary": _clean(event.get("summary"), limit=460),
                "importance_reasons": [_clean(item, limit=100) for item in (event.get("importance_reasons") or [])[:2]],
                "source_name": _clean(source.get("name"), limit=80),
                "published_at": _clean(event.get("published_at"), limit=32),
                "source_status": _clean(event.get("source_status"), limit=80),
                "event_freshness": event.get("event_freshness") if isinstance(event.get("event_freshness"), dict) else {},
                "audience_routing": event.get("audience_routing") if isinstance(event.get("audience_routing"), dict) else {},
            }
        )
    schema = '{"items":[{"official_rank":1,"title_zh":"","summary_zh":"","why_it_matters_zh":"","plain_explanation_zh":""}]}'
    system = (
        "你是谨慎的中文科技新闻编辑。只能把输入的一手公开来源或公开新闻索引的标题、摘要、日期和既有关注理由翻译或压缩成中文，"
        "不能联网、补充背景、推断因果、添加评价，或把来源归因写成已核验事实。英文产品名可以保留。"
        "不得新增任何数字、日期、人物、公司、产品、能力或未来结果。标题必须用主体加具体动作加对象写成完整新闻句，"
        "例如‘某公司发布某产品’或‘某机构启动某项目’，不能写成‘产品名：抽象变化’或只有名词的短语。每条只输出一个短标题、1至2句摘要和一个简短关注点。"
        "输入中的 event_freshness 只说明本次动作层级，source_published_at 仅是源页面日期，绝不能把它写成产品首次发布日期。"
        "new_platform_or_capability 必须使用‘开放、新增、更新’等动词，不能改写成产品首次上线；preview_or_plan 必须保留预告或计划状态。"
        "写给只想快速了解新闻的人，直接叙述事件。标题要先说谁做了什么，再说对象；不用冒号、破折号、口号、来源口吻或营销腔。"
        "不要让生态、基座、端侧、具身、评测基准、迁移这类术语主导标题；可用‘AI 助手’‘手机本地’‘机器人 AI’‘测试标准’等普通说法。"
        "不要写‘据输入摘要’‘来源显示’‘官方表示’或任何来源、核验、传播、模型处理话术。严格输出 JSON。"
    )
    # A twenty-four card request often returns a syntactically valid but
    # incomplete JSON tail.  Small independent batches make missing cards
    # observable and repairable without ever borrowing text from another item.
    indexed: dict[int, dict[str, Any]] = {}
    request_count = 0
    errors: list[str] = []
    batch_size = 8
    for start in range(0, len(source_rows), batch_size):
        batch = source_rows[start:start + batch_size]
        prompt = f"输出结构：{schema}\n每一项 official_rank 必须对应输入。\n输入：{json.dumps(batch, ensure_ascii=False)}"
        request_count += 1
        try:
            result = generate(system, prompt, min(2_000, max(256, int(max_output_tokens))))
        except Exception as exc:
            errors.append(f"batch_{start // batch_size + 1}:model_error:{type(exc).__name__}")
            continue
        items = result.get("items") if isinstance(result, dict) else None
        if not isinstance(items, list):
            errors.append(f"batch_{start // batch_size + 1}:invalid_items")
            continue
        expected_ranks = {int(row["official_rank"] or 0) for row in batch}
        for item in items:
            if not isinstance(item, dict) or not str(item.get("official_rank") or "").isdigit():
                continue
            rank = int(item["official_rank"])
            if rank in expected_ranks and rank not in indexed:
                indexed[rank] = item
    success = 0
    for event, row in zip(selected, source_rows):
        rank = int(event.get("official_rank") or 0)
        item = indexed.get(rank)
        source_numbers = set(_NUMBER.findall(json.dumps(row, ensure_ascii=False)))
        title = _valid_editorial_title((item or {}).get("title_zh"), source_numbers=source_numbers, event=event)
        summary = _valid_editorial_text((item or {}).get("summary_zh"), maximum=280, source_numbers=source_numbers)
        why = _valid_editorial_text((item or {}).get("why_it_matters_zh"), maximum=140, source_numbers=source_numbers)
        if title and summary and why:
            title, summary = _apply_freshness_language_guard(event, title, summary)
            event["editorial_card"] = {
                "status": "success",
                "locale": "zh-CN",
                "title": title,
                "summary": summary,
                "why_it_matters": why,
                "plain_explanation": _valid_editorial_text((item or {}).get("plain_explanation_zh"), maximum=220, source_numbers=source_numbers) or why,
                "source_bound_fields": ["title", "summary", "importance_reasons", "source_refs"],
            }
            _record_reader_language(event, event["editorial_card"])
            success += 1
        else:
            reason = "invalid_model_item" if item else "missing_model_item"
            event["editorial_card"] = _fallback_editorial_card(event, reason)
            _record_reader_language(event, event["editorial_card"])
            errors.append(f"rank_{rank}:{reason}")
    fallback = len(selected) - success
    return events, {"status": "success" if not fallback else "partial", "requested": len(selected), "success_count": success, "fallback_count": fallback, "request_count": request_count, "industry_brief_count": len(events) - len(mainstream), "errors": errors}


def render_major_event_brief(pack: dict[str, Any]) -> str:
    """Render a consumer-facing hot list; audit evidence stays in JSON artifacts."""
    events = pack.get("official_major_events") if isinstance(pack.get("official_major_events"), list) else []
    lines = ["# 科技热榜", ""]
    if not events:
        lines.extend(["暂无可展示的科技新闻。", ""])
    for event in events[:10]:
        card = event.get("editorial_card") if isinstance(event.get("editorial_card"), dict) else {}
        card_ok = card.get("status") == "success" and card.get("locale") == "zh-CN"
        if not card_ok:
            continue
        display_title = _clean(card.get("title"), limit=80)
        display_summary = _clean(card.get("summary"), limit=280)
        lines.extend(
            [
                f"## {int(event.get('official_rank') or 0)}. {display_title}",
                "",
                display_summary,
                "",
            ]
        )
    if len(lines) == 2:
        lines.extend(["暂无可展示的科技新闻。", ""])
    return "\n".join(lines)
