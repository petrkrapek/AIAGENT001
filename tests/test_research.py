"""research_company: one company per short-lived sub-agent, with its own context and tools."""

from aiagent.agent import Agent
from aiagent.config import SmtpConfig
from aiagent.research import NestedEvents, enable_research
from aiagent.tools import build_registry
from memory_repos import memory_repos
from test_agent import FakeClient, Recorder, response, text, tool_use

SERVICE = {"id": "recSERVICE0000001", "name": "Audit skladu", "description": "Projdeme sklad"}


def make_agent(config, responses, profile="### Firma\nPříklad s.r.o.", services=(SERVICE,)):
    client = FakeClient(responses)
    events = Recorder()
    repos = memory_repos()
    loads = {"profile": 0}

    def load_profile():
        loads["profile"] += 1
        return profile

    agent = Agent(config, build_registry(config, repos), client=client, events=events, audit=repos.audit,
                  load_profile=load_profile, load_services=lambda: list(services))
    enable_research(agent)
    return agent, client, repos, loads


SUB_RUN = [
    response("tool_use", tool_use("s1", "save_lead", {
        "company": "Alfa s.r.o.", "website": "https://alfa.cz", "email": "info@alfa.cz",
        "email_source": "https://alfa.cz/kontakt", "status": "qualified", "fit_reason": "vlastní sklad"})),
    response("tool_use", tool_use("s2", "save_draft", {
        "id": "rec00000000000001", "subject": "Sklad Alfy", "body": "Dobrý den, ..."})),
    response("end_turn", text("Návrh uložen, lead rec00000000000001, vlastní sklad.")),
]


def test_research_runs_in_its_own_context(config):
    config.smtp = SmtpConfig(host="smtp.example.com", sender="obchod@priklad.cz")
    config.send_mode = "approve"
    agent, client, repos, loads = make_agent(config, [
        response("tool_use", tool_use("m1", "research_company", {"company": "Alfa s.r.o.",
                                                                  "website": "https://alfa.cz"})),
        *SUB_RUN,
        response("end_turn", text("Prošel jsem 1 firmu, návrh je připraven.")),
    ])
    assert loads["profile"] == 1  # enabling research doesn't reload Airtable
    assert "research_company" in agent.system and "Weby kandidátů sám nečti" in agent.system
    assert agent.run("Najdi firmy") == "Prošel jsem 1 firmu, návrh je připraven."

    main_first, sub_first, *_, main_last = client.calls
    # The sub-agent starts from scratch with the task, a narrow tool set and its own prompt.
    assert sub_first["messages"] == [{"role": "user", "content": "Firma: Alfa s.r.o.\nWeb: https://alfa.cz"}]
    assert sub_first["system"][0]["text"].startswith("Jsi analytik")
    assert "Příklad s.r.o." in sub_first["system"][0]["text"] and "Audit skladu" in sub_first["system"][0]["text"]
    assert [t["name"] for t in sub_first["tools"]] == ["ares_company", "get_lead", "save_draft", "save_lead"]
    # The main conversation gets only the short report back, not the sub-agent's history.
    result = main_last["messages"][-1]["content"][0]
    assert result["content"] == "Návrh uložen, lead rec00000000000001, vlastní sklad." and not result["is_error"]
    assert len(main_last["messages"]) == 3
    lead = repos.leads.get("rec00000000000001")
    assert lead["status"] == "drafted" and lead["draft"]["subject"] == "Sklad Alfy"
    # Short research reports are needed for the final summary and are never cleared.
    assert main_first["context_management"]["edits"][0]["exclude_tools"] == ["research_company"]
    assert "exclude_tools" not in sub_first["context_management"]["edits"][0]


def test_research_needs_profile_and_services(config):
    agent, client, _, _ = make_agent(config, [
        response("tool_use", tool_use("m1", "research_company", {"company": "Alfa"})),
        response("end_turn", text("Doplňte profil.")),
    ], profile=None)
    agent.run("Najdi firmy")
    result = client.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "profil" in result["content"]
    assert len(client.calls) == 2  # no sub-agent request


def test_unfinished_research_is_reported(config):
    config.subagent_max_iterations = 1
    agent, client, _, _ = make_agent(config, [
        response("tool_use", tool_use("m1", "research_company", {"company": "Alfa"})),
        response("tool_use", tool_use("s1", "ares_company", {"ico": "abc"})),
        response("end_turn", text("Hotovo.")),
    ])
    agent.run("Najdi firmy")
    result = client.calls[2]["messages"][-1]["content"][0]["content"]
    assert result.startswith("Prověrka nedokončena (došel limit kroků).")


def test_subagent_model_and_without_research(config):
    config.subagent_model = "claude-sonnet-5-5"
    agent, client, _, _ = make_agent(config, [
        response("tool_use", tool_use("m1", "research_company", {"company": "Alfa"})),
        response("end_turn", text("Nehodí se, nemá vlastní sklad.")),
        response("end_turn", text("Hotovo.")),
    ])
    agent.run("Najdi firmy")
    assert [c["model"] for c in client.calls] == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
    plain = Agent(config, build_registry(config, memory_repos()), client=FakeClient([]))
    assert "research_company" not in plain.registry and "2. Kvalifikace" in plain.system


def test_nested_events_are_labelled():
    events = Recorder()
    events.calls = []
    events.on_tool_call = lambda name, tool_input: events.calls.append(name)
    nested = NestedEvents(events, "Velmi dlouhý název firmy, s.r.o. a spol.")
    nested.on_tool_call("web_fetch", {})
    nested.on_notice("pozor")
    assert events.calls == ["[Velmi dlouhý název firmy, s.r…] web_fetch"]
    assert events.notices == ["[Velmi dlouhý název firmy, s.r…] pozor"]


def test_subagent_settings_from_env(monkeypatch):
    from aiagent.config import Config
    config = Config.from_env()
    assert config.subagents is True and config.subagent_model == ""
    monkeypatch.setenv("AIAGENT_SUBAGENTS", "0")
    monkeypatch.setenv("AIAGENT_SUBAGENT_MODEL", "claude-sonnet-5-5")
    config = Config.from_env()
    assert config.subagents is False and config.subagent_model == "claude-sonnet-5-5"
