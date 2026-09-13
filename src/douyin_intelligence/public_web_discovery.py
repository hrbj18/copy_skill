from __future__ import annotations

"""Bounded public-web event discovery for the daily technology candidate pool.

This module deliberately treats search results as attributable *leads*.  It
does not authenticate claims, calculate web popularity, scrape arbitrary result
pages, or make a search engine result equivalent to a publisher article.
"""

from datetime import datetime
import re
import time
from typing import Any, Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .material_probe import RequestBudget, SafeFetcher, _safe_error
from .news_sources import NewsArticle
from .official_major_events import _clean, _parse_source


_DEFAULT_ENDPOINT = "https://news.google.com/rss/search"
_GENERIC_SUBJECT = re.compile(r"^(?:汽车行业|行业内|多家企业|某(?:公司|厂商|团队)|媒体|市场)")
_LATIN_OWNER = re.compile(r"[A-Z][A-Za-z0-9-]{2,}")
_CHINESE_OWNER_ACTION = re.compile(r"^[\u4e00-\u9fff]{2,12}(?:发布|开源|上线|推出|升级|更新|开放|量产|交付|获批|融资|收购|启动|建成|投产|接入|首发|测试)")
_CHINESE_OWNER_SUFFIX = re.compile(r"[\u4e00-\u9fff]{2,12}(?:科技|汽车|自动驾驶|机器人|工软|影像|鸿蒙|芯片|算力)")
_KNOWN_OWNER_TERMS = ("特斯拉", "比亚迪", "滴滴", "讯飞", "华为", "腾讯", "阿里", "百度", "小米", "苹果", "魅族", "影眸", "Claude", "Anthropic")
_NON_ENTITY_WORDS = frozenset({"发布", "模型", "最新", "最强", "能力", "价格", "发布会", "公司", "系统", "平台", "升级", "更新", "推出", "上线", "即将", "新闻", "科技"})


def _query_url(endpoint: str, query: str) -> str:
    """Build the only supported public search URL without carrying credentials."""
    values = {"q": query, "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"}
    return f"{endpoint}?{urlencode(values)}"


