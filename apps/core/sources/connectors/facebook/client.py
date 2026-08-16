"""Facebook client: Playwright-based scraping for pages and groups."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from typing import Any

from playwright.sync_api import Page, sync_playwright

from .payloads import NewFacebookPostPayload

FB_BASE_URL = "https://facebook.com"


class FacebookClient:
    """Sync Playwright client for observing Facebook posts."""

    def __init__(
        self,
        headless: bool = True,
        timeout_ms: int = 30_000,
        scroll_limit: int = 5,
        cookies_file: str | None = None,
    ) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.scroll_limit = scroll_limit
        self.cookies_file = cookies_file

    def _load_cookies(self, context) -> None:
        """Load cookies from JSON file into Playwright context."""
        if self.cookies_file and os.path.exists(self.cookies_file):
            with open(self.cookies_file, encoding="utf-8") as f:
                cookies = json.load(f)
            # Handle both raw list and Netscape-ish wrappers
            if isinstance(cookies, dict) and "cookies" in cookies:
                cookies = cookies["cookies"]
            context.add_cookies(cookies)

    def fetch_page_posts(self, page_url: str) -> list[NewFacebookPostPayload]:
        """Scroll a Facebook page/group and extract post payloads."""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            context = browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.0"
                ),
            )
            self._load_cookies(context)
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = context.new_page()
            page.set_default_timeout(self.timeout_ms)

            try:
                page.goto(page_url, wait_until="networkidle")
                # Dismiss cookie banner if present (EU / logged-out)
                try:
                    page.click(
                        '[data-testid="cookie-policy-manage-dialog-accept-button"]',
                        timeout=5_000,
                    )
                except Exception:
                    pass

                posts: list[NewFacebookPostPayload] = []
                seen_ids: set[str] = set()

                for _ in range(self.scroll_limit):
                    article_nodes = page.query_selector_all('div[role="article"]')
                    for node in article_nodes:
                        payload = self._extract_post(node, page_url)
                        if payload and payload.external_id not in seen_ids:
                            seen_ids.add(payload.external_id)
                            posts.append(payload)

                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(2_000)

                return posts
            finally:
                page.close()
                context.close()
                browser.close()

    @staticmethod
    def _extract_post_id(href: str) -> str:
        """Best-effort post ID extraction from a Facebook URL."""
        patterns = [
            r"/posts/(\d+)",
            r"/photos/[a-z.]*/(\d+)",
            r"/videos/[a-z.]*/(\d+)",
            r"story_fbid=(\d+)",
            r"/groups/[^/]+/posts/(\d+)",
        ]
        for pat in patterns:
            m = re.search(pat, href)
            if m:
                return m.group(1)
        nums = re.findall(r"\d+", href)
        return nums[-1] if nums else ""

    @staticmethod
    def _parse_count(text: str | None) -> int | None:
        """Parse '1.2K', '3M', '42' into int or None."""
        if not text:
            return None
        text = text.strip().lower().replace(",", "")
        multipliers = {"k": 1_000, "m": 1_000_000}
        for suffix, mult in multipliers.items():
            if suffix in text:
                try:
                    return int(float(text.replace(suffix, "")) * mult)
                except ValueError:
                    return None
        try:
            return int(text)
        except ValueError:
            return None

    def _extract_post(self, node: Any, page_url: str) -> NewFacebookPostPayload | None:
        """Extract a single post from a DOM node."""
        try:
            author_el = node.query_selector(
                "h3 a, h4 a, strong a, span a[href*='/']"
            )
            author = author_el.inner_text() if author_el else ""
            author_href = author_el.get_attribute("href") if author_el else ""
            author_id = author_href.strip("/").split("/")[-1] if author_href else ""

            content_el = node.query_selector(
                'div[data-ad-preview="message"], span[dir="auto"]'
            )
            content = content_el.inner_text() if content_el else ""

            link_el = node.query_selector(
                "a[href*='/posts/'], a[href*='story_fbid=']"
            )
            href = link_el.get_attribute("href") if link_el else ""
            post_id = self._extract_post_id(href)
            full_url = f"{FB_BASE_URL}{href}" if href.startswith("/") else href

            likes_el = node.query_selector(
                "span[aria-label*='Like'], span[aria-label*='like']"
            )
            comments_el = node.query_selector(
                "span[aria-label*='Comment'], span[aria-label*='comment']"
            )
            shares_el = node.query_selector(
                "span[aria-label*='Share'], span[aria-label*='share']"
            )

            likes = self._parse_count(
                likes_el.inner_text() if likes_el else None
            )
            comments = self._parse_count(
                comments_el.inner_text() if comments_el else None
            )
            shares = self._parse_count(
                shares_el.inner_text() if shares_el else None
            )

            return NewFacebookPostPayload(
                external_id=post_id or f"unknown_{datetime.now(UTC).timestamp()}",
                kind=NewFacebookPostPayload.PAYLOAD_KIND,
                occurred_at=datetime.now(UTC),
                source="facebook_search",
                title="",
                content=content,
                url=full_url or page_url,
                author=author,
                author_id=author_id,
                lang="",
                metrics={"likes": likes, "comments": comments, "shares": shares},
            )
        except Exception:
            return None

    def fetch_group_posts(self, group_url: str) -> list[NewFacebookPostPayload]:
        """Alias for fetch_page_posts — group feeds are structurally similar."""
        return self.fetch_page_posts(group_url)
