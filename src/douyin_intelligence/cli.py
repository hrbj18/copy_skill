from __future__ import annotations

import argparse
import json
import subprocess
import sys
import re
from pathlib import Path
from typing import Any

from .config import ConfigurationError, load_config, resolve_path
from .collector import BrowserSession, close_project_browser, collect_creators, creator_commands, prepare_douyin_login
from .account_evaluation import run_account_evaluation
from .account_pool import AccountPoolStore
from .doctor import run_doctor
from .exporter import atomic_write_json
from .material_pipeline import build_materials
from .material_probe import run_material_probe
from .visual_anchor import check_visual_anchor_rss_health, run_domestic_visual_anchor_smoke, run_visual_anchor_batch, summarize_domestic_visual_anchor_outputs
from .daily_material_pack import build_daily_material_pack, validate_daily_material_pack
from .daily_material_exchange import inspect_daily_material_exchange, simulate_daily_material_exchange
from .daily_hot_candidate_pool import promote_existing_v2_pack, run_account_matrix_topic_radar_smoke, run_daily_hot_candidate_pool
from .llm_analysis import OpenAICompatibleAnalyzer
from .local_secrets import llm_secret_status
from .resource_control import cleanup_temp_root
from .normalize import normalize_files
from .pipeline import run_pipeline
from .jobs import run_daily_news, run_inspiration, run_douyin_tech_ranking
from .scheduler import install as scheduler_install, query as scheduler_query, run_now as scheduler_run_now, uninstall as scheduler_uninstall
from .trusted_news import run_trusted_account_news
from .trusted_ai_brief import run_ai_brief


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _crawler_commands(config: dict[str, Any], mode: str) -> list[list[str]]:
    crawler = config["media_crawler"]
    python = str(resolve_path(crawler["python"]))
    main = str(resolve_path(crawler["root"]) / "main.py")
    raw_output = resolve_path(crawler["raw_output"])

    def common(destination: Path) -> list[str]:
        return [
            python,
            main,
            "--platform", "dy",
            "--type", mode,
            "--lt", "qrcode",
            "--save_data_option", "jsonl",
            "--save_data_path", str(destination),
            "--crawler_max_notes_count", str(int(crawler.get("max_notes_per_source") or 30)),
            "--get_comment", "true" if crawler.get("comments_enabled") else "false",
            "--get_sub_comment", "false",
            "--max_concurrency_num", "1",
            "--headless", "false",
        ]

    if mode == "creator":
        creators = [
            item
            for item in config.get("benchmark_accounts") or []
            if isinstance(item, dict) and item.get("enabled", True)
        ]
        commands: list[list[str]] = []
        for item in creators:
            account_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(item.get("id") or "unknown"))
            creator = str(item.get("url") or item.get("id") or "").strip()
            if creator:
                commands.append(common(raw_output / "creator" / account_id) + ["--creator_id", creator])
        return commands
    commands: list[list[str]] = []
    for category, keywords in (config.get("categories") or {}).items():
        usable = [str(value).strip() for value in keywords if str(value).strip()]
        if usable:
            safe_category = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(category))
            commands.append(common(raw_output / "search" / safe_category) + ["--keywords", ",".join(usable)])
    return commands


