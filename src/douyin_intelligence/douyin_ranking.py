from __future__ import annotations

import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import resolve_path
from .exporter import atomic_write_json
from .editorial_board import EditorialOverrideStore, apply_overrides, build_editorial_item, override_path, partition_views, write_editorial_exports
from .job_runtime import JobLock, JobState, now_iso, target_yesterday
from .news_sources import NewsArticle, cluster_articles, fetch_sources
from .normalize import normalize_files
from .reporting import atomic_text
from .search_collector import collect_search


DEFAULT_WEIGHTS = {
    "like": 22.0,
    "comment": 20.0,
    "collect": 18.0,
    "share": 22.0,
    "related_videos": 8.0,
    "freshness": 10.0,
}
_SAFE_DOUYIN_URL = re.compile(r"^https://(?:www\.)?douyin\.com/video/(\d{8,})$")


def _tokens(text: str) -> set[str]:
    latin = re.findall(r"[a-z0-9][a-z0-9+._-]{2,}", text.casefold())
    chinese = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    bigrams = [value[index:index + 2] for value in chinese for index in range(len(value) - 1)]
    return set(latin + bigrams)


def _plain_share_url(video_id: str, value: str) -> str:
    match = _SAFE_DOUYIN_URL.fullmatch(value.strip())
    if match:
        return f"https://www.douyin.com/video/{match.group(1)}"
    return f"https://www.douyin.com/video/{video_id}" if video_id.isdigit() else ""


def _log_points(value: int, maximum: float) -> float:
    return round(maximum * min(1.0, math.log1p(max(0, value)) / math.log1p(1_000_000)), 3)


def _video_components(record: Any, target_date: str, weights: dict[str, float]) -> dict[str, float]:
    end = datetime.fromisoformat(f"{target_date}T23:59:59+08:00")
    published = datetime.fromisoformat(record.published_at)
    age_hours = max(0.0, min(48.0, (end - published).total_seconds() / 3600))
    return {
        "like": _log_points(int(record.digg_count or 0), weights["like"]),
        "comment": _log_points(int(record.comment_count or 0), weights["comment"]),
        "collect": _log_points(int(record.collect_count or 0), weights["collect"]),
        "share": _log_points(int(record.share_count or 0), weights["share"]),
        "freshness": round(weights["freshness"] * math.exp(-age_hours / 24.0), 3),
    }


def _similarity(left: set[str], right: set[str]) -> float:
    return len(left & right) / max(1, min(len(left), len(right)))


