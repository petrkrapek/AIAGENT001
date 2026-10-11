"""Sending outreach e-mails over SMTP. Only registered when SMTP_HOST is configured."""

from __future__ import annotations

import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid

from ..config import Config
from ..storage import now_iso
from . import Tool, ToolError, schema
from .leads import LeadStore


def sent_today(store: LeadStore) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    return sum(
        1 for lead in store.load() for sent in lead.get("emails", []) if sent["sent_at"].startswith(today)
    )


def build(config: Config, store: LeadStore) -> list[Tool]:
    smtp = config.smtp
    assert smtp is not None

    def send_email(lead_id: int, to: str, subject: str, body: str, follow_up: bool = False) -> str:
        lead = store.get(lead_id)
        if lead["status"] == "do_not_contact" or store.is_suppressed(to):
            raise ToolError("Tento kontakt si nepřeje být oslovován. E-mail neodeslán.")
        if lead.get("emails") and not follow_up:
            raise ToolError(
                f"Lead #{lead_id} už byl osloven {lead['emails'][-1]['sent_at']}. "
                "Další zprávu pošli jen jako follow_up=true, pokud o to uživatel stojí."
            )
        if sent_today(store) >= config.daily_email_limit:
            raise ToolError(f"Dosažen denní limit {config.daily_email_limit} odeslaných e-mailů.")

        msg = EmailMessage()
        msg["From"] = smtp.sender
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid()
        msg.set_content(body)
        try:
            with smtplib.SMTP(smtp.host, smtp.port, timeout=30) as server:
                if smtp.starttls:
                    server.starttls()
                if smtp.user:
                    server.login(smtp.user, smtp.password)
                server.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise ToolError(f"Odeslání e-mailu selhalo: {exc}") from exc

        emails = [*lead.get("emails", []), {"to": to, "subject": subject, "body": body, "sent_at": now_iso()}]
        store.update(lead_id, emails=emails, status="contacted")
        return f"E-mail '{subject}' odeslán na {to}; lead #{lead_id} je ve stavu contacted."

    return [
        Tool(
            name="send_email",
            description=(
                f"Odešle obchodní e-mail z adresy {smtp.sender} kontaktu z databáze leadů a zapíše "
                "ho do historie leadu. Každé odeslání schvaluje uživatel, který uvidí celý text. "
                "Odmítne kontakty ve stavu do_not_contact, již oslovené leady (bez follow_up) "
                f"a vše nad denní limit {config.daily_email_limit} e-mailů."
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
            needs_approval=True,
        )
    ]
