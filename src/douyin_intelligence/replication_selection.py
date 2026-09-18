"""Visual proxy metrics and deterministic script/material replica selection.

All ordering is deterministic: ties break by ``(-heat_score, -duration,
video_id)``.  External effects (download, ffprobe, ASR, OCR, face detection)
are injected through :class:`ReplicationDeps` so the selection logic is
testable offline.
"""

from __future__ import annotations

import inspect
import re
import statistics
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .face_metrics import (
    FACE_UNAVAILABLE,
    face_settings,
    imread_unicode,
    truncated_face_class,
)
from .materials import MediaTooLargeError
from .replication_candidates import Candidate
from .replication_dedup import (
    cross_run_duplicate_reason,
    dedup_enabled,
    delivered_index_path,
    load_delivered_index,
)
from .replication_validation import (
    record_validation,
    validate_candidate,
    validation_enabled,
    validation_reason,
    validation_settings_snapshot,
)
from .sources.base import DownloadTarget, MediaResolutionError

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from .replication_pipeline import ReplicationDeps
    from .sources.base import MediaResolver


MEDIA_PROCESS_TIMEOUT_SECONDS = 180


def _run_media_process(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False, timeout=MEDIA_PROCESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, "", "media process timed out")


def material_replication_settings(config: dict[str, Any]) -> dict[str, Any]:
    return (config.get("jobs") or {}).get("material_replication") or {}


def script_settings(config: dict[str, Any]) -> dict[str, Any]:
    return material_replication_settings(config).get("script_replica") or {}


def material_settings(config: dict[str, Any]) -> dict[str, Any]:
    return material_replication_settings(config).get("material_replica") or {}


