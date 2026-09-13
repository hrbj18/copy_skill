from __future__ import annotations

import os
from pathlib import Path

import pytest

from douyin_intelligence.exporter import atomic_write_json


def test_atomic_write_failure_preserves_previous_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "snapshot.json"
    target.write_text('{"old": true}\n', encoding="utf-8")

    def fail_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        atomic_write_json(target, {"new": True})
    assert target.read_text(encoding="utf-8") == '{"old": true}\n'
    assert list(tmp_path.glob("*.tmp")) == []

