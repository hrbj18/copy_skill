from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .exporter import atomic_write_json, export_outputs
from .job_runtime import JobLock, JobState
from .material_probe import (
    MaterialProbeError,
    RequestBudget,
    SafeFetcher,
    _decode_image,
    redact_url,
)


EXCHANGE_VERSION = "1.0"
EXCHANGE_BUILDER_VERSION = "2026-08-29-v1"
STORY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
FORBIDDEN_FIELD_RE = re.compile(r"(?:api[_-]?key|authorization|cookie|password|secret|token|profile)", re.I)
FORBIDDEN_VISUAL_KINDS = {"video_screenshot", "social_ui_screenshot", "old_event_photo", "logo", "avatar", "qrcode", "generic_brand", "watermark_collage"}
ALLOWED_HEAT_STATUS = {"measured_multi_signal", "measured_single_account", "public_signal_only", "not_supplied"}
FACT_READY = {"confirmed_official", "confirmed_two_reliable"}


class DailyMaterialExchangeError(ValueError):
    """A bounded exchange publish or consumer validation error."""


def _root(config: dict[str, Any]) -> Path:
    return Path(str(config.get("_project_root") or Path(__file__).resolve().parents[2]))


def _path(config: dict[str, Any], value: str | Path) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else _root(config) / candidate


def _relative(value: str) -> Path:
    candidate = Path(str(value or ""))
    if not str(candidate) or candidate.is_absolute() or ".." in candidate.parts:
        raise DailyMaterialExchangeError("交换区路径必须是安全相对路径")
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def beijing_yesterday(now: datetime | None = None) -> str:
    current = now.astimezone(ZoneInfo("Asia/Shanghai")) if now is not None else datetime.now(ZoneInfo("Asia/Shanghai"))
    return (current.date() - timedelta(days=1)).isoformat()


def date_directory(business_date: str) -> str:
    try:
        parsed = date.fromisoformat(business_date)
    except ValueError as exc:
        raise DailyMaterialExchangeError("business_date 必须是 YYYY-MM-DD") from exc
    return f"{parsed.isoformat()}_每日素材"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DailyMaterialExchangeError(f"JSON 无法读取：{path.name}") from exc
    if not isinstance(value, dict):
        raise DailyMaterialExchangeError("JSON 根对象必须是对象")
    return value


