"""Bounded face-existence detection for the material-replication workflow.

Only presence metrics are produced (frame hit ratio and maximum box area
ratio).  No face embeddings, identity recognition or face crops are ever
written.  The detector falls back between two OpenCV backends and, when no
backend is available, degrades to ``unavailable`` instead of crashing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

from .exporter import atomic_write_json
from .media_tools import resolve_media_tool


FACE_FREE = "face_free"
FACE_LOW = "low_face"
FACE_HEAVY = "face_heavy"
FACE_UNAVAILABLE = "unavailable"

# The four values above are the frozen face classes used across the workflow.
FACE_CLASSES = (FACE_FREE, FACE_LOW, FACE_HEAVY, FACE_UNAVAILABLE)

# Detector backends, in the frozen priority order from the design.
BACKEND_YUNET = "opencv_yunet"
BACKEND_DNN = "opencv_dnn"
BACKEND_UNAVAILABLE = "unavailable"

MEDIA_PROCESS_TIMEOUT_SECONDS = 180
_MODEL_DOWNLOAD_CEILING_BYTES = 64 * 1024 * 1024

#: A sample whose emitted frames are below this share of the expected count is a
#: *severe* truncation: the sample covers less than half the clip, so a
#: ``face_free`` / ``low_face`` reading is not a statement about the whole video.
#: Such a result is downgraded to ``unavailable`` (reason ``sample_truncated``)
#: instead of being trusted.  A truncated-but-≥0.5 sample keeps its class but is
#: flagged ``low_confidence`` with an explicit ``sample_coverage``.
SAMPLE_TRUNCATION_SEVERE_RATIO = 0.5


def truncated_face_class(face: dict[str, Any]) -> dict[str, Any]:
    """Return ``face`` with a severe sample-truncation downgrade applied.

    Independent of where the face result came from (real detector or an injected
    runner), a sample covering less than ``SAMPLE_TRUNCATION_SEVERE_RATIO`` of the
    clip must not yield a trusted ``face_free`` / ``low_face``: the class is
    downgraded to ``unavailable`` (reason ``sample_truncated``) and marked
    ``low_confidence``.  A truncated-but-≥threshold sample is left alone (the
    detector already flags it ``low_confidence`` with a ``sample_coverage``).
    """
    if not isinstance(face, dict) or not face.get("truncated"):
        return face
    try:
        expected = int(face.get("expected_frames") or 0)
    except (TypeError, ValueError):
        expected = 0
    emitted = face.get("emitted_frames")
    if emitted is None:
        emitted = face.get("sampled_frames") or 0
    try:
        emitted = int(emitted)
    except (TypeError, ValueError):
        emitted = 0
    if expected <= 0 or emitted >= expected * SAMPLE_TRUNCATION_SEVERE_RATIO:
        return face
    updated = dict(face)
    updated["face_class"] = FACE_UNAVAILABLE
    updated["face_class_reason"] = str(updated.get("face_class_reason") or "") or "sample_truncated"
    updated["low_confidence"] = True
    updated["sample_coverage"] = round(emitted / expected, 6)
    return updated


def _project_path(config: dict[str, Any], value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    root = Path(str(config.get("_project_root") or Path(__file__).resolve().parents[2]))
    return root / path


def face_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Return the frozen ``jobs.material_replication.face`` settings block."""
    return (config.get("jobs") or {}).get("material_replication", {}).get("face") or {}


def _run_media_process(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False, timeout=MEDIA_PROCESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, "", f"media process timed out after {MEDIA_PROCESS_TIMEOUT_SECONDS} seconds")


def classify_face(face_frame_ratio: float, *, free_max: float = 0.05, low_max: float = 0.15) -> str:
    """Map a frame-hit ratio to one of the frozen face classes.

    ``<= free_max`` is ``face_free``; ``<= low_max`` is ``low_face``; anything
    larger is ``face_heavy``.  The function is deterministic and side-effect
    free so it can be asserted directly in offline tests.
    """
    ratio = max(0.0, float(face_frame_ratio))
    if ratio <= float(free_max):
        return FACE_FREE
    if ratio <= float(low_max):
        return FACE_LOW
    return FACE_HEAVY


