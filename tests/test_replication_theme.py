from __future__ import annotations

import json
from pathlib import Path

import pytest

from douyin_intelligence.config import load_config
from douyin_intelligence.replication_theme import (
    _CATEGORY_ATTRIBUTES,
    _SUBJECT_ALIASES,
    _subject_split_terms,
    delivery_folder_name,
    expand_keywords,
    sanitize_theme,
    subject_terms,
)


_INTENT_SUFFIXES = ("实测", "开箱", "对比", "评测", "上手", "新品")

# The real 9.12 pool artifact (``output/`` is gitignored) frozen under
# ``tests/fixtures/``: its ``keywords_requested`` is exactly what
# ``expand_keywords("苹果折叠屏", config)`` produced before subject
# qualification, so it pins "themes with nothing detachable are untouched"
# against a genuine delivery instead of a hand-written list.
_APPLE_FOLD_FIXTURE = Path(__file__).parent / "fixtures" / "apple_fold_pool.json"

# The 9.14 corpus' eleven real themes (recovered from each delivery's 清单.json,
# not from the truncated folder names) and the ``subject_terms`` vocabulary each
# produced *before* T1b's script-boundary split.  Offline recomputation:
# ``.tmp/predict_gate_0914.py``.
_PRE_SPLIT_VOCAB: dict[str, list[str]] = {
    "DeepSeek V4.1 Flash": ["DeepSeek", "V4.1", "Flash"],
    "iRobot Roomba 875 扫地机器人": ["iRobot", "Roomba", "875", "扫地机器人", "扫地机"],
    "Microduck 机械鸭机器人": ["Microduck", "机械鸭", "机器鸭", "机械鸭子"],
    "充电宝3C认证新规": ["充电宝3C认证新规"],
    "内存涨价 最贵装机季": ["内存涨价", "最贵装机季"],
    "华为Mate XT2 非凡大师": ["华为Mate", "XT2", "非凡大师"],
    "华为昇腾950DT涨价": ["华为昇腾950DT涨价"],
    "大疆 Osmo Pocket 4 Pro": ["大疆", "Osmo", "Pocket", "4", "Pro"],
    "显卡涨价 RTX5090": ["显卡涨价", "RTX5090"],
    "特斯拉 Cybercab 无人驾驶": ["特斯拉", "Cybercab", "无人驾驶"],
    "苹果 iPhone Duo 折叠屏": ["苹果", "iPhone", "Duo", "折叠机", "折屏"],
}

# The two whitespace-free themes whose single whole-phrase token admitted 0/31
# and 0/17 candidates: T1b's script-boundary split is *supposed* to change these.
_HOLE_THEMES = ("充电宝3C认证新规", "华为昇腾950DT涨价")

# Regression lock: nine of the eleven themes have no multi-script token to split
# and must keep their vocabulary byte-for-byte (acceptance criterion A5).
_UNCHANGED_VOCAB: dict[str, list[str]] = {
    theme: terms for theme, terms in _PRE_SPLIT_VOCAB.items() if theme not in _HOLE_THEMES
}

_ALIAS_VOCAB: set[str] = {alias for values in _SUBJECT_ALIASES.values() for alias in values}


def _bare_category_pairs() -> set[str]:
    return {
        f"{category} {attribute}"
        for category, attributes in _CATEGORY_ATTRIBUTES.items()
        for attribute in attributes
    }


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


def test_delivery_folder_name_keeps_long_theme() -> None:
    # Regression: the folder-name theme cap used to be 12 chars, which chopped
    # real themes mid-word (e.g. "iRobot Roomba 875 扫地机器人" -> "...Roomb").
    # The cap is now 24, so the longest of our themes (23 chars) survives whole.
    assert (
        delivery_folder_name("2026-09-14", "iRobot Roomba 875 扫地机器人")
        == "9.14iRobot Roomba 875 扫地机器人复刻视频"
    )
    # A theme longer than the cap is still bounded by ``max_path_chars``.
    folder = delivery_folder_name("2026-09-14", "超" * 60, max_path_chars=30)
    assert len(folder) <= 30


def test_delivery_folder_name_rejects_bad_date() -> None:
    with pytest.raises(ValueError):
        delivery_folder_name("2026/09/12", "苹果")


