"""Airtable-backed repositories for everything the agent reads and stores.

Tests use in-memory doubles with the same interface (tests/memory_repos.py).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from . import airtable as at
from .tools import ToolError

LEAD_FIELDS = {
    "company": "Firma",
    "ico": "IČO",
    "website": "Web",
    "domain": "Doména",
    "email": "E-mail",
    "email_source": "Zdroj e-mailu",
    "contact_name": "Kontaktní osoba",
    "contact_role": "Role",
    "city": "Město",
    "industry": "Obor",
    "fit_reason": "Proč se hodí",
    "notes": "Poznámky",
    "last_contacted": "Naposledy osloven",
    "sending_started": "Odesílání zahájeno",
    "created_at": "Vytvořeno",
    "updated_at": "Aktualizováno",
}
LEAD_STATUS_CODES = {label: code for code, label in at.LEAD_STATUS_LABELS.items()}
TASK_STATUS_CODES = {label: code for code, label in at.TASK_STATUS_LABELS.items()}
SEARCH_FIELDS = ("company", "city", "industry", "email", "website", "notes")

# Shared mailbox providers: their domain says nothing about the company.
FREEMAIL = {
    "gmail.com", "googlemail.com", "seznam.cz", "email.cz", "post.cz", "centrum.cz", "atlas.cz",
    "volny.cz", "tiscali.cz", "quick.cz", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
    "icloud.com", "me.com", "gmx.com", "gmx.net", "proton.me", "protonmail.com",
}

# Sites where companies only have a profile page (facebook.com/alfa, firmy.cz/detail/…), and their
# subdomains: the host says nothing about which company it is.
PROFILE_SITES = {
    "facebook.com", "fb.com", "instagram.com", "linkedin.com", "x.com", "twitter.com", "youtube.com",
    "youtu.be", "tiktok.com", "pinterest.com", "google.com", "goo.gl", "linktr.ee", "wa.me", "t.me",
    "firmy.cz", "mapy.cz", "mapy.com", "zivefirmy.cz", "najisto.centrum.cz", "kurzy.cz", "justice.cz",
    "ares.gov.cz", "heureka.cz", "zbozi.cz",
}

PROFILE_FIELDS = (
    "Firma", "IČO", "Web", "O nás", "Čím se lišíme", "Ideální zákazník", "Koho neoslovovat",
    "Obecné reference", "Výzva k akci", "Tón e-mailů", "Podpis",
)
SERVICE_FIELDS = {
    "name": "Název",
    "description": "Popis",
    "target": "Pro koho",
    "benefits": "Přínos pro zákazníka",
    "price": "Cena",
    "references": "Reference",
    "url": "Odkaz",
    "instructions": "Pokyny pro agenta",
}


# --- helpers -------------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def domain(url_or_email: str | None) -> str:
    """Host part of a URL or e-mail address, lower-case, without www. and port."""
    if not url_or_email:
        return ""
    value = url_or_email.strip().lower()
    if "@" in value and "://" not in value:
        host = value.rsplit("@", 1)[1]
    else:
        host = urlparse(value if "://" in value else f"http://{value}").hostname or ""
    return host.strip(" >.").removeprefix("www.")


def site_domain(website: str | None) -> str:
    """Domain of the company's own website; empty for a profile on a shared site (facebook.com/alfa)."""
    host = domain(website)
    return "" if any(host == site or host.endswith("." + site) for site in PROFILE_SITES) else host


def mail_domain(email: str | None) -> str:
    """Domain of a company mailbox; empty for free-mail providers (seznam.cz, gmail.com…)."""
    host = domain(email)
    return "" if host in FREEMAIL else host


def company_domain(website: str | None, email: str | None) -> str:
    """Domain identifying the company: from its own website, else from a company (non-freemail) address."""
    return site_domain(website) or mail_domain(email)


def normalize_ico(ico: str | None) -> str | None:
    if not ico:
        return None
    digits = re.sub(r"\D", "", ico)
    return digits.zfill(8) if digits else None


def local_day(iso: str, tz: str) -> str:
    moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return moment.astimezone(ZoneInfo(tz)).date().isoformat()


