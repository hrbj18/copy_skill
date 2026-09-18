"""Build or refresh the mandatory human-readable delivery report (`汇报文档.md`).

Every delivery folder must end with one report. This script owns the facts; the
agent owns the conclusions. Sections marked `auto` are regenerated on every run,
sections marked `human` keep whatever text is already there, so re-running never
destroys written analysis.

Usage:
    python scripts/build_report_doc.py --delivery <交付目录>
    python scripts/build_report_doc.py --delivery <交付目录> --check
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


REPORT_FILENAME = "汇报文档.md"
MANIFEST_FILENAME = "清单.json"
PROCESS_DIRNAME = "05-过程数据"

MIB = 1024 * 1024
# 交付口径（项目记忆 §2）：交付目录合计 70~150 MB，delivered_bytes 70~100 MiB。
DEFAULT_MIN_DELIVERED_MIB = 70.0

HUMAN_TODO = "<!-- HUMAN-TODO -->"
HEADER_SPLIT = re.compile(r"^(##\s+)([一二三四五六七八九十]+)(、.*)$", re.MULTILINE)


@dataclass(frozen=True)
class Section:
    ordinal: str
    title: str
    kind: str
    render: Callable[[dict[str, Any]], str] | None = None
    todo: str = ""


# --------------------------------------------------------------------------- #
# facts
# --------------------------------------------------------------------------- #


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _size(byte_count: int) -> str:
    if byte_count >= 1024 * 1024:
        return f"{byte_count / MIB:.1f} MiB"
    if byte_count >= 1024:
        return f"{byte_count / 1024:.0f} KB"
    return f"{byte_count} B"


def _seconds(value: Any) -> str:
    try:
        return f"{float(value):.2f} s"
    except (TypeError, ValueError):
        return "—"


def _inventory(root: Path) -> dict[str, Any]:
    subdirectories: list[dict[str, Any]] = []
    root_files: list[dict[str, Any]] = []
    total_files = 0
    total_bytes = 0
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if child.is_dir():
            files = [item for item in sorted(child.rglob("*")) if item.is_file()]
            size = sum(item.stat().st_size for item in files)
            subdirectories.append({"name": child.name, "files": len(files), "bytes": size})
            total_files += len(files)
            total_bytes += size
        elif child.is_file():
            root_files.append({"name": child.name, "bytes": child.stat().st_size})
            total_files += 1
            total_bytes += child.stat().st_size
    markdown = sorted(
        item.relative_to(root).as_posix() for item in root.rglob("*.md") if item.is_file()
    )
    return {
        "total_files": total_files,
        "total_bytes": total_bytes,
        "subdirectories": subdirectories,
        "root_files": root_files,
        "markdown": markdown,
    }


def collect_facts(
    delivery: Path,
    *,
    theme: str | None = None,
    business_date: str | None = None,
    min_delivered_mib: float = DEFAULT_MIN_DELIVERED_MIB,
) -> dict[str, Any]:
    delivery = delivery.resolve()
    if not delivery.is_dir():
        raise FileNotFoundError(f"交付目录不存在：{delivery}")

    manifest = _read_json(delivery / MANIFEST_FILENAME) or {}
    run_log = _read_json(delivery / PROCESS_DIRNAME / "run_log.json") or {}
    budget = _read_json(delivery / PROCESS_DIRNAME / "download_budget.json") or {}

    def pick(key: str, default: Any = None) -> Any:
        if key in manifest:
            return manifest[key]
        return run_log.get(key, default)

    material_replica = pick("material_replica") or {}
    script_replica = pick("script_replica") or {}
    freshness = pick("material_freshness") or {}
    validation = pick("validation") or {}
    manifest_budget = pick("download_budget") or {}
    used = budget.get("used") or manifest_budget.get("used") or {}
    delivery_folder = pick("delivery_folder") or {}

    # 口径（项目记忆 §2）：`delivered_bytes` = 入选源片字节和，唯一可精确控制的量。
    # `download_budget.used.delivered_bytes` 是**下载**量（含落选素材的流量），两者必须区分，
    # 否则会把「下载了 73 MiB、实际只交付 22 MiB」误读成达标。
    delivered_bytes = material_replica.get("delivered_bytes") or used.get("delivered_bytes") or 0
    min_bytes = int(material_replica.get("min_delivered_bytes") or min_delivered_mib * MIB)
    max_bytes = int(material_replica.get("max_delivered_bytes") or 0)

    facts: dict[str, Any] = {
        "delivery": delivery,
        "delivery_name": delivery.name,
        "theme": theme or manifest.get("theme") or run_log.get("theme") or delivery.name,
        "business_date": business_date or manifest.get("business_date") or "—",
        "generated_at": manifest.get("generated_at") or "—",
        "reported_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "status": pick("status") or "—",
        "degraded": bool(pick("degraded", False)),
        "insufficient": bool(pick("insufficient", False)),
        "candidate_pool_size": pick("candidate_pool_size") or pick("pool_size") or 0,
        "keywords": pick("keywords_used") or run_log.get("keywords") or [],
        "counters": pick("counters") or {},
        "warnings": pick("warnings") or [],
        "evidence_disclaimer": pick("evidence_disclaimer") or "",
        "material_replica": material_replica,
        "script_replica": script_replica,
        "freshness": freshness,
        "validation": validation,
        "face_backend_status": pick("face_backend_status") or "—",
        "ffmpeg_status": pick("ffmpeg_status") or "—",
        "direct_delivery": pick("direct_delivery") or {},
        "source_retention": pick("source_retention") or {},
        "search_attribution": pick("search_attribution") or run_log.get("search_attribution") or {},
        "main_materials": pick("main_materials") or [],
        "supporting_materials": pick("supporting_materials") or [],
        "delivered_bytes": int(delivered_bytes or 0),
        "min_delivered_bytes": min_bytes,
        "max_delivered_bytes": max_bytes,
        "downloaded_bytes": int(used.get("bytes") or 0),
        "downloaded_count": int(used.get("count") or 0),
        "volume_ok": bool(delivered_bytes) and int(delivered_bytes) >= min_bytes,
        "folder_bytes": int(delivery_folder.get("delivery_folder_bytes") or 0),
        "folder_max_bytes": int(delivery_folder.get("max_delivery_folder_bytes") or 0),
        "budget_limits": (budget.get("limits") or manifest_budget.get("limits") or {}),
        "has_manifest": bool(manifest),
    }
    facts["inventory"] = _inventory(delivery)
    return facts


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #


def _render_header(facts: dict[str, Any]) -> str:
    if facts["has_manifest"]:
        volume = "达标" if facts["volume_ok"] else "**未达标**"
        conclusion = (
            f"主题「{facts['theme']}」，业务日期 {facts['business_date']}；"
            f"管线状态 `{facts['status']}`，候选池 {facts['candidate_pool_size']} 条，"
            f"交付 {facts['inventory']['total_files']} 个文件 / "
            f"{_size(facts['inventory']['total_bytes'])}，"
            f"入选源片体积 {_size(facts['delivered_bytes'])}（{volume}），"
            f"告警 {len(facts['warnings'])} 条。"
        )
    else:
        conclusion = (
            f"主题「{facts['theme']}」，业务日期 {facts['business_date']}；"
            f"人工整理包，{facts['inventory']['total_files']} 个文件 / "
            f"{_size(facts['inventory']['total_bytes'])}，"
            f"{len(facts['inventory']['subdirectories'])} 个顶层子目录；无采集链账本。"
        )
    if facts["insufficient"]:
        conclusion += " **`insufficient: true`** —— 自动采集未达体积下限，需人工补源或按素材现状剪辑。"
    if facts["degraded"]:
        conclusion += " **`degraded: true`** —— 存在降级项，逐条见第五节。"
    wasted = max(facts["downloaded_bytes"] - facts["delivered_bytes"], 0)
    if facts["has_manifest"] and wasted > facts["downloaded_bytes"] * 0.2:
        conclusion += (
            f" 另注意：下载消耗 {_size(facts['downloaded_bytes'])} 中，"
            f"{_size(wasted)} 花在最终落选的素材上。"
        )
    meta = [
        f"- 主题：{facts['theme']}",
        f"- 业务日期：{facts['business_date']}",
        f"- 交付目录：`{facts['delivery_name']}/`",
    ]
    if facts["has_manifest"]:
        meta.append(f"- 管线完成时间：{facts['generated_at']}")
    meta.append(f"- 汇报时间：{facts['reported_at']}")
    return (
        f"# 本期汇报 · {facts['theme']}\n\n"
        f"> **一句话结论**：{conclusion}\n>\n"
        f"> 本文件由 `scripts/build_report_doc.py` 维护：自动段每次运行刷新，人工段保留原文。\n\n"
        + "\n".join(meta)
        + "\n"
    )


def _render_summary(facts: dict[str, Any]) -> str:
    counters = facts["counters"]
    if not facts["has_manifest"]:
        volume = "—（非采集链交付，无管线账本）"
        download = "—"
    else:
        if facts["volume_ok"]:
            volume = f"{_size(facts['delivered_bytes'])} / 下限 {_size(facts['min_delivered_bytes'])}　✅ 达标"
        else:
            shortage = max(facts["min_delivered_bytes"] - facts["delivered_bytes"], 0)
            volume = (
                f"{_size(facts['delivered_bytes'])} / 下限 {_size(facts['min_delivered_bytes'])}"
                f"　⚠️ 未达标（缺口 {_size(shortage)}）"
            )
        if facts["max_delivered_bytes"]:
            volume += f"，目标上限 {_size(facts['max_delivered_bytes'])}"
        wasted = max(facts["downloaded_bytes"] - facts["delivered_bytes"], 0)
        download = (
            f"{_size(facts['downloaded_bytes'])}（{facts['downloaded_count']} 条，落选消耗 {_size(wasted)}）"
            if facts["downloaded_bytes"]
            else "—"
        )
    folder = f"{_size(facts['inventory']['total_bytes'])}（当前实测）"
    if facts["folder_max_bytes"]:
        folder += f" / 上限 {_size(facts['folder_max_bytes'])}"
    if facts["has_manifest"]:
        rows = [
            ("管线状态", f"`{facts['status']}`"),
            ("降级 / 不足", f"`degraded={facts['degraded']}` / `insufficient={facts['insufficient']}`"),
            ("候选池", f"{facts['candidate_pool_size']} 条"),
            ("入选素材", f"{counters.get('clips_exported', '—')} 条（下载 {counters.get('downloaded', '—')}）"),
            ("脚本复刻", f"`{(facts['script_replica'] or {}).get('status', '—')}`"),
            ("交付源片体积", volume),
            ("下载消耗", download),
            ("交付目录合计", f"{folder} / {facts['inventory']['total_files']} 文件"),
            ("告警", f"{len(facts['warnings'])} 条"),
            ("人脸后端 / ffmpeg", f"`{facts['face_backend_status']}` / `{facts['ffmpeg_status']}`"),
        ]
    else:
        rows = [
            ("产物性质", "人工整理包（无采集链账本 `清单.json`）"),
            ("交付目录合计", f"{folder} / {facts['inventory']['total_files']} 文件"),
            ("顶层子目录", f"{len(facts['inventory']['subdirectories'])} 个"),
            ("交付源片体积", "—（非采集链交付，无管线账本）"),
            ("告警", "—"),
        ]
    body = ["| 项 | 值 |", "|---|---|"] + [f"| {name} | {value} |" for name, value in rows]
    return "\n".join(body)


def _render_inventory(facts: dict[str, Any]) -> str:
    blocks: list[str] = []
    subdirectories = facts["inventory"]["subdirectories"]
    if subdirectories:
        blocks.append(
            "\n".join(
                ["| 子目录 | 文件数 | 体积 |", "|---|---|---|"]
                + [
                    f"| `{item['name']}/` | {item['files']} | {_size(item['bytes'])} |"
                    for item in subdirectories
                ]
            )
        )
    root_files = facts["inventory"]["root_files"]
    if root_files:
        blocks.append(
            "**根目录文件**：" + "、".join(f"`{item['name']}`" for item in root_files)
        )

    for label, key in (("主素材", "main_materials"), ("辅助素材", "supporting_materials")):
        materials = facts[key]
        if not materials:
            continue
        rows = ["| 文件 | 时长 | 人脸 | 建议用途 |", "|---|---|---|---|"]
        for item in materials:
            rows.append(
                f"| `{item.get('file', '—')}` | {_seconds(item.get('duration'))} "
                f"| `{item.get('face_class', '—')}` | {item.get('suggested_use', '—')} |"
            )
        blocks.append(f"**{label}**\n\n" + "\n".join(rows))
    return "\n\n".join(blocks) if blocks else "_目录为空。_"


def _render_diagnostics(facts: dict[str, Any]) -> str:
    blocks: list[str] = []
    counters = facts["counters"]
    if counters:
        blocks.append(
            "\n".join(
                ["| 计数项 | 值 |", "|---|---|"]
                + [f"| `{name}` | {value} |" for name, value in sorted(counters.items())]
            )
        )
    freshness = facts["freshness"]
    if freshness:
        blocks.append(
            "**时效闸门**："
            f"`max_age_days={freshness.get('max_age_days', '—')}`，"
            f"判定 {freshness.get('judged', '—')} 条，剔除 {freshness.get('rejected', '—')} 条，"
            f"最旧 {freshness.get('oldest_age_days', '—')} 天。"
        )
    validation = facts["validation"]
    if validation.get("counts"):
        counts = validation["counts"]
        blocks.append(
            "**校验**："
            f"通过 {counts.get('passed', '—')} / 拒绝 {counts.get('rejected', '—')} / "
            f"校验 {counts.get('validated', '—')}。"
        )
    retention = facts["source_retention"]
    if retention.get("note"):
        blocks.append(f"**原片保留**：{retention['note']}（`effective_keep={retention.get('effective_keep', '—')}`）")
    if facts["folder_bytes"]:
        delta = facts["inventory"]["total_bytes"] - facts["folder_bytes"]
        note = f"**体积口径**：管线交付时刻记录 {_size(facts['folder_bytes'])}"
        note += f"，本目录当前实测 {_size(facts['inventory']['total_bytes'])}"
        if abs(delta) > 0:
            note += f"（差额 {_size(abs(delta))}，来自人工增补或后续清理）"
        blocks.append(note + "。")
    attribution = facts["search_attribution"]
    if attribution:
        blocks.append(
            "**检索归因**："
            f"池子 {attribution.get('pool_size', '—')} 条 / 目标 {attribution.get('min_pool_size', '—')} 条，"
            f"关键词使用 {attribution.get('keywords_used_count', '—')} / "
            f"请求 {attribution.get('keywords_requested_count', '—')}。"
        )
    keywords = facts["keywords"]
    if keywords:
        blocks.append("**检索关键词**：" + "、".join(f"`{item}`" for item in keywords))
    warnings = facts["warnings"]
    if warnings:
        blocks.append(
            "**告警明细**\n\n" + "\n".join(f"{index}. {item}" for index, item in enumerate(warnings, 1))
        )
    else:
        blocks.append("**告警明细**：无。")
    if facts["evidence_disclaimer"]:
        blocks.append(f"> {facts['evidence_disclaimer']}")
    return "\n\n".join(blocks)


def _render_documents(facts: dict[str, Any]) -> str:
    markdown = facts["inventory"]["markdown"]
    if not markdown:
        return "_本目录没有 Markdown 文档。_"
    rows = ["| 文件 | 位置 | 性质 |", "|---|---|---|"]
    for item in markdown:
        parent = Path(item).parent.as_posix()
        location = "根目录" if parent == "." else f"`{parent}/`"
        if item == REPORT_FILENAME:
            nature = "人工结论 + 自动事实"
        else:
            nature = "管线自动"
        rows.append(f"| `{Path(item).name}` | {location} | {nature} |")
    return "\n".join(rows)


def _todo(ordinal: str, hints: list[str]) -> str:
    lines = [HUMAN_TODO, "", f"> _{ordinal} 段待人工填写。_", ""]
    lines.extend(f"- {hint}" for hint in hints)
    return "\n".join(lines)


def _render_product(facts: dict[str, Any]) -> str:
    return _todo(
        "二",
        [
            "产品全称与厂商，以及这条参考片是官方发布还是博主二次剪辑。",
            "官方公开硬参数（算力 / 感知 / 自由度 / 量产状态 / 落地场景），只要可追溯来源的。",
            "参考视频中的叙事套路（哪一句钩子、第几秒才出现产品名、收尾 CTA）。",
        ],
    )


def _render_breakdown(facts: dict[str, Any]) -> str:
    return _todo(
        "三",
        [
            "时长 / 画幅 / 帧率 / 镜头数 / 口播句数。",
            "结构分段（钩子 → 场景举证 → 参数 → 情感爆点 → 号召）与各段时长。",
            "可直接复用的版式与节奏要点。",
        ],
    )


def _render_risks(facts: dict[str, Any]) -> str:
    hints = [
        "版权与授权：素材来源是否可商用。",
        "人脸：入选素材里是否含真人出镜。",
        "体积 / 时效 / 分辨率标注等已知口径风险。",
        "人工增补的文件在重跑 `--overwrite` 时会消失。",
    ]
    body = _todo("六", hints)
    if facts["insufficient"] or facts["degraded"] or facts["warnings"]:
        body += (
            f"\n\n> 自动检测：`insufficient={facts['insufficient']}`、"
            f"`degraded={facts['degraded']}`、{len(facts['warnings'])} 条告警，逐条见第五节，"
            "请在此处给出处置结论。"
        )
    return body


SECTIONS: tuple[Section, ...] = (
    Section("一", "结论速览", "auto", _render_summary),
    Section("二", "产品是什么（事实层）", "human", _render_product),
    Section("三", "参考视频拆解要点", "human", _render_breakdown),
    Section("四", "交付物清单", "auto", _render_inventory),
    Section("五", "管线结果与诊断", "auto", _render_diagnostics),
    Section("六", "风险与待办", "human", _render_risks),
    Section("七", "文档索引", "auto", _render_documents),
)


# --------------------------------------------------------------------------- #
# render + merge
# --------------------------------------------------------------------------- #


def _parse_sections(text: str) -> dict[str, str]:
    matches = list(HEADER_SPLIT.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(2)] = text[match.end() : end].strip("\n")
    return sections


def render_report(facts: dict[str, Any], existing: str | None = None) -> str:
    preserved = _parse_sections(existing) if existing else {}
    parts = [_render_header(facts)]
    for section in SECTIONS:
        if section.kind == "auto":
            body = section.render(facts)  # type: ignore[misc]
        else:
            body = preserved.get(section.ordinal, "").strip()
            if not body:
                body = section.render(facts)  # type: ignore[misc]
        parts.append(f"## {section.ordinal}、{section.title}\n\n{body}\n")
    return "\n".join(parts)


def pending_sections(text: str) -> list[str]:
    """Return titles of human sections that still carry the TODO marker."""
    pending: list[str] = []
    for match in re.finditer(r"^##\s+([一二三四五六七八九十]+、[^\n]*)$", text, re.MULTILINE):
        start = match.end()
        next_match = re.compile(r"^##\s+[一二三四五六七八九十]+、", re.MULTILINE).search(text, start)
        body = text[start : next_match.start() if next_match else len(text)]
        if HUMAN_TODO in body:
            pending.append(match.group(1))
    return pending


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #


def build(
    delivery: Path,
    *,
    out: Path | None = None,
    theme: str | None = None,
    business_date: str | None = None,
    force: bool = False,
    min_delivered_mib: float = DEFAULT_MIN_DELIVERED_MIB,
) -> tuple[Path, dict[str, Any]]:
    facts = collect_facts(
        delivery, theme=theme, business_date=business_date, min_delivered_mib=min_delivered_mib
    )
    target = (out or (delivery.resolve() / REPORT_FILENAME)).resolve()
    existing = None if force else (target.read_text(encoding="utf-8") if target.is_file() else None)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_report(facts, existing), encoding="utf-8", newline="\n")
    return target, facts


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 / 刷新交付目录的汇报文档")
    parser.add_argument("--delivery", required=True, type=Path, help="交付目录（如 output/复刻视频/9.17X复刻视频）")
    parser.add_argument("--out", type=Path, help=f"输出路径，默认 <交付目录>/{REPORT_FILENAME}")
    parser.add_argument("--theme", help="覆盖主题")
    parser.add_argument("--business-date", help="覆盖业务日期")
    parser.add_argument("--force", action="store_true", help="丢弃已保留的人工段，整份重写")
    parser.add_argument("--check", action="store_true", help="只检查人工段是否填完，不写文件")
    parser.add_argument("--json", action="store_true", dest="as_json", help="打印采集到的事实")
    parser.add_argument("--min-delivered-mib", type=float, default=DEFAULT_MIN_DELIVERED_MIB)
    args = parser.parse_args()

    try:
        if args.check:
            target = (args.out or (args.delivery.resolve() / REPORT_FILENAME)).resolve()
            if not target.is_file():
                print(f"[FAIL] 缺少汇报文档：{target}")
                return 2
            pending = pending_sections(target.read_text(encoding="utf-8"))
            if pending:
                print("[FAIL] 人工段未填完：" + "、".join(pending))
                return 2
            print(f"[OK] 汇报文档完整：{target}")
            return 0

        target, facts = build(
            args.delivery,
            out=args.out,
            theme=args.theme,
            business_date=args.business_date,
            force=args.force,
            min_delivered_mib=args.min_delivered_mib,
        )
    except (OSError, ValueError) as error:
        print(f"[FAIL] {error}")
        return 1

    if args.as_json:
        printable = {key: value for key, value in facts.items() if key != "delivery"}
        print(json.dumps(printable, ensure_ascii=False, indent=2, default=str))
        return 0
    pending = pending_sections(target.read_text(encoding="utf-8"))
    print(f"[OK] {target}")
    if pending:
        print("     待人工填写：" + "、".join(pending))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
