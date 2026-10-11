"""Tool registry: every capability the agent has is a `Tool` registered here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

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
    # True, False, or a predicate over the tool input (e.g. only non-GET requests).
    needs_approval: bool | Callable[[dict[str, Any]], bool] = False

    def requires_approval(self, tool_input: dict[str, Any]) -> bool:
        if callable(self.needs_approval):
            return bool(self.needs_approval(tool_input))
        return self.needs_approval

    def to_api(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


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


def build_registry(config: Config) -> ToolRegistry:
    from . import files, mail, shell, system, tasks, web

    config.workspace.mkdir(parents=True, exist_ok=True)

    tools: list[Tool] = []
    tools += system.build(config)
    tools += files.build(config)
    tools += shell.build(config)
    tools += tasks.build(config)
    tools += web.build(config)
    if config.smtp is not None:
        tools += mail.build(config)

    server_tools = web.server_tools() if config.web_tools else []
    return ToolRegistry(tools, server_tools)
