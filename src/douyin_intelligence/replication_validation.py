"""Download-time validation for the material-replication workflow.

A completed download is not trusted just because the bytes arrived: Douyin
serves truncated / corrupt MP4s whose error only surfaces much later (e.g. a
2 fps face sampling of 58 expected frames that decodes to 13, with ffmpeg
reporting ``Invalid NAL unit size``).  This module turns "the download
succeeded" into "the media actually decodes and matches its metadata" before a
single byte is charged to the download budget.

Three independent checks, in order:

1. **ffprobe stream info** -- reuse :func:`materials.probe_video`, which raises
   on a non-zero ffprobe exit or a non-positive duration (itself a corrupt-file
   signal);
2. **full-stream decode** -- ``ffmpeg -v error -nostats -progress pipe:1 -i
   <file> [-frames:v N] -map 0:v:0 -f null -``; the error-line count, the
   decoded frame count and the first error text are captured, together with the
   **last decoded frame timestamp** (``out_time_us``).  The completeness of the
   decode is judged by *timeline coverage* -- ``coverage = last_pts /
   container_duration`` -- and additionally, when ffprobe reports ``nb_frames``,
   by an exact frame-count comparison.  ``duration × fps`` is deliberately
   **not** used as a frame-count baseline (a static / very-low-fps clip would be
   misjudged as truncated).
3. **duration comparison** -- ``abs(measured - metadata) / metadata <=
   duration_tolerance`` (inclusive, default ±5%), only when metadata carries a
   duration (see ``require_metadata_duration`` / ``duration_checked``).

``decode_time_budget_seconds`` semantics: a **positive** value bounds the full
decode and a timeout degrades to the sampled probe; **``0`` means "skip the
full decode entirely"** -- the sampled probe runs immediately and the record is
truthfully marked ``degraded``, never a silent full pass.  A negative value is
rejected by ``config`` validation.

The conclusion is an explicit enum so a rejection can never be mistaken for a
different gate: ``ok`` / ``probe_failed`` / ``undecodable`` / ``short_decode``
/ ``duration_mismatch`` / ``metadata_missing`` / ``degraded`` /
``unknown_coverage``.

Everything external (prober, stream prober, decoder) is injectable so the
whole layer is unit-testable without a real ffmpeg.  When the ``validation``
block is absent or ``enabled`` is false every function here is a no-op: the
delivery is **behaviourally equivalent** to a pre-change run and differs from it
only by additive fields, never by a changed or removed value, and the layer's
own artifact surface (``validation.json``, the ``validation`` manifest block,
``validation_*`` counters) does not exist at all.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from .exporter import atomic_write_json
from .media_tools import resolve_media_tool


VALIDATION_SCHEMA_VERSION = 1

#: The skip stage used by every download loop for a validation rejection.  A
#: dedicated value keeps a rejected file from being attributed to
#: ``budget`` / ``budget_item`` / ``budget_bytes``.
VALIDATION_STAGE = "validation"

# --- conclusions ----------------------------------------------------------- #
CONCLUSION_OK = "ok"
CONCLUSION_PROBE_FAILED = "probe_failed"
CONCLUSION_UNDECODABLE = "undecodable"
CONCLUSION_SHORT_DECODE = "short_decode"
CONCLUSION_DURATION_MISMATCH = "duration_mismatch"
CONCLUSION_METADATA_MISSING = "metadata_missing"
CONCLUSION_DEGRADED = "degraded"
#: The full decode finished but neither a frame count (``nb_frames``) nor a
#: coverage timestamp was obtainable, so "the whole file decoded" cannot be
#: *proven*.  This is a **pass** (we do not reject on our own blindness) but it
#: is a distinct enum value so a report can never fold it into "全片解码通过".
CONCLUSION_UNKNOWN_COVERAGE = "unknown_coverage"

CONCLUSIONS = (
    CONCLUSION_OK,
    CONCLUSION_PROBE_FAILED,
    CONCLUSION_UNDECODABLE,
    CONCLUSION_SHORT_DECODE,
    CONCLUSION_DURATION_MISMATCH,
    CONCLUSION_METADATA_MISSING,
    CONCLUSION_DEGRADED,
    CONCLUSION_UNKNOWN_COVERAGE,
)

#: A conclusion in this set means the file may be delivered.  ``degraded`` and
#: ``unknown_coverage`` are *passes*: the full decode could not finish (timeout /
#: no decoder), or it finished without a usable completeness signal.  They are
#: accepted but never disguised as a full pass.
PASSING_CONCLUSIONS = frozenset({CONCLUSION_OK, CONCLUSION_DEGRADED, CONCLUSION_UNKNOWN_COVERAGE})

#: When ffprobe knows ``nb_frames`` the frame-count check is exact, with a small
#: slack so container rounding / the odd extra frame cannot trip it: a shortfall
#: is flagged only when it exceeds ``max(SLACK_MIN, expected * SLACK_RATIO)``.
FRAME_COUNT_SLACK_RATIO = 0.02
FRAME_COUNT_SLACK_MIN = 2

#: Coverage-based truncation.  ``coverage = last_decoded_pts / duration``; a
#: decode is *short* only when the tail gap is both relatively and absolutely
#: large (``> max(COVERAGE_MAX_GAP_SECONDS, duration * COVERAGE_MAX_GAP_RATIO)``
#: and ``coverage < COVERAGE_MIN_RATIO``), so the ~1-frame tail every complete
#: container leaves un-decoded is never mistaken for a truncation.
COVERAGE_MIN_RATIO = 0.95
COVERAGE_MAX_GAP_SECONDS = 1.0
COVERAGE_MAX_GAP_RATIO = 0.05

#: The sampled-frame fallback used when the full decode exceeds its time budget
#: (or when the budget is ``0``).
DEGRADED_SAMPLE_FRAMES = 3

#: Time budget for the *sampled* fallback decode.  Independent of the full-decode
#: budget so ``decode_time_budget_seconds = 0`` (skip the full decode) still lets
#: the 3-frame probe run.
SAMPLE_DECODE_TIME_BUDGET_SECONDS = 5.0

#: First ffmpeg error text is truncated to this many characters in the record.
FIRST_ERROR_MAX_CHARS = 160

#: The cache sidecar written next to a validated download.  Its presence lets a
#: later run skip re-decoding a file whose size, mtime **and** content
#: fingerprint are unchanged.
ATTESTATION_SUFFIX = ".validation.json"

#: Bytes sampled from each end of a cached file to fingerprint its content.
FINGERPRINT_SAMPLE_BYTES = 64 * 1024

DEFAULTS: dict[str, Any] = {
    # Off unless the config explicitly turns it on, so a config without the
    # block reproduces the pre-change behaviour exactly.
    "enabled": False,
    "duration_tolerance": 0.05,
    "full_decode": True,
    "decode_time_budget_seconds": 20.0,
    "require_metadata_duration": False,
    "cache_attestation": True,
}


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def validation_settings(config: dict[str, Any]) -> dict[str, Any]:
    """The ``jobs.material_replication.validation`` block (``{}`` when absent)."""
    return (config.get("jobs") or {}).get("material_replication", {}).get("validation") or {}


def validation_settings_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    """The *effective* validation knobs, recorded verbatim in the artifacts."""
    settings = validation_settings(config)
    return {
        "enabled": bool(settings.get("enabled", DEFAULTS["enabled"])),
        "duration_tolerance": float(settings.get("duration_tolerance", DEFAULTS["duration_tolerance"])),
        "full_decode": bool(settings.get("full_decode", DEFAULTS["full_decode"])),
        "decode_time_budget_seconds": float(
            settings.get("decode_time_budget_seconds", DEFAULTS["decode_time_budget_seconds"])
        ),
        "require_metadata_duration": bool(
            settings.get("require_metadata_duration", DEFAULTS["require_metadata_duration"])
        ),
        "cache_attestation": bool(settings.get("cache_attestation", DEFAULTS["cache_attestation"])),
    }


def validation_enabled(config: dict[str, Any]) -> bool:
    return bool(validation_settings_snapshot(config)["enabled"])


# --------------------------------------------------------------------------- #
# Default media probes (real ffmpeg/ffprobe; injectable for tests)
# --------------------------------------------------------------------------- #
def _run_media_process(command: list[str], timeout: float) -> tuple[subprocess.CompletedProcess[str] | None, bool]:
    """Run ``command`` with a *custom* timeout, returning ``(completed, timed_out)``."""
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=max(1.0, float(timeout)),
        )
        return completed, False
    except subprocess.TimeoutExpired:
        return None, True
    except OSError:
        # Missing binary / unreadable path behaves like a decode failure; keep
        # the caller's error path uniform instead of raising a fresh exception.
        return subprocess.CompletedProcess(command, 127, "", "media process could not be started"), False


def _parse_timecode(value: str) -> float | None:
    """Parse ffmpeg ``out_time`` (``HH:MM:SS.ffffff``) into seconds."""
    parts = str(value or "").strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (float(part) for part in parts)
    except (TypeError, ValueError):
        return None
    return hours * 3600 + minutes * 60 + seconds


def _parse_progress(stdout: str) -> dict[str, float | int | None]:
    """Last ``frame`` / ``out_time`` values from an ffmpeg ``-progress`` stream.

    Returns ``{"frames": int|None, "last_pts_seconds": float|None}``.  ffmpeg
    emits ``out_time_us`` and ``out_time_ms`` -- both are microseconds (a
    long-standing ffmpeg quirk) -- plus an ``out_time`` timecode; the last
    reported value wins, so the *final* decoded frame's timestamp is returned.
    """
    frames: int | None = None
    last_pts: float | None = None
    for line in (stdout or "").splitlines():
        key, _, value = line.strip().partition("=")
        if key == "frame":
            try:
                frames = int(value)
            except (TypeError, ValueError):
                continue
        elif key in ("out_time_us", "out_time_ms"):
            try:
                last_pts = int(value) / 1_000_000.0
            except (TypeError, ValueError):
                continue
        elif key == "out_time":
            parsed = _parse_timecode(value)
            if parsed is not None:
                last_pts = parsed
    return {"frames": frames, "last_pts_seconds": last_pts}


def default_stream_prober(path: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Best-effort ``nb_frames`` / fps lookup used for the expected-frame count."""
    command = [
        resolve_media_tool(config, "ffprobe"), "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=nb_frames,avg_frame_rate,r_frame_rate", "-of", "json", str(path),
    ]
    completed, _ = _run_media_process(command, 30.0)
    payload: dict[str, Any] = {}
    if completed is not None and not completed.returncode:
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError:
            payload = {}
    stream = (payload.get("streams") or [{}])[0] if isinstance(payload, dict) else {}
    nb_frames: int | None = None
    try:
        candidate = int(stream.get("nb_frames"))
        nb_frames = candidate if candidate > 0 else None
    except (TypeError, ValueError):
        nb_frames = None
    fps: float | None = None
    for key in ("avg_frame_rate", "r_frame_rate"):
        rate = str(stream.get(key) or "")
        if "/" in rate:
            numerator, _, denominator = rate.partition("/")
            try:
                value = float(numerator) / float(denominator)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            if value > 0:
                fps = value
                break
    return {"nb_frames": nb_frames, "fps": fps}


