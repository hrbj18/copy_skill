"""Resolve project-configured FFmpeg tools without mutating global PATH."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .config import resolve_path


def resolve_media_tool(config: dict[str, Any], name: str) -> str:
    configured = str((config.get("media_tools") or {}).get(name) or "").strip()
    if configured:
        path = resolve_path(configured)
        if path.is_file():
            return str(path)
    return shutil.which(name) or name


def resolve_visual_tool(settings: dict[str, Any], name: str) -> str:
    configured = str(settings.get(f"{name}_path") or "").strip()
    if configured and Path(configured).is_file():
        return configured
    return shutil.which(name) or name


def media_tool_available(config: dict[str, Any], name: str) -> bool:
    value = resolve_media_tool(config, name)
    return Path(value).is_file() or bool(shutil.which(value))
