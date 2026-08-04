# Agent-Reach reuse boundary

Reviewed on 2026-08-02 against official Agent-Reach `1.5.0`, base
`b4d52c46c9113cb0f653d6df4cf71ebadf4930ac`. Owner-fork PR #6's final reviewed
head is `e91e3efa045e75f08d4e7fdd9749fe26d4f774c5`, with tree
`e86ee839621360b991d985ad9d4cb18e36f86351`. It was rebase-merged with tree
equivalence into `hermes/execution-v1` as integration commit
`75cd48c6274e7f4740530d97877ec048708d5334`. Hermes pins that exact commit,
which contains exactly 33 descriptors. Final Hermes checks for provenance,
RECORD contents, runtime, pin sensitivity, and exact artifacts are complete.

Protected immutable recovery reference `hermes-reach-integration-0.1.0a4`
points to the integration commit. The tag is recovery authority only, never a
dependency selector. Rejected pre-freeze commit
`7bc42839d3dd290e4af93b24e0b03b738cff0ffa`, with tree
`382557e0bec76819f0633f31895580a0f549b6bd`, remains as rejection evidence
because it contains two unsafe LinkedIn descriptors.

Current recovery mapping: `hermes-reach-integration-0.1.0a4` ->
`75cd48c6274e7f4740530d97877ec048708d5334`.

This document sets the merge requirements for source execution work. The
plugin architecture and terminology are defined in
[Agent-Reach as a Hermes plugin](agent-reach-plugin-boundary.md). The complete
63-row state is recorded in
[agent-reach-operation-ledger.json](agent-reach-operation-ledger.json).

## Frozen execution ownership

```text
Hermes
  -> Hermes Reach plugin lifecycle and security/control plane
  -> owner-fork structured execution, for an exact reviewed operation
  -> platform
```

Official Agent-Reach provides the reviewed 15-channel baseline and the
evidence for its backend routes. The owner fork adds the narrow `execution.v1`
boundary. Its 33 operations are two RSS, four Bilibili, three YouTube, four
V2EX, one Exa Web, seven Reddit, four Facebook, four Instagram, and four
accepted searches for Twitter, Xiaohongshu, Xueqiu, and Exa Code. Hermes Reach
handles protocol admission, authorization, host capabilities, safe invocation,
isolation, normalization, bounds, redaction, receipts, availability, audit,
and rollback.

Official Agent-Reach 1.5.0 does not expose a unified operation execution API,
so direct official runtime calls remain zero. Hermes must not fill that gap
with its own platform runtime. The owner fork covers only its 33 reviewed
operations. It does not provide execution for the other 30 Hermes catalog
rows.

These responsibilities stay in Hermes Reach and do not count as platform
retrieval logic:

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

A fork operation may handle platform-specific invocation, operation selection,
source-native projection, partial classification, and backend provenance. A
thin wrapper may validate closed input, attest a package or executable, isolate
the process, environment, and credentials, enforce timeout and output bounds,
normalize a result, and record provenance. Neither path may expose endpoints,
commands, argv, backends, credentials, browser actions, or fallback choices to
the caller. Matching provider names alone does not count as execution reuse.

The 63-operation matrix is a Hermes product contract grounded in pinned
Agent-Reach evidence. It is not an official Agent-Reach operation catalog;
official 1.5.0 supplies the 15-channel registry and backend routes instead.

## Frozen audit matrix

