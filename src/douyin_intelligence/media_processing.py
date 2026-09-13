from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .config import resolve_path
from .exporter import atomic_write_json
from .media_tools import resolve_media_tool


MEDIA_PROCESS_TIMEOUT_SECONDS = 180

# Files faster-whisper needs in a materialized model directory.  ``model.bin``
# is the CTranslate2 weights; ``config.json`` carries the model descriptor.
_WHISPER_ESSENTIAL_FILES = ("model.bin", "config.json")


class TranscriptionModelError(RuntimeError):
    """Raised when the faster-whisper model cannot be loaded or downloaded."""


def _whisper_model_dir_candidates(model_name: str, cache_path: Path | None) -> list[Path]:
    """Directories where a materialized faster-whisper model may live."""
    candidates: list[Path] = []
    direct = Path(model_name)
    if direct.is_dir():
        candidates.append(direct)
    if cache_path is not None:
        candidates.append(Path(cache_path) / model_name)
    return candidates


def _whisper_model_present(model_name: str, cache_path: Path | None) -> bool:
    for candidate in _whisper_model_dir_candidates(model_name, cache_path):
        if candidate.is_dir() and (candidate / "model.bin").is_file():
            return True
    # HuggingFace snapshot layout: <cache>/models--<org>--<repo>/snapshots/<rev>/...
    if cache_path is not None and Path(cache_path).is_dir():
        if any(Path(cache_path).glob("**/model.bin")):
            return True
    return False


def _asr_cache_hint() -> str:
    endpoint = str(os.environ.get("HF_ENDPOINT") or "").strip()
    if endpoint:
        return f"当前 HF_ENDPOINT={endpoint}"
    return "可设置 HF_ENDPOINT=https://hf-mirror.com 使用镜像后重试"


def _hub_offline_forced() -> bool:
    """True when the environment explicitly asks the Hub client to stay offline."""
    return str(os.environ.get("HF_HUB_OFFLINE") or "").strip().lower() in {"1", "true", "yes", "on"}


def _whisper_offline_required(model_name: str, cache_path: Path | None) -> bool:
    """Decide whether ``WhisperModel`` must load without any Hub network check.

    Offline-first: when the weights are already materialized under
    ``model_cache`` (a ``<model>/`` directory or a HuggingFace ``models--*``
    snapshot), force ``local_files_only=True`` so an intercepting HTTP proxy
    cannot turn a local cache hit into an HTTP 502 ``Bad Gateway`` (faster-whisper
    calls ``snapshot_download`` for a ``repo_info`` check even when
    ``download_root`` is set).  ``HF_HUB_OFFLINE`` also forces the offline path.
    When no local weights exist the previous behaviour is preserved and the Hub
    download is still allowed.
    """
    if _hub_offline_forced():
        return True
    return _whisper_model_present(model_name, cache_path)


def faster_whisper_status(config: dict[str, Any]) -> dict[str, Any]:
    """Offline readiness probe for the faster-whisper ASR backend.

    Importability is not enough: ``WhisperModel`` must find real weights.  This
    checks the configured ``model_cache`` (materialized ``<model>/`` directory
    or HuggingFace ``models--*`` snapshot layout) without any network access,
    and returns an actionable reason when the model is missing.
    """
    settings = (config.get("materials") or {}).get("transcription") or {}
    model_name = str(settings.get("model") or "base")
    raw_cache = settings.get("model_cache")
    cache_path = resolve_path(raw_cache) if raw_cache else None
    cache_text = str(cache_path) if cache_path is not None else "(未配置)"
    base = {"model": model_name, "model_cache": cache_text}

    if importlib.util.find_spec("faster_whisper") is None:
        return {**base, "ready": False, "model_present": False,
                "reason": "faster-whisper 未安装（pip install faster-whisper）"}
    if not bool(settings.get("enabled", True)):
        return {**base, "ready": False, "model_present": False, "reason": "转写已在配置中禁用"}

    present = _whisper_model_present(model_name, cache_path)
    if present:
        return {**base, "ready": True, "model_present": True, "reason": ""}
    return {
        **base, "ready": False, "model_present": False,
        "reason": (
            f"ASR 模型 '{model_name}' 未下载，缓存目录 {cache_text} 内无可用模型文件（model.bin）。"
            f"请预置模型或联网首次运行以下载；{_asr_cache_hint()}"
        ),
    }



def _run_media_process(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False, timeout=MEDIA_PROCESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, "", f"media process timed out after {MEDIA_PROCESS_TIMEOUT_SECONDS} seconds")


