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
- Accepted non-LinkedIn Agent-Reach candidate commit:
  `ee200e7160c4b093a2ba0fcee9f2a6842aefe20d`
- Accepted non-LinkedIn Agent-Reach candidate tree:
  `56883c0872bed94050660b16d1ade2e46f73fef9`
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
The accepted candidate removes both descriptors and every LinkedIn execution
module, capability export, runtime handshake, and packaged runtime file.

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
gaps: query-free warning and error diagnostics, exact artifact/log-level/
timeout identity binding, and terminal `section_errors` handling that cannot
duplicate a submission.

## Rollback

The accepted batch omits the two descriptors and bindings. No public protocol,
grant, database, receipt, audit, secret, or stored-content migration is
required because LinkedIn never becomes executable.
