from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hermes_reach import plugin


class FakePluginContext:
    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []
        self.skills: list[dict[str, Any]] = []

    def register_tool(self, **kwargs: Any) -> None:
        self.tools.append(kwargs)

    def register_cli_command(self, **kwargs: Any) -> None:
        self.commands.append(kwargs)

    def register_skill(self, **kwargs: Any) -> None:
        self.skills.append(kwargs)


def test_registers_fixed_tools_cli_and_safe_agent_reach_skill() -> None:
    context = FakePluginContext()

    plugin.register(context)

    assert [tool["name"] for tool in context.tools] == [
        "reach_search",
        "reach_read",
        "reach_browse",
        "reach_transcribe",
        "reach_status",
    ]
    assert {tool["toolset"] for tool in context.tools} == {"reach"}
    assert all(not tool.get("override", False) for tool in context.tools)
    assert [tool["is_async"] for tool in context.tools] == [
        True,
        True,
        True,
        True,
        False,
    ]
    assert [command["name"] for command in context.commands] == ["reach"]
    assert len(context.skills) == 1
    skill = context.skills[0]
    assert skill["name"] == "agent-reach"
    assert skill["description"] == (
        "Route bounded, read-only internet retrieval through Hermes Reach."
    )
    skill_path = skill["path"]
    assert isinstance(skill_path, Path)
    assert skill_path.is_file()
    assert skill_path.parent.name == "skill"


def test_registration_validates_the_agent_reach_catalog_without_probing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = FakePluginContext()
    loaded = False

    def load_catalog() -> object:
        nonlocal loaded
        loaded = True
        return object()

    monkeypatch.setattr(plugin, "load_agent_reach_catalog", load_catalog)

    plugin.register(context)

    assert loaded is True


def test_agent_reach_catalog_failure_prevents_all_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = FakePluginContext()

    def fail_catalog() -> object:
        raise RuntimeError("catalog drift fixture")

    monkeypatch.setattr(plugin, "load_agent_reach_catalog", fail_catalog)

    with pytest.raises(RuntimeError, match="catalog drift fixture"):
        plugin.register(context)

    assert context.tools == []
    assert context.commands == []
    assert context.skills == []


def test_registration_fails_before_side_effects_without_plugin_skill_api() -> None:
    class LegacyPluginContext:
        def __init__(self) -> None:
            self.tools: list[dict[str, Any]] = []
            self.commands: list[dict[str, Any]] = []

        def register_tool(self, **kwargs: Any) -> None:
            self.tools.append(kwargs)

        def register_cli_command(self, **kwargs: Any) -> None:
            self.commands.append(kwargs)

    context = LegacyPluginContext()

    with pytest.raises(RuntimeError, match="plugin skill API"):
        plugin.register(context)

    assert context.tools == []
    assert context.commands == []


def test_registration_fails_before_side_effects_when_skill_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = FakePluginContext()
    monkeypatch.setattr(
        plugin, "_AGENT_REACH_SKILL_PATH", tmp_path / "missing" / "SKILL.md"
    )

    with pytest.raises(RuntimeError, match="skill resource is missing"):
        plugin.register(context)

    assert context.tools == []
    assert context.commands == []
    assert context.skills == []
