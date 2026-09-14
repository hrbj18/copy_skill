"""Multi-source candidate-pool wiring (``jobs.material_replication.sources``).

Covers the switch that turns the second discovery source (Bilibili) into real
pool members:

* absent -- the legacy Douyin-only path runs untouched (no new keys);
* present -- every configured source is fanned out and merged, each candidate is
  labelled with its own ``source``, and the pool is keyed on
  ``(source, video_id)`` so identical-looking ids from two platforms never
  collapse into one another;
* a broken / unknown source degrades that source only.

No test here touches the network: the Douyin adapter is driven through the
injected ``deps.collector`` seam and every other source through a stub.
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from unittest.mock import patch

from douyin_intelligence.config import load_config
from douyin_intelligence.replication_candidates import Candidate, collect_candidate_pool
from douyin_intelligence.sources.base import SourceResult

VIDEO_ID = "7300000000000000001"
BV_ID = "BV1uUbG6FEfb"


def _config(tmp_path: Path) -> dict:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    return config


def _row(video_id: str, *, url: str = "") -> dict:
    row = {
        "aweme_id": video_id,
        "desc": f"标题-{video_id}",
        "author": {"uid": "author-x", "nickname": "作者"},
        "create_time": "2026-09-11T08:00:00+08:00",
        "statistics": {"digg_count": 100, "comment_count": 1, "share_count": 1, "collect_count": 1},
        "duration": 45,
        "share_url": f"https://www.douyin.com/video/{video_id}",
    }
    if url:
        row["video_download_url"] = url
    return row


def _deps(tmp_path: Path, video_id: str = VIDEO_ID) -> types.SimpleNamespace:
    """Douyin's injected collector seam: writes one raw row before sanitizing."""

    source = tmp_path / "search" / "search_contents_1.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        json.dumps([_row(video_id, url="https://signed.example/secret")], ensure_ascii=False),
        encoding="utf-8",
    )

    def fake_collector(cfg, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None, **kwargs):
        before_sanitize([source])
        return {"status": "success", "budget": budget, "keywords": keywords}

    return types.SimpleNamespace(collector=fake_collector)


class _StubAdapter:
    """A source that returns a canned ``SourceResult``.

    ``resolve_media_url`` raises: it proves the collection stage never
    pre-resolves a *lazy* source (that call belongs at download time and would
    otherwise hit the network during collection).
    """

    def __init__(self, name: str, result: dict) -> None:
        self.name = name
        self.download_referer = None
        self._result = result
        self.searched: list[tuple[list[str], int]] = []

    def search(self, keywords, budget, *, config, run_id=None):
        self.searched.append((list(keywords), budget))
        payload = dict(self._result)
        payload.setdefault("keywords_requested", list(keywords))
        return SourceResult(**payload)

    def resolve_media_url(self, candidate):  # pragma: no cover - must never run
        raise AssertionError("采集阶段不得预解析懒加载素材源")


class _BoomAdapter:
    name = "boom"

    def search(self, keywords, budget, *, config, run_id=None):
        raise RuntimeError("adapter exploded")

    def resolve_media_url(self, candidate):  # pragma: no cover - must never run
        raise AssertionError


def _bilibili_result() -> dict:
    return {
        "source": "bilibili",
        "status": "success",
        "candidates": [
            Candidate(video_id=BV_ID, title="手搓microduck", author="AI研究室", duration_seconds=1317.0),
            # Deliberately the same video_id as the Douyin candidate: ids are only
            # unique within one source, so this must NOT be de-duplicated away.
            Candidate(video_id=VIDEO_ID, title="同号不同源", author="机器序言", duration_seconds=180.0),
            # A within-source duplicate: this one must be dropped.
            Candidate(video_id=BV_ID, title="重复", author="AI研究室", duration_seconds=1317.0),
        ],
        "keywords_used": ["机器鸭", "Microduck"],
        "report": {"site": "bilibili", "status": "success"},
        "warnings": ["b站限流提示"],
    }


