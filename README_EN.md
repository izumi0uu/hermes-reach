# Hermes Reach

[中文](README.md) | English

Hermes Reach is a plugin that adds five read-only retrieval tools to
[Hermes Agent](https://github.com/NousResearch/hermes-agent). It uses a pinned
commit of the [Agent-Reach fork](https://github.com/izumi0uu/Agent-Reach) to
choose each backend, call the platform, and project the platform response.

This source tree is version `0.1.0a2` and is still Pre-Alpha. Test it locally
before deploying it.

## Quick start

Requirements: Python 3.11 through 3.13, `uv`, Git, and Hermes Agent 0.19.x.

```bash
git clone https://github.com/izumi0uu/hermes-reach.git
cd hermes-reach
uv sync --locked --all-groups

uv run hermes plugins enable reach --no-allow-tool-override
```

The setting takes effect when Hermes next starts. In the new process, check
which capabilities are available:

```bash
uv run hermes reach status --json
uv run hermes reach sources --json
uv run hermes reach doctor --json
```

Start Hermes with only the Reach toolset and routing skill:

```bash
uv run hermes \
  --skills reach:agent-reach \
  --toolsets reach
```

Ask for the data you need:

```text
Find the five most relevant Bilibili videos about Rust async runtimes.
```

```text
Read the Chinese subtitles from this YouTube video:
https://www.youtube.com/watch?v=VIDEO_ID
```

By default, `doctor` checks local state only. Run
`uv run hermes reach doctor --upstream` to add the restricted Agent-Reach
checks with redacted output.

## Available capabilities

The catalog lists 63 read-only operations. Thirty-four are implemented. The
Agent-Reach fork supplies 33 executors. YouTube comments have a request
contract but no backend.

| Enablement | Source | Operations |
| --- | --- | --- |
| Available locally | RSS/Atom | `read.feed`, `browse.entries` |
| Available locally | Bilibili | `search.videos`, `read.video`, `browse.hot`, `browse.rank` |
| Available locally | YouTube | `search.videos`, `read.video`, `read.subtitles` |
| Available locally | V2EX | `browse.hot`, `browse.node_topics`, `read.topic`, `read.user` |
| All required Exa paths and hashes set | Exa | `search.web`, `search.code` |
| Paired and authorized Connector | Reddit | Seven search, read, and browse operations |
| Paired and authorized Connector | Facebook | Four search, read, and browse operations |
| Paired and authorized Connector | Instagram | Four search, read, and browse operations |
| Paired and authorized Connector | Twitter/X | `search.posts` |
| Paired and authorized Connector | Xiaohongshu | `search.notes` |
| Paired and authorized Connector | Xueqiu | `search.stocks` |
| Currently unavailable (unbound) | YouTube | `read.comments` |
| Currently unavailable | Web, GitHub, LinkedIn, Xiaoyuzhou, and remaining catalog operations | No reviewed executor |

Use `reach status` to check an installation. A command, Cookie, or API key on
its own does not make a source `available`.

## Tools

| Tool | What it does |
| --- | --- |
| `reach_status` | Inspect sources, operations, and runtime availability |
| `reach_search` | Search one to five explicit sources |
| `reach_read` | Read one explicit target |
| `reach_browse` | Browse a source-native collection |
| `reach_transcribe` | Transcribe supported media; no transcription operation is available by default |

All catalog operations are read-only. There are no operations for publishing,
commenting, liking, following, or changing an account.

## Install the published pre-release

[GitHub Releases](https://github.com/izumi0uu/hermes-reach/releases) is the
public download channel. `v0.1.0a1` does not include the 33-operation
integration. If `v0.1.0a2` is not on the Releases page, use the source checkout
from Quick start.

Each release includes a wheel, an sdist, and `SHA256SUMS`. Do not use GitHub's
generated `Source code (zip)` or `Source code (tar.gz)` archives as release
artifacts.

```bash
RELEASE_TAG="v0.1.0a2"
RELEASE_VERSION="${RELEASE_TAG#v}"
RELEASE_DIR="hermes-reach-${RELEASE_TAG}"

test "$(gh release view "$RELEASE_TAG" \
  --repo izumi0uu/hermes-reach \
  --json tagName \
  --jq '.tagName')" = "$RELEASE_TAG"

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

On GNU/Linux, use `sha256sum --check SHA256SUMS`. `SHA256SUMS` itself is not an
attestation subject; it only detects changes to the downloaded files.

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

Exa does not need an API key. Hermes does not load Node or mcporter from PATH,
npm, or editor configuration. Set every value below in the environment that
starts Hermes:

```bash
export HERMES_REACH_EXA_NODE_EXECUTABLE=/absolute/path/to/node
export HERMES_REACH_EXA_NODE_SHA256="<64-lowercase-hex>"
export HERMES_REACH_EXA_MCPORTER_ROOT=/absolute/path/to/mcporter
export HERMES_REACH_EXA_MCPORTER_CLI=/absolute/path/to/mcporter/dist/cli.js
export HERMES_REACH_EXA_MCPORTER_TREE_SHA256="<64-lowercase-hex>"
export HERMES_REACH_EXA_CONFIG_PATH=/absolute/path/to/sterile-config.json
export HERMES_REACH_EXA_CONFIG_SHA256="<64-lowercase-hex>"
```

If any value is missing, incomplete, or does not match, both Exa operations
remain `setup_required`. Web and Code use separate fixed methods; neither can
substitute for the other. Read the
[Exa backend decision](docs/agent-reach-decisions/exa-mcporter-1.5.0.md).

## Run the Connector on a trusted device

By default, Reddit, Facebook, Instagram, Twitter, Xiaohongshu, and Xueqiu run
through the Connector instead of on the VPS. Browser sessions, Cookies, and
Bitwarden access stay on a computer or another trusted device. The VPS does
not store platform credentials. It does store its device key, pairing record,
capability snapshots, grants, and receipt ledger.

Install and enable Reach on both the trusted device and the VPS as shown in
Quick start. On both machines, set `HERMES_REACH_CONNECTOR_HOST` to the trusted
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

Pass all four `--opencli-social-*` arguments together, or omit all four. To run
only Xueqiu, use:

```bash
uv run hermes reach connector serve \
  --state-directory /absolute/connector-state \
  --bind "$HERMES_REACH_CONNECTOR_HOST" \
  --port 8765 \
  --xueqiu-binding-manifest /absolute/owner-only-xueqiu-binding.json
```

To run both groups, add `--xueqiu-binding-manifest` to the first `serve`
command. The manifest contains locators. It must not contain a Cookie, BWS
token, or secret value. The terminal that started the Connector displays the
exact scopes and asks for `enable`. When the `Connector>` prompt appears, enter
`unlock`.

Initialize the VPS and request only the scopes it needs:

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

On the trusted device, run `pending` and compare the details shown on both
machines. If they match, run `approve <pairing-id>`. Start Hermes on the VPS
with the paired state:

```bash
HERMES_REACH_VPS_STATE_DIRECTORY=/absolute/vps-state uv run hermes \
  --skills reach:agent-reach \
  --toolsets reach
```

The [Connector security and operations](docs/connector-security.md) guide
covers network setup, grants, revocation, Bitwarden, audit, and recovery.

## Security boundary

- Every operation is read-only and identifies its source and operation.
- Each request has time, item, byte, and pagination limits.
- Reach fails closed for unreviewed backends. It does not try a less
  restrictive backend.
- Platform account sessions and credentials stay on the trusted device. The VPS
  still sees the queries it sends and the results it receives.
- If the VPS is compromised, its device identity and local Connector state are
  exposed. An attacker can also use any remaining requests on an active grant.
  Revocation blocks later requests.

Web `read.url`, GitHub, and LinkedIn remain frozen until they meet the evidence
and review conditions in the
[Agent-Reach reuse boundary](docs/agent-reach-reuse-boundary.md) and
[backend decision records](docs/agent-reach-decisions/).

## Agent-Reach boundary

Hermes Reach does not copy platform execution code. All 33 executors come from
the pinned Agent-Reach fork. Hermes Reach validates and authorizes each request.
It runs the executors in isolation, limits results, redacts sensitive data, and
records receipts and audit events.

The [plugin boundary](docs/agent-reach-plugin-boundary.md) defines which
project owns each part. The [release guide](docs/releasing.md) describes
artifact acceptance and rollback.

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

Disable Reach and start a new Hermes process without it before uninstalling the
wheel. Hermes `plugins remove`, `plugins rm`, and `plugins uninstall` apply to
directory plugins. Use the Python package manager to uninstall this package.

## Development

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Hermes Reach `0.1.0a2` is licensed under the [MIT License](LICENSE).
