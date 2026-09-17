from __future__ import annotations

import json
from pathlib import Path

from douyin_intelligence import workbench
from douyin_intelligence.workbench import EDITORIAL_TABS, TRUSTED_VISUAL_STAGES, brief_job_status, browser_prepare_command, daily_material_pack_command, daily_news_command, douyin_tech_ranking_command, editorial_card_text, launch_daily_news, process_running, trusted_account_news_command, trusted_ai_brief_command, trusted_ai_status_text, trusted_report_to_open, update_editorial_override
from douyin_intelligence.config import load_config
from douyin_intelligence.workbench_research_pack import (
    EMPTY_STATUS_TEXT,
    build_failure_text,
    consumer_ledger_path,
    consumer_root,
    consumer_snapshot_root,
    delivery_episode_id,
    episode_research_pack_command,
    latest_research_pack,
    research_pack_consumer_status,
    research_pack_evidence_path,
    research_pack_folder_target,
    research_pack_launch_text,
    research_pack_log_path,
    research_pack_output_root,
    research_pack_panel_text,
    research_pack_status_text,
)


def _research_pack_config(tmp_path):
    return {
        "_project_root": str(tmp_path),
        "jobs": {"material_replication": {"episode_research_pack": {"output_root": "output/每期研究包"}}},
    }


def _write_current_pointer(episode_dir, **pointer):
    episode_dir.mkdir(parents=True, exist_ok=True)
    (episode_dir / "current.json").write_text(json.dumps(pointer, ensure_ascii=False), encoding="utf-8")


def test_research_pack_output_root_reads_shipped_nested_block() -> None:
    assert research_pack_output_root(load_config()) == "output/每期研究包"
    assert research_pack_output_root({}) == "output/每期研究包"


def test_research_pack_status_shows_chinese_placeholder_without_any_pack(tmp_path) -> None:
    config = _research_pack_config(tmp_path)
    assert latest_research_pack(config) is None
    assert research_pack_status_text(config) == EMPTY_STATUS_TEXT == "尚未发布研究包"


def test_research_pack_status_formats_latest_current_pointer(tmp_path) -> None:
    config = _research_pack_config(tmp_path)
    root = tmp_path / "output" / "每期研究包"
    older = root / "2026-09-15_研究包" / "ep-old"
    newer = root / "2026-09-16_研究包" / "ep-demo"
    for episode, revision, pack_id, digest in ((older, 1, "ep-old-r1", "0" * 64), (newer, 3, "ep-demo-r3", "abcdef1234567890" * 4)):
        episode.mkdir(parents=True)
        (episode / "current.json").write_text(json.dumps({
            "episode_id": episode.name, "revision": revision, "pack_id": pack_id,
            "content_sha256": digest, "disposition": "fact_supported",
            "updated_at": "2026-09-16T02:00:00+08:00",
        }, ensure_ascii=False), encoding="utf-8")

    text = research_pack_status_text(config)
    assert "期次：ep-demo" in text and "版本：r3" in text
    assert "包 ID：ep-demo-r3" in text and "处置：fact_supported" in text
    assert "内容摘要：abcdef123456" in text and "abcdef1234567890" * 3 not in text
    assert "更新时间：2026-09-16T02:00:00+08:00" in text
    assert str(newer) in text
    assert "ep-old" not in text

    found = latest_research_pack(config)
    assert found["episode_root"] == newer
    assert research_pack_evidence_path(found) == newer / "每期研究证据包.md"
    assert research_pack_folder_target(found) == newer
    (newer / "packs" / "ep-demo-r3").mkdir(parents=True)
    assert research_pack_folder_target(found) == newer / "packs" / "ep-demo-r3"


def test_research_pack_status_survives_broken_current_json(tmp_path) -> None:
    config = _research_pack_config(tmp_path)
    episode = tmp_path / "output" / "每期研究包" / "2026-09-16_研究包" / "ep-broken"
    episode.mkdir(parents=True)
    (episode / "current.json").write_text("{ 不是合法 JSON", encoding="utf-8")

    text = research_pack_status_text(config)
    assert "无法解析" in text and str(episode / "current.json") in text
    assert latest_research_pack(config)["pointer"] is None


