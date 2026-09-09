from __future__ import annotations

import copy
import json
import math
from pathlib import Path

from douyin_intelligence.daily_hot_candidate_pool import (
    _annotate_account_metadata,
    cluster_candidate_videos,
    _approved_account_config,
    _markdown,
    _reused_raw_evidence_files,
    decide_v2_promotion,
    filter_target_day,
    plan_keywords,
    promote_existing_v2_pack,
    rank_candidate_events,
    retain_per_keyword,
    run_account_matrix_topic_radar_smoke,
    should_reuse_same_day_raw,
)
from douyin_intelligence.config import load_config
from douyin_intelligence.daily_material_exchange import _sha256, _write_manifest, inspect_daily_material_exchange
from douyin_intelligence.exporter import atomic_write_json
from douyin_intelligence.models import VideoRecord
from douyin_intelligence.workbench import daily_material_exchange_command


def _video(video_id: str, title: str, *, source: str = "douyin_search", keyword: str = "AI新品", account: str = "searcher", published: str = "2026-08-28T12:00:00+08:00", likes: int = 10, group: str = "", group_name: str = "", lane: str = "", role: str = "") -> VideoRecord:
    return VideoRecord(video_id=video_id, title=title, account_id=account, account_name=account, share_url=f"https://www.douyin.com/video/{video_id}", published_at=published, source=source, source_keyword=keyword, digg_count=likes, comment_count=2, collect_count=1, share_count=1, source_group_id=group, source_group_name=group_name, editorial_lane=lane, production_role=role)


def test_same_day_raw_reuse_recovers_only_exact_project_capture_files(tmp_path: Path) -> None:
    config = {"_project_root": str(tmp_path)}
    matched = tmp_path / "data" / "raw" / "mediacrawler" / "runs" / "candidate-pool-2026-09-01-a1b2c3d4e5f6" / "creator" / "one" / "rows.jsonl"
    matched.parent.mkdir(parents=True)
    matched.write_text('{"aweme_id":"1"}\n', encoding="utf-8")
    other = tmp_path / "data" / "raw" / "mediacrawler" / "runs" / "candidate-pool-2026-09-01-ffffeeee1111" / "rows.jsonl"
    other.parent.mkdir(parents=True)
    other.write_text('{"aweme_id":"2"}\n', encoding="utf-8")
    found = _reused_raw_evidence_files(config, "2026-09-01", "run-20260901-a1b2c3d4e5f6")
    assert found == [matched]
    assert _reused_raw_evidence_files(config, "2026-09-01", "not-a-run") == []


def test_keyword_plan_attempts_all_core_and_stops_supplemental_at_threshold() -> None:
    plan = plan_keywords(["科技快讯", "AI新品"], ["热门开源项目", "国际科技大厂"], event_count=0, stop_at_events=20)
    assert plan == ["科技快讯", "AI新品", "热门开源项目", "国际科技大厂"]
    assert plan_keywords(["科技快讯"], ["热门开源项目"], event_count=20, stop_at_events=20) == ["科技快讯"]


def test_same_day_raw_reuse_uses_current_coverage_as_non_regression_floor() -> None:
    current = {"target_day_videos": 34}
    assert should_reuse_same_day_raw(9, None)
    assert should_reuse_same_day_raw(18, current)
    assert not should_reuse_same_day_raw(34, current)
    assert not should_reuse_same_day_raw(40, current)


