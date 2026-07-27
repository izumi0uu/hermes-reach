# Web Agent-Reach 1.5.0 Safety Exception

- Status: approved
- Date: 2026-07-27
- Operation: `web:read.url`
- Classification: `hermes_native_equivalent`
- Current backend: `web-public-http-v1` (version `1`)
- Current runtime: `WebAdapter(PublicHttpClient)`
- Upstream callable: `agent_reach.channels.web.WebChannel.read`
- Agent-Reach pin: `1.5.0` at commit
  `1494c2ab239e7355a77e7cceaf3271453a1f34b5`

## Decision

Retain `WebAdapter(PublicHttpClient)` as a safety exception. Do not call or
compose the pinned `WebChannel.read` method into production execution,
registration, or availability checks. Public request/result contracts, Web
availability, and the `web-public-http-v1` provenance identity remain
unchanged.

The project owner explicitly approved this exception in the 2026-07-27 task
request: retain the Hermes-native Web path for this pin rather than weaken the
existing runtime and security guarantees.

## Pinned Evidence

The installed `agent_reach/channels/web.py` has SHA-256
`7594d25d10a0c81b262816b992866466805cb835f55f8434bc801c1c16d1a993`,
matching the file at the pinned commit. In that file, `WebChannel.read`:

- is synchronous and constructs `https://r.jina.ai/{url}`;
- sends only a browser-like `User-Agent` and `Accept: text/plain`;
- calls `urllib.request.urlopen(req, timeout=30)`;
- calls `resp.read()` without a byte limit, then strictly decodes the complete
  response as UTF-8; and
- exposes no transport, opener, timeout, byte-limit, cache, redirect, proxy,
  resolver, cancellation, or output-format injection point.

For example, `https://example.com/a?q=secret` is sent to Jina as
`https://r.jina.ai/https://example.com/a?q=secret`. `WebChannel.check()` does
not probe Jina; it sets the active backend to `Jina Reader` and reports success.

## Semantic And Security Delta

| Boundary | Current Hermes backend | Direct pinned callable |
| --- | --- | --- |
| Cancellation and deadline | Cancellable stream under a 15-second attempt and 30-second total budget | A thread wrapper cannot stop the blocking call; timed-out work can outlive the response and overlap a retry |
| Raw size | 1 MiB streamed-byte cap, then a 250,000-character decode cap and 16,000-character result cap | Unbounded `resp.read()` materializes and decodes the whole response before shared truncation |
| Proxy, SSRF, and redirects | Proxy-free, every DNS answer validated, socket pinned to a public IP, at most three revalidated redirects, no HTTPS downgrade | Default `urllib` proxy and redirect behavior; no equivalent DNS pinning or per-hop policy |
| Disclosure and redaction | Query data goes only to the origin and is omitted from public URL metadata | The complete query-bearing URL goes to Jina and may be echoed by Jina's `URL Source` text |
| Retention | No Hermes content store; direct origin retrieval | Hosted Jina documents a 3600-second cache path; the callable sends no no-store control |
| Output and failures | Allowed HTML/text becomes visible text plus normalized title; empty content and failures map to closed results | Returns Jina Markdown, may broaden supported formats, propagates raw `urllib`/decode exceptions, and treats `""` as success |

These are changes in retrieval ownership and product behavior, not gaps that
post-return normalization can repair. Adding a Jina parser would also make
Hermes own an upstream response format, contrary to the reuse boundary.

## Consequences

The exception preserves the current closed URL policy, direct-origin privacy,
bounded memory/network work, cancellation, redaction, error mapping,
metadata-only audit, and I/O-free registry construction. It also knowingly
keeps this operation outside the preferred direct-upstream architecture.

This decision adds no Jina parser, disposable process, fallback, endpoint,
operation, database change, grant change, or response migration.

## Review Milestone

Review this exception at the next Agent-Reach version or commit change, or
before any change to the Web execution path, backend identity, or
classification, whichever occurs first. A pin change reopens the complete
63-operation audit.

A direct migration requires reviewed pinned evidence for bounded streaming
before allocation, termination of the actual execution unit within the
15-second attempt budget, proxy-independent transport, controlled and
revalidated redirects, a stable output/error contract, query-safe output, and
a hosted-service retention contract compatible with the product claim.

## Rollback

Revert this decision record and its reuse-matrix/test assertion. Because the
exception introduces no runtime, catalog, database, grant, availability, or
response change, rollback requires no data or operational migration.
