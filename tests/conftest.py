"""Shared pytest guards.

Replication tests must never read from or write to the real project ``data/``
tree (cache, media, temp).  A function-scoped autouse fixture rewrites the
implicit project root (``config["_project_root"]``) to a throwaway directory so
a test that calls ``load_config()`` without an explicit override still stays
isolated.  Tests that assign their own ``tmp_path`` afterwards keep working
because their assignment simply replaces the guarded default.

A second, session-scoped safety net records the git-tracked files at the
repository root before the first test and re-checks them at session end.  It
exists because a malformed Windows batch launcher once ran with ``cwd=ROOT``
during a bare ``pytest tests`` and deleted every top-level file.  The net turns
that class of silent data loss into a loud, actionable failure.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


# Modules whose code paths can create cache/media/temp files under the project
# root.  Matching is by test-module name (tests/ is not a package).
_GUARDED_MODULE_PREFIXES = ("test_replication_", "test_face_metrics")

# Repository root: the directory that contains ``tests/``.
_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolate_replication_project_root(request, tmp_path_factory, monkeypatch):
    module_name = getattr(request.module, "__name__", "") or ""
    if not module_name.startswith(_GUARDED_MODULE_PREFIXES):
        yield
        return

    from douyin_intelligence import config as config_module

    isolated_root = tmp_path_factory.mktemp("replication-project-root")
    original_load_config = config_module.load_config

    def _load_config(*args, **kwargs):
        payload = original_load_config(*args, **kwargs)
        if isinstance(payload, dict):
            # Redirect any implicit root into the throwaway directory.  Tests
            # that want a specific root overwrite this afterwards.
            payload["_project_root"] = str(isolated_root)
        return payload

    monkeypatch.setattr(config_module, "load_config", _load_config)
    test_module = getattr(request, "module", None)
    if test_module is not None and hasattr(test_module, "load_config"):
        monkeypatch.setattr(test_module, "load_config", _load_config)
    yield


def _tracked_top_level_files(root: Path) -> set[str] | None:
    """Return the set of git-tracked files directly under ``root``.

    Only top-level entries (no path separator) are considered: those are exactly
    the files the historical bug wiped.  Returns ``None`` when git, or a git
    repository, is unavailable so the safety net degrades to a no-op instead of
    blocking the suite on machines without git.
    """
    git = shutil.which("git")
    if git is None:
        return None
    try:
        completed = subprocess.run(
            [git, "-C", str(root), "ls-files", "-z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    tracked: set[str] = set()
    for entry in completed.stdout.split(b"\0"):
        if not entry:
            continue
        relative = entry.decode("utf-8", "surrogateescape")
        if "/" not in relative and "\\" not in relative:
            tracked.add(relative)
    return tracked


@pytest.fixture(scope="session", autouse=True)
def _guard_tracked_root_files() -> None:
    """Fail loudly if a tracked repository-root file vanishes during the run.

    A session-scoped autouse fixture is used (rather than a
    ``pytest_sessionstart`` hook) because a ``tests/conftest.py`` plugin is only
    registered during collection -- after the session-start hook has already
    fired.  The snapshot is therefore taken just before the first test runs,
    which is early enough to catch the launcher regression.
    """
    before = _tracked_top_level_files(_REPO_ROOT)
    yield
    if before is None:
        return

    vanished = sorted(
        name for name in before if not (_REPO_ROOT / name).exists()
    )
    if not vanished:
        return

    restore_lines = "\n".join(
        f'    git checkout HEAD -- "{name}"' for name in vanished
    )
    pytest.fail(
        "[tracked-file-guard] repository-root files were DELETED during this "
        "test run:\n"
        + "\n".join(f"    {name}" for name in vanished)
        + "\n\nRestore each one individually (verify the path first):\n"
        + restore_lines
        + "\n\nNever run 'git checkout -- .' - that would discard unrelated "
        "work. Investigate which test mutated the working tree.",
        pytrace=False,
    )
