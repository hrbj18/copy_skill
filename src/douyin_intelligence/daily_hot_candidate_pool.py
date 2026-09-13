from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .collector import close_project_browser, collect_creators
from .consumer_hot_list import build_consumer_hot_list, build_public_reader_hot_list, render_consumer_hot_list
from .daily_material_exchange import DailyMaterialExchangeError, _path, _relative, _sha256, _validate_manifest, _write_manifest, _write_text, beijing_yesterday, date_directory, inspect_daily_material_exchange
from .editorial_priority import filter_technology_scope, prioritize_for_delivery
from .exporter import atomic_write_json
from .human_brief import render_human_brief
from .job_runtime import JobLock, JobState
from .llm_analysis import OpenAICompatibleAnalyzer
from .material_probe import MaterialProbeError, RequestBudget, SafeFetcher, _decode_image, redact_url
from .models import VideoRecord
from .news_semantics import semantic_counts
from .official_major_events import apply_public_reader_policy, build_official_major_events, enrich_public_event_details, fetch_official_sources, localize_official_event_cards
from .public_detail_discovery import PublicDetailDiscovery
from .public_web_discovery import build_public_web_candidate_pool, fetch_public_web_discovery, render_public_web_candidate_pool
from .normalize import normalize_files
from .search_collector import collect_search
from .story_consolidation import consolidate_story_videos
from .story_enrichment import RawContentIndex, enrich_ranked_stories


V2_VERSION = "2.6"
SUPPORTED_V2_VERSIONS = {"2.0", "2.1", "2.2", "2.3", "2.4", V2_VERSION}
DISCLAIMER = "copy_skill 未核验真实性，候选仅供 OP 选题与后续核验。"
_SAFE_VIDEO = re.compile(r"^https://(?:www\.)?douyin\.com/video/(\d{8,})$")
_SAFE_RUN_ID = re.compile(r"^run-[a-z0-9-]{8,96}$")


def _safe_url(video_id: str, url: str) -> str:
    match = _SAFE_VIDEO.fullmatch(str(url or "").strip())
    return f"https://www.douyin.com/video/{match.group(1)}" if match else (f"https://www.douyin.com/video/{video_id}" if str(video_id).isdigit() else "")


def _point(value: int | None, weight: float) -> float:
    return round(float(weight) * min(1.0, math.log1p(max(0, int(value or 0))) / math.log1p(1_000_000)), 3)


def _lane(record: Any) -> str:
    return "search" if str(record.source) in {"douyin_search", "douyin_hotboard"} else "account"


def _video_row(record: Any) -> dict[str, Any]:
    return {
        "video_id": str(record.video_id), "title": str(record.title), "author": str(record.account_name),
        "account_id": str(record.account_id), "share_url": _safe_url(str(record.video_id), str(record.share_url)),
        "published_at": str(record.published_at), "interactions": {
            "like": int(record.digg_count or 0), "comment": int(record.comment_count or 0),
            "collect": int(record.collect_count or 0), "share": int(record.share_count or 0),
        }, "source_lanes": [_lane(record)], "matched_keywords": [str(record.source_keyword)] if str(record.source_keyword).strip() else [],
        "production_role": str(getattr(record, "production_role", "") or ""),
        "source_group_id": str(getattr(record, "source_group_id", "") or ""),
        "source_group_name": str(getattr(record, "source_group_name", "") or ""),
        "editorial_lane": str(getattr(record, "editorial_lane", "") or ""),
    }


def filter_target_day(records: list[Any], business_date: str) -> tuple[list[Any], list[dict[str, str]]]:
    kept: list[Any] = []
    excluded: list[dict[str, str]] = []
    for record in records:
        if not record.published_at:
            excluded.append({"video_id": str(record.video_id), "reason": "缺少可靠发布时间"})
        elif not str(record.published_at).startswith(business_date):
            excluded.append({"video_id": str(record.video_id), "reason": "不在北京时间目标日"})
        elif not record.title or not _safe_url(str(record.video_id), str(record.share_url)):
            excluded.append({"video_id": str(record.video_id), "reason": "缺少标题或安全视频链接"})
        else:
            kept.append(record)
    return kept, excluded


def _merge_video(rows: list[Any]) -> dict[str, Any]:
    winner = max(rows, key=lambda row: (sum(int(getattr(row, field) or 0) for field in ("digg_count", "comment_count", "collect_count", "share_count")), str(row.published_at), str(row.video_id)))
    result = _video_row(winner)
    result["source_lanes"] = sorted({_lane(row) for row in rows})
    result["matched_keywords"] = sorted({str(row.source_keyword).strip() for row in rows if str(row.source_keyword).strip()})
    return result


def _interaction_total(interactions: dict[str, int]) -> int:
    return sum(int(interactions.get(field) or 0) for field in ("like", "comment", "collect", "share"))


def _source_group_key(video: dict[str, Any]) -> str:
    return str(video.get("source_group_id") or "").strip() or f"account:{video['account_id']}"


