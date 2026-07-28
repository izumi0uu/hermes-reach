from __future__ import annotations

import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.prepare_release import (
    CHECKSUM_FILENAME,
    MAX_CORE_METADATA_BYTES,
    ReleaseValidationError,
    append_github_outputs,
    load_release_metadata,
    prepare_artifacts,
)


def _write_metadata(root: Path, version: str = "0.1.0a0") -> None:
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "hermes-reach"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (root / "plugin.yaml").write_text(
        f"name: reach\nversion: {version}\n",
        encoding="utf-8",
    )


def _core_metadata(version: str = "0.1.0a0") -> bytes:
    return (
        f"Metadata-Version: 2.4\nName: hermes-reach\nVersion: {version}\n\n"
    ).encode("ascii")


def _write_artifacts(
    root: Path,
    version: str = "0.1.0a0",
    *,
    wheel_metadata: bytes | None = None,
    sdist_metadata: bytes | None = None,
) -> Path:
    dist = root / "dist"
    dist.mkdir()
    wheel = dist / f"hermes_reach-{version}-py3-none-any.whl"
    wheel_payload = wheel_metadata or _core_metadata(version)
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr(
            f"hermes_reach-{version}.dist-info/METADATA",
            wheel_payload,
        )

    sdist = dist / f"hermes_reach-{version}.tar.gz"
    sdist_payload = sdist_metadata or _core_metadata(version)
    with tarfile.open(sdist, mode="w:gz") as archive:
        member = tarfile.TarInfo(f"hermes_reach-{version}/PKG-INFO")
        member.size = len(sdist_payload)
        archive.addfile(member, io.BytesIO(sdist_payload))
    return dist


def _metadata_with_size(size: int) -> bytes:
    payload = _core_metadata()
    assert len(payload) <= size
    return payload + b"x" * (size - len(payload))


def test_release_metadata_requires_matching_canonical_prerelease(
    tmp_path: Path,
) -> None:
    _write_metadata(tmp_path)

    metadata = load_release_metadata(tmp_path, "v0.1.0a0")

    assert metadata.outputs() == {
        "artifact_name": "hermes-reach-0.1.0a0",
        "checksum_name": "SHA256SUMS",
        "prerelease": "true",
        "sdist_name": "hermes_reach-0.1.0a0.tar.gz",
        "tag": "v0.1.0a0",
        "version": "0.1.0a0",
        "wheel_name": "hermes_reach-0.1.0a0-py3-none-any.whl",
    }


@pytest.mark.parametrize(
    ("project_version", "plugin_version", "tag"),
    [
        ("0.1.0a0", "0.1.0a0", "v0.1.0a1"),
        ("0.1.0a0", "0.1.0a1", "v0.1.0a0"),
        ("0.1.0", "0.1.0", "v0.1.0"),
        ("0.1.0.dev1", "0.1.0.dev1", "v0.1.0.dev1"),
        ("0.1.0a0+local", "0.1.0a0+local", "v0.1.0a0+local"),
    ],
)
def test_release_metadata_rejects_unsupported_versions(
    tmp_path: Path,
    project_version: str,
    plugin_version: str,
    tag: str,
) -> None:
    _write_metadata(tmp_path, project_version)
    (tmp_path / "plugin.yaml").write_text(
        f"name: reach\nversion: {plugin_version}\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseValidationError):
        load_release_metadata(tmp_path, tag)


@pytest.mark.parametrize(
    "version",
    ["0.1.0-alpha0", "0.1.0A0", "0.1.0a00"],
)
def test_release_metadata_rejects_noncanonical_pep440_versions(
    tmp_path: Path,
    version: str,
) -> None:
    _write_metadata(tmp_path, version)

    with pytest.raises(ReleaseValidationError, match="not canonical PEP 440"):
        load_release_metadata(tmp_path, f"v{version}")


def test_prepare_artifacts_validates_metadata_and_writes_checksums(
    tmp_path: Path,
) -> None:
    _write_metadata(tmp_path)
    dist = _write_artifacts(tmp_path)

    metadata, artifacts = prepare_artifacts(tmp_path, dist.resolve(), "v0.1.0a0")

    assert metadata.version == "0.1.0a0"
    assert artifacts.checksums.name == CHECKSUM_FILENAME
    expected = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted((artifacts.wheel, artifacts.sdist), key=lambda p: p.name)
    )
    assert artifacts.checksums.read_text(encoding="ascii") == expected


@pytest.mark.parametrize(
    "missing_name",
    [
        "hermes_reach-0.1.0a0-py3-none-any.whl",
        "hermes_reach-0.1.0a0.tar.gz",
    ],
)
def test_prepare_artifacts_rejects_missing_artifact(
    tmp_path: Path,
    missing_name: str,
) -> None:
    _write_metadata(tmp_path)
    dist = _write_artifacts(tmp_path)
    (dist / missing_name).unlink()

    with pytest.raises(ReleaseValidationError, match="only the expected"):
        prepare_artifacts(tmp_path, dist.resolve(), "v0.1.0a0")


