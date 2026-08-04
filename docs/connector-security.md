# Connector security and operations

> [!IMPORTANT]
> Hermes Reach is pre-alpha. A Connector executor receives an authorized
> operation only through an explicitly registered exact binding.
> `ConnectorService` authorizes and delivers that fixed operation. It never
> selects a provider, credential, browser session, or local path. Connector
> execution composition is unbound by default. Local execution remains
> available for 15 owner-fork operations (RSS 2, Bilibili 4, YouTube 3, V2EX 4,
> and Exa Web/Code 2). The first 13 are composed and available without artifact
> setup. Exa requires a complete operator-supplied Node/mcporter/config
> attestation. The only Connector production path requires explicit activation
> on both sides for exactly 18 fork-owned operations: 17 OpenCLI social and one
> Xueqiu. The controls below authorize only this pre-alpha batch, not arbitrary
> commands or production credentials.

## Supported topology

The trusted-device Connector supports macOS and Linux. The remote virtual
private server (VPS) supports Linux. Windows is unsupported. On Windows, the
implementation must fail closed rather than weaken file permissions, terminal
unlock, process locking, or certificate checks.

Start the Connector only on a trusted device. Bind it to one explicit numeric
loopback or private-network address. The listener accepts only WebSocket Secure
(WSS) over Transport Layer Security (TLS). It rejects wildcard, public,
multicast, ambiguous link-local, and plaintext WebSocket binds. Never expose it
through `0.0.0.0`, `::`, or a public address. Private reachability, including
Tailscale or Headscale membership, does not grant authority by itself.

The Connector installs no launchd or systemd unit. It runs only as a foreground
process attached to its original controlling terminal.

## Prepare an isolated secrets profile

Use a dedicated Hermes profile for the Connector. Keep the normal Hermes
SecretSource registry disabled in that profile so startup cannot apply secrets
to the process-wide environment.

Create a dedicated Bitwarden Secrets Manager machine account and project. Put
only the keys needed by approved Reach backends in that project. The Connector
helper loads the project into isolated memory before selecting one binding, so
the project contents are a security boundary. Do not reuse a broad personal or
application project.

Provide the Bitwarden Secrets Manager (BWS) bootstrap token through the
operator-controlled profile environment. Do not put it in Reach configuration
or copy it to the VPS. Hermes Reach does not install or update `bws`, use stale
values, or use plaintext or encrypted Bitwarden caches.

## OpenCLI social executor boundary

Fixed source executors exist for seven Reddit, four Facebook, four Instagram,
one Twitter, and one Xiaohongshu operation. All 17 are disabled by default. The
trusted device must enable the complete exact composition, and Hermes on the
VPS must load matching paired state with a grant for each operation it may use.

The VPS request may contain only a catalog source-operation and its closed
query, identifier, URL, or limit fields. It cannot contain command, argv, backend,
executable, install root, browser profile, session home, credential, local
path, process environment, scope, or fallback fields. The Agent-Reach fork
owns every OpenCLI command, strict YAML/error parser, platform row validator,
bound, correlation rule, and source-native projection. Hermes contains no
OpenCLI platform command table or social result parser.

OpenCLI uses the trusted device's existing local Chrome/OpenCLI session. The
Connector neither exports that session nor copies cookies, passwords, browser
storage, or account material to the VPS or Bitwarden. The execution path has no
Bitwarden `SecretExecutionPlan` and rejects any non-empty secret environment.
The only local capability fields are the exact absolute Node executable and
digest, dedicated OpenCLI production-prefix root and CLI path plus tree digest,
and absolute trusted session home.

Before every backend call, the Agent-Reach runtime revalidates Node, the
complete OpenCLI dependency tree, and its packaged no-lifecycle-mutation guard,
then creates a private snapshot. It invokes only the copied bytes. The guard
allows an already-ready compatible daemon but blocks OpenCLI from starting,
restarting, stopping, signaling, or replacing a daemon. A stopped, stale,
logged-out, challenged, rate-limited, or incompatible session fails closed
without changing trusted-device browser state.

