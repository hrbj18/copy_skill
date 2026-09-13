"""Deterministic, high-precision story consolidation for Douyin discovery rows.

Douyin metadata is discovery/attention evidence, not factual evidence.  This
module only decides whether public posts appear to discuss the same event.  It
deliberately prefers a related-but-separate result over a broad-topic merge.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Any


REL_SAME = "same_story"
REL_RELATED = "related_topic"
REL_DIFFERENT = "different_story"

_HASHTAG = re.compile(r"#([^#\s]+)")
_BOOK_TITLE = re.compile(r"《([^》]{2,40})》")
_LATIN_TOKEN = re.compile(r"[a-z][a-z0-9]*(?:[._+-][a-z0-9]+)*", re.I)
_MODEL_WITH_VERSION = re.compile(r"\b([a-z][a-z0-9]{1,20})\s*[- ]?\s*(\d+(?:\.\d+){0,3}[a-z]*)\b", re.I)
_DATE = re.compile(r"(?<!\d)(?:20\d{2}[年./-])?\d{1,2}[月./-]\d{1,2}(?:日|号)?(?!\d)")
_SAFE_TEXT = re.compile(r"[^0-9a-z\u3400-\u9fff]+", re.I)

_ACTIONS = (
    "正式发布", "发布", "推出", "开源", "官宣", "定档", "上线", "发售", "首发", "亮相",
    "升级", "更新", "推送", "实测", "测评", "试玩", "体验", "回应", "曝光", "泄露", "获批",
)

_GENERIC_TAGS = {
    "ai", "人工智能", "科技", "科技快讯", "新闻", "热点", "模型", "大模型", "国产大模型",
    "开源大模型", "开源", "游戏", "游戏推荐", "新游推荐", "科普", "抖音精选", "热门",
    "程序员", "创作者大会", "流量扶持", "dou+小助手", "ai新星计划", "agent", "ai大模型", "多智能体",
    "github", "github项目", "github开源", "开源项目", "ai工具", "vibecoding", "skill", "skills",
    "媒体原创", "媒体精选计划", "正点财经", "抖音前沿科技首发计划", "我在抖音聊科技",
}
_GENERIC_LATIN = {
    "ai", "app", "api", "new", "news", "preview", "pro", "plus", "max", "ultra", "flash",
    "video", "reaction", "agent", "mmo", "rpg", "pvp", "pve", "pvpve", "steam",
    "github", "skill", "skills", "vibecoding",
}
_ENTITY_ONLY_LATIN = {
    "openai", "anthropic", "tencent", "alibaba", "zhipu", "huggingface", "minimax", "bytedance",
    "baidu", "google", "microsoft", "apple", "huawei", "xiaomi", "xpeng", "netease", "pantum",
    "deepseek", "codex",
}
_GENERIC_CHINESE = {
    "最新", "消息", "新品", "新作", "来了", "终于", "正式", "全新", "特别", "大家", "怎么",
    "为何", "为什么", "到底", "目前", "国内", "国产", "全球", "科技", "模型", "大模型",
}
_TAG_SUFFIXES = (
    "正式发布", "发布", "最新消息", "消息", "正式开源", "开源", "官宣定档", "定档", "官宣",
    "上线", "发售", "测试", "试玩", "测评", "实测", "游戏", "新作", "新品", "开源大模型",
    "大模型", "模型", "芯片", "硬件", "热点", "推荐",
)
_BROAD_TOPICS = {
    "开放世界", "技术突破", "国产", "科技", "新闻", "热点", "搞笑", "杂谈", "推荐", "ai编程",
    "人工智能", "程序员", "模型", "大模型", "芯片", "硬件", "游戏", "新游", "国产科技",
}
_BROAD_SUBJECT_MARKERS = ("ai", "模型", "芯片", "硬件", "游戏", "机器人", "智能体", "编程", "开源项目")

# Entity aliases are only conflict/relationship signals.  They never confer
# trust and are intentionally conservative: unknown subjects rely on hashtags,
# quoted product names and model/version anchors instead.
_ENTITY_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("腾讯", ("腾讯", "tencent")),
    ("阿里", ("阿里巴巴", "阿里", "alibaba", "qwen", "千问")),
    ("智谱", ("智谱", "zhipu", "glm")),
    ("小米", ("小米", "xiaomi", "澎湃os", "hyperos")),
    ("网易", ("网易", "netease")),
    ("华为", ("华为", "huawei", "鸿蒙", "harmonyos")),
    ("小鹏", ("小鹏", "xpeng")),
    ("openai", ("openai", "chatgpt", "codex")),
    ("deepseek", ("deepseek",)),
    ("anthropic", ("anthropic", "claude")),
    ("huggingface", ("hugging face", "huggingface")),
    ("minimax", ("minimax", "稀宇")),
    ("奔图", ("奔图", "pantum")),
    ("字节跳动", ("字节跳动", "字节", "bytedance", "豆包")),
    ("百度", ("百度", "baidu", "文心")),
    ("谷歌", ("谷歌", "google", "gemini")),
    ("微软", ("微软", "microsoft")),
    ("苹果", ("苹果", "apple")),
)
_ENTITY_TOPIC_NAMES = {
    _SAFE_TEXT.sub("", value.casefold())
    for canonical, aliases in _ENTITY_ALIASES
    for value in (canonical, *aliases)
}


def _normalized(value: str) -> str:
    value = _HASHTAG.sub(" ", str(value or "")).casefold()
    value = re.sub(r"https?://\S+|@\S+", " ", value)
    return _SAFE_TEXT.sub("", value)


def _display_title(value: str) -> str:
    text = _HASHTAG.sub(" ", str(value or ""))
    text = re.sub(r"https?://\S+|@\S+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ，,。.!！?？")
    first = re.split(r"(?<=[。！？!?])\s+", text, maxsplit=1)[0]
    return (first or text)[:180]


def _base_topic(value: str) -> str:
    topic = _SAFE_TEXT.sub("", value.casefold())
    changed = True
    while changed and len(topic) >= 3:
        changed = False
        for suffix in _TAG_SUFFIXES:
            normalized_suffix = _SAFE_TEXT.sub("", suffix.casefold())
            if topic.endswith(normalized_suffix) and len(topic) - len(normalized_suffix) >= 2:
                topic = topic[: -len(normalized_suffix)]
                changed = True
                break
    return topic


def _specific_subject(value: str) -> bool:
    if not value or value in _GENERIC_TAGS or value in _BROAD_TOPICS or value in _ENTITY_TOPIC_NAMES:
        return False
    if any(character.isdigit() for character in value):
        return True
    if len(value) <= 6 and any(marker in value for marker in _BROAD_SUBJECT_MARKERS):
        return False
    return not any(marker in value for marker in ("搞笑", "杂谈", "推荐", "流量", "热点", "技术突破"))


def _chinese_bigrams(value: str) -> set[str]:
    result: set[str] = set()
    for piece in re.findall(r"[\u3400-\u9fff]{2,}", value):
        if piece in _GENERIC_CHINESE:
            continue
        result.update(piece[index:index + 2] for index in range(len(piece) - 1))
    return result


def build_event_signature(title: str) -> dict[str, Any]:
    """Build an explainable signature without using discovery keywords."""
    original = str(title or "")
    lowered = original.casefold()
    normalized = _normalized(original)
    tags: set[str] = set()
    subjects: set[str] = set()
    for raw in _HASHTAG.findall(original):
        full = _SAFE_TEXT.sub("", raw.casefold())
        base = _base_topic(raw)
        if full and full not in _GENERIC_TAGS:
            tags.add(full)
        if len(base) >= 2 and _specific_subject(base):
            subjects.add(base)
    for raw in _BOOK_TITLE.findall(original):
        subject = _base_topic(raw)
        if _specific_subject(subject):
            subjects.add(subject)

    models: set[str] = set()
    for prefix, version in _MODEL_WITH_VERSION.findall(lowered):
        value = _SAFE_TEXT.sub("", f"{prefix}{version}")
        if value and value not in _GENERIC_LATIN:
            models.add(value)
    latin = {_SAFE_TEXT.sub("", token.casefold()) for token in _LATIN_TOKEN.findall(lowered)}
    for token in latin:
        if not token or token in _GENERIC_LATIN:
            continue
        if any(character.isdigit() for character in token) or len(token) >= 4:
            models.add(token)

    entities = {
        canonical
        for canonical, aliases in _ENTITY_ALIASES
        if any(alias.casefold() in lowered for alias in aliases)
    }
    models.difference_update(_ENTITY_ONLY_LATIN)
    actions = {action for action in _ACTIONS if action in original}
    dates = {_SAFE_TEXT.sub("", value.casefold()) for value in _DATE.findall(original)}
    lexical = _chinese_bigrams(_HASHTAG.sub(" ", lowered)) | {
        token for token in latin if token not in _GENERIC_LATIN and len(token) >= 3
    }
    strong = set(subjects) | set(models)
    return {
        "normalized_text": normalized[:1000],
        "display_title": _display_title(original),
        "subjects": sorted(subjects),
        "model_anchors": sorted(models),
        "entities": sorted(entities),
        "actions": sorted(actions),
        "dates": sorted(dates),
        "topic_tags": sorted(tags),
        "strong_anchors": sorted(strong),
        "lexical_tokens": sorted(lexical),
    }


def _sets(signature: dict[str, Any], key: str) -> set[str]:
    return {str(value) for value in signature.get(key) or [] if str(value)}


def classify_story_relation(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Classify two signatures, with named conflicts taking precedence."""
    left_models, right_models = _sets(left, "model_anchors"), _sets(right, "model_anchors")
    left_subjects, right_subjects = _sets(left, "subjects"), _sets(right, "subjects")
    left_entities, right_entities = _sets(left, "entities"), _sets(right, "entities")
    left_dates, right_dates = _sets(left, "dates"), _sets(right, "dates")
    shared_models = left_models & right_models
    shared_subjects = left_subjects & right_subjects
    shared_entities = left_entities & right_entities
    shared_dates = left_dates & right_dates

    conflicting_models = bool(left_models and right_models and not shared_models)
    # A shared explicit product name can survive an extra publisher/company
    # mention, but two disjoint named entities without a shared product cannot.
    conflicting_entities = bool(left_entities and right_entities and not shared_entities and not shared_subjects and not shared_models)
    if conflicting_models:
        return {
            "relation": REL_DIFFERENT, "confidence": "high", "reason": "conflicting_model_anchors",
            "positive_anchors": [], "negative_anchors": sorted(left_models | right_models),
        }
    if conflicting_entities:
        return {
            "relation": REL_DIFFERENT, "confidence": "high", "reason": "conflicting_named_entities",
            "positive_anchors": [], "negative_anchors": sorted(left_entities | right_entities),
        }
    if shared_models:
        smaller_anchor_count = min(len(left_models), len(right_models))
        overlap_ratio = len(shared_models) / max(1, smaller_anchor_count)
        if smaller_anchor_count >= 3 and len(shared_models) < 2 and overlap_ratio < 0.5:
            return {
                "relation": REL_RELATED, "confidence": "medium", "reason": "single_item_overlap_in_multi_item_topic",
                "positive_anchors": sorted(shared_models), "negative_anchors": [],
            }
        return {
            "relation": REL_SAME, "confidence": "high", "reason": "shared_model_anchor",
            "positive_anchors": sorted(shared_models), "negative_anchors": [],
        }
    if shared_subjects:
        return {
            "relation": REL_SAME, "confidence": "high", "reason": "shared_named_subject",
            "positive_anchors": sorted(shared_subjects), "negative_anchors": [],
        }

    left_text = str(left.get("normalized_text") or "")
    right_text = str(right.get("normalized_text") or "")
    if shared_entities and shared_dates:
        left_residual, right_residual = left_text, right_text
        for value in sorted(shared_entities | shared_dates, key=len, reverse=True):
            normalized_value = _SAFE_TEXT.sub("", value.casefold())
            left_residual = left_residual.replace(normalized_value, "")
            right_residual = right_residual.replace(normalized_value, "")
        match = SequenceMatcher(None, left_residual, right_residual).find_longest_match()
        if match.size >= 3:
            shared_phrase = left_residual[match.a:match.a + match.size]
            return {
                "relation": REL_SAME, "confidence": "medium", "reason": "shared_entity_date_specific_phrase",
                "positive_anchors": sorted(shared_entities | shared_dates | {shared_phrase}), "negative_anchors": [],
            }
    similarity = SequenceMatcher(None, left_text, right_text).ratio() if left_text and right_text else 0.0
    left_lexical, right_lexical = _sets(left, "lexical_tokens"), _sets(right, "lexical_tokens")
    overlap = left_lexical & right_lexical
    union = left_lexical | right_lexical
    jaccard = len(overlap) / max(1, len(union))
    if similarity >= 0.68 or (len(overlap) >= 5 and jaccard >= 0.52) or (shared_entities and len(overlap) >= 4 and jaccard >= 0.22):
        return {
            "relation": REL_SAME, "confidence": "medium", "reason": "high_text_similarity",
            "positive_anchors": sorted(overlap)[:12], "negative_anchors": [],
            "text_similarity": round(similarity, 4), "token_jaccard": round(jaccard, 4),
        }
    if shared_entities or len(overlap) >= 2:
        return {
            "relation": REL_RELATED, "confidence": "medium" if shared_entities else "low",
            "reason": "shared_entity_only" if shared_entities else "broad_topic_overlap",
            "positive_anchors": sorted(shared_entities or overlap)[:12], "negative_anchors": [],
            "text_similarity": round(similarity, 4), "token_jaccard": round(jaccard, 4),
        }
    return {
        "relation": REL_DIFFERENT, "confidence": "medium", "reason": "insufficient_shared_identity",
        "positive_anchors": [], "negative_anchors": [],
        "text_similarity": round(similarity, 4), "token_jaccard": round(jaccard, 4),
    }


