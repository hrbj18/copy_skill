"""Independent, immutable, self-verifying ``episode-research-pack-v1``.

This module is the **producer** side of the cross-project contract between
CopySkill and Haike.  It is deliberately additive: the historic
``material-replication`` delivery (``清单.json``, ``MM.DD<主题>复刻视频/``) keeps
its exact semantics, and this publisher **never writes into that delivery**.
After such a delivery finishes, a single-topic episode may *additionally*
publish a research pack a read-only consumer can trust without crawling,
guessing or re-deriving anything.

Authoritative layout (frozen)::

    output/每期研究包/<YYYY-MM-DD>_研究包/<episode_id>/
        .staging/<pack_id>/        # in-flight stage (same episode root / volume)
        packs/<pack_id>/           # immutable published pack
        current.json               # the only commit point
        latest.json                # best-effort, written after the current flip
        notify/<episode_id>.json   # best-effort, written after the current flip

Design invariants enforced here (see ``validate_episode_pack_stage``):

* The seven semantic JSON files carry a single identity key ``contract`` (no
  synonymous ``schema_version``/``pack_kind`` pair).
* ``content_sha256`` covers **only** the first seven semantic files, in a fixed
  file-name order, using a *framed* canonical encoding
  (``name + NUL + len + NUL + canonical + NUL``).  Operation timestamps never
  enter those files, so the same logical retry produces the same hash; business
  evidence times (observed/valid/published) are allowed and required.
* ``package-manifest.json`` excludes itself and ``_READY.json`` and must cover
  every other file **bidirectionally**, with correct byte size and SHA256.
* ``_READY.json`` is written *after* the manifest.
* Directory publication uses ``os.replace``.  A pre-existing orphan with the
  identical ``pack_id`` is reused only when its content/manifest/READY hashes
  match; otherwise it is a ``revision_collision``.
* ``current.json`` is the single commit point; ``latest.json`` and the notify
  file are best-effort and only written afterwards.

Everything here is offline and dependency-injectable: no network, no model, no
subprocess, no project ``runs``.  The single exception is the opt-in
``research_builder`` adapter (:func:`build_research_semantic`), which only
reaches the network when it is explicitly injected *without* a
``detail_provider``; the default publish path never touches it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from .exporter import atomic_write_json
from .replication_clips import remove_tree
from .replication_delivery import (
    FOLDER_MAIN,
    FOLDER_PROCESS,
    FOLDER_SOURCE,
    FOLDER_SUPPORT,
    build_manifest,
)
from .replication_theme import project_path, sanitize_theme


CONTRACT = "episode-research-pack-v1"
MANIFEST_SCHEMA = "episode-research-pack-manifest-v1"
PRODUCTION_MODE = "single_topic_material_replication"

DEFAULT_OUTPUT_ROOT = "output/每期研究包"
EPISODE_SUFFIX = "_研究包"
STAGING_DIRNAME = ".staging"
PACKS_DIRNAME = "packs"
NOTIFY_DIRNAME = "notify"
CURRENT_NAME = "current.json"
LATEST_NAME = "latest.json"
DELIVERY_MANIFEST_NAME = "清单.json"

#: The seven semantic JSON files.  Their order is the frozen ``content_sha256``
#: order and must never be reordered.
SEMANTIC_FILES: tuple[str, ...] = (
    "episode.json",
    "sources.json",
    "claims.json",
    "audience.json",
    "topics.json",
    "materials.json",
    "rights.json",
)
REVISION_NAME = "revision.json"
RUN_REPORT_NAME = "run-report.json"
README_NAME = "每期研究证据包.md"
MANIFEST_NAME = "package-manifest.json"
READY_NAME = "_READY.json"

REQUIRED_PACK_FILES: tuple[str, ...] = (
    *SEMANTIC_FILES,
    REVISION_NAME,
    RUN_REPORT_NAME,
    README_NAME,
    MANIFEST_NAME,
    READY_NAME,
)
MANIFEST_EXCLUSIONS: tuple[str, ...] = (MANIFEST_NAME, READY_NAME)

#: Fault-injection phases, in the exact order the orchestrator fires them.
FAULT_PHASES: tuple[str, ...] = (
    "after_stage_built",
    "after_stage_validated",
    "after_pack_directory_publish",
    "before_current_flip",
    "after_current_flip",
)

# --- Frozen enumerations ----------------------------------------------------
# ``ready`` requires every claim to be fact-ready; a pack without first-hand
# facts is honestly ``research_required`` -- Douyin heat and b-roll material are
# attention evidence, never factual sources.  ``rejected`` records a
# technically-complete pack vetoed on product/legal/safety/rights grounds.
DISPOSITIONS: tuple[str, ...] = ("ready", "partial", "research_required", "rejected")

#: Claim evidence status -- the project's fact language (``daily_material_exchange``
#: already ships ``FACT_READY = {"confirmed_official", "confirmed_two_reliable"}``).
EVIDENCE_STATUSES: tuple[str, ...] = (
    "confirmed_official",
    "confirmed_two_reliable",
    "creator_primary",
    "unverified",
    "conflicting",
    "insufficient",
)
FACT_READY: frozenset[str] = frozenset({"confirmed_official", "confirmed_two_reliable"})
FRESHNESS_REQUIREMENTS: tuple[str, ...] = ("fresh", "evergreen", "manual_review")
WORDING_POLICIES: tuple[str, ...] = ("assert", "attribute", "hedge", "prohibit")
SOURCE_AUTHORITIES: tuple[str, ...] = (
    "official",
    "reliable_independent",
    "primary_creator",
    "heat_only",
    "unknown",
)
VERIFICATION_STATES: tuple[str, ...] = ("verified", "unverified", "failed", "not_applicable")
FRESHNESS_POLICIES: tuple[str, ...] = ("event_window", "evergreen", "manual_review")
FRESHNESS_STATUSES: tuple[str, ...] = ("fresh", "aging", "expired", "unknown")
RIGHTS_STATUSES: tuple[str, ...] = ("cleared", "review_required", "restricted", "prohibited", "unknown")
ASSET_TYPES: tuple[str, ...] = ("video", "image", "audio", "text_evidence", "frame_capture")
RIGHTS_ORIGINS: tuple[str, ...] = (
    "producer_owned",
    "licensed",
    "platform_content",
    "public_source",
    "unknown",
)
SEGMENT_PURPOSES: tuple[str, ...] = ("visual_support", "context", "transition")
MATERIAL_PERMITTED_USE = "b_roll_only"

#: Frozen ``argument_graph`` node dimensions.
ALLOWED_DIMENSIONS: tuple[str, ...] = (
    "event_core",
    "evidence_detail",
    "mechanism",
    "user_impact",
    "action_tip",
    "industry_value",
    "use_case",
    "constraint",
    "method",
    "limitation",
    "key_number",
    "uncertainty",
    "visual_moment",
)

#: Frozen ``argument_graph`` edge relation whitelist.
ALLOWED_EDGE_RELATIONS: tuple[str, ...] = (
    "supports",
    "qualifies",
    "contrasts",
    "causes",
    "precedes",
)

#: Relations that form a causal/temporal sub-graph which must stay acyclic.
ACYCLIC_EDGE_RELATIONS: frozenset[str] = frozenset({"causes", "precedes"})

#: Edge object key set -- exactly these three keys, no more.  The frozen
#: cross-repo contract (``docs/tasks/2026-09-16-episode-research-pack-v1.md``)
#: names them ``from`` / ``to`` / ``relation``, and the Haike consumer pins the
#: same three keys (``backlot/copy_skill_research_pack.py``).  ``from`` is a
#: Python keyword, so access goes through these constants or ``edge["from"]``
#: instead of ``edge.from``.  Do **not** rename: any other key set is rejected
#: with ``invalid_contract`` on both sides.
EDGE_SOURCE_KEY = "from"
EDGE_TARGET_KEY = "to"
EDGE_KEYS: frozenset[str] = frozenset({EDGE_SOURCE_KEY, EDGE_TARGET_KEY, "relation"})

#: Machine-readable error token consumers can match on.
INVALID_CONTRACT = "invalid_contract"

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class EpisodePackError(RuntimeError):
    """Base class for every research-pack failure."""


class UnsafePathError(EpisodePackError):
    """A path escaped the package root (absolute, drive, UNC or ``..``)."""


class CrossVolumeError(EpisodePackError):
    """The stage and its destination are not on the same volume."""


class RevisionCollisionError(EpisodePackError):
    """A ``pack_id`` already exists with different content."""


class EpisodePackValidationError(EpisodePackError):
    """The staged package failed its self-check and must not be published."""


# --- Canonical encoding / hashing ------------------------------------------


def canonical_json(payload: Any) -> str:
    """The frozen canonical encoding used for ``content_sha256``.

    ``ensure_ascii=False`` keeps non-ASCII stable and readable, ``sort_keys``
    makes the byte stream independent of dict insertion order and the compact
    separators remove all incidental whitespace.
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _framed_bytes(name: str, payload: Any) -> bytes:
    """Framed chunk: ``utf8(name) + NUL + ascii(len) + NUL + canonical + NUL``.

    The length prefix and NUL boundaries make the aggregate unambiguous, so two
    different file splits can never collide.
    """
    canonical = canonical_json(payload).encode("utf-8")
    return name.encode("utf-8") + b"\x00" + str(len(canonical)).encode("ascii") + b"\x00" + canonical + b"\x00"


def content_sha256(payloads: Mapping[str, Any]) -> str:
    """SHA256 over the seven semantic payloads in :data:`SEMANTIC_FILES` order.

    ``payloads`` is keyed by file name (``"episode.json"`` …).  Each file is
    framed and fed in the fixed file-name order, never in directory order, so a
    re-run with identical logical content yields an identical hash regardless of
    filesystem enumeration order.
    """
    digest = hashlib.sha256()
    for name in SEMANTIC_FILES:
        if name not in payloads:
            raise EpisodePackError(f"content_sha256 缺少语义文件 {name}")
        digest.update(_framed_bytes(name, payloads[name]))
    return digest.hexdigest()


def content_sha256_test_vector() -> dict[str, Any]:
    """A shared, dependency-free vector so consumers can pin the algorithm."""
    payloads = {
        "episode.json": {"a": 1},
        "sources.json": {"b": [1, 2]},
        "claims.json": {"c": "文"},
        "audience.json": {"d": None},
        "topics.json": {"e": True},
        "materials.json": {"f": 0},
        "rights.json": {"g": {"h": "i"}},
    }
    return {"payloads": payloads, "content_sha256": content_sha256(payloads)}


#: Literal digest of :func:`content_sha256_test_vector`, exported so a consumer
#: that re-implements the algorithm (e.g. Haike) can *pin* it to the same value
#: and fail loudly on drift instead of both sides independently going green.
#: NOTE: this pins the **algorithm**, not any real pack -- see the
#: ``FROZEN_FIXTURE_*`` constants below for the golden pack's own digests.
FROZEN_CONTENT_SHA256 = "e5bc839ff60a91f29474c9bec50c8afca41fdaf367dc5e6978eae620b1cbaab8"

