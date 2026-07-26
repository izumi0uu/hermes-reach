from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from agent.secret_sources.base import FetchResult  # type: ignore[import-untyped]

from hermes_reach.connector import bitwarden_helper
from hermes_reach.connector.bitwarden_helper import (
    _assert_compatibility,
    _fetch_selected_secret,
    _HelperRequest,
    _parse_request,
)
from hermes_reach.connector.protocol import canonical_json_bytes

PROJECT_ID = "12345678-1234-5678-9234-567812345678"


def _request(profile: Path, selector: str = "SELECTED_KEY") -> _HelperRequest:
    return _HelperRequest(profile, PROJECT_ID, selector, "")


def _request_bytes(profile: Path, selector: str = "SELECTED_KEY") -> bytes:
    return canonical_json_bytes(
        {
            "profile_home": str(profile),
            "project_id": PROJECT_ID,
            "selector": selector,
            "server_url": "",
            "version": 1,
        }
    )


def _profile(tmp_path: Path, script_body: str) -> Path:
    profile = tmp_path / "connector-profile"
    profile.mkdir(mode=0o700)
    profile.chmod(0o700)
    binary_directory = profile / "bin"
    binary_directory.mkdir(mode=0o700)
    binary_directory.chmod(0o700)
    binary = binary_directory / "bws"
    binary.write_text(f"#!{sys.executable}\n{script_body}", encoding="utf-8")
    binary.chmod(0o700)
    return profile


def _helper_environment(profile: Path) -> dict[str, str]:
    return {
        "BWS_ACCESS_TOKEN": "BOOTSTRAP_TOKEN_CANARY",
        "HOME": str(profile),
        "HERMES_HOME": str(profile),
        "NO_COLOR": "1",
        "PATH": str(profile / "bin"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
    }


def _run_helper(
    profile: Path, *, selector: str = "SELECTED_KEY"
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "hermes_reach.connector.bitwarden_helper",
        ],
        input=_request_bytes(profile, selector),
        capture_output=True,
        env=_helper_environment(profile),
        cwd=profile,
        timeout=10,
        check=False,
    )


def test_helper_request_is_closed_canonical_and_redacted(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    parsed = _parse_request(_request_bytes(profile))
    assert parsed == _request(profile)
    assert repr(parsed) == "_HelperRequest(<redacted>)"
    assert PROJECT_ID not in repr(parsed)

    mappings = (
        {
            "profile_home": str(profile),
            "project_id": PROJECT_ID,
            "selector": "SELECTED_KEY",
            "server_url": "",
            "version": 2,
        },
        {
            "profile_home": str(profile),
            "project_id": PROJECT_ID,
            "selector": "SELECTED_KEY",
            "server_url": "",
            "version": True,
        },
        {
            "profile_home": str(profile),
            "project_id": PROJECT_ID,
            "selector": "SELECTED_KEY",
            "server_url": "",
            "unknown": "field",
            "version": 1,
        },
    )
    for mapping in mappings:
        with pytest.raises(ValueError):
            _parse_request(canonical_json_bytes(mapping))
    with pytest.raises(ValueError):
        _parse_request(b'{"version":1, "selector":"SELECTED_KEY"}')


def test_locked_hermes_bitwarden_compatibility_probe_passes() -> None:
    _assert_compatibility()


def test_same_version_source_drift_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drifted = dict(bitwarden_helper._EXPECTED_SOURCE_HASHES)
    drifted["bitwarden"] = hashlib.sha256(b"post-release-drift").hexdigest()
    monkeypatch.setattr(bitwarden_helper, "_EXPECTED_SOURCE_HASHES", drifted)
    monkeypatch.setattr(
        bitwarden_helper,
        "_load_hermes_runtime",
        lambda: pytest.fail("Hermes source was imported before its hash passed"),
    )
    with pytest.raises(RuntimeError):
        _assert_compatibility()


def test_module_shadowing_fails_before_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shadow_root = tmp_path / "shadow"
    shadow_package = shadow_root / "agent"
    shadow_package.mkdir(parents=True)
    executed = tmp_path / "shadow-executed"
    (shadow_package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(executed)!r}).touch()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(shadow_root))
    monkeypatch.setattr(
        bitwarden_helper,
        "_load_hermes_runtime",
        lambda: pytest.fail("A shadowed Hermes module was imported"),
    )

    with pytest.raises(RuntimeError):
        _assert_compatibility()

    assert not executed.exists()