def default_decoder(path: Path, config: dict[str, Any], *, time_budget: float, full: bool) -> dict[str, Any]:
    """Decode ``path`` to ``null`` and report errors, frames and coverage.

    ``-progress pipe:1`` writes machine-readable progress (including
    ``frame=N`` and ``out_time_us``) to stdout, independent of the ``-v error``
    log level, so the same run yields the error text, the decoded-frame count
    and the last decoded timestamp.

    Argument order matters: ``-frames:v`` and ``-map`` are **output** options
    and must come *after* ``-i <file>``.  Putting ``-frames:v`` before ``-i``
    makes ffmpeg refuse the whole command ("Option frames:v ... cannot be
    applied to input url ... Move this option before the file it belongs to"),
    which would be mis-read as a decode error and reject a perfectly good file.

    Returns ``{"kind": "ok"|"errors"|"timeout"|"unavailable", "decoded_frames",
    "last_pts_seconds", "error_lines", "first_error"}``.
    """
    ffmpeg = resolve_media_tool(config, "ffmpeg")
    command = [ffmpeg, "-v", "error", "-nostats", "-progress", "pipe:1", "-i", str(path)]
    if not full:
        command += ["-frames:v", str(DEGRADED_SAMPLE_FRAMES)]
    command += ["-map", "0:v:0", "-f", "null", "-"]
    completed, timed_out = _run_media_process(command, time_budget)
    if timed_out:
        return {"kind": "timeout", "decoded_frames": None, "last_pts_seconds": None, "error_lines": 0, "first_error": ""}
    assert completed is not None
    if completed.returncode == 127:
        # The binary could not be started at all (ffmpeg not installed).  That is
        # an *infrastructure* gap, not a corrupt file: never let it reject every
        # download.  The caller degrades instead.
        return {
            "kind": "unavailable", "decoded_frames": None, "last_pts_seconds": None, "error_lines": 1,
            "first_error": (completed.stderr or "").strip()[:FIRST_ERROR_MAX_CHARS],
        }
    error_lines = [line for line in (completed.stderr or "").splitlines() if line.strip()]
    progress = _parse_progress(completed.stdout or "")
    kind = "ok" if (completed.returncode == 0 and not error_lines) else "errors"
    return {
        "kind": kind,
        "decoded_frames": progress["frames"],
        "last_pts_seconds": progress["last_pts_seconds"],
        "error_lines": len(error_lines),
        "first_error": error_lines[0] if error_lines else "",
    }


