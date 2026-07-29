# Releasing Hermes Reach

Hermes Reach publishes pre-release wheel and source distributions through
GitHub Releases. PyPI is intentionally deferred because the package requires
official Agent-Reach from one exact Git commit, and Warehouse does not accept
that direct VCS `Requires-Dist`. Do not vendor, fork, loosen, or remove that
dependency to make an index upload succeed.

The release invariant is:

```text
build once -> install exact sdist offline -> lifecycle-test exact wheel
           -> checksum exact bytes -> attest exact bytes -> publish exact bytes
```

No release job rebuilds either distribution after exact-artifact acceptance.

## Repository prerequisites

Before the first tag, the repository owner must verify these external controls:

- `main` requires the reusable quality gate and owner review before a rebase
  merge, and rejects history rewrites/force pushes;
- a ruleset protects `v*` tags from update and deletion;
- the `github-release` environment has the intended required reviewer;
- Actions may create artifact attestations for the public repository;
- immutable releases are enabled if the repository supports them;
- workflow Actions and the uv binary checksum still match the reviewed pins.

The workflow cannot configure or prove these repository settings. A tag push
without them is an operator error, even if the YAML gate passes.

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

Build the candidate bytes once, without a generated file in `dist/`, then prove
the exact sdist installs offline, run the full Hermes lifecycle against the
exact wheel, and perform the release metadata checks:

```bash
uv run python scripts/prepare_release.py version --tag v0.1.0a0
uv build --clear --no-create-gitignore --out-dir dist
uv run pytest tests/test_connector_release_security.py \
  --release-dist "$PWD/dist"
uv run python scripts/prepare_release.py artifacts \
  --tag v0.1.0a0 \
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
git tag v0.1.0a0
git push origin refs/tags/v0.1.0a0
```

Do not push the tag unless the status output is empty and the local commit is
the reviewed `origin/main`. The tag push is the sole publication trigger;
there is no manual dispatch or branch publication path.

The workflow then:

1. verifies tag, version, commit, and `main` ancestry;
2. passes Python 3.11, 3.12, and 3.13 plus static gates;
3. builds one wheel/sdist pair, installs the exact sdist offline, and runs the
   full Hermes lifecycle against the exact wheel;
4. hands the checksummed files between jobs through one Actions artifact;
5. creates separate GitHub OIDC build attestations for the wheel and sdist
   whose digests are listed in `SHA256SUMS`;
6. rechecks the remote tag and current remote `main` ancestry, then publishes a
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

GitHub separately renders generated `Source code (zip)` and
`Source code (tar.gz)` links for the tag. Those snapshots are not uploaded by
the workflow and are not covered by `SHA256SUMS` or this workflow's build
attestations. Do not count them as part of the workflow's closed three-asset
set.

Verify the published wheel and sdist independently, then check their digests:

```bash
gh release download v0.1.0a0 --repo izumi0uu/hermes-reach --dir release-audit
gh attestation verify \
  release-audit/hermes_reach-0.1.0a0-py3-none-any.whl \
  --repo izumi0uu/hermes-reach \
  --signer-workflow izumi0uu/hermes-reach/.github/workflows/release.yml
gh attestation verify \
  release-audit/hermes_reach-0.1.0a0.tar.gz \
  --repo izumi0uu/hermes-reach \
  --signer-workflow izumi0uu/hermes-reach/.github/workflows/release.yml
cd release-audit
shasum -a 256 --check SHA256SUMS
```

The two attestation commands name only the wheel and sdist as subjects and
restrict the signer to the reviewed release workflow. `SHA256SUMS` itself is
not an attestation subject; it is only the manifest used for checksum
verification. Checksums establish byte equality, not publisher identity.
GitHub provenance does not prove that the reviewed source is defect-free or
that repository administrators are uncompromised.

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

Publishing to PyPI requires a separately reviewed dependency strategy after
official Agent-Reach has an acceptable index artifact. It is not an additional
job to enable in this workflow.
