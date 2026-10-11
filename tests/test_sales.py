import csv
from unittest import mock

import pytest

from aiagent.agent import build_system_prompt
from aiagent.config import SmtpConfig
from aiagent.tools import ToolError, build_registry
from aiagent.tools import ares


def call(registry, tool, **kwargs):
    return registry.get(tool).handler(**kwargs)


@pytest.fixture
def smtp_config(config):
    config.smtp = SmtpConfig(host="smtp.example.com", sender="obchod@priklad.cz")
    return config


@pytest.fixture
def fake_smtp():
    with mock.patch("aiagent.tools.mail.smtplib.SMTP") as smtp_cls:
        yield smtp_cls.return_value.__enter__.return_value


def add_lead(registry, **kwargs):
    fields = {"company": "Alfa s.r.o.", "ico": "11111111", "website": "https://www.alfa.cz",
              "email": "info@alfa.cz", "email_source": "https://www.alfa.cz/kontakt",
              "status": "qualified", **kwargs}
    return call(registry, "save_lead", **fields)


# --- leads ---------------------------------------------------------------

def test_save_lead_and_deduplicate(config):
    registry = build_registry(config)
    assert "Uloženo: #1" in add_lead(registry)
    assert "už existuje" in add_lead(registry, ico=None, email=None, website="http://alfa.cz/o-nas")
    assert "už existuje" in add_lead(registry, ico="2", email="INFO@alfa.cz", website=None)
    assert "Uloženo: #2" in add_lead(registry, company="Beta", ico="22222222",
                                     website="beta.cz", email="obchod@beta.cz")


def test_email_requires_source(config):
    registry = build_registry(config)
    with pytest.raises(ToolError):
        call(registry, "save_lead", company="X", email="a@x.cz")


def test_update_lead_validation(config):
    registry = build_registry(config)
    add_lead(registry)
    assert "[do_not_contact]" in call(registry, "update_lead", id=1, status="do_not_contact")
    with pytest.raises(ToolError):
        call(registry, "update_lead", id=1, status="hotovo")
    with pytest.raises(ToolError):
        call(registry, "update_lead", id=99, notes="x")


def test_save_draft_writes_file(config):
    registry = build_registry(config)
    add_lead(registry, company="Alfa & syn s.r.o.")
    out = call(registry, "save_draft", id=1, subject="Spolupráce", body="Dobrý den, ...")
    assert "navrhy/0001-alfa-syn-s-r-o.txt" in out
    text = (config.workspace / "navrhy" / "0001-alfa-syn-s-r-o.txt").read_text()
    assert text.startswith("Komu: info@alfa.cz\nPředmět: Spolupráce")
    assert "[drafted]" in call(registry, "list_leads")
    assert "Dobrý den" in call(registry, "get_lead", id=1)


def test_no_draft_for_do_not_contact(config):
    registry = build_registry(config)
    add_lead(registry, status="do_not_contact")
    with pytest.raises(ToolError):
        call(registry, "save_draft", id=1, subject="x", body="y")


def test_export_csv(config):
    registry = build_registry(config)
    add_lead(registry)
    call(registry, "export_leads")
    with open(config.workspace / "leady.csv", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh, delimiter=";"))
    assert rows[0]["company"] == "Alfa s.r.o." and rows[0]["email"] == "info@alfa.cz"


def test_list_leads_filters(config):
    registry = build_registry(config)
    add_lead(registry, city="Brno")
    add_lead(registry, company="Beta", ico="2", website="beta.cz", email=None, city="Praha", status="new")
    out = call(registry, "list_leads", query="brno")
    assert "Alfa" in out and "Beta" not in out
    assert "Beta" in call(registry, "list_leads", status="new")


# --- sending ---------------------------------------------------------------

def test_send_email_records_history(smtp_config, fake_smtp):
    registry = build_registry(smtp_config)
    add_lead(registry)
    out = call(registry, "send_email", lead_id=1, to="info@alfa.cz", subject="Nabídka", body="Text")
    assert "odeslán" in out
    sent = fake_smtp.send_message.call_args[0][0]
    assert sent["To"] == "info@alfa.cz" and sent["From"] == "obchod@priklad.cz"
    lead = registry.leads.get(1)
    assert lead["status"] == "contacted" and lead["emails"][0]["subject"] == "Nabídka"


