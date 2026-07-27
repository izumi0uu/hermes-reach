# Agent-Reach Reuse Boundary

Status: frozen on 2026-07-27 against Agent-Reach `1.5.0` at commit
`1494c2ab239e7355a77e7cceaf3271453a1f34b5`.

This document is the merge gate for source execution work. It distinguishes
using Agent-Reach as an upstream execution authority from merely copying its
platform list or choosing a similar backend.

## Frozen Ownership

```text
Hermes
  -> Hermes Reach protocol, authorization, safe invocation, normalization,
     bounds, redaction, receipts, and audit
  -> Agent-Reach callable or Agent-Reach-selected backend
  -> platform
```

Hermes Reach owns the control and security plane. Agent-Reach, or the exact
external backend selected by its pinned release, owns platform retrieval
semantics. A Reach wrapper may validate a closed request, invoke fixed methods
or argv, isolate credentials and processes, normalize a bounded result, and
record redacted evidence. It must not add platform endpoints, selectors,
pagination rules, response parsers, cookie behavior, or fallback semantics.

The following work remains fully inside the Hermes Reach boundary and is not
architecture drift:

- the five public tool envelopes and versioned catalog;
- read-only policy, grants, limits, retries, cancellation, and normalization;
- Connector identity, pairing, revocation, signed requests/results/receipts;
- SecretProvider and Bitwarden isolation; and
- redaction, local audit evidence, availability, and rollback behavior.

## Classification Rules

Every implemented `source + operation` has exactly one execution class:

| Class | Meaning | Default decision |
| --- | --- | --- |
| Direct upstream runtime | Calls a pinned, structured Agent-Reach execution method | Preferred |
| Exact backend thin wrapper | Calls the same pinned external execution mechanism with fixed inputs and a security wrapper | Allowed |
| Hermes-native equivalent | Uses a different provider or mechanism for the same operation | Exception required |
| Reach reimplementation | Rewrites platform retrieval or parsing, even against the same endpoint | Exception required |
| Not implemented | Catalog-only, with no production execution path | Must remain unavailable |

Provider identity alone is not execution reuse. The removed generic Exa client
could not become an exact wrapper merely by attesting the provider name while
bypassing Agent-Reach's `mcporter` path. Likewise, calling the same public V2EX
API through new request and parsing code is a reimplementation.

## Frozen Audit Matrix

The denominator is the 63 read-only operations in the Hermes Reach catalog.
Writing and account mutation commands documented by Agent-Reach are excluded.

| Source | Operations by class | Upstream execution evidence | Current decision |
| --- | --- | --- | --- |
| GitHub | 8 Hermes-native equivalent | `gh` CLI | P0 public-authority exception approved; anonymous REST retained |
| Twitter/X | 6 not implemented | `twitter-cli`, OpenCLI, `bird` | Frozen before implementation |
| YouTube | 4 exact backend thin contracts, 1 not implemented | `yt-dlp`; transcription pipeline | Contracts remain unbound until a concrete backend passes review |
| Reddit | 1 exact backend thin wrapper, 6 not implemented | OpenCLI, then `rdt-cli` | Fixed OpenCLI `read.post` is the reference pattern; other operations frozen |
| Facebook | 4 not implemented | OpenCLI | Frozen before implementation |
| Instagram | 4 not implemented | OpenCLI | Frozen before implementation |
| Bilibili | 4 exact backend thin contracts, 2 not implemented | `bili-cli`, OpenCLI, transcription pipeline | Contracts remain unbound until a concrete backend passes review |
| Xiaohongshu | 5 not implemented | OpenCLI, MCP, `xhs-cli` | Frozen before implementation |
| LinkedIn | 4 not implemented | scraper MCP, Jina fallback | Frozen before implementation |
| Xiaoyuzhou | 1 not implemented | Agent-Reach transcription scripts | Frozen before implementation |
| V2EX | 4 Reach reimplementations | Agent-Reach channel methods over public API | Grandfathered; P1 migration/exception review |
| Xueqiu | 4 not implemented | Agent-Reach cookie-aware API methods | Frozen before implementation |
| RSS | 2 Reach reimplementations | `feedparser` route | Grandfathered; P1 migration/exception review |
| Exa | 2 not implemented | Exa through `mcporter` | Generic client removed; exact route blocked by artifact, schema, and query-logging gaps |
| Web | 1 Hermes-native equivalent | Agent-Reach Jina Reader method | P0 safety exception approved; bounded direct origin reader retained |