Package import, `reach_status`, doctor, and default Hermes plugin startup do not
find OpenCLI, inspect a browser, start a daemon, or register these executors.
Removing the explicit bindings is the rollback: it restores signed
`backend_unbound` behavior without changing the browser session, Connector
authority, or Bitwarden state.

Local execution has 15 owner-fork operations regardless of Connector state:
two RSS bindings, four Bilibili/bili-cli bindings, three YouTube bindings, four
V2EX bindings, and separate Exa Web and Exa Code contracts over the same
attested artifacts. The first 13 are composed without artifact setup. Exa Web
and Code are composed only when their complete seven-field artifact attestation
is present; otherwise they report `setup_required`. `youtube:read.comments` is
implemented but unbound and also reports `setup_required`. After both explicit
activation gates pass, the Connector adds 18 direct owner-fork executors. It
does not replace or proxy the 15 local operations.

The RSS fork runs locally without credentials. Hermes gives it only an
already-fetched bounded document through `fetched_document.v1`; it receives no
Bitwarden secret plan, Connector identity, grant, paired-device state, browser
session, or Connector execution authority.

The Bilibili fork also runs locally without credentials. Hermes gives it only
a fieldless `network_access.v1` marker inside a private worker. The marker
contains no endpoint, proxy, Cookie, credential, path, command, backend
selector, or Connector authority. It approves four fixed registered operations
on the host. It does not grant generic network access and is not an OS sandbox.

The three executable YouTube operations use the same fieldless marker only
after the fixed YouTube worker validates the exact fork runtime. Subtitle
operations also receive a fieldless private-workspace marker for the worker's
per-attempt current directory. The fork owns the pinned yt-dlp calls,
subtitle-file safety checks, and native projection.

V2EX uses four fixed fork descriptors and the fork's bounded public API
transport. Exa Web and Code use separate fixed fork descriptors and a closed
mcporter artifact capability. That capability contains only operator-declared
absolute paths and digests. Neither receives Connector identity, grant,
Bitwarden secret, browser session, or remote execution authority. Exa receives
each query directly and may retain it; Hermes does not persist the query in
receipts or audit.

## Connector activation and Xueqiu secret boundaries

LinkedIn people/jobs search is not part of Connector activation. Both
operations remain planned and unavailable because the reviewed MCP 4.14.0
route logs query-bearing URLs at `WARNING`, persists query-bearing error
diagnostics, cannot bind exact artifacts plus effective log threshold and
12-second timeout to the listening service identity, and can combine
`section_errors` with retry behavior to duplicate a submission. See the
[frozen stop-condition decision](agent-reach-decisions/linkedin-scraper-mcp-4.14.0.md).

Xueqiu accepts one owner-only JSON binding manifest with mode `0600`. The
manifest has exactly `protocol_version`, `capability_id`, `project_id`,
`selector`, `profile_home`, `bws_sha256`, and `server_url`. It cannot contain a
Cookie or another secret value, a BWS bootstrap/access token, provider
selection, or an injection target. Project, selector, profile, and server
locators stay on the trusted device and never enter TTY output, VPS wire data,
receipts, audit, or logs. After authorization of an exact signed grant, the
Connector resolves that capability through its isolated SecretProvider and
supplies the Cookie only to the trusted worker attempt. The secret is excluded
from frames, public results, receipts, audit, exceptions, repr, argv, paths,
logs, and persisted artifacts.

### Exact activation sequence

On the trusted device, initialize the owner-only Connector state once:

```bash
uv run hermes reach connector init \
  --role connector \
  --state-directory /absolute/connector-state
```

Start the foreground service with one explicit private or loopback address.
Provide the exact closures for the operation groups being enabled:

```bash
export HERMES_REACH_CONNECTOR_HOST="<trusted-device-private-ip>"

uv run hermes reach connector serve \
  --state-directory /absolute/connector-state \
  --bind "$HERMES_REACH_CONNECTOR_HOST" \
  --port 8765 \
  --opencli-social-node /absolute/path/to/node \
  --opencli-social-root /absolute/opencli-production-prefix \
  --opencli-social-cli /absolute/opencli-production-prefix/node_modules/@jackwener/opencli/dist/src/main.js \
  --opencli-social-session-home /absolute/trusted-session-home \
  --xueqiu-binding-manifest /absolute/owner-only-xueqiu-binding.json
```

