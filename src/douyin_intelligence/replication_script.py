"""Offline, LLM-free heuristic script skeleton extraction.

The skeleton is derived deterministically from the ASR segments and the video
duration.  When ASR fails it still produces a well-formed skeleton (empty
summaries plus warnings) so the workflow never stops for one bad transcript.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from .exporter import atomic_write_json
from .replication_candidates import Candidate


SCRIPT_SCHEMA_VERSION = 1
_SECTION_ORDER = ("hook", "pain_or_context", "product_reveal", "demo_or_compare", "conclusion", "cta")
_BORROWABLE_HINTS = (
    "用该段结构建立信息节奏，替换为本主题内容",
    "提炼该段的论证顺序，不要照抄文案",
    "借鉴该段的镜头语言与语速",
    "参考该段的对比方式组织素材",
)
_DISCLAIMER = "抖音素材仅为发现与关注度证据，不得作为事实依据；脚本骨架为自动启发式分段，需人工改写后使用。"


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


def _boundaries(duration: float) -> list[float]:
    """Eight monotonic, rounded time boundaries ending exactly at ``duration``."""
    total = max(0.01, float(duration))
    raw = [0.0, min(5.0, total * 0.15), total * 0.25, total * 0.45, total * 0.75, total * 0.85, total * 0.92, total]
    points: list[float] = []
    last = 0.0
    for value in raw:
        value = round(max(last, value), 2)
        points.append(value)
        last = value
    points[-1] = round(total, 2)
    return points


def _window_text(segments: list[dict[str, Any]], start: float, end: float) -> str:
    texts: list[str] = []
    for segment in segments:
        seg_start = float(segment.get("start") or 0)
        seg_end = float(segment.get("end") or seg_start)
        midpoint = (seg_start + seg_end) / 2
        if start <= midpoint < end or start <= seg_start < end:
            text = str(segment.get("text") or "").strip()
            if text:
                texts.append(text)
    return " ".join(texts).strip()


def _summary(segments: list[dict[str, Any]], start: float, end: float, fallback: str) -> str:
    text = _window_text(segments, start, end)
    return text[:60] if text else fallback


def build_script_skeleton(candidate: Candidate, transcript: dict[str, Any] | None, probe: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Build a structured script skeleton from ASR output and audio duration."""
    duration = float(probe.get("duration_seconds") or 0) or 1.0
    segments = [segment for segment in ((transcript or {}).get("segments") or []) if isinstance(segment, dict)]
    asr_status = str((transcript or {}).get("status") or "unavailable")
    warnings: list[str] = []
    if asr_status != "success" or not segments:
        warnings.append("ASR 未产出有效分段，脚本骨架按时间比例降级生成")
    fallback = "(无口播原文)" if warnings else "(该时间段无口播)"
    points = _boundaries(duration)
    sections: dict[str, dict[str, Any]] = {
        "hook": {"start": points[0], "end": points[1], "summary": _summary(segments, points[0], points[1], fallback)},
        "pain_or_context": {"start": points[1], "end": points[2], "summary": _summary(segments, points[1], points[2], fallback)},
        "product_reveal": {"start": points[2], "end": points[3], "summary": _summary(segments, points[2], points[3], fallback)},
        "demo_or_compare": {"start": points[4], "end": points[5], "summary": _summary(segments, points[4], points[5], fallback)},
        "conclusion": {"start": points[5], "end": points[6], "summary": _summary(segments, points[5], points[6], fallback)},
        "cta": {"start": points[6], "end": points[7], "summary": _summary(segments, points[6], points[7], fallback)},
    }
    key_points: list[dict[str, Any]] = []
    body_start, body_end = points[3], points[4]
    count = 3
    span = (body_end - body_start) / count
    for index in range(count):
        start = round(body_start + span * index, 2)
        end = round(body_end if index == count - 1 else body_start + span * (index + 1), 2)
        end = max(start, end)
        key_points.append({
            "index": index + 1,
            "start": start,
            "end": end,
            "summary": _summary(segments, start, end, fallback),
            "borrowable": _BORROWABLE_HINTS[index % len(_BORROWABLE_HINTS)],
        })
    return {
        "schema_version": SCRIPT_SCHEMA_VERSION,
        "video_id": candidate.video_id,
        "author": candidate.author,
        "source_url": candidate.source_url,
        "duration_seconds": round(duration, 2),
        "play_count": candidate.play_count,
        "heat_score": candidate.heat_score,
        "asr_status": asr_status,
        "sections": sections,
        "key_points": key_points,
        "warnings": warnings,
    }


