from __future__ import annotations

import json
import os
import subprocess
import sys
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import Any
from zoneinfo import ZoneInfo

from .collector import browser_status, close_project_browser
from .account_pool_ui import AccountPoolWindow
from .config import resolve_path
from .editorial_board import EditorialOverrideStore, apply_overrides, override_path, partition_views, write_editorial_exports
from .exporter import atomic_write_json
from .job_runtime import load_json, temp_size
from .llm_analysis import OpenAICompatibleAnalyzer
from .material_probe import latest_material_probe_report
from .visual_anchor import latest_visual_anchor_report
from .daily_material_pack import DailyMaterialPackError, latest_daily_material_pack_report, resolve_daily_material_pack_input
from .daily_material_exchange import latest_daily_material_exchange_report
from .scheduler import install, query, uninstall
from .trusted_ai_brief import latest_ranking_path


EDITORIAL_TABS = (("hotspots", "抖音科技总热榜"), ("news_leads", "科技新闻线索"), ("tech_talk", "科技杂谈灵感"), ("manual_review", "待分类/待处理"))
TRUSTED_VISUAL_STAGES = "阶段：解析账号 → 获取作品 → 下载临时视频 → 选择画面 → 本地OCR → 合并画面文字 → 确定性提炼/HTTPS模型 → 排名 → 输出（视觉账号不处理音频）"


def load_latest_editorial_report(config: dict[str, Any]) -> tuple[Path | None, dict[str, Any]]:
    state = load_json(resolve_path(config["jobs"]["state_path"]), {"jobs": {}})
    output = ((state.get("jobs") or {}).get("douyin_tech_ranking") or {}).get("output_path")
    json_path = Path(output).with_name("ranking.json") if output else None
    if json_path and json_path.is_file():
        return json_path, load_json(json_path, {})
    return None, {}


def editorial_card_text(item: dict[str, Any]) -> str:
    if "heat_rank" not in item:
        return f"待处理：{item.get('title', '元数据不足')}\n证据：{item.get('evidence_status', '-')}\n状态：{item.get('editorial_status', '-')}\n原因：{item.get('reason', '-')}"
    first = (item.get("representative_videos") or [{}])[0]
    return "\n".join([
        f"总榜 #{item['heat_rank']}  ·  热度 {item['heat_score']}",
        f"类型：{item['primary_content_type']}  ·  次类型：{item.get('secondary_content_types') or ['无']}",
        f"证据：{item['evidence_status']}  ·  编辑状态：{item['editorial_status']}",
        f"作者：{first.get('author', '-')}  ·  发布时间：{first.get('published_at', '-')}  ·  视频数：{item.get('video_count', 0)}",
        f"普通链接：{first.get('share_url', '-')}", f"热度分项：{item.get('heat_components')}",
        f"待核实：{item.get('claims_to_verify') or ['无']}", f"为什么值得关注：{item.get('why_worth_attention')}",
        f"新闻安全表述：{item.get('safe_hook')}", f"杂谈角度：{item.get('tech_talk_angle')}",
        f"禁止宣称：{item.get('do_not_claim')}", f"备注：{item.get('editor_note') or '无'}",
    ])


def refresh_editorial_report(config: dict[str, Any], json_path: Path) -> dict[str, Any]:
    report = load_json(json_path, {})
    report["hotspot_rankings"] = apply_overrides(report.get("hotspot_rankings") or [], EditorialOverrideStore(override_path(config)).load())
    report["views"] = partition_views(report["hotspot_rankings"], report.get("pending_items") or [])
    report["editorial_exports"] = write_editorial_exports(config, report)
    from .douyin_ranking import ranking_broadcast, ranking_markdown
    from .reporting import atomic_text
    report["broadcast"] = ranking_broadcast(report)
    atomic_write_json(json_path, report)
    atomic_text(json_path.with_name("ranking.md"), ranking_markdown(report))
    return report


def update_editorial_override(config: dict[str, Any], topic_id: str, values: dict[str, Any], report_path: Path | None = None) -> dict[str, Any]:
    result = EditorialOverrideStore(override_path(config)).update(topic_id, values)
    if report_path:
        refresh_editorial_report(config, report_path)
    return result


def _next_run(config: dict[str, Any]) -> str:
    zone = ZoneInfo(str(config["timezone"]))
    now = datetime.now(zone)
    hour, minute = [int(value) for value in str(config["jobs"]["daily_news"].get("schedule_time") or "02:00").split(":")]
    value = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if value <= now:
        value += timedelta(days=1)
    return value.strftime("%m-%d %H:%M")


def launch_detached(config_path: str, maximum: int) -> None:
    python = resolve_path(".venv/Scripts/python.exe")
    command = [str(python), "-m", "douyin_intelligence.cli", "--config", str(resolve_path(config_path)), "inspiration", "--max-references", str(maximum), "--live-douyin"]
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    subprocess.Popen(command, cwd=resolve_path("."), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)


def daily_news_command(config_path: str, target_date: str | None = None) -> list[str]:
    python = resolve_path(".venv/Scripts/python.exe")
    command = [str(python), "-m", "douyin_intelligence.cli", "--config", str(resolve_path(config_path)), "daily-news"]
    if target_date:
        command.extend(["--target-date", target_date])
    return command


def launch_daily_news(config_path: str, target_date: str | None = None) -> subprocess.Popen[Any]:
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(daily_news_command(config_path, target_date), cwd=resolve_path("."), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)


def douyin_tech_ranking_command(config_path: str, target_date: str | None = None) -> list[str]:
    python = resolve_path(".venv/Scripts/python.exe")
    command = [str(python), "-m", "douyin_intelligence.cli", "--config", str(resolve_path(config_path)), "douyin-tech-ranking", "--live-douyin"]
    if target_date:
        command.extend(["--target-date", target_date])
    return command


