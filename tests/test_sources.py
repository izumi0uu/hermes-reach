from __future__ import annotations

import asyncio
import builtins
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

import hermes_reach.sources.registry as source_registry
from hermes_reach.contracts import validate_browse, validate_read, validate_search
from hermes_reach.runtime.adapters import AdapterRegistry
from hermes_reach.runtime.policy import ReadOnlyPolicy
from hermes_reach.sources.exa_artifacts import ExaArtifactAttestation
from hermes_reach.sources.public_http import HttpResponse
from hermes_reach.sources.registry import build_alpha1_registry, build_alpha1_runtime
from hermes_reach.sources.rss import FeedparserWorker, RssAdapter
from hermes_reach.sources.rss_worker import FeedparserProjection, FeedProjection

FEED_URL = "https://example.com/feed.xml"


class FixtureHttpClient:
    def __init__(self, *responses: HttpResponse | Exception) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    async def get(self, url: str) -> HttpResponse:
        self.calls.append(url)
        value = self._responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FalsyFixtureHttpClient(FixtureHttpClient):
    def __bool__(self) -> bool:
        return False


def _response(body: str, content_type: str, url: str) -> HttpResponse:
    return HttpResponse(200, content_type, body.encode(), url)


def test_rss_adapter_normalizes_atom_entries_and_strips_native_id_secrets() -> None:
    atom = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Example feed</title><subtitle>A useful feed</subtitle>
      <link rel="alternate" href="https://example.com/feed" />
      <entry><id>https://example.com/entry-2?token=private#section</id><title>Second</title>
        <link href="/second?tracking=yes" />
        <author><name>Alice</name></author><updated>2026-07-24T02:00:00Z</updated>
        <summary>&lt;p&gt;Second body&lt;/p&gt;</summary></entry>
      <entry><id>https://example.com/entry-1?signature=private#section</id><title>First</title>
        <link href="/first" /><summary>First body</summary></entry>
    </feed>"""
    client = FixtureHttpClient(
        _response(atom, "application/atom+xml", "https://example.com/feed.xml")
    )
    call = validate_browse(
        {
            "source": "rss",
            "operation": "browse.entries",
            "target": {"url": "https://example.com/feed.xml"},
            "options": {"limit": 2},
        }
    )

    result = asyncio.run(RssAdapter(client).execute(ReadOnlyPolicy().authorize(call)))

    assert result.is_success
    assert [item.native_id for item in result.items] == [
        "https://example.com/entry-2",
        "https://example.com/entry-1",
    ]
    assert result.items[0].title == "Second"
    assert result.items[0].url == "https://example.com/second"
    assert result.items[0].author == "Alice"


def test_rss_adapter_reads_rss_1_entries_beside_channel() -> None:
    rss = """<?xml version="1.0" encoding="utf-8"?>
    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
      xmlns="http://purl.org/rss/1.0/">
      <channel><title>RSS 1 feed</title><link>https://example.com/</link></channel>
      <item><title>First item</title><link>https://example.com/first</link>
        <description>First body</description></item>
    </rdf:RDF>"""
    client = FixtureHttpClient(
        _response(rss, "application/rss+xml", "https://example.com/feed.rdf")
    )
    call = validate_browse(
        {
            "source": "rss",
            "operation": "browse.entries",
            "target": {"url": "https://example.com/feed.rdf"},
        }
    )

    result = asyncio.run(RssAdapter(client).execute(ReadOnlyPolicy().authorize(call)))

    assert result.is_success
    assert [(item.title, item.text) for item in result.items] == [
        ("First item", "First body")
    ]


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("", ""),
        (
            "  application/rss+xml; charset=utf-8  ",
            "application/rss+xml; charset=utf-8",
        ),
    ],
)
def test_rss_adapter_normalizes_content_type_only_at_the_fork_boundary(
    monkeypatch: pytest.MonkeyPatch,
    content_type: str,
    expected: str,
) -> None:
    feed = (
        "<rss><channel><title>Feed</title><description>Body</description>"
        "</channel></rss>"
    )
    client = FixtureHttpClient(_response(feed, content_type, FEED_URL))
    captured: dict[str, object] = {}

    async def parse(
        _worker: FeedparserWorker,
        body: bytes,
        *,
        operation: str,
        content_type: str,
        content_location: str,
        max_entries: int,
    ) -> FeedparserProjection:
        captured.update(
            {
                "body": body,
                "operation": operation,
                "content_type": content_type,
                "content_location": content_location,
                "max_entries": max_entries,
            }
        )
        return FeedparserProjection(
            "read.feed",
            FeedProjection("Body", "Feed", None),
            (),
            None,
            False,
        )

    monkeypatch.setattr(FeedparserWorker, "parse", parse)
    call = validate_read(
        {
            "source": "rss",
            "operation": "read.feed",
            "target": {"url": FEED_URL},
        }
    )

    result = asyncio.run(RssAdapter(client).execute(ReadOnlyPolicy().authorize(call)))

    assert result.is_success
    assert captured == {
        "body": feed.encode(),
        "operation": "read.feed",
        "content_type": expected,
        "content_location": FEED_URL,
        "max_entries": 1,
    }


def test_rss_adapter_rejects_control_characters_in_content_type_before_fork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed = (
        "<rss><channel><title>Feed</title><description>Body</description>"
        "</channel></rss>"
    )
    client = FixtureHttpClient(
        _response(feed, "application/rss+xml\r\nproxy: private", FEED_URL)
    )
    called = False

    async def parse(*_: object, **__: object) -> FeedparserProjection:
        nonlocal called
        called = True
        raise AssertionError("unsafe content type crossed the worker boundary")

    monkeypatch.setattr(FeedparserWorker, "parse", parse)
    call = validate_read(
        {
            "source": "rss",
            "operation": "read.feed",
            "target": {"url": FEED_URL},
        }
    )

    result = asyncio.run(RssAdapter(client).execute(ReadOnlyPolicy().authorize(call)))

    assert result.failure_class == "permanent"
    assert called is False


def test_rss_adapter_rejects_dtd_and_entity_declarations() -> None:
    xml = """<?xml version="1.0"?>
    <!DOCTYPE rss [<!ENTITY private "private-body">]>
    <rss><channel><title>&private;</title></channel></rss>"""
    client = FixtureHttpClient(
        _response(xml, "application/rss+xml", "https://example.com/feed.xml")
    )
    call = validate_read(
        {
            "source": "rss",
            "operation": "read.feed",
            "target": {"url": "https://example.com/feed.xml"},
        }
    )

    result = asyncio.run(RssAdapter(client).execute(ReadOnlyPolicy().authorize(call)))

    assert result.failure_class == "permanent"
    assert result.items == ()


def test_rss_adapter_decodes_big_endian_xml_before_declaration_checks() -> None:
    for encoding in ("utf-16be", "utf-32be"):
        xml = (
            f'<?xml version="1.0" encoding="{encoding}"?>'
            "<rss><channel><title>Encoded feed</title>"
            "<description>Visible body</description></channel></rss>"
        )
        client = FixtureHttpClient(
            HttpResponse(
                200,
                "application/rss+xml",
                xml.encode(encoding),
                "https://example.com/feed.xml",
            )
        )
        call = validate_read(
            {
                "source": "rss",
                "operation": "read.feed",
                "target": {"url": "https://example.com/feed.xml"},
            }
        )

        result = asyncio.run(
            RssAdapter(client).execute(ReadOnlyPolicy().authorize(call))
        )

        assert result.is_success
        assert result.items[0].title == "Encoded feed"


def test_rss_adapter_rejects_big_endian_unsafe_declarations() -> None:
    xml = (
        '<?xml version="1.0" encoding="utf-16be"?>'
        '<!DOCTYPE rss [<!ENTITY private "private-body">]>'
        "<rss><channel><title>&private;</title></channel></rss>"
    )
    client = FixtureHttpClient(
        HttpResponse(
            200,
            "application/rss+xml",
            xml.encode("utf-16be"),
            "https://example.com/feed.xml",
        )
    )
    call = validate_read(
        {
            "source": "rss",
            "operation": "read.feed",
            "target": {"url": "https://example.com/feed.xml"},
        }
    )

    result = asyncio.run(RssAdapter(client).execute(ReadOnlyPolicy().authorize(call)))

    assert result.failure_class == "permanent"
    assert result.items == ()


def test_rss_adapter_rejects_encoding_that_conflicts_with_bom() -> None:
    xml = (
        '<?xml version="1.0" encoding="utf-16le"?>'
        "<rss><channel><title>Wrong byte order</title></channel></rss>"
    )
    body = b"\xfe\xff" + xml.encode("utf-16be")
    client = FixtureHttpClient(
        HttpResponse(
            200,
            "application/rss+xml",
            body,
            "https://example.com/feed.xml",
        )
    )
    call = validate_read(
        {
            "source": "rss",
            "operation": "read.feed",
            "target": {"url": "https://example.com/feed.xml"},
        }
    )

    result = asyncio.run(RssAdapter(client).execute(ReadOnlyPolicy().authorize(call)))

    assert result.failure_class == "permanent"


def test_rss_adapter_returns_recovered_feedparser_entries_as_partial() -> None:
    malformed = (
        "<rss><channel><title>Feed</title><item><title>Recovered</title>"
        "<description>Recovered body</description></item>"
    )
    client = FixtureHttpClient(
        _response(
            malformed,
            "application/rss+xml",
            "https://example.com/feed.xml",
        )
    )
    call = validate_browse(
        {
            "source": "rss",
            "operation": "browse.entries",
            "target": {"url": "https://example.com/feed.xml"},
        }
    )

    result = asyncio.run(RssAdapter(client).execute(ReadOnlyPolicy().authorize(call)))

    assert result.partial_failure_class == "permanent"
    assert [(item.title, item.text) for item in result.items] == [
        ("Recovered", "Recovered body")
    ]


def test_rss_adapter_rejects_bozo_result_without_usable_data() -> None:
    client = FixtureHttpClient(
        _response(
            "<not-feed",
            "application/rss+xml",
            "https://example.com/feed.xml",
        )
    )
    call = validate_browse(
        {
            "source": "rss",
            "operation": "browse.entries",
            "target": {"url": "https://example.com/feed.xml"},
        }
    )

    result = asyncio.run(RssAdapter(client).execute(ReadOnlyPolicy().authorize(call)))

    assert result.failure_class == "permanent"
    assert result.items == ()


def test_rss_adapter_observes_dropped_unusable_entries_as_partial() -> None:
    feed = """<rss><channel><title>Feed</title>
    <item><title>Usable</title><description>Visible body</description></item>
    <item><link>https://example.com/link-only</link></item>
    </channel></rss>"""
    client = FixtureHttpClient(
        _response(
            feed,
            "application/rss+xml",
            "https://example.com/feed.xml",
        )
    )
    call = validate_browse(
        {
            "source": "rss",
            "operation": "browse.entries",
            "target": {"url": "https://example.com/feed.xml"},
        }
    )

    result = asyncio.run(RssAdapter(client).execute(ReadOnlyPolicy().authorize(call)))

    assert result.partial_failure_class == "permanent"
    assert [item.title for item in result.items] == ["Usable"]


def test_rss_runner_keeps_a_single_overflow_sentinel_for_truncation() -> None:
    entries = "".join(
        f"<item><title>Entry {index}</title><description>Body</description></item>"
        for index in range(22)
    )
    client = FixtureHttpClient(
        _response(
            f"<rss><channel><title>Feed</title>{entries}</channel></rss>",
            "application/rss+xml",
            "https://example.com/feed.xml",
        )
    )
    call = validate_browse(
        {
            "source": "rss",
            "operation": "browse.entries",
            "target": {"url": "https://example.com/feed.xml"},
            "options": {"limit": 50},
        }
    )

    result = asyncio.run(build_alpha1_runtime(client).dispatch(call))

    assert result is not None
    assert len(result.items) == 20
    assert result.truncated is True


def test_web_remains_planned_without_http_execution() -> None:
    client = FixtureHttpClient()
    registry = build_alpha1_registry(client)
    runtime = build_alpha1_runtime(client)
    call = validate_read(
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": "https://example.com/article"},
        }
    )
    operation = call.operation
    record = registry.availability("web", operation.name)

    assert operation.implementation_state == "planned"
    assert record.state == "unavailable"
    assert record.reason == operation.unavailable_reason
    assert record.backend_id is None
    assert record.backend_version is None
    assert registry.has_binding("web", operation.name) is False
    assert asyncio.run(runtime.dispatch(call)) is None
    assert client.calls == []


def test_v2ex_rows_have_fixed_fork_bindings_without_registry_io() -> None:
    client = FixtureHttpClient()
    registry = build_alpha1_registry(client)
    calls = (
        validate_browse({"source": "v2ex", "operation": "browse.hot"}),
        validate_browse(
            {
                "source": "v2ex",
                "operation": "browse.node_topics",
                "options": {"node": "python", "page": 3, "limit": 5},
            }
        ),
        validate_read(
            {
                "source": "v2ex",
                "operation": "read.topic",
                "target": {"native_id": "42"},
            }
        ),
        validate_read(
            {
                "source": "v2ex",
                "operation": "read.user",
                "target": {"native_id": "alice"},
            }
        ),
    )
    for call in calls:
        operation = call.operation
        record = registry.availability("v2ex", operation.name)

        assert operation.implementation_state == "implemented"
        assert record.state == "available"
        assert record.backend_id == "v2ex-public-api"
        assert record.backend_version == "legacy-json-2026-07-31"
        assert registry.has_binding("v2ex", operation.name) is True

    assert client.calls == []


def test_both_exa_searches_require_the_same_complete_artifact_attestation() -> None:
    registry = build_alpha1_registry(FixtureHttpClient())
    runtime = build_alpha1_runtime(FixtureHttpClient())

    for operation in ("search.web", "search.code"):
        record = registry.availability("exa", operation)
        call = validate_search(
            {
                "requests": [
                    {
                        "source": "exa",
                        "operation": operation,
                        "query": "private-query",
                        "options": {"limit": 3},
                    }
                ]
            }
        )[0]

        assert call.operation.implementation_state == "implemented"
        assert record.state == "setup_required"
        assert record.reason == call.operation.unavailable_reason
        assert record.backend_id is None
        assert record.backend_version is None
        assert registry.has_binding("exa", operation) is False
        assert asyncio.run(runtime.dispatch(call)) is None


def _exa_artifacts() -> ExaArtifactAttestation:
    return ExaArtifactAttestation(
        node_executable=Path("/operator/node"),
        node_sha256="1" * 64,
        mcporter_root=Path("/operator/mcporter"),
        mcporter_cli=Path("/operator/mcporter/dist/cli.js"),
        mcporter_tree_sha256="2" * 64,
        config_path=Path("/operator/exa-config.json"),
        config_sha256="3" * 64,
    )


def test_exa_artifacts_enable_two_independently_versioned_bindings() -> None:
    registry = build_alpha1_registry(exa_artifacts=_exa_artifacts())
    for operation, backend_version in (
        ("search.web", "0.12.3+exa-web.v1"),
        ("search.code", "0.12.3+exa-code.v1"),
    ):
        call = validate_search(
            {
                "requests": [
                    {
                        "source": "exa",
                        "operation": operation,
                        "query": "bounded query",
                        "options": {"limit": 3},
                    }
                ]
            }
        )[0]
        candidates = registry.candidates(ReadOnlyPolicy().authorize(call))

        assert len(candidates) == 1
        assert candidates[0].backend_id == "exa-mcporter"
        assert candidates[0].backend_version == backend_version
        assert candidates[0].retry_owner == "binding"
        assert registry.has_binding("exa", operation) is True


def test_invalid_exa_attestation_degrades_both_exa_searches_only() -> None:
    builder: Callable[..., AdapterRegistry] = build_alpha1_registry

    registry = builder(exa_artifacts=object())

    assert registry.has_binding("exa", "search.web") is False
    assert registry.availability("exa", "search.web").state == "setup_required"
    assert registry.has_binding("exa", "search.code") is False
    assert registry.availability("exa", "search.code").state == "setup_required"
    for source, operation in (
        ("rss", "read.feed"),
        ("v2ex", "browse.hot"),
        ("youtube", "read.video"),
        ("bilibili", "read.video"),
    ):
        assert registry.has_binding(source, operation) is True


def test_exa_composition_does_not_mask_unrelated_programmer_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_composition(_: ExaArtifactAttestation) -> tuple[()]:
        raise RuntimeError("programmer error")

    monkeypatch.setattr(source_registry, "exa_bindings", fail_composition)

    with pytest.raises(RuntimeError, match="programmer error"):
        build_alpha1_registry(exa_artifacts=_exa_artifacts())


def test_exa_client_injection_is_not_a_registry_activation_path() -> None:
    builder: Callable[..., object] = build_alpha1_runtime

    with pytest.raises(TypeError, match="no longer supported"):
        builder(exa_client=object())


def test_removed_exa_slot_preserves_positional_media_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    youtube_backend = object()
    bilibili_backend = object()
    captured: list[tuple[object, object]] = []

    def capture_media(
        registry: AdapterRegistry,
        youtube: object,
        bilibili: object,
    ) -> None:
        del registry
        captured.append((youtube, bilibili))

    monkeypatch.setattr(source_registry, "_register_media_backends", capture_media)
    builder: Callable[..., AdapterRegistry] = build_alpha1_registry

    builder(FixtureHttpClient(), None, youtube_backend, bilibili_backend)

    assert captured == [(youtube_backend, bilibili_backend)]


def test_exa_registry_construction_performs_no_process_network_or_file_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_io(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("registry construction attempted I/O")

    monkeypatch.setattr(builtins, "open", unexpected_io)
    monkeypatch.setattr(Path, "open", unexpected_io)
    monkeypatch.setattr(socket, "create_connection", unexpected_io)
    monkeypatch.setattr(subprocess, "run", unexpected_io)
    monkeypatch.setattr(subprocess, "Popen", unexpected_io)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_io)

    registry = build_alpha1_registry(
        FixtureHttpClient(),
        exa_artifacts=_exa_artifacts(),
    )

    assert registry.has_binding("exa", "search.web") is True
    assert registry.has_binding("exa", "search.code") is True


def test_registry_preserves_a_falsy_injected_http_client_for_rss() -> None:
    client = FalsyFixtureHttpClient(
        _response(
            "<rss><channel><title>Injected feed</title>"
            "<description>Injected response</description></channel></rss>",
            "application/rss+xml",
            "https://example.com/feed.xml",
        )
    )
    runtime = build_alpha1_runtime(client)
    call = validate_read(
        {
            "source": "rss",
            "operation": "read.feed",
            "target": {"url": "https://example.com/feed.xml"},
        }
    )

    result = asyncio.run(runtime.dispatch(call))

    assert result is not None
    assert result.items[0].title == "Injected feed"
    assert result.items[0].text == "Injected response"
    assert client.calls == ["https://example.com/feed.xml"]