def test_send_refuses_second_contact_without_follow_up(smtp_config, fake_smtp):
    registry = build_registry(smtp_config)
    add_lead(registry)
    call(registry, "send_email", lead_id=1, to="info@alfa.cz", subject="A", body="x")
    with pytest.raises(ToolError, match="už byl osloven"):
        call(registry, "send_email", lead_id=1, to="info@alfa.cz", subject="B", body="x")
    call(registry, "send_email", lead_id=1, to="info@alfa.cz", subject="B", body="x", follow_up=True)
    assert fake_smtp.send_message.call_count == 2


def test_send_refuses_do_not_contact(smtp_config, fake_smtp):
    registry = build_registry(smtp_config)
    add_lead(registry, status="do_not_contact")
    add_lead(registry, company="Alfa pobočka", ico="3", email=None, website=None)
    with pytest.raises(ToolError, match="nepřeje"):
        call(registry, "send_email", lead_id=1, to="info@alfa.cz", subject="x", body="y")
    # Another address at the same company domain is suppressed too.
    with pytest.raises(ToolError, match="nepřeje"):
        call(registry, "send_email", lead_id=2, to="jan@alfa.cz", subject="x", body="y")
    fake_smtp.send_message.assert_not_called()


def test_daily_limit(smtp_config, fake_smtp):
    smtp_config.daily_email_limit = 1
    registry = build_registry(smtp_config)
    add_lead(registry)
    add_lead(registry, company="Beta", ico="2", website="beta.cz", email="a@beta.cz")
    call(registry, "send_email", lead_id=1, to="info@alfa.cz", subject="x", body="y")
    with pytest.raises(ToolError, match="limit"):
        call(registry, "send_email", lead_id=2, to="a@beta.cz", subject="x", body="y")


def test_send_requires_approval(smtp_config):
    assert build_registry(smtp_config).get("send_email").requires_approval({})


# --- ARES ----------------------------------------------------------------------

def test_ares_search_payload_and_format(config):
    registry = build_registry(config)
    reply = {"pocetCelkem": 1, "ekonomickeSubjekty": [
        {"ico": "02356571", "obchodniJmeno": "Intelligent Software s.r.o.", "pravniForma": "112",
         "datumVzniku": "2013-11-20", "sidlo": {"textovaAdresa": "Brno"}}]}
    with mock.patch.object(ares, "_request", return_value=reply) as req:
        out = call(registry, "ares_search", name="software", city="Brno", nace=["62"], limit=500)
    payload = req.call_args[0][1]
    assert payload == {"start": 0, "pocet": 50, "obchodniJmeno": "software",
                       "sidlo": {"textovaAdresa": "Brno"}, "czNace": ["62"]}
    assert "Intelligent Software s.r.o. (IČO 02356571)" in out and "s.r.o." in out


def test_ares_search_too_many_results(config):
    registry = build_registry(config)
    reply = {"kod": "CHYBA_VSTUPU", "popis": "Zadaný dotaz vrací příliš mnoho výsledků"}
    with mock.patch.object(ares, "_request", return_value=reply):
        with pytest.raises(ToolError, match="Zužte"):
            call(registry, "ares_search", nace=["62"])
    with pytest.raises(ToolError):
        call(registry, "ares_search")


def test_ares_company_validates_ico(config):
    registry = build_registry(config)
    with pytest.raises(ToolError):
        call(registry, "ares_company", ico="abc")
    with mock.patch.object(ares, "_request", return_value={"ico": "00000123", "obchodniJmeno": "X"}) as req:
        call(registry, "ares_company", ico="123")
    assert req.call_args[0][0].endswith("/00000123")


# --- system prompt ----------------------------------------------------------------

def test_system_prompt_includes_profile(config, tmp_path):
    config.profile_path = tmp_path / "profil.md"
    config.profile_path.write_text("Prodáváme {sklady} pro e-shopy.", encoding="utf-8")
    prompt = build_system_prompt(config, sending_enabled=False)
    assert "Prodáváme {sklady} pro e-shopy." in prompt
    assert "odesílání e-mailů není nastavené" in prompt


def test_system_prompt_without_profile(config, tmp_path):
    config.profile_path = tmp_path / "chybi.md"
    prompt = build_system_prompt(config, sending_enabled=True)
    assert "Profil zatím není vyplněný" in prompt
    assert "Denní limit je 20" in prompt