def validate_script_skeleton(skeleton: dict[str, Any]) -> dict[str, Any]:
    """Self-check the skeleton schema; returns ``{"status", "errors"}``."""
    errors: list[str] = []
    if skeleton.get("schema_version") != SCRIPT_SCHEMA_VERSION:
        errors.append("脚本骨架 schema_version 无效")
    for key in _SECTION_ORDER:
        section = (skeleton.get("sections") or {}).get(key)
        if not isinstance(section, dict):
            errors.append(f"缺少分段 {key}")
    key_points = skeleton.get("key_points") or []
    if len(key_points) < 3:
        errors.append("key_points 少于 3 段")
    duration = float(skeleton.get("duration_seconds") or 0)
    previous = -1.0
    for point in key_points:
        start = float(point.get("start") or 0)
        end = float(point.get("end") or 0)
        if start < previous - 1e-6 or end < start - 1e-6:
            errors.append("key_points 时间码非单调递增")
        if end > duration + 0.5:
            errors.append("key_points 时间码超出视频时长")
        previous = end
    return {"status": "pass" if not errors else "fail", "errors": errors}


def _render_markdown(skeleton: dict[str, Any], transcript: dict[str, Any] | None) -> str:
    lines = [
        f"# 脚本思路：{skeleton.get('video_id')}",
        "",
        f"> {_DISCLAIMER}",
        "",
        f"- 来源作者：{skeleton.get('author') or '未知'}",
        f"- 原始链接：{skeleton.get('source_url') or '未知'}",
        f"- 视频时长：{skeleton.get('duration_seconds')} 秒",
        f"- 热度（池内归一化）：{skeleton.get('heat_score')}",
        f"- 转写状态：{skeleton.get('asr_status')}",
        "",
        "## 分段骨架",
        "",
    ]
    label = {
        "hook": "开场钩子 (hook)",
        "pain_or_context": "背景/痛点 (pain_or_context)",
        "product_reveal": "主体亮相 (product_reveal)",
        "demo_or_compare": "演示/对比 (demo_or_compare)",
        "conclusion": "结论 (conclusion)",
        "cta": "互动引导 (cta)",
    }
    for key in ("hook", "pain_or_context", "product_reveal"):
        section = skeleton["sections"][key]
        lines.append(f"- **{label[key]}** `{section['start']}~{section['end']}s`：{section['summary']}")
    lines.extend(["", "## 要点（可借鉴点）", ""])
    for point in skeleton["key_points"]:
        lines.append(f"- **要点 {point['index']}** `{point['start']}~{point['end']}s`：{point['summary']}")
        lines.append(f"  - 可借鉴点：{point['borrowable']}")
    for key in ("demo_or_compare", "conclusion", "cta"):
        section = skeleton["sections"][key]
        lines.append(f"- **{label[key]}** `{section['start']}~{section['end']}s`：{section['summary']}")
    if skeleton.get("warnings"):
        lines.extend(["", "## 降级提示", ""])
        lines.extend(f"- {warning}" for warning in skeleton["warnings"])
    lines.append("")
    return "\n".join(lines)


def write_script_artifacts(
    directory: Path,
    candidate: Candidate,
    skeleton: dict[str, Any],
    transcript: dict[str, Any] | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Write the four script artifacts and return their file names."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_json(directory / "脚本骨架.json", skeleton)
    text = str((transcript or {}).get("text") or "")
    _atomic_text(directory / "口播全文.txt", text + ("\n" if text else ""))
    atomic_write_json(directory / "来源.json", {
        "video_id": candidate.video_id,
        "author": candidate.author,
        "source_url": candidate.source_url,
        "published_at": candidate.published_at,
        "play_count": candidate.play_count,
        "heat_score": candidate.heat_score,
        "heat_rank": candidate.heat_rank,
    })
    _atomic_text(directory / "脚本思路.md", _render_markdown(skeleton, transcript))
    return {
        "skeleton": "脚本骨架.json",
        "transcript_text": "口播全文.txt",
        "source": "来源.json",
        "script_notes": "脚本思路.md",
    }
