from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from PIL import Image

from .exporter import atomic_write_json, export_outputs
from .job_runtime import JobLock, JobState
from .material_probe import _publish_directory


PACK_VERSION = "1.0"
PACK_BUILDER_VERSION = "2026-08-28-v1.1"
IMAGE_GATE_VERSION = "2026-08-28-v1"
_ALLOWED_VISUAL_KINDS = {"event_photo", "official_product_visual", "game_promo", "feature_demo"}
_REJECTED_VISUAL_KINDS = {
    "video_screenshot", "social_ui_screenshot", "old_event_photo", "logo", "avatar", "qrcode",
    "chat_ui", "generic_brand", "watermark_collage",
}
_FORBIDDEN_FIELD_PARTS = ("cookie", "api_key", "apikey", "password", "authorization", "browser_profile")


class DailyMaterialPackError(ValueError):
    """Raised when a daily material pack contract is invalid."""


def _project_root(config: dict[str, Any]) -> Path:
    return Path(str(config.get("_project_root") or Path(__file__).resolve().parents[2])).resolve()


def _project_path(config: dict[str, Any], value: str | Path) -> Path:
    root = _project_root(config)
    path = Path(value)
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    if resolved != root and root not in resolved.parents:
        raise DailyMaterialPackError(f"路径必须位于copy_skill项目内：{value}")
    return resolved


def _relative_path(value: str) -> Path:
    path = Path(str(value or ""))
    if not str(path) or path.is_absolute() or ".." in path.parts:
        raise DailyMaterialPackError(f"包内路径必须是安全相对路径：{value}")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dhash(path: Path) -> str:
    with Image.open(path) as source:
        image = source.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        pixels = list(image.get_flattened_data())
    bits = [pixels[row * 9 + column] > pixels[row * 9 + column + 1] for row in range(8) for column in range(8)]
    return f"{sum((1 << index) for index, enabled in enumerate(bits) if enabled):016x}"


