from __future__ import annotations

import json
import math
import os
import re
import shutil
import statistics
import subprocess
import time as monotonic_time
import uuid
import copy
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .account_pool import AccountPoolStore
from .collector import BrowserSession
from .exporter import atomic_write_json
from .job_runtime import JobLock, JobState, now_iso
from .normalize import load_raw_records, normalize_record, parse_count
from .reporting import atomic_text
from .trusted_news import trusted_creator_command


INTERACTIONS = {
    "like": ("digg_count", "liked_count", "like_count"),
    "comment": ("comment_count",),
    "collect": ("collect_count", "collected_count"),
    "share": ("share_count",),
}
AUTH_MARKERS = ("qrcode", "scan", "login", "captcha", "verify", "扫码", "登录", "验证")


def _project_path(config: dict[str, Any], value: str | Path) -> Path:
    path = Path(value)
    root = Path(config.get("_project_root") or Path(__file__).resolve().parents[2])
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def evaluation_window(target_date: str | date, timezone_name: str) -> tuple[datetime, datetime]:
    day = date.fromisoformat(target_date) if isinstance(target_date, str) else target_date
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(day - timedelta(days=13), time.min, tzinfo=zone)
    end = datetime.combine(day, time.max, tzinfo=zone)
    return start, end


def _deep_get(item: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        value: Any = item
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                value = None
                break
            value = value[part]
        if value not in (None, ""):
            return value
    return None


def _duration_seconds(item: dict[str, Any]) -> float | None:
    seconds_value = _deep_get(item, "duration_seconds")
    if seconds_value not in (None, "") and not isinstance(seconds_value, bool):
        try:
            seconds = float(seconds_value)
        except (TypeError, ValueError):
            seconds = -1
        if math.isfinite(seconds) and seconds >= 0:
            return round(seconds, 3)

    value = _deep_get(item, "duration", "video.duration", "aweme_info.duration")
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    # Douyin's raw duration fields are milliseconds. Keep the explicit
    # duration_seconds field above separate so a legitimately long video is
    # never divided a second time.
    return round(number / 1_000, 3)


def _identity_value(item: dict[str, Any]) -> str:
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    return str(item.get("sec_uid") or item.get("sec_user_id") or author.get("sec_uid") or "").strip()


def _name_key(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)


def _display_name_matches(expected: str, observed: str) -> bool:
    expected_key = _name_key(expected)
    observed_text = str(observed or "").strip().casefold()
    if not expected_key or not observed_text:
        return False
    if "*" not in observed_text:
        return _name_key(observed_text) == expected_key
    parts = [_name_key(part) for part in re.split(r"\*+", observed_text)]
    parts = [part for part in parts if part]
    if not parts:
        return False
    cursor = 0
    for index, part in enumerate(parts):
        position = expected_key.find(part, cursor)
        if position < 0:
            return False
        if index == 0 and not observed_text.startswith("*") and position != 0:
            return False
        cursor = position + len(part)
    if not observed_text.endswith("*") and not expected_key.endswith(parts[-1]):
        return False
    return True


def _has_keyword(text: str, keywords: list[Any]) -> bool:
    corpus = text.casefold()
    return any(str(keyword).strip().casefold() in corpus for keyword in keywords if str(keyword).strip())


def _interaction_value(raw: dict[str, Any], aliases: tuple[str, ...]) -> int | None:
    paths = list(aliases) + [f"statistics.{name}" for name in aliases]
    return parse_count(_deep_get(raw, *paths))


def _ordinary_share_url(video_id: str) -> str:
    return f"https://www.douyin.com/video/{video_id}" if video_id.isdigit() else ""


def _placeholder(account: dict[str, Any], status: str, error: str) -> dict[str, Any]:
    return {
        "account_id": account["account_id"],
        "display_name": account["display_name"],
        "profile_url": account["profile_url"],
        "lifecycle_status": account["lifecycle_status"],
        "enabled": bool(account.get("enabled")),
        "status": status,
        "identity_status": "not_evaluated",
        "identity_evidence": "none",
        "data_quality": "unavailable",
        "evaluation_recommendation": "insufficient_data",
        "recommendation_reasons": [error],
        "metrics": {
            "total_posts": 0,
            "active_days": 0,
            "posts_per_active_day": None,
            "eligible_tech_news_count": 0,
            "technology_news_ratio": None,
            "short_video_ratio": None,
            "duration_missing_rate": None,
            "promotion_suspected_ratio": None,
            "interaction_medians": {key: None for key in INTERACTIONS},
            "interaction_missing_rates": {key: None for key in INTERACTIONS},
        },
        "collection_counts": {"raw_posts": 0, "window_posts_before_limit": 0, "processed_posts": 0},
        "recent_posts": [],
        "error": error,
    }


def evaluate_account(
    account: dict[str, Any],
    rows: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    target_date: str,
) -> dict[str, Any]:
    settings = config["jobs"]["account_pool"]
    start, end = evaluation_window(target_date, str(config["timezone"]))
    maximum = min(30, max(1, int(settings.get("max_posts_per_account") or 30)))
    expected_id = str(account["account_id"])
    expected_name = str(account["display_name"])
    observed_ids = sorted({_identity_value(row) for row in rows if _identity_value(row)})
    observed_names = sorted({str(_deep_get(row, "nickname", "author.nickname", "account_name") or "").strip() for row in rows if str(_deep_get(row, "nickname", "author.nickname", "account_name") or "").strip()})
    id_mismatch = any(value != expected_id for value in observed_ids)
    name_mismatch = any(not _display_name_matches(expected_name, value) for value in observed_names)
    # A stable sec_uid is authoritative. Display names are allowed to change;
    # only use the name as the identity gate when the stable ID is absent.
    identity_mismatch = id_mismatch if observed_ids else name_mismatch
    identity_status = "identity_mismatch" if identity_mismatch else "matched" if observed_ids or observed_names else "insufficient_metadata"
    if identity_status == "identity_mismatch":
        identity_evidence = "mismatch"
    elif observed_ids:
        identity_evidence = "stable_account_id"
    elif any("*" in value for value in observed_names):
        identity_evidence = "masked_display_name"
    elif observed_names:
        identity_evidence = "display_name"
    else:
        identity_evidence = "none"

    candidates: dict[str, dict[str, Any]] = {}
    dropped_outside = 0
    dropped_missing_publish = 0
    for raw in rows:
        record = normalize_record(raw, source="douyin_creator", timezone_name=str(config["timezone"]), categories=config["categories"])
        published = datetime.fromisoformat(record.published_at) if record.published_at else None
        if published is None:
            dropped_missing_publish += 1
            continue
        if published < start or published > end:
            dropped_outside += 1
            continue
        if record.video_id not in candidates:
            candidates[record.video_id] = {"raw": raw, "record": record, "published": published}
    ordered_all = sorted(candidates.values(), key=lambda item: (-item["published"].timestamp(), item["record"].video_id))
    ordered = ordered_all[:maximum]

    technology_keywords = list(settings.get("technology_keywords") or [])
    promotion_keywords = list(settings.get("promotion_keywords") or [])
    short_limit = float(settings.get("short_video_max_seconds") or 180)
    recent_posts: list[dict[str, Any]] = []
    interaction_values: dict[str, list[int]] = {key: [] for key in INTERACTIONS}
    interaction_missing: dict[str, int] = {key: 0 for key in INTERACTIONS}
    duration_known = 0
    short_count = 0
    tech_count = 0
    promotion_count = 0
    active_dates: set[str] = set()
    for item in ordered:
        raw, record, published = item["raw"], item["record"], item["published"]
        active_dates.add(published.date().isoformat())
        text = str(record.title or "")
        tech = _has_keyword(text, technology_keywords)
        promotion = _has_keyword(text, promotion_keywords)
        tech_count += int(tech)
        promotion_count += int(promotion)
        duration = _duration_seconds(raw)
        if duration is not None:
            duration_known += 1
            short_count += int(duration <= short_limit)
        interactions: dict[str, int | None] = {}
        for metric, aliases in INTERACTIONS.items():
            value = _interaction_value(raw, aliases)
            interactions[metric] = value
            if value is None:
                interaction_missing[metric] += 1
            else:
                interaction_values[metric].append(value)
        recent_posts.append({
            "video_id": record.video_id,
            "title": text[:500],
            "published_at": record.published_at,
            "duration_seconds": duration,
            "technology_news_relevant": tech,
            "promotion_suspected": promotion,
            "interactions": interactions,
            "share_url": _ordinary_share_url(record.video_id),
        })

    total = len(ordered)
    active_days = len(active_dates)
    technology_ratio = round(tech_count / total, 4) if total else None
    promotion_ratio = round(promotion_count / total, 4) if total else None
    short_ratio = round(short_count / duration_known, 4) if duration_known else None
    duration_missing_rate = round((total - duration_known) / total, 4) if total else None
    medians = {
        key: (round(float(statistics.median(values)), 2) if values else None)
        for key, values in interaction_values.items()
    }
    interaction_missing_rates = {
        key: (round(interaction_missing[key] / total, 4) if total else None)
        for key in INTERACTIONS
    }
    metrics = {
        "total_posts": total,
        "active_days": active_days,
        "posts_per_active_day": round(total / active_days, 3) if active_days else None,
        "eligible_tech_news_count": tech_count,
        "technology_news_ratio": technology_ratio,
        "short_video_ratio": short_ratio,
        "duration_missing_rate": duration_missing_rate,
        "promotion_suspected_count": promotion_count,
        "promotion_suspected_ratio": promotion_ratio,
        "interaction_medians": medians,
        "interaction_missing_rates": interaction_missing_rates,
    }

    thresholds = settings["thresholds"]
    minimum_sample = int(settings.get("min_sample_posts") or 5)
    duration_coverage = duration_known / total if total else 0.0
    missing_measurements = [value for value in interaction_missing_rates.values() if value is not None]
    average_interaction_missing = sum(missing_measurements) / len(missing_measurements) if missing_measurements else 1.0
    if identity_status == "identity_mismatch":
        recommendation = "do_not_use"
        reasons = ["实际账号身份与候选配置不一致，已停止合格建议。"]
        status = "partial"
    elif total == 0:
        recommendation = "insufficient_data"
        reasons = ["14日窗口内没有可确认的公开作品。"]
        status = "empty"
    elif total < minimum_sample or identity_status == "insufficient_metadata":
        recommendation = "insufficient_data"
        reasons = ["样本数量或账号身份元数据不足，不能形成稳定建议。"]
        status = "partial"
    elif (
        active_days >= int(thresholds["core_min_active_days"])
        and technology_ratio is not None and technology_ratio >= float(thresholds["min_technology_news_ratio"])
        and short_ratio is not None and short_ratio >= float(thresholds["min_short_video_ratio"])
        and promotion_ratio is not None and promotion_ratio <= float(thresholds["max_promotion_ratio"])
        and duration_coverage >= float(thresholds["min_duration_coverage"])
    ):
        recommendation = "core_candidate"
        reasons = ["活跃天数、科技新闻相关度、短视频比例和推广比例均达到核心候选阈值。"]
        status = "success"
    elif (
        active_days >= int(thresholds["supplemental_min_active_days"])
        and technology_ratio is not None and technology_ratio >= float(thresholds["min_technology_news_ratio"])
        and promotion_ratio is not None and promotion_ratio <= float(thresholds["max_promotion_ratio"])
    ):
        recommendation = "supplemental_candidate"
        reasons = ["活跃度和科技内容垂直度达到补充候选阈值；未满足全部核心候选条件。"]
        status = "success"
    elif technology_ratio is not None and technology_ratio < float(thresholds["min_technology_news_ratio"]):
        recommendation = "do_not_use"
        reasons = ["科技新闻相关度低于配置阈值。"]
        status = "success"
    elif promotion_ratio is not None and promotion_ratio > float(thresholds["max_promotion_ratio"]):
        recommendation = "do_not_use"
        reasons = ["疑似广告、课程或纯推广比例高于配置阈值。"]
        status = "success"
    else:
        recommendation = "insufficient_data"
        reasons = ["现有字段或活跃度不足以形成核心/补充候选建议。"]
        status = "partial"

    missing_publish_rate = dropped_missing_publish / len(rows) if rows else 0.0
    if identity_status == "identity_mismatch" or missing_publish_rate > 0.5 or average_interaction_missing > 0.75:
        data_quality = "low"
    elif duration_missing_rate is not None and duration_missing_rate > 0.4 or average_interaction_missing > 0.4:
        data_quality = "medium"
    else:
        data_quality = "high" if total else "unavailable"
    if duration_missing_rate == 1.0:
        reasons.append("作品时长全部缺失，短视频比例保持未知，未按0秒计算。")
    if any(value == 1.0 for value in interaction_missing_rates.values()):
        reasons.append("部分公开互动字段全部缺失，中位数保持未知。")
    if observed_ids and not id_mismatch and name_mismatch:
        reasons.append("稳定账号ID匹配，但观察到显示名称变化；未将改名误判为身份错配。")

    return {
        "account_id": account["account_id"],
        "display_name": account["display_name"],
        "profile_url": account["profile_url"],
        "lifecycle_status": account["lifecycle_status"],
        "enabled": bool(account.get("enabled")),
        "status": status,
        "identity_status": identity_status,
        "identity_evidence": identity_evidence,
        "observed_display_names": observed_names[:5],
        "data_quality": data_quality,
        "evaluation_recommendation": recommendation,
        "recommendation_reasons": reasons,
        "metrics": metrics,
        "collection_counts": {
            "raw_posts": len(rows),
            "window_posts_before_limit": len(ordered_all),
            "processed_posts": total,
            "dropped_outside_window": dropped_outside,
            "dropped_missing_publish_time": dropped_missing_publish,
            "locally_truncated": max(0, len(ordered_all) - total),
        },
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "recent_posts": recent_posts,
        "error": None,
    }


def collect_candidate_metadata(
    config: dict[str, Any], account: dict[str, Any], run_temp: Path, maximum: int
) -> dict[str, Any]:
    destination = run_temp / account["account_id"] / "raw"
    destination.mkdir(parents=True, exist_ok=True)
    command = trusted_creator_command(config, {"profile_url": account["profile_url"]}, destination, maximum)
    timeout = min(
        int(config["jobs"]["account_pool"].get("collection_timeout_seconds") or 120),
        int(config["jobs"]["account_pool"].get("total_timeout_seconds") or 300),
    )
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            command,
            cwd=_project_path(config, config["media_crawler"]["root"]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
            creationflags=flags,
        )
        diagnostic = (completed.stdout + "\n" + completed.stderr).casefold()[:100_000]
        returncode = int(completed.returncode)
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        diagnostic = ((exc.stdout or "") + "\n" + (exc.stderr or "")).casefold()[:100_000]
        returncode = 124
        timed_out = True
    files = sorted([*destination.rglob("creator_contents_*.jsonl"), *destination.rglob("creator_contents_*.json")])
    rows: list[dict[str, Any]] = []
    for path in files:
        rows.extend(load_raw_records(path))
    needs_login = not files and (timed_out or any(marker in diagnostic for marker in AUTH_MARKERS))
    status = "success" if rows and returncode == 0 else "partial" if rows else "empty" if files and returncode == 0 else "needs_login" if needs_login else "failed"
    return {
        "status": status,
        "rows": rows,
        "returncode": returncode,
        "timeout_seconds": timeout,
        "error": "请在项目专用浏览器完成抖音登录/验证后重试" if needs_login else ("采集进程未返回作品" if status == "failed" else None),
    }


def _overall_status(results: list[dict[str, Any]]) -> str:
    if not results or all(item["status"] == "empty" for item in results):
        return "empty"
    statuses = {str(item["status"]) for item in results}
    if statuses == {"needs_login"}:
        return "needs_login"
    if statuses == {"failed"}:
        return "failed"
    if statuses <= {"success"}:
        return "success"
    return "partial"


def _render_markdown(report: dict[str, Any]) -> str:
    thresholds = report["thresholds"]
    lines = [
        f"# {report['target_date']} 候选科技账号体检",
        "",
        "> 抖音公开数据仅用于活跃度、内容形态和热度观察，不是新闻事实证据。候选建议不会自动晋级可信账号。",
        "",
        f"- 运行状态：{report['status']}",
        f"- 北京时间窗口：{report['window']['start']} 至 {report['window']['end']}",
        f"- 账号数：{report['counts']['accounts']}；成功：{report['counts']['success']}；数据不足：{report['counts']['insufficient_data']}；身份不匹配：{report['counts']['identity_mismatch']}",
        f"- 预算：每账号最多 {report['budgets']['max_posts_per_account']} 条，并发 {report['budgets']['concurrency']}；媒体/ASR/OCR/LLM 均为 0",
        "",
        "## 建议阈值",
        "",
        f"- 核心候选：活跃至少 {thresholds['core_min_active_days']} 天、科技新闻相关度至少 {thresholds['min_technology_news_ratio']:.0%}、短视频比例至少 {thresholds['min_short_video_ratio']:.0%}、推广比例不高于 {thresholds['max_promotion_ratio']:.0%}",
        f"- 补充候选：活跃至少 {thresholds['supplemental_min_active_days']} 天且科技新闻相关度至少 {thresholds['min_technology_news_ratio']:.0%}",
        "",
    ]
    for index, item in enumerate(report["accounts"], 1):
        metrics = item["metrics"]
        lines.extend([
            f"## {index}. {item['display_name']}",
            "",
            f"- 生命周期：{item['lifecycle_status']}（算法不会自动修改）",
            f"- 运行状态：{item['status']}；身份：{item['identity_status']}；数据质量：{item['data_quality']}",
            f"- 评估建议：{item['evaluation_recommendation']}",
            f"- 主页：{item['profile_url']}",
            f"- 作品数：{metrics['total_posts']}；活跃天数：{metrics['active_days']}；活跃日均作品：{metrics['posts_per_active_day']}",
            f"- 科技新闻相关：{metrics['eligible_tech_news_count']}（比例 {metrics['technology_news_ratio']}）",
            f"- 短视频比例：{metrics['short_video_ratio']}；时长缺失率：{metrics['duration_missing_rate']}",
            f"- 疑似推广比例：{metrics['promotion_suspected_ratio']}",
            f"- 互动中位数：{json.dumps(metrics['interaction_medians'], ensure_ascii=False)}",
            f"- 互动缺失率：{json.dumps(metrics['interaction_missing_rates'], ensure_ascii=False)}",
            "- 建议理由：" + "；".join(item["recommendation_reasons"]),
            "",
        ])
    if report["errors"]:
        lines.extend(["## 运行提示", ""] + [f"- {item['account_name']}：{item['error']}" for item in report["errors"]] + [""])
    return "\n".join(lines)


def run_account_evaluation(
    config: dict[str, Any],
    *,
    account_ids: list[str] | None = None,
    target_date: str | None = None,
    live: bool = False,
    input_rows: dict[str, list[dict[str, Any]]] | None = None,
    input_files: dict[str, list[str | Path]] | None = None,
    collector: Callable[[dict[str, Any], dict[str, Any], Path, int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    settings = config["jobs"]["account_pool"]
    zone = ZoneInfo(str(config["timezone"]))
    selected_date = target_date or datetime.now(zone).date().isoformat()
    start, end = evaluation_window(selected_date, str(config["timezone"]))
    store = AccountPoolStore(config)
    all_accounts = store.list_accounts()
    if account_ids:
        accounts = [store.get(account_id) for account_id in account_ids]
    else:
        accounts = [item for item in all_accounts if item.get("enabled") and item.get("lifecycle_status") == "candidate"]
    maximum = min(30, max(1, int(settings.get("max_posts_per_account") or 30)))
    output_root = _project_path(config, settings["output_root"]) / selected_date
    output_json = output_root / "account-evaluation.json"
    output_markdown = output_root / "account-evaluation.md"
    run_temp = _project_path(config, settings["temp_root"]) / f"{selected_date}-{uuid.uuid4().hex[:8]}"
    state = JobState(config, "account_pool_evaluation")
    browser_session: BrowserSession | None = None
    browser_evidence = {"used": False, "single_project_browser": True, "initial_page_count": None}
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    input_rows = input_rows or {}
    input_files = input_files or {}
    with JobLock(config, "account_pool_evaluation"):
        state.update(status="running", phase="prepare", counts={"accounts": len(accounts)}, output_path=str(output_markdown.resolve()))
        try:
            browser_prepare_error: str | None = None
            if live and collector is None:
                browser_session = BrowserSession(config, "account_pool_evaluation")
                try:
                    prepared = browser_session.prepare()
                    browser_evidence = {
                        "used": True,
                        "single_project_browser": True,
                        "initial_page_count": prepared.get("page_count"),
                    }
                except (OSError, RuntimeError, TimeoutError) as exc:
                    browser_prepare_error = f"项目浏览器准备失败：{type(exc).__name__}"
            actual_collector = collector or collect_candidate_metadata
            auth_blocked = False
            deadline = monotonic_time.monotonic() + int(settings.get("total_timeout_seconds") or 300)
            for index, account in enumerate(accounts):
                state.update(status="running", phase="collect_metadata" if live else "evaluate_offline", counts={"accounts": len(accounts), "processed": index})
                if browser_prepare_error:
                    result = _placeholder(account, "failed", browser_prepare_error)
                    results.append(result)
                    errors.append({"account_name": account["display_name"], "error": result["error"]})
                    continue
                if auth_blocked:
                    result = _placeholder(account, "needs_login", "前一账号触发登录/验证，已停止剩余外部采集")
                    results.append(result)
                    errors.append({"account_name": account["display_name"], "error": result["error"]})
                    continue
                if live:
                    remaining = int(deadline - monotonic_time.monotonic())
                    if remaining <= 0:
                        result = _placeholder(account, "failed", "账号体检总时间预算已耗尽")
                        results.append(result)
                        errors.append({"account_name": account["display_name"], "error": result["error"]})
                        continue
                    runtime_config = copy.deepcopy(config)
                    runtime_config["jobs"]["account_pool"]["collection_timeout_seconds"] = min(
                        int(settings.get("collection_timeout_seconds") or 120), max(1, remaining)
                    )
                    try:
                        collection = actual_collector(runtime_config, account, run_temp, maximum)
                    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
                        collection = {"status": "failed", "rows": [], "error": f"账号元数据采集失败：{type(exc).__name__}"}
                    rows = list(collection.get("rows") or [])
                    collection_status = str(collection.get("status") or "failed")
                    if collection_status == "needs_login":
                        auth_blocked = True
                        result = _placeholder(account, "needs_login", str(collection.get("error") or "请完成人工登录/验证"))
                        results.append(result)
                        errors.append({"account_name": account["display_name"], "error": result["error"]})
                        continue
                    if collection_status == "failed":
                        result = _placeholder(account, "failed", str(collection.get("error") or "账号元数据采集失败"))
                        results.append(result)
                        errors.append({"account_name": account["display_name"], "error": result["error"]})
                        continue
                elif account["account_id"] in input_rows:
                    rows = list(input_rows[account["account_id"]])
                else:
                    rows = []
                    for path in input_files.get(account["account_id"], []):
                        rows.extend(load_raw_records(path))
                result = evaluate_account(account, rows, config, target_date=selected_date)
                results.append(result)
                if result["identity_status"] == "identity_mismatch":
                    errors.append({"account_name": account["display_name"], "error": "实际账号身份与配置不一致"})
            status = _overall_status(results)
            counts = {
                "accounts": len(results),
                "success": sum(item["status"] == "success" for item in results),
                "partial": sum(item["status"] == "partial" for item in results),
                "empty": sum(item["status"] == "empty" for item in results),
                "needs_login": sum(item["status"] == "needs_login" for item in results),
                "failed": sum(item["status"] == "failed" for item in results),
                "identity_mismatch": sum(item["identity_status"] == "identity_mismatch" for item in results),
                "insufficient_data": sum(item["evaluation_recommendation"] == "insufficient_data" for item in results),
                "total_posts": sum(int(item["metrics"]["total_posts"]) for item in results),
            }
            report = {
                "schema_version": "account-pool-evaluation-v1",
                "generated_at": now_iso(str(config["timezone"])),
                "target_date": selected_date,
                "status": status,
                "window": {"timezone": str(config["timezone"]), "start": start.isoformat(), "end": end.isoformat(), "natural_days": 14},
                "budgets": {
                    "max_posts_per_account": maximum,
                    "concurrency": 1,
                    "media_downloads": 0,
                    "asr_attempts": 0,
                    "ocr_attempts": 0,
                    "llm_requests": 0,
                    "visual_samples": 0,
                },
                "thresholds": dict(settings["thresholds"]),
                "counts": counts,
                "browser": browser_evidence,
                "accounts": results,
                "errors": errors,
                "disclaimer": "抖音公开数据仅用于活跃度、内容形态和热度观察，不是新闻事实证据。候选建议不会自动晋级可信账号。",
                "artifacts": {"json": str(output_json.resolve()), "markdown": str(output_markdown.resolve())},
            }
            atomic_write_json(output_json, report)
            atomic_text(output_markdown, _render_markdown(report))
            state.update(status=status, phase="complete", counts=counts, output_path=str(output_markdown.resolve()), errors=errors[:10])
            return report
        finally:
            if browser_session is not None:
                final_status = _overall_status(results)
                human_required = any(item.get("status") == "needs_login" for item in results)
                browser_session.finish(final_status, human_required=human_required)
            shutil.rmtree(run_temp, ignore_errors=True)
