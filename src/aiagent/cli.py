"""Command line interface: interactive chat (`aiagent`) or a one-shot task (`aiagent run`)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from typing import Any

import anthropic
import httpx2

from . import __version__
from .agent import Agent, AgentEvents, Approver, format_services
from .airtable import LEADS, AirtableClient, AirtableError, setup_base
from .config import EFFORT_LEVELS, SEND_MODES, Config
from .repos import airtable_repos, redact, sync_blocklist
from .research import enable_research
from .tools import ToolError, build_registry
from .tools.web import url_host

EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_INTERRUPTED = 0, 1, 2, 130

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


# Escape sequences and carriage returns from model output or web pages could rewrite what the
# terminal shows, e.g. hide the real recipient in an approval prompt.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def safe(text: str) -> str:
    return _CONTROL.sub("�", text)


def err(text: str) -> None:
    print(red(text), file=sys.stderr)


def _short(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(redact(value), ensure_ascii=False)
    text = safe(text.replace("\n", " ⏎ "))
    return text if len(text) <= limit else text[:limit] + "…"


class ConsoleEvents(AgentEvents):
    def on_text(self, text: str) -> None:
        print(f"\n{safe(text)}\n")

    def on_thinking(self, text: str) -> None:
        print(dim(f"💭 {safe(text)}"))

    def on_tool_call(self, name: str, tool_input: dict[str, Any]) -> None:
        print(dim(f"→ {name} {_short(tool_input)}"))

    def on_tool_result(self, name: str, result: str, is_error: bool) -> None:
        line = f"  ← {_short(result, 200)}"
        print(red(line) if is_error else dim(line))

    def on_server_tool(self, name: str, tool_input: Any) -> None:
        print(dim(f"→ {name} {_short(tool_input)}"))

    def on_notice(self, text: str) -> None:
        print(yellow(f"! {safe(text)}"))


def interactive_approver(name: str, tool_input: dict[str, Any]) -> bool:
    print(yellow(f"\nAgent chce provést akci: {bold(safe(name))}"))
    for key, value in redact(tool_input).items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        print(f"  {safe(str(key))}: {safe(text)}")
    try:
        answer = input(yellow("Povolit? [a/N] ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("a", "ano", "y", "yes")


def noninteractive_approver(name: str, tool_input: dict[str, Any]) -> bool:
    # Nobody to ask (cron, docker compose run -T): refuse actions that need approval.
    hint = " Pro odesílání bez terminálu nastavte AIAGENT_SEND_MODE=auto." if name == "send_email" else ""
    print(yellow(f"! Zamítnuto (běh bez terminálu): {name}.{hint}"))
    return False


def auto_approver(allowed_hosts: tuple[str, ...] = ()) -> Approver:
    """Approver for --yes: approves everything except e-mails and HTTP requests to hosts not allowed."""

    def approve(name: str, tool_input: dict[str, Any]) -> bool:
        if name == "send_email":
            # --yes must not become a way around the approved-draft guard of the auto send mode.
            print(yellow("! --yes neschvaluje e-maily. Pro odesílání bez dozoru použijte AIAGENT_SEND_MODE=auto."))
            return False
        if name == "http_request":
            # A web page read in the same run could ask the model to send data anywhere.
            host = url_host(str(tool_input.get("url", "")))
            if host not in allowed_hosts:
                print(yellow(f"! --yes neschvaluje požadavky na {safe(host) or 'tuto adresu'}. "
                             "Povolené servery nastavíte v AIAGENT_HTTP_ALLOWED_HOSTS."))
                return False
        print(yellow(f"! Automaticky schváleno: {name}"))
        return True

    return approve


HELP = """Příkazy:
  /help    nápověda
  /leads   přehled leadů (potenciálních zákazníků)
  /profil  profil firmy z Airtable
  /sluzby  nabízené služby z Airtable
  /tools   seznam nástrojů
  /tasks   otevřené úkoly
  /reset   začít novou konverzaci (znovu načte profil a služby)
  /exit    konec (také Ctrl+D)"""


def chat(agent: Agent) -> None:
    print(bold(f"AI obchodník {__version__}") + dim(f"  ·  model {agent.config.model}  ·  /help"))
    print(dim(f"Data: Airtable báze {agent.config.airtable_base_id}"))
    if agent.services:
        print(dim(f"Nabízené služby: {', '.join(s['name'] for s in agent.services)}"))
    else:
        print(yellow("! V Airtable (tabulka Služby) není žádná aktivní služba. "
                     "Popište, co nabízíte, a nastavte stav Aktivní."))
    if agent.profile is None:
        print(yellow("! V Airtable (tabulka Profil firmy) chybí profil. Vyplňte jeden řádek: "
                     "kdo jste, ideální zákazník, tón e-mailů, podpis…"))
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
                print(f"  {tool.name}{dim(' (schválení)') if tool.needs_approval else ''}")
            for server_tool in agent.registry.server_tools:
                print(f"  {server_tool['name']}{dim(' (server)')}")
            continue
        if user_input in ("/profil", "/profile"):
            agent.reset()
            print(agent.profile or "Profil firmy v Airtable není vyplněný.")
            print(dim("(Profil a služby znovu načteny, konverzace začíná znovu.)"))
            continue
        if user_input in ("/sluzby", "/služby", "/services"):
            agent.reset()
            # Show the services without the instructions meant for the model.
            print(format_services(agent.services).split("\n\n", 1)[1] if agent.services
                  else "Žádné aktivní služby.")
            print(dim("(Profil a služby znovu načteny, konverzace začíná znovu.)"))
            continue
        if user_input in ("/leads", "/tasks"):
            listing = agent.registry.get("list_leads" if user_input == "/leads" else "list_tasks")
            try:
                print(safe(listing.handler()) if listing else "Nedostupné.")
            except (AirtableError, ToolError) as exc:
                err(str(exc))
            continue
        run_safely(agent, user_input)


def run_safely(agent: Agent, task: str) -> int:
    """Run one task; return an exit code (0 = finished, 1 = error/unfinished, 130 = Ctrl+C)."""
    try:
        agent.run(task)
        return EXIT_OK if agent.outcome == "done" else EXIT_ERROR
    except KeyboardInterrupt:
        print(yellow("\n! Přerušeno."))
        return EXIT_INTERRUPTED
    except anthropic.AuthenticationError:
        err("Neplatný API klíč. Nastavte ANTHROPIC_API_KEY (viz .env.example).")
    except anthropic.PermissionDeniedError as exc:
        err(f"Klíč nemá oprávnění: {exc.message} (request id: {exc.request_id})")
    except anthropic.NotFoundError as exc:
        err(f"Model nebo endpoint nenalezen: {exc.message} (request id: {exc.request_id})")
    except anthropic.RateLimitError as exc:
        err(f"Překročen limit požadavků, zkuste to za chvíli. (request id: {exc.request_id})")
    except anthropic.BadRequestError as exc:
        err(f"Chybný požadavek: {exc.message} (request id: {exc.request_id})")
    except anthropic.APIStatusError as exc:
        err(f"Chyba API ({exc.status_code}): {exc.message} (request id: {exc.request_id})")
    except anthropic.APITimeoutError:
        err("API neodpovědělo včas. Zkuste to znovu, případně snižte AIAGENT_EFFORT.")
    except (anthropic.APIConnectionError, httpx2.TransportError):
        # httpx2: the connection dropped in the middle of a streamed response (after the agent's retries).
        err("Spojení s API selhalo. Zkontrolujte připojení a zkuste to znovu.")
    except AirtableError as exc:
        err(str(exc))
    except TypeError as exc:
        # The SDK raises a plain TypeError when no credentials are configured at all.
        if "authentication" not in str(exc):
            raise
        err("Chybí API klíč. Nastavte ANTHROPIC_API_KEY (viz .env.example).")
    return EXIT_ERROR


def smtp_test(config: Config, test_to: str | None) -> int:
    from .tools.mail import imap_check, smtp_check

    if config.smtp is None:
        err("SMTP není nastavené. Doplňte SMTP_HOST, SMTP_USER, SMTP_PASSWORD a SMTP_FROM do .env.")
        return EXIT_CONFIG
    try:
        print(smtp_check(config.smtp, test_to))
        if config.imap is not None:
            print(imap_check(config.imap))
    except ToolError as exc:
        err(str(exc))
        return EXIT_ERROR
    print(dim(f"Režim odesílání: {config.send_mode}"))
    return 0


def airtable_setup(config: Config) -> int:
    client = AirtableClient(config.airtable_token, config.airtable_base_id)
    try:
        for line in setup_base(client, config.timezone):
            print(line)
    except AirtableError as exc:
        err(str(exc))
        if exc.status == 403:
            print(dim("Token potřebuje oprávnění schema.bases:read a schema.bases:write."), file=sys.stderr)
        return EXIT_ERROR
    print("Airtable je připravený.")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aiagent", description="AI obchodník: hledá potenciální zákazníky a píše jim nabídky."
    )
    parser.add_argument("--version", action="version", version=f"aiagent {__version__}")
    parser.add_argument("--model", help="model Claude (výchozí z AIAGENT_MODEL)")
    parser.add_argument("--effort", choices=EFFORT_LEVELS, help="hloubka uvažování")
    parser.add_argument("--show-thinking", action="store_true", help="zobrazovat shrnutí uvažování")
    parser.add_argument("--yes", action="store_true",
                        help="schvalovat akce automaticky, kromě odeslání e-mailu a HTTP požadavků "
                             "na servery mimo AIAGENT_HTTP_ALLOWED_HOSTS (opatrně!)")
    parser.add_argument("--send-mode", choices=SEND_MODES,
                        help="odesílání e-mailů: off = jen návrhy, approve = se schválením, "
                             "auto = jen návrhy schválené v Airtable, bez dotazu")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("airtable-setup", help="vytvořit v Airtable tabulky a pole, která agent potřebuje")
    smtp_parser = sub.add_parser("smtp-test", help="ověřit připojení k SMTP serveru")
    smtp_parser.add_argument("--to", help="poslat testovací e-mail na tuto adresu")
    run_parser = sub.add_parser("run", help="provést jeden úkol a skončit (vhodné pro cron)")
    run_parser.add_argument("task", nargs="?", help="zadání úkolu")
    run_parser.add_argument("-f", "--file", help="načíst zadání ze souboru ('-' = stdin)")
    return parser


def setup_logging() -> None:
    level = (os.environ.get("AIAGENT_LOG_LEVEL") or "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def read_task(args: argparse.Namespace) -> str | None:
    if not args.file:
        return args.task
    if args.file == "-":
        return sys.stdin.read()
    try:
        with open(args.file, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        err(f"Soubor se zadáním nelze přečíst: {exc}")
        return None


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print()
        return EXIT_INTERRUPTED


def _main(argv: list[str] | None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = Config.from_env()
        if args.model:
            config.model = args.model
        if args.effort:
            config.effort = args.effort
        if args.send_mode:
            config.send_mode = args.send_mode
        config.validate()
    except ValueError as exc:
        err(str(exc))
        return EXIT_CONFIG
    setup_logging()

    if args.command == "smtp-test":
        return smtp_test(config, args.to)

    try:
        config.validate_airtable()
    except ValueError as exc:
        err(str(exc))
        return EXIT_CONFIG
    if args.command == "airtable-setup":
        return airtable_setup(config)

    task = None
    if args.command == "run":
        task = read_task(args)
        if task is None:
            return EXIT_CONFIG
        if not task.strip():
            parser.error("zadejte úkol jako argument nebo přes --file")

    client = AirtableClient(config.airtable_token, config.airtable_base_id)
    repos = airtable_repos(client, config.timezone)
    try:
        client.list_records(LEADS, fields=["Firma"], max_records=1)
        # Opt-outs set by hand in Airtable go to the blocklist too, so deleting the lead can't lift them.
        added = sync_blocklist(repos.leads, repos.blocklist)
    except AirtableError as exc:
        err(str(exc))
        if exc.status in (403, 404, 422):
            print(dim("Chybějící tabulky (i nové po aktualizaci agenta) vytvoříte příkazem: aiagent airtable-setup"),
                  file=sys.stderr)
        return EXIT_CONFIG
    if added:
        print(dim(f"Do tabulky Blokované kontakty doplněno kontaktů ve stavu Nekontaktovat: {added}."))

    if args.yes:
        approver = auto_approver(config.http_allowed_hosts)
    elif sys.stdin.isatty():
        approver = interactive_approver
    else:
        approver = noninteractive_approver

    agent = Agent(
        config,
        build_registry(config, repos),
        audit=repos.audit,
        load_services=repos.services.list_active,
        load_profile=repos.profile.load,
        approver=approver,
        events=ConsoleEvents(),
        show_thinking=args.show_thinking,
    )
    if config.subagents:
        enable_research(agent)

    if task is not None:
        return run_safely(agent, task.strip())
    chat(agent)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
