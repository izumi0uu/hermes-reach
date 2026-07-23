"""Authenticated encrypted resource and continuation tokens with no server state."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

from cryptography.fernet import Fernet, InvalidToken

from ..catalog import get_operation, get_source
from ..contracts import ReachValidationError

TOKEN_VERSION: Final = "v1"
TokenPurpose = Literal["resource_ref", "continuation"]
Clock = Callable[[], datetime]


class TokenError(ReachValidationError):
    """A stable token error that intentionally omits token and target values."""


@dataclass(frozen=True, slots=True)
class TokenPayload:
    """The decrypted, internal-only routing context for a stateless token."""

    purpose: TokenPurpose
    source: str
    operation: str
    target: Mapping[str, str]
    target_digest: str
    backend_id: str
    maximum_items: int
    maximum_characters: int
    issued_at: int
    expires_at: int
    cursor: str | None = None


class TokenCodec:
    """Mint and validate opaque tokens using an injected process-owned key."""

    def __init__(self, key: bytes, clock: Clock | None = None) -> None:
        if len(key) != 32:
            raise ValueError("Token keys must contain exactly 32 bytes.")
        self._fernet = Fernet(base64.urlsafe_b64encode(key))
        self._clock = clock or _utc_now

    def mint_resource_ref(
        self,
        source: str,
        operation: str,
        target: Mapping[str, str],
        backend_id: str,
        ttl_seconds: int,
    ) -> str:
        """Mint an encrypted resource routing reference."""

        return self._mint(
            "resource_ref", source, operation, target, backend_id, ttl_seconds, None
        )

    def mint_continuation(
        self,
        source: str,
        operation: str,
        target: Mapping[str, str],
        backend_id: str,
        ttl_seconds: int,
        cursor: str,
    ) -> str:
        """Mint an encrypted continuation cursor for the same operation."""

        if not cursor:
            raise ValueError("Continuation cursors must be non-empty.")
        return self._mint(
            "continuation", source, operation, target, backend_id, ttl_seconds, cursor
        )

    def decode_resource_ref(
        self,
        token: str,
        source: str,
        operation: str,
        target: Mapping[str, str] | None = None,
    ) -> TokenPayload:
        """Decode a resource reference and verify its operation and target."""

        return self._decode("resource_ref", token, source, operation, target)

    def decode_continuation(
        self,
        token: str,
        source: str,
        operation: str,
        target: Mapping[str, str] | None = None,
    ) -> TokenPayload:
        """Decode a continuation and verify its operation and target."""

        return self._decode("continuation", token, source, operation, target)

    def _mint(
        self,
        purpose: TokenPurpose,
        source: str,
        operation: str,
        target: Mapping[str, str],
        backend_id: str,
        ttl_seconds: int,
        cursor: str | None,
    ) -> str:
        if ttl_seconds <= 0:
            raise ValueError("Token lifetime must be positive.")
        normalized_target = _normalized_target(target)
        maximum_items, maximum_characters = _limits_for(source, operation)
        now = _timestamp(self._clock())
        payload = {
            "version": TOKEN_VERSION,
            "purpose": purpose,
            "source": source,
            "operation": operation,
            "target": normalized_target,
            "target_digest": _target_digest(normalized_target),
            "backend_id": backend_id,
            "maximum_items": maximum_items,
            "maximum_characters": maximum_characters,
            "issued_at": now,
            "expires_at": now + ttl_seconds,
            "cursor": cursor,
        }
        encoded = json.dumps(
            payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode()
        return self._fernet.encrypt(encoded).decode("ascii")

    def _decode(
        self,
        purpose: TokenPurpose,
        token: str,
        source: str,
        operation: str,
        target: Mapping[str, str] | None,
    ) -> TokenPayload:
        try:
            decoded = self._fernet.decrypt(token.encode("ascii"))
            raw_payload = json.loads(decoded)
        except (
            AttributeError,
            InvalidToken,
            UnicodeDecodeError,
            UnicodeEncodeError,
            json.JSONDecodeError,
        ):
            raise self._invalid(purpose) from None
        if not isinstance(raw_payload, dict):
            raise self._invalid(purpose)
        payload = self._payload_from(raw_payload, purpose)
        if payload.source != source or payload.operation != operation:
            raise self._invalid(purpose)
        try:
            current_limits = _limits_for(source, operation)
        except ValueError:
            raise self._invalid(purpose) from None
        if (payload.maximum_items, payload.maximum_characters) != current_limits:
            raise self._invalid(purpose)
        if payload.expires_at <= _timestamp(self._clock()):
            if purpose == "resource_ref":
                raise TokenError(
                    "resource_ref_expired",
                    "The resource reference has expired.",
                    "Repeat discovery to obtain a new resource reference.",
                )
            raise self._invalid(purpose)
        if target is not None and not self._matches_target(
            target, payload.target_digest, purpose
        ):
            raise TokenError(
                "resource_changed",
                "The target no longer matches the resource reference.",
                "Repeat discovery before continuing this operation.",
            )
        return payload

    def _matches_target(
        self, target: Mapping[str, str], digest: str, purpose: TokenPurpose
    ) -> bool:
        try:
            return _target_digest(_normalized_target(target)) == digest
        except (AttributeError, ValueError):
            raise self._invalid(purpose) from None

    def _payload_from(
        self, raw_payload: dict[object, object], purpose: TokenPurpose
    ) -> TokenPayload:
        expected = {
            "version",
            "purpose",
            "source",
            "operation",
            "target",
            "target_digest",
            "backend_id",
            "maximum_items",
            "maximum_characters",
            "issued_at",
            "expires_at",
            "cursor",
        }
        if set(raw_payload) != expected:
            raise self._invalid(purpose)
        if raw_payload["version"] != TOKEN_VERSION or raw_payload["purpose"] != purpose:
            raise self._invalid(purpose)
        target = raw_payload["target"]
        if not isinstance(target, dict):
            raise self._invalid(purpose)
        try:
            normalized_target = _normalized_target(target)
        except ValueError:
            raise self._invalid(purpose) from None
        target_digest = raw_payload["target_digest"]
        source = raw_payload["source"]
        operation = raw_payload["operation"]
        backend_id = raw_payload["backend_id"]
        maximum_items = raw_payload["maximum_items"]
        maximum_characters = raw_payload["maximum_characters"]
        issued_at = raw_payload["issued_at"]
        expires_at = raw_payload["expires_at"]
        cursor = raw_payload["cursor"]
        if (
            not isinstance(source, str)
            or not source
            or not isinstance(operation, str)
            or not operation
            or not isinstance(backend_id, str)
            or not backend_id
            or isinstance(maximum_items, bool)
            or not isinstance(maximum_items, int)
            or maximum_items <= 0
            or isinstance(maximum_characters, bool)
            or not isinstance(maximum_characters, int)
            or maximum_characters <= 0
            or not isinstance(target_digest, str)
            or target_digest != _target_digest(normalized_target)
            or isinstance(issued_at, bool)
            or not isinstance(issued_at, int)
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
            or (cursor is not None and (not isinstance(cursor, str) or not cursor))
        ):
            raise self._invalid(purpose)
        return TokenPayload(
            purpose=purpose,
            source=source,
            operation=operation,
            target=normalized_target,
            target_digest=target_digest,
            backend_id=backend_id,
            maximum_items=maximum_items,
            maximum_characters=maximum_characters,
            issued_at=issued_at,
            expires_at=expires_at,
            cursor=cursor,
        )

    def _invalid(self, purpose: TokenPurpose) -> TokenError:
        if purpose == "resource_ref":
            return TokenError(
                "resource_ref_invalid",
                "The resource reference is invalid.",
                "Repeat discovery to obtain a valid resource reference.",
            )
        return TokenError(
            "continuation_invalid",
            "The continuation is invalid.",
            "Repeat discovery before continuing this operation.",
        )


def _normalized_target(target: Mapping[str, str]) -> dict[str, str]:
    if not target or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in target.items()
    ):
        raise ValueError("Token targets must be a non-empty string mapping.")
    return dict(sorted(target.items()))


def _target_digest(target: Mapping[str, str]) -> str:
    encoded = json.dumps(
        target, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _limits_for(source_name: str, operation_name: str) -> tuple[int, int]:
    source = get_source(source_name)
    operation = get_operation(source, operation_name) if source is not None else None
    if operation is None:
        raise ValueError("Tokens must reference a catalog operation.")
    runtime = operation.runtime
    return runtime.maximum_items, runtime.maximum_characters


def _timestamp(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("Token clocks must return timezone-aware datetimes.")
    return int(value.timestamp())


def _utc_now() -> datetime:
    return datetime.now(UTC)
