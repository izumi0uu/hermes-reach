# YouTube yt-dlp 2026.7.4 Exact Backend Decision

- Status: approved
- Date: 2026-07-28
- Operations: `youtube:search.videos`, `youtube:read.video`,
  `youtube:read.subtitles`
- Classification: `exact_backend_thin_wrapper`
- Backend: `yt-dlp` version `2026.7.4`
- Execution closure: `yt-dlp-ejs==0.8.0`, `deno==2.8.3`
- Official Agent-Reach base: `Panniantong/Agent-Reach` `1.5.0` at
  `b4d52c46c9113cb0f653d6df4cf71ebadf4930ac`
- Owner-fork integration pin: `izumi0uu/Agent-Reach` at
  `806205fd106f4f4453624becfd773acce8418cf1`

## Decision

Activate three existing credential-free YouTube contracts through the exact
`yt-dlp` backend selected by Agent-Reach. Hermes invokes the structured
`yt_dlp.YoutubeDL` API inside a fixed, short-lived worker, then projects only a
closed metadata or VTT result. Hermes does not add a YouTube API, direct
subtitle HTTP path, browser/OpenCLI route, alternate search provider, or
transcription fallback.

The default registry binds the three operations with provenance backend
`yt-dlp`, version `2026.7.4`. `read.comments` remains catalog-implemented but
`setup_required`: yt-dlp has a maximum comment prefix but no stable page
selector compatible with Hermes v1 `(limit,page)`. `transcribe.video` remains
planned and unbound.

## Pinned Evidence

Agent-Reach 1.5.0 selects yt-dlp for YouTube metadata, search, and subtitles.
The owner fork adds no YouTube execution v1 descriptor, so these three
operations remain exact-backend thin wrappers rather than direct fork-runtime
calls. The activated closure is directly pinned and locked:

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

The parent process argv is always:

```text
<absolute sys.executable> -I -m hermes_reach.sources.youtube_worker
```

Query, canonical watch URL, limit, and language cross the process boundary
only in one length-prefixed UTF-8 JSON v1 frame. The child maps the closed
operation enum to exactly:

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

Hermes owns the closed mapping from the public `limit` to `ytsearch<limit>` and
from the optional public language to `subtitleslangs`. When no language is
requested, Hermes supplies the fixed preference order `zh-Hans`, then `zh`,
then `en`; this order is not an yt-dlp default or an Agent-Reach operation
contract.

The worker constructs `YoutubeDL(params)` directly. It never invokes yt-dlp's
CLI parser or `main`, so host config files and caller-selected flags cannot
enter the execution path.

## Security Composition

Every request receives private HOME, XDG config/cache/data, DENO_DIR, and TMP
directories. The child environment has no PATH, disables proxy variables,
sets `YTDLP_NO_PLUGINS=1`, and contains no provider credentials, Cookie,
browser, npm, or host configuration. The worker also resets yt-dlp's global
plugin directory list before constructing the downloader.

The backend uses `cachedir=False`, `proxy=""`, no cookie file or browser
cookie source, no netrc command or credentials, no postprocessors, and no
remote components. The only JavaScript runtime is the executable installed
beside the running Python interpreter; all three distribution versions and
that executable's regular, executable, single-link shape are checked before
network extraction. No Node, PATH, npm, GitHub EJS, or runtime-install fallback
exists.

Subtitle work stays under the private root. The selected file must be a
regular, single-link VTT beneath that root and is opened without following
links, byte-bounded before UTF-8 decoding, projected to a bounded result, and
removed. Hermes chooses one language using the fixed request/default mapping,
prefers manual over automatic captions for that selected language, verifies
the video and file identity, and chooses the single file that may cross the
closed result boundary. The parent removes all remaining private state after
the worker is reaped.

Stdin, stdout, JSON depth/items/nodes/scalars, projected fields, subtitle
files, and final results are bounded. Stderr and backend logging are discarded.
Timeout, cancellation, nonzero exit, malformed output, and overflow kill and
reap the worker process group before temporary state is removed. Raw yt-dlp
dictionaries, formats, signed media/subtitle URLs, HTTP headers, warnings,
exception strings, query, target URL, and temporary paths never cross stdout or
enter provenance, receipts, audit, or stable errors.

## Semantic Delta

There is no platform backend substitution, but the wrapper has explicit
product semantics. Hermes owns:

- the closed public search-limit mapping to `ytsearch<limit>`;
- the requested-language mapping and default `zh-Hans -> zh -> en` preference;
- manual-before-automatic caption choice for the selected language;
- video identity checks, canonical watch URL derivation, and subtitle file
  identity/containment/selection;
- projection into the v1 schema and every process, frame, file, item, and
  character bound.

yt-dlp owns native extraction ordering, YouTube metadata and caption
discovery, extractor behavior, subtitle download, and YouTube network
semantics. Hermes does not add a YouTube endpoint or parse a YouTube response
schema; it selects and bounds one result from yt-dlp's discovered data.

The wrapper deliberately does not approximate comment pages by refetching and
slicing larger prefixes. That would create new pagination semantics and growing
work per page. It also does not treat subtitle absence as permission to invoke
Whisper or another model.

## Review Milestone

Review this decision on any Agent-Reach, yt-dlp, yt-dlp-ejs, or Deno pin
change; dependency closure or wheel hash change; packaged EJS source/hash
change; JS runtime invocation change; extractor or subtitle-file behavior
change; worker protocol change; backend identity change; or classification
change. An Agent-Reach pin change reopens all 63 catalog operations.

## Rollback

Remove the three direct pins and the production worker/client, restore the
three default `setup_required` markers, and remove the three activated review
rows. Keep `read.comments` implemented but unbound and keep
`transcribe.video` planned. No database, grant, wire, catalog, or stored-content
migration is required.
