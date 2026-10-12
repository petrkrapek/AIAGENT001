"""Minimal Airtable REST client and the base schema the agent uses."""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API_URL = "https://api.airtable.com/v0"


class AirtableError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


RECORD_ID = re.compile(r"^rec[A-Za-z0-9]{14}$")


def check_record_id(record_id: str) -> str:
    """Record ids end up in URL paths; accept only the real Airtable format."""
    if not isinstance(record_id, str) or not RECORD_ID.match(record_id):
        raise AirtableError(f"Neplatné ID záznamu '{record_id}' (Airtable ID má tvar recXXXXXXXXXXXXXX).",
                            status=400)
    return record_id


def esc(value: str) -> str:
    """Quote a value for use inside an Airtable formula."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


class AirtableClient:
    # Airtable allows 5 requests per second per base and asks to wait 30 s after a 429.
    MIN_INTERVAL = 0.22
    MAX_RETRIES = 3
    RATE_LIMIT_WAIT = 30

    def __init__(self, token: str, base_id: str, api_url: str = API_URL):
        self.token = token
        self.base_id = base_id
        self.api_url = api_url.rstrip("/")
        self._last_request = 0.0

    # --- transport -------------------------------------------------------------

    def request(self, method: str, path: str, params: list[tuple[str, str]] | None = None,
                body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.api_url}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

        # Only reads are safe to repeat after an ambiguous failure; a repeated POST/PATCH
        # could create duplicates. A 429 means the request was not processed at all.
        idempotent = method == "GET"
        for attempt in range(self.MAX_RETRIES + 1):
            wait = self.MIN_INTERVAL - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            request = urllib.request.Request(url, data=data, method=method, headers=headers)
            last = attempt == self.MAX_RETRIES
            try:
                with urllib.request.urlopen(request, timeout=30) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and not last:
                    time.sleep(self.RATE_LIMIT_WAIT)
                    continue
                if exc.code >= 500 and idempotent and not last:
                    time.sleep(2 ** attempt)
                    continue
                raise AirtableError(self._describe(exc), status=exc.code) from exc
            except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
                # URLError, timeouts, dropped connections, malformed responses.
                if idempotent and not last:
                    time.sleep(2 ** attempt)
                    continue
                reason = getattr(exc, "reason", exc)
                raise AirtableError(f"Airtable je nedostupný nebo neodpověděl: {reason}") from exc
        raise AirtableError("Airtable neodpovídá.")  # pragma: no cover

    @staticmethod
    def _describe(exc: urllib.error.HTTPError) -> str:
        try:
            error = json.loads(exc.read().decode("utf-8")).get("error", {})
        except (ValueError, AttributeError):
            error = {}
        if isinstance(error, dict):
            detail = error.get("message") or error.get("type") or ""
        else:
            detail = str(error)
        hints = {
            401: "Neplatný AIRTABLE_TOKEN.",
            403: "Token nemá přístup k této bázi nebo potřebné oprávnění (scopes).",
            404: "Báze nebo tabulka nenalezena. Zkontrolujte AIRTABLE_BASE_ID a spusťte airtable-setup.",
            422: "Airtable odmítl data (chybí pole? spusťte airtable-setup).",
        }
        return f"Airtable chyba {exc.code}: {hints.get(exc.code, '')} {detail}".strip()

    # --- records ----------------------------------------------------------------

    def _table_path(self, table: str) -> str:
        return f"{self.base_id}/{urllib.parse.quote(table, safe='')}"

    def list_records(self, table: str, formula: str | None = None, fields: list[str] | None = None,
                     sort: list[tuple[str, str]] | None = None, max_records: int | None = None
                     ) -> list[dict[str, Any]]:
        params: list[tuple[str, str]] = [("pageSize", "100")]
        if formula:
            params.append(("filterByFormula", formula))
        for name in fields or []:
            params.append(("fields[]", name))
        for i, (field, direction) in enumerate(sort or []):
            params += [(f"sort[{i}][field]", field), (f"sort[{i}][direction]", direction)]
        if max_records:
            params.append(("maxRecords", str(max_records)))

        records: list[dict[str, Any]] = []
        offset = None
        while True:
            page = self.request("GET", self._table_path(table),
                                params + ([("offset", offset)] if offset else []))
            records += page.get("records", [])
            offset = page.get("offset")
            if not offset:
                return records

    def _record_path(self, table: str, record_id: str) -> str:
        return f"{self._table_path(table)}/{urllib.parse.quote(check_record_id(record_id), safe='')}"

    def get_record(self, table: str, record_id: str) -> dict[str, Any]:
        return self.request("GET", self._record_path(table, record_id))

    def create_record(self, table: str, fields: dict[str, Any]) -> dict[str, Any]:
        result = self.request("POST", self._table_path(table),
                              body={"records": [{"fields": fields}], "typecast": True})
        return result["records"][0]

    def update_record(self, table: str, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return self.request("PATCH", self._record_path(table, record_id),
                            body={"fields": fields, "typecast": True})

    # --- schema (Meta API) --------------------------------------------------------

    def list_tables(self) -> list[dict[str, Any]]:
        return self.request("GET", f"meta/bases/{self.base_id}/tables").get("tables", [])

    def create_table(self, name: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
        return self.request("POST", f"meta/bases/{self.base_id}/tables",
                            body={"name": name, "fields": fields})

    def create_field(self, table_id: str, field: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"meta/bases/{self.base_id}/tables/{table_id}/fields", body=field)


# --- schema --------------------------------------------------------------------

PROFILE = "Profil firmy"
SERVICES = "Služby"
LEADS = "Leady"
EMAILS = "E-maily"
TASKS = "Úkoly"
AUDIT = "Audit"

LEAD_STATUS_LABELS = {
    "new": "Nový",
    "qualified": "Kvalifikovaný",
    "drafted": "Návrh připraven",
    "contacted": "Osloven",
    "replied": "Odpověděl",
    "interested": "Má zájem",
    "not_interested": "Nemá zájem",
    "do_not_contact": "Nekontaktovat",
}
DRAFT_PENDING = "Čeká na schválení"
DRAFT_APPROVED = "Schváleno"
SERVICE_ACTIVE = "Aktivní"
SERVICE_INACTIVE = "Neaktivní"
TASK_STATUS_LABELS = {
    "todo": "K vyřízení",
    "in_progress": "Probíhá",
    "done": "Hotovo",
    "cancelled": "Zrušeno",
}


def _text(name: str) -> dict[str, Any]:
    return {"name": name, "type": "singleLineText"}


def _long(name: str) -> dict[str, Any]:
    return {"name": name, "type": "multilineText"}


def _select(name: str, choices: list[str]) -> dict[str, Any]:
    return {"name": name, "type": "singleSelect", "options": {"choices": [{"name": c} for c in choices]}}


def _datetime(name: str, timezone: str) -> dict[str, Any]:
    return {
        "name": name,
        "type": "dateTime",
        "options": {
            "dateFormat": {"name": "european", "format": "D/M/YYYY"},
            "timeFormat": {"name": "24hour", "format": "HH:mm"},
            "timeZone": timezone,
        },
    }


def schema(timezone: str) -> dict[str, list[dict[str, Any]]]:
    """Tables and fields; the first field of each table is its primary field."""
    return {
        PROFILE: [
            _text("Firma"),
            _text("IČO"),
            {"name": "Web", "type": "url"},
            _long("O nás"),
            _long("Čím se lišíme"),
            _long("Ideální zákazník"),
            _long("Koho neoslovovat"),
            _long("Obecné reference"),
            _long("Výzva k akci"),
            _long("Tón e-mailů"),
            _long("Podpis"),
        ],
        # Services before Leady, which links to it.
        SERVICES: [
            _text("Název"),
            _select("Stav", [SERVICE_ACTIVE, SERVICE_INACTIVE]),
            _long("Popis"),
            _long("Pro koho"),
            _long("Přínos pro zákazníka"),
            _text("Cena"),
            _long("Reference"),
            {"name": "Odkaz", "type": "url"},
            _long("Pokyny pro agenta"),
        ],
        LEADS: [
            _text("Firma"),
            _select("Stav", list(LEAD_STATUS_LABELS.values())),
            _text("IČO"),
            {"name": "Web", "type": "url"},
            _text("Doména"),
            {"name": "E-mail", "type": "email"},
            {"name": "Zdroj e-mailu", "type": "url"},
            _text("Kontaktní osoba"),
            _text("Role"),
            _text("Město"),
            _text("Obor"),
            _long("Proč se hodí"),
            _long("Poznámky"),
            {"name": "Služba", "type": "multipleRecordLinks", "options": {"linkedTableId": SERVICES}},
            _text("Předmět návrhu"),
            _long("Text návrhu"),
            _datetime("Návrh upraven", timezone),
            _select("Schválení návrhu", [DRAFT_PENDING, DRAFT_APPROVED]),
            _datetime("Naposledy osloven", timezone),
            _datetime("Vytvořeno", timezone),
            _datetime("Aktualizováno", timezone),
        ],
        EMAILS: [
            _text("Předmět"),
            {"name": "Lead", "type": "multipleRecordLinks", "options": {"linkedTableId": LEADS}},
            _text("Lead ID"),
            {"name": "Komu", "type": "email"},
            _long("Text"),
            _datetime("Odesláno", timezone),
        ],
        TASKS: [
            _text("Název"),
            _select("Stav", list(TASK_STATUS_LABELS.values())),
            {"name": "Termín", "type": "date",
             "options": {"dateFormat": {"name": "european", "format": "D/M/YYYY"}}},
            _long("Poznámka"),
            _datetime("Vytvořeno", timezone),
        ],
        AUDIT: [
            _text("Nástroj"),
            _datetime("Čas", timezone),
            _long("Vstup"),
            _select("Schváleno", ["ano", "ne", "nevyžadováno"]),
            _select("Výsledek", ["ok", "chyba"]),
        ],
    }


def setup_base(client: AirtableClient, timezone: str) -> list[str]:
    """Create missing tables and fields. Returns a human-readable log."""
    log: list[str] = []
    existing = {t["name"]: t for t in client.list_tables()}
    table_ids = {name: t["id"] for name, t in existing.items()}

    for table, fields in schema(timezone).items():
        # Link fields reference tables by id, which we only know after creating them.
        resolved = []
        for field in fields:
            if field["type"] == "multipleRecordLinks":
                field = {**field, "options": {"linkedTableId": table_ids[field["options"]["linkedTableId"]]}}
            resolved.append(field)

        if table not in existing:
            created = client.create_table(table, resolved)
            table_ids[table] = created["id"]
            log.append(f"Vytvořena tabulka {table} ({len(resolved)} polí).")
            continue

        have = {f["name"] for f in existing[table].get("fields", [])}
        missing = [f for f in resolved if f["name"] not in have]
        for field in missing:
            client.create_field(existing[table]["id"], field)
        log.append(
            f"Tabulka {table}: doplněno {len(missing)} polí ({', '.join(f['name'] for f in missing)})."
            if missing else f"Tabulka {table} je v pořádku."
        )
    return log
