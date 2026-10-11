"""A simple persistent task list stored as JSON in the workspace."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Config
from . import Tool, ToolError, schema

STATUSES = ("todo", "in_progress", "done", "cancelled")


class TaskStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, tasks: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _format(task: dict[str, Any]) -> str:
    line = f"#{task['id']} [{task['status']}] {task['title']}"
    if task.get("due"):
        line += f" (termín: {task['due']})"
    if task.get("notes"):
        line += f"\n    poznámka: {task['notes']}"
    return line


def build(config: Config) -> list[Tool]:
    store = TaskStore(config.workspace / ".aiagent" / "tasks.json")

    def add_task(title: str, due: str | None = None, notes: str | None = None) -> str:
        tasks = store.load()
        task = {
            "id": max((t["id"] for t in tasks), default=0) + 1,
            "title": title,
            "status": "todo",
            "due": due,
            "notes": notes,
            "created_at": _now(),
            "updated_at": _now(),
        }
        tasks.append(task)
        store.save(tasks)
        return f"Přidán úkol {_format(task)}"

    def list_tasks(status: str | None = None) -> str:
        tasks = store.load()
        if status:
            tasks = [t for t in tasks if t["status"] == status]
        else:
            tasks = [t for t in tasks if t["status"] not in ("done", "cancelled")]
        if not tasks:
            return "Žádné úkoly."
        return "\n".join(_format(t) for t in tasks)

    def update_task(
        id: int,
        status: str | None = None,
        title: str | None = None,
        due: str | None = None,
        notes: str | None = None,
    ) -> str:
        if status is not None and status not in STATUSES:
            raise ToolError(f"Neplatný stav '{status}', povolené: {', '.join(STATUSES)}.")
        tasks = store.load()
        for task in tasks:
            if task["id"] == id:
                for key, value in (("status", status), ("title", title), ("due", due), ("notes", notes)):
                    if value is not None:
                        task[key] = value
                task["updated_at"] = _now()
                store.save(tasks)
                return f"Upraveno: {_format(task)}"
        raise ToolError(f"Úkol #{id} neexistuje.")

    return [
        Tool(
            name="add_task",
            description="Přidá úkol do seznamu úkolů uživatele.",
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
                    "id": {"type": "integer", "description": "Číslo úkolu."},
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
