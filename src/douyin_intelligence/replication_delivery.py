"""Delivery directory tree, manifest schema and atomic publication.

The manifest always carries an ``evidence_disclaimer`` and explicit
``degraded`` / ``insufficient`` / ``warnings`` fields so downstream consumers
know exactly which parts are evidence and which are guesses.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from .exporter import atomic_write_json
from .replication_clips import remove_tree


MANIFEST_SCHEMA_VERSION = 1
MANIFEST_NAME = "清单.json"
FOLDER_SCRIPT = "01-脚本思路"
FOLDER_MAIN = "02-主素材"
FOLDER_SUPPORT = "03-辅助素材"
FOLDER_SOURCE = "04-原片"
FOLDER_PROCESS = "05-过程数据"
DELIVERY_README = "00-交付说明.md"

#: Machine-readable material catalog (downstream selection contract).  It sits
#: beside the manifest and, unlike ``清单.json``, lists one row per *physical*
#: media file with the provenance/tag/score a downstream picker needs.
CATALOG_SCHEMA_VERSION = 1
CATALOG_NAME = "00-素材目录.json"

# ``direct_delivery`` (opt-in) ships each selected source as a whole file into
# 02/03, so a *second* copy of the same file in 04-原片 would double the delivery's
# bytes for zero new information.  In that mode 04-原片 carries these two index
# files instead of the videos (machine-readable + human-readable), and every
# side-car's ``source.folder`` points at the index entry rather than a ``.mp4``
# that is not there.
SOURCE_INDEX_NAME = "原片索引.json"
SOURCE_INDEX_README = "原片索引.md"

# The user's stated ceiling for a delivery directory is 200,000,000 bytes
# (2026-09-18 acquisition strategy, §3.5).  It is a *hard* cap: the gate in
# ``replication_pipeline`` refuses to publish a run whose whole delivery folder
# exceeds it, so a size blow-up can never ship silently.  The catalog's own
# ``max_total_bytes`` uses the same value.
MAX_DELIVERY_FOLDER_BYTES = 200_000_000

DISCLAIMER = "抖音素材仅为发现与关注度证据，不得作为事实依据；人脸指标为自动检测结果，交付前需人工复核。"

REQUIRED_MANIFEST_KEYS = (
    "schema_version", "theme", "folder", "business_date", "generated_at", "keywords_used",
    "candidate_pool_size", "script_replica", "material_replica_sources", "main_materials",
    "supporting_materials", "counters", "face_backend", "face_backend_status", "ffmpeg_status",
    "degraded", "insufficient", "warnings", "evidence_disclaimer",
)

#: Deterministic, auditable label vocabularies for the catalog (2026-09-18
#: strategy §3.3).  ``unknown`` is legal in every set: a value that cannot be
#: judged must degrade honestly instead of inventing official authority.
SOURCE_KINDS = ("official_original", "news_broadcast", "creator_commentary", "platform_video", "unknown")
SOURCE_AUTHORITIES = ("official", "news_media", "creator", "unknown")
VISUAL_ROLES = (
    "event_direct", "subject_person", "product_or_scene",
    "news_anchor_or_reporter", "commentary", "unknown",
)
RECOMMENDED_USAGES = ("main", "supporting", "optional")
RIGHTS_STATUSES = (
    "unknown", "review_required", "reference_only",
    "renderable_with_attribution", "project_generated",
)

CATALOG_REQUIRED_KEYS = (
    "schema_version", "theme", "folder", "generated_at",
    "max_total_bytes", "total_bytes", "count", "entries",
)

CATALOG_ENTRY_KEYS = (
    "material_id", "file_path", "role", "recommended_usage", "source_url", "title", "author",
    "source_kind", "source_authority", "visual_role", "rights_status",
    "relevance_score", "heat_score", "duration_seconds", "file_bytes", "face_class",
)


class DeliveryFolderOverLimit(RuntimeError):
    """The staged delivery folder exceeds :data:`MAX_DELIVERY_FOLDER_BYTES`.

    Raised *before* publication, so the stage is left intact for inspection but
    no over-size delivery is ever published (and the destination tree is never
    half-overwritten).
    """


def evidence_disclaimer() -> str:
    return DISCLAIMER


def ensure_delivery_tree(root: Path) -> None:
    root = Path(root)
    for name in (FOLDER_SCRIPT, FOLDER_MAIN, FOLDER_SUPPORT, FOLDER_SOURCE, FOLDER_PROCESS):
        (root / name).mkdir(parents=True, exist_ok=True)


def delivery_folder_bytes(root: Path) -> int:
    """Total size in bytes of every *file* under a delivery directory.

    The user's spec is on the delivery directory **as a whole** (70~150 MB), not
    on any single sub-folder: measuring only ``02/03`` misses ``04-原片`` and
    measuring only ``04-原片`` misses ``02/03``.  Summing them all (recursively)
    keeps the number faithful to what a shell ``du -sb`` or Explorer reports, so
    it can be compared against the ceiling directly.  An unreadable entry is
    skipped rather than aborting the count: a transient Windows lock must not
    turn a size report into a crash.
    """
    total = 0
    for path in Path(root).rglob("*"):
        if not path.is_file():
            continue
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


# --------------------------------------------------------------------------- #
# 00-素材目录.json -- the machine-readable material catalog (strategy §3.4)
# --------------------------------------------------------------------------- #
def _safe_relative_path(value: Any) -> str | None:
    """Normalize a delivery-relative POSIX path, or ``None`` when it is unsafe.

    A catalog ``file_path`` must be relative to the delivery root and must not
    escape it: an absolute path (``C:\\…`` / ``/…``) leaks the machine layout and a
    ``..`` segment can point *outside* the delivery.  Both are rejected rather
    than silently normalized, so tampering is visible instead of absorbed.
    """
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return None
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


#: Media-CDN hosts and signing query keys ByteDance's CDNs actually use.  Kept
#: deliberately narrow so a normal share link (``https://www.douyin.com/video/…``)
#: is never misread as a signed download URL.
_SIGNED_HOST_SUFFIXES = (
    "douyinvod.com", "snssdk.com", "byteimg.com", "douyinpic.com",
    "douyincdn.com", "ixigua.com", "pstatp.com", "bytedance.com",
)
_SIGNED_QUERY_MARKERS = (
    "x-expires", "x-expire", "expires=", "ossexpires", "ossaccesskeyid",
    "signature=", "x-signature", "a_bogus", "x-bogus", "ms_token",
    "sessionid=", "session_key", "auth_key", "authkey", "token=", "ttl=",
    "policy=", "sig=", "secret=",
)


def is_signed_download_url(url: Any) -> bool:
    """True when ``url`` looks like a *signed* (temporary, credentialed) media URL.

    The catalog's ``source_url`` must be the candidate's public share page; a
    signed download URL carries temporary credentials and must never be published
    into a delivery (strategy §3.4).  ``""`` (unknown) is not a violation.
    """
    text = str(url or "").strip()
    if not text:
        return False
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if any(host == suffix or host.endswith("." + suffix) for suffix in _SIGNED_HOST_SUFFIXES):
        return True
    query = (parsed.query or "").lower()
    return any(marker in query for marker in _SIGNED_QUERY_MARKERS)


def _as_number(value: Any) -> float | None:
    """``float`` for a real number, ``None`` for anything else (bool excluded)."""
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _normalized_label(value: Any, allowed: tuple[str, ...], default: str) -> str:
    text = str(value or "").strip()
    return text if text in allowed else default


def _catalog_entry(
    record: dict[str, Any],
    *,
    role: str,
    index: int,
    detail: dict[str, Any] | None,
    root: Path | None,
) -> dict[str, Any]:
    """One catalog row for one physical media file.

    Built from the manifest's main/support record (guaranteeing exact coverage)
    plus optional ``detail`` provenance the pipeline knows at copy time.  Any
    label that cannot be judged falls back to ``unknown`` (or
    ``review_required`` for rights), never to an invented value.
    """
    info = dict(detail or {})
    file_path = _safe_relative_path(record.get("file")) or str(record.get("file") or "")
    usage = str(info.get("recommended_usage") or "")
    if usage not in RECOMMENDED_USAGES:
        usage = "main" if role == "main" else "supporting"
    file_bytes = info.get("file_bytes")
    if root is not None and file_path:
        try:
            file_bytes = (Path(root) / file_path).stat().st_size
        except OSError:
            pass
    if not isinstance(file_bytes, (int, float)):
        file_bytes = info.get("bytes") if isinstance(info.get("bytes"), (int, float)) else 0
    duration = record.get("duration")
    if not isinstance(duration, (int, float)):
        duration = info.get("duration_seconds")
    return {
        "material_id": str(record.get("clip_id") or info.get("clip_id") or f"{role}-{index:02d}"),
        "file_path": file_path,
        "role": role,
        "recommended_usage": usage,
        "source_url": str(info.get("source_url") or ""),
        "source_platform": str(info.get("source") or ""),
        "title": str(info.get("title") or ""),
        "author": str(info.get("author") or ""),
        "video_id": str(info.get("video_id") or ""),
        "source_kind": _normalized_label(info.get("source_kind"), SOURCE_KINDS, "unknown"),
        "source_authority": _normalized_label(info.get("source_authority"), SOURCE_AUTHORITIES, "unknown"),
        "visual_role": _normalized_label(info.get("visual_role"), VISUAL_ROLES, "unknown"),
        "rights_status": _normalized_label(info.get("rights_status"), RIGHTS_STATUSES, "review_required"),
        "relevance_score": _as_number(info.get("relevance_score")),
        "heat_score": _as_number(info.get("heat_score")),
        "duration_seconds": _as_number(duration) or 0.0,
        "file_bytes": int(file_bytes or 0),
        "face_class": str(record.get("face_class") or info.get("face_class") or "unknown"),
        "suggested_use": str(record.get("suggested_use") or ""),
    }


def build_material_catalog(
    *,
    theme: str,
    folder: str,
    business_date: str,
    generated_at: str,
    main_materials: list[dict[str, Any]],
    supporting_materials: list[dict[str, Any]],
    details: dict[str, dict[str, Any]] | None = None,
    root: Path | None = None,
    max_total_bytes: int = MAX_DELIVERY_FOLDER_BYTES,
) -> dict[str, Any]:
    """Assemble the ``00-素材目录.json`` payload.

    Rows come from the *same* ``main_materials`` / ``supporting_materials`` the
    manifest carries, so the catalog can never list a file the manifest does not
    (or miss one it does).  ``details`` maps a delivered relative path to the
    provenance the pipeline knows when it copies the file (title, author,
    ``source_url``, tags, scores).  ``root`` (the delivery root) lets ``file_bytes``
    be read straight from disk so it equals the entity size by construction.
    """
    lookup = {str(key): dict(value) for key, value in (details or {}).items()}
    entries: list[dict[str, Any]] = []
    for index, record in enumerate(main_materials, start=1):
        detail = lookup.get(str(record.get("file") or ""))
        entries.append(_catalog_entry(record, role="main", index=index, detail=detail, root=root))
    for index, record in enumerate(supporting_materials, start=1):
        detail = lookup.get(str(record.get("file") or ""))
        entries.append(_catalog_entry(record, role="support", index=index, detail=detail, root=root))
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "theme": theme,
        "folder": folder,
        "business_date": business_date,
        "generated_at": generated_at,
        "max_total_bytes": int(max_total_bytes),
        "total_bytes": sum(int(entry["file_bytes"]) for entry in entries),
        "count": len(entries),
        "entries": entries,
    }


def material_catalog_path(root: Path) -> Path:
    return Path(root) / CATALOG_NAME


def write_material_catalog(root: Path, catalog: dict[str, Any]) -> Path:
    """Write the catalog atomically so a partial file can never be published."""
    path = material_catalog_path(root)
    atomic_write_json(path, catalog)
    return path


def validate_material_catalog(
    path: Path,
    *,
    root: Path | None = None,
    expected_files: Iterable[str] | None = None,
    max_total_bytes: int = MAX_DELIVERY_FOLDER_BYTES,
) -> dict[str, Any]:
    """Self-contained catalog check: safe paths, real files, exact bytes, legality.

    Verifies each entry's path is a safe delivery-relative path, that the file
    exists and its size equals ``file_bytes``, that no physical file is listed
    twice, that every label is within a legal vocabulary, that no ``source_url``
    is a signed download URL, and that the catalog total is within the cap.
    ``expected_files`` (the manifest's main/support files) makes the check
    bidirectionally complete: nothing may be missing, nothing extra may appear.
    """
    target = Path(path)
    errors: list[str] = []
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "fail", "path": str(target), "errors": [f"素材目录无法读取：{exc}"],
                "count": 0, "total_bytes": 0, "files": []}
    if not isinstance(payload, dict):
        return {"status": "fail", "path": str(target), "errors": ["素材目录不是 JSON 对象"],
                "count": 0, "total_bytes": 0, "files": []}
    for key in CATALOG_REQUIRED_KEYS:
        if key not in payload:
            errors.append(f"素材目录缺少字段 {key}")
    delivery_root = Path(root) if root is not None else target.parent
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        errors.append("素材目录 entries 必须为列表")
        raw_entries = []
    seen: set[str] = set()
    files: list[str] = []
    total = 0
    for position, entry in enumerate(raw_entries, start=1):
        label = f"第 {position} 条"
        if not isinstance(entry, dict):
            errors.append(f"{label} 不是对象")
            continue
        for key in CATALOG_ENTRY_KEYS:
            if key not in entry:
                errors.append(f"{label} 缺少字段 {key}")
        for key, allowed in (
            ("source_kind", SOURCE_KINDS),
            ("source_authority", SOURCE_AUTHORITIES),
            ("visual_role", VISUAL_ROLES),
            ("recommended_usage", RECOMMENDED_USAGES),
            ("rights_status", RIGHTS_STATUSES),
        ):
            value = str(entry.get(key) or "")
            if value not in allowed:
                errors.append(f"{label} 的 {key}={value!r} 非法")
        if not str(entry.get("material_id") or "").strip():
            errors.append(f"{label} 缺少 material_id")
        safe = _safe_relative_path(entry.get("file_path"))
        if safe is None:
            errors.append(f"{label} 的 file_path 不安全：{entry.get('file_path')!r}")
            continue
        if safe in seen:
            errors.append(f"素材目录存在重复实体路径 {safe}")
        seen.add(safe)
        files.append(safe)
        url = str(entry.get("source_url") or "")
        if is_signed_download_url(url):
            errors.append(f"{label} 的 source_url 为签名下载 URL，禁止写入：{url}")
        entity = delivery_root / safe
        if not entity.is_file():
            errors.append(f"{label} 的实体文件不存在：{safe}")
            continue
        try:
            actual = entity.stat().st_size
        except OSError as exc:
            errors.append(f"{label} 无法读取实体文件 {safe}：{exc}")
            continue
        recorded = entry.get("file_bytes")
        if not isinstance(recorded, (int, float)) or int(recorded) != int(actual):
            errors.append(f"{label} 的 file_bytes 与实体不符：记录 {recorded} / 实际 {actual}（{safe}）")
        total += int(actual)
    declared = payload.get("total_bytes")
    if isinstance(declared, (int, float)) and int(declared) != total:
        errors.append(f"素材目录 total_bytes 与实体合计不符：记录 {int(declared)} / 实际 {total}")
    if total > int(max_total_bytes):
        errors.append(f"素材目录实体合计 {total} 字节超过上限 {int(max_total_bytes)} 字节")
    if expected_files is not None:
        expected = {str(item) for item in expected_files}
        listed = set(files)
        for item in sorted(expected - listed):
            errors.append(f"清单中的实体未列入素材目录：{item}")
        for item in sorted(listed - expected):
            errors.append(f"素材目录列出未交付实体：{item}")
    return {
        "status": "pass" if not errors else "fail",
        "path": str(target.resolve()),
        "errors": errors,
        "count": len(raw_entries),
        "total_bytes": total,
        "files": files,
    }


def build_manifest(
    *,
    theme: str,
    folder: str,
    business_date: str,
    generated_at: str,
    keywords_used: list[str],
    candidate_pool_size: int,
    script_replica: dict[str, Any],
    material_replica_sources: list[dict[str, Any]],
    main_materials: list[dict[str, Any]],
    supporting_materials: list[dict[str, Any]],
    counters: dict[str, Any],
    face_backend: str,
    face_backend_status: str,
    ffmpeg_status: str,
    degraded: bool,
    insufficient: bool,
    warnings: list[str] | None = None,
    keywords_requested: list[str] | None = None,
    search_attribution: dict[str, Any] | None = None,
    prefilter: dict[str, Any] | None = None,
    download_budget: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
    face_truncated_samples: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble the top-level delivery manifest.

    ``keywords_used`` are the keywords the crawler *actually searched* (the
    ``keywords[:budget // 10]`` list).  ``keywords_requested`` is the full
    ``expand_keywords`` expansion, kept so the manifest can never claim coverage
    it did not have.  ``search_attribution`` carries the counts / budgets that
    let a reader tell "too few keywords" apart from "too little content".

    ``prefilter`` / ``download_budget`` / ``validation`` carry the three
    download-cost-control layers.  Each is only included when that layer was
    actually active, so a manifest produced with all three absent/disabled is
    **behaviourally equivalent** to a pre-change one: the diff is additive only
    (new keys/records, never a changed or removed existing value) and the three
    layers' own artifact surfaces do not exist.
    """
    used = list(keywords_used)
    requested = list(keywords_requested) if keywords_requested is not None else list(used)
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "theme": theme,
        "folder": folder,
        "business_date": business_date,
        "generated_at": generated_at,
        "keywords_used": used,
        "keywords_requested": requested,
        "keywords_truncated": len(requested) > len(used),
        "search_attribution": dict(search_attribution or {}),
        "candidate_pool_size": int(candidate_pool_size),
        "script_replica": dict(script_replica),
        "material_replica_sources": list(material_replica_sources),
        "main_materials": list(main_materials),
        "supporting_materials": list(supporting_materials),
        "counters": dict(counters),
        "face_backend": face_backend,
        "face_backend_status": face_backend_status,
        "ffmpeg_status": ffmpeg_status,
        "degraded": bool(degraded),
        "insufficient": bool(insufficient),
        "warnings": list(warnings or []),
        "evidence_disclaimer": DISCLAIMER,
    }
    if prefilter is not None:
        manifest["prefilter"] = dict(prefilter)
    if download_budget is not None:
        manifest["download_budget"] = dict(download_budget)
    if validation is not None:
        manifest["validation"] = dict(validation)
    if face_truncated_samples:
        # Only present when at least one face sample was short, so a delivery
        # with complete samples renders exactly as before.
        manifest["face_truncated_samples"] = [dict(item) for item in face_truncated_samples]
    return manifest


