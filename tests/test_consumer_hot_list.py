from __future__ import annotations

from douyin_intelligence.consumer_hot_list import build_consumer_hot_list, build_public_reader_hot_list, render_consumer_hot_list


def _story(story_id: str, rank: int, *, title: str, detailed: bool = True) -> dict:
    evidence = [{
        "video_id": f"video-{story_id}",
        "method": "platform_text" if detailed else "title",
        "text": "腾讯开源混元新模型，支持代码生成和长文档处理，团队同时补充了部署方式与适用场景。" if detailed else title,
    }]
    return {
        "story_id": story_id,
        "heat_rank": rank,
        "title": title,
        "canonical_title": title,
        "event_summary": "腾讯开源混元新模型，新增代码生成和长文档处理能力，并补充了部署方式与适用场景。",
        "key_points": ["新增代码生成", "支持长文档处理"],
        "event_slots": {
            "subject": "腾讯", "action": "开源", "object": "混元新模型",
            "result_or_change": "新增代码生成和长文档处理能力",
        },
        "content_evidence": evidence,
    }


def test_consumer_cards_use_fact_cards_and_render_in_heat_order() -> None:
    stories = [
        _story("later", 2, title="混元新模型开源了吗"),
        _story("first", 1, title="混元新模型开源了吗"),
    ]

    def generate(_system: str, _prompt: str, _tokens: int) -> dict:
        return {"items": [
            {"story_id": "later", "consumer_title": "腾讯开源混元新模型", "consumer_summary": "混元新模型加入代码生成和长文档处理能力，并补充部署方式与适用场景。"},
            {"story_id": "first", "consumer_title": "腾讯开源混元新模型", "consumer_summary": "混元新模型加入代码生成和长文档处理能力，并补充部署方式与适用场景。"},
        ]}

    enriched, report = build_consumer_hot_list(stories, maximum=2, batch_size=10, max_output_tokens=800, generate=generate, deadline=10**12)
    assert report["status"] == "success" and report["consumer_card_count"] == 2
    assert all(row["consumer_fact_card"]["status"] == "ready" for row in enriched)
    rendered = render_consumer_hot_list({"candidates": enriched, "consumer_hot_list": report})
    assert rendered.index("## 1.") < rendered.index("## 2.")
    assert "https://" not in rendered and "热度" not in rendered and "来源" not in rendered
    assert "混元新模型开源了吗" not in rendered


def test_title_only_story_is_not_padded_to_reach_target() -> None:
    stories = [_story("weak", 1, title="4GB显存跑70B", detailed=False)]
    calls: list[str] = []

    def generate(*_args: object) -> dict:
        calls.append("called")
        return {"items": []}

    enriched, report = build_consumer_hot_list(stories, maximum=1, batch_size=10, max_output_tokens=800, generate=generate, deadline=10**12)
    assert calls == []
    assert report["status"] == "partial" and report["missing_count"] == 1
    assert enriched[0]["consumer_fact_card"]["reasons"] == ["title_only_evidence"]
    assert render_consumer_hot_list({"candidates": enriched, "consumer_hot_list": report}) == "# 每日科技热榜\n\n暂无具备完整内容证据的热点。\n"


def test_consumer_writer_cannot_reuse_hook_or_add_numbers() -> None:
    stories = [_story("one", 1, title="腾讯开源混元新模型")]

    def generate(_system: str, _prompt: str, _tokens: int) -> dict:
        return {"items": [{
            "story_id": "one", "consumer_title": "腾讯开源混元新模型", "consumer_summary": "混元新模型性能提升999倍，并补充部署方式与适用场景。",
        }]}

    enriched, report = build_consumer_hot_list(stories, maximum=1, batch_size=10, max_output_tokens=800, generate=generate, deadline=10**12)
    assert report["consumer_card_count"] == 0 and report["rejected_items"] >= 1
    assert enriched[0]["consumer_card"]["reason"] in {"raw_hook_title_reused", "unsupported_number"}


def test_consumer_cards_skip_hot_ineligible_hook_before_taking_twenty() -> None:
    stories = [
        _story("hook", 1, title="一个钩子标题", detailed=False),
        _story("qualified", 2, title="混元新模型开源了吗"),
    ]

    def generate(_system: str, _prompt: str, _tokens: int) -> dict:
        return {"items": [{
            "story_id": "qualified", "consumer_title": "腾讯开源混元新模型", "consumer_summary": "混元新模型加入代码生成和长文档处理能力，并补充部署方式与适用场景。",
        }]}

    enriched, report = build_consumer_hot_list(stories, maximum=1, batch_size=10, max_output_tokens=800, generate=generate, deadline=10**12)
    assert report["screened_count"] == 2 and report["selected_count"] == 1
    assert report["status"] == "success"
    assert enriched[0]["consumer_card"]["status"] == "insufficient"
    rendered = render_consumer_hot_list({"candidates": enriched, "consumer_hot_list": report})
    assert "腾讯开源混元新模型" in rendered


