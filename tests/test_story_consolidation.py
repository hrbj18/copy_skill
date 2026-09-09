from __future__ import annotations

from copy import deepcopy

from douyin_intelligence.story_consolidation import (
    REL_DIFFERENT,
    REL_RELATED,
    REL_SAME,
    build_event_signature,
    classify_story_relation,
    consolidate_story_videos,
)


def _row(video_id: str, title: str, *, keyword: str = "") -> dict:
    return {
        "video_id": video_id,
        "title": title,
        "account_id": f"account-{video_id}",
        "source_group_id": "",
        "matched_keywords": [keyword] if keyword else [],
        "interactions": {"like": 1, "comment": 0, "collect": 0, "share": 0},
    }


def test_known_cross_topic_false_merges_are_blocked_even_with_same_search_keyword() -> None:
    rows = [
        _row("1", "自研芯片+小米汽车+AI大模型，小米全面发展，到底有多强？", keyword="大模型发布"),
        _row("2", "阿里智谱同日发新品，大模型价格战升级，Qwen 3.8 与 GLM 5.3 降价", keyword="大模型发布"),
        _row("3", "9月15号V27开源", keyword="开源大模型"),
        _row("4", "腾讯混元 Hy4 Preview 发布并开源", keyword="开源大模型"),
    ]
    clusters = consolidate_story_videos(rows, "2026-08-30")
    assert len(clusters) == 4
    assert all(len(cluster["videos"]) == 1 for cluster in clusters)
    relation = classify_story_relation(build_event_signature(rows[2]["title"]), build_event_signature(rows[3]["title"]))
    assert relation["relation"] == REL_DIFFERENT
    assert relation["reason"] == "conflicting_model_anchors"


def test_infinite_release_reaction_and_hands_on_are_one_story_with_all_provenance() -> None:
    rows = [
        _row("11", "【Reaction】网易开放世界游戏的代表作 #游戏 #无限大 #无限大定档 #reaction"),
        _row("12", "《无限大》2027年1月15日全球上线，终于官宣定档 #无限大定档"),
        _row("13", "试玩无限大：不测就上真的行？缺点要命优点吹爆！ #无限大 #无限大测试"),
        _row("14", "无限大终于定档啦，期待了这么久 #无限大 #无限大游戏"),
    ]
    clusters = consolidate_story_videos(rows, "2026-08-30")
    assert len(clusters) == 1
    story = clusters[0]
    assert {row["video_id"] for row in story["videos"]} == {"11", "12", "13", "14"}
    assert "无限大" in story["event_signature"]["identity_anchors"]
    assert set(story["event_signature"]["actions"]) >= {"定档", "试玩", "上线"}


def test_story_id_is_independent_of_input_order_interactions_and_search_keywords() -> None:
    rows = [
        _row("21", "腾讯混元 Hy4 Preview 正式开源", keyword="开源大模型"),
        _row("22", "Hy4 Preview 发布后实测 #腾讯Hy4 #AI编程", keyword="AI新品"),
    ]
    first = consolidate_story_videos(rows, "2026-08-30")[0]
    changed = deepcopy(list(reversed(rows)))
    changed[0]["interactions"]["like"] = 999999
    changed[0]["matched_keywords"] = ["完全不同的搜索入口"]
    second = consolidate_story_videos(changed, "2026-08-30")[0]
    assert first["story_id"] == second["story_id"]


def test_non_chaining_membership_does_not_merge_conflicting_endpoints() -> None:
    rows = [
        _row("31", "腾讯 Hy4 模型发布 #腾讯AI"),
        _row("32", "腾讯新模型发布进展 #腾讯AI"),
        _row("33", "腾讯 Hy5 模型发布 #腾讯AI"),
    ]
    clusters = consolidate_story_videos(rows, "2026-08-30")
    assert len(clusters) == 2
    assert sorted(len(cluster["videos"]) for cluster in clusters) == [1, 2]
    left = build_event_signature("腾讯 Hy4 模型发布")
    right = build_event_signature("腾讯 Hy5 模型发布")
    assert classify_story_relation(left, right)["relation"] == REL_DIFFERENT


def test_same_company_without_shared_product_is_related_or_different_never_same() -> None:
    relation = classify_story_relation(
        build_event_signature("腾讯混元 Hy4 Preview 开源"),
        build_event_signature("腾讯元宝全新功能上线"),
    )
    assert relation["relation"] in {REL_RELATED, REL_DIFFERENT}
    assert relation["relation"] != REL_SAME


def test_generic_github_codex_and_deepseek_do_not_merge_distinct_stories() -> None:
    rows = [
        _row("41", "科研文献 Skill 两天内开源 #Codex #科研工具 #github"),
        _row("42", "Skills SwitchTool 项目级 Agent Skills 与 MCP 管理工具 #github项目"),
        _row("43", "GitHub 9月1日日榜 TOP3：open-seo、crawl4ai、LiveKit Agents"),
        _row("44", "小龙虾 OpenClaw 2.0 来了 #科技 #模型 #deepseek"),
        _row("45", "DeepSeek 多模态模型开源 #模型 #deepseek"),
        _row("46", "国产大模型价格战升级 #智谱 #deepseek"),
    ]
    clusters = consolidate_story_videos(rows, "2026-09-01")
    assert len(clusters) == len(rows)
    assert all(len(cluster["videos"]) == 1 for cluster in clusters)


def test_multi_item_roundups_need_more_than_one_overlapping_project() -> None:
    left = build_event_signature("GitHub 日榜：open-seo、crawl4ai、LiveKit Agents")
    right = build_event_signature("Agent 工具盘点：open-seo、OpenMAIC、archify")
    relation = classify_story_relation(left, right)
    assert relation["relation"] == REL_RELATED
    assert relation["reason"] == "single_item_overlap_in_multi_item_topic"


def test_specific_version_anchor_still_merges_same_openclaw_story() -> None:
    rows = [
        _row("51", "OpenClaw 2.0 正式发布，AI Agent 能力升级"),
        _row("52", "小龙虾 OpenClaw 2.0 实测体验"),
    ]
    clusters = consolidate_story_videos(rows, "2026-09-01")
    assert len(clusters) == 1
    assert {row["video_id"] for row in clusters[0]["videos"]} == {"51", "52"}


def test_broad_vibecoding_tag_does_not_merge_unrelated_projects() -> None:
    rows = [
        _row("61", "高德开源三维空间项目 #vibecoding #高德 #开源"),
        _row("62", "开源项目让你实现大模型自由 #vibecoding #大模型"),
    ]
    clusters = consolidate_story_videos(rows, "2026-09-01")
    assert len(clusters) == 2


def test_broad_skill_hashtag_does_not_merge_unrelated_tools() -> None:
    rows = [
        _row("65", "一键复刻大厂 UI 的开源项目 #skill #人工智能"),
        _row("66", "一行命令复刻任意网站 #skill #网站 #JavaPub"),
    ]
    clusters = consolidate_story_videos(rows, "2026-09-01")
    assert len(clusters) == 2


def test_same_entity_date_and_specific_phrase_merge_without_campaign_tags() -> None:
    rows = [
        _row("71", "苹果完成权力交接，约翰・特努斯 9月1日正式出任CEO #媒体原创"),
        _row("72", "美国苹果公司9月1日起正式进入特努斯时代 #媒体精选计划"),
    ]
    clusters = consolidate_story_videos(rows, "2026-09-01")
    assert len(clusters) == 1
    decision = classify_story_relation(build_event_signature(rows[0]["title"]), build_event_signature(rows[1]["title"]))
    assert decision["reason"] == "shared_entity_date_specific_phrase"
