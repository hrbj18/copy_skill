"""Visual-proxy thresholds: the motion gate must be dimensionally correct (P2/P3).

Background
----------
``compute_visual_metrics`` used to compare a **ratio** (``motion_ratio``) against
a **per-frame delta** (``motion_threshold = 0.30``) -- two different dimensions
-- so the motion branch was always false and every decision fell through to OCR
(even the clips that *passed* had ``motion_frame_ratio == 0.0``).  The OCR side
had the mirror problem: it divided the *fingerprint-deduped* text list by the
frame count, so coverage was really "distinct-text density" and a missing sample
count silently became a ratio of ``1.0``.

The decision now lives in the pure :func:`visual_verdict` helper (no media IO),
which these tests pin directly.
"""

from __future__ import annotations

import pytest

from douyin_intelligence.replication_selection import (
    DEFAULT_MAX_OCR_COVERAGE,
    DEFAULT_MIN_MOTION_FRAME_RATIO,
    DEFAULT_MOTION_DELTA_THRESHOLD,
    visual_verdict,
)


def test_motion_gate_is_not_dead_moving_clip_passes_despite_heavy_text() -> None:
    """A moving clip passes even when most frames carry text (OCR ratio > cap).

    This is the exact case the old code got wrong: the motion branch was dead, so
    a text-heavy clip was rejected even though it clearly moved.
    """
    result = visual_verdict(
        motion_frame_ratio=0.8,          # plenty of motion
        frames_with_text=9, frames_scanned=10,  # 90% frames have text
        max_ocr_coverage=0.40,
    )
    assert result["motion_ok"] is True
    assert result["ocr_ok"] is False
    assert result["visual_ok"] is True   # motion OR ocr
    assert result["reason"] == ""


def test_static_text_heavy_clip_is_rejected_with_both_reasons() -> None:
    result = visual_verdict(
        motion_frame_ratio=0.0,
        frames_with_text=8, frames_scanned=10,   # 80% > 0.40
        max_ocr_coverage=0.40,
    )
    assert result["visual_ok"] is False
    assert result["motion_ok"] is False and result["ocr_ok"] is False
    assert "运动不足" in result["reason"]
    assert "文字过多" in result["reason"]


def test_static_light_text_clip_passes_via_ocr_branch() -> None:
    result = visual_verdict(
        motion_frame_ratio=0.0,
        frames_with_text=1, frames_scanned=10,   # 10% <= 0.40
        max_ocr_coverage=0.40,
    )
    assert result["visual_ok"] is True
    assert result["ocr_ok"] is True and result["motion_ok"] is False


def test_motion_ratio_floor_is_a_ratio_not_a_delta() -> None:
    """``min_motion_frame_ratio`` compares like-for-like with the ratio."""
    # ratio 0.3 passes the default 0.20 floor; ratio 0.1 does not.
    passing = visual_verdict(motion_frame_ratio=0.30, frames_with_text=10, frames_scanned=10, max_ocr_coverage=0.0)
    failing = visual_verdict(motion_frame_ratio=0.10, frames_with_text=10, frames_scanned=10, max_ocr_coverage=0.0)
    assert passing["motion_ok"] is True and passing["visual_ok"] is True
    assert failing["motion_ok"] is False and failing["visual_ok"] is False


def test_unmeasurable_ocr_is_not_faked_and_skips_the_text_gate() -> None:
    """A missing frame count must not become a fabricated ratio (old bug: 1.0)."""
    # No ``frames_with_text`` -> unmeasurable.  A static clip cannot be rescued
    # by a *made-up* 0.0 coverage, so it is rejected on motion alone.
    static = visual_verdict(
        motion_frame_ratio=0.0, frames_with_text=None, frames_scanned=10, max_ocr_coverage=0.40,
    )
    assert static["ocr_measurable"] is False
    assert static["ocr_ok"] is None
    assert static["ocr_text_frame_ratio"] == 0.0
    assert static["visual_ok"] is False
    assert "OCR 覆盖不可测" in static["reason"]
    # ... a moving clip still passes: the verdict rests on motion.
    moving = visual_verdict(
        motion_frame_ratio=0.9, frames_with_text=None, frames_scanned=10, max_ocr_coverage=0.40,
    )
    assert moving["visual_ok"] is True


def test_zero_scanned_frames_is_unmeasurable_not_full_coverage() -> None:
    result = visual_verdict(motion_frame_ratio=0.0, frames_with_text=0, frames_scanned=0, max_ocr_coverage=0.40)
    assert result["ocr_measurable"] is False
    assert result["visual_ok"] is False  # not silently rejected as "coverage 1.0"


def test_ocr_coverage_is_frame_ratio_clamped_to_one() -> None:
    result = visual_verdict(motion_frame_ratio=0.0, frames_with_text=25, frames_scanned=10, max_ocr_coverage=0.40)
    assert result["ocr_text_frame_ratio"] == 1.0
    assert result["ocr_ok"] is False


def test_defaults_are_sane_and_configurable_per_call() -> None:
    assert 0.0 < DEFAULT_MOTION_DELTA_THRESHOLD < 1.0
    assert 0.0 < DEFAULT_MIN_MOTION_FRAME_RATIO <= 1.0
    assert 0.0 < DEFAULT_MAX_OCR_COVERAGE <= 1.0
    # An explicit threshold overrides the default (config wiring).
    strict = visual_verdict(
        motion_frame_ratio=0.25, frames_with_text=0, frames_scanned=10,
        min_motion_frame_ratio=0.50, max_ocr_coverage=0.40,
    )
    assert strict["visual_ok"] is True  # rescued by the (0%) text branch
    strict_text = visual_verdict(
        motion_frame_ratio=0.25, frames_with_text=1, frames_scanned=10,
        min_motion_frame_ratio=0.50, max_ocr_coverage=0.0,
    )
    assert strict_text["visual_ok"] is False  # motion below floor, text above cap


def test_shipped_config_exposes_the_split_motion_keys() -> None:
    from douyin_intelligence.config import load_config
    from douyin_intelligence.replication_selection import material_settings

    settings = material_settings(load_config())
    assert "motion_threshold" not in settings  # the conflated key is gone
    assert float(settings["motion_delta_threshold"]) == pytest.approx(0.02)
    assert float(settings["min_motion_frame_ratio"]) == pytest.approx(0.2)
