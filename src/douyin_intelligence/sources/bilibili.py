"""bilibili (B站) source adapter -- keyword discovery via the public web API.

Why this source exists
----------------------
The Douyin crawler has a real hole for **overseas / niche frontier hardware**
(for example "Microduck" the robot duck): Douyin's search returns zero on-topic
hits, while bilibili's index is a *tested 4/4* on the same topics.  This adapter
fills exactly that gap and nothing else.

Design goals
------------
* **Standard library only.**  ``urllib`` / ``http.cookiejar`` / ``hashlib`` /
  ``json`` / ``time`` / ``re`` -- no ``requests``, no ``httpx``, no ``yt_dlp``,
  no Playwright.  A keyword search is three plain GETs.
* **Disk safety.**  :attr:`SourceResult.report` is built by hand from a fixed
  whitelist of counts/keywords/status -- never a media URL, a ``wbi`` key or a
  cookie.  The only place a stream URL ever exists is the return value of
  :meth:`BilibiliSource.resolve_media_url`, which the caller keeps in memory.
* **Non-fatal degradation.**  A whole-source failure returns ``status="failed"``
  with an ``error`` string; a single keyword failing is recorded in ``warnings``.
  ``search`` never raises, so one broken source can never abort an aggregation
  run.

The ``wbi`` signature
---------------------
bilibili gates its web APIs behind a "wbi" signature (``w_rid``): a subset of a
``img_key``/``sub_key`` pair fetched from ``/x/web-interface/nav`` is used as an
MD5 salt over the sorted query string.  The ``nav`` endpoint answers ``code=-101``
("not logged in") for a guest **but still carries the keys**, so a missed
``-101`` is precisely the trap MediaCrawler falls into (it treats ``-101`` as a
failure and then runs a header-less fallback that bilibili rejects with HTTP
412).  Here the keys are read unconditionally, whatever the ``code`` is.

Empirically, an anonymous request with a valid guest cookie (``buvid3``, warmed
from the homepage) plus ``wbi`` signing is *enough*: no login is required.
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Sequence

from .base import MediaResolutionError, SourceResult

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from ..replication_candidates import Candidate


# --------------------------------------------------------------------------- #
# Endpoints / constants
# --------------------------------------------------------------------------- #
HOME_URL = "https://www.bilibili.com/"
NAV_ENDPOINT = "https://api.bilibili.com/x/web-interface/nav"
SEARCH_ENDPOINT = "https://api.bilibili.com/x/web-interface/wbi/search/type"
VIEW_ENDPOINT = "https://api.bilibili.com/x/web-interface/view"
PLAYURL_ENDPOINT = "https://api.bilibili.com/x/player/wbi/playurl"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: The permuted index table used to derive the 32-char ``mixin_key`` from
#: ``img_key + sub_key``.  Fixed by bilibili's client-side JS; copied verbatim.
MIXIN: tuple[int, ...] = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
)

#: bilibili rejects a foreign referer on media downloads, so the stream must be
#: fetched with the site referer -- see :class:`../sources.base.DownloadTarget`.
BILIBILI_REFERER = "https://www.bilibili.com/"

#: Characters stripped from query values before signing (matches bilibili's JS).
_SIGN_STRIP = "!'()*"

#: Default per-request throttle and socket timeout (seconds).
DEFAULT_SLEEP_SECONDS = 2.0
DEFAULT_TIMEOUT_SECONDS = 15.0

#: 412 back-off: retry up to ``MAX_ATTEMPTS`` times, sleeping ``3 * attempt``.
MAX_ATTEMPTS = 4
RETRY_BACKOFF_BASE = 3.0

#: Request guard: at most this many videos per keyword page.
MAX_PAGE_SIZE = 20
#: Request guard: at most this many keywords are queried per run (the remaining
#: ones are reported as truncated) so a large theme list cannot trip risk control.
MAX_KEYWORDS = 8

#: bilibili API ``code`` values that mean **"this candidate has no usable
#: media"** (as opposed to "the source is broken").  A content-level failure is a
#: normal miss: :meth:`BilibiliSource.resolve_media_url` returns ``""`` and the
#: caller simply moves to the next candidate -- it must not raise, warn or trip
#: a back-off.  Every *other* non-zero ``code`` is a channel-level failure
#: (risk control, rate limiting, ...) and raises
#: :class:`~douyin_intelligence.sources.base.MediaResolutionError`.
#:
#: This is the minimal set known from bilibili's documented semantics; extend it
#: from real observations.  The raised error carries the raw ``code`` and
#: ``message`` so a newly-seen content code can be spotted in the logs and added
#: here.
CONTENT_UNAVAILABLE_API_CODES: frozenset[int] = frozenset(
    {
        -404,  # 稿件不存在
        -403,  # 权限不足
        87007,  # 充电专属（付费）
        62002,  # 稿件不可见
        62004,  # 稿件审核中
    }
)


# --------------------------------------------------------------------------- #
# Pure wbi signing (unit-tested in isolation)
# --------------------------------------------------------------------------- #
def mixin_key_from(img_key: str, sub_key: str) -> str:
    """Derive the 32-char ``mixin_key`` salt from the two raw wbi keys.

    Args:
        img_key: The ``img_url`` basename (no extension) from ``/nav``.
        sub_key: The ``sub_url`` basename (no extension) from ``/nav``.

    Returns:
        The permuted 32-character salt; ``""`` when either key is missing.
    """
    if not img_key or not sub_key:
        return ""
    combined = str(img_key) + str(sub_key)
    return "".join(combined[index] for index in MIXIN)[:32]


def compute_w_rid(params: dict[str, Any], mixin_key: str) -> str:
    """Compute the bilibili ``w_rid`` signature for ``params`` (pure function).

    The algorithm: drop any existing ``w_rid``; stringify values and strip
    :data:`_SIGN_STRIP`; sort by key; URL-encode; append ``mixin_key``; MD5.

    Args:
        params: The query parameters (may or may not already contain ``wts``).
        mixin_key: The salt from :func:`mixin_key_from`.

    Returns:
        The lowercase hex MD5 digest.
    """
    cleaned: dict[str, str] = {}
    for key, value in params.items():
        name = str(key)
        if name == "w_rid":
            continue
        text = "" if value is None else str(value)
        cleaned[name] = "".join(ch for ch in text if ch not in _SIGN_STRIP)
    query = urllib.parse.urlencode(sorted(cleaned.items()))
    return hashlib.md5((query + str(mixin_key)).encode("utf-8")).hexdigest()


def sign_params(params: dict[str, Any], mixin_key: str, *, wts: int | None = None) -> dict[str, Any]:
    """Return ``params`` plus a fresh ``wts`` and its matching ``w_rid``."""
    signed: dict[str, Any] = {str(k): v for k, v in params.items() if str(k) != "w_rid"}
    signed["wts"] = int(wts if wts is not None else time.time())
    signed["w_rid"] = compute_w_rid(signed, mixin_key)
    return signed


# --------------------------------------------------------------------------- #
# Default fetcher (urllib + a persistent cookie jar)
# --------------------------------------------------------------------------- #
class _UrllibFetcher:
    """A stateful ``urllib`` fetch callable matching the ``fetcher`` seam.

    Holding one instance keeps a single :class:`http.cookiejar.CookieJar` alive
    across calls, so the guest cookies warmed from the homepage (``buvid3``,
    ``b_nut``) are replayed on the subsequent ``nav``/``search`` requests -- a
    fresh client per call would lose them and get HTTP 412.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout = float(timeout)
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )

    def __call__(
        self, url: str, headers: dict[str, str], timeout: float | None = None
    ) -> tuple[int, Any]:
        request = urllib.request.Request(url, headers=dict(headers or {}), method="GET")
        wait = self._timeout if timeout is None else float(timeout)
        try:
            with self._opener.open(request, timeout=wait) as response:
                status = int(getattr(response, "status", 200) or 200)
                raw = response.read()
        except urllib.error.HTTPError as exc:  # 4xx/5xx: read the body, keep the code
            status = int(exc.code)
            raw = exc.read() if hasattr(exc, "read") else b""
        except (urllib.error.URLError, OSError):
            return 0, None  # network failure -> status 0, caller retries
        try:
            payload: Any = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = None
        return status, payload


