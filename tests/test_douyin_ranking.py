from __future__ import annotations

import copy
import json
from pathlib import Path

from douyin_intelligence.config import load_config
from douyin_intelligence.douyin_ranking import DEFAULT_WEIGHTS, cluster_ranked_videos, run_douyin_tech_ranking
from douyin_intelligence.models import VideoRecord


ROOT = Path(__file__).parents[1]


def _config(tmp_path: Path) -> dict:
    config = copy.deepcopy(load_config())
    config["_project_root"] = str(tmp_path)
    config["jobs"]["state_path"] = str(tmp_path / "state.json")
    config["jobs"]["lock_root"] = str(tmp_path / "locks")
    config["jobs"]["douyin_tech_ranking"]["output_root"] = str(tmp_path / "ranking")
    config["workbench"]["editorial_override_path"] = "state/editorial-overrides.json"
    config["workbench"]["editorial_output_root"] = str(tmp_path / "editorial")
    config["jobs"]["daily_news"]["sources"] = [{"name": "Official", "url": str(ROOT / "tests/fixtures/news_feed.xml"), "kind": "official"}]
    return config


def _record(video_id: str, title: str, published: str = "2026-08-25T12:00:00+08:00", *, like: int = 100, comment: int = 10, collect: int = 5, share: int = 3) -> VideoRecord:
    return VideoRecord(video_id=video_id, title=title, account_id="author", account_name="作者", share_url=f"https://www.douyin.com/video/{video_id}", published_at=published, digg_count=like, comment_count=comment, collect_count=collect, share_count=share, source_keyword="AI新品", source="douyin_search")


def test_heat_formula_is_deterministic_decays_and_resists_extreme_values() -> None:
    early = _record("7600000000000000001", "Example AI Device launches globally", "2026-08-25T01:00:00+08:00")
    late = _record("7600000000000000002", "Example AI Device launches globally update", "2026-08-25T23:00:00+08:00")
    extreme = _record("7600000000000000003", "Another AI Device", like=10**12, comment=10**12, collect=10**12, share=10**12)
    first, _ = cluster_ranked_videos([early, late, extreme], "2026-08-25", DEFAULT_WEIGHTS)
    second, _ = cluster_ranked_videos([early, late, extreme], "2026-08-25", DEFAULT_WEIGHTS)

    assert [(item["title"], item["score"]) for item in first] == [(item["title"], item["score"]) for item in second]
    combined = next(item for item in first if item["video_count"] == 2)
    assert combined["score_components"]["freshness"] > 0
    assert first[0]["score"] < sum(DEFAULT_WEIGHTS.values()) * 2


def test_clustering_deduplicates_and_stable_ties() -> None:
    duplicate_a = _record("7600000000000000001", "同一 AI 新品发布")
    duplicate_b = _record("7600000000000000001", "同一 AI 新品发布", like=5)
    alpha = _record("7600000000000000002", "Alpha platform", like=0, comment=0, collect=0, share=0)
    beta = _record("7600000000000000003", "Beta hardware", like=0, comment=0, collect=0, share=0)
    clusters, dropped = cluster_ranked_videos([duplicate_a, duplicate_b, beta, alpha], "2026-08-25", DEFAULT_WEIGHTS)

    assert not dropped
    assert sum(item["video_count"] for item in clusters) == 3
    tied = [item["title"] for item in clusters if item["title"] in {"Alpha platform", "Beta hardware"}]
    assert tied == ["Alpha platform", "Beta hardware"]


