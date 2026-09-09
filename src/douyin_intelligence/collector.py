from __future__ import annotations

import json
import base64
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .artifact_safety import sanitize_raw_files
from .config import resolve_path
from .exporter import atomic_write_json
from .job_runtime import JobLock, load_json, now_iso
from .normalize import load_raw_records


_BROWSER_THREAD_LOCK = threading.Lock()


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "unknown"


def _cdp_info(port: int) -> dict[str, Any] | None:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
            payload = json.load(response)
        return payload if payload.get("webSocketDebuggerUrl") else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _project_path(config: dict[str, Any], value: str | Path) -> Path:
    path = Path(value)
    root = Path(config.get("_project_root") or Path(__file__).resolve().parents[2])
    return path.resolve() if path.is_absolute() else (root / path).resolve()


@contextmanager
def _browser_critical(config: dict[str, Any]) -> Iterator[None]:
    timeout = float(config["media_crawler"].get("browser_lock_timeout_seconds") or 10)
    deadline = time.monotonic() + timeout
    if not _BROWSER_THREAD_LOCK.acquire(timeout=timeout):
        raise TimeoutError("项目浏览器进程内锁等待超时")
    lock = JobLock(config, "project_browser_lifecycle")
    try:
        while not lock.acquire():
            if time.monotonic() >= deadline:
                raise TimeoutError("项目浏览器生命周期锁等待超时")
            time.sleep(0.05)
        try:
            yield
        finally:
            lock.release()
    finally:
        _BROWSER_THREAD_LOCK.release()


class CDPClient:
    def __init__(self, port: int, timeout: float = 2.0):
        self.port = port
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _json(self, path: str, *, method: str = "GET") -> Any:
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        with self.opener.open(request, timeout=self.timeout) as response:
            return json.load(response)

    def ready(self) -> bool:
        return _cdp_info(self.port) is not None

    def pages(self) -> list[dict[str, str]]:
        payload = self._json("/json/list")
        if not isinstance(payload, list):
            return []
        return [
            {"id": str(item.get("id") or ""), "url": str(item.get("url") or "")}
            for item in payload if isinstance(item, dict) and item.get("type") == "page" and item.get("id")
        ]

    def new_douyin_page(self) -> str:
        payload = self._json(f"/json/new?{quote('https://www.douyin.com/jingxuan', safe='')}", method="PUT")
        return str(payload.get("id") or "") if isinstance(payload, dict) else ""

    def close_target(self, target_id: str) -> bool:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", target_id):
            return False
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/json/close/{quote(target_id, safe='')}")
        try:
            with self.opener.open(request, timeout=self.timeout):
                return True
        except OSError:
            return False

    def close_browser(self) -> bool:
        info = _cdp_info(self.port)
        endpoint = str((info or {}).get("webSocketDebuggerUrl") or "")
        if not endpoint:
            return not self.ready()
        try:
            parsed = urlparse(endpoint)
            if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost"} or int(parsed.port or 0) != self.port:
                return False
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            connection = socket.create_connection(("127.0.0.1", self.port), timeout=self.timeout)
            try:
                request = (
                    f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nUpgrade: websocket\r\n"
                    f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                    f"Origin: http://127.0.0.1:{self.port}\r\n\r\n"
                ).encode("ascii")
                connection.sendall(request)
                response = connection.recv(4096)
                if b" 101 " not in response.split(b"\r\n", 1)[0]:
                    return False
                payload = json.dumps({"id": 1, "method": "Browser.close"}, separators=(",", ":")).encode("utf-8")
                mask = os.urandom(4)
                header = bytes([0x81, 0x80 | len(payload)])
                masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
                connection.sendall(header + mask + masked)
            finally:
                connection.close()
        except Exception:
            return False
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if not self.ready():
                return True
            time.sleep(0.1)
        return not self.ready()


def _safe_browser_state(config: dict[str, Any], state: str, *, page_count: int, lease_count: int, detail: str | None = None) -> dict[str, Any]:
    path = _project_path(config, config["media_crawler"].get("browser_state_path") or "data/state/project_browser_status.json")
    payload = {
        "version": "1.0", "state": state, "page_count": max(0, page_count),
        "lease_count": max(0, lease_count), "updated_at": now_iso(str(config["timezone"])),
    }
    if detail:
        payload["detail"] = detail[:160]
    atomic_write_json(path, payload)
    return payload