Provide either all four OpenCLI social arguments or none of them. The Xueqiu
manifest is an optional group. Omitting a group prevents all file, network,
process, and secret access for that group. Before state unlock or listener
construction, the original terminal (TTY) displays only safe backend
identities, digests, scopes, and, when enabled, the opaque Xueqiu capability ID
for the exact selected source-operation bindings. It never displays a path,
Bitwarden project, selector, Cookie, or token. Type exactly `enable` once to
continue. Paths must be canonical, absolute, current-user owned,
non-symlinked, and not group- or world-writable. Node must be a bounded
executable regular file; the CLI must be the fixed package entry point inside
the dedicated root. Agent-Reach authoritatively revalidates the current bytes
and tree and creates a private snapshot inside every attempt.

The service waits in the locked state at the original terminal's `Connector>`
prompt. Enter `unlock` and supply the Connector passphrase on that terminal.
The WSS listener is created only after a successful unlock.

Keep the foreground service running. On the VPS, initialize its owner-only
identity state once, then pair it with the exact subset of Connector scopes the
VPS may use. This example grants the complete batch:

```bash
export HERMES_REACH_CONNECTOR_HOST="<trusted-device-private-ip>"

uv run hermes reach connector init \
  --role vps \
  --state-directory /absolute/vps-state

uv run hermes reach connector pair \
  --state-directory /absolute/vps-state \
  --connector "wss://${HERMES_REACH_CONNECTOR_HOST}:8765" \
  --device-label hermes-vps \
  --scope reddit:search.posts:public \
  --scope reddit:read.post:public \
  --scope reddit:browse.subreddit:public \
  --scope reddit:browse.hot:public \
  --scope reddit:browse.popular:public \
  --scope reddit:browse.all:public \
  --scope reddit:read.subreddit:public \
  --scope facebook:search:public \
  --scope facebook:read.profile:public \
  --scope facebook:browse.feed:account_visible \
  --scope facebook:browse.groups:account_visible \
  --scope instagram:search.users:public \
  --scope instagram:read.profile:public \
  --scope instagram:browse.user_posts:public \
  --scope instagram:browse.explore:account_visible \
  --scope twitter:search.posts:public \
  --scope xiaohongshu:search.notes:public \
  --scope "xueqiu:search.stocks:public:<opaque-capability-id>"
```

While the VPS `pair` command waits, enter `pending` at the trusted device's
`Connector>` prompt. Verify the short authentication string (SAS), identity,
scope, expiry, and usage limit shown on both original terminals. Then enter
`approve <pairing-id>` on the
trusted terminal and type exactly `approve` at its confirmation prompt. After
the VPS reports `Pairing complete.`, leave the Connector unlocked to execute
requests. Entering `lock` stops its listener. Then start the normal Hermes
process on the VPS with:

```bash
HERMES_REACH_VPS_STATE_DIRECTORY=/absolute/vps-state hermes ...
```

`HERMES_REACH_VPS_STATE_DIRECTORY` points Hermes to verified owner-only local
state at process startup. It is not a credential, is never sent in an
operation, and cannot add a scope absent from the signed grant. If it is absent,
plugin registration performs no Connector file or network work. If it is
invalid, only the 18 Connector-only operations are unavailable; all 13
artifact-independent local owner-fork bindings continue to load. Exa Web and
Code remain independently available or report `setup_required` according to
their complete artifact attestation. Web and GitHub remain planned and
unavailable regardless of Connector state. Restart Hermes after changing the
pairing, local state, Xueqiu binding manifest, or Exa artifact declarations;
the runtime is composed once during plugin registration. Connector startup
never persists the Node/OpenCLI/session paths or digests.

## Foreground lifecycle and availability

