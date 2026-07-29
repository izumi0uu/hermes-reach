# Agent-Reach as a Hermes plugin

Status: canonical architecture, frozen on 2026-07-29 against official
Agent-Reach `1.5.0` base
`b4d52c46c9113cb0f653d6df4cf71ebadf4930ac` and owner-fork integration
commit `806205fd106f4f4453624becfd773acce8418cf1`.

Hermes Reach is a Hermes security wrapper around an exact, owner-maintained
[Agent-Reach fork](https://github.com/izumi0uu/Agent-Reach). That fork is based
on the reviewed [official Agent-Reach](https://github.com/Panniantong/Agent-Reach)
commit above. Hermes Reach does not copy the Agent-Reach runtime or platform
implementations into this repository.

The fork adds one small, versioned execution boundary that official
Agent-Reach 1.5.0 does not provide. Today it owns exactly two operations:
`rss:read.feed` and `rss:browse.entries`. Every other operation remains an
exact-backend wrapper, implemented but unbound, or not implemented according
to the closed operation ledger. The presence of the fork is not evidence that
all 15 channels or all 63 Hermes operations are executable.

## How the adapter is structured

```mermaid
flowchart TD
    Official["Official Agent-Reach 1.5.0<br/>15-channel registry and backend evidence"] --> Fork["Owner fork at exact commit<br/>additive execution v1: RSS only"]
    Fork --> Bridge["Compatibility bridge<br/>PEP 610 provenance and capability handshake"]
    Hermes["Hermes Agent"] --> Entry["hermes_reach.register<br/>five closed tools, CLI, restricted skill"]
    Bridge --> Entry
    Entry --> Control["Hermes security and control plane<br/>validation, grants, isolation, bounds, audit"]
    Control --> RSS["2 direct owner-fork runtime calls<br/>already-fetched RSS document"]
    Control --> Local["7 default-local exact-backend wrappers<br/>Bilibili and YouTube"]
    Control --> Remote["1 explicit Connector-only wrapper<br/>Reddit read.post"]
    RSS --> Feedparser["fork-owned feedparser invocation and projection"]
    Local --> Backends["bili-cli and yt-dlp"]
    Remote --> OpenCLI["fixed OpenCLI read"]
    Feedparser --> Results["bounded Hermes v1 results and audit metadata"]
    Backends --> Results
    OpenCLI --> Results
```

`hermes_reach.register` remains the only Hermes plugin entry point. It exposes
five closed tools, the `reach` operator command, and the reviewed
`reach:agent-reach` skill. It never exposes the fork API, a backend selector,
raw command, argv, browser session, credential, or arbitrary Agent-Reach
operation to Hermes tool input.

The compatibility bridge validates the installed distribution before plugin
registration:

- Agent-Reach version is exactly `1.5.0`;
- PEP 610 provenance names the owner fork and exact integration commit;
- before any fork code is imported, RECORD metadata and the current on-disk
  digest match for both parent package initializers and all four reviewed
  `execution.v1` files;
- the fork reports execution protocol `v1`;
- static discovery contains exactly the two reviewed RSS descriptors, schemas,
  host capability, backend identity/version, and hard limits; and
- the 15-channel projection still matches the Hermes source catalog.

Any mismatch fails closed before a Reach tool, CLI command, or skill is
registered.

## Who owns each boundary

| Boundary | Owner | Responsibility |
| --- | --- | --- |
| Official baseline | Official Agent-Reach | 15-channel registry, backend ordering evidence, compatibility metadata, reviewed doctor inputs |
| RSS execution API | Owner Agent-Reach fork | Closed `execution.v1` discovery, feedparser invocation, RSS operation selection, source-native projection, partial classification, backend provenance |
| Hermes plugin lifecycle | Hermes Reach | Plugin entry point, five `reach_*` tools, `reach` CLI, skill registration |
| Security and control plane | Hermes Reach | Closed validation, grants, Connector, Bitwarden isolation, RSS fetch policy, worker containment, cancellation, normalization, redaction, receipts, audit, rollback |
| Non-RSS retrieval | Exact selected backend | Bilibili, YouTube, and Connector-only Reddit semantics through fixed reviewed wrappers |
| Product contract | Hermes Reach | The 63-operation v1 catalog, availability, normalized groups/items/errors, result bounds |
| Routing skill | Hermes Reach safety overlay | Routes only through the five closed tools and never grants raw shell, browser, MCP, setup, or mutation authority |

The dependency direction is one way. The fork imports no Hermes types and
knows nothing about Hermes tools, grants, Connector, Bitwarden, receipts,
durable audit, or public response envelopes. Hermes provides the already
fetched bounded RSS document as a typed host capability; the fork cannot open
the URL through that contract or request filesystem, network, config, browser,
or credential capabilities.

The worker is an isolated Python process with an empty environment, fixed root
working directory, closed stdin/stdout framing, and no supplied secrets. It is
not a kernel-level syscall sandbox. A malicious accepted dependency could still
attempt ambient filesystem or network syscalls, so exact PEP 610 provenance,
commit review, artifact controls, and prompt rollback remain a supply-chain
trust boundary rather than being replaced by process isolation.

Hermes's internal `hermes_reach.runtime` remains the policy, dispatch, retry,
bounds, and audit control plane. It is not an Agent-Reach runtime and does not
own platform extraction semantics.

## Current executable surface

| Surface | Operations | Classification and binding |
| --- | ---: | --- |
| RSS/Atom | 2 | Direct owner-fork execution v1 calls in the default-local registry |
| Bilibili | 4 | Exact `bilibili-cli==0.6.2` wrappers in the default-local registry |
| YouTube | 3 | Exact `yt-dlp==2026.7.4` wrappers with pinned EJS and Deno closure |
| Reddit | 1 | Connector-only fixed OpenCLI `read.post`; absent from default composition |
| YouTube comments | 1 | Catalog-implemented but unbound and `setup_required` |

The frozen accounting is:

```text
Hermes catalog operations                  63
implemented                                11
planned                                    52
direct owner-fork runtime calls             2
exact-backend thin wrappers                 8
concrete executors                         10
default-local bindings                      9
Connector-only bindings                     1
implemented but unbound                     1
Hermes-owned platform exceptions            0
direct official Agent-Reach runtime calls   0
```

The eight exact-backend wrappers are Bilibili 4, YouTube 3, and Connector-only
Reddit 1. The concrete-executor count also includes the two fork-owned RSS
operations. It excludes `youtube:read.comments`, which has no binding or
backend attempt.

Web `read.url`, all eight GitHub operations, and all four V2EX operations stay
discoverable but planned and unavailable. Their former Hermes endpoint/parser
implementations remain closed. The owner fork contains no execution v1
descriptor for those operations. The shared registry also rejects bindings
for every catalog row that is still `planned`.

The complete machine-readable state lives in
[agent-reach-operation-ledger.json](agent-reach-operation-ledger.json). The
smaller [review manifest](agent-reach-reuse-decisions.json) contains detailed
P0/P1/P2 evidence; neither file grants authority beyond its exact rows.

## Why this is not a copied runtime

The execution fork is deliberately additive and narrow:

- fork `main` remains a fast-forward mirror of official `main`;
- execution work is rebased on a recorded official base;
- capability discovery is static and I/O-free;
- requests select only a registered source, operation, and closed arguments;
- host capabilities, limits, cancellation, private state, and secrets are not
  caller-controlled;
- results use closed typed schemas and redacted error codes; and
- migrated invocation and source projection are removed from Hermes rather
  than maintained twice.

This architecture moves the two RSS platform semantics to Agent-Reach while
leaving the compromised-VPS controls in Hermes. Expanding the fork operation by
operation is allowed only after the same ownership, safety, compatibility, and
63-operation audit gates pass. It is not permission to copy every channel or
to expose a generic execution surface.

## Fork updates, recovery, and rollback

Hermes depends on the exact fork commit, never a branch or tag. After the full
two-repository gate passes and before a Hermes release, maintainers must create
and protect an immutable integration tag as a reachability and recovery
reference. That tag name is chosen at release time and is not a dependency
selector; the commit pin remains authoritative.

For an upstream update:

1. fast-forward fork `main` to official `main`;
2. rebase the execution branch onto the new official head;
3. run fork tests, lint, type checks, build, and clean-wheel smoke;
4. rerun Hermes's complete 63-operation ledger and compatibility handshake;
5. publish a new reviewed integration commit and immutable recovery reference;
6. update Hermes in a separate rebase-only change pinned to that commit.

Rollback restores the previous Hermes release and its exact dependency pin.
No public Hermes protocol, Connector grant, database, or audit migration is
required. A consumed fork commit or immutable recovery reference must never be
moved or deleted.

Operation-level evidence lives in
[Agent-Reach reuse boundary](agent-reach-reuse-boundary.md). Connector threats,
activation, recovery, and rollback live in
[Connector security and operations](connector-security.md).
