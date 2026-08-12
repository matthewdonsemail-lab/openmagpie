"""yt-dlp-based YouTube client for search extraction.

Wraps yt-dlp's YoutubeDL to perform YouTube searches without downloading
video content. Uses extract_flat mode for efficiency and handles errors
via the error taxonomy in errors.py.

Key patterns (ported from listeningkit Twitter client):
- One YtDlpClient instance per search call; yt-dlp is thread-safe for
  read-only extraction operations.
- Search queries use the `ytsearch<N>:<query>` URI scheme.
- Results are returned as dicts (not downloaded), containing metadata.
- No authentication required for public search; cookies optional for
  age-restricted content.
"""

from __future__ import annotations

import logging
from typing import Any

import yt_dlp

from .errors import YouTubeError, map_ytdlp_error

log = logging.getLogger("sources.youtube")

# Maximum results per search query. yt-dlp accepts up to 100 but we cap
# lower to match the Twitter connector's default count.
MAX_SEARCH_RESULTS = 50


class YtDlpClient:
    """Thin wrapper around yt-dlp for search-only extraction.

    No auth state: YouTube search is public. Optional cookie file can be
    passed for age-restricted content (not commonly needed for search).
    """

    def __init__(
        self,
        *,
        quiet: bool = True,
        no_warnings: bool = True,
        cookie_file: str | None = None,
    ) -> None:
        self._quiet = quiet
        self._no_warnings = no_warnings
        self._cookie_file = cookie_file

    def _build_opts(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": self._quiet,
            "no_warnings": self._no_warnings,
            "extract_flat": False,  # need full metadata for payloads
            "skip_download": True,
        }
        if self._cookie_file:
            opts["cookies"] = self._cookie_file
        return opts

    def search(
        self,
        query: str,
        count: int = 20,
    ) -> list[dict[str, Any]]:
        """Run one YouTube search; returns list of video info dicts.

        Args:
            query: Search expression (keywords, phrases).
            count: Max results to fetch (capped at MAX_SEARCH_RESULTS).

        Returns:
            List of video metadata dicts, newest first.

        Raises:
            YouTubeError: On extraction failures (mapped from yt-dlp exceptions).
        """
        capped_count = min(count, MAX_SEARCH_RESULTS)
        search_uri = f"ytsearch{capped_count}:{query}"

        try:
            with yt_dlp.YoutubeDL(self._build_opts()) as ydl:
                info = ydl.extract_info(search_uri, download=False)
                entries = info.get("entries", []) or []
                return [e for e in entries if e is not None]
        except Exception as exc:
            err = map_ytdlp_error(exc, {"query": query, "count": capped_count})
            log.warning("youtube search failed query=%r code=%s: %s", query, err.code, err.message)
            raise YouTubeError(
                code=err.code,
                message=err.message,
                retryable=err.retryable,
                action=err.action,
                context=err.context,
            ) from exc
