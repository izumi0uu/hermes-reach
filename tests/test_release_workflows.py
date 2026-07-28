from __future__ import annotations

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

EXPECTED_ACTIONS = {
    "actions/attest-build-provenance@0f67c3f4856b2e3261c31976d6725780e5e4c373",
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
    "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
    "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9",
}


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

    for job in ("tests", "build"):
        assert quality["jobs"][job].get("continue-on-error") is None
        for step in quality["jobs"][job]["steps"]:
            assert step.get("continue-on-error") is None


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
    for text in documents.values():
        assert "`Source code (zip)`" in text
        assert "`Source code (tar.gz)`" in text
        assert "SHA256SUMS" in text

    release_guide = documents[ROOT / "docs" / "releasing.md"]
    assert "failure can be\nambiguous" in release_guide
    assert "gh release view <tag> --json isDraft,isPrerelease,url" in release_guide
    assert "GitHub has no disabled release state" in release_guide
    assert "treat it as published" in release_guide
    assert "`WITHDRAWN`" in release_guide
    assert "fix forward with a new version and tag" in release_guide
    assert "disable the public release" not in release_guide
