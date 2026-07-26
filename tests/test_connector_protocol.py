from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from hermes_reach.connector.errors import ConnectorErrorCode
from hermes_reach.connector.identity import (
    DevicePrivateIdentity,
    SignatureDomain,
)
from hermes_reach.connector.limits import (
    MAX_FRAME_BYTES,
    MAX_GRANT_TTL_SECONDS,
    MAX_GRANT_USES,
    MAX_TIMESTAMP_SECONDS,
    PAIRING_TTL_SECONDS,
)
from hermes_reach.connector.protocol import (
    ErrorFrame,
    FileGrant,
    GrantClaims,
    GrantScope,
    PairingChallenge,
    PairingComplete,
    PairingInit,
    PairingResolution,
    ProtectedOperationPayload,
    ProtocolValidationError,
    PublicBackendIdentity,
    ReceiptFailure,
    ReceiptUsage,
    SignedGrant,
    SignedReceipt,
    SignedRequest,
    canonical_json_bytes,
    create_file_grant,
    create_pairing_challenge,
    create_pairing_complete,
    create_pairing_init,
    create_pairing_resolution,
    create_signed_grant,
    create_signed_receipt,
    create_signed_request,
    encode_record,
    load_canonical_json,
    operation_payload_digest,
    pairing_ca_der,
    pairing_sas,
    pairing_transcript_hash,
    parse_protected_operation,
    parse_record,
    protect_operation_call,
    record_digest,
    record_signing_bytes,
    verify_file_grant,
    verify_pairing_challenge,
    verify_pairing_complete,
    verify_pairing_init,
    verify_pairing_resolution,
    verify_record,
    verify_signed_grant,
    verify_signed_receipt,
    verify_signed_request,
)
from hermes_reach.contracts import validate_read, validate_transcribe

VPS_SEED = bytes.fromhex(
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
)
CONNECTOR_SEED = bytes(range(32))
OTHER_SEED = bytes(range(32, 64))

NOW = 1_800_000_000
PAIRING_ID = "aaaqeayeaudaocajbifqydiob4"
PAIRING_MESSAGE_ID = "caireeyuculbogazdinryhi6d4"
CHALLENGE_MESSAGE_ID = "eaqseizeeutcokbjfivsyljof4"
COMPLETE_MESSAGE_ID = "gaytemzugu3doobzhi5typj6h4"
GRANT_ID = "ibaueq2eivdeoscjjjfuytkoj4"
GRANT_MESSAGE_ID = "kbiveu2ukvlfowczljnvyxk6l4"
REQUEST_ID = "mfyha3dspb2hq5dimvwgy3zao4"
REQUEST_MESSAGE_ID = "oj2xe43uov3ho6dzpj5xy7l6p4"
RECEIPT_ID = "qgi2dmob2hq7e3tqoj2hm5dyp4"
RECEIPT_MESSAGE_ID = "syljpf4x2zlypb4x2zlypcmlq4"
FILE_GRANT_ID = "vk54zxpo74aacaqdaqcqmbyha4"
FILE_MESSAGE_ID = "x3ahb4hq6dypb4hq6dypb4irce"

VPS_NONCE = bytes(range(32))
CONNECTOR_NONCE = bytes(range(32, 64))
ENDPOINT_DIGEST = "11" * 32
TLS_CA_DER = b"hermes-reach-frozen-connector-ca-der-v1"
TLS_CA_FINGERPRINT = hashlib.sha256(TLS_CA_DER).hexdigest()
TLS_LEAF_FINGERPRINT = "22" * 32
FILE_DIGEST = "33" * 32
PAYLOAD_DIGEST = "5bb4ccc1a49255a432eea400f0ac85cabf0fe31dc65a7d441b4d233b07028770"
QUERY_CANARY = "QUERY_CANARY=TOKEN_CANARY"
CANARY_URL = f"https://example.invalid/article?{QUERY_CANARY}"

FROZEN_PROTOCOL_VECTORS = json.loads(
    (
        Path(__file__).with_name("fixtures") / "connector_protocol_vectors.json"
    ).read_text(encoding="utf-8")
)


def _vector_bytes(record_type: str, field: str) -> bytes:
    value = FROZEN_PROTOCOL_VECTORS[record_type][field]
    assert isinstance(value, str)
    return base64.b64decode(value, validate=True)


def _identity(seed: bytes) -> DevicePrivateIdentity:
    return DevicePrivateIdentity._from_seed_for_testing(seed)


@pytest.fixture
def vps() -> DevicePrivateIdentity:
    return _identity(VPS_SEED)


@pytest.fixture
def connector() -> DevicePrivateIdentity:
    return _identity(CONNECTOR_SEED)


@pytest.fixture
def scope() -> GrantScope:
    return GrantScope(
        source="web",
        operation="read.url",
        data_scope="public",
        capability_id=None,
    )


@pytest.fixture
def pairing_init(vps: DevicePrivateIdentity, scope: GrantScope) -> PairingInit:
    return create_pairing_init(
        signer=vps,
        message_id=PAIRING_MESSAGE_ID,
        pairing_id=PAIRING_ID,
        device_label="reach-vps-1",
        endpoint_digest=ENDPOINT_DIGEST,
        vps_nonce=VPS_NONCE,
        requested_scopes=(scope,),
        grant_expires_at=NOW + 8 * 60 * 60,
        grant_max_uses=200,
        issued_at=NOW,
        deadline=NOW + PAIRING_TTL_SECONDS,
    )


@pytest.fixture
def pairing_challenge(
    connector: DevicePrivateIdentity,
    pairing_init: PairingInit,
) -> PairingChallenge:
    return create_pairing_challenge(
        signer=connector,
        message_id=CHALLENGE_MESSAGE_ID,
        pairing_id=pairing_init.pairing_id,
        init_digest=record_digest(pairing_init),
        vps_key_id=pairing_init.vps_key_id,
        connector_nonce=CONNECTOR_NONCE,
        tls_ca_der=TLS_CA_DER,
        tls_leaf_fingerprint=TLS_LEAF_FINGERPRINT,
        issued_at=NOW + 1,
        deadline=NOW + PAIRING_TTL_SECONDS,
    )


