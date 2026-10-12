"""Command line interface: interactive chat (`aiagent`) or a one-shot task (`aiagent run`)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import anthropic

from . import __version__
from .agent import Agent, AgentEvents
from .config import EFFORT_LEVELS, SEND_MODES, Config
from .airtable import AirtableClient, AirtableError, LEADS, setup_base
from .repos import airtable_repos
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


def noninteractive_approver(name: str, tool_input: dict[str, Any]) -> bool:
    # Nobody to ask (cron, docker compose run -T): refuse actions that need approval.
    hint = " Pro odesílání bez terminálu nastavte AIAGENT_SEND_MODE=auto." if name == "send_email" else ""
    print(yellow(f"! Zamítnuto (běh bez terminálu): {name}.{hint}"))
    return False


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
    print(dim(f"Data: Airtable báze {agent.config.airtable_base_id}"))
    if agent.config.load_profile() is None:
        print(yellow(f"! Chybí profil firmy ({agent.config.profile_path.name}). "
                     "Zkopírujte profil.example.md jako profil.md a vyplňte ho."))
    modes = {
        "off": "Odesílání e-mailů je vypnuté: agent připraví jen návrhy.",
        "approve": "Odesílání e-mailů zapnuto, každý e-mail schvalujete.",
        "auto": "Odesílání e-mailů zapnuto AUTOMATICKY bez schvalování (odesílá jen uložené návrhy).",
    }
    mode = agent.config.send_mode if "send_email" in agent.registry else "off"
    print(yellow(modes[mode]) if mode == "auto" else dim(modes[mode]))
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
        if user_input in ("/leads", "/tasks"):
            tool = agent.registry.get("list_leads" if user_input == "/leads" else "list_tasks")
            try:
                print(tool.handler())
            except AirtableError as exc:
                print(red(str(exc)))
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
    except AirtableError as exc:
        print(red(str(exc)))
    except TypeError as exc:
        # The SDK raises a plain TypeError when no credentials are configured at all.
        if "authentication" not in str(exc):
            raise
        print(red("Chybí API klíč. Nastavte ANTHROPIC_API_KEY (viz .env.example)."))
    return False


def smtp_test(config: Config, test_to: str | None) -> int:
    from .tools import ToolError
    from .tools.mail import smtp_check

    if config.smtp is None:
        print(red("SMTP není nastavené. Doplňte SMTP_HOST, SMTP_USER, SMTP_PASSWORD a SMTP_FROM do .env."))
        return 2
    try:
        print(smtp_check(config.smtp, test_to))
    except ToolError as exc:
        print(red(str(exc)))
        return 1
    print(dim(f"Režim odesílání: {config.send_mode}"))
    return 0


def airtable_setup(config: Config) -> int:
    client = AirtableClient(config.airtable_token, config.airtable_base_id)
    try:
        for line in setup_base(client, config.timezone):
            print(line)
    except AirtableError as exc:
        print(red(str(exc)))
        if "403" in str(exc):
            print(dim("Token potřebuje oprávnění schema.bases:read a schema.bases:write."))
        return 1
    print("Airtable je připravený.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aiagent", description="AI obchodník: hledá potenciální zákazníky a píše jim nabídky.")
    parser.add_argument("--version", action="version", version=f"aiagent {__version__}")
    parser.add_argument("--model", help="model Claude (výchozí z AIAGENT_MODEL)")
    parser.add_argument("--effort", choices=EFFORT_LEVELS, help="hloubka uvažování")
    parser.add_argument("--show-thinking", action="store_true", help="zobrazovat shrnutí uvažování")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="schvalovat všechny akce automaticky (opatrně!)",
    )
    parser.add_argument(
        "--send-mode",
        choices=SEND_MODES,
        help="odesílání e-mailů: off = jen návrhy, approve = se schválením, auto = bez schválení",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("airtable-setup", help="vytvořit v Airtable tabulky a pole, která agent potřebuje")
    smtp_parser = sub.add_parser("smtp-test", help="ověřit připojení k SMTP serveru")
    smtp_parser.add_argument("--to", help="poslat testovací e-mail na tuto adresu")
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
    if args.send_mode:
        config.send_mode = args.send_mode
    try:
        config.validate()
    except ValueError as exc:
        print(red(str(exc)), file=sys.stderr)
        return 2

    if args.command == "smtp-test":
        return smtp_test(config, args.to)

    try:
        config.validate_airtable()
    except ValueError as exc:
        print(red(str(exc)), file=sys.stderr)
        return 2
    if args.command == "airtable-setup":
        return airtable_setup(config)

    client = AirtableClient(config.airtable_token, config.airtable_base_id)
    try:
        client.list_records(LEADS, fields=["Firma"], max_records=1)
    except AirtableError as exc:
        print(red(str(exc)), file=sys.stderr)
        if "404" in str(exc) or "422" in str(exc):
            print(dim("Tabulky vytvoříte příkazem: aiagent airtable-setup"), file=sys.stderr)
        return 2
    repos = airtable_repos(client, config.timezone)

    if args.yes:
        approver = auto_approver
    elif sys.stdin.isatty():
        approver = interactive_approver
    else:
        approver = noninteractive_approver

    agent = Agent(
        config,
        build_registry(config, repos),
        audit=repos.audit,
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
