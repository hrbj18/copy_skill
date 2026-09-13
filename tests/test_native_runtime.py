"""Tests for the MSVC runtime preload bootstrap.

These tests never load a real system DLL: they fake ``sys.platform`` and the
candidate directories, and stub ``_load_dll`` so the suite runs identically on
any host.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest

from douyin_intelligence import native_runtime


@pytest.fixture(autouse=True)
def _fresh_preload_state():
    """Start and end every test with an empty preload cache."""
    native_runtime._reset_state()
    yield
    native_runtime._reset_state()


def _fake_runtime_dir(tmp_path: Path) -> Path:
    """Create a directory holding the complete (fake) runtime set."""
    directory = tmp_path / "runtime"
    directory.mkdir()
    for name in native_runtime._RUNTIME_DLLS:
        (directory / name).write_bytes(b"fake-dll")
    return directory


def _record_loader(loaded: list[str]):
    """A ``_load_dll`` stub that records names and returns a non-None handle."""

    def _loader(path: str):
        loaded.append(Path(path).name)
        return object()

    return _loader


def test_noop_on_non_windows(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    called: list[str] = []
    monkeypatch.setattr(native_runtime, "_load_dll", _record_loader(called))

    assert native_runtime.preload_msvc_runtime() is False
    assert called == []


def test_non_windows_does_not_warn(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        native_runtime.preload_msvc_runtime()
    assert caught == []


def test_preloads_complete_set_in_dependency_order(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    directory = _fake_runtime_dir(tmp_path)
    monkeypatch.setattr(native_runtime, "runtime_dir_candidates", lambda: [directory])
    loaded: list[str] = []
    monkeypatch.setattr(native_runtime, "_load_dll", _record_loader(loaded))

    assert native_runtime.preload_msvc_runtime() is True
    assert loaded == ["vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"]


def test_skips_incomplete_dir_for_a_complete_one(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "msvcp140.dll").write_bytes(b"only-one")
    complete = _fake_runtime_dir(tmp_path)
    monkeypatch.setattr(native_runtime, "runtime_dir_candidates", lambda: [partial, complete])
    loaded: list[str] = []
    monkeypatch.setattr(native_runtime, "_load_dll", _record_loader(loaded))

    assert native_runtime.preload_msvc_runtime() is True
    assert loaded == ["vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"]


def test_idempotent_second_call_loads_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    directory = _fake_runtime_dir(tmp_path)
    monkeypatch.setattr(native_runtime, "runtime_dir_candidates", lambda: [directory])
    loaded: list[str] = []
    monkeypatch.setattr(native_runtime, "_load_dll", _record_loader(loaded))

    assert native_runtime.preload_msvc_runtime() is True
    assert native_runtime.preload_msvc_runtime() is True
    assert loaded == ["vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"]


def test_warns_and_noops_when_no_complete_set(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(
        native_runtime,
        "runtime_dir_candidates",
        lambda: [empty, tmp_path / "does-not-exist"],
    )

    with pytest.warns(UserWarning, match="MSVC runtime"):
        assert native_runtime.preload_msvc_runtime() is False


def test_env_dir_pointing_to_empty_dir_degrades_without_raising(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("DOUYIN_MSVC_RUNTIME_DIR", str(empty))
    # Neutralise the real onnxruntime/ctranslate2 dirs so this test never loads
    # a genuine system DLL.
    monkeypatch.setattr(native_runtime, "_module_dir", lambda name: None)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = native_runtime.preload_msvc_runtime()

    assert result is False
    assert any("MSVC runtime" in str(item.message) for item in caught)


def test_env_override_dir_is_used(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    directory = _fake_runtime_dir(tmp_path)
    monkeypatch.setenv("DOUYIN_MSVC_RUNTIME_DIR", str(directory))
    monkeypatch.setattr(native_runtime, "_module_dir", lambda name: None)
    loaded: list[str] = []
    monkeypatch.setattr(native_runtime, "_load_dll", _record_loader(loaded))

    assert native_runtime.preload_msvc_runtime() is True
    assert loaded == ["vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"]


def test_load_failure_is_swallowed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    directory = _fake_runtime_dir(tmp_path)
    monkeypatch.setattr(native_runtime, "runtime_dir_candidates", lambda: [directory])

    def _boom(path: str):
        raise OSError("bad dll")

    monkeypatch.setattr(native_runtime, "_load_dll", _boom)

    with pytest.warns(UserWarning, match="预加载运行时失败"):
        assert native_runtime.preload_msvc_runtime() is False


def test_runtime_dir_candidates_priority(tmp_path: Path, monkeypatch) -> None:
    override = tmp_path / "override"
    onnx = tmp_path / "site-packages" / "onnxruntime"
    ctranslate = tmp_path / "site-packages" / "ctranslate2"
    monkeypatch.setenv("DOUYIN_MSVC_RUNTIME_DIR", str(override))
    monkeypatch.setattr(
        native_runtime,
        "_module_dir",
        lambda name: onnx if name == "onnxruntime" else ctranslate,
    )

    candidates = native_runtime.runtime_dir_candidates()

    assert candidates == [override, onnx / "capi", ctranslate]


def test_runtime_dir_candidates_without_env(monkeypatch) -> None:
    monkeypatch.delenv("DOUYIN_MSVC_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(native_runtime, "_module_dir", lambda name: None)

    assert native_runtime.runtime_dir_candidates() == []


def test_no_hardcoded_local_runtime_dir_is_shipped() -> None:
    # A machine-specific fallback path (e.g. the D:\vcruntime used during
    # bring-up) must not leak into production code.
    assert not hasattr(native_runtime, "_EXTRA_SEARCH_DIRS")


def test_shortfall_warning_is_actionable(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("DOUYIN_MSVC_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(native_runtime, "_module_dir", lambda name: None)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert native_runtime.preload_msvc_runtime() is False

    message = " ".join(str(item.message) for item in caught)
    assert "DOUYIN_MSVC_RUNTIME_DIR" in message
    assert "1114" in message
    assert "access violation" in message
    assert "msvcp140" in message
    assert "vcruntime140_1" in message