@pytest.fixture
def grant_claims(
    connector: DevicePrivateIdentity,
    vps: DevicePrivateIdentity,
    scope: GrantScope,
) -> GrantClaims:
    return GrantClaims(
        grant_id=GRANT_ID,
        revision=1,
        issuer_key_id=connector.public_identity.key_id,
        subject_key_id=vps.public_identity.key_id,
        issued_at=NOW + 2,
        not_before=NOW + 2,
        expires_at=NOW + 8 * 60 * 60,
        policy_revision=1,
        max_uses=200,
        scopes=(scope,),
        signature_algorithm="ed25519-v1",
    )


@pytest.fixture
def signed_grant(
    connector: DevicePrivateIdentity,
    grant_claims: GrantClaims,
) -> SignedGrant:
    return create_signed_grant(
        signer=connector,
        message_id=GRANT_MESSAGE_ID,
        claims=grant_claims,
    )


@pytest.fixture
def pairing_complete(
    connector: DevicePrivateIdentity,
    pairing_init: PairingInit,
    pairing_challenge: PairingChallenge,
    signed_grant: SignedGrant,
) -> PairingComplete:
    return create_pairing_complete(
        signer=connector,
        message_id=COMPLETE_MESSAGE_ID,
        pairing_id=pairing_init.pairing_id,
        transcript_digest=pairing_transcript_hash(
            encode_record(pairing_init),
            encode_record(pairing_challenge),
            observed_tls_leaf_fingerprint=TLS_LEAF_FINGERPRINT,
        ).hex(),
        vps_key_id=pairing_init.vps_key_id,
        signed_grant_digest=record_digest(signed_grant),
        completed_at=NOW + 3,
    )


@pytest.fixture
def protected() -> ProtectedOperationPayload:
    call = validate_read(
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": CANARY_URL},
        }
    )
    return protect_operation_call(call)


@pytest.fixture
def signed_request(
    vps: DevicePrivateIdentity,
    connector: DevicePrivateIdentity,
    protected: ProtectedOperationPayload,
) -> SignedRequest:
    return create_signed_request(
        signer=vps,
        message_id=REQUEST_MESSAGE_ID,
        request_id=REQUEST_ID,
        trace_id="0123456789abcdef0123456789abcdef",
        audience_key_id=connector.public_identity.key_id,
        grant_id=GRANT_ID,
        grant_revision=1,
        policy_revision=1,
        source="web",
        operation="read.url",
        issued_at=NOW + 4,
        deadline=NOW + 34,
        protected_payload=protected,
    )


@pytest.fixture
def signed_receipt(
    connector: DevicePrivateIdentity,
    signed_request: SignedRequest,
) -> SignedReceipt:
    return create_signed_receipt(
        signer=connector,
        message_id=RECEIPT_MESSAGE_ID,
        receipt_id=RECEIPT_ID,
        request=signed_request,
        decision="allow",
        failure=None,
        usage=ReceiptUsage(sequence=1, remaining=199),
        backend=PublicBackendIdentity("reach-bounded-executor-v1", "1"),
        started_at=NOW + 5,
        ended_at=NOW + 6,
        expires_at=NOW + 306,
        result_count=1,
        truncated=False,
        outcome="ok",
    )


@pytest.fixture
def file_grant(
    connector: DevicePrivateIdentity,
    vps: DevicePrivateIdentity,
) -> FileGrant:
    return create_file_grant(
        signer=connector,
        message_id=FILE_MESSAGE_ID,
        file_grant_id=FILE_GRANT_ID,
        subject_key_id=vps.public_identity.key_id,
        digest=FILE_DIGEST,
        size=4096,
        source="youtube",
        operation="transcribe.video",
        grant_revision=1,
        policy_revision=1,
        issued_at=NOW,
        expires_at=NOW + 10 * 60,
    )


@pytest.fixture
def all_records(
    pairing_init: PairingInit,
    pairing_challenge: PairingChallenge,
    pairing_complete: PairingComplete,
    signed_grant: SignedGrant,
    signed_request: SignedRequest,
    signed_receipt: SignedReceipt,
    file_grant: FileGrant,
) -> tuple[object, ...]:
    return (
        pairing_init,
        pairing_challenge,
        pairing_complete,
        signed_grant,
        signed_request,
        signed_receipt,
        file_grant,
        ErrorFrame(
            message_id=REQUEST_MESSAGE_ID,
            code=ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH,
        ),
    )


def _mapping(record: object) -> dict[str, object]:
    value = json.loads(encode_record(record))
    assert isinstance(value, dict)
    return value


def _raw(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def test_canonical_json_pins_exact_supported_scalar_encoding() -> None:
    value = {
        "z": [None, True, False, 0, -1, "caf\N{LATIN SMALL LETTER E WITH ACUTE}"],
        "a": {},
    }

    encoded = canonical_json_bytes(value)

    assert encoded == b'{"a":{},"z":[null,true,false,0,-1,"caf\\u00e9"]}'
    assert load_canonical_json(encoded) == value


@pytest.mark.parametrize(
    "raw",
    [
        b' {"a":1}',
        b'{"a":1} ',
        b'{"a" :1}',
        b'{"b":2,"a":1}',
        b'{"a":-0}',
        b'{"a":"\\u0061"}',
        b'{"a":"x\\/y"}',
        b'{"a":"\xc3\xa9"}',
        b'\xef\xbb\xbf{"a":1}',
        b'{"a":1}\n',
    ],
)
def test_loader_rejects_alternate_encodings(raw: bytes) -> None:
    with pytest.raises(ProtocolValidationError):
        load_canonical_json(raw)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}',
        b'{"outer":{"a":1,"a":2}}',
        b'{"a":1.0}',
        b'{"a":1e0}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":-Infinity}',
        b'{"a":"\\ud800"}',
        b'{"a":"\\u0000"}',
        b'{"a":"line\\nfeed"}',
    ],
)
def test_loader_rejects_duplicate_float_nonfinite_and_surrogate(
    raw: bytes,
) -> None:
    with pytest.raises(ProtocolValidationError):
        load_canonical_json(raw)


