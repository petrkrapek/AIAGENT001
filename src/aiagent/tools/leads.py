"""Lead database (a minimal CRM) stored in Airtable."""

from __future__ import annotations

from typing import Any

from ..config import Config
from ..repos import now_iso
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
    "city", "industry", "fit_reason", "notes", "status", "service_id",
)


def format_lead(lead: dict[str, Any], full: bool = False, emails: list[dict[str, Any]] | None = None) -> str:
    line = f"{lead['id']} [{lead['status']}] {lead['company']}"
    extras = [x for x in (lead.get("city"), lead.get("email"), lead.get("website")) if x]
    if extras:
        line += f" | {' | '.join(extras)}"
    if not full:
        return line
    rows = [line]
    for key in ("ico", "contact_name", "contact_role", "industry", "email_source", "fit_reason", "notes",
                "service_id"):
        if lead.get(key):
            rows.append(f"  {key}: {lead[key]}")
    if lead.get("draft"):
        rows.append(f"  návrh e-mailu: {lead['draft']['subject']}\n{lead['draft']['body']}")
    for sent in emails or []:
        rows.append(f"  odesláno {sent['sent_at']} na {sent['to']}: {sent['subject']}")
    return "\n".join(rows)


def _check_service(service_id: str | None) -> None:
    if service_id and not service_id.startswith("rec"):
        raise ToolError("service_id je ID služby z tabulky Služby (začíná 'rec'), viz přehled služeb.")


def build(config: Config, repo: Any) -> list[Tool]:
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
        service_id: str | None = None,
    ) -> str:
        _check_service(service_id)
        if status not in STATUSES:
            raise ToolError(f"Neplatný stav '{status}'.")
        if email and not email_source:
            raise ToolError("U e-mailu uveď email_source (URL stránky, kde je zveřejněný).")
        duplicate = repo.find_duplicate(ico, email, website)
        if duplicate:
            return f"Lead už existuje, neukládám znovu: {format_lead(duplicate)}"
        lead = repo.create({
            "company": company, "ico": ico, "website": website, "email": email,
            "email_source": email_source, "contact_name": contact_name, "contact_role": contact_role,
            "city": city, "industry": industry, "fit_reason": fit_reason, "notes": notes,
            "status": status, "service_id": service_id,
        })
        return f"Uloženo: {format_lead(lead)}"

    def list_leads(status: str | None = None, query: str | None = None) -> str:
        if status is not None and status not in STATUSES:
            raise ToolError(f"Neplatný stav '{status}'.")
        leads = repo.search(status=status, query=query)
        if not leads:
            return "Žádné leady."
        counts = repo.status_counts()
        header = "Celkem podle stavu: " + ", ".join(f"{k} {v}" for k, v in counts.items())
        return truncate(header + "\n" + "\n".join(format_lead(lead) for lead in leads))

    def get_lead(id: str) -> str:
        return format_lead(repo.get(id), full=True, emails=repo.emails(id))

    def update_lead(id: str, **changes: Any) -> str:
        changes = {k: v for k, v in changes.items() if v is not None}
        unknown = set(changes) - set(EDITABLE)
        if unknown:
            raise ToolError(f"Nelze měnit: {', '.join(sorted(unknown))}.")
        if "status" in changes and changes["status"] not in STATUSES:
            raise ToolError(f"Neplatný stav '{changes['status']}'.")
        _check_service(changes.get("service_id"))
        if changes.get("email") and not (changes.get("email_source") or repo.get(id).get("email_source")):
            raise ToolError("U e-mailu uveď email_source (URL stránky, kde je zveřejněný).")
        return f"Upraveno: {format_lead(repo.update(id, changes))}"

    def save_draft(id: str, subject: str, body: str, service_id: str | None = None) -> str:
        _check_service(service_id)
        lead = repo.get(id)
        if lead["status"] == "do_not_contact":
            raise ToolError("Tento kontakt si nepřeje být oslovován.")
        changes: dict[str, Any] = {"draft": {"subject": subject, "body": body, "updated_at": now_iso()}}
        if service_id:
            changes["service_id"] = service_id
        if lead["status"] in ("new", "qualified"):
            changes["status"] = "drafted"
        repo.update(id, changes)
        return f"Návrh uložen k leadu {id} (Airtable, tabulka Leady)."

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
        "service_id": {
            "type": "string",
            "description": "ID nabízené služby z tabulky Služby (rec…), pokud už je jasné, co firmě nabídnout.",
        },
    }
    id_field = {"id": {"type": "string", "description": "ID leadu v Airtable (rec…)."}}

    return [
        Tool(
            name="save_lead",
            description=(
                "Uloží potenciálního zákazníka do databáze leadů v Airtable. Duplicity (stejné IČO, "
                "e-mail nebo doména webu) odmítne a vrátí existující záznam."
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
            input_schema=schema(id_field, ["id"]),
            handler=get_lead,
        ),
        Tool(
            name="update_lead",
            description=(
                "Upraví údaje nebo stav leadu. Když kontakt odpoví, že nemá zájem nebo nechce "
                "další zprávy, nastav status do_not_contact."
            ),
            input_schema=schema({**id_field, **lead_fields}, ["id"]),
            handler=update_lead,
        ),
        Tool(
            name="save_draft",
            description=(
                "Uloží návrh obchodního e-mailu k leadu (stav drafted) do Airtable, kde ho uživatel "
                "může zkontrolovat a upravit. Nic neodesílá."
            ),
            input_schema=schema(
                {
                    **id_field,
                    "subject": {"type": "string"},
                    "body": {"type": "string", "description": "Celý text e-mailu včetně podpisu."},
                    "service_id": {"type": "string", "description": "ID služby, kterou e-mail nabízí (rec…)."},
                },
                ["id", "subject", "body"],
            ),
            handler=save_draft,
        ),
    ]
