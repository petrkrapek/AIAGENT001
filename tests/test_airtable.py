"""Airtable client and repositories against a fake HTTP layer (no network)."""

import io
import json
import urllib.error
import urllib.parse
from unittest import mock

import pytest

from aiagent import airtable as at
from aiagent.cli import main
from aiagent.repos import AirtableAudit, AirtableLeads, AirtableTasks, _lead_from_record, _lead_to_fields


class FakeHTTP:
    """Records requests; replies from a queue of (status, body) or a callable."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request, timeout=None):
        url = urllib.parse.urlparse(request.full_url)
        body = json.loads(request.data) if request.data else None
        self.requests.append({"method": request.get_method(), "path": urllib.parse.unquote(url.path),
                              "query": urllib.parse.parse_qs(url.query), "body": body,
                              "auth": request.get_header("Authorization")})
        status, payload = self.replies.pop(0)
        raw = json.dumps(payload).encode()
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "err", {}, io.BytesIO(raw))
        return mock.MagicMock(__enter__=lambda s: mock.MagicMock(read=lambda: raw), __exit__=lambda *a: False)


@pytest.fixture
def http():
    def install(*replies):
        fake = FakeHTTP(replies)
        patcher = mock.patch("aiagent.airtable.urllib.request.urlopen", fake)
        patcher.start()
        install.patchers.append(patcher)
        return fake
    install.patchers = []
    with mock.patch("aiagent.airtable.time.sleep"):
        yield install
    for p in install.patchers:
        p.stop()


@pytest.fixture
def client():
    return at.AirtableClient("pat123", "appTEST")


def test_esc():
    assert at.esc('Pekárna "U Nováků"') == '"Pekárna \\"U Nováků\\""'


def test_list_records_paginates_and_encodes(http, client):
    fake = http((200, {"records": [{"id": "rec1"}], "offset": "o1"}), (200, {"records": [{"id": "rec2"}]}))
    records = client.list_records("Leady", formula='{IČO} = "1"', fields=["Firma"], sort=[("Vytvořeno", "asc")])
    assert [r["id"] for r in records] == ["rec1", "rec2"]
    first, second = fake.requests
    assert first["path"] == "/v0/appTEST/Leady" and first["auth"] == "Bearer pat123"
    assert first["query"]["filterByFormula"] == ['{IČO} = "1"']
    assert first["query"]["fields[]"] == ["Firma"]
    assert first["query"]["sort[0][field]"] == ["Vytvořeno"]
    assert second["query"]["offset"] == ["o1"]


def test_retries_on_rate_limit(http, client):
    fake = http((429, {"errors": "rate"}), (200, {"records": []}))
    assert client.list_records("Leady") == []
    assert len(fake.requests) == 2


def test_errors_are_explained(http, client):
    http((404, {"error": "NOT_FOUND"}))
    with pytest.raises(at.AirtableError, match="airtable-setup"):
        client.list_records("Leady")


def test_create_uses_typecast(http, client):
    fake = http((200, {"records": [{"id": "rec1", "fields": {"Firma": "Alfa"}}]}))
    assert client.create_record("Leady", {"Firma": "Alfa"})["id"] == "rec1"
    assert fake.requests[0]["body"] == {"records": [{"fields": {"Firma": "Alfa"}}], "typecast": True}


# --- repositories ------------------------------------------------------------------

def test_lead_field_mapping():
    fields = _lead_to_fields({"company": "Alfa", "status": "do_not_contact", "website": "https://www.alfa.cz/x",
                              "draft": {"subject": "S", "body": "B", "updated_at": "2026-10-12T10:00:00+00:00"}})
    assert fields["Firma"] == "Alfa" and fields["Stav"] == "Nekontaktovat"
    assert fields["Text návrhu"] == "B" and fields["Schválení návrhu"] == "Čeká na schválení"
    lead = _lead_from_record({"id": "rec1", "fields": {**fields, "Stav": "Má zájem"}})
    assert lead["status"] == "interested" and lead["draft"]["subject"] == "S"
    assert lead["draft"]["approved"] is False
    approved = _lead_from_record({"id": "rec1", "fields": {**fields, "Schválení návrhu": "Schváleno"}})
    assert approved["draft"]["approved"] is True
    assert _lead_from_record({"id": "rec2", "fields": {}})["status"] == "new"
    # Unknown (renamed/custom) status labels fail closed.
    assert _lead_from_record({"id": "rec3", "fields": {"Stav": "Volat později"}})["status"] == "do_not_contact"
    assert _lead_to_fields({"ico": "123 45"})["IČO"] == "00012345"


def test_find_duplicate_formula(http, client):
    fake = http((200, {"records": []}))
    leads = AirtableLeads(client, "Europe/Prague")
    assert leads.find_duplicate("123", "Info@Alfa.cz", "https://www.alfa.cz:443") is None
    formula = fake.requests[0]["query"]["filterByFormula"][0]
    assert formula == 'OR({IČO} = "00000123", LOWER({E-mail}) = "info@alfa.cz", {Doména} = "alfa.cz")'


def test_suppression_and_daily_count_formulas(http, client):
    fake = http((200, {"records": [{"id": "rec1", "fields": {}}]}), (200, {"records": [{"id": "e1"}, {"id": "e2"}]}))
    leads = AirtableLeads(client, "Europe/Prague")
    assert leads.is_suppressed("jan@alfa.cz") is True
    assert leads.sent_count("2026-10-12") == 2
    suppress, count = (r["query"]["filterByFormula"][0] for r in fake.requests)
    assert suppress == ('AND({Stav} = "Nekontaktovat", OR(LOWER({E-mail}) = "jan@alfa.cz", '
                        '{Doména} = "alfa.cz"))')
    assert count == ("DATETIME_FORMAT(SET_TIMEZONE({Odesláno}, \"Europe/Prague\"), 'YYYY-MM-DD') "
                     '= "2026-10-12"')


def test_add_email_links_lead(http, client):
    fake = http((200, {"records": [{"id": "e1", "fields": {}}]}))
    lead_id = "recAAAAAAAAAAAAAA"
    AirtableLeads(client, "Europe/Prague").add_email(lead_id, "a@b.cz", "S", "B", "2026-10-12T10:00:00+00:00")
    fields = fake.requests[0]["body"]["records"][0]["fields"]
    assert fake.requests[0]["path"] == "/v0/appTEST/E-maily"
    assert fields["Lead"] == [lead_id] and fields["Lead ID"] == lead_id


def test_record_ids_are_validated_before_use_in_urls(client):
    for bad in ("5", "rec1", "recAAAAAAAAAAAAAA/../../appX/Audit/recBBBBBBBBBBBBBB", "recAAAAAAAAAAAA%2F"):
        with pytest.raises(at.AirtableError, match="Neplatné ID"):
            AirtableLeads(client, "Europe/Prague").get(bad)
        with pytest.raises(at.AirtableError, match="Neplatné ID"):
            AirtableTasks(client).update(bad, {"status": "done"})


def test_transport_errors_are_wrapped_and_reads_retried(client):
    calls = []

    def flaky(request, timeout=None):
        calls.append(request.get_method())
        raise TimeoutError("read timed out")

    with mock.patch("aiagent.airtable.urllib.request.urlopen", flaky), mock.patch("aiagent.airtable.time.sleep"):
        with pytest.raises(at.AirtableError, match="nedostupný"):
            client.list_records("Leady")
        assert len(calls) == client.MAX_RETRIES + 1
        calls.clear()
        with pytest.raises(at.AirtableError):
            client.create_record("Leady", {"Firma": "X"})
        assert calls == ["POST"]  # never repeated: it might have been stored already


def test_rate_limit_waits_30_seconds_also_for_writes(http, client):
    fake = http((429, {}), (200, {"records": [{"id": "recAAAAAAAAAAAAAA", "fields": {}}]}))
    with mock.patch("aiagent.airtable.time.sleep") as sleep:
        client.create_record("Leady", {"Firma": "X"})
    assert len(fake.requests) == 2
    assert mock.call(30) in sleep.call_args_list


def test_audit_redacts_secrets(http, client):
    fake = http((200, {"records": [{"id": "a1", "fields": {}}]}))
    AirtableAudit(client).log("http_request", {"url": "https://hook", "headers": {"Authorization": "Bearer x",
                                                                                  "X-Api-Key": "k"}}, True, False)
    stored = fake.requests[0]["body"]["records"][0]["fields"]["Vstup"]
    assert "Bearer x" not in stored and '"k"' not in stored and "https://hook" in stored


def test_tasks_and_audit(http, client):
    fake = http((200, {"records": [{"id": "t1", "fields": {"Název": "A", "Stav": "K vyřízení"}}]}),
                (200, {"records": [{"id": "a1", "fields": {}}]}))
    assert AirtableTasks(client).create("A", "2026-10-20", None)["status"] == "todo"
    AirtableAudit(client).log("save_lead", {"company": "Alfa"}, None, False)
    task_fields = fake.requests[0]["body"]["records"][0]["fields"]
    audit_fields = fake.requests[1]["body"]["records"][0]["fields"]
    assert task_fields["Termín"] == "2026-10-20" and task_fields["Stav"] == "K vyřízení"
    assert audit_fields["Schváleno"] == "nevyžadováno" and audit_fields["Výsledek"] == "ok"
    assert fake.requests[1]["path"] == "/v0/appTEST/Audit"


# --- schema setup ----------------------------------------------------------------------

def test_setup_creates_missing_tables_and_fields(http, client):
    existing = {"tables": [{"id": "tblTasks", "name": "Úkoly", "fields": [{"name": "Název"}, {"name": "Stav"}]}]}
    fake = http(
        (200, existing),
        (200, {"id": "tblProfile"}),          # create Profil firmy
        (200, {"id": "tblServices"}),         # create Služby
        (200, {"id": "tblLeads"}),            # create Leady
        (200, {"id": "tblEmails"}),           # create E-maily
        (200, {}), (200, {}), (200, {}),      # 3 missing fields in Úkoly
        (200, {"id": "tblBlocked"}),          # create Blokované kontakty
        (200, {"id": "tblAudit"}),            # create Audit
    )
    log = at.setup_base(client, "Europe/Prague")
    assert [line.split(" (")[0] for line in log[:4]] == [
        "Vytvořena tabulka Profil firmy", "Vytvořena tabulka Služby",
        "Vytvořena tabulka Leady", "Vytvořena tabulka E-maily",
    ]
    profile, services, leads, emails = (r["body"] for r in fake.requests[1:5])
    assert profile["name"] == "Profil firmy"
    assert [f["name"] for f in profile["fields"]][:3] == ["Firma", "IČO", "Web"]
    assert "Podpis" in [f["name"] for f in profile["fields"]]
    assert [f["name"] for f in services["fields"]][:3] == ["Název", "Stav", "Popis"]
    service_link = next(f for f in leads["fields"] if f["name"] == "Služba")
    assert service_link["options"] == {"linkedTableId": "tblServices"}
    lead_link = next(f for f in emails["fields"] if f["name"] == "Lead")
    assert lead_link["options"] == {"linkedTableId": "tblLeads"}
    added = [r["body"]["name"] for r in fake.requests[5:8]]
    assert added == ["Termín", "Poznámka", "Vytvořeno"]
    assert all(r["path"] == "/v0/meta/bases/appTEST/tables/tblTasks/fields" for r in fake.requests[5:8])
    assert leads["fields"][0] == {"name": "Firma", "type": "singleLineText"}
    assert "Odesílání zahájeno" in [f["name"] for f in leads["fields"]]
    blocked = fake.requests[8]["body"]
    assert blocked["name"] == "Blokované kontakty"
    assert [f["name"] for f in blocked["fields"]] == ["Název", "E-mail", "Doména", "IČO", "Důvod", "Přidáno"]


def test_setup_adds_service_link_to_existing_leads(http, client):
    leads_fields = [{"name": f["name"]} for f in at.schema("Europe/Prague")[at.LEADS] if f["name"] != "Služba"]
    others = [{"id": f"tbl{n}", "name": n, "fields": [{"name": f["name"]} for f in fields]}
              for n, fields in at.schema("Europe/Prague").items() if n not in (at.SERVICES, at.LEADS)]
    existing = {"tables": [{"id": "tblLeads", "name": "Leady", "fields": leads_fields}, *others]}
    fake = http((200, existing), (200, {"id": "tblServices"}), (200, {}))
    log = at.setup_base(client, "Europe/Prague")
    assert fake.requests[2]["body"] == {"name": "Služba", "type": "multipleRecordLinks",
                                        "options": {"linkedTableId": "tblServices"}}
    assert any("doplněno 1 polí (Služba)" in line for line in log)


def test_active_services(http, client):
    from aiagent.repos import AirtableServices
    fake = http((200, {"records": [
        {"id": "recSERVICE0000001",
         "fields": {"Název": "Audit skladu", "Popis": "Projdeme sklad", "Cena": "od 15 000 Kč"}},
        {"id": "recS2", "fields": {"Popis": "bez názvu se ignoruje"}},
    ]}))
    services = AirtableServices(client).list_active()
    assert services == [{"id": "recSERVICE0000001", "name": "Audit skladu", "description": "Projdeme sklad",
                         "target": None, "benefits": None, "price": "od 15 000 Kč",
                         "references": None, "url": None, "instructions": None}]
    assert fake.requests[0]["path"] == "/v0/appTEST/Služby"
    assert fake.requests[0]["query"]["filterByFormula"] == ['{Stav} = "Aktivní"']


def test_lead_service_link_mapping():
    assert _lead_to_fields({"service_id": "recSERVICE0000001"}) == {"Služba": ["recSERVICE0000001"]}
    lead = _lead_from_record({"id": "rec1", "fields": {"Služba": ["recSERVICE0000001"]}})
    assert lead["service_id"] == "recSERVICE0000001"


# --- CLI ---------------------------------------------------------------------------------

def test_cli_requires_airtable(monkeypatch, tmp_path, capsys):
    for key in ("AIRTABLE_TOKEN", "AIRTABLE_BASE_ID", "SMTP_HOST", "AIAGENT_SEND_MODE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    assert main(["run", "ahoj"]) == 2
    assert "AIRTABLE_TOKEN" in capsys.readouterr().err


class StatefulAirtable:
    """Just enough of Airtable's record API to round-trip data through the repos (formulas ignored)."""

    def __init__(self):
        self.tables = {}
        self.n = 0

    def __call__(self, request, timeout=None):
        url = urllib.parse.urlparse(request.full_url)
        parts = urllib.parse.unquote(url.path).split("/")[3:]  # ['Leady'] or ['Leady', 'recX']
        table = self.tables.setdefault(parts[0], {})
        body = json.loads(request.data) if request.data else None
        method = request.get_method()
        if method == "POST":
            self.n += 1
            record = {"id": f"rec{self.n:014d}", "fields": {k: v for k, v in body["records"][0]["fields"].items()
                                                         if v is not None}}
            table[record["id"]] = record
            payload = {"records": [record]}
        elif method == "PATCH":
            record = table[parts[1]]
            for k, v in body["fields"].items():
                if v is None:
                    record["fields"].pop(k, None)
                else:
                    record["fields"][k] = v
            payload = record
        elif len(parts) == 2:
            payload = table[parts[1]]
        else:
            payload = {"records": list(table.values())}
        raw = json.dumps(payload).encode()
        return mock.MagicMock(__enter__=lambda s: mock.MagicMock(read=lambda: raw), __exit__=lambda *a: False)