@pytest.mark.parametrize(
    "value",
    [
        1.0,
        float("nan"),
        (1, 2),
        {1: "not-a-string-key"},
        {"bad": b"bytes"},
        {"bad": "\ud800"},
        {"bad": "\x00"},
        {"bad": "line\nfeed"},
    ],
)
def test_serializer_rejects_values_outside_the_canonical_subset(value: object) -> None:
    with pytest.raises(ProtocolValidationError):
        canonical_json_bytes(value)


def test_canonical_json_enforces_depth_and_byte_bounds() -> None:
    nested: object = None
    for _ in range(64):
        nested = [nested]

    with pytest.raises(ProtocolValidationError):
        canonical_json_bytes(nested)
    with pytest.raises(ProtocolValidationError):
        load_canonical_json(b" " * (MAX_FRAME_BYTES + 1))
    with pytest.raises(ProtocolValidationError):
        canonical_json_bytes({"x": "x" * MAX_FRAME_BYTES})
    with pytest.raises(ProtocolValidationError):
        canonical_json_bytes({f"item-{index}": index for index in range(257)})


def test_pairing_init_fixed_frame_pins_schema_signature_and_domain(
    pairing_init: PairingInit,
    vps: DevicePrivateIdentity,
) -> None:
    frozen = FROZEN_PROTOCOL_VECTORS["pairing_init"]
    fixed_frame = _vector_bytes("pairing_init", "frame_b64")
    assert encode_record(pairing_init) == fixed_frame
    assert parse_record(fixed_frame) == pairing_init
    signature = base64.urlsafe_b64decode(
        _mapping(pairing_init)["body"]["signature"] + "=="  # type: ignore[index]
    )
    signing_bytes = record_signing_bytes(pairing_init)
    assert vps.public_identity.verify(
        SignatureDomain.PAIRING_INIT, signing_bytes, signature
    )
    assert not vps.public_identity.verify(
        SignatureDomain.PAIRING_CHALLENGE, signing_bytes, signature
    )
    assert signing_bytes == _vector_bytes("pairing_init", "signing_b64")
    assert signature.hex() == frozen["signature_hex"]
    assert record_digest(pairing_init) == frozen["digest"]


def test_every_signed_record_matches_an_independent_complete_vector(
    vps: DevicePrivateIdentity,
    connector: DevicePrivateIdentity,
    pairing_init: PairingInit,
    pairing_challenge: PairingChallenge,
    pairing_complete: PairingComplete,
    signed_grant: SignedGrant,
    signed_request: SignedRequest,
    signed_receipt: SignedReceipt,
    file_grant: FileGrant,
) -> None:
    records = {
        "pairing_init": (pairing_init, vps, SignatureDomain.PAIRING_INIT),
        "pairing_challenge": (
            pairing_challenge,
            connector,
            SignatureDomain.PAIRING_CHALLENGE,
        ),
        "pairing_complete": (
            pairing_complete,
            connector,
            SignatureDomain.PAIRING_COMPLETE,
        ),
        "signed_grant": (signed_grant, connector, SignatureDomain.GRANT),
        "signed_request": (signed_request, vps, SignatureDomain.REQUEST),
        "signed_receipt": (signed_receipt, connector, SignatureDomain.RECEIPT),
        "file_grant": (file_grant, connector, SignatureDomain.FILE_GRANT),
    }

    for record_type, (record, signer, domain) in records.items():
        vector = FROZEN_PROTOCOL_VECTORS[record_type]
        frame = _vector_bytes(record_type, "frame_b64")
        signing_bytes = _vector_bytes(record_type, "signing_b64")
        signature = bytes.fromhex(vector["signature_hex"])

        assert encode_record(record) == frame
        assert parse_record(frame) == record
        assert record_signing_bytes(record) == signing_bytes
        assert record.signature == signature
        assert record_digest(record) == vector["digest"]
        assert signer.public_identity.verify(domain, signing_bytes, signature)


def test_every_record_round_trips_as_one_closed_canonical_frame(
    all_records: tuple[object, ...],
) -> None:
    for record in all_records:
        encoded = encode_record(record)
        assert len(encoded) <= MAX_FRAME_BYTES
        assert encode_record(parse_record(encoded)) == encoded


def test_every_signed_record_uses_its_exact_domain(
    all_records: tuple[object, ...],
    connector: DevicePrivateIdentity,
    vps: DevicePrivateIdentity,
) -> None:
    domains = {
        PairingInit: (SignatureDomain.PAIRING_INIT, vps),
        PairingChallenge: (SignatureDomain.PAIRING_CHALLENGE, connector),
        PairingComplete: (SignatureDomain.PAIRING_COMPLETE, connector),
        SignedGrant: (SignatureDomain.GRANT, connector),
        SignedRequest: (SignatureDomain.REQUEST, vps),
        SignedReceipt: (SignatureDomain.RECEIPT, connector),
        FileGrant: (SignatureDomain.FILE_GRANT, connector),
    }
    for record in all_records:
        expected = domains.get(type(record))
        if expected is None:
            continue
        domain, signer = expected
        signature = record.signature  # type: ignore[union-attr]
        signing_bytes = record_signing_bytes(record)  # type: ignore[arg-type]
        wrong_domain = (
            SignatureDomain.RECEIPT
            if domain is not SignatureDomain.RECEIPT
            else SignatureDomain.REQUEST
        )
        assert signer.public_identity.verify(domain, signing_bytes, signature)
        assert not signer.public_identity.verify(wrong_domain, signing_bytes, signature)


def test_protocol_records_are_frozen(pairing_init: PairingInit) -> None:
    with pytest.raises(FrozenInstanceError):
        pairing_init.message_id = REQUEST_MESSAGE_ID  # type: ignore[misc]


@pytest.mark.parametrize("location", ["outer", "body", "claims"])
def test_record_parser_rejects_unknown_fields(
    pairing_init: PairingInit,
    location: str,
) -> None:
    value = _mapping(pairing_init)
    target = value
    if location == "body":
        target = value["body"]  # type: ignore[assignment]
    elif location == "claims":
        target = value["body"]["claims"]  # type: ignore[index,assignment]
    target["provider_SECRET_CANARY"] = "TOKEN_CANARY"

    with pytest.raises(ProtocolValidationError):
        parse_record(_raw(value))


