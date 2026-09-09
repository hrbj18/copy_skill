from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Iterable


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def directory_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def cleanup_temp_root(root: Path, *, ttl_hours: float, quota_bytes: int, protected: Iterable[Path] = ()) -> dict[str, int]:
    """Delete only files under an explicit temp root, oldest first."""
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    protected_roots = [path.resolve() for path in protected]
    removed_files = 0
    removed_bytes = 0
    now = time.time()

    def is_protected(path: Path) -> bool:
        resolved = path.resolve()
        return any(resolved == item or _inside(resolved, item) for item in protected_roots)

    files = sorted((path for path in root.rglob("*") if path.is_file()), key=lambda item: item.stat().st_mtime)
    for path in files:
        if not _inside(path, root) or is_protected(path):
            continue
        age_hours = (now - path.stat().st_mtime) / 3600
        if age_hours >= max(0.0, ttl_hours):
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            removed_files += 1
            removed_bytes += size

    remaining = sorted((path for path in root.rglob("*") if path.is_file() and not is_protected(path)), key=lambda item: item.stat().st_mtime)
    total = directory_bytes(root)
    for path in remaining:
        if total <= max(0, quota_bytes):
            break
        if not _inside(path, root):
            continue
        size = path.stat().st_size
        path.unlink(missing_ok=True)
        total -= size
        removed_files += 1
        removed_bytes += size

    for directory in sorted((item for item in root.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    return {"removed_files": removed_files, "removed_bytes": removed_bytes, "remaining_bytes": directory_bytes(root)}


def remove_tree(path: Path, root: Path) -> int:
    path = path.resolve()
    root = root.resolve()
    if path == root or not _inside(path, root):
        raise ValueError(f"拒绝删除临时根目录之外的路径：{path}")
    size = directory_bytes(path)
    if path.exists():
        shutil.rmtree(path)
    return size
