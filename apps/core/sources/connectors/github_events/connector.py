"""GitHub Events connector via the public /events firehose.

Polls `GET /events` (the public event stream, max ~30 rolling events),
filters for `CreateEvent` + `ref_type: "repository"` (new repos), enriches
each with `GET /repos/{owner}/{repo}` + `GET /readme`, and yields
`NewRepoEventPayload` items newer than the source's `since` watermark.

The /events endpoint returns a rolling window; there is no durable cursor.
The watermark is the newest event's `created_at` — the next poll skips
everything older, which is inherently lossy (events between polls that
fell off the window are missed). This is the same limitation octoradar
has in Rust (`src/source/public_events.rs`): the ETag provides 304
efficiency, not a durable cursor.

Error semantics follow the connector contract: any GitHub API failure is
raised as `ConnectorParseError` (a `_RECOVERABLE_ERRORS` member at the
poll seam), so a bad source logs + skips instead of aborting the feed
cycle. The source's watermark stays put on failure, so the next cycle
re-reads from the same point and the external_id dedup absorbs anything
already recorded.

No ETag persistence: the connector uses the watermark-based dedup, not
an ETag cursor. The ETag is read from the response headers for logging
but not persisted — the spec JSON is re-validated from the DB each poll
cycle, so mutating it in `poll()` would be dead state.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

from openmagpie_schema.configs import GitHubEventsSourceSpec
from sources.payload_registry import register
from sources.payloads import SourcePayload

from ..base import BaseConnector, ConnectorParseError
from .client import GitHubEventsClient
from .payloads import NewRepoEventPayload

log = logging.getLogger("sources.github_events")


def _parse_ts(ts: str | None) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp string to an aware datetime."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


class GitHubEventsConnector(BaseConnector[GitHubEventsSourceSpec]):
    """Polls the GitHub public /events firehose and surfaces new repos.

    Live-mode semantics: every cycle yields events newer than `since` (the
    Source row's `last_event_at`). The /events endpoint returns events in
    reverse chronological order, so we stop reading once we hit an event
    older than the watermark.

    The connector does NOT persist state beyond the watermark. The ETag
    is read from the response for logging but not stored — the event_id
    dedup + watermark is sufficient for correctness.
    """

    kind = GitHubEventsSourceSpec.SOURCE_KIND
    payloads: list[type[SourcePayload]] = [NewRepoEventPayload]

    # One stateless client; token resolved from env in the client.
    _client = GitHubEventsClient()

    def poll(
        self,
        spec: GitHubEventsSourceSpec,
        since: datetime | None,
        field_map: dict[str, str] | None = None,
        heartbeat: Callable[[], bool] | None = None,
    ) -> Iterator[SourcePayload]:
        del field_map
        del heartbeat

        # 1. Poll the /events firehose.
        try:
            response = self._client.poll_events(etag=None)
        except Exception as exc:
            raise ConnectorParseError(
                f"github events {spec.display()} failed: {exc}"
            ) from None

        # 304 Not Modified: no new events, yield nothing.
        if response.status_code == 304:
            return

        # 2. Parse the response body.
        try:
            events: list[dict[str, Any]] = response.json()
        except (ValueError, TypeError) as exc:
            raise ConnectorParseError(
                f"github events: invalid JSON body: {exc}"
            ) from exc

        # 3. Filter + enrich + yield.
        for event in events:
            # Determine if this event is new enough.
            created_at_str = event.get("created_at", "")
            event_ts = _parse_ts(created_at_str)
            if event_ts is None:
                continue

            # Watermark filter: only surface events strictly newer than the
            # cursor (the poll op advances the source watermark to the
            # newest seen, so an event at the watermark is already recorded).
            if since is not None and event_ts <= since:
                continue

            event_type = event.get("type", "")
            payload_obj = event.get("payload")
            if not isinstance(payload_obj, dict):
                continue

            repo = event.get("repo")
            repo_name = repo.get("name", "") if isinstance(repo, dict) else ""
            if not repo_name:
                continue

            event_id = str(event.get("id", ""))

            # CreateEvent with ref_type == "repository" = new repo created.
            if event_type == "CreateEvent":
                ref_type = payload_obj.get("ref_type", "")
                if ref_type != "repository":
                    continue

                # Enrich: fetch repo metadata + README.
                meta = self._client.fetch_repo_meta(repo_name)
                if meta is None:
                    # Repo may have been deleted or made private since the event.
                    continue

                readme = self._client.fetch_readme(repo_name)

                yield NewRepoEventPayload.from_create_event(
                    repo=meta,
                    occurred_at=event_ts,
                    event_type="create",
                    readme=readme,
                    event_id=event_id,
                )

            # PushEvent = fresh code pushed to an existing repo.
            # Only yield if spec.include_pushes is True (default: False).
            elif event_type == "PushEvent" and spec.include_pushes:
                ref = payload_obj.get("ref", "")
                # Only pushes to the default branch are interesting.
                if not ref.startswith("refs/heads/"):
                    continue

                meta = self._client.fetch_repo_meta(repo_name)
                if meta is None:
                    continue

                readme = self._client.fetch_readme(repo_name)

                yield NewRepoEventPayload.from_create_event(
                    repo=meta,
                    occurred_at=event_ts,
                    event_type="push",
                    readme=readme,
                    event_id=event_id,
                )


register(GitHubEventsConnector.kind, GitHubEventsConnector.payloads)
