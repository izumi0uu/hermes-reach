# V2EX Agent-Reach 1.5.0 Safety And Contract Exception

- Status: approved
- Date: 2026-07-27
- Operations: `v2ex:browse.hot`, `v2ex:browse.node_topics`,
  `v2ex:read.topic`, `v2ex:read.user`
- Classification: `reach_reimplementation`
- Current backend: `v2ex-public-api-v1` (version `1`)
- Current runtime: `V2exAdapter(PublicHttpClient)`
- Agent-Reach pin: `1.5.0` at commit
  `1494c2ab239e7355a77e7cceaf3271453a1f34b5`

## Decision

Retain the current safe V2EX adapter for all four implemented operations. Do
not compose the pinned `V2EXChannel` methods into production execution,
registration, or availability checks.

The project owner approved preserving the established security boundary while
maximizing honest Agent-Reach reuse. For this pin, direct V2EX reuse would
weaken cancellation and network bounds and would silently narrow or corrupt
published operation behavior. The exception is therefore preferable to a
misleading direct-runtime classification.

## Pinned Evidence

The installed `agent_reach/channels/v2ex.py` has SHA-256
`a3e53c462096118d0ff133a78c6a897b87f0e7aed98b52909a675ee94170eac5`,
matching the pinned commit. It exposes these synchronous methods:

| Hermes operation | Pinned callable |
| --- | --- |
| `browse.hot` | `V2EXChannel.get_hot_topics(limit=20)` |
| `browse.node_topics` | `V2EXChannel.get_node_topics(node_name, limit=20)` |
| `read.topic` | `V2EXChannel.get_topic(topic_id)` |
| `read.user` | `V2EXChannel.get_user(username)` |

Every method calls the module-level `_get_json`. That helper uses default
`urllib.request.urlopen(..., timeout=10)`, reads the entire response without a
byte cap, strictly decodes UTF-8, and applies `json.loads`. It exposes no
transport, opener, resolver, proxy, redirect, stream, raw-size, cancellation,
or total-deadline injection point. `V2EXChannel.check()` also performs a live
network request and is excluded from registration and status.

## Safety Delta

| Boundary | Current Hermes backend | Pinned Agent-Reach methods |
| --- | --- | --- |
| Cancellation | Async transport closes work on cancellation | A thread wrapper cannot terminate blocking `urllib`; topic can perform two sequential calls |
| Raw size | Rejects declared or streamed responses above 1 MiB before full allocation | Unbounded `resp.read()`, decode, and JSON allocation happen before item slicing |
| Network | Proxy-free, validates every DNS answer, pins a public IP, revalidates redirects, rejects HTTPS downgrade | Default proxy, DNS, and redirect behavior |
| Response policy | Checks JSON media type, decoded size, strict finite JSON, identity, and stable shapes | Permissive mapping access and raw Python exceptions |
| Retry ownership | Timed-out work is closed before retry | A timed-out thread can overlap a retry and outlive result delivery |

A disposable process could make the synchronous unit killable, but it would
still need a new egress boundary and bounded IPC. It would not repair the
operation-level contract differences below. Building that subsystem at this
pin would add control-plane code without removing the decisive semantic gaps.

## Operation Delta

- `browse.hot` truncates topic content to 200 characters and discards author
  identity before Hermes can normalize it.
- `browse.node_topics` has the same projection loss and hard-codes `page=1`,
  while Hermes accepts and currently honors pages 1 through 100.
- `read.topic` swallows every reply-fetch failure as an empty successful reply
  list, discards reply IDs and canonical reply anchors, and turns an absent
  topic into a blank success-shaped mapping.
- `read.user` does not correlate the returned username with the requested
  identity and passes through the returned profile URL.

Post-return normalization cannot recover discarded authors, reply IDs,
pagination, or partial-failure evidence. Silently accepting those changes
would violate the current public v1 operation contract and audit claims.

## Consequences

The exception preserves bounded, proxy-free and DNS-pinned public retrieval,
strict JSON/identity validation, node pagination, observable partial reply
failure, canonical result URLs, cancellation, redaction, and current
provenance. It knowingly leaves four operations in the least preferred
implemented reuse class.

This decision adds no endpoint, parser, process, operation, fallback,
credential, database, grant, or response migration.

## Review Milestone

Review this exception at the next Agent-Reach version or commit change, or
before any V2EX execution, backend identity, catalog, or classification change.
A pin change reopens the complete 63-operation audit.

A direct migration requires a pinned callable with cancellable bounded
transport or official transport injection, public DNS/proxy/redirect controls,
a node page argument, typed not-found and partial outcomes, stable reply IDs,
and a response contract that retains the fields already promised by Hermes.

## Rollback

There is no runtime change to roll back. Reverting this record and its
manifest/test rows is valid only together with an approved direct migration or
with making all four V2EX catalog operations planned and unavailable.
