"""Theme expansion and delivery-folder naming for material replication.

The expansion is fully offline and deterministic: it derives synonyms,
category/attribute combinations, brand-attribute pairs and brand-category pairs
from a small, auditable hint table, and weaves in in-domain intent suffixes
(``实测/开箱/上手`` ...) early enough that they survive the caller's budget cap.
A theme therefore always yields at least ``min_keywords`` non-duplicate
keywords, and a themed search always keeps some hands-on/real-shot intent
queries.

Every derived keyword is **subject-qualified**: the theme is first reduced to
its subject (``_subject_head`` drops category words and intent suffixes), and the
attribute/intent lanes qualify that subject instead of the raw theme string or a
detached category word -- a category term with no theme limitation (``机器人 演示``)
drags unrelated, hotter videos into the candidate pool.  A theme the category
word already identifies (``苹果折叠屏`` -> ``折叠屏 折痕``) keeps the historical
spelling, so a theme with nothing detachable expands exactly as it did before.
Subjects listed in ``_SUBJECT_ALIASES`` additionally contribute the platform's
own spellings (``机器鸭`` for ``机械鸭``), because a theme written in our wording
can be absent from the platform's vocabulary.
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

# Product-level noun dictionary: *our* spelling of a subject -> the spellings the
# platform actually uses.  ``expand_keywords`` adds every alias as a standalone
# keyword and as a subject-swapped keyword, because a theme written with our
# spelling can be *absent* from the platform's vocabulary: in the Microduck run
# 52% of pool titles contained ``microduck`` but **zero** contained 「机械鸭」,
# i.e. every keyword built from the theme's own wording missed.
#
# The table doubles as the guard used by ``_subject_head``: only a category word
# that rides on a *known* product noun (``机械鸭`` + ``机器人``) is treated as a
# detachable suffix.  Overridable/expandable via
# ``jobs.material_replication.subject_aliases`` (see ``_resolve_subject_aliases``).
_SUBJECT_ALIASES: dict[str, tuple[str, ...]] = {
    "机械鸭": ("机器鸭", "Microduck", "机械鸭子"),
    "扫地机器人": ("扫地机",),
    "折叠屏": ("折叠机", "折屏"),
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


def _theme_base(theme: str, settings: dict[str, Any]) -> str:
    """Sanitize a theme under the same budget cap ``expand_keywords`` uses."""
    theme_max = max(1, int(settings.get("theme_max_chars") or 12))
    return sanitize_theme(theme, max_length=max(theme_max, min(64, theme_max * 4)))


def _resolve_subject_aliases(settings: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """Built-in ``_SUBJECT_ALIASES`` merged with the optional config table.

    ``jobs.material_replication.subject_aliases`` is ``{subject: [alias, ...]}``.
    A present key *overrides* the built-in entry for that subject (an empty list
    therefore disables it); absent keys keep the built-in value, so a config
    without the key behaves exactly as before.
    """
    table: dict[str, tuple[str, ...]] = dict(_SUBJECT_ALIASES)
    raw = settings.get("subject_aliases")
    if not isinstance(raw, dict):
        return table
    for subject, values in raw.items():
        name = str(subject).strip()
        if not name:
            continue
        if isinstance(values, (list, tuple)):
            table[name] = tuple(str(item).strip() for item in values if str(item).strip())
            continue
        text = str(values).strip()
        if text:
            table[name] = (text,)
    return table


def _subject_head(base: str, aliases: dict[str, tuple[str, ...]] | None = None) -> str:
    """Strip intent suffixes and *attached* category words, leaving the subject.

    ``Microduck 机械鸭机器人`` -> ``Microduck 机械鸭``: 「机器人」 rides on a token
    whose remainder (「机械鸭」) is a known product noun, so it does not identify
    the product and is dropped.

    The stripping is deliberately conservative:

    * a compound that merely *contains* a category word keeps it
      (``iRobot Roomba 875 扫地机器人`` stays whole): cutting it would leave a
      dangling fragment (「扫地」) that matches nothing on the platform;
    * a bare category token is dropped only when it is the *trailing* token and
      something else remains (``苹果 折叠屏`` -> ``苹果``), so a theme that is
      itself a category word (``机器人``, ``折叠屏``) is untouched;
    * when nothing survives, ``base`` is returned unchanged.
    """
    table = _SUBJECT_ALIASES if aliases is None else aliases
    known = {subject.casefold() for subject in table}
    categories = tuple(_CATEGORY_ATTRIBUTES)
    folded_categories = {category.casefold() for category in categories}
    suffixes = {suffix.casefold() for suffix in _FALLBACK_SUFFIXES}

    tokens: list[str] = []
    for token in base.split():
        folded_token = token.casefold()
        if folded_token in suffixes:
            continue
        kept = token
        for category in categories:
            if not folded_token.endswith(category.casefold()) or len(token) <= len(category):
                continue
            remainder = token[: len(token) - len(category)]
            if remainder.casefold() in known:
                kept = remainder
                break
        tokens.append(kept)

    if len(tokens) > 1 and tokens[-1].casefold() in folded_categories:
        tokens.pop()

    head = " ".join(tokens).strip()
    # An intent suffix glued straight onto the last token (``苹果折叠屏实测``).
    for suffix in _FALLBACK_SUFFIXES:
        if head.endswith(suffix) and len(head) > len(suffix):
            head = head[: -len(suffix)].strip()
            break
    return head or base


def _category_names_a_product_noun(
    base: str, category: str, aliases: dict[str, tuple[str, ...]]
) -> bool:
    """True when the theme spells ``category`` as the tail of a *compound* noun.

    ``扫地机器人`` -> the token *is* a product noun we have on file, so 「机器人」
    alone (``机器人 演示``) is a different product class (humanoids) and pulls
    unrelated, hotter clips into the pool.  A theme that merely *belongs to* the
    category (``苹果折叠屏`` -> ``折叠屏 折痕``) is unaffected: its pool is already
    limited to that category, so the historical spelling stands.
    """
    for token in base.split():
        folded_token = token.casefold()
        for subject in aliases:
            if len(subject) > len(category) and subject.casefold().endswith(category.casefold()):
                if folded_token == subject.casefold():
                    return True
    return False


def _subject_alias_terms(
    head: str, folded: str, aliases: dict[str, tuple[str, ...]] | None = None
) -> list[str]:
    """Platform-vocabulary keywords for a theme whose subject is a known product.

    Returns each alias as a *standalone* keyword (``机器鸭``, ``Microduck``) plus
    the subject with the matched noun swapped for that alias
    (``Microduck 机械鸭`` -> ``Microduck 机器鸭``).  The standalone aliases come
    first: the caller truncates this list, and an alias is the only spelling a
    platform that never writes our wording can answer to.
    """
    table = _SUBJECT_ALIASES if aliases is None else aliases
    standalone: list[str] = []
    swapped: list[str] = []
    for subject, subject_aliases in table.items():
        if subject.casefold() not in folded:
            continue
        for alias in subject_aliases:
            standalone.append(alias)
            if alias.casefold() in head.casefold():
                continue  # The subject already spells it that way.
            swapped.append(_replace_casefold(head, subject, alias))
    return standalone + swapped


def _alias_terms(base: str, folded: str) -> list[str]:
    """``base`` with the theme's brand swapped for each known alias."""
    terms: list[str] = []
    for brand, aliases in _BRAND_ALIASES.items():
        if brand.casefold() in folded:
            for alias in aliases:
                terms.append(_replace_casefold(base, brand, alias))
    return terms


