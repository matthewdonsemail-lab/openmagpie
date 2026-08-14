"""GitHub Events connector tests (offline, fake HTTP responses).

The connector's only I/O is the client (`GitHubEventsClient.poll_events`,
`fetch_repo_meta`, `fetch_readme`); these tests swap in a mock client and
pin: spec validation, the watermark filter, event type filtering, error
translation (GitHubError -> ConnectorParseError), and payload mapping
(API repo dict -> NewRepoEventPayload).
"""

from datetime import UTC, datetime
from unittest import mock

import httpx
from django.test import SimpleTestCase
from pydantic import ValidationError

from openmagpie_schema.configs import GitHubEventsSourceSpec
from sources.connectors.base import ConnectorParseError
from sources.connectors.github_events.connector import GitHubEventsConnector
from sources.connectors.github_events.payloads import NewRepoEventPayload


class _FakeResponse:
    """Duck-typed httpx.Response for the mock client."""

    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json_data = json_data or []
        self.headers = headers or {"ETag": '"abc"', "X-Poll-Interval": "60"}

    def json(self):
        return self._json_data


def _make_repo_meta(
    full_name: str = "alice/proj",
    stars: int = 100,
    language: str | None = "Python",
    topics: list[str] | None = None,
    description: str | None = "A test project",
    license_name: str = "MIT",
) -> dict:
    return {
        "full_name": full_name,
        "owner": {"login": "alice"},
        "stargazers_count": stars,
        "forks_count": 20,
        "open_issues_count": 5,
        "language": language,
        "topics": topics or ["llm", "test"],
        "description": description,
        "homepage": "",
        "license": {"spdx_id": license_name} if license_name else None,
        "default_branch": "main",
        "pushed_at": "2026-06-01T12:00:00Z",
        "created_at": "2026-06-01T12:00:00Z",
        "updated_at": "2026-06-01T12:00:00Z",
    }


def _make_event(
    event_id: str = "100",
    event_type: str = "CreateEvent",
    repo_name: str = "alice/proj",
    created_at: str = "2026-06-19T12:00:00Z",
    ref_type: str | None = "repository",
    description: str | None = "A new project",
    ref: str | None = None,
) -> dict:
    payload = {"ref_type": ref_type, "description": description} if ref_type else {}
    if ref is not None:
        payload["ref"] = ref
    return {
        "id": event_id,
        "type": event_type,
        "actor": {"login": "alice", "id": 12345, "display_login": "alice"},
        "repo": {"id": 987, "name": repo_name, "url": f"https://api.github.com/repos/{repo_name}"},
        "public": True,
        "created_at": created_at,
        "org": None,
        "payload": payload,
    }


class GitHubEventsSourceSpecTests(SimpleTestCase):
    def test_rejects_unknown_event_types(self):
        with self.assertRaises(ValidationError):
            GitHubEventsSourceSpec(kind="github_events", event_types=["fork"])

    def test_accepts_known_event_types(self):
        spec = GitHubEventsSourceSpec(kind="github_events", event_types=["create", "push"])
        self.assertEqual(spec.event_types, ["create", "push"])
        self.assertEqual(spec.include_pushes, False)
        self.assertEqual(spec.min_stars, 0)

    def test_defaults(self):
        spec = GitHubEventsSourceSpec(kind="github_events")
        self.assertEqual(spec.event_types, ["create"])
        self.assertEqual(spec.min_stars, 0)

    def test_display(self):
        spec = GitHubEventsSourceSpec(kind="github_events")
        self.assertIn("GitHub events radar", spec.display())


