"""T4: the automatic visual-confirmation gate (frames + OCR + subject match).

The gate exists because metadata-only relevance still delivered clips about the
wrong product (a "mechanical duck robot" run shipped Unitree humanoid clips).
It reads the one visual dimension available on this machine -- on-screen text --
and must stay **fully automatic**: no prompt, no pause, no confirmation path.

These tests inject the frame extractor and the OCR function through ``deps``,
so no ffmpeg, no RapidOCR models and no network are needed, and the engine is
never allowed to raise: any failure degrades a single item to ``unknown``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from douyin_intelligence.replication_visual import video_id_from_name, verify_videos


@dataclass
class _Deps:
    """Injectable collaborators; ``None`` falls back to the real ffmpeg/OCR."""

    frame_extractor: Any = None
    ocr: Any = None


def _write_video(root: Path, name: str) -> Path:
    path = root / name
    path.write_bytes(b"not-a-real-video")
    return path


def _fake_extractor(frames: int = 3) -> Callable[[Path, Path], list[Path]]:
    def extract(video: Path, destination: Path) -> list[Path]:
        written = []
        for index in range(frames):
            path = destination / f"frame-{index:02d}.jpg"
            path.write_bytes(b"fake-jpeg")
            written.append(path)
        return written
    return extract


def _fake_ocr(text: str) -> Callable[[Path], str]:
    def recognise(frame: Path) -> str:
        return text
    return recognise


# --------------------------------------------------------------------------- #
# 1. Every clip hits -> conclusive
# --------------------------------------------------------------------------- #
def test_all_subjects_hit_is_conclusive(tmp_path: Path) -> None:
    on_screen = _write_video(tmp_path, "作者A_玩具测评_1234567890123456789.mp4")
    in_title = _write_video(tmp_path, "作者B_机械鸭机器人开箱_9876543210987654321.mp4")

    def ocr(frame: Path) -> str:
        # Only the first clip carries readable on-screen text.  The second one is
        # recognised through its *file name* (which holds the post title), the
        # other half of ``visual_text``.
        return "机械鸭机器人 演示" if frame.parent.name == "video-000" else ""

    result = verify_videos(
        [on_screen, in_title], ["机械鸭机器人"], {},
        deps=_Deps(_fake_extractor(), ocr),
    )

    assert result["enabled"] is True
    assert result["conclusive"] is True
    assert [item["verdict"] for item in result["items"]] == ["hit", "hit"]
    assert [item["subject_hits"] for item in result["items"]] == [1, 1]
    assert all(item["frames"] == 3 for item in result["items"])
    assert result["items"][0]["ocr_text"] == "机械鸭机器人 演示\n机械鸭机器人 演示\n机械鸭机器人 演示"
    assert result["items"][1]["ocr_text"] == ""
    assert result["items"][0]["video_id"] == "1234567890123456789"


# --------------------------------------------------------------------------- #
# 2. No clip hits -> NOT conclusive
# --------------------------------------------------------------------------- #
def test_no_subject_match_is_not_conclusive(tmp_path: Path) -> None:
    video = _write_video(tmp_path, "某账号_人形机器人测评_6666666666666666666.mp4")

    result = verify_videos(
        [video], ["机械鸭", "机械鸭机器人"], {},
        deps=_Deps(_fake_extractor(), _fake_ocr("Unitree H1 人形机器人 演示")),
    )

    assert result["enabled"] is True
    assert result["conclusive"] is False
    assert [item["verdict"] for item in result["items"]] == ["miss"]
    assert result["items"][0]["subject_hits"] == 0
    assert result["items"][0]["frames"] == 3


def test_only_unknown_is_not_conclusive(tmp_path: Path) -> None:
    """``conclusive`` is driven by real hits, never by "not a miss"."""
    missing = tmp_path / "作者_机械鸭机器人_7777777777777777777.mp4"

    result = verify_videos(
        [missing], ["机械鸭机器人"], {},
        deps=_Deps(_fake_extractor(), _fake_ocr("机械鸭机器人")),
    )

    assert result["conclusive"] is False
    assert [item["verdict"] for item in result["items"]] == ["unknown"]
    assert result["items"][0]["frames"] == 0

    empty = verify_videos([], ["机械鸭机器人"], {}, deps=_Deps(_fake_extractor(), _fake_ocr("x")))
    assert empty == {"enabled": True, "conclusive": False, "items": []}
    # A missing/short config must not blow up the tool resolution either.
    assert verify_videos([], [], None) == {"enabled": True, "conclusive": False, "items": []}


# --------------------------------------------------------------------------- #
# 3. A broken clip degrades alone; nothing is raised
# --------------------------------------------------------------------------- #
def test_failures_degrade_only_that_item(tmp_path: Path) -> None:
    good = _write_video(tmp_path, "作者_机械鸭机器人_1111111111111111111.mp4")
    extract_fails = _write_video(tmp_path, "作者_抽帧失败_2222222222222222222.mp4")
    ocr_fails = _write_video(tmp_path, "作者_OCR失败_3333333333333333333.mp4")
    missing = tmp_path / "作者_文件不存在_4444444444444444444.mp4"
    called: list[str] = []

    def extractor(video: Path, destination: Path) -> list[Path]:
        called.append(video.name)
        if video.name == extract_fails.name:
            raise RuntimeError("ffmpeg 抽帧失败")
        path = destination / "frame-00.jpg"
        path.write_bytes(b"fake-jpeg")
        return [path]

    def ocr(frame: Path) -> str:
        if frame.parent.name == "video-002":  # ``ocr_fails`` is the third path
            raise RuntimeError("OCR 引擎崩溃")
        return "机械鸭机器人 演示"

    result = verify_videos(
        [good, extract_fails, ocr_fails, missing], ["机械鸭机器人"], {},
        deps=_Deps(extractor, ocr),
    )

    assert [item["verdict"] for item in result["items"]] == ["hit", "unknown", "unknown", "unknown"]
    assert result["items"][0]["subject_hits"] == 1
    assert [item["subject_hits"] for item in result["items"][1:]] == [0, 0, 0]
    assert [item["frames"] for item in result["items"][1:]] == [0, 0, 0]
    # A missing file is rejected before ffmpeg is ever invoked.
    assert missing.name not in called


def test_no_frame_extracted_is_unknown_not_miss(tmp_path: Path) -> None:
    video = _write_video(tmp_path, "作者_机械鸭机器人_8888888888888888888.mp4")

    result = verify_videos(
        [video], ["机械鸭机器人"], {},
        deps=_Deps(_fake_extractor(frames=0), _fake_ocr("机械鸭机器人")),
    )

    assert [item["verdict"] for item in result["items"]] == ["unknown"]


# --------------------------------------------------------------------------- #
# 4. Temp frame directories are always cleaned up
# --------------------------------------------------------------------------- #
def test_temp_frame_directories_are_cleaned_up(tmp_path: Path) -> None:
    video = _write_video(tmp_path, "作者_机械鸭机器人_5555555555555555555.mp4")
    seen: list[Path] = []

    def extractor(source: Path, destination: Path) -> list[Path]:
        seen.append(destination)
        path = destination / "frame-00.jpg"
        path.write_bytes(b"fake-jpeg")
        return [path]

    def ocr(frame: Path) -> str:
        raise RuntimeError("boom")

    result = verify_videos([video], ["机械鸭机器人"], {}, deps=_Deps(extractor, ocr))

    assert result["items"][0]["verdict"] == "unknown"
    assert seen and all(not path.exists() for path in seen)


# --------------------------------------------------------------------------- #
# 5. ``video_id`` is the trailing aweme id
# --------------------------------------------------------------------------- #
def test_video_id_comes_from_the_trailing_digits() -> None:
    assert video_id_from_name(Path("作者_机械鸭机器人开箱_1234567890123456789.mp4")) == "1234567890123456789"
    assert video_id_from_name(Path("_7364812345678901234.mp4")) == "7364812345678901234"
    # No id in the name: fall back to the stem so the row stays identifiable.
    assert video_id_from_name(Path("没有编号的作品.mp4")) == "没有编号的作品"


# --------------------------------------------------------------------------- #
# 6. The switch is a config flag, never a prompt
# --------------------------------------------------------------------------- #
def test_disabled_switch_short_circuits_without_touching_files(tmp_path: Path) -> None:
    video = _write_video(tmp_path, "作者_机械鸭机器人_9999999999999999999.mp4")
    config = {"jobs": {"material_replication": {"visual_verify": {"enabled": False}}}}

    def extractor(source: Path, destination: Path) -> list[Path]:
        raise AssertionError("停用后不应触碰 ffmpeg")

    def ocr(frame: Path) -> str:
        raise AssertionError("停用后不应触碰 OCR")

    result = verify_videos([video], ["机械鸭机器人"], config, deps=_Deps(extractor, ocr))

    assert result == {"enabled": False, "conclusive": False, "items": []}
