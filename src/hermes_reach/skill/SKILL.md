---
name: agent-reach
description: Route bounded, read-only internet retrieval through Hermes Reach.
---

# Agent Reach for Hermes

This skill adapts the routing scope of Agent-Reach 1.5.0 from official baseline
`b4d52c46c9113cb0f653d6df4cf71ebadf4930ac` through exact owner-fork
commit `281dc3352c63cdb644f02e028cc5d645c279954a` to the Hermes Reach safety
contract. Fork execution protocol v1 owns exactly 29 direct owner-fork
operations: two RSS operations
(`rss:read.feed`, `rss:browse.entries`) and four Bilibili operations
(`bilibili:search.videos`, `bilibili:read.video`, `bilibili:browse.hot`,
`bilibili:browse.rank`), all three executable YouTube operations
(`youtube:search.videos`, `youtube:read.video`, `youtube:read.subtitles`), all
four V2EX operations, Exa `exa:search.web`, and all 15 catalog operations for
Reddit, Facebook, and Instagram through the exact pinned fork-owned social
runtime. The 15 social operations are Connector-only and
require exact `public` or `account_visible` grants; Hermes owns no social
platform command or parser. Exa Web still reports
`setup_required` until the operator supplies the complete reviewed artifact
attestation. Fork execution does not make the other 34 catalog operations
executable.

## Execution Boundary

- Use only `reach_status`, `reach_search`, `reach_read`, `reach_browse`, and
  `reach_transcribe` for internet retrieval covered by this skill.
- Do not bypass these tools with another execution surface or a
  provider-specific action.
- Treat `setup_required`, `degraded`, `unsupported_environment`, and
  `unavailable` as final capability states. Report the state and remediation;
  do not work around it.
- Keep every request read-only. Never perform an external mutation.

## Routing Workflow

1. Call `reach_status` for the intended sources and inspect the currently
   declared operations and availability.
2. Choose an explicit source and an operation returned for that source.
3. Use `reach_search` for queries, `reach_read` for one resource,
   `reach_browse` for a source-native collection, or `reach_transcribe` for an
   explicitly supported media target.
4. Preserve source grouping, backend provenance, native ordering, partial
   results, and continuation data in the response.
5. Only `reach_search` may group sources, with one to five explicit sources.
   Never interpret an omitted source list as all sources and never fan out to
   all channels implicitly.

## Intent Routing

The status result remains authoritative for exact operations, binding class,
and runtime availability. Use this table only to select the likely source.

| Source | Retrieval intent |
| --- | --- |
| `web` | Read a public web page. |
| `exa` | Search the public web or code corpus when locally available. |
| `rss` | Read or browse a one-shot RSS or Atom feed. |
| `github` | Search and read repositories, code, issues, pull requests, runs, or releases. |
| `youtube` | Search or read YouTube video metadata, subtitles, or comments. |
| `bilibili` | Search or read Bilibili videos, hot lists, or rankings. |
| `xiaoyuzhou` | Read or transcribe supported podcast material. |
| `v2ex` | Browse or read V2EX topics, replies, nodes, or users. |
| `twitter` | Search or read supported Twitter/X public or granted account-visible material. |
| `reddit` | Search, read, or browse supported Reddit communities. |
| `xiaohongshu` | Search, read, or browse supported Xiaohongshu material. |
| `facebook` | Search, read, or browse supported Facebook material. |
| `instagram` | Search users or read and browse supported Instagram material. |
| `linkedin` | Search jobs or read supported LinkedIn profiles and company material. |
| `xueqiu` | Search or read supported Xueqiu market and discussion material. |

## Result Handling

- An `ok` group is usable within its reported bounds.
- A `partial` outcome keeps successful groups and source-local failures
  separate; do not discard successful sources.
- An `error` or unavailable group must be reported using its normalized code
  and safe remediation.
- Do not claim a source is supported merely because it appears in this routing
  table. Availability is local, operation-specific, and environment-specific.
