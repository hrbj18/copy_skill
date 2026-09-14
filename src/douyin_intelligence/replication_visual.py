"""Fully automatic visual confirmation for delivered source videos.

A delivered source clip is often unrelated to the theme product (the
"mechanical duck robot" run shipped Unitree humanoid clips).  Keyword and
relevance gates only see *metadata*, so this module adds the one genuinely
visual dimension the machine can read without a VLM: **on-screen text**.

For every video it samples three frames (first / 1/3 / 2/3), OCRs them, and
concatenates the OCR text with the file name (which carries the author and the
post title) into ``visual_text``.  A clip is a ``hit`` when at least one
theme subject term appears in that text.

The whole step is automatic by construction: there is no confirmation prompt,
no pause, and no code path that waits for a human.  Every failure (missing
file, ffmpeg error, no frame extracted, OCR exception) degrades that single
item to ``verdict="unknown"`` and the batch keeps going; nothing is raised.

Both external effects are injectable through ``deps`` so the decision logic is
testable offline without ffmpeg or the RapidOCR models.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from .media_tools import resolve_media_tool


#: Sample points as a fraction of the clip duration: first frame, 1/3, 2/3.
FRAME_FRACTIONS = (0.0, 1.0 / 3.0, 2.0 / 3.0)
DEFAULT_FRAME_WIDTH = 960
DEFAULT_FRAME_TIMEOUT_SECONDS = 20.0

#: Delivered copies are named ``<author>_<title>_<19-digit aweme_id>.mp4``.
_AWEME_ID = re.compile(r"(\d{19})$")


def visual_verify_settings(config: dict[str, Any]) -> dict[str, Any]:
    """The ``material_replication.visual_verify`` block, or ``{}`` when absent."""
    jobs = config.get("jobs") if isinstance(config, dict) else None
    replication = (jobs or {}).get("material_replication") if isinstance(jobs, dict) else None
    block = (replication or {}).get("visual_verify") if isinstance(replication, dict) else None
    return block if isinstance(block, dict) else {}


def video_id_from_name(video: Path) -> str:
    """Extract the trailing aweme id; fall back to the bare file stem."""
    stem = Path(video).stem
    match = _AWEME_ID.search(stem)
    return match.group(1) if match else stem


def verify_videos(
    video_paths: list[Path],
    subject_terms: list[str],
    config: dict,
    *,
    deps: object | None = None,
) -> dict:
    """Return the automatic visual verdict for a batch of delivered clips.

    ``subject_terms`` must already be casefolded by the caller.  The batch is
    ``conclusive`` when at least one clip is a ``hit``; a batch that is all
    ``miss``/``unknown`` proves nothing and must not be used as a positive
    confirmation.  Never raises.
    """
    settings = visual_verify_settings(config)
    if not settings.get("enabled", True):
        return {"enabled": False, "conclusive": False, "items": []}

    terms = [str(term).casefold().strip() for term in subject_terms or [] if str(term or "").strip()]
    extract_frames = _frame_extractor(config if isinstance(config, dict) else {}, settings, deps)
    ocr_frame = _ocr_frame(deps)

    items: list[dict[str, Any]] = []
    workspace = Path(tempfile.mkdtemp(prefix="visual-verify-"))
    try:
        for index, video in enumerate(video_paths or []):
            items.append(_verify_one(Path(video), terms, workspace, index, extract_frames, ocr_frame))
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    return {
        "enabled": True,
        "conclusive": any(item["verdict"] == "hit" for item in items),
        "items": items,
    }


def _verify_one(
    video: Path,
    terms: list[str],
    workspace: Path,
    index: int,
    extract_frames: Callable[[Path, Path], list[Path]],
    ocr_frame: Callable[[Path], str],
) -> dict[str, Any]:
    """One clip's verdict; any failure degrades this item to ``unknown``."""
    item: dict[str, Any] = {
        "video_id": video_id_from_name(video),
        "frames": 0,
        "ocr_text": "",
        "subject_hits": 0,
        "verdict": "unknown",
    }
    try:
        if not video.is_file():
            return item
        destination = workspace / f"video-{index:03d}"
        destination.mkdir(parents=True, exist_ok=True)
        frames = [Path(frame) for frame in extract_frames(video, destination)]
        frames = [frame for frame in frames if frame.is_file()]
        if not frames:
            return item
        texts = [str(ocr_frame(frame) or "") for frame in frames]
    except Exception:
        # ffmpeg failed / no frame / OCR blew up -> this clip is unproven.
        return item

    ocr_text = "\n".join(text for text in texts if text.strip())
    # The file name carries the post title, so it is part of the visual evidence
    # even when the sampled frames happen to hold no readable text.
    visual_text = f"{video.name}\n{ocr_text}".casefold()
    hits = sum(1 for term in terms if term in visual_text)
    item.update({
        "frames": len(texts),
        "ocr_text": ocr_text,
        "subject_hits": hits,
        "verdict": "hit" if hits > 0 else "miss",
    })
    return item


def _frame_extractor(
    config: dict[str, Any], settings: dict[str, Any], deps: object | None
) -> Callable[[Path, Path], list[Path]]:
    provided = getattr(deps, "frame_extractor", None) if deps is not None else None
    return provided if provided is not None else _default_frame_extractor(config, settings)


def _ocr_frame(deps: object | None) -> Callable[[Path], str]:
    provided = getattr(deps, "ocr", None) if deps is not None else None
    if provided is not None:
        return provided
    # Reuse the project's existing RapidOCR parsing (visual_ocr) instead of
    # wiring a second engine: ``parse_rapidocr_output`` normalises the
    # ``txts``/``scores``/``boxes`` payload into reading-ordered lines.
    from .visual_ocr import parse_rapidocr_output

    engine: list[Any] = []

    def recognise(frame: Path) -> str:
        if not engine:
            from rapidocr import RapidOCR
            engine.append(RapidOCR())
        return "\n".join(line["text"] for line in parse_rapidocr_output(engine[0](frame)))
    return recognise


def _default_frame_extractor(
    config: dict[str, Any], settings: dict[str, Any]
) -> Callable[[Path, Path], list[Path]]:
    """Sample first / 1/3 / 2/3 frames using the existing visual_ocr helpers."""
    from .visual_ocr import _probe_duration, _rerender

    timeout = float(settings.get("frame_timeout_seconds") or DEFAULT_FRAME_TIMEOUT_SECONDS)
    width = int(settings.get("frame_width") or DEFAULT_FRAME_WIDTH)
    tools = {
        "ffmpeg_path": resolve_media_tool(config, "ffmpeg"),
        "ffprobe_path": resolve_media_tool(config, "ffprobe"),
    }

    def extract(video: Path, destination: Path) -> list[Path]:
        duration = _probe_duration(video, timeout, tools)
        frames: list[Path] = []
        for index, fraction in enumerate(FRAME_FRACTIONS):
            timestamp = round(duration * fraction, 3)
            frames.append(_rerender(
                video,
                {"timestamp_seconds": timestamp},
                destination / f"frame-{index:02d}.jpg",
                width,
                timeout,
                tools,
            ))
        return frames
    return extract
