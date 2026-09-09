from __future__ import annotations

from pathlib import Path

from douyin_intelligence.news_sources import NewsArticle, cluster_articles, fetch_sources
from douyin_intelligence.config import load_config


ROOT = Path(__file__).parents[1]


def test_feed_dates_are_converted_to_beijing_and_cluster_gate_works() -> None:
    config = load_config()
    config["jobs"]["daily_news"]["sources"] = [{"name": "Official", "url": str(ROOT / "tests/fixtures/news_feed.xml"), "kind": "official"}]
    rows, errors = fetch_sources(config)
    assert not errors
    assert rows[0].published_at == "2026-08-25T09:30:00+08:00"
    events = cluster_articles(rows)
    assert events[0].confirmed is True

    media = [
        NewsArticle("Same AI launch event", "https://a.example/1", "2026-08-25T09:00:00+08:00", "", "A", "media", "a.example"),
        NewsArticle("Same AI launch event details", "https://b.example/2", "2026-08-25T10:00:00+08:00", "", "B", "media", "b.example"),
    ]
    assert cluster_articles(media)[0].confirmed is True