def test_filter_target_day_and_cluster_preserves_all_video_provenance() -> None:
    videos = [
        _video("100000001", "小鹏第二代 VLA 新版本九月推送", source="douyin_search", keyword="科技快讯", likes=100),
        _video("100000002", "小鹏第二代VLA新版本 9月开启推送", source="douyin_creator", keyword="", account="approved-a", likes=30),
        _video("100000003", "不应混入的次日内容", published="2026-08-29T00:00:00+08:00"),
    ]
    kept, excluded = filter_target_day(videos, "2026-08-28")
    assert len(kept) == 2 and excluded[0]["reason"] == "不在北京时间目标日"
    events, dropped = cluster_candidate_videos(kept, "2026-08-28", approved_accounts={"approved-a"}, event_limit=50)
    assert not dropped and len(events) == 1
    event = events[0]
    assert event["video_count"] == 2
    assert {row["video_id"] for row in event["contributing_videos"]} == {"100000001", "100000002"}
    assert event["source_lanes"] == ["account", "search"]
    assert event["matched_keywords"] == ["科技快讯"]


def test_cluster_merges_differently_worded_named_product_posts() -> None:
    events, _ = cluster_candidate_videos([
        _video("100000021", "AI打印再升级，奔图喷墨打印机来了", keyword="AI新品"),
        _video("100000022", "奔图AI喷墨打印机掀桌，跑出打印超车时代", keyword="大模型发布"),
    ], "2026-08-28", approved_accounts=set(), event_limit=50)
    assert len(events) == 1
    assert events[0]["video_count"] == 2
    assert events[0]["matched_keywords"] == ["AI新品", "大模型发布"]


def test_ranking_ignores_truth_and_image_fields_and_is_stable() -> None:
    events, _ = cluster_candidate_videos([
        _video("100000010", "芯片发布 A", likes=100),
        _video("100000011", "芯片发布 B", likes=20),
    ], "2026-08-28", approved_accounts=set(), event_limit=50)
    weights = {"like": 22, "comment": 20, "collect": 18, "share": 22, "freshness": 10, "related_videos": 4, "approved_account_coverage": 2, "cross_lane": 2}
    first = rank_candidate_events(events, "2026-08-28", weights)
    events[0]["truth_status"] = "verified"; events[0]["images"] = [{"bad": "does_not_rank"}]
    second = rank_candidate_events(events, "2026-08-28", weights)
    assert [(row["event_id"], row["rank"], row["heat_score"]) for row in first] == [(row["event_id"], row["rank"], row["heat_score"]) for row in second]
    assert all("score_components" in row for row in first)


def test_unverified_single_source_event_is_retained() -> None:
    events, _ = cluster_candidate_videos([_video("100000020", "未经证实的新模型传闻", likes=1)], "2026-08-28", approved_accounts=set(), event_limit=50)
    assert len(events) == 1
    assert events[0]["truth_status"] == "not_checked"
    assert "copy_skill 未核验真实性" in events[0]["disclaimer"]


def test_workbench_command_uses_dynamic_v2_run_without_a_frozen_date() -> None:
    command = daily_material_exchange_command("config/content_intelligence.json")
    assert command[-2:] == ["daily-material-exchange", "run"]
    assert "2026-08-28" not in command


def test_per_keyword_retention_reports_discovered_and_enforces_cap() -> None:
    records = [_video(f"1000001{index:02d}", f"AI新品 {index}", keyword="AI新品", published=f"2026-08-28T{index:02d}:00:00+08:00") for index in range(12)]
    kept, counts = retain_per_keyword(records, ["AI新品"], 10)
    assert counts == {"AI新品": (12, 10)}
    assert len(kept) == 10
    assert [item.video_id for item in kept][:1] == ["100000111"]


