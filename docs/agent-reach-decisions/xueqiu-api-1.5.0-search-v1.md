# Xueqiu Stock Search Runtime Decision

## Decision

`xueqiu:search.stocks` executes only through the closed
`agent_reach.execution.v1` Xueqiu runtime in the owner-maintained fork. It is a
Connector-only `direct_owner_fork_runtime` operation using
`xueqiu-api/1.5.0+search.v1` and one `xueqiu_session.v1` capability.

## Provenance

- Agent-Reach official base:
  `b4d52c46c9113cb0f653d6df4cf71ebadf4930ac`
- Accepted Agent-Reach candidate commit:
  `ee200e7160c4b093a2ba0fcee9f2a6842aefe20d`
- Accepted Agent-Reach candidate tree:
  `56883c0872bed94050660b16d1ade2e46f73fef9`
- rollback integration:
  `281dc3352c63cdb644f02e028cc5d645c279954a`

The candidate is pinned by exact commit. It is the unmerged and untagged head
of owner-fork PR #6 and is not release-eligible under this task.

## Secret Boundary

The signed grant contains only one opaque capability ID. After exact grant
authorization, the trusted Connector resolves that ID through the existing
isolated `SecretProvider`/Bitwarden boundary. Project, selector, Bitwarden
profile, `bws` binary, bootstrap token, server, Cookie value, and local paths
remain Connector-local.

Activation reads one mode-`0600`, owner-only manifest with exactly
`protocol_version`, `capability_id`, `project_id`, `selector`, `profile_home`,
`bws_sha256`, and `server_url`. The locator fields are necessary to construct
the fixed `BitwardenSecretBinding`; they never enter TTY output, VPS wire data,
receipts, audit, or logs. The manifest cannot contain a Cookie or other secret
value, BWS bootstrap/access token, provider selection, or injection target.

The Cookie is copied into a one-attempt mutable capability and cleared on all
success, failure, deadline, and cancellation paths. It may not enter the VPS,
public result, receipt, audit, repr, log, argv, path, persisted artifact, or
exception text. Discovery, status, registration, and rejected authorization do
not resolve the secret or invoke the backend.

## Ownership And Transport

Agent-Reach alone owns the fixed Xueqiu HTTPS origin and stock-search request,
Cookie header construction, global-address DNS policy, pinned connection,
redirect/proxy rejection, response bounds, JSON validation, stock identity,
projection, and error classification. Hermes owns signed authorization,
one-use secret resolution, isolated worker framing, independent closed-result
validation, normalization, receipts, availability, and audit.

There is no ambient Agent-Reach config, browser-cookie extraction, homepage
fallback, inherited proxy, global Cookie jar, endpoint selector, or alternate
backend. Authentication, rate limit, transport drift, content drift, identity
mismatch, or oversized output fails through a stable redacted code.

## Rollback

Rollback restores Agent-Reach pin
`281dc3352c63cdb644f02e028cc5d645c279954a` and removes the single Connector
binding. Existing grants may retain their opaque capability value but become
unavailable; no database, receipt, audit, or secret migration is required.
