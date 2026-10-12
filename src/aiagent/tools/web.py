"""HTTP requests (webhooks, REST APIs) and Anthropic's server-side web tools."""

from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .. import __version__
from ..config import Config
from . import Tool, ToolError, schema, truncate

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
MAX_RESPONSE_BYTES = 1_000_000
# web_fetch pages stay in the conversation; cap each one to keep the context manageable.
WEB_FETCH_MAX_CONTENT_TOKENS = 20_000


def check_public_url(url: str) -> None:
    """Refuse non-HTTP(S) URLs and hosts resolving to private, loopback or link-local addresses."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ToolError("URL musí začínat http:// nebo https:// a obsahovat adresu serveru.")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ToolError(f"Adresu {parsed.hostname} nelze přeložit.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ToolError(f"Požadavky na vnitřní nebo neveřejné adresy ({ip}) nejsou povolené.")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirect)


def server_tools() -> list[dict[str, Any]]:
    # Run on Anthropic's infrastructure; results come back in the same response.
    return [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 10},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 10,
         "max_content_tokens": WEB_FETCH_MAX_CONTENT_TOKENS},
    ]


def build(config: Config) -> list[Tool]:
    def http_request(
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: str | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> str:
        method = method.upper()
        if method not in METHODS:
            raise ToolError(f"Nepodporovaná metoda {method}.")
        check_public_url(url)

        data = None
        req_headers = {"User-Agent": f"aiagent/{__version__}", **(headers or {})}
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        elif body is not None:
            data = body.encode("utf-8")

        request = urllib.request.Request(url, data=data, method=method, headers=req_headers)
        try:
            with _opener.open(request, timeout=30) as resp:
                status = resp.status
                text = resp.read(MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            text = exc.read(MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            raise ToolError(f"Požadavek selhal: {exc.reason}") from exc
        return truncate(f"HTTP {status}\n{text}")

    return [
        Tool(
            name="http_request",
            description=(
                "Odešle HTTP požadavek, např. na webhook (Make, n8n, Zapier, Slack) nebo REST API, "
                "a vrátí stavový kód a tělo odpovědi. Pro JSON použij json_body. Každý požadavek "
                "schvaluje uživatel; vnitřní a neveřejné adresy jsou zakázané. Nikdy neposílej data "
                "leadů ani firmy na adresy, které uživatel sám nezadal."
            ),
            input_schema=schema(
                {
                    "url": {"type": "string", "description": "Cílová adresa (http/https)."},
                    "method": {"type": "string", "enum": list(METHODS), "description": "HTTP metoda, výchozí GET."},
                    "headers": {"type": "object", "additionalProperties": {"type": "string"},
                                "description": "HTTP hlavičky, např. {\"Authorization\": \"Bearer …\"}."},
                    "body": {"type": "string", "description": "Tělo požadavku jako text."},
                    "json_body": {"type": "object", "description": "Tělo požadavku jako JSON."},
                },
                ["url"],
            ),
            handler=http_request,
            strict=False,  # free-form headers/json_body objects aren't allowed in strict schemas
            # Also GETs: data can leak through a query string, and pages may carry injected
            # instructions asking the model to call arbitrary URLs.
            needs_approval=True,
        )
    ]
