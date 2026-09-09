from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import shutil
import socket
import tempfile
import time
import uuid
import copy
import xml.etree.ElementTree as ET
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urljoin
from urllib.parse import urlsplit

import httpx
from PIL import Image

from .job_runtime import JobLock, JobState, now_iso
from .material_probe import (
    HTTPStatusProbeError,
    MaterialProbeError,
    RequestBudget,
    SafeFetcher,
    _decode_image,
    _hamming,
    _project_path,
    _publish_directory,
    _safe_error,
    redact_url,
    validate_https_url,
)


class VisualAnchorError(MaterialProbeError):
    """A bounded, auditable visual-anchor failure."""


_STORY_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_SPACE = re.compile(r"\s+")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+._-]{1,}|[\u4e00-\u9fff]{2,}")
_IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp")
_RIGHTS_DIRS = {
    "project_generated": "renderable",
    "renderable_with_attribution": "renderable",
    "review_required": "review-required",
    "reference_only": "reference-only",
}


def validate_visual_story(payload: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise VisualAnchorError("新闻输入必须是 JSON 对象")
    story_id = str(payload.get("story_id") or "").strip()
    if not _STORY_ID.fullmatch(story_id):
        raise VisualAnchorError("story_id 必须是 3 到 64 位小写字母、数字或连字符")
    try:
        target_date = date.fromisoformat(str(payload.get("target_date") or ""))
    except ValueError as exc:
        raise VisualAnchorError("target_date 必须是 YYYY-MM-DD") from exc
    title = str(payload.get("title_zh") or "").strip()
    summary = str(payload.get("summary_zh") or "").strip()
    if not 4 <= len(title) <= 180 or not 8 <= len(summary) <= 1200:
        raise VisualAnchorError("新闻标题或摘要长度不符合要求")
    confirmation = str(payload.get("confirmation_status") or "")
    if confirmation not in {"official_primary_source", "verified_multi_source"}:
        raise VisualAnchorError("视觉锚点只接受已确认新闻输入")
    sources = payload.get("official_sources")
    if not isinstance(sources, list) or not sources:
        raise VisualAnchorError("至少需要一个可追溯新闻来源")
    normalized_sources: list[dict[str, Any]] = []
    for row in sources[:3]:
        if not isinstance(row, dict):
            raise VisualAnchorError("新闻来源格式无效")
        normalized_sources.append({
            "publisher": str(row.get("publisher") or "来源").strip()[:120],
            "role": str(row.get("role") or "确认来源").strip()[:120],
            "url": redact_url(validate_https_url(str(row.get("url") or ""), settings["allowed_domains"])),
            "attribution_text": str(row.get("attribution_text") or "").strip()[:500],
            "page_context_only_allowed": bool(row.get("page_context_only_allowed", True)),
            "visual_context": str(row.get("visual_context") or "").strip()[:300],
            "max_images": min(12, max(0, int(row.get("max_images") or 0))),
        })
    subject = str(payload.get("visual_subject") or title).strip()[:160]
    aliases = []
    for value in payload.get("subject_aliases") or []:
        text = str(value).strip()[:100]
        if text and text.casefold() not in {item.casefold() for item in aliases}:
            aliases.append(text)
        if len(aliases) >= 8:
            break
    query_terms = []
    for value in payload.get("query_terms") or []:
        text = str(value).strip()[:120]
        if text and text.casefold() not in {item.casefold() for item in query_terms}:
            query_terms.append(text)
        if len(query_terms) >= 3:
            break
    visual_subscenes: list[dict[str, Any]] = []
    for index, row in enumerate(payload.get("visual_subscenes") or []):
        if not isinstance(row, dict):
            continue
        scene_id = str(row.get("scene_id") or f"scene-{index + 1}").strip()[:64]
        terms = [str(value).strip()[:100] for value in row.get("terms") or [] if str(value).strip()][:8]
        if scene_id and terms:
            visual_subscenes.append({"scene_id": scene_id, "terms": terms})
        if len(visual_subscenes) >= 6:
            break
    return {
        "schema_version": "1.0",
        "story_id": story_id,
        "target_date": target_date.isoformat(),
        "title_zh": title,
        "summary_zh": summary,
        "confirmation_status": confirmation,
        "category": str(payload.get("category") or "technology").strip()[:80],
        "visual_subject": subject,
        "subject_aliases": aliases,
        "negative_terms": [str(value).strip()[:120] for value in payload.get("negative_terms") or [] if str(value).strip()][:20],
        "visual_subscenes": visual_subscenes,
        "query_terms": query_terms,
        "region": str(payload.get("region") or "unknown").strip()[:80],
        "official_sources": normalized_sources,
    }


def build_visual_intent(story: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    category = story.get("category") or "technology"
    scenes = {
        "robot_event": ("比赛现场或对应机器人型号", "赛事官方海报或队伍合影"),
        "game_release": ("官方封面、实机或预告片画面", "开发商公告或商店页视觉"),
        "hardware": ("官方产品图或发布、演示现场", "产品官网界面或结构特写"),
        "software": ("真实产品界面、Logo 或官方公告视觉", "官方演示页面"),
        "ai_product": ("真实产品界面、Logo 或官方公告视觉", "官方演示页面"),
    }
    preferred, fallback = scenes.get(str(category), ("对应事件、产品或软件的真实画面", "来源页面的明确新闻视觉"))
    category_negatives = {
        "robot_event": ["housing", "hotel", "registration", "sponsor logo"],
        "game_release": ["corporate logo", "investor relations"],
        "hardware": ["stock office", "generic circuit"],
        "software": ["stock office", "generic code"],
        "ai_product": ["AI大脑", "蓝色电路", "generic AI"],
    }
    values = [story["visual_subject"], *story.get("subject_aliases", []), story["title_zh"]]
    queries = list(story.get("query_terms") or [])
    for value in values:
        if value and value.casefold() not in {item.casefold() for item in queries}:
            queries.append(value[:120])
        if len(queries) >= 3:
            break
    return {
        "schema_version": "1.0",
        "generator": "deterministic_v1",
        "llm_used": False,
        "visual_subject": story["visual_subject"],
        "subject_aliases": story.get("subject_aliases", []),
        "preferred_scene": preferred,
        "fallback_scene": fallback,
        "query_terms": queries[:3],
        "negative_terms": list(dict.fromkeys([*(settings.get("generic_negative_terms") or []), *category_negatives.get(str(category), []), *(story.get("negative_terms") or [])]))[:20],
        "visual_subscenes": story.get("visual_subscenes") or [],
        "story_region": story.get("region") or "unknown",
        "category": category,
        "fact_fields_copied_from_story_only": True,
    }


class _EmbeddedImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[dict[str, str]] = []
        self._figure_caption = ""
        self._in_caption = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).casefold(): str(value or "").strip() for key, value in attrs}
        lowered = tag.casefold()
        if lowered == "figcaption":
            self._in_caption = True
        if lowered == "img":
            source = values.get("src") or values.get("data-src") or values.get("data-original")
            if source:
                self.images.append({"url": source, "alt": values.get("alt", ""), "caption": self._figure_caption})

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "figcaption":
            self._in_caption = False

    def handle_data(self, data: str) -> None:
        if self._in_caption:
            self._figure_caption = _SPACE.sub(" ", f"{self._figure_caption} {data}").strip()[:300]


