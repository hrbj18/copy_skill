from __future__ import annotations

import copy

from douyin_intelligence.editorial_priority import filter_technology_scope, prioritize_for_delivery


SETTINGS = {
    "weights": {
        "heat": 0.45,
        "strategic_significance": 0.25,
        "public_relevance": 0.20,
        "discussion_value": 0.10,
    }
}


def _event(event_id: str, title: str, heat: float, rank: int) -> dict:
    return {
        "event_id": event_id,
        "story_id": event_id,
        "title": title,
        "aliases": [],
        "matched_keywords": [],
        "contributing_videos": [{"title": title}],
        "heat_score": heat,
        "rank": rank,
    }


def test_game_scope_excludes_pure_game_but_keeps_metaphor() -> None:
    events = [
        _event("steam", "Steam 九月游戏推荐", 100, 1),
        _event("switch", "Switch 新游试玩评测", 90, 2),
        _event("rule", "新模型将改变游戏规则", 80, 3),
        _event("model", "腾讯混元大模型开源", 70, 4),
    ]
    eligible, exclusions = filter_technology_scope(events)
    assert [row["event_id"] for row in eligible] == ["rule", "model"]
    assert {row["story_id"] for row in exclusions} == {"steam", "switch"}
    assert [row["heat_rank"] for row in eligible] == [1, 2]
    assert [row["source_heat_rank"] for row in eligible] == [3, 4]


def test_ascii_game_term_uses_word_boundaries() -> None:
    events = [
        _event("tool", "Skills SwitchTool 项目级 Agent 管理工具", 100, 1),
        _event("game", "Switch 九月新游推荐", 90, 2),
    ]
    eligible, exclusions = filter_technology_scope(events)
    assert [row["event_id"] for row in eligible] == ["tool"]
    assert [row["story_id"] for row in exclusions] == ["game"]


def test_single_game_mention_does_not_exclude_mixed_technology_roundup() -> None:
    event = _event("roundup", "AI Agent 开源仓库盘点", 100, 1)
    event["contributing_videos"] = [
        {"title": "AI Agent 开源仓库盘点，其中一个项目能生成 3D 游戏"},
        {"title": "OpenClaw 与开发者工具更新"},
        {"title": "GitHub AI 工具今日榜单"},
    ]
    eligible, exclusions = filter_technology_scope([event])
    assert not exclusions and eligible[0]["event_id"] == "roundup"


def test_majority_game_cluster_is_still_excluded() -> None:
    event = _event("games", "九月内容盘点", 100, 1)
    event["contributing_videos"] = [
        {"title": "Switch 九月新游推荐"},
        {"title": "Steam 解谜游戏试玩"},
        {"title": "本月科技新品"},
    ]
    eligible, exclusions = filter_technology_scope([event])
    assert not eligible and exclusions[0]["story_id"] == "games"


def test_polluted_legacy_alias_does_not_change_scope_or_priority() -> None:
    event = _event("phone", "华为手机系统更新引发用户讨论", 80, 1)
    event["aliases"] = ["Steam 游戏评测 三块钱手把手教程"]
    eligible, exclusions = filter_technology_scope([event])
    assert not exclusions and eligible[0]["event_id"] == "phone"
    ranked = prioritize_for_delivery(eligible, SETTINGS)
    assert ranked[0]["priority_components"]["penalty_total"] == 0


def test_discovery_keyword_does_not_turn_unrelated_item_into_model_news() -> None:
    event = _event("print", "3D 打印解压玩具免费开源", 80, 1)
    event["matched_keywords"] = ["开源大模型"]
    eligible, _ = filter_technology_scope([event])
    ranked = prioritize_for_delivery(eligible, SETTINGS)
    assert ranked[0]["content_category"] != "major_model_development"


def test_tutorial_word_in_a_complaint_is_not_tutorial_promotion() -> None:
    event = _event("phone", "连夜搜教程退回 华为手机强制更新引发用户质疑", 80, 1)
    eligible, _ = filter_technology_scope([event])
    ranked = prioritize_for_delivery(eligible, SETTINGS)
    assert ranked[0]["content_category"] != "tutorial_or_promotion"
    assert ranked[0]["priority_components"]["penalties"]["tutorial_promotion"] == 0


def test_delivery_priority_balances_heat_significance_and_public_relevance() -> None:
    events = [
        _event("lunar", "我国地月双向高速激光通信完成基础研究", 100, 1),
        _event("model", "腾讯混元旗舰大模型正式开源 普通用户可本地运行", 88, 2),
        _event("consumer", "手机系统更新 普通用户免费体验 AI 办公工具", 84, 3),
    ]
    eligible, _ = filter_technology_scope(events)
    ranked = prioritize_for_delivery(eligible, SETTINGS)
    assert ranked[0]["event_id"] == "model"
    assert ranked[0]["delivery_rank"] == ranked[0]["rank"] == 1
    assert next(row for row in ranked if row["event_id"] == "lunar")["heat_rank"] == 1
    assert all("priority_components" in row and "content_category" in row for row in ranked)


def test_delivery_priority_penalizes_rumor_tutorial_and_vague_items() -> None:
    events = [
        _event("release", "国际科技大厂发布开源大模型 普通用户免费体验", 80, 1),
        _event("rumor", "震惊爆料 OpenAI 秘密 AI 文明传闻", 100, 2),
        _event("tutorial", "三块钱手把手教程 训练你自己的 AI", 95, 3),
        _event("vague", "科技圈三件事正在悄悄改变", 90, 4),
    ]
    eligible, _ = filter_technology_scope(events)
    ranked = prioritize_for_delivery(eligible, SETTINGS)
    assert ranked[0]["event_id"] == "release"
    by_id = {row["event_id"]: row for row in ranked}
    assert by_id["rumor"]["priority_components"]["penalties"]["risk"] == 24
    assert by_id["tutorial"]["priority_components"]["penalties"]["tutorial_promotion"] == 18
    assert by_id["vague"]["priority_components"]["penalties"]["low_specificity"] == 8


def test_delivery_priority_is_stable_and_ignores_enrichment_truth_and_images() -> None:
    events = [
        _event("a", "腾讯发布大模型 开源免费体验", 80, 1),
        _event("b", "人形机器人新品发布", 70, 2),
    ]
    eligible, _ = filter_technology_scope(events)
    first = prioritize_for_delivery(eligible, SETTINGS)
    altered = copy.deepcopy(list(reversed(eligible)))
    altered[0].update(truth_status="verified", images=[{"ignored": True}], event_summary="模型改写内容", human_score=999)
    second = prioritize_for_delivery(altered, SETTINGS)
    assert [(row["event_id"], row["delivery_rank"], row["delivery_priority_score"], row["heat_score"], row["heat_rank"]) for row in first] == [
        (row["event_id"], row["delivery_rank"], row["delivery_priority_score"], row["heat_score"], row["heat_rank"]) for row in second
    ]
