# Exa mcporter Runtime Decision

## Decision

`exa:search.web` and `exa:search.code` are separate default-local
`direct_owner_fork_runtime` operations. Both use the exact reviewed mcporter
artifact closure, but they keep independent backend versions, endpoints,
methods, result schemas, parsers, and projection tests.

| Operation | Backend version | Result schema |
| --- | --- | --- |
| `search.web` | `0.12.3+exa-web.v1` | `exa.search.result.v1` |
| `search.code` | `0.12.3+exa-code.v1` | `exa.code.result.v1` |

## Provenance

- Agent-Reach official base:
  `b4d52c46c9113cb0f653d6df4cf71ebadf4930ac`
- Final reviewed owner-fork PR head:
  `e91e3efa045e75f08d4e7fdd9749fe26d4f774c5`
- Reviewed and final integration tree:
  `e86ee839621360b991d985ad9d4cb18e36f86351`
- Final rebase-merged owner-fork integration:
  `75cd48c6274e7f4740530d97877ec048708d5334`
- Rollback integration:
  `281dc3352c63cdb644f02e028cc5d645c279954a`

Hermes consumes the final integration by exact commit, never by branch or tag.
Owner-fork PR #6 rebase-merged the reviewed head into `hermes/execution-v1`;
the reviewed head and integration have the identical tree recorded above. The
integration branch remains untagged and is not publishable until immutable
recovery-tag protection and final Hermes verification are complete.

## Closed Invocation

Both descriptors accept only a trimmed `query` and bounded `limit`. Web is
fixed to the base Exa MCP endpoint and `web_search_exa`. Code is fixed to:

```text
endpoint: https://mcp.exa.ai/mcp?tools=get_code_context_exa
tool: get_code_context_exa
stdin: {"query": <query>, "numResults": <bounded limit>}
```

Official Agent-Reach 1.5.0 documented the stale `tokensNum` argument. A live
read-only schema probe showed that the method expects `numResults` and the
special endpoint above. The maintained fork records this as a contract
correction. Hermes does not translate the stale call or substitute Web search.

The fork alone owns endpoint and method selection, fixed mcporter argv,
stdin mapping, bounded process lifecycle, MCP envelope validation, Web text
grammar, Code `Title`/`URL`/`Code or Highlights or Text` grammar, native
projection, and backend error mapping. The request cannot select executable,
endpoint, method, output mode, OAuth, provider, or fallback.

## Artifact And Privacy Boundary

Both operations require ordered `network_access.v1` and
`mcporter_artifacts.v1` capabilities. Hermes constructs the latter only from a
complete operator declaration of the absolute Node executable and digest,
mcporter root/CLI and reviewed tree digest, and sterile config path/digest.
Missing or malformed evidence leaves both operations `setup_required`.

Before every call, Agent-Reach revalidates artifact ownership, modes, link
counts, containment, versions, dependency closure, tree identity, and sterile
config. The child has no shell, PATH, proxy, ambient credential, browser, or
fallback authority and runs in a private HOME/XDG/TMP process group.

Exa receives and may retain the submitted query. Hermes keeps the query out of
argv, environment, cwd, paths, provenance, receipts, audit, logs, exception
text, and persisted artifacts. Result and stderr bytes remain bounded and are
never included in stable failure envelopes. Each binding owns one provider
attempt so the generic runner cannot submit the query twice.

## Hermes Boundary

Hermes starts only the fixed isolated Python worker, performs the exact
`runtime_module="exa"` fork handshake, passes the already closed artifact
identity, independently validates the selected operation/backend/schema/result
frame, normalizes it, and owns receipts and audit. It contains no Exa endpoint,
method selector, result parser, API-key client, or semantic fallback.

## Review And Rollback

Reopen this decision on any official base or fork commit movement, execution
protocol/capability change, Node or mcporter closure change, endpoint/method/
schema/formatter change, provider-query policy change, worker framing, backend
identity, error mapping, or projection change.

Rollback restores `281dc3352c63cdb644f02e028cc5d645c279954a`, keeps Web
search on its previous exact descriptor, and returns Code search to
planned/unbound. No credential, public protocol, grant, Connector, database,
receipt, audit, or stored-data migration is required.
