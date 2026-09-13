"""Face-free interval derivation and lossless clip export.

Intervals are derived from the cached per-frame face flags.  Clips are cut
with ``ffmpeg -c copy``; when ffmpeg is unavailable the exporter degrades to
"source video + interval list" instead of failing.  Cleanup never uses
``shutil.rmtree`` — only per-file ``unlink`` plus ``rmdir``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MEDIA_PROCESS_TIMEOUT_SECONDS = 180
CLIP_SCHEMA_VERSION = 1


@dataclass(slots=True)
class ClipInterval:
    start: float
    end: float

    def duration(self) -> float:
        return round(max(0.0, self.end - self.start), 3)


def _run_media_process(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False, timeout=MEDIA_PROCESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, "", "media process timed out")


def _atomic_json(path: Path, payload: Any) -> None:
    from .exporter import atomic_write_json
    atomic_write_json(path, payload)


def remove_tree(path: Path) -> None:
    """Best-effort per-file recursive removal (never uses ``shutil.rmtree``)."""
    root = Path(path)
    if not root.exists():
        return
    for child in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        try:
            if child.is_dir():
                child.rmdir()
            else:
                child.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def derive_face_free_intervals(
    face_per_frame: list[bool],
    duration: float,
    *,
    min_seconds: float = 3.0,
    max_seconds: float = 8.0,
    interval_seconds: float = 1.0,
) -> list[ClipInterval]:
    """Turn per-frame face flags into 3~8s face-free clip intervals."""
    intervals: list[ClipInterval] = []
    total = max(0.0, float(duration))
    step = float(interval_seconds)
    if not face_per_frame or step <= 0 or total <= 0:
        return intervals
    index = 0
    count = len(face_per_frame)
    while index < count:
        if face_per_frame[index]:
            index += 1
            continue
        run_start = index
        while index < count and not face_per_frame[index]:
            index += 1
        run_end = index
        cursor = round(run_start * step, 3)
        segment_end = round(min(total, run_end * step), 3)
        while segment_end - cursor >= float(min_seconds) - 1e-9:
            chunk_end = round(min(segment_end, cursor + float(max_seconds)), 3)
            if chunk_end - cursor < float(min_seconds) - 1e-9:
                break
            intervals.append(ClipInterval(cursor, chunk_end))
            cursor = chunk_end
    return intervals


def build_clip_metadata(
    *,
    clip_id: str,
    role: str,
    file_name: str,
    source: dict[str, Any],
    timecode: dict[str, Any],
    media: dict[str, Any],
    face: dict[str, Any],
    suggested_use: str,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build a clip metadata record aligned with the output specification."""
    return {
        "schema_version": CLIP_SCHEMA_VERSION,
        "clip_id": clip_id,
        "role": role,
        "file": file_name,
        "source": source,
        "timecode": timecode,
        "media": media,
        "face": face,
        "suggested_use": suggested_use,
        "warnings": list(warnings or []),
    }


def export_video_clips(
    ffmpeg: str | None,
    source: Path,
    video_duration: float,
    clips: list[ClipInterval],
    destination_dir: Path,
    *,
    role: str = "main",
    label_prefix: str = "clip",
) -> dict[str, Any]:
    """Export clip files with ``ffmpeg -c copy``, or degrade to source + intervals."""
    source = Path(source)
    destination_dir = Path(destination_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    extension = source.suffix or ".mp4"

    if not ffmpeg:
        copied_name = ""
        try:
            copied = destination_dir / f"{label_prefix}-原片{extension}"
            shutil.copy2(source, copied)
            copied_name = copied.name
        except OSError:
            copied_name = ""
        intervals = [{"start": clip.start, "end": clip.end, "duration": clip.duration()} for clip in clips]
        intervals_file = f"{label_prefix}-区间.json"
        _atomic_json(destination_dir / intervals_file, {"source": source.name, "role": role, "intervals": intervals, "degraded": True})
        return {
            "status": "degraded",
            "degraded": True,
            "source_file": copied_name,
            "intervals": intervals,
            "intervals_file": intervals_file,
            "clips": [],
            "warnings": ["ffmpeg 不可用，已退化交付原片与区间清单"],
        }

    results: list[dict[str, Any]] = []
    for index, clip in enumerate(clips, 1):
        file_name = f"{label_prefix}-{index:02d}{extension}"
        output = destination_dir / file_name
        command = [
            ffmpeg, "-y", "-v", "error",
            "-ss", f"{clip.start:.3f}", "-to", f"{clip.end:.3f}",
            "-i", str(source), "-c", "copy", str(output),
        ]
        completed = _run_media_process(command)
        ok = completed.returncode == 0 and output.is_file() and output.stat().st_size > 0
        results.append({
            "index": index,
            "file": file_name if ok else "",
            "start": clip.start,
            "end": clip.end,
            "duration": clip.duration(),
            "status": "ok" if ok else "failed",
            "error": (completed.stderr or "").strip()[:200] if not ok else "",
        })
    succeeded = [row for row in results if row["status"] == "ok"]
    status = "success" if results and len(succeeded) == len(results) else "partial" if succeeded else "failed"
    return {"status": status, "degraded": False, "clips": results, "warnings": []}
