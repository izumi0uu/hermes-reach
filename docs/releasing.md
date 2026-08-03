# Releasing Hermes Reach

Hermes Reach publishes pre-release wheel and source distributions through
GitHub Releases. PyPI is intentionally deferred because the package requires
the reviewed `izumi0uu/Agent-Reach` owner fork from one exact Git commit, and
Warehouse does not accept that direct VCS `Requires-Dist`. The fork records its
official `Panniantong/Agent-Reach` base, but the install dependency is the exact
reviewed fork commit. Do not vendor, loosen, retarget, or remove that
dependency to make an index upload succeed.

Installing the public wheel therefore requires Git, PyPI access for normal
dependencies, and GitHub HTTPS access for that exact owner-fork commit. A wheel
installer does not read this repository's `uv.lock`. Before release, the exact
fork commit must have an immutable integration tag protected from movement and
deletion so old installs and rollback remain reachable. The tag is only a
recovery reference and never the dependency selector; the commit in wheel
metadata remains authoritative.

The current integration and rollback references are:

| Purpose | Recovery reference | Exact dependency commit |
| --- | --- | --- |
| Final 33-operation rebase integration and current pin | `hermes-reach-integration-0.1.0a4` (protected from update and deletion) | `75cd48c6274e7f4740530d97877ec048708d5334` |
| Rejected pre-freeze 35-operation state | none; retained only as LinkedIn rejection evidence | `7bc42839d3dd290e4af93b24e0b03b738cff0ffa` |
| Rollback: 29-operation public/social integration | pending; no immutable recovery tag exists | `281dc3352c63cdb644f02e028cc5d645c279954a` |
| Historical: pre-hardlink-fix 29-operation integration | pending; incompatible with uv hardlink installs | `ec4a5e36434c9df9ee236dc12734843163fc17ac` |
| Rollback: 14-operation public-platform integration | pending; no immutable recovery tag was created by the social batch | `9b69146588b1d162515b81db26b51643c15de8eb` |
| Rollback: integrated YouTube read-video runtime | `hermes-reach-integration-0.1.0a3` | `2a5829cf3b50bc435c647bfae4c050b1837d0235` |
| Rollback: RSS + Bilibili execution v1, YouTube exact wrappers | `hermes-reach-integration-0.1.0a2` | `f195253d53befdb012d7aa575e732ec627ec29ac` |
| Earlier rollback: RSS execution v1 / Hermes Bilibili wrapper | `hermes-reach-integration-0.1.0a1` | `806205fd106f4f4453624becfd773acce8418cf1` |

Owner-fork PR #6's final reviewed head
`e91e3efa045e75f08d4e7fdd9749fe26d4f774c5` resolves to tree
`e86ee839621360b991d985ad9d4cb18e36f86351`. It was rebase-merged with tree
equivalence into `hermes/execution-v1` as final 33-descriptor integration
`75cd48c6274e7f4740530d97877ec048708d5334`, which Hermes pins exactly.
Protected lightweight tag `hermes-reach-integration-0.1.0a4` points directly to
that commit. Active repository ruleset `Protect Hermes Reach integration tags`
(`19975135`) matches `refs/tags/hermes-reach-integration-*`, blocks update and
deletion, and has no bypass actor. Final Hermes provenance, RECORD, runtime,
pin-sensitive, and exact-artifact verification are complete. The tag remains a
recovery reference; installation metadata still selects the commit.

The rejected pre-freeze commit `7bc42839d3dd290e4af93b24e0b03b738cff0ffa`
resolves to tree `382557e0bec76819f0633f31895580a0f549b6bd`. It contains the two
rejected LinkedIn descriptors and is retained only as pre-freeze evidence, not
as routing, dependency, merge, tag, or release authority. Rollback restores the
prior final integration
`281dc3352c63cdb644f02e028cc5d645c279954a`, which resolves to tree
`385b9c95cb3a6372ed1b68b606abc3faed71f307`. That integration was rebase-merged
from reviewed hardlink-fix PR head `c57ae5b8d78fed6ad52a1f52731db589d875f8a9`, whose tree is
byte-equivalent. The final integration's recovery-tag prerequisite is now
satisfied. Package publication remains a separate operation: public
`v0.1.0a1` is immutable and must not be moved or reused, so the next candidate
must first advance Hermes Reach to a new version (sequentially `0.1.0a2`) and
pass every release gate against those exact new artifact bytes.
The prior 29-operation integration `ec4a5e36434c9df9ee236dc12734843163fc17ac`,
tree `302db7526ed84b1565fa24baf5c06ced69385d80`, remains provenance for the
social batch but is not a release-compatible rollback under uv hardlink
installation. The earlier integration
`9b69146588b1d162515b81db26b51643c15de8eb`, tree
`e19835071ae6560431b66d5a21e51b598d3d9c81`, remains the exact social-batch
rollback pin; it is historical evidence, not an active dependency selector.
The previous integration `2a5829cf3b50bc435c647bfae4c050b1837d0235`
remains protected by `hermes-reach-integration-0.1.0a3`. Do not confuse fork
recovery tags with the Hermes package release tag `v0.1.0a1`. Installation
metadata always selects the exact commit, never an integration tag.