def test_research_pack_button_builds_from_delivery_folder_without_side_effects(tmp_path) -> None:
    delivery = tmp_path / "复刻视频" / "9.16主题复刻视频"
    command = episode_research_pack_command("config/content_intelligence.json", str(delivery))
    assert command[-4:-2] == ["episode-research-pack", "build"]
    assert command[-2:] == ["--delivery-folder", str(delivery)]
    assert str(delivery) in command
    for forbidden in ("--annotate-delivery", "--live", "browser-start", "scheduler", "inspect"):
        assert forbidden not in command
    tagged = episode_research_pack_command("config/content_intelligence.json", str(delivery), theme="主题", business_date="2026-09-16")
    assert tagged[-4:] == ["--theme", "主题", "--business-date", "2026-09-16"]


def test_research_pack_log_lives_under_tmp_workbench(tmp_path) -> None:
    path = research_pack_log_path(_research_pack_config(tmp_path))
    assert path == tmp_path / ".tmp" / "workbench" / "research-pack-build.log"


def test_research_pack_failure_text_falls_back_when_log_missing(tmp_path) -> None:
    missing = tmp_path / "不存在.log"
    text = build_failure_text(missing, 3)
    assert "构建失败（exit=3）：" in text
    assert "未能读取构建日志" in text and str(missing) in text


def test_research_pack_failure_text_keeps_only_non_empty_lines_in_order(tmp_path) -> None:
    log = tmp_path / "build.log"
    log.write_text("第一行\n\n   \n第二行\n\n第三行\n", encoding="utf-8")
    assert build_failure_text(log, 2) == "构建失败（exit=2）：\n第一行\n第二行\n第三行"

    log.write_text("\n\n", encoding="utf-8")
    empty = build_failure_text(log, 9)
    assert "构建日志为空" in empty and str(log) in empty


def test_research_pack_failure_text_respects_line_and_char_limits(tmp_path) -> None:
    log = tmp_path / "build.log"
    log.write_text("\n".join(f"第{index}行" for index in range(1, 31)), encoding="utf-8")
    text = build_failure_text(log, 1, max_lines=20)
    assert "第11行" in text and "第10行" not in text and "第30行" in text
    assert text.count("\n") == 20

    log.write_text("错误" * 500, encoding="utf-8")
    clipped = build_failure_text(log, 1, max_chars=60)
    assert len(clipped) == len("构建失败（exit=1）：") + 1 + 60
    assert "构建失败（exit=1）：" in clipped


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


# --- 单期研究包面板：期次过滤 / 超时防抖 / 文字与配置一致 -------------------


def test_latest_research_pack_filters_by_episode_id_and_keeps_latest_default(tmp_path) -> None:
    config = _research_pack_config(tmp_path)
    root = tmp_path / "output" / "每期研究包"
    older = root / "2026-09-15_研究包" / "ep-a"
    newer = root / "2026-09-16_研究包" / "ep-b"
    _write_current_pointer(older, episode_id="ep-a", revision=1, pack_id="ep-a-r1", content_sha256="a" * 64)
    _write_current_pointer(newer, episode_id="ep-b", revision=2, pack_id="ep-b-r2", content_sha256="b" * 64)

    assert latest_research_pack(config)["episode_root"] == newer
    assert latest_research_pack(config, episode_id="ep-a")["episode_root"] == older
    assert latest_research_pack(config, episode_id="ep-b")["episode_root"] == newer
    assert latest_research_pack(config, episode_id="ep-c") is None
    assert latest_research_pack(config, episode_id="")["episode_root"] == newer


def test_research_pack_status_text_flags_this_build_produced_nothing(tmp_path) -> None:
    config = _research_pack_config(tmp_path)
    other = tmp_path / "output" / "每期研究包" / "2026-09-16_研究包" / "ep-other"
    _write_current_pointer(other, episode_id="ep-other", revision=7, pack_id="ep-other-r7", content_sha256="c" * 64)

    text = research_pack_status_text(config, episode_id="ep-target")
    assert "本次" in text and "未产出" in text and "ep-target" in text
    assert "最近一期：ep-other" in text
    for leaked in ("期次：ep-other", "r7", "ep-other-r7", "cccccccccccc"):
        assert leaked not in text

    assert "期次：ep-other" in research_pack_status_text(config)


