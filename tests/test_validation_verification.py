"""Independent verification suite for download-time validation (QA, Task #1).

Written by the QA engineer as a *second* angle on the "download-time validation +
bad-file replacement + silent-truncation fix" change.  It deliberately does not
re-run the Engineer's own ``tests/test_download_validation.py`` assertions; it
probes the boundaries that module leaves untested:

* the real ``ffmpeg`` fallback command is never exercised by a fake decoder, so
  its argument order is checked directly here (a regression guard: the sampled
  fallback used to reject a perfectly good file);
* the frame-count / coverage truncation criteria and the "no completeness
  signal" outcome (``unknown_coverage``, never a silent ``ok``);
* sidecar freshness (size/mtime/content fingerprint) and the corrupt-sidecar
  path;
* whether a ``degraded`` verdict actually reaches the delivery artifacts.

This file contains **no** source changes and does not touch the Engineer's tests.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from douyin_intelligence.config import load_config
from douyin_intelligence.replication_validation import (
    CONCLUSION_DEGRADED,
    CONCLUSION_SHORT_DECODE,
    CONCLUSION_UNDECODABLE,
    CONCLUSION_UNKNOWN_COVERAGE,
    attestation_path,
    default_decoder,
    validate_cached,
    validate_downloaded,
)

_PROBE = {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}


def _config(tmp: Path) -> dict:
    config = load_config()
    config["_project_root"] = str(tmp)
    mr = config["jobs"]["material_replication"]
    mr["prefilter"] = {"enabled": False}
    mr["validation"] = {
        "enabled": True, "duration_tolerance": 0.05, "full_decode": True,
        "decode_time_budget_seconds": 20, "require_metadata_duration": False,
        "cache_attestation": True,
    }
    return config


def _ok_decoder(decoded: int):
    def dec(path, config, *, time_budget, full):
        return {"kind": "ok", "decoded_frames": decoded, "error_lines": 0, "first_error": ""}
    return dec


def _frames(nb_frames: int, fps: float):
    return lambda path, config: {"nb_frames": nb_frames, "fps": fps}


# --------------------------------------------------------------------------- #
# Frame-count criteria: a modest loss must be caught; "no signal" is surfaced
# --------------------------------------------------------------------------- #
def test_twenty_percent_frame_loss_is_detected(tmp_path: Path) -> None:
    """A file losing 20% of its frames must be flagged ``short_decode``.

    The user's symptom was "58 -> 13" (78% lost); the fix also catches a loss as
    small as the slack allows (``> max(2, 2% × expected)``), so a 2000 -> 1600
    loss (20%) is no longer silently accepted.
    """
    config = _config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    record = validate_downloaded(
        video, config, metadata_duration=60.0, probe=dict(_PROBE),
        decoder=_ok_decoder(1600), stream_prober=_frames(2000, 30.0),
    )
    assert record["expected_frames"] == 2000
    assert record["decoded_frames"] == 1600
    assert record["conclusion"] == CONCLUSION_SHORT_DECODE


def test_exactly_half_frames_is_short_decode(tmp_path: Path) -> None:
    """``decoded == 0.5 × expected`` is a shortfall, not a pass."""
    config = _config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    record = validate_downloaded(
        video, config, metadata_duration=60.0, probe=dict(_PROBE),
        decoder=_ok_decoder(1000), stream_prober=_frames(2000, 30.0),
    )
    assert record["conclusion"] == CONCLUSION_SHORT_DECODE


def test_loss_beyond_half_is_short_decode(tmp_path: Path) -> None:
    config = _config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    record = validate_downloaded(
        video, config, metadata_duration=60.0, probe=dict(_PROBE),
        decoder=_ok_decoder(800), stream_prober=_frames(2000, 30.0),
    )
    assert record["conclusion"] == CONCLUSION_SHORT_DECODE


def test_missing_nb_frames_and_fps_yields_unknown_coverage(tmp_path: Path) -> None:
    """Without ``nb_frames`` *and* without a coverage timestamp the run must not
    record a silent ``ok``: it reports ``unknown_coverage`` instead."""
    config = _config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    record = validate_downloaded(
        video, config, metadata_duration=60.0, probe=dict(_PROBE),
        decoder=_ok_decoder(5),
        stream_prober=lambda path, config: {"nb_frames": None, "fps": None},
    )
    assert record["expected_frames"] is None
    assert record["conclusion"] == CONCLUSION_UNKNOWN_COVERAGE
    assert record["coverage_note"]


# --------------------------------------------------------------------------- #
# Sidecar freshness (size + mtime)
# --------------------------------------------------------------------------- #
def test_size_change_forces_revalidation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"a" * 5000)
    calls: list[str] = []

    def decoder(path, config, *, time_budget, full):
        calls.append(Path(path).name)
        return {"kind": "ok", "decoded_frames": 1800, "error_lines": 0, "first_error": ""}

    validate_cached(video, config, probe=dict(_PROBE), decoder=decoder, stream_prober=_frames(1800, 30.0))
    assert len(calls) == 1
    calls.clear()
    video.write_bytes(b"b" * 6000)  # same name, new size
    record = validate_cached(video, config, probe=dict(_PROBE), decoder=decoder, stream_prober=_frames(1800, 30.0))
    assert len(calls) == 1, "a size change must invalidate the sidecar"
    assert record["cache_attestation"] == "miss"


def test_corrupt_sidecar_is_ignored_and_revalidated(tmp_path: Path) -> None:
    config = _config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"a" * 5000)
    calls: list[str] = []

    def decoder(path, config, *, time_budget, full):
        calls.append(Path(path).name)
        return {"kind": "ok", "decoded_frames": 1800, "error_lines": 0, "first_error": ""}

    validate_cached(video, config, probe=dict(_PROBE), decoder=decoder, stream_prober=_frames(1800, 30.0))
    attestation_path(video).write_text("{ not json", encoding="utf-8")
    calls.clear()
    validate_cached(video, config, probe=dict(_PROBE), decoder=decoder, stream_prober=_frames(1800, 30.0))
    assert len(calls) == 1


def test_bad_cache_replay_still_rejects(tmp_path: Path) -> None:
    """A cached *bad* verdict is replayed and still refuses to deliver."""
    config = _config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"c" * 5000)
    calls: list[str] = []

    def decoder(path, config, *, time_budget, full):
        calls.append(Path(path).name)
        return {"kind": "errors", "decoded_frames": 0, "error_lines": 1, "first_error": "Invalid NAL unit size"}

    first = validate_cached(video, config, probe=dict(_PROBE), decoder=decoder, stream_prober=_frames(1800, 30.0))
    assert first["conclusion"] == CONCLUSION_UNDECODABLE and first["passed"] is False
    calls.clear()
    second = validate_cached(video, config, probe=dict(_PROBE), decoder=decoder, stream_prober=_frames(1800, 30.0))
    assert calls == [], "a matching sidecar must skip the re-decode"
    assert second["conclusion"] == CONCLUSION_UNDECODABLE and second["passed"] is False
    assert second["cache_attestation"] == "hit"
    assert video.is_file(), "a bad cache entry is marked, never deleted"


# --------------------------------------------------------------------------- #
# Content fingerprint: size + mtime alone is not enough
# --------------------------------------------------------------------------- #
def test_same_size_and_mtime_content_swap_is_revalidated(tmp_path: Path) -> None:
    import os

    config = _config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"G" * 5000)
    calls: list[str] = []

    def decoder(path, config, *, time_budget, full):
        calls.append(Path(path).name)
        return {"kind": "ok", "decoded_frames": 1800, "error_lines": 0, "first_error": ""}

    validate_cached(video, config, probe=dict(_PROBE), decoder=decoder, stream_prober=_frames(1800, 30.0))
    stamp = video.stat()
    video.write_bytes(b"B" * 5000)  # different content, identical length
    os.utime(video, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    calls.clear()
    validate_cached(video, config, probe=dict(_PROBE), decoder=decoder, stream_prober=_frames(1800, 30.0))
    assert calls, "content change with identical size+mtime should be re-validated"


# --------------------------------------------------------------------------- #
# Ordering: validation happens before any budget accounting
# --------------------------------------------------------------------------- #
def _make_pipeline_env(tmp: Path, bad: set[str], monkeypatch):
    """Return (config, deps, events) driving the download-only loop with a real validator."""
    from douyin_intelligence.replication_pipeline import ReplicationDeps
    from douyin_intelligence.replication_selection import DownloadBudget

    config = _config(tmp)
    config["jobs"]["material_replication"]["download_budget"] = {
        "enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9,
    }
    events: list[tuple[str, str]] = []

    def validator(path, cfg, **kwargs):
        events.append(("validate", Path(path).stem))
        return validate_cached(
            path, cfg, candidate=kwargs.get("candidate"), probe=kwargs.get("probe"),
            prober=kwargs.get("prober"),
            stream_prober=_frames(1800, 30.0),
            decoder=lambda p, c, *, time_budget, full: (
                {"kind": "errors", "decoded_frames": 0, "error_lines": 1, "first_error": "Invalid NAL unit size"}
                if Path(p).stem in bad
                else {"kind": "ok", "decoded_frames": 1800, "error_lines": 0, "first_error": ""}
            ),
            metadata_duration=kwargs.get("metadata_duration"),
        )

    original_select = DownloadBudget.select

    def traced_select(self, candidate, size_bytes, relevance=0.0):
        events.append(("select", candidate.video_id))
        return original_select(self, candidate, size_bytes, relevance)

    # monkeypatch restores the class attribute automatically after the test.
    monkeypatch.setattr(DownloadBudget, "select", traced_select)

    def collector(config, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None, **kwargs):
        source = Path(str(config["_project_root"])) / "raw" / "search_contents_1.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"aweme_id": f"v{i:02d}", "desc": f"标题-{i}",
             "author": {"uid": f"u{i}", "nickname": f"作者{i}"},
             "create_time": "2026-09-11T08:00:00+08:00",
             "statistics": {"digg_count": 100, "comment_count": 10, "share_count": 5, "collect_count": 20},
             "duration": 60.0, "video_download_url": f"https://s/{i}",
             "share_url": f"https://www.douyin.com/video/{i}"}
            for i in range(4)
        ]
        source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        before_sanitize([source])
        return {"status": "success", "keywords": keywords, "budget": budget}

    def downloader(url, destination, cfg, *, max_bytes=None):
        dest = Path(destination)
        if dest.is_file() and dest.stat().st_size > 1024:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * 2048)

    deps = ReplicationDeps(collector=collector, downloader=downloader,
                           prober=lambda p, c: dict(_PROBE), validator=validator)
    return config, deps, events


def test_validation_precedes_budget_select(tmp_path: Path, monkeypatch) -> None:
    from douyin_intelligence.replication_pipeline import run_material_replication

    config, deps, events = _make_pipeline_env(tmp_path, bad={"v00", "v01"}, monkeypatch=monkeypatch)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )

    bad_selects = [e for e in events if e[0] == "select" and e[1] in {"v00", "v01"}]
    assert bad_selects == [], "a rejected file must never charge a budget slot"
    for candidate in ("v02", "v03"):
        assert events.index(("validate", candidate)) < events.index(("select", candidate))

    block = json.loads(
        (Path(result["output_dir"]) / "05-过程数据" / "download_budget.json").read_text(encoding="utf-8")
    )
    assert block["used"]["count"] == 2
    assert {item["video_id"] for item in block["selected"]} == {"v02", "v03"}


# --------------------------------------------------------------------------- #
# Degraded verdict must be visible in the delivery artifacts
# --------------------------------------------------------------------------- #
def test_degraded_verdict_is_recorded_but_counted_as_passed(tmp_path: Path) -> None:
    """``degraded`` surfaces via ``by_conclusion`` yet is folded into "通过"."""
    config = _config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")

    def decoder(path, config, *, time_budget, full):
        if full:
            return {"kind": "timeout", "decoded_frames": None, "error_lines": 0, "first_error": ""}
        return {"kind": "ok", "decoded_frames": 3, "error_lines": 0, "first_error": ""}

    record = validate_downloaded(video, config, metadata_duration=60.0, probe=dict(_PROBE), decoder=decoder)
    assert record["conclusion"] == CONCLUSION_DEGRADED
    assert record["passed"] is True
    assert record["decode_mode"] == "degraded_sample"


# --------------------------------------------------------------------------- #
# Regression guard: the real degraded fallback command must be well-formed
# --------------------------------------------------------------------------- #
def test_degraded_fallback_command_is_accepted_by_ffmpeg(tmp_path: Path, monkeypatch) -> None:
    """The sampled-fallback command must place ``-frames:v`` AFTER ``-i``.

    Argument order is an *output* option rule: ``-frames:v`` before ``-i`` makes
    real ffmpeg refuse the command, which used to judge a good file undecodable.
    """
    captured: dict[str, list[str]] = {}

    def fake_run(command, timeout):
        captured["command"] = list(command)
        return subprocess.CompletedProcess(command, 0, "frame=3\n", ""), False

    monkeypatch.setattr(
        "douyin_intelligence.replication_validation._run_media_process", fake_run,
    )
    config = _config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    default_decoder(video, config, time_budget=5.0, full=False)

    command = captured["command"]
    assert "-frames:v" in command, command
    assert command.index("-i") < command.index("-frames:v"), (
        "'-frames:v' is an output option and must come after '-i'; "
        f"got {command}"
    )
