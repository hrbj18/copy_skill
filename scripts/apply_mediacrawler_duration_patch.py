#!/usr/bin/env python
"""Apply (or check) the MediaCrawler ``duration_ms`` patch.

``third_party/MediaCrawler`` is gitignored, so the whitelist fix that makes the
Douyin crawler persist the clip length cannot live in the vendor tree.  This
script applies it idempotently -- running it twice leaves the file unchanged.

Usage::

    python scripts/apply_mediacrawler_duration_patch.py           # apply
    python scripts/apply_mediacrawler_duration_patch.py --check    # status only

Exit codes: 0 = ok / already patched; 1 = not patched (``--check``) or the
anchor/file is unavailable; 2 = unexpected error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SRC = ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from douyin_intelligence.mediacrawler_patch import (  # noqa: E402
    apply_duration_patch,
    duration_patch_status,
    mediacrawler_store_path,
)


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MediaCrawler duration_ms 补丁（幂等）")
    parser.add_argument("--check", action="store_true", help="只检查补丁状态，不修改任何文件")
    parser.add_argument("--path", default=None, help="覆盖目标文件路径（默认 third_party/MediaCrawler/...）")
    args = parser.parse_args(argv)

    path = Path(args.path) if args.path else mediacrawler_store_path({})

    if args.check:
        status = duration_patch_status({})
        _emit({"action": "check", **status})
        return 0 if status["status"] == "ok" else 1

    try:
        result = apply_duration_patch(path)
    except OSError as exc:  # Disk full / permission / encoding -- never crash silently.
        _emit({"action": "apply", "status": "error", "path": str(path), "reason": str(exc)})
        return 2

    if result.file_missing:
        _emit({"action": "apply", "status": "missing_file", "path": str(result.path), "reason": result.message})
        return 1
    if not result.anchor_found and not result.already_applied:
        _emit({"action": "apply", "status": "anchor_missing", "path": str(result.path), "reason": result.message})
        return 1

    status = "already_patched" if result.already_applied else "patched"
    _emit({
        "action": "apply", "status": status, "changed": result.changed,
        "path": str(result.path), "reason": result.message,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
