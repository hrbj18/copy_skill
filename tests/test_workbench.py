from __future__ import annotations

from douyin_intelligence import workbench
from douyin_intelligence.workbench import EDITORIAL_TABS, TRUSTED_VISUAL_STAGES, brief_job_status, browser_prepare_command, daily_material_pack_command, daily_news_command, douyin_tech_ranking_command, editorial_card_text, launch_daily_news, process_running, trusted_account_news_command, trusted_ai_brief_command, trusted_ai_status_text, trusted_report_to_open, update_editorial_override
from douyin_intelligence.config import load_config


def test_trusted_account_workbench_exposes_visual_stages_without_audio_transcription() -> None:
    assert "下载临时视频" in TRUSTED_VISUAL_STAGES
    assert "选择画面" in TRUSTED_VISUAL_STAGES and "本地OCR" in TRUSTED_VISUAL_STAGES
    assert "本地转写" not in TRUSTED_VISUAL_STAGES and "不处理音频" in TRUSTED_VISUAL_STAGES


def test_login_button_command_only_prepares_browser_and_does_not_collect() -> None:
    command = browser_prepare_command("config/content_intelligence.json")
    assert "browser-start" in command and "--allow-browser" in command
    assert "trusted-account-news" not in command and "--live" not in command


def test_duplicate_click_guard_recognizes_running_process() -> None:
    class Process:
        def __init__(self, code): self.code = code
        def poll(self): return self.code
    assert process_running(Process(None)) is True
    assert process_running(Process(0)) is False
    assert process_running(None) is False


def test_workbench_daily_news_command_uses_formal_daily_news_path() -> None:
    command = daily_news_command("config/content_intelligence.json", "2026-08-26")

    assert "daily-news" in command
    assert "--target-date" in command
    assert "2026-08-26" in command
    assert "scheduler" not in command


def test_daily_material_pack_button_uses_quick_offline_cli_without_browser_or_models() -> None:
    command = daily_material_pack_command("config/content_intelligence.json", "config/daily_material_pack_domestic_2026-08-28.json")
    assert command[-5:] == ["daily-material-pack", "build", "--input", str(workbench.resolve_path("config/daily_material_pack_domestic_2026-08-28.json")), "--quick"]
    for forbidden in ("browser-start", "--live", "--douyin-fallback", "llm-doctor", "scheduler", "export-openmontage"):
        assert forbidden not in command


def test_workbench_status_includes_progress_report_path_and_concise_error() -> None:
    text = brief_job_status("每日新闻", {"status": "partial", "phase": "complete", "updated_at": "2026-08-27T00:00:00+08:00", "counts": {"confirmed": 3}, "output_path": "output/daily-news/report.md", "errors": [{"source": "llm", "error": "do not expose"}]})

    assert "partial / complete" in text
    assert "报告：output/daily-news/report.md" in text
    assert "简短错误：1 项（llm）" in text
    assert "do not expose" not in text


def test_workbench_launches_the_same_daily_news_command(monkeypatch) -> None:
    captured: dict[str, object] = {}
    expected = object()

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return expected

    monkeypatch.setattr(workbench.subprocess, "Popen", fake_popen)

    assert launch_daily_news("config/content_intelligence.json", "2026-08-26") is expected
    assert "daily-news" in captured["command"]
    assert "2026-08-26" in captured["command"]


def test_workbench_ranking_command_uses_dedicated_live_metadata_path() -> None:
    command = douyin_tech_ranking_command("config/content_intelligence.json", "2026-08-26")

    assert "douyin-tech-ranking" in command
    assert "--live-douyin" in command
    assert "daily-news" not in command
    assert "scheduler" not in command


