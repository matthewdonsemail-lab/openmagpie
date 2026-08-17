"""httpx-based GitHub Search API client.

Thin wrapper around the free GitHub REST API for repository search
(`GET /search/repositories`). No authentication required for the
public endpoint (60 requests/hour); a `GITHUB_TOKEN` env var upgrades
to 5,000 requests/hour and raises the search-result cap.

Follows the Twitter connector's pattern: one client per search call,
httpx is thread-safe for read-only operations, and the connector
bridges the sync httpx client with the sync poll iterator.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from .errors import GitHubError, map_github_error

log = logging.getLogger("sources.github")

# GitHub Search API base URL.
GITHUB_API_URL = "https://api.github.com"

# Rate limits: unauthenticated 60 req/hr, authenticated 5,000 req/hr.
# The search endpoint has a separate but matching cap.
_GITHUB_USER_AGENT = "openmagpie-github/1.0 (+https://github.com/obris-dev/openmagpie)"


class GitHubClient:
    """Thin wrapper around httpx for the GitHub Search API.

    No auth state: public repos are searchable without a token. One
    `GITHUB_TOKEN` env var for authenticated access (higher rate limit,
    more complete results).

    The connector's `poll` is sync; httpx.Client is sync, so no
    asyncio bridge needed (unlike the Twitter twikit client).
    """

    def __init__(self, *, token: str | None = None, timeout: float = 30.0) -> None:
        self._token = token or os.environ.get("GITHUB_TOKEN", "").strip() or None
        self._timeout = timeout

    def search_repositories(
        self,
        query: str,
        sort: str = "stars",
        order: str = "desc",
        per_page: int = 20,
    ) -> dict[str, Any]:
        """Run one repository search; returns the full JSON response dict.

        GET /search/repositories?q={query}&sort={sort}&order={order}&per_page={per_page}

        Args:
            query: GitHub search qualifiers (e.g. "openai-sdk language:typescript").
            sort: "stars" (default) or "updated".
            order: "desc" (default) or "asc".
            per_page: results per page (1-100; default 20).

        Returns:
            The parsed JSON response with keys: total_count, incomplete_results, items.

        Raises:
            GitHubError: Wrapped from httpx exceptions (see map_github_error).
        """
        headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": _GITHUB_USER_AGENT}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        url = f"{GITHUB_API_URL}/search/repositories"
        params: dict[str, str | int] = {"q": query, "sort": sort, "order": order, "per_page": per_page}

        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.get(url, headers=headers, params=params)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            err = map_github_error(exc, {"query": query, "sort": sort, "order": order, "per_page": per_page})
            log.warning(
                "github search failed query=%r status=%d code=%s retryable=%s: %s",
                query,
                exc.response.status_code,
                err.code,
                err.retryable,
                err.message,
            )
            raise err from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            err = map_github_error(exc, {"query": query, "sort": sort, "order": order, "per_page": per_page})
            log.warning(
                "github search failed query=%r code=%s retryable=%s: %s",
                query,
                err.code,
                err.retryable,
                err.message,
            )
            raise err from exc
