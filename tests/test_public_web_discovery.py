from __future__ import annotations

import copy
from datetime import datetime

from douyin_intelligence.config import load_config
from douyin_intelligence.news_sources import NewsArticle
from douyin_intelligence.official_major_events import build_official_major_events
from douyin_intelligence.public_web_discovery import (
    _candidate_exclusion_reason,
    build_public_web_candidate_pool,
    fetch_public_web_discovery,
    render_public_web_candidate_pool,
)


def test_public_web_queries_keep_search_provenance_and_isolate_failed_query() -> None:
    config = copy.deepcopy(load_config())
    config["jobs"]["daily_hot_candidate_pool_v2"]["public_web_discovery"]["queries"] = [
        {"id": "astra", "text": "OpenAI Astra 发布 when:3d"},
        {"id": "broken", "text": "Anthropic 模型 发布 when:3d"},
    ]
    config["jobs"]["daily_hot_candidate_pool_v2"]["public_web_discovery"]["max_queries"] = 2

    class FakeFetcher:
        def __init__(self, settings: dict, budget: object) -> None:
            self.settings = settings

        def get(self, url: str, **_kwargs: object) -> tuple[str, str, bytes]:
            if "Anthropic" in url:
                raise RuntimeError("fixture unavailable")
            payload = b"""<rss><channel><item>
            <title>OpenAI \xe5\x8f\x91\xe5\xb8\x83 Astra \xe6\xa8\xa1\xe5\x9e\x8b - \xe7\xa7\x91\xe6\x8a\x80\xe5\xaa\x92\xe4\xbd\x93</title>
            <link>https://news.google.com/rss/articles/astra</link>
            <pubDate>Tue, 02 Sep 2026 09:00:00 +0800</pubDate>
            <description>Astra \xe7\x9b\xb8\xe5\x85\xb3\xe5\x85\xac\xe5\xbc\x80\xe6\x8a\xa5\xe9\x81\x93</description>
            </item></channel></rss>"""
            return url, "application/rss+xml", payload

        def close(self) -> None:
            pass

    articles, by_url, report = fetch_public_web_discovery(
        config,
        fetcher_factory=FakeFetcher,
        now=lambda: datetime.fromisoformat("2026-09-03T02:00:00+08:00"),
    )
    assert len(articles) == 1
    assert report["success_count"] == 1 and report["failure_count"] == 1
    lead = by_url["https://news.google.com/rss/articles/astra"][0]
    assert lead["query_id"] == "astra"
    assert lead["source_category"] == "search_or_aggregator"
    assert lead["discovered_at"].startswith("2026-09-03T02:00:00")


def test_public_web_events_keep_lead_status_and_do_not_create_heat() -> None:
    url_one = "https://news.google.com/rss/articles/astra-one"
    url_two = "https://news.google.com/rss/articles/astra-two"
    rows = [
        NewsArticle("OpenAI 发布 Astra 模型", url_one, "2026-09-02T08:00:00+08:00", "模型能力更新。", "科技媒体甲", "news_index", "media-a"),
        NewsArticle("OpenAI 发布 Astra 模型并披露安全能力", url_two, "2026-09-02T09:00:00+08:00", "公开报道补充安全信息。", "科技媒体乙", "news_index", "media-b"),
    ]
    discovery = {
        url_one: [{"query_id": "astra", "query_text": "OpenAI Astra", "result_url": url_one, "source_category": "search_or_aggregator"}],
        url_two: [{"query_id": "ai_security", "query_text": "AI 安全", "result_url": url_two, "source_category": "search_or_aggregator"}],
    }
    events, excluded = build_official_major_events(rows, business_date="2026-09-02", maximum=20, public_web_discovery_by_url=discovery)
    assert not excluded and len(events) == 1
    event = events[0]
    assert len(event["public_web_discovery"]) == 2
    assert event["observed_heat_status"] == "unknown"
    assert event["candidate_status"] == "lead_only"
    assert event["douyin_signal"]["matched_video_count"] == 0


