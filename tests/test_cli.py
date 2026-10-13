"""CLI entry point: configuration errors, exit codes, task input."""

import inspect
from types import SimpleNamespace as NS
from unittest import mock

import anthropic

from aiagent import cli
from aiagent.airtable import AirtableError

AIRTABLE = {"AIRTABLE_TOKEN": "pat", "AIRTABLE_BASE_ID": "appAAAAAAAAAAAAAA"}


def set_env(monkeypatch, **values):
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_bad_numbers_and_timezone_are_reported(monkeypatch, capsys):
    set_env(monkeypatch, **AIRTABLE, AIAGENT_DAILY_EMAIL_LIMIT="dvacet")
    assert cli.main(["run", "x"]) == cli.EXIT_CONFIG
    assert "AIAGENT_DAILY_EMAIL_LIMIT musí být celé číslo" in capsys.readouterr().err
    set_env(monkeypatch, AIAGENT_DAILY_EMAIL_LIMIT="20", AIAGENT_TIMEZONE="Evropa/Praha")
    assert cli.main(["run", "x"]) == cli.EXIT_CONFIG
    assert "není platné časové pásmo" in capsys.readouterr().err


def test_env_file_only_from_current_directory(monkeypatch, tmp_path, capsys):
    (tmp_path / ".env").write_text("AIRTABLE_TOKEN=pat\nAIRTABLE_BASE_ID=wrong\n")
    child = tmp_path / "sub"
    child.mkdir()
    monkeypatch.chdir(child)  # the parent's .env must not be picked up
    assert cli.main(["run", "x"]) == cli.EXIT_CONFIG
    assert "AIRTABLE_TOKEN a AIRTABLE_BASE_ID" in capsys.readouterr().err


def test_missing_task_file(monkeypatch, capsys):
    set_env(monkeypatch, **AIRTABLE)
    assert cli.main(["run", "-f", "neexistuje.md"]) == cli.EXIT_CONFIG
    assert "nelze přečíst" in capsys.readouterr().err


def test_airtable_check_failure(monkeypatch, capsys):
    set_env(monkeypatch, **AIRTABLE)
    with mock.patch.object(cli.AirtableClient, "list_records",
                           side_effect=AirtableError("Airtable chyba 404", status=404)):
        assert cli.main(["run", "x"]) == cli.EXIT_CONFIG
    assert "airtable-setup" in capsys.readouterr().err


def test_ctrl_c_exits_130(monkeypatch):
    set_env(monkeypatch, **AIRTABLE)
    with mock.patch.object(cli.AirtableClient, "list_records", side_effect=KeyboardInterrupt):
        assert cli.main(["run", "x"]) == cli.EXIT_INTERRUPTED


def test_run_exit_code_follows_outcome():
    agent = NS(outcome="done", run=lambda task: None)
    assert cli.run_safely(agent, "x") == cli.EXIT_OK
    agent.outcome = "step_limit"
    assert cli.run_safely(agent, "x") == cli.EXIT_ERROR


def test_api_errors_go_to_stderr_with_request_id(capsys):
    request = mock.Mock()
    response = mock.Mock(status_code=400, headers={"request-id": "req_123"}, request=request)

    def fail(task):
        raise anthropic.BadRequestError("chyba", response=response, body=None)

    assert cli.run_safely(NS(run=fail), "x") == cli.EXIT_ERROR
    out = capsys.readouterr()
    assert "req_123" in out.err and out.out == ""


def test_dropped_connection_ends_the_task_not_the_program(capsys):
    import httpx2

    def fail(task):
        raise httpx2.RemoteProtocolError("peer closed connection without sending complete message body")

    assert cli.run_safely(NS(run=fail), "x") == cli.EXIT_ERROR
    assert "Spojení s API selhalo" in capsys.readouterr().err


def test_stream_accepts_every_parameter_the_agent_sends():
    """Guards against SDK changes: the FakeClient in other tests would hide them."""
    params = inspect.signature(anthropic.Anthropic(api_key="x").beta.messages.stream).parameters
    for name in ("model", "max_tokens", "system", "messages", "tools", "thinking", "output_config",
                 "cache_control", "betas", "fallbacks", "context_management"):
        assert name in params, name


def test_missing_blocklist_table_after_update(monkeypatch, capsys):
    set_env(monkeypatch, **AIRTABLE)
    missing = AirtableError("Airtable chyba 403: Token nemá přístup…", status=403)
    with mock.patch.object(cli.AirtableClient, "list_records", side_effect=[[], missing]):
        assert cli.main(["run", "x"]) == cli.EXIT_CONFIG
    assert "aiagent airtable-setup" in capsys.readouterr().err


# --- interactive chat and approvals ------------------------------------------------------


def chat_with(monkeypatch, config, inputs, responses=()):
    from aiagent.agent import Agent
    from aiagent.tools import build_registry
    from memory_repos import memory_repos
    from test_agent import FakeClient

    replies = iter(inputs)

    def fake_input(prompt=""):
        reply = next(replies)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr("builtins.input", fake_input)
    client = FakeClient(responses)
    agent = Agent(config, build_registry(config, memory_repos()), client=client, events=cli.ConsoleEvents())
    cli.chat(agent)
    return agent, client


def test_chat_commands(monkeypatch, capsys, config):
    from test_agent import response, text
    agent, client = chat_with(
        monkeypatch, config,
        ["", "/help", "/tools", "/leads", "/tasks", "/profil", "/sluzby", "ahoj", "/exit"],
        [response("end_turn", text("Dobrý den."))],
    )
    out = capsys.readouterr().out
    assert "Příkazy:" in out and "save_lead" in out and "Žádné leady." in out
    assert "Profil firmy v Airtable není vyplněný." in out and "Žádné aktivní služby." in out
    assert "jen návrhy" in out  # sending is off without SMTP
    assert len(client.calls) == 1 and "Dobrý den." in out


