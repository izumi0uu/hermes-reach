from __future__ import annotations

import asyncio
import builtins
import hashlib
import importlib
import importlib.metadata
import importlib.util
import io
import json
import os
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

PLUGIN_MODULE = "hermes_reach"
PLUGIN_NAME = "reach"
PLUGIN_SKILL = "reach:agent-reach"


def _inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def _entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
    discovered = importlib.metadata.entry_points()
    return tuple(discovered.select(group="hermes_agent.plugins", name=PLUGIN_NAME))


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


def _deny(effects: list[str], name: str) -> Any:
    def denied(*args: object, **kwargs: object) -> None:
        del args, kwargs
        effects.append(name)
        raise AssertionError(f"unexpected lifecycle probe side effect: {name}")

    return denied


def _install_discovery_guards(stack: ExitStack, effects: list[str]) -> None:
    original_import = builtins.__import__
    original_import_module = importlib.import_module
    original_open = builtins.open
    original_io_open = io.open
    original_os_open = os.open
    write_flags = (
        os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC | os.O_EXCL
    )

    def guarded_import(name: str, *args: object, **kwargs: object) -> Any:
        if name == PLUGIN_MODULE or name.startswith(f"{PLUGIN_MODULE}."):
            return _deny(effects, f"import:{name}")(name, *args, **kwargs)
        return original_import(name, *args, **kwargs)

    def guarded_import_module(name: str, package: str | None = None) -> Any:
        if name == PLUGIN_MODULE or name.startswith(f"{PLUGIN_MODULE}."):
            return _deny(effects, f"importlib.import_module:{name}")(name, package)
        return original_import_module(name, package)

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

    stack.enter_context(patch.object(builtins, "__import__", guarded_import))
    stack.enter_context(patch.object(importlib, "import_module", guarded_import_module))
    stack.enter_context(patch.object(builtins, "open", guarded_open))
    stack.enter_context(patch.object(io, "open", guarded_io_open))
    stack.enter_context(patch.object(os, "open", guarded_os_open))

    for module, name in (
        (subprocess, "Popen"),
        (subprocess, "run"),
        (asyncio, "create_subprocess_exec"),
        (asyncio, "create_subprocess_shell"),
        (asyncio, "open_connection"),
        (urllib.request, "urlopen"),
    ):
        stack.enter_context(
            patch.object(module, name, _deny(effects, f"{module.__name__}.{name}"))
        )

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
    for name in (
        "accept",
        "bind",
        "connect",
        "connect_ex",
        "listen",
        "send",
        "sendall",
        "sendmsg",
        "sendto",
    ):
        if hasattr(socket.socket, name):
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


def _assert_isolated_environment(
    acceptance_root: Path, repository_source: Path
) -> None:
    hermes_home = Path(os.environ["HERMES_HOME"]).resolve()
    bundled_plugins = Path(os.environ["HERMES_BUNDLED_PLUGINS"]).resolve()

    assert Path.cwd().resolve() == acceptance_root
    assert Path.home().resolve() == Path(os.environ["HOME"]).resolve()
    assert _inside(hermes_home, acceptance_root)
    assert _inside(bundled_plugins, acceptance_root)
    assert hermes_home.is_dir()
    assert bundled_plugins.is_dir()
    assert "PYTHONPATH" not in os.environ
    assert "HERMES_ENABLE_PROJECT_PLUGINS" not in os.environ
    assert "HERMES_REACH_VPS_STATE_DIRECTORY" not in os.environ

    for entry in sys.path:
        if entry:
            assert not _inside(Path(entry), repository_source)


def _installed_distribution(
    environment_root: Path, repository_source: Path
) -> importlib.metadata.Distribution:
    distribution = importlib.metadata.distribution("hermes-reach")
    distribution_root = Path(distribution.locate_file("")).resolve()
    assert _inside(distribution_root, environment_root)
    assert not _inside(distribution_root, repository_source)

    module_spec = importlib.util.find_spec(PLUGIN_MODULE)
    assert module_spec is not None
    assert module_spec.origin is not None
    module_path = Path(module_spec.origin).resolve()
    assert _inside(module_path, environment_root)
    assert not _inside(module_path, repository_source)

    entry_points = _entry_points()
    assert len(entry_points) == 1
    entry_point = entry_points[0]
    assert entry_point.value == PLUGIN_MODULE
    assert entry_point.dist is not None
    assert entry_point.dist.metadata["Name"] == distribution.metadata["Name"]
    assert entry_point.dist.version == distribution.version
    assert _inside(Path(entry_point.dist.locate_file("")), environment_root)
    return distribution


