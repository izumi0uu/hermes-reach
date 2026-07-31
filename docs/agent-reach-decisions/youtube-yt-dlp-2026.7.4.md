# YouTube yt-dlp 2026.7.4 Owner-Fork Execution Decision

- Status: approved and rebase-integrated; recovery tag pending
- Date: 2026-07-31
- Direct owner-fork operations: `youtube:search.videos`, `youtube:read.video`,
  `youtube:read.subtitles`
- Backend: `yt-dlp` version `2026.7.4`
- Execution closure: `yt-dlp-ejs==0.8.0`, `deno==2.8.3`
- Official Agent-Reach base: `Panniantong/Agent-Reach` `1.5.0` at
  `b4d52c46c9113cb0f653d6df4cf71ebadf4930ac`
- Final owner-fork integration: `izumi0uu/Agent-Reach` at
  `9b69146588b1d162515b81db26b51643c15de8eb`
- Final integration tree: `e19835071ae6560431b66d5a21e51b598d3d9c81`
- Rollback integration pin: `2a5829cf3b50bc435c647bfae4c050b1837d0235`
  (`hermes-reach-integration-0.1.0a3`)
- Previous read-video candidate: `9e744d0c33f9e6498cf66c2ea376a653000e9be4`;
  final integration `2a5829cf3b50bc435c647bfae4c050b1837d0235`;
  shared tree `070e4507fde7e55eceaba4d29e6a459c4a972f60`

## Decision

Move all three executable YouTube operations into the closed Agent-Reach
owner-fork `execution.v1` runtime. Search and subtitles keep the same exact
`yt-dlp`/EJS/Deno backend closure and public behavior while their fixed
invocation, raw validation, and YouTube-native projection move out of Hermes.
This completes the source migration without expanding the public operation set
or moving Hermes policy and containment into the fork.

The default registry still binds the same three credential-free operations
with provenance `yt-dlp` version `2026.7.4`. `read.comments` remains
catalog-implemented but `setup_required`: yt-dlp has a maximum comment prefix
but no stable page selector compatible with Hermes v1 `(limit,page)`.
`transcribe.video` remains planned and unbound.

Hermes does not add a YouTube API, direct subtitle HTTP path, browser/OpenCLI
route, alternate search provider, download operation, or transcription
fallback. The migration is one source-operation contract, not a generic
backend selector or a claim that all Agent-Reach YouTube behavior is exposed.

## Pinned Evidence

Agent-Reach 1.5.0 selects yt-dlp for YouTube metadata, search, and subtitles.
The reviewed owner-fork batch adds exactly three YouTube descriptors:

```text
source: youtube
operations: search.videos, read.video, read.subtitles
argument schemas: youtube.search.videos.arguments.v1,
                  youtube.read.video.arguments.v1,
                  youtube.read.subtitles.arguments.v1
result schemas: youtube.video.v1, youtube.subtitle.v1
backend: yt-dlp@2026.7.4
host capabilities: network_access.v1;
                   network_access.v1 + private_workspace.v1 for subtitles
items: search 0..50; video exactly 1; subtitles exactly 1
```

Together with two RSS, four Bilibili, four V2EX, and one Exa Web descriptor,
static discovery contains exactly 14 ordered operations. Discovery and
rejected requests do not import or probe yt-dlp, yt-dlp-ejs, Deno, the network,
or the filesystem.

Hermes pins the exact integration commit in package metadata and `uv.lock`. The
bridge validates exact PEP 610 provenance and RECORD hash, size, resolved path,
and current bytes for both parent initializers plus all nine reviewed
`execution/v1` files, including `youtube.py`, before importing fork execution
code. It then validates loaded origins, exports, signatures, type shapes, error
codes, and all 14 descriptors. The YouTube worker repeats the durable
handshake with `runtime_module="youtube"` before every fork-backed read.

The backend closure remains directly pinned and locked:

```text
yt-dlp      2026.7.4
yt-dlp-ejs  0.8.0
deno        2.8.3
```

The reviewed yt-dlp wheel SHA-256 is
`f11f2b11d5a8ac4059f9bdf29fa4407dc7c6bb00c5097e95ca22a7a9db518266`.
The reviewed yt-dlp-ejs wheel SHA-256 is
`79300e5fca7f937a1eeede11f0456862c1b41107ce1d726871e0207424f4bdb4`.
The lock contains Deno wheels for macOS x86_64/arm64, glibc Linux
x86_64/arm64, and Windows x86_64. This release does not claim musl, Windows
ARM64, or full Windows process-group parity.

## Fixed Invocation

The parent process argv remains:

```text
<absolute sys.executable> -I -m hermes_reach.sources.youtube_worker
```

Query, canonical watch URL, limit, and language cross the process boundary only
in one length-prefixed UTF-8 JSON v1 frame. Operation ownership inside the
worker is exact:

```text
search.videos  -> Agent-Reach execution.v1
read.video     -> Agent-Reach execution.v1
read.subtitles -> Agent-Reach execution.v1
```

Hermes validates the public request and creates only `ExecutionRequestV1`, a
fieldless `NetworkAccessV1`, narrowed execution limits, and, for subtitles, a
fieldless `PrivateWorkspaceV1`. The owner fork alone checks the exact
yt-dlp/EJS/Deno closure, constructs fixed `YoutubeDL` options, and invokes the
operation-specific calls:

