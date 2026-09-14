"""Cross-run material de-duplication: the ``delivered_index.json`` contract.

The 9.14 batch shipped the same ``video_id`` in more than one delivery, which the
acceptance list forbids (「跨期重复的 video_id 数量 = 0」).  The index is a *cache*,
so every failure mode of it must degrade to "nothing delivered yet" instead of
aborting a period.

Covered here:
* absent switch -> de-duplication off, no file created, an existing index ignored;
* switch on -> a previously delivered id is refused with a human-readable reason;
* the path follows ``cache_root`` and the injected ``_project_root``;
* missing / malformed / wrongly-shaped / undecodable indexes all degrade to empty;
* a re-delivered clip *overwrites* its earlier record (last delivery wins);
* a blank ``video_id`` is never recorded and never matches;
* a successful delivery persists and reloads, leaving no temp file behind.
"""

from __future__ import annotations

import json
from pathlib import Path

from douyin_intelligence.replication_candidates import Candidate
from douyin_intelligence.replication_dedup import (
    DELIVERED_INDEX_NAME,
    cross_run_duplicate_reason,
    dedup_enabled,
    delivered_entry,
    delivered_index_path,
    delivered_video_ids,
    duplicate_reason,
    load_delivered_index,
    record_delivered,
    remember_delivered,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _config(tmp_path: Path, *, enabled: bool | None = None, cache_root: str | None = None) -> dict:
    """A minimal config; ``enabled=None`` leaves the switch key absent."""
    settings: dict = {}
    if enabled is not None:
        settings["dedup_across_runs"] = enabled
    if cache_root is not None:
        settings["cache_root"] = cache_root
    return {"jobs": {"material_replication": settings}, "_project_root": str(tmp_path)}


def _candidate(video_id: str) -> Candidate:
    return Candidate(video_id=video_id, title="标题", author="作者", published_at="2026-09-10T10:00:00+08:00")


def _record(video_id: str, **extra: object) -> dict:
    return {"video_id": video_id, "author": "作者", "title": f"标题-{video_id}", "bytes": 1024, **extra}


# --------------------------------------------------------------------------- #
# 1. The switch
# --------------------------------------------------------------------------- #
def test_absent_switch_means_off_and_touches_no_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert dedup_enabled(config) is False

    # An index that *does* mention the candidate must be ignored while off.
    index = {"v1": {"theme": "旧期", "delivered_at": "2026-09-01T00:00:00+08:00"}}
    assert cross_run_duplicate_reason(config, index, _candidate("v1")) == ""

    assert remember_delivered(config, [_record("v1")], theme="本期", delivered_at="2026-09-14T09:00:00+08:00") == 0
    assert not delivered_index_path(config).exists()


def test_switch_on_refuses_an_already_delivered_id(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True)
    assert dedup_enabled(config) is True

    index: dict = {}
    record_delivered(index, [_record("v1")], theme="充电宝", delivered_at="2026-09-01T09:00:00+08:00")

    reason = cross_run_duplicate_reason(config, index, _candidate("v1"))
    assert "跨期重复" in reason
    assert "充电宝" in reason and "2026-09-01" in reason
    assert cross_run_duplicate_reason(config, index, _candidate("v2")) == ""


def test_reason_survives_a_record_without_provenance() -> None:
    assert "更早的一期" in duplicate_reason({})


# --------------------------------------------------------------------------- #
# 2. Where the index lives
# --------------------------------------------------------------------------- #
def test_index_path_follows_cache_root_and_project_root(tmp_path: Path) -> None:
    default = delivered_index_path(_config(tmp_path))
    assert default == tmp_path / "data/cache/material-replication" / DELIVERED_INDEX_NAME

    custom = delivered_index_path(_config(tmp_path, cache_root="data/cache/other"))
    assert custom == tmp_path / "data/cache/other" / DELIVERED_INDEX_NAME


# --------------------------------------------------------------------------- #
# 3. Every failure mode degrades to an empty index
# --------------------------------------------------------------------------- #
def test_missing_and_corrupt_indexes_degrade_to_empty(tmp_path: Path) -> None:
    path = tmp_path / DELIVERED_INDEX_NAME
    assert load_delivered_index(path) == {}

    path.write_text("{not json", encoding="utf-8")
    assert load_delivered_index(path) == {}

    path.write_text(json.dumps(["a", "b"]), encoding="utf-8")
    assert load_delivered_index(path) == {}

    # A top-level mapping whose values are not mappings is not an index.
    path.write_text(json.dumps({"v1": "delivered"}), encoding="utf-8")
    assert load_delivered_index(path) == {}

    # Undecodable bytes raise UnicodeDecodeError (a ValueError) inside the reader.
    path.write_bytes(b"\xff\xfe\x00\x00")
    assert load_delivered_index(path) == {}


def test_partially_shaped_index_keeps_only_usable_entries(tmp_path: Path) -> None:
    path = tmp_path / DELIVERED_INDEX_NAME
    path.write_text(json.dumps({"v1": {"author": "甲"}, "v2": 7}), encoding="utf-8")
    assert load_delivered_index(path) == {"v1": {"author": "甲"}}


# --------------------------------------------------------------------------- #
# 4. Recording
# --------------------------------------------------------------------------- #
def test_delivered_record_is_overwritten_by_the_later_delivery(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True)
    assert remember_delivered(config, [_record("v1")], theme="第一期", delivered_at="2026-09-01T09:00:00+08:00") == 1
    assert remember_delivered(config, [_record("v1")], theme="第二期", delivered_at="2026-09-08T09:00:00+08:00") == 1

    index = load_delivered_index(delivered_index_path(config))
    assert set(index) == {"v1"}
    assert index["v1"]["theme"] == "第二期"
    assert index["v1"]["delivered_at"] == "2026-09-08T09:00:00+08:00"


def test_recorded_entry_carries_the_delivery_provenance() -> None:
    index: dict = {}
    record_delivered(
        index,
        [{
            "video_id": "v1", "author": "作者", "title": "标题",
            "bytes": "2048", "published_at": "2026-09-10T10:00:00+08:00",
        }],
        theme="充电宝", delivered_at="2026-09-14T09:00:00+08:00",
    )
    assert index["v1"] == {
        "author": "作者",
        "title": "标题",
        "bytes": 2048,
        "published_at": "2026-09-10T10:00:00+08:00",
        "theme": "充电宝",
        "delivered_at": "2026-09-14T09:00:00+08:00",
    }


def test_blank_video_id_is_never_recorded_or_matched(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True)
    assert remember_delivered(config, [_record("")], theme="本期", delivered_at="2026-09-14T09:00:00+08:00") == 0
    assert not delivered_index_path(config).exists()

    index = {"": {"theme": "脏数据"}}
    assert delivered_entry(index, _candidate("")) is None
    assert cross_run_duplicate_reason(config, index, _candidate("")) == ""


def test_remember_writes_once_and_leaves_no_temp_file(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True)
    assert remember_delivered(
        config, [_record("v1"), _record("v2")], theme="本期", delivered_at="2026-09-14T09:00:00+08:00",
    ) == 2

    path = delivered_index_path(config)
    assert json.loads(path.read_text(encoding="utf-8"))["v1"]["theme"] == "本期"
    assert list(path.parent.glob("*.tmp")) == []

    # Re-running with the same records leaves the file (and its keys) stable.
    assert remember_delivered(config, [_record("v1")], theme="本期", delivered_at="2026-09-14T09:00:00+08:00") == 1
    assert set(load_delivered_index(path)) == {"v1", "v2"}


def test_delivered_video_ids_lists_every_key() -> None:
    assert delivered_video_ids({"v1": {}, "v2": {}}) == {"v1", "v2"}
    assert delivered_video_ids({}) == set()
