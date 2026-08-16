"""Facebook payloads: a post observed via Playwright scraping."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from openmagpie_schema.configs import FacebookSearchSourceSpec
from sources.payloads import SourcePayload


class NewFacebookPostPayload(SourcePayload):
    """A single Facebook post observed by a watched page/group stream.

    `author` is the page/person name; `author_id` is the within-kind source
    slug (grouping items by producing account). `content` is the post's
    full text (the engine's judgeable body). Metrics are likes, comments,
    shares.
    """

    PAYLOAD_KIND: ClassVar[str] = "new_facebook_post"

    author: str = ""
    author_id: str = ""
    lang: str = ""
    metrics: dict[str, int | None] = {}

    model_config = {"frozen": True, "extra": "ignore"}

    def source_slug(self) -> str | None:
        return self.author_id or None

    @classmethod
    def sample(cls, variant: int = 0) -> NewFacebookPostPayload:
        n = variant + 1
        post_id = str(999_000_000_000_000_000 + n)
        author_id = f"example_page_{n}"
        return cls(
            external_id=post_id,
            kind=cls.PAYLOAD_KIND,
            occurred_at=datetime(2026, 5, 27, 12, 0, tzinfo=UTC),
            source=FacebookSearchSourceSpec.SOURCE_KIND,
            title="",
            content=f"Example Facebook post {n}: the post text that matched this watch.",
            url=f"https://facebook.com/{author_id}/posts/{post_id}",
            author=f"Example Page {n}",
            author_id=author_id,
            lang="en",
            metrics={"likes": 100 + n, "comments": 20 + n, "shares": 5 + n},
        )
