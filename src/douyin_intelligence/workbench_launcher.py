from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from typing import Any

from .job_runtime import JobLock


WORKBENCH_LOCK_NAME = "workbench_ui"
ALREADY_RUNNING_EXIT_CODE = 4
_SMOKE_ENV = "COPY_SKILL_WORKBENCH_SMOKE_MS"
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|cookie|authorization|bearer|token|api[_-]?key|websocket(?:debugger)?url)\b"
    r"\s*[:=]?\s*[^\s,;]+"
)
_URL = re.compile(r"(?i)\b(?:https?|wss?)://[^\s]+")


def safe_launcher_error(exc: BaseException) -> str:
    """Return a short diagnostic without persisting credentials or browser URLs."""
    message = f"{type(exc).__name__}: {exc}".replace("\r", " ").replace("\n", " ")
    message = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<已脱敏>", message)
    message = _URL.sub("<已隐藏地址>", message)
    return message[:800]


def smoke_auto_close_ms(environ: dict[str, str] | None = None) -> int | None:
    """Read a bounded, test-only Tk auto-close delay."""
    raw = (environ or os.environ).get(_SMOKE_ENV)
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return min(60_000, max(250, value))


def launch_workbench(
    config: dict[str, Any],
    config_path: str,
    *,
    runner: Callable[..., None] | None = None,
    error_stream: Any = None,
) -> int:
    """Run the Tk workbench under the project's process-safe single-instance lock."""
    stream = error_stream or sys.stderr
    lock = JobLock(config, WORKBENCH_LOCK_NAME)
    if not lock.acquire():
        print("工作台已经在运行，请使用现有窗口。未启动第二个实例。", file=stream)
        return ALREADY_RUNNING_EXIT_CODE
    try:
        if runner is None:
            from .workbench import run_workbench

            runner = run_workbench
        runner(config, config_path, auto_close_ms=smoke_auto_close_ms())
        return 0
    except Exception as exc:
        print(f"工作台启动失败：{safe_launcher_error(exc)}", file=stream)
        return 2
    finally:
        lock.release()

