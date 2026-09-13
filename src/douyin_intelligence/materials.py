from __future__ import annotations

import math
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import load_config, resolve_path
from .media_tools import resolve_media_tool
from .exporter import atomic_write_json
from .normalize import load_raw_records, normalize_record


MEDIA_PROCESS_TIMEOUT_SECONDS = 180


def _run_media_process(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False, timeout=MEDIA_PROCESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, "", f"media process timed out after {MEDIA_PROCESS_TIMEOUT_SECONDS} seconds")


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.replace(temporary, path)
    except Exception:
        if temporary and temporary.exists():
            temporary.unlink()
        raise


def _engagement(item: Any) -> float:
    return float((item.digg_count or 0) + 3 * (item.comment_count or 0) + 5 * (item.share_count or 0) + 4 * (item.collect_count or 0) + .05 * (item.play_count or 0))


def select_candidates(run_dir: Path, config: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    now = datetime.now(ZoneInfo(str(config["timezone"]))).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = now - timedelta(days=int(config["materials"].get("recent_days") or 30))
    candidates: list[dict[str, Any]] = []
    warnings: list[str] = []
    account_by_id = {str(item["id"]): item for item in config["benchmark_accounts"] if item.get("enabled", True)}
    paths = sorted({*run_dir.rglob("creator_contents_*.jsonl"), *run_dir.rglob("creator_contents_*.json"), *run_dir.rglob("search_contents_*.jsonl"), *run_dir.rglob("search_contents_*.json")})
    for path in paths:
        source = "douyin_search" if "search_contents_" in path.name else "douyin_creator"
        account_hint = next((item for key, item in account_by_id.items() if key.casefold() in str(path).casefold()), None)
        try:
            for raw in load_raw_records(path):
                record = normalize_record(raw, source=source, timezone_name=str(config["timezone"]), categories=config["categories"], raw_file=str(path.resolve()))
                if account_hint:
                    record.account_id = str(account_hint["id"])
                    record.account_name = str(account_hint.get("name") or account_hint["id"])
                    if record.category == "unclassified":
                        record.category = str(account_hint.get("category") or record.category)
                published = datetime.fromisoformat(record.published_at) if record.published_at else None
                age_hours = max(0.0, (now - published).total_seconds() / 3600) if published else 99999.0
                freshness = max(0.0, 30.0 - age_hours / 24)
                interaction = min(55.0, math.log10(1 + _engagement(record)) * 11) if _engagement(record) else 0.0
                record.score = round(freshness + interaction + (15.0 if record.category != "unclassified" else 0.0), 3)
                record.score_reasons = [f"近 {age_hours / 24:.1f} 天", f"加权互动 {_engagement(record):.0f}", f"题材 {record.category}"]
                candidates.append({"record": record, "raw": raw, "recent": bool(published and published >= cutoff)})
        except (OSError, ValueError) as exc:
            warnings.append(f"无法读取 {path.name}：{exc}")
    recent = [item for item in candidates if item["recent"]]
    pool = recent
    if not pool and candidates:
        pool = candidates
        warnings.append("最近时间窗口内没有作品，已回退到账号最新作品；请谨慎使用时效性结论。")
    pool.sort(key=lambda item: (-item["record"].score, item["record"].video_id))
    selected: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for item in pool:
        account_id = item["record"].account_id
        if counts.get(account_id, 0) >= int(config["materials"].get("max_per_account") or 2):
            continue
        if not str(item["raw"].get("video_download_url") or "").strip():
            warnings.append(f"作品 {item['record'].video_id} 缺少下载地址，未入选。")
            continue
        selected.append(item)
        counts[account_id] = counts.get(account_id, 0) + 1
        if len(selected) >= int(config["materials"]["top_n"]):
            break
    return selected, warnings


class MediaTooLargeError(ValueError):
    """A download whose declared/streamed size exceeds the allowed cap.

    ``declared_bytes``/``limit`` let the caller tell a per-item-cap rejection
    apart from a run-budget exhaustion.  ``source`` records *where* the oversize
    was detected and therefore how much bandwidth it cost:

    * ``"declared"`` -- rejected on ``Content-Length`` (or a pre-existing cached
      file over the cap) *before* the response body was read, so ``bytes_read``
      is ``0`` and the item costs zero bandwidth;
    * ``"streamed"`` -- the running total crossed the cap mid-body (there was no
      usable ``Content-Length``); ``bytes_read`` is how many bytes were actually
      pulled off the wire before aborting.  Those bytes were really spent, so a
      caller accounting for traffic MUST charge ``bytes_read``.
    """

    def __init__(
        self,
        message: str,
        *,
        declared_bytes: int | None = None,
        limit: int | None = None,
        source: str = "declared",
        bytes_read: int = 0,
    ) -> None:
        super().__init__(message)
        self.declared_bytes = declared_bytes
        self.limit = limit
        self.source = str(source or "declared")
        self.bytes_read = max(0, int(bytes_read or 0))


def download_video(url: str, destination: Path, config: dict[str, Any], *, max_bytes: int | None = None) -> None:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    settings = config["materials"]
    # ``max_bytes`` is an explicit per-download cap (e.g. a run budget's
    # remaining allowance); when omitted the historical ``max_video_bytes``
    # semantics are preserved for every existing caller.
    cap = int(max_bytes) if max_bytes is not None else int(settings.get("max_video_bytes") or 524288000)
    cached_bytes = destination.stat().st_size if destination.is_file() else 0
    if cached_bytes > 1024:
        # A cache hit is still *delivered bytes* for this candidate, so it must
        # obey the very same cap as a fresh download.  Returning *before* the cap
        # was applied let a pre-existing oversize file slip past both the
        # per-item limit and the run budget and be copied straight into
        # ``04-原片`` untouched -- the persistent ``data/media/material-replication/
        # material`` tree is never reclaimed, so this is a real, not theoretical,
        # way to blow the "<=150 MB per run" acceptance line.
        if cached_bytes > cap:
            raise MediaTooLargeError(
                f"缓存文件 {cached_bytes} 字节超过上限 {cap}",
                declared_bytes=cached_bytes, limit=cap,
            )
        return
    attempts = int(settings.get("download_retries") or 3)
    timeout = int(settings.get("download_timeout_seconds") or 180)
    error: Exception | None = None
    for attempt in range(attempts):
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.douyin.com/"})
            size = 0
            with urllib.request.urlopen(request, timeout=timeout) as response:
                declared_bytes: int | None = None
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        declared_bytes = int(declared)
                    except (TypeError, ValueError):
                        declared_bytes = None
                # Zero-waste guard: reject on the declared size before the body
                # is touched, so an over-cap item never spends bandwidth.
                if declared_bytes is not None and declared_bytes > cap:
                    raise MediaTooLargeError(
                        f"视频声明体积 {declared_bytes} 字节超过上限 {cap}",
                        declared_bytes=declared_bytes, limit=cap,
                        source="declared", bytes_read=0,
                    )
                with temporary.open("wb") as stream:
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        # Second line of defence for a missing/incorrect
                        # Content-Length: keep accumulating and abort hard.  By
                        # now ``size`` bytes have actually been read off the wire
                        # (the chunk that tripped the cap is *not* written), so
                        # report that cost -- a traffic ledger must charge it.
                        if size > cap:
                            raise MediaTooLargeError(
                                f"视频超过允许的最大体积 {cap} 字节",
                                declared_bytes=size, limit=cap,
                                source="streamed", bytes_read=size,
                            )
                        stream.write(chunk)
            if size <= 1024:
                raise ValueError("下载内容过小，不是有效视频")
            os.replace(temporary, destination)
            return
        except MediaTooLargeError:
            # Oversize is not transient: do not burn retries on it.
            temporary.unlink(missing_ok=True)
            raise
        except Exception as exc:
            error = exc
            temporary.unlink(missing_ok=True)
            if attempt + 1 < attempts:
                time.sleep(1 + attempt)
    raise RuntimeError(f"视频下载失败：{error}")


def probe_video(path: Path, config: dict[str, Any] | None = None) -> dict[str, Any]:
    command = [resolve_media_tool(config or load_config(), "ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height:format=duration,size", "-of", "json", str(path)]
    completed = _run_media_process(command)
    if completed.returncode:
        raise ValueError(f"ffprobe 无法解析视频：{completed.stderr.strip()}")
    import json
    payload = json.loads(completed.stdout)
    stream = (payload.get("streams") or [{}])[0]
    fmt = payload.get("format") or {}
    duration = float(fmt.get("duration") or 0)
    if duration <= 0:
        raise ValueError("视频时长无效")
    return {"duration_seconds": round(duration, 3), "codec": stream.get("codec_name"), "width": stream.get("width"), "height": stream.get("height"), "size_bytes": int(fmt.get("size") or path.stat().st_size)}


class Transcriber:
    def __init__(self, config: dict[str, Any], enabled: bool = True):
        self.settings = config["materials"]["transcription"]
        self.enabled = enabled and bool(self.settings.get("enabled", True))
        self.ffmpeg = resolve_media_tool(config, "ffmpeg")
        self.model = None

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

    def _one(self, path: Path, offset: float = 0.0) -> tuple[list[dict[str, Any]], str]:
        self._load()
        segments, info = self.model.transcribe(str(path), language=str(self.settings.get("language") or "zh"), beam_size=int(self.settings.get("beam_size") or 5), vad_filter=True)
        rows = [{"start": round(item.start + offset, 2), "end": round(item.end + offset, 2), "text": item.text.strip()} for item in segments if item.text.strip()]
        return rows, getattr(info, "language", "zh")

    def run(self, path: Path, duration_seconds: float = 0.0) -> dict[str, Any]:
        if not self.enabled:
            return {"status": "skipped", "segments": [], "text": ""}
        try:
            if duration_seconds > 300:
                rows: list[dict[str, Any]] = []
                language = "zh"
                with tempfile.TemporaryDirectory(prefix="douyin-transcribe-") as temp_dir:
                    pattern = str(Path(temp_dir) / "chunk-%03d.wav")
                    command = [self.ffmpeg, "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-f", "segment", "-segment_time", "180", "-reset_timestamps", "1", pattern]
                    completed = _run_media_process(command)
                    if completed.returncode:
                        raise ValueError(f"长视频音频切片失败：{completed.stderr.strip()}")
                    for index, chunk in enumerate(sorted(Path(temp_dir).glob("chunk-*.wav"))):
                        part, language = self._one(chunk, index * 180.0)
                        rows.extend(part)
            else:
                rows, language = self._one(path)
            return {"status": "success" if rows else "no_speech", "language": language, "segments": rows, "text": "。".join(item["text"] for item in rows)}
        except Exception as exc:
            return {"status": "error", "error": str(exc), "segments": [], "text": ""}


def _timestamp(seconds: float) -> str:
    value = max(0, int(seconds))
    return f"{value // 60:02d}:{value % 60:02d}"


def _key_points(text: str, fallback: str) -> list[str]:
    sentences = [item.strip() for item in re.split(r"[。！？!?\n]+", text or fallback) if len(item.strip()) >= 5]
    result = []
    for sentence in sentences:
        if sentence not in result:
            result.append(sentence[:160] + ("…" if len(sentence) > 160 else ""))
        if len(result) == 5:
            break
    return result or [fallback or "无可提取文本"]


def render_markdown(item: dict[str, Any], media_path: Path, probe: dict[str, Any], transcript: dict[str, Any]) -> str:
    record = item["record"]
    points = _key_points(transcript.get("text", ""), record.title)
    lines = [
        f"# {record.title or record.video_id}", "",
        "> 定位：抖音趋势与选题线索，不是事实证据。进入成片前必须回查原始来源。", "",
        "## 来源与价值", "",
        f"- 对标账号：{record.account_name}（{record.account_id}）",
        f"- 原作品：[{record.share_url}]({record.share_url})",
        f"- 发布时间：{record.published_at or '未知'}",
        f"- 内容分类：{record.category}",
        f"- 价值评分：{record.score}",
        f"- 评分依据：{'；'.join(record.score_reasons)}",
        f"- 互动数据：点赞 {record.digg_count or 0} / 评论 {record.comment_count or 0} / 分享 {record.share_count or 0} / 收藏 {record.collect_count or 0}", "",
        "## 原始文案", "", record.title or "（无标题）", "",
        "## 可用内容提炼", "", f"- 开场钩子：{points[0]}",
    ]
    lines.extend(f"- 关键信息：{point}" for point in points)
    lines.extend(["- 建议角度：优先核验产品/项目官方信息，再组织为新品速览、实测拆解或趋势观察。", "", "## 语音转写", ""])
    if transcript.get("segments"):
        lines.extend(f"- `{_timestamp(segment['start'])}` {segment['text']}" for segment in transcript["segments"])
    else:
        lines.append(f"（{transcript.get('status', 'unknown')}：{transcript.get('error', '没有识别到有效语音')}）")
    lines.extend(["", "## 本地素材", "", f"- 视频文件：`{media_path.resolve()}`", f"- 时长：{probe['duration_seconds']} 秒", f"- 画面：{probe.get('width')}×{probe.get('height')} / {probe.get('codec')}", "", "## 使用约束", "", "- 不可把账号文案、字幕或互动数直接当成新闻事实。", "- 涉及发布日期、价格、规格、公司表态和性能结论时，必须用官方或一手来源复核。", ""])
    return "\n".join(lines)


def build_materials(run_dir: str | Path, config: dict[str, Any], output_dir: str | Path | None = None, *, transcription: bool = True) -> dict[str, Any]:
    source = Path(run_dir).resolve()
    destination = Path(output_dir).resolve() if output_dir else resolve_path(config["materials"]["output_root"]) / source.name
    media_root = resolve_path(config["materials"]["media_root"]) / source.name
    selected, warnings = select_candidates(source, config)
    transcriber = Transcriber(config, enabled=transcription)
    outputs = []
    errors = []
    for item in selected:
        record = item["record"]
        video = media_root / f"{record.account_id}-{record.video_id}.mp4"
        markdown = destination / "videos" / f"{record.account_id}-{record.video_id}.md"
        try:
            download_video(str(item["raw"]["video_download_url"]), video, config)
            technical = probe_video(video)
            transcript_cache = video.with_suffix(".transcript.json")
            if transcript_cache.is_file():
                transcript = json.loads(transcript_cache.read_text(encoding="utf-8"))
            else:
                transcript = transcriber.run(video, technical["duration_seconds"])
                atomic_write_json(transcript_cache, transcript)
            _atomic_text(markdown, render_markdown(item, video, technical, transcript))
            outputs.append({"video_id": record.video_id, "account_id": record.account_id, "title": record.title, "score": record.score, "source_url": record.share_url, "video_path": str(video.resolve()), "markdown_path": str(markdown.resolve()), "technical": technical, "transcription_status": transcript["status"]})
            if transcript["status"] == "error":
                warnings.append(f"作品 {record.video_id} 转写失败：{transcript.get('error')}")
        except Exception as exc:
            errors.append({"video_id": record.video_id, "error": str(exc)})
    status = "success" if outputs and not errors and all(item["transcription_status"] in {"success", "no_speech", "skipped"} for item in outputs) else "partial" if outputs else "failed"
    summary_lines = [f"# 抖音高价值素材摘要：{source.name}", "", "> 本文档仅提供趋势、选题和文案线索；事实必须由下游回查一手来源。", ""]
    for index, output in enumerate(outputs, 1):
        relative = Path(output["markdown_path"]).relative_to(destination)
        summary_lines.extend([f"## {index}. {output['title']}", "", f"- 账号：{output['account_id']}", f"- 评分：{output['score']}", f"- 详情：[打开素材文档]({relative.as_posix()})", f"- 原视频：[{output['source_url']}]({output['source_url']})", ""])
    _atomic_text(destination / "summary.md", "\n".join(summary_lines))
    report = {"status": status, "run_id": source.name, "source_run_dir": str(source), "output_dir": str(destination.resolve()), "selected_count": len(selected), "completed_count": len(outputs), "outputs": outputs, "warnings": warnings, "errors": errors}
    atomic_write_json(destination / "run_report.json", report)
    return report