def _default_prober(config: dict[str, Any]) -> Callable[[Path, dict[str, Any]], dict[str, Any]]:
    from .materials import probe_video
    return probe_video


# --------------------------------------------------------------------------- #
# Record helpers
# --------------------------------------------------------------------------- #
def validation_reason(record: dict[str, Any]) -> str:
    """A one-line, human-readable reason derived from a validation record."""
    conclusion = record.get("conclusion")
    detail = str(record.get("error") or record.get("first_error") or "").strip()
    labels = {
        CONCLUSION_PROBE_FAILED: "ffprobe 无法解析",
        CONCLUSION_UNDECODABLE: "全片解码失败",
        CONCLUSION_SHORT_DECODE: "解码覆盖/帧数明显不足",
        CONCLUSION_DURATION_MISMATCH: "时长与元数据不符",
        CONCLUSION_METADATA_MISSING: "缺少元数据时长",
        CONCLUSION_DEGRADED: "解码超时降级为抽帧",
        CONCLUSION_UNKNOWN_COVERAGE: "无法确证全片解码完整",
        CONCLUSION_OK: "通过",
    }
    label = labels.get(str(conclusion), str(conclusion))
    parts = [f"下载校验未通过（{conclusion}）：{label}"]
    if record.get("coverage") is not None:
        parts.append(f"解码覆盖 {float(record['coverage']) * 100:.1f}%")
    if record.get("decoded_frames") is not None or record.get("expected_frames") is not None:
        parts.append(
            f"解码帧 {record.get('decoded_frames')}/{record.get('expected_frames')}"
        )
    if record.get("deviation") is not None:
        parts.append(f"时长偏差 {float(record['deviation']) * 100:.1f}%")
    if detail:
        parts.append(detail[:FIRST_ERROR_MAX_CHARS])
    return "；".join(parts)


