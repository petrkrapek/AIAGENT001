"""Date and time."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import Config
from . import Tool, ToolError, schema

DAYS_CS = ["pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle"]


def build(config: Config) -> list[Tool]:
    def get_current_time(timezone: str | None = None) -> str:
        name = timezone or config.timezone
        try:
            now = datetime.now(ZoneInfo(name))
        except ZoneInfoNotFoundError as exc:
            raise ToolError(f"Neznámé časové pásmo: {name}") from exc
        return f"{now.isoformat(timespec='seconds')} ({DAYS_CS[now.weekday()]}, {name})"

    return [
        Tool(
            name="get_current_time",
            description=(
                "Vrátí aktuální datum a čas. Použij vždy, když potřebuješ znát dnešní datum, "
                "den v týdnu nebo počítat termíny."
            ),
            input_schema=schema(
                {
                    "timezone": {
                        "type": "string",
                        "description": f"IANA časové pásmo, výchozí {config.timezone}.",
                    }
                }
            ),
            handler=get_current_time,
        )
    ]