def _matrix_interactions(videos: list[dict[str, Any]]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for video in videos:
        groups.setdefault(_source_group_key(video), []).append(video)
    effective = {field: 0 for field in ("like", "comment", "collect", "share")}
    decisions: list[dict[str, Any]] = []
    for group_id, rows in sorted(groups.items()):
        selected = max(rows, key=lambda item: (_interaction_total(item["interactions"]), item["published_at"], item["video_id"]))
        for field in effective:
            effective[field] += int(selected["interactions"][field])
        decisions.append({"source_group_id": str(selected.get("source_group_id") or ""), "source_group_key": group_id, "source_group_name": str(selected.get("source_group_name") or ""), "selected_video_id": selected["video_id"], "suppressed_video_ids": sorted(item["video_id"] for item in rows if item["video_id"] != selected["video_id"]), "basis": "highest_raw_interaction_sum_per_source_group"})
    return effective, decisions


def cluster_candidate_videos(records: list[Any], business_date: str, *, approved_accounts: set[str], event_limit: int) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    unique: dict[str, list[Any]] = {}
    dropped: list[dict[str, str]] = []
    for record in sorted(records, key=lambda item: (str(item.video_id), str(item.share_url), str(item.raw_file))):
        video_id = str(record.video_id)
        if not video_id:
            dropped.append({"video_id": "", "reason": "缺少视频标识"}); continue
        unique.setdefault(video_id, []).append(record)
    videos = [_merge_video(rows) for _, rows in sorted(unique.items())]
    clusters = consolidate_story_videos(videos, business_date)
    events: list[dict[str, Any]] = []
    for cluster in clusters:
        videos_for_event = sorted(cluster["videos"], key=lambda row: (row["published_at"], row["video_id"]))
        representative = max(videos_for_event, key=lambda row: (sum(row["interactions"].values()), row["video_id"]))
        lanes = sorted({lane for row in videos_for_event for lane in row["source_lanes"]})
        raw_accounts = sorted({row["account_id"] for row in videos_for_event})
        approved = sorted({row["account_id"] for row in videos_for_event if row["account_id"] in approved_accounts})
        aggregate_raw = {key: sum(row["interactions"][key] for row in videos_for_event) for key in ("like", "comment", "collect", "share")}
        effective, matrix_decisions = _matrix_interactions(videos_for_event)
        source_group_keys = sorted({_source_group_key(row) for row in videos_for_event})
        events.append({
            "event_id": cluster["story_id"], "story_id": cluster["story_id"], "title": representative["title"][:180],
            "canonical_title": str(cluster["event_signature"].get("display_title") or representative["title"])[:180],
            "aliases": sorted({row["title"] for row in videos_for_event if row["title"] != representative["title"]})[:20], "business_date": business_date,
            "published_at_min": min(row["published_at"] for row in videos_for_event), "published_at_max": max(row["published_at"] for row in videos_for_event),
            "source_lanes": lanes, "matched_keywords": sorted({word for row in videos_for_event for word in row["matched_keywords"]}),
            "contributing_videos": videos_for_event, "aggregate_interactions": aggregate_raw, "aggregate_interactions_semantics": "raw_sum_legacy_not_used_for_rank", "aggregate_interactions_raw": aggregate_raw,
            "effective_interactions": effective, "effective_interactions_basis": "highest_raw_interaction_sum_per_source_group", "matrix_deduplication": matrix_decisions,
            "video_count": len(videos_for_event), "video_count_raw": len(videos_for_event), "video_count_semantics": "raw_contributing_videos_legacy", "effective_video_count": len(source_group_keys),
            "account_count": len(raw_accounts), "account_count_semantics": "raw_distinct_accounts_legacy", "account_count_raw": len(raw_accounts), "source_group_count": len(source_group_keys), "source_group_keys": source_group_keys, "approved_account_ids": approved,
            "editorial_lanes": sorted({str(row.get("editorial_lane") or "") for row in videos_for_event if str(row.get("editorial_lane") or "")}),
            "event_signature": cluster["event_signature"], "clustering_basis": cluster["clustering_basis"],
            "clustering_confidence": cluster["clustering_confidence"], "clustering_decisions": cluster["clustering_decisions"],
            "related_story_ids": cluster["related_story_ids"], "related_story_relations": cluster["related_story_relations"],
            "content_angles": list(cluster["event_signature"].get("actions") or []),
            "truth_status": "not_checked", "confirmed_facts": [], "evidence_status": "not_checked", "disclaimer": DISCLAIMER,
            "image_status": {"attempted": False, "state": "not_ranked_top3"}, "images": [],
        })
    events = sorted(events, key=lambda row: (row["published_at_min"], row["event_id"]))
    overflow = max(0, len(events) - int(event_limit))
    if overflow:
        dropped.append({"video_id": "", "reason": f"聚类事件超过上限，后续按热度截断 {overflow} 条"})
    return events, dropped


def rank_candidate_events(events: list[dict[str, Any]], business_date: str, weights: dict[str, float]) -> list[dict[str, Any]]:
    end = datetime.fromisoformat(f"{business_date}T23:59:59+08:00")
    ranked: list[dict[str, Any]] = []
    for event in events:
        interactions = event["effective_interactions"]
        newest = datetime.fromisoformat(event["published_at_max"])
        age_hours = max(0.0, min(24.0, (end - newest).total_seconds() / 3600))
        components = {
            "like": _point(interactions["like"], weights["like"]), "comment": _point(interactions["comment"], weights["comment"]),
            "collect": _point(interactions["collect"], weights["collect"]), "share": _point(interactions["share"], weights["share"]),
            "freshness": round(weights["freshness"] * math.exp(-age_hours / 24.0), 3),
            "related_videos": round(weights["related_videos"] * min(1.0, math.log1p(event["effective_video_count"]) / math.log(11)), 3),
            "source_group_coverage": round(float(weights.get("source_group_coverage") if weights.get("source_group_coverage") is not None else weights.get("approved_account_coverage", 0.0)) * min(1.0, event["source_group_count"] / 3), 3),
            "cross_lane": round(weights["cross_lane"] if len(event["source_lanes"]) > 1 else 0.0, 3),
        }
        ranked.append({**event, "score_components": components, "heat_score": round(sum(components.values()), 3)})
    ranked.sort(key=lambda row: (-row["heat_score"], -row["effective_video_count"], row["published_at_max"], row["event_id"]))
    for index, event in enumerate(ranked, 1): event["rank"] = index
    return ranked


def plan_keywords(core: list[str], supplemental: list[str], *, event_count: int, stop_at_events: int) -> list[str]:
    return list(core) + ([] if event_count >= stop_at_events else list(supplemental))


def retain_per_keyword(records: list[Any], words: list[str], per_keyword_limit: int) -> tuple[list[Any], dict[str, tuple[int, int]]]:
    retained: list[Any] = []; counts: dict[str, tuple[int, int]] = {}
    for word in words:
        matching = sorted((item for item in records if item.source_keyword == word), key=lambda item: (str(item.published_at or ""), str(item.video_id)), reverse=True)
        kept = matching[:per_keyword_limit]; retained.extend(kept); counts[word] = (len(matching), len(kept))
    return retained, counts


def attach_official_douyin_signals(
    events: list[dict[str, Any]],
    config: dict[str, Any],
    local: dict[str, Any],
    *,
    business_date: str,
    collection_key: str,
    collector: Callable[..., dict[str, Any]] = collect_search,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, str]]]:
    """Attach bounded, query-scoped propagation evidence without changing heat."""
    settings = config["jobs"]["daily_hot_candidate_pool_v2"]["official_discovery"]
    errors: list[dict[str, str]] = []
    selected = [
        item for item in events
        if (not isinstance(item.get("audience_routing"), dict) or item["audience_routing"].get("lane") == "mainstream")
        and str(item.get("douyin_query") or "").strip()
    ][: int(settings["douyin_signal_max_events"])]
    for event in selected:
        card = event.get("editorial_card") if isinstance(event.get("editorial_card"), dict) else {}
        localized = str(card.get("title") or "").strip() if card.get("status") == "success" and card.get("locale") == "zh-CN" else ""
        if localized:
            event["douyin_query"] = localized[:48].strip(" -—:：，,。.")
    for event in events:
        signal = event.get("douyin_signal") if isinstance(event.get("douyin_signal"), dict) else {}
        event["douyin_signal"] = {**signal, "status": "not_attempted", "matched_video_count": 0, "raw_interactions": {"like": 0, "comment": 0, "collect": 0, "share": 0}, "videos": []}
    if not selected:
        return events, {"status": "not_attempted", "queries": [], "reason": "no_named_event_queries"}, errors
    queries = [str(item["douyin_query"]) for item in selected]
    per_event = int(settings["douyin_signal_per_event_limit"])
    try:
        collection = collector(local, len(queries) * per_event, f"{collection_key}-official-signals", keywords=queries, hard_max=len(queries) * per_event)
    except Exception as exc:
        collection = {"status": "failed", "files": [], "error": type(exc).__name__}
    if collection.get("status") not in {"success", "empty"}:
        error = str(collection.get("error") or "official_douyin_signal_search_failed")[:220]
        errors.append({"phase": "official_douyin_signals", "message": error})
        for event in selected:
            event["douyin_signal"]["status"] = "partial"
            event["douyin_signal"]["reason"] = error
        return events, {"status": "partial", "queries": queries, "error": error}, errors
    files = [Path(item) for item in collection.get("files") or []]
    records = normalize_files(files, local, "douyin_search") if files else []
    records, _ = filter_target_day(records, business_date)
    by_query: dict[str, list[Any]] = {query: [] for query in queries}
    for record in records:
        query = str(record.source_keyword or "")
        if query in by_query:
            by_query[query].append(record)
    for event in selected:
        query = str(event["douyin_query"])
        rows = sorted(by_query.get(query) or [], key=lambda item: (-sum(int(getattr(item, field) or 0) for field in ("digg_count", "comment_count", "collect_count", "share_count")), str(item.video_id)))[:per_event]
        interactions = {"like": sum(int(row.digg_count or 0) for row in rows), "comment": sum(int(row.comment_count or 0) for row in rows), "collect": sum(int(row.collect_count or 0) for row in rows), "share": sum(int(row.share_count or 0) for row in rows)}
        event["douyin_signal"] = {
            "status": "found" if rows else "not_found", "query": query, "matched_video_count": len(rows),
            "raw_interactions": interactions, "videos": [_video_row(row) for row in rows],
            "semantics": "same_day_query_scoped_propagation_signal_not_fact_confirmation",
        }
    return events, {"status": "success" if records else "empty", "queries": queries, "returned_target_day_videos": len(records)}, errors


def _reuse_verified_raw(root: Path, business_date: str, current_run_id: str) -> tuple[list[VideoRecord], str | None]:
    """Use only a prior published V2 raw-video record for the same day after live lanes fail."""
    packs = root / date_directory(business_date) / "packs"; best: tuple[list[VideoRecord], str] | None = None
    for pack in packs.iterdir() if packs.is_dir() else []:
        if pack.name == current_run_id:
            continue
        raw_path = pack / "raw-videos.json"; ready_path = pack / "_READY.json"
        try:
            ready = json.loads(ready_path.read_text(encoding="utf-8")); raw = json.loads(raw_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if ready.get("contract_version") not in SUPPORTED_V2_VERSIONS or raw.get("business_date") != business_date:
            continue
        rows: list[VideoRecord] = []
        for item in raw.get("videos") or []:
            interactions = item.get("interactions") or {}
            for lane in item.get("source_lanes") or []:
                rows.append(VideoRecord(video_id=str(item.get("video_id") or ""), title=str(item.get("title") or ""), account_id=str(item.get("account_id") or ""), account_name=str(item.get("author") or ""), share_url=str(item.get("share_url") or ""), published_at=str(item.get("published_at") or ""), source="douyin_search" if lane == "search" else "douyin_creator", source_keyword=str((item.get("matched_keywords") or [""])[0]), digg_count=int(interactions.get("like") or 0), comment_count=int(interactions.get("comment") or 0), collect_count=int(interactions.get("collect") or 0), share_count=int(interactions.get("share") or 0), production_role=str(item.get("production_role") or ""), source_group_id=str(item.get("source_group_id") or ""), source_group_name=str(item.get("source_group_name") or ""), editorial_lane=str(item.get("editorial_lane") or "")))
        if best is None or len(rows) > len(best[0]):
            best = (rows, pack.name)
    return best if best else ([], None)


def _reused_raw_evidence_files(config: dict[str, Any], business_date: str, reused_run_id: str | None) -> list[Path]:
    """Locate the matching project-owned raw capture for a reused V2 pack.

    The published ``raw-videos.json`` intentionally excludes temporary media
    URLs.  Reusing it for heat without reloading the associated sanitized raw
    capture would silently turn off OCR and text extraction for every reused
    story.  Only project-owned JSONL captures from the exact run token are
    eligible here; no browser profile, cookie or other authentication artifact
    is read.
    """
    if not reused_run_id:
        return []
    token = str(reused_run_id).rsplit("-", 1)[-1]
    if not re.fullmatch(r"[0-9a-f]{12}", token):
        return []
    capture_root = _path(config, "data/raw/mediacrawler/runs") / f"candidate-pool-{business_date}-{token}"
    return sorted(path for path in capture_root.rglob("*.jsonl") if path.is_file())


def should_reuse_same_day_raw(target_count: int, current_quality: dict[str, Any] | None, *, minimum: int = 10) -> bool:
    """Prefer a larger immutable same-day raw set when live coverage regresses."""
    current_target = int((current_quality or {}).get("target_day_videos") or 0)
    return int(target_count) < max(int(minimum), current_target)


def _topic_radar_accounts(config: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = (config.get("jobs") or {}).get("account_pool", {}).get("initial_candidates") or []
    result: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict) or item.get("production_role") != "topic_radar" or item.get("discovery_enabled") is not True or item.get("enabled") is not True or item.get("lifecycle_status") != "candidate":
            continue
        stable_id = str(item.get("profile_url") or "").rstrip("/").rsplit("/", 1)[-1]
        result.append({"id": str(item.get("id") or stable_id), "stable_id": stable_id, "name": str(item.get("display_name") or item.get("id") or stable_id), "url": str(item.get("profile_url") or ""), "category": "topic_radar", "enabled": True, "lifecycle_status": "candidate", "production_role": "topic_radar", "source_group_id": str(item.get("source_group_id") or ""), "source_group_name": str(item.get("source_group_name") or ""), "editorial_lane": str(item.get("editorial_lane") or "")})
    return result


def _annotate_account_metadata(records: list[VideoRecord], accounts: list[dict[str, Any]]) -> list[VideoRecord]:
    metadata = {str(item["id"]): item for item in accounts}
    for record in records:
        item = metadata.get(str(record.account_id))
        if item:
            record.production_role = str(item.get("production_role") or "")
            record.source_group_id = str(item.get("source_group_id") or "")
            record.source_group_name = str(item.get("source_group_name") or "")
            record.editorial_lane = str(item.get("editorial_lane") or "")
    return records


def _approved_account_config(config: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], set[str]]:
    local = copy.deepcopy(config)
    accounts = [dict(item) for item in config.get("benchmark_accounts") or [] if item.get("enabled", True)]
    approved_ids = {str(item["id"]) for item in accounts}
    for trusted in config.get("trusted_news_accounts") or []:
        if trusted.get("enabled", True) and trusted.get("approval_basis"):
            accounts.append({"id": str(trusted["id"]), "name": str(trusted.get("name") or trusted["id"]), "url": str(trusted["profile_url"]), "category": str(trusted.get("category") or "technology_news"), "enabled": True, "source_tier": "trusted"})
            approved_ids.add(str(trusted["id"]))
    accounts.extend(_topic_radar_accounts(config))
    local["benchmark_accounts"] = accounts
    local["media_crawler"] = {**local["media_crawler"], "close_owned_browser_on_completion": False}
    return local, accounts, approved_ids


