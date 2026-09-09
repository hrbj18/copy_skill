from __future__ import annotations

import copy
from pathlib import Path

from douyin_intelligence.config import load_config
from douyin_intelligence.daily_hot_candidate_pool import attach_official_douyin_signals
from douyin_intelligence.news_sources import NewsArticle
from douyin_intelligence.official_major_events import (
    build_official_major_events,
    enrich_public_event_details,
    fetch_official_sources,
    localize_official_event_cards,
    _parse_source,
    parse_html_listing,
    render_major_event_brief,
)


def _source() -> dict:
    return {
        "name": "World Labs Blog", "url": "https://www.worldlabs.ai/blog", "kind": "official",
        "format": "html_listing", "allowed_domains": ["www.worldlabs.ai"], "article_path_prefixes": ["/blog/"],
    }


def test_html_listing_extracts_date_bearing_same_origin_cards() -> None:
    page = b'''<main>
    <a href="/blog/atlas"><article><time>September 1, 2026</time><h2>Atlas: A World Model</h2><p>One architecture supports image, video, and 3D.</p></article></a>
    <a href="https://outside.example/article"><time>September 1, 2026</time><h2>Must not pass</h2></a>
    <a href="/blog/no-date"><h2>Must have a date</h2></a>
    </main>'''
    rows = parse_html_listing(page, _source(), "Asia/Shanghai")
    assert [(row.title, row.url, row.published_at[:10]) for row in rows] == [
        ("Atlas: A World Model", "https://www.worldlabs.ai/blog/atlas", "2026-09-01")
    ]


def test_official_events_keep_target_day_and_do_not_fuzzy_merge() -> None:
    articles = [
        NewsArticle("OpenAI Astra reaches a cybersecurity threshold", "https://openai.com/news/astra", "2026-09-01T00:00:00+08:00", "A new model capability disclosure.", "OpenAI News", "official", "openai.com"),
        NewsArticle("World Labs introduces Atlas", "https://www.worldlabs.ai/blog/atlas", "2026-09-01T00:00:00+08:00", "A world model for spatial intelligence.", "World Labs Blog", "official", "www.worldlabs.ai"),
        NewsArticle("清朗整治 AI 应用乱象", "https://www.cac.gov.cn/2026-09/02/example.htm", "2026-09-02T10:30:00+08:00", "第二阶段专项行动。", "中国网信网", "government", "www.cac.gov.cn"),
    ]
    events, excluded = build_official_major_events(articles, business_date="2026-09-01", maximum=10)
    assert [item["title"] for item in events] == ["OpenAI Astra reaches a cybersecurity threshold", "World Labs introduces Atlas"]
    assert all(item["source_status"] == "official_primary_source_attributed" for item in events)
    assert excluded == [{"title": "清朗整治 AI 应用乱象", "reason": "not_target_business_date"}]


def test_signal_search_is_capped_and_no_match_keeps_source_event(tmp_path: Path) -> None:
    config = copy.deepcopy(load_config())
    config["_project_root"] = str(tmp_path)
    articles = [
        NewsArticle(f"Named model event {index}", f"https://openai.com/news/{index}", "2026-09-01T00:00:00+08:00", "Official event summary.", "OpenAI News", "official", "openai.com")
        for index in range(5)
    ]
    events, _ = build_official_major_events(articles, business_date="2026-09-01", maximum=10)
    seen: dict[str, object] = {}

    def empty_collector(local: dict, budget: int, run_id: str, **kwargs: object) -> dict:
        seen.update({"budget": budget, "run_id": run_id, **kwargs})
        return {"status": "success", "files": []}

    attached, report, errors = attach_official_douyin_signals(events, config, config, business_date="2026-09-01", collection_key="fixture", collector=empty_collector)
    assert not errors and report["status"] == "empty"
    assert len(seen["keywords"]) == 5 and seen["budget"] == 25
    assert all(item["douyin_signal"]["status"] in {"not_found", "not_attempted"} for item in attached)
    assert all(item["douyin_signal"]["matched_video_count"] == 0 for item in attached)


