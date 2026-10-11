"""Agent loop tests against a scripted fake client (no network, no API key)."""

import json
from types import SimpleNamespace as NS

from aiagent.agent import Agent, AgentEvents
from aiagent.tools import build_registry


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
    agent = Agent(config, build_registry(config), client=client, approver=approver, events=events)
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
    tasks = json.loads((config.workspace / ".aiagent" / "tasks.json").read_text())
    assert [t["title"] for t in tasks] == ["A", "B"]


def test_denied_action_is_not_executed(config):
    agent, client, events = make_agent(
        config,
        [
            response("tool_use", tool_use("t1", "write_file", {"path": "x.txt", "content": "x"})),
            response("end_turn", text("Dobře, nezapisuji.")),
        ],
    )
    agent.run("zapiš soubor")
    assert not (config.workspace / "x.txt").exists()
    result = client.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "zamítl" in result["content"]
    audit = (config.workspace / ".aiagent" / "audit.jsonl").read_text().splitlines()
    assert json.loads(audit[0])["approved"] is False


def test_approved_action_runs(config):
    agent, _, _ = make_agent(
        config,
        [
            response("tool_use", tool_use("t1", "write_file", {"path": "x.txt", "content": "x"})),
            response("end_turn", text("Hotovo.")),
        ],
        approver=lambda n, i: True,
    )
    agent.run("zapiš soubor")
    assert (config.workspace / "x.txt").read_text() == "x"


def test_tool_errors_are_reported_to_model(config):
    agent, client, _ = make_agent(
        config,
        [
            response(
                "tool_use",
                tool_use("t1", "read_file", {"path": "chybi.txt"}),
                tool_use("t2", "nonexistent", {}),
                tool_use("t3", "read_file", {"wrong": 1}),
            ),
            response("end_turn", text("Soubor neexistuje.")),
        ],
    )
    agent.run("přečti soubor")
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
