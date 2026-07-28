# Agent-Reach reuse boundary

Status: frozen on 2026-07-28 against official Agent-Reach `1.5.0` at commit
`1494c2ab239e7355a77e7cceaf3271453a1f34b5`.

This document is the merge gate for source execution work. The canonical
plugin architecture and terminology are defined in
[Agent-Reach as a Hermes plugin](agent-reach-plugin-boundary.md).

## Frozen execution ownership

```text
Hermes
  -> Hermes Reach plugin lifecycle and security/control plane
  -> official Agent-Reach callable, when one already exists and is admissible
     OR an exact Agent-Reach-selected backend through a fixed thin wrapper
  -> platform
```

Hermes Reach owns protocol, authorization, safe invocation, isolation,
normalization, bounds, redaction, receipts, availability, audit, and rollback.
Official Agent-Reach or its exact selected backend owns platform retrieval
semantics.

Agent-Reach 1.5.0 does not expose a unified operation execution API. Zero
direct official runtime calls is therefore an accurate integration property,
not a coverage deficit or a Hermes implementation backlog. Hermes must not
create or maintain a replacement runtime, locally or in a fork, to change that
number.

The following work stays fully inside Hermes Reach and is not platform drift:

- the five public tool envelopes and versioned Hermes product catalog;
- read-only policy, grants, limits, retries, cancellation, and normalization;
- Connector identity, pairing, revocation, signed requests/results/receipts;
- SecretProvider and Bitwarden isolation; and
- redaction, local audit evidence, availability, and rollback behavior.

## Classification rules

Every catalog operation has exactly one review classification:

| Class | Meaning | Decision |
| --- | --- | --- |
| Direct upstream runtime | Calls a structured execution method already shipped by the official pinned Agent-Reach commit | Allowed when reviewed and safe |
| Exact backend thin wrapper | Calls the exact Agent-Reach-selected backend through a fixed security wrapper | Normal Agent-Reach 1.5.0 integration |
| Implemented but unbound | The Hermes v1 request contract is released, but no production executor is registered | Never counted as concrete reuse or availability |
| Hermes-native equivalent | Uses a different provider or mechanism | Forbidden in production under strict adapter purity |
| Reach reimplementation | Rewrites platform retrieval or parsing | Forbidden in production under strict adapter purity |
| Not implemented | Catalog-only, with no production execution path | Planned/unavailable or explicitly setup-required |

A thin wrapper may validate closed input, attest a package or executable,
isolate process/environment/credentials, enforce timeout and output bounds,
normalize a result, and record provenance. It may not invent platform
endpoints, selectors, pagination, response parsing, credential import, or
fallback. Provider-name equality alone is not execution reuse.

The 63-operation matrix is a Hermes product contract grounded in pinned
Agent-Reach evidence. It is not an official Agent-Reach operation catalog;
upstream 1.5.0 supplies the 15-channel registry and backend routes instead.

## Frozen audit matrix

| Source | Operations by class | Upstream execution evidence | Current decision |
| --- | --- | --- | --- |
| GitHub | 8 not implemented | `gh` CLI | Planned/unavailable; former anonymous REST exception disabled |
| Twitter/X | 6 not implemented | `twitter-cli`, OpenCLI, `bird` | Planned/unavailable |
| YouTube | 3 exact backend thin wrappers, 1 implemented but unbound, 1 not implemented | `yt-dlp`; transcription pipeline | Search, metadata, and subtitles use pinned yt-dlp; comments remain setup-required; transcription planned |
| Reddit | 1 exact backend thin wrapper, 6 not implemented | OpenCLI, then `rdt-cli` | Fixed OpenCLI `read.post` is Connector-only; other operations planned |
| Facebook | 4 not implemented | OpenCLI | Planned/unavailable |
| Instagram | 4 not implemented | OpenCLI | Planned/unavailable |
| Bilibili | 4 exact backend thin wrappers, 2 not implemented | `bili-cli`, OpenCLI, transcription pipeline | Four public operations use pinned bili-cli; subtitles/transcription planned |
| Xiaohongshu | 5 not implemented | OpenCLI, MCP, `xhs-cli` | Planned/unavailable |
| LinkedIn | 4 not implemented | scraper MCP, Jina fallback | Planned/unavailable |
| Xiaoyuzhou | 1 not implemented | Agent-Reach transcription scripts | Planned/unavailable |
| V2EX | 4 not implemented | Agent-Reach channel methods over public API | Planned/unavailable; former local reimplementation disabled |
| Xueqiu | 4 not implemented | Agent-Reach cookie-aware API methods | Planned/unavailable |
| RSS | 2 exact backend thin wrappers | `feedparser` route | Pinned feedparser worker over bounded bytes |
| Exa | 2 not implemented | Exa through `mcporter` | No binding; artifact, schema, and query-retention gates remain open |
| Web | 1 not implemented | Agent-Reach Jina Reader method | Planned/unavailable; former direct-origin exception disabled |

The frozen accounting is:

| Accounting class | Count |
| --- | ---: |
| Hermes catalog operations | 63 |
| Catalog implemented | 11 |
| Catalog planned | 52 |
| Concrete exact-backend executors | 10 |
| Default-local bindings | 9 |
| Connector-only concrete bindings | 1 |
| Implemented but unbound contracts | 1 |
| Hermes-native equivalents | 0 |
| Reach reimplementations | 0 |
| Direct official Agent-Reach runtime calls | 0 |

The ten concrete exact-backend executors are:

| Binding surface | Source | Operations |
| --- | --- | --- |
| Default-local | RSS | `read.feed`, `browse.entries` |
| Default-local | Bilibili | `search.videos`, `read.video`, `browse.hot`, `browse.rank` |
| Default-local | YouTube | `search.videos`, `read.video`, `read.subtitles` |
| Connector-only | Reddit | `read.post` |

`youtube:read.comments` is catalog-implemented but is not one of those ten:
it has no binding or backend attempt and remains `setup_required`. Its public
v1 `(limit,page)` contract cannot be represented by yt-dlp's bounded comment
prefix without Hermes inventing pagination semantics.

## Strict exception closure

On 2026-07-28, the owner selected strict adapter purity and closed all thirteen
Hermes-owned platform exceptions:

- Web: one former `web-public-http-v1` path;
- GitHub: eight former `github-public-rest-v1` paths; and
- V2EX: four former `v2ex-public-api-v1` paths.

Their operation names remain stable and discoverable, but their catalog state
is planned and their availability is unavailable. Their local endpoint and
parser modules are removed. The historical architecture decision records
(ADRs) preserve the reason
the official 1.5.0 route could not satisfy the security/product contract and
the evidence required for reactivation:

- [Web 1.5.0](agent-reach-decisions/web-1.5.0.md)
- [GitHub gh 2.95.0](agent-reach-decisions/github-gh-2.95.0.md)
- [V2EX 1.5.0](agent-reach-decisions/v2ex-1.5.0.md)

Those records are disabled decision evidence, not approved exceptions. A
future reactivation must use an official callable or exact selected backend;
it cannot silently restore the removed local implementation.

## Active exact-backend decisions

### RSS

`rss:read.feed` and `rss:browse.entries` use `feedparser==6.0.12`, the exact
backend selected by Agent-Reach. Hermes retains proxy-free, DNS-pinned bounded
fetching, then gives only a `BytesIO` stream of bounded bytes to a killable
worker. Feedparser owns feed dialect, encoding recovery, native entry order,
and field extraction. See
[RSS feedparser 6.0.12](agent-reach-decisions/rss-feedparser-6.0.12.md).

### Bilibili

Four credential-free operations use `bilibili-cli==0.6.2` through a fixed
worker and closed operation-to-argv mapping. The wrapper exposes no login,
Cookie import, account, download, mutation, or fallback authority. See
[Bilibili CLI 0.6.2](agent-reach-decisions/bilibili-cli-0.6.2.md).

### YouTube

`search.videos`, `read.video`, and `read.subtitles` use
`yt-dlp==2026.7.4`, `yt-dlp-ejs==0.8.0`, and `deno==2.8.3` in a fixed isolated
worker. Hermes owns the closed public limit/language mapping, default
`zh-Hans -> zh -> en` preference, manual-before-automatic choice, identity and
file checks, projection, and bounds. yt-dlp owns native extraction ordering,
metadata and caption discovery, download behavior, and YouTube network
semantics. See
[YouTube yt-dlp 2026.7.4](agent-reach-decisions/youtube-yt-dlp-2026.7.4.md).

### Reddit Connector

`reddit:read.post` uses the exact OpenCLI-first route selected by Agent-Reach.
It is never a default-local binding. Explicit trusted-device executable
attestation, an exact Connector executor, paired VPS state, and a live exact
grant are all required. The request cannot choose argv, executable, browser
profile, credential, or fallback.

## Why the earlier drift happened

The drift began in planning, before Connector work. Agent-Reach 1.5.0 exposes
channel metadata and health checks but no stable, structured operation-level
execution API. Its skill routes an agent across shell commands, MCP methods,
browser-session tools, HTTP helpers, and local configuration. Exposing that
surface unchanged would violate the stolen-VPS threat model.

The safety adapter was necessary, but the project then used "Hermes owns
execution" too broadly. The local operation catalog and runtime were built
before the official Agent-Reach bridge, and Web/V2EX parsing plus GitHub REST
became permanent platform implementations without a migration deadline.
"Agent-Reach-compatible" obscured the difference between using similar
semantics and actually reusing the selected route.

The strict closure reduces platform-execution drift from 13 of 23 concrete
paths to zero of 10. The remaining Hermes code is not a copied platform
runtime: it is plugin lifecycle, the v1 product contract, security controls,
normalization, Connector, secrets, receipts, and audit.

## Merge gate for source execution

1. Do not add or expand Hermes-owned platform retrieval logic.
2. A new operation must identify pinned official evidence and use either an
   existing official callable or an exact selected backend thin wrapper before
   it becomes catalog-implemented.
3. Missing official execution APIs are upstream boundaries, not permission to
   implement a local or maintained-fork runtime.
4. An unbound contract remains `setup_required` or unavailable and is never
   counted as a concrete executor.
5. Public input never selects command, argv, executable, endpoint, backend,
   MCP method, Cookie, credential, browser action, or fallback.
6. An Agent-Reach pin change reopens all 63 operation reviews.
7. Connector and SecretProvider work may continue when it adds security and
   control capability without adding platform semantics.

The machine-readable reviewed rows live in
[agent-reach-reuse-decisions.json](agent-reach-reuse-decisions.json). CI keeps
their classifications disjoint and review-visible; code review still owns the
semantic decision. That manifest contains the 24 P0/P1/P2 review-wave evidence rows: 15
not-implemented decisions (13 closed exceptions plus two Exa rows) and nine
default-local exact wrappers. The Connector-only Reddit wrapper and the
implemented-but-unbound YouTube comments contract are frozen in their dedicated
Connector/runtime tests rather than duplicated in this manifest.
