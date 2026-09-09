from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import douyin_intelligence.account_evaluation as account_evaluation_module
from douyin_intelligence.account_evaluation import evaluate_account, evaluation_window, run_account_evaluation
from douyin_intelligence.account_pool import AccountPoolStore, normalize_profile_url
from douyin_intelligence.account_pool_ui import account_pool_evaluation_command, account_pool_status_text
from douyin_intelligence.config import load_config


def _config(tmp_path: Path) -> dict:
    config = copy.deepcopy(load_config())
    config["_project_root"] = str(tmp_path)
    config["jobs"]["state_path"] = str(tmp_path / "workbench-state.json")
    config["jobs"]["lock_root"] = str(tmp_path / "locks")
    config["jobs"]["account_pool"]["state_path"] = str(tmp_path / "account-pool.json")
    config["jobs"]["account_pool"]["output_root"] = str(tmp_path / "output")
    config["jobs"]["account_pool"]["temp_root"] = str(tmp_path / "temp")
    return config


def _row(
    account: dict,
    video_id: int,
    published: datetime,
    *,
    title: str = "AI 芯片公司发布科技快讯",
    duration: int | None = 90_000,
    actual_name: str | None = None,
    include_identity: bool = True,
    interactions: bool = True,
) -> dict:
    row = {
        "aweme_id": str(video_id),
        "desc": title,
        "create_time": published.isoformat(),
        "nickname": actual_name or account["display_name"],
        "aweme_url": f"https://www.douyin.com/video/{video_id}",
    }
    if include_identity:
        row["sec_uid"] = account["account_id"]
    if duration is not None:
        row["duration"] = duration
    if interactions:
        row.update({"digg_count": 100, "comment_count": 10, "collect_count": 5, "share_count": 2})
    return row


