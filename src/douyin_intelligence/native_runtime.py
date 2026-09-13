"""Preload a matching Visual C++ 14.4x runtime before any native extension loads.

WHY THIS MODULE EXISTS -- please read this before deleting it
=============================================================

``onnxruntime`` (1.29.x) and ``ctranslate2`` (4.8.x, the engine behind
``faster-whisper``) are both built with the MSVC 14.4x toolchain, i.e. they link
against ``msvcp140.dll`` / ``vcruntime140.dll`` / ``vcruntime140_1.dll`` at
version ``14.44.35211.0``.

On a machine whose ``C:\\Windows\\System32`` still ships the VS2017-era
``14.00.24215.1`` runtime, importing those extensions fails -- and the failure
is **deterministic, decided by import order, not intermittent**:

* ``import onnxruntime`` raises ``DLL load failed ... 动态链接库(DLL)初始化例程
  失败`` (**WinError 1114**).  The pipeline catches this and turns it into a
  stage ``status=error``;
* more seriously, if ``ctranslate2`` is then loaded in the *same* process it
  dies with a native **access violation** -- a segfault with no Python
  traceback, so ``try/except`` cannot catch it and the whole CLI process is
  killed silently.

Which runtime member actually breaks
------------------------------------

The *load-bearing* members are ``msvcp140.dll`` and ``vcruntime140_1.dll`` --
**not** ``vcruntime140.dll``.  ``vcruntime140.dll`` is already resident under
its base name when the interpreter starts, because CPython itself ships and
loads ``<python-dir>\\VCRUNTIME140.dll`` (e.g. ``14.42.34226.3``); the
``System32`` ``14.00.24215.1`` copy therefore never wins that binding, and
loading yet another ``vcruntime140.dll`` is a no-op.  What instead gets bound to
the *old* ``System32`` ``msvcp140.dll`` is the extension's own ``msvcp140``
dependency.

The trigger chain
-----------------

``from rapidocr import RapidOCR`` -> ``rapidocr.main`` -> ``import cv2`` ->
``import numpy`` -> ``numpy._delvewheel_patch_1_11_2()``, which calls
``os.add_dll_directory()`` and thereby activates ``SetDefaultDllDirectories``.
Once that is active, Windows **ignores ``LOAD_WITH_ALTERED_SEARCH_PATH``**, so a
``.pyd`` is *no longer searched in its own directory*.  The freshly copied
``msvcp140.dll`` sitting next to the extension is never consulted, the old
``System32`` ``msvcp140`` is picked up instead, and the version mismatch raises
WinError 1114; loading ``ctranslate2`` afterwards then hits the access
violation.

Copying a matching 14.44 runtime next to the extension is therefore *necessary
but not sufficient* on its own -- which is why merely dropping the DLLs into
``onnxruntime/capi`` did not help until this preload existed.

The fix
-------

Load a matching, *complete* runtime set by absolute path as early as possible,
before any native extension is imported.  Once a module is resident in the
process under its base name, later ``LoadLibrary`` calls reuse it and the
``System32`` copies are bypassed -- regardless of the ``SetDefaultDllDirectories``
search-path change.

Portability caveat
------------------

The 14.4x trio is expected to sit next to the wheels
(``site-packages/onnxruntime/capi`` and ``site-packages/ctranslate2``).  Where it
came from a **manual copy it is not recorded in the wheel ``RECORD``**, so a
reinstall or upgrade of ``onnxruntime`` / ``ctranslate2`` silently wipes it and
re-introduces the crash.  Set ``DOUYIN_MSVC_RUNTIME_DIR`` to a directory holding
a complete 14.4x trio to make the preload portable; otherwise
:func:`preload_msvc_runtime` warns (see below) but never fails.

``douyin_intelligence/__init__.py`` calls :func:`preload_msvc_runtime`, so every
entry point (``python -m douyin_intelligence.cli ...``, the
``douyin-intelligence`` console script, or any probe that imports the package)
preloads the runtime before importing anything that links against it.

This is deliberately defensive: it must never raise.  On non-Windows, or when no
complete matching runtime set can be found, it degrades to a warning so that CLI
startup and ``pytest`` collection can never be broken by a missing optional DLL.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import sys
import warnings
from pathlib import Path
from typing import Any

__all__ = ["preload_msvc_runtime", "runtime_dir_candidates"]


# A complete 14.4x set.  Order matters: ``vcruntime140.dll`` backs
# ``vcruntime140_1.dll``, and ``msvcp140.dll`` depends on ``vcruntime140.dll``.
# ``vcruntime140.dll`` is normally already resident (CPython loads its own copy
# from the interpreter directory), so it is usually a no-op; the *load-bearing*
# members here are ``msvcp140.dll`` and ``vcruntime140_1.dll``.
_RUNTIME_DLLS: tuple[str, ...] = (
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "msvcp140.dll",
)

# Environment variable that lets an operator point at an explicit runtime dir.
_RUNTIME_DIR_ENV = "DOUYIN_MSVC_RUNTIME_DIR"

# Handles are kept alive for the lifetime of the process.  If the returned
# ``WinDLL`` objects were garbage-collected, ctypes would ``FreeLibrary`` them
# and silently undo the preload.
_LOADED_HANDLES: list[Any] = []

# Cached outcome of the (idempotent) preload.  ``None`` means "not attempted".
_PRELOADED: bool | None = None

_WARN_PREFIX = "MSVC runtime 预加载"


def _module_dir(name: str) -> Path | None:
    """Return the on-disk directory of an installed package, if importable.

    ``importlib.util.find_spec`` only locates a package; it never executes it,
    so calling this before native extensions are loaded is safe.
    """
    try:
        spec = importlib.util.find_spec(name)
    except Exception:
        return None
    if spec is None:
        return None
    locations = list(spec.submodule_search_locations or ())
    if locations:
        return Path(locations[0])
    origin = spec.origin
    if origin and origin not in {"built-in", "frozen"}:
        return Path(origin).parent
    return None


def runtime_dir_candidates() -> list[Path]:
    """Directories that may hold a complete MSVC 14.4x runtime set, in priority order.

    Priority:

    1. ``DOUYIN_MSVC_RUNTIME_DIR`` -- explicit operator override;
    2. ``<site-packages>/onnxruntime/capi`` -- the wheel's own directory;
    3. ``<site-packages>/ctranslate2`` -- the ASR engine's directory.

    :func:`preload_msvc_runtime` picks the first entry that contains the whole
    ``_RUNTIME_DLLS`` set, so a partial copy in one directory is skipped in
    favour of a complete one further down the list.
    """
    candidates: list[Path] = []

    override = os.environ.get(_RUNTIME_DIR_ENV)
    if override and override.strip():
        candidates.append(Path(override.strip()))

    onnx_dir = _module_dir("onnxruntime")
    if onnx_dir is not None:
        candidates.append(onnx_dir / "capi")

    ctranslate_dir = _module_dir("ctranslate2")
    if ctranslate_dir is not None:
        candidates.append(ctranslate_dir)

    return candidates


def _load_dll(path: str) -> Any:
    """Load a DLL by absolute path.

    Thin wrapper around ``ctypes.WinDLL`` so tests can stub out the actual load
    (which must never happen on a machine without the DLLs).
    """
    return ctypes.WinDLL(path)


def _first_complete_dir(candidates: list[Path]) -> Path | None:
    """First candidate directory that contains the *complete* runtime set."""
    for directory in candidates:
        try:
            if all((directory / name).is_file() for name in _RUNTIME_DLLS):
                return directory
        except OSError:
            # A malformed / unreadable path must never abort the search.
            continue
    return None


def _reset_state() -> None:
    """Forget the cached preload outcome (test helper only)."""
    global _PRELOADED
    _PRELOADED = None


def preload_msvc_runtime(*, force: bool = False) -> bool:
    """Preload a matching MSVC 14.4x runtime set before any native import.

    Returns ``True`` when a complete, matching set was loaded (or had already
    been loaded earlier in the process) and ``False`` when the call was a
    deliberate no-op -- i.e. a non-Windows platform or no complete set found.

    Never raises: any failure is downgraded to a :class:`UserWarning` so this
    best-effort optimisation can never prevent the CLI from starting.  The call
    is idempotent -- repeated invocations reuse the first outcome and load
    nothing again.  Pass ``force=True`` to bypass the cache (used by tests).
    """
    global _PRELOADED

    if _PRELOADED is not None and not force:
        return _PRELOADED

    if sys.platform != "win32":
        # The runtime mismatch is a Windows-only phenomenon.
        _PRELOADED = False
        return False

    directory = _first_complete_dir(runtime_dir_candidates())
    if directory is None:
        warnings.warn(
            f"{_WARN_PREFIX}：未找到成套的 MSVC 14.4x 三件套"
            "（msvcp140.dll + vcruntime140.dll + vcruntime140_1.dll），跳过预加载。"
            "若随后 onnxruntime/ctranslate2 报 WinError 1114"
            "（DLL 初始化例程失败）或 native access violation 崩溃，"
            f"请设置环境变量 {_RUNTIME_DIR_ENV} 指向含这三个 DLL 的目录后重试。"
            "注意：这三个 DLL 通常为手工拷入、不在 wheel RECORD 中，"
            "重装/升级依赖即会失效，需重新拷入或改用该环境变量。",
            UserWarning,
            stacklevel=2,
        )
        _PRELOADED = False
        return False

    try:
        for name in _RUNTIME_DLLS:
            handle = _load_dll(str((directory / name).resolve()))
            _LOADED_HANDLES.append(handle)
    except Exception as exc:  # pragma: no cover - depends on the host runtime
        warnings.warn(
            f"{_WARN_PREFIX}：从 {directory} 预加载运行时失败（{exc}），已忽略。"
            "若随后出现 WinError 1114（DLL 初始化例程失败）或 native access violation，"
            "请核实该目录下是否有成套 14.4x 三件套"
            f"（msvcp140.dll + vcruntime140.dll + vcruntime140_1.dll），或用 {_RUNTIME_DIR_ENV} 指定其它来源。",
            UserWarning,
            stacklevel=2,
        )
        _PRELOADED = False
        return False

    _PRELOADED = True
    return True
