from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from .artifact_safety import sanitize_raw_files
from .collector import BrowserSession, make_run_id
from .config import resolve_path
from .exporter import atomic_write_json


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "search"


def controlled_keywords(config: dict[str, Any], limit: int = 10) -> list[str]:
    preferred = config["jobs"]["inspiration"].get("search_keywords") or []
    values = preferred or [keyword for keywords in config.get("categories", {}).values() for keyword in keywords]
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text.casefold() not in {item.casefold() for item in result}:
            result.append(text)
        if len(result) >= min(10, max(1, limit)):
            break
    return result


def _effective_publish_time_type(publish_time_type: int | None) -> int:
    """The publish-time filter MediaCrawler actually runs with.

    Single source of truth shared by ``search_command`` (the CLI token) and
    ``collect_search`` (the value recorded in ``search_report.json``), so the
    window the crawler runs with and the window we log can never diverge.
    ``None`` (the historical default) resolves to ``1`` (one day); an int or a
    numeric string resolves to its ``int`` value.
    """
    if publish_time_type is None:
        return 1
    return int(publish_time_type)


def search_command(
    config: dict[str, Any],
    destination: Path,
    total_budget: int,
    keywords: list[str],
    *,
    publish_time_type: int | None = None,
) -> list[str]:
    """Build the MediaCrawler search invocation.

    ``publish_time_type`` is the upstream publish-time filter (MediaCrawler
    ``PublishTimeType``: ``0`` = unlimited / ``1`` = one day / ``7`` = one week /
    ``180`` = half a year).  ``None`` (the default) keeps the historical one-day
    window (``"1"``) byte-identically, so ``daily_news`` / ``inspiration`` /
    ``douyin_ranking`` / ``daily_hot_candidate_pool`` are unaffected; callers
    that need the full history pass ``0`` explicitly (see
    ``jobs.material_replication.search.publish_time_type``).
    """
    if not keywords:
        raise ValueError("没有可用搜索词")
    crawler = config["media_crawler"]
    per_keyword = max(10, math.ceil(total_budget / len(keywords)))
    runner = Path(__file__).with_name("mediacrawler_runner.py").resolve()
    publish_value = str(_effective_publish_time_type(publish_time_type))
    return [
        str(resolve_path(crawler["python"])), str(runner), "--crawler-root", str(resolve_path(crawler["root"])),
        "--cdp-port", str(int(crawler["cdp_port"])), "--navigation-timeout", str(int(crawler.get("navigation_timeout_seconds") or 90)),
        "--publish-time-type", publish_value, "--", "--platform", "dy", "--type", "search", "--lt", "qrcode",
        "--save_data_option", "jsonl", "--save_data_path", str(destination), "--crawler_max_notes_count", str(per_keyword),
        "--get_comment", "false", "--get_sub_comment", "false", "--max_concurrency_num", "1", "--headless", "false",
        "--keywords", ",".join(keywords),
    ]


def collect_search(config: dict[str, Any], total_budget: int, run_id: str | None = None, *, keywords: list[str] | None = None, hard_max: int | None = None, keep_browser_on_failure: bool = False, before_sanitize: Callable[[list[Path]], None] | None = None, publish_time_type: int | None = None) -> dict[str, Any]:
    hard_max = int(hard_max or config["jobs"]["inspiration"].get("hard_max_reference_videos") or 100)
    budget = min(max(1, int(total_budget)), hard_max)
    keywords = keywords or controlled_keywords(config)
    keywords = [str(value).strip() for value in keywords if str(value).strip()]
    # Upstream obtains at least ten results per keyword. Keep query count bounded
    # so the raw request plan cannot exceed the declared global budget.
    keywords = keywords[:max(1, min(len(keywords), budget // 10))]
    root = resolve_path(config["media_crawler"]["runs_output"])
    run_dir = root / _safe(run_id or f"search-{make_run_id(config)}")
    destination = run_dir / "search"
    destination.mkdir(parents=True, exist_ok=True)
    browser_session = BrowserSession(config, "collect_search")
    browser = browser_session.prepare()
    command = search_command(config, destination, budget, keywords, publish_time_type=publish_time_type)
    timeout_seconds = max(10, int(config["media_crawler"].get("collection_timeout_seconds") or 120))
    failure = None
    final_status = "failed"
    try:
        try:
            completed = subprocess.run(
                command,
                cwd=resolve_path(config["media_crawler"]["root"]),
                check=False,
                timeout=timeout_seconds,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            completed = None
            failure = f"crawler timed out after {timeout_seconds} seconds"
        files = sorted([*destination.rglob("search_contents_*.json"), *destination.rglob("search_contents_*.jsonl")])
        inspection_error: Exception | None = None
        if files and before_sanitize is not None:
            try:
                before_sanitize(files)
            except Exception as exc:  # Sanitization must still run before the bounded callback error escapes.
                inspection_error = exc
        sanitization = sanitize_raw_files(files, config, "douyin_search", maximum_records=budget) if files else []
        if inspection_error is not None:
            raise inspection_error
        if completed is not None and completed.returncode == 0 and files:
            final_status = "success"
        elif completed is not None and completed.returncode == 0:
            # The upstream process completed cleanly but did not emit a
            # normalized search file.  This is an empty external observation,
            # not a local crawler crash; callers must retain the distinction.
            final_status = "empty"
            failure = "crawler completed without output files"
        else:
            final_status = "failed"
        report = {
            "status": final_status,
            "run_dir": str(run_dir.resolve()),
            "budget": budget,
            "publish_time_type": _effective_publish_time_type(publish_time_type),
            "keywords": keywords,
            "per_keyword_budget": max(10, math.ceil(budget / len(keywords))),
            "raw_request_ceiling": max(10, math.ceil(budget / len(keywords))) * len(keywords),
            "files": [str(path.resolve()) for path in files],
            "returncode": int(completed.returncode) if completed is not None else None,
            "timeout_seconds": timeout_seconds,
            "error": failure,
            "output_observation": "files_present" if files else "no_output_files",
            "sanitization": sanitization,
            "browser": browser,
        }
        atomic_write_json(run_dir / "search_report.json", report)
        return report
    finally:
        if keep_browser_on_failure and final_status == "failed":
            browser_session.finish(final_status, human_required=True)
        else:
            browser_session.finish(final_status)
