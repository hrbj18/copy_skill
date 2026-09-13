from __future__ import annotations

import copy
import json
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from douyin_intelligence import collector
from douyin_intelligence.collector import BrowserSession, browser_status, close_project_browser, ensure_browser, prepare_douyin_login
from douyin_intelligence.config import load_config


def _config(tmp_path: Path) -> dict:
    config = copy.deepcopy(load_config())
    config["_project_root"] = str(tmp_path)
    config["jobs"]["lock_root"] = str(tmp_path / "locks")
    crawler = config["media_crawler"]
    crawler["browser_lease_root"] = str(tmp_path / "leases")
    crawler["browser_state_path"] = str(tmp_path / "browser-state.json")
    crawler["user_data_dir"] = str(tmp_path / "profile")
    crawler["chrome_path"] = str(tmp_path / "chrome.exe")
    Path(crawler["chrome_path"]).write_bytes(b"fake")
    return config


class FakeCDP:
    def __init__(self, pages: list[dict[str, str]] | None = None, *, close_result: bool = True):
        self._ready = True
        self._pages = list(pages or [])
        self.closed_targets: list[str] = []
        self.close_result = close_result
        self.browser_close_calls = 0

    def ready(self) -> bool:
        return self._ready

    def pages(self) -> list[dict[str, str]]:
        return list(self._pages) if self._ready else []

    def new_douyin_page(self) -> str:
        target_id = f"new-{len(self._pages) + 1}"
        self._pages.append({"id": target_id, "url": "https://www.douyin.com/jingxuan"})
        return target_id

    def close_target(self, target_id: str) -> bool:
        self.closed_targets.append(target_id)
        self._pages = [row for row in self._pages if row["id"] != target_id]
        return True

    def close_browser(self) -> bool:
        self.browser_close_calls += 1
        if self.close_result:
            self._ready = False
            self._pages = []
        return self.close_result


def _ensure(_config: dict) -> dict:
    return {"status": "reused", "port": 9223, "started_pid": None}


def test_existing_browser_is_reused_without_second_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(collector, "_cdp_info", lambda _port: {"webSocketDebuggerUrl": "redacted"})
    monkeypatch.setattr(collector.subprocess, "Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not start Chrome")))
    result = ensure_browser(config)
    assert result == {"status": "reused", "port": 9223, "started_pid": None}


def test_concurrent_ensure_requests_start_one_project_instance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    ready = {"value": False}
    starts = {"count": 0}
    monkeypatch.setattr(collector, "_cdp_info", lambda _port: {"webSocketDebuggerUrl": "redacted"} if ready["value"] else None)

    class FakeProcess:
        pid = 4321
        def terminate(self): pass
        def wait(self, timeout): return 0
        def kill(self): pass

    def fake_popen(*_args, **_kwargs):
        starts["count"] += 1
        ready["value"] = True
        return FakeProcess()

    monkeypatch.setattr(collector.subprocess, "Popen", fake_popen)
    results: list[dict] = []
    threads = [threading.Thread(target=lambda: results.append(ensure_browser(config))) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=5)
    assert starts["count"] == 1
    assert sorted(row["status"] for row in results) == ["reused", "started"]


def test_prepare_converges_only_project_pages_and_needs_login_keeps_one(tmp_path: Path) -> None:
    config = _config(tmp_path)
    cdp = FakeCDP([
        {"id": "personal-looking-but-project-profile", "url": "https://example.test/"},
        {"id": "douyin-1", "url": "https://www.douyin.com/jingxuan"},
        {"id": "douyin-2", "url": "https://www.douyin.com/user/example"},
    ])
    session = BrowserSession(config, "trusted", client=cdp, ensure_func=_ensure)
    prepared = session.prepare()
    result = session.finish("needs_login", human_required=True)
    assert prepared["port"] == 9223
    assert prepared["page_count"] == 1
    assert result["state"] == "waiting_for_login" and result["page_count"] == 1
    assert cdp.browser_close_calls == 0
    assert set(cdp.closed_targets) == {"personal-looking-but-project-profile", "douyin-2"}