def test_record_parser_rejects_unknown_nested_scope_and_backend_fields(
    pairing_init: PairingInit,
    signed_receipt: SignedReceipt,
) -> None:
    pairing = _mapping(pairing_init)
    scopes = pairing["body"]["claims"]["requested_scopes"]  # type: ignore[index]
    scopes[0]["selector"] = "SECRET_CANARY"  # type: ignore[index]
    receipt = _mapping(signed_receipt)
    backend = receipt["body"]["claims"]["backend"]  # type: ignore[index]
    backend["project"] = "PROJECT_CANARY"  # type: ignore[index]

    for value in (pairing, receipt):
        with pytest.raises(ProtocolValidationError):
            parse_record(_raw(value))


@pytest.mark.parametrize(
    ("field", "mutate"),
    [
        ("signature", lambda value: value + "="),
        ("signature", lambda value: value[:-1] + "x"),
        ("vps_nonce", lambda value: value + "="),
        ("vps_nonce", lambda value: value[:-1] + "9"),
    ],
)
def test_record_parser_rejects_noncanonical_base64url_fields(
    pairing_init: PairingInit,
    field: str,
    mutate: object,
) -> None:
    mapping = _mapping(pairing_init)
    body = mapping["body"]  # type: ignore[assignment]
    target = body if field == "signature" else body["claims"]  # type: ignore[index]
    value = target[field]  # type: ignore[index]
    assert isinstance(value, str)
    target[field] = mutate(value)  # type: ignore[index,operator]

    with pytest.raises(ProtocolValidationError):
        parse_record(_raw(mapping))


def test_pairing_challenge_binds_bounded_ca_der_and_declared_leaf(
    pairing_challenge: PairingChallenge,
) -> None:
    with pytest.raises(ProtocolValidationError):
        replace(pairing_challenge, tls_ca_der=pairing_challenge.tls_ca_der + "=")
    with pytest.raises(ProtocolValidationError):
        replace(pairing_challenge, tls_ca_fingerprint="00" * 32)
    with pytest.raises(ProtocolValidationError):
        replace(pairing_challenge, tls_leaf_fingerprint="not-a-digest")
    with pytest.raises(ProtocolValidationError):
        replace(
            pairing_challenge,
            tls_ca_der=base64.urlsafe_b64encode(b"x" * (8 * 1024 + 1))
            .decode("ascii")
            .rstrip("="),
        )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("protocol",), "reach-connector/v2"),
        (("type",), "pairing_init_v2"),
        (("body", "claims", "signature_algorithm"), "ed25519-v2"),
    ],
)
def test_record_parser_rejects_unknown_version_type_and_algorithm(
    pairing_init: PairingInit,
    path: tuple[str, ...],
    value: str,
) -> None:
    mapping = _mapping(pairing_init)
    target: dict[str, object] = mapping
    for name in path[:-1]:
        target = target[name]  # type: ignore[assignment]
    target[path[-1]] = value

    with pytest.raises(ProtocolValidationError):
        parse_record(_raw(mapping))


def test_signature_tamper_and_wrong_key_fail_closed(
    pairing_init: PairingInit,
    vps: DevicePrivateIdentity,
) -> None:
    mapping = _mapping(pairing_init)
    claims = mapping["body"]["claims"]  # type: ignore[index]
    claims["device_label"] = "attacker-vps"  # type: ignore[index]
    tampered = parse_record(_raw(mapping))

    with pytest.raises(ProtocolValidationError):
        verify_pairing_init(tampered, now=NOW)
    with pytest.raises(ProtocolValidationError):
        verify_record(pairing_init, _identity(OTHER_SEED).public_identity)
    assert vps.public_identity.key_id != _identity(OTHER_SEED).public_identity.key_id


def test_pairing_context_verification_binds_complete_transcript_and_grant(
    connector: DevicePrivateIdentity,
    vps: DevicePrivateIdentity,
    pairing_init: PairingInit,
    pairing_challenge: PairingChallenge,
    pairing_complete: PairingComplete,
    signed_grant: SignedGrant,
) -> None:
    assert verify_pairing_init(pairing_init, now=NOW) == vps.public_identity
    assert (
        verify_pairing_challenge(
            pairing_challenge,
            expected_pairing_id=pairing_init.pairing_id,
            expected_vps_key_id=pairing_init.vps_key_id,
            expected_init_digest=record_digest(pairing_init),
            observed_tls_leaf_fingerprint=TLS_LEAF_FINGERPRINT,
            now=NOW + 2,
        )
        == connector.public_identity
    )
    assert pairing_ca_der(pairing_challenge) == TLS_CA_DER
    assert pairing_challenge.tls_ca_fingerprint == TLS_CA_FINGERPRINT
    with pytest.raises(ProtocolValidationError):
        verify_pairing_challenge(
            pairing_challenge,
            expected_pairing_id=pairing_init.pairing_id,
            expected_vps_key_id=pairing_init.vps_key_id,
            expected_init_digest=record_digest(pairing_init),
            observed_tls_leaf_fingerprint="44" * 32,
            now=NOW + 2,
        )
    assert (
        verify_pairing_complete(
            pairing_complete,
            pinned_connector=connector.public_identity,
            expected_pairing_id=pairing_init.pairing_id,
            expected_vps_key_id=pairing_init.vps_key_id,
            expected_transcript_digest=pairing_transcript_hash(
                encode_record(pairing_init),
                encode_record(pairing_challenge),
                observed_tls_leaf_fingerprint=TLS_LEAF_FINGERPRINT,
            ).hex(),
            expected_grant_digest=record_digest(signed_grant),
            expected_deadline=pairing_challenge.deadline,
            now=NOW + 3,
        )
        is None
    )


