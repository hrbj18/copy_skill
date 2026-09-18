"""Delivery-folder size gate + ``04-原片`` de-duplication in direct mode.

Two root-cause fixes, each with its own falsifiable test:

* ``material_replica.direct_delivery`` (opt-in) ships each selected source as a
  **whole file** into 02/03, so a second copy in 04-原片 was byte-for-byte
  redundant and doubled the delivery folder.  Direct mode now writes an *index*
  (``04-原片/原片索引.json`` / ``原片索引.md``) instead, and every side-car's
  ``source.folder`` points at the index entry rather than a ``.mp4`` that is not
  there.  The slicing path (non direct) is untouched: there 04-原片 holds the real
  source, so it is not redundant.

* The user's spec is a **delivery directory totalling 200,000,000 bytes**.  No
  existing gate measured that whole: ``material_replica.delivered_bytes`` counts
  only 02/03 and the old budget test measured only 04-原片 -- each blind to the
  other half, so a doubled folder shipped silently.  The pipeline now records
  ``manifest["delivery_folder"]["delivery_folder_bytes"]`` (every file, summed)
  and, when it exceeds the cap, **refuses to publish** the folder
  (:class:`DeliveryFolderOverLimit`) instead of degrading-and-shipping it.

These tests opt into ``direct_delivery`` explicitly; ``tests/conftest.py`` strips
it from the live fixture, so nothing here depends on the shipped switch.

Every delivery also carries ``00-素材目录.json`` (the downstream selection
contract); the full-chain case below pins its completeness and byte fidelity.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from douyin_intelligence.replication_delivery import (
    CATALOG_NAME,
    DeliveryFolderOverLimit,
    validate_delivery_manifest,
)
from douyin_intelligence.config import load_config
from douyin_intelligence.replication_pipeline import ReplicationDeps, run_material_replication
from douyin_intelligence.replication_selection import VisualMetrics

MiB = 1024 * 1024
SOURCE_DIR = "04-原片"
MAIN_DIR = "02-主素材"
SUPPORT_DIR = "03-辅助素材"
MANIFEST = "清单.json"
SOURCE_INDEX = "原片索引.json"
SOURCE_INDEX_MD = "原片索引.md"


def _row(video_id: str, author: str, *, duration: float = 60.0, digg: int = 100) -> dict:
    return {
        "aweme_id": video_id,
        "desc": f"标题-{video_id}",
        "author": {"uid": f"uid-{author}", "nickname": author},
        "create_time": "2026-09-11T08:00:00+08:00",
        "statistics": {"digg_count": digg, "comment_count": 10, "share_count": 5, "collect_count": 20},
        "duration": duration,
        "video_download_url": f"https://signed.example/{video_id}",
        "share_url": f"https://www.douyin.com/video/{video_id}",
    }


def _config(tmp_path: Path, *, direct: bool, keep_source: bool = True) -> dict:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    material = config["jobs"]["material_replication"]
    material["prefilter"] = {"enabled": False}
    material["validation"] = {"enabled": False}
    material["retention"] = {"keep_source_video": keep_source}
    material.pop("download_budget", None)
    if direct:
        material["direct_delivery"] = {"enabled": True, "main_min_seconds": 0}
    else:
        material.pop("direct_delivery", None)
    return config


def _write_sized(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    block = b"x" * MiB
    with path.open("wb") as stream:
        remaining = size
        while remaining > 0:
            chunk = block if remaining >= len(block) else block[:remaining]
            stream.write(chunk)
            remaining -= len(chunk)


def _collector(rows: list[dict]):
    def collect(config, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None, **kwargs):
        source = Path(str(config.get("_project_root"))) / "raw" / "search_contents_1.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        before_sanitize([source])
        return {"status": "success", "keywords": keywords, "budget": budget}

    return collect


class _Ocr:
    def run(self, video, duration, cache_dir, temp_dir):
        return {"status": "no_text", "items": [], "sampled_frames": 10}


class _Transcriber:
    def run(self, video, cache_dir, temp_dir, **kwargs):
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
            "sample_interval_seconds": 1,
        }


def _prober(path, config):
    return {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}


def _sized_downloader(sizes: dict[str, int]):
    def downloader(url, destination, config, *, max_bytes=None):
        video_id = Path(destination).stem
        _write_sized(Path(destination), int(sizes.get(video_id, 2048)))
    return downloader


def _full_chain_deps(rows: list[dict], sizes: dict[str, int] | None = None) -> ReplicationDeps:
    return ReplicationDeps(
        collector=_collector(rows), downloader=_sized_downloader(sizes or {}), prober=_prober,
        transcriber=_Transcriber(), ocr=_Ocr(), face_detector=_Face(),
    )


def _healthy_tooling(monkeypatch) -> None:
    monkeypatch.setattr("douyin_intelligence.replication_pipeline.media_tool_available", lambda config, name: True)
    monkeypatch.setattr(
        "douyin_intelligence.replication_pipeline.export_video_clips",
        lambda *args, **kwargs: {"degraded": False, "clips": []},
    )
    monkeypatch.setattr(
        "douyin_intelligence.replication_selection.compute_visual_metrics",
        lambda *args, **kwargs: VisualMetrics(
            sampled_frames=10, motion_frame_ratio=0.9, ocr_text_frame_ratio=0.0, visual_ok=True,
        ),
    )


def _delivery_folder_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in Path(root).rglob("*") if path.is_file())


# =========================================================================== #
# (a) direct mode: 04-原片 carries an index, never a second copy of a video
# =========================================================================== #
def test_direct_mode_writes_source_index_not_a_second_video_copy(tmp_path: Path, monkeypatch) -> None:
    _healthy_tooling(monkeypatch)
    rows = [_row(f"v{index:02d}", f"作者{index}") for index in range(3)]
    config = _config(tmp_path, direct=True, keep_source=True)

    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", deps=_full_chain_deps(rows),
    )
    output_dir = Path(result["output_dir"])
    manifest = json.loads((output_dir / MANIFEST).read_text(encoding="utf-8"))

    # The whole point: no duplicated video byte in 04-原片.
    assert list((output_dir / SOURCE_DIR).glob("*.mp4")) == []
    assert (output_dir / SOURCE_DIR / SOURCE_INDEX).is_file()
    assert (output_dir / SOURCE_DIR / SOURCE_INDEX_MD).is_file()

    index = json.loads((output_dir / SOURCE_DIR / SOURCE_INDEX).read_text(encoding="utf-8"))
    records = index["records"]
    assert records, "直投模式必须为每条入选源片写一条索引"
    assert index["count"] == len(records)
    for record in records:
        # Every ``delivered_as`` must name a file that actually exists (relative to
        # the delivery root), otherwise the index would be a dangling pointer.
        delivered = output_dir / record["delivered_as"]
        assert delivered.is_file(), record["delivered_as"]
        assert record["delivered_as"].startswith((f"{MAIN_DIR}/", f"{SUPPORT_DIR}/"))
        assert record["video_id"] and record["author"]
        assert record["size_bytes"] > 0

    # The side-car must not point at a ``.mp4`` that is not in 04-原片.
    sidecars = list((output_dir / MAIN_DIR).glob("*.json")) + list((output_dir / SUPPORT_DIR).glob("*.json"))
    assert sidecars, "直投模式应至少产出一条侧车"
    for sidecar in sidecars:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        folder = payload["source"]["folder"]
        assert folder.startswith(f"{SOURCE_DIR}/{SOURCE_INDEX}#"), folder
        assert (output_dir / folder.split("#", 1)[0]).is_file()
        assert (output_dir / payload["source"]["delivered_as"]).is_file()

    # The switch is on, but nothing was *copied* into 04-原片 -- both facts shown.
    retention = manifest["source_retention"]
    assert retention["keep_source_video"] is True
    assert retention["effective_keep"] is False
    assert retention["kept_count"] == 0
    assert manifest["direct_delivery"]["enabled"] is True


# =========================================================================== #
# (b) non-direct: historical behaviour, byte for byte -- 04-原片 keeps videos
# =========================================================================== #
def test_non_direct_mode_keeps_source_videos_and_no_index(tmp_path: Path, monkeypatch) -> None:
    _healthy_tooling(monkeypatch)
    rows = [_row(f"v{index:02d}", f"作者{index}") for index in range(3)]
    config = _config(tmp_path, direct=False, keep_source=True)

    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", deps=_full_chain_deps(rows),
    )
    output_dir = Path(result["output_dir"])
    manifest = json.loads((output_dir / MANIFEST).read_text(encoding="utf-8"))

    assert list((output_dir / SOURCE_DIR).glob("*.mp4")), "非直投模式必须保留原片视频"
    assert not (output_dir / SOURCE_DIR / SOURCE_INDEX).exists()
    assert not (output_dir / SOURCE_DIR / SOURCE_INDEX_MD).exists()
    retention = manifest["source_retention"]
    assert retention["keep_source_video"] is True
    assert retention["effective_keep"] is True
    assert retention["kept_count"] >= 1
    assert "direct_delivery" not in manifest


# =========================================================================== #
# (c) the size gate measures the *whole* folder and is a hard cap
# =========================================================================== #
def test_over_limit_delivery_is_refused_not_published(tmp_path: Path) -> None:
    """A folder over the 200,000,000-byte cap is refused, never published.

    Two whole files of ~105 MiB = ~210 MiB in 04-原片 alone -> over the cap by
    construction.  The cap is *hard*: the pipeline raises before publication, so
    the destination tree is never created (atomicity preserved).
    """
    rows = [_row("v0000", "作者A"), _row("v0001", "作者B")]
    sizes = {"v0000": 105 * MiB, "v0001": 105 * MiB}
    config = _config(tmp_path, direct=False, keep_source=True)

    with pytest.raises(DeliveryFolderOverLimit) as captured:
        run_material_replication(
            config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True,
            deps=ReplicationDeps(collector=_collector(rows), downloader=_sized_downloader(sizes), prober=_prober),
        )
    message = str(captured.value)
    assert "超过上限" in message
    assert "200000000" in message
    # Nothing was published: the delivery folder name never appears under the root.
    assert list(tmp_path.rglob("9.12苹果折叠屏复刻视频")) == []


def test_delivery_folder_bytes_within_limit_publishes_and_is_exact(tmp_path: Path) -> None:
    """A small folder stays green, and the recorded byte count equals the real total."""
    rows = [_row("v0000", "作者A"), _row("v0001", "作者B")]
    sizes = {"v0000": MiB, "v0001": MiB}
    config = _config(tmp_path, direct=False, keep_source=True)

    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True,
        deps=ReplicationDeps(collector=_collector(rows), downloader=_sized_downloader(sizes), prober=_prober),
    )
    output_dir = Path(result["output_dir"])
    manifest = json.loads((output_dir / MANIFEST).read_text(encoding="utf-8"))

    measured = manifest["delivery_folder"]["delivery_folder_bytes"]
    assert measured <= 200_000_000
    assert manifest["degraded"] is False
    assert result["degraded"] is False
    assert not any("交付目录合计超限" in w for w in manifest["warnings"])
    # Exact: the recorded number is the on-disk total of *every* file in the
    # published folder (publish is a rename, so the bytes are preserved).
    assert measured == _delivery_folder_bytes(output_dir)


# =========================================================================== #
# (d) the material catalog is complete and byte-faithful on a full-chain run
# =========================================================================== #
def test_full_chain_delivery_writes_a_complete_material_catalog(tmp_path: Path, monkeypatch) -> None:
    _healthy_tooling(monkeypatch)
    rows = [_row(f"v{index:02d}", f"作者{index}") for index in range(3)]
    config = _config(tmp_path, direct=True, keep_source=True)

    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", deps=_full_chain_deps(rows),
    )
    output_dir = Path(result["output_dir"])
    catalog = json.loads((output_dir / CATALOG_NAME).read_text(encoding="utf-8"))
    manifest = json.loads((output_dir / MANIFEST).read_text(encoding="utf-8"))

    listed = sorted(entry["file_path"] for entry in catalog["entries"])
    delivered = sorted(
        record["file"] for record in [*manifest["main_materials"], *manifest["supporting_materials"]]
    )
    assert listed == delivered and listed, "目录必须与清单实体逐一对应"
    assert catalog["count"] == len(listed)
    for entry in catalog["entries"]:
        assert entry["file_path"].startswith((f"{MAIN_DIR}/", f"{SUPPORT_DIR}/"))
        assert (output_dir / entry["file_path"]).stat().st_size == entry["file_bytes"]
        assert entry["source_url"].startswith("https://www.douyin.com/video/")
        assert entry["author"] and entry["title"] and entry["material_id"]
        assert entry["file_bytes"] > 0
    # Direct mode ships each source once into 02/03 and 04-原片 keeps only the
    # index, so no entity is duplicated across folders.
    assert list((output_dir / SOURCE_DIR).glob("*.mp4")) == []
    assert validate_delivery_manifest(output_dir / MANIFEST)["status"] == "pass"
