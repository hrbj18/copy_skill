"""Project-owned redaction boundary for untrusted crawler metadata."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .normalize import infer_source, load_raw_records, normalize_record


_PLAIN_SHARE_URL = re.compile(r"https://(?:www\.)?douyin\.com/video/(\d{8,})(?:[/?#].*)?$")
_SENSITIVE_FIELD = re.compile(r"(?:authorization|cookie|token|signature|play_addr|download_addr|video_(?:download_)?url|websocket|browser_data|user_data)", re.IGNORECASE)


def _text(value: Any, maximum: int = 2_000) -> str:
    return str(value or "").strip()[:maximum]


def _safe_share_url(video_id: str, value: str) -> str:
    if video_id.isdigit():
        return f"https://www.douyin.com/video/{video_id}"
    match = _PLAIN_SHARE_URL.fullmatch(value)
    return f"https://www.douyin.com/video/{match.group(1)}" if match else ""


# Clip length in **milliseconds**.  Douyin nests it under ``video.duration``; the
# patched crawler flattens it to ``duration_ms``.  Both are plain metadata (not a
# signed URL / credential), so redaction keeps them -- without this the pre-download
# duration window would have nothing to read and would defer to post-download.
_DURATION_MS_PATHS = (
    "duration_ms", "video_duration_ms",
    "video.duration", "aweme_detail.video.duration",
)


def _duration_ms(item: dict[str, Any]) -> Any:
    for path in _DURATION_MS_PATHS:
        current: Any = item
        for part in path.split("."):
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(part)
        if current not in (None, ""):
            return current
    return None


def _canonical_row(item: dict[str, Any], config: dict[str, Any], source: str) -> dict[str, Any]:
    record = normalize_record(
        item,
        source=source,
        timezone_name=str(config["timezone"]),
        categories=config["categories"],
    )
    row = {
        "aweme_id": record.video_id,
        "title": _text(record.title),
        "share_url": _safe_share_url(record.video_id, record.share_url),
        "account_id": _text(record.account_id, 512),
        "account_name": _text(record.account_name, 512),
        "published_at": record.published_at or "",
        "source_keyword": _text(record.source_keyword, 256),
        "creator_hash": _text(item.get("creator_hash"), 512),
    }
    for key in ("play_count", "digg_count", "comment_count", "share_count", "collect_count"):
        value = getattr(record, key)
        if value is not None:
            row[key] = value
    duration_ms = _duration_ms(item)
    if duration_ms not in (None, ""):
        row["duration_ms"] = duration_ms
    return {key: value for key, value in row.items() if value not in (None, "")}


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    temporary: Path | None = None
    try:
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent, delete=False)
        temporary = Path(handle.name)
        handle.write(text)
        handle.close()
        handle = None
        os.replace(temporary, path)
    except Exception:
        if handle is not None:
            handle.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _sensitive_field_count(value: Any) -> int:
    if isinstance(value, dict):
        return sum((1 if _SENSITIVE_FIELD.search(str(key)) else 0) + _sensitive_field_count(item) for key, item in value.items())
    if isinstance(value, list):
        return sum(_sensitive_field_count(item) for item in value)
    return 0


def sanitize_raw_file(path: str | Path, config: dict[str, Any], source: str | None = None, maximum_records: int | None = None) -> dict[str, int | str]:
    """Rewrite one crawler artifact to a metadata-only allowlist in place."""
    target = Path(path)
    rows = load_raw_records(target)
    original_count = len(rows)
    if maximum_records is not None:
        rows = rows[:max(0, maximum_records)]
    canonical = [_canonical_row(row, config, source or infer_source(target)) for row in rows]
    if target.suffix.lower() == ".jsonl":
        payload = "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in canonical)
        _atomic_text(target, payload + ("\n" if payload else ""))
    else:
        _atomic_text(target, json.dumps(canonical, ensure_ascii=False, indent=2) + "\n")
    original_fields = sum(len(row) for row in rows)
    safe_fields = sum(len(row) for row in canonical)
    return {
        "path": str(target.resolve()),
        "record_count": len(canonical),
        "discarded_record_count": max(0, original_count - len(canonical)),
        "removed_field_count": max(0, original_fields - safe_fields),
        "removed_sensitive_field_count": sum(_sensitive_field_count(row) for row in rows),
    }


def sanitize_raw_files(paths: Iterable[str | Path], config: dict[str, Any], source: str | None = None, maximum_records: int | None = None) -> list[dict[str, int | str]]:
    remaining = maximum_records
    results: list[dict[str, int | str]] = []
    for path in paths:
        result = sanitize_raw_file(path, config, source, remaining)
        results.append(result)
        if remaining is not None:
            remaining = max(0, remaining - int(result["record_count"]))
    return results