| Source | Operations by class | Agent-Reach execution evidence | Current decision |
| --- | --- | --- | --- |
| GitHub | 8 not implemented | `gh` CLI | Planned/unavailable; former anonymous REST exception disabled |
| Twitter/X | 1 direct owner-fork runtime call, 5 not implemented | fork `execution.v1` over `opencli_session.v1`; OpenCLI | Search is Connector-only; remaining operations planned |
| YouTube | 3 direct owner-fork runtime calls, 1 implemented but unbound, 1 not implemented | fork `execution.v1` over `network_access.v1` and `private_workspace.v1`; `yt-dlp`; transcription pipeline | Search, read-video, and subtitles use fork-owned fixed yt-dlp execution; comments remain setup-required; transcription planned |
| Reddit | 7 direct owner-fork runtime calls | fork `execution.v1` over `opencli_session.v1`; OpenCLI | Connector-only; fork owns all commands, parsing, bounds, and projection |
| Facebook | 4 direct owner-fork runtime calls | fork `execution.v1` over `opencli_session.v1`; OpenCLI | Connector-only with exact public/account-visible grants |
| Instagram | 4 direct owner-fork runtime calls | fork `execution.v1` over `opencli_session.v1`; typed OpenCLI owner artifact | Connector-only with exact public/account-visible grants |
| Bilibili | 4 direct owner-fork runtime calls, 2 not implemented | fork `execution.v1` over `network_access.v1`; `bili-cli`, OpenCLI, transcription pipeline | Four public operations use fork-owned fixed bili-cli execution; subtitles/transcription planned |
| Xiaohongshu | 1 direct owner-fork runtime call, 4 not implemented | fork `execution.v1` over `opencli_session.v1`; OpenCLI | Search is Connector-only with session-bearing URL material removed; remaining operations planned |
| LinkedIn | 4 not implemented | reviewed `linkedin-scraper-mcp==4.14.0` methods retained as rejection evidence | All operations planned/unavailable; people/jobs search failed the frozen stop condition; Jina and generic MCP fallback forbidden |
| Xiaoyuzhou | 1 not implemented | Agent-Reach transcription scripts | Planned/unavailable |
| V2EX | 4 direct owner-fork runtime calls | fork `execution.v1` over `network_access.v1`; fixed V2EX public API contract | Default-local; fork owns bounded transport, identity correlation, partial semantics, and projection |
| Xueqiu | 1 direct owner-fork runtime call, 3 not implemented | fork `execution.v1` over `xueqiu_session.v1`; fixed Xueqiu stock-search API | Connector-only; Cookie resolves after exact authorization through SecretProvider |
| RSS | 2 direct owner-fork runtime calls | fork `execution.v1` over `fetched_document.v1` | Default-local; fork owns feedparser invocation and projection |
| Exa | 2 direct owner-fork runtime calls | fork `execution.v1` over `network_access.v1` and `mcporter_artifacts.v1`; distinct Exa Web/Code methods | Both have default-local binding surfaces and remain `setup_required` without complete artifact attestation; no cross-fallback |
| Web | 1 not implemented | Agent-Reach Jina Reader method | Planned/unavailable; former direct-origin exception disabled |

Current totals:

| Accounting class | Count |
| --- | ---: |
| Hermes catalog operations | 63 |
| Catalog implemented | 34 |
| Catalog planned | 29 |
| Direct owner-fork runtime calls | 33 |
| Exact-backend thin wrappers | 0 |
| Concrete executors | 33 |
| Default-local bindings | 15 |
| Connector-only concrete bindings | 18 |
| Implemented but unbound contracts | 1 |
| Operations outside fork execution | 30 |
| Hermes-native equivalents | 0 |
| Reach reimplementations | 0 |
| Direct official Agent-Reach runtime calls | 0 |

The 33 concrete executors are:

| Binding surface | Source | Classification | Operations |
| --- | --- | --- | --- |
| Default-local | RSS | Direct owner-fork runtime | `read.feed`, `browse.entries` |
| Default-local | Bilibili | Direct owner-fork runtime | `search.videos`, `read.video`, `browse.hot`, `browse.rank` |
| Default-local | YouTube | Direct owner-fork runtime | `search.videos`, `read.video`, `read.subtitles` |
| Default-local | V2EX | Direct owner-fork runtime | `browse.hot`, `browse.node_topics`, `read.topic`, `read.user` |
| Default-local | Exa | Direct owner-fork runtime | `search.web`, `search.code` (conditionally composed after complete artifact attestation) |
| Connector-only | Reddit | Direct owner-fork runtime | `search.posts`, `read.post`, `browse.subreddit`, `browse.hot`, `browse.popular`, `browse.all`, `read.subreddit` |
| Connector-only | Facebook | Direct owner-fork runtime | `search`, `read.profile`, `browse.feed`, `browse.groups` |
| Connector-only | Instagram | Direct owner-fork runtime | `search.users`, `read.profile`, `browse.user_posts`, `browse.explore` |
| Connector-only | Twitter/X | Direct owner-fork runtime | `search.posts` |
| Connector-only | Xiaohongshu | Direct owner-fork runtime | `search.notes` |
| Connector-only | Xueqiu | Direct owner-fork runtime | `search.stocks` |