def browser_status(config: dict[str, Any], *, client: CDPClient | None = None) -> dict[str, Any]:
    crawler = config["media_crawler"]
    client = client or CDPClient(int(crawler["cdp_port"]), float(crawler.get("cdp_timeout_seconds") or 2))
    state_path = _project_path(config, crawler.get("browser_state_path") or "data/state/project_browser_status.json")
    saved = load_json(state_path, {})
    try:
        ready = client.ready()
        page_count = len(client.pages()) if ready else 0
    except (OSError, ValueError, TimeoutError):
        ready, page_count = False, 0
    state = str(saved.get("state") or ("connected_ready" if ready else "not_started"))
    if not ready:
        state = "not_started" if state not in {"completed_closed"} else state
    return {"state": state, "connected": ready, "page_count": page_count, "port": int(crawler["cdp_port"])}


def _ensure_browser_unlocked(config: dict[str, Any]) -> dict[str, Any]:
    crawler = config["media_crawler"]
    port = int(crawler["cdp_port"])
    existing = _cdp_info(port)
    if existing:
        return {"status": "reused", "port": port, "started_pid": None}

    chrome = resolve_path(crawler["chrome_path"])
    profile = resolve_path(crawler["user_data_dir"])
    if not chrome.is_file():
        raise FileNotFoundError(f"Chrome 不存在：{chrome}")
    profile.mkdir(parents=True, exist_ok=True)
    command = [
        str(chrome),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        f"--remote-allow-origins=http://127.0.0.1:{port}",
        "https://www.douyin.com/jingxuan",
    ]
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    launched = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    deadline = time.monotonic() + int(crawler.get("browser_start_timeout_seconds") or 20)
    while time.monotonic() < deadline:
        info = _cdp_info(port)
        if info:
            return {"status": "started", "port": port, "started_pid": int(launched.pid)}
        time.sleep(0.25)
    launched.terminate()
    try:
        launched.wait(timeout=5)
    except subprocess.TimeoutExpired:
        launched.kill()
    raise TimeoutError(f"专用 Chrome 已启动，但 {port} 端口未在限时内就绪")


def ensure_browser(config: dict[str, Any]) -> dict[str, Any]:
    with _browser_critical(config):
        return _ensure_browser_unlocked(config)


class BrowserSession:
    def __init__(
        self,
        config: dict[str, Any],
        purpose: str,
        *,
        client: CDPClient | None = None,
        ensure_func: Callable[[dict[str, Any]], dict[str, Any]] = _ensure_browser_unlocked,
    ):
        self.config = config
        self.purpose = re.sub(r"[^A-Za-z0-9_.-]+", "-", purpose)[:60] or "browser"
        crawler = config["media_crawler"]
        self.client = client or CDPClient(int(crawler["cdp_port"]), float(crawler.get("cdp_timeout_seconds") or 2))
        self.ensure_func = ensure_func
        self.lease_id = uuid.uuid4().hex
        self.lease_path = _project_path(config, crawler.get("browser_lease_root") or "data/state/browser-leases") / f"{self.lease_id}.json"
        self.baseline_ids: set[str] = set()
        self.started_pid: int | None = None
        self.prepared = False
        self.finished = False

    def _active_leases(self, *, exclude_self: bool = False) -> list[Path]:
        root = self.lease_path.parent
        root.mkdir(parents=True, exist_ok=True)
        active: list[Path] = []
        for path in root.glob("*.json"):
            if exclude_self and path == self.lease_path:
                continue
            payload = load_json(path, {})
            pid = int(payload.get("pid") or 0)
            host = str(payload.get("host") or "")
            if host == socket.gethostname() and JobLock._pid_alive(pid):
                active.append(path)
            else:
                path.unlink(missing_ok=True)
        return active

    def _register_lease(self) -> None:
        self.lease_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.lease_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "host": socket.gethostname(), "purpose": self.purpose}, stream)

    def _converge_pages(self, *, ensure_page: bool) -> int:
        pages = self.client.pages()
        douyin = [row for row in pages if urlparse_safe_host(row["url"]) in {"douyin.com", "www.douyin.com", "www.iesdouyin.com"}]
        keep_id = douyin[0]["id"] if douyin else ""
        if ensure_page and not keep_id:
            keep_id = self.client.new_douyin_page()
            pages = self.client.pages()
        maximum = max(1, int(self.config["media_crawler"].get("max_project_pages") or 1))
        kept = 0
        for row in pages:
            if row["id"] == keep_id and kept < maximum:
                kept += 1
                continue
            if kept < maximum and not keep_id:
                keep_id, kept = row["id"], 1
                continue
            self.client.close_target(row["id"])
        return len(self.client.pages())

    def prepare(self) -> dict[str, Any]:
        with _browser_critical(self.config):
            self._register_lease()
            try:
                result = self.ensure_func(self.config)
                self.started_pid = int(result.get("started_pid") or 0) or None
                page_count = self._converge_pages(ensure_page=True)
                self.baseline_ids = {row["id"] for row in self.client.pages()}
                self.prepared = True
                leases = len(self._active_leases())
                _safe_browser_state(self.config, "task_running" if self.purpose != "login_prepare" else "waiting_for_login", page_count=page_count, lease_count=leases)
                return {"status": result["status"], "port": result["port"], "page_count": page_count, "state": "task_running" if self.purpose != "login_prepare" else "waiting_for_login"}
            except Exception:
                self.lease_path.unlink(missing_ok=True)
                raise

    def finish(self, status: str, *, human_required: bool = False) -> dict[str, Any]:
        if self.finished:
            return {"state": "already_finished", "closed_pages": 0, "browser_closed": False}
        closed_pages = 0
        browser_closed = False
        with _browser_critical(self.config):
            try:
                if self.client.ready():
                    pages = self.client.pages()
                    for row in pages:
                        if row["id"] not in self.baseline_ids and self.client.close_target(row["id"]):
                            closed_pages += 1
                    keep_open = human_required or status == "needs_login"
                    if keep_open and self.config["media_crawler"].get("keep_open_on_needs_login", True):
                        page_count = self._converge_pages(ensure_page=True)
                        state = "waiting_for_login"
                    else:
                        other_leases = self._active_leases(exclude_self=True)
                        close_on_completion = bool(self.config["media_crawler"].get("close_owned_browser_on_completion", True))
                        if close_on_completion and not other_leases and status in {"success", "partial", "empty", "failed"}:
                            browser_closed = self.client.close_browser()
                        page_count = 0 if browser_closed else len(self.client.pages()) if self.client.ready() else 0
                        state = "completed_closed" if browser_closed else "task_running" if other_leases else "connected_ready"
                else:
                    page_count, state = 0, "completed_closed"
                self.lease_path.unlink(missing_ok=True)
                leases = len(self._active_leases(exclude_self=True))
                _safe_browser_state(self.config, state, page_count=page_count, lease_count=leases)
            finally:
                self.lease_path.unlink(missing_ok=True)
                self.finished = True
        return {"state": state, "closed_pages": closed_pages, "browser_closed": browser_closed, "page_count": page_count}


