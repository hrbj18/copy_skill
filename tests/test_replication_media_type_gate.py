"""P-C: image-album / audio posts are dropped *before* download.

Douyin image albums (``aweme_type=68``) carry no video stream -- their
``video_download_url`` is the post's background music -- so they can never be
delivered.  They used to enter the candidate pool anyway, consume a relevance
rank and a download-budget slot, and only surface as a ``not_video`` failure
*after* the loop had already skipped them.  This module pins the new
pre-download media-type gate:

* it **reuses** :func:`is_video_candidate` (no second type table);
* stage ``pre_media_type``;
* order ``exclude -> media_type -> duration -> heat``;
* governed by ``prefilter.enabled`` (unlike ``exclude_terms``), so the
  "switch off + no terms => no layer artifact, field-for-field identical"
  contract is preserved;
* the real 40-candidate recon pool drops exactly its 9 image albums (31 kept).

Everything is offline: ``_project_root`` is redirected to ``tmp_path`` and the
controlled pool lives under ``tests/fixtures/``.
"""

from __future__ import annotations

import json
from pathlib import Path

from douyin_intelligence.config import load_config
from douyin_intelligence.replication_candidates import Candidate
from douyin_intelligence.replication_pipeline import ReplicationDeps, run_material_replication
from douyin_intelligence.replication_selection import (
    _PREFILTER_MEDIA_TYPE_STAGE,
    is_video_candidate,
    prefilter_candidates,
    prefilter_drop_non_video,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "xiaomi_n90_pool.json"
_PROCESS = "05-过程数据"

_ENABLED = {
    "enabled": True, "min_seconds": 10, "max_seconds": 300,
    "heat_gate_percentile": 0.0, "allow_unknown_duration": True, "drop_non_video": True,
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _cand(
    video_id: str, *, aweme_type: str = "0", media_is_audio: bool = False,
    title: str = "", duration: float = 60.0,
) -> Candidate:
    candidate = Candidate(
        video_id=video_id, title=title, duration_seconds=duration,
        aweme_type=aweme_type, media_is_audio=media_is_audio,
    )
    candidate.heat_score = 0.0
    return candidate


def _config(tmp_path: Path, *, prefilter: dict | None) -> dict:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    if prefilter is None:
        config["jobs"]["material_replication"].pop("prefilter", None)
    else:
        config["jobs"]["material_replication"]["prefilter"] = prefilter
    config["jobs"]["material_replication"]["validation"] = {"enabled": False}
    return config


def _row(video_id: str, author: str, *, title: str = "", duration: float = 60.0, digg: int = 100, url: str = "", aweme_type: str | None = None) -> dict:
    row = {
        "aweme_id": video_id,
        "desc": title or f"标题-{video_id}",
        "author": {"uid": f"uid-{author}", "nickname": author},
        "create_time": "2026-09-11T08:00:00+08:00",
        "statistics": {"digg_count": digg, "comment_count": 10, "share_count": 5, "collect_count": 20},
        "duration": duration,
        "share_url": f"https://www.douyin.com/video/{video_id}",
    }
    if url:
        row["video_download_url"] = url
    if aweme_type is not None:
        row["aweme_type"] = aweme_type
    return row


def _deps(rows: list[dict]):
    downloaded: list[str] = []

    def downloader(url, destination, config):
        downloaded.append(Path(destination).stem)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"video-bytes")

    def prober(path, config):
        return {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}

    return ReplicationDeps(collector=_collector(rows), downloader=downloader, prober=prober), downloaded


def _collector(rows: list[dict]):
    def collect(config, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None, **kwargs):
        source = Path(str(config.get("_project_root"))) / "raw" / "search_contents_1.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        before_sanitize([source])
        return {"status": "success", "keywords": keywords, "budget": budget}
    return collect


def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _fixture_candidates(payload: dict) -> list[Candidate]:
    return [
        Candidate(
            video_id=row["video_id"], title=row.get("title") or "",
            source_keyword=row.get("source_keyword") or "",
            aweme_type=str(row.get("aweme_type") or ""),
            media_is_audio=bool(row.get("media_is_audio")),
            duration_seconds=float(row.get("duration_seconds") or 0.0),
        )
        for row in payload["candidates"]
    ]


# --------------------------------------------------------------------------- #
# The gate itself (pure, no IO)
# --------------------------------------------------------------------------- #
def test_image_album_is_dropped_at_the_media_type_stage() -> None:
    config = _config(Path("."), prefilter=dict(_ENABLED))
    album = _cand("album", aweme_type="68")
    video = _cand("video", aweme_type="0")
    passed, rejected = prefilter_candidates([album, video], config)

    assert {c.video_id for c in passed} == {"video"}
    assert len(rejected) == 1
    record = rejected[0]
    assert record["stage"] == _PREFILTER_MEDIA_TYPE_STAGE == "pre_media_type"
    assert record["video_id"] == "album"
    assert record["aweme_type"] == "68"
    assert "图文" in record["reason"] and "无视频流" in record["reason"]


def test_audio_media_candidate_is_dropped_by_the_second_is_video_rule() -> None:
    """``media_is_audio`` (image-album music) is the second ``is_video_candidate`` rule."""
    config = _config(Path("."), prefilter=dict(_ENABLED))
    audio = _cand("audio", aweme_type="0", media_is_audio=True)
    assert is_video_candidate(audio)[0] is False  # sanity: the reused rule fires

    passed, rejected = prefilter_candidates([audio], config)
    assert passed == []
    assert rejected[0]["stage"] == "pre_media_type"
    assert "音频" in rejected[0]["reason"]


def test_media_type_gate_runs_after_exclude_and_before_duration() -> None:
    """A candidate failing several gates is attributed to the first one that fires."""
    config = _config(Path("."), prefilter={
        "enabled": True, "min_seconds": 10, "max_seconds": 300,
        "heat_gate_percentile": 0.0, "allow_unknown_duration": True,
        "drop_non_video": True, "exclude_terms": ["带货"],
    })
    # Fails exclude (title), media-type (68) and duration (900 s) simultaneously.
    all_three = _cand("all-three", aweme_type="68", title="带货图文帖", duration=900)
    # Fails media-type (68) and duration (900 s): media-type must win.
    media_and_duration = _cand("media-dur", aweme_type="68", title="干净标题", duration=900)
    # Fails only duration.
    duration_only = _cand("dur", aweme_type="0", title="干净标题", duration=900)

    passed, rejected = prefilter_candidates([all_three, media_and_duration, duration_only], config)
    stages = {entry["video_id"]: entry["stage"] for entry in rejected}
    assert stages["all-three"] == "pre_exclude"
    assert stages["media-dur"] == "pre_media_type"
    assert stages["dur"] == "pre_duration"
    assert passed == []


def test_media_type_gate_is_governed_by_the_prefilter_switch() -> None:
    """``drop_non_video`` alone (enabled off) is inert -- it is *not* like ``exclude_terms``."""
    album = _cand("album", aweme_type="68")
    # enabled off, drop_non_video on -> the media-type gate does not run.
    passed, rejected = prefilter_candidates([album], _config(Path("."), prefilter={
        "enabled": False, "drop_non_video": True,
    }))
    assert passed == [album] and rejected == []
    # enabled on, drop_non_video off -> the gate is explicitly disabled.
    passed, rejected = prefilter_candidates([album], _config(Path("."), prefilter={
        "enabled": True, "min_seconds": 10, "max_seconds": 300,
        "allow_unknown_duration": True, "drop_non_video": False,
    }))
    assert passed == [album] and rejected == []


def test_drop_non_video_setting_defaults_to_true() -> None:
    assert prefilter_drop_non_video({"jobs": {"material_replication": {"prefilter": {}}}}) is True
    assert prefilter_drop_non_video({"jobs": {"material_replication": {"prefilter": {"drop_non_video": False}}}}) is False


# --------------------------------------------------------------------------- #
# Pipeline contract: switch off => no layer artifact, behaviour unchanged
# --------------------------------------------------------------------------- #
def test_disabled_prefilter_leaves_image_albums_to_the_download_loop(tmp_path: Path) -> None:
    """Contract: ``enabled: false`` + ``drop_non_video: true`` emits no prefilter layer.

    The media-type gate is governed by ``prefilter.enabled`` -- exactly like the
    duration/heat gates and *unlike* ``exclude_terms``.  With the switch off the
    image album is not dropped pre-download; the loop's ``not_video`` backstop
    still refuses it, and no ``prefilter.json`` / readme section is produced.
    """
    config = _config(tmp_path, prefilter={"enabled": False, "drop_non_video": True})
    rows = [
        _row("keep", "作者A", url="https://signed.example/1"),
        _row("album", "作者B", aweme_type="68", url="https://signed.example/2"),
    ]
    deps, downloaded = _deps(rows)
    result = run_material_replication(
        config, "小米澎程N90", business_date="2026-09-13", download_only=True, deps=deps,
    )
    out = Path(result["output_dir"])
    manifest = json.loads((out / "清单.json").read_text(encoding="utf-8"))

    assert "prefilter" not in manifest
    assert not (out / _PROCESS / "prefilter.json").exists()
    assert "下载前预筛" not in (out / "00-交付说明.md").read_text(encoding="utf-8")
    # The loop backstop still refuses the image album (no regresssion in coverage).
    assert {item["stage"] for item in manifest["download_failures"]} == {"not_video"}
    assert downloaded == ["keep"]


def test_readme_reports_the_image_album_drop(tmp_path: Path) -> None:
    config = _config(tmp_path, prefilter=dict(_ENABLED))
    rows = [
        _row("keep", "作者A", url="https://signed.example/1"),
        _row("album", "作者B", aweme_type="68", url="https://signed.example/2"),
    ]
    deps, downloaded = _deps(rows)
    result = run_material_replication(
        config, "小米澎程N90", business_date="2026-09-13", download_only=True, deps=deps,
    )
    out = Path(result["output_dir"])
    manifest = json.loads((out / "清单.json").read_text(encoding="utf-8"))
    prefilter = json.loads((out / _PROCESS / "prefilter.json").read_text(encoding="utf-8"))

    assert prefilter["passed"] == 1 and prefilter["rejected"] == 1
    assert prefilter["config"]["drop_non_video"] is True
    assert prefilter["rejections"][0]["stage"] == "pre_media_type"
    assert manifest["prefilter"]["rejections"][0]["aweme_type"] == "68"

    readme = (out / "00-交付说明.md").read_text(encoding="utf-8")
    assert "图文剔除 True" in readme
    assert "其中图文帖（无视频流）1 条" in readme
    assert "pre_media_type" in readme
    assert downloaded == ["keep"]


# --------------------------------------------------------------------------- #
# Real-pool regression: 40 candidates -> exactly the 9 image albums dropped
# --------------------------------------------------------------------------- #
def test_real_pool_fixture_drops_exactly_the_non_video_posts() -> None:
    """Unconditional (no skip): the committed fixture pins 40 -> 31."""
    assert FIXTURE.exists(), "受控回归 fixture 必须随仓库提供"
    payload = _load_fixture()
    assert payload["pool_size"] == 40
    assert payload["aweme_type_distribution"] == {"0": 29, "55": 1, "61": 1, "68": 9}

    candidates = _fixture_candidates(payload)
    assert len(candidates) == 40
    # Shipped prefilter (enabled, window 10~300, allow_unknown, drop_non_video).
    config = _config(Path("."), prefilter={
        "enabled": True, "min_seconds": 10, "max_seconds": 300,
        "heat_gate_percentile": 0.0, "allow_unknown_duration": True, "drop_non_video": True,
    })
    passed, rejected = prefilter_candidates(candidates, config)

    assert len(passed) == 31, len(passed)
    assert len(rejected) == 9, len(rejected)
    assert {entry["stage"] for entry in rejected} == {"pre_media_type"}
    assert {entry["video_id"] for entry in rejected} == set(payload["expected_non_video_ids"])
    assert {entry["aweme_type"] for entry in rejected} == {"68"}
