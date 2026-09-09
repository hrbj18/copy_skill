from __future__ import annotations

import asyncio

from douyin_intelligence.mediacrawler_runner import reuse_existing_page


class FakePage:
    def __init__(self, closed: bool = False):
        self.closed = closed

    def is_closed(self) -> bool:
        return self.closed


class FakeContext:
    def __init__(self, pages):
        self.pages = list(pages)


def test_runner_reuses_existing_project_page_without_creating_second_page() -> None:
    existing = FakePage()
    context = FakeContext([existing])

    async def forbidden(_context):
        raise AssertionError("must not create another page")

    page, audit = asyncio.run(reuse_existing_page(context, forbidden))
    assert page is existing
    assert audit == {"pages_before": 1, "pages_after": 1, "reused_existing_page": True}


def test_runner_creates_one_page_only_when_context_is_empty() -> None:
    context = FakeContext([])

    async def create(value):
        page = FakePage()
        value.pages.append(page)
        return page

    page, audit = asyncio.run(reuse_existing_page(context, create))
    assert page is context.pages[0]
    assert audit == {"pages_before": 0, "pages_after": 1, "reused_existing_page": False}