def format_profile(fields: dict[str, Any]) -> str | None:
    """Render the profile record for the system prompt; None if it is empty."""
    parts = []
    for name in PROFILE_FIELDS:
        value = str(fields.get(name) or "").strip()
        if value:
            parts.append(f"### {name}\n{value}")
    return "\n\n".join(parts) or None


# --- leads ---------------------------------------------------------------------------------


def _lead_from_record(record: dict[str, Any]) -> dict[str, Any]:
    f = record.get("fields", {})
    lead = {key: f.get(name) for key, name in LEAD_FIELDS.items()}
    lead["id"] = record["id"]
    lead["company"] = lead["company"] or "(bez názvu)"
    # A status label the agent doesn't know (renamed or custom in Airtable) fails closed.
    stav = f.get("Stav")
    lead["status"] = LEAD_STATUS_CODES.get(stav, "do_not_contact") if stav else "new"
    lead["service_id"] = (f.get("Služba") or [None])[0]
    lead["draft"] = (
        {
            "subject": f.get("Předmět návrhu", ""),
            "body": f.get("Text návrhu", ""),
            "updated_at": f.get("Návrh upraven"),
            # Only a person can set this, in Airtable; no tool writes it.
            "approved": f.get("Schválení návrhu") == at.DRAFT_APPROVED,
        }
        if f.get("Text návrhu") else None
    )
    return lead