#: Canonical input of the golden fixture that the ``FROZEN_FIXTURE_*`` digests
#: below describe.  Those digests are a pure function of this input (independent
#: of ``output_root``), so a consumer can pin the whole supersession chain
#: **without reading the pack**.
FIXTURE_BUSINESS_DATE = "2026-09-16"
FIXTURE_THEME = "苹果折叠屏手机"

#: ``content_sha256`` of the canonical golden pack at each revision, as produced
#: by ``build_fixture_pack(business_date=FIXTURE_BUSINESS_DATE, theme=FIXTURE_THEME,
#: revision=N)``.  Distinct from :data:`FROZEN_CONTENT_SHA256`, which pins the
#: algorithm test vector rather than a real pack.
FROZEN_FIXTURE_CONTENT_SHA256_R1 = "7e20195dd253a12de0793b941e510674df9bcb4a715f205e668de39b972d2123"
FROZEN_FIXTURE_CONTENT_SHA256_R2 = "9a465cd53f0b79aad186ed878d8ea10d5f27902323243c179aacbfe04a79d215"

#: Both frozen generations keyed by revision, so a consumer can pin the full
#: ``r1 -> r2`` supersession chain in a single lookup.
FROZEN_FIXTURE_CONTENT_SHA256_BY_REVISION = {
    1: FROZEN_FIXTURE_CONTENT_SHA256_R1,
    2: FROZEN_FIXTURE_CONTENT_SHA256_R2,
}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(Path(path).read_bytes())


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --- Path safety ------------------------------------------------------------


def safe_relative_path(value: str) -> str:
    """Normalise ``value`` to a POSIX relative path or raise :class:`UnsafePathError`.

    Rejects absolute paths, Windows drive letters (``C:``), UNC paths
    (``\\\\server\\share``) and any ``.``/``..`` component.  Used both when
    writing the manifest and when validating it, so a manifest can never point
    outside its own package directory.
    """
    text = str(value or "")
    if not text.strip():
        raise UnsafePathError("路径为空")
    posix = text.replace("\\", "/")
    if posix.startswith("/"):
        raise UnsafePathError(f"拒绝绝对路径：{value}")
    if _DRIVE_RE.match(posix):
        raise UnsafePathError(f"拒绝盘符路径：{value}")
    parts = posix.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise UnsafePathError(f"拒绝不安全路径（空/./..）：{value}")
    return "/".join(parts)


def _same_volume(first: Path | str, second: Path | str) -> bool:
    """True when two paths resolve to the same volume/drive."""
    return Path(first).resolve().drive.lower() == Path(second).resolve().drive.lower()


def _assert_same_volume(stage: Path, destination: Path) -> None:
    if not _same_volume(stage, destination):
        raise CrossVolumeError(
            f"研究包 stage 与目标不在同一卷，拒绝发布：{stage} -> {destination}"
        )


# --- Atomic text write ------------------------------------------------------


def _atomic_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.replace(temporary, path)
    except Exception:
        if temporary and temporary.exists():
            temporary.unlink()
        raise


# --- Naming / identity ------------------------------------------------------


def pack_id_for(episode_id: str, revision: int) -> str:
    return f"{episode_id}-r{int(revision)}"


def default_episode_id(business_date: str, theme: str) -> str:
    """Stable, filename-safe episode identity derived from date + theme."""
    slug = sanitize_theme(theme, max_length=40) or "未命名主题"
    date = str(business_date or "").strip() or "unknown-date"
    return f"{date}-{slug}"


def episode_root_for(config: dict[str, Any], *, output_root: str | Path | None, business_date: str, episode_id: str) -> Path:
    root = project_path(config, str(output_root or DEFAULT_OUTPUT_ROOT))
    return root / f"{business_date}{EPISODE_SUFFIX}" / episode_id


def research_pack_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Read the ``jobs.material_replication.episode_research_pack`` block.

    The feature ships **enabled** (production wants the single-topic research
    pack live); an explicit ``"enabled": false`` turns it off.  Tests neutralise
    it via ``tests/conftest.py`` rather than by relying on the shipped value.
    """
    jobs = config.get("jobs") or {}
    material = jobs.get("material_replication") if isinstance(jobs, dict) else None
    block = material.get("episode_research_pack") if isinstance(material, dict) else None
    block = block if isinstance(block, dict) else {}
    return {
        "enabled": bool(block.get("enabled", True)),
        "output_root": block.get("output_root") or DEFAULT_OUTPUT_ROOT,
        "ledger_root": block.get("ledger_root") or "input/research-ledgers",
        "annotate_delivery_manifest": bool(block.get("annotate_delivery_manifest", False)),
    }


# --- Origin freeze ----------------------------------------------------------


def freeze_origin_ref(delivery_dir: Path) -> dict[str, Any]:
    """Freeze a finished delivery's manifest/item/asset hashes.

    The pack must *not* dynamically chase the delivery's ``latest``/``current``;
    it records exactly the bytes it consumed by SHA256, and it **never writes**
    into the delivery.  Douyin material is material/discovery evidence only, so
    the reference carries ``authority="material_only"`` and
    ``discovery_only=true`` and can never be used to cross the fact gate.
    """
    delivery = Path(delivery_dir)
    manifest_path = delivery / DELIVERY_MANIFEST_NAME
    if not manifest_path.is_file():
        raise FileNotFoundError(f"旧交付缺少 {DELIVERY_MANIFEST_NAME}：{manifest_path}")
    items: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    for child in sorted(delivery.rglob("*")):
        if not child.is_file():
            continue
        rel = child.relative_to(delivery).as_posix()
        safe_relative_path(rel)
        entry = {"path": rel, "bytes": child.stat().st_size, "sha256": _sha256_file(child)}
        top = rel.split("/", 1)[0]
        if top == FOLDER_SOURCE:
            entry["item_id"] = rel
            items.append(entry)
        elif top in (FOLDER_MAIN, FOLDER_SUPPORT):
            entry["asset_id"] = rel
            assets.append(entry)
    return {
        "origin_contract": "material_replication_delivery",
        "origin_pack_id": None,
        "origin_item_id": None,
        "delivery_folder": delivery.name,
        "manifest_path": DELIVERY_MANIFEST_NAME,
        "manifest_sha256": _sha256_file(manifest_path),
        "asset_path": None,
        "asset_sha256": None,
        "asset_bytes": None,
        "authority": "material_only",
        "discovery_only": True,
        "items": items,
        "assets": assets,
    }


# --- Semantic payload derivation -------------------------------------------


def _ms(seconds: Any) -> int:
    try:
        return int(round(float(seconds) * 1000))
    except (TypeError, ValueError):
        return 0


def _clip_metadata(delivery: Path, video_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for folder in (FOLDER_MAIN, FOLDER_SUPPORT):
        directory = delivery / folder
        if not directory.is_dir():
            continue
        for meta_path in sorted(directory.glob("*.json")):
            try:
                meta = _read_json(meta_path)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(meta, Mapping):
                continue
            source = meta.get("source")
            if not isinstance(source, Mapping) or str(source.get("video_id") or "") != video_id:
                continue
            segment_id = str(meta.get("clip_id") or "")
            if not segment_id or segment_id in seen:
                continue
            seen.add(segment_id)
            rows.append(meta)
    return rows


def _derive_materials(delivery: Path, manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build ``materials.json`` records (and matching ``rights.json``) from a delivery.

    Each delivered source video is one material whose segments are its exported
    clips.  ``permitted_use`` is frozen to ``b_roll_only``; the rights record for
    a platform clip defaults to ``review_required`` with rendering disabled --
    Douyin material is b-roll reference, never a cleared/renderable asset.
    """
    sources = manifest.get("material_replica_sources") or []
    materials: list[dict[str, Any]] = []
    rights: list[dict[str, Any]] = []
    seen_video: set[str] = set()
    for index, item in enumerate(sources, start=1):
        video_id = str(item.get("video_id") or "")
        if not video_id or video_id in seen_video:
            continue
        seen_video.add(video_id)
        material_id = f"m{index:02d}-{video_id}"
        segments: list[dict[str, Any]] = []
        for meta in _clip_metadata(delivery, video_id):
            timecode = meta.get("timecode") or {}
            start_ms = _ms(timecode.get("start"))
            end_ms = _ms(timecode.get("end"))
            if end_ms <= start_ms:
                continue
            segments.append({
                "segment_id": str(meta.get("clip_id") or ""),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "claim_ids": [],
                "purpose": "visual_support",
                "transcript_excerpt": "",
                "frame_evidence_ids": [],
            })
        end_candidates = [int(seg["end_ms"]) for seg in segments]
        if not end_candidates and isinstance(item.get("duration_seconds"), (int, float)):
            if float(item["duration_seconds"]) > 0:
                end_candidates.append(_ms(item["duration_seconds"]))
        duration_ms = max(end_candidates) if end_candidates else 0
        clean: list[dict[str, Any]] = []
        for segment in segments:
            end = int(segment["end_ms"])
            if duration_ms and end > duration_ms:
                end = duration_ms
            if end <= int(segment["start_ms"]):
                continue
            clean.append({**segment, "end_ms": end})
        materials.append({
            "material_id": material_id,
            "kind": "b_roll",
            "origin": {
                "video_id": video_id,
                "author": str(item.get("author") or ""),
                "title": str(item.get("title") or ""),
                "share_url": str(item.get("share_url") or item.get("source_url") or ""),
            },
            "permitted_use": MATERIAL_PERMITTED_USE,
            "freshness_status": "unknown",
            "duration_ms": int(duration_ms),
            "segments": clean,
        })
        rights.append({
            "asset_id": f"asset-{material_id}",
            "asset_type": "video",
            "origin": "platform_content",
            "rights_status": "review_required",
            "license": None,
            "attribution": None,
            "redistribution_allowed": False,
            "render_eligible": False,
            "review_reason": "抖音素材仅为发现与关注度证据，不得直接渲染使用；需人工复核权利。",
            "material_id": material_id,
        })
    return materials, rights