def test_tools_round_trip_through_airtable(config):
    from aiagent.repos import airtable_repos
    from aiagent.tools import ToolError, build_registry
    from conftest import call

    server = StatefulAirtable()
    with mock.patch("aiagent.airtable.urllib.request.urlopen", server), \
            mock.patch("aiagent.airtable.time.sleep"):
        client = at.AirtableClient("pat", "appTEST")
        client.MIN_INTERVAL = 0
        registry = build_registry(config, airtable_repos(client, "Europe/Prague"))
        out = call(registry, "save_lead", company="Alfa s.r.o.", website="https://www.alfa.cz",
                   email="info@alfa.cz", email_source="https://alfa.cz/kontakt", status="qualified")
        assert "Uloženo: rec00000000000001 [qualified] Alfa s.r.o." in out
        call(registry, "save_draft", id="rec00000000000001", subject="Nabídka", body="Dobrý den",
             service_id="recSERVICE0000001")
        fields = server.tables["Leady"]["rec00000000000001"]["fields"]
        assert fields["Služba"] == ["recSERVICE0000001"]
        assert fields["Stav"] == "Návrh připraven" and fields["Text návrhu"] == "Dobrý den"
        assert fields["Doména"] == "alfa.cz"
        assert "Dobrý den" in call(registry, "get_lead", id="rec00000000000001")
        call(registry, "update_lead", id="rec00000000000001", status="do_not_contact", notes="Odhlásil se")
        assert server.tables["Leady"]["rec00000000000001"]["fields"]["Stav"] == "Nekontaktovat"
        blocked = server.tables["Blokované kontakty"]["rec00000000000002"]["fields"]
        assert (blocked["Název"], blocked["E-mail"], blocked["Doména"]) == ("Alfa s.r.o.", "info@alfa.cz", "alfa.cz")
        with pytest.raises(ToolError, match="Blokované kontakty"):
            call(registry, "save_lead", company="Alfa pobočka", email="jan@alfa.cz",
                 email_source="https://alfa.cz/kontakt")
        call(registry, "add_task", title="Zavolat", due="2026-10-20")
        assert server.tables["Úkoly"]["rec00000000000003"]["fields"]["Stav"] == "K vyřízení"


