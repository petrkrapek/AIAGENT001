"""research_company: qualify one company and draft its e-mail in a separate, short-lived agent.

The main agent only finds candidates and hands each one over. Web pages read during the research
stay in the sub-agent's own conversation, which is thrown away afterwards; the main conversation
gets back a few lines. The sub-agent can't send e-mails, call webhooks or change existing leads.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .agent import RESEARCH_PROMPT, Agent, AgentEvents, deny_all
from .tools import Tool, ToolError, ToolRegistry, schema, truncate

# Everything the sub-agent may do; nothing here needs approval or reaches outside Airtable and the web.
RESEARCH_TOOLS = ("ares_company", "get_lead", "save_draft", "save_lead")
MAX_REPORT_CHARS = 2_000
OUTCOMES = {
    "refused": "model požadavek odmítl",
    "truncated": "odpověď byla useknuta",
    "context_full": "zaplnil se kontext",
    "step_limit": "došel limit kroků",
}


class NestedEvents(AgentEvents):
    """Shows the sub-agent's progress inside the main agent's output, marked with the company."""

    def __init__(self, parent: AgentEvents, label: str):
        self.parent = parent
        self.label = label if len(label) <= 30 else label[:29] + "…"

    def on_text(self, text: str) -> None:
        self.parent.on_thinking(f"[{self.label}] {text}")

    def on_thinking(self, text: str) -> None:
        self.parent.on_thinking(f"[{self.label}] {text}")

    def on_tool_call(self, name: str, tool_input: dict[str, Any]) -> None:
        self.parent.on_tool_call(f"[{self.label}] {name}", tool_input)

    def on_tool_result(self, name: str, result: str, is_error: bool) -> None:
        self.parent.on_tool_result(f"[{self.label}] {name}", result, is_error)

    def on_server_tool(self, name: str, tool_input: Any) -> None:
        self.parent.on_server_tool(f"[{self.label}] {name}", tool_input)

    def on_notice(self, text: str) -> None:
        self.parent.on_notice(f"[{self.label}] {text}")


def research_registry(registry: ToolRegistry) -> ToolRegistry:
    tools = [tool for name in RESEARCH_TOOLS if (tool := registry.get(name)) is not None]
    return ToolRegistry(tools, registry.server_tools)


def build_research_tool(parent: Agent) -> Tool:
    config = replace(parent.config, model=parent.config.subagent_model or parent.config.model,
                     max_iterations=parent.config.subagent_max_iterations)
    registry = research_registry(parent.registry)

    def research_company(company: str, website: str | None = None, ico: str | None = None,
                         notes: str | None = None) -> str:
        if parent.profile is None or not parent.services:
            raise ToolError("Chybí profil firmy nebo aktivní služby v Airtable. Než budeš firmy prověřovat, "
                            "požádej uživatele, aby je doplnil.")
        task = [f"Firma: {company}"]
        task += [f"{label}: {value}" for label, value in (("Web", website), ("IČO", ico), ("Poznámky", notes))
                 if value]
        sub = Agent(
            config, registry, client=parent.client, approver=deny_all,
            events=NestedEvents(parent.events, company), audit=parent.audit,
            # The parent's copies: no extra Airtable reads, and the same profile for every company.
            load_services=lambda: parent.services, load_profile=lambda: parent.profile,
            system_template=RESEARCH_PROMPT,
        )
        report = sub.run("\n".join(task)).strip() or "(bez závěrečné zprávy)"
        if sub.outcome != "done":
            report = f"Prověrka nedokončena ({OUTCOMES.get(sub.outcome, sub.outcome)}). {report}"
        return truncate(report, MAX_REPORT_CHARS)

    return Tool(
        name="research_company",
        description=(
            "Prověří jednu firmu v samostatném kontextu: projde její web, ověří, že odpovídá profilu, "
            "dohledá veřejný e-mail, uloží lead (save_lead) a připraví návrh e-mailu (save_draft). "
            "Vrátí několik řádků s výsledkem a ID leadu. Nic neodesílá. Pro každou kandidátní firmu "
            "zavolej zvlášť; web, IČO a důvod výběru předej, pokud je znáš."
        ),
        input_schema=schema(
            {
                "company": {"type": "string", "description": "Název firmy."},
                "website": {"type": "string", "description": "Web firmy, pokud je znám."},
                "ico": {"type": "string", "description": "IČO, pokud je známo."},
                "notes": {"type": "string",
                          "description": "Co už o firmě víš a proč jsi ji vybral (zdroj, obor, město)."},
            },
            ["company"],
        ),
        handler=research_company,
    )


def enable_research(agent: Agent) -> None:
    """Give the agent the research_company tool and switch its prompt to delegating."""
    tool = build_research_tool(agent)
    agent.set_registry(ToolRegistry(agent.registry.tools + [tool], agent.registry.server_tools))
