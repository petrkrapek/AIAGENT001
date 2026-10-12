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
