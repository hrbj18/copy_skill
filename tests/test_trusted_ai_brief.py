from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest

from douyin_intelligence.config import load_config
from douyin_intelligence.llm_analysis import OpenAICompatibleAnalyzer
from douyin_intelligence.trusted_ai_brief import ANALYSIS_ROLE, build_safe_input, run_ai_brief, validate_model_output


def _config(tmp_path: Path) -> dict:
    config = copy.deepcopy(load_config())
    config["_project_root"] = str(tmp_path)
    config["jobs"]["state_path"] = str(tmp_path / "state.json")
    config["jobs"]["lock_root"] = str(tmp_path / "locks")
    config["jobs"]["trusted_account_news"]["output_root"] = str(tmp_path / "output")
    config["materials"]["llm"].update({"enabled": True, "base_url": "https://unit.test/v1", "model": "safe-model", "timeout_seconds": 5})
    return config


def _ranking(tmp_path: Path) -> Path:
    root = tmp_path / "output" / "caiyan-ai" / "2026-08-28"
    ocr = root / "ocr"
    ocr.mkdir(parents=True)
    items = []
    for rank, video_id, score in ((1, "1001", 50.0), (2, "1002", 40.0)):
        transcript = ocr / f"{video_id}.json"
        transcript.write_text(json.dumps({"text": f"视频{video_id}介绍模型更新与行业变化"}, ensure_ascii=False), encoding="utf-8")
        items.append({
            "heat_rank": rank, "heat_score": score, "heat_components": {"like": score},
            "metadata": {
                "video_id": video_id, "title": f"标题{video_id}", "author": "财研网AI",
                "published_at": f"2026-08-28T0{rank}:00:00+08:00",
                "share_url": f"https://www.douyin.com/video/{video_id}",
                "interactions": {"like": 100 // rank, "comment": 10, "collect": 5, "share": 2},
            },
            "content": {"content_source": "screen_ocr", "visual_text_status": "success", "complete": True},
            "transcript_path": str(transcript.resolve()),
            "enrichment": {"headline": f"确定性标题{video_id}", "one_sentence_summary": "确定性摘要"},
        })
    payload = {
        "schema": "trusted-account-news-ranking-v2", "status": "success",
        "account": {"account_id": "caiyan-ai", "name": "财研网AI", "source_tier": "trusted_creator", "editorial_confidence": "high"},
        "window": {"timezone": "Asia/Shanghai", "start": "2026-08-26T12:00:00+08:00", "end": "2026-08-28T12:00:00+08:00"},
        "items": items,
    }
    path = root / "ranking.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "ranking.md").write_text("# 原始底账\n", encoding="utf-8")
    return path


def _model_payload() -> dict:
    return {
        "analysis_role": ANALYSIS_ROLE, "verification_performed": False,
        "daily_overview": ["这些视频围绕模型更新。", "互动较高的内容优先展示。"],
        "themes": [{"name": "模型动态", "video_ids": ["1001", "1002"], "summary": "两条内容均讨论模型变化。"}],
        "editorial_items": [
            {"ai_editorial_order": 1, "video_id": "1002", "source_heat_rank": 2, "headline": "模型变化的后续影响", "one_sentence_summary": "从行业变化切入。", "key_points": ["关注内容差异"], "organization_reason": "主题更适合作为开场。", "public_engagement_summary": "公开互动较集中。"},
            {"ai_editorial_order": 2, "video_id": "1001", "source_heat_rank": 1, "headline": "模型更新的主要内容", "one_sentence_summary": "梳理视频中的更新信息。", "key_points": ["保持来源边界"], "organization_reason": "承接前一条展开。", "public_engagement_summary": "公开互动更高。"},
        ],
    }


def _analyzer(config: dict, monkeypatch: pytest.MonkeyPatch, handler) -> OpenAICompatibleAnalyzer:
    monkeypatch.setattr("douyin_intelligence.llm_analysis.secret_value", lambda name: "unit-secret" if name == "DOUYIN_LLM_API_KEY" else "")
    return OpenAICompatibleAnalyzer(config, transport=httpx.MockTransport(handler))


def test_batch_brief_uses_one_request_preserves_bottom_line_and_writes_independent_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, ranking = _config(tmp_path), _ranking(tmp_path)
    original = ranking.read_bytes()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path.endswith("/chat/completions")
        sent = request.read().decode("utf-8")
        for forbidden in ("unit-secret", "cookie", "signature=", "transcript_path", str(tmp_path)):
            assert forbidden not in sent.casefold()
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(_model_payload(), ensure_ascii=False)}}]})

    result = run_ai_brief(config, ranking, analyzer=_analyzer(config, monkeypatch, handler))
    assert result["status"] == "success"
    assert result["analysis_metadata"]["request_count"] == 1 and result["analysis_metadata"]["cache_hit"] is False
    assert result["analysis_metadata"]["network_attempt_count"] == 1
    assert [item["source_heat_rank"] for item in result["editorial_items"]] == [2, 1]
    assert ranking.read_bytes() == original
    assert len(requests) == 1
    assert ranking.with_name("ai-brief.json").is_file() and ranking.with_name("ai-brief.md").is_file()
    assert "未核验新闻真实性" in ranking.with_name("ai-brief.md").read_text(encoding="utf-8")


