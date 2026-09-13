from __future__ import annotations

import json
import os
import socket
import tempfile
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import resolve_path
from .exporter import atomic_write_json


def now_iso(timezone_name: str) -> str:
    return datetime.now(ZoneInfo(timezone_name)).isoformat(timespec="seconds")


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else dict(default)
    except (OSError, json.JSONDecodeError):
        return dict(default)


class JobState:
    def __init__(self, config: dict[str, Any], job: str):
        self.config = config
        self.job = job
        self.path = resolve_path(config["jobs"]["state_path"])

    def update(self, **values: Any) -> dict[str, Any]:
        payload = load_json(self.path, {"version": "1.0", "jobs": {}})
        jobs = dict(payload.get("jobs") or {})
        current = dict(jobs.get(self.job) or {})
        current.update(values)
        current["updated_at"] = now_iso(str(self.config["timezone"]))
        jobs[self.job] = current
        payload.update({"version": "1.0", "jobs": jobs})
        atomic_write_json(self.path, payload)
        return current


class JobLock(AbstractContextManager["JobLock"]):
    def __init__(self, config: dict[str, Any], job: str):
        self.config = config
        self.job = job
        self.path = resolve_path(config["jobs"]["lock_root"]) / f"{job}.lock"
        self.acquired = False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            # On Windows, signal.CTRL_C_EVENT is numerically 0. Calling
            # os.kill(pid, 0) therefore broadcasts Ctrl+C to the target
            # console process group instead of performing a POSIX-style
            # existence probe. Query the process handle without signalling it.
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            error_access_denied = 5
            still_active = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            open_process = kernel32.OpenProcess
            open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            open_process.restype = wintypes.HANDLE
            get_exit_code = kernel32.GetExitCodeProcess
            get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            get_exit_code.restype = wintypes.BOOL
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL

            handle = open_process(process_query_limited_information, False, pid)
            if not handle:
                # A protected process that cannot be queried is still alive;
                # preserve its lock rather than risk concurrent execution.
                return ctypes.get_last_error() == error_access_denied
            try:
                exit_code = wintypes.DWORD()
                if not get_exit_code(handle, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == still_active
            finally:
                close_handle(handle)
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump({"pid": os.getpid(), "host": socket.gethostname(), "created_at": now_iso(str(self.config["timezone"]))}, stream)
                self.acquired = True
                return True
            except FileExistsError:
                existing = load_json(self.path, {})
                if existing.get("host") == socket.gethostname() and self._pid_alive(int(existing.get("pid") or 0)):
                    return False
                self.path.unlink(missing_ok=True)
        return False

    def release(self) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
            self.acquired = False

    def __enter__(self) -> "JobLock":
        if not self.acquire():
            raise RuntimeError(f"任务 {self.job} 已在运行")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


def target_yesterday(timezone_name: str, now: datetime | None = None) -> str:
    zone = ZoneInfo(timezone_name)
    current = now.astimezone(zone) if now else datetime.now(zone)
    return (current.date() - timedelta(days=1)).isoformat()


def temp_size(config: dict[str, Any]) -> int:
    root = resolve_path(config["materials"]["retention"]["temp_root"])
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file()) if root.exists() else 0
