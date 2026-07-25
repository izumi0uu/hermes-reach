"""Fixed-route, anonymous GitHub REST adapter."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Final
from urllib.parse import quote, urlencode

from ..runtime.adapters import AdapterResult, ItemKind, RawItem
from ..runtime.policy import AuthorizedCall
from .documents import MAX_DECODED_CHARACTERS, normalize_whitespace
from .public_http import HttpFailure, HttpResponse, PublicHttpClient

_BASE: Final = "https://api.github.com"
_WEB_BASE: Final = "https://github.com"
_GITHUB_OWNER: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
_GITHUB_REPOSITORY: Final = re.compile(r"[A-Za-z0-9._-]{1,100}")
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")


class GitHubDataError(Exception):
    """A closed upstream-shape error that does not retain GitHub payloads."""


class GitHubAdapter:
    """Execute only the eight catalog-owned anonymous GitHub GET operations."""

    def __init__(self, client: PublicHttpClient) -> None:
        self._client = client

    async def execute(self, authorized: AuthorizedCall) -> AdapterResult:
        try:
            operation = authorized.operation.name
            if operation == "search.repositories":
                return await self._search(authorized, "repositories")
            if operation == "search.code":
                return await self._search(authorized, "code")
            if operation == "read.repository":
                repository = _repository_target(authorized)
                response = await self._client.get(f"{_BASE}/repos/{repository}")
                return AdapterResult((_repository(_json(response), repository),))
            if operation in {"read.issue", "read.pull_request"}:
                repository, item_id = _resource_target(authorized)
                resource = "issues" if operation == "read.issue" else "pulls"
                response = await self._client.get(
                    f"{_BASE}/repos/{repository}/{resource}/{item_id}"
                )
                return AdapterResult(
                    (_issue_or_pull(_json(response), repository, item_id, resource),)
                )
            if operation == "browse.actions":
                repository = _repository_target(authorized)
                response = await self._client.get(
                    f"{_BASE}/repos/{repository}/actions/runs?{_per_page(authorized)}"
                )
                return AdapterResult(
                    tuple(
                        _action_runs(_json(response), repository)[: _limit(authorized)]
                    )
                )
            if operation == "read.action_run":
                repository, run_id = _resource_target(authorized)
                response = await self._client.get(
                    f"{_BASE}/repos/{repository}/actions/runs/{run_id}"
                )
                return AdapterResult(
                    (_action_run(_json(response), repository, run_id),)
                )
            if operation == "browse.releases":
                repository = _repository_target(authorized)
                response = await self._client.get(
                    f"{_BASE}/repos/{repository}/releases?{_per_page(authorized)}"
                )
                return AdapterResult(
                    tuple(_releases(_json(response), repository)[: _limit(authorized)])
                )
            return AdapterResult(failure_class="invalid_input")
        except HttpFailure as error:
            return AdapterResult(failure_class=error.failure_class)
        except (UnicodeError, json.JSONDecodeError, GitHubDataError, ValueError):
            return AdapterResult(failure_class="permanent")
        except Exception:
            return AdapterResult(failure_class="transient")

    async def _search(self, authorized: AuthorizedCall, kind: str) -> AdapterResult:
        query = authorized.call.query
        if query is None:
            return AdapterResult(failure_class="invalid_input")
        parameters = urlencode((("q", query), ("per_page", str(_limit(authorized)))))
        response = await self._client.get(f"{_BASE}/search/{kind}?{parameters}")
        value = _mapping(_json(response))
        items = _list(value.get("items"))
        if kind == "repositories":
            return AdapterResult(
                tuple(_repository(item, None, kind="result") for item in items)
            )
        return AdapterResult(tuple(_code_result(item) for item in items))


def _json(response: HttpResponse) -> object:
    media_type = response.content_type.split(";", 1)[0].strip().lower()
    if media_type not in {
        "application/json",
        "application/vnd.github+json",
        "application/vnd.github.v3+json",
    }:
        raise GitHubDataError("json_content_type_invalid")
    text = response.body.decode("utf-8", "strict")
    if len(text) > MAX_DECODED_CHARACTERS:
        raise GitHubDataError("json_character_limit")
    return json.loads(text, parse_constant=_reject_json_constant)


def _reject_json_constant(value: str) -> object:
    del value
    raise GitHubDataError("json_constant_invalid")


def _repository(
    value: object, expected: str | None, *, kind: ItemKind = "content"
) -> RawItem:
    item = _mapping(value)
    full_name = _required_string(item, "full_name")
    if _repository_segments(full_name) is None or (
        expected is not None and full_name.lower() != expected.lower()
    ):
        raise GitHubDataError("repository_identity_invalid")
    name = _optional_string(item, "name") or full_name
    owner = _optional_mapping(item, "owner")
    author = _optional_string(owner, "login") if owner is not None else None
    return RawItem(
        text=normalize_whitespace(_optional_string(item, "description") or ""),
        native_id=full_name,
        kind=kind,
        title=name,
        url=f"{_WEB_BASE}/{full_name}",
        author=author,
        published_at=_optional_string(item, "updated_at"),
    )


def _code_result(value: object) -> RawItem:
    item = _mapping(value)
    path = _required_string(item, "path")
    if len(path) > 1024 or _CONTROL.search(path):
        raise GitHubDataError("code_path_invalid")
    repository = _mapping(item.get("repository"))
    full_name = _required_string(repository, "full_name")
    if _repository_segments(full_name) is None:
        raise GitHubDataError("repository_identity_invalid")
    return RawItem(
        text=_optional_string(item, "name") or path,
        native_id=f"{full_name}:{path}",
        kind="result",
        title=path,
    )


def _issue_or_pull(
    value: object, repository: str, expected_id: int, resource: str
) -> RawItem:
    item = _mapping(value)
    item_id = _positive_integer(item, "number")
    if item_id != expected_id:
        raise GitHubDataError("resource_identity_mismatch")
    title = _required_string(item, "title")
    user = _optional_mapping(item, "user")
    author = _optional_string(user, "login") if user is not None else None
    route = "issues" if resource == "issues" else "pull"
    return RawItem(
        text=normalize_whitespace(_optional_string(item, "body") or ""),
        native_id=f"{repository}#{item_id}",
        kind="entry",
        title=title,
        url=f"{_WEB_BASE}/{repository}/{route}/{item_id}",
        author=author,
        published_at=_optional_string(item, "created_at"),
    )


def _action_runs(value: object, repository: str) -> list[RawItem]:
    container = _mapping(value)
    return [
        _action_run(item, repository, None)
        for item in _list(container.get("workflow_runs"))
    ]


def _action_run(value: object, repository: str, expected_id: int | None) -> RawItem:
    item = _mapping(value)
    run_id = _positive_integer(item, "id")
    if expected_id is not None and run_id != expected_id:
        raise GitHubDataError("action_identity_mismatch")
    name = _required_string(item, "name")
    title = _optional_string(item, "display_title") or name
    return RawItem(
        text=normalize_whitespace(
            " ".join(
                value
                for value in (
                    _optional_string(item, "status"),
                    _optional_string(item, "conclusion"),
                )
                if value
            )
        ),
        native_id=f"{repository}#{run_id}",
        kind="entry",
        title=title,
        url=f"{_WEB_BASE}/{repository}/actions/runs/{run_id}",
        published_at=_optional_string(item, "created_at"),
    )


def _releases(value: object, repository: str) -> list[RawItem]:
    return [_release(item, repository) for item in _list(value)]


def _release(value: object, repository: str) -> RawItem:
    item = _mapping(value)
    release_id = _positive_integer(item, "id")
    tag = _required_string(item, "tag_name")
    if len(tag) > 256 or _CONTROL.search(tag):
        raise GitHubDataError("release_tag_invalid")
    author_data = _optional_mapping(item, "author")
    author = _optional_string(author_data, "login") if author_data is not None else None
    return RawItem(
        text=normalize_whitespace(_optional_string(item, "body") or ""),
        native_id=str(release_id),
        kind="entry",
        title=_optional_string(item, "name") or tag,
        url=f"{_WEB_BASE}/{repository}/releases/tag/{quote(tag, safe='')}",
        author=author,
        published_at=_optional_string(item, "published_at"),
    )


def _repository_target(authorized: AuthorizedCall) -> str:
    target = authorized.call.target
    if target is None:
        raise GitHubDataError("repository_target_missing")
    repository = target.get("native_id")
    if not isinstance(repository, str) or _repository_segments(repository) is None:
        raise GitHubDataError("repository_target_invalid")
    return repository


def _resource_target(authorized: AuthorizedCall) -> tuple[str, int]:
    target = authorized.call.target
    if target is None:
        raise GitHubDataError("resource_target_missing")
    value = target.get("native_id")
    if not isinstance(value, str):
        raise GitHubDataError("resource_target_invalid")
    repository, separator, raw_id = value.partition("#")
    if (
        not separator
        or _repository_segments(repository) is None
        or not raw_id.isdigit()
    ):
        raise GitHubDataError("resource_target_invalid")
    item_id = int(raw_id)
    if item_id <= 0:
        raise GitHubDataError("resource_target_invalid")
    return repository, item_id


def _repository_segments(value: str) -> tuple[str, str] | None:
    parts = value.split("/")
    if len(parts) != 2:
        return None
    owner, repository = parts
    if not _GITHUB_OWNER.fullmatch(owner) or not _GITHUB_REPOSITORY.fullmatch(
        repository
    ):
        return None
    if repository in {".", ".."}:
        return None
    return owner, repository


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise GitHubDataError("json_object_required")
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise GitHubDataError("json_list_required")
    return value


def _required_string(value: Mapping[str, object], name: str) -> str:
    raw = value.get(name)
    if not isinstance(raw, str) or not raw.strip():
        raise GitHubDataError("json_string_required")
    return raw.strip()


def _optional_string(value: Mapping[str, object], name: str) -> str | None:
    raw = value.get(name)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise GitHubDataError("json_string_invalid")
    return raw.strip() or None


def _optional_mapping(
    value: Mapping[str, object], name: str
) -> Mapping[str, object] | None:
    raw = value.get(name)
    if raw is None:
        return None
    return _mapping(raw)


def _positive_integer(value: Mapping[str, object], name: str) -> int:
    raw = value.get(name)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise GitHubDataError("json_positive_integer_required")
    return raw


def _limit(authorized: AuthorizedCall) -> int:
    raw = authorized.call.options.get(
        "limit", authorized.operation.runtime.maximum_items
    )
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise GitHubDataError("limit_invalid")
    return raw


def _per_page(authorized: AuthorizedCall) -> str:
    return urlencode({"per_page": str(_limit(authorized))})
