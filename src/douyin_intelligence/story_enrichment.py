"""Bounded, secret-safe content enrichment for story-level candidate packs."""

from __future__ import annotations

import json
import copy
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .llm_analysis import OpenAICompatibleAnalyzer
from .materials import download_video
from .news_semantics import (
    deterministic_semantics,
    finalize_story_semantics,
    semantic_counts,
    semantic_detail_present,
    validate_model_semantics,
)
from .normalize import load_raw_records
from .story_consolidation import display_title
from .trusted_news import _run_asr
from .visual_ocr import VisualBatchBudget, process_visual_video


_NUMBER = re.compile(r"\d+(?:\.\d+)?%?")
_CONTENT_KEYS = ("platform_caption", "caption", "subtitle", "subtitle_text", "video_caption", "desc", "content", "title")
_MEDIA_KEYS = ("video_download_url", "video_url", "download_addr")
_MAX_PERSISTED_EVIDENCE_CHARS = 20_000
_FORBIDDEN_KEY_FRAGMENTS = ("cookie", "authorization", "download_addr", "video_download_url", "video_url", "signed_url")
_FORBIDDEN_TOKEN_KEYS = {"token", "access_token", "api_token", "auth_token", "bearer_token"}


def _video_id(raw: dict[str, Any]) -> str:
    nested = raw.get("aweme_info") if isinstance(raw.get("aweme_info"), dict) else {}
    return str(raw.get("aweme_id") or raw.get("video_id") or raw.get("item_id") or raw.get("id") or nested.get("aweme_id") or "").strip()


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized in _FORBIDDEN_TOKEN_KEYS or any(fragment in normalized for fragment in _FORBIDDEN_KEY_FRAGMENTS):
                return True
            if _contains_forbidden_key(item):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _flatten_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        rows: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                rows.append(item.strip())
            elif isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip():
                rows.append(item["text"].strip())
        return rows
    return []


def _https_media(raw: dict[str, Any]) -> str:
    for key in _MEDIA_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.startswith("https://"):
            return value
        if isinstance(value, dict):
            for nested_key in ("url", "uri", "play_addr", "download_addr"):
                nested = value.get(nested_key)
                if isinstance(nested, str) and nested.startswith("https://"):
                    return nested
            for nested in value.get("url_list") or []:
                if isinstance(nested, str) and nested.startswith("https://"):
                    return nested
    return ""


@dataclass(slots=True)
class RawContentIndex:
    """Ephemeral pre-sanitization text/media index; never serialize ``_rows``."""

    _rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    captured_records: int = 0
    records_with_media: int = 0
    records_with_extended_text: int = 0

    def capture_files(self, files: list[Path], _account: dict[str, Any] | None = None) -> None:
        for path in files:
            for raw in load_raw_records(path):
                video_id = _video_id(raw)
                if not video_id:
                    continue
                texts: list[str] = []
                sources: list[str] = []
                for key in _CONTENT_KEYS:
                    for value in _flatten_text(raw.get(key)):
                        if value not in texts:
                            texts.append(value[:100_000])
                            sources.append(key)
                media_url = _https_media(raw)
                previous = self._rows.get(video_id) or {"texts": [], "text_sources": [], "media_url": ""}
                combined = list(previous["texts"])
                combined_sources = list(previous["text_sources"])
                for source, value in zip(sources, texts):
                    if value not in combined:
                        combined.append(value)
                        combined_sources.append(source)
                self._rows[video_id] = {
                    "texts": combined,
                    "text_sources": combined_sources,
                    "media_url": str(previous.get("media_url") or media_url),
                }
                self.captured_records += 1
                self.records_with_media += int(bool(media_url))
                self.records_with_extended_text += int(any(len(value) >= 80 for value in texts))

    def get(self, video_id: str) -> dict[str, Any]:
        return self._rows.get(str(video_id), {"texts": [], "text_sources": [], "media_url": ""})

    def safe_stats(self) -> dict[str, int]:
        return {
            "captured_records": self.captured_records,
            "unique_video_ids": len(self._rows),
            "records_with_media": self.records_with_media,
            "records_with_extended_text": self.records_with_extended_text,
        }


