"""Theme expansion and delivery-folder naming for material replication.

The expansion is fully offline and deterministic: it derives synonyms,
category/attribute combinations, brand-attribute pairs and brand-category pairs
from a small, auditable hint table, and weaves in in-domain intent suffixes
(``实测/开箱/上手`` ...) early enough that they survive the caller's budget cap.
A theme therefore always yields at least ``min_keywords`` non-duplicate
keywords, and a themed search always keeps some hands-on/real-shot intent
queries.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any


_INVALID_CHARS = re.compile(r'[\\/:*?"<>|]')
_CONTROL_CHARS = re.compile(r"[\x00-\x1f]")
_WHITESPACE = re.compile(r"\s+")

# Brand/company aliases that stay inside the theme's semantic domain.
_BRAND_ALIASES: dict[str, tuple[str, ...]] = {
    "苹果": ("Apple", "iPhone"),
    "apple": ("苹果",),
    "华为": ("Huawei",),
    "小米": ("Xiaomi",),
    "三星": ("Samsung",),
    "特斯拉": ("Tesla",),
    "英伟达": ("NVIDIA",),
    "谷歌": ("Google",),
    "微软": ("Microsoft",),
    "索尼": ("Sony",),
    "荣耀": ("HONOR",),
    "魅族": ("MEIZU",),
}

# Product categories that may appear inside a theme; the tuple lists the
# borrowable attributes worth a dedicated search term.
_CATEGORY_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    "折叠屏": ("折痕", "铰链", "开合"),
    "手机": ("外观", "屏幕"),
    "芯片": ("性能", "跑分"),
    "机器人": ("演示", "交互"),
    "汽车": ("内饰", "智能驾驶"),
    "眼镜": ("佩戴", "显示"),
    "耳机": ("降噪", "音质"),
    "电脑": ("性能", "散热"),
    "笔记本": ("性能", "屏幕"),
    "平板": ("屏幕", "手写"),
    "手表": ("续航", "健康"),
    "相机": ("画质", "样张"),
    "无人机": ("航拍", "避障"),
    "显卡": ("性能", "散热"),
    "系统": ("界面", "功能"),
}

# In-domain intent suffixes ("上手/实拍" style).  These carry the strongest
# signal for finding hands-on / real-shot clips, so they are interleaved early
# (see ``_LANE_CYCLE``) instead of being appended as a last-resort pad.
_FALLBACK_SUFFIXES = ("实测", "开箱", "对比", "评测", "上手", "新品")

# Deterministic round-robin order over the keyword "lanes".  Intent words carry
# a double weight (three entries per cycle) so at least three of them survive
# inside ``max_keywords`` -- and inside the crawler's ``budget // 10`` keyword
# truncation in ``search_collector``.  The previous strict-priority ordering let
# the brand/category tiers fill every slot and pushed *all* intent words past
# the cut, so a theme like ``苹果折叠屏`` produced no "上手"-style query at all.
_LANE_CYCLE: tuple[str, ...] = (
    "intent",
    "alias",
    "category_attribute",
    "intent",
    "brand_attribute",
    "alias",
    "intent",
    "category_attribute",
)


def _replace_casefold(text: str, needle: str, replacement: str) -> str:
    pattern = re.compile(re.escape(needle), re.IGNORECASE)
    return pattern.sub(replacement, text, count=1)


def sanitize_theme(theme: str, *, max_length: int = 12) -> str:
    """Strip filesystem-illegal characters and bound the theme length."""
    text = _CONTROL_CHARS.sub("", str(theme or ""))
    text = _INVALID_CHARS.sub("", text).strip()
    text = _WHITESPACE.sub(" ", text)
    if max_length > 0 and len(text) > max_length:
        text = text[:max_length].strip()
    return text


def _alias_terms(base: str, folded: str) -> list[str]:
    """``base`` with the theme's brand swapped for each known alias."""
    terms: list[str] = []
    for brand, aliases in _BRAND_ALIASES.items():
        if brand.casefold() in folded:
            for alias in aliases:
                terms.append(_replace_casefold(base, brand, alias))
    return terms


def _category_attribute_terms(folded: str) -> list[str]:
    """``{category} {attribute}`` terms for every category present in the theme."""
    terms: list[str] = []
    for category, attributes in _CATEGORY_ATTRIBUTES.items():
        if category.casefold() in folded:
            for attribute in attributes:
                terms.append(f"{category} {attribute}")
    return terms


