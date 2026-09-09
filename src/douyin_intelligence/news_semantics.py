"""Evidence-bound news semantics for daily Douyin story candidates.

This module decides whether a collected technology story is structurally ready
for a news brief.  It never verifies truth and never affects heat ranking.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any


CONTENT_TYPES = {
    "news_lead",
    "creator_review",
    "creator_experiment",
    "tutorial",
    "opinion",
    "roundup",
    "project_showcase",
    "mixed",
    "uncertain",
}
EVENT_STATUSES = {"released", "announced", "upcoming", "tested", "rumored", "discussed", "unknown"}
NEWS_ACTIONS = ("发布", "开源", "推出", "上线", "更新", "升级", "宣布", "回应", "获批", "完成", "启动", "定档")
TEST_ACTIONS = ("实测", "测试", "体验", "测评", "部署")
VAGUE_PHRASES = ("实测内容", "相关消息", "最新进展", "引发热议", "方案预告", "实测来了", "重磅来袭", "又进化了", "AI圈炸锅", "王炸")
_SPACE = re.compile(r"\s+")
_HASHTAG = re.compile(r"#[^#\s]+")
_NUMBER = re.compile(r"\d+(?:\.\d+)?%?")
_SUBJECT_BEFORE_ACTION = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9·.-]{2,36}?)(?:正式|首次|宣布|预告|计划|正在|准备|将于|将在|发布|推出|开源|更新|升级|上线|回应)"
)
_FOLLOW_SUBJECT = re.compile(r"(?:关注|来自|由)([\u4e00-\u9fffA-Za-z0-9·.-]{2,36})")
_TIME_PATTERNS = (
    re.compile(r"预计(?:在)?(?:这|未来|接下来)?[^，。；\s]{0,12}(?:内|发布|上线)"),
    re.compile(r"(?:将于|将在|计划于|定于)[^，。；\s]{1,18}"),
    re.compile(r"(?:今年|明年|本月|下月|今日|今天|昨日|昨天|近期|本周|下周|两周内|\d{1,2}月\d{0,2}[日号]?)"),
)
_OBJECT_PATTERNS = (
    re.compile(r"([A-Za-z][A-Za-z0-9._-]*(?:\s+[A-Za-z0-9._-]+){0,3}[\u4e00-\u9fff]{0,24}(?:方案|模型|项目|教程|课程|产品|版本|工具|系统|平台))", re.IGNORECASE),
    re.compile(r"([A-Z][A-Za-z0-9._-]{2,60}(?:\s+\d+(?:\.\d+)?)?)"),
    re.compile(r"(《[^》]{2,60}》(?:公益)?(?:教程|课程|项目)?)"),
)
_LEADING_NOISE = re.compile(r"^(?:据视频(?:介绍|内容)?|视频(?:介绍|称)|消息称|据称|这次|最近|目前|关于|欢迎大家|开源预告|重磅|最新)+")
_SUBJECT_HINTS = (
    "公司", "集团", "大学", "学院", "研究院", "实验室", "团队", "科技", "智能", "官方",
    "腾讯", "阿里", "小米", "华为", "百度", "字节", "苹果", "高德", "特斯拉", "小鹏", "智谱",
    "MiniMax", "DeepSeek", "OpenAI", "Google", "Meta", "微软", "英伟达", "Anthropic", "Hugging Face",
)
_KNOWN_SUBJECTS = (
    "腾讯", "阿里", "小米", "华为", "百度", "字节", "苹果", "高德", "特斯拉", "小鹏", "智谱",
    "MiniMax", "DeepSeek", "OpenAI", "Google", "Meta", "微软", "英伟达", "Anthropic", "Hugging Face",
)
_SUBJECT_NOISE = ("必看", "最新", "重磅", "一口气", "合集", "盘点", "玩家", "网友", "消息", "内容", "教程", "模型", "项目", "产品", "方案")
_NON_NEWS_INTENTS = {"creator_review", "creator_experiment", "tutorial", "opinion", "roundup"}


def _clean(value: Any) -> str:
    text = _HASHTAG.sub(lambda match: " " + match.group(0).lstrip("#") + " ", str(value or ""))
    text = text.replace("【", " ").replace("】", " ")
    return _SPACE.sub(" ", text).strip(" ，,。；;：:")


def evidence_text(evidence: list[dict[str, Any]]) -> str:
    return "\n".join(_clean(item.get("text")) for item in evidence if isinstance(item, dict) and _clean(item.get("text")))


def _normalized(value: Any) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(value or "")).casefold()


def _supported(value: str, corpus: str) -> bool:
    needle = _normalized(value)
    return bool(len(needle) >= 2 and needle in _normalized(corpus))


def _content_type(text: str) -> str:
    has_news_action = any(action in text for action in NEWS_ACTIONS)
    if any(word in text for word in ("盘点", "日榜", "周榜", "汇总", "一口气看", "合集", "必看10", "个最新开源仓库")):
        return "roundup"
    if any(word in text for word in ("教程", "入门", "教学", "手把手", "动手学")) and not has_news_action:
        return "tutorial"
    if any(word in text for word in ("怎么看", "我认为", "观点", "锐评", "吐槽")) and not has_news_action:
        return "opinion"
    if any(word in text for word in TEST_ACTIONS) and not has_news_action:
        return "creator_review"
    if has_news_action:
        return "news_lead"
    if any(word in text for word in ("复刻", "项目展示", "作品展示")):
        return "project_showcase"
    return "uncertain"


def _plausible_subject(value: str) -> bool:
    if not value or any(word in value for word in _SUBJECT_NOISE):
        return False
    if any(hint.casefold() in value.casefold() for hint in _SUBJECT_HINTS):
        return True
    if re.fullmatch(r"[A-Z][A-Za-z0-9._-]{2,30}", value):
        return True
    # Newer companies, research groups and product teams cannot all live in a
    # static brand allowlist.  They are still checked against the captured
    # evidence by ``validate_model_semantics`` before this predicate is used.
    if re.fullmatch(r"[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9·._-]{1,35}", value):
        return True
    return False


def _plausible_object(value: str) -> bool:
    cleaned = str(value or "").strip()
    if not cleaned or cleaned[0] in "！!？，,。；;：:、" or len(_normalized(cleaned)) < 2:
        return False
    if _normalized(cleaned) in {_normalized(item) for item in ("模型", "项目", "产品", "工具", "方案", "消息", "内容", "新闻", "AI大模型")}:
        return False
    return True


def _extract_subject(text: str) -> str:
    lowered = text.casefold()
    known = [(lowered.find(hint.casefold()), hint) for hint in _KNOWN_SUBJECTS if lowered.find(hint.casefold()) >= 0]
    if known:
        return min(known, key=lambda item: item[0])[1]
    explicit = [match.group(1) for match in _FOLLOW_SUBJECT.finditer(text)]
    before_action = [match.group(1) for match in _SUBJECT_BEFORE_ACTION.finditer(text)]
    for raw in explicit + before_action:
        value = _LEADING_NOISE.sub("", raw).strip(" ，,。；;：:")
        value = re.split(r"[，。；;：:]", value)[-1]
        if any(word in value for word in ("预计", "两周", "本周", "下周", "今年", "明年", "今日", "昨日", "近期")):
            continue
        if value in NEWS_ACTIONS + TEST_ACTIONS or value in {"预告", "热议", "重磅", "最新"}:
            continue
        if 2 <= len(value) <= 36 and _plausible_subject(value):
            return value
    return ""


def _extract_action(text: str) -> str:
    future = re.search(r"(?:预计|即将|准备|计划|将于|将在)[^，。；]{0,24}?(发布|开源|推出|上线|更新|升级|启动|定档)", text)
    if future:
        return future.group(1)
    positions: list[tuple[int, str]] = []
    for action in NEWS_ACTIONS + TEST_ACTIONS:
        pattern = re.compile(re.escape(action) + (r"(?!会)" if action == "发布" else ""))
        match = pattern.search(text)
        if match:
            positions.append((match.start(), action))
    return min(positions, key=lambda item: item[0])[1] if positions else ""


def _extract_object(text: str, subject: str, action: str) -> str:
    if subject and action:
        # Chinese product/model names commonly appear before the action, for
        # example ``DeepSeek 多模态模型开源``.  Prefer that bounded phrase so a
        # hashtag after the verb cannot become the event object.
        before_action = re.search(
            re.escape(subject)
            + r"\s*([^，。；#!！?？]{2,60}?(?:方案|模型|项目|教程|课程|产品|版本|工具|系统|平台))\s*"
            + re.escape(action),
            text,
        )
        if before_action:
            return before_action.group(1).strip(" ，,。；;：:！!？?").removeprefix("的")
        bounded = re.search(
            re.escape(subject) + r"[^，。；]{0,12}?" + re.escape(action)
            + r"(?:的)?([^，。；#]{2,60}?(?:方案|模型|项目|教程|课程|产品|版本|工具|系统|平台))",
            text,
        )
        if bounded:
            return bounded.group(1).strip(" ，,。；;：:！!？?").removeprefix("的")
        match = re.search(re.escape(subject) + r"[^，。；]{0,12}?" + re.escape(action) + r"([^，。；#]{2,60})", text)
        if match:
            value = re.split(r"(?:并|同时|其中|，)", match.group(1))[0].strip(" ，,。；;：:！!？?")
            if value:
                return value.removeprefix("的")
    for pattern in _OBJECT_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1).strip().removeprefix("的")
    return ""


def _extract_time(text: str) -> str:
    for pattern in _TIME_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0).strip()
    return ""


def _event_status(text: str, action: str) -> str:
    if any(word in text for word in ("传闻", "爆料", "据称", "或将")):
        return "rumored"
    if any(word in text for word in ("预计", "即将", "预告", "准备", "计划", "将于", "将在", "两周内")):
        return "upcoming"
    if action in TEST_ACTIONS:
        return "tested"
    if action in NEWS_ACTIONS:
        return "released" if action in {"发布", "开源", "推出", "上线", "更新", "升级", "完成", "启动"} else "announced"
    if "热议" in text or "讨论" in text:
        return "discussed"
    return "unknown"


def _evidence_refs(evidence: list[dict[str, Any]]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for item in evidence:
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        refs.append({
            "video_id": str(item.get("video_id") or "")[:80],
            "method": str(item.get("method") or "unavailable")[:40],
        })
    return refs


def deterministic_semantics(story: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    corpus = evidence_text(evidence)
    intent_evidence = [
        item for item in evidence
        if str(item.get("method") or "") in {"title", "story_title", "platform_text", "local_asr"}
    ]
    intent_corpus = evidence_text(intent_evidence) or corpus
    source_intent_type = _content_type(intent_corpus)
    subject = _extract_subject(corpus)
    action = _extract_action(corpus)
    object_value = _extract_object(corpus, subject, action)
    time_text = _extract_time(corpus)
    content_type = _content_type(corpus)
    if source_intent_type in _NON_NEWS_INTENTS:
        content_type = source_intent_type
    if content_type == "creator_review" and action == "测试" and subject and object_value and any(
        phrase in corpus for phrase in ("道路测试", "飞行测试", "公开测试", "首次测试", "测试成功")
    ):
        content_type = "news_lead"
    return {
        "content_type": content_type,
        "source_intent_type": source_intent_type,
        "event_slots": {
            "subject": subject,
            "action": action,
            "object": object_value,
            "time_text": time_text,
            "event_status": _event_status(corpus, action),
            "result_or_change": "",
        },
        "event_evidence_refs": _evidence_refs(evidence),
        "semantic_source": "deterministic_evidence",
    }


def validate_model_semantics(item: dict[str, Any], evidence: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate model slots against evidence.  Unsupported slots reject the item."""
    flags: list[str] = []
    content_type = str(item.get("content_type") or "")
    slots = item.get("event_slots")
    if content_type not in CONTENT_TYPES or not isinstance(slots, dict):
        return None, ["invalid_semantic_schema"]
    corpus = evidence_text(evidence)
    normalized = {
        "subject": str(slots.get("subject") or "").strip()[:80],
        "action": str(slots.get("action") or "").strip()[:20],
        "object": str(slots.get("object") or "").strip()[:160],
        "time_text": str(slots.get("time_text") or "").strip()[:80],
        "event_status": str(slots.get("event_status") or "unknown").strip(),
        "result_or_change": str(slots.get("result_or_change") or "").strip()[:300],
    }
    if normalized["event_status"] not in EVENT_STATUSES:
        return None, ["invalid_event_status"]
    for field in ("subject", "object", "time_text"):
        if normalized[field] and not _supported(normalized[field], corpus):
            flags.append(f"unsupported_{field}")
    if normalized["subject"] and not _plausible_subject(normalized["subject"]):
        flags.append("implausible_subject")
    if normalized["object"] and not _plausible_object(normalized["object"]):
        flags.append("implausible_object")
    action = normalized["action"]
    if action and action not in NEWS_ACTIONS + TEST_ACTIONS:
        flags.append("unsupported_action")
    elif action and action not in corpus:
        flags.append("unsupported_action")
    output_numbers = set(_NUMBER.findall(" ".join(str(value) for value in normalized.values())))
    if output_numbers - set(_NUMBER.findall(corpus)):
        flags.append("unsupported_number")
    if flags:
        return None, flags
    return {
        "content_type": content_type,
        "event_slots": normalized,
        "event_evidence_refs": _evidence_refs(evidence),
        "semantic_source": "llm_evidence_slots",
    }, []