def test_ranking_partitions_evidence_without_removing_hotspots_and_filters_sensitive_fields(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = tmp_path / "safe-input.json"
    source.write_text(json.dumps({"items": [
        {"aweme_id": "7600000000000000001", "desc": "Example AI Device launches globally", "create_time": "2026-08-25T10:00:00+08:00", "author": {"nickname": "作者"}, "statistics": {"digg_count": 1000}, "share_url": "https://www.douyin.com/video/7600000000000000001?token=unsafe", "cookie": "never-export"},
        {"aweme_id": "7600000000000000002", "desc": "M6 新品来了但尚未证实", "create_time": "2026-08-25T11:00:00+08:00", "author": {"nickname": "作者"}, "statistics": {"digg_count": 900}, "share_url": "https://www.douyin.com/video/7600000000000000002", "play_addr": {"url": "https://bad.example/signature=secret"}},
    ]}, ensure_ascii=False), encoding="utf-8")

    result = run_douyin_tech_ranking(config, target_date="2026-08-25", douyin_inputs=[str(source)])
    payload = json.loads((tmp_path / "ranking/2026-08-25/ranking.json").read_text(encoding="utf-8"))

    assert result["status"] == "success"
    assert len(payload["hotspot_rankings"]) == 2
    assert len(payload["views"]["news_leads"]) == 2
    verified_item = next(item for item in payload["hotspot_rankings"] if item["evidence_status"] == "verified_official")
    unverified_item = next(item for item in payload["hotspot_rankings"] if item["evidence_status"] == "unverified_claim")
    assert verified_item["content_type"] == "news_lead"
    assert verified_item["verified_facts"] and verified_item["claims_to_verify"]
    assert unverified_item["official_sources"] == []
    assert "never-export" not in json.dumps(payload, ensure_ascii=False)
    assert "signature=secret" not in json.dumps(payload, ensure_ascii=False)
    assert verified_item["representative_videos"][0]["share_url"].endswith("0001")
    assert Path(payload["editorial_exports"]["news_json"]).is_file()


def test_creator_original_enters_main_ranking_without_official_news_and_uses_bounded_wording(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["jobs"]["daily_news"]["sources"] = []
    source = tmp_path / "creator.json"
    source.write_text(json.dumps([{
        "aweme_id": "7600000000000000001", "desc": "同样是扫地机，为什么差距这么大？云鲸 JXUltra 评测",
        "create_time": "2026-08-25T10:00:00+08:00", "author": {"nickname": "评测博主"},
        "statistics": {"digg_count": 3000, "comment_count": 100}, "share_url": "https://www.douyin.com/video/7600000000000000001",
    }], ensure_ascii=False), encoding="utf-8")

    result = run_douyin_tech_ranking(config, target_date="2026-08-25", douyin_inputs=[str(source)])
    item = result["hotspot_rankings"][0]
    markdown = Path(result["output_path"]).read_text(encoding="utf-8")

    assert item["content_type"] == "creator_review"
    assert item["evidence_status"] == "creator_primary"
    assert item["editorial_status"] == "ready_for_tech_talk"
    assert item["rank"] == 1
    assert item["representative_videos"][0]["share_url"] == "https://www.douyin.com/video/7600000000000000001"
    assert "creator_review" in markdown
    assert "科技杂谈参考" in result["broadcast"]
    talk_path = Path(result["editorial_exports"]["tech_talk_json"])
    talk_payload = json.loads(talk_path.read_text(encoding="utf-8"))
    talk_item = talk_payload["items"][0]
    assert talk_payload["schema"] == "tech-talk-reference-v1"
    assert {
        "heat_rank", "heat_score", "original_author", "douyin_url", "one_line_topic",
        "recommended_angle", "why_discuss", "three_part_structure", "claims_to_verify",
        "do_not_claim", "original_reference_notice",
    } <= talk_item.keys()
    assert talk_item["douyin_url"] == "https://www.douyin.com/video/7600000000000000001"


def test_official_evidence_changes_label_not_deterministic_heat_rank(tmp_path: Path) -> None:
    source = tmp_path / "topic.json"
    source.write_text(json.dumps([{
        "aweme_id": "7600000000000000001", "desc": "Example AI Device launches globally", "create_time": "2026-08-25T10:00:00+08:00",
        "statistics": {"digg_count": 1000}, "share_url": "https://www.douyin.com/video/7600000000000000001",
    }]), encoding="utf-8")
    verified = run_douyin_tech_ranking(_config(tmp_path / "verified"), target_date="2026-08-25", douyin_inputs=[str(source)])
    unverified_config = _config(tmp_path / "unverified")
    unverified_config["jobs"]["daily_news"]["sources"] = []
    unverified = run_douyin_tech_ranking(unverified_config, target_date="2026-08-25", douyin_inputs=[str(source)])

    assert verified["hotspot_rankings"][0]["score"] == unverified["hotspot_rankings"][0]["score"]
    assert verified["hotspot_rankings"][0]["rank"] == unverified["hotspot_rankings"][0]["rank"] == 1
    assert verified["hotspot_rankings"][0]["evidence_status"] == "verified_official"
    assert unverified["hotspot_rankings"][0]["evidence_status"] == "unverified_claim"
    news_payload = json.loads(Path(unverified["editorial_exports"]["news_json"]).read_text(encoding="utf-8"))
    news_item = news_payload["items"][0]
    assert news_payload["schema"] == "news-reference-v1"
    assert {
        "heat_rank", "heat_score", "verified_facts", "claims_to_verify", "official_sources",
        "recommended_news_angle", "safe_hook", "do_not_claim", "editorial_status",
        "representative_videos",
    } <= news_item.keys()
    assert news_item["verified_facts"] == [] and news_item["claims_to_verify"]


def test_budget_empty_and_source_failure_degrade_without_mixing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    rows = [{"aweme_id": str(7600000000000000000 + index), "desc": f"科技项目 {index}", "create_time": "2026-08-25T12:00:00+08:00", "share_url": f"https://www.douyin.com/video/{7600000000000000000 + index}"} for index in range(12)]
    source = tmp_path / "many.json"
    source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    broken = tmp_path / "broken.xml"
    broken.write_text("<rss><broken>", encoding="utf-8")
    config["jobs"]["daily_news"]["sources"] = [{"name": "Broken", "url": str(broken), "kind": "official"}]

    result = run_douyin_tech_ranking(config, target_date="2026-08-25", douyin_inputs=[str(source)])

    assert result["metadata_budget"] == 10
    assert result["metadata_retained"] == 10
    assert not result["views"]["news_leads"]
    assert result["views"]["manual_review"]
    assert result["source_errors"]
    empty = run_douyin_tech_ranking(_config(tmp_path / "empty"), target_date="2026-08-25")
    assert empty["status"] == "empty"


def test_live_collection_without_safe_metadata_degrades_without_retrying_login(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    observed: dict[str, object] = {}

    def no_metadata(_config, budget, run_id, *, keywords, hard_max):
        observed.update({"budget": budget, "keywords": keywords, "hard_max": hard_max})
        return {"status": "failed", "files": [], "error": "authentication required"}

    monkeypatch.setattr("douyin_intelligence.douyin_ranking.collect_search", no_metadata)
    result = run_douyin_tech_ranking(config, target_date="2026-08-25", live_douyin=True)

    assert observed == {"budget": 10, "keywords": ["AI新品"], "hard_max": 10}
    assert result["status"] == "empty"
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["jobs"]["douyin_tech_ranking"]["errors"][0]["source"] == "douyin"
