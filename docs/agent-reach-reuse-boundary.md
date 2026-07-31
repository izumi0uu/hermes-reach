# Agent-Reach reuse boundary

Status: frozen on 2026-07-31 against official Agent-Reach `1.5.0` base
`b4d52c46c9113cb0f653d6df4cf71ebadf4930ac` and reviewed owner-fork integration
candidate `2755b0c140a03ab5793540fb3245288891526586`.

This document is the merge gate for source execution work. The canonical
plugin architecture and terminology are defined in
[Agent-Reach as a Hermes plugin](agent-reach-plugin-boundary.md). The complete
63-row state is recorded in
[agent-reach-operation-ledger.json](agent-reach-operation-ledger.json).

## Frozen execution ownership

```text
Hermes
  -> Hermes Reach plugin lifecycle and security/control plane
  -> owner-fork structured execution, for an exact reviewed operation
     OR an exact Agent-Reach-selected backend through a fixed thin wrapper
  -> platform
```

Official Agent-Reach owns the reviewed 15-channel baseline and backend-routing
evidence. The owner fork adds a narrow `execution.v1` boundary and currently
owns exactly two RSS, four Bilibili, three YouTube, four V2EX, and one Exa Web
operation. Hermes Reach owns
protocol admission, authorization, host capabilities, safe invocation,
isolation, normalization, bounds, redaction, receipts, availability, audit,
and rollback.

Official Agent-Reach 1.5.0 does not expose a unified operation execution API,
so direct official runtime calls remain zero. That fact does not authorize a
Hermes platform runtime. The reviewed owner fork is additive and operation
scoped; its 14 calls do not imply execution support for the other 49 Hermes
catalog rows.

The following work stays fully inside Hermes Reach and is not platform drift:

- the five public tool envelopes and versioned Hermes product catalog;
- read-only policy, grants, limits, retries, cancellation, and normalization;
- Connector identity, pairing, revocation, signed requests/results/receipts;
- SecretProvider and Bitwarden isolation; and
- redaction, local audit evidence, availability, and rollback behavior.

## Classification rules

Every catalog operation has exactly one ledger classification:

| Class | Meaning | Decision |
| --- | --- | --- |
| `direct_owner_fork_runtime` | Calls one closed structured operation shipped by the exact owner-fork commit | Allowed only after fork provenance, capability, schema, ownership, and security review |
| `exact_backend_thin_wrapper` | Calls the exact Agent-Reach-selected backend through a fixed security wrapper | Allowed when no admissible structured call exists and the wrapper adds no platform semantics |
| `implemented_but_unbound` | The Hermes v1 request contract is released, but no production executor is registered | Never counted as concrete reuse or availability |
| `hermes_native_equivalent` | Uses a different provider or mechanism | Forbidden in production under strict adapter purity |
| `reach_reimplementation` | Rewrites platform retrieval or parsing in Hermes | Forbidden in production under strict adapter purity |
| `not_implemented` | Catalog-only, with no production execution path | Planned/unavailable or explicitly setup-required |

A fork operation may own platform-specific invocation, operation selection,
source-native projection, partial classification, and backend provenance. A
thin wrapper may validate closed input, attest a package or executable, isolate
process/environment/credentials, enforce timeout and output bounds, normalize
a result, and record provenance. Neither path may expose caller-selected
endpoints, commands, argv, backends, credentials, browser actions, or fallback.
Provider-name equality alone is not execution reuse.

The 63-operation matrix is a Hermes product contract grounded in pinned
Agent-Reach evidence. It is not an official Agent-Reach operation catalog;
official 1.5.0 supplies the 15-channel registry and backend routes instead.

## Frozen audit matrix

