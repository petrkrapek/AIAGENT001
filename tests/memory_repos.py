"""In-memory repositories with the same interface and rules as the Airtable ones."""

from __future__ import annotations

import itertools
from typing import Any

from aiagent.repos import (
    SEARCH_FIELDS,
    Repos,
    company_domain,
    format_profile,
    local_day,
    normalize_ico,
    now_iso,
)
from aiagent.tools import ToolError


def _id(counter) -> str:
    return f"rec{next(counter):014d}"


class MemoryLeads:
    def __init__(self, tz: str = "Europe/Prague"):
        self.tz = tz
        self.leads: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []
        self._ids = itertools.count(1)

    def create(self, lead: dict[str, Any]) -> dict[str, Any]:
        lead = {"service_id": None, **lead, "id": _id(self._ids), "ico": normalize_ico(lead.get("ico")),
                "domain": company_domain(lead.get("website"), lead.get("email")),
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
        changes = dict(changes)
        if changes.get("draft"):
            changes["draft"] = {**changes["draft"], "approved": False}
        if "ico" in changes:
            changes["ico"] = normalize_ico(changes["ico"])
        lead.update(changes, updated_at=now_iso())
        if "website" in changes or "email" in changes:
            lead["domain"] = company_domain(lead.get("website"), lead.get("email"))
        return dict(lead)

    def approve_draft(self, lead_id: str) -> None:
        """What a person does in Airtable (Schválení návrhu = Schváleno)."""
        self.leads[lead_id]["draft"]["approved"] = True

    def find_duplicate(self, ico: str | None, email: str | None, website: str | None):
        ico, host = normalize_ico(ico), company_domain(website, email)
        for lead in self.leads.values():
            if (ico and lead.get("ico") == ico) or (email and (lead.get("email") or "").lower() == email.lower()) \
                    or (host and lead.get("domain") == host):
                return dict(lead)
        return None

    def is_suppressed(self, email: str) -> bool:
        email, host = email.strip().lower(), company_domain(None, email)
        return any(
            lead["status"] == "do_not_contact"
            and ((lead.get("email") or "").lower() == email or (host and lead.get("domain") == host))
            for lead in self.leads.values()
        )

    def search(self, status: str | None = None, query: str | None = None) -> list[dict[str, Any]]:
        leads = list(self.leads.values())
        if status:
            leads = [lead for lead in leads if lead["status"] == status]
        if query:
            q = query.lower()
            leads = [lead for lead in leads
                     if q in " ".join(str(lead.get(k) or "") for k in SEARCH_FIELDS).lower()]
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


class MemoryTasks:
    def __init__(self):
        self.tasks: dict[str, dict[str, Any]] = {}
        self._ids = itertools.count(1)

    def create(self, title: str, due: str | None, notes: str | None) -> dict[str, Any]:
        task = {"id": _id(self._ids), "title": title, "status": "todo", "due": due, "notes": notes}
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


def memory_repos(tz: str = "Europe/Prague") -> Repos:
    return Repos(MemoryLeads(tz), MemoryTasks(), MemoryAudit(), MemoryServices(), MemoryProfile())
