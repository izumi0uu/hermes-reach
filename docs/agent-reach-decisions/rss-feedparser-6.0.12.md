# RSS Feedparser 6.0.12 Exact Backend Decision

- Status: approved
- Date: 2026-07-27
- Operations: `rss:read.feed`, `rss:browse.entries`
- Classification: `exact_backend_thin_wrapper`
- Backend: `feedparser` version `6.0.12`
- Agent-Reach pin: `1.5.0` at commit
  `1494c2ab239e7355a77e7cceaf3271453a1f34b5`

## Decision

Replace the Reach-owned ElementTree dialect parser with Agent-Reach's exact
selected RSS backend, feedparser. Hermes keeps the public-network boundary and
passes only already fetched, bounded bytes to a fixed, killable parser worker.
Hermes then validates a closed projection into the existing `RawItem` schema.

Both RSS operations remain available with unchanged request/result envelopes.
Their runtime provenance changes from the generic `rss-atom-parser-v1` identity
to backend `feedparser`, version `6.0.12`.

## Pinned Evidence

Pinned Agent-Reach `RSSChannel` is health-only: it declares one backend,
`feedparser`, and its `check()` imports that package. The pinned skill reference
at `agent_reach/skill/references/web.md` routes RSS through
`feedparser.parse(...)`; no Agent-Reach RSS callable, CLI, or MCP execution API
exists at version 1.5.0. An exact backend wrapper is therefore the highest
honest reuse class.

Agent-Reach declares only `feedparser>=6.0`. Hermes directly pins
`feedparser==6.0.12`, and `uv.lock` records the 6.0.12 sdist and wheel hashes.
Plugin registration rejects an installed parser version mismatch, registry
provenance records 6.0.12, and the offline release report exposes the installed
parser version.

## Security Composition

Calling `feedparser.parse(url)` or passing bare `bytes` is forbidden.
Feedparser accepts URL, file, stream, text, and bytes inputs; even its bare
bytes branch first attempts `open(bytes_value, "rb")` before treating the value
as feed content. The worker therefore passes an `io.BytesIO` stream so input
dispatch cannot select network or filesystem retrieval. The production
composition is:

```text
validated RSS call
  -> proxy-free DNS-pinned PublicHttpTransport with 1 MiB cap
  -> encoding/declaration safety preflight
  -> fixed isolated feedparser worker over BytesIO(bounded bytes)
  -> closed bounded JSON projection
  -> Hermes normalization, URL sanitation, runner bounds, provenance, audit
```

The worker command and module are fixed, use no shell, receive no credential or
proxy configuration, and expose no request-selected backend. Its stdin/stdout
protocol and selected fields are bounded. On timeout, cancellation, protocol
failure, or excess output, Hermes kills the process group and waits for cleanup.
Parser exceptions, stderr, raw bytes, headers, and `bozo_exception` never cross
the boundary. The package entry point imports plugin registration lazily so the
`-m` target is not preloaded or executed twice during worker startup.

Hermes intentionally retains its stricter decoded `DOCTYPE`/`ENTITY` denial
and XML declaration/BOM consistency checks before trusting parser output.
Feedparser owns feed dialect, namespace, encoding recovery after that preflight,
native entry ordering, and field extraction. Hermes owns visible-text
normalization and safe public result URLs.

## Semantic Delta

There is no backend substitution: feedparser is the exact Agent-Reach-selected
mechanism. Hermes narrows its authority and input forms. The existing v1
operations add product-level normalization that Agent-Reach does not define:

- `read.feed` projects feed metadata into one content item;
- `browse.entries` applies the catalog limit and projects selected entry fields;
- unsafe declarations, unsafe result URLs, unusable parser output, and raw
  parser errors are rejected or redacted; and
- the shared runner remains the final item/character/truncation authority.

Feedparser recognizes more valid RSS/Atom dialects than the removed parser and
can recover usable data from some malformed documents. Only a closed usable
projection is accepted; an unusable result remains a permanent failure.

## Review Milestone

Review this decision on any Agent-Reach or feedparser version change, or before
changing the RSS worker protocol, parser invocation, fetch ownership, backend
identity, or classification. An Agent-Reach pin change reopens all 63 catalog
operations.

## Rollback

Remove the feedparser binding, direct pin, and worker, then keep both RSS rows
planned/unavailable until an official callable or another exact
Agent-Reach-selected backend passes review. Do not restore
`rss-atom-parser-v1`, the ElementTree implementation, or any other local
platform parser. No database, grant, wire, or stored-content migration is
required.