```text
search.videos:
  extract_info("ytsearch<limit>:<query>", download=False,
               ie_key="YoutubeSearch")

read.video:
  extract_info(<canonical-watch-url>, download=False, ie_key="Youtube")

read.subtitles:
  writesubtitles=True, writeautomaticsub=True, subtitlesformat="vtt",
  skip_download=True, subtitleslangs=[requested] or ["zh-Hans", "zh", "en"]
  extract_info(<canonical-watch-url>, download=True, ie_key="Youtube")
```

The public request cannot select argv, executable, backend, extractor,
endpoint, proxy, Cookie, credential, browser, output path, plugin, remote
component, download behavior, or fallback.

## Security Composition

Every request receives private HOME, XDG config/cache/data, DENO_DIR, and TMP
directories. The child environment has no PATH, disables proxy variables, sets
`YTDLP_NO_PLUGINS=1`, and contains no provider credentials, Cookie, browser,
npm, or host configuration. The fixed worker, hard timeout/cancellation,
process-group kill/reap, bounded stdin/stdout, discarded stderr, and cleanup
remain Hermes responsibilities for all three operations.

The fork enforces no config/cache/proxy/Cookie/netrc/browser, no plugins or
remote components, no shell or CLI, and no alternate JavaScript runtime.
Packaged EJS plus the regular, executable, single-link Deno installed beside
the running Python interpreter are the only JavaScript route. Subtitle files
must remain below the worker's private current directory and pass regular-file,
symlink, hardlink, inode, size, and cleanup checks before parsing.

The fork returns zero to 50 `youtube.video.v1` search items, exactly one
`youtube.video.v1` read item, or exactly one `youtube.subtitle.v1` item. It owns
raw yt-dlp validation, backend error interpretation, identity correlation,
language and manual-before-automatic selection, VTT normalization,
date/integer normalization, source-field byte bounds, and YouTube-native
projection. Hermes independently validates the complete typed result before
stdout and again in the parent before constructing `RawItem` and
`MediaMetadata`; the parent also correlates read-video and subtitle results
with the original requested URL.

Source-field UTF-8 truncation remains the existing projection behavior.
Execution-context text truncation is bounded to 16,000 Unicode code points and
sets `truncated=true`. Malformed data, unsafe scalars, unknown fields, identity
drift, non-projectable overflow, or envelope overflow fail closed rather than
being rescued by arbitrary truncation.

Raw yt-dlp dictionaries, formats, signed media/subtitle URLs, HTTP headers,
warnings, exception strings, query, target URL, and temporary paths never
cross stdout or enter provenance, receipts, audit, or stable errors.

## Error Compatibility

The worker freezes an operation-specific translation:

```text
not_found/authentication/authorization/rate_limit/transient/permanent
  -> matching existing public failure class
backend_unavailable/backend_incompatible
  -> setup_required -> public permanent
invalid/unsupported/capability/contract failures
  -> public permanent
deadline_exceeded/cancelled returned by the fork
  -> unreachable contract failure -> public permanent
```

Real caller cancellation still propagates `CancelledError` in the parent and
kills/reaps the process group. The migration does not inherit Bilibili's
transient setup/contract mapping and does not create an additional retry.

## Semantic Delta

Hermes owns public validation and canonicalization, grants, fixed worker
containment, deadlines and cancellation, retry policy, independent frame/result
validation, product normalization, provenance, receipts, and audit.

The owner fork owns all three executable YouTube platform paths: exact
dependency and Deno validation, fixed yt-dlp options and calls, search limit
mapping, subtitle language preference, manual-before-automatic caption
selection, private-workspace file safety, raw result validation, error mapping,
identity/date/integer normalization, and native projection. Hermes retains no
yt-dlp call or YouTube response parser. This split does not authorize new
YouTube platform logic in Hermes or a broader Agent-Reach execution surface.

## Review Milestone

Review this decision on any official Agent-Reach base or owner-fork commit
change; execution protocol, descriptor, schema, capability, signature, or
RECORD change; yt-dlp, yt-dlp-ejs, or Deno pin/closure change; fixed options or
API call change; EJS/runtime behavior change; worker/frame protocol change;
backend identity, projection, error translation, or classification change. Any
Agent-Reach pin movement reopens all 63 catalog operations.

## Rollout And Rollback

`9b69146588b1d162515b81db26b51643c15de8eb` is the final rebase-integrated
commit, with tree `e19835071ae6560431b66d5a21e51b598d3d9c81`. That tree exactly
matches reviewed PR head `fd93d2ec86511a4a1514b7ebd13cd996be709692`.
Hermes pins the final SHA and every pin-sensitive gate must pass before merge.
A new protected immutable recovery tag remains a separate release prerequisite.

Rollback restores exact pin `2a5829cf3b50bc435c647bfae4c050b1837d0235`,
recoverable through immutable tag `hermes-reach-integration-0.1.0a3`, and
restores search and subtitles to their former exact-backend wrappers.
`read.video`, comments, and transcription retain their previous states. No
database, grant, wire, Connector, receipt, audit, or stored-content migration
is required.

The active integration-tag ruleset prevents update and deletion of
`hermes-reach-integration-0.1.0a3` with no bypass actor. Runtime authority
remains the exact commit selected by Hermes package metadata; the tag is not a
dependency selector.
