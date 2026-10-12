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
    assert fields["Doména"] == "alfa.cz" and fields["Text návrhu"] == "B"
    lead = _lead_from_record({"id": "rec1", "fields": {**fields, "Stav": "Má zájem"}})
    assert lead["status"] == "interested" and lead["draft"]["subject"] == "S"
    assert _lead_from_record({"id": "rec2", "fields": {}})["status"] == "new"


def test_find_duplicate_formula(http, client):
    fake = http((200, {"records": []}))
    assert AirtableLeads(client, "Europe/Prague").find_duplicate("123", "Info@Alfa.cz", "https://www.alfa.cz") is None
    formula = fake.requests[0]["query"]["filterByFormula"][0]
    assert formula == 'OR({IČO} = "123", LOWER({E-mail}) = "info@alfa.cz", {Doména} = "alfa.cz")'


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
    AirtableLeads(client, "Europe/Prague").add_email("rec1", "a@b.cz", "S", "B", "2026-10-12T10:00:00+00:00")
    fields = fake.requests[0]["body"]["records"][0]["fields"]
    assert fake.requests[0]["path"] == "/v0/appTEST/E-maily"
    assert fields["Lead"] == ["rec1"] and fields["Lead ID"] == "rec1"


def test_get_rejects_non_airtable_id(client):
    from aiagent.tools import ToolError
    with pytest.raises(ToolError, match="rec"):
        AirtableLeads(client, "Europe/Prague").get("5")


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
        (200, {"id": "tblLeads"}),            # create Leady
        (200, {"id": "tblEmails"}),           # create E-maily
        (200, {}), (200, {}), (200, {}),      # 3 missing fields in Úkoly
        (200, {"id": "tblAudit"}),            # create Audit
    )
    log = at.setup_base(client, "Europe/Prague")
    assert log[0].startswith("Vytvořena tabulka Leady")
    emails_req = fake.requests[2]
    link = next(f for f in emails_req["body"]["fields"] if f["name"] == "Lead")
    assert link["options"] == {"linkedTableId": "tblLeads"}
    added = [r["body"]["name"] for r in fake.requests[3:6]]
    assert added == ["Termín", "Poznámka", "Vytvořeno"]
    assert all(r["path"] == "/v0/meta/bases/appTEST/tables/tblTasks/fields" for r in fake.requests[3:6])
    leads_fields = fake.requests[1]["body"]["fields"]
    assert leads_fields[0] == {"name": "Firma", "type": "singleLineText"}


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
            record = {"id": f"rec{self.n}", "fields": {k: v for k, v in body["records"][0]["fields"].items()
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
    from aiagent.tools import build_registry
    from conftest import call

    server = StatefulAirtable()
    with mock.patch("aiagent.airtable.urllib.request.urlopen", server), \
            mock.patch("aiagent.airtable.time.sleep"):
        client = at.AirtableClient("pat", "appTEST")
        client.MIN_INTERVAL = 0
        registry = build_registry(config, airtable_repos(client, "Europe/Prague"))
        out = call(registry, "save_lead", company="Alfa s.r.o.", website="https://www.alfa.cz",
                   email="info@alfa.cz", email_source="https://alfa.cz/kontakt", status="qualified")
        assert "Uloženo: rec1 [qualified] Alfa s.r.o." in out
        call(registry, "save_draft", id="rec1", subject="Nabídka", body="Dobrý den")
        fields = server.tables["Leady"]["rec1"]["fields"]
        assert fields["Stav"] == "Návrh připraven" and fields["Text návrhu"] == "Dobrý den"
        assert fields["Doména"] == "alfa.cz"
        assert "Dobrý den" in call(registry, "get_lead", id="rec1")
        call(registry, "update_lead", id="rec1", status="do_not_contact", notes="Odhlásil se")
        assert server.tables["Leady"]["rec1"]["fields"]["Stav"] == "Nekontaktovat"
        call(registry, "add_task", title="Zavolat", due="2026-10-20")
        assert server.tables["Úkoly"]["rec2"]["fields"]["Stav"] == "K vyřízení"
