from __future__ import annotations

import re
from typing import Any


_SPACE = re.compile(r"\s+")
_GAME_RULE_METAPHOR = re.compile(r"(?:改变|改写|重塑|颠覆|重新定义)游戏规则")
_STRONG_GAME_TERMS = (
    "游戏新作", "游戏定档", "新游", "手游", "端游", "主机游戏", "单机游戏", "steam游戏",
    "switch", "xbox", "ps5", "ns1", "游戏评测", "游民评测", "游戏攻略", "游戏推荐",
    "游戏资讯", "游戏体验", "试玩", "主机大作", "游戏流量风向标", "解谜游戏", "动作游戏",
    "开放世界游戏", "游戏发布", "游戏上线", "游戏公测", "游戏发售", "游戏首发", "游戏开发",
)
_TECH_CONTEXT_TERMS = ("ai", "大模型", "模型", "agent", "github", "开源项目", "科技工具", "机器人", "芯片", "系统", "硬件")
_POLICY_TERMS = ("工信部", "国务院", "国家标准", "国家政策", "专项行动", "监管", "采购力度", "首购首用")
_MODEL_TERMS = (
    "大模型", "模型发布", "模型开源", "开源模型", "minimax", "deepseek", "openai", "anthropic",
    "gemini", "gpt", "kimi", "glm", "腾讯混元", "通义", "qwen", "hugging face", "hy4", "h3 max",
)
_MODEL_ACTIONS = ("发布", "开源", "上线", "preview", "预览", "升级", "扩容", "调用", "推理", "权重")
_BIG_TECH_TERMS = ("腾讯", "阿里", "华为", "字节", "百度", "小米", "苹果", "谷歌", "微软", "meta", "英伟达", "亚马逊")
_STRATEGY_TERMS = ("战略", "收购", "合作", "组织调整", "回应", "扩容", "投入", "路线", "生态")
_CORE_TECH_TERMS = ("芯片", "半导体", "算力", "机器人", "人形机器人", "航天", "地月", "量子", "激光通信", "具身智能", "存储")
_CONSUMER_TERMS = (
    "手机", "电脑", "个人电脑", "消费级", "本地运行", "本地部署", "普通用户", "用户", "办公", "工作",
    "免费", "价格", "汽车", "智驾", "系统更新", "打印机", "ai工具", "ai眼镜", "可体验", "直播", "互动",
)
_DISCUSSION_TERMS = (
    "免费", "开源", "价格", "隐私", "强制", "争议", "排队", "扩容", "用户", "普通人", "是否", "能否",
    "取代", "影响", "本地", "体验", "选择", "限制", "成本", "工作", "直播", "互动",
)
_RISK_TERMS = ("疑似", "秘密", "逃避", "震惊", "救命", "内幕", "爆料", "传闻", "据称", "未证实", "文明")
_TUTORIAL_PROMOTION_TERMS = ("三块钱", "训练你自己的", "领取", "私信", "套壳", "手把手", "保姆级")
_TUTORIAL_INSTRUCTION_TERMS = ("教你", "如何", "步骤", "实操", "入门")
_VAGUE_TERMS = ("盘点", "三件事", "五件事", "从夯到拉", "悄悄改变", "科技圈大事", "钱从哪来")
_ABSTRACT_TERMS = ("地月", "量子", "载荷", "光机电控", "双向高速激光通信", "基础研究")


def _normalize(value: Any) -> str:
    return _SPACE.sub(" ", str(value or "")).strip().casefold()


def _event_corpus(event: dict[str, Any]) -> str:
    values: list[str] = [str(event.get("title") or "")]
    # Aliases are intentionally excluded: older story packs can contain a
    # cross-topic alias, which must never contaminate scope or editorial score.
    # Search keywords are discovery provenance, not evidence of story content.
    for video in event.get("contributing_videos") or []:
        if isinstance(video, dict):
            values.append(str(video.get("title") or ""))
    signature = event.get("event_signature") if isinstance(event.get("event_signature"), dict) else {}
    for key in ("identity_anchors", "model_anchors", "entities", "actions"):
        values.extend(str(item) for item in signature.get(key) or [] if item)
    return _normalize(" ".join(values))


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term.casefold() in text for term in terms)


def _term_matches(text: str, term: str) -> bool:
    normalized = term.casefold()
    if normalized.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", text) is not None
    return normalized in text


def _game_terms(text: str) -> list[str]:
    without_metaphor = _GAME_RULE_METAPHOR.sub("", text)
    matched = [term for term in _STRONG_GAME_TERMS if _term_matches(without_metaphor, term)]
    generic_game = "游戏" in without_metaphor and "游戏规则" not in without_metaphor
    if generic_game and not _contains_any(without_metaphor, _TECH_CONTEXT_TERMS):
        matched.append("游戏")
    return sorted(set(matched))


def classify_scope(event: dict[str, Any]) -> dict[str, Any]:
    """Return a conservative deterministic scope decision before expensive enrichment."""
    representative_terms = _game_terms(_normalize(event.get("title")))
    video_term_sets = [
        _game_terms(_normalize(video.get("title")))
        for video in event.get("contributing_videos") or []
        if isinstance(video, dict) and video.get("title")
    ]
    game_video_count = sum(bool(terms) for terms in video_term_sets)
    majority_game = bool(video_term_sets) and game_video_count * 2 > len(video_term_sets)
    if representative_terms or majority_game:
        matched = representative_terms or sorted({term for terms in video_term_sets for term in terms})
        return {
            "eligible": False,
            "content_scope": "excluded_game",
            "reason": "纯游戏内容不进入每日科技新闻候选",
            "matched_terms": matched,
        }
    return {"eligible": True, "content_scope": "technology", "reason": "通过科技范围门", "matched_terms": []}


