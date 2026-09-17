"""工作台「单期研究包」面板的纯函数支撑。

本模块只做这几件可测试的事：

* 只读定位研究包根目录下最近一期的 ``current.json`` 并渲染中文状态文本；
* 只读消费端（Haike）已经落盘的收编账本与快照，报出这个包**有没有被接收**；
* 拼装 ``episode-research-pack build`` 的命令行。

它不发布、不修改任何研究包，也**不 import 消费端代码、不跑消费端 CLI**：消费端只以
``.backlot/research_pack_intake.json`` 与 ``.backlot/research-pack-snapshots/`` 的落盘物
出现，路径由 ``episode_research_pack.consumer_root`` 显式配置，没配就明说没配。
``current.json`` 是唯一提交点：文件缺失、损坏或非对象时一律降级为中文提示，绝不抛出异常。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .config import resolve_path
from .episode_research_pack import default_episode_id, research_pack_settings

EPISODE_SUFFIX = "_研究包"
CURRENT_NAME = "current.json"
PACKS_DIRNAME = "packs"
EVIDENCE_NAME = "每期研究证据包.md"
DELIVERY_MANIFEST_NAME = "清单.json"
DEFAULT_OUTPUT_ROOT = "output/每期研究包"

EMPTY_STATUS_TEXT = "尚未发布研究包"
BROKEN_STATUS_TEXT = "研究包指针损坏：current.json 无法解析，请重新生成该研究包。"
#: 本次目标期次没有产出时使用；必须与「最近一期」的状态文本可区分。
MISSING_TARGET_TEXT = "本次构建未产出研究包（目标期次：{episode_id}）；面板显示的可能是别期结果。最近一期：{latest_id}"
PANEL_NOTE = "Haike 只读 current.json，本面板不回写交付清单、不修改研究包。"
PANEL_ANNOTATE_NOTE = "Haike 只读 current.json，当前配置 annotate_delivery_manifest=true：本面板会回写交付清单，但不修改研究包。"
ANNOTATE_NOTE = "当前配置 annotate_delivery_manifest=true：本次构建会回写交付清单。"
ANNOTATE_NOTICE = f"提示：{ANNOTATE_NOTE}"

LOG_DIR = (".tmp", "workbench")
LOG_NAME = "research-pack-build.log"
FAILURE_HEADLINE = "构建失败（exit={code}）："

#: 消费端（Haike）仓库根目录的配置键。未配置时面板明说未配置，绝不猜路径。
CONSUMER_ROOT_KEY = "consumer_root"
#: 消费端落盘物，口径与 ``backlot/research_pack_intake.py`` 的模块默认值一致。
CONSUMER_LEDGER_PARTS = (".backlot", "research_pack_intake.json")
CONSUMER_SNAPSHOT_PARTS = (".backlot", "research-pack-snapshots")
CONSUMER_UNSET_TEXT = "消费端接收结果：未配置消费端仓库根目录（episode_research_pack.consumer_root），无法只读接收记录。"
CONSUMER_MISSING_TEXT = "消费端接收结果：未接收（消费端账本与快照都没有 {key} 的记录）"
CONSUMER_BROKEN_TEXT = "消费端接收结果：未接收（消费端账本无法解析，按未接收处理）：{path}"
CONSUMER_SNAPSHOT_ONLY_TEXT = "消费端接收结果：已接收（账本里没有同摘要记录，但消费端已有收编快照）：{path}"
#: 只报「接收 / 收编」事实；编辑门结论不落盘，必须说清楚，不能让操作者误以为本面板能报门的结果。
CONSUMER_GATE_NOTE = "说明：以上是消费端「接收/收编」事实；编辑门（exit 3）结论只在消费端 CLI 输出里，不落盘。"
#: 消费端账本 ``state`` 与 ``events[].kind`` 的取值，文案口径来自
#: ``backlot/research_pack_intake.py`` 的 ``STATES`` / ``classify_revision``。
CONSUMER_STATE_LABELS = {
    "admitted": "已接收",
    "noop": "已接收（消费端早有同摘要内容，未重复入库）",
    "rejected": "消费端拒绝",
    "failed": "接收失败",
    "invalid_contract": "接收失败（合同无效）",
    "revision_collision": "接收失败（同版本不同内容）",
    "revision_gap": "未接收（缺中间版本，消费端按版本顺序收编）",
    "stale_revision": "未接收（版本低于消费端已入账的最高版本）",
    "stale": "未接收（已被更新版本取代）",
    "discovered": "已发现未校验",
    "validated": "已校验未入库",
}
#: 只有这些状态才可能继续走到编辑门，需要附上「门结论不在这里」的说明。
CONSUMER_GATE_RELEVANT_STATES = ("admitted", "noop", "validated")


def _project_root(config: dict[str, Any]) -> Path:
    root = config.get("_project_root")
    return Path(str(root)) if root else resolve_path(".")


def _research_pack_block(config: dict[str, Any]) -> dict[str, Any]:
    """读取 ``episode_research_pack`` 配置块；兼容放在 material_replication 内外的两种写法。"""
    jobs = config.get("jobs") if isinstance(config.get("jobs"), dict) else {}
    material = jobs.get("material_replication") if isinstance(jobs.get("material_replication"), dict) else {}
    block = material.get("episode_research_pack")
    if not isinstance(block, dict):
        block = jobs.get("episode_research_pack") if isinstance(jobs.get("episode_research_pack"), dict) else {}
    return block


def research_pack_output_root(config: dict[str, Any]) -> str:
    """读取研究包输出根目录；兼容配置块放在 material_replication 内外的两种写法。"""
    return str(_research_pack_block(config).get("output_root") or DEFAULT_OUTPUT_ROOT)


def research_pack_root(config: dict[str, Any]) -> Path:
    value = Path(research_pack_output_root(config))
    return value if value.is_absolute() else _project_root(config) / value


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def latest_research_pack(config: dict[str, Any], *, episode_id: str | None = None) -> dict[str, Any] | None:
    """定位最近一期研究包。

    返回 ``{"episode_root", "episode_dir", "current_path", "pointer"}``；
    没有任何研究包时返回 ``None``；找到目录但 ``current.json`` 损坏时
    ``pointer`` 为 ``None``。目录名以 ``YYYY-MM-DD_研究包`` 开头，故按名称
    倒序即为时间倒序，结果稳定不依赖文件时间戳。

    给了 ``episode_id`` 时只在该期次目录下查找，避免把**别的期次**（或同一
    期的旧版本）当成本次构建的结果；默认 ``None`` 保持「最近一期」的旧行为。
    """
    root = research_pack_root(config)
    wanted = str(episode_id or "").strip()
    try:
        episode_dirs = sorted(
            (path for path in root.iterdir() if path.is_dir() and path.name.endswith(EPISODE_SUFFIX)),
            key=lambda path: path.name,
            reverse=True,
        )
    except OSError:
        return None
    broken: dict[str, Any] | None = None
    for episode_dir in episode_dirs:
        try:
            children = sorted((path for path in episode_dir.iterdir() if path.is_dir()), key=lambda path: path.name, reverse=True)
        except OSError:
            continue
        for child in children:
            if wanted and child.name != wanted:
                continue
            current_path = child / CURRENT_NAME
            if not current_path.is_file():
                continue
            pointer = _read_json_object(current_path)
            found = {
                "episode_root": child,
                "episode_dir": episode_dir,
                "current_path": current_path,
                "pointer": pointer,
            }
            if pointer is not None:
                return found
            if broken is None:
                broken = found
    return broken


def research_pack_annotates_delivery(config: dict[str, Any]) -> bool:
    """本次构建是否会回写旧交付清单。

    与 CLI 的判定口径一致：``annotate_delivery_manifest`` 为真时，
    ``episode-research-pack build`` 会把 ``research_pack_ref`` 写回交付清单，
    面板文字必须如实说明，不能继续宣称「不回写」。
    """
    try:
        return bool(research_pack_settings(config)["annotate_delivery_manifest"])
    except (AttributeError, KeyError, TypeError):
        return False


def research_pack_panel_text(config: dict[str, Any]) -> str:
    """面板固定说明文字，随 ``annotate_delivery_manifest`` 配置变化。"""
    prefix = "发布不可变研究包到 output/每期研究包/<日期>_研究包/<episode_id>/；"
    return prefix + (PANEL_ANNOTATE_NOTE if research_pack_annotates_delivery(config) else PANEL_NOTE)


def research_pack_launch_text(config: dict[str, Any], log_path: str | Path) -> str:
    """启动提示文字，随 ``annotate_delivery_manifest`` 配置变化。"""
    action = (
        "已启动研究包发布；只读取交付目录、不修改研究包，但"
        f"{ANNOTATE_NOTE}"
    ) if research_pack_annotates_delivery(config) else (
        "已启动研究包发布；只读取交付目录，不修改旧交付，也不回写清单。"
    )
    return f"{action}\n日志：{log_path}"


def delivery_episode_id(delivery_folder: str | Path) -> str | None:
    """从交付清单推导本次构建的 ``episode_id``（口径同 ``publish_from_delivery``）。

    ``theme`` 取清单 ``theme``、``business_date`` 取清单 ``business_date``，
    都没有时 CLI 与面板都会退化成 ``unknown-date-未命名主题``。清单读不到时
    返回 ``None``，此时调用方不做过滤。
    """
    manifest = _read_json_object(Path(delivery_folder) / DELIVERY_MANIFEST_NAME)
    if manifest is None:
        return None
    return default_episode_id(str(manifest.get("business_date") or ""), str(manifest.get("theme") or ""))


def research_pack_status_text(config: dict[str, Any], *, episode_id: str | None = None) -> str:
    """渲染研究包中文状态文本；任何异常路径都降级为提示文字。

    ``episode_id`` 非空时只报该期次的状态：找不到时明确写「本次构建未产出
    研究包」，并只把「最近一期」的期次号作为参考列出，绝不把别期的版本、
    包 ID 或摘要当作本次结果展示。
    """
    wanted = str(episode_id or "").strip()
    try:
        found = latest_research_pack(config, episode_id=wanted or None)
        annotate = research_pack_annotates_delivery(config)
    except (OSError, ValueError, TypeError):
        return EMPTY_STATUS_TEXT
    if found is None:
        if not wanted:
            return EMPTY_STATUS_TEXT
        other = latest_research_pack(config)
        latest_id = str(((other or {}).get("pointer") or {}).get("episode_id") or "-")
        text = MISSING_TARGET_TEXT.format(episode_id=wanted, latest_id=latest_id)
    else:
        pointer = found.get("pointer") or {}
        if not pointer:
            text = f"{BROKEN_STATUS_TEXT}\n位置：{found['current_path']}"
        else:
            digest = str(pointer.get("content_sha256") or "")
            lines = [
                f"期次：{pointer.get('episode_id') or '-'}  ·  版本：r{pointer.get('revision', '-')}",
                f"包 ID：{pointer.get('pack_id') or '-'}  ·  处置：{pointer.get('disposition') or '-'}",
                f"内容摘要：{digest[:12] or '-'}  ·  更新时间：{pointer.get('updated_at') or '-'}",
                f"位置：{found['episode_root']}",
                research_pack_consumer_text(config, pointer),
            ]
            text = "\n".join(line for line in lines if line)
    return f"{text}\n{ANNOTATE_NOTICE}" if annotate else text


def consumer_root(config: dict[str, Any]) -> Path | None:
    """消费端仓库根目录；未配置时返回 ``None``。

    生产端**不 import 消费端代码、不跑消费端 CLI**，只按这个根目录去只读消费端已经
    落盘的收编账本与快照。路径没配就明说没配，绝不猜一个同级目录。
    """
    value = str(_research_pack_block(config).get(CONSUMER_ROOT_KEY) or "").strip()
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else _project_root(config) / path


def consumer_ledger_path(config: dict[str, Any]) -> Path | None:
    root = consumer_root(config)
    return root.joinpath(*CONSUMER_LEDGER_PARTS) if root else None


def consumer_snapshot_root(config: dict[str, Any]) -> Path | None:
    root = consumer_root(config)
    return root.joinpath(*CONSUMER_SNAPSHOT_PARTS) if root else None


def consumer_snapshot_dir(config: dict[str, Any], pointer: dict[str, Any]) -> Path | None:
    """消费端收编快照目录 ``<快照根>/<episode_id>/<content_sha256>``；不存在时 ``None``。

    目录名本身就是内容摘要，所以这个判定按摘要对齐，不依赖期次名是否重名。
    """
    root = consumer_snapshot_root(config)
    if root is None:
        return None
    episode_id = str((pointer or {}).get("episode_id") or "")
    digest = str((pointer or {}).get("content_sha256") or "")
    if not episode_id or not digest:
        return None
    candidate = root / episode_id / digest
    try:
        return candidate if candidate.is_dir() else None
    except OSError:
        return None


def _ledger_records(entry: Any) -> list[dict[str, Any]]:
    if not isinstance(entry, dict):
        return []
    records: list[dict[str, Any]] = []
    current = entry.get("current")
    if isinstance(current, dict):
        records.append(current)
    history = entry.get("history")
    if isinstance(history, list):
        records.extend(item for item in history if isinstance(item, dict))
    return records


def _ledger_match(
    ledger: dict[str, Any], *, pack_id: str, content_sha256: str
) -> dict[str, Any] | None:
    """按 ``content_sha256``（其次 ``pack_id``）匹配账本，返回归一化记录。

    绝不用期次名匹配：同一期次可能已经产出 r2，拿 r1 的摘要去问就必须报「未接收」，
    不能把 r2 的记录当成本次结果。``current`` / ``history`` 是入库结论；``events``
    兜底 ``rejected`` / ``failed`` 这类**只写事件、不写 current** 的结论，否则被拒
    的包会看起来像「消费端没收到」。
    """
    episodes = ledger.get("episodes")
    if not isinstance(episodes, dict):
        return None
    entries = [entry for entry in episodes.values() if isinstance(entry, dict)]
    for entry in entries:
        records = _ledger_records(entry)
        record: dict[str, Any] | None = None
        matched_by = ""
        if content_sha256:
            record = next(
                (item for item in records if str(item.get("content_sha256") or "") == content_sha256), None
            )
            matched_by = "content_sha256" if record is not None else ""
        if record is None and pack_id:
            record = next((item for item in records if str(item.get("pack_id") or "") == pack_id), None)
            matched_by = "pack_id" if record is not None else ""
        if record is not None:
            event = _ledger_last_event(entry, str(record.get("content_sha256") or content_sha256))
            return {
                "matched_by": matched_by,
                "state": str(record.get("state") or ""),
                "disposition": str(record.get("disposition") or ""),
                "at": str(record.get("admitted_at") or record.get("checked_at") or ""),
                "snapshot_dir": str(record.get("snapshot_dir") or ""),
                "event_at": str(event.get("at") or ""),
                "event_message": str(event.get("message") or event.get("kind") or ""),
            }
    if not content_sha256:
        return None
    for entry in entries:
        event = _ledger_last_event(entry, content_sha256)
        if str(event.get("content_sha256") or "") != content_sha256:
            continue
        return {
            "matched_by": "content_sha256",
            "state": str(event.get("kind") or ""),
            "disposition": "",
            "at": str(event.get("at") or ""),
            "snapshot_dir": "",
            "event_at": str(event.get("at") or ""),
            "event_message": str(event.get("message") or event.get("kind") or ""),
        }
    return None


def _ledger_last_event(entry: Any, content_sha256: str) -> dict[str, Any]:
    events = entry.get("events") if isinstance(entry, dict) else None
    items = [item for item in events if isinstance(item, dict)] if isinstance(events, list) else []
    if content_sha256:
        for event in reversed(items):
            if str(event.get("content_sha256") or "") == content_sha256:
                return event
    return items[-1] if items else {}


def research_pack_consumer_status(config: dict[str, Any], pointer: dict[str, Any]) -> dict[str, Any]:
    """只读消费端落盘物，判断这个包有没有被接收；任何异常都降级，绝不抛出。

    判定顺序：先按 ``content_sha256``（其次 ``pack_id``）在消费端收编账本里找记录，
    账本缺失/损坏时退回「收编快照目录是否存在」。返回 ``found=False`` 时调用方必须
    明说「未接收」，不得拿别期次的记录顶上。
    """
    pointer = pointer if isinstance(pointer, dict) else {}
    pack_id = str(pointer.get("pack_id") or "")
    digest = str(pointer.get("content_sha256") or "")
    result: dict[str, Any] = {
        "configured": False, "found": False, "source": "", "matched_by": "",
        "state": "", "disposition": "", "at": "", "snapshot_dir": "",
        "event_at": "", "event_message": "", "broken_ledger": "",
    }
    try:
        ledger_path = consumer_ledger_path(config)
        snapshot_dir = consumer_snapshot_dir(config, pointer)
        if ledger_path is None:
            return result
        result["configured"] = True
        ledger = _read_json_object(ledger_path)
        if ledger is not None:
            match = _ledger_match(ledger, pack_id=pack_id, content_sha256=digest)
            if match is not None:
                result.update(found=True, source="ledger", **match)
                if not result["snapshot_dir"] and snapshot_dir is not None:
                    result["snapshot_dir"] = str(snapshot_dir)
                return result
        elif ledger_path.is_file():
            result["broken_ledger"] = str(ledger_path)
        if snapshot_dir is not None:
            result.update(
                found=True, source="snapshot", matched_by="content_sha256", snapshot_dir=str(snapshot_dir),
            )
    except (OSError, ValueError, TypeError):
        return result
    return result


def research_pack_consumer_text(config: dict[str, Any], pointer: dict[str, Any]) -> str:
    """渲染「消费端接收结果」中文文本；未配置、未接收、账本损坏都各有明确文案。"""
    status = research_pack_consumer_status(config, pointer)
    if not status["configured"]:
        return CONSUMER_UNSET_TEXT
    if not status["found"]:
        if status["broken_ledger"]:
            return CONSUMER_BROKEN_TEXT.format(path=status["broken_ledger"])
        digest = str((pointer or {}).get("content_sha256") or "")[:12]
        pack_id = str((pointer or {}).get("pack_id") or "-")
        return CONSUMER_MISSING_TEXT.format(key=f"content_sha256={digest or '-'} / 包 ID {pack_id}")
    if status["source"] == "snapshot":
        return CONSUMER_SNAPSHOT_ONLY_TEXT.format(path=status["snapshot_dir"])
    state = status["state"]
    label = CONSUMER_STATE_LABELS.get(state, f"状态 {state or '-'}")
    lines = [
        f"消费端接收结果：{label}（{state or '-'}）  ·  匹配方式：{status['matched_by']}",
        f"消费端处置：{status['disposition'] or '-'}  ·  消费端记录时间：{status['at'] or '-'}",
    ]
    event = f"{status['event_at'] or ''} {status['event_message'] or ''}".strip()
    if event:
        lines.append(f"消费端最近事件：{event}")
    if status["snapshot_dir"]:
        lines.append(f"消费端快照：{status['snapshot_dir']}")
    if state in CONSUMER_GATE_RELEVANT_STATES:
        lines.append(CONSUMER_GATE_NOTE)
    return "\n".join(lines)


def research_pack_folder_target(found: dict[str, Any]) -> Path:
    """优先打开不可变的 ``packs/<pack_id>``，缺失时退回期次根目录。"""
    pointer = found.get("pointer") or {}
    pack_id = str(pointer.get("pack_id") or "")
    if pack_id:
        pack_dir = Path(found["episode_root"]) / PACKS_DIRNAME / pack_id
        if pack_dir.is_dir():
            return pack_dir
    return Path(found["episode_root"])


def research_pack_evidence_path(found: dict[str, Any]) -> Path:
    return Path(found["episode_root"]) / EVIDENCE_NAME


def suggested_delivery_dir(config: dict[str, Any]) -> Path | None:
    """猜一个待转研究包的交付目录，仅用于文件对话框的初始位置。"""
    jobs = config.get("jobs") if isinstance(config.get("jobs"), dict) else {}
    material = jobs.get("material_replication") if isinstance(jobs.get("material_replication"), dict) else {}
    value = Path(str(material.get("output_root") or "output/复刻视频"))
    root = value if value.is_absolute() else _project_root(config) / value
    try:
        candidates = [path for path in root.iterdir() if path.is_dir() and (path / DELIVERY_MANIFEST_NAME).is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def research_pack_log_path(config: dict[str, Any]) -> Path:
    """研究包构建日志路径，每次构建前以覆盖模式重写，避免无限增长。"""
    return _project_root(config).joinpath(*LOG_DIR, LOG_NAME)


def build_failure_text(
    log_path: str | Path,
    exit_code: int,
    *,
    max_lines: int = 20,
    max_chars: int = 1500,
) -> str:
    """把「进程非零退出」变成可读中文提示：日志尾部 N 行非空内容。

    日志缺失、为空或读取失败时返回含退出码与日志路径的中文兜底，绝不抛异常。
    """
    path = Path(log_path)
    headline = FAILURE_HEADLINE.format(code=exit_code)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return f"{headline}\n未能读取构建日志：{path}"
    lines = [line.rstrip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return f"{headline}\n构建日志为空：{path}"
    tail = "\n".join(lines[-max(1, int(max_lines)):])
    return f"{headline}\n{tail[:max(1, int(max_chars))]}"


def episode_research_pack_command(
    config_path: str,
    delivery_folder: str,
    *,
    theme: str | None = None,
    business_date: str | None = None,
) -> list[str]:
    python = resolve_path(".venv/Scripts/python.exe")
    command = [
        str(python), "-m", "douyin_intelligence.cli", "--config", str(resolve_path(config_path)),
        "episode-research-pack", "build", "--delivery-folder", str(resolve_path(delivery_folder)),
    ]
    if theme:
        command += ["--theme", theme]
    if business_date:
        command += ["--business-date", business_date]
    return command


def launch_episode_research_pack(
    config_path: str,
    delivery_folder: str,
    *,
    theme: str | None = None,
    business_date: str | None = None,
    log_path: str | Path | None = None,
) -> subprocess.Popen[Any]:
    """启动研究包构建。

    给了 ``log_path`` 就把 stdout/stderr（合并）落到该文件，失败时工作台才能
    把原因显示给使用者；目录建不出或文件打不开时退回丢弃输出，不阻断启动。
    """
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    command = episode_research_pack_command(config_path, delivery_folder, theme=theme, business_date=business_date)
    handle = None
    if log_path is not None:
        target = Path(log_path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            handle = target.open("w", encoding="utf-8")
        except OSError:
            handle = None
    try:
        if handle is None:
            return subprocess.Popen(
                command, cwd=resolve_path("."), stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=flags,
            )
        return subprocess.Popen(
            command, cwd=resolve_path("."), stdout=handle,
            stderr=subprocess.STDOUT, creationflags=flags,
        )
    finally:
        if handle is not None:
            handle.close()
