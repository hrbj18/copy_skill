from __future__ import annotations

import json
from pathlib import Path

import httpx

from douyin_intelligence.config import load_config
from douyin_intelligence.llm_analysis import OpenAICompatibleAnalyzer


def test_llm_analysis_chunks_caches_and_never_persists_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DOUYIN_LLM_API_KEY", "unit-test-secret")
    monkeypatch.setenv("DOUYIN_LLM_MODEL", "test-model")
    monkeypatch.setenv("DOUYIN_LLM_BASE_URL", "https://unit.test/v1")
    config = load_config()
    config["materials"]["llm"]["enabled"] = True
    config["materials"]["llm"]["chunk_chars"] = 40
    analyzer = OpenAICompatibleAnalyzer(config)
    calls = []

    def fake_chat(system: str, user: str):
        calls.append(user)
        if "视频级高价值素材" in user or "合并并去重" in user:
            return {"value_summary": "摘要", "core_points": ["要点"], "best_moments": [{"timestamp": "00:01", "reason": "重要", "content": "内容"}], "content_angles": ["方向"], "claims_to_verify": ["数字"]}
        return {"topic": "主题", "summary": "分段", "key_points": ["信息"], "notable_moments": [], "narrative_techniques": [], "content_angles": [], "claims_to_verify": []}

    monkeypatch.setattr(analyzer, "_chat_json", fake_chat)
    transcript = {"segments": [{"start": index, "end": index + 1, "text": "一段需要分析的转写内容"} for index in range(8)], "text": "内容"}
    cache = tmp_path / "analysis.json"
    first = analyzer.analyze(title="标题", transcript=transcript, ocr=None, cache_path=cache)
    assert first["status"] == "success"
    assert first["chunk_count"] > 1
    assert "unit-test-secret" not in cache.read_text(encoding="utf-8")
    count = len(calls)
    second = analyzer.analyze(title="标题", transcript=transcript, ocr=None, cache_path=cache)
    assert second["cache_hit"] is True
    assert len(calls) == count


def test_llm_analysis_is_explicitly_unavailable_without_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("douyin_intelligence.llm_analysis.secret_value", lambda name: "")
    config = load_config()
    config["materials"]["llm"].update({"enabled": True, "base_url": "https://unit.test/v1"})
    analyzer = OpenAICompatibleAnalyzer(config)
    result = analyzer.analyze(title="标题", transcript={"segments": []}, ocr=None, cache_path=tmp_path / "x.json")
    assert result["status"] == "unavailable"
    assert "API_KEY" in result["error"]


def test_openai_compatible_protocol_with_mock_transport(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DOUYIN_LLM_API_KEY", "transport-secret")
    monkeypatch.setenv("DOUYIN_LLM_BASE_URL", "https://unit.test/v1")
    monkeypatch.delenv("DOUYIN_LLM_MODEL", raising=False)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer transport-secret"
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-5-test"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"value_summary": "协议摘要", "core_points": ["协议要点"], "best_moments": [], "content_angles": [], "claims_to_verify": []}, ensure_ascii=False)}}]})

    config = load_config()
    config["materials"]["llm"]["enabled"] = True
    analyzer = OpenAICompatibleAnalyzer(config, transport=httpx.MockTransport(handler))
    # This test exercises model discovery and must not inherit a developer's
    # ignored .env.local model override.
    analyzer.model = ""
    assert analyzer.resolve_model() == "gpt-5-test"
    result = analyzer.analyze(title="标题", transcript={"segments": [{"start": 0, "end": 1, "text": "内容"}]}, ocr=None, cache_path=tmp_path / "analysis.json")
    assert result["status"] == "success"
    assert result["result"]["value_summary"] == "协议摘要"
    persisted = (tmp_path / "analysis.json").read_text(encoding="utf-8")
    assert "transport-secret" not in persisted
    assert len(requests) >= 3


def test_default_llm_is_disabled_without_reading_secret_storage(monkeypatch) -> None:
    monkeypatch.setattr("douyin_intelligence.llm_analysis.secret_value", lambda _name: (_ for _ in ()).throw(AssertionError("disabled LLM must not read secrets")))
    config = load_config()
    config["materials"]["llm"]["enabled"] = False
    analyzer = OpenAICompatibleAnalyzer(config)

    assert analyzer.status()["enabled"] is False
    assert analyzer.status()["transport_security"] == "disabled"
    assert "未启用" in str(analyzer.status()["unavailable_reason"])


def test_http_requires_explicit_opt_in_and_reports_insecure_transport(monkeypatch) -> None:
    monkeypatch.setattr(
        "douyin_intelligence.llm_analysis.secret_value",
        lambda name: "unit-secret" if name == "DOUYIN_LLM_API_KEY" else "",
    )
    config = load_config()
    config["materials"]["llm"].update({
        "enabled": True, "base_url": "http://unit.test/v1", "model": "safe-model",
        "allow_insecure_http": False,
    })
    blocked = OpenAICompatibleAnalyzer(config)
    assert blocked.status()["enabled"] is False
    assert blocked.status()["transport_security"] == "disabled"

    config["materials"]["llm"]["allow_insecure_http"] = True
    allowed = OpenAICompatibleAnalyzer(config)
    assert allowed.status()["enabled"] is True
    assert allowed.status()["transport_security"] == "insecure_http_user_authorized"