def _assert_removed_distribution() -> None:
    try:
        importlib.metadata.distribution("hermes-reach")
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        raise AssertionError("hermes-reach distribution remains installed")

    assert _entry_points() == ()
    assert importlib.util.find_spec(PLUGIN_MODULE) is None
    try:
        importlib.import_module(PLUGIN_MODULE)
    except ModuleNotFoundError as exc:
        assert exc.name == PLUGIN_MODULE
    else:
        raise AssertionError("hermes_reach remains importable after uninstall")


def _reach_tools(registry: Any) -> list[str]:
    return sorted(
        name
        for name, toolset in registry.get_tool_to_toolset_map().items()
        if name.startswith("reach_") or toolset == PLUGIN_NAME
    )


def _reach_cli_commands(manager: Any) -> list[str]:
    return sorted(
        name
        for name, command in manager._cli_commands.items()
        if name == PLUGIN_NAME or command.get("plugin") == PLUGIN_NAME
    )


def main() -> None:
    assert len(sys.argv) == 4, (
        "usage: hermes_plugin_lifecycle_probe.py "
        "<disabled|removed> <acceptance-root> <repository-src>"
    )
    mode = sys.argv[1]
    assert mode in {"disabled", "removed"}, mode
    acceptance_root = Path(sys.argv[2]).resolve()
    repository_source = Path(sys.argv[3]).resolve()
    environment_root = Path(sys.prefix).resolve()

    _assert_isolated_environment(acceptance_root, repository_source)
    assert not any(
        name == PLUGIN_MODULE or name.startswith(f"{PLUGIN_MODULE}.")
        for name in sys.modules
    )

    if mode == "disabled":
        _installed_distribution(environment_root, repository_source)
        distribution_state = "installed"
        entry_point_count = 1
        module_state = "not_imported"
    else:
        _assert_removed_distribution()
        distribution_state = "absent"
        entry_point_count = 0
        module_state = "absent"

    from hermes_cli.config import ensure_hermes_home
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    from tools.registry import registry

    ensure_hermes_home()
    before_tree = _tree_snapshot(acceptance_root)
    before_tools = registry.get_all_tool_names()
    manager = get_plugin_manager()
    assert manager is get_plugin_manager()
    assert manager._discovered is False

    effects: list[str] = []
    with ExitStack() as stack:
        _install_discovery_guards(stack, effects)
        discover_plugins()

    assert effects == [], effects
    assert _tree_snapshot(acceptance_root) == before_tree
    assert manager is get_plugin_manager()
    assert manager._discovered is True
    assert registry.get_all_tool_names() == before_tools
    assert not any(
        name == PLUGIN_MODULE or name.startswith(f"{PLUGIN_MODULE}.")
        for name in sys.modules
    )

    tools = _reach_tools(registry)
    cli_commands = _reach_cli_commands(manager)
    assert tools == []
    assert registry.get_tool_names_for_toolset(PLUGIN_NAME) == []
    assert cli_commands == []
    assert manager.find_plugin_skill(PLUGIN_SKILL) is None
    assert not any(
        name.startswith(f"{PLUGIN_NAME}:") for name in manager._plugin_skills
    )

    plugin_records = [
        record for record in manager.list_plugins() if record["key"] == PLUGIN_NAME
    ]
    if mode == "disabled":
        assert len(plugin_records) == 1
        plugin_record = plugin_records[0]
        assert plugin_record["name"] == PLUGIN_NAME
        assert plugin_record["source"] == "entrypoint"
        assert plugin_record["enabled"] is False
        assert plugin_record["tools"] == 0
        assert plugin_record["hooks"] == 0
        assert plugin_record["middleware"] == 0
        assert plugin_record["commands"] == 0
        assert plugin_record["error"] in {
            "disabled via config",
            "not enabled in config (run `hermes plugins enable reach` to activate)",
        }
        plugin_record_state = "disabled"
    else:
        assert plugin_records == []
        plugin_record_state = "absent"

    print(
        json.dumps(
            {
                "cli_commands": cli_commands,
                "distribution": distribution_state,
                "entry_points": entry_point_count,
                "module": module_state,
                "plugin_record": plugin_record_state,
                "skill": False,
                "state": mode,
                "tools": tools,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
