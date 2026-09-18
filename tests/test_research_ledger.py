"""Contract tests for the agent-authored episode research ledger."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from douyin_intelligence.research_ledger import (
    LEDGER_SCHEMA,
    ResearchLedgerError,
    discover_research_ledger,
    load_research_ledger,
    render_ledger_markdown,
    verify_ledger_identity,
)


def _source(
    source_id: str,
    *,
    publisher: str = "新华社",
    authority: str = "official",
    verification_state: str = "verified",
) -> dict:
    return {
        "source_id": source_id,
        "publisher": publisher,
        "title": f"来源标题-{source_id}",
        "url": f"https://example.com/{source_id}",
        "published_at": "2026-09-16",
        "excerpt": "支持该主张的原文摘录。",
        "authority": authority,
        "verification_state": verification_state,
        "heat_only": False,
        "freshness": {
            "observed_at": "2026-09-17T10:00:00+08:00",
            "policy": "event_window",
            "status_at_publish": "fresh",
        },
    }


def _claim(
    claim_id: str,
    *,
    source_ids: list[str] | None = None,
    evidence_status: str = "confirmed_official",
    wording_policy: str = "assert",
    fact_sources_present: int | None = None,
) -> dict:
    refs = list(source_ids or ["s1"])
    minimum = {"confirmed_official": 1, "confirmed_two_reliable": 2}.get(evidence_status, 0)
    return {
        "claim_id": claim_id,
        "topic_id": "topic-01",
        "text": "平陆运河是西部陆海新通道的骨干工程，可直接改写为口播完整句。",
        "evidence_status": evidence_status,
        "wording_policy": wording_policy,
        "freshness_requirement": "fresh",
        "source_ids": refs,
        "fact_sources_min": minimum,
        "fact_sources_present": len(refs) if fact_sources_present is None else fact_sources_present,
        "claims_to_verify": [],
        "do_not_claim": [],
        "material_refs": [],
    }


def _ledger() -> dict:
    return {
        "schema": LEDGER_SCHEMA,
        "theme": "平陆运河",
        "business_date": "2026-09-17",
        "episode_id": "2026-09-17-平陆运河",
        "sources": [_source("s1")],
        "claims": [_claim("c1")],
    }


def _write(tmp_path: Path, payload: dict, name: str = "ledger.json") -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _assert_bad(tmp_path: Path, payload: dict, field: str) -> None:
    with pytest.raises(ResearchLedgerError) as caught:
        load_research_ledger(_write(tmp_path, payload))
    assert field in str(caught.value)


def test_load_valid_minimum_ledger_returns_identity_sources_and_claims(tmp_path: Path) -> None:
    result = load_research_ledger(_write(tmp_path, _ledger()))
    assert result == {
        "theme": "平陆运河",
        "business_date": "2026-09-17",
        "episode_id": "2026-09-17-平陆运河",
        "sources": _ledger()["sources"],
        "claims": _ledger()["claims"],
    }


def test_load_omits_episode_id_when_absent(tmp_path: Path) -> None:
    payload = _ledger()
    payload.pop("episode_id")
    result = load_research_ledger(_write(tmp_path, payload))
    assert "episode_id" not in result
    assert result["theme"] == "平陆运河"
    assert result["business_date"] == "2026-09-17"


def test_verify_ledger_identity_accepts_match_and_rejects_mismatch(tmp_path: Path) -> None:
    inputs = load_research_ledger(_write(tmp_path, _ledger()))

    verify_ledger_identity(inputs, theme="平陆运河", business_date="2026-09-17")  # no raise

    with pytest.raises(ResearchLedgerError, match="theme"):
        verify_ledger_identity(inputs, theme="智元A3", business_date="2026-09-17")
    with pytest.raises(ResearchLedgerError, match="business_date"):
        verify_ledger_identity(inputs, theme="平陆运河", business_date="2020-01-01")

    # An empty target means "no target known": that dimension is skipped.
    verify_ledger_identity(inputs, theme="", business_date="")


def test_load_preserves_optional_research_inputs(tmp_path: Path) -> None:
    payload = _ledger()
    payload.update({"audience": {"primary": "科技公众"}, "disposition": "partial"})
    result = load_research_ledger(_write(tmp_path, payload))
    assert result["audience"] == {"primary": "科技公众"}
    assert result["disposition"] == "partial"


def test_wrong_schema_is_rejected_with_field_name(tmp_path: Path) -> None:
    payload = _ledger()
    payload["schema"] = "episode-research-ledger/v0"
    _assert_bad(tmp_path, payload, "schema")


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (lambda data: data["sources"][0].update(authority="forum"), "authority"),
        (lambda data: data["sources"][0].update(verification_state="maybe"), "verification_state"),
        (lambda data: data["sources"][0]["freshness"].update(policy="forever"), "freshness.policy"),
        (lambda data: data["claims"][0].update(evidence_status="certain"), "evidence_status"),
        (lambda data: data["claims"][0].update(wording_policy="shout"), "wording_policy"),
        (lambda data: data["claims"][0].update(freshness_requirement="stale"), "freshness_requirement"),
    ],
)
def test_invalid_frozen_enumerations_are_rejected(tmp_path: Path, mutate, field: str) -> None:
    payload = _ledger()
    mutate(payload)
    _assert_bad(tmp_path, payload, field)


def test_duplicate_source_id_is_rejected(tmp_path: Path) -> None:
    payload = _ledger()
    payload["sources"].append(_source("s1", publisher="交通运输部"))
    _assert_bad(tmp_path, payload, "source_id 重复")


def test_duplicate_claim_id_is_rejected(tmp_path: Path) -> None:
    payload = _ledger()
    payload["claims"].append(_claim("c1"))
    _assert_bad(tmp_path, payload, "claim_id 重复")


def test_claim_reference_to_unknown_source_is_rejected(tmp_path: Path) -> None:
    payload = _ledger()
    payload["claims"][0].update(source_ids=["missing"], fact_sources_present=0)
    _assert_bad(tmp_path, payload, "不存在的事实源")


def test_fact_sources_present_mismatch_is_rejected(tmp_path: Path) -> None:
    payload = _ledger()
    payload["claims"][0]["fact_sources_present"] = 0
    _assert_bad(tmp_path, payload, "fact_sources_present")


def test_confirmed_two_reliable_requires_two_publishers(tmp_path: Path) -> None:
    payload = _ledger()
    payload["sources"] = [
        _source("s1", publisher="同一媒体", authority="reliable_independent"),
        _source("s2", publisher="同一媒体", authority="reliable_independent"),
    ]
    payload["claims"] = [
        _claim(
            "c1",
            source_ids=["s1", "s2"],
            evidence_status="confirmed_two_reliable",
            wording_policy="assert",
        )
    ]
    _assert_bad(tmp_path, payload, "不足两个独立可靠来源")


def test_claim_array_fields_are_required(tmp_path: Path) -> None:
    payload = _ledger()
    payload["claims"][0]["source_ids"] = "s1"
    _assert_bad(tmp_path, payload, "source_ids")


def _topics() -> dict:
    return {
        "keyword_graph": {
            "seed": "平陆运河",
            "expanded": [],
            "subject_terms": [],
            "event_terms": [],
            "keywords_requested": [],
            "keywords_used": [],
            "keywords_truncated": False,
        },
        "topic_candidates": [],
        "selected_topic": None,
        "argument_graph": {
            "topic_id": "topic-01",
            "nodes": [
                {
                    "claim_id": "c1",
                    "dim": "event_core",
                    "claim": "平陆运河是西部陆海新通道骨干工程。",
                    "source_candidate_ids": ["s1"],
                }
            ],
            "edges": [],
        },
    }


def test_topics_argument_graph_validates_closed_claim_and_source_sets(tmp_path: Path) -> None:
    payload = _ledger()
    payload["topics"] = _topics()
    result = load_research_ledger(_write(tmp_path, payload))
    assert result["topics"] == payload["topics"]

    bad_claim = deepcopy(payload)
    bad_claim["topics"]["argument_graph"]["nodes"][0]["claim_id"] = "unknown-claim"
    _assert_bad(tmp_path, bad_claim, "不存在的 claim_id")

    bad_source = deepcopy(payload)
    bad_source["topics"]["argument_graph"]["nodes"][0]["source_candidate_ids"] = ["unknown-source"]
    _assert_bad(tmp_path, bad_source, "不存在的 source_id")


def test_topics_argument_graph_rejects_illegal_dimension_and_edge_endpoint(tmp_path: Path) -> None:
    payload = _ledger()
    payload["topics"] = _topics()
    payload["topics"]["argument_graph"]["nodes"][0]["dim"] = "made_up"
    _assert_bad(tmp_path, payload, "dim")

    payload = _ledger()
    payload["topics"] = _topics()
    payload["topics"]["argument_graph"]["edges"] = [
        {"from": "c1", "to": "missing", "relation": "supports"}
    ]
    _assert_bad(tmp_path, payload, "未引用已知节点")


def test_render_markdown_includes_url_date_and_conflicts_section() -> None:
    inputs = {"sources": [_source("s1")], "claims": [_claim("c1")]}
    conflict = _claim(
        "c2", evidence_status="conflicting", wording_policy="hedge", fact_sources_present=1
    )
    conflict["text"] = "不同来源对预计通航时间存在分歧。"
    inputs["claims"].append(conflict)

    rendered = render_ledger_markdown(inputs, theme="平陆运河", business_date="2026-09-17")

    assert "## 概览" in rendered
    assert "## 事实来源" in rendered
    assert "https://example.com/s1" in rendered
    assert "2026-09-16" in rendered
    assert "## 主张与证据状态" in rendered
    assert "## 冲突与待核" in rendered
    assert "不同来源对预计通航时间存在分歧" in rendered
    assert "并列来源" in rendered


def _config(tmp_path: Path) -> dict:
    return {
        "_project_root": str(tmp_path),
        "jobs": {
            "material_replication": {
                "episode_research_pack": {"ledger_root": "input/research-ledgers"}
            }
        },
    }


def test_discover_matches_file_content_not_filename(tmp_path: Path) -> None:
    root = tmp_path / "input" / "research-ledgers"
    mismatch = _ledger()
    mismatch["theme"] = "智元A3"
    _write(root, mismatch, "000-looks-like-match.json")
    matching = _ledger()
    expected = _write(root, matching, "zzz-unrelated-name.json")

    found = discover_research_ledger(
        _config(tmp_path), theme="平陆运河", business_date="2026-09-17"
    )
    assert found == expected.resolve()


def test_discover_ignores_invalid_json_and_is_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "input" / "research-ledgers"
    root.mkdir(parents=True)
    (root / "000-broken.json").write_text("{", encoding="utf-8")
    first = _write(root, _ledger(), "a.json")
    _write(root, _ledger(), "b.json")
    assert discover_research_ledger(
        _config(tmp_path), theme="平陆运河", business_date="2026-09-17"
    ) == first.resolve()


def test_discover_returns_canonical_path_even_when_json_is_corrupt(tmp_path: Path) -> None:
    """A canonically named ledger is an explicit input, never "no ledger".

    The A13 boundary: the canonical ``<business_date>-<theme>.json`` file must be
    returned unparsed so that a corrupt file surfaces as a loud
    ``ResearchLedgerError`` instead of being silently dropped by the scanner.
    """
    root = tmp_path / "input" / "research-ledgers"
    root.mkdir(parents=True)
    canonical = root / "2026-09-17-平陆运河.json"
    canonical.write_text("{", encoding="utf-8")

    found = discover_research_ledger(
        _config(tmp_path), theme="平陆运河", business_date="2026-09-17"
    )
    assert found == canonical.resolve()
    with pytest.raises(ResearchLedgerError, match="JSON"):
        load_research_ledger(found)


def test_discover_canonical_corrupt_json_does_not_fall_back_to_valid_candidate(
    tmp_path: Path,
) -> None:
    """A broken canonical ledger must not be silently replaced by a content match."""
    root = tmp_path / "input" / "research-ledgers"
    root.mkdir(parents=True)
    canonical = root / "2026-09-17-平陆运河.json"
    canonical.write_text("{", encoding="utf-8")
    _write(root, _ledger(), "zzz-valid-content-match.json")

    found = discover_research_ledger(
        _config(tmp_path), theme="平陆运河", business_date="2026-09-17"
    )
    assert found == canonical.resolve()


def test_discover_canonical_name_has_priority_over_content_scan(tmp_path: Path) -> None:
    """The canonical name wins without being parsed, even if its theme differs."""
    root = tmp_path / "input" / "research-ledgers"
    root.mkdir(parents=True)
    mismatch = _ledger()
    mismatch["theme"] = "智元A3"
    canonical = _write(root, mismatch, "2026-09-17-平陆运河.json")

    found = discover_research_ledger(
        _config(tmp_path), theme="平陆运河", business_date="2026-09-17"
    )
    assert found == canonical.resolve()


def test_discover_raises_for_unparseable_candidate_when_no_match(tmp_path: Path) -> None:
    """Corruption alone must not degrade into "no ledger" (A13)."""
    root = tmp_path / "input" / "research-ledgers"
    root.mkdir(parents=True)
    broken = root / "broken.json"
    broken.write_text("{", encoding="utf-8")

    with pytest.raises(ResearchLedgerError) as caught:
        discover_research_ledger(
            _config(tmp_path), theme="平陆运河", business_date="2026-09-17"
        )
    assert "broken.json" in str(caught.value)


def test_discover_returns_none_for_only_other_theme_valid_ledgers(tmp_path: Path) -> None:
    """A directory of only other episodes' valid ledgers is a genuine absence."""
    root = tmp_path / "input" / "research-ledgers"
    other = _ledger()
    other["theme"] = "智元A3"
    _write(root, other, "other-theme.json")

    assert (
        discover_research_ledger(
            _config(tmp_path), theme="平陆运河", business_date="2026-09-17"
        )
        is None
    )


