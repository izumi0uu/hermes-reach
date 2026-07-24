"""Strict V2EX public API adapter with fixed read-only routes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final
from urllib.parse import urlencode

from ..runtime.adapters import AdapterResult, RawItem
from ..runtime.policy import AuthorizedCall
from .documents import MAX_DECODED_CHARACTERS, normalize_whitespace
from .public_http import HttpFailure, HttpResponse, PublicHttpClient

_BASE: Final = "https://www.v2ex.com"
_HOT_PATH: Final = "/api/topics/hot.json"
_TOPICS_PATH: Final = "/api/topics/show.json"
_REPLIES_PATH: Final = "/api/replies/show.json"
_MEMBER_PATH: Final = "/api/members/show.json"


class V2exDataError(Exception):
    """A safe upstream shape error without payload content."""


class V2exAdapter:
    def __init__(self, client: PublicHttpClient) -> None:
        self._client = client

    async def execute(self, authorized: AuthorizedCall) -> AdapterResult:
        operation = authorized.operation.name
        try:
            if operation == "browse.hot":
                response = await self._client.get(f"{_BASE}{_HOT_PATH}")
                items = _topics(_json(response))
                return AdapterResult(tuple(items[: _limit(authorized)]))
            if operation == "browse.node_topics":
                node = _string_option(authorized.call.options, "node")
                page = _integer_option(authorized.call.options, "page", 1)
                query = urlencode({"node_name": node, "page": page})
                response = await self._client.get(f"{_BASE}{_TOPICS_PATH}?{query}")
                items = _topics(_json(response))
                return AdapterResult(tuple(items[: _limit(authorized)]))
            if operation == "read.topic":
                return await self._topic(authorized)
            if operation == "read.user":
                return await self._user(authorized)
            return AdapterResult(failure_class="invalid_input")
        except HttpFailure as error:
            return AdapterResult(failure_class=error.failure_class)
        except (UnicodeError, json.JSONDecodeError, V2exDataError, ValueError):
            return AdapterResult(failure_class="permanent")
        except Exception:
            return AdapterResult(failure_class="transient")

    async def _topic(self, authorized: AuthorizedCall) -> AdapterResult:
        topic_id = _native_id(authorized)
        topic_query = urlencode({"id": topic_id})
        response = await self._client.get(f"{_BASE}{_TOPICS_PATH}?{topic_query}")
        values = _list(_json(response))
        if not values:
            return AdapterResult(failure_class="not_found")
        if len(values) != 1:
            return AdapterResult(failure_class="permanent")
        topic = _topic(values[0])
        replies_query = urlencode({"topic_id": topic_id, "page": 1})
        try:
            replies_response = await self._client.get(
                f"{_BASE}{_REPLIES_PATH}?{replies_query}"
            )
            replies = tuple(_replies(_json(replies_response), topic_id))
        except HttpFailure as error:
            return AdapterResult((topic,), partial_failure_class=error.failure_class)
        except (UnicodeError, json.JSONDecodeError, V2exDataError, ValueError):
            return AdapterResult((topic,), partial_failure_class="permanent")
        except Exception:
            return AdapterResult((topic,), partial_failure_class="transient")
        return AdapterResult((topic, *replies))

    async def _user(self, authorized: AuthorizedCall) -> AdapterResult:
        username = _native_id(authorized)
        query = urlencode({"username": username})
        response = await self._client.get(f"{_BASE}{_MEMBER_PATH}?{query}")
        return AdapterResult((_user(_json(response), username),))


def _json(response: HttpResponse) -> object:
    media_type = response.content_type.split(";", 1)[0].strip()
    if media_type not in {"", "application/json", "text/json"}:
        raise V2exDataError("json_content_type_invalid")
    text = response.body.decode("utf-8", "strict")
    if len(text) > MAX_DECODED_CHARACTERS:
        raise V2exDataError("json_character_limit")
    return json.loads(text, parse_constant=_reject_json_constant)


def _reject_json_constant(value: str) -> object:
    del value
    raise V2exDataError("json_constant_invalid")


def _topics(value: object) -> list[RawItem]:
    return [_topic(item) for item in _list(value)]


def _topic(value: object) -> RawItem:
    item = _mapping(value)
    topic_id = _positive_integer(item, "id")
    title = _required_string(item, "title")
    text = normalize_whitespace(_optional_string(item, "content"))
    member = _optional_mapping(item, "member")
    author = _optional_string(member, "username") if member is not None else ""
    created = _optional_integer(item, "created")
    return RawItem(
        text=text,
        native_id=str(topic_id),
        kind="topic",
        title=title,
        url=f"{_BASE}/t/{topic_id}",
        author=author or None,
        published_at=_timestamp(created),
    )


def _replies(value: object, topic_id: str) -> list[RawItem]:
    results: list[RawItem] = []
    for raw in _list(value):
        item = _mapping(raw)
        reply_id = _positive_integer(item, "id")
        content = normalize_whitespace(_required_string(item, "content"))
        member = _mapping(item.get("member"))
        author = _required_string(member, "username")
        created = _optional_integer(item, "created")
        results.append(
            RawItem(
                text=content,
                native_id=str(reply_id),
                kind="reply",
                url=f"{_BASE}/t/{topic_id}#reply{reply_id}",
                author=author,
                published_at=_timestamp(created),
            )
        )
    return results


def _user(value: object, requested_username: str) -> RawItem:
    item = _mapping(value)
    username = _required_string(item, "username")
    if username.lower() != requested_username.lower():
        raise V2exDataError("member_identity_mismatch")
    member_id = _positive_integer(item, "id")
    text_parts = [
        _optional_string(item, name)
        for name in ("bio", "location", "website", "github")
    ]
    return RawItem(
        text=normalize_whitespace(" ".join(part for part in text_parts if part)),
        native_id=str(member_id),
        kind="profile",
        title=username,
        url=f"{_BASE}/member/{username}",
        published_at=_timestamp(_optional_integer(item, "created")),
    )


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise V2exDataError("json_object_required")
    return value


def _optional_mapping(
    value: Mapping[str, object], name: str
) -> Mapping[str, object] | None:
    raw = value.get(name)
    if raw is None:
        return None
    return _mapping(raw)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise V2exDataError("json_list_required")
    return value


def _required_string(value: Mapping[str, object], name: str) -> str:
    raw = value.get(name)
    if not isinstance(raw, str) or not raw.strip():
        raise V2exDataError("json_string_required")
    return raw.strip()


def _optional_string(value: Mapping[str, object], name: str) -> str:
    raw = value.get(name)
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise V2exDataError("json_string_invalid")
    return raw.strip()


def _positive_integer(value: Mapping[str, object], name: str) -> int:
    raw = value.get(name)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise V2exDataError("json_integer_required")
    return raw


def _optional_integer(value: Mapping[str, object], name: str) -> int | None:
    raw = value.get(name)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise V2exDataError("json_integer_invalid")
    return raw


def _native_id(authorized: AuthorizedCall) -> str:
    target = authorized.call.target
    if target is None or "native_id" not in target:
        raise V2exDataError("native_id_required")
    return target["native_id"]


def _string_option(options: Mapping[str, object], name: str) -> str:
    value = options.get(name)
    if not isinstance(value, str) or not value:
        raise V2exDataError("string_option_required")
    return value


def _integer_option(options: Mapping[str, object], name: str, default: int) -> int:
    value = options.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise V2exDataError("integer_option_invalid")
    return value


def _limit(authorized: AuthorizedCall) -> int:
    return _integer_option(
        authorized.call.options,
        "limit",
        authorized.operation.runtime.maximum_items,
    )


def _timestamp(value: int | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, UTC).isoformat()
    except (OverflowError, OSError, ValueError) as error:
        raise V2exDataError("timestamp_invalid") from error