class _Source:
    def __init__(self, result: FetchResult) -> None:
        self.result = result
        self.calls = 0
        self.config: dict[str, object] | None = None
        self.home: Path | None = None

    def fetch(self, cfg: dict[str, object], home_path: Path) -> FetchResult:
        self.calls += 1
        self.config = cfg
        self.home = home_path
        return self.result


def test_fetch_calls_hermes_once_with_no_cache_or_global_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, "print('[]')\n")
    result = FetchResult(
        secrets={
            "SELECTED_KEY": "SELECTED_SECRET",
            "UNRELATED_KEY": "UNRELATED_SECRET_CANARY",
        },
        binary_path=profile / "bin" / "bws",
    )
    source = _Source(result)
    monkeypatch.setenv("HOME", str(profile))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "BOOTSTRAP_TOKEN")
    monkeypatch.delenv("BWS_SERVER_URL", raising=False)
    before = dict(os.environ)

    material = _fetch_selected_secret(
        _request(profile),
        source_factory=lambda: source,
        compatibility_check=lambda: None,
    )

    assert material == bytearray(b"SELECTED_SECRET")
    assert source.calls == 1
    assert source.home == profile
    assert source.config == {
        "access_token_env": "BWS_ACCESS_TOKEN",
        "auto_install": False,
        "cache_ttl_seconds": 0,
        "enabled": True,
        "encrypted_cache": {"enabled": False, "max_stale_seconds": 0},
        "project_id": PROJECT_ID,
        "server_url": "",
    }
    assert result.secrets == {}
    assert os.environ == before
    assert b"UNRELATED_SECRET_CANARY" not in material


@pytest.mark.parametrize(
    "result",
    [
        FetchResult(secrets={}),
        FetchResult(secrets={"SELECTED_KEY": ""}),
        FetchResult(secrets={"SELECTED_KEY": "value"}, warnings=["NAME_CANARY"]),
        FetchResult(secrets={"SELECTED_KEY": "value"}, error="STDERR_CANARY"),
        FetchResult(secrets={"SELECTED_KEY": "value"}, applied=["SELECTED_KEY"]),
    ],
)
def test_fetch_failure_shapes_never_return_provider_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: FetchResult,
) -> None:
    profile = _profile(tmp_path, "print('[]')\n")
    result.binary_path = profile / "bin" / "bws"
    source = _Source(result)
    monkeypatch.setenv("HOME", str(profile))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.delenv("BWS_SERVER_URL", raising=False)
    with pytest.raises(RuntimeError) as failure:
        _fetch_selected_secret(
            _request(profile),
            source_factory=lambda: source,
            compatibility_check=lambda: None,
        )
    rendered = str(failure.value)
    assert "STDERR_CANARY" not in rendered
    assert "NAME_CANARY" not in rendered


def test_real_helper_returns_only_selected_value_and_writes_no_cache(
    tmp_path: Path,
) -> None:
    script = """
import json
import os
import sys

assert sys.argv[1] == "secret"
assert sys.argv[2] == "list"
assert sys.argv[3] == "12345678-1234-5678-9234-567812345678"
assert sys.argv[4:] == ["--output", "json"]
assert os.environ["BWS_ACCESS_TOKEN"] == "BOOTSTRAP_TOKEN_CANARY"
payload = [
    {"key": "SELECTED_KEY", "value": "SELECTED_SECRET"},
    {"key": "UNRELATED_KEY", "value": "UNRELATED_SECRET_CANARY"},
]
print(json.dumps(payload))
"""
    profile = _profile(tmp_path, script)
    before = dict(os.environ)
    completed = _run_helper(profile)

    assert completed.returncode == 0
    assert completed.stderr == b""
    assert completed.stdout[:1] == b"\x00"
    size = int.from_bytes(completed.stdout[1:5], "big")
    assert completed.stdout[5:] == b"SELECTED_SECRET"
    assert size == len(b"SELECTED_SECRET")
    assert b"UNRELATED_SECRET_CANARY" not in completed.stdout
    assert b"BOOTSTRAP_TOKEN_CANARY" not in completed.stdout
    assert not (profile / "cache" / "bws_cache.json").exists()
    assert os.environ == before


