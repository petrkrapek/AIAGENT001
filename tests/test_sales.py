from unittest import mock

import pytest

from aiagent.agent import build_system_prompt
from aiagent.config import SmtpConfig
from aiagent.tools import ToolError, ares, build_registry
from conftest import call, rid


@pytest.fixture
def smtp_config(config):
    config.smtp = SmtpConfig(host="smtp.example.com", sender="obchod@priklad.cz")
    config.send_mode = "approve"
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

def test_save_lead_and_deduplicate(registry):
    assert "Uloženo: rec00000000000001" in add_lead(registry)
    assert "už existuje" in add_lead(registry, ico=None, email=None, website="http://alfa.cz/o-nas")
    assert "už existuje" in add_lead(registry, ico="2", email="INFO@alfa.cz", website=None)
    assert "Uloženo: rec00000000000002" in add_lead(registry, company="Beta", ico="22222222",
                                           website="beta.cz", email="obchod@beta.cz")


def test_email_requires_source(registry):
    with pytest.raises(ToolError):
        call(registry, "save_lead", company="X", email="a@x.cz")


def test_update_lead_validation(registry):
    add_lead(registry)
    assert "[do_not_contact]" in call(registry, "update_lead", id="rec00000000000001", status="do_not_contact")
    with pytest.raises(ToolError):
        call(registry, "update_lead", id="rec00000000000001", status="hotovo")
    with pytest.raises(ToolError):
        call(registry, "update_lead", id="rec99999999999999", notes="x")


def test_save_draft(registry, repos):
    add_lead(registry)
    out = call(registry, "save_draft", id="rec00000000000001", subject="Spolupráce", body="Dobrý den, ...")
    assert "Airtable" in out
    lead = repos.leads.get("rec00000000000001")
    assert lead["status"] == "drafted" and lead["draft"]["subject"] == "Spolupráce"
    assert "Dobrý den" in call(registry, "get_lead", id="rec00000000000001")


def test_no_draft_for_do_not_contact(registry):
    add_lead(registry, status="do_not_contact")
    with pytest.raises(ToolError):
        call(registry, "save_draft", id="rec00000000000001", subject="x", body="y")


def test_list_leads_filters(registry):
    add_lead(registry, city="Brno")
    add_lead(registry, company="Beta", ico="2", website="beta.cz", email=None, city="Praha", status="new")
    out = call(registry, "list_leads", query="brno")
    assert "Alfa" in out and "Beta" not in out
    assert "Beta" in call(registry, "list_leads", status="new")
    assert "Celkem podle stavu: qualified 1, new 1" in out


# --- sending ---------------------------------------------------------------

def test_send_email_records_history(smtp_config, repos, fake_smtp):
    registry = build_registry(smtp_config, repos)
    add_lead(registry)
    out = call(registry, "send_email", lead_id="rec00000000000001", to="info@alfa.cz", subject="Nabídka", body="Text")
    assert "odeslán" in out
    sent = fake_smtp.send_message.call_args[0][0]
    assert sent["To"] == "info@alfa.cz" and sent["From"] == "obchod@priklad.cz"
    lead = repos.leads.get("rec00000000000001")
    assert lead["status"] == "contacted" and lead["last_contacted"]
    assert repos.leads.emails("rec00000000000001")[0]["subject"] == "Nabídka"
    assert "odesláno" in call(registry, "get_lead", id="rec00000000000001")


def test_send_refuses_second_contact_without_follow_up(smtp_config, repos, fake_smtp):
    registry = build_registry(smtp_config, repos)
    add_lead(registry)
    call(registry, "send_email", lead_id=rid(1), to="info@alfa.cz", subject="A", body="x")
    with pytest.raises(ToolError, match="v tomto běhu"):
        call(registry, "send_email", lead_id=rid(1), to="info@alfa.cz", subject="B", body="x", follow_up=True)
    # A later session: needs an explicit follow_up.
    registry = build_registry(smtp_config, repos)
    with pytest.raises(ToolError, match="už byl osloven"):
        call(registry, "send_email", lead_id=rid(1), to="info@alfa.cz", subject="B", body="x")
    call(registry, "send_email", lead_id=rid(1), to="info@alfa.cz", subject="B", body="x", follow_up=True)
    assert fake_smtp.send_message.call_count == 2


