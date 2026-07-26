from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Collection
from pathlib import Path, PurePosixPath
from typing import TypeGuard

import pytest

_CONNECTOR_MODULES = frozenset(
    {
        "__init__.py",
        "audit.py",
        "authority.py",
        "bitwarden_helper.py",
        "cli.py",
        "client.py",
        "errors.py",
        "identity.py",
        "limits.py",
        "media_policy.py",
        "protocol.py",
        "secrets.py",
        "service.py",
        "store.py",
        "tls.py",
        "transport.py",
    }
)
_ARCHIVE_CANARIES = (
    b"CONNECTOR_RELEASE_SECRET_CANARY_7f8b1d",
    b"CONNECTOR_RELEASE_PATH_CANARY_c02e91",
    b"CONNECTOR_RELEASE_QUERY_CANARY_45a63c",
)
_RUNTIME_BASENAMES = frozenset(
    {
        "bws_cache.enc.json",
        "bws_cache.json",
        "connector-audit.jsonl",
        "connector-authority.sqlite3",
        "connector-authority.sqlite3-shm",
        "connector-authority.sqlite3-wal",
        "connector-identity.pem",
        "connector-receipts.jsonl",
        "connector-runtime.json",
        "connector-snapshot.json",
        "vps-connector-snapshot.json",
        "vps-identity.pem",
        "vps-profile.json",
    }
)
_SENSITIVE_FILE_SUFFIXES = (
    ".cer",
    ".crt",
    ".db",
    ".key",
    ".p12",
    ".pem",
    ".pfx",
    ".sqlite",
    ".sqlite3",
    "-shm",
    "-wal",
)
_TELEMETRY_IMPORTS = frozenset(
    {
        "apscheduler",
        "datadog",
        "newrelic",
        "opentelemetry",
        "posthog",
        "prometheus_client",
        "schedule",
        "sentry_sdk",
        "statsd",
    }
)
_URL = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)


def _is_export_call(node: ast.AST) -> TypeGuard[ast.Call]:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "export"
    )


