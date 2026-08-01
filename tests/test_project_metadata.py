from __future__ import annotations

import ast
import re
import tomllib
from importlib.metadata import entry_points
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PROJECT_VERSION = "0.1.0a1"
AGENT_REACH_COMMIT = "281dc3352c63cdb644f02e028cc5d645c279954a"
ROLLBACK_AGENT_REACH_COMMIT = "9b69146588b1d162515b81db26b51643c15de8eb"
LEGACY_AGENT_REACH_COMMITS = frozenset(
    {
        "2755b0c140a03ab5793540fb3245288891526586",
        "1494c2ab239e7355a77e7cceaf3271453a1f34b5",
        "0f0edca8d2d5f6179de2b38cd777d3c93232a99e",
        "3416c83ce588fadb3e8b007395b7175b26df769d",
        "b33506ac15f8aad27e4a3c5a595fb5f757347509",
    }
)
LEGACY_AGENT_REACH_TREES = frozenset({"55648469505908aa655745f5ca7704d495f12183"})
OWNER_FORK_AGENT_REACH_DEPENDENCY = (
    "agent-reach @ git+https://github.com/izumi0uu/Agent-Reach.git@"
    f"{AGENT_REACH_COMMIT}"
)
OWNER_FORK_AGENT_REACH_LOCK_SOURCE = (
    "https://github.com/izumi0uu/Agent-Reach.git"
    f"?rev={AGENT_REACH_COMMIT}#{AGENT_REACH_COMMIT}"
)
_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def _normalized_requirement_name(requirement: str) -> str:
    match = _REQUIREMENT_NAME.match(requirement)
    assert match is not None
    return match.group().replace("_", "-").lower()


def _import_targets(source_path: Path) -> set[str]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=source_path.name)
    targets: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            targets.add(node.module)
            targets.update(
                f"{node.module}.{alias.name}"
                for alias in node.names
                if alias.name != "*"
            )
        elif (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "__import__")
                or (isinstance(node.func, ast.Name) and node.func.id == "import_module")
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                )
            )
        ):
            targets.add(node.args[0].value)
    return targets


def _is_fork_runtime_import(module: str) -> bool:
    return module == "agent_reach.execution" or module.startswith(
        "agent_reach.execution."
    )


def test_project_declares_the_documented_hermes_plugin_entry_point() -> None:
    with (ROOT / "pyproject.toml").open("rb") as project_file:
        project = tomllib.load(project_file)
    plugin_manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))

    dependencies = project["project"]["dependencies"]
    agent_reach_dependencies = [
        dependency
        for dependency in dependencies
        if _normalized_requirement_name(dependency) == "agent-reach"
    ]

    assert project["project"]["version"] == PROJECT_VERSION
    assert plugin_manifest["version"] == PROJECT_VERSION
    assert project["project"]["requires-python"] == ">=3.11,<3.14"
    assert project["project"]["entry-points"]["hermes_agent.plugins"] == {
        "reach": "hermes_reach"
    }
    assert "hermes-agent>=0.19.0,<0.20.0" in dependencies
    assert project["tool"]["setuptools"]["package-data"] == {
        "hermes_reach": ["skill/SKILL.md"]
    }
    assert agent_reach_dependencies == [OWNER_FORK_AGENT_REACH_DEPENDENCY]
    assert not any(
        "panniantong" in dependency.lower() for dependency in agent_reach_dependencies
    )
    assert "feedparser==6.0.12" in dependencies
    assert "bilibili-cli==0.6.2" in dependencies
    assert "yt-dlp==2026.7.4" in dependencies
    assert "yt-dlp-ejs==0.8.0" in dependencies
    assert "deno==2.8.3" in dependencies