def _rights_for_materials(materials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Conservative default ``rights.json`` records for supplied materials.

    Used when a caller injects ``research_inputs["materials"]`` without an
    explicit ``rights`` list.  Mirrors :func:`_derive_materials`'s stance: any
    platform-sourced material starts ``review_required`` with redistribution and
    rendering disabled until a human clears it, so a supplied b-roll clip can
    never silently become a renderable asset.
    """
    rights: list[dict[str, Any]] = []
    for material in materials:
        material_id = str(material.get("material_id") or "")
        if not material_id:
            continue
        rights.append({
            "asset_id": f"asset-{material_id}",
            "asset_type": "video",
            "origin": "platform_content",
            "rights_status": "review_required",
            "license": None,
            "attribution": None,
            "redistribution_allowed": False,
            "render_eligible": False,
            "review_reason": "默认权利待人工复核；素材仅可作 B-roll 参考，不得直接渲染。",
            "material_id": material_id,
        })
    return rights


def _derive_keyword_graph(theme: str, manifest: dict[str, Any]) -> dict[str, Any]:
    used = list(manifest.get("keywords_used") or [])
    requested = list(manifest.get("keywords_requested") or used)
    return {
        "seed": str(theme or manifest.get("theme") or ""),
        "expanded": requested,
        "subject_terms": [],
        "event_terms": [],
        "keywords_requested": requested,
        "keywords_used": used,
        "keywords_truncated": bool(manifest.get("keywords_truncated") or len(requested) > len(used)),
    }


def _derive_disposition(sources: list[dict[str, Any]], claims: list[dict[str, Any]], explicit: str | None) -> str:
    if explicit:
        if explicit not in DISPOSITIONS:
            raise EpisodePackError(f"未知 disposition：{explicit}")
        return explicit
    if not sources and not claims:
        return "research_required"
    statuses = {str(claim.get("evidence_status") or "") for claim in claims}
    if claims and statuses and statuses <= FACT_READY:
        return "ready"
    return "partial"


def derive_semantic_from_delivery(
    delivery_dir: Path,
    *,
    episode_id: str,
    theme: str | None = None,
    business_date: str | None = None,
    research_inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive the seven semantic payloads from a finished delivery.

    ``research_inputs`` (optional) may supply first-hand ``sources`` / ``claims``
    / ``topics`` / ``audience`` / ``disposition``.  Without it the pack is
    technically complete but honestly ``research_required``: there are no
    first-hand facts, so no claim may be promoted and material references are
    never counted as factual sources.
    """
    delivery = Path(delivery_dir)
    manifest = _read_json(delivery / DELIVERY_MANIFEST_NAME)
    theme = theme or str(manifest.get("theme") or "")
    business_date = business_date or str(manifest.get("business_date") or "")
    inputs = dict(research_inputs or {})

    materials, rights = _derive_materials(delivery, manifest)
    if inputs.get("materials") is not None:
        materials = [dict(item) for item in inputs["materials"]]
        supplied_rights = inputs.get("rights")
        rights = ([dict(item) for item in supplied_rights] if supplied_rights is not None
                  else _rights_for_materials(materials))
    elif inputs.get("rights") is not None:
        rights = [dict(item) for item in inputs["rights"]]
    sources = list(inputs.get("sources") or [])
    claims = list(inputs.get("claims") or [])
    audience = inputs.get("audience") or {"status": "unknown", "summary": "", "segments": []}
    default_topics = {
        "keyword_graph": _derive_keyword_graph(theme, manifest),
        "topic_candidates": [{
            "topic_id": "topic-01",
            "title": theme,
            "keywords": list(manifest.get("keywords_used") or []),
            "selection_basis": "single_theme",
            "producer_proposal": True,
        }],
        "selected_topic": {"topic_id": "topic-01", "selection_basis": "single_theme", "producer_proposal": True},
        "argument_graph": {"topic_id": "topic-01", "nodes": [], "edges": []},
    }
    topics = inputs.get("topics") or default_topics
    disposition = _derive_disposition(sources, claims, inputs.get("disposition"))
    identity = {"contract": CONTRACT, "episode_id": episode_id, "business_date": business_date}

    episode = {
        **identity,
        "production_mode": PRODUCTION_MODE,
        "theme": theme,
        "disposition": disposition,
        "keywords": list(manifest.get("keywords_used") or []),
        "origin_ref": freeze_origin_ref(delivery),
        "warnings": list(manifest.get("warnings") or []),
    }
    return {
        "episode.json": episode,
        "sources.json": {**identity, "sources": sources},
        "claims.json": {**identity, "claims": claims},
        "audience.json": {**identity, "audience": audience},
        "topics.json": {**identity, **topics},
        "materials.json": {**identity, "materials": materials},
        "rights.json": {**identity, "rights": rights},
    }


# --- Human-readable evidence pack ------------------------------------------


def render_episode_readme(semantic: Mapping[str, Any], *, pack_id: str, revision: int) -> str:
    episode = semantic.get("episode.json") or {}
    materials = (semantic.get("materials.json") or {}).get("materials") or []
    sources = (semantic.get("sources.json") or {}).get("sources") or []
    claims = (semantic.get("claims.json") or {}).get("claims") or []
    topics_payload = semantic.get("topics.json") or {}
    selected = topics_payload.get("selected_topic") or {}
    lines = [
        f"# 每期研究证据包（{episode.get('theme') or ''}）",
        "",
        "- 证据声明：抖音素材仅为发现与关注度证据，不得作为事实依据；"
        "material 仅可作 B-roll 参考，且默认 render_eligible=false。",
        "",
        f"- episode_id：{episode.get('episode_id')}",
        f"- pack_id：{pack_id}（revision r{revision}）",
        f"- 业务日期：{episode.get('business_date')}",
        f"- 生产模式：{episode.get('production_mode')}",
        f"- 处置：{episode.get('disposition')}",
        f"- 选定主题：{selected.get('topic_id') or '无'}",
        "",
        "## 事实来源",
        "",
    ]
    if sources:
        for source in sources:
            freshness = source.get("freshness") or {}
            lines.append(
                f"- {source.get('source_id')}｜{source.get('authority')}｜"
                f"{source.get('verification_state')}｜{source.get('publisher')}｜"
                f"{source.get('title')}｜{source.get('url')}｜时效 {freshness.get('status_at_publish')}"
            )
    else:
        lines.append("- 无第一手事实来源：本包仅提供 B-roll 素材，证据状态为 research_required。")
    lines.extend(["", "## 主张与证据状态", ""])
    if claims:
        for claim in claims:
            lines.append(
                f"- {claim.get('claim_id')}｜{claim.get('evidence_status')}｜{claim.get('wording_policy')}"
                f"｜{claim.get('text')}｜引用素材 {claim.get('material_refs') or '无'}"
            )
    else:
        lines.append("- 无：未提供第一手事实数据，未生成任何主张。")
    lines.extend(["", "## B-roll 素材", ""])
    for material in materials:
        segments = material.get("segments") or []
        seg_text = "、".join(
            f"{seg.get('segment_id')}[{seg.get('start_ms')}~{seg.get('end_ms')}ms]" for seg in segments
        )
        lines.append(
            f"- {material.get('material_id')}｜{material.get('origin', {}).get('video_id')}"
            f"｜{material.get('permitted_use')}｜时长 {material.get('duration_ms')}ms｜片段：{seg_text or '无'}"
        )
    if not materials:
        lines.append("- 无")
    lines.append("")
    return "\n".join(lines)


# --- Public interfaces ------------------------------------------------------


def _episode_identity(semantic: Mapping[str, Any]) -> tuple[str, str, str, str]:
    episode = semantic.get("episode.json") or {}
    return (
        str(episode.get("episode_id") or ""),
        str(episode.get("business_date") or ""),
        str(episode.get("disposition") or ""),
        str(episode.get("production_mode") or ""),
    )


def build_episode_pack_stage(
    stage: Path,
    *,
    episode_id: str,
    revision: int,
    semantic: Mapping[str, Any],
    generated_at: str,
    business_date: str | None = None,
    supersedes: Mapping[str, Any] | None = None,
    run_report: Mapping[str, Any] | None = None,
    readme: str | None = None,
) -> Path:
    """Write a complete, ready-to-validate stage directory; return its path.

    The seven semantic files are written verbatim; ``content_sha256`` is then
    computed from their framed canonical form.  ``revision.json`` and
    ``run-report.json`` are the only files allowed to carry operation
    timestamps, and both are excluded from the hash.  ``package-manifest.json``
    is written last-but-one and ``_READY.json`` strictly after it.
    """
    stage = Path(stage)
    remove_tree(stage)
    stage.mkdir(parents=True, exist_ok=True)

    for name in SEMANTIC_FILES:
        if name not in semantic:
            raise EpisodePackError(f"缺少语义文件 {name}")
    for name in SEMANTIC_FILES:
        atomic_write_json(stage / name, semantic[name])

    episode = semantic.get("episode.json") or {}
    business_date = business_date or str(episode.get("business_date") or "")
    disposition = str(episode.get("disposition") or "")
    csha = content_sha256(semantic)
    pack_id = pack_id_for(episode_id, revision)
    identity = {"contract": CONTRACT, "episode_id": episode_id, "business_date": business_date}
    supersedes_payload = dict(supersedes) if supersedes else None

    atomic_write_json(stage / REVISION_NAME, {
        **identity,
        "revision": int(revision),
        "pack_id": pack_id,
        "content_sha256": csha,
        "disposition": disposition,
        "generated_at": generated_at,
        "supersedes": supersedes_payload,
        "change": "initial" if not supersedes_payload else "content_changed",
    })
    report = dict(run_report or {})
    report.update({
        **identity,
        "revision": int(revision),
        "pack_id": pack_id,
        "generated_at": generated_at,
        "disposition": disposition,
    })
    report.setdefault("counts", _semantic_counts(semantic))
    atomic_write_json(stage / RUN_REPORT_NAME, report)

    text = readme if readme is not None else render_episode_readme(semantic, pack_id=pack_id, revision=int(revision))
    _atomic_text(stage / README_NAME, text)

    _write_manifest_and_ready(
        stage, pack_id=pack_id, episode_id=episode_id, business_date=business_date,
        revision=int(revision), content_sha256=csha, supersedes=supersedes_payload, ready_at=generated_at,
    )
    return stage


def _semantic_counts(semantic: Mapping[str, Any]) -> dict[str, int]:
    return {
        "sources": len((semantic.get("sources.json") or {}).get("sources") or []),
        "claims": len((semantic.get("claims.json") or {}).get("claims") or []),
        "topic_candidates": len((semantic.get("topics.json") or {}).get("topic_candidates") or []),
        "materials": len((semantic.get("materials.json") or {}).get("materials") or []),
        "rights": len((semantic.get("rights.json") or {}).get("rights") or []),
    }


def _write_manifest_and_ready(
    stage: Path, *, pack_id: str, episode_id: str, business_date: str,
    revision: int, content_sha256: str, supersedes: Mapping[str, Any] | None, ready_at: str,
) -> None:
    files: list[dict[str, Any]] = []
    for child in sorted(stage.rglob("*")):
        if not child.is_file():
            continue
        rel = child.relative_to(stage).as_posix()
        if rel in MANIFEST_EXCLUSIONS:
            continue
        safe_relative_path(rel)
        data = child.read_bytes()
        files.append({"path": rel, "bytes": len(data), "sha256": _sha256_bytes(data)})
    files.sort(key=lambda item: item["path"])
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "contract": CONTRACT,
        "episode_id": episode_id,
        "business_date": business_date,
        "revision": int(revision),
        "pack_id": pack_id,
        "content_sha256": content_sha256,
        "files": files,
        "exclusions": list(MANIFEST_EXCLUSIONS),
    }
    manifest_path = stage / MANIFEST_NAME
    atomic_write_json(manifest_path, manifest)
    ready = {
        "contract": CONTRACT,
        "episode_id": episode_id,
        "business_date": business_date,
        "revision": int(revision),
        "pack_id": pack_id,
        "content_sha256": content_sha256,
        "manifest_sha256": _sha256_file(manifest_path),
        "file_count": len(files),
        "supersedes": dict(supersedes) if supersedes else None,
        "ready_at": ready_at,
    }
    atomic_write_json(stage / READY_NAME, ready)


