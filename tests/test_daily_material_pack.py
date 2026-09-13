from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from douyin_intelligence.config import load_config
from douyin_intelligence.daily_material_pack import (
    DailyMaterialPackError,
    build_daily_material_pack,
    resolve_daily_material_pack_input,
    validate_daily_material_pack,
    validate_selection,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path, *, visual_kind: str = "event_photo", event_status: str = "pass", usability_status: str = "pass") -> tuple[dict, Path, Path]:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    config["jobs"]["state_path"] = str(tmp_path / "state" / "jobs.json")
    config["jobs"]["lock_root"] = str(tmp_path / "state" / "locks")
    config["jobs"]["daily_material_pack"].update({
        "selection_input": "config/selection.json", "output_root": "output/packs", "temp_root": "temp/packs",
        "max_wall_seconds": 300, "max_network_requests": 40, "max_download_bytes": 209715200,
    })
    upstream = tmp_path / "output" / "visual" / "story-a"
    image_path = upstream / "review-required" / "asset-a.jpg"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (800, 600), (24, 90, 160)).save(image_path, "JPEG", quality=92)
    asset = {
        "asset_id": "asset-a", "local_path": "review-required/asset-a.jpg", "sha256": _sha(image_path),
        "mime_type": "image/jpeg", "width": 800, "height": 600, "bytes": image_path.stat().st_size,
        "rights_status": "review_required", "source_article_url": "https://official.test/story-a",
        "image_source_url": "https://official.test/asset-a.jpg", "attribution_text": "Photo Author",
        "selection_reason": "exact event",
    }
    (upstream / "manifest.json").write_text(json.dumps({"story_id": "story-a", "status": "success", "assets": [asset]}), encoding="utf-8")
    selection = {
        "selection_version": "1.0", "business_date": "2026-08-28", "selection_source": "user_approved_script",
        "stories": [{
            "story_id": "story-a", "title_zh": "稳定故事标题", "editorial_order": 1,
            "selection_source": "user_approved_script", "heat_status": "not_supplied", "heat_score": None,
            "heat_rank": None, "account_coverage": 0, "heat_evidence": [],
            "facts": [{"claim": "官方确认事件", "source_name": "Official", "source_url": "https://official.test/story-a", "use": "fact"}],
            "pending_verification": [], "do_not_claim": ["不得扩写"], "visual_intent": {"subject": "subject", "event": "event"},
            "visual_manifest_path": "output/visual/story-a/manifest.json", "visual_source_site": "Official",
            "asset_reviews": {"asset-a": {
                "visual_kind": visual_kind,
                "subject_match": {"status": "pass", "reason": "exact subject"},
                "event_match": {"status": event_status, "reason": "event review"},
                "visual_usability": {"status": usability_status, "reason": "usability review"},
                "selection_reason": "three gates", "composition_hint": "landscape", "crop_hint": "keep subject",
                "visual_qa": {"reviewed": True, "content": "event scene", "accuracy": "exact", "duplicate_or_wrong": "none", "watermark_text": "none", "clarity": "clear", "orientation": "landscape"},
            }},
            "missing_visual_reason": "honest missing",
        }],
    }
    selection_path = tmp_path / "config" / "selection.json"
    selection_path.parent.mkdir(parents=True)
    selection_path.write_text(json.dumps(selection, ensure_ascii=False), encoding="utf-8")
    return config, selection_path, image_path


def test_builds_self_contained_pack_validates_manifest_and_warm_cache_is_zero_network(tmp_path: Path) -> None:
    config, selection, original = _fixture(tmp_path)
    original_sha = _sha(original)
    cold = build_daily_material_pack(config, selection, quick=True)
    assert cold["status"] == "success" and cold["cache_status"] == "cold_build"
    assert cold["network_requests"] == 0 and cold["images"] == 1
    validation = validate_daily_material_pack(cold["json_path"])
    assert validation["status"] == "valid" and validation["files"] >= 8
    pack = json.loads(Path(cold["json_path"]).read_text(encoding="utf-8"))
    brief = (Path(cold["output_dir"]) / "daily-material-brief.md").read_text(encoding="utf-8")
    assert pack["stories"][0]["heat_status"] == "not_supplied"
    assert pack["stories"][0]["primary"]["relative_path"] == "stories/story-a/primary.jpg"
    assert pack["stories"][0]["primary"]["subject_match"]["status"] == "pass"
    assert "- 待核实：无" in brief
    assert "- 禁止宣称：\n  - 不得扩写" in brief
    assert "- 待核实：[" not in brief and "- 禁止宣称：[" not in brief
    assert all((Path(cold["output_dir"]) / value).is_file() for value in pack["openmontage"]["snapshots"])
    hashes_before = {_sha(path) for path in Path(cold["output_dir"]).rglob("primary.*")}
    warm = build_daily_material_pack(config, selection, quick=True)
    hashes_after = {_sha(path) for path in Path(warm["output_dir"]).rglob("primary.*")}
    assert warm["cache_status"] == "warm_hit" and warm["network_requests"] == 0
    assert warm["elapsed_seconds"] < 15 and hashes_before == hashes_after == {original_sha}
    assert _sha(original) == original_sha


def test_video_social_ui_and_wrong_event_are_rejected_even_when_subject_matches(tmp_path: Path) -> None:
    config, selection, _original = _fixture(tmp_path, visual_kind="video_screenshot", event_status="fail", usability_status="fail")
    result = build_daily_material_pack(config, selection, quick=False)
    pack = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
    story = pack["stories"][0]
    assert result["status"] == "partial" and story["visual_status"] == "missing"
    assert not list((Path(result["output_dir"]) / "stories" / "story-a").glob("primary.*"))
    reasons = story["rejected_assets"][0]["reasons"]
    assert "event_mismatch" in reasons and "visual_unusable" in reasons
    assert "visual_kind_rejected:video_screenshot" in reasons