@pytest.fixture(scope="module")
def built_archives(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[1]
    temporary_root = tmp_path_factory.mktemp("connector-release-package")
    source = temporary_root / "source"
    output = temporary_root / "dist"
    source.mkdir()

    for filename in ("LICENSE", "MANIFEST.in", "README.md", "pyproject.toml"):
        shutil.copy2(root / filename, source / filename)
    (source / "docs").mkdir()
    shutil.copy2(
        root / "docs" / "connector-security.md",
        source / "docs" / "connector-security.md",
    )
    shutil.copytree(
        root / "src",
        source / "src",
        ignore=shutil.ignore_patterns("*.egg-info", "*.pyc", "__pycache__"),
    )
    _add_forbidden_build_inputs(source)

    uv = shutil.which("uv")
    assert uv is not None, "Package acceptance requires the project's uv tool."
    environment = os.environ.copy()
    environment.update({"PIP_NO_INDEX": "1", "UV_OFFLINE": "1"})
    completed = subprocess.run(
        [
            uv,
            "build",
            "--offline",
            "--no-python-downloads",
            "--no-create-gitignore",
            "--out-dir",
            str(output),
            "--python",
            sys.executable,
            str(source),
        ],
        cwd=source,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    wheels = tuple(output.glob("*.whl"))
    source_distributions = tuple(output.glob("*.tar.gz"))
    assert len(wheels) == 1
    assert len(source_distributions) == 1
    return wheels[0], source_distributions[0]


def test_built_distributions_include_every_connector_module(
    built_archives: tuple[Path, Path],
) -> None:
    for archive_path in built_archives:
        members = _archive_members(archive_path)
        for module in _CONNECTOR_MODULES:
            expected = f"hermes_reach/connector/{module}"
            assert _contains_path(members, expected), (
                f"{archive_path.name} does not contain {expected}"
            )


def test_source_distribution_includes_exact_operator_security_guide(
    built_archives: tuple[Path, Path],
) -> None:
    root = Path(__file__).resolve().parents[1]
    _, source_distribution = built_archives
    members = _archive_members(source_distribution)
    suffix = "docs/connector-security.md"
    packaged_guides = [
        content
        for member, content in members.items()
        if member == suffix or member.endswith(f"/{suffix}")
    ]

    assert packaged_guides == [(root / suffix).read_bytes()]


def test_built_distributions_exclude_runtime_state_and_test_secrets(
    built_archives: tuple[Path, Path],
) -> None:
    for archive_path in built_archives:
        members = _archive_members(archive_path)
        for member in members:
            path = PurePosixPath(member)
            lowered = member.lower()
            basename = path.name.lower()
            lowered_parts = {part.lower() for part in path.parts}
            assert "tests" not in lowered_parts
            assert "fixtures" not in lowered_parts
            assert not basename.startswith("test_")
            assert not basename.endswith("_test.py")
            assert basename not in _RUNTIME_BASENAMES
            assert not basename.endswith(_SENSITIVE_FILE_SUFFIXES)
            if basename.endswith((".json", ".jsonl", ".ledger", ".log")):
                assert not any(
                    marker in basename
                    for marker in ("audit", "profile", "receipt", "runtime", "snapshot")
                )
            assert "private-key" not in lowered
            assert "private_key" not in lowered
            assert "leaf-key" not in lowered
            assert "leaf_key" not in lowered
            assert "launchd" not in lowered
            assert "systemd" not in lowered
            assert not lowered.endswith((".plist", ".service"))

        skill_files = {
            member for member in members if PurePosixPath(member).name == "SKILL.md"
        }
        assert len(skill_files) == 1
        assert _contains_path(skill_files, "hermes_reach/skill/SKILL.md")
        assert not any(
            {part.lower() for part in PurePosixPath(member).parts}
            & {"agent-reach", "agent_reach"}
            for member in members
        )

        archive_content = b"\n".join(members.values())
        for canary in _ARCHIVE_CANARIES:
            assert canary not in archive_content
        assert b"class _ReleaseFixtureExecutor" not in archive_content


def test_production_has_no_automatic_telemetry_or_background_service() -> None:
    root = Path(__file__).resolve().parents[1]
    production_files = tuple(sorted((root / "src" / "hermes_reach").rglob("*.py")))
    imported_roots: set[str] = set()
    implicit_exports: list[tuple[Path, int]] = []

    for source_path in production_files:
        source = source_path.read_text(encoding="utf-8")
        for endpoint in _URL.findall(source):
            assert "hermes-reach" not in endpoint.lower()
        lowered = source.lower()
        assert "launchctl" not in lowered
        assert "systemctl" not in lowered

        tree = ast.parse(source, filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(
                    alias.name.partition(".")[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.partition(".")[0])
            if _is_export_call(node):
                implicit_exports.append((source_path, node.lineno))

    assert imported_roots.isdisjoint(_TELEMETRY_IMPORTS)
    assert implicit_exports == []

    ledger_path = root / "src" / "hermes_reach" / "audit" / "ledger.py"
    ledger_source = ledger_path.read_text(encoding="utf-8")
    assert "class ImmutableAuditExporter(Protocol):" in ledger_source


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("audit_sink.export(records)", True),
        ("get_sink().export(records)", True),
        ("audit_sink.export", False),
        ("audit_sink.write(records)", False),
    ],
)
def test_export_call_detection_is_receiver_independent(
    source: str, expected: bool
) -> None:
    tree = ast.parse(source)

    assert any(_is_export_call(node) for node in ast.walk(tree)) is expected


def _add_forbidden_build_inputs(source: Path) -> None:
    tests = source / "tests" / "fixtures"
    tests.mkdir(parents=True)
    (tests / "test_release_fixture_executor.py").write_bytes(
        b"class _ReleaseFixtureExecutor:\n"
        b'    secret = b"' + _ARCHIVE_CANARIES[0] + b'"\n'
        b'    path = b"' + _ARCHIVE_CANARIES[1] + b'"\n'
        b'    query = b"' + _ARCHIVE_CANARIES[2] + b'"\n'
    )

    generated_files = {
        "connector-identity.pem": b"not-a-real-private-key",
        "connector-ca.pem": b"not-a-real-certificate",
        "connector-leaf.key": b"not-a-real-leaf-key",
        "connector-authority.sqlite3": b"not-a-real-database",
        "bws_cache.json": _ARCHIVE_CANARIES[0],
        "connector-audit.jsonl": _ARCHIVE_CANARIES[2],
        "vps-connector-snapshot.json": _ARCHIVE_CANARIES[1],
    }
    for filename, content in generated_files.items():
        (source / filename).write_bytes(content)
        (source / "src" / "hermes_reach" / filename).write_bytes(content)


def _archive_members(archive_path: Path) -> dict[str, bytes]:
    if archive_path.suffix == ".whl":
        with zipfile.ZipFile(archive_path) as archive:
            return {
                name: archive.read(name)
                for name in archive.namelist()
                if not name.endswith("/")
            }

    with tarfile.open(archive_path, mode="r:gz") as archive:
        members: dict[str, bytes] = {}
        for member in archive.getmembers():
            if not member.isfile():
                continue
            extracted = archive.extractfile(member)
            assert extracted is not None
            members[member.name] = extracted.read()
        return members


def _contains_path(members: Collection[str], suffix: str) -> bool:
    return any(member == suffix or member.endswith(f"/{suffix}") for member in members)
