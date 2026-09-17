"""Offline acceptance tests for ``episode-research-pack-v1`` (contract v2).

Every test is fully offline and uses ``tmp_path``: no network, no model, no
subprocess, no real project ``runs``.  The delivery fixtures are synthetic but
structurally faithful to a real ``material-replication`` delivery.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from douyin_intelligence.config import load_config
from douyin_intelligence.episode_research_pack import (
    CONTRACT,
    CURRENT_NAME,
    FIXTURE_BUSINESS_DATE,
    FIXTURE_THEME,
    FROZEN_CONTENT_SHA256,
    FROZEN_FIXTURE_CONTENT_SHA256_BY_REVISION,
    FROZEN_FIXTURE_CONTENT_SHA256_R1,
    FROZEN_FIXTURE_CONTENT_SHA256_R2,
    LATEST_NAME,
    MANIFEST_NAME,
    MANIFEST_SCHEMA,
    PRODUCTION_MODE,
    READY_NAME,
    REVISION_NAME,
    SEMANTIC_FILES,
    CrossVolumeError,
    EpisodePackError,
    RevisionCollisionError,
    UnsafePathError,
    _framed_bytes,
    _pack_identity,
    _same_volume,
    build_episode_pack_stage,
    build_fixture_pack,
    canonical_json,
    content_sha256,
    content_sha256_test_vector,
    default_episode_id,
    publish_episode_pack_directory,
    publish_from_delivery,
    safe_relative_path,
    validate_episode_pack_stage,
)
from douyin_intelligence.exporter import atomic_write_json
from douyin_intelligence.replication_delivery import build_manifest, validate_delivery_manifest

_V1 = "777"
_THEME = "苹果折叠屏手机"
_DATE = "2026-09-16"


# --- Fixtures ---------------------------------------------------------------


def _config(tmp_path: Path) -> dict:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    return config


def _delivery(tmp_path: Path, *, video_id: str = _V1, extra_clip: bool = False) -> Path:
    """Build a synthetic but structurally real material-replication delivery."""
    delivery = tmp_path / f"9.16{_THEME}复刻视频"
    for folder in ("02-主素材", "03-辅助素材", "04-原片", "05-过程数据"):
        (delivery / folder).mkdir(parents=True, exist_ok=True)
    (delivery / "04-原片" / f"作者_作品_{video_id}.mp4").write_bytes(b"source-bytes")
    (delivery / "02-主素材" / "main-01.mp4").write_bytes(b"clip-bytes")
    atomic_write_json(delivery / "02-主素材" / "main-01.json", {
        "schema_version": 1, "clip_id": "main-01", "role": "main",
        "file": "02-主素材/main-01.mp4",
        "source": {"video_id": video_id, "author": "作者", "source_url": ""},
        "timecode": {"start": 3.0, "end": 11.0, "duration": 8.0},
        "media": {"width": 1080, "height": 1920, "fps": 30, "has_audio": True},
        "face": {"face_class": "face_free"}, "suggested_use": "hook", "warnings": [],
    })
    if extra_clip:
        (delivery / "02-主素材" / "main-02.mp4").write_bytes(b"clip-bytes-2")
        atomic_write_json(delivery / "02-主素材" / "main-02.json", {
            "schema_version": 1, "clip_id": "main-02", "role": "main",
            "file": "02-主素材/main-02.mp4",
            "source": {"video_id": video_id, "author": "作者", "source_url": ""},
            "timecode": {"start": 30.0, "end": 38.0, "duration": 8.0},
            "media": {"width": 1080, "height": 1920, "fps": 30, "has_audio": True},
            "face": {"face_class": "face_free"}, "suggested_use": "key_points[1]", "warnings": [],
        })
    manifest = build_manifest(
        theme=_THEME, folder=delivery.name, business_date=_DATE, generated_at="2026-09-16T10:00:00+08:00",
        keywords_used=["苹果折叠屏"], candidate_pool_size=12,
        script_replica={"status": "not_found"},
        material_replica_sources=[{
            "video_id": video_id, "author": "作者", "title": "标题",
            "face_class": "face_free", "bytes": 11, "published_at": "2026-09-15T00:00:00+08:00",
            "source": "douyin",
        }],
        main_materials=[{
            "clip_id": "main-01", "file": "02-主素材/main-01.mp4", "duration": 8.0,
            "face_class": "face_free", "suggested_use": "hook",
        }],
        supporting_materials=[],
        counters={"candidates": 12, "downloaded": 1},
        face_backend="opencv_yunet", face_backend_status="ok", ffmpeg_status="ok",
        degraded=False, insufficient=False,
    )
    manifest["material_replica"] = {"status": "done", "conclusion": "ok", "pool_size": 12, "selected": 1}
    atomic_write_json(delivery / "清单.json", manifest)
    return delivery


def _episode_roots(tmp_path: Path) -> Path:
    return tmp_path / "output" / "每期研究包" / f"{_DATE}_研究包"


def _identity(episode_id: str = "ep-demo") -> dict:
    return {"contract": CONTRACT, "episode_id": episode_id, "business_date": _DATE}


def _source(source_id="src1", *, authority="official", verification_state="verified", publisher="p", heat_only=False):
    return {
        "source_id": source_id, "authority": authority, "verification_state": verification_state,
        "heat_only": heat_only, "title": "t", "url": "https://x", "publisher": publisher,
        "published_at": "2026-09-15T00:00:00+08:00",
        "freshness": {
            "observed_at": "2026-09-15T00:00:00+08:00", "valid_until": None,
            "policy": "event_window", "status_at_publish": "fresh",
        },
    }


_WORDING = {
    "confirmed_official": "assert",
    "confirmed_two_reliable": "assert",
    "creator_primary": "attribute",
    "unverified": "hedge",
    "conflicting": "prohibit",
    "insufficient": "prohibit",
}
_EXPECTED_MIN = {"confirmed_official": 1, "confirmed_two_reliable": 2}


def _claim(claim_id="c1", *, evidence_status="unverified", material_refs=None,
           source_ids=None, text="x", fact_sources_present=None):
    refs = list(source_ids or [])
    return {
        "claim_id": claim_id, "topic_id": "topic-01", "text": text,
        "evidence_status": evidence_status, "wording_policy": _WORDING[evidence_status],
        "freshness_requirement": "fresh",
        "source_ids": refs,
        "fact_sources_min": _EXPECTED_MIN.get(evidence_status, 0),
        "fact_sources_present": len(refs) if fact_sources_present is None else fact_sources_present,
        "claims_to_verify": [], "do_not_claim": [], "material_refs": list(material_refs or []),
    }


def _material(*, material_id="m01-777", duration_ms=20000, claim_ids=None, segments=None):
    if segments is None:
        segments = [{
            "segment_id": "main-01", "start_ms": 3000, "end_ms": 11000,
            "claim_ids": list(claim_ids or []), "purpose": "visual_support",
            "transcript_excerpt": "", "frame_evidence_ids": [],
        }]
    return {
        "material_id": material_id, "kind": "b_roll",
        "origin": {"video_id": _V1, "author": "作者", "title": "标题", "share_url": ""},
        "permitted_use": "b_roll_only", "freshness_status": "unknown",
        "duration_ms": duration_ms, "segments": segments,
    }


def _rights(*, material_id="m01-777"):
    return {
        "asset_id": f"asset-{material_id}", "asset_type": "video", "origin": "platform_content",
        "rights_status": "review_required", "license": None, "attribution": None,
        "redistribution_allowed": False, "render_eligible": False,
        "review_reason": "review", "material_id": material_id,
    }


def _topics(*, nodes=None, edges=None):
    return {
        "keyword_graph": {
            "seed": _THEME, "expanded": [], "subject_terms": [], "event_terms": [],
            "keywords_requested": [], "keywords_used": [], "keywords_truncated": False,
        },
        "topic_candidates": [{
            "topic_id": "topic-01", "title": _THEME, "keywords": [],
            "selection_basis": "single_theme", "producer_proposal": True,
        }],
        "selected_topic": {"topic_id": "topic-01", "selection_basis": "single_theme", "producer_proposal": True},
        "argument_graph": {"topic_id": "topic-01", "nodes": list(nodes or []), "edges": list(edges or [])},
    }


def _base_semantic(
    *,
    episode_id: str = "ep-demo",
    disposition: str = "partial",
    claims: list | None = None,
    sources: list | None = None,
    topics: dict | None = None,
    audience: dict | None = None,
    materials: list | None = None,
    rights: list | None = None,
) -> dict:
    identity = _identity(episode_id)
    episode = {
        **identity,
        "production_mode": PRODUCTION_MODE,
        "theme": _THEME,
        "disposition": disposition,
        "keywords": ["苹果折叠屏"],
        "origin_ref": {
            "origin_contract": "material_replication_delivery", "origin_pack_id": None,
            "origin_item_id": None, "delivery_folder": "d", "manifest_path": "清单.json",
            "manifest_sha256": "0" * 64, "asset_path": None, "asset_sha256": None, "asset_bytes": None,
            "authority": "material_only", "discovery_only": True, "items": [], "assets": [],
        },
        "warnings": [],
    }
    return {
        "episode.json": episode,
        "sources.json": {**identity, "sources": list(sources or [])},
        "claims.json": {**identity, "claims": list(claims or [])},
        "audience.json": {**identity, "audience": audience or {"status": "unknown", "summary": "", "segments": []}},
        "topics.json": {**identity, **(topics or _topics())},
        "materials.json": {**identity, "materials": list(materials if materials is not None else [_material()])},
        "rights.json": {**identity, "rights": list(rights if rights is not None else [_rights()])},
    }


def _publish(config: dict, delivery: Path, *, episode_id: str | None = None, fault_hook=None, clock=None,
             annotate_delivery: bool = False) -> dict:
    return publish_from_delivery(
        config, delivery_dir=delivery, theme=_THEME, business_date=_DATE,
        episode_id=episode_id, fault_hook=fault_hook, clock=clock, annotate_delivery=annotate_delivery,
    )


def _fault_at(phase: str):
    def hook(current: str):
        if current == phase:
            raise RuntimeError(f"fault@{current}")
    return hook


# --- Tests ------------------------------------------------------------------


def test_first_publish_creates_r1_and_commits(tmp_path: Path) -> None:
    config = _config(tmp_path)
    delivery = _delivery(tmp_path)
    result = _publish(config, delivery)

    assert result["status"] == "published"
    assert result["revision"] == 1
    assert result["pack_id"].endswith("-r1")
    assert result["disposition"] == "research_required"  # no first-hand facts

    episode_root = _episode_roots(tmp_path) / result["episode_id"]
    pack_path = episode_root / "packs" / result["pack_id"]
    for name in (*SEMANTIC_FILES, REVISION_NAME, "run-report.json", "每期研究证据包.md", MANIFEST_NAME, READY_NAME):
        assert (pack_path / name).is_file(), name
    assert not (episode_root / ".staging" / result["pack_id"]).exists()
    assert validate_episode_pack_stage(pack_path)["status"] == "pass"

    current = json.loads((episode_root / CURRENT_NAME).read_text(encoding="utf-8"))
    assert current["contract"] == CONTRACT
    assert current["pack_id"] == result["pack_id"]
    assert current["revision"] == 1
    assert current["pack_path"] == f"packs/{result['pack_id']}"
    assert current["ready_path"] == f"packs/{result['pack_id']}/{READY_NAME}"

    manifest = json.loads((pack_path / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["schema"] == MANIFEST_SCHEMA
    ready = json.loads((pack_path / READY_NAME).read_text(encoding="utf-8"))
    assert ready["contract"] == CONTRACT
    assert "supersedes" in ready and ready["ready_at"]
    assert (episode_root / LATEST_NAME).is_file()
    assert (episode_root / "notify" / f"{result['episode_id']}.json").is_file()


def test_derived_pack_has_no_first_hand_facts(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = _publish(config, _delivery(tmp_path))
    pack_path = _episode_roots(tmp_path) / result["episode_id"] / "packs" / result["pack_id"]
    payloads = {name: json.loads((pack_path / name).read_text(encoding="utf-8")) for name in SEMANTIC_FILES}
    assert payloads["sources.json"]["sources"] == []
    assert payloads["claims.json"]["claims"] == []
    material = payloads["materials.json"]["materials"][0]
    assert material["permitted_use"] == "b_roll_only"
    assert material["duration_ms"] == 11000
    assert material["segments"][0]["start_ms"] == 3000
    assert payloads["rights.json"]["rights"][0]["rights_status"] == "review_required"
    assert payloads["rights.json"]["rights"][0]["render_eligible"] is False
    assert payloads["episode.json"]["origin_ref"]["authority"] == "material_only"
    assert payloads["episode.json"]["origin_ref"]["discovery_only"] is True
    assert payloads["topics.json"]["selected_topic"]["topic_id"] == "topic-01"


def test_same_content_is_noop_and_revision_does_not_grow(tmp_path: Path) -> None:
    config = _config(tmp_path)
    delivery = _delivery(tmp_path)
    first = _publish(config, delivery)
    second = _publish(config, delivery)

    assert second["status"] == "noop"
    assert second["revision"] == 1
    assert second["pack_id"] == first["pack_id"]
    episode_root = _episode_roots(tmp_path) / first["episode_id"]
    assert sorted(p.name for p in (episode_root / "packs").iterdir()) == [first["pack_id"]]


def test_content_change_bumps_revision_exactly_one(tmp_path: Path) -> None:
    config = _config(tmp_path)
    delivery = _delivery(tmp_path)
    first = _publish(config, delivery)
    _delivery(tmp_path, extra_clip=True)  # mutates the same delivery dir
    second = _publish(config, delivery, episode_id=first["episode_id"])

    assert second["status"] == "published"
    assert second["revision"] == 2
    assert second["pack_id"].endswith("-r2")
    assert second["content_sha256"] != first["content_sha256"]
    revision = json.loads(
        (_episode_roots(tmp_path) / first["episode_id"] / "packs" / second["pack_id"] / REVISION_NAME)
        .read_text(encoding="utf-8")
    )
    assert revision["supersedes"]["pack_id"] == first["pack_id"]
    assert revision["change"] == "content_changed"


def test_content_sha256_is_framed_time_free_and_order_independent() -> None:
    vector = content_sha256_test_vector()
    assert content_sha256(vector["payloads"]) == vector["content_sha256"]
    assert len(vector["content_sha256"]) == 64 and int(vector["content_sha256"], 16) >= 0

    left = _base_semantic(episode_id="x", disposition="research_required")
    reversed_payload = {name: left[name] for name in reversed(list(left))}
    assert content_sha256(left) == content_sha256(reversed_payload)
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})

    # Framing: same total bytes but different file boundaries must not collide.
    framed_a = _framed_bytes("episode.json", {"k": "1"}) + _framed_bytes("sources.json", {"k": "23"})
    framed_b = _framed_bytes("episode.json", {"k": "12"}) + _framed_bytes("sources.json", {"k": "3"})
    assert hashlib.sha256(framed_a).hexdigest() != hashlib.sha256(framed_b).hexdigest()
    for name in SEMANTIC_FILES:
        assert "generated_at" not in left[name]
        assert "updated_at" not in left[name]


def test_manifest_is_bidirectional_with_correct_hashes(tmp_path: Path) -> None:
    stage = build_episode_pack_stage(
        tmp_path / "stage", episode_id="ep-demo", revision=1,
        semantic=_base_semantic(), generated_at="2026-09-16T10:00:00+08:00",
    )
    assert validate_episode_pack_stage(stage)["status"] == "pass"
    manifest = json.loads((stage / MANIFEST_NAME).read_text(encoding="utf-8"))
    listed = {entry["path"] for entry in manifest["files"]}
    on_disk = {p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file()}
    assert listed == on_disk - {MANIFEST_NAME, READY_NAME}

    manifest["files"][0]["bytes"] += 1
    atomic_write_json(stage / MANIFEST_NAME, manifest)
    result = validate_episode_pack_stage(stage)
    assert result["status"] == "fail"
    assert any("字节数不符" in err or "SHA256" in err for err in result["errors"])


def test_manifest_rejects_unsafe_and_uncovered_paths(tmp_path: Path) -> None:
    stage = build_episode_pack_stage(
        tmp_path / "stage", episode_id="ep-demo", revision=1, semantic=_base_semantic(), generated_at="t",
    )
    manifest = json.loads((stage / MANIFEST_NAME).read_text(encoding="utf-8"))
    manifest["files"].append({"path": "../evil", "bytes": 1, "sha256": "x"})
    atomic_write_json(stage / MANIFEST_NAME, manifest)
    result = validate_episode_pack_stage(stage)
    assert result["status"] == "fail"
    assert any("路径不安全" in err or "未覆盖" in err or "不存在" in err for err in result["errors"])

    for bad in ("/abs", "C:/abs", "\\\\server\\share", "a/../b", ""):
        with pytest.raises(UnsafePathError):
            safe_relative_path(bad)


def test_before_current_flip_failure_keeps_old_current(tmp_path: Path) -> None:
    config = _config(tmp_path)
    delivery = _delivery(tmp_path)
    first = _publish(config, delivery)
    episode_root = _episode_roots(tmp_path) / first["episode_id"]
    current_before = (episode_root / CURRENT_NAME).read_bytes()
    latest_before = (episode_root / LATEST_NAME).read_bytes()
    notify_before = (episode_root / "notify" / f"{first['episode_id']}.json").read_bytes()

    _delivery(tmp_path, extra_clip=True)
    with pytest.raises(RuntimeError):
        _publish(config, delivery, episode_id=first["episode_id"], fault_hook=_fault_at("before_current_flip"))

    assert (episode_root / CURRENT_NAME).read_bytes() == current_before
    assert (episode_root / LATEST_NAME).read_bytes() == latest_before
    assert (episode_root / "notify" / f"{first['episode_id']}.json").read_bytes() == notify_before


def test_after_current_flip_failure_is_committed_and_retry_noops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    delivery = _delivery(tmp_path)
    result = _publish(config, delivery, fault_hook=_fault_at("after_current_flip"))

    assert result["status"] == "published"
    assert result["advisories"], "post-commit fault must be surfaced as an advisory"
    episode_root = _episode_roots(tmp_path) / result["episode_id"]
    current = json.loads((episode_root / CURRENT_NAME).read_text(encoding="utf-8"))
    assert current["pack_id"] == result["pack_id"]

    retry = _publish(config, delivery, episode_id=result["episode_id"])
    assert retry["status"] == "noop"
    assert retry["revision"] == result["revision"]


def test_orphan_with_identical_content_is_reused(tmp_path: Path) -> None:
    config = _config(tmp_path)
    delivery = _delivery(tmp_path)
    first = _publish(config, delivery)
    episode_root = _episode_roots(tmp_path) / first["episode_id"]
    (episode_root / CURRENT_NAME).unlink()
    (episode_root / LATEST_NAME).unlink()
    (episode_root / "notify" / f"{first['episode_id']}.json").unlink()

    second = _publish(config, delivery, episode_id=first["episode_id"])
    assert second["status"] == "reused"
    assert second["revision"] == 1
    assert second["pack_id"] == first["pack_id"]
    assert validate_episode_pack_stage(episode_root / "packs" / first["pack_id"])["status"] == "pass"


def test_pack_identity_ignores_operation_timestamps(tmp_path: Path) -> None:
    # revision.json / run-report.json officially carry operation timestamps, and
    # the manifest transitively hashes them -- so two rebuilds of the *same*
    # content produce *different* manifest bytes.  The pack identity must stay
    # content-based, otherwise an identical-content retry spuriously collides.
    first = build_episode_pack_stage(
        tmp_path / "a", episode_id="ep-demo", revision=1,
        semantic=_base_semantic(), generated_at="2026-09-16T10:00:00+08:00",
    )
    second = build_episode_pack_stage(
        tmp_path / "b", episode_id="ep-demo", revision=1,
        semantic=_base_semantic(), generated_at="2026-09-16T10:00:05+08:00",
    )
    assert (first / REVISION_NAME).read_bytes() != (second / REVISION_NAME).read_bytes()
    assert (first / MANIFEST_NAME).read_bytes() != (second / MANIFEST_NAME).read_bytes()
    assert _pack_identity(first) == _pack_identity(second)


def test_orphan_reuse_tolerates_different_operation_timestamp(tmp_path: Path) -> None:
    config = _config(tmp_path)
    delivery = _delivery(tmp_path)
    first = _publish(config, delivery, clock=lambda: "2026-09-16T10:00:00+08:00")
    episode_root = _episode_roots(tmp_path) / first["episode_id"]
    (episode_root / CURRENT_NAME).unlink()
    (episode_root / LATEST_NAME).unlink()
    (episode_root / "notify" / f"{first['episode_id']}.json").unlink()

    # Same content, a later operation time: the orphan must be reused, not collide.
    second = _publish(
        config, delivery, episode_id=first["episode_id"],
        clock=lambda: "2026-09-16T11:22:33+08:00",
    )
    assert second["status"] == "reused"
    assert second["revision"] == 1
    assert second["pack_id"] == first["pack_id"]


def test_revision_collision_when_orphan_differs(tmp_path: Path) -> None:
    config = _config(tmp_path)
    delivery = _delivery(tmp_path)
    first = _publish(config, delivery)
    episode_root = _episode_roots(tmp_path) / first["episode_id"]

    orphan = episode_root / "packs" / f"{first['episode_id']}-r2"
    orphan.mkdir(parents=True)
    for child in (episode_root / "packs" / first["pack_id"]).iterdir():
        (orphan / child.name).write_bytes(child.read_bytes())

    _delivery(tmp_path, extra_clip=True)
    with pytest.raises(RevisionCollisionError):
        _publish(config, delivery, episode_id=first["episode_id"])


def test_claim_material_bidirectional_foreign_keys(tmp_path: Path) -> None:
    source = _source()
    claim = _claim(evidence_status="unverified", source_ids=["src1"], material_refs=["m01-777"])
    segments = [{
        "segment_id": "main-01", "start_ms": 3000, "end_ms": 11000, "claim_ids": ["c1"],
        "purpose": "visual_support", "transcript_excerpt": "", "frame_evidence_ids": [],
    }]
    semantic = _base_semantic(claims=[claim], sources=[source], materials=[_material(segments=segments)])
    stage = build_episode_pack_stage(tmp_path / "ok", episode_id="ep-demo", revision=1, semantic=semantic, generated_at="t")
    assert validate_episode_pack_stage(stage)["status"] == "pass"

    # Forward broken: segment no longer claims c1.
    broken_segments = [{**segments[0], "claim_ids": []}]
    broken = _base_semantic(claims=[dict(claim)], sources=[source], materials=[_material(segments=broken_segments)])
    stage2 = build_episode_pack_stage(tmp_path / "broken", episode_id="ep-demo", revision=1, semantic=broken, generated_at="t")
    result = validate_episode_pack_stage(stage2)
    assert result["status"] == "fail"
    assert any("material_ref" in err for err in result["errors"])

    # Reverse broken: claim drops the material ref.
    reverse = _base_semantic(claims=[_claim(evidence_status="unverified", source_ids=["src1"], material_refs=[])],
                             sources=[source], materials=[_material(segments=segments)])
    stage3 = build_episode_pack_stage(tmp_path / "reverse", episode_id="ep-demo", revision=1, semantic=reverse, generated_at="t")
    result2 = validate_episode_pack_stage(stage3)
    assert result2["status"] == "fail"
    assert any("反向引用" in err for err in result2["errors"])


def test_material_ref_never_counts_as_fact_source(tmp_path: Path) -> None:
    claim = _claim(evidence_status="unverified", source_ids=["m01-777"], material_refs=[])
    semantic = _base_semantic(claims=[claim], sources=[])
    stage = build_episode_pack_stage(tmp_path / "bad", episode_id="ep-demo", revision=1, semantic=semantic, generated_at="t")
    result = validate_episode_pack_stage(stage)
    assert result["status"] == "fail"
    assert any("把 material" in err or "事实源" in err for err in result["errors"])


def test_evidence_status_recomputed_from_sources(tmp_path: Path) -> None:
    # confirmed_official needs >=1 official verified source.
    good = _base_semantic(
        claims=[_claim(evidence_status="confirmed_official", source_ids=["src1"], fact_sources_present=1)],
        sources=[_source("src1", authority="official", verification_state="verified")],
    )
    assert validate_episode_pack_stage(build_episode_pack_stage(
        tmp_path / "good", episode_id="ep-demo", revision=1, semantic=good, generated_at="t"))["status"] == "pass"

    # confirmed_two_reliable needs two independent verified reliable sources.
    one = _base_semantic(
        claims=[_claim(evidence_status="confirmed_two_reliable", source_ids=["src1"], fact_sources_present=1)],
        sources=[_source("src1", authority="reliable_independent", verification_state="verified")],
    )
    result = validate_episode_pack_stage(build_episode_pack_stage(
        tmp_path / "one", episode_id="ep-demo", revision=1, semantic=one, generated_at="t"))
    assert result["status"] == "fail"
    assert any("两个独立可靠来源" in err for err in result["errors"])

    # Present count must be honest.
    lying = _base_semantic(
        claims=[_claim(evidence_status="unverified", source_ids=["src1"], fact_sources_present=5)],
        sources=[_source("src1")],
    )
    result2 = validate_episode_pack_stage(build_episode_pack_stage(
        tmp_path / "lying", episode_id="ep-demo", revision=1, semantic=lying, generated_at="t"))
    assert result2["status"] == "fail"
    assert any("fact_sources_present" in err for err in result2["errors"])


def test_heat_only_source_never_counts_as_fact_source(tmp_path: Path) -> None:
    """热度源不是证据：与消费端同口径复算，否则会发布消费端吃不进去的包。

    消费端（Haike）复算 ``fact_sources_present`` 时排除 heat_only。生产端若
    用宽口径算成 3 就放行 ``validation.status=pass``，消费端必抛
    「``fact_sources_present`` 与已核验来源数不一致」⇒ ``exit 2`` / ``stage=intake``。
    """
    sources = [
        _source("src-real", authority="official", verification_state="verified", publisher="p1"),
        # 标志位路线：已核验但 heat_only=True
        _source("src-flagged", authority="official", verification_state="verified", publisher="p2", heat_only=True),
        # authority 路线：authority="heat_only"
        _source("src-tagged", authority="heat_only", verification_state="verified", publisher="p3"),
    ]
    refs = ["src-real", "src-flagged", "src-tagged"]

    honest = _base_semantic(
        claims=[_claim(evidence_status="unverified", source_ids=refs, fact_sources_present=1)],
        sources=sources,
    )
    assert validate_episode_pack_stage(build_episode_pack_stage(
        tmp_path / "honest", episode_id="ep-demo", revision=1, semantic=honest, generated_at="t"))["status"] == "pass"

    # 旧宽口径会把三者都算成事实源（fact_sources_present=3）——必须被拒。
    wide = _base_semantic(
        claims=[_claim(evidence_status="unverified", source_ids=refs, fact_sources_present=3)],
        sources=sources,
    )
    result = validate_episode_pack_stage(build_episode_pack_stage(
        tmp_path / "wide", episode_id="ep-demo", revision=1, semantic=wide, generated_at="t"))
    assert result["status"] == "fail"
    assert any("fact_sources_present" in err for err in result["errors"])


def test_confirmed_two_reliable_rejects_heat_only_source(tmp_path: Path) -> None:
    """两个源里只要有一个是热度源，独立出版方计数只能算 1 ⇒ 必须被拒。"""
    claim = _claim(
        evidence_status="confirmed_two_reliable",
        source_ids=["src-real", "src-hot"],
        fact_sources_present=1,
    )
    semantic = _base_semantic(
        claims=[claim],
        sources=[
            _source("src-real", authority="reliable_independent", verification_state="verified", publisher="p1"),
            _source("src-hot", authority="reliable_independent", verification_state="verified",
                    publisher="p2", heat_only=True),
        ],
    )
    result = validate_episode_pack_stage(build_episode_pack_stage(
        tmp_path / "hot", episode_id="ep-demo", revision=1, semantic=semantic, generated_at="t"))
    assert result["status"] == "fail"
    assert any("两个独立可靠来源" in err for err in result["errors"])


def test_missing_or_non_array_ref_arrays_are_rejected(tmp_path: Path) -> None:
    """E1/E2 同族：``or []`` 把「缺键」混同「空集」，``isinstance(..., list)`` 才分得开。

    缺键与非数组都必须被拒；**空数组是合法值**，不得误伤。消费端用 ``_as_list``
    严格判类型，生产端必须同口径，否则会放行消费端吃不进去的包。
    """

    def _errors(claim=None, materials=None, *, name: str) -> list[str]:
        semantic = _base_semantic(
            claims=[] if claim is None else [claim],
            materials=materials if materials is not None else [_material()],
        )
        return validate_episode_pack_stage(build_episode_pack_stage(
            tmp_path / name, episode_id="ep-demo", revision=1, semantic=semantic, generated_at="t"))["errors"]

    # 键缺失 ⇒ 报错
    missing_refs = _claim(evidence_status="unverified", source_ids=[])
    del missing_refs["material_refs"]
    assert any("claim c1 material_refs 必须是数组" in err for err in _errors(missing_refs, name="no-material-refs"))

    missing_ids = _claim(evidence_status="unverified", source_ids=[])
    del missing_ids["source_ids"]
    assert any("claim c1 source_ids 必须是数组" in err for err in _errors(missing_ids, name="no-source-ids"))

    # 非数组（字符串）⇒ 报错，且不得退化成逐字符报错的噪声
    str_refs = _claim(evidence_status="unverified", source_ids=[])
    str_refs["material_refs"] = "m01-777"
    assert any("claim c1 material_refs 必须是数组" in err for err in _errors(str_refs, name="str-material-refs"))

    str_ids = _claim(evidence_status="unverified", source_ids=[])
    str_ids["source_ids"] = "src1"
    str_errors = _errors(str_ids, name="str-source-ids")
    assert any("claim c1 source_ids 必须是数组" in err for err in str_errors)
    assert not any("引用了不存在的事实源" in err for err in str_errors)

    # 片段 claim_ids 是同一病根：缺键曾被当成「空集」
    material = _material(claim_ids=[])
    del material["segments"][0]["claim_ids"]
    assert any("claim_ids 必须是数组" in err for err in _errors(None, [material], name="no-segment-claim-ids"))

    # 正向：空数组是合法值
    assert not _errors(_claim(evidence_status="unverified", source_ids=[], material_refs=[]), name="empty-arrays")


def test_empty_string_freshness_status_is_rejected(tmp_path: Path) -> None:
    """E3：键存在但为空串是**非法枚举值**，不能落进「未提供」分支（缺键才是未提供）。"""

    def _errors(material, *, name: str) -> list[str]:
        semantic = _base_semantic(claims=[], materials=[material])
        return validate_episode_pack_stage(build_episode_pack_stage(
            tmp_path / name, episode_id="ep-demo", revision=1, semantic=semantic, generated_at="t"))["errors"]

    empty = _material()
    empty["freshness_status"] = ""
    assert any("material m01-777 freshness_status 非法：''" in err for err in _errors(empty, name="empty"))

    absent = _material()
    del absent["freshness_status"]
    assert not _errors(absent, name="absent")


def test_bool_bytes_is_rejected_in_origin_ref(tmp_path: Path) -> None:
    """E4：``isinstance(True, int)`` 为真 ⇒ 布尔会冒充整数通过，必须显式排 bool。"""

    def _errors(rows, *, name: str) -> list[str]:
        semantic = _base_semantic()
        semantic["episode.json"]["origin_ref"]["items"] = rows
        return validate_episode_pack_stage(build_episode_pack_stage(
            tmp_path / name, episode_id="ep-demo", revision=1, semantic=semantic, generated_at="t"))["errors"]

    row = {"item_id": "原片/a.mp4", "path": "原片/a.mp4", "bytes": 1024, "sha256": "0" * 64}
    assert not _errors([row], name="int-bytes")
    assert not _errors([{**row, "bytes": 0}], name="zero-bytes")

    assert any("origin_ref.items 缺少整数 bytes" in err for err in _errors([{**row, "bytes": True}], name="bool-bytes"))
    assert any("origin_ref.items 缺少整数 bytes" in err for err in _errors([{**row, "bytes": "1024"}], name="str-bytes"))


def test_segment_timecode_purpose_and_rights_invariants(tmp_path: Path) -> None:
    bad_segment = [{
        "segment_id": "s1", "start_ms": 5000, "end_ms": 5000, "claim_ids": [],
        "purpose": "nope", "transcript_excerpt": "", "frame_evidence_ids": [],
    }]
    semantic = _base_semantic(materials=[_material(segments=bad_segment)])
    result = validate_episode_pack_stage(build_episode_pack_stage(
        tmp_path / "tc", episode_id="ep-demo", revision=1, semantic=semantic, generated_at="t"))
    assert result["status"] == "fail"
    assert any("时间码越界" in err for err in result["errors"])
    assert any("purpose" in err for err in result["errors"])

    prohibited = _rights()
    prohibited.update({"rights_status": "prohibited", "render_eligible": True, "redistribution_allowed": True})
    semantic2 = _base_semantic(rights=[prohibited])
    result2 = validate_episode_pack_stage(build_episode_pack_stage(
        tmp_path / "rights", episode_id="ep-demo", revision=1, semantic=semantic2, generated_at="t"))
    assert result2["status"] == "fail"
    assert any("prohibited" in err for err in result2["errors"])


def test_topics_argument_graph_structure(tmp_path: Path) -> None:
    def _stage(semantic, name):
        return build_episode_pack_stage(
            tmp_path / name, episode_id="ep-demo", revision=1, semantic=semantic, generated_at="t")

    def _nodes(*ids_and_dims):
        return [
            {"claim_id": cid, "dim": dim, "claim": "x", "source_candidate_ids": ["src1"]}
            for cid, dim in ids_and_dims
        ]

    # Valid: known dims, acyclic causes chain.
    nodes = _nodes(("c1", "event_core"), ("c2", "user_impact"))
    edges = [{"from": "c1", "to": "c2", "relation": "causes"}, {"from": "c2", "to": "c1", "relation": "supports"}]
    ok = _base_semantic(topics=_topics(nodes=nodes, edges=edges))
    assert validate_episode_pack_stage(_stage(ok, "ok"))["status"] == "pass"

    # Unknown dim -> invalid_contract.
    bad_dim = _base_semantic(topics=_topics(nodes=_nodes(("c1", "not_a_dim"))))
    result = validate_episode_pack_stage(_stage(bad_dim, "baddim"))
    assert result["status"] == "fail"
    assert any("invalid_contract" in err for err in result["errors"])

    # Unknown relation -> invalid_contract.
    bad_rel = _base_semantic(topics=_topics(
        nodes=nodes, edges=[{"from": "c1", "to": "c2", "relation": "implies"}]))
    assert any("invalid_contract" in err for err in
               validate_episode_pack_stage(_stage(bad_rel, "badrel"))["errors"])

    # Self edge.
    self_edge = _base_semantic(topics=_topics(
        nodes=nodes, edges=[{"from": "c1", "to": "c1", "relation": "supports"}]))
    assert any("自指" in err for err in validate_episode_pack_stage(_stage(self_edge, "self"))["errors"])

    # Duplicate triple.
    dup = _base_semantic(topics=_topics(nodes=nodes, edges=[
        {"from": "c1", "to": "c2", "relation": "supports"},
        {"from": "c1", "to": "c2", "relation": "supports"},
    ]))
    assert any("重复" in err for err in validate_episode_pack_stage(_stage(dup, "dup"))["errors"])

    # Extra edge key.
    extra = _base_semantic(topics=_topics(nodes=nodes, edges=[
        {"from": "c1", "to": "c2", "relation": "supports", "note": "x"}]))
    assert any("invalid_contract" in err for err in validate_episode_pack_stage(_stage(extra, "extra"))["errors"])

    # Cycle in the causes/precedes sub-graph.
    cycle = _base_semantic(topics=_topics(nodes=nodes, edges=[
        {"from": "c1", "to": "c2", "relation": "causes"},
        {"from": "c2", "to": "c1", "relation": "precedes"},
    ]))
    result2 = validate_episode_pack_stage(_stage(cycle, "cycle"))
    assert result2["status"] == "fail"
    assert any("环" in err for err in result2["errors"])


def test_argument_graph_closed_sets_are_frozen() -> None:
    from douyin_intelligence.episode_research_pack import ALLOWED_DIMENSIONS, ALLOWED_EDGE_RELATIONS

    assert ALLOWED_DIMENSIONS == (
        "event_core", "evidence_detail", "mechanism", "user_impact", "action_tip",
        "industry_value", "use_case", "constraint", "method", "limitation",
        "key_number", "uncertainty", "visual_moment",
    )
    assert len(ALLOWED_DIMENSIONS) == 13
    assert ALLOWED_EDGE_RELATIONS == ("supports", "qualifies", "contrasts", "causes", "precedes")


def test_same_volume_assertion_and_cross_volume_rejection(tmp_path: Path) -> None:
    episode_root = tmp_path / "output" / "每期研究包" / f"{_DATE}_研究包" / "ep-demo"
    stage = episode_root / ".staging" / "ep-demo-r1"
    destination = episode_root / "packs" / "ep-demo-r1"
    assert _same_volume(stage, destination) is True

    stage.mkdir(parents=True)
    if os.name == "nt":
        assert _same_volume("C:/a", "D:/b") is False
        with pytest.raises(CrossVolumeError):
            publish_episode_pack_directory(stage, Path("Z:/other/packs/ep-demo-r1"))
    else:
        with pytest.raises(RevisionCollisionError):
            destination.mkdir(parents=True)
            publish_episode_pack_directory(stage, destination)


def test_legacy_delivery_is_never_written_by_default(tmp_path: Path) -> None:
    config = _config(tmp_path)
    delivery = _delivery(tmp_path)
    manifest_before = (delivery / "清单.json").read_bytes()
    snapshot = {p.relative_to(delivery).as_posix(): p.read_bytes()
                for p in delivery.rglob("*") if p.is_file()}

    result = _publish(config, delivery)
    assert result["delivery_ref_mode"] == "none"
    assert not (delivery / "05-过程数据" / "research_pack_ref.json").exists()
    assert (delivery / "清单.json").read_bytes() == manifest_before
    after = {p.relative_to(delivery).as_posix(): p.read_bytes()
             for p in delivery.rglob("*") if p.is_file()}
    assert after == snapshot


def test_explicit_delivery_annotation_stays_compatible(tmp_path: Path) -> None:
    config = _config(tmp_path)
    delivery = _delivery(tmp_path)
    result = _publish(config, delivery, annotate_delivery=True)

    assert result["delivery_ref_mode"] == "manifest"
    payload = json.loads((delivery / "清单.json").read_text(encoding="utf-8"))
    assert payload["research_pack_ref"]["pack_id"] == result["pack_id"]
    assert payload["research_pack_ref"]["contract"] == CONTRACT
    assert validate_delivery_manifest(delivery / "清单.json")["status"] == "pass"


def test_research_pack_settings_default_and_test_seam() -> None:
    from douyin_intelligence.episode_research_pack import research_pack_settings

    # The feature ships ENABLED (production wants it live); an explicit false
    # disables it.  We assert the contract, never the shipped JSON value.
    assert research_pack_settings({})["enabled"] is True
    assert research_pack_settings({})["output_root"] == "output/每期研究包"
    disabled = research_pack_settings({"jobs": {"material_replication": {
        "episode_research_pack": {"enabled": False}}}})
    assert disabled["enabled"] is False

    settings = research_pack_settings(load_config())
    assert settings["output_root"] == "output/每期研究包"
    assert settings["annotate_delivery_manifest"] is False
    # tests/conftest.py neutralises the nested opt-in block for tests that do not
    # opt in, so a fixture built from the live config stays closed by default.
    assert settings["enabled"] is False


def test_automatic_pipeline_path_never_annotates_delivery(tmp_path: Path) -> None:
    from douyin_intelligence.replication_pipeline import _maybe_publish_research_pack

    config = _config(tmp_path)
    config.setdefault("jobs", {}).setdefault("material_replication", {})["episode_research_pack"] = {
        "enabled": True, "annotate_delivery_manifest": True, "output_root": "output/每期研究包",
    }
    delivery = _delivery(tmp_path)
    snapshot = {
        p.relative_to(delivery).as_posix(): p.read_bytes()
        for p in delivery.rglob("*") if p.is_file()
    }

    warnings: list[str] = []
    result = _maybe_publish_research_pack(
        config, destination=delivery, theme=_THEME, business_date=_DATE, warnings=warnings,
    )
    assert result is not None and result["status"] == "published"
    # Even with the configured switch on, the automatic path must never touch the
    # legacy delivery (annotation is a manual, CLI-only opt-in).
    assert result["delivery_ref_mode"] == "none"
    assert not (delivery / "05-过程数据" / "research_pack_ref.json").exists()
    after = {
        p.relative_to(delivery).as_posix(): p.read_bytes()
        for p in delivery.rglob("*") if p.is_file()
    }
    assert after == snapshot


def test_default_episode_id_is_stable_and_safe() -> None:
    first = default_episode_id(_DATE, _THEME)
    assert first == default_episode_id(_DATE, _THEME)
    assert first.startswith(_DATE)
    assert "/" not in first and "\\" not in first


def test_cli_registers_and_inspects_offline(capsys) -> None:
    from douyin_intelligence import cli

    assert "episode-research-pack" in cli.build_parser().format_help()
    code = cli.main([
        "episode-research-pack", "inspect",
        "--business-date", "1999-01-01", "--episode-id", "no-such-episode",
    ])
    assert code == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "missing"


# --- Deterministic fixture builder ------------------------------------------


def test_frozen_content_sha256_matches_test_vector() -> None:
    vector = content_sha256_test_vector()
    assert FROZEN_CONTENT_SHA256 == vector["content_sha256"]
    assert FROZEN_CONTENT_SHA256 == (
        "e5bc839ff60a91f29474c9bec50c8afca41fdaf367dc5e6978eae620b1cbaab8"
    )


def test_frozen_fixture_content_sha256_by_revision_matches_builder(tmp_path: Path) -> None:
    # The algorithm pin and the golden-pack pin are deliberately different kinds.
    assert FROZEN_CONTENT_SHA256 == "e5bc839ff60a91f29474c9bec50c8afca41fdaf367dc5e6978eae620b1cbaab8"
    assert FROZEN_FIXTURE_CONTENT_SHA256_BY_REVISION[1] == FROZEN_FIXTURE_CONTENT_SHA256_R1
    assert FROZEN_FIXTURE_CONTENT_SHA256_BY_REVISION[2] == FROZEN_FIXTURE_CONTENT_SHA256_R2
    assert FROZEN_FIXTURE_CONTENT_SHA256_R1 != FROZEN_CONTENT_SHA256
    assert FROZEN_FIXTURE_CONTENT_SHA256_R1 != FROZEN_FIXTURE_CONTENT_SHA256_R2

    assert FIXTURE_BUSINESS_DATE == "2026-09-16"
    assert FIXTURE_THEME == "苹果折叠屏手机"
    for revision, expected in FROZEN_FIXTURE_CONTENT_SHA256_BY_REVISION.items():
        root = build_fixture_pack(
            tmp_path / f"r{revision}", business_date=FIXTURE_BUSINESS_DATE,
            theme=FIXTURE_THEME, revision=revision,
        )
        current = json.loads((root / CURRENT_NAME).read_text(encoding="utf-8"))
        assert current["revision"] == revision
        assert current["content_sha256"] == expected, revision
        assert current["pack_id"].endswith(f"-r{revision}")
        assert current["episode_id"] == default_episode_id(FIXTURE_BUSINESS_DATE, FIXTURE_THEME)


def test_fixture_builder_module_does_not_import_tests() -> None:
    import douyin_intelligence.episode_research_pack as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert "import tests" not in source
    assert "from tests" not in source


def test_build_fixture_pack_is_reproducible_and_idempotent(tmp_path: Path) -> None:
    out = tmp_path / "output" / "每期研究包"
    first = build_fixture_pack(out, business_date=_DATE, theme=_THEME)
    assert (first / CURRENT_NAME).is_file()
    current1 = json.loads((first / CURRENT_NAME).read_text(encoding="utf-8"))
    pack1 = first / current1["pack_path"]
    assert validate_episode_pack_stage(pack1)["status"] == "pass"
    snapshot = {name: (pack1 / name).read_bytes() for name in SEMANTIC_FILES}

    second = build_fixture_pack(out, business_date=_DATE, theme=_THEME)
    assert second == first
    current2 = json.loads((second / CURRENT_NAME).read_text(encoding="utf-8"))
    # Same content -> noop: revision does not grow and no extra pack appears.
    assert current1["revision"] == 1 and current2["revision"] == 1
    assert current2["content_sha256"] == current1["content_sha256"]
    assert sorted(p.name for p in (first / "packs").iterdir()) == [current1["pack_id"]]
    pack2 = second / current2["pack_path"]
    for name in SEMANTIC_FILES:
        assert (pack2 / name).read_bytes() == snapshot[name]


def test_build_fixture_pack_is_byte_identical_across_roots(tmp_path: Path) -> None:
    left = build_fixture_pack(tmp_path / "a", business_date=_DATE, theme=_THEME)
    right = build_fixture_pack(tmp_path / "b", business_date=_DATE, theme=_THEME)
    cl = json.loads((left / CURRENT_NAME).read_text(encoding="utf-8"))
    cr = json.loads((right / CURRENT_NAME).read_text(encoding="utf-8"))
    assert cl["content_sha256"] == cr["content_sha256"]
    assert cl["pack_id"] == cr["pack_id"]
    pl, pr = left / cl["pack_path"], right / cr["pack_path"]
    for name in SEMANTIC_FILES:
        assert (pl / name).read_bytes() == (pr / name).read_bytes()


def test_build_fixture_pack_accepts_claims_and_materials(tmp_path: Path) -> None:
    claim = _claim(claim_id="c1", evidence_status="unverified", material_refs=["m-1"])
    material = _material(material_id="m-1", segments=[{
        "segment_id": "seg-1", "start_ms": 0, "end_ms": 8000, "claim_ids": ["c1"],
        "purpose": "visual_support", "transcript_excerpt": "", "frame_evidence_ids": [],
    }])
    root = build_fixture_pack(
        tmp_path / "out", business_date=_DATE, theme=_THEME,
        disposition="partial", claims=[claim], materials=[material],
    )
    current = json.loads((root / CURRENT_NAME).read_text(encoding="utf-8"))
    pack = root / current["pack_path"]
    assert validate_episode_pack_stage(pack)["status"] == "pass"
    payloads = {name: json.loads((pack / name).read_text(encoding="utf-8")) for name in SEMANTIC_FILES}
    assert payloads["claims.json"]["claims"][0]["claim_id"] == "c1"
    assert payloads["materials.json"]["materials"][0]["material_id"] == "m-1"
    # Supplied materials get conservative default rights (never renderable).
    rights = payloads["rights.json"]["rights"][0]
    assert rights["material_id"] == "m-1"
    assert rights["rights_status"] == "review_required"
    assert rights["render_eligible"] is False
    assert payloads["episode.json"]["disposition"] == "partial"


def test_build_fixture_pack_revision_chain_is_idempotent(tmp_path: Path) -> None:
    out = tmp_path / "out"
    root = build_fixture_pack(out, business_date=_DATE, theme=_THEME, revision=2)
    current = json.loads((root / CURRENT_NAME).read_text(encoding="utf-8"))
    assert current["revision"] == 2
    assert current["pack_id"].endswith("-r2")
    assert sorted(p.name for p in (root / "packs").iterdir()) == [
        f"{current['episode_id']}-r1", current["pack_id"],
    ]

    again = build_fixture_pack(out, business_date=_DATE, theme=_THEME, revision=2)
    current2 = json.loads((again / CURRENT_NAME).read_text(encoding="utf-8"))
    assert current2["revision"] == 2
    assert current2["content_sha256"] == current["content_sha256"]


def test_build_fixture_pack_rejects_bad_revision(tmp_path: Path) -> None:
    with pytest.raises(EpisodePackError):
        build_fixture_pack(tmp_path / "out", business_date=_DATE, theme=_THEME, revision=0)