def validate_episode_pack_stage(stage: Path) -> dict[str, Any]:
    """Self-contained package check; no network, no external tooling.

    Returns ``{"status": "pass"|"fail", "path", "errors", ...}``.  A ``fail``
    must block publication -- the orchestrator raises rather than publishing.
    """
    stage = Path(stage)
    errors: list[str] = []
    if not stage.is_dir():
        return {"status": "fail", "path": str(stage), "errors": ["研究包目录不存在"]}

    for name in REQUIRED_PACK_FILES:
        if not (stage / name).is_file():
            errors.append(f"缺少文件 {name}")

    payloads: dict[str, Any] = {}
    for name in SEMANTIC_FILES:
        path = stage / name
        if not path.is_file():
            continue
        try:
            payloads[name] = _read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"语义文件无法解析 {name}：{exc}")

    csha: str | None = None
    if len(payloads) == len(SEMANTIC_FILES):
        csha = content_sha256(payloads)

    revision_payload = _safe_load(stage / REVISION_NAME, errors)
    ready = _safe_load(stage / READY_NAME, errors)
    manifest = _safe_load(stage / MANIFEST_NAME, errors)

    if csha is not None:
        for label, payload in (("revision.json", revision_payload), (MANIFEST_NAME, manifest), (READY_NAME, ready)):
            if payload.get("content_sha256") != csha:
                errors.append(f"{label} 的 content_sha256 与语义文件不一致")

    _validate_identity(payloads, revision_payload, ready, manifest, errors)
    _validate_episode(payloads, revision_payload, errors)
    _validate_topics(payloads, errors)
    _validate_manifest(stage, manifest, csha, errors)
    _validate_ready(stage, manifest, ready, errors)
    _validate_materials_claims_rights(payloads, errors)
    _validate_sources(payloads, errors)

    return {
        "status": "pass" if not errors else "fail",
        "path": str(stage.resolve()),
        "errors": errors,
        "content_sha256": csha,
        "pack_id": revision_payload.get("pack_id"),
        "revision": revision_payload.get("revision"),
        "counts": _semantic_counts(payloads) if payloads else {},
    }


def _safe_load(path: Path, errors: list[str]) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{path.name} 无法解析：{exc}")
        return {}
    return payload if isinstance(payload, dict) else {}


def _validate_identity(
    payloads: Mapping[str, Any], revision_payload: Mapping[str, Any],
    ready: Mapping[str, Any], manifest: Mapping[str, Any], errors: list[str],
) -> None:
    episode = payloads.get("episode.json") or {}
    episode_id = str(episode.get("episode_id") or "")
    business_date = str(episode.get("business_date") or "")
    for name in SEMANTIC_FILES:
        payload = payloads.get(name)
        if not isinstance(payload, Mapping):
            continue
        if payload.get("contract") != CONTRACT:
            errors.append(f"{name} 缺少/非法 contract 键")
        if payload.get("episode_id") != episode_id:
            errors.append(f"{name} episode_id 与 episode 不一致")
        if payload.get("business_date") != business_date:
            errors.append(f"{name} business_date 与 episode 不一致")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        errors.append(f"manifest.schema 必须为 {MANIFEST_SCHEMA}")
    if manifest.get("contract") != CONTRACT:
        errors.append("manifest.contract 非法")
    for label, payload in ((REVISION_NAME, revision_payload), (READY_NAME, ready)):
        if payload and payload.get("contract") != CONTRACT:
            errors.append(f"{label} contract 非法")
        if payload and payload.get("episode_id") != episode_id:
            errors.append(f"{label} episode_id 与 episode 不一致")


def _validate_episode(payloads: Mapping[str, Any], revision_payload: Mapping[str, Any], errors: list[str]) -> None:
    episode = payloads.get("episode.json") or {}
    if episode.get("production_mode") != PRODUCTION_MODE:
        errors.append(f"episode.production_mode 必须为 {PRODUCTION_MODE}")
    episode_id = str(episode.get("episode_id") or "")
    if not episode_id:
        errors.append("episode.episode_id 缺失")
    if episode.get("disposition") not in DISPOSITIONS:
        errors.append(f"episode.disposition 非法：{episode.get('disposition')}")
    origin = episode.get("origin_ref") or {}
    if not isinstance(origin, Mapping):
        errors.append("episode.origin_ref 缺失或非法")
    else:
        if origin.get("origin_contract") != "material_replication_delivery":
            errors.append("origin_ref.origin_contract 非法")
        if not origin.get("manifest_sha256"):
            errors.append("origin_ref.manifest_sha256 缺失")
        if origin.get("authority") not in ("material_only", "heat_only", "fact_candidate"):
            errors.append(f"origin_ref.authority 非法：{origin.get('authority')}")
        if not isinstance(origin.get("discovery_only"), bool):
            errors.append("origin_ref.discovery_only 必须为布尔值")

        def _check_frozen(rows: list[Any], label: str, id_key: str) -> None:
            for row in rows or []:
                if not isinstance(row, Mapping):
                    errors.append(f"origin_ref.{label} 记录非法")
                    continue
                if not row.get(id_key):
                    errors.append(f"origin_ref.{label} 缺少 {id_key}")
                try:
                    safe_relative_path(str(row.get("path") or ""))
                except UnsafePathError as exc:
                    errors.append(f"origin_ref.{label} 路径不安全：{exc}")
                # ``isinstance(True, int)`` 为真 ⇒ 布尔会冒充整数，必须显式排除。
                raw_bytes = row.get("bytes")
                if isinstance(raw_bytes, bool) or not isinstance(raw_bytes, int):
                    errors.append(f"origin_ref.{label} 缺少整数 bytes")
                if not row.get("sha256"):
                    errors.append(f"origin_ref.{label} 缺少 sha256")

        _check_frozen(list(origin.get("items") or []), "items", "item_id")
        _check_frozen(list(origin.get("assets") or []), "assets", "asset_id")

    if revision_payload:
        if not str(revision_payload.get("pack_id") or "").startswith(f"{episode_id}-r"):
            errors.append("revision.pack_id 与 episode_id 不一致")
        if revision_payload.get("disposition") not in DISPOSITIONS:
            errors.append(f"revision.disposition 非法：{revision_payload.get('disposition')}")


def _validate_topics(payloads: Mapping[str, Any], errors: list[str]) -> None:
    topics = payloads.get("topics.json") or {}
    graph = topics.get("keyword_graph")
    if not isinstance(graph, Mapping):
        errors.append("topics.keyword_graph 缺失或非法")
    else:
        for key in ("seed", "expanded", "subject_terms", "event_terms",
                    "keywords_requested", "keywords_used", "keywords_truncated"):
            if key not in graph:
                errors.append(f"topics.keyword_graph 缺少 {key}")
    if not isinstance(topics.get("topic_candidates"), list):
        errors.append("topics.topic_candidates 必须为列表")
    selected = topics.get("selected_topic")
    if selected is not None and not isinstance(selected, Mapping):
        errors.append("topics.selected_topic 必须为对象或 null")
    argument = topics.get("argument_graph")
    if not isinstance(argument, Mapping):
        errors.append("topics.argument_graph 缺失或非法")
        return
    nodes = argument.get("nodes")
    edges = argument.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        errors.append("topics.argument_graph.nodes/edges 必须为列表")
        return
    node_claim_ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, Mapping):
            errors.append("argument_graph.node 记录非法")
            continue
        claim_id = str(node.get("claim_id") or "")
        if not claim_id:
            errors.append("argument_graph.node 缺少 claim_id")
        node_claim_ids.add(claim_id)
        dim = node.get("dim")
        if not isinstance(dim, str) or not dim:
            errors.append(f"argument_graph.node {claim_id} 缺少 dim（{INVALID_CONTRACT}）")
        elif dim not in ALLOWED_DIMENSIONS:
            errors.append(f"argument_graph.node {claim_id} dim {INVALID_CONTRACT}：{dim}")
        if not isinstance(node.get("claim"), str) or not node.get("claim"):
            errors.append(f"argument_graph.node {claim_id} 缺少 claim 文本")
        if not isinstance(node.get("source_candidate_ids"), list):
            errors.append(f"argument_graph.node {claim_id} 缺少 source_candidate_ids")

    seen_triples: set[tuple[str, str, str]] = set()
    acyclic_edges: list[tuple[str, str]] = []
    for edge in edges:
        if not isinstance(edge, Mapping):
            errors.append("argument_graph.edge 记录非法")
            continue
        if set(edge.keys()) != EDGE_KEYS:
            errors.append(f"argument_graph.edge 键集 {INVALID_CONTRACT}：{sorted(edge.keys())}")
        source = str(edge.get(EDGE_SOURCE_KEY) or "")
        target = str(edge.get(EDGE_TARGET_KEY) or "")
        relation = str(edge.get("relation") or "")
        if not source or not target or not relation:
            errors.append(f"argument_graph.edge 缺少 {EDGE_SOURCE_KEY}/{EDGE_TARGET_KEY}/relation（{INVALID_CONTRACT}）")
        if relation and relation not in ALLOWED_EDGE_RELATIONS:
            errors.append(f"argument_graph.edge relation {INVALID_CONTRACT}：{relation}")
        if source and source not in node_claim_ids:
            errors.append(f"argument_graph.edge.{EDGE_SOURCE_KEY} 未引用已知节点：{source}")
        if target and target not in node_claim_ids:
            errors.append(f"argument_graph.edge.{EDGE_TARGET_KEY} 未引用已知节点：{target}")
        if source and source == target:
            errors.append(f"argument_graph.edge 不允许自指：{source}")
        triple = (source, target, relation)
        if triple in seen_triples:
            errors.append(f"argument_graph.edge 三元组重复：{triple}")
        seen_triples.add(triple)
        if relation in ACYCLIC_EDGE_RELATIONS and source and target and source != target:
            acyclic_edges.append((source, target))
    if _has_cycle(acyclic_edges):
        errors.append(f"argument_graph causes/precedes 子图存在环（{INVALID_CONTRACT}）")


def _has_cycle(edges: list[tuple[str, str]]) -> bool:
    """True when the directed edge list (causes/precedes sub-graph) has a cycle."""
    adjacency: dict[str, list[str]] = {}
    for source, target in edges:
        adjacency.setdefault(source, []).append(target)
        adjacency.setdefault(target, [])
    state: dict[str, int] = {}

    def visit(node: str) -> bool:
        state[node] = 1
        for neighbour in adjacency.get(node, []):
            marker = state.get(neighbour, 0)
            if marker == 1 or (marker == 0 and visit(neighbour)):
                return True
        state[node] = 2
        return False

    return any(state.get(node, 0) == 0 and visit(node) for node in list(adjacency))


