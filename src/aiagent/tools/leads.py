"""Lead database (a minimal CRM) stored as JSON in the workspace."""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import Config
from ..storage import JsonStore, now_iso
from . import Tool, ToolError, schema, truncate

STATUSES = (
    "new",            # found, not yet checked
    "qualified",      # fits the target customer profile
    "drafted",        # e-mail draft ready for review
    "contacted",      # e-mail sent
    "replied",        # they answered
    "interested",
    "not_interested",
    "do_not_contact",  # never contact again (opt-out)
)

EDITABLE = (
    "company", "ico", "website", "email", "email_source", "contact_name", "contact_role",
    "city", "industry", "fit_reason", "notes", "status",
)


def domain(url_or_email: str | None) -> str:
    if not url_or_email:
        return ""
    value = url_or_email.strip().lower()
    if "@" in value and "://" not in value:
        value = value.split("@", 1)[1]
    else:
        value = urlparse(value if "://" in value else f"http://{value}").netloc
    return value.removeprefix("www.")


class LeadStore(JsonStore):
    def get(self, lead_id: int) -> dict[str, Any]:
        for lead in self.load():
            if lead["id"] == lead_id:
                return lead
        raise ToolError(f"Lead #{lead_id} neexistuje.")

    def update(self, lead_id: int, **changes: Any) -> dict[str, Any]:
        leads = self.load()
        for lead in leads:
            if lead["id"] == lead_id:
                lead.update(changes)
                lead["updated_at"] = now_iso()
                self.save(leads)
                return lead
        raise ToolError(f"Lead #{lead_id} neexistuje.")

    def find_duplicate(self, leads: list[dict[str, Any]], ico: str | None, email: str | None,
                       website: str | None) -> dict[str, Any] | None:
        for lead in leads:
            if ico and lead.get("ico") == ico:
                return lead
            if email and (lead.get("email") or "").lower() == email.lower():
                return lead
            if website and domain(website) and domain(lead.get("website")) == domain(website):
                return lead
        return None

    def is_suppressed(self, email: str) -> bool:
        """True if the address, or the company website's domain, is marked do_not_contact."""
        email = email.strip().lower()
        for lead in self.load():
            if lead.get("status") != "do_not_contact":
                continue
            if (lead.get("email") or "").lower() == email:
                return True
            if lead.get("website") and domain(lead["website"]) == domain(email):
                return True
        return False


def format_lead(lead: dict[str, Any], full: bool = False) -> str:
    line = f"#{lead['id']} [{lead['status']}] {lead['company']}"
    extras = [x for x in (lead.get("city"), lead.get("email"), lead.get("website")) if x]
    if extras:
        line += f" | {' | '.join(extras)}"
    if not full:
        return line
    rows = [line]
    for key in ("ico", "contact_name", "contact_role", "industry", "email_source", "fit_reason", "notes"):
        if lead.get(key):
            rows.append(f"  {key}: {lead[key]}")
    if lead.get("draft"):
        rows.append(f"  návrh e-mailu: {lead['draft']['subject']}\n{lead['draft']['body']}")
    for sent in lead.get("emails", []):
        rows.append(f"  odesláno {sent['sent_at']} na {sent['to']}: {sent['subject']}")
    return "\n".join(rows)


