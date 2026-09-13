"""Download-time validation: ffprobe + full decode + duration check (Task #2).

The user-reported defect this locks down:

* a Douyin MP4 that downloads fine but is internally corrupt (``Invalid NAL
  unit size``) reached ``04-原片`` and the candidate pool with **no** check ever
  noticing -- a 2 fps sample of an expected 58 frames decoded only 13, and the
  face ratio was silently computed over 13.

Covered here:

* the three checks and every conclusion enum (``ok`` / ``probe_failed`` /
  ``undecodable`` / ``short_decode`` / ``duration_mismatch`` /
  ``metadata_missing`` / ``degraded``);
* the exact duration-tolerance boundary (5% passes, >5% fails);
* a rejected file never enters ``04-原片``, never charges the download budget,
  and the loop continues to the next candidate;
* a *cache hit* on a bad file is rejected (it is not short-circuited straight to
  delivery) and leaves a visible attestation sidecar rather than being deleted;
* every candidate failing validation returns boundedly (no ``while`` retry);
* the four count surfaces (manifest / readme / validation.json / counters)
  agree, and the whole layer is byte-identical when disabled or absent;
* ``face_metrics`` no longer hides a truncated frame sample.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path

import pytest

from douyin_intelligence.config import ConfigurationError, load_config
from douyin_intelligence.face_metrics import truncated_face_class
from douyin_intelligence.media_tools import media_tool_available, resolve_media_tool
from douyin_intelligence.replication_candidates import _row_duration_detail
from douyin_intelligence.replication_pipeline import ReplicationDeps, run_material_replication
from douyin_intelligence.replication_validation import (
    CONCLUSION_DEGRADED,
    CONCLUSION_DURATION_MISMATCH,
    CONCLUSION_METADATA_MISSING,
    CONCLUSION_OK,
    CONCLUSION_PROBE_FAILED,
    CONCLUSION_SHORT_DECODE,
    CONCLUSION_UNDECODABLE,
    CONCLUSION_UNKNOWN_COVERAGE,
    attestation_path,
    default_decoder,
    validate_cached,
    validate_downloaded,
)

_PROBE = {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _config(tmp_path: Path) -> dict:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    return config


def _validation_enabled_config(tmp_path: Path) -> dict:
    config = _config(tmp_path)
    config["jobs"]["material_replication"]["validation"] = {
        "enabled": True,
        "duration_tolerance": 0.05,
        "full_decode": True,
        "decode_time_budget_seconds": 20,
        "require_metadata_duration": False,
        "cache_attestation": True,
    }
    # Isolate the download cost-control layers that share the same pipeline.
    config["jobs"]["material_replication"]["prefilter"] = {"enabled": False}
    return config


def _row(video_id: str, author: str, *, duration: float = 60.0, digg: int = 100, url: str = "") -> dict:
    return {
        "aweme_id": video_id,
        "desc": f"标题-{video_id}",
        "author": {"uid": f"uid-{author}", "nickname": author},
        "create_time": "2026-09-11T08:00:00+08:00",
        "statistics": {"digg_count": digg, "comment_count": 10, "share_count": 5, "collect_count": 20},
        "duration": duration,
        "video_download_url": url or f"https://signed.example/{video_id}",
        "share_url": f"https://www.douyin.com/video/{video_id}",
    }


def _collector(rows: list[dict]):
    def collect(config, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None, **kwargs):
        source = Path(str(config.get("_project_root"))) / "raw" / "search_contents_1.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        before_sanitize([source])
        return {"status": "success", "keywords": keywords, "budget": budget}
    return collect


def _deps(rows: list[dict], *, validator=None, item_size: int = 2048):
    downloaded: list[str] = []

    def downloader(url, destination, config, *, max_bytes=None):
        dest = Path(destination)
        video_id = dest.stem
        # Mirror ``materials.download_video``'s cache short-circuit: a
        # pre-existing, plausible file is reused instead of re-fetched.
        if dest.is_file() and dest.stat().st_size > 1024:
            downloaded.append(video_id)
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * item_size)
        downloaded.append(video_id)

    def prober(path, config):
        return dict(_PROBE)

    return ReplicationDeps(
        collector=_collector(rows), downloader=downloader, prober=prober, validator=validator,
    ), downloaded


def _make_validator(*, decoder=None, stream_prober=None):
    """A pipeline ``validator`` that runs the REAL core with faked media effects.

    Uses :func:`validate_cached` (not ``validate_downloaded``) so the full
    production path -- attestation read/write included -- is exercised.
    """
    def _validate(path, config, **kwargs):
        return validate_cached(
            path, config,
            candidate=kwargs.get("candidate"),
            probe=kwargs.get("probe"),
            prober=kwargs.get("prober"),
            stream_prober=stream_prober,
            decoder=decoder,
            metadata_duration=kwargs.get("metadata_duration"),
        )
    return _validate


def _rejecting_decoder(reason: str):
    def decoder(path, config, *, time_budget, full):
        return {"kind": "errors", "decoded_frames": 0, "error_lines": 1, "first_error": reason}
    return decoder


def _ok_decoder(decoded: int | None = None):
    def decoder(path, config, *, time_budget, full):
        return {"kind": "ok", "decoded_frames": decoded, "error_lines": 0, "first_error": ""}
    return decoder


def _source_files(output_dir: Path) -> list[str]:
    return sorted(path.name for path in (output_dir / "04-原片").glob("*") if path.is_file())


# --------------------------------------------------------------------------- #
# 1. Core checks and every conclusion
# --------------------------------------------------------------------------- #
def test_core_detects_invalid_nal_unit_as_undecodable(tmp_path: Path) -> None:
    """The exact user symptom: a decodable-looking file whose decode errors."""
    config = _validation_enabled_config(tmp_path)
    video = tmp_path / "7684280662489120165.mp4"
    video.write_bytes(b"fake")
    record = validate_downloaded(
        video, config, metadata_duration=29.371,
        probe={"duration_seconds": 29.371, "width": 1080, "height": 1920},
        decoder=_rejecting_decoder("[h264 @ 0000] Invalid NAL unit size (24794 > 22581)."),
    )
    assert record["conclusion"] == CONCLUSION_UNDECODABLE
    assert record["passed"] is False
    assert record["error_lines"] == 1
    assert "Invalid NAL unit size" in record["first_error"]
    # Every mandated field is present and populated.
    for key in (
        "video_id", "title", "author", "metadata_duration", "measured_duration", "deviation",
        "decoded_frames", "expected_frames", "error_lines", "first_error", "duration_seconds", "conclusion",
    ):
        assert key in record, key
    assert record["measured_duration"] == 29.371
    assert record["duration_seconds"] == 29.371


def test_core_probe_failure_and_missing_video_stream(tmp_path: Path) -> None:
    config = _validation_enabled_config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")

    def boom(path, config):
        raise ValueError("ffprobe 无法解析视频")

    record = validate_downloaded(video, config, prober=boom, decoder=_ok_decoder())
    assert record["conclusion"] == CONCLUSION_PROBE_FAILED
    assert "ffprobe" in record["error"]

    # Audio-only payload (no width/height) is a probe failure, not a decode crash.
    record = validate_downloaded(
        video, config, probe={"duration_seconds": 12.0, "width": None, "height": None},
        decoder=_ok_decoder(),
    )
    assert record["conclusion"] == CONCLUSION_PROBE_FAILED
    assert "无视频流" in record["error"]


def test_short_decode_is_flagged_when_frames_are_missing(tmp_path: Path) -> None:
    """58 expected frames, 13 decoded -> short_decode (the silent-truncation case)."""
    config = _validation_enabled_config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    record = validate_downloaded(
        video, config, metadata_duration=0.0,
        probe=dict(_PROBE),
        decoder=_ok_decoder(decoded=13),
        stream_prober=lambda path, config: {"nb_frames": 58, "fps": 2.0},
    )
    assert record["conclusion"] == CONCLUSION_SHORT_DECODE
    assert record["decoded_frames"] == 13
    assert record["expected_frames"] == 58


def test_full_decode_with_matching_frames_passes(tmp_path: Path) -> None:
    config = _validation_enabled_config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    record = validate_downloaded(
        video, config, metadata_duration=60.0,
        probe=dict(_PROBE),
        decoder=_ok_decoder(decoded=1800),
        stream_prober=lambda path, config: {"nb_frames": 1800, "fps": 30.0},
    )
    assert record["conclusion"] == CONCLUSION_OK
    assert record["passed"] is True


def test_decode_timeout_degrades_to_sampling_but_stays_honest(tmp_path: Path) -> None:
    config = _validation_enabled_config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    calls: list[bool] = []

    def decoder(path, config, *, time_budget, full):
        calls.append(full)
        if full:
            return {"kind": "timeout", "decoded_frames": None, "error_lines": 0, "first_error": ""}
        return {"kind": "ok", "decoded_frames": 3, "error_lines": 0, "first_error": ""}

    record = validate_downloaded(video, config, metadata_duration=60.0, probe=dict(_PROBE), decoder=decoder)
    assert calls == [True, False], "a timeout must fall back to the sampled decode"
    assert record["conclusion"] == CONCLUSION_DEGRADED
    assert record["passed"] is True
    assert record["decode_mode"] == "degraded_sample"


# --------------------------------------------------------------------------- #
# 2. Duration tolerance boundary (documented: inclusive)
# --------------------------------------------------------------------------- #
def test_duration_tolerance_boundary_is_inclusive(tmp_path: Path) -> None:
    """``abs(measured - metadata) / metadata <= tolerance`` -> exactly 5% passes."""
    config = _validation_enabled_config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")

    def run(measured: float) -> dict:
        return validate_downloaded(
            video, config, metadata_duration=60.0,
            probe={"duration_seconds": measured, "width": 1080, "height": 1920},
            decoder=_ok_decoder(decoded=1800),
            stream_prober=lambda path, config: {"nb_frames": 1800, "fps": 30.0},
        )

    exact = run(63.0)  # +5.0% -> inclusive pass
    assert exact["conclusion"] == CONCLUSION_OK
    assert abs(exact["deviation"] - 0.05) < 1e-9

    below = run(56.94)  # -5.1% -> fail
    assert below["conclusion"] == CONCLUSION_DURATION_MISMATCH
    assert below["passed"] is False

    above = run(63.06)  # +5.1% -> fail
    assert above["conclusion"] == CONCLUSION_DURATION_MISMATCH


def test_missing_metadata_duration_is_configurable(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    base = _validation_enabled_config(tmp_path)

    # Default: a missing metadata duration only skips the duration compare.
    record = validate_downloaded(
        video, base, metadata_duration=0.0, probe=dict(_PROBE),
        decoder=_ok_decoder(decoded=1800),
        stream_prober=lambda path, config: {"nb_frames": 1800, "fps": 30.0},
    )
    assert record["conclusion"] == CONCLUSION_OK

    strict = _validation_enabled_config(tmp_path)
    strict["jobs"]["material_replication"]["validation"]["require_metadata_duration"] = True
    record = validate_downloaded(
        video, strict, metadata_duration=0.0, probe=dict(_PROBE),
        decoder=_ok_decoder(decoded=1800),
    )
    assert record["conclusion"] == CONCLUSION_METADATA_MISSING
    assert record["passed"] is False


def test_decoder_unavailable_degrades_instead_of_rejecting_everything(tmp_path: Path) -> None:
    """A missing ffmpeg is an infrastructure gap, not a corrupt file."""
    config = _validation_enabled_config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")

    def decoder(path, config, *, time_budget, full):
        return {"kind": "unavailable", "decoded_frames": None, "error_lines": 1, "first_error": "media process could not be started"}

    record = validate_downloaded(video, config, metadata_duration=60.0, probe=dict(_PROBE), decoder=decoder)
    assert record["conclusion"] == CONCLUSION_DEGRADED
    assert record["passed"] is True
    assert record["decode_mode"] == "unavailable"


def test_validator_exception_fails_closed_but_is_visible(tmp_path: Path) -> None:
    config = _validation_enabled_config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")

    def broken_validator(*args, **kwargs):
        raise RuntimeError("boom")

    from douyin_intelligence.replication_validation import validate_candidate

    record = validate_candidate(video, config, validator=broken_validator)
    assert record is not None
    assert record["passed"] is False
    assert record["conclusion"] == CONCLUSION_PROBE_FAILED
    assert "boom" in record["error"]


# --------------------------------------------------------------------------- #
# 3. End to end: reject before the budget, replace from the same pool
# --------------------------------------------------------------------------- #
def test_bad_download_is_rejected_before_budget_and_replaced(tmp_path: Path) -> None:
    config = _validation_enabled_config(tmp_path)
    config["jobs"]["material_replication"]["download_budget"] = {
        "enabled": True, "max_count": 12, "max_bytes": 157_286_400, "max_item_bytes": 31_457_280,
    }
    rows = [_row(f"v{index:02d}", f"作者{index}") for index in range(6)]
    bad = {"v00", "v01"}

    validator = _make_validator(
        decoder=lambda path, config, *, time_budget, full: (
            _rejecting_decoder("Invalid NAL unit size (24794 > 22581).")(path, config, time_budget=time_budget, full=full)
            if Path(path).stem in bad
            else _ok_decoder(1800)(path, config, time_budget=time_budget, full=full)
        ),
        stream_prober=lambda path, config: {"nb_frames": 1800, "fps": 30.0},
    )
    deps, downloaded = _deps(rows, validator=validator)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    output_dir = Path(result["output_dir"])

    # 1. The bad files are NOT delivered, and the loop replaced them.
    assert set(downloaded) == {f"v{i:02d}" for i in range(6)}, "every candidate attempted once"
    assert _source_files(output_dir) == ["作者2_标题-v02_v02.mp4", "作者3_标题-v03_v03.mp4",
                                         "作者4_标题-v04_v04.mp4", "作者5_标题-v05_v05.mp4"]
    assert len(result["downloads"]) == 4

    # 2. The bad files never charged the budget.
    block = json.loads((output_dir / "05-过程数据" / "download_budget.json").read_text(encoding="utf-8"))
    assert block["used"]["count"] == 4
    assert block["used"]["bytes"] == 4 * 2048
    assert block["used"]["bytes"] <= 157_286_400

    # 3. Attribution distinguishes a validation rejection.
    manifest = json.loads((output_dir / "清单.json").read_text(encoding="utf-8"))
    rejected = [item for item in manifest["download_failures"] if item["stage"] == "validation"]
    assert {item["video_id"] for item in rejected} == bad
    assert all("Invalid NAL unit size" in item["reason"] for item in rejected)

    # 4. Four count surfaces agree.
    counts = manifest["validation"]["counts"]
    assert counts["validated"] == 6 and counts["passed"] == 4 and counts["rejected"] == 2
    assert manifest["counters"]["validation_passed"] == counts["passed"] == 4
    assert manifest["counters"]["validation_rejected"] == counts["rejected"] == 2
    artifact = json.loads((output_dir / "05-过程数据" / "validation.json").read_text(encoding="utf-8"))
    assert artifact["counts"] == counts
    assert len(artifact["records"]) == 6
    readme = (output_dir / "00-交付说明.md").read_text(encoding="utf-8")
    assert "## 下载校验" in readme
    assert "校验 6 次" in readme
    assert "全片解码通过 4 条 / 降级抽帧通过 0 条 / 覆盖未测通过 0 条，剔除 2 条" in readme
    assert "undecodable 2" in readme
    # Every promised file actually exists.
    assert (output_dir / "05-过程数据" / "validation.json").is_file()
    assert "05-过程数据/validation.json" in readme


def test_all_candidates_failing_validation_is_bounded(tmp_path: Path) -> None:
    config = _validation_enabled_config(tmp_path)
    config["jobs"]["material_replication"]["download_budget"] = {
        "enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9,
    }
    rows = [_row(f"v{index:02d}", f"作者{index}") for index in range(30)]
    validator = _make_validator(decoder=_rejecting_decoder("Invalid NAL unit size"))
    deps, downloaded = _deps(rows, validator=validator)

    started = time.monotonic()
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 15.0, f"疑似死循环：{elapsed:.1f}s"
    assert len(downloaded) == 30, "each candidate is attempted exactly once (bounded for-loop)"
    assert result["downloads"] == []
    assert result["status"] == "failed"
    output_dir = Path(result["output_dir"])
    block = json.loads((output_dir / "05-过程数据" / "download_budget.json").read_text(encoding="utf-8"))
    assert block["used"]["count"] == 0
    manifest = json.loads((output_dir / "清单.json").read_text(encoding="utf-8"))
    counts = manifest["validation"]["counts"]
    assert counts["validated"] == 30 and counts["passed"] == 0 and counts["rejected"] == 30
    assert counts["cache_attested"] == 0
    assert {k: v for k, v in counts["by_conclusion"].items() if v} == {"undecodable": 30}
    assert {item["stage"] for item in manifest["download_failures"]} == {"validation"}


def test_rejections_do_not_consume_the_count_cap(tmp_path: Path) -> None:
    """A rejected file frees its slot: the run still reaches the full 12."""
    config = _validation_enabled_config(tmp_path)
    config["jobs"]["material_replication"]["download_budget"] = {
        "enabled": True, "max_count": 12, "max_bytes": 157_286_400, "max_item_bytes": 31_457_280,
    }
    rows = [_row(f"v{index:02d}", f"作者{index}") for index in range(20)]
    bad = {"v00", "v01", "v02"}

    validator = _make_validator(
        decoder=lambda path, config, *, time_budget, full: (
            _rejecting_decoder("Invalid NAL unit size")(path, config, time_budget=time_budget, full=full)
            if Path(path).stem in bad
            else _ok_decoder(1800)(path, config, time_budget=time_budget, full=full)
        ),
        stream_prober=lambda path, config: {"nb_frames": 1800, "fps": 30.0},
    )
    deps, _ = _deps(rows, validator=validator)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    output_dir = Path(result["output_dir"])
    block = json.loads((output_dir / "05-过程数据" / "download_budget.json").read_text(encoding="utf-8"))
    # The three rejections did not burn budget slots -> still a full 12 delivered.
    assert block["used"]["count"] == 12
    assert len(result["downloads"]) == 12
    assert len(_source_files(output_dir)) == 12
    assert block["used"]["bytes"] <= 157_286_400
    manifest = json.loads((output_dir / "清单.json").read_text(encoding="utf-8"))
    counts = manifest["validation"]["counts"]
    assert (counts["validated"], counts["passed"], counts["rejected"], counts["cache_attested"]) == (15, 12, 3, 0)
    assert counts["by_conclusion"]["ok"] == 12
    assert counts["by_conclusion"]["undecodable"] == 3


def test_full_chain_all_rejected_is_bounded(tmp_path: Path, monkeypatch) -> None:
    config = _validation_enabled_config(tmp_path)
    config["jobs"]["material_replication"]["download_budget"] = {
        "enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9,
    }
    monkeypatch.setattr("douyin_intelligence.replication_pipeline.media_tool_available", lambda config, name: True)
    rows = [_row(f"v{index:02d}", f"作者{index}") for index in range(10)]
    calls: list[str] = []

    def downloader(url, destination, config, *, max_bytes=None):
        calls.append(Path(destination).stem)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"x" * 2048)

    deps = ReplicationDeps(
        collector=_collector(rows), downloader=downloader,
        prober=lambda path, config: dict(_PROBE),
        validator=_make_validator(decoder=_rejecting_decoder("Invalid NAL unit size")),
        transcriber=_Transcriber(), ocr=_Ocr(), face_detector=_Face(),
    )
    started = time.monotonic()
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", deps=deps)
    elapsed = time.monotonic() - started
    assert elapsed < 20.0, f"疑似死循环：{elapsed:.1f}s"
    # Each candidate is attempted at most once per selection loop.
    assert len(calls) <= 20
    output_dir = Path(result["output_dir"])
    manifest = json.loads((output_dir / "清单.json").read_text(encoding="utf-8"))
    assert manifest["validation"]["counts"]["rejected"] >= 1
    assert manifest["validation"]["counts"]["passed"] == 0
    # Attribution distinguishes "validation-rejected" from "empty pool".
    reasons = json.dumps(manifest["material_replica"]["rejected"], ensure_ascii=False)
    assert "下载校验未通过" in reasons
    assert "候选池为空" not in json.dumps(manifest["warnings"], ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 4. Cache hit must NOT bypass validation
# --------------------------------------------------------------------------- #
def _seed_cache(config: dict, video_id: str, payload: bytes) -> Path:
    from douyin_intelligence.replication_theme import project_path

    root = project_path(config, config["jobs"]["material_replication"]["media_root"]) / "material"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{video_id}.mp4"
    path.write_bytes(payload)
    return path


def test_cache_hit_on_bad_file_is_rejected_not_delivered(tmp_path: Path) -> None:
    config = _validation_enabled_config(tmp_path)
    config["jobs"]["material_replication"]["download_budget"] = {
        "enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9,
    }
    rows = [_row("v00", "作者A")]
    cached = _seed_cache(config, "v00", b"c" * 5000)
    validator = _make_validator(decoder=_rejecting_decoder("Invalid NAL unit size"))
    deps, _ = _deps(rows, validator=validator)

    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    output_dir = Path(result["output_dir"])
    assert result["downloads"] == []
    assert _source_files(output_dir) == []
    block = json.loads((output_dir / "05-过程数据" / "download_budget.json").read_text(encoding="utf-8"))
    assert block["used"]["count"] == 0 and block["used"]["bytes"] == 0
    # The bad cache file is marked, never silently deleted.
    assert cached.is_file()
    sidecar = json.loads(attestation_path(cached).read_text(encoding="utf-8"))
    assert sidecar["conclusion"] == CONCLUSION_UNDECODABLE
    assert sidecar["passed"] is False


def test_cache_attestation_skips_redecode_and_replays_the_verdict(tmp_path: Path) -> None:
    config = _validation_enabled_config(tmp_path)
    rows = [_row("v00", "作者A")]
    calls: list[str] = []

    def decoder(path, config, *, time_budget, full):
        calls.append(Path(path).stem)
        return {"kind": "ok", "decoded_frames": 1800, "error_lines": 0, "first_error": ""}

    validator = _make_validator(
        decoder=decoder, stream_prober=lambda path, config: {"nb_frames": 1800, "fps": 30.0},
    )
    deps, _ = _deps(rows, validator=validator)

    first = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    assert len(calls) == 1
    sidecar = attestation_path(
        Path(str(config["_project_root"])) / config["jobs"]["material_replication"]["media_root"]
        / "material" / "v00.mp4"
    )
    assert sidecar.is_file()

    # Same file, same size/mtime -> the attestation short-circuits the decode.
    shutil.rmtree(Path(first["output_dir"]))
    deps2, _ = _deps(rows, validator=validator)
    second = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps2,
    )
    assert len(calls) == 1, "a clean attestation must skip the second decode"
    manifest = json.loads((Path(second["output_dir"]) / "清单.json").read_text(encoding="utf-8"))
    assert manifest["validation"]["counts"]["cache_attested"] == 1


def test_stale_attestation_triggers_revalidation(tmp_path: Path) -> None:
    config = _validation_enabled_config(tmp_path)
    rows = [_row("v00", "作者A")]
    calls: list[str] = []

    def decoder(path, config, *, time_budget, full):
        calls.append(Path(path).stem)
        return {"kind": "ok", "decoded_frames": 1800, "error_lines": 0, "first_error": ""}

    validator = _make_validator(
        decoder=decoder, stream_prober=lambda path, config: {"nb_frames": 1800, "fps": 30.0},
    )
    deps, _ = _deps(rows, validator=validator)
    first = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    assert len(calls) == 1
    # Mutate the file: the size/mtime stamp no longer matches -> re-validate.
    cached = Path(str(config["_project_root"])) / config["jobs"]["material_replication"]["media_root"] / "material" / "v00.mp4"
    cached.write_bytes(b"y" * 6000)
    shutil.rmtree(Path(first["output_dir"]))
    deps2, _ = _deps(rows, validator=validator)
    run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps2,
    )
    assert len(calls) == 2, "a changed file must be re-validated"


# --------------------------------------------------------------------------- #
# 5. Download-only and the full chain behave the same
# --------------------------------------------------------------------------- #
class _Ocr:
    def run(self, video, duration, cache_dir, temp_dir):
        return {"status": "no_text", "items": [], "sampled_frames": 10}


class _Transcriber:
    def run(self, video, cache_dir, temp_dir, **kwargs):
        # Stage via ``cache_dir``: since P1a both stages share one video cache
        # root, so the source ``.mp4`` path no longer distinguishes them.
        if "script" in str(cache_dir):
            return {"status": "success", "text": "字" * 200, "segments": [{"start": 0, "end": 5, "text": "开场"}]}
        return {"status": "no_speech", "text": "", "segments": []}


class _Face:
    backend = "opencv_yunet"

    def status(self):
        return {"backend": "opencv_yunet", "status": "ok", "model_present": True}

    def run(self, video, duration, cache_dir, temp_dir):
        return {
            "backend": "opencv_yunet", "status": "ok", "face_frame_ratio": 0.0, "max_face_area_ratio": 0.0,
            "face_class": "face_free", "sampled_frames": 10, "face_per_frame": [False] * 10,
            "sample_interval_seconds": 1, "expected_frames": 10, "truncated": False,
        }


def test_full_chain_also_validates_and_reports(tmp_path: Path, monkeypatch) -> None:
    config = _validation_enabled_config(tmp_path)
    monkeypatch.setattr("douyin_intelligence.replication_pipeline.media_tool_available", lambda config, name: True)
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: __import__(
            "douyin_intelligence.replication_selection", fromlist=["VisualMetrics"]
        ).VisualMetrics(sampled_frames=10, motion_frame_ratio=0.9, ocr_text_frame_ratio=0.0, visual_ok=True),
    )
    rows = [_row(f"v{index:02d}", f"作者{index}") for index in range(4)]
    bad = {"v00", "v01"}

    validator = _make_validator(
        decoder=lambda path, config, *, time_budget, full: (
            _rejecting_decoder("Invalid NAL unit size")(path, config, time_budget=time_budget, full=full)
            if Path(path).stem in bad
            else _ok_decoder(1800)(path, config, time_budget=time_budget, full=full)
        ),
        stream_prober=lambda path, config: {"nb_frames": 1800, "fps": 30.0},
    )

    def downloader(url, destination, config, *, max_bytes=None):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"x" * 2048)

    deps = ReplicationDeps(
        collector=_collector(rows), downloader=downloader,
        prober=lambda path, config: dict(_PROBE), validator=validator,
        transcriber=_Transcriber(), ocr=_Ocr(), face_detector=_Face(),
    )
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", deps=deps)
    output_dir = Path(result["output_dir"])
    manifest = json.loads((output_dir / "清单.json").read_text(encoding="utf-8"))

    script_stage = manifest["script_replica"]["stage"]
    assert script_stage.get("stage_validation_rejected", 0) >= 1 or manifest["material_replica"]["rejected"]
    rejected = [item for item in manifest["material_replica"]["rejected"] if item.get("stage") == "validation"]
    assert rejected, "the full chain must attribute validation rejections"
    assert (output_dir / "05-过程数据" / "validation.json").is_file()
    # A delivered source video is never one of the rejected ids.
    delivered = {Path(item["file"]).stem for item in manifest.get("downloads") or []}
    assert not (delivered & bad)


# --------------------------------------------------------------------------- #
# 6. Disabled / absent == the pre-change shape (byte-identical promise)
# --------------------------------------------------------------------------- #
def _run_download_only(tmp_path: Path, *, validation_block) -> tuple[dict, Path]:
    config = _config(tmp_path)
    config["jobs"]["material_replication"]["prefilter"] = {"enabled": False}
    config["jobs"]["material_replication"]["download_budget"] = {
        "enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9,
    }
    if validation_block is None:
        config["jobs"]["material_replication"].pop("validation", None)
    else:
        config["jobs"]["material_replication"]["validation"] = validation_block
    rows = [_row(f"v{index:02d}", f"作者{index}") for index in range(3)]
    deps, _ = _deps(rows)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    return result, Path(result["output_dir"])


def test_disabled_and_absent_validation_diff_is_additive_only(tmp_path: Path) -> None:
    """With the layer off, the delivery is behaviourally equivalent: the diff is
    additive only and none of the layer's own artifact surfaces exist."""
    absent_dir = tmp_path / "absent"
    disabled_dir = tmp_path / "disabled"
    absent_dir.mkdir()
    disabled_dir.mkdir()
    _, out_absent = _run_download_only(absent_dir, validation_block=None)
    _, out_disabled = _run_download_only(disabled_dir, validation_block={"enabled": False})

    absent = json.loads((out_absent / "清单.json").read_text(encoding="utf-8"))
    disabled = json.loads((out_disabled / "清单.json").read_text(encoding="utf-8"))
    # No validation surface anywhere.
    for manifest in (absent, disabled):
        assert "validation" not in manifest
        assert not any(key.startswith("validation_") for key in manifest["counters"])
        assert not any(key.startswith("validation_") for key in (manifest["script_replica"].get("stage") or {}))
    assert absent["counters"] == disabled["counters"]
    absent.pop("generated_at"), disabled.pop("generated_at")
    # ``media_path`` is an absolute path under each run's own root; strip it
    # so the comparison is about content, not location.
    for manifest in (absent, disabled):
        for item in manifest.get("downloads") or []:
            item.pop("media_path", None)
    assert absent == disabled

    readme_a = (out_absent / "00-交付说明.md").read_text(encoding="utf-8")
    readme_d = (out_disabled / "00-交付说明.md").read_text(encoding="utf-8")
    assert "## 下载校验" not in readme_a and "## 下载校验" not in readme_d
    assert not (out_absent / "05-过程数据" / "validation.json").exists()
    assert not (out_disabled / "05-过程数据" / "validation.json").exists()
    assert _source_files(out_absent) == _source_files(out_disabled)
    # ``scope`` of the whole process tree is otherwise identical.
    names_a = sorted(p.relative_to(out_absent).as_posix() for p in out_absent.rglob("*"))
    names_d = sorted(p.relative_to(out_disabled).as_posix() for p in out_disabled.rglob("*"))
    assert names_a == names_d