def test_public_reader_hot_list_renders_twenty_clean_cards_in_signal_order() -> None:
    events = []
    for index in range(21):
        events.append({
            "official_event_id": f"event-{index}", "official_importance_score": 80 - index,
            "truth_status": "not_checked",
            "source_refs": [{"url": f"https://news.google.com/rss/articles/{index}"}],
            "douyin_signal": {"raw_interactions": {"like": index, "comment": 0, "collect": 0, "share": 0}},
            "editorial_card": {"status": "success", "locale": "zh-CN", "title": f"公司{index}发布模型{index}", "summary": f"公司{index}发布模型{index}，面向具体技术场景提供新功能。"},
        })
    report = build_public_reader_hot_list(events, maximum=20)
    assert report["status"] == "success" and report["card_count"] == 20
    assert report["cards"][0]["event_id"] == "event-20"
    rendered = render_consumer_hot_list({"public_reader_hot_list": report})
    assert rendered.count("## ") == 20
    assert "https://" not in rendered and "来源" not in rendered and "热度" not in rendered and "抖音" not in rendered


def test_public_reader_hot_list_uses_company_delivery_only_after_douyin_signal() -> None:
    events = [
        {
            "official_event_id": "priority-company", "official_importance_score": 80, "reader_delivery_score": 92,
            "company_event_priority": {"status": "boosted", "tier": "ai_core", "boost": 12},
            "truth_status": "not_checked", "douyin_signal": {"raw_interactions": {"like": 0, "comment": 0, "collect": 0, "share": 0}},
            "editorial_card": {"status": "success", "locale": "zh-CN", "title": "OpenAI 发布新模型", "summary": "该模型面向具体开发任务开放。"},
        },
        {
            "official_event_id": "higher-source", "official_importance_score": 88, "reader_delivery_score": 88,
            "company_event_priority": {"status": "unmatched", "boost": 0},
            "truth_status": "not_checked", "douyin_signal": {"raw_interactions": {"like": 0, "comment": 0, "collect": 0, "share": 0}},
            "editorial_card": {"status": "success", "locale": "zh-CN", "title": "研究团队发布新模型", "summary": "该模型面向具体开发任务开放。"},
        },
        {
            "official_event_id": "signal-first", "official_importance_score": 70, "reader_delivery_score": 70,
            "company_event_priority": {"status": "unmatched", "boost": 0},
            "truth_status": "not_checked", "douyin_signal": {"raw_interactions": {"like": 1, "comment": 0, "collect": 0, "share": 0}},
            "editorial_card": {"status": "success", "locale": "zh-CN", "title": "团队发布机器人平台", "summary": "该平台面向机器人开发场景开放。"},
        },
    ]
    report = build_public_reader_hot_list(events, maximum=3)
    assert [row["event_id"] for row in report["cards"]] == ["signal-first", "priority-company", "higher-source"]
    assert report["cards"][1]["ranking_basis"] == "company_event_fallback"
    assert report["cards"][0]["ranking_basis"] == "douyin_signal"


def test_public_reader_hot_list_keeps_industry_events_out_of_mainstream_cards() -> None:
    events = [
        {
            "official_event_id": "mainstream", "official_importance_score": 80,
            "truth_status": "not_checked", "source_status": "official_primary_source_attributed",
            "audience_routing": {"lane": "mainstream", "reason": "consumer_impact"},
            "event_freshness": {"classification": "new_platform_or_capability"},
            "reader_language": {"writing_status": "success", "title": "腾讯开放 WorkBuddy 开发者平台", "summary": "腾讯向硬件厂商和开发者开放 AI 助手能力。", "plain_explanation": "这次是开放能力，不等于产品首次推出。"},
            "douyin_signal": {"raw_interactions": {"like": 0, "comment": 0, "collect": 0, "share": 0}},
        },
        {
            "official_event_id": "industry", "official_importance_score": 99,
            "truth_status": "not_checked", "source_status": "official_primary_source_attributed",
            "audience_routing": {"lane": "industry_brief", "reason": "research_standard"},
            "event_freshness": {"classification": "research_or_industry_infrastructure"},
            "reader_language": {"writing_status": "success", "title": "OPPO 发布手机 AI 记忆测试标准", "summary": "该标准用于比较技术方案。", "plain_explanation": "不是普通用户直接可用的新功能。"},
            "douyin_signal": {"raw_interactions": {"like": 100, "comment": 0, "collect": 0, "share": 0}},
        },
    ]
    report = build_public_reader_hot_list(events, maximum=20)
    assert report["card_count"] == 1 and report["industry_brief_count"] == 1
    assert report["cards"][0]["event_id"] == "mainstream"
    assert "测试标准" not in render_consumer_hot_list({"public_reader_hot_list": report})


def test_public_reader_hot_list_accepts_only_explicitly_safe_deterministic_fallback() -> None:
    event = {
        "official_event_id": "safe-fallback", "official_importance_score": 80,
        "truth_status": "not_checked", "source_status": "official_primary_source_attributed",
        "audience_routing": {"lane": "mainstream", "reason": "consumer_impact"},
        "event_freshness": {"classification": "new_platform_or_capability"},
        "reader_language": {"writing_status": "fallback_safe", "title": "腾讯开放 WorkBuddy 开发者平台", "summary": "这次新增的是开放能力，不能把它理解为 WorkBuddy 产品第一次上线。", "plain_explanation": "面向设备厂商和开发者。"},
        "douyin_signal": {"raw_interactions": {"like": 0, "comment": 0, "collect": 0, "share": 0}},
    }
    report = build_public_reader_hot_list([event], maximum=1)
    assert report["card_count"] == 1
    assert "不能把它理解为 WorkBuddy 产品第一次上线" in render_consumer_hot_list({"public_reader_hot_list": report})