def build(config: Config, store: LeadStore) -> list[Tool]:
    drafts_dir = config.workspace / "navrhy"

    def save_lead(
        company: str,
        ico: str | None = None,
        website: str | None = None,
        email: str | None = None,
        email_source: str | None = None,
        contact_name: str | None = None,
        contact_role: str | None = None,
        city: str | None = None,
        industry: str | None = None,
        fit_reason: str | None = None,
        notes: str | None = None,
        status: str = "new",
    ) -> str:
        if status not in STATUSES:
            raise ToolError(f"Neplatný stav '{status}'.")
        if email and not email_source:
            raise ToolError("U e-mailu uveď email_source (URL stránky, kde je zveřejněný).")
        leads = store.load()
        duplicate = store.find_duplicate(leads, ico, email, website)
        if duplicate:
            return f"Lead už existuje, neukládám znovu: {format_lead(duplicate)}"
        lead = {
            "id": store.next_id(leads),
            "company": company,
            "ico": ico,
            "website": website,
            "email": email,
            "email_source": email_source,
            "contact_name": contact_name,
            "contact_role": contact_role,
            "city": city,
            "industry": industry,
            "fit_reason": fit_reason,
            "notes": notes,
            "status": status,
            "draft": None,
            "emails": [],
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        leads.append(lead)
        store.save(leads)
        return f"Uloženo: {format_lead(lead)}"

    def list_leads(status: str | None = None, query: str | None = None) -> str:
        leads = store.load()
        if status:
            leads = [lead for lead in leads if lead["status"] == status]
        if query:
            q = query.lower()
            leads = [lead for lead in leads if q in " ".join(str(v) for v in lead.values()).lower()]
        if not leads:
            return "Žádné leady."
        counts: dict[str, int] = {}
        for lead in store.load():
            counts[lead["status"]] = counts.get(lead["status"], 0) + 1
        header = "Celkem podle stavu: " + ", ".join(f"{k} {v}" for k, v in counts.items())
        return truncate(header + "\n" + "\n".join(format_lead(lead) for lead in leads))

    def get_lead(id: int) -> str:
        return format_lead(store.get(id), full=True)

    def update_lead(id: int, **changes: Any) -> str:
        changes = {k: v for k, v in changes.items() if v is not None}
        unknown = set(changes) - set(EDITABLE)
        if unknown:
            raise ToolError(f"Nelze měnit: {', '.join(sorted(unknown))}.")
        if "status" in changes and changes["status"] not in STATUSES:
            raise ToolError(f"Neplatný stav '{changes['status']}'.")
        if changes.get("email") and not (changes.get("email_source") or store.get(id).get("email_source")):
            raise ToolError("U e-mailu uveď email_source (URL stránky, kde je zveřejněný).")
        return f"Upraveno: {format_lead(store.update(id, **changes))}"

    def save_draft(id: int, subject: str, body: str) -> str:
        lead = store.get(id)
        if lead["status"] == "do_not_contact":
            raise ToolError("Tento kontakt si nepřeje být oslovován.")
        store.update(id, draft={"subject": subject, "body": body, "updated_at": now_iso()},
                     status="drafted" if lead["status"] in ("new", "qualified", "drafted") else lead["status"])
        drafts_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", lead["company"].lower()).strip("-")[:40] or "lead"
        path = drafts_dir / f"{id:04d}-{slug}.txt"
        path.write_text(
            f"Komu: {lead.get('email') or '(e-mail chybí)'}\nPředmět: {subject}\n\n{body}\n",
            encoding="utf-8",
        )
        return f"Návrh uložen k leadu #{id} a do souboru {path.relative_to(config.workspace)}."

    def export_leads(path: str = "leady.csv") -> str:
        from .files import resolve_in_workspace

        target = resolve_in_workspace(config.workspace, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        leads = store.load()
        columns = ["id", "status", *[c for c in EDITABLE if c != "status"], "created_at", "updated_at"]
        with target.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore", delimiter=";")
            writer.writeheader()
            writer.writerows(leads)
        return f"Exportováno {len(leads)} leadů do {Path(target).relative_to(config.workspace)}."

    lead_fields = {
        "company": {"type": "string", "description": "Obchodní jméno firmy."},
        "ico": {"type": "string"},
        "website": {"type": "string"},
        "email": {"type": "string", "description": "Jen veřejně zveřejněný pracovní e-mail."},
        "email_source": {"type": "string", "description": "URL, kde je e-mail zveřejněný."},
        "contact_name": {"type": "string"},
        "contact_role": {"type": "string"},
        "city": {"type": "string"},
        "industry": {"type": "string"},
        "fit_reason": {
            "type": "string",
            "description": "Proč firma odpovídá cílovému zákazníkovi a čeho se nabídka může chytit.",
        },
        "notes": {"type": "string"},
        "status": {"type": "string", "enum": list(STATUSES)},
    }

    return [
        Tool(
            name="save_lead",
            description=(
                "Uloží potenciálního zákazníka do databáze leadů. Duplicity (stejné IČO, e-mail "
                "nebo doména webu) odmítne a vrátí existující záznam."
            ),
            input_schema=schema(lead_fields, ["company"]),
            handler=save_lead,
        ),
        Tool(
            name="list_leads",
            description=(
                "Vypíše leady, volitelně podle stavu nebo hledaného textu. Použij před hledáním "
                "nových firem, abys neoslovoval někoho podruhé."
            ),
            input_schema=schema(
                {
                    "status": {"type": "string", "enum": list(STATUSES)},
                    "query": {"type": "string", "description": "Hledaný text (firma, město, obor…)."},
                }
            ),
            handler=list_leads,
        ),
        Tool(
            name="get_lead",
            description="Úplný detail leadu včetně návrhu e-mailu a historie odeslaných zpráv.",
            input_schema=schema({"id": {"type": "integer"}}, ["id"]),
            handler=get_lead,
        ),
        Tool(
            name="update_lead",
            description=(
                "Upraví údaje nebo stav leadu. Když kontakt odpoví, že nemá zájem nebo nechce "
                "další zprávy, nastav status do_not_contact."
            ),
            input_schema=schema({"id": {"type": "integer"}, **lead_fields}, ["id"]),
            handler=update_lead,
        ),
        Tool(
            name="save_draft",
            description=(
                "Uloží návrh obchodního e-mailu k leadu (stav drafted) a do složky navrhy/ "
                "k revizi uživatelem. Nic neodesílá."
            ),
            input_schema=schema(
                {
                    "id": {"type": "integer"},
                    "subject": {"type": "string"},
                    "body": {"type": "string", "description": "Celý text e-mailu včetně podpisu."},
                },
                ["id", "subject", "body"],
            ),
            handler=save_draft,
        ),
        Tool(
            name="export_leads",
            description="Exportuje všechny leady do CSV (oddělovač středník, otevře se v Excelu).",
            input_schema=schema({"path": {"type": "string", "description": "Výchozí leady.csv"}}),
            handler=export_leads,
        ),
    ]
