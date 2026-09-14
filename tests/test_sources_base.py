"""Unit tests for the pluggable material-source base layer.

Covers the registry (``register_source`` / ``get_source`` / ``known_sources``),
both media resolvers (returning a :class:`DownloadTarget` or ``None``), the
:class:`SourceResult` defaults, the exception hierarchy, and -- the top-priority
guarantee -- that an *absent* ``sources`` config key is a strict no-op.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from douyin_intelligence import sources
from douyin_intelligence.config import ConfigurationError, load_config
from douyin_intelligence.sources import (
    CompositeMediaResolver,
    DictMediaResolver,
    DownloadTarget,
    MediaResolutionError,
    SourceError,
    SourceNotConfigured,
    SourceResult,
)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def test_known_sources_is_exactly_the_wired_set() -> None:
    """Only *usable* sources are registered -- the whitelist must not overclaim."""
    assert sources.known_sources() == {"douyin", "ytdlp", "bilibili"}


def test_get_source_round_trips_a_registered_factory() -> None:
    class _Adapter:
        name = "unit-test-source"
        download_referer = None

        def search(self, keywords, budget, *, config, run_id=None):  # pragma: no cover
            return SourceResult(
                source=self.name, status="empty", candidates=[],
                keywords_requested=[], keywords_used=[], report={},
            )

        def resolve_media_url(self, candidate):  # pragma: no cover
            return ""

    sources.register_source("unit-test-source", _Adapter)
    try:
        adapter = sources.get_source("unit-test-source")
        assert isinstance(adapter, _Adapter)
        assert "unit-test-source" in sources.known_sources()
    finally:
        # Keep the registry clean for the exact-set assertion above (which runs
        # first) and for unrelated tests.
        sources._REGISTRY.pop("unit-test-source", None)


def test_get_source_rejects_unknown_name() -> None:
    # ``wechat_channels`` is not wired yet: its real adapter lands in a later
    # batch, so naming it is a config error raised *early*, not a runtime
    # surprise.
    for name in ("does-not-exist", "wechat_channels"):
        with pytest.raises(SourceError):
            sources.get_source(name)


def test_registry_exposes_douyin_with_legacy_referer() -> None:
    adapter = sources.get_source("douyin")
    assert adapter.name == "douyin"
    assert adapter.download_referer == "https://www.douyin.com/"


# --------------------------------------------------------------------------- #
# Resolvers
# --------------------------------------------------------------------------- #
def test_dict_resolver_hit_carries_douyin_referer_and_miss_is_none() -> None:
    resolver = DictMediaResolver({"v1": "https://signed.example/one"})

    hit = resolver.resolve_target(SimpleNamespace(video_id="v1"))
    assert isinstance(hit, DownloadTarget)
    assert hit.url == "https://signed.example/one"
    # The legacy path keeps sending the historic Douyin referer, byte for byte.
    assert hit.referer == "https://www.douyin.com/"
    # Miss -> None (no usable address), never an exception and never "".
    assert resolver.resolve_target(SimpleNamespace(video_id="missing")) is None


class _FakeAdapter:
    def __init__(self, name: str, mapping: dict[str, str], referer: str | None = None) -> None:
        self.name = name
        self.download_referer = referer
        self._mapping = dict(mapping)
        self.calls: list[str] = []

    def resolve_media_url(self, candidate) -> str:
        self.calls.append(candidate.video_id)
        return self._mapping.get(candidate.video_id, "")


def test_composite_resolver_dispatches_by_source() -> None:
    ytdlp = _FakeAdapter("ytdlp", {"yt-abc": "https://cdn.example/yt"}, referer=None)
    douyin = _FakeAdapter("douyin", {"123": "https://cdn.example/dy"}, referer="https://www.douyin.com/")
    resolver = CompositeMediaResolver({"ytdlp": ytdlp, "douyin": douyin})

    yt_target = resolver.resolve_target(SimpleNamespace(source="ytdlp", video_id="yt-abc"))
    assert yt_target is not None
    assert yt_target.url == "https://cdn.example/yt"
    # yt-dlp is cross-platform: no referer coupling.
    assert yt_target.referer is None

    dy_target = resolver.resolve_target(SimpleNamespace(source="douyin", video_id="123"))
    assert dy_target is not None
    assert dy_target.url == "https://cdn.example/dy"
    assert dy_target.referer == "https://www.douyin.com/"

    # Only the matching adapter is consulted.
    assert ytdlp.calls == ["yt-abc"]
    assert douyin.calls == ["123"]
    # Unknown source -> None.
    assert resolver.resolve_target(SimpleNamespace(source="wechat_channels", video_id="x")) is None
    # A candidate with no source attribute at all still degrades to None.
    assert resolver.resolve_target(SimpleNamespace(video_id="x")) is None


def test_composite_resolver_returns_none_when_adapter_has_no_address() -> None:
    empty = _FakeAdapter("ytdlp", {}, referer=None)
    resolver = CompositeMediaResolver({"ytdlp": empty})
    # Adapter returns "" -> composite turns it into None, not a DownloadTarget("").
    assert resolver.resolve_target(SimpleNamespace(source="ytdlp", video_id="nothing")) is None


# --------------------------------------------------------------------------- #
# SourceResult / exceptions
# --------------------------------------------------------------------------- #
def test_source_result_defaults_are_independent() -> None:
    first = SourceResult(
        source="ytdlp", status="empty", candidates=[],
        keywords_requested=[], keywords_used=[], report={},
    )
    second = SourceResult(
        source="ytdlp", status="empty", candidates=[],
        keywords_requested=[], keywords_used=[], report={},
    )
    assert first.warnings == []
    assert first.error == ""
    first.warnings.append("boom")
    assert second.warnings == []  # default_factory, not a shared list


def test_exception_hierarchy() -> None:
    assert issubclass(SourceNotConfigured, SourceError)
    assert issubclass(MediaResolutionError, SourceError)
    assert issubclass(SourceError, RuntimeError)


def test_download_target_is_frozen() -> None:
    target = DownloadTarget(url="https://cdn.example/x")
    assert target.referer is None
    with pytest.raises(Exception):
        target.url = "https://cdn.example/y"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Config: absent `sources` is a strict no-op
# --------------------------------------------------------------------------- #
def _config_payload() -> dict:
    return json.loads(json.dumps(load_config()))


def _load_with(tmp_path: Path, mutate) -> dict:
    payload = _config_payload()
    mutate(payload["jobs"]["material_replication"])
    target = tmp_path / "config.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return load_config(target)


def test_absent_sources_key_is_a_noop(tmp_path: Path) -> None:
    payload = _config_payload()
    material_replication = payload["jobs"]["material_replication"]
    assert "sources" not in material_replication
    assert "source_budgets" not in material_replication
    assert "source_gate" not in material_replication

    target = tmp_path / "config.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    loaded = load_config(target)
    # Loads without error and nothing is injected into the section.
    assert "sources" not in loaded["jobs"]["material_replication"]
    assert loaded == payload


def test_valid_sources_and_gate_pass(tmp_path: Path) -> None:
    loaded = _load_with(
        tmp_path,
        lambda section: section.update(
            {
                "sources": ["ytdlp"],
                "source_budgets": {"ytdlp": 30},
                "source_gate": {"enabled": True, "on_zero_match": "skip_source"},
            }
        ),
    )
    assert loaded["jobs"]["material_replication"]["sources"] == ["ytdlp"]


def test_unknown_source_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        _load_with(tmp_path, lambda section: section.update({"sources": ["tiktok"]}))
    # ``wechat_channels`` is not wired yet -> rejected early rather than failing
    # at runtime.
    with pytest.raises(ConfigurationError):
        _load_with(tmp_path, lambda section: section.update({"sources": ["wechat_channels"]}))


def test_sources_must_be_a_list(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        _load_with(tmp_path, lambda section: section.update({"sources": "ytdlp"}))


def test_source_budgets_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        _load_with(tmp_path, lambda section: section.update({"source_budgets": {"ytdlp": 0}}))
    with pytest.raises(ConfigurationError):
        _load_with(tmp_path, lambda section: section.update({"source_budgets": ["ytdlp"]}))


def test_source_gate_on_zero_match_enum(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        _load_with(tmp_path, lambda section: section.update({"source_gate": {"on_zero_match": "nope"}}))
    with pytest.raises(ConfigurationError):
        _load_with(tmp_path, lambda section: section.update({"source_gate": {"enabled": "yes"}}))
    # Default on_zero_match is accepted.
    loaded = _load_with(tmp_path, lambda section: section.update({"source_gate": {}}))
    assert loaded["jobs"]["material_replication"]["source_gate"] == {}