def launch_douyin_tech_ranking(config_path: str, target_date: str | None = None) -> subprocess.Popen[Any]:
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(douyin_tech_ranking_command(config_path, target_date), cwd=resolve_path("."), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)


def trusted_account_news_command(config_path: str, account_id: str, maximum: int = 10, *, auto_ai_brief: bool = True) -> list[str]:
    python = resolve_path(".venv/Scripts/python.exe")
    command = [
        str(python), "-m", "douyin_intelligence.cli", "--config", str(resolve_path(config_path)),
        "trusted-account-news", "--account-id", account_id, "--max-items", str(min(max(1, maximum), 10)), "--live",
    ]
    if not auto_ai_brief:
        command.append("--skip-ai-brief")
    return command


def trusted_ai_brief_command(config_path: str, ranking_path: Path) -> list[str]:
    python = resolve_path(".venv/Scripts/python.exe")
    return [
        str(python), "-m", "douyin_intelligence.cli", "--config", str(resolve_path(config_path)),
        "trusted-ai-brief", "--ranking", str(ranking_path.resolve()),
    ]


def browser_prepare_command(config_path: str) -> list[str]:
    python = resolve_path(".venv/Scripts/python.exe")
    return [str(python), "-m", "douyin_intelligence.cli", "--config", str(resolve_path(config_path)), "browser-start", "--allow-browser"]


def launch_browser_prepare(config_path: str) -> subprocess.Popen[Any]:
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(browser_prepare_command(config_path), cwd=resolve_path("."), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)


def process_running(process: subprocess.Popen[Any] | None) -> bool:
    return process is not None and process.poll() is None


def launch_trusted_account_news(config_path: str, account_id: str, maximum: int = 10, *, auto_ai_brief: bool = True) -> subprocess.Popen[Any]:
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(trusted_account_news_command(config_path, account_id, maximum, auto_ai_brief=auto_ai_brief), cwd=resolve_path("."), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)


def launch_trusted_ai_brief(config_path: str, ranking_path: Path) -> subprocess.Popen[Any]:
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(trusted_ai_brief_command(config_path, ranking_path), cwd=resolve_path("."), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)


def material_probe_command(config_path: str, story_path: str | None = None) -> list[str]:
    python = resolve_path(".venv/Scripts/python.exe")
    command = [
        str(python), "-m", "douyin_intelligence.cli", "--config", str(resolve_path(config_path)),
        "material-probe", "run",
    ]
    if story_path:
        command.extend(["--story", str(resolve_path(story_path))])
    return command


def launch_material_probe(config_path: str, story_path: str | None = None) -> subprocess.Popen[Any]:
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(material_probe_command(config_path, story_path), cwd=resolve_path("."), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)


def visual_anchor_command(config_path: str, stories_path: str | None = None, *, douyin_fallback: bool = False) -> list[str]:
    python = resolve_path(".venv/Scripts/python.exe")
    command = [
        str(python), "-m", "douyin_intelligence.cli", "--config", str(resolve_path(config_path)),
        "visual-anchor", "run",
    ]
    if stories_path:
        command.extend(["--stories", str(resolve_path(stories_path))])
    if douyin_fallback:
        command.append("--douyin-fallback")
    return command


def launch_visual_anchor(config_path: str, stories_path: str | None = None, *, douyin_fallback: bool = False) -> subprocess.Popen[Any]:
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(visual_anchor_command(config_path, stories_path, douyin_fallback=douyin_fallback), cwd=resolve_path("."), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)


def daily_material_pack_command(config_path: str, selection_path: str) -> list[str]:
    python = resolve_path(".venv/Scripts/python.exe")
    return [
        str(python), "-m", "douyin_intelligence.cli", "--config", str(resolve_path(config_path)),
        "daily-material-pack", "build", "--input", str(resolve_path(selection_path)), "--quick",
    ]


def launch_daily_material_pack(config_path: str, selection_path: str) -> subprocess.Popen[Any]:
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(daily_material_pack_command(config_path, selection_path), cwd=resolve_path("."), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)


def daily_material_exchange_command(config_path: str, business_date: str | None = None) -> list[str]:
    python = resolve_path(".venv/Scripts/python.exe")
    return [
        str(python), "-m", "douyin_intelligence.cli", "--config", str(resolve_path(config_path)),
        "daily-material-exchange", "run",
    ] + (["--business-date", business_date] if business_date else [])


def launch_daily_material_exchange(config_path: str, business_date: str | None = None) -> subprocess.Popen[Any]:
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(daily_material_exchange_command(config_path, business_date), cwd=resolve_path("."), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)


def trusted_ai_status_text(config: dict[str, Any], state_item: dict[str, Any]) -> str:
    ai = state_item.get("ai") or state_item.get("ai_brief") or {}
    llm = OpenAICompatibleAnalyzer(config).status()
    transport = ai.get("transport_security") or llm.get("transport_security")
    suffix = "（远程 HTTP 明文模式，不安全，用户已授权）" if transport == "insecure_http_user_authorized" else ""
    if state_item.get("status") == "running":
        return f"大模型整理：运行中{suffix}"
    if ai.get("status") in {"success", "cached"} or state_item.get("status") == "success" and state_item.get("output_path"):
        return f"大模型整理：已完成{suffix}"
    if ai.get("status") == "degraded" or state_item.get("status") == "partial":
        return f"大模型整理：已降级{suffix}"
    return f"大模型整理：已配置{suffix}" if llm.get("enabled") and llm.get("api_key_configured") else "大模型整理：未启用"


def trusted_report_to_open(config: dict[str, Any], account_id: str | None = None) -> Path | None:
    ranking = latest_ranking_path(config, account_id)
    if not ranking:
        return None
    ai_json = ranking.with_name("ai-brief.json")
    ai_markdown = ranking.with_name("ai-brief.md")
    if ai_json.is_file() and ai_markdown.is_file():
        payload = load_json(ai_json, {})
        if payload.get("status") == "success":
            return ai_markdown
    markdown = ranking.with_name("ranking.md")
    return markdown if markdown.is_file() else None


