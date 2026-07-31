from __future__ import annotations

import ast
import re
import shlex
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SHA_PIN = re.compile(r"\A[^@]+@[0-9a-f]{40}\Z")
ATTESTATION_COMMAND = re.compile(
    r"^gh attestation verify \\\n(?:^  .+(?:\n|\Z))+", re.MULTILINE
)
RELEASE_REPOSITORY = "izumi0uu/hermes-reach"
RELEASE_SIGNER_WORKFLOW = "izumi0uu/hermes-reach/.github/workflows/release.yml"

EXPECTED_ACTIONS = {
    "actions/attest-build-provenance@0f67c3f4856b2e3261c31976d6725780e5e4c373",
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
    "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
    "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9",
}
EXPECTED_EXECUTION_CAPABILITY_FIELDS = (
    "protocol_version",
    "source",
    "operation",
    "argument_schema_id",
    "result_schema_ids",
    "backend_id",
    "backend_version",
    "required_host_capabilities",
    "maximum_items",
    "maximum_document_bytes",
    "maximum_metadata_bytes",
    "maximum_output_bytes",
    "maximum_content_type_characters",
    "maximum_content_location_characters",
    "maximum_text_characters",
    "maximum_title_characters",
    "maximum_url_characters",
    "maximum_native_id_characters",
    "maximum_author_characters",
    "maximum_published_characters",
)
EXPECTED_EXECUTION_CAPABILITIES = (
    (
        "v1",
        "rss",
        "read.feed",
        "rss.read.feed.arguments.v1",
        ("rss.feed.v1",),
        "feedparser",
        "6.0.12",
        ("fetched_document.v1",),
        1,
        1_048_576,
        16_384,
        1_048_576,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "rss",
        "browse.entries",
        "rss.browse.entries.arguments.v1",
        ("rss.entry.v1",),
        "feedparser",
        "6.0.12",
        ("fetched_document.v1",),
        21,
        1_048_576,
        16_384,
        1_048_576,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "bilibili",
        "search.videos",
        "bilibili.search.videos.arguments.v1",
        ("bilibili.video.v1",),
        "bili-cli",
        "0.6.2",
        ("network_access.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        1_024,
        512,
    ),
    (
        "v1",
        "bilibili",
        "read.video",
        "bilibili.read.video.arguments.v1",
        ("bilibili.video.v1",),
        "bili-cli",
        "0.6.2",
        ("network_access.v1",),
        1,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        1_024,
        512,
    ),
    (
        "v1",
        "bilibili",
        "browse.hot",
        "bilibili.browse.hot.arguments.v1",
        ("bilibili.video.v1",),
        "bili-cli",
        "0.6.2",
        ("network_access.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        1_024,
        512,
    ),
    (
        "v1",
        "bilibili",
        "browse.rank",
        "bilibili.browse.rank.arguments.v1",
        ("bilibili.video.v1",),
        "bili-cli",
        "0.6.2",
        ("network_access.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        1_024,
        512,
    ),
    (
        "v1",
        "youtube",
        "read.video",
        "youtube.read.video.arguments.v1",
        ("youtube.video.v1",),
        "yt-dlp",
        "2026.7.4",
        ("network_access.v1",),
        1,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        1_024,
        512,
    ),
    (
        "v1",
        "youtube",
        "search.videos",
        "youtube.search.videos.arguments.v1",
        ("youtube.video.v1",),
        "yt-dlp",
        "2026.7.4",
        ("network_access.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        1_024,
        512,
    ),
    (
        "v1",
        "youtube",
        "read.subtitles",
        "youtube.read.subtitles.arguments.v1",
        ("youtube.subtitle.v1",),
        "yt-dlp",
        "2026.7.4",
        ("network_access.v1", "private_workspace.v1"),
        1,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        1_024,
        512,
    ),
    (
        "v1",
        "v2ex",
        "browse.hot",
        "v2ex.browse.hot.arguments.v1",
        ("v2ex.topic.v1",),
        "v2ex-public-api",
        "legacy-json-2026-07-31",
        ("network_access.v1",),
        50,
        1_048_576,
        16_384,
        1_048_576,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "v2ex",
        "browse.node_topics",
        "v2ex.browse.node_topics.arguments.v1",
        ("v2ex.topic.v1",),
        "v2ex-public-api",
        "legacy-json-2026-07-31",
        ("network_access.v1",),
        50,
        1_048_576,
        16_384,
        1_048_576,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "v2ex",
        "read.topic",
        "v2ex.read.topic.arguments.v1",
        ("v2ex.topic.v1", "v2ex.reply.v1"),
        "v2ex-public-api",
        "legacy-json-2026-07-31",
        ("network_access.v1",),
        21,
        1_048_576,
        16_384,
        1_048_576,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "v2ex",
        "read.user",
        "v2ex.read.user.arguments.v1",
        ("v2ex.profile.v1",),
        "v2ex-public-api",
        "legacy-json-2026-07-31",
        ("network_access.v1",),
        1,
        1_048_576,
        16_384,
        1_048_576,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "exa",
        "search.web",
        "exa.search.web.arguments.v1",
        ("exa.search.result.v1",),
        "exa-mcporter",
        "0.12.3+exa-web.v1",
        ("network_access.v1", "mcporter_artifacts.v1"),
        20,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
)


def _workflow(name: str) -> dict[str, Any]:
    loaded = yaml.load(
        (WORKFLOWS / name).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert isinstance(loaded, dict)
    return loaded


def _walk(value: object) -> Iterator[object]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _external_actions(*workflows: dict[str, Any]) -> set[str]:
    actions: set[str] = set()
    for workflow in workflows:
        for value in _walk(workflow):
            if isinstance(value, dict):
                candidate = value.get("uses")
                if isinstance(candidate, str) and not candidate.startswith("./"):
                    actions.add(candidate)
    return actions


def _commands(workflow: dict[str, Any]) -> list[str]:
    return [
        value["run"]
        for value in _walk(workflow)
        if isinstance(value, dict) and isinstance(value.get("run"), str)
    ]


def _attestation_commands(markdown: str) -> list[list[str]]:
    return [
        shlex.split(match.group(0).replace("\\\n", " "))
        for match in ATTESTATION_COMMAND.finditer(markdown)
    ]


def _step(workflow: dict[str, Any], job: str, name: str) -> dict[str, Any]:
    matches = [
        step for step in workflow["jobs"][job]["steps"] if step.get("name") == name
    ]
    assert len(matches) == 1
    return matches[0]


def _step_names(workflow: dict[str, Any], job: str) -> list[str]:
    names = [step.get("name") for step in workflow["jobs"][job]["steps"]]
    assert all(isinstance(name, str) for name in names)
    return names


def _python_heredoc(command: str) -> str:
    prefix, marker, remainder = command.partition("<<'PY'\n")
    assert marker and prefix.strip().endswith("-I -")
    source, marker, trailer = remainder.rpartition("\nPY")
    assert marker and not trailer.strip()
    return source


def _literal_assignment(source: str, name: str) -> object:
    matches: list[ast.expr] = []
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == name
                for target in statement.targets
            ):
                matches.append(statement.value)
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == name
            and statement.value is not None
        ):
            matches.append(statement.value)
    assert len(matches) == 1
    return ast.literal_eval(matches[0])


def test_ci_and_release_call_one_local_quality_workflow() -> None:
    ci = _workflow("ci.yml")
    release = _workflow("release.yml")

    assert ci["on"] == {"pull_request": "", "push": {"branches": ["main"]}}
    assert ci["permissions"] == {"contents": "read"}
    assert ci["jobs"] == {
        "quality": {
            "permissions": {"contents": "read"},
            "uses": "./.github/workflows/quality.yml",
        }
    }
    assert release["jobs"]["build"] == {
        "permissions": {"contents": "read"},
        "uses": "./.github/workflows/quality.yml",
        "with": {"release": "true"},
    }


def test_quality_workflow_pins_toolchain_and_complete_gate() -> None:
    quality = _workflow("quality.yml")
    commands = "\n".join(_commands(quality))

    assert quality["on"].keys() == {"workflow_call"}
    assert quality["permissions"] == {"contents": "read"}
    assert quality["env"] == {
        "UV_VERSION": "0.9.11",
        "UV_LINUX_X86_64_SHA256": (
            "817c0722b437b4b45b9a7e0231616a09db76bab1b8d178ba7a9680c690db19f0"
        ),
    }
    assert quality["jobs"]["tests"]["strategy"]["matrix"] == {
        "python-version": ["3.11", "3.12", "3.13"]
    }
    assert quality["jobs"]["wheel-resolution"]["strategy"]["matrix"] == {
        "python-version": ["3.11", "3.12", "3.13"]
    }
    assert _step_names(quality, "tests") == [
        "Check out the reviewed revision",
        "Set up Python",
        "Set up pinned uv",
        "Install the locked project",
        "Check the lockfile",
        "Lint",
        "Check formatting",
        "Type-check",
        "Test",
    ]
    static_only = {
        "Check the lockfile",
        "Lint",
        "Check formatting",
        "Type-check",
    }
    for step in quality["jobs"]["tests"]["steps"]:
        expected = (
            "matrix.python-version == '3.12'" if step["name"] in static_only else None
        )
        assert step.get("if") == expected
    for command in (
        "uv sync --locked --all-groups",
        "uv lock --check",
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run mypy",
        "uv run pytest",
    ):
        assert command in commands
    assert commands.count("uv build") == 1
    assert "uv build --clear --no-create-gitignore" in commands
    assert '--release-dist "${GITHUB_WORKSPACE}/dist"' in commands
    assert "git merge-base --is-ancestor" in commands
    assert "scripts/prepare_release.py" in commands


def test_quality_build_gate_order_and_conditions_fail_closed() -> None:
    quality = _workflow("quality.yml")
    build = quality["jobs"]["build"]

    assert build["needs"] == "tests"
    assert build.get("if") is None
    assert _step_names(quality, "build") == [
        "Check out complete reviewed history",
        "Set up Python",
        "Set up pinned uv",
        "Install the locked project",
        "Validate release tag and main ancestry",
        "Validate release version",
        "Build distributions once",
        "Test the exact built distributions",
        "Retain wheel for dependency-resolution matrix",
        "Validate and checksum release artifacts",
        "Retain exact release artifacts",
    ]
    release_only = {
        "Validate release tag and main ancestry",
        "Validate release version",
        "Validate and checksum release artifacts",
        "Retain exact release artifacts",
    }
    for step in build["steps"]:
        expected = "inputs.release" if step["name"] in release_only else None
        assert step.get("if") == expected

    tag_validation = _step(quality, "build", "Validate release tag and main ancestry")[
        "run"
    ]
    assert tag_validation.startswith("set -euo pipefail\n")
    for command in (
        'test "${GITHUB_REF_TYPE}" = "tag"',
        'test "$(git cat-file -t "refs/tags/${RELEASE_TAG}")" = "commit"',
        'test "${tag_commit}" = "${event_commit}"',
        'git merge-base --is-ancestor "${event_commit}" refs/remotes/origin/main',
    ):
        assert command in tag_validation

    for job in ("tests", "build", "wheel-resolution"):
        assert quality["jobs"][job].get("continue-on-error") is None
        for step in quality["jobs"][job]["steps"]:
            assert step.get("continue-on-error") is None


def test_public_wheel_resolution_is_clean_three_version_and_pin_checked() -> None:
    quality = _workflow("quality.yml")
    build_upload = _step(
        quality,
        "build",
        "Retain wheel for dependency-resolution matrix",
    )
    resolution = quality["jobs"]["wheel-resolution"]

    assert build_upload["with"] == {
        "name": "wheel-resolution-${{ github.run_id }}-${{ github.run_attempt }}",
        "path": "dist/*.whl",
        "if-no-files-found": "error",
        "retention-days": "1",
        "compression-level": "0",
        "overwrite": "false",
        "include-hidden-files": "false",
    }
    assert resolution["needs"] == "build"
    assert resolution["strategy"] == {
        "fail-fast": "false",
        "matrix": {"python-version": ["3.11", "3.12", "3.13"]},
    }
    assert _step_names(quality, "wheel-resolution") == [
        "Set up Python",
        "Set up pinned uv without dependency cache",
        "Download the exact wheel under test",
        "Resolve public dependencies in a clean environment",
        "Verify fork provenance and execution handshake",
    ]
    assert _step(
        quality,
        "wheel-resolution",
        "Download the exact wheel under test",
    )["with"] == {
        "name": "wheel-resolution-${{ github.run_id }}-${{ github.run_attempt }}",
        "path": "wheel-under-test",
        "digest-mismatch": "error",
    }
    uv_setup = _step(
        quality,
        "wheel-resolution",
        "Set up pinned uv without dependency cache",
    )
    assert uv_setup["with"]["enable-cache"] == "false"
    install = _step(
        quality,
        "wheel-resolution",
        "Resolve public dependencies in a clean environment",
    )
    assert install["env"] == {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    assert "uv venv --no-project --no-config --no-python-downloads" in install["run"]
    assert (
        "uv pip install --no-cache --no-config --no-python-downloads" in install["run"]
    )
    assert "shopt -s nullglob" in install["run"]
    assert "--offline" not in install["run"]
    handshake = _step(
        quality,
        "wheel-resolution",
        "Verify fork provenance and execution handshake",
    )["run"]
    handshake_source = _python_heredoc(handshake)
    fixture_source = (ROOT / "tests" / "fixtures" / "hermes_plugin_probe.py").read_text(
        encoding="utf-8"
    )
    assert (
        'validate_agent_reach_execution_contract(runtime_module="youtube")'
        in handshake_source
    )
    assert "load_agent_reach_catalog()" in handshake_source
    assert "assert len(catalog.channels) == 15" in handshake_source
    assert 'sys.modules.get("agent_reach.execution.v1.youtube")' in handshake_source
    assert "tuple(signature(execute_youtube).parameters)" in handshake_source
    assert "api.execute(" not in handshake_source
    assert "execute_youtube(" not in handshake_source

    assert (
        _literal_assignment(handshake_source, "capability_fields")
        == EXPECTED_EXECUTION_CAPABILITY_FIELDS
    )
    assert (
        _literal_assignment(handshake_source, "expected_capabilities")
        == EXPECTED_EXECUTION_CAPABILITIES
    )
    assert (
        _literal_assignment(fixture_source, "EXPECTED_EXECUTION_CAPABILITY_FIELDS")
        == EXPECTED_EXECUTION_CAPABILITY_FIELDS
    )
    assert (
        _literal_assignment(fixture_source, "EXPECTED_EXECUTION_CAPABILITIES")
        == EXPECTED_EXECUTION_CAPABILITIES
    )
    assert fixture_source.count("validate_agent_reach_execution_contract(") == 1
    assert 'runtime_module="youtube"' in fixture_source
    assert 'for module_name in ("yt_dlp", "yt_dlp_ejs", "deno")' in fixture_source


def test_build_backend_matches_audited_release_generator() -> None:
    with (ROOT / "pyproject.toml").open("rb") as pyproject_file:
        build_system = tomllib.load(pyproject_file)["build-system"]

    assert build_system["build-backend"] == "setuptools.build_meta"
    assert build_system["requires"] == ["setuptools==81.0.0"]


def test_every_external_action_has_the_reviewed_commit_pin() -> None:
    workflows = tuple(
        _workflow(name) for name in ("ci.yml", "quality.yml", "release.yml")
    )
    actions = _external_actions(*workflows)

    assert actions == EXPECTED_ACTIONS
    assert all(SHA_PIN.fullmatch(action) for action in actions)


def test_release_is_tag_only_least_privilege_and_never_stable() -> None:
    release = _workflow("release.yml")
    commands = "\n".join(_commands(release))

    assert release["on"] == {"push": {"tags": ["v*"]}}
    assert release["permissions"] == {}
    assert release["concurrency"] == {
        "group": "release-${{ github.ref }}",
        "cancel-in-progress": "false",
    }
    assert release["jobs"]["attest"]["permissions"] == {
        "attestations": "write",
        "contents": "read",
        "id-token": "write",
    }
    assert release["jobs"]["release"]["permissions"] == {"contents": "write"}
    assert release["jobs"]["release"]["environment"] == "github-release"
    assert release["jobs"]["attest"]["needs"] == "build"
    assert release["jobs"]["release"]["needs"] == ["build", "attest"]
    assert release["jobs"]["attest"].get("if") is None
    assert release["jobs"]["release"].get("if") is None
    assert _step_names(release, "attest") == [
        "Download exact release artifacts",
        "Verify artifact handoff",
        "Attest wheel and source distribution",
    ]
    assert _step_names(release, "release") == [
        "Download attested release artifacts",
        "Verify artifact handoff",
        "Verify remote tag and main ancestry",
        "Create pre-release from exact artifacts",
    ]
    assert commands.count("sha256sum --check SHA256SUMS") == 2
    assert "--verify-tag" in commands
    assert "--prerelease" in commands
    assert "--latest=false" in commands
    assert "--generate-notes" in commands
    assert "uv build" not in commands
    assert "${{ secrets." not in (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    for job in ("attest", "release"):
        for step in release["jobs"][job]["steps"]:
            assert step.get("if") is None
            assert step.get("continue-on-error") is None

    remote_validation = _step(
        release, "release", "Verify remote tag and main ancestry"
    )["run"]
    assert remote_validation.startswith("set -euo pipefail\n")
    for command in (
        "git/ref/tags/${RELEASE_TAG}",
        'test "${tag_type}" = "commit"',
        'test "${tag_sha}" = "${GITHUB_SHA}"',
        "git/ref/heads/main",
        'test "${main_type}" = "commit"',
        "compare/${GITHUB_SHA}...${main_sha}",
        'case "${comparison_status}" in',
        "ahead|identical)",
    ):
        assert command in remote_validation


def test_release_attests_and_uploads_only_closed_artifacts() -> None:
    quality = _workflow("quality.yml")
    release = _workflow("release.yml")
    build_steps = quality["jobs"]["build"]["steps"]
    upload = next(
        step for step in build_steps if step["name"] == "Retain exact release artifacts"
    )
    attest_steps = release["jobs"]["attest"]["steps"]
    attestation = next(
        step
        for step in attest_steps
        if step["name"] == "Attest wheel and source distribution"
    )
    release_steps = release["jobs"]["release"]["steps"]
    publish = next(
        step
        for step in release_steps
        if step["name"] == "Create pre-release from exact artifacts"
    )

    assert upload["with"] == {
        "name": "${{ steps.artifacts.outputs.artifact_name }}",
        "path": (
            "dist/${{ steps.artifacts.outputs.wheel_name }}\n"
            "dist/${{ steps.artifacts.outputs.sdist_name }}\n"
            "dist/${{ steps.artifacts.outputs.checksum_name }}\n"
        ),
        "if-no-files-found": "error",
        "retention-days": "7",
        "compression-level": "0",
        "overwrite": "false",
        "include-hidden-files": "false",
    }
    assert attestation["with"] == {"subject-checksums": "dist/SHA256SUMS"}
    assert shlex.split(publish["run"]) == [
        "gh",
        "release",
        "create",
        "${RELEASE_TAG}",
        "dist/${WHEEL_NAME}",
        "dist/${SDIST_NAME}",
        "dist/${CHECKSUM_NAME}",
        "--repo",
        "${GITHUB_REPOSITORY}",
        "--verify-tag",
        "--prerelease",
        "--latest=false",
        "--generate-notes",
        "--title",
        "Hermes Reach ${RELEASE_VERSION}",
    ]


def test_release_docs_distinguish_uploaded_assets_from_generated_sources() -> None:
    paths = (
        ROOT / "README.md",
        ROOT / "README_EN.md",
        ROOT / "docs" / "releasing.md",
    )

    documents = {path: path.read_text(encoding="utf-8") for path in paths}
    expected_subjects = {
        ROOT / "README.md": [
            "$RELEASE_DIR/hermes_reach-0.1.0a1-py3-none-any.whl",
            "$RELEASE_DIR/hermes_reach-0.1.0a1.tar.gz",
        ],
        ROOT / "README_EN.md": [
            "$RELEASE_DIR/hermes_reach-0.1.0a1-py3-none-any.whl",
            "$RELEASE_DIR/hermes_reach-0.1.0a1.tar.gz",
        ],
        ROOT / "docs" / "releasing.md": [
            "release-audit/hermes_reach-0.1.0a1-py3-none-any.whl",
            "release-audit/hermes_reach-0.1.0a1.tar.gz",
        ],
    }
    subject_disclaimers = {
        ROOT / "README.md": "`SHA256SUMS` 本身不是 attestation subject",
        ROOT / "README_EN.md": "`SHA256SUMS` itself is not an attestation subject",
        ROOT / "docs" / "releasing.md": (
            "`SHA256SUMS` itself is not an attestation subject"
        ),
    }
    for path, text in documents.items():
        normalized_text = " ".join(text.split())
        assert "`Source code (zip)`" in text
        assert "`Source code (tar.gz)`" in text
        assert "SHA256SUMS" in text
        assert subject_disclaimers[path] in normalized_text
        assert text.count("gh attestation verify") == 2

        attestation_commands = _attestation_commands(text)
        assert attestation_commands == [
            [
                "gh",
                "attestation",
                "verify",
                subject,
                "--repo",
                RELEASE_REPOSITORY,
                "--signer-workflow",
                RELEASE_SIGNER_WORKFLOW,
            ]
            for subject in expected_subjects[path]
        ]
        assert text.rindex("gh attestation verify") < text.index(
            "shasum -a 256 --check SHA256SUMS"
        )

    release_guide = documents[ROOT / "docs" / "releasing.md"]
    assert "installs the exact sdist offline" in release_guide
    assert "full Hermes lifecycle against the exact wheel" in release_guide
    assert "lifecycle-tests one wheel/sdist pair" not in release_guide
    assert "failure can be\nambiguous" in release_guide
    assert "gh release view <tag> --json isDraft,isPrerelease,url" in release_guide
    assert "GitHub has no disabled release state" in release_guide
    assert "treat it as published" in release_guide
    assert "`WITHDRAWN`" in release_guide
    assert "fix forward with a new version and tag" in release_guide
    assert "disable the public release" not in release_guide


def test_release_guide_public_smoke_is_fresh_complete_and_cwd_safe() -> None:
    release_guide = (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
    public_smoke_start = release_guide.index("## Public wheel smoke")
    public_smoke_end = release_guide.index("## Failure and recovery")
    public_smoke = release_guide[public_smoke_start:public_smoke_end]
    install_block = public_smoke.split("```bash", 1)[1].split("```", 1)[0]
    audit_commands = release_guide[:public_smoke_start]

    assert audit_commands.rindex("cd ..") > audit_commands.rindex(
        "shasum -a 256 --check SHA256SUMS"
    )
    assert 'WHEEL="$(pwd)/release-audit/hermes_reach-0.1.0a1-py3-none-any.whl"' in (
        install_block
    )
    assert 'test -f "$WHEEL"' in install_block
    assert "uv venv --no-project --no-config --no-python-downloads" in install_block
    assert "uv pip install \\" in install_block
    assert "--no-cache" in install_block
    assert "--no-config" in install_block
    assert "--no-python-downloads" in install_block
    assert "--offline" not in install_block
    assert "--no-deps" not in install_block
    assert "GIT_CONFIG_GLOBAL=/dev/null" in install_block
    assert "GIT_CONFIG_NOSYSTEM=1" in install_block
    assert "GIT_TERMINAL_PROMPT=0" in install_block
    assert 'test "$HOME" = "$ORIGINAL_HOME"' in install_block

    normalized_smoke = " ".join(public_smoke.split())
    assert "repeat the complete smoke in separate roots for Python 3.12 and 3.13" in (
        normalized_smoke
    )
    assert 'distribution("agent-reach").read_text("direct_url.json")' in public_smoke
    assert "requested_revision" in public_smoke
    assert "commit_id" in public_smoke
    assert "plugins enable reach --no-allow-tool-override" in public_smoke
    assert "https://github.com/izumi0uu/hermes-reach/releases.atom" in public_smoke
    assert "plugins disable reach" in public_smoke
    assert "uv pip uninstall --no-config --no-python-downloads" in public_smoke


def test_release_docs_define_immutable_tag_as_recovery_not_selection() -> None:
    expected = {
        ROOT / "README.md": (
            "该 tag 只用于恢复定位，不是依赖选择器，精确 commit 始终是权威 pin。"
        ),
        ROOT / "README_EN.md": (
            "The tag is only a recovery reference, not a dependency selector; "
            "the exact commit remains authoritative."
        ),
        ROOT / "docs" / "releasing.md": (
            "The tag is only a recovery reference and never the dependency selector; "
            "the commit in wheel metadata remains authoritative."
        ),
    }

    for path, statement in expected.items():
        normalized = " ".join(path.read_text(encoding="utf-8").split())
        assert statement in normalized
