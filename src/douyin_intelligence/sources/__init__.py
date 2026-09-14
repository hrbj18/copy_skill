"""Pluggable material-source registry.

The registry decouples "which sources exist" from "which module implements
them".  Adapters register a zero-argument *factory* so a heavy adapter (a
browser session, an SDK client) is only constructed when it is actually asked
for -- importing this package must stay cheap.

``known_sources()`` is the single source of truth used by :mod:`..config` to
validate the ``jobs.material_replication.sources`` list, so the config layer can
never name a source that does not exist.
"""

from __future__ import annotations

from typing import Callable

from .base import (  # noqa: F401  (re-exported for convenience)
    CompositeMediaResolver,
    DictMediaResolver,
    DownloadTarget,
    MediaResolutionError,
    MediaResolver,
    SourceAdapter,
    SourceError,
    SourceNotConfigured,
    SourceResult,
)


#: ``name -> factory()``.  A factory returns a *fresh* adapter on every call so
#: per-run mutable state (pending candidates, caches) can never leak between
#: runs.
_REGISTRY: dict[str, Callable[[], SourceAdapter]] = {}


def register_source(name: str, factory: Callable[[], SourceAdapter]) -> None:
    """Register ``factory`` under ``name`` (last registration wins)."""
    key = str(name or "").strip()
    if not key:
        raise SourceError("素材源名称不能为空")
    if not callable(factory):
        raise SourceError(f"素材源 {key!r} 的工厂必须可调用")
    _REGISTRY[key] = factory


def get_source(name: str) -> SourceAdapter:
    """Construct and return the adapter registered under ``name``.

    Raises :class:`SourceError` for an unknown name.
    """
    key = str(name or "").strip()
    factory = _REGISTRY.get(key)
    if factory is None:
        raise SourceError(f"未知素材源：{key!r}（可用：{sorted(_REGISTRY)}）")
    return factory()


def known_sources() -> frozenset[str]:
    """The set of registered source names (used to validate config).

    Only sources that are *actually usable* are registered here, because this set
    is the whitelist for ``jobs.material_replication.sources``: a name that
    passed validation must not blow up only at run time.  ``douyin``, ``ytdlp``
    and ``bilibili`` are wired; ``wechat_channels`` joins them once its endpoint
    is configured.
    """
    return frozenset(_REGISTRY)


# Wire the real adapters.  Registration happens *here* (not as a module side
# effect inside each module) so the modules need no import of the package and
# there is no chance of an import cycle.
from .bilibili import BilibiliSource  # noqa: E402  (import must follow register_source)
from .douyin import DouyinSource  # noqa: E402  (import must follow register_source)
from .ytdlp import YtDlpSource  # noqa: E402  (import must follow register_source)

register_source("douyin", DouyinSource)
register_source("ytdlp", YtDlpSource)
register_source("bilibili", BilibiliSource)
