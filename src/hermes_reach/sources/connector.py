"""Explicit VPS-side adapters for verified Connector operation results."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Protocol, cast

from ..catalog import get_operation, get_source
from ..connector.errors import (
    ConnectorError,
    ConnectorErrorCategory,
    ConnectorErrorCode,
    category_for_code,
)
from ..connector.protocol import (
    OperationResponseV1,
    OperationResultItemV1,
    OperationResultMediaV1,
    OperationResultV1,
    PublicBackendIdentity,
)
from ..contracts import OperationCall
from ..runtime.adapters import (
    AdapterBinding,
    AdapterResult,
    FailureClass,
    ItemKind,
    MediaCoverage,
    MediaMetadata,
    RawItem,
    SubtitleOrigin,
)
from ..runtime.availability import AvailabilityRecord
from ..runtime.policy import AuthorizedCall
from .opencli_social_contract import (
    OPENCLI_SOCIAL_BACKEND,
    OPENCLI_SOCIAL_SCOPE_BY_OPERATION,
    OPENCLI_SOCIAL_SOURCES,
)

_CONNECTOR_BACKEND = PublicBackendIdentity("reach-bounded-executor-v1", "1")
_BACKEND_FAILURE_CLASSES: Final[dict[ConnectorErrorCode, FailureClass]] = {
    ConnectorErrorCode.BACKEND_INVALID_INPUT: "invalid_input",
    ConnectorErrorCode.BACKEND_NOT_FOUND: "not_found",
    ConnectorErrorCode.BACKEND_AUTHENTICATION_REQUIRED: "authentication",
    ConnectorErrorCode.BACKEND_AUTHORIZATION_DENIED: "authorization",
    ConnectorErrorCode.BACKEND_UNAVAILABLE: "transient",
    ConnectorErrorCode.BACKEND_INCOMPATIBLE: "setup_required",
    ConnectorErrorCode.BACKEND_DEADLINE_EXCEEDED: "transient",
    ConnectorErrorCode.BACKEND_RATE_LIMITED: "rate_limit",
    ConnectorErrorCode.BACKEND_TRANSIENT: "transient",
    ConnectorErrorCode.BACKEND_PERMANENT: "permanent",
    ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION: "permanent",
}


class _ConnectorOperationClient(Protocol):
    async def execute(
        self, call: OperationCall, *, trace_id: str
    ) -> OperationResponseV1: ...


class _ConnectorOperationAvailability(Protocol):
    def __call__(self, source: str, operation: str) -> AvailabilityRecord: ...


class _ConnectorAdapter:
    __slots__ = ("_backend", "_client", "_operation", "_source")

    def __init__(
        self,
        source: str,
        operation: str,
        client: _ConnectorOperationClient,
        backend: PublicBackendIdentity,
    ) -> None:
        self._source = source
        self._operation = operation
        self._client = client
        self._backend = backend

    async def execute(self, authorized: AuthorizedCall) -> AdapterResult:
        if (
            not isinstance(authorized, AuthorizedCall)
            or authorized.call.source.name != self._source
            or authorized.operation.name != self._operation
            or authorized.trace_id is None
        ):
            return AdapterResult(failure_class="policy")
        try:
            response = await self._client.execute(
                authorized.call, trace_id=authorized.trace_id
            )
        except ConnectorError as error:
            return AdapterResult(failure_class=_failure_class(error))
        if response.result is None or response.receipt.backend != self._backend:
            return AdapterResult(failure_class="permanent")
        return _adapter_result(response.result)


def connector_bindings(
    client: _ConnectorOperationClient,
    availability: _ConnectorOperationAvailability,
    operations: Sequence[tuple[str, str]],
) -> tuple[AdapterBinding, ...]:
    """Build only explicitly selected exact Connector-backed runtime bindings."""

    if (
        not callable(getattr(client, "execute", None))
        or not callable(availability)
        or isinstance(operations, str | bytes | bytearray)
    ):
        raise TypeError("The Connector adapter composition is invalid.")
    selected: set[tuple[str, str]] = set()
    bindings: list[AdapterBinding] = []
    for selection in operations:
        if (
            type(selection) is not tuple
            or len(selection) != 2
            or not all(type(value) is str for value in selection)
        ):
            raise ValueError("The Connector adapter selection is invalid.")
        source_name, operation_name = selection
        key = (source_name, operation_name)
        source = get_source(source_name)
        operation = (
            get_operation(source, operation_name) if source is not None else None
        )
        if (
            key in selected
            or operation is None
            or operation.tool == "status"
            or operation.implementation_state != "implemented"
        ):
            raise ValueError("The Connector adapter selection is invalid.")
        selected.add(key)
        backend = _backend_for(source_name, operation_name)
        adapter = _ConnectorAdapter(source_name, operation_name, client, backend)
        bindings.append(
            AdapterBinding(
                source=source_name,
                operation=operation_name,
                backend_id=backend.backend_id,
                backend_version=backend.backend_version,
                priority=10,
                required_scope=operation.runtime.data_scope,
                equivalence_group=f"{source_name}:{operation_name}:v1",
                execute=adapter.execute,
                availability_resolver=availability,
                retry_owner="binding",
            )
        )
    return tuple(bindings)


def _adapter_result(result: OperationResultV1) -> AdapterResult:
    return AdapterResult(
        tuple(_raw_item(item) for item in result.items),
        truncated=result.truncated,
    )


def _raw_item(item: OperationResultItemV1) -> RawItem:
    return RawItem(
        text=item.text,
        native_id=item.native_id,
        kind=cast(ItemKind, item.kind),
        title=item.title,
        url=item.url,
        author=item.author,
        published_at=item.published_at,
        media=_media(item.media),
    )


def _media(media: OperationResultMediaV1 | None) -> MediaMetadata | None:
    if media is None:
        return None
    return MediaMetadata(
        duration_seconds=media.duration_seconds,
        view_count=media.view_count,
        comment_count=media.comment_count,
        subtitle_language=media.subtitle_language,
        subtitle_origin=cast(SubtitleOrigin | None, media.subtitle_origin),
        coverage=cast(MediaCoverage, media.coverage),
    )


def _failure_class(error: ConnectorError) -> FailureClass:
    code = ConnectorErrorCode(error.code)
    backend_failure = _BACKEND_FAILURE_CLASSES.get(code)
    if backend_failure is not None:
        return backend_failure
    category = category_for_code(code)
    if category is ConnectorErrorCategory.TRANSPORT:
        return "transient"
    if category in {ConnectorErrorCategory.SETUP, ConnectorErrorCategory.SECRET}:
        return "setup_required"
    if category is ConnectorErrorCategory.AUTHORITY:
        return "authorization"
    if category in {ConnectorErrorCategory.MODEL, ConnectorErrorCategory.FILE}:
        return "policy"
    return "permanent"


def _backend_for(source: str, operation: str) -> PublicBackendIdentity:
    if (source, operation) in OPENCLI_SOCIAL_SCOPE_BY_OPERATION:
        return OPENCLI_SOCIAL_BACKEND
    if source in OPENCLI_SOCIAL_SOURCES:
        raise ValueError("The Connector adapter selection is invalid.")
    return _CONNECTOR_BACKEND


__all__ = ["connector_bindings"]