def frame_hit_flags(
    detections: list[list[tuple[float, float, float, float]]],
    frame_sizes: list[tuple[int, int]],
    *,
    min_area_ratio: float = 0.015,
) -> list[bool]:
    """Return, per sampled frame, whether a face box clears ``min_area_ratio``."""
    flags: list[bool] = []
    for index, size in enumerate(frame_sizes):
        boxes = detections[index] if index < len(detections) else []
        width = max(1.0, float(size[0]))
        height = max(1.0, float(size[1]))
        frame_area = width * height
        hit = False
        for box in boxes:
            area = max(0.0, float(box[2])) * max(0.0, float(box[3]))
            if frame_area > 0 and area / frame_area >= float(min_area_ratio):
                hit = True
                break
        flags.append(hit)
    return flags


def face_frame_hits(
    detections: list[list[tuple[float, float, float, float]]],
    frame_sizes: list[tuple[int, int]],
    *,
    min_area_ratio: float = 0.015,
) -> tuple[float, float]:
    """Return ``(face_frame_ratio, max_face_area_ratio)`` for one video.

    Only boxes whose area clears ``min_area_ratio`` of the frame count as
    present.  With zero sampled frames both metrics are ``0.0``.
    """
    flags = frame_hit_flags(detections, frame_sizes, min_area_ratio=min_area_ratio)
    total = len(flags)
    if total == 0:
        return (0.0, 0.0)
    hit_ratio = sum(1 for flag in flags if flag) / total
    max_ratio = 0.0
    for index, size in enumerate(frame_sizes):
        boxes = detections[index] if index < len(detections) else []
        area = max(1.0, float(size[0]) * float(size[1]))
        for box in boxes:
            ratio = max(0.0, float(box[2])) * max(0.0, float(box[3])) / area
            if ratio > max_ratio:
                max_ratio = ratio
    return (round(hit_ratio, 6), round(max_ratio, 6))


def imread_unicode(path: Path | str, flag: int | None = None) -> Any:
    """Read an image with OpenCV in a path-encoding independent way.

    OpenCV 5.0 fails to open image files whose path contains non-ASCII
    characters (this project root contains Chinese characters), silently
    returning ``None`` for every sampled frame and collapsing the face
    metrics to ``unavailable``.  Reading the raw bytes through
    ``numpy.fromfile`` and decoding them in memory with ``cv2.imdecode``
    bypasses the Windows code-page limitation, so frames written under the
    project root are always readable.  ASCII paths keep using
    ``cv2.imread`` so the common case stays unchanged.

    Returns ``None`` when OpenCV/numpy is unavailable or decoding fails.
    """
    try:
        import cv2
    except Exception:
        return None
    if flag is None:
        flag = cv2.IMREAD_COLOR
    text = str(path)
    if text.isascii():
        return cv2.imread(text, flag)
    try:
        import numpy as np
        raw = np.fromfile(text, dtype=np.uint8)
    except Exception:
        # numpy is unavailable: fall back to imread and accept its limitation.
        return cv2.imread(text, flag)
    if raw is None or getattr(raw, "size", 0) == 0:
        return None
    return cv2.imdecode(raw, flag)


def _download_to(url: str, destination: Path, *, timeout: int, maximum_bytes: int) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "copy-skill-material-replication/1.0"})
        size = 0
        with urllib.request.urlopen(request, timeout=timeout) as response, temporary.open("wb") as stream:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > maximum_bytes:
                    raise ValueError("模型文件超过允许的最大体积")
                stream.write(chunk)
        if size <= 0:
            raise ValueError("模型下载内容为空")
        os.replace(temporary, destination)
        return size
    finally:
        temporary.unlink(missing_ok=True)


def yunet_model_path(config: dict[str, Any]) -> Path:
    """Return the configured YuNet path without touching the network."""
    settings = face_settings(config)
    yunet = settings.get("yunet") or {}
    file_name = str(yunet.get("file") or "face_detection_yunet_2023mar.onnx")
    model_root = _project_path(config, settings.get("model_root") or "data/models/face")
    return model_root / file_name


