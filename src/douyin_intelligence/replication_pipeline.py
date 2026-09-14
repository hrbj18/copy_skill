"""End-to-end orchestration for the material-replication workflow.

The runner wires candidate collection, selection, script extraction, clip
export and delivery publication, applying a per-phase wall-clock budget.
Every external effect is injectable through :class:`ReplicationDeps`, so the
whole flow can be exercised offline in tests.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .exporter import atomic_write_json
from .face_metrics import FACE_FREE, FACE_UNAVAILABLE, FaceDetector
from .media_processing import faster_whisper_status
from .media_tools import media_tool_available, resolve_media_tool
from .mediacrawler_patch import duration_patch_status
from .replication_candidates import Candidate, collect_candidate_pool
from .replication_clips import ClipInterval, build_clip_metadata, derive_face_free_intervals, export_video_clips, remove_tree
from .replication_delivery import (
    DELIVERY_README,
    FOLDER_MAIN,
    FOLDER_PROCESS,
    FOLDER_SCRIPT,
    FOLDER_SOURCE,
    FOLDER_SUPPORT,
    MANIFEST_NAME,
    build_manifest,
    ensure_delivery_tree,
    publish_directory,
    render_delivery_readme,
    validate_delivery_manifest,
)
from .replication_script import build_script_skeleton, write_script_artifacts
from .replication_selection import (
    DownloadBudget,
    REPLICATION_VIDEO_SUBDIR,
    file_size,
    invoke_downloader,
    is_video_candidate,
    material_replication_settings,
    measure_transferred_bytes,
    measured_duration_window_reject,
    prefilter_active,
    prefilter_candidates,
    prefilter_drop_non_video,
    prefilter_exclude_terms,
    prefilter_settings,
    ranked_candidates,
    relevance_report,
    select_material_replicas,
    select_script_replica,
    validate_probe,
)
from .replication_theme import delivery_folder_name, project_path, sanitize_theme, subject_terms
from .replication_validation import (
    build_validation_block,
    record_validation,
    validate_candidate,
    validation_enabled,
    validation_reason,
    write_validation_artifact,
)
from .replication_visual import verify_videos, visual_verify_settings


_FACE_RANK = {FACE_FREE: 0, "low_face": 1, "face_heavy": 2, FACE_UNAVAILABLE: 3}
_USAGE_TAGS = ("开场钩子", "要点画面", "演示对比", "结论画面", "补充画面", "互动画面")
_SUGGESTED_USE = ("hook", "key_points[1]", "demo_or_compare", "key_points[2]", "conclusion", "cta")


@dataclass
class ReplicationDeps:
    """Injectable collaborators; ``None`` falls back to the real implementation."""

    collector: Any = None
    downloader: Any = None
    prober: Any = None
    transcriber: Any = None
    ocr: Any = None
    face_detector: Any = None
    slicer: Any = None
    clock: Any = None
    #: Whole-download-validation effect.  ``None`` runs the real ffprobe+ffmpeg
    #: layer; a callable replaces it entirely (offline tests).
    validator: Any = None
    #: Bundle passed straight to ``replication_visual.verify_videos(deps=...)``;
    #: ``None`` runs the real ffmpeg + RapidOCR path.  It is a *separate* bundle
    #: and not ``ocr``: that one is a ``KeyframeOCR``-style object with ``run()``,
    #: while the visual gate wants a plain ``ocr(frame_path) -> str``.
    visual: Any = None


def _now_iso(config: dict[str, Any]) -> str:
    return datetime.now(ZoneInfo(str(config.get("timezone") or "Asia/Shanghai"))).isoformat(timespec="seconds")


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.replace(temporary, path)
    except Exception:
        if temporary and temporary.exists():
            temporary.unlink()
        raise


def _scoring_payload(
    theme: str,
    keywords: list[str],
    candidates: list[Candidate],
    *,
    keywords_requested: list[str] | None = None,
) -> dict[str, Any]:
    # ``keywords`` are the keywords actually searched; the full expansion is
    # kept separately so the scoring artifact cannot over-claim coverage either.
    requested = list(keywords_requested) if keywords_requested is not None else list(keywords)
    ordered = sorted(candidates, key=lambda item: item.heat_rank or 10**9)
    return {
        "schema_version": 1,
        "theme": theme,
        "keywords": keywords,
        "keywords_requested": requested,
        "keywords_truncated": len(requested) > len(keywords),
        "pool_size": len(candidates),
        "candidates": [
            {
                "video_id": candidate.video_id,
                "heat_score": candidate.heat_score,
                "heat_rank": candidate.heat_rank,
                "duration_seconds": candidate.duration_seconds,
                "author": candidate.author,
                "media_url_present": candidate.media_url_present,
            }
            for candidate in ordered
        ],
    }


def _phase_expired(start: float, seconds: float, clock: Callable[[], float]) -> bool:
    return (clock() - start) >= float(seconds)


def _source_copy_name(candidate: Candidate) -> str:
    author = sanitize_theme(candidate.author, max_length=20) or "作者"
    title = sanitize_theme(candidate.title, max_length=20) or "作品"
    return f"{author}_{title}_{candidate.video_id}.mp4"


def _visual_verify_payload(
    config: dict[str, Any],
    ordered: list[dict[str, Any]],
    theme: str,
    deps: "ReplicationDeps | None",
    warnings: list[str],
) -> dict[str, Any] | None:
    """Run the automatic visual gate over the *selected* source videos.

    Returns the payload for ``05-过程数据/visual_verify.json``, or ``None`` when the
    gate is switched off: off has to stay byte-for-byte equivalent to the feature
    not existing, which includes "no artifact appears in the delivery".

    A ``conclusive=False`` result never removes material (a product name that only
    appears on screen without text is a normal case) and never touches the
    top-level ``degraded`` -- that flag already carries "relevance could not
    discriminate the pool", and folding a second, unrelated signal into it would
    destroy the distinction.  It is surfaced as a warning plus a manifest block.
    """
    if not visual_verify_settings(config).get("enabled", False):
        return None

    paths: list[Path] = []
    seen: set[str] = set()
    for item in ordered:
        video_id = str(item["candidate"].video_id)
        if video_id in seen:
            continue
        seen.add(video_id)
        paths.append(Path(item["video_path"]))
    # ``subject_terms`` already case-folds; ``verify_videos`` compares substrings
    # against case-folded text, so the vocabulary is passed through as-is.
    terms = [term.casefold() for term in subject_terms(theme, config)]
    try:
        payload = verify_videos(paths, terms, config, deps=getattr(deps, "visual", None))
    except Exception as exc:  # ``verify_videos`` promises not to raise; belt and braces
        warnings.append(f"视觉确认未能完成：{type(exc).__name__}: {str(exc)[:160]}")
        return {"enabled": True, "conclusive": False, "items": [], "error": type(exc).__name__}

    if not payload["conclusive"]:
        warnings.append(
            f"视觉确认不结论：{len(payload['items'])} 条源片均未在画面文字中命中主体词"
        )
    return payload


def _visual_verdicts(payload: dict[str, Any] | None) -> dict[str, str]:
    """``video_id -> verdict`` for the rows of ``material_replica_sources``."""
    if not payload:
        return {}
    return {
        str(item.get("video_id")): str(item.get("verdict"))
        for item in payload.get("items") or []
    }


def _prefilter_config_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    """The effective prefilter knobs, recorded verbatim in the audit artifacts.

    ``enabled`` is the *raw* config switch and ``exclude_only`` says whether the
    run was the exclude-only mode (``enabled`` false yet the exclude gate still
    ran because a term was supplied), so the snapshot can never claim a gate ran
    that did not, nor hide one that did.
    """
    settings = prefilter_settings(config)
    enabled = bool(settings.get("enabled", False))
    exclude_terms = prefilter_exclude_terms(config)
    return {
        "enabled": enabled,
        "exclude_only": (not enabled) and bool(exclude_terms),
        "min_seconds": float(settings.get("min_seconds") or 0),
        "max_seconds": float(settings.get("max_seconds") or 0),
        "heat_gate_percentile": float(settings.get("heat_gate_percentile") or 0.0),
        "allow_unknown_duration": bool(settings.get("allow_unknown_duration", True)),
        # The media-type gate is governed by ``enabled`` (default on), so record
        # the *effective* value -- a reader can tell whether non-video posts were
        # dropped by this run or merely passed through.
        "drop_non_video": prefilter_drop_non_video(config),
        # The exclude terms are additive (config defaults to ``[]``); recording
        # the *effective* list -- config values plus any ``--exclude-term`` -- lets
        # a reader see exactly what gate ran, not what the config alone implied.
        "exclude_terms": exclude_terms,
    }


def _config_with_extra_excludes(config: dict[str, Any], extra_terms: list[str] | None) -> dict[str, Any]:
    """Return ``config`` with CLI ``--exclude-term`` values **appended** to prefilter.

    Appending (never overwriting) keeps the config's own terms authoritative while
    letting a one-off run add to them.  When there is nothing extra the *same*
    object is returned, so a run without the flag is byte-for-byte unchanged.
    """
    extra: list[str] = []
    seen: set[str] = set()
    for value in extra_terms or []:
        text = str(value or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            extra.append(text)
    if not extra:
        return config

    merged = copy.deepcopy(config)
    settings = material_replication_settings(merged)
    prefilter = settings.setdefault("prefilter", {})
    existing = prefilter.get("exclude_terms") or []
    if isinstance(existing, str):
        existing = [existing]
    combined = prefilter_exclude_terms({"jobs": {"material_replication": {"prefilter": {"exclude_terms": [*existing, *extra]}}}})
    prefilter["exclude_terms"] = combined
    return merged


def _prefilter_reject_reasons(rejected: list[dict[str, Any]]) -> str:
    return "；".join(
        f"{entry.get('video_id') or '候选'}（{entry.get('stage')}）：{entry.get('reason')}"
        for entry in rejected
    ) or "无"


def _script_not_found_summary(script_result: dict[str, Any]) -> str:
    """Human-readable one-line reason for a ``not_found`` script replica."""
    stage = script_result.get("stage") or {}
    unmet = script_result.get("unmet") or []
    details = "；".join(
        f"{entry.get('video_id') or '候选池'}：{entry.get('reason') or ''}".rstrip("：")
        for entry in unmet
    ) or "无候选进入脚本筛选"
    return (
        f"未找到脚本复刻视频（候选池 {stage.get('candidate_pool', 0)} 条，"
        f"进入 ASR {stage.get('asr_attempted', 0)} 条）：{details}"
    )


def _budget_starvation_warning(budget: DownloadBudget) -> str | None:
    """A visible, actionable note when the byte ceiling starved healthy candidates.

    Stopping early because the byte budget is spent is *correct* (a hard
    "<=150 MB per run" line means an all-bad top-N legitimately yields nothing),
    but it must never be *silent*: the user has to be able to tell "the byte
    ceiling starved us" apart from "Douyin had no content".  Returns ``None``
    (no noise) unless the run stopped on a byte ceiling *before* meeting the item
    target.
    """
    if budget.stopped_by not in {"bytes", "transferred_bytes"}:
        return None
    if budget.max_count <= 0 or budget.count >= budget.max_count:
        return None
    ledger = "真实传输字节" if budget.stopped_by == "transferred_bytes" else "交付字节"
    # The "wasted traffic" gloss only holds when the *wire* ledger bound the
    # run: that is the case where bytes were written (a download that was then
    # validation/duration-dropped) without a delivered file to show for it.  On
    # the delivered-bytes branch there was no waste -- the run simply filled the
    # delivered-byte ledger -- so it gets a neutral, accurate description.
    if budget.stopped_by == "transferred_bytes":
        tail = "剩余候选未被尝试（真实传输已写满上限，多因已下载文件被校验/时长剔除而未交付、白耗流量）。"
    else:
        tail = "剩余候选未被尝试（交付字节账已写满上限）。"
    return (
        f"下载预算因{ledger}上限提前停止：实际交付 {budget.count} 条 / 目标 {budget.max_count} 条；"
        f"本轮真实传输 {budget.transferred_bytes} 字节、交付 {budget.bytes} 字节。"
        f"{tail}"
        f"如需更多条数，可提高 jobs.material_replication.download_budget.max_bytes，"
        f"或降低 download_budget.max_item_bytes 以先让更小的候选入选。"
    )


def run_material_replication(
    config: dict[str, Any],
    theme: str,
    *,
    business_date: str | None = None,
    pool_size: int | None = None,
    dry_run: bool = False,
    download_only: bool = False,
    overwrite: bool = False,
    exclude_terms: list[str] | None = None,
    deps: "ReplicationDeps | None" = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run the full material-replication workflow for one theme.

    ``download_only`` short-circuits the pipeline right after collection: it
    downloads every eligible candidate as a raw ``原片`` and stops, skipping the
    face gate, clip export and script replica entirely.  It never reports
    ``insufficient`` because no face/speech gate was ever applied.

    ``exclude_terms`` are extra pre-download exclude terms (from the repeatable
    ``--exclude-term`` CLI flag) appended to the config's own list.
    """
    config = _config_with_extra_excludes(config, exclude_terms)
    settings = material_replication_settings(config)
    deps = deps if deps is not None else ReplicationDeps()
    active_clock: Callable[[], float] = getattr(deps, "clock", None) or clock
    phase_budget = settings.get("budget") or {}
    warnings: list[str] = []
    run_id = f"replication-{uuid.uuid4().hex[:12]}"

    zone = ZoneInfo(str(config.get("timezone") or "Asia/Shanghai"))
    if not business_date:
        business_date = datetime.now(zone).date().isoformat()
    folder = delivery_folder_name(business_date, theme, max_path_chars=int(settings.get("max_path_chars") or 260))
    output_root = project_path(config, settings.get("output_root") or "output/复刻视频")
    temp_root = project_path(config, settings.get("temp_root") or "data/temp/material-replication")
    destination = output_root / folder
    stage = temp_root / run_id
    remove_tree(stage)
    ensure_delivery_tree(stage)
    process_dir = stage / FOLDER_PROCESS

    # --- Collection -------------------------------------------------------
    collection_start = active_clock()
    pool = collect_candidate_pool(config, theme, pool_size=int(pool_size or settings.get("default_pool_size") or 80), run_id=run_id, deps=deps)
    candidates: list[Candidate] = pool["candidates"]
    # ``collected_candidate_count`` is the *raw* pool size, kept intact even
    # after the pre-download gate trims ``candidates``: the existing
    # ``candidate_pool_size`` / ``search_attribution.pool_size`` / ``counters``
    # semantics stay "collected", while the post-prefilter count is recorded
    # separately in the ``prefilter`` block so the two can never be conflated.
    collected_candidate_count = len(candidates)
    media_urls: dict[str, str] = pool["media_urls"]
    # ``keywords_used`` == what the crawler actually searched; ``keywords_requested``
    # == the full expansion.  Keeping them apart stops the delivery from claiming
    # coverage the crawler's ``budget // 10`` truncation never provided.
    keywords_requested: list[str] = list(pool["keywords_requested"])
    keywords_used: list[str] = list(pool["keywords_used"])
    keywords_truncated: bool = bool(pool["keywords_truncated"])
    search_report: dict[str, Any] = pool.get("search_report") or {}
    search_attribution: dict[str, Any] = {
        "min_pool_size": int(pool.get("min_pool_size") or 0),
        "pool_size": collected_candidate_count,
        "keywords_requested_count": len(keywords_requested),
        "keywords_used_count": len(keywords_used),
        "keywords_truncated": keywords_truncated,
        "per_keyword_budget": search_report.get("per_keyword_budget"),
        "raw_request_ceiling": search_report.get("raw_request_ceiling"),
        "search_report_path": f"{FOLDER_PROCESS}/search_report.json",
    }
    warnings.extend(pool.get("warnings") or [])
    if _phase_expired(collection_start, float(phase_budget.get("collection_seconds") or 480), active_clock):
        warnings.append("候选池采集超出软预算，仅使用已捕获结果")
    atomic_write_json(process_dir / "candidate_pool.json", pool["candidate_pool"])
    atomic_write_json(process_dir / "search_report.json", search_report)
    atomic_write_json(
        process_dir / "scoring.json",
        _scoring_payload(theme, keywords_used, candidates, keywords_requested=keywords_requested),
    )

    theme_safe = sanitize_theme(theme, max_length=int(settings.get("theme_max_chars") or 12)) or "未命名主题"

    if not candidates:
        warnings.append("候选池为空，未产出可交付素材")
        manifest = build_manifest(
            theme=theme, folder=folder, business_date=business_date, generated_at=_now_iso(config),
            keywords_used=keywords_used, candidate_pool_size=0,
            keywords_requested=keywords_requested, search_attribution=search_attribution,
            script_replica={
                "status": "not_found",
                "unmet_conditions": [{"video_id": "", "stage": "pool", "reason": "候选池为空"}],
                "errors": [], "downloaded": 0,
                "stage": {"candidate_pool": 0, "asr_attempted": 0, "rejected": 0, "errors": 0, "conclusion": "not_found"},
            },
            material_replica_sources=[], main_materials=[], supporting_materials=[],
            counters={"candidates": 0, "downloaded": 0, "face_checked": 0, "face_errors": 0, "invalid_media": 0, "clips_exported": 0, "clips_rejected_face_heavy": 0, "clips_rejected_duration": 0, "rejected_pool": 0, "rejected_author_duplicate": 0, "rejected_visual": 0, "rejected_speech": 0, "rejected_not_video": 0},
            face_backend=FACE_UNAVAILABLE, face_backend_status=FACE_UNAVAILABLE, ffmpeg_status="not_checked",
            degraded=True, insufficient=True, warnings=warnings,
        )
        manifest["material_replica"] = {
            "status": "not_run", "conclusion": "empty", "pool_size": 0, "heat_median": 0.0,
            "face_checked": 0, "selected": 0,
            "rejected": [{"video_id": "", "stage": "pool", "reason": "候选池为空"}], "errors": [],
        }
        atomic_write_json(stage / MANIFEST_NAME, manifest)
        _atomic_text(stage / DELIVERY_README, render_delivery_readme(manifest))
        _publish(stage, destination, overwrite)
        return {
            "status": "failed", "output_dir": str(destination.resolve()),
            "manifest_path": str((destination / MANIFEST_NAME).resolve()), "counts": manifest["counters"],
            "degraded": True, "insufficient": True, "warnings": warnings, "dry_run": dry_run,
        }

    # --- Dry run ----------------------------------------------------------
    if dry_run:
        warnings.append("dry-run：仅产出候选池与打分，未下载、未切片")
        counters = {"candidates": len(candidates), "downloaded": 0, "face_checked": 0, "face_errors": 0, "invalid_media": 0, "clips_exported": 0, "clips_rejected_face_heavy": 0, "clips_rejected_duration": 0, "rejected_pool": 0, "rejected_author_duplicate": 0, "rejected_visual": 0, "rejected_speech": 0, "rejected_not_video": 0}
        manifest = build_manifest(
            theme=theme, folder=folder, business_date=business_date, generated_at=_now_iso(config),
            keywords_used=keywords_used, candidate_pool_size=len(candidates), script_replica={"status": "not_run"},
            keywords_requested=keywords_requested, search_attribution=search_attribution,
            material_replica_sources=[], main_materials=[], supporting_materials=[], counters=counters,
            face_backend="not_checked", face_backend_status="not_checked", ffmpeg_status="not_checked",
            degraded=False, insufficient=False, warnings=warnings,
        )
        manifest["material_replica"] = {
            "status": "not_run", "conclusion": "dry_run", "pool_size": len(candidates),
            "heat_median": 0.0, "face_checked": 0, "selected": 0, "rejected": [], "errors": [],
        }
        atomic_write_json(stage / MANIFEST_NAME, manifest)
        _atomic_text(stage / DELIVERY_README, render_delivery_readme(manifest))
        atomic_write_json(process_dir / "run_log.json", {
            "status": "success", "dry_run": True,
            "keywords": keywords_used, "keywords_requested": keywords_requested,
            "keywords_used": keywords_used, "keywords_truncated": keywords_truncated,
            "pool_size": len(candidates),
        })
        _publish(stage, destination, overwrite)
        return {
            "status": "success", "output_dir": str(destination.resolve()),
            "manifest_path": str((destination / MANIFEST_NAME).resolve()), "counts": counters,
            "degraded": False, "insufficient": False, "warnings": warnings, "dry_run": True,
        }

    # --- Download-before prefilter (metadata only) ------------------------
    # Applied once, after collection and before *any* download, so both the
    # download-only branch below and the full selection chain inherit the same
    # gate.  It is deliberately metadata-only: a duration/heat cut that needs a
    # downloaded file would defeat the point of a *pre*-download filter.  The
    # gate runs when the duration/heat switch is on **or** any exclude term is
    # effective (``prefilter_active``): an explicitly supplied exclude term must
    # never be dropped just because the other switches are off.  With the shipped
    # ``enabled: false`` and ``exclude_terms: []`` the gate is inert and no extra
    # artifact is emitted, keeping legacy output intact.
    prefilter_block: dict[str, Any] | None = None
    if prefilter_active(config):
        prefilter_enabled = bool(prefilter_settings(config).get("enabled", False))
        prefilter_passed, prefilter_rejected = prefilter_candidates(candidates, config)
        snapshot = _prefilter_config_snapshot(config)
        # Honest degradation: without the MediaCrawler metadata-duration patch the
        # search JSONL carries no length, so the pre-download duration window can
        # only pass everything through (``allow_unknown_duration``) and defer to the
        # post-download measured check.  Record that fact instead of letting the
        # window look like a no-op bug.
        metadata_duration_available = duration_patch_status(config)["status"] == "ok"
        prefilter_block = {
            # ``enabled`` reflects the *actual* execution: the duration/heat gate
            # only runs when the switch is on.  ``exclude_only`` marks the
            # exclude-only mode so a reader can never read "enabled: false" as
            # "nothing happened" while the exclude gate did run.
            "enabled": prefilter_enabled,
            "exclude_only": (not prefilter_enabled) and bool(prefilter_exclude_terms(config)),
            "reason": (
                "" if prefilter_enabled else "仅排除词闸门生效（prefilter.enabled=false）"
            ),
            "config": snapshot,
            "metadata_duration_available": metadata_duration_available,
            "pool_size": collected_candidate_count,
            "passed": len(prefilter_passed),
            "rejected": len(prefilter_rejected),
            "rejections": prefilter_rejected,
        }
        if (
            prefilter_enabled
            and not metadata_duration_available
            and (snapshot["min_seconds"] > 0 or snapshot["max_seconds"] > 0)
        ):
            lower = f"{snapshot['min_seconds']:.0f}" if snapshot["min_seconds"] > 0 else "不限"
            upper = f"{snapshot['max_seconds']:.0f}" if snapshot["max_seconds"] > 0 else "不限"
            prefilter_block["metadata_duration_note"] = (
                "本编辑器/环境未启用元数据时长（MediaCrawler 时长补丁缺失）："
                f"下载前时长窗口（{lower}~{upper}s）无法按元数据生效，仅在下载后按实测时长执行；"
                "运行 python scripts/apply_mediacrawler_duration_patch.py 可在下载前生效。"
            )
        candidates = prefilter_passed
        search_attribution["prefilter_enabled"] = prefilter_enabled
        search_attribution["prefilter_exclude_only"] = prefilter_block["exclude_only"]
        search_attribution["prefiltered_pool_size"] = len(prefilter_passed)

        if candidates:
            prefilter_block["conclusion"] = "applied"
            atomic_write_json(process_dir / "prefilter.json", {"schema_version": 1, **prefilter_block})
        else:
            # The pool was *not* empty -- every candidate was dropped here.
            # Reporting this as "候选池为空" would make the operator believe
            # Douyin had no content at all, so it gets its own conclusion and a
            # warning that states exactly how many were collected and why each
            # one was cut.
            prefilter_block["conclusion"] = "prefiltered_empty"
            atomic_write_json(process_dir / "prefilter.json", {"schema_version": 1, **prefilter_block})
            warnings.append(
                f"候选池采集到 {collected_candidate_count} 条，全部被下载前预筛剔除"
                f"（{len(prefilter_rejected)} 条）：{_prefilter_reject_reasons(prefilter_rejected)}"
            )
            counters = {
                "candidates": collected_candidate_count, "downloaded": 0, "face_checked": 0, "face_errors": 0,
                "invalid_media": 0, "clips_exported": 0, "clips_rejected_face_heavy": 0, "clips_rejected_duration": 0,
                "rejected_pool": 0, "rejected_author_duplicate": 0, "rejected_visual": 0, "rejected_speech": 0,
                "rejected_not_video": 0,
            }
            if download_only:
                script_replica_block: dict[str, Any] = {"status": "skipped", "reason": "下载前预筛后无候选"}
            else:
                script_replica_block = {
                    "status": "not_found",
                    "unmet_conditions": [
                        {"video_id": "", "stage": "prefilter", "reason": "全部候选被下载前预筛剔除"}
                    ],
                    "errors": [], "downloaded": 0,
                    "stage": {"candidate_pool": 0, "asr_attempted": 0, "rejected": 0, "errors": 0, "conclusion": "not_found"},
                }
            manifest = build_manifest(
                theme=theme, folder=folder, business_date=business_date, generated_at=_now_iso(config),
                keywords_used=keywords_used, candidate_pool_size=collected_candidate_count,
                keywords_requested=keywords_requested, search_attribution=search_attribution,
                script_replica=script_replica_block,
                material_replica_sources=[], main_materials=[], supporting_materials=[], counters=counters,
                face_backend="not_checked", face_backend_status="not_checked", ffmpeg_status="not_checked",
                degraded=True, insufficient=True, warnings=warnings, prefilter=prefilter_block,
            )
            if download_only:
                manifest["mode"] = "download_only"
                manifest["downloads"] = []
                manifest["download_failures"] = []
            manifest["material_replica"] = {
                "status": "not_run", "conclusion": "prefiltered_empty",
                "pool_size": collected_candidate_count, "heat_median": 0.0,
                "face_checked": 0, "selected": 0, "rejected": [], "errors": [],
            }
            _write_manifest(stage, manifest)
            atomic_write_json(process_dir / "run_log.json", {
                "status": "failed", "mode": "download_only" if download_only else "full",
                "keywords": keywords_used, "keywords_requested": keywords_requested,
                "keywords_used": keywords_used, "keywords_truncated": keywords_truncated,
                "pool_size": collected_candidate_count,
                "prefilter": {
                    "passed": 0, "rejected": len(prefilter_rejected), "conclusion": "prefiltered_empty",
                    "exclude_only": bool(prefilter_block.get("exclude_only")),
                },
            })
            _publish(stage, destination, overwrite)
            return {
                "status": "failed", "mode": "download_only" if download_only else "full",
                "output_dir": str(destination.resolve()),
                "manifest_path": str((destination / MANIFEST_NAME).resolve()),
                "counts": counters, "downloads": [], "failures": [],
                "degraded": True, "insufficient": True, "warnings": warnings, "dry_run": False,
                "prefilter": {"passed": 0, "rejected": len(prefilter_rejected), "conclusion": "prefiltered_empty"},
            }

    # --- Download budget (layer 2: which of the survivors to fetch) -------
    # The prefilter (layer 1) drops clearly unsuitable candidates; the budget
    # (layer 2) then takes the top-N by relevance/heat within a byte ceiling so
    # cost tracks value instead of "download everything".  The two layers are
    # reported separately (``prefilter`` vs ``download_budget``).  Visual
    # quality is the user's top priority but is undecidable pre-download, so the
    # order is relevance -> heat -> video_id and the limitation is stated.
    relevance_detail = relevance_report(candidates, theme, keywords_requested, config=config)
    relevance: dict[str, float] = relevance_detail["scores"]
    # "Relevance could not discriminate this pool" is a *degradation of the whole
    # delivery*, not a private detail of the ordering: the 9.14 corpus had four
    # runs (机械鸭 / 充电宝3C / 内存涨价 / 华为昇腾950DT) whose relevance was
    # degraded while the top level reported ``degraded=False, status="done"`` --
    # i.e. a false pass that was invisible to every upstream reader.  All four were
    # full runs (``mode: null`` in ``run_log.json``), so the OR below lands on the
    # full path's single top-level ``degraded``.  The download-only path keeps its
    # own contract (it never had a ``degraded`` computed from relevance, and its
    # ``search_attribution.relevance`` block already carries the flag).
    relevance_degraded = bool(relevance_detail["degraded"])
    # Attribution lives in ``search_attribution`` (additive) so a reader can tell
    # *why* the order looks the way it does: which terms actually matched this
    # pool and which were dead on arrival.  The subject fields are what makes the
    # warning below auditable: ``hit_ratio`` is the share of the pool that
    # mentions the theme's subject at all, which is a different question from the
    # per-term scores.  ``.get`` keeps this readable if the report ever lacks them.
    search_attribution["relevance"] = {
        "live_terms": list(relevance_detail["live_terms"]),
        "dead_terms": list(relevance_detail["dead_terms"]),
        "live_count": int(relevance_detail["live_count"]),
        "degraded": relevance_degraded,
        "hit_ratio": relevance_detail.get("hit_ratio"),
        "subject_terms": list(relevance_detail.get("subject_terms") or []),
        "subject_hits": relevance_detail.get("subject_hits"),
    }
    if relevance_degraded:
        # Two distinct causes must not share one sentence: "no term hit anything"
        # and "too few candidates hit the subject" are different defects with
        # different fixes, and the reader must not be told the wrong one.  Today
        # only the first can trigger ``degraded`` (``live_count == 0``); the
        # hit-ratio floor that makes the second reachable is configured inside
        # ``relevance_report``, so the ratio is read defensively here: if it is
        # ever absent the cause is still named correctly, only the number is
        # omitted -- the branch never invents a value and never raises.
        if int(relevance_detail["live_count"]) > 0:
            try:
                hit_ratio: float | None = float(relevance_detail["hit_ratio"])
            except (KeyError, TypeError, ValueError):
                hit_ratio = None
            measured = f"主体词命中率 {round(hit_ratio, 4)}，" if hit_ratio is not None else ""
            warnings.append(
                "题材相关度命中率过低："
                f"{measured}主体词未覆盖足够的候选标题，"
                "相关度无法有效区分本轮候选池；请检查主题措辞或候选池来源。"
            )
        else:
            warnings.append(
                "题材相关度无法区分本轮候选池："
                f"{len(relevance_detail['terms'])} 个搜索词均未在任何候选标题中命中，"
                "下载顺序退化为热度 → video_id；请检查主题措辞或候选池来源。"
            )
    budget: DownloadBudget | None = DownloadBudget.from_config(config)

    # --- Download-only ----------------------------------------------------
    # Collect every eligible candidate as a raw source video and stop.  No face
    # gate, no clip export, no script replica: the delivery is just "原片 + 清单".
    # The order is *always* relevance -> heat -> video_id (``ranked_candidates``):
    # the budget only decides *how many* to fetch, never *what order* to try them
    # in, so disabling the budget cannot silently fall back to a pure-heat order
    # whose header rows all share ``heat_score == 0``.
    if download_only:
        warnings.append("download-only 模式：仅采集与下载原片，未做人脸筛选/切片/脚本复刻")
        video_root = project_path(
            config, settings.get("media_root") or "data/media/material-replication"
        ) / REPLICATION_VIDEO_SUBDIR
        from .materials import MediaTooLargeError, download_video, probe_video

        downloader = getattr(deps, "downloader", None) or download_video
        prober = getattr(deps, "prober", None) or probe_video
        validator = getattr(deps, "validator", None)
        source_dir = stage / FOLDER_SOURCE
        # ``retention.keep_source_video`` gates whether originals are copied into
        # ``04-原片``.  It is honoured on the download-only path too (it used to
        # be ignored here) so the switch means exactly the same thing on every
        # path, and so "04-原片 is empty" is never a silent surprise.
        keep_source = bool((settings.get("retention") or {}).get("keep_source_video", True))
        kept_sources = 0
        downloads: list[dict[str, Any]] = []
        download_failures: list[dict[str, str]] = []
        validation_store: list[dict[str, Any]] = []
        validation_passed = 0
        validation_rejected = 0
        duration_window_rejected = 0
        ordered = ranked_candidates(candidates, relevance)
        for candidate in ordered:
            usable, not_video_reason = is_video_candidate(candidate)
            if not usable:
                download_failures.append({
                    "video_id": candidate.video_id, "stage": "not_video", "reason": not_video_reason,
                })
                continue
            media_url = media_urls.get(candidate.video_id, "")
            if not media_url:
                download_failures.append({
                    "video_id": candidate.video_id, "stage": "no_media_url", "reason": "缺少下载地址",
                })
                continue
            if budget is not None:
                allowed, budget_reason = budget.allow()
                if not allowed:
                    budget.skip(candidate, "budget", budget_reason, relevance=relevance.get(candidate.video_id, 0.0))
                    break
            rel = relevance.get(candidate.video_id, 0.0)
            video_path = video_root / f"{candidate.video_id}.mp4"
            pre_size = file_size(video_path)
            try:
                invoke_downloader(downloader, media_url, video_path, config, budget.item_cap() if budget is not None else None)
            except Exception as exc:
                if budget is not None and isinstance(exc, MediaTooLargeError):
                    # Charge any bytes a *streamed* oversize already read off the
                    # wire (0 for a declared/Content-Length rejection).
                    budget.mark_transferred(int(getattr(exc, "bytes_read", 0) or 0))
                    action = budget.note_oversize(budget.item_cap())
                    budget.skip(candidate, "budget_item" if action == "skip" else "budget_bytes", str(exc)[:160], relevance=rel)
                    if action == "stop":
                        break
                    continue
                download_failures.append({
                    "video_id": candidate.video_id, "stage": "download", "reason": f"下载失败：{str(exc)[:120]}",
                })
                continue
            if budget is not None:
                # Charge real traffic *before* the download-time validation /
                # duration window can reject this file: a file that is pulled
                # and then dropped still cost bandwidth, so the wire ledger --
                # not just the delivered one -- must grow.  Without this a
                # download-only run kept downloading rejected files forever.
                budget.mark_transferred(measure_transferred_bytes(video_path, pre_size))
            try:
                probe = prober(video_path, config)
            except Exception as exc:
                download_failures.append({
                    "video_id": candidate.video_id, "stage": "invalid_media", "reason": f"媒体无效：{str(exc)[:120]}",
                })
                continue
            media_ok, media_reason = validate_probe(probe)
            if not media_ok:
                download_failures.append({
                    "video_id": candidate.video_id, "stage": "invalid_media", "reason": f"媒体无效：{media_reason}",
                })
                continue
            # Download-time validation runs *before* ``budget.select``, so a
            # corrupt file never occupies a slot or a byte of the run budget and
            # the loop simply continues to the next candidate.
            validation_record = validate_candidate(
                video_path, config, candidate=candidate, probe=probe, prober=prober, validator=validator,
            )
            if validation_record is not None:
                record_validation(validation_store, validation_record, stage="material")
                if not validation_record.get("passed"):
                    validation_rejected += 1
                    download_failures.append({
                        "video_id": candidate.video_id, "stage": "validation",
                        "reason": validation_reason(validation_record),
                    })
                    continue
                validation_passed += 1
            # Post-download half of the duration gate: candidates whose metadata
            # carried no duration slipped past the pre-download window; now that
            # the real length is known, the same window is applied.  A file
            # outside it is treated like a validation failure -- not delivered,
            # no budget charge, loop continues.
            window_reject, window_reason = measured_duration_window_reject(
                float(probe.get("duration_seconds") or 0), config,
                metadata_duration=float(getattr(candidate, "duration_seconds", 0.0) or 0.0),
            )
            if window_reject:
                duration_window_rejected += 1
                download_failures.append({
                    "video_id": candidate.video_id, "stage": "duration_post", "reason": window_reason,
                })
                continue
            try:
                size_bytes = int(Path(video_path).stat().st_size)
            except OSError:
                size_bytes = 0
            if budget is not None:
                budget.select(candidate, size_bytes, relevance=rel)
            file_rel = ""
            if keep_source:
                try:
                    source_name = _source_copy_name(candidate)
                    shutil.copy2(video_path, source_dir / source_name)
                    file_rel = f"{FOLDER_SOURCE}/{source_name}"
                    kept_sources += 1
                except OSError as exc:
                    warnings.append(f"原片保留失败：{candidate.video_id} {exc}")
            record = {
                "video_id": candidate.video_id,
                "title": candidate.title,
                "author": candidate.author,
                "source_url": candidate.source_url,
                "heat_score": round(float(candidate.heat_score), 6),
                "duration_seconds": round(float(probe.get("duration_seconds") or 0), 3),
                "size_bytes": size_bytes,
                "width": probe.get("width"),
                "height": probe.get("height"),
                "file": file_rel,
                "media_path": str(video_path),
            }
            if budget is not None:
                # Only surfaced when the budget is active; with it disabled the
                # output is behaviourally equivalent to the pre-change one (the
                # diff is additive only, never a changed/removed value).
                record["relevance_score"] = round(float(rel), 6)
            downloads.append(record)
        counters = {
            "candidates": collected_candidate_count, "downloaded": len(downloads), "face_checked": 0, "face_errors": 0,
            "invalid_media": 0, "clips_exported": 0, "clips_rejected_face_heavy": 0, "clips_rejected_duration": 0,
            "rejected_pool": 0, "rejected_author_duplicate": 0, "rejected_visual": 0, "rejected_speech": 0,
            "rejected_not_video": 0,
        }
        validation_block = build_validation_block(config, validation_store)
        if validation_block is not None:
            # New fields only -- ``counters`` keeps every existing key untouched.
            counters["validation_passed"] = validation_passed
            counters["validation_rejected"] = validation_rejected
            counters["validation_duration_window_rejected"] = duration_window_rejected
            if validation_rejected:
                warnings.append(
                    f"下载校验剔除 {validation_rejected} 条坏件，未占用下载预算"
                    f"（详见 {FOLDER_PROCESS}/validation.json）"
                )
            if duration_window_rejected:
                warnings.append(
                    f"下载后实测时长窗口剔除 {duration_window_rejected} 条"
                    f"（元数据无时长，按实测时长判定，未占用下载预算）"
                )
        if budget is not None:
            if budget.stopped_by is None:
                budget.stopped_by = "queue_exhausted"
            starvation_note = _budget_starvation_warning(budget)
            if starvation_note:
                warnings.append(starvation_note)
            download_budget_block: dict[str, Any] | None = budget.snapshot()
        else:
            download_budget_block = None
        ffprobe_ok = media_tool_available(config, "ffprobe")
        manifest = build_manifest(
            theme=theme, folder=folder, business_date=business_date, generated_at=_now_iso(config),
            keywords_used=keywords_used, candidate_pool_size=collected_candidate_count,
            keywords_requested=keywords_requested, search_attribution=search_attribution,
            script_replica={"status": "skipped", "reason": "download-only 模式未执行脚本复刻"},
            material_replica_sources=[], main_materials=[], supporting_materials=[], counters=counters,
            face_backend="not_checked", face_backend_status="not_checked",
            ffmpeg_status="ok" if ffprobe_ok else "unavailable",
            degraded=False, insufficient=False, warnings=warnings, prefilter=prefilter_block,
            download_budget=download_budget_block, validation=validation_block,
        )
        manifest["mode"] = "download_only"
        manifest["downloads"] = downloads
        manifest["download_failures"] = download_failures
        manifest["source_retention"] = {
            "keep_source_video": keep_source,
            "kept_count": kept_sources,
            "note": "04-原片 收录本次交付的下载原片" if keep_source else "retention.keep_source_video=false：04-原片 不收录原片",
            "persistent_store": str(settings.get("media_root") or "data/media/material-replication"),
        }
        manifest["material_replica"] = {
            "status": "skipped", "conclusion": "download_only", "pool_size": collected_candidate_count,
            "selected": 0, "rejected": [], "errors": [],
        }
        atomic_write_json(process_dir / "download_log.json", {
            "mode": "download_only", "downloads": downloads, "failures": download_failures,
            "counters": counters,
        })
        if download_budget_block is not None:
            atomic_write_json(process_dir / "download_budget.json", {"schema_version": 1, **download_budget_block})
        write_validation_artifact(process_dir, config, validation_store)
        _write_manifest(stage, manifest)
        download_run_log = {
            "status": "done", "mode": "download_only", "dry_run": False,
            "keywords": keywords_used, "keywords_requested": keywords_requested,
            "keywords_used": keywords_used, "keywords_truncated": keywords_truncated,
            "search_attribution": search_attribution, "pool_size": collected_candidate_count,
            "downloaded": len(downloads), "failed": len(download_failures),
        }
        if download_budget_block is not None:
            download_run_log["download_budget"] = download_budget_block
        if validation_block is not None:
            download_run_log["validation"] = {"counts": validation_block["counts"]}
        atomic_write_json(process_dir / "run_log.json", download_run_log)
        _publish(stage, destination, overwrite)
        if downloads and not download_failures:
            status = "success"
        elif downloads:
            status = "partial"
        else:
            status = "failed"
        return {
            "status": status, "mode": "download_only",
            "output_dir": str(destination.resolve()),
            "manifest_path": str((destination / MANIFEST_NAME).resolve()),
            "counts": counters, "downloads": downloads, "failures": download_failures,
            "degraded": False, "insufficient": False, "warnings": warnings, "dry_run": False,
        }

    # --- Runtime probes (no download) ------------------------------------
    face_detector = getattr(deps, "face_detector", None) or FaceDetector(config)
    face_status = face_detector.status() if hasattr(face_detector, "status") else {"backend": FACE_UNAVAILABLE, "status": FACE_UNAVAILABLE, "model_present": False}
    ffmpeg_ok = media_tool_available(config, "ffmpeg")
    ffmpeg_status = "ok" if ffmpeg_ok else "unavailable"
    ffmpeg_arg = resolve_media_tool(config, "ffmpeg") if ffmpeg_ok else None
    degraded = not ffmpeg_ok or face_status["status"] != "ok" or relevance_degraded
    if not ffmpeg_ok:
        warnings.append("ffmpeg 不可用，片段将退化为原片与区间清单")
    if face_status["status"] != "ok":
        warnings.append("人脸后端不可用，人脸指标为 unavailable，交付前需人工复核")

    # --- Script replica ---------------------------------------------------
    # Both selection loops append their per-file download-validation records
    # here, so ``validation.json`` reflects the whole run.
    validation_store: list[dict[str, Any]] = []
    script_start = active_clock()
    script_result = select_script_replica(
        config, candidates, media_urls=media_urls, deps=deps, clock=active_clock,
        budget=budget, relevance=relevance, validation_store=validation_store,
    )
    if _phase_expired(script_start, float(phase_budget.get("asr_seconds") or 180) + float(phase_budget.get("download_seconds") or 480), active_clock):
        warnings.append("脚本复刻视频选择超出软预算")
    script_manifest: dict[str, Any] = {
        "status": script_result["status"],
        "unmet_conditions": list(script_result.get("unmet") or []),
        "errors": list(script_result.get("errors") or []),
        "downloaded": int(script_result.get("downloaded") or 0),
        "stage": dict(script_result.get("stage") or {}),
    }
    if script_result["status"] == "found":
        chosen = script_result["candidate"]
        skeleton = build_script_skeleton(chosen, script_result["transcript"], script_result["probe"], config)
        files = write_script_artifacts(stage / FOLDER_SCRIPT, chosen, skeleton, script_result["transcript"], config)
        script_manifest.update({
            "video_id": chosen.video_id,
            "author": chosen.author,
            "heat_score": chosen.heat_score,
            "skeleton": f"{FOLDER_SCRIPT}/{files['skeleton']}",
            "script_notes": f"{FOLDER_SCRIPT}/{files['script_notes']}",
        })
        atomic_write_json(process_dir / "script_skeleton.json", skeleton)
    else:
        warnings.append(_script_not_found_summary(script_result))

    # --- Material replicas ------------------------------------------------
    material_result = select_material_replicas(
        config, candidates, media_urls=media_urls, deps=deps, clock=active_clock,
        budget=budget, relevance=relevance, validation_store=validation_store,
        theme=theme,
    )
    selected: list[dict[str, Any]] = material_result["selected"]
    insufficient = bool(material_result["insufficient"])
    warnings.extend(material_result.get("warnings") or [])
    if selected and face_status["status"] != "ok":
        degraded = True
    material_face_errors = int(material_result["counters"].get("face_errors", 0))
    if material_face_errors > 0:
        degraded = True
        warnings.append(f"存在人脸采样失败的视频（{material_face_errors} 条），交付清单标记 degraded")
    material_invalid_media = int(material_result["counters"].get("invalid_media", 0))
    if material_invalid_media > 0:
        degraded = True
        warnings.append(f"存在 {material_invalid_media} 条下载文件无有效视频流，已跳过")
    # Structured, attributable material-selection outcome.  ``select_material_replicas``
    # already appended a human-readable summary warning when the set is short, so an
    # empty/short set is explainable straight from the delivery folder.
    material_replica_block: dict[str, Any] = {
        "status": material_result["status"],
        "conclusion": (material_result.get("stage") or {}).get("conclusion", ""),
        "pool_size": int((material_result.get("stage") or {}).get("candidate_pool", 0)),
        "heat_median": (material_result.get("stage") or {}).get("heat_median", 0.0),
        "face_checked": int((material_result.get("stage") or {}).get("face_checked", 0)),
        "selected": len(selected),
        "delivered_bytes": int(material_result.get("delivered_bytes") or 0),
        "min_delivered_bytes": int((material_result.get("stage") or {}).get("min_delivered_bytes") or 0),
        "max_delivered_bytes": int((material_result.get("stage") or {}).get("max_delivered_bytes") or 0),
        "rejected": list(material_result.get("unmet") or []),
        "errors": list(material_result.get("errors") or []),
    }

    clips_cfg = settings.get("clips") or {}
    min_clip = float(clips_cfg.get("min_seconds") or 3)
    max_clip = float(clips_cfg.get("max_seconds") or 8)
    max_per_video = int(clips_cfg.get("max_per_video") or 2)
    max_total = int(clips_cfg.get("max_total") or 12)
    keep_source = bool((settings.get("retention") or {}).get("keep_source_video", True))

    ordered = sorted(
        selected,
        key=lambda item: (
            _FACE_RANK.get(str(item["face"].get("face_class")), 3),
            -float(item["candidate"].heat_score),
            -float(item["probe"].get("duration_seconds") or 0),
            item["candidate"].video_id,
        ),
    )
    main_ids: set[str] = set()
    seen_authors: set[str] = set()
    for item in ordered:
        if len(main_ids) >= 2:
            break
        if str(item["face"].get("face_class")) != FACE_FREE:
            continue
        author = item["candidate"].author
        if author in seen_authors:
            continue
        seen_authors.add(author)
        main_ids.add(item["candidate"].video_id)

    main_dir = stage / FOLDER_MAIN
    support_dir = stage / FOLDER_SUPPORT
    source_dir = stage / FOLDER_SOURCE
    # Automatic visual confirmation of the selected source clips (on-screen text
    # only -- no VLM on this machine).  It runs before any copy/export so the
    # per-row ``visual_verdict`` and the manifest summary are already known, and
    # it writes nothing at all when the gate is off.
    visual_payload = _visual_verify_payload(config, ordered, theme, deps, warnings)
    if visual_payload is not None:
        atomic_write_json(process_dir / "visual_verify.json", visual_payload)
    visual_verdicts = _visual_verdicts(visual_payload)
    main_materials: list[dict[str, Any]] = []
    supporting_materials: list[dict[str, Any]] = []
    material_sources: list[dict[str, Any]] = []
    main_seq = 0
    support_seq = 0
    clips_exported = 0
    kept_sources = 0
    for item in ordered:
        candidate = item["candidate"]
        face = item["face"]
        probe = item["probe"]
        duration = float(probe.get("duration_seconds") or 0)
        is_main = candidate.video_id in main_ids
        role = "main" if is_main else "support"
        role_label = "主素材" if is_main else "辅助素材"
        if is_main:
            main_seq += 1
            seq = main_seq
        else:
            support_seq += 1
            seq = support_seq
        tag = _USAGE_TAGS[min(seq - 1, len(_USAGE_TAGS) - 1)]
        suggested_use = _SUGGESTED_USE[min(seq - 1, len(_SUGGESTED_USE) - 1)]
        prefix = f"{theme_safe}-{role_label}-{seq:02d}-{tag}"
        target_dir = main_dir if is_main else support_dir
        target_folder = FOLDER_MAIN if is_main else FOLDER_SUPPORT

        source_rel = ""
        if keep_source:
            source_name = _source_copy_name(candidate)
            try:
                shutil.copy2(item["video_path"], source_dir / source_name)
                source_rel = f"{FOLDER_SOURCE}/{source_name}"
                kept_sources += 1
            except OSError as exc:
                warnings.append(f"原片保留失败：{candidate.video_id} {exc}")

        material_sources.append({
            "video_id": candidate.video_id,
            "author": candidate.author,
            "face_class": face.get("face_class"),
            "face_class_reason": face.get("face_class_reason", ""),
            "expected_frames": face.get("expected_frames"),
            "emitted_frames": face.get("emitted_frames"),
            "sample_coverage": face.get("sample_coverage"),
            "truncated": bool(face.get("truncated", False)),
            "low_confidence": bool(face.get("low_confidence", False)),
            "selected_reason": item.get("selected_reason", ""),
            # Additive provenance fields: a reader of the manifest can now tell
            # *what* was delivered (title), how big it is, when it was published,
            # where it came from, how well the pool matched the subject, and
            # whether the frames carried the subject on screen.  ``source`` falls
            # back for pools built before multi-source existed; ``hit_ratio`` /
            # ``visual_verdict`` are ``None`` when the corresponding gate did not
            # produce a verdict for this row.
            "title": candidate.title,
            "bytes": file_size(item["video_path"]),
            "published_at": candidate.published_at,
            "source": str(getattr(candidate, "source", "douyin") or "douyin"),
            "hit_ratio": relevance_detail.get("hit_ratio"),
            "visual_verdict": visual_verdicts.get(str(candidate.video_id)),
        })

        face_per_frame = face.get("face_per_frame") or []
        intervals = derive_face_free_intervals(
            [bool(flag) for flag in face_per_frame], duration,
            min_seconds=min_clip, max_seconds=max_clip,
            interval_seconds=float(face.get("sample_interval_seconds") or 1),
        )
        intervals = intervals[:max_per_video]
        if clips_exported + len(intervals) > max_total:
            intervals = intervals[: max(0, max_total - clips_exported)]
        if not intervals:
            warnings.append(f"{candidate.video_id} 未能反推 3~8 秒无人脸区间，仅保留原片")
            degraded = True
            continue

        export = export_video_clips(ffmpeg_arg, item["video_path"], duration, intervals, target_dir, role=role, label_prefix=prefix)
        if export.get("degraded"):
            degraded = True
            warnings.extend(export.get("warnings") or [])
        face_block = {
            "backend": face.get("backend", FACE_UNAVAILABLE),
            "status": face.get("status", FACE_UNAVAILABLE),
            "face_frame_ratio": face.get("face_frame_ratio", 0.0),
            "max_face_area_ratio": face.get("max_face_area_ratio", 0.0),
            "face_class": face.get("face_class", FACE_UNAVAILABLE),
            "face_class_reason": face.get("face_class_reason", ""),
            "sampled_frames": face.get("sampled_frames", 0),
            "expected_frames": face.get("expected_frames"),
            "emitted_frames": face.get("emitted_frames"),
            "sample_coverage": face.get("sample_coverage"),
            "truncated": bool(face.get("truncated", False)),
            "low_confidence": bool(face.get("low_confidence", False)),
        }
        media_block = {"width": probe.get("width"), "height": probe.get("height"), "fps": probe.get("fps"), "has_audio": True}
        for row in export.get("clips") or []:
            if row.get("status") != "ok" or not row.get("file"):
                warnings.append(f"{candidate.video_id} 片段 {row.get('index')} 切片失败")
                continue
            clip_id = f"{ 'main' if is_main else 'support' }-{row['index']:02d}"
            metadata = build_clip_metadata(
                clip_id=clip_id, role=role, file_name=f"{target_folder}/{row['file']}",
                source={
                    "video_id": candidate.video_id,
                    "author": candidate.author,
                    "source_url": candidate.source_url,
                    "play_count": candidate.play_count,
                    "heat_score": candidate.heat_score,
                    "folder": source_rel,
                },
                timecode={"start": row["start"], "end": row["end"], "duration": row["duration"]},
                media=media_block, face=face_block, suggested_use=suggested_use, warnings=[],
            )
            atomic_write_json(target_dir / f"{row['file'].rsplit('.', 1)[0]}.json", metadata)
            record = {
                "clip_id": clip_id,
                "file": metadata["file"],
                "duration": row["duration"],
                "face_class": face_block["face_class"],
                "suggested_use": suggested_use,
            }
            (main_materials if is_main else supporting_materials).append(record)
            clips_exported += 1

    # Run-level log of every truncated face sample -- both the ones that reached
    # delivery (mild truncation: kept with ``low_confidence``) and the ones the
    # class gate rejected (severe truncation: downgraded to ``unavailable``).  The
    # readme renders this as 「人脸样本截断」 so a short sample is visible even
    # when the video itself was not delivered.
    face_truncated_samples: list[dict[str, Any]] = []
    for item in material_result.get("unmet") or []:
        if item.get("truncated"):
            face_truncated_samples.append({
                "video_id": item.get("video_id"),
                "face_class": item.get("face_class"),
                "face_class_reason": item.get("face_class_reason"),
                "expected_frames": item.get("expected_frames"),
                "emitted_frames": item.get("emitted_frames"),
                "sample_coverage": item.get("sample_coverage"),
                # Kept for field parity with ``material_replica_sources``: these
                # entries are by construction truncated, so the flag is always
                # true and a reader can filter every surface the same way.
                "truncated": True,
                "low_confidence": True,
                "delivered": False,
            })
    for item in material_sources:
        if item.get("truncated"):
            face_truncated_samples.append({
                "video_id": item.get("video_id"),
                "face_class": item.get("face_class"),
                "face_class_reason": item.get("face_class_reason"),
                "expected_frames": item.get("expected_frames"),
                "emitted_frames": item.get("emitted_frames"),
                "sample_coverage": item.get("sample_coverage"),
                "truncated": True,
                "low_confidence": bool(item.get("low_confidence")),
                "delivered": True,
            })

    counters = {
        "candidates": collected_candidate_count,
        "downloaded": int(material_result["counters"].get("downloaded", 0)) + int(script_result.get("downloaded", 0)),
        "face_checked": int(material_result["counters"].get("face_checked", 0)),
        "face_errors": material_face_errors,
        "invalid_media": material_invalid_media,
        "clips_exported": clips_exported,
        "clips_rejected_face_heavy": int(material_result["counters"].get("clips_rejected_face_heavy", 0)),
        "clips_rejected_duration": int(material_result["counters"].get("clips_rejected_duration", 0)),
        "rejected_pool": int(material_result["counters"].get("rejected_pool", 0)),
        "rejected_author_duplicate": int(material_result["counters"].get("rejected_author_duplicate", 0)),
        "rejected_visual": int(material_result["counters"].get("rejected_visual", 0)),
        "rejected_speech": int(material_result["counters"].get("rejected_speech", 0)),
        "rejected_not_video": int(material_result["counters"].get("rejected_not_video", 0)),
    }
    duration_window_rejected = (
        sum(1 for item in (script_result.get("unmet") or []) if item.get("stage") == "duration_post")
        + int(material_result["counters"].get("rejected_duration_post", 0))
    )
    validation_block = build_validation_block(config, validation_store)
    if validation_block is not None:
        # New fields only -- no existing counter changes meaning.
        counters["validation_passed"] = int(validation_block["counts"]["passed"])
        counters["validation_rejected"] = int(validation_block["counts"]["rejected"])
        counters["validation_duration_window_rejected"] = duration_window_rejected
        if counters["validation_rejected"]:
            warnings.append(
                f"下载校验剔除 {counters['validation_rejected']} 条坏件，未占用下载预算"
                f"（详见 {FOLDER_PROCESS}/validation.json）"
            )
        if duration_window_rejected:
            warnings.append(
                f"下载后实测时长窗口剔除 {duration_window_rejected} 条"
                f"（元数据无时长，按实测时长判定，未占用下载预算）"
            )

    if budget is not None:
        if budget.stopped_by is None:
            budget.stopped_by = "queue_exhausted"
        starvation_note = _budget_starvation_warning(budget)
        if starvation_note:
            warnings.append(starvation_note)
        download_budget_block: dict[str, Any] | None = budget.snapshot()
    else:
        download_budget_block = None

    if validation_block is not None:
        write_validation_artifact(process_dir, config, validation_store)

    manifest = build_manifest(
        theme=theme, folder=folder, business_date=business_date, generated_at=_now_iso(config),
        keywords_used=keywords_used, candidate_pool_size=collected_candidate_count, script_replica=script_manifest,
        keywords_requested=keywords_requested, search_attribution=search_attribution,
        material_replica_sources=material_sources, main_materials=main_materials,
        supporting_materials=supporting_materials, counters=counters,
        face_backend=str(face_status.get("backend") or FACE_UNAVAILABLE),
        face_backend_status=str(face_status.get("status") or FACE_UNAVAILABLE),
        ffmpeg_status=ffmpeg_status, degraded=degraded, insufficient=insufficient, warnings=warnings,
        prefilter=prefilter_block, download_budget=download_budget_block, validation=validation_block,
        face_truncated_samples=face_truncated_samples or None,
    )
    manifest["material_replica"] = material_replica_block
    # ``keep_source_video`` and "only publish selected sources" are the *same*
    # switch: 04-原片 receives one copy of every *selected* material source (never
    # the non-selected downloads, which stay in the persistent media store).  This
    # block makes that retention rule explicit in the delivery instead of implicit.
    manifest["source_retention"] = {
        "keep_source_video": keep_source,
        "kept_count": kept_sources,
        "selected_count": len(material_sources),
        "note": (
            "04-原片 仅收录最终选用素材源片（每个最终选用源 1 份）；未选用的下载原片保留在持久化媒体库，不进入交付目录"
            if keep_source
            else "retention.keep_source_video=false：04-原片 不收录原片"
        ),
        "persistent_store": str(settings.get("media_root") or "data/media/material-replication"),
    }
    if visual_payload is not None:
        # Summary only: the per-clip OCR text and hit counts stay in
        # ``05-过程数据/visual_verify.json`` so the manifest does not carry a
        # second copy of it.  ``conclusive=False`` is recorded honestly here and
        # deliberately *not* folded into ``degraded``.
        manifest["visual_verify"] = {
            "conclusive": bool(visual_payload["conclusive"]),
            "verdicts": [
                {"video_id": item.get("video_id"), "verdict": item.get("verdict")}
                for item in visual_payload.get("items") or []
            ],
        }
    validation = validate_delivery_manifest(_write_manifest(stage, manifest))
    if validation["status"] != "pass":
        manifest["degraded"] = True
        manifest["warnings"] = [*manifest["warnings"], *[f"清单自校验：{error}" for error in validation["errors"]]]
        degraded = True
        _write_manifest(stage, manifest)
    if download_budget_block is not None:
        atomic_write_json(process_dir / "download_budget.json", {"schema_version": 1, **download_budget_block})
    atomic_write_json(process_dir / "face_metrics.json", {
        "backend": face_status.get("backend"),
        "status": face_status.get("status"),
        "model_present": face_status.get("model_present"),
        "checked_videos": counters["face_checked"],
    })
    full_run_log: dict[str, Any] = {
        "status": "done", "dry_run": False,
        "keywords": keywords_used, "keywords_requested": keywords_requested,
        "keywords_used": keywords_used, "keywords_truncated": keywords_truncated,
        "search_attribution": search_attribution, "pool_size": collected_candidate_count,
        "main_materials": len(main_materials), "supporting_materials": len(supporting_materials),
        "degraded": manifest["degraded"], "insufficient": insufficient,
        "script_replica": {
            "status": script_manifest.get("status"),
            "video_id": script_manifest.get("video_id"),
            "stage": script_manifest.get("stage"),
            "unmet_conditions": script_manifest.get("unmet_conditions"),
            "errors": script_manifest.get("errors"),
        },
        "material_replica": {
            "status": material_replica_block.get("status"),
            "conclusion": material_replica_block.get("conclusion"),
            "pool_size": material_replica_block.get("pool_size"),
            "heat_median": material_replica_block.get("heat_median"),
            "face_checked": material_replica_block.get("face_checked"),
            "selected": material_replica_block.get("selected"),
            "unmet_conditions": material_replica_block.get("rejected"),
            "errors": material_replica_block.get("errors"),
        },
    }
    if download_budget_block is not None:
        full_run_log["download_budget"] = download_budget_block
    if validation_block is not None:
        full_run_log["validation"] = {"counts": validation_block["counts"]}
    atomic_write_json(process_dir / "run_log.json", full_run_log)
    _publish(stage, destination, overwrite)

    if not candidates:
        status = "failed"
    elif script_result["status"] != "found" and not selected:
        status = "not_found"
    elif manifest["degraded"] or insufficient or script_result["status"] != "found":
        status = "partial"
    else:
        status = "success"
    return {
        "status": status,
        "output_dir": str(destination.resolve()),
        "manifest_path": str((destination / MANIFEST_NAME).resolve()),
        "counts": counters,
        "degraded": bool(manifest["degraded"]),
        "insufficient": insufficient,
        "warnings": manifest["warnings"],
        "dry_run": False,
    }