def _walk_forbidden(value: Any, prefix: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            if FORBIDDEN_FIELD_RE.search(str(key)):
                raise DailyMaterialExchangeError(f"交换输入包含敏感字段：{name}")
            _walk_forbidden(item, name)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk_forbidden(item, f"{prefix}[{index}]")


def _clean_list(value: Any) -> list[str]:
    return [str(item).strip() for item in (value or []) if str(item).strip()]


def _validate_source(item: Any, label: str) -> dict[str, str]:
    if not isinstance(item, dict):
        raise DailyMaterialExchangeError(f"{label} 格式无效")
    url = redact_url(str(item.get("url") or "").strip())
    if not url.startswith("https://"):
        raise DailyMaterialExchangeError(f"{label} 必须使用 HTTPS")
    title = str(item.get("name") or "来源").strip()
    expected = str(item.get("expected_text") or "").strip()
    if not title or not expected:
        raise DailyMaterialExchangeError(f"{label} 缺少名称或核对文本")
    result = {"name": title[:120], "url": url, "expected_text": expected[:160], "use": str(item.get("use") or "事实核对").strip()[:120]}
    evidence_path = str(item.get("evidence_path") or "").strip()
    if evidence_path:
        result["evidence_path"] = _relative(evidence_path).as_posix()
        evidence_sha256 = str(item.get("evidence_sha256") or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{64}", evidence_sha256):
            raise DailyMaterialExchangeError(f"{label} 本地证据必须提供 SHA-256")
        result["evidence_sha256"] = evidence_sha256
    return result


def validate_exchange_input(payload: dict[str, Any]) -> dict[str, Any]:
    _walk_forbidden(payload)
    if payload.get("schema_version") != "1.0":
        raise DailyMaterialExchangeError("exchange 输入 schema_version 必须为1.0")
    business_date = str(payload.get("business_date") or "")
    date_directory(business_date)
    simulated = str(payload.get("simulated_run_at") or "")
    if not simulated.startswith(f"{business_date[:4]}-08-29T02:00:00+08:00"):
        raise DailyMaterialExchangeError("模拟时间必须冻结为 2026-08-29T02:00:00+08:00")
    stories = payload.get("stories")
    if not isinstance(stories, list) or not 4 <= len(stories) <= 6:
        raise DailyMaterialExchangeError("交换输入必须含4到6条候选")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ordinal, raw in enumerate(stories, 1):
        if not isinstance(raw, dict):
            raise DailyMaterialExchangeError("候选必须是对象")
        story_id = str(raw.get("story_id") or "").strip()
        if not STORY_ID_RE.fullmatch(story_id) or story_id in seen:
            raise DailyMaterialExchangeError("story_id 无效或重复")
        seen.add(story_id)
        published_at = str(raw.get("content_published_at") or "")
        if not published_at.startswith(business_date):
            raise DailyMaterialExchangeError(f"{story_id} 内容发布时间不在业务日期窗口")
        heat = raw.get("heat") or {}
        heat_status = str(heat.get("status") or "")
        signals = heat.get("public_signals") or []
        if heat_status not in ALLOWED_HEAT_STATUS:
            raise DailyMaterialExchangeError(f"{story_id} heat_status 无效")
        if heat_status == "public_signal_only" and not signals:
            raise DailyMaterialExchangeError(f"{story_id} 缺少公开发现信号")
        normalized_signals = []
        for signal in signals:
            source = _validate_source(signal, "公开信号")
            observed = str(signal.get("published_at") or "")
            if not observed.startswith(business_date):
                raise DailyMaterialExchangeError(f"{story_id} 公开信号日期越界")
            normalized_signals.append({**source, "published_at": observed, "signal_kind": str(signal.get("signal_kind") or "reliable_publication")})
        interactions = heat.get("interactions")
        if interactions is not None and not isinstance(interactions, dict):
            raise DailyMaterialExchangeError(f"{story_id} interactions 必须是对象或null")
        sources = [_validate_source(item, "事实来源") for item in (raw.get("fact_sources") or [])]
        evidence_status = str(raw.get("evidence_status") or "research")
        if evidence_status in FACT_READY and len(sources) < (1 if evidence_status == "confirmed_official" else 2):
            raise DailyMaterialExchangeError(f"{story_id} 事实门来源不足")
        images: list[dict[str, Any]] = []
        for image in raw.get("images") or []:
            if not isinstance(image, dict):
                raise DailyMaterialExchangeError(f"{story_id} 图片格式无效")
            kind = str(image.get("visual_kind") or "")
            qa = image.get("visual_qa") or {}
            if kind in FORBIDDEN_VISUAL_KINDS or not all(bool(image.get(key)) for key in ("subject_match", "event_match", "visual_usable")):
                raise DailyMaterialExchangeError(f"{story_id} 最终图片未通过三道门")
            if not isinstance(qa, dict) or qa.get("reviewed") is not True or not all(str(qa.get(key) or "").strip() for key in ("content", "accuracy", "duplicate_or_wrong", "watermark_text", "clarity", "orientation")):
                raise DailyMaterialExchangeError(f"{story_id} 图片缺少完整人工QA")
            image_url = redact_url(str(image.get("image_url") or ""))
            source_article_url = redact_url(str(image.get("source_article_url") or ""))
            if not image_url.startswith("https://"):
                raise DailyMaterialExchangeError(f"{story_id} 图片必须使用 HTTPS")
            if not source_article_url.startswith("https://"):
                raise DailyMaterialExchangeError(f"{story_id} 图片来源页必须使用 HTTPS")
            images.append({
                "role": "primary" if not images else "backup", "image_url": image_url,
                "source_article_url": source_article_url,
                "source_name": str(image.get("source_name") or "来源页").strip()[:120],
                "visual_kind": kind, "selection_reason": str(image.get("selection_reason") or "通过三道门").strip()[:300],
                "attribution_text": str(image.get("attribution_text") or "来源页未见单独摄影署名").strip()[:240],
                "composition_hint": str(image.get("composition_hint") or "按原画幅使用").strip()[:200],
                "crop_hint": str(image.get("crop_hint") or "避免裁掉关键信息").strip()[:200],
                "visual_qa": qa,
            })
        if len(images) > 2:
            raise DailyMaterialExchangeError(f"{story_id} 最多1主+1备图")
        result.append({
            "story_id": story_id, "title_zh": str(raw.get("title_zh") or "").strip()[:180],
            "event_at": str(raw.get("event_at") or published_at), "content_published_at": published_at,
            "editorial_order": ordinal, "heat": {"status": heat_status, "account_coverage": int(heat.get("account_coverage") or 0), "accounts": _clean_list(heat.get("accounts")), "interactions": interactions, "public_signals": normalized_signals},
            "evidence_status": evidence_status, "confirmed_facts": _clean_list(raw.get("confirmed_facts")),
            "pending_verification": _clean_list(raw.get("pending_verification")), "claims_to_verify": _clean_list(raw.get("claims_to_verify")), "do_not_claim": _clean_list(raw.get("do_not_claim")),
            "fact_sources": sources, "user_value": str(raw.get("user_value") or "").strip()[:500],
            "editorial_angle": str(raw.get("editorial_angle") or "").strip()[:500], "visual_intent": str(raw.get("visual_intent") or "").strip()[:500],
            "images": images, "rejected_visuals": _clean_list(raw.get("rejected_visuals")),
        })
    return {"schema_version": "1.0", "business_date": business_date, "simulated_run_at": simulated, "selection_source": str(payload.get("selection_source") or "public_web_reuse"), "stories": result}


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _verify_source(fetcher: SafeFetcher, source: dict[str, str], *, maximum_bytes: int, project_root: Path | None = None) -> dict[str, str]:
    if source.get("evidence_path"):
        if project_root is None:
            raise DailyMaterialExchangeError("本地热度证据缺少项目根目录")
        evidence_file = project_root / _relative(source["evidence_path"])
        if not evidence_file.is_file() or _sha256(evidence_file) != source.get("evidence_sha256"):
            raise DailyMaterialExchangeError(f"本地热度证据 hash 不匹配：{source['name']}")
        text = evidence_file.read_text(encoding="utf-8", errors="replace")
        if source["expected_text"] not in text:
            raise DailyMaterialExchangeError(f"本地热度证据未找到：{source['name']}")
        return {**source, "verified_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"), "verification_mode": "local_ranked_snapshot"}
    url, _content_type, body = fetcher.get(source["url"], maximum_bytes=maximum_bytes, accepted_types=("text/html", "text/*"))
    text = body.decode("utf-8", errors="replace")
    if source["expected_text"] not in text:
        raise DailyMaterialExchangeError(f"来源核对文本未找到：{source['name']}")
    published_at = str(source.get("published_at") or "")
    if published_at:
        try:
            published_date = date.fromisoformat(published_at[:10])
        except ValueError as exc:
            raise DailyMaterialExchangeError(f"来源日期无效：{source['name']}") from exc
        date_markers = (
            published_date.isoformat(),
            f"{published_date.year}/{published_date.month}/{published_date.day}",
            f"{published_date.year}年{published_date.month}月{published_date.day}日",
        )
        if not any(marker in text for marker in date_markers):
            raise DailyMaterialExchangeError(f"公开信号页面未见业务日期：{source['name']}")
    return {**source, "url": redact_url(url), "verified_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")}


