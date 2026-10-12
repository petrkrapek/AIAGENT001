"""Command line interface: interactive chat (`aiagent`) or a one-shot task (`aiagent run`)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import anthropic

from . import __version__
from .agent import Agent, AgentEvents, deny_all
from .config import EFFORT_LEVELS, Config
from .tools import build_registry

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def dim(text: str) -> str:
    return _c("2", text)


def bold(text: str) -> str:
    return _c("1", text)


def yellow(text: str) -> str:
    return _c("33", text)


def red(text: str) -> str:
    return _c("31", text)


def _short(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", " ⏎ ")
    return text if len(text) <= limit else text[:limit] + "…"


class ConsoleEvents(AgentEvents):
    def on_text(self, text: str) -> None:
        print(f"\n{text}\n")

    def on_thinking(self, text: str) -> None:
        print(dim(f"💭 {text}"))

    def on_tool_call(self, name: str, tool_input: dict[str, Any]) -> None:
        print(dim(f"→ {name} {_short(tool_input)}"))

    def on_tool_result(self, name: str, result: str, is_error: bool) -> None:
        line = f"  ← {_short(result, 200)}"
        print(red(line) if is_error else dim(line))

    def on_server_tool(self, name: str, tool_input: Any) -> None:
        print(dim(f"→ {name} {_short(tool_input)}"))

    def on_notice(self, text: str) -> None:
        print(yellow(f"! {text}"))


def interactive_approver(name: str, tool_input: dict[str, Any]) -> bool:
    print(yellow(f"\nAgent chce provést akci: {bold(name)}"))
    for key, value in tool_input.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        print(f"  {key}: {text}")
    try:
        answer = input(yellow("Povolit? [a/N] ")).strip().lower()
    except EOFError:
        return False
    return answer in ("a", "ano", "y", "yes")


def auto_approver(name: str, tool_input: dict[str, Any]) -> bool:
    print(yellow(f"! Automaticky schváleno: {name}"))
    return True


HELP = """Příkazy:
  /help    nápověda
  /leads   přehled leadů (potenciálních zákazníků)
  /tools   seznam nástrojů
  /tasks   otevřené úkoly
  /reset   začít novou konverzaci
  /exit    konec (také Ctrl+D)"""


def chat(agent: Agent) -> None:
    print(bold(f"AI obchodník {__version__}") + dim(f"  ·  model {agent.config.model}  ·  /help"))
    print(dim(f"Pracovní adresář: {agent.config.workspace}"))
    if agent.config.load_profile() is None:
        print(yellow(f"! Chybí profil firmy ({agent.config.profile_path.name}). "
                     "Zkopírujte profil.example.md jako profil.md a vyplňte ho."))
    if "send_email" not in agent.registry:
        print(dim("Odesílání e-mailů není nastavené (SMTP): agent připraví jen návrhy."))
    while True:
        try:
            user_input = input(bold("\nvy › ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not user_input:
            continue
        if user_input in ("/exit", "/quit"):
            return
        if user_input == "/help":
            print(HELP)
            continue
        if user_input == "/reset":
            agent.reset()
            print(dim("Konverzace vymazána."))
            continue
        if user_input == "/tools":
            for tool in agent.registry.tools:
                mark = {True: " (schválení)", False: ""}.get(tool.needs_approval, " (schválení u ne-GET)")
                print(f"  {tool.name}{dim(mark)}")
            for tool in agent.registry.server_tools:
                print(f"  {tool['name']}{dim(' (server)')}")
            continue
        if user_input == "/leads":
            list_leads = agent.registry.get("list_leads")
            print(list_leads.handler() if list_leads else "Databáze leadů není dostupná.")
            continue
        if user_input == "/tasks":
            list_tasks = agent.registry.get("list_tasks")
            print(list_tasks.handler() if list_tasks else "Nástroj úkolů není dostupný.")
            continue
        run_safely(agent, user_input)


def run_safely(agent: Agent, task: str) -> bool:
    try:
        agent.run(task)
        return True
    except KeyboardInterrupt:
        print(yellow("\n! Přerušeno."))
    except anthropic.AuthenticationError:
        print(red("Neplatný API klíč. Nastavte ANTHROPIC_API_KEY (viz .env.example)."))
    except anthropic.PermissionDeniedError as exc:
        print(red(f"Klíč nemá oprávnění: {exc.message}"))
    except anthropic.NotFoundError as exc:
        print(red(f"Model nebo endpoint nenalezen: {exc.message}"))
    except anthropic.RateLimitError:
        print(red("Překročen limit požadavků, zkuste to za chvíli."))
    except anthropic.BadRequestError as exc:
        print(red(f"Chybný požadavek: {exc.message}"))
    except anthropic.APIStatusError as exc:
        print(red(f"Chyba API ({exc.status_code}): {exc.message}"))
    except anthropic.APIConnectionError:
        print(red("Nepodařilo se spojit s API. Zkontrolujte připojení."))
    except TypeError as exc:
        # The SDK raises a plain TypeError when no credentials are configured at all.
        if "authentication" not in str(exc):
            raise
        print(red("Chybí API klíč. Nastavte ANTHROPIC_API_KEY (viz .env.example)."))
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aiagent", description="AI obchodník: hledá potenciální zákazníky a píše jim nabídky.")
    parser.add_argument("--version", action="version", version=f"aiagent {__version__}")
    parser.add_argument("--model", help="model Claude (výchozí z AIAGENT_MODEL)")
    parser.add_argument("--effort", choices=EFFORT_LEVELS, help="hloubka uvažování")
    parser.add_argument("--workspace", help="pracovní adresář agenta")
    parser.add_argument("--show-thinking", action="store_true", help="zobrazovat shrnutí uvažování")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="schvalovat všechny akce automaticky (opatrně!)",
    )
    sub = parser.add_subparsers(dest="command")
    run_parser = sub.add_parser("run", help="provést jeden úkol a skončit (vhodné pro cron)")
    run_parser.add_argument("task", nargs="?", help="zadání úkolu")
    run_parser.add_argument("-f", "--file", help="načíst zadání ze souboru ('-' = stdin)")
    args = parser.parse_args(argv)

    try:
        config = Config.from_env()
    except ValueError as exc:
        print(red(str(exc)), file=sys.stderr)
        return 2
    if args.model:
        config.model = args.model
    if args.effort:
        config.effort = args.effort
    if args.workspace:
        from pathlib import Path

        config.workspace = Path(args.workspace).expanduser().resolve()

    if args.yes:
        approver = auto_approver
    elif sys.stdin.isatty():
        approver = interactive_approver
    else:
        approver = deny_all  # nobody to ask: refuse actions that need approval

    try:
        registry = build_registry(config)
        writable = os.access(config.workspace, os.W_OK)
    except PermissionError:
        writable = False
    if not writable:
        print(red(f"Do pracovního adresáře {config.workspace} nelze zapisovat. V Dockeru nastavte "
                  "AIAGENT_UID a AIAGENT_GID podle vlastníka složky workspace (id -u, id -g)."),
              file=sys.stderr)
        return 2

    agent = Agent(
        config,
        registry,
        approver=approver,
        events=ConsoleEvents(),
        show_thinking=args.show_thinking,
    )

    if args.command == "run":
        if args.file:
            task = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
        else:
            task = args.task
        if not task or not task.strip():
            run_parser.error("zadejte úkol jako argument nebo přes --file")
        return 0 if run_safely(agent, task.strip()) else 1

    chat(agent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