The release invariant is:

```text
build once -> install exact sdist offline -> lifecycle-test exact wheel
           -> checksum exact bytes -> resolve exact wheel uncached on 3 Pythons
           -> attest exact bytes -> publish exact bytes
```

No release job rebuilds either distribution after exact-artifact acceptance.

## Repository prerequisites

Before every release tag, the repository owner must verify these external
controls:

- `main` requires the reusable quality gate and owner review before a rebase
  merge, and rejects history rewrites/force pushes;
- a ruleset protects `v*` tags from update and deletion;
- the `github-release` environment has the intended required reviewer;
- Actions may create artifact attestations for the public repository;
- immutable releases are enabled if the repository supports them;
- the exact Agent-Reach fork commit has one immutable integration tag protected
  from update and deletion, and its tag-to-commit mapping is recorded;
- workflow Actions and the uv binary checksum still match the reviewed pins.

The workflow cannot configure or prove these repository settings. A tag push
without them is an operator error, even if the YAML gate passes.
Final integration `75cd48c6274e7f4740530d97877ec048708d5334`, tree
`e86ee839621360b991d985ad9d4cb18e36f86351`, is the tree-equivalent rebase
integration of owner-fork PR #6's final reviewed head
`e91e3efa045e75f08d4e7fdd9749fe26d4f774c5` into `hermes/execution-v1`.
Protected immutable recovery reference `hermes-reach-integration-0.1.0a4`
points directly to the integration commit. Ruleset `19975135` prevents update
and deletion with no bypass actor, and final Hermes verification is complete.
This clears the Agent-Reach recovery prerequisite only; publishing the next
Hermes Reach pre-release still requires a new version and the complete workflow
below. The rejected pre-freeze state remains evidence only.

## Local dry run

Run the complete normal gate first:

```bash
uv sync --locked --all-groups
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

After the reviewed release change advances `pyproject.toml`, `plugin.yaml`, and
the lock metadata to `0.1.0a2`, build the candidate bytes once without a
generated file in `dist/`. Then prove the exact sdist installs offline, run the
full Hermes lifecycle against the exact wheel, and perform the release metadata
checks:

```bash
uv run python scripts/prepare_release.py version --tag v0.1.0a2
uv build --clear --no-create-gitignore --out-dir dist
uv run pytest tests/test_connector_release_security.py \
  --release-dist "$PWD/dist"
uv run python scripts/prepare_release.py artifacts \
  --tag v0.1.0a2 \
  --dist-dir "$PWD/dist"
