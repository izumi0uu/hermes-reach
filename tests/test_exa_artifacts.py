from __future__ import annotations

import builtins
from collections.abc import Callable
from pathlib import Path

import pytest

from hermes_reach.sources.exa_artifacts import (
    EXA_CONFIG_PATH_ENVIRONMENT,
    EXA_CONFIG_SHA256_ENVIRONMENT,
    EXA_MCPORTER_CLI_ENVIRONMENT,
    EXA_MCPORTER_ROOT_ENVIRONMENT,
    EXA_MCPORTER_TREE_SHA256_ENVIRONMENT,
    EXA_NODE_EXECUTABLE_ENVIRONMENT,
    EXA_NODE_SHA256_ENVIRONMENT,
    ExaArtifactAttestation,
    exa_artifacts_from_environment,
)

_DIGEST = "a" * 64


def _environment() -> dict[str, str]:
    return {
        EXA_NODE_EXECUTABLE_ENVIRONMENT: "/opt/hermes-reach/exa/bin/node",
        EXA_NODE_SHA256_ENVIRONMENT: _DIGEST,
        EXA_MCPORTER_ROOT_ENVIRONMENT: "/opt/hermes-reach/exa/mcporter",
        EXA_MCPORTER_CLI_ENVIRONMENT: ("/opt/hermes-reach/exa/mcporter/dist/cli.js"),
        EXA_MCPORTER_TREE_SHA256_ENVIRONMENT: "b" * 64,
        EXA_CONFIG_PATH_ENVIRONMENT: "/opt/hermes-reach/exa/config.json",
        EXA_CONFIG_SHA256_ENVIRONMENT: "c" * 64,
    }


def test_complete_exa_artifact_environment_is_decoded_without_file_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_io(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("artifact environment attempted file I/O")

    monkeypatch.setattr(builtins, "open", unexpected_io)
    monkeypatch.setattr(Path, "open", unexpected_io)
    monkeypatch.setattr(Path, "stat", unexpected_io)
    monkeypatch.setattr(Path, "lstat", unexpected_io)

    attestation = exa_artifacts_from_environment(_environment())

    assert attestation == ExaArtifactAttestation(
        Path("/opt/hermes-reach/exa/bin/node"),
        _DIGEST,
        Path("/opt/hermes-reach/exa/mcporter"),
        Path("/opt/hermes-reach/exa/mcporter/dist/cli.js"),
        "b" * 64,
        Path("/opt/hermes-reach/exa/config.json"),
        "c" * 64,
    )
    assert attestation.frame_fields()["mcporter_cli"].endswith("/dist/cli.js")


def test_absent_exa_artifact_environment_is_not_setup() -> None:
    assert exa_artifacts_from_environment({}) is None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: {
            name: item
            for name, item in value.items()
            if name != EXA_NODE_SHA256_ENVIRONMENT
        },
        lambda value: {**value, EXA_NODE_SHA256_ENVIRONMENT: "A" * 64},
        lambda value: {**value, EXA_NODE_EXECUTABLE_ENVIRONMENT: "relative/node"},
        lambda value: {**value, EXA_CONFIG_PATH_ENVIRONMENT: "/opt/../private/config"},
        lambda value: {**value, EXA_MCPORTER_CLI_ENVIRONMENT: "/other/cli.js"},
    ],
)
def test_exa_artifact_environment_fails_closed_on_incomplete_or_unsafe_values(
    mutation: Callable[[dict[str, str]], dict[str, str]],
) -> None:
    with pytest.raises(ValueError, match="Exa artifact"):
        exa_artifacts_from_environment(mutation(_environment()))
