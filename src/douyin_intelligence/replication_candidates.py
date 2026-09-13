"""Theme-driven candidate-pool collection and normalization.

The download URL captured from the crawler is a signed, sensitive value: the
sanitizer removes it from the on-disk records, so it is captured in memory via
``collect_search(before_sanitize=...)`` and never written to
``candidate_pool.json``.  Only ``media_url_present`` survives to disk.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from .normalize import load_raw_records, normalize_record, parse_count
from .replication_delivery import FOLDER_PROCESS
from .replication_theme import expand_keywords

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from .replication_pipeline import ReplicationDeps


# The crawler enforces ``keywords[:budget // 10]`` (see ``search_collector``),
# so the expanded keyword list can be shorter after collection than before.
# The shortfall warning must say which of the two the pool actually reflects.
SEARCH_REPORT_NAME = "search_report.json"


def _engagement(candidate: "Candidate") -> float:
    """Interaction-weighted engagement, tolerating a missing ``play_count``."""
    value = (
        (candidate.digg_count or 0)
        + 3 * (candidate.comment_count or 0)
        + 5 * (candidate.share_count or 0)
        + 4 * (candidate.collect_count or 0)
    )
    if candidate.play_count:
        value += 0.05 * candidate.play_count
    return float(value)


@dataclass(slots=True)
class Candidate:
    """A normalized search candidate (no signed URL is ever stored here)."""

    video_id: str
    title: str = ""
    author: str = ""
    author_hash: str = ""
    source_url: str = ""
    published_at: str = ""
    digg_count: int = 0
    comment_count: int = 0
    share_count: int = 0
    collect_count: int = 0
    play_count: int | None = None
    heat_score: float = 0.0
    heat_rank: int = 0
    duration_seconds: float = 0.0
    #: Which source field the duration came from (e.g. ``"video.duration_ms"``),
    #: so a future unit regression is attributable.  Empty when unknown.
    duration_source: str = ""
    source_keyword: str = ""
    media_url_present: bool = False
    aweme_type: str = ""
    media_is_audio: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Candidate duration field paths, in priority order.  Douyin's raw ``aweme`` JSON
# nests the length under ``video`` (and, in some exports, ``aweme_detail.video``)
# and expresses it in **milliseconds**; our own flattened rows use ``duration``
# in seconds.  ``_row_duration_detail`` understands both rather than silently
# reporting 0 -- which is what let the 10~300 s window become a no-op.
_SECONDS_PATHS = (
    "duration", "duration_seconds", "video_duration", "video_duration_seconds",
)
_MILLISECOND_PATHS = (
    "duration_ms", "video_duration_ms",
    "video.duration", "aweme_detail.video.duration", "aweme_detail.duration",
)
#: A seconds-typed value larger than this is really milliseconds (no Douyin clip
#: is 10 000 s long); the value is divided by 1000 and the source is tagged.
_DURATION_MS_THRESHOLD = 10_000


def _dig(row: dict[str, Any], path: str) -> Any:
    current: Any = row
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _row_duration_detail(row: dict[str, Any]) -> tuple[float, str]:
    """Return ``(duration_seconds, source)`` for a raw crawler row.

    Understands top-level seconds keys, nested ``video.duration`` (milliseconds),
    and explicit ``*_ms`` keys.  Unknown -> ``(0.0, "")``.
    """
    for path in _MILLISECOND_PATHS:
        value = parse_count(_dig(row, path))
        if value is not None and value > 0:
            source = path if path.endswith("_ms") else f"{path}_ms"
            return round(float(value) / 1000.0, 3), source
    for path in _SECONDS_PATHS:
        value = parse_count(_dig(row, path))
        if value is not None and value > 0:
            if value > _DURATION_MS_THRESHOLD:
                # An obviously-millisecond value under a seconds-named key: keep
                # the reader honest about the unit instead of reporting 1000×.
                return round(float(value) / 1000.0, 3), f"{path}_ms"
            return float(value), path
    return 0.0, ""


def _row_duration(row: dict[str, Any]) -> float:
    return _row_duration_detail(row)[0]


def _row_media_url(row: dict[str, Any]) -> str:
    for key in ("video_download_url", "video_url", "download_addr", "play_addr"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def looks_like_audio_url(url: str) -> bool:
    """True when a crawler ``video_download_url`` actually points at an audio asset.

    Douyin image-album posts (``aweme_type=68``) carry no video stream at all.  The
    upstream extractor then falls back to the post's background music, which is served
    from an ``ies-music`` path or as a bare ``.mp3``.  Downloading such a URL produces an
    MP3 (ID3) file, which the pipeline would otherwise only discover *after* spending
    bandwidth and reporting a misleading "invalid media" reason.
    """
    lowered = str(url or "").strip().lower()
    if not lowered:
        return False
    if lowered.endswith(".mp3"):
        return True
    return "ies-music" in lowered or "/music/" in lowered


def compute_heat_scores(candidates: list[Candidate]) -> None:
    """Normalize ``heat_score`` into ``[0, 1]`` and assign deterministic ranks."""
    if not candidates:
        return
    engagements = [_engagement(candidate) for candidate in candidates]
    pool_max = max(engagements)
    for candidate, engagement in zip(candidates, engagements):
        candidate.heat_score = round(engagement / pool_max, 6) if pool_max > 0 else 0.0
    order = sorted(
        range(len(candidates)),
        key=lambda index: (
            -candidates[index].heat_score,
            -candidates[index].duration_seconds,
            candidates[index].video_id,
        ),
    )
    for rank, index in enumerate(order, 1):
        candidates[index].heat_rank = rank


def normalize_candidates(raw_rows: list[dict[str, Any]], config: dict[str, Any], *, keywords: list[str]) -> list[Candidate]:
    """Normalize raw crawler rows into deduplicated ``Candidate`` records."""
    seen: set[str] = set()
    result: list[Candidate] = []
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        try:
            record = normalize_record(
                row,
                source="douyin_search",
                timezone_name=str(config.get("timezone") or "Asia/Shanghai"),
                categories=config.get("categories") or {},
            )
        except (ValueError, KeyError):
            continue
        if not record.video_id or record.video_id in seen:
            continue
        seen.add(record.video_id)
        author = record.account_name or record.account_id or "unknown"
        author_hash = str(row.get("creator_hash") or row.get("author_hash") or record.account_id or "")
        duration_seconds, duration_source = _row_duration_detail(row)
        if duration_seconds <= 0:
            # No usable duration: the source label must not survive on its own.
            # Otherwise a row could report ``duration_seconds=0`` while a
            # non-empty ``duration_source`` implies "we did read a duration" --
            # exactly the misleading pair a reader must never see.
            duration_seconds, duration_source = 0.0, ""
        result.append(
            Candidate(
                video_id=record.video_id,
                title=record.title,
                author=author,
                author_hash=author_hash,
                source_url=record.share_url,
                published_at=record.published_at or "",
                digg_count=int(record.digg_count or 0),
                comment_count=int(record.comment_count or 0),
                share_count=int(record.share_count or 0),
                collect_count=int(record.collect_count or 0),
                play_count=record.play_count,
                duration_seconds=duration_seconds,
                duration_source=duration_source,
                source_keyword=record.source_keyword,
                media_url_present=bool(_row_media_url(row)),
                aweme_type=str(row.get("aweme_type") or ""),
                media_is_audio=looks_like_audio_url(_row_media_url(row)),
            )
        )
    return result


def media_url_map(raw_rows: list[dict[str, Any]]) -> dict[str, str]:
    """Capture the signed download URLs in memory, keyed by ``video_id``."""
    result: dict[str, str] = {}
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        video_id = str(row.get("aweme_id") or row.get("video_id") or "").strip()
        url = _row_media_url(row)
        if video_id and url and video_id not in result:
            result[video_id] = url
    return result


def _searched_keywords(report: dict[str, Any], requested: list[str]) -> list[str]:
    """The keywords the crawler was actually asked to search.

    ``collect_search`` truncates to ``budget // 10`` keywords before running and
    reports that truncated list, so ``report["keywords"]`` -- not the expanded
    ``requested`` list -- is the truthful "used" set.  When a report carries no
    keyword list (e.g. the synthetic failure report) fall back to ``requested``
    so no information is silently dropped.
    """
    reported = report.get("keywords")
    if isinstance(reported, list):
        return [str(value) for value in reported]
    return list(requested)


def _pool_shortfall_warning(
    pool_size: int,
    min_pool: int,
    requested_count: int,
    used_count: int,
    report: dict[str, Any],
) -> str:
    """Attributable "pool too small" warning readable straight from the delivery.

    Says whether the shortfall came from *too few keywords* (a truncation the
    crawler applied) or simply *too little matching content*, and points at the
    ``search_report.json`` that backs the claim.
    """
    truncated = requested_count > used_count
    coverage = f"实际搜索关键词 {used_count} 个 / 请求 {requested_count} 个"
    if str(report.get("status") or "") == "failed":
        coverage += "（候选池采集未成功，未取得有效关键词覆盖）"
    elif truncated:
        coverage += "（发生关键词截断）"
    else:
        coverage += "（未发生关键词截断）"
    parts = [f"候选池规模 {pool_size} 低于最小目标 {min_pool}：{coverage}"]
    details: list[str] = []
    if report.get("per_keyword_budget") is not None:
        details.append(f"per_keyword_budget={report['per_keyword_budget']}")
    if report.get("raw_request_ceiling") is not None:
        details.append(f"raw_request_ceiling={report['raw_request_ceiling']}")
    if details:
        parts.append("；".join(details))
    parts.append(f"详见 {FOLDER_PROCESS}/{SEARCH_REPORT_NAME}")
    return "；".join(parts)


def _keyword_truncation_warning(requested_count: int, used_count: int) -> str:
    """Say a keyword cut happened even though the *pool* was large enough.

    The shortfall warning only fires when the pool is too small; a large pool can
    still have searched far fewer keywords than requested (the crawler takes only
    ``budget // 10`` of them), which silently narrows theme coverage.  This note
    makes that cut visible on every affected run, not just undersized ones.
    """
    return (
        f"关键词覆盖：请求 {requested_count} 个词、实际搜索 {used_count} 个（发生截断）："
        f"爬虫每次搜索按 候选池规模 // 10 取词；如需完整覆盖，可提高候选池规模，"
        f"或设置 jobs.material_replication.search.min_searched_keywords。"
        f"详见 {FOLDER_PROCESS}/{SEARCH_REPORT_NAME}"
    )


def _planned_searched_keywords(budget: int) -> int:
    """Keywords the crawler will search for a given pool ``budget`` (``// 10``)."""
    return max(0, int(budget) // 10)


def collect_candidate_pool(
    config: dict[str, Any],
    theme: str,
    *,
    pool_size: int,
    run_id: str | None = None,
    deps: "ReplicationDeps | None" = None,
) -> dict[str, Any]:
    """Collect and normalize a themed candidate pool without persisting media URLs.

    ``jobs.material_replication.search.min_searched_keywords`` (optional, unset by
    default) raises the pool ``budget`` so the crawler's ``budget // 10`` keyword
    slice reaches that many keywords -- never above ``max_pool_size``; when the cap
    prevents it a warning says so.  Unset, the budget is exactly as before.
    """
    settings = (config.get("jobs") or {}).get("material_replication") or {}
    min_pool = int(settings.get("min_pool_size") or 40)
    max_pool = int(settings.get("max_pool_size") or 120)
    budget = max(1, min(int(pool_size), max_pool))
    budget_warnings: list[str] = []
    raw_min_searched = (settings.get("search") or {}).get("min_searched_keywords")
    if raw_min_searched is not None:
        try:
            min_searched = int(raw_min_searched)
        except (TypeError, ValueError):
            min_searched = 0
        if min_searched > 0 and _planned_searched_keywords(budget) < min_searched:
            needed = min_searched * 10
            if needed > budget:
                budget = min(needed, max_pool)
            if _planned_searched_keywords(budget) < min_searched:
                budget_warnings.append(
                    f"受 max_pool_size（{max_pool}）限制：覆盖 {min_searched} 个搜索词需候选池规模 ≥ {needed}，"
                    f"当前候选池规模为 {budget}，实际最多搜索 {_planned_searched_keywords(budget)} 个词"
                )
    # ``jobs.material_replication.search.publish_time_type`` (optional, unset by
    # default) overrides the MediaCrawler publish-time window.  Only a present,
    # int-parseable value is forwarded; when the key is absent the collector is
    # invoked exactly as before (no extra kwarg), so injected test doubles keep
    # their historical signature and the default run stays byte-identical.
    raw_publish_time = (settings.get("search") or {}).get("publish_time_type")
    publish_time_type: int | None = None
    if raw_publish_time is not None:
        try:
            publish_time_type = int(raw_publish_time)
        except (TypeError, ValueError):
            publish_time_type = None
    keywords = expand_keywords(theme, config)
    raw_rows: list[dict[str, Any]] = []

    def capture(files: list[Any]) -> None:
        for path in files:
            try:
                raw_rows.extend(load_raw_records(path))
            except (OSError, ValueError):
                continue

    collector = getattr(deps, "collector", None) if deps is not None else None
    if collector is None:
        from .search_collector import collect_search
        collector = collect_search
    collector_kwargs: dict[str, Any] = {}
    if publish_time_type is not None:
        collector_kwargs["publish_time_type"] = publish_time_type
    try:
        report = collector(
            config, budget, run_id=run_id, keywords=keywords, hard_max=budget, before_sanitize=capture,
            **collector_kwargs,
        )
    except Exception as exc:  # A failed collection only degrades this stage.
        # Nothing ran, so report an empty *searched* keyword set rather than
        # echoing the requested list back as if it had produced results.
        report = {"status": "failed", "error": str(exc)[:300], "keywords": [], "budget": budget}

    requested = list(keywords)
    searched = _searched_keywords(report, requested)
    candidates = normalize_candidates(raw_rows, config, keywords=requested)
    compute_heat_scores(candidates)
    collection_failed = str(report.get("status") or "") == "failed"
    warnings: list[str] = list(budget_warnings)
    shortfall = len(candidates) < min_pool
    if shortfall:
        warnings.append(_pool_shortfall_warning(len(candidates), min_pool, len(requested), len(searched), report))
    if collection_failed:
        warnings.append("候选池采集未成功，仅使用已捕获的记录")
    elif len(requested) > len(searched) and not shortfall:
        # The shortfall warning already states the truncation when the pool is
        # undersized; this surfaces it for a pool that cleared ``min_pool`` too.
        warnings.append(_keyword_truncation_warning(len(requested), len(searched)))
    status = "success" if candidates and not collection_failed else "partial" if candidates else "failed"
    return {
        "status": status,
        "theme": theme,
        # ``keywords`` stays the list of keywords actually searched so existing
        # consumers cannot over-claim coverage; ``keywords_requested`` carries
        # the full expansion for attribution.
        "keywords": searched,
        "keywords_used": searched,
        "keywords_requested": requested,
        "keywords_truncated": len(requested) > len(searched),
        "budget": budget,
        "min_pool_size": min_pool,
        "max_pool_size": max_pool,
        "candidates": candidates,
        "media_urls": media_url_map(raw_rows),
        "candidate_pool": {
            "schema_version": 1,
            "theme": theme,
            "keywords": searched,
            "keywords_requested": requested,
            "pool_size": len(candidates),
            "candidates": [candidate.to_dict() for candidate in candidates],
        },
        "search_report": report,
        "warnings": warnings,
    }
