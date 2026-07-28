#!/usr/bin/env python3
"""Validate and describe exact Hermes Reach release artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from typing import BinaryIO, Final

import yaml
from packaging.utils import (
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

PROJECT_NAME: Final = "hermes-reach"
CHECKSUM_FILENAME: Final = "SHA256SUMS"
MAX_CORE_METADATA_BYTES: Final = 1024 * 1024
_OUTPUT_VALUE = re.compile(r"\A[A-Za-z0-9._-]+\Z")


class ReleaseValidationError(ValueError):
    """Raised when local release inputs violate the closed release contract."""


@dataclass(frozen=True)
class ReleaseMetadata:
    version: str
    parsed_version: Version
    tag: str
    wheel_name: str
    sdist_name: str
    artifact_name: str

    def outputs(self) -> dict[str, str]:
        return {
            "artifact_name": self.artifact_name,
            "checksum_name": CHECKSUM_FILENAME,
            "prerelease": "true",
            "sdist_name": self.sdist_name,
            "tag": self.tag,
            "version": self.version,
            "wheel_name": self.wheel_name,
        }


@dataclass(frozen=True)
class ReleaseArtifacts:
    wheel: Path
    sdist: Path
    checksums: Path


def load_release_metadata(root: Path, tag: str) -> ReleaseMetadata:
    project_path = root / "pyproject.toml"
    plugin_path = root / "plugin.yaml"
    try:
        with project_path.open("rb") as project_file:
            project = tomllib.load(project_file)
        plugin = yaml.safe_load(plugin_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, yaml.YAMLError) as exc:
        raise ReleaseValidationError("release metadata is unreadable") from exc

    project_table = project.get("project")
    if not isinstance(project_table, dict):
        raise ReleaseValidationError("pyproject project table is missing")
    project_name = project_table.get("name")
    version_text = project_table.get("version")
    if project_name != PROJECT_NAME or type(version_text) is not str:
        raise ReleaseValidationError("project name or version is invalid")
    if not isinstance(plugin, dict) or type(plugin.get("version")) is not str:
        raise ReleaseValidationError("plugin version is invalid")
    if plugin["version"] != version_text:
        raise ReleaseValidationError("project and plugin versions differ")

    try:
        parsed_version = Version(version_text)
    except InvalidVersion as exc:
        raise ReleaseValidationError("project version is not PEP 440") from exc
    if str(parsed_version) != version_text:
        raise ReleaseValidationError("project version is not canonical PEP 440")
    if parsed_version.pre is None or parsed_version.dev is not None:
        raise ReleaseValidationError("initial public releases must be pre-releases")
    if parsed_version.local is not None or parsed_version.post is not None:
        raise ReleaseValidationError("local and post releases are unsupported")

    expected_tag = f"v{version_text}"
    if tag != expected_tag:
        raise ReleaseValidationError("tag does not match project version")

    distribution_name = canonicalize_name(project_name).replace("-", "_")
    wheel_name = f"{distribution_name}-{version_text}-py3-none-any.whl"
    sdist_name = f"{distribution_name}-{version_text}.tar.gz"
    return ReleaseMetadata(
        version=version_text,
        parsed_version=parsed_version,
        tag=expected_tag,
        wheel_name=wheel_name,
        sdist_name=sdist_name,
        artifact_name=f"{project_name}-{version_text}",
    )


def prepare_artifacts(
    root: Path,
    dist_directory: Path,
    tag: str,
) -> tuple[ReleaseMetadata, ReleaseArtifacts]:
    metadata = load_release_metadata(root, tag)
    _require_directory(dist_directory)
    entries = tuple(sorted(dist_directory.iterdir(), key=lambda path: path.name))
    expected_names = {metadata.wheel_name, metadata.sdist_name}
    if {path.name for path in entries} != expected_names:
        raise ReleaseValidationError(
            "distribution directory must contain only the expected wheel and sdist"
        )
    for path in entries:
        _require_regular_file(path)

    wheel = dist_directory / metadata.wheel_name
    sdist = dist_directory / metadata.sdist_name
    _validate_wheel(wheel, metadata)
    _validate_sdist(sdist, metadata)

    checksums = dist_directory / CHECKSUM_FILENAME
    if checksums.exists() or checksums.is_symlink():
        raise ReleaseValidationError("checksum manifest already exists")
    lines = [
        f"{_sha256(path)}  {path.name}\n"
        for path in sorted((wheel, sdist), key=lambda path: path.name)
    ]
    try:
        with checksums.open("x", encoding="ascii", newline="\n") as checksum_file:
            checksum_file.writelines(lines)
    except OSError as exc:
        raise ReleaseValidationError("checksum manifest cannot be written") from exc
    _require_regular_file(checksums)
    return metadata, ReleaseArtifacts(wheel, sdist, checksums)


def append_github_outputs(path: Path, outputs: dict[str, str]) -> None:
    for key, value in outputs.items():
        if not _OUTPUT_VALUE.fullmatch(key) or not _OUTPUT_VALUE.fullmatch(value):
            raise ReleaseValidationError("GitHub output contains an unsafe value")
    if not path.is_absolute():
        raise ReleaseValidationError("GitHub output path must be absolute")
    try:
        before = path.lstat()
    except OSError as exc:
        raise ReleaseValidationError("GitHub output file is unavailable") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ReleaseValidationError(
            "GitHub output must be an existing regular, non-symlink file"
        )

    flags = os.O_WRONLY | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise ReleaseValidationError(
                "GitHub output must be an existing regular, non-symlink file"
            )
        output_file = os.fdopen(descriptor, "a", encoding="utf-8", newline="\n")
        descriptor = -1
        with output_file:
            output_file.write(
                "".join(f"{key}={outputs[key]}\n" for key in sorted(outputs))
            )
    except ReleaseValidationError:
        raise
    except OSError as exc:
        raise ReleaseValidationError("GitHub output file cannot be written") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _require_directory(path: Path) -> None:
    if not path.is_absolute():
        raise ReleaseValidationError("distribution directory must be absolute")
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ReleaseValidationError("distribution directory is unavailable") from exc
    if path.is_symlink() or not stat.S_ISDIR(mode):
        raise ReleaseValidationError("distribution directory must be a real directory")


def _require_regular_file(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ReleaseValidationError(
            f"release artifact is unavailable: {path.name}"
        ) from exc
    if path.is_symlink() or not stat.S_ISREG(mode):
        raise ReleaseValidationError(
            f"release artifact must be a regular file: {path.name}"
        )


def _validate_wheel(path: Path, metadata: ReleaseMetadata) -> None:
    try:
        name, version, _build, tags = parse_wheel_filename(path.name)
    except ValueError as exc:
        raise ReleaseValidationError("wheel filename is invalid") from exc
    if canonicalize_name(name) != PROJECT_NAME or version != metadata.parsed_version:
        raise ReleaseValidationError("wheel filename metadata differs")
    if {str(tag) for tag in tags} != {"py3-none-any"}:
        raise ReleaseValidationError("wheel must be universal py3-none-any")
    try:
        with zipfile.ZipFile(path) as archive:
            candidates = [
                member
                for member in archive.infolist()
                if member.filename.endswith(".dist-info/METADATA")
            ]
            if len(candidates) != 1:
                raise ReleaseValidationError("wheel must contain one METADATA file")
            member = candidates[0]
            with archive.open(member) as extracted:
                payload = _read_bounded_core_metadata(
                    extracted,
                    member.file_size,
                    "wheel METADATA",
                )
    except (
        OSError,
        KeyError,
        NotImplementedError,
        RuntimeError,
        zipfile.BadZipFile,
    ) as exc:
        raise ReleaseValidationError("wheel cannot be inspected") from exc
    _validate_core_metadata(payload, metadata, "wheel")


def _validate_sdist(path: Path, metadata: ReleaseMetadata) -> None:
    try:
        name, version = parse_sdist_filename(path.name)
    except ValueError as exc:
        raise ReleaseValidationError("sdist filename is invalid") from exc
    if canonicalize_name(name) != PROJECT_NAME or version != metadata.parsed_version:
        raise ReleaseValidationError("sdist filename metadata differs")
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            candidates = [
                member
                for member in archive.getmembers()
                if member.isfile()
                and member.name.count("/") == 1
                and member.name.endswith("/PKG-INFO")
            ]
            if len(candidates) != 1:
                raise ReleaseValidationError(
                    "sdist must contain one top-level PKG-INFO"
                )
            extracted = archive.extractfile(candidates[0])
            if extracted is None:
                raise ReleaseValidationError("sdist PKG-INFO is unreadable")
            payload = _read_bounded_core_metadata(
                extracted,
                candidates[0].size,
                "sdist PKG-INFO",
            )
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseValidationError("sdist cannot be inspected") from exc
    _validate_core_metadata(payload, metadata, "sdist")


def _read_bounded_core_metadata(
    source: BinaryIO,
    declared_size: int,
    label: str,
) -> bytes:
    if declared_size < 0:
        raise ReleaseValidationError(f"{label} has an invalid size")
    if declared_size > MAX_CORE_METADATA_BYTES:
        raise ReleaseValidationError(f"{label} exceeds the 1 MiB limit")

    payload = bytearray()
    remaining = declared_size
    while remaining:
        chunk = source.read(min(64 * 1024, remaining))
        if not chunk or len(chunk) > remaining:
            raise ReleaseValidationError(f"{label} is unreadable")
        payload.extend(chunk)
        remaining -= len(chunk)
    return bytes(payload)


def _validate_core_metadata(
    payload: bytes,
    metadata: ReleaseMetadata,
    artifact: str,
) -> None:
    message = BytesParser(policy=default).parsebytes(payload)
    if (
        canonicalize_name(message.get("Name", "")) != PROJECT_NAME
        or message.get("Version") != metadata.version
    ):
        raise ReleaseValidationError(f"{artifact} core metadata differs")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--github-output", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)

    version = commands.add_parser("version")
    version.add_argument("--tag", required=True)

    artifacts = commands.add_parser("artifacts")
    artifacts.add_argument("--tag", required=True)
    artifacts.add_argument("--dist-dir", type=Path, required=True)
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    try:
        root = args.root.resolve(strict=True)
        if args.command == "version":
            metadata = load_release_metadata(root, args.tag)
        else:
            metadata, _ = prepare_artifacts(root, args.dist_dir, args.tag)
        outputs = metadata.outputs()
        if args.github_output is not None:
            append_github_outputs(args.github_output, outputs)
    except (OSError, ReleaseValidationError) as exc:
        parser.error(str(exc))
    print(json.dumps(outputs, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