def test_trusted_ai_button_uses_existing_ranking_without_browser_or_collection(tmp_path) -> None:
    ranking = tmp_path / "ranking.json"
    ranking.write_text("{}", encoding="utf-8")
    command = trusted_ai_brief_command("config/content_intelligence.json", ranking)
    assert "trusted-ai-brief" in command and "--ranking" in command
    for forbidden in ("--live", "browser-start", "trusted-account-news", "ocr", "scheduler"):
        assert forbidden not in command
    collection = trusted_account_news_command("config/content_intelligence.json", "caiyan-ai", auto_ai_brief=False)
    assert "--skip-ai-brief" in collection


def test_trusted_report_button_prefers_successful_ai_brief_and_falls_back_on_degrade(tmp_path) -> None:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    config["jobs"]["trusted_account_news"]["output_root"] = str(tmp_path / "output")
    root = tmp_path / "output" / "caiyan-ai" / "2026-08-28"
    root.mkdir(parents=True)
    (root / "ranking.json").write_text("{}", encoding="utf-8")
    (root / "ranking.md").write_text("base", encoding="utf-8")
    (root / "ai-brief.json").write_text('{"status":"success"}', encoding="utf-8")
    (root / "ai-brief.md").write_text("ai", encoding="utf-8")
    assert trusted_report_to_open(config, "caiyan-ai") == root / "ai-brief.md"
    (root / "ai-brief.json").write_text('{"status":"degraded"}', encoding="utf-8")
    assert trusted_report_to_open(config, "caiyan-ai") == root / "ranking.md"


def test_trusted_ai_status_distinguishes_configured_running_complete_and_degraded(monkeypatch) -> None:
    config = load_config()

    class FakeAnalyzer:
        def __init__(self, _config): pass
        def status(self): return {"enabled": True, "api_key_configured": True}

    monkeypatch.setattr(workbench, "OpenAICompatibleAnalyzer", FakeAnalyzer)
    assert trusted_ai_status_text(config, {}) == "大模型整理：已配置"
    assert trusted_ai_status_text(config, {"status": "running"}) == "大模型整理：运行中"
    assert trusted_ai_status_text(config, {"status": "success", "output_path": "brief.md"}) == "大模型整理：已完成"
    assert trusted_ai_status_text(config, {"status": "partial", "ai": {"status": "degraded"}}) == "大模型整理：已降级"

    class InsecureAnalyzer:
        def __init__(self, _config): pass
        def status(self): return {"enabled": True, "api_key_configured": True, "transport_security": "insecure_http_user_authorized"}

    monkeypatch.setattr(workbench, "OpenAICompatibleAnalyzer", InsecureAnalyzer)
    assert "远程 HTTP 明文模式" in trusted_ai_status_text(config, {})


def test_workbench_exposes_four_editorial_views_and_full_card_fields() -> None:
    assert [key for key, _label in EDITORIAL_TABS] == ["hotspots", "news_leads", "tech_talk", "manual_review"]
    text = editorial_card_text({
        "heat_rank": 1, "heat_score": 52.0, "primary_content_type": "news_lead", "secondary_content_types": [],
        "evidence_status": "unverified_claim", "editorial_status": "research_required", "video_count": 1,
        "heat_components": {"like": 10}, "claims_to_verify": ["发布日期"], "why_worth_attention": "互动升温",
        "safe_hook": "尚未官方确认", "tech_talk_angle": "讨论为何爆火", "do_not_claim": ["不得称已发布"],
        "representative_videos": [{"author": "作者", "published_at": "2026-08-26", "share_url": "https://www.douyin.com/video/7600000000000000001"}],
    })
    for value in ("总榜 #1", "news_lead", "unverified_claim", "research_required", "普通链接", "待核实", "禁止宣称"):
        assert value in text


def test_workbench_override_helper_persists_without_evidence_promotion(tmp_path) -> None:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    config["workbench"]["editorial_override_path"] = "state/overrides.json"
    result = update_editorial_override(config, "topic-123", {"primary_content_type": "creator_review", "editor_note": "可恢复"})

    assert result["primary_content_type"] == "creator_review"
    assert "evidence_status" not in result