def _stub_factory(result: dict):
    def factory(name: str) -> _StubAdapter:
        return _StubAdapter(name, result)

    return factory


def test_absent_sources_key_keeps_the_legacy_douyin_only_pool(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert "sources" not in (config["jobs"]["material_replication"] or {})

    pool = collect_candidate_pool(config, "机械鸭", pool_size=40, run_id="legacy", deps=_deps(tmp_path))

    # No additive block on the default path: every pre-existing key keeps its
    # historical value, including the signed-URL capture.
    assert "sources" not in pool
    assert [candidate.source for candidate in pool["candidates"]] == ["douyin"]
    assert pool["media_urls"] == {VIDEO_ID: "https://signed.example/secret"}
    assert "signed.example" not in json.dumps(pool["candidate_pool"], ensure_ascii=False)


def test_configured_sources_merge_every_source_into_one_pool(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["jobs"]["material_replication"]["sources"] = ["douyin", "bilibili"]

    with patch("douyin_intelligence.sources.get_source", side_effect=_stub_factory(_bilibili_result())):
        pool = collect_candidate_pool(config, "机械鸭", pool_size=40, run_id="multi", deps=_deps(tmp_path))

    keys = {(candidate.source, candidate.video_id) for candidate in pool["candidates"]}
    assert keys == {("bilibili", BV_ID), ("bilibili", VIDEO_ID), ("douyin", VIDEO_ID)}

    records = {record["source"]: record for record in pool["sources"]}
    assert set(records) == {"douyin", "bilibili"}
    # ``returned_count`` is what the source returned, ``candidate_count`` what
    # survived cross-source de-duplication.
    assert (records["bilibili"]["returned_count"], records["bilibili"]["candidate_count"]) == (3, 2)
    assert (records["douyin"]["returned_count"], records["douyin"]["candidate_count"]) == (1, 1)
    assert records["bilibili"]["status"] == "success"

    # The per-source truth reaches disk through the existing search_report write.
    assert pool["search_report"]["sources"] == pool["sources"]
    assert pool["warnings"][0] == "b站限流提示"

    # Every candidate on disk carries its own source, and only the URL-capturing
    # source contributes signed addresses.
    serialized = json.dumps(pool["candidate_pool"], ensure_ascii=False)
    assert '"source": "bilibili"' in serialized and '"source": "douyin"' in serialized
    assert pool["media_urls"] == {VIDEO_ID: "https://signed.example/secret"}
    assert "signed.example" not in serialized


def test_same_video_id_from_two_sources_is_kept_once_per_source(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["jobs"]["material_replication"]["sources"] = ["douyin", "bilibili"]

    with patch("douyin_intelligence.sources.get_source", side_effect=_stub_factory(_bilibili_result())):
        pool = collect_candidate_pool(config, "机械鸭", pool_size=40, run_id="dedup", deps=_deps(tmp_path))

    ids = [candidate.video_id for candidate in pool["candidates"]]
    # The Douyin id appears twice -- once per source -- and no more: a within-source
    # duplicate must not survive.
    assert ids.count(VIDEO_ID) == 2
    assert ids.count(BV_ID) == 1


def test_identical_id_string_from_two_sources_is_two_candidates(tmp_path: Path) -> None:
    """The composite key must not depend on the id *shape*.

    Adversarial form of the test above: the Douyin row and the Bilibili item
    carry the **very same id string**.  If a candidate were keyed on the id alone
    -- or if the Bilibili candidate had kept the ``Candidate.source`` default and
    been mislabelled ``douyin`` -- one of the two would silently disappear.
    """
    config = _config(tmp_path)
    config["jobs"]["material_replication"]["sources"] = ["douyin", "bilibili"]

    from douyin_intelligence.sources.bilibili import BilibiliSource

    bilibili_candidate = BilibiliSource()._to_candidate(
        {"bvid": BV_ID, "title": "机器鸭", "author": "AI研究室", "duration": "03:01"}, "机器鸭",
    )
    assert bilibili_candidate is not None
    assert bilibili_candidate.source == "bilibili"

    stub_result = {
        "source": "bilibili", "status": "success", "candidates": [bilibili_candidate],
        "keywords_used": ["机器鸭"], "report": {},
    }
    with patch("douyin_intelligence.sources.get_source", side_effect=_stub_factory(stub_result)):
        pool = collect_candidate_pool(
            config, "机器鸭", pool_size=40, run_id="clash", deps=_deps(tmp_path, video_id=BV_ID),
        )

    keys = {(candidate.source, candidate.video_id) for candidate in pool["candidates"]}
    assert keys == {("douyin", BV_ID), ("bilibili", BV_ID)}


def test_a_broken_or_unknown_source_degrades_that_source_only(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["jobs"]["material_replication"]["sources"] = ["douyin", "boom", "ghost"]

    # Captured *before* the patch so the unknown name still hits the real
    # registry (and its real SourceError) instead of recursing into the stub.
    from douyin_intelligence.sources import get_source as real_get_source

    def factory(name: str):
        if name == "ghost":
            return real_get_source(name)
        return _BoomAdapter()

    with patch("douyin_intelligence.sources.get_source", side_effect=factory):
        pool = collect_candidate_pool(config, "机械鸭", pool_size=40, run_id="broken", deps=_deps(tmp_path))

    assert pool["status"] == "partial"
    assert [candidate.source for candidate in pool["candidates"]] == ["douyin"]
    records = {record["source"]: record for record in pool["sources"]}
    assert records["boom"]["status"] == "failed" and "adapter exploded" in records["boom"]["error"]
    assert records["ghost"]["status"] == "failed" and "未知素材源" in records["ghost"]["error"]
    # The pool is not empty, so the failure must be attributed to the source --
    # not reported as "the crawl produced no keyword coverage".
    detail = next(item for item in pool["warnings"] if "素材源失败 2/3" in item)
    assert "boom、ghost" in detail
    assert "其余源候选仍已入池（1 条）" in detail


def test_every_source_failing_claims_no_keyword_coverage(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["jobs"]["material_replication"]["sources"] = ["boom"]

    with patch("douyin_intelligence.sources.get_source", side_effect=lambda name: _BoomAdapter()):
        pool = collect_candidate_pool(config, "机械鸭", pool_size=40, run_id="dead", deps=_deps(tmp_path))

    assert pool["status"] == "failed"
    assert pool["candidates"] == [] and pool["keywords_used"] == []
    shortfall = next(item for item in pool["warnings"] if "低于最小目标" in item)
    assert "候选池采集未成功，未取得有效关键词覆盖" in shortfall
    detail = next(item for item in pool["warnings"] if "素材源失败 1/1" in item)
    assert "候选池未获得任何来源的候选" in detail


def test_douyin_only_configuration_reproduces_the_legacy_pool(tmp_path: Path) -> None:
    """``sources: ["douyin"]`` must equal the legacy pool, plus the source block.

    Douyin is routed through ``DouyinSource`` here, driven by the same injected
    collector -- proven by making a real registry lookup fail the test.
    """
    config = _config(tmp_path)
    legacy = collect_candidate_pool(config, "机械鸭", pool_size=40, run_id="a", deps=_deps(tmp_path))

    config["jobs"]["material_replication"]["sources"] = ["douyin"]
    with patch(
        "douyin_intelligence.sources.get_source",
        side_effect=AssertionError("douyin 必须走注入的 collector，而不是真爬虫"),
    ):
        solo = collect_candidate_pool(config, "机械鸭", pool_size=40, run_id="b", deps=_deps(tmp_path))

    assert [candidate.to_dict() for candidate in solo["candidates"]] == [
        candidate.to_dict() for candidate in legacy["candidates"]
    ]
    for key, value in legacy.items():
        if key == "search_report":
            continue
        assert solo[key] == value, key
    assert set(solo["search_report"]) - set(legacy["search_report"]) == {"sources"}
    assert {k: v for k, v in solo["search_report"].items() if k != "sources"} == legacy["search_report"]
    assert [record["source"] for record in solo["sources"]] == ["douyin"]
