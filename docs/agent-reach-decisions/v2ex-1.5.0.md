# V2EX Agent-Reach 1.5.0 Owner-Fork Execution Decision

- Historical milestone status: approved and rebase-integrated; superseded
  without a dedicated recovery tag
- Date: 2026-07-31
- Operations: `v2ex:browse.hot`, `v2ex:browse.node_topics`,
  `v2ex:read.topic`, `v2ex:read.user`
- Classification: `direct_owner_fork_runtime`
- Backend: `v2ex-public-api` contract revision
  `legacy-json-2026-07-31`
- Official Agent-Reach base: `Panniantong/Agent-Reach` `1.5.0` at
  `b4d52c46c9113cb0f653d6df4cf71ebadf4930ac`
- Final owner-fork integration: `izumi0uu/Agent-Reach` at
  `9b69146588b1d162515b81db26b51643c15de8eb`
- Final integration tree: `e19835071ae6560431b66d5a21e51b598d3d9c81`
- Rollback integration pin: `2a5829cf3b50bc435c647bfae4c050b1837d0235`
  (`hermes-reach-integration-0.1.0a3`)

## Decision

Reactivate all four credential-free V2EX operations through closed
`execution.v1` descriptors in the Agent-Reach owner fork. The fork owns the
fixed public API origin and paths, bounded transport, complete JSON validation,
operation semantics, identity correlation, partial classification, and native
projection. Hermes owns the fixed isolated worker, public request validation,
timeout/cancellation, retry policy, independent typed-result validation,
normalization, receipts, and audit.

The former Hermes `v2ex-public-api-v1` endpoint/parser implementation remains
removed. This is a reviewed structured reactivation, not restoration of a
Hermes exception and not composition of the unsafe synchronous
`V2EXChannel` methods from official 1.5.0.

## Closed Descriptors

| Operation | Argument schema | Result schemas | Maximum items |
| --- | --- | --- | ---: |
| `browse.hot` | `v2ex.browse.hot.arguments.v1` | `v2ex.topic.v1` | 50 |
| `browse.node_topics` | `v2ex.browse.node_topics.arguments.v1` | `v2ex.topic.v1` | 50 |
| `read.topic` | `v2ex.read.topic.arguments.v1` | `v2ex.topic.v1`, then `v2ex.reply.v1` | 21 |
| `read.user` | `v2ex.read.user.arguments.v1` | `v2ex.profile.v1` | 1 |

Every descriptor requires only the fieldless `network_access.v1` host
capability. No request or capability accepts an origin, endpoint, query key,
header, proxy, backend, credential, Cookie, browser session, transport,
callable, or fallback. Static discovery and rejected requests perform no
network or backend I/O.

## Transport And Response Boundary

The private fork transport fixes `www.v2ex.com:443`, the four reviewed legacy
JSON paths, the permitted query keys, HTTPS SNI, headers, and HTTP/1.1 behavior.
It lazily gates `httpcore==1.0.9`, resolves every DNS answer, rejects any
non-global address, pins one validated address, disables proxies and redirects,
and streams at most 1 MiB under bounded connect/read and total deadlines.

Before operation projection, the fork rejects unsupported compression or media
type, oversized declared or streamed data, invalid UTF-8 or JSON,
NaN/Infinity, booleans used as integers, integers outside the JSON-safe range,
and excessive depth, nodes, strings, or scalars. Unknown fields and upstream
URLs may be ignored only after the complete document passes these global
bounds. The audited backend version describes the public API contract revision,
not the `httpcore` transport version.

Arguments and identity rules are closed:

- `browse.hot` accepts only `limit` 1 through 50.
- `browse.node_topics` accepts a bounded node identifier, page 1 through 100,
  and limit 1 through 50; every result must belong to the requested node.
- `read.topic` accepts a positive decimal topic ID; leading zeros correlate
  numerically and output uses the canonical decimal identity.
- `read.user` accepts a bounded username and correlates the result
  case-insensitively.

Topic, reply, and profile URLs are rebuilt from validated identities. Reply
IDs must be positive, JSON-safe, unique, and tied to the requested topic.
`read.topic` validates the complete reply list atomically. After a valid topic,
a reply transport/status/shape failure returns only that topic with one closed
`partial_error_code`; a partial result is not retried. A transient primary
failure retains the normal single bounded retry.

## Hermes Composition

Hermes starts only:

```text
<absolute sys.executable> -I -m hermes_reach.sources.v2ex_worker
```

The worker receives one bounded length-prefixed UTF-8 JSON frame in a private
temporary directory and minimal environment. It validates the exact
Agent-Reach PEP 610/RECORD/origin handshake with `runtime_module="v2ex"`, calls
the closed descriptor, validates the typed response, and emits one bounded
frame. The parent independently validates operation, order, item count,
identity, partial state, and every projected field before creating `RawItem`.
Hermes contains no V2EX endpoint or upstream JSON parser.

The isolated process is killable and cleans up on timeout or cancellation, but
it is not a kernel syscall sandbox. Exact reviewed package provenance remains
a supply-chain trust boundary.

## Historical Closure

The 2026-07-28 decision correctly disabled the official channel methods. They
use default `urllib.request.urlopen`, unbounded reads, ambient proxy/DNS/
redirect behavior, hard-coded page 1, lossy topic projection, missing reply
identity, swallowed reply failures, and insufficient user/topic correlation.
Those methods remain unapproved. The new fork descriptor does not wrap or
relax them; it supplies the missing bounded contract inside Agent-Reach.

## Review Milestone

Review this decision on any official base or owner-fork commit change;
execution protocol, descriptor, schema, capability, RECORD, transport policy,
fixed origin/path, V2EX public API shape, pagination, identity, partial/error,
worker framing, backend identity, or projection change. Any Agent-Reach pin
movement reopens the complete 63-operation audit.

## Rollout And Rollback

At this milestone, final integration
`9b69146588b1d162515b81db26b51643c15de8eb` was consumed by exact SHA. Its tree
`e19835071ae6560431b66d5a21e51b598d3d9c81` exactly matched reviewed PR head
`fd93d2ec86511a4a1514b7ebd13cd996be709692`. All pin-sensitive gates had to
pass before Hermes merge and a protected immutable recovery tag was still
required before release. This integration was later
superseded without receiving one. Current final integration
`75cd48c6274e7f4740530d97877ec048708d5334` is protected by
`hermes-reach-integration-0.1.0a4`; that tag does not retag or change the
historical recovery state of `9b69146588b1d162515b81db26b51643c15de8eb`.

Rollback restores exact pin `2a5829cf3b50bc435c647bfae4c050b1837d0235`,
recoverable through immutable tag `hermes-reach-integration-0.1.0a3`, and
returns all four V2EX operations to planned/unavailable. The former Hermes
adapter is not restored. No protocol, grant, Connector, database, receipt,
audit, or stored-data migration is required.