def test_validation_enabled_false_is_a_strict_noop_on_the_execution_path(tmp_path: Path, monkeypatch) -> None:
    """With the block disabled, the validation module is never even consulted."""
    import douyin_intelligence.replication_validation as validation_module

    def _boom(*args, **kwargs):
        raise AssertionError("disabled validation must not run the validator")

    monkeypatch.setattr(validation_module, "validate_downloaded", _boom)
    monkeypatch.setattr(validation_module, "validate_cached", _boom)
    _, out = _run_download_only(tmp_path, validation_block={"enabled": False})
    assert "validation" not in json.loads((out / "清单.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 7. Config validation
# --------------------------------------------------------------------------- #
def _load_with_validation(tmp_path: Path, block):
    base = load_config()
    if block is None:
        base["jobs"]["material_replication"].pop("validation", None)
    else:
        base["jobs"]["material_replication"]["validation"] = block
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    return load_config(path)


def test_shipped_config_ships_validation_enabled() -> None:
    settings = load_config()["jobs"]["material_replication"]["validation"]
    assert {key: settings[key] for key in (
        "enabled", "duration_tolerance", "full_decode",
        "decode_time_budget_seconds", "require_metadata_duration", "cache_attestation",
    )} == {
        "enabled": True,
        "duration_tolerance": 0.05,
        "full_decode": True,
        "decode_time_budget_seconds": 20,
        "require_metadata_duration": False,
        "cache_attestation": True,
    }
    # The block carries an explanatory comment (JSON has no comment syntax).
    assert isinstance(settings.get("_comment"), str) and settings["_comment"]


def test_config_rejects_invalid_tolerance_and_negative_budget(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        _load_with_validation(tmp_path, {"enabled": True, "duration_tolerance": 1.5})
    with pytest.raises(ConfigurationError):
        _load_with_validation(tmp_path, {"enabled": True, "duration_tolerance": -0.1})
    with pytest.raises(ConfigurationError):
        _load_with_validation(tmp_path, {"enabled": True, "decode_time_budget_seconds": -1})


def test_config_absent_or_none_validation_is_accepted(tmp_path: Path) -> None:
    assert _load_with_validation(tmp_path, None)["jobs"]["material_replication"].get("validation") is None
    with pytest.raises(ConfigurationError):
        _load_with_validation(tmp_path, "nope")


# --------------------------------------------------------------------------- #
# 8. face_metrics: truncated sampling is never silent
# --------------------------------------------------------------------------- #
class _DummyImage:
    def __init__(self, width: int = 64, height: int = 64) -> None:
        import numpy as np
        self.shape = (height, width, 3)


def test_face_metrics_flags_truncated_frame_sample(tmp_path: Path, monkeypatch) -> None:
    """Duration implies 58 frames; ffmpeg emits 13 -> explicit ``truncated``."""
    from douyin_intelligence import face_metrics

    config = _config(tmp_path)
    config["jobs"]["material_replication"]["face"]["auto_download"] = False
    detector = face_metrics.FaceDetector(config)
    monkeypatch.setattr(detector, "_load", lambda: True)
    detector._backend = face_metrics.BACKEND_YUNET

    def fake_process(command):
        pattern = Path(command[-1])
        pattern.parent.mkdir(parents=True, exist_ok=True)
        for index in range(1, 14):  # 13 frames, not 58
            (pattern.parent / f"frame-{index:04d}.jpg").write_bytes(b"jpeg")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(face_metrics, "_run_media_process", fake_process)
    monkeypatch.setattr(face_metrics, "imread_unicode", lambda path, flag=None: _DummyImage())
    monkeypatch.setattr(detector, "detect_frame", lambda image: [])

    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    result = detector.run(video, 58.0, tmp_path / "cache", tmp_path / "temp")
    assert result["expected_frames"] == 58
    assert result["sampled_frames"] == 13
    assert result["truncated"] is True
    assert result["emitted_frames"] == 13
    assert "58" in result["warning"] and "13" in result["warning"]


def test_face_metrics_complete_sample_is_not_flagged(tmp_path: Path, monkeypatch) -> None:
    from douyin_intelligence import face_metrics

    config = _config(tmp_path)
    config["jobs"]["material_replication"]["face"]["auto_download"] = False
    detector = face_metrics.FaceDetector(config)
    monkeypatch.setattr(detector, "_load", lambda: True)
    detector._backend = face_metrics.BACKEND_YUNET

    def fake_process(command):
        pattern = Path(command[-1])
        pattern.parent.mkdir(parents=True, exist_ok=True)
        for index in range(1, 59):
            (pattern.parent / f"frame-{index:04d}.jpg").write_bytes(b"jpeg")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(face_metrics, "_run_media_process", fake_process)
    monkeypatch.setattr(face_metrics, "imread_unicode", lambda path, flag=None: _DummyImage())
    monkeypatch.setattr(detector, "detect_frame", lambda image: [])

    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    result = detector.run(video, 58.0, tmp_path / "cache", tmp_path / "temp")
    assert result["expected_frames"] == 58
    assert result["sampled_frames"] == 58
    assert result["truncated"] is False
    assert "warning" not in result


# --------------------------------------------------------------------------- #
# 9. Real ffmpeg/ffprobe smoke test on an actually-corrupt file
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not media_tool_available(load_config(), "ffmpeg"), reason="需要本机 ffmpeg")
def test_real_ffmpeg_detects_a_truncated_file(tmp_path: Path) -> None:
    """Encode a tiny clip, truncate it, then let the REAL decoder judge it."""
    config = _validation_enabled_config(tmp_path)
    ffmpeg = resolve_media_tool(config, "ffmpeg")
    good = tmp_path / "good.mp4"
    built = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=4:size=128x128:rate=25",
         "-pix_fmt", "yuv420p", str(good)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if built.returncode != 0 or not good.is_file():
        pytest.skip("本机 ffmpeg 无法生成测试片段")

    valid = validate_downloaded(good, config, metadata_duration=4.0)
    assert valid["conclusion"] == CONCLUSION_OK, valid

    broken = tmp_path / "broken.mp4"
    payload = good.read_bytes()
    broken.write_bytes(payload[: max(4096, len(payload) * 3 // 5)])
    record = validate_downloaded(broken, config, metadata_duration=4.0)
    assert record["passed"] is False
    assert record["conclusion"] in {CONCLUSION_UNDECODABLE, CONCLUSION_SHORT_DECODE, CONCLUSION_PROBE_FAILED}


# --------------------------------------------------------------------------- #
# 10. BLOCKER regression: the sampled fallback must run under REAL ffmpeg
# --------------------------------------------------------------------------- #
def _build_clip(directory: Path, config: dict, *, seconds: float = 4.0, rate: int = 25) -> Path:
    """Encode a tiny synthetic clip with the real ffmpeg (or skip the test)."""
    ffmpeg = resolve_media_tool(config, "ffmpeg")
    target = directory / "clip.mp4"
    built = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-f", "lavfi",
         "-i", f"testsrc=duration={seconds}:size=128x128:rate={rate}",
         "-pix_fmt", "yuv420p", str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if built.returncode != 0 or not target.is_file():
        pytest.skip("本机 ffmpeg 无法生成测试片段")
    return target


@pytest.mark.skipif(not media_tool_available(load_config(), "ffmpeg"), reason="需要本机 ffmpeg")
def test_real_ffmpeg_sampled_fallback_exits_zero(tmp_path: Path) -> None:
    """``full=False`` must place ``-frames:v`` AFTER ``-i`` and be accepted.

    This is the regression guard for the blocker: the sampled fallback command
    used to put ``-frames:v 3`` before ``-i``, which every real ffmpeg rejects,
    so a slow-but-good file was judged ``undecodable`` instead of ``degraded``.
    """
    config = _validation_enabled_config(tmp_path)
    clip = _build_clip(tmp_path, config)
    outcome = default_decoder(clip, config, time_budget=20.0, full=False)
    assert outcome["kind"] == "ok", outcome
    assert (outcome["decoded_frames"] or 0) >= 1


@pytest.mark.skipif(not media_tool_available(load_config(), "ffmpeg"), reason="需要本机 ffmpeg")
def test_real_ffmpeg_zero_budget_degrades_to_sampled_decode(tmp_path: Path) -> None:
    """``decode_time_budget_seconds = 0`` skips the full decode and degrades."""
    config = _validation_enabled_config(tmp_path)
    config["jobs"]["material_replication"]["validation"]["decode_time_budget_seconds"] = 0
    clip = _build_clip(tmp_path, config)
    record = validate_downloaded(clip, config, metadata_duration=4.0)
    assert record["conclusion"] == CONCLUSION_DEGRADED, record
    assert record["passed"] is True
    assert record["decode_mode"] == "degraded_sample"
    assert record["decode_degraded_reason"] == "budget_zero"
    assert (record["decoded_frames"] or 0) >= 1


@pytest.mark.skipif(not media_tool_available(load_config(), "ffmpeg"), reason="需要本机 ffmpeg")
def test_real_ffmpeg_reports_coverage_and_decoded_frames(tmp_path: Path) -> None:
    """A real full decode yields a real coverage ratio and frame count."""
    config = _validation_enabled_config(tmp_path)
    clip = _build_clip(tmp_path, config, seconds=4.0, rate=25)
    record = validate_downloaded(clip, config, metadata_duration=4.0)
    assert record["conclusion"] == CONCLUSION_OK, record
    assert record["coverage"] is not None and record["coverage"] >= 0.9
    assert record["coverage_checked"] is True
    assert (record["decoded_frames"] or 0) > 0
    assert record["last_decoded_seconds"] is not None


# --------------------------------------------------------------------------- #
# 11. ② decode budget 0 / ③ coverage-based frame criteria
# --------------------------------------------------------------------------- #
def test_zero_decode_budget_skips_the_full_decode(tmp_path: Path) -> None:
    config = _validation_enabled_config(tmp_path)
    config["jobs"]["material_replication"]["validation"]["decode_time_budget_seconds"] = 0
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    calls: list[bool] = []

    def decoder(path, config, *, time_budget, full):
        calls.append(full)
        return {"kind": "ok", "decoded_frames": 3, "error_lines": 0, "first_error": ""}

    record = validate_downloaded(video, config, metadata_duration=60.0, probe=dict(_PROBE), decoder=decoder)
    assert calls == [False], "a 0 budget must skip the full decode entirely"
    assert record["conclusion"] == CONCLUSION_DEGRADED
    assert record["decode_degraded_reason"] == "budget_zero"
    assert record["passed"] is True


def test_twenty_percent_frame_loss_is_detected(tmp_path: Path) -> None:
    """The old ``< 0.5 × expected`` ratio let a 20% loss through; it must not."""
    config = _validation_enabled_config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    record = validate_downloaded(
        video, config, metadata_duration=60.0, probe=dict(_PROBE),
        decoder=_ok_decoder(1600), stream_prober=lambda path, config: {"nb_frames": 2000, "fps": 30.0},
    )
    assert record["expected_frames"] == 2000
    assert record["decoded_frames"] == 1600
    assert record["conclusion"] == CONCLUSION_SHORT_DECODE


def test_static_low_fps_sample_is_not_misjudged(tmp_path: Path) -> None:
    """A static / very-low-fps clip with no ``nb_frames`` must not be short_decode."""
    config = _validation_enabled_config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    record = validate_downloaded(
        video, config, metadata_duration=10.0,
        probe={"duration_seconds": 10.0, "width": 1080, "height": 1920},
        decoder=_ok_decoder(1),
        stream_prober=lambda path, config: {"nb_frames": None, "fps": 25.0},
    )
    assert record["conclusion"] != CONCLUSION_SHORT_DECODE
    assert record["conclusion"] == CONCLUSION_UNKNOWN_COVERAGE
    assert record["passed"] is True


def test_unknown_coverage_never_silently_records_ok(tmp_path: Path) -> None:
    config = _validation_enabled_config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    record = validate_downloaded(
        video, config, metadata_duration=60.0, probe=dict(_PROBE),
        decoder=_ok_decoder(5),
        stream_prober=lambda path, config: {"nb_frames": None, "fps": None},
    )
    assert record["conclusion"] == CONCLUSION_UNKNOWN_COVERAGE
    assert record["coverage_checked"] is False
    assert record["coverage"] is None
    assert record["coverage_note"], "the record must state the check was not performed"


def test_coverage_shortfall_is_short_decode(tmp_path: Path) -> None:
    config = _validation_enabled_config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")

    def decoder(path, config, *, time_budget, full):
        return {"kind": "ok", "decoded_frames": 300, "last_pts_seconds": 2.0, "error_lines": 0, "first_error": ""}

    record = validate_downloaded(
        video, config, metadata_duration=60.0,
        probe={"duration_seconds": 60.0, "width": 1080, "height": 1920},
        decoder=decoder, stream_prober=lambda path, config: {"nb_frames": None, "fps": 30.0},
    )
    assert record["conclusion"] == CONCLUSION_SHORT_DECODE


def test_severe_face_sample_truncation_downgrades_the_class() -> None:
    face = {
        "face_class": "face_free", "sampled_frames": 13,
        "expected_frames": 60, "emitted_frames": 13, "truncated": True,
    }
    updated = truncated_face_class(face)
    assert updated["face_class"] == "unavailable"
    assert updated["face_class_reason"] == "sample_truncated"
    assert updated["low_confidence"] is True
    # A mild truncation keeps its class but is flagged.
    mild = {"face_class": "face_free", "sampled_frames": 50, "expected_frames": 60,
            "emitted_frames": 50, "truncated": True}
    assert truncated_face_class(mild) is mild


# --------------------------------------------------------------------------- #
# 12. ⑦ cache fingerprint: identical size+mtime, different content
# --------------------------------------------------------------------------- #
def test_cache_content_swap_with_same_size_and_mtime_is_revalidated(tmp_path: Path) -> None:
    config = _validation_enabled_config(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"G" * 5000)
    calls: list[int] = []

    def decoder(path, config, *, time_budget, full):
        calls.append(1)
        return {"kind": "ok", "decoded_frames": 1800, "error_lines": 0, "first_error": ""}

    validate_cached(video, config, probe=dict(_PROBE), decoder=decoder,
                    stream_prober=lambda path, config: {"nb_frames": 1800, "fps": 30.0})
    assert len(calls) == 1
    stamp = video.stat()
    video.write_bytes(b"B" * 5000)  # different content, same length
    os.utime(video, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))  # ...and same mtime
    calls.clear()
    record = validate_cached(video, config, probe=dict(_PROBE), decoder=decoder,
                             stream_prober=lambda path, config: {"nb_frames": 1800, "fps": 30.0})
    assert calls, "a content change with identical size+mtime must be re-validated"
    assert record["cache_attestation"] == "miss"


# --------------------------------------------------------------------------- #
# 13. ⑤ duration unit normalization (nested keys + ms)
# --------------------------------------------------------------------------- #
def test_row_duration_understands_nested_and_millisecond_values() -> None:
    assert _row_duration_detail({"duration": 60.0}) == (60.0, "duration")
    assert _row_duration_detail({"video": {"duration": 60000}}) == (60.0, "video.duration_ms")
    assert _row_duration_detail({"video": {"duration": 8000}}) == (8.0, "video.duration_ms")
    assert _row_duration_detail({"duration_ms": 60000}) == (60.0, "duration_ms")
    # The strict boundary: exactly 10000 stays seconds, 10001 is normalized.
    assert _row_duration_detail({"duration": 10000}) == (10000.0, "duration")
    assert _row_duration_detail({"duration": 10001}) == (10.001, "duration_ms")
    assert _row_duration_detail({}) == (0.0, "")


# --------------------------------------------------------------------------- #
# 14. ⑤ post-download duration window (metadata missing)
# --------------------------------------------------------------------------- #
def _row_without_duration(video_id: str, author: str) -> dict:
    return {
        "aweme_id": video_id,
        "desc": f"标题-{video_id}",
        "author": {"uid": f"uid-{author}", "nickname": author},
        "create_time": "2026-09-11T08:00:00+08:00",
        "statistics": {"digg_count": 100, "comment_count": 10, "share_count": 5, "collect_count": 20},
        "video_download_url": f"https://signed.example/{video_id}",
        "share_url": f"https://www.douyin.com/video/{video_id}",
    }


def test_measured_duration_window_rejects_after_download(tmp_path: Path) -> None:
    config = _validation_enabled_config(tmp_path)
    config["jobs"]["material_replication"]["prefilter"] = {
        "enabled": True, "min_seconds": 10, "max_seconds": 300,
        "heat_gate_percentile": 0.0, "allow_unknown_duration": True,
    }
    config["jobs"]["material_replication"]["download_budget"] = {
        "enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9,
    }
    rows = [_row_without_duration("v00", "作者A"), _row_without_duration("v01", "作者B")]

    def downloader(url, destination, config, *, max_bytes=None):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"x" * 2048)

    def prober(path, config):
        return {"duration_seconds": 5.0, "width": 1080, "height": 1920}  # below the 10s floor

    deps = ReplicationDeps(
        collector=_collector(rows), downloader=downloader, prober=prober,
        validator=_make_validator(decoder=_ok_decoder(150),
                                  stream_prober=lambda path, config: {"nb_frames": 150, "fps": 30.0}),
    )
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    output_dir = Path(result["output_dir"])
    assert result["downloads"] == []
    assert _source_files(output_dir) == []
    stages = {item["stage"] for item in result["failures"]}
    assert "duration_post" in stages
    block = json.loads((output_dir / "05-过程数据" / "download_budget.json").read_text(encoding="utf-8"))
    assert block["used"]["count"] == 0


# --------------------------------------------------------------------------- #
# 15. ④ face truncation reaches the delivery; ⑧ by_stage; ⑤ duration skip note
# --------------------------------------------------------------------------- #
class _TruncatingFace:
    backend = "opencv_yunet"

    def __init__(self, *, expected: int, emitted: int) -> None:
        self._expected = expected
        self._emitted = emitted

    def status(self):
        return {"backend": "opencv_yunet", "status": "ok", "model_present": True}

    def run(self, video, duration, cache_dir, temp_dir):
        return {
            "backend": "opencv_yunet", "status": "ok", "face_frame_ratio": 0.0,
            "max_face_area_ratio": 0.0, "face_class": "face_free",
            "sampled_frames": self._emitted, "expected_frames": self._expected,
            "emitted_frames": self._emitted, "sample_coverage": self._emitted / self._expected,
            "truncated": True, "low_confidence": True,
            "face_per_frame": [False] * self._emitted, "sample_interval_seconds": 1,
        }


def _run_full_chain(tmp_path: Path, monkeypatch, *, face, rows) -> Path:
    config = _validation_enabled_config(tmp_path)
    monkeypatch.setattr("douyin_intelligence.replication_pipeline.media_tool_available", lambda config, name: True)
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: __import__(
            "douyin_intelligence.replication_selection", fromlist=["VisualMetrics"]
        ).VisualMetrics(sampled_frames=10, motion_frame_ratio=0.9, ocr_text_frame_ratio=0.0, visual_ok=True),
    )

    def downloader(url, destination, config, *, max_bytes=None):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"x" * 2048)

    deps = ReplicationDeps(
        collector=_collector(rows), downloader=downloader,
        prober=lambda path, config: dict(_PROBE),
        validator=_make_validator(decoder=_ok_decoder(1800),
                                  stream_prober=lambda path, config: {"nb_frames": 1800, "fps": 30.0}),
        transcriber=_Transcriber(), ocr=_Ocr(), face_detector=face,
    )
    result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", deps=deps)
    return Path(result["output_dir"])


def test_severely_truncated_face_sample_is_listed_and_blocks_selection(tmp_path: Path, monkeypatch) -> None:
    rows = [_row(f"v{index:02d}", f"作者{index}") for index in range(4)]
    output_dir = _run_full_chain(
        tmp_path, monkeypatch, face=_TruncatingFace(expected=60, emitted=13), rows=rows,
    )
    manifest = json.loads((output_dir / "清单.json").read_text(encoding="utf-8"))
    # A 13/60 sample must not be trusted as face_free -> downgraded and rejected.
    assert manifest["material_replica_sources"] == []
    truncated = manifest["face_truncated_samples"]
    assert {item["video_id"] for item in truncated} == {"v00", "v01", "v02", "v03"}
    assert all(item["expected_frames"] == 60 and item["emitted_frames"] == 13 for item in truncated)
    # Field parity with ``material_replica_sources``: the truncation flag is
    # present on every aggregated entry (not only on the per-source records).
    assert all(item.get("truncated") is True for item in truncated)
    readme = (output_dir / "00-交付说明.md").read_text(encoding="utf-8")
    assert "## 人脸样本截断" in readme
    assert "预期 60 帧 / 实际 13 帧" in readme


def test_manifest_validation_has_consistent_by_stage(tmp_path: Path, monkeypatch) -> None:
    rows = [_row(f"v{index:02d}", f"作者{index}") for index in range(4)]
    output_dir = _run_full_chain(
        tmp_path, monkeypatch,
        face=_Face(), rows=rows,
    )
    manifest = json.loads((output_dir / "清单.json").read_text(encoding="utf-8"))
    block = manifest["validation"]
    counts = block["counts"]
    # by_stage is exposed on the block and mirrors the per-stage sums.
    assert block["by_stage"] == counts["by_stage"]
    assert counts["by_stage"], "the full chain must attribute script/material validations"
    for value in counts["by_stage"].values():
        assert value["validated"] == value["passed"] + value["rejected"]
    assert sum(v["validated"] for v in counts["by_stage"].values()) == counts["validated"]


def test_readme_states_duration_compare_was_skipped_without_metadata(tmp_path: Path) -> None:
    config = _validation_enabled_config(tmp_path)
    config["jobs"]["material_replication"]["download_budget"] = {
        "enabled": True, "max_count": 12, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9,
    }
    rows = [_row_without_duration("v00", "作者A")]
    deps, _ = _deps(rows)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    readme = (Path(result["output_dir"]) / "00-交付说明.md").read_text(encoding="utf-8")
    assert "时长比对已跳过（元数据无时长）" in readme