def _image_for_top3(stage: Path, events: list[dict[str, Any]], settings: dict[str, Any], config: dict[str, Any], started: float) -> dict[str, Any]:
    budget = RequestBudget(int(settings["max_image_requests"]), int(settings["max_image_bytes"]), float(settings["image_total_seconds"]), time.monotonic())
    fetcher = SafeFetcher(settings, budget); seen_sha: set[str] = set(); seen_dhash: set[str] = set(); hints = settings.get("image_hints") or []
    attempts: list[dict[str, Any]] = []
    try:
        for event in events[:3]:
            status: dict[str, Any] = {"attempted": True, "state": "failed", "reason": "没有匹配到受控图片提示", "assets": []}
            if time.monotonic() - started > float(settings["max_wall_seconds"]):
                status.update(state="timed_out", reason="全局墙钟预算耗尽"); event["image_status"] = status; attempts.append({"event_id": event["event_id"], **status}); continue
            corpus = event["title"].casefold()
            hint = next((item for item in hints if all(str(term).casefold() in corpus for term in item.get("match_all") or [])), None)
            if hint:
                try:
                    url, content_type, body = fetcher.get(str(hint["image_url"]), maximum_bytes=int(settings["max_asset_bytes"]), accepted_types=("image/jpeg", "image/png", "image/webp"))
                    details = _decode_image(body, content_type, int(settings["min_dimension"]))
                    if max(details["width"], details["height"]) / min(details["width"], details["height"]) > float(settings["max_aspect_ratio"]): raise MaterialProbeError("图片过长，不适合作为候选主画面")
                    sha = hashlib.sha256(body).hexdigest()
                    if sha in seen_sha or details["dhash"] in seen_dhash: raise MaterialProbeError("图片与Top3其他素材重复")
                    relative = Path("images") / event["event_id"] / f"primary{details['extension']}"; destination = stage / relative; destination.parent.mkdir(parents=True, exist_ok=True); destination.write_bytes(body)
                    asset = {"role": "primary", "relative_path": relative.as_posix(), "image_source_url": redact_url(url), "source_article_url": redact_url(str(hint["source_article_url"])), "source_name": str(hint.get("source_name") or "受控图片来源"), "selection_reason": "仅因标题与受控视觉提示匹配而尝试；不构成事实核验。", "rights_status": "review_required", "mime_type": details["mime_type"], "width": details["width"], "height": details["height"], "bytes": len(body), "sha256": sha, "dhash": details["dhash"]}
                    seen_sha.add(sha); seen_dhash.add(details["dhash"]); event["images"] = [asset]; status.update(state="succeeded", reason=None, assets=[asset]);
                except (MaterialProbeError, OSError, ValueError) as exc:
                    status.update(reason=str(exc)[:220])
            event["image_status"] = status; attempts.append({"event_id": event["event_id"], **status})
        for event in events[3:]: event["image_status"] = {"attempted": False, "state": "not_ranked_top3", "assets": []}
        return {"attempts": attempts, "usage": budget.snapshot()}
    finally:
        fetcher.close()


def _markdown(pack: dict[str, Any]) -> str:
    lines = ["# 昨日抖音科技热点候选", "", f"> {DISCLAIMER}", "", f"业务日期：`{pack['business_date']}`　状态：`{pack['status']}`　候选：`{len(pack['candidates'])}`", "", "> 当前顺序是选题推荐顺序；`heat_rank` 和 `heat_score` 保留原始抖音热度，不受推荐权重影响。", ""]
    for event in pack["candidates"]:
        title = str(event.get("canonical_title") or event["title"])
        points = event.get("key_points") or []
        delivery_rank = int(event.get("delivery_rank") or event.get("rank") or 0)
        heat_rank = int(event.get("heat_rank") or event.get("rank") or 0)
        priority_score = float(event.get("delivery_priority_score") or event.get("heat_score") or 0)
        lines.extend([f"## {delivery_rank}. {title}", "", f"- 事件 ID：`{event['story_id']}`；聚类：`{event.get('clustering_confidence')}` / `{event.get('clustering_basis')}`", f"- 内容摘要：{event.get('event_summary') or title}", f"- 要点：{'；'.join(str(value) for value in points) or '仅有标题级线索'}", f"- 内容角度：{', '.join(event.get('content_angles') or []) or '未识别'}；提取：`{event.get('extraction_status') or 'not_run'}` / {', '.join(event.get('extraction_methods') or []) or '无'}", f"- 待核验主张：{'；'.join(event.get('claims_to_verify') or []) or '整条抖音线索均未由 copy_skill 核验'}", f"- 推荐分：`{priority_score}`；类型：`{event.get('content_category') or 'legacy_unclassified'}`；依据：{'；'.join(event.get('priority_reasons') or ['旧合同未计算推荐原因'])}", f"- 原始热度第 `{heat_rank}` 名，热度：`{event['heat_score']}`；分量：`{event['score_components']}`", f"- 推荐分量：`{event.get('priority_components') or {}}`", f"- 视频：{event['video_count']}；原始账号：{event['account_count_raw']}；独立运营主体：{event['source_group_count']}；赛道：{', '.join(event['editorial_lanes']) or '未标注'}", f"- 原始互动：`{event['aggregate_interactions_raw']}`；有效互动：`{event['effective_interactions']}`", f"- 矩阵去重：同 source_group 仅最高互动视频计入有效互动，全部视频仍保留；`{event['matrix_deduplication']}`", f"- 通道：{', '.join(event['source_lanes'])}；关键词仅作发现记录：{', '.join(event['matched_keywords']) or '无'}", f"- 相关但未合并事件：{', '.join(event.get('related_story_ids') or []) or '无'}", f"- 真实性：`not_checked`；{DISCLAIMER}", "- 贡献视频："])
        lines.extend(f"  - [{video['title']}]({video['share_url']})（{video['author']}，{video['published_at']}，赛道 {video.get('editorial_lane') or '未标注'}，矩阵 {video.get('source_group_name') or '独立账号'}，互动 {video['interactions']}）" for video in event["contributing_videos"])
        lines.append("- 图片：" + ("；".join(f"{asset['role']} `{asset['relative_path']}` {asset['width']}×{asset['height']}（review_required）" for asset in event["images"]) or f"{event['image_status']['state']}：{event['image_status'].get('reason') or '未尝试'}")); lines.append("")
    return "\n".join(lines)


def _op_rules_v2() -> str:
    return """# OP 每日素材读取规则（昨日抖音科技热点候选池 V2）

- OP 从 `output/每日新闻素材/YYYY-MM-DD_每日素材/current.json` 定位 READY 包并校验 manifest/hash；不得通过 run-id 猜测包。
- 人工查看应打开 READY 的 `human_brief`（`昨日热点图文简报.md`）；程序消费必须读取 `machine_contract`，不得解析人类简报。`technical_brief` 保存完整热度与聚类审计信息。
- `每日科技热点榜.md` 优先交付补全后的公共科技事件卡：先按同日定向抖音搜索信号排序，未命中的同分后备再按“重点公司 + 重大科技动作”的已审计投递分与公开来源明确性排序。公司名称不改变真实性，也不改写原始 `heat_rank`；完整证据、事实卡、优先级判定、缺口与真实性状态只在机器 JSON 中保留。
- `official-discovery.json` 是独立的官方/权威公开源事件清单；每项的 `source_refs` 负责来源归因，`douyin_signal` 只反映定向搜索到的同日传播线索。程序不得把抖音信号当作事实证明。
- `candidate-pool.json` 是长候选池，不是事实新闻清单。所有候选均为 `truth_status: not_checked`；OP 必须自行核验后才能播报。
- `news_readiness=ready` 只表示已从抖音证据中提取出主体、动作和对象，可进入“昨日科技新闻候选”；它不表示事实已经核验。
- 评测、实验、教程、观点、盘点及信息不足项保留在“高热度科技内容与待补充线索”，不得直接当作完整新闻，也不得因分流而丢失热度。
- 纯游戏新闻在事件层被排除，不进入候选、内容提取或配图；`scope_exclusions` 留存排除审计，原始视频仍在 `raw-videos.json`。
- `delivery_rank` / `rank` 是供 OP 选题的推荐顺序，综合原始热度 45%、战略意义 25%、公众关联 20%、讨论价值 10%，并对传闻、教程营销和含糊标题扣分。
- `heat_rank` / `heat_score` 是独立保存的原始抖音热度证据。推荐权重不改写原始热度；真实性、图片、模型摘要和人工判断都不参与两类分数。
- 顶层每一行是一个事件故事，不是一个视频；同事件视频、别名、角度和提取证据在故事内嵌套。`related_topic` 只表示相关，不能当作同一新闻。
- 新闻区优先读取 `news_headline`、`event_slots`、`event_completeness` 和 `event_evidence_refs`；`canonical_title` 仍是内容标签。模型失败会确定性降级，所有 `claims_to_verify` 仍需 OP 核验。
- `topic_radar` 是候选发现角色，不是事实或信任升级；矩阵内同事件视频全部保留，但有效互动和覆盖按 `source_group` 去重。
- 仅 Top 3 有受控图片尝试；`review_required` 不是授权。不得把无图候选删除或以视频/社交截图补图。
- 官方事件不会写入 `candidates`、`heat_ranking` 或 `delivery_ranking`，也不改变它们的原始互动量、热度和推荐分。来源失败或事件不足时热点榜可以少于十条，并应检查 `official-discovery.json` 的错误与预算记录。
- 同一业务日期只会把覆盖更好的同合同 V2 READY 包提升为 `current`；较少候选的后续 partial 包保留审计，但不会倒退 OP 可选池。
"""