def test_send_refuses_do_not_contact(smtp_config, repos, fake_smtp):
    registry = build_registry(smtp_config, repos)
    add_lead(registry, status="do_not_contact")
    # Added by hand in Airtable (save_lead would reject it as a duplicate of the same domain).
    repos.leads.create({"company": "Alfa pobočka", "email": "jan@alfa.cz", "status": "qualified"})
    with pytest.raises(ToolError, match="nepřeje"):
        call(registry, "send_email", lead_id=rid(1), to="info@alfa.cz", subject="x", body="y")
    # Another address at the same company domain is suppressed too.
    with pytest.raises(ToolError, match="nepřeje"):
        call(registry, "send_email", lead_id=rid(2), to="jan@alfa.cz", subject="x", body="y")
    fake_smtp.send_message.assert_not_called()


def test_email_only_opt_out_blocks_company_domain(smtp_config, repos, fake_smtp):
    registry = build_registry(smtp_config, repos)
    add_lead(registry, company="Gama", ico="5", website=None, email="info@gama.cz", status="do_not_contact")
    with pytest.raises(ToolError, match="Blokované kontakty"):
        add_lead(registry, company="Gama obchod", ico="6", website=None, email="obchod@gama.cz")
    repos.leads.create({"company": "Gama obchod", "email": "obchod@gama.cz", "status": "qualified"})
    with pytest.raises(ToolError, match="nepřeje"):
        call(registry, "send_email", lead_id=rid(2), to="obchod@gama.cz", subject="x", body="y")


def test_freemail_domain_is_not_a_company(registry):
    add_lead(registry, company="Pekař", ico="7", website=None, email="pekar@seznam.cz")
    out = add_lead(registry, company="Řezník", ico="8", website=None, email="reznik@seznam.cz",
                   email_source="https://reznik.cz")
    assert out.startswith("Uloženo")


def test_ico_is_normalized_for_duplicates(registry):
    add_lead(registry, ico="123 45 678")
    assert "už existuje" in add_lead(registry, company="X", ico="12345678", website=None, email=None)
    add_lead(registry, company="Y", ico="12345", website="y.cz", email=None)
    assert "už existuje" in add_lead(registry, company="Y2", ico="00012345", website=None, email=None)


def test_send_only_to_the_leads_address(smtp_config, repos, fake_smtp):
    registry = build_registry(smtp_config, repos)
    add_lead(registry)
    for to in ("utocnik@evil.com", "info@alfa.cz, dalsi@firma.cz", "info@alfa.cz; x@y.cz"):
        with pytest.raises(ToolError):
            call(registry, "send_email", lead_id=rid(1), to=to, subject="x", body="y")
    fake_smtp.send_message.assert_not_called()
    call(registry, "send_email", lead_id=rid(1), to="Alfa <INFO@alfa.cz>", subject="x", body="y")
    assert fake_smtp.send_message.call_args[0][0]["To"] == "info@alfa.cz"


def test_not_interested_is_not_contacted(smtp_config, repos, fake_smtp):
    registry = build_registry(smtp_config, repos)
    add_lead(registry, status="not_interested")
    with pytest.raises(ToolError, match="nepřeje"):
        call(registry, "send_email", lead_id=rid(1), to="info@alfa.cz", subject="x", body="y")


def test_sent_email_is_not_resent_when_recording_fails(smtp_config, repos, fake_smtp):
    registry = build_registry(smtp_config, repos)
    add_lead(registry)
    with mock.patch.object(repos.leads, "add_email", side_effect=RuntimeError("Airtable 503")):
        out = call(registry, "send_email", lead_id=rid(1), to="info@alfa.cz", subject="x", body="y")
    assert "BYL odeslán" in out and "Neodesílej" in out
    with pytest.raises(ToolError, match="v tomto běhu"):
        call(registry, "send_email", lead_id=rid(1), to="info@alfa.cz", subject="x", body="y")
    # A later run (cron) doesn't know about this process: the mark in Airtable blocks the lead.
    registry = build_registry(smtp_config, repos)
    with pytest.raises(ToolError, match="nebylo potvrzeno"):
        call(registry, "send_email", lead_id=rid(1), to="info@alfa.cz", subject="x", body="y")
    assert fake_smtp.send_message.call_count == 1
    assert "ODESLÁNÍ NEPOTVRZENO" in call(registry, "list_leads")