def test_pairing_resolution_carries_only_exact_signed_approval_records(
    pairing_init: PairingInit,
    pairing_challenge: PairingChallenge,
    pairing_complete: PairingComplete,
    signed_grant: SignedGrant,
) -> None:
    resolution = create_pairing_resolution(
        message_id=FILE_MESSAGE_ID,
        pairing_id=pairing_init.pairing_id,
        signed_grant=signed_grant,
        pairing_complete=pairing_complete,
    )
    assert isinstance(parse_record(encode_record(resolution)), PairingResolution)
    assert parse_record(encode_record(resolution)) == resolution
    assert (
        verify_pairing_resolution(
            resolution,
            pairing_init=pairing_init,
            pairing_challenge=pairing_challenge,
            observed_tls_leaf_fingerprint=TLS_LEAF_FINGERPRINT,
            now=NOW + 3,
        )
        == signed_grant
    )

    with pytest.raises(ProtocolValidationError):
        create_pairing_resolution(
            message_id=FILE_MESSAGE_ID,
            pairing_id=REQUEST_ID,
            signed_grant=signed_grant,
            pairing_complete=pairing_complete,
        )

    tampered = _mapping(resolution)
    body = tampered["body"]
    assert isinstance(body, dict)
    body["signed_grant"] = (
        base64.urlsafe_b64encode(
            encode_record(
                ErrorFrame(
                    message_id=FILE_MESSAGE_ID,
                    code=ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH,
                )
            )
        )
        .decode("ascii")
        .rstrip("=")
    )
    with pytest.raises(ProtocolValidationError):
        parse_record(_raw(tampered))

    with pytest.raises(ProtocolValidationError):
        verify_pairing_resolution(
            resolution,
            pairing_init=pairing_init,
            pairing_challenge=pairing_challenge,
            observed_tls_leaf_fingerprint="44" * 32,
            now=NOW + 3,
        )


def test_pairing_resolution_rejects_a_signed_grant_that_differs_from_display(
    connector: DevicePrivateIdentity,
    pairing_init: PairingInit,
    pairing_challenge: PairingChallenge,
    signed_grant: SignedGrant,
) -> None:
    changed_grant = create_signed_grant(
        signer=connector,
        message_id=signed_grant.message_id,
        claims=replace(signed_grant.claims, max_uses=201),
    )
    changed_complete = create_pairing_complete(
        signer=connector,
        message_id=COMPLETE_MESSAGE_ID,
        pairing_id=pairing_init.pairing_id,
        transcript_digest=pairing_transcript_hash(
            encode_record(pairing_init),
            encode_record(pairing_challenge),
            observed_tls_leaf_fingerprint=TLS_LEAF_FINGERPRINT,
        ).hex(),
        vps_key_id=pairing_init.vps_key_id,
        signed_grant_digest=record_digest(changed_grant),
        completed_at=NOW + 3,
    )
    resolution = create_pairing_resolution(
        message_id=FILE_MESSAGE_ID,
        pairing_id=pairing_init.pairing_id,
        signed_grant=changed_grant,
        pairing_complete=changed_complete,
    )

    with pytest.raises(ProtocolValidationError):
        verify_pairing_resolution(
            resolution,
            pairing_init=pairing_init,
            pairing_challenge=pairing_challenge,
            observed_tls_leaf_fingerprint=TLS_LEAF_FINGERPRINT,
            now=NOW + 3,
        )


def test_pairing_verification_rejects_substituted_init_challenge_or_grant(
    connector: DevicePrivateIdentity,
    vps: DevicePrivateIdentity,
    scope: GrantScope,
    pairing_init: PairingInit,
    pairing_challenge: PairingChallenge,
    pairing_complete: PairingComplete,
    signed_grant: SignedGrant,
) -> None:
    other_init = create_pairing_init(
        signer=vps,
        message_id=REQUEST_MESSAGE_ID,
        pairing_id=REQUEST_ID,
        device_label="reach-vps-2",
        endpoint_digest=ENDPOINT_DIGEST,
        vps_nonce=VPS_NONCE,
        requested_scopes=(scope,),
        grant_expires_at=NOW + 8 * 60 * 60,
        grant_max_uses=200,
        issued_at=NOW,
        deadline=NOW + PAIRING_TTL_SECONDS,
    )
    for override in (
        {"expected_pairing_id": other_init.pairing_id},
        {
            "expected_transcript_digest": pairing_transcript_hash(
                encode_record(other_init),
                encode_record(pairing_challenge),
                observed_tls_leaf_fingerprint=TLS_LEAF_FINGERPRINT,
            ).hex()
        },
        {
            "expected_grant_digest": record_digest(
                replace(signed_grant, message_id=REQUEST_ID)
            )
        },
    ):
        values = {
            "expected_pairing_id": pairing_init.pairing_id,
            "expected_vps_key_id": pairing_init.vps_key_id,
            "expected_transcript_digest": pairing_transcript_hash(
                encode_record(pairing_init),
                encode_record(pairing_challenge),
                observed_tls_leaf_fingerprint=TLS_LEAF_FINGERPRINT,
            ).hex(),
            "expected_grant_digest": record_digest(signed_grant),
            "expected_deadline": pairing_challenge.deadline,
            "now": NOW + 3,
        }
        values.update(override)
        with pytest.raises(ProtocolValidationError):
            verify_pairing_complete(
                pairing_complete,
                **values,
                pinned_connector=connector.public_identity,
            )


def test_pairing_deadline_and_display_label_bounds_fail_closed(
    vps: DevicePrivateIdentity,
    scope: GrantScope,
) -> None:
    base = dict(
        signer=vps,
        message_id=PAIRING_MESSAGE_ID,
        pairing_id=PAIRING_ID,
        device_label="reach-vps-1",
        endpoint_digest=ENDPOINT_DIGEST,
        vps_nonce=VPS_NONCE,
        requested_scopes=(scope,),
        grant_expires_at=NOW + 8 * 60 * 60,
        grant_max_uses=200,
        issued_at=NOW,
        deadline=NOW + PAIRING_TTL_SECONDS,
    )
    for override in (
        {"deadline": NOW - 1},
        {"deadline": NOW + PAIRING_TTL_SECONDS + 1},
        {"device_label": "terminal\x1b[31mspoof"},
        {"device_label": "\N{LATIN SMALL LETTER E WITH ACUTE}"},
        {"device_label": " "},
    ):
        with pytest.raises(ProtocolValidationError):
            create_pairing_init(**(base | override))


