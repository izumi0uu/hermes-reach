# Hermes Reach

[中文](README.md) | English

Hermes Reach is a read-only retrieval plugin for
[Hermes Agent](https://github.com/NousResearch/hermes-agent). The plugin
registers five read-only tools. An exact, pinned
[Agent-Reach fork](https://github.com/izumi0uu/Agent-Reach) owns platform
invocation, native result projection, and backend selection.

The source tree is version `0.1.0a2` and remains Pre-Alpha. Start with a local
evaluation.

## Quick start

Requirements: Python 3.11 through 3.13, `uv`, Git, and Hermes Agent 0.19.x.

```bash
git clone https://github.com/izumi0uu/hermes-reach.git
cd hermes-reach
uv sync --locked --all-groups

uv run hermes plugins enable reach --no-allow-tool-override
```

The enable setting takes effect in the next Hermes process. Start a new process
and inspect the actual capabilities:

```bash
uv run hermes reach status --json
uv run hermes reach sources --json
uv run hermes reach doctor --json
```

Start a session with only the Reach toolset and routing skill:

```bash
uv run hermes \
  --skills reach:agent-reach \
  --toolsets reach
```

Then describe the retrieval task:

```text
Find the five most relevant Bilibili videos about Rust async runtimes.
```

```text
Read the Chinese subtitles from this YouTube video:
https://www.youtube.com/watch?v=VIDEO_ID
```

The default `doctor` is local-only. `uv run hermes reach doctor --upstream`
also runs the restricted, redacted Agent-Reach checks.

## Available capabilities

The catalog contains 63 read-only operations. Thirty-four are implemented,
33 have an Agent-Reach fork executor, and YouTube comments have a request
contract but no backend.

| Enablement | Source | Operations |
| --- | --- | --- |
| Available locally | RSS/Atom | `read.feed`, `browse.entries` |
| Available locally | Bilibili | `search.videos`, `read.video`, `browse.hot`, `browse.rank` |
| Available locally | YouTube | `search.videos`, `read.video`, `read.subtitles` |
| Available locally | V2EX | `browse.hot`, `browse.node_topics`, `read.topic`, `read.user` |
| Complete Exa artifacts supplied | Exa | `search.web`, `search.code` |
| Paired and authorized Connector | Reddit | Seven search, read, and browse operations |
| Paired and authorized Connector | Facebook | Four search, read, and browse operations |
| Paired and authorized Connector | Instagram | Four search, read, and browse operations |
| Paired and authorized Connector | Twitter/X | `search.posts` |
| Paired and authorized Connector | Xiaohongshu | `search.notes` |
| Paired and authorized Connector | Xueqiu | `search.stocks` |
| Currently unavailable (unbound) | YouTube | `read.comments` |
| Currently unavailable | Web, GitHub, LinkedIn, Xiaoyuzhou, and remaining catalog operations | No reviewed executor |

`reach status` is authoritative for each installation. Installing a command,
Cookie, or API key does not make a source `available`.

## Tools

| Tool | Purpose |
| --- | --- |
| `reach_status` | Inspect sources, operations, and runtime availability |
| `reach_search` | Search one to five explicit sources |
| `reach_read` | Read one explicit target |
| `reach_browse` | Browse a source-native collection |
| `reach_transcribe` | Transcribe supported media; no transcription operation is available by default |

The catalog is read-only. It has no publish, comment, like, follow, or
account-mutation operation.

## Install the published pre-release

[GitHub Releases](https://github.com/izumi0uu/hermes-reach/releases) is the
current public channel. `v0.1.0a1` does not contain the final 33-operation
integration. Use the source checkout above if the Releases page does not yet
contain `v0.1.0a2`.

The workflow uploads a wheel, an sdist, and `SHA256SUMS`. GitHub-generated
`Source code (zip)` and `Source code (tar.gz)` links are not part of that
accepted artifact set.

```bash
RELEASE_TAG="$(gh release list \
  --repo izumi0uu/hermes-reach \
  --exclude-drafts \
  --limit 1 \
  --json tagName \
  --jq '.[0].tagName')"
test -n "$RELEASE_TAG"
RELEASE_VERSION="${RELEASE_TAG#v}"
RELEASE_DIR="hermes-reach-${RELEASE_TAG}"

gh release download "$RELEASE_TAG" \
  --repo izumi0uu/hermes-reach \
  --dir "$RELEASE_DIR"

gh attestation verify \
  "$RELEASE_DIR/hermes_reach-${RELEASE_VERSION}-py3-none-any.whl" \
  --repo izumi0uu/hermes-reach \
  --signer-workflow izumi0uu/hermes-reach/.github/workflows/release.yml

gh attestation verify \
  "$RELEASE_DIR/hermes_reach-${RELEASE_VERSION}.tar.gz" \
  --repo izumi0uu/hermes-reach \
  --signer-workflow izumi0uu/hermes-reach/.github/workflows/release.yml

cd "$RELEASE_DIR"
shasum -a 256 --check SHA256SUMS
cd ..
```

Use `sha256sum --check SHA256SUMS` on GNU/Linux. `SHA256SUMS` itself is not an
attestation subject; it only detects changed download bytes.

Install the wheel into the Python environment that runs Hermes:

```bash
HERMES_PYTHON=/absolute/path/to/hermes-environment/bin/python
HERMES_BIN=/absolute/path/to/hermes-environment/bin/hermes

uv pip install \
  --python "$HERMES_PYTHON" \
  "$RELEASE_DIR/hermes_reach-${RELEASE_VERSION}-py3-none-any.whl"
uv pip check --python "$HERMES_PYTHON"

"$HERMES_BIN" plugins enable reach --no-allow-tool-override
```

## Configure Exa

Exa uses no API key. Hermes does not discover Node or mcporter from PATH, npm,
or editor configuration. Declare the complete reviewed artifact set in the
environment that starts Hermes:

```bash
export HERMES_REACH_EXA_NODE_EXECUTABLE=/absolute/path/to/node
export HERMES_REACH_EXA_NODE_SHA256="<64-lowercase-hex>"
export HERMES_REACH_EXA_MCPORTER_ROOT=/absolute/path/to/mcporter
export HERMES_REACH_EXA_MCPORTER_CLI=/absolute/path/to/mcporter/dist/cli.js
export HERMES_REACH_EXA_MCPORTER_TREE_SHA256="<64-lowercase-hex>"
export HERMES_REACH_EXA_CONFIG_PATH=/absolute/path/to/sterile-config.json
export HERMES_REACH_EXA_CONFIG_SHA256="<64-lowercase-hex>"
```

Missing, partial, or mismatched declarations leave both Exa operations
`setup_required`. Web and Code use different fixed methods and cannot fall
back to each other. See the
[Exa backend decision](docs/agent-reach-decisions/exa-mcporter-1.5.0.md).

## Run the Connector on a trusted device

Reddit, Facebook, Instagram, Twitter, Xiaohongshu, and Xueqiu do not execute on
the VPS by default. The Connector keeps browser sessions, Cookies, and
Bitwarden access on a computer or another trusted device. The VPS does not
store platform credentials, but it does store its device key, pairing record,
capability snapshots, grants, and receipt ledger.

Complete the Quick start installation and enablement on both the trusted device
and the VPS. Set `HERMES_REACH_CONNECTOR_HOST` on both machines to the trusted
device's reachable private IP address.

Initialize the trusted device and start the foreground service:

```bash
export HERMES_REACH_CONNECTOR_HOST="<trusted-device-private-ip>"

uv run hermes reach connector init \
  --role connector \
  --state-directory /absolute/connector-state

uv run hermes reach connector serve \
  --state-directory /absolute/connector-state \
  --bind "$HERMES_REACH_CONNECTOR_HOST" \
  --port 8765 \
  --opencli-social-node /absolute/path/to/node \
  --opencli-social-root /absolute/opencli-production-prefix \
  --opencli-social-cli /absolute/opencli-production-prefix/node_modules/@jackwener/opencli/dist/src/main.js \
  --opencli-social-session-home /absolute/trusted-session-home
```

All four `--opencli-social-*` arguments must be supplied together or omitted
together. To enable only Xueqiu, run:

```bash
uv run hermes reach connector serve \
  --state-directory /absolute/connector-state \
  --bind "$HERMES_REACH_CONNECTOR_HOST" \
  --port 8765 \
  --xueqiu-binding-manifest /absolute/owner-only-xueqiu-binding.json
```

To enable both groups, add `--xueqiu-binding-manifest` to the first `serve`
command. The manifest contains locators, not a Cookie, BWS token, or secret
value. The original terminal displays the exact scopes and requires `enable`,
followed by `unlock` at the `Connector>` prompt.

Initialize the VPS and pair only the required scopes:

```bash
export HERMES_REACH_CONNECTOR_HOST="<trusted-device-private-ip>"

uv run hermes reach connector init \
  --role vps \
  --state-directory /absolute/vps-state

uv run hermes reach connector pair \
  --state-directory /absolute/vps-state \
  --connector "wss://${HERMES_REACH_CONNECTOR_HOST}:8765" \
  --device-label hermes-vps \
  --scope reddit:read.post:public \
  --scope instagram:browse.explore:account_visible
```

Run `pending` on the trusted device, compare both displays, then run
`approve <pairing-id>`. Point the VPS Hermes process at the paired state:

```bash
HERMES_REACH_VPS_STATE_DIRECTORY=/absolute/vps-state uv run hermes \
  --skills reach:agent-reach \
  --toolsets reach
```

See [Connector security and operations](docs/connector-security.md) for
network setup, grants, revocation, Bitwarden, audit, and recovery.

## Security boundary

- Every operation is read-only and names an explicit source and operation.
- Requests have time, item, byte, and pagination bounds.
- An unreviewed backend fails closed; Reach does not search for a looser fallback.
- Platform account sessions and credentials stay on the trusted device. The VPS
  still sees the queries it sends and the results it receives.
- A compromised VPS exposes its device identity and local Connector state. An
  attacker may also consume the remaining uses of a live grant. Revocation
  denies later requests.

Web `read.url`, GitHub, and LinkedIn are intentionally frozen. The evidence and
review conditions are in the
[Agent-Reach reuse boundary](docs/agent-reach-reuse-boundary.md) and
[backend decision records](docs/agent-reach-decisions/).

## Agent-Reach boundary

Hermes Reach does not copy platform runtimes. All 33 current executors come
from the exact Agent-Reach fork. Hermes Reach owns validation, authorization,
isolated invocation, result bounds, redaction, receipts, and audit.

See the [plugin boundary](docs/agent-reach-plugin-boundary.md) for ownership
details and the [release guide](docs/releasing.md) for artifact acceptance and
rollback.

## Disable and uninstall

Source environment:

```bash
uv run hermes plugins disable reach
uv pip uninstall --python .venv/bin/python hermes-reach
```

Wheel environment:

```bash
"$HERMES_BIN" plugins disable reach
uv pip uninstall --python "$HERMES_PYTHON" hermes-reach
uv pip check --python "$HERMES_PYTHON"
```

Disable Reach, start a fresh Hermes process without it, then uninstall the
wheel. Hermes `plugins remove`, `plugins rm`, and `plugins uninstall` manage
directory plugins and do not replace the Python package manager.

## Development

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Hermes Reach `0.1.0a2` is licensed under the [MIT License](LICENSE).