def test_discover_prefers_valid_match_over_unrelated_broken_candidate(tmp_path: Path) -> None:
    """An unrelated broken file must not block a legitimate match."""
    root = tmp_path / "input" / "research-ledgers"
    root.mkdir(parents=True)
    (root / "000-broken.json").write_text("{", encoding="utf-8")
    match = _write(root, _ledger(), "zzz-match.json")

    assert discover_research_ledger(
        _config(tmp_path), theme="平陆运河", business_date="2026-09-17"
    ) == match.resolve()


def test_discover_raises_for_non_object_json_candidate_when_no_match(tmp_path: Path) -> None:
    """A parseable root that is not an object is still an illegal ledger candidate."""
    root = tmp_path / "input" / "research-ledgers"
    root.mkdir(parents=True)
    (root / "array.json").write_text("[]", encoding="utf-8")

    with pytest.raises(ResearchLedgerError) as caught:
        discover_research_ledger(
            _config(tmp_path), theme="平陆运河", business_date="2026-09-17"
        )
    message = str(caught.value)
    assert "array.json" in message
    assert "根节点必须是对象" in message


def test_discover_prefers_valid_match_over_non_object_candidate(tmp_path: Path) -> None:
    """A non-object candidate must not block a legitimate match."""
    root = tmp_path / "input" / "research-ledgers"
    root.mkdir(parents=True)
    (root / "000-array.json").write_text("[]", encoding="utf-8")
    match = _write(root, _ledger(), "zzz-match.json")

    assert discover_research_ledger(
        _config(tmp_path), theme="平陆运河", business_date="2026-09-17"
    ) == match.resolve()


def test_discover_missing_directory_returns_none(tmp_path: Path) -> None:
    assert discover_research_ledger(
        _config(tmp_path), theme="平陆运河", business_date="2026-09-17"
    ) is None


def test_load_is_byte_deterministic(tmp_path: Path) -> None:
    path = _write(tmp_path, _ledger())
    first = json.dumps(load_research_ledger(path), sort_keys=True, ensure_ascii=False)
    second = json.dumps(load_research_ledger(path), sort_keys=True, ensure_ascii=False)
    assert first.encode("utf-8") == second.encode("utf-8")


def test_missing_file_and_invalid_json_name_path_or_json_field(tmp_path: Path) -> None:
    with pytest.raises(ResearchLedgerError, match="path"):
        load_research_ledger(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(ResearchLedgerError, match="JSON"):
        load_research_ledger(broken)