def test_grant_scope_requires_exact_catalog_scope_and_canonical_order(
    connector: DevicePrivateIdentity,
    vps: DevicePrivateIdentity,
    scope: GrantScope,
) -> None:
    rss = GrantScope("rss", "read.feed", "public", None)
    with pytest.raises(ProtocolValidationError):
        GrantScope("web", "read.url", "account_visible", None)
    with pytest.raises(ProtocolValidationError):
        GrantScope("unknown", "read.url", "public", None)
    with pytest.raises(ProtocolValidationError):
        GrantScope("web", "read.url", "public", "bitwarden-project-canary")
    with pytest.raises(ProtocolValidationError):
        replace(
            GrantClaims(
                grant_id=GRANT_ID,
                revision=1,
                issuer_key_id=connector.public_identity.key_id,
                subject_key_id=vps.public_identity.key_id,
                issued_at=NOW,
                not_before=NOW,
                expires_at=NOW + 60,
                policy_revision=1,
                max_uses=1,
                scopes=(rss, scope),
                signature_algorithm="ed25519-v1",
            ),
            scopes=(scope, rss),
        )


def test_grant_claims_reject_duplicate_scope_invalid_time_and_integer_bounds(
    grant_claims: GrantClaims,
    scope: GrantScope,
) -> None:
    invalid = (
        {"revision": 0},
        {"revision": True},
        {"policy_revision": 0},
        {"issued_at": -1},
        {"expires_at": MAX_TIMESTAMP_SECONDS + 1},
        {"not_before": grant_claims.issued_at - 1},
        {"expires_at": grant_claims.issued_at},
        {"expires_at": grant_claims.issued_at + MAX_GRANT_TTL_SECONDS + 1},
        {"max_uses": 0},
        {"max_uses": MAX_GRANT_USES + 1},
        {"scopes": (scope, scope)},
    )
    for values in invalid:
        with pytest.raises(ProtocolValidationError):
            replace(grant_claims, **values)


def test_signed_grant_verification_binds_issuer_subject_and_active_window(
    connector: DevicePrivateIdentity,
    vps: DevicePrivateIdentity,
    signed_grant: SignedGrant,
) -> None:
    assert (
        verify_signed_grant(
            signed_grant,
            pinned_connector=connector.public_identity,
            expected_subject_key_id=vps.public_identity.key_id,
            now=NOW + 10,
        )
        is None
    )
    for subject, now, identity in (
        (_identity(OTHER_SEED).public_identity.key_id, NOW + 10, connector),
        (vps.public_identity.key_id, NOW - 29, connector),
        (vps.public_identity.key_id, NOW + 8 * 60 * 60 + 1, connector),
        (vps.public_identity.key_id, NOW + 10, _identity(OTHER_SEED)),
    ):
        with pytest.raises(ProtocolValidationError):
            verify_signed_grant(
                signed_grant,
                pinned_connector=identity.public_identity,
                expected_subject_key_id=subject,
                now=now,
            )


def test_protected_operation_call_has_fixed_projection_digest_and_redaction(
    protected: ProtectedOperationPayload,
) -> None:
    expected = (
        b'{"operation":"read.url","options":{},"query":null,"source":"web",'
        b'"target":{"url":"https://example.invalid/article?QUERY_CANARY=TOKEN_CANARY"}}'
    )

    assert protected.transport_bytes() == expected
    assert operation_payload_digest(protected) == PAYLOAD_DIGEST
    parsed = parse_protected_operation(expected)
    assert parsed.transport_bytes() == protected.transport_bytes()
    assert parsed.to_operation_call() == protected.to_operation_call()
    assert QUERY_CANARY not in repr(protected)
    assert QUERY_CANARY not in str(protected)
    with pytest.raises(TypeError):
        json.dumps(protected)


def test_protected_operation_rejects_noncanonical_tamper_and_raw_local_path(
    protected: ProtectedOperationPayload,
) -> None:
    with pytest.raises(ProtocolValidationError):
        parse_protected_operation(protected.transport_bytes() + b" ")
    with pytest.raises(ProtocolValidationError):
        parse_protected_operation(
            protected.transport_bytes().replace(b"read.url", b"write.url")
        )
    local = validate_transcribe(
        {
            "source": "xiaoyuzhou",
            "operation": "transcribe.episode",
            "target": {"local_file": "/private/PATH_CANARY/audio.wav"},
        }
    )
    with pytest.raises(ProtocolValidationError):
        protect_operation_call(local)


def test_signed_request_verification_binds_key_audience_operation_time_and_digest(
    vps: DevicePrivateIdentity,
    connector: DevicePrivateIdentity,
    signed_request: SignedRequest,
    protected: ProtectedOperationPayload,
) -> None:
    assert (
        verify_signed_request(
            signed_request,
            pinned_vps=vps.public_identity,
            expected_connector_key_id=connector.public_identity.key_id,
            protected_payload=protected,
            now=NOW + 5,
        )
        is None
    )
    other_payload = protect_operation_call(
        validate_read(
            {
                "source": "rss",
                "operation": "read.feed",
                "target": {"url": "https://example.invalid/feed.xml"},
            }
        )
    )
    attempts = (
        (vps, _identity(OTHER_SEED).public_identity.key_id, protected, NOW + 5),
        (_identity(OTHER_SEED), connector.public_identity.key_id, protected, NOW + 5),
        (vps, connector.public_identity.key_id, other_payload, NOW + 5),
        (vps, connector.public_identity.key_id, protected, NOW - 27),
        (vps, connector.public_identity.key_id, protected, NOW + 60),
    )
    for identity, audience, payload, now in attempts:
        with pytest.raises(ProtocolValidationError):
            verify_signed_request(
                signed_request,
                pinned_vps=identity.public_identity,
                expected_connector_key_id=audience,
                protected_payload=payload,
                now=now,
            )


@pytest.mark.parametrize(
    ("backend_id", "version"),
    [
        ("unknown-backend", "1"),
        ("bitwarden-project", "1"),
        ("provider-account", "1"),
        ("secret-selector", "1"),
        ("web-public-http-v1", "TOKEN_CANARY"),
        ("web-public-http-v1/path", "1"),
        ("https://backend.invalid", "1"),
    ],
)
def test_public_backend_identity_is_an_explicit_allowlist(
    backend_id: str,
    version: str,
) -> None:
    with pytest.raises(ProtocolValidationError):
        PublicBackendIdentity(backend_id, version)


