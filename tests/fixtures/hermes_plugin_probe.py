from __future__ import annotations

import asyncio
import builtins
import hashlib
import importlib.metadata
import inspect
import io
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import urllib.request
import webbrowser
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

EXPECTED_TOOLS = (
    "reach_browse",
    "reach_read",
    "reach_search",
    "reach_status",
    "reach_transcribe",
)
EXPECTED_SOURCES = (
    "github",
    "twitter",
    "youtube",
    "reddit",
    "facebook",
    "instagram",
    "bilibili",
    "xiaohongshu",
    "linkedin",
    "xiaoyuzhou",
    "v2ex",
    "xueqiu",
    "rss",
    "exa",
    "web",
)
EXPECTED_EXECUTION_CAPABILITY_FIELDS = (
    "protocol_version",
    "source",
    "operation",
    "argument_schema_id",
    "result_schema_ids",
    "backend_id",
    "backend_version",
    "required_host_capabilities",
    "maximum_items",
    "maximum_document_bytes",
    "maximum_metadata_bytes",
    "maximum_output_bytes",
    "maximum_content_type_characters",
    "maximum_content_location_characters",
    "maximum_text_characters",
    "maximum_title_characters",
    "maximum_url_characters",
    "maximum_native_id_characters",
    "maximum_author_characters",
    "maximum_published_characters",
)
EXPECTED_EXECUTION_CAPABILITIES = (
    (
        "v1",
        "rss",
        "read.feed",
        "rss.read.feed.arguments.v1",
        ("rss.feed.v1",),
        "feedparser",
        "6.0.12",
        ("fetched_document.v1",),
        1,
        1_048_576,
        16_384,
        1_048_576,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "rss",
        "browse.entries",
        "rss.browse.entries.arguments.v1",
        ("rss.entry.v1",),
        "feedparser",
        "6.0.12",
        ("fetched_document.v1",),
        21,
        1_048_576,
        16_384,
        1_048_576,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "bilibili",
        "search.videos",
        "bilibili.search.videos.arguments.v1",
        ("bilibili.video.v1",),
        "bili-cli",
        "0.6.2",
        ("network_access.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        1_024,
        512,
    ),
    (
        "v1",
        "bilibili",
        "read.video",
        "bilibili.read.video.arguments.v1",
        ("bilibili.video.v1",),
        "bili-cli",
        "0.6.2",
        ("network_access.v1",),
        1,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        1_024,
        512,
    ),
    (
        "v1",
        "bilibili",
        "browse.hot",
        "bilibili.browse.hot.arguments.v1",
        ("bilibili.video.v1",),
        "bili-cli",
        "0.6.2",
        ("network_access.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        1_024,
        512,
    ),
    (
        "v1",
        "bilibili",
        "browse.rank",
        "bilibili.browse.rank.arguments.v1",
        ("bilibili.video.v1",),
        "bili-cli",
        "0.6.2",
        ("network_access.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        1_024,
        512,
    ),
    (
        "v1",
        "youtube",
        "read.video",
        "youtube.read.video.arguments.v1",
        ("youtube.video.v1",),
        "yt-dlp",
        "2026.7.4",
        ("network_access.v1",),
        1,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        1_024,
        512,
    ),
)
FROZEN_CALLS: tuple[tuple[str, dict[str, object], str, str], ...] = (
    (
        "reach_read",
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": "https://example.com/article"},
        },
        "web",
        "read.url",
    ),
    (
        "reach_search",
        {
            "requests": [
                {
                    "source": "github",
                    "operation": "search.repositories",
                    "query": "repositories",
                }
            ]
        },
        "github",
        "search.repositories",
    ),
    (
        "reach_search",
        {
            "requests": [
                {
                    "source": "github",
                    "operation": "search.code",
                    "query": "code",
                    "options": {"limit": 3},
                }
            ]
        },
        "github",
        "search.code",
    ),
    (
        "reach_read",
        {
            "source": "github",
            "operation": "read.repository",
            "target": {"native_id": "openai/hermes-reach"},
        },
        "github",
        "read.repository",
    ),
    (
        "reach_read",
        {
            "source": "github",
            "operation": "read.issue",
            "target": {"native_id": "openai/hermes-reach#42"},
        },
        "github",
        "read.issue",
    ),
    (
        "reach_read",
        {
            "source": "github",
            "operation": "read.pull_request",
            "target": {"native_id": "openai/hermes-reach#43"},
        },
        "github",
        "read.pull_request",
    ),
    (
        "reach_browse",
        {
            "source": "github",
            "operation": "browse.actions",
            "target": {"native_id": "openai/hermes-reach"},
            "options": {"limit": 2},
        },
        "github",
        "browse.actions",
    ),
    (
        "reach_read",
        {
            "source": "github",
            "operation": "read.action_run",
            "target": {"native_id": "openai/hermes-reach#15"},
        },
        "github",
        "read.action_run",
    ),
    (
        "reach_browse",
        {
            "source": "github",
            "operation": "browse.releases",
            "target": {"native_id": "openai/hermes-reach"},
        },
        "github",
        "browse.releases",
    ),
    (
        "reach_browse",
        {
            "source": "v2ex",
            "operation": "browse.hot",
            "options": {"limit": 3},
        },
        "v2ex",
        "browse.hot",
    ),
    (
        "reach_browse",
        {
            "source": "v2ex",
            "operation": "browse.node_topics",
            "options": {"node": "python", "page": 3, "limit": 5},
        },
        "v2ex",
        "browse.node_topics",
    ),
    (
        "reach_read",
        {
            "source": "v2ex",
            "operation": "read.topic",
            "target": {"native_id": "42"},
        },
        "v2ex",
        "read.topic",
    ),
    (
        "reach_read",
        {
            "source": "v2ex",
            "operation": "read.user",
            "target": {"native_id": "alice"},
        },
        "v2ex",
        "read.user",
    ),
)


def _inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def _entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
    discovered = importlib.metadata.entry_points()
    return tuple(discovered.select(group="hermes_agent.plugins", name="reach"))


def _deny(effects: list[str], name: str) -> Any:
    def denied(*args: object, **kwargs: object) -> None:
        del args, kwargs
        effects.append(name)
        raise AssertionError(f"unexpected acceptance side effect: {name}")

    return denied


def _guard_outbound_socket_method(effects: list[str], name: str) -> Any:
    def guarded(instance: socket.socket, *args: object, **kwargs: object) -> Any:
        endpoint = args[-1] if args else kwargs.get("address", "none")
        effect = f"socket.{name}:family={int(instance.family)}:endpoint={endpoint!r}"
        return _deny(effects, effect)(instance, *args, **kwargs)

    return guarded


def _guard_ipv6_capability_probe_bind(
    effects: list[str],
    original: Any,
    capability_probe_binds: list[tuple[str, int]],
) -> Any:
    def guarded(instance: socket.socket, *args: object, **kwargs: object) -> Any:
        endpoint = args[0] if args else kwargs.get("address")
        is_exact_probe = (
            instance.family == socket.AF_INET6
            and instance.type == socket.SOCK_STREAM
            and instance.proto == 0
            and type(endpoint) is tuple
            and len(endpoint) == 2
            and type(endpoint[0]) is str
            and endpoint[0] == "::1"
            and type(endpoint[1]) is int
            and endpoint[1] == 0
            and not capability_probe_binds
        )
        if not is_exact_probe:
            effect = f"socket.bind:family={int(instance.family)}:endpoint={endpoint!r}"
            return _deny(effects, effect)(instance, *args, **kwargs)
        capability_probe_binds.append(("::1", 0))
        return original(instance, *args, **kwargs)

    return guarded


def _install_network_guards(
    stack: ExitStack, effects: list[str]
) -> list[tuple[str, int]]:
    capability_probe_binds: list[tuple[str, int]] = []
    for name in (
        "getaddrinfo",
        "gethostbyaddr",
        "gethostbyname",
        "gethostbyname_ex",
        "getnameinfo",
    ):
        stack.enter_context(
            patch.object(socket, name, _deny(effects, f"socket.{name}"))
        )
    stack.enter_context(
        patch.object(
            socket, "create_connection", _deny(effects, "socket.create_connection")
        )
    )
    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        if not hasattr(socket.socket, name):
            continue
        stack.enter_context(
            patch.object(
                socket.socket,
                name,
                _guard_outbound_socket_method(effects, name),
            )
        )
    stack.enter_context(
        patch.object(
            socket.socket,
            "bind",
            _guard_ipv6_capability_probe_bind(
                effects,
                socket.socket.bind,
                capability_probe_binds,
            ),
        )
    )
    for name in ("listen", "accept"):
        stack.enter_context(
            patch.object(
                socket.socket,
                name,
                _deny(effects, f"socket.{name}"),
            )
        )
    stack.enter_context(
        patch.object(
            asyncio, "open_connection", _deny(effects, "asyncio.open_connection")
        )
    )
    stack.enter_context(
        patch.object(
            urllib.request, "urlopen", _deny(effects, "urllib.request.urlopen")
        )
    )
    return capability_probe_binds


def _install_process_guards(
    stack: ExitStack,
    effects: list[str],
    *,
    overrides: dict[tuple[object, str], Any] | None = None,
) -> None:
    selected_overrides = overrides or {}
    for module, name in (
        (subprocess, "Popen"),
        (subprocess, "run"),
        (asyncio, "create_subprocess_exec"),
        (asyncio, "create_subprocess_shell"),
        (os, "system"),
        (os, "popen"),
    ):
        replacement = selected_overrides.get((module, name))
        if replacement is None:
            replacement = _deny(effects, f"{module.__name__}.{name}")
        stack.enter_context(patch.object(module, name, replacement))
    for name in (
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "fork",
        "forkpty",
        "posix_spawn",
        "posix_spawnp",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
    ):
        if hasattr(os, name):
            stack.enter_context(patch.object(os, name, _deny(effects, f"os.{name}")))


def _install_rss_worker_process_guards(
    stack: ExitStack, effects: list[str]
) -> list[tuple[str, ...]]:
    command = (
        sys.executable,
        "-I",
        "-m",
        "hermes_reach.sources.rss_worker",
    )
    create_kwargs = {
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.DEVNULL,
        "cwd": "/",
        "env": {},
        "close_fds": True,
        "start_new_session": True,
    }
    popen_kwargs = {
        "shell": False,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "universal_newlines": False,
        "bufsize": 0,
        **create_kwargs,
    }
    original_create_subprocess_exec = asyncio.create_subprocess_exec
    original_popen = subprocess.Popen
    launches: list[tuple[str, ...]] = []
    launch_gate = False

    async def guarded_create_subprocess_exec(
        *args: object, **kwargs: object
    ) -> asyncio.subprocess.Process:
        nonlocal launch_gate
        if tuple(args) != command or kwargs != create_kwargs or launches:
            _deny(effects, "asyncio.create_subprocess_exec.non_rss")(*args, **kwargs)
            raise AssertionError("unreachable")
        launches.append(command)
        launch_gate = True
        try:
            return await original_create_subprocess_exec(*args, **kwargs)
        finally:
            launch_gate = False

    def guarded_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        if not launch_gate or args != (command,) or kwargs != popen_kwargs:
            _deny(effects, "subprocess.Popen.non_rss")(*args, **kwargs)
            raise AssertionError("unreachable")
        return original_popen(*args, **kwargs)

    _install_process_guards(
        stack,
        effects,
        overrides={
            (asyncio, "create_subprocess_exec"): guarded_create_subprocess_exec,
            (subprocess, "Popen"): guarded_popen,
        },
    )
    return launches


def _install_registration_guards(stack: ExitStack, effects: list[str]) -> None:
    original_open = builtins.open
    original_io_open = io.open
    original_os_open = os.open
    write_flags = (
        os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC | os.O_EXCL
    )

    def guarded_open(
        file: object, mode: str = "r", *args: object, **kwargs: object
    ) -> Any:
        if any(marker in mode for marker in "wax+"):
            return _deny(effects, "builtins.open.write")(file, mode)
        return original_open(file, mode, *args, **kwargs)

    def guarded_io_open(
        file: object, mode: str = "r", *args: object, **kwargs: object
    ) -> Any:
        if any(marker in mode for marker in "wax+"):
            return _deny(effects, "io.open.write")(file, mode)
        return original_io_open(file, mode, *args, **kwargs)

    def guarded_os_open(
        path: object, flags: int, *args: object, **kwargs: object
    ) -> int:
        if flags & write_flags:
            return _deny(effects, "os.open.write")(path, flags)
        return original_os_open(path, flags, *args, **kwargs)

    stack.enter_context(patch.object(builtins, "open", guarded_open))
    stack.enter_context(patch.object(io, "open", guarded_io_open))
    stack.enter_context(patch.object(os, "open", guarded_os_open))
    for name in ("accept", "bind", "listen"):
        stack.enter_context(
            patch.object(socket.socket, name, _deny(effects, f"socket.{name}"))
        )
    for name in (
        "chdir",
        "chmod",
        "chown",
        "link",
        "makedirs",
        "mkdir",
        "remove",
        "rename",
        "replace",
        "rmdir",
        "symlink",
        "truncate",
        "umask",
        "unlink",
        "utime",
    ):
        if hasattr(os, name):
            stack.enter_context(patch.object(os, name, _deny(effects, f"os.{name}")))
    stack.enter_context(patch.object(shutil, "which", _deny(effects, "shutil.which")))
    for name in ("open", "open_new", "open_new_tab"):
        stack.enter_context(
            patch.object(webbrowser, name, _deny(effects, f"webbrowser.{name}"))
        )


def _tree_snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in (root, *sorted(root.rglob("*"))):
        relative = "." if path == root else path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[relative] = ("symlink", os.readlink(path))
        elif path.is_file():
            content = path.read_bytes()
            snapshot[relative] = (
                "file",
                len(content),
                hashlib.sha256(content).hexdigest(),
            )
        elif path.is_dir():
            snapshot[relative] = ("directory",)
        else:
            snapshot[relative] = ("other",)
    return snapshot


def _json_dispatch(registry: Any, name: str, arguments: dict[str, object]) -> Any:
    raw = registry.dispatch(name, arguments)
    assert isinstance(raw, str), f"{name} returned {type(raw).__name__}"
    return json.loads(raw)


def _execution_capabilities(api: Any) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(
            getattr(capability, field) for field in EXPECTED_EXECUTION_CAPABILITY_FIELDS
        )
        for capability in api.capabilities
    )


class FixtureHttpClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get(self, url: str) -> Any:
        from hermes_reach.sources.public_http import HttpResponse

        self.calls.append(url)
        if url != "https://example.com/feed.xml":
            raise AssertionError("unexpected fixture HTTP target")
        return HttpResponse(
            200,
            "application/rss+xml; charset=utf-8",
            (
                b"<rss><channel><title>Acceptance Feed</title>"
                b"<description>Wheel-backed RSS content</description>"
                b"<item><title>Installed item</title>"
                b"<link>https://example.com/items/1</link>"
                b"<description>Wheel-backed RSS content</description></item>"
                b"</channel></rss>"
            ),
            url,
        )


def _assert_frozen_calls(registry: Any, client: FixtureHttpClient) -> None:
    from hermes_reach.catalog import SOURCE_CATALOG
    from hermes_reach.runtime.dispatcher import RuntimeDispatcher

    requested_operations = [
        (source, operation) for _, _, source, operation in FROZEN_CALLS
    ]
    catalog_operations = {
        (source.name, operation.name)
        for source in SOURCE_CATALOG
        if source.name in {"web", "github", "v2ex"}
        for operation in source.operations
    }
    assert len(requested_operations) == 13
    assert len(set(requested_operations)) == 13
    assert set(requested_operations) == catalog_operations

    effects: list[str] = []
    with ExitStack() as stack:
        _install_process_guards(stack, effects)
        stack.enter_context(
            patch.object(
                RuntimeDispatcher,
                "dispatch",
                _deny(effects, "RuntimeDispatcher.dispatch.frozen"),
            )
        )
        for tool, arguments, source, operation in FROZEN_CALLS:
            response = _json_dispatch(registry, tool, arguments)
            assert response["outcome"] == "error"
            assert response["error"]["code"] == "all_sources_failed"
            assert len(response["groups"]) == 1
            group = response["groups"][0]
            assert group["source"] == source
            assert group["operation"] == operation
            assert group["availability"] == "unavailable"
            assert group["error"]["code"] == "capability_unavailable"
            assert group["attempts"] == []
            assert group["items"] == []
            assert group["provenance"] == {
                "catalog_version": "v1",
                "implementation_state": "planned",
                "owner": "foundation",
            }
    assert effects == [], effects
    assert client.calls == ["https://example.com/feed.xml"]


def main() -> None:
    acceptance_root = Path(sys.argv[1]).resolve()
    repository_source = Path(sys.argv[2]).resolve()
    environment_root = Path(sys.prefix).resolve()
    hermes_home = Path(os.environ["HERMES_HOME"]).resolve()

    assert Path.home().resolve() == Path(os.environ["HOME"]).resolve()
    assert _inside(hermes_home, acceptance_root)
    assert "PYTHONPATH" not in os.environ
    assert "HERMES_REACH_VPS_STATE_DIRECTORY" not in os.environ

    from hermes_cli.config import ensure_hermes_home
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    from tools.registry import registry
    from tools.skills_tool import skill_view

    # Hermes lazily imports croniter, whose platform check may invoke `file`.
    # Prime it without starting a child so RSS receives only its worker capability.
    assert sys.maxsize > 2**32
    with patch.object(platform, "architecture", return_value=("64bit", "")):
        importlib.import_module("croniter")
    assert "hermes_reach" not in sys.modules

    distribution = importlib.metadata.distribution("hermes-reach")
    distribution_root = Path(distribution.locate_file("")).resolve()
    assert _inside(distribution_root, environment_root)
    entry_points = _entry_points()
    assert len(entry_points) == 1
    assert entry_points[0].value == "hermes_reach"
    assert entry_points[0].dist is not None
    assert entry_points[0].dist.metadata["Name"] == distribution.metadata["Name"]
    assert entry_points[0].dist.version == distribution.version
    assert _inside(Path(entry_points[0].dist.locate_file("")), environment_root)

    ensure_hermes_home()
    before = _tree_snapshot(acceptance_root)
    effects: list[str] = []
    with ExitStack() as network_stack:
        capability_probe_binds = _install_network_guards(network_stack, effects)
        with ExitStack() as registration_stack:
            _install_process_guards(registration_stack, effects)
            _install_registration_guards(registration_stack, effects)

            assert "hermes_reach" not in sys.modules
            import hermes_reach

            plugin_path = Path(hermes_reach.__file__).resolve()
            assert _inside(plugin_path, environment_root)
            assert not _inside(plugin_path, repository_source)

            import agent_reach.config as agent_reach_config
            import agent_reach.doctor as agent_reach_doctor
            from agent_reach.channels import get_all_channels

            import hermes_reach.bootstrap as bootstrap
            from hermes_reach.connector.client import (
                ConnectorSnapshotStore,
                VpsProfileStore,
            )
            from hermes_reach.connector.identity import VpsKeyStore
            from hermes_reach.runtime.dispatcher import RuntimeDispatcher

            registration_stack.enter_context(
                patch.object(
                    agent_reach_config.Config,
                    "__init__",
                    _deny(effects, "agent_reach.Config"),
                )
            )
            registration_stack.enter_context(
                patch.object(
                    agent_reach_doctor,
                    "check_all",
                    _deny(effects, "agent_reach.doctor.check_all"),
                )
            )
            for channel_type in {type(channel) for channel in get_all_channels()}:
                registration_stack.enter_context(
                    patch.object(
                        channel_type,
                        "check",
                        _deny(effects, f"{channel_type.__name__}.check"),
                    )
                )
            registration_stack.enter_context(
                patch.object(
                    bootstrap,
                    "build_vps_runtime",
                    _deny(effects, "bootstrap.build_vps_runtime"),
                )
            )
            for store_type in (VpsProfileStore, ConnectorSnapshotStore, VpsKeyStore):
                registration_stack.enter_context(
                    patch.object(
                        store_type,
                        "load",
                        _deny(effects, f"{store_type.__name__}.load"),
                    )
                )
            registration_stack.enter_context(
                patch.object(
                    RuntimeDispatcher,
                    "dispatch",
                    _deny(effects, "RuntimeDispatcher.dispatch.registration"),
                )
            )

            discover_plugins()
            manager = get_plugin_manager()
            plugin_records = [
                record for record in manager.list_plugins() if record["key"] == "reach"
            ]
            assert len(plugin_records) == 1
            plugin_record = plugin_records[0]
            assert plugin_record["source"] == "entrypoint"
            assert plugin_record["enabled"] is True
            assert plugin_record["error"] is None
            assert plugin_record["tools"] == 5

            tool_names = tuple(registry.get_tool_names_for_toolset("reach"))
            assert tool_names == EXPECTED_TOOLS
            assert tuple(manager._cli_commands) == ("reach",)
            skill_path = manager.find_plugin_skill("reach:agent-reach")
            assert skill_path is not None
            assert skill_path.is_file()
            assert _inside(skill_path, environment_root)
            assert not _inside(skill_path, repository_source)

            skill = json.loads(skill_view("reach:agent-reach", preprocess=False))
            assert skill["success"] is True
            assert skill["name"] == "reach:agent-reach"
            assert "reach_status" in skill["content"]

            status = _json_dispatch(registry, "reach_status", {})
            assert status["protocol_version"] == "v1"
            assert status["outcome"] == "ok"
            sources = status["data"]["sources"]
            assert tuple(source["source"] for source in sources) == EXPECTED_SOURCES
            availability = {
                source["source"]: source["availability"] for source in sources
            }
            assert availability["rss"] == "available"
            assert availability["web"] == "unavailable"
            assert availability["github"] == "unavailable"
            assert availability["v2ex"] == "unavailable"

            from hermes_reach.agent_reach_bridge import (
                validate_agent_reach_execution_contract,
            )

            execution_api = validate_agent_reach_execution_contract(
                runtime_module="youtube"
            )
            assert execution_api.protocol_version == "v1"
            assert execution_api.list_capabilities() == execution_api.capabilities
            assert _execution_capabilities(execution_api) == (
                EXPECTED_EXECUTION_CAPABILITIES
            )
            youtube_runtime = sys.modules.get("agent_reach.execution.v1.youtube")
            assert youtube_runtime is not None
            execute_youtube = youtube_runtime.execute_youtube
            assert execute_youtube.__module__ == "agent_reach.execution.v1.youtube"
            assert execute_youtube.__qualname__ == "execute_youtube"
            assert tuple(inspect.signature(execute_youtube).parameters) == (
                "request",
                "context",
            )
            assert all(
                module_name not in sys.modules
                for module_name in ("yt_dlp", "yt_dlp_ejs", "deno")
            )

        assert effects == [], effects
        assert capability_probe_binds == []
        assert _tree_snapshot(acceptance_root) == before

        from hermes_reach.sources.registry import build_alpha1_runtime
        from hermes_reach.tools import _set_runtime

        rss_worker_launches = _install_rss_worker_process_guards(network_stack, effects)
        client = FixtureHttpClient()
        _set_runtime(build_alpha1_runtime(client))
        rss = _json_dispatch(
            registry,
            "reach_read",
            {
                "source": "rss",
                "operation": "read.feed",
                "target": {"url": "https://example.com/feed.xml"},
            },
        )
        assert rss["outcome"] == "ok"
        assert len(rss["groups"]) == 1
        rss_group = rss["groups"][0]
        assert rss_group["source"] == "rss"
        assert rss_group["operation"] == "read.feed"
        assert rss_group["availability"] == "available"
        assert rss_group["items"] == [
            {
                "kind": "content",
                "text": "Wheel-backed RSS content",
                "title": "Acceptance Feed",
            }
        ]
        assert rss_group["provenance"]["backend_id"] == "feedparser"
        assert rss_group["provenance"]["backend_version"] == "6.0.12"
        assert client.calls == ["https://example.com/feed.xml"]
        assert effects == [], effects
        assert capability_probe_binds == [("::1", 0)]
        assert rss_worker_launches == [
            (
                sys.executable,
                "-I",
                "-m",
                "hermes_reach.sources.rss_worker",
            )
        ]

        _assert_frozen_calls(registry, client)
        assert effects == [], effects
        assert capability_probe_binds == [("::1", 0)]
        assert len(rss_worker_launches) == 1

    print(
        json.dumps(
            {
                "frozen_operations": len(FROZEN_CALLS),
                "hermes_agent_version": importlib.metadata.version("hermes-agent"),
                "hermes_reach_version": distribution.version,
                "plugin_source": "entrypoint",
                "registration_side_effects": effects,
                "rss_backend": "feedparser@6.0.12",
                "tools": list(EXPECTED_TOOLS),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