@pytest.mark.parametrize("kind", ["logo", "avatar", "qrcode", "social_ui_screenshot", "old_event_photo"])
def test_forbidden_visual_kinds_never_enter_final_assets(tmp_path: Path, kind: str) -> None:
    config, selection, _original = _fixture(tmp_path, visual_kind=kind)
    result = build_daily_material_pack(config, selection, quick=False)
    pack = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
    assert pack["stories"][0]["assets"] == []
    assert f"visual_kind_rejected:{kind}" in pack["stories"][0]["rejected_assets"][0]["reasons"]


def test_perceptual_near_duplicate_cannot_fill_backup(tmp_path: Path) -> None:
    config, selection_path, original = _fixture(tmp_path)
    second = original.with_name("asset-b.jpg")
    Image.open(original).save(second, "JPEG", quality=70)
    manifest_path = original.parents[1] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    second_asset = {**manifest["assets"][0], "asset_id": "asset-b", "local_path": "review-required/asset-b.jpg", "sha256": _sha(second), "bytes": second.stat().st_size}
    manifest["assets"].append(second_asset)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    selection["stories"][0]["asset_reviews"]["asset-b"] = dict(selection["stories"][0]["asset_reviews"]["asset-a"])
    selection_path.write_text(json.dumps(selection), encoding="utf-8")
    result = build_daily_material_pack(config, selection_path, quick=False)
    pack = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
    assert len(pack["stories"][0]["assets"]) == 1
    assert "perceptual_near_duplicate" in pack["stories"][0]["rejected_assets"][0]["reasons"]


def test_stable_story_id_controls_join_and_title_change_does_not_break_it(tmp_path: Path) -> None:
    config, selection_path, _original = _fixture(tmp_path)
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    selection["stories"][0]["title_zh"] = "标题可以改变但ID稳定"
    selection_path.write_text(json.dumps(selection, ensure_ascii=False), encoding="utf-8")
    assert build_daily_material_pack(config, selection_path, quick=False)["status"] == "success"
    selection["stories"][0]["story_id"] = "different-story-id"
    selection_path.write_text(json.dumps(selection, ensure_ascii=False), encoding="utf-8")
    failed = build_daily_material_pack(config, selection_path, quick=False)
    pack = json.loads(Path(failed["json_path"]).read_text(encoding="utf-8"))
    assert failed["status"] == "partial" and pack["stories"][0]["visual_status"] == "missing"
    assert "story_id不一致" in pack["stories"][0]["missing_reason"]


def test_not_supplied_heat_cannot_carry_fake_score_or_rank() -> None:
    payload = {
        "selection_version": "1.0", "business_date": "2026-08-28", "stories": [{
            "story_id": "story-a", "title_zh": "新闻标题有效", "heat_status": "not_supplied", "heat_score": 99,
            "heat_rank": None, "facts": [{"claim": "fact"}], "do_not_claim": [],
            "visual_manifest_path": "manifest.json", "asset_reviews": {},
        }],
    }
    with pytest.raises(DailyMaterialPackError, match="不得填写"):
        validate_selection(payload)


def test_cache_key_changes_when_selection_or_source_changes(tmp_path: Path) -> None:
    config, selection_path, original = _fixture(tmp_path)
    first = build_daily_material_pack(config, selection_path, quick=True)
    payload = json.loads(selection_path.read_text(encoding="utf-8"))
    payload["stories"][0]["do_not_claim"].append("新增边界")
    selection_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    second = build_daily_material_pack(config, selection_path, quick=True)
    assert second["cache_status"] == "cold_build" and second["output_dir"] != first["output_dir"]
    Image.new("RGB", (800, 600), (180, 40, 20)).save(original, "JPEG", quality=92)
    third = build_daily_material_pack(config, selection_path, quick=True)
    assert third["cache_status"] == "cold_build" and third["output_dir"] != second["output_dir"]


def test_manifest_detects_tampering_and_paths_are_relative(tmp_path: Path) -> None:
    config, selection_path, _original = _fixture(tmp_path)
    result = build_daily_material_pack(config, selection_path, quick=False)
    pack = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
    assert not Path(pack["stories"][0]["primary"]["relative_path"]).is_absolute()
    primary = Path(result["output_dir"]) / pack["stories"][0]["primary"]["relative_path"]
    primary.write_bytes(primary.read_bytes() + b"tamper")
    with pytest.raises(DailyMaterialPackError, match="图片文件校验失败"):
        validate_daily_material_pack(result["json_path"])


def test_global_wall_budget_yields_valid_partial_instead_of_losing_package(tmp_path: Path) -> None:
    config, selection_path, _original = _fixture(tmp_path)
    config["jobs"]["daily_material_pack"]["max_wall_seconds"] = 1
    values = iter([0.0, 2.0, 2.0, 2.0, 2.0])
    result = build_daily_material_pack(config, selection_path, quick=False, clock=lambda: next(values, 2.0))
    assert result["status"] == "partial"
    assert validate_daily_material_pack(result["json_path"])["status"] == "valid"
    pack = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
    assert "墙钟预算" in pack["stories"][0]["missing_reason"]


def test_input_auto_selection_prefers_configured_valid_file_and_rejects_secret_fields(tmp_path: Path) -> None:
    config, selection_path, _original = _fixture(tmp_path)
    assert resolve_daily_material_pack_input(config) == selection_path
    payload = json.loads(selection_path.read_text(encoding="utf-8"))
    payload["api_key"] = "must-not-exist"
    selection_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DailyMaterialPackError, match="敏感字段"):
        resolve_daily_material_pack_input(config)