def test_profile_reads_first_filled_row(http, client):
    from aiagent.repos import AirtableProfile
    fake = http((200, {"records": [
        {"id": "rec1", "fields": {}},
        {"id": "rec2", "fields": {"Firma": "Příklad s.r.o.", "Tón e-mailů": "Vykání, věcně."}},
    ]}))
    text = AirtableProfile(client).load()
    assert text == "### Firma\nPříklad s.r.o.\n\n### Tón e-mailů\nVykání, věcně."
    assert fake.requests[0]["path"] == "/v0/appTEST/Profil firmy"


def test_profile_missing(http, client):
    from aiagent.repos import AirtableProfile
    http((200, {"records": []}))
    assert AirtableProfile(client).load() is None


def test_search_and_counts(http, client):
    fake = http(
        (200, {"records": [{"id": "rec1", "fields": {"Firma": "Alfa", "Stav": "Kvalifikovaný"}}]}),
        (200, {"records": [{"id": "rec1", "fields": {"Stav": "Kvalifikovaný"}},
                           {"id": "rec2", "fields": {"Stav": "Nekontaktovat"}}, {"id": "rec3", "fields": {}}]}),
    )
    leads = AirtableLeads(client, "Europe/Prague")
    assert [lead["company"] for lead in leads.search(status="qualified", query="Brno")] == ["Alfa"]
    formula = fake.requests[0]["query"]["filterByFormula"][0]
    assert formula.startswith('AND({Stav} = "Kvalifikovaný", SEARCH("brno", LOWER({Firma} & \' \' & {Město}')
    assert leads.status_counts() == {"qualified": 1, "do_not_contact": 1, "new": 1}