def _validate_manifest(stage: Path, manifest: Mapping[str, Any], csha: str | None, errors: list[str]) -> None:
    if not manifest:
        errors.append("package-manifest.json 缺失或为空")
        return
    if list(manifest.get("exclusions") or []) != list(MANIFEST_EXCLUSIONS):
        errors.append("manifest.exclusions 必须为 package-manifest.json 与 _READY.json")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        errors.append("manifest.files 必须为列表")
        return
    listed: dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            errors.append("manifest.files 记录非法")
            continue
        raw = str(entry.get("path") or "")
        try:
            rel = safe_relative_path(raw)
        except UnsafePathError as exc:
            errors.append(f"manifest 路径不安全：{exc}")
            continue
        if rel != raw:
            errors.append(f"manifest 路径未规范化：{raw}")
        if rel in MANIFEST_EXCLUSIONS:
            errors.append(f"manifest 不得收录自身/READY：{rel}")
        if rel in listed:
            errors.append(f"manifest 重复收录：{rel}")
        listed[rel] = entry
        target = stage / rel
        if not target.is_file():
            errors.append(f"manifest 记录的文件不存在：{rel}")
            continue
        data = target.read_bytes()
        if entry.get("bytes") != len(data):
            errors.append(f"manifest 字节数不符：{rel}")
        if entry.get("sha256") != _sha256_bytes(data):
            errors.append(f"manifest SHA256 不符：{rel}")

    on_disk: set[str] = set()
    for child in sorted(stage.rglob("*")):
        if not child.is_file():
            continue
        rel = child.relative_to(stage).as_posix()
        if rel in MANIFEST_EXCLUSIONS:
            continue
        on_disk.add(rel)
    missing = on_disk - set(listed)
    extra = set(listed) - on_disk
    if missing:
        errors.append(f"manifest 未覆盖文件：{sorted(missing)}")
    if extra:
        errors.append(f"manifest 收录了不存在的文件：{sorted(extra)}")
    if csha is not None and manifest.get("content_sha256") != csha:
        errors.append("manifest.content_sha256 与语义文件不一致")


def _validate_ready(stage: Path, manifest: Mapping[str, Any], ready: Mapping[str, Any], errors: list[str]) -> None:
    if not ready:
        errors.append("_READY.json 缺失或为空")
        return
    if manifest:
        manifest_path = stage / MANIFEST_NAME
        if manifest_path.is_file() and ready.get("manifest_sha256") != _sha256_file(manifest_path):
            errors.append("_READY.manifest_sha256 与 manifest 不一致")
        if ready.get("pack_id") != manifest.get("pack_id"):
            errors.append("_READY.pack_id 与 manifest 不一致")
        if isinstance(manifest.get("files"), list) and ready.get("file_count") != len(manifest["files"]):
            errors.append("_READY.file_count 与 manifest 不一致")
    if "supersedes" not in ready:
        errors.append("_READY 缺少 supersedes")
    if not ready.get("ready_at"):
        errors.append("_READY 缺少 ready_at")


def _array_field(record: Mapping[str, Any], key: str, label: str, errors: list[str]) -> list[Any]:
    """「键必须存在且为数组」—— 缺键与假值必须分开，空数组是合法值。

    ``record.get(key) or []`` 把「键缺失」「键为 ``None``」「键为 ``""``」和
    「键为空数组」混为一谈：前三者是非法的，最后一个是合法的。消费端用
    ``_as_list`` 严格判类型（``{label} 必须是数组``），这里与之同口径，
    错误文案也一致，便于两端互认。返回可直接迭代的列表；不合法时返回 ``[]``
    并追加一条错误。
    """
    value = record.get(key)
    if not isinstance(value, list):
        errors.append(f"{label} 必须是数组")
        return []
    return value


def _validate_materials_claims_rights(payloads: Mapping[str, Any], errors: list[str]) -> None:
    materials = (payloads.get("materials.json") or {}).get("materials") or []
    claims = (payloads.get("claims.json") or {}).get("claims") or []
    rights = (payloads.get("rights.json") or {}).get("rights") or []

    material_ids: set[str] = set()
    segment_pairs: set[tuple[str, str]] = set()
    segment_claim_refs: dict[str, set[str]] = {}
    for material in materials:
        material_id = str(material.get("material_id") or "")
        if not material_id:
            errors.append("material 缺少 material_id")
            continue
        if material_id in material_ids:
            errors.append(f"material_id 重复：{material_id}")
        material_ids.add(material_id)
        if material.get("permitted_use") != MATERIAL_PERMITTED_USE:
            errors.append(f"material {material_id} permitted_use 必须为 {MATERIAL_PERMITTED_USE}")
        # 键存在但为空串是**非法枚举值**，不能落进「未提供」分支（缺键才是未提供）。
        freshness_status = material.get("freshness_status")
        if freshness_status is not None and freshness_status not in FRESHNESS_STATUSES:
            errors.append(f"material {material_id} freshness_status 非法：{freshness_status!r}")
        if isinstance(material.get("duration_ms"), bool) or not isinstance(material.get("duration_ms"), int):
            errors.append(f"material {material_id} duration_ms 必须为整数")
            continue
        duration_ms = material["duration_ms"]
        if duration_ms < 0:
            errors.append(f"material {material_id} duration_ms 不得为负")
        if not isinstance(material.get("segments"), list):
            errors.append(f"material {material_id} segments 必须为列表")
            continue
        for segment in material.get("segments") or []:
            segment_id = str(segment.get("segment_id") or "")
            if not segment_id:
                errors.append(f"material {material_id} 片段缺少 segment_id")
                continue
            pair = (material_id, segment_id)
            if pair in segment_pairs:
                errors.append(f"(material_id, segment_id) 重复：{pair}")
            segment_pairs.add(pair)
            start_ms, end_ms = segment.get("start_ms"), segment.get("end_ms")
            if any(isinstance(v, bool) or not isinstance(v, int) for v in (start_ms, end_ms)):
                errors.append(f"片段 {pair} 时间码必须为整数毫秒")
                continue
            if not (0 <= start_ms < end_ms <= duration_ms):
                errors.append(f"片段 {pair} 时间码越界：0<= {start_ms} < {end_ms} <= {duration_ms}")
            if segment.get("purpose") not in SEGMENT_PURPOSES:
                errors.append(f"片段 {pair} purpose 非法：{segment.get('purpose')}")
            if not isinstance(segment.get("transcript_excerpt"), str):
                errors.append(f"片段 {pair} transcript_excerpt 必须为字符串")
            if not isinstance(segment.get("frame_evidence_ids"), list):
                errors.append(f"片段 {pair} frame_evidence_ids 必须为列表")
            refs = segment_claim_refs.setdefault(material_id, set())
            for claim_id in _array_field(segment, "claim_ids", f"片段 {pair} claim_ids", errors):
                refs.add(str(claim_id))

    claims_by_id: dict[str, Mapping[str, Any]] = {}
    claim_material_refs: dict[str, list[Any]] = {}
    for claim in claims:
        claim_id = str(claim.get("claim_id") or "")
        if not claim_id:
            errors.append("claim 缺少 claim_id")
            continue
        if claim_id in claims_by_id:
            errors.append(f"claim_id 重复：{claim_id}")
        claims_by_id[claim_id] = claim
        if claim.get("evidence_status") not in EVIDENCE_STATUSES:
            errors.append(f"claim {claim_id} evidence_status 非法：{claim.get('evidence_status')}")
        if claim.get("wording_policy") not in WORDING_POLICIES:
            errors.append(f"claim {claim_id} wording_policy 非法：{claim.get('wording_policy')}")
        if claim.get("freshness_requirement") not in FRESHNESS_REQUIREMENTS:
            errors.append(f"claim {claim_id} freshness_requirement 非法：{claim.get('freshness_requirement')}")
        if not isinstance(claim.get("text"), str) or not claim.get("text"):
            errors.append(f"claim {claim_id} 缺少 text")
        if not isinstance(claim.get("claims_to_verify"), list):
            errors.append(f"claim {claim_id} 缺少 claims_to_verify")
        if not isinstance(claim.get("do_not_claim"), list):
            errors.append(f"claim {claim_id} 缺少 do_not_claim")
        material_refs = _array_field(claim, "material_refs", f"claim {claim_id} material_refs", errors)
        claim_material_refs[claim_id] = material_refs
        for ref in material_refs:
            if str(ref) not in material_ids:
                errors.append(f"claim {claim_id} 引用了不存在的 material：{ref}")

    # Bidirectional: every claim's material_refs must appear in that material's
    # segments' claim_ids and vice versa.
    for claim in claims:
        claim_id = str(claim.get("claim_id") or "")
        for ref in claim_material_refs.get(claim_id, []):
            if claim_id not in segment_claim_refs.get(str(ref), set()):
                errors.append(f"claim {claim_id} 的 material_ref {ref} 未在其片段 claim_ids 中出现")
    for material_id, refs in segment_claim_refs.items():
        for claim_id in refs:
            claim = claims_by_id.get(claim_id)
            if claim is None:
                errors.append(f"material {material_id} 片段引用了不存在的 claim：{claim_id}")
            elif material_id not in {str(ref) for ref in claim_material_refs.get(claim_id, [])}:
                errors.append(f"material {material_id} 片段 claim_id {claim_id} 未被该 claim 的 material_refs 反向引用")

    rights_materials: set[str] = set()
    for record in rights:
        asset_id = str(record.get("asset_id") or "")
        if not asset_id:
            errors.append("rights 记录缺少 asset_id")
        if record.get("asset_type") not in ASSET_TYPES:
            errors.append(f"rights {asset_id} asset_type 非法：{record.get('asset_type')}")
        if record.get("origin") not in RIGHTS_ORIGINS:
            errors.append(f"rights {asset_id} origin 非法：{record.get('origin')}")
        if record.get("rights_status") not in RIGHTS_STATUSES:
            errors.append(f"rights {asset_id} rights_status 非法：{record.get('rights_status')}")
        for key in ("redistribution_allowed", "render_eligible"):
            if not isinstance(record.get(key), bool):
                errors.append(f"rights {asset_id} {key} 必须为布尔值")
        if record.get("rights_status") == "prohibited" and (
            record.get("redistribution_allowed") or record.get("render_eligible")
        ):
            errors.append(f"rights {asset_id} 为 prohibited 但允许再分发/渲染")
        if record.get("origin") == "platform_content" and record.get("render_eligible"):
            errors.append(f"rights {asset_id} 为平台内容但 render_eligible=true")
        material_id = str(record.get("material_id") or "")
        if material_id:
            if material_id not in material_ids:
                errors.append(f"rights 记录引用了不存在的 material：{material_id}")
            rights_materials.add(material_id)
    for material_id in material_ids:
        if material_id not in rights_materials:
            errors.append(f"material {material_id} 缺少 rights 记录")


def _is_heat_only_source(source: Mapping[str, Any]) -> bool:
    """热度信号不是证据：``heat_only`` 标志或 ``authority="heat_only"`` 都算。

    与消费端（Haike ``copy_skill_research_pack.py``）**同口径**。分叉会朝最坏
    的方向失败：生产端把热度源算成事实源就会放行 ``validation.status=pass``，
    消费端复算同一 claim 时必抛「``fact_sources_present`` 与已核验来源数不一致」
    （``exit 2`` / ``stage=intake``），运营方会发布一个消费端永远吃不进去的包。
    """
    if source.get("heat_only") is True:
        return True
    return source.get("authority") == "heat_only"


def _is_fact_source(source: Mapping[str, Any]) -> bool:
    """事实源 = 已核验 **且** 非热度源。冻结合同要求复算的只有这一种。

    凡在本模块内统计「已核验事实来源数量」（``fact_sources_present``、
    ``confirmed_official`` 的官方源、``confirmed_two_reliable`` 的独立出版方
    计数）都必须走这一个谓词，避免改一处漏一处。
    """
    return not _is_heat_only_source(source) and source.get("verification_state") == "verified"