def filter_technology_scope(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    eligible: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    for event in events:
        decision = classify_scope(event)
        if decision["eligible"]:
            eligible.append({**event, "content_scope": decision["content_scope"], "scope_reason": decision["reason"]})
        else:
            exclusions.append(
                {
                    "story_id": str(event.get("story_id") or event.get("event_id") or ""),
                    "title": str(event.get("title") or "")[:180],
                    "source_heat_rank": int(event.get("rank") or 0),
                    "heat_score": float(event.get("heat_score") or 0.0),
                    "content_scope": decision["content_scope"],
                    "reason": decision["reason"],
                    "matched_terms": decision["matched_terms"],
                }
            )
    for index, event in enumerate(eligible, start=1):
        event["source_heat_rank"] = int(event.get("rank") or index)
        event["heat_rank"] = index
    return eligible, exclusions


def _dimensions(event: dict[str, Any]) -> tuple[str, int, int, int, dict[str, int], list[str]]:
    corpus = _event_corpus(event)
    policy = _contains_any(corpus, _POLICY_TERMS)
    model = _contains_any(corpus, _MODEL_TERMS)
    model_action = _contains_any(corpus, _MODEL_ACTIONS)
    big_tech = _contains_any(corpus, _BIG_TECH_TERMS)
    strategy = _contains_any(corpus, _STRATEGY_TERMS)
    core = _contains_any(corpus, _CORE_TECH_TERMS)
    consumer = _contains_any(corpus, _CONSUMER_TERMS)
    discussion = _contains_any(corpus, _DISCUSSION_TERMS)
    risk = _contains_any(corpus, _RISK_TERMS)
    tutorial = _contains_any(corpus, _TUTORIAL_PROMOTION_TERMS) or (
        "教程" in corpus and _contains_any(corpus, _TUTORIAL_INSTRUCTION_TERMS)
    )
    vague = _contains_any(corpus, _VAGUE_TERMS)
    abstract = _contains_any(corpus, _ABSTRACT_TERMS)

    category = "general_technology"
    importance = 55
    reasons: list[str] = []
    if policy:
        category, importance = "national_policy", 92
        reasons.append("国家政策或产业行动")
    elif model and model_action:
        category, importance = "major_model_development", 88
        reasons.append("大模型发布、开源、调用或能力变化")
    elif big_tech and strategy:
        category, importance = "major_tech_company_direction", 84
        reasons.append("国际或国内科技大厂方向变化")
    elif core:
        category, importance = "core_technology", 82
        reasons.append("芯片、算力、机器人、航天或基础技术进展")
    elif model:
        category, importance = "model_ecosystem", 74
        reasons.append("大模型生态相关")
    elif consumer:
        category, importance = "consumer_technology", 66
        reasons.append("面向普通用户的科技产品或体验")

    public = 48
    if consumer:
        public = 86
        reasons.append("普通用户可理解或近期可体验")
    elif model and ("开源" in corpus or "本地" in corpus or "免费" in corpus):
        public = 78
        reasons.append("开发者或个人用户可直接使用")
    elif abstract:
        public = 32
        reasons.append("技术意义较高但近期公众关联较弱")
    elif policy:
        public = 58

    discussion_value = 45
    if discussion:
        discussion_value = 78
        reasons.append("具有选择、成本、开放性或用户体验讨论点")
    if risk:
        discussion_value = max(discussion_value, 72)
    if abstract and not discussion:
        discussion_value = 32

    penalties = {
        "risk": 24 if risk else 0,
        "tutorial_promotion": 18 if tutorial else 0,
        "low_specificity": 8 if vague else 0,
    }
    if risk:
        reasons.append("猎奇或未经支持的强主张扣分")
    if tutorial:
        category = "tutorial_or_promotion"
        importance = min(importance, 38)
        reasons.append("教程或营销表达扣分")
    if vague:
        reasons.append("标题具体度不足扣分")
    return category, importance, public, discussion_value, penalties, reasons


def prioritize_for_delivery(events: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Create a deterministic editorial delivery order while preserving raw heat fields."""
    if not events:
        return []
    weights = {key: float(value) for key, value in (settings.get("weights") or {}).items()}
    maximum_heat = max(float(event.get("heat_score") or 0.0) for event in events) or 1.0
    scored: list[dict[str, Any]] = []
    for event in events:
        category, importance, public, discussion, penalties, reasons = _dimensions(event)
        heat_normalized = round(100.0 * float(event.get("heat_score") or 0.0) / maximum_heat, 3)
        weighted = {
            "heat": round(weights["heat"] * heat_normalized, 3),
            "strategic_significance": round(weights["strategic_significance"] * importance, 3),
            "public_relevance": round(weights["public_relevance"] * public, 3),
            "discussion_value": round(weights["discussion_value"] * discussion, 3),
        }
        penalty_total = sum(penalties.values())
        score = round(max(0.0, sum(weighted.values()) - penalty_total), 3)
        scored.append(
            {
                **event,
                "content_category": category,
                "delivery_priority_score": score,
                "priority_components": {
                    "heat_normalized": heat_normalized,
                    "strategic_significance": importance,
                    "public_relevance": public,
                    "discussion_value": discussion,
                    "weighted": weighted,
                    "penalties": penalties,
                    "penalty_total": penalty_total,
                },
                "priority_reasons": reasons or ["普通科技候选"],
            }
        )
    scored.sort(key=lambda row: (-row["delivery_priority_score"], int(row.get("heat_rank") or 9999), str(row.get("event_id") or "")))
    for index, event in enumerate(scored, start=1):
        event["delivery_rank"] = index
        event["rank"] = index
    return scored