class CheckpointTranscriber:
    def __init__(self, config: dict[str, Any], enabled: bool = True):
        self.settings = config["materials"]["transcription"]
        self.enabled = enabled and bool(self.settings.get("enabled", True))
        self.model = None
        self.ffmpeg = resolve_media_tool(config, "ffmpeg")

    def _model_error(self, detail: str, *, offline: bool = False) -> str:
        model_name = str(self.settings.get("model") or "base")
        cache_setting = self.settings.get("model_cache")
        cache_path = str(resolve_path(cache_setting)) if cache_setting else "(未配置)"
        mode = "（离线加载 local_files_only=True）" if offline else ""
        return (
            f"ASR 模型不可用/下载失败{mode}：模型 '{model_name}'，缓存目录 {cache_path}。"
            f"原始错误：{detail}。{_asr_cache_hint()}"
        )

    def _load(self) -> None:
        if self.model is not None:
            return
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # missing dependency
            raise TranscriptionModelError(self._model_error(f"faster-whisper 未安装：{exc}")) from exc

        model_name = str(self.settings.get("model") or "base")
        cache_setting = self.settings.get("model_cache")
        cache_path = resolve_path(cache_setting) if cache_setting else None
        # Offline-first: when the weights are already cached, never let an
        # intercepting HTTP proxy break a cache hit with an HTTP 502.
        offline = _whisper_offline_required(model_name, cache_path)
        try:
            self.model = WhisperModel(
                model_name,
                device=str(self.settings.get("device") or "cpu"),
                compute_type=str(self.settings.get("compute_type") or "int8"),
                cpu_threads=int(self.settings.get("cpu_threads") or 2),
                num_workers=1,
                download_root=str(cache_path) if cache_path is not None else None,
                local_files_only=offline,
            )
        except Exception as exc:  # download/load failure, e.g. HF 502 Bad Gateway
            raise TranscriptionModelError(self._model_error(str(exc), offline=offline)) from exc

    def _one(self, path: Path, offset: float) -> dict[str, Any]:
        self._load()
        segments, info = self.model.transcribe(
            str(path), language=str(self.settings.get("language") or "zh"),
            beam_size=int(self.settings.get("beam_size") or 5), vad_filter=True,
        )
        rows = [
            {"start": round(item.start + offset, 2), "end": round(item.end + offset, 2), "text": item.text.strip()}
            for item in segments if item.text.strip()
        ]
        return {"status": "success" if rows else "no_speech", "language": getattr(info, "language", "zh"), "segments": rows}

    def run(self, video: Path, cache_dir: Path, temp_dir: Path, *, legacy_cache: Path | None = None) -> dict[str, Any]:
        final_path = cache_dir / "transcript.json"
        if final_path.is_file():
            result = json.loads(final_path.read_text(encoding="utf-8"))
            result["cache_hit"] = True
            return result
        if legacy_cache and legacy_cache.is_file():
            result = json.loads(legacy_cache.read_text(encoding="utf-8"))
            result["cache_hit"] = True
            result["migrated_from_legacy"] = True
            atomic_write_json(final_path, result)
            return result
        if not self.enabled:
            return {"status": "skipped", "segments": [], "text": "", "cache_hit": False}

        try:
            self._load()
        except TranscriptionModelError as exc:
            # Surface a model-provisioning problem instead of a bare HTTP 502.
            return {"status": "error", "error": str(exc), "error_kind": "model_unavailable", "segments": [], "text": "", "cache_hit": False}

        cache_dir.mkdir(parents=True, exist_ok=True)
        parts_dir = cache_dir / "transcript_parts"
        parts_dir.mkdir(parents=True, exist_ok=True)
        audio_dir = temp_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        chunk_seconds = int(self.settings.get("chunk_seconds") or 180)
        pattern = str(audio_dir / "part-%04d.wav")
        command = [
            self.ffmpeg, "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
            "-f", "segment", "-segment_time", str(chunk_seconds), "-reset_timestamps", "1", pattern,
        ]
        completed = _run_media_process(command)
        if completed.returncode:
            return {"status": "error", "error": f"音频切片失败：{completed.stderr.strip()[:300]}", "segments": [], "text": "", "cache_hit": False}

        part_files = sorted(audio_dir.glob("part-*.wav"))
        all_segments: list[dict[str, Any]] = []
        statuses: list[str] = []
        language = "zh"
        try:
            for index, audio in enumerate(part_files):
                checkpoint = parts_dir / f"part-{index:04d}.json"
                if checkpoint.is_file():
                    part = json.loads(checkpoint.read_text(encoding="utf-8"))
                else:
                    part = self._one(audio, index * chunk_seconds)
                    atomic_write_json(checkpoint, part)
                statuses.append(str(part.get("status") or "error"))
                language = str(part.get("language") or language)
                all_segments.extend(part.get("segments") or [])
                audio.unlink(missing_ok=True)
        except TranscriptionModelError as exc:
            return {"status": "error", "error": str(exc), "error_kind": "model_unavailable", "segments": all_segments, "text": "。".join(item["text"] for item in all_segments), "cache_hit": False}
        except Exception as exc:
            return {"status": "error", "error": str(exc)[:300], "error_kind": "transcription_failed", "segments": all_segments, "text": "。".join(item["text"] for item in all_segments), "cache_hit": False}
        finally:
            for audio in audio_dir.glob("part-*.wav"):
                audio.unlink(missing_ok=True)
            try:
                audio_dir.rmdir()
            except OSError:
                pass

        status = "success" if all_segments else "no_speech" if statuses and all(item == "no_speech" for item in statuses) else "no_speech"
        result = {"status": status, "language": language, "segments": all_segments, "text": "。".join(item["text"] for item in all_segments), "chunk_count": len(part_files), "cache_hit": False}
        atomic_write_json(final_path, result)
        return result


