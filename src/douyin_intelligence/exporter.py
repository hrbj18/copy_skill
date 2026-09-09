from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .models import VideoRecord


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.replace(temporary, path)
    except Exception:
        if temporary and temporary.exists():
            temporary.unlink()
        raise


def build_outputs(
    records: Iterable[VideoRecord],
    *,
    target_date: str,
    timezone_name: str,
    top_n: int,
    run_report: dict[str, Any],
) -> dict[str, Any]:
    selected = list(records)[: max(1, top_n)]
    captured_at = datetime.now(ZoneInfo(timezone_name)).isoformat(timespec="seconds")
    hot_items = []
    benchmark_videos = []
    for record in selected:
        if record.source in {"douyin_search", "douyin_hotboard"}:
            observed_heat = record.play_count
            if observed_heat is None:
                observed_heat = (record.digg_count or 0) + (record.comment_count or 0) * 3 + (record.share_count or 0) * 5
            hot_items.append({
                "word": record.title,
                "hotScore": int(observed_heat),
                "url": record.share_url,
                "video_id": record.video_id,
                "signal_kind": record.source,
                "score": record.score,
            })
        if record.source == "douyin_creator":
            benchmark_videos.append({
                "title": record.title,
                "account_name": record.account_name,
                "play_count": int(record.play_count or 0),
                "share_url": record.share_url,
                "video_id": record.video_id,
                "digg_count": record.digg_count,
                "comment_count": record.comment_count,
                "share_count": record.share_count,
                "collect_count": record.collect_count,
                "published_at": record.published_at,
                "score": record.score,
                "play_count_missing": record.play_count is None,
            })
    return {
        "hotboard.json": {"captured_at": captured_at, "target_date": target_date, "items": hot_items},
        "benchmark_accounts.json": {"captured_at": captured_at, "target_date": target_date, "videos": benchmark_videos},
        "content_candidates.json": {
            "version": "1.0",
            "captured_at": captured_at,
            "target_date": target_date,
            "items": [record.to_dict() for record in selected],
        },
        "run_report.json": {**run_report, "captured_at": captured_at, "target_date": target_date},
    }


def export_outputs(output_dir: str | Path, payloads: dict[str, Any]) -> list[Path]:
    root = Path(output_dir)
    written: list[Path] = []
    for name, payload in payloads.items():
        destination = root / name
        atomic_write_json(destination, payload)
        written.append(destination)
    return written

