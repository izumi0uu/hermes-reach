"""Shared policy, execution, token, and release primitives for Reach adapters."""

from .adapters import AdapterBinding, AdapterRegistry, AdapterResult, RawItem
from .dispatcher import RuntimeDispatcher
from .policy import AuthorizedCall, ReadOnlyPolicy, RuntimePolicyError
from .release import ReleaseReport, check_release_pins
from .runner import AttemptProvenance, BoundedRunner, RunnerResult
from .tokens import TokenCodec, TokenError, TokenPayload

__all__ = [
    "AdapterBinding",
    "AdapterRegistry",
    "AdapterResult",
    "AttemptProvenance",
    "AuthorizedCall",
    "BoundedRunner",
    "RawItem",
    "ReadOnlyPolicy",
    "ReleaseReport",
    "RunnerResult",
    "RuntimeDispatcher",
    "RuntimePolicyError",
    "TokenCodec",
    "TokenError",
    "TokenPayload",
    "check_release_pins",
]
