from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx

from .collector import BrowserSession
from .config import project_root, resolve_path
from .exporter import atomic_write_json
from .job_runtime import JobLock, JobState, now_iso
from .llm_analysis import OpenAICompatibleAnalyzer
from .materials import download_video
from .normalize import load_raw_records, normalize_record
from .reporting import atomic_text
from .resource_control import directory_bytes, remove_tree
from .scheduler import query as scheduler_query
from .visual_ocr import VisualBatchBudget, process_visual_video


FORMULA_VERSION = "trusted-account-heat-v1"
METRICS = {
    "like": "digg_count",
    "comment": "comment_count",
    "collect": "collect_count",
    "share": "share_count",
}
SENSITIVE = re.compile(
    r"(?i)(?:sk-[a-z0-9_-]{16,}|bearer\s+[a-z0-9._-]{12,}|cookie[\"'\s:=]+|"
    r"authorization[\"'\s:=]+|signature=|x-bogus|mstoken|play_addr|download_addr|"
    r"video_download_url|music_download_url|note_download_url|browser[_ -]?profile|user_data_dir)"
)


def _root(config: dict[str, Any]) -> Path:
    return Path(config.get("_project_root") or project_root()).resolve()


def _path(config: dict[str, Any], value: str | Path) -> Path:
    candidate = Path(value)
    return candidate.resolve() if candidate.is_absolute() else (_root(config) / candidate).resolve()


def get_trusted_account(config: dict[str, Any], account_id: str | None = None) -> dict[str, Any]:
    accounts = [item for item in config.get("trusted_news_accounts") or [] if item.get("enabled", True)]
    selected = next((item for item in accounts if account_id is None or str(item.get("id")) == account_id), None)
    if selected is None:
        raise ValueError("没有启用的可信新闻账号")
    return dict(selected)


