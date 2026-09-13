from __future__ import annotations

import json
import types
from pathlib import Path

from douyin_intelligence.config import load_config
from douyin_intelligence.replication_candidates import (
    Candidate,
    collect_candidate_pool,
    compute_heat_scores,
    media_url_map,
    normalize_candidates,
)
from douyin_intelligence.replication_theme import expand_keywords


def _row(video_id: str, *, digg: int, comment: int, share: int, collect: int, play: int | None = None, duration: float = 0.0, url: str = "") -> dict:
    row = {
        "aweme_id": video_id,
        "desc": f"标题-{video_id}",
        "author": {"uid": "author-x", "nickname": "作者"},
        "create_time": "2026-09-11T08:00:00+08:00",
        "statistics": {"digg_count": digg, "comment_count": comment, "share_count": share, "collect_count": collect},
        "duration": duration,
        "share_url": f"https://www.douyin.com/video/{video_id}",
    }
    if play is not None:
        row["play_count"] = play
    if url:
        row["video_download_url"] = url
    return row


def test_compute_heat_scores_normalizes_and_ranks_deterministically() -> None:
    high = Candidate(video_id="2", digg_count=100, duration_seconds=10)
    low = Candidate(video_id="1", digg_count=50, duration_seconds=20)
    tie_a = Candidate(video_id="3", digg_count=50, duration_seconds=20)
    candidates = [high, low, tie_a]
    compute_heat_scores(candidates)
    assert high.heat_score == 1.0
    assert low.heat_score == round(0.5, 6)
    # Tie broken by video_id ascending (same score and duration).
    assert low.heat_rank == 2 and tie_a.heat_rank == 3
    assert high.heat_rank == 1


def test_compute_heat_scores_tolerates_missing_play_count() -> None:
    without_play = Candidate(video_id="a", digg_count=10, comment_count=10, share_count=10, collect_count=10)
    compute_heat_scores([without_play])
    assert without_play.heat_score == 1.0


def test_normalize_candidates_deduplicates_and_keeps_fields() -> None:
    config = load_config()
    rows = [
        _row("111", digg=10, comment=1, share=1, collect=1, duration=45, url="https://signed/1"),
        _row("111", digg=99, comment=9, share=9, collect=9, duration=45, url="https://signed/1b"),
        _row("222", digg=5, comment=0, share=0, collect=0, duration=0),
    ]
    candidates = normalize_candidates(rows, config, keywords=["苹果"])
    assert [candidate.video_id for candidate in candidates] == ["111", "222"]
    first = candidates[0]
    assert first.author == "作者"
    assert first.duration_seconds == 45.0
    assert first.media_url_present is True
    assert "video_download_url" not in first.to_dict()
    assert candidates[1].media_url_present is False


def test_duration_source_is_empty_when_no_duration_is_read() -> None:
    """A candidate with no usable duration must NOT carry a duration *source*.

    Otherwise a reader sees ``duration_seconds=0`` next to a non-empty
    ``duration_source`` (e.g. ``"duration"``), which falsely implies a duration
    was read from that field -- the misleading pair reported in review.
    """
    config = load_config()
    rows = [
        _row("has", digg=10, comment=0, share=0, collect=0, duration=45),
        _row("zero", digg=10, comment=0, share=0, collect=0, duration=0),
    ]
    missing = _row("missing", digg=10, comment=0, share=0, collect=0, duration=0)
    missing.pop("duration")  # the raw crawler row carried no duration key at all
    rows.append(missing)

    candidates = {candidate.video_id: candidate for candidate in normalize_candidates(rows, config, keywords=["苹果"])}
    assert candidates["has"].duration_seconds == 45.0
    assert candidates["has"].duration_source == "duration"
    for video_id in ("zero", "missing"):
        assert candidates[video_id].duration_seconds == 0.0
        assert candidates[video_id].duration_source == ""


def test_media_url_map_captures_signed_urls() -> None:
    rows = [_row("111", digg=1, comment=0, share=0, collect=0, url="https://signed/1")]
    assert media_url_map(rows) == {"111": "https://signed/1"}


def test_collect_candidate_pool_captures_media_url_before_sanitize(tmp_path: Path) -> None:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    source = tmp_path / "search" / "search_contents_1.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps([_row("7300000000000000001", digg=1000, comment=10, share=5, collect=20, duration=60, url="https://signed.example/secret")], ensure_ascii=False),
        encoding="utf-8",
    )

    def fake_collector(cfg, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None):
        assert before_sanitize is not None
        before_sanitize([source])
        return {"status": "success", "budget": budget, "keywords": keywords}

    pool = collect_candidate_pool(
        config, "苹果折叠屏手机", pool_size=80, run_id="run-1",
        deps=types.SimpleNamespace(collector=fake_collector),
    )
    assert pool["status"] == "success"
    assert pool["media_urls"] == {"7300000000000000001": "https://signed.example/secret"}
    serialized = json.dumps(pool["candidate_pool"], ensure_ascii=False)
    assert "signed.example" not in serialized
    assert "video_download_url" not in serialized
    assert pool["candidate_pool"]["candidates"][0]["media_url_present"] is True
    # The fake collector echoed the full request, so nothing was truncated.
    assert pool["keywords_used"] == pool["keywords_requested"]
    assert pool["keywords_truncated"] is False