def test_chat_ends_on_ctrl_d_and_survives_api_errors(monkeypatch, capsys, config):
    # A failed request is reported and the chat goes on; Ctrl+D ends it.
    chat_with(monkeypatch, config, ["ahoj", "/reset", EOFError()],
              [anthropic.APIConnectionError(request=mock.Mock())])
    out = capsys.readouterr()
    assert "Spojení s API selhalo" in out.err and "Konverzace vymazána." in out.out


def test_broken_characters_in_input_dont_crash_the_chat(monkeypatch, capsys, config):
    import json

    from test_agent import response, text
    # Backspace over "á" in a terminal that erases bytes leaves "\udcc3" (half of the character).
    agent, client = chat_with(monkeypatch, config, ["Dobrý den, najdi firmy \udcc3", "/exit"],
                              [response("end_turn", text("Dobře."))])
    sent = client.calls[0]["messages"][0]["content"]
    assert sent == "Dobrý den, najdi firmy \ufffd"
    json.dumps(sent, ensure_ascii=False).encode("utf-8")  # what the SDK does; used to raise UnicodeEncodeError
    assert cli.clean_input("bez chyby: žluťoučký") == "bez chyby: žluťoučký"


def api_error(cls, status):
    response = mock.Mock(status_code=status, headers={"request-id": "req_1"}, request=mock.Mock())
    return cls("chyba", response=response, body=None)


def test_run_safely_explains_every_failure(capsys):
    import httpx2
    cases = [
        (api_error(anthropic.AuthenticationError, 401), "Neplatný API klíč"),
        (api_error(anthropic.PermissionDeniedError, 403), "oprávnění"),
        (api_error(anthropic.NotFoundError, 404), "nenalezen"),
        (api_error(anthropic.RateLimitError, 429), "limit požadavků"),
        (api_error(anthropic.InternalServerError, 500), "Chyba API (500)"),
        (anthropic.APITimeoutError(request=mock.Mock()), "neodpovědělo včas"),
        (anthropic.APIConnectionError(request=mock.Mock()), "Spojení s API selhalo"),
        (httpx2.ReadTimeout("timeout"), "Spojení s API selhalo"),
        (AirtableError("Airtable chyba 401: Neplatný AIRTABLE_TOKEN."), "AIRTABLE_TOKEN"),
        (TypeError("Could not resolve authentication method"), "Chybí API klíč"),
    ]
    for exc, message in cases:
        def fail(task, exc=exc):
            raise exc
        assert cli.run_safely(NS(run=fail), "x") == cli.EXIT_ERROR, message
        assert message in capsys.readouterr().err

    def interrupted(task):
        raise KeyboardInterrupt
    assert cli.run_safely(NS(run=interrupted), "x") == cli.EXIT_INTERRUPTED

    def bug(task):
        raise TypeError("unsupported operand")
    try:
        cli.run_safely(NS(run=bug), "x")
    except TypeError:
        pass
    else:
        raise AssertionError("a TypeError that is a bug must not be hidden")


def test_interactive_approver(monkeypatch, capsys):
    answers = iter(["a", "", "ne"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    tool_input = {"to": "info@alfa.cz‮", "headers": {"Authorization": "Bearer tajne"}}
    assert cli.interactive_approver("send_email", tool_input) is True
    assert cli.interactive_approver("send_email", tool_input) is False
    assert cli.interactive_approver("send_email", tool_input) is False
    out = capsys.readouterr().out
    assert "tajne" not in out and "‮" not in out

    def eof(prompt=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    assert cli.interactive_approver("send_email", {}) is False


def test_noninteractive_approver_refuses_and_explains(capsys):
    assert cli.noninteractive_approver("send_email", {}) is False
    assert "AIAGENT_SEND_MODE=auto" in capsys.readouterr().out


def test_smtp_test_without_smtp(monkeypatch, capsys):
    assert cli.main(["smtp-test"]) == cli.EXIT_CONFIG
    assert "SMTP není nastavené" in capsys.readouterr().err


def test_airtable_setup_command(monkeypatch, capsys):
    set_env(monkeypatch, **AIRTABLE)
    with mock.patch.object(cli, "setup_base", return_value=["Tabulka Leady vytvořena."]):
        assert cli.main(["airtable-setup"]) == cli.EXIT_OK
    assert "Tabulka Leady vytvořena." in capsys.readouterr().out
    with mock.patch.object(cli, "setup_base", side_effect=AirtableError("Airtable chyba 403")):
        assert cli.main(["airtable-setup"]) != cli.EXIT_OK
    assert "403" in capsys.readouterr().err


def test_yes_flag_uses_the_allowlist(monkeypatch):
    set_env(monkeypatch, **AIRTABLE, AIAGENT_HTTP_ALLOWED_HOSTS="hook.eu1.make.com")
    seen = {}

    def fake_agent(config, registry, **kwargs):
        seen["approver"] = kwargs["approver"]
        return NS(outcome="done", run=lambda task: None)

    with mock.patch.object(cli, "AirtableClient"), mock.patch.object(cli, "airtable_repos"), \
            mock.patch.object(cli, "sync_blocklist", return_value=0), \
            mock.patch.object(cli, "build_registry"), mock.patch.object(cli, "Agent", fake_agent), \
            mock.patch.object(cli, "enable_research"):
        assert cli.main(["--yes", "run", "úkol"]) == cli.EXIT_OK
    approve = seen["approver"]
    assert approve("http_request", {"url": "https://hook.eu1.make.com/x"}) is True
    assert approve("http_request", {"url": "https://jinde.example/x"}) is False