def _validate_sources(payloads: Mapping[str, Any], errors: list[str]) -> None:
    sources = (payloads.get("sources.json") or {}).get("sources") or []
    claims = (payloads.get("claims.json") or {}).get("claims") or []
    materials = (payloads.get("materials.json") or {}).get("materials") or []
    source_ids: set[str] = set()
    sources_by_id: dict[str, Mapping[str, Any]] = {}
    material_ids = {str(item.get("material_id") or "") for item in materials}
    for source in sources:
        source_id = str(source.get("source_id") or "")
        if not source_id:
            errors.append("source 缺少 source_id")
            continue
        if source_id in source_ids:
            errors.append(f"source_id 重复：{source_id}")
        source_ids.add(source_id)
        sources_by_id[source_id] = source
        if source.get("authority") not in SOURCE_AUTHORITIES:
            errors.append(f"source {source_id} authority 非法：{source.get('authority')}")
        if source.get("verification_state") not in VERIFICATION_STATES:
            errors.append(f"source {source_id} verification_state 非法：{source.get('verification_state')}")
        if not isinstance(source.get("heat_only"), bool):
            errors.append(f"source {source_id} heat_only 必须为布尔值")
        freshness = source.get("freshness")
        if not isinstance(freshness, Mapping):
            errors.append(f"source {source_id} 缺少 freshness 结构")
        else:
            if not freshness.get("observed_at"):
                errors.append(f"source {source_id} freshness.observed_at 缺失")
            if freshness.get("policy") not in FRESHNESS_POLICIES:
                errors.append(f"source {source_id} freshness.policy 非法：{freshness.get('policy')}")
            if freshness.get("status_at_publish") not in FRESHNESS_STATUSES:
                errors.append(f"source {source_id} freshness.status_at_publish 非法：{freshness.get('status_at_publish')}")

    for claim in claims:
        claim_id = str(claim.get("claim_id") or "")
        # 缺键 / 非数组必须先拦下：否则字符串会被逐字符迭代，报出一串
        # `引用了不存在的事实源：g / p / -` 之类的噪声，掩盖真正的原因。
        source_ids_in_claim = [
            str(ref) for ref in _array_field(claim, "source_ids", f"claim {claim_id} source_ids", errors)
        ]
        for ref in source_ids_in_claim:
            if ref in material_ids:
                errors.append(f"claim {claim_id} 把 material {ref} 当作事实源")
            elif ref not in source_ids:
                errors.append(f"claim {claim_id} 引用了不存在的事实源：{ref}")
        verified = [
            ref for ref in source_ids_in_claim
            if ref in sources_by_id and _is_fact_source(sources_by_id[ref])
        ]
        if claim.get("fact_sources_present") != len(verified):
            errors.append(f"claim {claim_id} fact_sources_present 与已核验来源数不一致")
        expected_min = {"confirmed_official": 1, "confirmed_two_reliable": 2}.get(str(claim.get("evidence_status")), 0)
        if claim.get("fact_sources_min") != expected_min:
            errors.append(f"claim {claim_id} fact_sources_min 应为 {expected_min}")
        status = str(claim.get("evidence_status"))
        if status == "confirmed_official":
            if not any(sources_by_id[ref].get("authority") == "official" for ref in verified):
                errors.append(f"claim {claim_id} 声明 confirmed_official 但没有官方已核验来源")
        elif status == "confirmed_two_reliable":
            independent = {
                str(sources_by_id[ref].get("publisher") or ref)
                for ref in verified
                if sources_by_id[ref].get("authority") in ("official", "reliable_independent")
            }
            if len(independent) < 2:
                errors.append(f"claim {claim_id} 声明 confirmed_two_reliable 但不足两个独立可靠来源")


def _pack_identity(pack_dir: Path) -> tuple[str | None, str | None]:
    """The immutable identity of a pack: ``(content_sha256, pack_id)``.

    Operation timestamps are deliberately **excluded**.  ``revision.json`` and
    ``run-report.json`` legitimately carry ``generated_at``, and the manifest
    transitively hashes those two files, so the manifest bytes differ between two
    rebuilds of the *same* content.  Comparing manifest bytes would therefore
    turn an identical-content retry into a spurious
    :class:`RevisionCollisionError`; the pack's identity is its content, and
    :func:`validate_episode_pack_stage` separately guarantees the orphan is a
    coherent, self-verifying package.
    """
    pack_dir = Path(pack_dir)
    content: str | None = None
    pack_id: str | None = None
    if (pack_dir / READY_NAME).is_file():
        try:
            ready = _read_json(pack_dir / READY_NAME)
        except (OSError, json.JSONDecodeError):
            ready = {}
        content = ready.get("content_sha256")
        pack_id = ready.get("pack_id")
    return (content, pack_id)