def test_expand_keywords_qualifies_category_terms_with_the_subject() -> None:
    """``Microduck 机械鸭机器人`` must stop emitting 「机器人 演示」/「机器人 交互」.

    Regression: those two terms are a *category* query with no theme
    limitation, so they filled the 9.14 Microduck pool with unrelated (and much
    hotter) Unitree G1 videos, pushing every genuine product term past the
    download cut.  The theme must instead be searched by its subject
    (``Microduck 机械鸭``) and by the platform's own spelling (``机器鸭``).
    """
    config = load_config()
    keywords = expand_keywords("Microduck 机械鸭机器人", config)

    assert "机器人 演示" not in keywords and "机器人 交互" not in keywords, keywords
    assert keywords[0] == "Microduck 机械鸭机器人"
    assert "Microduck 机械鸭" in keywords
    assert "机器鸭" in keywords
    # Attribute and intent lanes are qualified by the subject, not by the theme tail.
    assert all("机械鸭机器人" not in keyword for keyword in keywords[1:]), keywords
    assert expand_keywords("Microduck 机械鸭机器人", config) == keywords


def test_expand_keywords_qualifies_a_compound_product_noun_category() -> None:
    """``扫地机器人`` is a product noun: 「机器人」 alone is another product class."""
    config = load_config()
    keywords = expand_keywords("iRobot Roomba 875 扫地机器人", config)

    assert "机器人 演示" not in keywords and "机器人 交互" not in keywords, keywords
    assert "iRobot Roomba 875 扫地机器人 演示" in keywords
    # The platform's own short spelling is what titles actually contain.
    assert "扫地机" in keywords


def test_expand_keywords_leaves_theme_without_a_detachable_suffix_untouched() -> None:
    """A theme that *belongs to* a category keeps its historical lane contents.

    ``苹果折叠屏`` is not a compound product noun (the remainder 「苹果」 is a
    brand, not a product we have on file), so nothing detaches and the issued
    terms must equal those of the frozen 9.12 delivery.
    """
    config = load_config()
    frozen = json.loads(_APPLE_FOLD_FIXTURE.read_text(encoding="utf-8"))

    assert expand_keywords("苹果折叠屏", config) == frozen["keywords_requested"]
    # Same rule for the spaced spelling and for a theme with no category at all.
    assert expand_keywords("显卡 性能", config) == expand_keywords("显卡 性能", config)
    assert "显卡 散热" in expand_keywords("显卡 性能", config)
    assert all(keyword.startswith("量子计算") for keyword in expand_keywords("量子计算", config))


def test_expand_keywords_never_emits_a_bare_category_pair_for_a_qualified_theme() -> None:
    config = load_config()
    bare = _bare_category_pairs()
    for theme in ("Microduck 机械鸭机器人", "iRobot Roomba 875 扫地机器人", "扫地机器人"):
        keywords = expand_keywords(theme, config)
        assert not (set(keywords) & bare), (theme, sorted(set(keywords) & bare))


def test_subject_terms_returns_head_tokens_plus_aliases() -> None:
    config = load_config()

    assert subject_terms("Microduck 机械鸭机器人", config) == ["Microduck", "机械鸭", "机器鸭", "机械鸭子"]
    # The stripped category word never becomes a relevance token.
    assert "机器人" not in subject_terms("Microduck 机械鸭机器人", config)
    assert subject_terms("iRobot Roomba 875 扫地机器人", config) == [
        "iRobot", "Roomba", "875", "扫地机器人", "扫地机",
    ]
    assert subject_terms("华为Mate XT2 非凡大师", config) == ["华为Mate", "XT2", "非凡大师"]
    assert subject_terms("   ", config) == []
    # Case-folded and stable across calls (it is consumed by other modules).
    terms = subject_terms("Microduck 机械鸭机器人", config)
    assert len(terms) == len({term.casefold() for term in terms})


def test_subject_aliases_config_extends_and_overrides_the_builtin_table() -> None:
    config = load_config()
    config["jobs"]["material_replication"]["subject_aliases"] = {"机械鸭": ["机器鸭", "DuckBot"]}

    assert subject_terms("Microduck 机械鸭机器人", config) == ["Microduck", "机械鸭", "机器鸭", "DuckBot"]
    assert "DuckBot" in expand_keywords("Microduck 机械鸭机器人", config)

    # An empty list disables the built-in entry instead of silently keeping it.
    config["jobs"]["material_replication"]["subject_aliases"] = {"机械鸭": []}
    assert subject_terms("Microduck 机械鸭机器人", config) == ["Microduck", "机械鸭"]
    # A malformed value is ignored, leaving the built-in table in charge.
    config["jobs"]["material_replication"]["subject_aliases"] = "不是字典"
    assert "机器鸭" in subject_terms("Microduck 机械鸭机器人", config)