| Source | Operations by class | Agent-Reach execution evidence | Current decision |
| --- | --- | --- | --- |
| GitHub | 8 not implemented | `gh` CLI | Planned/unavailable; former anonymous REST exception disabled |
| Twitter/X | 6 not implemented | `twitter-cli`, OpenCLI, `bird` | Planned/unavailable |
| YouTube | 3 direct owner-fork runtime calls, 1 implemented but unbound, 1 not implemented | fork `execution.v1` over `network_access.v1` and `private_workspace.v1`; `yt-dlp`; transcription pipeline | Search, read-video, and subtitles use fork-owned fixed yt-dlp execution; comments remain setup-required; transcription planned |
| Reddit | 1 exact backend thin wrapper, 6 not implemented | OpenCLI, then `rdt-cli` | Fixed OpenCLI `read.post` is Connector-only; other operations planned |
| Facebook | 4 not implemented | OpenCLI | Planned/unavailable |
| Instagram | 4 not implemented | OpenCLI | Planned/unavailable |
| Bilibili | 4 direct owner-fork runtime calls, 2 not implemented | fork `execution.v1` over `network_access.v1`; `bili-cli`, OpenCLI, transcription pipeline | Four public operations use fork-owned fixed bili-cli execution; subtitles/transcription planned |
| Xiaohongshu | 5 not implemented | OpenCLI, MCP, `xhs-cli` | Planned/unavailable |
| LinkedIn | 4 not implemented | scraper MCP, Jina fallback | Planned/unavailable |
| Xiaoyuzhou | 1 not implemented | Agent-Reach transcription scripts | Planned/unavailable |
| V2EX | 4 direct owner-fork runtime calls | fork `execution.v1` over `network_access.v1`; fixed V2EX public API contract | Default-local; fork owns bounded transport, identity correlation, partial semantics, and projection |
| Xueqiu | 4 not implemented | Agent-Reach cookie-aware API methods | Planned/unavailable |
| RSS | 2 direct owner-fork runtime calls | fork `execution.v1` over `fetched_document.v1` | Default-local; fork owns feedparser invocation and projection |
| Exa | 1 direct owner-fork runtime call, 1 not implemented | fork `execution.v1` over `network_access.v1` and `mcporter_artifacts.v1`; Exa through `mcporter` | Web search has a default-local binding surface but is `setup_required` without a complete operator artifact attestation; code search remains planned |
| Web | 1 not implemented | Agent-Reach Jina Reader method | Planned/unavailable; former direct-origin exception disabled |

The frozen accounting is:

| Accounting class | Count |
| --- | ---: |
| Hermes catalog operations | 63 |
| Catalog implemented | 16 |
| Catalog planned | 47 |
| Direct owner-fork runtime calls | 14 |
| Exact-backend thin wrappers | 1 |
| Concrete executors | 15 |
| Default-local bindings | 14 |
| Connector-only concrete bindings | 1 |
| Implemented but unbound contracts | 1 |
| Operations outside fork execution | 49 |
| Hermes-native equivalents | 0 |
| Reach reimplementations | 0 |
| Direct official Agent-Reach runtime calls | 0 |

The fifteen concrete executors are:

| Binding surface | Source | Classification | Operations |
| --- | --- | --- | --- |
| Default-local | RSS | Direct owner-fork runtime | `read.feed`, `browse.entries` |
| Default-local | Bilibili | Direct owner-fork runtime | `search.videos`, `read.video`, `browse.hot`, `browse.rank` |
| Default-local | YouTube | Direct owner-fork runtime | `search.videos`, `read.video`, `read.subtitles` |
| Default-local | V2EX | Direct owner-fork runtime | `browse.hot`, `browse.node_topics`, `read.topic`, `read.user` |
| Default-local | Exa | Direct owner-fork runtime | `search.web` (conditionally composed after complete artifact attestation) |
| Connector-only | Reddit | Exact backend thin wrapper | `read.post` |

`youtube:read.comments` is catalog-implemented but is not one of those fifteen:
it has no binding or backend attempt and remains `setup_required`. Its public
v1 `(limit,page)` contract cannot be represented by yt-dlp's bounded comment
prefix without Hermes inventing pagination semantics.

## Strict exception closure and reviewed reactivation

On 2026-07-28, the owner selected strict adapter purity and closed all thirteen
Hermes-owned platform exceptions:

- Web: one former `web-public-http-v1` path;
- GitHub: eight former `github-public-rest-v1` paths; and
- V2EX: four former `v2ex-public-api-v1` paths.

Their local endpoint and parser modules remain removed. Web and GitHub stay
planned/unavailable. The four V2EX operations were later reactivated only by
moving transport, response validation, identity semantics, and projection into
closed owner-fork descriptors; the former Hermes implementation did not
return. The architecture decision records preserve why the official 1.5.0
routes could not satisfy the security/product contract and what changed for
the reviewed V2EX reactivation:

