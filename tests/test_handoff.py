from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_handoff_policy_and_character_budgets_pass() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "audit_handoff.py"), "--root", str(ROOT), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload
    assert all(item["exists"] and item["ok"] for item in payload)


def test_handoff_forbids_process_history_as_context() -> None:
    policy = json.loads((ROOT / "docs" / "handoff" / "context-policy.json").read_text(encoding="utf-8"))
    forbidden = set(policy["forbidden_context_inputs"])
    read_set = set(policy["default_read_set"]) | set(policy["project_task_read_set"])
    assert "docs/项目开发过程文档.md" in forbidden
    assert forbidden.isdisjoint(read_set)


def test_process_record_helper_appends_without_rewriting_history(tmp_path: Path) -> None:
    process_document = tmp_path / "docs" / "项目开发过程文档.md"
    process_document.parent.mkdir(parents=True)
    process_document.write_text("existing-history", encoding="utf-8")
    record = "## 2026-08-27 test record\n\nverified"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "append_process_record.py"),
            "--root",
            str(tmp_path),
            "--record",
            record,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert process_document.read_text(encoding="utf-8") == f"existing-history\n\n{record}\n"
