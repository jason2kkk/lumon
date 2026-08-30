# Lumon Project Structure

Lumon is a local self-hosted application with a FastAPI backend and a React
frontend. Runtime data, credentials, logs, dependencies, and build output are
not source files and are excluded from Git.

## Repository layout

```text
Lumon/
|-- server.py                  # FastAPI entry point and local access guard
|-- backend/                   # API, data-source, model, and report services
|-- prompts/                   # LLM prompt templates
|-- frontend/                  # React, TypeScript, Vite, and static assets
|-- data/demo/                 # Deterministic synthetic demo data
|-- docs/                      # Design and implementation documentation
|-- scripts/                   # Local development and demo-data tools
|-- tests/                     # Local self-hosting regression tests
|-- Dockerfile
|-- docker-compose.yml
|-- requirements.txt
|-- requirements.lock
|-- LICENSE
|-- THIRD_PARTY_NOTICES.md
`-- README.md
```

## Backend

```text
backend/
|-- api_routes.py              # REST and SSE endpoints
|-- debate.py                  # Multi-role discussion workflow
|-- feishu_client.py           # Optional Feishu document export
|-- llm_client.py              # OpenAI-compatible model clients
|-- opportunity_scoring.py     # Opportunity and evidence scoring
|-- quote_extractor.py         # Quote extraction and FEMWC scoring
|-- rdt_client.py              # Local rdt-cli integration
|-- scrapers.py                # Reddit and Hacker News collection
|-- session_context.py         # Per-session state and credentials
|-- st_client.py               # Optional local st-cli integration
`-- web_search.py              # Tavily and model web-search integrations
```

`server.py` mounts the API router and serves `frontend/dist` when a production
frontend build exists. By default, the API accepts only loopback requests.

## Frontend

```text
frontend/
|-- public/                    # Existing icons, logos, fonts, and images
|-- src/api/                   # REST and SSE client
|-- src/components/            # Product views and interaction components
|-- src/stores/                # Zustand state stores
|-- src/App.tsx
|-- src/i18n.tsx
|-- src/index.css
`-- vite.config.ts
```

The development server listens on `127.0.0.1:5173` and proxies `/api` to the
local backend on `127.0.0.1:8001`.

## Runtime data

Only `data/demo/` is committed. Lumon creates other directories locally as
needed:

```text
data/
|-- sessions/                  # Per-session config, state, and reports
|-- cache/                     # Local caches
|-- reports/                   # Generated reports
|-- poc_evaluations/           # POC validation output
|-- analytics/                 # Optional local usage counters
`-- demo/                      # Synthetic demo content
```

Runtime directories and `.env` files are ignored by Git and excluded from the
Docker build context.

## Local commands

```bash
# Backend: http://127.0.0.1:8001
./scripts/start-local-dev.sh

# Frontend: http://127.0.0.1:5173
cd frontend
npm run dev

# Regression checks
.venv/bin/python -m unittest discover -s tests -q
cd frontend && npm run lint && npx tsc -b --noEmit
```

See `README.md` for installation, user-owned API keys, optional CLI setup, and
remote-access security requirements.