- [Web 1.5.0](agent-reach-decisions/web-1.5.0.md)
- [GitHub gh 2.95.0](agent-reach-decisions/github-gh-2.95.0.md)
- [V2EX 1.5.0](agent-reach-decisions/v2ex-1.5.0.md)

Those records grant no Hermes-owned platform exception. A future Web or GitHub
reactivation must use a reviewed structured fork operation or exact selected
backend; it cannot silently restore the removed local implementation.

## Active execution decisions

### RSS owner-fork runtime

`rss:read.feed` and `rss:browse.entries` call the owner fork's closed
`execution.v1` API. Hermes provides an already-fetched, bounded document through
the `fetched_document.v1` host capability inside a killable worker. The fork
owns feedparser invocation, operation selection, source projection,
bozo/partial classification, and backend provenance. Hermes independently
revalidates the closed response before normalization. See
[RSS feedparser 6.0.12](agent-reach-decisions/rss-feedparser-6.0.12.md).

### Bilibili owner-fork runtime

Four credential-free operations use the fork's closed `execution.v1` API with
a fieldless `network_access.v1` marker inside a fixed isolated worker. The fork
owns the operation-to-Click argv mapping, `bilibili-cli==0.6.2` gate and
invocation, raw envelope validation, backend error mapping, and Bilibili-native
projection. Hermes owns process containment, timeout/cancellation, one bounded
retry, public normalization, receipts, and audit. Neither layer exposes login,
Cookie import, account, download, mutation, or fallback authority. See
[Bilibili CLI 0.6.2](agent-reach-decisions/bilibili-cli-0.6.2.md).

### YouTube owner-fork runtime

All three executable operations use the fork's closed `execution.v1` API with
a fieldless `network_access.v1` marker inside the fixed isolated YouTube
worker. `read.subtitles` additionally receives `private_workspace.v1`, which
authorizes only the worker's per-attempt private current directory and carries
no request-selected path. The fork owns exact yt-dlp/EJS/Deno validation,
fixed metadata/search/subtitle calls, language and manual-before-automatic
selection, subtitle file safety, raw result validation, error mapping,
identity correlation, and YouTube-native projection. Hermes owns public
validation, process containment, timeout/cancellation, retry policy,
independent result validation and URL correlation, product normalization,
receipts, and audit. See
[YouTube yt-dlp 2026.7.4](agent-reach-decisions/youtube-yt-dlp-2026.7.4.md).

### V2EX owner-fork runtime

All four V2EX operations use the fork's closed `execution.v1` API with
`network_access.v1`. The fork fixes the public origin and paths, performs
DNS-pinned proxy-free bounded HTTPS, validates the complete JSON document,
owns node/page/limit and topic/reply/user identity semantics, classifies
not-found and partial-reply outcomes, and emits typed native projections.
Hermes owns the fixed worker, timeout/cancellation, retry policy, independent
result validation, product normalization, receipts, and audit. Hermes contains
no V2EX endpoint or upstream response parser. See
[V2EX 1.5.0](agent-reach-decisions/v2ex-1.5.0.md).

### Exa Web owner-fork runtime

`exa:search.web` has a closed owner-fork descriptor for the exact fixed
`mcporter==0.12.3` `web_search_exa` route. It needs both
`network_access.v1` and `mcporter_artifacts.v1`; the latter carries only the
operator-attested absolute Node, mcporter tree/CLI, and sterile-config
identities. With no attestation, local status is `setup_required` and no
backend is probed or launched. The query is sent through stdin, never argv or
environment, and is excluded from Hermes receipts and audit; Exa still sees
the query and may retain it. `exa:search.code` remains planned because the
pinned `tokensNum` contract is incompatible with the live deprecated method's
`numResults` schema. See
[Exa mcporter 1.5.0](agent-reach-decisions/exa-mcporter-1.5.0.md).

### Reddit Connector exact backend

`reddit:read.post` uses the exact OpenCLI-first route selected by Agent-Reach.
It is never a default-local binding. Explicit trusted-device executable
attestation, an exact Connector executor, paired VPS state, and a live exact
grant are all required. The request cannot choose argv, executable, browser
profile, credential, or fallback.

