# V2EX Agent-Reach 1.5.0 Disabled Path Decision

- Status: disabled
- Date: 2026-07-28
- Historical exception review: 2026-07-27
- Operations: `v2ex:browse.hot`, `v2ex:browse.node_topics`,
  `v2ex:read.topic`, `v2ex:read.user`
- Classification: `not_implemented`
- Current backend: none
- Current runtime: none
- Official Agent-Reach base: `Panniantong/Agent-Reach` `1.5.0` at
  `b4d52c46c9113cb0f653d6df4cf71ebadf4930ac`
- Original audit pin: `izumi0uu/Agent-Reach` at
  `f195253d53befdb012d7aa575e732ec627ec29ac`
- Current revalidation pin:
  `2a5829cf3b50bc435c647bfae4c050b1837d0235`

## Decision

Disable the former safe V2EX adapter for all four operations. Keep their names
in the stable Hermes catalog as planned/unavailable, with no production
binding or local platform implementation. Do not compose the pinned
`V2EXChannel` methods because they still fail the reviewed safety and contract
gates below.

The owner selected strict adapter purity on 2026-07-28. The earlier exception
approval is preserved only as historical evidence; it no longer grants
production authority to `v2ex-public-api-v1`.

The 2026-07-31 full-pin audit against `2a5829cf3b50bc435c647bfae4c050b1837d0235`
found no V2EX execution descriptor or safety-contract change, so this disabled
decision remains in force.

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

| Boundary | Former Hermes backend | Pinned Agent-Reach methods |
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
  while the Hermes v1 contract accepts pages 1 through 100 and the former
  adapter honored that range.
- `read.topic` swallows every reply-fetch failure as an empty successful reply
  list, discards reply IDs and canonical reply anchors, and turns an absent
  topic into a blank success-shaped mapping.
- `read.user` does not correlate the returned username with the requested
  identity and passes through the returned profile URL.

Post-return normalization cannot recover discarded authors, reply IDs,
pagination, or partial-failure evidence. Silently accepting those changes
would violate the current public v1 operation contract and audit claims.

## Consequences

V2EX retrieval is no longer executable in this release. The four operations
remain discoverable and fail closed instead of using either unsafe direct
callables or a Hermes-owned reimplementation. This removes all four platform
reimplementation exceptions without changing public request/result schemas,
database state, grants, or stored data.

The former implementation remains useful evidence for the controls and
operation semantics an official callable must satisfy, but it is not a
fallback and must not be silently recomposed.

## Review Milestone

Review this exception at the next Agent-Reach version or commit change, or
before any V2EX execution, backend identity, catalog, or classification change.
A pin change reopens the complete 63-operation audit.

A direct migration requires a pinned callable with cancellable bounded
transport or official transport injection, public DNS/proxy/redirect controls,
a node page argument, typed not-found and partial outcomes, stable reply IDs,
and a response contract that retains the fields already promised by Hermes.

## Rollback

Operational rollback keeps all four V2EX operations planned and unavailable.
Execution may return only through a reviewed structured Agent-Reach execution
capability or exact Agent-Reach-selected backend with the required safety
properties; the current fork ledger contains no V2EX execution capability.
The former Hermes adapter cannot be restored as an exception. No data
migration is required.
