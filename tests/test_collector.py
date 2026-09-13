from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from douyin_intelligence.collector import collect_creators, creator_commands, validate_collection
from douyin_intelligence.config import load_config


def _write_result(root: Path, account: str, creator_hash: str, video_ids: list[str]) -> None:
    target = root / "creator" / account / "douyin" / "jsonl"
    target.mkdir(parents=True)
    path = target / "creator_contents_2026-08-26.jsonl"
    path.write_text("\n".join(json.dumps({"aweme_id": item, "creator_hash": creator_hash}) for item in video_ids), encoding="utf-8")


def test_creator_commands_use_canonical_urls_and_runtime_adapter(tmp_path: Path) -> None:
    config = load_config()
    commands = creator_commands(config, tmp_path)
    assert len(commands) == 3
    for account, command, _ in commands:
        assert "mediacrawler_runner.py" in command[1]
        assert command[command.index("--creator_id") + 1] == account["url"]
        assert "--cookies" not in command


def test_collection_validation_rejects_false_identical_accounts(tmp_path: Path) -> None:
    accounts = [{"id": "one"}, {"id": "two"}]
    _write_result(tmp_path, "one", "same-hash", ["1", "2"])
    _write_result(tmp_path, "two", "same-hash", ["1", "2"])
    report = validate_collection(tmp_path, accounts, [{"account_id": "one", "returncode": 0}, {"account_id": "two", "returncode": 0}])
    assert report["status"] == "failed"
    assert any("相同 creator_hash" in error for error in report["errors"])
    assert any("完全相同作品集合" in error for error in report["errors"])


def test_collection_validation_accepts_distinct_accounts(tmp_path: Path) -> None:
    accounts = [{"id": "one"}, {"id": "two"}]
    _write_result(tmp_path, "one", "hash-one", ["1", "2"])
    _write_result(tmp_path, "two", "hash-two", ["3", "4"])
    report = validate_collection(tmp_path, accounts, [{"account_id": "one", "returncode": 0}, {"account_id": "two", "returncode": 0}])
    assert report["status"] == "success"


def test_creator_pre_sanitize_callback_failure_still_removes_temporary_media_and_cookie(tmp_path: Path, monkeypatch) -> None:
    config = load_config()
    config["media_crawler"]["runs_output"] = str(tmp_path / "runs")

    class FakeBrowserSession:
        def __init__(self, *_args, **_kwargs):
            pass

        def prepare(self) -> dict:
            return {"status": "reused", "port": 9222}

        def finish(self, _status: str) -> None:
            return None

    def fake_commands(_config: dict, run_dir: Path) -> list[tuple[dict, list[str], Path]]:
        destination = run_dir / "creator" / "one"
        raw_dir = destination / "douyin" / "jsonl"
        raw_dir.mkdir(parents=True)
        (raw_dir / "creator_contents_2026-08-31.jsonl").write_text(json.dumps({
            "aweme_id": "123456789", "title": "安全标题", "share_url": "https://www.douyin.com/video/123456789",
            "published_at": "2026-08-31T10:00:00+08:00", "video_download_url": "https://signed.test/video",
            "cookie": "must-not-survive",
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        return [({"id": "one"}, ["fake"], destination)]

    monkeypatch.setattr("douyin_intelligence.collector.BrowserSession", FakeBrowserSession)
    monkeypatch.setattr("douyin_intelligence.collector.creator_commands", fake_commands)
    monkeypatch.setattr("douyin_intelligence.collector.subprocess.run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr("douyin_intelligence.collector.validate_collection", lambda _root, _accounts, attempts: {"status": "success", "accounts": [], "attempts": attempts, "errors": []})

    def broken_callback(_files: list[Path], _account: dict) -> None:
        raise RuntimeError("fixture")

    result = collect_creators(config, "callback-fixture", before_sanitize=broken_callback)
    assert result["attempts"][0]["before_sanitize_error"] == "RuntimeError"
    path = next((tmp_path / "runs" / "callback-fixture").rglob("creator_contents_*.jsonl"))
    text = path.read_text(encoding="utf-8")
    assert "安全标题" in text
    assert "video_download_url" not in text and "signed.test" not in text and "cookie" not in text
