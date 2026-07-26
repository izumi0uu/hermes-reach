"""Frozen Connector protocol, storage, and authority bounds."""

from typing import Final

CONNECTOR_PROTOCOL_VERSION: Final = "reach-connector/v1"
CONNECTOR_STORAGE_SCHEMA_VERSION: Final = 1

MAX_FRAME_BYTES: Final = 256 * 1024
MAX_JSON_DEPTH: Final = 32
MAX_JSON_CONTAINER_ITEMS: Final = 256
AUDIT_RETENTION_SECONDS: Final = 30 * 24 * 60 * 60

# Random wire IDs use 128 bits encoded as unpadded base32. Device key IDs use
# the first 160 bits of the public-key digest and therefore need 32 characters.
ID_ENTROPY_BYTES: Final = 16
ID_BASE32_LENGTH: Final = 26
KEY_ID_BASE32_LENGTH: Final = 32

MIN_TIMESTAMP_SECONDS: Final = 0
MAX_TIMESTAMP_SECONDS: Final = 253_402_300_799

PAIRING_TTL_SECONDS: Final = 5 * 60
MAX_PENDING_PAIRINGS: Final = 3
MAX_PENDING_PAIRINGS_PER_DEVICE: Final = 1
PAIRING_SAS_BITS: Final = 50
PAIRING_SAS_LENGTH: Final = 10
DEVICE_NONCE_BYTES: Final = 32
MAX_DEVICE_LABEL_LENGTH: Final = 64
MAX_GRANT_SCOPES: Final = 64
MAX_TLS_CA_DER_BYTES: Final = 8 * 1024

DEFAULT_GRANT_TTL_SECONDS: Final = 8 * 60 * 60
MAX_GRANT_TTL_SECONDS: Final = 24 * 60 * 60
DEFAULT_GRANT_USES: Final = 200
MAX_GRANT_USES: Final = 1_000

MAX_REQUEST_TTL_SECONDS: Final = 60
MAX_RECEIPT_TTL_SECONDS: Final = 5 * 60
MAX_CLOCK_SKEW_SECONDS: Final = 30

DEFAULT_FILE_GRANT_TTL_SECONDS: Final = 10 * 60
MAX_FILE_GRANT_TTL_SECONDS: Final = 30 * 60
MAX_FILE_GRANT_USES: Final = 1
MAX_FILE_BYTES: Final = 1024 * 1024 * 1024

SUPPORTED_CONNECTOR_PLATFORMS: Final[frozenset[str]] = frozenset({"darwin", "linux"})
SUPPORTED_VPS_PLATFORMS: Final[frozenset[str]] = frozenset({"linux"})
