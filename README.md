# Hermes Reach

Hermes Reach is a standalone Hermes Agent plugin for reliable, normalized,
read-only access to platform-specific internet sources.

The current Alpha-1 release provides the stable five-tool contract and a
15-source capability catalog. It executes credential-free Web reads, one-shot
RSS/Atom reads, and fixed-route V2EX public reads through a bounded,
DNS-pinned public HTTP transport. It does not access accounts, start a
Connector, read credentials, persist content, or provide browser automation.

Exa search is represented by an exact injectable client boundary only. Until a
separately audited client is supplied through operator setup, its operations
remain `setup_required`; Reach does not accept API keys from tool input or make
direct Exa requests.

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

Operator commands live under `hermes reach`. Status and discovery commands are
local-only; they report operation availability without DNS, credential, or
source health probes.