def test_collect_candidate_pool_separates_requested_from_searched_keywords(tmp_path: Path) -> None:
    # The crawler only searches ``keywords[:budget // 10]``; the shortfall
    # warning must attribute the pool size to *that* truncation, and
    # ``keywords_used`` must never claim the un-searched keywords.
    config = load_config()
    config["_project_root"] = str(tmp_path)
    source = tmp_path / "search" / "search_contents_1.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps([_row("7300000000000000001", digg=10, comment=0, share=0, collect=0)], ensure_ascii=False),
        encoding="utf-8",
    )
    requested = expand_keywords("苹果折叠屏", config)
    assert len(requested) > 4

    def truncating_collector(cfg, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None):
        assert keywords == requested
        before_sanitize([source])
        return {
            "status": "success", "budget": budget, "keywords": list(keywords)[:4],
            "per_keyword_budget": 10, "raw_request_ceiling": 40,
        }

    pool = collect_candidate_pool(
        config, "苹果折叠屏", pool_size=40, run_id="run-trunc",
        deps=types.SimpleNamespace(collector=truncating_collector),
    )
    assert pool["keywords_requested"] == requested
    assert pool["keywords_used"] == requested[:4]
    assert pool["keywords"] == requested[:4]
    assert pool["keywords_truncated"] is True
    assert pool["candidate_pool"]["keywords"] == requested[:4]
    assert pool["candidate_pool"]["keywords_requested"] == requested
    warning = next(item for item in pool["warnings"] if "低于最小目标" in item)
    assert "实际搜索关键词 4 个 / 请求 10 个（发生关键词截断）" in warning
    assert "per_keyword_budget=10" in warning
    assert "raw_request_ceiling=40" in warning
    assert "05-过程数据/search_report.json" in warning


def test_collect_candidate_pool_failed_collection_claims_no_searched_keywords(tmp_path: Path) -> None:
    config = load_config()
    config["_project_root"] = str(tmp_path)

    def failing_collector(cfg, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None):
        raise RuntimeError("browser down")

    pool = collect_candidate_pool(
        config, "苹果折叠屏", pool_size=40, run_id="run-fail",
        deps=types.SimpleNamespace(collector=failing_collector),
    )
    assert pool["keywords_used"] == []
    assert pool["keywords_requested"] == expand_keywords("苹果折叠屏", config)
    assert pool["keywords_truncated"] is True
    assert any("候选池采集未成功，仅使用已捕获的记录" in item for item in pool["warnings"])
    shortfall = next(item for item in pool["warnings"] if "低于最小目标" in item)
    assert "候选池采集未成功，未取得有效关键词覆盖" in shortfall


def test_looks_like_audio_url_detects_image_album_music() -> None:
    """Douyin image-album posts expose their background music as the 'video' URL."""
    from douyin_intelligence.replication_candidates import looks_like_audio_url

    assert looks_like_audio_url(
        "https://lf26-music-east.douyinstatic.com/obj/ies-music-hj/7667767130976324401.mp3"
    ) is True
    assert looks_like_audio_url(
        "https://sf11-cdn-tos.douyinstatic.com/obj/ies-music/7684298771236260650.mp3"
    ) is True
    assert looks_like_audio_url(
        "https://www.douyin.com/aweme/v1/play/?video_id=v0200fg10000dahqju7og65h21opt4i0&line=0"
    ) is False
    assert looks_like_audio_url("") is False


def test_normalize_candidates_records_aweme_type_and_audio_flag() -> None:
    config = load_config()
    rows = [
        {
            **_row(
                "7400000000000000001",
                digg=800, comment=5, share=1, collect=2, duration=40,
                url="https://lf26-music-east.douyinstatic.com/obj/ies-music-hj/123.mp3",
            ),
            "aweme_type": "68",
        },
        {
            **_row(
                "7400000000000000002",
                digg=100, comment=5, share=1, collect=2, duration=40,
                url="https://www.douyin.com/aweme/v1/play/?video_id=abc&line=0",
            ),
            "aweme_type": "0",
        },
    ]
    candidates = normalize_candidates(rows, config, keywords=["折叠屏"])
    by_id = {candidate.video_id: candidate for candidate in candidates}
    album = by_id["7400000000000000001"]
    video = by_id["7400000000000000002"]
    assert album.aweme_type == "68" and album.media_is_audio is True
    assert video.aweme_type == "0" and video.media_is_audio is False
