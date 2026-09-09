from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from douyin_intelligence.resource_control import cleanup_temp_root, directory_bytes, remove_tree


def test_cleanup_enforces_ttl_and_quota_inside_temp_root(tmp_path: Path) -> None:
    root = tmp_path / "temp"
    old = root / "old.bin"
    new = root / "new.bin"
    root.mkdir()
    old.write_bytes(b"x" * 20)
    new.write_bytes(b"y" * 20)
    old_time = time.time() - 7200
    os.utime(old, (old_time, old_time))
    result = cleanup_temp_root(root, ttl_hours=1, quota_bytes=10)
    assert result["removed_files"] == 2
    assert directory_bytes(root) == 0


def test_remove_tree_refuses_root_or_outside(tmp_path: Path) -> None:
    root = tmp_path / "temp"
    child = root / "run" / "video"
    child.mkdir(parents=True)
    (child / "x").write_text("x", encoding="utf-8")
    assert remove_tree(child, root) == 1
    with pytest.raises(ValueError):
        remove_tree(root, root)
    with pytest.raises(ValueError):
        remove_tree(tmp_path, root)
