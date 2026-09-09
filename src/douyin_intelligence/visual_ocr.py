from __future__ import annotations

import math
import os
import re
import shutil
import statistics
import subprocess
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, TypeVar

from PIL import Image, ImageChops

from .media_tools import resolve_visual_tool


_SHOWINFO_TIME = re.compile(r"pts_time:([0-9.]+)")
_SAFE_TEXT = re.compile(r"[^0-9A-Za-z\u3400-\u9fff]+")
_UI_TEXT = {"抖音", "关注", "点击关注", "长按点赞", "点赞关注", "关注我"}
T = TypeVar("T")


def _process_flags() -> int:
    return subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0


def _bounded_run(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    if timeout <= 0:
        raise TimeoutError("视觉处理预算已耗尽")
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=max(0.1, timeout),
            creationflags=_process_flags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"子进程超过 {timeout:.1f} 秒预算") from exc


def frame_budget_for_duration(duration: float, settings: dict[str, Any]) -> tuple[int, int]:
    for row in settings["frame_budgets"]:
        maximum = row.get("max_duration_seconds")
        if maximum is None or duration <= float(maximum):
            return int(row["first_pass"]), int(row["hard"])
    raise ValueError("visual_ocr.frame_budgets 缺少兜底档")


def evenly_limit(items: list[T], maximum: int) -> list[T]:
    if maximum <= 0:
        return []
    if len(items) <= maximum:
        return list(items)
    if maximum == 1:
        return [items[0]]
    indices = [round(index * (len(items) - 1) / (maximum - 1)) for index in range(maximum)]
    return [items[index] for index in indices]


