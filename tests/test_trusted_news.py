from __future__ import annotations

import copy
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from douyin_intelligence.config import load_config
from douyin_intelligence.llm_analysis import OpenAICompatibleAnalyzer
from douyin_intelligence.trusted_news import (
    collect_trusted_account,
    get_trusted_account,
    obtain_transcript,
    rank_account_items,
    resolve_account_entry,
    run_trusted_account_news,
    select_window_records,
)
from douyin_intelligence.workbench import trusted_account_news_command


def _config(tmp_path: Path) -> dict:
    config = copy.deepcopy(load_config())
    config["_project_root"] = str(tmp_path)
    config["jobs"]["state_path"] = str(tmp_path / "state.json")
    config["jobs"]["lock_root"] = str(tmp_path / "locks")
    config["jobs"]["trusted_account_news"]["output_root"] = str(tmp_path / "output")
    config["jobs"]["trusted_account_news"]["temp_root"] = str(tmp_path / "temp")
    config["jobs"]["trusted_account_news"]["ai_brief"]["auto_after_collection"] = False
    config["materials"]["llm"]["enabled"] = False
    return config


def _row(account: dict, video_id: int, published: datetime, *, title: str = "AI公司发布新模型", creator_hash: str | None = None, like=100, comment=10, collect=5, share=2) -> dict:
    return {
        "aweme_id": str(video_id), "title": title, "create_time": published.isoformat(),
        "sec_uid": account["stable_id"], "creator_hash": creator_hash or account.get("expected_creator_hash") or "creator-hash", "nickname": "财***AI",
        "liked_count": like, "comment_count": comment, "collected_count": collect, "share_count": share,
        "aweme_url": f"https://www.douyin.com/video/{video_id}",
    }


def test_short_link_resolution_locks_stable_account_and_does_not_infer_followers(tmp_path: Path) -> None:
    config = _config(tmp_path)
    account = get_trusted_account(config, "caiyan-ai")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.iesdouyin.com":
            return httpx.Response(200, text="ok")
        return httpx.Response(302, headers={"location": f"https://www.iesdouyin.com/share/user/{account['stable_id']}?sec_uid={account['stable_id']}"})

    result = resolve_account_entry(account, transport=httpx.MockTransport(handler))

    assert result["stable_id"] == account["stable_id"]
    assert result["profile_url"] == account["profile_url"]
    assert result["follower_count_observed"] is None


def test_window_identity_boundary_and_global_ten_item_cap(tmp_path: Path) -> None:
    config = _config(tmp_path)
    account = get_trusted_account(config)
    zone = ZoneInfo("Asia/Shanghai")
    end = datetime(2026, 8, 27, 15, 0, tzinfo=zone)
    rows = [_row(account, 7600000000000000000 + index, end - timedelta(hours=index)) for index in range(12)]
    rows += [_row(account, 7700000000000000001, end - timedelta(hours=49))]
    wrong = _row(account, 7800000000000000001, end - timedelta(hours=1))
    wrong["sec_uid"] = "MS4wLjABAAAAwrongwrongwrongwrongwrong"
    rows.append(wrong)

    selected, evidence = select_window_records(rows, config, account, end, 10)

    assert len(selected) == 10
    assert evidence["window_count_before_limit"] == 12
    assert evidence["dropped_wrong_account"] == 1
    assert evidence["dropped_outside_window"] == 1
    assert all(item["record"].account_id == "caiyan-ai" for item in selected)
    assert evidence["window_start"] == "2026-08-25T15:00:00+08:00"


