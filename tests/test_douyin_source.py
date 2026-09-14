"""Offline tests for :class:`DouyinSource`.

A fake ``collect_search`` is injected, so nothing touches the network or the
crawler.  The tests pin the security-critical contract -- a signed
``video_download_url`` is captured in memory and never appears in the adapter's
``candidates`` or ``report`` -- plus the mapping, the resolve hit/miss paths, and
the graceful-degradation paths for both an exception and a ``status="failed"``
report.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from douyin_intelligence import sources
from douyin_intelligence.config import load_config
from douyin_intelligence.replication_candidates import Candidate
from douyin_intelligence.sources.douyin import DouyinSource
from douyin_intelligence.sources.ytdlp import SENSITIVE_REPORT_KEYS


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
VIDEO_A = "11111111111"
VIDEO_B = "22222222222"
SIGNED_A = "https://signed.example/dl/11111111111?token=SECRET_A"
SIGNED_B = "https://signed.example/dl/22222222222?token=SECRET_B"


def _raw_row(video_id: str, keyword: str, url: str) -> dict:
    return {
        "aweme_id": video_id,
        "desc": f"标题-{video_id}",
        "author": {"uid": f"uid-{video_id}", "nickname": f"作者{video_id}"},
        "create_time": "2026-09-11T08:00:00+08:00",
        "statistics": {
            "digg_count": 100,
            "comment_count": 10,
            "share_count": 5,
            "collect_count": 20,
            "play_count": 1000,
        },
        "duration": 60,
        "video_download_url": url,
        "share_url": f"https://www.douyin.com/video/{video_id}",
        "source_keyword": keyword,
    }


def _report(**overrides) -> dict:
    report = {
        "status": "success",
        "run_dir": "runs/search-x",
        "budget": 40,
        "publish_time_type": 1,
        "keywords": ["猫咪"],
        "per_keyword_budget": 40,
        "raw_request_ceiling": 40,
        "files": ["runs/search-x/search/search_contents_1.jsonl"],
        "returncode": 0,
        "timeout_seconds": 120,
        "error": None,
        "output_observation": "files_present",
        "sanitization": [],
        "browser": {"status": "ok"},
    }
    report.update(overrides)
    return report


def _make_collector(tmp_path: Path, rows: list[dict], report: dict):
    """A fake ``collect_search`` that writes ``rows`` and hands them to the callback."""

    def collector(config, total_budget, run_id=None, *, keywords=None, hard_max=None, before_sanitize=None, publish_time_type=None, **kwargs):
        collector.calls.append(  # type: ignore[attr-defined]
            {
                "config": config,
                "budget": total_budget,
                "run_id": run_id,
                "keywords": list(keywords or []),
                "hard_max": hard_max,
                "publish_time_type": publish_time_type,
            }
        )
        if before_sanitize is not None:
            path = tmp_path / "search_contents_1.jsonl"
            path.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
                encoding="utf-8",
            )
            # The real collector passes the on-disk files *before* sanitizing;
            # the adapter must read the signed URL out of them here.
            before_sanitize([path])
        return dict(report)

    collector.calls = []  # type: ignore[attr-defined]
    return collector


def _rows() -> list[dict]:
    return [
        _raw_row(VIDEO_A, "猫咪", SIGNED_A),
        _raw_row(VIDEO_B, "猫咪", SIGNED_B),
    ]


def _walk_keys(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield str(key)
            yield from _walk_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


def _walk_strings(obj):
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _walk_strings(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_strings(item)
    elif isinstance(obj, str):
        yield obj


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_search_maps_candidates_and_keywords(tmp_path: Path) -> None:
    collector = _make_collector(tmp_path, _rows(), _report())
    source = DouyinSource(collector=collector)

    result = source.search(["猫咪"], 40, config=load_config(), run_id="run-1")

    assert result.source == "douyin"
    assert result.status == "success"
    assert result.keywords_requested == ["猫咪"]
    assert result.keywords_used == ["猫咪"]
    assert {candidate.video_id for candidate in result.candidates} == {VIDEO_A, VIDEO_B}
    assert all(candidate.heat_rank >= 1 for candidate in result.candidates)
    assert all(candidate.duration_seconds == 60.0 for candidate in result.candidates)
    # The adapter forwarded budget/keywords/run_id straight through.
    assert collector.calls[-1]["budget"] == 40
    assert collector.calls[-1]["keywords"] == ["猫咪"]
    assert collector.calls[-1]["run_id"] == "run-1"


def test_resolve_media_url_hit_and_miss(tmp_path: Path) -> None:
    collector = _make_collector(tmp_path, _rows(), _report())
    source = DouyinSource(collector=collector)
    result = source.search(["猫咪"], 40, config=load_config())

    by_id = {candidate.video_id: candidate for candidate in result.candidates}
    assert source.resolve_media_url(by_id[VIDEO_A]) == SIGNED_A
    assert source.resolve_media_url(by_id[VIDEO_B]) == SIGNED_B
    # Unknown candidate -> "" (never raises for the Douyin path).
    assert source.resolve_media_url(Candidate(video_id="does-not-exist")) == ""


def test_signed_urls_never_leak_into_candidates_or_report(tmp_path: Path) -> None:
    collector = _make_collector(tmp_path, _rows(), _report())
    source = DouyinSource(collector=collector)
    result = source.search(["猫咪"], 40, config=load_config())

    # The signed URL is only exposed through resolve_media_url (in memory).
    candidate_blob = json.dumps([c.to_dict() for c in result.candidates], ensure_ascii=False)
    assert "signed.example" not in candidate_blob
    assert "SECRET_A" not in candidate_blob and "SECRET_B" not in candidate_blob

    report_blob = json.dumps(result.report, ensure_ascii=False)
    assert "signed.example" not in report_blob
    assert "SECRET_A" not in report_blob and "SECRET_B" not in report_blob
    # No URL of any kind sneaks into the disk-bound report.
    assert all("http" not in value for value in _walk_strings(result.report))
    assert set(_walk_keys(result.report)).isdisjoint(SENSITIVE_REPORT_KEYS)


def test_report_is_whitelisted(tmp_path: Path) -> None:
    report = _report(secret_token="SIGNED-LEAK", formats=[{"url": SIGNED_A}])
    collector = _make_collector(tmp_path, _rows(), report)
    source = DouyinSource(collector=collector)
    result = source.search(["猫咪"], 40, config=load_config())

    # Unknown upstream keys are dropped by the whitelist.
    assert "secret_token" not in result.report
    assert "formats" not in result.report
    # A whitelisted key is preserved with its value.
    assert result.report["status"] == "success"
    assert result.report["publish_time_type"] == 1


def test_collect_exception_degrades_to_failed(tmp_path: Path) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("crawler boom")

    source = DouyinSource(collector=boom)
    result = source.search(["猫咪"], 40, config=load_config())

    assert result.status == "failed"
    assert "boom" in result.error
    assert result.candidates == []
    assert result.report["status"] == "failed"


def test_failed_crawl_report_degrades_to_failed(tmp_path: Path) -> None:
    collector = _make_collector(
        tmp_path, [], _report(status="failed", error="crawler timed out after 120 seconds", keywords=[])
    )
    source = DouyinSource(collector=collector)
    result = source.search(["猫咪"], 40, config=load_config())

    assert result.status == "failed"
    assert "timed out" in result.error


def test_empty_crawl_report_maps_to_empty_status(tmp_path: Path) -> None:
    collector = _make_collector(tmp_path, [], _report(status="empty", keywords=["猫咪"]))
    source = DouyinSource(collector=collector)
    result = source.search(["猫咪"], 40, config=load_config())

    assert result.status == "empty"
    assert result.candidates == []


def test_keyword_truncation_is_reported(tmp_path: Path) -> None:
    # The crawler searched fewer keywords than requested (budget // 10 slice).
    collector = _make_collector(tmp_path, _rows(), _report(keywords=["猫咪"]))
    source = DouyinSource(collector=collector)
    result = source.search(["猫咪", "狗", "鸟"], 20, config=load_config())

    assert result.keywords_requested == ["猫咪", "狗", "鸟"]
    assert result.keywords_used == ["猫咪"]
    assert result.warnings  # a truncation note is surfaced


def test_publish_time_type_forwarded_only_when_configured(tmp_path: Path) -> None:
    collector = _make_collector(tmp_path, _rows(), _report())
    source = DouyinSource(collector=collector)

    # A config that predates the key: nothing is forwarded, so an injected
    # double with the historical signature keeps working.
    config = load_config()
    config["jobs"]["material_replication"].setdefault("search", {}).pop("publish_time_type", None)
    source.search(["猫咪"], 40, config=config)
    assert collector.calls[-1]["publish_time_type"] is None

    # Present and int-parseable -> forwarded.
    config["jobs"]["material_replication"]["search"]["publish_time_type"] = 0
    source.search(["猫咪"], 40, config=config)
    assert collector.calls[-1]["publish_time_type"] == 0

    # Present but not int-parseable -> NOT forwarded (historical signature).
    config["jobs"]["material_replication"]["search"]["publish_time_type"] = "not-an-int"
    source.search(["猫咪"], 40, config=config)
    assert collector.calls[-1]["publish_time_type"] is None


def test_empty_keywords_skips_the_crawler(tmp_path: Path) -> None:
    collector = _make_collector(tmp_path, _rows(), _report())
    source = DouyinSource(collector=collector)
    result = source.search([], 40, config=load_config())

    assert result.status == "empty"
    assert collector.calls == []


def test_registered_in_package() -> None:
    assert "douyin" in sources.known_sources()
    assert isinstance(sources.get_source("douyin"), DouyinSource)