def _headline(slots: dict[str, Any]) -> str:
    subject = str(slots.get("subject") or "").strip()
    action = str(slots.get("action") or "").strip()
    object_value = str(slots.get("object") or "").strip()
    time_text = str(slots.get("time_text") or "").strip()
    prefix = subject
    if time_text and time_text not in prefix:
        prefix += time_text
    if action and action not in prefix:
        prefix += action
    if object_value and object_value not in prefix:
        prefix += object_value
    return prefix[:160]


def finalize_story_semantics(story: dict[str, Any]) -> dict[str, Any]:
    slots = story.get("event_slots") if isinstance(story.get("event_slots"), dict) else {}
    present = [field for field in ("subject", "action", "object", "time_text") if str(slots.get(field) or "").strip()]
    missing = [field for field in ("subject", "action", "object") if field not in present]
    score = sum({"subject": 30, "action": 30, "object": 30, "time_text": 10}[field] for field in present)
    content_type = str(story.get("content_type") or "uncertain")
    source_intent_type = str(story.get("source_intent_type") or "uncertain")
    if source_intent_type in _NON_NEWS_INTENTS:
        content_type = source_intent_type
    corpus = evidence_text(story.get("content_evidence") or [])
    named_subjects = {hint for hint in _KNOWN_SUBJECTS if hint.casefold() in corpus.casefold()}
    dated_sections = len(re.findall(r"(?:\d{1,2}[./月-]\d{1,2}|今日|昨日|今天|昨天)", corpus))
    action_mentions = sum(corpus.count(action) for action in NEWS_ACTIONS)
    if len(named_subjects) >= 2 and (dated_sections >= 2 or action_mentions >= 3):
        content_type = "roundup"
    action = str(slots.get("action") or "")
    headline = _headline(slots)
    flags = [f"missing_{field}" for field in missing]
    flags.extend(f"vague_phrase:{phrase}" for phrase in VAGUE_PHRASES if phrase in headline)
    refs = story.get("event_evidence_refs") if isinstance(story.get("event_evidence_refs"), list) else []
    if not refs:
        flags.append("missing_evidence_refs")
    news_like = content_type == "news_lead" or (content_type == "project_showcase" and action in NEWS_ACTIONS)
    ready = news_like and not missing and bool(refs) and not any(flag.startswith("vague_phrase:") for flag in flags)
    if ready:
        readiness = "ready"
    elif content_type in {"creator_review", "creator_experiment", "tutorial", "opinion", "roundup"}:
        readiness = "not_news"
    elif refs and any(str(item.get("method") or "") == "title" for item in story.get("content_evidence") or []):
        readiness = "needs_enrichment"
    else:
        readiness = "insufficient"
    story["content_type"] = content_type if content_type in CONTENT_TYPES else "uncertain"
    story["event_completeness"] = {
        "score": score,
        "required_present": [field for field in present if field != "time_text"],
        "missing": missing,
        "quality_flags": flags,
    }
    story["news_readiness"] = readiness
    story["news_headline"] = headline if ready else ""
    story["headline_quality_flags"] = [] if ready else flags
    return story


def semantic_detail_present(text: str) -> bool:
    probe = {"story_id": "probe"}
    evidence = [{"video_id": "probe", "method": "platform_text", "text": text}]
    probe.update(deterministic_semantics(probe, evidence))
    finalize_story_semantics(probe)
    return probe["news_readiness"] == "ready"


def semantic_counts(stories: list[dict[str, Any]]) -> dict[str, Any]:
    readiness = Counter(str(item.get("news_readiness") or "legacy_missing") for item in stories)
    content_types = Counter(str(item.get("content_type") or "legacy_missing") for item in stories)
    headline_failures = sum(
        bool(item.get("news_readiness") == "ready" and item.get("headline_quality_flags")) for item in stories
    )
    return {
        "news_readiness": dict(sorted(readiness.items())),
        "content_types": dict(sorted(content_types.items())),
        "news_ready": int(readiness.get("ready", 0)),
        "headline_quality_failures": int(headline_failures),
    }
