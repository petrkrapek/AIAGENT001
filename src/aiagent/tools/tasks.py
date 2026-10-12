"""Task list (follow-up reminders) stored in Airtable."""

from __future__ import annotations

from typing import Any

from ..config import Config
from . import Tool, ToolError, schema

STATUSES = ("todo", "in_progress", "done", "cancelled")


def _format(task: dict[str, Any]) -> str:
    line = f"{task['id']} [{task['status']}] {task['title']}"
    if task.get("due"):
        line += f" (termín: {task['due']})"
    if task.get("notes"):
        line += f"\n    poznámka: {task['notes']}"
    return line


def build(config: Config, repo: Any) -> list[Tool]:
    def add_task(title: str, due: str | None = None, notes: str | None = None) -> str:
        return f"Přidán úkol {_format(repo.create(title, due, notes))}"

    def list_tasks(status: str | None = None) -> str:
        if status is not None and status not in STATUSES:
            raise ToolError(f"Neplatný stav '{status}', povolené: {', '.join(STATUSES)}.")
        tasks = repo.list(status)
        return "\n".join(_format(t) for t in tasks) if tasks else "Žádné úkoly."

    def update_task(
        id: str,
        status: str | None = None,
        title: str | None = None,
        due: str | None = None,
        notes: str | None = None,
    ) -> str:
        if status is not None and status not in STATUSES:
            raise ToolError(f"Neplatný stav '{status}', povolené: {', '.join(STATUSES)}.")
        changes = {k: v for k, v in (("status", status), ("title", title), ("due", due), ("notes", notes))
                   if v is not None}
        return f"Upraveno: {_format(repo.update(id, changes))}"

    return [
        Tool(
            name="add_task",
            description="Přidá úkol (např. připomínku follow-upu) do tabulky Úkoly v Airtable.",
            input_schema=schema(
                {
                    "title": {"type": "string", "description": "Krátký název úkolu."},
                    "due": {"type": "string", "description": "Termín ve formátu YYYY-MM-DD."},
                    "notes": {"type": "string", "description": "Doplňující poznámka."},
                },
                ["title"],
            ),
            handler=add_task,
        ),
        Tool(
            name="list_tasks",
            description=(
                "Vypíše úkoly. Bez parametru vrátí otevřené úkoly (todo, in_progress); "
                "s parametrem status jen úkoly v daném stavu."
            ),
            input_schema=schema({"status": {"type": "string", "enum": list(STATUSES)}}),
            handler=list_tasks,
        ),
        Tool(
            name="update_task",
            description="Změní stav, název, termín nebo poznámku existujícího úkolu.",
            input_schema=schema(
                {
                    "id": {"type": "string", "description": "ID úkolu v Airtable (rec…)."},
                    "status": {"type": "string", "enum": list(STATUSES)},
                    "title": {"type": "string"},
                    "due": {"type": "string", "description": "YYYY-MM-DD"},
                    "notes": {"type": "string"},
                },
                ["id"],
            ),
            handler=update_task,
        ),
    ]