def _lead_to_fields(changes: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "status":
            fields["Stav"] = at.LEAD_STATUS_LABELS[value]
        elif key == "service_id":
            fields["Služba"] = [at.check_record_id(value)] if value else []
        elif key == "draft":
            draft = value or {}
            fields["Předmět návrhu"] = draft.get("subject")
            fields["Text návrhu"] = draft.get("body")
            fields["Návrh upraven"] = draft.get("updated_at")
            # A new or changed draft always needs a fresh human approval.
            fields["Schválení návrhu"] = at.DRAFT_PENDING if value else None
        elif key == "ico":
            fields["IČO"] = normalize_ico(value)
        elif key in LEAD_FIELDS:
            fields[LEAD_FIELDS[key]] = value if value != "" else None
        else:
            raise ValueError(f"Neznámé pole leadu: {key}")
    return fields


def _not_found(exc: at.AirtableError, what: str) -> Exception:
    return ToolError(f"{what} neexistuje.") if exc.status in (403, 404) else exc


class AirtableLeads:
    def __init__(self, client: at.AirtableClient, tz: str):
        self.client = client
        self.tz = tz

    def create(self, lead: dict[str, Any]) -> dict[str, Any]:
        fields = _lead_to_fields({k: v for k, v in lead.items() if v is not None})
        fields["Doména"] = company_domain(lead.get("website"), lead.get("email")) or None
        fields["Vytvořeno"] = fields["Aktualizováno"] = now_iso()
        return _lead_from_record(self.client.create_record(at.LEADS, fields))

    def get(self, lead_id: str) -> dict[str, Any]:
        try:
            return _lead_from_record(self.client.get_record(at.LEADS, lead_id))
        except at.AirtableError as exc:
            raise _not_found(exc, f"Lead {lead_id}") from exc

    def update(self, lead_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        fields = _lead_to_fields(changes)
        if "website" in changes or "email" in changes:
            current = self.get(lead_id)
            merged = {**current, **changes}
            fields["Doména"] = company_domain(merged.get("website"), merged.get("email")) or None
        fields["Aktualizováno"] = now_iso()
        try:
            return _lead_from_record(self.client.update_record(at.LEADS, lead_id, fields))
        except at.AirtableError as exc:
            raise _not_found(exc, f"Lead {lead_id}") from exc

    def find_duplicate(self, ico: str | None, email: str | None, website: str | None) -> dict[str, Any] | None:
        clauses = []
        normalized_ico = normalize_ico(ico)
        if normalized_ico:
            clauses.append(f"{{IČO}} = {at.esc(normalized_ico)}")
        if email:
            clauses.append(f"LOWER({{E-mail}}) = {at.esc(email.strip().lower())}")
        host = company_domain(website, email)
        if host:
            clauses.append(f"{{Doména}} = {at.esc(host)}")
        if not clauses:
            return None
        records = self.client.list_records(at.LEADS, f"OR({', '.join(clauses)})", max_records=1)
        return _lead_from_record(records[0]) if records else None

    def is_suppressed(self, email: str) -> bool:
        """True if the address, or its company domain, belongs to a do_not_contact lead."""
        email = email.strip().lower()
        clauses = [f"LOWER({{E-mail}}) = {at.esc(email)}"]
        host = company_domain(None, email)
        if host:
            clauses.append(f"{{Doména}} = {at.esc(host)}")
        formula = (
            f"AND({{Stav}} = {at.esc(at.LEAD_STATUS_LABELS['do_not_contact'])}, OR({', '.join(clauses)}))"
        )
        return bool(self.client.list_records(at.LEADS, formula, fields=["Firma"], max_records=1))

    def search(self, status: str | None = None, query: str | None = None,
               limit: int | None = None) -> list[dict[str, Any]]:
        """Matching leads, newest first; at most `limit` of them."""
        clauses = []
        if status:
            clauses.append(f"{{Stav}} = {at.esc(at.LEAD_STATUS_LABELS[status])}")
        if query:
            haystack = " & ' ' & ".join(f"{{{LEAD_FIELDS[k]}}}" for k in SEARCH_FIELDS)
            clauses.append(f"SEARCH({at.esc(query.lower())}, LOWER({haystack}))")
        formula = f"AND({', '.join(clauses)})" if clauses else None
        records = self.client.list_records(at.LEADS, formula, sort=[("Vytvořeno", "desc")], max_records=limit)
        return [_lead_from_record(r) for r in records]

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.client.list_records(at.LEADS, fields=["Stav"]):
            status = _lead_from_record(record)["status"]
            counts[status] = counts.get(status, 0) + 1
        return counts

    def add_email(self, lead_id: str, to: str, subject: str, body: str, sent_at: str) -> None:
        self.client.create_record(at.EMAILS, {
            "Předmět": subject, "Lead": [at.check_record_id(lead_id)], "Lead ID": lead_id,
            "Komu": to, "Text": body, "Odesláno": sent_at,
        })

    def emails(self, lead_id: str) -> list[dict[str, Any]]:
        records = self.client.list_records(at.EMAILS, f"{{Lead ID}} = {at.esc(lead_id)}",
                                           sort=[("Odesláno", "asc")])
        return [
            {"to": r["fields"].get("Komu"), "subject": r["fields"].get("Předmět"),
             "body": r["fields"].get("Text"), "sent_at": r["fields"].get("Odesláno")}
            for r in records
        ]

    def sent_count(self, day: str) -> int:
        formula = (
            f"DATETIME_FORMAT(SET_TIMEZONE({{Odesláno}}, {at.esc(self.tz)}), 'YYYY-MM-DD') = {at.esc(day)}"
        )
        return len(self.client.list_records(at.EMAILS, formula, fields=["Předmět"]))


# --- tasks, audit, profile, services --------------------------------------------------------


class AirtableTasks:
    def __init__(self, client: at.AirtableClient):
        self.client = client

    @staticmethod
    def _from_record(record: dict[str, Any]) -> dict[str, Any]:
        f = record.get("fields", {})
        return {"id": record["id"], "title": f.get("Název", ""), "status": TASK_STATUS_CODES.get(f.get("Stav"), "todo"),
                "due": f.get("Termín"), "notes": f.get("Poznámka")}

    def create(self, title: str, due: str | None, notes: str | None) -> dict[str, Any]:
        fields = {"Název": title, "Stav": at.TASK_STATUS_LABELS["todo"], "Vytvořeno": now_iso()}
        if due:
            fields["Termín"] = due
        if notes:
            fields["Poznámka"] = notes
        return self._from_record(self.client.create_record(at.TASKS, fields))

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            formula = f"{{Stav}} = {at.esc(at.TASK_STATUS_LABELS[status])}"
        else:
            done = [at.esc(at.TASK_STATUS_LABELS[s]) for s in ("done", "cancelled")]
            formula = f"AND({{Stav}} != {done[0]}, {{Stav}} != {done[1]})"
        records = self.client.list_records(at.TASKS, formula, sort=[("Termín", "asc")])
        return [self._from_record(r) for r in records]

    def update(self, task_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        names = {"title": "Název", "due": "Termín", "notes": "Poznámka"}
        fields = {names[k]: v for k, v in changes.items() if k in names}
        if "status" in changes:
            fields["Stav"] = at.TASK_STATUS_LABELS[changes["status"]]
        try:
            return self._from_record(self.client.update_record(at.TASKS, task_id, fields))
        except at.AirtableError as exc:
            raise _not_found(exc, f"Úkol {task_id}") from exc


SECRET_KEYS = re.compile(r"authorization|cookie|token|secret|password|passwd|api[-_]?key", re.I)


def redact(value: Any) -> Any:
    """Mask values of secret-looking keys (e.g. webhook Authorization headers)."""
    if isinstance(value, dict):
        return {k: "***" if SECRET_KEYS.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class AirtableAudit:
    def __init__(self, client: at.AirtableClient):
        self.client = client

    def log(self, tool: str, tool_input: dict[str, Any], approved: bool | None, is_error: bool) -> None:
        self.client.create_record(at.AUDIT, {
            "Nástroj": tool,
            "Čas": now_iso(),
            "Vstup": json.dumps(redact(tool_input), ensure_ascii=False, indent=2)[:90_000],
            "Schváleno": {True: "ano", False: "ne", None: "nevyžadováno"}[approved],
            "Výsledek": "chyba" if is_error else "ok",
        })


class AirtableProfile:
    def __init__(self, client: at.AirtableClient):
        self.client = client

    def load(self) -> str | None:
        """The company profile is the first non-empty row of the Profil firmy table."""
        for record in self.client.list_records(at.PROFILE, max_records=10):
            text = format_profile(record.get("fields", {}))
            if text:
                return text
        return None


class AirtableServices:
    def __init__(self, client: at.AirtableClient):
        self.client = client

    def list_active(self) -> list[dict[str, Any]]:
        records = self.client.list_records(
            at.SERVICES, f"{{Stav}} = {at.esc(at.SERVICE_ACTIVE)}", sort=[("Název", "asc")]
        )
        services = []
        for record in records:
            f = record.get("fields", {})
            if not f.get("Název"):
                continue
            services.append({"id": record["id"], **{k: f.get(name) for k, name in SERVICE_FIELDS.items()}})
        return services


# --- blocked contacts ----------------------------------------------------------------------


def blocked_entry(name: str | None, email: str | None, domain_or_url: str | None, ico: str | None,
                  reason: str | None = None) -> dict[str, Any]:
    """A blocklist row normalized for matching (people type domains as URLs, IČO with spaces…)."""
    return {"name": (name or "").strip(), "email": (email or "").strip().lower(),
            "domain": domain(domain_or_url), "ico": normalize_ico(ico), "reason": (reason or "").strip()}


def find_blocked(entries: list[dict[str, Any]], email: str | None = None, website: str | None = None,
                 ico: str | None = None) -> dict[str, Any] | None:
    """The blocklist entry matching the e-mail, the company's domain (or a subdomain) or the IČO."""
    email = (email or "").strip().lower()
    # Only domains that identify one company: a row for seznam.cz or facebook.com must not block
    # everyone with a mailbox or a profile there.
    hosts = {host for host in (mail_domain(email), site_domain(website)) if host}
    ico = normalize_ico(ico)
    for entry in entries:
        if (email and entry["email"] == email) or (ico and entry["ico"] == ico):
            return entry
        if entry["domain"] and any(h == entry["domain"] or h.endswith("." + entry["domain"]) for h in hosts):
            return entry
    return None


def describe_blocked(entry: dict[str, Any]) -> str:
    label = entry["name"] or entry["email"] or entry["domain"] or entry["ico"] or "záznam"
    return f"{label}: {entry['reason']}" if entry["reason"] else label


def block_lead(blocklist: Blocklist, lead: dict[str, Any], entries: list[dict[str, Any]] | None = None) -> bool:
    """Copy an opted-out lead to the blocklist unless it's already covered; True if a row was added."""
    entries = blocklist.entries() if entries is None else entries
    if find_blocked(entries, lead.get("email"), lead.get("website"), lead.get("ico")):
        return False
    entry = blocked_entry(lead.get("company"), lead.get("email"),
                          company_domain(lead.get("website"), lead.get("email")), lead.get("ico"),
                          f"Stav Nekontaktovat (lead {lead['id']})")
    if not (entry["email"] or entry["domain"] or entry["ico"]):
        return False  # nothing a row could ever match; it would only be added again on every start
    blocklist.add(entry)
    entries.append(entry)
    return True


def sync_blocklist(leads: LeadRepo, blocklist: Blocklist) -> int:
    """Copy do_not_contact leads (also ones set by hand in Airtable) to the blocklist; returns the count."""
    entries = blocklist.entries()
    return sum(block_lead(blocklist, lead, entries) for lead in leads.search(status="do_not_contact"))


class AirtableBlocklist:
    # Every saved lead and draft is checked against the whole table: keep it for a short while
    # instead of downloading it each time. Rows a person adds in Airtable count within a minute.
    CACHE_SECONDS = 60

    def __init__(self, client: at.AirtableClient):
        self.client = client
        self._cache: tuple[float, list[dict[str, Any]]] | None = None

    def entries(self, fresh: bool = False) -> list[dict[str, Any]]:
        """All rows, normalized; `fresh` skips the cache (before sending an e-mail)."""
        if not fresh and self._cache and time.monotonic() - self._cache[0] < self.CACHE_SECONDS:
            return list(self._cache[1])
        records = self.client.list_records(at.BLOCKED, fields=["Název", "E-mail", "Doména", "IČO", "Důvod"])
        entries = [
            blocked_entry(f.get("Název"), f.get("E-mail"), f.get("Doména"), f.get("IČO"), f.get("Důvod"))
            for f in (r.get("fields", {}) for r in records)
        ]
        self._cache = (time.monotonic(), entries)
        return list(entries)

    def add(self, entry: dict[str, Any]) -> None:
        self.client.create_record(at.BLOCKED, {
            "Název": entry["name"] or None, "E-mail": entry["email"] or None, "Doména": entry["domain"] or None,
            "IČO": entry["ico"], "Důvod": entry["reason"] or None, "Přidáno": now_iso(),
        })
        if self._cache:
            self._cache[1].append(entry)


# --- interfaces ----------------------------------------------------------------------------
# What the tools and the agent need from storage. The Airtable classes above implement them, and
# so do the in-memory doubles in tests/memory_repos.py (tests/test_repos_protocols.py checks it).


@runtime_checkable
class LeadRepo(Protocol):
    def create(self, lead: dict[str, Any]) -> dict[str, Any]: ...
    def get(self, lead_id: str) -> dict[str, Any]: ...
    def update(self, lead_id: str, changes: dict[str, Any]) -> dict[str, Any]: ...
    def find_duplicate(self, ico: str | None, email: str | None, website: str | None) -> dict[str, Any] | None: ...
    def is_suppressed(self, email: str) -> bool: ...
    def search(self, status: str | None = None, query: str | None = None,
               limit: int | None = None) -> list[dict[str, Any]]: ...
    def status_counts(self) -> dict[str, int]: ...
    def add_email(self, lead_id: str, to: str, subject: str, body: str, sent_at: str) -> None: ...
    def emails(self, lead_id: str) -> list[dict[str, Any]]: ...
    def sent_count(self, day: str) -> int: ...


@runtime_checkable
class TaskRepo(Protocol):
    def create(self, title: str, due: str | None, notes: str | None) -> dict[str, Any]: ...
    def list(self, status: str | None = None) -> list[dict[str, Any]]: ...
    def update(self, task_id: str, changes: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class AuditLog(Protocol):
    def log(self, tool: str, tool_input: dict[str, Any], approved: bool | None, is_error: bool) -> None: ...


@runtime_checkable
class ServiceRepo(Protocol):
    def list_active(self) -> list[dict[str, Any]]: ...


@runtime_checkable
class ProfileRepo(Protocol):
    def load(self) -> str | None: ...


@runtime_checkable
class Blocklist(Protocol):
    def entries(self, fresh: bool = False) -> list[dict[str, Any]]: ...
    def add(self, entry: dict[str, Any]) -> None: ...


@dataclass
class Repos:
    leads: LeadRepo
    tasks: TaskRepo
    audit: AuditLog
    services: ServiceRepo
    profile: ProfileRepo
    blocklist: Blocklist


def airtable_repos(client: at.AirtableClient, tz: str) -> Repos:
    return Repos(AirtableLeads(client, tz), AirtableTasks(client), AirtableAudit(client),
                 AirtableServices(client), AirtableProfile(client), AirtableBlocklist(client))