def test_public_web_candidate_pool_separates_selection_from_observed_douyin_attention() -> None:
    source_url = "https://news.google.com/rss/articles/astra"
    rows = [NewsArticle("OpenAI 发布 Astra 模型", source_url, "2026-09-02T08:00:00+08:00", "Astra 面向网络安全任务更新能力。", "科技媒体", "news_index", "media-a")]
    events, _ = build_official_major_events(
        rows,
        business_date="2026-09-02",
        maximum=20,
        public_web_discovery_by_url={source_url: [{"query_id": "astra", "query_text": "OpenAI Astra", "result_url": source_url, "source_category": "search_or_aggregator"}]},
    )
    event = events[0]
    event["detail_evidence"] = [{"method": "allowlisted_article_meta", "text": "一篇允许域名文章详情，说明 OpenAI 对 Astra 的网络安全能力进行了公开披露。", "url": "https://www.stdaily.com/astra"}]
    event["reader_language"] = {"writing_status": "success", "title": "OpenAI 公布 Astra 的安全能力", "summary": "OpenAI 披露 Astra 在网络安全任务上的新能力，具体开放范围仍要以正式说明为准。"}
    event["douyin_signal"] = {"status": "found", "matched_video_count": 1, "raw_interactions": {"like": 30, "comment": 5, "collect": 2, "share": 1}, "videos": []}
    original_heat = {"heat_score": 999.0, "heat_rank": 1}
    event.update(original_heat)

    pool = build_public_web_candidate_pool(events, business_date="2026-09-02", target_count=1)
    candidate = pool["candidates"][0]
    assert pool["status"] == "success"
    assert candidate["selection_basis"] == "source_strength_company_impact_public_relevance_not_web_heat"
    assert candidate["candidate_status"] == "detail_backed"
    assert candidate["observed_heat_status"] == "douyin_observed"
    assert candidate["douyin_attention_rank"] == 1
    assert candidate["discovery_audit"]["origin"] == "public_web_search"
    assert candidate["discovery_audit"]["query_leads"][0]["query_id"] == "astra"
    assert event["heat_score"] == original_heat["heat_score"] and event["heat_rank"] == original_heat["heat_rank"]
    rendered = render_public_web_candidate_pool(pool)
    assert "https://" not in rendered and "已获详情支持" in rendered


def test_public_web_candidate_pool_excludes_routine_changelog_but_keeps_machine_event() -> None:
    rows = [
        NewsArticle("Copilot code review can now approve pull requests", "https://github.blog/changelog/copilot", "2026-09-02T08:00:00+08:00", "A routine repository administration update.", "GitHub Changelog", "official", "github.blog"),
        NewsArticle("ATV Big Air Tour turned 3 days of work into 3 hours with ChatGPT", "https://openai.com/customer/atv", "2026-09-02T08:30:00+08:00", "A customer uses ChatGPT in its workflow.", "OpenAI News", "official", "openai.com"),
        NewsArticle("OpenAI 发布 Astra 模型", "https://news.google.com/rss/articles/astra", "2026-09-02T09:00:00+08:00", "Astra 模型带来网络安全能力更新。", "科技媒体", "news_index", "media-a"),
    ]
    events, _ = build_official_major_events(rows, business_date="2026-09-02", maximum=20)
    pool = build_public_web_candidate_pool(events, business_date="2026-09-02", target_count=1)
    assert pool["selected_count"] == 1
    assert all("Copilot code review" not in row["title"] for row in pool["candidates"])
    assert any(row["reason"] == "routine_developer_changelog" for row in pool["excluded"])
    assert any(row["reason"] == "enterprise_case_or_administration" for row in pool["excluded"])
    github_event = next(item for item in events if "Copilot code review" in item["title"])
    assert github_event["public_web_candidate_export"]["status"] == "excluded"


def test_public_web_candidate_summary_uses_first_concrete_action_sentence() -> None:
    article = NewsArticle(
        "支付宝上线物业缴费专属入口",
        "https://www.ithome.com/alipay.htm",
        "2026-09-02T10:00:00+08:00",
        "今日，某行业服务发布大会举办。支付宝生活缴费同步上线物业缴费专属入口，用户可查询账单并授权自动代扣。该服务后续还会继续完善。",
        "IT之家 RSS",
        "media",
        "www.ithome.com",
    )
    events, _ = build_official_major_events([article], business_date="2026-09-02", maximum=20)
    events[0]["event_category"] = "consumer_product"
    pool = build_public_web_candidate_pool(events, business_date="2026-09-02", target_count=1)
    assert pool["candidates"][0]["summary"].startswith("支付宝生活缴费同步上线物业缴费专属入口")
    assert "发布大会举办" not in pool["candidates"][0]["summary"]


