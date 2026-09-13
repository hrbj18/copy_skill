from __future__ import annotations

import json
from pathlib import Path

from douyin_intelligence.config import load_config
from douyin_intelligence.replication_pipeline import ReplicationDeps, run_material_replication


def _row(
    video_id: str,
    author: str,
    *,
    duration: float = 60.0,
    digg: int = 100,
    url: str = "",
    aweme_type: str | None = None,
) -> dict:
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
    # The fake downloader writes non-media bytes; isolate the pipeline tests from
    # the download-validation layer (real ffprobe+ffmpeg), which has its own
    # dedicated test module.
    config["jobs"]["material_replication"]["validation"] = {"enabled": False}
    return config


def _collector(rows: list[dict]):
    def collect(config, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None):
        source = Path(str(config.get("_project_root"))) / "raw" / "search_contents_1.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        assert before_sanitize is not None
        before_sanitize([source])
        return {"status": "success", "keywords": keywords, "budget": budget}
    return collect


class _BoomFace:
    """Any call proves the download-only path wrongly touched face detection."""

    backend = "opencv_yunet"

    def status(self):
        raise AssertionError("download-only 不应触碰人脸后端")

    def run(self, *args, **kwargs):
        raise AssertionError("download-only 不应运行人脸检测")


def _deps(tmp_path: Path, rows: list[dict]) -> ReplicationDeps:
    def downloader(url, destination, config):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"video-bytes")

    def prober(path, config):
        return {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}

    return ReplicationDeps(
        collector=_collector(rows), downloader=downloader, prober=prober, face_detector=_BoomFace(),
    )


def test_download_only_downloads_all_candidates_without_face_or_clips(tmp_path: Path) -> None:
    config = _config(tmp_path)
    rows = [
        _row("7300000000000000001", "作者A", url="https://signed.example/1", digg=300),
        _row("7300000000000000002", "作者B", url="https://signed.example/2", digg=200),
        _row("7300000000000000003", "作者C", aweme_type="68", url="https://signed.example/3", digg=100),
        _row("7300000000000000004", "作者D", digg=50),  # no media url
    ]
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True,
        deps=_deps(tmp_path, rows),
    )
    assert result["mode"] == "download_only"
    assert result["status"] in {"success", "partial"}
    assert result["degraded"] is False and result["insufficient"] is False
    output_dir = Path(result["output_dir"])
    assert output_dir.name == "9.12苹果折叠屏手机复刻视频"

    manifest = json.loads((output_dir / "清单.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "download_only"
    assert manifest["script_replica"]["status"] == "skipped"
    assert manifest["main_materials"] == []
    assert manifest["supporting_materials"] == []
    assert manifest["counters"]["downloaded"] == 2
    assert manifest["counters"]["clips_exported"] == 0
    assert manifest["counters"]["face_checked"] == 0

    assert {item["video_id"] for item in manifest["downloads"]} == {
        "7300000000000000001", "7300000000000000002",
    }
    # The image album (aweme_type=68) is now dropped *before* the download loop by
    # the prefilter media-type gate (governed by prefilter.enabled, on by default),
    # so the loop's "not_video" backstop no longer fires for it: it is reported in
    # the prefilter section instead and never touches a download slot.  The only
    # failure that reaches the loop is the genuinely-unreachable "no_media_url".
    assert {item["stage"] for item in manifest["download_failures"]} == {"no_media_url"}
    prefilter = json.loads((output_dir / "05-过程数据" / "prefilter.json").read_text(encoding="utf-8"))
    assert prefilter["rejected"] == 1
    assert [entry["stage"] for entry in prefilter["rejections"]] == ["pre_media_type"]
    assert prefilter["rejections"][0]["video_id"] == "7300000000000000003"
    assert prefilter["rejections"][0]["aweme_type"] == "68"

    # No face / clip / script work happened: 01/02/03 stay empty.
    assert not list((output_dir / "01-脚本思路").iterdir())
    assert not list((output_dir / "02-主素材").iterdir())
    assert not list((output_dir / "03-辅助素材").iterdir())
    copies = sorted(path.name for path in (output_dir / "04-原片").glob("*.mp4"))
    assert len(copies) == 2
    for item in manifest["downloads"]:
        assert item["file"].startswith("04-原片/")

    assert (output_dir / "05-过程数据" / "download_log.json").is_file()
    readme = (output_dir / "00-交付说明.md").read_text(encoding="utf-8")
    assert "## 下载清单" in readme
    assert "## 下载失败" in readme
    assert "模式：仅采集与下载" in readme


def test_download_only_ignores_heat_median_gate(tmp_path: Path) -> None:
    # A high ``heat_gate_percentile`` would drop low-heat rows from the material
    # pool; download-only must still fetch every eligible video candidate.
    config = _config(tmp_path)
    config["jobs"]["material_replication"].setdefault("material_replica", {})["heat_gate_percentile"] = 1.0
    rows = [
        _row("7300000000000000001", "作者A", url="https://signed.example/1", digg=1000),
        _row("7300000000000000002", "作者B", url="https://signed.example/2", digg=10),
        _row("7300000000000000003", "作者C", url="https://signed.example/3", digg=5),
    ]
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True,
        deps=_deps(tmp_path, rows),
    )
    manifest = json.loads((Path(result["output_dir"]) / "清单.json").read_text(encoding="utf-8"))
    assert {item["video_id"] for item in manifest["downloads"]} == {
        "7300000000000000001", "7300000000000000002", "7300000000000000003",
    }
    assert manifest["download_failures"] == []
    assert result["status"] == "success"