def test_no_send_without_the_mark(smtp_config, repos, fake_smtp):
    registry = build_registry(smtp_config, repos)
    add_lead(registry)
    with mock.patch.object(repos.leads, "update", side_effect=RuntimeError("Airtable 503")):
        with pytest.raises(ToolError, match="neodeslán"):
            call(registry, "send_email", lead_id=rid(1), to="info@alfa.cz", subject="x", body="y")
    fake_smtp.send_message.assert_not_called()


def test_successful_send_clears_the_mark(smtp_config, repos, fake_smtp):
    registry = build_registry(smtp_config, repos)
    add_lead(registry)
    call(registry, "send_email", lead_id=rid(1), to="info@alfa.cz", subject="x", body="y")
    assert repos.leads.get(rid(1))["sending_started"] is None


def test_refused_send_clears_the_mark(smtp_config, repos, fake_smtp):
    import smtplib
    registry = build_registry(smtp_config, repos)
    add_lead(registry)
    fake_smtp.send_message.side_effect = smtplib.SMTPRecipientsRefused({"info@alfa.cz": (550, b"no")})
    with pytest.raises(ToolError, match="selhalo"):
        call(registry, "send_email", lead_id=rid(1), to="info@alfa.cz", subject="x", body="y")
    lead = repos.leads.get(rid(1))
    assert lead["sending_started"] is None and lead["status"] == "qualified"


def test_ambiguous_send_keeps_the_lead_blocked(smtp_config, repos, fake_smtp):
    import smtplib
    registry = build_registry(smtp_config, repos)
    add_lead(registry)
    fake_smtp.send_message.side_effect = smtplib.SMTPServerDisconnected("spojení ukončeno")
    with pytest.raises(ToolError, match="Odeslané"):
        call(registry, "send_email", lead_id=rid(1), to="info@alfa.cz", subject="x", body="y")
    assert repos.leads.get(rid(1))["sending_started"]
    fake_smtp.send_message.side_effect = None
    with pytest.raises(ToolError, match="nebylo potvrzeno"):
        call(build_registry(smtp_config, repos), "send_email", lead_id=rid(1), to="info@alfa.cz",
             subject="x", body="y")
    assert fake_smtp.send_message.call_count == 1


def test_failing_quit_after_accepted_message_counts_as_sent(smtp_config, repos):
    import smtplib
    registry = build_registry(smtp_config, repos)
    add_lead(registry)
    with mock.patch("aiagent.tools.mail.smtplib.SMTP") as smtp_cls:
        smtp_cls.return_value.__exit__.side_effect = smtplib.SMTPResponseException(421, b"bye")
        assert "odeslán" in call(registry, "send_email", lead_id=rid(1), to="info@alfa.cz", subject="x", body="y")
    assert repos.leads.get(rid(1))["status"] == "contacted"


def test_opt_out_can_only_be_lifted_by_a_person(registry):
    add_lead(registry, status="do_not_contact")
    with pytest.raises(ToolError, match="jen uživatel"):
        call(registry, "update_lead", id=rid(1), status="qualified")
    assert "Upraveno" in call(registry, "update_lead", id=rid(1), notes="Odhlásil se 12. 10.")


# --- blocked contacts ----------------------------------------------------------------

def test_blocklist_entries_typed_by_a_person(smtp_config, repos, fake_smtp):
    registry = build_registry(smtp_config, repos)
    repos.leads.create({"company": "Konkurence", "website": "konkurence.cz", "email": "info@konkurence.cz",
                        "status": "qualified"})
    repos.blocklist.block(name="Konkurence", domain="https://www.Konkurence.cz/", reason="konkurent")
    repos.blocklist.block(name="Stálý zákazník", ico="123 45 678")
    for kwargs in ({"website": "https://eshop.konkurence.cz"}, {"email": "obchod@konkurence.cz"},
                   {"ico": "12345678", "website": "zakaznik.cz", "email": None}):
        with pytest.raises(ToolError, match="Blokované kontakty"):
            add_lead(registry, company="X", **{"ico": None, **kwargs})
    # Leads saved before the row was added can't get a draft or an e-mail either.
    with pytest.raises(ToolError, match="konkurent"):
        call(registry, "save_draft", id=rid(1), subject="x", body="y")
    with pytest.raises(ToolError, match="Blokované kontakty"):
        call(registry, "send_email", lead_id=rid(1), to="info@konkurence.cz", subject="x", body="y")
    fake_smtp.send_message.assert_not_called()
    assert add_lead(registry).startswith("Uloženo")


