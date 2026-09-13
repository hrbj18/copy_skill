from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from .config import project_root, resolve_path
from .exporter import atomic_write_json
from .reporting import atomic_text


CONTENT_TYPES = {"news_lead", "creator_review", "creator_experiment", "creator_tutorial", "creator_opinion", "mixed", "uncertain"}
EVIDENCE_STATUSES = {"verified_official", "verified_multi_source", "creator_primary", "unverified_claim", "conflicting", "insufficient_metadata"}
EDITORIAL_STATUSES = {"ready_for_news_script", "research_required", "rumor_analysis_only", "ready_for_tech_talk", "manual_review", "ignored"}
OVERRIDE_FIELDS = {"primary_content_type", "secondary_content_types", "ignored", "pinned", "video_candidate", "editor_note"}


def stable_topic_id(item: dict[str, Any]) -> str:
    video_ids = sorted(str(video.get("video_id") or "") for video in item.get("representative_videos") or [])
    material = "|".join(video_ids) or str(item.get("title") or "")
    return "topic-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _corpus(item: dict[str, Any]) -> str:
    return " ".join([str(item.get("title") or ""), *[str(video.get("title") or "") for video in item.get("representative_videos") or []]]).casefold()


def classify_content(item: dict[str, Any]) -> tuple[str, list[str]]:
    corpus = _corpus(item)
    news = any(value in corpus for value in ("发布", "上市", "首发", "来了", "公告", "漏洞", "政策", "事故", "价格", "涨价", "降价", "融资", "收购", "launch", "release", "announc", "security advisory", "m6"))
    review = any(value in corpus for value in ("评测", "体验", "对比", "差距", "值不值", "好不好"))
    experiment = any(value in corpus for value in ("实验", "实测", "演示", "测试", "跑本地", "跑ai", "跑 ai"))
    tutorial = any(value in corpus for value in ("教程", "技巧", "怎么用", "如何", "方法", "一步步"))
    opinion = any(value in corpus for value in ("观点", "为什么", "还是", "焦虑", "争议", "判断", "怎么看"))
    creator_types = [name for name, matched in (("creator_review", review), ("creator_experiment", experiment), ("creator_tutorial", tutorial), ("creator_opinion", opinion)) if matched]
    if news and creator_types:
        return "mixed", ["news_lead", *creator_types]
    if creator_types:
        return creator_types[0], creator_types[1:]
    if news:
        return "news_lead", []
    return "uncertain", []


def evidence_for(item: dict[str, Any], official_event: Any | None) -> tuple[str, list[dict[str, Any]], str | None, str]:
    if official_event is not None:
        sources = [{"name": article.source_name, "url": article.url, "kind": article.source_kind} for article in official_event.articles]
        official = any(source["kind"] in {"official", "primary"} for source in sources)
        summary = official_event.articles[0].summary or f"{official_event.articles[0].source_name} 发布了相关记录。"
        return ("verified_official" if official else "verified_multi_source", sources, official_event.articles[0].published_at, summary[:500])
    primary, secondary = classify_content(item)
    if primary.startswith("creator_") or primary == "mixed" and any(value.startswith("creator_") for value in secondary):
        return "creator_primary", [], None, "原视频可证明作者发布、演示、评测或表达了该内容；不自动证明其中外部事实。"
    if primary == "uncertain":
        return "insufficient_metadata", [], None, "自动分类信息不足，保留给人工分流。"
    return "unverified_claim", [], None, "未找到满足门槛的可靠来源；外部事实性主张不得作为已证实事实。"


def _claims(item: dict[str, Any], primary_type: str, _evidence_status: str) -> tuple[list[str], list[str]]:
    corpus = _corpus(item)
    claims: list[str] = []
    if any(value in corpus for value in ("发布", "上市", "首发", "来了")):
        claims.append("是否已经由品牌或公司官方发布，以及准确发布日期。")
    if any(value in corpus for value in ("芯片", "m6", "gb", "内存", "参数", "配置", "续航", "性能")):
        claims.append("产品型号、规格、配置与性能结论。")
    if any(value in corpus for value in ("价格", "万元", "万起", "元")):
        claims.append("价格、地区、币种和销售条件。")
    if primary_type.startswith("creator_") or primary_type == "mixed":
        claims.append("视频中涉及的外部产品参数、公司表态和比较结论。")
    claims = list(dict.fromkeys(claims))
    if not claims:
        claims.append("标题或视频中涉及的所有外部事实性陈述。")
    return claims, []


