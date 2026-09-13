from __future__ import annotations

import copy
import subprocess
import types
from pathlib import Path

import pytest

from douyin_intelligence.config import load_config
from douyin_intelligence.replication_candidates import collect_candidate_pool
from douyin_intelligence.scheduler import _run, task_xml
from douyin_intelligence.search_collector import collect_search, controlled_keywords, search_command


def test_scheduler_xml_has_daily_safety_settings_and_no_secret() -> None:
    config = load_config()
    xml = task_xml(config)
    assert "T02:00:00" in xml
    assert "<WakeToRun>true</WakeToRun>" in xml
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    assert "sk-" not in xml
    assert "daily-news --live-douyin --scheduled" in xml


def test_search_command_ceiling_is_global_budget() -> None:
    config = load_config()
    keywords = controlled_keywords(config)
    command = search_command(config, Path("example"), 100, keywords)
    per_keyword = int(command[command.index("--crawler_max_notes_count") + 1])
    assert len(keywords) <= 10
    assert per_keyword * len(keywords) <= 100


def test_collect_search_has_finite_timeout_and_sanitized_failure(tmp_path: Path, monkeypatch) -> None:
    config = load_config()
    config["media_crawler"]["runs_output"] = str(tmp_path / "runs")
    config["media_crawler"]["root"] = str(tmp_path / "crawler")
    config["media_crawler"]["collection_timeout_seconds"] = 17
    (tmp_path / "crawler").mkdir()
    observed: dict[str, int] = {}

    class FakeSession:
        def __init__(self, *_args, **_kwargs): pass
        def prepare(self): return {"status": "reused", "port": 9223, "page_count": 1}
        def finish(self, _status): return {"state": "completed_closed"}

    monkeypatch.setattr("douyin_intelligence.search_collector.BrowserSession", FakeSession)

    def expire(_command, **kwargs):
        observed["timeout"] = kwargs["timeout"]
        raise subprocess.TimeoutExpired("crawler", kwargs["timeout"])

    monkeypatch.setattr("douyin_intelligence.search_collector.subprocess.run", expire)
    report = collect_search(config, 10, "timeout-test")

    assert observed["timeout"] == 17
    assert report["status"] == "failed"
    assert report["returncode"] is None
    assert report["files"] == []
    assert report["error"] == "crawler timed out after 17 seconds"
    assert report["raw_request_ceiling"] == 10


def test_collect_search_distinguishes_clean_empty_upstream_output(tmp_path: Path, monkeypatch) -> None:
    config = load_config()
    config["media_crawler"]["runs_output"] = str(tmp_path / "runs")
    config["media_crawler"]["root"] = str(tmp_path / "crawler")
    (tmp_path / "crawler").mkdir()

    class FakeSession:
        def __init__(self, *_args, **_kwargs): pass
        def prepare(self): return {"status": "reused", "port": 9223, "page_count": 1}
        def finish(self, _status): return {"state": "completed_closed"}

    monkeypatch.setattr("douyin_intelligence.search_collector.BrowserSession", FakeSession)
    monkeypatch.setattr("douyin_intelligence.search_collector.subprocess.run", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0))
    report = collect_search(config, 10, "empty-test")

    assert report["status"] == "empty"
    assert report["returncode"] == 0 and report["files"] == []
    assert report["error"] == "crawler completed without output files"
    assert report["output_observation"] == "no_output_files"


