# Connector security and operations

> [!IMPORTANT]
> Hermes Reach is pre-alpha. The runtime can deliver an authorized operation to
> a Connector executor only through an explicitly registered exact binding.
> `ConnectorService` authorizes and delivers that fixed operation; it does not
> select a provider, credential, browser session, or local path. Default
> Connector execution composition remains unbound; this is distinct from the
> local execution surface, which has 14 owner-fork operations (RSS 2,
> Bilibili 4, YouTube 3, V2EX 4, and Exa Web 1). The first 13 are composed and
> available without artifact setup; Exa Web is composed only after a complete
> operator-supplied Node/mcporter/config attestation. The
> only Connector production composition
> path is the explicit two-sided activation of Reddit `read.post` through one
> attested OpenCLI executable. Treat the controls below as approval for that
> exact pre-alpha slice only, not for arbitrary commands or production
> credentials.

## Supported topology

The trusted-device Connector supports macOS and Linux. The remote virtual
private server (VPS) supports Linux. Windows is unsupported; the implementation must fail closed rather than
weaken file permissions, terminal unlock, process locking, or certificate
checks.

Start the Connector only on a trusted device and give it one explicit numeric
loopback or private-network address. The listener accepts only WebSocket Secure
(WSS) over Transport Layer Security (TLS). It rejects wildcard, public,
multicast, ambiguous link-local, and plaintext WebSocket
binds. In particular, never expose it through `0.0.0.0`, `::`, or a public
address. Private reachability, including Tailscale or Headscale membership, does
not grant authority by itself.

The Connector installs no launchd or systemd unit. It runs only as a foreground
process attached to its original controlling terminal.

## Prepare an isolated secrets profile

Use a dedicated Hermes profile for the Connector. Keep the normal Hermes
SecretSource registry disabled in that profile so startup cannot apply secrets
to the process-wide environment.

Create a dedicated Bitwarden Secrets Manager machine account and project that
contains only the keys needed by approved Reach backends. The Connector helper
fetches the dedicated project in isolated memory before selecting one binding,
so project-level least privilege is a required security boundary. Do not reuse
a broad personal or application project.

Provide the Bitwarden Secrets Manager (BWS) bootstrap token through the
operator-controlled profile
environment. Do not put it in Reach configuration or copy it to the VPS. The
runtime does not install or update `bws`, use stale values, or use plaintext or
encrypted Bitwarden caches.

## Reddit OpenCLI executor boundary

`reddit:read.post` is the only account-session operation with an implemented
source executor. It is disabled by default. The trusted device must explicitly
enable its exact executor and the Hermes process on the VPS must explicitly load
the matching paired state before it can become available.

The VPS may send only a canonical HTTPS Reddit post URL. The trusted executor
derives the post ID and invokes the operator-selected absolute OpenCLI binary
with one fixed `reddit read` argument vector. It does not accept a command,
provider, executable path, browser profile, session identifier, local path,
process environment, or fallback from the request. Nearby OpenCLI write
commands such as reply, save, subscribe, and vote are outside the binding.

OpenCLI uses the trusted device's existing local browser session. This path has
no Bitwarden `SecretExecutionPlan`, and the executor rejects any non-empty
secret environment. The process receives only allowlisted local `HOME`, `PATH`,
locale, timezone, and temporary-directory fields; stderr is discarded and
stdout is size-bounded before a closed safe-YAML parser maps it to normalized
post and reply items. Parser drift, process failure, timeout, and cancellation
fail closed without returning provider output.

Package import, `reach_status`, doctor, and default Hermes plugin startup do not
find OpenCLI, inspect a browser, start a daemon, or register this executor. An
explicit composition decision is therefore also the rollback point: removing
the binding restores signed `backend_unbound` behavior without changing browser
or Bitwarden state.

This unbound Connector default does not make the local execution surface empty.
It contains two direct owner-fork RSS bindings, four direct owner-fork
Bilibili/bili-cli bindings, three direct owner-fork YouTube bindings, four
direct owner-fork V2EX bindings, and one direct owner-fork Exa Web contract.
The first 13 are composed without artifact setup. Exa Web is composed only
after its complete seven-field artifact attestation is present; otherwise it
reports `setup_required`. `youtube:read.comments` is implemented but unbound
and also reports `setup_required`. The Connector contributes only the
fifteenth concrete executor, `reddit:read.post`, after both explicit activation
gates pass; it does not replace or proxy the 14 local operations.

The RSS fork path is credential-free and local. Hermes gives it only an
already-fetched bounded document through `fetched_document.v1`; it receives no
Bitwarden secret plan, Connector identity, grant, paired-device state, browser
session, or Connector execution authority.

The Bilibili fork path is also credential-free and local. Hermes gives it only
a fieldless `network_access.v1` marker inside a private worker; the marker
contains no endpoint, proxy, Cookie, credential, path, command, backend
selector, or Connector authority. It is explicit host approval for four fixed
registered operations, not generic network access or an OS sandbox.