def cluster_ranked_videos(records: list[Any], target_date: str, weights: dict[str, float]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    safe: list[Any] = []
    dropped: list[dict[str, str]] = []
    for record in records:
        url = _plain_share_url(record.video_id, record.share_url)
        if not record.title or not record.published_at or not url:
            dropped.append({"video_id": record.video_id, "reason": "缺少安全链接、标题或发布时间"})
            continue
        record.share_url = url
        if record.published_at[:10] != target_date:
            dropped.append({"video_id": record.video_id, "reason": "不在目标热度窗口"})
            continue
        safe.append(record)
    unique: dict[str, Any] = {}
    for record in sorted(safe, key=lambda item: (item.video_id, item.share_url)):
        current = unique.get(record.video_id)
        if current is None or sum(_video_components(record, target_date, weights).values()) > sum(_video_components(current, target_date, weights).values()):
            unique[record.video_id] = record
    retained = sorted(unique.values(), key=lambda item: (item.published_at or "", item.video_id))[:10]
    clusters: list[dict[str, Any]] = []
    for record in retained:
        tokens = _tokens(f"{record.title} {record.source_keyword}")
        match = next((cluster for cluster in clusters if len(tokens & cluster["tokens"]) >= 3 and _similarity(tokens, cluster["tokens"]) >= 0.55), None)
        if match is None:
            match = {"tokens": set(tokens), "videos": []}
            clusters.append(match)
        match["tokens"].update(tokens)
        match["videos"].append(record)
    output: list[dict[str, Any]] = []
    for cluster in clusters:
        videos = sorted(cluster["videos"], key=lambda item: (item.video_id, item.share_url))
        components = {key: round(sum(_video_components(item, target_date, weights)[key] for item in videos), 3) for key in ("like", "comment", "collect", "share", "freshness")}
        components["related_videos"] = round(weights["related_videos"] * min(1.0, math.log1p(len(videos)) / math.log(11)), 3)
        title_record = max(videos, key=lambda item: (sum(_video_components(item, target_date, weights).values()), item.video_id))
        total = round(sum(components.values()), 3)
        output.append({
            "title": title_record.title,
            "tokens": sorted(cluster["tokens"]),
            "score": total,
            "score_components": components,
            "video_count": len(videos),
            "heat_window": f"{target_date} 00:00–23:59 Asia/Shanghai",
            "why_hot": f"聚合 {len(videos)} 条去重公开元数据；点赞、评论、收藏、分享经对数压缩后参与评分，并计入发布时间衰减。",
            "representative_videos": [{
                "video_id": item.video_id, "title": item.title, "published_at": item.published_at,
                "author": item.account_name, "share_url": item.share_url,
                "interactions": {"like": int(item.digg_count or 0), "comment": int(item.comment_count or 0), "collect": int(item.collect_count or 0), "share": int(item.share_count or 0)},
            } for item in videos],
        })
    return sorted(output, key=lambda item: (-item["score"], -item["video_count"], item["title"].casefold(), item["representative_videos"][0]["video_id"])), dropped


def _official_match(cluster: dict[str, Any], events: list[Any]) -> Any | None:
    for event in events:
        corpus = _tokens(f"{event.title} {' '.join(article.summary for article in event.articles)}")
        overlap = set(cluster["tokens"]) & corpus
        if len(overlap) >= 2 and _similarity(set(cluster["tokens"]), corpus) >= 0.2:
            return event
    return None


def _creator_original(cluster: dict[str, Any]) -> bool:
    corpus = " ".join([cluster["title"], *[video["title"] for video in cluster["representative_videos"]]]).casefold()
    return any(marker in corpus for marker in ("评测", "实测", "体验", "教程", "演示", "实验", "观点", "为什么", "还是", "如何", "怎么", "对比", "生产力"))


def _item(cluster: dict[str, Any], event: Any | None) -> dict[str, Any]:
    base = {key: value for key, value in cluster.items() if key != "tokens"}
    if event is not None:
        first = event.articles[0]
        return base | {
            "content_type": "verified_news", "content_type_label": "已核验科技新闻",
            "evidence_status": "official_https_verified", "fact_credibility": "高：外部科技事件已由官方/第一方 HTTPS 来源核验。",
            "creative_reference_value": "高：兼具抖音热点证据和可回查的事实来源。",
            "verification_status": event.confirmation_reason,
            "official_summary": (first.summary or f"{first.source_name} 发布了与该主题相关的官方记录。")[:500],
            "official_published_at": first.published_at,
            "official_sources": [{"name": article.source_name, "url": article.url, "kind": article.source_kind} for article in event.articles],
            "content_angle": "先说明为什么在抖音中升温，再以官方链接逐项核对事实、时间和影响。",
        }
    if _creator_original(cluster):
        return base | {
            "content_type": "creator_original", "content_type_label": "博主原创内容",
            "evidence_status": "creator_primary_reference", "fact_credibility": "作者发布该评测、实验、教程或观点已由原视频链接证明；其中外部事实主张仍需另行核验。",
            "creative_reference_value": "高：可直接参考作者的演示、评测、实验或观点表达方式。",
            "verification_status": "原创内容一手参考",
            "official_summary": "不要求外部新闻来源；报告只陈述作者发布或演示了该内容，不将视频中的外部说法自动当成事实。",
            "official_published_at": None, "official_sources": [],
            "content_angle": "以作者的评测、实验或观点为创作参考，保留普通分享链接和互动证据；外部事实另行标注核验。",
        }
    return base | {
        "content_type": "unverified_claim", "content_type_label": "未核实事实主张",
        "evidence_status": "external_claim_unverified", "fact_credibility": "低：视频含有关新品、规格、公司行为或外部事件的说法，尚无可靠来源印证。",
        "creative_reference_value": "中：可用于观察讨论热度或追踪选题，不可作为事实性新闻播报依据。",
        "verification_status": "未核实主张/传闻",
        "official_summary": "未找到满足来源门槛的官方/第一方 HTTPS 新闻记录；不得将该产品或公司说法写成事实。",
        "official_published_at": None, "official_sources": [],
        "content_angle": "可以讨论为什么这一说法在抖音升温，但必须明确标注尚未获得官方确认。",
    }


def ranking_broadcast(report: dict[str, Any]) -> str:
    def spoken_title(value: str) -> str:
        compact = re.sub(r"\s+", " ", value).strip()
        return compact[:72].rstrip("，,。.!！?？ ") + ("…" if len(compact) > 72 else "")

    lines = [f"下面是北京时间 {report['target_date']} 的抖音科技热点榜，排名只由公开互动和发布时间决定。"]
    for item in report["hotspot_rankings"][:3]:
        video = item["representative_videos"][0]
        title = spoken_title(item["title"])
        if item["evidence_status"] in {"verified_official", "verified_multi_source"}:
            wording = f"第{item['rank']}名是已核验科技新闻《{title}》，由{video['author']}的相关视频带动讨论，并有官方 HTTPS 来源核验。"
        elif item["primary_content_type"].startswith("creator_") or item["primary_content_type"] == "mixed":
            wording = f"第{item['rank']}名是{video['author']}发布的{item['primary_content_type']}内容《{title}》，可作为科技杂谈参考；外部事实仍需另行核验。"
        else:
            wording = f"第{item['rank']}名涉及尚未获得官方确认的外部产品或事件说法《{title}》，当前只作为抖音热度选题，不作为事实播报。"
        lines.append(wording)
    return "".join(lines)


def ranking_markdown(report: dict[str, Any]) -> str:
    formula = report["formula"]
    lines = [f"# {report['target_date']} 双轨科技选题编辑榜", "", "> 热点先保留，类型再分流，事实最后把关。热度排名只使用公开抖音互动与时间信号。", "", "## 评分公式", "", f"- 互动按对数压缩；相关视频数权重 {formula['weights']['related_videos']}；新鲜度权重 {formula['weights']['freshness']}。", f"- 权重：{formula['weights']}；官方来源、分类和人工覆盖均不改变名次。", ""]
    def section(items: list[dict[str, Any]], empty: str) -> None:
        if not items:
            lines.extend([empty, ""])
        for item in items:
            if "heat_rank" not in item:
                lines.extend([f"### {item.get('title') or '元数据不足'}", "", f"- 证据状态：{item.get('evidence_status')}", f"- 编辑状态：{item.get('editorial_status')}", f"- 原因：{item.get('reason')}", ""])
                continue
            lines.extend([f"### {item['heat_rank']}. {item['title']}", "", f"- 热度：{item['heat_score']}；分项：{item['heat_components']}", f"- 主类型：{item['primary_content_type']}；次类型：{item['secondary_content_types'] or ['无']}", f"- 证据状态：{item['evidence_status']}；编辑状态：{item['editorial_status']}", f"- 相关视频：{item['video_count']}；窗口：{item['heat_window']}", f"- 为什么值得关注：{item['why_worth_attention']}", f"- 安全表述：{item['safe_hook']}", f"- 新闻方向：{item['news_usage_guidance']}", f"- 杂谈方向：{item['tech_talk_angle']}", f"- 待核实：{item['claims_to_verify'] or ['无']}", f"- 禁止直接宣称：{item['do_not_claim']}", f"- 创作价值：{item['creative_value']}", f"- 官方摘要：{item['official_summary']}", f"- 官方发布日期：{item['official_published_at'] or '无'}", "- 官方来源："])
            lines.extend(f"  - [{source['name']}]({source['url']})" for source in item["official_sources"])
            lines.extend(["- 代表性普通抖音分享链接："])
            lines.extend(f"  - [{video['title']}]({video['share_url']})（{video['author']}，{video['published_at']}）" for video in item["representative_videos"])
            lines.extend([f"- 编辑备注：{item.get('editor_note') or '无'}", ""])
    lines.extend(["## 抖音科技总热榜", ""])
    section(report["hotspot_rankings"], "本次没有达到元数据完整性与热度门槛的抖音科技主题。")
    lines.extend(["## 科技新闻线索", ""])
    section(report["views"]["news_leads"], "本次没有科技新闻线索。")
    lines.extend(["## 科技杂谈灵感", ""])
    section(report["views"]["tech_talk"], "本次没有科技杂谈灵感。")
    lines.extend(["## 待分类/待处理", ""])
    section(report["views"]["manual_review"], "本次没有待处理主题。")
    lines.extend(["## 口播", "", report["broadcast"], ""])
    lines.extend(["## AI 深度分析状态", "", "- 可用：否", "- 说明：AI 深度分析暂不可用；本排行榜使用确定性评分、聚类和来源匹配模板，LLM 不参与名次。", ""])
    return "\n".join(lines)


def run_douyin_tech_ranking(config: dict[str, Any], *, target_date: str | None = None, douyin_inputs: list[str] | None = None, news_inputs: list[str] | None = None, live_douyin: bool = False) -> dict[str, Any]:
    settings = config["jobs"]["douyin_tech_ranking"]
    target = target_date or target_yesterday(str(config["timezone"]))
    maximum = min(10, max(1, int(settings.get("max_metadata_records") or 10)))
    weights = {key: float((settings.get("weights") or {}).get(key, value)) for key, value in DEFAULT_WEIGHTS.items()}
    state = JobState(config, "douyin_tech_ranking")
    with JobLock(config, "douyin_tech_ranking"):
        state.update(status="running", phase="douyin_metadata", target_date=target, errors=[], warnings=[], counts={"metadata_budget": maximum})
        collection = collect_search(config, maximum, f"ranking-{target}", keywords=list(settings.get("keywords") or []), hard_max=maximum) if live_douyin else None
        paths = [Path(value) for value in (douyin_inputs or [])]
        if collection:
            paths.extend(Path(value) for value in collection.get("files") or [])
        records = normalize_files(paths, config, "douyin_search") if paths else []
        clusters, dropped = cluster_ranked_videos(records, target, weights)
        state.update(phase="official_verification", counts={"metadata_budget": maximum, "metadata_retained": min(maximum, len(records)), "clusters": len(clusters)})
        articles, source_errors = fetch_sources(config, news_inputs)
        eligible_articles = [article for article in articles if article.published_at and article.published_at[:10] == target]
        official_events = cluster_articles(eligible_articles)
        hotspots: list[dict[str, Any]] = []
        for cluster in clusters:
            item = build_editorial_item(cluster, _official_match(cluster, official_events))
            hotspots.append(item)
        for index, item in enumerate(hotspots, 1):
            item["rank"] = index
            item["heat_rank"] = index
        overrides = EditorialOverrideStore(override_path(config)).load()
        hotspots = apply_overrides(hotspots, overrides)
        pending = [{"topic_id": f"pending-{entry.get('video_id')}", "title": entry.get("video_id") or "元数据不足", "evidence_status": "insufficient_metadata", "editorial_status": "manual_review", "reason": entry.get("reason")} for entry in dropped]
        views = partition_views(hotspots, pending)
        status = "success" if hotspots and not source_errors else "partial" if hotspots else "empty"
        destination = resolve_path(settings["output_root"]) / target
        report = {
            "version": "1.0", "job": "douyin_tech_ranking", "target_date": target, "status": status,
            "formula": {"version": "deterministic-v1", "weights": weights, "count_transform": "log1p capped at 1,000,000", "freshness_half_life_hours": 24, "stable_sort": ["score desc", "video_count desc", "title asc", "representative_video_id asc"]},
            "metadata_budget": maximum, "metadata_retained": min(maximum, len(records)), "dropped_metadata": dropped,
            "collection": collection, "hotspot_rankings": hotspots, "pending_items": pending, "views": views,
            "source_errors": source_errors, "analysis_status": {"available": False, "reason": "AI 深度分析暂不可用；LLM 不参与排名。"}, "generated_at": now_iso(str(config["timezone"])),
        }
        report["broadcast"] = ranking_broadcast(report)
        report["editorial_exports"] = write_editorial_exports(config, report)
        atomic_write_json(destination / "ranking.json", report)
        atomic_text(destination / "ranking.md", ranking_markdown(report))
        report["output_path"] = str((destination / "ranking.md").resolve())
        errors = list(source_errors)
        if collection and collection.get("status") != "success":
            errors.append({"source": "douyin", "error": "未取得安全抖音元数据；如页面提示登录、二维码或 CAPTCHA，请在工作台专用浏览器完成验证后重试。"})
        state.update(status=status, phase="complete", completed_at=report["generated_at"], output_path=report["output_path"], counts={"metadata_budget": maximum, "metadata_retained": report["metadata_retained"], "hotspots": len(hotspots), "news_leads": len(views["news_leads"]), "tech_talk": len(views["tech_talk"]), "manual_review": len(views["manual_review"])}, errors=errors)
        return report