def test_fetcher_errors_are_isolated_and_safe_source_rows_stay_available() -> None:
    config = copy.deepcopy(load_config())
    config["jobs"]["daily_hot_candidate_pool_v2"]["official_discovery"]["sources"] = [
        {"name": "Fixture Feed", "url": "https://openai.com/news/rss.xml", "kind": "official", "format": "feed", "allowed_domains": ["openai.com"], "enabled": True},
        {"name": "Fixture Broken", "url": "https://www.anthropic.com/news", "kind": "official", "format": "html_listing", "allowed_domains": ["www.anthropic.com"], "article_path_prefixes": ["/news/"], "enabled": True},
    ]

    class FakeFetcher:
        def __init__(self, settings: dict, budget: object) -> None:
            self.settings = settings

        def get(self, url: str, **kwargs: object) -> tuple[str, str, bytes]:
            if "anthropic" in url:
                raise RuntimeError("fixture unavailable")
            return url, "application/xml", b"<rss><channel><item><title>Astra release</title><link>https://openai.com/news/astra</link><pubDate>Tue, 01 Sep 2026 01:00:00 +0800</pubDate><description>Official announcement</description></item></channel></rss>"

        def close(self) -> None:
            pass

    rows, errors, usage = fetch_official_sources(config, fetcher_factory=FakeFetcher)
    assert len(rows) == 1 and rows[0].url == "https://openai.com/news/astra"
    assert errors == [{"source": "Fixture Broken", "error": "fixture unavailable"}]
    assert usage["request_count"] == 0  # The fixture fetcher intentionally bypasses network accounting.


def test_google_news_index_keeps_index_url_and_drops_non_event_noise() -> None:
    feed = '''<rss><channel>
    <item><title>科大讯飞开源两款端侧大模型 - 新京报</title><link>https://news.google.com/rss/articles/example</link><pubDate>Tue, 01 Sep 2026 13:05:00 +0800</pubDate><description>公开新闻索引</description></item>
    <item><title>大模型月度盘点 - 某媒体</title><link>https://news.google.com/rss/articles/noise</link><pubDate>Tue, 01 Sep 2026 13:05:00 +0800</pubDate><description>索引</description></item>
    </channel></rss>'''.encode("utf-8")
    source = {"name": "Google 大模型", "url": "https://news.google.com/rss/search?q=test", "kind": "news_index", "format": "google_news_rss", "allowed_domains": ["news.google.com"]}
    rows = _parse_source(feed, source, "Asia/Shanghai")
    assert rows[0].title == "科大讯飞开源两款端侧大模型"
    assert rows[0].source_name == "新京报" and rows[0].url.startswith("https://news.google.com/")
    events, excluded = build_official_major_events(rows, business_date="2026-09-01", maximum=20)
    assert len(events) == 1 and events[0]["source_status"] == "public_news_index_attributed"
    assert excluded == [{"title": "大模型月度盘点", "reason": "routine_maintenance_not_major_event"}]


def test_google_news_syndications_merge_without_merging_different_versions() -> None:
    rows = [
        NewsArticle("科大讯飞等研发的智能化工大模型3.0 Pro发布", "https://news.google.com/rss/articles/one", "2026-09-01T08:00:00+08:00", "模型发布。", "中国科学院", "news_index", "cas"),
        NewsArticle("智能化工大模型3.0 Pro发布", "https://news.google.com/rss/articles/two", "2026-09-01T09:00:00+08:00", "模型从问答走向执行。", "中国科技网", "news_index", "stdaily"),
        NewsArticle("智能化工大模型4.0 发布", "https://news.google.com/rss/articles/three", "2026-09-01T09:00:00+08:00", "新版本发布。", "另一媒体", "news_index", "other"),
    ]
    events, _ = build_official_major_events(rows, business_date="2026-09-01", maximum=20)
    assert len(events) == 2
    assert sorted(len(event["source_refs"]) for event in events) == [1, 2]


