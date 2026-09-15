"""Whole-file delivery (``material_replica.direct_delivery``, 2026-09-16).

The opt-in switch ships **enabled** in ``config/content_intelligence.json``, and
``tests/conftest.py`` therefore strips it from every ``load_config()`` fixture --
so these cases read the shipped file directly when they need to assert on what
production actually says, and build their own window otherwise.

Three things must hold, and each is a separate failure mode:

* the event vocabulary resolves for the shipped themes (otherwise main/support
  classification silently degrades to "longest first" and nobody notices);
* a malformed or absent table yields ``[]`` (otherwise the classification would
  depend on whatever happened to be in the config);
* the two face rules in ``validate_delivery_manifest`` are waived **only** in
  direct mode (otherwise the waiver would silently weaken every other delivery).
"""

from __future__ import annotations

import json
from pathlib import Path

from douyin_intelligence import config as config_module
from douyin_intelligence.config import load_config
from douyin_intelligence.replication_delivery import validate_delivery_manifest
from douyin_intelligence.replication_theme import event_terms

_THEME = "特朗普致电黄仁勋"


def _material_config(**overrides: object) -> dict:
    """A config with all three override tables removed, then re-seeded."""
    config = load_config()
    material = config["jobs"]["material_replication"]
    for key in ("theme_keywords", "theme_subject_terms", "theme_event_terms"):
        material.pop(key, None)
    material.update(overrides)
    return config


def _shipped_material() -> dict:
    shipped = json.loads(
        (config_module.project_root() / "config" / "content_intelligence.json").read_text(encoding="utf-8")
    )
    return shipped["jobs"]["material_replication"]


def test_event_terms_is_empty_without_the_key() -> None:
    assert event_terms(_THEME, _material_config()) == []


def test_event_terms_reads_the_theme_key() -> None:
    terms = ["电话", "免提"]
    config = _material_config(theme_event_terms={_THEME: list(terms)})
    assert event_terms(_THEME, config) == terms


def test_event_terms_ignores_malformed_values() -> None:
    for bad in (None, {}, {"其他主题": ["x"]}, {_THEME: "电话"}, {_THEME: []}, [_THEME]):
        assert event_terms(_THEME, _material_config(theme_event_terms=bad)) == [], bad


def test_shipped_event_terms_resolve_for_every_listed_theme() -> None:
    """Whatever the shipped table names, each entry must actually take effect.

    The self-consistency shape matters more than a hard-coded list: a table entry
    that never resolves is exactly the 9.14 ``theme=theme`` failure -- a feature
    that is committed and looks configured while the pipeline never sees it.
    """
    table = _shipped_material().get("theme_event_terms") or {}
    assert table, "原片直投的主/辅分类依赖本表，出厂配置不得为空"
    for theme, terms in table.items():
        assert terms, theme
        assert event_terms(theme, _material_config(theme_event_terms=table)) == list(terms), theme


def _manifest(tmp_path: Path, *, direct: bool) -> Path:
    payload: dict = {
        "schema_version": 1,
        "theme": _THEME,
        "folder": "9.16特朗普致电黄仁勋复刻视频",
        "business_date": "2026-09-16",
        "generated_at": "2026-09-16T00:00:00+08:00",
        "keywords_used": [],
        "candidate_pool_size": 0,
        "script_replica": {},
        "material_replica_sources": [],
        "main_materials": [
            {"clip_id": "main-01", "file": "02-主素材/a.mp4", "duration": 120.0,
             "face_class": "face_heavy", "suggested_use": "hook"},
        ],
        "supporting_materials": [],
        "counters": {},
        "face_backend": "opencv_yunet",
        "face_backend_status": "ok",
        "ffmpeg_status": "ok",
        "degraded": False,
        "insufficient": False,
        "warnings": [],
        "evidence_disclaimer": "抖音素材仅为发现与关注度证据",
    }
    if direct:
        payload["direct_delivery"] = {"enabled": True, "event_terms": ["电话"], "main_min_seconds": 60}
    path = tmp_path / "清单.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_face_rules_apply_when_direct_delivery_is_absent(tmp_path: Path) -> None:
    verdict = validate_delivery_manifest(_manifest(tmp_path, direct=False))
    assert verdict["status"] == "fail"
    assert any("face_heavy" in err for err in verdict["errors"])
    assert any("非 face_free" in err for err in verdict["errors"])


def test_face_rules_are_waived_in_direct_mode(tmp_path: Path) -> None:
    """An interview has its subject on camera; that is the point of opting in."""
    verdict = validate_delivery_manifest(_manifest(tmp_path, direct=True))
    assert verdict["status"] == "pass", verdict["errors"]


def test_direct_delivery_table_is_registered_in_the_sources_it_touches() -> None:
    """The waiver must be *reachable*: pipeline writes the block, validator reads it."""
    pipeline = (config_module.project_root() / "src" / "douyin_intelligence" / "replication_pipeline.py").read_text(
        encoding="utf-8"
    )
    assert 'manifest["direct_delivery"]' in pipeline
    assert 'settings.get("direct_delivery")' in pipeline
