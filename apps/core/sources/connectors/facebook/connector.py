"""Facebook connector: polls pages/groups and yields NewFacebookPostPayloads."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime
from typing import ClassVar

from openmagpie_schema.configs import FacebookSearchSourceSpec
from sources.connectors.base import BaseConnector
from sources.payloads import SourcePayload

from .client import FacebookClient
from .payloads import NewFacebookPostPayload


class FacebookConnector(BaseConnector[FacebookSearchSourceSpec]):
    """Connector that polls Facebook pages/groups via Playwright."""

    kind: ClassVar[str] = "facebook_search"
    payloads: ClassVar[list[type[SourcePayload]]] = [NewFacebookPostPayload]

    def poll(
        self,
        spec: FacebookSearchSourceSpec,
        since: datetime | None = None,
        field_map: dict[str, str] | None = None,
        heartbeat: Callable[[], bool] | None = None,
    ) -> Iterator[SourcePayload]:
        """Fetch posts from a Facebook page/group newer than `since`."""
        client = FacebookClient(
            headless=getattr(spec, "headless", True),
            timeout_ms=getattr(spec, "timeout_ms", 30_000),
            scroll_limit=getattr(spec, "scroll_limit", 5),
            cookies_file=getattr(spec, "cookies_file", None) or None,
        )

        try:
            posts = client.fetch_page_posts(spec.page_url)
        except Exception:
            return

        terms: list[str] = getattr(spec, "terms", []) or []
        count = 0
        max_count = getattr(spec, "count", 20)

        for post in posts:
            if since is not None and post.occurred_at < since:
                continue
            if terms and not any(term.lower() in post.content.lower() for term in terms):
                continue
            if count >= max_count:
                break
            yield post
            count += 1

        if heartbeat:
            heartbeat()
