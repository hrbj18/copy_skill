from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from douyin_intelligence.cli import _run_crawl_command, build_parser
from douyin_intelligence.config import load_config

ROOT = Path(__file__).parents[1]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "douyin_intelligence.cli", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_doctor_and_crawl_plan_are_offline_and_secret_free() -> None:
    doctor = _run("doctor")
    assert doctor.returncode == 0
    assert json.loads(doctor.stdout)["ready_for_offline_pipeline"] is True

    plan = _run("crawl-plan", "--mode", "creator")
    assert plan.returncode == 0
    payload = json.loads(plan.stdout)
    assert len(payload["commands"]) == 3
    assert all("--cookies" not in command for command in payload["commands"])
    assert all("creator" in command[command.index("--save_data_path") + 1] for command in payload["commands"])


def test_crawl_refuses_to_open_browser_without_explicit_flag() -> None:
    result = _run("crawl", "--mode", "creator")
    assert result.returncode == 2
    assert "--allow-browser" in result.stderr


def test_new_collection_commands_refuse_browser_without_explicit_flag() -> None:
    for command in ("browser-start", "browser-close", "collect-creators", "collect-materials"):
        result = _run(command)
        assert result.returncode == 2
        assert "--allow-browser" in result.stderr


def test_normalize_cli_writes_standard_file(tmp_path: Path) -> None:
    output = tmp_path / "normalized.json"
    result = _run(
        "normalize",
        "--input",
        str(ROOT / "tests" / "fixtures" / "creator_contents_2026-08-25.jsonl"),
        "--output",
        str(output),
    )
    assert result.returncode == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert len(payload["items"]) == 4


def test_explicit_crawl_has_a_finite_sanitized_timeout(monkeypatch) -> None:
    config = load_config()
    observed: dict[str, object] = {}

    def expire(_command, **kwargs):
        observed.update(kwargs)
        raise subprocess.TimeoutExpired("crawler", kwargs["timeout"])

    monkeypatch.setattr("douyin_intelligence.cli.subprocess.run", expire)
    assert _run_crawl_command(config, ["crawler"]) == 124
    assert observed["timeout"] == 120
    assert observed["stdout"] is subprocess.DEVNULL
    assert observed["stderr"] is subprocess.DEVNULL


def test_daily_material_pack_cli_surface_has_build_validate_and_quick() -> None:
    parser = build_parser()
    build = parser.parse_args(["daily-material-pack", "build", "--input", "selection.json", "--quick"])
    validate = parser.parse_args(["daily-material-pack", "validate", "--pack", "daily-material-pack.json"])
    assert build.command == "daily-material-pack" and build.action == "build" and build.quick is True
    assert validate.action == "validate" and validate.pack == "daily-material-pack.json"
