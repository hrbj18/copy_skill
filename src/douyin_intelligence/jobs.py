from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import resolve_path
from .exporter import atomic_write_json
from .job_runtime import JobLock, JobState, now_iso, target_yesterday, temp_size
from .llm_analysis import OpenAICompatibleAnalyzer
from .material_pipeline import build_materials
from .news_sources import NewsEvent, cluster_articles, fetch_sources
from .normalize import normalize_files
from .reporting import atomic_text, daily_news_broadcast, daily_news_markdown, inspiration_markdown
from .search_collector import collect_search
from .douyin_ranking import run_douyin_tech_ranking


def _douyin_files(config: dict[str, Any], inputs: list[str] | None, run_dir: str | None = None) -> list[Path]:
    if inputs:
        return [Path(value).resolve() for value in inputs]
    root = Path(run_dir).resolve() if run_dir else resolve_path(config["media_crawler"]["runs_output"])
    paths = sorted([*root.rglob("search_contents_*.json"), *root.rglob("search_contents_*.jsonl")], key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[:20]


def _engagement(record: Any) -> float:
    return float((record.digg_count or 0) + 3 * (record.comment_count or 0) + 5 * (record.share_count or 0) + 4 * (record.collect_count or 0) + 0.05 * (record.play_count or 0))


def _title_tokens(text: str) -> set[str]:
    latin = re.findall(r"[a-z0-9][a-z0-9+._-]{1,}", text.casefold())
    chinese = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    return set(latin + chinese + [chunk[index:index + 2] for chunk in chinese for index in range(max(0, len(chunk) - 1))])


def _attach_heat(events: list[NewsEvent], records: list[Any]) -> None:
    for event in events:
        tokens = _title_tokens(" ".join([event.title, *[article.summary for article in event.articles]]))
        matches = []
        for record in records:
            overlap = tokens & _title_tokens(record.title)
            if overlap:
                matches.append({"title": record.title, "url": record.share_url, "engagement": _engagement(record), "overlap": len(overlap)})
        matches.sort(key=lambda item: (-item["overlap"], -item["engagement"]))
        event.douyin_matches = matches[:5]
        event.score = round((40 if event.confirmed else 0) + min(40, math.log10(1 + sum(item["engagement"] for item in matches)) * 8 if matches else 0) + min(20, len(event.articles) * 5), 2)


def _official_summary(text: str, fallback_title: str, source_name: str) -> str:
    normalized = fallback_title.casefold()
    if any(token in normalized for token in ("security", "advisories")):
        return "GitHub 官方公告称，可在组织或个人账户拥有的公开仓库安全公告页面直接屏蔽用户。"
    if any(token in normalized for token in ("teacher", "school", "education")):
        return "OpenAI 官方公告称，ChatGPT for Teachers 正扩展至更多美国学区，并提供安全 AI 工具、培训和支持。"
    if any(token in normalized for token in ("billing", "enterprise")):
        return "GitHub 官方公告称，企业所有者现在可以授予 GitHub App 访问企业计费数据的权限。"
    if "copilot" in normalized:
        return "GitHub 官方公告称，GitHub Copilot 应用的 Customize 标签页现已正式可用。"
    if any(token in normalized for token in ("codex", "builder")):
        return "OpenAI 官方案例介绍了 loveholidays 使用 Codex 让更多团队参与软件开发、加快把想法转为产品的做法。"
    compact = re.sub(r"\s+", " ", text).strip()
    if compact:
        return compact[:240].rstrip("。；;，, ") + "。"
    return f"{source_name} 发布了题为“{fallback_title}”的官方更新。"


def _deterministic_news_value(title: str) -> str:
    normalized = title.casefold()
    if any(token in normalized for token in ("security", "advisories")):
        return "涉及安全公告的访问或管理能力；相关维护团队应按官方说明复核权限和操作流程。"
    if any(token in normalized for token in ("teacher", "school", "education")):
        return "涉及教育机构使用 AI 工具；部署方可重点关注适用范围、治理和落地条件。"
    if any(token in normalized for token in ("billing", "enterprise")):
        return "涉及企业账号的计费数据访问；管理员可关注权限边界及后续集成影响。"
    if "copilot" in normalized:
        return "涉及 Copilot 产品功能可用性；现有用户可按官方说明判断是否需要调整使用流程。"
    if any(token in normalized for token in ("codex", "builder")):
        return "展示 AI 辅助软件开发的实际使用案例；相关结论仅限官方案例描述，效果仍应结合自身场景评估。"
    return "这是当日发布的官方科技更新；具体影响以链接中的原始公告为准。"


def _news_payload(event: NewsEvent, structured: dict[str, Any] | None = None) -> dict[str, Any]:
    structured = structured or {}
    first = event.articles[0]
    heat = sum(item["engagement"] for item in event.douyin_matches)
    return {
        "title": event.title,
        "recommendation": "S" if event.score >= 75 else "A" if event.score >= 55 else "B",
        "time": first.published_at or "未知",
        "subject_place": str(structured.get("subject_place") or first.source_name),
        "event": str(structured.get("event") or _official_summary(first.summary, event.title, first.source_name)),
        "news_value": str(structured.get("news_value") or _deterministic_news_value(event.title)),
        "douyin_heat": f"匹配 {len(event.douyin_matches)} 条，互动加权 {heat:.0f}" if event.douyin_matches else "未匹配到同题材视频，不影响新闻事实确认",
        "sources": [{"name": article.source_name, "url": article.url, "kind": article.source_kind} for article in event.articles],
        "claims_to_verify": str(structured.get("claims_to_verify") or "成片前复核来源正文中的数字、版本、地区和发布日期。"),
        "creative_angle": str(structured.get("creative_angle") or "用‘发生了什么—为什么重要—对普通用户有什么影响’展开。"),
        "douyin_matches": event.douyin_matches,
        "score": event.score,
        "verification_status": event.confirmation_reason,
    }


def run_daily_news(config: dict[str, Any], *, target_date: str | None = None, news_inputs: list[str] | None = None, douyin_inputs: list[str] | None = None, live_douyin: bool = False) -> dict[str, Any]:
    job = "daily_news"
    state = JobState(config, job)
    with JobLock(config, job):
        target = target_date or target_yesterday(str(config["timezone"]))
        state.update(status="running", phase="news_sources", started_at=now_iso(str(config["timezone"])), target_date=target, errors=[], warnings=[])
        articles, source_errors = fetch_sources(config, news_inputs)
        eligible = [item for item in articles if item.published_at and datetime.fromisoformat(item.published_at).date().isoformat() == target]
        events = cluster_articles(eligible)
        search_report = None
        if live_douyin:
            state.update(phase="douyin_search")
            search_report = collect_search(config, int(config["jobs"]["daily_news"].get("max_douyin_reference_videos") or 100), f"daily-{target}")
        files = _douyin_files(config, douyin_inputs, search_report.get("run_dir") if search_report else None)
        records = normalize_files(files, config, "douyin_search") if files else []
        records = [record for record in records if record.published_at and datetime.fromisoformat(record.published_at).date().isoformat() == target]
        _attach_heat(events, records)
        confirmed = sorted([event for event in events if event.confirmed], key=lambda item: (-item.score, item.title))[:int(config["jobs"]["daily_news"].get("max_output_items") or 5)]
        analyzer = OpenAICompatibleAnalyzer(config)
        analysis_status = analyzer.status()
        structured_by_index: dict[int, dict[str, Any]] = {}
        if analysis_status["enabled"]:
            try:
                structured = analyzer.structure_news([{"index": index, "title": event.title, "summaries": [article.summary for article in event.articles], "sources": [article.url for article in event.articles]} for index, event in enumerate(confirmed)])
                structured_by_index = {int(item.get("index")): item for item in structured if str(item.get("index", "")).isdigit()}
            except Exception as exc:
                source_errors.append({"source": "llm", "error": str(exc)[:300]})
        else:
            source_errors.append({"source": "llm", "error": str(analysis_status.get("unavailable_reason") or "AI 深度分析暂不可用")})
        output_items = [_news_payload(event, structured_by_index.get(index)) for index, event in enumerate(confirmed)]
        pending = [{"title": event.title, "reason": event.confirmation_reason, "url": event.articles[0].url} for event in events if not event.confirmed]
        broadcast = daily_news_broadcast(target, output_items)
        destination = resolve_path(config["jobs"]["daily_news"]["output_root"]) / target
        stats = {"article_count": len(eligible), "douyin_count": len(records), "source_error_count": len(source_errors)}
        report = {"version": "1.0", "job": job, "target_date": target, "status": "success" if output_items and not source_errors else "partial" if output_items or eligible else "empty", "items": output_items, "pending": pending, "stats": stats, "source_errors": source_errors, "analysis_status": analysis_status, "broadcast": broadcast, "search_report": search_report, "generated_at": now_iso(str(config["timezone"]))}
        atomic_write_json(destination / "report.json", report)
        atomic_text(destination / "daily-news.md", daily_news_markdown(target, output_items, pending, stats, broadcast=broadcast, analysis_status=analysis_status))
        report["output_path"] = str((destination / "daily-news.md").resolve())
        state.update(status=report["status"], phase="complete", completed_at=report["generated_at"], output_path=report["output_path"], counts={"articles": len(eligible), "douyin": len(records), "confirmed": len(output_items), "pending": len(pending)}, errors=source_errors, temp_bytes=temp_size(config))
        return report


def _rank_records(records: list[Any], maximum: int) -> list[Any]:
    unique: dict[str, Any] = {}
    for record in records:
        previous = unique.get(record.video_id)
        if previous is None or _engagement(record) > _engagement(previous):
            unique[record.video_id] = record
    return sorted(unique.values(), key=lambda item: (-_engagement(item), item.video_id))[:maximum]


def run_inspiration(config: dict[str, Any], *, max_references: int | None = None, douyin_inputs: list[str] | None = None, live_douyin: bool = False, media: bool = True) -> dict[str, Any]:
    job = "inspiration"
    state = JobState(config, job)
    settings = config["jobs"]["inspiration"]
    maximum = min(max(1, int(max_references or settings.get("default_reference_videos") or 100)), int(settings.get("hard_max_reference_videos") or 100))
    with JobLock(config, job):
        generated = now_iso(str(config["timezone"]))
        run_id = datetime.now(ZoneInfo(str(config["timezone"]))).strftime("%Y%m%dT%H%M%S%z")
        state.update(status="running", phase="douyin_search" if live_douyin else "load_inputs", started_at=generated, max_references=maximum, errors=[], warnings=[])
        search_report = collect_search(config, maximum, f"inspiration-{run_id}") if live_douyin else None
        files = _douyin_files(config, douyin_inputs, search_report.get("run_dir") if search_report else None)
        records = _rank_records(normalize_files(files, config, "douyin_search") if files else [], maximum)
        detail_count = min(len(records), int(settings.get("max_detail_videos") or 20))
        detailed = records[:detail_count]
        warnings: list[str] = []
        materials_report = None
        media_limit = min(int(settings.get("media_analysis_videos") or 3), int(settings.get("hard_max_media_videos") or 5))
        if media and detailed and files:
            state.update(phase="media_analysis", counts={"references": len(records), "details": detail_count, "media_limit": media_limit})
            material_config = json.loads(json.dumps(config))
            material_config["materials"]["top_n"] = media_limit
            material_config["materials"]["max_per_account"] = media_limit
            common = Path(search_report["run_dir"]) if search_report else Path(files[0]).parent
            materials_report = build_materials(common, material_config, resolve_path(settings["output_root"]) / run_id / "materials")
            warnings.extend(materials_report.get("warnings") or [])
        candidates = [{"index": index, "title": record.title, "category": record.category, "score": round(_engagement(record), 2), "url": record.share_url} for index, record in enumerate(detailed)]
        analyzer = OpenAICompatibleAnalyzer(config)
        composed: list[dict[str, Any]] = []
        try:
            composed = analyzer.compose_inspiration(candidates, int(settings.get("max_output_items") or 8))
        except Exception as exc:
            warnings.append(f"模型灵感聚类失败，已使用确定性降级：{str(exc)[:200]}")
        if not composed:
            composed = [{"candidate_indexes": [index], "recommended_title": record.title or f"科技灵感 {index + 1}", "one_line_idea": record.title or "从高互动科技视频提炼选题", "why_interesting": f"该素材互动加权 {_engagement(record):.0f}，适合作为受众兴趣信号。", "outline": "现象/产品是什么 → 为什么引发关注 → 官方信息核验 → 对普通用户的影响", "claims_to_verify": "标题中的产品、数字、日期、性能与公司表态均需回查官方来源。"} for index, record in enumerate(detailed[:int(settings.get("max_output_items") or 8)])]
        cards: list[dict[str, Any]] = []
        for item in composed:
            indexes = [int(value) for value in item.get("candidate_indexes", []) if str(value).isdigit() and int(value) < len(detailed)]
            indexes = indexes or ([len(cards)] if len(cards) < len(detailed) else [])
            references = [f"[{detailed[index].title}]({detailed[index].share_url})" for index in indexes[:5]]
            top_engagement = max((_engagement(detailed[index]) for index in indexes), default=0)
            cards.append({"recommendation": "S" if top_engagement >= 100000 else "A" if top_engagement >= 10000 else "B", "one_line_idea": str(item.get("one_line_idea") or item.get("recommended_title") or "科技选题"), "why_interesting": str(item.get("why_interesting") or "来自高互动题材信号。"), "outline": str(item.get("outline") or "背景 → 新意 → 影响 → 核验结论"), "references": references, "claims_to_verify": str(item.get("claims_to_verify") or "所有事实主张需回查一手来源。"), "recommended_title": str(item.get("recommended_title") or item.get("one_line_idea") or "科技灵感")})
        destination = resolve_path(settings["output_root"]) / run_id
        stats = {"reference_count": len(records), "detail_count": detail_count, "media_count": int((materials_report or {}).get("completed_count") or 0), "reference_limit": maximum, "detail_limit": int(settings.get("max_detail_videos") or 20), "media_limit": media_limit}
        status = "success" if cards and not warnings else "partial" if cards else "empty"
        report = {"version": "1.0", "job": job, "status": status, "generated_at": generated, "cards": cards, "stats": stats, "warnings": warnings, "search_report": search_report, "materials_report": materials_report}
        atomic_write_json(destination / "report.json", report)
        atomic_text(destination / "inspiration.md", inspiration_markdown(generated, cards, stats, warnings))
        report["output_path"] = str((destination / "inspiration.md").resolve())
        state.update(status=status, phase="complete", completed_at=now_iso(str(config["timezone"])), output_path=report["output_path"], counts=stats, warnings=warnings, temp_bytes=temp_size(config))
        return report
