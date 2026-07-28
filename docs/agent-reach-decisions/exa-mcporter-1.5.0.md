# Exa mcporter decision for Agent-Reach 1.5.0

Status: owner-approved fail-closed decision on 2026-07-27.

## Context

Agent-Reach `1.5.0` at commit
`1494c2ab239e7355a77e7cceaf3271453a1f34b5` selects these methods:

- `search.web`: `exa.web_search_exa(query, numResults)` through mcporter.
- `search.code`: `exa.get_code_context_exa(query, tokensNum)` through
  mcporter.

Hermes Reach previously exposed an injected `AuditedExaClient` that returned
already normalized items. Provider-name attestation did not prove that the
client used either selected method or mcporter, so it was not execution reuse.

## Decision

Remove the generic client activation path and classify both operations as not
implemented. Their request schemas remain reserved in the catalog, and local
status remains `setup_required` without discovering or executing a backend.

This is a fail-closed correction, not an Exa production activation. No runtime
may install Node or mcporter, import editor configuration, start OAuth, read an
API key, choose an endpoint or method from request data, or claim availability
from a typed interface.

## Blockers

- Agent-Reach does not pin the mcporter or Node artifact and dependency tree.
- The live `get_code_context_exa` schema rejects the pinned `tokensNum`
  argument, expects a different result-count argument, and exposes the method
  only through an explicit deprecated-tool endpoint.
- The hosted Exa implementation logs the submitted query, contradicting the
  former `logs_queries=False` eligibility claim.
- mcporter result formatting and the hosted Exa deployment are mutable and do
  not provide a pinned structured response contract.

Changing `tokensNum` to `numResults`, replacing code search with Web search, or
accepting provider-side query logging would be a new semantic/privacy decision,
not an exact wrapper around the pinned route.

## Revisit milestone

Revisit on the next Agent-Reach pin that supplies compatible method schemas,
or when Hermes Reach approves all of the following together: exact Node and
mcporter dependency attestation, fixed stdin-only calls, sterile configuration
and environment, bounded output, kill-and-reap cancellation, closed result
parsing, and explicit provider-query retention policy.

## Rollback

Keep both Exa operations planned and unbound if this review record is rolled
back or replaced. Execution may be enabled only through a reviewed official
callable or the exact Agent-Reach-selected mcporter route; the pre-alpha
generic injected-client API must not be restored. There is no persisted data
or credential migration.