```

The final command requires exactly one expected wheel and one expected sdist,
checks both embedded metadata records, and creates `dist/SHA256SUMS`. A second
run intentionally fails until `uv build --clear` recreates a clean directory.

## Tag and publish

The workflow accepts only the canonical pre-release tag matching both
`pyproject.toml` and `plugin.yaml`. It currently requires a lightweight tag so
the remote ref directly identifies the reviewed commit.

After the release change is reviewed, rebased, merged, and pushed to `main`:

```bash
git switch main
git pull --ff-only
git status --short
git tag v0.1.0a2
git push origin refs/tags/v0.1.0a2
```

Do not push the tag unless the status output is empty and the local commit is
the reviewed `origin/main`. The tag push is the sole publication trigger;
there is no manual dispatch or branch publication path.

The workflow then:

1. verifies tag, version, commit, and `main` ancestry;
2. passes Python 3.11, 3.12, and 3.13 plus static gates;
3. builds one wheel/sdist pair, installs the exact sdist offline, and runs the
   full Hermes lifecycle against the exact wheel;
4. retains that wheel alone in a one-day Actions artifact for a clean
   dependency-resolution matrix, while the release handoff remains the closed
   checksummed wheel, sdist, and `SHA256SUMS` set;
5. on Python 3.11, 3.12, and 3.13, installs the exact wheel without a dependency
   cache, repository lock, offline mode, or source checkout, then runs
   `uv pip check` and validates fork provenance, the 33-descriptor execution
   handshake including each of the seven runtime modules and all 14 attested
   execution files, and the 15-channel catalog;
6. creates separate GitHub OIDC build attestations for the wheel and sdist
   whose digests are listed in `SHA256SUMS`;
7. rechecks the remote tag and current remote `main` ancestry, then publishes a
   non-latest pre-release.

`gh release create` normally stages asset uploads through a private draft and
publishes only after they succeed. A client, network, or API failure can be
ambiguous, however: the tag may have no release, a private draft, or a public
pre-release even when the command reports failure. Inspect the tag in the
Releases UI and with
`gh release view <tag> --json isDraft,isPrerelease,url` before taking any
recovery action. A run that fails before release creation leaves only the
short-lived Actions artifact. Never retry publication until the observed state
is understood, and never silently overwrite an existing release.

## Observe and audit

Review the workflow run, all matrix jobs, the environment approval, and the
attestation URLs before announcing a release. The workflow must upload exactly
these three release assets:

```text
hermes_reach-<version>-py3-none-any.whl
hermes_reach-<version>.tar.gz
SHA256SUMS
```

The one-day wheel-only dependency-resolution artifact is an internal Actions
handoff for the three-version gate. It is not a release asset, a fourth
distribution, or an alternative download channel.

GitHub separately renders generated `Source code (zip)` and
`Source code (tar.gz)` links for the tag. Those snapshots are not uploaded by
the workflow and are not covered by `SHA256SUMS` or this workflow's build
attestations. Do not count them as part of the workflow's closed three-asset
set.

Verify the published wheel and sdist independently, then check their digests:

```bash
gh release download v0.1.0a2 --repo izumi0uu/hermes-reach --dir release-audit
gh attestation verify \
  release-audit/hermes_reach-0.1.0a2-py3-none-any.whl \
  --repo izumi0uu/hermes-reach \
  --signer-workflow izumi0uu/hermes-reach/.github/workflows/release.yml
gh attestation verify \
  release-audit/hermes_reach-0.1.0a2.tar.gz \
  --repo izumi0uu/hermes-reach \
  --signer-workflow izumi0uu/hermes-reach/.github/workflows/release.yml
cd release-audit
shasum -a 256 --check SHA256SUMS
cd ..
```

The two attestation commands name only the wheel and sdist as subjects and
restrict the signer to the reviewed release workflow. `SHA256SUMS` itself is
not an attestation subject; it is only the manifest used for checksum
verification. Checksums establish byte equality, not publisher identity.
GitHub provenance does not prove that the reviewed source is defect-free or
that repository administrators are uncompromised.

## Public wheel smoke

The pre-publication gate already resolves the unpublished exact wheel on all
three supported Python versions. After provenance and checksums pass, recheck
the published bytes with real, uncached dependency resolution in a fresh
environment. Run this outside the repository checkout. Keep the process `HOME`
unchanged; isolate Hermes and XDG state with their supported overrides instead.
The commands show Python 3.11; repeat the complete smoke in separate roots for
Python 3.12 and 3.13 before announcing the release:

```bash
WHEEL="$(pwd)/release-audit/hermes_reach-0.1.0a2-py3-none-any.whl"
SMOKE_ROOT="$(mktemp -d)"
SMOKE_VENV="$SMOKE_ROOT/venv"
SMOKE_PYTHON_VERSION=3.11
ORIGINAL_HOME="$HOME"
test -f "$WHEEL"

uv venv --no-project --no-config --no-python-downloads \
  --python "$SMOKE_PYTHON_VERSION" \
  "$SMOKE_VENV"
mkdir -p \
  "$SMOKE_ROOT/hermes" \
  "$SMOKE_ROOT/bundled-plugins" \
  "$SMOKE_ROOT/xdg-config" \
  "$SMOKE_ROOT/xdg-cache" \
  "$SMOKE_ROOT/xdg-data"

export HERMES_HOME="$SMOKE_ROOT/hermes"
export HERMES_BUNDLED_PLUGINS="$SMOKE_ROOT/bundled-plugins"
export XDG_CONFIG_HOME="$SMOKE_ROOT/xdg-config"
export XDG_CACHE_HOME="$SMOKE_ROOT/xdg-cache"
export XDG_DATA_HOME="$SMOKE_ROOT/xdg-data"
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1
export GIT_TERMINAL_PROMPT=0
unset PYTHONPATH HERMES_ENABLE_PROJECT_PLUGINS HERMES_REACH_VPS_STATE_DIRECTORY
test "$HOME" = "$ORIGINAL_HOME"

