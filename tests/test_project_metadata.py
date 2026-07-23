from __future__ import annotations

import tomllib
from importlib.metadata import entry_points
from pathlib import Path


def test_project_declares_the_documented_hermes_plugin_entry_point() -> None:
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as project_file:
        project = tomllib.load(project_file)

    assert project["project"]["requires-python"] == ">=3.11,<3.14"
    assert project["project"]["entry-points"]["hermes_agent.plugins"] == {
        "reach": "hermes_reach"
    }
    assert "hermes-agent>=0.19.0,<0.20.0" in project["project"]["dependencies"]


def test_installed_entry_point_loads_the_plugin_module() -> None:
    plugins = entry_points(group="hermes_agent.plugins")
    reach = next(plugin for plugin in plugins if plugin.name == "reach")

    module = reach.load()

    assert callable(module.register)
