from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .exporter import atomic_write_json
from .local_secrets import secret_value


ANALYSIS_SYSTEM = """你是科技内容情报编辑。输入是抖音视频的时间戳转写或OCR文字，只能作为选题线索，不是事实证据。请严格输出JSON，不要Markdown。所有数字、公司表态、规格、发布日期和性能结论都要列入claims_to_verify。不要补造输入中没有的事实。"""


def _clean_json_text(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    start = value.find("{")
    end = value.rfind("}")
    return value[start:end + 1] if start >= 0 and end > start else value


def _safe_error(exc: Exception) -> str:
    text = str(exc)
    key = secret_value("DOUYIN_LLM_API_KEY")
    if key:
        text = text.replace(key, "[REDACTED]")
    text = re.sub(r"(?i)\b(?:https?|wss?)://[^\s]+", "[REDACTED_URL]", text)
    text = re.sub(r"(?i)\b(?:authorization|cookie|bearer|api[_-]?key)\b\s*[:=]?\s*[^\s,;]+", "[REDACTED]", text)
    return text[:500]


@dataclass(slots=True)
class LLMSettings:
    enabled: bool
    base_url: str
    model: str
    timeout: int
    retries: int
    chunk_chars: int
    version: str
    transport_security: str
    unavailable_reason: str | None

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "LLMSettings":
        raw = config["materials"].get("llm") or {}
        requested = bool(raw.get("enabled", False))
        configured_base = str(raw.get("base_url") or "").rstrip("/")
        base_url = (secret_value("DOUYIN_LLM_BASE_URL") or configured_base).rstrip("/") if requested else configured_base
        scheme = urlsplit(base_url).scheme.casefold() if base_url else ""
        allow_insecure_http = bool(raw.get("allow_insecure_http", False))
        secure = scheme == "https"
        explicitly_allowed_http = scheme == "http" and allow_insecure_http
        usable = requested and bool(base_url) and (secure or explicitly_allowed_http)
        transport_security = "https" if usable and secure else "insecure_http_user_authorized" if usable else "disabled"
        unavailable_reason = None
        if not requested:
            unavailable_reason = "AI 深度分析暂不可用：模型整理未启用"
        elif not base_url:
            unavailable_reason = "AI 深度分析暂不可用：OpenAI-compatible base URL 未配置"
        elif scheme == "http" and not allow_insecure_http:
            unavailable_reason = "AI 深度分析暂不可用：HTTP 端点未获得显式授权"
        elif scheme not in {"http", "https"}:
            unavailable_reason = "AI 深度分析暂不可用：base URL 只支持 HTTP 或 HTTPS"
        return cls(
            enabled=usable,
            base_url=base_url,
            model=(secret_value("DOUYIN_LLM_MODEL") or str(raw.get("model") or "")) if usable else str(raw.get("model") or ""),
            timeout=int(raw.get("timeout_seconds") or 120),
            retries=int(raw.get("retries") or 3),
            chunk_chars=int(raw.get("chunk_chars") or 8000),
            version=str(raw.get("analysis_version") or "v1"),
            transport_security=transport_security,
            unavailable_reason=unavailable_reason,
        )


class OpenAICompatibleAnalyzer:
    def __init__(self, config: dict[str, Any], *, transport: httpx.BaseTransport | None = None):
        self.settings = LLMSettings.from_config(config)
        self.api_key = secret_value("DOUYIN_LLM_API_KEY") if self.settings.enabled else ""
        self.model = self.settings.model
        self.request_count = 0
        self.network_attempt_count = 0
        self.transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.settings.timeout, trust_env=False, transport=self.transport)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.enabled, "base_url_configured": bool(self.settings.base_url),
            "api_key_configured": bool(self.api_key), "model": self.model or None,
            "transport_security": self.settings.transport_security,
            "unavailable_reason": self.settings.unavailable_reason,
        }

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise RuntimeError("缺少 DOUYIN_LLM_API_KEY")
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def discover_models(self) -> list[str]:
        if not self.settings.enabled:
            raise RuntimeError(self.settings.unavailable_reason or "AI 深度分析已禁用")
        if not self.settings.base_url:
            raise RuntimeError("中转站 base_url 未配置")
        with self._client() as client:
            self.network_attempt_count += 1
            response = client.get(f"{self.settings.base_url}/models", headers=self._headers())
            response.raise_for_status()
            payload = response.json()
        return [str(item.get("id")) for item in payload.get("data", []) if isinstance(item, dict) and item.get("id")]

    def resolve_model(self) -> str:
        if self.model:
            return self.model
        models = self.discover_models()
        if not models:
            raise RuntimeError("中转站 /models 未返回可用模型")
        priorities = ("gpt-5.4", "gpt-5", "claude", "gemini", "qwen", "deepseek")
        self.model = next((item for prefix in priorities for item in models if prefix in item.casefold()), models[0])
        return self.model

    def _chat_json(self, system: str, user: str) -> dict[str, Any]:
        model = self.resolve_model()
        payload = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": 0.1, "response_format": {"type": "json_object"}}
        last_error: Exception | None = None
        for attempt in range(max(1, self.settings.retries)):
            try:
                with self._client() as client:
                    self.network_attempt_count += 1
                    response = client.post(f"{self.settings.base_url}/chat/completions", headers=self._headers(), json=payload)
                    if response.status_code == 400 and "response_format" in response.text:
                        payload.pop("response_format", None)
                        self.network_attempt_count += 1
                        response = client.post(f"{self.settings.base_url}/chat/completions", headers=self._headers(), json=payload)
                    response.raise_for_status()
                    result = response.json()
                self.request_count += 1
                content = result["choices"][0]["message"]["content"]
                return json.loads(_clean_json_text(content))
            except Exception as exc:
                last_error = exc
                if attempt + 1 < max(1, self.settings.retries):
                    time.sleep(1 + attempt * 2)
        raise RuntimeError(f"中转站请求失败：{_safe_error(last_error or RuntimeError('unknown'))}")

    def chat_json_once(self, system: str, user: str, *, max_output_tokens: int = 3200) -> dict[str, Any]:
        """Perform exactly one generation request with no retry or model fallback."""
        if not self.settings.enabled:
            raise RuntimeError(self.settings.unavailable_reason or "HTTPS 模型未启用")
        if not self.api_key:
            raise RuntimeError("缺少 DOUYIN_LLM_API_KEY")
        model = self.resolve_model()
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.1,
            "max_tokens": min(4096, max(256, int(max_output_tokens))),
            "response_format": {"type": "json_object"},
        }
        try:
            with self._client() as client:
                self.network_attempt_count += 1
                response = client.post(f"{self.settings.base_url}/chat/completions", headers=self._headers(), json=payload)
                response.raise_for_status()
                result = response.json()
            content = result["choices"][0]["message"]["content"]
            parsed = json.loads(_clean_json_text(content))
            if not isinstance(parsed, dict):
                raise ValueError("模型响应不是 JSON 对象")
            self.request_count += 1
            return parsed
        except Exception as exc:
            raise RuntimeError(f"中转站单次请求失败：{_safe_error(exc)}") from None

    def _chunks(self, transcript: dict[str, Any], ocr: dict[str, Any] | None = None) -> list[str]:
        rows = [f"[{int(item['start']) // 60:02d}:{int(item['start']) % 60:02d}] {item['text']}" for item in transcript.get("segments", [])]
        for item in (ocr or {}).get("items", []):
            rows.append(f"[画面 {int(item.get('time', 0)) // 60:02d}:{int(item.get('time', 0)) % 60:02d}] {item.get('text', '')}")
        if not rows and transcript.get("text"):
            rows = [str(transcript["text"])]
        chunks: list[str] = []
        current: list[str] = []
        length = 0
        for row in rows:
            if current and length + len(row) + 1 > self.settings.chunk_chars:
                chunks.append("\n".join(current))
                current, length = [], 0
            current.append(row)
            length += len(row) + 1
        if current:
            chunks.append("\n".join(current))
        return chunks

    def analyze(self, *, title: str, transcript: dict[str, Any], ocr: dict[str, Any] | None, cache_path: Path) -> dict[str, Any]:
        if not self.settings.enabled:
            return {"status": "unavailable", "error": self.settings.unavailable_reason or "AI 深度分析已禁用"}
        if not self.api_key:
            return {"status": "unavailable", "error": "缺少 DOUYIN_LLM_API_KEY"}
        model = self.resolve_model()
        material_hash = hashlib.sha256(json.dumps({"title": title, "transcript": transcript, "ocr": ocr, "model": model, "version": self.settings.version}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        if cache_path.is_file():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("cache_key") == material_hash and cached.get("status") == "success":
                cached["cache_hit"] = True
                return cached
        chunks = self._chunks(transcript, ocr)
        if not chunks:
            return {"status": "unavailable", "error": "没有可供分析的转写或 OCR 文本"}
        partials = []
        schema = '{"topic":"","summary":"","key_points":[],"notable_moments":[{"timestamp":"MM:SS","reason":"","content":""}],"narrative_techniques":[],"content_angles":[],"claims_to_verify":[]}'
        for index, chunk in enumerate(chunks, 1):
            prompt = f"视频标题：{title}\n这是第 {index}/{len(chunks)} 段。请按结构提炼本段：{schema}\n\n{chunk}"
            partials.append(self._chat_json(ANALYSIS_SYSTEM, prompt))
        merge_schema = '{"value_summary":"","core_points":[],"best_moments":[{"timestamp":"MM:SS","reason":"","content":""}],"narrative_structure":[],"content_angles":[],"claims_to_verify":[],"recommended_use":""}'
        merge_input = json.dumps(partials, ensure_ascii=False)
        # Hierarchical reduction keeps each request bounded for exceptionally long videos.
        merge_limit = max(self.settings.chunk_chars * 2, 2000)
        while len(merge_input) > merge_limit and len(partials) > 1:
            reduced = []
            batch: list[dict[str, Any]] = []
            batch_size = 0
            for item in partials:
                encoded = json.dumps(item, ensure_ascii=False)
                if len(batch) >= 2 and batch_size + len(encoded) > merge_limit:
                    reduced.append(self._chat_json(ANALYSIS_SYSTEM, f"合并并去重这些分段分析，保留时间戳和待核验项，输出结构：{merge_schema}\n{json.dumps(batch, ensure_ascii=False)}"))
                    batch, batch_size = [], 0
                batch.append(item)
                batch_size += len(encoded)
            if batch:
                reduced.append(self._chat_json(ANALYSIS_SYSTEM, f"合并并去重这些分段分析，保留时间戳和待核验项，输出结构：{merge_schema}\n{json.dumps(batch, ensure_ascii=False)}"))
            if len(reduced) >= len(partials):
                break
            partials = reduced
            merge_input = json.dumps(partials, ensure_ascii=False)
        final = self._chat_json(ANALYSIS_SYSTEM, f"将以下分段分析合并为视频级高价值素材。去重、保留最有价值时间戳，不能把未经核验的主张写成确定事实。输出结构：{merge_schema}\n{merge_input}")
        result = {"status": "success", "cache_key": material_hash, "analysis_version": self.settings.version, "model": model, "chunk_count": len(chunks), "request_count": self.request_count, "cache_hit": False, "result": final}
        atomic_write_json(cache_path, result)
        return result

    def enrich_trusted_news(self, *, account_name: str, metadata: dict[str, Any], transcript: dict[str, Any]) -> dict[str, Any]:
        """Extract one creator report without promoting it to official evidence."""
        if not self.settings.enabled:
            return {"status": "unavailable", "error": self.settings.unavailable_reason or "HTTPS 模型未启用"}
        if not self.api_key:
            return {"status": "unavailable", "error": "缺少 DOUYIN_LLM_API_KEY"}
        text = str(transcript.get("text") or "").strip()[: self.settings.chunk_chars]
        if not text:
            return {"status": "unavailable", "error": "没有可供提炼的安全字幕文本"}
        schema = '{"headline":"","one_sentence_summary":"","key_points":[""],"why_it_matters":"","safe_broadcast":"","claims_to_verify":[""],"content_angle":"","do_not_claim":[""]}'
        system = (
            "你是可信对标账号科技快讯编辑。只能提取输入字幕已有内容，不能联网、补事实或把博主报道写成官方公告。"
            f"safe_broadcast 必须以‘据{account_name}本条视频介绍’开头；数字、日期、规格、价格、公司决定和性能结论必须保留在 claims_to_verify。严格输出 JSON。"
        )
        prompt = f"输出结构：{schema}\n安全元数据：{json.dumps(metadata, ensure_ascii=False)}\n字幕：{text}"
        result = self._chat_json(system, prompt)
        required = {"headline", "one_sentence_summary", "key_points", "why_it_matters", "safe_broadcast", "claims_to_verify", "content_angle", "do_not_claim"}
        if not isinstance(result, dict) or not required <= result.keys():
            return {"status": "rejected", "error": "模型输出缺少规定字段"}
        source_numbers = set(re.findall(r"\d+(?:\.\d+)?", json.dumps({"metadata": metadata, "transcript": text}, ensure_ascii=False)))
        output_numbers = set(re.findall(r"\d+(?:\.\d+)?", json.dumps(result, ensure_ascii=False)))
        if output_numbers - source_numbers:
            return {"status": "rejected", "error": "模型输出包含字幕外数字事实"}
        normalized = {
            "headline": str(result["headline"])[:120],
            "one_sentence_summary": str(result["one_sentence_summary"])[:300],
            "key_points": [str(value)[:300] for value in (result["key_points"] if isinstance(result["key_points"], list) else [])][:4],
            "why_it_matters": str(result["why_it_matters"])[:500],
            "safe_broadcast": str(result["safe_broadcast"])[:800],
            "claims_to_verify": [str(value)[:300] for value in (result["claims_to_verify"] if isinstance(result["claims_to_verify"], list) else [])][:10],
            "content_angle": str(result["content_angle"])[:500],
            "do_not_claim": [str(value)[:300] for value in (result["do_not_claim"] if isinstance(result["do_not_claim"], list) else [])][:10],
        }
        prefix = f"据{account_name}本条视频介绍"
        if not normalized["safe_broadcast"].startswith(prefix):
            normalized["safe_broadcast"] = f"{prefix}，{normalized['safe_broadcast'].lstrip('，,。 ')}"
        if not normalized["claims_to_verify"]:
            normalized["claims_to_verify"] = ["字幕中的数字、日期、规格、价格、公司决定和性能结论。"]
        if not normalized["do_not_claim"]:
            normalized["do_not_claim"] = ["不得扩展字幕未支持的事实，也不得表述为官方公告。"]
        return {"status": "success", "result": normalized}

    def structure_news(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Structure already sourced events without promoting unsupported claims."""
        if not self.settings.enabled or not self.api_key or not events:
            return []
        schema = '{"items":[{"index":0,"subject_place":"","event":"","news_value":"","creative_angle":"","claims_to_verify":""}]}'
        system = "你是严谨的科技新闻编辑。只能改写输入中已有的标题、摘要和来源信息，不得补造事实。抖音数据只代表关注度。严格输出JSON。"
        prompt = f"把这些已通过来源门槛的事件整理成新闻三要素和创作建议。无法从来源确认的内容放入claims_to_verify。输出结构：{schema}\n{json.dumps(events, ensure_ascii=False)}"
        result = self._chat_json(system, prompt)
        return [item for item in result.get("items", []) if isinstance(item, dict)]

    def compose_inspiration(self, candidates: list[dict[str, Any]], maximum: int) -> list[dict[str, Any]]:
        if not self.settings.enabled or not self.api_key or not candidates:
            return []
        schema = '{"items":[{"candidate_indexes":[0],"recommended_title":"","one_line_idea":"","why_interesting":"","outline":"","claims_to_verify":""}]}'
        system = "你是科技短视频选题编辑。输入来自抖音，只能作为创作线索。不得把标题、字幕、互动数据当成事实；所有事实主张必须列为待核验。严格输出JSON。"
        prompt = f"从候选中聚类并生成最多{maximum}张互不重复的灵感卡。输出结构：{schema}\n{json.dumps(candidates, ensure_ascii=False)}"
        result = self._chat_json(system, prompt)
        return [item for item in result.get("items", []) if isinstance(item, dict)][:maximum]