def _cluster_signature(signatures: list[dict[str, Any]]) -> dict[str, Any]:
    count = max(1, len(signatures))
    counters = {
        key: Counter(value for signature in signatures for value in signature.get(key) or [])
        for key in ("subjects", "model_anchors", "entities", "actions", "dates", "topic_tags")
    }
    identity_counter = counters["subjects"] + counters["model_anchors"]
    identity = [value for value, seen in identity_counter.most_common() if seen * 2 >= count]
    if not identity:
        identity = [value for value, _seen in identity_counter.most_common(3)]
    if not identity:
        identity = [value for value, seen in counters["entities"].most_common() if seen * 2 >= count]
    if not identity:
        fallback = sorted(str(signature.get("normalized_text") or "") for signature in signatures if signature.get("normalized_text"))
        identity = fallback[:1]
    display_options = sorted(
        (str(signature.get("display_title") or "") for signature in signatures if signature.get("display_title")),
        key=lambda value: (-min(len(value), 120), value),
    )
    return {
        "identity_anchors": identity[:8],
        "subjects": sorted(counters["subjects"]),
        "model_anchors": sorted(counters["model_anchors"]),
        "entities": sorted(counters["entities"]),
        "actions": sorted(counters["actions"]),
        "dates": sorted(counters["dates"]),
        "topic_tags": sorted(counters["topic_tags"]),
        "display_title": display_options[0] if display_options else "科技热点",
    }