class _WebImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: list[tuple[str, str]] = []
        self.body: list[dict[str, str]] = []
        self.jsonld: list[str] = []
        self._jsonld = False
        self._json_parts: list[str] = []
        self._caption = False
        self._caption_text = ""
        self._title = False
        self.page_title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).casefold(): str(value or "").strip() for key, value in attrs}
        lowered = tag.casefold()
        if lowered == "meta":
            key = (values.get("property") or values.get("name") or "").casefold()
            if key in {"og:title", "twitter:title"} and values.get("content") and not self.page_title:
                self.page_title = values["content"][:300]
            kind = "og_image" if key in {"og:image", "og:image:secure_url"} else "twitter_image" if key in {"twitter:image", "twitter:image:src"} else ""
            if kind and values.get("content"):
                self.meta.append((kind, values["content"]))
        elif lowered == "script" and values.get("type", "").casefold() == "application/ld+json":
            self._jsonld = True
            self._json_parts = []
        elif lowered == "figcaption":
            self._caption = True
            self._caption_text = ""
        elif lowered == "title":
            self._title = True
        elif lowered == "img":
            source = values.get("data-original") or values.get("data-src") or values.get("src")
            if source:
                self.body.append({"url": source, "title": values.get("title", ""), "alt": values.get("alt", ""), "caption": self._caption_text, "kind": "body_image"})
        elif lowered == "source" and values.get("srcset") and values.get("type", "image/webp").casefold() == "image/webp":
            source = _largest_srcset_url(values["srcset"])
            if source:
                self.body.append({"url": source, "title": values.get("title", ""), "alt": values.get("alt", ""), "caption": self._caption_text, "kind": "body_srcset"})

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "script" and self._jsonld:
            self.jsonld.append("".join(self._json_parts))
            self._jsonld = False
        elif lowered == "figcaption":
            self._caption = False
        elif lowered == "title":
            self._title = False

    def handle_data(self, data: str) -> None:
        if self._jsonld:
            self._json_parts.append(data)
        elif self._caption:
            self._caption_text = _SPACE.sub(" ", f"{self._caption_text} {data}").strip()[:300]
        elif self._title and not self.page_title:
            self.page_title = _SPACE.sub(" ", f"{self.page_title} {data}").strip()[:300]


def _safe_candidate_url(base_url: str, value: str, allowed_domains: Iterable[str]) -> str | None:
    try:
        return redact_url(validate_https_url(urljoin(base_url, html.unescape(str(value or ""))), allowed_domains))
    except MaterialProbeError:
        return None


def _largest_srcset_url(value: str) -> str:
    rows: list[tuple[float, str]] = []
    for index, part in enumerate(str(value or "").split(",")):
        fields = part.strip().split()
        if not fields:
            continue
        score = float(index)
        if len(fields) > 1:
            descriptor = fields[-1].casefold()
            try:
                score = float(descriptor[:-1]) * (1000 if descriptor.endswith("x") else 1)
            except (ValueError, TypeError):
                pass
        rows.append((score, fields[0]))
    return max(rows, default=(0, ""), key=lambda row: row[0])[1]


