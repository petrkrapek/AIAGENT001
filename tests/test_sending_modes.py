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
                "SMTP_SECURITY", "AIAGENT_SEND_MODE", "AIRTABLE_TOKEN", "AIRTABLE_BASE_ID"):
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


def test_off_mode_has_no_send_tool(config, repos):
    assert "send_email" not in build_registry(smtp_config(config, "off"), repos)


def test_auto_mode_sends_only_drafts_approved_in_airtable(config, repos):
    registry = build_registry(smtp_config(config, "auto"), repos)
    tool = registry.get("send_email")
    assert not tool.requires_approval({})
    lead_id = "rec00000000000001"
    registry.get("save_lead").handler(company="Alfa", email="info@alfa.cz",
                                      email_source="https://alfa.cz/kontakt")
    send = dict(lead_id=lead_id, to="info@alfa.cz", subject="Nabídka", body="Text")
    with pytest.raises(ToolError, match="schválil"):
        tool.handler(**send)
    # The model saving a draft is not enough: a person has to approve it in Airtable.
    registry.get("save_draft").handler(id=lead_id, subject="Nabídka", body="Text\n")
    with pytest.raises(ToolError, match="schválil"):
        tool.handler(**send)
    repos.leads.approve_draft(lead_id)
    with pytest.raises(ToolError, match="liší"):
        tool.handler(**{**send, "body": "Jiný text"})
    with pytest.raises(ToolError, match="follow_up"):
        tool.handler(**send, follow_up=True)
    with mock.patch("aiagent.tools.mail.smtplib.SMTP") as smtp_cls:
        tool.handler(**send)
    smtp_cls.return_value.__enter__.return_value.send_message.assert_called_once()
    lead = repos.leads.get(lead_id)
    assert lead["status"] == "contacted" and lead["draft"] is None


def test_new_draft_resets_approval(config, repos):
    registry = build_registry(smtp_config(config, "auto"), repos)
    registry.get("save_lead").handler(company="Alfa", email="info@alfa.cz", email_source="https://alfa.cz")
    registry.get("save_draft").handler(id="rec00000000000001", subject="S", body="B")
    repos.leads.approve_draft("rec00000000000001")
    registry.get("save_draft").handler(id="rec00000000000001", subject="S", body="B2")
    assert repos.leads.get("rec00000000000001")["draft"]["approved"] is False


def test_yes_flag_does_not_approve_emails(capsys):
    from aiagent.cli import auto_approver
    assert auto_approver("send_email", {}) is False
    assert auto_approver("http_request", {"url": "https://x"}) is True


def test_system_prompt_per_mode(config):
    smtp_config(config, "auto")
    assert "schválil v Airtable" in build_system_prompt(config, sending_enabled=True)
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


# --- deliverability ----------------------------------------------------------------------

def test_outreach_headers(config, repos):
    from datetime import datetime as dt

    from aiagent.tools.mail import within_hours
    registry = build_registry(smtp_config(config, "approve"), repos)
    registry.get("save_lead").handler(company="Alfa", email="info@alfa.cz", email_source="https://alfa.cz")
    with mock.patch("aiagent.tools.mail.smtplib.SMTP") as smtp_cls:
        registry.get("send_email").handler(lead_id="rec00000000000001", to="info@alfa.cz", subject="S", body="B")
    sent = smtp_cls.return_value.__enter__.return_value.send_message.call_args[0][0]
    assert sent["Date"] and sent["List-Unsubscribe"] == "<mailto:obchod@priklad.cz?subject=Odhlaseni>"
    assert within_hours(dt(2026, 10, 12, 8, 0), (8, 17))         # Monday 8:00
    assert not within_hours(dt(2026, 10, 12, 17, 0), (8, 17))    # Monday 17:00
    assert not within_hours(dt(2026, 10, 17, 10, 0), (8, 17))    # Saturday
    assert within_hours(dt(2026, 10, 17, 3, 0), None)


def test_sending_window_is_enforced(config, repos):
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo

    config = smtp_config(config, "approve")
    config.send_hours = (8, 17)
    registry = build_registry(config, repos)
    assert "po–pá 8–17 h" in registry.get("send_email").description
    registry.get("save_lead").handler(company="Alfa", email="info@alfa.cz", email_source="https://alfa.cz")
    send = dict(lead_id="rec00000000000001", to="info@alfa.cz", subject="S", body="B")
    evening = dt(2026, 10, 12, 19, 30, tzinfo=ZoneInfo("Europe/Prague"))
    with mock.patch("aiagent.tools.mail.now_in", return_value=evening), \
            mock.patch("aiagent.tools.mail.smtplib.SMTP") as smtp_cls:
        with pytest.raises(ToolError, match="mimo povolenou dobu"):
            registry.get("send_email").handler(**send)
    smtp_cls.assert_not_called()
    assert repos.leads.get("rec00000000000001")["sending_started"] is None


def test_pause_between_emails(config, repos):
    config = smtp_config(config, "approve")
    config.send_pause = (30, 90)
    registry = build_registry(config, repos)
    for domain in ("alfa", "beta"):
        registry.get("save_lead").handler(company=domain, email=f"info@{domain}.cz",
                                          email_source=f"https://{domain}.cz")
    with mock.patch("aiagent.tools.mail.smtplib.SMTP"), \
            mock.patch("aiagent.tools.mail.time.sleep") as sleep, \
            mock.patch("aiagent.tools.mail.time.monotonic", side_effect=[100.0, 110.0, 111.0]), \
            mock.patch("aiagent.tools.mail.random.uniform", return_value=60.0):
        registry.get("send_email").handler(lead_id="rec00000000000001", to="info@alfa.cz", subject="S", body="B")
        sleep.assert_not_called()  # the first e-mail goes out right away
        registry.get("send_email").handler(lead_id="rec00000000000002", to="info@beta.cz", subject="S", body="B")
    sleep.assert_called_once_with(50.0)  # 60 s pause, 10 s already passed (e.g. while approving)


def test_pause_and_hours_from_env(clean_env):
    assert Config.from_env().send_pause == (30, 90) and Config.from_env().send_hours is None
    clean_env.setenv("AIAGENT_SEND_PAUSE", "0")
    clean_env.setenv("AIAGENT_SEND_HOURS", "8-17")
    config = Config.from_env()
    assert config.send_pause == (0, 0) and config.send_hours == (8, 17)
    for bad in ("17-8", "8-25", "osm"):
        clean_env.setenv("AIAGENT_SEND_HOURS", bad)
        with pytest.raises(ValueError, match="AIAGENT_SEND_HOURS"):
            Config.from_env().validate()
