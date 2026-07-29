from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from hermes_reach.connector.errors import (
    ConnectorError,
    ConnectorErrorCategory,
    ConnectorErrorCode,
    category_for_code,
    codes_for_category,
)
from hermes_reach.connector.limits import (
    AUDIT_RETENTION_SECONDS,
    CONNECTOR_PROTOCOL_VERSION,
    CONNECTOR_STORAGE_SCHEMA_VERSION,
    DEFAULT_FILE_GRANT_TTL_SECONDS,
    DEFAULT_GRANT_TTL_SECONDS,
    DEFAULT_GRANT_USES,
    ID_BASE32_LENGTH,
    ID_ENTROPY_BYTES,
    KEY_ID_BASE32_LENGTH,
    MAX_FILE_GRANT_TTL_SECONDS,
    MAX_FILE_GRANT_USES,
    MAX_FRAME_BYTES,
    MAX_GRANT_TTL_SECONDS,
    MAX_GRANT_USES,
    MAX_PENDING_PAIRINGS,
    MAX_PENDING_PAIRINGS_PER_DEVICE,
    MAX_TIMESTAMP_SECONDS,
    MIN_TIMESTAMP_SECONDS,
    PAIRING_SAS_BITS,
    PAIRING_SAS_LENGTH,
    PAIRING_TTL_SECONDS,
    SUPPORTED_CONNECTOR_PLATFORMS,
    SUPPORTED_VPS_PLATFORMS,
)

EXPECTED_CODES = {
    ConnectorErrorCategory.SETUP: {
        "unsupported_platform",
        "connector_not_initialized",
        "connector_key_locked",
        "interactive_unlock_required",
        "connector_not_paired",
        "grant_required",
        "connector_state_invalid",
        "connector_schema_incompatible",
        "connector_service_running",
    },
    ConnectorErrorCategory.TRANSPORT: {
        "connector_offline",
        "connector_tls_failed",
        "connector_protocol_mismatch",
        "connector_deadline_exceeded",
    },
    ConnectorErrorCategory.AUTHORITY: {
        "device_revoked",
        "grant_revoked",
        "grant_expired",
        "grant_superseded",
        "grant_scope_denied",
        "grant_limit_exhausted",
        "policy_revision_stale",
        "backend_unbound",
        "request_replayed",
    },
    ConnectorErrorCategory.RECEIPT: {
        "receipt_invalid",
        "receipt_context_mismatch",
        "receipt_expired",
        "receipt_replayed",
    },
    ConnectorErrorCategory.SECRET: {
        "secret_unavailable",
        "secret_binding_denied",
    },
    ConnectorErrorCategory.MODEL: {"model_policy_denied"},
    ConnectorErrorCategory.FILE: {"file_grant_invalid", "file_changed"},
}


def test_connector_error_taxonomy_is_closed_and_partitioned() -> None:
    expected_all = set().union(*EXPECTED_CODES.values())

    assert {code.value for code in ConnectorErrorCode} == expected_all
    assert set(ConnectorErrorCategory) == set(EXPECTED_CODES)
    for category, expected_codes in EXPECTED_CODES.items():
        actual = codes_for_category(category)
        assert {code.value for code in actual} == expected_codes
        assert len(actual) == len(expected_codes)
        assert all(category_for_code(code) is category for code in actual)


@pytest.mark.parametrize("code", ConnectorErrorCode)
def test_connector_errors_expose_only_stable_safe_fields(
    code: ConnectorErrorCode,
) -> None:
    unsafe_context = {
        "query": "QUERY_CANARY_do-not-print",
        "url": "https://user:TOKEN_CANARY@example.invalid/private?q=URL_CANARY",
        "path": "/private/PATH_CANARY/provider.json",
        "secret": "SECRET_CANARY",
        "provider_stderr": "STDERR_CANARY: authentication failed",
    }

    error = ConnectorError(code, unsafe_context=unsafe_context)
    data = error.as_data()
    public_forms = (
        str(error),
        repr(error),
        repr(error.args),
        repr(vars(error)),
        json.dumps(data),
    )

    assert set(data) == {"code", "message", "remediation"}
    assert data == {
        "code": error.code,
        "message": error.message,
        "remediation": error.remediation,
    }
    assert error.code == code.value
    assert error.message
    assert error.remediation
    for canary in unsafe_context.values():
        assert all(canary not in form for form in public_forms)


def test_connector_error_rejects_open_ended_codes() -> None:
    canary = "provider_error_SECRET_CANARY"
    with pytest.raises(ValueError) as caught:
        ConnectorError("provider_error_SECRET_CANARY")

    public_forms = (
        str(caught.value),
        repr(caught.value),
        repr(caught.value.args),
        repr(caught.value.__cause__),
        repr(caught.value.__context__),
    )
    assert all(canary not in form for form in public_forms)
    assert caught.value.__cause__ is None


def test_connector_protocol_and_storage_limits_are_frozen() -> None:
    assert CONNECTOR_PROTOCOL_VERSION == "reach-connector/v1"
    assert CONNECTOR_STORAGE_SCHEMA_VERSION == 1
    assert MAX_FRAME_BYTES == 256 * 1024
    assert AUDIT_RETENTION_SECONDS == 30 * 24 * 60 * 60

    assert ID_ENTROPY_BYTES == 16
    assert ID_BASE32_LENGTH == 26
    assert KEY_ID_BASE32_LENGTH == 32
    assert MIN_TIMESTAMP_SECONDS == 0
    assert MAX_TIMESTAMP_SECONDS == 253_402_300_799

    assert PAIRING_TTL_SECONDS == 5 * 60
    assert MAX_PENDING_PAIRINGS == 3
    assert MAX_PENDING_PAIRINGS_PER_DEVICE == 1
    assert PAIRING_SAS_BITS == 50
    assert PAIRING_SAS_LENGTH == 10

    assert DEFAULT_GRANT_TTL_SECONDS == 8 * 60 * 60
    assert MAX_GRANT_TTL_SECONDS == 24 * 60 * 60
    assert DEFAULT_GRANT_USES == 200
    assert MAX_GRANT_USES == 1_000

    assert DEFAULT_FILE_GRANT_TTL_SECONDS == 10 * 60
    assert MAX_FILE_GRANT_TTL_SECONDS == 30 * 60
    assert MAX_FILE_GRANT_USES == 1

    assert SUPPORTED_CONNECTOR_PLATFORMS == frozenset({"darwin", "linux"})
    assert SUPPORTED_VPS_PLATFORMS == frozenset({"linux"})


def test_connector_dependencies_are_direct_and_bounded() -> None:
    project_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with project_path.open("rb") as project_file:
        dependencies = tomllib.load(project_file)["project"]["dependencies"]

    assert "cryptography>=46.0.7,<47" in dependencies
    assert "websockets>=15.0.1,<16" in dependencies
    assert "hermes-agent>=0.19.0,<0.20.0" in dependencies
    assert (
        "agent-reach @ git+https://github.com/izumi0uu/Agent-Reach.git@"
        "806205fd106f4f4453624becfd773acce8418cf1"
    ) in dependencies