def validate_delivery_manifest(path: Path) -> dict[str, Any]:
    """Self-contained manifest check (no network, no external tools).

    Face is **not** a delivery gate any more (2026-09-18 strategy §3.3): a
    ``face_heavy`` clip is legal material and ``face_class`` is descriptive only,
    so the old "face_heavy error" / "main must be face_free" rules are gone.  In
    their place the delivery must satisfy: a material catalog
    (``00-素材目录.json``) that lists exactly the delivered main/support entities,
    and a whole-folder byte total within :data:`MAX_DELIVERY_FOLDER_BYTES`.

    A current pipeline delivery is required to carry the catalog.  The only
    compatibility exception is an explicitly marked legacy/annotation manifest:
    it has no ``delivery_folder`` block, because it was not produced by the
    current publisher and therefore cannot satisfy the current physical-size
    contract either.  This keeps old research-pack annotations readable without
    allowing a newly published material delivery to silently omit its downstream
    selection catalog.
    """
    target = Path(path)
    errors: list[str] = []
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "fail", "path": str(target), "errors": [f"清单无法读取：{exc}"]}
    for key in REQUIRED_MANIFEST_KEYS:
        if key not in payload:
            errors.append(f"缺少字段 {key}")
    if not str(payload.get("evidence_disclaimer") or "").strip():
        errors.append("缺少 evidence_disclaimer")
    recorded_total = (payload.get("delivery_folder") or {}).get("delivery_folder_bytes")
    if isinstance(recorded_total, (int, float)) and recorded_total > MAX_DELIVERY_FOLDER_BYTES:
        errors.append(
            f"交付目录合计 {int(recorded_total)} 字节超过上限 {MAX_DELIVERY_FOLDER_BYTES} 字节"
        )
    # Same derivation as the catalog builder uses, so a path with a backslash or a
    # ``./`` prefix compares equal on both sides instead of failing spuriously.
    expected_files = []
    for item in [*(payload.get("main_materials") or []), *(payload.get("supporting_materials") or [])]:
        raw = str(item.get("file") or "")
        if raw:
            expected_files.append(_safe_relative_path(raw) or raw)
    catalog_file = target.parent / CATALOG_NAME
    if catalog_file.is_file():
        catalog = validate_material_catalog(catalog_file, expected_files=expected_files)
        errors.extend(catalog["errors"])
    elif "delivery_folder" in payload:
        errors.append(f"当前交付缺少 {CATALOG_NAME}")
    return {"status": "pass" if not errors else "fail", "path": str(target.resolve()), "errors": errors}