Every service process starts locked with no listener. Only `unlock` on the
original foreground terminal can decrypt the Connector identity, create the
short-lived TLS leaf key, and start WSS. `lock` and `exit` close connections and
discard the in-memory key lease. Device sleep interrupts reachability as well.

Device sleep, `lock`, or `exit` makes Connector-backed operations degraded. It
does not disable the 13 artifact-independent credential-free owner-fork
bindings or change the independently configured Exa Web/Code state. Status
reads a bounded local snapshot without contacting the Connector or probing Exa
artifacts.

A verified paired profile with a valid exact grant but no recent authenticated
snapshot normally reports `degraded`. Requests may still be dispatched, and
the first signed success can change the state to `available`. Connector-side
revocation is checked as live authority and applies to the next request without
a VPS restart. A recent signed `backend_unbound` response is cached as
`unavailable` to avoid repeatedly spending grant uses. After the trusted-device
binding is repaired, that snapshot expires in at most about 60 seconds and the
operation returns to retryable `degraded`.

The social executor may make one internal retry for a typed transient,
backend-unavailable, or deadline result. Both attempts remain inside the same
signed Connector invocation, one claimed grant use, and the original absolute
20-second budget. Hermes on the VPS does not sign a second request for this
retry. Authentication, authorization, rate-limit, invalid input, not found,
incompatibility, permanent, and contract failures are not resubmitted.

Accepted failures retain the attempted `opencli/1.8.6-hermes.1` identity and a
closed cause code in the signed receipt and evidence ledger. Query, username,
target, result data, backend stderr, and trusted-device paths are never receipt,
snapshot, or audit fields.

There is no Hermes Reach telemetry. Audit export occurs only when an operator
explicitly composes an operator-owned sink; no exporter, client, or scheduler
runs automatically.

## Grant and revocation limits

Assume a paired VPS can be fully compromised. An attacker can spend every use
remaining on the current grant and read every query result that grant allows
until the Connector claims a revocation, the grant expires, or its quota is
exhausted. Scope, expiry, and use limits reduce the loss; they do not make the
VPS trusted.

Claims are atomic and at most once. A crash after a claim commits can consume a
use without returning a result or receipt. Retrying the same request cannot
restore that use or execute it again.

Revocation applies to the next request that has not already been atomically
claimed. If a provider call was accepted before revocation won the race,
revocation cannot undo that external call. The emergency response is to lock
the local Connector first, then revoke the affected device or grant.

## Recover from key loss or compromise

If the Connector identity key is lost, restore it only from a separately
protected local backup. Without that backup, create a new Connector identity
and re-pair every VPS. A VPS cannot recover or replace the Connector identity.

If a Connector key may be compromised, lock the service, revoke its grants,
preserve any external audit checkpoints, and rotate the key or replace the
identity. Do not migrate revoked grants to the replacement identity.

If a VPS key is lost or compromised, revoke that VPS device and its grants,
create a new VPS identity, and pair it again. Do not treat a new key as a silent
continuation of the old device.

## Interpret audit evidence correctly

Signed receipts bind a request to its authority decision and result metadata.
Local ledgers add hash-chain tamper evidence, but a chain retained only on one
host is not independent proof after full compromise of that host. An attacker
who controls the host can replace both the ledger and its local checkpoints.

Retain signed receipts or periodic signed checkpoints in an operator-controlled
system outside the Connector and VPS when stronger forensic evidence is
required. Even external retention cannot protect evidence if its own keys and
storage are also compromised.

## Upgrade and rollback without reviving authority

Protocol, database schema, identity-key format, provider-wrapper compatibility,
and package release versions are separate compatibility gates. Unknown newer
formats fail closed. Installing or upgrading the package does not create an
identity, service, grant, or provider configuration automatically.

A package rollback preserves the current live authority state. It must never
restore an older revocation flag, grant revision, use counter, or policy
revision as live authority. Migration backups are recovery and forensic inputs,
not rollback snapshots. If an older package cannot read the current schema, it
must remain disabled and leave the state unchanged.

Uninstalling or rolling back code does not delete Connector state. State
deletion is a separate, explicit operator action.