def _story_id(business_date: str, signature: dict[str, Any]) -> str:
    material = business_date + "|" + "|".join(sorted(str(value) for value in signature["identity_anchors"]))
    return "story-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def consolidate_story_videos(videos: list[dict[str, Any]], business_date: str) -> list[dict[str, Any]]:
    """Return order-independent, non-chaining story groups for unique videos."""
    prepared = [
        {"video": video, "signature": build_event_signature(str(video.get("title") or ""))}
        for video in videos
    ]
    prepared.sort(key=lambda item: (str(item["video"].get("video_id") or ""), str(item["video"].get("title") or "")))
    clusters: list[dict[str, Any]] = []
    for item in prepared:
        selected: dict[str, Any] | None = None
        selected_relation: dict[str, Any] | None = None
        for cluster in clusters:
            anchor_relation = classify_story_relation(item["signature"], cluster["anchor_signature"])
            if anchor_relation["relation"] != REL_SAME:
                continue
            member_relations = [classify_story_relation(item["signature"], member["signature"]) for member in cluster["members"]]
            if any(relation["relation"] == REL_DIFFERENT for relation in member_relations):
                continue
            selected, selected_relation = cluster, anchor_relation
            break
        if selected is None:
            clusters.append({
                "anchor_signature": item["signature"],
                "anchor_video_id": str(item["video"].get("video_id") or ""),
                "members": [item],
                "decisions": [{
                    "video_id": str(item["video"].get("video_id") or ""),
                    "relation": REL_SAME, "confidence": "self", "reason": "cluster_anchor",
                    "positive_anchors": item["signature"].get("strong_anchors") or [], "negative_anchors": [],
                }],
            })
        else:
            selected["members"].append(item)
            selected["decisions"].append({"video_id": str(item["video"].get("video_id") or ""), **(selected_relation or {})})

    results: list[dict[str, Any]] = []
    for cluster in clusters:
        signatures = [item["signature"] for item in cluster["members"]]
        signature = _cluster_signature(signatures)
        decisions = cluster["decisions"]
        confidence = "single_video" if len(decisions) == 1 else (
            "high" if all(item.get("confidence") in {"high", "self"} for item in decisions) else "medium"
        )
        results.append({
            "story_id": _story_id(business_date, signature),
            "event_signature": signature,
            "videos": [item["video"] for item in cluster["members"]],
            "clustering_basis": "named-anchor-nonchaining-v1",
            "clustering_confidence": confidence,
            "clustering_decisions": decisions,
            "related_story_ids": [],
            "related_story_relations": [],
        })

    # Related-topic edges are explanatory only and never merge events.
    by_id = {item["story_id"]: item for item in results}
    for index, left in enumerate(results):
        left_signature = build_event_signature(str(left["event_signature"].get("display_title") or ""))
        # Retain aggregate named anchors that may have been present only in a
        # hashtag/model field and not in the selected display title.
        for key in ("subjects", "model_anchors", "entities"):
            left_signature[key] = list(left["event_signature"].get(key) or [])
        for right in results[index + 1:]:
            right_signature = build_event_signature(str(right["event_signature"].get("display_title") or ""))
            for key in ("subjects", "model_anchors", "entities"):
                right_signature[key] = list(right["event_signature"].get(key) or [])
            relation = classify_story_relation(left_signature, right_signature)
            if relation["relation"] != REL_RELATED:
                continue
            left["related_story_ids"].append(right["story_id"])
            right["related_story_ids"].append(left["story_id"])
            left["related_story_relations"].append({"story_id": right["story_id"], **relation})
            right["related_story_relations"].append({"story_id": left["story_id"], **relation})
    for item in by_id.values():
        item["related_story_ids"] = sorted(set(item["related_story_ids"]))
        item["related_story_relations"] = sorted(item["related_story_relations"], key=lambda row: row["story_id"])
    return sorted(results, key=lambda item: item["story_id"])


def display_title(value: str) -> str:
    """Public display-title helper shared by deterministic enrichment."""
    return _display_title(value)