def _ascii_temp_root() -> Path | None:
    """Return an ASCII-only temp directory, or ``None`` when none is available.

    OpenCV 5.0's ONNX importer fails to open models whose path contains
    non-ASCII characters (a Windows code-page limitation).  This project root
    contains Chinese characters, so a model read through OpenCV must be passed
    via an ASCII absolute path.
    """
    candidates: list[str] = []
    for value in (tempfile.gettempdir(), os.environ.get("TEMP"), os.environ.get("TMP")):
        if value:
            candidates.append(str(value))
    for candidate in candidates:
        if candidate.isascii():
            return Path(candidate)
    return None


def ascii_model_path(model: Path) -> Path | None:
    """Return an ASCII path to ``model`` (hardlink/copy when needed)."""
    path = Path(model)
    if str(path).isascii():
        return path
    root = _ascii_temp_root()
    if root is None:
        return None
    cache = root / "copy-skill-face-models"
    try:
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / path.name
        if not target.is_file() or target.stat().st_size != path.stat().st_size:
            try:
                os.link(path, target)
            except OSError:
                shutil.copy2(path, target)
        return target
    except OSError:
        return None


def ensure_yunet_model(config: dict[str, Any], *, download: bool | None = None) -> Path | None:
    """Return a usable YuNet model path, downloading at most once if allowed.

    A path is returned only when the file exists and (when ``expected_bytes``
    is configured) matches the expected size exactly.  When the model is
    missing and downloads are disabled or fail, ``None`` is returned so the
    caller degrades to ``unavailable`` instead of crashing.
    """
    settings = face_settings(config)
    yunet = settings.get("yunet") or {}
    path = yunet_model_path(config)
    expected = int(yunet.get("expected_bytes") or 0)
    if path.is_file() and (expected <= 0 or path.stat().st_size == expected):
        return path
    enabled = bool(settings.get("auto_download", True)) if download is None else bool(download)
    if not enabled:
        return None
    url = str(yunet.get("url") or "").strip()
    if not url:
        return None
    timeout = max(1, int(settings.get("download_timeout_seconds") or 60))
    maximum = expected if expected > 0 else _MODEL_DOWNLOAD_CEILING_BYTES
    try:
        _download_to(url, path, timeout=timeout, maximum_bytes=maximum)
    except Exception:
        return None
    if not path.is_file():
        return None
    if expected > 0 and path.stat().st_size != expected:
        path.unlink(missing_ok=True)
        return None
    return path


