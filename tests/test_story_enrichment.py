from __future__ import annotations

import copy
import json
from pathlib import Path

from douyin_intelligence.config import load_config
from douyin_intelligence.story_enrichment import RawContentIndex, enrich_ranked_stories, select_representative_videos


def _video(video_id: str, title: str, *, likes: int, account: str, group: str = "") -> dict:
    return {
        "video_id": video_id, "title": title, "author": account, "account_id": account,
        "published_at": "2026-08-30T12:00:00+08:00", "source_group_id": group,
        "interactions": {"like": likes, "comment": 0, "collect": 0, "share": 0},
    }


def _story(videos: list[dict]) -> dict:
    return {
        "story_id": "story-ab12cd34", "event_id": "story-ab12cd34", "title": videos[0]["title"],
        "canonical_title": videos[0]["title"], "content_angles": ["发布"], "contributing_videos": videos,
        "heat_score": 90.0, "rank": 1,
    }


def _index(tmp_path: Path, rows: list[dict]) -> RawContentIndex:
    raw = tmp_path / "raw.jsonl"
    raw.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    index = RawContentIndex()
    index.capture_files([raw])
    return index


def _config(tmp_path: Path) -> dict:
    config = copy.deepcopy(load_config())
    config["_project_root"] = str(tmp_path)
    config["jobs"]["daily_hot_candidate_pool_v2"]["story_enrichment"].update({
        "max_detailed_stories": 2, "max_selected_videos": 3, "max_supporting_videos": 1,
        "max_media_videos": 1, "llm_max_batches": 1, "llm_batch_size": 2,
    })
    return config


def test_representative_is_highest_heat_and_supporting_source_only_fills_missing_text(tmp_path: Path) -> None:
    videos = [
        _video("1001", "短标题", likes=100, account="a", group="matrix"),
        _video("1002", "低热但正文完整", likes=10, account="b", group="independent"),
        _video("1003", "同矩阵正文不能浪费第二份预算", likes=50, account="c", group="matrix"),
    ]
    index = _index(tmp_path, [
        {"aweme_id": "1001", "title": "短标题"},
        {"aweme_id": "1002", "title": "低热但正文完整", "caption": "这是一段足够长的发布文案，包含事件背景、产品动作和多个可供后续归纳的内容要点。" * 3},
        {"aweme_id": "1003", "title": "同矩阵正文不能浪费第二份预算", "caption": "同矩阵长正文" * 30},
    ])
    selected = select_representative_videos(_story(videos), index, minimum_chars=80, allow_supporting=True)
    assert [item["video"]["video_id"] for item in selected] == ["1001", "1002"]
    assert selected[1]["selection_reason"] == "primary_text_insufficient_supporting_source"


def test_representative_adds_cross_video_detail_when_primary_is_already_complete(tmp_path: Path) -> None:
    videos = [
        _video("1101", "腾讯开源模型", likes=100, account="a", group="a"),
        _video("1102", "腾讯模型补充说明", likes=20, account="b", group="b"),
        _video("1103", "第三个账号补充说明", likes=10, account="c", group="c"),
    ]
    index = _index(tmp_path, [
        {"aweme_id": "1101", "title": "腾讯开源模型", "caption": "腾讯开源混元模型，支持代码生成和长文档处理，并补充部署方式与适用场景。" * 3},
        {"aweme_id": "1102", "title": "腾讯模型补充说明", "caption": "腾讯开源混元模型后补充介绍了代码生成、长文档处理和部署方式。" * 3},
        {"aweme_id": "1103", "title": "第三个账号补充说明", "caption": "腾讯开源混元模型后补充了模型的适用场景、部署方法和功能说明。" * 3},
    ])
    selected = select_representative_videos(_story(videos), index, minimum_chars=80, allow_supporting=True, max_evidence_videos=3)
    assert [item["video"]["video_id"] for item in selected] == ["1101", "1102", "1103"]
    assert {item["selection_reason"] for item in selected[1:]} == {"cross_video_detail_evidence"}


def test_enrichment_uses_long_platform_text_then_bounded_media_and_never_persists_url(tmp_path: Path) -> None:
    stories = [
        _story([_video("2001", "短标题", likes=100, account="a")]),
        {**_story([_video("2002", "另一条短标题", likes=50, account="b")]), "story_id": "story-ef56ab78", "event_id": "story-ef56ab78", "rank": 2},
    ]
    long_text = "腾讯发布一个新版本，视频随后介绍了使用方式和实际体验。所有内容都只是抖音线索。" * 3
    index = _index(tmp_path, [
        {"aweme_id": "2001", "title": "短标题", "caption": long_text, "video_download_url": "https://temporary.test/a"},
        {"aweme_id": "2002", "title": "另一条短标题", "video_download_url": "https://temporary.test/b"},
    ])
    calls: list[str] = []

    def media(video_id: str, _url: str) -> dict:
        calls.append(video_id)
        return {"status": "success", "method": "screen_ocr", "text": "画面文字说明产品正式开源，并展示了功能界面。" * 3}

    class DisabledAnalyzer:
        def status(self) -> dict:
            return {"enabled": False}

    enriched, report = enrich_ranked_stories(
        stories, index, _config(tmp_path), tmp_path / "work", global_deadline=10**12,
        analyzer=DisabledAnalyzer(), media_extractor=media,
    )
    assert calls == ["2002"]
    assert enriched[0]["extraction_methods"] == ["platform_text"]
    assert set(enriched[1]["extraction_methods"]) == {"screen_ocr", "story_title"}
    assert all(item.get("event_summary") and item.get("content_evidence") for item in enriched)
    payload = json.dumps(enriched, ensure_ascii=False)
    assert "temporary.test" not in payload and "video_download_url" not in payload
    assert report["raw_content_index"]["records_with_media"] == 2