def test_research_pack_delivery_episode_id_matches_publish_naming(tmp_path) -> None:
    delivery = tmp_path / "复刻视频" / "9.16苹果折叠屏复刻视频"
    delivery.mkdir(parents=True)
    (delivery / "清单.json").write_text(
        json.dumps({"theme": "苹果折叠屏", "business_date": "2026-09-16"}, ensure_ascii=False), encoding="utf-8",
    )
    assert delivery_episode_id(delivery) == "2026-09-16-苹果折叠屏"
    assert delivery_episode_id(tmp_path / "没有清单") is None
    empty = tmp_path / "空清单"
    empty.mkdir()
    (empty / "清单.json").write_text("{}", encoding="utf-8")
    assert delivery_episode_id(empty) == "unknown-date-未命名主题"


def test_research_pack_panel_text_reflects_annotate_delivery_setting(tmp_path) -> None:
    config = _research_pack_config(tmp_path)
    assert "不回写交付清单" in research_pack_panel_text(config)
    assert "会回写交付清单" not in research_pack_panel_text(config)
    assert "不回写清单" in research_pack_launch_text(config, "日志")

    block = config["jobs"]["material_replication"]["episode_research_pack"]
    block["annotate_delivery_manifest"] = True
    assert "会回写交付清单" in research_pack_panel_text(config)
    launch = research_pack_launch_text(config, "日志")
    assert "会回写交付清单" in launch and "annotate_delivery_manifest=true" in launch

    _write_current_pointer(tmp_path / "output" / "每期研究包" / "2026-09-16_研究包" / "ep-a", episode_id="ep-a", revision=1, pack_id="ep-a-r1")
    assert "annotate_delivery_manifest=true" in research_pack_status_text(config)


# --- 单期研究包面板：消费端接收结果（只读 Haike 落盘物，绝不跑消费端） ------- #

_CONSUMER_DIGEST = "5c68f6dc14285b0d8147d5cfe00e43795084d295d5c37b8ec9cf21d06ce347c1"


def _consumer_config(tmp_path, consumer_root=None):
    """带消费端根目录的研究包配置；``consumer_root`` 为 None 时保持「未配置」。"""
    config = _research_pack_config(tmp_path)
    if consumer_root is not None:
        block = config["jobs"]["material_replication"]["episode_research_pack"]
        block["consumer_root"] = str(consumer_root)
    return config


def _write_ledger(consumer_root, episodes):
    path = Path(consumer_root) / ".backlot" / "research_pack_intake.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema": "episode-research-pack-intake-v1", "episodes": episodes}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_research_pack_consumer_root_resolves_relative_and_reports_unset(tmp_path) -> None:
    assert consumer_root(_consumer_config(tmp_path)) is None
    assert consumer_root(_consumer_config(tmp_path, "")) is None
    absolute = tmp_path / "Haike"
    assert consumer_root(_consumer_config(tmp_path, absolute)) == absolute
    assert consumer_root(_consumer_config(tmp_path, "Haike")) == tmp_path / "Haike"
    assert consumer_ledger_path(_consumer_config(tmp_path)) is None
    assert consumer_snapshot_root(_consumer_config(tmp_path)) is None