def test_finish_closes_only_new_targets_when_another_lease_exists(tmp_path: Path) -> None:
    config = _config(tmp_path)
    cdp = FakeCDP([{"id": "baseline", "url": "https://www.douyin.com/"}])
    session = BrowserSession(config, "first", client=cdp, ensure_func=_ensure)
    session.prepare()
    other = Path(config["media_crawler"]["browser_lease_root"]) / "other.json"
    other.write_text(json.dumps({"pid": __import__("os").getpid(), "host": socket.gethostname(), "purpose": "other"}), encoding="utf-8")
    cdp._pages.append({"id": "created-by-crawler", "url": "https://www.douyin.com/video/1"})
    result = session.finish("success")
    assert cdp.closed_targets[-1] == "created-by-crawler"
    assert cdp.browser_close_calls == 0
    assert result["state"] == "task_running"
    assert [row["id"] for row in cdp.pages()] == ["baseline"]


@pytest.mark.parametrize("status", ["success", "partial", "empty", "failed"])
def test_normal_completion_closes_project_browser_and_preserves_profile(tmp_path: Path, status: str) -> None:
    config = _config(tmp_path)
    profile = Path(config["media_crawler"]["user_data_dir"])
    profile.mkdir(parents=True)
    marker = profile / "authentication-owned-by-browser.bin"
    marker.write_bytes(b"opaque")
    cdp = FakeCDP([{"id": "baseline", "url": "https://www.douyin.com/"}])
    session = BrowserSession(config, "trusted", client=cdp, ensure_func=_ensure)
    session.prepare()
    result = session.finish(status)
    assert result["browser_closed"] is True and result["state"] == "completed_closed"
    assert marker.read_bytes() == b"opaque"


def test_cdp_close_failure_is_bounded_safe_and_does_not_delete_profile(tmp_path: Path) -> None:
    config = _config(tmp_path)
    profile = Path(config["media_crawler"]["user_data_dir"])
    profile.mkdir(parents=True)
    marker = profile / "keep.bin"
    marker.write_bytes(b"opaque")
    cdp = FakeCDP([{"id": "baseline", "url": "https://www.douyin.com/"}], close_result=False)
    session = BrowserSession(config, "trusted", client=cdp, ensure_func=_ensure)
    session.prepare()
    result = session.finish("success")
    assert result["browser_closed"] is False and result["state"] == "connected_ready"
    assert marker.exists()


def test_explicit_close_refuses_while_lease_active(tmp_path: Path) -> None:
    config = _config(tmp_path)
    lease_root = Path(config["media_crawler"]["browser_lease_root"])
    lease_root.mkdir(parents=True)
    (lease_root / "active.json").write_text(json.dumps({"pid": __import__("os").getpid(), "host": socket.gethostname()}), encoding="utf-8")
    cdp = FakeCDP([{"id": "baseline", "url": "https://www.douyin.com/"}])
    result = close_project_browser(config, client=cdp)
    assert result == {"status": "busy", "browser_closed": False, "active_leases": 1}
    assert cdp.browser_close_calls == 0


def test_login_prepare_returns_only_safe_counts_not_targets_or_urls(tmp_path: Path) -> None:
    config = _config(tmp_path)
    cdp = FakeCDP([])
    result = prepare_douyin_login(config, client=cdp, ensure_func=_ensure)
    serialized = json.dumps(result, ensure_ascii=False).casefold()
    assert result["status"] == "waiting_for_login" and result["page_count"] == 1
    for forbidden in ("target", "websocket", "cookie", "password", "token", "https://"):
        assert forbidden not in serialized
    state = Path(config["media_crawler"]["browser_state_path"]).read_text(encoding="utf-8").casefold()
    assert "https://" not in state and "target" not in state


def test_browser_status_exposes_count_only(tmp_path: Path) -> None:
    config = _config(tmp_path)
    cdp = FakeCDP([{"id": "secret-id", "url": "https://www.douyin.com/secret"}])
    result = browser_status(config, client=cdp)
    assert result == {"state": "connected_ready", "connected": True, "page_count": 1, "port": 9223}


def test_unreachable_cdp_degrades_without_process_termination(tmp_path: Path) -> None:
    config = _config(tmp_path)
    cdp = FakeCDP([])
    cdp._ready = False
    result = close_project_browser(config, client=cdp)
    assert result == {"status": "not_running", "browser_closed": False, "active_leases": 0}
    assert cdp.browser_close_calls == 0
