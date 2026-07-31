# GitHub gh disabled-path decision for Agent-Reach 1.5.0

- Status: disabled
- Date: 2026-07-28
- Historical exception review: 2026-07-27
- Operations: `github:search.repositories`, `github:search.code`,
  `github:read.repository`, `github:read.issue`, `github:read.pull_request`,
  `github:browse.actions`, `github:read.action_run`,
  `github:browse.releases`
- Classification: `not_implemented`
- Current backend: none

The public-authority exception approved on 2026-07-27 is retained below only
as historical evidence.

## Context

Official Agent-Reach `1.5.0` at base commit
`b4d52c46c9113cb0f653d6df4cf71ebadf4930ac`, carried by owner-fork
original integration commit `f195253d53befdb012d7aa575e732ec627ec29ac` and
revalidated at current integration
`2a5829cf3b50bc435c647bfae4c050b1837d0235`, selects GitHub CLI command
families for repository and code search, repository/issue/pull-request reads,
Actions, and releases. The reviewed local executable was `gh 2.95.0`. The fork
execution v1 ledger still contains no GitHub capability.

Hermetic tests with GitHub configuration and token variables removed showed
that both `gh search` and `gh api` exit with authentication required. Ambient
authentication is not an acceptable substitute: a normal user token and local
`gh` configuration can expose private repositories, code, issues, pull
requests, runs, drafts, and releases, while all eight Reach operations are
credential-free and public.

## Decision

Disable the fixed anonymous `github-public-rest-v1` adapter and classify all
eight GitHub operations as `not_implemented`. They remain discoverable in the
stable Hermes catalog as planned/unavailable, with no current backend. Do not
inherit `GH_TOKEN`, `GITHUB_TOKEN`, GitHub CLI configuration, keychain state,
enterprise hosts, or workspace repository context, and do not compose `gh` in
the current runtime.

The earlier anonymous REST exception narrowed authority safely, but it still
made Hermes own GitHub endpoints and parsing. Strict adapter purity closes that
exception rather than moving it into a local or maintained-fork runtime.

The reviewed read-only command families are `gh search repos`,
`gh search code`, `gh repo view`, `gh issue view`, `gh pr view`,
`gh run list`, `gh run view`, and `gh release list`. Request data may never
select flags, executable paths, commands, endpoints, credentials, or fallback.

## Historical semantic delta

The former exception preserved anonymous public authority, DNS-pinned
proxy-free transport, bounded streaming, and the normalized response contract.
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

Operational rollback keeps all eight GitHub operations planned and
unavailable. Execution may return only through a reviewed structured
Agent-Reach execution capability or exact Agent-Reach-selected backend with the
required safety properties; the former REST adapter cannot be restored as an
exception. This record alone does not authorize a `gh` migration, and no data
migration is required.
