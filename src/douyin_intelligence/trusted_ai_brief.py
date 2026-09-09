from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .config import project_root, resolve_path
from .exporter import atomic_write_json
from .job_runtime import JobLock, JobState
from .llm_analysis import OpenAICompatibleAnalyzer
from .reporting import atomic_text


SCHEMA = "trusted-news-ai-brief-v1"
ANALYSIS_ROLE = "editorial_organization_only"
NOTICE = "以下内容仅为对标账号视频与公开互动数据的 AI 整理，未核验新闻真实性。"
INSECURE_HTTP_NOTICE = "当前模型使用用户明确授权的远程 HTTP 明文传输，连接未加密。"
_NUMBER = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?")
_SENSITIVE_KEY = re.compile(r"(?i)(?:api.?key|authorization|cookie|password|token|endpoint|base.?url|websocket|profile|signed)")


def _root(config: dict[str, Any]) -> Path:
    return Path(config.get("_project_root") or project_root()).resolve()


def _settings(config: dict[str, Any]) -> dict[str, Any]:
    return dict((config["jobs"]["trusted_account_news"].get("ai_brief") or {}))


def _safe_reason(exc: BaseException) -> str:
    value = f"{type(exc).__name__}: {exc}".replace("\r", " ").replace("\n", " ")
    value = re.sub(r"(?i)\b(?:https?|wss?)://[^\s]+", "[已隐藏地址]", value)
    value = re.sub(r"(?i)\b(?:authorization|cookie|bearer|password|api[_-]?key|token)\b\s*[:=]?\s*[^\s,;]+", "[已脱敏]", value)
    return value[:500]


def _ranking_path(config: dict[str, Any], value: str | Path) -> Path:
    path = Path(value)
    path = path.resolve() if path.is_absolute() else (_root(config) / path).resolve()
    output_value = Path(config["jobs"]["trusted_account_news"]["output_root"])
    output_root = output_value.resolve() if output_value.is_absolute() else (_root(config) / output_value).resolve()
    try:
        path.relative_to(output_root)
    except ValueError as exc:
        raise ValueError("AI 简报输入必须位于可信账号输出目录") from exc
    if path.name != "ranking.json" or not path.is_file():
        raise FileNotFoundError("找不到可信账号 ranking.json")
    return path


def latest_ranking_path(config: dict[str, Any], account_id: str | None = None) -> Path | None:
    output_value = Path(config["jobs"]["trusted_account_news"]["output_root"])
    root = output_value.resolve() if output_value.is_absolute() else (_root(config) / output_value).resolve()
    search_root = root / account_id if account_id else root
    candidates = list(search_root.glob("*/ranking.json")) if account_id else list(search_root.glob("*/*/ranking.json"))
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def _safe_transcript(report_dir: Path, transcript_path: Any, maximum: int) -> str:
    if not transcript_path:
        return ""
    path = Path(str(transcript_path))
    path = path.resolve() if path.is_absolute() else (report_dir / path).resolve()
    allowed = (report_dir / "ocr").resolve()
    try:
        path.relative_to(allowed)
    except ValueError:
        return ""
    if path.suffix.casefold() != ".json" or not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(payload.get("text") or "").strip()[:maximum]


def build_safe_input(config: dict[str, Any], ranking: dict[str, Any], report_dir: Path) -> list[dict[str, Any]]:
    settings = _settings(config)
    maximum = min(10, max(1, int(settings.get("max_items") or 10)))
    chars_per_item = min(8000, max(500, int(settings.get("max_chars_per_item") or 6000)))
    total_limit = min(50000, max(2000, int(settings.get("max_total_chars") or 40000)))
    rows: list[dict[str, Any]] = []
    used = 0
    for index, item in enumerate((ranking.get("items") or [])[:maximum]):
        metadata = item.get("metadata") or {}
        video_id = str(metadata.get("video_id") or "")
        if not video_id or any(row["video_id"] == video_id for row in rows):
            raise ValueError("原始报告包含空或重复 video_id")
        remaining = max(0, total_limit - used)
        text = _safe_transcript(report_dir, item.get("transcript_path"), min(chars_per_item, remaining))
        used += len(text)
        enrichment = item.get("enrichment") or {}
        rows.append({
            "index": index,
            "video_id": video_id,
            "heat_rank": int(item["heat_rank"]),
            "heat_score": float(item["heat_score"]),
            "interactions": {
                key: (int(value) if isinstance(value, (int, float)) else None)
                for key, value in (metadata.get("interactions") or {}).items()
                if key in {"like", "comment", "collect", "share"}
            },
            "published_at": str(metadata.get("published_at") or ""),
            "ocr_status": {
                "content_source": str((item.get("content") or {}).get("content_source") or "unavailable"),
                "visual_text_status": str((item.get("content") or {}).get("visual_text_status") or "unavailable"),
                "complete": bool((item.get("content") or {}).get("complete")),
            },
            "deterministic_headline": str(enrichment.get("headline") or metadata.get("title") or "未命名内容")[:160],
            "deterministic_summary": str(enrichment.get("one_sentence_summary") or "")[:500],
            "safe_ocr_text": text,
        })
    return rows