def _v2_pack_quality(pack_dir: Path, business_date: str) -> dict[str, Any]:
    """Read a completed V2 pack without trusting its mutable-looking summary fields."""
    ready = json.loads((pack_dir / "_READY.json").read_text(encoding="utf-8"))
    if ready.get("contract_version") not in SUPPORTED_V2_VERSIONS or ready.get("business_date") != business_date or ready.get("run_id") != pack_dir.name:
        raise DailyMaterialExchangeError("不是同日 V2 READY 包")
    manifest_path = pack_dir / _relative(str(ready.get("manifest") or ""))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_sha = _sha256(manifest_path)
    if manifest_sha != ready.get("package_manifest_sha256"):
        raise DailyMaterialExchangeError("READY manifest SHA 不一致")
    _validate_manifest(pack_dir, manifest)
    contract = json.loads((pack_dir / _relative(str(ready.get("machine_contract") or ""))).read_text(encoding="utf-8"))
    if contract.get("schema") != "daily-hot-candidate-pool-v2" or contract.get("contract_version") not in SUPPORTED_V2_VERSIONS or contract.get("business_date") != business_date:
        raise DailyMaterialExchangeError("V2 机器合同不一致")
    if contract.get("contract_version") != ready.get("contract_version"):
        raise DailyMaterialExchangeError("V2 READY 与机器合同版本不一致")
    candidates = contract.get("candidates")
    if not isinstance(candidates, list):
        raise DailyMaterialExchangeError("V2 candidates 无效")
    report = contract.get("run_report") if isinstance(contract.get("run_report"), dict) else {}
    report_counts = report.get("counts") if isinstance(report.get("counts"), dict) else {}
    official_report = report.get("official_discovery") if isinstance(report.get("official_discovery"), dict) else {}
    official_errors = official_report.get("source_errors") if isinstance(official_report.get("source_errors"), list) else []
    official_events = contract.get("official_major_events") if isinstance(contract.get("official_major_events"), list) else []
    attempts = report.get("image_attempts") if isinstance(report.get("image_attempts"), list) else []
    image_successes = int(report_counts.get("top3_image_successes") or sum(item.get("state") == "succeeded" for item in attempts if isinstance(item, dict)))
    counts = contract.get("counts") if isinstance(contract.get("counts"), dict) else {}
    return {
        "run_id": pack_dir.name,
        "contract_version": str(contract.get("contract_version") or ready.get("contract_version") or ""),
        "business_date": business_date,
        "candidate_count": len(candidates),
        "target_day_videos": int(counts.get("target_day_videos") or report_counts.get("target_day_videos") or 0),
        "top3_image_successes": image_successes,
        "official_major_events": len(official_events),
        "official_source_errors": len(official_errors),
        "semantic_contract_revision": int(contract.get("semantic_contract_revision") or 0),
        "official_discovery_revision": int(contract.get("official_discovery_revision") or 0),
        "material_exchange_observability_revision": int(contract.get("material_exchange_observability_revision") or 0),
        "consumer_hot_list_revision": int(contract.get("consumer_hot_list_revision") or 0),
        "consumer_hot_list_card_count": int(((contract.get("consumer_hot_list") or {}).get("consumer_card_count")) or 0),
        "public_reader_hot_list_revision": int(contract.get("public_reader_hot_list_revision") or 0),
        "public_reader_hot_list_card_count": int(((contract.get("public_reader_hot_list") or {}).get("card_count")) or 0),
        "status": str(ready.get("status") or "partial"),
        "package_manifest_sha256": manifest_sha,
    }


def decide_v2_promotion(new_quality: dict[str, Any], current_quality: dict[str, Any] | None, *, override: bool = False) -> dict[str, Any]:
    """Apply the deterministic same-day non-regression order for V2 control pointers."""
    if override:
        return {"promotion_status": "promoted", "reason": "manual_override", "current_kept": None}
    if current_quality is None or current_quality.get("business_date") != new_quality.get("business_date"):
        return {"promotion_status": "promoted", "reason": "no_comparable_same_day_current", "current_kept": None}
    current_id = str(current_quality.get("run_id") or "")
    new_status = str(new_quality.get("status") or "partial")
    current_status = str(current_quality.get("status") or "partial")
    # Contract version changes never bypass completeness and status gates.  A
    # newer renderer is useful only when it has not replaced a healthy same-day
    # package with a partial one.  The symmetric recovery rule also lets an
    # older successful package recover from a mistakenly promoted newer partial
    # package without requiring a manual override.
    if new_status == "success" and current_status != "success":
        return {"promotion_status": "promoted", "reason": "run_status_recovered", "current_kept": None}
    if current_status == "success" and new_status != "success":
        return {"promotion_status": "rejected", "reason": "run_status_regressed", "current_kept": current_id}
    if int(new_quality.get("public_reader_hot_list_revision") or 0) > int(current_quality.get("public_reader_hot_list_revision") or 0) and int(new_quality.get("public_reader_hot_list_card_count") or 0) < 20:
        return {"promotion_status": "rejected", "reason": "public_reader_hot_list_incomplete", "current_kept": current_id}
    new_version = str(new_quality.get("contract_version") or "")
    current_version = str(current_quality.get("contract_version") or "")
    if new_version and current_version and new_version != current_version:
        try:
            is_forward_contract = float(new_version) > float(current_version)
        except ValueError:
            is_forward_contract = False
        if is_forward_contract and new_version in SUPPORTED_V2_VERSIONS and current_version in SUPPORTED_V2_VERSIONS:
            for field in ("candidate_count", "target_day_videos", "top3_image_successes"):
                if int(new_quality.get(field) or 0) < int(current_quality.get(field) or 0):
                    return {
                        "promotion_status": "rejected",
                        "reason": f"story_contract_upgrade_{field}_decreased",
                        "current_kept": str(current_quality.get("run_id") or ""),
                    }
            return {"promotion_status": "promoted", "reason": "story_contract_upgrade", "current_kept": None}
        return {"promotion_status": "rejected", "reason": "unsupported_contract_transition", "current_kept": str(current_quality.get("run_id") or "")}
    # A presentation/contract revision must never promote a less complete
    # official-source snapshot.  It remains in packs for audit, but OP keeps
    # the last known better same-day source coverage until a healthy rerun.
    if int(current_quality.get("official_discovery_revision") or 0) > 0:
        if int(new_quality.get("official_source_errors") or 0) > int(current_quality.get("official_source_errors") or 0):
            return {"promotion_status": "rejected", "reason": "official_source_errors_increased", "current_kept": current_id}
        if int(new_quality.get("official_major_events") or 0) < int(current_quality.get("official_major_events") or 0):
            return {"promotion_status": "rejected", "reason": "official_major_events_decreased", "current_kept": current_id}
        if int(new_quality.get("official_major_events") or 0) > int(current_quality.get("official_major_events") or 0):
            return {"promotion_status": "promoted", "reason": "official_major_events_increased", "current_kept": None}
        if int(new_quality.get("official_source_errors") or 0) < int(current_quality.get("official_source_errors") or 0):
            return {"promotion_status": "promoted", "reason": "official_source_errors_decreased", "current_kept": None}
    if int(new_quality.get("public_reader_hot_list_revision") or 0) > int(current_quality.get("public_reader_hot_list_revision") or 0):
        if int(new_quality.get("public_reader_hot_list_card_count") or 0) < 20:
            return {"promotion_status": "rejected", "reason": "public_reader_hot_list_incomplete", "current_kept": current_id}
        return {"promotion_status": "promoted", "reason": "public_reader_hot_list_revision_increased", "current_kept": None}
    if int(current_quality.get("public_reader_hot_list_revision") or 0) > 0 and int(new_quality.get("public_reader_hot_list_card_count") or 0) < int(current_quality.get("public_reader_hot_list_card_count") or 0):
        return {"promotion_status": "rejected", "reason": "public_reader_hot_list_card_count_decreased", "current_kept": current_id}
    if int(new_quality.get("consumer_hot_list_revision") or 0) > int(current_quality.get("consumer_hot_list_revision") or 0):
        if int(new_quality.get("consumer_hot_list_card_count") or 0) < 20:
            return {"promotion_status": "rejected", "reason": "consumer_hot_list_incomplete", "current_kept": current_id}
        return {"promotion_status": "promoted", "reason": "consumer_hot_list_revision_increased", "current_kept": None}
    if int(current_quality.get("consumer_hot_list_revision") or 0) > 0 and int(new_quality.get("consumer_hot_list_card_count") or 0) < int(current_quality.get("consumer_hot_list_card_count") or 0):
        return {"promotion_status": "rejected", "reason": "consumer_hot_list_card_count_decreased", "current_kept": current_id}
    comparisons = (
        ("candidate_count", "candidate_count_increased", "candidate_count_decreased"),
        ("target_day_videos", "target_day_videos_increased", "target_day_videos_decreased"),
        ("top3_image_successes", "top3_image_successes_increased", "top3_image_successes_decreased"),
        ("semantic_contract_revision", "semantic_contract_revision_increased", "semantic_contract_revision_decreased"),
        ("official_discovery_revision", "official_discovery_revision_increased", "official_discovery_revision_decreased"),
        ("material_exchange_observability_revision", "material_exchange_observability_revision_increased", "material_exchange_observability_revision_decreased"),
        ("consumer_hot_list_revision", "consumer_hot_list_revision_increased", "consumer_hot_list_revision_decreased"),
        ("public_reader_hot_list_revision", "public_reader_hot_list_revision_increased", "public_reader_hot_list_revision_decreased"),
    )
    for field, better, worse in comparisons:
        candidate_value, current_value = int(new_quality.get(field) or 0), int(current_quality.get(field) or 0)
        if candidate_value > current_value:
            return {"promotion_status": "promoted", "reason": better, "current_kept": None}
        if candidate_value < current_value:
            return {"promotion_status": "rejected", "reason": worse, "current_kept": current_id}
    return {"promotion_status": "rejected", "reason": "same_quality_not_promoted", "current_kept": current_id}


