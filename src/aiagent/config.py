"""Configuration loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "ano")


@dataclass
class SmtpConfig:
    host: str
    port: int = 587
    user: str = ""
    password: str = ""
    sender: str = ""
    starttls: bool = True


@dataclass
class Config:
    model: str = "claude-opus-5-5"
    effort: str = "high"
    workspace: Path = field(default_factory=lambda: Path("workspace").resolve())
    timezone: str = "Europe/Prague"
    web_tools: bool = True
    smtp: SmtpConfig | None = None
    max_iterations: int = 50

    @classmethod
    def from_env(cls) -> "Config":
        # Look for .env from the current directory upwards.
        load_dotenv(find_dotenv(usecwd=True))

        effort = (os.environ.get("AIAGENT_EFFORT") or "high").strip().lower()
        if effort not in EFFORT_LEVELS:
            raise ValueError(
                f"AIAGENT_EFFORT musí být jedno z {', '.join(EFFORT_LEVELS)}, ne '{effort}'"
            )

        smtp = None
        if os.environ.get("SMTP_HOST"):
            smtp = SmtpConfig(
                host=os.environ["SMTP_HOST"],
                port=int(os.environ.get("SMTP_PORT") or 587),
                user=os.environ.get("SMTP_USER", ""),
                password=os.environ.get("SMTP_PASSWORD", ""),
                sender=os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER", ""),
                starttls=_flag("SMTP_STARTTLS", True),
            )

        workspace = Path(os.environ.get("AIAGENT_WORKSPACE") or "workspace").expanduser().resolve()

        return cls(
            model=os.environ.get("AIAGENT_MODEL") or "claude-opus-5-5",
            effort=effort,
            workspace=workspace,
            timezone=os.environ.get("AIAGENT_TIMEZONE") or "Europe/Prague",
            web_tools=_flag("AIAGENT_WEB_TOOLS", True),
            smtp=smtp,
        )
