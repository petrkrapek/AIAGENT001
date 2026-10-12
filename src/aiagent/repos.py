"""Data repositories. Production data lives in Airtable; the in-memory versions are for tests."""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
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
    "created_at": "Vytvořeno",
    "updated_at": "Aktualizováno",
}
LEAD_STATUS_CODES = {label: code for code, label in at.LEAD_STATUS_LABELS.items()}
TASK_STATUS_CODES = {label: code for code, label in at.TASK_STATUS_LABELS.items()}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def domain(url_or_email: str | None) -> str:
    if not url_or_email:
        return ""
    value = url_or_email.strip().lower()
    if "@" in value and "://" not in value:
        value = value.split("@", 1)[1]
    else:
        value = urlparse(value if "://" in value else f"http://{value}").netloc
    return value.removeprefix("www.")


def local_day(iso: str, tz: str) -> str:
    moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return moment.astimezone(ZoneInfo(tz)).date().isoformat()


def _matches(lead: dict[str, Any], query: str) -> bool:
    haystack = " ".join(
        str(lead.get(k) or "") for k in ("company", "city", "industry", "email", "website", "notes")
    )
    return query.lower() in haystack.lower()


# --- in-memory (tests) --------------------------------------------------------------


class MemoryLeads:
    def __init__(self, tz: str = "Europe/Prague"):
        self.tz = tz
        self.leads: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []
        self._ids = itertools.count(1)

    def create(self, lead: dict[str, Any]) -> dict[str, Any]:
        lead = {"service_id": None, **lead, "id": f"rec{next(self._ids):05d}", "domain": domain(lead.get("website")),
                "draft": None, "last_contacted": None, "created_at": now_iso(), "updated_at": now_iso()}
        self.leads[lead["id"]] = lead
        return dict(lead)

    def get(self, lead_id: str) -> dict[str, Any]:
        if lead_id not in self.leads:
            raise ToolError(f"Lead {lead_id} neexistuje.")
        return dict(self.leads[lead_id])

    def update(self, lead_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        lead = self.leads.get(lead_id)
        if lead is None:
            raise ToolError(f"Lead {lead_id} neexistuje.")
        lead.update(changes, updated_at=now_iso())
        if "website" in changes:
            lead["domain"] = domain(changes["website"])
        return dict(lead)

    def find_duplicate(self, ico: str | None, email: str | None, website: str | None):
        dom = domain(website)
        for lead in self.leads.values():
            if (ico and lead.get("ico") == ico) or (email and (lead.get("email") or "").lower() == email.lower()) \
                    or (dom and lead.get("domain") == dom):
                return dict(lead)
        return None

    def is_suppressed(self, email: str) -> bool:
        email = email.strip().lower()
        return any(
            lead["status"] == "do_not_contact"
            and ((lead.get("email") or "").lower() == email or (lead.get("domain") and lead["domain"] == domain(email)))
            for lead in self.leads.values()
        )

    def search(self, status: str | None = None, query: str | None = None) -> list[dict[str, Any]]:
        leads = list(self.leads.values())
        if status:
            leads = [lead for lead in leads if lead["status"] == status]
        if query:
            leads = [lead for lead in leads if _matches(lead, query)]
        return [dict(lead) for lead in leads]

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for lead in self.leads.values():
            counts[lead["status"]] = counts.get(lead["status"], 0) + 1
        return counts

    def add_email(self, lead_id: str, to: str, subject: str, body: str, sent_at: str) -> None:
        self.sent.append({"lead_id": lead_id, "to": to, "subject": subject, "body": body, "sent_at": sent_at})

    def emails(self, lead_id: str) -> list[dict[str, Any]]:
        return [e for e in self.sent if e["lead_id"] == lead_id]

    def sent_count(self, day: str) -> int:
        return sum(1 for e in self.sent if local_day(e["sent_at"], self.tz) == day)


class MemoryProfile:
    def __init__(self, fields: dict[str, Any] | None = None):
        self.fields = fields or {}

    def load(self) -> str | None:
        return format_profile(self.fields)


class MemoryServices:
    def __init__(self, services: list[dict[str, Any]] | None = None):
        self.services = services or []

    def list_active(self) -> list[dict[str, Any]]:
        return [dict(s) for s in self.services]


class MemoryTasks:
    def __init__(self):
        self.tasks: dict[str, dict[str, Any]] = {}
        self._ids = itertools.count(1)

    def create(self, title: str, due: str | None, notes: str | None) -> dict[str, Any]:
        task = {"id": f"rec{next(self._ids):05d}", "title": title, "status": "todo", "due": due, "notes": notes}
        self.tasks[task["id"]] = task
        return dict(task)

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return [dict(t) for t in self.tasks.values() if t["status"] == status]
        return [dict(t) for t in self.tasks.values() if t["status"] not in ("done", "cancelled")]

    def update(self, task_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        if task_id not in self.tasks:
            raise ToolError(f"Úkol {task_id} neexistuje.")
        self.tasks[task_id].update(changes)
        return dict(self.tasks[task_id])


class MemoryAudit:
    def __init__(self):
        self.records: list[dict[str, Any]] = []

    def log(self, tool: str, tool_input: dict[str, Any], approved: bool | None, is_error: bool) -> None:
        self.records.append({"tool": tool, "input": tool_input, "approved": approved, "error": is_error})


# --- Airtable -------------------------------------------------------------------------


def _lead_from_record(record: dict[str, Any]) -> dict[str, Any]:
    f = record.get("fields", {})
    lead = {key: f.get(name) for key, name in LEAD_FIELDS.items()}
    lead["id"] = record["id"]
    lead["company"] = lead["company"] or "(bez názvu)"
    lead["status"] = LEAD_STATUS_CODES.get(f.get("Stav"), "new")
    lead["service_id"] = (f.get("Služba") or [None])[0]
    lead["draft"] = (
        {"subject": f.get("Předmět návrhu", ""), "body": f.get("Text návrhu", ""), "updated_at": f.get("Návrh upraven")}
        if f.get("Text návrhu") else None
    )
    return lead


def _lead_to_fields(changes: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "status":
            fields["Stav"] = at.LEAD_STATUS_LABELS[value]
        elif key == "service_id":
            fields["Služba"] = [value] if value else []
        elif key == "draft":
            draft = value or {}
            fields["Předmět návrhu"] = draft.get("subject")
            fields["Text návrhu"] = draft.get("body")
            fields["Návrh upraven"] = draft.get("updated_at")
        elif key in LEAD_FIELDS:
            fields[LEAD_FIELDS[key]] = value if value != "" else None
        else:
            raise ValueError(f"Neznámé pole leadu: {key}")
    if "website" in changes:
        fields["Doména"] = domain(changes["website"]) or None
    return fields


PROFILE_FIELDS = (
    "Firma", "IČO", "Web", "O nás", "Čím se lišíme", "Ideální zákazník", "Koho neoslovovat",
    "Obecné reference", "Výzva k akci", "Tón e-mailů", "Podpis",
)


def format_profile(fields: dict[str, Any]) -> str | None:
    """Render the profile record for the system prompt; None if it is empty."""
    parts = []
    for name in PROFILE_FIELDS:
        value = str(fields.get(name) or "").strip()
        if value:
            parts.append(f"### {name}\n{value}")
    return "\n\n".join(parts) or None


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


class AirtableLeads:
    def __init__(self, client: at.AirtableClient, tz: str):
        self.client = client
        self.tz = tz

    def create(self, lead: dict[str, Any]) -> dict[str, Any]:
        fields = _lead_to_fields({k: v for k, v in lead.items() if v is not None})
        fields["Vytvořeno"] = fields["Aktualizováno"] = now_iso()
        return _lead_from_record(self.client.create_record(at.LEADS, fields))

    def get(self, lead_id: str) -> dict[str, Any]:
        if not lead_id.startswith("rec"):
            raise ToolError(f"Neplatné ID leadu '{lead_id}' (Airtable ID začíná 'rec').")
        try:
            return _lead_from_record(self.client.get_record(at.LEADS, lead_id))
        except at.AirtableError as exc:
            if "404" in str(exc):
                raise ToolError(f"Lead {lead_id} neexistuje.") from exc
            raise

    def update(self, lead_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        fields = _lead_to_fields(changes)
        fields["Aktualizováno"] = now_iso()
        return _lead_from_record(self.client.update_record(at.LEADS, lead_id, fields))

    def find_duplicate(self, ico: str | None, email: str | None, website: str | None):
        clauses = []
        if ico:
            clauses.append(f"{{IČO}} = {at.esc(ico)}")
        if email:
            clauses.append(f"LOWER({{E-mail}}) = {at.esc(email.lower())}")
        if domain(website):
            clauses.append(f"{{Doména}} = {at.esc(domain(website))}")
        if not clauses:
            return None
        records = self.client.list_records(at.LEADS, f"OR({', '.join(clauses)})", max_records=1)
        return _lead_from_record(records[0]) if records else None

    def is_suppressed(self, email: str) -> bool:
        email = email.strip().lower()
        formula = (
            f"AND({{Stav}} = {at.esc(at.LEAD_STATUS_LABELS['do_not_contact'])}, "
            f"OR(LOWER({{E-mail}}) = {at.esc(email)}, {{Doména}} = {at.esc(domain(email))}))"
        )
        return bool(self.client.list_records(at.LEADS, formula, fields=["Firma"], max_records=1))

    def search(self, status: str | None = None, query: str | None = None) -> list[dict[str, Any]]:
        clauses = []
        if status:
            clauses.append(f"{{Stav}} = {at.esc(at.LEAD_STATUS_LABELS[status])}")
        if query:
            haystack = " & ' ' & ".join(
                f"{{{LEAD_FIELDS[k]}}}" for k in ("company", "city", "industry", "email", "website", "notes")
            )
            clauses.append(f"SEARCH({at.esc(query.lower())}, LOWER({haystack}))")
        formula = f"AND({', '.join(clauses)})" if clauses else None
        records = self.client.list_records(at.LEADS, formula, sort=[("Vytvořeno", "asc")])
        return [_lead_from_record(r) for r in records]

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.client.list_records(at.LEADS, fields=["Stav"]):
            code = LEAD_STATUS_CODES.get(record.get("fields", {}).get("Stav"), "new")
            counts[code] = counts.get(code, 0) + 1
        return counts

    def add_email(self, lead_id: str, to: str, subject: str, body: str, sent_at: str) -> None:
        self.client.create_record(at.EMAILS, {
            "Předmět": subject, "Lead": [lead_id], "Lead ID": lead_id,
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
            if "404" in str(exc):
                raise ToolError(f"Úkol {task_id} neexistuje.") from exc
            raise


class AirtableAudit:
    def __init__(self, client: at.AirtableClient):
        self.client = client

    def log(self, tool: str, tool_input: dict[str, Any], approved: bool | None, is_error: bool) -> None:
        self.client.create_record(at.AUDIT, {
            "Nástroj": tool,
            "Čas": now_iso(),
            "Vstup": json.dumps(tool_input, ensure_ascii=False, indent=2)[:90_000],
            "Schváleno": {True: "ano", False: "ne", None: "nevyžadováno"}[approved],
            "Výsledek": "chyba" if is_error else "ok",
        })


@dataclass
class Repos:
    leads: Any
    tasks: Any
    audit: Any
    services: Any
    profile: Any


def airtable_repos(client: at.AirtableClient, tz: str) -> Repos:
    return Repos(AirtableLeads(client, tz), AirtableTasks(client), AirtableAudit(client),
                 AirtableServices(client), AirtableProfile(client))


def memory_repos(tz: str = "Europe/Prague") -> Repos:
    return Repos(MemoryLeads(tz), MemoryTasks(), MemoryAudit(), MemoryServices(), MemoryProfile())