@pytest.mark.parametrize(
    ("source_name", "duplicate_name"),
    [
        (
            "hermes_reach-0.1.0a0-py3-none-any.whl",
            "hermes_reach-0.1.0a1-py3-none-any.whl",
        ),
        (
            "hermes_reach-0.1.0a0.tar.gz",
            "hermes_reach-0.1.0a1.tar.gz",
        ),
    ],
)
def test_prepare_artifacts_rejects_multiple_artifacts(
    tmp_path: Path,
    source_name: str,
    duplicate_name: str,
) -> None:
    _write_metadata(tmp_path)
    dist = _write_artifacts(tmp_path)
    (dist / duplicate_name).write_bytes((dist / source_name).read_bytes())

    with pytest.raises(ReleaseValidationError, match="only the expected"):
        prepare_artifacts(tmp_path, dist.resolve(), "v0.1.0a0")


@pytest.mark.parametrize("extra_name", ["unexpected.txt", "SHA256SUMS"])
def test_prepare_artifacts_rejects_unexpected_files(
    tmp_path: Path,
    extra_name: str,
) -> None:
    _write_metadata(tmp_path)
    dist = _write_artifacts(tmp_path)
    (dist / extra_name).write_text("unexpected", encoding="ascii")

    with pytest.raises(ReleaseValidationError):
        prepare_artifacts(tmp_path, dist.resolve(), "v0.1.0a0")


def test_prepare_artifacts_rejects_mismatched_core_metadata(tmp_path: Path) -> None:
    _write_metadata(tmp_path)
    dist = _write_artifacts(tmp_path)
    wheel = dist / "hermes_reach-0.1.0a0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr(
            "hermes_reach-0.1.0a0.dist-info/METADATA",
            _core_metadata("0.1.0a1"),
        )

    with pytest.raises(ReleaseValidationError, match="wheel core metadata differs"):
        prepare_artifacts(tmp_path, dist.resolve(), "v0.1.0a0")


def test_prepare_artifacts_accepts_core_metadata_at_one_mib_limit(
    tmp_path: Path,
) -> None:
    _write_metadata(tmp_path)
    payload = _metadata_with_size(MAX_CORE_METADATA_BYTES)
    dist = _write_artifacts(
        tmp_path,
        wheel_metadata=payload,
        sdist_metadata=payload,
    )

    metadata, _ = prepare_artifacts(tmp_path, dist.resolve(), "v0.1.0a0")

    assert metadata.version == "0.1.0a0"


@pytest.mark.parametrize(
    ("artifact", "error"),
    [
        ("wheel", "wheel METADATA exceeds the 1 MiB limit"),
        ("sdist", "sdist PKG-INFO exceeds the 1 MiB limit"),
    ],
)
def test_prepare_artifacts_rejects_oversized_core_metadata(
    tmp_path: Path,
    artifact: str,
    error: str,
) -> None:
    _write_metadata(tmp_path)
    payload = _metadata_with_size(MAX_CORE_METADATA_BYTES + 1)
    if artifact == "wheel":
        dist = _write_artifacts(tmp_path, wheel_metadata=payload)
    else:
        dist = _write_artifacts(tmp_path, sdist_metadata=payload)

    with pytest.raises(ReleaseValidationError, match=error):
        prepare_artifacts(tmp_path, dist.resolve(), "v0.1.0a0")


def test_prepare_artifacts_rejects_symlinked_artifact(tmp_path: Path) -> None:
    _write_metadata(tmp_path)
    dist = _write_artifacts(tmp_path)
    wheel = dist / "hermes_reach-0.1.0a0-py3-none-any.whl"
    target = tmp_path / "wheel-target"
    wheel.replace(target)
    try:
        wheel.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ReleaseValidationError, match="regular file"):
        prepare_artifacts(tmp_path, dist.resolve(), "v0.1.0a0")


def test_github_outputs_are_sorted_and_single_line(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    output.write_text("existing=value\n", encoding="utf-8")

    append_github_outputs(output, {"version": "0.1.0a0", "tag": "v0.1.0a0"})

    assert output.read_text(encoding="utf-8") == (
        "existing=value\ntag=v0.1.0a0\nversion=0.1.0a0\n"
    )


def test_github_outputs_reject_multiline_values(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    output.write_text("", encoding="utf-8")

    with pytest.raises(ReleaseValidationError, match="unsafe value"):
        append_github_outputs(output, {"version": "0.1.0a0\nmalicious=true"})

    assert output.read_text(encoding="utf-8") == ""


def test_github_outputs_reject_relative_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    output = Path("github-output")
    output.write_text("existing=value\n", encoding="utf-8")

    with pytest.raises(ReleaseValidationError, match="must be absolute"):
        append_github_outputs(output, {"version": "0.1.0a0"})

    assert output.read_text(encoding="utf-8") == "existing=value\n"


def test_github_outputs_reject_missing_path(tmp_path: Path) -> None:
    output = tmp_path / "missing-github-output"

    with pytest.raises(ReleaseValidationError, match="is unavailable"):
        append_github_outputs(output, {"version": "0.1.0a0"})

    assert not output.exists()


def test_github_outputs_reject_directory(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    output.mkdir()

    with pytest.raises(ReleaseValidationError, match="regular, non-symlink"):
        append_github_outputs(output, {"version": "0.1.0a0"})


def test_github_outputs_reject_symlink(tmp_path: Path) -> None:
    target = tmp_path / "github-output-target"
    target.write_text("existing=value\n", encoding="utf-8")
    output = tmp_path / "github-output"
    try:
        output.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ReleaseValidationError, match="regular, non-symlink"):
        append_github_outputs(output, {"version": "0.1.0a0"})

    assert target.read_text(encoding="utf-8") == "existing=value\n"