def test_semantically_incomplete_long_hook_still_uses_screen_ocr(tmp_path: Path) -> None:
    stories = [_story([_video("2101", "一句很长却没说清楚主体和具体动作的热门科技标题", likes=100, account="a")])]
    index = _index(tmp_path, [{
        "aweme_id": "2101",
        "title": "一句很长却没说清楚主体和具体动作的热门科技标题" * 4,
        "video_download_url": "https://temporary.test/long-hook",
    }])

    class DisabledAnalyzer:
        def status(self) -> dict:
            return {"enabled": False}

    calls: list[str] = []
    enriched, _ = enrich_ranked_stories(
        stories, index, _config(tmp_path), tmp_path / "work", global_deadline=10**12,
        analyzer=DisabledAnalyzer(),
        media_extractor=lambda video_id, _url: calls.append(video_id) or {
            "status": "success", "method": "screen_ocr", "text": "腾讯开源混元模型，并展示代码生成和长文档处理能力。" * 3,
        },
    )
    assert calls == ["2101"]
    assert "screen_ocr" in enriched[0]["extraction_methods"]


def test_llm_added_number_is_rejected_without_changing_story_order_or_heat(tmp_path: Path) -> None:
    stories = [_story([_video("3001", "腾讯发布新模型", likes=100, account="a")])]
    index = _index(tmp_path, [{"aweme_id": "3001", "title": "腾讯发布新模型", "caption": "视频介绍腾讯发布新模型，并展示了基础体验。" * 4}])

    class BadAnalyzer:
        def status(self) -> dict:
            return {"enabled": True}

        def chat_json_once(self, *_args, **_kwargs) -> dict:
            return {"items": [{
                "story_id": "story-ab12cd34", "canonical_title": "腾讯模型",
                "event_summary": "性能提升999倍", "key_points": ["性能提升999倍"],
                "content_angles": ["发布"], "claims_to_verify": ["999倍"],
            }]}

    original = [(item["story_id"], item["heat_score"], item["rank"]) for item in stories]
    enriched, report = enrich_ranked_stories(
        stories, index, _config(tmp_path), tmp_path / "work", global_deadline=10**12,
        analyzer=BadAnalyzer(), media_extractor=lambda *_: {"status": "unavailable", "text": ""},
    )
    assert report["llm"]["rejected_items"] == 1
    assert enriched[0]["summary_source"] == "deterministic_evidence_extract"
    assert [(item["story_id"], item["heat_score"], item["rank"]) for item in enriched] == original


def test_llm_event_slots_are_evidence_validated_before_news_headline(tmp_path: Path) -> None:
    stories = [_story([_video("4001", "Microduck低成本复刻方案", likes=100, account="陆吾智能")])]
    description = "陆吾智能正在准备Microduck国内低成本复刻方案，改用国产舵机并修改电路，预计两周内发布。"
    index = _index(tmp_path, [{"aweme_id": "4001", "title": "Microduck低成本复刻方案", "caption": description}])

    class GoodAnalyzer:
        def status(self) -> dict:
            return {"enabled": True}

        def chat_json_once(self, *_args, **_kwargs) -> dict:
            return {"items": [{
                "story_id": "story-ab12cd34", "canonical_title": "Microduck低成本复刻方案",
                "event_summary": description, "key_points": ["改用国产舵机", "修改电路"],
                "content_angles": ["项目发布"], "claims_to_verify": ["预计两周内发布"],
                "content_type": "news_lead",
                "event_slots": {
                    "subject": "陆吾智能", "action": "发布", "object": "Microduck国内低成本复刻方案",
                    "time_text": "两周内", "event_status": "upcoming", "result_or_change": "改用国产舵机并修改电路",
                },
            }]}

    enriched, report = enrich_ranked_stories(
        stories, index, _config(tmp_path), tmp_path / "work", global_deadline=10**12,
        analyzer=GoodAnalyzer(), media_extractor=lambda *_: {"status": "unavailable", "text": ""},
    )
    item = enriched[0]
    assert report["llm"]["successful_batches"] == 1
    assert item["news_readiness"] == "ready"
    assert item["semantic_source"] == "llm_evidence_slots"
    assert all(value in item["news_headline"] for value in ("陆吾智能", "两周内", "发布", "Microduck"))
    assert item["event_evidence_refs"] == [{"video_id": "4001", "method": "platform_text"}]


def test_ocr_incidental_event_cannot_override_tutorial_video_title(tmp_path: Path) -> None:
    stories = [_story([_video("5001", "国产大模型教程引发AI圈热议", likes=100, account="教程账号")])]
    index = _index(tmp_path, [{
        "aweme_id": "5001", "title": "国产大模型教程引发AI圈热议",
        "video_download_url": "https://temporary.test/tutorial",
    }])

    class DisabledAnalyzer:
        def status(self) -> dict:
            return {"enabled": False}

    enriched, _ = enrich_ranked_stories(
        stories, index, _config(tmp_path), tmp_path / "work", global_deadline=10**12,
        analyzer=DisabledAnalyzer(),
        media_extractor=lambda *_: {
            "status": "success", "method": "screen_ocr", "text": "华为上线GUIAgent大模型并展示产品页面" * 4,
        },
    )
    item = enriched[0]
    assert set(item["extraction_methods"]) == {"screen_ocr", "story_title"}
    assert item["source_intent_type"] == "tutorial"
    assert item["content_type"] == "tutorial"
    assert item["news_readiness"] == "not_news"
