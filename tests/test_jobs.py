from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest

from douyin_intelligence.config import load_config
from douyin_intelligence.job_runtime import JobLock
from douyin_intelligence.jobs import run_daily_news, run_inspiration


ROOT = Path(__file__).parents[1]


def _config(tmp_path: Path):
    config = copy.deepcopy(load_config())
    config["materials"]["llm"]["enabled"] = False
    config["jobs"]["state_path"] = str(tmp_path / "state.json")
    config["jobs"]["lock_root"] = str(tmp_path / "locks")
    config["jobs"]["daily_news"]["output_root"] = str(tmp_path / "news")
    config["jobs"]["inspiration"]["output_root"] = str(tmp_path / "ideas")
    config["materials"]["retention"]["temp_root"] = str(tmp_path / "temp")
    return config


def test_daily_report_enforces_target_day_and_fixed_fields(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["jobs"]["daily_news"]["sources"] = [{"name": "Official", "url": str(ROOT / "tests/fixtures/news_feed.xml"), "kind": "official"}]
    result = run_daily_news(config, target_date="2026-08-25", douyin_inputs=[str(ROOT / "tests/fixtures/search_contents_2026-08-25.json")])
    assert len(result["items"]) == 1
    markdown = Path(result["output_path"]).read_text(encoding="utf-8")
    for field in ("推荐级别", "时间", "主体/地点", "事件", "新闻价值", "抖音热度", "权威来源", "核验状态", "待核验", "创作角度", "60–90 秒口播总稿", "AI 深度分析状态"):
        assert field in markdown
    assert result["analysis_status"]["enabled"] is False
    assert result["items"][0]["event"] != result["items"][0]["title"]


def test_inspiration_obeys_all_three_limits_and_contract(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["jobs"]["inspiration"].update({"hard_max_reference_videos": 1, "max_detail_videos": 1, "media_analysis_videos": 1, "hard_max_media_videos": 1})
    result = run_inspiration(config, max_references=99, douyin_inputs=[str(ROOT / "tests/fixtures/search_contents_2026-08-25.json")], media=False)
    assert result["stats"]["reference_count"] == 1
    assert result["stats"]["detail_count"] == 1
    assert result["stats"]["media_count"] == 0
    markdown = Path(result["output_path"]).read_text(encoding="utf-8")
    for field in ("推荐度", "一句话灵感", "有趣之处", "内容骨架", "参考素材", "待核验", "推荐标题"):
        assert field in markdown


def test_job_lock_rejects_concurrency_and_recovers_stale_lock(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = JobLock(config, "daily_news")
    assert first.acquire()
    assert not JobLock(config, "daily_news").acquire()
    first.release()
    stale = Path(config["jobs"]["lock_root"]) / "daily_news.lock"
    stale.write_text('{"pid": 99999999, "host": "' + __import__("socket").gethostname() + '"}', encoding="utf-8")
    recovered = JobLock(config, "daily_news")
    assert recovered.acquire()
    recovered.release()


def test_windows_pid_probe_never_sends_a_console_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name != "nt":
        pytest.skip("Windows-specific regression test")

    def fail_if_called(pid: int, signal_number: int) -> None:
        raise AssertionError(f"os.kill must not be used on Windows: pid={pid}, signal={signal_number}")

    monkeypatch.setattr(os, "kill", fail_if_called)
    assert JobLock._pid_alive(os.getpid())
    assert not JobLock._pid_alive(99_999_999)
