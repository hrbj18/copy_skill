from __future__ import annotations

from pathlib import Path

from douyin_intelligence.config import load_config
from douyin_intelligence.face_metrics import (
    FACE_FREE,
    FACE_HEAVY,
    FACE_LOW,
    FACE_UNAVAILABLE,
    FaceDetector,
    ascii_model_path,
    classify_face,
    ensure_yunet_model,
    face_frame_hits,
    frame_hit_flags,
    imread_unicode,
)


def test_classify_face_boundaries_are_deterministic() -> None:
    assert classify_face(0.0) == FACE_FREE
    assert classify_face(0.05) == FACE_FREE
    assert classify_face(0.0500001) == FACE_LOW
    assert classify_face(0.15) == FACE_LOW
    assert classify_face(0.1500001) == FACE_HEAVY
    assert classify_face(1.0) == FACE_HEAVY
    assert classify_face(-1.0) == FACE_FREE


def test_face_frame_hits_use_area_threshold_and_are_deterministic() -> None:
    # Frame is 100x100 => 10 000 px; a 20x20 box is 4% (>= 1.5%), a 5x5 box is 0.25% (ignored).
    sizes = [(100, 100), (100, 100), (100, 100)]
    detections = [
        [(0.0, 0.0, 20.0, 20.0)],
        [(0.0, 0.0, 5.0, 5.0)],
        [],
    ]
    ratio, max_area = face_frame_hits(detections, sizes, min_area_ratio=0.015)
    assert ratio == round(1 / 3, 6)
    assert max_area == round(0.04, 6)
    assert face_frame_hits(detections, sizes, min_area_ratio=0.015) == (ratio, max_area)
    assert frame_hit_flags(detections, sizes, min_area_ratio=0.015) == [True, False, False]


def test_face_frame_hits_handles_zero_frames() -> None:
    assert face_frame_hits([], []) == (0.0, 0.0)


def test_ensure_yunet_model_returns_none_when_disabled_and_absent(tmp_path: Path) -> None:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    assert ensure_yunet_model(config, download=False) is None


def test_face_detector_degrades_to_unavailable_without_backend(tmp_path: Path) -> None:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    config["jobs"]["material_replication"]["face"]["auto_download"] = False
    detector = FaceDetector(config)
    status = detector.status()
    assert status["status"] == FACE_UNAVAILABLE
    assert status["model_present"] is False
    result = detector.run(tmp_path / "video.mp4", 12.0, tmp_path / "cache", tmp_path / "temp")
    assert result["face_class"] == FACE_UNAVAILABLE
    assert result["status"] == FACE_UNAVAILABLE
    assert result["sampled_frames"] == 0


def test_ascii_model_path_bridges_non_ascii_project_root(tmp_path: Path) -> None:
    # An ASCII path is returned unchanged.
    ascii_file = tmp_path / "plain.onnx"
    ascii_file.write_bytes(b"model")
    assert ascii_model_path(ascii_file) == ascii_file
    # A non-ASCII path is bridged to an ASCII location with identical bytes.
    chinese_dir = tmp_path / "刘宇钊" / "face"
    chinese_dir.mkdir(parents=True)
    model = chinese_dir / "face_detection_yunet_2023mar.onnx"
    model.write_bytes(b"onnx-model-bytes")
    bridged = ascii_model_path(model)
    assert bridged is not None
    assert str(bridged).isascii()
    assert bridged.read_bytes() == b"onnx-model-bytes"


def test_imread_unicode_reads_non_ascii_frame_path(tmp_path: Path) -> None:
    # cv2.imread silently returns None for non-ASCII paths; imread_unicode must
    # still decode the sampled frame written under the Chinese project root.
    import cv2
    import numpy as np

    image = np.zeros((8, 12, 3), dtype=np.uint8)
    image[:, :] = (10, 20, 30)
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    target = tmp_path / "刘宇钊素材" / "帧-01.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(buffer.tobytes())

    loaded = imread_unicode(target)
    assert loaded is not None
    assert loaded.shape == (8, 12, 3)
    assert int(loaded[0, 0, 0]) == 10

    gray = imread_unicode(target, cv2.IMREAD_GRAYSCALE)
    assert gray is not None
    assert gray.ndim == 2


def test_imread_unicode_handles_ascii_and_missing_paths(tmp_path: Path) -> None:
    import cv2
    import numpy as np

    image = np.full((4, 6, 3), 7, dtype=np.uint8)
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    ascii_path = tmp_path / "plain.png"
    ascii_path.write_bytes(buffer.tobytes())
    loaded = imread_unicode(ascii_path)
    assert loaded is not None
    assert loaded.shape == (4, 6, 3)
    assert imread_unicode(tmp_path / "missing.png") is None
