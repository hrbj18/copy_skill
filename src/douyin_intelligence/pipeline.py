from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .config import resolve_path
from .exporter import build_outputs, export_outputs
from .normalize import normalize_files
from .scoring import deduplicate, score_records
from .state import load_state, prepare_state, save_state


def run_pipeline(
    inputs: Iterable[str | Path],
    *,
    config: dict[str, Any],
    target_date: str | None = None,
    source: str | None = None,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    target = target_date or date.today().isoformat()
    input_paths = [Path(value) for value in inputs]
    normalized = normalize_files(input_paths, config, source)
    scored, selection_report = score_records(normalized, target, config)
    unique, duplicates = deduplicate(scored)
    state_path = resolve_path(str(config.get("state_path") or "data/state/seen_videos.json"))
    previous_state = load_state(state_path)
    next_state, state_report = prepare_state(previous_state, unique, target)
    top_n = int(config.get("top_n") or 30)
    destination = Path(output_dir or Path("output") / "openmontage" / target)
    report = {
        "version": "1.0",
        "inputs": [str(path.resolve()) for path in input_paths],
        "normalized_count": len(normalized),
        **selection_report,
        "deduplicated_count": len(unique),
        "duplicate_count": len(duplicates),
        "duplicates": duplicates,
        "state": {"path": str(state_path.resolve()), **state_report},
        "warnings": [
            "抖音内容仅作为题材与热度信号，不是新闻事实证据。",
            "播放量缺失时兼容快照中的 play_count 为 0；完整候选保留 play_count_missing 标记。",
        ],
    }
    payloads = build_outputs(
        unique,
        target_date=target,
        timezone_name=str(config["timezone"]),
        top_n=top_n,
        run_report=report,
    )
    written = export_outputs(destination, payloads)
    save_state(state_path, next_state)
    return {"output_dir": str(destination.resolve()), "files": [str(path.resolve()) for path in written], **report}