def _brand_attribute_terms(folded: str) -> list[str]:
    """``{brand|alias}{category} {attribute}`` pairings.

    E.g. ``iPhone折叠屏 折痕`` / ``Apple折叠屏 铰链``.  These stay inside the
    theme's semantic domain while adding a brand-qualified attribute angle the
    plain category/attribute tier cannot express.
    """
    terms: list[str] = []
    for brand, aliases in _BRAND_ALIASES.items():
        if brand.casefold() not in folded:
            continue
        prefixes = (brand, *aliases)
        for category, attributes in _CATEGORY_ATTRIBUTES.items():
            if category.casefold() not in folded or brand.casefold() == category.casefold():
                continue
            for attribute in attributes:
                for prefix in prefixes:
                    terms.append(f"{prefix}{category} {attribute}")
    return terms


def _brand_category_terms(folded: str) -> list[str]:
    """``{brand}{category}`` pairs, kept for themes whose base is not the pair."""
    terms: list[str] = []
    for brand in _BRAND_ALIASES:
        if brand.casefold() not in folded:
            continue
        for category in _CATEGORY_ATTRIBUTES:
            if category.casefold() in folded and brand.casefold() != category.casefold():
                terms.append(f"{brand}{category}")
    return terms


def expand_keywords(theme: str, config: dict[str, Any]) -> list[str]:
    """Expand ``theme`` into ``min_keywords``~``max_keywords`` in-domain keywords.

    Lanes are interleaved deterministically (``_LANE_CYCLE``) rather than ranked
    by tier, so a themed search always keeps a usable mix of *intent*
    ("上手/实拍" style) and *category-attribute* queries even after the caller
    truncates the list to its per-run budget.  Fully offline and deterministic;
    duplicates are removed case-insensitively.
    """
    settings = (config.get("jobs") or {}).get("material_replication") or {}
    min_keywords = max(1, int(settings.get("min_keywords") or 3))
    max_keywords = max(min_keywords, min(10, int(settings.get("max_keywords") or 6)))
    theme_max = max(1, int(settings.get("theme_max_chars") or 12))
    base = sanitize_theme(theme, max_length=max(theme_max, min(64, theme_max * 4)))
    if not base:
        return []

    ordered: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        text = str(value).strip()
        if not text:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        ordered.append(text)

    add(base)
    folded = base.casefold()
    lanes: dict[str, list[str]] = {
        "alias": _alias_terms(base, folded),
        "category_attribute": _category_attribute_terms(folded),
        "brand_attribute": _brand_attribute_terms(folded),
        "brand_category": _brand_category_terms(folded),
        "intent": [f"{base} {suffix}" for suffix in _FALLBACK_SUFFIXES],
    }
    cursors = {lane: 0 for lane in lanes}
    while len(ordered) < max_keywords:
        consumed = False
        for lane in _LANE_CYCLE:
            index = cursors[lane]
            if index >= len(lanes[lane]):
                continue
            cursors[lane] = index + 1
            consumed = True
            add(lanes[lane][index])
            if len(ordered) >= max_keywords:
                break
        if not consumed:
            break
    return ordered[:max_keywords]


def delivery_folder_name(business_date: str, theme: str, *, max_path_chars: int = 260) -> str:
    """Build the ``MM.DD<主题>复刻视频`` folder name (month not zero-padded)."""
    try:
        parsed = date.fromisoformat(str(business_date))
    except ValueError as exc:
        raise ValueError("business_date 必须是 YYYY-MM-DD") from exc
    max_theme = 12
    safe = sanitize_theme(theme, max_length=max_theme) or "未命名主题"
    prefix = f"{parsed.month}.{parsed.day:02d}"
    suffix = "复刻视频"
    folder = f"{prefix}{safe}{suffix}"
    while len(folder) > max(1, int(max_path_chars)) and len(safe) > 1:
        safe = safe[:-1].strip()
        folder = f"{prefix}{safe}{suffix}"
    return folder


def project_path(config: dict[str, Any], value: str | Path) -> Path:
    """Resolve a project-relative path, honouring an injected ``_project_root``."""
    path = Path(value)
    if path.is_absolute():
        return path
    root = Path(str(config.get("_project_root") or Path(__file__).resolve().parents[2]))
    return root / path
