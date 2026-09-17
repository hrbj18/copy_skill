"""Per-source duration windows (``jobs.material_replication.source_duration_windows``).

One *global* duration window cannot serve two platforms with opposite shapes.
Douyin's index is short clips (already inside the shipped ``10~300 s`` prefilter
window); Bilibili's is long-form (the measured pool ran ``268~12194 s``).  Widening
``prefilter.max_seconds`` to admit the Bilibili clips would also change which
Douyin clips survive -- a silent degradation of Douyin's selection quality.

``source_duration_windows`` overrides the window **per source** instead: a listed
source is judged by its own window at *every* checkpoint that runs the window
(the pre-download ``prefilter_candidates`` and the post-download
``measured_duration_window_reject``), while every other source -- and the whole
run, when the key is absent -- keeps the gate's original window, byte for byte.

These tests pin that independence: the same duration gets a different verdict
purely because of the candidate's ``source``, and the absent key changes nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from douyin_intelligence.config import ConfigurationError, load_config
from douyin_intelligence.replication_candidates import Candidate
from douyin_intelligence.replication_selection import (
    effective_duration_window,
    measured_duration_window_reject,
    prefilter_candidates,
    source_duration_windows,
)

BILIBILI_WINDOW = {"min_seconds": 10, "max_seconds": 1200}


def _candidate(video_id: str, *, source: str, duration: float) -> Candidate:
    candidate = Candidate(
        video_id=video_id, title=video_id, author="A", digg_count=10, duration_seconds=duration,
    )
    candidate.source = source
    return candidate


def _config_with_window(window: dict) -> dict:
    """The live config (the seam strips the shipped key) plus an explicit window."""
    config = load_config()
    config["jobs"]["material_replication"]["source_duration_windows"] = window
    return config


# --------------------------------------------------------------------------- #
# Reading the block
# --------------------------------------------------------------------------- #
def test_absent_key_reads_as_no_overrides() -> None:
    # The conftest seam strips the shipped key, so this is the absent-key default.
    assert "source_duration_windows" not in load_config()["jobs"]["material_replication"]
    assert source_duration_windows(load_config()) == {}


def test_block_is_read_as_source_to_window_pairs() -> None:
    config = load_config()
    config["jobs"]["material_replication"]["source_duration_windows"] = {
        "bilibili": {"min_seconds": 10, "max_seconds": 1200},
        "douyin": {"min_seconds": 5, "max_seconds": 60},
    }
    assert source_duration_windows(config) == {"bilibili": (10.0, 1200.0), "douyin": (5.0, 60.0)}


def test_malformed_entries_are_skipped_not_raised() -> None:
    """``config.load_config`` is the single validator; a hand-built config degrades."""
    config = load_config()
    config["jobs"]["material_replication"]["source_duration_windows"] = {
        "bilibili": {"min_seconds": 10, "max_seconds": 1200},
        "broken": "not-a-dict",
        "weird": {"min_seconds": "abc", "max_seconds": 10},
    }
    assert source_duration_windows(config) == {"bilibili": (10.0, 1200.0)}


# --------------------------------------------------------------------------- #
# ``effective_duration_window``
# --------------------------------------------------------------------------- #
def test_effective_window_uses_own_window_for_a_listed_source_only() -> None:
    overrides = {"bilibili": (10.0, 1200.0)}
    # A listed source gets its own window ...
    assert effective_duration_window(overrides, _candidate("b", source="bilibili", duration=60), 10.0, 300.0) == (
        10.0, 1200.0,
    )
    # ... every other source (and no source at all) keeps the defaults unchanged.
    assert effective_duration_window(overrides, _candidate("d", source="douyin", duration=60), 10.0, 300.0) == (
        10.0, 300.0,
    )
    assert effective_duration_window(overrides, _candidate("x", source="", duration=60), 10.0, 300.0) == (
        10.0, 300.0,
    )


# --------------------------------------------------------------------------- #
# Pre-download checkpoint: ``prefilter_candidates``
# --------------------------------------------------------------------------- #
def test_prefilter_verdict_depends_on_the_source_not_just_the_duration() -> None:
    config = _config_with_window({"bilibili": BILIBILI_WINDOW})
    candidates = [
        _candidate("dy-long", source="douyin", duration=600),        # 600 > 300 -> default window rejects
        _candidate("bili-long", source="bilibili", duration=600),    # within 10~1200 -> passes
        _candidate("bili-too-long", source="bilibili", duration=1500),  # 1500 > 1200 -> rejected
        _candidate("dy-ok", source="douyin", duration=60),           # passes
    ]

    passed, rejected = prefilter_candidates(candidates, config)

    assert [candidate.video_id for candidate in passed] == ["bili-long", "dy-ok"]
    assert {entry["video_id"] for entry in rejected} == {"dy-long", "bili-too-long"}


def test_prefilter_absent_key_is_byte_equivalent() -> None:
    """No key -> a long clip is dropped the same way for every source."""
    config = load_config()
    assert "source_duration_windows" not in config["jobs"]["material_replication"]
    candidates = [
        _candidate("dy", source="douyin", duration=600),
        _candidate("bili", source="bilibili", duration=600),
    ]

    passed, rejected = prefilter_candidates(candidates, config)

    assert passed == []
    assert {entry["video_id"] for entry in rejected} == {"dy", "bili"}


# --------------------------------------------------------------------------- #
# Post-download checkpoint: ``measured_duration_window_reject``
# --------------------------------------------------------------------------- #
def test_measured_window_uses_the_source_window_when_metadata_had_no_duration() -> None:
    config = _config_with_window({"bilibili": BILIBILI_WINDOW})

    # Bilibili 600 s: inside its own window -> kept.
    assert measured_duration_window_reject(600.0, config, metadata_duration=0.0, source="bilibili") == (False, "")
    # Douyin 600 s: outside the shared 10~300 window -> rejected.
    reject, reason = measured_duration_window_reject(600.0, config, metadata_duration=0.0, source="douyin")
    assert reject is True and "600" in reason
    # No source named -> the shared window (the pre-feature behaviour, byte for byte).
    assert measured_duration_window_reject(600.0, config, metadata_duration=0.0)[0] is True


def test_measured_window_is_a_noop_once_metadata_carried_a_duration() -> None:
    config = _config_with_window({"bilibili": BILIBILI_WINDOW})
    # The pre-download gate already judged it, so the post-download half stays out.
    assert measured_duration_window_reject(600.0, config, metadata_duration=600.0, source="douyin") == (False, "")


# --------------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------------- #
def _load_with(tmp_path: Path, window) -> dict:
    payload = json.loads(json.dumps(load_config()))
    payload["jobs"]["material_replication"]["source_duration_windows"] = window
    target = tmp_path / "config.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return load_config(target)


def test_config_accepts_a_valid_window(tmp_path: Path) -> None:
    # A valid window must not be rejected: ``_load_with`` raises on rejection.
    loaded = _load_with(tmp_path, {"bilibili": BILIBILI_WINDOW})
    # The seam strips the key from the *loaded* payload, so the acceptance signal
    # is the absence of a raise above; assert the seam really engaged (a failable
    # check) instead of reading the fixture file back, which ``load_config`` never
    # rewrites and so could never fail.
    assert "source_duration_windows" not in loaded["jobs"]["material_replication"]
    # Teeth: the same shape with min>max IS rejected, proving the check above ran.
    with pytest.raises(ConfigurationError):
        _load_with(tmp_path, {"bilibili": {"min_seconds": 1200, "max_seconds": 10}})


def test_config_rejects_bad_windows(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        _load_with(tmp_path, "not-a-dict")
    with pytest.raises(ConfigurationError):
        _load_with(tmp_path, {"bilibili": "not-a-dict"})
    with pytest.raises(ConfigurationError):
        _load_with(tmp_path, {"bilibili": {"min_seconds": "abc", "max_seconds": 1200}})
    # min > max (with a positive max) is invalid.
    with pytest.raises(ConfigurationError):
        _load_with(tmp_path, {"bilibili": {"min_seconds": 1200, "max_seconds": 10}})
    # A negative bound is invalid.
    with pytest.raises(ConfigurationError):
        _load_with(tmp_path, {"bilibili": {"min_seconds": -1, "max_seconds": 10}})