def test_opt_out_survives_deleting_the_lead(smtp_config, repos, fake_smtp):
    registry = build_registry(smtp_config, repos)
    add_lead(registry)
    call(registry, "update_lead", id=rid(1), status="do_not_contact")
    assert [r["email"] for r in repos.blocklist.rows] == ["info@alfa.cz"]
    del repos.leads.leads[rid(1)]  # someone deletes the row in Airtable
    with pytest.raises(ToolError, match="Blokované kontakty"):
        add_lead(registry, email="jan@alfa.cz")
    repos.leads.create({"company": "Alfa", "email": "jan@alfa.cz", "status": "qualified"})
    with pytest.raises(ToolError, match="Blokované kontakty"):
        call(registry, "send_email", lead_id=rid(2), to="jan@alfa.cz", subject="x", body="y")
    fake_smtp.send_message.assert_not_called()
    call(registry, "update_lead", id=rid(2), status="do_not_contact")
    assert len(repos.blocklist.rows) == 1  # already covered by the company domain


def test_freemail_opt_out_blocks_only_that_address(registry, repos):
    add_lead(registry, company="Pekař", ico=None, website=None, email="pekar@seznam.cz", status="do_not_contact")
    assert repos.blocklist.entries()[0]["domain"] == ""
    assert add_lead(registry, company="Řezník", ico=None, website=None, email="reznik@seznam.cz").startswith("Uloženo")


def test_failed_blocklist_write_is_reported(registry, repos):
    add_lead(registry)
    with mock.patch.object(repos.blocklist, "add", side_effect=RuntimeError("Airtable 503")):
        out = call(registry, "update_lead", id=rid(1), status="do_not_contact")
    assert "[do_not_contact]" in out and "Blokované kontakty selhal" in out


def test_sync_copies_opt_outs_set_by_hand(repos):
    from aiagent.repos import sync_blocklist
    repos.leads.create({"company": "Alfa", "email": "info@alfa.cz", "website": "alfa.cz", "status": "do_not_contact"})
    repos.leads.create({"company": "Beta", "email": "a@beta.cz", "status": "qualified"})
    repos.leads.create({"company": "Gama", "ico": "5", "status": "do_not_contact"})
    assert sync_blocklist(repos.leads, repos.blocklist) == 2
    assert sync_blocklist(repos.leads, repos.blocklist) == 0
    assert {e["name"] for e in repos.blocklist.entries()} == {"Alfa", "Gama"}


def test_daily_limit(smtp_config, repos, fake_smtp):
    smtp_config.daily_email_limit = 1
    registry = build_registry(smtp_config, repos)
    add_lead(registry)
    add_lead(registry, company="Beta", ico="2", website="beta.cz", email="a@beta.cz")
    call(registry, "send_email", lead_id="rec00000000000001", to="info@alfa.cz", subject="x", body="y")
    with pytest.raises(ToolError, match="limit"):
        call(registry, "send_email", lead_id="rec00000000000002", to="a@beta.cz", subject="x", body="y")


def test_send_requires_approval(smtp_config, repos):
    assert build_registry(smtp_config, repos).get("send_email").requires_approval({})


# --- ARES ----------------------------------------------------------------------

def test_ares_search_payload_and_format(registry):
    reply = {"pocetCelkem": 1, "ekonomickeSubjekty": [
        {"ico": "02356571", "obchodniJmeno": "Intelligent Software s.r.o.", "pravniForma": "112",
         "datumVzniku": "2013-11-20", "sidlo": {"textovaAdresa": "Brno"}}]}
    with mock.patch.object(ares, "_request", return_value=reply) as req:
        out = call(registry, "ares_search", name="software", city="Brno", nace=["62"], limit=500)
    payload = req.call_args[0][1]
    assert payload == {"start": 0, "pocet": 50, "obchodniJmeno": "software",
                       "sidlo": {"textovaAdresa": "Brno"}, "czNace": ["62"]}
    assert "Intelligent Software s.r.o. (IČO 02356571)" in out and "s.r.o." in out


def test_ares_search_too_many_results(registry):
    reply = {"kod": "CHYBA_VSTUPU", "popis": "Zadaný dotaz vrací příliš mnoho výsledků"}
    with mock.patch.object(ares, "_request", return_value=reply):
        with pytest.raises(ToolError, match="Zužte"):
            call(registry, "ares_search", nace=["62"])
    with pytest.raises(ToolError):
        call(registry, "ares_search")


