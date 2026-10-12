"""Sending outreach e-mails over SMTP. Only registered when sending is enabled (AIAGENT_SEND_MODE)."""

from __future__ import annotations

import random
import smtplib
import ssl
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid
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
    try:
        if smtp.security == "starttls":
            server.starttls(context=context)
        if smtp.user:
            server.login(smtp.user, smtp.password)
    except BaseException:
        server.close()
        raise
    return server


class DeliveryError(ToolError):
    """Sending failed; `maybe_sent` is True when the server may have accepted the message anyway."""

    def __init__(self, message: str, maybe_sent: bool):
        super().__init__(message)
        self.maybe_sent = maybe_sent


# The server explicitly refused the message: it was certainly not sent.
REJECTED = (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError)


def deliver(smtp: SmtpConfig, msg: EmailMessage) -> None:
    stage = "connect"
    try:
        with open_smtp(smtp) as server:
            stage = "send"
            server.send_message(msg)
            stage = "done"
    except (smtplib.SMTPException, OSError) as exc:
        if stage == "done":
            return  # only closing the connection failed; the message was already accepted
        maybe_sent = stage == "send" and not isinstance(exc, REJECTED)
        raise DeliveryError(f"Odeslání e-mailu selhalo: {exc}", maybe_sent) from exc


def build_message(smtp: SmtpConfig, to: str, subject: str, body: str, unsubscribe: bool = False) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = smtp.sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=smtp.sender.rsplit("@", 1)[-1] or None)
    if unsubscribe:
        # Mail clients show an "unsubscribe" button; the request arrives as a reply to the sender.
        msg["List-Unsubscribe"] = f"<mailto:{smtp.sender}?subject=Odhlaseni>"
    msg.set_content(body)
    return msg


def now_in(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz))


def within_hours(moment: datetime, hours: tuple[int, int] | None) -> bool:
    """Weekdays between the start hour (inclusive) and the end hour (exclusive); None = any time."""
    if hours is None:
        return True
    return moment.weekday() < 5 and hours[0] <= moment.hour < hours[1]


def describe_hours(hours: tuple[int, int]) -> str:
    return f"po–pá {hours[0]}–{hours[1]} h"


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
    last_send: dict[str, float] = {}

    def pause_between_emails() -> None:
        """Space out e-mails sent by one run; a burst looks like bulk mail to spam filters."""
        if "at" not in last_send or config.send_pause[1] <= 0:
            return
        remaining = random.uniform(*config.send_pause) - (time.monotonic() - last_send["at"])
        if remaining > 0:
            time.sleep(remaining)

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
        if lead.get("sending_started"):
            raise ToolError(
                f"Odesílání leadu {lead_id} bylo zahájeno {lead['sending_started']}, ale nebylo potvrzeno, "
                "takže e-mail mohl odejít. Neodesílej ho. Uživatel musí zkontrolovat složku Odeslané a pak "
                "v Airtable vymazat pole Odesílání zahájeno (případně nastavit stav Osloven)."
            )
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
        now = now_in(config.timezone)
        if not within_hours(now, config.send_hours):
            assert config.send_hours is not None
            raise ToolError(
                f"Teď je mimo povolenou dobu odesílání ({describe_hours(config.send_hours)}, AIAGENT_SEND_HOURS). "
                "E-mail neodeslán, návrh zůstává uložený; odešli ho až v povolené době."
            )
        if max(repo.sent_count(now.date().isoformat()), sent_count["n"]) >= config.daily_email_limit:
            raise ToolError(f"Dosažen denní limit {config.daily_email_limit} odeslaných e-mailů.")
        pause_between_emails()

        # Mark the lead before sending: if the outcome can't be recorded afterwards (Airtable down,
        # crash), the mark keeps later runs from sending the same e-mail again.
        try:
            repo.update(lead_id, {"sending_started": now_iso()})
        except Exception as exc:
            raise ToolError(f"E-mail neodeslán: nepodařilo se zapsat zahájení odesílání do Airtable ({exc}).") \
                from exc
        try:
            deliver(smtp, build_message(smtp, address, subject, body, unsubscribe=True))
        except DeliveryError as exc:
            if exc.maybe_sent:
                sent_this_session.add(lead_id)
                last_send["at"] = time.monotonic()
                raise ToolError(
                    f"{exc} Není jisté, že e-mail neodešel, proto lead zůstává zablokovaný (pole Odesílání "
                    "zahájeno). Neodesílej znovu; upozorni uživatele, ať zkontroluje složku Odeslané."
                ) from exc
            try:
                repo.update(lead_id, {"sending_started": None})
            except Exception:
                pass  # the mark stays and blocks the lead: the safe side
            raise
        sent_this_session.add(lead_id)
        sent_count["n"] += 1
        last_send["at"] = time.monotonic()

        sent_at = now_iso()
        try:
            # The e-mail record first: the lead's mark is cleared only once everything is stored.
            repo.add_email(lead_id, address, subject, body, sent_at)
            repo.update(lead_id, {"status": "contacted", "last_contacted": sent_at, "draft": None,
                                  "sending_started": None})
        except Exception as exc:
            return (
                f"E-mail BYL odeslán na {address}, ale zápis do Airtable selhal ({exc}). Neodesílej ho "
                "znovu (lead zůstává zablokovaný polem Odesílání zahájeno); upozorni uživatele, ať odeslání "
                "zapíše ručně (stav Osloven, tabulka E-maily) a pole Odesílání zahájeno vymaže."
            )
        return f"E-mail '{subject}' odeslán na {address}; lead {lead_id} je ve stavu contacted."

    if auto:
        approval_note = (
            "Běží automaticky bez dotazu: odešle jen návrh, který uživatel schválil v Airtable, beze změn."
        )
    else:
        approval_note = "Každé odeslání schvaluje uživatel, který uvidí celý text."
    if config.send_hours is not None:
        approval_note += f" Odesílá jen {describe_hours(config.send_hours)} ({config.timezone})."

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