class FaceDetector:
    """Two-level OpenCV face detector with a graceful ``unavailable`` fallback."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.settings = face_settings(config)
        self._backend = BACKEND_UNAVAILABLE
        self._status = BACKEND_UNAVAILABLE
        self._detector: Any = None
        self._net: Any = None
        self._load_attempted = False

    @property
    def backend(self) -> str:
        return self._backend

    def _priority(self) -> list[str]:
        priority = self.settings.get("backend_priority") or [BACKEND_YUNET, BACKEND_DNN]
        return [str(item) for item in priority]

    def _load_yunet(self) -> bool:
        model = ensure_yunet_model(self.config)
        if model is None:
            return False
        usable = ascii_model_path(model)
        if usable is None:
            return False
        try:
            import cv2
        except Exception:
            return False
        try:
            self._detector = cv2.FaceDetectorYN.create(
                str(usable), "", (320, 320), float(self.settings.get("score_threshold") or 0.9), 0.3, 5000,
            )
        except Exception:
            self._detector = None
            return False
        return self._detector is not None

    def _load_dnn(self) -> bool:
        dnn = self.settings.get("dnn") or {}
        prototxt = str(dnn.get("prototxt") or "").strip()
        weights = str(dnn.get("weights") or "").strip()
        if not prototxt or not weights:
            return False
        prototxt_path = ascii_model_path(_project_path(self.config, prototxt))
        weights_path = ascii_model_path(_project_path(self.config, weights))
        if prototxt_path is None or weights_path is None or not prototxt_path.is_file() or not weights_path.is_file():
            return False
        try:
            import cv2
        except Exception:
            return False
        try:
            self._net = cv2.dnn.readNetFromCaffe(str(prototxt_path), str(weights_path))
        except Exception:
            self._net = None
            return False
        return self._net is not None

    def _load(self) -> bool:
        if self._load_attempted:
            return self._detector is not None or self._net is not None
        self._load_attempted = True
        for backend in self._priority():
            if backend == BACKEND_YUNET and self._load_yunet():
                self._backend = BACKEND_YUNET
                self._status = "ok"
                return True
            if backend == BACKEND_DNN and self._load_dnn():
                self._backend = BACKEND_DNN
                self._status = "ok"
                return True
        self._backend = BACKEND_UNAVAILABLE
        self._status = BACKEND_UNAVAILABLE
        return False

    def detect_frame(self, image: Any) -> list[tuple[float, float, float, float]]:
        """Detect face boxes ``(x, y, w, h)`` in pixels for one BGR frame."""
        if not self._load():
            return []
        height = int(image.shape[0])
        width = int(image.shape[1])
        if self._backend == BACKEND_YUNET and self._detector is not None:
            try:
                self._detector.setInputSize((width, height))
                _, faces = self._detector.detect(image)
            except Exception:
                return []
            if faces is None:
                return []
            return [(float(row[0]), float(row[1]), float(row[2]), float(row[3])) for row in faces]
        if self._net is not None:
            try:
                import cv2
                import numpy as np
                blob = cv2.dnn.blobFromImage(image, 1.0, (300, 300), (104.0, 177.0, 123.0))
                self._net.setInput(blob)
                detections = self._net.forward()
            except Exception:
                return []
            threshold = float(self.settings.get("score_threshold") or 0.9)
            boxes: list[tuple[float, float, float, float]] = []
            for index in range(detections.shape[2]):
                if float(detections[0, 0, index, 2]) < threshold:
                    continue
                box = detections[0, 0, index, 3:7] * np.array([width, height, width, height])
                x1, y1, x2, y2 = box
                boxes.append((float(x1), float(y1), float(x2 - x1), float(y2 - y1)))
            return boxes
        return []

    def _unavailable_result(self, interval: int, *, expected_frames: int = 0) -> dict[str, Any]:
        return {
            "backend": BACKEND_UNAVAILABLE,
            "status": BACKEND_UNAVAILABLE,
            "face_frame_ratio": 0.0,
            "max_face_area_ratio": 0.0,
            "face_class": FACE_UNAVAILABLE,
            "sampled_frames": 0,
            "expected_frames": int(expected_frames),
            "emitted_frames": 0,
            "sample_coverage": None,
            "truncated": False,
            "low_confidence": False,
            "face_class_reason": "",
            "face_per_frame": [],
            "sample_interval_seconds": interval,
            "cache_hit": False,
        }

    def run(self, video: Path, duration: float, cache_dir: Path, temp_dir: Path) -> dict[str, Any]:
        """Sample ``video`` at 1 fps and return face metrics, cached by params."""
        interval = max(1, int(self.settings.get("sampling_interval_seconds") or 1))
        max_frames = max(1, int(self.settings.get("max_frames") or 120))
        width = max(320, int(self.settings.get("frame_width") or 960))
        min_area = float(self.settings.get("min_face_area_ratio") or 0.015)
        # How many frames ``fps=1/interval`` SHOULD emit for this duration, capped
        # by ``max_frames``.  A corrupt/truncated source makes ffmpeg emit far
        # fewer; using the shrunken count as the ratio denominator (the old
        # behaviour) silently inflates every face ratio, so the shortfall is now
        # recorded explicitly instead.
        expected_frames = min(max_frames, int(round(max(0.0, float(duration)) / interval))) if interval > 0 else 0
        raw_key = f"{Path(video).stem}|{interval}|{max_frames}|{width}|{min_area}"
        import hashlib
        cache_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"face-{cache_key}.json"
        if cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                cached["cache_hit"] = True
                return cached
            except (OSError, json.JSONDecodeError):
                pass
        if not self._load():
            return self._unavailable_result(interval, expected_frames=expected_frames)

        frames_dir = Path(temp_dir) / f"face-frames-{cache_key}"
        frames_dir.mkdir(parents=True, exist_ok=True)
        pattern = str(frames_dir / "frame-%04d.jpg")
        ffmpeg = resolve_media_tool(self.config, "ffmpeg")
        command = [
            ffmpeg, "-y", "-v", "error", "-i", str(video),
            "-vf", f"fps=1/{interval},scale='min({width},iw)':-2",
            "-frames:v", str(max_frames), pattern,
        ]
        completed = _run_media_process(command)
        frame_files = sorted(frames_dir.glob("frame-*.jpg"))
        emitted_frames = len(frame_files)
        sample_coverage = (emitted_frames / expected_frames) if expected_frames > 0 else None
        truncation_reason = ""
        if completed.returncode != 0:
            truncation_reason = (
                f"抽帧命令非零退出（{completed.returncode}）：{(completed.stderr or '').strip()[:200]}"
            )
        elif expected_frames > 0 and emitted_frames < expected_frames:
            truncation_reason = f"预期 {expected_frames} 帧，实际仅解出 {emitted_frames} 帧（源可能损坏/截断）"
        truncated = bool(truncation_reason)
        detections: list[list[tuple[float, float, float, float]]] = []
        sizes: list[tuple[int, int]] = []
        try:
            if completed.returncode == 0 and frame_files:
                for frame_file in frame_files:
                    image = imread_unicode(frame_file)
                    if image is None:
                        continue
                    sizes.append((int(image.shape[1]), int(image.shape[0])))
                    detections.append(self.detect_frame(image))
        finally:
            for frame_file in frame_files:
                frame_file.unlink(missing_ok=True)
            try:
                frames_dir.rmdir()
            except OSError:
                pass

        if not sizes:
            result = self._unavailable_result(interval, expected_frames=expected_frames)
            result["status"] = "error"
            result["truncated"] = True
            result["low_confidence"] = True
            result["emitted_frames"] = emitted_frames
            result["sample_coverage"] = round(sample_coverage, 6) if sample_coverage is not None else None
            result["face_class_reason"] = "sample_truncated"
            result["error"] = f"无法从视频中提取人脸采样帧：{(completed.stderr or '').strip()[:300]}"
            result["warning"] = truncation_reason or result["error"]
            return result

        free_max = float(self.settings.get("free_max_ratio") or 0.05)
        low_max = float(self.settings.get("low_max_ratio") or 0.15)
        ratio, max_area = face_frame_hits(detections, sizes, min_area_ratio=min_area)
        flags = frame_hit_flags(detections, sizes, min_area_ratio=min_area)
        result = {
            "backend": self._backend,
            "status": "ok",
            "face_frame_ratio": ratio,
            "max_face_area_ratio": max_area,
            "face_class": classify_face(ratio, free_max=free_max, low_max=low_max),
            "sampled_frames": len(sizes),
            "expected_frames": int(expected_frames),
            "emitted_frames": emitted_frames,
            "sample_coverage": round(sample_coverage, 6) if sample_coverage is not None else None,
            "truncated": truncated,
            "low_confidence": truncated,
            "face_class_reason": "",
            "face_per_frame": flags,
            "sample_interval_seconds": interval,
            "cache_hit": False,
        }
        if truncated:
            # Explicit, machine-readable marker plus a human note: the face
            # ratio above was computed over ``sampled_frames``, NOT over
            # ``expected_frames``, and the caller must not read it as complete.
            result["warning"] = truncation_reason
            result = truncated_face_class(result)
        atomic_write_json(cache_path, result)
        return result

    def status(self) -> dict[str, Any]:
        """Report backend availability for ``doctor`` without downloading."""
        model = yunet_model_path(self.config)
        expected = int((self.settings.get("yunet") or {}).get("expected_bytes") or 0)
        model_present = model.is_file() and (expected <= 0 or model.stat().st_size == expected)
        try:
            import cv2  # noqa: F401
            cv2_ok = True
        except Exception:
            cv2_ok = False
        backend = BACKEND_UNAVAILABLE
        ascii_ok = str(model).isascii() or _ascii_temp_root() is not None
        if cv2_ok and model_present and ascii_ok:
            backend = BACKEND_YUNET
        elif cv2_ok and ascii_ok:
            dnn = self.settings.get("dnn") or {}
            prototxt = _project_path(self.config, str(dnn.get("prototxt") or "")) if dnn.get("prototxt") else None
            weights = _project_path(self.config, str(dnn.get("weights") or "")) if dnn.get("weights") else None
            if prototxt is not None and weights is not None and prototxt.is_file() and weights.is_file():
                backend = BACKEND_DNN
        return {
            "backend": backend,
            "status": "ok" if backend != BACKEND_UNAVAILABLE else BACKEND_UNAVAILABLE,
            "model_present": model_present,
        }