def test_ares_company_validates_ico(registry):
    with pytest.raises(ToolError):
        call(registry, "ares_company", ico="abc")
    with mock.patch.object(ares, "_request", return_value={"ico": "00000123", "obchodniJmeno": "X"}) as req:
        call(registry, "ares_company", ico="123")
    assert req.call_args[0][0].endswith("/00000123")


# --- system prompt ----------------------------------------------------------------

def test_system_prompt_includes_profile(config):
    prompt = build_system_prompt(config, sending_enabled=False, profile="Prodáváme {sklady} pro e-shopy.")
    assert "Prodáváme {sklady} pro e-shopy." in prompt
    assert "odesílání e-mailů je vypnuté" in prompt


def test_system_prompt_without_profile(config):
    prompt = build_system_prompt(config, sending_enabled=True)
    assert "Profil firmy zatím není vyplněný (Airtable" in prompt
    assert "Denní limit je 20" in prompt


def test_profile_formatting_and_agent_loading(config, repos):
    from aiagent.agent import Agent
    from aiagent.repos import format_profile
    from memory_repos import MemoryProfile

    assert format_profile({}) is None and format_profile({"Firma": "  "}) is None
    repos.profile = MemoryProfile({"Firma": "Příklad s.r.o.", "Podpis": "Jan Novák\nPříklad s.r.o.",
                                   "Neznámé pole": "ignoruje se"})
    agent = Agent(config, build_registry(config, repos), client=object(), load_profile=repos.profile.load)
    assert "### Firma\nPříklad s.r.o." in agent.system and "### Podpis\nJan Novák" in agent.system
    assert "Neznámé pole" not in agent.system
    repos.profile.fields["Firma"] = "Nový název s.r.o."
    agent.reset()
    assert "Nový název s.r.o." in agent.system


# --- services ------------------------------------------------------------------------

SERVICES = [
    {"id": "recSERVICE0000001", "name": "Audit skladu", "description": "Projdeme sklad a navrhneme úspory.",
     "target": "E-shopy s vlastním skladem", "benefits": "Rychlejší expedice", "price": "od 15 000 Kč",
     "references": "Firma X: -40 % času expedice", "url": "https://priklad.cz/audit",
     "instructions": "Nezmiňuj cenu v prvním e-mailu."},
]


def test_system_prompt_lists_services(config):
    prompt = build_system_prompt(config, sending_enabled=False, services=SERVICES)
    assert "### Audit skladu (service_id: recSERVICE0000001)" in prompt
    assert "- Pro koho: E-shopy s vlastním skladem" in prompt
    assert "- Pokyny pro agenta: Nezmiňuj cenu v prvním e-mailu." in prompt
    assert "Nabízej jen tyto služby" in prompt


def test_system_prompt_without_services(config):
    assert "není žádná aktivní služba" in build_system_prompt(config, sending_enabled=False, services=[])


def test_draft_records_offered_service(registry, repos):
    add_lead(registry)
    call(registry, "save_draft", id="rec00000000000001", subject="S", body="B", service_id="recSERVICE0000001")
    assert repos.leads.get("rec00000000000001")["service_id"] == "recSERVICE0000001"
    assert "service_id: recSERVICE0000001" in call(registry, "get_lead", id="rec00000000000001")
    with pytest.raises(ToolError, match="service_id"):
        call(registry, "save_draft", id="rec00000000000001", subject="S", body="B", service_id="Audit skladu")


def test_agent_loads_and_reloads_services(config, repos):
    from aiagent.agent import Agent
    calls = []

    def loader():
        calls.append(1)
        return SERVICES if len(calls) > 1 else []

    agent = Agent(config, build_registry(config, repos), client=object(), load_services=loader)
    assert "není žádná aktivní služba" in agent.system
    agent.reset()
    assert "Audit skladu" in agent.system and len(calls) == 2


def test_agent_survives_service_load_failure(config, repos):
    from aiagent.agent import Agent, AgentEvents

    class Rec(AgentEvents):
        notices = []

        def on_notice(self, text):
            self.notices.append(text)

    def broken():
        raise RuntimeError("Airtable nedostupný")

    events = Rec()
    agent = Agent(config, build_registry(config, repos), client=object(), load_services=broken, events=events)
    assert "není žádná aktivní služba" in agent.system
    assert "Airtable nedostupný" in events.notices[0]