def _jsonld_images(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        image = value.get("image")
        if isinstance(image, str):
            yield image
        elif isinstance(image, list):
            for row in image:
                if isinstance(row, str):
                    yield row
                elif isinstance(row, dict) and isinstance(row.get("url"), str):
                    yield row["url"]
        elif isinstance(image, dict) and isinstance(image.get("url"), str):
            yield image["url"]
        for key, row in value.items():
            if key != "image":
                yield from _jsonld_images(row)
    elif isinstance(value, list):
        for row in value:
            yield from _jsonld_images(row)


def extract_web_image_candidates(page_url: str, document: bytes, allowed_domains: Iterable[str]) -> list[dict[str, Any]]:
    parser = _WebImageParser()
    parser.feed(document.decode("utf-8", errors="replace"))
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def append(kind: str, value: str, *, title: str = "", alt: str = "", caption: str = "") -> None:
        candidate = _safe_candidate_url(page_url, value, allowed_domains)
        if not candidate or candidate in seen:
            return
        seen.add(candidate)
        rows.append({"image_url": candidate, "article_url": redact_url(page_url), "extraction_kind": kind, "title": title[:300], "alt": alt[:300], "caption": caption[:300], "page_context": parser.page_title[:300]})

    for kind, value in parser.meta:
        append(kind, value)
    for block in parser.jsonld:
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        for value in _jsonld_images(payload):
            append("jsonld_image", value)
    for row in parser.body:
        append(row.get("kind") or "body_image", row["url"], title=row.get("title", ""), alt=row["alt"], caption=row["caption"])
    generic_markers = ("placeholder", "avatar", "qrcode", "二维码", "cloud.png", "logo", "site-icon", "favicon", "1x1.gif", "share_weixin", "search_jiucuo")
    rows.sort(key=lambda row: 1 if any(marker in " ".join(str(row.get(key) or "") for key in ("image_url", "title", "alt")).casefold() for marker in generic_markers) else 0)
    return rows[:12]


def extract_rss_image_candidates(feed_url: str, document: bytes, allowed_domains: Iterable[str]) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(document)
    except ET.ParseError as exc:
        raise VisualAnchorError("RSS XML 无法解析") from exc
    rows: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        title = str(item.findtext("title") or "").strip()[:300]
        article = _safe_candidate_url(feed_url, str(item.findtext("link") or feed_url), allowed_domains) or redact_url(feed_url)
        priorities: list[tuple[str, str]] = []
        for child in list(item):
            local = child.tag.rsplit("}", 1)[-1].casefold()
            namespace = child.tag[1:].split("}", 1)[0] if child.tag.startswith("{") else ""
            if namespace == "http://search.yahoo.com/mrss/" and local == "content" and child.attrib.get("url"):
                priorities.append(("media_content", child.attrib["url"]))
            elif namespace == "http://search.yahoo.com/mrss/" and local == "thumbnail" and child.attrib.get("url"):
                priorities.append(("media_thumbnail", child.attrib["url"]))
        for child in item.findall("enclosure"):
            if str(child.attrib.get("type") or "").casefold().startswith("image/") and child.attrib.get("url"):
                priorities.append(("image_enclosure", child.attrib["url"]))
        embedded: list[str] = []
        for child in list(item):
            local = child.tag.rsplit("}", 1)[-1].casefold()
            if local not in {"description", "encoded"} or not child.text:
                continue
            parser = _EmbeddedImageParser()
            parser.feed(child.text)
            if parser.images:
                embedded.append(parser.images[0]["url"])
        priorities.extend(("embedded_image", value) for value in embedded)
        seen: set[str] = set()
        for kind, value in priorities:
            image_url = _safe_candidate_url(article, value, allowed_domains)
            if image_url and image_url not in seen:
                seen.add(image_url)
                rows.append({"image_url": image_url, "article_url": article, "extraction_kind": kind, "title": "", "alt": "", "caption": "", "page_context": title})
    return rows


def _terms(intent: dict[str, Any]) -> list[str]:
    values = [intent.get("visual_subject", ""), *(intent.get("subject_aliases") or [])]
    result: list[str] = []
    for value in values:
        text = str(value).casefold().strip()
        if text and text not in result:
            result.append(text)
        for token in _TOKEN.findall(text):
            if len(token) >= 2 and token not in result:
                result.append(token)
    return result


def _subject_phrases(intent: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for value in [intent.get("visual_subject", ""), *(intent.get("subject_aliases") or [])]:
        text = _SPACE.sub(" ", str(value).casefold().strip())
        if text and text not in result:
            result.append(text)
    return result


def _contains_subject_phrase(value: str, phrase: str) -> bool:
    """Match a full entity phrase without accepting product-version prefixes."""
    haystack = str(value or "").casefold().replace("–", "-").replace("—", "-")
    expected = str(phrase or "").casefold().replace("–", "-").replace("—", "-").strip()
    if not expected:
        return False
    tokens = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", expected)
    if not tokens:
        return expected in haystack
    if all(re.fullmatch(r"[\u4e00-\u9fff]+", token) for token in tokens):
        return "".join(tokens) in re.sub(r"\s+", "", haystack)
    suffix_guard = r"(?![a-z0-9.]|-\d)" if tokens[-1].isdigit() else r"(?![a-z0-9])"
    pattern = r"(?<![a-z0-9])" + r"[\s._+\-/]*".join(re.escape(token) for token in tokens) + suffix_guard
    return re.search(pattern, haystack, flags=re.IGNORECASE) is not None


def rank_candidates(intent: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    terms = _terms(intent)
    phrases = _subject_phrases(intent)
    negatives = [str(value).casefold() for value in intent.get("negative_terms") or []]
    ranked: list[dict[str, Any]] = []
    for index, source in enumerate(candidates):
        row = dict(source)
        haystack = " ".join(str(row.get(key) or "") for key in ("title", "alt", "caption", "image_url", "context")).casefold()
        page_context = str(row.get("page_context") or "").casefold()
        strong_exact = [phrase for phrase in phrases if _contains_subject_phrase(haystack, phrase)]
        strong_page = [phrase for phrase in phrases if _contains_subject_phrase(page_context, phrase)]
        weak_terms = [term for term in terms if term and term in haystack and term not in strong_exact]
        combined = f"{haystack} {page_context}"
        negative = [term for term in negatives if term and term in combined]
        generic_marker = any(term in haystack for term in (
            "placeholder", "avatar", "logo", "site-icon", "favicon", "default-author", "qrcode", "二维码",
            "作者头像", "扫码关注", "share_weixin", "search_jiucuo", "/images/v2/t.png", "/image/new.png",
        ))
        scene_id = ""
        for scene in intent.get("visual_subscenes") or []:
            if any(str(term).casefold() in haystack for term in scene.get("terms") or []):
                scene_id = str(scene.get("scene_id") or "")
                break
        if not scene_id:
            for scene in intent.get("visual_subscenes") or []:
                if any(str(term).casefold() in page_context for term in scene.get("terms") or []):
                    scene_id = str(scene.get("scene_id") or "")
                    break
        breakdown = {
            "exact_subject": 50 if strong_exact else 0,
            "weak_subject_tokens": min(12, 4 * len(weak_terms)),
            "page_context": 20 if strong_page else 0,
            "official_source": 18 if str(row.get("source_kind") or "").startswith("official") else 10 if row.get("source_kind") in {"rss_image", "article_body"} else 0,
            "resolution": 8 if min(int(row.get("width") or 0), int(row.get("height") or 0)) >= 720 else 4 if min(int(row.get("width") or 0), int(row.get("height") or 0)) >= 480 else 0,
            "reasonable_ratio": 4 if 0.45 <= (float(row.get("width") or 1) / max(1, float(row.get("height") or 1))) <= 2.4 else -4,
            "generic_penalty": -40 * len(negative),
            "placeholder_penalty": -50 if generic_marker else 0,
        }
        score = sum(breakdown.values())
        if generic_marker or negative:
            relevance = "rejected_generic"
        elif strong_exact or strong_page and bool(row.get("page_context_only_allowed", True)):
            relevance = "exact_subject"
        else:
            relevance = "insufficient_match"
        row.update({"candidate_id": str(row.get("candidate_id") or f"candidate-{index:03d}"), "score": score, "score_breakdown": breakdown, "matched_terms": strong_exact, "weak_matched_terms": weak_terms, "page_context_terms": strong_page, "negative_matches": negative, "relevance_status": relevance, "scene_id": scene_id})
        ranked.append(row)
    return sorted(ranked, key=lambda row: (-int(row["score"]), str(row["candidate_id"])))


def _crop_dhashes(data: bytes) -> list[str]:
    """Create bounded hashes that can detect a screenshot inside a titled/cropped variant."""
    with Image.open(io.BytesIO(data)) as source:
        image = source.convert("L")
        width, height = image.size
        crops = [image]
        for fraction in (0.9, 0.8, 0.7):
            crop_height = max(1, int(height * fraction))
            crops.extend([
                image.crop((0, 0, width, crop_height)),
                image.crop((0, height - crop_height, width, height)),
                image.crop((0, (height - crop_height) // 2, width, (height + crop_height) // 2)),
            ])
        hashes: list[str] = []
        for crop in crops:
            resized = crop.resize((9, 8), Image.Resampling.LANCZOS)
            pixels = list(resized.get_flattened_data())
            bits = [pixels[row * 9 + column] > pixels[row * 9 + column + 1] for row in range(8) for column in range(8)]
            value = f"{sum((1 << index) for index, enabled in enumerate(bits) if enabled):016x}"
            if value not in hashes:
                hashes.append(value)
        return hashes


def _same_source_near_duplicate(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if redact_url(str(left.get("article_url") or "")) != redact_url(str(right.get("article_url") or "")):
        return False
    left_hashes = left.get("crop_hashes") or [left.get("perceptual_hash")]
    right_hashes = right.get("crop_hashes") or [right.get("perceptual_hash")]
    color_delta = max(abs(int(left.get("average_rgb", [0, 0, 0])[index]) - int(right.get("average_rgb", [0, 0, 0])[index])) for index in range(3))
    return color_delta <= 32 and any(_hamming(str(first), str(second)) <= 4 for first in left_hashes for second in right_hashes if first and second)


def _select_diverse_candidates(
    qualified: list[dict[str, Any]],
    limit: int,
    rejected: list[dict[str, str]],
    preferred_primary_scene: str = "",
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    remaining = list(qualified)
    while remaining and len(selected) < limit:
        eligible: list[dict[str, Any]] = []
        for row in remaining:
            if any(_same_source_near_duplicate(row, existing) for existing in selected):
                if not any(item["candidate_id"] == row["candidate_id"] for item in rejected):
                    rejected.append({"candidate_id": row["candidate_id"], "reason": "near_duplicate_visual"})
            else:
                eligible.append(row)
        if not eligible:
            break
        if not selected and preferred_primary_scene:
            preferred = [row for row in eligible if str(row.get("scene_id") or "") == preferred_primary_scene]
        else:
            preferred = []
        used_scenes = {str(row.get("scene_id") or "") for row in selected if row.get("scene_id")}
        novel = [row for row in eligible if row.get("scene_id") and str(row["scene_id"]) not in used_scenes]
        chosen = (preferred or novel or eligible)[0]
        selected.append(chosen)
        remaining = [row for row in eligible if row["candidate_id"] != chosen["candidate_id"]]
    return selected


def _asset_id(source_kind: str, source_url: str, digest: str) -> str:
    return "anchor-" + hashlib.sha256(f"{source_kind}\n{redact_url(source_url)}\n{digest}".encode("utf-8")).hexdigest()[:16]


def _write_selected_asset(stage: Path, candidate: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    data = candidate.pop("_data")
    digest = candidate["sha256"]
    asset_id = _asset_id(candidate["source_kind"], candidate["article_url"], digest)
    relative = Path(_RIGHTS_DIRS[candidate["rights_status"]]) / f"{asset_id}{candidate['extension']}"
    destination = stage / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    quality_note = "清晰度一般，建议仅用于中小画幅或参考" if min(int(candidate["width"]), int(candidate["height"])) <= 480 else "清晰度满足 V1 最低门槛"
    return {
        "asset_id": asset_id,
        "local_path": relative.as_posix(),
        "source_kind": candidate["source_kind"],
        "source_article_url": redact_url(candidate["article_url"]),
        "image_source_url": redact_url(candidate["image_url"]),
        "rights_status": candidate["rights_status"],
        "production_readiness": "ready_with_attribution" if candidate["rights_status"] == "renderable_with_attribution" else "manual_rights_review" if candidate["rights_status"] == "review_required" else "reference_only",
        "renderable": candidate["rights_status"] in {"project_generated", "renderable_with_attribution"},
        "selection_reason": f"图片证据匹配：{', '.join(candidate['matched_terms']) or '无'}；页面上下文匹配：{', '.join(candidate.get('page_context_terms') or []) or '无'}",
        "score": candidate["score"],
        "score_breakdown": candidate["score_breakdown"],
        "mime_type": candidate["mime_type"],
        "width": candidate["width"],
        "height": candidate["height"],
        "quality_note": quality_note,
        "bytes": candidate["bytes"],
        "sha256": digest,
        "perceptual_hash": candidate["perceptual_hash"],
        "attribution_text": str(candidate.get("attribution_text") or "")[:500],
        "license_name": str(candidate.get("license_name") or "")[:160],
        "license_url": redact_url(str(candidate.get("license_url") or "")),
    }


def _preview(story: dict[str, Any], manifest: dict[str, Any]) -> str:
    lines = [
        f"# {story['title_zh']}：新闻视觉锚点",
        "",
        f"状态：`{manifest['status']}`",
        "",
        f"> {story['summary_zh']}",
        "",
        "抖音只作为视觉线索与注意力证据，不是新闻事实来源。相关度与生产权利是两个独立门禁；`review_required` 和 `reference_only` 不会自动进入成片。",
        "",
        "## 最终锚点",
        "",
    ]
    roles = (
        ("主视觉", manifest.get("primary_candidate")),
        ("备用视觉 1", manifest.get("optional_secondary_candidate")),
        ("备用视觉 2", manifest.get("optional_tertiary_candidate")),
    )
    for label, item in roles:
        if not item:
            lines.extend([f"### {label}", "", "未取得合格素材；没有用无关图片补位。", ""])
            continue
        lines.extend([
            f"### {label} · {item['rights_status']}", "",
            f"- 文件：`{item['local_path']}`（{item['width']}×{item['height']}，{item['bytes']} bytes）",
            f"- 对应原因：{item['selection_reason']}",
            f"- 相关度得分：{item['score']}；生产准备度：`{item['production_readiness']}`",
            f"- 画质：{item['quality_note']}",
            f"- 来源文章：[{item['source_kind']}]({item['source_article_url']})",
            f"- 图片来源：[{item['mime_type']}]({item['image_source_url']})",
            f"- 署名：{item['attribution_text'] or '来源页未提供单独摄影署名；需人工权利复核'}",
            "",
        ])
    if manifest.get("missing_asset_reason"):
        lines.extend(["## 缺图说明", "", manifest["missing_asset_reason"], ""])
    lines.extend(["## 运行与边界", ""])
    counts, budget, douyin, rss = manifest["counts"], manifest["budget"], manifest["douyin"], manifest["rss"]
    lines.extend([
        f"- 候选：{counts['candidate_metadata']}；下载校验：{counts['decoded_images']}；最终素材：{counts['assets']} / {manifest['max_final_assets']}",
        f"- 请求：{budget['request_count']} / {budget['max_requests']}；耗时：{budget['elapsed_seconds']} / {budget['total_timeout_seconds']} 秒",
        f"- RSS：配置 {rss['configured_sources']} 个；触发 {rss['triggered']}；请求 {rss['feeds_requested']}；图片候选 {rss['image_candidates']}；匹配 {rss['matched_candidates']}",
        f"- 抖音查询/结果/封面候选/封面下载/视频/抽帧：{douyin['query_count']}/{douyin['result_count']}/{douyin['cover_candidates']}/{douyin['cover_downloads']}/{douyin['video_count']}/{douyin['frame_count']}；音频尝试：{douyin['audio_attempted']}",
        f"- 抖音封面网络预算：请求 {douyin['cover_request_count']} / {manifest['douyin_limits']['max_cover_requests']}；字节 {douyin['cover_downloaded_bytes']} / {manifest['douyin_limits']['max_cover_bytes_total']}；耗尽 {douyin['cover_budget_exhausted']}",
        "- OpenMontage 写入：0；LLM / ASR 调用：0",
        "",
    ])
    if manifest["failures"]:
        lines.extend(["## 降级记录", ""])
        for failure in manifest["failures"]:
            lines.append(f"- {failure['source']}：{failure['message']}")
        lines.append("")
    return "\n".join(lines)


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def _run_story(
    config: dict[str, Any], story: dict[str, Any], settings: dict[str, Any], *, client: httpx.Client | None, resolver: Callable[..., Any], douyin_fallback: bool, douyin_provider: Callable[..., dict[str, Any]] | None,
) -> dict[str, Any]:
    output_root = _project_path(config, settings["output_root"])
    destination = output_root / story["target_date"] / story["story_id"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.parent / f".{story['story_id']}.stage-{uuid.uuid4().hex}"
    stage.mkdir(parents=True)
    for folder in _RIGHTS_DIRS.values():
        (stage / folder).mkdir(exist_ok=True)
    intent = build_visual_intent(story, settings)
    failures: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    metadata: list[dict[str, Any]] = []
    decoded: list[dict[str, Any]] = []
    budget = RequestBudget(int(settings["max_requests_per_story"]), int(settings["max_total_bytes_per_story"]), float(settings["total_timeout_seconds_per_story"]), time.monotonic())
    fetch_settings = {
        **settings,
        "max_redirects": int(settings["max_redirects"]),
        "request_timeout_seconds": int(settings["request_timeout_seconds"]),
        "max_requests": int(settings["max_requests_per_story"]),
        "max_total_bytes": int(settings["max_total_bytes_per_story"]),
        "total_timeout_seconds": int(settings["total_timeout_seconds_per_story"]),
    }
    fetcher = SafeFetcher(fetch_settings, budget, client=client, resolver=resolver)
    douyin = {"enabled": bool(douyin_fallback), "status": "not_needed", "query_count": 0, "result_count": 0, "cover_candidates": 0, "cover_downloads": 0, "cover_request_count": 0, "cover_downloaded_bytes": 0, "cover_budget_exhausted": False, "video_count": 0, "frame_count": 0, "audio_attempted": 0}
    rss = {"configured_sources": len([row for row in settings.get("rss_sources") or [] if isinstance(row, dict) and row.get("enabled", True)]), "triggered": False, "feeds_requested": 0, "image_candidates": 0, "matched_candidates": 0}

    def decode_rows(rows: list[dict[str, Any]]) -> None:
        for candidate in rows:
            try:
                final_url, content_type, data = fetcher.get(candidate["image_url"], maximum_bytes=int(settings["max_asset_bytes"]), accepted_types=_IMAGE_TYPES)
                info = _decode_image(data, content_type, int(settings["min_dimension"]))
                digest = hashlib.sha256(data).hexdigest()
                duplicate = False
                for existing in decoded:
                    color_delta = max(abs(int(existing["average_rgb"][i]) - int(info["average_rgb"][i])) for i in range(3))
                    if existing["sha256"] == digest or _hamming(existing["perceptual_hash"], info["dhash"]) <= 3 and color_delta <= 24:
                        duplicate = True
                        break
                if duplicate:
                    rejected.append({"candidate_id": candidate["candidate_id"], "reason": "duplicate_visual"})
                    continue
                decoded.append({
                    **candidate, "image_url": redact_url(final_url), "_data": data, "sha256": digest,
                    "perceptual_hash": info["dhash"], "crop_hashes": _crop_dhashes(data),
                    "average_rgb": info["average_rgb"], "mime_type": info["mime_type"],
                    "extension": info["extension"], "width": info["width"], "height": info["height"], "bytes": len(data),
                })
            except (MaterialProbeError, httpx.HTTPError, ValueError) as exc:
                rejected.append({"candidate_id": candidate["candidate_id"], "reason": "validation_failed", "detail": _safe_error(exc)})

    try:
        for source_index, source in enumerate(story["official_sources"]):
            if len(metadata) >= int(settings["max_candidates"]):
                break
            try:
                final_url, _mime, document = fetcher.get(source["url"], maximum_bytes=int(settings["max_html_bytes"]), accepted_types=("text/html", "application/xhtml+xml"))
                remaining_sources = len(story["official_sources"]) - source_index
                remaining_slots = int(settings["max_candidates"]) - len(metadata)
                source_limit = max(1, remaining_slots // max(1, remaining_sources))
                if int(source.get("max_images") or 0) > 0:
                    source_limit = min(source_limit, int(source["max_images"]))
                for row in extract_web_image_candidates(final_url, document, settings["allowed_domains"])[:source_limit]:
                    row.update({
                        "candidate_id": f"official-{source_index}-{len(metadata):03d}", "source_kind": "official_article",
                        "publisher": source["publisher"], "rights_status": "review_required",
                        "attribution_text": source.get("attribution_text", ""),
                        "page_context_only_allowed": bool(source.get("page_context_only_allowed", True)),
                        "context": source.get("visual_context", ""),
                    })
                    metadata.append(row)
                    if len(metadata) >= int(settings["max_candidates"]):
                        break
            except (MaterialProbeError, httpx.HTTPError, ValueError) as exc:
                failures.append({"source": f"official_source_{source_index + 1}", "error_type": type(exc).__name__, "message": _safe_error(exc)})

        decode_rows(metadata[: int(settings["max_candidates"])])

        ranked = rank_candidates(intent, decoded)
        qualified = [row for row in ranked if row["relevance_status"] == "exact_subject" and int(row["score"]) >= int(settings["minimum_relevance_score"])]
        if not qualified and rss["configured_sources"]:
            rss["triggered"] = True
            rss_metadata: list[dict[str, Any]] = []
            for feed_index, feed in enumerate(settings.get("rss_sources") or []):
                if len(metadata) + len(rss_metadata) >= int(settings["max_candidates"]):
                    break
                if not isinstance(feed, dict) or not feed.get("enabled", True):
                    continue
                try:
                    rss["feeds_requested"] += 1
                    feed_url, _mime, document = fetcher.get(
                        str(feed.get("url") or ""), maximum_bytes=int(settings["max_html_bytes"]),
                        accepted_types=("application/rss+xml", "application/atom+xml", "application/xml", "text/xml"),
                    )
                    feed_rows = extract_rss_image_candidates(feed_url, document, settings["allowed_domains"])
                    rss["image_candidates"] += len(feed_rows)
                    provisional = []
                    for row in feed_rows:
                        row.update({"candidate_id": f"rss-{feed_index}-{len(provisional):03d}", "source_kind": "rss_image", "publisher": str(feed.get("name") or "RSS")[:120], "rights_status": "review_required"})
                        provisional.append(row)
                    matched = [row for row in rank_candidates(intent, provisional) if row["relevance_status"] == "exact_subject" and int(row["score"]) >= int(settings["minimum_relevance_score"])]
                    for row in matched:
                        rss_metadata.append({key: value for key, value in row.items() if key not in {"score", "score_breakdown", "matched_terms", "page_context_terms", "negative_matches", "relevance_status"}})
                        if len(metadata) + len(rss_metadata) >= int(settings["max_candidates"]):
                            break
                except (MaterialProbeError, httpx.HTTPError, ValueError) as exc:
                    failures.append({"source": f"rss_source_{feed_index + 1}", "error_type": type(exc).__name__, "message": _safe_error(exc)})
            rss["matched_candidates"] = len(rss_metadata)
            metadata.extend(rss_metadata)
            decode_rows(rss_metadata)
            ranked = rank_candidates(intent, decoded)
            qualified = [row for row in ranked if row["relevance_status"] == "exact_subject" and int(row["score"]) >= int(settings["minimum_relevance_score"])]
        if not qualified and douyin_fallback:
            douyin["status"] = "unavailable"
            provider = douyin_provider or (lambda selected_story, selected_intent, limits: _collect_douyin_clues(config, selected_story, selected_intent, limits))
            if provider is not None:
                try:
                    response = provider(story, intent, dict(settings["douyin"]))
                    allowed_status = str(response.get("status") or "failed")
                    douyin.update({
                        "status": allowed_status if allowed_status in {"success", "partial", "empty", "metadata_only_unavailable", "needs_login", "failed"} else "failed",
                        "query_count": min(int(settings["douyin"]["max_queries"]), max(0, int(response.get("query_count") or 0))),
                        "result_count": min(int(settings["douyin"]["max_results"]), max(0, int(response.get("result_count") or 0))),
                        "cover_candidates": min(int(settings["douyin"]["max_results"]), max(0, int(response.get("cover_candidates") or 0))),
                        "cover_downloads": min(int(settings["douyin"]["max_results"]), max(0, int(response.get("cover_downloads") or 0))),
                        "cover_request_count": min(int(settings["douyin"]["max_cover_requests"]), max(0, int(response.get("cover_request_count") or 0))),
                        "cover_downloaded_bytes": min(int(settings["douyin"]["max_cover_bytes_total"]), max(0, int(response.get("cover_downloaded_bytes") or 0))),
                        "cover_budget_exhausted": bool(response.get("cover_budget_exhausted", False)),
                        "video_count": min(int(settings["douyin"]["max_video_downloads"]), max(0, int(response.get("video_count") or 0))),
                        "frame_count": min(int(settings["douyin"]["max_frames"]), max(0, int(response.get("frame_count") or 0))),
                        "audio_attempted": 0,
                    })
                    for candidate in response.get("candidates") or []:
                        if not isinstance(candidate, dict) or not isinstance(candidate.get("_data"), bytes):
                            continue
                        data = candidate["_data"]
                        info = _decode_image(data, str(candidate.get("mime_type") or ""), int(settings["min_dimension"]))
                        digest = hashlib.sha256(data).hexdigest()
                        if any(existing["sha256"] == digest or _hamming(existing["perceptual_hash"], info["dhash"]) <= 3 for existing in decoded):
                            continue
                        decoded.append({
                            **candidate, "candidate_id": f"douyin-cover-{len(decoded):03d}", "source_kind": "douyin_cover",
                            "image_url": "", "rights_status": "reference_only", "sha256": digest, "perceptual_hash": info["dhash"],
                            "crop_hashes": _crop_dhashes(data), "average_rgb": info["average_rgb"],
                            "mime_type": info["mime_type"], "extension": info["extension"],
                            "width": info["width"], "height": info["height"], "bytes": len(data),
                        })
                except Exception as exc:
                    douyin.update({"status": "failed"})
                    failures.append({"source": "douyin_fallback", "error_type": type(exc).__name__, "message": _safe_error(exc)})
        ranked = rank_candidates(intent, decoded)
        qualified = [row for row in ranked if row["relevance_status"] == "exact_subject" and int(row["score"]) >= int(settings["minimum_relevance_score"])]
        primary_scene = str((intent.get("visual_subscenes") or [{}])[0].get("scene_id") or "")
        selected = _select_diverse_candidates(
            qualified,
            int(settings["max_final_assets"]),
            rejected,
            preferred_primary_scene=primary_scene,
        )
        selected_ids = {row["candidate_id"] for row in selected}
        for row in ranked:
            if row["candidate_id"] not in selected_ids and not any(item["candidate_id"] == row["candidate_id"] for item in rejected):
                rejected.append({"candidate_id": row["candidate_id"], "reason": "relevance_below_threshold" if row["relevance_status"] != "exact_subject" or int(row["score"]) < int(settings["minimum_relevance_score"]) else "final_asset_limit"})
        assets = [_write_selected_asset(stage, dict(row), settings) for row in selected]
        for index, asset in enumerate(assets):
            asset["role"] = "primary" if index == 0 else f"backup_{index}"
        if len(assets) >= 2 and not failures:
            status = "success"
        elif assets:
            status = "partial"
        elif douyin.get("status") == "needs_login":
            status = "needs_login"
        else:
            status = "failed"
        counts = {
            "candidate_metadata": len(metadata), "decoded_images": len(decoded), "assets": len(assets),
            "review_required": sum(item["rights_status"] == "review_required" for item in assets),
            "reference_only": sum(item["rights_status"] == "reference_only" for item in assets),
            "renderable": sum(bool(item["renderable"]) for item in assets), "rejected": len(rejected), "failures": len(failures),
        }
        missing_asset_reason = "" if len(assets) >= 2 else (
            "仅取得1张准确且非重复素材；未使用无关图、Logo、头像、二维码或近重复图补位。" if assets
            else "未取得达到相关度、解码、尺寸和去重门槛的准确素材。"
        )
        manifest = {
            "schema_version": "1.0", "status": status, "generated_at": now_iso(str(config["timezone"])), "story_id": story["story_id"],
            "primary_candidate": assets[0] if assets else None,
            "optional_secondary_candidate": assets[1] if len(assets) > 1 else None,
            "optional_third_candidate": assets[2] if len(assets) > 2 else None,
            "optional_tertiary_candidate": assets[2] if len(assets) > 2 else None,
            "max_final_assets": int(settings["max_final_assets"]),
            "missing_asset_reason": missing_asset_reason,
            "assets": assets, "rejected_candidates": rejected, "failures": failures, "counts": counts,
            "budget": {**budget.snapshot(), "request_timeout_seconds": int(settings["request_timeout_seconds"]), "max_asset_bytes": int(settings["max_asset_bytes"])},
            "rss": rss, "douyin": douyin, "douyin_limits": {key: value for key, value in settings["douyin"].items() if key != "enabled_by_default"},
            "confirmation": {"input_status": story["confirmation_status"], "model_verified_facts": False, "douyin_used_as_fact_source": False},
            "boundaries": {"openmontage_modified": False, "third_party_modified": False, "llm_calls": 0, "asr_calls": 0, "audio_attempted": douyin["audio_attempted"]},
        }
        _atomic_json(stage / "story.json", story)
        _atomic_json(stage / "visual-intent.json", intent)
        _atomic_json(stage / "manifest.json", manifest)
        (stage / "preview.md").write_text(_preview(story, manifest), encoding="utf-8", newline="\n")
        _publish_directory(stage, destination)
        return {"story_id": story["story_id"], "status": status, "output_dir": str(destination.resolve()), "output_path": str((destination / "preview.md").resolve()), "manifest_path": str((destination / "manifest.json").resolve()), "counts": counts, "budget": manifest["budget"], "douyin": douyin}
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise
    finally:
        fetcher.close()


def _collect_douyin_clues(config: dict[str, Any], story: dict[str, Any], intent: dict[str, Any], limits: dict[str, Any]) -> dict[str, Any]:
    """Search public metadata and fetch a few bounded covers without retaining signed URLs."""
    from .normalize import load_raw_records
    from .search_collector import collect_search

    queries = [str(value).strip() for value in intent.get("query_terms") or [] if str(value).strip()][: int(limits["max_queries"])]
    raw_covers: list[dict[str, str]] = []

    def capture(files: list[Path]) -> None:
        for path in files:
            for row in load_raw_records(path):
                if len(raw_covers) >= int(limits["max_results"]):
                    return
                video_id = str(row.get("aweme_id") or row.get("video_id") or "").strip()
                cover_url = str(row.get("cover_url") or "").strip()
                if not cover_url:
                    continue
                raw_covers.append({
                    "cover_url": cover_url,
                    "title": str(row.get("title") or row.get("desc") or "").strip()[:300],
                    "article_url": f"https://www.douyin.com/video/{video_id}" if video_id.isdigit() else "https://www.douyin.com/",
                })

    report = collect_search(
        config,
        int(limits["max_results"]),
        run_id=f"visual-anchor-{story['story_id']}-{uuid.uuid4().hex[:8]}",
        keywords=queries,
        hard_max=int(limits["max_results"]),
        keep_browser_on_failure=True,
        before_sanitize=capture,
    )
    result_count = sum(int(row.get("record_count") or 0) for row in report.get("sanitization") or [])
    cover_budget = {
        "max_requests": int(limits["max_cover_requests"]), "request_count": 0,
        "max_total_bytes": int(limits["max_cover_bytes_total"]), "downloaded_bytes": 0, "exhausted": False,
    }
    sortable = []
    for index, row in enumerate(raw_covers[: int(limits["max_results"])]):
        sortable.append({**row, "candidate_id": f"raw-cover-{index:03d}", "source_kind": "douyin_cover", "image_url": ""})
    ranked_covers = [row for row in rank_candidates(intent, sortable) if row["relevance_status"] == "exact_subject"]
    candidates: list[dict[str, Any]] = []
    for row in ranked_covers:
        if len(candidates) >= int(limits["max_cover_downloads"]) or cover_budget["exhausted"]:
            break
        downloaded = _download_douyin_cover(
            config, row["cover_url"], maximum_bytes=int(limits["max_cover_bytes_each"]), budget=cover_budget,
        )
        if downloaded is None:
            continue
        mime_type, data = downloaded
        candidates.append({
            "_data": data, "mime_type": mime_type, "title": row["title"], "alt": row["title"], "caption": "",
            "article_url": row["article_url"], "attribution_text": "抖音公开视频封面，仅作视觉参考",
            "license_name": "未取得复用许可", "license_url": "",
        })
    return {
        "status": "partial" if candidates else "metadata_only_unavailable" if report.get("status") == "success" and result_count else "needs_login",
        "query_count": len(report.get("keywords") or []),
        "result_count": min(result_count, int(limits["max_results"])),
        "cover_candidates": min(len(raw_covers), int(limits["max_results"])),
        "cover_downloads": len(candidates),
        "cover_request_count": cover_budget["request_count"],
        "cover_downloaded_bytes": cover_budget["downloaded_bytes"],
        "cover_budget_exhausted": bool(cover_budget["exhausted"]),
        "video_count": 0,
        "frame_count": 0,
        "audio_attempted": 0,
        "candidates": candidates,
    }


def _download_douyin_cover(
    config: dict[str, Any], value: str, *, maximum_bytes: int, budget: dict[str, Any],
    client: httpx.Client | None = None, resolver: Callable[..., Any] = socket.getaddrinfo,
) -> tuple[str, bytes] | None:
    """Fetch one public cover without persisting its temporary signed URL."""
    current = str(value or "").strip()
    timeout = min(15.0, float(config["jobs"]["visual_anchor"]["request_timeout_seconds"]))
    fake_networks = config["jobs"]["visual_anchor"].get("fake_ip_networks") or ()
    owns_client = client is None
    active_client = client or httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=False)
    try:
        for _redirect in range(2):
            if int(budget["request_count"]) >= int(budget["max_requests"]):
                budget["exhausted"] = True
                return None
            parts = urlsplit(current)
            host = str(parts.hostname or "").casefold()
            current = validate_https_url(current, [host])
            from .material_probe import assert_public_dns
            assert_public_dns(host, resolver, fake_ip_networks=fake_networks)
            budget["request_count"] = int(budget["request_count"]) + 1
            with active_client.stream("GET", current, headers={"User-Agent": "copy-skill-visual-anchor/1.0", "Accept": "image/jpeg,image/png,image/webp"}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        return None
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    return None
                mime_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
                if mime_type not in _IMAGE_TYPES:
                    return None
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > maximum_bytes or int(budget["downloaded_bytes"]) + len(chunk) > int(budget["max_total_bytes"]):
                        budget["downloaded_bytes"] = min(int(budget["max_total_bytes"]), int(budget["downloaded_bytes"]) + len(chunk))
                        budget["exhausted"] = True
                        return None
                    budget["downloaded_bytes"] = int(budget["downloaded_bytes"]) + len(chunk)
                    chunks.append(chunk)
                return mime_type, b"".join(chunks)
    except (MaterialProbeError, httpx.HTTPError, OSError, ValueError):
        return None
    finally:
        if owns_client:
            active_client.close()
    return None


def _load_story_payload(config: dict[str, Any], value: str | Path | dict[str, Any] | None, settings: dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    path = _project_path(config, value or settings["stories_path"])
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VisualAnchorError(f"新闻输入不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise VisualAnchorError("新闻输入不是有效 JSON") from exc


def run_visual_anchor_batch(
    config: dict[str, Any], stories_path: str | Path | dict[str, Any] | None = None, *, client: httpx.Client | None = None, resolver: Callable[..., Any] = socket.getaddrinfo, douyin_fallback: bool = False, douyin_provider: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    settings = config["jobs"]["visual_anchor"]
    payload = _load_story_payload(config, stories_path, settings)
    rows = payload.get("stories") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise VisualAnchorError("新闻批次必须包含 stories 数组")
    if len(rows) > int(settings["max_stories"]):
        raise VisualAnchorError("V1 每批最多处理 3 条新闻")
    stories = [validate_visual_story(row, settings) for row in rows]
    state = JobState(config, "visual_anchor")
    results: list[dict[str, Any]] = []
    with JobLock(config, "visual_anchor"):
        state.update(status="running", phase="source_images", output_path="", errors=[], counts={"stories": len(stories)})
        for story in stories:
            try:
                results.append(_run_story(config, story, settings, client=client, resolver=resolver, douyin_fallback=douyin_fallback, douyin_provider=douyin_provider))
            except Exception as exc:
                results.append({"story_id": story["story_id"], "status": "failed", "error": _safe_error(exc)})
        statuses = [row["status"] for row in results]
        status = "success" if statuses and all(value == "success" for value in statuses) else "needs_login" if "needs_login" in statuses and not any(value in {"success", "partial"} for value in statuses) else "partial" if any(value in {"success", "partial"} for value in statuses) else "failed"
        counts = {"stories": len(results), "success": statuses.count("success"), "partial": statuses.count("partial"), "failed": statuses.count("failed"), "needs_login": statuses.count("needs_login"), "assets": sum(int((row.get("counts") or {}).get("assets") or 0) for row in results)}
        latest = next((row.get("output_path") for row in reversed(results) if row.get("output_path")), "")
        state.update(status=status, phase="complete", output_path=latest, errors=[row.get("error") for row in results if row.get("error")], counts=counts)
        return {"status": status, "stories": results, "counts": counts, "output_path": latest}


def _domestic_summary_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# 国内四条新闻视觉素材真实获取测试", "",
        f"状态：`{summary['status']}`；故事：{summary['counts']['stories']}；素材：{summary['counts']['assets']}", "",
        "所有素材仅用于视觉验收；事实仍以输入来源为准。`review_required`/`reference_only` 未经权利确认不能自动进入成片。", "",
    ]
    for row in summary["stories"]:
        lines.extend([f"## {row['title_zh']}", "", f"状态：`{row['status']}`", ""])
        for index, asset in enumerate(row["assets"]):
            role = "主图" if index == 0 else f"备用 {index}"
            lines.extend([
                f"- {role}：`{asset['absolute_local_path']}`（{asset['width']}×{asset['height']}）",
                f"  - 来源：[{asset['source_kind']}]({asset['source_article_url']})；权利：`{asset['rights_status']}`",
                f"  - 相关度：`{asset.get('relevance_status', 'exact_subject')}`；得分：{asset['score']}；原因：{asset['selection_reason']}",
                f"  - 署名：{asset['attribution_text'] or '来源页未提供单独摄影署名；需人工权利复核'}",
            ])
            if asset.get("visual_qa"):
                qa = asset["visual_qa"]
                lines.append(
                    "  - 视觉QA："
                    f"{qa.get('content', '')}；对应：{qa.get('correspondence', '')}；"
                    f"重复/错图：{qa.get('duplicate_or_wrong', '')}；水印/文字：{qa.get('watermark_text', '')}；"
                    f"清晰度：{qa.get('clarity', '')}；版式：{qa.get('orientation_use', '')}"
                )
        if not row["assets"]:
            lines.append("- 未取得合格素材。")
        if row.get("missing_asset_reason"):
            lines.append(f"- 缺图原因：{row['missing_asset_reason']}")
        lines.extend([
            f"- 请求/字节/耗时：{row['budget']['request_count']} / {row['budget']['downloaded_bytes']} / {row['budget']['elapsed_seconds']} 秒",
            f"- RSS触发：{row['rss']['triggered']}；抖音状态：`{row['douyin']['status']}`；失败：{len(row['failures'])}", "",
        ])
    return "\n".join(lines)


def _load_visual_qa(root: Path) -> dict[str, dict[str, Any]]:
    path = root / "visual-qa.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    rows = payload.get("assets") if isinstance(payload, dict) else None
    return {str(key): value for key, value in (rows or {}).items() if isinstance(value, dict)}


def run_domestic_visual_anchor_smoke(
    config: dict[str, Any], *, douyin_fallback: bool = False, client: httpx.Client | None = None,
    resolver: Callable[..., Any] = socket.getaddrinfo, douyin_provider: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the frozen domestic 3+1 acceptance batches and publish one atomic summary."""
    domestic = config["jobs"]["visual_anchor"]["domestic_smoke"]
    root = _project_path(config, str(domestic["output_root"])) / str(domestic["target_date"])
    visual_qa = _load_visual_qa(root)
    batch_paths = [str(value) for value in domestic["batch_paths"]]
    if len(batch_paths) != 2:
        raise VisualAnchorError("国内验收必须使用两个冻结批次")
    payloads = [_load_story_payload(config, value, config["jobs"]["visual_anchor"]) for value in batch_paths]
    batch_story_counts = [len(payload.get("stories") or []) if isinstance(payload, dict) else 0 for payload in payloads]
    if batch_story_counts != [3, 1]:
        raise VisualAnchorError("国内验收必须严格按3+1两个批次执行")
    frozen_rows = [row for payload in payloads for row in payload["stories"]]
    story_ids = [str(row.get("story_id") or "") for row in frozen_rows]
    if len(set(story_ids)) != 4 or any(str(row.get("target_date") or "") != str(domestic["target_date"]) for row in frozen_rows):
        raise VisualAnchorError("国内验收必须包含4条不重复且日期一致的冻结新闻")
    all_results: list[dict[str, Any]] = []
    for batch_path in batch_paths:
        selected_config = copy.deepcopy(config)
        selected_config["jobs"]["visual_anchor"]["output_root"] = str(domestic["output_root"])
        all_results.append(run_visual_anchor_batch(
            selected_config, batch_path, client=client, resolver=resolver,
            douyin_fallback=douyin_fallback, douyin_provider=douyin_provider,
        ))

    stories: list[dict[str, Any]] = []
    for batch in all_results:
        for result in batch.get("stories") or []:
            manifest_path = Path(str(result.get("manifest_path") or ""))
            if not manifest_path.is_file():
                stories.append({
                    "story_id": str(result.get("story_id") or "unknown"), "title_zh": str(result.get("story_id") or "unknown"),
                    "status": "failed", "assets": [], "failures": [{"source": "batch", "message": str(result.get("error") or "未生成manifest")}],
                    "budget": {"request_count": 0, "downloaded_bytes": 0, "elapsed_seconds": 0},
                    "rss": {"triggered": False}, "douyin": {"status": "not_run", "audio_attempted": 0},
                })
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            story = json.loads((manifest_path.parent / "story.json").read_text(encoding="utf-8"))
            assets = []
            for asset in manifest["assets"]:
                row = dict(asset)
                row["absolute_local_path"] = str((manifest_path.parent / row["local_path"]).resolve())
                row["relevance_status"] = "exact_subject"
                if row.get("asset_id") in visual_qa:
                    row["visual_qa"] = visual_qa[row["asset_id"]]
                assets.append(row)
            stories.append({
                "story_id": manifest["story_id"], "title_zh": story["title_zh"], "status": manifest["status"],
                "output_dir": str(manifest_path.parent.resolve()), "preview_path": str((manifest_path.parent / "preview.md").resolve()),
                "manifest_path": str(manifest_path.resolve()), "assets": assets, "failures": manifest["failures"],
                "missing_asset_reason": manifest.get("missing_asset_reason", ""),
                "budget": manifest["budget"], "rss": manifest["rss"], "douyin": manifest["douyin"],
            })
    statuses = [row["status"] for row in stories]
    status = "success" if len(stories) == 4 and all(value == "success" for value in statuses) else "partial" if any(row["assets"] for row in stories) else "needs_login" if "needs_login" in statuses else "failed"
    summary = {
        "schema_version": "1.0", "status": status, "generated_at": now_iso(str(config["timezone"])),
        "execution": {"mode": "sequential_3_plus_1", "batch_count": 2, "batch_story_counts": batch_story_counts},
        "counts": {"stories": len(stories), "success": statuses.count("success"), "partial": statuses.count("partial"), "failed": statuses.count("failed"), "needs_login": statuses.count("needs_login"), "assets": sum(len(row["assets"]) for row in stories)},
        "stories": stories,
        "boundaries": {"openmontage_modified": False, "third_party_modified": False, "llm_calls": 0, "asr_calls": 0, "audio_attempted": sum(int(row["douyin"].get("audio_attempted") or 0) for row in stories)},
    }
    _atomic_json(root / "domestic-summary.json", summary)
    (root / "domestic-summary.md").write_text(_domestic_summary_markdown(summary), encoding="utf-8", newline="\n")
    return {**summary, "output_path": str((root / "domestic-summary.md").resolve()), "json_path": str((root / "domestic-summary.json").resolve())}


def summarize_domestic_visual_anchor_outputs(config: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the unified report from the frozen 3+1 manifests without any network access."""
    domestic = config["jobs"]["visual_anchor"]["domestic_smoke"]
    payloads = [_load_story_payload(config, value, config["jobs"]["visual_anchor"]) for value in domestic["batch_paths"]]
    batch_story_counts = [len(payload.get("stories") or []) if isinstance(payload, dict) else 0 for payload in payloads]
    if batch_story_counts != [3, 1]:
        raise VisualAnchorError("国内验收必须严格按3+1两个批次执行")
    frozen_rows = [row for payload in payloads for row in payload["stories"]]
    root = _project_path(config, str(domestic["output_root"])) / str(domestic["target_date"])
    visual_qa = _load_visual_qa(root)
    stories: list[dict[str, Any]] = []
    for frozen in frozen_rows:
        story_id = str(frozen.get("story_id") or "")
        story_root = root / story_id
        manifest_path = story_root / "manifest.json"
        if not manifest_path.is_file():
            stories.append({
                "story_id": story_id, "title_zh": str(frozen.get("title_zh") or story_id), "status": "failed",
                "assets": [], "failures": [{"source": "summary", "message": "未生成manifest"}],
                "missing_asset_reason": "本地没有该冻结新闻的完成输出。",
                "budget": {"request_count": 0, "downloaded_bytes": 0, "elapsed_seconds": 0},
                "rss": {"triggered": False}, "douyin": {"status": "not_run", "audio_attempted": 0},
            })
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assets = []
        for asset in manifest.get("assets") or []:
            row = dict(asset)
            row["absolute_local_path"] = str((story_root / row["local_path"]).resolve())
            row["relevance_status"] = "exact_subject"
            if row.get("asset_id") in visual_qa:
                row["visual_qa"] = visual_qa[row["asset_id"]]
            assets.append(row)
        stories.append({
            "story_id": story_id, "title_zh": str(frozen.get("title_zh") or story_id), "status": manifest["status"],
            "output_dir": str(story_root.resolve()), "preview_path": str((story_root / "preview.md").resolve()),
            "manifest_path": str(manifest_path.resolve()), "assets": assets, "failures": manifest.get("failures") or [],
            "missing_asset_reason": str(manifest.get("missing_asset_reason") or ""),
            "budget": manifest["budget"], "rss": manifest["rss"], "douyin": manifest["douyin"],
        })
    statuses = [row["status"] for row in stories]
    status = "success" if len(stories) == 4 and all(value == "success" for value in statuses) else "partial" if any(row["assets"] for row in stories) else "needs_login" if "needs_login" in statuses else "failed"
    summary = {
        "schema_version": "1.0", "status": status, "generated_at": now_iso(str(config["timezone"])),
        "execution": {"mode": "sequential_3_plus_1", "batch_count": 2, "batch_story_counts": batch_story_counts, "summary_rebuilt_without_network": True},
        "counts": {"stories": len(stories), "success": statuses.count("success"), "partial": statuses.count("partial"), "failed": statuses.count("failed"), "needs_login": statuses.count("needs_login"), "assets": sum(len(row["assets"]) for row in stories)},
        "stories": stories,
        "boundaries": {"openmontage_modified": False, "third_party_modified": False, "llm_calls": 0, "asr_calls": 0, "audio_attempted": sum(int(row["douyin"].get("audio_attempted") or 0) for row in stories)},
    }
    _atomic_json(root / "domestic-summary.json", summary)
    (root / "domestic-summary.md").write_text(_domestic_summary_markdown(summary), encoding="utf-8", newline="\n")
    return {**summary, "output_path": str((root / "domestic-summary.md").resolve()), "json_path": str((root / "domestic-summary.json").resolve())}


def latest_visual_anchor_report(config: dict[str, Any]) -> Path | None:
    root = _project_path(config, config["jobs"]["visual_anchor"]["output_root"])
    reports = list(root.glob("*/*/preview.md")) if root.exists() else []
    return max(reports, key=lambda path: path.stat().st_mtime) if reports else None


def check_visual_anchor_rss_health(
    config: dict[str, Any], *, client: httpx.Client | None = None, resolver: Callable[..., Any] = socket.getaddrinfo,
) -> dict[str, Any]:
    settings = config["jobs"]["visual_anchor"]
    sources = [row for row in settings.get("rss_sources") or [] if isinstance(row, dict) and row.get("enabled", True)]
    budget = RequestBudget(max(1, len(sources)), int(settings["max_html_bytes"]) * max(1, len(sources)), 45.0, time.monotonic())
    fetch_settings = {**settings, "max_requests": max(1, len(sources)), "max_total_bytes": budget.max_total_bytes, "total_timeout_seconds": 45}
    fetcher = SafeFetcher(fetch_settings, budget, client=client, resolver=resolver)
    rows: list[dict[str, Any]] = []
    try:
        for source in sources:
            try:
                final_url, content_type, data = fetcher.get(
                    str(source.get("url") or ""), maximum_bytes=int(settings["max_html_bytes"]),
                    accepted_types=("application/rss+xml", "application/atom+xml", "application/xml", "text/xml"),
                )
                candidates = extract_rss_image_candidates(final_url, data, settings["allowed_domains"])
                rows.append({
                    "name": str(source.get("name") or "RSS")[:120], "status": "reachable_with_images" if candidates else "reachable_no_images",
                    "content_type": content_type, "bytes": len(data), "image_candidates": len(candidates),
                    "image_kinds": sorted({str(row["extraction_kind"]) for row in candidates}), "url": redact_url(final_url),
                })
            except (MaterialProbeError, httpx.HTTPError, ValueError) as exc:
                rows.append({"name": str(source.get("name") or "RSS")[:120], "status": "unreachable", "error": _safe_error(exc)})
    finally:
        fetcher.close()
    result = {
        "schema_version": "1.0", "checked_at": now_iso(str(config["timezone"])),
        "status": "success" if rows and all(row["status"].startswith("reachable") for row in rows) else "partial",
        "sources": rows, "budget": budget.snapshot(), "fact_confirmation_role": False,
    }
    output = _project_path(config, settings["output_root"]) / "rss-health.json"
    _atomic_json(output, result)
    return {**result, "output_path": str(output.resolve())}
