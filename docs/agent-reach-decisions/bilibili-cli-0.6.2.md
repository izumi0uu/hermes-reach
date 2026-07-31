# Bilibili CLI 0.6.2 Owner-Fork Runtime Decision

- Status: approved
- Date: 2026-07-30
- Operations: `bilibili:search.videos`, `bilibili:read.video`,
  `bilibili:browse.hot`, `bilibili:browse.rank`
- Classification: `direct_owner_fork_runtime`
- Execution contract: `agent_reach.execution.v1`
- Backend: `bili-cli` version `0.6.2`
- Official Agent-Reach base: `Panniantong/Agent-Reach` `1.5.0` at
  `b4d52c46c9113cb0f653d6df4cf71ebadf4930ac`
- Original owner-fork integration pin: `izumi0uu/Agent-Reach` at
  `f195253d53befdb012d7aa575e732ec627ec29ac`
- Original recovery reference: `hermes-reach-integration-0.1.0a2`
- Current public-platform final integration:
  `9b69146588b1d162515b81db26b51643c15de8eb`

## Decision

Move the four credential-free Bilibili operations atomically from
Hermes-owned exact-backend wrappers to the owner fork's closed execution v1
runtime. Agent-Reach now owns the operation-to-Click argv mapping, lazy
`bilibili-cli==0.6.2` version and entry-point gate, backend invocation, raw JSON
envelope validation, backend error mapping, and Bilibili-native projection.

Hermes keeps the fixed isolated worker process, private environment, framing,
hard timeout and cancellation, process-group kill/reap, one bounded retry,
policy, normalization, provenance, receipts, and audit. The public Reach tools,
catalog, grants, database, result envelope, and backend provenance do not
change. `read.subtitles` and `transcribe.video` remain planned and unbound.

## Closed Execution Contract

The fork statically registers these exact descriptors, all with protocol `v1`,
backend `bili-cli@0.6.2`, result schema `bilibili.video.v1`, and required host
capability `network_access.v1`:

| Operation | Argument schema | Maximum items |
| --- | --- | ---: |
| `search.videos` | `bilibili.search.videos.arguments.v1`: trimmed query 1..4096 characters and limit 1..50 | 50 |
| `read.video` | `bilibili.read.video.arguments.v1`: canonical `https://www.bilibili.com/video/BV...` URL | 1 |
| `browse.hot` | `bilibili.browse.hot.arguments.v1`: limit 1..50 | 50 |
| `browse.rank` | `bilibili.browse.rank.arguments.v1`: limit 1..50 | 50 |

Each result item contains exactly `text`, `native_id`, `title`, `url`,
`author`, `duration_seconds`, and `view_count`. Text and identifiers are
bounded; the two numeric fields are non-boolean integers from zero through
`2^53-1`. The descriptor caps fork result payloads at 512 KiB and Bilibili
authors at 1024 characters.

`NetworkAccessV1` is a frozen fieldless marker. It records the host's approval
to invoke this already-registered network backend; it cannot carry an endpoint,
proxy, header, Cookie, credential, path, command, backend selector, or fallback.
It is not an operating-system syscall sandbox. Hermes creates it only inside
the existing isolated Bilibili worker after validating the request and exact
fork runtime.

## Fork-Owned Invocation

Only the fork owns these in-memory command shapes:

```text
search --type video --max <limit> --json -- <query>
video <validated-url> --json
hot --max <limit> --json
rank --max <limit> --json
```

The search option terminator keeps a leading-hyphen query positional. The fork
loads only the reviewed `bili = bili_cli.cli:cli` console entry point and calls
its Click `main` with `standalone_mode=False`. It accepts only the closed,
unique-key, finite `{ok,schema_version,data|error}` envelope, maps a fixed error
taxonomy without messages or details, and returns typed execution v1 objects.
Hermes does not import `bili_cli`, construct Click argv, parse its raw envelope,
or project platform-native response fields.