class GitHubEventsConnectorTests(SimpleTestCase):
    def _connector(self, events: list, meta: dict | None = None, readme: str | None = None):
        """Create a connector with a mock client returning canned responses."""
        client = mock.Mock()
        client.poll_events.return_value = _FakeResponse(200, json_data=events)

        def _fetch_meta(repo_name):
            return meta or _make_repo_meta(full_name=repo_name)

        client.fetch_repo_meta.side_effect = _fetch_meta
        client.fetch_readme.return_value = readme
        conn = GitHubEventsConnector()
        conn._client = client
        return conn, client

    def test_yields_payloads_newer_than_since(self):
        spec = GitHubEventsSourceSpec(kind="github_events")
        events = [
            _make_event("100", created_at="2026-06-19T12:00:00Z", repo_name="alice/new-project"),
            _make_event("99", created_at="2026-06-19T11:00:00Z", repo_name="bob/older"),
        ]
        conn, _client = self._connector(events)
        payloads = list(conn.poll(spec, since=datetime(2026, 6, 19, 11, 30, tzinfo=UTC)))
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0].full_name, "alice/new-project")

    def test_filters_out_old_events(self):
        spec = GitHubEventsSourceSpec(kind="github_events")
        events = [
            _make_event("100", created_at="2026-06-19T10:00:00Z", repo_name="alice/old"),
        ]
        conn, _client = self._connector(events)
        payloads = list(conn.poll(spec, since=datetime(2026, 6, 19, 12, 0, tzinfo=UTC)))
        self.assertEqual(len(payloads), 0)

    def test_304_returns_empty(self):
        spec = GitHubEventsSourceSpec(kind="github_events")
        client = mock.Mock()
        client.poll_events.return_value = _FakeResponse(304)
        conn = GitHubEventsConnector()
        conn._client = client
        payloads = list(conn.poll(spec, since=None))
        self.assertEqual(len(payloads), 0)

    def test_skips_non_create_events(self):
        spec = GitHubEventsSourceSpec(kind="github_events")
        events = [
            _make_event("100", event_type="PushEvent", repo_name="alice/proj", ref_type=None, ref="refs/heads/main"),
        ]
        conn, _client = self._connector(events)
        payloads = list(conn.poll(spec, since=None))
        # PushEvent not yielded by default (include_pushes=False)
        self.assertEqual(len(payloads), 0)

    def test_push_events_yielded_when_included(self):
        spec = GitHubEventsSourceSpec(kind="github_events", include_pushes=True)
        events = [
            _make_event("100", event_type="PushEvent", repo_name="alice/proj", ref_type=None, ref="refs/heads/main"),
        ]
        conn, _client = self._connector(events)
        payloads = list(conn.poll(spec, since=None))
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0].event_type, "push")

    def test_error_maps_to_connector_parse_error(self):
        spec = GitHubEventsSourceSpec(kind="github_events")
        client = mock.Mock()
        client.poll_events.side_effect = httpx.HTTPStatusError(
            "429 Too Many Requests",
            request=mock.Mock(),
            response=httpx.Response(429, headers={"X-RateLimit-Reset": "1700000000"}),
        )
        conn = GitHubEventsConnector()
        conn._client = client
        with self.assertRaises(ConnectorParseError) as ctx:
            list(conn.poll(spec, since=None))
        self.assertIn("429", str(ctx.exception))

    def test_skips_deleted_repo(self):
        spec = GitHubEventsSourceSpec(kind="github_events")
        events = [_make_event("100", repo_name="ghost/deleted")]
        client = mock.Mock()
        client.poll_events.return_value = _FakeResponse(200, json_data=events)
        client.fetch_repo_meta.return_value = None  # 404 = deleted
        conn = GitHubEventsConnector()
        conn._client = client
        payloads = list(conn.poll(spec, since=None))
        self.assertEqual(len(payloads), 0)

    def test_skips_create_event_with_non_repository_ref_type(self):
        spec = GitHubEventsSourceSpec(kind="github_events")
        # ref_type "branch" is not a new repo
        events = [_make_event("100", ref_type="branch")]
        conn, _client = self._connector(events)
        payloads = list(conn.poll(spec, since=None))
        self.assertEqual(len(payloads), 0)


class NewRepoEventPayloadTests(SimpleTestCase):
    def test_from_create_event(self):
        meta = _make_repo_meta(full_name="alice/proj", stars=100)
        p = NewRepoEventPayload.from_create_event(
            repo=meta, occurred_at=datetime(2026, 6, 19, 12, 0, tzinfo=UTC),
            event_type="create", readme="# Hello\nContent.", event_id="100",
        )
        self.assertEqual(p.external_id, "alice/proj")
        self.assertEqual(p.stars, 100)
        self.assertEqual(p.event_type, "create")
        self.assertEqual(p.source, "github_events")
        self.assertIn("Hello", p.content)

    def test_from_create_event_missing_readme(self):
        meta = _make_repo_meta(full_name="bob/repo")
        p = NewRepoEventPayload.from_create_event(
            repo=meta, occurred_at=datetime(2026, 6, 19, 12, 0, tzinfo=UTC),
            event_type="create", readme=None,
        )
        self.assertEqual(p.external_id, "bob/repo")
        self.assertIn("A test project", p.content)

    def test_sample_distinct(self):
        a = NewRepoEventPayload.sample(0)
        b = NewRepoEventPayload.sample(1)
        self.assertNotEqual(a.external_id, b.external_id)
        self.assertEqual(a.PAYLOAD_KIND, "new_repo_event")

    def test_source_slug(self):
        meta = _make_repo_meta(full_name="alice/proj")
        p = NewRepoEventPayload.from_create_event(
            repo=meta, occurred_at=datetime(2026, 6, 19, 12, 0, tzinfo=UTC),
            event_type="create",
        )
        self.assertEqual(p.source_slug(), "alice/proj")