def test_receipt_failure_category_and_success_backend_are_consistent(
    signed_receipt: SignedReceipt,
) -> None:
    with pytest.raises(ProtocolValidationError):
        ReceiptFailure(
            failure_class="authority",
            cause_code=ConnectorErrorCode.SECRET_UNAVAILABLE,
        )
    with pytest.raises(ProtocolValidationError):
        replace(signed_receipt, backend=None)


def test_receipt_contains_no_protected_values_and_verifies_exact_request_context(
    connector: DevicePrivateIdentity,
    vps: DevicePrivateIdentity,
    signed_request: SignedRequest,
    signed_receipt: SignedReceipt,
    protected: ProtectedOperationPayload,
) -> None:
    encoded = encode_record(signed_receipt)
    assert QUERY_CANARY.encode() not in encoded
    assert b'"query"' not in encoded
    assert b'"target"' not in encoded
    assert b'"options"' not in encoded
    assert b"https://" not in encoded
    assert (
        verify_signed_receipt(
            signed_receipt,
            pinned_connector=connector.public_identity,
            request=signed_request,
            now=NOW + 7,
        )
        is None
    )
    assert signed_request.payload_digest == operation_payload_digest(protected)


def test_denied_receipt_uses_closed_failure_metadata_without_result_or_backend(
    connector: DevicePrivateIdentity,
    signed_request: SignedRequest,
) -> None:
    receipt = create_signed_receipt(
        signer=connector,
        message_id=COMPLETE_MESSAGE_ID,
        receipt_id=FILE_GRANT_ID,
        request=signed_request,
        decision="deny",
        failure=ReceiptFailure(
            failure_class="authority",
            cause_code=ConnectorErrorCode.GRANT_SCOPE_DENIED,
        ),
        usage=None,
        backend=None,
        started_at=NOW + 5,
        ended_at=NOW + 5,
        expires_at=NOW + 305,
        result_count=0,
        truncated=False,
        outcome="error",
    )
    encoded = encode_record(receipt)

    assert b"grant_scope_denied" in encoded
    assert b"remediation" not in encoded
    assert b'"message":' not in encoded
    assert b"provider" not in encoded
    assert (
        verify_signed_receipt(
            receipt,
            pinned_connector=connector.public_identity,
            request=signed_request,
            now=NOW + 6,
        )
        is None
    )


def test_receipt_verification_rejects_wrong_key_subject_request_and_expiry(
    connector: DevicePrivateIdentity,
    vps: DevicePrivateIdentity,
    signed_request: SignedRequest,
    signed_receipt: SignedReceipt,
    protected: ProtectedOperationPayload,
) -> None:
    other_request = create_signed_request(
        signer=vps,
        message_id=COMPLETE_MESSAGE_ID,
        request_id=FILE_GRANT_ID,
        trace_id="fedcba9876543210fedcba9876543210",
        audience_key_id=connector.public_identity.key_id,
        grant_id=GRANT_ID,
        grant_revision=1,
        policy_revision=1,
        source="web",
        operation="read.url",
        issued_at=NOW + 4,
        deadline=NOW + 34,
        protected_payload=protected,
    )
    attempts = (
        (_identity(OTHER_SEED), signed_request, NOW + 7),
        (
            connector,
            replace(
                signed_request,
                subject_key_id=_identity(OTHER_SEED).public_identity.key_id,
            ),
            NOW + 7,
        ),
        (connector, other_request, NOW + 7),
        (connector, signed_request, NOW + 307),
    )
    for identity, request, now in attempts:
        with pytest.raises(ProtocolValidationError):
            verify_signed_receipt(
                signed_receipt,
                pinned_connector=identity.public_identity,
                request=request,
                now=now,
            )


def test_receipt_creation_binds_request_audience(
    connector: DevicePrivateIdentity,
    signed_request: SignedRequest,
) -> None:
    other_connector = _identity(OTHER_SEED)
    request_for_other = replace(
        signed_request,
        audience_key_id=other_connector.public_identity.key_id,
    )

    with pytest.raises(ProtocolValidationError):
        create_signed_receipt(
            signer=connector,
            message_id=RECEIPT_MESSAGE_ID,
            receipt_id=RECEIPT_ID,
            request=request_for_other,
            decision="deny",
            failure=ReceiptFailure(
                failure_class="authority",
                cause_code=ConnectorErrorCode.GRANT_SCOPE_DENIED,
            ),
            usage=None,
            backend=None,
            started_at=NOW + 5,
            ended_at=NOW + 5,
            expires_at=NOW + 305,
            result_count=0,
            truncated=False,
            outcome="error",
        )


def test_receipt_verification_rejects_a_request_with_b_signed_receipt(
    connector: DevicePrivateIdentity,
    signed_request: SignedRequest,
) -> None:
    other_connector = _identity(OTHER_SEED)
    assert signed_request.audience_key_id == connector.public_identity.key_id
    request_for_other = replace(
        signed_request,
        audience_key_id=other_connector.public_identity.key_id,
    )
    receipt_from_other = create_signed_receipt(
        signer=other_connector,
        message_id=RECEIPT_MESSAGE_ID,
        receipt_id=RECEIPT_ID,
        request=request_for_other,
        decision="deny",
        failure=ReceiptFailure(
            failure_class="authority",
            cause_code=ConnectorErrorCode.GRANT_SCOPE_DENIED,
        ),
        usage=None,
        backend=None,
        started_at=NOW + 5,
        ended_at=NOW + 5,
        expires_at=NOW + 305,
        result_count=0,
        truncated=False,
        outcome="error",
    )
    with pytest.raises(ProtocolValidationError):
        verify_signed_receipt(
            receipt_from_other,
            pinned_connector=other_connector.public_identity,
            request=signed_request,
            now=NOW + 6,
        )


