from __future__ import annotations

import ast
import hashlib
import json
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
import yaml

_CONNECTOR_MODULES = frozenset(
    {
        "__init__.py",
        "audit.py",
        "authority.py",
        "bitwarden_helper.py",
        "cli.py",
        "client.py",
        "errors.py",
        "execution.py",
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
    b"XUEQIU_COOKIE_CANARY_4b71f0",
    b"XUEQIU_BWS_PROJECT_CANARY_b951de",
    b"XUEQIU_BWS_SELECTOR_CANARY_22c8a4",
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
_ACCEPTANCE_COMMAND_TIMEOUT_SECONDS = 600.0


def _is_export_call(node: ast.AST) -> TypeGuard[ast.Call]:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "export"
    )


@pytest.fixture(scope="module")
def built_archives(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path]:
    release_dist = request.config.getoption("--release-dist")
    if release_dist is not None:
        return _existing_release_archives(release_dist)

    root = Path(__file__).resolve().parents[1]
    temporary_root = tmp_path_factory.mktemp("connector-release-package")
    source = temporary_root / "source"
    output = temporary_root / "dist"
    source.mkdir()

    for filename in ("LICENSE", "MANIFEST.in", "README.md", "pyproject.toml"):
        shutil.copy2(root / filename, source / filename)
    shutil.copytree(root / "docs", source / "docs")
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
    _run_acceptance_command(
        (
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
        ),
        cwd=source,
        environment=environment,
        phase="package build",
    )

    wheels = tuple(output.glob("*.whl"))
    source_distributions = tuple(output.glob("*.tar.gz"))
    assert len(wheels) == 1
    assert len(source_distributions) == 1
    return wheels[0], source_distributions[0]


def _existing_release_archives(dist_directory: Path) -> tuple[Path, Path]:
    assert dist_directory.is_absolute(), "--release-dist must be an absolute path"
    assert dist_directory.is_dir(), "--release-dist must name a directory"
    assert not dist_directory.is_symlink(), "--release-dist cannot be a symlink"

    wheels = tuple(sorted(dist_directory.glob("*.whl")))
    source_distributions = tuple(sorted(dist_directory.glob("*.tar.gz")))
    assert len(wheels) == 1, "--release-dist must contain exactly one wheel"
    assert len(source_distributions) == 1, (
        "--release-dist must contain exactly one source distribution"
    )
    for archive in (*wheels, *source_distributions):
        assert archive.is_file()
        assert not archive.is_symlink()
    return wheels[0], source_distributions[0]


def _run_acceptance_command(
    command: Collection[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    phase: str = "acceptance",
    expected_returncode: int = 0,
    timeout: float = _ACCEPTANCE_COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            tuple(command),
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as expired:
        stdout = expired.stdout or ""
        stderr = expired.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        raise AssertionError(
            f"{phase} command timed out after {timeout}s: {tuple(command)!r}\n"
            f"stdout:\n{stdout}\n"
            f"stderr:\n{stderr}"
        ) from expired
    assert completed.returncode == expected_returncode, (
        f"{phase} command returned {completed.returncode}, expected "
        f"{expected_returncode}: {tuple(command)!r}\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    return completed


def _virtual_environment_python(environment: Path) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def _virtual_environment_script(environment: Path, name: str) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / f"{name}.exe"
    return environment / "bin" / name


def _required_platform_environment() -> dict[str, str]:
    if os.name != "nt":
        return {}
    required = {
        name: value
        for name in ("SystemRoot", "WINDIR")
        if (value := os.environ.get(name))
    }
    assert "SystemRoot" in required or "WINDIR" in required
    return required


def _temporary_directory_environment(directory: Path) -> dict[str, str]:
    value = str(directory)
    return {"TEMP": value, "TMP": value, "TMPDIR": value}


def _tree_snapshot(
    root: Path,
    *,
    exclude: Collection[Path] = (),
) -> dict[str, tuple[object, ...]]:
    excluded = {path.resolve() for path in exclude}
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in (root, *sorted(root.rglob("*"))):
        if path.resolve() in excluded:
            continue
        relative = "." if path == root else path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[relative] = ("symlink", os.readlink(path))
        elif path.is_file():
            content = path.read_bytes()
            snapshot[relative] = (
                "file",
                len(content),
                hashlib.sha256(content).hexdigest(),
            )
        elif path.is_dir():
            snapshot[relative] = ("directory",)
        else:
            snapshot[relative] = ("other",)
    return snapshot


def _optional_file_bytes(path: Path) -> bytes:
    return path.read_bytes() if path.is_file() else b""


def _assert_plugin_discovery_log_append(
    path: Path,
    before: bytes,
    *,
    enabled: int,
) -> None:
    after = path.read_bytes()
    assert after.startswith(before)
    appended = after[len(before) :].decode("utf-8")
    assert re.fullmatch(
        rf"\d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}}:\d{{2}},\d{{3}} "
        rf"INFO hermes_cli\.plugins: Plugin discovery complete: 1 found, "
        rf"{enabled} enabled\n",
        appended,
    ), f"unexpected plugin discovery log append in {path}:\n{appended!r}"


def _run_json_probe(
    command: Collection[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    phase: str,
) -> object:
    completed = _run_acceptance_command(
        command,
        cwd=cwd,
        environment=environment,
        phase=phase,
    )
    output_lines = [line for line in completed.stdout.splitlines() if line]
    assert len(output_lines) == 1, (
        f"{phase} probe returned unexpected stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    return json.loads(output_lines[0])


def _assert_reach_cli_absent(
    executable: Path,
    *,
    cwd: Path,
    environment: dict[str, str],
    phase: str,
) -> None:
    completed = _run_acceptance_command(
        (str(executable), "reach", "--help"),
        cwd=cwd,
        environment=environment,
        phase=phase,
        expected_returncode=2,
    )
    assert completed.stdout == ""
    assert "invalid choice: 'reach'" in completed.stderr


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        (b"partial stdout", b"partial stderr"),
        ("partial stdout", "partial stderr"),
    ],
)
def test_acceptance_command_timeout_reports_partial_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stdout: str | bytes,
    stderr: str | bytes,
) -> None:
    def raise_timeout(command: tuple[str, ...], **kwargs: object) -> None:
        assert kwargs["timeout"] == _ACCEPTANCE_COMMAND_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(
            command,
            _ACCEPTANCE_COMMAND_TIMEOUT_SECONDS,
            output=stdout,
            stderr=stderr,
        )

    monkeypatch.setattr(subprocess, "run", raise_timeout)

    with pytest.raises(AssertionError) as raised:
        _run_acceptance_command(
            ("hung-command",),
            cwd=tmp_path,
            environment={},
            phase="timeout probe",
        )

    assert str(raised.value) == (
        "timeout probe command timed out after "
        f"{_ACCEPTANCE_COMMAND_TIMEOUT_SECONDS}s: {('hung-command',)!r}\n"
        "stdout:\npartial stdout\n"
        "stderr:\npartial stderr"
    )


def test_temporary_directory_environment_covers_subprocess_conventions(
    tmp_path: Path,
) -> None:
    value = str(tmp_path)
    assert _temporary_directory_environment(tmp_path) == {
        "TEMP": value,
        "TMP": value,
        "TMPDIR": value,
    }


def test_discovery_log_failure_reports_observed_append(tmp_path: Path) -> None:
    path = tmp_path / "agent.log"
    before = b"existing log entry\n"
    appended = "unexpected discovery output\n"
    path.write_bytes(before + appended.encode())

    with pytest.raises(AssertionError) as raised:
        _assert_plugin_discovery_log_append(path, before, enabled=1)

    message = str(raised.value)
    assert f"unexpected plugin discovery log append in {path}" in message
    assert repr(appended) in message
    assert "existing log entry" not in message


def test_built_distributions_install_and_wheel_follows_real_hermes_lifecycle(
    built_archives: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    wheel, source_distribution = built_archives
    virtual_environment = tmp_path / "venv"
    isolated_python = _virtual_environment_python(virtual_environment)
    isolated_hermes = _virtual_environment_script(virtual_environment, "hermes")
    sdist_environment = tmp_path / "sdist-venv"
    sdist_python = _virtual_environment_python(sdist_environment)
    uv = shutil.which("uv")
    assert uv is not None, "Plugin acceptance requires the project's uv tool."

    acceptance_root = tmp_path / "probe-state"
    hermes_home = acceptance_root / "hermes-home"
    hermes_logs = hermes_home / "logs"
    host_log_path = hermes_logs / "agent.log"
    host_error_log_path = hermes_logs / "errors.log"
    bundled_plugins = acceptance_root / "bundled-plugins"
    temporary_directory = acceptance_root / "tmp"
    xdg_config = acceptance_root / "xdg-config"
    xdg_cache = acceptance_root / "xdg-cache"
    xdg_data = acceptance_root / "xdg-data"
    for directory in (
        hermes_home,
        hermes_logs,
        hermes_home / "plugins",
        bundled_plugins,
        temporary_directory,
        xdg_config,
        xdg_cache,
        xdg_data,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    host_log_path.write_bytes(b"")
    host_error_log_path.write_bytes(b"")

    cache_discovery_environment = {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(Path(uv).parent),
        **_temporary_directory_environment(temporary_directory),
    }
    for name in ("HOME", "USERPROFILE", "UV_CACHE_DIR", "XDG_CACHE_HOME"):
        if value := os.environ.get(name):
            cache_discovery_environment[name] = value
    cache_discovery_environment.update(_required_platform_environment())
    cache_directory = Path(
        _run_acceptance_command(
            (uv, "cache", "dir", "--no-config"),
            cwd=tmp_path,
            environment=cache_discovery_environment,
            phase="offline cache discovery",
        ).stdout.strip()
    ).resolve()
    assert cache_directory.is_absolute()

    isolated_environment = {
        "HOME": str(hermes_home),
        "HERMES_BUNDLED_PLUGINS": str(bundled_plugins),
        "HERMES_HOME": str(hermes_home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(isolated_python.parent),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONUTF8": "1",
        "USERPROFILE": str(hermes_home),
        "XDG_CACHE_HOME": str(xdg_cache),
        "XDG_CONFIG_HOME": str(xdg_config),
        "XDG_DATA_HOME": str(xdg_data),
        **_temporary_directory_environment(temporary_directory),
    }
    isolated_environment.update(_required_platform_environment())
    bootstrap_environment = {
        **isolated_environment,
        "PIP_NO_INDEX": "1",
        "UV_CACHE_DIR": str(cache_directory),
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "VIRTUAL_ENV": str(virtual_environment),
    }
    sdist_bootstrap_environment = {
        **bootstrap_environment,
        "PATH": str(sdist_python.parent),
        "VIRTUAL_ENV": str(sdist_environment),
    }
    _run_acceptance_command(
        (
            sys.executable,
            "-m",
            "venv",
            "--without-pip",
            str(sdist_environment),
        ),
        cwd=tmp_path,
        environment=sdist_bootstrap_environment,
        phase="source distribution environment creation",
    )
    assert sdist_python.is_file()

    _run_acceptance_command(
        (
            uv,
            "pip",
            "install",
            "--offline",
            "--no-python-downloads",
            "--no-deps",
            "--python",
            str(sdist_python),
            str(source_distribution),
        ),
        cwd=tmp_path,
        environment=sdist_bootstrap_environment,
        phase="source distribution install",
    )
    sdist_probe = _run_acceptance_command(
        (
            str(sdist_python),
            "-I",
            "-c",
            "from importlib.metadata import distribution; "
            "print(next(ep.value for ep in distribution('hermes-reach').entry_points "
            "if ep.group == 'hermes_agent.plugins' and ep.name == 'reach'))",
        ),
        cwd=tmp_path,
        environment={
            **isolated_environment,
            "PATH": str(sdist_python.parent),
        },
        phase="source distribution entry point",
    )
    assert sdist_probe.stdout == "hermes_reach\n"

    _run_acceptance_command(
        (
            sys.executable,
            "-m",
            "venv",
            "--without-pip",
            str(virtual_environment),
        ),
        cwd=tmp_path,
        environment=bootstrap_environment,
        phase="wheel environment creation",
    )
    assert isolated_python.is_file()

    _run_acceptance_command(
        (
            uv,
            "sync",
            "--active",
            "--locked",
            "--offline",
            "--no-python-downloads",
            "--no-install-project",
            "--no-dev",
            "--no-progress",
        ),
        cwd=root,
        environment=bootstrap_environment,
    )
    _run_acceptance_command(
        (
            uv,
            "pip",
            "install",
            "--offline",
            "--no-python-downloads",
            "--no-deps",
            "--python",
            str(isolated_python),
            str(wheel),
        ),
        cwd=tmp_path,
        environment=bootstrap_environment,
        phase="wheel install",
    )
    _run_acceptance_command(
        (
            uv,
            "pip",
            "check",
            "--offline",
            "--no-python-downloads",
            "--python",
            str(isolated_python),
        ),
        cwd=tmp_path,
        environment=bootstrap_environment,
        phase="installed dependency check",
    )
    assert isolated_hermes.is_file()

    probe_environment = isolated_environment
    source_root = root / "src"
    lifecycle_probe = root / "tests" / "fixtures" / "hermes_plugin_lifecycle_probe.py"
    disabled_summary = {
        "cli_commands": [],
        "distribution": "installed",
        "entry_points": 1,
        "module": "not_imported",
        "plugin_record": "disabled",
        "skill": False,
        "state": "disabled",
        "tools": [],
    }
    assert (
        _run_json_probe(
            (
                str(isolated_python),
                "-I",
                str(lifecycle_probe),
                "disabled",
                str(acceptance_root),
                str(source_root),
            ),
            cwd=acceptance_root,
            environment=probe_environment,
            phase="initial disabled-state",
        )
        == disabled_summary
    )

    config_path = hermes_home / "config.yaml"
    assert not config_path.exists()
    before_initial_cli = _tree_snapshot(
        acceptance_root,
        exclude=(host_log_path,),
    )
    initial_log = _optional_file_bytes(host_log_path)
    _assert_reach_cli_absent(
        isolated_hermes,
        cwd=acceptance_root,
        environment=probe_environment,
        phase="initial disabled CLI",
    )
    assert not config_path.exists()
    assert (
        _tree_snapshot(acceptance_root, exclude=(host_log_path,)) == before_initial_cli
    )
    _assert_plugin_discovery_log_append(host_log_path, initial_log, enabled=0)
    before_enable = _tree_snapshot(acceptance_root, exclude=(config_path,))
    enabled = _run_acceptance_command(
        (
            str(isolated_hermes),
            "plugins",
            "enable",
            "reach",
            "--no-allow-tool-override",
        ),
        cwd=acceptance_root,
        environment=probe_environment,
        phase="plugin enable",
    )
    assert "Plugin reach enabled." in enabled.stdout
    assert "Takes effect on next session." in enabled.stdout
    enabled_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert enabled_config["plugins"]["enabled"] == ["reach"]
    assert enabled_config["plugins"]["disabled"] == []
    assert enabled_config["plugins"]["entries"]["reach"] == {
        "allow_tool_override": False
    }
    assert _tree_snapshot(acceptance_root, exclude=(config_path,)) == before_enable

    before_enabled_cli = _tree_snapshot(
        acceptance_root,
        exclude=(host_log_path,),
    )
    enabled_log = host_log_path.read_bytes()
    enabled_cli = _run_acceptance_command(
        (str(isolated_hermes), "reach", "status", "--json"),
        cwd=acceptance_root,
        environment=probe_environment,
        phase="enabled plugin CLI",
    )
    enabled_cli_status = json.loads(enabled_cli.stdout)
    assert enabled_cli_status["protocol_version"] == "v1"
    assert enabled_cli_status["outcome"] == "ok"
    assert len(enabled_cli_status["data"]["sources"]) == 15
    assert (
        _tree_snapshot(acceptance_root, exclude=(host_log_path,)) == before_enabled_cli
    )
    _assert_plugin_discovery_log_append(host_log_path, enabled_log, enabled=1)

    enabled_probe = root / "tests" / "fixtures" / "hermes_plugin_probe.py"
    assert _run_json_probe(
        (
            str(isolated_python),
            "-I",
            str(enabled_probe),
            str(acceptance_root),
            str(source_root),
        ),
        cwd=acceptance_root,
        environment=probe_environment,
        phase="enabled plugin host",
    ) == {
        "frozen_operations": 11,
        "hermes_agent_version": "0.19.0",
        "hermes_reach_version": "0.1.0a2",
        "plugin_source": "entrypoint",
        "registration_side_effects": [],
        "rss_backend": "feedparser@6.0.12",
        "tools": [
            "reach_browse",
            "reach_read",
            "reach_search",
            "reach_status",
            "reach_transcribe",
        ],
    }

    before_disable = _tree_snapshot(acceptance_root, exclude=(config_path,))
    disabled = _run_acceptance_command(
        (str(isolated_hermes), "plugins", "disable", "reach"),
        cwd=acceptance_root,
        environment=probe_environment,
        phase="plugin disable",
    )
    assert "Plugin reach disabled." in disabled.stdout
    assert "Takes effect on next session." in disabled.stdout
    disabled_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    disabled_config_bytes = config_path.read_bytes()
    assert disabled_config["plugins"]["enabled"] == []
    assert disabled_config["plugins"]["disabled"] == ["reach"]
    assert disabled_config["plugins"]["entries"]["reach"] == {
        "allow_tool_override": False
    }
    assert _tree_snapshot(acceptance_root, exclude=(config_path,)) == before_disable
    assert (
        _run_json_probe(
            (
                str(isolated_python),
                "-I",
                str(lifecycle_probe),
                "disabled",
                str(acceptance_root),
                str(source_root),
            ),
            cwd=acceptance_root,
            environment=probe_environment,
            phase="disabled plugin host",
        )
        == disabled_summary
    )
    before_disabled_cli = _tree_snapshot(
        acceptance_root,
        exclude=(host_log_path,),
    )
    disabled_log = host_log_path.read_bytes()
    _assert_reach_cli_absent(
        isolated_hermes,
        cwd=acceptance_root,
        environment=probe_environment,
        phase="disabled plugin CLI",
    )
    assert (
        _tree_snapshot(acceptance_root, exclude=(host_log_path,)) == before_disabled_cli
    )
    _assert_plugin_discovery_log_append(host_log_path, disabled_log, enabled=0)

    before_uninstall = _tree_snapshot(acceptance_root)
    _run_acceptance_command(
        (
            uv,
            "pip",
            "uninstall",
            "--offline",
            "--no-python-downloads",
            "--python",
            str(isolated_python),
            "hermes-reach",
        ),
        cwd=tmp_path,
        environment=bootstrap_environment,
        phase="wheel uninstall",
    )
    _run_acceptance_command(
        (
            uv,
            "pip",
            "check",
            "--offline",
            "--no-python-downloads",
            "--python",
            str(isolated_python),
        ),
        cwd=tmp_path,
        environment=bootstrap_environment,
        phase="removed dependency check",
    )
    assert _tree_snapshot(acceptance_root) == before_uninstall
    assert _run_json_probe(
        (
            str(isolated_python),
            "-I",
            str(lifecycle_probe),
            "removed",
            str(acceptance_root),
            str(source_root),
        ),
        cwd=acceptance_root,
        environment=probe_environment,
        phase="removed plugin host",
    ) == {
        "cli_commands": [],
        "distribution": "absent",
        "entry_points": 0,
        "module": "absent",
        "plugin_record": "absent",
        "skill": False,
        "state": "removed",
        "tools": [],
    }
    before_removed_cli = _tree_snapshot(acceptance_root)
    _assert_reach_cli_absent(
        isolated_hermes,
        cwd=acceptance_root,
        environment=probe_environment,
        phase="removed plugin CLI",
    )
    assert _tree_snapshot(acceptance_root) == before_removed_cli
    assert config_path.read_bytes() == disabled_config_bytes
    assert host_error_log_path.read_bytes() == b""


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


def test_source_distribution_includes_agent_reach_decision_evidence(
    built_archives: tuple[Path, Path],
) -> None:
    root = Path(__file__).resolve().parents[1]
    _, source_distribution = built_archives
    members = _archive_members(source_distribution)
    suffixes = (
        "docs/agent-reach-reuse-boundary.md",
        "docs/agent-reach-reuse-decisions.json",
        "docs/agent-reach-operation-ledger.json",
        "docs/agent-reach-decisions/web-1.5.0.md",
        "docs/agent-reach-decisions/exa-mcporter-1.5.0.md",
        "docs/agent-reach-decisions/github-gh-2.95.0.md",
        "docs/agent-reach-decisions/rss-feedparser-6.0.12.md",
        "docs/agent-reach-decisions/v2ex-1.5.0.md",
        "docs/agent-reach-decisions/bilibili-cli-0.6.2.md",
        "docs/agent-reach-decisions/youtube-yt-dlp-2026.7.4.md",
    )

    for suffix in suffixes:
        packaged = [
            content
            for member, content in members.items()
            if member == suffix or member.endswith(f"/{suffix}")
        ]
        assert packaged == [(root / suffix).read_bytes()]


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

    (source / "xueqiu-binding-manifest.json").write_bytes(
        b'{"cookie":"'
        + _ARCHIVE_CANARIES[3]
        + b'","project":"'
        + _ARCHIVE_CANARIES[4]
        + b'","selector":"'
        + _ARCHIVE_CANARIES[5]
        + b'"}'
    )


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
