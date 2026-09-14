"""Pluggable material-source base layer.

This module defines the *contract* shared by every material source (Douyin,
yt-dlp/YouTube, WeChat Channels, ...).  It is deliberately dependency-free: it
must not import :mod:`douyin_intelligence.replication_candidates` at runtime, or
the adapter package would form an import cycle with the pipeline.  The
:class:`~douyin_intelligence.replication_candidates.Candidate` type is therefore
referenced only through ``TYPE_CHECKING`` string annotations.

The four error/result conventions below exist so that a *single* misbehaving
source can be degraded without aborting an aggregation run that fans out to
several sources:

* :class:`SourceError` -- the whole source failed (browser/network/parse).  The
  aggregation layer catches it and degrades that source only.
* :class:`SourceNotConfigured` -- the adapter is registered but the platform is
  not wired up yet (e.g. the WeChat-Channels endpoint is absent).
* :class:`MediaResolutionError` -- a *single* candidate's stream resolution
  failed.  It must be caught per-item so it counts as one download failure
  rather than tearing down the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from ..replication_candidates import Candidate


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class SourceError(RuntimeError):
    """Whole-source failure (browser / network / parse).

    Caught by the aggregation layer, which then degrades *this* source only --
    other sources keep running.
    """


class SourceNotConfigured(SourceError):
    """The adapter is registered but its platform is not connected yet.

    Example: the WeChat-Channels ("视频号") source has no endpoint configured.
    Raising this (instead of a bare ``SourceError``) lets the caller tell
    "switch this source off / it is not set up" apart from "this source broke".
    """


class MediaResolutionError(SourceError):
    """A *single* candidate's stream resolution failed.

    Raised by ``SourceAdapter.resolve_media_url`` for a backend/network/parse
    error.  Callers MUST catch it per candidate and record one download failure
    rather than abort the whole pipeline.  A candidate that simply has *no*
    usable address returns ``""`` instead -- see the protocol docstring.
    """


# --------------------------------------------------------------------------- #
# Per-source result
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class SourceResult:
    """The outcome of one source's ``search`` call.

    Every field is *disk-safe*: ``candidates`` and ``report`` never carry a
    signed/download URL or any credential, so the whole object may be written to
    ``candidate_pool.json`` / a run report without redaction.
    """

    source: str
    status: str  # "success" | "empty" | "failed" | "no_match"
    candidates: list["Candidate"]
    keywords_requested: list[str]
    keywords_used: list[str]
    report: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    error: str = ""


# --------------------------------------------------------------------------- #
# Source protocol
# --------------------------------------------------------------------------- #
@runtime_checkable
class SourceAdapter(Protocol):
    """A pluggable material source.

    ``search`` discovers candidates (disk-safe metadata only); the actual stream
    URL is fetched lazily, one candidate at a time, by ``resolve_media_url`` so
    that a signed URL never leaves memory and is only ever fetched for a
    candidate that is about to be downloaded.
    """

    name: str
    #: Referer to send when downloading this source's media.  ``None`` means "no
    #: cross-platform coupling" (a source-agnostic CDN); Douyin sets the historic
    #: ``https://www.douyin.com/`` value.  Read defensively by the composite
    #: resolver, so an adapter may omit it.
    download_referer: str | None

    def search(
        self,
        keywords: Sequence[str],
        budget: int,
        *,
        config: dict[str, Any],
        run_id: str | None = None,
    ) -> SourceResult:
        """Discover up to ``budget`` candidates for ``keywords``.

        Returns a :class:`SourceResult`.  A total failure should be reported as
        ``status="failed"`` (with ``error`` set) or raised as a ``SourceError``;
        either way the caller degrades only this source.
        """
        ...

    def resolve_media_url(self, candidate: "Candidate") -> str:
        """Resolve the download URL for a *single* candidate, on demand.

        Contract:

        * no usable address -> return ``""`` (NOT an exception);
        * backend / network / parse error -> raise
          :class:`MediaResolutionError` (counts as one failed download only).

        Safety contract: the returned URL is passed through memory only and is
        **never** persisted to disk.
        """
        ...


# --------------------------------------------------------------------------- #
# Media resolvers
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class DownloadTarget:
    """Where and how to fetch one candidate's media.

    ``url`` is a signed/stream URL: it exists in memory only and must never be
    written to disk.  ``referer`` is the header value the download must send --
    ``None`` for a source-agnostic CDN, the source's own referer otherwise.
    """

    url: str
    referer: str | None = None


class MediaResolver(Protocol):
    """Maps a candidate to a :class:`DownloadTarget`.

    Returns ``None`` when the candidate has no usable address (the caller then
    counts one failed download).  A backend/network failure raises
    :class:`MediaResolutionError` instead.  ``None`` is used rather than an empty
    string because "no target" and "target with an empty URL" must not be
    confusable.
    """

    def resolve_target(self, candidate: "Candidate") -> "DownloadTarget | None": ...


class DictMediaResolver:
    """Legacy Douyin path: look the URL up in an in-memory ``video_id -> url`` map.

    The resolved :class:`DownloadTarget` carries the historic Douyin referer, so
    a candidate resolved here downloads exactly as it did before -- byte for
    byte.  A miss returns ``None`` (no usable address).
    """

    #: The referer the legacy Douyin download path has always sent.
    DOUYIN_REFERER = "https://www.douyin.com/"

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping: dict[str, str] = dict(mapping or {})

    def resolve_target(self, candidate: "Candidate") -> "DownloadTarget | None":
        url = self._mapping.get(candidate.video_id, "")
        if not url:
            return None
        return DownloadTarget(url=url, referer=self.DOUYIN_REFERER)


class CompositeMediaResolver:
    """Multi-source resolver: dispatch a candidate to its own source adapter.

    Candidates currently carry no ``source`` attribute (that field is added to
    :class:`~douyin_intelligence.replication_candidates.Candidate` in a later
    batch), so the source is read defensively with ``getattr``.  A candidate
    whose source is unknown resolves to ``None`` rather than raising, matching
    the "no usable address -> no target" half of the contract.
    """

    def __init__(self, by_source: dict[str, "SourceAdapter"]) -> None:
        self._by_source: dict[str, SourceAdapter] = dict(by_source or {})

    def resolve_target(self, candidate: "Candidate") -> "DownloadTarget | None":
        source = str(getattr(candidate, "source", "") or "")
        adapter = self._by_source.get(source)
        if adapter is None:
            return None
        url = adapter.resolve_media_url(candidate)
        if not url:
            return None
        return DownloadTarget(url=url, referer=getattr(adapter, "download_referer", None))
