"""Independent QA audit of ``material-replication run --download-only`` (Task #9).

Fresh-eyes falsification harness.  Unlike the engineer's happy-path test, this
file attacks the claims directly:

* C1 - the download-only branch must never construct/touch face/OCR/ASR/slicer;
* C2 - every eligible candidate is downloaded (no heat-median gate);
* C3 - the ``downloads`` ledger matches the delivered ``04-原片`` files; the
  loop failure stages (``no_media_url`` / ``download`` / ``invalid_media``) are
  reachable, and the ``not_video`` backstop is exercised separately (it is now
  normally caught earlier by the pre-download media-type gate);
* C4 - ``insufficient`` is ``False`` on every path that reaches the download
  loop; an all-non-video pool instead empties at the prefilter and honestly
  reports ``failed`` / ``insufficient`` (never ``success``);
* C5 - a legacy manifest without a ``downloads`` key renders unchanged.

All external effects are injected, and ``_project_root`` is redirected to
``tmp_path`` so the real ``data/`` and ``output/`` trees are never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from douyin_intelligence.config import load_config
from douyin_intelligence.replication_delivery import render_delivery_readme
from douyin_intelligence.replication_pipeline import ReplicationDeps, run_material_replication

_SOURCE_DIR = "04-原片"
_PROCESS_DIR = "05-过程数据"


def _row(video_id: str, author: str, *, duration: float = 60.0, digg: int = 100, url: str = "", aweme_type: str | None = None) -> dict:
    row = {
        "aweme_id": video_id,
        "desc": f"标题-{video_id}",
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


def _config(tmp_path: Path) -> dict:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    # Force a heat gate that WOULD drop the low-heat rows from the material pool.
    config["jobs"]["material_replication"].setdefault("material_replica", {})["heat_gate_percentile"] = 1.0
    # Isolate this audit from the download-validation layer: its fake downloader
    # writes non-media bytes, so a real ffprobe+ffmpeg check does not apply.
    config["jobs"]["material_replication"]["validation"] = {"enabled": False}
    return config


class _Forbidden:
    """Any attribute access or call proves the branch touched an unused backend."""

    def __init__(self, label: str) -> None:
        self.__dict__["_label"] = label

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"download-only 不应访问 {self.__dict__['_label']}.{name}")

    def __call__(self, *args: object, **kwargs: object) -> object:
        raise AssertionError(f"download-only 不应调用 {self.__dict__['_label']}")


def _collector(rows: list[dict]):
    def collect(config, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None, **kwargs):
        source = Path(str(config.get("_project_root"))) / "raw" / "search_contents_1.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        before_sanitize([source])
        return {"status": "success", "keywords": keywords, "budget": budget}
    return collect


def _deps(tmp_path: Path, rows: list[dict]) -> ReplicationDeps:
    def downloader(url, destination, config):
        if "boom" in url:
            raise OSError("simulated network failure")
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"video-bytes" * 100)

    def prober(path, config):
        if "7300000000000000006" in Path(path).name:
            return {"duration_seconds": 0.0}  # no width/height -> invalid_media
        return {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}

    return ReplicationDeps(
        collector=_collector(rows),
        downloader=downloader,
        prober=prober,
        face_detector=_Forbidden("face_detector"),
        ocr=_Forbidden("ocr"),
        transcriber=_Forbidden("transcriber"),
        slicer=_Forbidden("slicer"),
    )


def _run(tmp_path: Path, rows: list[dict]) -> tuple[dict, Path, dict]:
    config = _config(tmp_path)
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True,
        deps=_deps(tmp_path, rows),
    )
    output_dir = Path(result["output_dir"])
    manifest = json.loads((output_dir / "清单.json").read_text(encoding="utf-8"))
    return result, output_dir, manifest


def test_download_only_full_audit_all_stages_and_no_native(tmp_path: Path) -> None:
    rows = [
        _row("7300000000000000001", "作者A", url="https://signed.example/1", digg=300),
        _row("7300000000000000002", "作者B", url="https://signed.example/2", digg=1),  # very low heat
        _row("7300000000000000003", "作者C", aweme_type="68", url="https://signed.example/3", digg=200),  # not_video
        _row("7300000000000000004", "作者D", digg=150),  # no_media_url
        _row("7300000000000000005", "作者E", url="https://signed.example/boom", digg=120),  # download fails
        _row("7300000000000000006", "作者F", url="https://signed.example/audioonly", digg=90),  # invalid_media
    ]
    result, output_dir, manifest = _run(tmp_path, rows)

    # --- C4 / mode -------------------------------------------------------
    assert manifest["mode"] == "download_only"
    assert manifest["insufficient"] is False
    assert manifest["degraded"] is False
    assert result["mode"] == "download_only"
    assert result["insufficient"] is False
    assert result["status"] in {"success", "partial"}, result["status"]

    # --- C2: low-heat candidate 7302 still downloaded (no median gate) ---
    downloaded = {item["video_id"] for item in manifest["downloads"]}
    assert downloaded == {"7300000000000000001", "7300000000000000002"}, downloaded

    # --- C3: loop failure stages reachable -------------------------------
    # The image album (7303, aweme_type=68) is dropped *before* the loop by the
    # prefilter media-type gate (shipped default on), so its "not_video" failure
    # is reported in the prefilter ledger instead of the download ledger; the
    # backstop itself is pinned by the gate-off test below.
    stages = sorted({item["stage"] for item in manifest["download_failures"]})
    assert stages == ["download", "invalid_media", "no_media_url"], stages
    for item in manifest["download_failures"]:
        assert item["video_id"] and item["reason"], item
    prefilter = json.loads((output_dir / _PROCESS_DIR / "prefilter.json").read_text(encoding="utf-8"))
    assert [entry["stage"] for entry in prefilter["rejections"]] == ["pre_media_type"]
    assert prefilter["rejections"][0]["video_id"] == "7300000000000000003"

    # --- C1/C3: nothing but 原片 in the delivery -------------------------
    assert manifest["main_materials"] == []
    assert manifest["supporting_materials"] == []
    assert not list((output_dir / "01-脚本思路").iterdir())
    assert not list((output_dir / "02-主素材").iterdir())
    assert not list((output_dir / "03-辅助素材").iterdir())

    # --- C3: downloads ledger matches 04-原片 one-to-one ----------------
    copies = sorted(path.name for path in (output_dir / _SOURCE_DIR).glob("*.mp4"))
    assert len(copies) == len(manifest["downloads"]) == 2, copies
    for item in manifest["downloads"]:
        rel = Path(item["file"])
        assert rel.parts[0] == _SOURCE_DIR, item["file"]
        assert (output_dir / rel).is_file(), rel
        assert rel.name in copies
        assert Path(item["media_path"]).is_file()
        assert int(item["size_bytes"]) > 0
        assert item["width"] == 1080 and item["height"] == 1920

    # --- C1: no face/ocr/asr/motion artifacts anywhere ------------------
    process_dir = output_dir / _PROCESS_DIR
    names = {path.name for path in process_dir.iterdir()}
    assert "download_log.json" in names
    assert "face_metrics.json" not in names
    for dirpath, dirnames, filenames in tmp_path.walk():
        for bad in ("face", "ocr", "asr", "motion"):
            assert bad not in dirnames, f"{dirpath} 含 {bad}/ 目录"
        for name in filenames:
            assert "face_metrics" not in name, name

    # --- readme ----------------------------------------------------------
    readme = (output_dir / "00-交付说明.md").read_text(encoding="utf-8")
    assert "模式：仅采集与下载" in readme
    assert "## 下载清单" in readme
    assert "## 下载失败" in readme


def test_download_only_all_failures_reports_failed_not_success(tmp_path: Path) -> None:
    # A pool of only non-video posts yields zero downloads.  With the pre-download
    # media-type gate on (the shipped default) the pool is emptied *before* the
    # download loop, so the run takes the auditable "prefiltered_empty" path and
    # is honestly "failed" (degraded/insufficient), never "success".
    rows = [_row("7300000000000000003", "作者C", aweme_type="68", url="https://signed.example/3")]
    result, output_dir, manifest = _run(tmp_path, rows)
    assert manifest["downloads"] == []
    assert result["status"] == "failed", result["status"]
    assert result["insufficient"] is True
    assert manifest["prefilter"]["conclusion"] == "prefiltered_empty"
    assert [entry["stage"] for entry in manifest["prefilter"]["rejections"]] == ["pre_media_type"]


def test_download_only_not_video_backstop_fires_when_the_media_gate_is_off(tmp_path: Path) -> None:
    # The pre-download media-type gate and the download-loop "not_video" backstop
    # are two layers of the same rule.  With the gate explicitly disabled the loop
    # must still refuse the image album and record an actionable "not_video"
    # failure -- so coverage of the backstop is not silently lost.
    config = _config(tmp_path)
    config["jobs"]["material_replication"]["prefilter"]["drop_non_video"] = False
    rows = [
        _row("7300000000000000001", "作者A", url="https://signed.example/1", digg=300),
        _row("7300000000000000003", "作者C", aweme_type="68", url="https://signed.example/3", digg=200),
    ]
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True,
        deps=_deps(tmp_path, rows),
    )
    manifest = json.loads((Path(result["output_dir"]) / "清单.json").read_text(encoding="utf-8"))
    assert {item["stage"] for item in manifest["download_failures"]} == {"not_video"}
    assert {item["video_id"] for item in manifest["downloads"]} == {"7300000000000000001"}


def test_download_only_trap_is_armed_on_the_normal_path(tmp_path: Path) -> None:
    # Negative control: the SAME forbidden deps MUST blow up when download_only is
    # False, proving the trap in the audit test above is genuinely armed.
    config = _config(tmp_path)
    rows = [_row("7300000000000000001", "作者A", url="https://signed.example/1")]
    with pytest.raises(AssertionError, match="face_detector"):
        run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", deps=_deps(tmp_path, rows))


def test_render_legacy_manifest_without_downloads_key_is_unchanged() -> None:
    # C5 (synthetic): a manifest with no ``downloads`` key must render exactly as
    # before -- no crash, no download section.
    manifest = {
        "folder": "9.12苹果折叠屏复刻视频",
        "evidence_disclaimer": "本清单为过程证据，不得作为事实依据",
        "theme": "苹果折叠屏手机",
        "business_date": "2026-09-12",
        "generated_at": "2026-09-12T14:00:00+08:00",
        "keywords_used": ["苹果折叠屏"],
        "face_backend": "opencv_yunet",
        "face_backend_status": "ok",
        "ffmpeg_status": "ok",
        "degraded": False,
        "insufficient": False,
        "script_replica": {"status": "found"},
        "main_materials": [],
        "supporting_materials": [],
        "warnings": [],
    }
    text = render_delivery_readme(manifest)
    assert "## 下载清单" not in text
    assert "## 下载失败" not in text
    assert "模式：仅采集与下载" not in text
    assert "主素材" in text and "辅助素材" in text