def _interaction_total(video: dict[str, Any]) -> int:
    return sum(int((video.get("interactions") or {}).get(key) or 0) for key in ("like", "comment", "collect", "share"))


def _group_key(video: dict[str, Any]) -> str:
    return str(video.get("source_group_id") or "").strip() or f"account:{video.get('account_id') or video.get('video_id')}"


def _best_text(video: dict[str, Any], raw_index: RawContentIndex) -> tuple[str, str]:
    raw = raw_index.get(str(video.get("video_id") or ""))
    candidates = [(str(value).strip(), str(source)) for value, source in zip(raw.get("texts") or [], raw.get("text_sources") or []) if str(value).strip()]
    candidates.append((str(video.get("title") or "").strip(), "sanitized_title"))
    return max(candidates, key=lambda item: (len(item[0]), item[0]), default=("", "unavailable"))


def select_representative_videos(
    story: dict[str, Any], raw_index: RawContentIndex, *, minimum_chars: int, allow_supporting: bool,
    max_evidence_videos: int = 2,
) -> list[dict[str, Any]]:
    videos = sorted(
        story.get("contributing_videos") or [],
        key=lambda row: (-_interaction_total(row), str(row.get("published_at") or ""), str(row.get("video_id") or "")),
    )
    if not videos:
        return []
    first = videos[0]
    result = [{"video": first, "selection_reason": "highest_effective_interactions"}]
    first_text, _ = _best_text(first, raw_index)
    if not allow_supporting or max_evidence_videos <= 1:
        return result
    first_group = _group_key(first)
    primary_ready = len(first_text) >= minimum_chars and semantic_detail_present(first_text)
    alternatives = [row for row in videos[1:] if _group_key(row) != first_group]
    alternatives.sort(key=lambda row: (
        not semantic_detail_present(_best_text(row, raw_index)[0]),
        -len(_best_text(row, raw_index)[0]),
        -_interaction_total(row),
        str(row.get("video_id") or ""),
    ))
    for supporting in alternatives:
        if len(result) >= max_evidence_videos:
            break
        supporting_text, _ = _best_text(supporting, raw_index)
        if primary_ready:
            keep = len(supporting_text) >= minimum_chars and semantic_detail_present(supporting_text)
            reason = "cross_video_detail_evidence"
        else:
            keep = len(supporting_text) >= max(minimum_chars, len(first_text) + 30)
            reason = "primary_text_insufficient_supporting_source"
        if keep:
            result.append({"video": supporting, "selection_reason": reason})
    return result