`youtube:read.comments` has a catalog implementation but is not one of those
33 executors. It has no binding or backend attempt and remains
`setup_required`. yt-dlp's bounded comment prefix cannot represent the public
v1 `(limit,page)` contract without Hermes inventing pagination semantics.

## Closed exceptions and V2EX reactivation

On 2026-07-28, the owner chose strict adapter purity. This closed all thirteen
Hermes-owned platform exceptions:

- Web: one former `web-public-http-v1` path;
- GitHub: eight former `github-public-rest-v1` paths; and
- V2EX: four former `v2ex-public-api-v1` paths.

The local endpoint and parser modules for all 13 paths remain removed. Web and
GitHub stay planned/unavailable. The four V2EX operations returned only after
transport, response validation, identity semantics, and projection moved into
closed owner-fork descriptors. The former Hermes implementation did not
return. These decision records explain why the official 1.5.0 routes did not
meet the security and product contract, and what changed before V2EX was
approved:

- [Web 1.5.0](agent-reach-decisions/web-1.5.0.md)
- [GitHub gh 2.95.0](agent-reach-decisions/github-gh-2.95.0.md)
- [V2EX 1.5.0](agent-reach-decisions/v2ex-1.5.0.md)

These records do not allow a Hermes-owned platform exception. Web or GitHub
can return only through a reviewed structured fork operation or the exact
selected backend. The removed local implementation cannot be restored.

## Active execution decisions

### RSS owner-fork runtime

`rss:read.feed` and `rss:browse.entries` call the owner fork's closed
`execution.v1` API. Hermes passes an already-fetched, bounded document through
the `fetched_document.v1` host capability inside a killable worker. The fork
runs feedparser, selects the operation, projects the source result, classifies
bozo and partial results, and records backend provenance. Hermes validates the
closed response again before normalization. See
[RSS feedparser 6.0.12](agent-reach-decisions/rss-feedparser-6.0.12.md).

### Bilibili owner-fork runtime

Four credential-free operations use the fork's closed `execution.v1` API with
a fieldless `network_access.v1` marker inside a fixed, isolated worker. The fork
maps each operation to fixed Click argv, checks and runs
`bilibili-cli==0.6.2`, validates the raw envelope, maps backend errors, and
projects Bilibili-native results. Hermes runs it in a contained process,
handles timeout and cancellation, allows one bounded retry, normalizes the
public result, and writes receipts and audit records. Neither layer exposes
login, Cookie import, account, download, mutation, or fallback authority. See
[Bilibili CLI 0.6.2](agent-reach-decisions/bilibili-cli-0.6.2.md).

### YouTube owner-fork runtime

All three executable operations use the fork's closed `execution.v1` API with
a fieldless `network_access.v1` marker inside the fixed isolated YouTube
worker. `read.subtitles` also receives `private_workspace.v1`, which
authorizes only the worker's private current directory for that attempt. It
contains no request-selected path. The fork validates the exact yt-dlp, EJS,
and Deno installation; makes fixed metadata, search, and subtitle calls;
selects the language and prefers manual subtitles over automatic ones; checks
subtitle files; validates raw results; maps errors; correlates identity; and
projects YouTube-native results. Hermes validates public input and the returned
result, runs the fork in a contained process, handles timeout and cancellation,
applies retry policy, checks URL correlation, normalizes the product result,
and writes receipts and audit records. See
[YouTube yt-dlp 2026.7.4](agent-reach-decisions/youtube-yt-dlp-2026.7.4.md).

### V2EX owner-fork runtime

All four V2EX operations use the fork's closed `execution.v1` API with
`network_access.v1`. The fork fixes the public origin and paths, uses bounded
HTTPS with pinned DNS and no proxy, validates the complete JSON document,
applies node/page/limit and topic/reply/user identity rules, classifies
not-found and partial-reply outcomes, and returns typed native projections.
Hermes supplies the fixed worker, handles timeout and cancellation, applies
retry policy, validates the result again, normalizes the product result, and
writes receipts and audit records. Hermes contains no V2EX endpoint or
upstream response parser. See
[V2EX 1.5.0](agent-reach-decisions/v2ex-1.5.0.md).

### Exa Web and Code owner-fork runtime