def _write_manifest(pack_dir: Path) -> dict[str, Any]:
    files = []
    excluded = {"package-manifest.json", "_READY.json"}
    for path in sorted(item for item in pack_dir.rglob("*") if item.is_file() and item.name not in excluded):
        files.append({"path": path.relative_to(pack_dir).as_posix(), "bytes": path.stat().st_size, "sha256": _sha256(path)})
    payload = {"manifest_version": EXCHANGE_VERSION, "self_excluded": sorted(excluded), "files": files}
    atomic_write_json(pack_dir / "package-manifest.json", payload)
    return payload


def _validate_manifest(pack_dir: Path, manifest: dict[str, Any]) -> int:
    listed: set[str] = set()
    for row in manifest.get("files") or []:
        relative = _relative(str(row.get("path") or ""))
        target = pack_dir / relative
        if not target.is_file() or target.stat().st_size != int(row.get("bytes") or -1) or _sha256(target) != row.get("sha256"):
            raise DailyMaterialExchangeError(f"manifest 校验失败：{relative.as_posix()}")
        listed.add(relative.as_posix())
    actual = {item.relative_to(pack_dir).as_posix() for item in pack_dir.rglob("*") if item.is_file() and item.name not in {"package-manifest.json", "_READY.json"}}
    if listed != actual:
        raise DailyMaterialExchangeError("manifest 未完整覆盖可校验包文件")
    return len(listed)


