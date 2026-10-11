"""File access, confined to the workspace directory."""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from . import Tool, ToolError, schema, truncate


def resolve_in_workspace(workspace: Path, relative: str) -> Path:
    """Resolve a user-supplied path and refuse anything outside the workspace."""
    path = (workspace / relative).resolve()
    if path != workspace and workspace not in path.parents:
        raise ToolError(f"Cesta '{relative}' je mimo pracovní adresář.")
    return path


def build(config: Config) -> list[Tool]:
    workspace = config.workspace

    def list_files(path: str = ".") -> str:
        target = resolve_in_workspace(workspace, path)
        if not target.is_dir():
            raise ToolError(f"'{path}' není adresář.")
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        if not entries:
            return "(prázdný adresář)"
        lines = []
        for entry in entries[:500]:
            rel = entry.relative_to(workspace)
            lines.append(f"{rel}/" if entry.is_dir() else f"{rel}  ({entry.stat().st_size} B)")
        return "\n".join(lines)

    def read_file(path: str) -> str:
        target = resolve_in_workspace(workspace, path)
        if not target.is_file():
            raise ToolError(f"Soubor '{path}' neexistuje.")
        try:
            return truncate(target.read_text(encoding="utf-8"))
        except UnicodeDecodeError as exc:
            raise ToolError(f"Soubor '{path}' není textový (UTF-8).") from exc

    def write_file(path: str, content: str, append: bool = False) -> str:
        target = resolve_in_workspace(workspace, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a" if append else "w", encoding="utf-8") as fh:
            fh.write(content)
        action = "Doplněno do" if append else "Zapsáno do"
        return f"{action} '{target.relative_to(workspace)}' ({len(content)} znaků)."

    return [
        Tool(
            name="list_files",
            description="Vypíše soubory a podadresáře v pracovním adresáři agenta.",
            input_schema=schema(
                {"path": {"type": "string", "description": "Relativní cesta, výchozí '.'."}}
            ),
            handler=list_files,
        ),
        Tool(
            name="read_file",
            description="Přečte textový soubor z pracovního adresáře.",
            input_schema=schema(
                {"path": {"type": "string", "description": "Relativní cesta k souboru."}},
                ["path"],
            ),
            handler=read_file,
        ),
        Tool(
            name="write_file",
            description=(
                "Zapíše text do souboru v pracovním adresáři (vytvoří ho, nebo přepíše). "
                "S append=true text připojí na konec. Vyžaduje schválení uživatelem."
            ),
            input_schema=schema(
                {
                    "path": {"type": "string", "description": "Relativní cesta k souboru."},
                    "content": {"type": "string", "description": "Obsah k zapsání."},
                    "append": {
                        "type": "boolean",
                        "description": "Připojit na konec místo přepsání.",
                    },
                },
                ["path", "content"],
            ),
            handler=write_file,
            needs_approval=True,
        ),
    ]