def brief_job_status(label: str, item: dict[str, Any]) -> str:
    counts = item.get("counts") or {}
    lines = [f"{label}：{item.get('status', '尚未运行')} / {item.get('phase', '-')}", f"  最近更新：{item.get('updated_at', '-')}", f"  数量：{json.dumps(counts, ensure_ascii=False)}"]
    if item.get("output_path"):
        lines.append(f"  报告：{item['output_path']}")
    errors = item.get("errors") or []
    if errors:
        source = (errors[0] or {}).get("source") if isinstance(errors[0], dict) else "任务"
        lines.append(f"  简短错误：{len(errors)} 项（{source or '任务'}）")
    return "\n".join(lines)


class Workbench:
    def __init__(self, root: tk.Tk, config: dict[str, Any], config_path: str):
        self.root, self.config, self.config_path = root, config, config_path
        root.title(str(config["workbench"].get("title") or "科技内容情报工作台"))
        root.geometry("1100x820")
        root.minsize(920, 700)
        self.maximum = tk.IntVar(value=int(config["jobs"]["inspiration"].get("default_reference_videos") or 100))
        self.trusted_maximum = tk.IntVar(value=int(config["jobs"]["trusted_account_news"].get("max_items") or 10))
        trusted_accounts = [item for item in config.get("trusted_news_accounts") or [] if item.get("enabled", True)]
        self.trusted_account_names = {str(item["name"]): str(item["id"]) for item in trusted_accounts}
        self.trusted_account_name = tk.StringVar(value=next(iter(self.trusted_account_names), ""))
        self.browser_state = tk.StringVar(value="项目浏览器：未启动")
        ai_settings = config["jobs"]["trusted_account_news"].get("ai_brief") or {}
        self.auto_ai_brief = tk.BooleanVar(value=bool(ai_settings.get("auto_after_collection", True)))
        self.ai_brief_state = tk.StringVar(value="大模型整理：正在读取状态…")
        self.trusted_report_path = tk.StringVar(value="本次报告：尚未生成")
        self.browser_prepare_process: subprocess.Popen[Any] | None = None
        self.trusted_process: subprocess.Popen[Any] | None = None
        self.ai_brief_process: subprocess.Popen[Any] | None = None
        self.material_probe_process: subprocess.Popen[Any] | None = None
        self.visual_anchor_process: subprocess.Popen[Any] | None = None
        self.daily_material_pack_process: subprocess.Popen[Any] | None = None
        self.daily_material_exchange_process: subprocess.Popen[Any] | None = None
        self.schedule_var = tk.BooleanVar(value=query(config)["ok"])
        self.summary = tk.StringVar(value="正在读取状态…")
        self.health = tk.StringVar(value="")
        self.editorial_detail = tk.StringVar(value="请选择一个热点查看编辑详情。")
        self.editorial_report_path: Path | None = None
        self.editorial_report: dict[str, Any] = {}
        self.editorial_trees: dict[str, ttk.Treeview] = {}
        self.account_pool_window: AccountPoolWindow | None = None
        frame = ttk.Frame(root, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="双轨科技选题编辑工作台", font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
        ttk.Label(frame, text="控制任务开关与查看结果；关闭窗口不会终止后台任务。", foreground="#5f6b7a").pack(anchor="w", pady=(4, 18))
        nightly = ttk.LabelFrame(frame, text="每日科技新闻", padding=14)
        nightly.pack(fill="x")
        ttk.Checkbutton(nightly, text="每天 02:00 自动运行", variable=self.schedule_var, command=self.toggle_schedule).grid(row=0, column=0, sticky="w")
        ttk.Button(nightly, text="立即生成昨日播报", command=self.start_daily_news).grid(row=0, column=1, padx=12)
        ttk.Label(nightly, text=f"下次计划：{_next_run(config)}  ·  电脑关机时不能执行").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ranking = ttk.LabelFrame(frame, text="抖音科技热点", padding=14)
        ranking.pack(fill="x", pady=(14, 0))
        ttk.Label(ranking, text="仅采集最多 10 条公开元数据；不下载媒体；新闻事实另由官方 HTTPS 来源核验。").grid(row=0, column=0, sticky="w")
        ttk.Button(ranking, text="生成抖音科技热榜", command=self.start_douyin_tech_ranking).grid(row=0, column=1, padx=12)
        ttk.Button(ranking, text="候选账号池 / 账号体检", command=self.open_account_pool).grid(row=0, column=2, padx=4)
        trusted = ttk.LabelFrame(frame, text="可信账号科技快讯", padding=14)
        trusted.pack(fill="x", pady=(14, 0))
        ttk.Label(trusted, text="账号").grid(row=0, column=0, sticky="w")
        ttk.Combobox(trusted, textvariable=self.trusted_account_name, values=list(self.trusted_account_names), state="readonly", width=20).grid(row=0, column=1, padx=8)
        ttk.Label(trusted, text="窗口 48 小时  ·  最多作品").grid(row=0, column=2, padx=(12, 4))
        ttk.Spinbox(trusted, from_=1, to=10, textvariable=self.trusted_maximum, width=5).grid(row=0, column=3)
        ttk.Button(trusted, text="获取近两日科技快讯并排行", command=self.start_trusted_account_news).grid(row=0, column=4, padx=12)
        ttk.Button(trusted, text="打开/复用抖音登录窗口", command=self.prepare_douyin_login).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(trusted, text="关闭项目抖音浏览器", command=self.close_douyin_browser).grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Label(trusted, textvariable=self.browser_state).grid(row=1, column=3, columnspan=2, sticky="w", padx=(12, 0), pady=(8, 0))
        ttk.Label(trusted, text=TRUSTED_VISUAL_STAGES).grid(row=2, column=0, columnspan=5, sticky="w", pady=(8, 0))
        ttk.Checkbutton(trusted, text="采集完成后自动进行大模型整理", variable=self.auto_ai_brief).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(trusted, textvariable=self.ai_brief_state).grid(row=3, column=2, columnspan=3, sticky="w", padx=(12, 0), pady=(8, 0))
        ttk.Button(trusted, text="用大模型整理本次报告", command=self.start_trusted_ai_brief).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(trusted, text="打开本次报告", command=self.open_trusted_report).grid(row=4, column=2, sticky="w", pady=(8, 0))
        ttk.Button(trusted, text="打开报告文件夹", command=self.open_trusted_report_folder).grid(row=4, column=3, sticky="w", pady=(8, 0))
        ttk.Label(trusted, textvariable=self.trusted_report_path, wraplength=960).grid(row=5, column=0, columnspan=5, sticky="w", pady=(8, 0))
        material_probe = ttk.LabelFrame(frame, text="已确认新闻视觉素材", padding=14)
        material_probe.pack(fill="x", pady=(14, 0))
        ttk.Label(material_probe, text="新闻视觉锚点最多顺序处理 3 条已确认新闻，每条最多保留主图+备用图；相关度与权利分开显示，不修改 OpenMontage。", wraplength=690).grid(row=0, column=0, sticky="w")
        ttk.Button(material_probe, text="运行新闻视觉锚点测试", command=self.start_visual_anchor).grid(row=0, column=1, padx=8)
        ttk.Button(material_probe, text="打开最新锚点报告", command=self.open_visual_anchor_report).grid(row=0, column=2, padx=4)
        ttk.Button(material_probe, text="旧版单条素材探针", command=self.start_material_probe).grid(row=1, column=1, padx=8, pady=(6, 0))
        ttk.Button(material_probe, text="打开旧版报告", command=self.open_material_probe_report).grid(row=1, column=2, padx=4, pady=(6, 0))
        daily_pack = ttk.LabelFrame(frame, text="每日科技新闻素材供应包", padding=14)
        daily_pack.pack(fill="x", pady=(14, 0))
        ttk.Label(daily_pack, text="一键装配已选择新闻、事实证据和通过三道门的图片；默认快速缓存，不调用大模型、音频、抖音或浏览器。", wraplength=680).grid(row=0, column=0, sticky="w")
        ttk.Button(daily_pack, text="一键生成每日科技素材供应包", command=self.start_daily_material_pack).grid(row=0, column=1, padx=8)
        ttk.Button(daily_pack, text="打开供应包报告", command=self.open_daily_material_pack_report).grid(row=0, column=2, padx=4)
        ttk.Button(daily_pack, text="打开供应包目录", command=self.open_daily_material_pack_folder).grid(row=1, column=2, padx=4, pady=(6, 0))
        exchange = ttk.LabelFrame(frame, text="每日02:00素材交换区", padding=14)
        exchange.pack(fill="x", pady=(14, 0))
        ttk.Label(exchange, text="动态获取北京时间昨日的抖音科技热点候选，原子发布 READY/current；显示阶段、计数、剩余时间与中文错误。", wraplength=680).grid(row=0, column=0, sticky="w")
        ttk.Button(exchange, text="获取昨日抖音科技热点候选并发布", command=self.start_daily_material_exchange).grid(row=0, column=1, padx=8)
        ttk.Button(exchange, text="打开交换包简报", command=self.open_daily_material_exchange_report).grid(row=0, column=2, padx=4)
        ttk.Button(exchange, text="打开交换区目录", command=self.open_daily_material_exchange_folder).grid(row=1, column=2, padx=4, pady=(6, 0))
        inspiration = ttk.LabelFrame(frame, text="随时找灵感", padding=14)
        inspiration.pack(fill="x", pady=14)
        ttk.Label(inspiration, text="最大参考视频数").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(inspiration, from_=10, to=int(config["jobs"]["inspiration"]["hard_max_reference_videos"]), increment=10, textvariable=self.maximum, width=8).grid(row=0, column=1, padx=10)
        ttk.Button(inspiration, text="一键生成灵感", command=self.start_inspiration).grid(row=0, column=2, padx=10)
        editorial = ttk.LabelFrame(frame, text="双轨科技选题编辑台", padding=10)
        editorial.pack(fill="both", expand=True, pady=(0, 14))
        self.editorial_tabs = ttk.Notebook(editorial)
        self.editorial_tabs.pack(fill="both", expand=True)
        for key, label in EDITORIAL_TABS:
            tab = ttk.Frame(self.editorial_tabs, padding=6)
            self.editorial_tabs.add(tab, text=label)
            tree = ttk.Treeview(tab, columns=("rank", "title", "type", "evidence", "status"), show="headings", height=5)
            for column, title, width in (("rank", "名次", 55), ("title", "主题", 360), ("type", "主类型", 125), ("evidence", "证据", 145), ("status", "编辑状态", 155)):
                tree.heading(column, text=title)
                tree.column(column, width=width, anchor="w")
            tree.pack(fill="both", expand=True)
            tree.bind("<<TreeviewSelect>>", self._show_editorial_detail)
            self.editorial_trees[key] = tree
        ttk.Label(editorial, textvariable=self.editorial_detail, justify="left", wraplength=1020).pack(fill="x", pady=(8, 4))
        edit_actions = ttk.Frame(editorial)
        edit_actions.pack(fill="x")
        for label, content_type in (("标为新闻", "news_lead"), ("原创评测", "creator_review"), ("原创实验", "creator_experiment"), ("原创教程", "creator_tutorial"), ("原创观点", "creator_opinion"), ("混合", "mixed"), ("待分类", "uncertain")):
            ttk.Button(edit_actions, text=label, command=lambda value=content_type: self._set_editorial_type(value)).pack(side="left", padx=2)
        ttk.Button(edit_actions, text="忽略/恢复", command=self._toggle_ignored).pack(side="left", padx=2)
        ttk.Button(edit_actions, text="置顶", command=lambda: self._toggle_flag("pinned")).pack(side="left", padx=2)
        ttk.Button(edit_actions, text="视频候选", command=lambda: self._toggle_flag("video_candidate")).pack(side="left", padx=2)
        ttk.Button(edit_actions, text="编辑备注", command=self._edit_note).pack(side="left", padx=2)
        status = ttk.LabelFrame(frame, text="项目状态", padding=14)
        status.pack(fill="both", expand=True)
        ttk.Label(status, textvariable=self.summary, justify="left", wraplength=740).pack(anchor="w")
        ttk.Separator(status).pack(fill="x", pady=12)
        ttk.Label(status, textvariable=self.health, justify="left", wraplength=740).pack(anchor="w")
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(14, 0))
        ttk.Button(actions, text="刷新", command=self.refresh).pack(side="left")
        ttk.Button(actions, text="打开最新输出", command=self.open_latest).pack(side="left", padx=8)
        self.refresh()

    def toggle_schedule(self) -> None:
        result = install(self.config) if self.schedule_var.get() else uninstall(self.config)
        if not result["ok"]:
            self.schedule_var.set(not self.schedule_var.get())
            messagebox.showerror("计划任务", result["output"][:500])
        self.refresh()

    def start_inspiration(self) -> None:
        maximum = min(max(10, int(self.maximum.get())), int(self.config["jobs"]["inspiration"]["hard_max_reference_videos"]))
        self.maximum.set(maximum)
        launch_detached(self.config_path, maximum)
        messagebox.showinfo("已启动", "灵感任务已在后台启动，可以关闭工作台。")
        self.root.after(1000, self.refresh)

    def start_daily_news(self) -> None:
        try:
            launch_daily_news(self.config_path)
        except OSError as exc:
            messagebox.showerror("每日新闻", f"无法启动每日新闻任务：{type(exc).__name__}")
            return
        messagebox.showinfo("每日新闻", "已启动昨日新闻播报；状态、报告路径和简短错误会在本页更新。")
        self._poll_daily_news(120)

    def start_douyin_tech_ranking(self) -> None:
        try:
            launch_douyin_tech_ranking(self.config_path)
        except OSError as exc:
            messagebox.showerror("抖音科技热榜", f"无法启动热榜任务：{type(exc).__name__}")
            return
        messagebox.showinfo("抖音科技热榜", "已启动受限抖音元数据采集与官方核验；状态和报告路径会在本页更新。若出现登录、二维码或验证码，请在专用浏览器完成验证后重试。")
        self._poll_douyin_tech_ranking(120)

    def start_trusted_account_news(self) -> None:
        account_id = self.trusted_account_names.get(self.trusted_account_name.get())
        if not account_id:
            messagebox.showerror("可信账号科技快讯", "请选择一个已配置的可信账号。")
            return
        maximum = min(max(1, int(self.trusted_maximum.get())), 10)
        self.trusted_maximum.set(maximum)
        if process_running(self.trusted_process):
            messagebox.showinfo("可信账号科技快讯", "任务已经在运行，请勿重复点击。")
            return
        try:
            self.trusted_process = launch_trusted_account_news(self.config_path, account_id, maximum, auto_ai_brief=bool(self.auto_ai_brief.get()))
        except OSError as exc:
            messagebox.showerror("可信账号科技快讯", f"无法启动任务：{type(exc).__name__}")
            return
        messagebox.showinfo("可信账号科技快讯", "已启动单账号近 48 小时任务。若专用浏览器出现登录、二维码或验证码，请完成操作后重新点击；不会绕过认证。")
        self._poll_trusted_account_news(900)

    def open_account_pool(self) -> None:
        if self.account_pool_window is not None and self.account_pool_window.window.winfo_exists():
            self.account_pool_window.window.lift()
            self.account_pool_window.window.focus_force()
            return
        self.account_pool_window = AccountPoolWindow(self.root, self.config, self.config_path)

    def start_trusted_ai_brief(self) -> None:
        account_id = self.trusted_account_names.get(self.trusted_account_name.get())
        ranking = latest_ranking_path(self.config, account_id)
        if not ranking:
            messagebox.showerror("大模型整理", "还没有可整理的可信账号 ranking.json；请先完成一次采集。")
            return
        if process_running(self.ai_brief_process):
            messagebox.showinfo("大模型整理", "本次报告正在整理，请勿重复点击。")
            return
        try:
            self.ai_brief_process = launch_trusted_ai_brief(self.config_path, ranking)
        except OSError as exc:
            messagebox.showerror("大模型整理", f"无法启动整理任务：{type(exc).__name__}")
            return
        self.ai_brief_state.set("大模型整理：运行中")
        messagebox.showinfo("大模型整理", "只整理现有报告；不会重新采集、打开浏览器或运行 OCR。")
        self._poll_trusted_ai_brief(140)

    def start_material_probe(self) -> None:
        if process_running(self.material_probe_process):
            messagebox.showinfo("新闻素材测试", "素材测试已经在运行，请勿重复点击。")
            return
        try:
            self.material_probe_process = launch_material_probe(self.config_path)
        except OSError as exc:
            messagebox.showerror("新闻素材测试", f"无法启动素材测试：{type(exc).__name__}")
            return
        messagebox.showinfo("新闻素材测试", "已启动单条官方新闻素材测试；不会调用大模型、OCR、ASR或抖音浏览器，也不会写入 OpenMontage。")
        self._poll_material_probe(320)

    def start_visual_anchor(self) -> None:
        if process_running(self.visual_anchor_process):
            messagebox.showinfo("新闻视觉锚点", "视觉锚点任务已经在运行，请勿重复点击。")
            return
        try:
            self.visual_anchor_process = launch_visual_anchor(self.config_path)
        except OSError as exc:
            messagebox.showerror("新闻视觉锚点", f"无法启动视觉锚点任务：{type(exc).__name__}")
            return
        messagebox.showinfo("新闻视觉锚点", "已启动受控新闻视觉锚点测试；默认先走官方/原文图片，不会调用大模型、音频或 OpenMontage。")
        self._poll_visual_anchor(320)

    def start_daily_material_pack(self) -> None:
        if process_running(self.daily_material_pack_process):
            messagebox.showinfo("每日科技素材供应包", "供应包任务已经在运行，请勿重复点击。")
            return
        try:
            selection = resolve_daily_material_pack_input(self.config)
            self.daily_material_pack_process = launch_daily_material_pack(self.config_path, str(selection))
        except (DailyMaterialPackError, OSError, ValueError) as exc:
            messagebox.showerror("每日科技素材供应包", f"无法启动：{str(exc)[:300]}\n请检查唯一有效的selection输入。")
            return
        messagebox.showinfo("每日科技素材供应包", "已启动快速装配；不会调用大模型、ASR、音频、抖音或浏览器，也不会写入OpenMontage。")
        self._poll_daily_material_pack(320)

    def _poll_daily_material_pack(self, remaining: int) -> None:
        self.refresh()
        state = load_json(resolve_path(self.config["jobs"]["state_path"]), {"jobs": {}})
        item = ((state.get("jobs") or {}).get("daily_material_pack") or {})
        if remaining > 0 and item.get("status") == "running":
            self.root.after(1000, lambda: self._poll_daily_material_pack(remaining - 1))
            return
        self.daily_material_pack_process = None
        counts = item.get("counts") or {}
        usage = item.get("usage") or {}
        messagebox.showinfo(
            "每日科技素材供应包",
            f"状态：{item.get('status', '未知')}；新闻 {counts.get('stories', 0)} 条；图片 {counts.get('images', 0)} 张；"
            f"耗时 {usage.get('elapsed_seconds', '-')} 秒。\n报告：{item.get('output_path', '-')}",
        )

    def open_daily_material_pack_report(self) -> None:
        report = latest_daily_material_pack_report(self.config)
        if not report:
            messagebox.showinfo("每日科技素材供应包", "还没有可打开的供应包报告。")
            return
        os.startfile(report)  # type: ignore[attr-defined]

    def open_daily_material_pack_folder(self) -> None:
        report = latest_daily_material_pack_report(self.config)
        if not report:
            messagebox.showinfo("每日科技素材供应包", "还没有可打开的供应包目录。")
            return
        os.startfile(report.parent)  # type: ignore[attr-defined]

    def start_daily_material_exchange(self) -> None:
        if process_running(self.daily_material_exchange_process):
            messagebox.showinfo("每日02:00素材交换区", "交换区发布正在运行，请勿重复点击。")
            return
        try:
            self.daily_material_exchange_process = launch_daily_material_exchange(self.config_path)
        except OSError as exc:
            messagebox.showerror("每日02:00素材交换区", f"无法启动：{type(exc).__name__}")
            return
        messagebox.showinfo("每日02:00素材交换区", "已启动昨日抖音科技热点候选池；只采集公开元数据，事实由 OP 后续核验，不会写入 OpenMontage。")
        self._poll_daily_material_exchange(3600)

    def _poll_daily_material_exchange(self, remaining: int) -> None:
        self.refresh()
        state = load_json(resolve_path(self.config["jobs"]["state_path"]), {"jobs": {}})
        item = ((state.get("jobs") or {}).get("daily_hot_candidate_pool_v2") or {})
        if remaining > 0 and item.get("status") == "running":
            self.summary.set(f"昨日抖音科技热点候选池：阶段 {item.get('phase', '-')}；数量 {json.dumps(item.get('counts') or {}, ensure_ascii=False)}；剩余预算约 {remaining} 秒。")
            self.root.after(1000, lambda: self._poll_daily_material_exchange(remaining - 1))
            return
        self.daily_material_exchange_process = None
        counts = item.get("counts") or {}
        messagebox.showinfo(
            "每日02:00素材交换区",
            f"状态：{item.get('status', '未知')}；候选 {counts.get('candidates', 0)} 条；图片 {counts.get('images', 0)} 张；"
            f"阶段：{item.get('phase', '-')}。\n报告：{item.get('output_path', '-')}",
        )

    def open_daily_material_exchange_report(self) -> None:
        report = latest_daily_material_exchange_report(self.config)
        if not report:
            messagebox.showinfo("每日02:00素材交换区", "还没有通过 READY/current 发布的交换包简报。")
            return
        os.startfile(report)  # type: ignore[attr-defined]

    def open_daily_material_exchange_folder(self) -> None:
        report = latest_daily_material_exchange_report(self.config)
        if not report:
            messagebox.showinfo("每日02:00素材交换区", "还没有通过 READY/current 发布的交换区目录。")
            return
        os.startfile(report.parent)  # type: ignore[attr-defined]

    def open_visual_anchor_report(self) -> None:
        report = latest_visual_anchor_report(self.config)
        if not report:
            messagebox.showinfo("新闻视觉锚点", "还没有可打开的视觉锚点报告。")
            return
        os.startfile(report)  # type: ignore[attr-defined]

    def open_material_probe_report(self) -> None:
        report = latest_material_probe_report(self.config)
        if not report:
            messagebox.showinfo("新闻素材测试", "还没有可打开的素材测试报告。")
            return
        os.startfile(report)  # type: ignore[attr-defined]

    def open_trusted_report(self) -> None:
        account_id = self.trusted_account_names.get(self.trusted_account_name.get())
        path = trusted_report_to_open(self.config, account_id)
        if not path:
            messagebox.showinfo("本次报告", "还没有可打开的可信账号报告。")
            return
        os.startfile(path)  # type: ignore[attr-defined]

    def open_trusted_report_folder(self) -> None:
        account_id = self.trusted_account_names.get(self.trusted_account_name.get())
        ranking = latest_ranking_path(self.config, account_id)
        if not ranking:
            messagebox.showinfo("报告文件夹", "还没有可信账号报告文件夹。")
            return
        os.startfile(ranking.parent)  # type: ignore[attr-defined]

    def prepare_douyin_login(self) -> None:
        if process_running(self.browser_prepare_process):
            messagebox.showinfo("抖音登录", "项目登录窗口正在准备，请勿重复点击。")
            return
        try:
            self.browser_prepare_process = launch_browser_prepare(self.config_path)
        except OSError as exc:
            messagebox.showerror("抖音登录", f"无法准备项目浏览器：{type(exc).__name__}")
            return
        messagebox.showinfo("抖音登录", "已启动或复用项目专用浏览器。请只在该窗口人工登录；不要向项目提供账号密码、Cookie或认证材料。")
        self.root.after(1000, self.refresh)

    def close_douyin_browser(self) -> None:
        result = close_project_browser(self.config)
        if result["status"] == "busy":
            messagebox.showinfo("项目浏览器", "仍有项目任务在使用浏览器，当前不会关闭。")
        elif result["status"] == "close_failed":
            messagebox.showerror("项目浏览器", "项目专用浏览器未能在限时内优雅关闭；没有结束个人Chrome。")
        else:
            messagebox.showinfo("项目浏览器", "项目专用浏览器已关闭或原本未运行；持久登录目录未删除。")
        self.refresh()

    def _poll_daily_news(self, remaining: int) -> None:
        self.refresh()
        state = load_json(resolve_path(self.config["jobs"]["state_path"]), {"jobs": {}})
        item = ((state.get("jobs") or {}).get("daily_news") or {})
        if remaining > 0 and item.get("status") == "running":
            self.root.after(1000, lambda: self._poll_daily_news(remaining - 1))

    def _poll_douyin_tech_ranking(self, remaining: int) -> None:
        self.refresh()
        state = load_json(resolve_path(self.config["jobs"]["state_path"]), {"jobs": {}})
        item = ((state.get("jobs") or {}).get("douyin_tech_ranking") or {})
        if remaining > 0 and item.get("status") == "running":
            self.root.after(1000, lambda: self._poll_douyin_tech_ranking(remaining - 1))

    def _poll_trusted_account_news(self, remaining: int) -> None:
        self.refresh()
        state = load_json(resolve_path(self.config["jobs"]["state_path"]), {"jobs": {}})
        item = ((state.get("jobs") or {}).get("trusted_account_news") or {})
        if remaining > 0 and item.get("status") == "running":
            self.root.after(1000, lambda: self._poll_trusted_account_news(remaining - 1))
        else:
            self.trusted_process = None
            counts = item.get("counts") or {}
            ai = item.get("ai_brief") or {}
            messagebox.showinfo(
                "可信账号科技快讯",
                f"内容提取完成；处理 {counts.get('processed', 0)} 条。\n"
                f"大模型整理：{ai.get('status', '未运行')}。\n报告：{item.get('output_path', '-')}"
            )

    def _poll_trusted_ai_brief(self, remaining: int) -> None:
        self.refresh()
        state = load_json(resolve_path(self.config["jobs"]["state_path"]), {"jobs": {}})
        item = ((state.get("jobs") or {}).get("trusted_news_ai_brief") or {})
        if remaining > 0 and item.get("status") == "running":
            self.root.after(1000, lambda: self._poll_trusted_ai_brief(remaining - 1))
        else:
            self.ai_brief_process = None
            ai = item.get("ai") or {}
            messagebox.showinfo(
                "大模型整理",
                f"处理 {((item.get('counts') or {}).get('input_items', 0))} 条；"
                f"状态 {ai.get('status', item.get('status', '未知'))}；"
                f"网络尝试 {ai.get('network_attempt_count', ai.get('request_count', 0))} 次；"
                f"成功请求 {ai.get('request_count', 0)} 次；缓存 {ai.get('cache_hit', False)}。\n"
                f"报告：{item.get('output_path', '-')}"
            )

    def _poll_material_probe(self, remaining: int) -> None:
        self.refresh()
        state = load_json(resolve_path(self.config["jobs"]["state_path"]), {"jobs": {}})
        item = ((state.get("jobs") or {}).get("material_probe") or {})
        if remaining > 0 and item.get("status") == "running":
            self.root.after(1000, lambda: self._poll_material_probe(remaining - 1))
            return
        self.material_probe_process = None
        counts = item.get("counts") or {}
        messagebox.showinfo(
            "新闻素材测试",
            f"状态：{item.get('status', '未知')}；素材 {counts.get('assets', 0)} 个；"
            f"可渲染 {counts.get('renderable', 0)} 个；需复核 {counts.get('review_required', 0)} 个。\n"
            f"报告：{item.get('output_path', '-')}"
        )

    def _poll_visual_anchor(self, remaining: int) -> None:
        self.refresh()
        state = load_json(resolve_path(self.config["jobs"]["state_path"]), {"jobs": {}})
        item = ((state.get("jobs") or {}).get("visual_anchor") or {})
        if remaining > 0 and item.get("status") == "running":
            self.root.after(1000, lambda: self._poll_visual_anchor(remaining - 1))
            return
        self.visual_anchor_process = None
        counts = item.get("counts") or {}
        messagebox.showinfo(
            "新闻视觉锚点",
            f"状态：{item.get('status', '未知')}；新闻 {counts.get('stories', 0)} 条；"
            f"视觉素材 {counts.get('assets', 0)} 项；失败 {counts.get('failed', 0)} 条。\n"
            f"报告：{item.get('output_path', '-')}"
        )

    def _load_editorial_views(self) -> None:
        self.editorial_report_path, self.editorial_report = load_latest_editorial_report(self.config)
        views = self.editorial_report.get("views") or {}
        for key, _label in EDITORIAL_TABS:
            tree = self.editorial_trees[key]
            children = tree.get_children()
            if children:
                tree.delete(*children)
            for item in views.get(key) or []:
                topic_id = str(item.get("topic_id") or f"pending-{len(tree.get_children())}")
                rank = item.get("heat_rank") or "-"
                title = ("📌 " if item.get("pinned") else "") + str(item.get("title") or "元数据不足")
                tree.insert("", "end", iid=topic_id, values=(rank, title[:120], item.get("primary_content_type", "uncertain"), item.get("evidence_status", "insufficient_metadata"), item.get("editorial_status", "manual_review")))

    def _selected_editorial_item(self) -> dict[str, Any] | None:
        index = self.editorial_tabs.index(self.editorial_tabs.select())
        key = EDITORIAL_TABS[index][0]
        tree = self.editorial_trees[key]
        selection = tree.selection()
        if not selection:
            return None
        topic_id = selection[0]
        return next((item for item in (self.editorial_report.get("views") or {}).get(key, []) if item.get("topic_id") == topic_id), None)

    def _show_editorial_detail(self, _event: Any = None) -> None:
        item = self._selected_editorial_item()
        self.editorial_detail.set(editorial_card_text(item) if item else "请选择一个热点查看编辑详情。")

    def _apply_editorial_change(self, values: dict[str, Any]) -> None:
        item = self._selected_editorial_item()
        if not item or not str(item.get("topic_id") or "").startswith("topic-"):
            messagebox.showinfo("编辑操作", "请选择一个具有稳定主题 ID 的热点。")
            return
        try:
            update_editorial_override(self.config, item["topic_id"], values, self.editorial_report_path)
        except (OSError, ValueError, TimeoutError) as exc:
            messagebox.showerror("编辑操作", f"保存失败：{type(exc).__name__}")
            return
        self._load_editorial_views()

    def _set_editorial_type(self, content_type: str) -> None:
        self._apply_editorial_change({"primary_content_type": content_type})

    def _toggle_ignored(self) -> None:
        item = self._selected_editorial_item()
        if item:
            self._apply_editorial_change({"ignored": not bool(item.get("ignored"))})

    def _toggle_flag(self, field: str) -> None:
        item = self._selected_editorial_item()
        if item:
            self._apply_editorial_change({field: not bool(item.get(field))})

    def _edit_note(self) -> None:
        item = self._selected_editorial_item()
        if not item:
            return
        note = simpledialog.askstring("编辑备注", "输入不超过 500 字的编辑备注：", initialvalue=str(item.get("editor_note") or ""), parent=self.root)
        if note is not None:
            self._apply_editorial_change({"editor_note": note})

    def refresh(self) -> None:
        state = load_json(resolve_path(self.config["jobs"]["state_path"]), {"jobs": {}})
        lines = []
        for name, label in (("daily_hot_candidate_pool_v2", "昨日抖音科技热点候选池 V2"), ("daily_material_pack", "每日科技素材供应包"), ("daily_news", "每日新闻"), ("trusted_account_news", "可信账号快讯"), ("account_pool_evaluation", "候选账号体检"), ("visual_anchor", "新闻视觉锚点"), ("material_probe", "旧版新闻素材测试"), ("douyin_tech_ranking", "抖音科技热榜"), ("inspiration", "灵感任务")):
            item = (state.get("jobs") or {}).get(name) or {}
            lines.append(brief_job_status(label, item))
        self.summary.set("\n\n".join(lines))
        browser = browser_status(self.config)
        browser_labels = {"not_started": "未启动", "waiting_for_login": "等待人工登录", "connected_ready": "已连接可采集", "task_running": "任务运行中", "completed_closed": "任务完成已关闭"}
        self.browser_state.set(f"项目浏览器：{browser_labels.get(browser['state'], browser['state'])} / 页面 {browser['page_count']}")
        llm = OpenAICompatibleAnalyzer(self.config).status()
        ai_item = ((state.get("jobs") or {}).get("trusted_news_ai_brief") or {})
        self.ai_brief_state.set(trusted_ai_status_text(self.config, ai_item))
        selected_account = self.trusted_account_names.get(self.trusted_account_name.get())
        current_report = trusted_report_to_open(self.config, selected_account)
        self.trusted_report_path.set(f"本次报告：{current_report}" if current_report else "本次报告：尚未生成")
        schedule = "已启用" if query(self.config)["ok"] else "未启用"
        self.health.set(f"计划任务：{schedule}  ·  抖音浏览器：{browser_labels.get(browser['state'], browser['state'])}\n中转站：{'已配置' if llm['api_key_configured'] else '未配置'}  ·  新闻源：{len(self.config['jobs']['daily_news']['sources'])} 个\n临时目录：{temp_size(self.config) / 1024 / 1024:.1f} MiB")
        self._load_editorial_views()

    def open_latest(self) -> None:
        state = load_json(resolve_path(self.config["jobs"]["state_path"]), {"jobs": {}})
        candidates = [(item or {}).get("output_path") for item in (state.get("jobs") or {}).values()]
        existing = [Path(value) for value in candidates if value and Path(value).is_file()]
        if not existing:
            messagebox.showinfo("最新输出", "还没有可打开的报告。")
            return
        latest = max(existing, key=lambda path: path.stat().st_mtime)
        os.startfile(latest)  # type: ignore[attr-defined]


def run_workbench(config: dict[str, Any], config_path: str, *, auto_close_ms: int | None = None) -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if "vista" in ttk.Style().theme_names() else "clam")
        Workbench(root, config, config_path)
        if auto_close_ms is not None:
            root.after(auto_close_ms, root.destroy)
        root.mainloop()
    except Exception:
        try:
            root.destroy()
        except tk.TclError:
            pass
        raise
