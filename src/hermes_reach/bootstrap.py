"""Construction of default and explicitly configured process-local runtimes."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final

from .connector.audit import ReceiptEvidenceLedger
from .connector.client import (
    ConnectorAvailabilityResolver,
    ConnectorClient,
    ConnectorSnapshotStore,
    PairedVpsProfile,
    VpsProfileStore,
)
from .connector.identity import VpsKeyStore
from .connector.transport import PinnedWssClient
from .runtime.dispatcher import RuntimeDispatcher
from .sources.connector import connector_bindings
from .sources.registry import build_alpha1_registry, build_alpha1_runtime

VPS_STATE_DIRECTORY_ENVIRONMENT: Final = "HERMES_REACH_VPS_STATE_DIRECTORY"
_REDDIT_OPERATION: Final = ("reddit", "read.post")
_VPS_RECEIPT_LEDGER: Final = "receipts.jsonl"


def build_vps_runtime(state_directory: Path) -> RuntimeDispatcher:
    """Compose Alpha-1 plus the one exact Connector adapter from local state."""

    registry = build_alpha1_registry()
    try:
        if not isinstance(state_directory, Path) or not state_directory.is_absolute():
            raise ValueError("The VPS state directory must be absolute.")
        profile_store = VpsProfileStore(state_directory)
        profile = profile_store.load()
        if not isinstance(profile, PairedVpsProfile):
            raise ValueError("The VPS state is not paired.")
        vps_identity = VpsKeyStore(state_directory).load()
        snapshot_store = ConnectorSnapshotStore(state_directory)
        availability = ConnectorAvailabilityResolver(profile_store, snapshot_store)
        transport = PinnedWssClient(profile.endpoint, profile.authority())
        evidence = ReceiptEvidenceLedger(
            state_directory / _VPS_RECEIPT_LEDGER,
            profile.connector_identity,
            role="vps",
        )
        client = ConnectorClient(
            profile,
            vps_identity,
            transport,
            evidence,
            snapshot_store,
        )
        for binding in connector_bindings(client, availability, (_REDDIT_OPERATION,)):
            registry.register(binding)
    except Exception:
        registry.mark(
            _REDDIT_OPERATION[0],
            _REDDIT_OPERATION[1],
            "unavailable",
            "The configured Connector state could not be verified.",
        )
    return RuntimeDispatcher(registry)


def runtime_from_environment(environment: Mapping[str, str]) -> RuntimeDispatcher:
    """Read only the explicit VPS state pointer used at plugin registration."""

    try:
        value = environment.get(VPS_STATE_DIRECTORY_ENVIRONMENT)
    except Exception:
        value = ""
    if value is None:
        return DEFAULT_RUNTIME
    if type(value) is not str or not value or "\x00" in value:
        return build_vps_runtime(Path("."))
    try:
        state_directory = Path(value)
    except (TypeError, ValueError):
        state_directory = Path(".")
    return build_vps_runtime(state_directory)


DEFAULT_RUNTIME = build_alpha1_runtime()


__all__ = [
    "DEFAULT_RUNTIME",
    "VPS_STATE_DIRECTORY_ENVIRONMENT",
    "build_vps_runtime",
    "runtime_from_environment",
]