# ``os.replace`` (i.e. Windows ``MoveFileEx``) fails with ERROR_ACCESS_DENIED
# (``WinError 5`` -> ``PermissionError``) when antivirus / the Search indexer /
# Explorer preview / an editor file watcher momentarily holds a directory
# handle.  That is a *transient* condition on Windows, so retry with exponential
# backoff before surfacing an actionable error.
_REPLACE_RETRY_ATTEMPTS = 6
_REPLACE_BACKOFF_SECONDS = (0.25, 0.5, 1.0, 2.0, 3.0, 3.0)


def _replace_with_retry(
    src: Path,
    dst: Path,
    *,
    attempts: int = _REPLACE_RETRY_ATTEMPTS,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """``os.replace`` with exponential backoff on transient Windows locks.

    Only ``PermissionError`` (or an ``OSError`` carrying ``winerror == 5``) is
    retried; any other error is re-raised immediately so genuine problems are
    never masked by the backoff loop.
    """
    waiter = sleep or time.sleep
    total = max(1, int(attempts))
    last_error: OSError | None = None
    for index in range(total):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            if not (isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 5):
                raise
            last_error = exc
            if index < total - 1:
                waiter(_REPLACE_BACKOFF_SECONDS[min(index, len(_REPLACE_BACKOFF_SECONDS) - 1)])
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"os.replace 重试耗尽：{src} -> {dst}")


