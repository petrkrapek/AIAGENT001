"""Sending outreach e-mails over SMTP. Only registered when sending is enabled (AIAGENT_SEND_MODE)."""

from __future__ import annotations

import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid

from ..config import Config, SmtpConfig
from ..storage import now_iso
from . import Tool, ToolError, schema
from .leads import LeadStore


def open_smtp(smtp: SmtpConfig) -> smtplib.SMTP:
    """Connect, secure and log in according to the configuration."""
    context = ssl.create_default_context()
    if smtp.security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(smtp.host, smtp.port, timeout=30)
        if smtp.security == "starttls":
            server.starttls(context=context)
    if smtp.user:
        server.login(smtp.user, smtp.password)
    return server


def deliver(smtp: SmtpConfig, msg: EmailMessage) -> None:
    try:
        with open_smtp(smtp) as server:
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise ToolError(f"Odeslání e-mailu selhalo: {exc}") from exc


def build_message(smtp: SmtpConfig, to: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = smtp.sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=smtp.sender.rsplit("@", 1)[-1] or None)
    msg.set_content(body)
    return msg


def sent_today(store: LeadStore) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    return sum(
        1 for lead in store.load() for sent in lead.get("emails", []) if sent["sent_at"].startswith(today)
    )


def build(config: Config, store: LeadStore) -> list[Tool]:
    smtp = config.smtp
    assert smtp is not None
    auto = config.send_mode == "auto"

    def send_email(lead_id: int, to: str, subject: str, body: str, follow_up: bool = False) -> str:
        lead = store.get(lead_id)
        if lead["status"] == "do_not_contact" or store.is_suppressed(to):
            raise ToolError("Tento kontakt si nepřeje být oslovován. E-mail neodeslán.")
        if lead.get("emails") and not follow_up:
            raise ToolError(
                f"Lead #{lead_id} už byl osloven {lead['emails'][-1]['sent_at']}. "
                "Další zprávu pošli jen jako follow_up=true, pokud o to uživatel stojí."
            )
        if auto:
            # Without a human in the loop, only send exactly what was saved for review.
            draft = lead.get("draft")
            if not draft or (draft["subject"], draft["body"]) != (subject, body):
                raise ToolError(
                    "V automatickém režimu lze odeslat jen text shodný s uloženým návrhem. "
                    "Nejdřív ho ulož přes save_draft."
                )
        if sent_today(store) >= config.daily_email_limit:
            raise ToolError(f"Dosažen denní limit {config.daily_email_limit} odeslaných e-mailů.")

        deliver(smtp, build_message(smtp, to, subject, body))

        emails = [*lead.get("emails", []), {"to": to, "subject": subject, "body": body, "sent_at": now_iso()}]
        store.update(lead_id, emails=emails, status="contacted")
        return f"E-mail '{subject}' odeslán na {to}; lead #{lead_id} je ve stavu contacted."

    if auto:
        approval_note = (
            "Odesílá se automaticky bez schválení, proto smí odeslat jen text shodný s návrhem "
            "uloženým přes save_draft."
        )
    else:
        approval_note = "Každé odeslání schvaluje uživatel, který uvidí celý text."

    return [
        Tool(
            name="send_email",
            description=(
                f"Odešle obchodní e-mail z adresy {smtp.sender} kontaktu z databáze leadů a zapíše "
                f"ho do historie leadu. {approval_note} Odmítne kontakty ve stavu do_not_contact, "
                "již oslovené leady (bez follow_up) a vše nad denní limit "
                f"{config.daily_email_limit} e-mailů."
            ),
            input_schema=schema(
                {
                    "lead_id": {"type": "integer"},
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string", "description": "Celý text e-mailu včetně podpisu."},
                    "follow_up": {
                        "type": "boolean",
                        "description": "Navazující zpráva už osloveným (jen na pokyn uživatele).",
                    },
                },
                ["lead_id", "to", "subject", "body"],
            ),
            handler=send_email,
            needs_approval=not auto,
        )
    ]


def smtp_check(smtp: SmtpConfig, test_to: str | None = None) -> str:
    """Verify the SMTP connection and login; optionally send a test message."""
    try:
        with open_smtp(smtp) as server:
            if test_to:
                server.send_message(
                    build_message(
                        smtp, test_to, "Test odesílání z AI obchodníka",
                        "Tento e-mail ověřuje, že agent umí odesílat poštu přes SMTP.",
                    )
                )
    except (smtplib.SMTPException, OSError) as exc:
        raise ToolError(f"SMTP nefunguje: {exc}") from exc
    login = " a přihlášení" if smtp.user else ""
    result = f"Připojení k {smtp.host}:{smtp.port} ({smtp.security}){login} je v pořádku."
    if test_to:
        result += f" Testovací e-mail odeslán na {test_to}."
    return result