def test_three_frozen_topic_radar_candidates_are_valid_and_selected_without_promoting_lifecycle() -> None:
    config = load_config()
    candidates = config["jobs"]["account_pool"]["initial_candidates"]
    radar = [item for item in candidates if item.get("production_role") == "topic_radar"]
    assert [(item["id"], item["editorial_lane"]) for item in radar] == [("42896519316", "ai_general"), ("44882093155", "compute_infrastructure"), ("49506654128", "chips_hardware")]
    assert all(item["lifecycle_status"] == "candidate" and item["enabled"] and item["discovery_enabled"] and item["source_group_id"] == "kaishi-dajisuan-ai-matrix" for item in radar)
    local, accounts, approved = _approved_account_config(config)
    assert {item["id"] for item in accounts if item.get("production_role") == "topic_radar"} == {item["id"] for item in radar}
    assert not ({item["id"] for item in radar} & approved)
    altered = copy.deepcopy(config)
    altered["jobs"]["account_pool"]["initial_candidates"].append({"display_name": "普通候选", "profile_url": "https://www.douyin.com/user/MS4wLjABAAAAabcdefghijklmnopqrstuv", "enabled": True})
    assert "普通候选" not in {item["name"] for item in _approved_account_config(altered)[1]}
    assert local["benchmark_accounts"] == accounts


def test_matrix_same_event_keeps_all_provenance_but_deduplicates_effective_heat() -> None:
    matrix = "kaishi-dajisuan-ai-matrix"
    events, _ = cluster_candidate_videos([
        _video("100000031", "开源大模型发布", account="42896519316", likes=10, group=matrix, group_name="大计算 / 开市科技矩阵", lane="ai_general", role="topic_radar"),
        _video("100000032", "开源大模型发布最新消息", account="44882093155", likes=30, group=matrix, group_name="大计算 / 开市科技矩阵", lane="compute_infrastructure", role="topic_radar"),
        _video("100000033", "开源大模型发布进展", account="49506654128", likes=20, group=matrix, group_name="大计算 / 开市科技矩阵", lane="chips_hardware", role="topic_radar"),
    ], "2026-08-28", approved_accounts=set(), event_limit=50)
    assert len(events) == 1
    event = events[0]
    assert event["video_count"] == event["account_count_raw"] == 3 and event["source_group_count"] == 1
    assert event["video_count_raw"] == 3 and event["effective_video_count"] == 1
    assert event["aggregate_interactions_raw"]["like"] == 60 and event["effective_interactions"]["like"] == 30
    assert {item["editorial_lane"] for item in event["contributing_videos"]} == {"ai_general", "compute_infrastructure", "chips_hardware"}
    assert event["matrix_deduplication"][0]["selected_video_id"] == "100000032"
    ranked = rank_candidate_events(events, "2026-08-28", {"like": 22, "comment": 20, "collect": 18, "share": 22, "freshness": 10, "related_videos": 4, "source_group_coverage": 2, "cross_lane": 2})
    assert ranked[0]["score_components"]["source_group_coverage"] == round(2 / 3, 3)
    assert ranked[0]["score_components"]["related_videos"] == round(4 * (math.log1p(1) / math.log(11)), 3)
    markdown = _markdown({"business_date": "2026-08-28", "status": "partial", "candidates": ranked})
    assert "原始账号：3；独立运营主体：1" in markdown and "有效互动" in markdown and "ai_general" in markdown


def test_same_matrix_different_events_survive_and_independent_group_adds_effective_interactions() -> None:
    matrix = "kaishi-dajisuan-ai-matrix"
    events, _ = cluster_candidate_videos([
        _video("100000041", "量子光芯片突破", account="42896519316", likes=10, group=matrix, lane="ai_general"),
        _video("100000042", "机器人灵巧手发布", account="44882093155", likes=20, group=matrix, lane="compute_infrastructure"),
        _video("100000043", "国产存储新品", account="49506654128", likes=30, group=matrix, lane="chips_hardware"),
    ], "2026-08-28", approved_accounts=set(), event_limit=50)
    assert len(events) == 3
    shared, _ = cluster_candidate_videos([
        _video("100000044", "芯片发布会现场", account="42896519316", likes=10, group=matrix, lane="chips_hardware"),
        _video("100000045", "芯片发布会现场最新", account="independent-a", likes=40, lane="chips_hardware"),
    ], "2026-08-28", approved_accounts=set(), event_limit=50)
    assert shared[0]["source_group_count"] == 2 and shared[0]["effective_interactions"]["like"] == 50
    assert shared[0]["effective_video_count"] == 2
    legacy, _ = cluster_candidate_videos([_video("100000046", "显卡新品发布", account="legacy-a", likes=11), _video("100000047", "显卡新品发布消息", account="legacy-b", likes=12)], "2026-08-28", approved_accounts=set(), event_limit=50)
    assert legacy[0]["source_group_count"] == 2 and legacy[0]["effective_interactions"]["like"] == 23