def publish_directory(stage: Path, destination: Path, *, sleep: Callable[[float], None] | None = None) -> None:
    """Atomically publish ``stage`` as ``destination`` without half-overwriting.

    Transient ``WinError 5`` locks are retried with exponential backoff.  On a
    *permanent* failure the invariants are:

    * ``stage`` is left completely untouched, so the caller can retry the
      publish for free without re-crawling;
    * ``destination`` is either the old complete version or the new complete
      version -- never a half-written one.

    ``sleep`` is injectable purely for tests (defaults to ``time.sleep``); the
    existing two-argument call sites keep working unchanged.
    """
    stage = Path(stage)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.with_name(f".{destination.name}.backup")
    remove_tree(backup)
    if destination.exists():
        try:
            _replace_with_retry(destination, backup, sleep=sleep)
        except OSError as exc:
            raise RuntimeError(
                f"发布失败：无法移动现有交付目录 {destination} -> {backup}"
                "（交付目录被占用，可能是杀软/资源管理器/编辑器正在监视），请稍后重试；"
                f"产物仍保留在 {stage}（{exc}）"
            ) from exc
    try:
        _replace_with_retry(stage, destination, sleep=sleep)
    except OSError as exc:
        recovered = False
        if backup.exists() and not destination.exists():
            try:
                _replace_with_retry(backup, destination, sleep=sleep)
                recovered = True
            except OSError as rollback_exc:
                raise RuntimeError(
                    f"发布失败且回滚失败：目标 {destination} 被占用，旧版本仍保留在备份 {backup}"
                    f"（请手工恢复）；产物仍保留在 {stage}（{exc}）；回滚错误：{rollback_exc}"
                ) from exc
        suffix = "；已回滚到旧版本" if recovered else ""
        raise RuntimeError(
            f"发布失败：无法将产物目录发布到 {destination}"
            "（交付目录被占用，可能是杀软/资源管理器/编辑器正在监视），请稍后重试；"
            f"产物仍保留在 {stage}{suffix}（{exc}）"
        ) from exc
    if backup.exists():
        remove_tree(backup)


def keyword_coverage_line(manifest: dict[str, Any]) -> str:
    """``请求 N 个 / 实际搜索 M 个（发生关键词截断）`` summary line."""
    requested = list(manifest.get("keywords_requested") or manifest.get("keywords_used") or [])
    used = list(manifest.get("keywords_used") or [])
    truncated = bool(manifest.get("keywords_truncated")) or len(requested) > len(used)
    suffix = f"（发生关键词截断，仅搜索前 {len(used)} 个）" if truncated else ""
    return f"- 关键词覆盖：请求 {len(requested)} 个 / 实际搜索 {len(used)} 个{suffix}"


def candidate_pool_line(manifest: dict[str, Any]) -> str:
    """``候选池规模：N（最小目标 M，达标/低于目标）`` summary line."""
    size = int(manifest.get("candidate_pool_size") or 0)
    attribution = manifest.get("search_attribution") or {}
    min_pool = attribution.get("min_pool_size")
    if min_pool:
        verdict = "达标" if size >= int(min_pool) else "低于目标"
        return f"- 候选池规模：{size}（最小目标 {int(min_pool)}，{verdict}）"
    return f"- 候选池规模：{size}"


def search_report_lines(manifest: dict[str, Any]) -> list[str]:
    """``搜索报告：<交付目录内相对路径>`` line, when attribution is present."""
    path = (manifest.get("search_attribution") or {}).get("search_report_path")
    return [f"- 搜索报告：{path}"] if path else []


def human_size(num_bytes: int | float) -> str:
    """Human-readable byte size, e.g. ``1.5 KB`` / ``12.3 MB``."""
    size = float(num_bytes or 0)
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{int(size)} B" if index == 0 else f"{size:.1f} {units[index]}"


#: Any run of whitespace (newline, carriage return, tab, one-or-more spaces).
_WHITESPACE_RE = re.compile(r"\s+")