The fork's backend lock is intentionally nonblocking. An overlapping invocation
returns `transient`; Hermes may spend its existing single bounded retry within
the caller deadline. Waiting inside the fork would bypass Hermes timeout and
cancellation budgeting.

Worker-time `backend_unavailable`, `backend_incompatible`, and fork-returned
`backend_contract_violation` failures also map to `transient` at the Hermes
boundary. Unexpected worker exceptions retain the same classification. This
preserves the pre-migration single bounded retry for dependency metadata,
import, version, entry-point, backend-envelope, and projection drift; the
registration and release handshakes still reject known dependency drift before
a tool is exposed. Hermes-detected malformed framing, bounds, identity, or
scalar values remain `permanent`.

## Security Composition

The worker process argv remains exactly
`python -I -m hermes_reach.sources.bilibili_worker`; query and URL data cross
only the bounded binary stdin frame. Each invocation receives private HOME,
XDG config/cache/data, and TMP directories, an explicit minimal environment,
empty proxy variables, no PATH lookup, no user site, discarded stderr, closed
file descriptors, and a new process session. This containment matters because
lazy import of the CLI auth module evaluates a home-directory path even though
the four selected commands use no credential.

The `bilibili-cli` distribution also contains login, browser-cookie import,
account reads, downloads, posting/deletion, likes, coins, triples, and unfollow
operations. None is registered by execution v1. Malformed framing, integrity
drift, output overflow, timeout, cancellation, or invalid fork output kills and
reaps the process group before private temporary state is removed. Backend
messages, details, requests, credentials, and exception strings do not enter
public errors, receipts, audit, or logs.

The exact pin accepts the distribution's mandatory dependency closure,
including `browser-cookie3`; the optional audio extra and `av` are not
installed. The process boundary and closed marker constrain caller-selected
authority. They do not make an actively malicious pinned dependency safe.

## Integrity and Release Evidence

Before importing fork execution code, Hermes verifies exact PEP 610 owner-fork
URL and commit provenance, complete import-chain RECORD algorithm/digest/size
and disk bytes, module origins, exports, dataclass fields, union members, error
codes, function signatures, the ordered 14 capability descriptors, and the
unchanged 15-channel catalog. Immediately before execution it also checks the
origin and signature of `execute_bilibili(request, context)`.

The exact commit pin is the dependency authority. The protected immutable tag
`hermes-reach-integration-0.1.0a2` is now the rollback reachability reference
for the original Bilibili integration at
`f195253d53befdb012d7aa575e732ec627ec29ac`. Protected immutable tag
`hermes-reach-integration-0.1.0a3` preserves reachability for the previous
reviewed integration `2a5829cf3b50bc435c647bfae4c050b1837d0235`. The current
final integration has no recovery tag yet and remains release-ineligible until
one is protected. Hermes never resolves the dependency by tag or branch.

## Review Milestone

Reopen this decision and the complete 63-operation audit for any official base
or owner-fork commit change, execution protocol or descriptor change,
`bilibili-cli` pin or dependency closure change, Click entry point/command or
JSON schema change, credential behavior change, worker protocol change,
backend identity change, projection change, or classification change.

## Rollback

The current public-platform batch rollback restores the previous Hermes release
and exact owner-fork pin `2a5829cf3b50bc435c647bfae4c050b1837d0235`, whose
immutable recovery reference is `hermes-reach-integration-0.1.0a3`. The original
Bilibili integration at `f195253d53befdb012d7aa575e732ec627ec29ac` and tag
`hermes-reach-integration-0.1.0a2`, plus the earlier Hermes-owned wrapper at
`806205fd106f4f4453624becfd773acce8418cf1` and tag
`hermes-reach-integration-0.1.0a1`, remain rollback history only. Do not split
execution ownership between versions. No public protocol, grant, Connector,
database, receipt, audit, or stored-content migration is required. Neither
consumed commit nor recovery tag may be moved or deleted.