def test_each_helper_invocation_fetches_in_a_fresh_process(tmp_path: Path) -> None:
    script = """
import json
import os
from pathlib import Path

counter = Path(os.environ["HOME"]) / "bws-call-count"
count = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(count))
print(json.dumps([{"key": "SELECTED_KEY", "value": "SELECTED_SECRET"}]))
"""
    profile = _profile(tmp_path, script)

    first = _run_helper(profile)
    second = _run_helper(profile)

    assert first.returncode == second.returncode == 0
    assert first.stdout[5:] == second.stdout[5:] == b"SELECTED_SECRET"
    assert (profile / "bws-call-count").read_text() == "2"
    assert not (profile / "cache" / "bws_cache.json").exists()


def test_real_helper_child_environment_is_minimal(tmp_path: Path) -> None:
    script = """
import json
import os

allowed = ",".join(sorted(os.environ))
print(json.dumps([{"key": "ENV_KEYS", "value": allowed}]))
"""
    profile = _profile(tmp_path, script)
    completed = _run_helper(profile, selector="ENV_KEYS")
    assert completed.returncode == 0
    names = set(completed.stdout[5:].decode().split(","))
    assert names <= {
        "BWS_ACCESS_TOKEN",
        "HOME",
        "HERMES_HOME",
        "LC_CTYPE",
        "NO_COLOR",
        "PATH",
        "PYTHONNOUSERSITE",
        "PYTHONUTF8",
        "__CF_USER_TEXT_ENCODING",
    }
    for forbidden in (
        "AWS_SECRET_ACCESS_KEY",
        "BWS_SERVER_URL",
        "HTTPS_PROXY",
        "OPENAI_API_KEY",
        "PYTHONPATH",
    ):
        assert forbidden not in names


@pytest.mark.parametrize("failure", ["missing", "provider", "malformed"])
def test_real_helper_failures_emit_only_generic_status(
    tmp_path: Path, failure: str
) -> None:
    if failure == "provider":
        script = (
            "import sys\nprint('RAW_STDERR_SECRET', file=sys.stderr)\nsys.exit(7)\n"
        )
    elif failure == "malformed":
        script = "print('RAW_MALFORMED_SECRET')\n"
    else:
        script = "print('[]')\n"
    profile = _profile(tmp_path, script)
    completed = _run_helper(profile)
    assert completed.returncode == 1
    assert completed.stdout == b"\x01"
    assert completed.stderr == b""
    for canary in (b"RAW_STDERR_SECRET", b"RAW_MALFORMED_SECRET", PROJECT_ID.encode()):
        assert canary not in completed.stdout + completed.stderr


def test_helper_source_never_imports_registry_application() -> None:
    source = Path(bitwarden_helper.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "secret_sources.registry",
        "apply_all(",
        "apply_bitwarden_secrets(",
        "os.environ.update",
        "shell=True",
        "from agent.secret_sources",
    ):
        assert forbidden not in source
    mode = stat.S_IMODE(Path(bitwarden_helper.__file__).stat().st_mode)
    assert mode & stat.S_IWUSR


def test_fetch_result_fixture_type_is_exact() -> None:
    runtime = bitwarden_helper._load_hermes_runtime()
    assert runtime.fetch_result_type is FetchResult
    assert type(cast(object, FetchResult())) is FetchResult