def _editorial_status(primary_type: str, evidence_status: str, ignored: bool = False) -> str:
    if ignored:
        return "ignored"
    if evidence_status == "insufficient_metadata" or primary_type == "uncertain":
        return "manual_review"
    if primary_type == "news_lead":
        return "ready_for_news_script" if evidence_status in {"verified_official", "verified_multi_source"} else "research_required"
    if primary_type == "mixed":
        return "ready_for_news_script" if evidence_status in {"verified_official", "verified_multi_source"} else "ready_for_tech_talk"
    if primary_type.startswith("creator_") and evidence_status == "creator_primary":
        return "ready_for_tech_talk"
    return "rumor_analysis_only" if evidence_status == "unverified_claim" else "manual_review"


def _creative_value(item: dict[str, Any], primary_type: str) -> dict[str, Any]:
    components = item.get("score_components") or {}
    videos = item.get("representative_videos") or []
    interactions = [video.get("interactions") or {} for video in videos]
    comments = sum(int(value.get("comment") or 0) for value in interactions)
    likes = sum(int(value.get("like") or 0) for value in interactions)
    corpus = _corpus(item)
    conflict = "present" if any(value in corpus for value in ("对比", "为什么", "还是", "争议", "差距", "焦虑")) else "unknown"
    visual = "present" if primary_type in {"creator_review", "creator_experiment", "creator_tutorial", "mixed"} or any(value in corpus for value in ("演示", "实测", "对比")) else "unknown"
    discussion_density = round(comments / max(1, likes), 4) if likes or comments else None
    score = min(40.0, float(item.get("score") or 0) * 0.4)
    score += min(20.0, (discussion_density or 0) * 200)
    score += 15.0 if conflict == "present" else 0.0
    score += 15.0 if visual == "present" else 0.0
    score += min(10.0, float(components.get("freshness") or 0))
    return {
        "score": round(score, 3), "heat_basis": round(float(item.get("score") or 0), 3),
        "discussion_density": discussion_density if discussion_density is not None else "unknown",
        "contrast_or_conflict": conflict, "demonstration_or_visual": visual,
        "technology_audience_relevance": "present", "recency_points": components.get("freshness", "unknown"),
        "note": "结构化评估只使用标题和公开互动；unknown 表示安全元数据不足，未作推断。",
    }


def build_editorial_item(item: dict[str, Any], official_event: Any | None) -> dict[str, Any]:
    primary, secondary = classify_content(item)
    if official_event is not None and primary == "uncertain":
        primary = "news_lead"
    evidence, sources, official_date, official_summary = evidence_for(item, official_event)
    claims_to_verify, verified_facts = _claims(item, primary, evidence)
    if evidence in {"verified_official", "verified_multi_source"}:
        verified_facts = [official_summary]
    topic_id = stable_topic_id(item)
    author = str((item.get("representative_videos") or [{}])[0].get("author") or "未知作者")
    if primary == "news_lead":
        safe_hook = f"抖音正在热传“{str(item.get('title') or '')[:100]}”相关说法；" + ("已有可靠来源核验。" if evidence.startswith("verified_") else "目前尚未获得可靠官方确认。")
        news_guidance = "按已核验事实写稿。" if evidence.startswith("verified_") else "仅制作核查、传闻追踪或官方回应观察，不得按事实播报。"
        talk_angle = "讨论这一说法为何升温、受众关注什么，以及可靠来源是否回应。"
    elif primary == "mixed":
        safe_hook = f"{author}围绕“{str(item.get('title') or '')[:100]}”发布了兼具外部事件与个人分析的内容。"
        news_guidance = "只使用 verified_facts；其余外部说法继续核验。"
        talk_angle = "拆分外部事实和作者观点，讨论其冲突、体验或判断框架。"
    elif primary.startswith("creator_"):
        safe_hook = f"{author}发布了关于“{str(item.get('title') or '')[:100]}”的原创内容。"
        news_guidance = "不可将作者内容自动改写为已核验新闻。"
        talk_angle = "参考作者的评测、实验、教程或观点结构，形成独立表达，不照搬原脚本。"
    else:
        safe_hook = f"抖音出现了“{str(item.get('title') or '')[:100]}”相关热门内容，当前需要人工分流。"
        news_guidance = "分类与证据不足，暂不作为事实新闻写稿。"
        talk_angle = "先人工判断内容类型和可用素材，再决定创作方向。"
    do_not_claim = list(claims_to_verify) or ["不得超出所列 verified_facts 和可靠来源作延伸断言。"]
    return item | {
        "topic_id": topic_id, "heat_score": item["score"], "heat_components": item["score_components"],
        "primary_content_type": primary, "secondary_content_types": secondary, "content_type": primary,
        "evidence_status": evidence, "editorial_status": _editorial_status(primary, evidence),
        "verified_facts": verified_facts, "claims_to_verify": claims_to_verify, "do_not_claim": do_not_claim,
        "official_sources": sources, "official_published_at": official_date, "official_summary": official_summary,
        "why_worth_attention": item["why_hot"], "safe_hook": safe_hook,
        "news_usage_guidance": news_guidance, "tech_talk_angle": talk_angle,
        "creative_value": _creative_value(item, primary),
        "pinned": False, "video_candidate": False, "ignored": False, "editor_note": "",
        "automatic_primary_content_type": primary,
    }


class EditorialOverrideStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"version": "1.0", "revision": 0, "topics": {}}
        except (OSError, json.JSONDecodeError):
            return {"version": "1.0", "revision": 0, "topics": {}}

    def _acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(descriptor)
                return
            except FileExistsError:
                try:
                    if time.time() - self.lock_path.stat().st_mtime > 30:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                time.sleep(0.02)
        raise TimeoutError("编辑覆盖文件正在被其他操作更新")

    def update(self, topic_id: str, values: dict[str, Any]) -> dict[str, Any]:
        if not topic_id.startswith("topic-") or any(key not in OVERRIDE_FIELDS for key in values):
            raise ValueError("无效的编辑覆盖字段或主题 ID")
        if "primary_content_type" in values and values["primary_content_type"] not in CONTENT_TYPES:
            raise ValueError("无效的内容类型")
        if "secondary_content_types" in values and (not isinstance(values["secondary_content_types"], list) or any(value not in CONTENT_TYPES for value in values["secondary_content_types"])):
            raise ValueError("无效的次类型")
        if "editor_note" in values:
            values = dict(values)
            values["editor_note"] = str(values["editor_note"]).strip()[:500]
        self._acquire()
        try:
            payload = self.load()
            topics = dict(payload.get("topics") or {})
            current = dict(topics.get(topic_id) or {})
            current.update(values)
            topics[topic_id] = current
            payload = {"version": "1.0", "revision": int(payload.get("revision") or 0) + 1, "topics": topics}
            atomic_write_json(self.path, payload)
            return current
        finally:
            self.lock_path.unlink(missing_ok=True)


def override_path(config: dict[str, Any]) -> Path:
    raw = Path(str(config["workbench"].get("editorial_override_path") or "data/state/editorial_overrides.json"))
    if raw.is_absolute() or ".." in raw.parts:
        raise ValueError("editorial_override_path 必须是项目内相对路径")
    root = Path(config.get("_project_root") or project_root()).resolve()
    path = (root / raw).resolve()
    path.relative_to(root)
    return path


def apply_overrides(items: list[dict[str, Any]], overrides: dict[str, Any]) -> list[dict[str, Any]]:
    values = overrides.get("topics") or {}
    output: list[dict[str, Any]] = []
    for original in items:
        item = dict(original)
        override = values.get(item["topic_id"]) or {}
        for key in OVERRIDE_FIELDS:
            if key in override:
                item[key] = override[key]
        item["content_type"] = item["primary_content_type"]
        item["editorial_status"] = _editorial_status(item["primary_content_type"], item["evidence_status"], bool(item.get("ignored")))
        item["manual_override"] = bool(override)
        output.append(item)
    return output