def test_scheduler_timeout_degrades_to_a_non_success_result(monkeypatch) -> None:
    def expire(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("schtasks.exe", 15)

    monkeypatch.setattr("douyin_intelligence.scheduler.subprocess.run", expire)
    result = _run(["/Query", "/TN", "CopySkillDailyTechNews"])

    assert result.returncode == 124
    assert "timed out" in result.stderr


def test_search_command_publish_time_default_stays_one_day() -> None:
    # Hard constraint: the default path must keep emitting the historical
    # ``--publish-time-type 1`` (daily_news / inspiration / douyin_ranking /
    # daily_hot_candidate_pool all share ``search_command``).
    config = load_config()
    keywords = controlled_keywords(config)
    command = search_command(config, Path("example"), 100, keywords)
    index = command.index("--publish-time-type")
    assert command[index + 1] == "1"
    # An explicit ``None`` is byte-identical to the no-argument default.
    assert command == search_command(config, Path("example"), 100, keywords, publish_time_type=None)


def test_search_command_publish_time_override_is_honored() -> None:
    config = load_config()
    keywords = controlled_keywords(config)
    for value, expected in ((0, "0"), (7, "7")):
        command = search_command(config, Path("example"), 100, keywords, publish_time_type=value)
        index = command.index("--publish-time-type")
        assert command[index + 1] == expected


def test_collect_candidate_pool_forwards_publish_time_only_when_configured(tmp_path: Path) -> None:
    # ``jobs.material_replication.search.publish_time_type`` reaches the
    # collector only when the key is present; an absent key leaves the call --
    # and every old-signature test double -- untouched.
    seen: list[dict] = []

    def spy(cfg, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None, **kwargs):
        seen.append(kwargs)
        return {"status": "success", "budget": budget, "keywords": list(keywords or [])}

    with_key = copy.deepcopy(load_config())
    with_key["_project_root"] = str(tmp_path)
    with_key["jobs"]["material_replication"]["search"]["publish_time_type"] = 0
    collect_candidate_pool(with_key, "苹果折叠屏", pool_size=40, run_id="r", deps=types.SimpleNamespace(collector=spy))
    assert seen[-1] == {"publish_time_type": 0}

    without_key = copy.deepcopy(load_config())
    without_key["_project_root"] = str(tmp_path)
    without_key["jobs"]["material_replication"]["search"].pop("publish_time_type", None)
    collect_candidate_pool(without_key, "苹果折叠屏", pool_size=40, run_id="r", deps=types.SimpleNamespace(collector=spy))
    assert seen[-1] == {}


def _collect_config(tmp_path: Path) -> dict:
    config = load_config()
    config["media_crawler"]["runs_output"] = str(tmp_path / "runs")
    config["media_crawler"]["root"] = str(tmp_path / "crawler")
    (tmp_path / "crawler").mkdir()
    return config


def _run_collect_search(config: dict, run_id: str, *, publish_time_type, monkeypatch, seen: dict | None = None) -> dict:
    class FakeSession:
        def __init__(self, *_args, **_kwargs): pass
        def prepare(self): return {"status": "reused", "port": 9223, "page_count": 1}
        def finish(self, _status): return {"state": "completed_closed"}

    monkeypatch.setattr("douyin_intelligence.search_collector.BrowserSession", FakeSession)

    def fake_run(command, **_kwargs):
        if seen is not None:
            seen["command"] = command
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("douyin_intelligence.search_collector.subprocess.run", fake_run)
    return collect_search(config, 10, run_id, publish_time_type=publish_time_type)


def test_collect_search_reports_default_publish_time_type(tmp_path: Path, monkeypatch) -> None:
    # ``05-过程数据/search_report.json`` must record the *effective* window so a
    # delivered run can be read back without forensic guessing; ``None`` keeps
    # the historical one-day window (``1``).
    config = _collect_config(tmp_path)
    report = _run_collect_search(config, "default-window", publish_time_type=None, monkeypatch=monkeypatch)
    assert report["publish_time_type"] == 1


def test_collect_search_reports_explicit_publish_time_type(tmp_path: Path, monkeypatch) -> None:
    config = _collect_config(tmp_path)
    report = _run_collect_search(config, "unlimited-window", publish_time_type=0, monkeypatch=monkeypatch)
    assert report["publish_time_type"] == 0


@pytest.mark.parametrize("value", [None, 0, 1, 7])
def test_command_token_and_report_publish_time_type_agree(value, tmp_path: Path, monkeypatch) -> None:
    # Guard against a one-sided future change: the token the crawler actually
    # runs with and the int we log must be numerically equal for every window.
    config = _collect_config(tmp_path)
    seen: dict = {}
    report = _run_collect_search(config, "agree", publish_time_type=value, monkeypatch=monkeypatch, seen=seen)
    cli_token = seen["command"][seen["command"].index("--publish-time-type") + 1]
    assert report["publish_time_type"] == (1 if value is None else int(value))
    assert int(cli_token) == report["publish_time_type"]
