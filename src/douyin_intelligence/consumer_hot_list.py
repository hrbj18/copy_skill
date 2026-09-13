"""Evidence-first consumer cards for the immutable Douyin heat ranking.

This module deliberately has no crawler, web search or ranking code.  It turns
already captured story evidence into a short reader-facing card only after the
story has a supported subject, action and object.  The machine contract keeps
all provenance, while the Markdown renderer stays concise.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable


_NUMBER = re.compile(r"\d+(?:\.\d+)?%?")
_CHINESE = re.compile(r"[\u4e00-\u9fff]")
_META = re.compile(r"(?:据(?:视频|输入|来源|平台)|来源(?:显示|称|标注)?|原文(?:称|显示)?|抖音|热度|真实性|模型(?:生成|整理)|AI(?:生成|整理))")
_URL = re.compile(r"https?://|www\.", re.IGNORECASE)


def _normalized(value: Any) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(value or "")).casefold()


def _text(value: Any, maximum: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:maximum]


def _detail_evidence(story: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    fingerprints: set[str] = set()
    for item in story.get("content_evidence") or []:
        if not isinstance(item, dict):
            continue
        text = _text(item.get("text"), 1_200)
        method = _text(item.get("method"), 40) or "unavailable"
        fingerprint = _normalized(text)
        if len(fingerprint) < 12 or fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        rows.append({
            "video_id": _text(item.get("video_id"), 80),
            "method": method,
            "text": text,
        })
    return rows


def _fact_card(story: dict[str, Any]) -> dict[str, Any]:
    """Build a deterministic, audit-friendly eligibility record.

    A title alone never qualifies.  We accept either one non-title detailed
    evidence item (platform text, OCR or ASR) or two independent evidence
    texts, then require a supported SVO event frame from story enrichment.
    """
    slots = story.get("event_slots") if isinstance(story.get("event_slots"), dict) else {}
    subject = _text(slots.get("subject"), 80)
    action = _text(slots.get("action"), 20)
    object_value = _text(slots.get("object"), 160)
    evidence = _detail_evidence(story)
    methods = {item["method"] for item in evidence}
    detailed = [item for item in evidence if item["method"] not in {"title", "story_title", "unavailable"} and len(_normalized(item["text"])) >= 30]
    reasons: list[str] = []
    for name, value in (("subject", subject), ("action", action), ("object", object_value)):
        if not value:
            reasons.append(f"missing_{name}")
    if not detailed and len(evidence) < 2:
        reasons.append("title_only_evidence")
    source = {
        "story_id": _text(story.get("story_id") or story.get("event_id"), 100),
        "heat_rank": int(story.get("heat_rank") or story.get("rank") or 0),
        "original_title": _text(story.get("title") or story.get("canonical_title"), 300),
        "subject": subject,
        "action": action,
        "object": object_value,
        "event_summary": _text(story.get("event_summary"), 600),
        "key_points": [_text(item, 240) for item in story.get("key_points") or [] if _text(item, 240)][:4],
        "result_or_change": _text(slots.get("result_or_change"), 300),
        "evidence": evidence[:3],
    }
    return {
        "status": "ready" if not reasons else "insufficient",
        "reasons": reasons,
        "fact": source,
        "evidence_coverage": {
            "evidence_count": len(evidence),
            "detailed_evidence_count": len(detailed),
            "methods": sorted(methods),
        },
    }


def _valid_card(item: dict[str, Any], fact: dict[str, Any]) -> tuple[dict[str, str] | None, str | None]:
    title = _text(item.get("consumer_title"), 56)
    summary = _text(item.get("consumer_summary"), 180)
    if not title or not summary or not _CHINESE.search(title + summary):
        return None, "missing_chinese_text"
    if _URL.search(title + summary) or _META.search(title + summary):
        return None, "consumer_meta_language"
    if title.endswith(("。", "！", "!", "？", "?")) or "\n" in title:
        return None, "invalid_title_shape"
    subject = _normalized(fact.get("subject"))
    action = _normalized(fact.get("action"))
    object_value = _normalized(fact.get("object"))
    card_text = _normalized(title + summary)
    if subject not in card_text or action not in card_text or object_value not in card_text:
        return None, "missing_fact_anchor"
    original = _normalized(fact.get("original_title"))
    if original and _normalized(title) == original:
        return None, "raw_hook_title_reused"
    output_numbers = set(_NUMBER.findall(title + summary))
    input_numbers = set(_NUMBER.findall(json.dumps(fact, ensure_ascii=False)))
    if output_numbers - input_numbers:
        return None, "unsupported_number"
    return {"title": title, "summary": summary}, None


def build_consumer_hot_list(
    stories: list[dict[str, Any]],
    *,
    maximum: int,
    batch_size: int,
    max_output_tokens: int,
    generate: Callable[[str, str, int], dict[str, Any]] | None,
    deadline: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Attach consumer cards to all qualifying stories without changing rank.

    The input pool can contain hot tutorials and empty hooks.  Screening the
    full pool before taking the twenty hottest qualified events preserves heat
    order while avoiding a misleading sparse top-twenty document.
    """
    ranked = sorted(stories, key=lambda row: (int(row.get("heat_rank") or row.get("rank") or 10**9), str(row.get("story_id") or "")))
    all_eligible: list[dict[str, Any]] = []
    for story in ranked:
        state = _fact_card(story)
        story["consumer_fact_card"] = state
        story["consumer_card"] = {"status": "insufficient", "reason": "fact_card_insufficient"}
        if state["status"] == "ready":
            all_eligible.append(state["fact"])
    eligible = all_eligible[:maximum]
    selected_ids = {item["story_id"] for item in eligible}
    by_id: dict[str, dict[str, Any]] = {}
    for story in ranked:
        story_id = _text(story.get("story_id") or story.get("event_id"), 100)
        if story_id in selected_ids:
            by_id[story_id] = story
        elif story.get("consumer_fact_card", {}).get("status") == "ready":
            story["consumer_card"] = {"status": "eligible_not_selected", "reason": "lower_heat_than_top_twenty_qualified"}
    cards: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "target_count": maximum,
        "screened_count": len(ranked),
        "selected_count": len(eligible),
        "fact_ready_count": len(all_eligible),
        "consumer_card_count": 0,
        "attempted_batches": 0,
        "repair_attempts": 0,
        "successful_batches": 0,
        "rejected_items": 0,
        "errors": [],
        "missing_count": 0,
    }
    if generate is None:
        report["errors"].append("model_unavailable")
    else:
        for start in range(0, len(eligible), max(1, batch_size)):
            if time.monotonic() >= deadline:
                report["errors"].append("global_deadline_exhausted")
                break
            batch = eligible[start:start + max(1, batch_size)]
            system = (
                "你是中文科技新闻编辑。你只可根据每条给出的事实卡写一条短新闻，不联网、不核真、不添加输入以外的事实。"
                "标题必须把主体和动作说清楚，摘要补足对象或关键变化。语言直接自然，像认真编辑写给普通读者的科技热榜，"
                "不用来源口吻、宣传套话、原话、链接、热度、真实性说明或模型说明。标题不可照抄原始视频标题。"
                "每条严格返回 consumer_title 和 consumer_summary，输出 JSON。"
            )
            prompt = "每个 story_id 必须原样返回且只出现一次。输出 {\"items\":[{\"story_id\":\"\",\"consumer_title\":\"\",\"consumer_summary\":\"\"}]}。\n" + json.dumps(batch, ensure_ascii=False)
            report["attempted_batches"] += 1
            try:
                payload = generate(system, prompt, max_output_tokens)
            except Exception as exc:
                report["errors"].append(type(exc).__name__)
                continue
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                report["errors"].append("invalid_items")
                continue
            applied = 0
            seen: set[str] = set()
            batch_facts = {item["story_id"]: item for item in batch}
            for item in items:
                if not isinstance(item, dict):
                    report["rejected_items"] += 1
                    continue
                story_id = _text(item.get("story_id"), 100)
                if story_id not in batch_facts or story_id in seen:
                    report["rejected_items"] += 1
                    continue
                card, reason = _valid_card(item, batch_facts[story_id])
                if card is None:
                    report["rejected_items"] += 1
                    by_id[story_id]["consumer_card"] = {"status": "rejected", "reason": reason}
                    continue
                by_id[story_id]["consumer_card"] = {"status": "success", **card}
                cards.append({"story_id": story_id, "heat_rank": batch_facts[story_id]["heat_rank"], **card})
                seen.add(story_id)
                applied += 1
            report["successful_batches"] += int(applied > 0)
        # A constrained repair pass is cheaper and safer than silently
        # falling back to a raw title.  It only sees the same fact cards and
        # must still pass every deterministic field and number check.
        repair_ids = [
            fact["story_id"]
            for fact in eligible
            if by_id[fact["story_id"]].get("consumer_card", {}).get("status") == "rejected"
        ]
        if repair_ids and time.monotonic() < deadline:
            repair_facts = [fact for fact in eligible if fact["story_id"] in repair_ids]
            system = (
                "你是中文科技新闻编辑。请修复下列新闻卡。标题和摘要合计必须完整保留事实卡中的 subject、action、object，"
                "不得补充任何输入没有的公司、数字、时间或结论。写成普通读者一眼能懂的简洁中文新闻，严格输出 JSON。"
            )
            prompt = "每个 story_id 必须原样返回且只出现一次。输出 {\"items\":[{\"story_id\":\"\",\"consumer_title\":\"\",\"consumer_summary\":\"\"}]}。\n" + json.dumps(repair_facts, ensure_ascii=False)
            report["repair_attempts"] = 1
            report["attempted_batches"] += 1
            try:
                payload = generate(system, prompt, max_output_tokens)
                items = payload.get("items") if isinstance(payload, dict) else None
                seen: set[str] = set()
                facts_by_id = {fact["story_id"]: fact for fact in repair_facts}
                if isinstance(items, list):
                    for item in items:
                        story_id = _text(item.get("story_id") if isinstance(item, dict) else "", 100)
                        if not isinstance(item, dict) or story_id not in facts_by_id or story_id in seen:
                            report["rejected_items"] += 1
                            continue
                        card, reason = _valid_card(item, facts_by_id[story_id])
                        if card is None:
                            report["rejected_items"] += 1
                            by_id[story_id]["consumer_card"] = {"status": "rejected", "reason": reason}
                            continue
                        by_id[story_id]["consumer_card"] = {"status": "success", **card}
                        cards.append({"story_id": story_id, "heat_rank": facts_by_id[story_id]["heat_rank"], **card})
                        seen.add(story_id)
                else:
                    report["errors"].append("invalid_repair_items")
            except Exception as exc:
                report["errors"].append(type(exc).__name__)
    report["consumer_card_count"] = len(cards)
    report["missing_count"] = max(0, maximum - len(cards))
    report["status"] = "success" if len(eligible) >= maximum and len(cards) >= maximum else "partial"
    return stories, report


