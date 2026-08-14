"""httpx-based GitHub Events API client.

Polls `GET /events` (the public firehose) and enriches qualifying repos
via `GET /repos/{owner}/{repo}` + `GET /repos/{owner}/{repo}/readme` (raw
media type). No authentication required for the public `/events` endpoint
(60 req/hr); a `GITHUB_TOKEN` env var upgrades to 5,000 req/hr and raises
the event-stream rate limit.

Error translation reuses the GitHub Search connector's taxonomy
(`..github_search.errors`) — both hit the same GitHub REST surface, so
the canonical codes/actions stay in one place.
"""

from __future__ import annotations

import logging
import os

import httpx

# Reuse the GitHub Search connector's error taxonomy: same REST surface,
# same canonical codes / retry semantics. One source of truth for GitHub.
from ..github_search.errors import map_github_error

log = logging.getLogger("sources.github_events")

# GitHub Events API base URL.
GITHUB_API_URL = "https://api.github.com"

_GITHUB_USER_AGENT = "openmagpie-github-events/1.0 (+https://github.com/obris-dev/openmagpie)"


class GitHubEventsClient:
    """Thin wrapper around httpx for the GitHub Events API.

    No auth state: the public `/events` endpoint works without a token. One
    `GITHUB_TOKEN` env var for authenticated access (higher rate limit).

    Each method is a single bounded HTTP call (`poll_events` -> one GET; the
    repo endpoints -> one GET each), so the connector's "one call per action"
    contract holds at the client boundary.
    """

    def __init__(self, *, token: str | None = None, timeout: float = 30.0) -> None:
        self._token = token or os.environ.get("GITHUB_TOKEN", "").strip() or None
        self._timeout = timeout

    def _headers(self, *, raw_readme: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github.raw+json" if raw_readme else "application/vnd.github+json",
            "User-Agent": _GITHUB_USER_AGENT,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def poll_events(self, etag: str | None = None) -> httpx.Response:
        """GET /events, optionally conditional on a stored ETag.

        Returns the RAW response so the connector can read both status
        (304 Not Modified = nothing new, a valid poll result) and the
        response ETag / X-Poll-Interval headers, then parse the JSON body.
        The body is buffered (no stream=True), so returning the response
        after the client context closes is safe.

        Raises GitHubError on a non-304 transport/status failure.
        """
        headers = self._headers()
        if etag:
            headers["If-None-Match"] = etag
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.get(f"{GITHUB_API_URL}/events", headers=headers)
                if response.status_code == 304:
                    return response
                response.raise_for_status()
                return response
        except httpx.HTTPStatusError as exc:
            err = map_github_error(exc, {"endpoint": "/events", "etag": etag})
            log.warning(
                "github events poll failed status=%d code=%s retryable=%s: %s",
                exc.response.status_code,
                err.code,
                err.retryable,
                err.message,
            )
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            err = map_github_error(exc, {"endpoint": "/events", "etag": etag})
            log.warning("github events poll failed code=%s: %s", err.code, err.message)
            raise

    def fetch_repo_meta(self, repo: str) -> dict | None:
        """GET /repos/{owner}/{repo}: stars, language, topics, description,
        license, timestamps, default_branch. None on 404 (deleted/private
        since the event fired).
        """
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.get(
                    f"{GITHUB_API_URL}/repos/{repo}", headers=self._headers()
                )
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            err = map_github_error(exc, {"endpoint": f"/repos/{repo}"})
            log.warning("github events repo meta failed repo=%s code=%s: %s", repo, err.code, err.message)
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            err = map_github_error(exc, {"endpoint": f"/repos/{repo}"})
            log.warning("github events repo meta failed repo=%s: %s", repo, err.message)
            raise

    def fetch_readme(self, repo: str) -> str | None:
        """GET /repos/{owner}/{repo}/readme as raw text. None on 404 (no README)."""
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.get(
                    f"{GITHUB_API_URL}/repos/{repo}/readme", headers=self._headers(raw_readme=True)
                )
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.text
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            err = map_github_error(exc, {"endpoint": f"/repos/{repo}/readme"})
            log.warning("github events readme failed repo=%s code=%s: %s", repo, err.code, err.message)
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            err = map_github_error(exc, {"endpoint": f"/repos/{repo}/readme"})
            log.warning("github events readme failed repo=%s: %s", repo, err.message)
            raise
