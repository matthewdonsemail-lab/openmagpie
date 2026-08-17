"""Error taxonomy for the GitHub (Search API) connector.

Maps httpx / GitHub API failures to canonical error shapes with retry
semantics, following the same pattern as the Twitter connector's
ListenerError and the YouTube connector's YouTubeError.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class GitHubError:
    """Canonical error shape for one GitHub API fetch failure."""

    code: str  # stable machine code
    message: str  # human-readable
    retryable: bool  # safe to retry with backoff?
    action: str  # what the ops layer should do
    context: dict[str, Any] = field(default_factory=dict)
    rate_limit_reset: int | None = None  # unix ts from X-RateLimit-Reset header


# httpx status -> canonical code. GitHub uses plain HTTP status codes on its
# REST search endpoint (no error-code envelope), so the status IS the code.
STATUS_CODES: dict[int, str] = {
    401: "unauthorized",
    403: "rate_limited",  # search API primary/secondary rate limit
    404: "not_found",
    422: "validation_failed",
    429: "rate_limited",
    500: "upstream_error",
    502: "upstream_error",
    503: "upstream_error",
    504: "upstream_error",
}

RETRYABLE_ACTIONS: dict[str, tuple[bool, str]] = {
    "unauthorized": (False, "check token / env config; do not retry as-is"),
    "rate_limited": (True, f"backoff until reset (X-RateLimit-Reset)" ),
    "not_found": (False, "no results / endpoint 404; skip"),
    "validation_failed": (False, "fix query / params; do not retry as-is"),
    "upstream_error": (True, "retry with backoff; alert after 5 consecutive"),
    "timeout": (True, "retry with backoff"),
    "network_error": (True, "retry with backoff"),
}


def map_github_error(exc: Exception, context: dict[str, Any] | None = None) -> GitHubError:
    """Translate an httpx / GitHub API exception into a canonical GitHubError."""

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        code = STATUS_CODES.get(status, "http_error")
        reset = exc.response.headers.get("X-RateLimit-Reset")
        retryable, action = RETRYABLE_ACTIONS.get(code, (status >= 500, "unknown HTTP error; log and retry on 5xx"))
        return GitHubError(
            code=code,
            message=str(exc),
            retryable=retryable,
            action=action,
            context=context or {},
            rate_limit_reset=int(reset) if reset and reset.isdigit() else None,
        )

    if isinstance(exc, httpx.TimeoutException):
        return GitHubError(
            code="timeout",
            message=str(exc),
            retryable=True,
            action="retry with backoff",
            context=context or {},
        )

    # Network / connection errors (httpx.TransportError)
    if isinstance(exc, httpx.TransportError):
        return GitHubError(
            code="network_error",
            message=str(exc),
            retryable=True,
            action="retry with backoff",
            context=context or {},
        )

    return GitHubError(
        code="github_error",
        message=str(exc),
        retryable=True,
        action="unknown error; log and retry with backoff",
        context=context or {},
    )