def fetch_public_web_discovery(
    config: dict[str, Any],
    *,
    fetcher_factory: Callable[[dict[str, Any], RequestBudget], SafeFetcher] = SafeFetcher,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] | None = None,
) -> tuple[list[NewsArticle], dict[str, list[dict[str, str]]], dict[str, Any]]:
    """Fetch configured Google News RSS queries under one shared finite budget.

    The URL in each returned article remains the Google News index URL.  The
    result can later earn article detail only through the existing allowlisted
    redirect/detail flow.  A failed individual query is retained in the report
    but never prevents already parsed query rows from being returned.
    """
    settings = config["jobs"]["daily_hot_candidate_pool_v2"]["public_web_discovery"]
    timezone = str(config["timezone"])
    endpoint = str(settings.get("endpoint") or _DEFAULT_ENDPOINT).rstrip("?")
    budget = RequestBudget(
        max_requests=int(settings["max_queries"]),
        max_total_bytes=int(settings["max_total_bytes"]),
        total_timeout_seconds=float(settings["total_timeout_seconds"]),
        started_at=clock(),
    )
    fetcher = fetcher_factory(
        {
            "request_timeout_seconds": int(settings["request_timeout_seconds"]),
            "max_redirects": 0,
            "fake_ip_networks": list(settings.get("fake_ip_networks") or []),
            "allowed_domains": ["news.google.com"],
        },
        budget,
    )
    discovered_at = (now or (lambda: datetime.now(ZoneInfo(timezone))))().astimezone(ZoneInfo(timezone)).isoformat(timespec="seconds")
    articles: list[NewsArticle] = []
    by_url: dict[str, list[dict[str, str]]] = {}
    query_reports: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    try:
        for query in [item for item in settings.get("queries") or [] if isinstance(item, dict)][: int(settings["max_queries"])]:
            query_id = _clean(query.get("id"), limit=80)
            query_text = _clean(query.get("text"), limit=180)
            if not query_id or not query_text:
                continue
            url = _query_url(endpoint, query_text)
            source = {
                "name": f"Google News 探索：{query_id}",
                "url": url,
                "kind": "news_index",
                "format": "google_news_rss",
                "allowed_domains": ["news.google.com"],
            }
            try:
                _, _, payload = fetcher.get(
                    url,
                    maximum_bytes=int(settings["max_response_bytes"]),
                    accepted_types=("application/xml", "text/xml", "application/rss+xml", "application/atom+xml"),
                )
                rows = _parse_source(payload, source, timezone)[: int(settings["max_results_per_query"])]
                retained = 0
                for article in rows:
                    if not article.url or article.url in seen_urls:
                        continue
                    seen_urls.add(article.url)
                    lead = {
                        "query_id": query_id,
                        "query_text": query_text,
                        "result_url": article.url,
                        "result_title": _clean(article.title, limit=240),
                        "result_published_at": _clean(article.published_at, limit=48),
                        "publisher": _clean(article.source_name, limit=120),
                        "publisher_domain": _clean(article.source_domain, limit=120),
                        "source_category": "search_or_aggregator",
                        "discovered_at": discovered_at,
                    }
                    by_url.setdefault(article.url, []).append(lead)
                    articles.append(article)
                    retained += 1
                query_reports.append({"query_id": query_id, "query_text": query_text, "status": "success", "parsed_count": len(rows), "retained_count": retained})
            except Exception as exc:
                query_reports.append({"query_id": query_id, "query_text": query_text, "status": "failed", "parsed_count": 0, "retained_count": 0, "error": _safe_error(exc)})
    finally:
        fetcher.close()
    failures = [item for item in query_reports if item["status"] != "success"]
    return articles, by_url, {
        "provider": "google_news_rss",
        "query_count": len(query_reports),
        "success_count": len(query_reports) - len(failures),
        "failure_count": len(failures),
        "discovered_article_count": len(articles),
        "queries": query_reports,
        "usage": budget.snapshot(),
    }


def refresh_public_event_provenance(event: dict[str, Any]) -> dict[str, Any]:
    """Derive auditable lead state without altering any Douyin ranking field."""
    refs = event.get("source_refs") if isinstance(event.get("source_refs"), list) else []
    leads = event.get("public_web_discovery") if isinstance(event.get("public_web_discovery"), list) else []
    category_counts = {
        "first_party": 0,
        "reputable_media": 0,
        "search_or_aggregator": 0,
        "social_or_unverified": 0,
        "unknown": 0,
    }
    domains: set[str] = set()
    first_party = False
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        kind = str(ref.get("kind") or "")
        domain = _clean(ref.get("domain"), limit=120).casefold()
        if domain:
            domains.add(domain)
        if kind in {"official", "primary", "authority", "government"}:
            category_counts["first_party"] += 1
            first_party = True
        elif kind == "media":
            category_counts["reputable_media"] += 1
        elif kind == "news_index":
            category_counts["search_or_aggregator"] += 1
        else:
            category_counts["unknown"] += 1
    if leads:
        category_counts["search_or_aggregator"] = max(category_counts["search_or_aggregator"], len(leads))
    details = event.get("detail_evidence") if isinstance(event.get("detail_evidence"), list) else []
    direct_source = str(event.get("primary_source_kind") or "") in {"official", "primary", "authority", "government", "media"}
    detail_backed = bool(details) or direct_source
    if detail_backed:
        candidate_status = "detail_backed"
    elif len(leads) > 1 or len(domains) > 1:
        candidate_status = "needs_more_sources"
    else:
        candidate_status = "lead_only"
    signal = event.get("douyin_signal") if isinstance(event.get("douyin_signal"), dict) else {}
    observed = signal.get("status") == "found" and int(signal.get("matched_video_count") or 0) > 0
    channels = ["configured_source"]
    if leads:
        channels.append("public_web_search")
    if observed:
        channels.append("douyin_attention")
    event["discovery_channels"] = channels
    event["source_strength"] = {
        "source_category_counts": category_counts,
        "distinct_source_domains": len(domains),
        "allowlisted_detail_count": len(details),
        "first_party_present": first_party,
        "detail_backed": detail_backed,
    }
    event["observed_heat_status"] = "douyin_observed" if observed else "unknown"
    event["candidate_status"] = candidate_status
    return event


