"""Shared pytest guards.

Replication tests must never read from or write to the real project ``data/``
tree (cache, media, temp).  A function-scoped autouse fixture rewrites the
implicit project root (``config["_project_root"]``) to a throwaway directory so
a test that calls ``load_config()`` without an explicit override still stays
isolated.  Tests that assign their own ``tmp_path`` afterwards keep working
because their assignment simply replaces the guarded default.

The same fixture also strips the production-enabled *opt-in* switches
(``_OPT_IN_MATERIAL_SWITCHES``) out of every ``load_config()`` payload a test
sees.  A test fixture built from the live config must be closed by default: a
feature the operator switched on in ``config/content_intelligence.json`` must
not silently change a test that never asked for it.  A test that does exercise
such a feature opts in explicitly, by writing the config block itself after
building its fixture.  By the same logic nothing here ever asserts on the
*shipped* value of a switch: such an assertion passes only while the switch
happens to be off, and turns into a lie -- or a red suite -- the moment an
operator turns it on.

A second, session-scoped safety net records the git-tracked files at the
repository root that *exist* before the first test and re-checks them at session
end.  It exists because a malformed Windows batch launcher once ran with
``cwd=ROOT`` during a bare ``pytest tests`` and deleted every top-level file.
The net turns that class of silent data loss into a loud, actionable failure.
"""

from __future__ import annotations

import shutil
import subprocess
import warnings
from pathlib import Path

import pytest


# Modules whose code paths can create cache/media/temp files under the project
# root.  Matching is by test-module name (tests/ is not a package).
_GUARDED_MODULE_PREFIXES = ("test_replication_", "test_face_metrics")

# Opt-in behaviour switches that ship ENABLED in config/content_intelligence.json
# (jobs.material_replication.*).  Production wants them on, but a test that builds
# its fixture from the live ``load_config()`` wants the *previous* behaviour
# unless it explicitly opts in.  They are therefore stripped from every
# ``load_config()`` call made inside a test, mirroring the long-standing
# per-helper convention of ``mr.pop("download_budget", None)``.  Do NOT
# "helpfully" re-enable them or delete this constant: a test that exercises one
# of these features must set the block itself after building its fixture (see
# test_replication_selection.py / test_replication_cli.py).
_OPT_IN_MATERIAL_SWITCHES = (
    "relevance_gate", "visual_verify", "dedup_across_runs", "theme_event_terms", "direct_delivery",
    "sources", "source_duration_windows", "theme_material_profiles", "theme_profile_map",
)

# Same contract, one level deeper: switches that live under
# ``jobs.material_replication.material_replica.*``.  ``max_age_days`` ships at 90
# in production, and a fixture that inherits it would make the "switched off
# changes nothing" guard cases vacuous -- they must build their window from
# scratch, so the seam clears it here.
_OPT_IN_MATERIAL_REPLICA_SWITCHES = ("max_age_days",)

# Nested opt-in blocks under ``jobs.material_replication.*`` that ship enabled in
# production.  ``episode_research_pack`` publishes an extra, independent pack on
# every material-replication run; that must stay a no-op for a test that never
# asked for it, so the seam forces the block's ``enabled`` back to False while
# leaving its other keys (``output_root`` / ``annotate_delivery_manifest``) alone.
# A test that exercises the feature sets ``enabled: true`` itself.
_OPT_IN_NESTED_MATERIAL_BLOCKS = ("episode_research_pack",)


def _strip_opt_in_material_switches(payload: dict) -> dict:
    jobs = payload.get("jobs")
    material = jobs.get("material_replication") if isinstance(jobs, dict) else None
    if isinstance(material, dict):
        for key in _OPT_IN_MATERIAL_SWITCHES:
            material.pop(key, None)
        replica = material.get("material_replica")
        if isinstance(replica, dict):
            for key in _OPT_IN_MATERIAL_REPLICA_SWITCHES:
                replica.pop(key, None)
        for name in _OPT_IN_NESTED_MATERIAL_BLOCKS:
            block = material.get(name)
            if isinstance(block, dict):
                block["enabled"] = False
                block.pop("ledger_root", None)
    return payload