def resolve_account_entry(account: dict[str, Any], *, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    entry = str(account["share_entry_url"])
    parsed = urlparse(entry)
    if parsed.scheme != "https" or parsed.hostname != "v.douyin.com":
        raise ValueError("可信账号入口必须是 HTTPS 抖音短链接")
    with httpx.Client(
        timeout=httpx.Timeout(10.0), follow_redirects=True, trust_env=False,
        verify=True, transport=transport, headers={"User-Agent": "Mozilla/5.0"},
    ) as client:
        response = client.get(entry)
        response.raise_for_status()
    final = urlparse(str(response.url))
    if final.scheme != "https" or final.hostname not in {"www.douyin.com", "douyin.com", "www.iesdouyin.com"}:
        raise ValueError("短链接没有解析到受信任的抖音域名")
    query = parse_qs(final.query)
    path_match = re.search(r"/share/user/([A-Za-z0-9_-]+)", final.path)
    stable_id = (query.get("sec_uid") or [path_match.group(1) if path_match else ""])[0]
    if stable_id != str(account["stable_id"]):
        raise ValueError("短链接解析到的账号与配置稳定 ID 不一致")
    return {
        "account_id": account["id"], "account_name": account["name"], "name": account["name"], "stable_id": stable_id,
        "profile_url": f"https://www.douyin.com/user/{stable_id}",
        "follower_count_observed": account.get("follower_count_observed"),
        "resolved_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
    }


def trusted_creator_command(config: dict[str, Any], account: dict[str, Any], destination: Path, maximum: int) -> list[str]:
    crawler = config["media_crawler"]
    return [
        str(_path(config, crawler["python"])), str(Path(__file__).with_name("mediacrawler_runner.py").resolve()),
        "--crawler-root", str(_path(config, crawler["root"])), "--cdp-port", str(int(crawler["cdp_port"])),
        "--navigation-timeout", str(int(crawler.get("navigation_timeout_seconds") or 90)), "--publish-time-type", "0", "--",
        "--platform", "dy", "--type", "creator", "--lt", "qrcode", "--save_data_option", "jsonl",
        "--save_data_path", str(destination), "--crawler_max_notes_count", str(maximum),
        "--get_comment", "false", "--get_sub_comment", "false", "--max_concurrency_num", "1",
        "--headless", "false", "--creator_id", str(account["profile_url"]),
    ]


def collect_trusted_account(config: dict[str, Any], account: dict[str, Any], run_temp: Path, maximum: int) -> dict[str, Any]:
    browser_session = BrowserSession(config, "trusted_account_news")
    browser = browser_session.prepare()
    destination = run_temp / "raw"
    destination.mkdir(parents=True, exist_ok=True)
    command = trusted_creator_command(config, account, destination, maximum)
    timeout = min(
        int(config["jobs"]["trusted_account_news"].get("collection_timeout_seconds") or 120),
        int(config["jobs"]["trusted_account_news"].get("total_timeout_seconds") or 900),
    )
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    status = "failed"
    try:
        try:
            completed = subprocess.run(
                command, cwd=_path(config, config["media_crawler"]["root"]), capture_output=True,
                text=True, encoding="utf-8", errors="replace", check=False, timeout=timeout,
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
        auth_markers = ("qrcode", "scan", "login", "captcha", "verify", "扫码", "登录", "验证")
        needs_login = not files and (timed_out or any(marker in diagnostic for marker in auth_markers))
        status = "success" if files and returncode == 0 else "partial" if files else "needs_login" if needs_login else "failed"
        audit_match = re.search(r"copy_skill_cdp_audit\s+pages_before=(\d+)\s+pages_after=(\d+)\s+reused_existing_page=(\d+)", diagnostic)
        runner_audit = {
            "pages_before": int(audit_match.group(1)) if audit_match else None,
            "pages_after": int(audit_match.group(2)) if audit_match else None,
            "reused_existing_page": bool(int(audit_match.group(3))) if audit_match else None,
            "ownership": f"project_cdp_port_{int(config['media_crawler']['cdp_port'])}",
            "os_window_count": "not_observed",
        }
        return {
            "status": status, "files": [str(path.resolve()) for path in files], "returncode": returncode,
            "timeout_seconds": timeout,
            "browser": {
                "status": browser.get("status"), "port": browser.get("port"),
                "initial_page_count": browser.get("page_count"), "runner": runner_audit,
            },
            "error": "请在项目专用浏览器完成抖音登录/验证后重试" if needs_login else ("采集进程未返回作品" if not files else None),
        }
    finally:
        cleanup = browser_session.finish(status, human_required=status == "needs_login")
        browser["completion_state"] = cleanup["state"]


def _identity_value(raw: dict[str, Any]) -> str:
    author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    return str(raw.get("sec_uid") or raw.get("sec_user_id") or author.get("sec_uid") or "").strip()


def select_window_records(
    rows: list[dict[str, Any]], config: dict[str, Any], account: dict[str, Any],
    window_end: datetime, maximum: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    zone = ZoneInfo(str(config["timezone"]))
    end = window_end.astimezone(zone)
    start = end - timedelta(hours=int(account.get("window_hours") or 48))
    expected_hash = str(account.get("expected_creator_hash") or "")
    observed_hashes = sorted({str(row.get("creator_hash") or "") for row in rows if row.get("creator_hash")})
    identity_error = bool(len(observed_hashes) > 1 and not expected_hash)
    accepted: dict[str, dict[str, Any]] = {}
    dropped_wrong_account = 0
    dropped_outside_window = 0
    for raw in rows:
        explicit_identity = _identity_value(raw)
        creator_hash = str(raw.get("creator_hash") or "")
        if identity_error or explicit_identity and explicit_identity != str(account["stable_id"]) or expected_hash and creator_hash != expected_hash:
            dropped_wrong_account += 1
            continue
        record = normalize_record(raw, source="douyin_creator", timezone_name=str(config["timezone"]), categories=config["categories"])
        published = datetime.fromisoformat(record.published_at) if record.published_at else None
        if published is None or published < start or published > end:
            dropped_outside_window += 1
            continue
        record.account_id = str(account["id"])
        record.account_name = str(account["name"])
        previous = accepted.get(record.video_id)
        if previous is None:
            accepted[record.video_id] = {"record": record, "raw": raw}
    ordered = sorted(
        accepted.values(),
        key=lambda item: (-datetime.fromisoformat(item["record"].published_at).timestamp(), item["record"].video_id),
    )[:maximum]
    return ordered, {
        "raw_count": len(rows), "window_count_before_limit": len(accepted), "processed_limit": maximum,
        "dropped_wrong_account": dropped_wrong_account, "dropped_outside_window": dropped_outside_window,
        "observed_creator_hash": observed_hashes[0] if len(observed_hashes) == 1 else None,
        "identity_status": "mismatch" if identity_error else "locked_to_configured_account",
        "window_start": start.isoformat(timespec="seconds"), "window_end": end.isoformat(timespec="seconds"),
    }


def rank_account_items(items: list[dict[str, Any]], window_end: datetime, weights: dict[str, float]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    maximum_logs: dict[str, float] = {}
    for metric, field in METRICS.items():
        known = [getattr(item["record"], field) for item in items if getattr(item["record"], field) is not None]
        maximum_logs[metric] = max((math.log1p(value) for value in known), default=0.0)
    ranked: list[dict[str, Any]] = []
    for item in items:
        record = item["record"]
        components: dict[str, float | None] = {}
        available = 0
        for metric, field in METRICS.items():
            value = getattr(record, field)
            if value is None:
                components[metric] = None
                continue
            available += 1
            denominator = maximum_logs[metric]
            components[metric] = round(float(weights[metric]) * (math.log1p(value) / denominator if denominator else 0.0), 3)
        age_hours = max(0.0, (window_end - datetime.fromisoformat(record.published_at)).total_seconds() / 3600)
        components["freshness"] = round(float(weights["freshness"]) * max(0.0, 1.0 - age_hours / 48.0), 3)
        score = round(sum(value for value in components.values() if value is not None), 3)
        ranked.append(item | {
            "heat_score": score, "heat_components": components, "age_hours": round(age_hours, 3),
            "data_completeness": {"available_interactions": available, "total_interactions": 4, "ratio": round(available / 4, 2)},
        })
    ranked.sort(key=lambda item: (-item["heat_score"], -datetime.fromisoformat(item["record"].published_at).timestamp(), item["record"].video_id))
    for index, item in enumerate(ranked, 1):
        item["heat_rank"] = index
    formula = {
        "version": FORMULA_VERSION, "weights": weights,
        "interaction_transform": "weight * log1p(value) / max_window_log1p(value); missing remains null",
        "freshness": "weight * max(0, 1 - age_hours / 48)",
        "tie_break": "published_at descending, stable video_id ascending", "window_max_logs": maximum_logs,
    }
    return ranked, formula


def _caption_text(raw: dict[str, Any]) -> str:
    candidates: list[str] = []
    for key in ("platform_caption", "caption", "subtitle", "subtitle_text", "video_caption"):
        value = raw.get(key)
        if isinstance(value, str):
            candidates.append(value)
        elif isinstance(value, list):
            for row in value:
                if isinstance(row, str):
                    candidates.append(row)
                elif isinstance(row, dict) and isinstance(row.get("text"), str):
                    candidates.append(row["text"])
    return "\n".join(value.strip() for value in candidates if value.strip())[:20_000]


def _run_asr(video: Path, config: dict[str, Any], run_temp: Path, config_path: str) -> dict[str, Any]:
    output = run_temp / "asr-result.json"
    command = [
        sys.executable, "-m", "douyin_intelligence.trusted_news_asr", "--config", str(_path(config, config_path)),
        "--video", str(video.resolve()), "--output", str(output.resolve()), "--temp-root", str((run_temp / "asr").resolve()),
    ]
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    timeout = int(config["jobs"]["trusted_account_news"].get("asr_timeout_seconds") or 300)
    try:
        completed = subprocess.run(command, cwd=_root(config), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=flags)
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"本地转写超过 {timeout} 秒预算", "segments": [], "text": ""}
    if completed.returncode or not output.is_file():
        return {"status": "error", "error": "本地转写子进程失败", "segments": [], "text": ""}
    try:
        return json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "error", "error": "本地转写结果无效", "segments": [], "text": ""}


def obtain_transcript(
    item: dict[str, Any], config: dict[str, Any], run_temp: Path, config_path: str,
    *, allow_media: bool = True, asr_runner: Callable[..., dict[str, Any]] = _run_asr,
    visual_runner: Callable[..., dict[str, Any]] = process_visual_video,
    visual_budget: VisualBatchBudget | None = None,
    qa_export_dir: Path | None = None,
) -> dict[str, Any]:
    raw, record = item["raw"], item["record"]
    account = get_trusted_account(config, str(record.account_id or ""))
    visual_profile = account.get("content_extraction_profile") == "visual_text_only"
    audio_base = {
        "audio_attempted": False,
        "audio_skip_reason": "account_visual_text_profile" if visual_profile else None,
    }
    caption = _caption_text(raw)
    if caption:
        return {
            **audio_base, "content_source": "platform_caption", "visual_text_status": "unavailable",
            "transcript_source": "platform_caption", "transcript_status": "success", "complete": True,
            "text": caption, "segments": [], "visual_evidence": None,
            "fact_boundary": f"高可信对标账号来源／依据{account['name']}平台字幕整理",
        }
    description = str(record.title or "").strip()[:20_000]
    if visual_profile:
        empty_metrics = {
            "candidate_frames": 0, "selected_frames": 0, "ocr_frames": 0, "retry_frames": 0,
            "unique_text_cards": 0, "unique_content_chars": 0, "median_ocr_confidence": 0.0,
            "visual_text_coverage": 0.0, "budget_exhausted": False, "frames": [], "text_cards": [], "merged_text": "",
        }

        def visual_fallback(visual_status: str, error: str | None, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
            source = "description_only" if description else "unavailable"
            return {
                **audio_base, "content_source": source, "visual_text_status": visual_status,
                "transcript_source": source, "transcript_status": "partial" if description else "unavailable",
                "complete": False, "text": description, "segments": [], "error": error,
                "visual_evidence": evidence or (empty_metrics | {"visual_text_status": visual_status, "error": error}),
                "fact_boundary": f"高可信对标账号来源／仅依据{account['name']}发布文案降级整理",
            }

        if not allow_media:
            return visual_fallback("media_unavailable", "视觉媒体处理已禁用")
        media_url = str(raw.get("video_download_url") or "")
        if not media_url.startswith("https://"):
            return visual_fallback("media_unavailable", "没有可用的临时视频地址")
        item_temp = run_temp / f"media-{record.video_id}"
        video = item_temp / "source.mp4"
        try:
            download_video(media_url, video, config)
            settings = config["jobs"]["trusted_account_news"]["visual_ocr"]
            if visual_budget is None:
                visual_budget = VisualBatchBudget(
                    soft_limit=int(settings["batch_soft_frame_limit"]),
                    hard_limit=int(settings["batch_hard_frame_limit"]),
                    deadline=time.monotonic() + float(settings["batch_timeout_seconds"]),
                )
            evidence = visual_runner(
                video, item_temp / "visual", settings, visual_budget,
                qa_export_dir=(qa_export_dir / record.video_id) if qa_export_dir else None,
            )
            quality = str(evidence.get("visual_text_status") or "unavailable")
            quality_tier = str(evidence.get("quality_tier") or quality)
            ocr_text = str(evidence.get("merged_text") or "").strip()[:100_000]
            if quality_tier == "success" and ocr_text:
                return {
                    **audio_base, "content_source": "screen_ocr", "visual_text_status": quality,
                    "transcript_source": "screen_ocr", "transcript_status": "success", "complete": True,
                    "text": ocr_text, "segments": [], "visual_evidence": evidence,
                    "fact_boundary": f"高可信对标账号来源／依据{account['name']}视频画面文字整理",
                }
            if quality_tier == "partial" and ocr_text:
                combined = ocr_text + (f"\n\n发布文案补充：{description}" if description else "")
                return {
                    **audio_base, "content_source": "screen_ocr_partial", "visual_text_status": quality,
                    "transcript_source": "screen_ocr_partial", "transcript_status": "partial", "complete": False,
                    "text": combined, "segments": [], "visual_evidence": evidence,
                    "error": evidence.get("error") or "画面文字提取不完整",
                    "fact_boundary": f"高可信对标账号来源／依据可识别的部分画面文字及{account['name']}发布文案整理",
                }
            return visual_fallback(quality, str(evidence.get("error") or "画面文字不足以形成可靠摘要"), evidence)
        except Exception as exc:
            return visual_fallback("media_unavailable", f"临时视觉媒体处理失败：{type(exc).__name__}")
        finally:
            if item_temp.exists():
                remove_tree(item_temp, run_temp)

    minimum = int(config["jobs"]["trusted_account_news"].get("min_description_chars") or 60)
    if len(description) >= minimum or not allow_media:
        return {
            "transcript_source": "description_only" if description else "unavailable",
            "transcript_status": "partial" if description else "unavailable", "complete": False,
            "text": description, "segments": [], **audio_base,
        }
    media_url = str(raw.get("video_download_url") or "")
    if not media_url.startswith("https://"):
        return {"transcript_source": "description_only" if description else "unavailable", "transcript_status": "partial" if description else "unavailable", "complete": False, "text": description, "segments": [], "error": "没有可用的平台字幕或临时媒体", **audio_base}
    item_temp = run_temp / f"media-{record.video_id}"
    video = item_temp / "source.mp4"
    try:
        download_video(media_url, video, config)
        result = asr_runner(video, config, item_temp, config_path)
        text = str(result.get("text") or "").strip()[:100_000]
        minimum_asr = int(config["jobs"]["trusted_account_news"].get("min_asr_chars") or 40)
        if result.get("status") == "success" and len(text) >= minimum_asr:
            return {"transcript_source": "local_asr", "transcript_status": "success", "complete": True, "text": text, "segments": result.get("segments") or [], "audio_attempted": True, "audio_skip_reason": None}
        error = result.get("error") or ("本地转写文本过短" if text else "本地转写没有获得文本")
        return {"transcript_source": "description_only" if description else "unavailable", "transcript_status": "partial" if description else "unavailable", "complete": False, "text": description, "segments": [], "error": str(error)[:200], "audio_attempted": True, "audio_skip_reason": None}
    except Exception as exc:
        return {"transcript_source": "description_only" if description else "unavailable", "transcript_status": "partial" if description else "unavailable", "complete": False, "text": description, "segments": [], "error": f"临时媒体处理失败：{type(exc).__name__}", "audio_attempted": True, "audio_skip_reason": None}
    finally:
        if item_temp.exists():
            remove_tree(item_temp, run_temp)


def deterministic_enrichment(item: dict[str, Any], transcript: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
    text = re.sub(r"\s+", " ", str(transcript.get("text") or item["record"].title)).strip()
    sentences = [value.strip() for value in re.split(r"(?<=[。！？!?])", text) if value.strip()]
    headline = re.split(r"[#\n]", item["record"].title)[0].strip()[:80] or "科技快讯"
    summary = headline[:180]
    return {
        "headline": headline, "one_sentence_summary": summary,
        "key_points": (sentences[:4] or [headline]),
        "why_it_matters": f"该作品在{account['name']}近 48 小时窗口内按公开互动与新鲜度排名第 {item['heat_rank']}。",
        "safe_broadcast": f"据{account['name']}本条视频画面文字及发布文案介绍，{summary}",
        "claims_to_verify": ["画面文字或发布文案中涉及的日期、数字、规格、价格、公司决定和性能结论。"],
        "content_angle": "围绕博主本条快讯讲清发生了什么、受众为何关注，以及哪些外部主张仍需核实。",
        "do_not_claim": ["不得把高可信对标账号来源表述为官方公告或独立事实核验。", "不得扩展画面文字或发布文案未支持的结论。"],
    }


def _safe_metadata(item: dict[str, Any]) -> dict[str, Any]:
    record = item["record"]
    return {
        "video_id": record.video_id, "title": record.title, "author": record.account_name,
        "published_at": record.published_at, "share_url": f"https://www.douyin.com/video/{record.video_id}" if record.video_id.isdigit() else record.share_url,
        "interactions": {
            "like": record.digg_count, "comment": record.comment_count,
            "collect": record.collect_count, "share": record.share_count,
        },
    }


def _render_markdown(report: dict[str, Any]) -> str:
    account = report["account"]
    lines = [
        f"# {account['name']}近 48 小时可信账号科技快讯榜", "",
        f"> 高可信对标账号来源；依据{account['name']}视频画面文字/发布文案整理，不等同于官方公告或独立事实核验。", "",
        f"- 窗口：{report['window']['start']} 至 {report['window']['end']}（Asia/Shanghai）",
        f"- 原始/窗口内/处理：{report['counts']['raw']} / {report['counts']['in_window']} / {report['counts']['processed']}",
        f"- LLM 提炼：{report['llm_enrichment']}", f"- 运行状态：{report['status']}", "",
        "## 排名公式", "", f"- 版本：`{report['formula']['version']}`", f"- 权重：`{json.dumps(report['formula']['weights'], ensure_ascii=False)}`",
        f"- 互动：{report['formula']['interaction_transform']}", f"- 新鲜度：{report['formula']['freshness']}", "",
    ]
    if not report["items"]:
        lines.extend(["本窗口没有可处理作品；未扩大时间范围。", ""])
    for item in report["items"]:
        meta, result = item["metadata"], item["enrichment"]
        lines.extend([
            f"## #{item['heat_rank']} {result['headline']}", "", f"- 热度：{item['heat_score']}",
            f"- 发布时间：{meta['published_at']}", f"- 互动：{json.dumps(meta['interactions'], ensure_ascii=False)}",
            f"- 热度分项：{json.dumps(item['heat_components'], ensure_ascii=False)}", f"- 数据完整度：{item['data_completeness']['available_interactions']}/4",
            f"- 内容来源：{item['content']['content_source']} / 画面文字={item['content']['visual_text_status']} / 完整={item['content']['complete']}",
            f"- 画面帧：候选 {item['content']['candidate_frames']} / OCR {item['content']['ocr_frames']} / 重试 {item['content']['retry_frames']}",
            f"- OCR质量：字符 {item['content']['unique_content_chars']} / 中位置信度 {item['content']['median_ocr_confidence']} / 覆盖率 {item['content']['visual_text_coverage']}",
            f"- 音频尝试：{item['content']['audio_attempted']}",
            f"- 提炼方式：{item['enrichment_status']}", f"- 一句话：{result['one_sentence_summary']}",
            f"- 为什么重要：{result['why_it_matters']}", f"- 安全口播：{result['safe_broadcast']}",
            f"- 选题角度：{result['content_angle']}", f"- 待核实：{result['claims_to_verify']}", f"- 禁止宣称：{result['do_not_claim']}",
            f"- 可信标签：{item['fact_boundary']}", f"- 抖音链接：[{meta['share_url']}]({meta['share_url']})", "",
        ])
    if report.get("warnings"):
        lines.extend(["## 降级与警告", "", *[f"- {value}" for value in report["warnings"]], ""])
    return "\n".join(lines)


def _scan_values(paths: list[Path]) -> dict[str, Any]:
    findings: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        if SENSITIVE.search(path.read_text(encoding="utf-8", errors="replace")):
            findings.append(path.name)
    return {"status": "passed" if not findings else "failed", "files_scanned": len(paths), "finding_count": len(findings)}


def run_trusted_account_news(
    config: dict[str, Any], *, account_id: str | None = None, input_files: list[str] | None = None,
    live: bool = False, window_end: datetime | None = None, maximum: int | None = None,
    allow_media: bool = True, config_path: str = "config/content_intelligence.json",
    qa_export_dir: Path | None = None,
    auto_ai_brief: bool | None = None,
) -> dict[str, Any]:
    job = "trusted_account_news"
    settings = config["jobs"][job]
    account = get_trusted_account(config, account_id)
    limit = min(max(1, int(maximum or account.get("max_items") or settings["max_items"])), int(settings["hard_max_items"]), 10)
    zone = ZoneInfo(str(config["timezone"]))
    end = (window_end or datetime.now(zone)).astimezone(zone).replace(microsecond=0)
    run_date = end.date().isoformat()
    output_root = _path(config, settings["output_root"]) / str(account["id"]) / run_date
    ocr_root = output_root / "ocr"
    run_temp = _path(config, settings["temp_root"]) / f"{account['id']}-{end.strftime('%Y%m%dT%H%M%S%z')}"
    state = JobState(config, job)
    collection: dict[str, Any] | None = None
    warnings: list[str] = []
    errors: list[dict[str, str]] = []
    with JobLock(config, job):
        state.update(
            status="running", phase="resolve_account", started_at=now_iso(str(config["timezone"])),
            completed_at=None, current_index=None, output_path=None, counts={}, errors=[], warnings=[],
        )
        identity = {
            "account_id": account["id"], "account_name": account["name"], "stable_id": account["stable_id"],
            "profile_url": account["profile_url"], "follower_count_observed": account.get("follower_count_observed"),
        }
        rows: list[dict[str, Any]] = []
        try:
            if live:
                try:
                    identity = resolve_account_entry(account)
                    state.update(phase="collect_account")
                    collection = collect_trusted_account(config, account, run_temp, limit)
                    for value in collection.get("files") or []:
                        rows.extend(load_raw_records(value))
                except Exception as exc:
                    collection = {"status": "failed", "returncode": None, "error": f"账号解析或采集失败：{type(exc).__name__}"}
                    errors.append({"phase": "collection", "error": str(collection["error"])})
            elif input_files:
                state.update(phase="load_inputs")
                for value in input_files:
                    try:
                        rows.extend(load_raw_records(value))
                    except (OSError, ValueError) as exc:
                        errors.append({"phase": "input", "error": f"无法读取输入：{type(exc).__name__}"})
            selected, selection = select_window_records(rows, config, account, end, limit)
            if selection["identity_status"] == "mismatch":
                errors.append({"phase": "identity", "error": "采集结果包含多个作者身份，已拒绝处理"})
                selected = []
            state.update(phase="visual_ocr", counts={"raw": len(rows), "in_window": selection["window_count_before_limit"], "processed": len(selected), "audio_attempts": 0})
            ranked, formula = rank_account_items(selected, end, {key: float(value) for key, value in settings["weights"].items()})
            analyzer = OpenAICompatibleAnalyzer(config)
            llm_status = analyzer.status()
            if not llm_status["enabled"]:
                warnings.append("HTTPS 模型端点未配置，已使用确定性提炼；未调用明文 HTTP 模型。")
            outputs: list[dict[str, Any]] = []
            transcript_counts = {"success": 0, "partial": 0, "unavailable": 0}
            transcript_sources = {"platform_caption": 0, "screen_ocr": 0, "screen_ocr_partial": 0, "description_only": 0, "local_asr": 0, "unavailable": 0}
            visual_counts = {"success": 0, "partial": 0, "unavailable": 0, "media_unavailable": 0, "ocr_error": 0, "budget_exhausted": 0}
            visual_frames = {"candidate": 0, "selected": 0, "ocr": 0, "retry": 0}
            audio_attempts = 0
            llm_counts = {"success": 0, "disabled": 0, "failed": 0, "batch_deferred": 0}
            ocr_root.mkdir(parents=True, exist_ok=True)
            visual_settings = settings["visual_ocr"]
            visual_budget = VisualBatchBudget(
                soft_limit=int(visual_settings["batch_soft_frame_limit"]),
                hard_limit=int(visual_settings["batch_hard_frame_limit"]),
                deadline=time.monotonic() + float(visual_settings["batch_timeout_seconds"]),
            )
            for item in ranked:
                state.update(phase="visual_ocr", current_index=item["heat_rank"], counts={"raw": len(rows), "in_window": selection["window_count_before_limit"], "processed": len(selected), "audio_attempts": audio_attempts, "visual": visual_counts, "frames": visual_frames})
                transcript = obtain_transcript(
                    item, config, run_temp, config_path, allow_media=allow_media,
                    visual_budget=visual_budget, qa_export_dir=qa_export_dir,
                )
                transcript_counts[transcript["transcript_status"]] = transcript_counts.get(transcript["transcript_status"], 0) + 1
                transcript_sources[transcript["transcript_source"]] = transcript_sources.get(transcript["transcript_source"], 0) + 1
                audio_attempts += int(bool(transcript.get("audio_attempted")))
                evidence = transcript.get("visual_evidence") or {}
                if transcript.get("content_source") != "platform_caption":
                    visual_status = str(transcript.get("visual_text_status") or "unavailable")
                    visual_counts[visual_status] = visual_counts.get(visual_status, 0) + 1
                visual_frames["candidate"] += int(evidence.get("candidate_frames") or 0)
                visual_frames["selected"] += int(evidence.get("selected_frames") or 0)
                visual_frames["ocr"] += int(evidence.get("ocr_frames") or 0)
                visual_frames["retry"] += int(evidence.get("retry_frames") or 0)
                transcript_payload = {
                    "version": "2.0", "video_id": item["record"].video_id, "title": item["record"].title,
                    "content_source": transcript.get("content_source") or transcript["transcript_source"],
                    "visual_text_status": transcript.get("visual_text_status") or "unavailable",
                    "source": transcript["transcript_source"], "status": transcript["transcript_status"],
                    "complete": transcript["complete"], "text": transcript["text"], "segments": transcript.get("segments") or [],
                    "audio_attempted": bool(transcript.get("audio_attempted")), "audio_skip_reason": transcript.get("audio_skip_reason"),
                    "fact_boundary": transcript.get("fact_boundary"), "visual_evidence": evidence,
                    "error": transcript.get("error"),
                }
                transcript_path = ocr_root / f"{item['record'].video_id}.json"
                atomic_write_json(transcript_path, transcript_payload)
                deterministic = deterministic_enrichment(item, transcript, account)
                enrichment = deterministic
                enrichment_status = "deterministic_fallback"
                if llm_status["enabled"]:
                    llm_counts["batch_deferred"] += 1
                else:
                    llm_counts["disabled"] += 1
                outputs.append({
                    "heat_rank": item["heat_rank"], "heat_score": item["heat_score"], "heat_components": item["heat_components"],
                    "data_completeness": item["data_completeness"], "metadata": _safe_metadata(item),
                    "transcript": {key: transcript.get(key) for key in ("transcript_source", "transcript_status", "complete", "error")},
                    "content": {
                        "content_source": transcript.get("content_source") or transcript["transcript_source"],
                        "visual_text_status": transcript.get("visual_text_status") or "unavailable",
                        "complete": transcript["complete"], "audio_attempted": bool(transcript.get("audio_attempted")),
                        "audio_skip_reason": transcript.get("audio_skip_reason"),
                        **{key: evidence.get(key, 0) for key in ("candidate_frames", "selected_frames", "ocr_frames", "retry_frames", "unique_text_cards", "unique_content_chars", "median_ocr_confidence", "visual_text_coverage", "budget_exhausted")},
                    },
                    "transcript_path": str(transcript_path.resolve()), "enrichment_status": enrichment_status, "enrichment": enrichment,
                    "source_tier": "trusted_creator", "editorial_confidence": account["editorial_confidence"],
                    "evidence_status": "trusted_creator_report", "fact_boundary": transcript.get("fact_boundary"),
                })
            if collection and collection["status"] == "needs_login":
                status = "needs_login"
                warnings.append(str(collection["error"]))
            elif not outputs:
                status = "failed" if errors or collection and collection["status"] == "failed" else "empty"
            elif errors or transcript_counts["partial"] or transcript_counts["unavailable"] or not llm_status["enabled"]:
                status = "partial"
            else:
                status = "success"
            scheduler = scheduler_query(config)
            report = {
                "version": "2.0", "schema": "trusted-account-news-ranking-v2", "job": job, "status": status,
                "generated_at": now_iso(str(config["timezone"])),
                "account": {**identity, "name": account["name"], "category": account["category"], "source_tier": "trusted_creator", "editorial_confidence": account["editorial_confidence"], "approval_basis": account["approval_basis"], "notes": account.get("notes")},
                "window": {"timezone": str(config["timezone"]), "hours": 48, "start": selection["window_start"], "end": selection["window_end"]},
                "counts": {"raw": len(rows), "in_window": selection["window_count_before_limit"], "processed": len(outputs), "transcripts": transcript_counts, "transcript_sources": transcript_sources, "visual_text": visual_counts, "frames": visual_frames, "audio_attempts": audio_attempts, "llm": llm_counts},
                "formula": formula, "items": outputs, "collection": {key: value for key, value in (collection or {}).items() if key not in {"files"}},
                "identity_validation": selection, "llm": llm_status,
                "llm_enrichment": "batch_ai_brief_pending" if llm_status["enabled"] else "disabled_missing_https_endpoint",
                "warnings": warnings, "errors": errors,
                "scheduler": {"task_name": scheduler["task_name"], "installed": bool(scheduler["ok"])},
                "visual_ocr_policy": {key: visual_settings[key] for key in ("frame_strategy", "scene_threshold", "interval_seconds", "first_pass_width", "retry_width", "retry_max_frames", "per_video_timeout_seconds", "batch_timeout_seconds", "batch_soft_frame_limit", "batch_hard_frame_limit", "frame_budgets")},
                "artifacts": {"markdown": str((output_root / "ranking.md").resolve()), "json": str((output_root / "ranking.json").resolve()), "ocr": str(ocr_root.resolve()), "transcripts": str(ocr_root.resolve())},
                "temporary_media": {"retained": False, "remaining_bytes": 0},
                "security_scan": {"status": "pending", "files_scanned": 0, "finding_count": 0},
            }
            state.update(phase="output")
            atomic_write_json(output_root / "ranking.json", report)
            atomic_text(output_root / "ranking.md", _render_markdown(report))
            scan_paths = [output_root / "ranking.json", output_root / "ranking.md", *sorted(ocr_root.glob("*.json"))]
            report["security_scan"] = _scan_values(scan_paths)
            atomic_write_json(output_root / "ranking.json", report)
            atomic_text(output_root / "ranking.md", _render_markdown(report))
            run_ai = bool((settings.get("ai_brief") or {}).get("auto_after_collection", True)) if auto_ai_brief is None else bool(auto_ai_brief)
            brief_summary: dict[str, Any] | None = None
            output_path = report["artifacts"]["markdown"]
            state_status = status
            if run_ai and outputs and status not in {"needs_login", "failed"}:
                state.update(phase="ai_editorial_brief", current_index=None)
                try:
                    from .trusted_ai_brief import run_ai_brief

                    brief = run_ai_brief(config, output_root / "ranking.json")
                    brief_summary = {
                        "status": brief["status"], "request_count": brief["analysis_metadata"]["request_count"],
                        "cache_hit": brief["analysis_metadata"]["cache_hit"], "artifacts": brief["artifacts"],
                    }
                    if brief["status"] == "success":
                        output_path = brief["artifacts"]["markdown"]
                    elif state_status == "success":
                        state_status = "partial"
                except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
                    brief_summary = {"status": "degraded", "request_count": 0, "cache_hit": False, "reason": f"{type(exc).__name__}"}
                    if state_status == "success":
                        state_status = "partial"
            report["runtime_ai_brief"] = brief_summary
            state.update(status=state_status, phase="complete", completed_at=now_iso(str(config["timezone"])), output_path=output_path, counts=report["counts"], warnings=warnings, errors=errors, ai_brief=brief_summary)
            return report
        finally:
            if run_temp.exists():
                remove_tree(run_temp, _path(config, settings["temp_root"]))
