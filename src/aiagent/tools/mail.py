"""Sending outreach e-mails over SMTP. Only registered when sending is enabled (AIAGENT_SEND_MODE)."""

from __future__ import annotations

import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Config, SmtpConfig
from ..repos import now_iso
from . import Tool, ToolError, schema


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


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def single_address(value: str) -> str:
    """Exactly one plain address; anything else could smuggle in extra recipients."""
    pairs = getaddresses([value])
    if len(pairs) != 1 or not pairs[0][1] or "@" not in pairs[0][1] or "," in value or ";" in value:
        raise ToolError("Pole 'to' musí obsahovat právě jednu e-mailovou adresu.")
    return pairs[0][1].strip().lower()


def build(config: Config, repo: Any) -> list[Tool]:
    smtp = config.smtp
    assert smtp is not None
    auto = config.send_mode == "auto"
    # Leads e-mailed in this process: a guard if recording a sent e-mail to Airtable failed.
    sent_this_session: set[str] = set()
    sent_count = {"n": 0}

    def send_email(lead_id: str, to: str, subject: str, body: str, follow_up: bool = False) -> str:
        lead = repo.get(lead_id)
        address = single_address(to)
        if not lead.get("email") or address != lead["email"].strip().lower():
            raise ToolError(
                f"E-mail lze poslat jen na adresu uloženou u leadu ({lead.get('email') or 'žádná'}). "
                "Jinou adresu nejdřív ulož přes update_lead i se zdrojem."
            )
        if lead["status"] in ("do_not_contact", "not_interested") or repo.is_suppressed(address):
            raise ToolError("Tento kontakt si nepřeje být oslovován. E-mail neodeslán.")
        if lead_id in sent_this_session:
            raise ToolError(f"Leadu {lead_id} už byl v tomto běhu e-mail odeslán. Neodesílej znovu.")
        if lead.get("last_contacted") and not follow_up:
            raise ToolError(
                f"Lead {lead_id} už byl osloven {lead['last_contacted']}. "
                "Další zprávu pošli jen jako follow_up=true, pokud o to uživatel výslovně požádá."
            )
        if auto:
            # Nobody approves in the terminal: send only a draft a person approved in Airtable,
            # unchanged, and never a follow-up.
            if follow_up:
                raise ToolError("Navazující zprávy (follow_up) se v automatickém režimu neodesílají.")
            draft = lead.get("draft")
            if not draft or not draft.get("approved"):
                raise ToolError(
                    "V automatickém režimu lze odeslat jen návrh, který uživatel schválil v Airtable "
                    "(pole Schválení návrhu = Schváleno)."
                )
            if (_normalize(draft["subject"]), _normalize(draft["body"])) != (_normalize(subject), _normalize(body)):
                raise ToolError("Text se liší od schváleného návrhu. Odeslat lze jen schválený text beze změn.")
        today = datetime.now(ZoneInfo(config.timezone)).date().isoformat()
        if max(repo.sent_count(today), sent_count["n"]) >= config.daily_email_limit:
            raise ToolError(f"Dosažen denní limit {config.daily_email_limit} odeslaných e-mailů.")

        deliver(smtp, build_message(smtp, address, subject, body))
        sent_this_session.add(lead_id)
        sent_count["n"] += 1

        sent_at = now_iso()
        try:
            repo.update(lead_id, {"status": "contacted", "last_contacted": sent_at, "draft": None})
            repo.add_email(lead_id, address, subject, body, sent_at)
        except Exception as exc:
            return (
                f"E-mail BYL odeslán na {address}, ale zápis do Airtable selhal ({exc}). "
                "Neodesílej ho znovu; upozorni uživatele, ať odeslání zapíše ručně."
            )
        return f"E-mail '{subject}' odeslán na {address}; lead {lead_id} je ve stavu contacted."

    if auto:
        approval_note = (
            "Běží automaticky bez dotazu: odešle jen návrh, který uživatel schválil v Airtable, beze změn."
        )
    else:
        approval_note = "Každé odeslání schvaluje uživatel, který uvidí celý text."

    return [
        Tool(
            name="send_email",
            description=(
                f"Odešle obchodní e-mail z adresy {smtp.sender} na e-mail uložený u leadu a zapíše ho "
                f"do tabulky E-maily v Airtable. {approval_note} Odmítne kontakty ve stavu do_not_contact "
                "nebo not_interested, již oslovené leady (bez follow_up) a vše nad denní limit "
                f"{config.daily_email_limit} e-mailů."
            ),
            input_schema=schema(
                {
                    "lead_id": {"type": "string", "description": "ID leadu v Airtable (rec…)."},
                    "to": {"type": "string", "description": "E-mail leadu (musí se shodovat s uloženým)."},
                    "subject": {"type": "string", "description": "Předmět e-mailu."},
                    "body": {"type": "string", "description": "Celý text e-mailu včetně podpisu."},
                    "follow_up": {
                        "type": "boolean",
                        "description": "Navazující zpráva už oslovenému (jen na výslovný pokyn uživatele).",
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
