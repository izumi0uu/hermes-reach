from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hermes_reach.runtime.tokens import TokenCodec, TokenError


def test_resource_references_are_opaque_and_round_trip() -> None:
    now = datetime(2026, 7, 23, tzinfo=UTC)
    codec = TokenCodec(b"r" * 32, clock=lambda: now)
    target = {"url": "https://example.test/article?private=query"}

    token = codec.mint_resource_ref(
        "github", "read.repository", target, "backend-v1", 60
    )
    payload = codec.decode_resource_ref(token, "github", "read.repository", target)

    assert "example.test" not in token
    assert "private=query" not in token
    assert payload.target == target
    assert payload.cursor is None
    assert (payload.maximum_items, payload.maximum_characters) == (20, 16_000)


def test_tokens_reject_tampering_expiry_wrong_operation_and_target_changes() -> None:
    state = {"now": datetime(2026, 7, 23, tzinfo=UTC)}
    codec = TokenCodec(b"s" * 32, clock=lambda: state["now"])
    target = {"native_id": "public-id"}
    token = codec.mint_resource_ref("github", "read.repository", target, "backend", 60)

    with pytest.raises(TokenError) as tampered:
        codec.decode_resource_ref(token[:-1] + "A", "github", "read.repository")
    assert tampered.value.code == "resource_ref_invalid"

    with pytest.raises(TokenError) as wrong_operation:
        codec.decode_resource_ref(token, "github", "read.issue")
    assert wrong_operation.value.code == "resource_ref_invalid"

    with pytest.raises(TokenError) as changed:
        codec.decode_resource_ref(
            token, "github", "read.repository", {"native_id": "changed"}
        )
    assert changed.value.code == "resource_changed"

    state["now"] += timedelta(seconds=60)
    with pytest.raises(TokenError) as expired:
        codec.decode_resource_ref(token, "github", "read.repository")
    assert expired.value.code == "resource_ref_expired"


def test_continuations_round_trip_and_malformed_targets_are_redacted() -> None:
    now = datetime(2026, 7, 23, tzinfo=UTC)
    codec = TokenCodec(b"c" * 32, clock=lambda: now)
    target = {"native_id": "public-id"}
    token = codec.mint_continuation(
        "github", "search.repositories", target, "backend", 60, "cursor-1"
    )

    payload = codec.decode_continuation(token, "github", "search.repositories", target)
    assert payload.cursor == "cursor-1"

    with pytest.raises(TokenError) as invalid:
        codec.decode_continuation(
            token,
            "github",
            "search.repositories",
            ["not-a-target"],  # type: ignore[arg-type]
        )
    assert invalid.value.code == "continuation_invalid"
    assert "not-a-target" not in str(invalid.value)