def test_heat_formula_preserves_missing_values_resists_extremes_and_stable_ties(tmp_path: Path) -> None:
    config = _config(tmp_path)
    account = get_trusted_account(config)
    zone = ZoneInfo("Asia/Shanghai")
    end = datetime(2026, 8, 27, 15, 0, tzinfo=zone)
    rows = [
        _row(account, 7600000000000000001, end - timedelta(hours=1), like=10**15, comment=None, collect=None, share=None),
        _row(account, 7600000000000000002, end - timedelta(hours=2), like=100, comment=10, collect=5, share=2),
        _row(account, 7600000000000000003, end - timedelta(hours=2), like=100, comment=10, collect=5, share=2),
    ]
    selected, _ = select_window_records(rows, config, account, end, 10)
    first, formula = rank_account_items(selected, end, config["jobs"]["trusted_account_news"]["weights"])
    second, _ = rank_account_items(selected, end, config["jobs"]["trusted_account_news"]["weights"])

    assert [(item["record"].video_id, item["heat_score"]) for item in first] == [(item["record"].video_id, item["heat_score"]) for item in second]
    extreme = next(item for item in first if item["record"].video_id.endswith("1"))
    assert extreme["heat_components"]["comment"] is None
    assert extreme["data_completeness"]["available_interactions"] == 1
    assert max(item["heat_score"] for item in first) <= sum(config["jobs"]["trusted_account_news"]["weights"].values())
    tied = [item["record"].video_id for item in first if item["record"].video_id.endswith(("2", "3"))]
    assert tied == sorted(tied)
    assert formula["version"] == "trusted-account-heat-v1"


def test_transcript_priority_and_media_cleanup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    account = get_trusted_account(config)
    end = datetime(2026, 8, 27, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    caption_row = _row(account, 7600000000000000001, end, title="短标题") | {"platform_caption": "平台已有完整字幕"}
    selected, _ = select_window_records([caption_row], config, account, end, 10)
    caption = obtain_transcript(selected[0], config, tmp_path / "run", "config/content_intelligence.json")
    assert caption["transcript_source"] == "platform_caption"

    media_row = _row(account, 7600000000000000002, end, title="短标题") | {"video_download_url": "https://media.example/video?signature=temporary"}
    selected, _ = select_window_records([media_row], config, account, end, 10)

    def fake_download(_url: str, path: Path, _config: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")

    monkeypatch.setattr("douyin_intelligence.trusted_news.download_video", fake_download)
    asr_calls = {"count": 0}

    def forbidden_asr(*_args):
        asr_calls["count"] += 1
        raise AssertionError("visual_text_only must never call ASR")

    def fake_visual(*_args, **_kwargs):
        return {
            "visual_text_status": "success", "quality_tier": "success",
            "merged_text": "这是从视频画面提取的科技新闻正文，包含足够的公司名称、日期和关键数字用于安全整理。",
            "candidate_frames": 12, "selected_frames": 10, "ocr_frames": 10, "retry_frames": 0,
            "unique_text_cards": 3, "unique_content_chars": 42, "median_ocr_confidence": 0.9,
            "visual_text_coverage": 0.8, "budget_exhausted": False, "frames": [], "text_cards": [],
        }

    transcript = obtain_transcript(
        selected[0], config, tmp_path / "run", "config/content_intelligence.json",
        asr_runner=forbidden_asr, visual_runner=fake_visual,
    )
    assert transcript["content_source"] == "screen_ocr"
    assert transcript["audio_attempted"] is False
    assert asr_calls["count"] == 0
    assert not (tmp_path / "run" / "media-7600000000000000002").exists()

    short_row = _row(account, 7600000000000000003, end, title="短标题") | {"video_download_url": "https://media.example/video"}
    selected, _ = select_window_records([short_row], config, account, end, 10)
    degraded = obtain_transcript(selected[0], config, tmp_path / "run", "config/content_intelligence.json", asr_runner=forbidden_asr, visual_runner=lambda *_args, **_kwargs: {"visual_text_status": "unavailable", "quality_tier": "unavailable", "merged_text": "", "error": "OCR无有效文字"})
    assert degraded["transcript_source"] == "description_only"
    assert degraded["transcript_status"] == "partial"
    assert degraded["error"] == "OCR无有效文字"
    assert degraded["audio_attempted"] is False
    assert asr_calls["count"] == 0


def test_https_llm_rejects_facts_not_in_transcript(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["materials"]["llm"].update({"enabled": True, "base_url": "https://llm.example/v1", "model": "safe-model", "retries": 1})
    monkeypatch.setattr("douyin_intelligence.llm_analysis.secret_value", lambda name: "secret-for-test" if name == "DOUYIN_LLM_API_KEY" else None)

    def handler(_request: httpx.Request) -> httpx.Response:
        result = {
            "headline": "模型更新", "one_sentence_summary": "该博主介绍模型更新。", "key_points": ["出现 2027 年发布日期"],
            "why_it_matters": "值得关注", "safe_broadcast": "模型更新", "claims_to_verify": ["发布日期"],
            "content_angle": "解释更新", "do_not_claim": ["不要说成官方确认"],
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}]})

    analyzer = OpenAICompatibleAnalyzer(config, transport=httpx.MockTransport(handler))
    result = analyzer.enrich_trusted_news(account_name="财研网AI", metadata={"title": "模型更新"}, transcript={"text": "该博主介绍模型更新"})
    assert result["status"] == "rejected"
    assert analyzer.status()["transport_security"] == "https"