def test_search_only_topic_radar_video_receives_matrix_metadata() -> None:
    config = load_config()
    _, accounts, _ = _approved_account_config(config)
    rows = [_video("100000048", "搜索通道发现的矩阵新闻", account="42896519316")]
    _annotate_account_metadata(rows, accounts)
    assert rows[0].production_role == "topic_radar"
    assert rows[0].source_group_id == "kaishi-dajisuan-ai-matrix"
    assert rows[0].editorial_lane == "ai_general"


def test_matrix_rank_stays_independent_of_truth_image_llm_and_human_fields() -> None:
    event, _ = cluster_candidate_videos([_video("100000051", "AI 芯片新品", account="42896519316", likes=30, group="matrix"), _video("100000052", "AI 芯片新品发布", account="44882093155", likes=10, group="matrix")], "2026-08-28", approved_accounts=set(), event_limit=50)
    weights = {"like": 22, "comment": 20, "collect": 18, "share": 22, "freshness": 10, "related_videos": 4, "source_group_coverage": 2, "cross_lane": 2}
    first = rank_candidate_events(event, "2026-08-28", weights)
    event[0].update(truth_status="verified", images=[{"ignored": True}], llm="ignored", editor="ignored")
    assert [(row["event_id"], row["heat_score"]) for row in first] == [(row["event_id"], row["heat_score"]) for row in rank_candidate_events(event, "2026-08-28", weights)]


def test_account_matrix_smoke_is_account_only_and_records_timeout_without_network_fixture(tmp_path: Path) -> None:
    config = copy.deepcopy(load_config())
    config["_project_root"] = str(tmp_path)

    def simulated_collector(local: dict, run_id: str) -> dict:
        selected = local["benchmark_accounts"]
        assert len(selected) == 3 and all(item["production_role"] == "topic_radar" for item in selected)
        return {"status": "partial", "accounts": [], "attempts": [{"account_id": selected[0]["id"], "returncode": 0, "timeout_seconds": 120}, {"account_id": selected[1]["id"], "returncode": 124, "timeout_seconds": 120, "error": "timeout"}, {"account_id": selected[2]["id"], "returncode": 0, "timeout_seconds": 120}], "browser": {"single_project_browser": True}}

    result = run_account_matrix_topic_radar_smoke(config, run_id="fixture", collector=simulated_collector)
    assert result["status"] == "partial" and [item["status"] for item in result["accounts"]] == ["empty", "timeout", "empty"]
    assert result["audio_asr_ocr_llm_images"] == 0 and result["full_keyword_search"] is False
    assert (tmp_path / "output" / "account-matrix-topic-radar-smoke" / "fixture" / "account-matrix-topic-radar-smoke.json").is_file()


