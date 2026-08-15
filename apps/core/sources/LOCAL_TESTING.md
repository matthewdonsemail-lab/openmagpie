---
title: "Local Testing — GitHub Connectors"
tags: [testing, github-connectors, development]
status: active
created: 2026-08-14
---

# Local Testing — GitHub Connectors

## Prerequisites

- Docker Desktop with WSL2 backend (Docker Compose v2)
- `uv` installed
- Clone of openmagpie at `REPOS/openmagpie`
- Apps/core `.env` with `DJANGO_ENV=local` and valid Postgres credentials

## Quick start

```bash
# 1. Start Postgres container (standalone, no full stack needed)
cd /mnt/c/Users/0/.buzz/REPOS/openmagpie
wsl.exe -d Ubuntu-22.04 -- docker compose up -d db

# 2. Run the GitHub connector test suite
cd apps/core
uv run --package openmagpie-core --project ../../pyproject.toml bash -c \
  'export $(grep -v "^#" ../../apps/core/.env | xargs) && \
   python -m django test sources.tests_github_events \
     --settings=conf.settings.local -v 2'
```

## Test suites

| Test file | What it covers |
|-----------|----------------|
| `sources/tests_github_events.py` | 16 tests: spec validation, watermark filter, event type filtering, push event support, error translation, payload mapping, 304 handling, deleted repo handling |
| `sources/tests.py::FeedItemPayloadParityTests` | Schema parity guard: every connector PAYLOAD_KIND must have a matching FeedItemData variant with matching field names |

## All tests

```bash
# Full source test suite (20 tests)
uv run --package openmagpie-core --project ../../pyproject.toml bash -c \
  'export $(grep -v "^#" ../../apps/core/.env | xargs) && \
   python -m django test sources.tests sources.tests_github_events \
     --settings=conf.settings.local -v 2'
```

## Known issues

- **ENGINE_MODEL not set** — warning only; the semantic-filter tests skip when no engine is configured
- **Django 3.14 Windows path** — `uv run --project` and `--package` are needed because the venv and the repo root are different directories
- **Postgres is required** — the app does not support SQLite even in dev (multi-writer pipeline)

## When you add a new payload kind

1. Add the `PAYLOAD_KIND` class var to the server-side `SourcePayload` subclass
2. Add a mirror class in `packages/openmagpie-schema/src/openmagpie_schema/feed_payloads.py` with `kind: Literal["your_kind"]`
3. Add the new class to the `FeedItemData` union in the same file
4. Run the parity test to confirm it's wired correctly