def test_tasks_list_and_missing_task(http, client):
    fake = http((200, {"records": [{"id": "t1", "fields": {"Název": "A", "Stav": "Probíhá"}}]}),
                (404, {"error": "NOT_FOUND"}))
    tasks = AirtableTasks(client)
    assert tasks.list()[0]["status"] == "in_progress"
    assert fake.requests[0]["query"]["filterByFormula"] == ['AND({Stav} != "Hotovo", {Stav} != "Zrušeno")']
    from aiagent.tools import ToolError
    with pytest.raises(ToolError, match="neexistuje"):
        tasks.update("recAAAAAAAAAAAAAA", {"status": "done"})


def test_blocklist_rows_are_normalized(http, client):
    from aiagent.repos import AirtableBlocklist, blocked_entry, find_blocked
    fake = http(
        (200, {"records": [{"id": "rec1", "fields": {"Název": "Konkurence", "Doména": "https://www.Konkurence.cz/",
                                                     "IČO": "123 45 678", "Důvod": "konkurent"}}]}),
        (200, {"records": [{"id": "rec2", "fields": {}}]}),
    )
    blocklist = AirtableBlocklist(client)
    entries = blocklist.entries()
    assert entries == [{"name": "Konkurence", "email": "", "domain": "konkurence.cz", "ico": "12345678",
                        "reason": "konkurent"}]
    assert fake.requests[0]["path"] == "/v0/appTEST/Blokované kontakty"
    assert find_blocked(entries, website="https://eshop.konkurence.cz") is entries[0]
    assert find_blocked(entries, ico="12345678") is entries[0]
    assert find_blocked(entries, email="info@nekonkurence.cz") is None
    blocklist.add(blocked_entry("Alfa", "Info@Alfa.cz", None, None, "odhlášení"))
    fields = fake.requests[1]["body"]["records"][0]["fields"]
    assert fields["E-mail"] == "info@alfa.cz" and fields["Doména"] is None and fields["Přidáno"]