def _sentences(text: str) -> list[str]:
    cleaned = re.sub(r"#[^#\s]+", " ", str(text or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    rows = [value.strip(" ，,。 ") for value in re.split(r"(?<=[。！？!?])|\n+", cleaned) if value.strip()]
    unique: list[str] = []
    fingerprints: set[str] = set()
    for value in rows:
        fingerprint = re.sub(r"\W+", "", value).casefold()
        if len(fingerprint) < 4 or fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        unique.append(value[:300])
    return unique


def _normalized_evidence_text(value: Any) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(value or "")).casefold()


def _deterministic_summary(story: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    title = display_title(str(story.get("canonical_title") or story.get("title") or "科技热点"))
    rows: list[str] = []
    for item in evidence:
        rows.extend(_sentences(str(item.get("text") or "")))
    if not rows:
        rows = [title]
    summary = rows[0]
    if len(summary) < 35 and len(rows) > 1:
        summary = f"{summary}。{rows[1]}"
    key_points = rows[:4]
    claims = [value for value in key_points if _NUMBER.search(value)]
    angles = sorted({str(value) for value in (story.get("content_angles") or []) if str(value)})
    return {
        "canonical_title": title[:120],
        "event_summary": summary[:500],
        "key_points": key_points,
        "content_angles": angles,
        "claims_to_verify": claims[:10],
        "summary_source": "deterministic_evidence_extract",
    }


class StoryMediaExtractor:
    def __init__(self, config: dict[str, Any], settings: dict[str, Any], workspace: Path, deadline: float):
        self.config = config
        self.settings = settings
        self.workspace = workspace
        self.deadline = deadline
        visual = config["jobs"]["trusted_account_news"]["visual_ocr"]
        self.visual_budget = VisualBatchBudget(
            soft_limit=min(int(visual["batch_soft_frame_limit"]), int(settings.get("ocr_frame_soft_limit") or 160)),
            hard_limit=min(int(visual["batch_hard_frame_limit"]), int(settings.get("ocr_frame_hard_limit") or 220)),
            deadline=min(deadline, time.monotonic() + float(settings["media_total_seconds"])),
        )
        self.media_attempts = 0
        self.ocr_calls = 0
        self.asr_calls = 0
        self.asr_deadline = min(deadline, time.monotonic() + float(settings["asr_total_seconds"]))

    def __call__(self, video_id: str, media_url: str) -> dict[str, Any]:
        if not media_url.startswith("https://"):
            return {"status": "unavailable", "method": "title_only", "text": "", "reason": "no_temporary_media"}
        if self.media_attempts >= int(self.settings["max_media_videos"]) or time.monotonic() >= self.deadline:
            return {"status": "budget_exhausted", "method": "title_only", "text": "", "reason": "media_budget_exhausted"}
        self.media_attempts += 1
        item_root = self.workspace / f"video-{re.sub(r'[^0-9A-Za-z_.-]+', '-', video_id)}"
        video_path = item_root / "source.mp4"
        item_root.mkdir(parents=True, exist_ok=True)
        try:
            media_config = copy.deepcopy(self.config)
            media_config["materials"]["download_retries"] = 1
            media_config["materials"]["download_timeout_seconds"] = min(
                int(media_config["materials"].get("download_timeout_seconds") or 180),
                int(self.settings["per_media_timeout_seconds"]),
                max(1, int(self.deadline - time.monotonic())),
            )
            download_video(media_url, video_path, media_config)
            visual_settings = dict(self.config["jobs"]["trusted_account_news"]["visual_ocr"])
            visual_settings["per_video_timeout_seconds"] = min(
                int(visual_settings["per_video_timeout_seconds"]), int(self.settings["per_media_timeout_seconds"])
            )
            self.ocr_calls += 1
            evidence = process_visual_video(video_path, item_root / "visual", visual_settings, self.visual_budget)
            text = str(evidence.get("merged_text") or "").strip()[:100_000]
            if len(text) >= int(self.settings["min_evidence_chars"]):
                return {
                    "status": "success" if evidence.get("quality_tier") == "success" else "partial",
                    "method": "screen_ocr", "text": text,
                    "quality": str(evidence.get("quality_tier") or evidence.get("visual_text_status") or "partial"),
                    "selected_frames": int(evidence.get("selected_frames") or 0),
                }
            if self.asr_calls >= int(self.settings["max_asr_videos"]) or time.monotonic() >= self.asr_deadline:
                return {"status": "partial", "method": "screen_ocr", "text": text, "reason": "ocr_insufficient_asr_budget_exhausted"}
            self.asr_calls += 1
            asr_config = copy.deepcopy(self.config)
            asr_config["jobs"]["trusted_account_news"]["asr_timeout_seconds"] = min(
                int(asr_config["jobs"]["trusted_account_news"].get("asr_timeout_seconds") or 300),
                int(self.settings["per_asr_timeout_seconds"]),
                max(1, int(self.asr_deadline - time.monotonic())),
            )
            asr = _run_asr(video_path, asr_config, item_root / "asr", str(self.settings["config_path"]))
            asr_text = str(asr.get("text") or "").strip()[:100_000]
            if len(asr_text) >= int(self.settings["min_evidence_chars"]):
                return {"status": "success", "method": "local_asr", "text": asr_text, "ocr_text": text[:2000]}
            return {"status": "partial", "method": "screen_ocr", "text": text, "reason": str(asr.get("error") or "ocr_and_asr_insufficient")[:200]}
        except Exception as exc:
            return {"status": "unavailable", "method": "title_only", "text": "", "reason": f"media_processing_failed:{type(exc).__name__}"}
        finally:
            shutil.rmtree(item_root, ignore_errors=True)


def _apply_llm_batches(
    stories: list[dict[str, Any]], analyzer: Any, settings: dict[str, Any], deadline: float,
) -> dict[str, Any]:
    report = {"attempted_batches": 0, "successful_batches": 0, "rejected_items": 0, "errors": []}
    if not stories or not getattr(analyzer, "status", lambda: {"enabled": True})().get("enabled", True):
        return report
    batch_size = max(1, int(settings["llm_batch_size"]))
    maximum_batches = max(0, int(settings["llm_max_batches"]))
    for offset in range(0, min(len(stories), batch_size * maximum_batches), batch_size):
        if time.monotonic() >= deadline:
            report["errors"].append("global_deadline_exhausted")
            break
        batch = stories[offset:offset + batch_size]
        source: list[dict[str, Any]] = []
        source_by_id: dict[str, str] = {}
        for story in batch:
            content = "\n".join(str(item.get("text") or "") for item in story.get("content_evidence") or [])
            row = {
                "story_id": story["story_id"], "title": story.get("canonical_title") or story.get("title"),
                "evidence": content[:6000], "angles": story.get("content_angles") or [],
                "published_at": story.get("published_at_max") or story.get("published_at_min") or "",
            }
            source.append(row)
            source_by_id[story["story_id"]] = json.dumps(row, ensure_ascii=False)
        schema = (
            '{"items":[{"story_id":"","canonical_title":"","event_summary":"","key_points":[""],'
            '"content_angles":[""],"claims_to_verify":[""],'
            '"content_type":"news_lead|creator_review|creator_experiment|tutorial|opinion|roundup|project_showcase|mixed|uncertain",'
            '"event_slots":{"subject":"","action":"","object":"","time_text":"",'
            '"event_status":"released|announced|upcoming|tested|rumored|discussed|unknown","result_or_change":""}}]}'
        )
        system = (
            "你是抖音科技热点事件结构整理员。只能归纳输入的title、evidence和published_at，不联网、不核真、不补充事实。"
            "先区分具体新闻与评测、实验、教程、观点、盘点、项目展示。subject/action/object/time_text只能填写输入中明确出现的文字；"
            "action只能使用发布、开源、推出、上线、更新、升级、宣布、回应、获批、完成、启动、定档、实测、测试、体验、测评、部署之一。"
            "不要用热议、来了、炸锅、相关消息、最新进展充当事件动作。数字、日期、规格、价格、性能和公司表态必须列入claims_to_verify。"
            "canonical_title只作内容标签，最终新闻标题由程序从通过校验的事件字段生成。严格输出JSON。"
        )
        prompt = f"每个story_id必须原样返回且只出现一次。输出结构：{schema}\n{json.dumps(source, ensure_ascii=False)}"
        report["attempted_batches"] += 1
        try:
            payload = analyzer.chat_json_once(system, prompt, max_output_tokens=3600)
        except Exception as exc:
            report["errors"].append(type(exc).__name__)
            continue
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            report["errors"].append("invalid_items")
            continue
        applied = 0
        by_id = {story["story_id"]: story for story in batch}
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                report["rejected_items"] += 1
                continue
            story_id = str(item.get("story_id") or "")
            if story_id not in by_id or story_id in seen:
                report["rejected_items"] += 1
                continue
            output_numbers = set(_NUMBER.findall(json.dumps(item, ensure_ascii=False)))
            source_numbers = set(_NUMBER.findall(source_by_id[story_id]))
            if output_numbers - source_numbers:
                report["rejected_items"] += 1
                continue
            required = ("canonical_title", "event_summary", "key_points", "content_angles", "claims_to_verify", "content_type", "event_slots")
            if any(key not in item for key in required):
                report["rejected_items"] += 1
                continue
            story = by_id[story_id]
            semantics, semantic_flags = validate_model_semantics(item, story.get("content_evidence") or [])
            if semantics is None:
                report["rejected_items"] += 1
                report.setdefault("semantic_rejections", []).append({"story_id": story_id, "flags": semantic_flags})
                continue
            story.update({
                "canonical_title": str(item["canonical_title"])[:120],
                "event_summary": str(item["event_summary"])[:500],
                "key_points": [str(value)[:300] for value in item["key_points"] if str(value).strip()][:5] if isinstance(item["key_points"], list) else story["key_points"],
                "content_angles": [str(value)[:120] for value in item["content_angles"] if str(value).strip()][:8] if isinstance(item["content_angles"], list) else story["content_angles"],
                "claims_to_verify": [str(value)[:300] for value in item["claims_to_verify"] if str(value).strip()][:12] if isinstance(item["claims_to_verify"], list) else story["claims_to_verify"],
                "summary_source": "llm_evidence_summary",
            })
            deterministic_slots = story.get("event_slots") if isinstance(story.get("event_slots"), dict) else {}
            model_slots = semantics.get("event_slots") if isinstance(semantics.get("event_slots"), dict) else {}
            semantics["event_slots"] = {
                key: (model_slots.get(key) if str(model_slots.get(key) or "").strip() else deterministic_slots.get(key, ""))
                for key in ("subject", "action", "object", "time_text", "event_status", "result_or_change")
            }
            if story.get("source_intent_type") in {"creator_review", "creator_experiment", "tutorial", "opinion", "roundup"}:
                semantics["content_type"] = story["source_intent_type"]
            story.update(semantics)
            seen.add(story_id)
            applied += 1
        report["successful_batches"] += int(applied > 0)
    return report


def enrich_ranked_stories(
    stories: list[dict[str, Any]], raw_index: RawContentIndex, config: dict[str, Any], workspace: Path,
    *, global_deadline: float, analyzer: Any | None = None,
    media_extractor: Callable[[str, str], dict[str, Any]] | None = None,
    public_detail_provider: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    settings = config["jobs"]["daily_hot_candidate_pool_v2"].get("story_enrichment") or {}
    if not settings.get("enabled", True):
        return stories, {"status": "disabled", "raw_content_index": raw_index.safe_stats()}
    detailed_limit = int(settings["max_detailed_stories"])
    selected_video_budget = int(settings["max_selected_videos"])
    supporting_budget = int(settings["max_supporting_videos"])
    selected_count = 0
    supporting_count = 0
    default_extractor = None
    if media_extractor is None:
        default_extractor = StoryMediaExtractor(config, settings, workspace, global_deadline)
        media_extractor = default_extractor
    detailed: list[dict[str, Any]] = []
    for index, story in enumerate(stories):
        allow_detailed = index < detailed_limit and selected_count < selected_video_budget and time.monotonic() < global_deadline
        selections = select_representative_videos(
            story, raw_index, minimum_chars=int(settings["min_description_chars"]),
            allow_supporting=allow_detailed and supporting_count < supporting_budget,
            max_evidence_videos=int(settings.get("max_evidence_videos") or 2),
        ) if allow_detailed else []
        if len(selections) > 1:
            supporting_count += len(selections) - 1
        selected_count += len(selections)
        evidence: list[dict[str, Any]] = []
        for selection in selections:
            video = selection["video"]
            text, source = _best_text(video, raw_index)
            item = {
                "video_id": str(video.get("video_id") or ""), "author": str(video.get("author") or ""),
                "selection_reason": selection["selection_reason"], "method": "platform_text" if source != "sanitized_title" else "title",
                "status": "success" if len(text) >= int(settings["min_evidence_chars"]) else "partial", "text": text[:_MAX_PERSISTED_EVIDENCE_CHARS],
            }
            raw = raw_index.get(item["video_id"])
            semantic_incomplete = not semantic_detail_present(text)
            # A long title can still be only a hook.  Detail collection must
            # follow semantic completeness, not character count; otherwise a
            # verbose but content-free title bypasses OCR entirely.
            needs_media = source == "sanitized_title" or semantic_incomplete
            if needs_media and raw.get("media_url") and time.monotonic() < global_deadline:
                media = media_extractor(item["video_id"], str(raw["media_url"]))
                media_text = str(media.get("text") or "").strip()
                if media_text and (semantic_incomplete or len(media_text) > len(item["text"])):
                    item.update({key: value for key, value in media.items() if key != "media_url"})
                    item["text"] = media_text[:_MAX_PERSISTED_EVIDENCE_CHARS]
            evidence.append(item)
        if not evidence:
            fallback_video = (story.get("contributing_videos") or [{}])[0]
            evidence = [{
                "video_id": str(fallback_video.get("video_id") or ""), "author": str(fallback_video.get("author") or ""),
                "selection_reason": "outside_detailed_budget" if index >= detailed_limit else "no_video",
                "method": "title", "status": "partial", "text": str(story.get("title") or "")[:_MAX_PERSISTED_EVIDENCE_CHARS],
            }]
        story_title = str(story.get("title") or story.get("canonical_title") or "").strip()
        combined_evidence = "\n".join(str(item.get("text") or "") for item in evidence)
        has_source_intent_evidence = any(
            str(item.get("method") or "") in {"title", "story_title", "platform_text", "local_asr"}
            for item in evidence
        )
        if (
            story_title
            and (not has_source_intent_evidence or not semantic_detail_present(combined_evidence))
            and _normalized_evidence_text(story_title) not in _normalized_evidence_text(combined_evidence)
        ):
            representative = (story.get("contributing_videos") or [{}])[0]
            evidence.append({
                "video_id": str(representative.get("video_id") or ""),
                "author": str(representative.get("author") or ""),
                "selection_reason": "story_title_context",
                "method": "story_title",
                "status": "partial",
                "text": story_title[:_MAX_PERSISTED_EVIDENCE_CHARS],
            })
        if allow_detailed and public_detail_provider is not None and time.monotonic() < global_deadline:
            public_result = public_detail_provider(story)
            public_evidence = public_result.get("evidence") if isinstance(public_result, dict) else []
            public_audit = public_result.get("audit") if isinstance(public_result, dict) else None
            if isinstance(public_evidence, list):
                evidence.extend(item for item in public_evidence if isinstance(item, dict))
            if isinstance(public_audit, dict):
                story["public_detail_discovery"] = public_audit
        story["content_evidence"] = evidence
        story["representative_video"] = evidence[0]["video_id"] if evidence else None
        story["supporting_videos"] = [item["video_id"] for item in evidence[1:]]
        story["extraction_methods"] = sorted({str(item.get("method") or "unavailable") for item in evidence})
        story["extraction_status"] = "success" if any(item.get("status") == "success" for item in evidence) else "partial"
        story.update(_deterministic_summary(story, evidence))
        story.update(deterministic_semantics(story, evidence))
        finalize_story_semantics(story)
        if allow_detailed:
            detailed.append(story)

    llm = analyzer or OpenAICompatibleAnalyzer(config)
    llm_report = _apply_llm_batches(detailed, llm, settings, global_deadline)
    for story in stories:
        finalize_story_semantics(story)
    if _contains_forbidden_key(stories):
        raise RuntimeError("story enrichment output contains a forbidden temporary field")
    media_report = {
        "media_attempts": int(getattr(default_extractor, "media_attempts", 0)),
        "ocr_calls": int(getattr(default_extractor, "ocr_calls", 0)),
        "asr_calls": int(getattr(default_extractor, "asr_calls", 0)),
    }
    statuses = {str(story.get("extraction_status") or "partial") for story in stories}
    return stories, {
        "status": "success" if statuses == {"success"} else "partial",
        "detailed_stories": len(detailed), "selected_videos": selected_count, "supporting_videos": supporting_count,
        "raw_content_index": raw_index.safe_stats(), "media": media_report, "llm": llm_report,
        "semantics": semantic_counts(stories),
    }