def test_offline_job_exports_safe_partial_ranking_without_promoting_trust(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    account = get_trusted_account(config)
    end = datetime(2026, 8, 27, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    source = tmp_path / "input.json"
    rows = [
        _row(account, 7600000000000000001, end - timedelta(hours=1), title="AI公司发布新模型，预计提升推理效率。") | {"cookie": "must-not-persist", "video_download_url": "https://bad.example/video?signature=secret"},
        _row(account, 7600000000000000002, end - timedelta(hours=3), title="芯片公司公布新产品路线和发布时间。"),
    ]
    source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr("douyin_intelligence.trusted_news.scheduler_query", lambda _config: {"ok": False, "task_name": "CopySkillDailyTechNews"})

    report = run_trusted_account_news(config, input_files=[str(source)], window_end=end, allow_media=False)
    payload = json.loads(Path(report["artifacts"]["json"]).read_text(encoding="utf-8"))
    serialized = json.dumps(payload, ensure_ascii=False)

    assert report["status"] == "partial"
    assert report["counts"]["processed"] == 2
    assert report["llm_enrichment"] == "disabled_missing_https_endpoint"
    assert all(item["source_tier"] == "trusted_creator" and item["evidence_status"] != "verified_official" for item in report["items"])
    assert all(item["enrichment"]["safe_broadcast"].startswith("据财研网AI本条视频画面文字及发布文案介绍") for item in report["items"])
    assert report["security_scan"]["status"] == "passed"
    assert "must-not-persist" not in serialized and "signature=secret" not in serialized
    assert len(list(Path(report["artifacts"]["transcripts"]).glob("*.json"))) == 2
    assert not any(Path(config["jobs"]["trusted_account_news"]["temp_root"]).glob("*"))


def test_live_login_requirement_stops_without_retry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    Path(config["jobs"]["state_path"]).write_text(json.dumps({"version": "1.0", "jobs": {"trusted_account_news": {"current_index": 9, "counts": {"local_asr": 5}, "output_path": "old.md"}}}), encoding="utf-8")
    monkeypatch.setattr("douyin_intelligence.trusted_news.resolve_account_entry", lambda account: {"account_id": account["id"], "account_name": account["name"], "stable_id": account["stable_id"], "profile_url": account["profile_url"], "follower_count_observed": None})
    calls = {"count": 0}
    running_state = {}

    def needs_login(*_args, **_kwargs):
        calls["count"] += 1
        running_state.update(json.loads(Path(config["jobs"]["state_path"]).read_text(encoding="utf-8"))["jobs"]["trusted_account_news"])
        return {"status": "needs_login", "files": [], "error": "请在项目专用浏览器完成抖音登录/验证后重试"}

    monkeypatch.setattr("douyin_intelligence.trusted_news.collect_trusted_account", needs_login)
    monkeypatch.setattr("douyin_intelligence.trusted_news.scheduler_query", lambda _config: {"ok": False, "task_name": "CopySkillDailyTechNews"})
    report = run_trusted_account_news(config, live=True, window_end=datetime(2026, 8, 27, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")))

    assert report["status"] == "needs_login"
    assert calls["count"] == 1
    assert report["items"] == []
    assert running_state["current_index"] is None and running_state["counts"] == {}
    assert running_state["output_path"] is None and running_state["completed_at"] is None


def test_account_resolution_failure_writes_safe_failed_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr("douyin_intelligence.trusted_news.resolve_account_entry", lambda _account: (_ for _ in ()).throw(httpx.ConnectTimeout("bounded timeout")))
    monkeypatch.setattr("douyin_intelligence.trusted_news.scheduler_query", lambda _config: {"ok": False, "task_name": "CopySkillDailyTechNews"})

    report = run_trusted_account_news(config, live=True, window_end=datetime(2026, 8, 27, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")))

    assert report["status"] == "failed"
    assert report["items"] == []
    assert report["errors"] == [{"phase": "collection", "error": "账号解析或采集失败：ConnectTimeout"}]


def test_collection_uses_independent_windows_process_group(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    account = get_trusted_account(config)
    observed = {}
    class FakeSession:
        def __init__(self, *_args, **_kwargs): pass
        def prepare(self): return {"status": "reused", "port": 9223, "page_count": 1}
        def finish(self, status, **_kwargs): return {"state": "waiting_for_login" if status == "needs_login" else "completed_closed"}

    monkeypatch.setattr("douyin_intelligence.trusted_news.BrowserSession", FakeSession)

    def fake_run(*_args, **kwargs):
        observed.update(kwargs)
        return subprocess.CompletedProcess([], 1, "login required", "")

    monkeypatch.setattr("douyin_intelligence.trusted_news.subprocess.run", fake_run)
    result = collect_trusted_account(config, account, tmp_path / "temp" / "run", 10)

    assert result["status"] == "needs_login"
    assert observed["timeout"] <= 120
    if __import__("os").name == "nt":
        assert observed["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP


def test_collection_reports_project_page_reuse_without_target_or_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    account = get_trusted_account(config)

    class FakeSession:
        def __init__(self, *_args, **_kwargs): pass
        def prepare(self): return {"status": "reused", "port": 9223, "page_count": 1}
        def finish(self, status, **_kwargs): return {"state": "completed_closed"}

    monkeypatch.setattr("douyin_intelligence.trusted_news.BrowserSession", FakeSession)

    def fake_run(*_args, **_kwargs):
        raw = tmp_path / "temp" / "run" / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "creator_contents_1.jsonl").write_text("{}\n", encoding="utf-8")
        return subprocess.CompletedProcess([], 0, "COPY_SKILL_CDP_AUDIT pages_before=1 pages_after=1 reused_existing_page=1", "")

    monkeypatch.setattr("douyin_intelligence.trusted_news.subprocess.run", fake_run)
    result = collect_trusted_account(config, account, tmp_path / "temp" / "run", 10)
    browser = result["browser"]
    assert browser["initial_page_count"] == 1
    assert browser["runner"] == {
        "pages_before": 1, "pages_after": 1, "reused_existing_page": True,
        "ownership": "project_cdp_port_9223", "os_window_count": "not_observed",
    }
    serialized = json.dumps(browser, ensure_ascii=False).casefold()
    for forbidden in ("target", "websocket", "cookie", "https://"):
        assert forbidden not in serialized


def test_trusted_collection_never_calls_per_item_llm_when_batch_mode_is_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    account = get_trusted_account(config)
    source = tmp_path / "source.jsonl"
    source.write_text(json.dumps(_row(account, 7600000000000000001, datetime(2026, 8, 27, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))), ensure_ascii=False) + "\n", encoding="utf-8")
    config["materials"]["llm"]["enabled"] = True

    class FakeAnalyzer:
        def __init__(self, _config): pass
        def status(self): return {"enabled": True, "api_key_configured": True, "model": "safe-model"}
        def enrich_trusted_news(self, **_kwargs): raise AssertionError("per-item LLM must not be called")

    monkeypatch.setattr("douyin_intelligence.trusted_news.OpenAICompatibleAnalyzer", FakeAnalyzer)
    monkeypatch.setattr("douyin_intelligence.trusted_news.scheduler_query", lambda _config: {"ok": False, "task_name": "CopySkillDailyTechNews"})
    report = run_trusted_account_news(
        config, input_files=[str(source)], window_end=datetime(2026, 8, 28, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        allow_media=False, auto_ai_brief=False,
    )
    assert report["counts"]["llm"]["batch_deferred"] == 1
    assert report["counts"]["llm"]["success"] == 0


def test_workbench_command_calls_single_account_formal_path() -> None:
    command = trusted_account_news_command("config/content_intelligence.json", "caiyan-ai", 99)
    assert "trusted-account-news" in command and "--live" in command
    assert command[command.index("--account-id") + 1] == "caiyan-ai"
    assert command[command.index("--max-items") + 1] == "10"