def test_profile_url_normalization_and_atomic_candidate_crud(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = AccountPoolStore(config)
    account_id, profile = normalize_profile_url(
        "https://www.douyin.com/user/MS4wLjABAAAAabcdefghijklmnopqrstuv/?from=web"
    )
    assert account_id == "MS4wLjABAAAAabcdefghijklmnopqrstuv"
    assert profile == f"https://www.douyin.com/user/{account_id}"

    added = store.add_candidate("测试科技号", profile, note="观察两周")
    assert added["lifecycle_status"] == "candidate" and added["enabled"] is True
    assert store.get(account_id)["note"] == "观察两周"
    assert not list((tmp_path).rglob("*.tmp"))

    with pytest.raises(ValueError, match="已存在"):
        store.add_candidate("重复主页", profile)
    with pytest.raises(ValueError, match="抖音用户主页"):
        store.add_candidate("非法", "https://example.com/user/abc")


def test_lifecycle_changes_are_manual_and_recoverable(tmp_path: Path) -> None:
    store = AccountPoolStore(_config(tmp_path))
    account = store.list_accounts()[0]
    account_id = account["account_id"]

    assert store.set_status(account_id, "trusted")["lifecycle_status"] == "trusted"
    assert store.set_status(account_id, "rejected")["lifecycle_status"] == "rejected"
    assert store.set_status(account_id, "paused")["enabled"] is False
    restored = store.set_status(account_id, "candidate")
    assert restored["lifecycle_status"] == "candidate" and restored["enabled"] is True


def test_beijing_fourteen_day_window_and_local_thirty_cap(tmp_path: Path) -> None:
    config = _config(tmp_path)
    account = AccountPoolStore(config).list_accounts()[0]
    start, end = evaluation_window("2026-08-28", "Asia/Shanghai")
    assert start.isoformat() == "2026-08-15T00:00:00+08:00"
    assert end.isoformat() == "2026-08-28T23:59:59.999999+08:00"

    rows = [_row(account, 7600000000000000000 + index, end - timedelta(hours=index)) for index in range(35)]
    rows += [_row(account, 7700000000000000001, start - timedelta(microseconds=1))]
    result = evaluate_account(account, rows, config, target_date="2026-08-28")

    assert result["metrics"]["total_posts"] == 30
    assert result["collection_counts"]["window_posts_before_limit"] == 35
    assert result["collection_counts"]["dropped_outside_window"] == 1
    assert result["window"]["start"] == start.isoformat()


def test_core_candidate_thresholds_do_not_change_lifecycle(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = AccountPoolStore(config)
    account = store.list_accounts()[0]
    end = datetime(2026, 8, 28, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    rows = [_row(account, 7600000000000001000 + day, end - timedelta(days=day)) for day in range(12)]
    result = evaluate_account(account, rows, config, target_date="2026-08-28")

    assert result["evaluation_recommendation"] == "core_candidate"
    assert result["metrics"]["active_days"] == 12
    assert result["metrics"]["technology_news_ratio"] == 1.0
    assert result["metrics"]["short_video_ratio"] == 1.0
    assert store.get(account["account_id"])["lifecycle_status"] == "candidate"


def test_missing_duration_and_interactions_are_not_fabricated_as_zero(tmp_path: Path) -> None:
    config = _config(tmp_path)
    account = AccountPoolStore(config).list_accounts()[0]
    end = datetime(2026, 8, 28, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    rows = [
        _row(account, 7600000000000002000 + day, end - timedelta(days=day), duration=None, interactions=False)
        for day in range(6)
    ]
    result = evaluate_account(account, rows, config, target_date="2026-08-28")
    metrics = result["metrics"]

    assert metrics["short_video_ratio"] is None
    assert metrics["duration_missing_rate"] == 1.0
    assert all(value is None for value in metrics["interaction_medians"].values())
    assert all(value == 1.0 for value in metrics["interaction_missing_rates"].values())
    assert all(item["duration_seconds"] is None for item in result["recent_posts"])


def test_identity_mismatch_blocks_positive_recommendation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    account = AccountPoolStore(config).list_accounts()[0]
    end = datetime(2026, 8, 28, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    row = _row(account, 7600000000000003000, end, actual_name="完全不同的账号")
    row["sec_uid"] = "MS4wLjABAAAAdifferentstableaccountid"
    rows = [row]
    result = evaluate_account(account, rows, config, target_date="2026-08-28")

    assert result["identity_status"] == "identity_mismatch"
    assert result["evaluation_recommendation"] == "do_not_use"
    assert result["status"] == "partial"


def test_matching_stable_id_allows_display_name_change(tmp_path: Path) -> None:
    config = _config(tmp_path)
    account = AccountPoolStore(config).list_accounts()[0]
    end = datetime(2026, 8, 28, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    rows = [
        _row(account, 7600000000000003100 + day, end - timedelta(days=day), actual_name="量子位官方")
        for day in range(6)
    ]
    result = evaluate_account(account, rows, config, target_date="2026-08-28")

    assert result["identity_status"] == "matched"
    assert result["evaluation_recommendation"] != "do_not_use"
    assert any("显示名称变化" in reason for reason in result["recommendation_reasons"])


def test_masked_display_name_matches_when_stable_id_is_absent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    account = AccountPoolStore(config).list_accounts()[0]
    end = datetime(2026, 8, 28, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    rows = [
        _row(account, 7600000000000003150 + day, end - timedelta(days=day), actual_name="量***位", include_identity=False)
        for day in range(6)
    ]
    result = evaluate_account(account, rows, config, target_date="2026-08-28")

    assert result["identity_status"] == "matched"
    assert result["identity_evidence"] == "masked_display_name"
    assert result["evaluation_recommendation"] == "supplemental_candidate"


def test_raw_duration_is_milliseconds_but_explicit_seconds_stays_seconds(tmp_path: Path) -> None:
    config = _config(tmp_path)
    account = AccountPoolStore(config).list_accounts()[0]
    end = datetime(2026, 8, 28, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    millisecond_row = _row(account, 7600000000000003201, end, duration=1_000)
    explicit_seconds_row = _row(account, 7600000000000003202, end - timedelta(hours=1), duration=None)
    explicit_seconds_row["duration_seconds"] = 1_001

    result = evaluate_account(account, [millisecond_row, explicit_seconds_row], config, target_date="2026-08-28")
    durations = {item["video_id"]: item["duration_seconds"] for item in result["recent_posts"]}

    assert durations["7600000000000003201"] == 1.0
    assert durations["7600000000000003202"] == 1_001.0


def test_single_account_failure_keeps_other_result_and_overall_partial(tmp_path: Path) -> None:
    config = _config(tmp_path)
    accounts = AccountPoolStore(config).list_accounts()
    end = datetime(2026, 8, 28, 12, tzinfo=ZoneInfo("Asia/Shanghai"))

    def collector(_config, account, _root, _maximum):
        if account["account_id"] == accounts[0]["account_id"]:
            return {
                "status": "success",
                "rows": [_row(account, 7600000000000004000 + day, end - timedelta(days=day)) for day in range(5)],
                "error": None,
            }
        return {"status": "failed", "rows": [], "error": "采集进程未返回作品"}

    report = run_account_evaluation(
        config,
        account_ids=[item["account_id"] for item in accounts],
        target_date="2026-08-28",
        live=True,
        collector=collector,
    )
    assert report["status"] == "partial"
    assert [item["status"] for item in report["accounts"]] == ["success", *("failed" for _ in accounts[1:])]
    assert Path(report["artifacts"]["json"]).is_file()
    assert Path(report["artifacts"]["markdown"]).is_file()


def test_needs_login_stops_remaining_external_collection_without_retry(tmp_path: Path) -> None:
    config = _config(tmp_path)
    accounts = AccountPoolStore(config).list_accounts()
    calls = []

    def collector(_config, account, _root, _maximum):
        calls.append(account["account_id"])
        return {"status": "needs_login", "rows": [], "error": "请在项目专用浏览器完成登录"}

    report = run_account_evaluation(
        config,
        account_ids=[item["account_id"] for item in accounts],
        target_date="2026-08-28",
        live=True,
        collector=collector,
    )
    assert report["status"] == "needs_login"
    assert len(calls) == 1
    assert len(report["accounts"]) == len(accounts)
    assert all(item["status"] == "needs_login" for item in report["accounts"])


def test_partial_needs_login_keeps_project_browser_for_human_action(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    accounts = AccountPoolStore(config).list_accounts()
    end = datetime(2026, 8, 28, 12, tzinfo=ZoneInfo("Asia/Shanghai"))

    class FakeBrowserSession:
        instance = None

        def __init__(self, *_args, **_kwargs):
            self.finished = None
            FakeBrowserSession.instance = self

        def prepare(self):
            return {"page_count": 1}

        def finish(self, status, *, human_required=False):
            self.finished = (status, human_required)

    calls = []

    def fake_collector(_config, account, _root, _maximum):
        calls.append(account["account_id"])
        if len(calls) == 1:
            return {"status": "success", "rows": [_row(account, 7600000000000004100, end)], "error": None}
        return {"status": "needs_login", "rows": [], "error": "请在项目专用浏览器完成登录"}

    monkeypatch.setattr(account_evaluation_module, "BrowserSession", FakeBrowserSession)
    monkeypatch.setattr(account_evaluation_module, "collect_candidate_metadata", fake_collector)
    report = run_account_evaluation(
        config,
        account_ids=[item["account_id"] for item in accounts],
        target_date="2026-08-28",
        live=True,
    )

    assert report["status"] == "partial"
    assert report["counts"]["needs_login"] == len(accounts) - 1
    assert FakeBrowserSession.instance.finished == ("partial", True)
    assert "等待登录" in account_pool_status_text({"status": "partial", "counts": {"needs_login": 1}})


def test_metadata_only_report_has_zero_media_asr_ocr_llm_and_removes_sensitive_fields(tmp_path: Path) -> None:
    config = _config(tmp_path)
    account = AccountPoolStore(config).list_accounts()[0]
    end = datetime(2026, 8, 28, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    row = _row(account, 7600000000000005000, end)
    authorization_value = "Bearer" + " hidden"
    row.update({"cookie": "secret-cookie-value", "play_addr": "https://signed.invalid/media", "authorization": authorization_value})

    report = run_account_evaluation(
        config,
        account_ids=[account["account_id"]],
        target_date="2026-08-28",
        input_rows={account["account_id"]: [row]},
    )
    assert report["budgets"] == {
        "max_posts_per_account": 30,
        "concurrency": 1,
        "media_downloads": 0,
        "asr_attempts": 0,
        "ocr_attempts": 0,
        "llm_requests": 0,
        "visual_samples": 0,
    }
    rendered = Path(report["artifacts"]["json"]).read_text(encoding="utf-8")
    for secret in ("secret-cookie-value", "signed.invalid", authorization_value, "play_addr", "authorization"):
        assert secret not in rendered


def test_visual_sampling_is_disabled_and_global_hard_limit_is_three(tmp_path: Path) -> None:
    job = _config(tmp_path)["jobs"]["account_pool"]
    assert job["visual_sampling"]["enabled"] is False
    assert job["visual_sampling"]["global_max_samples"] == 3
    assert job["visual_sampling"]["audio_policy"] == "never"


def test_workbench_command_uses_account_pool_metadata_path_only(tmp_path: Path) -> None:
    command = account_pool_evaluation_command(
        "config/content_intelligence.json", account_id="MS4wLjABAAAAabcdefghijklmnopqrstuv", live=True
    )
    assert "account-pool" in command and "evaluate" in command and "--live" in command
    for forbidden in ("trusted-account-news", "trusted-ai-brief", "ocr", "asr", "scheduler", "download"):
        assert forbidden not in command
    assert "运行中" in account_pool_status_text({"status": "running"})
    assert "等待登录" in account_pool_status_text({"status": "needs_login"})
    assert "身份不匹配" in account_pool_status_text({"status": "partial", "counts": {"identity_mismatch": 1}})


def test_initial_candidates_are_observation_only(tmp_path: Path) -> None:
    accounts = AccountPoolStore(_config(tmp_path)).list_accounts()
    assert {item["display_name"] for item in accounts} == {"量子位", "36氪", "大计算AI产业", "大计算AI产业平台", "大计算算力炼丹炉"}
    assert all(item["lifecycle_status"] == "candidate" for item in accounts)
    assert {item["editorial_lane"] for item in accounts if item.get("production_role") == "topic_radar"} == {"ai_general", "compute_infrastructure", "chips_hardware"}
    assert all(item["enabled"] is True for item in accounts)
