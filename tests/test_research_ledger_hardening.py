"""Hardening tests: no silent degradation in discovery, no agency inflation.

Two failure modes are covered.  (1) A broken/absent ledger must never look
identical to "there is no ledger", so the discovery path either raises or
records the file it refused in a warning bucket.  (2) ``confirmed_two_reliable``
must mean two *agencies*: two outlets reprinting one wire story is one agency,
and the frozen publisher-level check cannot see that.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from douyin_intelligence.research_ledger import (
    LEDGER_SCHEMA,
    ResearchLedgerError,
    discover_research_ledger,
    load_research_ledger,
)

_THEME = "平陆运河"
_DATE = "2026-09-17"


# --- fixtures ---------------------------------------------------------------


def _config(tmp_path: Path, *, ledger_root=None) -> dict:
    """A config whose project root is ``tmp_path``; ``ledger_root=None`` omits the key."""
    block: dict = {"enabled": True}
    if ledger_root is not None:
        block["ledger_root"] = ledger_root
    return {
        "_project_root": str(tmp_path),
        "jobs": {"material_replication": {"episode_research_pack": block}},
    }


def _source(
    source_id: str,
    *,
    publisher: str,
    authority: str = "reliable_independent",
    origin_publisher: str | None = None,
) -> dict:
    source = {
        "source_id": source_id,
        "publisher": publisher,
        "title": f"来源标题-{source_id}",
        "url": f"https://example.com/{source_id}",
        "published_at": "2026-09-16",
        "excerpt": "支持该主张的原文摘录。",
        "authority": authority,
        "verification_state": "verified",
        "heat_only": False,
        "freshness": {
            "observed_at": "2026-09-17T10:00:00+08:00",
            "policy": "event_window",
            "status_at_publish": "fresh",
        },
    }
    if origin_publisher is not None:
        source["origin_publisher"] = origin_publisher
    return source


def _claim(
    claim_id: str,
    *,
    source_ids: list[str],
    evidence_status: str = "confirmed_two_reliable",
) -> dict:
    minimum = {"confirmed_official": 1, "confirmed_two_reliable": 2}.get(evidence_status, 0)
    return {
        "claim_id": claim_id,
        "topic_id": "topic-01",
        "text": "平陆运河是西部陆海新通道的骨干工程，可直接改写为口播完整句。",
        "evidence_status": evidence_status,
        "wording_policy": "assert",
        "freshness_requirement": "fresh",
        "source_ids": list(source_ids),
        "fact_sources_min": minimum,
        "fact_sources_present": len(source_ids),
        "claims_to_verify": [],
        "do_not_claim": [],
        "material_refs": [],
    }


def _ledger(*, sources: list[dict], claims: list[dict], theme: str = _THEME) -> dict:
    return {
        "schema": LEDGER_SCHEMA,
        "theme": theme,
        "business_date": _DATE,
        "sources": sources,
        "claims": claims,
    }


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# --- T1 / T2: an explicitly configured ledger_root is a promise -------------


def test_t1_explicit_ledger_root_pointing_at_a_file_raises(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "ledger-root-is-a-file.json"
    _write(not_a_dir, _ledger(sources=[_source("s1", publisher="新华社")], claims=[]))

    with pytest.raises(ResearchLedgerError) as caught:
        discover_research_ledger(
            _config(tmp_path, ledger_root=str(not_a_dir)),
            theme=_THEME,
            business_date=_DATE,
        )
    assert str(not_a_dir) in str(caught.value)


def test_t1_explicit_ledger_root_of_illegal_type_raises(tmp_path: Path) -> None:
    with pytest.raises(ResearchLedgerError) as caught:
        discover_research_ledger(
            _config(tmp_path, ledger_root=123), theme=_THEME, business_date=_DATE
        )
    assert "123" in str(caught.value)
    assert "ledger_root" in str(caught.value)


def test_t2_default_ledger_root_absent_directory_returns_none(tmp_path: Path) -> None:
    """The *default* root may be absent: that is the ordinary "no ledger yet" case."""
    assert (
        discover_research_ledger(_config(tmp_path), theme=_THEME, business_date=_DATE)
        is None
    )


def test_t2_default_ledger_root_still_discovers_when_present(tmp_path: Path) -> None:
    expected = _write(
        tmp_path / "input" / "research-ledgers" / "zzz-other-name.json",
        _ledger(sources=[_source("s1", publisher="新华社")], claims=[]),
    )
    found = discover_research_ledger(_config(tmp_path), theme=_THEME, business_date=_DATE)
    assert found == expected.resolve()


# --- T3: missing_identity warning bucket ------------------------------------


def test_t3_missing_identity_is_reported_other_episode_is_not(tmp_path: Path) -> None:
    root = tmp_path / "input" / "research-ledgers"
    no_theme = _ledger(sources=[_source("s1", publisher="新华社")], claims=[])
    del no_theme["theme"]
    identity_less = _write(root / "aaa-no-theme.json", no_theme)
    foreign = _write(
        root / "zzz-other-episode.json",
        _ledger(sources=[_source("s2", publisher="光明日报")], claims=[], theme="智元A3"),
    )

    diagnostics: dict[str, list[Path]] = {}
    found = discover_research_ledger(
        _config(tmp_path, ledger_root="input/research-ledgers"),
        theme=_THEME,
        business_date=_DATE,
        diagnostics=diagnostics,
    )

    assert found is None  # a genuine absence, not a silent skip
    assert diagnostics["missing_identity"] == [identity_less]
    # A complete ledger of another episode is normal: it appears in no bucket.
    assert foreign not in diagnostics["missing_identity"]
    assert not diagnostics["unreadable"]
    assert not diagnostics["non_object"]


def test_t3_missing_identity_is_reported_even_when_a_match_wins(tmp_path: Path) -> None:
    root = tmp_path / "input" / "research-ledgers"
    no_date = _ledger(sources=[_source("s1", publisher="新华社")], claims=[])
    del no_date["business_date"]
    identity_less = _write(root / "aaa-no-date.json", no_date)
    match = _write(
        root / "zzz-match.json",
        _ledger(sources=[_source("s2", publisher="光明日报")], claims=[]),
    )

    diagnostics: dict[str, list[Path]] = {}
    found = discover_research_ledger(
        _config(tmp_path, ledger_root="input/research-ledgers"),
        theme=_THEME,
        business_date=_DATE,
        diagnostics=diagnostics,
    )

    assert found == match.resolve()
    assert diagnostics["missing_identity"] == [identity_less]


# --- T4 / T5: confirmed_two_reliable means two agencies ---------------------


def test_t4_one_wire_story_reprinted_by_two_outlets_is_one_agency(tmp_path: Path) -> None:
    payload = _ledger(
        sources=[
            _source("s1", publisher="人民网", origin_publisher="新华社"),
            _source("s2", publisher="新华社"),
        ],
        claims=[_claim("c1", source_ids=["s1", "s2"])],
    )

    with pytest.raises(ResearchLedgerError) as caught:
        load_research_ledger(_write(tmp_path / "ledger.json", payload))
    message = str(caught.value)
    assert "c1" in message
    assert "s1" in message and "s2" in message
    assert "新华社" in message


def test_t5_two_real_agencies_pass_and_other_statuses_are_untouched(tmp_path: Path) -> None:
    payload = _ledger(
        sources=[
            _source("s1", publisher="新华社"),
            _source("s2", publisher="光明日报"),
        ],
        claims=[
            _claim("c1", source_ids=["s1", "s2"]),
            # A single source is legal for creator_primary: this check governs
            # confirmed_two_reliable only.
            _claim("c2", source_ids=["s1"], evidence_status="creator_primary"),
        ],
    )
    result = load_research_ledger(_write(tmp_path / "ledger.json", payload))
    assert [item["claim_id"] for item in result["claims"]] == ["c1", "c2"]


def test_t5_origin_publisher_alone_does_not_create_a_second_agency(tmp_path: Path) -> None:
    """Three outlets, but every story originates from 新华社: still one agency."""
    payload = _ledger(
        sources=[
            _source("s1", publisher="新华社", origin_publisher="新华社"),
            _source("s2", publisher="人民网", origin_publisher="新华社"),
            _source("s3", publisher="光明日报"),
        ],
        claims=[_claim("c1", source_ids=["s1", "s2", "s3"])],
    )
    # s1/s2 collapse to 新华社, s3 is 光明日报 -> two agencies, so this passes.
    result = load_research_ledger(_write(tmp_path / "ledger.json", payload))
    assert result["claims"][0]["claim_id"] == "c1"

    payload["sources"] = [
        _source("s1", publisher="新华社", origin_publisher="新华社"),
        _source("s2", publisher="人民网", origin_publisher="新华社"),
    ]
    payload["claims"] = [_claim("c1", source_ids=["s1", "s2"])]
    with pytest.raises(ResearchLedgerError, match="来源机构不独立"):
        load_research_ledger(_write(tmp_path / "ledger2.json", payload))


def test_t5_confirmed_official_with_one_agency_is_not_governed(tmp_path: Path) -> None:
    payload = _ledger(
        sources=[
            _source("s1", publisher="新华社", authority="official"),
            _source("s2", publisher="人民网", authority="official", origin_publisher="新华社"),
        ],
        claims=[_claim("c1", source_ids=["s1", "s2"], evidence_status="confirmed_official")],
    )
    result = load_research_ledger(_write(tmp_path / "ledger.json", payload))
    assert result["claims"][0]["evidence_status"] == "confirmed_official"
