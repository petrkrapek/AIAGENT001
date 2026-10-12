"""Tool registry: every capability the agent has is a `Tool` registered here."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import Config

MAX_RESULT_CHARS = 20_000
# The API rejects requests whose strict tool schemas have more optional parameters than this.
MAX_STRICT_OPTIONAL_PARAMS = 24


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

    @property
    def optional_params(self) -> int:
        schema = self.input_schema
        return len(schema.get("properties", {})) - len(schema.get("required", []))

    def to_api(self, strict: bool | None = None) -> dict[str, Any]:
        definition: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
        if self.strict if strict is None else strict:
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


def strict_tool_names(tools: list[Tool], limit: int = MAX_STRICT_OPTIONAL_PARAMS) -> set[str]:
    """Tools that can keep strict mode within the API's optional-parameter budget.

    Strict mode is dropped first for the tools with the most optional parameters (e.g. save_lead);
    their handlers validate input themselves. Deterministic, so the prompt cache stays stable.
    """
    strict = sorted((t for t in tools if t.strict), key=lambda t: (-t.optional_params, t.name))
    total = sum(t.optional_params for t in strict)
    while strict and total > limit:
        total -= strict.pop(0).optional_params
    return {t.name for t in strict}


class ToolRegistry:
    def __init__(self, tools: list[Tool], server_tools: list[dict[str, Any]] | None = None):
        self._tools = {tool.name: tool for tool in tools}
        self.server_tools = server_tools or []
        self._strict = strict_tool_names(tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @property
    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def to_api(self) -> list[dict[str, Any]]:
        # Sorted, deterministic order keeps the prompt-cache prefix stable.
        client_tools = [self._tools[name].to_api(strict=name in self._strict) for name in sorted(self._tools)]
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