def render_consumer_hot_list(pack: dict[str, Any]) -> str:
    """Render only reader-facing cards; traceability remains in JSON."""
    public = pack.get("public_reader_hot_list") if isinstance(pack.get("public_reader_hot_list"), dict) else {}
    public_cards = public.get("cards") if isinstance(public.get("cards"), list) else []
    if public_cards:
        lines = ["# 每日科技热榜", ""]
        for ordinal, card in enumerate(public_cards[: int(public.get("target_count") or 20)], start=1):
            if not isinstance(card, dict):
                continue
            title = _text(card.get("title"), 80)
            summary = _text(card.get("summary"), 280)
            if title and summary:
                lines.extend([f"## {ordinal}. {title}", "", summary, ""])
        return "\n".join(lines) if len(lines) > 2 else "# 每日科技热榜\n\n暂无具备完整内容证据的热点。\n"
    stories = pack.get("candidates") if isinstance(pack.get("candidates"), list) else []
    maximum = int(((pack.get("consumer_hot_list") or {}).get("target_count")) or 20)
    ranked = sorted(stories, key=lambda row: (int(row.get("heat_rank") or row.get("rank") or 10**9), str(row.get("story_id") or "")))
    rows = [row for row in ranked if isinstance(row.get("consumer_card"), dict) and row["consumer_card"].get("status") == "success"][:maximum]
    lines = ["# 每日科技热榜", ""]
    if not rows:
        return "# 每日科技热榜\n\n暂无具备完整内容证据的热点。\n"
    for ordinal, story in enumerate(rows, start=1):
        card = story["consumer_card"]
        lines.extend([f"## {ordinal}. {card['title']}", "", str(card["summary"]), ""])
    return "\n".join(lines)


