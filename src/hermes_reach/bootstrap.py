"""I/O-free construction of the process-local Reach runtime."""

from .sources.registry import build_alpha1_runtime

DEFAULT_RUNTIME = build_alpha1_runtime()