# --------------------------------------------------------------------------- #
# Report whitelist
# --------------------------------------------------------------------------- #
#: Disk-safe report keys.  A whitelist (rather than ``dict(...)``) guarantees a
#: future field can never smuggle a media URL / wbi key / cookie into the report.
_REPORT_KEYS = (
    "site",
    "status",
    "keywords_requested",
    "keywords_used",
    "keywords_truncated",
    "budget",
    "per_keyword",
    "returned",
    "matched",
    "failed_keywords",
)


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
class BilibiliSource:
    """A :class:`~douyin_intelligence.sources.base.SourceAdapter` over bilibili."""

    name = "bilibili"
    #: bilibili's CDN rejects a missing/foreign referer for media downloads.
    #:
    #: The stream URL returned by :meth:`resolve_media_url` is *signed and
    #: expiring*: a live resolve confirmed the query string carries bilibili's
    #: expiry params ``e`` / ``deadline`` / ``gen`` (alongside ``buvid`` /
    #: ``mid`` / ``upsig``).  It is therefore valid for a short window only and
    #: must be consumed immediately -- never cached to disk.
    download_referer = BILIBILI_REFERER

    def __init__(
        self,
        *,
        fetcher: Callable[[str, dict[str, str], float], tuple[int, Any]] | None = None,
        sleeper: Callable[[float], None] | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        """Create the adapter.

        Args:
            fetcher: test seam -- ``fetcher(url, headers, timeout) ->
                (status_code, parsed_json)``.  When provided, the real
                ``urllib`` path is never used, so tests make no network calls.
            sleeper: test seam -- ``sleeper(seconds)``.  When provided, the
                adapter does not actually sleep (keeps tests fast).
            timeout_seconds: per-request socket timeout; ``None`` uses
                :data:`DEFAULT_TIMEOUT_SECONDS`.
        """
        self._fetcher = fetcher
        self._sleeper = sleeper
        self._timeout = float(timeout_seconds) if timeout_seconds is not None else DEFAULT_TIMEOUT_SECONDS
        self._delay = DEFAULT_SLEEP_SECONDS
        self._requests_made = 0
        #: Derived wbi salt, cached for the lifetime of the adapter (memory only).
        self._mixin_key = ""

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
        """Discover candidates for ``keywords`` (never raises)."""
        requested = [str(item).strip() for item in (keywords or []) if str(item).strip()]
        call_budget = max(1, int(budget or 0))
        self._delay = self._request_delay(config)

        if not requested:
            return SourceResult(
                source=self.name,
                status="empty",
                candidates=[],
                keywords_requested=[],
                keywords_used=[],
                report={
                    "site": "bilibili",
                    "status": "empty",
                    "keywords_requested": [],
                    "keywords_used": [],
                    "budget": call_budget,
                },
                warnings=["未提供关键词，未执行搜索"],
            )

        # ``per_keyword`` mirrors the Douyin semantics (``budget // 10``) with a
        # floor of 1 and a hard ceiling of one page, so a huge budget cannot ask
        # bilibili for an illegal page size.
        per_keyword = min(MAX_PAGE_SIZE, max(1, call_budget // 10))

        # Request guard: query at most ``MAX_KEYWORDS`` terms; the overflow is
        # reported as truncated instead of silently dropped.
        to_search = requested[:MAX_KEYWORDS]
        truncated = requested[MAX_KEYWORDS:]

        warnings: list[str] = []
        if truncated:
            warnings.append(
                f"关键词截断：请求 {len(requested)} 个词，仅搜索前 {len(to_search)} 个"
                f"（单次请求护栏 MAX_KEYWORDS={MAX_KEYWORDS}）"
            )

        headers = self._headers()
        try:
            mixin_key = self._ensure_mixin_key(headers)
        except Exception as exc:  # noqa: BLE001 - degrade, never raise through
            return self._failed_result(requested, call_budget, exc)

        if not mixin_key:
            return self._failed_result(
                requested, call_budget, RuntimeError("未能取得 wbi 签名钥匙（/nav 连续失败）")
            )

        candidates: list[Candidate] = []
        failed_keywords: list[str] = []
        used_keywords: list[str] = []
        returned = 0
        succeeded_any = False

        for keyword in to_search:
            used_keywords.append(keyword)
            params = {
                "search_type": "video",
                "keyword": keyword,
                "page": 1,
                "page_size": per_keyword,
                "order": "totalrank",
                "platform": "pc",
            }
            signed = sign_params(params, mixin_key)
            url = f"{SEARCH_ENDPOINT}?{urllib.parse.urlencode(signed)}"
            status, payload = self._http(url, headers)
            data = payload.get("data") if isinstance(payload, dict) else None
            items = data.get("result") if isinstance(data, dict) else None
            if status == 200 and isinstance(payload, dict) and payload.get("code") == 0 and items is not None:
                succeeded_any = True
                for item in items:
                    if returned >= per_keyword:
                        break
                    if not isinstance(item, dict):
                        continue
                    returned += 1
                    candidate = self._to_candidate(item, keyword)
                    if candidate is not None:
                        candidates.append(candidate)
            else:
                failed_keywords.append(keyword)
                warnings.append(
                    f"B站搜索关键词「{keyword}」失败（HTTP {status}，退避重试 {MAX_ATTEMPTS} 次仍未成功）"
                )

        if candidates:
            status_name = "success"
        elif succeeded_any:
            status_name = "no_match"
        else:
            status_name = "failed"

        report = {
            "site": "bilibili",
            "status": status_name,
            "keywords_requested": requested,
            "keywords_used": used_keywords,
            "keywords_truncated": truncated,
            "budget": call_budget,
            "per_keyword": per_keyword,
            "returned": returned,
            "matched": len(candidates),
            "failed_keywords": failed_keywords,
        }
        error = ""
        if status_name == "failed":
            error = "所有关键词的 B站搜索请求均失败"
            warnings.append(error)

        return SourceResult(
            source=self.name,
            status=status_name,
            candidates=candidates,
            keywords_requested=requested,
            keywords_used=used_keywords,
            report=report,
            warnings=warnings,
            error=error,
        )

    def resolve_media_url(self, candidate: "Candidate") -> str:
        """Resolve a *single* candidate's stream URL.

        Two GETs: ``/x/web-interface/view`` for the ``cid``, then the signed
        ``/x/player/wbi/playurl`` for the stream.  The two failure modes are
        kept apart (matching :mod:`..sources.base`'s intent):

        * **no usable media** (``code=0`` but no ``cid`` / no ``durl`` /
          ``dash``) -> return ``""``: a normal "move to the next candidate";
        * **source backend / transport failure** (412 retries exhausted, a
          network error, or a non-zero API ``code``) -> raise
          :class:`~douyin_intelligence.sources.base.MediaResolutionError`, so
          the download layer can back off / alert instead of silently skipping.

        The returned URL carries bilibili's expiring signature (the query string
        includes ``e`` / ``deadline`` / ``gen``), so it is valid for a short
        window only and must be consumed immediately; it is never written to
        disk.
        """
        bvid = str(getattr(candidate, "video_id", "") or "").strip()
        if not bvid:
            return ""
        try:
            headers = self._headers()
            mixin_key = self._ensure_mixin_key(headers)
            if not mixin_key:
                raise MediaResolutionError("bilibili：未能取得 wbi 签名钥匙（/nav 连续失败）")
            cid = self._fetch_cid(bvid, headers)
            if not cid:
                return ""  # code=0 but no cid -> no usable media (normal miss)
            return self._fetch_play_url(bvid, cid, headers, mixin_key)
        except MediaResolutionError:
            raise
        except Exception as exc:  # transport / parse -> a retryable source error
            raise MediaResolutionError(
                f"bilibili：取流异常 {type(exc).__name__}: {str(exc)[:200]}"
            ) from exc

    # ------------------------------------------------------------------ #
    # Internals -- HTTP
    # ------------------------------------------------------------------ #
    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
        }

    def _get_fetcher(self) -> Callable[[str, dict[str, str], float], tuple[int, Any]]:
        if self._fetcher is None:
            self._fetcher = _UrllibFetcher(self._timeout)
        return self._fetcher

    def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        call = self._sleeper if self._sleeper is not None else time.sleep
        call(float(seconds))

    def _http(self, url: str, headers: dict[str, str]) -> tuple[int, Any]:
        """GET ``url`` with throttling and 412/network back-off.

        Returns the last ``(status_code, parsed_json)``; a caller decides
        whether that is success (``200`` + a dict) or a failure.
        """
        fetch = self._get_fetcher()
        status, payload = 0, None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt == 1:
                # Throttle *between* requests (never before the very first one).
                if self._requests_made > 0:
                    self._sleep(self._delay)
            else:
                self._sleep(RETRY_BACKOFF_BASE * attempt)
            status, payload = fetch(url, headers, self._timeout)
            self._requests_made += 1
            if status == 200 and isinstance(payload, dict):
                return status, payload
        return status, payload

    # ------------------------------------------------------------------ #
    # Internals -- wbi / search / resolve
    # ------------------------------------------------------------------ #
    def _ensure_mixin_key(self, headers: dict[str, str]) -> str:
        """Warm a guest cookie, fetch ``/nav`` and derive the wbi salt (cached)."""
        if self._mixin_key:
            return self._mixin_key
        # Step 0: warm the cookie jar (buvid3 / b_nut).  The status is not
        # inspected -- even a blocked homepage still advances the jar on some
        # paths, and /nav retries anyway.
        self._http(HOME_URL, headers)
        # Step 1: the keys live under data.wbi_img regardless of ``code``; a
        # guest response is ``code=-101`` and MUST still be read.
        status, nav = self._http(NAV_ENDPOINT, headers)
        if status != 200 or not isinstance(nav, dict):
            return ""
        img_key, sub_key = self._wbi_keys(nav)
        if not img_key or not sub_key:
            return ""
        self._mixin_key = mixin_key_from(img_key, sub_key)
        return self._mixin_key

    @staticmethod
    def _wbi_keys(nav: dict[str, Any]) -> tuple[str, str]:
        """Extract ``(img_key, sub_key)`` from a ``/nav`` payload (code-agnostic)."""
        data = nav.get("data")
        wbi_img = data.get("wbi_img") if isinstance(data, dict) else None
        if not isinstance(wbi_img, dict):
            return "", ""
        img_url = str(wbi_img.get("img_url") or "")
        sub_url = str(wbi_img.get("sub_url") or "")
        img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
        sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
        return img_key, sub_key

    def _fetch_cid(self, bvid: str, headers: dict[str, str]) -> int:
        """Return the ``cid`` for ``bvid``.

        * content-level ``code`` (see :data:`CONTENT_UNAVAILABLE_API_CODES`) or
          ``code=0`` with no ``cid`` -> ``0`` (the caller maps it to ``""``);
        * transport failure or any *other* non-zero ``code`` -> raise
          :class:`MediaResolutionError`.
        """
        url = f"{VIEW_ENDPOINT}?{urllib.parse.urlencode({'bvid': bvid})}"
        status, payload = self._http(url, headers)
        if status != 200 or not isinstance(payload, dict):
            raise MediaResolutionError(f"bilibili：view 接口调用失败（HTTP {status}）")
        if payload.get("code") != 0:
            code = payload.get("code")
            if code in CONTENT_UNAVAILABLE_API_CODES:
                return 0  # content-level: no usable media -> "" upstream
            raise MediaResolutionError(
                f"bilibili：view 接口返回错误码 {code}（{payload.get('message') or ''}）"
            )
        data = payload.get("data")
        cid = data.get("cid") if isinstance(data, dict) else None
        try:
            return int(cid) if cid else 0
        except (TypeError, ValueError):
            return 0

    def _fetch_play_url(self, bvid: str, cid: int, headers: dict[str, str], mixin_key: str) -> str:
        """Return the stream URL, or ``""`` when the candidate has no stream.

        * content-level ``code`` (see :data:`CONTENT_UNAVAILABLE_API_CODES`) or
          ``code=0`` with no ``durl``/``dash`` -> ``""``;
        * transport failure or any *other* non-zero ``code`` -> raise
          :class:`MediaResolutionError`.
        """
        params = {"bvid": bvid, "cid": cid, "qn": 32, "fnval": 1, "fourk": 0}
        signed = sign_params(params, mixin_key)
        url = f"{PLAYURL_ENDPOINT}?{urllib.parse.urlencode(signed)}"
        status, payload = self._http(url, headers)
        if status != 200 or not isinstance(payload, dict):
            raise MediaResolutionError(f"bilibili：playurl 接口调用失败（HTTP {status}）")
        if payload.get("code") != 0:
            code = payload.get("code")
            if code in CONTENT_UNAVAILABLE_API_CODES:
                return ""  # content-level: no usable media -> normal miss
            raise MediaResolutionError(
                f"bilibili：playurl 接口返回错误码 {code}（{payload.get('message') or ''}）"
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            return ""
        durl = data.get("durl")
        if isinstance(durl, list) and durl and isinstance(durl[0], dict):
            direct = str(durl[0].get("url") or "")
            if direct:
                return direct
        dash = data.get("dash")
        videos = dash.get("video") if isinstance(dash, dict) else None
        if isinstance(videos, list) and videos and isinstance(videos[0], dict):
            first = videos[0]
            return str(first.get("baseUrl") or first.get("base_url") or "")
        return ""

    def _to_candidate(self, item: dict[str, Any], keyword: str) -> "Candidate | None":
        from ..replication_candidates import Candidate  # local import avoids a cycle

        bvid = str(item.get("bvid") or "").strip()
        if not bvid:
            return None
        author = str(item.get("author") or "")
        mid = item.get("mid")
        return Candidate(
            video_id=bvid,
            title=_strip_highlight(item.get("title")),
            author=author,
            author_hash=str(mid) if mid else "",
            source_url=f"https://www.bilibili.com/video/{bvid}",
            published_at=_published_at(item.get("pubdate")),
            digg_count=_as_int(item.get("like")) or 0,
            play_count=_as_int(item.get("play")),
            duration_seconds=_parse_duration(item.get("duration")),
            duration_source="bilibili.duration" if item.get("duration") else "",
            source_keyword=str(keyword),
            media_url_present=True,
        )

    # ------------------------------------------------------------------ #
    # Internals -- config / results
    # ------------------------------------------------------------------ #
    @staticmethod
    def _request_delay(config: dict[str, Any]) -> float:
        settings = ((config or {}).get("jobs") or {}).get("material_replication") or {}
        raw = settings.get("bilibili_sleep_seconds")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return DEFAULT_SLEEP_SECONDS
        return value if value >= 0 else DEFAULT_SLEEP_SECONDS

    def _failed_result(self, requested: list[str], budget: int, exc: BaseException) -> SourceResult:
        message = f"{type(exc).__name__}: {str(exc)[:300]}"
        return SourceResult(
            source=self.name,
            status="failed",
            candidates=[],
            keywords_requested=requested,
            keywords_used=[],
            report={
                "site": "bilibili",
                "status": "failed",
                "keywords_requested": requested,
                "keywords_used": [],
                "keywords_truncated": [],
                "budget": budget,
                "per_keyword": 0,
                "returned": 0,
                "matched": 0,
                "failed_keywords": [],
            },
            warnings=[f"B站采集未成功：{message}"],
            error=message,
        )


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #
_HIGHLIGHT_RE = re.compile(r"<[^>]+>")


def _strip_highlight(value: Any) -> str:
    """Remove the ``<em class="keyword">`` highlight tags bilibili injects."""
    return _HIGHLIGHT_RE.sub("", str(value or "")).strip()


def _published_at(pubdate: Any) -> str:
    """Convert a unix-seconds ``pubdate`` to an ISO-8601 UTC string (``""`` if none)."""
    try:
        value = int(pubdate)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _parse_duration(value: Any) -> float:
    """Parse bilibili's ``"MM:SS"`` / ``"HH:MM:SS"`` duration string to seconds."""
    text = str(value or "").strip()
    if not text:
        return 0.0
    parts = text.split(":")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return 0.0
    total = 0
    for number in numbers:
        total = total * 60 + number
    return float(total)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