`exa:search.web` has a closed owner-fork descriptor for the exact fixed
`mcporter==0.12.3` `web_search_exa` route. It needs both
`network_access.v1` and `mcporter_artifacts.v1`; the latter carries only the
operator-attested identities of the absolute Node executable, mcporter tree
and CLI, and sterile config. Without that attestation, local status is
`setup_required`; Hermes does not probe or launch the backend. Hermes sends the
query through stdin, never argv or the environment, and excludes it from
receipts and audit. Exa still receives the query and may retain it.
`exa:search.code` separately fixes the `get_code_context_exa` endpoint and live
`query + numResults` schema. Web search cannot satisfy its Code grammar. See
[Exa mcporter 1.5.0](agent-reach-decisions/exa-mcporter-1.5.0.md).

### OpenCLI social owner-fork runtime

All 17 Reddit, Facebook, Instagram, Twitter, and Xiaohongshu operations call
closed `execution.v1` descriptors with `opencli_session.v1`. None is a
default-local binding. The trusted Connector attests exact Node and OpenCLI
closure identities, keeps the browser session, requires an exact signed
`public` or `account_visible` grant, and starts one fixed isolated worker. Only
the fork selects commands, enforces the no-lifecycle-mutation guard, parses
YAML and errors, applies platform bounds, correlates results, and projects the
output. Hermes contains no social OpenCLI argv or platform parser. See
[OpenCLI social 1.8.6-hermes.1](agent-reach-decisions/opencli-social-1.8.6-hermes.1.md).

### LinkedIn stop condition

LinkedIn people/jobs search remains planned, unbound, and unavailable. The
reviewed `linkedin-scraper-mcp==4.14.0` route was rejected because it logs
query-bearing URLs at `WARNING`, persists query-bearing error diagnostics,
cannot bind the reviewed wheel hashes, effective log threshold, and 12-second
timeout to the already-listening service identity, and can combine native
`section_errors` with retry behavior to duplicate a submission. One reviewed
backend or service contract must close all four gaps before the decision can
reopen. See the
[LinkedIn stop-condition decision](agent-reach-decisions/linkedin-scraper-mcp-4.14.0.md).

### Xueqiu Connector runtime

Xueqiu stock search resolves one opaque capability through Connector
SecretProvider only after exact grant authorization. Only the fork defines the
fixed origin and request, validates the response, applies identity rules, and
projects the result. The Cookie cannot enter VPS frames, results, receipts,
audit, repr, logs, argv, paths, or persisted artifacts.

## Migration history

Official Agent-Reach 1.5.0 exposes channel metadata and health checks but has
no stable, structured operation-level execution API. Its skill uses shell
commands, MCP methods, browser-session tools, HTTP helpers, and local
configuration. That interface cannot be exposed unchanged under the stolen-VPS
threat model, so Hermes Reach added a fixed safety adapter.

The initial design gave "Hermes owns execution" too broad a scope. The local
catalog and runtime predated the Agent-Reach bridge; Web and V2EX parsers and
the GitHub REST client then remained in Hermes without a migration deadline.
The label "Agent-Reach-compatible" did not distinguish similar behavior from
execution through an Agent-Reach-selected route.

| Change | Result |
| --- | --- |
| Close Hermes-owned platform paths | Platform-execution drift fell from 13 of 23 concrete paths to zero of 10. |
| Move RSS and Bilibili invocation and projection to the fork | Structured reuse rose from zero to six operations. |
| Move YouTube `read.video` | Structured reuse rose to seven operations. |
| Move the other two YouTube operations, reactivate four V2EX operations without restoring Hermes parsing, and add Exa Web | The public-platform batch moved to fork execution. |
| Add the social operations | The baseline reached 29 direct owner-fork operations. |
| Review six final candidates and accept four | The total reached 33; the two LinkedIn candidates remained planned. |

All 33 concrete executors are direct owner-fork operations. There are no
exact-backend wrappers, Hermes-native equivalents, or Reach reimplementations.
Hermes contains the plugin lifecycle, v1 product contract, security controls,
host capabilities, normalization, Connector, secrets, receipts, and audit. It
contains no copied platform runtime.

## Merge gate for source execution

1. Do not add or expand Hermes-owned platform retrieval logic.
2. A new implemented operation must identify pinned official evidence and use
   either a reviewed structured fork operation or an exact selected backend
   thin wrapper.
3. Fork changes must be narrow and additive. A batch may contain one or more
   individually closed source-operation contracts only when they form a
   homogeneous reviewed group. It must not add generic command or backend
   dispatch, Hermes types, or Hermes authority to the fork.
4. Fork `main` remains a fast-forward mirror of official `main`; execution work
   is rebased onto a recorded official base and consumed by exact commit.