def test_public_web_candidate_pool_rejects_slogan_only_lead_and_strips_publisher_tail() -> None:
    rows = [
        NewsArticle(
            "智驾强制国标来了！",
            "https://news.google.com/rss/articles/standard",
            "2026-09-02T10:00:00+08:00",
            "智驾强制国标来了！",
            "科技媒体",
            "news_index",
            "media-a",
        ),
        NewsArticle(
            "特斯拉发布自动驾驶辅助更新 新浪财经",
            "https://news.google.com/rss/articles/tesla",
            "2026-09-02T11:00:00+08:00",
            "特斯拉发布自动驾驶辅助更新 新浪财经",
            "科技媒体",
            "news_index",
            "media-a",
        ),
    ]
    assert _candidate_exclusion_reason({"title": "智驾强制国标来了！", "summary": "智驾强制国标来了！"}) == "missing_minimum_event_detail"
    events, _ = build_official_major_events(rows, business_date="2026-09-02", maximum=20)
    pool = build_public_web_candidate_pool(events, business_date="2026-09-02", target_count=10)
    assert all("智驾强制国标" not in row["title"] for row in pool["candidates"])
    assert pool["candidates"][0]["title"] == "特斯拉发布自动驾驶辅助更新"
    assert pool["candidates"][0]["discovery_audit"]["origin"] == "configured_public_source"
    assert pool["candidates"][0]["discovery_audit"]["source_refs"][0]["url"].startswith("https://news.google.com/")


def test_public_web_candidate_pool_does_not_treat_l2_l4_as_an_actor_or_drop_named_model_release() -> None:
    rows = [
        NewsArticle("智驾强制国标来了！L2-L4智能驾驶进入合规高速路", "https://news.google.com/rss/articles/standard", "2026-09-02T10:00:00+08:00", "智驾强制国标来了！", "科技媒体", "news_index", "media-a"),
        NewsArticle("Claude Fable 5.1发布，价格降低", "https://news.google.com/rss/articles/fable", "2026-09-02T11:00:00+08:00", "Claude Fable 5.1发布新模型，价格降低。", "科技媒体", "news_index", "media-a"),
    ]
    events, _ = build_official_major_events(rows, business_date="2026-09-02", maximum=20)
    fable = next(item for item in events if "Fable" in item["title"])
    fable["event_category"] = "technology_update"
    pool = build_public_web_candidate_pool(events, business_date="2026-09-02", target_count=10)
    assert all("智驾强制国标" not in row["title"] for row in pool["candidates"])
    assert any("Fable" in row["title"] for row in pool["candidates"])


def test_public_web_candidate_pool_consolidates_same_entity_product_and_retains_audit() -> None:
    rows = [
        NewsArticle("OpenAI Astra模型即将发布", "https://news.google.com/rss/articles/astra-one", "2026-09-02T10:00:00+08:00", "OpenAI Astra模型即将发布。", "媒体甲", "news_index", "media-a"),
        NewsArticle("OpenAI即将发布Astra安全模型", "https://news.google.com/rss/articles/astra-two", "2026-09-02T11:00:00+08:00", "OpenAI即将发布Astra安全模型。", "媒体乙", "news_index", "media-b"),
    ]
    events, _ = build_official_major_events(rows, business_date="2026-09-02", maximum=20)
    # The upstream merger is intentionally conservative; candidate export adds
    # a second, named-entity-only consolidation barrier.
    if len(events) == 1:
        events.append({**events[0], "official_event_id": "astra-duplicate", "title": "OpenAI即将发布Astra安全模型", "summary": "OpenAI即将发布Astra安全模型。", "source_refs": [{"url": "https://news.google.com/rss/articles/astra-two"}], "public_web_discovery": [{"query_id": "astra", "result_url": "https://news.google.com/rss/articles/astra-two"}]})
    pool = build_public_web_candidate_pool(events, business_date="2026-09-02", target_count=10)
    assert pool["candidate_count"] == 1
    candidate = pool["candidates"][0]
    assert len(candidate["discovery_audit"]["source_refs"]) >= 2
