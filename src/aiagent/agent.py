"""The agent loop: call Claude, run the tools it asks for, feed results back, repeat."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import anthropic

from .config import Config
from .tools import ToolError, ToolRegistry

MAX_TOKENS = 16_000
BETAS = ["server-side-fallback-2026-07-01"]

SYSTEM_PROMPT = """\
Jsi obchodník (business developer) firmy popsané v profilu níže. Tvým úkolem je \
vyhledávat potenciální zákazníky, ověřit, že nabídka pro ně dává smysl, a psát jim \
osobní e-maily s nabídkou spolupráce. Pracuješ samostatně: rozvrhni si postup, \
použij nástroje a pokračuj, dokud zadání nesplníš nebo nepotřebuješ informaci, \
kterou nemůžeš zjistit sám.

## Postup
1. Hledání: české firmy hledej v rejstříku ARES (ares_search: obor CZ-NACE, město, \
název), dál přes web_search (katalogy, oborové seznamy, zprávy). Než začneš, podívej \
se do databáze leadů (list_leads), ať nikoho neoslovíš dvakrát.
2. Kvalifikace: projdi web firmy (web_fetch) a ověř, že odpovídá cílovému zákazníkovi \
z profilu. Najdi konkrétní důvod, proč by je nabídka mohla zajímat (co dělají, co \
nového oznámili, jaký problém pravděpodobně řeší). Firmy, které neodpovídají, neukládej.
3. Kontakt: použij jen e-mail, který firma sama veřejně uvádí (stránka Kontakt, \
obchodní oddělení, konkrétní osoba v relevantní roli), a zapiš URL, kde je uvedený. \
E-mailové adresy nikdy nehádej ani neodvozuj ze vzorů jmen. Když e-mail nenajdeš, \
ulož lead bez něj a poznamenej to.
4. Zápis: každou vhodnou firmu ulož přes save_lead se stavem qualified a vyplň \
fit_reason.
5. E-mail: napiš návrh a ulož ho přes save_draft. Pravidla pro e-mail:
   - krátký (zhruba 80 až 150 slov), věcný, v jazyce firmy (výchozí čeština, vykání),
   - první věta je o adresátovi a jeho firmě (konkrétní zjištění), ne o nás,
   - jasně řekni, co nabízíme a jaký přínos to pro ně může mít, a doplň jeden konkrétní \
důkaz z profilu (reference, výsledek), pokud se hodí,
   - jedna jednoduchá výzva k akci (např. krátký hovor),
   - předmět krátký a konkrétní, bez clickbaitu a bez „RE:“/„FW:“,
   - nic si nevymýšlej: žádné nepodložené sliby, falešná naléhavost ani předstírání \
předchozího kontaktu,
   - na konci podpis z profilu a věta, že pokud si adresát další zprávy nepřeje, \
stačí odpovědět a už ho nebudeme kontaktovat.
6. Odeslání: {sending}

## Další pravidla
- Kontakty ve stavu do_not_contact nikdy neoslovuj. Když uživatel nahlásí odpověď \
typu „nemám zájem“ nebo „neposílejte“, nastav do_not_contact.
- Akce s vnějším dopadem schvaluje uživatel; aplikace se ho zeptá sama. Když akci \
zamítne, neopakuj ji a zeptej se, co upravit.
- Neoslovuj hromadně: každý e-mail musí být psaný pro konkrétní firmu.
- Dnešní datum zjistíš přes get_current_time; pro připomínky follow-upů použij add_task.
- Odpovídej uživateli česky (nebo jeho jazykem), stručně. Na konci shrň: kolik firem \
jsi prošel, kolik je kvalifikovaných, kolik návrhů je připraveno a kolik e-mailů odešlo.

## Prostředí
- Pracovní adresář: {workspace} (návrhy e-mailů ukládáš do podsložky navrhy/)
- Časové pásmo: {timezone}