def _base_record(candidate: Any, metadata_duration: float) -> dict[str, Any]:
    return {
        "video_id": str(getattr(candidate, "video_id", "") or ""),
        "title": str(getattr(candidate, "title", "") or ""),
        "author": str(getattr(candidate, "author", "") or ""),
        "metadata_duration": round(float(metadata_duration or 0), 3),
        "measured_duration": None,
        "deviation": None,
        "decoded_frames": None,
        "expected_frames": None,
        "coverage": None,
        "last_decoded_seconds": None,
        "coverage_checked": False,
        "coverage_note": "",
        "duration_checked": False,
        "error_lines": 0,
        "first_error": "",
        "duration_seconds": None,
        "decode_mode": "skipped",
        "conclusion": CONCLUSION_PROBE_FAILED,
        "passed": False,
        "cache_attestation": "miss",
    }


# --------------------------------------------------------------------------- #
# Core validation
# --------------------------------------------------------------------------- #
def validate_downloaded(
    path: Path,
    config: dict[str, Any],
    *,
    candidate: Any = None,
    probe: dict[str, Any] | None = None,
    prober: Callable[[Path, dict[str, Any]], dict[str, Any]] | None = None,
    stream_prober: Callable[[Path, dict[str, Any]], dict[str, Any]] | None = None,
    decoder: Callable[..., dict[str, Any]] | None = None,
    metadata_duration: float | None = None,
) -> dict[str, Any]:
    """Run the three checks on one file and return a structured record.

    ``probe`` (a pre-computed ffprobe payload) / ``prober`` / ``stream_prober``
    / ``decoder`` are injectable so the layer can be exercised without a real
    ffmpeg.  This function never raises for a bad file: the failure is reported
    through :data:`CONCLUSIONS`.
    """
    settings = validation_settings_snapshot(config)
    if metadata_duration is None:
        metadata_duration = float(getattr(candidate, "duration_seconds", 0.0) or 0.0)
    record = _base_record(candidate, metadata_duration)

    # 1. ffprobe stream info ------------------------------------------------ #
    payload = probe
    if payload is None:
        active_prober = prober or _default_prober(config)
        try:
            payload = active_prober(Path(path), config)
        except Exception as exc:  # ffprobe non-zero exit / unreadable file
            record["conclusion"] = CONCLUSION_PROBE_FAILED
            record["error"] = str(exc)[:FIRST_ERROR_MAX_CHARS]
            return record
    try:
        measured = float((payload or {}).get("duration_seconds") or 0)
    except (TypeError, ValueError):
        measured = 0.0
    if measured <= 0:
        record["conclusion"] = CONCLUSION_PROBE_FAILED
        record["error"] = "ffprobe 未返回有效时长"
        return record
    # A file with a duration but no video stream (an image-album post whose
    # ``video_download_url`` is its background music) is not a usable download.
    # Only trigger when the payload actually carries the dimensions, so a
    # minimal probe payload is never punished for omitting them.
    if ("width" in (payload or {}) or "height" in (payload or {})) and not (
        (payload or {}).get("width") and (payload or {}).get("height")
    ):
        record["conclusion"] = CONCLUSION_PROBE_FAILED
        record["error"] = "下载文件无视频流（ffprobe 未返回画面尺寸）"
        return record
    record["measured_duration"] = round(measured, 3)
    record["duration_seconds"] = round(measured, 3)

    # 2. decode ------------------------------------------------------------- #
    degraded = False
    degraded_reason = ""
    unknown_coverage = False
    if settings["full_decode"]:
        active_decoder = decoder or default_decoder
        budget = float(settings["decode_time_budget_seconds"])
        if budget <= 0:
            # ``0`` means "do not run a full decode": go straight to the sampled
            # probe.  Truthfully recorded as ``degraded`` -- never a full pass.
            degraded = True
            degraded_reason = "budget_zero"
            outcome = _safe_decode(active_decoder, path, config, SAMPLE_DECODE_TIME_BUDGET_SECONDS, full=False)
        else:
            outcome = _safe_decode(active_decoder, path, config, budget, full=True)
            if str(outcome.get("kind")) == "timeout":
                # Out of time: fall back to a 3-frame probe.  The record must
                # never claim a full decode happened when it did not.
                degraded = True
                degraded_reason = "timeout"
                outcome = _safe_decode(active_decoder, path, config, SAMPLE_DECODE_TIME_BUDGET_SECONDS, full=False)
        unavailable = str(outcome.get("kind")) == "unavailable"
        if unavailable:
            # The decoder itself could not run (e.g. ffmpeg missing): we cannot
            # confirm the file, but we must not pretend it decoded *or* reject
            # every download.  Degrade and say so.
            degraded = True
            degraded_reason = degraded_reason or "unavailable"
        record["decode_mode"] = "unavailable" if unavailable else ("degraded_sample" if degraded else "full")
        record["decoded_frames"] = outcome.get("decoded_frames")
        record["last_decoded_seconds"] = _as_float(outcome.get("last_pts_seconds"))
        record["error_lines"] = int(outcome.get("error_lines") or 0)
        record["first_error"] = str(outcome.get("first_error") or "")[:FIRST_ERROR_MAX_CHARS]
        if str(outcome.get("kind")) == "errors":
            record["conclusion"] = CONCLUSION_UNDECODABLE
            return record
        if degraded:
            record["decode_degraded_reason"] = degraded_reason
        else:
            # Full decode completed.  Completeness is judged by *timeline
            # coverage* (last decoded pts / container duration), plus an exact
            # frame-count comparison when ffprobe reports ``nb_frames``.  The
            # old ``duration × fps`` baseline is deliberately gone: it made a
            # static / very-low-fps clip look truncated.
            coverage = None
            last_pts = record["last_decoded_seconds"]
            if last_pts is not None and measured > 0:
                coverage = max(0.0, min(1.0, float(last_pts) / measured))
            record["coverage"] = round(coverage, 6) if coverage is not None else None
            record["coverage_checked"] = coverage is not None
            expected = _expected_frames(path, config, stream_prober)
            record["expected_frames"] = expected
            decoded = record["decoded_frames"]
            short = False
            if expected is not None and decoded is not None:
                if (expected - decoded) > max(FRAME_COUNT_SLACK_MIN, expected * FRAME_COUNT_SLACK_RATIO):
                    short = True
            if coverage is not None:
                gap = measured - float(last_pts)
                if coverage < COVERAGE_MIN_RATIO and gap > max(COVERAGE_MAX_GAP_SECONDS, measured * COVERAGE_MAX_GAP_RATIO):
                    short = True
            if short:
                record["conclusion"] = CONCLUSION_SHORT_DECODE
                return record
            if expected is None and coverage is None:
                # Neither completeness signal is obtainable: do NOT quietly
                # record ``ok``.  Surface it as its own (passing) conclusion so a
                # reader can tell "verified" from "could not verify".
                unknown_coverage = True
                record["coverage_note"] = "未取得 nb_frames 与覆盖时长，未做全片解码完整性判定"

    # 3. duration comparison ------------------------------------------------ #
    if metadata_duration > 0:
        deviation = (measured - metadata_duration) / metadata_duration
        record["deviation"] = round(deviation, 6)
        record["duration_checked"] = True
        if abs(deviation) > settings["duration_tolerance"]:
            record["conclusion"] = CONCLUSION_DURATION_MISMATCH
            return record
    elif settings["require_metadata_duration"]:
        record["conclusion"] = CONCLUSION_METADATA_MISSING
        record["error"] = "元数据时长为 0（缺失），且配置要求必须比对"
        return record

    if degraded:
        conclusion = CONCLUSION_DEGRADED
    elif unknown_coverage:
        conclusion = CONCLUSION_UNKNOWN_COVERAGE
    else:
        conclusion = CONCLUSION_OK
    record["conclusion"] = conclusion
    record["passed"] = conclusion in PASSING_CONCLUSIONS
    return record


