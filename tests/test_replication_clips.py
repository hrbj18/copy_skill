from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

from douyin_intelligence import replication_clips
from douyin_intelligence.replication_clips import (
    ClipInterval,
    derive_clip_intervals,
    derive_face_free_intervals,
    export_video_clips,
    remove_tree,
)


def test_derive_intervals_keeps_runs_within_bounds() -> None:
    flags = [True, False, False, False, False, True]
    intervals = derive_face_free_intervals(flags, 6.0, min_seconds=3.0, max_seconds=8.0, interval_seconds=1.0)
    assert [(clip.start, clip.end) for clip in intervals] == [(1.0, 5.0)]


def test_derive_intervals_splits_long_runs_and_drops_short_tails() -> None:
    flags = [False] * 12
    intervals = derive_face_free_intervals(flags, 12.0, min_seconds=3.0, max_seconds=8.0, interval_seconds=1.0)
    assert [(clip.start, clip.end) for clip in intervals] == [(0.0, 8.0), (8.0, 12.0)]

    short = [True, False, False, True]
    assert derive_face_free_intervals(short, 4.0, min_seconds=3.0, max_seconds=8.0) == []


def test_derive_intervals_handles_empty_input() -> None:
    assert derive_face_free_intervals([], 10.0) == []
    assert derive_face_free_intervals([False], 0.0) == []


def test_delivery_intervals_cover_timeline_without_face_input() -> None:
    """Slice-mode material delivery may include people and must not need face-free frames."""
    assert [(clip.start, clip.end) for clip in derive_clip_intervals(12.0, min_seconds=3.0, max_seconds=8.0)] == [
        (0.0, 8.0), (8.0, 12.0)
    ]
    assert [(clip.start, clip.end) for clip in derive_clip_intervals(10.0, min_seconds=3.0, max_seconds=8.0)] == [
        (0.0, 8.0)
    ]
    assert derive_clip_intervals(2.9, min_seconds=3.0, max_seconds=8.0) == []


def test_export_without_ffmpeg_degrades_to_source_and_intervals(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    result = export_video_clips(None, source, 10.0, [ClipInterval(0.0, 5.0)], tmp_path / "out", role="main", label_prefix="clip")
    assert result["degraded"] is True
    assert result["status"] == "degraded"
    assert (tmp_path / "out" / result["intervals_file"]).is_file()
    assert result["intervals"] == [{"start": 0.0, "end": 5.0, "duration": 5.0}]


def test_export_with_ffmpeg_produces_clips(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")

    def fake_process(command):
        output = Path(command[-1])
        output.write_bytes(b"clip-data")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("douyin_intelligence.replication_clips._run_media_process", fake_process)
    result = export_video_clips("ffmpeg", source, 10.0, [ClipInterval(0.0, 4.0), ClipInterval(4.0, 9.0)], tmp_path / "out", role="main", label_prefix="clip")
    assert result["status"] == "success"
    assert [row["file"] for row in result["clips"]] == ["clip-01.mp4", "clip-02.mp4"]


def test_cleanup_helper_removes_tree_without_rmtree(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "file.txt").write_text("x", encoding="utf-8")
    remove_tree(tmp_path / "a")
    assert not (tmp_path / "a").exists()
    assert "shutil.rmtree(" not in inspect.getsource(replication_clips)
