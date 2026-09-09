from __future__ import annotations

import os
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import Any

from .account_pool import AccountPoolStore
from .config import resolve_path
from .job_runtime import load_json


def account_pool_evaluation_command(
    config_path: str,
    *,
    account_id: str | None = None,
    live: bool = True,
    target_date: str | None = None,
) -> list[str]:
    python = resolve_path(".venv/Scripts/python.exe")
    command = [
        str(python), "-m", "douyin_intelligence.cli", "--config", str(resolve_path(config_path)),
        "account-pool", "evaluate",
    ]
    if account_id:
        command.extend(["--account-id", account_id])
    else:
        command.append("--all-enabled")
    if target_date:
        command.extend(["--target-date", target_date])
    if live:
        command.append("--live")
    return command


def launch_account_pool_evaluation(config_path: str, *, account_id: str | None = None) -> subprocess.Popen[Any]:
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(
        account_pool_evaluation_command(config_path, account_id=account_id, live=True),
        cwd=resolve_path("."), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags,
    )


def account_pool_status_text(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "not_started")
    counts = item.get("counts") or {}
    if status == "running":
        return "账号体检：运行中"
    if status == "needs_login":
        return "账号体检：等待登录/人工验证"
    if int(counts.get("needs_login") or 0):
        return "账号体检：部分完成，等待登录/人工验证"
    if int(counts.get("identity_mismatch") or 0):
        return "账号体检：身份不匹配，请检查候选主页"
    labels = {
        "success": "完成",
        "partial": "部分完成",
        "empty": "数据不足/窗口内无作品",
        "failed": "失败",
        "not_started": "未运行",
    }
    return f"账号体检：{labels.get(status, status)}"


def latest_account_evaluation(config: dict[str, Any]) -> Path | None:
    root = Path(config.get("_project_root") or Path(__file__).resolve().parents[2])
    value = Path(config["jobs"]["account_pool"]["output_root"])
    output_root = value if value.is_absolute() else root / value
    candidates = list(output_root.glob("*/account-evaluation.md")) if output_root.exists() else []
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