def test_research_pack_status_text_reports_consumer_admitted_by_digest(tmp_path) -> None:
    consumer = tmp_path / "Haike"
    config = _consumer_config(tmp_path, consumer)
    episode = tmp_path / "output" / "每期研究包" / "2026-09-16_研究包" / "2026-09-16-马斯克谈特斯拉合并"
    _write_current_pointer(
        episode, episode_id=episode.name, revision=1, pack_id=f"{episode.name}-r1",
        content_sha256=_CONSUMER_DIGEST, disposition="research_required",
        updated_at="2026-09-16T21:35:00+08:00",
    )
    snapshot = consumer / ".backlot" / "research-pack-snapshots" / episode.name / _CONSUMER_DIGEST
    (snapshot / "pack").mkdir(parents=True)
    _write_ledger(consumer, {episode.name: {
        "episode_id": episode.name,
        "current": {
            "revision": 1, "pack_id": f"{episode.name}-r1", "content_sha256": _CONSUMER_DIGEST,
            "state": "admitted", "disposition": "research_required",
            "snapshot_dir": str(snapshot), "admitted_at": "2026-09-16T13:32:40+00:00",
        },
        "events": [
            {"at": "2026-09-16T13:32:40+00:00", "kind": "admitted", "content_sha256": _CONSUMER_DIGEST,
             "message": "revision r1 已入库并快照"},
            {"at": "2026-09-16T14:12:11+00:00", "kind": "noop", "content_sha256": _CONSUMER_DIGEST,
             "message": "noop：content_unchanged"},
        ],
    }})

    text = research_pack_status_text(config)
    # 旧 4 个交接字段不退化
    assert f"期次：{episode.name}" in text and "版本：r1" in text
    assert f"包 ID：{episode.name}-r1" in text and "处置：research_required" in text
    assert "内容摘要：5c68f6dc1428" in text and f"位置：{episode}" in text
    # 消费端接收结果
    assert "消费端接收结果：已接收（admitted）" in text
    assert "匹配方式：content_sha256" in text
    assert "消费端处置：research_required  ·  消费端记录时间：2026-09-16T13:32:40+00:00" in text
    assert "消费端最近事件：2026-09-16T14:12:11+00:00 noop：content_unchanged" in text
    assert f"消费端快照：{snapshot}" in text
    assert "编辑门（exit 3）结论只在消费端 CLI 输出里" in text


def test_research_pack_consumer_degrades_to_not_received_without_guessing_episode(tmp_path) -> None:
    consumer = tmp_path / "Haike"
    config = _consumer_config(tmp_path, consumer)
    episode = tmp_path / "output" / "每期研究包" / "2026-09-16_研究包" / "ep-target"
    _write_current_pointer(
        episode, episode_id="ep-target", revision=1, pack_id="ep-target-r1", content_sha256="a" * 64,
    )
    _write_ledger(consumer, {"ep-other": {
        "episode_id": "ep-other",
        "current": {
            "revision": 9, "pack_id": "ep-other-r9", "content_sha256": "b" * 64,
            "state": "admitted", "disposition": "ready",
            "snapshot_dir": "别期快照", "admitted_at": "2026-09-15T01:00:00+00:00",
        },
        "events": [{"at": "2026-09-15T01:00:00+00:00", "kind": "admitted", "content_sha256": "b" * 64,
                    "message": "revision r9 已入库并快照"}],
    }})

    text = research_pack_status_text(config)
    assert "消费端接收结果：未接收" in text
    assert "消费端账本与快照都没有 content_sha256=aaaaaaaaaaaa / 包 ID ep-target-r1 的记录" in text
    # 别期次的记录一律不得泄漏成「本期结果」
    for leaked in ("ep-other", "r9", "bbbbbbbbbbbb", "已接收（admitted）", "别期快照"):
        assert leaked not in text


def test_research_pack_consumer_reports_event_only_rejection(tmp_path) -> None:
    """消费端 `rejected` 只写 events、不写 current，面板必须仍能报出「被拒」。"""
    consumer = tmp_path / "Haike"
    config = _consumer_config(tmp_path, consumer)
    episode = tmp_path / "output" / "每期研究包" / "2026-09-16_研究包" / "ep-rights"
    _write_current_pointer(
        episode, episode_id="ep-rights", revision=1, pack_id="ep-rights-r1", content_sha256=_CONSUMER_DIGEST,
    )
    _write_ledger(consumer, {"ep-rights": {
        "episode_id": "ep-rights",
        "events": [{"at": "2026-09-16T13:32:40+00:00", "kind": "rejected", "revision": 1,
                    "content_sha256": _CONSUMER_DIGEST,
                    "message": "rights/product gate：素材不得用于本产品"}],
    }})

    text = research_pack_status_text(config)
    assert "消费端接收结果：消费端拒绝（rejected）" in text
    assert "消费端最近事件：2026-09-16T13:32:40+00:00 rights/product gate：素材不得用于本产品" in text
    assert "未接收" not in text
    assert "已接收（admitted）" not in text