def _story_markdown(story: dict[str, Any]) -> str:
    lines = [f"# {story['title_zh']}", "", f"- `story_id`：`{story['story_id']}`", f"- 业务日期：`{story['business_date']}`", f"- 热度状态：`{story['heat']['status']}`；账号覆盖：{story['heat']['account_coverage']}；互动：{json.dumps(story['heat']['interactions'], ensure_ascii=False) if story['heat']['interactions'] is not None else '未提供'}", f"- 事实状态：`{story['evidence_status']}`；素材状态：`{story['material_status']}`", "", "## 已确认事实", ""]
    lines.extend(f"- {value}" for value in story["confirmed_facts"] or ["无；保持研究状态。"])
    for label, values in (("待核实", story["pending_verification"]), ("禁止宣称", story["do_not_claim"]), ("可用角度", [story["editorial_angle"]]), ("普通用户价值", [story["user_value"]]), ("视觉意图", [story["visual_intent"]])):
        lines.extend(["", f"## {label}", ""])
        lines.extend(f"- {value}" for value in values if value)
        if not any(values): lines.append("- 无")
    lines.extend(["", "## 来源", ""])
    for item in story["fact_sources"]:
        lines.append(f"- [{item['name']}]({item['url']})（{item['use']}）")
    lines.extend(["", "## 图片", ""])
    for image in story["assets"]:
        qa = image["visual_qa"]
        lines.append(f"- {image['role']}：`{image['relative_path']}`，{image['width']}×{image['height']}，`review_required`；{image['selection_reason']}")
        lines.append(f"  - QA：{qa['content']}；准确性：{qa['accuracy']}；重复/错图：{qa['duplicate_or_wrong']}；水印：{qa['watermark_text']}；清晰度：{qa['clarity']}；画幅：{qa['orientation']}")
    if not story["assets"]: lines.append("- 无合格图片；不得用泛化图或错误事件补位。")
    return "\n".join(lines) + "\n"


def _brief(pack: dict[str, Any]) -> str:
    lines = ["# 昨日热点素材简报", "", f"模拟时间：`{pack['simulated_run_at']}`　业务日期：`{pack['business_date']}`　状态：`{pack['status']}`", "", "> 热度与事实独立记录；public_signal_only 表示有真实公开发现信号但没有伪造账号互动。所有图片均需权利复核。", "", "| 顺序 | 新闻 | 热度 | 事实 | 图片 |", "|---:|---|---|---|---:|"]
    for story in pack["stories"]:
        lines.append(f"| {story['editorial_order']} | {story['title_zh']} | `{story['heat']['status']}` | `{story['evidence_status']}` | {len(story['assets'])} |")
    for story in pack["stories"]:
        lines.extend(["", f"## {story['editorial_order']}. {story['title_zh']}", "", f"- 热度信号：`{story['heat']['status']}`；账号覆盖 {story['heat']['account_coverage']}；互动 {'未提供' if story['heat']['interactions'] is None else json.dumps(story['heat']['interactions'], ensure_ascii=False)}"])
        for signal in story["heat"]["public_signals"]:
            lines.append(f"  - [{signal['name']}]({signal['url']})，发布时间 `{signal['published_at']}`，{signal['signal_kind']}")
        lines.append(f"- 事实状态：`{story['evidence_status']}`；素材状态：`{story['material_status']}`")
        if story["confirmed_facts"]:
            lines.extend(f"  - 已确认：{fact}" for fact in story["confirmed_facts"])
        else:
            lines.append("  - 尚未确认：本条仅作为热度研究候选，不可进入事实播报。")
            for label, values in (("待核实", story["pending_verification"]), ("待核对声明", story["claims_to_verify"]), ("禁止宣称", story["do_not_claim"])):
                if values:
                    lines.append(f"  - {label}：")
                    lines.extend(f"    - {value}" for value in values)
        lines.append("- 图片：" + ("；".join(f"{asset['role']} `{asset['relative_path']}` {asset['width']}×{asset['height']}" for asset in story["assets"]) or "无合格图"))
        if story["rejected_visuals"]: lines.append("- 已拒素材：" + "；".join(story["rejected_visuals"]))
    lines.extend(["", "## 运行边界", "", f"- 网络请求：{pack['usage']['network_requests']} / {pack['usage']['max_network_requests']}；下载：{pack['usage']['downloaded_bytes']} bytes", f"- 耗时：{pack['usage']['elapsed_seconds']} / {pack['usage']['max_wall_seconds']} 秒", "- LLM / ASR / 音频 / 抖音 / 浏览器：0；OpenMontage 写入：0；未安装计划任务。", ""])
    return "\n".join(lines)