def test_same_day_promotion_never_regresses_candidates_and_uses_stable_tiebreaks() -> None:
    current = {"run_id": "run-20260828-current0001", "business_date": "2026-08-28", "candidate_count": 24, "target_day_videos": 31, "top3_image_successes": 2}
    assert decide_v2_promotion({**current, "run_id": "run-20260828-lower000001", "candidate_count": 3}, current)["promotion_status"] == "rejected"
    assert decide_v2_promotion({**current, "run_id": "run-20260828-higher00001", "candidate_count": 25}, current)["reason"] == "candidate_count_increased"
    assert decide_v2_promotion({**current, "run_id": "run-20260828-target00001", "target_day_videos": 32}, current)["reason"] == "target_day_videos_increased"
    assert decide_v2_promotion({**current, "run_id": "run-20260828-targetlower01", "target_day_videos": 30}, current)["reason"] == "target_day_videos_decreased"
    assert decide_v2_promotion({**current, "run_id": "run-20260828-image000001", "top3_image_successes": 3}, current)["reason"] == "top3_image_successes_increased"
    assert decide_v2_promotion({**current, "run_id": "run-20260828-imagelower01", "top3_image_successes": 1}, current)["reason"] == "top3_image_successes_decreased"
    assert decide_v2_promotion({**current, "run_id": "run-20260828-same0000001"}, current)["reason"] == "same_quality_not_promoted"
    partial_current = {**current, "run_id": "run-20260828-partial000001", "status": "partial", "public_reader_hot_list_revision": 1, "public_reader_hot_list_card_count": 20}
    recovered_status = {**partial_current, "run_id": "run-20260828-recovered001", "status": "success"}
    assert decide_v2_promotion(recovered_status, partial_current)["reason"] == "run_status_recovered"
    assert decide_v2_promotion(partial_current, recovered_status)["reason"] == "run_status_regressed"
    semantic_upgrade = {**current, "run_id": "run-20260828-semantic0001", "semantic_contract_revision": 2}
    assert decide_v2_promotion(semantic_upgrade, current)["reason"] == "semantic_contract_revision_increased"
    observability_upgrade = {**current, "run_id": "run-20260828-observe00001", "material_exchange_observability_revision": 1}
    assert decide_v2_promotion(observability_upgrade, current)["reason"] == "material_exchange_observability_revision_increased"
    assert decide_v2_promotion({**current, "run_id": "run-20260828-otherdate01", "business_date": "2026-08-29"}, current)["promotion_status"] == "promoted"
    assert decide_v2_promotion({**current, "run_id": "run-20260828-manual00001", "candidate_count": 1}, current, override=True)["reason"] == "manual_override"
    upgraded = {**current, "run_id": "run-20260828-upgrade0001", "contract_version": "2.3"}
    previous = {**current, "contract_version": "2.2"}
    assert decide_v2_promotion(upgraded, previous)["reason"] == "story_contract_upgrade"
    regressed_upgrade = {**upgraded, "run_id": "run-20260828-upgradelow001", "candidate_count": 18}
    assert decide_v2_promotion(regressed_upgrade, previous)["reason"] == "story_contract_upgrade_candidate_count_decreased"
    complete_old = {**current, "run_id": "run-20260828-complete-old", "contract_version": "2.4", "status": "success", "public_reader_hot_list_revision": 1, "public_reader_hot_list_card_count": 20}
    partial_new = {**complete_old, "run_id": "run-20260828-partial-new", "contract_version": "2.5", "status": "partial", "public_reader_hot_list_revision": 2, "public_reader_hot_list_card_count": 3}
    assert decide_v2_promotion(partial_new, complete_old)["reason"] == "run_status_regressed"
    assert decide_v2_promotion(complete_old, partial_new)["reason"] == "run_status_recovered"
    official_current = {**current, "run_id": "run-20260828-officialcurrent", "official_discovery_revision": 4, "official_major_events": 5, "official_source_errors": 0}
    degraded_official = {**official_current, "run_id": "run-20260828-officialdegraded", "official_discovery_revision": 5, "official_major_events": 2, "official_source_errors": 1}
    assert decide_v2_promotion(degraded_official, official_current)["reason"] == "official_source_errors_increased"
    recovered_official = {**official_current, "run_id": "run-20260828-officialrecovered", "official_major_events": 6}
    assert decide_v2_promotion(recovered_official, official_current)["reason"] == "official_major_events_increased"
    reader_current = {**current, "run_id": "run-20260828-reader-current", "public_reader_hot_list_revision": 0, "public_reader_hot_list_card_count": 0}
    reader_incomplete = {**reader_current, "run_id": "run-20260828-reader-incomplete", "public_reader_hot_list_revision": 1, "public_reader_hot_list_card_count": 19}
    assert decide_v2_promotion(reader_incomplete, reader_current)["reason"] == "public_reader_hot_list_incomplete"
    reader_complete = {**reader_current, "run_id": "run-20260828-reader-complete", "public_reader_hot_list_revision": 1, "public_reader_hot_list_card_count": 20}
    assert decide_v2_promotion(reader_complete, reader_current)["reason"] == "public_reader_hot_list_revision_increased"