def _clip_title(text: Any, limit: int = 30) -> str:
    """One clean line for a source title: collapse whitespace, *then* truncate.

    Source titles are free-form and routinely contain embedded newlines -- the
    real 9.13 delivery had ``"…帮助到大家\\n如果记不住的话…"``.  Slicing such a
    string at a fixed width can end the slice *on* the newline, so the rendered
    row breaks in two and leaves a bare ``…`` line that severs the Markdown
    list (evidence: ``00-交付说明.md`` had exactly such an orphan ``…`` line).
    Folding every whitespace run (``\\n`` / ``\\r`` / ``\\t`` / spaces) into a
    single space *before* truncating guarantees the result is one line, so a
    list row can never be split.  Used by every readme row that shows a source
    title: the download list, the prefilter rejections and the budget selection.
    """
    collapsed = _WHITESPACE_RE.sub(" ", str(text or "")).strip()
    if len(collapsed) > limit:
        collapsed = f"{collapsed[:limit]}…"
    return collapsed


def download_lines(manifest: dict[str, Any]) -> list[str]:
    """``## 下载清单`` (and ``## 下载失败``) section for a download-only delivery.

    Returns an empty list when the manifest carries no download information, so
    ordinary (non download-only) deliveries render exactly as before.
    """
    downloads = manifest.get("downloads")
    failures = manifest.get("download_failures")
    if not downloads and not failures:
        return []
    lines: list[str] = ["## 下载清单", ""]
    for item in downloads or []:
        title = _clip_title(item.get("title"))
        lines.append(
            f"- {item.get('video_id')}｜{item.get('author')}｜{title}｜"
            f"{item.get('duration_seconds') or 0}s｜{human_size(item.get('size_bytes') or 0)}｜{item.get('file')}"
        )
    if not downloads:
        lines.append("- 无")
    if failures:
        lines.extend(["", "## 下载失败", ""])
        for item in failures:
            lines.append(f"- {item.get('video_id')}｜{item.get('stage')}｜{item.get('reason')}")
    return lines


#: Chinese label for each pre-download rejection stage, so the readme can show a
#: per-stage breakdown ("图文 9 条 / 时长 3 条") instead of only a flat list.
_PREFILTER_STAGE_LABEL = {
    "pre_exclude": "排除词",
    "pre_media_type": "图文无视频流",
    "pre_duration": "时长",
    "pre_heat": "热度",
}


def _prefilter_stage_breakdown(rejections: list[dict[str, Any]]) -> str:
    """``图文无视频流 9 条 / 时长 3 条`` -- only the stages that actually fired."""
    counts: dict[str, int] = {}
    for item in rejections:
        stage = str(item.get("stage") or "")
        counts[stage] = counts.get(stage, 0) + 1
    parts = [
        f"{_PREFILTER_STAGE_LABEL.get(stage, stage)} {count} 条"
        for stage, count in counts.items()
    ]
    return " / ".join(parts)


def prefilter_lines(manifest: dict[str, Any]) -> list[str]:
    """``## 下载前预筛`` section: what the gate kept and what it dropped.

    Returns an empty list when the manifest carries no ``prefilter`` block, so
    a delivery produced with the gate absent/disabled renders exactly as before.
    Every dropped candidate is listed with its stage and reason so the operator
    can audit the cut without opening ``05-过程数据/prefilter.json``.  A
    per-stage breakdown plus a dedicated image-album count make "how many were
    dropped and why" answerable at a glance.
    """
    block = manifest.get("prefilter")
    if not block:
        return []
    config = block.get("config") or {}
    exclude_terms = config.get("exclude_terms") or []
    exclude_text = "、".join(str(term) for term in exclude_terms) if exclude_terms else "无"
    # ``enabled`` is the duration/heat switch; ``exclude_only`` means the run went
    # through the exclude gate with those switches off.  The status line must say
    # which gates actually ran, so the readme can never show the gate as "off"
    # while an exclude drop silently happened.
    if block.get("enabled"):
        gate_line = (
            f"- 预筛配置：时长 {config.get('min_seconds')}~{config.get('max_seconds')}s，"
            f"热度分位 {config.get('heat_gate_percentile')}，"
            f"未知时长放行 {config.get('allow_unknown_duration')}，"
            f"图文剔除 {config.get('drop_non_video')}"
        )
    elif block.get("exclude_only"):
        gate_line = "- 预筛状态：仅排除词闸门生效（时长/热度闸门未启用，prefilter.enabled=false）"
    else:
        gate_line = "- 预筛状态：未启用任何闸门"
    rejections = block.get("rejections") or []
    lines = [
        "## 下载前预筛",
        "",
        gate_line,
        f"- 排除词：{exclude_text}",
        f"- 采集候选 {block.get('pool_size', 0)} 条，通过 {block.get('passed', 0)} 条，"
        f"剔除 {block.get('rejected', 0)} 条",
    ]
    non_video = sum(1 for item in rejections if item.get("stage") == "pre_media_type")
    if non_video:
        lines.append(f"- 其中图文帖（无视频流）{non_video} 条（未占用下载名额）")
    breakdown = _prefilter_stage_breakdown(rejections)
    if breakdown:
        lines.append(f"- 剔除分布：{breakdown}")
    # Honest degradation: when the metadata-duration patch is absent the window
    # runs only after download; state that here rather than let the window look
    # like a silent no-op.
    note = block.get("metadata_duration_note")
    if note:
        lines.append(f"- 时长来源：{note}")
    if rejections:
        lines.append("")
        for item in rejections:
            title = _clip_title(item.get("title"))
            lines.append(
                f"- 剔除 {item.get('video_id')}｜{item.get('stage')}｜{item.get('reason')}"
                f"｜时长 {item.get('duration_seconds')}s｜热度 {item.get('heat_score')}"
                f"｜{item.get('author')}｜{title}"
            )
    return lines


#: Human-readable gloss for every ``stopped_by`` value, so the readme can never
#: show a bare machine token the reader would have to guess at.
_STOP_REASON_TEXT = {
    "count": "达到条数上限",
    "bytes": "达到字节上限（按交付字节计）",
    "transferred_bytes": "达到真实传输字节上限（含已下载但未交付的文件）",
    "queue_exhausted": "候选已穷尽（未触及上限）",
}


def _stop_reason_text(value: Any) -> str:
    """Render ``stopped_by`` as ``中文（raw）`` so nothing is lost or guessed."""
    if value is None:
        return "未触发（候选已穷尽）"
    text = _STOP_REASON_TEXT.get(str(value))
    return f"{text}（{value}）" if text else str(value)


#: Chinese label for each selection stage that can tag an ``入选`` row.
_STAGE_LABEL = {"script": "脚本", "material": "素材"}


def _selected_stages(item: dict[str, Any]) -> list[str]:
    """Stage tag(s) carried by one ``selected`` row (P1's ``stages``/``stage``)."""
    stages = [str(stage) for stage in (item.get("stages") or []) if stage]
    if not stages and item.get("stage"):
        stages = [str(item["stage"])]
    return stages