def _write_manifest(stage: Path, manifest: dict[str, Any]) -> Path:
    path = stage / MANIFEST_NAME
    atomic_write_json(path, manifest)
    _atomic_text(stage / DELIVERY_README, render_delivery_readme(manifest))
    return path


def _publish(stage: Path, destination: Path, overwrite: bool) -> None:
    if destination.exists() and not overwrite:
        remove_tree(stage)
        raise FileExistsError(f"交付目录已存在，使用 --overwrite 覆盖：{destination}")
    publish_directory(stage, destination)


def inspect_material_replication(config: dict[str, Any], folder: str | Path) -> dict[str, Any]:
    """Read and validate an existing delivery folder without any network access."""
    settings = material_replication_settings(config)
    output_root = project_path(config, settings.get("output_root") or "output/复刻视频")
    target = Path(folder)
    if not target.is_absolute():
        candidate = output_root / folder
        target = candidate if candidate.exists() else project_path(config, folder)
    manifest_path = target / MANIFEST_NAME
    validation = validate_delivery_manifest(manifest_path)
    payload: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
    return {
        "status": validation["status"],
        "folder": str(target),
        "manifest_path": str(manifest_path),
        "validation": validation,
        "counters": payload.get("counters", {}),
        "degraded": payload.get("degraded"),
        "insufficient": payload.get("insufficient"),
        "face_backend_status": payload.get("face_backend_status"),
        "evidence_disclaimer": payload.get("evidence_disclaimer", ""),
    }