class AccountPoolWindow:
    def __init__(self, parent: tk.Misc, config: dict[str, Any], config_path: str):
        self.config, self.config_path = config, config_path
        self.store = AccountPoolStore(config)
        self.process: subprocess.Popen[Any] | None = None
        self.window = tk.Toplevel(parent)
        self.window.title("候选账号池 / 账号体检")
        self.window.geometry("980x560")
        self.window.minsize(820, 480)
        self.status = tk.StringVar(value="账号体检：未运行")
        self.report_path = tk.StringVar(value="最新报告：尚未生成")

        frame = ttk.Frame(self.window, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="候选账号池 / 账号体检", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="候选建议不会自动晋级可信账号；默认只读取14日公开元数据，不下载媒体、不运行ASR/OCR/大模型。",
            foreground="#5f6b7a",
        ).pack(anchor="w", pady=(4, 10))
        self.tree = ttk.Treeview(frame, columns=("name", "status", "enabled", "updated"), show="headings", height=12)
        for column, label, width in (("name", "账号", 220), ("status", "生命周期", 140), ("enabled", "启用", 80), ("updated", "更新时间", 220)):
            self.tree.heading(column, text=label)
            self.tree.column(column, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True)

        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(10, 0))
        for label, callback in (
            ("添加候选账号", self.add_candidate),
            ("启用/暂停", self.toggle_enabled),
            ("体检选中账号", self.evaluate_selected),
            ("体检全部启用候选", self.evaluate_all),
            ("晋级正式账号", self.promote),
            ("排除", self.reject),
            ("打开最新报告", self.open_report),
        ):
            ttk.Button(actions, text=label, command=callback).pack(side="left", padx=3)
        ttk.Label(frame, textvariable=self.status).pack(anchor="w", pady=(10, 0))
        ttk.Label(frame, textvariable=self.report_path, wraplength=920).pack(anchor="w", pady=(4, 0))
        self.refresh()

    def _selected_id(self) -> str | None:
        selection = self.tree.selection()
        return selection[0] if selection else None

    def refresh(self) -> None:
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        for item in self.store.list_accounts():
            self.tree.insert("", "end", iid=item["account_id"], values=(
                item["display_name"], item["lifecycle_status"], "是" if item.get("enabled") else "否", item.get("updated_at") or "-",
            ))
        state = load_json(resolve_path(self.config["jobs"]["state_path"]), {"jobs": {}})
        job = ((state.get("jobs") or {}).get("account_pool_evaluation") or {})
        self.status.set(account_pool_status_text(job))
        report = latest_account_evaluation(self.config)
        self.report_path.set(f"最新报告：{report}" if report else "最新报告：尚未生成")

    def add_candidate(self) -> None:
        name = simpledialog.askstring("添加候选账号", "显示名称：", parent=self.window)
        if not name:
            return
        url = simpledialog.askstring("添加候选账号", "抖音用户主页 URL：", parent=self.window)
        if not url:
            return
        note = simpledialog.askstring("添加候选账号", "备注（可选，不超过500字）：", parent=self.window) or ""
        try:
            self.store.add_candidate(name, url, note=note)
        except (OSError, RuntimeError, ValueError) as exc:
            messagebox.showerror("添加候选账号", str(exc), parent=self.window)
            return
        self.refresh()

    def toggle_enabled(self) -> None:
        account_id = self._selected_id()
        if not account_id:
            messagebox.showinfo("候选账号", "请先选择一个账号。", parent=self.window)
            return
        item = self.store.get(account_id)
        self.store.set_enabled(account_id, not bool(item.get("enabled")))
        self.refresh()

    def _start(self, account_id: str | None) -> None:
        if self.process is not None and self.process.poll() is None:
            messagebox.showinfo("账号体检", "账号体检已经在运行，请勿重复点击。", parent=self.window)
            return
        try:
            self.process = launch_account_pool_evaluation(self.config_path, account_id=account_id)
        except OSError as exc:
            messagebox.showerror("账号体检", f"无法启动账号体检：{type(exc).__name__}", parent=self.window)
            return
        self.status.set("账号体检：运行中")
        self.window.after(1000, self._poll)

    def evaluate_selected(self) -> None:
        account_id = self._selected_id()
        if not account_id:
            messagebox.showinfo("账号体检", "请先选择一个账号。", parent=self.window)
            return
        self._start(account_id)

    def evaluate_all(self) -> None:
        self._start(None)

    def _poll(self) -> None:
        self.refresh()
        if self.process is not None and self.process.poll() is None:
            self.window.after(1000, self._poll)
            return
        self.process = None
        state = load_json(resolve_path(self.config["jobs"]["state_path"]), {"jobs": {}})
        item = ((state.get("jobs") or {}).get("account_pool_evaluation") or {})
        messagebox.showinfo("账号体检", f"{account_pool_status_text(item)}\n报告：{item.get('output_path', '-')}", parent=self.window)

    def promote(self) -> None:
        self._confirm_status("trusted", "确定将该候选手动晋级为正式账号吗？算法建议不会自动执行此操作。")

    def reject(self) -> None:
        self._confirm_status("rejected", "确定排除该候选吗？记录会保留，可稍后恢复为候选。")

    def _confirm_status(self, status: str, prompt: str) -> None:
        account_id = self._selected_id()
        if not account_id:
            messagebox.showinfo("候选账号", "请先选择一个账号。", parent=self.window)
            return
        if not messagebox.askyesno("确认人工操作", prompt, parent=self.window):
            return
        self.store.set_status(account_id, status)
        self.refresh()

    def open_report(self) -> None:
        report = latest_account_evaluation(self.config)
        if report is None:
            messagebox.showinfo("账号体检", "还没有可打开的账号体检报告。", parent=self.window)
            return
        os.startfile(report)  # type: ignore[attr-defined]
