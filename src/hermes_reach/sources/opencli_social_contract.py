"""Frozen Connector contract for the exact OpenCLI social operation batch."""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from ..connector.protocol import GrantScope, PublicBackendIdentity

OPENCLI_SOCIAL_BACKEND: Final = PublicBackendIdentity("opencli", "1.8.6-hermes.1")
OPENCLI_SOCIAL_SCOPES: Final = (
    GrantScope("reddit", "search.posts", "public"),
    GrantScope("reddit", "read.post", "public"),
    GrantScope("reddit", "browse.subreddit", "public"),
    GrantScope("reddit", "browse.hot", "public"),
    GrantScope("reddit", "browse.popular", "public"),
    GrantScope("reddit", "browse.all", "public"),
    GrantScope("reddit", "read.subreddit", "public"),
    GrantScope("facebook", "search", "public"),
    GrantScope("facebook", "read.profile", "public"),
    GrantScope("facebook", "browse.feed", "account_visible"),
    GrantScope("facebook", "browse.groups", "account_visible"),
    GrantScope("instagram", "search.users", "public"),
    GrantScope("instagram", "read.profile", "public"),
    GrantScope("instagram", "browse.user_posts", "public"),
    GrantScope("instagram", "browse.explore", "account_visible"),
    GrantScope("twitter", "search.posts", "public"),
    GrantScope("xiaohongshu", "search.notes", "public"),
)
OPENCLI_SOCIAL_OPERATIONS: Final = tuple(
    (scope.source, scope.operation) for scope in OPENCLI_SOCIAL_SCOPES
)
OPENCLI_SOCIAL_SOURCES: Final = frozenset(
    scope.source for scope in OPENCLI_SOCIAL_SCOPES
)
OPENCLI_SOCIAL_SCOPE_BY_OPERATION: Final = MappingProxyType(
    {(scope.source, scope.operation): scope for scope in OPENCLI_SOCIAL_SCOPES}
)

__all__ = [
    "OPENCLI_SOCIAL_BACKEND",
    "OPENCLI_SOCIAL_OPERATIONS",
    "OPENCLI_SOCIAL_SCOPES",
    "OPENCLI_SOCIAL_SCOPE_BY_OPERATION",
    "OPENCLI_SOCIAL_SOURCES",
]
