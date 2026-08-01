# OpenCLI Social Runtime Decision

## Decision

Reddit, Facebook, and Instagram execute only through the closed
`agent_reach.execution.v1` operations in the owner-maintained Agent-Reach fork.
Hermes Reach does not construct OpenCLI argv, parse OpenCLI YAML, select browser
actions, or project platform-native rows.

The complete batch is Connector-only:

| Source | Operations | Scope |
| --- | --- | --- |
| Reddit | `search.posts`, `read.post`, `browse.subreddit`, `browse.hot`, `browse.popular`, `browse.all`, `read.subreddit` | `public` |
| Facebook | `search`, `read.profile` | `public` |
| Facebook | `browse.feed`, `browse.groups` | `account_visible` |
| Instagram | `search.users`, `read.profile`, `browse.user_posts` | `public` |
| Instagram | `browse.explore` | `account_visible` |

Every row is classified `direct_owner_fork_runtime`, uses backend
`opencli/1.8.6-hermes.1`, requires `opencli_session.v1`, and has
`binding_surface=connector_only`.

## Provenance

- Official Agent-Reach version: `1.5.0`
- Official Agent-Reach audit base:
  `b4d52c46c9113cb0f653d6df4cf71ebadf4930ac`
- Original social-batch PR head:
  `a3dcdb3a6638e14ceda8cfa9a3cc7a010d80fa80`
- Original social-batch tree:
  `302db7526ed84b1565fa24baf5c06ced69385d80`
- Initial social-batch rebase integration:
  `ec4a5e36434c9df9ee236dc12734843163fc17ac`
- Reviewed hardlink-fix PR head:
  `c57ae5b8d78fed6ad52a1f52731db589d875f8a9`
- Reviewed hardlink-fix tree:
  `385b9c95cb3a6372ed1b68b606abc3faed71f307`
- Final rebase integration:
  `281dc3352c63cdb644f02e028cc5d645c279954a`
- Final integration tree:
  `385b9c95cb3a6372ed1b68b606abc3faed71f307`
- Pre-hardlink-fix integration:
  `ec4a5e36434c9df9ee236dc12734843163fc17ac`
- Social-disable rollback Agent-Reach pin:
  `9b69146588b1d162515b81db26b51643c15de8eb`

The reviewed hardlink-fix PR head and final integration have identical trees.
Hermes pins the final commit, not a branch or tag. The fix permits multiple
links only for the packaged lifecycle guard after RECORD/current-byte
validation; the private guard snapshot remains single-link and user-selected
Node/OpenCLI artifacts still reject hardlinks.

The exact backend artifact is:

- package: `@jackwener/opencli@1.8.6-hermes.1`
- official OpenCLI base:
  `399c0de2a76eb979aee3a3836cf2d24fd247780f`
- owner-fork commit:
  `594b21498680f6372279f178aa9b3aaed2c71e35`
- owner-fork tree:
  `be64296855727d00478cc857f2b26eb7d3790057`
- tarball SHA-256:
  `dac98c69802621d55d8e3a5ae7032f47ab22b3785331a69499a907456f9dfb73`

Published OpenCLI `1.8.6` is evidence for commands and schemas but is rejected
for activation. Its browser lifecycle can start or kill daemon processes, and
its Instagram adapters do not emit the complete typed error evidence required
by this boundary.

## Ownership Boundary

Agent-Reach owns:

- all 15 operation descriptors and argument schemas;
- the exact OpenCLI commands and fixed options;
- Node/OpenCLI tree revalidation and private byte snapshots;
- the no-lifecycle-mutation preload guard;
- process argv, environment, timeout checkpoints, and output collection;
- strict YAML and typed error parsing;
- platform row validation, correlation, bounds, and native projection;
- backend identity and error classification.

Hermes Reach owns:

- immutable public request validation and catalog scope;
- signed Connector grants and trusted-device isolation;
- startup attestation and explicit operator activation;
- the fixed isolated Agent-Reach worker transport;
- independent closed-result validation and product normalization;
- one same-invocation retry policy;
- signed receipts, availability snapshots, evidence, and audit.

