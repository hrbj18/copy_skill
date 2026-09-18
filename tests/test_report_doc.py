"""交付汇报文档生成器：自动段刷新、人工段保留、检查模式。"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "build_report_doc.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("build_report_doc", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_report_doc"] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


def _mib(value: float) -> int:
    return int(value * 1024 * 1024)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _delivery(tmp_path: Path, *, insufficient: bool = True) -> Path:
    delivery = tmp_path / "9.17测试主题复刻视频"
    _write_json(
        delivery / "清单.json",
        {
            "theme": "测试主题",
            "business_date": "2026-09-17",
            "generated_at": "2026-09-17T13:58:17+08:00",
            "status": "done",
            "degraded": False,
            "insufficient": insufficient,
            "candidate_pool_size": 10,
            "keywords_used": ["测试主题"],
            "counters": {"candidates": 10, "downloaded": 6, "clips_exported": 4},
            "warnings": ["示例告警"],
            "material_replica": {
                "status": "insufficient",
                "delivered_bytes": _mib(21.7),
                "min_delivered_bytes": _mib(70),
                "max_delivered_bytes": _mib(100),
            },
            "script_replica": {"status": "found"},
            "download_budget": {"used": {"count": 6, "bytes": _mib(72.9), "delivered_bytes": _mib(72.9)}},
            "material_freshness": {"max_age_days": 90, "judged": 10, "rejected": 2, "oldest_age_days": 157.0},
            "validation": {"counts": {"validated": 6, "passed": 6, "rejected": 0}},
            "source_retention": {"note": "直投模式", "effective_keep": False},
            "search_attribution": {"pool_size": 10, "min_pool_size": 40},
            "face_backend_status": "ok",
            "ffmpeg_status": "ok",
            "delivery_folder": {
                "delivery_folder_bytes": _mib(21.8),
                "max_delivery_folder_bytes": 150 * 1024 * 1024,
            },
            "main_materials": [
                {"file": "02-主素材/a.mp4", "duration": 173.15, "face_class": "face_free", "suggested_use": "hook"}
            ],
            "supporting_materials": [],
        },
    )
    (delivery / "02-主素材").mkdir(parents=True, exist_ok=True)
    (delivery / "02-主素材" / "a.mp4").write_bytes(b"0" * 2048)
    (delivery / "00-交付说明.md").write_text("# 交付说明\n", encoding="utf-8")
    return delivery


def test_volume_uses_delivered_not_downloaded(tmp_path: Path) -> None:
    facts = MODULE.collect_facts(_delivery(tmp_path))
    assert facts["delivered_bytes"] == _mib(21.7)
    assert facts["downloaded_bytes"] == _mib(72.9)
    assert facts["min_delivered_bytes"] == _mib(70)
    assert facts["volume_ok"] is False
    text = MODULE.render_report(facts)
    assert "未达标" in text
    assert "落选消耗" in text


def test_render_refreshes_auto_and_keeps_human(tmp_path: Path) -> None:
    facts = MODULE.collect_facts(_delivery(tmp_path))
    first = MODULE.render_report(facts)
    assert MODULE.HUMAN_TODO in first
    assert MODULE.pending_sections(first) == ["二、产品是什么（事实层）", "三、参考视频拆解要点", "六、风险与待办"]

    filled = first.replace(MODULE.HUMAN_TODO, "").replace("> _二 段待人工填写。_", "产品是甲方的 X1。")
    facts["candidate_pool_size"] = 42
    second = MODULE.render_report(facts, filled)
    assert "产品是甲方的 X1。" in second
    assert "| 候选池 | 42 条 |" in second
    assert MODULE.pending_sections(second) == []


def test_force_rewrites_and_restores_placeholders(tmp_path: Path) -> None:
    delivery = _delivery(tmp_path)
    target, _ = MODULE.build(delivery)
    filled = target.read_text(encoding="utf-8").replace(MODULE.HUMAN_TODO, "").replace(
        "> _二 段待人工填写。_", "写成结论了。"
    )
    target.write_text(filled, encoding="utf-8")
    _, facts = MODULE.build(delivery, force=True)
    text = target.read_text(encoding="utf-8")
    assert "写成结论了。" not in text
    assert MODULE.HUMAN_TODO in text
    assert facts["theme"] == "测试主题"


def test_missing_manifest_degrades_without_error(tmp_path: Path) -> None:
    delivery = tmp_path / "人工包"
    (delivery / "06-高清素材").mkdir(parents=True)
    (delivery / "06-高清素材" / "a.mp4").write_bytes(b"0" * 1024)
    facts = MODULE.collect_facts(delivery)
    assert facts["has_manifest"] is False
    text = MODULE.render_report(facts)
    assert "人工整理包" in text
    assert "无采集链账本" in text


def test_cli_check_exit_codes(tmp_path: Path) -> None:
    delivery = _delivery(tmp_path)
    base = [sys.executable, str(SCRIPT), "--delivery", str(delivery)]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    build = subprocess.run(base, capture_output=True, text=True, encoding="utf-8", env=env, check=False)
    assert build.returncode == 0, build.stdout + build.stderr

    check = subprocess.run([*base, "--check"], capture_output=True, text=True, encoding="utf-8", env=env, check=False)
    assert check.returncode == 2, check.stdout
    assert "人工段未填完" in check.stdout

    target = delivery / MODULE.REPORT_FILENAME
    target.write_text(target.read_text(encoding="utf-8").replace(MODULE.HUMAN_TODO, ""), encoding="utf-8")
    passed = subprocess.run([*base, "--check"], capture_output=True, text=True, encoding="utf-8", env=env, check=False)
    assert passed.returncode == 0, passed.stdout + passed.stderr


def test_cli_missing_delivery_fails(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--delivery", str(tmp_path / "nope")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )
    assert result.returncode == 1
    assert "[FAIL]" in result.stdout


@pytest.mark.parametrize("count", [0, 1, 3])
def test_inventory_counts_files(tmp_path: Path, count: int) -> None:
    delivery = tmp_path / "包"
    (delivery / "sub").mkdir(parents=True)
    for index in range(count):
        (delivery / "sub" / f"f{index}.mp4").write_bytes(b"x")
    facts = MODULE.collect_facts(delivery)
    assert facts["inventory"]["total_files"] == count
    assert facts["inventory"]["subdirectories"] == [{"name": "sub", "files": count, "bytes": count}]