def build_public_reader_hot_list(events: list[dict[str, Any]], *, maximum: int = 20) -> dict[str, Any]:
    """Select attributed public-event cards without changing raw Douyin heat.

    A same-day query-scoped Douyin signal orders public events when available.
    When there is no matching signal, the separately audited company-event
    delivery preference and attributed source importance provide a deterministic
    fallback.  The original Douyin candidate heat remains in its own machine
    contract and is never overwritten here.
    """
    candidates: list[dict[str, Any]] = []
    industry_brief_count = 0
    for event in events:
        routing = event.get("audience_routing") if isinstance(event.get("audience_routing"), dict) else {}
        if routing and routing.get("lane") != "mainstream":
            industry_brief_count += 1
            continue
        card = event.get("editorial_card") if isinstance(event.get("editorial_card"), dict) else {}
        language = event.get("reader_language") if isinstance(event.get("reader_language"), dict) else {}
        if language:
            language_ok = language.get("writing_status") in {"success", "fallback_safe"}
            title = _text(language.get("title"), 80)
            summary = _text(language.get("summary"), 280)
        else:
            language_ok = card.get("status") == "success" and card.get("locale") == "zh-CN"
            title = _text(card.get("title"), 80)
            summary = _text(card.get("summary"), 280)
        if not language_ok:
            continue
        if event.get("source_status") == "public_news_index_attributed" and not isinstance(event.get("detail_evidence"), list):
            continue
        if not title or not summary or _URL.search(title + summary) or _META.search(title + summary):
            continue
        signal = event.get("douyin_signal") if isinstance(event.get("douyin_signal"), dict) else {}
        interactions = signal.get("raw_interactions") if isinstance(signal.get("raw_interactions"), dict) else {}
        signal_score = (
            int(interactions.get("like") or 0)
            + int(interactions.get("comment") or 0) * 2
            + int(interactions.get("collect") or 0) * 3
            + int(interactions.get("share") or 0) * 4
        )
        company_priority = event.get("company_event_priority") if isinstance(event.get("company_event_priority"), dict) else {}
        delivery_score = int(event.get("reader_delivery_score") or event.get("official_importance_score") or 0)
        boosted = int(company_priority.get("boost") or 0) > 0
        candidates.append(
            {
                "event_id": _text(event.get("official_event_id"), 100),
                "title": title,
                "summary": summary,
                "signal_score": signal_score,
                "ranking_basis": "douyin_signal" if signal_score else "company_event_fallback" if boosted else "source_fallback",
                "source_importance": int(event.get("official_importance_score") or 0),
                "reader_delivery_score": delivery_score,
                "company_event_priority": company_priority,
                "event_freshness": event.get("event_freshness") if isinstance(event.get("event_freshness"), dict) else {},
                "audience_routing": routing,
                "reader_language": language,
                "source_refs": event.get("source_refs") if isinstance(event.get("source_refs"), list) else [],
                "truth_status": _text(event.get("truth_status"), 40) or "not_checked",
            }
        )
    candidates.sort(key=lambda item: (-int(item["signal_score"]), -int(item["reader_delivery_score"]), -int(item["source_importance"]), str(item["event_id"])))
    selected = candidates[: max(0, int(maximum))]
    return {
        "target_count": int(maximum),
        "candidate_count": len(candidates),
        "card_count": len(selected),
        "industry_brief_count": industry_brief_count,
        "status": "success" if len(selected) >= int(maximum) else "partial",
        "ranking_rule": "same_day_douyin_signal_desc_then_company_event_delivery_desc_then_attributed_source_importance_desc",
        "cards": selected,
    }
