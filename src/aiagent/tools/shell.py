"""Shell commands, run inside the workspace directory."""

from __future__ import annotations

import subprocess

from ..config import Config
from . import Tool, ToolError, schema, truncate

DEFAULT_TIMEOUT = 120


def build(config: Config) -> list[Tool]:
    def run_command(command: str, timeout_seconds: int = DEFAULT_TIMEOUT) -> str:
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=config.workspace,
                capture_output=True,
                text=True,
                timeout=max(1, min(timeout_seconds, 600)),
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"Příkaz nedoběhl do {exc.timeout} s.") from exc
        parts = [f"exit code: {proc.returncode}"]
        if proc.stdout:
            parts.append(f"stdout:\n{proc.stdout}")
        if proc.stderr:
            parts.append(f"stderr:\n{proc.stderr}")
        return truncate("\n".join(parts))

    return [
        Tool(
            name="run_command",
            description=(
                "Spustí příkaz v shellu v pracovním adresáři agenta a vrátí exit code, stdout "
                "a stderr. Vhodné pro skripty, zpracování souborů, git apod. Vyžaduje schválení."
            ),
            input_schema=schema(
                {
                    "command": {"type": "string", "description": "Příkaz ke spuštění."},
                    "timeout_seconds": {
                        "type": "integer",
                        "description": f"Časový limit v sekundách (výchozí {DEFAULT_TIMEOUT}, max 600).",
                    },
                },
                ["command"],
            ),
            handler=run_command,
            needs_approval=True,
        )
    ]