def _cache_key(rows: list[dict[str, Any]], model: str, prompt_version: str, schema_version: str) -> str:
    material = {"input": rows, "model": model, "prompt_version": prompt_version, "schema_version": schema_version}
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _numbers(value: Any) -> set[str]:
    return set(_NUMBER.findall(json.dumps(value, ensure_ascii=False, sort_keys=True)))


def _has_sensitive_keys(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_SENSITIVE_KEY.search(str(key)) or _has_sensitive_keys(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_has_sensitive_keys(item) for item in value)
    return False


def validate_model_output(payload: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    if payload.get("analysis_role") != ANALYSIS_ROLE or payload.get("verification_performed") is not False:
        raise ValueError("模型没有遵守仅做编辑整理的边界")
    if _has_sensitive_keys(payload):
        raise ValueError("模型输出包含禁止的配置或认证字段")
    expected = {row["video_id"]: row for row in rows}
    items = payload.get("editorial_items")
    if not isinstance(items, list) or len(items) != len(rows):
        raise ValueError("模型输出遗漏或增加了作品")
    seen_ids: set[str] = set()
    seen_orders: set[int] = set()
    normalized_items: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError("模型榜单项格式错误")
        video_id = str(raw.get("video_id") or "")
        if video_id not in expected or video_id in seen_ids:
            raise ValueError("模型输出包含未知或重复 video_id")
        source = expected[video_id]
        order = int(raw.get("ai_editorial_order") or 0)
        if order < 1 or order > len(rows) or order in seen_orders:
            raise ValueError("AI 易读顺序重复或越界")
        if int(raw.get("source_heat_rank") or 0) != source["heat_rank"]:
            raise ValueError("模型试图修改原始热度名次")
        seen_ids.add(video_id)
        seen_orders.add(order)
        key_points = raw.get("key_points") if isinstance(raw.get("key_points"), list) else []
        normalized_items.append({
            "ai_editorial_order": order,
            "video_id": video_id,
            "source_heat_rank": source["heat_rank"],
            "headline": str(raw.get("headline") or "")[:160],
            "one_sentence_summary": str(raw.get("one_sentence_summary") or "")[:500],
            "key_points": [str(value)[:400] for value in key_points[:3]],
            "organization_reason": str(raw.get("organization_reason") or "")[:500],
            "public_engagement_summary": str(raw.get("public_engagement_summary") or "")[:400],
        })
    if seen_ids != set(expected) or seen_orders != set(range(1, len(rows) + 1)):
        raise ValueError("模型输出没有完整覆盖输入")
    overview = payload.get("daily_overview")
    if not isinstance(overview, list) or not 2 <= len(overview) <= 4:
        raise ValueError("daily_overview 必须包含 2 至 4 句")
    themes: list[dict[str, Any]] = []
    if not isinstance(payload.get("themes"), list):
        raise ValueError("themes 格式错误")
    for raw in payload["themes"][:10]:
        if not isinstance(raw, dict):
            raise ValueError("主题格式错误")
        ids = [str(value) for value in (raw.get("video_ids") or [])]
        if not ids or len(ids) != len(set(ids)) or any(value not in expected for value in ids):
            raise ValueError("主题包含未知或重复 video_id")
        themes.append({"name": str(raw.get("name") or "")[:120], "video_ids": ids, "summary": str(raw.get("summary") or "")[:500]})
    normalized = {
        "daily_overview": [str(value)[:500] for value in overview],
        "themes": themes,
        "editorial_items": sorted(normalized_items, key=lambda item: item["ai_editorial_order"]),
    }
    allowed_numbers = _numbers(rows) | {str(value) for value in range(0, len(rows) + 1)}
    if _numbers(normalized) - allowed_numbers:
        raise ValueError("模型输出包含输入之外的新数字事实")
    return normalized


def _deterministic(rows: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    items = []
    for row in sorted(rows, key=lambda value: (value["heat_rank"], value["video_id"])):
        items.append({
            "ai_editorial_order": row["heat_rank"], "video_id": row["video_id"],
            "source_heat_rank": row["heat_rank"], "headline": row["deterministic_headline"],
            "one_sentence_summary": row["deterministic_summary"], "key_points": [],
            "organization_reason": "大模型整理不可用，按原始热度名次呈现。",
            "public_engagement_summary": "公开互动数据见下方固定分项。",
        })
    return {
        "daily_overview": [f"本次保留 {len(rows)} 条可信账号视频底账。", "当前使用确定性顺序，未执行新闻真实性核验。"],
        "themes": [{"name": "按原始热度顺序", "video_ids": [row["video_id"] for row in rows], "summary": "模型不可用或响应未通过校验，未自动归类。"}] if rows else [],
        "editorial_items": items,
        "degrade_reason": reason,
    }


def _source_lookup(ranking: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str((item.get("metadata") or {}).get("video_id") or ""): item for item in ranking.get("items") or []}


def _assemble(
    *, ranking: dict[str, Any], ranking_path: Path, normalized: dict[str, Any], status: str,
    model: str, prompt_version: str, schema_version: str, cache_key: str,
    request_count: int, network_attempt_count: int, cache_hit: bool,
    transport_security: str, warning: str | None,
) -> dict[str, Any]:
    lookup = _source_lookup(ranking)
    items = []
    for item in normalized["editorial_items"]:
        source = lookup[item["video_id"]]
        metadata = source.get("metadata") or {}
        items.append({
            **item,
            "source_heat_score": source["heat_score"],
            "interactions": metadata.get("interactions") or {},
            "published_at": metadata.get("published_at"),
            "share_url": metadata.get("share_url"),
            "ocr_status": {
                "content_source": (source.get("content") or {}).get("content_source"),
                "visual_text_status": (source.get("content") or {}).get("visual_text_status"),
                "complete": (source.get("content") or {}).get("complete"),
            },
        })
    generated_at = datetime.now(ZoneInfo(str(ranking.get("window", {}).get("timezone") or "Asia/Shanghai"))).isoformat(timespec="seconds")
    output = {
        "version": schema_version, "schema": SCHEMA, "status": status,
        "analysis_role": ANALYSIS_ROLE, "verification_performed": False, "notice": NOTICE,
        "account": {key: (ranking.get("account") or {}).get(key) for key in ("account_id", "name", "source_tier", "editorial_confidence")},
        "window": ranking.get("window") or {},
        "source_artifact": {"filename": ranking_path.name, "sha256": hashlib.sha256(ranking_path.read_bytes()).hexdigest()},
        "counts": {"input_items": len(items), "output_items": len(items)},
        "daily_overview": normalized["daily_overview"], "themes": normalized["themes"], "editorial_items": items,
        "analysis_metadata": {
            "model": model or None, "prompt_version": prompt_version, "schema_version": schema_version,
            "request_count": request_count, "network_attempt_count": network_attempt_count,
            "cache_hit": cache_hit, "transport_security": transport_security, "generated_at": generated_at,
        },
        "warnings": ([INSECURE_HTTP_NOTICE] if transport_security == "insecure_http_user_authorized" else []) + ([warning] if warning else []),
        "artifacts": {"json": str((ranking_path.parent / "ai-brief.json").resolve()), "markdown": str((ranking_path.parent / "ai-brief.md").resolve())},
        "cache_key": cache_key,
    }
    return output


def render_markdown(payload: dict[str, Any]) -> str:
    account = payload.get("account") or {}
    lines = [
        f"# {account.get('name') or '可信账号'} AI 易读简报", "", f"> {NOTICE}", "",
        f"- 时间窗口：{payload['window'].get('start', '-')} 至 {payload['window'].get('end', '-')}（{payload['window'].get('timezone', 'Asia/Shanghai')}）",
        f"- 处理数量：{payload['counts']['input_items']}", f"- 整理状态：{payload['status']}", "",
        "## 一眼看懂", "", *[f"- {value}" for value in payload["daily_overview"]], "",
        "## AI 易读顺序", "",
    ]
    for item in payload["editorial_items"]:
        lines.extend([
            f"### AI #{item['ai_editorial_order']} · 原始热度 #{item['source_heat_rank']} · {item['headline']}", "",
            f"- 一句话：{item['one_sentence_summary']}",
            f"- 公开互动：{json.dumps(item['interactions'], ensure_ascii=False)}",
            f"- 发布时间：{item['published_at']}",
            f"- OCR状态：{json.dumps(item['ocr_status'], ensure_ascii=False)}",
            *[f"- 要点：{value}" for value in item["key_points"]],
            f"- 整理理由：{item['organization_reason']}",
            f"- 互动概述：{item['public_engagement_summary']}",
            f"- 原视频：[{item['share_url']}]({item['share_url']})", "",
        ])
    lines.extend(["## 主题归类", ""])
    for theme in payload["themes"]:
        lines.extend([f"- **{theme['name']}**：{theme['summary']}（作品：{', '.join(theme['video_ids'])}）"])
    metadata = payload["analysis_metadata"]
    transport = metadata.get("transport_security") or "disabled"
    transport_label = {
        "https": "HTTPS 证书校验",
        "insecure_http_user_authorized": "远程 HTTP 明文模式（不安全，用户已授权）",
        "disabled": "未启用",
    }.get(transport, transport)
    lines.extend([
        "", "## 运行说明", "", f"- 模型：{metadata.get('model') or '未调用'}",
        f"- 传输模式：{transport_label}",
        f"- 网络尝试次数：{metadata.get('network_attempt_count', metadata['request_count'])}",
        f"- 成功请求次数：{metadata['request_count']}", f"- 缓存命中：{metadata['cache_hit']}",
        f"- 角色：`{ANALYSIS_ROLE}`", "- 新闻真实性核验：未执行", "",
    ])
    if payload.get("warnings"):
        lines.extend(["## 降级说明", "", *[f"- {value}" for value in payload["warnings"]], ""])
    return "\n".join(lines)


def run_ai_brief(
    config: dict[str, Any], ranking_path: str | Path, *,
    analyzer: OpenAICompatibleAnalyzer | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    path = _ranking_path(config, ranking_path)
    settings = _settings(config)
    state = JobState(config, "trusted_news_ai_brief")
    with JobLock(config, "trusted_news_ai_brief"):
        state.update(status="running", phase="load_report", started_at=datetime.now(ZoneInfo(str(config["timezone"]))).isoformat(timespec="seconds"), output_path=None, errors=[])
        ranking_bytes = path.read_bytes()
        ranking = json.loads(ranking_bytes.decode("utf-8"))
        rows = build_safe_input(config, ranking, path.parent)
        analyzer = analyzer or OpenAICompatibleAnalyzer(config, transport=transport)
        llm_status = analyzer.status()
        model = str(llm_status.get("model") or analyzer.model or "")
        prompt_version = str(settings.get("prompt_version") or "trusted-news-editorial-v1")
        schema_version = str(settings.get("schema_version") or "1.0")
        cache_key = _cache_key(rows, model or "unresolved", prompt_version, schema_version)
        json_path, markdown_path = path.parent / "ai-brief.json", path.parent / "ai-brief.md"
        if json_path.is_file():
            try:
                cached = json.loads(json_path.read_text(encoding="utf-8"))
                if cached.get("schema") == SCHEMA and cached.get("status") == "success" and cached.get("cache_key") == cache_key:
                    cached["analysis_metadata"]["request_count"] = 0
                    cached["analysis_metadata"]["network_attempt_count"] = 0
                    cached["analysis_metadata"]["cache_hit"] = True
                    atomic_write_json(json_path, cached)
                    atomic_text(markdown_path, render_markdown(cached))
                    state.update(status="success", phase="complete", output_path=str(markdown_path.resolve()), counts=cached["counts"], ai={"status": "cached", "request_count": 0, "network_attempt_count": 0, "cache_hit": True, "transport_security": (cached.get("analysis_metadata") or {}).get("transport_security")})
                    return cached
                if (
                    cached.get("schema") == SCHEMA and cached.get("status") == "degraded"
                    and cached.get("cache_key") == cache_key
                    and not (llm_status.get("enabled") and llm_status.get("api_key_configured"))
                ):
                    cached["analysis_metadata"]["request_count"] = 0
                    cached["analysis_metadata"]["network_attempt_count"] = 0
                    cached["analysis_metadata"]["cache_hit"] = True
                    atomic_write_json(json_path, cached)
                    atomic_text(markdown_path, render_markdown(cached))
                    state.update(status="partial", phase="complete", output_path=str(markdown_path.resolve()), counts=cached["counts"], ai={"status": "degraded", "request_count": 0, "network_attempt_count": 0, "cache_hit": True, "transport_security": (cached.get("analysis_metadata") or {}).get("transport_security"), "reason": (cached.get("warnings") or [None])[0]})
                    return cached
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                pass
        request_limit = int(settings.get("request_limit") or 1)
        if request_limit != 1:
            raise ValueError("AI 易读简报 request_limit 必须固定为 1")
        warning: str | None = None
        try:
            if not rows:
                raise ValueError("原始报告没有可整理作品")
            if not llm_status.get("enabled") or not llm_status.get("api_key_configured"):
                raise RuntimeError(str(llm_status.get("unavailable_reason") or "HTTPS 模型或凭据未就绪"))
            if not model:
                model = analyzer.resolve_model()
                cache_key = _cache_key(rows, model, prompt_version, schema_version)
            state.update(phase="single_model_request", counts={"input_items": len(rows), "request_limit": 1})
            output_schema = {
                "analysis_role": ANALYSIS_ROLE, "verification_performed": False,
                "daily_overview": ["第一句：仅概括输入", "第二句：仅概括输入"],
                "themes": [{"name": "", "video_ids": ["输入video_id"], "summary": ""}],
                "editorial_items": [{
                    "ai_editorial_order": 1, "video_id": "输入video_id", "source_heat_rank": 1,
                    "headline": "", "one_sentence_summary": "", "key_points": ["最多3条"],
                    "organization_reason": "", "public_engagement_summary": "",
                }],
            }
            system = (
                "你是科技快讯的编辑整理助手。只基于输入的可信账号OCR文字和公开互动数据安排阅读顺序并提高可读性。"
                "不得联网、不得核验新闻真实性、不得补充输入外事实或数字、不得修改原始热度名次和互动数据。"
                "必须覆盖每个输入video_id且只出现一次。严格输出JSON。"
            )
            prompt = (
                f"prompt_version={prompt_version}\n输出结构：{json.dumps(output_schema, ensure_ascii=False)}\n"
                f"安全输入：{json.dumps(rows, ensure_ascii=False)}"
            )
            raw = analyzer.chat_json_once(system, prompt, max_output_tokens=int(settings.get("max_output_tokens") or 3200))
            normalized = validate_model_output(raw, rows)
            status = "success"
        except Exception as exc:
            warning = f"大模型整理已降级：{_safe_reason(exc)}"
            normalized = _deterministic(rows, warning)
            status = "degraded"
        payload = _assemble(
            ranking=ranking, ranking_path=path, normalized=normalized, status=status, model=model,
            prompt_version=prompt_version, schema_version=schema_version, cache_key=cache_key,
            request_count=analyzer.request_count, network_attempt_count=analyzer.network_attempt_count,
            cache_hit=False, transport_security=str(llm_status.get("transport_security") or "disabled"), warning=warning,
        )
        atomic_write_json(json_path, payload)
        atomic_text(markdown_path, render_markdown(payload))
        if path.read_bytes() != ranking_bytes:
            raise RuntimeError("原始 ranking.json 在 AI 整理期间发生变化")
        state_status = "success" if status == "success" else "partial"
        state.update(
            status=state_status, phase="complete", output_path=str(markdown_path.resolve()), counts=payload["counts"],
            ai={"status": status, "request_count": analyzer.request_count, "network_attempt_count": analyzer.network_attempt_count,
                "cache_hit": False, "transport_security": llm_status.get("transport_security"), "reason": warning},
        )
        return payload
