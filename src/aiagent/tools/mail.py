"""Sending outreach e-mails over SMTP. Only registered when sending is enabled (AIAGENT_SEND_MODE)."""

from __future__ import annotations

import base64
import imaplib
import random
import re
import smtplib
import ssl
import time
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid, parseaddr
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Config, ImapConfig, SmtpConfig
from ..repos import describe_blocked, find_blocked, now_iso
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


# --- copy to the Sent folder (IMAP) ----------------------------------------------------------

# Fallbacks for servers that don't flag the Sent folder (names in IMAP's modified UTF-7).
SENT_FOLDER_NAMES = ("Sent", "Sent Items", "Sent Messages", "INBOX.Sent", "Odeslan&AOk-", "INBOX.Odeslan&AOk-")
_LIST_LINE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$')


def decode_folder(name: str) -> str:
    """IMAP folder names use modified UTF-7: 'Odeslan&AOk-' is 'Odeslané'."""
    def chunk(match: re.Match[str]) -> str:
        data = match.group(1)
        if not data:
            return "&"
        data = data.replace(",", "/")
        return base64.b64decode(data + "=" * (-len(data) % 4)).decode("utf-16-be")
    try:
        return re.sub(r"&([^-]*)-", chunk, name)
    except ValueError:
        return name


def _imap_quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def list_folders(server: imaplib.IMAP4) -> list[tuple[str, str]]:
    """(raw name, flags) of every folder on the server."""
    status, data = server.list()
    if status != "OK":
        raise ToolError(f"IMAP server nevrátil seznam složek: {data}")
    folders = []
    for item in data:
        if not isinstance(item, bytes):
            continue  # names sent as literals are rare and never the Sent folder
        match = _LIST_LINE.match(item.decode("utf-8", errors="replace"))
        if match:
            name = match.group("name").strip()
            if name.startswith('"') and name.endswith('"'):
                name = re.sub(r"\\(.)", r"\1", name[1:-1])
            folders.append((name, match.group("flags")))
    return folders


def pick_sent_folder(folders: list[tuple[str, str]], configured: str = "") -> str:
    if configured:
        for name, _ in folders:
            if configured.lower() in (name.lower(), decode_folder(name).lower()):
                return name
        available = ", ".join(decode_folder(name) for name, _ in folders)
        raise ToolError(f"Složka '{configured}' (IMAP_SENT_FOLDER) na serveru není. Dostupné složky: {available}.")
    for name, flags in folders:
        if "\\sent" in flags.lower():
            return name
    by_name = {name.lower(): name for name, _ in folders}
    for candidate in SENT_FOLDER_NAMES:
        if candidate.lower() in by_name:
            return by_name[candidate.lower()]
    raise ToolError("Složku Odeslané se nepodařilo najít. Nastavte IMAP_SENT_FOLDER (název složky ve schránce).")


def _imap_session(imap: ImapConfig) -> imaplib.IMAP4_SSL:
    return imaplib.IMAP4_SSL(imap.host, imap.port, ssl_context=ssl.create_default_context(), timeout=30)


def _imap_error(exc: Exception) -> str:
    if isinstance(exc, UnicodeError):
        # imaplib sends the login as ASCII.
        return "přihlašovací jméno nebo heslo obsahuje znaky mimo ASCII (diakritiku), IMAP je neumí přenést."
    return str(exc)


def save_to_sent(imap: ImapConfig, msg: EmailMessage) -> str:
    """Store a copy of a sent e-mail in the Sent folder; returns the folder name."""
    try:
        with _imap_session(imap) as server:
            server.login(imap.user, imap.password)
            folder = pick_sent_folder(list_folders(server), imap.folder)
            status, data = server.append(_imap_quote(folder), "(\\Seen)", imaplib.Time2Internaldate(time.time()),
                                         msg.as_bytes(policy=policy.SMTP))
            if status != "OK":
                raise ToolError(f"IMAP server kopii odmítl: {data}")
    except (imaplib.IMAP4.error, OSError, UnicodeError) as exc:
        raise ToolError(f"IMAP: {_imap_error(exc)}") from exc
    return decode_folder(folder)


