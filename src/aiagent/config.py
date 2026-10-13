"""Configuration loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# off: drafts only; approve: every e-mail needs confirmation; auto: send without asking
SEND_MODES = ("off", "approve", "auto")
SMTP_SECURITY = ("starttls", "ssl", "none")


def _int(name: str, default: int, low: int, high: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} musí být celé číslo, ne '{raw}'.") from None
    if not low <= value <= high:
        raise ValueError(f"{name} musí být mezi {low} a {high}, ne {value}.")
    return value


def _range(name: str, default: tuple[int, int] | None, low: int, high: int) -> tuple[int, int] | None:
    """'30-90' -> (30, 90), '60' -> (60, 60); empty -> default."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    parts = [p.strip() for p in raw.split("-")]
    try:
        start, end = (int(parts[0]), int(parts[-1])) if len(parts) <= 2 else (None, None)
    except ValueError:
        start = end = None
    if start is None or end is None or not low <= start <= end <= high:
        raise ValueError(f"{name} má tvar od-do (celá čísla {low} až {high}), např. '{low}-{high}', ne '{raw}'.")
    return start, end


def _pause() -> tuple[int, int]:
    pause = _range("AIAGENT_SEND_PAUSE", (30, 90), 0, 3600)
    assert pause is not None  # _range returns the default for an empty value
    return pause


def _context_trigger() -> int:
    value = _int("AIAGENT_CONTEXT_TRIGGER", 60_000, 0, 1_000_000)
    if 0 < value < 20_000:
        raise ValueError("AIAGENT_CONTEXT_TRIGGER musí být 0 (vypnuto) nebo aspoň 20000 tokenů.")
    return value


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
class ImapConfig:
    """Mailbox where copies of sent e-mails are stored (folder Odeslané / Sent)."""

    host: str
    port: int = 993
    user: str = ""
    password: str = ""
    # Empty: detect the Sent folder on the server.
    folder: str = ""


@dataclass
class Config:
    model: str = "claude-opus-5-5"
    # Claude Opus 5.5 at medium effort beats earlier models at high; raise it only if needed.
    effort: str = "medium"
    timezone: str = "Europe/Prague"
    web_tools: bool = True
    smtp: SmtpConfig | None = None
    imap: ImapConfig | None = None
    airtable_token: str = ""
    airtable_base_id: str = ""
    send_mode: str = "off"
    daily_email_limit: int = 20
    # Random pause in seconds between two e-mails sent by one run (deliverability).
    send_pause: tuple[int, int] = (30, 90)
    # Sending window (start hour inclusive, end hour exclusive) on weekdays; None = any time.
    send_hours: tuple[int, int] | None = None
    max_iterations: int = 80
    # Old tool results are cleared server-side once a request exceeds this many input tokens (0 = off).
    context_trigger: int = 60_000
    # Each company is researched by a separate, short-lived agent (research_company).
    subagents: bool = True
    subagent_model: str = ""  # empty = the main model
    subagent_max_iterations: int = 30
    # Hosts that `--yes` may call with http_request without asking (e.g. a Make webhook); empty = none.
    http_allowed_hosts: tuple[str, ...] = ()

    def validate(self) -> None:
        if self.send_mode not in SEND_MODES:
            raise ValueError(f"AIAGENT_SEND_MODE musí být jedno z {', '.join(SEND_MODES)}, ne '{self.send_mode}'")
        if self.send_mode != "off" and self.smtp is None:
            raise ValueError(f"AIAGENT_SEND_MODE={self.send_mode} vyžaduje nastavené SMTP (SMTP_HOST a další).")
        if self.send_hours is not None and self.send_hours[0] >= self.send_hours[1]:
            raise ValueError("AIAGENT_SEND_HOURS: začátek musí být dřív než konec, např. 8-17.")
        if self.smtp is not None and not self.smtp.sender:
            raise ValueError("Chybí SMTP_FROM (adresa odesílatele).")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                f"AIAGENT_TIMEZONE '{self.timezone}' není platné časové pásmo (např. Europe/Prague)."
            ) from None

    def validate_airtable(self) -> None:
        if not self.airtable_token or not self.airtable_base_id:
            raise ValueError(
                "Agent ukládá data do Airtable: nastavte AIRTABLE_TOKEN a AIRTABLE_BASE_ID v .env."
            )
        if not self.airtable_base_id.startswith("app"):
            raise ValueError("AIRTABLE_BASE_ID má tvar appXXXXXXXXXXXXXX (najdete ho v URL báze).")

    @property
    def sending_enabled(self) -> bool:
        return self.send_mode != "off" and self.smtp is not None

    @classmethod
    def from_env(cls) -> Config:
        # Only ./.env (or an explicit file), never one found in some parent directory.
        load_dotenv(os.environ.get("AIAGENT_ENV_FILE") or ".env")

        effort = (os.environ.get("AIAGENT_EFFORT") or "medium").strip().lower()
        if effort not in EFFORT_LEVELS:
            raise ValueError(
                f"AIAGENT_EFFORT musí být jedno z {', '.join(EFFORT_LEVELS)}, ne '{effort}'"
            )

        smtp = None
        if os.environ.get("SMTP_HOST"):
            port = _int("SMTP_PORT", 587, 1, 65535)
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

        imap = None
        if os.environ.get("IMAP_HOST"):
            imap = ImapConfig(
                host=os.environ["IMAP_HOST"],
                port=_int("IMAP_PORT", 993, 1, 65535),
                user=os.environ.get("IMAP_USER") or os.environ.get("SMTP_USER", ""),
                password=os.environ.get("IMAP_PASSWORD") or os.environ.get("SMTP_PASSWORD", ""),
                folder=(os.environ.get("IMAP_SENT_FOLDER") or "").strip(),
            )

        send_mode = ((os.environ.get("AIAGENT_SEND_MODE") or "").strip() or ("approve" if smtp else "off")).lower()

        return cls(
            model=os.environ.get("AIAGENT_MODEL") or "claude-opus-5-5",
            effort=effort,
            timezone=(os.environ.get("AIAGENT_TIMEZONE") or "Europe/Prague").strip(),
            web_tools=_flag("AIAGENT_WEB_TOOLS", True),
            smtp=smtp,
            imap=imap,
            airtable_token=(os.environ.get("AIRTABLE_TOKEN") or "").strip(),
            airtable_base_id=(os.environ.get("AIRTABLE_BASE_ID") or "").strip(),
            send_mode=send_mode,
            daily_email_limit=_int("AIAGENT_DAILY_EMAIL_LIMIT", 20, 0, 10_000),
            send_pause=_pause(),
            send_hours=_range("AIAGENT_SEND_HOURS", None, 0, 24),
            context_trigger=_context_trigger(),
            subagents=_flag("AIAGENT_SUBAGENTS", True),
            subagent_model=(os.environ.get("AIAGENT_SUBAGENT_MODEL") or "").strip(),
            http_allowed_hosts=tuple(
                host.strip().lower() for host in (os.environ.get("AIAGENT_HTTP_ALLOWED_HOSTS") or "").split(",")
                if host.strip()
            ),
        )
