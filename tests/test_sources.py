from __future__ import annotations

import asyncio
import json
from dataclasses import replace

from hermes_reach.contracts import validate_browse, validate_read, validate_search
from hermes_reach.runtime.adapters import RawItem
from hermes_reach.runtime.policy import ReadOnlyPolicy
from hermes_reach.sources.exa import (
    AuditedExaClient,
    ExaClientAttestation,
    exa_client_is_eligible,
)
from hermes_reach.sources.public_http import HttpFailure, HttpResponse
from hermes_reach.sources.registry import build_alpha1_runtime
from hermes_reach.sources.rss import RssAdapter
from hermes_reach.sources.v2ex import V2exAdapter
from hermes_reach.sources.web import WebAdapter


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


def test_web_adapter_extracts_visible_text_without_hidden_content() -> None:
    client = FixtureHttpClient(
        _response(
            """
            <html><head><title>Useful title</title><style>hidden-style</style></head>
            <body><main>Hello <strong>world</strong></main>
            <script>hidden-script</script></body></html>
            """,
            "text/html; charset=utf-8",
            "https://example.com/article",
        )
    )
    call = validate_read(
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": "https://example.com/article?private=value"},
        }
    )

    result = asyncio.run(WebAdapter(client).execute(ReadOnlyPolicy().authorize(call)))

    assert result.is_success
    assert result.items[0].title == "Useful title"
    assert "Hello world" in result.items[0].text
    assert "hidden-style" not in result.items[0].text
    assert "hidden-script" not in result.items[0].text
    assert result.items[0].url == "https://example.com/article"


def test_rss_adapter_normalizes_atom_entries_in_native_order() -> None:
    atom = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Example feed</title><subtitle>A useful feed</subtitle>
      <link rel="alternate" href="https://example.com/feed" />
      <entry><id>entry-2</id><title>Second</title>
        <link href="/second?tracking=yes" />
        <author><name>Alice</name></author><updated>2026-07-24T02:00:00Z</updated>
        <summary>&lt;p&gt;Second body&lt;/p&gt;</summary></entry>
      <entry><id>entry-1</id><title>First</title>
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
    assert [item.native_id for item in result.items] == ["entry-2", "entry-1"]
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


def test_v2ex_adapter_owns_routes_and_reports_reply_failure_as_partial() -> None:
    topic = json.dumps(
        [
            {
                "id": 42,
                "title": "Topic",
                "content": "Topic body",
                "created": 1_700_000_000,
                "member": {"username": "alice"},
            }
        ]
    )
    client = FixtureHttpClient(
        _response(topic, "application/json", "https://www.v2ex.com/t/42"),
        HttpFailure("transient", "reply_timeout"),
    )
    call = validate_read(
        {
            "source": "v2ex",
            "operation": "read.topic",
            "target": {"native_id": "42"},
        }
    )

    result = asyncio.run(V2exAdapter(client).execute(ReadOnlyPolicy().authorize(call)))

    assert result.partial_failure_class == "transient"
    assert result.items[0].native_id == "42"
    assert result.items[0].url == "https://www.v2ex.com/t/42"
    assert client.calls == [
        "https://www.v2ex.com/api/topics/show.json?id=42",
        "https://www.v2ex.com/api/replies/show.json?topic_id=42&page=1",
    ]


def test_v2ex_keeps_topic_when_reply_client_raises_unexpected_error() -> None:
    topic = json.dumps(
        [
            {
                "id": 42,
                "title": "Topic",
                "content": "Topic body",
                "created": 1_700_000_000,
                "member": {"username": "alice"},
            }
        ]
    )
    client = FixtureHttpClient(
        _response(topic, "application/json", "https://www.v2ex.com/t/42"),
        RuntimeError("private-upstream-error"),
    )
    call = validate_read(
        {
            "source": "v2ex",
            "operation": "read.topic",
            "target": {"native_id": "42"},
        }
    )

    result = asyncio.run(V2exAdapter(client).execute(ReadOnlyPolicy().authorize(call)))

    assert result.partial_failure_class == "transient"
    assert [item.native_id for item in result.items] == ["42"]


def test_v2ex_node_route_uses_only_validated_query_values() -> None:
    client = FixtureHttpClient(
        _response("[]", "application/json", "https://www.v2ex.com/")
    )
    call = validate_browse(
        {
            "source": "v2ex",
            "operation": "browse.node_topics",
            "options": {"node": "python", "page": 3, "limit": 5},
        }
    )

    result = asyncio.run(V2exAdapter(client).execute(ReadOnlyPolicy().authorize(call)))

    assert result.is_success
    assert client.calls == [
        "https://www.v2ex.com/api/topics/show.json?node_name=python&page=3"
    ]


class FixtureExaClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def search_web(self, query: str, limit: int) -> tuple[RawItem, ...]:
        self.calls.append(("web", query, limit))
        return (RawItem("result", kind="result", title="Web result"),)

    async def search_code(self, query: str, limit: int) -> tuple[RawItem, ...]:
        self.calls.append(("code", query, limit))
        return (RawItem("code", kind="result", title="Code result"),)


def _exa_bundle(client: FixtureExaClient) -> AuditedExaClient:
    return AuditedExaClient(
        client,
        ExaClientAttestation(
            provider_id="exa",
            provider_version="1.2.3",
            operations=frozenset({"search.web", "search.code"}),
            logs_queries=False,
            persists_content=False,
            hidden_model_processing=False,
            runtime_dependency_install=False,
        ),
    )


def test_exa_defaults_to_setup_required_and_binds_only_audited_client() -> None:
    default_runtime = build_alpha1_runtime()
    default = default_runtime.operation_availability("exa", "search.web")

    client = FixtureExaClient()
    bundle = _exa_bundle(client)
    assert exa_client_is_eligible(bundle)
    runtime = build_alpha1_runtime(FixtureHttpClient(), bundle)
    call = validate_search(
        {
            "requests": [
                {
                    "source": "exa",
                    "operation": "search.web",
                    "query": "private-query",
                    "options": {"limit": 3},
                }
            ]
        }
    )[0]
    result = asyncio.run(runtime.dispatch(call))
    default_result = asyncio.run(default_runtime.dispatch(call))

    assert default.state == "setup_required"
    assert default_result is None
    assert result is not None
    assert result.items[0].title == "Web result"
    assert client.calls == [("web", "private-query", 3)]


def test_registry_preserves_a_falsy_injected_http_client() -> None:
    client = FalsyFixtureHttpClient(
        _response(
            "<html><body>Injected response</body></html>",
            "text/html; charset=utf-8",
            "https://example.com/article",
        )
    )
    runtime = build_alpha1_runtime(client)
    call = validate_read(
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": "https://example.com/article"},
        }
    )

    result = asyncio.run(runtime.dispatch(call))

    assert result is not None
    assert result.items[0].text == "Injected response"
    assert client.calls == ["https://example.com/article"]


def test_exa_rejects_a_query_logging_attestation_without_calling_client() -> None:
    client = FixtureExaClient()
    bundle = _exa_bundle(client)
    unsafe = AuditedExaClient(
        client,
        replace(bundle.attestation, logs_queries=True),
    )

    runtime = build_alpha1_runtime(FixtureHttpClient(), unsafe)

    assert runtime.operation_availability("exa", "search.web").state == "unavailable"
    assert client.calls == []
