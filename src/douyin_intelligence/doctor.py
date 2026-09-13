from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import project_root, resolve_path
from .media_tools import media_tool_available, resolve_media_tool


def _version(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or result.stderr).strip().splitlines()[0] if (result.stdout or result.stderr).strip() else ""


def run_doctor(config: dict[str, Any]) -> dict[str, Any]:
    crawler = config.get("media_crawler") or {}
    crawler_root = resolve_path(str(crawler.get("root") or "third_party/MediaCrawler"))
    crawler_python = resolve_path(str(crawler.get("python") or "third_party/MediaCrawler/.venv/Scripts/python.exe"))
    chrome_candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]
    chrome = next((path for path in chrome_candidates if path.is_file()), None)
    checks = {
        "project_root": str(project_root()),
        "python": {"ok": bool(shutil.which("python")), "version": _version(["python", "--version"])},
        "node": {"ok": bool(shutil.which("node")), "version": _version(["node", "--version"])},
        "chrome": {"ok": chrome is not None, "path": str(chrome or "")},
        "media_crawler": {
            "ok": (crawler_root / "main.py").is_file(),
            "root": str(crawler_root),
            "venv_ok": crawler_python.is_file(),
            "python": str(crawler_python),
        },
        "benchmark_accounts": {
            "ok": any(item.get("enabled", True) for item in config.get("benchmark_accounts") or [] if isinstance(item, dict)),
            "enabled_count": sum(1 for item in config.get("benchmark_accounts") or [] if isinstance(item, dict) and item.get("enabled", True)),
        },
        "ffmpeg": {"ok": media_tool_available(config, "ffmpeg"), "path": resolve_media_tool(config, "ffmpeg"), "version": _version([resolve_media_tool(config, "ffmpeg"), "-version"])},
        "ffprobe": {"ok": media_tool_available(config, "ffprobe"), "path": resolve_media_tool(config, "ffprobe"), "version": _version([resolve_media_tool(config, "ffprobe"), "-version"])},
        "rapidocr": {"ok": _version([sys.executable, "-c", "from rapidocr import RapidOCR; print('ready')"]) == "ready"},
        "authentication": {
            "ok": False,
            "status": "not_probed",
            "action": "首次真实采集时使用专用 Chrome 登录态或二维码；doctor 不读取 Cookie，也不访问抖音。",
        },
    }
    required = ("python", "node", "chrome", "media_crawler", "benchmark_accounts", "ffmpeg", "ffprobe", "rapidocr")
    checks["ready_for_offline_pipeline"] = all(bool(checks[name]["ok"]) for name in required)
    checks["ready_for_live_collection"] = False
    try:
        from .replication_pipeline import replication_doctor
        checks["material_replication"] = replication_doctor(config)
    except Exception as exc:  # Self-check must never break doctor.
        checks["material_replication"] = {"status": "unavailable", "error": str(exc)[:200]}
    return checks
