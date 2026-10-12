"""Tool registry: every capability the agent has is a `Tool` registered here."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import Config

MAX_RESULT_CHARS = 20_000


class ToolError(Exception):
    """Raised by a tool handler to return an error result to the model."""


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., str]
    # True, False, or a predicate over the tool input.
    needs_approval: bool | Callable[[dict[str, Any]], bool] = False
    # Strict mode guarantees schema-valid arguments; needs a schema without free-form objects.
    strict: bool = True

    def requires_approval(self, tool_input: dict[str, Any]) -> bool:
        if callable(self.needs_approval):
            return bool(self.needs_approval(tool_input))
        return self.needs_approval

    def to_api(self) -> dict[str, Any]:
        definition: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
        if self.strict:
            definition["strict"] = True
        return definition


def schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def truncate(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... zkráceno, celkem {len(text)} znaků]"


class ToolRegistry:
    def __init__(self, tools: list[Tool], server_tools: list[dict[str, Any]] | None = None):
        self._tools = {tool.name: tool for tool in tools}
        self.server_tools = server_tools or []

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @property
    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def to_api(self) -> list[dict[str, Any]]:
        # Sorted, deterministic order keeps the prompt-cache prefix stable.
        client_tools = [self._tools[name].to_api() for name in sorted(self._tools)]
        return client_tools + self.server_tools


def build_registry(config: Config, repos: Any) -> ToolRegistry:
    """All agent data goes through `repos` (Airtable in production)."""
    from . import ares, leads, mail, system, tasks, web

    tools: list[Tool] = []
    tools += system.build(config)
    tools += ares.build(config)
    tools += leads.build(config, repos.leads)
    tools += tasks.build(config, repos.tasks)
    tools += web.build(config)
    if config.sending_enabled:
        tools += mail.build(config, repos.leads)

    server_tools = web.server_tools() if config.web_tools else []
    return ToolRegistry(tools, server_tools)