def test_identical_content_hits_cache_without_second_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, ranking = _config(tmp_path), _ranking(tmp_path)
    first = _analyzer(config, monkeypatch, lambda _request: httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(_model_payload(), ensure_ascii=False)}}]}))
    assert run_ai_brief(config, ranking, analyzer=first)["status"] == "success"

    def forbidden(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("cache hit must not call transport")

    second = _analyzer(config, monkeypatch, forbidden)
    cached = run_ai_brief(config, ranking, analyzer=second)
    assert cached["status"] == "success"
    assert cached["analysis_metadata"]["request_count"] == 0 and cached["analysis_metadata"]["cache_hit"] is True


@pytest.mark.parametrize("mutation", ["unknown", "duplicate", "rank", "number"])
def test_model_validation_rejects_unknown_duplicate_rank_and_new_numbers(tmp_path: Path, mutation: str) -> None:
    config, ranking = _config(tmp_path), _ranking(tmp_path)
    source = json.loads(ranking.read_text(encoding="utf-8"))
    rows = build_safe_input(config, source, ranking.parent)
    payload = _model_payload()
    if mutation == "unknown":
        payload["editorial_items"][0]["video_id"] = "9999"
    elif mutation == "duplicate":
        payload["editorial_items"][1]["video_id"] = "1002"
    elif mutation == "rank":
        payload["editorial_items"][0]["source_heat_rank"] = 1
    else:
        payload["editorial_items"][0]["headline"] = "输入之外的9999项事实"
    with pytest.raises(ValueError):
        validate_model_output(payload, rows)


def test_api_failure_makes_deterministic_brief_after_one_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, ranking = _config(tmp_path), _ranking(tmp_path)
    analyzer = _analyzer(config, monkeypatch, lambda _request: httpx.Response(500, text="failure at https://secret-endpoint.test/v1"))
    result = run_ai_brief(config, ranking, analyzer=analyzer)
    assert result["status"] == "degraded"
    assert result["analysis_metadata"]["request_count"] == 0
    assert result["analysis_metadata"]["network_attempt_count"] == 1
    serialized = json.dumps(result, ensure_ascii=False)
    assert "secret-endpoint" not in serialized and "unit-secret" not in serialized
    assert [item["source_heat_rank"] for item in result["editorial_items"]] == [1, 2]


def test_insecure_http_is_rejected_without_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, ranking = _config(tmp_path), _ranking(tmp_path)
    config["materials"]["llm"].update({"base_url": "http://unit.test/v1", "allow_insecure_http": False})
    called = []
    analyzer = _analyzer(config, monkeypatch, lambda request: called.append(request) or httpx.Response(200))
    result = run_ai_brief(config, ranking, analyzer=analyzer)
    assert result["status"] == "degraded"
    assert result["analysis_metadata"]["request_count"] == 0
    assert result["analysis_metadata"]["network_attempt_count"] == 0 and called == []
    cached = run_ai_brief(config, ranking, analyzer=_analyzer(config, monkeypatch, lambda request: called.append(request) or httpx.Response(200)))
    assert cached["status"] == "degraded"
    assert cached["analysis_metadata"]["cache_hit"] is True and called == []


def test_explicit_http_opt_in_uses_one_request_and_persists_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, ranking = _config(tmp_path), _ranking(tmp_path)
    config["materials"]["llm"].update({"base_url": "http://unit.test/v1", "allow_insecure_http": True})
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(_model_payload(), ensure_ascii=False)}}]})

    result = run_ai_brief(config, ranking, analyzer=_analyzer(config, monkeypatch, handler))
    assert result["status"] == "success"
    assert result["analysis_metadata"]["transport_security"] == "insecure_http_user_authorized"
    assert result["analysis_metadata"]["network_attempt_count"] == 1
    assert result["analysis_metadata"]["request_count"] == 1
    assert len(requests) == 1
    assert "远程 HTTP 明文" in ranking.with_name("ai-brief.md").read_text(encoding="utf-8")


def test_safe_input_ignores_transcript_path_outside_report(tmp_path: Path) -> None:
    config, ranking = _config(tmp_path), _ranking(tmp_path)
    payload = json.loads(ranking.read_text(encoding="utf-8"))
    outside = tmp_path / "outside.json"
    outside.write_text('{"text":"must-not-leak"}', encoding="utf-8")
    payload["items"][0]["transcript_path"] = str(outside)
    rows = build_safe_input(config, payload, ranking.parent)
    assert rows[0]["safe_ocr_text"] == ""


def test_config_freezes_explicit_http_opt_in_and_single_request_budget() -> None:
    config = load_config()
    assert config["materials"]["llm"]["enabled"] is True
    assert config["materials"]["llm"]["allow_insecure_http"] is True
    assert config["jobs"]["trusted_account_news"]["ai_brief"]["request_limit"] == 1
