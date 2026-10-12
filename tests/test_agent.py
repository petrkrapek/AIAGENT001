"""Agent loop tests against a scripted fake client (no network, no API key)."""

from types import SimpleNamespace as NS

from aiagent.agent import Agent, AgentEvents
from aiagent.repos import memory_repos
from aiagent.tools import Tool, build_registry, schema


def text(t):
    return NS(type="text", text=t)


def tool_use(id, name, input):
    return NS(type="tool_use", id=id, name=name, input=input)


def response(stop_reason, *content):
    return NS(stop_reason=stop_reason, content=list(content), stop_details=None)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        # Snapshot the messages: the agent keeps appending to the same list.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


class Recorder(AgentEvents):
    def __init__(self):
        self.texts, self.notices, self.tools = [], [], []

    def on_text(self, t):
        self.texts.append(t)

    def on_notice(self, t):
        self.notices.append(t)

    def on_tool_result(self, name, result, is_error):
        self.tools.append((name, result, is_error))


def make_agent(config, responses, approver=lambda n, i: False):
    client = FakeClient(responses)
    events = Recorder()
    repos = memory_repos()
    registry = build_registry(config, repos)
    performed = []
    # A tool with side effects that needs the user's approval.
    registry._tools["publish"] = Tool(
        name="publish", description="test", input_schema=schema({"text": {"type": "string"}}, ["text"]),
        handler=lambda text: performed.append(text) or "ok", needs_approval=True,
    )
    agent = Agent(config, registry, client=client, approver=approver, events=events, audit=repos.audit)
    agent.repos, agent.performed = repos, performed
    return agent, client, events


def test_simple_answer(config):
    agent, client, events = make_agent(config, [response("end_turn", text("Ahoj!"))])
    assert agent.run("Ahoj") == "Ahoj!"
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": "high"}
    assert "tool_choice" not in call


def test_tool_loop_returns_results_in_one_message(config):
    agent, client, events = make_agent(
        config,
        [
            response(
                "tool_use",
                tool_use("t1", "add_task", {"title": "A"}),
                tool_use("t2", "add_task", {"title": "B"}),
            ),
            response("end_turn", text("Přidáno.")),
        ],
    )
    assert agent.run("přidej úkoly A a B") == "Přidáno."
    second_call_messages = client.calls[1]["messages"]
    results = second_call_messages[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert all(r["is_error"] is False for r in results)
    assert [t["title"] for t in agent.repos.tasks.tasks.values()] == ["A", "B"]


def test_denied_action_is_not_executed(config):
    agent, client, events = make_agent(
        config,
        [
            response("tool_use", tool_use("t1", "publish", {"text": "x"})),
            response("end_turn", text("Dobře, nepublikuji.")),
        ],
    )
    agent.run("publikuj")
    assert agent.performed == []
    result = client.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "zamítl" in result["content"]
    assert agent.repos.audit.records[0] == {"tool": "publish", "input": {"text": "x"},
                                            "approved": False, "error": True}


def test_approved_action_runs(config):
    agent, _, _ = make_agent(
        config,
        [
            response("tool_use", tool_use("t1", "publish", {"text": "x"})),
            response("end_turn", text("Hotovo.")),
        ],
        approver=lambda n, i: True,
    )
    agent.run("publikuj")
    assert agent.performed == ["x"]
    assert agent.repos.audit.records[0]["approved"] is True


def test_tool_errors_are_reported_to_model(config):
    agent, client, _ = make_agent(
        config,
        [
            response(
                "tool_use",
                tool_use("t1", "get_lead", {"id": "recCHYBI"}),
                tool_use("t2", "nonexistent", {}),
                tool_use("t3", "get_lead", {"wrong": 1}),
            ),
            response("end_turn", text("Soubor neexistuje.")),
        ],
    )
    agent.run("ukaž lead")
    results = client.calls[1]["messages"][-1]["content"]
    assert [r["is_error"] for r in results] == [True, True, True]


def test_pause_turn_resumes(config):
    paused = response("pause_turn", NS(type="server_tool_use", name="web_search", input={"query": "x"}))
    agent, client, _ = make_agent(config, [paused, response("end_turn", text("Výsledek."))])
    assert agent.run("hledej") == "Výsledek."
    assert len(client.calls) == 2
    assert client.calls[1]["messages"][-1]["role"] == "assistant"


def test_refusal_keeps_history_append_only(config):
    refused = response("refusal")
    agent, _, events = make_agent(config, [refused])
    agent.run("něco")
    assert agent.messages == [{"role": "user", "content": "něco"}]
    assert events.notices and "odmítl" in events.notices[0]


def test_iteration_limit(config):
    config.max_iterations = 2
    loop = [response("tool_use", tool_use(f"t{i}", "list_tasks", {})) for i in range(2)]
    agent, client, events = make_agent(config, loop)
    agent.run("smyčka")
    assert len(client.calls) == 2
    assert "limitu" in events.notices[-1]


def test_conversation_continues(config):
    agent, client, _ = make_agent(
        config, [response("end_turn", text("Jsi Petr.")), response("end_turn", text("Ano."))]
    )
    agent.run("Jmenuji se Petr.")
    agent.run("Pamatuješ si to?")
    roles = [m["role"] for m in client.calls[1]["messages"]]
    assert roles == ["user", "assistant", "user"]


def test_audit_failure_does_not_break_agent(config):
    agent, _, events = make_agent(
        config,
        [response("tool_use", tool_use("t1", "list_tasks", {})), response("end_turn", text("Hotovo."))],
    )

    class BrokenAudit:
        def log(self, *args):
            raise RuntimeError("Airtable spadl")

    agent.audit = BrokenAudit()
    assert agent.run("úkoly") == "Hotovo."
    assert any("Airtable spadl" in n for n in events.notices)
