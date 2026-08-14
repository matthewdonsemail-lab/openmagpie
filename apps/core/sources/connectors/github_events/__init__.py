"""GitHub Events connector: poll the public /events firehose for new repos.

One file per concern:
  - `connector.py` ; the `GitHubEventsConnector` impl (poll loop)
  - `client.py` ; `GitHubEventsClient` wrapper (httpx, optional token, enrichment)
  - `payloads.py` ; `NewRepoEventPayload` (GitHub API repo dict -> SourcePayload)
  - `errors.py` ; httpx / GitHub API error taxonomy -> canonical GitHubError
"""

from .connector import GitHubEventsConnector
from .payloads import NewRepoEventPayload

__all__ = ["GitHubEventsConnector", "NewRepoEventPayload"]
