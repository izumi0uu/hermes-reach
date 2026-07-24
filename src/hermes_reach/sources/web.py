"""Deterministic generic web document adapter."""

from __future__ import annotations

from ..runtime.adapters import AdapterResult, RawItem
from ..runtime.policy import AuthorizedCall
from .documents import DocumentError, decode_document, extract_visible_html
from .public_http import HttpFailure, PublicHttpClient


class WebAdapter:
    def __init__(self, client: PublicHttpClient) -> None:
        self._client = client

    async def execute(self, authorized: AuthorizedCall) -> AdapterResult:
        target = authorized.call.target
        if target is None or "url" not in target:
            return AdapterResult(failure_class="invalid_input")
        try:
            response = await self._client.get(target["url"])
            media_type = response.content_type.split(";", 1)[0].strip()
            if media_type not in {
                "",
                "text/html",
                "application/xhtml+xml",
                "text/plain",
            }:
                return AdapterResult(failure_class="permanent")
            decoded = decode_document(response.body, response.content_type)
            if media_type in {"text/html", "application/xhtml+xml", ""}:
                text, title = extract_visible_html(decoded)
            else:
                text, title = decoded.strip(), None
            if not text:
                return AdapterResult(failure_class="permanent")
            return AdapterResult(
                (
                    RawItem(
                        text=text,
                        kind="content",
                        title=title,
                        url=response.public_url,
                    ),
                )
            )
        except HttpFailure as error:
            return AdapterResult(failure_class=error.failure_class)
        except DocumentError:
            return AdapterResult(failure_class="permanent")
        except Exception:
            return AdapterResult(failure_class="transient")