def imap_check(imap: ImapConfig) -> str:
    try:
        with _imap_session(imap) as server:
            server.login(imap.user, imap.password)
            folder = pick_sent_folder(list_folders(server), imap.folder)
    except (imaplib.IMAP4.error, OSError, UnicodeError) as exc:
        raise ToolError(f"IMAP nefunguje: {_imap_error(exc)}") from exc
    return (f"Připojení k IMAP {imap.host}:{imap.port} je v pořádku, kopie odeslaných e-mailů se budou ukládat "
            f"do složky {decode_folder(folder)}.")


# --- sending -----------------------------------------------------------------------------------


def build_message(smtp: SmtpConfig, to: str, subject: str, body: str, unsubscribe: bool = False) -> EmailMessage:
    """Raises ValueError for header values email can't encode, e.g. a subject with a line break."""
    # SMTP_FROM may carry a display name ('Obchod <obchod@firma.cz>'); headers below need the bare address.
    address = parseaddr(smtp.sender)[1]
    msg = EmailMessage()
    msg["From"] = smtp.sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=address.rpartition("@")[2] or None)
    if unsubscribe:
        # Mail clients show an "unsubscribe" button; the request arrives as a reply to the sender.
        msg["List-Unsubscribe"] = f"<mailto:{address}?subject=Odhlaseni>"
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


def build(config: Config, repo: Any, blocklist: Any) -> list[Tool]:
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

    def check_sending_hours() -> None:
        if not within_hours(now_in(config.timezone), config.send_hours):
            assert config.send_hours is not None
            raise ToolError(
                f"Teď je mimo povolenou dobu odesílání ({describe_hours(config.send_hours)}, AIAGENT_SEND_HOURS). "
                "E-mail neodeslán, návrh zůstává uložený; odešli ho až v povolené době."
            )

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
        blocked = find_blocked(blocklist.entries(), address, lead.get("website"), lead.get("ico"))
        if blocked:
            raise ToolError(f"Kontakt je v tabulce Blokované kontakty ({describe_blocked(blocked)}). E-mail neodeslán.")
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
        check_sending_hours()
        today = now_in(config.timezone).date().isoformat()
        if max(repo.sent_count(today), sent_count["n"]) >= config.daily_email_limit:
            raise ToolError(f"Dosažen denní limit {config.daily_email_limit} odeslaných e-mailů.")
        try:
            message = build_message(smtp, address, subject, body, unsubscribe=True)
        except ValueError as exc:
            raise ToolError(f"E-mail nelze sestavit: {exc}. Předmět musí být na jednom řádku.") from exc
        pause_between_emails()
        check_sending_hours()  # the pause may have run past the end of the window

        # Mark the lead before sending: if the outcome can't be recorded afterwards (Airtable down,
        # crash), the mark keeps later runs from sending the same e-mail again.
        try:
            repo.update(lead_id, {"sending_started": now_iso()})
        except Exception as exc:
            raise ToolError(f"E-mail neodeslán: nepodařilo se zapsat zahájení odesílání do Airtable ({exc}).") \
                from exc
        try:
            deliver(smtp, message)
        except DeliveryError as exc:
            if exc.maybe_sent:
                sent_this_session.add(lead_id)
                sent_count["n"] += 1  # probably delivered: count it toward the daily limit
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
            result = f"E-mail '{subject}' odeslán na {address}; lead {lead_id} je ve stavu contacted."
        except Exception as exc:
            result = (
                f"E-mail BYL odeslán na {address}, ale zápis do Airtable selhal ({exc}). Neodesílej ho "
                "znovu (lead zůstává zablokovaný polem Odesílání zahájeno); upozorni uživatele, ať odeslání "
                "zapíše ručně (stav Osloven, tabulka E-maily) a pole Odesílání zahájeno vymaže."
            )

        # Only after the record: the copy is a convenience and must never keep the send from being stored.
        if config.imap is not None:
            try:
                save_to_sent(config.imap, message)
            except Exception as exc:
                result += f" Kopii do složky Odeslané se nepodařilo uložit ({exc})."
        return result

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