def _op_rules() -> str:
    return """# OP 每日素材读取规则（每日 02:00 素材交换区 V1）

## 固定入口与职责

- 生产者只在 `D:\\work\\copy_skill\\output\\每日新闻素材\\` 发布包；消费者只读该根目录，不得写回或修改源包。
- OpenMontage 负责最终选题、双主持写稿、冷审、镜头和发布；本包不能触发模型付费视频阶段。

## 日期与定位

- 未指定日期时，用北京时间自然日减一天，不使用滚动 24 小时。
- 日期目录固定为 `YYYY-MM-DD_每日素材`。消费者必须用业务日期进入该目录，读取 `current.json`，不可用根级 `latest.json` 替代日期定位。
- `current.json` 仅含相对 `pack_relative_path` 与 manifest SHA；再读取该包的 `_READY.json`、`daily-material-pack.json`、`package-manifest.json`。

## 安全校验和降级

- 拒绝绝对路径、`..`、错误业务日期、缺 `_READY.json`、manifest SHA 不符、文件 hash/字节/尺寸不符的包。
- `_READY.json` 的 `partial` 可消费，但必须把风险、缺图和待核实项带入下游；缺目录、无 READY 或合同损坏应停止并报告，不猜测或补造。
- 源包与图片不可原地修改；导入使用 `run_id + package_manifest_sha256` 幂等，复制所选图片到消费者自己的缓存。

## 事实、热度与写作

- 以 `story_id` 连接，禁止仅按标题匹配。
- `heat_status`、账号覆盖和互动只反映真实采集到的信号；`public_signal_only` 不等于账号热榜，缺失互动保持 null。
- 仅 `confirmed_official` 或 `confirmed_two_reliable` 的 confirmed facts 可进入可播候选；pending、claims_to_verify 与 do_not_claim 必须进入写稿和冷审。

## 图片与权利

- 只使用通过主体、事件、画面可用性三道门且已记录 QA 的 primary/backup；禁止视频/社交 UI 截图、旧事件照、头像、纯 Logo、二维码、泛化图和近重复。
- `review_required` 不是商业授权；保留来源、署名、权利状态和裁切提示。无图时降级为无图，不以无关图补位。

## 路由要求

- 将本规则复制进 OP 后，必须由 OP 的 AGENT_GUIDE / 每日快报路由显式引用；文件存在本身不会自动被读取。
"""


def _openmontage_snapshots(pack: dict[str, Any]) -> dict[str, Any]:
    return {
        "hotboard.json": {"captured_at": pack["generated_at"], "target_date": pack["business_date"], "items": []},
        "benchmark_accounts.json": {"captured_at": pack["generated_at"], "target_date": pack["business_date"], "videos": [], "heat_status": "public_signal_only"},
        "content_candidates.json": {"version": "1.0", "captured_at": pack["generated_at"], "target_date": pack["business_date"], "items": [{"story_id": story["story_id"], "title": story["title_zh"], "heat_status": story["heat"]["status"], "evidence_status": story["evidence_status"], "primary_image": story["assets"][0]["relative_path"] if story["assets"] else None} for story in pack["stories"]]},
        "run_report.json": {"status": pack["status"], "target_date": pack["business_date"], "counts": pack["counts"], "source": "daily_material_exchange_v1", "openmontage_modified": False},
    }