def _stage_tag(item: dict[str, Any]) -> str:
    """``［脚本］`` / ``［脚本］［素材］`` for a tagged row, ``""`` when untagged."""
    stages = _selected_stages(item)
    if not stages:
        return ""
    return "".join(f"［{_STAGE_LABEL.get(stage, stage)}］" for stage in stages)


def _dedupe_selected_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the 「入选」 rows to one per ``video_id`` (P6).

    Independent of :meth:`DownloadBudget.select`: even a manifest built by an
    older build (or any caller that appended the same video twice) renders one
    row per video, so the listed count always equals the number of *distinct*
    delivered videos.  The first occurrence keeps its position and values; later
    duplicates only contribute their stage tag (``stages``).
    """
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for index, row in enumerate(rows):
        video_id = str(row.get("video_id") or "")
        key = video_id or f"__row_{index}"
        if key not in merged:
            copy = dict(row)
            copy["stages"] = _selected_stages(row)
            merged[key] = copy
            order.append(key)
            continue
        existing = merged[key]
        for stage in _selected_stages(row):
            if stage not in existing["stages"]:
                existing["stages"].append(stage)
    return [merged[key] for key in order]


def download_budget_lines(manifest: dict[str, Any]) -> list[str]:
    """``## 下载预算`` section: how the pre-download budget was spent, and why.

    Returns an empty list when the manifest carries no ``download_budget``
    block, so a delivery produced with the budget absent/disabled renders exactly
    as before.  ``selected`` explains "why these few are worth downloading";
    ``skipped`` lists what the budget rejected and the reason.  Both byte
    ledgers are shown -- 交付 (``delivered_bytes``) and 真实传输
    (``transferred_bytes``) -- because a download-only run can spend real
    bandwidth on files it never delivers.
    """
    block = manifest.get("download_budget")
    if not block:
        return []
    limits = block.get("limits") or {}
    used = block.get("used") or {}
    ranking = block.get("ranking") or {}
    max_count = limits.get("max_count") or 0
    max_bytes = limits.get("max_bytes") or 0
    max_item = limits.get("max_item_bytes") or 0
    delivered = used.get("delivered_bytes", used.get("bytes", 0))
    transferred = used.get("transferred_bytes", delivered)
    lines = [
        "## 下载预算",
        "",
        f"- 预算上限：条数 {f'{max_count} 条' if max_count else '不限'} / "
        f"总量 {human_size(max_bytes) if max_bytes else '不限'} / "
        f"单条 {human_size(max_item) if max_item else '不限'}",
        f"- 实际使用：{used.get('count', 0)} 条 / "
        f"交付 {human_size(delivered)}（下载文件账）/ 真实传输 {human_size(transferred)}",
        f"- 停止原因：{_stop_reason_text(block.get('stopped_by'))}",
        f"- 排序依据：{' → '.join(ranking.get('order') or [])}",
    ]
    if ranking.get("note"):
        lines.append(f"- 排序限制：{ranking['note']}")
    # One row per distinct video: the same source selected in two stages (脚本
    # then 素材) must not appear -- or be counted -- twice.
    selected = _dedupe_selected_rows(block.get("selected") or [])
    # Explicit scope word: this list is the *download budget's* selection, which
    # is a different set from the *material* sources that make it into 04-原片.
    # Calling both 「入选」 made 7 (budget) and 4 (kept sources) read as a
    # contradiction in the same document.
    lines.extend(["", f"下载预算入选（{len(selected)} 条，为什么这几条值得下）：", ""])
    for item in selected:
        title = _clip_title(item.get("title"))
        lines.append(
            f"- {item.get('video_id')}{_stage_tag(item)}｜相关度 {item.get('relevance_score')}｜热度 {item.get('heat_score')}"
            f"｜{human_size(item.get('size_bytes') or 0)}｜{item.get('author')}｜{title}"
        )
    if not selected:
        lines.append("- 无")
    skipped = block.get("skipped") or []
    if skipped:
        lines.extend(["", "跳过：", ""])
        for item in skipped:
            lines.append(f"- {item.get('video_id')}｜{item.get('stage')}｜{item.get('reason')}")
    return lines


def validation_lines(manifest: dict[str, Any]) -> list[str]:
    """``## 下载校验`` section: what was validated and what was rejected.

    Returns an empty list when the manifest carries no ``validation`` block, so
    a delivery produced with the layer absent/disabled renders exactly as
    before.  The counts here are the *same* numbers as ``validation.json``,
    ``manifest.counters.validation_*`` and ``manifest.validation.counts`` -- one
    arithmetic source, four surfaces.

    A ``degraded`` / ``unknown_coverage`` pass is reported **separately** from a
    full-decode pass, so "全片解码通过" is never inflated by a sampled fallback
    that only proved the file is *probably* fine.
    """
    block = manifest.get("validation")
    if not block:
        return []
    config = block.get("config") or {}
    counts = block.get("counts") or {}
    by_conclusion = counts.get("by_conclusion") or {}
    by_stage = block.get("by_stage") or counts.get("by_stage") or {}
    stage_labels = {"script": "脚本", "material": "素材"}
    # Deterministic order (script before material), then any unexpected stage.
    stage_order = [name for name in ("script", "material") if name in by_stage]
    stage_order += [name for name in by_stage if name not in stage_order]
    validated = int(counts.get("validated", 0))
    # ``validated`` counts *calls*, not files: a source validated in both the
    # script and the material stage is counted twice (9 calls vs 8 delivered
    # mp4 in the 9.13 run).  Say 「次」 and break it down so it reconciles with
    # the per-stage line instead of reading as a file count.
    stage_parts = [
        f"{stage_labels.get(name, name)} {int((by_stage.get(name) or {}).get('validated', 0))}"
        for name in stage_order
    ]
    stage_suffix = f"（{' + '.join(stage_parts)}）" if stage_parts else ""
    tolerance = float(config.get("duration_tolerance") or 0) * 100
    full_pass = int(by_conclusion.get("ok", 0))
    degraded_pass = int(by_conclusion.get("degraded", 0))
    unknown_pass = int(by_conclusion.get("unknown_coverage", 0))
    lines = [
        "## 下载校验",
        "",
        f"- 校验配置：时长容差 ±{tolerance:.0f}%，全片解码 {config.get('full_decode')}，"
        f"解码时间预算 {config.get('decode_time_budget_seconds')}s（0 = 跳过全片解码、仅抽帧校验），"
        f"缺失时长需比对 {config.get('require_metadata_duration')}，"
        f"缓存旁证 {config.get('cache_attestation')}",
        f"- 校验 {validated} 次{stage_suffix}：全片解码通过 {full_pass} 条 / "
        f"降级抽帧通过 {degraded_pass} 条 / 覆盖未测通过 {unknown_pass} 条，"
        f"剔除 {counts.get('rejected', 0)} 条",
        f"- 缓存旁证：命中 {counts.get('cache_attested', 0)} 条"
        f"（其中命中坏件旁证 {counts.get('cache_attested_bad', 0)} 条）",
    ]
    if by_stage:
        parts = [
            f"{stage_labels.get(name, name)} 校验 {value.get('validated', 0)} 次"
            f"（通过 {value.get('passed', 0)} / 剔除 {value.get('rejected', 0)}）"
            for name, value in by_stage.items()
        ]
        lines.append("- 分阶段：" + "；".join(parts))
    breakdown = "；".join(f"{name} {value}" for name, value in by_conclusion.items() if value)
    if breakdown:
        lines.append(f"- 结论分布：{breakdown}")
    if not counts.get("duration_checked"):
        # Where the post-download duration window's outcome lives depends on the
        # run.  The per-file detail lives under 「下载失败」, but ``download_lines``
        # renders that section *only* when ``download_failures`` is non-empty -- so
        # this pointer must key off the *same* condition, not ``downloads or
        # download_failures``.  Otherwise a download-only run with successes but no
        # failures emits a cross-reference to a section that does not exist (a
        # dangling pointer; regression-tested by
        # ``test_download_only_without_failures_does_not_point_at_absent_section``).
        # With no failures section there is nothing per-file to point at, so state
        # the count inline and point at the always-written validation artifact.
        if manifest.get("download_failures"):
            window_pointer = "下载后按实测时长执行的时长窗口结果见「下载失败」"
        else:
            window_rejected = int(
                (manifest.get("counters") or {}).get("validation_duration_window_rejected") or 0
            )
            window_pointer = (
                f"下载后按实测时长执行的时长窗口剔除 {window_rejected} 条"
                f"（逐条明细见 05-过程数据/validation.json）"
            )
        lines.append(
            "- 时长比对已跳过（元数据无时长）：本轮元数据未携带时长，±容差比对未执行；"
            + window_pointer
        )
    lines.append("- 逐条明细：05-过程数据/validation.json")
    return lines


def source_retention_lines(manifest: dict[str, Any]) -> list[str]:
    """``## 原片保留`` section: the ``04-原片`` retention rule, stated explicitly.

    Returns an empty list when the manifest carries no ``source_retention``
    block (older deliveries), so the readme is unchanged there.  ``04-原片``
    holds one copy of every *selected* source and **not** the non-selected
    downloads (which stay in the persistent media store); making that explicit
    stops the rule from being misread as data loss.

    Beyond the configured ``keep_source_video`` switch it prints
    ``effective_keep`` -- whether a *video* actually landed in ``04-原片`` this
    run.  The two differ in the opt-in whole-file mode (``direct_delivery``):
    each source is shipped as a whole file into 02/03 and the ``04-原片`` copy is
    replaced by an index, so the switch reads ``True`` while nothing is copied.
    Printing only the configured switch there would misread as a second copy (or
    as silent data loss).
    """
    block = manifest.get("source_retention")
    if not block:
        return []
    keep = bool(block.get("keep_source_video", True))
    effective = bool(block.get("effective_keep", keep))
    lines = [
        "## 原片保留",
        "",
        f"- retention.keep_source_video：{keep}",
        f"- 实际收录原片视频：{effective}",
        f"- 04-原片 收录 {block.get('kept_count', 0)} 份；"
        f"持久化媒体库：{block.get('persistent_store')}",
        f"- 说明：{block.get('note') or '04-原片 仅收录最终选用源片'}",
    ]
    reason = block.get("reason")
    if reason:
        lines.append(f"- 原因：{reason}")
    return lines


def delivery_folder_lines(manifest: dict[str, Any]) -> list[str]:
    """``## 交付体积`` section: the *whole* delivery folder against the ceiling.

    Returns an empty list when the manifest carries no ``delivery_folder`` block
    (older deliveries), so the readme is unchanged there.  This is the number
    that answers the "200,000,000 bytes" requirement: it counts **every** file in
    the delivery directory (00-交付说明.md / 00-素材目录.json / 01 / 02 / 03 / 04 /
    05 / 清单.json), not a single sub-folder, so it can never appear healthy while
    the folder as a whole is over the limit.
    """
    block = manifest.get("delivery_folder")
    if not block:
        return []
    total = block.get("delivery_folder_bytes")
    if not isinstance(total, (int, float)):
        return []
    ceiling = block.get("max_delivery_folder_bytes")
    text = f"- 交付目录合计：{human_size(total)}"
    if isinstance(ceiling, (int, float)) and ceiling > 0:
        text += f"（上限 {human_size(ceiling)}）"
        text += "；已超限" if total > ceiling else "；未超限"
    return ["## 交付体积", "", text]


def visual_proxy_lines(manifest: dict[str, Any]) -> list[str]:
    """``## 画面代理判据`` section: what the visual gate now *actually* does (P2).

    Only rendered when the material stage ran (a full-chain delivery); a
    download-only delivery never evaluates the visual proxy, so it gets no
    section.  The point is honesty about a deliberate semantics change: the
    motion branch used to be dead code (a ratio compared against a per-pair
    delta), so the gate effectively rejected on OCR text coverage alone.  Now
    the motion branch is live and almost every real clip passes it, which means
    the gate in practice only drops **near-static** clips -- it no longer drops
    text-heavy ones.  Spelled out so nobody reads it as a general quality filter.
    """
    material = manifest.get("material_replica") or {}
    if material.get("status") != "done":
        return []
    from .replication_selection import (
        DEFAULT_MAX_OCR_COVERAGE,
        DEFAULT_MIN_MOTION_FRAME_RATIO,
        DEFAULT_MOTION_DELTA_THRESHOLD,
    )

    return [
        "## 画面代理判据",
        "",
        f"- 判据：视觉合格 = 运动达标 **或** 文字未超限。运动达标 = 变化帧对占比 ≥ "
        f"{DEFAULT_MIN_MOTION_FRAME_RATIO:g}（单对帧灰度变化量 ≥ {DEFAULT_MOTION_DELTA_THRESHOLD:g}）；"
        f"文字未超限 = 有字帧占比 ≤ {DEFAULT_MAX_OCR_COVERAGE:g}。阈值可在 "
        f"jobs.material_replication.material_replica 调整。",
        "- 语义说明（重要，勿误读为质量把关）：运动分支此前因量纲错误恒为假（死代码），"
        "画面剔除实际只由「文字覆盖」决定；修复后运动分支生效，且绝大多数真实视频都能通过它。"
        "因此本闸门现在**实际只拦「近静止」片**（长时间几乎无变化），"
        "**不再拦「文字覆盖重」的片**——文字多的片子会被运动分支放行。",
        "- 逐条剔除原因（运动不足 / 文字过多 / OCR 不可测）见 清单.json 的 "
        "material_replica.rejected。",
    ]


def face_truncation_lines(manifest: dict[str, Any]) -> list[str]:
    """``## 人脸样本截断`` section listing every source with a short sample.

    Returns an empty list when no source was truncated, so ordinary deliveries
    render exactly as before.  A truncated sample means the face verdict was
    computed over fewer frames than the clip implies, so its ``face_class`` must
    be read with caution: a *severe* shortfall is already downgraded to
    ``unavailable`` (and the video, if it was rejected, is listed here too).
    """
    samples = manifest.get("face_truncated_samples")
    if not samples:
        # Back-compat: fall back to the delivered sources when a manifest was
        # built without the run-level list.
        samples = [item for item in (manifest.get("material_replica_sources") or []) if item.get("truncated")]
    if not samples:
        return []
    lines = [
        "## 人脸样本截断",
        "",
        "- 以下素材的人脸采样帧数少于预期，样本未覆盖整片，人脸分级仅供参考：",
        "",
    ]
    for item in samples:
        coverage = item.get("sample_coverage")
        coverage_text = f"{float(coverage) * 100:.0f}%" if isinstance(coverage, (int, float)) else "未知"
        confidence = "（低置信）" if item.get("low_confidence") else ""
        verdict = "未交付" if item.get("delivered") is False else "已交付"
        lines.append(
            f"- {item.get('video_id')}｜预期 {item.get('expected_frames')} 帧 / "
            f"实际 {item.get('emitted_frames')} 帧｜覆盖率 {coverage_text}｜"
            f"分级 {item.get('face_class')}{confidence}｜{verdict}"
        )
    return lines


def _delivered_source_bytes_line(manifest: dict[str, Any]) -> list[str]:
    """``- 交付源片体积：…`` line for the ``## 主素材`` section.

    Reports the summed size of the *selected* material source files (the
    ``material_replica.delivered_bytes`` the selection chain accumulated) plus
    the configured floor/ceiling, so a reader can tell "shipped 24 MiB against a
    70 MiB floor" at a glance.  Tolerant: a manifest without the field (older
    run, or the byte quota unset at 0) renders nothing, exactly as before.
    """
    material = manifest.get("material_replica") or {}
    delivered = material.get("delivered_bytes")
    if not isinstance(delivered, (int, float)) or delivered <= 0:
        return []
    floor = material.get("min_delivered_bytes")
    ceiling = material.get("max_delivered_bytes")
    bounds: list[str] = []
    if isinstance(floor, (int, float)) and floor > 0:
        bounds.append(f"下限 {human_size(floor)}")
    if isinstance(ceiling, (int, float)) and ceiling > 0:
        bounds.append(f"上限 {human_size(ceiling)}")
    bound_text = (
        f"（{' / '.join(bounds)}，仅计入选源片；8s 切片另占交付目录）"
        if bounds
        else "（仅计入选源片；8s 切片另占交付目录）"
    )
    return [f"- 交付源片体积：{human_size(delivered)}{bound_text}", ""]


def render_delivery_readme(manifest: dict[str, Any]) -> str:
    """Human-readable delivery summary (00-交付说明.md)."""
    lines = [
        f"# {manifest['folder']} 交付说明",
        "",
        f"> {manifest['evidence_disclaimer']}",
        "",
        f"- 主题：{manifest['theme']}",
        f"- 业务日期：{manifest['business_date']}",
        f"- 生成时间：{manifest['generated_at']}",
        f"- 搜索关键词（实际）：{'、'.join(manifest['keywords_used']) or '无'}",
        f"- 请求关键词（展开）：{'、'.join(manifest.get('keywords_requested') or manifest['keywords_used']) or '无'}",
        keyword_coverage_line(manifest),
        candidate_pool_line(manifest),
        *search_report_lines(manifest),
        f"- 人脸后端：{manifest['face_backend']}（{manifest['face_backend_status']}）",
        f"- ffmpeg：{manifest['ffmpeg_status']}",
        *(["- 模式：仅采集与下载（未做人脸筛选/切片/脚本复刻）"] if manifest.get("mode") == "download_only" else []),
        f"- 降级：{manifest['degraded']}；素材不足：{manifest['insufficient']}",
        "",
    ]
    prefilter_section = prefilter_lines(manifest)
    if prefilter_section:
        lines.extend([*prefilter_section, ""])
    budget_section = download_budget_lines(manifest)
    if budget_section:
        lines.extend([*budget_section, ""])
    validation_section = validation_lines(manifest)
    if validation_section:
        lines.extend([*validation_section, ""])
    visual_section = visual_proxy_lines(manifest)
    if visual_section:
        lines.extend([*visual_section, ""])
    lines.extend([
        "## 脚本复刻视频",
        "",
    ])
    script = manifest.get("script_replica") or {}
    if script.get("status") == "found":
        lines.append(f"- 视频：{script.get('video_id')}（{script.get('author')}），热度 {script.get('heat_score')}")
        lines.append(f"- 脚本骨架：{script.get('skeleton')}")
        lines.append(f"- 脚本思路：{script.get('script_notes')}")
    else:
        lines.append(f"- 状态：{script.get('status', 'unknown')}（未找到满足条件的脚本复刻视频）")
    lines.extend(["", "## 主素材", ""])
    lines.extend(_delivered_source_bytes_line(manifest))
    for clip in manifest.get("main_materials") or []:
        lines.append(f"- {clip.get('file')}（{clip.get('duration')}s，{clip.get('face_class')}）→ {clip.get('suggested_use')}")
    if not manifest.get("main_materials"):
        lines.append("- 无")
    lines.extend(["", "## 辅助素材", ""])
    for clip in manifest.get("supporting_materials") or []:
        lines.append(f"- {clip.get('file')}（{clip.get('duration')}s，{clip.get('face_class')}）→ {clip.get('suggested_use')}")
    if not manifest.get("supporting_materials"):
        lines.append("- 无")
    source_section = source_retention_lines(manifest)
    if source_section:
        lines.extend(["", *source_section])
    delivery_section = delivery_folder_lines(manifest)
    if delivery_section:
        lines.extend(["", *delivery_section])
    face_section = face_truncation_lines(manifest)
    if face_section:
        lines.extend(["", *face_section])
    downloads_section = download_lines(manifest)
    if downloads_section:
        lines.extend(["", *downloads_section])
    if manifest.get("warnings"):
        lines.extend(["", "## 警告", ""])
        lines.extend(f"- {warning}" for warning in manifest["warnings"])
    lines.append("")
    return "\n".join(lines)
