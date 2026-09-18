from __future__ import annotations

import json
from pathlib import Path

import pytest

from douyin_intelligence.config import load_config
from douyin_intelligence.replication_theme import (
    _CATEGORY_ATTRIBUTES,
    _SUBJECT_ALIASES,
    _subject_split_terms,
    RECOMMENDED_USAGES,
    RIGHTS_STATUSES,
    SOURCE_AUTHORITIES,
    SOURCE_KINDS,
    VISUAL_ROLES,
    delivery_folder_name,
    event_terms,
    expand_keywords,
    infer_material_labels,
    material_labels_for,
    material_profile_name,
    material_profiles,
    resolve_material_profile,
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


# --------------------------------------------------------------------------- #
# ``theme_material_profiles`` / ``theme_profile_map`` -- the genre-strategy
# layer (2026-09-18).
#
# A profile only *describes* preferences and label vocabularies; it never
# changes what may enter the pool (that stays with ``theme_subject_terms``).
# Profile resolution and label inference are pure functions of the candidate's
# platform/title/author plus the profile, so every branch below is offline and
# reproducible, and no candidate is ever assumed official or licensed.
# --------------------------------------------------------------------------- #

_PROFILE_NAMES = (
    "person_or_company_event",
    "official_notice_or_security_event",
    "product_or_industry_trend",
)

_PERSON_THEME = "特朗普致电黄仁勋"


def _shipped_material_block() -> dict:
    """The *raw* shipped ``material_replication`` block, read straight from disk.

    Deliberately bypasses ``load_config()``: tests/conftest.py strips the opt-in
    profile keys out of every ``load_config()`` payload, so only a direct read can
    assert on what the repository actually ships.
    """
    config_path = Path(__file__).resolve().parents[1] / "config" / "content_intelligence.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    return payload["jobs"]["material_replication"]


def _profile_config(**material_keys: object) -> dict:
    return _material_config(**material_keys)


def _label_config(**profile_overrides: object) -> dict:
    """A config whose ``person_or_company_event`` carries explicit account tables.

    Synthetic on purpose (mirrors ``_material_config``): an assertion pinned to
    the shipped tables would only test the data file and rot on the next edit.
    """
    profile: dict = {
        "label": "人物/企业事件",
        "preferred_source_kinds": [
            "official_original", "news_broadcast", "creator_commentary", "platform_video",
        ],
        "main_roles": ["event_direct", "subject_person", "news_anchor_or_reporter"],
        "official_accounts": ["华为终端官方", "工信部"],
        "news_accounts": ["央视新闻", "新华社"],
    }
    profile.update(profile_overrides)
    return _profile_config(
        theme_material_profiles={"default": {}, "person_or_company_event": profile},
        theme_profile_map={_PERSON_THEME: "person_or_company_event"},
    )


def test_profile_defaults_when_the_block_is_absent() -> None:
    config = _profile_config()

    assert material_profile_name("任意主题", config) == "default"
    profile = resolve_material_profile("任意主题", config)
    assert profile["name"] == "default"
    assert profile["label"]
    assert set(profile["main_roles"]) <= set(VISUAL_ROLES)
    assert set(profile["preferred_source_kinds"]) <= set(SOURCE_KINDS)
    # The built-in lanes are still there, so a bare default is not vocabulary-free.
    assert profile["role_terms"]["event_direct"]


def test_explicit_theme_map_selects_the_configured_profile() -> None:
    config = _profile_config(
        theme_material_profiles={name: {"label": name} for name in ("default", *_PROFILE_NAMES)},
        theme_profile_map={"手机集体涨价": "product_or_industry_trend"},
    )

    assert material_profile_name("手机集体涨价", config) == "product_or_industry_trend"
    assert material_profile_name("未列出主题", config) == "default"
    assert resolve_material_profile("手机集体涨价", config)["label"] == "product_or_industry_trend"


def test_sanitized_base_is_accepted_as_a_profile_map_key() -> None:
    raw = "手机集体涨价*"
    base = sanitize_theme(raw, max_length=48)
    assert base != raw  # the fixture really does differ from its base

    config = _profile_config(
        theme_material_profiles={"default": {}, "product_or_industry_trend": {}},
        theme_profile_map={base: "product_or_industry_trend"},
    )

    assert material_profile_name(raw, config) == "product_or_industry_trend"


@pytest.mark.parametrize(
    "bad",
    [
        {"theme_material_profiles": "不是字典"},
        {"theme_material_profiles": {"person_or_company_event": "坏值"}},
        {"theme_material_profiles": {}},
        {"theme_profile_map": "不是字典"},
        {"theme_profile_map": ["person_or_company_event"]},
        {"theme_profile_map": {"主题": "未知档案"}},
        {"theme_profile_map": {"主题": ""}},
        {"theme_profile_map": {"主题": None}},
    ],
)
def test_bad_profile_config_falls_back_to_default(bad: dict) -> None:
    # A typo in the profile name must never silently pick a wrong strategy.
    assert material_profile_name("主题", _profile_config(**bad)) == "default"
    assert resolve_material_profile("主题", _profile_config(**bad))["name"] == "default"


def test_underscore_keys_are_treated_as_comments() -> None:
    profiles = material_profiles(
        _profile_config(theme_material_profiles={"_comment": "x", "person_or_company_event": {}})
    )

    assert "_comment" not in profiles
    assert "person_or_company_event" in profiles
    assert "default" in profiles  # always present, so callers can index safely


def test_three_profiles_differ_yet_all_admit_self_media() -> None:
    profiles = {
        "default": {"label": "通用"},
        "person_or_company_event": {
            "label": "人物/企业事件",
            "main_roles": ["event_direct", "subject_person"],
            "preferred_source_kinds": [
                "official_original", "news_broadcast", "creator_commentary", "platform_video",
            ],
            "original_source_bonus": 0.15,
            "heat_weight": 0.25,
        },
        "official_notice_or_security_event": {
            "label": "官方通报/安全事件",
            "main_roles": ["news_anchor_or_reporter", "event_direct"],
            "preferred_source_kinds": [
                "official_original", "news_broadcast", "platform_video", "creator_commentary",
            ],
            "original_source_bonus": 0.35,
            "heat_weight": 0.15,
        },
        "product_or_industry_trend": {
            "label": "产品/行业趋势",
            "main_roles": ["product_or_scene", "commentary"],
            "preferred_source_kinds": [
                "official_original", "creator_commentary", "platform_video", "news_broadcast",
            ],
            "original_source_bonus": 0.2,
            "heat_weight": 0.3,
        },
    }
    config = _profile_config(
        theme_material_profiles=profiles,
        theme_profile_map={name: name for name in _PROFILE_NAMES},
    )
    resolved = {name: resolve_material_profile(name, config) for name in _PROFILE_NAMES}

    # Three distinct, deterministic report labels and main-role orderings.
    assert len({profile["label"] for profile in resolved.values()}) == 3
    assert (
        resolved["person_or_company_event"]["main_roles"]
        != resolved["product_or_industry_trend"]["main_roles"]
    )
    assert resolved["official_notice_or_security_event"]["main_roles"][0] == "news_anchor_or_reporter"
    # No profile hard-excludes self-media: a creator commentary source is always
    # inside ``preferred_source_kinds`` (a soft preference, not a rejection).
    for profile in resolved.values():
        assert "creator_commentary" in profile["preferred_source_kinds"]
    for name in _PROFILE_NAMES:
        assert resolve_material_profile(name, config) == resolved[name]


def test_official_account_yields_official_original_but_never_authorized() -> None:
    labels = material_labels_for(
        _PERSON_THEME, source="douyin", title="新品发布", author="华为终端官方", config=_label_config()
    )

    assert labels["source_kind"] == "official_original"
    assert labels["source_authority"] == "official"
    # Source nature and rights risk are separate fields: official ≠ licensed.
    assert labels["rights_status"] == "review_required"


def test_news_account_yields_news_broadcast() -> None:
    labels = material_labels_for(
        _PERSON_THEME, source="douyin", title="现场报道", author="央视新闻", config=_label_config()
    )

    assert labels["source_kind"] == "news_broadcast"
    assert labels["source_authority"] == "news_media"
    assert labels["rights_status"] == "review_required"


def test_creator_marker_yields_creator_commentary() -> None:
    labels = material_labels_for(
        _PERSON_THEME, source="douyin", title="黄仁勋最新访谈 深度解读", author="科技老王", config=_label_config()
    )

    assert labels["source_kind"] == "creator_commentary"
    assert labels["source_authority"] == "creator"
    assert labels["rights_status"] == "review_required"


def test_bare_platform_candidate_is_platform_video_with_unknown_authority() -> None:
    labels = material_labels_for(
        _PERSON_THEME, source="bilibili", title="随手拍的一段画面", author="路人甲", config=_label_config()
    )

    assert labels["source_kind"] == "platform_video"
    # A plain upload carries no identifiable publisher -> never promoted to creator.
    assert labels["source_authority"] == "unknown"
    assert labels["rights_status"] == "unknown"


def test_unrecognised_source_and_blank_candidate_degrade_to_unknown() -> None:
    unknown_platform = material_labels_for(
        _PERSON_THEME, source="someblog", title="一条普通视频", author="作者", config=_label_config()
    )
    assert unknown_platform["source_kind"] == "unknown"

    blank = material_labels_for(_PERSON_THEME, config=_label_config())
    assert blank["source_kind"] == "unknown"
    assert blank["source_authority"] == "unknown"
    assert blank["visual_role"] == "unknown"
    assert blank["rights_status"] == "unknown"
    assert blank["recommended_usage"] == "optional"


@pytest.mark.parametrize(
    ("title", "expected_role"),
    [
        ("记者现场连线报道", "news_anchor_or_reporter"),
        ("完整采访实录", "event_direct"),
        ("马斯克现身发布会现场", "event_direct"),
        ("公司CEO演讲", "subject_person"),
        ("新机开箱上手实拍", "product_or_scene"),
        ("我眼中的这波行情", "unknown"),
    ],
)
def test_visual_role_lanes_are_deterministic(title: str, expected_role: str) -> None:
    labels = infer_material_labels(
        source="douyin",
        title=title,
        author="路人",
        profile=resolve_material_profile(_PERSON_THEME, _label_config()),
    )

    assert labels["visual_role"] == expected_role


def test_profile_role_terms_override_a_single_lane_only() -> None:
    base = resolve_material_profile(_PERSON_THEME, _label_config())
    assert "现场" in base["role_terms"]["event_direct"]

    custom = resolve_material_profile(
        _PERSON_THEME, _label_config(role_terms={"event_direct": ["连线实录"]})
    )

    # The supplied lane replaces the built-in one...
    assert custom["role_terms"]["event_direct"] == ("连线实录",)
    # ...while every other lane keeps the built-in vocabulary.
    assert custom["role_terms"]["product_or_scene"] == base["role_terms"]["product_or_scene"]
    # And the replaced lane really does stop matching its old term.
    replaced = infer_material_labels(source="douyin", title="会议现场", author="路人", profile=custom)
    assert replaced["visual_role"] == "unknown"


def test_recommended_usage_follows_the_profile_preferences() -> None:
    config = _label_config(
        preferred_source_kinds=["official_original", "news_broadcast"],
        main_roles=["event_direct"],
    )
    profile = resolve_material_profile(_PERSON_THEME, config)

    # Outside the preferred kinds -> optional (a hint, not a rejection).
    creator = infer_material_labels(source="douyin", title="深度解读", author="老王", profile=profile)
    assert creator["recommended_usage"] == "optional"
    # Preferred kind + a main role -> main.
    strong = infer_material_labels(source="douyin", title="完整采访实录", author="华为终端官方", profile=profile)
    assert strong["recommended_usage"] == "main"
    # Preferred kind but an unknown role -> supporting.
    weak = infer_material_labels(source="douyin", title="随手拍", author="华为终端官方", profile=profile)
    assert weak["recommended_usage"] == "supporting"


def test_labels_only_use_the_canonical_vocabularies_and_are_stable() -> None:
    profile = resolve_material_profile(_PERSON_THEME, _label_config())
    labels = infer_material_labels(source="douyin", title="新机开箱", author="华为终端官方", profile=profile)

    assert set(labels) == {
        "source_kind", "source_authority", "visual_role", "recommended_usage", "rights_status",
    }
    assert labels["source_kind"] in SOURCE_KINDS
    assert labels["source_authority"] in SOURCE_AUTHORITIES
    assert labels["visual_role"] in VISUAL_ROLES
    assert labels["recommended_usage"] in RECOMMENDED_USAGES
    assert labels["rights_status"] in RIGHTS_STATUSES
    assert (
        infer_material_labels(source="douyin", title="新机开箱", author="华为终端官方", profile=profile)
        == labels
    )


def test_infer_material_labels_tolerates_a_malformed_profile() -> None:
    labels = infer_material_labels(source="douyin", title="", author="", profile=None)  # type: ignore[arg-type]

    assert labels["source_kind"] == "platform_video"
    assert labels["source_authority"] == "unknown"
    assert labels["rights_status"] == "unknown"


def test_profile_config_never_touches_the_three_word_tables() -> None:
    """The profile layer is additive: it must not leak into search/gate/event words."""
    plain = _profile_config()
    with_profiles = _label_config()

    for theme in ("苹果折叠屏", _TREND_THEME, "Microduck 机械鸭机器人"):
        assert expand_keywords(theme, with_profiles) == expand_keywords(theme, plain), theme
        assert subject_terms(theme, with_profiles) == subject_terms(theme, plain), theme
        assert event_terms(theme, with_profiles) == event_terms(theme, plain), theme


def test_shipped_profiles_and_map_are_well_formed() -> None:
    material = _shipped_material_block()
    profiles = material.get("theme_material_profiles") or {}
    assert {"default", *_PROFILE_NAMES} <= set(profiles)

    config = {"jobs": {"material_replication": material}}
    mapping = material.get("theme_profile_map") or {}
    assert mapping, "shipped profile map must bind at least one theme"
    for theme, name in mapping.items():
        assert name in profiles, (theme, name)
        assert material_profile_name(theme, config) == name, theme
        resolved = resolve_material_profile(theme, config)
        assert resolved["label"], theme
        assert set(resolved["main_roles"]) <= set(VISUAL_ROLES), theme
        assert set(resolved["preferred_source_kinds"]) <= set(SOURCE_KINDS), theme


def test_shipped_profiles_never_promote_a_platform_upload_to_official() -> None:
    """The honest default: an unidentified upload stays platform_video/unknown."""
    material = _shipped_material_block()
    config = {"jobs": {"material_replication": material}}

    labels = material_labels_for(
        "手机集体涨价",
        source="douyin",
        title="手机集体涨价 现场实录",
        author="某自媒体老王",
        config=config,
    )

    assert labels["source_kind"] == "platform_video"
    assert labels["source_authority"] == "unknown"
    assert labels["rights_status"] == "unknown"


def test_shipped_official_and_news_accounts_are_detected() -> None:
    """Self-consistency: whatever the shipped tables name must actually take effect."""
    material = _shipped_material_block()
    config = {"jobs": {"material_replication": material}}
    profiles = material_profiles(config)

    seen_official = 0
    for name, profile in profiles.items():
        for account in profile["official_accounts"]:
            labels = infer_material_labels(source="douyin", title="通报", author=account, profile=profile)
            assert labels["source_kind"] == "official_original", (name, account)
            assert labels["source_authority"] == "official", (name, account)
            assert labels["rights_status"] == "review_required", (name, account)
            seen_official += 1
        for account in profile["news_accounts"]:
            labels = infer_material_labels(source="douyin", title="报道", author=account, profile=profile)
            assert labels["source_kind"] == "news_broadcast", (name, account)
            assert labels["source_authority"] == "news_media", (name, account)
    assert seen_official >= 1, "shipped official_notice profile declares no official account"
