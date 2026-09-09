from __future__ import annotations

import copy

from douyin_intelligence.news_semantics import (
    deterministic_semantics,
    finalize_story_semantics,
    semantic_counts,
    validate_model_semantics,
)


def _story(text: str, *, method: str = "title") -> dict:
    story = {
        "story_id": "story-fixture",
        "content_evidence": [{"video_id": "video-fixture", "method": method, "text": text}],
        "heat_score": 88.8,
        "heat_rank": 1,
        "delivery_rank": 1,
    }
    story.update(deterministic_semantics(story, story["content_evidence"]))
    return finalize_story_semantics(story)


def test_minimax_title_only_review_cannot_enter_news_lane() -> None:
    story = _story("MiniMax H3，还在进化，实测来了！ #本地部署 #大模型 #MiniMaxH3")
    assert story["content_type"] == "creator_review"
    assert story["news_readiness"] == "not_news"
    assert story["news_headline"] == ""
    assert story["event_slots"]["action"] == "实测"


def test_microduck_complete_description_preserves_actor_action_object_and_time() -> None:
    text = (
        "【开源预告】Microduck国内低成本复刻方案。陆吾智能正在准备一版使用国内平替舵机的方案，"
        "并修改硬件电路，整体硬件成本会比原版更低，目前资料还在整理，预计两周内发布。"
    )
    story = _story(text, method="platform_text")
    assert story["news_readiness"] == "ready"
    assert "陆吾智能" in story["event_slots"]["subject"]
    assert story["event_slots"]["action"] == "发布"
    assert "Microduck" in story["event_slots"]["object"]
    assert "两周" in story["event_slots"]["time_text"]
    assert all(value in story["news_headline"] for value in ("陆吾智能", "发布", "Microduck"))
    assert "方案预告" not in story["news_headline"]


def test_vague_tutorial_heat_is_retained_but_not_presented_as_news() -> None:
    story = _story("国产大模型教程引发AI圈热议")
    before = (story["heat_score"], story["heat_rank"], story["delivery_rank"])
    assert story["content_type"] == "tutorial"
    assert story["news_readiness"] == "not_news"
    assert story["news_headline"] == ""
    assert before == (88.8, 1, 1)


def test_model_slots_must_be_supported_by_evidence() -> None:
    story = _story("腾讯发布混元新模型", method="platform_text")
    valid = {
        "content_type": "news_lead",
        "event_slots": {
            "subject": "腾讯", "action": "发布", "object": "混元新模型", "time_text": "",
            "event_status": "released", "result_or_change": "",
        },
    }
    semantics, flags = validate_model_semantics(valid, story["content_evidence"])
    assert semantics is not None and not flags
    invented = copy.deepcopy(valid)
    invented["event_slots"]["subject"] = "不存在的公司"
    semantics, flags = validate_model_semantics(invented, story["content_evidence"])
    assert semantics is None and "unsupported_subject" in flags


def test_evidence_grounded_new_company_name_is_not_blocked_by_static_allowlist() -> None:
    story = _story("星河智造发布具身智能操作模型", method="public_news_snippet")
    candidate = {
        "content_type": "news_lead",
        "event_slots": {
            "subject": "星河智造", "action": "发布", "object": "具身智能操作模型", "time_text": "",
            "event_status": "released", "result_or_change": "",
        },
    }
    semantics, flags = validate_model_semantics(candidate, story["content_evidence"])
    assert semantics is not None and not flags


def test_semantic_counts_are_observable_and_do_not_reorder_stories() -> None:
    stories = [_story("腾讯发布混元新模型", method="platform_text"), _story("国产大模型教程引发AI圈热议")]
    order = [item["story_id"] for item in stories]
    counts = semantic_counts(stories)
    assert counts["news_ready"] == 1
    assert counts["news_readiness"] == {"not_news": 1, "ready": 1}
    assert [item["story_id"] for item in stories] == order


def test_named_product_road_test_is_specific_event_not_generic_review() -> None:
    story = _story("特斯拉Cybercab发布会前在奥斯汀进行夜间道路测试", method="platform_text")
    assert story["content_type"] == "news_lead"
    assert story["news_readiness"] == "ready"
    assert story["event_slots"]["subject"] == "特斯拉"
    assert story["event_slots"]["object"] == "Cybercab"
    assert story["event_slots"]["action"] == "测试"
    assert "发布Cybercab" not in story["news_headline"]


def test_multi_company_multi_date_price_war_is_routed_as_roundup() -> None:
    story = _story(
        "8月25日智谱GLM-5.3-Flash正式开源；8月26日阿里Qwen跟进发布；8月28日腾讯混元更新。",
        method="platform_text",
    )
    assert story["content_type"] == "roundup"
    assert story["news_readiness"] == "not_news"
    assert story["news_headline"] == ""


def test_object_extraction_stops_before_following_hashtag_terms() -> None:
    story = _story("高德开源的三维空间实现开源项目 #ai #vibecoding #大模型", method="title")
    assert story["news_readiness"] == "ready"
    assert story["event_slots"]["object"] == "三维空间实现开源项目"
    assert "vibecoding" not in story["news_headline"]


def test_model_cannot_mix_subject_and_object_into_brand_subject() -> None:
    story = _story("DeepSeek多模态模型现已开源", method="platform_text")
    mixed = {
        "content_type": "news_lead",
        "event_slots": {
            "subject": "DeepSeek多模态模型", "action": "开源", "object": "", "time_text": "",
            "event_status": "released", "result_or_change": "",
        },
    }
    semantics, flags = validate_model_semantics(mixed, story["content_evidence"])
    assert semantics is None and "implausible_subject" in flags


def test_ocr_incidental_news_cannot_promote_tutorial_source_intent() -> None:
    evidence = [
        {"video_id": "1", "method": "story_title", "text": "国产大模型教程引发AI圈热议"},
        {"video_id": "1", "method": "screen_ocr", "text": "华为上线GUIAgent大模型，页面还展示其他行业消息"},
    ]
    story = {"story_id": "tutorial-ocr", "content_evidence": evidence}
    story.update(deterministic_semantics(story, evidence))
    finalize_story_semantics(story)
    assert story["source_intent_type"] == "tutorial"
    assert story["content_type"] == "tutorial"
    assert story["news_readiness"] == "not_news"


def test_punctuation_or_generic_model_object_is_rejected() -> None:
    story = _story("DeepSeek多模态模型现已开源", method="platform_text")
    invalid = {
        "content_type": "news_lead",
        "event_slots": {
            "subject": "DeepSeek", "action": "开源", "object": "！ 模型", "time_text": "",
            "event_status": "released", "result_or_change": "",
        },
    }
    semantics, flags = validate_model_semantics(invalid, story["content_evidence"])
    assert semantics is None and "implausible_object" in flags


def test_deterministic_semantics_prefers_object_before_action_over_hashtag_after_action() -> None:
    story = _story("DeepSeek 多模态模型开源！ #模型 #ai #deepseek", method="title")
    assert story["event_slots"]["subject"] == "DeepSeek"
    assert story["event_slots"]["action"] == "开源"
    assert story["event_slots"]["object"] == "多模态模型"
    assert story["news_headline"] == "DeepSeek开源多模态模型"
    assert story["news_readiness"] == "ready"
