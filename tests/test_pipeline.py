from __future__ import annotations

import json
from pathlib import Path

from douyin_intelligence.config import load_config
from douyin_intelligence.pipeline import run_pipeline


FIXTURES = Path(__file__).parent / "fixtures"


def test_pipeline_exports_openmontage_contract_and_is_repeatable(tmp_path: Path) -> None:
    output = tmp_path / "package"
    config = load_config()
    config["state_path"] = str(tmp_path / "seen_videos.json")
    result = run_pipeline(
        [FIXTURES / "creator_contents_2026-08-25.jsonl", FIXTURES / "search_contents_2026-08-25.json"],
        config=config,
        target_date="2026-08-25",
        output_dir=output,
    )
    assert result["normalized_count"] == 6
    assert result["eligible_count"] == 5
    assert result["deduplicated_count"] == 4
    hotboard = json.loads((output / "hotboard.json").read_text(encoding="utf-8"))
    benchmark = json.loads((output / "benchmark_accounts.json").read_text(encoding="utf-8"))
    candidates = json.loads((output / "content_candidates.json").read_text(encoding="utf-8"))
    assert set(hotboard) >= {"captured_at", "items"}
    assert set(hotboard["items"][0]) >= {"word", "hotScore", "url"}
    assert len(benchmark["videos"]) == 3
    assert set(benchmark["videos"][0]) >= {"title", "account_name", "play_count", "share_url"}
    assert all(item["play_count_missing"] for item in benchmark["videos"])
    assert len(candidates["items"]) == 4
    first_bytes = (output / "content_candidates.json").read_bytes()
    run_pipeline(
        [FIXTURES / "creator_contents_2026-08-25.jsonl", FIXTURES / "search_contents_2026-08-25.json"],
        config=config,
        target_date="2026-08-25",
        output_dir=output,
    )
    second = json.loads((output / "content_candidates.json").read_text(encoding="utf-8"))
    first = json.loads(first_bytes.decode("utf-8"))
    first.pop("captured_at")
    second.pop("captured_at")
    assert first == second
    state = json.loads((tmp_path / "seen_videos.json").read_text(encoding="utf-8"))
    assert len(state["videos"]) == 4
    assert all(item["target_dates"] == ["2026-08-25"] for item in state["videos"].values())

