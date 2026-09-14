"""Douyin source adapter.

This is a thin **wrapper** around the existing search pipeline -- it deliberately
does not re-implement any collection logic.  It reuses:

* :func:`douyin_intelligence.search_collector.collect_search` for discovery;
* :func:`douyin_intelligence.replication_candidates.normalize_candidates` /
  ``compute_heat_scores`` / ``media_url_map`` for normalization.

The security-critical part is inherited verbatim: the crawler's
``video_download_url`` is a *signed* value that the on-disk sanitizer strips.
The adapter captures it in memory only, through the ``before_sanitize`` callback,
into :attr:`DouyinSource._media_urls`; a signed URL therefore never reaches
:attr:`SourceResult.candidates`, :attr:`SourceResult.report`, or the disk.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Sequence

from .base import SourceResult

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from ..replication_candidates import Candidate


#: Disk-safe keys copied from ``collect_search``'s report.  A whitelist (rather
#: than ``dict(report)``) is used so that a future field added upstream cannot
#: silently leak a signed URL into the adapter's ``SourceResult``.
_CRAWL_REPORT_KEYS = (
    "status",
    "run_dir",
    "budget",
    "publish_time_type",
    "keywords",
    "per_keyword_budget",
    "raw_request_ceiling",
    "files",
    "returncode",
    "timeout_seconds",
    "error",
    "output_observation",
    "sanitization",
    "browser",
)


def _whitelist_report(report: dict[str, Any]) -> dict[str, Any]:
    return {key: report[key] for key in _CRAWL_REPORT_KEYS if key in report}


class DouyinSource:
    """A :class:`~douyin_intelligence.sources.base.SourceAdapter` over the Douyin crawler."""

    name = "douyin"
    #: Douyin's CDN rejects a missing/foreign referer, so downloads must keep the
    #: historic header -- see :class:`../sources.base.DownloadTarget`.
    download_referer = "https://www.douyin.com/"

    def __init__(self, *, collector: Callable[..., dict[str, Any]] | None = None) -> None:
        """Create the adapter.

        Args:
            collector: test seam -- an object callable with the same signature as
                :func:`douyin_intelligence.search_collector.collect_search`.
                When ``None`` the real collector is imported lazily on first use.
        """
        self._collector = collector
        #: ``video_id -> signed download URL``, captured in memory only.  Never
        #: written to a :class:`SourceResult`.
        self._media_urls: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def search(
        self,
        keywords: Sequence[str],
        budget: int,
        *,
        config: dict[str, Any],
        run_id: str | None = None,
    ) -> SourceResult:
        requested = [str(item).strip() for item in (keywords or []) if str(item).strip()]
        call_budget = max(1, int(budget or 0))
        if not requested:
            return SourceResult(
                source=self.name,
                status="empty",
                candidates=[],
                keywords_requested=[],
                keywords_used=[],
                report={"status": "empty", "keywords": [], "budget": call_budget},
                warnings=["未提供关键词，未执行搜索"],
            )

        # ``publish_time_type`` is forwarded only when present and int-parseable,
        # exactly as ``replication_candidates.collect_candidate_pool`` does, so an
        # injected test double keeps its historical signature and the default run
        # is byte-identical.
        publisher_kwargs = self._publish_time_kwargs(config)

        raw_rows: list[dict[str, Any]] = []

        def capture(files: list[Any]) -> None:
            from ..normalize import load_raw_records

            for path in files:
                try:
                    raw_rows.extend(load_raw_records(path))
                except (OSError, ValueError):
                    continue

        collector = self._collector
        if collector is None:
            from ..search_collector import collect_search

            collector = collect_search

        try:
            report = collector(
                config,
                call_budget,
                run_id=run_id,
                keywords=requested,
                hard_max=call_budget,
                before_sanitize=capture,
                **publisher_kwargs,
            )
        except Exception as exc:  # a failed collection only degrades this source
            return SourceResult(
                source=self.name,
                status="failed",
                candidates=[],
                keywords_requested=requested,
                keywords_used=[],
                report={"status": "failed", "error": str(exc)[:300], "keywords": [], "budget": call_budget},
                warnings=[f"抖音采集未成功：{type(exc).__name__}: {str(exc)[:200]}"],
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
            )

        if not isinstance(report, dict):
            report = {"status": "failed", "error": "采集返回非对象报告", "keywords": [], "budget": call_budget}

        candidates = self._normalize(raw_rows, config, requested)
        searched = self._searched_keywords(report, requested)
        # Capture the signed URLs in memory *after* normalization; they live only
        # in ``self._media_urls`` and are handed out one candidate at a time.
        self._media_urls = self._capture_media_urls(raw_rows)

        crawl_status = str(report.get("status") or "")
        collection_failed = crawl_status == "failed"
        if collection_failed:
            status = "failed"
        elif candidates:
            status = "success"
        elif crawl_status == "empty":
            status = "empty"
        else:
            status = "no_match"

        warnings: list[str] = []
        if not collection_failed and len(requested) > len(searched):
            warnings.append(
                f"关键词覆盖：请求 {len(requested)} 个词、实际搜索 {len(searched)} 个"
                f"（爬虫按 候选池规模 // 10 取词）"
            )

        return SourceResult(
            source=self.name,
            status=status,
            candidates=candidates,
            keywords_requested=requested,
            keywords_used=searched,
            report=_whitelist_report(report),
            warnings=warnings,
            error=str(report.get("error") or "") if collection_failed else "",
        )

    def resolve_media_url(self, candidate: "Candidate") -> str:
        """Return the in-memory signed URL for ``candidate`` (``""`` if unknown).

        Douyin URLs are captured during collection, so there is no lazy backend
        call to fail -- an absent entry simply means "no usable address" and
        returns ``""`` (never :class:`~douyin_intelligence.sources.base.MediaResolutionError`).
        """
        video_id = str(getattr(candidate, "video_id", "") or "")
        return self._media_urls.get(video_id, "")

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    @staticmethod
    def _publish_time_kwargs(config: dict[str, Any]) -> dict[str, Any]:
        settings = ((config or {}).get("jobs") or {}).get("material_replication") or {}
        raw = (settings.get("search") or {}).get("publish_time_type")
        if raw is None:
            return {}
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return {}
        return {"publish_time_type": value}

    @staticmethod
    def _searched_keywords(report: dict[str, Any], requested: list[str]) -> list[str]:
        reported = report.get("keywords")
        if isinstance(reported, list):
            return [str(value) for value in reported]
        return list(requested)

    @staticmethod
    def _normalize(raw_rows: list[dict[str, Any]], config: dict[str, Any], keywords: list[str]) -> list["Candidate"]:
        from ..replication_candidates import compute_heat_scores, normalize_candidates

        candidates = normalize_candidates(raw_rows, config, keywords=keywords)
        compute_heat_scores(candidates)
        return candidates

    @staticmethod
    def _capture_media_urls(raw_rows: list[dict[str, Any]]) -> dict[str, str]:
        from ..replication_candidates import media_url_map

        return media_url_map(raw_rows)
