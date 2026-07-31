# Web Agent-Reach 1.5.0 Disabled Path Decision

- Status: disabled
- Date: 2026-07-28
- Historical exception review: 2026-07-27
- Operation: `web:read.url`
- Classification: `not_implemented`
- Current backend: none
- Current runtime: none
- Upstream callable: `agent_reach.channels.web.WebChannel.read`
- Official Agent-Reach base: `Panniantong/Agent-Reach` `1.5.0` at
  `b4d52c46c9113cb0f653d6df4cf71ebadf4930ac`
- Original audit pin: `izumi0uu/Agent-Reach` at
  `f195253d53befdb012d7aa575e732ec627ec29ac`
- Current revalidation pin:
  `2a5829cf3b50bc435c647bfae4c050b1837d0235`

## Decision

Disable the former `WebAdapter(PublicHttpClient)` safety exception. Keep
`web:read.url` in the stable Hermes catalog as planned/unavailable, with no
production binding or local platform implementation. Do not compose the pinned
`WebChannel.read` method because it still fails the reviewed safety and
retention gates below.

The owner selected strict adapter purity on 2026-07-28. The earlier exception
approval is preserved only as historical evidence; it no longer grants
production authority to `web-public-http-v1`.

The 2026-07-31 full-pin audit against `2a5829cf3b50bc435c647bfae4c050b1837d0235`
found no Web execution descriptor or safety-contract change, so this disabled
decision remains in force.

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

| Boundary | Former Hermes backend | Direct pinned callable |
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

Web reading is no longer executable in this release. The operation remains
discoverable and fails closed instead of using either an unsafe direct
callable or a Hermes-owned replacement. This removes one platform exception
without changing the public request/result schema, database, grants, or stored
state.

The former implementation remains useful evidence for the controls an official
callable must satisfy, but it is not a fallback and must not be silently
recomposed.

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

Operational rollback keeps `web:read.url` planned and unavailable. Execution
may return only through a reviewed structured Agent-Reach execution capability
or exact Agent-Reach-selected backend with the required safety properties; the
current fork ledger contains no Web execution capability. The former Hermes
adapter cannot be restored as an exception. No data migration is required.