## Why the earlier drift happened

The drift began in planning, before Connector work. Official Agent-Reach 1.5.0
exposes channel metadata and health checks but no stable, structured
operation-level execution API. Its skill routes an agent across shell commands,
MCP methods, browser-session tools, HTTP helpers, and local configuration.
Exposing that surface unchanged would violate the stolen-VPS threat model.

The safety adapter was necessary, but the project then used "Hermes owns
execution" too broadly. The local operation catalog and runtime were built
before the Agent-Reach bridge, and Web/V2EX parsing plus GitHub REST became
permanent platform implementations without a migration deadline.
"Agent-Reach-compatible" obscured the difference between using similar
semantics and actually reusing a selected route.

Strict closure first reduced platform-execution drift from 13 of 23 concrete
paths to zero of 10. Moving RSS and Bilibili invocation/projection into the
owner fork raised structured reuse from zero to six operations, and moving
YouTube `read.video` raised it to seven. This homogeneous public-platform batch
then migrated the two remaining YouTube operations, reactivated four V2EX
operations without restoring Hermes parsing, and added the exact Exa Web route.
The result is 14 direct owner-fork operations and one exact-backend wrapper,
with zero Hermes-native or reimplementation paths among 15 concrete executors.
The remaining Hermes code is plugin lifecycle, the v1 product contract,
security controls, host capabilities, normalization, Connector, secrets,
receipts, and audit, not a copied platform runtime.

## Merge gate for source execution

1. Do not add or expand Hermes-owned platform retrieval logic.
2. A new implemented operation must identify pinned official evidence and use
   either a reviewed structured fork operation or an exact selected backend
   thin wrapper.
3. Fork growth must be narrow and additive: one or more individually closed
   source-operation contracts may ship in a homogeneous reviewed batch, with
   no generic command/backend dispatch and no Hermes types or authority in the
   fork.
4. Fork `main` remains a fast-forward mirror of official `main`; execution work
   is rebased onto a recorded official base and consumed by exact commit.
5. Keep `hermes-reach-integration-0.1.0a2` as the immutable reference for
   `f195253d53befdb012d7aa575e732ec627ec29ac`, and keep
   `hermes-reach-integration-0.1.0a3` as the protected immutable reference for
   the previous integration `2a5829cf3b50bc435c647bfae4c050b1837d0235`.
   Candidate `2755b0c140a03ab5793540fb3245288891526586` receives no tag before
   explicit merge approval and final-pin validation. Hermes never depends on
   either tag; the exact commit pin is authoritative.
6. Migrated platform invocation and projection must be removed from Hermes so
   there is one platform-semantics owner.
7. A catalog contract classified `implemented_but_unbound` remains
   `setup_required` or unavailable and is never counted as a concrete executor.
8. Public input never selects command, argv, executable, endpoint, backend,
   MCP method, Cookie, credential, browser action, or fallback.
9. An official-base or fork-pin change reopens all 63 operation reviews and the
   static capability handshake.
10. Connector and SecretProvider work may continue when it adds security and
    control capability without adding platform semantics.

The complete machine-readable classifications live in
[agent-reach-operation-ledger.json](agent-reach-operation-ledger.json). The
smaller [agent-reach-reuse-decisions.json](agent-reach-reuse-decisions.json)
contains 24 detailed P0/P1/P2 reviews: ten not-implemented decisions and all
14 direct owner-fork decisions. The Connector-only Reddit wrapper and
implemented-but-unbound YouTube comments contract remain in the complete
ledger and their dedicated tests.

Rollback of the current batch restores the previous Hermes release and exact
pin `2a5829cf3b50bc435c647bfae4c050b1837d0235`, whose immutable recovery
reference is `hermes-reach-integration-0.1.0a3`. It restores YouTube search and
subtitles to their exact wrappers and returns V2EX and Exa to their prior
disabled state. The older pins
`f195253d53befdb012d7aa575e732ec627ec29ac` and
`806205fd106f4f4453624becfd773acce8418cf1` remain reachable through
`hermes-reach-integration-0.1.0a2` and
`hermes-reach-integration-0.1.0a1`, respectively. No public protocol, grant,
Connector, database, receipt, or audit migration is needed; no recovery tag
may move.
