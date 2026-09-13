from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .exporter import atomic_write_json
from .models import VideoRecord


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": "1.0", "videos": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": "1.0", "videos": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("videos"), dict):
        return {"version": "1.0", "videos": {}}
    return payload


def prepare_state(state: dict[str, Any], records: Iterable[VideoRecord], target_date: str) -> tuple[dict[str, Any], dict[str, Any]]:
    videos = dict(state.get("videos") or {})
    previously_seen: list[str] = []
    new_video_ids: list[str] = []
    for record in records:
        existing = videos.get(record.video_id)
        if isinstance(existing, dict):
            targets = [str(value) for value in existing.get("target_dates") or []]
            if target_date not in targets:
                previously_seen.append(record.video_id)
                targets.append(target_date)
            existing = {
                **existing,
                "last_target_date": target_date,
                "target_dates": sorted(set(targets))[-30:],
                "last_title": record.title,
                "last_url": record.share_url,
            }
        else:
            new_video_ids.append(record.video_id)
            existing = {
                "first_target_date": target_date,
                "last_target_date": target_date,
                "target_dates": [target_date],
                "last_title": record.title,
                "last_url": record.share_url,
            }
        videos[record.video_id] = existing
    payload = {"version": "1.0", "videos": {key: videos[key] for key in sorted(videos)}}
    return payload, {
        "known_before_count": len(state.get("videos") or {}),
        "new_video_count": len(new_video_ids),
        "previously_seen_other_day_count": len(previously_seen),
        "previously_seen_other_day_ids": sorted(previously_seen),
        "known_after_count": len(videos),
    }


def save_state(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)

