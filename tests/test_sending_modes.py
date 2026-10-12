from unittest import mock

import pytest

from aiagent.agent import build_system_prompt
from aiagent.cli import main
from aiagent.config import Config, SmtpConfig
from aiagent.tools import ToolError, build_registry
from aiagent.tools.mail import open_smtp, smtp_check

SMTP_ENV = {"SMTP_HOST": "smtp.example.com", "SMTP_USER": "obchod@priklad.cz",
            "SMTP_PASSWORD": "tajne", "SMTP_FROM": "obchod@priklad.cz"}


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM",
                "SMTP_SECURITY", "AIAGENT_SEND_MODE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env lookup from the repo
    return monkeypatch


def test_send_mode_defaults(clean_env):
    assert Config.from_env().send_mode == "off"
    for key, value in SMTP_ENV.items():
        clean_env.setenv(key, value)
    config = Config.from_env()
    assert config.send_mode == "approve" and config.smtp.security == "starttls"


def test_port_465_uses_ssl(clean_env):
    for key, value in {**SMTP_ENV, "SMTP_PORT": "465"}.items():
        clean_env.setenv(key, value)
    assert Config.from_env().smtp.security == "ssl"


def test_validation(config):
    config.send_mode = "auto"
    with pytest.raises(ValueError, match="vyžaduje"):
        config.validate()
    config.send_mode = "sometimes"
    with pytest.raises(ValueError):
        config.validate()


def smtp_config(config, mode):
    config.smtp = SmtpConfig(host="smtp.example.com", sender="obchod@priklad.cz")
    config.send_mode = mode
    return config


def test_off_mode_has_no_send_tool(config):
    assert "send_email" not in build_registry(smtp_config(config, "off"))


def test_auto_mode_sends_only_saved_draft_without_approval(config):
    registry = build_registry(smtp_config(config, "auto"))
    tool = registry.get("send_email")
    assert not tool.requires_approval({})
    registry.get("save_lead").handler(company="Alfa", email="info@alfa.cz",
                                      email_source="https://alfa.cz/kontakt")
    with pytest.raises(ToolError, match="návrhem"):
        tool.handler(lead_id=1, to="info@alfa.cz", subject="Nabídka", body="Text")
    registry.get("save_draft").handler(id=1, subject="Nabídka", body="Text")
    with pytest.raises(ToolError, match="návrhem"):
        tool.handler(lead_id=1, to="info@alfa.cz", subject="Nabídka", body="Jiný text")
    with mock.patch("aiagent.tools.mail.smtplib.SMTP") as smtp_cls:
        tool.handler(lead_id=1, to="info@alfa.cz", subject="Nabídka", body="Text")
    smtp_cls.return_value.__enter__.return_value.send_message.assert_called_once()
    assert registry.leads.get(1)["status"] == "contacted"


def test_system_prompt_per_mode(config):
    smtp_config(config, "auto")
    assert "automaticky bez schvalování" in build_system_prompt(config, sending_enabled=True)
    config.send_mode = "approve"
    assert "uživatel schvaluje" in build_system_prompt(config, sending_enabled=True)


def test_open_smtp_security_modes():
    with mock.patch("aiagent.tools.mail.smtplib.SMTP_SSL") as ssl_cls:
        open_smtp(SmtpConfig(host="h", port=465, user="u", password="p", sender="s@x.cz", security="ssl"))
    ssl_cls.return_value.login.assert_called_once_with("u", "p")
    with mock.patch("aiagent.tools.mail.smtplib.SMTP") as smtp_cls:
        open_smtp(SmtpConfig(host="h", sender="s@x.cz", security="starttls"))
    smtp_cls.return_value.starttls.assert_called_once()
    smtp_cls.return_value.login.assert_not_called()
    with mock.patch("aiagent.tools.mail.smtplib.SMTP") as smtp_cls:
        open_smtp(SmtpConfig(host="h", port=25, sender="s@x.cz", security="none"))
    smtp_cls.return_value.starttls.assert_not_called()


def test_smtp_check_reports_failure():
    with mock.patch("aiagent.tools.mail.smtplib.SMTP", side_effect=OSError("Connection refused")):
        with pytest.raises(ToolError, match="Connection refused"):
            smtp_check(SmtpConfig(host="h", sender="s@x.cz"))


def test_cli_smtp_test(clean_env, capsys):
    for key, value in SMTP_ENV.items():
        clean_env.setenv(key, value)
    with mock.patch("aiagent.tools.mail.smtplib.SMTP") as smtp_cls:
        assert main(["smtp-test", "--to", "ja@priklad.cz"]) == 0
    sent = smtp_cls.return_value.__enter__.return_value.send_message.call_args[0][0]
    assert sent["To"] == "ja@priklad.cz"
    assert "a přihlášení je v pořádku" in capsys.readouterr().out


def test_cli_rejects_auto_without_smtp(clean_env, capsys):
    assert main(["--send-mode", "auto", "smtp-test"]) == 2
    assert "vyžaduje" in capsys.readouterr().err