def _as_float(value: Any) -> float | None:
    """Best-effort ``float`` conversion, or ``None``."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_decode(
    active_decoder: Callable[..., dict[str, Any]],
    path: Path,
    config: dict[str, Any],
    time_budget: float,
    *,
    full: bool,
) -> dict[str, Any]:
    """Call a decoder, turning any exception into an ``unavailable`` outcome.

    A broken/missing decoder must never abort the whole run, and must never be
    mistaken for a corrupt file.
    """
    try:
        return active_decoder(Path(path), config, time_budget=time_budget, full=full) or {}
    except Exception as exc:  # a broken decoder must not abort the whole run
        return {
            "kind": "unavailable", "decoded_frames": None, "last_pts_seconds": None,
            "error_lines": 1, "first_error": str(exc)[:FIRST_ERROR_MAX_CHARS],
        }


def _expected_frames(
    path: Path,
    config: dict[str, Any],
    stream_prober: Callable[[Path, dict[str, Any]], dict[str, Any]] | None,
) -> int | None:
    """``nb_frames`` for the exact frame-count check (or ``None``).

    Only ffprobe's ``nb_frames`` is used.  ``duration × fps`` is intentionally
    **not** used as a fallback: a static or very-low-fps clip has far fewer
    real frames than its duration implies, and multiplying would mislabel it as
    truncated.  When ``nb_frames`` is unknown the caller falls back to the
    coverage check (and to ``unknown_coverage`` if that is unknown too).
    """
    active = stream_prober or default_stream_prober
    try:
        details = active(Path(path), config) or {}
    except Exception:
        return None
    nb_frames = details.get("nb_frames")
    try:
        if nb_frames and int(nb_frames) > 0:
            return int(nb_frames)
    except (TypeError, ValueError):
        pass
    return None


# --------------------------------------------------------------------------- #
# Cache attestation
# --------------------------------------------------------------------------- #
def attestation_path(video_path: Path) -> Path:
    """``<video_id>.mp4.validation.json`` next to a cached download."""
    path = Path(video_path)
    return path.with_name(path.name + ATTESTATION_SUFFIX)


def _file_stamp(path: Path) -> dict[str, int]:
    stat = Path(path).stat()
    return {"size_bytes": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}


def _file_fingerprint(path: Path) -> str | None:
    """Content fingerprint: sha256 of the size plus head/tail samples.

    ``(size, mtime)`` alone is not enough: restoring a backup or copying a file
    with ``shutil.copy2`` preserves both, so a *different* file would wrongly
    reuse the old attestation.  Hashing the file size and the first/last
    ``FINGERPRINT_SAMPLE_BYTES`` bytes pins the actual content cheaply, without
    reading a whole multi-hundred-MB video on every cache check.
    """
    target = Path(path)
    try:
        size = int(target.stat().st_size)
        digest = hashlib.sha256()
        digest.update(str(size).encode("ascii"))
        with target.open("rb") as stream:
            head = stream.read(FINGERPRINT_SAMPLE_BYTES)
            digest.update(head)
            if size > FINGERPRINT_SAMPLE_BYTES:
                stream.seek(max(0, size - FINGERPRINT_SAMPLE_BYTES))
                digest.update(stream.read(FINGERPRINT_SAMPLE_BYTES))
        return digest.hexdigest()
    except OSError:
        return None


def _stamp_matches(attestation: dict[str, Any], stamp: dict[str, int], fingerprint: str | None) -> bool:
    """True only when size, mtime **and** content fingerprint all agree."""
    if (
        int(attestation.get("size_bytes") or -1) != stamp["size_bytes"]
        or int(attestation.get("mtime_ns") or -1) != stamp["mtime_ns"]
    ):
        return False
    stored = attestation.get("content_sha256")
    if not stored or fingerprint is None:
        # A sidecar written before content fingerprints existed (or an unreadable
        # file) can never be trusted -> force a fresh validation.
        return False
    return str(stored) == fingerprint


def read_attestation(video_path: Path) -> dict[str, Any] | None:
    path = attestation_path(video_path)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_attestation(video_path: Path, record: dict[str, Any]) -> None:
    """Persist a sidecar proving ``record`` describes *this* exact file.

    The sidecar also carries a failing conclusion.  A cached bad file is never
    deleted (nothing here removes anything) -- it is marked so a later run can
    skip the expensive re-decode and still refuse to deliver it.
    """
    try:
        stamp = _file_stamp(Path(video_path))
    except OSError:
        return
    atomic_write_json(attestation_path(video_path), {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "video_id": record.get("video_id"),
        "conclusion": record.get("conclusion"),
        "passed": bool(record.get("passed")),
        "metadata_duration": record.get("metadata_duration"),
        "measured_duration": record.get("measured_duration"),
        "deviation": record.get("deviation"),
        "decoded_frames": record.get("decoded_frames"),
        "expected_frames": record.get("expected_frames"),
        "coverage": record.get("coverage"),
        "last_decoded_seconds": record.get("last_decoded_seconds"),
        "coverage_checked": record.get("coverage_checked"),
        "coverage_note": record.get("coverage_note"),
        "duration_checked": record.get("duration_checked"),
        "error_lines": record.get("error_lines"),
        "first_error": record.get("first_error"),
        "duration_seconds": record.get("duration_seconds"),
        "decode_mode": record.get("decode_mode"),
        "size_bytes": stamp["size_bytes"],
        "mtime_ns": stamp["mtime_ns"],
        "content_sha256": _file_fingerprint(video_path),
    })


def _record_from_attestation(attestation: dict[str, Any], candidate: Any) -> dict[str, Any]:
    """Replay a stored attestation as a validation record."""
    record = _base_record(candidate, float(attestation.get("metadata_duration") or 0.0))
    for key in (
        "conclusion", "metadata_duration", "measured_duration", "deviation",
        "decoded_frames", "expected_frames", "coverage", "last_decoded_seconds",
        "coverage_checked", "coverage_note", "duration_checked", "error_lines", "first_error",
        "duration_seconds", "decode_mode", "video_id", "decode_degraded_reason",
    ):
        if key in attestation:
            record[key] = attestation[key]
    if not record["video_id"]:
        record["video_id"] = str(getattr(candidate, "video_id", "") or "")
    record["passed"] = bool(attestation.get("passed"))
    record["cache_attestation"] = "hit"
    return record


def validate_cached(
    video_path: Path,
    config: dict[str, Any],
    *,
    candidate: Any = None,
    probe: dict[str, Any] | None = None,
    prober: Callable[[Path, dict[str, Any]], dict[str, Any]] | None = None,
    stream_prober: Callable[[Path, dict[str, Any]], dict[str, Any]] | None = None,
    decoder: Callable[..., dict[str, Any]] | None = None,
    metadata_duration: float | None = None,
) -> dict[str, Any]:
    """Validate a file, honouring the on-disk cache attestation sidecar.

    A matching sidecar (same size + mtime *and* content fingerprint)
    short-circuits the expensive decode -- whether it recorded a pass *or* a
    failure, so a known-bad cache entry is skipped on subsequent runs instead of
    being re-decoded.  Any missing/stale sidecar forces a fresh validation and
    rewrites the sidecar.
    """
    settings = validation_settings_snapshot(config)
    if settings["cache_attestation"]:
        try:
            stamp = _file_stamp(Path(video_path))
        except OSError:
            stamp = None
        attestation = read_attestation(video_path)
        if stamp is not None and attestation is not None:
            fingerprint = _file_fingerprint(video_path)
            if _stamp_matches(attestation, stamp, fingerprint):
                return _record_from_attestation(attestation, candidate)
    record = validate_downloaded(
        video_path, config, candidate=candidate, probe=probe, prober=prober,
        stream_prober=stream_prober, decoder=decoder, metadata_duration=metadata_duration,
    )
    if settings["cache_attestation"]:
        write_attestation(Path(video_path), record)
    return record


def validate_candidate(
    video_path: Path,
    config: dict[str, Any],
    *,
    candidate: Any = None,
    probe: dict[str, Any] | None = None,
    prober: Callable[[Path, dict[str, Any]], dict[str, Any]] | None = None,
    stream_prober: Callable[[Path, dict[str, Any]], dict[str, Any]] | None = None,
    decoder: Callable[..., dict[str, Any]] | None = None,
    validator: Callable[..., dict[str, Any]] | None = None,
    metadata_duration: float | None = None,
) -> dict[str, Any] | None:
    """The pipeline entry point: a record, or ``None`` when the layer is off.

    ``validator`` lets a caller replace the whole validation effect (the real
    one runs ffprobe + ffmpeg); everything else is forwarded to it as keyword
    arguments.
    """
    if not validation_enabled(config):
        return None
    try:
        if validator is not None:
            record = validator(
                video_path, config, candidate=candidate, probe=probe, prober=prober,
                stream_prober=stream_prober, decoder=decoder, metadata_duration=metadata_duration,
            )
        else:
            record = validate_cached(
                video_path, config, candidate=candidate, probe=probe, prober=prober,
                stream_prober=stream_prober, decoder=decoder, metadata_duration=metadata_duration,
            )
    except Exception as exc:  # fail closed, but visibly and without killing the loop
        record = _base_record(candidate, metadata_duration or float(getattr(candidate, "duration_seconds", 0.0) or 0.0))
        record["conclusion"] = CONCLUSION_PROBE_FAILED
        record["error"] = f"下载校验执行异常：{str(exc)[:FIRST_ERROR_MAX_CHARS]}"
        record["passed"] = False
    if not isinstance(record, dict):
        return None
    record.setdefault("video_id", str(getattr(candidate, "video_id", "") or ""))
    record.setdefault("passed", str(record.get("conclusion")) in PASSING_CONCLUSIONS)
    return record


# --------------------------------------------------------------------------- #
# Run-level aggregation
# --------------------------------------------------------------------------- #
def record_validation(
    store: list[dict[str, Any]] | None,
    record: dict[str, Any] | None,
    *,
    stage: str | None = None,
) -> None:
    """Append a validation record to a run-level store (no-op when either is None).

    ``stage`` tags the record with the selection stage that produced it
    (``"script"`` / ``"material"``) so the run-level ``by_stage`` breakdown in
    ``manifest.validation`` / ``validation.json`` / the readme can be computed
    from one source.
    """
    if store is not None and isinstance(record, dict):
        if stage:
            record["stage"] = stage
        store.append(record)


def _empty_stage_counts() -> dict[str, int]:
    return {"validated": 0, "passed": 0, "rejected": 0}


def validation_counts(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate validation records into the counts shared by every artifact."""
    by_conclusion: dict[str, int] = {name: 0 for name in CONCLUSIONS}
    passed = 0
    cache_attested = 0
    cache_attested_bad = 0
    duration_checked = 0
    by_stage: dict[str, dict[str, int]] = {}
    for record in records:
        conclusion = str(record.get("conclusion") or "")
        by_conclusion[conclusion] = by_conclusion.get(conclusion, 0) + 1
        record_passed = bool(record.get("passed"))
        if record_passed:
            passed += 1
        cached = str(record.get("cache_attestation")) == "hit"
        if cached:
            cache_attested += 1
            if not record_passed:
                # A cached *bad* verdict: the file was skipped, not re-decoded,
                # and is still refused.  Surfaced so a reader can tell "cache
                # held a known-bad file" from "there simply were few candidates".
                cache_attested_bad += 1
        if record.get("duration_checked"):
            duration_checked += 1
        stage = str(record.get("stage") or "")
        if stage:
            bucket = by_stage.setdefault(stage, _empty_stage_counts())
            bucket["validated"] += 1
            bucket["passed" if record_passed else "rejected"] += 1
    validated = len(records)
    return {
        "validated": validated,
        "passed": passed,
        "rejected": validated - passed,
        "cache_attested": cache_attested,
        "cache_attested_bad": cache_attested_bad,
        "duration_checked": duration_checked,
        "by_conclusion": by_conclusion,
        "by_stage": by_stage,
    }


def build_validation_block(config: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The manifest block, or ``None`` when the layer is disabled."""
    if not validation_enabled(config):
        return None
    counts = validation_counts(records)
    return {
        "enabled": True,
        "config": validation_settings_snapshot(config),
        "counts": counts,
        # Per-stage breakdown, mirroring the stage dicts on the selection
        # results, so the run-level total can be reconciled against "脚本 X 条 /
        # 素材 Y 条" without the two layers sharing confusingly-identical keys.
        "by_stage": counts["by_stage"],
    }


def write_validation_artifact(process_dir: Path, config: dict[str, Any], records: list[dict[str, Any]]) -> None:
    """Write ``05-过程数据/validation.json`` with the full per-file detail."""
    if not validation_enabled(config):
        return
    atomic_write_json(Path(process_dir) / "validation.json", {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "config": validation_settings_snapshot(config),
        "counts": validation_counts(records),
        "records": list(records),
    })