SMOKE_PYTHON="$SMOKE_VENV/bin/python"
SMOKE_HERMES="$SMOKE_VENV/bin/hermes"
uv pip install \
  --no-cache \
  --no-config \
  --no-python-downloads \
  --python "$SMOKE_PYTHON" \
  "hermes-agent==0.19.0" \
  "$WHEEL"
uv pip check --no-config --no-python-downloads --python "$SMOKE_PYTHON"
```

The install must resolve the exact owner-fork VCS dependency from wheel
metadata. It must not use `--no-deps`, a repository checkout, `PYTHONPATH`, or a
pre-populated Hermes profile. Verify that the built VCS dependency recorded the
same exact PEP 610 provenance expected by Hermes Reach:

```bash
"$SMOKE_PYTHON" -I - <<'PY'
import json
from importlib.metadata import distribution

from hermes_reach.agent_reach_bridge import (
    AGENT_REACH_FORK_COMMIT,
    AGENT_REACH_FORK_URL,
    validate_agent_reach_execution_contract,
)

raw = distribution("agent-reach").read_text("direct_url.json")
assert raw is not None
document = json.loads(raw)
assert document["url"] == AGENT_REACH_FORK_URL, document
assert document["vcs_info"] == {
    "vcs": "git",
    "requested_revision": AGENT_REACH_FORK_COMMIT,
    "commit_id": AGENT_REACH_FORK_COMMIT,
}, document
apis = tuple(
    validate_agent_reach_execution_contract(runtime_module=runtime_module)
    for runtime_module in (
        "rss",
        "bilibili",
        "youtube",
        "v2ex",
        "exa",
        "opencli_social",
        "xueqiu",
    )
)
api = apis[0]
assert all(candidate.capabilities == api.capabilities for candidate in apis)
assert api.opencli_session_capability == "opencli_session.v1"
assert api.opencli_session_type.__name__ == "OpenCliSessionV1"
assert api.xueqiu_session_capability == "xueqiu_session.v1"
assert api.xueqiu_session_type.__name__ == "XueqiuSessionV1"
assert tuple((item.source, item.operation) for item in api.capabilities) == (
    ("rss", "read.feed"),
    ("rss", "browse.entries"),
    ("bilibili", "search.videos"),
    ("bilibili", "read.video"),
    ("bilibili", "browse.hot"),
    ("bilibili", "browse.rank"),
    ("youtube", "read.video"),
    ("youtube", "search.videos"),
    ("youtube", "read.subtitles"),
    ("v2ex", "browse.hot"),
    ("v2ex", "browse.node_topics"),
    ("v2ex", "read.topic"),
    ("v2ex", "read.user"),
    ("exa", "search.web"),
    ("reddit", "search.posts"),
    ("reddit", "read.post"),
    ("reddit", "browse.subreddit"),
    ("reddit", "browse.hot"),
    ("reddit", "browse.popular"),
    ("reddit", "browse.all"),
    ("reddit", "read.subreddit"),
    ("facebook", "search"),
    ("facebook", "read.profile"),
    ("facebook", "browse.feed"),
    ("facebook", "browse.groups"),
    ("instagram", "search.users"),
    ("instagram", "read.profile"),
    ("instagram", "browse.user_posts"),
    ("instagram", "browse.explore"),
    ("twitter", "search.posts"),
    ("xiaohongshu", "search.notes"),
    ("xueqiu", "search.stocks"),
    ("exa", "search.code"),
)
print("exact Agent-Reach PEP 610 and seven-module runtime handshake verified")
PY
```

Then, in a separate process, prove that the installed entry point remains
disabled by default without importing the plugin:

```bash
"$SMOKE_PYTHON" -I - <<'PY'
from hermes_cli.config import ensure_hermes_home
from hermes_cli.plugins import discover_plugins, get_plugin_manager
from tools.registry import registry

ensure_hermes_home()
discover_plugins()
manager = get_plugin_manager()
records = [record for record in manager.list_plugins() if record["key"] == "reach"]
assert len(records) == 1 and records[0]["enabled"] is False, records
assert registry.get_tool_names_for_toolset("reach") == []
assert manager.find_plugin_skill("reach:agent-reach") is None
assert "reach" not in manager._cli_commands
print("installed and disabled by default")
PY
```

Enable without tool-override authority. Each following command is a new
process, which proves that persisted activation is sufficient. The default
doctor is intentionally no-network; do not add `--upstream` to this smoke:

```bash
"$SMOKE_HERMES" plugins enable reach --no-allow-tool-override
"$SMOKE_HERMES" reach status --json
"$SMOKE_HERMES" reach doctor --json
```

Run one credential-free live RSS operation through normal Hermes plugin
discovery and assert the normalized backend provenance:

```bash
"$SMOKE_PYTHON" -I - <<'PY'
import json