def test_research_pack_consumer_falls_back_to_snapshot_without_ledger(tmp_path) -> None:
    consumer = tmp_path / "Haike"
    config = _consumer_config(tmp_path, consumer)
    episode = tmp_path / "output" / "每期研究包" / "2026-09-16_研究包" / "ep-snap"
    _write_current_pointer(
        episode, episode_id="ep-snap", revision=2, pack_id="ep-snap-r2", content_sha256=_CONSUMER_DIGEST,
    )
    snapshot = consumer / ".backlot" / "research-pack-snapshots" / "ep-snap" / _CONSUMER_DIGEST
    snapshot.mkdir(parents=True)

    text = research_pack_status_text(config)
    assert "消费端接收结果：已接收（账本里没有同摘要记录，但消费端已有收编快照）" in text
    assert str(snapshot) in text


def test_research_pack_consumer_survives_broken_or_missing_ledger(tmp_path) -> None:
    consumer = tmp_path / "Haike"
    config = _consumer_config(tmp_path, consumer)
    episode = tmp_path / "output" / "每期研究包" / "2026-09-16_研究包" / "ep-broken-ledger"
    _write_current_pointer(
        episode, episode_id="ep-broken-ledger", revision=1, pack_id="ep-bl-r1", content_sha256="c" * 64,
    )
    ledger = consumer / ".backlot" / "research_pack_intake.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("{ 不是合法 JSON", encoding="utf-8")

    text = research_pack_status_text(config)
    assert "消费端接收结果：未接收（消费端账本无法解析，按未接收处理）" in text
    assert str(ledger) in text

    ledger.unlink()
    text = research_pack_status_text(config)
    assert "消费端接收结果：未接收" in text and "账本与快照都没有" in text


def test_research_pack_consumer_reports_unconfigured_without_touching_disk(tmp_path) -> None:
    config = _research_pack_config(tmp_path)
    episode = tmp_path / "output" / "每期研究包" / "2026-09-16_研究包" / "ep-unset"
    _write_current_pointer(
        episode, episode_id="ep-unset", revision=1, pack_id="ep-unset-r1", content_sha256="d" * 64,
    )
    text = research_pack_status_text(config)
    assert "消费端接收结果：未配置消费端仓库根目录" in text
    assert research_pack_consumer_status(config, {"content_sha256": "d" * 64})["found"] is False
    assert research_pack_consumer_status(config, {})["configured"] is False


def test_research_pack_consumer_matches_pack_id_when_digest_absent(tmp_path) -> None:
    consumer = tmp_path / "Haike"
    config = _consumer_config(tmp_path, consumer)
    _write_ledger(consumer, {"ep-id": {
        "episode_id": "ep-id",
        "current": {"revision": 3, "pack_id": "ep-id-r3", "content_sha256": _CONSUMER_DIGEST,
                    "state": "admitted", "disposition": "ready",
                    "snapshot_dir": "快照", "admitted_at": "2026-09-16T13:32:40+00:00"},
    }})

    status = research_pack_consumer_status(config, {"pack_id": "ep-id-r3", "content_sha256": ""})
    assert status["found"] is True and status["matched_by"] == "pack_id"
    assert status["state"] == "admitted"
    assert research_pack_consumer_status(config, {"pack_id": "ep-id-r4"})["found"] is False


class _RunningProcess:
    """模拟仍在后端构建的 Popen：poll() 先返回 None（仍在跑）。"""

    def __init__(self, exit_code=None):
        self.exit_code = exit_code

    def poll(self):
        return self.exit_code


class _FakeRoot:
    """只记录 after 回调，避免在用例里构造真实 Tk。"""

    def __init__(self):
        self.callbacks = []

    def after(self, _delay, callback):
        self.callbacks.append(callback)


class _FakeStringVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _MessageboxRecorder:
    def __init__(self):
        self.calls = []

    def showinfo(self, *args, **kwargs):
        self.calls.append(("info", args))

    def showwarning(self, *args, **kwargs):
        self.calls.append(("warning", args))

    def showerror(self, *args, **kwargs):
        self.calls.append(("error", args))

    def notices(self):
        return [call for call in self.calls if call[0] in {"info", "warning"}]


class _FakeFiledialog:
    @staticmethod
    def askdirectory(**_kwargs):
        # 防抖一旦失效也要走「未选目录」分支返回，避免用例弹出真实文件对话框。
        return ""


def _research_pack_window(tmp_path, process):
    """绕过 Tk 构造，只装配 _poll_research_pack / start_research_pack 用到的属性。"""
    window = workbench.Workbench.__new__(workbench.Workbench)
    window.root = _FakeRoot()
    window.config = _research_pack_config(tmp_path)
    window.config_path = "config/content_intelligence.json"
    window.research_pack_process = process
    window.research_pack_log = tmp_path / "research-pack-build.log"
    window.research_pack_state = _FakeStringVar()
    window.research_pack_episode_id = None
    window._research_pack_timeout_notified = False
    return window


def _advance_poll(window, seconds):
    """按 Tk 主循环的方式推进已排期的轮询回调。"""
    for _ in range(seconds):
        pending, window.root.callbacks = list(window.root.callbacks), []
        for callback in pending:
            callback()


def test_research_pack_timeout_keeps_process_handle_and_blocks_second_launch(tmp_path, monkeypatch) -> None:
    launches = []
    recorder = _MessageboxRecorder()
    monkeypatch.setattr(workbench, "launch_episode_research_pack", lambda *args, **kwargs: launches.append(args))
    monkeypatch.setattr(workbench, "messagebox", recorder)
    monkeypatch.setattr(workbench, "filedialog", _FakeFiledialog)
    window = _research_pack_window(tmp_path, _RunningProcess())

    window._poll_research_pack(2)
    _advance_poll(window, 4)

    assert window.research_pack_process is not None
    assert process_running(window.research_pack_process) is True
    assert window.root.callbacks != []

    window.start_research_pack()
    assert launches == []
    # 防抖必须真的在入口拦住：弹出的是「已经在运行」，而不是走到选目录分支。
    assert "已经在运行" in recorder.notices()[-1][1][1]


def test_research_pack_timeout_notice_pops_up_only_once(tmp_path, monkeypatch) -> None:
    recorder = _MessageboxRecorder()
    monkeypatch.setattr(workbench, "messagebox", recorder)
    window = _research_pack_window(tmp_path, _RunningProcess())

    window._poll_research_pack(1)
    _advance_poll(window, 10)

    notices = recorder.notices()
    assert len(notices) == 1
    assert "仍在后台运行" in notices[0][1][1]


def test_research_pack_consumer_button_opens_only_the_matched_snapshot(tmp_path, monkeypatch) -> None:
    consumer = tmp_path / "Haike"
    opened: list[object] = []
    recorder = _MessageboxRecorder()
    monkeypatch.setattr(workbench, "messagebox", recorder)
    monkeypatch.setattr(workbench.os, "startfile", lambda target: opened.append(target), raising=False)
    window = _research_pack_window(tmp_path, None)
    window.config = _consumer_config(tmp_path, consumer)

    # 未配置消费端：明说未配置，绝不打开别处的目录。
    window.config = _research_pack_config(tmp_path)
    window.open_research_pack_consumer()
    assert opened == []
    assert "未配置消费端仓库根目录" in recorder.notices()[-1][1][1]

    # 已配置但没有该包的快照：只给中文说明，不打开快照根目录。
    window.config = _consumer_config(tmp_path, consumer)
    window.open_research_pack_consumer()
    assert opened == []
    assert "还没有这个包的收编快照" in recorder.notices()[-1][1][1]

    # 有收编快照才打开它。
    episode = tmp_path / "output" / "每期研究包" / "2026-09-16_研究包" / "ep-ok"
    _write_current_pointer(
        episode, episode_id="ep-ok", revision=1, pack_id="ep-ok-r1", content_sha256=_CONSUMER_DIGEST,
    )
    snapshot = consumer / ".backlot" / "research-pack-snapshots" / "ep-ok" / _CONSUMER_DIGEST
    snapshot.mkdir(parents=True)
    _write_ledger(consumer, {"ep-ok": {
        "episode_id": "ep-ok",
        "current": {"revision": 1, "pack_id": "ep-ok-r1", "content_sha256": _CONSUMER_DIGEST,
                    "state": "admitted", "disposition": "research_required",
                    "snapshot_dir": str(snapshot), "admitted_at": "2026-09-16T13:32:40+00:00"},
    }})

    window.open_research_pack_consumer()
    assert opened == [snapshot]
