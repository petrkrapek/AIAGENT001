"""Agent loop tests against a scripted fake client (no network, no API key)."""

from contextlib import contextmanager
from types import SimpleNamespace as NS

import pytest

from aiagent.agent import Agent, AgentEvents
from aiagent.tools import Tool, build_registry, schema
from memory_repos import memory_repos


def text(t):
    return NS(type="text", text=t)


def tool_use(id, name, input):
    return NS(type="tool_use", id=id, name=name, input=input)


def response(stop_reason, *content):
    usage = NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return NS(stop_reason=stop_reason, content=list(content), stop_details=None, usage=usage,
              model="claude-opus-5-5")


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = NS(messages=NS(stream=self._stream))

    @contextmanager
    def _stream(self, **kwargs):
        # Snapshot the messages: the agent keeps appending to the same list.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        yield NS(get_final_message=lambda: response)


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
    assert call["thinking"] == {"type": "adaptive", "display": "updates"}
    assert call["betas"] == ["thinking-display-updates-2026-08-18", "server-side-fallback-2026-07-01",
                             "context-management-2025-06-27"]
    assert call["max_tokens"] == 64_000
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["output_config"] == {"effort": config.effort}
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
            response("end_turn", text("Lead neexistuje.")),
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


def test_refused_message_is_dropped(config):
    agent, _, events = make_agent(config, [response("refusal")])
    agent.run("něco")
    assert agent.messages == []
    assert "odmítl" in events.notices[0] and "přeformulovat" in events.notices[0]


def test_refusal_after_tools_keeps_history(config):
    agent, _, events = make_agent(
        config, [response("tool_use", tool_use("t1", "list_tasks", {})), response("refusal")]
    )
    agent.run("něco")
    assert [m["role"] for m in agent.messages] == ["user", "assistant", "user"]
    assert "/reset" in events.notices[-1]


def test_max_tokens_answers_pending_tool_calls(config):
    agent, _, events = make_agent(
        config, [response("max_tokens", tool_use("t1", "publish", {"text": "x"}))],
        approver=lambda n, i: True,
    )
    agent.run("publikuj")
    assert agent.performed == []
    last = agent.messages[-1]
    assert last["role"] == "user" and last["content"][0]["tool_use_id"] == "t1"
    assert last["content"][0]["is_error"] is True


def test_ctrl_c_during_approval_keeps_history_valid(config):
    def interrupt(name, tool_input):
        raise KeyboardInterrupt

    agent, _, _ = make_agent(
        config,
        [response("tool_use", tool_use("t1", "list_tasks", {}), tool_use("t2", "publish", {"text": "x"}))],
        approver=interrupt,
    )
    with pytest.raises(KeyboardInterrupt):
        agent.run("publikuj")
    results = agent.messages[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert results[0]["is_error"] is False and results[1]["is_error"] is True


def test_iteration_limit_after_pause_turn_drops_paused_turn(config):
    config.max_iterations = 1
    paused = response("pause_turn", NS(type="server_tool_use", name="web_search", input={"query": "x"}))
    agent, _, _ = make_agent(config, [paused])
    agent.run("hledej")
    assert agent.messages[-1]["role"] == "user"


def test_paused_turn_is_dropped_when_resuming_fails(config, monkeypatch):
    import httpx2
    monkeypatch.setattr("aiagent.agent.STREAM_RETRIES", 0)
    paused = response("pause_turn", NS(type="server_tool_use", name="web_search", input={"query": "x"}))
    agent, client, _ = make_agent(config, [paused, httpx2.RemoteProtocolError("spojení ukončeno"),
                                           response("end_turn", text("Dobře."))])
    with pytest.raises(httpx2.RemoteProtocolError):
        agent.run("hledej")
    assert [m["role"] for m in agent.messages] == ["user"]
    # The conversation goes on: the next message doesn't follow the paused server-tool turn.
    agent.run("zkus to znovu")
    assert [m["role"] for m in client.calls[-1]["messages"]] == ["user", "user"]


def test_paused_turn_is_dropped_on_refusal(config):
    paused = response("pause_turn", NS(type="server_tool_use", name="web_search", input={"query": "x"}))
    agent, _, events = make_agent(config, [paused, response("refusal")])
    agent.run("hledej")
    assert agent.messages == [] and "přeformulovat" in events.notices[-1]


def test_dropped_stream_is_retried(config, monkeypatch):
    import anthropic
    import httpx2
    sleeps = []
    monkeypatch.setattr("aiagent.agent.time.sleep", sleeps.append)
    overloaded = anthropic.APIStatusError(
        "Overloaded", response=httpx2.Response(200, request=httpx2.Request("POST", "https://api.anthropic.com")),
        body={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
    agent, client, events = make_agent(config, [httpx2.RemoteProtocolError("spojení ukončeno"), overloaded,
                                                response("end_turn", text("Hotovo."))])
    assert agent.run("ahoj") == "Hotovo."
    assert len(client.calls) == 3 and sleeps == [5.0, 10.0]
    assert "zkouším znovu" in events.notices[0]
    # Persistent failures and other errors are not retried forever.
    agent, client, _ = make_agent(config, [httpx2.ReadTimeout("timeout")] * 3)
    with pytest.raises(httpx2.ReadTimeout):
        agent.run("ahoj")
    assert len(client.calls) == 3
    bad = anthropic.BadRequestError(
        "bad", response=httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com")), body=None)
    agent, client, _ = make_agent(config, [bad])
    with pytest.raises(anthropic.BadRequestError):
        agent.run("ahoj")
    assert len(client.calls) == 1


def test_model_specific_features(config):
    config.model = "claude-haiku-5-5"
    agent, client, _ = make_agent(config, [response("end_turn", text("Ahoj"))])
    agent.run("Ahoj")
    call = client.calls[0]
    assert "fallbacks" not in call and call["betas"] == ["context-management-2025-06-27"]
    assert call["thinking"] == {"type": "adaptive"}


def test_old_tool_results_are_cleared_server_side(config):
    agent, client, _ = make_agent(config, [response("end_turn", text("Ahoj"))])
    agent.run("Ahoj")
    edit = client.calls[0]["context_management"]["edits"][0]
    assert edit == {"type": "clear_tool_uses_20250919", "trigger": {"type": "input_tokens", "value": 60_000},
                    "keep": {"type": "tool_uses", "value": 4},
                    "clear_at_least": {"type": "input_tokens", "value": 20_000}}
    config.context_trigger = 0
    agent, client, _ = make_agent(config, [response("end_turn", text("Ahoj"))])
    agent.run("Ahoj")
    assert "context_management" not in client.calls[0]
    assert "context-management-2025-06-27" not in client.calls[0]["betas"]


def test_context_trigger_from_env(monkeypatch):
    from aiagent.config import Config
    assert Config.from_env().context_trigger == 60_000
    monkeypatch.setenv("AIAGENT_CONTEXT_TRIGGER", "0")
    assert Config.from_env().context_trigger == 0
    monkeypatch.setenv("AIAGENT_CONTEXT_TRIGGER", "5000")
    with pytest.raises(ValueError, match="AIAGENT_CONTEXT_TRIGGER"):
        Config.from_env()


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
