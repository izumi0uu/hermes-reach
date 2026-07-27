# Connector security and operations

> [!IMPORTANT]
> Hermes Reach is pre-alpha. The runtime can deliver an authorized operation to
> a Connector executor only through an explicitly registered exact binding.
> `ConnectorService` authorizes and delivers that fixed operation; it does not
> select a provider, credential, browser session, or local path. The default
> production runtime registers no Connector bindings and the production executor
> composition is empty. Treat the controls below as the boundary for ongoing
> implementation, not as approval to place production credentials behind the
> Connector today.

## Supported topology

The trusted-device Connector supports macOS and Linux. The remote VPS supports
Linux. Windows is unsupported; the implementation must fail closed rather than
weaken file permissions, terminal unlock, process locking, or certificate
checks.

Start the Connector only on a trusted device and give it one explicit numeric
loopback or private-network address. The listener accepts WSS only. It rejects
wildcard, public, multicast, ambiguous link-local, and plaintext WebSocket
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

Provide the BWS bootstrap token through the operator-controlled profile
environment. Do not put it in Reach configuration or copy it to the VPS. The
runtime does not install or update `bws`, use stale values, or use plaintext or
encrypted Bitwarden caches.

## Foreground lifecycle and availability

Every service process starts locked with no listener. Only `unlock` on the
original foreground terminal can decrypt the Connector identity, create the
short-lived TLS leaf key, and start WSS. `lock` and `exit` close connections and
discard the in-memory key lease. Device sleep interrupts reachability as well.

Sleep, lock, or exit therefore makes Connector-backed operations degraded. It
does not disable credential-free local Web, RSS, V2EX, or GitHub operations.
Status reads a bounded local snapshot and does not contact the Connector.

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