def urlparse_safe_host(value: str) -> str:
    try:
        from urllib.parse import urlparse
        return str(urlparse(value).hostname or "").casefold()
    except ValueError:
        return ""


def prepare_douyin_login(
    config: dict[str, Any], *, client: CDPClient | None = None,
    ensure_func: Callable[[dict[str, Any]], dict[str, Any]] = _ensure_browser_unlocked,
) -> dict[str, Any]:
    session = BrowserSession(config, "login_prepare", client=client, ensure_func=ensure_func)
    prepared = session.prepare()
    cleanup = session.finish("needs_login", human_required=True)
    return {"status": "waiting_for_login", "port": prepared["port"], "page_count": cleanup["page_count"], "authentication": "请仅在项目专用浏览器中人工登录；不要向项目提供密码或认证材料。"}


def close_project_browser(config: dict[str, Any], *, client: CDPClient | None = None) -> dict[str, Any]:
    crawler = config["media_crawler"]
    client = client or CDPClient(int(crawler["cdp_port"]), float(crawler.get("cdp_timeout_seconds") or 2))
    probe = BrowserSession(config, "explicit_close", client=client)
    with _browser_critical(config):
        active = probe._active_leases()
        if active:
            return {"status": "busy", "browser_closed": False, "active_leases": len(active)}
        if not client.ready():
            _safe_browser_state(config, "completed_closed", page_count=0, lease_count=0)
            return {"status": "not_running", "browser_closed": False, "active_leases": 0}
        closed = client.close_browser()
        page_count = 0 if closed else len(client.pages()) if client.ready() else 0
        _safe_browser_state(config, "completed_closed" if closed else "connected_ready", page_count=page_count, lease_count=0)
        return {"status": "closed" if closed else "close_failed", "browser_closed": closed, "active_leases": 0, "page_count": page_count}


def make_run_id(config: dict[str, Any]) -> str:
    return datetime.now(ZoneInfo(str(config["timezone"]))).strftime("%Y%m%dT%H%M%S%z")


def creator_commands(config: dict[str, Any], run_dir: Path) -> list[tuple[dict[str, Any], list[str], Path]]:
    crawler = config["media_crawler"]
    python = resolve_path(crawler["python"])
    root = resolve_path(crawler["root"])
    runner = Path(__file__).with_name("mediacrawler_runner.py").resolve()
    commands = []
    for account in config["benchmark_accounts"]:
        if not account.get("enabled", True):
            continue
        destination = run_dir / "creator" / _safe(str(account["id"]))
        command = [
            str(python), str(runner),
            "--crawler-root", str(root),
            "--cdp-port", str(int(crawler["cdp_port"])),
            "--navigation-timeout", str(int(crawler.get("navigation_timeout_seconds") or 90)),
            "--",
            "--platform", "dy", "--type", "creator", "--lt", "qrcode",
            "--save_data_option", "jsonl", "--save_data_path", str(destination),
            "--crawler_max_notes_count", str(int(crawler.get("max_notes_per_source") or 30)),
            "--get_comment", "false", "--get_sub_comment", "false",
            "--max_concurrency_num", "1", "--headless", "false",
            "--creator_id", str(account["url"]),
        ]
        commands.append((account, command, destination))
    return commands


