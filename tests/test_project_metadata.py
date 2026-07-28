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
    assert project["tool"]["setuptools"]["package-data"] == {
        "hermes_reach": ["skill/SKILL.md"]
    }
    assert (
        "agent-reach @ git+https://github.com/Panniantong/Agent-Reach.git@"
        "1494c2ab239e7355a77e7cceaf3271453a1f34b5"
    ) in project["project"]["dependencies"]
    assert "feedparser==6.0.12" in project["project"]["dependencies"]
    assert "bilibili-cli==0.6.2" in project["project"]["dependencies"]


def test_lockfile_keeps_the_inspected_agent_reach_commit() -> None:
    root = Path(__file__).resolve().parents[1]
    lockfile = (root / "uv.lock").read_text(encoding="utf-8")
    packages = tomllib.loads(lockfile)["package"]

    assert 'name = "agent-reach"' in lockfile
    assert "1494c2ab239e7355a77e7cceaf3271453a1f34b5" in lockfile
    assert 'name = "feedparser"' in lockfile
    assert "feedparser-6.0.12" in lockfile
    assert [
        (package["name"], package.get("version"))
        for package in packages
        if package["name"] == "bilibili-cli"
    ] == [("bilibili-cli", "0.6.2")]


def test_manifest_includes_reviewed_docs_and_prunes_tests() -> None:
    root = Path(__file__).resolve().parents[1]

    assert (root / "MANIFEST.in").read_text(encoding="ascii") == (
        "recursive-include docs *.md *.json\nprune tests\n"
    )


def test_installed_entry_point_loads_the_plugin_module() -> None:
    plugins = entry_points(group="hermes_agent.plugins")
    reach = next(plugin for plugin in plugins if plugin.name == "reach")

    module = reach.load()

    assert callable(module.register)
