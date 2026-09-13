from __future__ import annotations

import pytest

from douyin_intelligence.config import load_config
from douyin_intelligence.replication_theme import (
    _CATEGORY_ATTRIBUTES,
    delivery_folder_name,
    expand_keywords,
    sanitize_theme,
)


_INTENT_SUFFIXES = ("实测", "开箱", "对比", "评测", "上手", "新品")


def _is_intent(keyword: str) -> bool:
    return any(keyword.endswith(f" {suffix}") for suffix in _INTENT_SUFFIXES)


def _keywords_setting(config: dict, key: str) -> int:
    return int(config["jobs"]["material_replication"][key])


def test_expand_keywords_is_bounded_deduplicated_and_in_domain() -> None:
    config = load_config()
    keywords = expand_keywords("苹果折叠屏手机", config)
    # Bounds are derived from the live config: the previous hard-coded ``<= 6``
    # mirrored the old ``max_keywords: 6``; the value is now 10 so the upper
    # bound must track the setting instead of a frozen literal.
    assert _keywords_setting(config, "min_keywords") <= len(keywords) <= min(10, _keywords_setting(config, "max_keywords"))
    assert len(keywords) == len(set(keywords))
    assert keywords[0] == "苹果折叠屏手机"
    # Every derived keyword must stay inside the theme's semantic domain.
    for keyword in keywords:
        assert any(token in keyword for token in ("苹果", "Apple", "iPhone", "折叠屏", "手机")), keyword
    # Deterministic across invocations.
    assert expand_keywords("苹果折叠屏手机", config) == keywords


def test_expand_keywords_keeps_intent_and_attribute_lanes_for_folding_theme() -> None:
    """苹果折叠屏 must actually carry "上手/实拍"-style intent queries.

    Regression guard: a strict tier ordering filled every slot with
    brand/category terms and silently dropped the whole intent tier, so the
    result was searched with no hands-on/real-shot query at all.
    """
    config = load_config()
    keywords = expand_keywords("苹果折叠屏", config)

    assert _keywords_setting(config, "min_keywords") <= len(keywords) <= min(10, _keywords_setting(config, "max_keywords"))
    assert keywords[0] == "苹果折叠屏"

    intent = [keyword for keyword in keywords if _is_intent(keyword)]
    assert len(intent) >= 3, keywords

    category_attributes = {f"折叠屏 {attribute}" for attribute in _CATEGORY_ATTRIBUTES["折叠屏"]}
    assert len([keyword for keyword in keywords if keyword in category_attributes]) >= 2, keywords

    assert {"Apple折叠屏", "iPhone折叠屏"} <= set(keywords)

    brand_attribute = {f"{prefix}折叠屏 {attribute}" for prefix in ("苹果", "Apple", "iPhone") for attribute in _CATEGORY_ATTRIBUTES["折叠屏"]}
    assert any(keyword in brand_attribute for keyword in keywords), keywords


def test_intent_keywords_survive_the_crawler_budget_truncation() -> None:
    """The crawler searches only ``pool_budget // 10`` keywords (8 at the default
    pool of 80), so at least three intent queries must fit in that window."""
    config = load_config()
    pool_size = config["jobs"]["material_replication"]["default_pool_size"]
    searched = expand_keywords("苹果折叠屏", config)[: pool_size // 10]
    assert sum(1 for keyword in searched if _is_intent(keyword)) >= 3, searched


def test_expand_keywords_is_casefold_deduplicated() -> None:
    config = load_config()
    keywords = expand_keywords("苹果折叠屏", config)
    folded = [keyword.casefold() for keyword in keywords]
    assert len(folded) == len(set(folded))


def test_expand_keywords_always_reaches_minimum_for_unknown_theme() -> None:
    config = load_config()
    min_keywords = config["jobs"]["material_replication"]["min_keywords"]
    keywords = expand_keywords("量子计算", config)
    assert len(keywords) >= min_keywords
    assert keywords[0] == "量子计算"
    assert all(keyword.startswith("量子计算") for keyword in keywords)


def test_expand_keywords_boundary_theme_without_brand_or_category_uses_intent() -> None:
    # A theme with no known brand and no known category can only expand via the
    # intent lane; it must still reach ``min_keywords`` and stay deterministic.
    config = load_config()
    min_keywords = config["jobs"]["material_replication"]["min_keywords"]
    keywords = expand_keywords("量子计算", config)
    assert len(keywords) >= min_keywords
    assert len([keyword for keyword in keywords if _is_intent(keyword)]) >= min_keywords
    assert expand_keywords("量子计算", config) == keywords


def test_expand_keywords_empty_theme_returns_empty() -> None:
    config = load_config()
    assert expand_keywords("   ", config) == []


def test_sanitize_theme_strips_illegal_characters_and_bounds_length() -> None:
    assert sanitize_theme('苹果/折叠:屏*手机?"<>|') == "苹果折叠屏手机"
    assert sanitize_theme("一二三四五六七八九十十一十二十三") == "一二三四五六七八九十十一"
    assert sanitize_theme("") == ""


def test_delivery_folder_name_uses_month_without_zero_padding() -> None:
    assert delivery_folder_name("2026-09-12", "苹果折叠屏") == "9.12苹果折叠屏复刻视频"
    assert delivery_folder_name("2026-01-05", "芯片") == "1.05芯片复刻视频"


def test_delivery_folder_name_rejects_bad_date() -> None:
    with pytest.raises(ValueError):
        delivery_folder_name("2026/09/12", "苹果")
