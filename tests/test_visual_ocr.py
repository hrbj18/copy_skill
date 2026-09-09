from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw
import numpy as np

from douyin_intelligence.config import load_config
from douyin_intelligence.visual_ocr import (
    VisualBatchBudget,
    conservative_visual_dedupe,
    evenly_limit,
    frame_budget_for_duration,
    merge_text_cards,
    parse_rapidocr_output,
    process_visual_video,
)


def _settings() -> dict:
    return load_config()["jobs"]["trusted_account_news"]["visual_ocr"]


def _image(path: Path, text: str) -> Path:
    image = Image.new("RGB", (960, 540), "white")
    ImageDraw.Draw(image).text((120, 220), text, fill="black")
    image.save(path)
    return path


def _output(text: str, score: float = 0.9, y: int = 10):
    return SimpleNamespace(
        txts=(text,), scores=(score,), boxes=(([10, y], [500, y], [500, y + 40], [10, y + 40]),),
    )


def test_frame_budgets_are_frozen_and_even_cap_keeps_head_tail() -> None:
    settings = _settings()
    assert [frame_budget_for_duration(value, settings) for value in (15, 16, 31, 61, 121)] == [
        (20, 30), (30, 45), (40, 60), (60, 80), (80, 100),
    ]
    assert evenly_limit(list(range(101)), 5) == [0, 25, 50, 75, 100]


def test_visual_dedupe_is_conservative_and_fail_open_for_text_changes(tmp_path: Path) -> None:
    first = _image(tmp_path / "a.jpg", "2026")
    identical = tmp_path / "b.jpg"
    identical.write_bytes(first.read_bytes())
    changed = _image(tmp_path / "c.jpg", "2027")
    candidates = [
        {"path": str(first), "timestamp_seconds": 0.0},
        {"path": str(identical), "timestamp_seconds": 1.0},
        {"path": str(changed), "timestamp_seconds": 2.0},
    ]
    kept, dropped = conservative_visual_dedupe(candidates, _settings())
    assert dropped == 1
    assert [Path(row["path"]).name for row in kept] == ["a.jpg", "c.jpg"]


def test_rapidocr_lines_keep_boxes_scores_and_reading_order() -> None:
    output = SimpleNamespace(
        txts=("第二行", "第一行"), scores=(0.82, 0.91),
        boxes=(
            ([10, 100], [100, 100], [100, 130], [10, 130]),
            ([10, 10], [100, 10], [100, 40], [10, 40]),
        ),
    )
    lines = parse_rapidocr_output(output)
    assert [row["text"] for row in lines] == ["第一行", "第二行"]
    assert lines[0]["confidence"] == 0.91
    assert lines[0]["box"][0] == [10.0, 10.0]


def test_rapidocr_numpy_boxes_do_not_trigger_ambiguous_truth_value() -> None:
    output = SimpleNamespace(
        txts=("AI NEWS 2026",), scores=(0.99,),
        boxes=np.array([[[10, 10], [400, 10], [400, 50], [10, 50]]], dtype=np.float32),
    )
    assert parse_rapidocr_output(output)[0]["text"] == "AI NEWS 2026"


def test_incremental_cards_merge_but_numeric_changes_survive() -> None:
    frames = [
        {"frame_index": 0, "timestamp_seconds": 0.0, "clean_lines": [{"text": "苹果发布新产品"}]},
        {"frame_index": 1, "timestamp_seconds": 1.0, "clean_lines": [{"text": "苹果发布新产品售价999元"}]},
        {"frame_index": 2, "timestamp_seconds": 2.0, "clean_lines": [{"text": "苹果发布新产品售价1099元"}]},
    ]
    cards = merge_text_cards(frames, 0.88)
    assert len(cards) == 2
    assert cards[0]["text"] == "苹果发布新产品售价999元"
    assert cards[1]["text"] == "苹果发布新产品售价1099元"


def test_process_visual_video_retries_only_low_confidence_once_and_emits_quality(monkeypatch, tmp_path: Path) -> None:
    settings = _settings()
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    frame_paths = [_image(tmp_path / f"f{i}.jpg", f"新闻正文第{i}条包含足够有效文字") for i in range(2)]
    frames = [
        {"path": str(path), "timestamp_seconds": float(index), "selection_reason": "interval_guard", "frame_index": index}
        for index, path in enumerate(frame_paths)
    ]
    monkeypatch.setattr("douyin_intelligence.visual_ocr._probe_duration", lambda *_args: 10.0)
    monkeypatch.setattr(
        "douyin_intelligence.visual_ocr.extract_scene_interval_frames",
        lambda *_args, **_kwargs: (frames, {"duration_seconds": 10.0, "candidate_frames": 2, "selected_frames": 2, "visual_duplicates_removed": 0}),
    )
    retry_path = _image(tmp_path / "retry.jpg", "新闻正文重试后清晰")
    monkeypatch.setattr("douyin_intelligence.visual_ocr._rerender", lambda *_args, **_kwargs: retry_path)
    calls = {"count": 0}

    def engine(_path: Path):
        calls["count"] += 1
        if calls["count"] == 1:
            return _output("新闻正文第一条包含足够有效文字和公司名称", 0.6)
        return _output("新闻正文第二条包含足够有效文字和关键数字2026", 0.93)

    batch = VisualBatchBudget(soft_limit=300, hard_limit=400, deadline=time.monotonic() + 30)
    result = process_visual_video(video, tmp_path / "visual", settings, batch, engine=engine)
    assert result["ocr_frames"] == 2
    assert result["retry_frames"] == 1
    assert calls["count"] == 3
    assert result["visual_text_status"] in {"success", "partial"}
    assert result["unique_content_chars"] >= 15
    assert batch.total_ocr_frames == 3


def test_batch_budget_exhaustion_returns_without_ocr(tmp_path: Path) -> None:
    settings = _settings()
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    batch = VisualBatchBudget(soft_limit=0, hard_limit=0, deadline=time.monotonic() + 30)
    result = process_visual_video(video, tmp_path / "visual", settings, batch, engine=lambda _path: None)
    assert result["visual_text_status"] == "budget_exhausted"
    assert result["ocr_frames"] == 0


def test_per_video_deadline_stops_ocr_between_frames(monkeypatch, tmp_path: Path) -> None:
    settings = dict(_settings())
    settings["per_video_timeout_seconds"] = 0.01
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    frame_paths = [_image(tmp_path / f"deadline-{index}.jpg", f"frame {index}") for index in range(3)]
    frames = [
        {"path": str(path), "timestamp_seconds": float(index), "selection_reason": "interval_guard", "frame_index": index}
        for index, path in enumerate(frame_paths)
    ]
    monkeypatch.setattr("douyin_intelligence.visual_ocr._probe_duration", lambda *_args: 10.0)
    monkeypatch.setattr(
        "douyin_intelligence.visual_ocr.extract_scene_interval_frames",
        lambda *_args, **_kwargs: (frames, {"duration_seconds": 10.0, "candidate_frames": 3, "selected_frames": 3, "visual_duplicates_removed": 0}),
    )

    def slow_engine(_path: Path):
        time.sleep(0.02)
        return _output("正文内容足够长", 0.9)

    batch = VisualBatchBudget(soft_limit=10, hard_limit=10, deadline=time.monotonic() + 10)
    result = process_visual_video(video, tmp_path / "deadline-visual", settings, batch, engine=slow_engine)
    assert result["visual_text_status"] == "budget_exhausted"
    assert result["ocr_frames"] == 1
    assert batch.total_ocr_frames == 1
