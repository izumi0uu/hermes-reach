from __future__ import annotations

from typing import Any

from hermes_reach.plugin import register


class FakePluginContext:
    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []

    def register_tool(self, **kwargs: Any) -> None:
        self.tools.append(kwargs)

    def register_cli_command(self, **kwargs: Any) -> None:
        self.commands.append(kwargs)


def test_registers_exactly_the_five_public_tools_and_one_cli_namespace() -> None:
    context = FakePluginContext()

    register(context)

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
