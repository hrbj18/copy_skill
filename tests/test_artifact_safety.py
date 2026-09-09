from __future__ import annotations

import json
from pathlib import Path

from douyin_intelligence.artifact_safety import sanitize_raw_file
from douyin_intelligence.config import load_config


def test_sanitize_raw_file_keeps_metadata_and_removes_signed_or_auth_fields(tmp_path: Path) -> None:
    target = tmp_path / "search_contents_2026-08-27.jsonl"
    target.write_text(json.dumps({
        "aweme_id": "1234567890123456789",
        "desc": "安全元数据",
        "share_url": "https://www.douyin.com/video/1234567890123456789?previous_page=app_code_link",
        "create_time": 1787808000,
        "statistics": {"digg_count": 12, "comment_count": 3},
        "play_addr": {"url_list": ["https://douyinvod.example/signed?x-signature=secret"]},
        "download_addr": {"url_list": ["https://aweme.snssdk.example/download?auth_key=secret"]},
        "cookie": "test-cookie",
        "authorization": "Bearer test-token",
        "creator_hash": "public-creator-hash",
    }, ensure_ascii=False) + "\n", encoding="utf-8")

    result = sanitize_raw_file(target, load_config(), "douyin_search")
    text = target.read_text(encoding="utf-8")
    row = json.loads(text)

    assert result["record_count"] == 1
    assert result["removed_sensitive_field_count"] >= 4
    assert row["aweme_id"] == "1234567890123456789"
    assert row["title"] == "安全元数据"
    assert row["share_url"] == "https://www.douyin.com/video/1234567890123456789"
    assert row["digg_count"] == 12
    assert row["creator_hash"] == "public-creator-hash"
    assert "douyinvod" not in text
    assert "aweme.snssdk" not in text
    assert "test-cookie" not in text
    assert "test-token" not in text


def test_sanitize_raw_file_enforces_a_metadata_limit(tmp_path: Path) -> None:
    target = tmp_path / "search_contents_2026-08-27.jsonl"
    target.write_text("\n".join(json.dumps({"aweme_id": str(1234567890123456789 + index), "desc": f"项目 {index}"}, ensure_ascii=False) for index in range(3)) + "\n", encoding="utf-8")

    result = sanitize_raw_file(target, load_config(), "douyin_search", maximum_records=1)
    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]

    assert result["record_count"] == 1
    assert result["discarded_record_count"] == 2
    assert len(rows) == 1