def _category_attribute_terms(
    head: str, folded: str, base: str, aliases: dict[str, tuple[str, ...]]
) -> list[str]:
    """``{subject} {attribute}`` terms for every category present in the theme.

    The attribute is subject-qualified whenever the category word *detaches* from
    the product name.  The old ``{category} {attribute}`` form (``机器人 演示`` /
    ``机器人 交互``) carried no theme limitation, so it pulled unrelated *and
    hotter* videos (Unitree G1 demos) into the candidate pool, where they
    out-ranked every genuine product term.

    ``folded`` still comes from the raw theme, so the attribute angle survives
    even when ``_subject_head`` stripped the category word itself, and the
    historical spelling is kept for themes whose pool is already limited to the
    category (see ``_category_names_a_product_noun``).
    """
    subject = head if head.casefold() != base.casefold() else None
    terms: list[str] = []
    for category, attributes in _CATEGORY_ATTRIBUTES.items():
        if category.casefold() not in folded:
            continue
        prefix = subject
        if prefix is None and _category_names_a_product_noun(base, category, aliases):
            prefix = head
        for attribute in attributes:
            terms.append(f"{prefix} {attribute}" if prefix else f"{category} {attribute}")
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

    Every derived keyword is qualified by the theme's *subject* (``_subject_head``)
    rather than by its raw wording, and a subject found in ``_SUBJECT_ALIASES``
    also contributes its platform spellings -- see the module docstring of the
    helpers above.  When the theme carries no strippable category/suffix
    (``head == base``) the lane contents are unchanged.
    """
    settings = (config.get("jobs") or {}).get("material_replication") or {}
    min_keywords = max(1, int(settings.get("min_keywords") or 3))
    max_keywords = max(min_keywords, min(10, int(settings.get("max_keywords") or 6)))
    base = _theme_base(theme, settings)
    if not base:
        return []

    aliases = _resolve_subject_aliases(settings)
    head = _subject_head(base, aliases)
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
    # The subject itself is a keyword: it is the only spelling short enough to
    # survive the crawler's ``budget // 10`` truncation intact.
    add(head)
    folded = base.casefold()
    lanes: dict[str, list[str]] = {
        "alias": _alias_terms(base, folded) + _subject_alias_terms(head, folded, aliases),
        "category_attribute": _category_attribute_terms(head, folded, base, aliases),
        "brand_attribute": _brand_attribute_terms(folded),
        "brand_category": _brand_category_terms(folded),
        "intent": [f"{head} {suffix}" for suffix in _FALLBACK_SUFFIXES],
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


def subject_terms(theme: str, config: dict[str, Any]) -> list[str]:
    """Return the theme's *subject vocabulary*: the tokens a clip must mention.

    Every whitespace token of the subject head (``_subject_head``) plus every
    alias of a product noun found in the theme, case-folded and de-duplicated in
    first-seen order.  This is the vocabulary the relevance gate matches
    candidate titles against, so the signature is deliberately stable:
    ``subject_terms(theme, config) -> list[str]``.

    The category words stripped from the head never appear here: matching them
    is what let unrelated, hotter videos into the pool in the first place.
    """
    settings = (config.get("jobs") or {}).get("material_replication") or {}
    base = _theme_base(theme, settings)
    if not base:
        return []
    aliases = _resolve_subject_aliases(settings)
    head = _subject_head(base, aliases)
    folded = base.casefold()

    terms: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        text = str(value).strip()
        if not text:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        terms.append(text)

    for token in head.split():
        add(token)
    for subject, subject_aliases in aliases.items():
        if subject.casefold() in folded:
            for alias in subject_aliases:
                add(alias)
    return terms


def delivery_folder_name(business_date: str, theme: str, *, max_path_chars: int = 260) -> str:
    """Build the ``MM.DD<主题>复刻视频`` folder name (month not zero-padded)."""
    try:
        parsed = date.fromisoformat(str(business_date))
    except ValueError as exc:
        raise ValueError("business_date 必须是 YYYY-MM-DD") from exc
    max_theme = 24
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
