# Hermes Reach

Hermes Reach is a standalone Hermes Agent plugin for reliable, normalized,
read-only access to platform-specific internet sources.

The current foundation release provides the stable five-tool contract and a
15-source capability catalog. It does not yet execute source adapters, access
accounts, start a Connector, read credentials, or make network requests.

## Development

```bash
uv sync --all-groups
uv run ruff check .
uv run mypy src
uv run pytest
uv build
```

The package exposes the documented `hermes_agent.plugins` entry point. Once
installed alongside a supported Hermes version, it registers:

- `reach_search`
- `reach_read`
- `reach_browse`
- `reach_transcribe`
- `reach_status`

Operator commands live under `hermes reach`. Foundation discovery commands are
local-only and report planned capabilities as unavailable until their adapter
tasks are implemented.