# Repository root: the directory that contains ``tests/``.
_REPO_ROOT = Path(__file__).resolve().parents[1]

# Bundled PortableGit shipped with the WorkBuddy runner.  git is NOT on the
# system PATH on this machine, so a plain ``shutil.which`` misses it.  We glob
# the *version* directory (never pinning a version number) as a second probe.
_PORTABLE_GIT_BASE = Path(
    r"C:\Users\Administrator\.workbuddy\binaries\PortableGit\versions"
)


@pytest.fixture(autouse=True)
def _isolate_replication_project_root(request, tmp_path_factory, monkeypatch):
    # Two jobs, deliberately kept in ONE fixture so there is only ever one wrapper
    # around ``load_config`` in a given test:
    #   * every test: drop the opt-in switches (see _OPT_IN_MATERIAL_SWITCHES)
    #   * replication / face tests: additionally redirect the implicit project root
    module_name = getattr(request.module, "__name__", "") or ""
    isolated_root = (
        tmp_path_factory.mktemp("replication-project-root")
        if module_name.startswith(_GUARDED_MODULE_PREFIXES)
        else None
    )

    from douyin_intelligence import config as config_module

    original_load_config = config_module.load_config

    def _load_config(*args, **kwargs):
        payload = original_load_config(*args, **kwargs)
        if isinstance(payload, dict):
            if isolated_root is not None:
                # Redirect any implicit root into the throwaway directory.  Tests
                # that want a specific root overwrite this afterwards.
                payload["_project_root"] = str(isolated_root)
            _strip_opt_in_material_switches(payload)
        return payload

    monkeypatch.setattr(config_module, "load_config", _load_config)
    test_module = getattr(request, "module", None)
    if test_module is not None and hasattr(test_module, "load_config"):
        monkeypatch.setattr(test_module, "load_config", _load_config)
    yield


def _find_git() -> str | None:
    """Locate a usable git executable, or ``None`` when none can be found.

    Probe order:
    1. ``shutil.which("git")`` -- honours the caller's PATH.
    2. The bundled PortableGit under :data:`_PORTABLE_GIT_BASE`: glob
       ``*/cmd/git.exe`` so the version directory is never hard-coded.  A
       missing base directory is treated as "not found" rather than an error.
    """
    found = shutil.which("git")
    if found:
        return found
    try:
        candidates = sorted(_PORTABLE_GIT_BASE.glob("*/cmd/git.exe"))
    except OSError:
        candidates = []
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _tracked_top_level_files(root: Path) -> set[str] | None:
    """Return the set of git-tracked files directly under ``root``.

    Only top-level entries (no path separator) are considered: those are exactly
    the files the historical bug wiped.  Returns ``None`` when git, or a git
    repository, is unavailable so the caller can warn instead of failing
    silently.
    """
    git = _find_git()
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


def _warn_guard_disabled(config) -> None:
    """Emit a visible one-line warning; the guard must never fail silently."""
    message = "[tracked-file-guard] git unavailable - guard disabled"
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(message, yellow=True)
    else:
        warnings.warn(message, stacklevel=1)


@pytest.fixture(scope="session", autouse=True)
def _guard_tracked_root_files(pytestconfig) -> None:
    """Fail loudly if a tracked repository-root file vanishes during the run.

    A session-scoped autouse fixture is used (rather than a
    ``pytest_sessionstart`` hook) because a ``tests/conftest.py`` plugin is only
    registered during collection -- after the session-start hook has already
    fired.  The snapshot is therefore taken just before the first test runs.

    Only files that actually exist at snapshot time are watched, so a file that
    was already missing before the run can never be blamed on this run.
    """
    tracked = _tracked_top_level_files(_REPO_ROOT)
    if tracked is None:
        _warn_guard_disabled(pytestconfig)
        yield
        return

    # "was present before" -- a file already gone must not be reported later.
    before = {name for name in tracked if (_REPO_ROOT / name).exists()}
    yield

    vanished = sorted(name for name in before if not (_REPO_ROOT / name).exists())
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
