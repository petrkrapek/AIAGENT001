"""Configuration loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# off: drafts only; approve: every e-mail needs confirmation; auto: send without asking
SEND_MODES = ("off", "approve", "auto")
SMTP_SECURITY = ("starttls", "ssl", "none")


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
    security: str = "starttls"


@dataclass
class Config:
    model: str = "claude-opus-5-5"
    effort: str = "high"
    workspace: Path = field(default_factory=lambda: Path("workspace").resolve())
    timezone: str = "Europe/Prague"
    web_tools: bool = True
    smtp: SmtpConfig | None = None
    send_mode: str = "off"
    profile_path: Path = field(default_factory=lambda: Path("profil.md").resolve())
    daily_email_limit: int = 20
    max_iterations: int = 80

    def validate(self) -> None:
        if self.send_mode not in SEND_MODES:
            raise ValueError(f"AIAGENT_SEND_MODE musí být jedno z {', '.join(SEND_MODES)}, ne '{self.send_mode}'")
        if self.send_mode != "off" and self.smtp is None:
            raise ValueError(f"AIAGENT_SEND_MODE={self.send_mode} vyžaduje nastavené SMTP (SMTP_HOST a další).")
        if self.smtp is not None and not self.smtp.sender:
            raise ValueError("Chybí SMTP_FROM (adresa odesílatele).")

    @property
    def sending_enabled(self) -> bool:
        return self.send_mode != "off" and self.smtp is not None

    def load_profile(self) -> str | None:
        """The seller's company and offer description, or None if not filled in yet."""
        if not self.profile_path.is_file():
            return None
        text = self.profile_path.read_text(encoding="utf-8").strip()
        return text or None

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
            port = int(os.environ.get("SMTP_PORT") or 587)
            security = (os.environ.get("SMTP_SECURITY") or ("ssl" if port == 465 else "starttls")).lower()
            if security not in SMTP_SECURITY:
                raise ValueError(f"SMTP_SECURITY musí být jedno z {', '.join(SMTP_SECURITY)}, ne '{security}'")
            smtp = SmtpConfig(
                host=os.environ["SMTP_HOST"],
                port=port,
                user=os.environ.get("SMTP_USER", ""),
                password=os.environ.get("SMTP_PASSWORD", ""),
                sender=os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER", ""),
                security=security,
            )

        send_mode = (os.environ.get("AIAGENT_SEND_MODE") or ("approve" if smtp else "off")).lower()

        workspace = Path(os.environ.get("AIAGENT_WORKSPACE") or "workspace").expanduser().resolve()

        return cls(
            model=os.environ.get("AIAGENT_MODEL") or "claude-opus-5-5",
            effort=effort,
            workspace=workspace,
            timezone=os.environ.get("AIAGENT_TIMEZONE") or "Europe/Prague",
            web_tools=_flag("AIAGENT_WEB_TOOLS", True),
            smtp=smtp,
            send_mode=send_mode,
            profile_path=Path(os.environ.get("AIAGENT_PROFILE") or "profil.md").expanduser().resolve(),
            daily_email_limit=int(os.environ.get("AIAGENT_DAILY_EMAIL_LIMIT") or 20),
        )
