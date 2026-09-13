"""Controlled, idempotent patch that lets the Douyin crawler record clip length.

Why this exists
---------------
``third_party/MediaCrawler`` builds its on-disk row from a **whitelist**
(``store/douyin/__init__.py::update_douyin_aweme``) that never carried the clip
length, even though the raw ``aweme_item`` has it under
``aweme_item["video"]["duration"]`` (milliseconds).  A whitelist drop is silent:
the search JSONL simply has no ``duration`` field, so our pre-download duration
window (``prefilter.min_seconds`` / ``max_seconds``) could never run until after
a file had already been downloaded -- the root cause of "download first, filter
later, waste bandwidth".

``third_party/MediaCrawler`` is ``.gitignore``d, so an in-place code edit is not
version-controlled and would be lost on a re-clone / submodule update.  The
edit is therefore expressed here as a **re-runnable, idempotent** transform, a
marker is stamped into the patched line, and :func:`duration_patch_status` lets
``doctor`` report honestly whether the patch is in place.

The path is *not* injected via ``sitecustomize`` / ``PYTHONPATH``: MediaCrawler is
launched as a subprocess with its own ``cwd``, so import-time injection is
fragile.  A plain source edit with an explicit marker is auditable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .replication_theme import project_path

#: Relative location of the crawler store module we patch.
TARGET_RELPATH = "third_party/MediaCrawler/store/douyin/__init__.py"

#: Stamped into the inserted line so idempotency and the doctor check are a pure
#: string test (no fragile AST parsing).
PATCH_MARKER = "# [copy_skill] duration_ms patch v1"

#: Inserted immediately *after* this unique anchor inside ``save_content_item``.
ANCHOR = '        "video_download_url": _extract_video_download_url(aweme_item),\n'

#: The whitelist field that carries ``aweme_item["video"]["duration"]`` -- Douyin
#: reports this in **milliseconds**, so the name keeps the unit unambiguous.
DURATION_LINE = (
    '        "duration_ms": str((aweme_item.get("video") or {}).get("duration") or ""),'
    f"  {PATCH_MARKER}\n"
)

#: The command a human should run when the patch is missing.
INSTALL_COMMAND = "python scripts/apply_mediacrawler_duration_patch.py"


def mediacrawler_store_path(config: dict[str, Any] | None = None) -> Path:
    """Absolute path of the crawler store module (honours ``_project_root``)."""
    return project_path(config or {}, TARGET_RELPATH)


def is_duration_patch_applied(path: Path) -> bool:
    """``True`` when the store module already carries the duration patch.

    A missing file is *not* an error here -- callers (e.g. ``doctor``) must be
    able to ask the question on a machine that never installed MediaCrawler.
    """
    if not path.is_file():
        return False
    try:
        return PATCH_MARKER in path.read_text(encoding="utf-8")
    except OSError:
        return False


@dataclass(frozen=True)
class DurationPatchResult:
    """Outcome of :func:`apply_duration_patch`, explicit so the CLI can report it."""

    path: Path
    changed: bool
    already_applied: bool
    file_missing: bool
    anchor_found: bool
    message: str


def apply_duration_patch(path: Path) -> DurationPatchResult:
    """Idempotently add ``duration_ms`` to ``update_douyin_aweme``'s whitelist.

    Safe to run repeatedly: the second run detects the marker and changes
    nothing (byte-for-byte identical).  Never raises on a re-run; only a missing
    file / missing anchor is reported as an unpatched, non-fatal outcome.
    """
    if not path.is_file():
        return DurationPatchResult(
            path=path, changed=False, already_applied=False, file_missing=True,
            anchor_found=False,
            message=f"未找到 MediaCrawler 存储文件：{path}（MediaCrawler 可能未安装）",
        )

    text = path.read_text(encoding="utf-8")
    if PATCH_MARKER in text:
        return DurationPatchResult(
            path=path, changed=False, already_applied=True, file_missing=False,
            anchor_found=True,
            message="补丁已存在，未作任何修改（幂等）。",
        )
    if ANCHOR not in text:
        return DurationPatchResult(
            path=path, changed=False, already_applied=False, file_missing=False,
            anchor_found=False,
            message=(
                "未找到插入锚点（MediaCrawler 版本可能已变更），未作任何修改。"
                f" 请人工核对 {path} 的 update_douyin_aweme()。"
            ),
        )

    patched = text.replace(ANCHOR, ANCHOR + DURATION_LINE, 1)
    # ``newline=""`` avoids platform line-ending translation so the patch is
    # byte-stable across runs (and the idempotency hash holds).
    path.write_text(patched, encoding="utf-8", newline="")
    return DurationPatchResult(
        path=path, changed=True, already_applied=False, file_missing=False,
        anchor_found=True,
        message="已写入 duration_ms 字段（毫秒），下载前时长窗口可按元数据生效。",
    )


def duration_patch_status(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """A ``doctor``-shaped status dict for the MediaCrawler duration patch.

    ``status`` is ``"ok"`` when the patch is in place, else ``"not_patched"`` /
    ``"missing_file"`` with an explicit Chinese ``reason`` so the operator knows
    the consequence (metadata duration unavailable -> window only runs post-
    download) and the exact fix.
    """
    path = mediacrawler_store_path(config)
    if not path.is_file():
        return {
            "status": "missing_file",
            "patched": False,
            "path": str(path),
            "reason": f"MediaCrawler 未安装或路径不存在：{path}",
            "consequence": "无法获取元数据时长，时长窗口仅在下载后按实测时长生效。",
            "install_command": INSTALL_COMMAND,
        }
    if is_duration_patch_applied(path):
        return {
            "status": "ok",
            "patched": True,
            "path": str(path),
            "reason": "元数据时长补丁就绪：抖音搜索落盘含 duration_ms（毫秒），下载前时长窗口可按元数据生效。",
            "consequence": "",
            "install_command": INSTALL_COMMAND,
        }
    return {
        "status": "not_patched",
        "patched": False,
        "path": str(path),
        "reason": "未应用元数据时长补丁：抖音搜索落盘不含 duration_ms，下载前时长窗口无法生效。",
        "consequence": "本环境未启用元数据时长，时长窗口仅在下载后按实测时长执行（先下载后筛选）。",
        "install_command": INSTALL_COMMAND,
    }
