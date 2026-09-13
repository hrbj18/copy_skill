"""P-D: ``video.duration`` metadata is persisted (controlled patch) and used.

The Douyin crawler builds its on-disk row from a whitelist that omitted the clip
length, so every candidate carried ``duration_seconds == 0.0`` and the
pre-download duration window could never fire -- the root cause of "download
first, filter later".  This module pins the whole chain:

* the whitelist patch adds ``duration_ms`` (milliseconds) and is **idempotent**;
* our redaction boundary keeps ``duration_ms`` (plain metadata, not a secret);
* ``normalize_candidates`` reads it (and nested ``video.duration``) as seconds
  with an honest ``duration_source``, never conflating ms with seconds;
* ``doctor`` reports whether the (gitignored) patch is actually in place;
* with the patch absent the chain still runs and the readme degrades honestly.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from douyin_intelligence.artifact_safety import sanitize_raw_file
from douyin_intelligence.config import load_config
from douyin_intelligence.mediacrawler_patch import (
    PATCH_MARKER,
    apply_duration_patch,
    duration_patch_status,
    is_duration_patch_applied,
)
from douyin_intelligence.replication_candidates import normalize_candidates
from douyin_intelligence.replication_pipeline import ReplicationDeps, replication_doctor, run_material_replication
from douyin_intelligence.replication_selection import prefilter_candidates

_ENABLED_WINDOW = {
    "enabled": True, "min_seconds": 10, "max_seconds": 300,
    "heat_gate_percentile": 0.0, "allow_unknown_duration": True,
}
_PROCESS = "05-过程数据"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _raw_row(video_id: str, *, author: str = "作者", title: str = "", digest: int = 100, url: str = "", **extra) -> dict:
    row = {
        "aweme_id": video_id,
        "desc": title or f"标题-{video_id}",
        "author": {"uid": f"uid-{author}", "nickname": author},
        "create_time": "2026-09-11T08:00:00+08:00",
        "statistics": {"digg_count": digest, "comment_count": 10, "share_count": 5, "collect_count": 20},
        "share_url": f"https://www.douyin.com/video/{video_id}",
    }
    if url:
        row["video_download_url"] = url
    row.update(extra)
    return row


def _config(tmp_path: Path, *, prefilter: dict | None = None) -> dict:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    if prefilter is None:
        config["jobs"]["material_replication"].pop("prefilter", None)
    else:
        config["jobs"]["material_replication"]["prefilter"] = prefilter
    config["jobs"]["material_replication"]["validation"] = {"enabled": False}
    return config


def _install_fake_vendor_patch(tmp_path: Path) -> Path:
    """Create a marker-bearing MediaCrawler store file under ``tmp_path``."""
    vendor = tmp_path / "third_party" / "MediaCrawler" / "store" / "douyin" / "__init__.py"
    vendor.parent.mkdir(parents=True, exist_ok=True)
    vendor.write_text(f"# stub store\n{PATCH_MARKER}\n", encoding="utf-8")
    return vendor


def _deps(rows: list[dict]):
    downloaded: list[str] = []

    def downloader(url, destination, config):
        downloaded.append(Path(destination).stem)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"video-bytes")

    def prober(path, config):
        return {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}

    def collect(config, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None, **kwargs):
        source = Path(str(config["_project_root"])) / "raw" / "search_contents_1.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        before_sanitize([source])
        return {"status": "success", "keywords": keywords, "budget": budget}

    return ReplicationDeps(collector=collect, downloader=downloader, prober=prober), downloaded


# --------------------------------------------------------------------------- #
# Candidate side: ms -> seconds, honest source, no minute-level confusion
# --------------------------------------------------------------------------- #
def test_nested_video_duration_is_read_as_milliseconds(tmp_path: Path) -> None:
    row = _raw_row("a", video={"duration": 15000})  # Douyin unit: milliseconds
    [candidate] = normalize_candidates([row], _config(tmp_path), keywords=["x"])
    assert candidate.duration_seconds == 15.0  # NOT 15000
    assert candidate.duration_source == "video.duration_ms"


def test_patched_duration_ms_field_is_read(tmp_path: Path) -> None:
    # Exactly what the patched crawler writes (a string of ms).
    row = _raw_row("a", duration_ms="15000")
    [candidate] = normalize_candidates([row], _config(tmp_path), keywords=["x"])
    assert candidate.duration_seconds == 15.0
    assert candidate.duration_source == "duration_ms"


def test_a_seconds_named_key_is_not_divided(tmp_path: Path) -> None:
    row = _raw_row("a", duration=900)  # our own flattened seconds key
    [candidate] = normalize_candidates([row], _config(tmp_path), keywords=["x"])
    assert candidate.duration_seconds == 900.0
    assert candidate.duration_source == "duration"


def test_missing_duration_is_reported_honestly(tmp_path: Path) -> None:
    [candidate] = normalize_candidates([_raw_row("a")], _config(tmp_path), keywords=["x"])
    assert candidate.duration_seconds == 0.0
    assert candidate.duration_source == ""


def test_metadata_duration_drives_the_pre_download_window(tmp_path: Path) -> None:
    """The point of P-D: a metadata window now cuts before any download."""
    rows = [
        _raw_row("keep", duration_ms="60000"),     # 60 s -> inside 10~300
        _raw_row("toolong", duration_ms="900000"),  # 900 s -> outside
    ]
    config = _config(tmp_path, prefilter=dict(_ENABLED_WINDOW))
    candidates = normalize_candidates(rows, config, keywords=["x"])
    passed, rejected = prefilter_candidates(candidates, config)

    assert {c.video_id for c in passed} == {"keep"}
    assert [item["stage"] for item in rejected] == ["pre_duration"]
    # Both rows really carried an attributable duration (not the "unknown" fallback).
    assert {c.duration_source for c in candidates} == {"duration_ms"}


# --------------------------------------------------------------------------- #
# Redaction boundary keeps duration (plain metadata)
# --------------------------------------------------------------------------- #
def test_sanitize_preserves_duration_ms(tmp_path: Path) -> None:
    config = load_config()
    source = tmp_path / "search_contents_1.jsonl"
    rows = [
        {"aweme_id": "1", "desc": "标题1", "duration_ms": "15000",
         "author": {"uid": "u1", "nickname": "作者1"}},
        {"aweme_id": "2", "desc": "标题2", "video": {"duration": 45000},
         "author": {"uid": "u2", "nickname": "作者2"}},
    ]
    source.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    sanitize_raw_file(source, config, source="douyin_search")
    kept = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]

    assert kept[0]["duration_ms"] == "15000"      # top-level field preserved verbatim
    assert kept[1]["duration_ms"] == 45000        # nested video.duration flattened in
    # ... and no signed URL leaked through the boundary.
    assert "video_download_url" not in kept[0]


# --------------------------------------------------------------------------- #
# Patch script: idempotent, byte-stable, anchor- and file-aware
# --------------------------------------------------------------------------- #
_SAMPLE = (
    "async def update_douyin_aweme(aweme_item):\n"
    "    save_content_item = {\n"
    '        "video_download_url": _extract_video_download_url(aweme_item),\n'
    '        "music_download_url": _extract_music_download_url(aweme_item),\n'
    "    }\n"
)


def test_patch_script_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "store" / "__init__.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_SAMPLE, encoding="utf-8")

    first = apply_duration_patch(target)
    assert first.changed is True and first.already_applied is False
    assert "duration_ms" in target.read_text(encoding="utf-8")
    hash_after_first = hashlib.sha256(target.read_bytes()).hexdigest()

    second = apply_duration_patch(target)
    assert second.changed is False and second.already_applied is True
    # Running it again is a no-op: byte-for-byte identical, and no exception.
    assert hashlib.sha256(target.read_bytes()).hexdigest() == hash_after_first
    assert is_duration_patch_applied(target) is True


def test_patch_script_reports_a_missing_anchor(tmp_path: Path) -> None:
    target = tmp_path / "__init__.py"
    target.write_text("async def update_douyin_aweme(aweme_item):\n    pass\n", encoding="utf-8")
    result = apply_duration_patch(target)
    assert result.changed is False and result.anchor_found is False
    assert "锚点" in result.message
    # Untouched on disk.
    assert target.read_text(encoding="utf-8") == "async def update_douyin_aweme(aweme_item):\n    pass\n"


def test_patch_script_reports_a_missing_file(tmp_path: Path) -> None:
    result = apply_duration_patch(tmp_path / "nope" / "__init__.py")
    assert result.changed is False and result.file_missing is True


# --------------------------------------------------------------------------- #
# Doctor: honest "patch present / absent"
# --------------------------------------------------------------------------- #
def test_doctor_flags_a_missing_duration_patch(tmp_path: Path) -> None:
    config = _config(tmp_path)  # vendor file resolves under tmp_path -> absent
    status = duration_patch_status(config)
    assert status["status"] == "missing_file"
    assert status["patched"] is False
    assert "不存在" in status["reason"] and status["consequence"]
    assert status["install_command"]

    report = replication_doctor(config)
    assert report["metadata_duration_ok"] is False
    assert report["metadata_duration"]["status"] == "missing_file"
    assert report["status"] != "ok"


def test_doctor_accepts_a_present_duration_patch(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _install_fake_vendor_patch(tmp_path)
    status = duration_patch_status(config)
    assert status["status"] == "ok" and status["patched"] is True

    report = replication_doctor(config)
    assert report["metadata_duration_ok"] is True
    assert report["metadata_duration"]["status"] == "ok"


# --------------------------------------------------------------------------- #
# End-to-end: the chain runs and the delivery degrades honestly
# --------------------------------------------------------------------------- #
def test_flow_runs_and_readme_degrades_honestly_without_the_patch(tmp_path: Path) -> None:
    config = _config(tmp_path, prefilter=dict(_ENABLED_WINDOW))
    rows = [
        _raw_row("a", url="https://signed.example/a"),  # no duration metadata
        _raw_row("b", url="https://signed.example/b"),
    ]
    deps, downloaded = _deps(rows)
    result = run_material_replication(
        config, "小米澎程N90", business_date="2026-09-13", download_only=True, deps=deps,
    )
    out = Path(result["output_dir"])
    manifest = json.loads((out / "清单.json").read_text(encoding="utf-8"))
    pool = json.loads((out / _PROCESS / "candidate_pool.json").read_text(encoding="utf-8"))

    # Never fails, never fakes a duration: unknown metadata -> 0.0, allowed through.
    assert result["status"] == "success"
    assert downloaded == ["a", "b"]
    assert {c["duration_seconds"] for c in pool["candidates"]} == {0.0}
    assert {c["duration_source"] for c in pool["candidates"]} == {""}

    # ... and the delivery says so, in Chinese, with the fix.
    assert manifest["prefilter"]["metadata_duration_available"] is False
    assert "未启用元数据时长" in manifest["prefilter"]["metadata_duration_note"]
    readme = (out / "00-交付说明.md").read_text(encoding="utf-8")
    assert "本编辑器/环境未启用元数据时长" in readme
    assert "apply_mediacrawler_duration_patch.py" in readme


def test_readme_has_no_degradation_note_when_the_patch_is_present(tmp_path: Path) -> None:
    config = _config(tmp_path, prefilter=dict(_ENABLED_WINDOW))
    _install_fake_vendor_patch(tmp_path)
    rows = [_raw_row("a", duration_ms="60000", url="https://signed.example/a")]
    deps, _ = _deps(rows)
    result = run_material_replication(
        config, "小米澎程N90", business_date="2026-09-13", download_only=True, deps=deps,
    )
    out = Path(result["output_dir"])
    manifest = json.loads((out / "清单.json").read_text(encoding="utf-8"))
    assert manifest["prefilter"]["metadata_duration_available"] is True
    assert "metadata_duration_note" not in manifest["prefilter"]
    assert "未启用元数据时长" not in (out / "00-交付说明.md").read_text(encoding="utf-8")
