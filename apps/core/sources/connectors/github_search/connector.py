"""GitHub repository search connector via the public Search API.

Polls a `github_search` source: one live GitHub repository search per
cycle via the httpx client, mapping each result item to a `NewRepoPayload`
newer than the source's `since` watermark.

Error semantics follow the connector contract: any GitHub API failure is
raised as `ConnectorParseError` (a `_RECOVERABLE_ERRORS` member at the
poll seam), so a bad source logs + skips instead of aborting the feed
cycle. The source's watermark stays put on failure, so the next cycle
re-reads from the same point and the external_id dedup absorbs anything
already recorded.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import datetime

from openmagpie_schema.configs import GitHubSearchSourceSpec
from sources.payload_registry import register
from sources.payloads import SourcePayload

from ..base import BaseConnector, ConnectorParseError
from .client import GitHubClient
from .errors import GitHubError
from .payloads import NewRepoPayload

log = logging.getLogger("sources.github")


class GitHubSearchConnector(BaseConnector[GitHubSearchSourceSpec]):
    """Polls one GitHub repository search stream via the public Search API.

    Live-mode semantics mirror the other connectors: every cycle yields
    repos newer than `since` (the Source row's `last_event_at`). There is
    no pagination in phase 1: a search returns up to `spec.count` repos
    and the connector filters them by the watermark (GitHub's sort ordering
    keeps the highest-starred / most-recently-updated first; a quiet stream
    needs no backfill walk). Phase 1 walks one page only; multi-page
    backfill is a follow-up for when the pool is large enough to warrant it.
    """

    kind = GitHubSearchSourceSpec.SOURCE_KIND
    payloads: list[type[SourcePayload]] = [NewRepoPayload]

    # One stateless client; token resolved from env in the client.
    _client = GitHubClient()

    def poll(
        self,
        spec: GitHubSearchSourceSpec,
        since: datetime | None,
        field_map: dict[str, str] | None = None,
        heartbeat: Callable[[], bool] | None = None,
    ) -> Iterator[SourcePayload]:
        del field_map
        del heartbeat

        # Build the query: fold min_stars into the GitHub search expression
        # so the pre-filter happens server-side, not in the client.
        query = spec.query
        if spec.min_stars > 0:
            query = f"{query} stars:>={spec.min_stars}"

        try:
            data = self._client.search_repositories(
                query=query,
                sort=spec.sort,
                order="desc",
                per_page=min(spec.count, 100),
            )
        except GitHubError as exc:
            log.warning(
                "github search failed query=%r code=%s retryable=%s: %s",
                spec.query,
                exc.code,
                exc.retryable,
                exc.message,
            )
            raise ConnectorParseError(
                f"github search {spec.display()} failed: {exc.code}: {exc.message} ({exc.action})"
            ) from exc

        items = data.get("items", []) or []
        for repo in items:
            payload = NewRepoPayload.from_repo(repo)
            # Watermark filter: only surface repos strictly newer than the
            # cursor (the poll op advances the source watermark to the
            # newest seen, so a repo at the watermark is already recorded).
            if since is not None and payload.occurred_at <= since:
                continue
            yield payload


register(GitHubSearchConnector.kind, GitHubSearchConnector.payloads)
