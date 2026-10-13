"""HTTP requests (webhooks, REST APIs) and Anthropic's server-side web tools."""

from __future__ import annotations

import http.client
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


def public_ips(host: str, port: int) -> list[str]:
    """Addresses of the host; refuses it if any of them is private, loopback or link-local."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ToolError(f"Adresu {host} nelze přeložit.") from exc
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ToolError(f"Požadavky na vnitřní nebo neveřejné adresy ({ip}) nejsou povolené.")
        ips.append(str(ip))
    return ips


def check_public_url(url: str) -> None:
    """Refuse non-HTTP(S) URLs and hosts resolving to private, loopback or link-local addresses."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ToolError("URL musí začínat http:// nebo https:// a obsahovat adresu serveru.")
    public_ips(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))


def _connect_public(address: tuple[str, int], *args: Any) -> socket.socket:
    """socket.create_connection that connects only to an address it has just checked.

    Checking the URL first and letting the connection resolve the name again would let a DNS
    server answer with a public address for the check and an internal one for the connection.
    """
    host, port = address
    error: OSError | None = None
    for ip in public_ips(host, port):
        try:
            return socket.create_connection((ip, port), *args)
        except OSError as exc:
            error = exc
    raise error or OSError(f"Nelze se připojit k {host}.")


def _public_only(http_class: type[http.client.HTTPConnection]) -> type[http.client.HTTPConnection]:
    class PublicConnection(http_class):  # type: ignore[valid-type, misc]
        def __init__(self, *args: Any, **kwargs: Any):
            super().__init__(*args, **kwargs)
            self._create_connection = _connect_public  # TLS still verifies the certificate for the host name

    return PublicConnection


class _PublicOnly:
    def do_open(self, http_class, req, **kwargs):
        # Through a proxy, the connection goes to the proxy, which resolves the target itself.
        if not (req.has_proxy() or req._tunnel_host):
            http_class = _public_only(http_class)
        return super().do_open(http_class, req, **kwargs)  # type: ignore[misc]


class _PublicHTTPHandler(_PublicOnly, urllib.request.HTTPHandler):
    pass


class _PublicHTTPSHandler(_PublicOnly, urllib.request.HTTPSHandler):
    pass


def url_host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower()


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme, (parsed.hostname or "").lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public_url(newurl)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _origin(newurl) != _origin(req.full_url):
            # urllib copies every header to the new URL; tokens, API keys and cookies the caller set
            # belong to the original server only (and must not travel over plain http either).
            for name in list(new.headers):
                if name.lower() != "user-agent":
                    del new.headers[name]
        return new


_opener = urllib.request.build_opener(_SafeRedirect, _PublicHTTPHandler, _PublicHTTPSHandler)


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
