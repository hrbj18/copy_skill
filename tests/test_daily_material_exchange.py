from __future__ import annotations

import io
import json
import hashlib
from pathlib import Path

import pytest
from PIL import Image

from douyin_intelligence.config import load_config
from douyin_intelligence.daily_material_exchange import (
    DailyMaterialExchangeError,
    beijing_yesterday,
    inspect_daily_material_exchange,
    simulate_daily_material_exchange,
    validate_exchange_input,
)
from douyin_intelligence import daily_material_exchange as exchange


class _FakeFetcher:
    expected_texts: list[str] = []
    def __init__(self, _settings, budget):
        self.budget = budget

    def get(self, url: str, *, maximum_bytes: int, accepted_types: tuple[str, ...]):
        self.budget.start_request()
        if accepted_types[0].startswith("image/"):
            bits = int(hashlib.sha256(url.encode("utf-8")).hexdigest()[:16], 16)
            pixels = Image.new("L", (9, 8))
            pixels.putdata([255 if (bits >> index) & 1 else 0 for index in range(72)])
            stream = io.BytesIO()
            pixels.resize((960, 640)).convert("RGB").save(stream, "JPEG", quality=90)
            body = stream.getvalue()
            self.budget.add_bytes(len(body))
            return url, "image/jpeg", body
        text = " ".join([
            "2026-08-28", "2026/8/28", "2026年8月28日",
            "国家算力互联互通区域节点", "Token Factory", "长信科技", "集成电路", "HarmonyOS 7", "Persistent",
            *self.expected_texts,
        ]).encode("utf-8")
        self.budget.add_bytes(len(text))
        return url, "text/html", text

    def close(self) -> None:
        return None


def _fixture(tmp_path: Path) -> tuple[dict, Path]:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    config["jobs"]["state_path"] = str(tmp_path / "state" / "jobs.json")
    config["jobs"]["lock_root"] = str(tmp_path / "state" / "locks")
    config["jobs"]["daily_material_exchange"].update({
        "selection_input": "config/exchange.json",
        "output_root": "output/每日新闻素材",
        "max_wall_seconds": 300,
        "max_network_requests": 24,
    })
    source = Path("config/daily_material_exchange_2026-08-28.json")
    payload = json.loads(source.read_text(encoding="utf-8"))
    _FakeFetcher.expected_texts = [
        source["expected_text"]
        for story in payload["stories"]
        for source in [*(story["heat"].get("public_signals") or []), *(story.get("fact_sources") or [])]
    ]
    selection = tmp_path / "config" / "exchange.json"
    selection.parent.mkdir(parents=True)
    selection.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return config, selection


def test_simulate_publishes_immutable_ready_package_and_root_date_consumer(tmp_path: Path, monkeypatch) -> None:
    config, selection = _fixture(tmp_path)
    monkeypatch.setattr(exchange, "SafeFetcher", _FakeFetcher)
    first = simulate_daily_material_exchange(config, business_date="2026-08-28", input_path=selection)
    assert first["status"] == "success", first["errors"]
    inspected = inspect_daily_material_exchange(config, business_date="2026-08-28")
    assert inspected["status"] == "valid" and inspected["consumer_mode"] == "root_plus_business_date"
    assert inspected["stories"] == 6 and inspected["fact_ready"] == 5 and inspected["stories_with_primary"] == 4
    assert Path(first["ready_path"]).is_file() and Path(first["current_path"]).is_file()
    first_manifest = json.loads((Path(first["output_dir"]) / "package-manifest.json").read_text(encoding="utf-8"))
    assert any(row["path"] == "daily-material-pack.json" for row in first_manifest["files"])
    second = simulate_daily_material_exchange(config, business_date="2026-08-28", input_path=selection)
    assert second["run_id"] != first["run_id"]
    assert Path(first["ready_path"]).is_file(), "old immutable package remains readable"


def test_duplicate_image_becomes_a_traceable_partial_not_a_second_slot(tmp_path: Path, monkeypatch) -> None:
    config, selection = _fixture(tmp_path)
    payload = json.loads(selection.read_text(encoding="utf-8"))
    payload["stories"][1]["images"] = [dict(payload["stories"][0]["images"][0])]
    selection.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(exchange, "SafeFetcher", _FakeFetcher)
    result = simulate_daily_material_exchange(config, business_date="2026-08-28", input_path=selection)
    pack = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
    duplicate = next(story for story in pack["stories"] if story["story_id"] == payload["stories"][1]["story_id"])
    assert result["status"] == "partial" and duplicate["assets"] == []
    assert "SHA" in duplicate["asset_errors"][0]
    assert inspect_daily_material_exchange(config, business_date="2026-08-28")["status"] == "valid"


def test_brief_labels_research_candidates_as_unconfirmed_and_lists_safeguards(tmp_path: Path, monkeypatch) -> None:
    config, selection = _fixture(tmp_path)
    monkeypatch.setattr(exchange, "SafeFetcher", _FakeFetcher)
    result = simulate_daily_material_exchange(config, business_date="2026-08-28", input_path=selection)
    brief = Path(result["brief_path"]).read_text(encoding="utf-8")
    assert "尚未确认：本条仅作为热度研究候选，不可进入事实播报。" in brief
    assert "已确认：待补证，不能当作可播事实。" not in brief
    assert "待核实：" in brief and "待核对声明：" in brief and "禁止宣称：" in brief


def test_input_rejects_secret_and_wrong_business_date() -> None:
    payload = json.loads(Path("config/daily_material_exchange_2026-08-28.json").read_text(encoding="utf-8"))
    payload["stories"][0]["cookie"] = "must-not-be-read"
    with pytest.raises(DailyMaterialExchangeError, match="敏感字段"):
        validate_exchange_input(payload)
    payload["stories"][0].pop("cookie")
    payload["stories"][0]["content_published_at"] = "2026-08-27T23:00:00+08:00"
    with pytest.raises(DailyMaterialExchangeError, match="日期窗口"):
        validate_exchange_input(payload)


def test_beijing_yesterday_handles_new_year_boundary() -> None:
    from datetime import datetime, timezone
    assert beijing_yesterday(datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)) == "2025-12-31"