def replication_doctor(config: dict[str, Any]) -> dict[str, Any]:
    """Offline runtime self-check for the material-replication workflow.

    Readiness reflects reality: the ASR backend must have usable weights on
    disk, not merely an importable module, because a missing model makes every
    script replica silently ``not_found``.
    """
    ffmpeg = media_tool_available(config, "ffmpeg")
    ffprobe = media_tool_available(config, "ffprobe")
    face = FaceDetector(config).status()
    asr = faster_whisper_status(config)
    ocr_settings = (config.get("materials") or {}).get("ocr") or {}
    ocr_ready = bool(ocr_settings.get("enabled", True)) and importlib.util.find_spec("rapidocr") is not None
    # Metadata duration availability: without the MediaCrawler patch the pre-download
    # duration window silently cannot run (window deferred to post-download).  This
    # is a *degradation*, not a hard failure: the pipeline still works, it is just
    # slower, so it degrades ``status`` and carries an explicit Chinese reason.
    metadata_duration = duration_patch_status(config)
    metadata_duration_ok = metadata_duration["status"] == "ok"
    degraded = not (
        ffmpeg and ffprobe and face["status"] == "ok" and asr["ready"] and metadata_duration_ok
    )
    return {
        "status": "ok" if not degraded else "degraded",
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "face": face,
        "model_present": face["model_present"],
        "asr": bool(asr["ready"]),
        "asr_model_present": asr["model_present"],
        "asr_model": {"model": asr["model"], "model_cache": asr["model_cache"]},
        "asr_reason": asr["reason"],
        "ocr": ocr_ready,
        "metadata_duration": metadata_duration,
        "metadata_duration_ok": metadata_duration_ok,
    }
