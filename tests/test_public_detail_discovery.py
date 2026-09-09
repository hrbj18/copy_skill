from __future__ import annotations

import time

from douyin_intelligence.public_detail_discovery import PublicDetailDiscovery, detail_query


def _settings() -> dict:
    return {
        "max_queries": 2,
        "max_results_per_query": 2,
        "request_timeout_seconds": 5,
        "max_response_bytes": 4096,
        "max_total_bytes": 8192,
        "min_detail_chars": 20,
        "freshness_days": 3,
        "freshness_grace_hours": 12,
    }


def test_detail_query_prefers_structured_anchor_then_cleans_hook() -> None:
    assert detail_query({"event_slots": {"subject": "DeepSeek", "object": "多模态模型", "action": "开源"}}) == "DeepSeek 多模态模型 开源"
    assert "福利" not in detail_query({"title": "福利！某个热门开源项目来了 #AI"})


def test_public_detail_discovery_accepts_a_fresh_relevant_headline_when_rss_summary_repeats_title(monkeypatch) -> None:
    body = '''<?xml version="1.0"?><rss><channel><item><title>端侧首个百万上下文 科大讯飞星火X2.5-4B、1.7B开源</title><link>https://news.google.com/rss/articles/1</link><description>端侧首个百万上下文 科大讯飞星火X2.5-4B、1.7B开源 新浪财经</description><pubDate>Tue, 01 Sep 2026 05:24:00 GMT</pubDate></item></channel></rss>'''.encode("utf-8")

    class Response:
        content = body

        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, _url: str, **_kwargs: object) -> Response:
            return Response()

    monkeypatch.setattr("douyin_intelligence.public_detail_discovery.httpx.Client", Client)
    finder = PublicDetailDiscovery(_settings(), deadline=time.monotonic() + 5, business_date="2026-09-01", timezone="Asia/Shanghai")
    result = finder({"story_id": "story-a", "title": "1.7B 模型也能跑百万上下文了", "event_slots": {}})
    assert result["audit"]["status"] == "success"


def test_public_detail_discovery_keeps_only_fresh_relevant_rss_results(monkeypatch) -> None:
    body = b'''<?xml version="1.0"?><rss><channel>
    <item><title>DeepSeek opens a multimodal model</title><link>https://news.google.com/rss/articles/1</link><description>&lt;b&gt;DeepSeek&lt;/b&gt; released a multimodal model with image understanding features for developers.</description><pubDate>Tue, 01 Sep 2026 08:00:00 GMT</pubDate><source url="https://example.test">Example Tech</source></item>
    <item><title>Old DeepSeek model news</title><link>https://news.google.com/rss/articles/2</link><description>DeepSeek released an older multimodal model with detailed capabilities.</description><pubDate>Tue, 01 Jul 2026 08:00:00 GMT</pubDate><source url="https://old.test">Old Tech</source></item>
    </channel></rss>'''

    class Response:
        content = body

        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, _url: str, **_kwargs: object) -> Response:
            return Response()

    monkeypatch.setattr("douyin_intelligence.public_detail_discovery.httpx.Client", Client)
    finder = PublicDetailDiscovery(_settings(), deadline=time.monotonic() + 5, business_date="2026-09-01", timezone="Asia/Shanghai")
    result = finder({"story_id": "story-a", "title": "DeepSeek 多模态模型开源", "event_slots": {}})
    assert result["audit"]["status"] == "success"
    assert result["evidence"][0]["method"] == "public_news_snippet"
    assert "index_url" not in result["evidence"][0]
    assert "<b>" not in result["evidence"][0]["text"]
    assert finder.report()["outcomes"][0]["results"][0]["index_url"] == "https://news.google.com/rss/articles/1"
    assert finder.report()["outcomes"][0]["discarded"][0]["reason"] == "outside_business_window"


def test_public_detail_discovery_rejects_an_unrelated_fresh_result(monkeypatch) -> None:
    body = '''<?xml version="1.0"?><rss><channel><item><title>苹果发布新品</title><link>https://news.google.com/rss/articles/1</link><description>苹果发布了一款新产品，带来多项功能更新。</description><pubDate>Tue, 01 Sep 2026 08:00:00 GMT</pubDate></item></channel></rss>'''.encode("utf-8")

    class Response:
        content = body

        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, _url: str, **_kwargs: object) -> Response:
            return Response()

    monkeypatch.setattr("douyin_intelligence.public_detail_discovery.httpx.Client", Client)
    finder = PublicDetailDiscovery(_settings(), deadline=time.monotonic() + 5, business_date="2026-09-01", timezone="Asia/Shanghai")
    result = finder({"story_id": "story-a", "title": "MiniMax H3正式开源", "event_slots": {}})
    assert result["audit"]["status"] == "empty"
    assert result["audit"]["discarded"][0]["reason"] == "query_not_supported"


def test_public_detail_discovery_stops_at_query_budget() -> None:
    finder = PublicDetailDiscovery(_settings() | {"max_queries": 0}, deadline=time.monotonic() + 5, business_date="2026-09-01", timezone="Asia/Shanghai")
    result = finder({"story_id": "story-a", "title": "DeepSeek 多模态模型开源", "event_slots": {}})
    assert result["audit"]["status"] == "skipped"
    assert result["audit"]["reason"] == "query_budget_exhausted"
