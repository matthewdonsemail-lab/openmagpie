"""GitHub payloads: a repository observed via the GitHub Search API.

Maps a GitHub repository search result to the openmagpie SourcePayload
contract: the engine judges `title` + `content`, so the repo's name goes
to `title` and its description + primary language + topics become the
judgeable body in `content`. The owner's login becomes the within-kind
`source_slug`. Metrics (stars, forks, open issues) / owner / topics /
language / dates stay on the payload as source-specific fields (available
to actions that read them, omitted from the engine prompt unless included).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from openmagpie_schema.configs import GitHubSearchSourceSpec
from sources.payloads import SourcePayload

# GitHub repository URL base.
GITHUB_REPO_URL = "https://github.com"


class NewRepoPayload(SourcePayload):
    """A single GitHub repository observed by a watched search stream.

    `title` is the repo name (e.g. `langfuse/langfuse`); `content` is the
    description augmented with language + topics (the engine's judgeable
    body). `full_name` is the canonical `owner/name` and the within-kind
    source slug (grouping items by producing repository). The rest is
    source-specific: `stars`, `forks`, `open_issues`, `language`,
    `topics`, `owner`, `homepage`, `license`, `pushed_at`, `created_at`,
    `updated_at`.
    """

    PAYLOAD_KIND: ClassVar[str] = "new_repo"

    full_name: str = ""
    owner: str = ""
    stars: int = 0
    forks: int = 0
    open_issues: int = 0
    language: str = ""
    topics: list[str] = []
    homepage: str = ""
    license_name: str = ""
    pushed_at: str = ""
    created_at: str = ""
    updated_at: str = ""

    model_config = {"frozen": True, "extra": "ignore"}

    def source_slug(self) -> str | None:
        return self.full_name or None

    @classmethod
    def sample(cls, variant: int = 0) -> NewRepoPayload:
        n = variant + 1
        full_name = f"example-org/example-project-{n}"
        return cls(
            external_id=full_name,
            kind=cls.PAYLOAD_KIND,
            occurred_at=datetime(2026, 5, 27, 12, 0, tzinfo=UTC),
            source=GitHubSearchSourceSpec.SOURCE_KIND,
            title=f"Example Project {n}",
            content=f"Example GitHub repository {n}: a description that matched this watch, written in Python, about LLM tooling.",
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
            pushed_at="2026-05-27T12:00:00Z",
            created_at="2025-01-01T12:00:00Z",
            updated_at="2026-05-27T12:00:00Z",
        )

    @classmethod
    def from_repo(cls, repo: dict[str, Any]) -> NewRepoPayload:
        """Map a GitHub API repository-search result dict to a payload.

        GitHub's search endpoint returns plain dicts; all attributes are
        accessed via dict get() with defaults, so the connector's unit
        tests can hand in lightweight fakes (a real `items` entry from the
        API works too).
        """
        full_name = str(repo.get("full_name") or "")
        owner_obj = repo.get("owner") or {}
        owner = str(owner_obj.get("login") or "") if isinstance(owner_obj, dict) else ""
        language = str(repo.get("language") or "")
        topics = [str(t) for t in (repo.get("topics") or []) if t]
        license_obj = repo.get("license")
        license_name = str(license_obj.get("spdx_id") or "") if isinstance(license_obj, dict) else ""

        # Timestamp parsing: GitHub returns ISO-ish strings with a trailing Z
        # and sometimes fractional seconds. Keep them as strings on the
        # payload; `occurred_at` drives the watermark (pushed_at is the
        # signal of a live repo, updated_at is the search-index version).
        occurred_at = datetime.now(UTC)
        pushed_at = str(repo.get("pushed_at") or "")
        if pushed_at:
            try:
                occurred_at = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
            except ValueError:
                pass

        # Judgeable body: description + language + topics are what the
        # semantic filter needs to score against an instruction like
        # "repo uses @ai-sdk/openai-compatible".
        description = str(repo.get("description") or "")
        parts = [p for p in (description, language, ", ".join(topics)) if p]
        content = ". ".join(parts) if parts else ""

        return cls(
            external_id=full_name,
            kind=cls.PAYLOAD_KIND,
            occurred_at=occurred_at,
            source=GitHubSearchSourceSpec.SOURCE_KIND,
            title=f"{full_name}",
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
            pushed_at=pushed_at,
            created_at=str(repo.get("created_at") or ""),
            updated_at=str(repo.get("updated_at") or ""),
        )