## Profil firmy a nabídky
{profile}"""

SENDING_APPROVE = (
    "e-mail odešli přes send_email jen tehdy, když o to uživatel požádá nebo to zadání "
    "výslovně obsahuje. Každé odeslání uživatel schvaluje. Denní limit je {limit} e-mailů."
)
SENDING_AUTO = (
    "odesílání běží automaticky bez schvalování. E-mail odešli přes send_email jen tehdy, "
    "když to zadání výslovně požaduje, a vždy nejdřív ulož návrh přes save_draft: odeslat "
    "lze jen text shodný s uloženým návrhem. Buď o to pečlivější, protože e-mail před "
    "odesláním nikdo nekontroluje. Denní limit je {limit} e-mailů."
)
SENDING_DISABLED = (
    "odesílání e-mailů je vypnuté, návrhy pouze ukládej. Uživatel je odešle sám nebo si "
    "odesílání zapne."
)
PROFILE_MISSING = (
    "Profil zatím není vyplněný (soubor {path}). Než začneš psát e-maily, zeptej se "
    "uživatele, co firma nabízí, komu, čím se liší, na jaké reference se lze odvolat a "
    "jaký má být podpis. Vyhledávat firmy můžeš i bez profilu, pokud zadání obor určuje."
)


def build_system_prompt(config: Config, sending_enabled: bool) -> str:
    profile = config.load_profile() or PROFILE_MISSING.format(path=config.profile_path)
    if not sending_enabled:
        sending = SENDING_DISABLED
    elif config.send_mode == "auto":
        sending = SENDING_AUTO.format(limit=config.daily_email_limit)
    else:
        sending = SENDING_APPROVE.format(limit=config.daily_email_limit)
    return SYSTEM_PROMPT.format(
        sending=sending, workspace=config.workspace, timezone=config.timezone, profile=profile
    )


class AgentEvents:
    """UI hooks. The CLI overrides these; the defaults do nothing."""

    def on_text(self, text: str) -> None: ...
    def on_thinking(self, text: str) -> None: ...
    def on_tool_call(self, name: str, tool_input: dict[str, Any]) -> None: ...
    def on_tool_result(self, name: str, result: str, is_error: bool) -> None: ...
    def on_server_tool(self, name: str, tool_input: Any) -> None: ...
    def on_notice(self, text: str) -> None: ...


Approver = Callable[[str, dict[str, Any]], bool]


def deny_all(name: str, tool_input: dict[str, Any]) -> bool:
    return False


class Agent:
    def __init__(
        self,
        config: Config,
        registry: ToolRegistry,
        client: anthropic.Anthropic | None = None,
        approver: Approver = deny_all,
        events: AgentEvents | None = None,
        show_thinking: bool = False,
    ):
        self.config = config
        self.registry = registry
        self.client = client or anthropic.Anthropic()
        self.approver = approver
        self.events = events or AgentEvents()
        self.show_thinking = show_thinking
        self.system = build_system_prompt(config, sending_enabled="send_email" in registry)
        self.messages: list[dict[str, Any]] = []
        self.audit_path = config.workspace / ".aiagent" / "audit.jsonl"

    def reset(self) -> None:
        self.messages = []

    def run(self, user_input: str) -> str:
        """Process one user message; returns the final text of the agent's reply."""
        self.messages.append({"role": "user", "content": user_input})
        final_text = ""

        for _ in range(self.config.max_iterations):
            response = self._call_model()

            if response.stop_reason == "refusal":
                # Keep history append-only: the refused turn is simply not added.
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                self.events.on_notice(
                    "Model požadavek odmítl" + (f" (kategorie: {category})." if category else ".")
                )
                return final_text

            # Append the full content (thinking, tool_use, server tool blocks) unchanged.
            self.messages.append({"role": "assistant", "content": response.content})
            text = self._emit_blocks(response.content)
            if text:
                final_text = text

            if response.stop_reason == "tool_use":
                self.messages.append({"role": "user", "content": self._run_tools(response.content)})
                continue
            if response.stop_reason == "pause_turn":
                # A long server-side tool turn paused; re-sending resumes it.
                continue
            if response.stop_reason == "max_tokens":
                self.events.on_notice("Odpověď byla useknuta (limit max_tokens).")
            return final_text

        self.events.on_notice(
            f"Agent dosáhl limitu {self.config.max_iterations} kroků a zastavil se."
        )
        return final_text

    def _call_model(self) -> Any:
        thinking: dict[str, Any] = {"type": "adaptive"}
        if self.show_thinking:
            thinking["display"] = "summarized"
        return self.client.beta.messages.create(
            model=self.config.model,
            max_tokens=MAX_TOKENS,
            system=self.system,
            messages=self.messages,
            tools=self.registry.to_api(),
            thinking=thinking,
            output_config={"effort": self.config.effort},
            cache_control={"type": "ephemeral"},
            # If the model declines for policy reasons, the API retries on a fallback model.
            betas=BETAS,
            fallbacks="default",
        )

    def _emit_blocks(self, content: list[Any]) -> str:
        texts = []
        for block in content:
            if block.type == "text" and block.text:
                texts.append(block.text)
                self.events.on_text(block.text)
            elif block.type == "thinking" and getattr(block, "thinking", ""):
                self.events.on_thinking(block.thinking)
            elif block.type == "server_tool_use":
                self.events.on_server_tool(block.name, block.input)
            elif block.type == "fallback":
                self.events.on_notice(
                    f"Model {block.from_.model} odmítl, pokračuje {block.to.model}."
                )
        return "\n".join(texts)

    def _run_tools(self, content: list[Any]) -> list[dict[str, Any]]:
        # All results go back in a single user message, in the order they were requested.
        results = []
        for block in content:
            if block.type != "tool_use":
                continue
            tool_input = block.input if isinstance(block.input, dict) else {}
            result, is_error = self._execute(block.name, tool_input)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                    "is_error": is_error,
                }
            )
        return results

    def _execute(self, name: str, tool_input: dict[str, Any]) -> tuple[str, bool]:
        tool = self.registry.get(name)
        self.events.on_tool_call(name, tool_input)
        if tool is None:
            result, is_error, approved = f"Neznámý nástroj: {name}", True, None
        elif tool.requires_approval(tool_input) and not self.approver(name, tool_input):
            result, is_error, approved = "Uživatel tuto akci zamítl.", True, False
        else:
            approved = True if tool.requires_approval(tool_input) else None
            try:
                result, is_error = tool.handler(**tool_input), False
            except ToolError as exc:
                result, is_error = str(exc), True
            except TypeError as exc:
                result, is_error = f"Neplatné parametry nástroje: {exc}", True
            except Exception as exc:  # report unexpected failures to the model
                result, is_error = f"Chyba při běhu nástroje: {type(exc).__name__}: {exc}", True
        self.events.on_tool_result(name, result, is_error)
        self._audit(name, tool_input, approved, is_error)
        return result, is_error

    def _audit(self, name: str, tool_input: dict[str, Any], approved: bool | None, is_error: bool) -> None:
        record = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": name,
            "input": tool_input,
            "approved": approved,
            "error": is_error,
        }
        try:
            Path(self.audit_path).parent.mkdir(parents=True, exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass
