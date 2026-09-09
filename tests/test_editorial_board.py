from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from douyin_intelligence.editorial_board import EditorialOverrideStore, apply_overrides, build_editorial_item, classify_content, partition_views


def _hotspot(title: str, video_id: str = "7600000000000000001") -> dict:
    return {
        "title": title, "score": 42.0, "score_components": {"like": 10.0, "comment": 5.0, "collect": 3.0, "share": 4.0, "related_videos": 2.0, "freshness": 8.0},
        "video_count": 1, "heat_window": "2026-08-26 00:00–23:59 Asia/Shanghai", "why_hot": "公开互动和时间信号达到门槛。",
        "representative_videos": [{"video_id": video_id, "title": title, "published_at": "2026-08-26T12:00:00+08:00", "author": "作者", "share_url": f"https://www.douyin.com/video/{video_id}", "interactions": {"like": 1000, "comment": 100, "collect": 50, "share": 20}}],
    }


def test_fixed_real_sample_semantics_and_claim_boundaries() -> None:
    m6 = build_editorial_item(_hotspot("M6芯片的Mac mini来了！"), None)
    cleaner = build_editorial_item(_hotspot("同样是扫地机，为什么差距这么大？云鲸 JXUltra 评测", "7600000000000000002"), None)
    studio = build_editorial_item(_hotspot("Mac Studio跑本地AI，是生产力还是性能焦虑？4.7万起", "7600000000000000003"), None)

    assert (m6["primary_content_type"], m6["evidence_status"], m6["editorial_status"]) == ("news_lead", "unverified_claim", "research_required")
    assert "尚未获得可靠官方确认" in m6["safe_hook"]
    assert m6["claims_to_verify"] and m6["verified_facts"] == []
    assert (cleaner["primary_content_type"], cleaner["evidence_status"], cleaner["editorial_status"]) == ("creator_review", "creator_primary", "ready_for_tech_talk")
    assert studio["primary_content_type"] in {"creator_experiment", "mixed"}
    assert studio["evidence_status"] == "creator_primary"
    assert any("价格" in claim or "规格" in claim for claim in studio["claims_to_verify"])


def test_mixed_content_enters_both_views_with_separate_guidance() -> None:
    item = build_editorial_item(_hotspot("新品发布后实测对比：到底值不值？"), None)
    item["heat_rank"] = item["rank"] = 1
    views = partition_views([item], [])

    assert item["primary_content_type"] == "mixed"
    assert item in views["news_leads"] and item in views["tech_talk"]
    assert item["news_usage_guidance"] != item["tech_talk_angle"]


def test_override_is_atomic_recoverable_and_cannot_forge_evidence(tmp_path: Path) -> None:
    store = EditorialOverrideStore(tmp_path / "overrides.json")
    item = build_editorial_item(_hotspot("M6芯片的Mac mini来了！"), None)
    item["heat_rank"] = item["rank"] = 1

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda patch: store.update(item["topic_id"], patch), ({"primary_content_type": "creator_opinion"}, {"video_candidate": True})))
    stored = store.load()["topics"][item["topic_id"]]
    assert stored["primary_content_type"] == "creator_opinion"
    assert stored["video_candidate"] is True
    overridden = apply_overrides([item], store.load())[0]
    assert overridden["evidence_status"] == "unverified_claim"
    assert overridden["heat_rank"] == 1 and overridden["heat_score"] == 42.0

    store.update(item["topic_id"], {"ignored": True, "editor_note": "稍后恢复"})
    ignored = apply_overrides([item], store.load())[0]
    assert ignored["editorial_status"] == "ignored"
    assert ignored in partition_views([ignored], [])["manual_review"]
    store.update(item["topic_id"], {"ignored": False})
    restored = apply_overrides([item], store.load())[0]
    assert restored["editorial_status"] != "ignored"
    assert not store.lock_path.exists()


def test_override_rejects_evidence_and_unsafe_identifiers(tmp_path: Path) -> None:
    store = EditorialOverrideStore(tmp_path / "overrides.json")
    with pytest.raises(ValueError):
        store.update("../../escape", {"primary_content_type": "news_lead"})
    with pytest.raises(ValueError):
        store.update("topic-123", {"evidence_status": "verified_official"})


def test_uncertain_classification_is_retained_for_manual_review() -> None:
    item = build_editorial_item(_hotspot("今日科技随手记录"), None)
    item["heat_rank"] = item["rank"] = 1
    views = partition_views([item], [])

    assert classify_content(item)[0] == "uncertain"
    assert item["evidence_status"] == "insufficient_metadata"
    assert item["editorial_status"] == "manual_review"
    assert item in views["manual_review"]