def test_lockfile_keeps_the_inspected_agent_reach_commit() -> None:
    lockfile = (ROOT / "uv.lock").read_text(encoding="utf-8")
    packages = tomllib.loads(lockfile)["package"]
    agent_reach_packages = [
        package for package in packages if package["name"] == "agent-reach"
    ]

    assert len(agent_reach_packages) == 1
    assert agent_reach_packages[0]["version"] == "1.5.0"
    assert agent_reach_packages[0]["source"] == {
        "git": OWNER_FORK_AGENT_REACH_LOCK_SOURCE
    }
    assert "panniantong" not in str(agent_reach_packages[0]["source"]).lower()
    assert 'name = "feedparser"' in lockfile
    assert "feedparser-6.0.12" in lockfile
    assert [
        (package["name"], package.get("version"))
        for package in packages
        if package["name"] == "bilibili-cli"
    ] == [("bilibili-cli", "0.6.2")]
    assert [
        (package["name"], package.get("version"))
        for package in packages
        if package["name"] in {"yt-dlp", "yt-dlp-ejs", "deno"}
    ] == [
        ("deno", "2.8.3"),
        ("yt-dlp", "2026.7.4"),
        ("yt-dlp-ejs", "0.8.0"),
    ]
    assert (
        "f11f2b11d5a8ac4059f9bdf29fa4407dc7c6bb00c5097e95ca22a7a9db518266" in lockfile
    )
    assert (
        "79300e5fca7f937a1eeede11f0456862c1b41107ce1d726871e0207424f4bdb4" in lockfile
    )


def test_release_surface_contains_no_legacy_agent_reach_pin() -> None:
    release_paths = [
        ROOT / ".github" / "workflows" / "quality.yml",
        ROOT / "README.md",
        ROOT / "README_EN.md",
        ROOT / "plugin.yaml",
        ROOT / "pyproject.toml",
        ROOT / "uv.lock",
    ]
    release_paths.extend(sorted((ROOT / "docs").rglob("*.json")))
    release_paths.extend(sorted((ROOT / "docs").rglob("*.md")))
    release_paths.extend(sorted((ROOT / "src" / "hermes_reach").rglob("*.py")))
    release_paths.extend(sorted((ROOT / "src" / "hermes_reach").rglob("*.md")))

    for path in release_paths:
        text = path.read_text(encoding="utf-8")
        for legacy_commit in LEGACY_AGENT_REACH_COMMITS:
            assert legacy_commit not in text, path.relative_to(ROOT).as_posix()
        for legacy_tree in LEGACY_AGENT_REACH_TREES:
            assert legacy_tree not in text, path.relative_to(ROOT).as_posix()


def test_active_selectors_do_not_replace_historical_rollback_evidence() -> None:
    active_selectors = [
        ROOT / "pyproject.toml",
        ROOT / "uv.lock",
    ]
    production_package = ROOT / "src" / "hermes_reach"
    active_selectors.extend(sorted(production_package.rglob("*.py")))
    active_selectors.extend(sorted(production_package.rglob("*.md")))

    for path in active_selectors:
        assert ROLLBACK_AGENT_REACH_COMMIT not in path.read_text(encoding="utf-8"), (
            path.relative_to(ROOT).as_posix()
        )

    release_guide = (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
    assert ROLLBACK_AGENT_REACH_COMMIT in release_guide


def test_manifest_includes_reviewed_docs_and_prunes_tests() -> None:
    assert (ROOT / "MANIFEST.in").read_text(encoding="ascii") == (
        "recursive-include docs *.md *.json\nprune tests\n"
    )


def test_production_source_does_not_vendor_agent_reach() -> None:
    source_root = ROOT / "src"
    vendored_directories = [
        path.relative_to(ROOT).as_posix()
        for path in source_root.rglob("*")
        if path.is_dir() and path.name.replace("-", "_").lower() == "agent_reach"
    ]

    assert vendored_directories == []


def test_production_source_has_no_direct_fork_runtime_imports() -> None:
    production_package = ROOT / "src" / "hermes_reach"
    forbidden_imports = [
        (source_path.relative_to(ROOT).as_posix(), target)
        for source_path in sorted(production_package.rglob("*.py"))
        for target in sorted(_import_targets(source_path))
        if _is_fork_runtime_import(target)
    ]

    assert _is_fork_runtime_import("agent_reach.execution") is True
    assert _is_fork_runtime_import("agent_reach.execution.runtime") is True
    assert _is_fork_runtime_import("hermes_reach.runtime") is False
    assert (production_package / "runtime").is_dir()
    assert forbidden_imports == []


def test_installed_entry_point_loads_the_plugin_module() -> None:
    plugins = entry_points(group="hermes_agent.plugins")
    reach = next(plugin for plugin in plugins if plugin.name == "reach")

    module = reach.load()

    assert callable(module.register)
