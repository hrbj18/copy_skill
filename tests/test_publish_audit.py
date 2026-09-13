"""QA audit of ``publish_directory`` WinError-5 handling (Task #12).

Complements the engineer's tests with the boundaries they did not cover:
only ``PermissionError`` / ``winerror == 5`` may be retried; every other
``OSError`` must surface immediately, and the two-positional-argument signature
plus the manifest schema must be unchanged.

Windows fact (verified in ``.tmp/qa_errno_probe.py`` on this machine):
``OSError(5)`` is a *plain* ``OSError`` with ``winerror is None``; a real
WinError-5 from the OS arrives as ``PermissionError`` carrying ``.winerror=5``.
So the retry predicate really keys off "PermissionError OR winerror==5"; the
tests below pin both halves separately.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from douyin_intelligence.replication_delivery import (
    MANIFEST_SCHEMA_VERSION,
    REQUIRED_MANIFEST_KEYS,
    ensure_delivery_tree,
    publish_directory,
)


def _stage(tmp_path: Path) -> Path:
    stage = tmp_path / ".stage"
    ensure_delivery_tree(stage)
    (stage / "清单.json").write_text("new", encoding="utf-8")
    return stage


def _win_exc(kind: type[OSError], message: str, winerror: int) -> OSError:
    """Build an ``OSError`` carrying an explicit ``winerror`` (Windows)."""
    exc = kind(message)
    exc.winerror = winerror  # type: ignore[attr-defined]
    return exc


def test_non_winerror5_oserror_is_not_retried(tmp_path: Path, monkeypatch) -> None:
    # winerror 17 (ERROR_NOT_SAME_DEVICE) is a genuine error, not a transient lock.
    stage = _stage(tmp_path)
    destination = tmp_path / "9.12苹果折叠屏复刻视频"
    calls = {"n": 0}

    def boom(src, dst):
        calls["n"] += 1
        raise _win_exc(OSError, "跨卷移动失败", 17)

    monkeypatch.setattr(os, "replace", boom)
    # publish_directory wraps the still-unretried OSError into a RuntimeError.
    with pytest.raises(RuntimeError):
        publish_directory(stage, destination, sleep=lambda _: None)
    assert calls["n"] == 1, f"非 winerror5 不应重试，实际调用 {calls['n']} 次"
    assert stage.exists() and (stage / "清单.json").is_file()


def test_real_windows_sharing_violation_is_retried_as_transient(tmp_path: Path, monkeypatch) -> None:
    # A real WinError 32 (ERROR_SHARING_VIOLATION) reaches Python as PermissionError
    # and is therefore treated as a transient lock -- by design, bounded to 6 tries.
    stage = _stage(tmp_path)
    destination = tmp_path / "9.12苹果折叠屏复刻视频"
    calls = {"n": 0}

    def boom(src, dst):
        calls["n"] += 1
        raise _win_exc(PermissionError, "另一个程序正在使用此文件", 32)

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(RuntimeError):
        publish_directory(stage, destination, sleep=lambda _: None)
    assert calls["n"] == 6, f"PermissionError 应重试满 6 次，实际 {calls['n']}"
    assert stage.exists() and (stage / "清单.json").is_file()


def test_permission_error_without_winerror_is_retried(tmp_path: Path, monkeypatch) -> None:
    # A bare PermissionError (no winerror attr) is still a transient-lock signal.
    stage = _stage(tmp_path)
    destination = tmp_path / "9.12苹果折叠屏复刻视频"
    calls = {"n": 0}

    def boom(src, dst):
        calls["n"] += 1
        raise PermissionError("denied")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(Exception):
        publish_directory(stage, destination, sleep=lambda _: None)
    assert calls["n"] == 6, f"PermissionError 应重试满 6 次，实际 {calls['n']}"
    assert stage.exists()


def test_destination_exists_but_first_replace_fails_keeps_both(tmp_path: Path, monkeypatch) -> None:
    """``destination`` exists and moving it to backup fails permanently.

    Invariants: the OLD complete ``destination`` is untouched, ``stage`` is
    untouched, and no leftover ``.backup`` is created.
    """
    stage = _stage(tmp_path)
    destination = tmp_path / "9.12苹果折叠屏复刻视频"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    calls = {"n": 0}

    def boom(src, dst):
        calls["n"] += 1
        raise _win_exc(PermissionError, "denied", 5)

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(RuntimeError) as excinfo:
        publish_directory(stage, destination, sleep=lambda _: None)
    assert calls["n"] == 6
    assert "被占用" in str(excinfo.value)
    assert str(stage) in str(excinfo.value)
    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert stage.exists() and (stage / "清单.json").is_file()
    assert not (tmp_path / ".9.12苹果折叠屏复刻视频.backup").exists()


def test_rollback_failure_branch_preserves_backup_for_manual_recovery(tmp_path: Path, monkeypatch) -> None:
    """Reach the rollback-FAILURE branch (source lines 206-210).

    ``destination -> backup`` succeeds, ``stage -> destination`` fails, and the
    rollback ``backup -> destination`` *also* fails.  Invariants: the OLD version
    survives in ``.backup`` for manual recovery, ``destination`` is absent, and
    ``stage`` is untouched.
    """
    stage = _stage(tmp_path)
    destination = tmp_path / "9.12苹果折叠屏复刻视频"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    backup = tmp_path / ".9.12苹果折叠屏复刻视频.backup"
    real_replace = os.replace

    def boom(src, dst):
        s, d = Path(src), Path(dst)
        if (s == stage or s == backup) and d == destination:
            raise _win_exc(PermissionError, "拒绝访问", 5)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(RuntimeError) as excinfo:
        publish_directory(stage, destination, sleep=lambda _: None)

    message = str(excinfo.value)
    assert "回滚失败" in message
    assert str(backup) in message
    assert str(stage) in message
    assert not destination.exists()
    assert backup.exists() and (backup / "old.txt").is_file()
    assert stage.exists() and (stage / "清单.json").is_file()


def test_publish_directory_two_positional_args_still_supported(tmp_path: Path) -> None:
    # replication_pipeline._publish calls publish_directory(stage, destination).
    stage = _stage(tmp_path)
    destination = tmp_path / "9.12苹果折叠屏复刻视频"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")

    publish_directory(stage, destination)  # no sleep= kwarg

    assert (destination / "清单.json").read_text(encoding="utf-8") == "new"
    assert not (destination / "old.txt").exists()
    assert not stage.exists()
    assert not (tmp_path / ".9.12苹果折叠屏复刻视频.backup").exists()


def test_manifest_schema_constants_unchanged() -> None:
    assert MANIFEST_SCHEMA_VERSION == 1
    assert REQUIRED_MANIFEST_KEYS == (
        "schema_version", "theme", "folder", "business_date", "generated_at", "keywords_used",
        "candidate_pool_size", "script_replica", "material_replica_sources", "main_materials",
        "supporting_materials", "counters", "face_backend", "face_backend_status", "ffmpeg_status",
        "degraded", "insufficient", "warnings", "evidence_disclaimer",
    )