def _probe_duration(video: Path, timeout: float, settings: dict[str, Any]) -> float:
    result = _bounded_run(
        [resolve_visual_tool(settings, "ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(video)],
        timeout,
    )
    if result.returncode:
        raise RuntimeError("无法读取视频时长")
    duration = float(result.stdout.strip())
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("视频时长无效")
    return duration


def _extract_pass(video: Path, output: Path, vf: str, timeout: float, settings: dict[str, Any]) -> tuple[list[Path], list[float]]:
    output.mkdir(parents=True, exist_ok=True)
    pattern = str(output / "frame-%05d.jpg")
    result = _bounded_run(
        [resolve_visual_tool(settings, "ffmpeg"), "-y", "-hide_banner", "-loglevel", "info", "-i", str(video), "-vf", vf, "-vsync", "vfr", "-q:v", "3", pattern],
        timeout,
    )
    if result.returncode:
        raise RuntimeError(f"画面提取失败：{result.stderr[-200:]}")
    return sorted(output.glob("frame-*.jpg")), [float(value) for value in _SHOWINFO_TIME.findall(result.stderr)]


def _extract_tail(video: Path, output: Path, width: int, timeout: float, settings: dict[str, Any]) -> Path | None:
    path = output / "tail.jpg"
    result = _bounded_run(
        [resolve_visual_tool(settings, "ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-sseof", "-0.5", "-i", str(video), "-frames:v", "1", "-vf", f"scale=min({width}\\,iw):-2", "-q:v", "3", str(path)],
        timeout,
    )
    return path if result.returncode == 0 and path.is_file() else None


def _mean_delta_and_ratio(left: Path, right: Path) -> tuple[float, float] | None:
    try:
        with Image.open(left) as first, Image.open(right) as second:
            first_gray = first.convert("L").resize((96, 96))
            second_gray = second.convert("L").resize((96, 96))
            diff = ImageChops.difference(first_gray, second_gray)
            values = list(diff.get_flattened_data())
    except (OSError, ValueError):
        return None
    if not values:
        return None
    return sum(values) / len(values), sum(value >= 8 for value in values) / len(values)


def conservative_visual_dedupe(candidates: list[dict[str, Any]], settings: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    if len(candidates) < 2:
        return candidates, 0
    kept = [candidates[0]]
    dropped = 0
    for candidate in candidates[1:]:
        delta = _mean_delta_and_ratio(Path(kept[-1]["path"]), Path(candidate["path"]))
        duplicate = bool(
            delta is not None
            and delta[0] <= float(settings["near_identical_mean_delta"])
            and delta[1] <= float(settings["near_identical_changed_ratio"])
        )
        if duplicate and candidate is not candidates[-1]:
            Path(candidate["path"]).unlink(missing_ok=True)
            dropped += 1
        else:
            kept.append(candidate)
    return kept, dropped


def extract_scene_interval_frames(
    video: Path,
    workspace: Path,
    settings: dict[str, Any],
    *,
    maximum: int,
    deadline: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    duration = _probe_duration(video, min(10.0, deadline - time.monotonic()), settings)
    width = int(settings["first_pass_width"])
    threshold = float(settings["scene_threshold"])
    interval = float(settings["interval_seconds"])
    scene_paths, scene_times = _extract_pass(
        video,
        workspace / "scene",
        f"select=eq(n\\,0)+gt(scene\\,{threshold}),scale=min({width}\\,iw):-2,showinfo",
        deadline - time.monotonic(), settings,
    )
    interval_paths, interval_times = _extract_pass(
        video,
        workspace / "interval",
        f"fps=1/{interval},scale=min({width}\\,iw):-2,showinfo",
        deadline - time.monotonic(), settings,
    )
    candidates: list[dict[str, Any]] = []
    for reason, paths, times in (("scene_change", scene_paths, scene_times), ("interval_guard", interval_paths, interval_times)):
        for index, path in enumerate(paths):
            timestamp = times[index] if index < len(times) else min(duration, index * interval)
            candidates.append({"path": str(path), "timestamp_seconds": round(timestamp, 3), "selection_reason": reason})
    tail = _extract_tail(video, workspace, width, deadline - time.monotonic(), settings)
    if tail:
        candidates.append({"path": str(tail), "timestamp_seconds": round(duration, 3), "selection_reason": "tail_guard"})
    candidates.sort(key=lambda row: (row["timestamp_seconds"], row["selection_reason"], row["path"]))
    merged: list[dict[str, Any]] = []
    for candidate in candidates:
        if merged and abs(candidate["timestamp_seconds"] - merged[-1]["timestamp_seconds"]) <= 0.12:
            reasons = set(str(merged[-1]["selection_reason"]).split("+")) | {str(candidate["selection_reason"])}
            merged[-1]["selection_reason"] = "+".join(sorted(reasons))
            Path(candidate["path"]).unlink(missing_ok=True)
        else:
            merged.append(candidate)
    candidate_count = len(merged)
    limited = evenly_limit(merged, maximum)
    selected_paths = {row["path"] for row in limited}
    for candidate in merged:
        if candidate["path"] not in selected_paths:
            Path(candidate["path"]).unlink(missing_ok=True)
    deduped, dropped = conservative_visual_dedupe(limited, settings)
    for index, row in enumerate(deduped):
        row["frame_index"] = index
    return deduped, {
        "duration_seconds": round(duration, 3),
        "candidate_frames": candidate_count,
        "selected_frames": len(deduped),
        "visual_duplicates_removed": dropped,
    }


def _box_value(box: Any) -> list[list[float]]:
    try:
        return [[round(float(point[0]), 2), round(float(point[1]), 2)] for point in box]
    except (TypeError, ValueError, IndexError):
        return []


def _reading_key(line: dict[str, Any]) -> tuple[float, float]:
    box = line["box"]
    if not box:
        return (0.0, 0.0)
    return (min(point[1] for point in box), min(point[0] for point in box))


def parse_rapidocr_output(output: Any) -> list[dict[str, Any]]:
    raw_texts = getattr(output, "txts", None)
    raw_scores = getattr(output, "scores", None)
    raw_boxes = getattr(output, "boxes", None)
    texts = list(raw_texts) if raw_texts is not None else []
    scores = list(raw_scores) if raw_scores is not None else []
    boxes = list(raw_boxes) if raw_boxes is not None else []
    lines = []
    for index, text in enumerate(texts):
        value = str(text or "").strip()
        if not value:
            continue
        lines.append({
            "text": value[:1000],
            "confidence": round(float(scores[index]) if index < len(scores) else 0.0, 4),
            "box": _box_value(boxes[index]) if index < len(boxes) else [],
        })
    return sorted(lines, key=_reading_key)


def _normalized(text: str) -> str:
    return _SAFE_TEXT.sub("", text).casefold()


def _looks_like_ui(line: dict[str, Any], image_size: tuple[int, int]) -> bool:
    normalized = _normalized(line["text"])
    if normalized not in {_normalized(value) for value in _UI_TEXT}:
        return False
    box = line.get("box") or []
    if not box:
        return False
    width, height = image_size
    center_x = sum(point[0] for point in box) / len(box)
    center_y = sum(point[1] for point in box) / len(box)
    return center_x <= width * 0.16 or center_x >= width * 0.84 or center_y >= height * 0.88


def _numeric_tokens(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?%?", text))


def merge_text_cards(frames: list[dict[str, Any]], similarity: float) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for frame in frames:
        text = "\n".join(line["text"] for line in frame.get("clean_lines") or []).strip()
        normalized = _normalized(text)
        if not normalized:
            continue
        replacement: int | None = None
        duplicate: int | None = None
        for index, card in enumerate(cards):
            existing = card["normalized"]
            new_numbers = _numeric_tokens(text)
            old_numbers = _numeric_tokens(card["text"])
            if new_numbers and old_numbers and new_numbers != old_numbers:
                continue
            ratio = SequenceMatcher(None, existing, normalized).ratio()
            if normalized in existing or existing in normalized or ratio >= similarity:
                duplicate = index
                if len(normalized) > len(existing):
                    replacement = index
                break
        if duplicate is None:
            cards.append({
                "text": text,
                "normalized": normalized,
                "timestamps": [frame["timestamp_seconds"]],
                "source_frame_indexes": [frame["frame_index"]],
            })
        else:
            card = cards[duplicate]
            card["timestamps"].append(frame["timestamp_seconds"])
            card["source_frame_indexes"].append(frame["frame_index"])
            if replacement is not None:
                card["text"], card["normalized"] = text, normalized
    for card in cards:
        card.pop("normalized", None)
    return cards


@dataclass
class VisualBatchBudget:
    soft_limit: int
    hard_limit: int
    deadline: float
    first_pass_frames: int = 0
    total_ocr_frames: int = 0

    def first_pass_allowance(self, requested: int) -> int:
        if time.monotonic() >= self.deadline:
            return 0
        return max(0, min(requested, self.soft_limit - self.first_pass_frames, self.hard_limit - self.total_ocr_frames))

    def retry_allowance(self, requested: int) -> int:
        if time.monotonic() >= self.deadline:
            return 0
        return max(0, min(requested, self.hard_limit - self.total_ocr_frames))


def _ocr_one(engine: Callable[[Path], Any], frame: dict[str, Any]) -> dict[str, Any]:
    with Image.open(frame["path"]) as image:
        size = image.size
    lines = parse_rapidocr_output(engine(Path(frame["path"])))
    clean = [line for line in lines if not _looks_like_ui(line, size)]
    confidence = statistics.fmean(line["confidence"] for line in clean) if clean else 0.0
    return {
        **{key: frame[key] for key in ("frame_index", "timestamp_seconds", "selection_reason")},
        "image_size": list(size),
        "raw_lines": lines,
        "clean_lines": clean,
        "clean_text": "\n".join(line["text"] for line in clean),
        "mean_confidence": round(confidence, 4),
        "retry_used": False,
    }


def _rerender(video: Path, frame: dict[str, Any], destination: Path, width: int, timeout: float, settings: dict[str, Any]) -> Path:
    result = _bounded_run(
        [resolve_visual_tool(settings, "ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-ss", str(frame["timestamp_seconds"]), "-i", str(video), "-frames:v", "1", "-vf", f"scale=min({width}\\,iw):-2", "-q:v", "2", str(destination)],
        timeout,
    )
    if result.returncode or not destination.is_file():
        raise RuntimeError("高分辨率定向重试抽帧失败")
    return destination


def _quality(frames: list[dict[str, Any]], cards: list[dict[str, Any]], settings: dict[str, Any], budget_exhausted: bool) -> dict[str, Any]:
    suspected = [row for row in frames if row["clean_lines"]]
    usable = [
        row for row in suspected
        if len(_normalized(row["clean_text"])) >= int(settings["usable_min_chars"])
        and row["mean_confidence"] >= float(settings["usable_min_confidence"])
    ]
    coverage = len(usable) / len(suspected) if suspected else 0.0
    scores = [line["confidence"] for row in suspected for line in row["clean_lines"]]
    median = statistics.median(scores) if scores else 0.0
    content = "\n\n".join(card["text"] for card in cards)
    chars = len(_normalized(content))
    long_cards = sum(len(_normalized(card["text"])) >= int(settings["success_min_card_chars"]) for card in cards)
    success = (
        (chars >= int(settings["success_min_chars"]) or long_cards >= int(settings["success_min_cards"]))
        and median >= float(settings["success_min_confidence"])
        and coverage >= float(settings["success_min_coverage"])
        and not budget_exhausted
    )
    partial = chars >= int(settings["partial_min_chars"]) and (
        median >= float(settings["partial_min_confidence"]) or coverage >= float(settings["partial_min_coverage"])
    )
    return {
        "quality_tier": "success" if success else "partial" if partial else "unavailable",
        "unique_content_chars": chars,
        "median_ocr_confidence": round(median, 4),
        "visual_text_coverage": round(coverage, 4),
        "suspected_text_frames": len(suspected),
        "usable_text_frames": len(usable),
        "unique_text_cards": len(cards),
    }


def process_visual_video(
    video: Path,
    workspace: Path,
    settings: dict[str, Any],
    batch: VisualBatchBudget,
    *,
    engine: Callable[[Path], Any] | None = None,
    qa_export_dir: Path | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = min(started + float(settings["per_video_timeout_seconds"]), batch.deadline)
    base = {
        "visual_text_status": "unavailable", "candidate_frames": 0, "selected_frames": 0,
        "ocr_frames": 0, "retry_frames": 0, "unique_text_cards": 0, "unique_content_chars": 0,
        "median_ocr_confidence": 0.0, "visual_text_coverage": 0.0, "budget_exhausted": False,
        "frames": [], "text_cards": [], "merged_text": "",
    }
    try:
        if batch.first_pass_allowance(1) <= 0:
            return base | {"visual_text_status": "budget_exhausted", "budget_exhausted": True, "error": "全批视觉预算已耗尽"}
        duration = _probe_duration(video, min(10.0, deadline - time.monotonic()), settings)
        requested, _hard = frame_budget_for_duration(duration, settings)
        allowed = batch.first_pass_allowance(requested)
        if allowed <= 0:
            return base | {"visual_text_status": "budget_exhausted", "budget_exhausted": True, "error": "全批视觉预算已耗尽"}
        frames, meta = extract_scene_interval_frames(video, workspace / "frames", settings, maximum=allowed, deadline=deadline)
        truncated_by_batch = allowed < requested
        if engine is None:
            from rapidocr import RapidOCR
            rapid = RapidOCR()
            engine = rapid
        evidence: list[dict[str, Any]] = []
        for frame in frames:
            if time.monotonic() >= deadline:
                break
            evidence.append(_ocr_one(engine, frame))
        batch.first_pass_frames += len(evidence)
        batch.total_ocr_frames += len(evidence)
        truncated_by_deadline = len(evidence) < len(frames)
        low_confidence = [row for row in evidence if row["raw_lines"] and row["mean_confidence"] < float(settings["success_min_confidence"])]
        retry_count = batch.retry_allowance(min(len(low_confidence), int(settings["retry_max_frames"])))
        retry_dir = workspace / "retry"
        retry_dir.mkdir(parents=True, exist_ok=True)
        completed_retries = 0
        for row in evenly_limit(low_confidence, retry_count):
            if time.monotonic() >= deadline:
                break
            path = _rerender(video, row, retry_dir / f"retry-{row['frame_index']:04d}.jpg", int(settings["retry_width"]), deadline - time.monotonic(), settings)
            retried = _ocr_one(engine, {"path": str(path), **{key: row[key] for key in ("frame_index", "timestamp_seconds", "selection_reason")}})
            completed_retries += 1
            if retried["mean_confidence"] >= row["mean_confidence"]:
                retried["retry_used"] = True
                evidence[row["frame_index"]] = retried
        batch.total_ocr_frames += completed_retries
        cards = merge_text_cards(evidence, float(settings["text_similarity"]))
        budget_exhausted = truncated_by_batch or truncated_by_deadline or time.monotonic() >= deadline
        quality = _quality(evidence, cards, settings, budget_exhausted)
        visual_status = "budget_exhausted" if budget_exhausted else quality["quality_tier"]
        if qa_export_dir and evidence:
            qa_export_dir.mkdir(parents=True, exist_ok=True)
            for row in evenly_limit(evidence, min(3, len(evidence))):
                source = frames[row["frame_index"]]["path"]
                shutil.copy2(source, qa_export_dir / f"frame-{row['frame_index']:04d}-{row['timestamp_seconds']:.3f}.jpg")
        return base | meta | quality | {
            "visual_text_status": visual_status,
            "ocr_frames": len(evidence),
            "retry_frames": completed_retries,
            "budget_exhausted": budget_exhausted,
            "frames": evidence,
            "text_cards": cards,
            "merged_text": "\n\n".join(card["text"] for card in cards),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    except TimeoutError as exc:
        return base | {"visual_text_status": "budget_exhausted", "budget_exhausted": True, "error": str(exc)[:200]}
    except Exception as exc:
        return base | {"visual_text_status": "ocr_error", "error": f"视觉OCR失败：{type(exc).__name__}"}
