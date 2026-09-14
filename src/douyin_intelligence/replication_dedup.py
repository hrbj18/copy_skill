"""Cross-run material de-duplication index.

``jobs.material_replication.dedup_across_runs`` (optional, unset by default)
stops a period from delivering a clip an earlier period already delivered: the
9.14 batch shipped the same ``video_id`` in more than one delivery, which the
acceptance list forbids (「跨期重复的 video_id 数量 = 0」).

The index lives next to the other material caches
(``<cache_root>/delivered_index.json``) and is a *cache*, never a source of
truth: a missing, unreadable or malformed file means "nothing has been delivered
yet" and must never abort a run -- losing it costs duplicate material, not a
failed batch.  Writes go through :func:`exporter.atomic_write_json` (temp file +
``os.replace`` + fsync), so an interrupted write can never leave a half-file
behind that would silently switch de-duplication off.

Shape -- one mapping, keyed by the bare ``video_id``::

    {"<video_id>": {"author", "title", "bytes", "published_at",
                    "theme", "delivered_at"}}

A later delivery of the same clip **overwrites** the earlier record.  The
composite ``(source, video_id)`` identity is deliberately *not* used here: this
round runs douyin-only (``sources`` is off), and an id is only unique within one
source, so when multi-source acquisition is switched on the key must be widened
to ``source:video_id`` in the same change that turns that switch on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .exporter import atomic_write_json

#: File name inside ``jobs.material_replication.cache_root``.
DELIVERED_INDEX_NAME = "delivered_index.json"
#: Fallback cache root, matching ``jobs.material_replication.cache_root``.
DEFAULT_CACHE_ROOT = "data/cache/material-replication"
#: The switch that turns cross-run de-duplication on (absent == off).
SWITCH_KEY = "dedup_across_runs"


def _settings(config: dict[str, Any]) -> dict[str, Any]:
    return ((config or {}).get("jobs") or {}).get("material_replication") or {}


def dedup_enabled(config: dict[str, Any]) -> bool:
    """Whether cross-run de-duplication is switched on (default: off).

    Off by default so every config written before this feature behaves exactly as
    before, and so a period can deliberately be re-run against the same pool.
    """
    return bool(_settings(config).get(SWITCH_KEY, False))


def delivered_index_path(config: dict[str, Any]) -> Path:
    """Resolve ``<cache_root>/delivered_index.json`` (honouring ``_project_root``)."""
    # Local import: ``replication_theme`` is shared with other stages, and this
    # module must stay importable without pulling it in at module load.
    from .replication_theme import project_path

    cache_root = _settings(config).get("cache_root") or DEFAULT_CACHE_ROOT
    return project_path(config, cache_root) / DELIVERED_INDEX_NAME


def empty_index() -> dict[str, Any]:
    """A fresh, empty index."""
    return {}


def load_delivered_index(path: Path) -> dict[str, Any]:
    """Read the index, degrading to an empty one whenever it is unusable.

    Only ``dict`` payloads are honoured, and only their ``dict`` values are
    kept: an entry of any other shape is dropped rather than trusted, because a
    half-written record must not be able to fake a delivery.  A missing file, an
    unreadable file and malformed JSON are all "nothing delivered yet" -- never
    an exception.
    """
    if not path.is_file():
        return empty_index()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return empty_index()
    if not isinstance(payload, dict):
        return empty_index()
    return {str(key): dict(value) for key, value in payload.items() if isinstance(value, dict)}


def save_delivered_index(path: Path, index: dict[str, Any]) -> None:
    """Write the index atomically (temp file + ``os.replace`` + fsync)."""
    atomic_write_json(path, index)


def delivered_entry(index: dict[str, Any], candidate: "Any") -> dict[str, Any] | None:
    """This candidate's previous delivery record, or ``None`` when it is new."""
    video_id = str(getattr(candidate, "video_id", "") or "").strip()
    if not video_id:
        return None
    entry = index.get(video_id) if isinstance(index, dict) else None
    return entry if isinstance(entry, dict) else None


def duplicate_reason(entry: dict[str, Any]) -> str:
    """A human-readable "why this candidate is skipped" line for ``unmet``."""
    theme = str(entry.get("theme") or "").strip()
    when = str(entry.get("delivered_at") or "").strip()[:10]
    where = "、".join(part for part in (theme, when) if part) or "更早的一期"
    return f"跨期重复：该素材已在 {where} 交付过（{entry.get('title') or ''}），本期跳过"


def cross_run_duplicate_reason(
    config: dict[str, Any], index: dict[str, Any], candidate: "Any"
) -> str:
    """``""`` when the candidate is new, else the reason it must be skipped.

    The switch is honoured *here* rather than at every call site, so a caller
    cannot accidentally apply the gate with the feature switched off.
    """
    if not dedup_enabled(config):
        return ""
    entry = delivered_entry(index, candidate)
    return duplicate_reason(entry) if entry is not None else ""


def record_delivered(
    index: dict[str, Any], records: Iterable[dict[str, Any]], *, theme: str, delivered_at: str
) -> list[str]:
    """Merge ``records`` into ``index`` in memory; return the ``video_id``s seen.

    Each record is a mapping carrying ``video_id`` plus the provenance fields
    (``author`` / ``title`` / ``bytes`` / ``published_at``).  A clip delivered
    again **overwrites** its earlier record, so the entry always describes the
    most recent delivery.  A record without a ``video_id`` is skipped: a blank
    key would poison the index into matching every future blank id.
    """
    added: list[str] = []
    for record in records:
        video_id = str(record.get("video_id") or "").strip()
        if not video_id:
            continue
        index[video_id] = {
            "author": str(record.get("author") or ""),
            "title": str(record.get("title") or ""),
            "bytes": int(record.get("bytes") or 0),
            "published_at": str(record.get("published_at") or ""),
            "theme": str(theme or ""),
            "delivered_at": str(delivered_at or ""),
        }
        added.append(video_id)
    return added


def remember_delivered(
    config: dict[str, Any], records: Iterable[dict[str, Any]], *, theme: str, delivered_at: str = ""
) -> int:
    """Load, merge and persist the index in one call; return the recorded count.

    A no-op (0, no file touched) when cross-run de-duplication is switched off,
    so the call site needs no switch of its own.
    """
    if not dedup_enabled(config):
        return 0
    entries = [
        record for record in records if str(record.get("video_id") or "").strip()
    ]
    if not entries:
        return 0
    path = delivered_index_path(config)
    index = load_delivered_index(path)
    added = record_delivered(index, entries, theme=theme, delivered_at=delivered_at)
    if added:
        save_delivered_index(path, index)
    return len(added)


def delivered_video_ids(index: dict[str, Any]) -> set[str]:
    """Every delivered ``video_id`` (for the cross-period QA cross-check)."""
    if not isinstance(index, dict):
        return set()
    return {str(key).strip() for key in index if str(key).strip()}
