from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

import hermes_reach.sources.bilibili as bilibili
import hermes_reach.sources.exa as exa
import hermes_reach.sources.v2ex as v2ex
import hermes_reach.sources.youtube as youtube
from hermes_reach.sources._worker_cleanup import cleanup_worker_resources
from hermes_reach.sources.bilibili import BilibiliWorker, BilibiliWorkerError
from hermes_reach.sources.exa import ExaWorker, ExaWorkerError
from hermes_reach.sources.exa_artifacts import ExaArtifactAttestation
from hermes_reach.sources.v2ex import V2exWorker, V2exWorkerError
from hermes_reach.sources.youtube import YouTubeWorker, YouTubeWorkerError

WorkerCall = Callable[[], Awaitable[object]]
WorkerFailure = type[
    BilibiliWorkerError | ExaWorkerError | V2exWorkerError | YouTubeWorkerError
]


def _exa_artifacts() -> ExaArtifactAttestation:
    return ExaArtifactAttestation(
        Path("/opt/hermes-reach/exa/bin/node"),
        "a" * 64,
        Path("/opt/hermes-reach/exa/mcporter"),
        Path("/opt/hermes-reach/exa/mcporter/dist/cli.js"),
        "b" * 64,
        Path("/opt/hermes-reach/exa/config.json"),
        "c" * 64,
    )


WORKERS: tuple[tuple[ModuleType, WorkerFailure, WorkerCall], ...] = (
    (
        bilibili,
        BilibiliWorkerError,
        lambda: BilibiliWorker().execute("browse.hot", limit=1),
    ),
    (
        youtube,
        YouTubeWorkerError,
        lambda: YouTubeWorker().execute(
            "read.video", url="https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        ),
    ),
    (v2ex, V2exWorkerError, lambda: V2exWorker().execute("browse.hot", limit=1)),
    (exa, ExaWorkerError, lambda: ExaWorker(_exa_artifacts()).execute("query", 1)),
)


@pytest.mark.parametrize(("module", "failure_type", "worker_call"), WORKERS)
def test_workers_reject_relative_python_before_process_or_state_creation(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    failure_type: WorkerFailure,
    worker_call: WorkerCall,
) -> None:
    spawned = False
    temporary_created = False

    async def create(*_: str, **__: object) -> None:
        nonlocal spawned
        spawned = True

    class ForbiddenTemporaryDirectory:
        def __init__(self, *_: object, **__: object) -> None:
            nonlocal temporary_created
            temporary_created = True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(module.sys, "executable", "python")
    monkeypatch.setattr(
        module.tempfile,
        "TemporaryDirectory",
        ForbiddenTemporaryDirectory,
    )

    with pytest.raises(failure_type) as raised:
        asyncio.run(worker_call())

    assert raised.value.failure_class == "permanent"
    assert spawned is False
    assert temporary_created is False


@pytest.mark.parametrize(("module", "_failure_type", "worker_call"), WORKERS)
def test_workers_preserve_cancellation_when_private_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    _failure_type: WorkerFailure,
    worker_call: WorkerCall,
) -> None:
    real_temporary_directory = tempfile.TemporaryDirectory
    cleaned = False

    class FailingTemporaryDirectory:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._temporary = real_temporary_directory(*args, **kwargs)
            self.name = self._temporary.name

        def cleanup(self) -> None:
            nonlocal cleaned
            self._temporary.cleanup()
            cleaned = True
            raise OSError("private cleanup detail")

    async def cancel_spawn(*_: str, **__: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", cancel_spawn)
    monkeypatch.setattr(
        module.tempfile,
        "TemporaryDirectory",
        FailingTemporaryDirectory,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker_call())

    assert cleaned is True


def test_shared_cleanup_preserves_business_error_after_both_cleanups_fail() -> None:
    events: list[str] = []

    async def failing_process_cleanup() -> None:
        events.append("process")
        raise OSError("process cleanup detail")

    class FailingTemporaryDirectory:
        def cleanup(self) -> None:
            events.append("temporary")
            raise OSError("temporary cleanup detail")

    async def run() -> None:
        try:
            raise ValueError("business failure")
        finally:
            await cleanup_worker_resources(
                failing_process_cleanup(),
                cast(
                    tempfile.TemporaryDirectory[str],
                    FailingTemporaryDirectory(),
                ),
            )

    with pytest.raises(ValueError, match="business failure"):
        asyncio.run(run())

    assert events == ["process", "temporary"]