def test_company_priority_requires_a_major_action_and_keeps_base_importance_separate() -> None:
    settings = copy.deepcopy(load_config())["jobs"]["daily_hot_candidate_pool_v2"]["official_discovery"]["company_event_priority"]
    rows = [
        NewsArticle("OpenAI 发布 Orion 模型", "https://www.ithome.com/1.htm", "2026-09-02T08:00:00+08:00", "模型面向代码任务开放。", "IT之家 RSS", "media", "www.ithome.com"),
        NewsArticle("小鹏发布智能驾驶 VLA 升级", "https://www.ithome.com/2.htm", "2026-09-02T09:00:00+08:00", "智能驾驶系统增加连续路况理解能力。", "IT之家 RSS", "media", "www.ithome.com"),
        NewsArticle("华为发布 FreeBuds 耳机售价 999 元", "https://www.ithome.com/3.htm", "2026-09-02T10:00:00+08:00", "耳机提供常规音频更新。", "IT之家 RSS", "media", "www.ithome.com"),
        NewsArticle("某研究团队发布 Nexus 模型", "https://www.ithome.com/4.htm", "2026-09-02T11:00:00+08:00", "模型面向代码任务开放。", "IT之家 RSS", "media", "www.ithome.com"),
        NewsArticle("阿里更新 Qwen 模型", "https://www.ithome.com/5.htm", "2026-09-02T12:00:00+08:00", "摘要同时提到 Kimi 的价格比较，但不属于该事件标题。", "IT之家 RSS", "media", "www.ithome.com"),
        NewsArticle("元点机器人发布 OpenBridge 具身智能模型生态", "https://www.ithome.com/6.htm", "2026-09-02T13:00:00+08:00", "摘要把该项目比作谷歌安卓，但标题未指向谷歌。", "IT之家 RSS", "media", "www.ithome.com"),
        NewsArticle("博主吐槽车企发布 Qwen 模型", "https://example.com/7.htm", "2026-09-02T14:00:00+08:00", "这是一条第三方评论。", "测试一手源", "official", "example.com"),
    ]
    events, _ = build_official_major_events(rows, business_date="2026-09-02", maximum=10, company_priority=settings)
    by_title = {item["title"]: item for item in events}
    openai = by_title["OpenAI 发布 Orion 模型"]
    xpeng = by_title["小鹏发布智能驾驶 VLA 升级"]
    huawei = by_title["华为发布 FreeBuds 耳机售价 999 元"]
    ordinary = by_title["某研究团队发布 Nexus 模型"]
    qwen = by_title["阿里更新 Qwen 模型"]
    zeroth = by_title["元点机器人发布 OpenBridge 具身智能模型生态"]
    opinion = by_title["博主吐槽车企发布 Qwen 模型"]
    assert openai["company_event_priority"]["tier"] == "ai_core" and openai["company_event_priority"]["boost"] == 12
    assert xpeng["company_event_priority"]["tier"] == "industry_leaders" and xpeng["company_event_priority"]["boost"] == 6
    assert huawei["company_event_priority"]["status"] == "excluded_routine_product" and huawei["company_event_priority"]["boost"] == 0
    assert ordinary["company_event_priority"]["status"] == "unmatched" and ordinary["company_event_priority"]["boost"] == 0
    assert qwen["company_event_priority"]["status"] == "boosted" and qwen["company_event_priority"]["boost"] == 12
    assert zeroth["company_event_priority"]["status"] == "unmatched" and zeroth["company_event_priority"]["boost"] == 0
    assert opinion["company_event_priority"]["status"] == "matched_non_company_action" and opinion["company_event_priority"]["boost"] == 0
    assert openai["reader_delivery_score"] - openai["official_importance_score"] == 12
    assert xpeng["reader_delivery_score"] - xpeng["official_importance_score"] == 6
    assert huawei["reader_delivery_score"] == huawei["official_importance_score"]


def test_reader_policy_distinguishes_new_platform_research_and_preview() -> None:
    config = load_config()
    policy = config["jobs"]["daily_hot_candidate_pool_v2"]["official_discovery"]["reader_editorial"]
    rows = [
        NewsArticle(
            "腾讯WorkBuddy开放平台上线：首批伙伴接入",
            "https://www.ithome.com/workbuddy.htm",
            "2026-09-02T15:15:00+08:00",
            "WorkBuddy 向智能硬件、行业应用和开发者开放 AI 助手能力。",
            "IT之家 RSS", "media", "www.ithome.com",
        ),
        NewsArticle(
            "OPPO联合OpenKG推出首个端侧AI记忆评测基准MobileMem",
            "https://www.ithome.com/mobilemem.htm",
            "2026-09-02T11:05:00+08:00",
            "该测试标准用于比较手机里的 AI 记忆能力。",
            "IT之家 RSS", "media", "www.ithome.com",
        ),
        NewsArticle(
            "元点机器人预告发布OpenBridge具身智能开源生态",
            "https://www.ithome.com/openbridge.htm",
            "2026-09-02T18:15:00+08:00",
            "项目仍处于预告阶段。",
            "IT之家 RSS", "media", "www.ithome.com",
        ),
    ]
    events, _ = build_official_major_events(rows, business_date="2026-09-02", maximum=10, reader_editorial=policy)
    by_title = {item["title"]: item for item in events}
    workbuddy = by_title["腾讯WorkBuddy开放平台上线：首批伙伴接入"]
    mobilemem = by_title["OPPO联合OpenKG推出首个端侧AI记忆评测基准MobileMem"]
    openbridge = by_title["元点机器人预告发布OpenBridge具身智能开源生态"]
    assert workbuddy["event_freshness"]["classification"] == "new_platform_or_capability"
    assert workbuddy["event_freshness"]["event_first_release_status"] == "not_inferred_from_source_publication_date"
    assert workbuddy["audience_routing"]["lane"] == "mainstream"
    assert mobilemem["event_freshness"]["classification"] == "research_or_industry_infrastructure"
    assert mobilemem["audience_routing"]["lane"] == "industry_brief"
    assert openbridge["event_freshness"]["classification"] == "preview_or_plan"
    assert openbridge["audience_routing"]["lane"] == "industry_brief"


