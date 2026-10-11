"""HTTP requests (webhooks, REST APIs) and Anthropic's server-side web tools."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..config import Config
from . import Tool, ToolError, schema, truncate

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def server_tools() -> list[dict[str, Any]]:
    # Run on Anthropic's infrastructure; results come back in the same response.
    return [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 5},
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
        if not url.startswith(("http://", "https://")):
            raise ToolError("URL musí začínat http:// nebo https://.")

        data = None
        req_headers = {"User-Agent": "aiagent/0.1", **(headers or {})}
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        elif body is not None:
            data = body.encode("utf-8")

        request = urllib.request.Request(url, data=data, method=method, headers=req_headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                status = resp.status
                text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            text = exc.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            raise ToolError(f"Požadavek selhal: {exc.reason}") from exc
        return truncate(f"HTTP {status}\n{text}")

    return [
        Tool(
            name="http_request",
            description=(
                "Odešle HTTP požadavek, např. na webhook (Make, n8n, Zapier, Slack) nebo REST API, "
                "a vrátí stavový kód a tělo odpovědi. Pro JSON použij json_body. "
                "Jiné metody než GET vyžadují schválení uživatelem."
            ),
            input_schema=schema(
                {
                    "url": {"type": "string"},
                    "method": {"type": "string", "enum": list(METHODS)},
                    "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                    "body": {"type": "string", "description": "Tělo požadavku jako text."},
                    "json_body": {"type": "object", "description": "Tělo požadavku jako JSON."},
                },
                ["url"],
            ),
            handler=http_request,
            needs_approval=lambda tool_input: str(tool_input.get("method", "GET")).upper() != "GET",
        )
    ]