# --------------------------------------------------------------------------- #
# Per-source duration windows (opt-in)
#
# One *global* duration window cannot serve both a short-clip platform (Douyin's
# in-window clips) and a long-form one (Bilibili's 4~20-minute index): widening
# the global window to admit Bilibili would silently degrade Douyin's selection
# quality, and narrowing it starves Bilibili.  ``source_duration_windows``
# therefore overrides the window *per source*: a listed source is judged by its
# own window, every other source -- and the whole run, when the key is absent --
# keeps the gate's original window byte for byte.
# --------------------------------------------------------------------------- #
def source_duration_windows(config: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """``jobs.material_replication.source_duration_windows`` as ``{source: (min, max)}``.

    Absent/empty -> ``{}`` (no source is overridden, i.e. a strict no-op).  A
    malformed entry is skipped rather than raised here -- ``config.load_config``
    is the single place that rejects a bad window, so a hand-built test config
    degrades gracefully instead of turning a download into a crash.
    """
    raw = material_replication_settings(config).get("source_duration_windows") or {}
    if not isinstance(raw, dict):
        return {}
    windows: dict[str, tuple[float, float]] = {}
    for name, window in raw.items():
        if not isinstance(window, dict):
            continue
        try:
            window_min = float(window.get("min_seconds") or 0)
            window_max = float(window.get("max_seconds") or 0)
        except (TypeError, ValueError):
            continue
        windows[str(name)] = (window_min, window_max)
    return windows


def effective_duration_window(
    overrides: dict[str, tuple[float, float]],
    candidate: Candidate,
    default_min: float,
    default_max: float,
) -> tuple[float, float]:
    """The ``(min, max)`` window to judge ``candidate`` against.

    Returns the source's own window when the candidate's ``source`` is listed in
    ``overrides``, otherwise the gate's ``default_min``/``default_max``
    unchanged -- so the default path is the pre-feature window, byte for byte.
    """
    window = overrides.get(str(getattr(candidate, "source", "") or ""))
    if window is None:
        return default_min, default_max
    return window


# --------------------------------------------------------------------------- #
# Freshness gate (T6-1)
#
# ``material_replica.max_age_days`` (absent or ``0`` == **off**) drops a
# candidate whose ``published_at`` is older than the window *before* it is
# downloaded.  It is the one gate that can shrink the pool without spending a
# byte on the wire, which is exactly why it ships switched off: two individually
# reasonable gates already starved ``充电宝3C认证新规`` from 30/31 admissions to 0,
# so the window is set from behind a measurement, never by default.
# --------------------------------------------------------------------------- #
def material_max_age_days(material: dict[str, Any]) -> int:
    """``material_replica.max_age_days``; ``0`` means "do not judge freshness"."""
    raw = material.get("max_age_days", 0)
    if raw is None or raw == "":
        return 0
    try:
        days = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("素材时效闸门 max_age_days 必须是整数（0 表示不过滤）") from exc
    if days < 0:
        raise ValueError("素材时效闸门 max_age_days 必须 ≥ 0（0 表示不过滤）")
    return days


def published_age_days(candidate: Candidate, now: datetime, zone: ZoneInfo) -> float | None:
    """How old ``candidate`` is, or ``None`` when it carries no usable date.

    ``None`` means "cannot judge" and callers must let the candidate through.
    ``published_at`` is only as trustworthy as the collector that filled it, and
    refusing an undated clip would turn one missing field into a silent material
    famine -- the same honesty contract ``duration_pre`` follows for durations.
    """
    text = str(getattr(candidate, "published_at", "") or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return (now - parsed).total_seconds() / 86400.0


def freshness_block(
    max_days: int, ages_before: list[float], ages_after: list[float], undated: int
) -> dict[str, Any]:
    """The run-level ``material_freshness`` audit for the manifest.

    The two medians are the whole point: a gate can trim the tail while leaving
    the *median* clip as old as it always was, so reporting only the kept count
    would let a run claim freshness it does not have.
    """
    judged = len(ages_before)
    rejected = judged - len(ages_after)
    return {
        "max_age_days": max_days,
        "judged": judged,
        "rejected": rejected,
        "undated": undated,
        "stale_ratio": round(rejected / judged, 4) if judged else 0.0,
        "oldest_age_days": round(max(ages_before), 3) if ages_before else None,
        "median_age_days_before": round(float(statistics.median(ages_before)), 3) if ages_before else None,
        "median_age_days_after": round(float(statistics.median(ages_after)), 3) if ages_after else None,
    }


def _freshness_now(config: dict[str, Any], zone: ZoneInfo) -> datetime:
    """The gate's reference instant, as "now" in the configured timezone.

    ``config["_now"]`` is the test seam -- the same shape as the injected
    ``config["_project_root"]`` that :func:`replication_theme.project_path`
    honours.  It must *not* be confused with the ``clock`` argument of
    ``select_material_replicas``, which is a monotonic *float* clock used by the
    phase budgets and knows nothing about wall time.
    """
    injected = config.get("_now")
    if injected is not None:
        value = injected() if callable(injected) else injected
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=zone)
    return datetime.now(zone)


def validate_probe(probe: dict[str, Any] | None) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for a ffprobe payload.

    A downloaded file that lacks a decodable video stream (audio-only or an
    error payload) must be rejected before OCR/ASR/face work runs, otherwise it
    only surfaces much later as an opaque ffmpeg error ("Output file does not
    contain any stream").
    """
    if not probe:
        return False, "无 ffprobe 结果"
    if not probe.get("width") or not probe.get("height"):
        return False, "下载文件无视频流（ffprobe 未返回画面尺寸）"
    if float(probe.get("duration_seconds") or 0) <= 0:
        return False, "视频时长无效"
    return True, ""


# Douyin image-album posts carry no video stream at all.  Their ``video_download_url``
# is the post's background music, so downloading yields an MP3 (ID3) file that can only
# be rejected after the fact.  Skip them up front and report an actionable reason.
NON_VIDEO_AWEME_TYPES = frozenset({"68"})


def is_video_candidate(candidate: Candidate) -> tuple[bool, str]:
    """Return ``(usable, reason)`` for whether a candidate can yield a video stream."""
    aweme_type = str(getattr(candidate, "aweme_type", "") or "").strip()
    if aweme_type in NON_VIDEO_AWEME_TYPES:
        return False, f"图文作品（aweme_type={aweme_type}），无视频流"
    if getattr(candidate, "media_is_audio", False):
        return False, "下载地址指向音频（图文帖配乐），无视频流"
    return True, ""


# --------------------------------------------------------------------------- #
# Visual-proxy thresholds (P2/P3)
#
# These are two *different dimensions* and must never be compared to each other:
#
# * ``motion_delta_threshold`` -- a per-frame-pair gray delta (0~1).  A single
#   adjacent-frame pair "moved" when its mean abs-diff reaches this value.
# * ``min_motion_frame_ratio`` -- a *fraction of frame pairs* (0~1).  A clip has
#   enough motion when at least this fraction of its sampled pairs moved.
#
# The old config key ``motion_threshold: 0.30`` conflated the two (a ratio was
# compared against a per-pair delta), so the motion branch was dead code.  It is
# intentionally **not** mapped onto either new key: 0.30 has no correct meaning
# here.  The defaults below were calibrated on the 46 real Douyin clips / 3493
# adjacent-frame pairs of the 9.12 corpus (per-pair gray delta: min 0.00, p25
# 0.041, median 0.095, p90 0.265, max 0.939).  ``motion_delta_threshold=0.02``
# sits just under the 5th percentile (0.008) x median band -- above codec/denoise
# jitter yet below genuine motion; ``min_motion_frame_ratio=0.20`` demands that a
# fifth of the pairs actually moved.  At (0.02, 0.20) the corpus's per-video
# moving-pair ratio has median 0.96 / p10 0.71, and only 2/46 clips fall below
# the floor -- i.e. near-static clips fail while real clips pass comfortably.
# --------------------------------------------------------------------------- #
DEFAULT_MOTION_DELTA_THRESHOLD = 0.02
DEFAULT_MIN_MOTION_FRAME_RATIO = 0.20
DEFAULT_MAX_OCR_COVERAGE = 0.40

# --------------------------------------------------------------------------- #
# Single video cache root (P1a)
#
# The script chain, the material chain and the download-only loop must all cache
# a downloaded video under the *same* ``<media_root>/<REPLICATION_VIDEO_SUBDIR>/
# <video_id>.mp4``.  When they used different sub-roots (``script`` vs
# ``material``) the very same video was fetched once per chain -- real, measured
# waste (the 9.13 run pulled two ids twice: 33,998,762 B, ~26% of all traffic).
# Sharing the root makes the second chain a genuine cache hit: the file already
# exists, ``download_video`` returns before touching the body, and
# :func:`measure_transferred_bytes` charges 0 wire bytes while the idempotent
# delivered ledger still counts the one file (see :meth:`DownloadBudget.select`).
#
# The name is deliberately ``material`` (not a fresh ``video``): the material
# chain and download-only already used it and the persistent store is keyed on
# it, so reusing it also lets the next run *reuse* the already-downloaded media
# instead of orphaning it.  Stage-specific *scratch* keeps its own sub-dirs
# (``cache_root/script`` vs ``cache_root/material``) -- only the source ``.mp4``
# is shared.
# --------------------------------------------------------------------------- #
REPLICATION_VIDEO_SUBDIR = "material"


def visual_verdict(
    motion_frame_ratio: float,
    frames_with_text: int | None,
    frames_scanned: int,
    *,
    motion_delta_threshold: float = DEFAULT_MOTION_DELTA_THRESHOLD,
    min_motion_frame_ratio: float = DEFAULT_MIN_MOTION_FRAME_RATIO,
    max_ocr_coverage: float = DEFAULT_MAX_OCR_COVERAGE,
) -> dict[str, Any]:
    """Pure visual-proxy decision (no media IO) -- trivially unit-testable.

    Two independent, dimensionally-correct gates decide ``visual_ok``:

    * ``motion_ok``  = ``motion_frame_ratio >= min_motion_frame_ratio``;
    * ``ocr_ok``     = the fraction of OCR'd frames that carried text is
      ``<= max_ocr_coverage``.  When the OCR frame count is unknown
      (``frames_with_text is None`` or ``frames_scanned == 0``) the text signal
      is **unmeasurable**: ``ocr_ok`` is ``None`` and the text gate is *skipped*
      rather than passed, so it can neither rescue nor reject a clip -- the
      verdict then rests on motion alone.  A ratio is never fabricated from the
      deduped text catalogue.

    ``visual_ok = motion_ok or ocr_ok`` (with an unmeasurable ``ocr_ok`` treated
    as "no evidence either way", i.e. ``visual_ok = motion_ok``).

    ``reason`` names the criterion/criteria that failed (运动不足 / 文字过多 /
    OCR 覆盖不可测) so the caller can record *why* a clip was dropped.
    """
    ratio = float(motion_frame_ratio or 0.0)
    scanned = int(frames_scanned or 0)
    motion_ok = ratio >= float(min_motion_frame_ratio)
    ocr_measurable = frames_with_text is not None and scanned > 0
    ocr_ok: bool | None
    if ocr_measurable:
        ocr_ratio = min(1.0, max(0.0, int(frames_with_text) / scanned))
        ocr_ok = ocr_ratio <= float(max_ocr_coverage)
        visual_ok = bool(motion_ok or ocr_ok)
    else:
        ocr_ratio = 0.0
        ocr_ok = None  # unmeasurable: skip the text gate, never fake a ratio
        visual_ok = bool(motion_ok)
    reasons: list[str] = []
    if not motion_ok:
        reasons.append(
            f"运动不足（帧间变化≥{float(motion_delta_threshold):g} 的帧对占比 "
            f"{ratio:.2f} < {float(min_motion_frame_ratio):g}）"
        )
    if ocr_ok is False:
        reasons.append(f"文字过多（有字帧占比 {ocr_ratio:.2f} > {float(max_ocr_coverage):g}）")
    if not ocr_measurable:
        reasons.append("OCR 覆盖不可测（缺有字帧计数）")
    return {
        "motion_ok": motion_ok,
        "ocr_ok": ocr_ok,
        "ocr_measurable": ocr_measurable,
        "ocr_text_frame_ratio": round(ocr_ratio, 6),
        "visual_ok": visual_ok,
        # A *reject* reason: empty for a clip that passed (a criterion can fail
        # yet still be rescued by the other branch -- that is not a rejection).
        "reason": "、".join(reasons) if not visual_ok else "",
    }


@dataclass(slots=True)
class VisualMetrics:
    sampled_frames: int = 0
    motion_frame_ratio: float = 0.0
    ocr_text_frame_ratio: float = 0.0
    visual_ok: bool = False
    cache_hit: bool = False
    # --- additive fields (P2/P3): the two signals' thresholds and verdicts ----
    # Kept so the readme / process data can show *which* criterion decided the
    # verdict, instead of an opaque boolean.
    motion_delta_threshold: float = 0.0
    min_motion_frame_ratio: float = 0.0
    max_ocr_coverage: float = 0.0
    ocr_measurable: bool = True
    motion_ok: bool = False
    ocr_ok: bool | None = None
    reject_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sort_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Deterministic candidate order: heat desc, duration desc, video_id asc."""
    return sorted(candidates, key=lambda item: (-item.heat_score, -item.duration_seconds, item.video_id))


def pool_heat_median(candidates: list[Candidate]) -> float:
    if not candidates:
        return 0.0
    return float(statistics.median(candidate.heat_score for candidate in candidates))


def script_candidate_pool(candidates: list[Candidate], config: dict[str, Any]) -> list[Candidate]:
    """Top-heat, in-duration candidates eligible for the script replica."""
    settings = script_settings(config)
    ordered = sort_candidates(candidates)
    if not ordered:
        return []
    top_ratio = float(settings.get("top_ratio") or 0.10)
    min_top = int(settings.get("min_top") or 5)
    cutoff = max(min_top, _ceil(len(ordered) * top_ratio))
    min_seconds = float(settings.get("min_seconds") or 30)
    max_seconds = float(settings.get("max_seconds") or 300)
    pool = [candidate for candidate in ordered[:cutoff] if min_seconds <= candidate.duration_seconds <= max_seconds]
    if not pool:
        # Duration may be unknown from search metadata; keep heat-qualified rows.
        pool = [candidate for candidate in ordered[:cutoff] if candidate.duration_seconds <= 0]
    return pool


def _ceil(value: float) -> int:
    import math
    return int(math.ceil(value))


def evaluate_script_transcript(transcript: dict[str, Any] | None, duration: float, config: dict[str, Any]) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for the script-replica speech density gate."""
    settings = script_settings(config)
    min_chars = int(settings.get("min_chars") or 150)
    min_cps = float(settings.get("min_chars_per_second") or 1.2)
    if not transcript:
        return False, "未获得转写结果"
    status = str(transcript.get("status") or "unknown")
    if status != "success":
        detail = str(transcript.get("error") or "").strip()[:120]
        suffix = f"（{detail}）" if detail else "，无有效口播"
        return False, f"ASR 状态 {status}{suffix}"
    text = re.sub(r"\s+", "", str(transcript.get("text") or ""))
    chars = len(text)
    if chars < min_chars:
        return False, f"口播字数 {chars} 低于 {min_chars}"
    chars_per_second = chars / max(1.0, float(duration))
    if chars_per_second < min_cps:
        return False, f"口播密度 {chars_per_second:.2f} 低于 {min_cps}"
    return True, ""


def material_candidate_pool(candidates: list[Candidate], config: dict[str, Any]) -> tuple[list[Candidate], float]:
    """Candidates eligible for material selection, in deterministic order.

    Heat is used to *order* candidates, not to discard them.  Whether a clip is usable
    as footage (no face, moving picture, little speech) is largely independent of how
    popular the post is, whereas a half-pool median cut removes exactly the mid-tier
    creators who tend to publish hands-on footage.  ``heat_gate_percentile`` therefore
    defaults to 0.0 (keep every candidate); raise it to reintroduce a heat floor.
    """
    median = pool_heat_median(candidates)
    percentile = float(material_settings(config).get("heat_gate_percentile") or 0.0)
    if percentile <= 0.0 or not candidates:
        return sort_candidates(list(candidates)), median
    values = sorted(candidate.heat_score for candidate in candidates)
    index = min(len(values) - 1, max(0, int(round((len(values) - 1) * percentile))))
    threshold = values[index]
    eligible = [candidate for candidate in candidates if candidate.heat_score >= threshold]
    return sort_candidates(eligible), median


def prefilter_settings(config: dict[str, Any]) -> dict[str, Any]:
    """The ``jobs.material_replication.prefilter`` block (``{}`` when absent)."""
    return material_replication_settings(config).get("prefilter") or {}


def prefilter_exclude_terms(config: dict[str, Any]) -> list[str]:
    """The effective exclude terms for the pre-download gate.

    Reads ``jobs.material_replication.prefilter.exclude_terms`` and returns the
    terms as written (trimmed, de-duplicated case-insensitively, original order
    preserved) so the audit artifacts can show *exactly* what was applied.  There
    is deliberately **no global default** -- the block defaults to ``[]`` -- so a
    blank config never silently drops candidates.  A bare string is accepted and
    treated as a one-element list, mirroring the CLI's repeatable ``--exclude-term``.
    """
    raw = prefilter_settings(config).get("exclude_terms") or []
    if isinstance(raw, str):
        raw = [raw]
    terms: list[str] = []
    seen: set[str] = set()
    for value in raw:
        text = str(value or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            terms.append(text)
    return terms


def prefilter_active(config: dict[str, Any]) -> bool:
    """Whether the pre-download gate will drop anything at all.

    ``True`` when the duration/heat gate is enabled **or** at least one exclude
    term is effective.  The exclude gate is deliberately independent of
    ``prefilter.enabled``: a term the user explicitly supplied (config or
    ``--exclude-term``) must never be silently dropped just because the
    duration/heat switches happen to be off.  With the shipped ``exclude_terms:
    []`` and ``enabled: false`` the gate is inert, so the "three switches off =>
    no layer artifact" contract still holds by default.
    """
    return bool(prefilter_settings(config).get("enabled", False)) or bool(prefilter_exclude_terms(config))


def prefilter_drop_non_video(config: dict[str, Any]) -> bool:
    """Whether the media-type gate drops non-video posts (default ``True``).

    This gate is **governed by** ``prefilter.enabled`` (unlike ``exclude_terms``):
    with the shipped ``enabled: false`` the whole prefilter is inert and the
    "no layer artifact + behavioural equivalence" contract is preserved
    exactly.  Non-video posts (image albums, ``aweme_type=68``) are already
    skipped by the download loop, so turning the switch off only changes the
    *pool composition* the ordering sees -- never the delivered result.  Reset it
    to ``False`` to restore the pre-change pool exactly.
    """
    return bool(prefilter_settings(config).get("drop_non_video", True))


# Distinct ``pre_*`` stages so a pre-download rejection can never be confused
# with a downstream gate that shares a similar meaning (``duration`` / ``pool``).
_PREFILTER_EXCLUDE_STAGE = "pre_exclude"
_PREFILTER_MEDIA_TYPE_STAGE = "pre_media_type"
_PREFILTER_DURATION_STAGE = "pre_duration"
_PREFILTER_HEAT_STAGE = "pre_heat"


def _prefilter_reject(candidate: Candidate, stage: str, reason: str) -> dict[str, Any]:
    """A rejection record in the project-wide ``unmet`` shape, fit for review.

    Carries the metadata the gate actually saw (duration / heat / aweme_type /
    title / author) so a reader can tell whether a candidate was wrongly
    dropped.  ``aweme_type`` is included for every stage because it is cheap and
    makes the media-type gate's verdict auditable without a second lookup.
    """
    return {
        "video_id": candidate.video_id,
        "stage": stage,
        "reason": reason,
        "aweme_type": str(getattr(candidate, "aweme_type", "") or ""),
        "duration_seconds": round(float(candidate.duration_seconds or 0), 3),
        "heat_score": round(float(candidate.heat_score or 0.0), 6),
        "title": candidate.title,
        "author": candidate.author,
    }


def _prefilter_heat_threshold(candidates: list[Candidate], percentile: float) -> float | None:
    """Heat floor for the prefilter, or ``None`` when the gate is off.

    Fully independent of ``material_replica.heat_gate_percentile``: this is an
    explicit opt-in download-time floor, not the material-pool ordering floor.
    """
    if percentile <= 0.0 or not candidates:
        return None
    values = sorted(candidate.heat_score for candidate in candidates)
    index = min(len(values) - 1, max(0, int(round((len(values) - 1) * percentile))))
    return values[index]


def prefilter_candidates(
    candidates: list[Candidate], config: dict[str, Any]
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """Metadata-only gate applied once, before *any* download.

    Pure function: no IO, no network.  A candidate is dropped purely from the
    search metadata that was already collected -- a downloaded file is never
    needed to know its length, so the pipeline should not have to spend the
    bandwidth before it can apply a duration window.

    Three gates are supported, evaluated in this order:

    * the **exclude-term gate** (``exclude_terms``): the title, casefolded, is
      dropped when it contains *any* exclude term as a substring -- a blunt
      "keep these out of the pool" switch for content the theme must never use.
      This gate is **independent of ``enabled``**: a term the user explicitly
      supplied must never be silently dropped just because the duration/heat
      switches are off.  It runs first because it is the only gate driven by an
      explicit user input, so its attribution must be the one a reader sees;
    * the **media-type gate** (``drop_non_video``, default ``True``, runs only
      when ``enabled`` is true): reuses :func:`is_video_candidate` to drop posts
      that carry no video stream at all (Douyin image albums, ``aweme_type=68``,
      or an audio download address).  A hard validity fact, so it is judged
      before the window gates -- there is no point asking whether a 0 s image
      album is "in duration";
    * the **duration window** (``min_seconds`` / ``max_seconds``), where
      ``duration_seconds <= 0`` (metadata missing) is *kept* when
      ``allow_unknown_duration`` is true, mirroring ``script_candidate_pool`` --
      runs only when ``enabled`` is true;
    * the optional **heat floor** (``heat_gate_percentile``), defaulting to
      ``0.0`` (off) and decoupled from ``material_replica.heat_gate_percentile``
      -- runs only when ``enabled`` is true.

    Returns ``(passed, rejected)``.  Only when the duration/heat gate is off
    **and** no exclude term is effective is the input list returned untouched
    with an empty rejection list, so a caller with the shipped ``enabled: false``
    and ``exclude_terms: []`` keeps its pre-change behaviour exactly.
    """
    settings = prefilter_settings(config)
    enabled = bool(settings.get("enabled", False))
    exclude_terms = prefilter_exclude_terms(config)
    if not enabled and not exclude_terms:
        return list(candidates), []

    min_seconds = float(settings.get("min_seconds") or 0)
    max_seconds = float(settings.get("max_seconds") or 0)
    percentile = float(settings.get("heat_gate_percentile") or 0.0)
    allow_unknown = bool(settings.get("allow_unknown_duration", True))
    drop_non_video = enabled and prefilter_drop_non_video(config)
    threshold = _prefilter_heat_threshold(candidates, percentile) if enabled else None
    # Per-source windows are resolved once, up front: a listed source is judged by
    # its own window *instead of* the shared one, so the global window is never
    # loosened for everyone (see ``source_duration_windows``).
    window_overrides = source_duration_windows(config)

    passed: list[Candidate] = []
    rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        if exclude_terms:
            title_folded = str(candidate.title or "").casefold()
            matched = next((term for term in exclude_terms if term.casefold() in title_folded), None)
            if matched is not None:
                rejected.append(
                    _prefilter_reject(candidate, _PREFILTER_EXCLUDE_STAGE, f"标题命中排除词「{matched}」")
                )
                continue
        if not enabled:
            # Exclude-only mode: the duration/heat windows are switched off, so
            # every surviving candidate passes untouched.
            passed.append(candidate)
            continue
        if drop_non_video:
            usable, media_reason = is_video_candidate(candidate)
            if not usable:
                rejected.append(
                    _prefilter_reject(candidate, _PREFILTER_MEDIA_TYPE_STAGE, media_reason)
                )
                continue
        duration = float(candidate.duration_seconds or 0)
        if duration <= 0:
            # Metadata may simply be missing; do not punish a candidate for it.
            if not allow_unknown:
                rejected.append(
                    _prefilter_reject(
                        candidate, _PREFILTER_DURATION_STAGE, "元数据缺失时长，且未允许未知时长放行"
                    )
                )
                continue
        else:
            candidate_min, candidate_max = effective_duration_window(
                window_overrides, candidate, min_seconds, max_seconds
            )
            if (candidate_min > 0 and duration < candidate_min) or (
                candidate_max > 0 and duration > candidate_max
            ):
                lower = f"{candidate_min:.0f}s" if candidate_min > 0 else "不限"
                upper = f"{candidate_max:.0f}s" if candidate_max > 0 else "不限"
                rejected.append(
                    _prefilter_reject(
                        candidate, _PREFILTER_DURATION_STAGE, f"时长 {duration:.0f}s 不在 {lower}~{upper}"
                    )
                )
                continue
        if threshold is not None and candidate.heat_score < threshold:
            rejected.append(
                _prefilter_reject(
                    candidate,
                    _PREFILTER_HEAT_STAGE,
                    f"热度 {candidate.heat_score:.3f} 低于预筛下限 {threshold:.3f}",
                )
            )
            continue
        passed.append(candidate)
    return passed, rejected


def measured_duration_window_reject(
    measured: float,
    config: dict[str, Any],
    *,
    metadata_duration: float = 0.0,
    source: str = "",
) -> tuple[bool, str]:
    """Post-download half of the "一个窗口、两处执行" duration gate.

    The prefilter window (``min_seconds`` / ``max_seconds``) cannot judge a
    candidate whose metadata carries no duration, so such a candidate passes the
    *pre*-download gate (``allow_unknown_duration``).  Once the file is
    downloaded its real length is known, so the **same** window is executed
    again against the measured duration -- one config, two checkpoints.  A file
    outside the window is treated exactly like a validation failure: it is not
    delivered, does not charge the budget, and the loop moves on.

    Returns ``(reject, reason)``.  No-op when the prefilter is disabled, when the
    metadata already carried a duration (the pre-download gate already judged
    it), or when the window is open on both sides.

    ``source`` names the candidate's origin so a per-source window
    (``source_duration_windows``) is applied here exactly as it was at the
    pre-download checkpoint; it defaults to ``""`` (no source -> the shared
    window) so every existing caller keeps the original behaviour.
    """
    settings = prefilter_settings(config)
    if not bool(settings.get("enabled", False)):
        return False, ""
    if float(metadata_duration or 0) > 0:
        return False, ""
    min_seconds = float(settings.get("min_seconds") or 0)
    max_seconds = float(settings.get("max_seconds") or 0)
    window = source_duration_windows(config).get(str(source or ""))
    if window is not None:
        min_seconds, max_seconds = window
    if (min_seconds <= 0 and max_seconds <= 0) or measured <= 0:
        return False, ""
    if (min_seconds > 0 and measured < min_seconds) or (max_seconds > 0 and measured > max_seconds):
        lower = f"{min_seconds:.0f}s" if min_seconds > 0 else "不限"
        upper = f"{max_seconds:.0f}s" if max_seconds > 0 else "不限"
        return (
            True,
            f"实测时长 {measured:.0f}s 不在下载前预筛窗口 {lower}~{upper}"
            f"（元数据无时长，下载后按实测判定）",
        )
    return False, ""


def download_budget_settings(config: dict[str, Any]) -> dict[str, Any]:
    """The ``jobs.material_replication.download_budget`` block (``{}`` absent)."""
    return material_replication_settings(config).get("download_budget") or {}


# Ranking key = theme relevance -> heat -> video_id.  Visual quality is the
# user's *top* priority, but it is physically undecidable before a decode, so
# it is deliberately excluded from the pre-download order and this limitation
# is stated in the delivery instead of being silently glossed over.
RANKING_LIMITATION_NOTE = (
    "下载前无法评估画面质量（需解码帧才能判断），故本期排序为：题材相关度 → 热度 → video_id；"
    "画面质量留待下载后的校验/筛选阶段，不参与下载预算排序。"
)


def _term_tokens(term: str) -> list[str]:
    """Casefolded, non-empty whitespace-delimited tokens of a relevance term."""
    return [token for token in str(term or "").casefold().split() if token]


def term_hits_title(term: str, title_casefolded: str) -> bool:
    """True when *every* token of ``term`` appears in the casefolded title.

    AND semantics: a multi-word phrase such as ``苹果折叠屏 实测`` hits only when
    both ``苹果折叠屏`` and ``实测`` occur in the title.  Chinese is not
    whitespace-segmented, so a plain ``in`` test is exactly right for each token;
    the whitespace split only means "these must co-occur" (a single-token term
    degrades to a plain substring test).

    The candidate's ``source_keyword`` -- the query that surfaced it -- is
    deliberately **not** consulted: counting it would let every candidate
    self-certify membership in the theme and no candidate could ever score zero.
    """
    tokens = _term_tokens(term)
    if not tokens:
        return False
    return all(token in title_casefolded for token in tokens)


def _dedup_terms(theme: str, keywords: list[str] | None) -> list[str]:
    """The theme plus its keyword expansion, de-duplicated (case-sensitive key)."""
    terms: list[str] = []
    seen: set[str] = set()
    for raw in [theme, *(keywords or [])]:
        key = str(raw or "").strip()
        if key and key not in seen:
            seen.add(key)
            terms.append(key)
    return terms


def subject_hit_count(candidate: Candidate, terms: list[str]) -> int:
    """How many subject ``terms`` the candidate title mentions.

    Same matcher as the relevance scores (:func:`term_hits_title`): one term hits
    when *all* of its tokens occur in the title, so a single-token subject term
    degrades to a plain substring test.  Used by the relevance gate, which must
    answer "does this title mention the subject at all", independently of how
    many search terms the pool happens to make live.
    """
    title_folded = str(getattr(candidate, "title", "") or "").casefold()
    return sum(1 for term in terms if term_hits_title(term, title_folded))


def candidate_relevance(candidate: Candidate, terms: list[str]) -> float:
    """Share of ``terms`` whose tokens all appear in the candidate title.

    Model-free and explainable: no LLM, no embeddings.  The denominator here is
    ``len(terms)``; the pipeline uses :func:`relevance_report`, whose denominator
    is the pool-aware *live-term* count (dead terms are excluded), via
    :func:`build_relevance_index`.
    """
    if not terms:
        return 0.0
    title_folded = str(getattr(candidate, "title", "") or "").casefold()
    hits = sum(1 for term in terms if term_hits_title(term, title_folded))
    return round(hits / len(terms), 6)


def relevance_report(
    candidates: list[Candidate],
    theme: str,
    keywords: list[str] | None = None,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Explainable relevance plus the *live-term* denominator it was scaled by.

    A term is **live** when at least one candidate title in the current pool hits
    it; a term that hits *nothing* (e.g. a space-containing phrase that never
    co-occurs, or a brand alias the pool does not use) is **dead** and excluded
    from the denominator -- otherwise a pool-independent construct nobody matches
    would depress every score and blur the tier boundaries.

    ``relevance = live_hits / live_count``.  When ``live_count == 0`` (no term
    hits anything) every candidate scores ``0.0`` and ``degraded`` is ``True``,
    so the caller can surface "relevance could not discriminate this pool"
    without a division by zero.

    ``degraded`` additionally covers the *pool-level subject floor*: with
    ``relevance_gate.min_hit_ratio`` set (and a non-empty subject vocabulary),
    a pool whose ``hit_ratio`` falls below it is degraded -- "this pool does not
    contain the theme".  That is deliberately a low sanity floor, never a
    quality bar; per-candidate admission is the download gate's job.  Both the
    threshold and the verdict are reported back (``min_hit_ratio``,
    ``below_subject_floor``) so the cause of a degraded run is attributable.

    The *subject* dimension answers a different question than the scores do: a
    score is relative to however many terms the pool made live, while
    ``subject_hit_ids`` / ``hit_ratio`` say how much of the pool mentions the
    theme's subject at all (``replication_theme.subject_terms``).  The 9.14
    Microduck run needed exactly that: every keyword scored 0 (whole-phrase
    matching) yet 52% of titles contained ``microduck``, so the pool looked
    undifferentiated while it was in fact half on-topic.  ``config`` only
    supplies the optional ``subject_aliases`` dictionary and the subject floor;
    ``None`` works and uses the built-in table.

    Returns ``{theme, terms, live_terms, dead_terms, live_count, dead_count,
    degraded, below_subject_floor, min_hit_ratio, scores, subject_terms,
    subject_hit_ids, subject_hits, hit_ratio}`` where ``scores`` is
    ``video_id -> float``.
    """
    from .replication_theme import subject_terms as _subject_terms

    terms = _dedup_terms(theme, keywords)
    titles = {candidate.video_id: str(getattr(candidate, "title", "") or "").casefold() for candidate in candidates}
    live_terms = [term for term in terms if any(term_hits_title(term, text) for text in titles.values())]
    live_count = len(live_terms)
    live_set = set(live_terms)
    dead_terms = [term for term in terms if term not in live_set]
    scores: dict[str, float] = {}
    for candidate in candidates:
        text = titles[candidate.video_id]
        hits = sum(1 for term in live_terms if term_hits_title(term, text))
        scores[candidate.video_id] = round(hits / live_count, 6) if live_count else 0.0

    resolved_subject_terms = _subject_terms(theme, config or {})
    subject_hit_ids: list[str] = []
    seen_hit_ids: set[str] = set()
    for candidate in candidates:
        if candidate.video_id in seen_hit_ids:
            continue
        if subject_hit_count(candidate, resolved_subject_terms):
            seen_hit_ids.add(candidate.video_id)
            subject_hit_ids.append(candidate.video_id)
    subject_hits = len(subject_hit_ids)
    hit_ratio = round(subject_hits / len(candidates), 6) if candidates else 0.0
    # Pool-level sanity floor.  ``hit_ratio`` is a *pool* number (denominator = the
    # whole candidate pool, ~120 rows), so it can only answer "is this theme in
    # this pool at all" -- it must never be used as a per-run quality bar (a high
    # one would mark every period degraded and destroy the signal).  Real
    # "every delivered clip is on-topic" is enforced by the per-candidate download
    # gate in ``select_material_replicas``.  Only meaningful when the theme
    # actually yielded a subject vocabulary: a theme with no subject term cannot
    # be gated against, so the threshold must NOT fire there (that would flip
    # ``degraded`` for a legitimate theme -- a false positive).  Absent / zero
    # ``min_hit_ratio`` keeps ``degraded`` byte-identical to ``live_count == 0``.
    min_hit_ratio = 0.0
    if config:
        min_hit_ratio = float(
            (material_replication_settings(config).get("relevance_gate") or {}).get("min_hit_ratio") or 0.0
        )
    below_subject_floor = bool(resolved_subject_terms) and min_hit_ratio > 0 and hit_ratio < min_hit_ratio
    return {
        "theme": theme,
        "terms": terms,
        "live_terms": live_terms,
        "dead_terms": dead_terms,
        "live_count": live_count,
        "dead_count": len(dead_terms),
        "degraded": live_count == 0 or below_subject_floor,
        "below_subject_floor": below_subject_floor,
        "min_hit_ratio": min_hit_ratio,
        "scores": scores,
        "subject_terms": resolved_subject_terms,
        "subject_hit_ids": subject_hit_ids,
        "subject_hits": subject_hits,
        "hit_ratio": hit_ratio,
    }


def build_relevance_index(
    candidates: list[Candidate], theme: str, keywords: list[str] | None = None
) -> dict[str, float]:
    """Map ``video_id`` -> relevance in ``[0, 1]``, scaled by the live-term count.

    Delegates to :func:`relevance_report` so the returned index and the reported
    ``live_terms`` / ``dead_terms`` attribution can never drift apart.
    """
    return relevance_report(candidates, theme, keywords)["scores"]


def ranked_candidates(
    candidates: list[Candidate], relevance: dict[str, float] | None = None
) -> list[Candidate]:
    """Deterministic download order: relevance desc, heat desc, video_id asc."""
    scores = relevance or {}
    return sorted(
        candidates,
        key=lambda item: (-float(scores.get(item.video_id, 0.0)), -float(item.heat_score), item.video_id),
    )


#: Weight of one step down a profile's ``preferred_source_kinds`` / ``main_roles``
#: ranking.  Deliberately small: the *explicit* ``original_source_bonus`` must
#: dominate an ordering position, and the step only ever breaks near-ties.
MATERIAL_PROFILE_BONUS_STEP = 0.01


def _profile_metric(value: Any) -> float:
    """A profile number, or ``0.0`` when absent/malformed (never raises)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _profile_rank(value: str, ranked: Any) -> float:
    """Bonus for ``value`` sitting early in a profile's ranked vocabulary.

    ``0.0`` when ``value`` is empty, ``ranked`` is not a list/tuple, or ``value``
    is absent -- so an unknown label never scores, it merely stays unranked.
    """
    if not value or not isinstance(ranked, (list, tuple)):
        return 0.0
    try:
        index = ranked.index(value)
    except ValueError:
        return 0.0
    return MATERIAL_PROFILE_BONUS_STEP * (len(ranked) - index)


def material_profile_bonus(labels: Any, profile: Any) -> float:
    """Deterministic source/role bonus for one candidate, from its material labels.

    The integration point with the theme-profile layer
    (``replication_theme.resolve_material_profile`` / ``infer_material_labels``).
    ``profile`` is the resolved profile mapping and ``labels`` that candidate's
    label mapping; both may be ``None``, in which case the bonus is ``0.0`` and
    the ordering falls back to theme/event hits, heat, duration and ``video_id``.

    Only **explicit** profile facts contribute (higher = preferred):

    * ``original_source_bonus`` for an ``official_original`` source -- the single
      documented "original source" credit;
    * a small step per position in ``preferred_source_kinds``;
    * a small step per position in ``main_roles``.

    Nothing is inferred from a name that merely *sounds* official, and an
    unidentified candidate keeps the rest of the key unchanged.  A creator
    commentary clip is therefore never hard-displaced -- it only loses a small
    tie-breaker -- which is exactly the "official is a bonus, not an admission
    requirement" rule of the 2026-09-18 guide.
    """
    if not isinstance(labels, Mapping) or not isinstance(profile, Mapping):
        return 0.0
    kind = str(labels.get("source_kind") or "")
    role = str(labels.get("visual_role") or "")
    bonus = 0.0
    if kind == "official_original":
        bonus += _profile_metric(profile.get("original_source_bonus"))
    bonus += _profile_rank(kind, profile.get("preferred_source_kinds"))
    bonus += _profile_rank(role, profile.get("main_roles"))
    return bonus


def material_rank_key(
    candidate: Candidate,
    probe: Mapping[str, Any] | None = None,
    *,
    theme_terms: Sequence[str] | None = None,
    event_terms: Sequence[str] | None = None,
    labels: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | None = None,
) -> tuple[Any, ...]:
    """Explainable, stable material ordering key: theme hits, event hits, profile
    source/role bonus, weighted heat, duration, ``video_id``.  A profile's
    explicit ``event_term_weight`` and ``heat_weight`` scale only their own
    components; absent/invalid values fall back to ``1.0``.

    Replaces the old face-first ordering (``_FACE_RANK`` of the pipeline).
    ``face_class`` is descriptive metadata only after 2026-09-18 -- a themed run
    may legitimately want a host, an interview or an on-site recording on screen
    -- so no face term appears here.  Every component is a field the run already
    carries (title, ``heat_score``, the probed duration, ``video_id``) plus the
    profile source/role term (:func:`material_profile_bonus`, ``0.0`` when no
    profile/labels are supplied).  The tie-break on ``video_id`` ascending makes
    the order total and reproducible; ``probe`` is read through ``.get`` so a
    missing/odd payload degrades to duration ``0`` instead of raising.
    """
    title_folded = str(getattr(candidate, "title", "") or "").casefold()
    theme_hits = sum(1 for term in (theme_terms or []) if term_hits_title(term, title_folded))
    event_hits = sum(1 for term in (event_terms or []) if term_hits_title(term, title_folded))
    profile_payload = profile if isinstance(profile, Mapping) else {}
    event_weight = _profile_metric(profile_payload.get("event_term_weight")) or 1.0
    heat_weight = _profile_metric(profile_payload.get("heat_weight")) or 1.0
    probe_payload = probe if isinstance(probe, Mapping) else {}
    duration = float(probe_payload.get("duration_seconds") or 0.0)
    return (
        -theme_hits,
        -(event_hits * event_weight),
        -material_profile_bonus(labels, profile),
        -(float(getattr(candidate, "heat_score", 0.0) or 0.0) * heat_weight),
        -duration,
        str(getattr(candidate, "video_id", "") or ""),
    )


@dataclass(slots=True)
class DownloadBudget:
    """A run-wide cap on downloads, shared by every download loop.

    Two byte ledgers, because "what we kept" and "what we actually pulled over
    the wire" are different numbers and the run must be bounded by *both*:

    * ``bytes`` (reported as ``delivered_bytes``) counts media that was finally
      **delivered**: a download only lands here once it produced usable media
      (download + probe + validation all passed), so a *failed* attempt frees
      its slot and the loop automatically continues to the next candidate;
    * ``transferred_bytes`` counts **every attempt that actually wrote bytes**,
      whether or not it was delivered.  A corrupt file, a wrong-duration clip
      or a ``short_decode`` rejection still cost real bandwidth, so a
      download-only run cannot keep pulling files it never keeps.  Zero-byte
      failures -- HTTP 4xx/5xx, a ``Content-Length`` oversize rejected before
      the body is read, a probe that fails before any byte is fetched -- wrote
      nothing and are **not** charged; a *cache hit* writes nothing either, so
      it grows only ``bytes`` (the delivered ledger), never this one.

    Consequences:

    * the byte ceiling is enforced on ``max(bytes, transferred_bytes)`` (see
      :meth:`consumed_bytes`), so neither ledger can be silently under-counted;
    * the *delivered* ledger is idempotent per ``video_id``: the same video
      selected in two stages (script then material) occupies one slot and one
      ``bytes`` charge -- see :meth:`select` -- so ``count`` / ``bytes`` never
      overstate what was delivered;
    * a single item over ``max_item_bytes`` is skipped -- never "patched up" by
      fetching a smaller/partial variant, which would bypass the per-item cap;
    * running out of budget stops the run, attributed via ``stopped_by``:
      ``"count"`` (item cap), ``"bytes"`` (the delivered ledger filled the byte
      ceiling) or ``"transferred_bytes"`` (real traffic filled it without a
      delivered file accounting for it).
    """

    max_count: int = 0
    max_bytes: int = 0
    max_item_bytes: int = 0
    count: int = 0
    bytes: int = 0
    transferred_bytes: int = 0
    stopped_by: str | None = None
    selected: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "DownloadBudget | None":
        settings = download_budget_settings(config)
        if not bool(settings.get("enabled", False)):
            return None
        return cls(
            max_count=int(settings.get("max_count") or 0),
            max_bytes=int(settings.get("max_bytes") or 0),
            max_item_bytes=int(settings.get("max_item_bytes") or 0),
        )

    def consumed_bytes(self) -> int:
        """The run's byte cost so far = ``max(delivered, transferred)``.

        Preferring the larger of the two keeps *both* ledgers honest: a cache
        hit grows only ``bytes`` and a rejected download grows only
        ``transferred_bytes``; taking the max means neither is silently
        under-counted when computing what is left of the ceiling.
        """
        return max(self.bytes, self.transferred_bytes)

    def remaining_bytes(self) -> int | None:
        if self.max_bytes <= 0:
            return None
        return max(0, self.max_bytes - self.consumed_bytes())

    def item_cap(self) -> int | None:
        """Per-download byte cap = min(per-item cap, remaining run budget).

        The remaining budget is measured on real traffic (``transferred_bytes``,
        via :meth:`consumed_bytes`), so a candidate that would still fit the
        *delivered* ledger but overflow the *wire* ceiling is refused.
        """
        caps: list[int] = []
        if self.max_item_bytes > 0:
            caps.append(self.max_item_bytes)
        remaining = self.remaining_bytes()
        if remaining is not None:
            caps.append(remaining)
        return min(caps) if caps else None

    def allow(self) -> tuple[bool, str]:
        """May another download start?  Sets ``stopped_by`` when it may not."""
        if self.max_count > 0 and self.count >= self.max_count:
            self.stopped_by = self.stopped_by or "count"
            return False, f"已达下载条数上限 {self.max_count} 条"
        if self.max_bytes > 0 and self.bytes >= self.max_bytes:
            self.stopped_by = self.stopped_by or "bytes"
            return False, f"已达下载总量上限 {self.max_bytes} 字节"
        if self.max_bytes > 0 and self.transferred_bytes >= self.max_bytes:
            # Real traffic reached the ceiling while the delivered ledger did
            # not -- e.g. a download-only run whose files were all rejected.
            # Without this the loop would keep downloading forever.
            self.stopped_by = self.stopped_by or "transferred_bytes"
            return False, (
                f"已达真实传输上限 {self.max_bytes} 字节"
                f"（其中交付 {self.bytes} 字节，已写出 {self.transferred_bytes} 字节）"
            )
        return True, ""

    def mark_transferred(self, size_bytes: int) -> None:
        """Charge a download attempt that actually wrote bytes to the wire ledger.

        Call this right after a successful ``invoke_downloader`` (or the real
        ``materials.download_video`` cache hit).  The value is the number of
        bytes that appeared on disk for this attempt -- see
        :func:`measure_transferred_bytes` -- so a cache hit charges ``0`` while
        a freshly written file the caller later rejects still charges its size.
        """
        self.transferred_bytes += max(0, int(size_bytes or 0))

    def note_oversize(self, cap: int | None) -> str:
        """Classify a ``MediaTooLargeError`` as ``"skip"`` or ``"stop"`` (P1d).

        A single oversized item is **not** the run being exhausted: an item whose
        declared size exceeds the *current* allowance says nothing about the
        candidates behind it.  Treating it as exhaustion (the old behaviour) was
        the direct cause of the "only 2 material sources" bug -- one oversize
        item ``break``-ed the loop and left the ranked candidates behind it
        (24 of 25 in the 9.13 run, incl. 10 real videos) entirely unevaluated.

        The rule is now: while the run still has byte budget left
        (``remaining_bytes() > 0``, or there is no byte ceiling at all) an
        oversize is a plain ``"skip"`` -- drop the item and keep scanning.  Only
        when the byte budget is *fully* spent (``remaining_bytes() == 0``) does an
        oversize mean "nothing can fit any more" -> ``"stop"``.  ``allow()``
        remains the authority for the count / byte ceilings and is called before
        every download, so scanning can neither loop forever nor exceed a cap.

        This is safe precisely because an oversize is cheap: a declared /
        ``Content-Length`` / cached rejection reads **no body** (0 wire bytes --
        see ``materials.download_video``), so "scan but do not download" costs
        nothing.  A *streamed* oversize does read some bytes; the caller charges
        those via :meth:`mark_transferred`, which shrinks ``remaining_bytes`` and
        so still self-terminates.

        This method only *classifies*: it never charges bytes.  ``cap`` is kept
        for call-site compatibility and diagnostics.
        """
        remaining = self.remaining_bytes()
        if remaining is None or remaining > 0:
            return "skip"
        self.stopped_by = self.stopped_by or "bytes"
        return "stop"

    def select(
        self,
        candidate: Candidate,
        size_bytes: int,
        relevance: float = 0.0,
        stage: str = "",
    ) -> None:
        """Record one *delivered* file, idempotent per ``video_id``.

        The same video legitimately enters the budget more than once: a video
        pulled for the script replica is often re-selected as a material source
        (the two stages keep separate on-disk copies).  It is still **one
        delivered file**, so ``count`` and ``bytes`` (the delivered ledger) grow
        only on the *first* delivery of a given ``video_id``.  A later selection
        of the same id is recorded as a *stage tag* (``stage`` / ``stages``) on
        the existing entry instead of being double-counted, so ``selected``
        holds exactly one row per delivered video.

        This idempotence is deliberately **not** extended to
        ``transferred_bytes`` (charged by :meth:`mark_transferred`): a genuine
        re-fetch really did put bytes on the wire, and hiding them would let a
        run exceed the wire ceiling -- the exact failure ``transferred_bytes``
        exists to prevent.  The two ledgers therefore answer different
        questions: ``bytes`` = distinct delivered media, ``transferred_bytes`` =
        real traffic.

        ``stage`` ("script" / "material") is optional and purely additive: an
        empty stage keeps the entry byte-for-byte identical to the pre-change
        format (download-only runs), while a tagged entry lets the readme show
        which stage(s) selected the video.
        """
        video_id = candidate.video_id
        for entry in self.selected:
            if entry["video_id"] == video_id:
                # Same video, another stage: merge the tag, never re-charge.
                if stage and stage not in entry.get("stages", []):
                    entry.setdefault("stages", []).append(stage)
                return
        size = max(0, int(size_bytes or 0))
        self.count += 1
        self.bytes += size
        entry: dict[str, Any] = {
            "video_id": video_id,
            "title": candidate.title,
            "author": candidate.author,
            "heat_score": round(float(candidate.heat_score or 0.0), 6),
            "relevance_score": round(float(relevance or 0.0), 6),
            "size_bytes": size,
        }
        if stage:
            # New keys only -- an untagged (download-only) entry is unchanged.
            entry["stage"] = stage
            entry["stages"] = [stage]
        self.selected.append(entry)

    def skip(self, candidate: Candidate, stage: str, reason: str, relevance: float = 0.0) -> None:
        self.skipped.append({
            "video_id": candidate.video_id,
            "stage": stage,
            "reason": reason,
            "title": candidate.title,
            "author": candidate.author,
            "heat_score": round(float(candidate.heat_score or 0.0), 6),
            "relevance_score": round(float(relevance or 0.0), 6),
        })

    def snapshot(self, ranking_note: str = "") -> dict[str, Any]:
        return {
            "enabled": True,
            "limits": {
                "max_count": self.max_count,
                "max_bytes": self.max_bytes,
                "max_item_bytes": self.max_item_bytes,
            },
            # ``bytes`` is kept as a backwards-compatible alias of
            # ``delivered_bytes`` (existing readers/tests); the two named keys
            # make the delivered-vs-transferred distinction explicit.
            "used": {
                "count": self.count,
                "bytes": self.bytes,
                "delivered_bytes": self.bytes,
                "transferred_bytes": self.transferred_bytes,
            },
            "ranking": {
                "order": ["relevance", "heat_score", "video_id"],
                "note": ranking_note or RANKING_LIMITATION_NOTE,
            },
            "stopped_by": self.stopped_by,
            "selected": list(self.selected),
            "skipped": list(self.skipped),
        }


def invoke_downloader(
    downloader,
    url: str,
    path: Path,
    config: dict[str, Any],
    cap: int | None,
    referer: str | None = None,
) -> None:
    """Call ``downloader``, passing ``max_bytes``/``referer`` only when supported.

    The real :func:`materials.download_video` enforces the cap in-flight and
    accepts a ``referer`` override; the offline fakes across the test suite keep a
    3-arg signature, so each kwarg is only added when the callable actually
    declares it.  ``referer`` is the cross-platform hook: ``None`` (the default,
    and everything the legacy Douyin path passes) makes the downloader fall back
    to its historic ``https://www.douyin.com/`` header, byte for byte.
    """
    if cap is None and referer is None:
        # Fast path: no optional kwarg requested -> never probe the signature, so
        # a non-inspectable callable keeps its exact historical invocation.
        downloader(url, path, config)
        return
    try:
        parameters = inspect.signature(downloader).parameters
    except (TypeError, ValueError):
        parameters = None
    kwargs: dict[str, Any] = {}
    if cap is not None and parameters is not None and "max_bytes" in parameters:
        kwargs["max_bytes"] = cap
    if referer is not None and parameters is not None and "referer" in parameters:
        kwargs["referer"] = referer
    downloader(url, path, config, **kwargs)


def resolve_download_target(
    resolver: "MediaResolver | None",
    candidate: Candidate,
    media_urls: dict[str, str],
) -> "tuple[str, str | None] | None":
    """Resolve ``candidate`` to ``(url, referer)`` for one download attempt.

    Two contracts coexist here, and the split is deliberate:

    * **Multi-source** (``resolver`` is not ``None``): the resolver dispatches to
      the candidate's own adapter.  It returns ``None`` when the candidate has no
      usable address -- a *normal* miss the caller records as one download failure
      and moves on.  A :class:`~douyin_intelligence.sources.base.MediaResolutionError`
      is a *source-level* failure and is **not** swallowed here: it propagates so
      the caller can record it distinctly (never as "no address"), matching the
      adapter contract.
    * **Legacy** (``resolver`` is ``None``): the historic ``media_urls`` lookup,
      returned verbatim -- including an empty string.  The pre-resolver chain
      handed that empty string straight to the downloader (which then failed and
      was counted as one download failure), so returning it unchanged is what
      keeps the default Douyin path byte-for-byte equivalent.
    """
    if resolver is None:
        return media_urls.get(candidate.video_id, ""), None
    target = resolver.resolve_target(candidate)
    if target is None:
        return None
    return target.url, target.referer


def file_size(path: "Path | str") -> int:
    """``st_size`` of ``path`` in bytes, or ``0`` when it is missing/unreadable."""
    try:
        return int(Path(path).stat().st_size)
    except OSError:
        return 0


#: Backwards-compatible private alias (older call sites / tests use this name).
_safe_size = file_size


def measure_transferred_bytes(path: "Path | str", pre_size: int) -> int:
    """Bytes a just-finished download attempt wrote to ``path``.

    Callers capture ``pre_size = file_size(path)`` *before* invoking the
    downloader, then pass it here afterwards.  A fresh download leaves a larger
    file behind and charges the growth; a **cache hit** leaves the file
    unchanged and therefore charges ``0`` (no new traffic) while still counting
    against the delivered ledger.  A file that was downloaded but then rejected
    by validation/duration still grew, so its bytes *are* charged -- that is the
    whole point of tracking real traffic separately from delivered bytes.
    """
    current = file_size(path)
    return current - pre_size if current > pre_size else 0


def compute_visual_metrics(video: Path, duration: float, ocr_result: dict[str, Any] | None, temp_dir: Path, config: dict[str, Any]) -> VisualMetrics:
    """Motion proxy (adjacent-frame gray delta) + OCR frame coverage for one video.

    Two independent, dimensionally-correct signals feed :func:`visual_verdict`:

    * **motion** -- ``motion_frame_ratio`` = fraction of sampled adjacent-frame
      gray-delta pairs that reach ``motion_delta_threshold``; the clip has enough
      motion when that fraction reaches ``min_motion_frame_ratio``.
    * **text** -- ``ocr_text_frame_ratio`` = fraction of OCR'd frames that
      carried on-screen text (``frames_with_text`` / ``frames_scanned``), bounded
      by ``max_ocr_coverage``.

    ``visual_ok = motion_ok or ocr_ok``.  When the OCR frame count is unknown
    (an older cache without ``frames_with_text``) the text signal is reported as
    *unmeasurable* instead of being fabricated into a ratio.
    """
    from .media_tools import resolve_media_tool

    face = face_settings(config)
    settings = material_settings(config)
    interval = max(1, int(face.get("sampling_interval_seconds") or 1))
    max_frames = max(1, int(face.get("max_frames") or 120))
    width = max(320, int(face.get("frame_width") or 960))
    # ``is not None`` (not ``or``) so a deliberate 0.0 stays 0.0.
    delta_threshold = float(
        settings["motion_delta_threshold"]
        if settings.get("motion_delta_threshold") is not None
        else DEFAULT_MOTION_DELTA_THRESHOLD
    )
    min_motion_ratio = float(
        settings["min_motion_frame_ratio"]
        if settings.get("min_motion_frame_ratio") is not None
        else DEFAULT_MIN_MOTION_FRAME_RATIO
    )
    max_ocr = float(
        settings["max_ocr_coverage"]
        if settings.get("max_ocr_coverage") is not None
        else DEFAULT_MAX_OCR_COVERAGE
    )

    ocr = ocr_result or {}
    # ``frames_scanned`` (frames actually OCR'd) is the honest denominator; the
    # older ``sampled_frames`` guess is only a fallback for cached results that
    # predate the new field.
    frames_scanned = int(ocr.get("frames_scanned") or ocr.get("sampled_frames") or 0)
    raw_with_text = ocr.get("frames_with_text")
    frames_with_text = int(raw_with_text) if raw_with_text is not None else None

    frames_dir = Path(temp_dir) / "motion-frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(frames_dir / "frame-%04d.jpg")
    ffmpeg = resolve_media_tool(config, "ffmpeg")
    command = [
        ffmpeg, "-y", "-v", "error", "-i", str(video),
        "-vf", f"fps=1/{interval},scale='min({width},iw)':-2",
        "-frames:v", str(max_frames), pattern,
    ]
    completed = _run_media_process(command)
    frame_files = sorted(frames_dir.glob("frame-*.jpg"))
    diffs: list[float] = []
    try:
        if completed.returncode == 0:
            import cv2
            import numpy as np
            previous = None
            for frame_file in frame_files:
                gray = imread_unicode(frame_file, cv2.IMREAD_GRAYSCALE)
                if gray is None:
                    continue
                if previous is not None and previous.shape == gray.shape:
                    diffs.append(float(np.mean(cv2.absdiff(gray, previous))) / 255.0)
                previous = gray
    finally:
        for frame_file in frame_files:
            frame_file.unlink(missing_ok=True)
        try:
            frames_dir.rmdir()
        except OSError:
            pass

    motion_ratio = sum(1 for delta in diffs if delta >= delta_threshold) / len(diffs) if diffs else 0.0
    verdict = visual_verdict(
        motion_frame_ratio=motion_ratio,
        frames_with_text=frames_with_text,
        frames_scanned=frames_scanned,
        motion_delta_threshold=delta_threshold,
        min_motion_frame_ratio=min_motion_ratio,
        max_ocr_coverage=max_ocr,
    )
    return VisualMetrics(
        sampled_frames=len(diffs) + 1 if diffs else len(frame_files),
        motion_frame_ratio=round(motion_ratio, 6),
        ocr_text_frame_ratio=verdict["ocr_text_frame_ratio"],
        visual_ok=verdict["visual_ok"],
        motion_delta_threshold=round(delta_threshold, 6),
        min_motion_frame_ratio=round(min_motion_ratio, 6),
        max_ocr_coverage=round(max_ocr, 6),
        ocr_measurable=verdict["ocr_measurable"],
        motion_ok=verdict["motion_ok"],
        ocr_ok=verdict["ocr_ok"],
        reject_reason=verdict["reason"],
    )


def speech_rate(transcript: dict[str, Any] | None, duration: float) -> float:
    if not transcript or transcript.get("status") != "success":
        return 0.0
    chars = len(re.sub(r"\s+", "", str(transcript.get("text") or "")))
    return round(chars / max(1.0, float(duration)), 6)


def _downloader(deps: "ReplicationDeps | None"):
    provided = getattr(deps, "downloader", None) if deps is not None else None
    if provided is not None:
        return provided
    from .materials import download_video
    return download_video


def _prober(deps: "ReplicationDeps | None"):
    provided = getattr(deps, "prober", None) if deps is not None else None
    if provided is not None:
        return provided
    from .materials import probe_video
    return probe_video


def _validator(deps: "ReplicationDeps | None"):
    """The injectable whole-validation effect (``None`` -> real ffprobe+ffmpeg)."""
    return getattr(deps, "validator", None) if deps is not None else None


def _transcriber(config: dict[str, Any], deps: "ReplicationDeps | None"):
    provided = getattr(deps, "transcriber", None) if deps is not None else None
    if provided is not None:
        return provided
    from .media_processing import CheckpointTranscriber
    return CheckpointTranscriber(config)


def _ocr(config: dict[str, Any], deps: "ReplicationDeps | None"):
    provided = getattr(deps, "ocr", None) if deps is not None else None
    if provided is not None:
        return provided
    from .media_processing import KeyframeOCR
    return KeyframeOCR(config)


def _face_detector(config: dict[str, Any], deps: "ReplicationDeps | None"):
    provided = getattr(deps, "face_detector", None) if deps is not None else None
    if provided is not None:
        return provided
    from .face_metrics import FaceDetector
    return FaceDetector(config)


def select_script_replica(
    config: dict[str, Any],
    candidates: list[Candidate],
    *,
    media_urls: dict[str, str] | None = None,
    deps: "ReplicationDeps | None" = None,
    clock: Any = None,
    budget: "DownloadBudget | None" = None,
    relevance: dict[str, float] | None = None,
    validation_store: list[dict[str, Any]] | None = None,
    resolver: "MediaResolver | None" = None,
) -> dict[str, Any]:
    """Pick exactly one script replica, or report ``not_found`` with reasons.

    ``resolver`` is the optional multi-source download dispatch (see
    :class:`~douyin_intelligence.sources.base.CompositeMediaResolver`).  ``None``
    -- the default -- keeps the legacy ``media_urls`` lookup and the historic
    Douyin referer, byte for byte; a resolver resolves each candidate lazily
    against its own source adapter at download time.

    Every rejected candidate is recorded as a structured
    ``{"video_id", "stage", "reason"}`` entry (``stage`` is one of ``pool``,
    ``duration``, ``validation`` or ``speech``) so a ``not_found`` outcome is
    fully attributable downstream instead of being a silent dead end.

    ``validation_store`` collects the per-file download-validation records for
    the run-level ``validation.json``.
    """
    from .replication_theme import project_path

    media_urls = media_urls or {}
    settings = material_replication_settings(config)
    media_root = project_path(config, settings.get("media_root") or "data/media/material-replication")
    cache_root = project_path(config, settings.get("cache_root") or "data/cache/material-replication")
    temp_root = project_path(config, settings.get("temp_root") or "data/temp/material-replication")
    pool = script_candidate_pool(candidates, config)
    if budget is not None:
        pool = ranked_candidates(pool, relevance)
    unmet: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    asr_attempted = 0
    validation_on = validation_enabled(config)
    validation_passed = 0
    validation_rejected = 0

    def _stage(conclusion: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "candidate_pool": len(pool),
            "asr_attempted": asr_attempted,
            "rejected": len(unmet),
            "errors": len(errors),
            "conclusion": conclusion,
        }
        if validation_on:
            payload["stage_validation_passed"] = validation_passed
            payload["stage_validation_rejected"] = validation_rejected
        return payload

    if not pool:
        unmet.append({"video_id": "", "stage": "pool", "reason": "无候选满足热度与时长门槛"})
        return {
            "status": "not_found", "candidate": None, "video_path": "", "probe": {}, "transcript": {},
            "unmet": unmet, "errors": errors, "downloaded": 0, "stage": _stage("not_found"),
        }

    downloader = _downloader(deps)
    prober = _prober(deps)
    transcriber = _transcriber(config, deps)
    downloaded = 0
    min_seconds = float(script_settings(config).get("min_seconds") or 30)
    max_seconds = float(script_settings(config).get("max_seconds") or 300)
    # Per-source window overrides, resolved once for the whole loop.
    window_overrides = source_duration_windows(config)
    # Shared single video cache root (see ``REPLICATION_VIDEO_SUBDIR``): the
    # script replica and the material sources reuse the same ``<id>.mp4`` so a
    # video common to both stages is fetched once, not twice.
    video_root = media_root / REPLICATION_VIDEO_SUBDIR
    for candidate in pool:
        usable, not_video_reason = is_video_candidate(candidate)
        if not usable:
            unmet.append({"video_id": candidate.video_id, "stage": "not_video", "reason": not_video_reason})
            continue
        if budget is not None:
            allowed, budget_reason = budget.allow()
            if not allowed:
                unmet.append({"video_id": candidate.video_id, "stage": "budget", "reason": budget_reason})
                break
        rel = (relevance or {}).get(candidate.video_id, 0.0)
        try:
            resolved = resolve_download_target(resolver, candidate, media_urls)
        except MediaResolutionError as exc:
            # Source-level / transport failure (retries exhausted, API error,
            # network): recorded apart from a normal "no address" miss so the
            # operator can tell "the source broke" from "this clip had nothing".
            errors.append({
                "video_id": candidate.video_id,
                "stage": "resolve",
                "error": str(exc)[:300],
            })
            continue
        except Exception as exc:  # an adapter blowing up must not abort the run
            errors.append({
                "video_id": candidate.video_id,
                "stage": "resolve",
                "error": f"{type(exc).__name__}: {str(exc)[:280]}",
            })
            continue
        if resolved is None:
            # No usable address (normal miss): one download failure, no alert and
            # no retry -- the adapter contract's "return '' / None" half.
            unmet.append({
                "video_id": candidate.video_id,
                "stage": "no_media_url",
                "reason": "未解析到可用下载地址",
            })
            continue
        download_url, download_referer = resolved
        try:
            video_path = video_root / f"{candidate.video_id}.mp4"
            pre_size = file_size(video_path)
            try:
                invoke_downloader(downloader, download_url, video_path, config, budget.item_cap() if budget is not None else None, download_referer)
            except MediaTooLargeError as exc:
                if budget is None:
                    raise
                # A *streamed* oversize already pulled real bytes off the wire
                # before aborting (``source == "streamed"``, ``bytes_read > 0``);
                # charge them so the traffic ledger is honest.  A *declared*
                # oversize read no body (``bytes_read == 0``) and charges nothing.
                budget.mark_transferred(int(getattr(exc, "bytes_read", 0) or 0))
                action = budget.note_oversize(budget.item_cap())
                budget.skip(candidate, "budget_item" if action == "skip" else "budget_bytes", str(exc)[:160], relevance=rel)
                if action == "stop":
                    unmet.append({"video_id": candidate.video_id, "stage": "budget", "reason": str(exc)[:160]})
                    break
                unmet.append({"video_id": candidate.video_id, "stage": "too_large", "reason": f"体积超限：{str(exc)[:120]}"})
                continue
            if budget is not None:
                # Charge real traffic *before* any later rejection: a file that
                # is downloaded and then dropped still cost bandwidth.
                budget.mark_transferred(measure_transferred_bytes(video_path, pre_size))
            downloaded += 1
            try:
                probe = prober(video_path, config)
            except Exception as exc:
                unmet.append({"video_id": candidate.video_id, "stage": "invalid_media", "reason": f"媒体无效：{str(exc)[:120]}"})
                continue
            media_ok, media_reason = validate_probe(probe)
            if not media_ok:
                unmet.append({"video_id": candidate.video_id, "stage": "invalid_media", "reason": f"媒体无效：{media_reason}"})
                continue
            # Download-time validation sits *before* ``budget.select`` so a
            # corrupt file can never hold a slot or a byte of the run budget.
            # A rejection simply falls through to the next candidate.
            validation_record = validate_candidate(
                video_path, config, candidate=candidate, probe=probe, prober=prober,
                validator=_validator(deps),
            )
            if validation_record is not None:
                record_validation(validation_store, validation_record, stage="script")
                if not validation_record.get("passed"):
                    validation_rejected += 1
                    unmet.append({
                        "video_id": candidate.video_id,
                        "stage": "validation",
                        "reason": validation_reason(validation_record),
                    })
                    continue
                validation_passed += 1
            # Post-download half of the duration gate: when the metadata carried
            # no duration the pre-download window could not judge this file, so
            # the measured duration is judged against the same window now.
            window_reject, window_reason = measured_duration_window_reject(
                float(probe.get("duration_seconds") or 0), config,
                metadata_duration=float(getattr(candidate, "duration_seconds", 0.0) or 0.0),
                source=str(getattr(candidate, "source", "") or ""),
            )
            if window_reject:
                unmet.append({"video_id": candidate.video_id, "stage": "duration_post", "reason": window_reason})
                continue
            duration = float(probe.get("duration_seconds") or 0)
            script_min, script_max = effective_duration_window(
                window_overrides, candidate, min_seconds, max_seconds
            )
            if not script_min <= duration <= script_max:
                unmet.append({
                    "video_id": candidate.video_id,
                    "stage": "duration",
                    "reason": f"时长 {duration:.0f}s 不在 {script_min:.0f}~{script_max:.0f}s",
                })
                continue
            # P1c: ``select`` sits *after* the duration gate so a file rejected on
            # its measured duration never holds a slot or a byte of the run
            # budget.  (The download-time validation above already sits before
            # ``select`` for the same reason; only ``select`` moves, not it.)
            if budget is not None:
                budget.select(candidate, _safe_size(video_path), relevance=rel, stage="script")
            cache_dir = cache_root / "script" / candidate.video_id
            temp_dir = temp_root / "script" / candidate.video_id
            asr_attempted += 1
            transcript = transcriber.run(video_path, cache_dir, temp_dir)
            ok, reason = evaluate_script_transcript(transcript, duration, config)
            if not ok:
                unmet.append({"video_id": candidate.video_id, "stage": "speech", "reason": reason})
                continue
            return {
                "status": "found",
                "candidate": candidate,
                "video_path": video_path,
                "probe": probe,
                "transcript": transcript,
                "unmet": unmet,
                "errors": errors,
                "downloaded": downloaded,
                "stage_validation_passed": validation_passed,
                "stage_validation_rejected": validation_rejected,
                "stage": _stage("found"),
            }
        except Exception as exc:
            errors.append({"video_id": candidate.video_id, "stage": "download_or_probe", "error": str(exc)[:300]})
    if not unmet and not errors:
        unmet.append({"video_id": "", "stage": "transcript", "reason": "全部候选转写未达标"})
    return {
        "status": "not_found", "candidate": None, "video_path": "", "probe": {}, "transcript": {},
        "unmet": unmet, "errors": errors, "downloaded": downloaded,
        "stage_validation_passed": validation_passed, "stage_validation_rejected": validation_rejected,
        "stage": _stage("not_found"),
    }


def _material_summary(
    pool_size: int, face_checked: int, selected_count: int, min_count: int, unmet: list[dict[str, Any]]
) -> str:
    """Human-readable one-line reason why the material replica set is short."""
    details = "；".join(
        f"{entry.get('video_id') or '候选池'}：{entry.get('reason') or ''}".rstrip("：")
        for entry in unmet
    ) or "无候选进入素材筛选"
    headline = (
        f"仅选出素材复刻视频 {selected_count} 条，不足最小 {min_count} 条"
        if selected_count
        else "未选出素材复刻视频"
    )
    return f"{headline}（候选池 {pool_size} 条，进入人脸检测 {face_checked} 条）：{details}"


def _material_byte_floor_warning(
    selected_count: int,
    target: int,
    delivered_bytes: int,
    min_delivered_bytes: int,
    unmet: list[dict[str, Any]],
) -> str:
    """Actionable one-line reason why the delivered source bytes fell short.

    The delivered-bytes floor exists to keep a period's *material volume* inside
    a band -- the "only 24.86 MiB across 4 sources" complaint is exactly a floor
    miss.  A floor miss can happen while ``min_count`` is still satisfied (a few
    short clips), so it needs its own wording rather than the count shortfall:
    spell out the gap and the knob that closes it, then the per-candidate detail.
    """
    shortfall = max(0, min_delivered_bytes - delivered_bytes)
    details = "；".join(
        f"{entry.get('video_id') or '候选池'}：{entry.get('reason') or ''}".rstrip("：")
        for entry in unmet
    ) or "无候选进入素材筛选"
    return (
        f"交付源片体积 {delivered_bytes / 1048576:.1f} MiB 低于下限 "
        f"{min_delivered_bytes / 1048576:.1f} MiB（缺口 {shortfall / 1048576:.1f} MiB，"
        f"已选 {selected_count}/{target} 条）：可提高 material_replica.max_seconds 或补充候选，"
        f"或下调 material_replica.min_delivered_bytes；候选落选明细：{details}"
    )


def select_material_replicas(
    config: dict[str, Any],
    candidates: list[Candidate],
    *,
    media_urls: dict[str, str] | None = None,
    deps: "ReplicationDeps | None" = None,
    clock: Any = None,
    budget: "DownloadBudget | None" = None,
    relevance: dict[str, float] | None = None,
    validation_store: list[dict[str, Any]] | None = None,
    theme: str | None = None,
    resolver: "MediaResolver | None" = None,
) -> dict[str, Any]:
    """Select 2~4 low-speech, deduplicated material videos.

    Every dropped candidate is recorded in ``unmet`` as a structured
    ``{"video_id", "stage", "reason"}`` entry so an empty/insufficient material
    set is fully attributable downstream.  ``stage`` is one of ``pool``,
    ``relevance``, ``not_video``, ``cross_run_duplicate``, ``stale``,
    ``duration_pre``, ``author_duplicate``, ``invalid_media``, ``validation``,
    ``duration``, ``visual``, ``speech`` or ``quota``.  Face class is
    **descriptive only** (2026-09-18): it is computed and carried on every
    selected row but never admits or refuses a candidate.

    ``theme`` enables the **relevance gate** (opt-in, see below): it must be the
    same theme the pool was collected for.  ``None`` -- the pre-gate behaviour --
    leaves the download chain untouched.

    ``validation_store`` collects the per-file download-validation records for
    the run-level ``validation.json``.

    Two further gates are **off unless their key is present**, and both are
    judged before a single byte is downloaded:

    * ``material_replica.max_age_days`` (absent or ``0`` == off) refuses a clip
      whose ``published_at`` is older than the window (``stage="stale"``) and
      reports the pool's before/after age medians as ``freshness`` /
      ``stage["material_freshness"]``, because trimming the tail does not by
      itself make the *median* clip younger;
    * ``jobs.material_replication.dedup_across_runs`` (absent == off) refuses a
      clip an earlier period already delivered (``stage="cross_run_duplicate"``),
      reading ``<cache_root>/delivered_index.json`` -- see
      :mod:`douyin_intelligence.replication_dedup` for the file contract.

    Two *different* duration windows guard this chain and they must never be
    conflated:

    * ``prefilter.min_seconds/max_seconds`` -- a **pre-download** window applied
      to the whole replication run (``prefilter_candidates``), whose post-download
      half is :func:`measured_duration_window_reject`;
    * ``material_replica.min_seconds/max_seconds`` -- the **material clip** window,
      historically applied *only after* the download, against the measured length.

    ``measured_duration_window_reject`` is explicitly a no-op once the metadata
    already carried a duration (the prefilter already judged it), and it measures
    the *prefilter* window, not the material one.  So a candidate the prefilter
    lets through (e.g. 202 s < 300 s) used to be **downloaded** and only then
    refused by the material window (202 s > 180 s): the file landed on disk and
    its bytes were burned for nothing.  The ``duration_pre`` gate below moves the
    **material** window *ahead of the download* so such a candidate is dropped
    without any traffic; candidates whose metadata carries no duration keep the
    **Relevance gate** (``jobs.material_replication.relevance_gate``, optional
    and off unless ``enabled`` is true **and** ``theme`` is given).  Relevance
    used to be an ordering key only, so a candidate whose title never mentions
    the subject was still downloaded -- merely last.  The 9.14 Microduck period
    shipped zero on-topic material from a pool whose titles contained
    ``microduck`` 52% of the time, because nothing ever *refused* an unrelated
    clip.  With the gate on, a candidate whose title hits fewer than
    ``min_subject_hits`` (default 1) of the theme's subject terms
    (``replication_theme.subject_terms``) is recorded as
    ``stage="relevance"`` and dropped **before any download**, so it costs
    neither traffic nor a slot.  (``relevance_gate.min_hit_ratio`` is a different,
    *pool-level* knob -- see :func:`relevance_report`.)  A theme that yields no
    subject term disables the
    gate rather than rejecting the whole pool, and that no-op is reported instead
    of hidden: a ``warnings`` entry plus ``stage["relevance_gate"] =
    "inactive:no_subject_terms"`` (``"active"`` when it did bite).  With the key
    absent -- every config written before this feature -- nothing changes: the
    gate is off and the chain is byte-for-byte the pre-gate one.

    ``resolver`` is the optional multi-source download dispatch: when given, the
    download URL and the ``Referer`` header are resolved per candidate through the
    candidate's own source adapter (so a Bilibili clip is fetched with Bilibili's
    referer); ``None`` -- the default -- keeps the legacy ``media_urls`` lookup and
    the historic Douyin referer, byte for byte.  A ``MediaResolutionError`` is a
    source-level failure and is recorded separately from a normal "no address"
    miss.
    """
    from .replication_theme import project_path

    media_urls = media_urls or {}
    settings = material_replication_settings(config)
    material = material_settings(config)
    media_root = project_path(config, settings.get("media_root") or "data/media/material-replication")
    cache_root = project_path(config, settings.get("cache_root") or "data/cache/material-replication")
    temp_root = project_path(config, settings.get("temp_root") or "data/temp/material-replication")
    min_count = int(material.get("min_count") or 2)
    target = int(material.get("target_count") or 4)
    min_seconds = float(material.get("min_seconds") or 15)
    max_seconds = float(material.get("max_seconds") or 180)
    # Per-source window overrides (e.g. Bilibili's long-form clips), resolved once.
    window_overrides = source_duration_windows(config)
    max_speech = float(material.get("max_speech_rate") or 1.2)
    max_per_author = int(material.get("max_per_author") or 1)
    # --- Delivered-bytes quota (optional; absent keys == byte-identical run) ---
    # ``min_delivered_bytes`` is a *floor* on the summed size of the selected
    # source files: the loop keeps scanning past ``target`` until the sum reaches
    # it, so a period that would otherwise ship a handful of tiny clips keeps
    # looking for longer / more candidates instead of stopping at ``target``.
    # ``0`` (the absent default) means "no floor" -- the loop then breaks purely
    # on ``target``, exactly as before.  ``max_delivered_bytes`` is a *ceiling*:
    # a candidate that would push the sum past it is skipped (never appended), so
    # the delivered set can never overshoot; ``0`` means "no ceiling".
    # ``max_selected_count`` is an optional hard cap on how many sources the
    # floor scan may collect; ``0`` means "no extra cap".  The quota counts only
    # the *selected source files* (``_safe_size`` of each ``video_path``), never
    # the 8 s clip slices, and never touches ``DownloadBudget``.
    min_delivered_bytes = int(material.get("min_delivered_bytes") or 0)
    max_delivered_bytes = int(material.get("max_delivered_bytes") or 0)
    max_selected_count = int(material.get("max_selected_count") or 0)
    # --- Freshness window (optional; absent/0 == byte-identical run) ----------
    # ``max_age_days`` is resolved once, here, so "is the gate on at all" is
    # decided in a single place instead of per candidate.
    max_age_days = material_max_age_days(material)

    pool, median = material_candidate_pool(candidates, config)
    if budget is not None:
        pool = ranked_candidates(pool, relevance)
    pool_ids = {candidate.video_id for candidate in pool}
    # --- Freshness pre-pass (runs only when the gate is on) ------------------
    # Ages are computed once for the whole pool so the loop can look them up and
    # the manifest can report *before/after* medians taken from the same numbers.
    # ``freshness["rejected"]`` is therefore a **pool-level** count, while the
    # ``rejected_stale`` counter below counts the rows the loop actually refused:
    # a stale row a cheaper gate already dropped is not counted twice.
    freshness_on = max_age_days > 0
    ages: dict[str, float | None] = {}
    freshness: dict[str, Any] | None = None
    if freshness_on:
        zone = ZoneInfo(str(config.get("timezone") or "Asia/Shanghai"))
        ages = {
            candidate.video_id: published_age_days(candidate, _freshness_now(config, zone), zone)
            for candidate in pool
        }
        ages_before = [age for age in ages.values() if age is not None]
        ages_after = [age for age in ages_before if age <= max_age_days]
        freshness = freshness_block(max_age_days, ages_before, ages_after, len(pool) - len(ages_before))

    # --- Cross-run de-duplication index (loaded only when switched on) -------
    # Read once, before the loop, so a single period cannot both skip a clip and
    # then re-record it; ``replication_dedup.remember_delivered`` writes the file
    # back once delivery has finished.
    dedup_on = dedup_enabled(config)
    delivered_index = load_delivered_index(delivered_index_path(config)) if dedup_on else {}
    # --- Relevance gate (optional; absent key == byte-identical run) ----------
    # Resolved *before* the loop so the subject vocabulary (and the "is this pool
    # on-topic at all" decision) is computed once, not per candidate.  A theme
    # that yields no subject term cannot be gated against, so the gate stays off
    # instead of rejecting every candidate.
    gate_cfg = material_replication_settings(config).get("relevance_gate") or {}
    gate_requested = bool(gate_cfg.get("enabled")) and bool(theme)
    gate_subject_terms: list[str] = []
    if gate_requested:
        gate_subject_terms = list(
            relevance_report(candidates, str(theme), None, config=config)["subject_terms"]
        )
    gate_enabled = bool(gate_subject_terms)
    min_subject_hits = max(1, int(gate_cfg.get("min_subject_hits") or 1))
    downloader = _downloader(deps)
    prober = _prober(deps)
    ocr_runner = _ocr(config, deps)
    face_runner = _face_detector(config, deps)
    transcriber = _transcriber(config, deps)

    selected: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    warnings: list[str] = []
    unmet: list[dict[str, Any]] = []
    author_counts: dict[str, int] = {}
    downloaded = 0
    delivered_bytes = 0
    face_checked = 0
    face_errors = 0
    rejected_duration = 0
    invalid_media = 0
    rejected_pool = 0
    rejected_relevance = 0
    rejected_author_duplicate = 0
    rejected_visual = 0
    rejected_speech = 0
    rejected_not_video = 0
    rejected_duration_post = 0
    rejected_stale = 0
    rejected_cross_run = 0
    validation_on = validation_enabled(config)
    validation_passed = 0
    validation_rejected = 0
    if gate_requested and not gate_enabled:
        # The operator asked for the gate and it cannot judge anything (the theme
        # resolved to no subject term).  Doing nothing *silently* is exactly the
        # false-negative mode this round exists to kill -- "no warning, wrong
        # content" -- so say so in ``warnings`` and in the ``stage`` audit.
        warnings.append(f"主题「{theme}」未解析出主体词，相关性闸门未生效")

    # Candidates below the heat median never enter the loop; record them so the
    # pool stage is attributable too, instead of vanishing silently.
    for candidate in candidates:
        if candidate.video_id in pool_ids:
            continue
        rejected_pool += 1
        unmet.append({
            "video_id": candidate.video_id,
            "stage": "pool",
            "reason": f"热度 {candidate.heat_score:.3f} 低于池中位数 {median:.3f}",
            "heat_score": round(float(candidate.heat_score), 6),
        })

    # Same shared video cache root as the script chain (see
    # ``REPLICATION_VIDEO_SUBDIR``): a video pulled for the script replica is
    # found here as a cache hit instead of being downloaded a second time.
    video_root = media_root / REPLICATION_VIDEO_SUBDIR
    for index, candidate in enumerate(pool):
        # Stop only when *both* the target count and the byte floor are met, so a
        # short/small pool keeps scanning for more volume.  With ``min_delivered_
        # bytes`` absent (0) the floor test is trivially true and this reduces to
        # the original "break once ``target`` is reached" -- byte-identical.
        reached_target = len(selected) >= target
        reached_floor = delivered_bytes >= min_delivered_bytes
        reached_cap = max_selected_count > 0 and len(selected) >= max_selected_count
        if reached_cap or (reached_target and reached_floor):
            if reached_cap and not (reached_target and reached_floor):
                reason = f"已达选择上限 {max_selected_count} 条，未评估"
            elif min_delivered_bytes > 0:
                reason = (
                    f"已达目标 {target} 条且交付源片体积 {delivered_bytes} 字节 "
                    f"≥ 下限 {min_delivered_bytes} 字节，未评估"
                )
            else:
                reason = f"已达目标 {target} 条，未评估"
            for leftover in pool[index:]:
                unmet.append({"video_id": leftover.video_id, "stage": "quota", "reason": reason})
            break
        usable, not_video_reason = is_video_candidate(candidate)
        if not usable:
            rejected_not_video += 1
            unmet.append({
                "video_id": candidate.video_id,
                "stage": "not_video",
                "reason": not_video_reason,
                "aweme_type": str(getattr(candidate, "aweme_type", "") or ""),
            })
            continue
        # --- Cross-run de-duplication (optional; switch absent == no-op) ------
        # Judged before the theme/author gates on purpose: "we already shipped
        # this clip" is a fact about the candidate itself, and letting it be
        # recorded as a *relevance* rejection would make the theme gate look
        # blunter than it actually is.
        duplicate = cross_run_duplicate_reason(config, delivered_index, candidate)
        if duplicate:
            rejected_cross_run += 1
            unmet.append({
                "video_id": candidate.video_id,
                "stage": "cross_run_duplicate",
                "reason": duplicate,
            })
            continue
        # --- Freshness window (optional; absent/0 == no-op) -------------------
        # An *undated* candidate is deliberately let through: a gap in
        # ``published_at`` is a hole in the pool, not evidence of staleness.
        if freshness_on:
            age = ages.get(candidate.video_id)
            if age is not None and age > max_age_days:
                rejected_stale += 1
                unmet.append({
                    "video_id": candidate.video_id,
                    "stage": "stale",
                    "reason": (
                        f"发布时间 {str(candidate.published_at)[:10]} 距今 {age:.1f} 天，"
                        f"超出时效窗口 {max_age_days} 天（下载前判定，未消耗流量）"
                    ),
                    "published_at": candidate.published_at,
                    "age_days": round(age, 3),
                })
                continue
        if gate_enabled:
            subject_hits = subject_hit_count(candidate, gate_subject_terms)
            if subject_hits < min_subject_hits:
                rejected_relevance += 1
                unmet.append({
                    "video_id": candidate.video_id,
                    "stage": "relevance",
                    "reason": (
                        f"标题仅命中主体词 {subject_hits} 个（门槛 {min_subject_hits}，"
                        f"下载前判定，未消耗流量）；主体词：{'、'.join(gate_subject_terms)}"
                    ),
                    "subject_hits": subject_hits,
                    "subject_terms": list(gate_subject_terms),
                })
                continue
        if author_counts.get(candidate.author, 0) >= max_per_author:
            rejected_author_duplicate += 1
            unmet.append({
                "video_id": candidate.video_id,
                "stage": "author_duplicate",
                "reason": f"作者 {candidate.author or '未知'} 已入选 {author_counts.get(candidate.author, 0)} 条（上限 {max_per_author}）",
                "author": candidate.author,
            })
            continue
        # --- Pre-download chain-level duration gate (material window) ---------
        # The *material* window (``min_seconds``/``max_seconds`` below) used to be
        # applied only after the download.  A candidate the (wider) prefilter let
        # through was therefore fully downloaded and *then* refused by it -- pure
        # wasted traffic (the 9.13 cold rerun burned one such 202 s file's full
        # size).  Judge the same window here, *before* any download, so a
        # candidate whose metadata already proves it is out of range is dropped
        # without a single byte on the wire.
        #
        # Fires only when the metadata genuinely carries a duration
        # (``duration_seconds > 0`` **and** a non-empty ``duration_source`` -- the
        # honesty contract of ``replication_candidates``).  Without a metadata
        # duration the candidate falls through *unchanged* to the original
        # post-download measured check, so those rows behave exactly as before.
        #
        # A ``validation.duration_tolerance`` margin (default ±5 %) keeps a
        # boundary candidate that could still pass on its *measured* length
        # (e.g. 182 s against a 180 s ceiling) from being killed up front.
        metadata_duration = float(getattr(candidate, "duration_seconds", 0.0) or 0.0)
        duration_source = str(getattr(candidate, "duration_source", "") or "")
        if metadata_duration > 0 and duration_source:
            tolerance = float(validation_settings_snapshot(config)["duration_tolerance"])
            material_min, material_max = effective_duration_window(
                window_overrides, candidate, min_seconds, max_seconds
            )
            below_window = material_min > 0 and metadata_duration < material_min * (1.0 - tolerance)
            above_window = material_max > 0 and metadata_duration > material_max * (1.0 + tolerance)
            if below_window or above_window:
                unmet.append({
                    "video_id": candidate.video_id,
                    "stage": "duration_pre",
                    "reason": (
                        f"元数据时长 {metadata_duration:.0f}s 不在素材窗口 "
                        f"{material_min:.0f}~{material_max:.0f}s（下载前判定，未消耗流量）"
                    ),
                    "duration_source": duration_source,
                })
                continue
        if budget is not None:
            allowed, budget_reason = budget.allow()
            if not allowed:
                unmet.append({"video_id": candidate.video_id, "stage": "budget", "reason": budget_reason})
                break
        rel = (relevance or {}).get(candidate.video_id, 0.0)
        try:
            resolved = resolve_download_target(resolver, candidate, media_urls)
        except MediaResolutionError as exc:
            # Source-level / transport failure (retries exhausted, API error,
            # network): recorded apart from a normal "no address" miss so the
            # operator can tell "the source broke" from "this clip had nothing".
            errors.append({
                "video_id": candidate.video_id,
                "stage": "resolve",
                "error": str(exc)[:300],
            })
            continue
        except Exception as exc:  # an adapter blowing up must not abort the run
            errors.append({
                "video_id": candidate.video_id,
                "stage": "resolve",
                "error": f"{type(exc).__name__}: {str(exc)[:280]}",
            })
            continue
        if resolved is None:
            # No usable address (normal miss): one download failure, no alert and
            # no retry -- the adapter contract's "return '' / None" half.
            unmet.append({
                "video_id": candidate.video_id,
                "stage": "no_media_url",
                "reason": "未解析到可用下载地址",
            })
            continue
        download_url, download_referer = resolved
        try:
            video_path = video_root / f"{candidate.video_id}.mp4"
            pre_size = file_size(video_path)
            try:
                invoke_downloader(downloader, download_url, video_path, config, budget.item_cap() if budget is not None else None, download_referer)
            except MediaTooLargeError as exc:
                if budget is None:
                    raise
                # A *streamed* oversize already pulled real bytes off the wire
                # before aborting (``source == "streamed"``, ``bytes_read > 0``);
                # charge them so the traffic ledger is honest.  A *declared*
                # oversize read no body (``bytes_read == 0``) and charges nothing.
                budget.mark_transferred(int(getattr(exc, "bytes_read", 0) or 0))
                action = budget.note_oversize(budget.item_cap())
                budget.skip(candidate, "budget_item" if action == "skip" else "budget_bytes", str(exc)[:160], relevance=rel)
                if action == "stop":
                    unmet.append({"video_id": candidate.video_id, "stage": "budget", "reason": str(exc)[:160]})
                    break
                unmet.append({"video_id": candidate.video_id, "stage": "too_large", "reason": f"体积超限：{str(exc)[:120]}"})
                continue
            if budget is not None:
                # Charge real traffic *before* any later rejection: a file that
                # is downloaded and then dropped still cost bandwidth.
                budget.mark_transferred(measure_transferred_bytes(video_path, pre_size))
            downloaded += 1
            try:
                probe = prober(video_path, config)
            except Exception as exc:
                invalid_media += 1
                warnings.append(f"{candidate.video_id} 媒体无效，已跳过：{str(exc)[:120]}")
                unmet.append({"video_id": candidate.video_id, "stage": "invalid_media", "reason": f"媒体无效：{str(exc)[:120]}"})
                continue
            media_ok, media_reason = validate_probe(probe)
            if not media_ok:
                invalid_media += 1
                warnings.append(f"{candidate.video_id} 媒体无效，已跳过：{media_reason}")
                unmet.append({"video_id": candidate.video_id, "stage": "invalid_media", "reason": f"媒体无效：{media_reason}"})
                continue
            # Download-time validation sits *before* ``budget.select`` so a
            # corrupt file can never hold a slot or a byte of the run budget.
            # A rejection simply falls through to the next candidate.
            validation_record = validate_candidate(
                video_path, config, candidate=candidate, probe=probe, prober=prober,
                validator=_validator(deps),
            )
            if validation_record is not None:
                record_validation(validation_store, validation_record, stage="material")
                if not validation_record.get("passed"):
                    validation_rejected += 1
                    reason = validation_reason(validation_record)
                    warnings.append(f"{candidate.video_id} 下载校验未通过，已剔除：{reason}")
                    unmet.append({
                        "video_id": candidate.video_id,
                        "stage": "validation",
                        "reason": reason,
                        "conclusion": validation_record.get("conclusion"),
                    })
                    continue
                validation_passed += 1
            # Post-download half of the duration gate (see
            # ``measured_duration_window_reject``): the pre-download window can
            # only judge candidates whose metadata carried a duration.
            window_reject, window_reason = measured_duration_window_reject(
                float(probe.get("duration_seconds") or 0), config,
                metadata_duration=float(getattr(candidate, "duration_seconds", 0.0) or 0.0),
                source=str(getattr(candidate, "source", "") or ""),
            )
            if window_reject:
                rejected_duration_post += 1
                unmet.append({"video_id": candidate.video_id, "stage": "duration_post", "reason": window_reason})
                continue
            duration = float(probe.get("duration_seconds") or 0)
            material_min, material_max = effective_duration_window(
                window_overrides, candidate, min_seconds, max_seconds
            )
            if not material_min <= duration <= material_max:
                rejected_duration += 1
                unmet.append({
                    "video_id": candidate.video_id,
                    "stage": "duration",
                    "reason": f"时长 {duration:.0f}s 不在 {material_min:.0f}~{material_max:.0f}s",
                })
                continue
            # P1c: only a candidate that cleared both duration gates may hold a
            # slot and a byte of the run budget (see the script chain for the
            # rationale); a duration-rejected file is dropped outright.
            if budget is not None:
                budget.select(candidate, _safe_size(video_path), relevance=rel, stage="material")
            cache_dir = cache_root / "material" / candidate.video_id
            temp_dir = temp_root / "material" / candidate.video_id
            ocr_result = ocr_runner.run(video_path, duration, cache_dir / "ocr", temp_dir / "ocr")
            visual = compute_visual_metrics(video_path, duration, ocr_result, temp_dir / "motion", config)
            face = face_runner.run(video_path, duration, cache_dir / "face", temp_dir / "face")
            face_checked += 1
            # A severely truncated sample (covers < half the clip) must not be
            # read as a trustworthy ``face_free`` verdict, so downgrade it.  The
            # class is descriptive metadata now (see below), yet the honest label
            # still matters for the delivery/catalogue.
            face = truncated_face_class(face)
            face_status_value = face.get("status")
            if face_status_value is not None and str(face_status_value) != "ok":
                face_errors += 1
                if str(face_status_value) == "error":
                    detail = str(face.get("error") or "").strip()[:160]
                    warnings.append(f"{candidate.video_id} 人脸采样失败：{detail or '未知错误'}")
            transcript = transcriber.run(video_path, cache_dir / "asr", temp_dir / "asr")
            rate = speech_rate(transcript, duration)
            face_class = str(face.get("face_class") or FACE_UNAVAILABLE)
            # Face class is **descriptive only** after 2026-09-18: a themed run may
            # legitimately need footage with people in it (a host, an interview,
            # an on-site recording), so ``face_class`` no longer admits or refuses
            # a candidate.  It is still computed and carried on every selected row
            # (``material_replica_sources`` / clip metadata / 00-素材目录.json) so a
            # reader can always see what shipped.
            if not visual.visual_ok:
                rejected_visual += 1
                unmet.append({
                    "video_id": candidate.video_id,
                    "stage": "visual",
                    # ``reject_reason`` names the failing criterion (运动不足 /
                    # 文字过多 / OCR 不可测); the fallback keeps mocked/legacy
                    # metrics (no reason field) readable.
                    "reason": visual.reject_reason or (
                        f"画面代理不达标：变化率 {visual.motion_frame_ratio:.2f}、"
                        f"OCR 覆盖 {visual.ocr_text_frame_ratio:.2f}"
                    ),
                    "motion_frame_ratio": visual.motion_frame_ratio,
                    "ocr_text_frame_ratio": visual.ocr_text_frame_ratio,
                    "ocr_measurable": visual.ocr_measurable,
                })
                continue
            if rate >= max_speech:
                chars = len(re.sub(r"\s+", "", str((transcript or {}).get("text") or "")))
                rejected_speech += 1
                unmet.append({
                    "video_id": candidate.video_id,
                    "stage": "speech",
                    "reason": f"口播密度 {rate:.2f} ≥ {max_speech} 字/秒（{chars} 字/{duration:.0f}s）",
                    "speech_rate": rate,
                    "chars": chars,
                })
                continue
            # Byte ceiling: a file that cleared every quality gate but would push
            # the delivered sum past ``max_delivered_bytes`` is skipped (never
            # appended), so the delivered set can never overshoot.  ``0`` = no
            # ceiling -> this block is dead code, exactly as before.
            delivered_size = _safe_size(video_path)
            if max_delivered_bytes > 0 and delivered_bytes + delivered_size > max_delivered_bytes:
                unmet.append({
                    "video_id": candidate.video_id,
                    "stage": "quota_bytes",
                    "reason": (
                        f"加入后交付源片体积 {delivered_bytes + delivered_size} 字节将超上限 "
                        f"{max_delivered_bytes} 字节（本条 {delivered_size} 字节），跳过"
                    ),
                    "size_bytes": delivered_size,
                })
                continue
            selected.append({
                "candidate": candidate,
                "video_path": video_path,
                "probe": probe,
                "visual": visual,
                "face": face,
                "speech_rate": rate,
                "selected_reason": (
                    f"画面变化率 {visual.motion_frame_ratio:.2f}/OCR覆盖 {visual.ocr_text_frame_ratio:.2f}/"
                    f"口播 {rate:.2f} 字每秒/人脸 {face_class}"
                ),
            })
            author_counts[candidate.author] = author_counts.get(candidate.author, 0) + 1
            delivered_bytes += delivered_size
        except Exception as exc:
            errors.append({"video_id": candidate.video_id, "error": str(exc)[:300]})

    # The byte quota strengthens ``insufficient``: a run can satisfy ``min_count``
    # yet still be a floor miss (the "24.86 MiB / 4 sources" complaint), which the
    # count-only check would have called "success".  ``0`` floor -> never missed.
    byte_floor_missed = min_delivered_bytes > 0 and delivered_bytes < min_delivered_bytes
    insufficient = len(selected) < min_count or byte_floor_missed
    if not insufficient:
        conclusion = "success"
    elif not selected:
        conclusion = "empty"
    elif byte_floor_missed and len(selected) >= min_count:
        conclusion = "insufficient_bytes"
    else:
        conclusion = "insufficient"
    stage = {
        "candidate_pool": len(pool),
        "heat_median": round(float(median), 6),
        "face_checked": face_checked,
        "selected": len(selected),
        "delivered_bytes": delivered_bytes,
        "min_delivered_bytes": min_delivered_bytes,
        "max_delivered_bytes": max_delivered_bytes,
        "rejected": len(unmet),
        "errors": len(errors),
        "conclusion": conclusion,
    }
    if validation_on:
        stage["stage_validation_passed"] = validation_passed
        stage["stage_validation_rejected"] = validation_rejected
    if gate_requested:
        # Present only when the switch is on, so a config without it keeps the
        # historic ``stage`` payload byte-identical.
        stage["relevance_gate"] = "active" if gate_enabled else "inactive:no_subject_terms"
    if freshness is not None:
        # Same convention as ``relevance_gate``: present only when the window is
        # set, so a config without it keeps the historic ``stage`` byte-identical.
        stage["material_freshness"] = freshness
    if insufficient:
        warnings.append(_material_summary(len(pool), face_checked, len(selected), min_count, unmet))
        if byte_floor_missed:
            warnings.append(
                _material_byte_floor_warning(len(selected), target, delivered_bytes, min_delivered_bytes, unmet)
            )
    face_backend = str(getattr(face_runner, "backend", FACE_UNAVAILABLE))
    counters: dict[str, Any] = {
        "downloaded": downloaded,
        "face_checked": face_checked,
        "face_errors": face_errors,
        # Kept at ``0`` for manifest-key compatibility: face class no longer
        # rejects anything (2026-09-18), so nothing can be counted here.  The
        # descriptive distribution is on ``material_replica_sources`` instead.
        "clips_rejected_face_heavy": 0,
        "clips_rejected_duration": rejected_duration,
        "rejected_duration_post": rejected_duration_post,
        "rejected_not_video": rejected_not_video,
        "invalid_media": invalid_media,
        "rejected_pool": rejected_pool,
        "rejected_relevance": rejected_relevance,
        "rejected_author_duplicate": rejected_author_duplicate,
        "rejected_visual": rejected_visual,
        "rejected_speech": rejected_speech,
        "material_selected": len(selected),
    }
    if dedup_on:
        counters["rejected_cross_run"] = rejected_cross_run
    if freshness is not None:
        counters["rejected_stale"] = rejected_stale
    if validation_on:
        counters["stage_validation_passed"] = validation_passed
        counters["stage_validation_rejected"] = validation_rejected
    return {
        "status": "success" if not insufficient else "insufficient",
        "selected": selected,
        "insufficient": insufficient,
        "delivered_bytes": delivered_bytes,
        "median_heat": median,
        "unmet": unmet,
        "stage": stage,
        "face_backend": face_backend,
        "face_backend_status": "ok" if face_backend != FACE_UNAVAILABLE else FACE_UNAVAILABLE,
        "stage_validation_passed": validation_passed,
        "stage_validation_rejected": validation_rejected,
        "counters": counters,
        "warnings": warnings,
        "errors": errors,
        # Present only when the freshness window is set: a config without the key
        # must produce a result dict that is byte-for-byte the pre-change one.
        **({"freshness": freshness} if freshness is not None else {}),
    }
