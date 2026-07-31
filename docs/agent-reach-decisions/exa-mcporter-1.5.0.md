# Exa mcporter decision for Agent-Reach 1.5.0

- Status: Web search reviewed as a conditional owner-fork binding; code search
  remains fail-closed
- Date: 2026-07-31
- Direct owner-fork operation: `exa:search.web`
- Not implemented: `exa:search.code`
- Web backend: `exa-mcporter` version `0.12.3+exa-web.v1`
- Official Agent-Reach base: `Panniantong/Agent-Reach` `1.5.0` at
  `b4d52c46c9113cb0f653d6df4cf71ebadf4930ac`
- Reviewed owner-fork batch candidate: `izumi0uu/Agent-Reach` at
  `2755b0c140a03ab5793540fb3245288891526586`
- Candidate tree: `55648469505908aa655745f5ca7704d495f12183`
- Rollback integration pin: `2a5829cf3b50bc435c647bfae4c050b1837d0235`
  (`hermes-reach-integration-0.1.0a3`)

## Decision

Implement `exa:search.web` through one closed owner-fork `execution.v1`
descriptor for the exact Agent-Reach-selected mcporter + Exa Web method. Keep
`exa:search.code` planned and unavailable because the pinned Agent-Reach
`tokensNum` call is incompatible with the live deprecated method's
`numResults` schema and special endpoint. Renaming the argument or substituting
Web search would be a semantic change, not exact reuse.

Web search has a `default_local` binding surface in the operation ledger, but
Hermes composes it only when the operator supplies one complete valid artifact
attestation. With no values or a partial or malformed declaration, status is
`setup_required`. Registration, status, and rejected requests do not
probe Node, mcporter, configuration, the provider, PATH, or the network.

The owner approved Exa-only provider query visibility on 2026-07-31. Exa sees
the submitted query and may retain it. Hermes excludes query text from argv,
environment, provenance, stable errors, receipts, and audit, but this design
does not claim that the provider does not log queries.

## Closed Descriptor And Invocation

The descriptor accepts exact `{query, limit}` arguments: a trimmed query from
1 through 4,096 characters and a non-boolean integer limit from 1 through 50.
The provider receives at most 20 results. The result is zero to 20 closed
`exa.search.result.v1` items under backend
`exa-mcporter@0.12.3+exa-web.v1`.

The fork owns one fixed invocation:

```text
NODE MCPORTER_CLI --config CONFIG --log-level error call
  --http-url https://mcp.exa.ai/mcp --name exa --tool web_search_exa
  --args - --output json --timeout 14000 --no-oauth
```

Canonical compact stdin is exactly a JSON object containing `query` and
`numResults`. The request cannot select executable, argv, endpoint, method,
MCP server name, config, environment, credential, output format, timeout, OAuth,
or fallback. The provider call is keyless and does not read a Hermes, Exa, or
Bitwarden secret.

The child runs with no shell and a sterile environment containing only private
HOME/XDG/TMP roots plus fixed locale/time values. It has no PATH, Node options,
npm variables, credentials, proxy/TLS overrides, or editor configuration. The
fork bounds stdin/stdout, uses a new process group, kills and reaps on timeout
or cancellation, rejects mutable/oversized/non-JSON MCP envelopes, and projects
only reviewed result fields.

## Artifact Capability

The descriptor requires fieldless `network_access.v1` plus
`mcporter_artifacts.v1`. Hermes builds the latter only from all seven explicit
operator values:

- absolute Node executable path and SHA-256;
- absolute mcporter root, an in-root CLI path, and the reviewed tree SHA-256;
- absolute sterile config path and SHA-256.

The config bytes are exactly `{"imports":[],"mcpServers":{}}` with no trailing
newline. The mcporter tree digest is the fork's bounded canonical tree digest,
not a plain archive checksum; operators must retain the reviewed generation
record together with the deployed tree.

The capability contains no query, endpoint, method, argv, environment, secret,
or callable. Before invocation, the fork revalidates canonical paths, exact
file/tree identities, ownership and write constraints, Node and mcporter
versions, the CLI's pinned dependency graph, and the closed sterile config.
Missing evidence maps to `backend_unavailable`; identity drift maps to
`backend_incompatible`.

Hermes starts only the fixed isolated Python worker, passes the closed
attestation in its internal length-prefixed frame, validates the exact
Agent-Reach handshake with `runtime_module="exa"`, and independently validates
the typed result before normalization. The worker process is killable but is
not a kernel syscall sandbox; the accepted artifacts remain an operator-owned
supply-chain trust boundary.

## Code Search Blocker

Official Agent-Reach 1.5.0 documents
`exa.get_code_context_exa(query, tokensNum)`. Archived live-contract evidence
shows that the current deprecated provider method rejects `tokensNum`, expects
`numResults`, and is exposed only through a special endpoint. The Web method
cannot stand in for code search. `exa:search.code` therefore remains planned,
has no descriptor or production binding, and reports unavailable.

Review code search only when a future pinned Agent-Reach contract and live
provider schema agree exactly and the same artifact, process, output, and query
visibility gates pass.

## Review Milestone

Review this decision on any official base or owner-fork commit change;
execution protocol, schema, capability, RECORD, Node/mcporter closure, tree
digest algorithm, sterile-config contract, Exa endpoint/method/schema/formatter,
provider query visibility, worker framing, backend identity, error mapping, or
projection change. Any Agent-Reach pin movement reopens all 63 operations.

## Rollout And Rollback

Candidate `2755b0c140a03ab5793540fb3245288891526586` is consumed by exact SHA for
cross-repository review and has no recovery tag. After explicit owner approval,
the rebase-integrated fork commit must be tree-equivalent, Hermes must pin its
final SHA, and all pin-sensitive gates must pass again before tagging.

Rollback restores exact pin `2a5829cf3b50bc435c647bfae4c050b1837d0235`,
recoverable through immutable tag `hermes-reach-integration-0.1.0a3`, and
returns both Exa operations to their previous planned/unbound state. The old
generic injected-client path is not restored. No credential, protocol, grant,
Connector, database, receipt, audit, or stored-data migration is required.