All three executable YouTube operations use the same fieldless marker only
after the fixed YouTube worker validates the exact fork runtime. Subtitles also
receive a fieldless private-workspace marker for the worker's per-attempt
current directory. The fork owns the pinned yt-dlp calls, subtitle-file safety,
and native projection.

V2EX uses four fixed fork descriptors and the fork-owned bounded public API
transport. Exa Web uses one fixed fork descriptor and a closed mcporter
artifact capability containing only operator-declared absolute paths and
digests. Neither receives Connector identity, grant, Bitwarden secret, browser
session, or remote execution authority. Exa receives each Web query directly
and may retain it; Hermes does not persist the query in receipts or audit.

### Exact activation sequence

On the trusted device, initialize the owner-only Connector state once:

```bash
uv run hermes reach connector init \
  --role connector \
  --state-directory /absolute/connector-state
```

Start the foreground service with one explicit private or loopback address and
the canonical OpenCLI executable candidate:

```bash
uv run hermes reach connector serve \
  --state-directory /absolute/connector-state \
  --bind 100.64.0.10 \
  --port 8765 \
  --reddit-opencli /absolute/path/to/opencli
```

Before state unlock or listener construction, the original terminal (TTY) displays
`reddit:read.post:public`, the resolved path, and its SHA-256. Type exactly
`enable` to continue. The executable must resolve to a bounded regular file,
be owned by the current user or root, be executable, have one hard link, and
not be group- or world-writable. Its metadata and digest are rechecked
immediately before every spawn. The current implementation does not use
`fexecve`; replacement in the small interval between recheck and path-based
spawn remains a trusted-device time-of-check-to-time-of-use (TOCTOU)
limitation. For script executables, the
shebang interpreter and the allowlisted `PATH` are also outside the attested
file digest and must be controlled by the trusted-device operator.

The service now waits at its original-terminal `Connector>` prompt in the
locked state. Enter `unlock` and supply the Connector passphrase on that same
terminal. The WSS listener is constructed only after this unlock succeeds.

Keep the foreground service running. On the VPS, initialize its owner-only
identity state once and pair it with exactly the public Reddit post-read scope:

```bash
uv run hermes reach connector init \
  --role vps \
  --state-directory /absolute/vps-state

uv run hermes reach connector pair \
  --state-directory /absolute/vps-state \
  --connector wss://100.64.0.10:8765 \
  --device-label hermes-vps \
  --scope reddit:read.post:public
```

While the VPS `pair` command waits, enter `pending` at the trusted device's
`Connector>` prompt. Verify the short authentication string (SAS), identity,
scope, expiry, and usage limit
shown on both original terminals, then enter `approve <pairing-id>` on the
trusted terminal and type exactly `approve` at its confirmation prompt. After
the VPS reports `Pairing complete.`, leave the Connector unlocked to execute
requests. Entering `lock` stops its listener. Then start the normal Hermes
process on the VPS with:

```bash
HERMES_REACH_VPS_STATE_DIRECTORY=/absolute/vps-state hermes ...
```

`HERMES_REACH_VPS_STATE_DIRECTORY` is only a process-start pointer to verified
owner-only local state. It is not a credential, is never sent in an operation,
and cannot add a scope absent from the signed grant. If it is absent, plugin
registration performs no Connector file or network work. If it is invalid,
only `reddit:read.post` is unavailable; all 13 artifact-independent local
owner-fork bindings continue to load. Exa Web is independently available or
`setup_required` according to its complete artifact attestation. Web and GitHub
remain planned/unavailable independently of Connector state. Pairing, local
state, or Exa artifact declarations require restarting Hermes because the
runtime is composed once at plugin registration. Connector startup never
persists the OpenCLI path or digest.

## Foreground lifecycle and availability

Every service process starts locked with no listener. Only `unlock` on the
original foreground terminal can decrypt the Connector identity, create the
short-lived TLS leaf key, and start WSS. `lock` and `exit` close connections and
discard the in-memory key lease. Device sleep interrupts reachability as well.

Sleep, lock, or exit therefore makes Connector-backed operations degraded. It
does not disable the 13 artifact-independent credential-free owner-fork
bindings or change the independently configured Exa Web state. Status reads a
bounded local snapshot and does not contact the Connector or probe Exa
artifacts.

A verified paired profile with a valid exact grant but no recent authenticated
snapshot normally reports `degraded`; this state is dispatchable so the first
signed success can change it to `available`. Connector-side revocation is
checked as live authority and applies to the next request without a VPS
restart. A recent signed `backend_unbound` response is intentionally cached as
`unavailable` to avoid repeatedly spending grant uses. After the trusted-device
binding is repaired, that snapshot expires in at most about 60 seconds and the
operation returns to retryable `degraded`.

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
