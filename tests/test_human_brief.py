from __future__ import annotations

from douyin_intelligence.human_brief import render_human_brief


def _event(*, rank: int, images: list[dict] | None = None, image_state: str = "failed") -> dict:
    return {
        "rank": rank,
        "story_id": f"story-{rank}",
        "canonical_title": f"示例热点 {rank}",
        "heat_score": 50 - rank,
        "video_count": 1,
        "event_summary": "这是一段用于人类阅读的简短摘要。",
        "key_points": ["要点一", "要点二"],
        "content_angles": ["发布", "行业影响"],
        "claims_to_verify": ["其中的数字和公司表态仍需核验"],
        "extraction_status": "success",
        "images": images or [],
        "image_status": {
            "state": image_state,
            "reason": "没有匹配到受控图片提示" if image_state == "failed" else None,
        },
        "contributing_videos": [
            {
                "title": "来源视频",
                "share_url": "https://www.douyin.com/video/12345678",
                "author": "示例账号",
                "published_at": "2026-08-30T12:00:00+08:00",
            }
        ],
    }


def _pack(events: list[dict], images: int) -> dict:
    return {
        "business_date": "2026-08-30",
        "status": "partial",
        "disclaimer": "copy_skill 未核验真实性，候选仅供 OP 选题与后续核验。",
        "counts": {"images": images},
        "candidates": events,
    }


def test_human_brief_embeds_acquired_image_and_keeps_rights_notice() -> None:
    asset = {
        "relative_path": "images/story-1/primary.jpg",
        "source_name": "官方新闻稿",
        "source_article_url": "https://example.com/news",
        "rights_status": "review_required",
        "width": 1280,
        "height": 720,
    }
    rendered = render_human_brief(_pack([_event(rank=1, images=[asset], image_state="succeeded")], 1))
    assert "![示例热点 1 配图 1](images/story-1/primary.jpg)" in rendered
    assert "[官方新闻稿](https://example.com/news)" in rendered
    assert "权利状态：`review_required`" in rendered
    assert "**已获取配图** 1 张" in rendered


def test_human_brief_explains_missing_top3_and_unattempted_lower_rank() -> None:
    rendered = render_human_brief(_pack([_event(rank=1), _event(rank=4, image_state="not_ranked_top3")], 0))
    assert "暂无合格配图：没有匹配到受控图片提示" in rendered
    assert "本流程只为推荐 Top 3 尝试配图" in rendered
    assert "![](" not in rendered


def test_human_brief_sanitizes_untrusted_markdown_and_limits_long_fields() -> None:
    event = _event(rank=1)
    event["canonical_title"] = "# 标题 [点我](https://bad.example) <script>"
    event["event_summary"] = "A" * 500
    event["contributing_videos"][0]["title"] = "![坏图](https://bad.example/a.jpg)"
    rendered = render_human_brief(_pack([event], 0))
    assert "https://bad.example" not in rendered
    assert "<script>" not in rendered
    assert "![坏图]" not in rendered
    assert "A" * 321 not in rendered
    assert "…" in rendered


def test_human_brief_rejects_unsafe_image_paths() -> None:
    asset = {
        "relative_path": "../outside.jpg",
        "source_name": "来源",
        "source_article_url": "https://example.com/news",
        "rights_status": "review_required",
        "width": 800,
        "height": 600,
    }
    rendered = render_human_brief(_pack([_event(rank=1, images=[asset], image_state="succeeded")], 1))
    assert "../outside.jpg" not in rendered
    assert "没有可安全嵌入的有效相对路径" in rendered


def test_human_brief_supports_safe_package_prefix_for_external_preview() -> None:
    asset = {
        "relative_path": "images/story-1/primary.jpg",
        "source_name": "来源",
        "source_article_url": "https://example.com/news",
        "rights_status": "review_required",
        "width": 800,
        "height": 600,
    }
    rendered = render_human_brief(
        _pack([_event(rank=1, images=[asset], image_state="succeeded")], 1),
        artifact_prefix="packs/run-20260830-example",
    )
    assert "(packs/run-20260830-example/images/story-1/primary.jpg)" in rendered
    assert "(packs/run-20260830-example/昨日抖音科技热点候选.md)" in rendered
    assert "(packs/run-20260830-example/candidate-pool.json)" in rendered


def test_human_brief_rejects_parent_traversal_prefix() -> None:
    try:
        render_human_brief(_pack([_event(rank=1)], 0), artifact_prefix="../pack")
    except ValueError as exc:
        assert "安全相对路径" in str(exc)
    else:
        raise AssertionError("parent traversal prefix should be rejected")


def test_human_brief_omits_engineering_fields_but_links_technical_contracts() -> None:
    event = _event(rank=1)
    event["matrix_deduplication"] = [{"selected_video_id": "12345678"}]
    event["score_components"] = {"like": 10}
    rendered = render_human_brief(_pack([event], 0))
    assert "matrix_deduplication" not in rendered and "score_components" not in rendered
    assert "[技术审计稿](昨日抖音科技热点候选.md)" in rendered
    assert "[candidate-pool.json](candidate-pool.json)" in rendered


def test_human_brief_separates_ready_news_from_reviews_and_uses_news_headline() -> None:
    news = _event(rank=1)
    news.update({
        "content_type": "news_lead", "news_readiness": "ready",
        "news_headline": "腾讯发布混元新模型",
        "event_completeness": {"score": 90, "missing": [], "quality_flags": []},
    })
    review = _event(rank=2)
    review.update({
        "canonical_title": "MiniMax H3模型实测内容", "content_type": "creator_review",
        "news_readiness": "not_news", "news_headline": "",
        "event_completeness": {"score": 30, "missing": ["subject", "object"], "quality_flags": []},
    })
    rendered = render_human_brief(_pack([news, review], 0))
    assert "## 昨日科技新闻候选" in rendered
    assert "## 高热度科技内容与待补充线索" in rendered
    assert "腾讯发布混元新模型" in rendered
    assert "MiniMax H3模型实测内容" in rendered
    news_table = rendered.split("## 高热度科技内容与待补充线索", 1)[0]
    assert "MiniMax H3模型实测内容" not in news_table
