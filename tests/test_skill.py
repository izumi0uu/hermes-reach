from __future__ import annotations

import re
from pathlib import Path

from hermes_reach import plugin
from hermes_reach.catalog import SOURCE_CATALOG


def _skill_text() -> str:
    path = Path(plugin.__file__).resolve().parent / "skill" / "SKILL.md"
    return path.read_text(encoding="utf-8")


def test_installed_hermes_resolves_the_namespaced_plugin_skill() -> None:
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

    path = Path(plugin.__file__).resolve().parent / "skill" / "SKILL.md"
    manager = PluginManager()
    context = PluginContext(
        PluginManifest(name="reach", version="0.1.0a2", source="entry_point"),
        manager,
    )

    context.register_skill("agent-reach", path, "Safe Agent-Reach routing")

    assert manager.find_plugin_skill("reach:agent-reach") == path


def test_safe_skill_routes_every_canonical_source_and_no_unknown_source() -> None:
    text = _skill_text()
    routed_sources = set(re.findall(r"^\| `([a-z0-9_]+)` \|", text, flags=re.MULTILINE))

    assert routed_sources == {source.name for source in SOURCE_CATALOG}


def test_safe_skill_does_not_route_planned_social_operations() -> None:
    text = _skill_text()
    routed_intents = dict(
        re.findall(r"^\| `([a-z0-9_]+)` \| ([^|]+) \|$", text, flags=re.MULTILINE)
    )

    assert routed_intents["twitter"] == (
        "Search Twitter/X posts through the trusted-session-backed Connector "
        "operation with a `public` grant."
    )
    assert routed_intents["xiaohongshu"] == (
        "Search Xiaohongshu notes through the trusted-session-backed Connector "
        "operation with a `public` grant."
    )
    assert routed_intents["linkedin"] == (
        "People and jobs search is planned and unavailable; do not attempt it "
        "through Reach or another backend."
    )
    assert routed_intents["xueqiu"] == (
        "Search Xueqiu stocks through the secret-backed Connector operation with a "
        "`public` grant."
    )


def test_safe_skill_exposes_only_the_five_semantic_reach_tools() -> None:
    text = _skill_text()
    documented_tools = set(re.findall(r"`(reach_[a-z_]+)`", text))

    assert documented_tools == {
        "reach_search",
        "reach_read",
        "reach_browse",
        "reach_transcribe",
        "reach_status",
    }
    assert "one to five explicit sources" in text
    assert "Never interpret an omitted source list as all sources" in text


def test_safe_skill_contains_no_upstream_execution_escape_hatches() -> None:
    text = " ".join(_skill_text().lower().split())
    forbidden_fragments = (
        "```",
        "http://",
        "https://",
        "agent-reach doctor",
        "check-update",
        "mcporter",
        "opencli",
        "yt-dlp",
        "pipx",
        "npm install",
        "curl ",
        "`gh ",
        "`rdt ",
        "`xhs ",
        "`bili ",
        "`twitter ",
    )

    for fragment in forbidden_fragments:
        assert fragment not in text
    assert "b4d52c46c9113cb0f653d6df4cf71ebadf4930ac" in text
    assert "75cd48c6274e7f4740530d97877ec048708d5334" in text
    assert "e86ee839621360b991d985ad9d4cb18e36f86351" in text
    assert "7bc42839d3dd290e4af93b24e0b03b738cff0ffa" in text
    assert "f195253d53befdb012d7aa575e732ec627ec29ac" not in text
    assert "rss:read.feed" in text
    assert "rss:browse.entries" in text
    assert "bilibili:search.videos" in text
    assert "bilibili:read.video" in text
    assert "bilibili:browse.hot" in text
    assert "bilibili:browse.rank" in text
    assert "youtube:read.video" in text
    assert "youtube:search.videos" in text
    assert "youtube:read.subtitles" in text
    assert "33 direct owner-fork operations" in text
    assert "all 15 catalog operations" in text
    assert "18 remote operations" in text
    assert "other 30 catalog operations" in text
    assert "twitter:search.posts" in text
    assert "xiaohongshu:search.notes" in text
    assert "xueqiu:search.stocks" in text
    assert "exa:search.code" in text
    assert "linkedin:search.people" not in text
    assert "linkedin:search.jobs" not in text