def test_reader_language_rejects_product_relaunch_wording_for_platform_update() -> None:
    config = load_config()
    policy = config["jobs"]["daily_hot_candidate_pool_v2"]["official_discovery"]["reader_editorial"]
    article = NewsArticle(
        "腾讯WorkBuddy开放平台上线",
        "https://www.ithome.com/workbuddy.htm",
        "2026-09-02T15:15:00+08:00",
        "WorkBuddy 面向智能硬件、行业应用和开发者开放 AI 助手能力。",
        "IT之家 RSS", "media", "www.ithome.com",
    )
    events, _ = build_official_major_events([article], business_date="2026-09-02", maximum=10, reader_editorial=policy)

    def incorrect_card(_: str, __: str, ___: int) -> dict:
        return {"items": [{"official_rank": 1, "title_zh": "腾讯上线 WorkBuddy 开放平台", "summary_zh": "腾讯向开发者开放了新的 AI 助手能力。", "why_it_matters_zh": "开发者可以接入相关能力。"}]}

    localized, report = localize_official_event_cards(events, enabled=True, maximum=10, max_output_tokens=1800, generate=incorrect_card)
    assert report["fallback_count"] == 1
    assert localized[0]["reader_language"]["writing_status"] == "fallback_safe"
    assert localized[0]["reader_language"]["title"] == "腾讯开放 WorkBuddy 开发者平台"
    assert "不能把它理解为 WorkBuddy 产品第一次上线" in localized[0]["reader_language"]["summary"]


def test_reader_language_keeps_platform_action_plain_and_source_bound() -> None:
    config = load_config()
    policy = config["jobs"]["daily_hot_candidate_pool_v2"]["official_discovery"]["reader_editorial"]
    article = NewsArticle(
        "腾讯WorkBuddy开放平台上线",
        "https://www.ithome.com/workbuddy.htm",
        "2026-09-02T15:15:00+08:00",
        "WorkBuddy 向智能硬件、行业应用和开发者开放 AI 助手能力。",
        "IT之家 RSS", "media", "www.ithome.com",
    )
    events, _ = build_official_major_events([article], business_date="2026-09-02", maximum=10, reader_editorial=policy)

    def plain_card(_: str, __: str, ___: int) -> dict:
        return {"items": [{"official_rank": 1, "title_zh": "腾讯开放 WorkBuddy 开发者平台", "summary_zh": "腾讯把 WorkBuddy 的 AI 助手能力开放给硬件厂商、行业应用和开发者。此次重点是新增开放能力，并非把 WorkBuddy 当作当天首次推出的新产品。", "why_it_matters_zh": "后续硬件和应用可以接入这项能力。", "plain_explanation_zh": "主要影响开发者和设备厂商，普通用户是否直接使用取决于后续产品。"}]}

    localized, report = localize_official_event_cards(events, enabled=True, maximum=10, max_output_tokens=1800, generate=plain_card)
    assert report["success_count"] == 1
    card = localized[0]["reader_language"]
    assert card["writing_status"] == "success"
    assert card["title"] == "腾讯开放 WorkBuddy 开发者平台"
    assert "生态" not in card["title"] and "基座" not in card["title"]
    assert "不能把它理解为产品第一次上线" in card["summary"]
    assert "当天首次推出" in card["summary"]


def test_public_event_detail_is_taken_only_from_allowlisted_redirect() -> None:
    config = copy.deepcopy(load_config())
    event = {
        "official_event_id": "public-1", "source_status": "public_news_index_attributed",
        "primary_source_url": "https://news.google.com/rss/articles/one", "summary": "新闻索引摘要。",
    }

    class FakeFetcher:
        def __init__(self, settings: dict, budget: object) -> None:
            self.settings = settings

        def get(self, _url: str, **_kwargs: object) -> tuple[str, str, bytes]:
            return "https://www.stdaily.com/article", "text/html", "<meta name=\"description\" content=\"科技日报提供的公共新闻详情文字，说明这项科技产品已经完成具体发布，并补充了面向开发者的使用场景和后续安排。\">".encode("utf-8")

        def close(self) -> None:
            pass

    enriched, report = enrich_public_event_details([event], config, fetcher_factory=FakeFetcher)
    assert report["success_count"] == 1 and report["failure_count"] == 0
    assert enriched[0]["detail_evidence"][0]["method"] == "allowlisted_article_meta"
    assert "公共新闻详情文字" in enriched[0]["summary"]


