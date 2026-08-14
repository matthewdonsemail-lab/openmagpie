"""GitHub connector: repository search via the public Search API.

One file per concern:
  - `connector.py` ; the `GitHubSearchConnector` impl (poll loop)
  - `client.py` ; `GitHubClient` wrapper (httpx, optional token)
  - `payloads.py` ; `NewRepoPayload` (GitHub API repo dict -> SourcePayload)
  - `errors.py` ; httpx / GitHub API error taxonomy -> canonical GitHubError

Future variants (user repos, issue search, code search) reuse
`GitHubClient` with their own spec + payload.
"""

from .connector import GitHubSearchConnector
from .payloads import NewRepoPayload

__all__ = ["GitHubSearchConnector", "NewRepoPayload"]