| Execution classification | All 63 operations | 24 catalog-implemented operations |
| --- | ---: | ---: |
| Direct Agent-Reach execution | 0 (0%) | 0 (0%) |
| Exact backend thin wrapper | 9 (14.3%) | 9 (37.5%) |
| Hermes-native equivalent | 9 (14.3%) | 9 (37.5%) |
| Reach reimplementation | 6 (9.5%) | 6 (25.0%) |
| Not implemented | 39 (61.9%) | 0 |

The exact implemented-operation classifications are frozen as follows:

| Class | Source | Operations |
| --- | --- | --- |
| Exact backend thin wrapper | YouTube | `search.videos`, `read.video`, `read.subtitles`, `read.comments` |
| Exact backend thin wrapper | Reddit | `read.post` |
| Exact backend thin wrapper | Bilibili | `search.videos`, `read.video`, `browse.hot`, `browse.rank` |
| Hermes-native equivalent | GitHub | `search.repositories`, `search.code`, `read.repository`, `read.issue`, `read.pull_request`, `browse.actions`, `read.action_run`, `browse.releases` |
| Hermes-native equivalent | Web | `read.url` |
| Reach reimplementation | V2EX | `browse.hot`, `browse.node_topics`, `read.topic`, `read.user` |
| Reach reimplementation | RSS | `read.feed`, `browse.entries` |
| Not implemented | Exa | `search.web`, `search.code` |

Only 16 operations currently have a concrete retrieval implementation: 15
default-bound Web/RSS/V2EX/GitHub operations are Reach-owned and the Reddit
operation is a fixed upstream-selected OpenCLI wrapper that remains unbound by
default. The YouTube and Bilibili rows are typed, audited contracts, not
concrete production backend implementations. Exa is catalog-planned with no
production backend interface.

## P0 Review Resolution

The P0 review is resolved without pretending unsafe compatibility is reuse.
The machine-readable review set is
[`agent-reach-reuse-decisions.json`](agent-reach-reuse-decisions.json), and
the source decisions are:

- [`web-1.5.0.md`](agent-reach-decisions/web-1.5.0.md): keep
  `web-public-http-v1` because the pinned synchronous Jina callable cannot
  preserve cancellation, streamed raw-size bounds, transport isolation, or
  the retention claim.
- [`github-gh-2.95.0.md`](agent-reach-decisions/github-gh-2.95.0.md): keep
  anonymous `github-public-rest-v1` because isolated `gh` requires
  authentication and ambient authentication can exceed public authority.
- [`exa-mcporter-1.5.0.md`](agent-reach-decisions/exa-mcporter-1.5.0.md):
  remove the generic injected-client path and keep both rows planned because
  mcporter is unpinned, code-search arguments have drifted, and the provider
  logs queries.

This correction removes a dishonest activation surface but does not change
the 16 concrete retrieval paths. Overall execution-ownership drift therefore
remains approximately 6.5/10; the difference is that every P0 deviation now
has evidence, an approval disposition, a rollback, and a review milestone.

## Drift Assessment

Relative to the intended `Hermes -> Agent-Reach -> backend` product shape, the
current overall drift is **medium-high, approximately 6.5/10**. This is a
decision aid, not a code-volume formula:

| Axis | Drift | Evidence |
| --- | --- | --- |
| Platform execution ownership | High | No implemented operation calls an Agent-Reach execution path; 15 of 16 concrete paths are Reach-owned |
| Catalog/backend metadata | Low | All 15 channels are projected and compatibility-checked from the pinned upstream registry |
| Platform logic duplication | Local but material | 6 of 24 implemented rows, all RSS/V2EX, repeat platform logic |
| Security and control plane | Minimal | Connector, grants, secret isolation, bounds, receipts, and audit have no upstream equivalent |

This is not a claim that 65% of the repository should be removed. Most code is
the required security and product control plane. The drift concerns the
ownership of platform-specific retrieval behavior.

There are two valid baselines. Against the previously written Trellis design,
implementation drift is low because that design explicitly assigned execution
to Hermes Reach. Against the user's original product goal of placing
Agent-Reach inside Hermes and maximizing its reuse, drift is medium-high. The
planning decision itself moved the boundary, which is why normal conformance
reviews did not flag the product-level change.

## Why The Drift Happened

The drift began in planning, before Connector work. Agent-Reach 1.5.0 exposes
channel metadata and health checks but no stable, structured, operation-level
execution API. Its skill directs an agent to a mixture of shell commands, MCP
methods, browser-session tools, HTTP helpers, and local configuration. Running
that surface unchanged cannot satisfy a stolen-VPS threat model with exact
grants, fixed argv, credential isolation, bounded output, and signed receipts.

That explains why a Hermes safety adapter is necessary. It does not, by
itself, justify replacing the backend or rewriting platform logic. Agent-Reach
already contains a Web read method and four V2EX read methods. During Alpha-1,
the project first built its own runtime and adapters, then added the upstream
catalog/doctor bridge. The temporary rule "Hermes owns execution" consequently
expanded into new HTTP transports, parsers, and GitHub REST routes, and it had
no migration deadline. The phrase "Agent-Reach-compatible" hid the difference
between compatibility and reuse.

The repository history makes that sequence visible:

| Commit | Boundary effect |
| --- | --- |
| `efb7839` | Created the five tools and a Reach-owned 15-platform operation catalog before the Agent-Reach dependency was integrated |
| `281bb89` | Added policy, bounded dispatch, references, and audit; these are necessary Hermes control-plane capabilities |
| `40bfb70` | Added Reach-owned Web/RSS/V2EX transport and parsing, the first material platform-execution drift |
| `d88b7f5` | Replaced upstream GitHub `gh` execution with anonymous REST and added unbound media interfaces |
| `9d1da20` | Added the Agent-Reach catalog/doctor bridge after the local execution layer already existed |
| `483ff17` | Registered a restricted Hermes skill instead of raw upstream skill commands; this was a necessary safety change |
| Connector series from `094620d` | Added compromised-VPS controls without adding a platform wave; not platform-ownership drift |
| `be02c89` | Added fixed OpenCLI Reddit `read.post`, the closest current example of the frozen thin-wrapper target |

## Merge Gate And Exceptions

Until the audit debt above is resolved:

1. Do not add or expand platform retrieval logic for any source.
2. A new operation must record pinned upstream evidence and use direct reuse
   or an exact backend thin wrapper before its catalog state becomes
   `implemented`.
3. A native equivalent or reimplementation requires a separate ADR, explicit
   owner approval, semantic differences, threat-model evidence, rollback, and
   a review milestone. "No unified execute API" is not sufficient evidence.
4. An unbound interface remains `setup_required` or `unavailable`; it must not
   be described as a working production executor.
5. Changing the Agent-Reach pin reopens the complete 63-operation audit.
6. Connector production activation may proceed because it composes the
   existing Reddit wrapper and security controls. It may not add another
   Reddit operation or platform parser.

The CI reuse-boundary test makes every newly implemented catalog operation
declare one of these classes. Code review still owns the semantic decision;
the test prevents silent expansion, not dishonest classification.