def _ready_pack(root: Path, business_date: str, run_id: str, *, candidates: int, target_videos: int, images: int) -> None:
    pack = root / "out" / f"{business_date}_每日素材" / "packs" / run_id
    pack.mkdir(parents=True)
    rows = [{"event_id": f"event-{index}", "title": f"候选 {index}"} for index in range(candidates)]
    report = {"counts": {"target_day_videos": target_videos, "top3_image_successes": images}, "image_attempts": [{"state": "succeeded"} for _ in range(images)]}
    contract = {"schema": "daily-hot-candidate-pool-v2", "contract_version": "2.0", "business_date": business_date, "run_id": run_id, "candidates": rows, "counts": {"target_day_videos": target_videos, "images": images, "fact_ready": 0, "stories_with_primary": images}, "run_report": report}
    atomic_write_json(pack / "candidate-pool.json", contract)
    atomic_write_json(pack / "run-report.json", report)
    (pack / "昨日抖音科技热点候选.md").write_text("# test\n", encoding="utf-8")
    manifest = _write_manifest(pack)
    sha = _sha256(pack / "package-manifest.json")
    atomic_write_json(pack / "_READY.json", {"contract_version": "2.0", "business_date": business_date, "run_id": run_id, "generated_at": "2026-08-29T01:00:00+08:00", "status": "partial", "machine_contract": "candidate-pool.json", "human_brief": "昨日抖音科技热点候选.md", "manifest": "package-manifest.json", "package_manifest_sha256": sha, "counts": contract["counts"], "missing": []})
    assert manifest["files"]


def test_existing_ready_pack_promotion_keeps_better_same_day_current_and_inspects(tmp_path: Path) -> None:
    config = {"_project_root": str(tmp_path), "timezone": "Asia/Shanghai", "jobs": {"daily_hot_candidate_pool_v2": {"output_root": "out"}, "daily_material_exchange": {"output_root": "out"}}}
    business_date = "2026-08-28"
    _ready_pack(tmp_path, business_date, "run-20260828-better00001", candidates=24, target_videos=31, images=2)
    _ready_pack(tmp_path, business_date, "run-20260828-worse000001", candidates=3, target_videos=3, images=1)
    promoted = promote_existing_v2_pack(config, business_date=business_date, run_id="run-20260828-better00001")
    assert promoted["promotion_status"] == "promoted"
    rejected = promote_existing_v2_pack(config, business_date=business_date, run_id="run-20260828-worse000001")
    assert rejected["promotion_status"] == "rejected" and rejected["current_kept"] == "run-20260828-better00001"
    date_root = tmp_path / "out" / "2026-08-28_每日素材"
    current = json.loads((date_root / "current.json").read_text(encoding="utf-8"))
    latest = json.loads((tmp_path / "out" / "latest.json").read_text(encoding="utf-8"))
    assert current["pack_relative_path"] == "packs/run-20260828-better00001"
    assert latest["current_relative_path"] == "2026-08-28_每日素材/current.json"
    assert json.loads((date_root / "promotion-decisions" / "run-20260828-worse000001.json").read_text(encoding="utf-8"))["reason"] == "candidate_count_decreased"
    assert inspect_daily_material_exchange(config, business_date=business_date)["stories"] == 24
