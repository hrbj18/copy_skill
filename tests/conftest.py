"""Shared pytest guards.

Replication tests must never read from or write to the real project ``data/``
tree (cache, media, temp).  A module-scoped autouse fixture rewrites the
implicit project root (``config["_project_root"]``) to a throwaway directory so
a test that calls ``load_config()`` without an explicit override still stays
isolated.  Tests that assign their own ``tmp_path`` afterwards keep working
because their assignment simply replaces the guarded default.
"""

from __future__ import annotations

import pytest


# Modules whose code paths can create cache/media/temp files under the project
# root.  Matching is by test-module name (tests/ is not a package).
_GUARDED_MODULE_PREFIXES = ("test_replication_", "test_face_metrics")


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
