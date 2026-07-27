# GitHub gh decision for Agent-Reach 1.5.0

Status: owner-approved public-authority exception on 2026-07-27.

## Context

Agent-Reach `1.5.0` at commit
`1494c2ab239e7355a77e7cceaf3271453a1f34b5` selects GitHub CLI command
families for repository and code search, repository/issue/pull-request reads,
Actions, and releases. The reviewed local executable was `gh 2.95.0`.

Hermetic tests with GitHub configuration and token variables removed showed
that both `gh search` and `gh api` exit with authentication required. Ambient
authentication is not an acceptable substitute: a normal user token and local
`gh` configuration can expose private repositories, code, issues, pull
requests, runs, drafts, and releases, while all eight Reach operations are
credential-free and public.

## Decision

Retain the fixed anonymous `github-public-rest-v1` adapter at public scope as
an explicit authority-narrowing exception. Do not inherit `GH_TOKEN`,
`GITHUB_TOKEN`, GitHub CLI configuration, keychain state, enterprise hosts, or
workspace repository context, and do not compose `gh` in the current runtime.

The reviewed read-only command families are `gh search repos`,
`gh search code`, `gh repo view`, `gh issue view`, `gh pr view`,
`gh run list`, `gh run view`, and `gh release list`. Request data may never
select flags, executable paths, commands, endpoints, credentials, or fallback.

## Semantic delta

The exception preserves anonymous public authority, DNS-pinned proxy-free
transport, bounded streaming, and the current normalized response contract.
The upstream CLI would require authentication, changes repository-search
ordering, loses release fields, collapses the existing HTTP error taxonomy,
and owns different DNS/TLS behavior. Those differences prevent an honest
zero-delta migration.

## Revisit milestone

Revisit when GitHub or Agent-Reach provides a credential-free `gh` route, or
when an issuer-backed credential can be proven incapable of reading private
content. A future CLI design must also attest the absolute executable, isolate
configuration and environment, use fixed read-only argv, bound stdout, and
kill and reap the process on timeout or cancellation. General account access
requires a separate account-visible catalog and grant design.

## Rollback

This decision changes no runtime behavior. Reverting its record and CI rows
returns the exception to unresolved status; it does not authorize a `gh`
migration.
