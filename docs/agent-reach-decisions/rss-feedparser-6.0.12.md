# RSS Feedparser 6.0.12 Owner-Fork Runtime Decision

- Status: approved
- Date: 2026-07-29
- Operations: `rss:read.feed`, `rss:browse.entries`
- Classification: `direct_owner_fork_runtime`
- Backend: `feedparser` version `6.0.12`
- Execution protocol: `v1`
- Required host capability: `fetched_document.v1`
- Official Agent-Reach base: `1.5.0` at commit
  `b4d52c46c9113cb0f653d6df4cf71ebadf4930ac`
- Owner-fork integration commit:
  `806205fd106f4f4453624becfd773acce8418cf1`

## Decision

Move RSS parser invocation and source projection out of Hermes Reach and into
the exact owner Agent-Reach fork. Hermes calls the fork's closed
`execution.v1` contract for only `rss:read.feed` and `rss:browse.entries`.
Hermes supplies an already-fetched bounded document through
`fetched_document.v1`; the fork receives no general network, filesystem,
configuration, browser, or credential capability through that contract.

Both operations keep their existing Hermes v1 request/result envelopes and
remain default-local. Their public provenance remains backend `feedparser`,
version `6.0.12`; the internal execution owner changes from a Hermes exact
backend wrapper to the owner-fork runtime.

## Pinned evidence and handshake

Official Agent-Reach 1.5.0 identifies feedparser as the RSS backend but ships no
structured RSS execution callable. The reviewed owner fork adds a small,
additive execution module on top of official base
`b4d52c46c9113cb0f653d6df4cf71ebadf4930ac`.

Before plugin registration, Hermes validates all of the following without
executing a backend:

- installed Agent-Reach version is exactly `1.5.0`;
- PEP 610 provenance names `izumi0uu/Agent-Reach` and exact integration commit
  `806205fd106f4f4453624becfd773acce8418cf1`;
- RECORD SHA-256, size, and installed content match for the two parent package
  initializers Python executes and the four reviewed `execution.v1` files
  before any fork module is imported;
- execution protocol is exactly `v1`;
- discovery contains exactly the two reviewed RSS descriptors and closed
  argument/result schemas;
- both descriptors require only `fetched_document.v1`; and
- backend identity, feedparser version, and every hard limit match Hermes's
  frozen expectations.

Any mismatch fails closed before Reach tools, CLI, or skill registration. A
protected immutable integration tag is required before release as a
reachability and recovery reference, but Hermes is always pinned by commit and
never resolves the dependency by tag.

## Ownership and security composition

The owner fork owns:

- the `rss` operation dispatch for exactly `read.feed` and `browse.entries`;
- feedparser invocation over the supplied host document;
- feed and entry field projection, native entry order, and result schema;
- feedparser bozo handling and partial-result classification; and
- backend identity and version provenance.

Hermes Reach owns:

- URL validation plus proxy-free, DNS-pinned public HTTP fetching;
- response size, redirect, content-location, and declaration preflight;
- construction of the bounded `fetched_document.v1` capability;
- fixed isolated worker launch, deadline, cancellation, process-group kill,
  stdin/stdout framing, and output caps;
- independent revalidation of every response field, schema, count, string
  bound, URL, backend identity, and redacted error code;
- normalization into the unchanged Hermes result envelope; and
- runtime limits, provenance recording, receipts, and audit.

The production composition is:

```text
validated RSS call
  -> proxy-free DNS-pinned PublicHttpTransport with 1 MiB cap
  -> encoding/declaration safety preflight
  -> fixed isolated worker
  -> owner-fork execution.v1 with fetched_document.v1
  -> fork-owned feedparser invocation, projection, and partial classification
  -> independent Hermes response revalidation
  -> Hermes normalization, runner bounds, provenance, and audit
```

Raw feed bytes, headers, parser exceptions, stderr, and
`bozo_exception` never enter the public result. The request cannot select a
module, callable, backend, URL-fetch mechanism, filesystem path, or fallback.

The worker's empty environment, fixed root working directory, isolated mode,
closed file descriptors, and closed protocol reduce ambient input and contain
failure. They are not an operating-system syscall sandbox. The exact accepted
fork commit remains trusted code and could attempt filesystem or network
syscalls if its reviewed implementation were compromised. PEP 610 provenance,
commit review, release attestation, and rollback therefore remain part of the
security boundary.

## Semantic delta

There is no backend substitution: feedparser remains the exact backend selected
by Agent-Reach. The fork now owns the platform parsing semantics that Hermes
previously wrapped. Hermes deliberately narrows the fork's authority by
providing only a typed already-fetched document and hard limits.

The existing Hermes operations still add product-level behavior outside the
fork:

- `read.feed` exposes one normalized feed item;
- `browse.entries` applies the catalog limit after accepting only the closed
  fork projection;
- unsafe declarations, unsafe result URLs, incompatible output, and raw errors
  are rejected or redacted; and
- the shared runner remains the final item, character, and truncation authority.

This ownership split changes neither public request fields nor normalized
result fields. No Connector protocol, grant, database, receipt, or stored-data
migration is introduced.

## Review milestone

Review this decision on any official-base, fork-commit, execution-protocol,
feedparser-version, capability, schema, limit, worker-protocol, fetch-ownership,
backend-identity, or classification change. Either Agent-Reach pin change
reopens all 63 catalog operations.

## Rollback

Restore the previous Hermes Reach release and its exact Agent-Reach dependency
pin. The previous release and commit are the rollback unit; do not move or
retarget a consumed fork commit or recovery reference. RSS may be disabled if
the prior dependency is unavailable. No database, grant, wire, audit, or
stored-content migration is required.
