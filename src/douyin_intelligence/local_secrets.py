from __future__ import annotations

import os
from pathlib import Path

from .config import project_root


def _read_local_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def secret_value(name: str) -> str:
    """Read a secret without ever returning it in diagnostics or config."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    return _read_local_env(project_root() / ".env.local").get(name, "").strip()


def llm_secret_status() -> dict[str, bool]:
    return {
        "api_key_configured": bool(secret_value("DOUYIN_LLM_API_KEY")),
        "base_url_override_configured": bool(secret_value("DOUYIN_LLM_BASE_URL")),
        "model_override_configured": bool(secret_value("DOUYIN_LLM_MODEL")),
    }