@pytest.mark.parametrize(
    ("ended_at", "verified_at"),
    [
        pytest.param(NOW + 35, NOW + 33, id="ended-after-deadline"),
        pytest.param(NOW + 34, NOW + 33, id="ended-at-deadline"),
        pytest.param(NOW + 33, NOW + 34, id="accepted-at-deadline"),
    ],
)
def test_successful_receipt_must_finish_and_arrive_before_request_deadline(
    connector: DevicePrivateIdentity,
    signed_request: SignedRequest,
    ended_at: int,
    verified_at: int,
) -> None:
    receipt = create_signed_receipt(
        signer=connector,
        message_id=RECEIPT_MESSAGE_ID,
        receipt_id=RECEIPT_ID,
        request=signed_request,
        decision="allow",
        failure=None,
        usage=ReceiptUsage(sequence=1, remaining=199),
        backend=PublicBackendIdentity("reach-bounded-executor-v1", "1"),
        started_at=NOW + 5,
        ended_at=ended_at,
        expires_at=ended_at + 300,
        result_count=1,
        truncated=False,
        outcome="ok",
    )

    with pytest.raises(ProtocolValidationError):
        verify_signed_receipt(
            receipt,
            pinned_connector=connector.public_identity,
            request=signed_request,
            now=verified_at,
        )


def test_file_grant_is_path_free_single_use_transcribe_authority(
    connector: DevicePrivateIdentity,
    vps: DevicePrivateIdentity,
    file_grant: FileGrant,
) -> None:
    encoded = encode_record(file_grant)
    assert b"path" not in encoded
    assert b"basename" not in encoded
    assert b"local_file" not in encoded
    assert _mapping(file_grant)["body"]["claims"]["single_use"] is True  # type: ignore[index]
    assert (
        verify_file_grant(
            file_grant,
            pinned_connector=connector.public_identity,
            expected_subject_key_id=vps.public_identity.key_id,
            expected_grant_revision=1,
            expected_policy_revision=1,
            expected_source="youtube",
            expected_operation="transcribe.video",
            now=NOW + 1,
        )
        is None
    )


def test_file_grant_rejects_wrong_operation_subject_revision_expiry_and_size(
    connector: DevicePrivateIdentity,
    vps: DevicePrivateIdentity,
    file_grant: FileGrant,
) -> None:
    base = dict(
        record=file_grant,
        pinned_connector=connector.public_identity,
        expected_subject_key_id=vps.public_identity.key_id,
        expected_grant_revision=1,
        expected_policy_revision=1,
        expected_source="youtube",
        expected_operation="transcribe.video",
        now=NOW + 1,
    )
    for override in (
        {"expected_subject_key_id": _identity(OTHER_SEED).public_identity.key_id},
        {"expected_grant_revision": 2},
        {"expected_policy_revision": 2},
        {"expected_operation": "read.video"},
        {"now": NOW + 10 * 60 + 1},
    ):
        with pytest.raises(ProtocolValidationError):
            verify_file_grant(**(base | override))
    for size in (0, -1, True, 1 << 63):
        with pytest.raises(ProtocolValidationError):
            create_file_grant(
                signer=connector,
                message_id=FILE_MESSAGE_ID,
                file_grant_id=FILE_GRANT_ID,
                subject_key_id=vps.public_identity.key_id,
                digest=FILE_DIGEST,
                size=size,
                source="youtube",
                operation="transcribe.video",
                grant_revision=1,
                policy_revision=1,
                issued_at=NOW,
                expires_at=NOW + 60,
            )


def test_error_frame_is_closed_and_carries_no_free_form_text() -> None:
    frame = ErrorFrame(
        message_id=REQUEST_MESSAGE_ID,
        code=ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH,
    )
    mapping = _mapping(frame)

    assert mapping["body"] == {"code": "connector_protocol_mismatch"}
    mapping["body"] = {"code": "provider_error_SECRET_CANARY"}
    with pytest.raises(ProtocolValidationError):
        parse_record(_raw(mapping))


def test_transcript_hash_is_length_delimited_and_sas_is_exact_crockford(
    pairing_init: PairingInit,
    pairing_challenge: PairingChallenge,
) -> None:
    init_bytes = encode_record(pairing_init)
    challenge_bytes = encode_record(pairing_challenge)
    digest = pairing_transcript_hash(
        init_bytes,
        challenge_bytes,
        observed_tls_leaf_fingerprint=TLS_LEAF_FINGERPRINT,
    )

    assert len(digest) == 32
    other_init = encode_record(replace(pairing_init, message_id=REQUEST_ID))
    other_challenge = encode_record(
        replace(pairing_challenge, message_id=REQUEST_MESSAGE_ID)
    )
    assert digest != pairing_transcript_hash(
        other_init,
        challenge_bytes,
        observed_tls_leaf_fingerprint=TLS_LEAF_FINGERPRINT,
    )
    assert digest != pairing_transcript_hash(
        init_bytes,
        other_challenge,
        observed_tls_leaf_fingerprint=TLS_LEAF_FINGERPRINT,
    )
    assert digest != pairing_transcript_hash(
        init_bytes,
        challenge_bytes,
        observed_tls_leaf_fingerprint="44" * 32,
    )
    transcript = FROZEN_PROTOCOL_VECTORS["pairing_transcript"]
    assert digest.hex() == transcript["hash_hex"]
    assert pairing_sas(digest) == transcript["sas"]
    assert (
        pairing_sas(
            bytes.fromhex(
                "f16d05ec6b29248d2c61adb1e9263f78e4f7bace1b955014a2d17872cfe4064d"
            )
        )
        == "Y5PGB-V3B54"
    )
    sas = pairing_sas(digest)
    assert len(sas) == 11
    assert sas[5] == "-"
    assert set(sas.replace("-", "")) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


@pytest.mark.parametrize(
    "identifier",
    [
        PAIRING_ID.upper(),
        PAIRING_ID[:-1],
        PAIRING_ID + "a",
        PAIRING_ID[:-1] + "5",  # Same decoded 128 bits, noncanonical low bits.
        "0" * 26,
        "!" + PAIRING_ID[1:],
    ],
)
def test_random_ids_require_canonical_128_bit_lowercase_base32(
    vps: DevicePrivateIdentity,
    scope: GrantScope,
    identifier: str,
) -> None:
    with pytest.raises(ProtocolValidationError):
        create_pairing_init(
            signer=vps,
            message_id=PAIRING_MESSAGE_ID,
            pairing_id=identifier,
            device_label="reach-vps-1",
            endpoint_digest=ENDPOINT_DIGEST,
            vps_nonce=VPS_NONCE,
            requested_scopes=(scope,),
            grant_expires_at=NOW + 60,
            grant_max_uses=1,
            issued_at=NOW,
            deadline=NOW + 60,
        )