def _hamming(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DailyMaterialPackError(f"输入文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise DailyMaterialPackError(f"JSON无效：{path}") from exc
    if not isinstance(payload, dict):
        raise DailyMaterialPackError(f"JSON根节点必须是对象：{path}")
    return payload


def _walk_forbidden_fields(value: Any, prefix: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).casefold()
            if any(part in lowered for part in _FORBIDDEN_FIELD_PARTS):
                raise DailyMaterialPackError(f"合同包含敏感字段：{prefix}{key}")
            _walk_forbidden_fields(item, f"{prefix}{key}.")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk_forbidden_fields(item, f"{prefix}{index}.")


def validate_selection(payload: dict[str, Any]) -> dict[str, Any]:
    _walk_forbidden_fields(payload)
    if payload.get("selection_version") != PACK_VERSION:
        raise DailyMaterialPackError("selection_version必须为1.0")
    business_date = str(payload.get("business_date") or "")
    try:
        datetime.strptime(business_date, "%Y-%m-%d")
    except ValueError as exc:
        raise DailyMaterialPackError("business_date必须为YYYY-MM-DD") from exc
    stories = payload.get("stories")
    if not isinstance(stories, list) or not 1 <= len(stories) <= 4:
        raise DailyMaterialPackError("每日素材包必须包含1到4条已选新闻")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, source in enumerate(stories):
        if not isinstance(source, dict):
            raise DailyMaterialPackError("新闻选择项必须是对象")
        story_id = str(source.get("story_id") or "").strip()
        if not story_id or story_id in seen:
            raise DailyMaterialPackError("story_id不能为空或重复")
        seen.add(story_id)
        title = str(source.get("title_zh") or "").strip()
        if len(title) < 4:
            raise DailyMaterialPackError(f"{story_id}缺少有效标题")
        if source.get("heat_status") not in {"not_supplied", "supplied"}:
            raise DailyMaterialPackError(f"{story_id}的heat_status无效")
        if source.get("heat_status") == "not_supplied" and any(source.get(key) is not None for key in ("heat_score", "heat_rank")):
            raise DailyMaterialPackError(f"{story_id}没有热度证据时不得填写heat_score/heat_rank")
        if not isinstance(source.get("facts"), list) or not source["facts"]:
            raise DailyMaterialPackError(f"{story_id}至少需要一条事实证据")
        if not isinstance(source.get("do_not_claim"), list):
            raise DailyMaterialPackError(f"{story_id}必须提供do_not_claim")
        manifest_path = str(source.get("visual_manifest_path") or "")
        if not manifest_path:
            raise DailyMaterialPackError(f"{story_id}缺少visual_manifest_path")
        reviews = source.get("asset_reviews")
        if not isinstance(reviews, dict):
            raise DailyMaterialPackError(f"{story_id}必须提供asset_reviews三道门结论")
        normalized.append({**source, "story_id": story_id, "title_zh": title, "editorial_order": int(source.get("editorial_order") or index + 1)})
    return {**payload, "business_date": business_date, "stories": normalized}


def _dependency_fingerprint(config: dict[str, Any], selection: dict[str, Any], max_assets_per_story: int) -> tuple[str, list[dict[str, str]]]:
    dependencies: list[dict[str, str]] = []
    for story in selection["stories"]:
        manifest_path = _project_path(config, story["visual_manifest_path"])
        manifest = _load_json(manifest_path)
        dependencies.append({"path": str(Path(story["visual_manifest_path"]).as_posix()), "sha256": _sha256(manifest_path)})
        for asset in manifest.get("assets") or []:
            local = manifest_path.parent / _relative_path(str(asset.get("local_path") or ""))
            if local.is_file():
                dependencies.append({"path": str(local.relative_to(_project_root(config)).as_posix()), "sha256": _sha256(local)})
    canonical = json.dumps({
        "selection": selection,
        "builder_version": PACK_BUILDER_VERSION,
        "rule_version": IMAGE_GATE_VERSION,
        "max_assets_per_story": max_assets_per_story,
        "dependencies": dependencies,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest(), dependencies


def _gate_asset(asset: dict[str, Any], review: dict[str, Any], source_path: Path) -> tuple[dict[str, Any], list[str]]:
    visual_kind = str(review.get("visual_kind") or "unknown")
    subject = dict(review.get("subject_match") or {})
    event = dict(review.get("event_match") or {})
    usability = dict(review.get("visual_usability") or {})
    reasons: list[str] = []
    if subject.get("status") != "pass":
        reasons.append("subject_mismatch")
    if event.get("status") != "pass":
        reasons.append("event_mismatch")
    if usability.get("status") != "pass":
        reasons.append("visual_unusable")
    if visual_kind in _REJECTED_VISUAL_KINDS or visual_kind not in _ALLOWED_VISUAL_KINDS:
        reasons.append(f"visual_kind_rejected:{visual_kind}")
    if str(asset.get("rights_status") or "") == "reference_only":
        reasons.append("reference_only_not_final")
    if not source_path.is_file():
        reasons.append("source_file_missing")
    else:
        digest = _sha256(source_path)
        if digest != str(asset.get("sha256") or ""):
            reasons.append("source_sha256_mismatch")
        try:
            with Image.open(source_path) as image:
                image.verify()
            with Image.open(source_path) as image:
                width, height = image.size
            if (width, height) != (int(asset.get("width") or 0), int(asset.get("height") or 0)):
                reasons.append("source_dimensions_mismatch")
            if min(width, height) < 480:
                reasons.append("low_resolution")
        except (OSError, ValueError):
            reasons.append("image_decode_failed")
    gates = {
        "subject_match": subject,
        "event_match": event,
        "visual_usability": usability,
        "visual_kind": visual_kind,
        "gate_version": IMAGE_GATE_VERSION,
        "accepted": not reasons,
    }
    return gates, list(dict.fromkeys(reasons))


def _story_stub(story: dict[str, Any], error: str) -> dict[str, Any]:
    return {
        "story_id": story["story_id"], "title_zh": story["title_zh"], "editorial_order": story["editorial_order"],
        "selection_source": story.get("selection_source", "unknown"), "heat_status": story["heat_status"],
        "heat_score": story.get("heat_score"), "heat_rank": story.get("heat_rank"),
        "account_coverage": int(story.get("account_coverage") or 0), "heat_evidence": story.get("heat_evidence") or [],
        "facts": story["facts"], "pending_verification": story.get("pending_verification") or [],
        "do_not_claim": story["do_not_claim"], "visual_intent": story.get("visual_intent") or {},
        "visual_status": "missing", "primary": None, "backup": None, "assets": [], "rejected_assets": [],
        "missing_reason": error, "errors": [{"stage": "story_assembly", "message": error}],
    }


def _build_story(config: dict[str, Any], stage: Path, story: dict[str, Any], max_assets: int) -> dict[str, Any]:
    manifest_path = _project_path(config, story["visual_manifest_path"])
    manifest = _load_json(manifest_path)
    if str(manifest.get("story_id") or "") != story["story_id"]:
        raise DailyMaterialPackError(f"{story['story_id']}与视觉manifest的story_id不一致")
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    accepted_hashes: set[str] = set()
    accepted_visual_hashes: list[str] = []
    story_dir = stage / "stories" / story["story_id"]
    story_dir.mkdir(parents=True, exist_ok=True)
    reviews = story["asset_reviews"]
    for asset in manifest.get("assets") or []:
        asset_id = str(asset.get("asset_id") or "")
        review = reviews.get(asset_id)
        if not isinstance(review, dict):
            rejected.append({"asset_id": asset_id, "reasons": ["missing_three_gate_review"]})
            continue
        source_path = manifest_path.parent / _relative_path(str(asset.get("local_path") or ""))
        gates, reasons = _gate_asset(asset, review, source_path)
        digest = str(asset.get("sha256") or "")
        if digest in accepted_hashes:
            reasons.append("duplicate_sha256")
            gates["accepted"] = False
        perceptual_hash = _dhash(source_path) if source_path.is_file() else ""
        if perceptual_hash and any(_hamming(perceptual_hash, value) <= 3 for value in accepted_visual_hashes):
            reasons.append("perceptual_near_duplicate")
            gates["accepted"] = False
        if reasons or len(accepted) >= max_assets:
            rejected.append({
                "asset_id": asset_id, "source_path": str(Path(story["visual_manifest_path"]).parent / str(asset.get("local_path") or "")),
                "gates": gates, "reasons": reasons or ["final_asset_limit"],
            })
            continue
        role = "primary" if not accepted else "backup"
        extension = source_path.suffix.casefold() or ".img"
        destination = story_dir / ("primary" if role == "primary" else "backup-01")
        destination = destination.with_suffix(extension)
        shutil.copy2(source_path, destination)
        copied_sha = _sha256(destination)
        if copied_sha != digest:
            destination.unlink(missing_ok=True)
            rejected.append({"asset_id": asset_id, "gates": {**gates, "accepted": False}, "reasons": ["copy_sha256_mismatch"]})
            continue
        accepted_hashes.add(digest)
        accepted_visual_hashes.append(perceptual_hash)
        accepted.append({
            "asset_id": asset_id, "role": role, "relative_path": destination.relative_to(stage).as_posix(),
            "sha256": digest, "bytes": destination.stat().st_size, "mime_type": asset.get("mime_type"),
            "perceptual_hash": perceptual_hash,
            "width": int(asset.get("width") or 0), "height": int(asset.get("height") or 0),
            "source_article_url": asset.get("source_article_url"), "image_source_url": asset.get("image_source_url"),
            "source_site": asset.get("publisher") or story.get("visual_source_site") or "来源页",
            "attribution_text": asset.get("attribution_text") or "", "rights_status": "review_required",
            "composition_hint": review.get("composition_hint") or "按原画幅使用，裁切前人工复核主体与文字",
            "crop_hint": review.get("crop_hint") or "避免裁掉主体、署名或关键界面",
            "relevance": review.get("relevance") or "exact_subject_event",
            "selection_reason": review.get("selection_reason") or asset.get("selection_reason") or "通过主体、事件与画面可用性三道门",
            "visual_qa": review.get("visual_qa") or {},
            **gates,
        })
    visual_status = "ready" if len(accepted) == max_assets else "partial" if accepted else "missing"
    missing_reason = "" if len(accepted) == max_assets else str(story.get("missing_visual_reason") or (
        "仅有1张素材通过主体、事件和画面可用性三道门。" if accepted else "没有素材同时通过主体、事件和画面可用性三道门。"
    ))
    result = {
        "story_id": story["story_id"], "title_zh": story["title_zh"], "editorial_order": story["editorial_order"],
        "selection_source": story.get("selection_source", "unknown"), "heat_status": story["heat_status"],
        "heat_score": story.get("heat_score"), "heat_rank": story.get("heat_rank"),
        "account_coverage": int(story.get("account_coverage") or 0), "heat_evidence": story.get("heat_evidence") or [],
        "facts": story["facts"], "pending_verification": story.get("pending_verification") or [],
        "do_not_claim": story["do_not_claim"], "visual_intent": story.get("visual_intent") or {},
        "visual_status": visual_status, "primary": accepted[0] if accepted else None,
        "backup": accepted[1] if len(accepted) > 1 else None, "assets": accepted, "rejected_assets": rejected,
        "missing_reason": missing_reason, "errors": [],
        "upstream": {"visual_manifest_path": str(Path(story["visual_manifest_path"]).as_posix()), "status": manifest.get("status")},
    }
    atomic_write_json(story_dir / "story.json", result)
    return result


def _markdown(pack: dict[str, Any]) -> str:
    lines = [
        "# 每日科技新闻素材供应包", "",
        f"业务日期：`{pack['business_date']}`　状态：`{pack['status']}`　新闻：{pack['counts']['stories']}　合格图片：{pack['counts']['images']}", "",
        "> 本包用于上游事实与素材供应。没有真实账号热度证据时明确标为 not_supplied；所有图片均需人工权利复核，OpenMontage 尚未被修改。", "",
        "| 编辑顺序 | 新闻 | 热度状态 | 视觉状态 | 主图/备用 |", "|---:|---|---|---|---:|",
    ]
    for story in pack["stories"]:
        lines.append(f"| {story['editorial_order']} | {story['title_zh']} | `{story['heat_status']}` | `{story['visual_status']}` | {len(story['assets'])} |")
    for story in pack["stories"]:
        lines.extend(["", f"## {story['editorial_order']}. {story['title_zh']}", "", f"- `story_id`：`{story['story_id']}`", f"- 热度：`{story['heat_status']}`；账号覆盖：{story['account_coverage']}；热度排名：{story['heat_rank'] if story['heat_rank'] is not None else '未提供'}", "- 已核实要点："])
        for fact in story["facts"]:
            lines.append(f"  - {fact.get('claim')}（[{fact.get('source_name') or '来源'}]({fact.get('source_url')}), 用途：{fact.get('use') or '事实确认'}）")
        for label, values in (("待核实", story["pending_verification"]), ("禁止宣称", story["do_not_claim"])):
            cleaned = [str(value).strip() for value in values if str(value).strip()]
            if cleaned:
                lines.append(f"- {label}：")
                lines.extend(f"  - {value}" for value in cleaned)
            else:
                lines.append(f"- {label}：无")
        if story["assets"]:
            lines.append("- 最终图片：")
            for asset in story["assets"]:
                lines.append(
                    f"  - {asset['role']}：`{asset['relative_path']}`，{asset['width']}×{asset['height']}，"
                    f"`{asset['rights_status']}`；{asset['selection_reason']}；构图：{asset['composition_hint']}"
                )
                qa = asset.get("visual_qa") or {}
                if qa:
                    lines.append(
                        f"    - 逐图QA：{qa.get('content', '')}；准确性：{qa.get('accuracy', '')}；"
                        f"重复/错图：{qa.get('duplicate_or_wrong', '')}；水印/文字：{qa.get('watermark_text', '')}；"
                        f"清晰度：{qa.get('clarity', '')}；画幅：{qa.get('orientation', '')}"
                    )
        else:
            lines.append(f"- 最终图片：缺失。原因：{story['missing_reason']}")
        if story["rejected_assets"]:
            rejected_text = ", ".join(f"{item['asset_id']} ({'/'.join(item['reasons'])})" for item in story["rejected_assets"])
            lines.append(f"- 被拒素材：{rejected_text}")
    usage = pack["usage"]
    lines.extend(["", "## 运行与下游边界", "", f"- 网络请求：{usage['network_requests']} / {usage['max_network_requests']}；下载：{usage['downloaded_bytes']} / {usage['max_download_bytes']} bytes", f"- 耗时：{usage['elapsed_seconds']} / {usage['max_wall_seconds']} 秒；缓存：`{usage['cache_status']}`", "- LLM / ASR / 音频 / 抖音 / 浏览器：0", "- OpenMontage写入：0；包内仅生成兼容快照，所有图片默认review_required。", ""])
    return "\n".join(lines)


def _openmontage_payloads(pack: dict[str, Any]) -> dict[str, Any]:
    captured_at = pack["generated_at"]
    heat_items = []
    for story in pack["stories"]:
        if story["heat_status"] == "supplied":
            heat_items.append({"story_id": story["story_id"], "word": story["title_zh"], "hotScore": story["heat_score"], "heat_rank": story["heat_rank"]})
    candidates = [{
        "story_id": story["story_id"], "title": story["title_zh"], "editorial_order": story["editorial_order"],
        "heat_status": story["heat_status"], "visual_status": story["visual_status"],
        "primary_image": story["primary"]["relative_path"] if story["primary"] else None,
        "backup_image": story["backup"]["relative_path"] if story["backup"] else None,
        "do_not_claim": story["do_not_claim"],
    } for story in pack["stories"]]
    return {
        "hotboard.json": {"captured_at": captured_at, "target_date": pack["business_date"], "items": heat_items},
        "benchmark_accounts.json": {"captured_at": captured_at, "target_date": pack["business_date"], "videos": [], "heat_status": "not_supplied" if not heat_items else "supplied"},
        "content_candidates.json": {"version": "1.0", "captured_at": captured_at, "target_date": pack["business_date"], "items": candidates},
        "run_report.json": {"status": pack["status"], "captured_at": captured_at, "target_date": pack["business_date"], "counts": pack["counts"], "source": "daily_material_pack_v1", "openmontage_modified": False},
    }


def _write_manifest(stage: Path) -> dict[str, Any]:
    files = []
    for path in sorted(item for item in stage.rglob("*") if item.is_file() and item.name != "package-manifest.json"):
        files.append({"path": path.relative_to(stage).as_posix(), "bytes": path.stat().st_size, "sha256": _sha256(path)})
    payload = {"manifest_version": PACK_VERSION, "self_excluded": "package-manifest.json", "files": files}
    atomic_write_json(stage / "package-manifest.json", payload)
    return payload


def validate_daily_material_pack(pack_path: str | Path) -> dict[str, Any]:
    path = Path(pack_path).resolve()
    pack = _load_json(path)
    _walk_forbidden_fields(pack)
    if pack.get("pack_version") != PACK_VERSION:
        raise DailyMaterialPackError("pack_version必须为1.0")
    stories = pack.get("stories")
    if not isinstance(stories, list) or not 1 <= len(stories) <= 4:
        raise DailyMaterialPackError("供应包故事数量无效")
    if len({str(row.get("story_id") or "") for row in stories}) != len(stories):
        raise DailyMaterialPackError("供应包story_id重复")
    root = path.parent
    image_count = 0
    for story in stories:
        if story.get("heat_status") == "not_supplied" and any(story.get(key) is not None for key in ("heat_score", "heat_rank")):
            raise DailyMaterialPackError("not_supplied新闻不得包含虚构热度")
        assets = story.get("assets") or []
        if len(assets) > 2:
            raise DailyMaterialPackError("每条新闻最多1主+1备用")
        for asset in assets:
            relative = _relative_path(str(asset.get("relative_path") or ""))
            local = root / relative
            if not local.is_file() or local.stat().st_size != int(asset.get("bytes") or -1) or _sha256(local) != asset.get("sha256"):
                raise DailyMaterialPackError(f"图片文件校验失败：{relative}")
            with Image.open(local) as image:
                if image.size != (int(asset.get("width") or 0), int(asset.get("height") or 0)):
                    raise DailyMaterialPackError(f"图片尺寸不一致：{relative}")
            if not all((asset.get(key) or {}).get("status") == "pass" for key in ("subject_match", "event_match", "visual_usability")):
                raise DailyMaterialPackError(f"最终图片未通过三道门：{relative}")
            if asset.get("rights_status") != "review_required":
                raise DailyMaterialPackError(f"V1最终图片必须为review_required：{relative}")
            qa = asset.get("visual_qa") or {}
            if qa.get("reviewed") is not True or not all(str(qa.get(key) or "").strip() for key in ("content", "accuracy", "duplicate_or_wrong", "watermark_text", "clarity", "orientation")):
                raise DailyMaterialPackError(f"最终图片缺少完整逐图QA：{relative}")
            image_count += 1
    manifest_path = root / _relative_path(str(pack.get("manifest_path") or "package-manifest.json"))
    manifest = _load_json(manifest_path)
    listed = set()
    for row in manifest.get("files") or []:
        relative = _relative_path(str(row.get("path") or ""))
        local = root / relative
        if not local.is_file() or local.stat().st_size != int(row.get("bytes") or -1) or _sha256(local) != row.get("sha256"):
            raise DailyMaterialPackError(f"manifest文件校验失败：{relative}")
        listed.add(relative.as_posix())
    actual = {item.relative_to(root).as_posix() for item in root.rglob("*") if item.is_file() and item.name != "package-manifest.json"}
    if listed != actual:
        raise DailyMaterialPackError("package-manifest未完整覆盖包内文件")
    return {"status": "valid", "pack_path": str(path), "stories": len(stories), "images": image_count, "files": len(listed)}


def build_daily_material_pack(
    config: dict[str, Any], selection_path: str | Path | None = None, output_root: str | Path | None = None,
    *, quick: bool = True, clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    settings = config["jobs"]["daily_material_pack"]
    selected_path = _project_path(config, selection_path or settings["selection_input"])
    selection = validate_selection(_load_json(selected_path))
    if len(selection["stories"]) > int(settings["max_stories"]):
        raise DailyMaterialPackError("选择新闻数量超过配置的整包上限")
    max_assets = int(settings["max_assets_per_story"])
    cache_key, dependencies = _dependency_fingerprint(config, selection, max_assets)
    root = _project_path(config, output_root or settings["output_root"]) / selection["business_date"]
    run_id = f"pack-{selection['business_date'].replace('-', '')}-{cache_key[:12]}"
    destination = root / run_id
    started = clock()
    state = JobState(config, "daily_material_pack")
    if quick and (destination / "daily-material-pack.json").is_file():
        try:
            validation = validate_daily_material_pack(destination / "daily-material-pack.json")
            cached = _load_json(destination / "daily-material-pack.json")
            if cached.get("cache_key") == cache_key:
                elapsed = round(clock() - started, 3)
                state.update(
                    status=cached["status"], phase="complete", output_path=str(destination / "daily-material-brief.md"),
                    errors=cached.get("errors") or [], counts=cached.get("counts") or {},
                    usage={**(cached.get("usage") or {}), "cache_status": "warm_hit", "network_requests": 0, "elapsed_seconds": elapsed},
                )
                return {
                    "status": cached["status"], "cache_status": "warm_hit", "network_requests": 0,
                    "elapsed_seconds": elapsed, "stories": validation["stories"], "images": validation["images"],
                    "output_dir": str(destination), "markdown_path": str(destination / "daily-material-brief.md"),
                    "json_path": str(destination / "daily-material-pack.json"), "manifest_path": str(destination / "package-manifest.json"),
                }
        except DailyMaterialPackError:
            pass
    stage_parent = _project_path(config, settings["temp_root"])
    stage_parent.mkdir(parents=True, exist_ok=True)
    stage: Path | None = Path(tempfile.mkdtemp(prefix="daily-pack-", dir=stage_parent))
    try:
        with JobLock(config, "daily_material_pack"):
            state.update(status="running", phase="assemble", output_path="", errors=[], counts={"stories": len(selection["stories"])})
            stories: list[dict[str, Any]] = []
            errors: list[dict[str, str]] = []
            for story in selection["stories"]:
                if clock() - started > float(settings["max_wall_seconds"]):
                    result = _story_stub(story, "整包墙钟预算已耗尽，未继续处理该故事。")
                    stories.append(result)
                    errors.extend(result["errors"])
                    continue
                try:
                    stories.append(_build_story(config, stage, story, max_assets))
                except (DailyMaterialPackError, OSError, ValueError) as exc:
                    message = f"{type(exc).__name__}: {str(exc)[:300]}"
                    stories.append(_story_stub(story, message))
                    errors.append({"story_id": story["story_id"], "message": message})
            image_count = sum(len(row["assets"]) for row in stories)
            ready_count = sum(bool(row["assets"]) for row in stories)
            status = "success" if ready_count == len(stories) and not errors else "partial" if stories else "failed"
            generated_at = datetime.now(ZoneInfo(str(config["timezone"]))).isoformat(timespec="seconds")
            pack = {
                "pack_version": PACK_VERSION, "builder_version": PACK_BUILDER_VERSION, "image_gate_version": IMAGE_GATE_VERSION, "generated_at": generated_at,
                "business_date": selection["business_date"], "run_id": run_id, "status": status,
                "selection_input": selected_path.relative_to(_project_root(config)).as_posix(),
                "selection_source": selection.get("selection_source") or "unknown", "cache_key": cache_key,
                "dependencies": dependencies, "stories": stories,
                "counts": {"stories": len(stories), "stories_with_images": ready_count, "images": image_count, "errors": len(errors)},
                "usage": {
                    "quick": bool(quick), "cache_status": "cold_build", "network_requests": 0, "downloaded_bytes": 0,
                    "max_network_requests": int(settings["max_network_requests"]), "max_download_bytes": int(settings["max_download_bytes"]),
                    "elapsed_seconds": 0.0, "max_wall_seconds": float(settings["max_wall_seconds"]),
                    "llm_calls": 0, "asr_calls": 0, "audio_calls": 0, "douyin_calls": 0, "browser_calls": 0,
                },
                "errors": errors, "manifest_path": "package-manifest.json",
                "openmontage": {
                    "contract_version": "1.0", "root": "openmontage",
                    "snapshots": ["openmontage/hotboard.json", "openmontage/benchmark_accounts.json", "openmontage/content_candidates.json", "openmontage/run_report.json"],
                    "openmontage_modified": False,
                },
                "rights_notice": "所有最终图片均为review_required；供应包不构成商业授权。",
            }
            pack["usage"]["elapsed_seconds"] = round(clock() - started, 3)
            atomic_write_json(stage / "daily-material-pack.json", pack)
            (stage / "daily-material-brief.md").write_text(_markdown(pack), encoding="utf-8", newline="\n")
            export_outputs(stage / "openmontage", _openmontage_payloads(pack))
            _write_manifest(stage)
            validate_daily_material_pack(stage / "daily-material-pack.json")
            _publish_directory(stage, destination)
            stage = None
            state.update(status=status, phase="complete", output_path=str(destination / "daily-material-brief.md"), errors=errors, counts=pack["counts"], usage=pack["usage"])
            return {
                "status": status, "cache_status": "cold_build", "network_requests": 0,
                "elapsed_seconds": pack["usage"]["elapsed_seconds"], "stories": len(stories), "images": image_count,
                "output_dir": str(destination), "markdown_path": str(destination / "daily-material-brief.md"),
                "json_path": str(destination / "daily-material-pack.json"), "manifest_path": str(destination / "package-manifest.json"),
            }
    finally:
        if stage is not None and stage.exists() and stage.is_dir():
            shutil.rmtree(stage, ignore_errors=True)


def latest_daily_material_pack_report(config: dict[str, Any]) -> Path | None:
    root = _project_path(config, config["jobs"]["daily_material_pack"]["output_root"])
    reports = list(root.glob("*/*/daily-material-brief.md")) if root.exists() else []
    return max(reports, key=lambda path: path.stat().st_mtime) if reports else None


def resolve_daily_material_pack_input(config: dict[str, Any]) -> Path:
    """Select the configured valid input; never guess between same-date alternatives."""
    configured = _project_path(config, config["jobs"]["daily_material_pack"]["selection_input"])
    if configured.is_file():
        validate_selection(_load_json(configured))
        return configured
    config_root = _project_root(config) / "config"
    valid: list[tuple[str, Path]] = []
    for path in config_root.glob("daily_material_pack_*.json"):
        try:
            payload = validate_selection(_load_json(path))
        except DailyMaterialPackError:
            continue
        valid.append((payload["business_date"], path))
    if not valid:
        raise DailyMaterialPackError("没有找到符合合同的每日素材包输入；请先生成selection JSON。")
    latest_date = max(value[0] for value in valid)
    latest = [path for target_date, path in valid if target_date == latest_date]
    if len(latest) != 1:
        raise DailyMaterialPackError(f"最新日期{latest_date}存在{len(latest)}个有效输入，无法唯一选择；请在配置中指定selection_input。")
    return latest[0]
