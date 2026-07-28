# Bilibili CLI 0.6.2 Exact Backend Decision

- Status: approved
- Date: 2026-07-28
- Operations: `bilibili:search.videos`, `bilibili:read.video`,
  `bilibili:browse.hot`, `bilibili:browse.rank`
- Classification: `exact_backend_thin_wrapper`
- Backend: `bili-cli` version `0.6.2`
- Agent-Reach pin: `1.5.0` at commit
  `1494c2ab239e7355a77e7cceaf3271453a1f34b5`

## Decision

Activate the four existing credential-free Bilibili contracts through the
exact `bili-cli` backend selected by Agent-Reach. Hermes invokes the pinned
Click entry point inside a fixed, killable worker and projects its stable JSON
v1 envelope into the existing `RawItem` schema. Hermes does not call
`bilibili-api-python` directly and owns no Bilibili endpoint, response parser,
pagination behavior, ranking behavior, or fallback.

The default registry now binds these four operations with provenance backend
`bili-cli`, version `0.6.2`. `read.subtitles` and `transcribe.video` remain
planned and unbound.

## Pinned Evidence

Agent-Reach 1.5.0 declares `bili-cli` as the Bilibili execution backend but
does not expose a structured operation-level execution API. The reviewed
PyPI wheel has SHA-256
`185b5df16262415c830a74216ca9c4a74df0e63cf542537444fb295a236a9f5d`
and declares console entry point `bili = bili_cli.cli:cli`. It has no
`bili_cli.__main__`, so the worker imports that exact Click entry point rather
than discovering an executable through PATH.

Version 0.6.2 emits `{ok,schema_version,data|error}` JSON envelopes. The four
selected public command paths use no credential: base video explicitly passes
`credential=None`, while search, hot, and rank do not call credential helpers.
Search defaults to page 1, hot defaults to page 1, and rank defaults to the
three-day ranking. Those defaults remain backend-owned semantics.

## Fixed Invocation

The only in-memory command shapes are:

```text
search --type video --max <limit> --json -- <query>
video <validated-url> --json
hot --max <limit> --json
rank --max <limit> --json
```

The search option terminator is required so a query beginning with `-` remains
positional input. Query and URL values cross the OS process boundary only in a
bounded framed stdin request; the worker process argv is always
`python -I -m hermes_reach.sources.bilibili_worker`. The worker calls
`cli.main(args=..., prog_name="bili", standalone_mode=False)` and captures
bounded stdout. A structured backend error writes JSON then exits 1; the worker
accepts that exact combination and rejects Click usage errors or exit drift.

## Security Composition

The `bilibili-cli` distribution also contains login, browser-cookie import,
account reads, audio download, dynamic posting/deletion, likes, coins, triples,
and unfollow operations. Those commands and every optional video flag are
unreachable from the closed operation-to-argv mapping. No shell, PATH lookup,
runtime install, OpenCLI route, yt-dlp route, SDK fallback, or request-selected
backend exists.

Each invocation starts with a private HOME, XDG config/cache/data directories,
and TMP directory. The child receives a minimal environment with empty proxy
variables and no ambient credential, Cookie, output-mode, or user-site state.
This is important because importing the upstream CLI also imports its auth
module, which computes `Path.home()/.bilibili-cli`, although the selected
commands never perform credential I/O.

Stdin, stdout, JSON depth, item counts, scalar sizes, and final normalized
results are bounded. Stderr is discarded. Timeout, cancellation, output
overflow, malformed framing, or invalid output kills and reaps the worker
process group before temporary state is removed. Backend messages, details,
query text, URLs, credentials, and exception strings never enter public errors,
provenance, receipts, audit, or logs.

The exact pin accepts the distribution's mandatory dependency closure,
including `browser-cookie3`; the optional audio extra and `av` are not
installed. The lock file freezes the complete closure. The process boundary
prevents caller-selected authority and ambient credential discovery; it is not
a claim that an actively malicious pinned dependency is an OS sandbox.

## Semantic Delta

There is no platform backend substitution. Hermes narrows authority and adds
product-level normalization, bounds, stable failures, provenance, receipts,
and audit. Canonical result URLs are derived only from validated BV IDs. The
public Hermes request and result envelopes do not change.

## Review Milestone

Review this decision on any Agent-Reach or `bilibili-cli` pin change, dependency
closure change, Click entry-point or command shape change, JSON schema change,
credential behavior change, worker protocol change, backend identity change,
or classification change. An Agent-Reach pin change reopens all 63 catalog
operations.

## Rollback

Remove the direct `bilibili-cli` pin and production worker/client, restore the
four default `setup_required` markers, and return the four review rows to
reviewed-but-unbound contracts. `read.subtitles` and `transcribe.video` are
unaffected. No database, grant, wire, or stored-content migration is required.