def _candidate_text(event: dict[str, Any]) -> tuple[str, str]:
    language = event.get("reader_language") if isinstance(event.get("reader_language"), dict) else {}
    source_summary = _compact_candidate_summary(event.get("summary"))
    if language.get("writing_status") in {"success", "fallback_safe"}:
        title = _clean_candidate_title(language.get("title"))
        summary = _compact_candidate_summary(language.get("summary"))
        if title and _has_concrete_action(summary):
            return title, summary
        if title and source_summary:
            return title, source_summary
    return _clean_candidate_title(event.get("title")), source_summary


def _clean_candidate_title(value: Any) -> str:
    """Remove publisher tails without rewriting the source event claim."""
    title = _clean(value, limit=140)
    if not title:
        return ""
    title = re.sub(
        r"(?:\s*[-—|｜]\s*|\s+)(?:[\u4e00-\u9fff]{2,12}(?:网|报|财经|科技|媒体|财富)|[A-Za-z0-9.-]+\.com|36\s*Kr|IT之家|新浪(?:网|财经)?|财联社)$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    return _clean(title, limit=120)


def _compact_candidate_summary(value: Any) -> str:
    """Keep the handoff readable while the full detail stays in event audit."""
    text = _clean(value, limit=900)
    if not text:
        return ""
    sentences = [item.strip() for item in re.split(r"(?<=[。！？!?])", text) if item.strip()]
    for sentence in sentences:
        if _has_target_action(sentence):
            return _clean_candidate_title(sentence)
    return _clean_candidate_title(sentences[0] if sentences else text)


def _has_concrete_action(value: str) -> bool:
    text = _clean(value, limit=220)
    if text.startswith(("这次", "本次", "相关内容", "现有信息")):
        return False
    return _has_target_action(text)


def _has_target_action(value: str) -> bool:
    text = _clean(value, limit=240).replace("发布大会", "").replace("发布会", "")
    return any(term in text for term in ("发布", "开源", "上线", "推出", "更新", "升级", "开放", "量产", "交付", "获批", "启动", "接入", "测试", "融资", "扩大"))


def _has_named_actor(value: str) -> bool:
    """Recognise a company/product actor, never a technical version such as L2-L4."""
    text = _clean(value, limit=300)
    latin_owners = [match for match in _LATIN_OWNER.findall(text) if not re.fullmatch(r"L\d+(?:-L\d+)?", match, flags=re.IGNORECASE)]
    return bool(latin_owners) or bool(_CHINESE_OWNER_ACTION.search(text)) or bool(_CHINESE_OWNER_SUFFIX.search(text)) or any(term in text for term in _KNOWN_OWNER_TERMS)


def _candidate_exclusion_reason(event: dict[str, Any]) -> str | None:
    """Keep routine software administration and commentary out of the OP pool.

    This only controls the new reader-facing selection export.  The original
    official-event record, its source URL and its source classification remain
    in the machine package for audit.
    """
    title = _clean(event.get("title"), limit=300)
    text = f"{title} {_clean(event.get('summary'), limit=700)}".casefold()
    refs = event.get("source_refs") if isinstance(event.get("source_refs"), list) else []
    source_names = " ".join(_clean(item.get("name"), limit=120).casefold() for item in refs if isinstance(item, dict))
    priority = event.get("company_event_priority") if isinstance(event.get("company_event_priority"), dict) else {}
    category = _clean(event.get("event_category"), limit=80)
    named_owner = _has_named_actor(title)
    priority_owner = int(priority.get("boost") or 0) > 0
    impactful_technology = any(term in text for term in ("模型", "model", "芯片", "chip", "机器人", "自动驾驶", "无人驾驶", "人工智能", "ai"))
    named_model_release = named_owner and _has_target_action(title) and any(term in text for term in ("模型", "model", "claude", "fable", "gemini", "qwen", "glm", "grok", "deepseek"))
    if "github changelog" in source_names:
        return "routine_developer_changelog"
    if any(term in text for term in ("case study", "customer story", "workflows into operating", "turned 3 days of work", "uses chatgpt", "discussion comments", "pull requests", "user budgets", "live migrations", "落地样板间", "按结果领工资", "第二曲线")):
        return "enterprise_case_or_administration"
    if any(term in title for term in ("研报", "周报", "盘点", "见面会", "交流活动", "来广州", "体验", "展会", "现场")):
        return "commentary_or_event_promotion"
    if any(term in title for term in ("被曝", "传", "有望", "或成", "呼吁", "吐槽", "怎么看", "不甘落后", "可惜")):
        return "commentary_or_unconfirmed_prediction"
    if _GENERIC_SUBJECT.search(title):
        return "missing_specific_event_subject"
    summary = _compact_candidate_summary(event.get("summary"))
    if _is_slogan_only_lead(title, summary):
        return "missing_minimum_event_detail"
    if category == "technology_update" and not (((named_owner or priority_owner) and impactful_technology) or named_model_release):
        return "routine_technology_update"
    if str(event.get("primary_source_kind") or "") == "news_index" and not isinstance(event.get("detail_evidence"), list):
        policy_exception = category == "policy_governance"
        if not policy_exception and not named_owner and not priority_owner:
            return "index_lead_missing_named_subject"
    return None


def _is_slogan_only_lead(title: str, summary: str) -> bool:
    """Reject a headline that supplies neither actor nor concrete event detail.

    A broad candidate may be single-source, but it must still tell an editor
    what happened.  This avoids exporting lines such as \"智驾强制国标来了\".
    """
    normalized_title = re.sub(r"[\s，。！？!?：:、‘’'\"“”]", "", _clean_candidate_title(title))
    normalized_summary = re.sub(r"[\s，。！？!?：:、‘’'\"“”]", "", _clean_candidate_title(summary))
    if len(normalized_title) < 12 and normalized_summary == normalized_title:
        return True
    generic_slogan = any(term in normalized_title for term in ("来了", "重磅", "大爆发", "引关注"))
    has_actor = _has_named_actor(title)
    return generic_slogan and not has_actor


def _candidate_discovery_audit(event: dict[str, Any]) -> dict[str, Any]:
    """Expose one explicit provenance shape for every candidate.

    Search-origin events retain their query rows.  Events discovered directly
    from a configured feed keep the source entry which led to them.  Both are
    discovery evidence, not factual verification or popularity evidence.
    """
    query_leads = event.get("public_web_discovery") if isinstance(event.get("public_web_discovery"), list) else []
    source_refs = event.get("source_refs") if isinstance(event.get("source_refs"), list) else []
    return {
        "origin": "public_web_search" if query_leads else "configured_public_source",
        "query_leads": query_leads,
        "source_refs": source_refs,
        "audit_status": "discovery_only_not_fact_verification",
    }


def _candidate_entity_anchors(value: str) -> set[str]:
    """Return stable named anchors used only for safe duplicate consolidation."""
    text = _clean(value, limit=500)
    anchors = {
        token.casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9.-]{2,}", text)
        if token.casefold() not in _NON_ENTITY_WORDS and not re.fullmatch(r"l\d+(?:-l\d+)?", token, flags=re.IGNORECASE)
    }
    anchors.update(term for term in _KNOWN_OWNER_TERMS if term in text)
    return anchors


