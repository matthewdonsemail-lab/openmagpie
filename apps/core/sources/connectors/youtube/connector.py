"""YouTube search connector using yt-dlp.

Polls a `youtube_search` source: one live YouTube search per cycle via
the yt-dlp client, mapping each result video to a `NewVideoPayload`
newer than the source's `since` watermark.

Error semantics follow the connector contract: any YouTube/yt-dlp
failure is raised as `ConnectorParseError` (a `_RECOVERABLE_ERRORS`
member at the poll seam), so a bad source logs + skips instead of
aborting the feed cycle. The source's watermark stays put on failure,
so the next cycle re-reads from the same point and the external_id
dedup absorbs anything already recorded.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import datetime

from openmagpie_schema.configs import YouTubeSearchSourceSpec
from sources.payload_registry import register
from sources.payloads import SourcePayload

from ..base import BaseConnector, ConnectorParseError
from .client import YtDlpClient
from .errors import YouTubeError
from .payloads import NewVideoPayload

log = logging.getLogger("sources.youtube")


class YouTubeSearchConnector(BaseConnector[YouTubeSearchSourceSpec]):
    """Polls one YouTube search stream via yt-dlp.

    Live-mode semantics mirror the other connectors: every cycle yields
    videos newer than `since` (the Source row's `last_event_at`). There
    is no pagination in phase 1: a search returns up to `spec.count`
    videos and the connector filters them by the watermark (YouTube's
    search ordering is newest-first; a quiet stream needs no backfill
    walk).
    """

    kind = YouTubeSearchSourceSpec.SOURCE_KIND
    payloads: list[type[SourcePayload]] = [NewVideoPayload]

    # One stateless client; no auth needed for public search.
    _client = YtDlpClient()

    def poll(
        self,
        spec: YouTubeSearchSourceSpec,
        since: datetime | None,
        field_map: dict[str, str] | None = None,
        heartbeat: Callable[[], bool] | None = None,
    ) -> Iterator[SourcePayload]:
        del field_map
        del heartbeat
        try:
            results = self._client.search(spec.query, spec.count)
        except YouTubeError as exc:
            log.warning(
                "youtube search failed query=%r code=%s retryable=%s: %s",
                spec.query,
                exc.code,
                exc.retryable,
                exc.message,
            )
            raise ConnectorParseError(
                f"youtube search {spec.display()} failed: {exc.code}: {exc.message} ({exc.action})"
            ) from exc

        for video in results:
            payload = NewVideoPayload.from_video(video)
            # Watermark filter: only surface videos strictly newer than the
            # cursor (the poll op advances the source watermark to the
            # newest seen, so a video at the watermark is already recorded).
            if since is not None and payload.occurred_at <= since:
                continue
            yield payload


register(YouTubeSearchConnector.kind, YouTubeSearchConnector.payloads)
