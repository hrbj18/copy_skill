"""Offline tests for :class:`BilibiliSource`.

Everything runs against an injected fake ``fetcher`` (no network) and a recording
``sleeper`` (no real sleeping), so the suite is fast and deterministic.  The
tests pin:

* the ``wbi`` signature (a pure function -- if it is wrong everything is wrong);
* the ``-101`` ``/nav`` trap (the keys must be read from a *guest* response);
* mapping/stripping of the search results;
* the 412 back-off path (retry, then degrade to ``warnings`` -- never raise);
* the disk-safety whitelist (no URL / ``wbi`` key / cookie ever reaches the
  report);
* the lazy per-candidate resolution hit/miss.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable

import pytest

from douyin_intelligence import sources
from douyin_intelligence.config import load_config
from douyin_intelligence.replication_candidates import Candidate
from douyin_intelligence.sources.base import MediaResolutionError
from douyin_intelligence.sources.bilibili import (
    CONTENT_UNAVAILABLE_API_CODES,
    BilibiliSource,
    compute_w_rid,
    mixin_key_from,
)
from douyin_intelligence.sources.ytdlp import SENSITIVE_REPORT_KEYS


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
IMG_KEY = "7cd084941338484aae1ad9425b84077c"
SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"
MIXIN_KEY = "ea1db124af3c7062474693fa704f4ff8"  # well-known bilibili example salt

HOME_FRAG = "www.bilibili.com/"
NAV_FRAG = "/x/web-interface/nav"
SEARCH_FRAG = "/x/web-interface/wbi/search/type"
VIEW_FRAG = "/x/web-interface/view"
PLAYURL_FRAG = "/x/player/wbi/playurl"


def _nav_payload(code: int = 0) -> dict[str, Any]:
    return {
        "code": code,
        "data": {
            "wbi_img": {
                "img_url": f"https://i0.hdslb.com/bfs/wbi/{IMG_KEY}.png",
                "sub_url": f"https://i0.hdslb.com/bfs/wbi/{SUB_KEY}.png",
            }
        },
    }


def _item(
    bvid: str,
    title: str,
    author: str = "某个UP主",
    pubdate: int = 1788852600,
    play: int = 53294,
    duration: str = "05:12",
    mid: int = 123,
    like: int = 1200,
) -> dict[str, Any]:
    return {
        "bvid": bvid,
        "aid": 117233732815841,
        "title": title,
        "author": author,
        "mid": mid,
        "pubdate": pubdate,
        "play": play,
        "like": like,
        "duration": duration,
        "pic": "https://i0.hdslb.com/bfs/archive/cover.jpg",
    }


def _search_payload(items: list[dict[str, Any]], num_results: int = 1000) -> dict[str, Any]:
    return {"code": 0, "data": {"numResults": num_results, "result": items}}


class _FakeFetcher:
    """A route-based fake matching the ``fetcher(url, headers, timeout)`` seam."""

    def __init__(self, routes: list[tuple[str, Any]]) -> None:
        self._routes = routes
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, Any]:
        index = len(self.calls)
        self.calls.append(url)
        for fragment, response in self._routes:
            if fragment in url:
                return response(url, index) if callable(response) else response
        return 404, None

    def calls_for(self, fragment: str) -> list[str]:
        return [url for url in self.calls if fragment in url]


class _Sleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(float(seconds))


def _keyword_from(url: str) -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return query.get("keyword", [""])[0]


def _search_handler(by_keyword: dict[str, list[dict[str, Any]]]) -> Callable[..., tuple[int, Any]]:
    def handler(url: str, index: int) -> tuple[int, Any]:
        return 200, _search_payload(by_keyword.get(_keyword_from(url), []))

    return handler


def _make_source(routes: list[tuple[str, Any]]) -> tuple[BilibiliSource, _FakeFetcher, _Sleeper]:
    fetcher = _FakeFetcher(routes)
    sleeper = _Sleeper()
    source = BilibiliSource(fetcher=fetcher, sleeper=sleeper)
    return source, fetcher, sleeper


def _walk_strings(obj: Any):
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _walk_strings(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_strings(item)
    elif isinstance(obj, str):
        yield obj


def _walk_keys(obj: Any):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield str(key)
            yield from _walk_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


# --------------------------------------------------------------------------- #
# Pure wbi signature
# --------------------------------------------------------------------------- #
def test_mixin_key_from_known_keys() -> None:
    assert mixin_key_from(IMG_KEY, SUB_KEY) == MIXIN_KEY
    # Missing keys -> "" (never a half-salt that would silently sign wrongly).
    assert mixin_key_from("", SUB_KEY) == ""
    assert mixin_key_from(IMG_KEY, "") == ""


def test_compute_w_rid_is_a_stable_pure_function() -> None:
    """A wrong ``w_rid`` breaks every request, so it is pinned to a fixed digest."""
    search_params = {
        "search_type": "video",
        "keyword": "Microduck",
        "page": 1,
        "page_size": 20,
        "order": "totalrank",
        "platform": "pc",
        "wts": 1700000000,
    }
    assert compute_w_rid(search_params, MIXIN_KEY) == "10811e0e1aebb2e9d18d13894352a12e"

    playurl_params = {
        "bvid": "BV1uUbG6FEfb",
        "cid": 41685224881,
        "qn": 32,
        "fnval": 1,
        "fourk": 0,
        "wts": 1700000000,
    }
    assert compute_w_rid(playurl_params, MIXIN_KEY) == "5abd9d2dda739511c16f82d5039134aa"

    # An incoming ``w_rid`` is ignored, so re-signing is idempotent.
    assert compute_w_rid({**search_params, "w_rid": "stale"}, MIXIN_KEY) == compute_w_rid(
        search_params, MIXIN_KEY
    )


# --------------------------------------------------------------------------- #
# search()
# --------------------------------------------------------------------------- #
def test_search_maps_candidates_and_strips_highlight() -> None:
    keywords = ["Microduck", "机器鸭", "具身智能"]
    by_keyword = {
        "Microduck": [
            _item("BV1uUbG6FEfb", '手搓<em class="keyword">microduck</em>保姆级教程'),
            _item("BV1U2tV6xEru", '【开源预告】<em class="keyword">Microduck</em>复刻方案'),
        ],
        "机器鸭": [_item("BV1R7Yi6GEg7", '<em class="keyword">Microduck</em>机器鸭')],
        "具身智能": [_item("BV1R2tH6tEsK", '具身智能硬件架构')],
    }
    source, fetcher, sleeper = _make_source(
        [(HOME_FRAG, (200, {"code": 0})), (NAV_FRAG, (200, _nav_payload(0))),
         (SEARCH_FRAG, _search_handler(by_keyword))]
    )

    result = source.search(keywords, 40, config=load_config(), run_id="run-1")

    assert result.source == "bilibili"  # the source of truth for provenance
    assert result.status == "success"
    assert result.keywords_requested == keywords
    assert result.keywords_used == keywords
    # 2 + 1 + 1 items, and ``budget // 10 == 4`` is enough to keep all of them.
    assert len(result.candidates) == 4

    first = next(c for c in result.candidates if c.video_id == "BV1uUbG6FEfb")
    assert first.title == "手搓microduck保姆级教程"  # the <em> tags are gone
    assert "<em" not in first.title and "</em>" not in first.title
    assert first.source_url == "https://www.bilibili.com/video/BV1uUbG6FEfb"
    assert first.source_keyword == "Microduck"
    assert first.author == "某个UP主"
    assert first.author_hash == "123"
    assert first.play_count == 53294
    assert first.digg_count == 1200
    assert first.duration_seconds == 312.0  # "05:12"
    assert first.duration_source == "bilibili.duration"
    assert first.published_at == datetime.fromtimestamp(1788852600, tz=timezone.utc).isoformat()

    assert len(fetcher.calls_for(SEARCH_FRAG)) == 3
    assert sleeper.calls  # requests were throttled


def test_nav_minus_101_still_yields_wbi_keys() -> None:
    """Regression: a guest ``code=-101`` /nav must NOT abort (the MediaCrawler trap)."""
    by_keyword = {"Microduck": [_item("BV1uUbG6FEfb", "Microduck 视频")]}
    source, _fetcher, _sleeper = _make_source(
        [(HOME_FRAG, (200, {"code": 0})), (NAV_FRAG, (200, _nav_payload(-101))),
         (SEARCH_FRAG, _search_handler(by_keyword))]
    )

    result = source.search(["Microduck"], 40, config=load_config())

    assert result.status == "success"
    assert [c.video_id for c in result.candidates] == ["BV1uUbG6FEfb"]


def test_retry_on_412_then_success() -> None:
    calls = {"search": 0}

    def flaky(url: str, index: int) -> tuple[int, Any]:
        calls["search"] += 1
        if calls["search"] == 1:
            return 412, None  # risk control rejects the first attempt
        return 200, _search_payload([_item("BV1uUbG6FEfb", "Microduck 视频")])

    source, fetcher, sleeper = _make_source(
        [(HOME_FRAG, (200, {"code": 0})), (NAV_FRAG, (200, _nav_payload(0))),
         (SEARCH_FRAG, flaky)]
    )

    result = source.search(["Microduck"], 40, config=load_config())

    assert result.status == "success"
    assert [c.video_id for c in result.candidates] == ["BV1uUbG6FEfb"]
    assert len(fetcher.calls_for(SEARCH_FRAG)) == 2  # retried once
    assert any(seconds == 6.0 for seconds in sleeper.calls)  # 3 * attempt(2)


def test_retries_exhausted_records_warning_and_returns() -> None:
    """A hard-412 keyword must degrade to a warning, never raise through."""
    source, fetcher, sleeper = _make_source(
        [(HOME_FRAG, (200, {"code": 0})), (NAV_FRAG, (200, _nav_payload(0))),
         (SEARCH_FRAG, (412, None))]
    )

    result = source.search(["无人问津的关键词"], 40, config=load_config())

    assert result.status == "failed"  # every keyword failed -> whole-source failure
    assert result.error  # an error string is set
    assert result.candidates == []
    assert any("无人问津的关键词" in warning for warning in result.warnings)
    assert result.report["failed_keywords"] == ["无人问津的关键词"]
    # The retry budget was spent (4 attempts), the source still returned.
    assert len(fetcher.calls_for(SEARCH_FRAG)) == 4
    assert sleeper.calls


def test_keyword_truncation_is_reported() -> None:
    keywords = [f"kw{i}" for i in range(10)]  # more than the request guard allows
    source, _fetcher, _sleeper = _make_source(
        [(HOME_FRAG, (200, {"code": 0})), (NAV_FRAG, (200, _nav_payload(0))),
         (SEARCH_FRAG, (200, _search_payload([])))]
    )

    result = source.search(keywords, 40, config=load_config())

    assert result.status == "no_match"  # requests succeeded, zero hits
    assert len(result.keywords_used) == 8
    assert result.report["keywords_truncated"] == ["kw8", "kw9"]
    assert any("关键词截断" in warning for warning in result.warnings)


def test_empty_keywords_skip_the_network() -> None:
    source, fetcher, _sleeper = _make_source([(HOME_FRAG, (200, {"code": 0}))])
    result = source.search([], 40, config=load_config())

    assert result.status == "empty"
    assert fetcher.calls == []


def test_report_never_carries_urls_or_keys() -> None:
    # A hostile payload tries to smuggle a URL / wbi key through the search items.
    hostile = _item("BV1uUbG6FEfb", "Microduck 视频")
    hostile["url"] = "https://signed.example/stream.mp4?token=SECRET"
    hostile["wbi"] = "should-not-leak"
    source, _fetcher, _sleeper = _make_source(
        [(HOME_FRAG, (200, {"code": 0})), (NAV_FRAG, (200, _nav_payload(0))),
         (SEARCH_FRAG, (200, _search_payload([hostile])))]
    )

    result = source.search(["Microduck"], 40, config=load_config())
    blob = json.dumps(result.report, ensure_ascii=False)

    assert "http" not in blob
    assert "wbi" not in blob
    assert "SECRET" not in blob
    assert all("http" not in value for value in _walk_strings(result.report))
    assert set(_walk_keys(result.report)).isdisjoint(SENSITIVE_REPORT_KEYS)
    # The MediaCrawler salt never appears either.
    assert MIXIN_KEY not in blob


# --------------------------------------------------------------------------- #
# resolve_media_url()
# --------------------------------------------------------------------------- #
def test_resolve_media_url_hit() -> None:
    source, fetcher, _sleeper = _make_source(
        [
            (HOME_FRAG, (200, {"code": 0})),
            (NAV_FRAG, (200, _nav_payload(0))),
            (VIEW_FRAG, (200, {"code": 0, "data": {"cid": 41685224881}})),
            (
                PLAYURL_FRAG,
                (200, {"code": 0, "data": {"durl": [{"url": "https://cdn.example/s.mp4?e=SIGNED"}]}}),
            ),
        ]
    )

    url = source.resolve_media_url(Candidate(video_id="BV1uUbG6FEfb"))

    assert url == "https://cdn.example/s.mp4?e=SIGNED"
    assert fetcher.calls_for(VIEW_FRAG)  # cid was fetched first
    assert fetcher.calls_for(PLAYURL_FRAG)


def test_resolve_media_url_miss_returns_empty() -> None:
    # No cid in the view response -> "" and no playurl call.
    source, fetcher, _sleeper = _make_source(
        [(HOME_FRAG, (200, {"code": 0})), (NAV_FRAG, (200, _nav_payload(0))),
         (VIEW_FRAG, (200, {"code": 0, "data": {}}))]
    )
    assert source.resolve_media_url(Candidate(video_id="BVmissing")) == ""
    assert not fetcher.calls_for(PLAYURL_FRAG)

    # Empty id -> "" without any request at all.
    source2, fetcher2, _sleeper2 = _make_source([(HOME_FRAG, (200, {"code": 0}))])
    assert source2.resolve_media_url(Candidate(video_id="")) == ""
    assert fetcher2.calls == []


def test_resolve_code_zero_but_empty_stream_is_a_miss() -> None:
    # code=0 but no durl/dash -> "no usable media" -> "" (NOT an error).
    source, _fetcher, _sleeper = _make_source(
        [
            (HOME_FRAG, (200, {"code": 0})),
            (NAV_FRAG, (200, _nav_payload(0))),
            (VIEW_FRAG, (200, {"code": 0, "data": {"cid": 41685224881}})),
            (PLAYURL_FRAG, (200, {"code": 0, "data": {"durl": []}})),
        ]
    )
    assert source.resolve_media_url(Candidate(video_id="BV1uUbG6FEfb")) == ""


def test_resolve_raises_on_view_transport_failure() -> None:
    # 412 retries exhausted on /view -> a *source* error, not an empty miss.
    source, _fetcher, _sleeper = _make_source(
        [(HOME_FRAG, (200, {"code": 0})), (NAV_FRAG, (200, _nav_payload(0))),
         (VIEW_FRAG, (412, None))]
    )
    with pytest.raises(MediaResolutionError):
        source.resolve_media_url(Candidate(video_id="BV1uUbG6FEfb"))


def test_resolve_raises_on_view_rate_limit_code() -> None:
    # A channel-level code on /view (e.g. -412 intercepted) -> raise.
    source, _fetcher, _sleeper = _make_source(
        [(HOME_FRAG, (200, {"code": 0})), (NAV_FRAG, (200, _nav_payload(0))),
         (VIEW_FRAG, (200, {"code": -412, "message": "请求被拦截"}))]
    )
    with pytest.raises(MediaResolutionError):
        source.resolve_media_url(Candidate(video_id="BV1uUbG6FEfb"))


def test_resolve_raises_on_playurl_transport_failure() -> None:
    source, _fetcher, _sleeper = _make_source(
        [(HOME_FRAG, (200, {"code": 0})), (NAV_FRAG, (200, _nav_payload(0))),
         (VIEW_FRAG, (200, {"code": 0, "data": {"cid": 41685224881}})),
         (PLAYURL_FRAG, (412, None))]
    )
    with pytest.raises(MediaResolutionError):
        source.resolve_media_url(Candidate(video_id="BV1uUbG6FEfb"))


def test_resolve_raises_on_playurl_unknown_code() -> None:
    # An unknown non-zero code is treated as a channel failure -> raise.
    source, _fetcher, _sleeper = _make_source(
        [(HOME_FRAG, (200, {"code": 0})), (NAV_FRAG, (200, _nav_payload(0))),
         (VIEW_FRAG, (200, {"code": 0, "data": {"cid": 41685224881}})),
         (PLAYURL_FRAG, (200, {"code": 12345, "message": "未知错误"}))]
    )
    with pytest.raises(MediaResolutionError):
        source.resolve_media_url(Candidate(video_id="BV1uUbG6FEfb"))


@pytest.mark.parametrize("code", sorted(CONTENT_UNAVAILABLE_API_CODES))
def test_resolve_content_level_code_returns_empty(code: int) -> None:
    # A content-level code (deleted / private / paid ...) is a normal miss: ""
    # and NOT an exception, whichever endpoint reports it.
    source, _fetcher, _sleeper = _make_source(
        [(HOME_FRAG, (200, {"code": 0})), (NAV_FRAG, (200, _nav_payload(0))),
         (VIEW_FRAG, (200, {"code": code, "message": "内容不可用"}))]
    )
    assert source.resolve_media_url(Candidate(video_id="BVgone")) == ""


def test_resolve_content_level_playurl_code_returns_empty() -> None:
    # A valid cid but a content-level playurl code (87007 充电专属) -> "".
    source, _fetcher, _sleeper = _make_source(
        [(HOME_FRAG, (200, {"code": 0})), (NAV_FRAG, (200, _nav_payload(0))),
         (VIEW_FRAG, (200, {"code": 0, "data": {"cid": 41685224881}})),
         (PLAYURL_FRAG, (200, {"code": 87007, "message": "充电专属视频"}))]
    )
    assert source.resolve_media_url(Candidate(video_id="BV1uUbG6FEfb")) == ""


def test_resolve_rate_limit_playurl_code_raises() -> None:
    # -509 请求超频 -> channel-level -> raise.
    source, _fetcher, _sleeper = _make_source(
        [(HOME_FRAG, (200, {"code": 0})), (NAV_FRAG, (200, _nav_payload(0))),
         (VIEW_FRAG, (200, {"code": 0, "data": {"cid": 41685224881}})),
         (PLAYURL_FRAG, (200, {"code": -509, "message": "请求超频"}))]
    )
    with pytest.raises(MediaResolutionError):
        source.resolve_media_url(Candidate(video_id="BV1uUbG6FEfb"))


# --------------------------------------------------------------------------- #
# Registration / config no-op
# --------------------------------------------------------------------------- #
def test_registered_in_package() -> None:
    assert "bilibili" in sources.known_sources()
    adapter = sources.get_source("bilibili")
    assert isinstance(adapter, BilibiliSource)
    assert adapter.name == "bilibili"
    assert adapter.download_referer == "https://www.bilibili.com/"


def test_absent_sources_key_stays_a_noop() -> None:
    # Registering bilibili must not inject anything into a config that never
    # named a ``sources`` key -- the no-op guarantee is unchanged.
    payload = load_config()
    section = payload["jobs"]["material_replication"]
    assert "sources" not in section
    assert "source_budgets" not in section
    assert "source_gate" not in section