def validate_collection(run_dir: Path, accounts: list[dict[str, Any]], attempts: list[dict[str, Any]]) -> dict[str, Any]:
    summaries: list[dict[str, Any]] = []
    for account in accounts:
        account_dir = run_dir / "creator" / _safe(str(account["id"]))
        files = sorted(account_dir.rglob("creator_contents_*.jsonl")) if account_dir.exists() else []
        rows = [row for path in files for row in load_raw_records(path)]
        hashes = sorted({str(row.get("creator_hash") or "").strip() for row in rows if row.get("creator_hash")})
        video_ids = sorted({str(row.get("aweme_id") or row.get("video_id") or "").strip() for row in rows if row.get("aweme_id") or row.get("video_id")})
        errors = []
        if len(hashes) > 1:
            errors.append("单账号结果中出现多个 creator_hash")
        summaries.append({
            "account_id": str(account["id"]), "account_name": str(account.get("name") or account["id"]),
            "files": [str(path.resolve()) for path in files], "record_count": len(rows),
            "creator_hashes": hashes, "video_ids": video_ids, "errors": errors,
        })

    nonempty = [item for item in summaries if item["record_count"]]
    global_errors: list[str] = []
    seen_hashes: dict[str, str] = {}
    for item in nonempty:
        if len(item["creator_hashes"]) == 1:
            value = item["creator_hashes"][0]
            if value in seen_hashes:
                global_errors.append(f"账号 {seen_hashes[value]} 与 {item['account_id']} 返回相同 creator_hash")
            seen_hashes[value] = item["account_id"]
    for index, left in enumerate(nonempty):
        for right in nonempty[index + 1:]:
            if left["video_ids"] and left["video_ids"] == right["video_ids"]:
                global_errors.append(f"账号 {left['account_id']} 与 {right['account_id']} 返回完全相同作品集合")

    failed_processes = [item for item in attempts if item["returncode"] != 0]
    validation_errors = global_errors + [error for item in summaries for error in item["errors"]]
    if validation_errors or not nonempty:
        status = "failed"
    elif failed_processes or len(nonempty) < len(summaries):
        status = "partial"
    else:
        status = "success"
    return {"status": status, "accounts": summaries, "attempts": attempts, "errors": validation_errors}


def collect_creators(
    config: dict[str, Any],
    run_id: str | None = None,
    *,
    before_sanitize: Callable[[list[Path], dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    browser_session = BrowserSession(config, "collect_creators")
    browser = browser_session.prepare()
    root = resolve_path(config["media_crawler"]["runs_output"])
    run_dir = root / _safe(run_id or make_run_id(config))
    run_dir.mkdir(parents=True, exist_ok=True)
    attempts = []
    commands = creator_commands(config, run_dir)
    timeout_seconds = max(10, int(config["media_crawler"].get("collection_timeout_seconds") or 120))
    final_status = "failed"
    try:
        for account, command, destination in commands:
            destination.mkdir(parents=True, exist_ok=True)
            error = None
            try:
                completed = subprocess.run(
                    command,
                    cwd=resolve_path(config["media_crawler"]["root"]),
                    check=False,
                    timeout=timeout_seconds,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired:
                completed = None
                error = f"crawler timed out after {timeout_seconds} seconds"
            files = sorted([*destination.rglob("creator_contents_*.json"), *destination.rglob("creator_contents_*.jsonl")])
            inspection_error = None
            if files and before_sanitize is not None:
                try:
                    before_sanitize(files, account)
                except Exception as exc:
                    # Raw artifacts must still cross the normal sanitization
                    # boundary even when an optional in-memory inspector fails.
                    inspection_error = type(exc).__name__
            sanitization = sanitize_raw_files(files, config, "douyin_creator") if files else []
            attempts.append({
                "account_id": str(account["id"]),
                "returncode": int(completed.returncode) if completed is not None else 124,
                "output_dir": str(destination.resolve()),
                "timeout_seconds": timeout_seconds,
                "error": error,
                "sanitization": sanitization,
                "before_sanitize_error": inspection_error,
            })
        report = validate_collection(run_dir, [item[0] for item in commands], attempts)
        final_status = report["status"]
        report.update({"run_id": run_dir.name, "run_dir": str(run_dir.resolve()), "browser": browser})
        atomic_write_json(run_dir / "collection_report.json", report)
        return report
    finally:
        browser_session.finish(final_status)
