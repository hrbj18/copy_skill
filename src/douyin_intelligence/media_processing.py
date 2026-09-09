from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .config import resolve_path
from .exporter import atomic_write_json
from .media_tools import resolve_media_tool


MEDIA_PROCESS_TIMEOUT_SECONDS = 180


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

    def _load(self) -> None:
        if self.model is None:
            from faster_whisper import WhisperModel
            self.model = WhisperModel(
                str(self.settings.get("model") or "base"),
                device=str(self.settings.get("device") or "cpu"),
                compute_type=str(self.settings.get("compute_type") or "int8"),
                cpu_threads=int(self.settings.get("cpu_threads") or 2),
                num_workers=1,
                download_root=str(resolve_path(self.settings["model_cache"])),
            )

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
        except Exception as exc:
            return {"status": "error", "error": str(exc)[:300], "segments": all_segments, "text": "。".join(item["text"] for item in all_segments), "cache_hit": False}
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
        try:
            self._load()
            for index, frame in enumerate(sorted(frames.glob("frame-*.jpg"))):
                output = self.engine(frame)
                text = " ".join(value.strip() for value in (getattr(output, "txts", None) or []) if value and value.strip())
                fingerprint = re.sub(r"\W+", "", text).casefold()
                if text and fingerprint and fingerprint not in seen:
                    seen.add(fingerprint)
                    items.append({"time": round(index * interval, 2), "text": text})
        except Exception as exc:
            return {"status": "error", "error": str(exc)[:300], "items": items}
        finally:
            for frame in frames.glob("frame-*.jpg"):
                frame.unlink(missing_ok=True)
            try:
                frames.rmdir()
            except OSError:
                pass
        result = {"status": "success" if items else "no_text", "items": items, "sample_interval_seconds": interval, "sampled_frames": min(max_frames, int(duration // interval) + 1), "cache_hit": False}
        atomic_write_json(cache_path, result)
        return result
