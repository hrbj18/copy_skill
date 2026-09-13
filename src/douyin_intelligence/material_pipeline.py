from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .config import resolve_path
from .exporter import atomic_write_json
from .llm_analysis import OpenAICompatibleAnalyzer
from .materials import download_video, probe_video, select_candidates
from .media_processing import CheckpointTranscriber, KeyframeOCR
from .resource_control import cleanup_temp_root, remove_tree


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.replace(temporary, path)
    except Exception:
        if temporary and temporary.exists():
            temporary.unlink()
        raise


def _timestamp(seconds: float) -> str:
    value = max(0, int(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _analysis_result(analysis: dict[str, Any]) -> dict[str, Any]:
    return analysis.get("result") if analysis.get("status") == "success" and isinstance(analysis.get("result"), dict) else {}


def render_material_markdown(
    item: dict[str, Any], technical: dict[str, Any], transcript: dict[str, Any],
    ocr: dict[str, Any] | None, analysis: dict[str, Any], retained_video: Path | None,
) -> str:
    record = item["record"]
    result = _analysis_result(analysis)
    lines = [
        f"# {record.title or record.video_id}", "",
        "> 本文档是选题与创作素材，不是事实证据；所有事实主张必须回查官方或一手来源。", "",
        "## 高价值结论", "",
        result.get("value_summary") or f"语义分析状态：{analysis.get('status', 'unknown')}。当前可先使用原始标题和时间戳转写。", "",
        "### 核心信息", "",
    ]
    core_points = result.get("core_points") or []
    lines.extend(f"- {point}" for point in core_points)
    if not core_points:
        lines.append("- 尚未生成大模型核心信息。")
    lines.extend(["", "### 最佳内容片段", ""])
    moments = result.get("best_moments") or []
    for moment in moments:
        lines.append(f"- `{moment.get('timestamp', '--:--')}` {moment.get('content', '')} — {moment.get('reason', '')}")
    if not moments:
        lines.append("- 尚未生成最佳片段。")
    lines.extend(["", "### 可延伸选题", ""])
    lines.extend(f"- {angle}" for angle in (result.get("content_angles") or []))
    if not result.get("content_angles"):
        lines.append("- 优先回查产品、项目或公司的官方信息后再确定选题。")
    lines.extend(["", "### 待核验事项", ""])
    lines.extend(f"- {claim}" for claim in (result.get("claims_to_verify") or []))
    if not result.get("claims_to_verify"):
        lines.append("- 标题、字幕、转写中的日期、数字、规格、价格、排名与公司表态均需核验。")
    lines.extend([
        "", "## 来源与热度", "",
        f"- 对标账号：{record.account_name}（{record.account_id}）",
        f"- 原作品：[{record.share_url}]({record.share_url})",
        f"- 发布时间：{record.published_at or '未知'}",
        f"- 内容分类：{record.category}", f"- 价值评分：{record.score}",
        f"- 评分依据：{'；'.join(record.score_reasons)}",
        f"- 互动数据：点赞 {record.digg_count or 0} / 评论 {record.comment_count or 0} / 分享 {record.share_count or 0} / 收藏 {record.collect_count or 0}",
        "", "## 原始发布文案", "", record.title or "（无标题）", "",
    ])
    if ocr and ocr.get("items"):
        lines.extend(["## 画面 OCR", ""])
        lines.extend(f"- `{_timestamp(float(row.get('time') or 0))}` {row.get('text', '')}" for row in ocr["items"])
        lines.append("")
    lines.extend(["## 语音转写", ""])
    if transcript.get("segments"):
        lines.extend(f"- `{_timestamp(float(segment['start']))}` {segment['text']}" for segment in transcript["segments"])
    else:
        lines.append(f"（{transcript.get('status', 'unknown')}：{transcript.get('error', '没有识别到有效语音')}）")
    lines.extend([
        "", "## 媒体与处理状态", "", f"- 时长：{technical['duration_seconds']} 秒",
        f"- 画面：{technical.get('width')}×{technical.get('height')} / {technical.get('codec')}",
        f"- 原视频长期保留：{'是：`' + str(retained_video.resolve()) + '`' if retained_video else '否（处理后已清理）'}",
        f"- 转写状态：{transcript.get('status')}", f"- OCR 状态：{(ocr or {}).get('status', 'not_needed')}",
        f"- 语义分析状态：{analysis.get('status')}", f"- 分析模型：{analysis.get('model') or '未使用'}", "",
        "## 使用约束", "", "- 不可直接复制账号结论作为新闻事实。",
        "- 涉及发布日期、价格、规格、公司表态、销量、排名和性能时，必须用官方或一手来源复核。", "",
    ])
    return "\n".join(lines)


def render_summary(run_id: str, outputs: list[dict[str, Any]], destination: Path) -> str:
    lines = [f"# 抖音高价值素材摘要：{run_id}", "", "> 以下内容用于选题与创作参考，事实必须回查一手来源。", ""]
    for index, output in enumerate(outputs, 1):
        result = _analysis_result(output["analysis"])
        relative = Path(output["markdown_path"]).relative_to(destination)
        lines.extend([
            f"## {index}. {output['title']}", "", result.get("value_summary") or "尚未完成语义分析，请查看时间戳转写。", "",
            f"- 账号：{output['account_name']}（{output['account_id']}）", f"- 评分：{output['score']}",
            f"- 详情：[打开完整素材文档]({relative.as_posix()})", f"- 原视频：[{output['source_url']}]({output['source_url']})", "",
            "### 核心信息", "",
        ])
        points = result.get("core_points") or []
        lines.extend(f"- {point}" for point in points[:5])
        if not points:
            lines.append("- 尚未生成。")
        lines.extend(["", "### 最佳时间片段", ""])
        moments = result.get("best_moments") or []
        lines.extend(f"- `{item.get('timestamp', '--:--')}` {item.get('content', '')} — {item.get('reason', '')}" for item in moments[:3])
        if not moments:
            lines.append("- 尚未生成。")
        lines.extend(["", "### 延伸方向与核验", ""])
        lines.extend(f"- 选题：{value}" for value in (result.get("content_angles") or [])[:3])
        lines.extend(f"- 核验：{value}" for value in (result.get("claims_to_verify") or [])[:3])
        lines.append("")
    return "\n".join(lines)


def build_materials(
    run_dir: str | Path, config: dict[str, Any], output_dir: str | Path | None = None,
    *, transcription: bool = True, analysis_enabled: bool = True, keep_video: bool | None = None,
) -> dict[str, Any]:
    source = Path(run_dir).resolve()
    settings = config["materials"]
    retention = settings.get("retention") or {}
    keep = bool(retention.get("keep_video", False)) if keep_video is None else keep_video
    destination = Path(output_dir).resolve() if output_dir else resolve_path(settings["output_root"]) / source.name
    temp_root = resolve_path(retention.get("temp_root") or "data/temp/materials")
    cache_root = resolve_path(settings.get("cache_root") or "data/cache/materials") / source.name
    media_root = resolve_path(settings["media_root"]) / source.name
    cleanup_before = cleanup_temp_root(temp_root, ttl_hours=float(retention.get("ttl_hours") or 24), quota_bytes=int(retention.get("quota_bytes") or 2147483648))
    selected, warnings = select_candidates(source, config)
    transcriber = CheckpointTranscriber(config, enabled=transcription)
    ocr_engine = KeyframeOCR(config)
    analyzer = OpenAICompatibleAnalyzer(config)
    if not analysis_enabled:
        analyzer.settings.enabled = False
    outputs: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    cleaned_bytes = 0

    for item in selected:
        record = item["record"]
        material_id = f"{record.account_id}-{record.video_id}"
        work_dir = temp_root / source.name / material_id
        cache_dir = cache_root / material_id
        work_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        legacy_video = media_root / f"{material_id}.mp4"
        retained_video = media_root / f"{material_id}.mp4" if keep else None
        working_video = legacy_video if legacy_video.is_file() else (retained_video or work_dir / "video.mp4")
        markdown = destination / "videos" / f"{material_id}.md"
        try:
            technical_cache = cache_dir / "technical.json"
            transcript_cache = cache_dir / "transcript.json"
            ocr_cache = cache_dir / "ocr.json"
            cached_transcript = json.loads(transcript_cache.read_text(encoding="utf-8")) if transcript_cache.is_file() else None
            can_run_text_only = bool(
                technical_cache.is_file() and cached_transcript
                and (cached_transcript.get("status") != "no_speech" or ocr_cache.is_file())
            )
            if can_run_text_only:
                technical = json.loads(technical_cache.read_text(encoding="utf-8"))
                transcript = cached_transcript
                transcript["cache_hit"] = True
                ocr = json.loads(ocr_cache.read_text(encoding="utf-8")) if cached_transcript.get("status") == "no_speech" else None
                if ocr is not None:
                    ocr["cache_hit"] = True
            else:
                download_video(str(item["raw"]["video_download_url"]), working_video, config)
                technical = probe_video(working_video)
                atomic_write_json(technical_cache, technical)
                legacy_transcript = legacy_video.with_suffix(".transcript.json") if legacy_video.with_suffix(".transcript.json").is_file() else None
                transcript = transcriber.run(working_video, cache_dir, work_dir, legacy_cache=legacy_transcript)
                ocr = None
                if transcript.get("status") == "no_speech":
                    ocr = ocr_engine.run(working_video, float(technical["duration_seconds"]), cache_dir, work_dir)
            try:
                analysis = analyzer.analyze(title=record.title, transcript=transcript, ocr=ocr, cache_path=cache_dir / "analysis.json")
            except Exception as exc:
                analysis = {"status": "error", "error": str(exc)[:500]}
            _atomic_text(markdown, render_material_markdown(item, technical, transcript, ocr, analysis, retained_video if keep else None))
            output = {
                "video_id": record.video_id, "account_id": record.account_id, "account_name": record.account_name,
                "title": record.title, "score": record.score, "source_url": record.share_url,
                "markdown_path": str(markdown.resolve()), "media_retained": keep,
                "retained_video_path": str(retained_video.resolve()) if retained_video else None,
                "technical": technical, "transcription_status": transcript.get("status"),
                "ocr_status": (ocr or {}).get("status", "not_needed"), "analysis": analysis,
            }
            outputs.append(output)
            if transcript.get("status") == "error":
                warnings.append(f"作品 {record.video_id} 转写失败：{transcript.get('error')}")
            if transcript.get("status") == "no_speech" and (ocr or {}).get("status") != "success":
                warnings.append(f"作品 {record.video_id} 无语音且 OCR 未获得正文")
            if analyzer.settings.enabled and analysis.get("status") != "success":
                warnings.append(f"作品 {record.video_id} 语义分析未完成：{analysis.get('error', analysis.get('status'))}")
            if not keep:
                if legacy_video.is_file():
                    cleaned_bytes += legacy_video.stat().st_size
                    legacy_video.unlink()
                legacy_transcript_path = legacy_video.with_suffix(".transcript.json")
                if legacy_transcript_path.is_file():
                    cleaned_bytes += legacy_transcript_path.stat().st_size
                    legacy_transcript_path.unlink()
            if work_dir.exists():
                cleaned_bytes += remove_tree(work_dir, temp_root)
        except Exception as exc:
            errors.append({"video_id": record.video_id, "error": str(exc)[:500]})

    semantic_required = analyzer.settings.enabled
    all_good = bool(outputs) and not errors
    for output in outputs:
        if output["transcription_status"] == "error":
            all_good = False
        if output["transcription_status"] == "no_speech" and output["ocr_status"] != "success":
            all_good = False
        if semantic_required and output["analysis"].get("status") != "success":
            all_good = False
    status = "success" if all_good else "partial" if outputs else "failed"
    _atomic_text(destination / "summary.md", render_summary(source.name, outputs, destination))
    report_outputs = [{**output, "analysis": {key: value for key, value in output["analysis"].items() if key != "cache_key"}} for output in outputs]
    report = {
        "status": status, "run_id": source.name, "source_run_dir": str(source), "output_dir": str(destination.resolve()),
        "selected_count": len(selected), "completed_count": len(outputs), "outputs": report_outputs,
        "retention": {"keep_video": keep, "cleaned_bytes": cleaned_bytes, "temp_cleanup": cleanup_before, "temp_remaining_bytes": sum(path.stat().st_size for path in temp_root.rglob("*") if path.is_file())},
        "llm": analyzer.status(), "warnings": warnings, "errors": errors,
    }
    atomic_write_json(destination / "run_report.json", report)
    return report