Hermes source must contain no social-platform OpenCLI argv table, YAML parser,
DOM selector, endpoint, browser action, or fallback backend. The former
Reddit-only wrapper is retired by this decision.

## Trusted-Device Capability

`OpenCliSessionV1` contains only process-local identities:

```text
node_executable + node_sha256
opencli_root + opencli_cli + opencli_tree_sha256
session_home
```

The paths and session home stay on the trusted Connector. They may cross the
private parent/worker stdin boundary on that device, but never enter a signed
VPS request, result, receipt, availability snapshot, audit record, CLI error,
or backend failure envelope. Cookies, browser storage, passwords, and session
contents are never extracted or copied to the VPS.

Agent-Reach revalidates and privately snapshots Node, the complete production
npm prefix, and its packaged lifecycle guard before each backend invocation.
It uses only an already-running compatible OpenCLI daemon. A stopped or stale
daemon fails closed; the guarded runtime cannot spawn, restart, stop, signal,
or replace it.

## Nested Process Cleanup

The isolated Hermes worker and the Node/OpenCLI invocation are separate
session and process-group leaders. Killing only the worker group cannot reap
Node, while exposing Node's PID to Hermes would add recycled-PID signaling
authority across the runtime boundary.

On deadline or caller cancellation, Hermes therefore sends `SIGTERM` only to
the still-running worker. The worker's handler records cancellation, and its
next Agent-Reach execution checkpoint unwinds through the fork-owned runtime,
which kills and reaps its own Node/OpenCLI process group. Hermes waits for at
most five seconds and then kills and reaps the worker group only as a bounded
fallback. Cleanup never replaces the active deadline failure or caller
cancellation.

Real-process regressions prove that Node enters a session distinct from the
worker and that its PID is gone after both deadline and caller cancellation.
This is a process-level guarantee for the supported cooperative paths; a
worker killed with `SIGKILL`, a kernel hang, or a crash that bypasses Python
cleanup would require kernel containment such as a cgroup for a stronger
guarantee.

## Error And Retry Contract

OpenCLI public typed evidence maps to the closed Agent-Reach error taxonomy.
Hermes then maps it to Connector backend codes without preserving message,
stderr, query, username, target, result body, or path text.

`invalid_input` and `not_found` prove that the exact authenticated backend was
reached and therefore retain an authenticated availability snapshot. Backend
unavailable, deadline, rate-limit, and transient results degrade connectivity.
Authentication, authorization, incompatibility, permanent, and contract
violations fail closed as unavailable until normal snapshot recovery policy
allows another authorized attempt.

The Connector signs one invocation and spends one grant use. Inside that same
trusted-device execution, the social worker may retry once only for the closed
transient, backend-unavailable, or deadline classes. Both attempts share the
original absolute operation deadline. The VPS runtime never creates a second
signed request for this retry.

## Activation

`connector serve` requires all four explicit absolute inputs:

```text
--opencli-social-node
--opencli-social-root
--opencli-social-cli
--opencli-social-session-home
```

All four must be present or absent. Before state unlock or listener creation,
the operator sees the Node digest, OpenCLI tree digest, backend identity, and
all 15 exact source-operation scopes, then types the literal `enable`.
Absence leaves the Connector execution composition empty. Partial, unsafe, or
drifted input fails closed without service startup or backend work.

## Update And Rollback

Any change to the Agent-Reach official base or fork commit, OpenCLI package or
tree, Node/OpenCLI/session capability, lifecycle guard, command/schema,
Instagram typed error behavior, worker framing, backend identity, error map,
or platform projection reopens all 15 decisions.

Rollback restores Hermes to the preceding exact Agent-Reach pin and removes
the 15 Connector bindings. Existing grants and database state need no
migration; the operations simply become unavailable. No browser session,
Connector authority, receipt, or audit data is rewritten.
