# LinkedIn Search Stop-Condition Decision

## Decision

`linkedin:search.people` and `linkedin:search.jobs` remain catalog-visible but
are `planned`, `not_implemented`, unbound, and unavailable. The reviewed
`linkedin-scraper-mcp==4.14.0` route does not satisfy the Hermes security and
single-submission boundary, so no LinkedIn runtime, Connector capability,
activation flag, or fallback is part of the accepted batch.

The reviewed methods and result shape remain evidence for a future review.
They are not execution authority.

## Provenance

- Agent-Reach official base:
  `b4d52c46c9113cb0f653d6df4cf71ebadf4930ac`
- Rejected pre-freeze Agent-Reach candidate commit:
  `7bc42839d3dd290e4af93b24e0b03b738cff0ffa`
- Rejected pre-freeze Agent-Reach candidate tree:
  `382557e0bec76819f0633f31895580a0f549b6bd`
- Final reviewed non-LinkedIn Agent-Reach PR head:
  `e91e3efa045e75f08d4e7fdd9749fe26d4f774c5`
- Reviewed and final non-LinkedIn Agent-Reach integration tree:
  `e86ee839621360b991d985ad9d4cb18e36f86351`
- Final rebase-merged non-LinkedIn Agent-Reach integration commit:
  `75cd48c6274e7f4740530d97877ec048708d5334`
- LinkedIn source release: `stickerdaniel/linkedin-mcp-server` `v4.14.0`
- LinkedIn source commit:
  `7edbd32231afa6d40fabad207329591ad5a4feb0`
- compatibility wheel SHA-256:
  `2173ead9777f6202fd581b4ec227d7a7212e9798f26f530b3174ff4683797558`
- executable backend wheel SHA-256:
  `62a889ac417e5e04d1635d5698df7178edc667a232dca42f417647e2ea25926d`
- runtime lock SHA-256:
  `9150a44d903ecfecdc48d115b87385bb78f3c69f4067951cf238e7fda6f09a17`
- selected read-tool schema SHA-256:
  `2549d379d2306ba22c24f06015db67f448d109943fb96f2d656986d2d92f0699`

The rejected candidate contains 35 descriptors, including the two LinkedIn
descriptors. It is not the accepted 33-descriptor boundary and must not be
merged, tagged, pinned for release, or described as an activatable candidate.
The final integration excludes both descriptors and every LinkedIn execution
module, capability export, runtime handshake, and packaged runtime file.
Owner-fork PR #6's final reviewed head and its rebase integration on
`hermes/execution-v1` have the identical tree recorded above.

## Stop Condition

The route was rejected for four independent reasons:

1. `linkedin-scraper-mcp==4.14.0` logs query-bearing URLs at `WARNING`.
2. Error paths persist query-bearing diagnostics.
3. Hermes cannot bind the reviewed wheel hashes, effective log threshold, and
   12-second timeout to the identity of an already-listening service.
4. Native `section_errors` can combine with retry behavior and submit the same
   search more than once.

Loopback binding, fixed read method names, and a reviewed `tools/list` schema
do not close these gaps. Jina Reader, a generic MCP route, write tools, and
request-selected methods remain forbidden fallbacks.

## Ownership And Failure Boundary

The owner fork may retain non-executable review fixtures or method/schema
evidence, but Hermes must expose no LinkedIn runtime selection, capability,
binding, CLI activation path, or release handshake. Catalog validation and
status remain I/O-free and report the two operations unavailable with no
backend identity or attempt.

Review may reopen only when one backend or service contract closes all four
gaps:

1. `WARNING` logs contain no query-bearing URLs.
2. Error paths persist no query-bearing diagnostics.
3. One service identity binds the reviewed wheel hashes, effective log
   threshold, and 12-second timeout.
4. Native `section_errors` and retry handling cannot duplicate a submission.

## Rollback

The accepted batch omits the two descriptors and bindings. No public protocol,
grant, database, receipt, audit, secret, or stored-content migration is
required because LinkedIn never becomes executable.
