from __future__ import annotations

from douyin_intelligence.config import load_config
from douyin_intelligence.scheduler import _run, task_xml
import subprocess

from douyin_intelligence.search_collector import collect_search, controlled_keywords, search_command
from pathlib import Path


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