5. Keep `hermes-reach-integration-0.1.0a2` as the immutable reference for
   `f195253d53befdb012d7aa575e732ec627ec29ac`, and keep
   `hermes-reach-integration-0.1.0a3` as the protected immutable reference for
   the previous integration `2a5829cf3b50bc435c647bfae4c050b1837d0235`.
   The final 33-descriptor integration
   `75cd48c6274e7f4740530d97877ec048708d5334`, with tree
   `e86ee839621360b991d985ad9d4cb18e36f86351`, is the exact dependency pin.
   It is the tree-equivalent rebase integration of owner-fork PR #6's final
   reviewed head `e91e3efa045e75f08d4e7fdd9749fe26d4f774c5` into
   `hermes/execution-v1`. Protected lightweight tag
   `hermes-reach-integration-0.1.0a4` points directly to the integration commit.
   Active repository ruleset `Protect Hermes Reach integration tags`
   (`19975135`) blocks update and deletion and has no bypass actor. Final Hermes
   provenance, RECORD, runtime, pin-sensitive, and exact-artifact verification
   are complete; package publication remains a separate versioned release.
   Rejected pre-freeze commit `7bc42839d3dd290e4af93b24e0b03b738cff0ffa`, with tree
   `382557e0bec76819f0633f31895580a0f549b6bd`, contains the rejected
   35-descriptor state and remains rejection evidence only. Rollback integration
   `281dc3352c63cdb644f02e028cc5d645c279954a`, with tree
   `385b9c95cb3a6372ed1b68b606abc3faed71f307`, was rebase-merged from reviewed
   hardlink-fix head `c57ae5b8d78fed6ad52a1f52731db589d875f8a9` with an identical tree.
   The previous pre-hardlink-fix 29-operation integration
   `ec4a5e36434c9df9ee236dc12734843163fc17ac`, tree
   `302db7526ed84b1565fa24baf5c06ced69385d80`, was rebase-merged from reviewed
   head `a3dcdb3a6638e14ceda8cfa9a3cc7a010d80fa80`; it remains provenance only
   because it is incompatible with uv hardlink installs. The social-disable
   rollback pin `9b69146588b1d162515b81db26b51643c15de8eb`
   was rebase-merged from reviewed head
   `fd93d2ec86511a4a1514b7ebd13cd996be709692`; both have tree
   `e19835071ae6560431b66d5a21e51b598d3d9c81`. Hermes never depends on a
   recovery tag; the exact commit pin is authoritative.
6. Migrated platform invocation and projection must be removed from Hermes so
   that one component owns platform semantics.
7. A catalog contract classified `implemented_but_unbound` remains
   `setup_required` or unavailable and is never counted as a concrete executor.
8. Public input never selects command, argv, executable, endpoint, backend,
   MCP method, Cookie, credential, browser action, or fallback.
9. Changing the official base or fork pin reopens all 63 operation reviews and
   the static capability handshake.
10. Connector and SecretProvider work may continue when it adds security and
    control capability without adding platform semantics.

The complete machine-readable classifications are in
[agent-reach-operation-ledger.json](agent-reach-operation-ledger.json). The
smaller [agent-reach-reuse-decisions.json](agent-reach-reuse-decisions.json)
contains 44 detailed P0/P1/P2 reviews: 11 not-implemented decisions and all
33 direct owner-fork decisions. The implemented-but-unbound YouTube comments
contract remains in the complete ledger and its dedicated tests.

Protected immutable reference `hermes-reach-integration-0.1.0a4` keeps the
current 33-operation integration reachable. Rolling back the current batch
restores the previous Hermes release and exact pin
`281dc3352c63cdb644f02e028cc5d645c279954a`. The rollback removes the four
accepted search descriptors and bindings but keeps the preceding 29-operation
integration. The older pin
`2a5829cf3b50bc435c647bfae4c050b1837d0235`, whose immutable recovery
reference is `hermes-reach-integration-0.1.0a3`, and the older pins
`f195253d53befdb012d7aa575e732ec627ec29ac` and
`806205fd106f4f4453624becfd773acce8418cf1` remain reachable through
`hermes-reach-integration-0.1.0a2` and
`hermes-reach-integration-0.1.0a1`, respectively. No public protocol, grant,
Connector, database, receipt, or audit migration is needed; no recovery tag
may move.