def publish_episode_pack_directory(stage: Path, destination: Path) -> bool:
    """Atomically move ``stage`` to ``destination``; return ``True`` when reused.

    ``os.replace`` is used so the destination is never half-written.  When the
    destination already exists (an orphan from a crashed run), it is reused only
    if the pack identity -- ``content_sha256`` and ``pack_id`` -- matches exactly
    *and* the orphan still validates as a complete package; otherwise the frozen
    ``pack_id`` now names different (or corrupt) content and the run fails with
    :class:`RevisionCollisionError`.
    """
    stage = Path(stage)
    destination = Path(destination)
    _assert_same_volume(stage, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if (
            _pack_identity(stage) == _pack_identity(destination)
            and validate_episode_pack_stage(destination)["status"] == "pass"
        ):
            remove_tree(stage)
            return True
        raise RevisionCollisionError(
            f"pack_id 冲突：{destination} 已存在且内容不同（拒绝覆盖不可变研究包）"
        )
    if not stage.is_dir():
        raise FileNotFoundError(f"研究包 stage 不存在：{stage}")
    os.replace(stage, destination)
    return False


def flip_episode_current_pointer(current_path: Path, pointer: Mapping[str, Any]) -> None:
    """Atomically write ``current.json`` -- the single commit point."""
    payload = dict(pointer)
    payload.setdefault("contract", CONTRACT)
    atomic_write_json(Path(current_path), payload)


def load_current_pointer(current_path: Path) -> dict[str, Any] | None:
    path = Path(current_path)
    if not path.is_file():
        return None
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


# --- Orchestration ----------------------------------------------------------


def _fire(fault_hook: Callable[[str], Any] | None, phase: str) -> None:
    if fault_hook is None:
        return
    outcome = fault_hook(phase)
    if isinstance(outcome, BaseException):
        raise outcome
    if outcome:
        raise RuntimeError(f"fault_hook {phase}: {outcome}")


def _now_iso(config: dict[str, Any] | None = None) -> str:
    timezone = "Asia/Shanghai"
    if config:
        timezone = str(config.get("timezone") or timezone)
    return datetime.now(ZoneInfo(timezone)).isoformat(timespec="seconds")


def _best_effort_latest_notify(
    *,
    episode_root: Path,
    episode_id: str,
    pointer: Mapping[str, Any],
    now: str,
) -> list[str]:
    """Write ``latest.json`` and ``notify/<episode_id>.json``; never raise.

    Both are advisory mirrors of the commit point: the authoritative record is
    ``current.json``, so a failure here is reported as an advisory warning only.
    """
    advisories: list[str] = []
    latest_path = episode_root / LATEST_NAME
    try:
        atomic_write_json(latest_path, dict(pointer))
    except OSError as exc:
        advisories.append(f"latest.json 未写入（best-effort）：{exc}")
    notify_path = episode_root / NOTIFY_DIRNAME / f"{episode_id}.json"
    event_id = f"{pointer.get('pack_id')}"
    try:
        atomic_write_json(notify_path, {
            "contract": CONTRACT,
            "event_id": event_id,
            "event_type": "episode_research_pack_ready",
            "episode_id": episode_id,
            "pack_id": pointer.get("pack_id"),
            "revision": pointer.get("revision"),
            "content_sha256": pointer.get("content_sha256"),
            "current_path": CURRENT_NAME,
            "disposition": pointer.get("disposition"),
            "emitted_at": now,
        })
    except OSError as exc:
        advisories.append(f"notify 未写入（best-effort）：{exc}")
    return advisories


def publish_episode_research_pack(
    config: dict[str, Any],
    *,
    episode_id: str,
    semantic: Mapping[str, Any],
    business_date: str | None = None,
    output_root: str | Path | None = None,
    fault_hook: Callable[[str], Any] | None = None,
    clock: Callable[[], str] | None = None,
    run_report: Mapping[str, Any] | None = None,
    research_builder: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Publish one immutable research pack revision; return a status summary.

    ``status`` is one of ``published`` (new revision), ``reused`` (an identical
    orphan already existed at this ``pack_id``) or ``noop`` (same episode and
    content hash as the current commit; revision does not grow).  Operation
    timestamps are injected via ``clock`` so tests stay deterministic and the
    hashed semantic files never depend on wall-clock time.

    ``research_builder`` is the injection point for a *real* single-topic
    research construction: when it is ``None`` (the default) the seven semantic
    files are exactly ``semantic`` -- byte-for-byte the pre-change behaviour.
    When it is supplied it is called **once**, before anything is hashed or
    written, as::

        research_builder(episode_id=..., business_date=..., semantic=semantic)

    and must return a complete mapping of the seven :data:`SEMANTIC_FILES`
    payloads that replaces ``semantic``.  The builder is a pure function of its
    inputs: it may not read the wall clock or a network socket itself, so
    ``content_sha256`` stays the deterministic function of the seven files that
    :func:`content_sha256` already defines.  See
    :func:`build_research_semantic` / :func:`research_builder_from_discovery`
    for the public-detail adapter shipped with this module.
    """
    now = clock or (lambda: _now_iso(config))
    settings = research_pack_settings(config)
    resolved_root = output_root if output_root is not None else settings["output_root"]

    identity_id, identity_date, disposition, _mode = _episode_identity(semantic)
    episode_id = episode_id or identity_id
    business_date = business_date or identity_date
    if not episode_id:
        raise EpisodePackError("episode_id 缺失")
    if not business_date:
        raise EpisodePackError("business_date 缺失")

    if research_builder is not None:
        semantic = research_builder(
            episode_id=episode_id, business_date=business_date, semantic=semantic,
        )
        disposition = _episode_identity(semantic)[2]

    episode_root = episode_root_for(config, output_root=resolved_root, business_date=business_date, episode_id=episode_id)
    current_path = episode_root / CURRENT_NAME
    current = load_current_pointer(current_path)

    csha = content_sha256(semantic)
    if current is not None and str(current.get("content_sha256") or "") == csha:
        advisories = _best_effort_latest_notify(
            episode_root=episode_root, episode_id=episode_id, pointer=current, now=now(),
        )
        return {
            "status": "noop",
            "reason": "content_unchanged",
            "episode_id": episode_id,
            "pack_id": current.get("pack_id"),
            "revision": current.get("revision"),
            "pack_path": current.get("pack_path"),
            "content_sha256": csha,
            "disposition": current.get("disposition", disposition),
            "advisories": advisories,
            "warnings": [*advisories],
        }

    revision = int(current.get("revision") or 0) + 1 if current else 1
    pack_id = pack_id_for(episode_id, revision)
    supersedes = (
        {
            "pack_id": current.get("pack_id"),
            "revision": current.get("revision"),
            "content_sha256": current.get("content_sha256"),
        }
        if current
        else None
    )

    stage = episode_root / STAGING_DIRNAME / pack_id
    generated_at = now()
    build_episode_pack_stage(
        stage,
        episode_id=episode_id,
        revision=revision,
        semantic=semantic,
        business_date=business_date,
        generated_at=generated_at,
        supersedes=supersedes,
        run_report=run_report,
    )
    _fire(fault_hook, "after_stage_built")

    validation = validate_episode_pack_stage(stage)
    _fire(fault_hook, "after_stage_validated")
    if validation["status"] != "pass":
        raise EpisodePackValidationError(
            "研究包自校验失败：" + "；".join(validation.get("errors") or [])
        )

    destination = episode_root / PACKS_DIRNAME / pack_id
    reused = publish_episode_pack_directory(stage, destination)
    _fire(fault_hook, "after_pack_directory_publish")

    manifest_sha = _sha256_file(destination / MANIFEST_NAME)
    pointer = {
        "contract": CONTRACT,
        "episode_id": episode_id,
        "business_date": business_date,
        "pack_id": pack_id,
        "revision": revision,
        "content_sha256": csha,
        "manifest_sha256": manifest_sha,
        "disposition": disposition,
        "pack_path": f"{PACKS_DIRNAME}/{pack_id}",
        "ready_path": f"{PACKS_DIRNAME}/{pack_id}/{READY_NAME}",
        "updated_at": generated_at,
    }

    # ``before_current_flip`` failing must leave the old current bytes untouched
    # and write neither latest.json nor notify.
    _fire(fault_hook, "before_current_flip")
    flip_episode_current_pointer(current_path, pointer)

    advisories: list[str] = []
    try:
        _fire(fault_hook, "after_current_flip")
    except Exception as exc:  # noqa: BLE001 - a post-commit fault is advisory only
        advisories.append(
            f"current 翻转后注入故障（{exc}）：已视为提交；latest/notify 可能滞后，重试为 noop。"
        )
    advisories.extend(_best_effort_latest_notify(
        episode_root=episode_root, episode_id=episode_id, pointer=pointer, now=now(),
    ))

    return {
        "status": "reused" if reused else "published",
        "episode_id": episode_id,
        "pack_id": pack_id,
        "revision": revision,
        "pack_path": str(destination),
        "content_sha256": csha,
        "manifest_sha256": manifest_sha,
        "disposition": disposition,
        "validation": validation,
        "advisories": advisories,
        "warnings": [*advisories],
    }


# --- Delivery compatibility + convenience ----------------------------------


def annotate_delivery_manifest(manifest: Mapping[str, Any], ref: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of a delivery manifest with ``research_pack_ref`` added.

    This is a **pure** helper for the *delivery's own* publish flow to call
    **before** it writes its manifest/READY -- the research publisher never
    backpatches an already-published delivery.  The caller must still re-run the
    historic validator; :func:`write_delivery_manifest_ref` does that.
    """
    payload = dict(manifest)
    payload["research_pack_ref"] = dict(ref)
    return payload


def write_delivery_manifest_ref(delivery_dir: Path, ref: Mapping[str, Any]) -> str:
    """Explicit opt-in: append ``research_pack_ref`` to a delivery's ``清单.json``.

    Default behaviour for the research publisher is to write **nothing** into
    the delivery; this helper exists only for an operator/explicit call and
    re-validates the modified manifest with the historic validator before
    writing.  Returns ``"manifest"`` on success, ``"skipped"`` otherwise.
    """
    from .replication_delivery import validate_delivery_manifest

    delivery = Path(delivery_dir)
    manifest_path = delivery / DELIVERY_MANIFEST_NAME
    if not manifest_path.is_file():
        return "skipped"
    try:
        payload = _read_json(manifest_path)
    except (OSError, json.JSONDecodeError):
        return "skipped"
    if not isinstance(payload, dict):
        return "skipped"
    annotated = annotate_delivery_manifest(payload, ref)
    with tempfile.TemporaryDirectory() as tmp:
        check_path = Path(tmp) / DELIVERY_MANIFEST_NAME
        atomic_write_json(check_path, annotated)
        if validate_delivery_manifest(check_path)["status"] != "pass":
            return "skipped"
    atomic_write_json(manifest_path, annotated)
    return "manifest"


def publish_from_delivery(
    config: dict[str, Any],
    *,
    delivery_dir: Path,
    theme: str | None = None,
    business_date: str | None = None,
    episode_id: str | None = None,
    research_inputs: Mapping[str, Any] | None = None,
    output_root: str | Path | None = None,
    fault_hook: Callable[[str], Any] | None = None,
    clock: Callable[[], str] | None = None,
    annotate_delivery: bool = False,
    research_builder: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Derive and publish a research pack for one finished delivery.

    The research publisher **never writes into the delivery** by default; it only
    reads it and freezes it into ``origin_ref``.  ``annotate_delivery=True`` is an
    explicit opt-in for an operator who wants ``research_pack_ref`` appended to
    the delivery manifest (validated before writing); the default leaves the
    delivery byte-identical.

    ``research_builder`` is forwarded verbatim to
    :func:`publish_episode_research_pack`; ``None`` (the default) keeps the legacy
    behaviour of publishing the ``semantic`` derived from the delivery.
    """
    delivery = Path(delivery_dir)
    manifest = _read_json(delivery / DELIVERY_MANIFEST_NAME)
    theme = theme or str(manifest.get("theme") or "")
    business_date = business_date or str(manifest.get("business_date") or "")
    episode_id = episode_id or default_episode_id(business_date, theme)

    semantic = derive_semantic_from_delivery(
        delivery, episode_id=episode_id, theme=theme, business_date=business_date,
        research_inputs=research_inputs,
    )
    result = publish_episode_research_pack(
        config, episode_id=episode_id, semantic=semantic, business_date=business_date,
        output_root=output_root, fault_hook=fault_hook, clock=clock,
        research_builder=research_builder,
    )
    ref = {
        "contract": CONTRACT,
        "episode_id": result.get("episode_id"),
        "pack_id": result.get("pack_id"),
        "revision": result.get("revision"),
        "content_sha256": result.get("content_sha256"),
        "pack_path": result.get("pack_path"),
    }
    if annotate_delivery:
        try:
            result["delivery_ref_mode"] = write_delivery_manifest_ref(delivery, ref)
        except OSError as exc:
            result.setdefault("advisories", []).append(f"research_pack_ref 未写入旧交付：{exc}")
    else:
        result["delivery_ref_mode"] = "none"
    result["research_pack_ref"] = ref
    return result


# --- Public-detail research builder (the ``research_builder`` adapter) ------

#: Bundled defaults for the Google News RSS detail channel.  Kept here (and not
#: hard-coded inside :class:`PublicDetailDiscovery`) so an operator can override
#: any key through ``detail_settings``.
DEFAULT_PUBLIC_DETAIL_SETTINGS: dict[str, Any] = {
    "max_queries": 2,
    "max_total_bytes": 262_144,
    "max_response_bytes": 262_144,
    "request_timeout_seconds": 10.0,
    "freshness_days": 3,
    "freshness_grace_hours": 12,
    "max_results_per_query": 5,
    "min_detail_chars": 40,
}


def _public_detail_source_id(text: str) -> str:
    """Stable source id: a pure function of the evidence text (never a clock)."""
    return "pub-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def build_research_semantic(
    *,
    episode_id: str,
    business_date: str,
    semantic: Mapping[str, Any],
    story: Mapping[str, Any],
    detail_provider: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    detail_settings: Mapping[str, Any] | None = None,
    timezone: str = "Asia/Shanghai",
    deadline_seconds: float = 20.0,
) -> dict[str, Any]:
    """Default ``research_builder``: enrich a pack semantic with public detail.

    Signature matches the ``research_builder`` contract of
    :func:`publish_episode_research_pack` (keyword-only ``episode_id`` /
    ``business_date`` / ``semantic``) plus the research-specific inputs, so bind
    ``story`` and the optional provider first with
    :func:`research_builder_from_discovery`.

    Behaviour, in order:

    1. ``detail_provider`` (a :class:`~.public_detail_discovery.PublicDetailDiscovery`
       instance or any callable ``story -> {"evidence": [...], "audit": {...}}``)
       is called once with ``story``.  When it is ``None`` a real
       ``PublicDetailDiscovery`` is constructed from
       :data:`DEFAULT_PUBLIC_DETAIL_SETTINGS` merged with ``detail_settings`` --
       that is the **only** path that touches the network.
    2. The story title is prepended as a ``story_title`` evidence row (the same
       deterministic intent row ``story_enrichment`` uses), then
       :func:`~.news_semantics.deterministic_semantics` derives the event slots.
    3. ``sources.json`` is rebuilt from the evidence (one deterministic
       ``pub-<sha16>`` row each, ``authority="unknown"``,
       ``verification_state="unverified"``) and ``claims.json`` carries at most
       one honest ``unverified`` / ``hedge`` claim; ``episode.json``,
       ``audience.json``, ``topics.json``, ``materials.json`` and ``rights.json``
       are passed through from the baseline ``semantic``.

    Determinism: every produced byte is a pure function of ``semantic``,
    ``story``, the provider's evidence text and ``business_date`` -- no wall
    clock, no ``generated_at``, so ``content_sha256`` remains stable across
    retries.

    Frozen-contract limitation (why a delivery-derived baseline is required):
    ``episode-research-pack-v1`` still pins
    ``episode.origin_ref.origin_contract == "material_replication_delivery"``
    with a real ``manifest_sha256``, so this builder cannot invent a
    delivery-free pack: it raises :class:`EpisodePackError` unless the baseline
    ``semantic`` already carries such an ``origin_ref``.  Lifting that needs a
    contract revision, not a builder tweak.
    """
    from .news_semantics import deterministic_semantics

    missing = [name for name in SEMANTIC_FILES if not isinstance(semantic.get(name), Mapping)]
    if missing:
        raise EpisodePackError(f"research_builder 基线 semantic 缺少语义文件：{missing}")
    episode = dict(semantic["episode.json"])
    origin = episode.get("origin_ref") or {}
    if origin.get("origin_contract") != "material_replication_delivery" or not origin.get("manifest_sha256"):
        raise EpisodePackError(
            "research_builder 需要基线 semantic 携带 material_replication_delivery 的 origin_ref"
            "（episode-research-pack-v1 尚无 delivery-free 来源）"
        )

    provider = detail_provider
    if provider is None:
        from .public_detail_discovery import PublicDetailDiscovery

        provider = PublicDetailDiscovery(
            {**DEFAULT_PUBLIC_DETAIL_SETTINGS, **dict(detail_settings or {})},
            deadline=time.monotonic() + float(deadline_seconds),
            business_date=business_date,
            timezone=timezone,
        )
    story = dict(story)
    result = provider(dict(story))
    evidence = [
        dict(item) for item in ((result or {}).get("evidence") or [])
        if isinstance(item, Mapping) and str(item.get("text") or "").strip()
    ]
    title = str(story.get("title") or story.get("canonical_title") or "").strip()
    if title and not any(str(item.get("text") or "").strip() == title for item in evidence):
        evidence.insert(0, {
            "video_id": "story-title", "author": "",
            "selection_reason": "story_title_context", "method": "story_title",
            "status": "partial", "text": title,
        })

    semantics = deterministic_semantics(story, evidence)
    observed_at = f"{business_date}T00:00:00+08:00"
    sources: list[dict[str, Any]] = []
    source_ids: list[str] = []
    for item in evidence:
        text = str(item.get("text") or "").strip()
        source_id = _public_detail_source_id(text)
        if source_id in source_ids:
            continue
        source_ids.append(source_id)
        sources.append({
            "source_id": source_id,
            "authority": "unknown",
            "verification_state": "unverified",
            "heat_only": False,
            "publisher": str(item.get("author") or ""),
            "method": str(item.get("method") or "unavailable"),
            "excerpt": text[:400],
            "freshness": {
                "observed_at": observed_at,
                "policy": "event_window",
                "status_at_publish": "unknown",
            },
        })

    slots = semantics.get("event_slots") or {}
    claim_text = "".join(str(slots.get(key) or "") for key in ("subject", "action", "object")) or title
    claims: list[dict[str, Any]] = []
    if claim_text and source_ids:
        claims.append({
            "claim_id": "claim-01",
            "text": claim_text,
            "evidence_status": "unverified",
            "wording_policy": "hedge",
            "freshness_requirement": "fresh",
            "fact_sources_present": 0,
            "fact_sources_min": 0,
            "source_ids": list(source_ids),
            "material_refs": [],
            "claims_to_verify": list(source_ids),
            "do_not_claim": ["未经核验的具体数字", "官方未确认的结论"],
        })

    identity = {"contract": CONTRACT, "episode_id": episode_id, "business_date": business_date}
    return {
        "episode.json": episode,
        "sources.json": {**identity, "sources": sources},
        "claims.json": {**identity, "claims": claims},
        "audience.json": dict(semantic["audience.json"]),
        "topics.json": dict(semantic["topics.json"]),
        "materials.json": dict(semantic["materials.json"]),
        "rights.json": dict(semantic["rights.json"]),
    }


def research_builder_from_discovery(
    story: Mapping[str, Any],
    *,
    detail_provider: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    detail_settings: Mapping[str, Any] | None = None,
    timezone: str = "Asia/Shanghai",
    deadline_seconds: float = 20.0,
) -> Callable[..., Mapping[str, Any]]:
    """Bind one ``story`` into a ``research_builder`` for
    :func:`publish_episode_research_pack`; inject ``detail_provider`` to keep a
    run fully offline and deterministic."""
    payload = dict(story)

    def builder(*, episode_id: str, business_date: str, semantic: Mapping[str, Any]) -> Mapping[str, Any]:
        return build_research_semantic(
            episode_id=episode_id, business_date=business_date, semantic=semantic,
            story=payload, detail_provider=detail_provider, detail_settings=detail_settings,
            timezone=timezone, deadline_seconds=deadline_seconds,
        )

    return builder


# --- Deterministic fixture builder (producer-side golden generator) ---------

#: Fixed synthetic source video id used by :func:`build_fixture_pack`.
FIXTURE_VIDEO_ID = "fixture0001"


def _fixture_generated_at(business_date: str) -> str:
    """The fixed control-file timestamp: ``<business_date>T10:00:00+08:00``.

    It is a pure function of the inputs (never wall-clock time) so a rebuilt
    fixture stamps byte-identical ``revision.json`` / ``run-report.json`` /
    ``_READY.json``.
    """
    return f"{business_date}T10:00:00+08:00"


def _delivery_folder_name(business_date: str, theme: str) -> str:
    """``9.16苹果折叠屏手机复刻视频`` -- the historic delivery folder stem."""
    parts = str(business_date or "").split("-")
    month, day = (parts[1], parts[2]) if len(parts) >= 3 else ("0", "0")
    try:
        stamp = f"{int(month)}.{int(day)}"
    except ValueError:
        stamp = f"{month}.{day}"
    return f"{stamp}{sanitize_theme(theme, max_length=40)}复刻视频"


def _build_fixture_delivery(base: Path, *, business_date: str, theme: str, video_id: str) -> Path:
    """Create a deterministic, structurally faithful ``material-replication`` delivery.

    Mirrors the real delivery tree (``02-主素材`` / ``03-辅助素材`` / ``04-原片`` /
    ``05-过程数据`` plus ``清单.json``) with fixed bytes and fixed metadata, so
    ``origin_ref`` freezes the same hashes on every run.
    """
    delivery = Path(base) / _delivery_folder_name(business_date, theme)
    for folder in (FOLDER_MAIN, FOLDER_SUPPORT, FOLDER_SOURCE, FOLDER_PROCESS):
        (delivery / folder).mkdir(parents=True, exist_ok=True)
    (delivery / FOLDER_SOURCE / f"作者_作品_{video_id}.mp4").write_bytes(b"source-bytes")
    (delivery / FOLDER_MAIN / "main-01.mp4").write_bytes(b"clip-bytes")
    atomic_write_json(delivery / FOLDER_MAIN / "main-01.json", {
        "schema_version": 1, "clip_id": "main-01", "role": "main",
        "file": f"{FOLDER_MAIN}/main-01.mp4",
        "source": {"video_id": video_id, "author": "作者", "source_url": ""},
        "timecode": {"start": 3.0, "end": 11.0, "duration": 8.0},
        "media": {"width": 1080, "height": 1920, "fps": 30, "has_audio": True},
        "face": {"face_class": "face_free"}, "suggested_use": "hook", "warnings": [],
    })
    manifest = build_manifest(
        theme=theme, folder=delivery.name, business_date=business_date,
        generated_at=_fixture_generated_at(business_date),
        keywords_used=[theme], candidate_pool_size=1,
        script_replica={"status": "not_found"},
        material_replica_sources=[{
            "video_id": video_id, "author": "作者", "title": "标题",
            "face_class": "face_free", "bytes": 12, "source": "douyin",
            "published_at": _fixture_generated_at(business_date),
        }],
        main_materials=[{
            "clip_id": "main-01", "file": f"{FOLDER_MAIN}/main-01.mp4", "duration": 8.0,
            "face_class": "face_free", "suggested_use": "hook",
        }],
        supporting_materials=[],
        counters={"candidates": 1, "downloaded": 1},
        face_backend="fixture", face_backend_status="ok", ffmpeg_status="ok",
        degraded=False, insufficient=False,
    )
    manifest["material_replica"] = {"status": "done", "conclusion": "ok", "pool_size": 1, "selected": 1}
    atomic_write_json(delivery / DELIVERY_MANIFEST_NAME, manifest)
    return delivery


def _fixture_audience(revision: int) -> dict[str, Any]:
    """Deterministic audience payload; distinct per revision (r>=2) to bump content."""
    return {"status": "fixture", "summary": f"fixture revision r{revision}", "segments": []}


def _fixture_inputs(base: Mapping[str, Any], revision: int) -> dict[str, Any]:
    inputs = dict(base)
    if revision > 1:
        inputs["audience"] = _fixture_audience(revision)
    return inputs


def build_fixture_pack(
    output_root: str | Path,
    *,
    business_date: str,
    theme: str,
    episode_id: str | None = None,
    disposition: str = "research_required",
    claims: list[dict[str, Any]] | None = None,
    materials: list[dict[str, Any]] | None = None,
    revision: int = 1,
) -> Path:
    """Deterministically produce a golden ``episode-research-pack-v1`` fixture.

    ``output_root`` is the research-pack root (identical meaning to the
    ``jobs.material_replication.episode_research_pack.output_root`` setting): the
    returned episode root lives at
    ``<output_root>/<business_date>_研究包/<episode_id>/`` and contains
    ``current.json``.  A faithful synthetic ``material-replication`` delivery is
    created in a throwaway directory -- its bytes are frozen into the pack's
    ``origin_ref`` but the delivery itself is **not** part of the pack contract
    and is cleaned up afterwards.

    The function drives the **real** production chain --
    :func:`publish_from_delivery` -> :func:`publish_episode_research_pack` ->
    :func:`build_episode_pack_stage` -> :func:`validate_episode_pack_stage` --
    with a fixed clock, so identical inputs yield an identical ``content_sha256``
    and byte-identical semantic files.  ``revision`` selects the target revision:
    ``1`` (default) is a first publish; ``N > 1`` publishes a deterministic chain
    so a consumer can exercise supersession.

    The call is idempotent: when the episode root already holds the requested
    revision with the expected content it returns that root **without any write**;
    when the root exists but does not match it is removed and rebuilt.  A fixture
    generator must therefore be pointed at an isolated ``output_root``, never at
    a live research-pack root.
    """
    target_revision = int(revision)
    if target_revision < 1:
        raise EpisodePackError(f"revision 必须 >= 1：{revision}")
    if not business_date:
        raise EpisodePackError("business_date 缺失")
    if not theme:
        raise EpisodePackError("theme 缺失")

    root = Path(output_root)
    # ``_project_root`` = cwd so a relative ``output_root`` resolves predictably
    # from wherever the generator is invoked; an absolute root is used verbatim.
    config = {"_project_root": str(Path.cwd()), "timezone": "Asia/Shanghai"}
    eid = episode_id or default_episode_id(business_date, theme)
    episode_root = episode_root_for(config, output_root=root, business_date=business_date, episode_id=eid)

    base_inputs: dict[str, Any] = {"disposition": disposition}
    if claims is not None:
        base_inputs["claims"] = [dict(item) for item in claims]
    if materials is not None:
        base_inputs["materials"] = [dict(item) for item in materials]

    def clock() -> str:
        return _fixture_generated_at(business_date)

    with tempfile.TemporaryDirectory(prefix="ep-pack-fixture-") as tmp:
        delivery = _build_fixture_delivery(
            Path(tmp), business_date=business_date, theme=theme, video_id=FIXTURE_VIDEO_ID,
        )

        expected = derive_semantic_from_delivery(
            delivery, episode_id=eid, theme=theme, business_date=business_date,
            research_inputs=_fixture_inputs(base_inputs, target_revision),
        )
        current = load_current_pointer(episode_root / CURRENT_NAME)
        if (
            current
            and int(current.get("revision") or 0) == target_revision
            and str(current.get("content_sha256") or "") == content_sha256(expected)
        ):
            return episode_root

        if episode_root.exists():
            remove_tree(episode_root)

        for rev in range(1, target_revision + 1):
            publish_from_delivery(
                config, delivery_dir=delivery, theme=theme, business_date=business_date,
                episode_id=eid, research_inputs=_fixture_inputs(base_inputs, rev),
                output_root=root, clock=clock,
            )
    return episode_root


def inspect_episode_pack(config: dict[str, Any], *, business_date: str, episode_id: str, output_root: str | Path | None = None) -> dict[str, Any]:
    """Read-only inspection of an episode's current committed pack (offline)."""
    settings = research_pack_settings(config)
    root = output_root if output_root is not None else settings["output_root"]
    episode_root = episode_root_for(config, output_root=root, business_date=business_date, episode_id=episode_id)
    current = load_current_pointer(episode_root / CURRENT_NAME)
    if not current:
        return {"status": "missing", "episode_root": str(episode_root), "current": None}
    pack_dir = episode_root / str(current.get("pack_path") or "")
    validation = validate_episode_pack_stage(pack_dir) if pack_dir.is_dir() else {"status": "fail", "errors": ["pack 目录不存在"]}
    return {
        "status": validation["status"],
        "episode_root": str(episode_root),
        "current": current,
        "pack_path": str(pack_dir),
        "validation": validation,
    }
