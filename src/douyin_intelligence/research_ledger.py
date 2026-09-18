"""Agent-authored research-ledger loading, discovery, and human rendering.

The ledger is deliberately a transport format, not a crawler.  An agent performs
research and writes JSON; this module validates that JSON against the frozen
``episode_research_pack`` vocabulary before it can enter a research pack.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import episode_research_pack


LEDGER_SCHEMA = "episode-research-ledger/v1"


class ResearchLedgerError(ValueError):
    """Raised when a research ledger cannot be read or violates its contract."""


def _nonempty_string(value: Any, field: str, errors: list[str]) -> str:
    """Return a stripped string and record a field-specific error when empty."""
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field} 必须为非空字符串")
        return ""
    return value.strip()


def _require_list(record: Mapping[str, Any], field: str, label: str, errors: list[str]) -> list[Any]:
    """Return an array field, recording one precise error for absent/wrong types."""
    value = record.get(field)
    if not isinstance(value, list):
        errors.append(f"{label}.{field} 必须是数组")
        return []
    return value


def _validate_source_shape(sources: list[Any], errors: list[str]) -> None:
    """Validate source records before the frozen cross-reference validator runs."""
    for index, raw in enumerate(sources):
        label = f"sources[{index}]"
        if not isinstance(raw, Mapping):
            errors.append(f"{label} 必须是对象")
            continue
        source_id = _nonempty_string(raw.get("source_id"), f"{label}.source_id", errors)
        item_label = f"source {source_id}" if source_id else label
        for field in ("publisher", "title", "url", "published_at", "excerpt"):
            _nonempty_string(raw.get(field), f"{item_label}.{field}", errors)
        if raw.get("authority") not in episode_research_pack.SOURCE_AUTHORITIES:
            errors.append(f"{item_label}.authority 非法：{raw.get('authority')}")
        if raw.get("verification_state") not in episode_research_pack.VERIFICATION_STATES:
            errors.append(f"{item_label}.verification_state 非法：{raw.get('verification_state')}")
        if not isinstance(raw.get("heat_only"), bool):
            errors.append(f"{item_label}.heat_only 必须为布尔值")
        freshness = raw.get("freshness")
        if not isinstance(freshness, Mapping):
            errors.append(f"{item_label}.freshness 必须是对象")
            continue
        _nonempty_string(freshness.get("observed_at"), f"{item_label}.freshness.observed_at", errors)
        if freshness.get("policy") not in episode_research_pack.FRESHNESS_POLICIES:
            errors.append(f"{item_label}.freshness.policy 非法：{freshness.get('policy')}")
        if freshness.get("status_at_publish") not in episode_research_pack.FRESHNESS_STATUSES:
            errors.append(
                f"{item_label}.freshness.status_at_publish 非法：{freshness.get('status_at_publish')}"
            )


def _validate_claim_shape(claims: list[Any], errors: list[str]) -> None:
    """Validate claim-local fields and frozen enumerations."""
    seen: set[str] = set()
    for index, raw in enumerate(claims):
        label = f"claims[{index}]"
        if not isinstance(raw, Mapping):
            errors.append(f"{label} 必须是对象")
            continue
        claim_id = _nonempty_string(raw.get("claim_id"), f"{label}.claim_id", errors)
        item_label = f"claim {claim_id}" if claim_id else label
        if claim_id:
            if claim_id in seen:
                errors.append(f"claim_id 重复：{claim_id}")
            seen.add(claim_id)
        _nonempty_string(raw.get("text"), f"{item_label}.text", errors)
        if raw.get("evidence_status") not in episode_research_pack.EVIDENCE_STATUSES:
            errors.append(f"{item_label}.evidence_status 非法：{raw.get('evidence_status')}")
        if raw.get("wording_policy") not in episode_research_pack.WORDING_POLICIES:
            errors.append(f"{item_label}.wording_policy 非法：{raw.get('wording_policy')}")
        if raw.get("freshness_requirement") not in episode_research_pack.FRESHNESS_REQUIREMENTS:
            errors.append(
                f"{item_label}.freshness_requirement 非法：{raw.get('freshness_requirement')}"
            )
        for field in ("source_ids", "claims_to_verify", "do_not_claim", "material_refs"):
            _require_list(raw, field, item_label, errors)
        for field in ("fact_sources_min", "fact_sources_present"):
            value = raw.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(f"{item_label}.{field} 必须是非负整数")


def _validate_topics(topics: Any, claims: list[Any], sources: list[Any], errors: list[str]) -> None:
    """Validate the optional frozen topic/argument graph and close its references."""
    if not isinstance(topics, Mapping):
        errors.append("topics 必须是对象")
        return
    episode_research_pack._validate_topics({"topics.json": topics}, errors)

    argument = topics.get("argument_graph")
    if not isinstance(argument, Mapping):
        return
    nodes = argument.get("nodes")
    if not isinstance(nodes, list):
        return
    claim_ids = {
        str(item.get("claim_id") or "")
        for item in claims
        if isinstance(item, Mapping) and item.get("claim_id")
    }
    source_ids = {
        str(item.get("source_id") or "")
        for item in sources
        if isinstance(item, Mapping) and item.get("source_id")
    }
    seen_nodes: set[str] = set()
    for index, node in enumerate(nodes):
        if not isinstance(node, Mapping):
            continue
        claim_id = str(node.get("claim_id") or "")
        if claim_id:
            if claim_id in seen_nodes:
                errors.append(f"topics.argument_graph.nodes claim_id 重复：{claim_id}")
            seen_nodes.add(claim_id)
            if claim_id not in claim_ids:
                errors.append(f"topics.argument_graph.node 引用了不存在的 claim_id：{claim_id}")
        refs = node.get("source_candidate_ids")
        if isinstance(refs, list):
            for ref in refs:
                if str(ref) not in source_ids:
                    errors.append(
                        f"topics.argument_graph.nodes[{index}].source_candidate_ids 引用了不存在的 source_id：{ref}"
                    )


def _source_agency(source: Mapping[str, Any]) -> str:
    """Return the *owning* agency of one source record.

    ``origin_publisher`` wins because a republished wire story belongs to the
    agency that produced it, not to whoever carried it.  Two outlets running the
    same 新华社 story are one agency, never two independent ones.  Falls back to
    ``publisher`` and finally to ``source_id`` so a source always maps to some
    stable identity.
    """
    for field in ("origin_publisher", "publisher"):
        value = source.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(source.get("source_id") or "")


def _validate_independent_agencies(
    claims: list[Any], sources: list[Any], errors: list[str]
) -> None:
    """Reject ``confirmed_two_reliable`` whose sources collapse to one agency.

    The frozen validator counts distinct ``publisher`` strings, so two outlets
    that reprinted the same wire story still look like "two independent
    sources".  This check counts distinct *agencies* instead and therefore
    rejects exactly that inflation.  Only ``confirmed_two_reliable`` is
    governed -- every other ``evidence_status`` is left to the frozen contract.
    """
    sources_by_id = {
        str(item.get("source_id") or ""): item
        for item in sources
        if isinstance(item, Mapping)
    }
    for index, raw in enumerate(claims):
        if not isinstance(raw, Mapping) or raw.get("evidence_status") != "confirmed_two_reliable":
            continue
        claim_id = str(raw.get("claim_id") or f"claims[{index}]")
        refs = raw.get("source_ids")
        if not isinstance(refs, list):
            continue
        agency_by_ref: dict[str, str] = {}
        for ref in refs:
            key = str(ref)
            source = sources_by_id.get(key)
            if source is None:
                # An unknown reference is already reported by the frozen
                # validator; do not drown that diagnostic in a second one.
                continue
            agency_by_ref[key] = _source_agency(source)
        if len(set(agency_by_ref.values())) < 2:
            detail = "、".join(f"{ref}→{agency}" for ref, agency in agency_by_ref.items())
            errors.append(
                f"claim {claim_id} 声明 confirmed_two_reliable 但来源机构不独立"
                f"（同一机构的转载稿只算一家）：source_ids="
                f"{'、'.join(agency_by_ref) or '空'}；解析出的机构：{detail or '无'}"
            )


def _read_json_object(path: Path) -> dict[str, Any]:
    """Read one UTF-8 JSON object, normalising IO/decode errors."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ResearchLedgerError(f"研究台账读取失败 path：{path}（{exc}）") from exc
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ResearchLedgerError(f"研究台账 JSON 非法 path：{path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise ResearchLedgerError("研究台账根节点必须是对象")
    return payload


def load_research_ledger(path) -> dict:
    """Read and validate a ledger JSON file, returning research-pack inputs.

    The returned mapping always carries the ledger's own validated identity
    (``theme`` / ``business_date``) plus ``sources`` and ``claims``, and includes
    ``episode_id`` and the optional ``topics`` / ``audience`` / ``disposition``
    only when present in the ledger.  Callers that know the target episode must
    cross-check the identity with :func:`verify_ledger_identity` before injecting
    the inputs -- a canonically named file whose *content* is a different episode
    must fail loudly, never silently attach the wrong facts.  Every failure is
    reported as :class:`ResearchLedgerError` with field names in Chinese
    diagnostics suitable for CLI display.
    """
    ledger_path = Path(path)
    payload = _read_json_object(ledger_path)
    errors: list[str] = []

    if payload.get("schema") != LEDGER_SCHEMA:
        errors.append(f"schema 必须等于 {LEDGER_SCHEMA}，实际为：{payload.get('schema')}")
    theme_value = _nonempty_string(payload.get("theme"), "theme", errors)
    business_date_value = _nonempty_string(payload.get("business_date"), "business_date", errors)

    sources_raw = payload.get("sources")
    claims_raw = payload.get("claims")
    if not isinstance(sources_raw, list):
        errors.append("sources 必须是数组")
        sources: list[Any] = []
    else:
        sources = sources_raw
    if not isinstance(claims_raw, list):
        errors.append("claims 必须是数组")
        claims: list[Any] = []
    else:
        claims = claims_raw

    _validate_source_shape(sources, errors)
    _validate_claim_shape(claims, errors)

    # The frozen validator owns cross-record truth: source-id uniqueness,
    # references, fact_sources_present/min, and independent publishers.
    if all(isinstance(item, Mapping) for item in sources) and all(
        isinstance(item, Mapping) for item in claims
    ):
        frozen_payloads = {
            "sources.json": {"sources": sources},
            "claims.json": {"claims": claims},
            "materials.json": {"materials": []},
        }
        episode_research_pack._validate_sources(frozen_payloads, errors)
        # Agency-level independence: the frozen check is publisher-level and
        # cannot see "two outlets, one wire story".
        _validate_independent_agencies(claims, sources, errors)

    if "topics" in payload:
        _validate_topics(payload.get("topics"), claims, sources, errors)
    if "audience" in payload and not isinstance(payload.get("audience"), Mapping):
        errors.append("audience 必须是对象")
    if "disposition" in payload and payload.get("disposition") not in episode_research_pack.DISPOSITIONS:
        errors.append(f"disposition 非法：{payload.get('disposition')}")

    if errors:
        # Stable order makes CLI output and tests deterministic.  Duplicates can
        # arise because local shape checks and the frozen validator deliberately
        # overlap on enum fields; de-duplicate without losing first occurrence.
        unique_errors = list(dict.fromkeys(errors))
        raise ResearchLedgerError("研究台账校验失败：\n- " + "\n- ".join(unique_errors))

    result: dict[str, Any] = {
        "theme": theme_value,
        "business_date": business_date_value,
        "sources": sources,
        "claims": claims,
    }
    if "episode_id" in payload:
        result["episode_id"] = payload["episode_id"]
    for optional in ("topics", "audience", "disposition"):
        if optional in payload:
            result[optional] = payload[optional]
    return result


def verify_ledger_identity(
    inputs: Mapping[str, Any], *, theme: str, business_date: str, path=None
) -> None:
    """Fail loudly when a loaded ledger belongs to a different episode.

    ``theme`` / ``business_date`` are the *target* episode's identity (from the
    delivery manifest, the CLI flags or the pipeline call).  A ledger whose own
    ``theme`` or ``business_date`` disagrees would silently attach another
    episode's facts, so it is a hard :class:`ResearchLedgerError` -- this holds
    even when the file name is the canonical ``<business_date>-<theme>.json``,
    and equally for an explicit ``--ledger``.  An empty target value means "no
    target known" and skips that dimension (used by bare structural checks).
    """
    where = f"（path：{path}）" if path is not None else ""
    actual_theme = str((inputs or {}).get("theme") or "")
    actual_date = str((inputs or {}).get("business_date") or "")
    if theme and actual_theme != theme:
        raise ResearchLedgerError(
            f"研究台账主题不匹配：目标 theme={theme}，台账 theme={actual_theme}{where}"
        )
    if business_date and actual_date != business_date:
        raise ResearchLedgerError(
            f"研究台账业务日期不匹配：目标 business_date={business_date}，"
            f"台账 business_date={actual_date}{where}"
        )


def discover_research_ledger(
    config, *, theme: str, business_date: str, diagnostics: dict[str, list[Path]] | None = None
) -> Path | None:
    """Find the ledger matching ``theme`` and date.

    The canonical ``<business_date>-<theme>.json`` path has priority and is
    returned *without parsing*.  This is an A13 safety boundary: a canonically
    named but corrupt ledger is an explicit input error for
    :func:`load_research_ledger`, never silently indistinguishable from "no
    ledger".

    Only non-canonical files are scanned by content.  Scan order is
    deterministic (sorted by name) and a matching valid candidate wins outright.
    This is a dedicated ledger directory, so a scanned ``.json`` that is
    unparseable *or* whose root is not an object is an illegal candidate rather
    than "no ledger": if no candidate matches, such a file makes the call raise a
    :class:`ResearchLedgerError` naming the path (and the expected object root),
    so the caller cannot mistake corruption for absence.  A directory of only
    other episodes' valid ledgers still returns ``None`` (a genuine absence).

    ``diagnostics`` is an optional *warning* sink.  When supplied it is filled
    with ``{"unreadable": [...], "non_object": [...], "missing_identity": [...]}``
    -- the files that were seen but could not be used, so a caller is never left
    believing the directory was simply empty.  ``missing_identity`` collects a
    JSON object that lacks ``theme`` and/or ``business_date`` outright: it is not
    a ledger at all.  A file whose keys are present but whose values belong to
    another episode is a normal foreign ledger and is reported nowhere.

    ``ledger_root`` written explicitly in the config is a promise by the
    operator: when it cannot be resolved to a usable directory the call raises
    :class:`ResearchLedgerError` instead of silently reporting "no ledger".
    Only the *default* root is allowed to be absent, because its absence is the
    ordinary "this feature has no ledger directory yet" case.
    """
    settings = (
        ((config.get("jobs") or {}).get("material_replication") or {}).get(
            "episode_research_pack"
        )
        or {}
    )
    configured_root = settings.get("ledger_root")
    explicit_root = configured_root not in (None, "")
    root_value = configured_root or "input/research-ledgers"
    try:
        root = episode_research_pack.project_path(config, root_value).resolve()
    except (OSError, TypeError, ValueError) as exc:
        if explicit_root:
            raise ResearchLedgerError(
                f"研究台账目录配置不可用：ledger_root={root_value!r}（{exc}）"
            ) from exc
        return None
    if not root.is_dir():
        if explicit_root and root.exists():
            raise ResearchLedgerError(
                f"研究台账目录配置不可用：ledger_root={root_value!r} 存在但不是目录"
                f"（path：{root}）"
            )
        return None

    # Windows forbids ``<>:\\|?*`` in file names.  Replace only those characters
    # (plus embedded path separators) so the canonical name remains predictable
    # and human-readable while never escaping ``root``.
    safe_theme = "".join("_" if character in '<>:\\|?*/' else character for character in str(theme))
    canonical = root / f"{business_date}-{safe_theme}.json"
    try:
        if canonical.is_file():
            return canonical.resolve()
        candidates = sorted(root.glob("*.json"), key=lambda item: item.name)
    except OSError as exc:
        raise ResearchLedgerError(f"研究台账目录无法列举：{root}（{exc}）") from exc
    buckets: dict[str, list[Path]] = {
        "unreadable": [],
        "non_object": [],
        "missing_identity": [],
    }

    def publish_diagnostics() -> None:
        if diagnostics is not None:
            diagnostics.update({key: list(value) for key, value in buckets.items()})

    for candidate in candidates:
        if candidate == canonical:
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            # A corrupt candidate must never silently degrade into "no ledger".  A
            # matching valid candidate still wins because it returns first below;
            # only when nothing matches do we surface the corruption.
            buckets["unreadable"].append(candidate)
            continue
        if not isinstance(payload, dict):
            # This is a dedicated ledger directory: a parseable file whose root is
            # not an object (``[]`` / ``null`` / ``"x"``) is still an illegal
            # ledger candidate, not a silent "no ledger".
            buckets["non_object"].append(candidate)
            continue
        if "theme" not in payload or "business_date" not in payload:
            # Not a ledger at all: it carries no identity, so it cannot even be
            # recognised as another episode's ledger.  Warn instead of dropping
            # it silently -- a mistyped key must not look like "no ledger found".
            buckets["missing_identity"].append(candidate)
            continue
        if payload.get("theme") == theme and payload.get("business_date") == business_date:
            publish_diagnostics()
            return candidate.resolve()
    publish_diagnostics()
    if buckets["unreadable"] or buckets["non_object"]:
        reasons: list[str] = []
        if buckets["unreadable"]:
            reasons.append("无法解析：" + "、".join(str(item) for item in buckets["unreadable"]))
        if buckets["non_object"]:
            reasons.append(
                "根节点必须是对象：" + "、".join(str(item) for item in buckets["non_object"])
            )
        raise ResearchLedgerError(
            f"研究台账目录存在非法的台账候选文件，且未找到匹配 theme={theme} "
            f"business_date={business_date} 的台账；" + "；".join(reasons)
        )
    return None


def _markdown_cell(value: Any) -> str:
    """Render a safe one-line Markdown-table cell."""
    return str(value if value is not None else "").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_ledger_markdown(
    inputs: dict, *, theme: str, business_date: str
) -> str:
    """Render a deterministic, human-readable fact ledger as Markdown."""
    sources = inputs.get("sources") if isinstance(inputs, Mapping) else []
    claims = inputs.get("claims") if isinstance(inputs, Mapping) else []
    source_rows = sources if isinstance(sources, list) else []
    claim_rows = claims if isinstance(claims, list) else []

    lines = [
        "# 事实台账",
        "",
        "## 概览",
        "",
        f"- 主题：{theme}",
        f"- 业务日期：{business_date}",
        f"- 来源数：{len(source_rows)}",
        f"- 主张数：{len(claim_rows)}",
        "",
        "## 事实来源",
        "",
        "| 等级 | 标题 | 媒体 | 发布日 | URL |",
        "|---|---|---|---|---|",
    ]
    for source in source_rows:
        item = source if isinstance(source, Mapping) else {}
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(item.get(field))
                for field in ("authority", "title", "publisher", "published_at", "url")
            )
            + " |"
        )
    if not source_rows:
        lines.append("| - | 暂无 | - | - | - |")

    lines.extend(
        [
            "",
            "## 主张与证据状态",
            "",
            "| 状态 | 措辞策略 | 文本 |",
            "|---|---|---|",
        ]
    )
    for claim in claim_rows:
        item = claim if isinstance(claim, Mapping) else {}
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(item.get(field))
                for field in ("evidence_status", "wording_policy", "text")
            )
            + " |"
        )
    if not claim_rows:
        lines.append("| - | - | 暂无 |")

    conflicts = [
        item
        for item in claim_rows
        if isinstance(item, Mapping) and item.get("evidence_status") == "conflicting"
    ]
    lines.extend(["", "## 冲突与待核", ""])
    if conflicts:
        for item in conflicts:
            claim_id = _markdown_cell(item.get("claim_id"))
            text = _markdown_cell(item.get("text"))
            source_ids = item.get("source_ids")
            refs = "、".join(str(ref) for ref in source_ids) if isinstance(source_ids, list) else ""
            lines.append(f"- **{claim_id}**：{text}（并列来源：{refs or '未列明'}）")
    else:
        lines.append("- 无冲突主张。")
    lines.append("")
    return "\n".join(lines)