class KeyframeOCR:
    def __init__(self, config: dict[str, Any]):
        self.settings = config["materials"].get("ocr") or {}
        self.engine = None
        self.ffmpeg = resolve_media_tool(config, "ffmpeg")

    def _load(self) -> None:
        if self.engine is None:
            from rapidocr import RapidOCR
            self.engine = RapidOCR()

    def run(self, video: Path, duration: float, cache_dir: Path, temp_dir: Path) -> dict[str, Any]:
        cache_path = cache_dir / "ocr.json"
        if cache_path.is_file():
            result = json.loads(cache_path.read_text(encoding="utf-8"))
            result["cache_hit"] = True
            return result
        if not self.settings.get("enabled", True):
            return {"status": "disabled", "items": []}
        frames = temp_dir / "frames"
        frames.mkdir(parents=True, exist_ok=True)
        interval = max(1, int(self.settings.get("frame_interval_seconds") or 3))
        max_frames = max(1, int(self.settings.get("max_frames") or 20))
        width = max(320, int(self.settings.get("max_width") or 960))
        pattern = str(frames / "frame-%04d.jpg")
        command = [self.ffmpeg, "-y", "-v", "error", "-i", str(video), "-vf", f"fps=1/{interval},scale='min({width},iw)':-2", "-frames:v", str(max_frames), pattern]
        completed = _run_media_process(command)
        if completed.returncode:
            return {"status": "error", "error": f"关键帧提取失败：{completed.stderr.strip()[:300]}", "items": []}
        items = []
        seen: set[str] = set()
        # ``frames_with_text`` counts *every* sampled frame that carried text,
        # before the fingerprint dedupe below; ``items`` stays the deduped text
        # catalogue.  The two are deliberately separate: the deduped list cannot
        # answer "what fraction of frames had on-screen text", which is what the
        # material OCR-coverage gate needs.
        frames_with_text = 0
        frames_scanned = 0
        try:
            self._load()
            for index, frame in enumerate(sorted(frames.glob("frame-*.jpg"))):
                frames_scanned += 1
                output = self.engine(frame)
                text = " ".join(value.strip() for value in (getattr(output, "txts", None) or []) if value and value.strip())
                if text:
                    frames_with_text += 1
                fingerprint = re.sub(r"\W+", "", text).casefold()
                if text and fingerprint and fingerprint not in seen:
                    seen.add(fingerprint)
                    items.append({"time": round(index * interval, 2), "text": text})
        except Exception as exc:
            return {
                "status": "error", "error": str(exc)[:300], "items": items,
                "frames_scanned": frames_scanned, "frames_with_text": frames_with_text,
            }
        finally:
            for frame in frames.glob("frame-*.jpg"):
                frame.unlink(missing_ok=True)
            try:
                frames.rmdir()
            except OSError:
                pass
        result = {
            "status": "success" if items else "no_text",
            "items": items,
            "sample_interval_seconds": interval,
            "sampled_frames": min(max_frames, int(duration // interval) + 1),
            "frames_scanned": frames_scanned,
            "frames_with_text": frames_with_text,
            "cache_hit": False,
        }
        atomic_write_json(cache_path, result)
        return result
