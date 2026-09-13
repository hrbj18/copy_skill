from __future__ import annotations

"""Runtime adapter for MediaCrawler.

This file is executed by MediaCrawler's isolated Python.  It changes runtime
configuration only; the vendored upstream checkout remains untouched.
"""

import argparse
import os
import runpy
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable


async def reuse_existing_page(
    context: Any,
    original_new_page: Callable[[Any], Awaitable[Any]],
) -> tuple[Any, dict[str, int | bool]]:
    """Reuse the single project page instead of opening another visible page."""
    pages = [page for page in list(getattr(context, "pages", []) or []) if not page.is_closed()]
    before = len(pages)
    if pages:
        page = pages[0]
        reused = True
    else:
        page = await original_new_page(context)
        reused = False
    after = len([value for value in list(getattr(context, "pages", []) or []) if not value.is_closed()])
    return page, {"pages_before": before, "pages_after": after, "reused_existing_page": reused}


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--crawler-root", required=True)
    parser.add_argument("--cdp-port", type=int, required=True)
    parser.add_argument("--navigation-timeout", type=int, default=90)
    parser.add_argument("--publish-time-type", type=int, choices=(0, 1, 7, 180), default=0)
    known, remainder = parser.parse_known_args()
    if remainder and remainder[0] == "--":
        remainder = remainder[1:]

    root = Path(known.crawler_root).resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    # MediaCrawler's CDP discovery uses HTTP clients that may honor the host
    # system proxy.  Local debugging endpoints must never leave this machine.
    local_bypass = "localhost,127.0.0.1"
    os.environ["NO_PROXY"] = local_bypass
    os.environ["no_proxy"] = local_bypass

    import config  # type: ignore
    from playwright.async_api import BrowserContext, Page  # type: ignore
    from media_platform.douyin.client import DouYinClient  # type: ignore

    config.ENABLE_CDP_MODE = True
    config.CDP_CONNECT_EXISTING = True
    config.CDP_DEBUG_PORT = known.cdp_port
    config.BROWSER_LAUNCH_TIMEOUT = max(10, known.navigation_timeout)
    config.PUBLISH_TIME_TYPE = known.publish_time_type

    async def bounded_creator_posts(self, sec_user_id, callback=None):
        """Honor CRAWLER_MAX_NOTES_COUNT, which upstream creator mode ignores."""
        posts_has_more = 1
        max_cursor = ""
        result = []
        limit = max(1, int(config.CRAWLER_MAX_NOTES_COUNT))
        while posts_has_more == 1 and len(result) < limit:
            response = await self.get_user_aweme_posts(sec_user_id, max_cursor)
            posts_has_more = response.get("has_more", 0)
            max_cursor = response.get("max_cursor")
            items = response.get("aweme_list") or []
            remaining = limit - len(result)
            items = items[:remaining]
            if callback and items:
                await callback(items)
            result.extend(items)
        return result

    DouYinClient.get_all_user_aweme_posts = bounded_creator_posts

    original_new_page = BrowserContext.new_page

    async def project_owned_new_page(self):
        page, audit = await reuse_existing_page(self, original_new_page)
        print(
            "COPY_SKILL_CDP_AUDIT "
            f"pages_before={audit['pages_before']} pages_after={audit['pages_after']} "
            f"reused_existing_page={int(bool(audit['reused_existing_page']))}"
        )
        return page

    BrowserContext.new_page = project_owned_new_page

    original_goto = Page.goto

    async def resilient_goto(self, url, **kwargs):
        kwargs.setdefault("wait_until", "domcontentloaded")
        kwargs.setdefault("timeout", known.navigation_timeout * 1000)
        return await original_goto(self, url, **kwargs)

    Page.goto = resilient_goto
    sys.argv = [str(root / "main.py"), *remainder]
    runpy.run_path(str(root / "main.py"), run_name="__main__")


if __name__ == "__main__":
    main()
