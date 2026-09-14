"""T6 material-layer gates: the freshness window and cross-run de-duplication.

Both gates sit in the material selection chain *before* a byte is downloaded, and
both ship **off** -- the switch absent (or ``max_age_days: 0``) must leave the
chain byte-for-byte what it was, which is the property the first two tests below
pin.

Covered here:
* key absent vs ``max_age_days: 0`` -> identical selection, no new keys anywhere;
* an over-age candidate is refused as ``stage="stale"`` with **zero** downloads;
* an *undated* candidate is never refused (a missing date is a gap, not evidence);
* ``material_freshness`` reports the pool's before/after age medians;
* an already-delivered id is refused as ``stage="cross_run_duplicate"``;
* with the dedup switch off an existing index is ignored entirely;
* every spelling of "off" is off, read from a *synthetic* config.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from douyin_intelligence.config import load_config
from douyin_intelligence.replication_candidates import Candidate
from douyin_intelligence.replication_pipeline import ReplicationDeps
from douyin_intelligence.replication_selection import (
    VisualMetrics,
    material_max_age_days,
    published_age_days,
    select_material_replicas,
)

# A pinned "today" so the ages below are exact instead of wall-clock dependent.
_ZONE = ZoneInfo("Asia/Shanghai")
_NOW = datetime(2026, 9, 14, 9, 0, tzinfo=_ZONE)


def _at(days_ago: float) -> str:
    return datetime.fromtimestamp(_NOW.timestamp() - days_ago * 86400, tz=_ZONE).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Helpers (mirroring tests/test_replication_material_quota.py)
# --------------------------------------------------------------------------- #
def _candidate(video_id: str, *, heat: float, published_at: str = "", duration: float = 60.0) -> Candidate:
    return Candidate(
        video_id=video_id,
        title=f"标题-{video_id}",
        author=f"作者-{video_id}",
        duration_seconds=duration,
        heat_score=heat,
        published_at=published_at,
    )


def _config(tmp_path: Path, *, window: int | None = None, meta: dict | None = None) -> dict:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    config["_now"] = _NOW
    # The fakes below write non-media bytes, so the real ffprobe/ffmpeg check
    # does not apply.
    settings = config["jobs"]["material_replication"]
    settings["validation"] = {"enabled": False}
    settings.pop("dedup_across_runs", None)
    block = settings["material_replica"]
    # Clear the shipped delivered-bytes quota too: with a floor set, the loop
    # legitimately scans *past* ``target_count`` and these tests would then be
    # measuring the quota instead of the gate under test.
    for key in ("min_delivered_bytes", "max_delivered_bytes", "max_selected_count", "max_age_days"):
        block.pop(key, None)
    block.update({"target_count": 4, "min_count": 1})
    if window is not None:
        block["max_age_days"] = window
    if meta:
        settings.update(meta)
    return config


class _Ocr:
    def run(self, video, duration, cache_dir, temp_dir):
        return {"status": "no_text", "items": [], "sampled_frames": 10}


class _Transcriber:
    def run(self, video, cache_dir, temp_dir, **kwargs):
        return {"status": "no_speech", "text": "", "segments": []}


class _Face:
    backend = "opencv_yunet"

    def status(self):
        return {"backend": "opencv_yunet", "status": "ok", "model_present": True}

    def run(self, video, duration, cache_dir, temp_dir):
        return {
            "backend": "opencv_yunet", "status": "ok", "face_frame_ratio": 0.0, "max_face_area_ratio": 0.0,
            "face_class": "face_free", "sampled_frames": 10, "face_per_frame": [False] * 10,
            "sample_interval_seconds": 1,
        }


def _visual_ok(*args, **kwargs):
    return VisualMetrics(sampled_frames=10, motion_frame_ratio=0.9, ocr_text_frame_ratio=0.0, visual_ok=True)


def _deps(sizes: dict[str, int]) -> ReplicationDeps:
    def downloader(url, destination, config, *, max_bytes=None):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"x" * sizes[Path(destination).stem])

    def prober(path, config):
        return {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}

    deps = ReplicationDeps(downloader=downloader, prober=prober)
    deps.transcriber = _Transcriber()
    deps.ocr = _Ocr()
    deps.face_detector = _Face()
    return deps


def _select(root: Path, candidates: list[Candidate], *, window: int | None = None, meta: dict | None = None) -> dict:
    sizes = {candidate.video_id: 1000 for candidate in candidates}
    return select_material_replicas(
        _config(root, window=window, meta=meta), candidates, deps=_deps(sizes), validation_store=[],
    )


def _shape(result: dict) -> dict:
    """Everything about a result that must not depend on the tmp path."""
    return {
        "keys": sorted(result),
        "selected": [item["candidate"].video_id for item in result["selected"]],
        "unmet": sorted((entry["video_id"], entry["stage"]) for entry in result["unmet"]),
        "counters": result["counters"],
        "stage": result["stage"],
    }


def _four(monkeypatch, tmp_path: Path, *, window: int | None = None, meta: dict | None = None) -> dict:
    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics", _visual_ok)
    candidates = [
        # v9 is the *hottest*, so it is judged before ``target_count`` is reached
        # -- otherwise the loop would break on quota and never look at it.
        _candidate("v9", heat=110.0, published_at=_at(90)),
        _candidate("v0", heat=100.0, published_at=_at(1)),
        _candidate("v1", heat=90.0, published_at=_at(2)),
        _candidate("v2", heat=80.0, published_at=_at(3)),
    ]
    return _select(tmp_path, candidates, window=window, meta=meta)


# --------------------------------------------------------------------------- #
# 1. Off by default
# --------------------------------------------------------------------------- #
def test_max_age_days_absent_is_byte_identical(tmp_path: Path, monkeypatch) -> None:
    """The guard case: every default spelling of "off" changes nothing."""
    absent = _four(monkeypatch, tmp_path / "absent")
    zero = _four(monkeypatch, tmp_path / "zero", window=0)

    assert _shape(absent) == _shape(zero)
    assert "freshness" not in absent
    assert "material_freshness" not in absent["stage"]
    assert "rejected_stale" not in absent["counters"]
    assert "rejected_cross_run" not in absent["counters"]
    assert {entry["stage"] for entry in absent["unmet"]} & {"stale", "cross_run_duplicate"} == set()
    # Nothing was refused: the pre-change selection is intact.
    assert [item["candidate"].video_id for item in absent["selected"]] == ["v9", "v0", "v1", "v2"]
    assert absent["counters"]["downloaded"] == 4


def test_dedup_across_runs_absent_ignores_an_existing_index(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics", _visual_ok)
    config = _config(tmp_path)
    # An index naming v1 as delivered must be ignored while the switch is absent.
    (tmp_path / "data/cache/material-replication").mkdir(parents=True)
    (tmp_path / "data/cache/material-replication/delivered_index.json").write_text(
        '{"v1": {"theme": "旧期", "delivered_at": "2026-09-01T09:00:00+08:00"}}', encoding="utf-8",
    )
    candidates = [_candidate(f"v{i}", heat=100.0 - i, published_at=_at(1)) for i in range(4)]
    result = select_material_replicas(
        config, candidates, deps=_deps({c.video_id: 1000 for c in candidates}), validation_store=[],
    )

    assert [item["candidate"].video_id for item in result["selected"]] == ["v0", "v1", "v2", "v3"]
    assert "rejected_cross_run" not in result["counters"]
    assert {entry["stage"] for entry in result["unmet"]} & {"cross_run_duplicate"} == set()


# --------------------------------------------------------------------------- #
# 2. The freshness window
# --------------------------------------------------------------------------- #
def test_stale_candidate_is_dropped_before_any_download(tmp_path: Path, monkeypatch) -> None:
    result = _four(monkeypatch, tmp_path, window=30)

    assert [item["candidate"].video_id for item in result["selected"]] == ["v0", "v1", "v2"]
    stale = [entry for entry in result["unmet"] if entry["stage"] == "stale"]
    assert [entry["video_id"] for entry in stale] == ["v9"]
    assert "超出时效窗口 30 天" in stale[0]["reason"]
    assert result["counters"]["rejected_stale"] == 1
    # The refused candidate never cost a byte: only the three kept clips downloaded.
    assert result["counters"]["downloaded"] == 3


def test_material_freshness_reports_before_and_after_medians(tmp_path: Path, monkeypatch) -> None:
    result = _four(monkeypatch, tmp_path, window=30)
    freshness = result["freshness"]

    assert freshness == {
        "max_age_days": 30,
        "judged": 4,
        "rejected": 1,
        "undated": 0,
        "stale_ratio": 0.25,
        "oldest_age_days": 90.0,
        "median_age_days_before": 2.5,
        "median_age_days_after": 2.0,
    }
    assert result["stage"]["material_freshness"] == freshness


def test_undated_candidate_is_never_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics", _visual_ok)
    candidates = [
        _candidate("v0", heat=100.0, published_at=_at(1)),
        _candidate("v1", heat=90.0, published_at=""),
        _candidate("v2", heat=80.0, published_at=_at(90)),
    ]
    result = _select(tmp_path, candidates, window=30)

    assert [item["candidate"].video_id for item in result["selected"]] == ["v0", "v1"]
    assert result["freshness"]["undated"] == 1
    assert result["freshness"]["judged"] == 2
    assert [entry["video_id"] for entry in result["unmet"] if entry["stage"] == "stale"] == ["v2"]


# --------------------------------------------------------------------------- #
# 3. Cross-run de-duplication
# --------------------------------------------------------------------------- #
def test_already_delivered_candidate_is_skipped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("douyin_intelligence.replication_selection.compute_visual_metrics", _visual_ok)
    config = _config(tmp_path, meta={"dedup_across_runs": True})
    cache = tmp_path / "data/cache/material-replication"
    cache.mkdir(parents=True)
    (cache / "delivered_index.json").write_text(
        '{"v1": {"theme": "上一期", "delivered_at": "2026-09-07T09:00:00+08:00", "title": "标题-v1"}}',
        encoding="utf-8",
    )
    candidates = [_candidate(f"v{i}", heat=100.0 - i, published_at=_at(1)) for i in range(4)]
    result = select_material_replicas(
        config, candidates, deps=_deps({c.video_id: 1000 for c in candidates}), validation_store=[],
    )

    assert [item["candidate"].video_id for item in result["selected"]] == ["v0", "v2", "v3"]
    duplicates = [entry for entry in result["unmet"] if entry["stage"] == "cross_run_duplicate"]
    assert [entry["video_id"] for entry in duplicates] == ["v1"]
    assert "跨期重复" in duplicates[0]["reason"] and "上一期" in duplicates[0]["reason"]
    assert result["counters"]["rejected_cross_run"] == 1
    # v1 was refused before its download, so only three clips hit the wire.
    assert result["counters"]["downloaded"] == 3


def test_selection_only_reads_the_index(tmp_path: Path, monkeypatch) -> None:
    """Selection must never *record* a delivery -- that belongs to the pipeline.

    A period that selects candidates and then fails before writing any file must
    leave the index untouched, or the next period would skip clips that were
    never delivered.
    """
    _four(monkeypatch, tmp_path)

    assert not (tmp_path / "data/cache/material-replication/delivered_index.json").exists()


# --------------------------------------------------------------------------- #
# 4. Config reading
# --------------------------------------------------------------------------- #
def test_max_age_days_reads_the_switch_from_a_synthetic_config() -> None:
    """Every spelling of "off" is off, and a set value is read as written.

    Deliberately a *synthetic* mapping instead of ``load_config()``.  Asserting
    what production enables only passes while the switch happens to be off, so
    the case flips red the day an operator turns the window on -- and worse, it
    would have been a tautology all along, because the conftest seam strips
    these keys out of every ``load_config()`` payload a test ever sees.
    Whether production enables the window is an operational decision measured by
    its own acceptance criteria, never a unit-test invariant.
    """
    for off in ({}, {"max_age_days": 0}, {"max_age_days": None}, {"max_age_days": ""}):
        assert material_max_age_days(off) == 0
    assert material_max_age_days({"max_age_days": 90}) == 90
    assert material_max_age_days({"max_age_days": "90"}) == 90


@pytest.mark.parametrize("value", [-1, "一周"])
def test_max_age_days_rejects_nonsense(value: object) -> None:
    with pytest.raises(ValueError):
        material_max_age_days({"max_age_days": value})


def test_published_age_days_is_none_for_a_missing_or_bad_date() -> None:
    assert published_age_days(_candidate("v", heat=1.0), _NOW, _ZONE) is None
    assert published_age_days(_candidate("v", heat=1.0, published_at="不是日期"), _NOW, _ZONE) is None
    assert published_age_days(_candidate("v", heat=1.0, published_at=_at(2)), _NOW, _ZONE) == pytest.approx(2.0)
    # A naive timestamp is read as wall time in the configured zone.
    naive = _candidate("v", heat=1.0, published_at="2026-09-13T09:00:00")
    assert published_age_days(naive, _NOW, _ZONE) == pytest.approx(1.0)