def test_subject_terms_splits_a_whitespace_free_theme_on_script_boundaries() -> None:
    """T1b: a theme without spaces must not stay one whole-phrase token.

    ``term_hits_title`` matches a token as one whole phrase, so the single
    nine-character token admitted **nothing** (0/31 and 0/17 on the real 9.14
    pools) -- switching the gate on would have failed those periods outright.
    """
    config = load_config()

    assert subject_terms("充电宝3C认证新规", config) == ["充电宝3C认证新规", "充电宝", "3C", "认证新规"]
    assert subject_terms("华为昇腾950DT涨价", config) == ["华为昇腾950DT涨价", "华为昇腾", "950DT"]


def test_subject_terms_is_byte_identical_for_single_script_themes() -> None:
    """Regression lock ①: nothing to split, so nothing may change."""
    config = load_config()

    assert subject_terms("苹果折叠屏", config) == ["苹果折叠屏", "折叠机", "折屏"]
    assert subject_terms("RTX5090", config) == ["RTX5090"]


def test_subject_terms_keeps_the_nine_unchanged_9_14_vocabularies() -> None:
    """Regression lock ②: nine of the eleven real themes keep their vocabulary."""
    config = load_config()

    for theme, expected in _UNCHANGED_VOCAB.items():
        assert subject_terms(theme, config) == expected, theme


def test_subject_terms_abandons_a_split_that_yields_a_bare_brand() -> None:
    """All-or-nothing: 「华为Mate」 -> 华为 + Mate would admit every Huawei video."""
    config = load_config()

    assert subject_terms("华为Mate", config) == ["华为Mate"]
    assert subject_terms("华为Mate XT2 非凡大师", config) == ["华为Mate", "XT2", "非凡大师"]


def test_split_guard_never_splits_a_single_script_token() -> None:
    """Guard ①: a pure-CJK or pure-non-CJK token has nothing to split."""
    assert _subject_split_terms("苹果折叠屏") == []
    assert _subject_split_terms("RTX5090") == []
    assert _subject_split_terms("Microduck") == []


def test_split_guard_never_emits_a_pure_event_word() -> None:
    """Guard ②: 「涨价」 has no product identity, so it never stands alone."""
    config = load_config()

    assert _subject_split_terms("华为昇腾950DT涨价") == ["华为昇腾", "950DT"]
    assert _subject_split_terms("涨价") == []
    assert "涨价" not in subject_terms("华为昇腾950DT涨价", config)


def test_split_guard_is_all_or_nothing() -> None:
    """Guard ③: one non-specific run poisons the whole split."""
    assert _subject_split_terms("华为Mate") == []
    assert _subject_split_terms("华为P70") == []
    # 「内存」 is only 2 CJK characters: the split is dropped, not trimmed.
    assert _subject_split_terms("内存涨价") == []


def test_subject_terms_split_is_add_only_and_invents_no_vocabulary() -> None:
    """Invariant: the split only adds terms, and only terms the theme supplies."""
    config = load_config()

    for theme, pre_split in _PRE_SPLIT_VOCAB.items():
        terms = subject_terms(theme, config)
        # ① recall is monotonically non-decreasing: the old vocabulary survives.
        assert set(pre_split) <= set(terms), theme
        # ② add-only never reorders: the pre-split vocabulary stays the prefix.
        assert terms[: len(pre_split)] == pre_split, theme
        # ③ still case-fold de-duplicated (other modules consume it directly).
        assert len(terms) == len({term.casefold() for term in terms}), theme
        # ④ no invented vocabulary: every term comes from the theme or an alias.
        sanitized = sanitize_theme(theme, max_length=48)
        for term in terms:
            assert term in sanitized or term in _ALIAS_VOCAB, (theme, term)
        for added in terms[len(pre_split):]:
            assert added.casefold() in sanitized.casefold(), (theme, added)


# --------------------------------------------------------------------------- #
# ``theme_keywords`` / ``theme_subject_terms`` -- the trend-topic override.
#
# A trend / 行情 / 事件 topic is an editorial headline with no product model to
# name, so the wording-derived lanes emit queries no creator would ever type
# (9.14「内存涨价 最贵装机季」: 13 of its 26 pool candidates were refused by the
# relevance gate for exactly that reason).  Two *separate* opt-in tables let an
# operator supply the platform's own vocabulary -- one for the search words, one
# for the gate vocabulary -- because widening one must never silently widen the
# other.
# --------------------------------------------------------------------------- #

_TREND_THEME = "内存涨价 最贵装机季"
_TREND_KEYWORDS = ["内存 涨价", "DDR5 涨价", "内存条 价格"]
_TREND_SUBJECT = ["内存", "DDR5", "涨价"]