def _hamming_distance(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _build_story(
    stage: Path,
    story: dict[str, Any],
    fetcher: SafeFetcher,
    settings: dict[str, Any],
    *,
    seen_sha256: set[str],
    seen_dhashes: set[str],
    project_root: Path,
) -> dict[str, Any]:
    verified_signals = [_verify_source(fetcher, source, maximum_bytes=int(settings["max_html_bytes"]), project_root=project_root) for source in story["heat"]["public_signals"]]
    verified_facts = []
    for source in story["fact_sources"]:
        try:
            verified_facts.append(_verify_source(fetcher, source, maximum_bytes=int(settings["max_html_bytes"]), project_root=project_root))
        except (MaterialProbeError, DailyMaterialExchangeError) as exc:
            if story["evidence_status"] in FACT_READY:
                raise
            verified_facts.append({**source, "verification_error": str(exc)[:180]})
    target = stage / "stories" / story["story_id"]
    target.mkdir(parents=True, exist_ok=True)
    accepted: list[dict[str, Any]] = []
    asset_errors: list[str] = []
    for image in story["images"]:
        try:
            url, content_type, body = fetcher.get(image["image_url"], maximum_bytes=int(settings["max_asset_bytes"]), accepted_types=("image/jpeg", "image/png", "image/webp"))
            details = _decode_image(body, content_type, int(settings["min_dimension"]))
            sha = hashlib.sha256(body).hexdigest()
            if sha in seen_sha256:
                raise DailyMaterialExchangeError(f"{story['story_id']} 图片 SHA 与包内其他故事重复")
            if any(_hamming_distance(details["dhash"], prior) <= 4 for prior in seen_dhashes):
                raise DailyMaterialExchangeError(f"{story['story_id']} 图片感知哈希与包内其他故事近重复")
            name = "primary" if not accepted else f"backup-{len(accepted):02d}"
            relative = Path("stories") / story["story_id"] / f"{name}{details['extension']}"
            destination = stage / relative
            destination.write_bytes(body)
            seen_sha256.add(sha)
            seen_dhashes.add(details["dhash"])
            accepted.append({
                **image, "role": name, "relative_path": relative.as_posix(), "image_source_url": redact_url(url), "sha256": sha,
                "mime_type": details["mime_type"], "width": details["width"], "height": details["height"], "bytes": len(body), "dhash": details["dhash"], "rights_status": "review_required",
            })
        except (MaterialProbeError, DailyMaterialExchangeError, OSError) as exc:
            asset_errors.append(str(exc)[:240])
    material_status = "ready" if story["evidence_status"] in FACT_READY and accepted else "partial" if accepted or story["evidence_status"] in FACT_READY else "research"
    result = {**story, "business_date": story["business_date"], "heat": {**story["heat"], "public_signals": verified_signals}, "fact_sources": verified_facts, "assets": accepted, "asset_errors": asset_errors, "material_status": material_status}
    atomic_write_json(target / "story.json", result)
    _write_text(target / "文案素材.md", _story_markdown(result))
    return result


def _failed_story_stub(stage: Path, story: dict[str, Any], *, business_date: str, error: str) -> dict[str, Any]:
    """Preserve a traceable candidate when one bounded external check fails."""
    target = stage / "stories" / story["story_id"]
    target.mkdir(parents=True, exist_ok=True)
    result = {
        **story,
        "business_date": business_date,
        "evidence_status": "research",
        "confirmed_facts": [],
        "pending_verification": [*story.get("pending_verification", []), "本次受限联网核对未完成，不能作为可播事实。"],
        "assets": [],
        "material_status": "research",
        "verification_error": error[:240],
    }
    atomic_write_json(target / "story.json", result)
    _write_text(target / "文案素材.md", _story_markdown(result))
    return result


def _counts(stories: list[dict[str, Any]]) -> dict[str, int]:
    return {"stories": len(stories), "fact_ready": sum(story["evidence_status"] in FACT_READY for story in stories), "stories_with_primary": sum(bool(story["assets"]) for story in stories), "fact_ready_with_primary": sum(story["evidence_status"] in FACT_READY and bool(story["assets"]) for story in stories), "images": sum(len(story["assets"]) for story in stories), "real_signal_stories": sum(bool(story["heat"]["public_signals"]) for story in stories)}


def simulate_daily_material_exchange(config: dict[str, Any], *, business_date: str | None = None, input_path: str | Path | None = None, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    settings = config["jobs"]["daily_material_exchange"]
    selected = _path(config, input_path or settings["selection_input"])
    selection = validate_exchange_input(_load_json(selected))
    target_date = business_date or beijing_yesterday()
    if target_date != selection["business_date"]:
        raise DailyMaterialExchangeError("显式业务日期必须与冻结输入一致")
    root = _path(config, settings["output_root"])
    date_root = root / date_directory(target_date)
    run_id = f"run-{target_date.replace('-', '')}-{uuid.uuid4().hex[:12]}"
    stage = root / ".staging" / run_id
    stage.mkdir(parents=True, exist_ok=False)
    started = clock()
    state = JobState(config, "daily_material_exchange")
    budget = RequestBudget(int(settings["max_network_requests"]), int(settings["max_download_bytes"]), float(settings["max_wall_seconds"]), started)
    fetcher = SafeFetcher(settings, budget)
    try:
        with JobLock(config, "daily_material_exchange"):
            state.update(status="running", phase="verify_and_assemble", output_path="", errors=[], counts={"stories": len(selection["stories"])})
            stories: list[dict[str, Any]] = []
            errors: list[dict[str, str]] = []
            seen_sha256: set[str] = set()
            seen_dhashes: set[str] = set()
            for story in selection["stories"]:
                if clock() - started > float(settings["max_wall_seconds"]):
                    errors.append({"story_id": story["story_id"], "message": "整包墙钟预算耗尽"})
                    continue
                try:
                    stories.append(_build_story(
                        stage, {**story, "business_date": target_date}, fetcher, settings,
                        seen_sha256=seen_sha256, seen_dhashes=seen_dhashes, project_root=_root(config),
                    ))
                except (MaterialProbeError, DailyMaterialExchangeError, OSError) as exc:
                    errors.append({"story_id": story["story_id"], "message": str(exc)[:240]})
                    stories.append(_failed_story_stub(stage, story, business_date=target_date, error=str(exc)))
            counts = _counts(stories)
            for story in stories:
                for asset_error in story.get("asset_errors") or []:
                    errors.append({"story_id": story["story_id"], "message": f"图片未入选：{asset_error}"})
            status = "success" if len(stories) == len(selection["stories"]) and not errors and counts["fact_ready_with_primary"] >= 3 else "partial" if stories else "failed"
            generated_at = datetime.now(ZoneInfo(str(config["timezone"]))).isoformat(timespec="seconds")
            pack = {"contract_version": EXCHANGE_VERSION, "builder_version": EXCHANGE_BUILDER_VERSION, "producer": "copy_skill", "business_date": target_date, "simulated_run_at": selection["simulated_run_at"], "generated_at": generated_at, "run_id": run_id, "status": status, "selection_input": selected.relative_to(_root(config)).as_posix(), "selection_source": selection["selection_source"], "stories": stories, "counts": counts, "errors": errors, "usage": {"network_requests": budget.request_count, "downloaded_bytes": budget.downloaded_bytes, "max_network_requests": budget.max_requests, "max_download_bytes": budget.max_total_bytes, "elapsed_seconds": round(clock() - started, 3), "max_wall_seconds": budget.total_timeout_seconds, "llm_calls": 0, "asr_calls": 0, "audio_calls": 0, "douyin_calls": 0, "browser_calls": 0}, "openmontage_modified": False, "rights_review_required": True}
            atomic_write_json(stage / "daily-material-pack.json", pack)
            _write_text(stage / "昨日热点素材简报.md", _brief(pack))
            export_outputs(stage / "openmontage", _openmontage_snapshots(pack))
            atomic_write_json(stage / "consumer-dry-run.json", {"mode": "prepublish_contract_check", "business_date": target_date, "run_id": run_id, "requires_root_date_inspect": True, "status": "validatable"})
            manifest = _write_manifest(stage)
            manifest_sha = _sha256(stage / "package-manifest.json")
            ready = {"contract_version": EXCHANGE_VERSION, "producer": "copy_skill", "business_date": target_date, "run_id": run_id, "generated_at": generated_at, "status": status, "human_brief": "昨日热点素材简报.md", "machine_contract": "daily-material-pack.json", "manifest": "package-manifest.json", "counts": counts, "heat_summary": {"real_signal_stories": counts["real_signal_stories"], "account_signal_stories": 0}, "fact_summary": {"fact_ready": counts["fact_ready"]}, "missing": errors, "package_manifest_sha256": manifest_sha, "usage": pack["usage"], "openmontage_modified": False, "rights_review_required": True}
            atomic_write_json(stage / "_READY.json", ready)
            _validate_manifest(stage, manifest)
            destination = date_root / "packs" / run_id
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, destination)
            current = {"contract_version": EXCHANGE_VERSION, "business_date": target_date, "status": status, "pack_relative_path": f"packs/{run_id}", "ready_relative_path": f"packs/{run_id}/_READY.json", "package_manifest_sha256": manifest_sha, "updated_at": generated_at}
            atomic_write_json(date_root / "current.json", current)
            atomic_write_json(root / "latest.json", {"contract_version": EXCHANGE_VERSION, "business_date": target_date, "date_directory": date_directory(target_date), "current_relative_path": f"{date_directory(target_date)}/current.json", "status": status, "updated_at": generated_at})
            _write_text(root / "OP每日素材读取规则.md", _op_rules())
            consumer_result = inspect_daily_material_exchange(config, business_date=target_date)
            atomic_write_json(date_root / "consumer-dry-run.json", {"mode": "root_plus_business_date", "executed_at": datetime.now(ZoneInfo(str(config["timezone"]))).isoformat(timespec="seconds"), "result": consumer_result})
            state.update(status=status, phase="published", output_path=str(destination / "昨日热点素材简报.md"), errors=errors, counts=counts, usage=pack["usage"])
            return {"status": status, "business_date": target_date, "run_id": run_id, "output_dir": str(destination), "brief_path": str(destination / "昨日热点素材简报.md"), "json_path": str(destination / "daily-material-pack.json"), "ready_path": str(destination / "_READY.json"), "current_path": str(date_root / "current.json"), "latest_path": str(root / "latest.json"), "consumer_report_path": str(date_root / "consumer-dry-run.json"), "counts": counts, "usage": pack["usage"], "errors": errors}
    except Exception as exc:
        atomic_write_json(stage / "failure.json", {"status": "failed", "error_type": type(exc).__name__, "message": re.sub(r"(?i)(cookie|authorization|api[_-]?key|secret)\\s*[:=]\\s*\\S+", r"\\1=[已隐藏]", str(exc))[:300]})
        state.update(status="failed", phase="failed", output_path="", errors=[{"message": str(exc)[:240]}], counts={"stories": 0})
        raise
    finally:
        fetcher.close()


def inspect_daily_material_exchange(config: dict[str, Any], *, business_date: str) -> dict[str, Any]:
    root = _path(config, config["jobs"]["daily_material_exchange"]["output_root"])
    date_root = root / date_directory(business_date)
    current = _load_json(date_root / "current.json")
    if current.get("business_date") != business_date:
        raise DailyMaterialExchangeError("current 业务日期不一致")
    pack_relative = _relative(str(current.get("pack_relative_path") or ""))
    pack_dir = date_root / pack_relative
    ready = _load_json(pack_dir / "_READY.json")
    if ready.get("business_date") != business_date or ready.get("run_id") != pack_dir.name or ready.get("status") not in {"success", "partial", "empty"}:
        raise DailyMaterialExchangeError("READY 无效或与 current 不一致")
    manifest_path = pack_dir / _relative(str(ready.get("manifest") or ""))
    manifest = _load_json(manifest_path)
    manifest_sha = _sha256(manifest_path)
    if manifest_sha != current.get("package_manifest_sha256") or manifest_sha != ready.get("package_manifest_sha256"):
        raise DailyMaterialExchangeError("manifest SHA 与 current/READY 不一致")
    files = _validate_manifest(pack_dir, manifest)
    pack = _load_json(pack_dir / _relative(str(ready.get("machine_contract") or "")))
    if pack.get("business_date") != business_date or pack.get("run_id") != ready.get("run_id"):
        raise DailyMaterialExchangeError("机器合同与 READY 不一致")
    stories = pack.get("candidates", pack.get("stories", []))
    if not isinstance(stories, list):
        raise DailyMaterialExchangeError("机器合同候选字段无效")
    counts = ready.get("counts") or {}
    return {"status": "valid", "consumer_mode": "root_plus_business_date", "business_date": business_date, "run_id": ready["run_id"], "package_path": str(pack_dir), "package_status": ready["status"], "stories": len(stories), "fact_ready": int(counts.get("fact_ready") or 0), "stories_with_primary": int(counts.get("stories_with_primary") or 0), "images": int(counts.get("images") or 0), "manifest_files": files, "partial_risks": ready.get("missing") or [], "openmontage_modified": False}


def latest_daily_material_exchange_report(config: dict[str, Any]) -> Path | None:
    """Return only a published human brief selected through the root pointer."""
    root = _path(config, config["jobs"]["daily_material_exchange"]["output_root"])
    try:
        latest = _load_json(root / "latest.json")
        date_root = root / _relative(str(latest.get("date_directory") or ""))
        current = _load_json(date_root / "current.json")
        pack_dir = date_root / _relative(str(current.get("pack_relative_path") or ""))
        ready = _load_json(pack_dir / "_READY.json")
        report = pack_dir / _relative(str(ready.get("human_brief") or ""))
        return report if report.is_file() else None
    except DailyMaterialExchangeError:
        return None