def partition_views(items: list[dict[str, Any]], pending: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    active = [item for item in items if not item.get("ignored")]
    news = [item for item in active if item["primary_content_type"] in {"news_lead", "mixed"} or "news_lead" in item.get("secondary_content_types", [])]
    talk = [item for item in active if item["primary_content_type"].startswith("creator_") or item["primary_content_type"] == "mixed" or any(value.startswith("creator_") for value in item.get("secondary_content_types", []))]
    ignored = [item for item in items if item.get("ignored")]
    review = [item for item in active if item["editorial_status"] == "manual_review"] + pending + ignored
    return {"hotspots": active, "news_leads": news, "tech_talk": talk, "manual_review": review, "ignored": ignored}


def _news_entry(item: dict[str, Any], target_date: str) -> dict[str, Any]:
    return {key: item[key] for key in ("topic_id", "heat_rank", "heat_score", "title", "primary_content_type", "secondary_content_types", "evidence_status", "editorial_status", "verified_facts", "claims_to_verify", "official_sources", "safe_hook", "do_not_claim", "representative_videos")} | {"target_date": target_date, "recommended_news_angle": item["news_usage_guidance"]}


def _talk_entry(item: dict[str, Any], target_date: str) -> dict[str, Any]:
    first = item["representative_videos"][0]
    conflict = item["creative_value"]["contrast_or_conflict"]
    visual = item["creative_value"]["demonstration_or_visual"]
    return {
        "topic_id": item["topic_id"], "heat_rank": item["heat_rank"], "heat_score": item["heat_score"], "target_date": target_date,
        "title": item["title"], "primary_content_type": item["primary_content_type"], "secondary_content_types": item["secondary_content_types"],
        "original_author": first["author"], "douyin_url": first["share_url"], "editorial_status": item["editorial_status"],
        "one_line_topic": item["safe_hook"], "recommended_angle": item["tech_talk_angle"], "why_discuss": item["why_worth_attention"],
        "controversy_or_conflict": conflict, "visual_demonstration": visual, "target_audience": "关注科技产品、AI 工具和实际使用体验的受众",
        "three_part_structure": ["热点与作者原始观点/演示", "独立拆解体验、冲突或方法", "外部事实核验与适用边界"],
        "claims_to_verify": item["claims_to_verify"], "do_not_claim": item["do_not_claim"],
        "original_reference_notice": "仅参考选题、实验和结构，不得照搬原作者脚本、画面或表达。",
    }


def _reference_markdown(title: str, items: list[dict[str, Any]], kind: str) -> str:
    lines = [f"# {title}", "", "> 本快照由项目内编辑工作台生成；热度不等于事实，所有禁说内容必须保留。", ""]
    if not items:
        lines.extend(["本次没有符合该编辑路径的条目。", ""])
    for item in items:
        lines.extend([f"## #{item['heat_rank']} {item['title']}", "", f"- 热度：{item['heat_score']}", f"- 编辑状态：{item['editorial_status']}"])
        if kind == "news":
            lines.extend([f"- 安全开场：{item['safe_hook']}", f"- 推荐角度：{item['recommended_news_angle']}", f"- 已核验事实：{item['verified_facts'] or ['无']}", f"- 待核实：{item['claims_to_verify'] or ['无']}", f"- 禁止宣称：{item['do_not_claim']}"])
        else:
            lines.extend([f"- 原创作者：{item['original_author']}", f"- 抖音链接：{item['douyin_url']}", f"- 一句话选题：{item['one_line_topic']}", f"- 推荐角度：{item['recommended_angle']}", f"- 讨论价值：{item['why_discuss']}", f"- 争议/冲突：{item['controversy_or_conflict']}", f"- 可视化：{item['visual_demonstration']}", f"- 三段式结构：{item['three_part_structure']}", f"- 待核实：{item['claims_to_verify']}", f"- 禁止宣称：{item['do_not_claim']}", f"- 原创参考说明：{item['original_reference_notice']}"])
        lines.append("")
    return "\n".join(lines)


def write_editorial_exports(config: dict[str, Any], report: dict[str, Any]) -> dict[str, str]:
    root = resolve_path(config["workbench"].get("editorial_output_root") or "output/editorial-board") / report["target_date"]
    views = report["views"]
    news_items = [_news_entry(item, report["target_date"]) for item in views["news_leads"]]
    talk_items = [_talk_entry(item, report["target_date"]) for item in views["tech_talk"]]
    news_payload = {"version": "1.0", "schema": "news-reference-v1", "target_date": report["target_date"], "items": news_items}
    talk_payload = {"version": "1.0", "schema": "tech-talk-reference-v1", "target_date": report["target_date"], "items": talk_items}
    paths = {
        "news_json": str((root / "news-reference.json").resolve()), "news_markdown": str((root / "news-reference.md").resolve()),
        "tech_talk_json": str((root / "tech-talk-reference.json").resolve()), "tech_talk_markdown": str((root / "tech-talk-reference.md").resolve()),
    }
    atomic_write_json(root / "news-reference.json", news_payload)
    atomic_text(root / "news-reference.md", _reference_markdown(f"{report['target_date']} 科技新闻视频参考", news_items, "news"))
    atomic_write_json(root / "tech-talk-reference.json", talk_payload)
    atomic_text(root / "tech-talk-reference.md", _reference_markdown(f"{report['target_date']} 科技杂谈视频参考", talk_items, "talk"))
    return paths
