"""GitHub Events payloads: a new repo observed via the public /events firehose.

Maps a `CreateEvent` with `ref_type: "repository"` (the new-repo signal)
onto the openmagpie `SourcePayload` contract: the engine judges `title` +
`content`, so the repo's `full_name` goes to `title` and its description +
primary language + topics (+ README excerpt when one was fetched) become
the judgeable body in `content`. The repo's `full_name` is the within-kind
`source_slug` AND the `external_id` — the same dedup key the GitHub Search
connector uses, so a repo discovered first via radar and later via search
collapses to one FeedItem at the DB seam (dedup keys on
(feed_id, source_kind, external_id)).

Note that this payload's source_slug is the *repo*, not the actor: the
listener's grouping is "which repository was created", and multiple events
can reference the same repo.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from openmagpie_schema.configs import GitHubEventsSourceSpec
from sources.payloads import SourcePayload

# GitHub repository URL base.
GITHUB_REPO_URL = "https://github.com"

# Truncate the README excerpt added to `content` (the full README text stays
# on the `readme` field for actions that read it). The engine scores
# title + content; a 500-char excerpt is plenty of signal and keeps payloads
# small for items that only carry the excerpt.
_README_EXCERPT_CHARS = 500


class NewRepoEventPayload(SourcePayload):
    """A newly created GitHub repository observed via the public /events firehose.

    `title` is the repo name (e.g. `langfuse/langfuse`); `content` is the
    description augmented with language + topics + README excerpt (the
    engine's judgeable body). `full_name` is the canonical `owner/name` and
    the within-kind source slug. The rest is source-specific: `stars`,
    `forks`, `open_issues`, `language`, `topics`, `owner`, `homepage`,
    `license`, `readme` (full text, when fetched), `event_type`
    ("create" | "push"), `pushed_at`, `created_at`, `updated_at`.
    `actor_login`, `actor_id`, `actor_avatar_url` capture the person who
    triggered the event (the GitHub user who created the repo or pushed).
    """

    PAYLOAD_KIND: ClassVar[str] = "new_repo_event"

    full_name: str = ""
    owner: str = ""
    stars: int = 0
    forks: int = 0
    open_issues: int = 0
    language: str = ""
    topics: list[str] = []
    homepage: str = ""
    license_name: str = ""
    readme: str = ""
    event_type: str = "create"  # "create" (new repo) or "push" (fresh code)
    pushed_at: str = ""
    created_at: str = ""
    updated_at: str = ""
    # Actor (the person who triggered the event)
    actor_login: str = ""
    actor_id: int = 0
    actor_avatar_url: str = ""

    model_config = {"frozen": True, "extra": "ignore"}

    def source_slug(self) -> str | None:
        return self.full_name or None

    @classmethod
    def sample(cls, variant: int = 0) -> NewRepoEventPayload:
        n = variant + 1
        full_name = f"example-org/example-project-{n}"
        return cls(
            external_id=full_name,
            kind=cls.PAYLOAD_KIND,
            occurred_at=datetime(2026, 5, 27, 12, 0, tzinfo=UTC),
            source=GitHubEventsSourceSpec.SOURCE_KIND,
            title=f"Example Project {n}",
            content="Example GitHub repository detected via /events radar: a newly pushed Python project that matches this watch.",
            url=f"{GITHUB_REPO_URL}/{full_name}",
            full_name=full_name,
            owner=f"example-org-{n}",
            stars=1000 + n,
            forks=100 + n,
            open_issues=10 + n,
            language="Python",
            topics=["llm", "openai", "tooling"],
            homepage="",
            license_name="MIT",
            readme="# Example\nA newly created repo.",
            event_type="new",
            pushed_at="2026-05-27T12:00:00Z",
            created_at="2026-05-27T12:00:00Z",
            updated_at="2026-05-27T12:00:00Z",
            actor_login=f"example-user-{n}",
            actor_id=1000 + n,
            actor_avatar_url=f"https://avatars.githubusercontent.com/u/1000{n}",
        )

    @classmethod
    def from_create_event(
        cls,
        repo: dict[str, Any],
        *,
        occurred_at: datetime,
        event_type: str = "create",
        readme: str | None = None,
        event_id: str | None = None,
        actor: dict[str, Any] | None = None,
    ) -> NewRepoEventPayload:
        """Map a GitHub API repo dict to a payload.

        `repo` is the enrichment result of `GET /repos/{owner}/{repo}`.
        `event_type` is "create" (new repo) or "push" (fresh code pushed to
        an existing repo). `readme` is the raw README text when fetched.
        `event_id` is the /events event ID, recorded in the source-meta (not
        the external_id — that stays `full_name` for cross-connector dedup).
        `actor` is the raw `/events` actor dict ({login, id, avatar_url,
        url}); its fields are mapped to `actor_login` / `actor_id` /
        `actor_avatar_url` so the *person* behind the event is visible.
        """
        full_name = str(repo.get("full_name") or "")
        owner_obj = repo.get("owner") or {}
        owner = str(owner_obj.get("login") or "") if isinstance(owner_obj, dict) else ""
        language = str(repo.get("language") or "")
        topics = [str(t) for t in (repo.get("topics") or []) if t]
        license_obj = repo.get("license")
        license_name = str(license_obj.get("spdx_id") or "") if isinstance(license_obj, dict) else ""
        description = str(repo.get("description") or "")
        readme_text = str(readme or "")

        # Actor (the person who triggered the event). Raw shape from /events:
        # {"login": "octocat", "id": 1, "node_id": "...", "avatar_url": "...",
        #  "url": "https://api.github.com/users/octocat", ...}
        actor_obj = actor if isinstance(actor, dict) else {}
        actor_login = str(actor_obj.get("login") or "")
        actor_id = int(actor_obj.get("id") or 0)
        actor_avatar_url = str(actor_obj.get("avatar_url") or "")

        # Judgeable body: description + language + topics, then a README
        # excerpt when one exists (the engine scores title + content).
        parts = [p for p in (description, language, ", ".join(topics)) if p]
        content = ". ".join(parts) if parts else ""
        if readme_text:
            excerpt = readme_text.replace("\r", "").replace("\n", " ").strip()[:_README_EXCERPT_CHARS]
            excerpt = excerpt.strip()
            if excerpt:
                content = f"{content}\n\nREADME: {excerpt}" if content else f"README: {excerpt}"

        return cls(
            external_id=full_name,
            kind=cls.PAYLOAD_KIND,
            occurred_at=occurred_at,
            source=GitHubEventsSourceSpec.SOURCE_KIND,
            title=full_name or f"github:{event_id or ''}",
            content=content,
            url=f"{GITHUB_REPO_URL}/{full_name}" if full_name else "",
            full_name=full_name,
            owner=owner,
            stars=int(repo.get("stargazers_count") or 0),
            forks=int(repo.get("forks_count") or 0),
            open_issues=int(repo.get("open_issues_count") or 0),
            language=language,
            topics=topics,
            homepage=str(repo.get("homepage") or ""),
            license_name=license_name,
            readme=readme_text,
            event_type=event_type,
            pushed_at=str(repo.get("pushed_at") or ""),
            created_at=str(repo.get("created_at") or ""),
            updated_at=str(repo.get("updated_at") or ""),
            actor_login=actor_login,
            actor_id=actor_id,
            actor_avatar_url=actor_avatar_url,
        )