def _current_v2_quality(date_root: Path, business_date: str) -> dict[str, Any] | None:
    try:
        current = json.loads((date_root / "current.json").read_text(encoding="utf-8"))
        if current.get("contract_version") not in SUPPORTED_V2_VERSIONS or current.get("business_date") != business_date:
            return None
        return _v2_pack_quality(date_root / _relative(str(current.get("pack_relative_path") or "")), business_date)
    except (DailyMaterialExchangeError, OSError, ValueError, json.JSONDecodeError):
        return None


def _record_promotion(date_root: Path, *, run_id: str, decision: dict[str, Any], new_quality: dict[str, Any]) -> None:
    atomic_write_json(date_root / "promotion-decisions" / f"{run_id}.json", {"run_id": run_id, "new_quality": new_quality, **decision})


def _promote_v2_pointer(config: dict[str, Any], *, business_date: str, date_root: Path, quality: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    root = _path(config, config["jobs"]["daily_hot_candidate_pool_v2"]["output_root"])
    run_id = str(quality["run_id"])
    ready_path = date_root / "packs" / run_id / "_READY.json"
    ready = json.loads(ready_path.read_text(encoding="utf-8"))
    generated = str(ready.get("generated_at") or datetime.now(ZoneInfo(str(config["timezone"]))).isoformat(timespec="seconds"))
    pointer_version = str(quality.get("contract_version") or ready.get("contract_version") or V2_VERSION)
    atomic_write_json(date_root / "current.json", {"contract_version": pointer_version, "business_date": business_date, "status": quality["status"], "pack_relative_path": f"packs/{run_id}", "ready_relative_path": f"packs/{run_id}/_READY.json", "package_manifest_sha256": quality["package_manifest_sha256"], "updated_at": generated, "promotion": decision})
    atomic_write_json(root / "latest.json", {"contract_version": pointer_version, "business_date": business_date, "date_directory": date_directory(business_date), "current_relative_path": f"{date_directory(business_date)}/current.json", "status": quality["status"], "updated_at": generated})
    consumer = inspect_daily_material_exchange(config, business_date=business_date)
    atomic_write_json(date_root / "consumer-dry-run.json", {"mode": "root_plus_business_date", "result": consumer})
    return consumer


def promote_existing_v2_pack(config: dict[str, Any], *, business_date: str, run_id: str, override: bool = False) -> dict[str, Any]:
    """Offline-only repair/administration path for a READY pack; package contents stay immutable."""
    if not _SAFE_RUN_ID.fullmatch(run_id):
        raise DailyMaterialExchangeError("run_id 格式无效")
    root = _path(config, config["jobs"]["daily_hot_candidate_pool_v2"]["output_root"])
    date_root = root / date_directory(business_date)
    quality = _v2_pack_quality(date_root / "packs" / run_id, business_date)
    decision = decide_v2_promotion(quality, _current_v2_quality(date_root, business_date), override=override)
    _record_promotion(date_root, run_id=run_id, decision=decision, new_quality=quality)
    consumer: dict[str, Any] | None = None
    if decision["promotion_status"] == "promoted":
        consumer = _promote_v2_pointer(config, business_date=business_date, date_root=date_root, quality=quality, decision=decision)
    return {"business_date": business_date, "run_id": run_id, "new_quality": quality, **decision, "consumer": consumer}


def run_account_matrix_topic_radar_smoke(config: dict[str, Any], *, run_id: str | None = None, collector: Callable[[dict[str, Any], str], dict[str, Any]] = collect_creators) -> dict[str, Any]:
    """Bounded, account-only live proof for the three configured topic-radar candidates."""
    radar_accounts = _topic_radar_accounts(config)
    stamp = run_id or f"account-matrix-topic-radar-smoke-{uuid.uuid4().hex[:12]}"
    output_root = _path(config, "output/account-matrix-topic-radar-smoke") / stamp
    raw_root = _path(config, "data/raw/mediacrawler/runs/account-matrix-topic-radar-smoke")
    local = copy.deepcopy(config)
    local["benchmark_accounts"] = radar_accounts
    local["media_crawler"] = {**local["media_crawler"], "runs_output": str(raw_root), "close_owned_browser_on_completion": True}
    started = time.monotonic()
    try:
        collection = collector(local, stamp)
    except Exception as exc:
        collection = {"status": "failed", "accounts": [], "attempts": [{"account_id": item["id"], "returncode": None, "error": type(exc).__name__} for item in radar_accounts], "error": type(exc).__name__}
    raw_paths = [Path(path) for account in collection.get("accounts") or [] for path in account.get("files") or []]
    records = _annotate_account_metadata(normalize_files(raw_paths, local, "douyin_creator"), radar_accounts) if raw_paths else []
    by_id = {str(item["id"]): item for item in radar_accounts}
    attempts: list[dict[str, Any]] = []
    for raw in collection.get("attempts") or []:
        account_id = str(raw.get("account_id") or "")
        rows = [item for item in records if str(item.account_id) == account_id]
        code = raw.get("returncode")
        status = "timeout" if code == 124 else "failed" if code not in (None, 0) else "succeeded" if rows else "empty"
        item = by_id.get(account_id, {})
        attempts.append({"account_id": account_id, "account_name": item.get("name"), "status": status, "returncode": code, "timeout_seconds": raw.get("timeout_seconds"), "error": raw.get("error"), "raw_records": len(rows), "source_group_id": item.get("source_group_id"), "editorial_lane": item.get("editorial_lane")})
    missing = sorted(set(by_id) - {str(item["account_id"]) for item in attempts})
    attempts.extend({"account_id": account_id, "account_name": by_id[account_id]["name"], "status": "failed", "returncode": None, "error": "collector omitted configured account", "raw_records": 0, "source_group_id": by_id[account_id]["source_group_id"], "editorial_lane": by_id[account_id]["editorial_lane"]} for account_id in missing)
    status = "success" if attempts and all(item["status"] in {"succeeded", "empty"} for item in attempts) else "partial" if attempts else "failed"
    result = {"schema": "account-matrix-topic-radar-smoke-v1", "status": status, "run_id": stamp, "elapsed_seconds": round(time.monotonic() - started, 3), "accounts": attempts, "configured_topic_radar_accounts": [{key: item.get(key) for key in ("id", "stable_id", "name", "lifecycle_status", "production_role", "source_group_id", "source_group_name", "editorial_lane")} for item in radar_accounts], "records": [_video_row(item) for item in records], "browser": collection.get("browser"), "audio_asr_ocr_llm_images": 0, "full_keyword_search": False}
    _write_text(output_root / "account-matrix-topic-radar-smoke.md", "# 账号矩阵选题雷达 Smoke\n\n" + "\n".join(f"- {item['account_name']}：`{item['status']}`；赛道 `{item['editorial_lane']}`；原始记录 {item['raw_records']}" for item in attempts) + "\n")
    atomic_write_json(output_root / "account-matrix-topic-radar-smoke.json", result)
    if collector is collect_creators:
        close_project_browser(local)
    return {**result, "output_dir": str(output_root)}


def run_daily_hot_candidate_pool(config: dict[str, Any], *, business_date: str | None = None, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    settings = config["jobs"]["daily_hot_candidate_pool_v2"]; target = business_date or beijing_yesterday(); root = _path(config, settings["output_root"]); run_id = f"run-{target.replace('-', '')}-{uuid.uuid4().hex[:12]}"; stage = root / ".staging" / run_id; stage.mkdir(parents=True, exist_ok=False); started = clock(); state = JobState(config, "daily_hot_candidate_pool_v2")
    local, accounts, approved_ids = _approved_account_config(config); radar_accounts = [item for item in accounts if item.get("production_role") == "topic_radar"]; source_attempts: list[dict[str, Any]] = []; keyword_attempts: list[dict[str, Any]] = []; errors: list[dict[str, str]] = []; warnings: list[dict[str, str]] = []; raw_paths: list[Path] = []; raw_content = RawContentIndex()
    collection_key = f"candidate-pool-{target}-{run_id.rsplit('-', 1)[-1]}"
    try:
        with JobLock(config, "daily_hot_candidate_pool_v2"):
            state.update(status="running", phase="official_discovery", target_date=target, counts={"approved_accounts": len(approved_ids), "topic_radar_accounts": len(radar_accounts), "raw_records": 0, "events": 0, "official_events": 0}, errors=[])
            try:
                configured_articles, official_source_errors, official_usage = fetch_official_sources(config)
                errors.extend({"phase": "official_discovery", "message": f"{item['source']}: {item['error']}"} for item in official_source_errors)
            except Exception as exc:
                configured_articles = []
                official_source_errors = [{"source": "official_discovery", "error": type(exc).__name__}]
                official_usage = {"request_count": 0, "downloaded_bytes": 0, "status": "failed"}
                errors.append({"phase": "official_discovery", "message": type(exc).__name__})
            official_elapsed = round(clock() - started, 3)
            try:
                public_web_articles, public_web_by_url, public_web_report = fetch_public_web_discovery(config)
                for query in public_web_report.get("queries") or []:
                    if isinstance(query, dict) and query.get("status") != "success":
                        errors.append({"phase": "public_web_discovery", "message": f"{query.get('query_id')}: {query.get('error') or 'failed'}"})
            except Exception as exc:
                public_web_articles, public_web_by_url = [], {}
                public_web_report = {"provider": "google_news_rss", "query_count": 0, "success_count": 0, "failure_count": 1, "queries": [], "usage": {"status": "failed"}, "error": type(exc).__name__}
                errors.append({"phase": "public_web_discovery", "message": type(exc).__name__})
            official_articles = [*configured_articles, *public_web_articles]
            official_events, official_excluded = build_official_major_events(
                official_articles,
                business_date=target,
                maximum=int(settings["official_discovery"]["max_events"]),
                company_priority=settings["official_discovery"]["company_event_priority"],
                reader_editorial=settings["official_discovery"]["reader_editorial"],
                public_web_discovery_by_url=public_web_by_url,
            )
            phase_elapsed = {"official_discovery": official_elapsed, "public_web_discovery": round(clock() - started - official_elapsed, 3)}
            try:
                account_run = collect_creators(local, collection_key, before_sanitize=raw_content.capture_files)
                for attempt in account_run.get("attempts") or []:
                    files = [Path(path) for path in (attempt.get("sanitization") or []) if False]
                    source_attempts.append({"account_id": attempt["account_id"], "status": "attempted", "returncode": attempt.get("returncode"), "timeout_seconds": attempt.get("timeout_seconds"), "error": attempt.get("error"), "raw_count": 0, "date_count": 0})
                raw_paths.extend(Path(path) for account in account_run.get("accounts") or [] for path in account.get("files") or [])
            except Exception as exc:
                errors.append({"phase": "approved_accounts", "message": type(exc).__name__})
                source_attempts.extend({"account_id": str(item["id"]), "status": "failed", "error": type(exc).__name__, "raw_count": 0, "date_count": 0} for item in accounts)
            raw_account_records = _annotate_account_metadata(normalize_files(raw_paths, local, "douyin_creator"), accounts) if raw_paths else []
            for attempt in source_attempts:
                account_rows = [row for row in raw_account_records if str(row.account_id) == attempt["account_id"]]
                attempt["raw_count"] = len(account_rows)
                attempt["date_count"] = sum(1 for row in account_rows if str(row.published_at or "").startswith(target))
                if attempt["status"] == "attempted":
                    attempt["status"] = "failed" if attempt.get("returncode") not in (None, 0) else "succeeded" if attempt["date_count"] else "empty"
            core = list(settings["core_keywords"]); supplemental = list(settings["supplemental_keywords"]); global_limit = int(settings["max_raw_records"])
            # Reserve capacity so a prolific approved account can never suppress a required core keyword.
            account_limit = max(0, global_limit - len(core) * int(settings["per_keyword_limit"]))
            records = raw_account_records[:account_limit]
            if len(raw_account_records) > len(records):
                # This is an intentional deterministic budget trim, not a source failure.
                for attempt in source_attempts:
                    attempt["record_budget"] = {"account_records_total": len(raw_account_records), "account_records_retained_before_required_search": len(records)}
            phase_elapsed["approved_accounts"] = round(clock() - started - sum(phase_elapsed.values()), 3)

            def within_deadline(phase: str) -> bool:
                if clock() - started <= float(settings["max_wall_seconds"]):
                    return True
                errors.append({"phase": phase, "message": "global wall-clock budget exhausted"})
                return False

            def search(words: list[str]) -> None:
                nonlocal records
                if not words or len(records) >= global_limit or not within_deadline("search"):
                    return
                try:
                    collection = collect_search(local, min(global_limit - len(records), len(words) * int(settings["per_keyword_limit"])), f"{collection_key}-{len(keyword_attempts)}", keywords=words, hard_max=global_limit, before_sanitize=raw_content.capture_files)
                except Exception as exc:
                    collection = {"status": "failed", "files": [], "error": type(exc).__name__}
                paths = [Path(item) for item in collection.get("files") or []]; found = normalize_files(paths, local, "douyin_search") if paths else []
                retained, counts_by_word = retain_per_keyword(found, words, int(settings["per_keyword_limit"])); _annotate_account_metadata(retained, accounts)
                for word in words:
                    discovered_count, retained_count = counts_by_word[word]
                    keyword_attempts.append({"keyword": word, "status": "attempted" if collection.get("status") in {"success", "empty"} else "failed", "discovered_count": discovered_count, "retained_count": retained_count, "raw_count": retained_count, "error": collection.get("error")})
                records.extend(retained)
                if collection.get("status") not in {"success", "empty"}: errors.append({"phase": "search", "message": str(collection.get("error") or "搜索未完成")[:220]})
            state.update(phase="core_keywords", counts={"approved_accounts": len(approved_ids), "topic_radar_accounts": len(radar_accounts), "raw_records": len(records), "events": 0}); search(core); phase_elapsed["core_keywords"] = round(clock() - started - phase_elapsed["approved_accounts"], 3)
            target_records, excluded = filter_target_day(records, target); events, dropped = cluster_candidate_videos(target_records, target, approved_accounts=approved_ids, event_limit=int(settings["max_events"]))
            if len(events) < int(settings["supplemental_stop_events"]):
                for word in supplemental:
                    if len(records) >= global_limit or len(events) >= int(settings["supplemental_stop_events"]): break
                    search([word]); target_records, excluded = filter_target_day(records, target); events, dropped = cluster_candidate_videos(target_records, target, approved_accounts=approved_ids, event_limit=int(settings["max_events"]))
            phase_elapsed["supplemental_keywords"] = round(clock() - started - sum(phase_elapsed.values()), 3)
            reused_from: str | None = None
            date_root = root / date_directory(target)
            current_quality = _current_v2_quality(date_root, target)
            if should_reuse_same_day_raw(len(target_records), current_quality):
                reusable, reused_from = _reuse_verified_raw(root, target, run_id)
                if reusable:
                    records = _annotate_account_metadata(reusable, accounts)
                    raw_content.capture_files(_reused_raw_evidence_files(config, target, reused_from))
                    target_records, excluded = filter_target_day(records, target)
                    events, dropped = cluster_candidate_videos(target_records, target, approved_accounts=approved_ids, event_limit=int(settings["max_events"]))
                    errors.append({"phase": "raw_reuse", "message": f"live target-day coverage不足，复用同日已验证原始记录：{reused_from}"})
            weights = {key: float(value) for key, value in settings["weights"].items()}
            heat_ranked = rank_candidate_events(events, target, weights)[:int(settings["max_events"])]
            ranked, scope_exclusions = filter_technology_scope(heat_ranked)
            state.update(phase="story_enrichment", counts={"approved_accounts": len(approved_ids), "topic_radar_accounts": len(radar_accounts), "raw_records": len(records), "events": len(ranked)})
            content_workspace = stage / ".content-work"
            public_details = PublicDetailDiscovery(
                settings["public_detail_discovery"],
                deadline=time.monotonic() + max(0.0, float(settings["max_wall_seconds"]) - (clock() - started)),
                business_date=target,
                timezone=str(config["timezone"]),
            )
            try:
                ranked, content_result = enrich_ranked_stories(
                    ranked, raw_content, local, content_workspace,
                    global_deadline=time.monotonic() + max(0.0, float(settings["max_wall_seconds"]) - (clock() - started)),
                    public_detail_provider=public_details,
                ) if within_deadline("story_enrichment") else (ranked, {"status": "budget_exhausted", "raw_content_index": raw_content.safe_stats()})
            except Exception as exc:
                errors.append({"phase": "story_enrichment", "message": type(exc).__name__})
                content_result = {"status": "failed", "error": type(exc).__name__, "raw_content_index": raw_content.safe_stats()}
            finally:
                shutil.rmtree(content_workspace, ignore_errors=True)
            public_detail_report = public_details.report()
            ranked = prioritize_for_delivery(ranked, settings["editorial_priority"])
            semantic_summary = semantic_counts(ranked)
            phase_elapsed["story_enrichment"] = round(clock() - started - sum(phase_elapsed.values()), 3)
            state.update(phase="consumer_hot_list", counts={"approved_accounts": len(approved_ids), "topic_radar_accounts": len(radar_accounts), "raw_records": len(records), "events": len(ranked)})
            consumer_settings = settings["consumer_hot_list"]
            consumer_analyzer = OpenAICompatibleAnalyzer(config)
            consumer_status = consumer_analyzer.status()

            def _generate_consumer_card(system: str, prompt: str, max_tokens: int) -> dict[str, Any]:
                return consumer_analyzer.chat_json_once(system, prompt, max_output_tokens=max_tokens)

            ranked, consumer_hot_list = build_consumer_hot_list(
                ranked,
                maximum=int(consumer_settings["target_count"]),
                batch_size=int(consumer_settings["llm_batch_size"]),
                max_output_tokens=int(consumer_settings["max_output_tokens"]),
                generate=_generate_consumer_card if consumer_status["enabled"] and consumer_status["api_key_configured"] else None,
                deadline=time.monotonic() + max(0.0, float(settings["max_wall_seconds"]) - (clock() - started)),
            )
            consumer_hot_list["model_available"] = bool(consumer_status["enabled"] and consumer_status["api_key_configured"])
            phase_elapsed["consumer_hot_list"] = round(clock() - started - sum(phase_elapsed.values()), 3)
            state.update(phase="public_article_details", counts={"approved_accounts": len(approved_ids), "topic_radar_accounts": len(radar_accounts), "raw_records": len(records), "events": len(ranked), "official_events": len(official_events)})
            if within_deadline("public_article_details"):
                official_events, public_article_detail_report = enrich_public_event_details(official_events, config)
                official_events = apply_public_reader_policy(official_events, settings["official_discovery"]["reader_editorial"])
            else:
                public_article_detail_report = {"selected_events": 0, "success_count": 0, "failure_count": 0, "errors": [], "usage": {"status": "budget_exhausted"}}
            phase_elapsed["public_article_details"] = round(clock() - started - sum(phase_elapsed.values()), 3)
            state.update(phase="official_brief_localization", counts={"approved_accounts": len(approved_ids), "topic_radar_accounts": len(radar_accounts), "raw_records": len(records), "events": len(ranked), "official_events": len(official_events)})
            localization_settings = settings["official_discovery"]["brief_localization"]
            analyzer = OpenAICompatibleAnalyzer(config)
            analyzer_status = analyzer.status()

            def _generate_official_card(system: str, prompt: str, max_tokens: int) -> dict[str, Any]:
                return analyzer.chat_json_once(system, prompt, max_output_tokens=max_tokens)

            generator = _generate_official_card if analyzer_status["enabled"] and analyzer_status["api_key_configured"] else None
            official_events, official_editorial = localize_official_event_cards(
                official_events,
                enabled=bool(localization_settings["enabled"]),
                maximum=int(localization_settings["max_events"]),
                max_output_tokens=int(localization_settings["max_output_tokens"]),
                generate=generator,
            )
            official_editorial["model_available"] = bool(analyzer_status["enabled"] and analyzer_status["api_key_configured"])
            if bool(localization_settings["enabled"]) and official_editorial.get("status") != "success":
                # A few rejected cards remain visible in the machine audit, but
                # they do not invalidate a package that still has twenty fully
                # source-bound reader cards.  Treat this as a delivery warning,
                # not a false all-or-nothing run failure.
                warnings.append({"phase": "official_brief_localization", "message": f"{official_editorial.get('status')}: 部分中文卡片已回退，未进入人读热榜"})
            phase_elapsed["official_brief_localization"] = round(clock() - started - sum(phase_elapsed.values()), 3)
            state.update(phase="official_douyin_signals", counts={"approved_accounts": len(approved_ids), "topic_radar_accounts": len(radar_accounts), "raw_records": len(records), "events": len(ranked), "official_events": len(official_events)})
            if within_deadline("official_douyin_signals"):
                official_events, official_signal_report, official_signal_errors = attach_official_douyin_signals(
                    official_events, config, local, business_date=target, collection_key=collection_key
                )
                errors.extend(official_signal_errors)
            else:
                official_signal_report = {"status": "budget_exhausted", "queries": []}
            phase_elapsed["official_douyin_signals"] = round(clock() - started - sum(phase_elapsed.values()), 3)
            public_reader_hot_list = build_public_reader_hot_list(official_events, maximum=int(consumer_settings["target_count"]))
            if public_reader_hot_list["status"] != "success":
                errors.append({"phase": "public_reader_hot_list", "message": f"complete_cards:{public_reader_hot_list['card_count']}/{public_reader_hot_list['target_count']}"})
            public_web_candidate_pool = build_public_web_candidate_pool(
                official_events,
                business_date=target,
                target_count=int(settings["public_web_discovery"]["target_count"]),
            )
            if public_web_candidate_pool["status"] != "success":
                errors.append({"phase": "public_web_candidate_pool", "message": f"auditable_candidates:{public_web_candidate_pool['selected_count']}/{public_web_candidate_pool['target_count']}"})
            state.update(phase="top3_images", counts={"approved_accounts": len(approved_ids), "topic_radar_accounts": len(radar_accounts), "raw_records": len(records), "events": len(ranked)})
            try:
                image_result = _image_for_top3(stage, ranked, settings, local, started) if within_deadline("top3_images") else {"attempts": []}
            except Exception as exc:
                errors.append({"phase": "top3_images", "message": type(exc).__name__}); image_result = {"attempts": []}
            phase_elapsed["top3_images"] = round(clock() - started - sum(phase_elapsed.values()), 3)
            elapsed = round(clock() - started, 3); required_search_failed = any(item["status"] != "attempted" for item in keyword_attempts if item["keyword"] in core); blocking_errors = [item for item in errors if item.get("phase") != "raw_reuse"]; status = "success" if public_reader_hot_list["status"] == "success" and not blocking_errors and not required_search_failed and elapsed <= float(settings["max_wall_seconds"]) else "empty" if not ranked and not errors else "partial"
            generated = datetime.now(ZoneInfo(str(config["timezone"]))).isoformat(timespec="seconds")
            new_quality = {"run_id": run_id, "contract_version": V2_VERSION, "semantic_contract_revision": 5, "official_discovery_revision": 9, "public_web_discovery_revision": 1, "material_exchange_observability_revision": 1, "consumer_hot_list_revision": 1, "consumer_hot_list_card_count": int(consumer_hot_list["consumer_card_count"]), "public_reader_hot_list_revision": 2, "public_reader_hot_list_card_count": int(public_reader_hot_list["card_count"]), "public_web_candidate_pool_revision": 1, "public_web_candidate_pool_count": int(public_web_candidate_pool["selected_count"]), "business_date": target, "candidate_count": len(ranked), "target_day_videos": len(target_records), "top3_image_successes": sum(item.get("state") == "succeeded" for item in image_result["attempts"]), "official_major_events": len(official_events), "official_source_errors": len(official_source_errors), "status": status}
            promotion = decide_v2_promotion(new_quality, _current_v2_quality(date_root, target))
            run_counts = {
                "approved_accounts": len(approved_ids), "topic_radar_accounts": len(radar_accounts),
                "raw_records": len(records), "target_day_videos": len(target_records), "candidates": len(ranked),
                "scope_excluded": len(scope_exclusions), "top3_image_attempts": len(image_result["attempts"]),
                "top3_image_successes": new_quality["top3_image_successes"],
                "images": sum(len(item["images"]) for item in ranked),
                "stories_with_content_evidence": sum(bool(item.get("content_evidence")) for item in ranked),
                "news_ready": semantic_summary["news_ready"],
                "headline_quality_failures": semantic_summary["headline_quality_failures"],
                "news_readiness": semantic_summary["news_readiness"],
                "content_types": semantic_summary["content_types"],
                "consumer_hot_list_fact_ready": int(consumer_hot_list["fact_ready_count"]), "consumer_hot_list_cards": int(consumer_hot_list["consumer_card_count"]),
                "public_reader_hot_list_cards": int(public_reader_hot_list["card_count"]), "public_reader_industry_brief": int(public_reader_hot_list.get("industry_brief_count") or 0),
                "official_articles": len(configured_articles), "public_web_articles": len(public_web_articles), "official_major_events": len(official_events), "official_source_errors": len(official_source_errors), "public_web_candidates": int(public_web_candidate_pool["selected_count"]),
            }
            pack_counts = {
                "candidates": len(ranked), "scope_excluded": len(scope_exclusions), "raw_records": len(records),
                "target_day_videos": len(target_records), "images": sum(len(row["images"]) for row in ranked),
                "fact_ready": 0, "stories_with_primary": sum(bool(row["images"]) for row in ranked),
                "stories_with_content_evidence": sum(bool(row.get("content_evidence")) for row in ranked),
                "news_ready": semantic_summary["news_ready"],
                "headline_quality_failures": semantic_summary["headline_quality_failures"],
                "news_readiness": semantic_summary["news_readiness"],
                "content_types": semantic_summary["content_types"],
                "consumer_hot_list_fact_ready": int(consumer_hot_list["fact_ready_count"]), "consumer_hot_list_cards": int(consumer_hot_list["consumer_card_count"]),
                "public_reader_hot_list_cards": int(public_reader_hot_list["card_count"]), "public_reader_industry_brief": int(public_reader_hot_list.get("industry_brief_count") or 0),
                "official_articles": len(configured_articles), "public_web_articles": len(public_web_articles), "official_major_events": len(official_events), "official_source_errors": len(official_source_errors), "public_web_candidates": int(public_web_candidate_pool["selected_count"]),
            }
            run_report = {"schema": "daily-hot-candidate-pool-v2", "business_date": target, "run_id": run_id, "status": status, "stage_elapsed_seconds": {**phase_elapsed, "total": elapsed}, "budget": {key: settings[key] for key in ("max_wall_seconds", "per_keyword_limit", "max_raw_records", "max_events", "image_total_seconds")} | {"story_enrichment": settings.get("story_enrichment") or {}, "public_detail_discovery": settings.get("public_detail_discovery") or {}, "consumer_hot_list": consumer_settings, "official_discovery": settings.get("official_discovery") or {}}, "counts": run_counts, "account_attempts": source_attempts, "account_matrix": [{key: item.get(key) for key in ("id", "stable_id", "name", "lifecycle_status", "production_role", "source_group_id", "source_group_name", "editorial_lane")} for item in radar_accounts], "candidate_accounts_excluded": False, "candidate_accounts_excluded_semantics": "explicit topic_radar candidates are included; ordinary candidates remain excluded", "ordinary_candidate_accounts_excluded": True, "topic_radar_candidates_included": len(radar_accounts), "matrix_deduplication_rule": "same source_group keeps all provenance but only its highest raw-interaction video contributes to effective_interactions and related-video heat", "story_consolidation_rule": "named anchors plus conflict guards; search keywords excluded; non-chaining membership", "technology_scope_rule": "pure game stories excluded before OCR, LLM and image work", "news_semantic_rule": "news delivery requires evidence-linked subject, concrete action and object; readiness never verifies truth or changes heat", "consumer_hot_list_rule": "the legacy Douyin-only list remains evidence-first; the public reader list needs attributed, dated event text and is never padded", "public_detail_rule": "direct configured publisher feeds and Google News RSS supply bounded, dated public discovery; only an allowlisted article detail may enter the reader list, and it never verifies candidate truth or alters heat", "official_discovery_rule": "official and public news indexes discover attributed events; source publication time is never inferred as a product-first-launch date; reader routing separates mainstream cards from research, preview and industry items without altering candidate heat or truth", "public_reader_hot_list": public_reader_hot_list, "scope_exclusions": scope_exclusions, "story_enrichment": content_result, "public_detail_discovery": public_detail_report, "consumer_hot_list": consumer_hot_list, "official_discovery": {"usage": official_usage, "source_errors": official_source_errors, "excluded": official_excluded, "article_details": public_article_detail_report, "signal": official_signal_report, "brief_localization": official_editorial}, "keyword_attempts": keyword_attempts, "raw_reused_from": reused_from, "excluded_records": excluded, "dedupe_cluster_notes": dropped, "image_attempts": image_result["attempts"], "fact_verification_requests": 0, "rank4_plus_image_requests": 0, "llm_calls": int((content_result.get("llm") or {}).get("attempted_batches") or 0) + int(consumer_hot_list.get("attempted_batches") or 0) + int(official_editorial.get("request_count") or 0), "ocr_calls": int((content_result.get("media") or {}).get("ocr_calls") or 0), "asr_calls": int((content_result.get("media") or {}).get("asr_calls") or 0), "errors": errors, "warnings": warnings, "browser_policy": "single project CDP browser reused and closed after run", "promotion": promotion}
            run_report["budget"]["public_web_discovery"] = settings.get("public_web_discovery") or {}
            run_report["public_web_discovery_rule"] = "bounded Google News RSS query matrix discovers attributed leads; search results do not establish facts or web heat, and the separate candidate pool ranks source strength, company impact and public relevance"
            run_report["public_web_discovery"] = public_web_report
            run_report["public_web_candidate_pool"] = public_web_candidate_pool
            heat_ranking = [{"heat_rank": row["heat_rank"], "story_id": row["story_id"], "heat_score": row["heat_score"]} for row in sorted(ranked, key=lambda row: row["heat_rank"])]
            delivery_ranking = [{"delivery_rank": row["delivery_rank"], "story_id": row["story_id"], "delivery_priority_score": row["delivery_priority_score"]} for row in ranked]
            pack = {"schema": "daily-hot-candidate-pool-v2", "contract_version": V2_VERSION, "story_contract": "story-consolidation-content-editorial-v3", "official_discovery_contract": "official-major-events-v3-mainstream-reader", "producer": "copy_skill", "business_date": target, "generated_at": generated, "run_id": run_id, "status": status, "truth_status": "not_checked", "disclaimer": DISCLAIMER, "candidates": ranked, "stories": ranked, "official_major_events": official_events, "consumer_hot_list": consumer_hot_list, "public_reader_hot_list": public_reader_hot_list, "heat_ranking": heat_ranking, "delivery_ranking": delivery_ranking, "scope_exclusions": scope_exclusions, "counts": pack_counts, "formula": {"heat": {"version": "v2.1-deterministic-heat", "weights": weights, "stable_sort": ["heat_score desc", "effective_video_count desc", "published_at_max asc", "event_id asc"], "excluded_inputs": ["truth_status", "image_status", "LLM", "content_enrichment", "editorial_judgment", "news_readiness", "official_major_events"]}, "delivery_priority": {"version": "v2.2-deterministic-editorial-priority", "weights": settings["editorial_priority"]["weights"], "penalties": ["risk", "tutorial_promotion", "low_specificity"], "stable_sort": ["delivery_priority_score desc", "heat_rank asc", "event_id asc"], "excluded_inputs": ["truth_status", "image_status", "LLM", "content_enrichment", "human_editorial_judgment", "news_readiness", "official_major_events"]}}, "run_report": run_report, "openmontage_modified": False}
            pack["official_discovery_contract"] = "official-major-events-v4-public-web-discovery"
            pack["public_web_candidate_pool"] = public_web_candidate_pool
            for ranking in pack["formula"].values():
                ranking["excluded_inputs"].append("public_web_candidate_pool")
            pack["semantic_contract_revision"] = 5
            pack["official_discovery_revision"] = 9
            pack["public_web_discovery_revision"] = 1
            pack["material_exchange_observability_revision"] = 1
            pack["consumer_hot_list_revision"] = 1
            pack["public_reader_hot_list_revision"] = 2
            human_brief = render_human_brief(pack)
            technical_brief = _markdown(pack)
            consumer_brief = render_consumer_hot_list(pack)
            target_day_articles = [item.to_dict() for item in official_articles if str(item.published_at or "").startswith(target)]
            exclusion_counts: dict[str, int] = {}
            for item in official_excluded:
                reason = str(item.get("reason") or "unknown")
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
            official_artifact = {
                "schema": "official-major-events-v4-public-web-discovery",
                "business_date": target,
                "source_article_count": len(official_articles),
                "configured_source_article_count": len(configured_articles),
                "public_web_article_count": len(public_web_articles),
                "target_day_articles": target_day_articles,
                "events": official_events,
                "excluded_counts": exclusion_counts,
                "excluded_samples": official_excluded[:20],
                "source_errors": official_source_errors,
                "usage": official_usage,
                "public_web_discovery": public_web_report,
                "article_details": public_article_detail_report,
                "signal": official_signal_report,
                "brief_localization": official_editorial,
            }
            public_web_brief = render_public_web_candidate_pool(public_web_candidate_pool)
            atomic_write_json(stage / "candidate-pool.json", pack)
            atomic_write_json(stage / "raw-videos.json", {"business_date": target, "videos": [_video_row(item) for item in target_records], "excluded": excluded})
            atomic_write_json(stage / "official-discovery.json", official_artifact)
            atomic_write_json(stage / "全网选题候选池.json", public_web_candidate_pool)
            atomic_write_json(stage / "run-report.json", run_report)
            _write_text(stage / "昨日抖音科技热点候选.md", technical_brief)
            atomic_write_json(stage / "daily-material-pack.json", pack)
            _write_text(stage / "昨日热点图文简报.md", human_brief)
            _write_text(stage / "昨日热点素材简报.md", human_brief)
            _write_text(stage / "每日科技热点榜.md", consumer_brief)
            _write_text(stage / "全网选题候选池.md", public_web_brief)
            _write_text(root / "OP每日素材读取规则.md", _op_rules_v2())
            atomic_write_json(stage / "consumer-dry-run.json", {"mode": "prepublish_contract_check", "business_date": target, "run_id": run_id})
            atomic_write_json(stage / "manifest.json", {"schema": "v2-pointer", "candidate_pool": "candidate-pool.json"})
            manifest = _write_manifest(stage)
            manifest_sha = _sha256(stage / "package-manifest.json")
            new_quality["package_manifest_sha256"] = manifest_sha
            ready = {
                "contract_version": V2_VERSION,
                "producer": "copy_skill",
                "business_date": target,
                "run_id": run_id,
                "generated_at": generated,
                "status": status,
                "human_brief": "昨日热点图文简报.md",
                "consumer_hot_list": "每日科技热点榜.md",
                "major_event_brief": "每日科技热点榜.md",
                "technical_brief": "昨日抖音科技热点候选.md",
                "public_web_candidate_pool": "全网选题候选池.md",
                "public_web_candidate_machine": "全网选题候选池.json",
                "official_discovery": "official-discovery.json",
                "machine_contract": "candidate-pool.json",
                "manifest": "package-manifest.json",
                "package_manifest_sha256": manifest_sha,
                "counts": pack["counts"],
                "missing": errors,
                "promotion": promotion,
                "openmontage_modified": False,
            }
            atomic_write_json(stage / "_READY.json", ready)
            _validate_manifest(stage, manifest)
            destination = date_root / "packs" / run_id; destination.parent.mkdir(parents=True, exist_ok=True); os.replace(stage, destination); _record_promotion(date_root, run_id=run_id, decision=promotion, new_quality=new_quality); consumer = _promote_v2_pointer(config, business_date=target, date_root=date_root, quality=new_quality, decision=promotion) if promotion["promotion_status"] == "promoted" else None; phase = "published" if consumer is not None else "published_not_promoted"; state.update(status=status, phase=phase, output_path=str(destination / "昨日热点图文简报.md"), counts=pack["counts"], errors=errors); return {"status": status, "business_date": target, "run_id": run_id, "output_dir": str(destination), "brief_path": str(destination / "昨日热点图文简报.md"), "technical_brief_path": str(destination / "昨日抖音科技热点候选.md"), "counts": pack["counts"], "promotion": promotion, "run_report": run_report}
    finally:
        close_project_browser(local)
