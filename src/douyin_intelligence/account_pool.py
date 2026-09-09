from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .config import SEC_UID_RE
from .exporter import atomic_write_json
from .job_runtime import JobLock, load_json, now_iso


LIFECYCLE_STATUSES = {"candidate", "trusted", "rejected", "paused"}
_STORE_THREAD_LOCK = threading.Lock()


def _project_path(config: dict[str, Any], value: str | Path) -> Path:
    path = Path(value)
    root = Path(config.get("_project_root") or Path(__file__).resolve().parents[2])
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def normalize_profile_url(value: str) -> tuple[str, str]:
    """Return the stable sec_uid and a canonical public Douyin profile URL."""
    text = str(value or "").strip()
    parsed = urlparse(text)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in {"douyin.com", "www.douyin.com"}:
        raise ValueError("请输入有效的 HTTPS 抖音用户主页")
    match = re.fullmatch(r"/user/([^/]+)/?", parsed.path)
    account_id = match.group(1) if match else ""
    if not SEC_UID_RE.fullmatch(account_id):
        raise ValueError("请输入包含稳定账号ID的抖音用户主页")
    return account_id, f"https://www.douyin.com/user/{account_id}"


class AccountPoolStore:
    """Atomic project-owned lifecycle state; evaluation never mutates trust."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.settings = config["jobs"]["account_pool"]
        self.path = _project_path(config, self.settings["state_path"])

    def _seed_rows(self, timestamp: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in self.settings.get("initial_candidates") or []:
            account_id, profile = normalize_profile_url(str(item.get("profile_url") or ""))
            rows.append({
                "account_id": account_id,
                "display_name": str(item.get("display_name") or "").strip()[:100],
                "profile_url": profile,
                "lifecycle_status": "candidate",
                "enabled": True,
                "production_role": str(item.get("production_role") or ""),
                "discovery_enabled": bool(item.get("discovery_enabled", False)),
                "source_group_id": str(item.get("source_group_id") or ""),
                "source_group_name": str(item.get("source_group_name") or ""),
                "editorial_lane": str(item.get("editorial_lane") or ""),
                "added_at": timestamp,
                "updated_at": timestamp,
                "note": str(item.get("note") or "").strip()[:500],
            })
        return rows

    def _load(self) -> dict[str, Any]:
        payload = load_json(self.path, {"version": "1.0", "accounts": []})
        accounts = payload.get("accounts")
        return {"version": "1.0", "accounts": list(accounts) if isinstance(accounts, list) else []}

    def _mutate(self, callback: Callable[[list[dict[str, Any]], str], Any]) -> Any:
        with _STORE_THREAD_LOCK:
            with JobLock(self.config, "account_pool_state"):
                payload = self._load()
                timestamp = now_iso(str(self.config["timezone"]))
                accounts = [dict(item) for item in payload["accounts"] if isinstance(item, dict)]
                if not accounts:
                    accounts.extend(self._seed_rows(timestamp))
                result = callback(accounts, timestamp)
                atomic_write_json(self.path, {"version": "1.0", "accounts": accounts})
                return result

    def ensure_seeded(self) -> None:
        self._mutate(lambda _accounts, _timestamp: None)

    def list_accounts(self) -> list[dict[str, Any]]:
        self.ensure_seeded()
        rows = [dict(item) for item in self._load()["accounts"]]
        return sorted(rows, key=lambda item: (str(item.get("added_at") or ""), str(item.get("account_id") or "")))

    def get(self, account_id: str) -> dict[str, Any]:
        item = next((row for row in self.list_accounts() if row.get("account_id") == account_id), None)
        if item is None:
            raise ValueError("候选账号不存在")
        return item

    def add_candidate(self, display_name: str, profile_url: str, *, note: str = "") -> dict[str, Any]:
        name = str(display_name or "").strip()
        if not name:
            raise ValueError("候选账号显示名称不能为空")
        if len(name) > 100:
            raise ValueError("候选账号显示名称不能超过100字")
        if len(str(note or "")) > 500:
            raise ValueError("候选账号备注不能超过500字")
        account_id, canonical = normalize_profile_url(profile_url)

        def apply(accounts: list[dict[str, Any]], timestamp: str) -> dict[str, Any]:
            if any(row.get("account_id") == account_id or row.get("profile_url") == canonical for row in accounts):
                raise ValueError("该抖音候选账号已存在")
            item = {
                "account_id": account_id,
                "display_name": name,
                "profile_url": canonical,
                "lifecycle_status": "candidate",
                "enabled": True,
                "added_at": timestamp,
                "updated_at": timestamp,
                "note": str(note or "").strip(),
            }
            accounts.append(item)
            return dict(item)

        return self._mutate(apply)

    def set_status(self, account_id: str, status: str) -> dict[str, Any]:
        if status not in LIFECYCLE_STATUSES:
            raise ValueError("账号状态无效")

        def apply(accounts: list[dict[str, Any]], timestamp: str) -> dict[str, Any]:
            item = next((row for row in accounts if row.get("account_id") == account_id), None)
            if item is None:
                raise ValueError("候选账号不存在")
            item["lifecycle_status"] = status
            item["enabled"] = status not in {"paused", "rejected"}
            item["updated_at"] = timestamp
            return dict(item)

        return self._mutate(apply)

    def set_enabled(self, account_id: str, enabled: bool) -> dict[str, Any]:
        current = self.get(account_id)
        if not enabled:
            return self.set_status(account_id, "paused")
        return self.set_status(account_id, "candidate" if current.get("lifecycle_status") == "paused" else str(current["lifecycle_status"]))

    def update_note(self, account_id: str, note: str) -> dict[str, Any]:
        if len(str(note or "")) > 500:
            raise ValueError("候选账号备注不能超过500字")

        def apply(accounts: list[dict[str, Any]], timestamp: str) -> dict[str, Any]:
            item = next((row for row in accounts if row.get("account_id") == account_id), None)
            if item is None:
                raise ValueError("候选账号不存在")
            item["note"] = str(note or "").strip()
            item["updated_at"] = timestamp
            return dict(item)

        return self._mutate(apply)