from hermes_cli.config import ensure_hermes_home
from hermes_cli.plugins import discover_plugins
from tools.registry import registry

ensure_hermes_home()
discover_plugins()
raw = registry.dispatch(
    "reach_read",
    {
        "source": "rss",
        "operation": "read.feed",
        "target": {
            "url": "https://github.com/izumi0uu/hermes-reach/releases.atom"
        },
    },
)
assert isinstance(raw, str), type(raw)
result = json.loads(raw)
assert result["outcome"] == "ok", result
assert len(result["groups"]) == 1, result
group = result["groups"][0]
assert group["provenance"]["backend_id"] == "feedparser", group
assert group["provenance"]["backend_version"] == "6.0.12", group
assert group["items"], group
print(json.dumps(result, sort_keys=True))
PY
```

This one live probe depends on DNS, GitHub egress, and service availability. It
can also encounter rate limits. Those are distinct from the fixture-backed
release acceptance test; production RSS fetching remains bounded by a 3-second
DNS limit and 5-second connect/read limits.

Finally disable the plugin, prove the `reach` command is absent in another
process, uninstall through the same environment's package manager, and recheck
the remaining dependency graph:

```bash
"$SMOKE_HERMES" plugins disable reach
if "$SMOKE_HERMES" reach status --json; then
  echo "reach command remained active after disable" >&2
  exit 1
fi

uv pip uninstall --no-config --no-python-downloads \
  --python "$SMOKE_PYTHON" hermes-reach
"$SMOKE_PYTHON" -I -c \
  'import importlib.util; assert importlib.util.find_spec("hermes_reach") is None'
uv pip check --no-config --no-python-downloads --python "$SMOKE_PYTHON"
```

Retain `$SMOKE_ROOT` with the release evidence until the audit is complete.

## Failure and recovery

- After an ambiguous `gh release create` failure, inspect the tag's actual
  release state before retrying. If no release exists, retain the workflow logs
  and let the short-lived Actions artifact expire. If a private draft exists,
  record the failure and explicitly delete that draft; do not automate draft or
  tag deletion.
- If inspection finds a public pre-release, treat it as published even if the
  client reported failure. GitHub has no disabled release state. Mark the title
  or release notes `WITHDRAWN` when repository policy permits, publish the
  affected artifact digests in a durable notice, and state that assets may
  remain downloadable. Do not retry `gh release create` for the same tag.
- After a release covered by the repository's external immutable-release
  setting is published, fix forward with a new version and tag. Never replace
  assets, move the old tag, or reuse the withdrawn version.
- User rollback remains: disable Reach, start a new Hermes session, uninstall
  from the same Hermes Python environment, then run `uv pip check`.
- A code rollback that reverses the four accepted search operations restores exact fork pin
  `281dc3352c63cdb644f02e028cc5d645c279954a`. Twitter/X search,
  Xiaohongshu search, Xueqiu stock search, and Exa
  Code become unavailable again. No protocol, grant, Connector, database,
  receipt, audit, or secret migration is needed.
- A code rollback that reverses the OpenCLI social batch restores the prior
  Hermes release and exact fork pin
  `9b69146588b1d162515b81db26b51643c15de8eb`. The 15 Connector-only social
  bindings become unavailable again; the rollback pin remains evidence and
  must not become an active selector in the current release.
- A code rollback that reverses the 14-operation public-platform batch restores
  exact fork pin `2a5829cf3b50bc435c647bfae4c050b1837d0235`, recoverable
  through `hermes-reach-integration-0.1.0a3`. YouTube search/subtitles return to
  exact wrappers, V2EX and Exa return to their prior disabled states, and
  YouTube `read.video` remains fork-owned. Protocol, grants, Connector state,
  database, receipts, and audit require no migration.
- The older YouTube read-video rollback pin
  `f195253d53befdb012d7aa575e732ec627ec29ac` remains recoverable through
  `hermes-reach-integration-0.1.0a2`.
- A code rollback that specifically reverses Bilibili fork execution restores
  the previous Hermes release and exact fork pin
  `806205fd106f4f4453624becfd773acce8418cf1`, recoverable through
  `hermes-reach-integration-0.1.0a1`. It needs no protocol, grant, Connector,
  database, receipt, or audit migration. Never move any integration tag.

Publishing to PyPI requires a separately reviewed, index-compatible dependency
strategy for the reviewed Agent-Reach integration. The current exact owner-fork
VCS dependency cannot be uploaded to Warehouse; PyPI is not an additional job
to enable in this workflow.
