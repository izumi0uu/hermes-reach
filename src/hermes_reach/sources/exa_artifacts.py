"""I/O-free operator attestation for the fixed Exa mcporter artifact bundle."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

EXA_NODE_EXECUTABLE_ENVIRONMENT: Final = "HERMES_REACH_EXA_NODE_EXECUTABLE"
EXA_NODE_SHA256_ENVIRONMENT: Final = "HERMES_REACH_EXA_NODE_SHA256"
EXA_MCPORTER_ROOT_ENVIRONMENT: Final = "HERMES_REACH_EXA_MCPORTER_ROOT"
EXA_MCPORTER_CLI_ENVIRONMENT: Final = "HERMES_REACH_EXA_MCPORTER_CLI"
EXA_MCPORTER_TREE_SHA256_ENVIRONMENT: Final = "HERMES_REACH_EXA_MCPORTER_TREE_SHA256"
EXA_CONFIG_PATH_ENVIRONMENT: Final = "HERMES_REACH_EXA_CONFIG_PATH"
EXA_CONFIG_SHA256_ENVIRONMENT: Final = "HERMES_REACH_EXA_CONFIG_SHA256"

_SHA256: Final = re.compile(r"[0-9a-f]{64}")
_MAX_PATH_CHARACTERS: Final = 8_192
_ENVIRONMENT_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("node_executable", EXA_NODE_EXECUTABLE_ENVIRONMENT),
    ("node_sha256", EXA_NODE_SHA256_ENVIRONMENT),
    ("mcporter_root", EXA_MCPORTER_ROOT_ENVIRONMENT),
    ("mcporter_cli", EXA_MCPORTER_CLI_ENVIRONMENT),
    ("mcporter_tree_sha256", EXA_MCPORTER_TREE_SHA256_ENVIRONMENT),
    ("config_path", EXA_CONFIG_PATH_ENVIRONMENT),
    ("config_sha256", EXA_CONFIG_SHA256_ENVIRONMENT),
)


@dataclass(frozen=True, slots=True)
class ExaArtifactAttestation:
    """Closed process-local identity passed only to the isolated Exa worker."""

    node_executable: Path
    node_sha256: str
    mcporter_root: Path
    mcporter_cli: Path
    mcporter_tree_sha256: str
    config_path: Path
    config_sha256: str

    def __post_init__(self) -> None:
        paths = (
            self.node_executable,
            self.mcporter_root,
            self.mcporter_cli,
            self.config_path,
        )
        digests = (
            self.node_sha256,
            self.mcporter_tree_sha256,
            self.config_sha256,
        )
        if (
            any(not _valid_path(path) for path in paths)
            or any(
                type(value) is not str or _SHA256.fullmatch(value) is None
                for value in digests
            )
            or self.mcporter_cli == self.mcporter_root
            or not self.mcporter_cli.is_relative_to(self.mcporter_root)
        ):
            raise ValueError("The Exa artifact attestation is invalid.")

    def frame_fields(self) -> dict[str, str]:
        """Return the exact internal worker fields without probing artifacts."""

        return {
            "node_executable": str(self.node_executable),
            "node_sha256": self.node_sha256,
            "mcporter_root": str(self.mcporter_root),
            "mcporter_cli": str(self.mcporter_cli),
            "mcporter_tree_sha256": self.mcporter_tree_sha256,
            "config_path": str(self.config_path),
            "config_sha256": self.config_sha256,
        }


def exa_artifacts_from_environment(
    environment: Mapping[str, str],
) -> ExaArtifactAttestation | None:
    """Decode only the explicit complete Exa attestation; never discover files."""

    try:
        values = {field: environment.get(name) for field, name in _ENVIRONMENT_FIELDS}
    except Exception:
        raise ValueError("The Exa artifact environment is invalid.") from None
    present = tuple(value is not None for value in values.values())
    if not any(present):
        return None
    if not all(present) or any(
        type(value) is not str or not value for value in values.values()
    ):
        raise ValueError("The Exa artifact environment is incomplete.")
    complete = {name: cast(str, value) for name, value in values.items()}
    try:
        return ExaArtifactAttestation(
            node_executable=Path(complete["node_executable"]),
            node_sha256=complete["node_sha256"],
            mcporter_root=Path(complete["mcporter_root"]),
            mcporter_cli=Path(complete["mcporter_cli"]),
            mcporter_tree_sha256=complete["mcporter_tree_sha256"],
            config_path=Path(complete["config_path"]),
            config_sha256=complete["config_sha256"],
        )
    except (TypeError, ValueError):
        raise ValueError("The Exa artifact environment is invalid.") from None


def _valid_path(value: object) -> bool:
    if not isinstance(value, Path) or not value.is_absolute():
        return False
    rendered = str(value)
    return bool(
        0 < len(rendered) <= _MAX_PATH_CHARACTERS
        and rendered.isprintable()
        and "\x00" not in rendered
        and ".." not in value.parts
    )


__all__ = [
    "EXA_CONFIG_PATH_ENVIRONMENT",
    "EXA_CONFIG_SHA256_ENVIRONMENT",
    "EXA_MCPORTER_CLI_ENVIRONMENT",
    "EXA_MCPORTER_ROOT_ENVIRONMENT",
    "EXA_MCPORTER_TREE_SHA256_ENVIRONMENT",
    "EXA_NODE_EXECUTABLE_ENVIRONMENT",
    "EXA_NODE_SHA256_ENVIRONMENT",
    "ExaArtifactAttestation",
    "exa_artifacts_from_environment",
]
