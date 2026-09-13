from __future__ import annotations

from pathlib import Path

from douyin_intelligence.config import load_config
from douyin_intelligence.replication_candidates import Candidate
from douyin_intelligence.replication_script import (
    build_script_skeleton,
    validate_script_skeleton,
    write_script_artifacts,
)


def _candidate() -> Candidate:
    return Candidate(
        video_id="7300000000000000001",
        author="老师好我叫何同学",
        source_url="https://www.douyin.com/video/7300000000000000001",
        play_count=8120000,
        heat_score=0.91,
    )


def test_build_script_skeleton_is_structured_and_monotonic() -> None:
    config = load_config()
    transcript = {
        "status": "success",
        "text": "开场提问。现有折叠屏的顾虑。产品亮相。铰链结构。对比上代。结论。关注我。",
        "segments": [
            {"start": 0.0, "end": 5.0, "text": "开场提问"},
            {"start": 5.0, "end": 22.0, "text": "现有折叠屏的顾虑"},
            {"start": 22.0, "end": 41.0, "text": "产品亮相"},
            {"start": 41.0, "end": 62.0, "text": "铰链结构"},
            {"start": 96.0, "end": 132.0, "text": "对比上代"},
            {"start": 133.0, "end": 158.0, "text": "结论"},
            {"start": 158.0, "end": 168.0, "text": "关注我"},
        ],
    }
    skeleton = build_script_skeleton(_candidate(), transcript, {"duration_seconds": 168.4}, config)
    assert set(skeleton["sections"]) == {"hook", "pain_or_context", "product_reveal", "demo_or_compare", "conclusion", "cta"}
    assert len(skeleton["key_points"]) >= 3
    assert validate_script_skeleton(skeleton)["status"] == "pass"
    previous = -1.0
    for point in skeleton["key_points"]:
        assert point["start"] >= previous - 1e-6
        assert point["end"] <= skeleton["duration_seconds"] + 0.5
        previous = point["end"]


def test_build_script_skeleton_degrades_without_asr() -> None:
    config = load_config()
    skeleton = build_script_skeleton(_candidate(), {"status": "no_speech", "segments": [], "text": ""}, {"duration_seconds": 90.0}, config)
    assert skeleton["asr_status"] == "no_speech"
    assert skeleton["warnings"]
    assert len(skeleton["key_points"]) >= 3
    assert validate_script_skeleton(skeleton)["status"] == "pass"


def test_write_script_artifacts_includes_disclaimer(tmp_path: Path) -> None:
    config = load_config()
    skeleton = build_script_skeleton(_candidate(), {"status": "success", "segments": [], "text": "口播全文"}, {"duration_seconds": 60.0}, config)
    files = write_script_artifacts(tmp_path / "01-脚本思路", _candidate(), skeleton, {"status": "success", "text": "口播全文", "segments": []}, config)
    directory = tmp_path / "01-脚本思路"
    for name in files.values():
        assert (directory / name).is_file()
    notes = (directory / files["script_notes"]).read_text(encoding="utf-8")
    assert "不得作为事实依据" in notes