def test_major_event_brief_is_consumer_only_and_keeps_audit_fields_outside() -> None:
    article = NewsArticle("Astra release", "https://openai.com/news/astra", "2026-09-01T00:00:00+08:00", "Official summary.", "OpenAI News", "official", "openai.com")
    events, _ = build_official_major_events([article], business_date="2026-09-01", maximum=10)
    events[0]["douyin_signal"] = {"status": "not_found", "matched_video_count": 0, "raw_interactions": {}, "videos": []}
    events[0]["editorial_card"] = {"status": "success", "locale": "zh-CN", "title": "Astra 模型迎来安全更新", "summary": "OpenAI 为 Astra 增加了更强的网络安全防护措施。"}
    rendered = render_major_event_brief({"business_date": "2026-09-01", "official_major_events": events})
    assert "Astra 模型迎来安全更新" in rendered
    assert "OpenAI News" not in rendered and "https://" not in rendered
    assert "2026-09-01" not in rendered and "抖音" not in rendered and "来源" not in rendered
    assert events[0]["source_refs"][0]["url"] == "https://openai.com/news/astra"


def test_chinese_editorial_card_is_source_bound_and_brief_prefers_it() -> None:
    article = NewsArticle(
        "Astra 5.1 release", "https://openai.com/news/astra", "2026-09-01T00:00:00+08:00",
        "A model release for coding.", "OpenAI News", "official", "openai.com",
    )
    events, _ = build_official_major_events([article], business_date="2026-09-01", maximum=10)

    def chinese_card(_: str, __: str, ___: int) -> dict:
        return {"items": [{"official_rank": 1, "title_zh": "OpenAI 发布 Astra 5.1", "summary_zh": "Astra 5.1 面向编程场景带来一项模型更新。", "why_it_matters_zh": "模型能力与开放策略出现新变化。"}]}

    localized, report = localize_official_event_cards(events, enabled=True, maximum=10, max_output_tokens=1800, generate=chinese_card)
    assert report["status"] == "success" and report["success_count"] == 1
    assert localized[0]["editorial_card"]["locale"] == "zh-CN"
    rendered = render_major_event_brief({"business_date": "2026-09-01", "official_major_events": localized})
    assert "OpenAI 发布 Astra 5.1" in rendered
    assert "来源" not in rendered and "抖音" not in rendered and "据输入" not in rendered


def test_chinese_editorial_card_rejects_new_numbers_and_falls_back() -> None:
    article = NewsArticle("Astra 5.1 release", "https://openai.com/news/astra", "2026-09-01T00:00:00+08:00", "A model release.", "OpenAI News", "official", "openai.com")
    events, _ = build_official_major_events([article], business_date="2026-09-01", maximum=10)

    def invented_number(_: str, __: str, ___: int) -> dict:
        return {"items": [{"official_rank": 1, "title_zh": "Astra 6.0 发布", "summary_zh": "这是一项模型更新。", "why_it_matters_zh": "模型能力出现变化。"}]}

    localized, report = localize_official_event_cards(events, enabled=True, maximum=10, max_output_tokens=1800, generate=invented_number)
    assert report["status"] == "partial" and report["fallback_count"] == 1
    assert localized[0]["editorial_card"]["status"] == "fallback"


def test_chinese_editorial_card_rejects_source_meta_language() -> None:
    article = NewsArticle("Astra release", "https://openai.com/news/astra", "2026-09-01T00:00:00+08:00", "A model release.", "OpenAI News", "official", "openai.com")
    events, _ = build_official_major_events([article], business_date="2026-09-01", maximum=10)

    def meta_language(_: str, __: str, ___: int) -> dict:
        return {"items": [{"official_rank": 1, "title_zh": "Astra 模型发布", "summary_zh": "据输入摘要，这是一项模型更新。", "why_it_matters_zh": "模型能力出现变化。"}]}

    localized, report = localize_official_event_cards(events, enabled=True, maximum=10, max_output_tokens=1800, generate=meta_language)
    assert report["status"] == "partial" and localized[0]["editorial_card"]["status"] == "fallback"
    assert render_major_event_brief({"official_major_events": localized}) == "# 科技热榜\n\n暂无可展示的科技新闻。\n"