def _same_candidate_event(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Merge only when two separately sourced rows share two named anchors."""
    shared = _candidate_entity_anchors(str(left.get("title") or "")) & _candidate_entity_anchors(str(right.get("title") or ""))
    return len(shared) >= 2


def _unique_dict_rows(rows: list[Any]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        marker = repr(sorted((str(key), repr(value)) for key, value in item.items()))
        if marker not in seen:
            seen.add(marker)
            unique.append(item)
    return unique


def _merge_duplicate_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Consolidate duplicate public leads while preserving every audit row."""
    merged: list[dict[str, Any]] = []
    for row in rows:
        existing = next((item for item in merged if _same_candidate_event(item, row)), None)
        if existing is None:
            merged.append(row)
            continue
        existing["source_refs"] = _unique_dict_rows([*(existing.get("source_refs") or []), *(row.get("source_refs") or [])])
        existing["public_web_discovery"] = _unique_dict_rows([*(existing.get("public_web_discovery") or []), *(row.get("public_web_discovery") or [])])
        audit = existing.get("discovery_audit") if isinstance(existing.get("discovery_audit"), dict) else {}
        incoming_audit = row.get("discovery_audit") if isinstance(row.get("discovery_audit"), dict) else {}
        audit["query_leads"] = _unique_dict_rows([*(audit.get("query_leads") or []), *(incoming_audit.get("query_leads") or [])])
        audit["source_refs"] = _unique_dict_rows([*(audit.get("source_refs") or []), *(incoming_audit.get("source_refs") or [])])
        audit["origin"] = "public_web_search" if audit["query_leads"] else "configured_public_source"
        audit["audit_status"] = "discovery_only_not_fact_verification"
        existing["discovery_audit"] = audit
        existing["selection_priority"] = max(int(existing["selection_priority"]), int(row["selection_priority"]))
        statuses = {str(existing.get("candidate_status") or ""), str(row.get("candidate_status") or "")}
        existing["candidate_status"] = next((item for item in ("detail_backed", "needs_more_sources", "lead_only") if item in statuses), existing["candidate_status"])
    return merged


def build_public_web_candidate_pool(events: list[dict[str, Any]], *, business_date: str, target_count: int) -> dict[str, Any]:
    """Build a wider selection pool with an explicitly non-heat ranking."""
    rows: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for event in events:
        refresh_public_event_provenance(event)
        exclusion_reason = _candidate_exclusion_reason(event)
        if exclusion_reason:
            event["public_web_candidate_export"] = {"status": "excluded", "reason": exclusion_reason}
            excluded.append({"event_id": _clean(event.get("official_event_id"), limit=100), "title": _clean(event.get("title"), limit=180), "reason": exclusion_reason})
            continue
        title, summary = _candidate_text(event)
        if not title or not summary:
            event["public_web_candidate_export"] = {"status": "excluded", "reason": "missing_reader_text"}
            excluded.append({"event_id": _clean(event.get("official_event_id"), limit=100), "title": _clean(event.get("title"), limit=180), "reason": "missing_reader_text"})
            continue
        strength = event.get("source_strength") if isinstance(event.get("source_strength"), dict) else {}
        category_counts = strength.get("source_category_counts") if isinstance(strength.get("source_category_counts"), dict) else {}
        routing = event.get("audience_routing") if isinstance(event.get("audience_routing"), dict) else {}
        company_priority = event.get("company_event_priority") if isinstance(event.get("company_event_priority"), dict) else {}
        signal = event.get("douyin_signal") if isinstance(event.get("douyin_signal"), dict) else {}
        interactions = signal.get("raw_interactions") if isinstance(signal.get("raw_interactions"), dict) else {}
        attention_score = (
            int(interactions.get("like") or 0)
            + int(interactions.get("comment") or 0) * 2
            + int(interactions.get("collect") or 0) * 3
            + int(interactions.get("share") or 0) * 4
        )
        priority = (
            36 * int(bool(strength.get("detail_backed")))
            + 18 * int(bool(strength.get("first_party_present")))
            + 8 * min(2, int(strength.get("distinct_source_domains") or 0))
            + min(12, int(company_priority.get("boost") or 0))
            + 10 * int(routing.get("lane") == "mainstream")
            + 4 * min(2, int(category_counts.get("reputable_media") or 0))
        )
        candidate = {
                "event_id": _clean(event.get("official_event_id"), limit=100),
                "title": title,
                "summary": summary,
                "selection_priority": priority,
                "selection_basis": "source_strength_company_impact_public_relevance_not_web_heat",
                "candidate_status": _clean(event.get("candidate_status"), limit=80),
                "observed_heat_status": _clean(event.get("observed_heat_status"), limit=80) or "unknown",
                "douyin_attention_rank": None,
                "douyin_attention_score": attention_score,
                "event_freshness": event.get("event_freshness") if isinstance(event.get("event_freshness"), dict) else {},
                "audience_routing": routing,
                "source_strength": strength,
                "public_web_discovery": event.get("public_web_discovery") if isinstance(event.get("public_web_discovery"), list) else [],
                "source_refs": refs if (refs := event.get("source_refs")) and isinstance(refs, list) else [],
                "discovery_audit": _candidate_discovery_audit(event),
                "truth_status": _clean(event.get("truth_status"), limit=40) or "not_checked",
        }
        event["public_web_candidate_export"] = {"status": "included", "selection_priority": priority}
        rows.append(candidate)
    rows = _merge_duplicate_candidates(rows)
    rows.sort(key=lambda item: (-int(item["selection_priority"]), str(item["event_id"])))
    for ordinal, row in enumerate(rows, 1):
        row["selection_rank"] = ordinal
    observed_rows = [row for row in rows if row["observed_heat_status"] == "douyin_observed"]
    for ordinal, row in enumerate(sorted(observed_rows, key=lambda item: (-int(item["douyin_attention_score"]), str(item["event_id"]))), 1):
        row["douyin_attention_rank"] = ordinal
    selected = rows[: max(0, int(target_count))]
    return {
        "business_date": business_date,
        "target_count": int(target_count),
        "candidate_count": len(rows),
        "selected_count": len(selected),
        "status": "success" if len(selected) >= int(target_count) else "partial",
        "selection_rule": "source_strength_company_impact_public_relevance_not_web_heat",
        "disclaimer": "公开网络结果用于发现和归因，不等于事实核验或平台真实热度。",
        "candidates": selected,
        "excluded_count": len(excluded),
        "excluded": excluded,
    }


def render_public_web_candidate_pool(pool: dict[str, Any]) -> str:
    """Render a concise lead list while keeping URLs and audit metadata in JSON."""
    candidates = pool.get("candidates") if isinstance(pool.get("candidates"), list) else []
    lines = ["# 全网科技选题候选池", "", f"本次找到 {len(candidates)} 条可审计候选，目标 {int(pool.get('target_count') or 0)} 条。", "", "本列表按选题优先级排列，不把公开网页搜索结果称为真实热度。", ""]
    if not candidates:
        return "\n".join(lines + ["暂无可审计的公开网络候选。", ""])
    labels = {
        "detail_backed": "已获详情支持",
        "needs_more_sources": "需要补充来源",
        "lead_only": "单条线索待补充",
    }
    for ordinal, row in enumerate(candidates, 1):
        title = _clean(row.get("title"), limit=100)
        summary = _clean(row.get("summary"), limit=320)
        status = labels.get(str(row.get("candidate_status") or ""), "来源状态待确认")
        lines.extend([f"## {ordinal}. {title}", "", summary, "", f"线索状态：{status}", ""])
    return "\n".join(lines)