def _material_config(**material_keys: object) -> dict:
    """A synthetic config for the two override tables.

    Synthetic on purpose: an assertion pinned to whatever the *shipped* table
    happens to say today would only be testing the data file, and would rot the
    moment an operator edits it.
    """
    return {
        "jobs": {
            "material_replication": {"min_keywords": 3, "max_keywords": 10, **material_keys}
        }
    }


def test_theme_keywords_replace_the_derived_lanes() -> None:
    config = _material_config(theme_keywords={_TREND_THEME: list(_TREND_KEYWORDS)})
    keywords = expand_keywords(_TREND_THEME, config)

    assert keywords[0] == _TREND_THEME  # the theme itself still leads the list
    assert set(_TREND_KEYWORDS) <= set(keywords)
    # The whole point: no wording-derived lane survives.
    assert not any(_is_intent(keyword) for keyword in keywords), keywords


def test_theme_keywords_absent_is_byte_identical() -> None:
    baseline = expand_keywords(_TREND_THEME, _material_config())
    assert any(_is_intent(keyword) for keyword in baseline), baseline  # lanes did run

    for absent in (
        {},
        {"theme_keywords": None},
        {"theme_keywords": []},
        {"theme_keywords": {_TREND_THEME: "not-a-list"}},
        {"theme_keywords": {_TREND_THEME: []}},
        {"theme_keywords": {"其他主题": list(_TREND_KEYWORDS)}},
    ):
        assert expand_keywords(_TREND_THEME, _material_config(**absent)) == baseline, absent


def test_theme_keywords_accepts_the_sanitized_base_as_key() -> None:
    raw = "内存涨价 最贵装机季*"
    base = sanitize_theme(raw, max_length=48)
    assert base != raw

    config = _material_config(theme_keywords={base: list(_TREND_KEYWORDS)})
    keywords = expand_keywords(raw, config)

    assert keywords[0] == base
    assert set(_TREND_KEYWORDS) <= set(keywords)
    assert not any(_is_intent(keyword) for keyword in keywords), keywords


def test_theme_subject_terms_are_added_not_substituted() -> None:
    before = subject_terms(_TREND_THEME, _material_config())
    after = subject_terms(
        _TREND_THEME, _material_config(theme_subject_terms={_TREND_THEME: list(_TREND_SUBJECT)})
    )

    assert after != before  # genuinely widens recall
    assert after[: len(before)] == before  # add-only never reorders
    assert set(before) <= set(after)
    assert set(_TREND_SUBJECT) <= set(after)
    assert len(after) == len({term.casefold() for term in after})


def test_theme_subject_terms_absent_is_byte_identical() -> None:
    baseline = subject_terms(_TREND_THEME, _material_config())

    for absent in (
        {},
        {"theme_subject_terms": None},
        {"theme_subject_terms": {}},
        {"theme_subject_terms": {_TREND_THEME: "内存"}},
        {"theme_subject_terms": {_TREND_THEME: []}},
        {"theme_subject_terms": {"其他主题": list(_TREND_SUBJECT)}},
    ):
        assert subject_terms(_TREND_THEME, _material_config(**absent)) == baseline, absent


def test_the_two_override_tables_are_independent() -> None:
    """Widening the search words must not widen the gate, and vice versa.

    This is the whole reason the tables are separate keys.  A bare category word
    in the subject vocabulary is what let a competitor's video through on the
    9.14 Roomba run; it must take a deliberate, named edit to do that.
    """
    plain = _material_config()

    keywords_only = _material_config(theme_keywords={_TREND_THEME: list(_TREND_KEYWORDS)})
    assert subject_terms(_TREND_THEME, keywords_only) == subject_terms(_TREND_THEME, plain)

    subject_only = _material_config(theme_subject_terms={_TREND_THEME: list(_TREND_SUBJECT)})
    assert expand_keywords(_TREND_THEME, subject_only) == expand_keywords(_TREND_THEME, plain)


def test_shipped_override_tables_are_effective() -> None:
    """Whatever the delivered tables name, each entry must actually take effect.

    Self-consistency rather than a hard-coded expectation, on purpose: this is
    the shape of guard that would have caught the 9.14 ``theme=theme`` defect,
    where the feature was committed and unit-tested while the pipeline never
    passed the argument that switched it on.
    """
    config = load_config()
    material = config["jobs"]["material_replication"]

    for theme, terms in (material.get("theme_keywords") or {}).items():
        assert terms, theme
        keywords = expand_keywords(theme, config)
        assert set(terms) <= set(keywords), theme
        assert not any(_is_intent(keyword) for keyword in keywords), (theme, keywords)

    for theme, terms in (material.get("theme_subject_terms") or {}).items():
        assert terms, theme
        assert set(terms) <= set(subject_terms(theme, config)), theme