def _run_crawl_command(config: dict[str, Any], command: list[str]) -> int:
    timeout_seconds = max(10, int(config["media_crawler"].get("collection_timeout_seconds") or 120))
    try:
        completed = subprocess.run(
            command,
            cwd=resolve_path(config["media_crawler"]["root"]),
            check=False,
            timeout=timeout_seconds,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return int(completed.returncode)
    except subprocess.TimeoutExpired:
        return 124


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="douyin-intelligence", description="抖音科技内容情报供应层")
    parser.add_argument("--config", default="config/content_intelligence.json", help="项目配置 JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="离线检查运行环境，不读取 Cookie、不访问抖音")

    normalize = sub.add_parser("normalize", help="将 MediaCrawler JSON/JSONL 标准化")
    normalize.add_argument("--input", action="append", required=True, help="输入文件，可重复")
    normalize.add_argument("--source", choices=("douyin_creator", "douyin_search", "douyin_hotboard"))
    normalize.add_argument("--output", required=True)

    run = sub.add_parser("run", help="标准化、筛选、评分、去重并输出 OpenMontage 候选包")
    run.add_argument("--input", action="append", required=True, help="输入文件，可重复")
    run.add_argument("--source", choices=("douyin_creator", "douyin_search", "douyin_hotboard"))
    run.add_argument("--target-date", required=True, help="北京时间目标自然日 YYYY-MM-DD")
    run.add_argument("--output-dir")

    export = sub.add_parser("export-openmontage", help="从已有标准化文件生成 OpenMontage 兼容快照")
    export.add_argument("--input", action="append", required=True)
    export.add_argument("--target-date", required=True)
    export.add_argument("--output-dir")

    plan = sub.add_parser("crawl-plan", help="生成 MediaCrawler 安全采集命令，不启动浏览器")
    plan.add_argument("--mode", choices=("creator", "search", "all"), default="all")

    execute = sub.add_parser("crawl", help="显式启动 MediaCrawler；可能要求扫码或人工验证")
    execute.add_argument("--mode", choices=("creator", "search"), required=True)
    execute.add_argument("--allow-browser", action="store_true", help="确认允许打开/连接浏览器")

    browser = sub.add_parser("browser-start", help="启动或复用项目专用 Chrome 登录会话")
    browser.add_argument("--allow-browser", action="store_true")
    browser_close = sub.add_parser("browser-close", help="在无运行租约时关闭项目专用 Chrome")
    browser_close.add_argument("--allow-browser", action="store_true")

    collect = sub.add_parser("collect-creators", help="按账号隔离目录批量采集并校验身份")
    collect.add_argument("--allow-browser", action="store_true")
    collect.add_argument("--run-id")

    materials = sub.add_parser("build-materials", help="从一次采集生成高价值视频与 Markdown 素材")
    materials.add_argument("--run-dir", required=True)
    materials.add_argument("--output-dir")
    materials.add_argument("--skip-transcription", action="store_true")
    materials.add_argument("--skip-analysis", action="store_true")
    materials.add_argument("--keep-video", action="store_true")

    all_in_one = sub.add_parser("collect-materials", help="采集三个账号并生成高价值 Markdown 素材")
    all_in_one.add_argument("--allow-browser", action="store_true")
    all_in_one.add_argument("--run-id")
    all_in_one.add_argument("--output-dir")
    all_in_one.add_argument("--skip-transcription", action="store_true")
    all_in_one.add_argument("--skip-analysis", action="store_true")
    all_in_one.add_argument("--keep-video", action="store_true")

    sub.add_parser("llm-doctor", help="检查中转站配置和模型列表，不显示 API Key")
    sub.add_parser("cleanup-temp", help="按 TTL 和磁盘配额清理项目临时媒体")
    daily = sub.add_parser("daily-news", help="生成上一自然日的已核验科技新闻报告")
    daily.add_argument("--target-date", help="覆盖默认的昨日日期 YYYY-MM-DD")
    daily.add_argument("--news-input", action="append", help="离线 RSS/Atom/JSON 新闻源夹具，可重复")
    daily.add_argument("--douyin-input", action="append", help="已有抖音搜索 JSON/JSONL，可重复")
    daily.add_argument("--live-douyin", action="store_true", help="运行受控的近一天抖音搜索")
    daily.add_argument("--scheduled", action="store_true", help="标记为计划任务调用")
    ranking = sub.add_parser("douyin-tech-ranking", help="按抖音公开互动生成并核验科技热榜")
    ranking.add_argument("--target-date", help="覆盖默认的昨日日期 YYYY-MM-DD")
    ranking.add_argument("--douyin-input", action="append", help="已有安全抖音搜索 JSON/JSONL，可重复")
    ranking.add_argument("--news-input", action="append", help="离线官方新闻源夹具，可重复")
    ranking.add_argument("--live-douyin", action="store_true", help="受限采集最多 10 条抖音元数据，不下载媒体")
    trusted = sub.add_parser("trusted-account-news", help="生成固定可信账号近 48 小时科技快讯排行")
    trusted.add_argument("--account-id", help="配置中的可信账号 ID；默认首个启用账号")
    trusted.add_argument("--max-items", type=int, default=10, help="最多处理作品数，硬上限 10")
    trusted.add_argument("--input", action="append", help="已有可信账号 JSON/JSONL；提供时不启动浏览器")
    trusted.add_argument("--live", action="store_true", help="解析账号并运行一次受限真实采集")
    trusted.add_argument("--no-media", action="store_true", help="仅使用平台字幕或发布文案，不临时获取媒体")
    trusted.add_argument("--skip-ai-brief", action="store_true", help="采集完成后不自动运行 AI 易读整理")
    ai_brief = sub.add_parser("trusted-ai-brief", help="只整理已有可信账号 ranking.json，不重新采集")
    ai_brief.add_argument("--ranking", required=True, help="可信账号 ranking.json 路径")
    account_pool = sub.add_parser("account-pool", help="管理候选账号池并执行14日元数据体检")
    account_pool.add_argument("action", choices=("list", "add", "set-status", "evaluate"))
    account_pool.add_argument("--name", help="候选账号显示名称")
    account_pool.add_argument("--url", help="规范抖音用户主页")
    account_pool.add_argument("--note", default="", help="有界用户备注")
    account_pool.add_argument("--account-id", action="append", help="稳定账号ID，可重复")
    account_pool.add_argument("--status", choices=("candidate", "trusted", "rejected", "paused"))
    account_pool.add_argument("--all-enabled", action="store_true", help="体检全部启用候选")
    account_pool.add_argument("--target-date", help="北京时间目标日 YYYY-MM-DD")
    account_pool.add_argument("--input", action="append", help="离线输入 ACCOUNT_ID=JSON/JSONL")
    account_pool.add_argument("--live", action="store_true", help="使用项目专用浏览器进行一次受限元数据体检")
    material_probe = sub.add_parser("material-probe", help="为一条官方已确认新闻生成受限静态素材包")
    material_probe.add_argument("action", choices=("run",))
    material_probe.add_argument("--story", help="故事输入 JSON；默认使用项目配置")
    visual_anchor = sub.add_parser("visual-anchor", help="为最多三条已确认新闻生成具体且可追溯的视觉锚点")
    visual_anchor.add_argument("action", choices=("run", "rss-health", "domestic-smoke", "domestic-summary"))
    visual_anchor.add_argument("--stories", help="新闻批次 JSON；默认使用项目配置")
    visual_anchor.add_argument("--douyin-fallback", action="store_true", help="仅在来源图片不足时尝试一次受限抖音视觉线索降级")
    daily_pack = sub.add_parser("daily-material-pack", help="生成或校验每日科技新闻素材供应包")
    daily_pack.add_argument("action", choices=("build", "validate"))
    daily_pack.add_argument("--input", help="已选择新闻selection JSON；默认使用项目配置")
    daily_pack.add_argument("--output-root", help="项目内输出根目录；默认使用项目配置")
    daily_pack.add_argument("--pack", help="validate操作需要的daily-material-pack.json")
    daily_pack.add_argument("--quick", action="store_true", help="校验联合指纹并优先使用零联网温缓存")
    exchange = sub.add_parser("daily-material-exchange", help="构建昨日抖音科技热点候选池或只读检查交换区")
    exchange.add_argument("action", choices=("run", "simulate", "inspect", "promote-existing", "account-matrix-smoke"))
    exchange.add_argument("--business-date", help="北京时间业务日期 YYYY-MM-DD；run 默认昨天")
    exchange.add_argument("--input", help="冻结候选 JSON；仅 simulate 可用")
    exchange.add_argument("--run-id", help="promote-existing 的同日 READY V2 包 ID")
    exchange.add_argument("--override", action="store_true", help="仅 promote-existing：明确人工覆盖同日质量门")
    inspiration = sub.add_parser("inspiration", help="生成资源受控的科技灵感报告")
    inspiration.add_argument("--max-references", type=int, help="最多参考元数据，硬上限 100")
    inspiration.add_argument("--douyin-input", action="append", help="已有抖音搜索 JSON/JSONL，可重复")
    inspiration.add_argument("--live-douyin", action="store_true", help="运行受控抖音搜索")
    inspiration.add_argument("--skip-media", action="store_true", help="只分析元数据，不下载媒体")
    scheduler = sub.add_parser("scheduler", help="管理每日 02:00 Windows 计划任务")
    scheduler.add_argument("action", choices=("install", "query", "run", "uninstall"))
    replication = sub.add_parser("material-replication", help="主题驱动的抖音素材复刻采集与交付")
    replication.add_argument("action", choices=("run", "inspect", "doctor"))
    replication.add_argument("--theme", help="主题，例如 苹果折叠屏手机")
    replication.add_argument("--business-date", help="北京时间业务日期 YYYY-MM-DD")
    replication.add_argument("--pool-size", type=int, help="候选池目标规模，硬上限来自配置")
    replication.add_argument("--dry-run", action="store_true", help="只产出候选池与打分，不下载不切片")
    replication.add_argument("--download-only", action="store_true", help="只采集与下载原片，跳过人脸/画面/口播/切片")
    replication.add_argument("--overwrite", action="store_true", help="覆盖已存在的同日交付目录")
    replication.add_argument(
        "--exclude-term",
        action="append",
        help="下载前排除词：标题包含任一排除词即剔除（可重复，追加到 prefilter.exclude_terms）",
    )
    replication.add_argument("--folder", help="inspect 需要的交付目录")
    sub.add_parser("workbench", help="打开轻量本地工作台")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "doctor":
            result = run_doctor(config)
            _print(result)
            return 0 if result["ready_for_offline_pipeline"] else 2
        if args.command == "normalize":
            records = normalize_files(args.input, config, args.source)
            destination = resolve_path(args.output)
            atomic_write_json(destination, {"version": "1.0", "items": [record.to_dict() for record in records]})
            _print({"output": str(destination.resolve()), "count": len(records)})
            return 0
        if args.command in {"run", "export-openmontage"}:
            result = run_pipeline(
                args.input,
                config=config,
                target_date=args.target_date,
                source=getattr(args, "source", None),
                output_dir=args.output_dir,
            )
            _print(result)
            return 0
        if args.command == "crawl-plan":
            modes = ("creator", "search") if args.mode == "all" else (args.mode,)
            planned = []
            for mode in modes:
                if mode == "creator":
                    preview_dir = resolve_path(config["media_crawler"]["runs_output"]) / "RUN_ID_PREVIEW"
                    planned.extend(command for _, command, _ in creator_commands(config, preview_dir))
                else:
                    planned.extend(_crawler_commands(config, mode))
            _print({
                "status": "planned",
                "commands": planned,
                "authentication": "默认二维码/持久化登录态；不在命令行传递 Cookie。",
                "warning": "执行前确认使用的是专用 Chrome 会话；验证码或登录挑战必须人工处理。",
            })
            return 0
        if args.command == "crawl":
            if not args.allow_browser:
                parser.error("crawl 必须显式提供 --allow-browser")
            commands = _crawler_commands(config, args.mode)
            browser_session = BrowserSession(config, "cli_crawl")
            browser_session.prepare()
            final_status = "failed"
            try:
                for command in commands:
                    returncode = _run_crawl_command(config, command)
                    if returncode:
                        return returncode
                final_status = "success"
                return 0
            finally:
                browser_session.finish(final_status)
        if args.command == "browser-start":
            if not args.allow_browser:
                parser.error("browser-start 必须显式提供 --allow-browser")
            _print(prepare_douyin_login(config))
            return 0
        if args.command == "browser-close":
            if not args.allow_browser:
                parser.error("browser-close 必须显式提供 --allow-browser")
            result = close_project_browser(config)
            _print(result)
            return 0 if result["status"] in {"closed", "not_running"} else 3
        if args.command == "collect-creators":
            if not args.allow_browser:
                parser.error("collect-creators 必须显式提供 --allow-browser")
            result = collect_creators(config, args.run_id)
            _print(result)
            return 0 if result["status"] in {"success", "partial"} else 3
        if args.command == "build-materials":
            result = build_materials(args.run_dir, config, args.output_dir, transcription=not args.skip_transcription, analysis_enabled=not args.skip_analysis, keep_video=args.keep_video)
            _print(result)
            return 0 if result["status"] in {"success", "partial"} else 3
        if args.command == "collect-materials":
            if not args.allow_browser:
                parser.error("collect-materials 必须显式提供 --allow-browser")
            collection = collect_creators(config, args.run_id)
            if collection["status"] == "failed":
                _print({"status": "failed", "collection": collection})
                return 3
            result = build_materials(collection["run_dir"], config, args.output_dir, transcription=not args.skip_transcription, analysis_enabled=not args.skip_analysis, keep_video=args.keep_video)
            _print({"status": result["status"], "collection": collection, "materials": result})
            return 0 if result["status"] in {"success", "partial"} else 3
        if args.command == "llm-doctor":
            analyzer = OpenAICompatibleAnalyzer(config)
            client_status = analyzer.status()
            if not client_status["enabled"]:
                _print({
                    "status": "disabled",
                    "secrets": {"inspected": False},
                    "client": client_status,
                    "models": [],
                    "reason": client_status["unavailable_reason"],
                })
                return 0
            payload = {"status": "not_ready", "secrets": llm_secret_status(), "client": client_status, "models": []}
            try:
                payload["models"] = analyzer.discover_models()
                payload["selected_model"] = analyzer.resolve_model()
                payload["status"] = "ready"
            except Exception as exc:
                payload["error"] = str(exc)[:500]
            _print(payload)
            return 0 if payload["status"] == "ready" else 2
        if args.command == "cleanup-temp":
            retention = config["materials"].get("retention") or {}
            result = cleanup_temp_root(resolve_path(retention.get("temp_root") or "data/temp/materials"), ttl_hours=float(retention.get("ttl_hours") or 24), quota_bytes=int(retention.get("quota_bytes") or 2147483648))
            _print({"status": "success", **result})
            return 0
        if args.command == "daily-news":
            result = run_daily_news(config, target_date=args.target_date, news_inputs=args.news_input, douyin_inputs=args.douyin_input, live_douyin=args.live_douyin)
            _print(result)
            return 0 if result["status"] in {"success", "partial", "empty"} else 3
        if args.command == "douyin-tech-ranking":
            result = run_douyin_tech_ranking(config, target_date=args.target_date, douyin_inputs=args.douyin_input, news_inputs=args.news_input, live_douyin=args.live_douyin)
            _print(result)
            return 0 if result["status"] in {"success", "partial", "empty"} else 3
        if args.command == "trusted-account-news":
            if args.live and args.input:
                parser.error("trusted-account-news 不能同时使用 --live 与 --input")
            result = run_trusted_account_news(
                config, account_id=args.account_id, input_files=args.input, live=args.live,
                maximum=args.max_items, allow_media=not args.no_media, config_path=args.config,
                auto_ai_brief=not args.skip_ai_brief,
            )
            _print(result)
            return 0 if result["status"] in {"success", "partial", "empty", "needs_login"} else 3
        if args.command == "trusted-ai-brief":
            result = run_ai_brief(config, args.ranking)
            _print({
                "status": result["status"], "processed": result["counts"]["input_items"],
                "network_attempt_count": result["analysis_metadata"].get("network_attempt_count", result["analysis_metadata"]["request_count"]),
                "request_count": result["analysis_metadata"]["request_count"],
                "cache_hit": result["analysis_metadata"]["cache_hit"], "artifacts": result["artifacts"],
            })
            return 0
        if args.command == "account-pool":
            store = AccountPoolStore(config)
            if args.action == "list":
                _print({"status": "success", "accounts": store.list_accounts()})
                return 0
            if args.action == "add":
                if not args.name or not args.url:
                    parser.error("account-pool add 必须提供 --name 与 --url")
                _print({"status": "success", "account": store.add_candidate(args.name, args.url, note=args.note)})
                return 0
            if args.action == "set-status":
                if not args.account_id or len(args.account_id) != 1 or not args.status:
                    parser.error("account-pool set-status 必须提供一个 --account-id 与 --status")
                _print({"status": "success", "account": store.set_status(args.account_id[0], args.status)})
                return 0
            if args.live and args.input:
                parser.error("account-pool evaluate 不能同时使用 --live 与 --input")
            input_files: dict[str, list[str]] = {}
            for value in args.input or []:
                if "=" not in value:
                    parser.error("--input 必须使用 ACCOUNT_ID=PATH")
                account_id, path = value.split("=", 1)
                input_files.setdefault(account_id.strip(), []).append(path.strip())
            result = run_account_evaluation(
                config,
                account_ids=args.account_id,
                target_date=args.target_date,
                live=args.live,
                input_files=input_files,
            )
            _print({
                "status": result["status"], "counts": result["counts"], "budgets": result["budgets"],
                "artifacts": result["artifacts"], "browser": result["browser"],
            })
            return 0 if result["status"] in {"success", "partial", "empty", "needs_login"} else 3
        if args.command == "material-probe":
            result = run_material_probe(config, args.story)
            _print(result)
            return 0 if result["status"] in {"success", "partial"} else 3
        if args.command == "visual-anchor":
            if args.action == "rss-health":
                _print(check_visual_anchor_rss_health(config))
                return 0
            if args.action == "domestic-smoke":
                result = run_domestic_visual_anchor_smoke(config, douyin_fallback=args.douyin_fallback)
                _print(result)
                return 0 if result["status"] in {"success", "partial", "needs_login"} else 3
            if args.action == "domestic-summary":
                result = summarize_domestic_visual_anchor_outputs(config)
                _print(result)
                return 0 if result["status"] in {"success", "partial", "needs_login"} else 3
            result = run_visual_anchor_batch(config, args.stories, douyin_fallback=args.douyin_fallback)
            _print(result)
            return 0 if result["status"] in {"success", "partial", "needs_login"} else 3
        if args.command == "daily-material-pack":
            if args.action == "validate":
                if not args.pack:
                    parser.error("daily-material-pack validate必须提供--pack")
                result = validate_daily_material_pack(args.pack)
                _print(result)
                return 0
            result = build_daily_material_pack(config, args.input, args.output_root, quick=bool(args.quick))
            _print(result)
            return 0 if result["status"] in {"success", "partial"} else 3
        if args.command == "daily-material-exchange":
            if args.action == "inspect":
                if args.input:
                    parser.error("daily-material-exchange inspect 不接受 --input")
                if not args.business_date:
                    parser.error("daily-material-exchange inspect 需要 --business-date")
                result = inspect_daily_material_exchange(config, business_date=args.business_date)
                _print(result)
                return 0
            if args.action == "account-matrix-smoke":
                if args.input or args.run_id or args.override:
                    parser.error("daily-material-exchange account-matrix-smoke 不接受 --input、--run-id 或 --override")
                result = run_account_matrix_topic_radar_smoke(config)
                _print(result)
                return 0 if result["status"] in {"success", "partial"} else 3
            if args.action == "promote-existing":
                if args.input:
                    parser.error("daily-material-exchange promote-existing 不接受 --input")
                if not args.business_date or not args.run_id:
                    parser.error("daily-material-exchange promote-existing 需要 --business-date 和 --run-id")
                result = promote_existing_v2_pack(config, business_date=args.business_date, run_id=args.run_id, override=bool(args.override))
                _print(result)
                return 0 if result["promotion_status"] == "promoted" else 3
            if args.action == "run":
                if args.input:
                    parser.error("daily-material-exchange run 不接受冻结输入；仅 simulate 可用")
                result = run_daily_hot_candidate_pool(config, business_date=args.business_date)
                _print(result)
                return 0 if result["status"] in {"success", "partial", "empty"} else 3
            if not args.business_date:
                parser.error("daily-material-exchange simulate 需要 --business-date")
            result = simulate_daily_material_exchange(config, business_date=args.business_date, input_path=args.input)
            _print(result)
            return 0 if result["status"] in {"success", "partial"} else 3
        if args.command == "inspiration":
            result = run_inspiration(config, max_references=args.max_references, douyin_inputs=args.douyin_input, live_douyin=args.live_douyin, media=not args.skip_media)
            _print(result)
            return 0 if result["status"] in {"success", "partial", "empty"} else 3
        if args.command == "scheduler":
            actions = {"install": scheduler_install, "query": scheduler_query, "run": scheduler_run_now, "uninstall": scheduler_uninstall}
            result = actions[args.action](config)
            _print(result)
            return 0 if result["ok"] else 2
        if args.command == "material-replication":
            from .replication_pipeline import inspect_material_replication, replication_doctor, run_material_replication
            if args.action == "doctor":
                _print(replication_doctor(config))
                return 0
            if args.action == "inspect":
                if not args.folder:
                    parser.error("material-replication inspect 需要 --folder")
                result = inspect_material_replication(config, args.folder)
                _print(result)
                return 0 if result["status"] == "pass" else 3
            if not args.theme:
                parser.error("material-replication run 需要 --theme")
            if args.download_only and args.dry_run:
                parser.error("--download-only 与 --dry-run 不能同时使用")
            result = run_material_replication(
                config, args.theme, business_date=args.business_date, pool_size=args.pool_size,
                dry_run=bool(args.dry_run), download_only=bool(getattr(args, "download_only", False)),
                overwrite=bool(args.overwrite), exclude_terms=getattr(args, "exclude_term", None),
            )
            _print(result)
            return 0 if result["status"] in {"success", "partial"} else 3
        if args.command == "workbench":
            from .workbench_launcher import launch_workbench
            return launch_workbench(config, args.config)
    except (ConfigurationError, FileNotFoundError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
